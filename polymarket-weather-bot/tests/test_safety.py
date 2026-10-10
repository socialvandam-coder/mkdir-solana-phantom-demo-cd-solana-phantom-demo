"""Safety and money rules: sizing caps, geoblock gating, freshness, never averaging down,
the 30-second confirmation alarm, buying pause / emergency stop, tripwires and notifications."""
import dataclasses
import json
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from weatherbot.bankroll import Bankroll
from weatherbot.config import Config
from weatherbot.execution import LiveExecutor, PaperExecutor, Position, backoff, event_day_end
from weatherbot.journal import Journal, read_journal
from weatherbot.markets import Market, TempEvent, parse_bucket
from weatherbot.metar import Observation, ObservationStore, plausible
from weatherbot.strategy import find_signals

REPORT = datetime(2026, 10, 10, 11, 30, tzinfo=timezone.utc)
T0 = REPORT.timestamp()


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    """Serves scripted order books; markets never resolve."""

    def __init__(self):
        self.books = {}
        self.posts = 0
        self.fail = False
        self.on_post = None

    def post(self, url, json=None, timeout=None):
        self.posts += 1
        if self.on_post:
            self.on_post()
        if self.fail:
            raise requests.ConnectionError("down")
        return FakeResponse([dict(self.books[x["token_id"]], asset_id=x["token_id"]) for x in json
                             if x["token_id"] in self.books])

    def get(self, url, params=None, timeout=None):
        return FakeResponse([])


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, kind, title, text, now=None):
        self.calls.append((kind, title, text))
        return True

    def kinds(self):
        return [c[0] for c in self.calls]


def book(asks=(), bids=()):
    return {"asks": [{"price": str(p), "size": str(s)} for p, s in asks],
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids]}


LABELS = ["13°C", "14°C", "15°C", "16°C", "17°C"]


def make_event():
    mks = [Market(question=lbl, condition_id=f"c{i}", yes_token=f"y{i}", no_token=f"n{i}", bucket=parse_bucket(lbl))
           for i, lbl in enumerate(LABELS)]
    return TempEvent(slug="paris-high", title="t", kind="high", city="Paris", station="LFPB", unit="C",
                     day=date(2026, 10, 10), tz="Europe/Paris", markets=mks)


def signals(temp=16, at=REPORT, only=None):
    sigs = find_signals(make_event(), [Observation("LFPB", at, temp, None, "")])
    return [s for s in sigs if only is None or s.market.bucket.label in only]


class RebuyExecutor(PaperExecutor):
    """Paper fills, but with live's rebuy rules (paper itself never rebuys)."""
    allow_rebuy = True


class Rig:
    """An executor with a temp journal + state file."""

    def __init__(self, d, cls=PaperExecutor, session=None, **over):
        self.d = d
        self.cfg = dataclasses.replace(Config(), journal_dir=d, **over)
        self.session = session or FakeSession()
        self.journal = Journal(d)
        self.bank = Bankroll(self.cfg, Path(d) / "state.json")
        self.notifier = RecordingNotifier()
        self.ex = cls(self.cfg, self.session, self.journal, self.bank, self.notifier)

    def rows(self, kind=None):
        return [r for r in read_journal(self.d) if kind is None or r["type"] == kind]

    def skips(self):
        return [r["reason"] for r in self.rows("skip")]


