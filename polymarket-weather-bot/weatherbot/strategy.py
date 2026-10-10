"""The core rule, copied from HighTempTation.

- Highest-temperature market: the day's maximum can only go up. As soon as an
  observation is above a bucket, that bucket can no longer win -> buy NO.
  If the bucket is "X or higher" and X has been reached -> YES is certain.
- Lowest-temperature market: mirror image. The minimum can only go down.

Nothing here is a forecast: a signal is only produced when the observed
reading already rules the bucket out.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .markets import Market, TempEvent
from .metar import Observation, displayed_bounds


@dataclass(frozen=True)
class Signal:
    event_slug: str
    city: str
    kind: str            # "high" / "low"
    market: Market
    outcome: str         # "NO" or "YES"
    token_id: str
    observed: int        # guaranteed running max (high) or min (low), in the market's unit
    obs_time: datetime   # time of the report that produced it
    reason: str


def day_observations(event: TempEvent, observations: list[Observation]) -> list[Observation]:
    tz = ZoneInfo(event.tz)
    return [o for o in observations
            if o.station == event.station and o.time.astimezone(tz).date() == event.day]


def guaranteed_extreme(event: TempEvent, observations: list[Observation]) -> tuple[int, datetime] | None:
    """For highs: the official max is at least this. For lows: the official min is at most this."""
    obs = day_observations(event, observations)
    if not obs:
        return None
    if event.kind == "high":
        best = max(obs, key=lambda o: (displayed_bounds(o, event.unit)[0], -o.time.timestamp()))
        return displayed_bounds(best, event.unit)[0], best.time
    best = min(obs, key=lambda o: (displayed_bounds(o, event.unit)[1], o.time.timestamp()))
    return displayed_bounds(best, event.unit)[1], best.time


def find_signals(event: TempEvent, observations: list[Observation],
                 safety_margin: int = 0, enable_yes: bool = True) -> list[Signal]:
    ext = guaranteed_extreme(event, observations)
    if ext is None:
        return []
    g, t = ext
    out: list[Signal] = []
    for mk in event.markets:
        if not mk.accepting_orders:
            continue
        b = mk.bucket
        if event.kind == "high":
            if not b.open_high and b.hi < g - safety_margin:
                out.append(Signal(event.slug, event.city, event.kind, mk, "NO", mk.no_token, g, t,
                                  f"max already {g}°{event.unit} > bucket {b.label}"))
            elif enable_yes and b.open_high and g >= b.lo + safety_margin:
                out.append(Signal(event.slug, event.city, event.kind, mk, "YES", mk.yes_token, g, t,
                                  f"max already {g}°{event.unit} >= {b.label}"))
        else:
            if not b.open_low and b.lo > g + safety_margin:
                out.append(Signal(event.slug, event.city, event.kind, mk, "NO", mk.no_token, g, t,
                                  f"min already {g}°{event.unit} < bucket {b.label}"))
            elif enable_yes and b.open_low and g <= b.hi - safety_margin:
                out.append(Signal(event.slug, event.city, event.kind, mk, "YES", mk.yes_token, g, t,
                                  f"min already {g}°{event.unit} <= {b.label}"))
    return out
