"""Settings the dashboard may change, saved to the .env file in the data folder.

The wallet private key is never shown again once saved. On a Mac it goes into the
Keychain; elsewhere into .env with owner-only permissions.
"""
from __future__ import annotations

import dataclasses
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

KEYCHAIN_SERVICE = "Weerbot wallet key"
KEYCHAIN_ACCOUNT = "weerbot"


def usd(v: float) -> str:
    """$1.234,56 (Dutch notation)."""
    text = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if v < 0 else "") + "$" + text


def usd_short(v: float) -> str:
    return usd(v)[:-3] if abs(v - round(v)) < 0.005 else usd(v)


def num_nl(v: float, digits: int = 3) -> str:
    text = f"{v:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{v:.0f}"
    return text.replace(".", ",")


POLY_MIN_SHARES = 5          # every weather market: orderMinSize 5 shares


def min_start_amount(trade_pct: float) -> float:
    """Below this a buy at TRADE_PCT is smaller than 5 shares (Polymarket's minimum order)."""
    return float(max(50, math.ceil(5.5 / (trade_pct / 100.0)))) if trade_pct > 0 else 50.0


def _edge_pct(p: float, sell: float = 0.99, fee_rate: float = 0.05) -> float:
    fee = lambda x: fee_rate * x * (1 - x)  # noqa: E731 - Polymarket's taker fee per share
    return (sell - p - fee(p) - fee(sell)) / p * 100


# --- what a setting means for you right now (shown live under each control) ---------------------
def _c_trade(cfg, eq):
    return f"Eén foute meting kost je max {usd(min(eq * cfg.trade_pct / 100, cfg.max_trade_usdc))}"


def _c_market(cfg, eq):
    return f"Per vak per dag max {usd(eq * cfg.market_pct / 100)} (één aankoop plus één snelle bijkoop)"


def _c_event(cfg, eq):
    return f"Eén foute meting op meerdere vakken: max {usd(min(eq * cfg.event_pct / 100, cfg.max_event_usdc))}"


def _c_open(cfg, eq):
    return f"Tegelijk max {usd(eq * cfg.open_pct / 100)} in open posities"


def _c_daily(cfg, eq):
    return f"Na -{usd(eq * cfg.daily_loss_pct / 100)} op een dag stopt hij met kopen"


def _c_drawdown(cfg, eq):
    return f"Noodrem bij {usd(eq * cfg.max_drawdown_pct / 100)} onder je hoogste stand"


def _c_max_trade(cfg, eq):
    by_pct = eq * cfg.trade_pct / 100
    if by_pct < cfg.max_trade_usdc:
        return f"Nu nog niet bereikt: {num_nl(cfg.trade_pct, 1)}% van je saldo is {usd(by_pct)}"
    return f"Dit bedrag begrenst nu elke aankoop ({num_nl(cfg.trade_pct, 1)}% zou {usd(by_pct)} zijn)"


def _c_max_price(cfg, eq):
    if cfg.max_buy_price > 0.975:
        return "Boven 0,975: ~1% winst per trade, één foute meting kost 100%"
    return (f"Koopt tot {num_nl(cfg.max_buy_price)}: minstens ~{num_nl(_edge_pct(cfg.max_buy_price), 1)}% "
            "winst per trade, één foute meting kost 100%")


def _c_margin(cfg, eq):
    if cfg.safety_margin >= 1:
        return "Kost naar schatting 60-80% van de winst; de alarmen beschermen je beter"
    return "Zoals HighTempTation: ook het vak naast de winnaar, daar zat 60-80% van zijn winst"


def _c_poll(cfg, eq):
    text = f"Alle stations elke {num_nl(cfg.poll_seconds, 1)} s; rond hun eigen meetmoment elke 2 s"
    if cfg.poll_seconds > 10:
        text += ". Trager = vaker te laat voor een extra meting (SPECI)"
    return text


@dataclass(frozen=True)
class Field:
    key: str            # .env name (never shown in the settings screen)
    attr: str           # Config attribute
    kind: str           # "float", "int", "bool", "choice", "text"
    label: str
    help: str = ""
    lo: float | None = None         # hard limits: outside = refused (red)
    hi: float | None = None
    choices: tuple[str, ...] = ()
    safe_lo: float | None = None    # advised range: inside = green, outside (but allowed) = amber
    safe_hi: float | None = None
    ge: str | None = None           # must be at least the value of this other setting
    unit: str = ""                  # for 'aanbevolen 2-8%'
    consequence: Callable | None = dataclasses.field(default=None, compare=False)   # (cfg, equity) -> str


