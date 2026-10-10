"""Summarise the bot's journal: signals, opportunities, buys, sells and profit.

    python tools/report.py [logs]
"""
from __future__ import annotations

import collections
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weatherbot.journal import read_journal  # noqa: E402


def main() -> None:
    rows = read_journal(sys.argv[1] if len(sys.argv) > 1 else "logs")
    if not rows:
        print("journal is empty - run the bot first")
        return
    by = collections.defaultdict(list)
    for r in rows:
        by[r["type"]].append(r)

    print(f"period: {rows[0]['time']} .. {rows[-1]['time']}")
    sig = by["signal"]
    print(f"signals (buckets that became dead/certain): {len(sig)}")
    if sig:
        lags = [s["lag_s"] for s in sig if s.get("lag_s") is not None and s["lag_s"] < 3 * 3600]
        if lags:
            print(f"  delay observation -> signal: median {statistics.median(lags):.0f}s")
    opp = by["opportunity"]
    print(f"opportunities (cheap asks found): {len(opp)}  est. profit {sum(o['est_profit'] for o in opp):.2f} USDC")
    buys = by["buy"]
    print(f"buys: {len(buys)}  spent {sum(float(b.get('cost') or 0) for b in buys):.2f} USDC")
    closes = by["close"]
    pnl = sum(c["pnl"] for c in closes)
    wins = sum(1 for c in closes if c["pnl"] > 0)
    print(f"closed: {len(closes)}  won {wins}  pnl {pnl:+.2f} USDC")
    hows = collections.Counter(c["how"] for c in closes)
    print(f"  how: {dict(hows)}")
    per_city = collections.defaultdict(float)
    for c in closes:
        per_city[c["city"]] += c["pnl"]
    for city, v in sorted(per_city.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {city:16s} {v:+.2f}")
    if by["error"] or by["buy_error"]:
        print(f"errors: {len(by['error'])}  buy errors: {len(by['buy_error'])}")


if __name__ == "__main__":
    main()
