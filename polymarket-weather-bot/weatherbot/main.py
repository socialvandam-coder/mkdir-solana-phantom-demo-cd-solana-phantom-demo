"""Run the bot from a terminal (the Mac app uses weatherbot.app instead).

    python -m weatherbot.main            # paper mode (default): simulate, no money
    python -m weatherbot.main --check    # test connections and show what it would watch
    MODE=live python -m weatherbot.main  # real orders (only where Polymarket allows trading)
"""
from __future__ import annotations

import argparse
import logging
import threading
from datetime import datetime, timezone

import requests

from .bankroll import Bankroll
from .config import Config, load_config
from .engine import BotRunner, TradingNotAllowed, make_session
from .geoblock import check_geoblock
from .journal import Journal
from .markets import fetch_events
from .metar import ObservationStore, make_source
from .strategy import find_signals, guaranteed_extreme

log = logging.getLogger("weatherbot")


def run_check(cfg: Config, bankroll: Bankroll | None = None) -> dict:
    """Connection test; returns a dict (printed by the CLI, shown by the dashboard)."""
    session = make_session()
    out: dict = {"geoblock": check_geoblock(session), "markets": None, "stations": [], "weather": None,
                 "events": [], "account": None}
    now = datetime.now(timezone.utc)
    try:
        events = fetch_events(session, now, cfg.cities)
        out["markets"] = {"ok": True, "count": len(events)}
    except requests.RequestException as exc:
        out["markets"] = {"ok": False, "error": str(exc)}
        return out
    stations = sorted({e.station for e in events})
    out["stations"] = stations
    store = ObservationStore()
    try:
        for o in make_source(cfg.metar_source, session).fetch(stations):
            store.add(o)
        out["weather"] = {"ok": True, "source": cfg.metar_source, "reports": sum(len(store.get(s)) for s in stations)}
    except requests.RequestException as exc:
        out["weather"] = {"ok": False, "source": cfg.metar_source, "error": str(exc)}
    for e in sorted(events, key=lambda e: (e.city, e.kind)):
        ext = guaranteed_extreme(e, store.get(e.station))
        sigs = find_signals(e, store.get(e.station), cfg.safety_margin, cfg.enable_yes_bets)
        out["events"].append({"city": e.city, "kind": e.kind, "station": e.station,
                              "local_time": e.local_now(now).strftime("%H:%M"),
                              "observed": ext[0] if ext else None, "unit": e.unit,
                              "dead": len(sigs), "buckets": len(e.markets)})
    if cfg.live:
        try:
            from .execution import LiveExecutor
            ex = LiveExecutor(cfg, session, Journal(cfg.journal_dir), bankroll)
            out["account"] = {"ok": True, "usdc": round(ex.usdc_balance(), 2)}
        except Exception as exc:  # noqa: BLE001 - report any account problem
            out["account"] = {"ok": False, "error": str(exc)}
    return out


def print_check(res: dict) -> int:
    print(f"geoblock: {res['geoblock']}")
    m = res["markets"]
    print(f"markets: {m}")
    if not m or not m.get("ok"):
        return 1
    print(f"stations ({len(res['stations'])}): {', '.join(res['stations'])}")
    print(f"weather: {res['weather']}")
    for e in res["events"][:80]:
        obs = f"observed {e['observed']}°{e['unit']}" if e["observed"] is not None else "no obs yet"
        print(f"  {e['city']:16s} {e['kind']:4s} {e['station']}  local {e['local_time']}  {obs:18s} "
              f"dead/certain buckets: {e['dead']}/{e['buckets']}")
    if res["account"]:
        print(f"live account: {res['account']}")
    ok = res["weather"] and res["weather"].get("ok") and (not res["account"] or res["account"].get("ok"))
    return 0 if ok else 1


def run(cfg: Config) -> None:
    journal = Journal(cfg.journal_dir)
    bank = Bankroll(cfg, cfg.state_file)
    try:
        runner = BotRunner(cfg, journal, bank)
    except TradingNotAllowed as exc:
        raise SystemExit(f"{exc} Zet MODE=paper.") from None
    log.info("%s mode - balance %.2f (start %.2f)", cfg.mode.upper(), bank.balance, bank.start_amount)
    journal.write("start", mode=cfg.mode)
    stop = threading.Event()
    try:
        runner.run(stop)
    except KeyboardInterrupt:
        stop.set()
    except TradingNotAllowed as exc:
        raise SystemExit(str(exc)) from None
    finally:
        journal.write("stop")


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
        raise SystemExit(print_check(run_check(cfg)))
    run(cfg)


if __name__ == "__main__":
    main()