FIELDS: tuple[Field, ...] = (
    Field("MODE", "mode", "choice", "Modus", "paper = oefenen zonder geld, live = echt geld", choices=("paper", "live")),
    Field("TRADE_PCT", "trade_pct", "float", "Max per aankoop (% van saldo)",
          "Groeit mee met je saldo. Een foute meting kost bijna de hele aankoop.", 1, 20,
          safe_lo=2, safe_hi=8, unit="%", consequence=_c_trade),
    Field("MARKET_PCT", "market_pct", "float", "Max per vak per dag (% van saldo)",
          "Minstens zoveel als per aankoop", 1, 50, safe_hi=15, ge="TRADE_PCT", unit="%", consequence=_c_market),
    Field("EVENT_PCT", "event_pct", "float", "Max per markt tegelijk (% van saldo)",
          "Eén foute meting raakt vaak 2-3 vakken van dezelfde markt; telt alleen nog niet verkochte posities",
          1, 25, safe_hi=15, ge="TRADE_PCT", unit="%", consequence=_c_event),
    Field("OPEN_PCT", "open_pct", "float", "Max tegelijk in posities (% van saldo)",
          "HighTempTation had nooit meer dan 3-4 nieuwe aankopen binnen een minuut", 10, 60,
          safe_lo=20, safe_hi=40, unit="%", consequence=_c_open),
    Field("DAILY_LOSS_PCT", "daily_loss_pct", "float", "Stop na verlies op een dag (%)",
          "Open posities tellen mee; de volgende dag koopt hij weer", 2, 30,
          safe_lo=4, safe_hi=10, ge="TRADE_PCT", unit="%", consequence=_c_daily),
    Field("MAX_DRAWDOWN_PCT", "max_drawdown_pct", "float", "Noodrem: daling vanaf je hoogste stand (%)",
          "Daarna koopt hij niets meer tot jij op Start drukt", 5, 50, safe_lo=10, safe_hi=25, unit="%",
          consequence=_c_drawdown),
    Field("MAX_TRADE_USDC", "max_trade_usdc", "float", "Max per aankoop ($)",
          "Pas verhogen na 2 weken live zonder problemen", 5, 100000, consequence=_c_max_trade),
    Field("MAX_EVENT_USDC", "max_event_usdc", "float", "Max per markt tegelijk ($)", "", 5, 100000),
    Field("MAX_BUY_PRICE", "max_buy_price", "float", "Hoogste aankoopprijs",
          "Tussen 0,975 en 0,98 verdiende HighTempTation maar 1,4% per trade; 0,98 is het maximum", 0.80, 0.98,
          safe_hi=0.975, consequence=_c_max_price),
    Field("SELL_PRICE", "sell_price", "float", "Verkoopprijs", "HighTempTation: 0,99", 0.9, 0.999),
    Field("MIN_EDGE", "min_edge", "float", "Minimale winst per share", "Na geschatte kosten", 0.0, 0.2),
    Field("SAFETY_MARGIN", "safety_margin", "int", "Extra graden boven het vak",
          "0 = zoals HighTempTation. Bij 1 koopt hij het vak naast de winnaar nooit meer.", 0, 2,
          safe_lo=0, safe_hi=0, consequence=_c_margin),
    Field("ENABLE_YES_BETS", "enable_yes_bets", "bool", "Ook YES op 'X or higher/below'", ""),
    Field("METAR_SOURCE", "metar_source", "choice", "Weerbron", "", choices=("aviationweather", "tgftp")),
    Field("POLL_SECONDS", "poll_seconds", "float", "Elke hoeveel seconden alle stations bekijken",
          "Rond het eigen meetmoment van elk station kijkt hij vanzelf elke 2 seconden", 2, 30,
          safe_lo=5, safe_hi=10, unit=" s", consequence=_c_poll),
    Field("CITIES", "cities", "text", "Alleen deze steden (leeg = alle)", "Komma-gescheiden, bijv. London,Paris,NYC"),
    Field("FUNDER", "funder", "text", "Polymarket wallet-adres (live)", "Het 0x-adres op je profiel"),
    Field("SIGNATURE_TYPE", "signature_type", "int", "Soort account (live)", "1 = e-mail-login, 2 = MetaMask e.d.", 1, 2),
)
BY_KEY = {f.key: f for f in FIELDS}


