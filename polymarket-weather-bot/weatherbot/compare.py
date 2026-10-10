"""How closely does the bot copy HighTempTation? Matches his real buys with our signals."""
from __future__ import annotations

import collections
import statistics

import requests

WALLET = "0x6011655c4afb76f36dd1b08a137a1ba73466b31e"  # HighTempTation
DATA_API = "https://data-api.polymarket.com/activity"


def his_trades(session: requests.Session, start: float, end: float) -> list[dict]:
    out, offset = [], 0
    while offset < 3000:
        r = session.get(DATA_API, params={"user": WALLET, "limit": 500, "offset": offset, "type": "TRADE",
                                          "start": int(start), "end": int(end)}, timeout=30)
        r.raise_for_status()
        page = r.json()
        out.extend(page)
        if len(page) < 500:
            break
        offset += 500
    return [t for t in out if t.get("type") == "TRADE"]


def compare(rows: list[dict], session: requests.Session, start: float | None = None) -> dict:
    """rows = the bot's journal. Returns a summary dict (also used by the dashboard)."""
    starts = [r["ts"] for r in rows if r.get("type") == "start"]
    if not rows or not starts:
        return {"ok": False, "message": "Nog geen gegevens: laat de bot eerst draaien."}
    start = start if start is not None else min(starts)
    end = rows[-1]["ts"]
    signals = {r["token_id"]: r for r in rows if r.get("type") == "signal"}
    opps = collections.defaultdict(list)
    buys_bot = collections.defaultdict(list)
    for r in rows:
        if r.get("type") == "opportunity":
            opps[r["token_id"]].append(r)
        elif r.get("type") == "buy":
            buys_bot[r["token_id"]].append(r)

    his = [t for t in his_trades(session, start, end) if t.get("side") == "BUY"]
    seen: dict[str, dict] = {}
    for t in sorted(his, key=lambda t: t["timestamp"]):
        seen.setdefault(t["asset"], t)
    matched, deltas, with_opp, bought_too, missed = 0, [], 0, 0, []
    his_usdc = collections.defaultdict(float)
    for t in his:
        his_usdc[t["asset"]] += float(t.get("usdcSize") or 0)
    for asset, t in seen.items():
        s = signals.get(asset)
        if not s:
            missed.append({"title": t.get("title"), "outcome": t.get("outcome"), "price": t.get("price"),
                           "ts": t["timestamp"]})
            continue
        matched += 1
        deltas.append(s["ts"] - t["timestamp"])
        with_opp += bool(opps.get(asset))
        bought_too += bool(buys_bot.get(asset))
    total = len(seen)
    early = sum(1 for d in deltas if d <= 0)
    return {
        "ok": True,
        "period_start": start, "period_end": end,
        "his_buckets": total,
        "his_usdc": round(sum(his_usdc.values()), 2),
        "matched": matched,
        "match_pct": round(matched / total * 100, 1) if total else None,
        "bot_saw_cheap_asks": with_opp,
        "bot_bought_too": bought_too,
        "bot_first": early,
        "bot_later": len(deltas) - early,
        "median_delta_s": round(statistics.median(deltas)) if deltas else None,
        "missed": missed[:25],
    }
