"""Timing: tomorrow's market from 23:30 local (item 9), fast polling per station on its own report
minutes (item 10), and keeping the Mac awake / recovering after a sleep (item 3)."""
import dataclasses
import json
import logging
import tempfile
import time as real_time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

from weatherbot.bankroll import Bankroll
from weatherbot.config import Config
from weatherbot.execution import PaperExecutor, Position
from weatherbot.journal import Journal, read_journal
from weatherbot.markets import Market, TempEvent, fetch_events, parse_bucket
from weatherbot.metar import (LEARN_MIN_REPORTS, Observation, ObservationStore, ReportSchedule, TgftpSource,
                              _merge_windows)
from weatherbot.strategy import day_observations, find_signals

UTC = timezone.utc


def ts(*args) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


def make_event(city="Sao Paulo", station="SBGR", tz="America/Sao_Paulo", day=date(2026, 10, 10), kind="high",
               labels=("22°C", "23°C", "24°C", "25°C", "26°C", "27°C", "28°C or higher"), tag="t"):
    mks = [Market(question=lbl, condition_id=f"c{tag}{i}", yes_token=f"y{tag}{i}", no_token=f"n{tag}{i}",
                  bucket=parse_bucket(lbl)) for i, lbl in enumerate(labels)]
    return TempEvent(slug=f"{city}-{kind}-{day}", title="t", kind=kind, city=city, station=station, unit="C",
                     day=day, tz=tz, markets=mks)


def obs(station, when: datetime, temp: int) -> Observation:
    return Observation(station, when, temp, None, f"{station} {when:%d%H%M}Z {temp:02d}/10")


# ----------------------------------------------------------------------------- fakes
class Clock:
    """Stands in for the `time` module inside weatherbot.engine."""

    def __init__(self, t: float) -> None:
        self.t = t
        self.mono = t

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def advance(self, s: float, slept: bool = False) -> None:
        self.t += s
        if not slept:
            self.mono += s

    def __getattr__(self, name):
        return getattr(real_time, name)


def frozen(clock: Clock):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock.t, tz)
    return mock.patch.multiple("weatherbot.engine", time=clock, datetime=FakeDT)


class Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class BookSession:
    """Order books for the paper executor; nothing else is reachable."""

    def __init__(self, books=None):
        self.books = books or {}

    def post(self, url, json=None, timeout=None):
        return Resp([dict(self.books[x["token_id"]], asset_id=x["token_id"]) for x in json
                     if x["token_id"] in self.books])

    def get(self, url, params=None, timeout=None, headers=None):
        return Resp([])


def asks(*levels):
    return {"asks": [{"price": str(p), "size": str(s)} for p, s in levels], "bids": []}


class ListSource:
    """Weather source that returns whatever the test puts in `.reports`."""
    last_used = "aviationweather"

    def __init__(self):
        self.reports: list[Observation] = []
        self.calls: list[list[str]] = []
        self.reprimed = 0

    def fetch(self, stations):
        self.calls.append(list(stations))
        return [o for o in self.reports if o.station in stations]

    def reprime(self):
        self.reprimed += 1


class HotSource:
    def __init__(self):
        self.reports: list[Observation] = []
        self.calls: list[list[str]] = []
        self.fail = False

    def fetch_changed(self, stations):
        self.calls.append(list(stations))
        if self.fail:
            raise requests.ConnectionError("tgftp down")
        out, self.reports = [o for o in self.reports if o.station in stations], []
        return out


class Notes:
    def __init__(self):
        self.calls = []

    def notify(self, kind, title, text, now=None):
        self.calls.append(kind)
        return True


