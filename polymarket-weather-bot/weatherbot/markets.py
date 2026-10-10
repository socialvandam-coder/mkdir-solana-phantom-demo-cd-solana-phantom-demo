"""Discover the daily "Highest/Lowest temperature in <city> on <date>" markets."""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from .cities import CITY_TZ

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
TAG_DAILY_TEMPERATURE = 103040  # gamma tag "daily-temperature"

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}

_BUCKET_RE = re.compile(
    r"^\s*(?:between\s+)?(-?\d+)(?:\s*-\s*(-?\d+))?\s*°\s*([CF])(?:\s+or\s+(below|higher))?\s*$", re.I)
_TITLE_RE = re.compile(r"^(Highest|Lowest) temperature in (.+?) on ([A-Za-z]+) (\d{1,2})\??$")
_STATION_RES = (
    re.compile(r"timeseries\?site=([A-Za-z0-9]{3,5})"),
    re.compile(r"wunderground\.com/history/daily/\S*?/([A-Z0-9]{4})(?=[/?.\s]|$)"),
)


@dataclass(frozen=True)
class Bucket:
    lo: float          # -inf for "X or below"
    hi: float          # +inf for "X or higher"
    unit: str          # "C" or "F"
    label: str

    @property
    def open_low(self) -> bool:
        return math.isinf(self.lo)

    @property
    def open_high(self) -> bool:
        return math.isinf(self.hi)


def parse_bucket(label: str) -> Bucket | None:
    m = _BUCKET_RE.match(label or "")
    if not m:
        return None
    a, b, unit, tail = m.group(1), m.group(2), m.group(3).upper(), (m.group(4) or "").lower()
    lo = float(a)
    hi = float(b) if b is not None else lo
    if tail == "below":
        lo = -math.inf
    elif tail == "higher":
        hi = math.inf
    return Bucket(lo=lo, hi=hi, unit=unit, label=label.strip())


def parse_station(*texts: str) -> str | None:
    for text in texts:
        for rx in _STATION_RES:
            m = rx.search(text or "")
            if m:
                return m.group(1).upper()
    return None


@dataclass
class Market:
    question: str
    condition_id: str
    yes_token: str
    no_token: str
    bucket: Bucket
    tick_size: str = "0.001"
    min_size: float = 5.0
    neg_risk: bool = True
    accepting_orders: bool = True


@dataclass
class TempEvent:
    slug: str
    title: str
    kind: str          # "high" or "low"
    city: str
    station: str
    unit: str
    day: date
    tz: str
    markets: list[Market] = field(default_factory=list)

    def local_now(self, now_utc: datetime) -> datetime:
        return now_utc.astimezone(ZoneInfo(self.tz))

    def is_live(self, now_utc: datetime) -> bool:
        """True while it is still (or already) the market's day at the station."""
        return self.local_now(now_utc).date() == self.day


def _tick(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0.001"
    for t in ("0.1", "0.01", "0.001", "0.0001"):
        if abs(v - float(t)) < 1e-9:
            return t
    return "0.001"


def parse_event(ev: dict) -> TempEvent | None:
    m = _TITLE_RE.match(ev.get("title", "").strip())
    if not m:
        return None
    kind = "high" if m.group(1) == "Highest" else "low"
    city = m.group(2)
    month = MONTHS.get(m.group(3).lower())
    if not month:
        return None
    ym = re.search(r"-(\d{4})$", ev.get("slug", ""))
    year = int(ym.group(1)) if ym else int((ev.get("endDate") or "1970")[:4])
    try:
        day = date(year, month, int(m.group(4)))
    except ValueError:
        return None

    tz = CITY_TZ.get(city)
    if not tz:
        log.warning("unknown city %r (no timezone) - skipping %s", city, ev.get("slug"))
        return None

    markets_raw = ev.get("markets") or []
    first_desc = markets_raw[0].get("description", "") if markets_raw else ""
    station = parse_station(ev.get("resolutionSource") or "",
                            markets_raw[0].get("resolutionSource", "") if markets_raw else "",
                            first_desc)
    if not station:
        # e.g. Hong Kong resolves on the Hong Kong Observatory, not on an airport METAR
        log.debug("no METAR station for %s - skipping", ev.get("slug"))
        return None

    markets: list[Market] = []
    unit = None
    for mk in markets_raw:
        if mk.get("closed"):
            continue
        bucket = parse_bucket(mk.get("groupItemTitle", ""))
        if not bucket:
            continue
        try:
            tokens = json.loads(mk["clobTokenIds"]) if isinstance(mk.get("clobTokenIds"), str) else mk["clobTokenIds"]
            outcomes = json.loads(mk["outcomes"]) if isinstance(mk.get("outcomes"), str) else mk["outcomes"]
        except (KeyError, TypeError, ValueError):
            continue
        if len(tokens) != 2 or [o.lower() for o in outcomes] != ["yes", "no"]:
            continue
        unit = unit or bucket.unit
        markets.append(Market(
            question=mk.get("question", ""),
            condition_id=mk.get("conditionId", ""),
            yes_token=str(tokens[0]),
            no_token=str(tokens[1]),
            bucket=bucket,
            tick_size=_tick(mk.get("orderPriceMinTickSize")),
            min_size=float(mk.get("orderMinSize") or 5),
            neg_risk=bool(mk.get("negRisk", ev.get("negRisk", True))),
            accepting_orders=bool(mk.get("acceptingOrders", True)),
        ))
    if not markets:
        return None
    return TempEvent(slug=ev.get("slug", ""), title=ev.get("title", ""), kind=kind, city=city,
                     station=station, unit=unit or "C", day=day, tz=tz, markets=markets)


def fetch_events(session: requests.Session, now_utc: datetime, cities: tuple[str, ...] = ()) -> list[TempEvent]:
    """All open temperature events whose day is 'today' somewhere in the world right now."""
    raw: list[dict] = []
    offset = 0
    while True:
        r = session.get(f"{GAMMA}/events", params={
            "tag_id": TAG_DAILY_TEMPERATURE, "closed": "false", "limit": 100, "offset": offset}, timeout=30)
        r.raise_for_status()
        page = r.json()
        raw.extend(page)
        if len(page) < 100:
            break
        offset += 100

    events = []
    for ev in raw:
        te = parse_event(ev)
        if not te:
            continue
        if cities and te.city not in cities:
            continue
        if te.is_live(now_utc):
            events.append(te)
    return events


def fetch_resolution(session: requests.Session, condition_id: str) -> float | None:
    """1.0 if YES won, 0.0 if NO won, None if not resolved yet."""
    r = session.get(f"{GAMMA}/markets", params={"condition_ids": condition_id}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    mk = data[0]
    if not mk.get("closed"):
        return None
    prices = mk.get("outcomePrices")
    prices = json.loads(prices) if isinstance(prices, str) else prices
    if not prices:
        return None
    yes = float(prices[0])
    if yes >= 0.99:
        return 1.0
    if yes <= 0.01:
        return 0.0
    return None
