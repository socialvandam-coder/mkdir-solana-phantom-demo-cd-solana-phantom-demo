"""How closely does the bot copy HighTempTation? Matches his real buys with our signals."""
from __future__ import annotations

import collections
import re
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


# --- 'HighTempTation vandaag': his buys since local midnight next to what the bot saw ----------
_Q_RE = re.compile(r"^Will the (highest|lowest) temperature in (.+?) be (.+?) on ", re.I)
KIND_NL = {"highest": "max", "lowest": "min"}


def exact_fee_per_share(price: float, fee_rate: float = 0.05) -> float:
    """Polymarket's taker fee on these markets: fee_rate * p * (1 - p) per share
    (checked against his fills: usdcSize = price*size + this)."""
    return fee_rate * price * (1.0 - price)


def short_title(title: str, outcome: str | None) -> str:
    """'Will the highest temperature in Madrid be 21°C on October 10?' -> 'Madrid max 21°C · NO'."""
    m = _Q_RE.match(title or "")
    side = (outcome or "").upper()
    if not m:
        return f"{title} · {side}" if side else (title or "?")
    return f"{m.group(2)} {KIND_NL[m.group(1).lower()]} {m.group(3)}" + (f" · {side}" if side else "")


def compare_today(rows: list[dict], session: requests.Session, since: float, budget: float | None = None,
                  fee_rate: float = 0.05, sell_price: float = 0.99) -> dict:
    """One data-api call: every bucket HighTempTation bought since `since`, and what the bot did with it.

    rows = the bot's journal rows (signal / skip / buy) since `since`.
    status per bucket: 'gekocht' (the bot bought it too), 'gezien_te_laat' (seen, cheap asks gone),
    'gezien_overgeslagen' (seen, skipped for another reason), 'niet_gezien' (no signal at all:
    check the station, rounding or time zone - these are the bug list).
    delta_s = bot signal time - his first buy (negative = the bot was first).
    missed_est = what the not-bought buckets would have earned at his price with the bot's budget
    (an upper bound: when he was first, the cheap asks were usually gone)."""
    r = session.get(DATA_API, params={"user": WALLET, "type": "TRADE", "side": "BUY", "start": int(since),
                                      "limit": 500}, timeout=20)
    r.raise_for_status()
    buys = [t for t in (r.json() or []) if t.get("type") == "TRADE" and t.get("side") == "BUY"
            and t.get("asset")]
    signal_ts: dict[str, float] = {}
    skips: dict[str, list[dict]] = collections.defaultdict(list)
    bought: set[str] = set()
    for row in rows:
        tok, kind = row.get("token_id"), row.get("type")
        if not tok:
            continue
        if kind == "signal":
            signal_ts.setdefault(tok, float(row["ts"]))
        elif kind == "skip":
            skips[tok].append(row)
        elif kind == "buy" and float(row.get("shares") or 0) > 0:
            bought.add(tok)

    his: dict[str, dict] = {}
    for t in sorted(buys, key=lambda t: t.get("timestamp") or 0):
        a = his.setdefault(t["asset"], {"title": t.get("title"), "outcome": t.get("outcome"),
                                        "his_ts": float(t.get("timestamp") or 0), "shares": 0.0, "value": 0.0,
                                        "usdc": 0.0})
        size, price = float(t.get("size") or 0), float(t.get("price") or 0)
        a["shares"] += size
        a["value"] += size * price
        a["usdc"] += float(t.get("usdcSize") or size * price)

    out_rows, deltas, missed = [], [], 0.0
    sell_fee = exact_fee_per_share(sell_price, fee_rate)
    for asset, a in his.items():
        price = a["value"] / a["shares"] if a["shares"] else 0.0
        sig = signal_ts.get(asset)
        reason = None
        if asset in bought:
            status = "gekocht"
        elif sig is None:
            status = "niet_gezien"
        else:
            reasons = [s.get("reason") for s in skips.get(asset, [])]
            if "late_no_cheap_asks" in reasons:
                status, reason = "gezien_te_laat", "late_no_cheap_asks"
            else:
                status, reason = "gezien_overgeslagen", (reasons[0] if reasons else None)
        delta = None if sig is None else round(sig - a["his_ts"], 1)
        if delta is not None:
            deltas.append(delta)
        if status != "gekocht" and 0 < price < 1:
            own = [s.get("budget") for s in skips.get(asset, []) if isinstance(s.get("budget"), (int, float))]
            cap = own[0] if own else budget
            stake = a["usdc"] if cap is None else min(cap, a["usdc"])
            edge = sell_price - price - exact_fee_per_share(price, fee_rate) - sell_fee
            missed += max(0.0, stake * edge / price)
        out_rows.append({"asset": asset, "title": a["title"], "label": short_title(a["title"], a["outcome"]),
                         "his_ts": a["his_ts"], "his_price": round(price, 4), "his_usdc": round(a["usdc"], 2),
                         "status": status, "reason": reason, "delta_s": delta})
    out_rows.sort(key=lambda x: x["his_ts"], reverse=True)
    seen = [x for x in out_rows if x["status"] != "niet_gezien"]
    return {
        "ok": True, "since": since,
        "his_n": len(out_rows),
        "his_usdc": round(sum(x["his_usdc"] for x in out_rows), 2),
        "seen_n": len(seen),
        "bought_n": sum(1 for x in out_rows if x["status"] == "gekocht"),
        "first_n": sum(1 for d in deltas if d < 0),
        "late_n": sum(1 for x in out_rows if x["status"] == "gezien_te_laat"),
        "missed_n": len(out_rows) - len(seen),
        "median_delta_s": round(statistics.median(deltas)) if deltas else None,
        "missed_est": round(missed, 2),
        "rows": out_rows[:10],
    }
