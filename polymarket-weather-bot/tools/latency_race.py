"""Which free METAR source delivers new airport reports first?

Polls several sources side by side for a while and records, per station and report, the
moment each source first showed it. Prints how many minutes after the report's own time
each source had it, and how often each source was first.

    python tools/latency_race.py --minutes 35
    python tools/latency_race.py --hours 5

Polled sources (all stations): aviationweather.gov API, aviationweather.gov bulk cache file,
NOAA tgftp station files, Iowa Environmental Mesonet "currents".

Extra sources for a few stations, each in its own thread. They are optional: a missing library
or a failed connection is logged and counted as an error, and the race goes on without it.
  msc-bulletin  CYYZ        MSC Datamart AMQP push (needs pika): Canadian SA/SP bulletins, the
                            CYYZ METAR/SPECI line is parsed like any other METAR
  msc-swob      CYYZ        same AMQP feed: the CYYZ-MAN SWOB-ML file. Not the METAR itself, only
                            its air_temp rounded to a whole degree (.5 up), timed per report
  wis2-kma      RKSI, RKPK  WMO WIS2 Global Broker, MQTT origin notifications for KMA's aviation
                            dataset (needs paho-mqtt); the linked bulletin is parsed
  fmi-iwxxm     EFHK        FMI open data WFS (IWXXM), every 10 s, only at HH:19-28 and HH:49-58
Each source's usage terms are followed: push (no polling, no directory listings) for MSC, one
GET per announced file, the public WIS2 login, polite FMI intervals, an honest User-Agent
(set RACE_CONTACT to add a contact URL to it).

Needs internet access (runs on GitHub Actions).

Data: contains information licensed under the Data Servers End-use Licence of Environment and
Climate Change Canada; Finnish Meteorological Institute open data (CC BY 4.0); Korea
Meteorological Administration aviation bulletins via WMO WIS2 (core data).
"""
from __future__ import annotations

import argparse
import base64
import collections
import csv
import gzip
import importlib
import io
import json
import logging
import math
import os
import re
import ssl
import statistics
import sys
import threading
import time
import textwrap
import uuid
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weatherbot.cities import STATION_MINUTES  # noqa: E402
from weatherbot.markets import fetch_events  # noqa: E402
from weatherbot.metar import parse_metar  # noqa: E402

_CONTACT = re.sub(r"[^\x21-\x7e]", "", os.environ.get("RACE_CONTACT", ""))
UA = {"User-Agent": "weerbot-latency/1.0 (research; low volume" + (f"; +{_CONTACT}" if _CONTACT else "") + ")"}
seen: dict[tuple[str, datetime], dict[str, float]] = collections.defaultdict(dict)
temps: dict[tuple[str, datetime], dict[str, int]] = collections.defaultdict(dict)  # whole degrees C per source
details: dict[tuple[tuple[str, datetime], str], str] = {}  # e.g. the SWOB air_temp before rounding
lock = threading.Lock()
errors = collections.Counter()
headings = collections.Counter()   # announced files we deliberately did not download, by WMO heading
_logged_errors: set[str] = set()

MAX_DOWNLOAD = 2_000_000            # bytes; bulletins and SWOB files are a few kB
PROGRESS_EVERY_S = 1800
REF = "noaa-tgftp"                  # the extra sources are compared against this one


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def note_error(source: str, what: str, exc: BaseException | None = None) -> None:
    key = f"{source}: {what}"
    with lock:
        errors[key] += 1
        first = key not in _logged_errors
        _logged_errors.add(key)
    if first:   # log each kind of error once, the count is in the final report
        log(f"error {key}" + (f" - {str(exc)[:240]}" if exc is not None and str(exc) else ""))


def sleep_until(t: float) -> None:
    d = t - time.time()
    if d > 0:
        time.sleep(d)


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    return s


def record_obs(source: str, station: str, t: datetime, temp_c: int | None = None,
               detail: str | None = None) -> bool:
    """Note the first moment `source` showed the report (station, t). True if it is new for this source."""
    now = time.time()
    key = (station, t)
    with lock:
        by = seen[key]
        if source in by:
            return False
        by[source] = now
        if temp_c is not None:
            temps[key][source] = temp_c
        if detail is not None:
            details[(key, source)] = detail
    if source in NEW_SOURCES:
        log(f"{source}: {station} {t:%d %H:%M}Z first seen {(now - t.timestamp()) / 60:+.1f} min after report time"
            + (f", {temp_c} C" if temp_c is not None else ""))
    return True


def record(source: str, raw: str) -> None:
    o = parse_metar(raw, ref=datetime.now(timezone.utc))
    if not o:
        return
    record_obs(source, o.station, o.time, o.temp_c)