def _use_keychain() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def keychain_get() -> str | None:
    if not _use_keychain():
        return None
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def keychain_set(value: str) -> bool:
    if not _use_keychain():
        return False
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
                        "-w", value], capture_output=True, text=True)
    return r.returncode == 0


def keychain_delete() -> None:
    if _use_keychain():
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
                       capture_output=True, text=True)


def apply_secret_env() -> None:
    """Put the Keychain key into the process environment so load_config() sees it."""
    key = keychain_get()
    if key:
        os.environ["PRIVATE_KEY"] = key


def read_env_file(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.is_file() else []


def write_env_values(path: Path, values: dict[str, str]) -> None:
    """Update/add KEY=value lines, keeping comments and unknown keys."""
    lines = read_env_file(path)
    done = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            out.append(f"{key}={values[key]}")
            done.add(key)
        else:
            out.append(line)
    for key, value in values.items():
        if key not in done:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return ",".join(value)
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))                # 15 instead of 15.0 (other floats keep every digit)
    return str(value)


def current_values(cfg) -> dict[str, str]:
    return {f.key: _fmt(getattr(cfg, f.attr)) for f in FIELDS}


def current(cfg, geo: dict | None = None, equity: float | None = None) -> dict:
    """geo = the last Polymarket access check (engine.live_access()); live is offered only when allowed.
    equity = the current account value, for the live consequence under each control."""
    from .engine import live_allowed, live_block_text   # late import: engine imports this module's users

    vals = current_values(cfg)
    allowed = live_allowed(geo, cfg)
    eq = cfg.start_amount if equity is None else equity
    return {
        "values": vals,
        "fields": [{"key": f.key, "kind": f.kind, "label": f.label, "help": f.help, "lo": f.lo, "hi": f.hi,
                    "safe_lo": f.safe_lo, "safe_hi": f.safe_hi, "choices": list(f.choices)} for f in FIELDS],
        "checks": check(vals, cfg, eq),
        "equity": round(eq, 2),
        "min_start_amount": min_start_amount(cfg.trade_pct),
        "private_key_set": bool(cfg.private_key),
        "keychain": _use_keychain(),
        "live_allowed": allowed,
        "live_block_text": "" if allowed else live_block_text(geo),
        "country": (geo or {}).get("country"),
    }


def _range_text(lo: float | None, hi: float | None) -> str:
    fmt = lambda v: num_nl(v, 3)  # noqa: E731
    if lo is not None and hi is not None:
        return f"{fmt(lo)}" if lo == hi else f"{fmt(lo)}-{fmt(hi)}"
    if hi is not None:
        return f"max {fmt(hi)}"
    return f"min {fmt(lo)}"


def _parse(f: Field, raw) -> float | int | str:
    """One value, cleaned. Raises ValueError with a Dutch message."""
    text = str(raw).strip()
    if f.kind in ("float", "int"):
        try:
            num = float(text.replace(",", ".")) if f.kind == "float" else int(text)
        except ValueError:
            raise ValueError(f"{f.label}: geen geldig getal") from None
        if not math.isfinite(num):
            raise ValueError(f"{f.label}: geen geldig getal")
        if (f.lo is not None and num < f.lo) or (f.hi is not None and num > f.hi):
            if f.lo is not None and f.hi is not None:
                raise ValueError(f"{f.label}: moet tussen {num_nl(f.lo)} en {num_nl(f.hi)} liggen")
            raise ValueError(f"{f.label}: moet {_range_text(f.lo, f.hi)} zijn")
        return num
    if f.kind == "bool":
        return "true" if text.lower() in ("1", "true", "yes", "ja", "on") else "false"
    if f.kind == "choice":
        if text not in f.choices:
            raise ValueError(f"{f.label}: kies uit {', '.join(f.choices)}")
        return text
    if "\n" in text or "=" in text:
        raise ValueError(f"{f.label}: ongeldige tekens")
    return text


