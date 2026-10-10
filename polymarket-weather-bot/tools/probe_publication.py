"""How early/late do airport reports appear, compared with the time printed in them?

Uses aviationweather.gov's receiptTime (when the report reached the US data system) and the
report's own time group. Negative = published BEFORE its nominal time.

    python tools/probe_publication.py LFPB NZWN FACT SBGR ...
"""
from __future__ import annotations

import collections
import statistics
import sys
from datetime import datetime, timezone

import requests

URL = "https://aviationweather.gov/api/data/metar"


def main() -> None:
    stations = sys.argv[1:] or ["NZWN", "FACT", "SBGR", "MPMG", "LFPB", "EGLC", "KLGA", "RJTT", "WSSS"]
    r = requests.get(URL, params={"ids": ",".join(stations), "format": "json", "hours": 48},
                     headers={"User-Agent": "weerbot-probe/1.0"}, timeout=60)
    r.raise_for_status()
    per = collections.defaultdict(list)
    for row in r.json():
        try:
            rt = datetime.strptime(row["receiptTime"][:19], "%Y-%m-%dT%H:%M:%S" if "T" in row["receiptTime"] else "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            nominal = datetime.strptime(row["reportTime"][:19], "%Y-%m-%dT%H:%M:%S" if "T" in row["reportTime"] else "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        obs_t = datetime.fromtimestamp(row["obsTime"], timezone.utc) if isinstance(row.get("obsTime"), (int, float)) else None
        per[row.get("icaoId")].append(((rt - nominal).total_seconds(), (nominal - obs_t).total_seconds() if obs_t else None,
                                       row.get("metarType")))
    print("station  n   receipt-minus-nominal (min): p10 / median / p90   nominal-minus-obsTime (s)   types")
    for st in stations:
        rows = per.get(st, [])
        if not rows:
            print(f"{st:6s}  0")
            continue
        d = sorted(x[0] / 60 for x in rows)
        q = lambda p: d[min(len(d) - 1, int(len(d) * p))]  # noqa: E731
        diffs = sorted({x[1] for x in rows if x[1] is not None})
        types = collections.Counter(x[2] for x in rows)
        print(f"{st:6s} {len(rows):3d}   {q(.1):+6.1f} / {statistics.median(d):+6.1f} / {q(.9):+6.1f}"
              f"            {diffs[:5]}   {dict(types)}")


if __name__ == "__main__":
    main()
