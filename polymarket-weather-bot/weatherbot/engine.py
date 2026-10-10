"""The bot loop, plus a thread-safe controller the dashboard uses (start/stop/snapshot)."""
from __future__ import annotations

import collections
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .bankroll import Bankroll
from .config import Config, load_config
from .execution import Executor, LiveExecutor, PaperExecutor
from .geoblock import check_geoblock
from .journal import Journal, read_journal
from .markets import TempEvent, fetch_events
from .metar import ObservationStore, make_source
from .strategy import find_signals, guaranteed_extreme

log = logging.getLogger("weatherbot")

TRADE_TYPES = {"buy", "sell", "sell_order", "close", "buy_error", "error", "start", "stop"}


class TradingNotAllowed(RuntimeError):
    pass


def short_error(exc: Exception) -> str:
    """One readable line instead of a urllib3 traceback string."""
    url = getattr(getattr(exc, "request", None), "url", "") or ""
    host = url.split("/")[2] if url.count("/") >= 2 else ""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return f"geen verbinding met {host or 'de server'}"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"{host or 'server'} gaf fout {exc.response.status_code}"
    return str(exc)[:200]


# Minutes past the hour when airport reports come out (routine METARs at :50-:00, many
# stations also :20/:30). HighTempTation's buys cluster right after these.
FAST_MINUTES = set(range(49, 60)) | set(range(0, 6)) | set(range(19, 26)) | set(range(29, 36))


def poll_interval(cfg: Config, now: float | None = None) -> float:
    minute = time.gmtime(now if now is not None else time.time()).tm_min
    return min(cfg.poll_seconds, cfg.fast_poll_seconds) if minute in FAST_MINUTES else cfg.poll_seconds


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "weatherbot/1.0"
    return s


