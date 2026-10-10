"""Check how closely the bot copies HighTempTation.

Fetches his real trades for the period the bot was running and matches them with the
bot's signals: did we flag the same bucket, and were we earlier or later than him?

    python tools/compare_hightemptation.py [logs]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weatherbot.compare import compare  # noqa: E402
from weatherbot.engine import make_session  # noqa: E402
from weatherbot.journal import read_journal  # noqa: E402


def main() -> None:
    rows = read_journal(sys.argv[1] if len(sys.argv) > 1 else "logs")
    res = compare(rows, make_session())
    if not res["ok"]:
        print(res["message"])
        return
    print(f"HighTempTation bought {res['his_buckets']} buckets while the bot ran (${res['his_usdc']:.0f})")
    if not res["his_buckets"]:
        return
    print(f"  also flagged by the bot: {res['matched']} ({res['match_pct']}%)")
    print(f"  bot also saw cheap asks there: {res['bot_saw_cheap_asks']}, bot bought too: {res['bot_bought_too']}")
    if res["median_delta_s"] is not None:
        print(f"  bot earlier than him: {res['bot_first']}x, later: {res['bot_later']}x, "
              f"median difference {res['median_delta_s']:+d}s (negative = bot first)")
    if res["missed"]:
        print("missed (first 25) - check station/rounding/timezone for these:")
        for t in res["missed"]:
            print(f"   {t['title']}  {t['outcome']} @ {t['price']:.3f}")


if __name__ == "__main__":
    main()
