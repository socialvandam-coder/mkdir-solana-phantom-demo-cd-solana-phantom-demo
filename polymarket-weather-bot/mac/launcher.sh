#!/bin/bash
# Weerbot-starter voor macOS (werkt ook op Linux, handig om te testen).
#
# Dit script is Contents/MacOS/Weerbot in Weerbot.app en wordt ook gebruikt
# door "Start Weerbot.command". Het:
#   1. kijkt of Weerbot al draait (dan alleen het dashboard openen);
#   2. zoekt een Python 3.10 of nieuwer;
#   3. maakt/ververst een eigen Python-omgeving (venv) met de onderdelen uit
#      requirements.txt;
#   4. start het dashboard: python -m weatherbot.app [--port N] [--no-browser]
#
# Geschreven voor bash 3.2 (standaard op macOS) en BSD-tools: geen
# associatieve arrays, geen mapfile, geen readlink -f, geen sha256sum.
#
# Instelbaar via omgevingsvariabelen (allemaal optioneel):
#   WEERBOT_APP_DIR   map met weatherbot/ en requirements.txt
#   WEATHERBOT_HOME   datamap (standaard ~/Library/Application Support/Weerbot)
#   WEATHERBOT_PORT   poort van het dashboard (standaard 8765)
#   WEERBOT_PYTHON    pad naar een eigen Python 3.10+
#   WEERBOT_OS        doe alsof dit besturingssysteem draait (alleen voor tests)

PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

OS="${WEERBOT_OS:-$(uname -s 2>/dev/null)}"
is_mac() { [ "$OS" = "Darwin" ]; }

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- mappen ---

