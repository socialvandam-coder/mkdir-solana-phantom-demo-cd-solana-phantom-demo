"""Order books and the "sweep the stale asks" calculation."""
from __future__ import annotations

import math
from dataclasses import dataclass

import requests

CLOB = "https://clob.polymarket.com"


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    token_id: str
    bids: list[Level]   # best (highest) first
    asks: list[Level]   # best (lowest) first

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None


def _book_from_json(d: dict) -> Book:
    bids = sorted((Level(float(x["price"]), float(x["size"])) for x in d.get("bids", [])), key=lambda l: -l.price)
    asks = sorted((Level(float(x["price"]), float(x["size"])) for x in d.get("asks", [])), key=lambda l: l.price)
    return Book(token_id=str(d.get("asset_id")), bids=bids, asks=asks)


def fetch_books(session: requests.Session, token_ids: list[str], host: str = CLOB) -> dict[str, Book]:
    """Batch-fetch order books (the CLOB accepts many token ids in one POST)."""
    out: dict[str, Book] = {}
    for i in range(0, len(token_ids), 100):
        chunk = token_ids[i:i + 100]
        r = session.post(f"{host}/books", json=[{"token_id": t} for t in chunk], timeout=15)
        r.raise_for_status()
        for d in r.json() or []:
            b = _book_from_json(d)
            out[b.token_id] = b
    return out


def taker_fee_per_share(price: float, fee_rate: float) -> float:
    """Rough estimate of the weather-market taker fee, in USDC per share."""
    return fee_rate * min(price, 1.0 - price)


@dataclass(frozen=True)
class SweepPlan:
    shares: float
    cost: float          # USDC
    avg_price: float
    worst_price: float   # limit price for the order
    est_profit: float    # if resold at sell_price, after estimated fees


def plan_sweep(book: Book, *, max_price: float, sell_price: float, budget_usdc: float,
               min_size: float, min_edge: float, fee_rate: float) -> tuple[SweepPlan | None, str | None]:
    """Take every ask that still leaves `min_edge` profit per share after fees.

    Returns (plan, None), or (None, why) with why one of:
      'no_asks'  - nobody is selling at all
      'price'    - every ask is above max_price (the cheap ones are gone)
      'edge'     - the asks under max_price leave too little profit after fees
      'min_size' - fewer cheap shares left than the market's minimum order
      'budget'   - the money we may spend buys fewer shares than the minimum order
    """
    if not book.asks:
        return None, "no_asks"
    sell_fee = taker_fee_per_share(sell_price, fee_rate)
    shares = cost = profit = 0.0
    worst = 0.0
    stop = "exhausted"
    for lvl in book.asks:
        if lvl.price > max_price:
            stop = "price"
            break
        edge = sell_price - lvl.price - taker_fee_per_share(lvl.price, fee_rate) - sell_fee
        if edge < min_edge:
            stop = "edge"
            break
        remaining = budget_usdc - cost
        if remaining <= 0:
            stop = "budget"
            break
        take = min(lvl.size, math.floor(remaining / lvl.price * 100) / 100)
        if take <= 0:
            stop = "budget"
            break
        shares += take
        cost += take * lvl.price
        profit += take * edge
        worst = lvl.price
        if take < lvl.size:
            stop = "budget"            # the money ran out inside this level
    if shares < min_size or cost <= 0:
        if shares <= 0 and stop in ("price", "edge"):
            return None, stop
        return None, "budget" if stop == "budget" else "min_size"
    return SweepPlan(shares=round(shares, 2), cost=round(cost, 4), avg_price=cost / shares,
                     worst_price=worst, est_profit=round(profit, 4)), None


def fillable_at(book: Book, price: float, shares: float) -> float:
    """How many of `shares` could be sold right now at `price` or better."""
    total = 0.0
    for lvl in book.bids:
        if lvl.price < price:
            break
        total += lvl.size
        if total >= shares:
            return shares
    return total