def aw_api(stations: list[str], s: requests.Session) -> None:
    r = s.get("https://aviationweather.gov/api/data/metar",
              params={"ids": ",".join(stations), "format": "json", "hours": 1}, timeout=20)
    r.raise_for_status()
    for row in r.json() or []:
        record("aviationweather-api", row.get("rawOb") or "")


def aw_cache(stations: list[str], s: requests.Session) -> None:
    r = s.get("https://aviationweather.gov/data/cache/metars.cache.csv.gz", timeout=60)
    r.raise_for_status()
    text = gzip.decompress(r.content).decode("utf-8", "replace")
    want = set(stations)
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("raw_text")), 0)
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        if row.get("station_id") in want:
            record("aviationweather-cache", row.get("raw_text") or "")


def tgftp(stations: list[str], s: requests.Session) -> None:
    def one(st):
        r = s.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{st}.TXT", timeout=15)
        if r.status_code == 200:
            lines = [ln for ln in r.text.splitlines() if ln.strip()]
            if len(lines) >= 2:
                record("noaa-tgftp", lines[1])
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(one, stations))


def iem(stations: list[str], s: requests.Session) -> None:
    def one(st):
        sid = st[1:] if len(st) == 4 and st[0] == "K" else st
        r = s.get("https://mesonet.agron.iastate.edu/api/1/currents.json", params={"station": sid}, timeout=20)
        if r.status_code == 200:
            for row in (r.json().get("data") or []):
                raw = row.get("raw") or ""
                if raw:
                    record("iem-currents", raw if raw.split()[0] in ("METAR", "SPECI") or raw[:4] == st else f"{st} {raw}")
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(one, stations))


SOURCES = {"aviationweather-api": (aw_api, 10), "aviationweather-cache": (aw_cache, 30),
           "noaa-tgftp": (tgftp, 15), "iem-currents": (iem, 30)}


def loop(name: str, fn, every: float, stations: list[str], until: float) -> None:
    s = new_session()
    while time.time() < until:
        t0 = time.time()
        try:
            fn(stations, s)
        except Exception as exc:  # noqa: BLE001 - count and keep going
            note_error(name, type(exc).__name__, exc)
        sleep_until(min(until, max(time.time() + 1.0, t0 + every)))


# --- helpers shared by the extra sources -----------------------------------------------------
def _optional_import(name: str):
    """The module, or None when it is not installed (the source is then skipped)."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def join_url(base: str, rel: str) -> str:
    if not base or not rel:
        return base or rel
    return base.rstrip("/") + "/" + rel.lstrip("/")


def get_limited(s: requests.Session, url: str, max_bytes: int = MAX_DOWNLOAD, timeout: float = 15) -> bytes:
    """GET one file (http/https only), refusing anything larger than max_bytes."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"not an http(s) URL: {url[:80]}")
    with s.get(url, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > max_bytes:
                raise ValueError(f"file larger than {max_bytes} bytes")
    return bytes(buf)


def round_half_up(x: float) -> int:
    """Whole degree with .5 going up, as the research notes say the METAR does (15.5 -> 16, 14.9 -> 15)."""
    return math.floor(x + 0.5)


_ISO_RE = re.compile(r"\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\s*(?:Z|[+-]00:?00)?\s*$")


def utc_minute(text: str) -> datetime | None:
    """'2026-10-10T14:00:00.000Z' -> aware UTC datetime to the minute (report keys are whole minutes)."""
    m = _ISO_RE.match(text or "")
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse_xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("XML with a DTD is not accepted")
    return ET.fromstring(data)


# A report runs until '=', the next METAR/SPECI, the next line that starts a report, or a blank line.
_REPORT_END = r"(?==|\n[ \t]*(?:METAR|SPECI)\b|\n[ \t]*[A-Z]{4}[ \t]+\d{6}Z|\n[ \t]*\n|\Z)"


def bulletin_reports(text: str, station: str, tt: str | None = None) -> list[str]:
    """The METAR/SPECI reports for `station` in a WMO bulletin, one line each, ready for parse_metar.

    Handles CR CR LF line ends, reports wrapped over several lines, bulletins where only the second
    line says METAR, and 'METAR COR CYYZ ...'. `tt` (SA or SP) gives the type when a report has none."""
    text = re.sub(r"\r*\n|\r+", "\n", text)                    # CR CR LF -> one line end, not a blank line
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "\n", text)     # SOH, ETX, ...
    default = "SPECI" if (tt or "").upper().startswith("SP") else "METAR"
    st = re.escape(station.upper())
    pat = re.compile(rf"(?:\b(METAR|SPECI)\s+(?:COR\s+)?)?(?<![A-Z0-9]){st}\s+(\d{{6}}Z)(?![A-Z0-9])(.*?){_REPORT_END}",
                     re.S)
    out: list[str] = []
    for m in pat.finditer(text):
        raw = " ".join(f"{m.group(1) or default} {station.upper()} {m.group(2)} {m.group(3)}".split())
        if raw not in out:
            out.append(raw)
    return out


