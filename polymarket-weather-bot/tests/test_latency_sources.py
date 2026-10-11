"""Extra sources of tools/latency_race.py (MSC AMQP, WIS2 MQTT, FMI WFS), tested offline with sample
payloads built from the documented formats and fake pika / paho-mqtt / requests objects."""
import base64
import contextlib
import gzip
import importlib.util
import io
import json
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "latency_race", Path(__file__).resolve().parents[1] / "tools" / "latency_race.py")
lt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lt)

UTC = timezone.utc


def this_hour() -> datetime:
    """A report time parse_metar resolves to (it only knows the day of month)."""
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def stamp(t: datetime) -> str:
    return t.strftime("%d%H%M")


def swob_xml(t: datetime, air_temp: str | None = "15.5", icao: str = "CYYZ") -> bytes:
    temp = (f'<element name="air_temp" uom="°C" value="{air_temp}">'
            '<qualifier name="qa_summary" uom="unitless" value="100"/></element>') if air_temp is not None else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<om:ObservationCollection xmlns="http://dms.ec.gc.ca/schema/point-observation/2.1"
    xmlns:gml="http://www.opengis.net/gml" xmlns:om="http://www.opengis.net/om/1.0"
    xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <om:member>
    <om:Observation>
      <om:metadata>
        <set>
          <general>
            <author build="build.4" name="MSC-DMS-PG-SWOB" version="4.0"/>
            <dataset name="msc/observation/atmospheric/surface_weather/ca-1.1-ascii"/>
          </general>
          <identification-elements>
            <element name="stn_nam" uom="unitless" value="TORONTO/PEARSON INT'L ONT"/>
            <element name="tc_id" uom="unitless" value="YYZ"/>
            <element name="icao_stn_id" uom="unitless" value="{icao}"/>
            <element name="date_tm" uom="datetime" value="{t:%Y-%m-%dT%H:%M}:00.000Z"/>
          </identification-elements>
        </set>
      </om:metadata>
      <om:samplingTime><gml:TimeInstant><gml:timePosition>{t:%Y-%m-%dT%H:%M}:00.000Z</gml:timePosition></gml:TimeInstant></om:samplingTime>
      <om:resultTime><gml:TimeInstant><gml:timePosition>{t:%Y-%m-%dT%H:%M}:00.000Z</gml:timePosition></gml:TimeInstant></om:resultTime>
      <om:result>
        <elements>
          <element name="stn_pres" uom="hPa" value="1001.3"/>
          <element name="max_air_temp_pst1hr" uom="°C" value="15.8"/>
          {temp}
          <element name="dwpt_temp" uom="°C" value="7.2"/>
        </elements>
      </om:result>
    </om:Observation>
  </om:member>