class RunnerRig:
    def __init__(self, d, clock: Clock, events, **over):
        from weatherbot.engine import BotRunner
        self.d = d
        self.clock = clock
        self.cfg = dataclasses.replace(Config(), journal_dir=d, **over)
        self.session = BookSession()
        self.journal = Journal(d)
        self.bank = Bankroll(self.cfg, Path(d) / "state.json")
        self.notes = Notes()
        self.ex = PaperExecutor(self.cfg, self.session, self.journal, self.bank, self.notes)
        self.source, self.hot = ListSource(), HotSource()
        self.events = events
        with mock.patch("weatherbot.engine.make_source", return_value=self.source), \
                mock.patch("weatherbot.engine.make_hot_source", return_value=self.hot):
            self.runner = BotRunner(self.cfg, self.journal, self.bank, session=self.session, executor=self.ex,
                                    notifier=self.notes, geo_check=lambda s: {"blocked": False, "country": "US"})
        self.runner.power_check = lambda: None
        self.fetches = 0

    def _events(self, *a, **kw):
        self.fetches += 1
        return list(self.events)

    def step(self):
        with frozen(self.clock), mock.patch("weatherbot.engine.fetch_events", side_effect=self._events):
            self.runner.step()

    def rows(self, kind):
        return [r for r in read_journal(self.d) if r["type"] == kind]