_TT_RE = re.compile(r"^(?:A_)?([A-Z]{4}\d{2})(?![0-9])")


def wmo_heading(name: str) -> str | None:
    """'.../A_SAKO31RKSL080000_C_RKSL_20261008000030.txt' -> 'SAKO31' (None when the file name has none)."""
    base = urlsplit(name or "").path.rstrip("/").rsplit("/", 1)[-1]
    m = _TT_RE.match(base.upper())
    return m.group(1) if m else None


# --- MSC Datamart (Canada): AMQP push --------------------------------------------------------
MSC_HOSTS = ("dd.weather.gc.ca", "hpfx.collab.science.gc.ca")   # second copy of the same feed
MSC_EXCHANGE = "xpublic"
MSC_STATIONS = ("CYYZ",)
MSC_MAX_BULLETINS_PER_HOUR = 3000   # stays below the 86,400 requests/day above which MSC wants to be contacted
_SWOB_RE = re.compile(r"/swob-ml/(?:[^?#]*/)?\d{4}-\d{2}-\d{2}-\d{4}-(?P<st>[A-Z0-9]{3,5})-(?P<kind>[A-Za-z0-9-]+?)-swob\.xml$")
_MSC_BULLETIN_RE = re.compile(r"/bulletins/alphanumeric/(?:\d{8}/)?(?P<tt>S[AP])/(?P<cccc>[A-Z]{4})/(?:\d{2}/)?[^/]+$")


def msc_binding_keys(stations) -> list[str]:
    keys = [f"v02.post.*.WXO-DD.observations.swob-ml.*.{st}.#" for st in stations]
    return keys + ["v02.post.*.WXO-DD.bulletins.alphanumeric.*.SA.#",
                   "v02.post.*.WXO-DD.bulletins.alphanumeric.*.SP.#"]


def parse_msc_notice(body) -> tuple[str, str] | None:
    """(pubTime, file URL) from a v02 body 'pubTime baseUrl relPath' (or a v03 JSON body)."""
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    text = text.strip()
    if text.startswith("{"):
        try:
            d = json.loads(text)
        except ValueError:
            return None
        base, rel = d.get("baseUrl"), d.get("relPath")
        if not isinstance(base, str) or not isinstance(rel, str):
            return None
        return str(d.get("pubTime", "")), join_url(base, rel)
    parts = text.split()
    if len(parts) < 3 or urlsplit(parts[1]).scheme not in ("http", "https"):
        return None
    return parts[0], join_url(parts[1], parts[2])


def classify_msc(url: str, stations) -> tuple[str, str | tuple[str, str] | None]:
    """('swob', station) | ('bulletin', (TT, CCCC)) | ('heading', 'TT CCCC') | ('other', None)."""
    path = urlsplit(url).path
    m = _SWOB_RE.search(path)
    if m:
        if m.group("st") in stations and m.group("kind").upper() == "MAN":
            return "swob", m.group("st")
        return "other", None
    m = _MSC_BULLETIN_RE.search(path)
    if m:
        if m.group("cccc").startswith("C"):          # Canadian-issued: download
            return "bulletin", (m.group("tt"), m.group("cccc"))
        return "heading", f"{m.group('tt')} {m.group('cccc')}"   # log only, no download
    return "other", None


def parse_swob(data: bytes) -> tuple[str | None, datetime, float | None] | None:
    """(ICAO id or None, observation time, air_temp in C or None) from a SWOB-ML file."""
    root = _parse_xml(data)
    vals: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) == "element":
            name = el.get("name")
            if name in ("date_tm", "air_temp", "icao_stn_id") and name not in vals:
                vals[name] = el.get("value") or ""
    t = utc_minute(vals.get("date_tm", ""))
    if t is None:
        return None
    try:
        temp = float(vals["air_temp"])
    except (KeyError, ValueError):
        temp = None
    if temp is not None and not math.isfinite(temp):
        temp = None
    return (vals.get("icao_stn_id") or "").strip().upper() or None, t, temp