</om:ObservationCollection>
""".encode("utf-8")


def canadian_sa(t: datetime) -> bytes:
    """Traditional layout: heading, 'METAR' on its own line, reports wrapped over lines, CR CR LF."""
    s = stamp(t)
    lines = ["\x01", "123", f"SACN31 CWAO {s}", "METAR",
             f"CYYZ {s}Z 27012G20KT 15SM FEW040 BKN250 M02/M08 A2992 RMK",
             "     CU1CI5 SLP134=",
             f"CYTZ {s}Z 26010KT 15SM FEW040 M01/M07 A2991=", "", "\x03"]
    return "\r\r\n".join(lines).encode("latin-1")


def kma_sa(t: datetime) -> bytes:
    s = stamp(t)
    return "\r\r\n".join([
        f"SAKO31 RKSL {s}", f"METAR {s}Z",
        f"METAR RKSI {s}Z 04004KT CAVOK 17/13 Q1026 NOSIG=",
        f"METAR RKSS {s}Z 36003KT 9999 FEW040 18/12 Q1025 NOSIG=",
        f"METAR RKPK {s}Z 02006KT 9999 FEW030 19/12 Q1024",
        "      NOSIG=", ""]).encode("ascii")


def wis2_notice(t: datetime, href: str | None = None, content: dict | None = None, tt: str = "SAKO31") -> bytes:
    name = f"A_{tt}RKSL{stamp(t)}_C_RKSL_{t:%Y%m%d%H%M}30.txt"
    n = {
        "id": "5d3c2a46-0a9b-4c1e-9f0e-3d2b8f4a1c77",
        "conformsTo": ["http://wis.wmo.int/spec/wnm/1/conf/core"],
        "type": "Feature", "geometry": None,
        "properties": {
            "data_id": f"kr-kma/data/core/weather/aviation/{name}",
            "metadata_id": "urn:wmo:md:kr-kma:core.weather.aviation",
            "pubtime": f"{t:%Y-%m-%dT%H:%M}:41Z", "datetime": f"{t:%Y-%m-%dT%H:%M}:00Z",
            "integrity": {"method": "sha512", "value": "AAAA"},
        },
        "links": [{"href": "https://example.org/other", "rel": "via", "type": "text/html"},
                  {"href": href or f"https://wis2-node.example/kr-kma/{name}", "rel": "canonical",
                   "type": "text/plain", "length": 412}],
    }
    if content is not None:
        n["properties"]["content"] = content
    return json.dumps(n).encode()


def fmi_iwxxm21(t: datetime, temp: str = "11.0") -> bytes:
    iso = f"{t:%Y-%m-%dT%H:%M}:00Z"
    later = f"{t + timedelta(minutes=5):%Y-%m-%dT%H:%M}:00Z"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection timeStamp="{later}" numberMatched="1" numberReturned="1"
    xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:avi="http://xml.fmi.fi/namespace/aviation-weather/2014/02/15"
    xmlns:iwxxm="http://icao.int/iwxxm/2.1" xmlns:om="http://www.opengis.net/om/2.0"
    xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:aixm="http://www.aixm.aero/schema/5.1.1"
    xmlns:sams="http://www.opengis.net/samplingSpatial/2.0" xmlns:sam="http://www.opengis.net/sampling/2.0"
    xmlns:xlink="http://www.w3.org/1999/xlink">
  <wfs:member>
    <avi:VerifiableMessage gml:id="vm-1">
      <avi:metadata><avi:MessageMetadata><avi:validTime><gml:TimePeriod gml:id="vt-1">
        <gml:beginPosition>{iso}</gml:beginPosition><gml:endPosition>{later}</gml:endPosition>
      </gml:TimePeriod></avi:validTime></avi:MessageMetadata></avi:metadata>
      <avi:message>
        <iwxxm:METAR gml:id="metar-EFHK" status="NORMAL" automatedStation="false">
          <iwxxm:observation>
            <om:OM_Observation gml:id="obs-1">
              <om:type xlink:href="http://codes.wmo.int/49-2/observation-type/IWXXM/1.0/MeteorologicalAerodromeObservation"/>
              <om:phenomenonTime><gml:TimeInstant gml:id="ti-1"><gml:timePosition>{iso}</gml:timePosition></gml:TimeInstant></om:phenomenonTime>
              <om:resultTime xlink:href="#ti-1"/>
              <om:featureOfInterest><sams:SF_SpatialSamplingFeature gml:id="sp-1"><sam:sampledFeature>
                <aixm:AirportHeliport gml:id="ad-EFHK"><aixm:timeSlice><aixm:AirportHeliportTimeSlice gml:id="ts-1">
                  <gml:validTime/><aixm:interpretation>SNAPSHOT</aixm:interpretation>
                  <aixm:designator>EFHK</aixm:designator><aixm:name>HELSINKI-VANTAA</aixm:name>
                  <aixm:locationIndicatorICAO>EFHK</aixm:locationIndicatorICAO>
                </aixm:AirportHeliportTimeSlice></aixm:timeSlice></aixm:AirportHeliport>
              </sam:sampledFeature></sams:SF_SpatialSamplingFeature></om:featureOfInterest>
              <om:result>
                <iwxxm:MeteorologicalAerodromeObservationRecord gml:id="rec-1" cloudAndVisibilityOK="false">
                  <iwxxm:airTemperature uom="Cel">{temp}</iwxxm:airTemperature>
                  <iwxxm:dewpointTemperature uom="Cel">7.0</iwxxm:dewpointTemperature>
                  <iwxxm:qnh uom="hPa">1018.0</iwxxm:qnh>
                  <iwxxm:rvr><iwxxm:AerodromeRunwayVisualRange><iwxxm:runway><aixm:RunwayDirection gml:id="rd-1">
                    <aixm:timeSlice><aixm:RunwayDirectionTimeSlice gml:id="rdts-1"><gml:validTime/>
                    <aixm:interpretation>SNAPSHOT</aixm:interpretation><aixm:designator>04R</aixm:designator>
                    </aixm:RunwayDirectionTimeSlice></aixm:timeSlice></aixm:RunwayDirection></iwxxm:runway>
                  </iwxxm:AerodromeRunwayVisualRange></iwxxm:rvr>
                </iwxxm:MeteorologicalAerodromeObservationRecord>
              </om:result>
            </om:OM_Observation>
          </iwxxm:observation>
          <iwxxm:trendForecast>
            <om:OM_Observation gml:id="trend-1">
              <om:phenomenonTime><gml:TimePeriod gml:id="tp-1"><gml:beginPosition>{iso}</gml:beginPosition>
                <gml:endPosition>{later}</gml:endPosition></gml:TimePeriod></om:phenomenonTime>
              <om:resultTime><gml:TimeInstant gml:id="ti-2"><gml:timePosition>{later}</gml:timePosition></gml:TimeInstant></om:resultTime>
            </om:OM_Observation>
          </iwxxm:trendForecast>
        </iwxxm:METAR>
      </avi:message>
    </avi:VerifiableMessage>
  </wfs:member>
</wfs:FeatureCollection>
""".encode("utf-8")