# ----------------------------------------------------------------------------- item 9
class TomorrowTests(unittest.TestCase):
    def test_watchable_from_2330_local(self):
        tomorrow = make_event(day=date(2026, 10, 11))
        today = make_event(day=date(2026, 10, 10))
        sp = lambda h, m, d=10: datetime(2026, 10, d, h, m, tzinfo=UTC) + timedelta(hours=3)   # Sao Paulo = UTC-3
        self.assertFalse(tomorrow.is_watchable(sp(23, 29)))
        self.assertTrue(tomorrow.is_watchable(sp(23, 30)))
        self.assertTrue(tomorrow.is_tomorrow(sp(23, 30)))
        self.assertTrue(tomorrow.is_watchable(sp(12, 0, 11)))
        self.assertFalse(tomorrow.is_tomorrow(sp(12, 0, 11)))
        self.assertTrue(today.is_watchable(sp(23, 59)))
        self.assertFalse(today.is_watchable(sp(0, 1, 11)))
        self.assertFalse(make_event(day=date(2026, 10, 12)).is_watchable(sp(23, 45)))   # two days ahead: no

    def test_fetch_events_keeps_tomorrow_and_looks_ahead(self):
        def raw(day):
            return {"title": f"Highest temperature in Paris on October {day}?",
                    "slug": f"highest-temperature-in-paris-on-october-{day}-2026",
                    "resolutionSource": "https://www.weather.gov/wrh/timeseries?site=lfpb",
                    "markets": [{"groupItemTitle": "15°C", "question": "q", "conditionId": f"0x{day}",
                                 "clobTokenIds": f'["y{day}", "n{day}"]', "outcomes": '["Yes", "No"]'}]}

        class S:
            def get(self, url, params=None, timeout=None):
                return Resp([raw(10), raw(11), raw(12)])

        paris = lambda h, m: datetime(2026, 10, 10, h, m, tzinfo=UTC) - timedelta(hours=2)   # CEST = UTC+2
        days = lambda evs: sorted(e.day.day for e in evs)
        self.assertEqual(days(fetch_events(S(), paris(15, 0))), [10])
        self.assertEqual(days(fetch_events(S(), paris(23, 31))), [10, 11])
        self.assertEqual(days(fetch_events(S(), paris(23, 25))), [10])
        # refreshed at 23:25 with 10 min look-ahead: tomorrow is already in the list for 23:30
        self.assertEqual(days(fetch_events(S(), paris(23, 25), ahead_s=660)), [10, 11])

    def test_midnight_report_counts_for_tomorrow_only(self):
        today = make_event(day=date(2026, 10, 10), tag="a")
        tomorrow = make_event(day=date(2026, 10, 11), tag="b")
        reports = [obs("SBGR", datetime(2026, 10, 10, 18, 0, tzinfo=UTC), 25),     # 15:00 local today
                   obs("SBGR", datetime(2026, 10, 11, 3, 0, tzinfo=UTC), 27)]      # stamped 00:00 tomorrow
        dead_today = sorted(s.market.bucket.label for s in find_signals(today, reports) if s.outcome == "NO")
        dead_tomorrow = sorted(s.market.bucket.label for s in find_signals(tomorrow, reports) if s.outcome == "NO")
        self.assertEqual(dead_today, ["22°C", "23°C", "24°C"])                  # 27 does not touch today
        self.assertEqual(dead_tomorrow, ["22°C", "23°C", "24°C", "25°C", "26°C"])
        self.assertEqual([o.temp_c for o in day_observations(tomorrow, reports)], [27])

    def test_2359_report_counts_for_today(self):
        today = make_event(day=date(2026, 10, 10), tag="a")
        tomorrow = make_event(day=date(2026, 10, 11), tag="b")
        late = [obs("SBGR", datetime(2026, 10, 11, 2, 59, tzinfo=UTC), 26)]        # 23:59 local today
        self.assertEqual(len([s for s in find_signals(today, late) if s.outcome == "NO"]), 4)
        self.assertEqual(find_signals(tomorrow, late), [])

    def test_runner_buys_tomorrow_at_2355_without_double_counting(self):
        today = make_event(day=date(2026, 10, 10), tag="a")
        tomorrow = make_event(day=date(2026, 10, 11), tag="b")
        clock = Clock(ts(2026, 10, 11, 2, 55, 30))                                 # 23:55:30 in Sao Paulo
        with tempfile.TemporaryDirectory() as d:
            rig = RunnerRig(d, clock, [today, tomorrow])
            rig.source.reports = [obs("SBGR", datetime(2026, 10, 10, 18, 0, tzinfo=UTC), 25),
                                  obs("SBGR", datetime(2026, 10, 11, 3, 0, tzinfo=UTC), 27)]
            rig.session.books = {m.no_token: asks((0.95, 6)) for m in tomorrow.markets + today.markets}
            rig.step()
            self.assertEqual(rig.runner.health["market_refresh_s"], 60)            # near midnight: every minute
            self.assertEqual(rig.source.calls, [["SBGR"]])                          # one station, fetched once
            buys = rig.rows("buy")
            self.assertTrue(buys)
            self.assertTrue(all(b["event"] == tomorrow.slug for b in buys))        # fresh 00:00 report
            self.assertNotIn(today.slug, {b["event"] for b in buys})               # today's kills are old
            self.assertEqual({s["event"] for s in rig.rows("skip") if s["reason"] == "too_old"}, {today.slug})
            signals = {(s["event"], s["bucket"]) for s in rig.rows("signal")}
            self.assertEqual(signals, {(today.slug, b) for b in ("22°C", "23°C", "24°C")}
                             | {(tomorrow.slug, b) for b in ("22°C", "23°C", "24°C", "25°C", "26°C")})
            view = rig.runner.view["events"]
            self.assertEqual(sorted((e["day"], e["tomorrow"]) for e in view),
                             [("2026-10-10", False), ("2026-10-11", True)])
            self.assertEqual([r["station"] for r in rig.runner.health["stations"]], ["SBGR"])
            # one minute later the market list is fetched again (normally only every 10 minutes)
            n = rig.fetches
            clock.advance(30)
            rig.step()
            self.assertEqual(rig.fetches, n)
            clock.advance(31)
            rig.step()
            self.assertEqual(rig.fetches, n + 1)
            self.assertEqual(rig.rows("gap"), [])

    def test_normal_refresh_away_from_midnight(self):
        clock = Clock(ts(2026, 10, 10, 15, 0, 0))                                  # 12:00 in Sao Paulo
        with tempfile.TemporaryDirectory() as d:
            rig = RunnerRig(d, clock, [make_event(day=date(2026, 10, 10))])
            rig.step()
            self.assertEqual(rig.runner.health["market_refresh_s"], 600)
            for _ in range(3):
                clock.advance(30)
                rig.step()
            self.assertEqual(rig.fetches, 1)

    def test_near_local_midnight(self):
        from weatherbot.engine import near_local_midnight
        at = lambda h, m: datetime(2026, 10, 10, h, m, tzinfo=UTC)
        self.assertTrue(near_local_midnight(at(23, 40), ["UTC"]))
        self.assertTrue(near_local_midnight(at(0, 9), ["UTC"]))
        self.assertFalse(near_local_midnight(at(0, 10), ["UTC"]))
        self.assertFalse(near_local_midnight(at(23, 39), ["UTC"]))
        self.assertTrue(near_local_midnight(at(21, 45), ["UTC", "Europe/Paris"]))   # 23:45 in Paris


