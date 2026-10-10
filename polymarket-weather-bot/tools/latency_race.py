"""Which free METAR source delivers new airport reports first?

Polls several sources side by side for a while and records, per station and report, the
moment each source first showed it. Prints how many minutes after the report's own time
each source had it, and how often each source was first.

    python tools/latency_race.py --minutes 35

Sources: aviationweather.gov API, aviationweather.gov bulk cache file, NOAA tgftp station
files, Iowa Environmental Mesonet "currents". Needs internet access (runs on GitHub Actions).
"""
from __future__ import annotations

import argparse
import collections
import csv
import gzip
import io
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weatherbot.markets import fetch_events  # noqa: E402
from weatherbot.metar import parse_metar  # noqa: E402

UA = {"User-Agent": "weerbot-latency/1.0 (research; low volume)"}
seen: dict[tuple[str, datetime], dict[str, float]] = collections.defaultdict(dict)
lock = threading.Lock()
errors = collections.Counter()


def record(source: str, raw: str) -> None:
    o = parse_metar(raw, ref=datetime.now(timezone.utc))
    if not o:
        return
    with lock:
        seen[(o.station, o.time)].setdefault(source, time.time())


def aw_api(stations: list[str], s: requests.Session) -> None:
    r = s.get("https://aviationweather.gov/api/data/metar",
              params={"ids": ",".join(stations), "format": "json", "hours": 1}, timeout=20)
    r.raise_for_status()
    for row in r.json() or []:
        record("aviationweather-api", row.get("rawOb") or "")


def aw_cache(stations: list[str], s: requests.Session) -> None:
    r = s.get("https://aviationweather.gov/data/cache/metars.cache.csv.gz", timeout=60)
    r.raise_for_status()
    text = gzip.decompress(r.content).decode("utf-8", "replace")
    want = set(stations)
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("raw_text")), 0)
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        if row.get("station_id") in want:
            record("aviationweather-cache", row.get("raw_text") or "")


def tgftp(stations: list[str], s: requests.Session) -> None:
    def one(st):
        r = s.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{st}.TXT", timeout=15)
        if r.status_code == 200:
            lines = [ln for ln in r.text.splitlines() if ln.strip()]
            if len(lines) >= 2:
                record("noaa-tgftp", lines[1])
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(one, stations))


def iem(stations: list[str], s: requests.Session) -> None:
    def one(st):
        sid = st[1:] if len(st) == 4 and st[0] == "K" else st
        r = s.get("https://mesonet.agron.iastate.edu/api/1/currents.json", params={"station": sid}, timeout=20)
        if r.status_code == 200:
            for row in (r.json().get("data") or []):
                raw = row.get("raw") or ""
                if raw:
                    record("iem-currents", raw if raw.split()[0] in ("METAR", "SPECI") or raw[:4] == st else f"{st} {raw}")
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(one, stations))


SOURCES = {"aviationweather-api": (aw_api, 10), "aviationweather-cache": (aw_cache, 30),
           "noaa-tgftp": (tgftp, 15), "iem-currents": (iem, 30)}


def loop(name: str, fn, every: float, stations: list[str], until: float) -> None:
    s = requests.Session()
    s.headers.update(UA)
    while time.time() < until:
        t0 = time.time()
        try:
            fn(stations, s)
        except Exception as exc:  # noqa: BLE001 - count and keep going
            errors[f"{name}: {type(exc).__name__}"] += 1
        time.sleep(max(1.0, every - (time.time() - t0)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=35)
    args = ap.parse_args()
    s = requests.Session()
    s.headers.update(UA)
    events = fetch_events(s, datetime.now(timezone.utc))
    stations = sorted({e.station for e in events})
    print(f"racing {len(SOURCES)} sources on {len(stations)} stations for {args.minutes:.0f} min")
    start = time.time()
    until = start + args.minutes * 60
    threads = [threading.Thread(target=loop, args=(n, f, e, stations, until), daemon=True)
               for n, (f, e) in SOURCES.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # only reports that appeared during the race (not the backlog already there at the start)
    fresh = {k: v for k, v in seen.items() if k[1].timestamp() > start - 300}
    delays = collections.defaultdict(list)
    first = collections.Counter()
    lead_vs_aw = collections.defaultdict(list)
    per_station = collections.defaultdict(list)
    for (st, t), by in fresh.items():
        if not by:
            continue
        win = min(by, key=by.get)
        first[win] += 1
        for src, ts in by.items():
            delays[src].append((ts - t.timestamp()) / 60)
        if "aviationweather-api" in by:
            for src, ts in by.items():
                lead_vs_aw[src].append(by["aviationweather-api"] - ts)
        per_station[st].append((win, round((by[win] - t.timestamp()) / 60, 1)))
    print(f"\nnew reports seen during the race: {len(fresh)}")
    print("source                   n   minutes after report time (p10/median/p90)   first   median lead vs aviationweather-api (s)")
    for src in SOURCES:
        d = sorted(delays.get(src, []))
        if not d:
            print(f"{src:22s}   0")
            continue
        q = lambda p: d[min(len(d) - 1, int(len(d) * p))]  # noqa: E731
        lead = statistics.median(lead_vs_aw[src]) if lead_vs_aw.get(src) else float("nan")
        print(f"{src:22s} {len(d):3d}   {q(.1):+6.1f} / {statistics.median(d):+6.1f} / {q(.9):+6.1f}"
              f"                     {first[src]:4d}    {lead:+7.0f}")
    print("\nper station (winner, minutes after report time):")
    for st in sorted(per_station):
        print(f"  {st}: {per_station[st]}")
    if errors:
        print("\nerrors:", dict(errors))


if __name__ == "__main__":
    main()
