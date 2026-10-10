"""Settings the dashboard may change, saved to the .env file in the data folder.

The wallet private key is never shown again once saved. On a Mac it goes into the
Keychain; elsewhere into .env with owner-only permissions.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

KEYCHAIN_SERVICE = "Weerbot wallet key"
KEYCHAIN_ACCOUNT = "weerbot"


@dataclass(frozen=True)
class Field:
    key: str            # .env name
    attr: str           # Config attribute
    kind: str           # "float", "int", "bool", "choice", "text"
    label: str
    help: str = ""
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()


FIELDS: tuple[Field, ...] = (
    Field("MODE", "mode", "choice", "Modus", "paper = oefenen zonder geld, live = echt geld", choices=("paper", "live")),
    Field("TRADE_PCT", "trade_pct", "float", "Max per aankoop (% van saldo)", "Groeit mee met je saldo", 1, 50),
    Field("MARKET_PCT", "market_pct", "float", "Max per vak per dag (% van saldo)", "", 1, 100),
    Field("OPEN_PCT", "open_pct", "float", "Max tegelijk in posities (% van saldo)", "", 5, 100),
    Field("DAILY_LOSS_PCT", "daily_loss_pct", "float", "Stop na verlies op een dag (%)", "Daarna koopt hij die dag niets meer", 1, 50),
    Field("MAX_BUY_PRICE", "max_buy_price", "float", "Hoogste aankoopprijs", "HighTempTation: tot ~0,98", 0.5, 0.989),
    Field("SELL_PRICE", "sell_price", "float", "Verkoopprijs", "HighTempTation: 0,99", 0.9, 0.999),
    Field("MIN_EDGE", "min_edge", "float", "Minimale winst per share", "Na geschatte fees", 0.0, 0.2),
    Field("SAFETY_MARGIN", "safety_margin", "int", "Veiligheidsmarge (graden)", "0 = zoals hij, 1 = veiliger maar minder trades", 0, 3),
    Field("ENABLE_YES_BETS", "enable_yes_bets", "bool", "Ook YES op 'X or higher/below'", ""),
    Field("METAR_SOURCE", "metar_source", "choice", "Weerbron", "", choices=("aviationweather", "tgftp")),
    Field("POLL_SECONDS", "poll_seconds", "float", "Elke hoeveel seconden kijken", "Buiten de vaste meetmomenten", 3, 120),
    Field("FAST_POLL_SECONDS", "fast_poll_seconds", "float", "Snel kijken rond meetmomenten (s)", "Rond :50-:05, :20-:35, als nieuwe metingen uitkomen", 1, 60),
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
    return str(value)


def current(cfg) -> dict:
    vals = {f.key: _fmt(getattr(cfg, f.attr)) for f in FIELDS}
    return {
        "values": vals,
        "fields": [{"key": f.key, "kind": f.kind, "label": f.label, "help": f.help, "lo": f.lo, "hi": f.hi,
                    "choices": list(f.choices)} for f in FIELDS],
        "private_key_set": bool(cfg.private_key),
        "keychain": _use_keychain(),
    }


def validate(updates: dict) -> dict[str, str]:
    """Returns cleaned .env values; raises ValueError with a Dutch message."""
    clean: dict[str, str] = {}
    for key, raw in updates.items():
        f = BY_KEY.get(key)
        if f is None:
            raise ValueError(f"Onbekende instelling: {key}")
        text = str(raw).strip()
        if f.kind in ("float", "int"):
            try:
                num = float(text.replace(",", ".")) if f.kind == "float" else int(text)
            except ValueError:
                raise ValueError(f"{f.label}: geen geldig getal") from None
            if (f.lo is not None and num < f.lo) or (f.hi is not None and num > f.hi):
                raise ValueError(f"{f.label}: moet tussen {f.lo:g} en {f.hi:g} liggen")
            clean[key] = str(num)
        elif f.kind == "bool":
            clean[key] = "true" if text.lower() in ("1", "true", "yes", "ja", "on") else "false"
        elif f.kind == "choice":
            if text not in f.choices:
                raise ValueError(f"{f.label}: kies uit {', '.join(f.choices)}")
            clean[key] = text
        else:
            if "\n" in text or "=" in text:
                raise ValueError(f"{f.label}: ongeldige tekens")
            clean[key] = text
    return clean


def save(home: Path, updates: dict, private_key: str | None = None) -> None:
    clean = validate(updates)
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
