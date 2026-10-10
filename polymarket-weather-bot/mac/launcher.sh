#!/bin/bash
# Weerbot-starter voor macOS (werkt ook op Linux, handig om te testen).
#
# Dit script staat in Weerbot.app als Contents/Resources/launcher.sh (het
# AppleScript-programmaatje in de app start het) en wordt ook gebruikt door
# "Start Weerbot.command". Het:
#   1. kijkt of Weerbot al draait (dan alleen het dashboard openen);
#   2. zoekt een Python 3.10 t/m 3.14 (nieuwer alleen als er niets anders is);
#   3. maakt/ververst een eigen Python-omgeving (venv) met de onderdelen uit
#      requirements.txt;
#   4. start het dashboard: python -m weatherbot.app [--port N] [--no-browser]
#
# Twee manieren van draaien:
#   - vanuit Terminal (Start Weerbot.command): Weerbot draait in dit venster en
#     stopt als het venster dicht gaat;
#   - als app (geen Terminal): Weerbot wordt los gestart, dit script wacht tot
#     het dashboard antwoordt en stopt dan zelf. Zo kan een nieuwe dubbelklik
#     op de app altijd het dashboard weer openen.
#
# Geschreven voor bash 3.2 (standaard op macOS) en BSD-tools: geen
# associatieve arrays, geen mapfile, geen readlink -f, geen sha256sum.
#
# Instelbaar via omgevingsvariabelen (allemaal optioneel):
#   WEERBOT_APP_DIR     map met weatherbot/ en requirements.txt
#   WEATHERBOT_HOME     datamap (standaard ~/Library/Application Support/Weerbot)
#   WEATHERBOT_PORT     poort van het dashboard (standaard 8765)
#   WEERBOT_PYTHON      pad naar een eigen Python 3.10+
#   WEERBOT_FOREGROUND  1 = altijd zoals in Terminal, 0 = altijd zoals de app
#   WEERBOT_OS          doe alsof dit besturingssysteem draait (alleen voor tests)

PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

OS="${WEERBOT_OS:-$(uname -s 2>/dev/null)}"
is_mac() { [ "$OS" = "Darwin" ]; }

have() { command -v "$1" >/dev/null 2>&1; }

# Nieuwste Python-versie (3.x) waarvoor alle onderdelen kant-en-klaar bestaan.
MAX_TESTED_MINOR=14
PY_ADVICE="3.14"
PY_DOWNLOAD_URL="https://www.python.org/downloads/release/python-3147/"

# Hoe lang (seconden) de app wacht tot het dashboard antwoordt, en hoe lang
# een tweede start wacht op een eerste start die nog bezig is.
START_WAIT=120
LOCK_MAX_WAIT=900

# Finder geeft bij de eerste start van een gedownloade app soms "-psn_0_12345"
# mee; weatherbot.app kent die optie niet. Haal hem weg (bash 3.2-proof).
n_args=$#
while [ "$n_args" -gt 0 ]; do
    arg="$1"
    shift
    case "$arg" in
        -psn_*) ;;
        *) set -- "$@" "$arg" ;;
    esac
    n_args=$((n_args - 1))
done

# Vanuit Terminal of als app?
case "${WEERBOT_FOREGROUND:-auto}" in
    1 | yes) FOREGROUND=1 ;;
    0 | no) FOREGROUND="" ;;
    *)
        if [ -t 0 ] || [ -t 1 ]; then FOREGROUND=1; else FOREGROUND=""; fi
        ;;
esac

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

if [ -n "$FOREGROUND" ]; then
    say "---- Weerbot-starter ($OS, Terminal) ----"
else
    say "---- Weerbot-starter ($OS, app) ----"
fi

# ------------------------------------------------------------ macOS-vensters ---

open_path() {
    if is_mac && have open; then
        open "$1" >/dev/null 2>&1 || true
    elif have xdg-open; then
        xdg-open "$1" >/dev/null 2>&1 &
    fi
}

