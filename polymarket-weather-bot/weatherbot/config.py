"""Settings, read from environment variables (or a local .env file).

Defaults mirror what HighTempTation does, scaled down to small amounts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader so we don't need python-dotenv. Existing env vars win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _get(name: str, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "ja", "on")
    return cast(raw)


@dataclass(frozen=True)
class Config:
    mode: str = "paper"                 # "paper" (simulate) or "live" (real orders)

    # --- strategy (copied from HighTempTation) ---
    max_buy_price: float = 0.98         # he bought NO between ~0.60 and 0.98
    sell_price: float = 0.99            # he resold almost everything at 0.99
    min_edge: float = 0.005             # min profit per share after estimated fees
    safety_margin: int = 0              # extra degrees required beyond the bucket (0 = exactly like him)
    enable_yes_bets: bool = True        # "X or higher"/"X or below" YES once it is certain
    sell_delay_seconds: float = 5.0     # he sold 4-6 s after buying

    # --- small-scale risk limits ---
    max_usdc_per_trade: float = 10.0
    max_usdc_per_market: float = 20.0
    max_open_usdc: float = 50.0
    daily_loss_limit: float = 20.0

    # --- fees (weather markets charge takers) ---
    fee_rate: float = 0.05              # estimate: fee/share ~= fee_rate * min(p, 1-p)

    # --- timing ---
    poll_seconds: float = 10.0
    market_refresh_seconds: float = 600.0
    recheck_window_seconds: float = 600.0   # keep looking for stale asks this long after a bucket dies
    recheck_every_seconds: float = 30.0

    # --- data ---
    metar_source: str = "aviationweather"   # or "tgftp" (NOAA per-station files, often a bit faster)
    cities: tuple[str, ...] = ()            # empty = all cities
    journal_dir: str = "logs"

    # --- live trading (only used in mode=live) ---
    private_key: str = ""
    funder: str = ""                    # your Polymarket proxy wallet address (shown on your profile)
    signature_type: int = 1             # 1 = email/Magic account, 2 = browser wallet (MetaMask etc.)
    clob_host: str = "https://clob.polymarket.com"

    @property
    def live(self) -> bool:
        return self.mode == "live"


def load_config() -> Config:
    load_dotenv()
    cities = tuple(c.strip() for c in _get("CITIES", "").split(",") if c.strip())
    return Config(
        mode=_get("MODE", "paper").lower(),
        max_buy_price=_get("MAX_BUY_PRICE", 0.98, float),
        sell_price=_get("SELL_PRICE", 0.99, float),
        min_edge=_get("MIN_EDGE", 0.005, float),
        safety_margin=_get("SAFETY_MARGIN", 0, int),
        enable_yes_bets=_get("ENABLE_YES_BETS", True, bool),
        sell_delay_seconds=_get("SELL_DELAY_SECONDS", 5.0, float),
        max_usdc_per_trade=_get("MAX_USDC_PER_TRADE", 10.0, float),
        max_usdc_per_market=_get("MAX_USDC_PER_MARKET", 20.0, float),
        max_open_usdc=_get("MAX_OPEN_USDC", 50.0, float),
        daily_loss_limit=_get("DAILY_LOSS_LIMIT", 20.0, float),
        fee_rate=_get("FEE_RATE", 0.05, float),
        poll_seconds=_get("POLL_SECONDS", 10.0, float),
        market_refresh_seconds=_get("MARKET_REFRESH_SECONDS", 600.0, float),
        recheck_window_seconds=_get("RECHECK_WINDOW_SECONDS", 600.0, float),
        recheck_every_seconds=_get("RECHECK_EVERY_SECONDS", 30.0, float),
        metar_source=_get("METAR_SOURCE", "aviationweather").lower(),
        cities=cities,
        journal_dir=_get("JOURNAL_DIR", "logs"),
        private_key=_get("PRIVATE_KEY", ""),
        funder=_get("FUNDER", ""),
        signature_type=_get("SIGNATURE_TYPE", 1, int),
        clob_host=_get("CLOB_HOST", "https://clob.polymarket.com"),
    )