class BotRunner:
    """One running bot: markets + live observations + executor. `step()` is one loop."""

    def __init__(self, cfg: Config, journal: Journal, bankroll: Bankroll,
                 session: requests.Session | None = None, executor: Executor | None = None) -> None:
        self.cfg = cfg
        self.journal = journal
        self.bank = bankroll
        self.session = session or make_session()
        self.health = {"metar_ok": None, "metar_error": None, "metar_at": None, "markets_ok": None,
                       "markets_error": None, "markets_at": None, "last_step": None, "step_ms": None,
                       "geoblock": None}
        if executor is not None:
            self.executor = executor
        elif cfg.live:
            self.require_trading_allowed()
            self.executor = LiveExecutor(cfg, self.session, journal, bankroll)
        else:
            self.executor = PaperExecutor(cfg, self.session, journal, bankroll)
        self.source = make_source(cfg.metar_source, self.session)
        self.store = ObservationStore()
        self.events: list[TempEvent] = []
        self.last_refresh = 0.0
        self.last_geo = time.time() if cfg.live else 0.0
        self.view: dict = {"events": [], "positions": []}

    def require_trading_allowed(self) -> None:
        geo = check_geoblock(self.session)
        self.health["geoblock"] = geo
        if geo.get("blocked"):
            raise TradingNotAllowed(
                "Polymarket staat handelen vanaf deze verbinding niet toe "
                f"({geo.get('country') or geo.get('error') or 'onbekend'}). Live handelen is uitgeschakeld.")

    def step(self) -> None:
        t0 = time.time()
        now_dt = datetime.now(timezone.utc)
        if t0 - self.last_refresh >= self.cfg.market_refresh_seconds or not self.events:
            try:
                self.events = fetch_events(self.session, now_dt, self.cfg.cities)
                self.last_refresh = t0
                self.health.update(markets_ok=True, markets_error=None, markets_at=t0)
                log.info("watching %d markets on %d stations", len(self.events), len({e.station for e in self.events}))
            except requests.RequestException as exc:
                self.health.update(markets_ok=False, markets_error=short_error(exc), markets_at=t0)
                log.warning("market list failed: %s", short_error(exc))
        if self.cfg.live and t0 - self.last_geo >= 3600:
            self.require_trading_allowed()
            self.last_geo = t0

        live_events = [e for e in self.events if e.is_live(now_dt)]
        stations = sorted({e.station for e in live_events})
        if stations:
            try:
                for obs in self.source.fetch(stations):
                    self.store.add(obs)
                self.health.update(metar_ok=True, metar_error=None, metar_at=t0)
            except requests.RequestException as exc:
                self.health.update(metar_ok=False, metar_error=short_error(exc), metar_at=t0)
                log.warning("weather data failed: %s", short_error(exc))
        self.store.prune(now_dt)

        self.executor.refresh(time.time())
        signals = []
        for e in live_events:
            signals.extend(find_signals(e, self.store.get(e.station), self.cfg.safety_margin, self.cfg.enable_yes_bets))
        self.executor.on_signals(signals, time.time())
        self.executor.tick(time.time())

        self.health.update(last_step=time.time(), step_ms=round((time.time() - t0) * 1000))
        self.view = self._build_view(live_events, now_dt)

    def _build_view(self, live_events: list[TempEvent], now_dt: datetime) -> dict:
        events = []
        for e in sorted(live_events, key=lambda e: (e.city, e.kind)):
            obs = self.store.get(e.station)
            ext = guaranteed_extreme(e, obs)
            sigs = find_signals(e, obs, self.cfg.safety_margin, self.cfg.enable_yes_bets)
            latest = obs[-1] if obs else None
            events.append({
                "city": e.city, "kind": e.kind, "station": e.station, "unit": e.unit,
                "local_time": e.local_now(now_dt).strftime("%H:%M"),
                "observed": ext[0] if ext else None,
                "latest_temp_c": latest.temp_c if latest else None,
                "obs_age_min": round((now_dt - latest.time).total_seconds() / 60, 1) if latest else None,
                "dead": len(sigs), "buckets": len(e.markets), "slug": e.slug,
            })
        positions = [{
            "city": p.signal.city, "kind": p.signal.kind, "bucket": p.signal.market.bucket.label,
            "outcome": p.signal.outcome, "shares": round(p.shares, 2), "cost": round(p.cost, 2),
            "sold": round(p.sold_shares, 2), "status": p.status, "age_s": round(time.time() - p.opened_at),
        } for p in self.executor.positions if p.status != "closed"]
        return {"events": events, "positions": positions}

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            t0 = time.time()
            try:
                self.step()
            except (TradingNotAllowed, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive, but record what happened
                log.exception("unexpected error")
                self.journal.write("error", error=repr(exc))
            stop.wait(max(0.5, poll_interval(self.cfg) - (time.time() - t0)))


class _FeedLogHandler(logging.Handler):
    def __init__(self, sink: collections.deque) -> None:
        super().__init__(level=logging.INFO)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.append({"ts": record.created, "level": record.levelname, "msg": record.getMessage()})
        except Exception:  # noqa: BLE001
            pass


class Engine:
    """Owns the bot thread for the dashboard. All public methods are thread-safe."""

    def __init__(self, home: str | os.PathLike) -> None:
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.runner: BotRunner | None = None
        self.status = "stopped"          # stopped / running / error
        self.error: str | None = None
        self.started_at: float | None = None
        self.trades: collections.deque = collections.deque(maxlen=400)   # buys, sells, closes, errors
        self.signals: collections.deque = collections.deque(maxlen=200)
        self.counts: dict[str, collections.Counter] = {}                   # local day -> counters
        self.logs: collections.deque = collections.deque(maxlen=300)
        logging.getLogger("weatherbot").addHandler(_FeedLogHandler(self.logs))
        self.cfg = self.load_config()
        self.journal = Journal(str(self.home / self.cfg.journal_dir))
        self.journal.subscribe(self._on_row)
        self.bank = Bankroll(self.cfg, self.home / self.cfg.state_file)
        for row in read_journal(str(self.home / self.cfg.journal_dir), since_ts=time.time() - 86400):
            self._on_row(row)

    def load_config(self) -> Config:
        from .settings import apply_secret_env
        apply_secret_env()
        return load_config(self.home / ".env")

    def _on_row(self, row: dict) -> None:
        kind = row.get("type")
        day = time.strftime("%Y-%m-%d", time.localtime(row.get("ts", time.time())))
        c = self.counts.setdefault(day, collections.Counter())
        c[kind] += 1
        if kind == "close":
            c["pnl_cents"] += round(float(row.get("pnl") or 0) * 100)
            if float(row.get("pnl") or 0) > 0:
                c["wins"] += 1
        if len(self.counts) > 3:
            for old in sorted(self.counts)[:-3]:
                del self.counts[old]
        if kind in TRADE_TYPES:
            self.trades.append(row)
        elif kind == "signal":
            self.signals.append(row)

    # -- control -------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.cfg = self.load_config()
            self.bank.cfg = self.cfg
            self.error = None
            try:
                self.runner = BotRunner(self.cfg, self.journal, self.bank)
            except (TradingNotAllowed, SystemExit) as exc:
                self.status, self.error = "error", str(exc)
                log.warning("%s", exc)
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, name="weatherbot", daemon=True)
            self.status, self.started_at = "running", time.time()
            self.journal.write("start", mode=self.cfg.mode)
            self._thread.start()

    def _run(self) -> None:
        try:
            self.runner.run(self._stop)
            with self._lock:
                self.status = "stopped"
        except (TradingNotAllowed, SystemExit) as exc:
            with self._lock:
                self.status, self.error = "error", str(exc)
            log.warning("bot stopped: %s", exc)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.status, self.error = "error", repr(exc)
            log.exception("bot crashed")

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread:
            thread.join(timeout=30)
        with self._lock:
            if self.status == "running":
                self.status = "stopped"
            self.journal.write("stop")

    def restart(self) -> None:
        was_running = self.status == "running"
        self.stop()
        if was_running:
            self.start()

    def reset_bankroll(self, amount: float) -> None:
        with self._lock:
            self.bank.reset(amount)
            log.info("new start amount: %.2f", amount)

    # -- view ----------------------------------------------------------------
    def _daily(self, days: int = 14) -> list[dict]:
        """Profit per local day, from the balance history."""
        hist = self.bank.state["history"]
        by_day: dict[str, float] = {}
        prev = None
        for ts, bal in hist:
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            if prev is not None:
                by_day[day] = by_day.get(day, 0.0) + (bal - prev)
            else:
                by_day.setdefault(day, 0.0)
            prev = bal
        out = []
        for i in range(days - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            out.append({"day": day, "pnl": round(by_day.get(day, 0.0), 4)})
        return out

    def snapshot(self) -> dict:
        with self._lock:
            runner, status, error = self.runner, self.status, self.error
            cfg = self.cfg
        c = self.counts.get(time.strftime("%Y-%m-%d", time.localtime()), collections.Counter())
        view = runner.view if runner else {"events": [], "positions": []}
        health = dict(runner.health) if runner else {}
        return {
            "status": status, "error": error, "mode": cfg.mode, "started_at": self.started_at,
            "now": time.time(),
            "bank": self.bank.snapshot(),
            "history": self.bank.history(),
            "daily": self._daily(),
            "today": {
                "signals": c["signal"], "opportunities": c["opportunity"], "buys": c["buy"],
                "closes": c["close"], "wins": c["wins"], "pnl": c["pnl_cents"] / 100,
            },
            "events": view["events"],
            "positions": view["positions"],
            "trades": list(reversed(list(self.trades)))[:150],
            "signals": list(reversed(list(self.signals)))[:60],
            "logs": list(reversed(list(self.logs)))[:120],
            "health": health,
            "settings": {"trade_pct": cfg.trade_pct, "market_pct": cfg.market_pct, "open_pct": cfg.open_pct,
                         "daily_loss_pct": cfg.daily_loss_pct, "poll_seconds": cfg.poll_seconds},
        }
