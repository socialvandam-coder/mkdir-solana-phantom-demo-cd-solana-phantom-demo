import dataclasses
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from weatherbot import settings as settings_mod
from weatherbot.bankroll import Bankroll
from weatherbot.config import Config


class BankrollTests(unittest.TestCase):
    def test_compounding_sizes_grow_with_balance(self):
        cfg = dataclasses.replace(Config(), start_amount=100, trade_pct=10, market_pct=20, open_pct=80)
        b = Bankroll(cfg)
        self.assertAlmostEqual(b.budget("t1"), 10.0)
        b.on_buy("t1", 10.0)
        b.on_close(10.0, 30.0)          # +20 profit
        self.assertAlmostEqual(b.balance, 120.0)
        self.assertAlmostEqual(b.budget("t2"), 12.0)   # 10% of the new balance
        self.assertAlmostEqual(b.total_pct, 20.0)

    def test_limits(self):
        cfg = dataclasses.replace(Config(), start_amount=100, trade_pct=50, market_pct=20, open_pct=30)
        b = Bankroll(cfg)
        self.assertAlmostEqual(b.budget("t"), 20.0)      # per-market cap beats per-trade
        b.on_buy("t", 20.0)
        self.assertAlmostEqual(b.budget("t"), 0.0)       # bucket full for today
        self.assertAlmostEqual(b.budget("u"), 10.0)      # only 30% may be open at once
        b.wallet_usdc = 4.0
        self.assertAlmostEqual(b.budget("u"), 4.0)       # live: never more than the real wallet

    def test_daily_loss_stop(self):
        cfg = dataclasses.replace(Config(), start_amount=100, daily_loss_pct=10)
        b = Bankroll(cfg)
        b.on_buy("t", 10); b.on_close(10, 0)             # -10 = -10% today
        self.assertTrue(b.stopped_for_today())
        self.assertEqual(b.budget("x"), 0.0)

    def test_today_pct_and_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            cfg = dataclasses.replace(Config(), start_amount=200)
            b = Bankroll(cfg, path)
            b.on_buy("t", 20); b.on_close(20, 22)
            self.assertAlmostEqual(b.today_pct, 1.0)
            b2 = Bankroll(dataclasses.replace(cfg, start_amount=999), path)   # saved state wins
            self.assertAlmostEqual(b2.balance, 202.0)
            self.assertEqual(len(b2.state["history"]), 2)
            b2.reset(50)
            self.assertAlmostEqual(Bankroll(cfg, path).balance, 50.0)

    def test_new_day_resets_today(self):
        cfg = dataclasses.replace(Config(), start_amount=100)
        b = Bankroll(cfg)
        b.on_buy("t", 10); b.on_close(10, 15)
        b.state["day"] = "2000-01-01"                    # pretend yesterday
        self.assertAlmostEqual(b.today_pnl, 0.0)
        self.assertAlmostEqual(b.state["day_start_balance"], 105.0)


