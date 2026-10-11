import math
import unittest
from datetime import date, datetime, timezone

from weatherbot.markets import Market, TempEvent, parse_bucket, parse_event, parse_station
from weatherbot.metar import Observation, displayed_bounds, parse_metar
from weatherbot.orderbook import Book, Level, fillable_at, plan_sweep
from weatherbot.strategy import find_signals, guaranteed_extreme


def obs(station, iso, temp_c, tenths=None):
    return Observation(station=station, time=datetime.fromisoformat(iso).astimezone(timezone.utc),
                       temp_c=temp_c, temp_c_tenths=tenths, raw="")


def event(kind, unit, labels, station="LFPB", tz="Europe/Paris", day=date(2026, 10, 10)):
    mks = [Market(question=l, condition_id=f"c{i}", yes_token=f"y{i}", no_token=f"n{i}", bucket=parse_bucket(l))
           for i, l in enumerate(labels)]
    return TempEvent(slug="ev", title="t", kind=kind, city="Paris", station=station, unit=unit,
                     day=day, tz=tz, markets=mks)


class BucketTests(unittest.TestCase):
    def test_formats(self):
        b = parse_bucket("17°C")
        self.assertEqual((b.lo, b.hi, b.unit), (17, 17, "C"))
        b = parse_bucket("60-61°F")
        self.assertEqual((b.lo, b.hi, b.unit), (60, 61, "F"))
        b = parse_bucket("10°C or below")
        self.assertTrue(math.isinf(b.lo) and b.hi == 10)
        b = parse_bucket("78°F or higher")
        self.assertTrue(b.lo == 78 and math.isinf(b.hi))
        b = parse_bucket("-1°C")
        self.assertEqual((b.lo, b.hi), (-1, -1))
        b = parse_bucket("-2°C or below")
        self.assertEqual(b.hi, -2)
        self.assertIsNone(parse_bucket("Other"))

    def test_station(self):
        self.assertEqual(parse_station("https://www.weather.gov/wrh/timeseries?site=lfpb"), "LFPB")
        self.assertEqual(parse_station("", "see https://www.weather.gov/wrh/timeseries?site=LLBG\n"), "LLBG")
        self.assertEqual(parse_station("https://www.wunderground.com/history/daily/tw/taipei/RCSS"), "RCSS")
        self.assertIsNone(parse_station("https://www.weather.gov.hk/en/cis/climat.htm"))

    def test_parse_event(self):
        ev = {
            "title": "Highest temperature in Paris on October 11?",
            "slug": "highest-temperature-in-paris-on-october-11-2026",
            "resolutionSource": "https://www.weather.gov/wrh/timeseries?site=lfpb",
            "negRisk": True,
            "markets": [{
                "groupItemTitle": "11°C", "question": "q", "conditionId": "0xc",
                "clobTokenIds": '["111", "222"]', "outcomes": '["Yes", "No"]',
                "orderPriceMinTickSize": 0.001, "orderMinSize": 5, "acceptingOrders": True,
            }],
        }
        te = parse_event(ev)
        self.assertEqual((te.kind, te.city, te.station, te.unit, te.day), ("high", "Paris", "LFPB", "C", date(2026, 10, 11)))
        self.assertEqual((te.markets[0].yes_token, te.markets[0].no_token), ("111", "222"))


class MetarTests(unittest.TestCase):
    REF = datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)

    def test_international(self):
        o = parse_metar("METAR LFPB 101330Z 24008KT 9999 FEW040 17/09 Q1018 NOSIG", ref=self.REF)
        self.assertEqual((o.station, o.temp_c, o.temp_c_tenths), ("LFPB", 17, None))
        self.assertEqual(o.time, datetime(2026, 10, 10, 13, 30, tzinfo=timezone.utc))

    def test_us_with_tenths(self):
        o = parse_metar("KLGA 101351Z 21008KT 10SM FEW250 18/09 A3012 RMK AO2 SLP199 T01780089", ref=self.REF)
        self.assertEqual((o.temp_c, o.temp_c_tenths), (18, 17.8))

    def test_negative_and_speci(self):
        o = parse_metar("SPECI EFHK 101320Z 36005KT 1/2SM M05/M07 Q1030", ref=self.REF)
        self.assertEqual(o.temp_c, -5)
        o = parse_metar("KORD 101351Z 00000KT 10SM CLR M01/M06 A3030 RMK AO2 T10061056", ref=self.REF)
        self.assertEqual(o.temp_c_tenths, -0.6)

    def test_previous_month_day(self):
        ref = datetime(2026, 11, 1, 0, 10, tzinfo=timezone.utc)
        o = parse_metar("LFPB 312330Z 24008KT 9999 12/09 Q1018", ref=ref)
        self.assertEqual(o.time, datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc))

    def test_missing_temp(self):
        self.assertIsNone(parse_metar("LFPB 101330Z 24008KT 9999 FEW040 Q1018", ref=self.REF))

    def test_displayed_bounds(self):
        o = obs("KLGA", "2026-10-10T13:51:00+00:00", 18, 17.8)   # 64.04°F
        self.assertEqual(displayed_bounds(o, "F"), (64, 64))
        o = obs("KLGA", "2026-10-10T13:51:00+00:00", 18, None)   # 17.5..18.5°C = 63.5..65.3°F
        self.assertEqual(displayed_bounds(o, "F"), (63, 65))
        o = obs("LFPB", "2026-10-10T13:30:00+00:00", 17)
        self.assertEqual(displayed_bounds(o, "C"), (17, 17))