# Map van dit script, ook als het via een symlink is gestart (BSD readlink kent geen -f).
script_dir() {
    src="$1"
    while [ -h "$src" ]; do
        dir=$(cd -P "$(dirname "$src")" && pwd)
        src=$(readlink "$src")
        case "$src" in
            /*) ;;
            *) src="$dir/$src" ;;
        esac
    done
    cd -P "$(dirname "$src")" && pwd
}
SCRIPT_DIR=$(script_dir "${BASH_SOURCE[0]:-$0}")

if [ -z "$WEATHERBOT_HOME" ]; then
    if is_mac; then
        WEATHERBOT_HOME="$HOME/Library/Application Support/Weerbot"
    else
        WEATHERBOT_HOME="$HOME/.weerbot"
    fi
fi
mkdir -p "$WEATHERBOT_HOME" || { echo "Kan map niet maken: $WEATHERBOT_HOME" >&2; exit 1; }
WEATHERBOT_HOME=$(cd -P "$WEATHERBOT_HOME" && pwd)
export WEATHERBOT_HOME

if is_mac; then
    LOG_DIR="$HOME/Library/Logs/Weerbot"
else
    LOG_DIR="$WEATHERBOT_HOME"
fi
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="$WEATHERBOT_HOME"
LOG_FILE="$LOG_DIR/launcher.log"

# Houd het logbestand klein: boven 1 MB wordt het oude log launcher.log.1.
if [ -f "$LOG_FILE" ]; then
    log_size=$(wc -c <"$LOG_FILE" 2>/dev/null | tr -d ' ')
    if [ -n "$log_size" ] && [ "$log_size" -gt 1048576 ]; then
        mv -f "$LOG_FILE" "$LOG_FILE.1"
    fi
fi

# Alles gaat naar het logbestand. In een Terminal-venster (Start Weerbot.command)
# zie je het daarnaast ook gewoon op het scherm.
if [ -t 1 ] && have tee; then
    exec > >(tee -a "$LOG_FILE") 2>&1
else
    exec >>"$LOG_FILE" 2>&1
fi

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

say "---- Weerbot-starter ($OS) ----"

# ------------------------------------------------------- macOS-meldingen ---

# Tekst gaat via argv naar AppleScript, dus aanhalingstekens en spaties zijn geen probleem.
notify() {
    say "Melding: $1"
    if is_mac && have osascript; then
        osascript -e 'on run argv' \
                  -e 'display notification (item 1 of argv) with title "Weerbot"' \
                  -e 'end run' "$1" >/dev/null 2>&1 || true
    fi
}

open_path() {
    if is_mac && have open; then
        open "$1" >/dev/null 2>&1 || true
    elif have xdg-open; then
        xdg-open "$1" >/dev/null 2>&1 &
    fi
}

# Foutmelding met knop om het logbestand te tonen.
error_dialog() {
    say "FOUT: $1"
    if is_mac && have osascript; then
        answer=$(osascript -e 'on run argv' \
            -e 'activate' \
            -e 'set r to display dialog (item 1 of argv) with title "Weerbot" buttons {"Toon logbestand", "OK"} default button "OK" with icon stop' \
            -e 'return button returned of r' \
            -e 'end run' "$1" 2>/dev/null)
        if [ "$answer" = "Toon logbestand" ]; then
            open_path "$LOG_FILE"
        fi
    else
        printf '\n%s\n\n' "$1" >&2
    fi
}

fail() {
    error_dialog "$1

Meer details staan in het logbestand:
$LOG_FILE"
    release_lock
    exit 1
}

# -------------------------------------------------------------- argumenten ---

PORT="${WEATHERBOT_PORT:-8765}"
NO_BROWSER=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "--port" ]; then
        PORT="$arg"
    fi
    case "$arg" in
        --port=*) PORT="${arg#--port=}" ;;
        --no-browser) NO_BROWSER=1 ;;
    esac
    prev="$arg"
done
case "$PORT" in
    '' | *[!0-9]*) say "Ongeldige poort '$PORT', gebruik 8765."; PORT=8765 ;;
esac
URL="http://127.0.0.1:$PORT"

open_dashboard() {
    if [ -n "$NO_BROWSER" ]; then
        say "Dashboard: $URL/ (browser niet geopend: --no-browser)"
        return 0
    fi
    say "Dashboard openen: $URL/"
    open_path "$URL/"
}

already_running() {
    have curl || return 1
    resp=$(curl -fsS --noproxy '*' --max-time 2 "$URL/api/ping" 2>/dev/null) || return 1
    case "$resp" in
        *weerbot*) return 0 ;;
    esac
    return 1
}

# ---------------------------------------------------------------- vergrendeling ---

# Voorkomt dat twee starts tegelijk de venv gaan installeren (mkdir is atomair).
LOCK_DIR="$WEATHERBOT_HOME/.starter.lock"
LOCK_HELD=""
LOCK_WAITED=""

release_lock() {
    if [ -n "$LOCK_HELD" ]; then
        rm -rf "$LOCK_DIR"
        LOCK_HELD=""
    fi
}

acquire_lock() {
    waited=0
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        owner=$(cat "$LOCK_DIR/pid" 2>/dev/null)
        if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
            say "Oude vergrendeling van proces $owner opgeruimd."
            rm -rf "$LOCK_DIR"
            continue
        fi
        if [ "$waited" -ge 600 ]; then
            say "Vergrendeling na 10 minuten geforceerd."
            rm -rf "$LOCK_DIR"
            continue
        fi
        if [ "$waited" -eq 0 ]; then
            say "Er is al een Weerbot-start bezig, even wachten..."
        fi
        LOCK_WAITED=1
        sleep 1
        waited=$((waited + 1))
    done
    echo $$ >"$LOCK_DIR/pid"
    LOCK_HELD=1
}

trap 'release_lock' EXIT
trap 'release_lock; exit 130' INT TERM HUP

# ------------------------------------------------------------ al actief? ---

if already_running; then
    say "Weerbot draait al op $URL."
    open_dashboard
    exit 0
fi

# ------------------------------------------------------------- app-map ---

if [ -n "$WEERBOT_APP_DIR" ]; then
    APP_DIR="$WEERBOT_APP_DIR"
elif [ -d "$SCRIPT_DIR/../Resources/app/weatherbot" ]; then
    APP_DIR="$SCRIPT_DIR/../Resources/app"      # binnen Weerbot.app
else
    APP_DIR="$SCRIPT_DIR/.."                     # projectmap (mac/launcher.sh)
fi
if ! APP_DIR=$(cd -P "$APP_DIR" 2>/dev/null && pwd) \
   || [ ! -d "$APP_DIR/weatherbot" ] || [ ! -f "$APP_DIR/requirements.txt" ]; then
    fail "De programmabestanden van Weerbot zijn niet gevonden (map weatherbot en requirements.txt).
Gezocht in: ${APP_DIR:-$WEERBOT_APP_DIR}"
fi
say "Programmamap: $APP_DIR"
say "Datamap: $WEATHERBOT_HOME"

# ---------------------------------------------------------------- Python ---

python_ok() {
    "$1" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

python_version() {
    "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null
}

# /usr/bin/python3 op macOS kan een "stub" zijn die een installatievenster
# voor de Command Line Tools opent. Alleen gebruiken als die tools er zijn.
is_clt_stub() {
    is_mac || return 1
    [ "$1" = "/usr/bin/python3" ] || return 1
    if have xcode-select && xcode-select -p >/dev/null 2>&1; then
        return 1
    fi
    return 0
}

newest_framework_python() {
    best=""
    best_minor=-1
    for p in /Library/Frameworks/Python.framework/Versions/3.*/bin/python3; do
        [ -x "$p" ] || continue
        minor=${p#/Library/Frameworks/Python.framework/Versions/3.}
        minor=${minor%%/*}
        case "$minor" in
            '' | *[!0-9]*) continue ;;
        esac
        if [ "$minor" -gt "$best_minor" ]; then
            best_minor=$minor
            best=$p
        fi
    done
    printf '%s' "$best"
}

PY=""
find_python() {
    framework_py=$(newest_framework_python)
    path_py=$(command -v python3 2>/dev/null)
    for cand in "$WEERBOT_PYTHON" \
                /opt/homebrew/bin/python3 \
                /usr/local/bin/python3 \
                "$framework_py" \
                "$path_py"; do
        [ -n "$cand" ] || continue
        [ -x "$cand" ] || continue
        if is_clt_stub "$cand"; then
            say "Overgeslagen: $cand (Command Line Tools niet geïnstalleerd)"
            continue
        fi
        if python_ok "$cand"; then
            PY="$cand"
            say "Python gevonden: $cand ($(python_version "$cand"))"
            return 0
        fi
        say "Overgeslagen: $cand (versie $(python_version "$cand") is te oud of mist venv)"
    done
    return 1
}

no_python() {
    msg="Weerbot heeft Python nodig, maar er is geen geschikte versie (3.10 of nieuwer) op deze computer gevonden.

Installeer Python 3.12 van python.org (kies de \"macOS 64-bit universal2 installer\") en start Weerbot daarna opnieuw."
    say "FOUT: geen Python 3.10+ gevonden."
    if is_mac && have osascript; then
        answer=$(osascript -e 'on run argv' \
            -e 'activate' \
            -e 'set r to display dialog (item 1 of argv) with title "Weerbot" buttons {"Annuleren", "Download Python"} default button "Download Python" cancel button "Annuleren" with icon caution' \
            -e 'return button returned of r' \
            -e 'end run' "$msg" 2>/dev/null)
        if [ "$answer" = "Download Python" ]; then
            open_path "https://www.python.org/downloads/macos/"
        fi
    else
        printf '\n%s\n\nDownload: https://www.python.org/downloads/\n\n' "$msg" >&2
    fi
    release_lock
    exit 1
}

# ------------------------------------------------------------------ venv ---

VENV="$WEATHERBOT_HOME/venv"
VPY="$VENV/bin/python"
STAMP="$VENV/.weerbot-requirements.sha256"
REQ="$APP_DIR/requirements.txt"

file_hash() {
    if have shasum; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif have sha256sum; then
        sha256sum "$1" | awk '{print $1}'
    else
        cksum "$1" | awk '{print $1 "-" $2}'
    fi
}

venv_ok() {
    [ -x "$VPY" ] && python_ok "$VPY"
}

acquire_lock
if [ -n "$LOCK_WAITED" ]; then
    # Een andere start was bezig en heeft Weerbot misschien net gestart.
    i=0
    while [ "$i" -lt 15 ]; do
        if already_running; then
            say "Weerbot is intussen gestart door de andere start."
            release_lock
            open_dashboard
            exit 0
        fi
        sleep 1
        i=$((i + 1))
    done
fi

REQ_HASH=$(file_hash "$REQ")
if venv_ok; then
    OLD_HASH=$(cat "$STAMP" 2>/dev/null)
else
    OLD_HASH=""
    find_python || no_python
    if [ -d "$VENV" ]; then
        say "Bestaande venv werkt niet (meer); wordt opnieuw gemaakt."
        notify "Weerbot wordt opnieuw geïnstalleerd, dit duurt ongeveer een minuut…"
        rm -rf "$VENV"
    else
        notify "Weerbot wordt voor de eerste keer geïnstalleerd, dit duurt ongeveer een minuut…"
    fi
    say "Venv maken in $VENV"
    if ! "$PY" -m venv "$VENV"; then
        rm -rf "$VENV"
        fail "Het maken van de Python-omgeving voor Weerbot is mislukt."
    fi
fi

if [ "$OLD_HASH" != "$REQ_HASH" ]; then
    if [ -n "$OLD_HASH" ]; then
        notify "Weerbot wordt bijgewerkt, een ogenblik geduld…"
    fi
    say "Onderdelen installeren uit $REQ"
    "$VPY" -m pip install --disable-pip-version-check --quiet --upgrade pip \
        || say "Waarschuwing: pip bijwerken mislukt, ga door met de huidige versie."
    if ! "$VPY" -m pip install --disable-pip-version-check --progress-bar off -r "$REQ"; then
        rm -f "$STAMP"
        fail "Het installeren van de onderdelen van Weerbot is mislukt. Controleer je internetverbinding en probeer het opnieuw."
    fi
    printf '%s\n' "$REQ_HASH" >"$STAMP"
    say "Installatie klaar."
else
    say "Venv is up-to-date."
fi

# ----------------------------------------------------------------- start ---

release_lock
trap - EXIT INT TERM HUP

cd "$APP_DIR" || fail "Kan niet naar de programmamap gaan: $APP_DIR"
PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
PYTHONUNBUFFERED=1
# Bytecode buiten de app-bundel houden (anders klopt de handtekening niet meer).
PYTHONPYCACHEPREFIX="$WEATHERBOT_HOME/pycache"
export PYTHONPATH PYTHONUNBUFFERED PYTHONPYCACHEPREFIX WEATHERBOT_HOME

say "Start: $VPY -m weatherbot.app $*"
exec "$VPY" -m weatherbot.app "$@"
