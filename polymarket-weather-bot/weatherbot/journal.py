"""Append-only JSONL log of everything the bot sees and does (used by the report tools)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class Journal:
    def __init__(self, directory: str) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._listeners: list[Callable[[dict], None]] = []

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        """fn(row) is called for every row written (the dashboard uses this for its live feed)."""
        self._listeners.append(fn)

    def write(self, type_: str, **data) -> None:
        now = datetime.now(timezone.utc)
        row = {"ts": now.timestamp(), "time": now.isoformat(timespec="seconds"), "type": type_, **data}
        path = self.dir / f"journal-{now:%Y-%m-%d}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        for fn in self._listeners:
            fn(row)


def read_journal(directory: str, since_ts: float = 0.0) -> list[dict]:
    rows = []
    # files are per UTC day: a file from before since_ts's day only holds older rows
    first_day = datetime.fromtimestamp(since_ts, timezone.utc).strftime("%Y-%m-%d") if since_ts > 0 else ""
    for path in sorted(Path(directory).glob("journal-*.jsonl")):
        if first_day and path.stem[len("journal-"):] < first_day:
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a line cut off by a crash
            if row.get("ts", 0) >= since_ts:
                rows.append(row)
    return rows
