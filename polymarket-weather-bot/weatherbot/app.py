"""Desktop entry point: start the bot + dashboard and open it in the browser.

    python -m weatherbot.app [--port 8765] [--no-browser]

Data (settings, saved balance, logs) lives in WEATHERBOT_HOME,
on a Mac by default ~/Library/Application Support/Weerbot.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

log = logging.getLogger("weatherbot")


def default_home() -> Path:
    env = os.environ.get("WEATHERBOT_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Weerbot"
    return Path.home() / ".weerbot"


def is_weerbot(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.load(r).get("app") == "weerbot"
    except Exception:  # noqa: BLE001 - nothing (or something else) is listening
        return False


def _setup_logging(home: Path) -> None:
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(logs / "weerbot.log", maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


BUNDLE_ID = os.environ.get("WEATHERBOT_BUNDLE_ID", "local.weerbot.app")
KEEP_AWAKE_TIP = "Laat de Mac aan de stroom en de klep open (klep dicht = bot stopt, tenzij extern scherm)."


def caffeinate_cmd(pid: int) -> list[str]:
    """-i: no idle sleep, -s: no system sleep (only works on mains power), -m: keep the disk awake.
    -w: stop as soon as the app stops. Closing the lid still sleeps the Mac (unless an
    external screen is connected)."""
    return ["caffeinate", "-i", "-s", "-m", "-w", str(pid)]


def _keep_awake() -> subprocess.Popen | None:
    """On a Mac, keep it awake while the app runs."""
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        log.info("Mac blijft wakker zolang Weerbot draait. %s", KEEP_AWAKE_TIP)
        return subprocess.Popen(caffeinate_cmd(os.getpid()))
    return None


def disable_app_nap(home: Path, run=subprocess.run, platform: str | None = None) -> bool:
    """First launch on a Mac: tell macOS not to put Weerbot to 'App Nap' (it has no window, so
    macOS would otherwise slow it down in the background). Done once per data folder."""
    if (platform or sys.platform) != "darwin" or not shutil.which("defaults"):
        return False
    marker = home / ".app-nap-off"
    if marker.exists():
        return False
    try:
        r = run(["defaults", "write", BUNDLE_ID, "NSAppSleepDisabled", "-bool", "YES"],
                capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("could not switch off App Nap: %s", exc)
        return False
    if getattr(r, "returncode", 1) != 0:
        return False
    try:
        marker.write_text("NSAppSleepDisabled=YES\n")
    except OSError:
        pass
    return True


def show_error(message: str) -> None:
    """A visible error for people who started the app by double-clicking (no terminal)."""
    log.error("%s", message)
    if sys.platform == "darwin" and shutil.which("osascript"):
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        'display dialog (item 1 of argv) with title "Weerbot" buttons {"OK"} '
                        'default button "OK" with icon stop', "-e", "end run", message],
                       capture_output=True, timeout=600)
    else:
        print(message, file=sys.stderr)


def _port_from_env() -> int:
    try:
        return int(os.environ.get("WEATHERBOT_PORT", "8765"))
    except ValueError:
        return 8765


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weerbot dashboard")
    ap.add_argument("--port", type=int, default=_port_from_env())
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--home", help="data folder (default: WEATHERBOT_HOME or the per-OS default)")
    # Finder sometimes adds "-psn_0_12345" on the first launch of a downloaded app; ignore extras.
    args, _unknown = ap.parse_known_args(argv)
    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 - last resort: tell the user instead of vanishing
        logging.getLogger("weatherbot").exception("Weerbot could not start")
        show_error(f"Weerbot kon niet starten:\n{exc}\n\nDetails staan in het logboek in "
                   f"{default_home() / 'logs' / 'weerbot.log'}")
        return 1


def _run(args: argparse.Namespace) -> int:

    home = Path(args.home).expanduser() if args.home else default_home()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["WEATHERBOT_HOME"] = str(home)
    _setup_logging(home)

    # Already running? Just show it.
    for port in range(args.port, args.port + 10):
        if is_weerbot(port):
            log.info("Weerbot already running on port %d", port)
            if not args.no_browser:
                webbrowser.open(f"http://127.0.0.1:{port}/")
            return 0

    from .dashboard import DashboardServer
    from .engine import Engine

    engine = Engine(home)
    server = None
    for port in range(args.port, args.port + 10):
        try:
            server = DashboardServer(port, engine)
            break
        except OSError:
            log.info("port %d is in use, trying the next one", port)
    if server is None:
        show_error(f"Weerbot kon niet starten: de poorten {args.port} t/m {args.port + 9} zijn allemaal "
                   "bezet door andere programma's. Herstart je Mac en probeer het opnieuw.")
        return 1

    url = f"http://127.0.0.1:{server.port}/"
    log.info("Weerbot dashboard: %s (data in %s)", url, home)
    if engine.cfg.autostart:
        engine.start()
    awake = _keep_awake()
    threading.Thread(target=disable_app_nap, args=(home,), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()

    def _stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        engine.stop()
        server.server_close()
        if awake:
            awake.terminate()
        log.info("Weerbot stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