FMI_IWXXM30 = b"""<?xml version="1.0" encoding="UTF-8"?>
<iwxxm:SPECI xmlns:iwxxm="http://icao.int/iwxxm/3.0" xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:aixm="http://www.aixm.aero/schema/5.1.1" gml:id="speci-1" reportStatus="NORMAL" automatedStation="false">
  <iwxxm:issueTime><gml:TimeInstant gml:id="ti-1"><gml:timePosition>2026-01-10T08:41:00Z</gml:timePosition></gml:TimeInstant></iwxxm:issueTime>
  <iwxxm:aerodrome><aixm:AirportHeliport gml:id="ad-1"><aixm:timeSlice><aixm:AirportHeliportTimeSlice gml:id="ts-1">
    <gml:validTime/><aixm:interpretation>SNAPSHOT</aixm:interpretation><aixm:designator>EFHK</aixm:designator>
  </aixm:AirportHeliportTimeSlice></aixm:timeSlice></aixm:AirportHeliport></iwxxm:aerodrome>
  <iwxxm:observationTime><gml:TimeInstant gml:id="ti-2"><gml:timePosition>2026-01-10T08:40:00Z</gml:timePosition></gml:TimeInstant></iwxxm:observationTime>
  <iwxxm:observation><iwxxm:MeteorologicalAerodromeObservation gml:id="obs-1" cloudAndVisibilityOK="false">
    <iwxxm:airTemperature uom="Cel">-3</iwxxm:airTemperature>
    <iwxxm:dewpointTemperature uom="Cel">-5</iwxxm:dewpointTemperature>
  </iwxxm:MeteorologicalAerodromeObservation></iwxxm:observation>
</iwxxm:SPECI>
"""


class _Base(unittest.TestCase):
    def setUp(self):
        for d in (lt.seen, lt.temps, lt.details, lt.errors, lt.headings, lt._logged_errors):
            d.clear()
        patcher = mock.patch.object(lt, "log", lambda msg: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sources(self, station, t):
        return set(lt.seen.get((station, t), {}))


class HelperTests(_Base):
    def test_round_half_up(self):
        self.assertEqual([lt.round_half_up(x) for x in (15.5, 14.9, 15.4, 0.5, -0.4, -1.5, -2.6)],
                         [16, 15, 15, 1, 0, -1, -3])

    def test_utc_minute(self):
        want = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)
        for s in ("2026-10-10T14:00:00.000Z", "2026-10-10T14:00:00Z", "2026-10-10T14:00Z", "2026-10-10 14:00:59+00:00"):
            self.assertEqual(lt.utc_minute(s), want, s)
        for s in ("", "2026-10-10", "2026-13-10T14:00:00Z", "2026-10-10T14:00:00+02:00"):
            self.assertIsNone(lt.utc_minute(s), s)

    def test_join_url(self):
        for base, rel in (("https://dd.weather.gc.ca", "/a/b.xml"), ("https://dd.weather.gc.ca/", "a/b.xml"),
                          ("https://dd.weather.gc.ca/", "/a/b.xml")):
            self.assertEqual(lt.join_url(base, rel), "https://dd.weather.gc.ca/a/b.xml")

    def test_wmo_heading(self):
        self.assertEqual(lt.wmo_heading("https://x.example/kr/A_SAKO31RKSL080000_C_RKSL_20261008000030.txt"), "SAKO31")
        self.assertEqual(lt.wmo_heading("kr-kma/data/core/weather/aviation/FTKO31_RKSL_080500"), "FTKO31")
        self.assertIsNone(lt.wmo_heading("https://x.example/DATA01/metar_latest.txt"))
        self.assertIsNone(lt.wmo_heading(""))

    def test_xml_with_dtd_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
        with self.assertRaises(ValueError):
            lt.parse_swob(bomb)

    def test_get_limited(self):
        class Resp:
            def __init__(self, chunks):
                self.chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, n):
                return iter(self.chunks)

        class S:
            def __init__(self, chunks):
                self.chunks, self.calls = chunks, []

            def get(self, url, **kw):
                self.calls.append((url, kw))
                return Resp(self.chunks)

        s = S([b"abc", b"def"])
        self.assertEqual(lt.get_limited(s, "https://dd.weather.gc.ca/x"), b"abcdef")
        self.assertTrue(s.calls[0][1]["stream"])
        with self.assertRaises(ValueError):
            lt.get_limited(S([b"x" * 10, b"x" * 10]), "https://dd.weather.gc.ca/x", max_bytes=15)
        with self.assertRaises(ValueError):
            lt.get_limited(s, "file:///etc/passwd")

    def test_hours_option(self):
        self.assertEqual(lt.parse_args(["--hours", "5"]).minutes, 300)
        self.assertEqual(lt.parse_args(["--minutes", "0.2"]).minutes, 0.2)
        self.assertEqual(lt.parse_args([]).minutes, 35)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                lt.parse_args(["--hours", "1", "--minutes", "5"])
            with self.assertRaises(SystemExit):
                lt.parse_args(["--minutes", "-1"])


