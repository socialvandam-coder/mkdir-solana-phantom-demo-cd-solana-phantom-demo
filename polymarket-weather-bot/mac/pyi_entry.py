"""Startpunt van de zelfstandige Weerbot.app (gebouwd met PyInstaller).

- Zet WEATHERBOT_HOME (standaard ~/Library/Application Support/Weerbot).
- Schrijft uitvoer naar ~/Library/Logs/Weerbot/app.log (een app heeft geen Terminal).
- Start weatherbot.app.main() (die opent alleen het dashboard als Weerbot al draait).

WEERBOT_SELFTEST=1 controleert alleen of alle onderdelen in de bundel zitten
(gebruikt door mac/build_standalone.sh) en start niets.
"""
from __future__ import annotations

import os
import sys

LOG_LIMIT_BYTES = 2 * 1024 * 1024


def default_home() -> str:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Weerbot")
    return os.path.join(home, ".weerbot")


def log_dir(data_home: str) -> str:
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Logs", "Weerbot")
    return data_home


def redirect_output(path: str) -> None:
    """In een app zonder Terminal gaat print/logging naar een logbestand."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > LOG_LIMIT_BYTES:
            os.replace(path, path + ".1")
        fh = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return
    sys.stdout = fh
    sys.stderr = fh


def selftest() -> int:
    """Controleer dat de bundel compleet is, zonder iets te starten."""
    import importlib
    import zoneinfo

    problems = []
    for mod in ("weatherbot", "weatherbot.app", "weatherbot.main", "requests",
                "py_clob_client.client", "py_clob_client.clob_types",
                "py_clob_client.order_builder.constants"):
        try:
            importlib.import_module(mod)
        except Exception as exc:  # pragma: no cover - alleen bij een kapotte build
            problems.append(f"import {mod}: {exc!r}")
    try:
        zoneinfo.ZoneInfo("Europe/Amsterdam")
        zoneinfo.ZoneInfo("America/New_York")
    except Exception as exc:
        problems.append(f"tijdzones: {exc!r}")
    try:
        import weatherbot

        static = os.path.join(os.path.dirname(weatherbot.__file__), "static")
        if not os.path.isdir(static) or not os.listdir(static):
            problems.append(f"dashboardbestanden ontbreken: {static}")
    except Exception as exc:
        problems.append(f"static: {exc!r}")
    if problems:
        print("ZELFTEST MISLUKT:")
        for p in problems:
            print("  - " + p)
        return 1
    print("zelftest ok")
    return 0


def main() -> int:
    # Oudere macOS-versies geven bij starten uit Finder een "-psn_..."-argument mee.
    sys.argv[1:] = [a for a in sys.argv[1:] if not a.startswith("-psn_")]

    home = os.environ.get("WEATHERBOT_HOME") or default_home()
    os.environ["WEATHERBOT_HOME"] = home
    os.makedirs(home, exist_ok=True)

    if os.environ.get("WEERBOT_SELFTEST"):
        return selftest()

    frozen = getattr(sys, "frozen", False)
    if frozen and not (sys.stdout is not None and sys.stdout.isatty()):
        redirect_output(os.path.join(log_dir(home), "app.log"))

    # Een app start met "/" als werkmap; zorg dat relatieve paden in de datamap landen.
    try:
        os.chdir(home)
    except OSError:
        pass

    from weatherbot.app import main as app_main

    result = app_main()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
