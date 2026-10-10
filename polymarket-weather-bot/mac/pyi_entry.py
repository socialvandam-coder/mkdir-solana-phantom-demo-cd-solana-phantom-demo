"""Startpunt van de zelfstandige Weerbot.app (gebouwd met PyInstaller).

- Zet WEATHERBOT_HOME (standaard ~/Library/Application Support/Weerbot).
- Schrijft uitvoer naar ~/Library/Logs/Weerbot/app.log (een app heeft geen Terminal).
- Start weatherbot.app.main(). Draait Weerbot al, dan opent die alleen het dashboard
  (bijvoorbeeld als je hem start met `open -n`). Let op: dubbelklikken op een app die
  al draait start geen tweede proces; macOS stuurt dan alleen een "heropen"-bericht,
  dat deze app niet afhandelt. Het dashboard open je dan via de bladwijzer (README).
- Stopt Weerbot met een fout, dan toont hij een venster met een knop naar app.log.

WEERBOT_SELFTEST=1 controleert alleen of alle onderdelen in de bundel zitten
(gebruikt door mac/build_standalone.sh) en start niets.
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback

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


def error_dialog(message: str, log_path: str) -> None:
    """Toon een foutvenster (alleen op een Mac) met een knop om het logbestand te openen."""
    if sys.platform != "darwin":
        return
    script = [
        "on run argv",
        'set r to display dialog (item 1 of argv) with title "Weerbot" '
        'buttons {"Toon logbestand", "OK"} default button "OK" with icon stop',
        "return button returned of r",
        "end run",
    ]
    cmd = ["osascript"]
    for line in script:
        cmd += ["-e", line]
    try:
        res = subprocess.run(cmd + [message], capture_output=True, text=True, timeout=3600)
        if res.stdout.strip() == "Toon logbestand":
            subprocess.run(["open", log_path], timeout=30)
    except Exception:  # noqa: BLE001 - een venster is een extraatje
        pass


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
    interactive = sys.stdout is not None and sys.stdout.isatty()
    log_path = os.path.join(log_dir(home), "app.log")
    if frozen and not interactive:
        redirect_output(log_path)

    # Een app start met "/" als werkmap; zorg dat relatieve paden in de datamap landen.
    try:
        os.chdir(home)
    except OSError:
        pass

    shown: list[str] = []
    try:
        import weatherbot.app as wapp

        # weatherbot.app toont bij bekende startfouten zelf een venster; onthoud
        # dat, zodat er hieronder geen tweede venster over dezelfde fout komt.
        orig_show_error = getattr(wapp, "show_error", None)
        if callable(orig_show_error):
            def _show_error(message, *a, **kw):
                shown.append(str(message))
                return orig_show_error(message, *a, **kw)

            wapp.show_error = _show_error

        result = wapp.main()
    except KeyboardInterrupt:
        raise
    except SystemExit as exc:
        if exc.code in (None, 0):
            raise
        if not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
        result = exc.code if isinstance(exc.code, int) else 1
    except BaseException:  # noqa: BLE001 - elke fout moet zichtbaar worden
        traceback.print_exc()
        result = 1
    if not isinstance(result, int):
        result = 0
    if result != 0:
        print(f"Weerbot is gestopt met code {result}.", file=sys.stderr, flush=True)
        if frozen and not interactive and not shown:
            error_dialog(
                f"Weerbot kon niet starten of is onverwacht gestopt (code {result}).\n\n"
                f"Meer details staan in het logbestand:\n{log_path}",
                log_path,
            )
    return result


if __name__ == "__main__":
    sys.exit(main())
