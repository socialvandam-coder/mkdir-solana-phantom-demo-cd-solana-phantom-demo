"""Buying the dead bucket and reselling it at 0.99 - simulated (paper) or for real (live).

Safety rules (from HighTempTation's own record):
- Only buy within FRESH_SECONDS of the report that killed the bucket, never average down.
- A buy that has no 0.99 exit within CONFIRM_SECONDS is the main alarm: the market is locked
  for the rest of its day and the weather data is checked again. There is no price stop-loss;
  the 0.99 ask stays up until the market resolves.
- Every buy checks the buying pause/halt first; selling and settling always go on.
"""
from __future__ import annotations

import collections
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from .bankroll import Bankroll
from .cities import CITY_TZ
from .config import Config
from .journal import Journal
from .markets import Market, fetch_resolution, parse_bucket
from .notify import Notifier
from .orderbook import Book, SweepPlan, fetch_books, fillable_at, plan_sweep, taker_fee_per_share
from .strategy import Signal

log = logging.getLogger(__name__)

FAST_PHASE_SECONDS = 60.0     # the first minute after a signal or a buy: look every few seconds
FAST_RECHECK_SECONDS = 5.0    # signal recheck cadence in that minute (then RECHECK_EVERY_SECONDS)
FAST_MANAGE_SECONDS = 3.0     # position poll cadence in that minute (then RECHECK_EVERY_SECONDS)
REBUY_MAX_DROP = 0.03         # a quick rebuy may be at most this much cheaper than the first fill
CONFIRM_FRACTION = 0.5        # confirmed = at least half sold ...
CONFIRM_PRICE_SLACK = 0.005   # ... at SELL_PRICE - 0.005 or better
WAIT_FOR_RESULT_SECONDS = 3600
RECHECK_TRIES = 5
KIND_NL = {"high": "max", "low": "min"}


def nl(v: float, digits: int = 2) -> str:
    """Number with a decimal comma, for Dutch messages."""
    return f"{v:.{digits}f}".replace(".", ",")


def backoff(n: int) -> float:
    """Retry delay after n failures in a row: 5 s, 10 s, 20 s ... up to 5 minutes."""
    return min(300.0, 5.0 * 2 ** max(0, n - 1))


def event_day_end(s: Signal) -> float:
    """End of the market's local day (an event lock lasts until then)."""
    tz = CITY_TZ.get(s.city)
    if not tz:
        return s.obs_time.timestamp() + 86400
    zone = ZoneInfo(tz)
    day = s.obs_time.astimezone(zone).date()
    return datetime.combine(day + timedelta(days=1), dtime(0), tzinfo=zone).timestamp()


