"""Mac notifications for the rare moments that need attention (never one per trade).

On macOS this shows a normal notification ("display notification" via osascript).
Elsewhere it does nothing except write the 'notify' row to the journal.
At most one notification per kind per 15 minutes.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)

MIN_GAP_SECONDS = 15 * 60
KINDS = ("halt", "unconfirmed", "loss", "gap", "geoblock")

_SCRIPT = ("on run argv\n"
           "display notification (item 2 of argv) with title \"Weerbot\" subtitle (item 1 of argv)\n"
           "end run")


def _show_mac(title: str, text: str) -> None:
    try:
        # the texts go in as arguments, never into the script itself
        subprocess.run(["osascript", "-e", _SCRIPT, title[:120], text[:300]],
                       capture_output=True, timeout=15, check=False)
    except Exception as exc:  # noqa: BLE001 - a missing notification must never hurt the bot
        log.debug("notification failed: %s", exc)


class Notifier:
    def __init__(self, journal=None, min_gap: float = MIN_GAP_SECONDS) -> None:
        self.journal = journal
        self.min_gap = min_gap
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def notify(self, kind: str, title: str, text: str, now: float | None = None) -> bool:
        """Returns True when the notification was sent (not rate-limited)."""
        now = time.time() if now is None else now
        with self._lock:
            last = self._last.get(kind)
            if last is not None and now - last < self.min_gap:
                return False
            self._last[kind] = now
        shown = sys.platform == "darwin"
        if self.journal is not None:
            self.journal.write("notify", kind=kind, title=title, text=text, shown=shown)
        log.info("melding (%s): %s - %s", kind, title, text)
        if shown:
            # osascript can take a moment; never hold up the trading loop for it
            threading.Thread(target=_show_mac, args=(title, text), daemon=True).start()
        return True


def notify(kind: str, title: str, text: str) -> bool:
    """Module-level convenience (no journal)."""
    return _default.notify(kind, title, text)


_default = Notifier()
