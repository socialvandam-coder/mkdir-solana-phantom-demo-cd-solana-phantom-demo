"""Local web dashboard (http://127.0.0.1:<port>) for the bot.

Only reachable from this computer. Every state-changing request must carry a random
token that is only embedded in the page we serve, and the Host/Origin headers are
checked, so other websites open in the same browser can't control the bot.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import settings as settings_mod
from .compare import compare
from .engine import Engine, live_allowed, live_block_text, make_session
from .journal import read_journal
from .main import run_check

log = logging.getLogger("weatherbot")

STATIC = Path(__file__).with_name("static")
STATIC_FILES = {"/app.js": "application/javascript; charset=utf-8",
                "/style.css": "text/css; charset=utf-8",
                "/icon.svg": "image/svg+xml"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
MAX_BODY = 64 * 1024


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True    # only lets us rebind over TIME_WAIT; two live listeners still conflict

    def __init__(self, port: int, engine: Engine) -> None:
        self.engine = engine
        self.token = secrets.token_urlsafe(32)
        self.port = port
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}
        self._busy = threading.Lock()   # one slow action (check/compare) at a time
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "Weerbot"
    sys_version = ""

    def log_message(self, fmt, *args):  # quiet access log
        log.debug("http %s", fmt % args)

    # -- helpers ---------------------------------------------------------------
    def _headers(self, status: int, ctype: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode()
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _post_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            return False
        given = self.headers.get("X-Weerbot-Token", "")
        return secrets.compare_digest(given, self.server.token)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("verzoek te groot")
        raw = self.rfile.read(n) if n else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("ongeldig verzoek")
        return data

    # -- GET -------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/ping":
            return self._json({"app": "weerbot", "ok": True})
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "verkeerde host")
        engine = self.server.engine
        if path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", self.server.token)
            body = html.encode()
            self._headers(200, "text/html; charset=utf-8", len(body))
            return self.wfile.write(body)
        if path in STATIC_FILES:
            body = (STATIC / path.lstrip("/")).read_bytes()
            self._headers(200, STATIC_FILES[path], len(body))
            return self.wfile.write(body)
        if path == "/api/state":
            return self._json(engine.snapshot())
        if path == "/api/settings":
            return self._json(settings_mod.current(engine.cfg, engine.live_access(refresh=True), engine.bank.equity))
        return self._error(HTTPStatus.NOT_FOUND, "niet gevonden")

    # -- POST ------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok() or not self._post_allowed():
            return self._error(HTTPStatus.FORBIDDEN, "geen toegang")
        engine = self.server.engine
        path = self.path.split("?", 1)[0]
        try:
            body = self._body()
            if path == "/api/start":
                engine.start()
                if engine.status == "running":
                    engine.pause(False)          # Start = buying on again (a Noodstop needs Hervat)
                return self._json({"ok": engine.status == "running", "status": engine.status, "error": engine.error,
                                   "halt": engine.bank.halt_info()})
            if path in ("/api/stop", "/api/pause"):
                # stopping buying never stops selling: the loop keeps managing open positions
                on = True if path == "/api/stop" else body.get("on", True)
                if not isinstance(on, bool):
                    raise ValueError("on moet true of false zijn")
                engine.pause(on)
                return self._json({"ok": True, "status": engine.status, "halt": engine.bank.halt_info()})
            if path == "/api/halt":
                engine.emergency_stop()
                return self._json({"ok": True, "status": engine.status, "halt": engine.bank.halt_info()})
            if path == "/api/resume":
                left = engine.resume()
                if engine.status != "running":
                    engine.start()
                return self._json({"ok": True, "status": engine.status, "error": engine.error, "remaining": left,
                                   "halt": engine.bank.halt_info()})
            if path == "/api/settings":
                values = body.get("values") or {}
                pk = body.get("private_key")
                if not isinstance(values, dict):
                    raise ValueError("ongeldige instellingen")
                if str(values.get("MODE", "")).strip() == "live":
                    geo = engine.live_access(refresh=True)
                    if not live_allowed(geo, engine.cfg):
                        raise ValueError(live_block_text(geo))
                settings_mod.save(engine.home, values, pk if isinstance(pk, str) else None,
                                  base=settings_mod.current_values(engine.cfg))
                engine.cfg = engine.load_config()
                engine.bank.cfg = engine.cfg
                if engine.status == "running":
                    engine.restart()
                return self._json({"ok": True, **settings_mod.current(engine.cfg, engine.live_access(),
                                                                      engine.bank.equity)})
            if path == "/api/settings/check":
                # colours and '$ consequence' while the user types; saves nothing
                values = body.get("values") or {}
                if not isinstance(values, dict):
                    raise ValueError("ongeldige instellingen")
                return self._json({"ok": True, "checks": settings_mod.check(values, engine.cfg, engine.bank.equity)})
            if path == "/api/bankroll":
                try:
                    amount = float(str(body.get("start_amount", "")).replace(",", "."))
                except ValueError:
                    raise ValueError("Startbedrag: geen geldig getal") from None
                if not amount == amount or not 1 <= amount <= 1_000_000:     # also NaN
                    raise ValueError("Startbedrag moet tussen 1 en 1.000.000 liggen")
                pct = engine.cfg.trade_pct
                low = settings_mod.min_start_amount(pct)
                if amount < low:
                    raise ValueError(f"Met {settings_mod.usd_short(amount)} en {settings_mod.num_nl(pct, 1)}% per "
                                     "aankoop is een aankoop kleiner dan 5 shares (Polymarket-minimum). "
                                     f"Kies minstens {settings_mod.usd_short(low)}.")
                engine.reset_bankroll(amount)
                return self._json({"ok": True, "bank": engine.bank.snapshot()})
            if path in ("/api/check", "/api/compare"):
                if not self.server._busy.acquire(blocking=False):
                    return self._error(HTTPStatus.CONFLICT, "even geduld, er loopt al een controle")
                try:
                    if path == "/api/check":
                        return self._json({"ok": True, "result": run_check(engine.cfg, engine.bank)})
                    rows = read_journal(str(engine.home / engine.cfg.journal_dir))
                    return self._json({"ok": True, "result": compare(rows, make_session())})
                finally:
                    self.server._busy.release()
            if path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self._shutdown, daemon=True).start()
                return None
            return self._error(HTTPStatus.NOT_FOUND, "niet gevonden")
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001 - show the problem in the dashboard instead of a dead request
            log.exception("dashboard action failed")
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"er ging iets mis: {exc}")

    def _shutdown(self) -> None:
        log.info("shutting down (requested from the dashboard)")
        self.server.engine.stop()
        self.server.shutdown()
