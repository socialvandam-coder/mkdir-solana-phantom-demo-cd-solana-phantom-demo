"""The bot loop, plus a thread-safe controller the dashboard uses (start/stop/snapshot)."""
from __future__ import annotations

import collections
import dataclasses
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .bankroll import HALT_LABELS, USER_CLEARABLE, Bankroll, next_local_midnight
from .config import Config, load_config
from .execution import Executor, LiveExecutor, PaperExecutor, Position, position_row
from .geoblock import check_geoblock
from .journal import Journal, read_journal
from .markets import TempEvent, fetch_events
from .metar import FailoverSource, ObservationStore, make_source, plausible
from .notify import Notifier
from .strategy import find_signals, guaranteed_extreme

log = logging.getLogger("weatherbot")

TRADE_TYPES = {"buy", "sell", "sell_order", "close", "buy_error", "error", "start", "stop",
               "unconfirmed", "halt", "resume"}

COUNTRY_NL = {"NL": "Nederland", "BE": "België", "DE": "Duitsland", "FR": "Frankrijk", "IT": "Italië",
              "GB": "het Verenigd Koninkrijk", "UK": "het Verenigd Koninkrijk", "US": "de Verenigde Staten",
              "PL": "Polen", "CH": "Zwitserland", "AU": "Australië", "SG": "Singapore", "TH": "Thailand"}

GAP_SECONDS = 60          # a longer pause between two loops = the bot was not looking (Mac asleep?)
GAP_NOTIFY_SECONDS = 120
PAUSE_SECONDS = 30 * 60   # automatic pause after an unconfirmed alarm or a burst of order errors
MAX_CLOCK_SKEW = 10.0


class TradingNotAllowed(RuntimeError):
    pass


def live_allowed(geo: dict | None, cfg: Config) -> bool:
    """Live trading only when Polymarket says so AND the country is not on our own block list."""
    if not geo or geo.get("blocked", True):
        return False
    return (geo.get("country") or "").upper() not in cfg.hard_block_countries


def live_block_text(geo: dict | None) -> str:
    if not geo:
        return "Nog niet gecontroleerd of Polymarket handelen vanaf hier toestaat. Alleen oefenmodus."
    country = (geo.get("country") or "").upper()
    if not country:
        return "Kon niet controleren of Polymarket handelen vanaf hier toestaat. Alleen oefenmodus."
    return f"Polymarket staat handelen vanuit {COUNTRY_NL.get(country, country)} niet toe. Alleen oefenmodus."


def geo_info(geo: dict, cfg: Config, now: float) -> dict:
    """What we keep from Polymarket's answer (no IP address)."""
    info = {"blocked": bool(geo.get("blocked", True)), "country": geo.get("country"),
            "region": geo.get("region"), "checked_at": now, "error": geo.get("error")}
    info["live_allowed"] = live_allowed(info, cfg)
    info["text"] = "" if info["live_allowed"] else live_block_text(info)
    return info


def require_live_allowed(session: requests.Session, cfg: Config, check=check_geoblock) -> dict:
    """Used before starting in live mode. Raises TradingNotAllowed with a Dutch message."""
    info = geo_info(check(session), cfg, time.time())
    if not info["live_allowed"]:
        raise TradingNotAllowed(info["text"].replace("Alleen oefenmodus.", "Live handelen is uitgeschakeld."))
    return info


def short_error(exc: Exception) -> str:
    """One readable line instead of a urllib3 traceback string."""
    url = getattr(getattr(exc, "request", None), "url", "") or ""
    host = url.split("/")[2] if url.count("/") >= 2 else ""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return f"geen verbinding met {host or 'de server'}"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"{host or 'server'} gaf fout {exc.response.status_code}"
    return str(exc)[:200]


# Minutes past the hour when airport reports come out. Routine METARs are stamped :00/:30
# (or :20/:50), but many stations publish them a few minutes *before* that time - the
# backtest showed his buys at e.g. :28 for Wellington's :30 report and :55 for Sao Paulo's :00.
FAST_MINUTES = set(range(45, 60)) | set(range(0, 6)) | set(range(15, 36))


def poll_interval(cfg: Config, now: float | None = None) -> float:
    minute = time.gmtime(now if now is not None else time.time()).tm_min
    return min(cfg.poll_seconds, cfg.fast_poll_seconds) if minute in FAST_MINUTES else cfg.poll_seconds