# ----------------------------------------------------------------------------- item 10
def hour_at(minute: int, second: int = 0, hour: int = 12, day: int = 1) -> float:
    return ts(2026, 10, day, hour, minute, second)


class ScheduleTests(unittest.TestCase):
    def test_seed_windows(self):
        from weatherbot.cities import seed_minutes
        store = ObservationStore()
        hot = lambda st, m, s=0: store.is_hot(st, hour_at(m, s))
        self.assertEqual(store.schedule("LFPB").source, "seed")
        self.assertEqual(store.schedule("LFPB").minutes, [0, 30])
        self.assertTrue(hot("LFPB", 57) and hot("LFPB", 0) and hot("LFPB", 6) and hot("LFPB", 33))
        self.assertFalse(hot("LFPB", 56, 59) or hot("LFPB", 6, 1) or hot("LFPB", 12) or hot("LFPB", 45))
        self.assertTrue(hot("NZWN", 26) and hot("NZWN", 58))
        self.assertFalse(hot("NZWN", 40))
        self.assertTrue(hot("SBGR", 52) and hot("SBGR", 59))
        self.assertFalse(hot("SBGR", 1))
        # Denver: wide, from :52 to :15
        self.assertEqual(store.schedule("KBKF").windows, ((52 * 60, 75 * 60),))
        self.assertTrue(all(hot("KBKF", m) for m in (52, 59, 0, 8, 14)))
        self.assertFalse(hot("KBKF", 16) or hot("KBKF", 40))
        # mainland China by prefix; unknown stations :50/:20
        self.assertEqual(seed_minutes("ZGGG"), (0, 30))
        self.assertEqual(store.schedule("XXXX").source, "default")
        self.assertTrue(hot("XXXX", 48) and hot("XXXX", 18))
        self.assertFalse(hot("XXXX", 0))

    def test_merge_windows_across_the_hour(self):
        self.assertEqual(_merge_windows([(3120, 3660), (0, 540), (360, 900)]), ((3120, 4500),))
        self.assertEqual(_merge_windows([(-180, 360), (1620, 2160)]), ((1620, 2160), (3420, 3960)))
        sched = ReportSchedule.around([0], "seed")
        self.assertEqual(sched.window_start(hour_at(2)), hour_at(57, hour=11))
        self.assertEqual(sched.window_start(hour_at(58)), hour_at(57))
        self.assertIsNone(sched.window_start(hour_at(30)))

    def test_unknown_station_uses_its_own_report_minutes(self):
        store = ObservationStore()
        now = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
        for i in range(20):                                             # Amsterdam reports at :25 and :55
            store.add(obs("EHAM", datetime(2026, 10, 1, 12, 25, tzinfo=UTC) + timedelta(minutes=30 * i), 15), now)
        sched = store.schedule("EHAM")
        self.assertEqual((sched.source, sched.minutes), ("reports", [25, 55]))
        self.assertTrue(store.is_hot("EHAM", hour_at(53)))
        self.assertFalse(store.is_hot("EHAM", hour_at(45)))

    def test_learns_from_reports_seen_live(self):
        store = ObservationStore()
        start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
        now = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
        for i in range(48):                                             # history from the first fetch: not live
            store.add(obs("LFPB", start + timedelta(minutes=30 * i), 15), now, seen_at=now.timestamp())
        self.assertEqual(store.schedule("LFPB").source, "seed")
        t = start + timedelta(hours=24)
        store.add(obs("LFPB", t + timedelta(minutes=45), 16), now, seen_at=t.timestamp() + 2710, live=True)  # SPECI
        for i in range(LEARN_MIN_REPORTS - 1):
            when = t + timedelta(minutes=30 * (i + 2))
            self.assertTrue(store.add(obs("LFPB", when, 15), now, seen_at=when.timestamp() + 70, live=True))
        self.assertEqual(store.schedule("LFPB").source, "seed")         # 5 routine + 1 SPECI: not yet
        when = t + timedelta(minutes=30 * 8)
        store.add(obs("LFPB", when, 15), now, seen_at=when.timestamp() + 130, live=True)
        sched = store.schedule("LFPB")
        self.assertEqual((sched.source, sched.samples, sched.minutes), ("learned", 6, [1, 31]))
        self.assertEqual(sched.delay_s, 70)
        # learned moment :01:10 -> hot from :58:10 to :07:10
        self.assertTrue(store.is_hot("LFPB", hour_at(58, 20, hour=22, day=2)))
        self.assertFalse(store.is_hot("LFPB", hour_at(58, 0, hour=22, day=2)))
        self.assertTrue(store.is_hot("LFPB", hour_at(7, 0, hour=23, day=2)))
        self.assertFalse(store.is_hot("LFPB", hour_at(7, 20, hour=23, day=2)))
        self.assertFalse(store.is_hot("LFPB", hour_at(1, 0, hour=4, day=2)))   # 04:00 report already seen

    def test_hot_ends_once_the_report_is_in(self):
        store = ObservationStore()
        now = datetime(2026, 10, 1, 12, 1, tzinfo=UTC)
        store.add(obs("LFPB", datetime(2026, 10, 1, 11, 30, tzinfo=UTC), 15), now, seen_at=hour_at(31, hour=11))
        self.assertTrue(store.is_hot("LFPB", hour_at(0, 30)))
        store.add(obs("LFPB", datetime(2026, 10, 1, 12, 0, tzinfo=UTC), 16), now, seen_at=hour_at(0, 40), live=True)
        self.assertFalse(store.is_hot("LFPB", hour_at(1)))
        self.assertTrue(store.is_hot("LFPB", hour_at(28)))              # the next window opens at :27

    def test_history_and_late_sightings_teach_nothing(self):
        store = ObservationStore()
        t = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        store.add(obs("LFPB", t, 15), t, seen_at=t.timestamp() + 30)                        # not live
        self.assertEqual(store.delays("LFPB"), [])
        self.assertFalse(store.note_seen("LFPB", t + timedelta(minutes=30), t.timestamp() + 1800 + 1500))
        self.assertTrue(store.note_seen("LFPB", t + timedelta(minutes=30), t.timestamp() + 1800 - 90))
        self.assertEqual(store.delays("LFPB"), [-90])                   # published 90 s before its own time

    def test_station_status(self):
        store = ObservationStore()
        t = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        store.add(obs("LFPB", t, 15), t, seen_at=t.timestamp() + 30, live=True)
        st = store.station_status("LFPB", t + timedelta(minutes=5))
        self.assertEqual((st["station"], st["age_s"], st["cadence_min"], st["publish_delay_s_p50"], st["stale"]),
                         ("LFPB", 300, 30.0, 30, False))
        self.assertEqual((st["report_minutes"], st["schedule"]), ([0, 30], "seed"))
        self.assertFalse(store.station_status("LFPB", t + timedelta(minutes=50))["stale"])
        self.assertTrue(store.station_status("LFPB", t + timedelta(minutes=51))["stale"])   # 1.5 x 30 min + 5

    def test_prune_forgets_old_sightings(self):
        store = ObservationStore()
        t = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        store.add(obs("LFPB", t, 15), t, seen_at=t.timestamp() + 30, live=True)
        store.prune(t + timedelta(hours=49))
        self.assertEqual((store.get("LFPB"), store.delays("LFPB"), store.first_seen["LFPB"]), ([], [], {}))


