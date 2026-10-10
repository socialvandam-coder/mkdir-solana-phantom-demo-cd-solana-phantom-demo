"""The bot's money: a start amount that grows (or shrinks) with every closed trade.

Position sizes are percentages of the *current* balance, so profits are reinvested
automatically and the bets grow with the account - the way HighTempTation's bot scaled up.
State is saved to disk so a restart continues where it left off.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .config import Config

MAX_HISTORY = 5000


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


class Bankroll:
    def __init__(self, cfg: Config, path: str | os.PathLike | None = None) -> None:
        self.cfg = cfg
        self.path = Path(path) if path else None
        self.open_cost = 0.0
        self.spent_today: dict[str, float] = {}
        self.wallet_usdc: float | None = None   # live mode: real USDC in the account
        self.state = self._load() or self._fresh(cfg.start_amount)

    # -- persistence -------------------------------------------------------
    @staticmethod
    def _fresh(start_amount: float) -> dict:
        now = time.time()
        return {
            "start_amount": round(float(start_amount), 2),
            "started_at": now,
            "realized": 0.0,
            "trades": 0,
            "wins": 0,
            "day": _today(),
            "day_start_balance": round(float(start_amount), 2),
            "history": [[now, round(float(start_amount), 2)]],
            "positions": [],
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
        return base

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)  # atomic: never leaves a half-written file

    def reset(self, start_amount: float) -> None:
        """Start over with a new amount (history is cleared)."""
        if start_amount <= 0:
            raise ValueError("start amount must be positive")
        positions = self.state.get("positions", [])
        self.state = self._fresh(start_amount)
        self.state["positions"] = positions  # positions still open keep being managed
        self.spent_today.clear()
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
    def available(self) -> float:
        free = self.balance - self.open_cost
        if self.wallet_usdc is not None:
            free = min(free, self.wallet_usdc)
        return max(0.0, free)

    def _roll_day(self) -> None:
        today = _today()
        if self.state["day"] != today:
            self.state["day"] = today
            self.state["day_start_balance"] = round(self.balance, 6)
            self.spent_today.clear()
            self.save()

    @property
    def today_pnl(self) -> float:
        self._roll_day()
        return self.balance - float(self.state["day_start_balance"])

    @property
    def today_pct(self) -> float:
        base = float(self.state["day_start_balance"])
        return self.today_pnl / base * 100 if base > 0 else 0.0

    @property
    def total_pnl(self) -> float:
        return float(self.state["realized"])

    @property
    def total_pct(self) -> float:
        return self.total_pnl / self.start_amount * 100 if self.start_amount > 0 else 0.0

    def stopped_for_today(self) -> bool:
        base = float(self.state["day_start_balance"])
        return self.today_pnl <= -(self.cfg.daily_loss_pct / 100.0) * base

    def budget(self, token_id: str) -> float:
        """How much USDC the next buy of this token may use."""
        self._roll_day()
        if self.stopped_for_today():
            return 0.0
        bal = self.balance
        per_trade = bal * self.cfg.trade_pct / 100.0
        per_market = bal * self.cfg.market_pct / 100.0 - self.spent_today.get(token_id, 0.0)
        open_room = bal * self.cfg.open_pct / 100.0 - self.open_cost
        return max(0.0, min(per_trade, per_market, open_room, self.available))

    # -- events --------------------------------------------------------------
    def on_buy(self, token_id: str, cost: float) -> None:
        self._roll_day()
        self.spent_today[token_id] = self.spent_today.get(token_id, 0.0) + cost
        self.open_cost += cost

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
        self.save()

    def set_positions(self, positions: list[dict]) -> None:
        self.state["positions"] = positions
        self.save()

    def snapshot(self) -> dict:
        self._roll_day()
        return {
            "start_amount": round(self.start_amount, 2),
            "started_at": self.state["started_at"],
            "balance": round(self.balance, 4),
            "available": round(self.available, 4),
            "open_cost": round(self.open_cost, 4),
            "today_pnl": round(self.today_pnl, 4),
            "today_pct": round(self.today_pct, 3),
            "total_pnl": round(self.total_pnl, 4),
            "total_pct": round(self.total_pct, 3),
            "trades": self.state["trades"],
            "wins": self.state["wins"],
            "stopped_for_today": self.stopped_for_today(),
            "next_trade_size": round(self.balance * self.cfg.trade_pct / 100.0, 2),
            "wallet_usdc": self.wallet_usdc,
        }

    def history(self, max_points: int = 400) -> list[list[float]]:
        hist = self.state["history"]
        if len(hist) <= max_points:
            return [list(p) for p in hist]
        step = len(hist) / max_points
        picked = [hist[int(i * step)] for i in range(max_points - 1)] + [hist[-1]]
        return [list(p) for p in picked]