# ----------------------------------------------------------------------------- item 5
class SizingTests(unittest.TestCase):
    def test_defaults(self):
        c = Config()
        self.assertEqual((c.trade_pct, c.market_pct, c.event_pct, c.open_pct, c.daily_loss_pct),
                         (5.0, 8.0, 10.0, 30.0, 6.0))
        self.assertEqual((c.max_drawdown_pct, c.max_trade_usdc, c.max_event_usdc, c.max_buy_price),
                         (15.0, 250.0, 500.0, 0.975))

    def test_max_buy_price_hard_cap(self):
        from weatherbot.config import load_config
        with mock.patch.dict("os.environ", {"MAX_BUY_PRICE": "0.99"}):
            self.assertEqual(load_config("/nonexistent").max_buy_price, 0.98)

    def test_percent_caps(self):
        b = Bankroll(Config(start_amount=100))
        self.assertAlmostEqual(b.budget("t", "ev"), 5.0)                    # 5% per buy
        b.on_buy("t", 5.0, "ev")
        self.assertAlmostEqual(b.budget("t", "ev"), 3.0)                    # 8% per bucket per day
        self.assertAlmostEqual(b.budget("u", "ev"), 5.0)                    # 10% per market: 10 - 5
        b.on_buy("u", 5.0, "ev")
        self.assertAlmostEqual(b.budget("v", "ev"), 0.0)                    # market full
        self.assertEqual(b.limiting_cap("v", "ev"), "event")
        self.assertAlmostEqual(b.budget("v", "other"), 5.0)                 # other markets are fine
        b.on_buy("w", 5.0, "e2"); b.on_buy("x", 5.0, "e3"); b.on_buy("y", 5.0, "e4")
        self.assertAlmostEqual(b.budget("z", "e5"), 5.0)                    # 25 open, 30% allowed
        b.on_buy("z", 5.0, "e5")
        self.assertAlmostEqual(b.budget("q", "e6"), 0.0)                    # 30 open = OPEN_PCT
        self.assertEqual(b.limiting_cap("q", "e6"), "open")

    def test_dollar_caps(self):
        b = Bankroll(Config(start_amount=10_000))
        self.assertAlmostEqual(b.budget("t", "ev"), 250.0)                  # MAX_TRADE_USDC beats 5% ($500)
        b.on_buy("t", 250.0, "ev")
        b.on_buy("u", 200.0, "ev")
        self.assertAlmostEqual(b.budget("v", "ev"), 50.0)                   # MAX_EVENT_USDC 500 - 450
        self.assertEqual(b.limiting_cap("v", "ev"), "event_usdc")

    def test_next_trade_size_after_all_caps(self):
        b = Bankroll(Config(start_amount=10_000))
        self.assertEqual(b.snapshot()["next_trade_size"], 250.0)

    def test_confirmed_positions_free_the_market(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d, start_amount=100)
            s1, s2 = signals(only={"13°C"})[0], signals(only={"14°C"})[0]
            rig.session.books = {"n0": book([(0.9, 100)]), "n1": book([(0.9, 100)])}
            rig.ex.on_signals([s1], T0 + 20)
            self.assertAlmostEqual(rig.bank.open_event_cost["paris-high"], rig.ex.positions[0].cost)
            # second bucket in the same market: only 10% - ~5% left
            self.assertLess(rig.bank.budget(s2.token_id, s2.event_slug), 5.1)
            rig.session.books["n0"] = book(bids=[(0.99, 500)])
            rig.ex.tick(T0 + 26)                                            # sold at 0.99 = confirmed
            self.assertNotIn("paris-high", rig.bank.open_event_cost)
            self.assertAlmostEqual(rig.bank.budget(s2.token_id, s2.event_slug), 5.0, places=1)

    def test_daily_loss_on_equity_and_drawdown(self):
        b = Bankroll(Config(start_amount=100))
        b.on_buy("t", 5.0, "ev")
        self.assertFalse(b.stopped_for_today())
        b.open_value = 0.0                     # the position's bid collapsed: -5% equity
        self.assertFalse(b.stopped_for_today())
        b.on_buy("u", 2.0, "ev2"); b.open_value = 0.0
        self.assertTrue(b.stopped_for_today())  # -7% of today's start equity
        self.assertEqual(b.budget("x"), 0.0)
        b2 = Bankroll(Config(start_amount=100))
        b2.on_buy("t", 10, None); b2.on_close(10, 30)          # peak 120
        b2.on_buy("u", 19, None); b2.on_close(19, 0)           # 101
        self.assertAlmostEqual(b2.peak_equity, 120.0)
        self.assertAlmostEqual(b2.drawdown_pct, 15.83, places=2)

    def test_memory_is_saved(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            b = Bankroll(Config(start_amount=100), path)
            b.on_buy("t", 5.0, "ev")
            b.set_halt("pauze", "user", now=123.0)
            b.on_close(5.0, 6.0)
            b2 = Bankroll(Config(start_amount=999), path)
            self.assertAlmostEqual(b2.spent_today["t"], 5.0)
            self.assertAlmostEqual(b2.open_event_cost["ev"], 5.0)
            self.assertAlmostEqual(b2.state["day_start_equity"], 100.0)
            self.assertAlmostEqual(b2.peak_equity, 101.0)
            self.assertEqual(b2.halt_info()["reason"], "pauze")
            self.assertEqual(b2.state["halt"], {"on": True, "reason": "pauze", "by": "user", "since": 123.0,
                                                "auto_resume_at": None})

    def test_old_state_file_gets_equity_fields(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text(json.dumps({"start_amount": 100, "realized": 20, "day": time.strftime("%Y-%m-%d"),
                                        "day_start_balance": 110}))
            b = Bankroll(Config(), path)
            self.assertAlmostEqual(b.state["day_start_equity"], 110.0)
            self.assertAlmostEqual(b.peak_equity, 120.0)
            self.assertEqual(b.spent_today, {})

    def test_reset_keeps_user_pause_but_clears_loss_stops(self):
        b = Bankroll(Config(start_amount=100))
        b.set_halt("pauze", "user"); b.set_halt("drawdown"); b.set_halt("dagverlies", until=1e12)
        b.reset(200)
        self.assertEqual(set(b.halts), {"pauze"})
        self.assertAlmostEqual(b.peak_equity, 200.0)


# ----------------------------------------------------------------------------- item 8
class FreshnessTests(unittest.TestCase):
    def test_restart_with_20_min_old_signal_does_not_buy(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            s = signals(only={"13°C"})
            rig.session.books = {"n0": book([(0.995, 100)])}           # nothing cheap yet
            rig.ex.on_signals(s, T0 + 60)
            self.assertEqual(rig.skips(), ["late_no_cheap_asks"])
            # the app restarts 20 minutes later; cheap asks are lying around now
            rig.session.books = {"n0": book([(0.80, 100)])}
            posts = rig.session.posts
            ex2 = PaperExecutor(rig.cfg, rig.session, rig.journal, Bankroll(rig.cfg, Path(d) / "state.json"),
                                rig.notifier)
            self.assertIn("n0", ex2.watch)                              # memory survived the restart
            ex2.on_signals(s, T0 + 1200)
            self.assertEqual(ex2.positions, [])
            self.assertEqual(rig.session.posts, posts)                  # did not even look at the book
            self.assertEqual(rig.skips()[-1], "too_old")
            self.assertEqual(len(rig.rows("signal")), 1)                # not journaled as a new signal

    def test_old_report_is_never_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            rig.session.books = {"n0": book([(0.80, 100)])}
            rig.ex.on_signals(signals(only={"13°C"}), T0 + 20 * 60)
            self.assertEqual(rig.ex.positions, [])
            self.assertEqual(rig.skips(), ["too_old"])

    def test_freshness_counts_from_the_first_kill(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            rig.session.books = {"n0": book([(0.995, 100)])}
            rig.ex.on_signals(signals(16, REPORT, {"13°C"}), T0 + 30)
            # 30 min later a higher reading: bucket 13 is still dead, but it died 30 min ago
            later = REPORT + timedelta(minutes=30)
            rig.session.books = {"n0": book([(0.80, 100)])}
            rig.ex.on_signals(signals(17, later, {"13°C"}), later.timestamp() + 30)
            self.assertEqual(rig.ex.positions, [])
            self.assertEqual(rig.skips()[-1], "too_old")

    def test_recheck_cadence(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            rig.session.books = {"n0": book([(0.995, 100)])}
            s = signals(only={"13°C"})
            seen = T0 + 10
            looked = []
            for dt in (0, 3, 5, 12, 61, 80, 91, 200):
                before = rig.session.posts
                rig.ex.on_signals(s, seen + dt)
                if rig.session.posts > before:
                    looked.append(dt)
            # every 5 s in the first minute, then every 30 s
            self.assertEqual(looked, [0, 5, 12, 61, 91, 200])

    def test_no_rebuy_when_the_price_collapses(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d, cls=RebuyExecutor, start_amount=1000)
            s = signals(only={"13°C"})
            rig.session.books = {"n0": book([(0.85, 100)])}
            rig.ex.on_signals(s, T0 + 30)
            self.assertEqual(len(rig.ex.positions), 1)
            rig.session.books = {"n0": book([(0.25, 1000)])}             # wrong reading: NO collapses
            rig.ex.on_signals(s, T0 + 40)
            self.assertEqual(len(rig.ex.positions), 1)
            self.assertEqual(rig.skips(), ["rebuy_cheaper"])
            # after the 60 s window, an unconfirmed position blocks any rebuy
            rig.ex.on_signals(s, T0 + 95)
            self.assertEqual(len(rig.ex.positions), 1)
            self.assertIn("rebuy_unconfirmed", rig.skips())

    def test_quick_rebuy_up_the_ladder_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d, cls=RebuyExecutor, start_amount=1000)
            s = signals(only={"13°C"})
            rig.session.books = {"n0": book([(0.85, 100)])}
            rig.ex.on_signals(s, T0 + 30)
            rig.session.books = {"n0": book([(0.86, 100)])}
            rig.ex.on_signals(s, T0 + 38)
            self.assertEqual(len(rig.ex.positions), 2)
            self.assertAlmostEqual(sum(p.cost for p in rig.ex.positions),
                                   rig.bank.spent_today["n0"], places=6)

    def test_rebuy_after_window_only_when_confirmed(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d, cls=RebuyExecutor, start_amount=1000)
            s = signals(only={"13°C"})
            rig.session.books = {"n0": book([(0.85, 100)], bids=[(0.99, 1000)])}
            rig.ex.on_signals(s, T0 + 30)
            rig.ex.tick(T0 + 36)
            self.assertIsNotNone(rig.ex.positions[0].confirmed_at)
            rig.ex.on_signals(s, T0 + 100)
            self.assertEqual(len(rig.ex.positions), 2)

    def test_stale_station_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            rig.session.books = {"n0": book([(0.80, 100)])}
            rig.ex.stale_events = {"paris-high"}
            rig.ex.on_signals(signals(only={"13°C"}), T0 + 30)
            self.assertEqual(rig.ex.positions, [])
            self.assertEqual(rig.skips(), ["stale_station"])

    def test_implausible_reports_are_rejected(self):
        now = REPORT
        store = ObservationStore()
        self.assertFalse(store.add(Observation("LFPB", now + timedelta(minutes=11), 15, None, ""), now))
        self.assertTrue(store.add(Observation("LFPB", now + timedelta(minutes=9), 15, None, ""), now))
        self.assertFalse(store.add(Observation("LFPB", now, 56, None, ""), now))
        self.assertFalse(store.add(Observation("LFPB", now - timedelta(minutes=30), -61, None, ""), now))
        self.assertFalse(plausible(Observation("KLGA", now, 20, 70.0, ""), now))
        self.assertEqual(store.rejected, 3)
        self.assertEqual(len(store.get("LFPB")), 1)

    def test_cadence_and_staleness(self):
        store = ObservationStore()
        self.assertEqual(store.cadence_min("KLGA"), 60.0)
        self.assertEqual(store.cadence_min("LFPB"), 30.0)
        for i in range(6):
            store.add(Observation("LFPB", REPORT - timedelta(minutes=30 * i), 15, None, ""), REPORT)
        store.add(Observation("LFPB", REPORT - timedelta(minutes=45), 15, None, ""), REPORT)   # a SPECI
        self.assertEqual(store.cadence_min("LFPB"), 30.0)
        self.assertFalse(store.is_stale("LFPB", REPORT + timedelta(minutes=59)))
        self.assertTrue(store.is_stale("LFPB", REPORT + timedelta(minutes=61)))
        self.assertTrue(store.is_stale("EGLL", REPORT))


# ----------------------------------------------------------------------------- item 6
class ConfirmationTests(unittest.TestCase):
    def buy_unconfirmed(self, d, hook=None):
        rig = Rig(d)
        rig.ex.recheck_hook = hook
        rig.session.books = {"n0": book([(0.90, 100)], bids=[(0.5, 3), (0.45, 100)]),
                             "y0": book(bids=[(0.6, 50)])}
        rig.ex.on_signals(signals(only={"13°C"}), T0 + 30)
        rig.ex.tick(T0 + 36)                                           # no 0.99 bids: not sold
        self.assertEqual(rig.ex.positions[0].status, "open")
        rig.ex.tick(T0 + 61)                                           # 31 s after the buy
        return rig

    def test_alarm_locks_the_market_and_rechecks(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            rig = self.buy_unconfirmed(d, hook=lambda p: calls.append(p.token_id) or True)
            p = rig.ex.positions[0]
            self.assertIsNotNone(p.unconfirmed_at)
            row = rig.rows("unconfirmed")[0]
            self.assertEqual((row["best_bid"], row["yes_bid"], row["avg"]), (0.5, 0.6, 0.9))
            self.assertEqual(rig.notifier.kinds(), ["unconfirmed"])
            self.assertEqual(calls, ["n0"])
            # locked until the end of Paris' day (22:00 UTC in summer time)
            self.assertEqual(rig.ex.locked_events["paris-high"],
                             datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc).timestamp())
            self.assertEqual(p.recheck, "dead")
            self.assertIsNone(p.exit_reason)                           # data still says dead: hold, no stop-loss
            rig.ex.tick(T0 + 300)
            self.assertEqual(p.status, "open")
            self.assertEqual(rig.rows("sell"), [])
            # another bucket in the same market is not bought
            rig.session.books["n1"] = book([(0.80, 100)])
            rig.ex.on_signals(signals(only={"14°C"}), T0 + 120)
            self.assertEqual(len(rig.ex.positions), 1)
            self.assertIn("event_locked", rig.skips())
            # the lock survives a restart
            ex2 = PaperExecutor(rig.cfg, rig.session, rig.journal, Bankroll(rig.cfg, Path(d) / "state.json"))
            self.assertIn("paris-high", ex2.locked_events)
            self.assertIsNotNone(ex2.positions[0].unconfirmed_at)

    def test_recheck_says_alive_sells_at_best_bid_with_floor(self):
        with tempfile.TemporaryDirectory() as d:
            rig = self.buy_unconfirmed(d, hook=lambda p: False)
            p = rig.ex.positions[0]
            self.assertEqual(p.exit_reason, "data_recheck")
            rig.ex.tick(T0 + 62)
            closes = rig.rows("close")
            self.assertEqual(len(closes), 1)
            self.assertEqual(closes[0]["how"], "exit")
            self.assertIsNone(closes[0]["confirmed_after_s"])
            self.assertEqual([r["price"] for r in rig.rows("sell")], [0.5, 0.45])
            self.assertIn("loss", rig.notifier.kinds())

    def test_exit_never_below_the_floor(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            rig.ex.recheck_hook = lambda p: False
            rig.session.books = {"n0": book([(0.90, 100)], bids=[(0.40, 100)])}
            rig.ex.on_signals(signals(only={"13°C"}), T0 + 30)
            rig.ex.tick(T0 + 61)
            rig.ex.tick(T0 + 70)
            self.assertEqual(rig.ex.positions[0].status, "open")      # 0.40 < floor 0.45: hold
            self.assertEqual(rig.rows("sell"), [])

    def test_late_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            rig = self.buy_unconfirmed(d, hook=lambda p: True)
            rig.session.books["n0"] = book(bids=[(0.99, 1000)])
            rig.ex.tick(T0 + 70)
            self.assertEqual(rig.rows("close")[0]["confirmed_after_s"], 40.0)
            self.assertEqual(len(rig.rows("confirmed")), 1)

    def test_never_gives_up(self):
        class Broken(PaperExecutor):
            def manage(self, p, now, book=None):
                raise RuntimeError("api down")

        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d, cls=Broken)
            rig.session.books = {"n0": book([(0.90, 100)])}
            rig.ex.on_signals(signals(only={"13°C"}), T0 + 30)
            p = rig.ex.positions[0]
            p.unconfirmed_at = T0 + 61          # (the alarm itself is tested elsewhere)
            now, gaps = T0 + 40, []
            for _ in range(12):
                rig.ex.tick(now)
                gaps.append(round(p.next_poll - now))
                now = p.next_poll
            self.assertEqual(p.status, "open")
            self.assertEqual(gaps[:4], [5, 10, 20, 40])
            self.assertEqual(gaps[-1], 300)
            self.assertEqual(backoff(50), 300)
            self.assertEqual(len(rig.ex.error_times), 12)

    def test_poll_fast_in_the_first_minute(self):
        with tempfile.TemporaryDirectory() as d:
            rig = Rig(d)
            rig.session.books = {"n0": book([(0.90, 100)], bids=[(0.95, 100)])}
            rig.ex.on_signals(signals(only={"13°C"}), T0 + 30)
            p = rig.ex.positions[0]
            rig.ex.tick(T0 + 36)
            self.assertEqual(p.next_poll, T0 + 39)
            p.unconfirmed_at = T0 + 61
            rig.ex.tick(T0 + 100)
            self.assertEqual(p.next_poll, T0 + 130)
            self.assertTrue(rig.ex.wants_fast_loop(T0 + 50))
            self.assertFalse(rig.ex.wants_fast_loop(T0 + 200))

    def test_event_day_end_uses_city_timezone(self):
        s = signals(only={"13°C"})[0]
        self.assertEqual(event_day_end(s), datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc).timestamp())


# ----------------------------------------------------------------------------- live executor
def _ns(**kw):
    return SimpleNamespace(**kw)


class FakeTypes:
    class OrderType:
        FAK, GTC = "FAK", "GTC"

    class AssetType:
        COLLATERAL, CONDITIONAL = "C", "T"

    MarketOrderArgs = OrderArgs = PartialCreateOrderOptions = BalanceAllowanceParams = staticmethod(_ns)


class FakeClob:
    def __init__(self):
        self.orders, self.posted, self.cancelled, self.shares, self.n = {}, [], [], 0.0, 0

    def get_balance_allowance(self, params):
        return {"balance": int((self.shares if params.asset_type == "T" else 1000) * 1e6)}

    def create_order(self, args, opts):
        return ("limit", args)

    def create_market_order(self, args, opts):
        return ("market", args)

    def post_order(self, order, typ):
        kind, args = order
        self.posted.append((kind, args.side, args.price, typ))
        if kind == "limit":
            self.n += 1
            oid = f"o{self.n}"
            self.orders[oid] = {"status": "LIVE", "size_matched": "0"}
            return {"success": True, "orderID": oid}
        if args.side == "BUY":
            shares = round(args.amount / args.price, 2)
            self.shares += shares
            return {"success": True, "makingAmount": str(args.amount), "takingAmount": str(shares)}
        return {"success": True, "makingAmount": str(args.amount), "takingAmount": str(args.amount * args.price)}

    def get_order(self, oid):
        return self.orders[oid]

    def cancel(self, oid):
        self.cancelled.append(oid)
        self.orders[oid]["status"] = "CANCELED"


def live_rig(d):
    rig = Rig(d, cls=PaperExecutor)
    ex = LiveExecutor.__new__(LiveExecutor)
    from weatherbot.execution import Executor
    Executor.__init__(ex, rig.cfg, rig.session, rig.journal, rig.bank, rig.notifier)
    ex.t, ex.BUY, ex.SELL, ex.client, ex._last_wallet = FakeTypes, "BUY", "SELL", FakeClob(), 0.0
    rig.ex = ex
    return rig


class LiveExecutorTests(unittest.TestCase):
    def test_reposts_a_cancelled_exit_and_noodstop_never_cancels(self):
        with tempfile.TemporaryDirectory() as d:
            rig = live_rig(d)
            clob = rig.ex.client
            rig.session.books = {"n0": book([(0.90, 100)])}
            rig.ex.on_signals(signals(only={"13°C"}), T0 + 30)
            p = rig.ex.positions[0]
            rig.ex.tick(T0 + 36)
            self.assertEqual((p.status, p.sell_order_id), ("selling", "o1"))
            rig.bank.set_halt("noodstop", "user")                       # emergency stop: exits stay up
            clob.orders["o1"]["status"] = "CANCELED"                     # e.g. cancelled on the website
            rig.ex.tick(T0 + 40)
            self.assertEqual(p.sell_order_id, "o2")
            self.assertEqual([x[3] for x in clob.posted], ["FAK", "GTC", "GTC"])
            self.assertEqual(clob.cancelled, [])
            clob.orders["o2"]["size_matched"] = str(p.shares)
            rig.ex.tick(T0 + 44)
            self.assertEqual(p.status, "closed")
            self.assertEqual(rig.rows("close")[0]["confirmed_after_s"], 14.0)

    def test_waits_for_shares_without_giving_up(self):
        with tempfile.TemporaryDirectory() as d:
            rig = live_rig(d)
            rig.session.books = {"n0": book([(0.90, 100)])}
            rig.ex.on_signals(signals(only={"13°C"}), T0 + 30)
            rig.ex.client.shares = 0.0                                  # shares not arrived (yet)
            p = rig.ex.positions[0]
            now = T0 + 36
            for _ in range(40):
                rig.ex.tick(now)
                now = max(p.next_poll, now + 1)
            self.assertEqual(p.status, "open")
            self.assertIsNone(p.sell_order_id)
            self.assertLessEqual(p.next_poll - now, 300)

    def test_halt_is_checked_right_before_the_order(self):
        with tempfile.TemporaryDirectory() as d:
            rig = live_rig(d)
            rig.session.books = {"n0": book([(0.90, 100)])}
            rig.session.on_post = lambda: rig.bank.set_halt("noodstop", "user")
            rig.ex.on_signals(signals(only={"13°C"}), T0 + 30)
            self.assertEqual(rig.ex.client.posted, [])
            self.assertEqual(rig.ex.positions, [])
            self.assertEqual(rig.skips(), ["halted"])

    def test_alive_after_recheck_cancels_and_sells_at_bid(self):
        with tempfile.TemporaryDirectory() as d:
            rig = live_rig(d)
            rig.ex.recheck_hook = lambda p: False
            rig.session.books = {"n0": book([(0.90, 100)], bids=[(0.6, 100)])}
            rig.ex.on_signals(signals(only={"13°C"}), T0 + 30)
            p = rig.ex.positions[0]
            rig.ex.tick(T0 + 36)
            rig.ex.tick(T0 + 62)
            self.assertEqual(rig.ex.client.cancelled, ["o1"])
            self.assertEqual(p.status, "closed")
            self.assertEqual(rig.rows("close")[0]["how"], "exit")
            self.assertEqual(rig.ex.client.posted[-1][:3], ("market", "SELL", 0.6))


# ----------------------------------------------------------------------------- items 2 + 7 (runner)
class FakeSource:
    last_used = "aviationweather"

    def fetch(self, stations):
        return []


def runner_rig(d, mode="paper", geo=None, **over):
    from weatherbot.engine import BotRunner
    rig = Rig(d, **over)
    rig.cfg = dataclasses.replace(rig.cfg, mode=mode)
    rig.ex.cfg = rig.cfg
    answers = geo or [{"blocked": False, "country": "US"}]
    rig.geo_calls = 0

    def geo_check(session):
        rig.geo_calls += 1
        return dict(answers[min(rig.geo_calls - 1, len(answers) - 1)])

    with mock.patch("weatherbot.engine.make_source", return_value=FakeSource()):
        rig.runner = BotRunner(rig.cfg, rig.journal, rig.bank, session=rig.session, executor=rig.ex,
                               notifier=rig.notifier, geo_check=geo_check)
    return rig


def open_position(rig, now):
    s = signals(only={"13°C"})[0]
    p = Position(signal=s, shares=10, cost=9.0, opened_at=now, sell_after=now, avg_price=0.9)
    rig.ex.positions.append(p)
    rig.bank.open_cost += p.cost
    return p


class GeoblockTests(unittest.TestCase):
    def test_live_allowed_rules(self):
        from weatherbot.engine import live_allowed, live_block_text
        cfg = Config()
        self.assertTrue(live_allowed({"blocked": False, "country": "US"}, cfg))
        self.assertFalse(live_allowed({"blocked": False, "country": "NL"}, cfg))   # our own hard block
        self.assertFalse(live_allowed({"blocked": True, "country": "US"}, cfg))
        self.assertFalse(live_allowed(None, cfg))
        self.assertEqual(live_block_text({"blocked": True, "country": "NL"}),
                         "Polymarket staat handelen vanuit Nederland niet toe. Alleen oefenmodus.")

    def test_checked_in_paper_mode_too(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("weatherbot.engine.fetch_events", return_value=[]):
            rig = runner_rig(d, "paper", geo=[{"blocked": True, "country": "NL", "ip": "1.2.3.4"}])
            rig.runner.step()
            geo = rig.runner.health["geoblock"]
            self.assertEqual((geo["country"], geo["live_allowed"]), ("NL", False))
            self.assertNotIn("ip", geo)
            self.assertEqual(rig.bank.halts, {})                       # paper keeps simulating

    def test_block_mid_run_keeps_the_loop_and_the_positions_alive(self):
        answers = [{"blocked": False, "country": "US"}, {"blocked": True, "country": "US"},
                   {"blocked": False, "country": "US"}]
        with tempfile.TemporaryDirectory() as d, mock.patch("weatherbot.engine.fetch_events", return_value=[]):
            rig = runner_rig(d, "live", geo=answers, geoblock_every_seconds=0)
            p = open_position(rig, time.time())
            rig.session.books = {"n0": book(bids=[(0.5, 100)])}
            rig.runner.step()
            self.assertEqual(rig.bank.halts, {})
            rig.session.books = {"n0": book(bids=[(0.99, 100)])}
            p.next_poll = 0
            rig.runner.step()                                          # now blocked: must not raise
            self.assertIn("geoblock", rig.bank.halts)
            self.assertEqual(rig.ex.can_buy(), (False, "geoblock"))
            self.assertEqual(p.status, "closed")                       # ...and the position was still sold
            self.assertIn("geoblock", rig.notifier.kinds())
            rig.runner.step()
            self.assertNotIn("geoblock", rig.bank.halts)

    def test_run_loop_survives_a_flip(self):
        answers = [{"blocked": False, "country": "US"}, {"blocked": True, "country": "US"}]
        with tempfile.TemporaryDirectory() as d, mock.patch("weatherbot.engine.fetch_events", return_value=[]):
            rig = runner_rig(d, "live", geo=answers, geoblock_every_seconds=0, poll_seconds=0.1)
            open_position(rig, time.time())
            stop = threading.Event()
            t = threading.Thread(target=rig.runner.run, args=(stop,), daemon=True)
            t.start()
            time.sleep(1.3)
            self.assertTrue(t.is_alive())
            self.assertGreaterEqual(rig.geo_calls, 2)
            stop.set()
            t.join(5)
            self.assertIn("geoblock", rig.bank.halts)
            self.assertEqual([r for r in rig.rows("error")], [])

    def test_engine_refuses_live_start_when_not_allowed(self):
        from weatherbot.engine import Engine
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=False):
            (Path(d) / ".env").write_text("MODE=live\nPRIVATE_KEY=0x" + "ab" * 32 + "\nFUNDER=0xabc\n")
            with mock.patch("weatherbot.settings.keychain_get", return_value=None):
                eng = Engine(d)
                for answer in ({"blocked": True, "country": "US"}, {"blocked": False, "country": "NL"}):
                    eng.geo_check = lambda s, a=answer: dict(a)
                    eng.start()
                    self.assertEqual(eng.status, "error")
                    self.assertIn("niet toe", eng.error)
                    self.assertIsNone(eng._thread)
            import os
            for k in ("MODE", "PRIVATE_KEY", "FUNDER"):
                os.environ.pop(k, None)

    def test_wake_up_rechecks_access_and_notifies(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("weatherbot.engine.fetch_events", return_value=[]):
            rig = runner_rig(d, "paper")
            rig.runner.step()
            self.assertEqual(rig.geo_calls, 1)
            rig.runner._last_wall -= 600                               # the Mac slept 10 minutes
            rig.runner.step()
            self.assertEqual(rig.geo_calls, 2)
            gap = rig.rows("gap")[0]
            self.assertGreater(gap["seconds"], 590)
            self.assertIn("gap", rig.notifier.kinds())


class TripwireTests(unittest.TestCase):
    def test_daily_loss_resumes_next_day(self):
        from weatherbot.bankroll import next_local_midnight
        with tempfile.TemporaryDirectory() as d:
            rig = runner_rig(d)
            rig.bank.on_buy("t", 7.0, "ev"); rig.bank.on_close(7.0, 0.0)
            now = time.time()
            rig.runner.tripwires(now, [])
            h = rig.bank.halts["dagverlies"]
            self.assertEqual(h["auto_resume_at"], next_local_midnight(now))
            self.assertEqual(rig.ex.can_buy(), (False, "halted"))
            self.assertEqual(rig.notifier.kinds(), ["halt"])
            rig.bank.state["day"] = "2000-01-01"                        # a new day starts
            rig.runner.tripwires(h["auto_resume_at"] + 1, [])
            self.assertNotIn("dagverlies", rig.bank.halts)

    def test_drawdown_needs_hervat(self):
        from weatherbot.engine import Engine
        with tempfile.TemporaryDirectory() as d, mock.patch("weatherbot.settings.keychain_get", return_value=None):
            eng = Engine(d)
            b = eng.bank
            b.on_buy("t", 10, None); b.on_close(10, 30)                 # peak 120
            b.on_buy("u", 19, None); b.on_close(19, 0)                  # 101: -15.8%
            with mock.patch("weatherbot.engine.make_source", return_value=FakeSource()):
                from weatherbot.engine import BotRunner
                runner = BotRunner(eng.cfg, eng.journal, b, session=FakeSession(), notifier=RecordingNotifier(),
                                   geo_check=lambda s: {"blocked": False, "country": "US"})
            runner.tripwires(time.time(), [])
            self.assertIn("drawdown", b.halts)
            runner.tripwires(time.time() + 7 * 86400, [])
            self.assertIn("drawdown", b.halts)                          # never lifts by itself
            eng.runner = runner
            self.assertEqual(eng.resume(), [])
            self.assertAlmostEqual(b.peak_equity, 101.0)
            runner.tripwires(time.time(), [])
            self.assertNotIn("drawdown", b.halts)

    def test_two_unconfirmed_pause_30_minutes(self):
        with tempfile.TemporaryDirectory() as d:
            rig = runner_rig(d)
            now = time.time()
            p1, p2 = open_position(rig, now - 100), open_position(rig, now - 90)
            p1.unconfirmed_at = now - 60
            rig.runner.tripwires(now, [])
            self.assertNotIn("onbevestigd", rig.bank.halts)             # one small one: event lock only
            p2.unconfirmed_at = now
            rig.runner.tripwires(now, [])
            self.assertEqual(rig.bank.halts["onbevestigd"]["auto_resume_at"], now + 1800)
            rig.runner.tripwires(now + 1801, [])
            self.assertNotIn("onbevestigd", rig.bank.halts)             # same alarm does not trip again
            # ...not even after a restart
            from weatherbot.engine import BotRunner
            with mock.patch("weatherbot.engine.make_source", return_value=FakeSource()):
                r2 = BotRunner(rig.cfg, rig.journal, rig.bank, session=rig.session, executor=rig.ex,
                               notifier=rig.notifier, geo_check=lambda s: {"blocked": False, "country": "US"})
            r2.tripwires(now + 1802, [])
            self.assertNotIn("onbevestigd", rig.bank.halts)

    def test_big_unconfirmed_cost_pauses(self):
        with tempfile.TemporaryDirectory() as d:
            rig = runner_rig(d)
            now = time.time()
            p = open_position(rig, now - 100)
            p.cost = 11.0
            p.unconfirmed_at = now
            rig.runner.tripwires(now, [])
            self.assertIn("onbevestigd", rig.bank.halts)

    def test_order_errors(self):
        with tempfile.TemporaryDirectory() as d:
            rig = runner_rig(d)
            now = time.time()
            for i in range(4):
                rig.ex.note_error(now - 500 + i, "buy", "x")
            rig.runner.tripwires(now, [])
            self.assertNotIn("fouten", rig.bank.halts)
            rig.ex.note_error(now, "sell", "x")
            rig.runner.tripwires(now, [])
            self.assertEqual(rig.bank.halts["fouten"]["auto_resume_at"], now + 1800)

    def test_clock_skew_live_only(self):
        with tempfile.TemporaryDirectory() as d:
            rig = runner_rig(d, "paper")
            rig.runner.health["clock_skew_s"] = 15
            rig.runner.tripwires(time.time(), [])
            self.assertNotIn("klok", rig.bank.halts)
        with tempfile.TemporaryDirectory() as d:
            rig = runner_rig(d, "live")
            rig.runner.health["clock_skew_s"] = -15
            rig.runner.tripwires(time.time(), [])
            self.assertIn("klok", rig.bank.halts)
            rig.runner.health["clock_skew_s"] = 1
            rig.runner.tripwires(time.time(), [])
            self.assertNotIn("klok", rig.bank.halts)

    def test_weather_failures_and_silent_stations(self):
        with tempfile.TemporaryDirectory() as d:
            rig = runner_rig(d)
            r = rig.runner
            r.health["metar_fail_streak"] = 6
            r.tripwires(time.time(), [])
            self.assertIn("weerdata", rig.bank.halts)
            r.health["metar_fail_streak"] = 0
            r.tripwires(time.time(), [])
            self.assertNotIn("weerdata", rig.bank.halts)
            # 2 of 3 stations silent for hours
            now = datetime.now(timezone.utc)
            evs = []
            for st, age in (("LFPB", 10), ("EGLL", 300), ("EDDM", 300)):
                r.store.add(Observation(st, now - timedelta(minutes=age), 15, None, ""), now)
                evs.append(dataclasses.replace(make_event(), station=st, slug=st))
            r.health["metar_last_ok"] = time.time()
            r.tripwires(time.time(), evs, now)
            self.assertIn("weerdata", rig.bank.halts)
            self.assertEqual(r.health["stale_stations"], ["EDDM", "EGLL"])
            r.store.add(Observation("EGLL", now, 15, None, ""), now)
            r.tripwires(time.time(), evs, now)
            self.assertNotIn("weerdata", rig.bank.halts)

    def test_recheck_position_uses_the_other_source(self):
        from weatherbot.metar import FailoverSource

        class Src:
            def __init__(self, obs):
                self.obs = obs

            def fetch(self, stations):
                return list(self.obs)

        with tempfile.TemporaryDirectory() as d:
            rig = runner_rig(d)
            r = rig.runner
            ev = make_event()
            r.events = [ev]
            r.store.add(Observation("LFPB", REPORT, 16, None, "LFPB 101130Z 16/10"), REPORT)
            p = open_position(rig, T0 + 30)                            # NO on 13°C
            # the other source agrees: still dead
            r.source = FailoverSource(Src([]), Src([Observation("LFPB", REPORT, 16, None, "x")]), ("a", "b"))
            self.assertTrue(r.recheck_position(p))
            # a correction (COR) brings the max down to 13: the bucket is alive
            cor = Observation("LFPB", REPORT, 13, None, "METAR COR LFPB 101130Z 13/10")
            r.source = FailoverSource(Src([]), Src([cor]), ("a", "b"))
            self.assertFalse(r.recheck_position(p))
            self.assertEqual(r.store.get("LFPB")[0].temp_c, 13)
            r.events = []
            self.assertIsNone(r.recheck_position(p))                    # unknown market: hold


# ----------------------------------------------------------------------------- notifications
class NotifyTests(unittest.TestCase):
    def test_rate_limit_and_journal(self):
        from weatherbot.notify import Notifier
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            n = Notifier(j)
            with mock.patch("weatherbot.notify.sys.platform", "linux"), \
                    mock.patch("weatherbot.notify.subprocess.run") as run:
                self.assertTrue(n.notify("halt", "a", "b", now=1000))
                self.assertFalse(n.notify("halt", "a", "b", now=1000 + 899))
                self.assertTrue(n.notify("loss", "a", "b", now=1001))
                self.assertTrue(n.notify("halt", "a", "b", now=1000 + 900))
                run.assert_not_called()
            rows = [r for r in read_journal(d) if r["type"] == "notify"]
            self.assertEqual([r["kind"] for r in rows], ["halt", "loss", "halt"])
            self.assertFalse(rows[0]["shown"])

    def test_mac_uses_osascript_with_arguments(self):
        from weatherbot import notify as notify_mod

        class Now(threading.Thread):
            def start(self):
                self.run()

        with mock.patch.object(notify_mod.sys, "platform", "darwin"), \
                mock.patch.object(notify_mod.threading, "Thread", Now), \
                mock.patch.object(notify_mod.subprocess, "run") as run:
            notify_mod.Notifier().notify("halt", 'Weer"bot', "tekst; do shell script")
        args = run.call_args[0][0]
        self.assertEqual(args[0], "osascript")
        self.assertIn("display notification", args[2])
        self.assertIn('with title "Weerbot"', args[2])
        self.assertEqual(args[3:], ['Weer"bot', "tekst; do shell script"])   # data never inside the script


class EngineViewTests(unittest.TestCase):
    def test_open_positions_shown_when_the_loop_is_not_running(self):
        from weatherbot.engine import Engine
        with tempfile.TemporaryDirectory() as d, mock.patch("weatherbot.settings.keychain_get", return_value=None):
            eng = Engine(d)
            s = signals(only={"13°C"})[0]
            p = Position(signal=s, shares=10, cost=9.0, opened_at=time.time() - 45, sell_after=0, avg_price=0.9)
            eng.bank.set_positions([p.to_dict()])
            snap = eng.snapshot()
            self.assertFalse(snap["loop_alive"])
            self.assertEqual(len(snap["positions"]), 1)
            row = snap["positions"][0]
            self.assertEqual((row["city"], row["bucket"], row["avg"], row["state"]), ("Paris", "13°C", 0.9, "unconfirmed"))
            eng.pause(True)
            self.assertEqual(eng.snapshot()["halt"]["label"], "door jou")
            eng.emergency_stop()
            self.assertEqual(eng.snapshot()["halt"]["reason"], "noodstop")
            self.assertEqual(eng.resume(), [])


if __name__ == "__main__":
    unittest.main()