class TgftpConditionalTests(unittest.TestCase):
    def test_if_modified_since(self):
        class R:
            def __init__(self, code, text="", headers=None):
                self.status_code, self.text, self.headers = code, text, headers or {}

        class S:
            def __init__(self):
                self.sent = []
                self.down = False

            def get(self, url, timeout=None, headers=None):
                if self.down:
                    raise requests.ConnectionError("down")
                self.sent.append((url.rsplit("/", 1)[1], headers))
                if headers and headers.get("If-Modified-Since") == "Sat, 10 Oct 2026 12:01:00 GMT":
                    return R(304)
                return R(200, "2026/10/10 12:00\nLFPB 101200Z 24008KT 9999 17/09 Q1018\n",
                         {"Last-Modified": "Sat, 10 Oct 2026 12:01:00 GMT"})

        s = S()
        src = TgftpSource(s)
        got = src.fetch_changed(["LFPB"])
        self.assertEqual([(o.station, o.temp_c) for o in got], [("LFPB", 17)])
        self.assertEqual(src.fetch_changed(["LFPB"]), [])                 # unchanged: 304
        self.assertEqual(s.sent[1], ("LFPB.TXT", {"If-Modified-Since": "Sat, 10 Oct 2026 12:01:00 GMT"}))
        s.down = True
        with self.assertRaises(requests.ConnectionError):
            src.fetch_changed(["LFPB", "EGLC"])