# Informatievenster dat het script niet ophoudt (meldingen van osascript
# verschijnen vaak niet zonder toestemming, een venster wel). Het venster gaat
# vanzelf dicht zodra Weerbot klaar is (close_info_dialog) of na 10 minuten.
INFO_PID=""
info_dialog() {
    say "Melding: $1"
    is_mac && have osascript || return 0
    close_info_dialog
    # Tekst gaat via argv naar AppleScript, dus aanhalingstekens en spaties zijn geen probleem.
    osascript -e 'on run argv' \
              -e 'activate' \
              -e 'display dialog (item 1 of argv) with title "Weerbot" buttons {"OK"} default button "OK" with icon note giving up after 600' \
              -e 'end run' "$1" </dev/null >/dev/null 2>&1 &
    INFO_PID=$!
}

close_info_dialog() {
    if [ -n "$INFO_PID" ]; then
        kill "$INFO_PID" 2>/dev/null
        INFO_PID=""
    fi
}

# Foutmelding met knop om het logbestand te tonen.
error_dialog() {
    close_info_dialog
    say "FOUT: $1"
    if is_mac && have osascript; then
        answer=$(osascript -e 'on run argv' \
            -e 'activate' \
            -e 'set r to display dialog (item 1 of argv) with title "Weerbot" buttons {"Toon logbestand", "OK"} default button "OK" with icon stop' \
            -e 'return button returned of r' \
            -e 'end run' "$1" </dev/null 2>/dev/null)
        if [ "$answer" = "Toon logbestand" ]; then
            open_path "$LOG_FILE"
        fi
    else
        printf '\n%s\n\n' "$1" >&2
    fi
}

log_hint() {
    printf '%s\n\nMeer details staan in het logbestand:\n%s' "$1" "$LOG_FILE"
}

fail() {
    error_dialog "$(log_hint "$1")"
    release_lock
    exit 1
}

# Venster "Python nodig / Python past niet" met een knop naar de download.
python_dialog() {
    close_info_dialog
    say "FOUT: $1"
    if is_mac && have osascript; then
        answer=$(osascript -e 'on run argv' \
            -e 'activate' \
            -e 'set r to display dialog (item 1 of argv) with title "Weerbot" buttons {"Annuleren", "Download Python"} default button "Download Python" cancel button "Annuleren" with icon caution' \
            -e 'return button returned of r' \
            -e 'end run' "$1" </dev/null 2>/dev/null)
        if [ "$answer" = "Download Python" ]; then
            open_path "$PY_DOWNLOAD_URL"
        fi
    else
        printf '\n%s\n\nDownload: %s\n\n' "$1" "$PY_DOWNLOAD_URL" >&2
    fi
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
RUNNING_PORT="$PORT"

open_dashboard() {
    url="http://127.0.0.1:$RUNNING_PORT/"
    if [ -n "$NO_BROWSER" ]; then
        say "Dashboard: $url (browser niet geopend: --no-browser)"
        return 0
    fi
    say "Dashboard openen: $url"
    open_path "$url"
}

# Draait Weerbot al? weatherbot.app gebruikt de eerste vrije poort van PORT
# t/m PORT+9, dus kijk op al die poorten. Zet RUNNING_PORT.
find_running() {
    have curl || return 1
    probe=$PORT
    while [ "$probe" -le $((PORT + 9)) ]; do
        resp=$(curl -fsS --noproxy '*' --connect-timeout 1 --max-time 2 \
            "http://127.0.0.1:$probe/api/ping" 2>/dev/null)
        case "$resp" in
            *weerbot*) RUNNING_PORT=$probe; return 0 ;;
        esac
        probe=$((probe + 1))
    done
    return 1
}

# ---------------------------------------------------------------- vergrendeling ---

# Er mag maar één Weerbot tegelijk starten en draaien (mkdir is atomair).
# In $LOCK_DIR/pid staat het proces dat de vergrendeling heeft (regel 1) en
# wanneer dat proces gestart is (regel 2), zodat een hergebruikt procesnummer
# (bijvoorbeeld na een herstart van de Mac) niet als "draait nog" telt.
# Bij de app is dat na het starten het Weerbot-proces zelf.
LOCK_DIR="$WEATHERBOT_HOME/.starter.lock"
LOCK_HELD=""
LOCK_OWNER=""