class BulletinTests(_Base):
    def test_canadian_traditional_layout(self):
        t = this_hour()
        reps = lt.bulletin_reports(canadian_sa(t).decode("latin-1"), "CYYZ", "SA")
        self.assertEqual(reps, [f"METAR CYYZ {stamp(t)}Z 27012G20KT 15SM FEW040 BKN250 M02/M08 A2992 RMK CU1CI5 SLP134"])
        o = lt.parse_metar(reps[0], ref=t + timedelta(minutes=2))
        self.assertEqual((o.station, o.time, o.temp_c), ("CYYZ", t, -2))
        self.assertEqual(lt.bulletin_reports(canadian_sa(t).decode("latin-1"), "CYOW", "SA"), [])

    def test_kma_layout_and_wrapped_line(self):
        t = this_hour()
        text = kma_sa(t).decode()
        self.assertEqual(lt.bulletin_reports(text, "RKSI", "SA"),
                         [f"METAR RKSI {stamp(t)}Z 04004KT CAVOK 17/13 Q1026 NOSIG"])
        self.assertEqual(lt.bulletin_reports(text, "RKPK", "SA"),
                         [f"METAR RKPK {stamp(t)}Z 02006KT 9999 FEW030 19/12 Q1024 NOSIG"])

    def test_speci_cor_nil_and_missing_terminator(self):
        t = this_hour().replace(minute=23)
        s = stamp(t)
        sp = f"SPCN31 CWAO {s}\nSPECI COR CYYZ {s}Z 28015G25KT 10SM -SHRA BKN030 OVC080 14/09 A2995 RMK SC5AC3=\n"
        self.assertEqual(lt.bulletin_reports(sp, "CYYZ", "SP"),
                         [f"SPECI CYYZ {s}Z 28015G25KT 10SM -SHRA BKN030 OVC080 14/09 A2995 RMK SC5AC3"])
        bare = f"SPCN31 CWAO {s}\nCYYZ {s}Z 28015KT 10SM 14/09 A2995\n"   # type comes from the heading
        self.assertEqual(lt.bulletin_reports(bare, "CYYZ", "SP")[0][:11], "SPECI CYYZ ")
        nil = f"METAR RKPK {s}Z NIL=\n"
        self.assertEqual(lt.bulletin_reports(nil, "RKPK"), [f"METAR RKPK {s}Z NIL"])
        self.assertIsNone(lt.parse_metar(f"METAR RKPK {s}Z NIL"))
        # no '=' anywhere: the next report must not run into this one (its temperature would win)
        run_on = f"METAR RKSI {s}Z 04004KT CAVOK 17/13 Q1026\nMETAR RKPK {s}Z 02006KT 9999 21/12 Q1024\n"
        self.assertEqual(lt.bulletin_reports(run_on, "RKSI"), [f"METAR RKSI {s}Z 04004KT CAVOK 17/13 Q1026"])