@dataclass
class Watch:
    """A dead bucket the bot has seen. Saved, so a restart does not treat old kills as new."""
    token_id: str
    first_seen: float
    obs_time: float                  # the report that first made this bucket dead
    last_check: float = 0.0
    signal: Signal | None = None
    first_fill_at: float | None = None
    first_avg: float | None = None

    def to_dict(self) -> dict:
        return {"first_seen": self.first_seen, "obs_time": self.obs_time,
                "first_fill_at": self.first_fill_at, "first_avg": self.first_avg}

    @classmethod
    def from_dict(cls, token_id: str, d: dict) -> "Watch | None":
        try:
            return cls(token_id=token_id, first_seen=float(d["first_seen"]), obs_time=float(d["obs_time"]),
                       first_fill_at=None if d.get("first_fill_at") is None else float(d["first_fill_at"]),
                       first_avg=None if d.get("first_avg") is None else float(d["first_avg"]))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class Position:
    signal: Signal
    shares: float
    cost: float                  # USDC incl. estimated buy fee
    opened_at: float
    sell_after: float
    sold_shares: float = 0.0
    proceeds: float = 0.0
    status: str = "open"         # open -> selling -> closed
    sell_order_id: str | None = None
    sell_attempts: int = 0
    errors: int = 0
    last_poll: float = 0.0           # last time we looked at the book / sell order
    last_settle_check: float = 0.0   # last time we asked whether the market resolved
    avg_price: float = 0.0           # average buy price per share (without fee)
    target_sold: float = 0.0         # shares sold at SELL_PRICE - 0.005 or better (the normal exit)
    confirmed_at: float | None = None
    unconfirmed_at: float | None = None
    next_poll: float = 0.0
    order_matched: float = 0.0       # size_matched of the current live sell order
    exit_reason: str | None = None   # set when the data re-check says the bucket is not dead after all
    recheck: str | None = None       # data re-check result: dead / alive / failed
    bid: float | None = None         # best bid at the last look
    no_bid_count: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def token_id(self) -> str:
        return self.signal.token_id

    @property
    def remaining(self) -> float:
        return round(self.shares - self.sold_shares, 4)

    @property
    def avg(self) -> float:
        if self.avg_price:
            return self.avg_price
        return self.cost / self.shares if self.shares else 0.0

    def to_dict(self) -> dict:
        s, m = self.signal, self.signal.market
        return {
            "signal": {"event_slug": s.event_slug, "city": s.city, "kind": s.kind, "outcome": s.outcome,
                       "token_id": s.token_id, "observed": s.observed, "obs_time": s.obs_time.isoformat(),
                       "reason": s.reason},
            "market": {"question": m.question, "condition_id": m.condition_id, "yes_token": m.yes_token,
                       "no_token": m.no_token, "bucket": m.bucket.label, "tick_size": m.tick_size,
                       "min_size": m.min_size, "neg_risk": m.neg_risk},
            "shares": self.shares, "cost": self.cost, "opened_at": self.opened_at, "sell_after": self.sell_after,
            "sold_shares": self.sold_shares, "proceeds": self.proceeds, "status": self.status,
            "sell_order_id": self.sell_order_id, "sell_attempts": self.sell_attempts,
            "avg_price": self.avg_price, "target_sold": self.target_sold, "confirmed_at": self.confirmed_at,
            "unconfirmed_at": self.unconfirmed_at, "order_matched": self.order_matched,
            "exit_reason": self.exit_reason, "recheck": self.recheck,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position | None":
        try:
            m, s = d["market"], d["signal"]
            bucket = parse_bucket(m["bucket"])
            if bucket is None:
                return None
            market = Market(question=m["question"], condition_id=m["condition_id"], yes_token=m["yes_token"],
                            no_token=m["no_token"], bucket=bucket, tick_size=m.get("tick_size", "0.001"),
                            min_size=float(m.get("min_size", 5)), neg_risk=bool(m.get("neg_risk", True)))
            signal = Signal(event_slug=s["event_slug"], city=s["city"], kind=s["kind"], market=market,
                            outcome=s["outcome"], token_id=s["token_id"], observed=s["observed"],
                            obs_time=datetime.fromisoformat(s["obs_time"]), reason=s.get("reason", ""))
            opt = lambda k: None if d.get(k) is None else float(d[k])  # noqa: E731
            return cls(signal=signal, shares=float(d["shares"]), cost=float(d["cost"]),
                       opened_at=float(d["opened_at"]), sell_after=float(d["sell_after"]),
                       sold_shares=float(d.get("sold_shares", 0)), proceeds=float(d.get("proceeds", 0)),
                       status=d.get("status", "open"), sell_order_id=d.get("sell_order_id"),
                       sell_attempts=int(d.get("sell_attempts", 0)), avg_price=float(d.get("avg_price") or 0),
                       target_sold=float(d.get("target_sold") or 0), confirmed_at=opt("confirmed_at"),
                       unconfirmed_at=opt("unconfirmed_at"), order_matched=float(d.get("order_matched") or 0),
                       exit_reason=d.get("exit_reason"), recheck=d.get("recheck"))
        except (KeyError, TypeError, ValueError):
            return None


def position_row(p: Position, now: float, cfg: Config, locked: bool = False) -> dict:
    """One open position as the dashboard shows it."""
    age = now - p.opened_at
    if p.exit_reason:
        state = "exiting"
    elif p.confirmed_at is not None:
        state = "confirmed"
    elif age > WAIT_FOR_RESULT_SECONDS:
        state = "waiting"
    elif p.unconfirmed_at is not None or age > cfg.confirm_seconds:
        state = "unconfirmed"
    else:
        state = "selling"
    s = p.signal
    return {
        "city": s.city, "kind": s.kind, "bucket": s.market.bucket.label, "outcome": s.outcome,
        "event": s.event_slug, "token_id": s.token_id, "shares": round(p.shares, 2), "cost": round(p.cost, 2),
        "sold": round(p.sold_shares, 2), "avg": round(p.avg, 4), "bid": p.bid, "status": p.status,
        "state": state, "age_s": round(age), "sell_price": cfg.sell_price, "recheck": p.recheck,
        "confirmed_after_s": None if p.confirmed_at is None else round(p.confirmed_at - p.opened_at, 1),
        "locked": locked,
    }


class Executor:
    """Shared logic; subclasses implement the actual buy/sell."""

    # Paper fills don't remove the asks from the real book, so buying the same token again
    # would count the same stale orders twice. Live re-buys are real new asks (he did up to 3).
    allow_rebuy = True

    def __init__(self, cfg: Config, session: requests.Session, journal: Journal,
                 bankroll: Bankroll | None = None, notifier: Notifier | None = None) -> None:
        self.cfg = cfg
        self.session = session
        self.journal = journal
        self.bank = bankroll or Bankroll(cfg)
        self.notifier = notifier or Notifier(journal)
        self.watch: dict[str, Watch] = {}
        self.positions: list[Position] = []
        self.locked_events: dict[str, float] = {}      # event slug -> locked until (end of its local day)
        self.stale_events: set[str] = set()            # set by the runner: the station went quiet
        # set by the runner: re-check the weather data for a position (True = still dead,
        # False = not dead after all -> sell now, None = could not check)
        self.recheck_hook: Callable[[Position], bool | None] | None = None
        self.error_times: collections.deque = collections.deque(maxlen=500)
        self._skipped: set[tuple[str, str]] = set()
        st = self.bank.state
        # pick up positions that were still open when the app last stopped
        for d in st.get("positions", []):
            p = Position.from_dict(d)
            if p and p.status != "closed":
                self.positions.append(p)
        self.bank.open_cost = sum(p.cost for p in self.positions)
        for token, d in (st.get("watch") or {}).items():
            w = Watch.from_dict(token, d) if isinstance(d, dict) else None
            if w:
                self.watch[token] = w
        now = time.time()
        self.locked_events = {k: float(v) for k, v in (st.get("locked_events") or {}).items()
                              if isinstance(v, (int, float)) and v > now}
        self._sync_event_cost()
        if self.positions:
            log.info("resumed %d open position(s) from the last run", len(self.positions))

    # -- memory --------------------------------------------------------------
    def _sync_event_cost(self) -> None:
        """Money per market in positions that are not confirmed (and not closed) yet."""
        by_event: dict[str, float] = {}
        for p in self.positions:
            if p.status != "closed" and p.confirmed_at is None:
                by_event[p.signal.event_slug] = by_event.get(p.signal.event_slug, 0.0) + p.cost
        self.bank.set_open_event_cost(by_event)

    def _persist(self) -> None:
        self._sync_event_cost()
        st = self.bank.state
        st["watch"] = {t: w.to_dict() for t, w in self.watch.items()}
        st["locked_events"] = dict(self.locked_events)
        self.bank.set_positions([p.to_dict() for p in self.positions if p.status != "closed"])

    def refresh(self, now: float) -> None:
        """Called every loop; live mode uses it to keep the real wallet balance in view."""

    def note_error(self, now: float, what: str, exc: Exception | str) -> None:
        self.error_times.append(now)
        log.warning("%s error: %s", what, exc)

    def errors_since(self, since: float) -> int:
        return sum(1 for t in self.error_times if t >= since)

    def wants_fast_loop(self, now: float) -> bool:
        """True in the first minute after a signal or buy (look every few seconds then)."""
        return (any(p.status != "closed" and now - p.opened_at < FAST_PHASE_SECONDS for p in self.positions)
                or any(now - w.first_seen < FAST_PHASE_SECONDS for w in self.watch.values()))

    def unconfirmed(self) -> list[Position]:
        return [p for p in self.positions
                if p.status != "closed" and p.unconfirmed_at is not None and p.confirmed_at is None]

    # -- buying pause / halt -----------------------------------------------
    def can_buy(self) -> tuple[bool, str]:
        """(True, '') or (False, skip reason). Checked before every buy, also right before the order."""
        halts = self.bank.halts
        if not halts:
            return True, ""
        if "geoblock" in halts:
            return False, "geoblock"
        if set(halts) == {"pauze"}:
            return False, "paused"
        return False, "halted"

    def _skip(self, s: Signal, reason: str, now: float, **extra) -> None:
        """Journal why a signal was not bought - once per token and reason."""
        key = (s.token_id, reason)
        if key in self._skipped:
            return
        self._skipped.add(key)
        w = self.watch.get(s.token_id)
        self.journal.write("skip", token_id=s.token_id, city=s.city, kind=s.kind, event=s.event_slug,
                           bucket=s.market.bucket.label, outcome=s.outcome, reason=reason,
                           since_signal_s=round(now - w.first_seen, 1) if w else None,
                           obs_age_s=round(now - (w.obs_time if w else s.obs_time.timestamp()), 1), **extra)

    def _budget_reason(self, s: Signal) -> str:
        if self.bank.stopped_for_today():
            return "daily_stop"
        cap = self.bank.limiting_cap(s.token_id, s.event_slug)
        return {"market": "limit_market", "event": "limit_event", "event_usdc": "limit_event",
                "open": "limit_open"}.get(cap, "budget_too_small")

    # -- signals -> buys -------------------------------------------------
    def on_signals(self, signals: list[Signal], now: float) -> None:
        changed = False
        for token in [t for t, w in self.watch.items() if now - w.first_seen > 2 * 86400]:
            del self.watch[token]
            changed = True
        for slug in [e for e, until in self.locked_events.items() if now >= until]:
            del self.locked_events[slug]
            changed = True
        if changed:
            self._skipped = {k for k in self._skipped if k[0] in self.watch}
        due: list[Signal] = []
        for s in signals:
            w = self.watch.get(s.token_id)
            if w is None:
                w = self.watch[s.token_id] = Watch(token_id=s.token_id, first_seen=now,
                                                   obs_time=s.obs_time.timestamp(), signal=s)
                changed = True
                self.journal.write("signal", city=s.city, kind=s.kind, event=s.event_slug,
                                   bucket=s.market.bucket.label, outcome=s.outcome, token_id=s.token_id,
                                   condition_id=s.market.condition_id, observed=s.observed,
                                   obs_time=s.obs_time.isoformat(), lag_s=round(now - s.obs_time.timestamp(), 1),
                                   reason=s.reason)
                log.info("SIGNAL %s %s %s -> %s (%s)", s.city, s.kind, s.market.bucket.label, s.outcome, s.reason)
            w.signal = s
            # freshness counts from the report that first killed the bucket, not from when we saw it
            obs_age = now - min(w.obs_time, s.obs_time.timestamp())
            if obs_age > self.cfg.fresh_seconds:
                self._skip(s, "too_old", now)
                continue
            if w.last_check:
                gap = FAST_RECHECK_SECONDS if now - w.first_seen < FAST_PHASE_SECONDS else self.cfg.recheck_every_seconds
                if now - w.last_check < gap:
                    continue
            ok, why = self.can_buy()
            if not ok:
                self._skip(s, why, now)
                continue
            if s.event_slug in self.locked_events:
                self._skip(s, "event_locked", now)
                continue
            if s.event_slug in self.stale_events:
                self._skip(s, "stale_station", now)
                continue
            held = [p for p in self.positions if p.token_id == s.token_id]
            if held:
                if not self.allow_rebuy:
                    self._skip(s, "already_holding", now)
                    continue
                first_fill = w.first_fill_at if w.first_fill_at is not None else min(p.opened_at for p in held)
                if (now - first_fill > self.cfg.rebuy_window_seconds
                        and not all(p.confirmed_at is not None for p in held)):
                    self._skip(s, "rebuy_unconfirmed", now)
                    continue
            budget = self.bank.budget(s.token_id, s.event_slug)
            if budget < 1.0:
                self._skip(s, self._budget_reason(s), now, budget=round(budget, 2))
                continue
            due.append(s)
        if changed:
            self._persist()
        if not due:
            return
        try:
            books = fetch_books(self.session, [s.token_id for s in due], self.cfg.clob_host)
        except requests.RequestException as exc:
            log.warning("order book fetch failed: %s", exc)
            for s in due:
                self._skip(s, "book_error", now)
            return
        for s in due:
            w = self.watch[s.token_id]
            w.last_check = now
            book = books.get(s.token_id)
            if not book:
                continue
            budget = self.bank.budget(s.token_id, s.event_slug)
            plan = plan_sweep(book, max_price=self.cfg.max_buy_price, sell_price=self.cfg.sell_price,
                              budget_usdc=budget, min_size=s.market.min_size,
                              min_edge=self.cfg.min_edge, fee_rate=self.cfg.fee_rate)
            if plan is None:
                if book.best_ask is None or book.best_ask > self.cfg.max_buy_price:
                    why = "late_no_cheap_asks"
                elif budget / book.best_ask < s.market.min_size:
                    why = "budget_too_small"
                else:
                    why = "edge_too_small"
                self._skip(s, why, now, best_ask=book.best_ask, budget=round(budget, 2))
                continue
            # never average down: a quick rebuy must not be much cheaper than the first fill
            if (w.first_avg is not None and w.first_fill_at is not None
                    and now - w.first_fill_at <= self.cfg.rebuy_window_seconds
                    and plan.avg_price < w.first_avg - REBUY_MAX_DROP):
                self._skip(s, "rebuy_cheaper", now, best_ask=book.best_ask, avg_price=round(plan.avg_price, 4),
                           first_avg=round(w.first_avg, 4))
                continue
            self.journal.write("opportunity", city=s.city, bucket=s.market.bucket.label, outcome=s.outcome,
                               token_id=s.token_id, best_ask=book.best_ask, shares=plan.shares,
                               cost=plan.cost, avg_price=round(plan.avg_price, 4), worst_price=plan.worst_price,
                               est_profit=plan.est_profit, since_signal_s=round(now - w.first_seen, 1),
                               obs_age_s=round(now - w.obs_time, 1))
            self.buy(s, plan, book, now)

    def _on_fill(self, s: Signal, avg_price: float, now: float) -> None:
        w = self.watch.get(s.token_id)
        if w is not None and w.first_fill_at is None:
            w.first_fill_at, w.first_avg = now, avg_price

    # -- positions -> sells ------------------------------------------------
    def _fetch_books(self, tokens: list[str]) -> dict[str, Book] | None:
        try:
            return fetch_books(self.session, sorted(set(tokens)), self.cfg.clob_host)
        except requests.RequestException as exc:
            log.warning("order book fetch failed: %s", exc)
            return None

    def _mark(self, p: Position, book: Book | None) -> None:
        """Remember the best bid; it values the position (and shows on the dashboard)."""
        if book is None:
            return
        if book.bids:
            p.bid, p.no_bid_count = book.best_bid, 0
            self.bank.marks[p.token_id] = min(self.cfg.sell_price, book.best_bid)
        else:
            p.bid = None
            p.no_bid_count += 1
            if p.no_bid_count >= 2:
                self.bank.marks[p.token_id] = 0.0

    def _update_open_value(self, now: float) -> None:
        active = [p for p in self.positions if p.status != "closed"]
        for token in [t for t in self.bank.marks if t not in {p.token_id for p in active}]:
            del self.bank.marks[token]
        if not active:
            self.bank.open_value = None
            return
        total = 0.0
        for p in active:
            mark = self.bank.marks.get(p.token_id)
            if mark is None or now - p.opened_at < self.cfg.confirm_seconds:
                mark = p.avg
            total += p.proceeds + p.remaining * mark
        self.bank.open_value = total

    @staticmethod
    def _signature(positions: list[Position]) -> list[tuple]:
        return [(id(p), p.status, p.sold_shares, p.shares, p.confirmed_at, p.unconfirmed_at, p.sell_order_id,
                 p.exit_reason, p.recheck) for p in positions]

    def tick(self, now: float) -> None:
        before = self._signature(self.positions)
        active = [p for p in self.positions if p.status != "closed"]
        due = [p for p in active if now >= p.sell_after and now >= p.next_poll]
        books = (self._fetch_books([p.token_id for p in due]) if due else None) or {}
        for p in due:
            book = books.get(p.token_id)
            self._mark(p, book)
            self._manage_safe(p, now, book)
        for p in active:
            if p.status != "closed":
                self._check_confirmation(p, now)
                if p.recheck == "failed" and p.exit_reason is None:
                    self._retry_recheck(p, now)
        self._update_open_value(now)
        # keep closed positions a day so paper mode still knows what it already bought
        self.positions = [p for p in self.positions if p.status != "closed" or now - p.opened_at < 86400]
        if before != self._signature(self.positions):
            self._persist()                 # partial sells, confirmations, new orders: survive a restart

    def _manage_safe(self, p: Position, now: float, book: Book | None) -> None:
        p.next_poll = now + (FAST_MANAGE_SECONDS if now - p.opened_at < FAST_PHASE_SECONDS
                             else self.cfg.recheck_every_seconds)
        p.last_poll = now
        try:
            self.manage(p, now, book)
            p.errors = 0
        except Exception as exc:  # noqa: BLE001 - one bad position must not stop the others
            # never give up on a position: try again later, slower each time, until it resolves
            p.errors += 1
            p.next_poll = now + backoff(p.errors)
            self.note_error(now, f"manage {p.token_id[:10]} ({p.errors}x)", exc)

    def _record_sale(self, p: Position, n: float, price: float, now: float, mode: str,
                     proceeds: float | None = None) -> None:
        if n <= 0:
            return
        fee = taker_fee_per_share(price, self.cfg.fee_rate) * n
        p.sold_shares += n
        p.proceeds += (n * price - fee) if proceeds is None else proceeds
        if price >= self.cfg.sell_price - CONFIRM_PRICE_SLACK:
            p.target_sold += n
        self.journal.write("sell", mode=mode, token_id=p.token_id, shares=round(n, 4), price=round(price, 4),
                           after_s=round(now - p.opened_at, 1), exit=bool(p.exit_reason))
        self._check_confirmation(p, now)

    def _check_confirmation(self, p: Position, now: float) -> None:
        if p.confirmed_at is not None:
            return
        if p.shares > 0 and p.target_sold >= CONFIRM_FRACTION * p.shares - 1e-9:
            p.confirmed_at = now
            if p.unconfirmed_at is not None:
                self.journal.write("confirmed", token_id=p.token_id, city=p.signal.city,
                                   bucket=p.signal.market.bucket.label, after_s=round(now - p.opened_at, 1))
            self._sync_event_cost()
            return
        if p.unconfirmed_at is None and now - p.opened_at > self.cfg.confirm_seconds:
            self._on_unconfirmed(p, now)

    def _on_unconfirmed(self, p: Position, now: float) -> None:
        """No 0.99 exit within CONFIRM_SECONDS: the main alarm."""
        s, m = p.signal, p.signal.market
        p.unconfirmed_at = now
        books = self._fetch_books([m.yes_token, m.no_token]) or {}
        own, yes = books.get(p.token_id), books.get(m.yes_token)
        self._mark(p, own)
        self.journal.write("unconfirmed", token_id=p.token_id, city=s.city, kind=s.kind, event=s.event_slug,
                           bucket=m.bucket.label, outcome=s.outcome, avg=round(p.avg, 4),
                           best_bid=own.best_bid if own else None, yes_bid=yes.best_bid if yes else None,
                           age_s=round(now - p.opened_at, 1), sold=round(p.sold_shares, 4), shares=p.shares)
        log.warning("UNCONFIRMED %s %s %s %s: no %.2f exit after %.0f s - market locked, re-checking the data",
                    s.city, s.kind, m.bucket.label, s.outcome, self.cfg.sell_price, now - p.opened_at)
        # (1) no more buys in this market for the rest of its day
        self.locked_events[s.event_slug] = max(self.locked_events.get(s.event_slug, 0.0), event_day_end(s))
        self.notifier.notify("unconfirmed", "Verkoop niet bevestigd",
                             f"{s.city} {KIND_NL.get(s.kind, s.kind)} {m.bucket.label} {s.outcome}: na "
                             f"{round(now - p.opened_at)} s nog niet verkocht voor {nl(self.cfg.sell_price)}. "
                             "Kopen in deze markt staat stil; de meting wordt opnieuw gecontroleerd.")
        # (3) check the weather data again (other source, corrections)
        p.next_poll = now
        self._run_recheck(p, now)
        self._persist()

    def _run_recheck(self, p: Position, now: float) -> None:
        if self.recheck_hook is None:
            return
        p.extra["recheck_tries"] = p.extra.get("recheck_tries", 0) + 1
        p.extra["recheck_at"] = now
        try:
            still_dead = self.recheck_hook(p)
        except Exception as exc:  # noqa: BLE001
            log.warning("data re-check failed: %s", exc)
            still_dead = None
        p.recheck = {True: "dead", False: "alive"}.get(still_dead, "failed")
        self.journal.write("recheck", token_id=p.token_id, city=p.signal.city,
                           bucket=p.signal.market.bucket.label, result=p.recheck)
        if still_dead is False:
            p.exit_reason = "data_recheck"
            log.warning("re-check: %s %s is not dead after all - selling now", p.signal.city,
                        p.signal.market.bucket.label)
            self.begin_exit(p, now)
            if p.status != "closed" and now >= p.sell_after:
                book = (self._fetch_books([p.token_id]) or {}).get(p.token_id)
                self._mark(p, book)
                self._manage_safe(p, now, book)     # sell right away, not on the next loop

    def _retry_recheck(self, p: Position, now: float) -> None:
        if p.extra.get("recheck_tries", 0) >= RECHECK_TRIES or now - p.extra.get("recheck_at", 0) < 60:
            return
        self._run_recheck(p, now)
        self._persist()

    def begin_exit(self, p: Position, now: float) -> None:
        """Prepare to sell at the best bid (live: frees the shares from the 0.99 order)."""
        p.next_poll = now

    def exit_floor(self, p: Position) -> float:
        return max(0.5 * p.avg, 0.01)

    def _close(self, p: Position, how: str, now: float) -> None:
        p.status = "closed"
        self.bank.on_close(p.cost, p.proceeds)
        pnl = p.proceeds - p.cost
        confirmed_after = None if p.confirmed_at is None else round(p.confirmed_at - p.opened_at, 1)
        self.journal.write("close", how=how, city=p.signal.city, kind=p.signal.kind, event=p.signal.event_slug,
                           bucket=p.signal.market.bucket.label, outcome=p.signal.outcome, token_id=p.token_id,
                           shares=p.shares, cost=round(p.cost, 4), proceeds=round(p.proceeds, 4),
                           pnl=round(pnl, 4), pct=round(pnl / p.cost * 100, 3) if p.cost else 0.0,
                           held_s=round(now - p.opened_at, 1), balance=round(self.bank.balance, 4),
                           avg_price=round(p.avg, 4), confirmed_after_s=confirmed_after)
        self._persist()
        log.info("CLOSE %s %s %s pnl=%.4f (%s)", p.signal.city, p.signal.market.bucket.label,
                 p.signal.outcome, pnl, how)
        if pnl < 0 or how == "resolved_loss":
            how_nl = {"resolved_loss": "uitslag verloren", "exit": "verkocht na controle"}.get(how, "verkocht")
            self.notifier.notify("loss", "Verlies op een trade",
                                 f"{p.signal.city} {p.signal.market.bucket.label} {p.signal.outcome}: "
                                 f"{'-' if pnl < 0 else ''}${nl(abs(pnl))} ({how_nl}).")

    def _settle_if_resolved(self, p: Position, now: float) -> bool:
        if now - p.last_settle_check < 300:
            return False
        p.last_settle_check = now
        res = fetch_resolution(self.session, p.signal.market.condition_id)
        if res is None:
            return False
        won = (res == 1.0) == (p.signal.outcome == "YES")
        p.proceeds += p.remaining * (1.0 if won else 0.0)
        p.sold_shares = p.shares
        self._close(p, "resolved_win" if won else "resolved_loss", now)
        return True

    def position_rows(self, now: float) -> list[dict]:
        return [position_row(p, now, self.cfg, p.signal.event_slug in self.locked_events)
                for p in self.positions if p.status != "closed"]

    def buy(self, s: Signal, plan: SweepPlan, book: Book, now: float) -> None:
        raise NotImplementedError

    def manage(self, p: Position, now: float, book: Book | None = None) -> None:
        raise NotImplementedError


class PaperExecutor(Executor):
    """Simulates fills against the real order book. Optimistic: in reality faster bots
    may take the cheap asks first, so treat paper profit as an upper bound."""

    allow_rebuy = False

    def buy(self, s: Signal, plan: SweepPlan, book: Book, now: float) -> None:
        ok, why = self.can_buy()          # the pause may have started a moment ago
        if not ok:
            self._skip(s, why, now)
            return
        fee = taker_fee_per_share(plan.avg_price, self.cfg.fee_rate) * plan.shares
        cost = plan.cost + fee
        p = Position(signal=s, shares=plan.shares, cost=cost, opened_at=now,
                     sell_after=now + self.cfg.sell_delay_seconds, avg_price=plan.avg_price)
        self.positions.append(p)
        self.bank.on_buy(s.token_id, cost, s.event_slug)
        self._on_fill(s, plan.avg_price, now)
        self.journal.write("buy", mode="paper", city=s.city, kind=s.kind, event=s.event_slug,
                           bucket=s.market.bucket.label, outcome=s.outcome, token_id=s.token_id,
                           shares=plan.shares, cost=round(cost, 4), avg_price=round(plan.avg_price, 4))
        self._persist()
        log.info("PAPER BUY %s %s %s %.2f sh @ %.3f", s.city, s.market.bucket.label, s.outcome,
                 plan.shares, plan.avg_price)

    def manage(self, p: Position, now: float, book: Book | None = None) -> None:
        p.sell_attempts += 1
        if book is not None:
            if p.exit_reason:
                # the data said the bucket is alive: sell at the best bids, never below the floor
                floor = self.exit_floor(p)
                for lvl in book.bids:
                    if lvl.price < floor or p.remaining < 0.01:
                        break
                    self._record_sale(p, min(lvl.size, p.remaining), lvl.price, now, "paper")
            else:
                n = fillable_at(book, self.cfg.sell_price, p.remaining)
                if n > 0:
                    self._record_sale(p, n, self.cfg.sell_price, now, "paper")
            if p.remaining < 0.01:
                self._close(p, "exit" if p.exit_reason else "sold", now)
                return
        self._settle_if_resolved(p, now)


class LiveExecutor(Executor):
    """Real orders through Polymarket's official py-clob-client."""

    def __init__(self, cfg: Config, session: requests.Session, journal: Journal,
                 bankroll: Bankroll | None = None, notifier: Notifier | None = None) -> None:
        super().__init__(cfg, session, journal, bankroll, notifier)
        from py_clob_client.client import ClobClient  # imported here so paper mode doesn't need it
        from py_clob_client import clob_types
        from py_clob_client.order_builder.constants import BUY, SELL

        if not cfg.private_key or not cfg.funder:
            raise SystemExit("MODE=live needs PRIVATE_KEY and FUNDER in .env")
        self.t = clob_types
        self.BUY, self.SELL = BUY, SELL
        self.client = ClobClient(cfg.clob_host, chain_id=137, key=cfg.private_key,
                                 signature_type=cfg.signature_type, funder=cfg.funder)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        self._last_wallet = 0.0

    def refresh(self, now: float) -> None:
        if now - self._last_wallet >= 60:
            self._last_wallet = now
            try:
                self.bank.wallet_usdc = self.usdc_balance()
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read USDC balance: %s", exc)

    def _opts(self, s: Signal):
        return self.t.PartialCreateOrderOptions(tick_size=s.market.tick_size, neg_risk=s.market.neg_risk)

    def usdc_balance(self) -> float:
        r = self.client.get_balance_allowance(self.t.BalanceAllowanceParams(
            asset_type=self.t.AssetType.COLLATERAL, signature_type=self.cfg.signature_type))
        return int(r.get("balance", 0)) / 1e6

    def token_balance(self, token_id: str) -> float:
        r = self.client.get_balance_allowance(self.t.BalanceAllowanceParams(
            asset_type=self.t.AssetType.CONDITIONAL, token_id=token_id, signature_type=self.cfg.signature_type))
        return int(r.get("balance", 0)) / 1e6

    def buy(self, s: Signal, plan: SweepPlan, book: Book, now: float) -> None:
        amount = math.floor(plan.cost * 100) / 100
        if amount < 1.0:
            return
        ok, why = self.can_buy()          # last check right before the order goes out
        if not ok:
            self._skip(s, why, now)
            return
        args = self.t.MarketOrderArgs(token_id=s.token_id, amount=amount, side=self.BUY,
                                      price=plan.worst_price, order_type=self.t.OrderType.FAK)
        try:
            order = self.client.create_market_order(args, self._opts(s))
            resp = self.client.post_order(order, self.t.OrderType.FAK)
        except Exception as exc:  # noqa: BLE001 - API errors come back as plain exceptions
            self.journal.write("buy_error", token_id=s.token_id, error=str(exc))
            self.note_error(now, "buy", exc)
            return
        spent = float(resp.get("makingAmount") or 0)
        shares = float(resp.get("takingAmount") or 0)
        self.journal.write("buy", mode="live", city=s.city, kind=s.kind, event=s.event_slug,
                           bucket=s.market.bucket.label, outcome=s.outcome, token_id=s.token_id, shares=shares,
                           cost=spent, avg_price=round(spent / shares, 4) if shares else None, response=resp)
        if not resp.get("success") or shares <= 0:
            log.info("LIVE BUY not filled: %s", resp)
            self._skip(s, "fak_unfilled", now)
            return
        self.positions.append(Position(signal=s, shares=shares, cost=spent, opened_at=now,
                                       sell_after=now + self.cfg.sell_delay_seconds, avg_price=spent / shares))
        self.bank.on_buy(s.token_id, spent, s.event_slug)
        self._on_fill(s, spent / shares, now)
        self._persist()
        log.info("LIVE BUY %s %s %s %.2f sh for %.2f USDC", s.city, s.market.bucket.label, s.outcome, shares, spent)

    def _post_sell(self, p: Position, size: float, now: float, repost: bool = False) -> None:
        order = self.client.create_order(
            self.t.OrderArgs(token_id=p.token_id, price=self.cfg.sell_price, size=size, side=self.SELL),
            self._opts(p.signal))
        resp = self.client.post_order(order, self.t.OrderType.GTC) or {}
        self.journal.write("sell_order", mode="live", token_id=p.token_id, size=size, price=self.cfg.sell_price,
                           response=resp, after_s=round(now - p.opened_at, 1), repost=repost)
        if not resp.get("orderID"):
            raise RuntimeError(f"sell order not accepted: {resp}")
        p.sell_order_id, p.order_matched, p.status = resp["orderID"], 0.0, "selling"
        self._persist()

    def _sync_order(self, p: Position, now: float) -> dict:
        """Count new fills of our 0.99 order. Returns the order as the API gives it."""
        o = self.client.get_order(p.sell_order_id) or {}
        matched = float(o.get("size_matched") or 0)
        if matched > p.order_matched:
            self._record_sale(p, matched - p.order_matched, self.cfg.sell_price, now, "live")
            p.order_matched = matched
        return o

    def _wallet_size(self, p: Position) -> float:
        have = self.token_balance(p.token_id)
        return math.floor(min(have, p.remaining) * 100) / 100

    def manage(self, p: Position, now: float, book: Book | None = None) -> None:
        if p.exit_reason:
            self._exit_now(p, now, book)
        elif p.status == "open":
            # shares arrive after on-chain settlement (that's the 4-6 s HighTempTation waits)
            size = self._wallet_size(p)
            if size < p.signal.market.min_size:
                p.sell_attempts += 1
                if now - p.opened_at >= FAST_PHASE_SECONDS:
                    p.next_poll = now + backoff(p.sell_attempts)
            else:
                p.shares = round(p.sold_shares + size, 4)
                self._post_sell(p, size, now)
                return
        elif p.sell_order_id:
            o = self._sync_order(p, now)
            if p.remaining < 0.01:
                self._close(p, "sold", now)
                return
            if str(o.get("status") or "").upper().startswith("CANCEL"):
                # our exit was cancelled (not by us): put the 0.99 ask back
                log.warning("sell order %s was cancelled - posting it again", p.sell_order_id)
                self._repost(p, now)
        else:
            self._repost(p, now)            # 'selling' without an order: post it (again)
        if p.remaining < 0.01:
            self._close(p, "exit" if p.exit_reason else "sold", now)
            return
        # Unsold leftovers resolve on their own; claim winnings in the Polymarket app.
        self._settle_if_resolved(p, now)

    def _repost(self, p: Position, now: float) -> None:
        size = self._wallet_size(p)
        if size < p.signal.market.min_size:
            p.sell_attempts += 1
            p.next_poll = now + backoff(p.sell_attempts)
            return
        self._post_sell(p, size, now, repost=True)

    def begin_exit(self, p: Position, now: float) -> None:
        # The only time the 0.99 order is cancelled: the data re-check says the bucket is alive,
        # so the shares are sold now instead (never on a price drop, never on Noodstop).
        if p.sell_order_id:
            try:
                self._sync_order(p, now)
                if p.remaining < 0.01:
                    return                  # the 0.99 order filled after all: nothing to cancel
                self.client.cancel(p.sell_order_id)
                self.journal.write("sell_cancel", token_id=p.token_id, order_id=p.sell_order_id,
                                   reason=p.exit_reason)
                p.sell_order_id = None
            except Exception as exc:  # noqa: BLE001 - try again on the next look
                self.note_error(now, "cancel", exc)
        p.next_poll = now

    def _exit_now(self, p: Position, now: float, book: Book | None) -> None:
        if p.sell_order_id:
            self.begin_exit(p, now)
            if p.sell_order_id:
                return
        if book is None or book.best_bid is None or book.best_bid < self.exit_floor(p):
            return                          # nothing worth selling into: hold to resolution
        size = self._wallet_size(p)
        if size <= 0:
            return
        args = self.t.MarketOrderArgs(token_id=p.token_id, amount=size, side=self.SELL,
                                      price=book.best_bid, order_type=self.t.OrderType.FAK)
        order = self.client.create_market_order(args, self._opts(p.signal))
        resp = self.client.post_order(order, self.t.OrderType.FAK) or {}
        sold = float(resp.get("makingAmount") or 0)
        got = float(resp.get("takingAmount") or 0)
        self.journal.write("sell_order", mode="live", token_id=p.token_id, size=size, price=book.best_bid,
                           response=resp, after_s=round(now - p.opened_at, 1), exit=True)
        if resp.get("success") and sold > 0:
            self._record_sale(p, sold, got / sold, now, "live", proceeds=got)