proc_start() {
    ps -o lstart= -p "$1" 2>/dev/null | sed -e 's/^ *//' -e 's/ *$//'
}

write_lock_owner() {
    { echo "$1"; proc_start "$1"; } >"$LOCK_DIR/pid.new" 2>/dev/null \
        && mv -f "$LOCK_DIR/pid.new" "$LOCK_DIR/pid"
}

lock_owner_alive() {
    LOCK_OWNER=$(sed -n 1p "$LOCK_DIR/pid" 2>/dev/null)
    owner_start=$(sed -n 2p "$LOCK_DIR/pid" 2>/dev/null)
    [ -n "$LOCK_OWNER" ] || return 1
    kill -0 "$LOCK_OWNER" 2>/dev/null || return 1
    if [ -n "$owner_start" ]; then
        now_start=$(proc_start "$LOCK_OWNER")
        if [ -n "$now_start" ] && [ "$now_start" != "$owner_start" ]; then
            return 1
        fi
    fi
    return 0
}

release_lock() {
    if [ -n "$LOCK_HELD" ]; then
        rm -rf "$LOCK_DIR"
        LOCK_HELD=""
    fi
}

acquire_lock() {
    waited=0
    no_pid=0
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        if [ ! -s "$LOCK_DIR/pid" ]; then
            # Net aangemaakt door een andere start, of een overblijfsel.
            no_pid=$((no_pid + 1))
            if [ "$no_pid" -gt 10 ]; then
                say "Lege vergrendeling opgeruimd."
                rm -rf "$LOCK_DIR"
                no_pid=0
                continue
            fi
        elif ! lock_owner_alive; then
            say "Oude vergrendeling van proces ${LOCK_OWNER:-?} opgeruimd."
            rm -rf "$LOCK_DIR"
            continue
        fi
        # Een andere start is bezig of Weerbot draait al: wacht tot het dashboard antwoordt.
        if find_running; then
            say "Weerbot draait al op poort $RUNNING_PORT."
            open_dashboard
            exit 0
        fi
        if [ "$waited" -eq 0 ]; then
            say "Er is al een Weerbot-start bezig (proces ${LOCK_OWNER:-?}), even wachten..."
        fi
        if [ "$waited" -ge "$LOCK_MAX_WAIT" ]; then
            error_dialog "$(log_hint "Er is al een andere start van Weerbot bezig, maar die is na $((LOCK_MAX_WAIT / 60)) minuten nog niet klaar.

Wacht nog even en probeer het dan opnieuw. Helpt dat niet, herstart dan je Mac.")"
            exit 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    LOCK_HELD=1
    write_lock_owner $$
}

trap 'release_lock; close_info_dialog' EXIT
trap 'exit 130' INT TERM HUP

# ------------------------------------------------------------ al actief? ---

if find_running; then
    say "Weerbot draait al op poort $RUNNING_PORT."
    open_dashboard
    exit 0
fi

# ------------------------------------------------------------- app-map ---

if [ -n "$WEERBOT_APP_DIR" ]; then
    APP_DIR="$WEERBOT_APP_DIR"
elif [ -d "$SCRIPT_DIR/app/weatherbot" ]; then
    APP_DIR="$SCRIPT_DIR/app"                    # binnen Weerbot.app (Contents/Resources)
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

