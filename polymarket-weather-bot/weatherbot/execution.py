"""Buying the dead bucket and reselling it at 0.99 - simulated (paper) or for real (live)."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime

import requests

from .bankroll import Bankroll
from .config import Config
from .journal import Journal
from .markets import Market, fetch_resolution, parse_bucket
from .orderbook import Book, SweepPlan, fetch_books, fillable_at, plan_sweep, taker_fee_per_share
from .strategy import Signal

log = logging.getLogger(__name__)


@dataclass
class Watch:
    signal: Signal
    first_seen: float
    last_check: float = 0.0


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
    extra: dict = field(default_factory=dict)

    @property
    def token_id(self) -> str:
        return self.signal.token_id

    @property
    def remaining(self) -> float:
        return round(self.shares - self.sold_shares, 4)

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
            return cls(signal=signal, shares=float(d["shares"]), cost=float(d["cost"]),
                       opened_at=float(d["opened_at"]), sell_after=float(d["sell_after"]),
                       sold_shares=float(d.get("sold_shares", 0)), proceeds=float(d.get("proceeds", 0)),
                       status=d.get("status", "open"), sell_order_id=d.get("sell_order_id"),
                       sell_attempts=int(d.get("sell_attempts", 0)))
        except (KeyError, TypeError, ValueError):
            return None


class Executor:
    """Shared logic; subclasses implement the actual buy/sell."""

    # Paper fills don't remove the asks from the real book, so buying the same token again
    # would count the same stale orders twice. Live re-buys are real new asks (he did up to 3).
    allow_rebuy = True

    def __init__(self, cfg: Config, session: requests.Session, journal: Journal,
                 bankroll: Bankroll | None = None) -> None:
        self.cfg = cfg
        self.session = session
        self.journal = journal
        self.bank = bankroll or Bankroll(cfg)
        self.watch: dict[str, Watch] = {}
        self.positions: list[Position] = []
        # pick up positions that were still open when the app last stopped
        for d in self.bank.state.get("positions", []):
            p = Position.from_dict(d)
            if p and p.status != "closed":
                self.positions.append(p)
                self.bank.open_cost += p.cost
        if self.positions:
            log.info("resumed %d open position(s) from the last run", len(self.positions))

    def _persist(self) -> None:
        self.bank.set_positions([p.to_dict() for p in self.positions if p.status != "closed"])

    def refresh(self, now: float) -> None:
        """Called every loop; live mode uses it to keep the real wallet balance in view."""

    # -- signals -> buys -------------------------------------------------
    def on_signals(self, signals: list[Signal], now: float) -> None:
        for token in [t for t, w in self.watch.items() if now - w.first_seen > 2 * 86400]:
            del self.watch[token]
        due: list[Signal] = []
        for s in signals:
            w = self.watch.get(s.token_id)
            if w is None:
                w = self.watch[s.token_id] = Watch(signal=s, first_seen=now)
                self.journal.write("signal", city=s.city, kind=s.kind, event=s.event_slug,
                                   bucket=s.market.bucket.label, outcome=s.outcome, token_id=s.token_id,
                                   condition_id=s.market.condition_id, observed=s.observed,
                                   obs_time=s.obs_time.isoformat(), lag_s=round(now - s.obs_time.timestamp(), 1),
                                   reason=s.reason)
                log.info("SIGNAL %s %s %s -> %s (%s)", s.city, s.kind, s.market.bucket.label, s.outcome, s.reason)
            if now - w.first_seen > self.cfg.recheck_window_seconds:
                continue
            if w.last_check and now - w.last_check < self.cfg.recheck_every_seconds:
                continue
            if self.bank.budget(s.token_id) < 1.0:
                continue
            if not self.allow_rebuy and any(p.token_id == s.token_id for p in self.positions):
                continue
            due.append(s)
        if not due:
            return
        try:
            books = fetch_books(self.session, [s.token_id for s in due], self.cfg.clob_host)
        except requests.RequestException as exc:
            log.warning("order book fetch failed: %s", exc)
            return
        for s in due:
            self.watch[s.token_id].last_check = now
            book = books.get(s.token_id)
            if not book:
                continue
            plan = plan_sweep(book, max_price=self.cfg.max_buy_price, sell_price=self.cfg.sell_price,
                              budget_usdc=self.bank.budget(s.token_id), min_size=s.market.min_size,
                              min_edge=self.cfg.min_edge, fee_rate=self.cfg.fee_rate)
            if plan is None:
                continue
            self.journal.write("opportunity", city=s.city, bucket=s.market.bucket.label, outcome=s.outcome,
                               token_id=s.token_id, best_ask=book.best_ask, shares=plan.shares,
                               cost=plan.cost, avg_price=round(plan.avg_price, 4), worst_price=plan.worst_price,
                               est_profit=plan.est_profit, since_signal_s=round(now - self.watch[s.token_id].first_seen, 1))
            self.buy(s, plan, book, now)

    # -- positions -> sells ------------------------------------------------
    def tick(self, now: float) -> None:
        for p in self.positions:
            if p.status == "closed" or now < p.sell_after:
                continue
            try:
                self.manage(p, now)
            except Exception as exc:  # noqa: BLE001 - one bad position must not stop the others
                p.errors += 1
                log.warning("manage position %s failed (%d): %s", p.token_id[:10], p.errors, exc)
                if p.errors >= 10 and p.status != "closed":
                    p.status, p.sell_order_id = "selling", None   # stop trying to sell; wait for resolution
        # keep closed positions a day so paper mode still knows what it already bought
        before = [(p.status, p.sold_shares) for p in self.positions]
        self.positions = [p for p in self.positions if p.status != "closed" or now - p.opened_at < 86400]
        if before != [(p.status, p.sold_shares) for p in self.positions]:
            self._persist()

    def _close(self, p: Position, how: str, now: float) -> None:
        p.status = "closed"
        self.bank.on_close(p.cost, p.proceeds)
        pnl = p.proceeds - p.cost
        self.journal.write("close", how=how, city=p.signal.city, kind=p.signal.kind,
                           bucket=p.signal.market.bucket.label, outcome=p.signal.outcome, token_id=p.token_id,
                           shares=p.shares, cost=round(p.cost, 4), proceeds=round(p.proceeds, 4),
                           pnl=round(pnl, 4), pct=round(pnl / p.cost * 100, 3) if p.cost else 0.0,
                           held_s=round(now - p.opened_at, 1), balance=round(self.bank.balance, 4))
        self._persist()
        log.info("CLOSE %s %s %s pnl=%.4f (%s)", p.signal.city, p.signal.market.bucket.label,
                 p.signal.outcome, p.proceeds - p.cost, how)

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

    def buy(self, s: Signal, plan: SweepPlan, book: Book, now: float) -> None:
        raise NotImplementedError

    def manage(self, p: Position, now: float) -> None:
        raise NotImplementedError


class PaperExecutor(Executor):
    """Simulates fills against the real order book. Optimistic: in reality faster bots
    may take the cheap asks first, so treat paper profit as an upper bound."""

    allow_rebuy = False

    def buy(self, s: Signal, plan: SweepPlan, book: Book, now: float) -> None:
        fee = taker_fee_per_share(plan.avg_price, self.cfg.fee_rate) * plan.shares
        cost = plan.cost + fee
        p = Position(signal=s, shares=plan.shares, cost=cost, opened_at=now,
                     sell_after=now + self.cfg.sell_delay_seconds)
        self.positions.append(p)
        self.bank.on_buy(s.token_id, cost)
        self.journal.write("buy", mode="paper", city=s.city, kind=s.kind, bucket=s.market.bucket.label,
                           outcome=s.outcome, token_id=s.token_id, shares=plan.shares, cost=round(cost, 4),
                           avg_price=round(plan.avg_price, 4))
        self._persist()
        log.info("PAPER BUY %s %s %s %.2f sh @ %.3f", s.city, s.market.bucket.label, s.outcome,
                 plan.shares, plan.avg_price)

    def manage(self, p: Position, now: float) -> None:
        if p.sell_attempts == 0 or now - p.last_poll >= self.cfg.recheck_every_seconds:
            p.sell_attempts += 1
            p.last_poll = now
            book = fetch_books(self.session, [p.token_id], self.cfg.clob_host).get(p.token_id)
            if book:
                n = fillable_at(book, self.cfg.sell_price, p.remaining)
                if n > 0:
                    fee = taker_fee_per_share(self.cfg.sell_price, self.cfg.fee_rate) * n
                    p.sold_shares += n
                    p.proceeds += n * self.cfg.sell_price - fee
                    self.journal.write("sell", mode="paper", token_id=p.token_id, shares=n,
                                       price=self.cfg.sell_price, after_s=round(now - p.opened_at, 1))
            if p.remaining < 0.01:
                self._close(p, "sold", now)
                return
        self._settle_if_resolved(p, now)


class LiveExecutor(Executor):
    """Real orders through Polymarket's official py-clob-client."""

    def __init__(self, cfg: Config, session: requests.Session, journal: Journal,
                 bankroll: Bankroll | None = None) -> None:
        super().__init__(cfg, session, journal, bankroll)
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
        args = self.t.MarketOrderArgs(token_id=s.token_id, amount=amount, side=self.BUY,
                                      price=plan.worst_price, order_type=self.t.OrderType.FAK)
        try:
            order = self.client.create_market_order(args, self._opts(s))
            resp = self.client.post_order(order, self.t.OrderType.FAK)
        except Exception as exc:  # noqa: BLE001 - API errors come back as plain exceptions
            self.journal.write("buy_error", token_id=s.token_id, error=str(exc))
            log.warning("buy failed: %s", exc)
            return
        spent = float(resp.get("makingAmount") or 0)
        shares = float(resp.get("takingAmount") or 0)
        self.journal.write("buy", mode="live", city=s.city, kind=s.kind, bucket=s.market.bucket.label,
                           outcome=s.outcome, token_id=s.token_id, shares=shares, cost=spent,
                           avg_price=round(spent / shares, 4) if shares else None, response=resp)
        if not resp.get("success") or shares <= 0:
            log.info("LIVE BUY not filled: %s", resp)
            return
        self.positions.append(Position(signal=s, shares=shares, cost=spent, opened_at=now,
                                       sell_after=now + self.cfg.sell_delay_seconds))
        self.bank.on_buy(s.token_id, spent)
        self._persist()
        log.info("LIVE BUY %s %s %s %.2f sh for %.2f USDC", s.city, s.market.bucket.label, s.outcome, shares, spent)

    def manage(self, p: Position, now: float) -> None:
        if p.status == "open":
            # shares arrive after on-chain settlement (that's the 4-6 s HighTempTation waits)
            have = self.token_balance(p.token_id)
            size = math.floor(min(have, p.shares) * 100) / 100
            if size < p.signal.market.min_size:
                p.sell_attempts += 1
                if p.sell_attempts > 30:
                    log.info("could not sell %s (balance %.2f) - holding to resolution", p.token_id, have)
                    p.status = "selling"
                return
            p.shares = size
            order = self.client.create_order(
                self.t.OrderArgs(token_id=p.token_id, price=self.cfg.sell_price, size=size, side=self.SELL),
                self._opts(p.signal))
            resp = self.client.post_order(order, self.t.OrderType.GTC)
            p.sell_order_id = resp.get("orderID")
            p.status = "selling"
            self._persist()
            self.journal.write("sell_order", mode="live", token_id=p.token_id, size=size,
                               price=self.cfg.sell_price, response=resp, after_s=round(now - p.opened_at, 1))
            return
        if p.sell_order_id and now - p.last_poll >= self.cfg.recheck_every_seconds:
            p.last_poll = now
            o = self.client.get_order(p.sell_order_id) or {}
            matched = float(o.get("size_matched") or 0)
            if matched > p.sold_shares:
                fee = taker_fee_per_share(self.cfg.sell_price, self.cfg.fee_rate) * (matched - p.sold_shares)
                p.proceeds += (matched - p.sold_shares) * self.cfg.sell_price - fee
                p.sold_shares = matched
            if p.remaining < 0.01:
                self._close(p, "sold", now)
                return
        # Unsold leftovers resolve on their own; claim winnings in the Polymarket app.
        self._settle_if_resolved(p, now)
