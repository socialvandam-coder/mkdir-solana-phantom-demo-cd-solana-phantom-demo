"""Append-only JSONL log of everything the bot sees and does (used by the report tools)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class Journal:
    def __init__(self, directory: str) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, type_: str, **data) -> None:
        now = datetime.now(timezone.utc)
        row = {"ts": now.timestamp(), "time": now.isoformat(timespec="seconds"), "type": type_, **data}
        path = self.dir / f"journal-{now:%Y-%m-%d}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")


def read_journal(directory: str) -> list[dict]:
    rows = []
    for path in sorted(Path(directory).glob("journal-*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows
