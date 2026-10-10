"""Backtest: would the bot have made the same trades as HighTempTation?

For every buy HighTempTation made in a period, this fetches the real airport reports
(METAR/SPECI) of that day from the Iowa Environmental Mesonet archive and replays them
through the bot's own strategy code:

  covered    the bot would have flagged that bucket before (or at) his trade
  later      the bot would flag it, but only from a report time AFTER his trade
             (he saw something earlier - a faster/other data source)
  never      no report that day rules the bucket out (a forecast-style bet, or data mismatch)
  no_data    no archive data / station unknown / event not parseable

It also runs a safety check over every bucket of those events: a bucket the bot would
call dead (NO) that actually WON, or a YES it calls certain that LOST, would be a real
loss - those must be zero.

    python tools/backtest_hightemptation.py --days 14 [--end 2026-10-09] [--hf] [--out report.json]

Needs internet access to data-api.polymarket.com, gamma-api.polymarket.com and
mesonet.agron.iastate.edu (runs fine on GitHub Actions).
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weatherbot.markets import GAMMA, TempEvent, parse_event  # noqa: E402
from weatherbot.metar import Observation, displayed_bounds, parse_metar  # noqa: E402
from weatherbot.strategy import day_observations, find_signals, guaranteed_extreme  # noqa: E402

WALLET = "0x6011655c4afb76f36dd1b08a137a1ba73466b31e"
DATA_API = "https://data-api.polymarket.com/activity"
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "weerbot-backtest/1.0 (research; low volume)"


def get(url: str, params, tries: int = 4, timeout: int = 60) -> requests.Response:
    """GET with retries; params may be a dict or a list of (key, value) pairs."""
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"status {r.status_code}")
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            if i == tries - 1:
                raise
            wait = 3 * (i + 1)
            print(f"  retry {url.split('/')[2]} in {wait}s ({exc})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


# ------------------------------------------------------------------ his trades ---
def his_buys(start: float, end: float) -> list[dict]:
    out = []
    t = start
    while t < end:
        t2 = min(end, t + 86400)
        offset = 0
        while offset <= 2500:
            page = get(DATA_API, {"user": WALLET, "type": "TRADE", "start": int(t), "end": int(t2),
                                  "limit": 500, "offset": offset}).json()
            out.extend(page)
            if len(page) < 500:
                break
            offset += 500
        t = t2
    seen, buys = set(), []
    for x in out:
        key = (x.get("transactionHash"), x.get("asset"), x.get("side"), x.get("size"))
        if key in seen:
            continue
        seen.add(key)
        if x.get("type") == "TRADE" and x.get("side") == "BUY" and "temperature" in (x.get("title") or "").lower():
            buys.append(x)
    return sorted(buys, key=lambda x: x["timestamp"])


# ---------------------------------------------------------------- events ---
_event_cache: dict[str, tuple[TempEvent | None, dict]] = {}


def load_event(slug: str) -> tuple[TempEvent | None, dict]:
    """Returns (parsed event incl. resolved buckets, {condition_id: 'YES'|'NO'|None})."""
    if slug in _event_cache:
        return _event_cache[slug]
    data = get(f"{GAMMA}/events", {"slug": slug}).json()
    if not data:
        _event_cache[slug] = (None, {})
        return _event_cache[slug]
    return cache_event(data[0])


def cache_event(ev: dict) -> tuple[TempEvent | None, dict]:
    slug = ev.get("slug", "")
    te = parse_event(ev, include_closed=True)
    winners = {}
    for mk in ev.get("markets") or []:
        prices = mk.get("outcomePrices")
        prices = json.loads(prices) if isinstance(prices, str) else prices
        res = None
        if mk.get("closed") and prices:
            yes = float(prices[0])
            res = "YES" if yes >= 0.99 else "NO" if yes <= 0.01 else None
        winners[mk.get("conditionId")] = res
    if te:
        for m in te.markets:
            m.accepting_orders = True     # closed now, but it was open when he traded
    _event_cache[slug] = (te, winners)
    return _event_cache[slug]


# ------------------------------------------------------------------ METAR ---
_obs_cache: dict[tuple[str, date, date, bool], list[Observation]] = {}


def iem_ids(icao: str) -> list[str]:
    ids = [icao]
    if len(icao) == 4 and icao[0] == "K":
        ids.insert(0, icao[1:])          # IEM uses LGA for KLGA
    return ids


SPECI_TIMES: dict[str, set[datetime]] = collections.defaultdict(set)   # station -> times of SPECI reports


def _iem(sid: str, d1: date, d2: date, report_types: tuple[int, ...]) -> str:
    params = [("station", sid), ("data", "metar"), ("tz", "Etc/UTC"), ("format", "onlycomma"),
              ("latlon", "no"), ("elev", "no"), ("missing", "M"), ("trace", "T"), ("direct", "no"),
              ("year1", d1.year), ("month1", d1.month), ("day1", d1.day), ("hour1", 0), ("minute1", 0),
              ("year2", d2.year), ("month2", d2.month), ("day2", d2.day), ("hour2", 23), ("minute2", 59)]
    params += [("report_type", t) for t in report_types]
    return get(IEM, params, timeout=120).text


PERIOD: list[date] = []   # [first, last] UTC day of the whole backtest; set in main()


def fetch_obs(icao: str, d1: date, d2: date, hf: bool) -> list[Observation]:
    """All METAR/SPECI (plus US 5-minute reports if hf) for a station.

    Fetches the whole backtest period once per station (much faster than per day)."""
    if PERIOD:
        d1, d2 = PERIOD
    key = (icao, d1, d2, hf)
    if key in _obs_cache:
        return _obs_cache[key]
    obs: list[Observation] = []
    for sid in iem_ids(icao):
        routine = parse_iem_csv(_iem(sid, d1, d2, (3,)), icao)
        if not routine:
            time.sleep(1)
            continue
        specials = parse_iem_csv(_iem(sid, d1, d2, (4,)), icao)
        SPECI_TIMES[icao].update(o.time for o in specials)
        extra = parse_iem_csv(_iem(sid, d1, d2, (1,)), icao) if hf else []
        merged = {o.time: o for o in extra}
        merged.update({o.time: o for o in specials})
        merged.update({o.time: o for o in routine})
        obs = sorted(merged.values(), key=lambda o: o.time)
        break
    _obs_cache[key] = obs
    return obs


def list_all_events(d1: date, d2: date) -> list[str]:
    """Slugs of every daily-temperature event whose day lies in [d1, d2] (also caches them).

    Asks one day of end dates at a time: gamma refuses offsets beyond ~2000."""
    slugs: list[str] = []
    day = d1 - timedelta(days=1)
    while day <= d2 + timedelta(days=2):
        lo = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        hi = lo + timedelta(days=1)
        offset = 0
        while offset < 2000:
            page = get(f"{GAMMA}/events", {"tag_id": 103040, "closed": "true", "limit": 100, "offset": offset,
                                           "end_date_min": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                           "end_date_max": hi.strftime("%Y-%m-%dT%H:%M:%SZ")}).json()
            for e in page:
                if e.get("slug"):
                    slugs.append(e["slug"])
                    if e["slug"] not in _event_cache:
                        cache_event(e)
            if len(page) < 100:
                break
            offset += 100
        day += timedelta(days=1)
    return sorted(set(slugs))


def parse_iem_csv(text: str, icao: str) -> list[Observation]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        raw, valid = (row.get("metar") or "").strip(), (row.get("valid") or "").strip()
        if not raw or raw == "M" or not valid:
            continue
        try:
            t = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        o = parse_metar(raw, obs_time=t)
        if o:
            # IEM's id can differ from the ICAO (LGA vs KLGA); the bot matches on the ICAO
            out.append(Observation(icao, o.time, o.temp_c, o.temp_c_tenths, o.raw))
    return sorted(out, key=lambda o: o.time)


# --------------------------------------------------------------- variants ---
VARIANTS = {
    # name: (routine reports only, reports needed for the bucket right next to the reading, skip NO on lowest bracket of highs)
    "now (METAR+SPECI)":            (False, 1, False),
    "no SPECI":                      (True, 1, False),
    "no SPECI, skip lowest":         (True, 1, True),
    "no SPECI, adjacent needs 2":    (True, 2, False),
    "no SPECI, adj 2, skip lowest":  (True, 2, True),
    "METAR+SPECI, adj 2, skip low":  (False, 2, True),
}


def variant_signals(ev: TempEvent, obs: list[Observation], variant: tuple) -> list:
    routine_only, confirm, skip_lowest = variant
    if routine_only:
        obs = [o for o in obs if o.time not in SPECI_TIMES[ev.station]]
    sigs = find_signals(ev, obs)
    if confirm <= 1 and not skip_lowest:
        return sigs
    day = day_observations(ev, obs)
    ext = guaranteed_extreme(ev, obs)
    if not ext:
        return sigs
    g = ext[0]
    out = []
    for s in sigs:
        b = s.market.bucket
        if skip_lowest and ev.kind == "high" and s.outcome == "NO" and b.open_low:
            continue
        if confirm > 1:
            if ev.kind == "high":
                edge = b.hi if s.outcome == "NO" else b.lo - 1   # value the reading must exceed
                adjacent = (s.outcome == "NO" and b.hi == g - 1) or (s.outcome == "YES" and b.lo == g)
                n = sum(1 for o in day if displayed_bounds(o, ev.unit)[0] > edge)
            else:
                edge = b.lo if s.outcome == "NO" else b.hi + 1
                adjacent = (s.outcome == "NO" and b.lo == g + 1) or (s.outcome == "YES" and b.hi == g)
                n = sum(1 for o in day if displayed_bounds(o, ev.unit)[1] < edge)
            if adjacent and n < confirm:
                continue
        out.append(s)
    return out


# ----------------------------------------------------------------- replay ---
def first_signal_time(ev: TempEvent, obs: list[Observation], token: str) -> datetime | None:
    seen: list[Observation] = []
    for o in day_observations(ev, obs):
        seen.append(o)
        if any(s.token_id == token for s in find_signals(ev, seen)):
            return o.time
    return None


def pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--end", help="last UTC date (default: yesterday)")
    ap.add_argument("--hf", action="store_true", help="also use US 5-minute (HFMETAR) reports")
    ap.add_argument("--all-events", action="store_true",
                    help="run the safety check on every temperature event in the period, not only his")
    ap.add_argument("--out", default="backtest-report.json")
    ap.add_argument("--misses", default="backtest-misses.csv")
    args = ap.parse_args()

    end_day = date.fromisoformat(args.end) if args.end else (datetime.now(timezone.utc).date() - timedelta(days=1))
    start_day = end_day - timedelta(days=args.days - 1)
    start_ts = datetime(start_day.year, start_day.month, start_day.day, tzinfo=timezone.utc).timestamp()
    end_ts = datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone.utc).timestamp() + 86400
    print(f"Backtest {start_day} .. {end_day} (UTC){'  +HF 5-min reports' if args.hf else ''}")
    PERIOD[:] = [start_day - timedelta(days=1), end_day + timedelta(days=1)]
    all_slugs = list_all_events(start_day, end_day) if args.all_events else []
    if all_slugs:
        print(f"temperature events in the period: {len(all_slugs)}")

    buys = his_buys(start_ts, end_ts)
    print(f"HighTempTation buys on temperature markets: {len(buys)}")

    # group buys per asset (he sometimes bought the same bucket 2-3 times; judge the first buy)
    first_buy: dict[str, dict] = {}
    usdc_per_asset = collections.defaultdict(float)
    for b in buys:
        first_buy.setdefault(b["asset"], b)
        usdc_per_asset[b["asset"]] += float(b.get("usdcSize") or 0)

    results = []
    events_seen: dict[str, TempEvent] = {}
    winners_all: dict[str, dict] = {}
    for i, (asset, b) in enumerate(first_buy.items(), 1):
        if i % 100 == 0:
            print(f"  {i}/{len(first_buy)} buckets...")
        res = {"title": b.get("title"), "outcome": b.get("outcome"), "price": b.get("price"),
               "usdc": round(usdc_per_asset[asset], 2), "trade_ts": b["timestamp"], "event": b.get("eventSlug"),
               "asset": asset}
        try:
            ev, winners = load_event(b.get("eventSlug") or "")
        except requests.RequestException as exc:
            res.update(category="no_data", why=f"gamma: {exc}")
            results.append(res)
            continue
        if ev is None:
            res.update(category="no_data", why="event not parseable (e.g. Hong Kong / unknown city)")
            results.append(res)
            continue
        events_seen[ev.slug] = ev
        winners_all[ev.slug] = winners
        market = next((m for m in ev.markets if asset in (m.yes_token, m.no_token)), None)
        if market is None:
            res.update(category="no_data", why="token not found in event")
            results.append(res)
            continue
        tz = ZoneInfo(ev.tz)
        try:
            obs = fetch_obs(ev.station, ev.day - timedelta(days=1), ev.day + timedelta(days=1), args.hf)
        except requests.RequestException as exc:
            res.update(category="no_data", why=f"IEM: {exc}")
            results.append(res)
            continue
        trade_dt = datetime.fromtimestamp(b["timestamp"], timezone.utc)
        res.update(city=ev.city, kind=ev.kind, station=ev.station, unit=ev.unit, bucket=market.bucket.label,
                   local_trade_time=trade_dt.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                   winner=winners.get(market.condition_id))
        day_obs = day_observations(ev, obs)
        if not day_obs:
            res.update(category="no_data", why="no METAR for that station/day in the archive")
            results.append(res)
            continue
        ext_at_trade = guaranteed_extreme(ev, [o for o in obs if o.time <= trade_dt])
        res["observed_at_trade"] = ext_at_trade[0] if ext_at_trade else None
        t_dead = first_signal_time(ev, obs, asset)
        if t_dead is None:
            ext_day = guaranteed_extreme(ev, obs)
            res.update(category="never", observed_end_of_day=ext_day[0] if ext_day else None)
        elif t_dead <= trade_dt:
            res.update(category="covered", lag_s=round((trade_dt - t_dead).total_seconds()))
        else:
            res.update(category="later", early_s=round((t_dead - trade_dt).total_seconds()))
        results.append(res)

    # -------- safety: buckets the bot would call dead/certain that resolved the other way
    if all_slugs:
        print(f"safety check over all {len(all_slugs)} temperature events in the period...")
        for i, slug in enumerate(all_slugs, 1):
            if slug in events_seen:
                continue
            if i % 200 == 0:
                print(f"  {i}/{len(all_slugs)} events...")
            try:
                ev, winners = load_event(slug)
            except requests.RequestException:
                continue
            if ev is not None and start_day <= ev.day <= end_day:
                events_seen[slug] = ev
                winners_all[slug] = winners
    his_tokens = {r["asset"]: r for r in results if r["category"] in ("covered", "later", "never")}
    vstats = {name: collections.Counter() for name in VARIANTS}
    false_signals = []
    checked_buckets = 0
    speci_dependent = speci_dependent_wrong = 0
    skipped_events = 0
    for slug, ev in events_seen.items():
        winners = winners_all.get(slug, {})
        try:
            obs = fetch_obs(ev.station, ev.day - timedelta(days=1), ev.day + timedelta(days=1), args.hf)
        except requests.RequestException:
            skipped_events += 1
            continue
        for name, v in VARIANTS.items():
            st = vstats[name]
            vs = variant_signals(ev, obs, v)
            vtok = {x.token_id for x in vs}
            for x in vs:
                w = winners.get(x.market.condition_id)
                if w is None:
                    continue
                st["signals"] += 1
                wrong = (x.outcome == "NO" and w == "YES") or (x.outcome == "YES" and w == "NO")
                st["wrong"] += wrong
                if x.token_id in his_tokens:
                    st["wrong_his"] += wrong
            for tok, r in his_tokens.items():
                if r.get("event") == slug and tok in vtok:
                    st["his_kept"] += 1
                    st["his_usdc_kept_c"] += round(r["usdc"] * 100)
        sigs = find_signals(ev, obs)
        routine_only = [o for o in obs if o.time not in SPECI_TIMES[ev.station]]
        routine_tokens = {s.token_id for s in find_signals(ev, routine_only)}
        for s in sigs:
            w = winners.get(s.market.condition_id)
            if w is None:
                continue
            checked_buckets += 1
            wrong = (s.outcome == "NO" and w == "YES") or (s.outcome == "YES" and w == "NO")
            if s.token_id not in routine_tokens:
                speci_dependent += 1
                speci_dependent_wrong += wrong
            if wrong:
                false_signals.append({"event": slug, "bucket": s.market.bucket.label, "bot_says": s.outcome,
                                      "resolved": w, "observed": s.observed, "unit": ev.unit,
                                      "station": ev.station, "needs_speci": s.token_id not in routine_tokens})

    # -------- summary
    cats = collections.Counter(r["category"] for r in results)
    usd = collections.defaultdict(float)
    for r in results:
        usd[r["category"]] += r["usdc"]
    n = len(results)
    lags = sorted(r["lag_s"] for r in results if r["category"] == "covered")
    earl = sorted(r["early_s"] for r in results if r["category"] == "later")
    q = lambda xs, p: xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None  # noqa: E731
    summary = {
        "period": [str(start_day), str(end_day)], "hf_reports": args.hf,
        "his_buys": len(buys), "his_buckets": n,
        "categories": {k: {"n": v, "pct": pct(v, n), "usdc": round(usd[k], 2)} for k, v in cats.most_common()},
        "covered_lag_s": {"p10": q(lags, .1), "median": q(lags, .5), "p90": q(lags, .9)} if lags else None,
        "later_by_s": {"p10": q(earl, .1), "median": q(earl, .5), "p90": q(earl, .9)} if earl else None,
        "by_kind": {k: {c: sum(1 for r in results if r.get("kind") == k and r["category"] == c)
                        for c in ("covered", "later", "never", "no_data")} for k in ("high", "low")},
        "safety_events": len(events_seen), "safety_events_skipped": skipped_events,
        "safety_checked_signals": checked_buckets,
        "safety_speci_dependent": speci_dependent, "safety_speci_dependent_wrong": speci_dependent_wrong,
        "safety_false_signals": false_signals,
        "variants": {k: dict(v) for k, v in vstats.items()},
    }
    Path(args.out).write_text(json.dumps({"summary": summary, "results": results}, indent=1, default=str))
    with open(args.misses, "w", newline="") as f:
        cols = ["category", "title", "outcome", "price", "usdc", "local_trade_time", "station", "unit",
                "observed_at_trade", "observed_end_of_day", "early_s", "winner", "why"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            if r["category"] != "covered":
                w.writerow(r)

    print("\n================ BACKTEST SUMMARY ================")
    print(f"period {start_day} .. {end_day}   his buckets: {n}   (buys: {len(buys)})")
    for k, v in summary["categories"].items():
        print(f"  {k:8s} {v['n']:5d}  {v['pct']:>6s}   ${v['usdc']:,.0f}")
    if lags:
        print(f"  he bought {q(lags, .5) / 60:.1f} min (median) after the report that killed the bucket "
              f"(p10 {q(lags, .1) / 60:.1f}, p90 {q(lags, .9) / 60:.1f} min)")
    if earl:
        print(f"  'later' cases: the bot's first report came {q(earl, .5) / 60:.1f} min (median) after his trade")
    print(f"  by kind: {summary['by_kind']}")
    print(f"SAFETY: {len(events_seen)} events, {checked_buckets} bot signals checked against the official "
          f"result, {len(false_signals)} would have LOST  (skipped events: {skipped_events})")
    print(f"  signals that only exist thanks to a SPECI report: {speci_dependent} "
          f"(wrong: {speci_dependent_wrong})")
    for fs in false_signals[:30]:
        print(f"   !! {fs}")
    total_his = len(his_tokens)
    total_usdc = sum(r["usdc"] for r in his_tokens.values())
    print("\nRULE VARIANTS (all events in the period):")
    print(f"  {'variant':30s} {'signals':>8s} {'wrong':>6s} {'wrong%':>7s} {'his buckets kept':>17s} {'his $ kept':>11s}")
    for name, st in vstats.items():
        sig = st["signals"] or 1
        print(f"  {name:30s} {st['signals']:8d} {st['wrong']:6d} {st['wrong'] / sig * 100:6.3f}% "
              f"{st['his_kept']:7d} ({st['his_kept'] / max(1, total_his) * 100:5.1f}%) "
              f"{st['his_usdc_kept_c'] / 100 / max(1, total_usdc) * 100:9.1f}%")
    misses = [r for r in results if r["category"] != "covered"]
    if misses:
        print("\nFirst 40 non-covered buckets:")
        for r in misses[:40]:
            extra = r.get("why") or (f"obs at trade {r.get('observed_at_trade')}, end of day {r.get('observed_end_of_day')}"
                                     if r["category"] == "never" else f"bot {r.get('early_s')}s later")
            print(f"  [{r['category']}] {r['title']} {r['outcome']} @{r['price']} "
                  f"({r.get('local_trade_time')} {r.get('station')}) - {extra}")
    print("==================================================")
    return 1 if false_signals else 0


if __name__ == "__main__":
    raise SystemExit(main())