class HotPollingTests(unittest.TestCase):
    def paris(self):
        return make_event(city="Paris", station="LFPB", tz="Europe/Paris", day=date(2026, 10, 10),
                          labels=("13°C", "14°C", "15°C", "16°C", "17°C"))

    def test_hot_station_every_2s_all_stations_every_poll(self):
        from weatherbot.engine import FAST_LOOP_SECONDS, HOT_POLL_SECONDS
        clock = Clock(ts(2026, 10, 10, 12, 0, 10))
        ev = self.paris()
        london = make_event(city="London", station="EGLC", tz="Europe/London", day=date(2026, 10, 10), tag="l",
                            labels=("13°C", "14°C"))
        with tempfile.TemporaryDirectory() as d:
            rig = RunnerRig(d, clock, [ev, london])
            rig.source.reports = [obs("LFPB", datetime(2026, 10, 10, 11, 30, tzinfo=UTC), 13),
                                  obs("EGLC", datetime(2026, 10, 10, 11, 50, tzinfo=UTC), 12)]
            rig.step()                                                          # first: full fetch, all stations
            self.assertEqual(rig.source.calls, [["EGLC", "LFPB"]])
            self.assertEqual(rig.runner.hot, ["LFPB"])                          # Paris :00 is due; London (:53) is not
            self.assertEqual(rig.hot.calls, [["LFPB"]])
            self.assertEqual(rig.runner.next_wait(clock.t), HOT_POLL_SECONDS)
            self.assertEqual(rig.rows("report_seen"), [])                       # history is not a live sighting
            clock.advance(2)
            rig.step()                                                          # quick look only
            self.assertEqual(len(rig.source.calls), 1)
            self.assertEqual(rig.hot.calls[-1], ["LFPB"])
            # the 12:00 report shows up in the quick look at 12:00:16: signals and buys in the same loop
            rig.session.books = {m.no_token: asks((0.95, 6)) for m in ev.markets}
            clock.advance(4)
            rig.hot.reports = [obs("LFPB", datetime(2026, 10, 10, 12, 0, tzinfo=UTC), 16)]
            rig.step()
            self.assertEqual(len(rig.source.calls), 1)                          # still < POLL_SECONDS
            seen = rig.rows("report_seen")
            self.assertEqual([(r["station"], r["delay_s"], r["source"]) for r in seen], [("LFPB", 16.0, "tgftp")])
            self.assertEqual({r["bucket"] for r in rig.rows("signal")}, {"13°C", "14°C", "15°C"})
            self.assertTrue(rig.rows("buy"))
            self.assertEqual(rig.runner.hot, [])                                # report is in: Paris cools down
            self.assertEqual(rig.runner.next_wait(clock.t), FAST_LOOP_SECONDS)  # first minute after a buy
            clock.advance(10)
            rig.step()
            self.assertEqual(len(rig.source.calls), 2)                          # all stations every 10 s
            row = next(e for e in rig.runner.view["events"] if e["station"] == "LFPB")
            self.assertEqual((row["report_minutes"], row["schedule"], row["delay_s"]), ([0, 30], "seed", 16))

    def test_quick_look_failure_backs_off_without_counting_as_weather_failure(self):
        clock = Clock(ts(2026, 10, 10, 11, 59, 0))
        with tempfile.TemporaryDirectory() as d:
            rig = RunnerRig(d, clock, [self.paris()])
            rig.hot.fail = True
            rig.step()
            h = rig.runner.health
            self.assertEqual((h["hot_ok"], h["metar_ok"], h["metar_fail_streak"]), (False, True, 0))
            self.assertEqual(rig.runner.next_wait(clock.t), rig.cfg.poll_seconds)
            clock.advance(2)
            rig.step()
            self.assertEqual(len(rig.hot.calls), 1)                             # left alone for 60 s
            rig.hot.fail = False
            clock.advance(60)
            rig.step()
            self.assertEqual(len(rig.hot.calls), 2)
            self.assertTrue(rig.runner.health["hot_ok"])

    def test_learned_timing_survives_a_restart(self):
        from weatherbot.engine import BotRunner
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=12)
            for i in range(8):
                t = base + timedelta(minutes=30 * i)
                j.write("report_seen", station="LFPB", obs_time=t.isoformat(), first_seen=t.timestamp() + 95,
                        delay_s=95.0, source="tgftp")
            cfg = dataclasses.replace(Config(), journal_dir=d)
            with mock.patch("weatherbot.engine.make_source", return_value=ListSource()):
                r = BotRunner(cfg, j, Bankroll(cfg, Path(d) / "s.json"), session=BookSession(),
                              geo_check=lambda s: {"blocked": True, "country": "NL"})
            sched = r.store.schedule("LFPB")
            self.assertEqual((sched.source, sched.delay_s, sched.minutes), ("learned", 95, [1, 31]))