class MscTests(_Base):
    SWOB_REL = "/{d}/WXO-DD/observations/swob-ml/{d}/CYYZ/{t:%Y-%m-%d-%H%M}-CYYZ-MAN-swob.xml"
    SA_REL = "/{d}/WXO-DD/bulletins/alphanumeric/{d}/SA/CWAO/{t:%H}/SACN31_CWAO_{s}___40123"
    US_REL = "/{d}/WXO-DD/bulletins/alphanumeric/{d}/SA/KWBC/{t:%H}/SAUS70_KWBC_{s}___40124"

    def rel(self, tmpl, t):
        return tmpl.format(d=t.strftime("%Y%m%d"), t=t, s=stamp(t))

    def body(self, tmpl, t):
        return f"{t:%Y%m%d%H%M}12.345 https://dd.weather.gc.ca {self.rel(tmpl, t)}".encode()

    def test_notice_formats(self):
        t = this_hour()
        pub, url = lt.parse_msc_notice(self.body(self.SWOB_REL, t))
        self.assertEqual(url, "https://dd.weather.gc.ca" + self.rel(self.SWOB_REL, t))
        self.assertTrue(pub.endswith("12.345"))
        v03 = json.dumps({"pubTime": "20261010T140112.345", "baseUrl": "https://dd.weather.gc.ca/",
                          "relPath": self.rel(self.SA_REL, t).lstrip("/"), "integrity": {"method": "md5"}})
        self.assertEqual(lt.parse_msc_notice(v03.encode())[1], "https://dd.weather.gc.ca" + self.rel(self.SA_REL, t))
        for bad in (b"", b"garbage", b"20261010 ftp://x y", b"{not json", b'{"baseUrl": 1}'):
            self.assertIsNone(lt.parse_msc_notice(bad), bad)

    def test_classify(self):
        t = this_hour()
        base = "https://dd.weather.gc.ca"
        self.assertEqual(lt.classify_msc(base + self.rel(self.SWOB_REL, t), {"CYYZ"}), ("swob", "CYYZ"))
        self.assertEqual(lt.classify_msc(base + self.rel(self.SA_REL, t), {"CYYZ"}), ("bulletin", ("SA", "CWAO")))
        self.assertEqual(lt.classify_msc(base + self.rel(self.US_REL, t), {"CYYZ"}), ("heading", "SA KWBC"))
        sp_old = f"{base}/bulletins/alphanumeric/{t:%Y%m%d}/SP/CWAO/{t:%H}/SPCN31_CWAO_{stamp(t)}___1"
        self.assertEqual(lt.classify_msc(sp_old, {"CYYZ"}), ("bulletin", ("SP", "CWAO")))
        auto = base + self.rel(self.SWOB_REL, t).replace("-MAN-", "-AUTO-minute-")
        self.assertEqual(lt.classify_msc(auto, {"CYYZ"})[0], "other")
        self.assertEqual(lt.classify_msc(base + self.rel(self.SWOB_REL, t), {"CYOW"})[0], "other")

    def test_parse_swob(self):
        t = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)
        self.assertEqual(lt.parse_swob(swob_xml(t)), ("CYYZ", t, 15.5))
        self.assertEqual(lt.parse_swob(swob_xml(t, air_temp=None)), ("CYYZ", t, None))
        self.assertEqual(lt.parse_swob(swob_xml(t, air_temp="MSNG"))[2], None)
        # attribute order does not matter
        swapped = b'<r><element value="2026-10-10T14:00:00.000Z" name="date_tm"/><element value="-0.5" uom="C" name="air_temp"/></r>'
        self.assertEqual(lt.parse_swob(swapped), (None, t, -0.5))
        self.assertIsNone(lt.parse_swob(b"<r/>"))

    def test_feed_records_bulletin_and_swob(self):
        t = this_hour()
        files = {"https://dd.weather.gc.ca" + self.rel(self.SA_REL, t): canadian_sa(t),
                 "https://dd.weather.gc.ca" + self.rel(self.SWOB_REL, t): swob_xml(t, "-2.5")}
        fetched = []

        def fetch(url):
            fetched.append(url)
            return files[url]

        feed = lt.MscFeed(["CYYZ"], fetch)
        for tmpl in (self.SA_REL, self.SWOB_REL, self.US_REL, self.SA_REL):   # last one: a repeat
            feed.on_message(self.body(tmpl, t))
        feed.on_message(b"rubbish")
        self.assertEqual(sorted(fetched), sorted(files))                    # no US download, no repeat
        self.assertEqual(self.sources("CYYZ", t), {"msc-bulletin", "msc-swob"})
        self.assertEqual(lt.temps[("CYYZ", t)], {"msc-bulletin": -2, "msc-swob": -2})   # -2.5 rounds up to -2
        self.assertEqual(lt.details[(("CYYZ", t), "msc-swob")], "-2.5")
        self.assertNotIn(("CYTZ", t), lt.seen)                                # other stations ignored
        self.assertEqual(lt.headings["msc SA KWBC"], 1)
        self.assertEqual(lt.errors["msc-amqp: unreadable notification"], 1)

    def test_feed_survives_bad_files(self):
        t = this_hour()
        feed = lt.MscFeed(["CYYZ"], lambda url: b"<not xml")
        feed.on_message(self.body(self.SWOB_REL, t))
        self.assertEqual(lt.errors["msc-swob: parse ParseError"], 1)

        def boom(url):
            raise OSError("down")
        lt.MscFeed(["CYYZ"], boom).on_message(self.body(self.SA_REL, t))
        self.assertEqual(lt.errors["msc-bulletin: download OSError"], 1)
        foreign = f"1 https://evil.example {self.rel(self.SA_REL, t)}".encode()
        lt.MscFeed(["CYYZ"], boom).on_message(foreign)
        self.assertEqual(lt.errors["msc-amqp: file not on a gc.ca host, skipped"], 1)
        self.assertEqual(self.sources("CYYZ", t), set())
        # a URL urlsplit rejects must not escape the pika callback (that would drop the connection)
        lt.MscFeed(["CYYZ"], boom).on_message(b"1 http://[::1 /x")
        self.assertEqual(lt.errors["msc-amqp: notification ValueError"], 1)

    def test_hourly_download_cap(self):
        t = this_hour()
        fetched = []
        feed = lt.MscFeed(["CYYZ"], lambda url: fetched.append(url) or b"")
        with mock.patch.object(lt, "MSC_MAX_BULLETINS_PER_HOUR", 2):
            for i in range(4):
                feed.on_message(self.body(self.SA_REL, t).replace(b"___40123", f"___{i}".encode()))
            feed.on_message(self.body(self.SWOB_REL, t))        # SWOB files do not count against the cap
        self.assertEqual(len(fetched), 3)
        self.assertEqual(lt.errors["msc-amqp: hourly bulletin download cap reached, skipped"], 2)


