"""The bot's money: a start amount that grows (or shrinks) with every closed trade.

Position sizes are percentages of the *current* equity, so profits are reinvested
automatically and the bets grow with the account - the way HighTempTation's bot scaled up.
State is saved to disk so a restart continues where it left off: the balance, the open
positions, what was already spent today, the buying pause/halt and the bot's trading memory.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config

MAX_HISTORY = 5000

# Why buying is paused, most important first. The label finishes the sentence
# "Gepauzeerd sinds 14:02 - ...".
HALT_LABELS = {
    "noodstop": "door jou (noodstop)",
    "drawdown": "door verlies vanaf je hoogste stand",
    "geoblock": "omdat Polymarket handelen vanaf hier niet toestaat",
    "dagverlies": "door dagverlies",
    "onbevestigd": "door een onbevestigde verkoop",
    "fouten": "door fouten bij kopen of verkopen",
    "klok": "doordat de klok van je computer afwijkt",
    "weerdata": "door storing weerdata",
    "pauze": "door jou",
}
# 'Hervat' may lift these. The others lift themselves (next day, data fresh again, access allowed).
USER_CLEARABLE = ("pauze", "noodstop", "drawdown", "onbevestigd", "fouten")


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def next_local_midnight(now: float | None = None) -> float:
    d = datetime.fromtimestamp(now if now is not None else time.time())
    return (datetime(d.year, d.month, d.day) + timedelta(days=1)).timestamp()


class Bankroll:
    def __init__(self, cfg: Config, path: str | os.PathLike | None = None) -> None:
        self.cfg = cfg
        self.path = Path(path) if path else None
        self.open_cost = 0.0
        self.open_value: float | None = None    # open positions at their current marks (None = at cost)
        self.marks: dict[str, float] = {}       # token -> value per share, set by the executor
        self.wallet_usdc: float | None = None   # live mode: real USDC in the account
        self._save_lock = threading.Lock()      # the dashboard (pause/stop) and the bot loop both save
        self.state = self._load() or self._fresh(cfg.start_amount)

    # -- persistence -------------------------------------------------------
    @staticmethod
    def _fresh(start_amount: float) -> dict:
        now = time.time()
        amount = round(float(start_amount), 2)
        return {
            "start_amount": amount,
            "started_at": now,
            "realized": 0.0,
            "trades": 0,
            "wins": 0,
            "day": _today(),
            "day_start_balance": amount,
            "day_start_equity": amount,
            "peak_equity": amount,
            "history": [[now, amount]],
            "positions": [],
            "spent_today": {},        # token -> USDC bought today (MARKET_PCT)
            "open_event_cost": {},    # event slug -> USDC in positions not yet confirmed (EVENT_PCT)
            "watch": {},              # token -> {first_seen, obs_time, ...}: signals already seen
            "locked_events": {},      # event slug -> until (after an unconfirmed position)
            "halts": {},              # reason -> {by, since, auto_resume_at}
            "halt": {"on": False},
        }

    def _load(self) -> dict | None:
        if not self.path or not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "start_amount" not in data:
            return None
        base = self._fresh(data["start_amount"])
        base.update(data)
        # state files from before equity tracking: start from the saved balance
        bal = float(base["start_amount"]) + float(base.get("realized") or 0)
        if "day_start_equity" not in data:
            base["day_start_equity"] = float(base.get("day_start_balance") or bal)
        if "peak_equity" not in data:
            base["peak_equity"] = max(bal, float(base["day_start_equity"]))
        for key in ("spent_today", "open_event_cost", "watch", "locked_events", "halts"):
            if not isinstance(base.get(key), dict):
                base[key] = {}
        return base

    def save(self) -> None:
        if not self.path:
            return
        with self._save_lock:
            for _ in range(5):
                try:
                    text = json.dumps(self.state)
                    break
                except RuntimeError:        # changed by the other thread mid-copy: try again
                    time.sleep(0.01)
            else:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, self.path)  # atomic: never leaves a half-written file

    def reset(self, start_amount: float) -> None:
        """Start over with a new amount (history is cleared)."""
        if start_amount <= 0:
            raise ValueError("start amount must be positive")
        old = self.state
        self.state = self._fresh(start_amount)
        # positions still open keep being managed; the trading memory and your own pause stay
        for key in ("positions", "watch", "locked_events", "open_event_cost"):
            self.state[key] = old.get(key, self.state[key])
        self.state["halts"] = {k: v for k, v in (old.get("halts") or {}).items()
                               if k not in ("drawdown", "dagverlies")}
        self._sync_halt()
        self.save()

    # -- numbers -----------------------------------------------------------
    @property
    def start_amount(self) -> float:
        return float(self.state["start_amount"])

    @property
    def balance(self) -> float:
        """Start amount plus everything won/lost on closed trades."""
        return self.start_amount + float(self.state["realized"])

    @property
    def equity(self) -> float:
        """Balance with the open positions at their current value (at cost until marked)."""
        value = self.open_cost if self.open_value is None else self.open_value
        return self.balance - self.open_cost + value

    @property
    def available(self) -> float:
        free = self.balance - self.open_cost
        if self.wallet_usdc is not None:
            free = min(free, self.wallet_usdc)
        return max(0.0, free)

    @property
    def spent_today(self) -> dict[str, float]:
        return self.state.setdefault("spent_today", {})

    @property
    def open_event_cost(self) -> dict[str, float]:
        return self.state.setdefault("open_event_cost", {})

    def set_open_event_cost(self, by_event: dict[str, float]) -> None:
        self.state["open_event_cost"] = {k: round(v, 6) for k, v in by_event.items() if v > 1e-9}

    def _roll_day(self) -> None:
        today = _today()
        if self.state["day"] != today:
            self.state["day"] = today
            self.state["day_start_balance"] = round(self.balance, 6)
            self.state["day_start_equity"] = round(self.equity, 6)
            self.spent_today.clear()
            self.save()

    def update_peak(self) -> None:
        eq = self.equity
        if eq > float(self.state.get("peak_equity") or 0):
            self.state["peak_equity"] = round(eq, 6)

    @property
    def peak_equity(self) -> float:
        self.update_peak()
        return float(self.state["peak_equity"])

    @property
    def drawdown_pct(self) -> float:
        """How far equity is below its highest point, in %."""
        peak = self.peak_equity
        return max(0.0, (peak - self.equity) / peak * 100) if peak > 0 else 0.0

    @property
    def today_pnl(self) -> float:
        self._roll_day()
        return self.balance - float(self.state["day_start_balance"])

    @property
    def today_realized_pct(self) -> float:
        """Today's result on closed trades only, in % of the day's start balance."""
        base = float(self.state["day_start_balance"])
        return self.today_pnl / base * 100 if base > 0 else 0.0

    @property
    def today_equity_pnl(self) -> float:
        self._roll_day()
        return self.equity - float(self.state["day_start_equity"])

    @property
    def today_pct(self) -> float:
        """Today's result including open positions at their current value (what the dashboard shows):
        a wrong bucket counts the moment its price drops, not only when the market resolves."""
        pnl = self.today_equity_pnl
        base = float(self.state["day_start_equity"])
        return pnl / base * 100 if base > 0 else 0.0

    @property
    def total_pnl(self) -> float:
        return float(self.state["realized"])

    @property
    def total_pct(self) -> float:
        return self.total_pnl / self.start_amount * 100 if self.start_amount > 0 else 0.0

    @property
    def total_equity_pct(self) -> float:
        """Since the start, open positions included at their current value."""
        return (self.equity - self.start_amount) / self.start_amount * 100 if self.start_amount > 0 else 0.0

    def stopped_for_today(self) -> bool:
        """Lost DAILY_LOSS_PCT of today's start equity (open positions at their current value)."""
        base = float(self.state["day_start_equity"])
        return base > 0 and self.today_equity_pnl <= -(self.cfg.daily_loss_pct / 100.0) * base

    def budget_caps(self, token_id: str, event_slug: str | None = None) -> dict[str, float]:
        """Every limit on the next buy, in USDC. The smallest one wins."""
        self._roll_day()
        c = self.cfg
        eq = max(0.0, self.equity)
        in_event = self.open_event_cost.get(event_slug, 0.0) if event_slug else 0.0
        return {
            "trade": eq * c.trade_pct / 100.0,
            "market": eq * c.market_pct / 100.0 - self.spent_today.get(token_id, 0.0),
            "event": eq * c.event_pct / 100.0 - in_event,
            "trade_usdc": c.max_trade_usdc,
            "event_usdc": c.max_event_usdc - in_event,
            "open": eq * c.open_pct / 100.0 - self.open_cost,
            "available": self.available,
        }

    def budget(self, token_id: str, event_slug: str | None = None) -> float:
        """How much USDC the next buy of this token (in this market) may use."""
        if self.stopped_for_today():
            return 0.0
        return max(0.0, min(self.budget_caps(token_id, event_slug).values()))

    def limiting_cap(self, token_id: str, event_slug: str | None = None) -> str:
        caps = self.budget_caps(token_id, event_slug)
        return min(caps, key=caps.get)

    # -- buying pause / halt ---------------------------------------------------
    @property
    def halts(self) -> dict[str, dict]:
        return self.state.setdefault("halts", {})

    def set_halt(self, reason: str, by: str = "auto", until: float | None = None,
                 now: float | None = None, detail: str = "") -> bool:
        """Pause buying for `reason`. Returns True when this reason was not active yet."""
        now = time.time() if now is None else now
        cur = self.halts.get(reason)
        if cur is None:
            self.halts[reason] = {"by": by, "since": now, "auto_resume_at": until, "detail": detail}
        elif until is not None and cur.get("auto_resume_at") is not None and cur["auto_resume_at"] < until:
            cur["auto_resume_at"] = until
        else:
            return False
        self._sync_halt()
        self.save()
        return cur is None

    def clear_halt(self, reason: str) -> bool:
        if self.halts.pop(reason, None) is None:
            return False
        self._sync_halt()
        self.save()
        return True

    def expired_halts(self, now: float) -> list[str]:
        return [r for r, h in dict(self.halts).items() if h.get("auto_resume_at") and now >= h["auto_resume_at"]]

    def _sync_halt(self) -> None:
        """state['halt'] = the most important active reason (what the dashboard shows)."""
        halts = dict(self.halts)                # a copy: the other thread may change it meanwhile
        active = [r for r in HALT_LABELS if r in halts] + [r for r in halts if r not in HALT_LABELS]
        if not active:
            self.state["halt"] = {"on": False}
            return
        top = active[0]
        h = halts[top]
        self.state["halt"] = {"on": True, "reason": top, "by": h.get("by", "auto"), "since": h.get("since"),
                              "auto_resume_at": h.get("auto_resume_at")}

    def halt_info(self) -> dict:
        self._sync_halt()
        info = dict(self.state["halt"])
        if info.get("on"):
            info["label"] = HALT_LABELS.get(info["reason"], info["reason"])
            halts = dict(self.halts)
            order = list(HALT_LABELS)
            ranked = sorted(halts, key=lambda r: order.index(r) if r in order else len(order))
            info["reasons"] = [{"reason": r, "label": HALT_LABELS.get(r, r), **halts[r]} for r in ranked]
            info["can_resume"] = any(r in USER_CLEARABLE for r in halts)
        return info

    # -- events --------------------------------------------------------------
    def on_buy(self, token_id: str, cost: float, event_slug: str | None = None) -> None:
        self._roll_day()
        self.spent_today[token_id] = self.spent_today.get(token_id, 0.0) + cost
        self.open_cost += cost
        if event_slug:
            self.open_event_cost[event_slug] = self.open_event_cost.get(event_slug, 0.0) + cost

    def on_close(self, cost: float, proceeds: float) -> None:
        self._roll_day()
        self.open_cost = max(0.0, self.open_cost - cost)
        pnl = proceeds - cost
        self.state["realized"] = round(float(self.state["realized"]) + pnl, 6)
        self.state["trades"] += 1
        if pnl > 0:
            self.state["wins"] += 1
        hist = self.state["history"]
        hist.append([time.time(), round(self.balance, 6)])
        if len(hist) > MAX_HISTORY:
            del hist[1:len(hist) - MAX_HISTORY + 1]
        self.update_peak()
        self.save()

    def set_positions(self, positions: list[dict]) -> None:
        self.state["positions"] = positions
        self.save()

    def snapshot(self) -> dict:
        self._roll_day()
        self.update_peak()
        return {
            "start_amount": round(self.start_amount, 2),
            "started_at": self.state["started_at"],
            "balance": round(self.balance, 4),
            "equity": round(self.equity, 4),
            "available": round(self.available, 4),
            "open_cost": round(self.open_cost, 4),
            "open_value": round(self.open_cost if self.open_value is None else self.open_value, 4),
            "today_pnl": round(self.today_pnl, 4),                   # closed trades only
            "today_realized_pct": round(self.today_realized_pct, 3),
            "today_pct": round(self.today_pct, 3),                   # incl. open positions at their value
            "today_equity_pnl": round(self.today_equity_pnl, 4),
            "day_start_equity": round(float(self.state["day_start_equity"]), 4),
            "total_equity_pct": round(self.total_equity_pct, 3),
            "peak_equity": round(self.peak_equity, 4),
            "drawdown_pct": round(self.drawdown_pct, 3),
            "total_pnl": round(self.total_pnl, 4),
            "total_pct": round(self.total_pct, 3),
            "trades": self.state["trades"],
            "wins": self.state["wins"],
            "stopped_for_today": self.stopped_for_today(),
            "next_trade_size": round(self.budget(""), 2),   # a fresh bucket, after every limit
            "wallet_usdc": self.wallet_usdc,
            "halt": self.halt_info(),
        }

    def history(self, max_points: int = 400) -> list[list[float]]:
        hist = self.state["history"]
        if len(hist) <= max_points:
            return [list(p) for p in hist]
        step = len(hist) / max_points
        picked = [hist[int(i * step)] for i in range(max_points - 1)] + [hist[-1]]
        return [list(p) for p in picked]