class StrategyTests(unittest.TestCase):
    LABELS_C = ["10°C or below", "11°C", "12°C", "13°C", "14°C", "15°C", "16°C", "17°C", "18°C", "19°C", "20°C or higher"]

    def test_high_dead_buckets(self):
        ev = event("high", "C", self.LABELS_C)
        o = [obs("LFPB", "2026-10-10T09:00:00+02:00", 12), obs("LFPB", "2026-10-10T13:30:00+02:00", 15)]
        sigs = find_signals(ev, o)
        dead = sorted(s.market.bucket.label for s in sigs if s.outcome == "NO")
        self.assertEqual(dead, ["10°C or below", "11°C", "12°C", "13°C", "14°C"])
        self.assertFalse(any(s.outcome == "YES" for s in sigs))

    def test_high_yes_or_higher(self):
        ev = event("high", "C", self.LABELS_C)
        sigs = find_signals(ev, [obs("LFPB", "2026-10-10T15:00:00+02:00", 20)])
        yes = [s for s in sigs if s.outcome == "YES"]
        self.assertEqual([s.market.bucket.label for s in yes], ["20°C or higher"])
        self.assertEqual(len([s for s in sigs if s.outcome == "NO"]), 10)

    def test_low_dead_buckets(self):
        ev = event("low", "C", self.LABELS_C)
        o = [obs("LFPB", "2026-10-10T02:00:00+02:00", 14), obs("LFPB", "2026-10-10T05:30:00+02:00", 12)]
        sigs = find_signals(ev, o)
        dead = sorted((s.market.bucket.label for s in sigs if s.outcome == "NO"), key=lambda l: parse_bucket(l).hi)
        self.assertEqual(dead, ["13°C", "14°C", "15°C", "16°C", "17°C", "18°C", "19°C", "20°C or higher"])

    def test_low_yes_or_below(self):
        ev = event("low", "C", self.LABELS_C)
        sigs = find_signals(ev, [obs("LFPB", "2026-10-10T05:00:00+02:00", 9)])
        self.assertEqual([s.market.bucket.label for s in sigs if s.outcome == "YES"], ["10°C or below"])

    def test_only_same_local_day_counts(self):
        ev = event("high", "C", self.LABELS_C)
        # 23:30 local the day before must be ignored
        sigs = find_signals(ev, [obs("LFPB", "2026-10-09T23:30:00+02:00", 19)])
        self.assertEqual(sigs, [])

    def test_safety_margin(self):
        ev = event("high", "C", self.LABELS_C)
        o = [obs("LFPB", "2026-10-10T13:30:00+02:00", 15)]
        dead = {s.market.bucket.label for s in find_signals(ev, o, safety_margin=1) if s.outcome == "NO"}
        self.assertNotIn("14°C", dead)
        self.assertIn("13°C", dead)

    def test_fahrenheit_ranges(self):
        labels = ["59°F or below", "60-61°F", "62-63°F", "64-65°F", "66-67°F", "68°F or higher"]
        ev = event("high", "F", labels, station="KLGA", tz="America/New_York")
        # 17.8°C = 64.04°F -> 64 is guaranteed, so 62-63 and below are dead, 64-65 is not
        sigs = find_signals(ev, [obs("KLGA", "2026-10-10T13:51:00-04:00", 18, 17.8)])
        dead = {s.market.bucket.label for s in sigs if s.outcome == "NO"}
        self.assertEqual(dead, {"59°F or below", "60-61°F", "62-63°F"})

    def test_extreme_reports_first_time(self):
        ev = event("high", "C", self.LABELS_C)
        o = [obs("LFPB", "2026-10-10T13:00:00+02:00", 16), obs("LFPB", "2026-10-10T14:00:00+02:00", 16)]
        g, t = guaranteed_extreme(ev, o)
        self.assertEqual(g, 16)
        self.assertEqual(t, o[0].time)