# ----------------------------------------------------------------------------- item 3
class WakeTests(unittest.TestCase):
    def test_after_a_sleep_buy_nothing_for_one_loop_and_look_at_everything(self):
        ev = make_event(city="Paris", station="LFPB", tz="Europe/Paris", day=date(2026, 10, 10),
                        labels=("13°C", "14°C", "15°C", "16°C", "17°C"))
        clock = Clock(ts(2026, 10, 10, 0, 10, 0))
        with tempfile.TemporaryDirectory() as d, self.assertLogs("weatherbot", level="WARNING") as logs:
            rig = RunnerRig(d, clock, [ev])
            rig.source.reports = [obs("LFPB", datetime(2026, 10, 10, 0, 0, tzinfo=UTC), 12)]
            rig.step()
            sig = find_signals(ev, [obs("LFPB", datetime(2026, 10, 10, 0, 0, tzinfo=UTC), 14)])[0]
            p = Position(signal=sig, shares=10, cost=9.5, opened_at=clock.t - 3600, sell_after=0,
                         avg_price=0.95, confirmed_at=clock.t - 3590)
            p.next_poll = clock.t + 9999
            rig.ex.positions.append(p)
            clock.advance(2 * 3600 + 14 * 60, slept=True)                       # Mac asleep 00:10 -> 02:24 UTC
            rig.session.books = {m.no_token: asks((0.95, 6)) for m in ev.markets}
            rig.source.reports.append(obs("LFPB", datetime(2026, 10, 10, 2, 0, tzinfo=UTC), 12))
            rig.source.reports.append(obs("LFPB", datetime(2026, 10, 10, 2, 22, tzinfo=UTC), 15))   # fresh SPECI
            rig.step()
            gap = rig.rows("gap")[-1]
            self.assertTrue(gap["slept"])
            self.assertGreater(gap["seconds"], 8000)
            self.assertTrue(any("Mac sliep van" in m for m in logs.output))
            self.assertEqual(rig.source.reprimed, 1)                            # full history again
            self.assertGreater(p.last_poll, 0)                                  # position looked at right away
            self.assertEqual(rig.rows("buy"), [])
            self.assertIn("woke_up", [s["reason"] for s in rig.rows("skip")])
            self.assertEqual(rig.rows("report_seen"), [])                       # published while asleep
            self.assertIn("gap", rig.notes.calls)
            clock.advance(2)
            rig.step()                                                          # next loop: buys again
            self.assertTrue(rig.rows("buy"))
            self.assertIsNone(rig.ex.hold_reason)


    def test_live_exit_that_is_gone_after_a_sleep_is_posted_again(self):
        from test_safety import T0, book, live_rig, signals
        with tempfile.TemporaryDirectory() as d:
            rig = live_rig(d)
            clob = rig.ex.client
            rig.session.books = {"n0": book([(0.90, 100)])}
            rig.ex.on_signals(signals(only={"13°C"}), T0 + 30)
            p = rig.ex.positions[0]
            rig.ex.tick(T0 + 36)
            self.assertEqual(p.sell_order_id, "o1")
            clob.orders["o1"] = {}                                      # the exchange no longer knows it
            now = T0 + 4000                                             # the Mac slept an hour
            rig.ex.wake(now)
            rig.ex.tick(now)
            self.assertEqual(p.sell_order_id, "o1")                     # one empty answer: could be a glitch
            rig.ex.tick(p.next_poll)
            self.assertEqual(p.sell_order_id, "o2")                     # gone twice: the 0.99 ask is back
            self.assertEqual([x[3] for x in clob.posted], ["FAK", "GTC", "GTC"])
            self.assertEqual(clob.cancelled, [])