def _msc_host_ok(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "gc.ca" or host.endswith(".gc.ca")


class MscFeed:
    """Turns MSC Datamart notifications into recorded reports (msc-bulletin and msc-swob)."""

    def __init__(self, stations, fetch, submit=None) -> None:
        self.stations = {s.upper() for s in stations}
        self.fetch = fetch                       # url -> bytes
        self.submit = submit or (lambda fn, *a: fn(*a))
        self.done: set[str] = set()
        self.hour, self.bulletins_this_hour = -1, 0

    def _bulletin_budget(self) -> bool:
        hour = int(time.time() // 3600)
        if hour != self.hour:
            self.hour, self.bulletins_this_hour = hour, 0
        self.bulletins_this_hour += 1
        return self.bulletins_this_hour <= MSC_MAX_BULLETINS_PER_HOUR

    def on_message(self, body) -> None:
        try:
            self._on_message(body)
        except Exception as exc:  # noqa: BLE001 - an exception here would drop the AMQP connection
            note_error("msc-amqp", f"notification {type(exc).__name__}", exc)

    def _on_message(self, body) -> None:
        notice = parse_msc_notice(body)
        if not notice:
            note_error("msc-amqp", "unreadable notification")
            return
        url = notice[1]
        kind, detail = classify_msc(url, self.stations)
        if kind == "heading":
            with lock:
                headings[f"msc {detail}"] += 1
            return
        if kind not in ("swob", "bulletin") or url in self.done:
            return
        if not _msc_host_ok(url):
            note_error("msc-amqp", "file not on a gc.ca host, skipped")
            return
        if kind == "bulletin" and not self._bulletin_budget():
            note_error("msc-amqp", "hourly bulletin download cap reached, skipped")
            return
        if len(self.done) > 50_000:
            self.done.clear()
        self.done.add(url)
        self.submit(self._download, kind, detail, url)

    def _download(self, kind, detail, url) -> None:
        name = "msc-swob" if kind == "swob" else "msc-bulletin"
        try:
            data = self.fetch(url)
        except Exception as exc:  # noqa: BLE001
            note_error(name, f"download {type(exc).__name__}", exc)
            return
        try:
            if kind == "swob":
                self.handle_swob(data, detail)
            else:
                self.handle_bulletin(data, detail[0])
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the feed
            note_error(name, f"parse {type(exc).__name__}", exc)

    def handle_swob(self, data: bytes, station: str) -> None:
        parsed = parse_swob(data)
        if not parsed:
            note_error("msc-swob", "no date_tm in file")
            return
        icao, t, temp = parsed
        st = icao or station
        if st not in self.stations:
            return
        if temp is None:
            note_error("msc-swob", "no air_temp in file")
            return
        record_obs("msc-swob", st, t, round_half_up(temp), detail=f"{temp:g}")

    def handle_bulletin(self, data: bytes, tt: str | None = None) -> None:
        text = data.decode("latin-1")
        for st in sorted(self.stations):
            for raw in bulletin_reports(text, st, tt):
                record("msc-bulletin", raw)


def msc_params(pika, host: str):
    return pika.ConnectionParameters(
        host=host, port=5671, virtual_host="/",
        credentials=pika.PlainCredentials("anonymous", "anonymous"),
        ssl_options=pika.SSLOptions(ssl.create_default_context(), host),
        heartbeat=300, connection_attempts=1, socket_timeout=10, stack_timeout=20,
        blocked_connection_timeout=120, client_properties={"connection_name": UA["User-Agent"]})


def _backoff(attempt: int) -> float:
    return min(300.0, 15.0 * 2 ** min(attempt, 5))


def run_msc(stations: list[str], until: float, fetch=None) -> None:
    pika = _optional_import("pika")
    if pika is None:
        note_error("msc-amqp", "pika not installed (pip install pika), CYYZ AMQP sources skipped")
        return
    logging.getLogger("pika").setLevel(logging.CRITICAL)    # our own one-line errors are enough
    pool = ThreadPoolExecutor(4)
    if fetch is None:
        s = new_session()
        fetch = lambda url: get_limited(s, url)  # noqa: E731
    feed = MscFeed(stations, fetch, submit=pool.submit)
    attempt = 0
    try:
        while time.time() < until:
            host = MSC_HOSTS[attempt % len(MSC_HOSTS)]
            conn = None
            try:
                conn = pika.BlockingConnection(msc_params(pika, host))
                ch = conn.channel()
                queue = f"q_anonymous.subscribe.weerbot-latency.{uuid.uuid4().hex[:8]}"  # must start with q_anonymous
                ch.queue_declare(queue=queue, auto_delete=True, arguments={"x-expires": 600000})
                for key in msc_binding_keys(stations):
                    ch.queue_bind(queue=queue, exchange=MSC_EXCHANGE, routing_key=key)
                ch.basic_consume(queue=queue, auto_ack=True,
                                 on_message_callback=lambda _ch, _m, _p, body: feed.on_message(body))
                log(f"msc-amqp: connected to {host}, queue {queue}")
                attempt = 0
                while time.time() < until:
                    conn.process_data_events(time_limit=max(0.05, min(1.0, until - time.time())))
            except Exception as exc:  # noqa: BLE001 - log, wait, try again (other host)
                note_error("msc-amqp", f"{type(exc).__name__} ({host})", exc)
                attempt += 1
                sleep_until(min(until, time.time() + _backoff(attempt)))
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


# --- WMO WIS2 Global Broker: KMA origin notifications over MQTT ------------------------------
WIS2_BROKERS = ("globalbroker.meteo.fr", "gb.wis.cma.cn", "wis2globalbroker.nws.noaa.gov")
WIS2_TOPIC = "origin/a/wis2/kr-kma/data/core/weather/aviation/#"
KMA_STATIONS = ("RKSI", "RKPK")


def wis2_link(n: dict) -> str | None:
    """The data link of a WIS2 notification: rel=canonical, else rel=update; http(s) only."""
    links = n.get("links") if isinstance(n, dict) else None
    if not isinstance(links, list):
        return None
    for rel in ("canonical", "update"):
        for ln in links:
            if isinstance(ln, dict) and ln.get("rel") == rel and isinstance(ln.get("href"), str) \
                    and urlsplit(ln["href"]).scheme in ("http", "https"):
                return ln["href"]
    return None


def wis2_inline(props: dict) -> bytes | None:
    """Data embedded in the notification (properties.content), so nothing needs downloading."""
    c = props.get("content") if isinstance(props, dict) else None
    if not isinstance(c, dict) or not isinstance(c.get("value"), str):
        return None
    enc = str(c.get("encoding", "")).lower()
    try:
        if enc == "utf-8":
            return c["value"].encode("utf-8")
        packed = base64.b64decode("".join(c["value"].split()), validate=True)
        if enc == "base64":
            return packed
        if enc == "gzip":   # bounded, so a small payload cannot inflate without limit
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(packed, MAX_DOWNLOAD)
    except (ValueError, OSError, zlib.error):
        return None
    return None


class Wis2Feed:
    """Turns WIS2 notifications for KMA's aviation dataset into recorded reports (wis2-kma)."""

    def __init__(self, stations, fetch, submit=None) -> None:
        self.stations = {s.upper() for s in stations}
        self.fetch = fetch
        self.submit = submit or (lambda fn, *a: fn(*a))
        self.done: set[str] = set()

    def on_message(self, topic: str, payload: bytes) -> None:
        try:
            self._on_message(topic, payload)
        except Exception as exc:  # noqa: BLE001 - an exception here would drop the MQTT connection
            note_error("wis2-kma", f"notification {type(exc).__name__}", exc)

    def _on_message(self, topic: str, payload: bytes) -> None:
        try:
            n = json.loads(payload)
        except (ValueError, TypeError):
            n = None
        if not isinstance(n, dict):
            note_error("wis2-kma", "unreadable notification")
            return
        props = n.get("properties") if isinstance(n.get("properties"), dict) else {}
        link = wis2_link(n)
        tt = wmo_heading(link or "") or wmo_heading(str(props.get("data_id", "")))
        if tt and tt[:2] not in ("SA", "SP"):        # TAF, SIGMET, ...: log only
            with lock:
                headings[f"wis2 {tt}"] += 1
            return
        key = link or str(n.get("id") or props.get("data_id") or "")
        if not key or key in self.done:
            return
        if len(self.done) > 50_000:
            self.done.clear()
        self.done.add(key)
        inline = wis2_inline(props)
        if inline is not None:
            self._handle(inline, tt)
        elif link:
            self.submit(self._download, link, tt)
        else:
            note_error("wis2-kma", "notification without a data link")

    def _download(self, link: str, tt: str | None) -> None:
        try:
            data = self.fetch(link)
        except Exception as exc:  # noqa: BLE001
            note_error("wis2-kma", f"download {type(exc).__name__}", exc)
            return
        self._handle(data, tt)

    def _handle(self, data: bytes, tt: str | None) -> None:
        try:
            text = data.decode("utf-8", "replace")
            for st in sorted(self.stations):
                for raw in bulletin_reports(text, st, tt):
                    record("wis2-kma", raw)
        except Exception as exc:  # noqa: BLE001
            note_error("wis2-kma", f"parse {type(exc).__name__}", exc)


def make_mqtt_client(mqtt):
    cid = f"weerbot-latency-{uuid.uuid4().hex[:8]}"
    api = getattr(mqtt, "CallbackAPIVersion", None)
    return mqtt.Client(api.VERSION2, client_id=cid) if api is not None else mqtt.Client(client_id=cid)


def _mqtt_failed(rc) -> bool:
    failed = getattr(rc, "is_failure", None)
    return bool(failed) if failed is not None else rc != 0


def run_wis2(stations: list[str], until: float, fetch=None) -> None:
    mqtt = _optional_import("paho.mqtt.client")
    if mqtt is None:
        note_error("wis2-kma", "paho-mqtt not installed (pip install paho-mqtt), RKSI/RKPK WIS2 source skipped")
        return
    pool = ThreadPoolExecutor(2)
    if fetch is None:
        s = new_session()
        fetch = lambda url: get_limited(s, url)  # noqa: E731
    feed = Wis2Feed(stations, fetch, submit=pool.submit)
    attempt = 0
    try:
        while time.time() < until:
            host = WIS2_BROKERS[attempt % len(WIS2_BROKERS)]
            client = None
            state: dict = {"refused": None, "up": False}

            def on_connect(c, _userdata, _flags, rc, _properties=None, host=host, state=state):
                if _mqtt_failed(rc):
                    state["refused"] = str(rc)
                    return
                state["up"] = True
                c.subscribe(WIS2_TOPIC, qos=1)
                log(f"wis2-kma: connected to {host}, subscribed to {WIS2_TOPIC}")

            try:
                client = make_mqtt_client(mqtt)
                client.on_connect = on_connect
                client.on_message = lambda _c, _u, msg: feed.on_message(msg.topic, msg.payload)
                client.username_pw_set("everyone", "everyone")   # public WIS2 Global Broker login
                client.tls_set()
                client.connect(host, 8883, keepalive=60)
                while time.time() < until:
                    rc = client.loop(timeout=1.0)
                    if state["refused"]:
                        raise ConnectionRefusedError(f"broker refused the login: {state['refused']}")
                    if rc != 0:
                        raise ConnectionError(f"MQTT connection lost (code {int(rc)})")
                    if state["up"]:
                        attempt = 0
            except Exception as exc:  # noqa: BLE001 - log, wait, try again (next broker)
                note_error("wis2-kma", f"{type(exc).__name__} ({host})", exc)
                attempt += 1
                sleep_until(min(until, time.time() + _backoff(attempt)))
            finally:
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


# --- FMI open data WFS (Finland): IWXXM -------------------------------------------------------
FMI_URL = "https://opendata.fmi.fi/wfs"
FMI_STATIONS = ("EFHK",)
FMI_WINDOWS = ((19, 29), (49, 59))   # minutes past the hour [start, end): EFHK reports at :20 and :50
FMI_EVERY_S = 10.0


def fmi_params(icao: str) -> dict:
    return {"service": "WFS", "version": "2.0.0", "request": "getFeature",
            "storedquery_id": "fmi::avi::observations::latest::iwxxm", "icaocode": icao}


def fmi_wait(now: float) -> float:
    """0 inside a polling window, otherwise the seconds until the next window opens."""
    sec = now % 3600
    if any(a * 60 <= sec < b * 60 for a, b in FMI_WINDOWS):
        return 0.0
    return min((a * 60 - sec) % 3600 for a, _ in FMI_WINDOWS)


def _first_time(el: ET.Element) -> datetime | None:
    for sub in el.iter():
        if _local(sub.tag) == "timePosition" and sub.text:
            t = utc_minute(sub.text)
            if t:
                return t
    return None


def parse_iwxxm(data: bytes) -> list[tuple[str | None, datetime, int | None]]:
    """(ICAO id, observation time, whole-degree temperature) for each METAR/SPECI in an IWXXM response."""
    root = _parse_xml(data)
    if _local(root.tag) == "ExceptionReport":
        text = " ".join(t.strip() for t in root.itertext() if t.strip())
        raise ValueError(f"WFS exception: {text[:200]}")
    out = []
    for rep in root.iter():
        if _local(rep.tag) not in ("METAR", "SPECI"):
            continue
        t = temp = None
        by_name: dict[str, ET.Element] = {}
        ids: dict[str, str] = {}
        for el in rep.iter():
            ln = _local(el.tag)
            by_name.setdefault(ln, el)
            text = (el.text or "").strip().upper()
            if ln in ("locationIndicatorICAO", "designator") and re.fullmatch(r"[A-Z]{4}", text):
                ids.setdefault(ln, text)        # (a runway's designator is never 4 letters)
            elif ln == "airTemperature" and temp is None:
                try:
                    v = float((el.text or "").strip())
                    temp = round_half_up(v) if math.isfinite(v) else None
                except ValueError:
                    pass
        # IWXXM 3: iwxxm:observationTime; IWXXM 2.1: om:phenomenonTime of the observation
        for name in ("observationTime", "phenomenonTime", "issueTime"):
            if name in by_name:
                t = _first_time(by_name[name])
                if t:
                    break
        t = t or _first_time(rep)
        if t is not None:
            out.append((ids.get("locationIndicatorICAO") or ids.get("designator"), t, temp))
    return out


def fmi_once(s: requests.Session, station: str) -> None:
    r = s.get(FMI_URL, params=fmi_params(station), timeout=15)
    r.raise_for_status()
    for st, t, temp in parse_iwxxm(r.content):
        if temp is not None:
            record_obs("fmi-iwxxm", st or station, t, temp)


def run_fmi(stations: list[str], until: float, session: requests.Session | None = None) -> None:
    s = session or new_session()
    first = True
    while time.time() < until:
        wait = 0.0 if first else fmi_wait(time.time())
        if wait > 0:
            sleep_until(min(until, time.time() + wait))
            continue
        first = False
        t0 = time.time()
        for st in stations:
            try:
                fmi_once(s, st)
            except Exception as exc:  # noqa: BLE001 - count and keep going
                note_error("fmi-iwxxm", type(exc).__name__, exc)
        sleep_until(min(until, t0 + FMI_EVERY_S))


# name -> (thread function, stations it covers, sources it records)
EXTRA = {
    "msc-amqp": (run_msc, MSC_STATIONS, ("msc-bulletin", "msc-swob")),
    "wis2-kma": (run_wis2, KMA_STATIONS, ("wis2-kma",)),
    "fmi-iwxxm": (run_fmi, FMI_STATIONS, ("fmi-iwxxm",)),
}
NEW_SOURCES = {src: stations for _fn, stations, srcs in EXTRA.values() for src in srcs}
ALL_SOURCES = list(SOURCES) + list(NEW_SOURCES)
NOT_METAR = ("msc-swob", "fmi-iwxxm")      # temperature only, checked against the METAR's
ATTRIBUTION = (
    "msc-*: contains information licensed under the Data Servers End-use Licence of Environment and Climate "
    "Change Canada (https://eccc-msc.github.io/open-data/licence/readme_en/)",
    "fmi-iwxxm: Finnish Meteorological Institute open data, CC BY 4.0",
    "wis2-kma: Korea Meteorological Administration, WMO WIS2 core data (WMO Unified Data Policy)",
)


def _guarded(name: str, fn, *args) -> None:
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - one broken source must not end the race
        note_error(name, f"crashed {type(exc).__name__}", exc)


def lead_vs_ref(fresh: dict, ref: str = REF) -> tuple[dict, collections.Counter]:
    """{source: {station: [seconds the source was earlier than ref]}} for the extra sources,
    and per (source, station) how many reports ref never showed during the race."""
    leads: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    only = collections.Counter()
    for (st, _t), by in fresh.items():
        for src in NEW_SOURCES:
            if src in by:
                if ref in by:
                    leads[src][st].append(by[ref] - by[src])
                else:
                    only[(src, st)] += 1
    return leads, only


def temp_check(keys, tsnap: dict) -> dict[str, tuple[int, list]]:
    """For each non-METAR source: (agreements, [(key, its value, METAR value), ...])."""
    metar_sources = [s for s in ALL_SOURCES if s not in NOT_METAR]
    out = {}
    for src in NOT_METAR:
        agree, differ = 0, []
        for key in sorted(keys):
            vals = tsnap.get(key, {})
            ref = next((vals[m] for m in metar_sources if m in vals), None)
            if src in vals and ref is not None:
                if vals[src] == ref:
                    agree += 1
                else:
                    differ.append((key, vals[src], ref))
        out[src] = (agree, differ)
    return out


def report(start: float) -> None:
    with lock:
        snap = {k: dict(v) for k, v in seen.items() if v}
        tsnap = {k: dict(v) for k, v in temps.items()}
        dsnap = dict(details)
        heads = collections.Counter(headings)
        errs = collections.Counter(errors)
    # only reports that appeared during the race (not the backlog already there at the start)
    fresh = {k: v for k, v in snap.items() if k[1].timestamp() > start - 300}
    delays = collections.defaultdict(list)
    first = collections.Counter()
    lead_vs_aw = collections.defaultdict(list)
    per_station = collections.defaultdict(list)
    for (st, t), by in fresh.items():
        win = min(by, key=by.get)
        first[win] += 1
        for src, ts in by.items():
            delays[src].append((ts - t.timestamp()) / 60)
        if "aviationweather-api" in by:
            for src, ts in by.items():
                lead_vs_aw[src].append(by["aviationweather-api"] - ts)
        per_station[st].append((win, round((by[win] - t.timestamp()) / 60, 1)))
    print(f"\nnew reports seen during the race: {len(fresh)}")
    print("source                   n   minutes after report time (p10/median/p90)   first   median lead vs aviationweather-api (s)")
    for src in ALL_SOURCES:
        d = sorted(delays.get(src, []))
        if not d:
            print(f"{src:22s}   0")
            continue
        q = lambda p: d[min(len(d) - 1, int(len(d) * p))]  # noqa: E731
        lead = statistics.median(lead_vs_aw[src]) if lead_vs_aw.get(src) else float("nan")
        print(f"{src:22s} {len(d):3d}   {q(.1):+6.1f} / {statistics.median(d):+6.1f} / {q(.9):+6.1f}"
              f"                     {first[src]:4d}    {lead:+7.0f}")
    print("\nper station (winner, minutes after report time):")
    for st in sorted(per_station):
        print(f"  {st}: {per_station[st]}")

    leads, only = lead_vs_ref(fresh)
    print(f"\nextra sources: median lead vs {REF} per station, seconds (+ = earlier than {REF}, "
          f"which is polled every {SOURCES[REF][1]} s)")
    print(f"source         station    n   median      min      max   not seen by {REF}")
    for src, stations in NEW_SOURCES.items():
        for st in stations:
            xs = leads.get(src, {}).get(st, [])
            miss = only.get((src, st), 0)
            if xs:
                print(f"{src:14s} {st:7s} {len(xs):4d}  {statistics.median(xs):+7.0f}  {min(xs):+7.0f}  {max(xs):+7.0f}   {miss:4d}")
            else:
                print(f"{src:14s} {st:7s} {0:4d}        -        -        -   {miss:4d}")

    print("\ntemperature check (not-METAR sources vs the METAR's whole degree):")
    for src, (agree, differ) in temp_check(fresh.keys(), tsnap).items():
        print(f"  {src}: {agree}/{agree + len(differ)} agree")
        for (st, t), v, ref in differ:
            raw = dsnap.get(((st, t), src))
            print(f"    {st} {t:%d %H:%M}Z: {src} {raw + ' -> ' if raw else ''}{v}, METAR {ref}")
    if heads:
        print("\nannounced but not downloaded (WMO heading: count):")
        print(textwrap.fill(", ".join(f"{k}: {n}" for k, n in sorted(heads.items())), 110,
                            initial_indent="  ", subsequent_indent="  "))
    if errs:
        print("\nerrors:", dict(errs))
    print("\ndata attribution:")
    for line in ATTRIBUTION:
        print(f"  {line}")


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Race free METAR sources against each other.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--minutes", type=float, help="how long to race (default 35)")
    g.add_argument("--hours", type=float, help="how long to race, in hours (--hours 5 = --minutes 300)")
    args = ap.parse_args(argv)
    args.minutes = args.hours * 60 if args.hours is not None else (35.0 if args.minutes is None else args.minutes)
    if not args.minutes >= 0:
        ap.error("the duration must be 0 or more")
    return args


def race_stations(s: requests.Session) -> list[str]:
    """Stations of the open markets, plus the stations the extra sources cover."""
    try:
        stations = {e.station for e in fetch_events(s, datetime.now(timezone.utc))}
    except Exception as exc:  # noqa: BLE001 - race the known stations instead
        note_error("polymarket", f"event list {type(exc).__name__}", exc)
        stations = set(STATION_MINUTES)
    extra = {st for _fn, sts, _srcs in EXTRA.values() for st in sts}
    if extra - stations:
        log(f"adding {', '.join(sorted(extra - stations))} for the extra sources")
    return sorted(stations | extra)


def main(argv=None) -> None:
    args = parse_args(argv)
    stations = race_stations(new_session())
    print(f"racing {len(SOURCES) + len(NEW_SOURCES)} sources on {len(stations)} stations for {args.minutes:g} min",
          flush=True)
    start = time.time()
    until = start + args.minutes * 60
    threads = [threading.Thread(target=_guarded, args=(n, loop, n, f, e, stations, until), daemon=True, name=n)
               for n, (f, e) in SOURCES.items()]
    threads += [threading.Thread(target=_guarded, args=(n, fn, [s for s in sts if s in stations], until),
                                 daemon=True, name=n)
                for n, (fn, sts, _srcs) in EXTRA.items()]
    for t in threads:
        t.start()
    next_progress = start + PROGRESS_EVERY_S
    while any(t.is_alive() for t in threads) and time.time() < until + 120:   # stuck threads are left behind
        for t in threads:
            t.join(timeout=5)
        if time.time() >= next_progress:
            next_progress += PROGRESS_EVERY_S
            with lock:   # other threads add keys to both
                counts = collections.Counter(src for by in seen.values() for src in by)
                n_errors = sum(errors.values())
            log(f"progress: reports per source so far {dict(counts)}; errors {n_errors}")
    report(start)


if __name__ == "__main__":
    main()