class OrderbookTests(unittest.TestCase):
    def book(self, asks, bids=()):
        return Book("t", bids=[Level(p, s) for p, s in bids], asks=[Level(p, s) for p, s in asks])

    def test_sweep_respects_budget_and_edge(self):
        b = self.book([(0.80, 10), (0.90, 10), (0.975, 100), (0.985, 100)])
        plan, why = plan_sweep(b, max_price=0.98, sell_price=0.99, budget_usdc=20, min_size=5,
                               min_edge=0.005, fee_rate=0.05)
        self.assertIsNone(why)
        self.assertEqual(plan.worst_price, 0.975)
        self.assertLessEqual(plan.cost, 20.0001)
        self.assertGreater(plan.est_profit, 0)

    def test_sweep_skips_when_nothing_cheap(self):
        b = self.book([(0.99, 100)])
        self.assertEqual(plan_sweep(b, max_price=0.98, sell_price=0.99, budget_usdc=20, min_size=5,
                                    min_edge=0.005, fee_rate=0.05), (None, "price"))
        self.assertEqual(plan_sweep(self.book([]), max_price=0.98, sell_price=0.99, budget_usdc=20, min_size=5,
                                    min_edge=0.005, fee_rate=0.05), (None, "no_asks"))

    def test_sweep_min_size(self):
        b = self.book([(0.90, 3)])
        self.assertEqual(plan_sweep(b, max_price=0.98, sell_price=0.99, budget_usdc=20, min_size=5,
                                    min_edge=0.005, fee_rate=0.05), (None, "min_size"))

    def test_sweep_reasons_budget_and_edge(self):
        b = self.book([(0.95, 100)])
        # $3 buys 3.15 shares: fewer than the 5-share minimum because of the money, not the book
        self.assertEqual(plan_sweep(b, max_price=0.98, sell_price=0.99, budget_usdc=3, min_size=5,
                                    min_edge=0.005, fee_rate=0.05), (None, "budget"))
        # an ask under the max price that leaves less than min_edge after fees
        self.assertEqual(plan_sweep(self.book([(0.98, 100)]), max_price=0.98, sell_price=0.99, budget_usdc=20,
                                    min_size=5, min_edge=0.02, fee_rate=0.05), (None, "edge"))

    def test_fillable(self):
        b = self.book([], bids=[(0.995, 3), (0.99, 4), (0.98, 100)])
        self.assertEqual(fillable_at(b, 0.99, 5), 5)
        self.assertEqual(fillable_at(b, 0.99, 10), 7)


if __name__ == "__main__":
    unittest.main()


class BacktestParsingTests(unittest.TestCase):
    def test_iem_csv(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "bt", Path(__file__).resolve().parents[1] / "tools" / "backtest_hightemptation.py")
        bt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bt)
        text = ("station,valid,metar\n"
                "LGA,2026-10-09 13:51,KLGA 091351Z 21008KT 10SM FEW250 18/09 A3012 RMK AO2 T01780089\n"
                "LGA,2026-10-09 14:51,M\n"
                "LGA,2026-10-09 15:51,KLGA 091551Z 22009KT 10SM FEW250 19/09 A3010 RMK AO2 T01940089\n")
        obs = bt.parse_iem_csv(text, "KLGA")
        self.assertEqual([o.station for o in obs], ["KLGA", "KLGA"])
        self.assertEqual([o.temp_c_tenths for o in obs], [17.8, 19.4])
        self.assertEqual(obs[0].time.hour, 13)
        self.assertEqual(bt.iem_ids("KLGA"), ["LGA", "KLGA"])
        self.assertEqual(bt.iem_ids("LFPB"), ["LFPB"])


class FailoverTests(unittest.TestCase):
    def test_backup_used_when_primary_fails(self):
        import requests
        from weatherbot.metar import FailoverSource

        class Bad:
            def fetch(self, stations):
                raise requests.ConnectionError("down")

        class Good:
            def fetch(self, stations):
                return ["obs"]

        src = FailoverSource(Bad(), Good(), ("aviationweather", "tgftp"))
        self.assertEqual(src.fetch(["LFPB"]), ["obs"])
        self.assertEqual(src.last_used, "tgftp")
        src2 = FailoverSource(Bad(), Bad(), ("a", "b"))
        with self.assertRaises(requests.ConnectionError):
            src2.fetch(["LFPB"])

    def test_report_time_comes_from_the_report(self):
        # a 00:00Z report must count for the new day even if fetched a bit earlier
        from datetime import datetime, timezone
        o = parse_metar("METAR EGLC 110000Z 24008KT 9999 12/09 Q1018", ref=datetime(2026, 10, 10, 23, 58, tzinfo=timezone.utc))
        self.assertEqual(o.time, datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc))