class SettingsTests(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(settings_mod.validate({"TRADE_PCT": "12,5"}), {"TRADE_PCT": "12.5"})
        self.assertEqual(settings_mod.validate({"ENABLE_YES_BETS": "ja"}), {"ENABLE_YES_BETS": "true"})
        for bad in ({"TRADE_PCT": "99"}, {"MODE": "yolo"}, {"NOPE": "1"}, {"CITIES": "a=b"}, {"SAFETY_MARGIN": "x"}):
            with self.assertRaises(ValueError):
                settings_mod.validate(bad)

    def test_save_keeps_other_lines_and_hides_key(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(settings_mod, "_use_keychain", return_value=False):
            home = Path(d)
            (home / ".env").write_text("# my comment\nFOO=bar\nTRADE_PCT=10\n")
            settings_mod.save(home, {"TRADE_PCT": "15"}, private_key="0x" + "ab" * 32)
            text = (home / ".env").read_text()
            self.assertIn("# my comment", text)
            self.assertIn("FOO=bar", text)
            self.assertIn("TRADE_PCT=15.0", text)
            self.assertEqual(oct((home / ".env").stat().st_mode & 0o777), "0o600")
            with self.assertRaises(ValueError):
                settings_mod.save(home, {}, private_key="not-a-key")
            cur = settings_mod.current(Config(private_key="0xabc"))
            self.assertTrue(cur["private_key_set"])
            self.assertNotIn("0xabc", json.dumps(cur))


class DashboardTests(unittest.TestCase):
    """Starts the real HTTP server on a free port with a stubbed bot loop."""

    @classmethod
    def setUpClass(cls):
        from weatherbot import engine as engine_mod
        from weatherbot.dashboard import DashboardServer
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patch = mock.patch.object(engine_mod.BotRunner, "run", lambda self, stop: stop.wait(30))
        cls.patch.start()
        cls.engine = engine_mod.Engine(cls.tmp.name)
        cls.server = DashboardServer(0, cls.engine)
        cls.port = cls.server.server_address[1]
        cls.server.allowed_hosts = {f"127.0.0.1:{cls.port}", f"localhost:{cls.port}"}
        cls.server.allowed_origins = {f"http://{h}" for h in cls.server.allowed_hosts}
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.engine.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.patch.stop()
        cls.tmp.cleanup()

    def req(self, path, body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h,
                                   method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def token(self):
        status, html = self.req("/")
        self.assertEqual(status, 200)
        return html.split('name="weerbot-token" content="')[1].split('"')[0]

    def test_ping_and_page(self):
        self.assertEqual(json.loads(self.req("/api/ping")[1])["app"], "weerbot")
        self.assertNotIn("{{TOKEN}}", self.req("/")[1])
        self.assertEqual(self.req("/app.js")[0], 200)
        self.assertEqual(self.req("/../weatherbot/settings.py")[0], 404)

    def test_state_shape(self):
        status, body = self.req("/api/state")
        d = json.loads(body)
        for key in ("status", "bank", "history", "daily", "today", "events", "trades", "signals", "health"):
            self.assertIn(key, d)
        self.assertEqual(len(d["daily"]), 14)

    def test_protections(self):
        self.assertEqual(self.req("/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("/api/stop", {})[0], 403)                                   # no token
        tok = self.token()
        self.assertEqual(self.req("/api/stop", {}, {"X-Weerbot-Token": "wrong"})[0], 403)
        self.assertEqual(self.req("/api/stop", {}, {"X-Weerbot-Token": tok, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("/api/stop", {}, {"X-Weerbot-Token": tok})[0], 200)

    def test_settings_and_bankroll(self):
        tok = {"X-Weerbot-Token": self.token()}
        status, body = self.req("/api/settings", {"values": {"TRADE_PCT": "500"}}, tok)
        self.assertEqual(status, 400)
        self.assertIn("tussen", json.loads(body)["error"])
        status, body = self.req("/api/settings", {"values": {"TRADE_PCT": "7"}}, tok)
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.cfg.trade_pct, 7.0)
        status, body = self.req("/api/bankroll", {"start_amount": "321"}, tok)
        self.assertEqual(json.loads(body)["bank"]["balance"], 321.0)
        self.assertEqual(self.req("/api/bankroll", {"start_amount": "0"}, tok)[0], 400)

    def test_start_stop(self):
        tok = {"X-Weerbot-Token": self.token()}
        with mock.patch("weatherbot.engine.make_source"):
            self.assertEqual(json.loads(self.req("/api/start", {}, tok)[1])["status"], "running")
        time.sleep(0.2)
        self.assertEqual(json.loads(self.req("/api/stop", {}, tok)[1])["status"], "stopped")


if __name__ == "__main__":
    unittest.main()


class AppArgsTests(unittest.TestCase):
    def test_finder_psn_argument_is_ignored(self):
        from weatherbot import app as app_mod
        with mock.patch.object(app_mod, "_run", return_value=0) as run:
            self.assertEqual(app_mod.main(["-psn_0_12345", "--no-browser"]), 0)
            self.assertTrue(run.call_args[0][0].no_browser)

    def test_startup_crash_shows_error_and_returns_1(self):
        from weatherbot import app as app_mod
        with mock.patch.object(app_mod, "_run", side_effect=RuntimeError("boom")), \
                mock.patch.object(app_mod, "show_error") as show:
            self.assertEqual(app_mod.main([]), 1)
            self.assertIn("boom", show.call_args[0][0])