python_minor() {
    "$1" -c 'import sys; print(sys.version_info[1])' 2>/dev/null
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

# Python van python.org, nieuwste geteste versie eerst (3.14, 3.13, ..., 3.10),
# daarna eventuele nieuwere versies (3.15+), die alleen gebruikt worden als er
# niets anders is.
framework_pythons() {
    m=$MAX_TESTED_MINOR
    while [ "$m" -ge 10 ]; do
        p="/Library/Frameworks/Python.framework/Versions/3.$m/bin/python3"
        [ -x "$p" ] && printf '%s\n' "$p"
        m=$((m - 1))
    done
    m=$((MAX_TESTED_MINOR + 1))
    while [ "$m" -le $((MAX_TESTED_MINOR + 10)) ]; do
        p="/Library/Frameworks/Python.framework/Versions/3.$m/bin/python3"
        [ -x "$p" ] && printf '%s\n' "$p"
        m=$((m + 1))
    done
}

PY=""
find_python() {
    cands=()
    [ -n "$WEERBOT_PYTHON" ] && cands+=("$WEERBOT_PYTHON")
    cands+=(/opt/homebrew/bin/python3 /usr/local/bin/python3)
    for p in $(framework_pythons); do
        cands+=("$p")
    done
    path_py=$(command -v python3 2>/dev/null)
    [ -n "$path_py" ] && cands+=("$path_py")

    too_new=""
    for cand in "${cands[@]}"; do
        [ -x "$cand" ] || continue
        if is_clt_stub "$cand"; then
            say "Overgeslagen: $cand (Command Line Tools niet geïnstalleerd)"
            continue
        fi
        if ! python_ok "$cand"; then
            say "Overgeslagen: $cand (versie $(python_version "$cand") is te oud of mist venv)"
            continue
        fi
        minor=$(python_minor "$cand")
        if [ -n "$minor" ] && [ "$minor" -gt "$MAX_TESTED_MINOR" ]; then
            say "Bewaard als reserve: $cand ($(python_version "$cand") is nieuwer dan getest)"
            [ -n "$too_new" ] || too_new="$cand"
            continue
        fi
        PY="$cand"
        say "Python gevonden: $cand ($(python_version "$cand"))"
        return 0
    done
    if [ -n "$too_new" ]; then
        PY="$too_new"
        say "Alleen een nieuwere Python gevonden, die wordt geprobeerd: $too_new ($(python_version "$too_new"))"
        return 0
    fi
    return 1
}

no_python() {
    python_dialog "Weerbot heeft Python nodig, maar er is geen geschikte versie (3.10 of nieuwer) op deze computer gevonden.

Klik op \"Download Python\", download de \"macOS 64-bit universal2 installer\" van Python $PY_ADVICE, installeer die en start Weerbot daarna opnieuw."
    release_lock
    exit 1
}

# ------------------------------------------------------------------ venv ---

VENV="$WEATHERBOT_HOME/venv"
VPY="$VENV/bin/python"
# Het stempel bevat de hash van requirements.txt en het soort Python
# (processor + versie, bijvoorbeeld arm64-py3.14). Past dat niet meer, dan
# wordt de venv opnieuw gemaakt (een venv voor Intel werkt niet op Apple-chip).
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

venv_tag() {
    "$VPY" -c 'import platform, sys; print("%s-py%d.%d" % (platform.machine(), sys.version_info[0], sys.version_info[1]))' 2>/dev/null
}

pip_ok() {
    "$VPY" -m pip --version >/dev/null 2>&1
}

make_venv() {
    if [ -z "$PY" ]; then
        find_python || no_python
    fi
    rm -rf "$VENV"
    say "Venv maken in $VENV"
    if ! "$PY" -m venv "$VENV"; then
        rm -rf "$VENV"
        fail "Het maken van de Python-omgeving voor Weerbot is mislukt."
    fi
    if ! pip_ok; then
        rm -rf "$VENV"
        fail "Het maken van de Python-omgeving voor Weerbot is mislukt (pip ontbreekt in $PY)."
    fi
}

# Is PyPI (waar pip de onderdelen haalt) onbereikbaar?
pypi_unreachable() {
    have curl || return 1
    ! curl -fsS --max-time 10 -o /dev/null https://pypi.org/simple/pip/ 2>/dev/null
}

# Installeer requirements.txt; bij een fout een passende uitleg.
install_requirements() {
    say "Onderdelen installeren uit $REQ"
    "$VPY" -m pip install --disable-pip-version-check --quiet --upgrade pip \
        || say "Waarschuwing: pip bijwerken mislukt, ga door met de huidige versie."
    pip_out=$(mktemp "${TMPDIR:-/tmp}/weerbot-pip.XXXXXX" 2>/dev/null) || pip_out="$WEATHERBOT_HOME/.pip-uitvoer.txt"
    # --only-binary: nooit zelf iets compileren (dat vraagt om Xcode-tools).
    "$VPY" -m pip install --disable-pip-version-check --progress-bar off \
        --only-binary=:all: -r "$REQ" 2>&1 | tee "$pip_out"
    pip_rc=${PIPESTATUS[0]}
    if [ "$pip_rc" -eq 0 ]; then
        rm -f "$pip_out"
        return 0
    fi

    rm -f "$STAMP"
    py_ver=$("$VPY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
    py_minor=${py_ver#*.}
    if grep -Eqi 'NewConnectionError|Max retries exceeded|Failed to establish|name resolution|nodename nor servname|Name or service not known|Network is unreachable|Connection refused|Connection reset|ConnectTimeout|ReadTimeout|timed out|SSLError|CERTIFICATE_VERIFY_FAILED|ProxyError' "$pip_out" \
       || { grep -q 'from versions: none' "$pip_out" && pypi_unreachable; }; then
        rm -f "$pip_out"
        fail "Het installeren van de onderdelen van Weerbot is mislukt: er was geen verbinding met internet.

Controleer je internetverbinding en start Weerbot opnieuw."
    fi
    if grep -Eqi 'No matching distribution|ResolutionImpossible|Could not find a version|conflicting dependencies|not a supported wheel' "$pip_out" \
       && [ -n "$py_minor" ] && [ "$py_minor" -gt "$MAX_TESTED_MINOR" ] 2>/dev/null; then
        rm -f "$pip_out"
        rm -rf "$VENV"
        python_dialog "$(log_hint "Python $py_ver is te nieuw voor een onderdeel van Weerbot.

Klik op \"Download Python\", installeer Python $PY_ADVICE (de \"macOS 64-bit universal2 installer\") en start Weerbot daarna opnieuw. Python $py_ver mag je gewoon laten staan.")"
        release_lock
        exit 1
    fi
    rm -f "$pip_out"
    # Onbekende fout: begin de volgende keer met een schone Python-omgeving.
    rm -rf "$VENV"
    fail "Het installeren van de onderdelen van Weerbot is mislukt.

Start Weerbot opnieuw; hij maakt dan een nieuwe Python-omgeving. Lukt het dan nog steeds niet, kijk dan in het logbestand."
}

acquire_lock
if find_running; then
    say "Weerbot is intussen gestart door een andere start."
    release_lock
    open_dashboard
    exit 0
fi

REQ_HASH=$(file_hash "$REQ")
REBUILD=""
NEED_INSTALL=""
if venv_ok; then
    TAG=$(venv_tag)
    OLD_HASH=""
    OLD_TAG=""
    if [ -f "$STAMP" ]; then
        read -r OLD_HASH OLD_TAG <"$STAMP"
    fi
    if [ "$OLD_TAG" != "$TAG" ]; then
        say "Venv past niet (stempel: '${OLD_TAG:-geen}', nu: '$TAG'); wordt opnieuw gemaakt."
        REBUILD=1
    elif [ "$OLD_HASH" != "$REQ_HASH" ]; then
        if pip_ok; then
            NEED_INSTALL=1
        else
            say "pip ontbreekt in de venv (eerder afgebroken installatie?); wordt opnieuw gemaakt."
            REBUILD=1
        fi
    fi
else
    REBUILD=1
    if [ -d "$VENV" ]; then
        say "Bestaande venv werkt niet (meer); wordt opnieuw gemaakt."
    fi
fi

if [ -n "$REBUILD" ]; then
    find_python || no_python
    if [ -d "$VENV" ]; then
        info_dialog "Weerbot wordt opnieuw geïnstalleerd. Dit duurt ongeveer een minuut; daarna opent het dashboard vanzelf in je browser."
    else
        info_dialog "Weerbot wordt voor de eerste keer geïnstalleerd. Dit duurt ongeveer een minuut; daarna opent het dashboard vanzelf in je browser."
    fi
    make_venv
    TAG=$(venv_tag)
    NEED_INSTALL=1
elif [ -n "$NEED_INSTALL" ]; then
    info_dialog "Weerbot wordt bijgewerkt, een ogenblik geduld…"
fi

if [ -n "$NEED_INSTALL" ]; then
    install_requirements
    printf '%s %s\n' "$REQ_HASH" "$TAG" >"$STAMP"
    say "Installatie klaar ($TAG)."
else
    say "Venv is up-to-date ($TAG)."
fi

# ----------------------------------------------------------------- start ---

# Werkmap = datamap: relatieve paden (zoals logs/) komen daar terecht en
# nooit in de (ondertekende) app-bundel.
cd "$WEATHERBOT_HOME" || fail "Kan niet naar de datamap gaan: $WEATHERBOT_HOME"
PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
PYTHONUNBUFFERED=1
# Bytecode buiten de app-bundel houden (anders klopt de handtekening niet meer).
PYTHONPYCACHEPREFIX="$WEATHERBOT_HOME/pycache"
export PYTHONPATH PYTHONUNBUFFERED PYTHONPYCACHEPREFIX WEATHERBOT_HOME

# Waar het logbestand stond toen Weerbot startte (om alleen nieuwe regels te bekijken).
LOG_MARK=$(wc -c <"$LOG_FILE" 2>/dev/null | tr -d ' ')
case "$LOG_MARK" in '' | *[!0-9]*) LOG_MARK=0 ;; esac

report_exit() {
    case "$1" in
        0 | 129 | 130 | 143)
            say "Weerbot is gestopt (code $1)."
            ;;
        *)
            # weatherbot.app toont bij bekende startfouten zelf al een venster
            # ("Weerbot kon niet starten ..."); dan geen tweede venster.
            if tail -c +"$((LOG_MARK + 1))" "$LOG_FILE" 2>/dev/null | grep -q 'Weerbot kon niet starten'; then
                say "FOUT: Weerbot is gestopt (code $1); de oorzaak is hierboven al gemeld."
                return 0
            fi
            error_dialog "$(log_hint "Weerbot is onverwacht gestopt (code $1).

