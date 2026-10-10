"""Is the Mac on battery? On battery a Mac may still go to sleep, and then the bot stops looking.

Only on macOS (`pmset -g batt`); elsewhere `power_status()` returns None.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time

_SOURCE_RE = re.compile(r"Now drawing from '([^']+)'")
_PCT_RE = re.compile(r"(\d{1,3})%")


def parse_pmset(text: str) -> dict | None:
    """Reads the output of `pmset -g batt`. None when it says nothing about the power source."""
    m = _SOURCE_RE.search(text or "")
    if not m:
        return None
    pct = _PCT_RE.search(text)
    return {"on_battery": "battery" in m.group(1).lower(), "source": m.group(1),
            "percent": int(pct.group(1)) if pct else None}


def power_status(run=subprocess.run, platform: str | None = None) -> dict | None:
    if (platform or sys.platform) != "darwin" or not shutil.which("pmset"):
        return None
    try:
        r = run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    info = parse_pmset(r.stdout)
    if info is not None:
        info["checked_at"] = time.time()
    return info
