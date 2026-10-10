"""Check how closely the bot copies HighTempTation.

Fetches his real trades for the period the bot was running and matches them with the
bot's signals: did we flag the same bucket, and were we earlier or later than him?

    python tools/compare_hightemptation.py [logs]
"""
from __future__ import annotations

import collections
import statistics
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weatherbot.journal import read_journal  # noqa: E402

WALLET = "0x6011655c4afb76f36dd1b08a137a1ba73466b31e"  # HighTempTation
DATA_API = "https://data-api.polymarket.com/activity"


def his_trades(start: float, end: float) -> list[dict]:
    s = requests.Session()
    s.headers["User-Agent"] = "weatherbot/1.0"
    out, offset = [], 0
    while offset < 3000:
        r = s.get(DATA_API, params={"user": WALLET, "limit": 500, "offset": offset, "type": "TRADE",
                                    "start": int(start), "end": int(end)}, timeout=30)
        r.raise_for_status()
        page = r.json()
        out.extend(page)
        if len(page) < 500:
            break
        offset += 500
    return [t for t in out if t.get("type") == "TRADE"]


def main() -> None:
    rows = read_journal(sys.argv[1] if len(sys.argv) > 1 else "logs")
    starts = [r["ts"] for r in rows if r["type"] == "start"]
    if not rows or not starts:
        print("journal is empty - run the bot first")
        return
    start, end = min(starts), rows[-1]["ts"]
    signals = {r["token_id"]: r for r in rows if r["type"] == "signal"}
    opps = collections.defaultdict(list)
    for r in rows:
        if r["type"] == "opportunity":
            opps[r["token_id"]].append(r)

    buys = [t for t in his_trades(start, end) if t["side"] == "BUY"]
    print(f"HighTempTation buys while the bot ran: {len(buys)}")
    if not buys:
        return
    matched, missed, deltas, with_opp = 0, [], [], 0
    seen = set()
    for t in sorted(buys, key=lambda t: t["timestamp"]):
        if t["asset"] in seen:
            continue
        seen.add(t["asset"])
        s = signals.get(t["asset"])
        if not s:
            missed.append(t)
            continue
        matched += 1
        deltas.append(s["ts"] - t["timestamp"])
        if opps.get(t["asset"]):
            with_opp += 1
    total = len(seen)
    print(f"buckets he bought: {total}")
    print(f"  also flagged by the bot: {matched} ({matched / total:.0%})")
    print(f"  bot also saw cheap asks there: {with_opp}")
    if deltas:
        early = sum(1 for d in deltas if d <= 0)
        print(f"  bot earlier than him: {early}x, later: {len(deltas) - early}x, "
              f"median difference {statistics.median(deltas):+.0f}s (negative = bot first)")
    if missed:
        print("missed (first 20) - check station/rounding/timezone for these:")
        for t in missed[:20]:
            print(f"   {t['title']}  {t['outcome']} @ {t['price']:.3f}")


if __name__ == "__main__":
    main()
