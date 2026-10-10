"""Run the bot.

    python -m weatherbot.main            # paper mode (default): simulate, no money
    python -m weatherbot.main --check    # test connections and show what it would watch
    MODE=live python -m weatherbot.main  # real orders (only where Polymarket allows trading)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

import requests

from .config import load_config
from .execution import LiveExecutor, PaperExecutor
from .geoblock import check_geoblock
from .journal import Journal
from .markets import fetch_events
from .metar import ObservationStore, make_source
from .strategy import find_signals, guaranteed_extreme

log = logging.getLogger("weatherbot")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "weatherbot/1.0 (+https://github.com/)"
    return s


def _require_trading_allowed(session: requests.Session) -> None:
    geo = check_geoblock(session)
    if geo.get("blocked"):
        raise SystemExit(
            f"Polymarket does not allow trading from this connection ({geo}). "
            "Live mode is off; run in paper mode (MODE=paper) instead.")


def check(cfg) -> int:
    session = _session()
    ok = True
    geo = check_geoblock(session)
    print(f"geoblock: {geo}")
    now = datetime.now(timezone.utc)
    try:
        events = fetch_events(session, now, cfg.cities)
        print(f"gamma: {len(events)} temperature events live right now")
    except requests.RequestException as exc:
        print(f"gamma: FAILED {exc}")
        return 1
    stations = sorted({e.station for e in events})
    print(f"stations ({len(stations)}): {', '.join(stations)}")
    store = ObservationStore()
    try:
        for o in make_source(cfg.metar_source, session).fetch(stations):
            store.add(o)
        print(f"metar ({cfg.metar_source}): OK")
    except requests.RequestException as exc:
        print(f"metar ({cfg.metar_source}): FAILED {exc}")
        ok = False
    for e in sorted(events, key=lambda e: (e.city, e.kind))[:60]:
        ext = guaranteed_extreme(e, store.get(e.station))
        sigs = find_signals(e, store.get(e.station), cfg.safety_margin, cfg.enable_yes_bets)
        local = e.local_now(now).strftime("%H:%M")
        print(f"  {e.city:16s} {e.kind:4s} {e.station}  local {local}  "
              f"{'observed ' + str(ext[0]) + '°' + e.unit if ext else 'no obs yet':18s} "
              f"dead/certain buckets: {len(sigs)}/{len(e.markets)}")
    if cfg.live:
        try:
            ex = LiveExecutor(cfg, session, Journal(cfg.journal_dir))
            print(f"live account: USDC balance {ex.usdc_balance():.2f}")
        except SystemExit as exc:
            print(f"live account: {exc}")
            ok = False
    return 0 if ok else 1


def run(cfg) -> None:
    session = _session()
    journal = Journal(cfg.journal_dir)
    if cfg.live:
        _require_trading_allowed(session)
        executor = LiveExecutor(cfg, session, journal)
        log.info("LIVE mode - USDC balance %.2f", executor.usdc_balance())
    else:
        executor = PaperExecutor(cfg, session, journal)
        log.info("PAPER mode - no real orders")
    journal.write("start", mode=cfg.mode, config={k: v for k, v in vars(cfg).items() if k != "private_key"})

    source = make_source(cfg.metar_source, session)
    store = ObservationStore()
    events = []
    last_refresh = last_geo = 0.0
    while True:
        loop_start = time.time()
        now_dt = datetime.now(timezone.utc)
        try:
            if loop_start - last_refresh >= cfg.market_refresh_seconds:
                events = fetch_events(session, now_dt, cfg.cities)
                last_refresh = loop_start
                log.info("watching %d events on %d stations", len(events), len({e.station for e in events}))
            if cfg.live and loop_start - last_geo >= 3600:
                _require_trading_allowed(session)
                last_geo = loop_start

            live_events = [e for e in events if e.is_live(now_dt)]
            stations = sorted({e.station for e in live_events})
            new = 0
            for obs in source.fetch(stations):
                if store.add(obs):
                    new += 1
            if new:
                log.debug("%d new reports", new)
            store.prune(now_dt)

            signals = []
            for e in live_events:
                signals.extend(find_signals(e, store.get(e.station), cfg.safety_margin, cfg.enable_yes_bets))
            executor.on_signals(signals, time.time())
            executor.tick(time.time())
        except requests.RequestException as exc:
            log.warning("network error: %s", exc)
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive, but record what happened
            log.exception("unexpected error")
            journal.write("error", error=repr(sys.exc_info()[1]))
        time.sleep(max(0.5, cfg.poll_seconds - (time.time() - loop_start)))


def main() -> None:
    ap = argparse.ArgumentParser(description="HighTempTation-style Polymarket weather bot")
    ap.add_argument("--check", action="store_true", help="test connections and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = load_config()
    if args.check:
        raise SystemExit(check(cfg))
    run(cfg)


if __name__ == "__main__":
    main()