def make_session() -> requests.Session:
    """HTTP session with quick automatic retries for brief network hiccups.

    Only used for reading data (weather, market lists, order books). Orders go through
    py-clob-client's own connection and are never retried automatically.
    """
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers["User-Agent"] = "weatherbot/1.0"
    retry = Retry(total=2, connect=2, read=1, backoff_factor=0.3,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET", "POST"}),
                  respect_retry_after_header=False)
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class BotRunner:
    """One running bot: markets + live observations + executor. `step()` is one loop."""

    def __init__(self, cfg: Config, journal: Journal, bankroll: Bankroll,
                 session: requests.Session | None = None, executor: Executor | None = None,
                 notifier: Notifier | None = None, geo_check=None, initial_geo: dict | None = None) -> None:
        self.cfg = cfg
        self.journal = journal
        self.bank = bankroll
        self.session = session or make_session()
        self.notifier = notifier or Notifier(journal)
        self.geo_check = geo_check or check_geoblock
        self.health = {"metar_ok": None, "metar_error": None, "metar_at": None, "markets_ok": None,
                       "markets_error": None, "markets_at": None, "last_step": None, "step_ms": None,
                       "geoblock": initial_geo, "metar_fail_streak": 0, "metar_failures_1h": 0,
                       "metar_source": None, "metar_last_ok": None, "clock_skew_s": None,
                       "stale_stations": [], "loop_gap_s": None}
        self._metar_failures: collections.deque = collections.deque(maxlen=2000)
        if executor is not None:
            self.executor = executor
            self.executor.notifier = self.notifier
        elif cfg.live:
            # Engine.start() / main.run() already refused when live trading is not allowed here
            self.executor = LiveExecutor(cfg, self.session, journal, bankroll, self.notifier)
        else:
            self.executor = PaperExecutor(cfg, self.session, journal, bankroll, self.notifier)
        self.executor.recheck_hook = self.recheck_position
        self.source = make_source(cfg.metar_source, self.session)
        self.store = ObservationStore()
        self.events: list[TempEvent] = []
        self.last_refresh = 0.0
        self.last_geo = initial_geo["checked_at"] if initial_geo else 0.0
        self.last_clock = 0.0
        self._last_wall: float | None = None
        self._last_mono: float | None = None
        # alarms from before a restart were already handled (and their pause saved): don't trip on them again
        self._tripped: dict[str, float] = {
            "onbevestigd": max((p.unconfirmed_at or 0.0 for p in self.executor.unconfirmed()), default=0.0)}
        self.view: dict = {"events": [], "positions": []}

    # -- Polymarket access (geoblock) --------------------------------------
    def check_access(self, now: float) -> dict:
        """Ask Polymarket whether trading is allowed from here. Never stops the loop:
        when live trading becomes impossible, buying is halted and open positions are
        still sold and settled."""
        prev = self.health.get("geoblock")
        geo = self.geo_check(self.session)
        self.last_geo = now
        if geo.get("error") and prev and not prev.get("error") and now - prev.get("checked_at", 0) < 3600:
            # one failed check keeps the last good answer for up to an hour
            self.health["geoblock"] = dict(prev, last_error=str(geo["error"])[:200])
            return self.health["geoblock"]
        info = geo_info(geo, self.cfg, now)
        self.health["geoblock"] = info
        if prev is None or prev.get("live_allowed") != info["live_allowed"] or prev.get("country") != info["country"]:
            self.journal.write("geoblock", blocked=info["blocked"], country=info["country"], region=info["region"],
                               live_allowed=info["live_allowed"], error=info["error"])
            if prev is not None and prev.get("live_allowed") != info["live_allowed"]:
                self.notifier.notify("geoblock", "Polymarket-toegang veranderd",
                                     "Live handelen is nu toegestaan." if info["live_allowed"] else info["text"])
        if self.cfg.live and not info["live_allowed"]:
            self._halt("geoblock", detail=info["country"] or "")
        else:
            self._resume("geoblock", "auto")
        return info

    def _check_clock(self, now: float) -> None:
        """Difference between this computer's clock and Polymarket's (orders depend on it)."""
        try:
            r = self.session.get(f"{self.cfg.clob_host}/time", timeout=(3, 5))
            r.raise_for_status()
            server = float(r.json())
            self.health["clock_skew_s"] = round(time.time() - server, 1)
        except Exception as exc:  # noqa: BLE001 - only informative unless it clearly fails
            log.debug("clock check failed: %s", exc)
        self.last_clock = now

    # -- wake-up / gap detection ----------------------------------------------
    def _detect_gap(self, now: float) -> float | None:
        mono = time.monotonic()
        gap = None
        if self._last_wall is not None:
            wall_gap = now - self._last_wall
            if wall_gap > GAP_SECONDS or wall_gap - (mono - self._last_mono) > 20:
                gap = wall_gap
        self._last_wall, self._last_mono = now, mono
        if gap is None:
            return None
        self.health["loop_gap_s"] = round(gap)
        self.journal.write("gap", **{"from": self._last_wall - gap, "to": now, "seconds": round(gap, 1)})
        log.warning("the bot did not look for %.0f s (%s - %s); checking everything again", gap,
                    time.strftime("%H:%M", time.localtime(now - gap)), time.strftime("%H:%M", time.localtime(now)))
        if gap > GAP_NOTIFY_SECONDS:
            self.notifier.notify("gap", "Bot stond stil",
                                 f"Weerbot heeft {round(gap / 60)} min niet gekeken (sliep de Mac?). "
                                 "Hij controleert nu alles opnieuw.")
        return gap

    # -- the loop ------------------------------------------------------------
    def step(self) -> None:
        t0 = time.time()
        now_dt = datetime.now(timezone.utc)
        woke = self._detect_gap(t0) is not None
        if woke or t0 - self.last_geo >= self.cfg.geoblock_every_seconds:
            self.check_access(t0)
        if woke or t0 - self.last_clock >= (600 if "klok" in self.bank.halts else 3600):
            self._check_clock(t0)
        if woke or t0 - self.last_refresh >= self.cfg.market_refresh_seconds or not self.events:
            try:
                self.events = fetch_events(self.session, now_dt, self.cfg.cities)
                self.last_refresh = t0
                self.health.update(markets_ok=True, markets_error=None, markets_at=t0)
                log.info("watching %d markets on %d stations", len(self.events), len({e.station for e in self.events}))
            except requests.RequestException as exc:
                self.health.update(markets_ok=False, markets_error=short_error(exc), markets_at=t0)
                log.warning("market list failed: %s", short_error(exc))

        live_events = [e for e in self.events if e.is_live(now_dt)]
        stations = sorted({e.station for e in live_events})
        if stations:
            try:
                for obs in self.source.fetch(stations):
                    self.store.add(obs)
                self.health.update(metar_ok=True, metar_error=None, metar_at=t0, metar_fail_streak=0,
                                   metar_last_ok=t0, metar_source=getattr(self.source, "last_used", None))
            except requests.RequestException as exc:
                self._metar_failures.append(t0)
                streak = self.health["metar_fail_streak"] + 1
                self.health.update(metar_ok=False, metar_error=short_error(exc), metar_at=t0,
                                   metar_fail_streak=streak)
                log.warning("weather data failed (both sources, %dx in a row): %s", streak, short_error(exc))
        while self._metar_failures and self._metar_failures[0] < t0 - 3600:
            self._metar_failures.popleft()
        self.health["metar_failures_1h"] = len(self._metar_failures)
        self.store.prune(now_dt)

        self.executor.refresh(time.time())
        # a station that went quiet (no report for 2x its usual interval) is not traded on
        self.executor.stale_events = {e.slug for e in live_events if self.store.is_stale(e.station, now_dt)}
        signals = []
        for e in live_events:
            signals.extend(find_signals(e, self.store.get(e.station), self.cfg.safety_margin, self.cfg.enable_yes_bets))
        self.executor.on_signals(signals, time.time())
        self.executor.tick(time.time())
        self.tripwires(time.time(), live_events, now_dt)

        self.health.update(last_step=time.time(), step_ms=round((time.time() - t0) * 1000))
        self.view = self._build_view(live_events, now_dt)

    # -- data re-check for an unconfirmed position ---------------------------------
    def recheck_position(self, p: Position) -> bool | None:
        """Is the bucket still dead when the station is fetched again from the other source
        (including corrections)? True = yes, False = no (sell now), None = could not check."""
        ev = next((e for e in self.events if e.slug == p.signal.event_slug), None)
        if ev is None:
            return None
        src = self.source
        sources = []
        if isinstance(src, FailoverSource):
            other = src.backup if src.last_used == src.names[0] else src.primary
            sources = [other, src]
        else:
            sources = [src]
        fetched = None
        for source in sources:
            try:
                fetched = source.fetch([ev.station])
                break
            except requests.RequestException as exc:
                log.info("re-check fetch failed: %s", short_error(exc))
        if fetched is None:
            return None
        merged = {o.time: o for o in self.store.get(ev.station)}
        corrected = False
        for o in fetched:
            if o.station != ev.station or not plausible(o):
                continue
            old = merged.get(o.time)
            is_cor = " COR " in f" {o.raw} "
            if old is None or (is_cor and o.raw != old.raw):
                corrected = corrected or (old is not None)
                merged[o.time] = o
                self.store.add(o)
        # judge this one bucket on the data alone: no safety margin, ignore 'accepting orders'
        market = dataclasses.replace(p.signal.market, accepting_orders=True)
        probe = dataclasses.replace(ev, markets=[market])
        sigs = find_signals(probe, list(merged.values()), 0, True)
        still_dead = any(s.token_id == p.token_id for s in sigs)
        ext = guaranteed_extreme(probe, list(merged.values()))
        log.warning("re-check %s %s: %s (extreme now %s%s)", p.signal.city, p.signal.market.bucket.label,
                    "still dead" if still_dead else "NOT dead", ext[0] if ext else "?",
                    ", after a correction (COR)" if corrected else "")
        return still_dead

    # -- automatic tripwires ---------------------------------------------------
    def _halt(self, reason: str, until: float | None = None, detail: str = "") -> None:
        if self.bank.set_halt(reason, "auto", until=until, detail=detail):
            self.journal.write("halt", reason=reason, by="auto", until=until, detail=detail)
            log.warning("buying paused: %s %s", reason, detail)
            when = ("Kopen gaat vanzelf verder als het probleem weg is." if until is None
                    else f"Kopen gaat vanzelf verder om {time.strftime('%H:%M', time.localtime(until))}.")
            if reason == "drawdown":
                when = "Druk in het dashboard op Start om weer te kopen."
            self.notifier.notify("halt", "Weerbot koopt even niets",
                                 f"Gepauzeerd {HALT_LABELS.get(reason, reason)}. Open posities worden gewoon "
                                 f"verkocht. {when}")

    def _resume(self, reason: str, by: str) -> None:
        if self.bank.clear_halt(reason):
            self.journal.write("resume", reason=reason, by=by)
            log.info("buying resumed (%s)", reason)

    def tripwires(self, now: float, live_events: list[TempEvent] | None = None,
                  now_dt: datetime | None = None) -> None:
        """Pause buying by itself when something is clearly wrong. Selling always continues."""
        b, cfg = self.bank, self.cfg
        now_dt = now_dt or datetime.fromtimestamp(now, timezone.utc)
        for reason in b.expired_halts(now):
            self._resume(reason, "auto")
        if b.stopped_for_today():
            self._halt("dagverlies", until=next_local_midnight(now),
                       detail=f"{b.today_equity_pnl:.2f} sinds middernacht")
        if b.drawdown_pct >= cfg.max_drawdown_pct:
            self._halt("drawdown", detail=f"{b.drawdown_pct:.1f}% onder de hoogste stand")
        unconfirmed = self.executor.unconfirmed()
        if unconfirmed:
            newest = max(p.unconfirmed_at for p in unconfirmed)
            cost = sum(p.cost for p in unconfirmed)
            if ((len(unconfirmed) >= 2 or cost > cfg.unconfirmed_pct / 100.0 * max(b.equity, 0.0))
                    and newest > self._tripped.get("onbevestigd", 0.0)):
                self._tripped["onbevestigd"] = now
                b.clear_halt("onbevestigd")      # a new alarm starts a fresh 30 minutes
                self._halt("onbevestigd", until=now + PAUSE_SECONDS,
                           detail=f"{len(unconfirmed)} onbevestigd, ${cost:.2f}")
        if self.executor.errors_since(max(now - 600, self._tripped.get("fouten", 0.0))) >= 5:
            self._tripped["fouten"] = now
            b.clear_halt("fouten")
            self._halt("fouten", until=now + PAUSE_SECONDS, detail="5 of meer fouten in 10 minuten")
        skew = self.health.get("clock_skew_s")
        if cfg.live and skew is not None and abs(skew) > MAX_CLOCK_SKEW:
            self._halt("klok", detail=f"{skew:+.0f} s")
        else:
            self._resume("klok", "auto")
        # weather data: failing again and again, or most stations silent
        events = live_events if live_events is not None else [e for e in self.events if e.is_live(now_dt)]
        stations = sorted({e.station for e in events})
        stale = [st for st in stations if self.store.is_stale(st, now_dt, factor=1.5, extra_s=300)]
        self.health["stale_stations"] = stale
        failing = self.health.get("metar_fail_streak", 0) >= 6
        mostly_stale = bool(self.health.get("metar_last_ok")) and bool(stations) and len(stale) > 0.5 * len(stations)
        if failing or mostly_stale:
            self._halt("weerdata", detail=(f"{self.health.get('metar_fail_streak')}x mislukt" if failing
                                           else f"{len(stale)}/{len(stations)} stations stil"))
        else:
            self._resume("weerdata", "auto")

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
                "locked": e.slug in self.executor.locked_events,
            })
        return {"events": events, "positions": self.executor.position_rows(time.time())}

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            t0 = time.time()
            try:
                self.step()
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive, but record what happened
                log.exception("unexpected error")
                self.journal.write("error", error=repr(exc))
            wait = poll_interval(self.cfg)
            if self.executor.wants_fast_loop(time.time()):
                wait = min(wait, self.cfg.fast_poll_seconds)   # first minute after a signal or buy
            stop.wait(max(0.5, wait - (time.time() - t0)))


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
        self.notifier = Notifier(self.journal)
        self.bank = Bankroll(self.cfg, self.home / self.cfg.state_file)
        self._geo: dict | None = None           # last Polymarket access check (when the bot is not running)
        self.geo_check = check_geoblock
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
        elif kind == "gap":
            c["gap_s"] += round(float(row.get("seconds") or 0))
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
                geo = None
                if self.cfg.live:
                    # the only place that refuses to start: live mode where Polymarket does not allow it
                    geo = self._geo = require_live_allowed(make_session(), self.cfg, self.geo_check)
                self.runner = BotRunner(self.cfg, self.journal, self.bank, notifier=self.notifier,
                                        geo_check=self.geo_check, initial_geo=geo)
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

    # -- buying pause / emergency stop (the loop keeps selling and settling) -------
    def pause(self, on: bool) -> None:
        """'Pauzeer kopen': stop buying, keep selling what is open."""
        with self._lock:
            if on and self.bank.set_halt("pauze", "user"):
                self.journal.write("halt", reason="pauze", by="user")
                log.info("buying paused by the user")
            elif not on and self.bank.clear_halt("pauze"):
                self.journal.write("resume", reason="pauze", by="user")
                log.info("buying resumed by the user")

    def emergency_stop(self) -> None:
        """'Noodstop': no more buys until the user resumes. The 0.99 sell orders stay up
        (they are the way out), and open positions keep being sold and settled."""
        with self._lock:
            if self.bank.set_halt("noodstop", "user"):
                self.journal.write("halt", reason="noodstop", by="user")
                log.warning("NOODSTOP: buying stopped by the user; open positions are still sold")

    def resume(self) -> list[str]:
        """'Hervat': lift the pauses the user may lift. Returns the reasons that remain."""
        with self._lock:
            for reason in [r for r in self.bank.halts if r in USER_CLEARABLE]:
                if reason == "drawdown":
                    self.bank.state["peak_equity"] = round(self.bank.equity, 6)   # start counting again from here
                if self.bank.clear_halt(reason):
                    self.journal.write("resume", reason=reason, by="user")
            if self.runner is not None:
                self.runner._tripped["onbevestigd"] = time.time()
                self.runner._tripped["fouten"] = time.time()
            self.bank.save()
            return list(self.bank.halts)

    def live_access(self, refresh: bool = False) -> dict | None:
        """Last answer to 'may this connection trade live?' (checks again when asked and stale)."""
        runner = self.runner
        geo = (runner.health.get("geoblock") if runner else None) or self._geo
        if refresh and (geo is None or time.time() - geo.get("checked_at", 0) > self.cfg.geoblock_every_seconds):
            geo = self._geo = geo_info(self.geo_check(make_session()), self.cfg, time.time())
        return geo

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
        if not health.get("geoblock") and self._geo:
            health["geoblock"] = self._geo
        positions = view["positions"]
        if runner is None or status != "running":
            # the loop is not running: show what is open from the saved state
            now = time.time()
            positions = [position_row(p, now, cfg) for p in
                         (Position.from_dict(d) for d in self.bank.state.get("positions", [])) if p]
        elif runner.health.get("last_step") is None:
            positions = runner.executor.position_rows(time.time())   # first loop still busy
        thread = self._thread
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
            "positions": positions,
            "loop_alive": bool(thread and thread.is_alive()),
            "halt": self.bank.halt_info(),
            "trades": list(reversed(list(self.trades)))[:150],
            "signals": list(reversed(list(self.signals)))[:60],
            "logs": list(reversed(list(self.logs)))[:120],
            "health": health,
            "settings": {"trade_pct": cfg.trade_pct, "market_pct": cfg.market_pct, "open_pct": cfg.open_pct,
                         "daily_loss_pct": cfg.daily_loss_pct, "poll_seconds": cfg.poll_seconds,
                         "event_pct": cfg.event_pct, "max_drawdown_pct": cfg.max_drawdown_pct,
                         "max_trade_usdc": cfg.max_trade_usdc, "max_event_usdc": cfg.max_event_usdc,
                         "max_buy_price": cfg.max_buy_price, "sell_price": cfg.sell_price,
                         "confirm_seconds": cfg.confirm_seconds},
        }
