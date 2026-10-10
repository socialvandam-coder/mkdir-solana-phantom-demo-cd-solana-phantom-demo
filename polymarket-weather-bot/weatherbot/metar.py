"""Live airport observations (METAR) and what they guarantee about the official reading.

The markets resolve on NOAA's timeseries page for one airport station, which is built
from that station's METAR/SPECI reports. Every new report can make a bucket impossible.
"""
from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

_TEMP_RE = re.compile(r"^(M?\d{2})/(M?\d{2}|//)?$")
_TGROUP_RE = re.compile(r"\bT([01])(\d{3})([01])(\d{3}|////)?\b")
_TIME_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})Z$")


@dataclass(frozen=True)
class Observation:
    station: str
    time: datetime              # UTC
    temp_c: int                 # whole degrees from the main METAR body
    temp_c_tenths: float | None  # from the "T" remark group (mostly US stations)
    raw: str


def _signed(v: str) -> int:
    return -int(v[1:]) if v.startswith("M") else int(v)


def _resolve_time(dd: int, hh: int, mm: int, ref: datetime) -> datetime | None:
    """METAR only carries day-of-month; pick the matching moment closest before `ref`."""
    for months_back in range(0, 2):
        y, m = ref.year, ref.month - months_back
        if m <= 0:
            y, m = y - 1, m + 12
        try:
            t = datetime(y, m, dd, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            continue
        if t <= ref + timedelta(hours=1):
            return t
    return None


def parse_metar(raw: str, obs_time: datetime | None = None, ref: datetime | None = None) -> Observation | None:
    tokens = raw.strip().split()
    if not tokens:
        return None
    i = 0
    while i < len(tokens) and tokens[i] in ("METAR", "SPECI"):
        i += 1
    if i >= len(tokens):
        return None
    station = tokens[i].upper()
    body, _, remarks = " ".join(tokens[i + 1:]).partition(" RMK ")

    t = obs_time
    temp = None
    for tok in body.split():
        if t is None:
            m = _TIME_RE.match(tok)
            if m:
                t = _resolve_time(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                  ref or datetime.now(timezone.utc))
        m = _TEMP_RE.match(tok)
        if m:
            temp = _signed(m.group(1))
    if temp is None or t is None:
        return None

    tenths = None
    m = _TGROUP_RE.search(remarks)
    if m:
        tenths = int(m.group(2)) / 10.0 * (-1 if m.group(1) == "1" else 1)
    return Observation(station=station, time=t, temp_c=temp, temp_c_tenths=tenths, raw=raw.strip())


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def displayed_bounds(obs: Observation, unit: str) -> tuple[int, int]:
    """Lowest and highest whole-degree value the resolution page can show for this report.

    For Celsius markets the page shows the METAR's whole degree directly.
    For Fahrenheit markets it converts; we don't know the exact rounding, so we take
    the full range of values that are still possible.
    """
    if unit == "C":
        return obs.temp_c, obs.temp_c
    if obs.temp_c_tenths is not None:
        c_lo, c_hi = obs.temp_c_tenths - 0.05, obs.temp_c_tenths + 0.05
    else:
        c_lo, c_hi = obs.temp_c - 0.5, obs.temp_c + 0.5
    f_lo, f_hi = _c_to_f(c_lo), _c_to_f(c_hi)
    return math.ceil(f_lo - 0.5 - 1e-9), math.floor(f_hi + 0.5 + 1e-9)


# A report outside these limits is a typo or a decoding error, never a reading to trade on.
MIN_TEMP_C, MAX_TEMP_C = -60, 55
MAX_FUTURE = timedelta(minutes=10)


def plausible(obs: Observation, now: datetime | None = None) -> bool:
    """False for reports timed more than 10 min in the future or outside -60..55 °C."""
    now = now or datetime.now(timezone.utc)
    if obs.time > now + MAX_FUTURE:
        return False
    if not MIN_TEMP_C <= obs.temp_c <= MAX_TEMP_C:
        return False
    if obs.temp_c_tenths is not None and not MIN_TEMP_C <= obs.temp_c_tenths <= MAX_TEMP_C:
        return False
    return True


def default_cadence_min(station: str) -> float:
    """US (K*) and Canadian (C*) stations report hourly; most others every 30 minutes."""
    return 60.0 if station[:1] in ("K", "C") else 30.0


class ObservationStore:
    """Keeps the last ~2 days of reports per station."""

    def __init__(self) -> None:
        self._by_station: dict[str, dict[datetime, Observation]] = {}
        self.rejected = 0     # implausible reports thrown away

    def add(self, obs: Observation, now: datetime | None = None) -> bool:
        """Returns True if this report is new. Implausible reports are rejected (False)."""
        if not plausible(obs, now):
            self.rejected += 1
            log.warning("ignored implausible report %s %s: %s", obs.station, obs.time.isoformat(), obs.raw[:80])
            return False
        bucket = self._by_station.setdefault(obs.station, {})
        is_new = obs.time not in bucket
        bucket[obs.time] = obs  # a correction (COR) for the same time replaces the old one
        return is_new

    def get(self, station: str) -> list[Observation]:
        return sorted(self._by_station.get(station, {}).values(), key=lambda o: o.time)

    def latest_time(self, station: str) -> datetime | None:
        obs = self._by_station.get(station)
        return max(obs) if obs else None

    def cadence_min(self, station: str) -> float:
        """Usual minutes between reports: median gap of the last 6 reports (or the default)."""
        times = sorted(self._by_station.get(station, {}))[-6:]
        gaps = sorted((b - a).total_seconds() / 60 for a, b in zip(times, times[1:]) if b > a)
        if not gaps:
            return default_cadence_min(station)
        mid = len(gaps) // 2
        return gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2

    def age_s(self, station: str, now: datetime) -> float | None:
        """Seconds since the station's newest report (None = no report yet)."""
        last = self.latest_time(station)
        return (now - last).total_seconds() if last else None

    def is_stale(self, station: str, now: datetime, factor: float = 2.0, extra_s: float = 0.0) -> bool:
        """The newest report is older than `factor` x the station's cadence (+ extra_s), or there is none."""
        age = self.age_s(station, now)
        return age is None or age > factor * self.cadence_min(station) * 60 + extra_s

    def prune(self, now: datetime, keep: timedelta = timedelta(hours=48)) -> None:
        for st, obs in self._by_station.items():
            for t in [t for t in obs if t < now - keep]:
                del obs[t]


class AviationWeatherSource:
    """aviationweather.gov data API: one request for many stations."""

    URL = "https://aviationweather.gov/api/data/metar"

    def __init__(self, session: requests.Session) -> None:
        self.session = session
        self._primed: set[str] = set()

    def fetch(self, stations: list[str]) -> list[Observation]:
        out: list[Observation] = []
        now = datetime.now(timezone.utc)
        for chunk_start in range(0, len(stations), 40):
            chunk = stations[chunk_start:chunk_start + 40]
            hours = 2 if all(s in self._primed for s in chunk) else 30
            r = self.session.get(self.URL, params={"ids": ",".join(chunk), "format": "json", "hours": hours},
                                 timeout=(5, 12))
            r.raise_for_status()
            for row in r.json() or []:
                # Use the report's own time group (DDHHMMZ), like the resolution page and the
                # backtest do - not the API's obsTime, which can put a midnight report on the wrong day.
                obs = parse_metar(row.get("rawOb") or "", ref=now)
                if obs:
                    out.append(obs)
            self._primed.update(chunk)
        return out


class TgftpSource:
    """NOAA's per-station latest-METAR text files (updated quickly, one request per station).

    Only holds the latest report, so the first fetch is backfilled from aviationweather.gov.
    """

    URL = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/{}.TXT"

    def __init__(self, session: requests.Session) -> None:
        self.session = session
        self._backfill = AviationWeatherSource(session)
        self._primed: set[str] = set()

    def _one(self, station: str) -> Observation | None:
        r = self.session.get(self.URL.format(station), timeout=(5, 8))
        if r.status_code != 200:
            return None
        lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
        if len(lines) < 2:
            return None
        return parse_metar(lines[1], ref=datetime.now(timezone.utc))

    def fetch(self, stations: list[str]) -> list[Observation]:
        out: list[Observation] = []
        new = [s for s in stations if s not in self._primed]
        if new:
            try:
                out.extend(self._backfill.fetch(new))      # earlier reports of today
                self._primed.update(new)
            except requests.RequestException:
                pass                                       # fine: try the history again next time
        with ThreadPoolExecutor(max_workers=16) as pool:
            for obs in pool.map(self._one, stations):
                if obs:
                    out.append(obs)
        return out


class FailoverSource:
    """Primary source, with the other one as backup when the primary can't be reached.

    Note: only METAR/SPECI. US 5-minute data must never be used - the US markets resolve on
    the hourly reports, and the backtest showed 5-minute peaks would call winning buckets dead.
    """

    def __init__(self, primary, backup, names: tuple[str, str]) -> None:
        self.primary, self.backup, self.names = primary, backup, names
        self.last_used = names[0]

    def fetch(self, stations: list[str]) -> list[Observation]:
        try:
            out = self.primary.fetch(stations)
            self.last_used = self.names[0]
            return out
        except requests.RequestException as exc:
            log.info("%s unreachable (%s), using %s", self.names[0], type(exc).__name__, self.names[1])
            out = self.backup.fetch(stations)      # raises if the backup fails too
            self.last_used = self.names[1]
            return out


def make_source(name: str, session: requests.Session):
    aw, tg = AviationWeatherSource(session), TgftpSource(session)
    if name == "tgftp":
        return FailoverSource(tg, aw, ("tgftp", "aviationweather"))
    return FailoverSource(aw, tg, ("aviationweather", "tgftp"))
