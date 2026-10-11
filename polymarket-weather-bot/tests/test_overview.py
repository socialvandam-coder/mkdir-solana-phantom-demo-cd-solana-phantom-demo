"""Overview: the 'Werkt alles?' status bar data (item 0), the 'Vandaag' card on equity (item 1),
the 'why (not) bought' funnel (item 4), the 'HighTempTation vandaag' panel (item 11) and the
settings with safe ranges and a consequence in $ (item 12)."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import requests

from test_safety import T0, Rig, book, live_rig, open_position, runner_rig, signals
from weatherbot import settings as settings_mod
from weatherbot.compare import compare_today, exact_fee_per_share, short_title
from weatherbot.config import Config
from weatherbot.engine import Engine, daily_pcts, local_midnight, percentile, uptime
from weatherbot.execution import skip_labels
from weatherbot.journal import Journal
from weatherbot.metar import Observation


# ----------------------------------------------------------------------------- item 0: uptime
class UptimeTests(unittest.TestCase):
    T0, T1 = 1_000_000.0, 1_000_000.0 + 86400

    def test_running_all_day(self):
        u = uptime([(self.T0 - 3600, "start", None)], self.T0, self.T1, alive=True, last_loop_ts=self.T1 - 1)
        self.assertEqual((u["pct"], u["missed_s"], u["reason"]), (100.0, 0, None))

    def test_app_closed_and_mac_asleep(self):
        marks = [(self.T0 - 3600, "start", None),
                 (self.T0 + 3600, "stop", self.T0 + 3590),            # quit at 01:00
                 (self.T0 + 7200, "start", self.T0 + 3590),           # back at 02:00
                 (self.T0 + 9000, "gap", (self.T0 + 8400, self.T0 + 9000, True))]   # slept 10 min
        u = uptime(marks, self.T0, self.T1, alive=True, last_loop_ts=self.T1)
        self.assertEqual(u["closed_s"], 3600)
        self.assertEqual(u["slept_s"], 600)
        self.assertEqual(u["missed_s"], 4200)
        self.assertEqual(u["reason"], "closed")
        self.assertAlmostEqual(u["pct"], round((86400 - 4200) / 86400 * 100, 1))

    def test_mostly_slept_says_mac_slept(self):
        marks = [(self.T0 - 60, "start", None), (self.T0 + 7200, "gap", (self.T0 + 600, self.T0 + 7200, True))]
        self.assertEqual(uptime(marks, self.T0, self.T1, True, self.T1)["reason"], "slept")

    def test_killed_app_counts_until_its_last_row(self):
        # a start without a stop (killed): that run ends at its last loop row before the next start
        marks = [(self.T0 + 100, "start", None), (self.T0 + 5000, "start", self.T0 + 1100)]
        u = uptime(marks, self.T0, self.T0 + 6000, alive=True, last_loop_ts=self.T0 + 5990)
        self.assertEqual(u["active_s"], 1000 + 1000)
        self.assertEqual(u["closed_s"], 100 + 3900)

    def test_running_since_before_the_journal_window(self):
        # the first row is a stop: it was running from before the rows we read
        marks = [(self.T0 + 600, "stop", self.T0 + 590)]
        u = uptime(marks, self.T0, self.T0 + 1200, alive=False, last_loop_ts=self.T0 + 590)
        self.assertEqual((u["active_s"], u["closed_s"]), (600, 600))
        # no start/stop rows at all, but loop rows: it ran all along
        u = uptime([], self.T0, self.T0 + 1200, alive=True, last_loop_ts=self.T0 + 1190)
        self.assertEqual(u["pct"], 100.0)
        # nothing at all: not running
        u = uptime([], self.T0, self.T0 + 1200, alive=False, last_loop_ts=None)
        self.assertEqual((u["pct"], u["reason"]), (0.0, "closed"))

    def test_stuck_right_now_counts_as_missed(self):
        u = uptime([(self.T0, "start", None)], self.T0, self.T0 + 1000, True, self.T0 + 700, stalled_from=self.T0 + 700)
        self.assertEqual((u["missed_s"], u["reason"]), (300, "stalled"))

    def test_engine_reads_start_stop_gap_from_the_journal(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            mid = local_midnight(now)
            if not 4000 < now - mid < 86000:
                self.skipTest("too close to midnight for this scenario")
            j = Journal(str(Path(d) / "logs"))
            rows = [{"ts": mid - 100, "type": "start"},
                    {"ts": mid + 1000, "type": "signal", "token_id": "a"},
                    {"ts": mid + 1500, "type": "stop"},
                    {"ts": mid + 2500, "type": "start"},
                    {"ts": mid + 3000, "type": "gap", "from": mid + 2700, "to": mid + 3000, "seconds": 300.0,
                     "slept": True},
                    {"ts": mid + 3500, "type": "report_seen", "station": "X"}]
            (j.dir / "journal-x.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            (Path(d) / "state.json").write_text(json.dumps({"start_amount": 100, "started_at": mid - 86400 * 3}))
            eng = Engine(d)
            u = eng.uptime_today(mid + 3600)        # not running in this process: ends at the last loop row
            self.assertEqual(u["closed_s"], 1000 + 100)
            self.assertEqual(u["slept_s"], 300)
            self.assertEqual(u["active_s"], 3600 - 1100 - 300)

    def test_stop_row_only_when_a_loop_ran(self):
        with tempfile.TemporaryDirectory() as d:
            eng = Engine(d)
            eng.stop()
            eng.stop()
            self.assertEqual([r for r in eng.trades if r["type"] == "stop"], [])


# ----------------------------------------------------------------------------- item 0: sources, stations
class SourcesTests(unittest.TestCase):
    def test_sources_and_errors(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("weatherbot.engine.fetch_events", return_value=[]):
            rig = runner_rig(d)
            r = rig.runner
            r.step()
            src = r.health["sources"]
            self.assertEqual(set(src), {"weer", "markten", "orderboek"})
            self.assertEqual(src["markten"]["consecutive_fail"], 0)
            self.assertIsNotNone(src["markten"]["last_ok_ts"])
            # the order book source: failures in a row, then a success resets the count
            rig.session.fail = True
            for _ in range(3):
                self.assertIsNone(rig.ex._fetch_books(["n0"]))
            self.assertEqual(rig.ex.book_status["consecutive_fail"], 3)
            self.assertIn("down", rig.ex.book_status["last_error"])
            rig.session.fail = False
            rig.ex._fetch_books(["n0"])
            self.assertEqual(rig.ex.book_status["consecutive_fail"], 0)
            self.assertIsNotNone(rig.ex.book_status["last_ok_ts"])
            # failing market list
            r.last_refresh = 0
            r._market_retry_at = 0
            with mock.patch("weatherbot.engine.fetch_events", side_effect=requests.ConnectionError("x")):
                r.step()
            self.assertEqual(r.health["sources"]["markten"]["consecutive_fail"], 1)
            errs = r.health["errors"]
            self.assertLessEqual(len(errs), 5)
            self.assertEqual(errs[0]["what"], "markten")          # newest first
            self.assertIn("orderboek", [e["what"] for e in errs])

    def test_station_rows_have_kind_last_reading_and_seen_delay(self):
        from test_safety import make_event
        with tempfile.TemporaryDirectory() as d:
            rig = runner_rig(d)
            r = rig.runner
            ev = make_event()
            now = datetime(2026, 10, 10, 12, 1, 10, tzinfo=timezone.utc)
            obs = Observation("LFPB", datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc), 17, None, "LFPB 101200Z 17/10")
            r.store.add(obs, now, seen_at=now.timestamp(), live=True)
            rows = r._station_rows([ev], now)
            self.assertEqual(rows[0]["kinds"], ["high"])
            self.assertEqual(rows[0]["cities"], ["Paris"])
            self.assertEqual(rows[0]["last_temp_c"], 17)
            self.assertFalse(rows[0]["stale"])
            self.assertEqual(r.health["seen_delay_s_p50"], 70)


# ----------------------------------------------------------------------------- item 1: Vandaag
class TodayEquityTests(unittest.TestCase):
    def test_today_pct_counts_open_positions_at_their_mark(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            p = open_position(rig, T0)                  # 10 shares bought for $9.00
            rig.bank.marks[p.token_id] = 0.30           # the price collapsed: wrong bucket?
            rig.ex._update_open_value(T0 + 120)
            snap = rig.bank.snapshot()
            self.assertAlmostEqual(snap["open_value"], 3.0)
            self.assertAlmostEqual(snap["equity"], 94.0)
            self.assertAlmostEqual(snap["today_pct"], -6.0)          # visible today, not at resolution
            self.assertAlmostEqual(snap["today_realized_pct"], 0.0)
            self.assertAlmostEqual(snap["total_equity_pct"], -6.0)
            self.assertTrue(rig.bank.stopped_for_today())             # 6% daily stop on equity
            self.assertEqual(snap["next_trade_size"], 0.0)

    def test_fresh_position_is_marked_at_its_buy_price(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            p = open_position(rig, T0)
            rig.bank.marks[p.token_id] = 0.30
            rig.ex._update_open_value(T0 + 10)                        # younger than CONFIRM_SECONDS
            self.assertAlmostEqual(rig.bank.snapshot()["today_pct"], 0.0)

    def test_fees_trades_wins_losses_from_the_journal(self):
        with tempfile.TemporaryDirectory() as d:
            eng = Engine(d)
            for row in ({"type": "buy", "fee": 0.05, "token_id": "a", "shares": 5},
                        {"type": "sell", "fee": 0.01, "token_id": "a"},
                        {"type": "close", "pnl": 0.4, "token_id": "a"},
                        {"type": "close", "pnl": -2.0, "token_id": "b"},
                        {"type": "close", "pnl": 0.0, "token_id": "c"}):
                eng.journal.write(row.pop("type"), **row)
            snap = eng.snapshot()
            b = snap["bank"]
            self.assertEqual((b["today_trades"], b["today_wins"], b["today_losses"]), (3, 1, 1))
            self.assertAlmostEqual(b["fees_today"], 0.06)
            self.assertAlmostEqual(snap["today"]["fees"], 0.06)
            for key in ("equity", "open_value", "today_pct", "today_realized_pct", "peak_equity", "drawdown_pct",
                        "next_trade_size"):
                self.assertIn(key, b)

    def test_normal_range_only_after_seven_trading_days(self):
        day = 86400
        base = local_midnight(time.time()) - 20 * day
        hist = [[base, 100.0]]
        bal = 100.0
        for i in range(6):
            bal *= 1.02
            hist.append([base + i * day + 3600, bal])
        today = time.strftime("%Y-%m-%d", time.localtime())
        self.assertEqual(len(daily_pcts(hist, today)), 6)
        with tempfile.TemporaryDirectory() as d:
            eng = Engine(d)
            eng.bank.state["history"] = list(hist)
            self.assertIsNone(eng.normal_range())
            bal *= 0.99
            hist.append([base + 6 * day + 3600, bal])
            hist.append([base + 6 * day + 7200, bal * 1.03])          # day 7: -1% then +3% -> +1.97%
            eng.bank.state["history"] = list(hist)
            lo, hi = eng.normal_range()
            pcts = daily_pcts(hist, today)
            self.assertEqual(len(pcts), 7)
            self.assertAlmostEqual(pcts[-1], (0.99 * 1.03 - 1) * 100, places=6)
            self.assertAlmostEqual(lo, round(percentile(pcts, 0.1), 2))
            self.assertAlmostEqual(hi, round(percentile(pcts, 0.9), 2))
            self.assertLessEqual(lo, hi)


# ----------------------------------------------------------------------------- item 4: funnel
class FunnelTests(unittest.TestCase):
    def test_skip_rows_carry_ask_budget_and_reason(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            rig.session.books = {"n0": book([(0.99, 100)]), "n1": book([(0.95, 2)])}
            rig.ex.on_signals(signals(only={"13°C", "14°C", "15°C"}), T0 + 30)
            skips = {r["token_id"]: r for r in rig.rows("skip")}
            self.assertEqual(skips["n0"]["reason"], "late_no_cheap_asks")     # asks only above the max price
            self.assertEqual(skips["n0"]["best_ask"], 0.99)
            self.assertEqual(skips["n0"]["budget"], 5.0)
            self.assertEqual(skips["n1"]["reason"], "late_no_cheap_asks")     # 2 cheap shares < 5-share minimum
            self.assertEqual(skips["n1"]["sweep"], "min_size")
            self.assertEqual(skips["n2"]["reason"], "no_book")                  # no order book returned
            for key in ("token_id", "city", "kind", "bucket", "outcome", "reason", "best_ask", "budget",
                        "since_signal_s", "obs_age_s"):
                self.assertIn(key, skips["n0"])

    def test_budget_reason_names_the_limit(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d, start_amount=100, trade_pct=5, market_pct=8, event_pct=12)
            open_position(rig, T0)                                # $9 unconfirmed in this market (13°C)
            rig.session.books = {"n1": book([(0.95, 100)])}
            rig.ex.on_signals(signals(only={"14°C"}), T0 + 30)
            # market cap: 12 - 9 = $3, too little for 5 shares at 0.95 -> says which limit
            self.assertEqual(rig.skips(), ["limit_event"])

    def test_live_buy_too_small_is_not_silent(self):
        from weatherbot.orderbook import Book, Level, SweepPlan
        with tempfile.TemporaryDirectory() as d:
            rig = live_rig(d)
            s = signals(only={"13°C"})[0]
            plan = SweepPlan(shares=1.0, cost=0.95, avg_price=0.95, worst_price=0.95, est_profit=0.01)
            rig.ex.buy(s, plan, Book("n0", [], [Level(0.95, 1)]), T0 + 30)
            self.assertEqual(rig.skips(), ["budget_too_small"])

    def test_engine_funnel_counts_each_bucket_once_and_leaves_bought_ones_out(self):
        with tempfile.TemporaryDirectory() as d:
            eng = Engine(d)
            w = eng.journal.write
            for tok in ("a", "b", "c", "d"):
                w("signal", token_id=tok, city="Paris", kind="high", bucket="13°C", outcome="NO", lag_s=3)
            w("signal", token_id="a", city="Paris")                               # seen again after a restart
            w("opportunity", token_id="a")
            w("buy", token_id="a", shares=5, fee=0.01)
            w("sell", token_id="a", fee=0.0)
            w("skip", token_id="a", reason="already_holding")                     # bought: not a 'why not'
            w("skip", token_id="b", reason="late_no_cheap_asks")
            w("skip", token_id="b", reason="late_no_cheap_asks")                  # again after a restart
            w("skip", token_id="c", reason="too_old")
            w("opportunity", token_id="d")
            w("buy", token_id="d", shares=0, fee=0)                               # live FAK not filled
            w("skip", token_id="d", reason="fak_unfilled")
            snap = eng.snapshot()
            f = snap["today"]["funnel"]
            self.assertEqual((f["signalen"], f["met_goedkope_orders"], f["gekocht"], f["verkocht"]), (4, 2, 1, 1))
            self.assertEqual(f["skips"], {"late_no_cheap_asks": 1, "too_old": 1, "fak_unfilled": 1})
            self.assertEqual(eng.counts[time.strftime("%Y-%m-%d")]["skip:late_no_cheap_asks"], 2)
            labels = snap["skip_labels"]
            self.assertEqual(labels["late_no_cheap_asks"], "Te laat: goedkope orders al weg")
            self.assertEqual(labels["too_old"], "Meting te oud (>10 min)")
            # the 'Laatste signalen' table: the result next to each signal
            res = {s["token_id"]: (s["result"], s["result_label"]) for s in snap["signals"]}
            self.assertEqual(res["a"], ("bought", "gekocht"))
            self.assertEqual(res["b"], ("late_no_cheap_asks", "Te laat: goedkope orders al weg"))
            self.assertEqual(res["d"], ("fak_unfilled", "Order niet gevuld (iemand was sneller)"))

    def test_too_old_label_follows_the_setting(self):
        self.assertEqual(skip_labels(Config(fresh_seconds=300))["too_old"], "Meting te oud (>5 min)")


# ----------------------------------------------------------------------------- item 11: HighTempTation
class FakeApi:
    def __init__(self, trades, fail=False):
        self.trades, self.fail, self.calls = trades, fail, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        if self.fail:
            raise requests.ConnectionError("data-api down")
        data = self.trades

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return data
        return R()


def his(asset, ts, price=0.9, size=100.0, title="Will the highest temperature in Madrid be 21°C on October 10?"):
    return {"type": "TRADE", "side": "BUY", "asset": asset, "timestamp": ts, "price": price, "size": size,
            "usdcSize": price * size * 1.0, "title": title, "outcome": "No"}


class HighTempTationTests(unittest.TestCase):
    SINCE = 1_791_590_400.0

    def rows(self):
        S = self.SINCE
        return [{"type": "signal", "token_id": "A", "ts": S + 990},          # bot 10 s earlier, bought
                {"type": "buy", "token_id": "A", "ts": S + 991, "shares": 5},
                {"type": "signal", "token_id": "B", "ts": S + 2012},         # 12 s later, cheap asks gone
                {"type": "skip", "token_id": "B", "ts": S + 2013, "reason": "late_no_cheap_asks", "budget": 5.0},
                {"type": "signal", "token_id": "C", "ts": S + 3020},         # 20 s later, budget too small
                {"type": "skip", "token_id": "C", "ts": S + 3021, "reason": "budget_too_small", "budget": 3.0},
                {"type": "signal", "token_id": "X", "ts": S + 100}]          # he did not buy this one

    def test_statuses_deltas_and_missed_estimate(self):
        S = self.SINCE
        api = FakeApi([his("A", S + 1000), his("A", S + 1005, price=0.92), his("B", S + 2000, price=0.95, size=10),
                       his("C", S + 3000, price=0.9, size=10), his("D", S + 4000, price=0.8, size=50)])
        r = compare_today(self.rows(), api, since=S, budget=5.0, fee_rate=0.05, sell_price=0.99)
        self.assertEqual(api.calls[0]["side"], "BUY")
        self.assertEqual(api.calls[0]["start"], int(S))
        self.assertEqual(api.calls[0]["limit"], 500)
        by = {x["asset"]: x for x in r["rows"]}
        self.assertEqual(by["A"]["status"], "gekocht")
        self.assertEqual(by["A"]["delta_s"], -10.0)
        self.assertAlmostEqual(by["A"]["his_price"], 0.91)                     # volume-weighted
        self.assertEqual((by["B"]["status"], by["B"]["delta_s"]), ("gezien_te_laat", 12.0))
        self.assertEqual((by["C"]["status"], by["C"]["reason"]), ("gezien_overgeslagen", "budget_too_small"))
        self.assertEqual((by["D"]["status"], by["D"]["delta_s"]), ("niet_gezien", None))
        self.assertEqual(by["A"]["label"], "Madrid max 21°C · NO")
        self.assertEqual([x["asset"] for x in r["rows"]], ["D", "C", "B", "A"])   # newest first
        self.assertEqual((r["his_n"], r["seen_n"], r["bought_n"], r["first_n"], r["late_n"]), (4, 3, 1, 1, 1))
        self.assertEqual(r["median_delta_s"], 12)

        def gain(stake, p):
            return stake * (0.99 - p - exact_fee_per_share(p) - exact_fee_per_share(0.99)) / p
        # B: min(skip budget 5, his $9.50); C: min(skip budget 3, his $9); D: min(default 5, his $40)
        self.assertAlmostEqual(r["missed_est"], round(gain(5.0, 0.95) + gain(3.0, 0.9) + gain(5.0, 0.8), 2))

    def test_exact_fee_and_titles(self):
        self.assertAlmostEqual(exact_fee_per_share(0.98), 0.05 * 0.98 * 0.02)
        self.assertEqual(short_title("Will the lowest temperature in Taipei be 24°C on October 11?", "No"),
                         "Taipei min 24°C · NO")
        self.assertEqual(short_title("Something else", None), "Something else")

    def test_engine_refresh_keeps_last_result_on_error(self):
        with tempfile.TemporaryDirectory() as d:
            eng = Engine(d)
            now = time.time()
            mid = local_midnight(now)
            if local_midnight(now + 300) != mid:
                self.skipTest("too close to midnight")
            eng.journal.write("signal", token_id="A", city="Madrid")
            eng.refresh_htt(FakeApi([his("A", mid + 1)]), now)
            self.assertEqual((eng.htt["his_n"], eng.htt["seen_n"]), (1, 1))
            self.assertEqual(eng.htt["updated_at"], now)
            eng.refresh_htt(FakeApi([], fail=True), now + 200)
            self.assertEqual(eng.htt["his_n"], 1)                               # old numbers stay
            self.assertTrue(eng.htt["error"])
            self.assertEqual(eng.snapshot()["htt"]["his_n"], 1)

    def test_background_refresh_only_while_the_dashboard_is_open(self):
        with tempfile.TemporaryDirectory() as d:
            eng = Engine(d)
            now = time.time()
            self.assertFalse(eng.htt_due(now))                 # nobody has polled /api/state
            eng.snapshot()                                     # the dashboard polls
            self.assertTrue(eng.htt_due(time.time()))
            eng._htt_at = time.time()
            self.assertFalse(eng.htt_due(time.time() + 60))    # every 120 s at most
            self.assertTrue(eng.htt_due(time.time() + 121))
            eng._last_view = time.time() - 601                 # not polled for 10 minutes: pause
            self.assertFalse(eng.htt_due(time.time() + 121))


# ----------------------------------------------------------------------------- item 12: settings
class SafeSettingsTests(unittest.TestCase):
    def test_hard_ranges(self):
        for key, bad, ok in (("TRADE_PCT", "21", "20"), ("POLL_SECONDS", "31", "2"), ("SAFETY_MARGIN", "3", "2"),
                             ("MAX_BUY_PRICE", "0.79", "0.98"), ("OPEN_PCT", "9", "60"), ("DAILY_LOSS_PCT", "31", "30"),
                             ("EVENT_PCT", "26", "25")):
            with self.assertRaises(ValueError, msg=key):
                settings_mod.validate({key: bad})
            settings_mod.validate({key: ok}, base={})

    def test_rules_between_settings(self):
        base = settings_mod.current_values(Config())
        with self.assertRaisesRegex(ValueError, "minstens even groot"):
            settings_mod.validate({"TRADE_PCT": "7"}, base)             # daily stop 6% < 7% per buy
        with self.assertRaisesRegex(ValueError, "minstens even groot"):
            settings_mod.validate({"EVENT_PCT": "4"}, base)
        with self.assertRaisesRegex(ValueError, "minstens even groot"):
            settings_mod.validate({"MARKET_PCT": "4"}, base)
        self.assertEqual(settings_mod.validate({"TRADE_PCT": "6"}, base), {"TRADE_PCT": "6.0"})

    def test_consequences_in_dollars_and_levels(self):
        c = settings_mod.check({}, Config(), 100.0)
        self.assertEqual(c["TRADE_PCT"]["text"], "Eén foute meting kost je max $5,00")
        self.assertEqual(c["TRADE_PCT"]["level"], "good")
        self.assertEqual(c["EVENT_PCT"]["text"], "Eén foute meting op meerdere vakken: max $10,00")
        self.assertEqual(c["DAILY_LOSS_PCT"]["text"], "Na -$6,00 op een dag stopt hij met kopen")
        self.assertIn("de winnaar", c["SAFETY_MARGIN"]["text"])
        c = settings_mod.check({"TRADE_PCT": "12", "DAILY_LOSS_PCT": "12", "EVENT_PCT": "12", "MARKET_PCT": "12",
                                "MAX_BUY_PRICE": "0.98", "SAFETY_MARGIN": "1", "POLL_SECONDS": "60"},
                               Config(), 1000.0)
        self.assertEqual((c["TRADE_PCT"]["level"], c["TRADE_PCT"]["text"]), ("warn", "Eén foute meting kost je max $120,00"))
        self.assertEqual(c["TRADE_PCT"]["safe"], "aanbevolen 2-8%")
        self.assertEqual(c["MAX_BUY_PRICE"]["level"], "warn")
        self.assertEqual(c["MAX_BUY_PRICE"]["text"], "Boven 0,975: ~1% winst per trade, één foute meting kost 100%")
        self.assertEqual(c["SAFETY_MARGIN"]["level"], "warn")
        self.assertEqual(c["SAFETY_MARGIN"]["text"],
                         "Kost naar schatting 60-80% van de winst; de alarmen beschermen je beter")
        self.assertEqual(c["POLL_SECONDS"]["level"], "bad")
        self.assertIn("tussen 2 en 30", c["POLL_SECONDS"]["error"])
        c = settings_mod.check({"TRADE_PCT": "7"}, Config(), 100.0)
        self.assertEqual(c["DAILY_LOSS_PCT"]["level"], "bad")
        # $ caps win over the %: 5% of $10,000 is $500, but at most $250 per buy
        self.assertEqual(settings_mod.check({}, Config(), 10000.0)["TRADE_PCT"]["text"],
                         "Eén foute meting kost je max $250,00")

    def test_minimum_start_amount(self):
        self.assertEqual(settings_mod.min_start_amount(5), 110.0)
        self.assertEqual(settings_mod.min_start_amount(10), 55.0)
        self.assertEqual(settings_mod.min_start_amount(20), 50.0)

    def test_settings_screen_shows_no_env_names(self):
        cur = settings_mod.current(Config(), None, 100.0)
        for f in cur["fields"]:
            self.assertNotRegex(f["label"] + " " + f["help"], r"\b[A-Z]{3,}_[A-Z_]+\b")
        self.assertNotIn("veiliger", json.dumps(cur, ensure_ascii=False))
        self.assertEqual(cur["min_start_amount"], 110.0)
        with self.assertRaises(ValueError) as e:
            settings_mod.validate({"NOPE": "1"})
        self.assertNotIn("NOPE", str(e.exception))


class OverviewDashboardTests(unittest.TestCase):
    """The real HTTP server, stubbed bot loop: new endpoints and snapshot keys."""

    @classmethod
    def setUpClass(cls):
        from weatherbot import engine as engine_mod
        from weatherbot.dashboard import DashboardServer
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ)
        cls.env.start()
        cls.engine = engine_mod.Engine(cls.tmp.name)
        cls.engine.geo_check = lambda session: {"blocked": True, "country": "NL"}     # no network
        cls.server = DashboardServer(0, cls.engine)
        cls.port = cls.server.server_address[1]
        cls.server.allowed_hosts = {f"127.0.0.1:{cls.port}", f"localhost:{cls.port}"}
        cls.server.allowed_origins = {f"http://{h}" for h in cls.server.allowed_hosts}
        import threading
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.env.stop()
        cls.tmp.cleanup()

    def req(self, path, body=None, headers=None):
        import urllib.error
        import urllib.request
        h = {"Content-Type": "application/json", **(headers or {})}
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h,
                                   method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "null")

    def token(self):
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as resp:
            html = resp.read().decode()
        return {"X-Weerbot-Token": html.split('name="weerbot-token" content="')[1].split('"')[0]}

    def test_state_has_the_overview_keys(self):
        status, d = self.req("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("uptime", d["health"])
        self.assertIn("uptime_today_pct", d["health"])
        self.assertIn("funnel", d["today"])
        self.assertIn("skip_labels", d)
        self.assertIn("htt", d)
        for key in ("fees_today", "today_trades", "today_wins", "today_losses", "today_realized_pct", "open_value"):
            self.assertIn(key, d["bank"])

    def test_settings_check_endpoint(self):
        self.assertEqual(self.req("/api/settings/check", {"values": {}})[0], 403)          # needs the token
        status, d = self.req("/api/settings/check", {"values": {"TRADE_PCT": "12"}}, self.token())
        self.assertEqual(status, 200)
        self.assertEqual(d["checks"]["TRADE_PCT"]["level"], "warn")
        status, d = self.req("/api/settings")
        self.assertIn("checks", d)
        self.assertEqual(d["min_start_amount"], 110.0)

    def test_bankroll_minimum(self):
        tok = self.token()
        status, d = self.req("/api/bankroll", {"start_amount": "100"}, tok)
        self.assertEqual(status, 400)
        self.assertEqual(d["error"], "Met $100 en 5% per aankoop is een aankoop kleiner dan 5 shares "
                                     "(Polymarket-minimum). Kies minstens $110.")
        status, d = self.req("/api/bankroll", {"start_amount": "110"}, tok)
        self.assertEqual((status, d["bank"]["balance"]), (200, 110.0))
        self.assertEqual(self.req("/api/bankroll", {"start_amount": "nan"}, tok)[0], 400)


if __name__ == "__main__":
    unittest.main()