def _cross_error(f: Field, merged: dict) -> str | None:
    """'Stop na verlies' must be at least 'Max per aankoop', etc."""
    if not f.ge or f.key not in merged or f.ge not in merged:
        return None
    try:
        mine, other = float(str(merged[f.key]).replace(",", ".")), float(str(merged[f.ge]).replace(",", "."))
    except ValueError:
        return None
    if mine < other:
        return f"{f.label}: moet minstens even groot zijn als '{BY_KEY[f.ge].label}' ({num_nl(other)})"
    return None


def validate(updates: dict, base: dict | None = None) -> dict[str, str]:
    """Returns cleaned .env values; raises ValueError with a Dutch message.
    base = the current values, so rules between settings (e.g. daily stop >= per buy) can be checked."""
    clean: dict[str, str] = {}
    for key, raw in updates.items():
        f = BY_KEY.get(key)
        if f is None:
            raise ValueError("Onbekende instelling")
        clean[key] = str(_parse(f, raw))
    merged = {**(base or {}), **clean}
    for f in FIELDS:
        if f.ge and (f.key in clean or f.ge in clean):
            err = _cross_error(f, merged)
            if err:
                raise ValueError(err)
    return clean


def preview_config(cfg, values: dict):
    """cfg with the (valid) numeric/bool values from the settings screen applied."""
    changes = {}
    for key, raw in values.items():
        f = BY_KEY.get(key)
        if f is None or f.kind not in ("float", "int", "bool"):
            continue
        try:
            v = _parse(f, raw)
        except ValueError:
            continue
        changes[f.attr] = (v == "true") if f.kind == "bool" else v
    return dataclasses.replace(cfg, **changes)


def check(values: dict, cfg, equity: float) -> dict[str, dict]:
    """Per setting: level good / warn / bad, the live consequence in $, and the advised range.
    Used for the colours and sentences in the settings screen (also while typing)."""
    merged = {**current_values(cfg), **{k: v for k, v in values.items() if k in BY_KEY}}
    preview = preview_config(cfg, merged)
    out: dict[str, dict] = {}
    for f in FIELDS:
        if f.kind not in ("float", "int"):
            continue
        res: dict = {"level": None, "text": "", "error": None,
                     "safe": None if f.safe_lo is None and f.safe_hi is None
                     else "aanbevolen " + _range_text(f.safe_lo, f.safe_hi) + f.unit}
        try:
            num = _parse(f, merged.get(f.key, ""))
            err = _cross_error(f, merged)
        except ValueError as exc:
            num, err = None, str(exc)
        if err:
            res.update(level="bad", error=err.split(": ", 1)[-1])
        else:
            outside = ((f.safe_lo is not None and num < f.safe_lo) or (f.safe_hi is not None and num > f.safe_hi))
            if f.safe_lo is not None or f.safe_hi is not None:
                res["level"] = "warn" if outside else "good"
            if f.consequence:
                try:
                    res["text"] = f.consequence(preview, max(0.0, equity))
                except (TypeError, ValueError, ZeroDivisionError):
                    res["text"] = ""
        out[f.key] = res
    return out


def save(home: Path, updates: dict, private_key: str | None = None, base: dict | None = None) -> None:
    clean = validate(updates, base)
    env_path = home / ".env"
    if private_key is not None:
        pk = private_key.strip()
        if pk and not (pk.startswith("0x") and len(pk) == 66):
            raise ValueError("Private key moet beginnen met 0x en 66 tekens lang zijn")
        if not pk:                             # empty = remove the key
            keychain_delete()
            clean["PRIVATE_KEY"] = ""
            os.environ.pop("PRIVATE_KEY", None)
        elif keychain_set(pk):
            clean["PRIVATE_KEY"] = ""          # stored in the Keychain instead of the file
            os.environ["PRIVATE_KEY"] = pk
        else:
            clean["PRIVATE_KEY"] = pk          # no Keychain: .env with owner-only permissions
            os.environ["PRIVATE_KEY"] = pk
    write_env_values(env_path, clean)
    for key, value in clean.items():
        if key == "PRIVATE_KEY":
            continue
        os.environ[key] = value                # load_config() prefers the environment
