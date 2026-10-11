"""The bot loop, plus a thread-safe controller the dashboard uses (start/stop/snapshot)."""
from __future__ import annotations

import collections
import dataclasses
import logging
import math
import os
import statistics
import threading
import time
from datetime import datetime, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .bankroll import HALT_LABELS, USER_CLEARABLE, Bankroll, next_local_midnight
from .cities import CITY_TZ
from .compare import compare_today
from .config import Config, load_config
from .execution import Executor, LiveExecutor, PaperExecutor, Position, position_row, skip_labels
from .geoblock import check_geoblock
from .journal import Journal, read_journal
from .markets import TempEvent, fetch_events
from .metar import FailoverSource, ObservationStore, TgftpSource, make_source, plausible
from .notify import Notifier
from .power import power_status
from .strategy import find_signals, guaranteed_extreme

log = logging.getLogger("weatherbot")

TRADE_TYPES = {"buy", "sell", "sell_order", "close", "buy_error", "error", "start", "stop",
               "unconfirmed", "halt", "resume"}

COUNTRY_NL = {"NL": "Nederland", "BE": "België", "DE": "Duitsland", "FR": "Frankrijk", "IT": "Italië",
              "GB": "het Verenigd Koninkrijk", "UK": "het Verenigd Koninkrijk", "US": "de Verenigde Staten",
              "PL": "Polen", "CH": "Zwitserland", "AU": "Australië", "SG": "Singapore", "TH": "Thailand"}

GAP_SECONDS = 60          # a longer pause between two loops = the bot was not looking (Mac asleep?)
SLEEP_DRIFT_SECONDS = 20  # the wall clock ran this much further than the monotonic clock = the Mac slept
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


# Timing. Every station has its own report moments (cities.STATION_MINUTES, then measured):
# around them the bot looks at that station every 2 s; all stations every POLL_SECONDS.
HOT_POLL_SECONDS = 2.0          # quick look at the stations whose report is due now
FAST_LOOP_SECONDS = 1.0         # loop pace in the first minute after a signal or a buy
HOT_SOURCE_BACKOFF = 60.0       # the quick-look source failed: leave it alone this long
MIDNIGHT_REFRESH_SECONDS = 60.0  # market list refresh around a watched city's midnight
MIDNIGHT_FROM, MIDNIGHT_TO = dtime(23, 40), dtime(0, 10)
MARKET_RETRY_SECONDS = 30.0     # the market list failed: try again after this
POWER_EVERY_SECONDS = 300.0     # look whether the Mac runs on battery


def near_local_midnight(now_utc: datetime, tzs) -> bool:
    """True when it is between 23:40 and 00:10 in any of these time zones: a new day's
    market starts there, so the market list is refreshed every minute."""
    for tz in tzs:
        try:
            t = now_utc.astimezone(ZoneInfo(tz)).time()
        except (KeyError, ValueError):
            continue
        if t >= MIDNIGHT_FROM or t < MIDNIGHT_TO:
            return True
    return False