class FakePika:
    """Just enough of pika.BlockingConnection to drive run_msc."""

    def __init__(self, bodies, fail=False):
        self.bodies, self.fail, self.conns = bodies, fail, []
        self.ConnectionParameters = lambda **kw: kw
        self.PlainCredentials = lambda user, pw: (user, pw)
        self.SSLOptions = lambda ctx, host: host
        fake = self

        class Channel:
            def __init__(self):
                self.declared, self.binds, self.cb = [], [], None

            def queue_declare(self, queue, **kw):
                self.declared.append((queue, kw))

            def queue_bind(self, queue, exchange, routing_key):
                self.binds.append((queue, exchange, routing_key))

            def basic_consume(self, queue, on_message_callback, auto_ack=False):
                self.cb, self.auto_ack = on_message_callback, auto_ack

        class Conn:
            def __init__(self, params):
                fake.conns.append(self)
                self.params, self.ch, self.sent, self.closed = params, Channel(), False, False
                if fake.fail:
                    raise ConnectionError("connection refused")

            def channel(self):
                return self.ch

            def process_data_events(self, time_limit=0):
                if not self.sent:
                    self.sent = True
                    for b in fake.bodies:
                        self.ch.cb(self.ch, None, None, b)
                time.sleep(min(time_limit, 0.02))

            def close(self):
                self.closed = True

        self.BlockingConnection = Conn


class FakeMqtt:
    """Just enough of paho.mqtt.client to drive run_wis2."""

    def __init__(self, payloads, rc=0, v2=True):
        self.payloads, self.rc, self.clients = payloads, rc, []
        if v2:
            self.CallbackAPIVersion = types.SimpleNamespace(VERSION2="v2")
        fake = self

        class Client:
            def __init__(self, *args, **kw):
                fake.clients.append(self)
                self.args, self.kw, self.subs, self.n = args, kw, [], 0

            def username_pw_set(self, user, pw):
                self.auth = (user, pw)

            def tls_set(self):
                self.tls = True

            def connect(self, host, port, keepalive=60):
                self.host, self.port = host, port

            def subscribe(self, topic, qos=0):
                self.subs.append((topic, qos))

            def loop(self, timeout=1.0):
                self.n += 1
                if self.n == 1:
                    self.on_connect(self, None, {}, fake.rc, None)
                elif self.n == 2:
                    for p in fake.payloads:
                        self.on_message(self, None, types.SimpleNamespace(topic="origin/a/wis2/kr-kma/x", payload=p))
                else:
                    time.sleep(0.02)
                return 0

            def disconnect(self):
                self.disconnected = True

        self.Client = Client