class PowerTests(unittest.TestCase):
    AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1234)\t100%; charged; 0:00 remaining present: true\n"
    BATT = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1234)\t45%; discharging; 3:10 remaining\n"

    def test_parse(self):
        from weatherbot.power import parse_pmset, power_status
        self.assertEqual(parse_pmset(self.AC), {"on_battery": False, "source": "AC Power", "percent": 100})
        self.assertEqual(parse_pmset(self.BATT)["on_battery"], True)
        self.assertEqual(parse_pmset("Now drawing from 'AC Power'\n")["percent"], None)      # desktop Mac
        self.assertIsNone(parse_pmset(""))
        self.assertIsNone(power_status(platform="linux"))

    def test_runner_warns_when_unplugged(self):
        clock = Clock(ts(2026, 10, 10, 15, 0, 0))
        with tempfile.TemporaryDirectory() as d:
            rig = RunnerRig(d, clock, [])
            from weatherbot.power import parse_pmset
            rig.runner.power_check = lambda: parse_pmset(self.AC)
            rig.step()
            self.assertEqual(rig.notes.calls, [])
            rig.runner.power_check = lambda: parse_pmset(self.BATT)
            clock.advance(2)
            rig.step()
            self.assertEqual(rig.notes.calls, [])                               # looked at every 5 minutes
            rig.runner.last_power -= 300
            rig.step()
            self.assertEqual(rig.runner.health["power"]["on_battery"], True)
            self.assertEqual(rig.notes.calls, ["power"])
            self.assertEqual([r["on_battery"] for r in rig.rows("power")], [False, True])


class AppTests(unittest.TestCase):
    def test_caffeinate_keeps_the_mac_awake(self):
        from weatherbot.app import KEEP_AWAKE_TIP, caffeinate_cmd
        self.assertEqual(caffeinate_cmd(42), ["caffeinate", "-i", "-s", "-m", "-w", "42"])
        self.assertIn("klep open", KEEP_AWAKE_TIP)

    def test_app_nap_off_once(self):
        from weatherbot.app import disable_app_nap
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as d, mock.patch("weatherbot.app.shutil.which", return_value="/usr/bin/defaults"):
            self.assertFalse(disable_app_nap(Path(d), run, platform="linux"))
            self.assertTrue(disable_app_nap(Path(d), run, platform="darwin"))
            self.assertFalse(disable_app_nap(Path(d), run, platform="darwin"))   # only the first launch
        self.assertEqual(calls, [["defaults", "write", "local.weerbot.app", "NSAppSleepDisabled", "-bool", "YES"]])


class JournalTests(unittest.TestCase):
    def test_reads_only_files_that_can_hold_new_rows(self):
        with tempfile.TemporaryDirectory() as d:
            rows = {"2026-10-01": ts(2026, 10, 1, 12), "2026-10-09": ts(2026, 10, 9, 23, 59),
                    "2026-10-10": ts(2026, 10, 10, 1)}
            for day, t in rows.items():
                (Path(d) / f"journal-{day}.jsonl").write_text(json.dumps({"ts": t, "type": "x", "day": day}) + "\n")
            got = [r["day"] for r in read_journal(d, since_ts=ts(2026, 10, 9, 12))]
            self.assertEqual(got, ["2026-10-09", "2026-10-10"])
            self.assertEqual(len(read_journal(d)), 3)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