def make_hot_source(session: requests.Session):
    """The quick look: NOAA's per-station files, asked 'changed since?' (If-Modified-Since)."""
    return TgftpSource(session)


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
                       "metar_source": None, "metar_primary": cfg.metar_source, "metar_last_ok": None,
                       "clock_skew_s": None,
                       "stale_stations": [], "loop_gap_s": None, "hot_stations": [], "hot_ok": None,
                       "hot_error": None, "hot_at": None, "power": None, "stations": [],
                       "market_refresh_s": cfg.market_refresh_seconds, "last_wake": None,
                       "sources": {}, "errors": [], "seen_delay_s_p50": None}
        self._metar_failures: collections.deque = collections.deque(maxlen=2000)
        self._market_fail_streak = 0
        self._markets_last_ok: float | None = None
        self._last_errors: dict[str, tuple[float, str]] = {}          # source -> (when, text) of its last error
        self.recent_errors: collections.deque = collections.deque(maxlen=20)   # for the dashboard's details sheet
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
        self.hot_source = make_hot_source(self.session)
        self.power_check = power_status
        self.store = ObservationStore()
        self.events: list[TempEvent] = []
        self.last_refresh = 0.0
        self._market_retry_at = 0.0
        self.last_full = 0.0             # last fetch of all stations
        self._last_hot = 0.0             # last quick look at the hot stations
        self._hot_down_until = 0.0
        self.hot: list[str] = []         # stations whose report is due right now
        self._fetched_once: set[str] = set()   # stations fetched since the start (or the last wake-up)
        self.last_power = 0.0
        self._last_prune = 0.0
        self.last_geo = initial_geo["checked_at"] if initial_geo else 0.0
        self.last_clock = 0.0
        self._last_wall: float | None = None
        self._last_mono: float | None = None
        # alarms from before a restart were already handled (and their pause saved): don't trip on them again
        self._tripped: dict[str, float] = {
            "onbevestigd": max((p.unconfirmed_at or 0.0 for p in self.executor.unconfirmed()), default=0.0)}
        self.view: dict = {"events": [], "positions": []}
        self._load_report_timing()

    def _load_report_timing(self) -> None:
        """Reload the measured report moments of the last 2 days, so a restart keeps them."""
        try:
            rows = read_journal(str(self.journal.dir), since_ts=time.time() - 48 * 3600)
        except OSError:
            return
        n = 0
        for row in rows:
            if row.get("type") != "report_seen":
                continue
            try:
                n += self.store.note_seen(str(row["station"]), datetime.fromisoformat(row["obs_time"]),
                                          float(row["first_seen"]))
            except (KeyError, TypeError, ValueError):
                continue
        if n:
            log.info("reloaded %d measured report times", n)

    def _note_error(self, source: str, text: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._last_errors[source] = (now, text)
        self.recent_errors.append({"ts": now, "what": source, "text": text[:200]})

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
        gap, slept, prev = None, False, self._last_wall
        if self._last_wall is not None:
            wall_gap = now - self._last_wall
            drift = wall_gap - (mono - self._last_mono)    # the monotonic clock stands still while the Mac sleeps
            if wall_gap > GAP_SECONDS or drift > SLEEP_DRIFT_SECONDS:
                gap, slept = wall_gap, drift > SLEEP_DRIFT_SECONDS
        self._last_wall, self._last_mono = now, mono
        if gap is None:
            return None
        self.health["loop_gap_s"] = round(gap)
        self.health["last_wake"] = {"from": prev, "to": now, "seconds": round(gap), "slept": slept}
        self.journal.write("gap", **{"from": prev, "to": now, "seconds": round(gap, 1), "slept": slept})
        t_from, t_to = time.strftime("%H:%M", time.localtime(prev)), time.strftime("%H:%M", time.localtime(now))
        if slept:
            log.warning("Mac sliep van %s tot %s: alles wordt opnieuw opgehaald en deze ronde koopt de bot niets",
                        t_from, t_to)
        else:
            log.warning("Bot keek %.0f s niet (van %s tot %s): alles wordt opnieuw opgehaald en deze ronde "
                        "koopt de bot niets", gap, t_from, t_to)
        if gap > GAP_NOTIFY_SECONDS:
            self.notifier.notify("gap", "Bot stond stil",
                                 f"Weerbot heeft {round(gap / 60)} min niet gekeken (sliep de Mac?). "
                                 "Hij controleert nu alles opnieuw.")
        return gap

    def _after_wake(self, now: float) -> None:
        """After a sleep: fetch the full report history again, look at every position and its
        0.99 order right away (live re-posts a missing one), and buy nothing this loop."""
        if hasattr(self.source, "reprime"):
            self.source.reprime()
        self._fetched_once.clear()       # what comes in now was published while we slept
        self.last_full = 0.0             # all stations right away ...
        self._last_hot = now             # ... and the quick look from the next loop on
        self._hot_down_until = 0.0
        self.executor.wake(now)

    def _check_power(self) -> None:
        prev = self.health.get("power")
        info = self.power_check()
        self.health["power"] = info
        if not info or (prev and prev.get("on_battery") == info.get("on_battery")):
            return
        self.journal.write("power", on_battery=info["on_battery"], percent=info.get("percent"))
        if info["on_battery"]:
            log.warning("Mac op batterij: sluit de lader aan, anders kan de Mac gaan slapen en stopt de bot")
            self.notifier.notify("power", "Mac op batterij",
                                 "Sluit de lader aan en laat de klep open, anders kan de Mac gaan slapen "
                                 "en stopt de bot.")
        elif prev:
            log.info("Mac weer aan de stroom")

    # -- markets -----------------------------------------------------------------
    def _watched_tzs(self) -> set[str]:
        return {e.tz for e in self.events} | {CITY_TZ[c] for c in self.cfg.cities if c in CITY_TZ}

    def _refresh_markets(self, t0: float, now_dt: datetime, woke: bool) -> None:
        every = self._refresh_every(now_dt)
        due = woke or ((t0 - self.last_refresh >= every or not self.events) and t0 >= self._market_retry_at)
        if due:
            try:
                # also keep what becomes watchable before the next normal refresh (tomorrow from 23:30)
                self.events = fetch_events(self.session, now_dt, self.cfg.cities,
                                           ahead_s=self.cfg.market_refresh_seconds + 60)
                self.last_refresh = t0
                self._market_fail_streak, self._markets_last_ok = 0, t0
                self.health.update(markets_ok=True, markets_error=None, markets_at=t0)
                log.info("watching %d markets on %d stations", len(self.events),
                         len({e.station for e in self.events}))
            except requests.RequestException as exc:
                self._market_retry_at = t0 + MARKET_RETRY_SECONDS
                self._market_fail_streak += 1
                self._note_error("markten", short_error(exc), t0)
                self.health.update(markets_ok=False, markets_error=short_error(exc), markets_at=t0)
                log.warning("market list failed: %s", short_error(exc))
        self.health["market_refresh_s"] = self._refresh_every(now_dt)

    def _refresh_every(self, now_dt: datetime) -> float:
        if near_local_midnight(now_dt, self._watched_tzs()):
            return min(self.cfg.market_refresh_seconds, MIDNIGHT_REFRESH_SECONDS)   # a new day's market starts
        return self.cfg.market_refresh_seconds

    # -- weather -----------------------------------------------------------------
    def _ingest(self, observations, source: str, fetched: list[str]) -> int:
        """Store new reports. For stations we were already watching, the moment we first saw a
        report is journaled ('report_seen'): that is how each station's timing is learned."""
        new = 0
        for obs in observations:
            seen = time.time()
            live = obs.station in self._fetched_once
            if self.store.add(obs, datetime.fromtimestamp(seen, timezone.utc), seen_at=seen, live=live):
                new += 1
                if live:
                    self.journal.write("report_seen", station=obs.station, obs_time=obs.time.isoformat(),
                                       first_seen=round(seen, 2), delay_s=round(seen - obs.time.timestamp(), 1),
                                       source=source)
        self._fetched_once.update(fetched)
        return new

    def _fetch_weather(self, t0: float, stations: list[str]) -> None:
        """All stations every POLL_SECONDS; the stations whose report is due now every 2 s."""
        if not stations:
            self.hot = []
            self.health["hot_stations"] = []
            return
        full_via_tgftp = False
        if t0 - self.last_full >= self.cfg.poll_seconds - 0.5:
            self.last_full = t0
            try:
                observations = self.source.fetch(stations)
                used = getattr(self.source, "last_used", None)
                self._ingest(observations, used or "?", stations)
                full_via_tgftp = used == "tgftp"
                self.health.update(metar_ok=True, metar_error=None, metar_at=t0, metar_fail_streak=0,
                                   metar_last_ok=t0, metar_source=used)
            except requests.RequestException as exc:
                self._metar_failures.append(t0)
                streak = self.health["metar_fail_streak"] + 1
                self.health.update(metar_ok=False, metar_error=short_error(exc), metar_at=t0,
                                   metar_fail_streak=streak)
                self._note_error("weer", short_error(exc), t0)
                log.warning("weather data failed (both sources, %dx in a row): %s", streak, short_error(exc))
        now = time.time()
        self.hot = [st for st in stations if self.store.is_hot(st, now)]
        self.health["hot_stations"] = list(self.hot)
        if (not self.hot or full_via_tgftp or now < self._hot_down_until
                or now - self._last_hot < HOT_POLL_SECONDS - 0.25):
            return
        self._last_hot = now
        try:
            if self._ingest(self.hot_source.fetch_changed(self.hot), "tgftp", self.hot):
                self.hot = [st for st in self.hot if self.store.is_hot(st, time.time())]   # report is in
                self.health["hot_stations"] = list(self.hot)
            self.health.update(hot_ok=True, hot_error=None, hot_at=now)
        except requests.RequestException as exc:
            self._hot_down_until = now + HOT_SOURCE_BACKOFF
            self.health.update(hot_ok=False, hot_error=short_error(exc), hot_at=now)
            self._note_error("snel kijken", short_error(exc), now)
            log.info("quick look failed (%s); every %.0f s via the normal source for now", short_error(exc),
                     self.cfg.poll_seconds)

    # -- the loop ------------------------------------------------------------
    def step(self) -> None:
        t0 = time.time()
        now_dt = datetime.now(timezone.utc)
        woke = self._detect_gap(t0) is not None
        if woke:
            self._after_wake(t0)
        self.executor.hold_reason = "woke_up" if woke else None     # buy nothing in the loop right after a wake-up
        if woke or t0 - self.last_geo >= self.cfg.geoblock_every_seconds:
            self.check_access(t0)
        if woke or t0 - self.last_clock >= (600 if "klok" in self.bank.halts else 3600):
            self._check_clock(t0)
        if woke or t0 - self.last_power >= POWER_EVERY_SECONDS:
            self.last_power = t0
            self._check_power()
        self._refresh_markets(t0, now_dt, woke)

        # today's markets, and tomorrow's from 23:30 local (strategy.day_observations keeps them apart)
        live_events = [e for e in self.events if e.is_watchable(now_dt)]
        stations = sorted({e.station for e in live_events})
        self._fetch_weather(t0, stations)
        while self._metar_failures and self._metar_failures[0] < t0 - 3600:
            self._metar_failures.popleft()
        self.health["metar_failures_1h"] = len(self._metar_failures)
        if t0 - self._last_prune >= 60:
            self._last_prune = t0
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
        self.health["stations"] = self._station_rows(live_events, now_dt)
        self.health["sources"] = self.sources()
        self.health["errors"] = self.last_errors(5)
        self.view = self._build_view(live_events, now_dt)

    def sources(self) -> dict:
        """Per data source: last success, failures in a row, last error ('Werkt alles?' on the dashboard)."""
        def last_err(name):
            when, text = self._last_errors.get(name, (None, None))
            return {"last_error": text, "last_error_ts": when}
        h = self.health
        return {
            "weer": {"last_ok_ts": h.get("metar_last_ok"), "consecutive_fail": h.get("metar_fail_streak", 0),
                     **last_err("weer")},
            "markten": {"last_ok_ts": self._markets_last_ok, "consecutive_fail": self._market_fail_streak,
                        **last_err("markten")},
            "orderboek": dict(self.executor.book_status),
        }

    def last_errors(self, n: int = 5) -> list[dict]:
        """The newest errors of the data sources and of buying/selling, newest first."""
        rows = list(self.recent_errors) + list(getattr(self.executor, "error_log", []))
        return sorted(rows, key=lambda r: r["ts"], reverse=True)[:n]

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
        events = live_events if live_events is not None else [e for e in self.events if e.is_watchable(now_dt)]
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

    def _station_rows(self, live_events: list[TempEvent], now_dt: datetime) -> list[dict]:
        """Per watched station: last report, cadence, when its reports come out, measured delay."""
        cities: dict[str, set[str]] = {}
        kinds: dict[str, set[str]] = {}
        for e in live_events:
            cities.setdefault(e.station, set()).add(e.city)
            kinds.setdefault(e.station, set()).add(e.kind)
        rows, delays = [], []
        since = now_dt.timestamp() - 86400
        for st in sorted(cities):
            row = self.store.station_status(st, now_dt)
            row["cities"] = sorted(cities[st])
            row["kinds"] = sorted(kinds[st])
            last = self.store.latest(st)
            row["last_temp_c"] = last.temp_c if last else None
            rows.append(row)
            delays.extend(self.store.delays(st, since=since))
        # how long after a report's own time the bot usually saw it (all stations, last 24 h)
        self.health["seen_delay_s_p50"] = round(statistics.median(delays)) if delays else None
        self.health["seen_delay_n"] = len(delays)
        return rows

    def _build_view(self, live_events: list[TempEvent], now_dt: datetime) -> dict:
        stations = {r["station"]: r for r in self.health.get("stations") or []}
        events = []
        for e in sorted(live_events, key=lambda e: (e.city, e.kind, e.day)):
            obs = self.store.get(e.station)
            ext = guaranteed_extreme(e, obs)
            sigs = find_signals(e, obs, self.cfg.safety_margin, self.cfg.enable_yes_bets)
            latest = obs[-1] if obs else None
            st = stations.get(e.station) or {}
            events.append({
                "city": e.city, "kind": e.kind, "station": e.station, "unit": e.unit,
                "day": e.day.isoformat(), "tomorrow": e.is_tomorrow(now_dt),
                "local_time": e.local_now(now_dt).strftime("%H:%M"),
                "observed": ext[0] if ext else None,
                "latest_temp_c": latest.temp_c if latest else None,
                "obs_age_min": round((now_dt - latest.time).total_seconds() / 60, 1) if latest else None,
                "dead": len(sigs), "buckets": len(e.markets), "slug": e.slug,
                "locked": e.slug in self.executor.locked_events,
                "report_minutes": st.get("report_minutes", []), "schedule": st.get("schedule"),
                "delay_s": st.get("publish_delay_s_p50"), "hot": e.station in self.hot,
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
                self._note_error("bot", repr(exc))
            now = time.time()
            stop.wait(max(0.5, self.next_wait(now) - (now - t0)))

    def next_wait(self, now: float) -> float:
        """Seconds between two loops: POLL_SECONDS, 2 s while a station's report is due,
        1 s in the first minute after a signal or a buy."""
        wait = self.cfg.poll_seconds
        if self.hot and now >= self._hot_down_until:
            wait = min(wait, HOT_POLL_SECONDS)
        if self.executor.wants_fast_loop(now):
            wait = min(wait, FAST_LOOP_SECONDS)
        return wait


# Rows only a running bot loop writes: evidence that the bot was looking at that moment.
LOOP_TYPES = {"report_seen", "signal", "skip", "opportunity", "buy", "buy_error", "sell", "sell_order", "close",
              "gap", "geoblock", "power", "unconfirmed", "recheck", "confirmed", "error", "sell_cancel"}


def uptime(marks: list[tuple], t0: float, t1: float, alive: bool, last_loop_ts: float | None,
           stalled_from: float | None = None) -> dict:
    """How much of [t0, t1] the bot was looking, from the journal's start / stop / gap rows.

    marks: (ts, 'start', last loop row before it) / (ts, 'stop', last loop row before it) /
    (ts, 'gap', (from, to, slept)), in time order. A start without a stop means the app was
    killed: that run counts until its last loop row. alive: the loop runs right now."""
    segs: list[tuple[float, float]] = []
    run_from = None
    seen = False
    for ts, kind, info in marks:
        if kind == "gap":
            continue
        if kind == "start":
            if run_from is not None:
                segs.append((run_from, max(run_from, info or run_from)))
            elif not seen and info is not None:
                segs.append((-math.inf, info))           # an earlier run without a stop row
            run_from = ts
        elif kind == "stop":
            if run_from is not None:
                segs.append((run_from, ts))
            elif not seen and info is not None:
                segs.append((-math.inf, ts))             # running since before the journal we read
            run_from = None
        seen = True
    if run_from is not None:
        segs.append((run_from, t1 if alive else max(run_from, last_loop_ts or run_from)))
    elif not seen and last_loop_ts is not None:
        segs.append((-math.inf, t1 if alive else last_loop_ts))

    def clip(a: float, b: float) -> float:
        return max(0.0, min(b, t1) - max(a, t0))

    running = sum(clip(a, b) for a, b in segs)
    gaps = slept = 0.0
    for ts, kind, info in marks:
        if kind == "gap":
            g = clip(info[0], info[1])
            gaps += g
            slept += g if info[2] else 0.0
    if alive and stalled_from is not None:
        gaps += clip(stalled_from, t1)               # stuck right now (the gap row comes later)
    span = max(0.0, t1 - t0)
    active = max(0.0, min(span, running - gaps))
    missed = span - active
    closed = max(0.0, span - running)
    if missed < 60:
        reason = None
    elif slept >= max(gaps - slept, closed):
        reason = "slept"                 # the Mac slept
    elif closed >= gaps:
        reason = "closed"                # the app or the bot was off
    else:
        reason = "stalled"               # running, but not looking (network, a hang)
    return {"since": t0, "active_s": round(active), "missed_s": round(missed), "slept_s": round(slept),
            "closed_s": round(closed), "pct": round(active / span * 100, 1) if span > 0 else None,
            "reason": reason}


def local_midnight(now: float) -> float:
    d = datetime.fromtimestamp(now)
    return datetime(d.year, d.month, d.day).timestamp()


def percentile(values: list[float], q: float) -> float:
    xs = sorted(values)
    if not xs:
        raise ValueError("no values")
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def daily_pcts(history: list, today: str) -> list[float]:
    """The bot's own result per local day with at least one closed trade (today excluded), in %."""
    out: list[float] = []
    prev_bal: float | None = None
    day_start: float | None = None
    cur_day: str | None = None
    last_bal: float | None = None
    for ts, bal in history:
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if prev_bal is None:              # the start amount
            prev_bal = bal
            continue
        if day != cur_day:
            if cur_day is not None and cur_day != today and day_start:
                out.append((last_bal - day_start) / day_start * 100)
            cur_day, day_start = day, prev_bal
        last_bal = bal
        prev_bal = bal
    if cur_day is not None and cur_day != today and day_start:
        out.append((last_bal - day_start) / day_start * 100)
    return out


NORMAL_MIN_DAYS = 7          # own trading days before 'normaal voor jouw bot' is shown
HTT_EVERY_SECONDS = 120      # HighTempTation panel: refresh this often ...
HTT_IDLE_SECONDS = 600       # ... but only while someone looks at the dashboard


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
        # 'Werkt alles?': start/stop/gap rows of the last two days, and the newest row of a running loop
        self._marks: collections.deque = collections.deque(maxlen=5000)
        self._last_loop_ts: float | None = None
        # 'Vandaag gezien': per local day, the buckets at each step (each bucket counted once)
        self.funnel: dict[str, dict] = {}
        self.token_result: collections.OrderedDict = collections.OrderedDict()   # token -> bought / skip reason
        # 'HighTempTation vandaag' (filled by a background thread while the dashboard is open)
        self.htt: dict | None = None
        self._htt_at = 0.0
        self._last_view = 0.0
        self._bg: threading.Thread | None = None
        self._bg_stop = threading.Event()
        self.htt_session_factory = make_session
        for row in read_journal(str(self.home / self.cfg.journal_dir), since_ts=time.time() - 86400):
            self._on_row(row)

    def load_config(self) -> Config:
        from .settings import apply_secret_env
        apply_secret_env()
        return load_config(self.home / ".env")

    def _on_row(self, row: dict) -> None:
        kind = row.get("type")
        ts = float(row.get("ts") or time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        c = self.counts.setdefault(day, collections.Counter())
        c[kind] += 1
        if kind == "close":
            pnl = float(row.get("pnl") or 0)
            c["pnl_cents"] += round(pnl * 100)
            if pnl > 0:
                c["wins"] += 1
            elif pnl < 0:
                c["losses"] += 1
        elif kind == "gap":
            c["gap_s"] += round(float(row.get("seconds") or 0))
        elif kind == "skip":
            c["skip:" + str(row.get("reason"))] += 1
        if kind in ("buy", "sell") and row.get("fee"):
            c["fee_microusd"] += round(float(row["fee"]) * 1e6)
        self._track(kind, row, ts, day)
        if len(self.counts) > 3:
            for old in sorted(self.counts)[:-3]:
                del self.counts[old]
        if kind in TRADE_TYPES:
            self.trades.append(row)
        elif kind == "signal":
            self.signals.append(row)

    def _track(self, kind: str, row: dict, ts: float, day: str) -> None:
        """Uptime markers, today's funnel and what became of each signal."""
        if kind in ("start", "stop"):
            self._marks.append((ts, kind, self._last_loop_ts))
        elif kind == "gap":
            secs = float(row.get("seconds") or 0)
            frm = row.get("from")
            self._marks.append((ts, "gap", (float(frm) if frm else ts - secs, ts, bool(row.get("slept")))))
        if kind in LOOP_TYPES:
            self._last_loop_ts = ts
        while self._marks and self._marks[0][0] < ts - 2 * 86400:
            self._marks.popleft()
        token = row.get("token_id")
        if not token or kind not in ("signal", "skip", "opportunity", "buy", "sell"):
            return
        f = self.funnel.setdefault(day, {"signal": set(), "opportunity": set(), "buy": set(), "sell": set(),
                                         "skips": {}})
        if len(self.funnel) > 3:
            for old in sorted(self.funnel)[:-3]:
                del self.funnel[old]
        res = self.token_result.setdefault(token, {"bought": False, "skip": None})
        self.token_result.move_to_end(token)
        while len(self.token_result) > 5000:
            self.token_result.popitem(last=False)
        if kind == "skip":
            f["skips"].setdefault(str(row.get("reason")), set()).add(token)
            res["skip"] = row.get("reason")
        elif kind == "buy":
            if float(row.get("shares") or 0) > 0:      # live writes unfilled orders too
                f["buy"].add(token)
                res["bought"] = True
        else:
            f[kind].add(token)

    def uptime_today(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        t0 = max(local_midnight(now), float(self.bank.state.get("started_at") or 0))
        runner, thread = self.runner, self._thread
        alive = bool(thread and thread.is_alive())
        last_step = runner.health.get("last_step") if runner else None
        stalled = last_step if alive and last_step and now - last_step > GAP_SECONDS else None
        return uptime(list(self._marks), t0, now, alive, self._last_loop_ts, stalled)

    def funnel_today(self, day: str | None = None) -> dict:
        f = self.funnel.get(day or time.strftime("%Y-%m-%d", time.localtime()))
        if not f:
            return {"signalen": 0, "met_goedkope_orders": 0, "gekocht": 0, "verkocht": 0, "skips": {}}
        # 'why not bought': buckets that were bought after all don't count (e.g. 'al gekocht').
        # Copies first: the bot thread may add rows while the dashboard reads.
        bought = set(f["buy"])
        skips = {r: len(set(t) - bought) for r, t in dict(f["skips"]).items()}
        skips = {r: n for r, n in skips.items() if n}
        return {"signalen": len(f["signal"]), "met_goedkope_orders": len(f["opportunity"]),
                "gekocht": len(f["buy"]), "verkocht": len(f["sell"]),
                "skips": dict(sorted(skips.items(), key=lambda kv: -kv[1]))}

    def normal_range(self) -> list[float] | None:
        """p10-p90 of the bot's own daily % (only after NORMAL_MIN_DAYS trading days)."""
        pcts = daily_pcts(self.bank.state.get("history") or [], time.strftime("%Y-%m-%d", time.localtime()))
        if len(pcts) < NORMAL_MIN_DAYS:
            return None
        return [round(percentile(pcts, 0.1), 2), round(percentile(pcts, 0.9), 2)]

    # -- HighTempTation panel ------------------------------------------------
    def start_background(self) -> None:
        """Start the thread that keeps the 'HighTempTation vandaag' panel up to date."""
        if self._bg and self._bg.is_alive():
            return
        self._bg_stop.clear()
        self._bg = threading.Thread(target=self._bg_loop, name="weerbot-htt", daemon=True)
        self._bg.start()

    def stop_background(self) -> None:
        self._bg_stop.set()

    def htt_due(self, now: float) -> bool:
        """Refresh every HTT_EVERY_SECONDS, but only while the dashboard was polled in the last 10 minutes."""
        return now - self._last_view <= HTT_IDLE_SECONDS and now - self._htt_at >= HTT_EVERY_SECONDS

    def _bg_loop(self) -> None:
        session = None
        while not self._bg_stop.wait(5):
            now = time.time()
            if not self.htt_due(now):
                continue                  # nobody is looking, or refreshed recently
            session = session or self.htt_session_factory()
            self.refresh_htt(session, now)

    def refresh_htt(self, session, now: float | None = None) -> dict:
        """One data-api call: HighTempTation's buys since local midnight next to what the bot saw."""
        now = time.time() if now is None else now
        self._htt_at = now
        since = local_midnight(now)
        try:
            rows = [r for r in read_journal(str(self.home / self.cfg.journal_dir), since_ts=since)
                    if r.get("type") in ("signal", "skip", "buy")]
            res = compare_today(rows, session, since=since, budget=self.bank.budget(""),
                                fee_rate=self.cfg.fee_rate, sell_price=self.cfg.sell_price)
            res["updated_at"] = now
            self.htt = res
        except Exception as exc:  # noqa: BLE001 - keep the last result, show the error
            msg = short_error(exc) if isinstance(exc, requests.RequestException) else str(exc)[:200]
            log.info("HighTempTation panel: %s", msg)
            old = dict(self.htt or {})
            if old.get("since") and old["since"] != since:
                old = {}                  # yesterday's numbers are not 'vandaag'
            self.htt = {**old, "error": msg, "error_at": now}
        return self.htt

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
            was_alive = bool(thread and thread.is_alive())
            self._stop.set()
        if thread:
            thread.join(timeout=30)
        with self._lock:
            if self.status == "running":
                self.status = "stopped"
            if was_alive:
                # only when a loop was running: 'Vandaag x% actief' counts from start to stop
                # (a loop that crashed has no stop row; it counts until its last row)
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

    def _signal_rows(self, labels: dict[str, str]) -> list[dict]:
        out = []
        for row in list(reversed(list(self.signals)))[:60]:
            res = self.token_result.get(row.get("token_id")) or {}
            row = dict(row)
            if res.get("bought"):
                row["result"], row["result_label"] = "bought", "gekocht"
            elif res.get("skip"):
                row["result"] = res["skip"]
                row["result_label"] = labels.get(res["skip"], res["skip"])
            else:
                row["result"], row["result_label"] = None, None
            out.append(row)
        return out

    def snapshot(self) -> dict:
        self._last_view = time.time()          # someone is looking: keep the HighTempTation panel fresh
        with self._lock:
            runner, status, error = self.runner, self.status, self.error
            cfg = self.cfg
        c = self.counts.get(time.strftime("%Y-%m-%d", time.localtime()), collections.Counter())
        view = runner.view if runner else {"events": [], "positions": []}
        health = dict(runner.health) if runner else {}
        if not health.get("geoblock") and self._geo:
            health["geoblock"] = self._geo
        up = self.uptime_today()
        health["uptime"] = up
        health["uptime_today_pct"] = up["pct"]
        labels = skip_labels(cfg)
        bank = self.bank.snapshot()
        bank.update(fees_today=round(c["fee_microusd"] / 1e6, 4), today_trades=c["close"],
                    today_wins=c["wins"], today_losses=c["losses"])
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
            "bank": bank,
            "history": self.bank.history(),
            "daily": self._daily(),
            "today": {
                "signals": c["signal"], "opportunities": c["opportunity"], "buys": c["buy"],
                "closes": c["close"], "wins": c["wins"], "losses": c["losses"], "pnl": c["pnl_cents"] / 100,
                "fees": round(c["fee_microusd"] / 1e6, 4), "funnel": self.funnel_today(),
                "normal_range": self.normal_range(),
            },
            "skip_labels": labels,
            "htt": self.htt,
            "events": view["events"],
            "positions": positions,
            "loop_alive": bool(thread and thread.is_alive()),
            "halt": self.bank.halt_info(),
            "trades": list(reversed(list(self.trades)))[:150],
            "signals": self._signal_rows(labels),
            "logs": list(reversed(list(self.logs)))[:120],
            "health": health,
            "settings": {"trade_pct": cfg.trade_pct, "market_pct": cfg.market_pct, "open_pct": cfg.open_pct,
                         "daily_loss_pct": cfg.daily_loss_pct, "poll_seconds": cfg.poll_seconds,
                         "event_pct": cfg.event_pct, "max_drawdown_pct": cfg.max_drawdown_pct,
                         "max_trade_usdc": cfg.max_trade_usdc, "max_event_usdc": cfg.max_event_usdc,
                         "max_buy_price": cfg.max_buy_price, "sell_price": cfg.sell_price,
                         "confirm_seconds": cfg.confirm_seconds, "hot_poll_seconds": HOT_POLL_SECONDS,
                         "fresh_seconds": cfg.fresh_seconds},
        }