class RunnerTests(_Base):
    def test_msc_runner_subscribes_as_documented(self):
        t = this_hour()
        m = MscTests()
        bodies = [m.body(MscTests.SA_REL, t), m.body(MscTests.SWOB_REL, t)]
        pika = FakePika(bodies)
        files = {"https://dd.weather.gc.ca" + m.rel(MscTests.SA_REL, t): canadian_sa(t),
                 "https://dd.weather.gc.ca" + m.rel(MscTests.SWOB_REL, t): swob_xml(t)}
        with mock.patch.object(lt, "_optional_import", lambda name: pika if name == "pika" else None):
            lt.run_msc(["CYYZ"], time.time() + 0.3, fetch=files.__getitem__)
        conn = pika.conns[0]
        self.assertEqual(conn.params["host"], "dd.weather.gc.ca")
        self.assertEqual((conn.params["port"], conn.params["credentials"]), (5671, ("anonymous", "anonymous")))
        self.assertEqual(conn.params["ssl_options"], "dd.weather.gc.ca")
        queue, kw = conn.ch.declared[0]
        self.assertTrue(queue.startswith("q_anonymous"))
        self.assertTrue(kw["auto_delete"])
        self.assertEqual({b[1] for b in conn.ch.binds}, {"xpublic"})
        self.assertEqual([b[2] for b in conn.ch.binds], [
            "v02.post.*.WXO-DD.observations.swob-ml.*.CYYZ.#",
            "v02.post.*.WXO-DD.bulletins.alphanumeric.*.SA.#",
            "v02.post.*.WXO-DD.bulletins.alphanumeric.*.SP.#"])
        self.assertTrue(conn.ch.auto_ack and conn.closed)
        self.assertEqual(self.sources("CYYZ", t), {"msc-bulletin", "msc-swob"})
        self.assertEqual(lt.temps[("CYYZ", t)]["msc-swob"], 16)

    def test_msc_runner_without_pika_or_connection(self):
        with mock.patch.object(lt, "_optional_import", lambda name: None):
            t0 = time.time()
            lt.run_msc(["CYYZ"], time.time() + 30)
            self.assertLess(time.time() - t0, 1)
        self.assertEqual(lt.errors["msc-amqp: pika not installed (pip install pika), CYYZ AMQP sources skipped"], 1)
        pika = FakePika([], fail=True)
        with mock.patch.object(lt, "_optional_import", lambda name: pika):
            t0 = time.time()
            lt.run_msc(["CYYZ"], time.time() + 0.3)
            self.assertLess(time.time() - t0, 2)                   # waits for the next try only until the end
        self.assertEqual(lt.errors["msc-amqp: ConnectionError (dd.weather.gc.ca)"], 1)

    def test_wis2_runner(self):
        t = this_hour()
        sa_href = "https://wis2-node.example/kr-kma/sa.txt"
        inline = {"encoding": "base64", "value": base64.b64encode(kma_sa(t)).decode()}
        payloads = [wis2_notice(t, href=sa_href),
                    wis2_notice(t, href=sa_href),                          # same file again: no new download
                    wis2_notice(t, href="https://wis2-node.example/kr-kma/ft.txt", tt="FTKO31"),
                    b"not json"]
        fetched = []

        def fetch(url):
            fetched.append(url)
            return kma_sa(t)

        mqtt = FakeMqtt(payloads)
        with mock.patch.object(lt, "_optional_import", lambda name: mqtt if name == "paho.mqtt.client" else None):
            lt.run_wis2(["RKSI", "RKPK"], time.time() + 0.3, fetch=fetch)
        c = mqtt.clients[0]
        self.assertEqual(c.args, ("v2",))
        self.assertTrue(c.kw["client_id"].startswith("weerbot-latency-"))
        self.assertEqual((c.host, c.port, c.auth, c.tls), ("globalbroker.meteo.fr", 8883, ("everyone", "everyone"), True))
        self.assertEqual(c.subs, [("origin/a/wis2/kr-kma/data/core/weather/aviation/#", 1)])
        self.assertEqual(fetched, [sa_href])
        self.assertEqual(self.sources("RKSI", t), {"wis2-kma"})
        self.assertEqual(self.sources("RKPK", t), {"wis2-kma"})
        self.assertNotIn(("RKSS", t), lt.seen)
        self.assertEqual(lt.headings["wis2 FTKO31"], 1)
        self.assertEqual(lt.errors["wis2-kma: unreadable notification"], 1)

        # data inside the notification: nothing is downloaded
        lt.seen.clear()
        feed = lt.Wis2Feed(["RKSI"], lambda url: self.fail("downloaded"))
        feed.on_message("t", wis2_notice(t, content=inline))
        self.assertEqual(self.sources("RKSI", t), {"wis2-kma"})
        gz = {"encoding": "gzip", "value": base64.b64encode(gzip.compress(kma_sa(t))).decode()}
        self.assertEqual(lt.wis2_inline({"content": gz}), kma_sa(t))
        self.assertEqual(lt.wis2_inline({"content": {"encoding": "utf-8", "value": "abc"}}), b"abc")
        self.assertIsNone(lt.wis2_inline({"content": {"encoding": "base64", "value": "%%%"}}))

    def test_wis2_links(self):
        self.assertIsNone(lt.wis2_link({"links": [{"rel": "canonical", "href": "ftp://x/y"}]}))
        self.assertEqual(lt.wis2_link({"links": [{"rel": "update", "href": "https://x/u"},
                                                 {"rel": "canonical", "href": "https://x/c"}]}), "https://x/c")
        self.assertEqual(lt.wis2_link({"links": [{"rel": "update", "href": "https://x/u"}]}), "https://x/u")
        self.assertIsNone(lt.wis2_link({"links": "nope"}))
        # a link urlsplit rejects must not escape the paho callback (that would drop the connection)
        bad = json.dumps({"links": [{"rel": "canonical", "href": "http://[::1"}]}).encode()
        lt.Wis2Feed(["RKSI"], lambda url: self.fail("downloaded")).on_message("t", bad)
        self.assertEqual(lt.errors["wis2-kma: notification ValueError"], 1)

    def test_wis2_runner_refused_or_missing(self):
        mqtt = FakeMqtt([], rc=5, v2=False)       # 5 = not authorised (paho 1.x style client)
        with mock.patch.object(lt, "_optional_import", lambda name: mqtt):
            t0 = time.time()
            lt.run_wis2(["RKSI"], time.time() + 0.3)
            self.assertLess(time.time() - t0, 2)
        self.assertEqual(mqtt.clients[0].args, ())
        self.assertEqual(lt.errors["wis2-kma: ConnectionRefusedError (globalbroker.meteo.fr)"], 1)
        with mock.patch.object(lt, "_optional_import", lambda name: None):
            lt.run_wis2(["RKSI"], time.time() + 30)
        self.assertEqual(
            lt.errors["wis2-kma: paho-mqtt not installed (pip install paho-mqtt), RKSI/RKPK WIS2 source skipped"], 1)


