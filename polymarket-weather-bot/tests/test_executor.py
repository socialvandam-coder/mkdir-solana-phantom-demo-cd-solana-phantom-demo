import dataclasses
import tempfile
import unittest
from datetime import date, datetime, timezone

from weatherbot.config import Config
from weatherbot.execution import PaperExecutor
from weatherbot.journal import Journal, read_journal
from weatherbot.markets import Market, TempEvent, parse_bucket
from weatherbot.metar import Observation
from weatherbot.strategy import find_signals


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    """Stands in for requests.Session: serves a scripted order book per token."""

    def __init__(self):
        self.books = {}

    def post(self, url, json=None, timeout=None):
        return FakeResponse([dict(self.books[x["token_id"]], asset_id=x["token_id"]) for x in json
                             if x["token_id"] in self.books])

    def get(self, url, params=None, timeout=None):
        return FakeResponse([])  # market not resolved yet


class PaperCycleTest(unittest.TestCase):
    def test_buy_once_then_sell_at_099(self):
        labels = ["14°C", "15°C", "16°C"]
        mks = [Market(question=l, condition_id=f"c{i}", yes_token=f"y{i}", no_token=f"n{i}", bucket=parse_bucket(l))
               for i, l in enumerate(labels)]
        ev = TempEvent(slug="ev", title="t", kind="high", city="Paris", station="LFPB", unit="C",
                       day=date(2026, 10, 10), tz="Europe/Paris", markets=mks)
        obs = [Observation("LFPB", datetime(2026, 10, 10, 11, 30, tzinfo=timezone.utc), 15, None, "")]
        signals = find_signals(ev, obs)
        self.assertEqual([s.market.bucket.label for s in signals], ["14°C"])

        session = FakeSession()
        session.books["n0"] = {"bids": [{"price": "0.5", "size": "100"}],
                               "asks": [{"price": "0.90", "size": "5"}, {"price": "0.95", "size": "100"}]}
        with tempfile.TemporaryDirectory() as d:
            cfg = dataclasses.replace(Config(), journal_dir=d, sell_delay_seconds=5)
            ex = PaperExecutor(cfg, session, Journal(d))
            ex.on_signals(signals, 1000.0)
            self.assertEqual(len(ex.positions), 1)
            p = ex.positions[0]
            self.assertLessEqual(p.cost, cfg.start_amount * cfg.trade_pct / 100 + 0.1)

            # same stale asks still in the book 60 s later: paper must not buy them again
            ex.on_signals(signals, 1060.0)
            self.assertEqual(len(ex.positions), 1)

            # bots now bid 0.99 -> we sell
            session.books["n0"] = {"bids": [{"price": "0.99", "size": "500"}], "asks": []}
            ex.tick(1004.0)
            self.assertEqual(p.status, "open")      # before the sell delay: nothing happens
            ex.tick(1006.0)
            self.assertEqual(p.status, "closed")
            self.assertGreater(p.proceeds - p.cost, 0)

            types = [r["type"] for r in read_journal(d)]
            self.assertEqual(types, ["signal", "opportunity", "buy", "sell", "close"])


if __name__ == "__main__":
    unittest.main()