Kijk in het logbestand wat er mis ging (onderaan staat de laatste melding). Heb je net een instelling in .env veranderd? Controleer die dan; gebruik bijvoorbeeld een punt in plaats van een komma in getallen.")"
            ;;
    esac
}

close_info_dialog
say "Start: $VPY -m weatherbot.app $*"

if [ -n "$FOREGROUND" ]; then
    # Terminal: Weerbot draait in dit venster; de vergrendeling blijft van dit
    # script tot Weerbot stopt. Ctrl-C of het venster sluiten stopt Weerbot.
    "$VPY" -m weatherbot.app "$@"
    rc=$?
    release_lock
    report_exit "$rc"
    exit "$rc"
fi

# App: start Weerbot los van dit script (eigen sessie, zodat hij blijft draaien
# als de app stopt) en wacht tot het dashboard antwoordt.
DETACH_PY='import os, signal, sys
try:
    os.setsid()
except OSError:
    pass
signal.signal(signal.SIGINT, signal.SIG_DFL)
os.execv(sys.argv[1], [sys.argv[1], "-m", "weatherbot.app"] + sys.argv[2:])'
"$VPY" -c "$DETACH_PY" "$VPY" "$@" </dev/null >>"$LOG_FILE" 2>&1 &
SERVER_PID=$!
write_lock_owner "$SERVER_PID"
LOCK_HELD=""    # de vergrendeling hoort nu bij het Weerbot-proces

waited=0
while [ "$waited" -lt "$START_WAIT" ]; do
    if find_running; then
        say "Weerbot draait op http://127.0.0.1:$RUNNING_PORT/ (proces $SERVER_PID)."
        exit 0
    fi
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 1
    waited=$((waited + 1))
done

if kill -0 "$SERVER_PID" 2>/dev/null; then
    say "Weerbot (proces $SERVER_PID) antwoordt na $START_WAIT seconden nog niet; hij start waarschijnlijk nog."
    exit 0
fi

wait "$SERVER_PID"
rc=$?
if [ "$(sed -n 1p "$LOCK_DIR/pid" 2>/dev/null)" = "$SERVER_PID" ]; then
    rm -rf "$LOCK_DIR"
fi
report_exit "$rc"
exit "$rc"