class FmiTests(_Base):
    def test_parse_iwxxm_21(self):
        t = datetime(2026, 10, 10, 8, 20, tzinfo=UTC)
        self.assertEqual(lt.parse_iwxxm(fmi_iwxxm21(t)), [("EFHK", t, 11)])

    def test_parse_iwxxm_30_and_errors(self):
        self.assertEqual(lt.parse_iwxxm(FMI_IWXXM30), [("EFHK", datetime(2026, 1, 10, 8, 40, tzinfo=UTC), -3)])
        empty = b'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" numberReturned="0"/>'
        self.assertEqual(lt.parse_iwxxm(empty), [])
        exc = (b'<ExceptionReport xmlns="http://www.opengis.net/ows/1.1" version="2.0.0"><Exception '
               b'exceptionCode="OperationParsingFailed"><ExceptionText>Invalid icaocode</ExceptionText>'
               b'</Exception></ExceptionReport>')
        with self.assertRaisesRegex(ValueError, "Invalid icaocode"):
            lt.parse_iwxxm(exc)

    def test_polling_windows(self):
        def at(h, m, s=0):
            return datetime(2026, 10, 10, h, m, s, tzinfo=UTC).timestamp()
        self.assertEqual(lt.fmi_wait(at(10, 19)), 0)
        self.assertEqual(lt.fmi_wait(at(10, 28, 59)), 0)
        self.assertEqual(lt.fmi_wait(at(10, 52)), 0)
        self.assertEqual(lt.fmi_wait(at(10, 29)), 20 * 60)
        self.assertEqual(lt.fmi_wait(at(10, 0)), 19 * 60)
        self.assertEqual(lt.fmi_wait(at(10, 59, 30)), 19 * 60 + 30)
        # inside the windows every 10 s = 2 x 60 polls per hour, about 2,900 a day
        polls = sum(1 for k in range(0, 3600, 10) if lt.fmi_wait(at(10, 0) + k) == 0)
        self.assertEqual(polls, 120)

    def test_runner_polls_once_at_start_then_waits_for_window(self):
        t = this_hour().replace(minute=20)

        class S:
            calls = []

            def get(self, url, params=None, timeout=None):
                self.calls.append((url, params))
                return types.SimpleNamespace(status_code=200, content=fmi_iwxxm21(t), raise_for_status=lambda: None)

        s = S()
        with mock.patch.object(lt, "fmi_wait", lambda now: 3600.0):
            lt.run_fmi(["EFHK"], time.time() + 0.3, session=s)
        self.assertEqual(len(s.calls), 1)
        url, params = s.calls[0]
        self.assertEqual(url, "https://opendata.fmi.fi/wfs")
        self.assertEqual(params["storedquery_id"], "fmi::avi::observations::latest::iwxxm")
        self.assertEqual(params["icaocode"], "EFHK")
        self.assertEqual(lt.temps[("EFHK", t)], {"fmi-iwxxm": 11})


class ReportTests(_Base):
    def test_leads_temperature_check_and_report(self):
        t = this_hour()
        k, k2, k3 = ("CYYZ", t), ("RKSI", t), ("EFHK", t)
        base = t.timestamp()
        lt.seen.update({
            k: {"msc-swob": base + 82, "msc-bulletin": base + 150, "noaa-tgftp": base + 460, "aviationweather-api": base + 470},
            k2: {"wis2-kma": base + 45, "noaa-tgftp": base + 280},
            k3: {"fmi-iwxxm": base + 70},
        })
        lt.temps.update({k: {"msc-swob": 16, "msc-bulletin": 15, "noaa-tgftp": 15}, k3: {"fmi-iwxxm": 11}})
        lt.details[(k, "msc-swob")] = "15.5"
        leads, only = lt.lead_vs_ref(lt.seen)
        self.assertEqual(leads["msc-swob"]["CYYZ"], [378])
        self.assertEqual(leads["msc-bulletin"]["CYYZ"], [310])
        self.assertEqual(leads["wis2-kma"]["RKSI"], [235])
        self.assertEqual(only[("fmi-iwxxm", "EFHK")], 1)
        check = lt.temp_check(lt.seen.keys(), lt.temps)
        self.assertEqual(check["msc-swob"], (0, [(k, 16, 15)]))
        self.assertEqual(check["fmi-iwxxm"], (0, []))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lt.report(base - 60)
        text = out.getvalue()
        self.assertRegex(text, r"msc-swob\s+CYYZ\s+1\s+\+378")
        self.assertRegex(text, r"wis2-kma\s+RKSI\s+1\s+\+235")
        self.assertRegex(text, r"wis2-kma\s+RKPK\s+0\s+-")
        self.assertIn("msc-swob 15.5 -> 16, METAR 15", text)
        self.assertIn("Environment and Climate Change Canada", text)
        self.assertIn("CC BY 4.0", text)


if __name__ == "__main__":
    unittest.main()
