#!/bin/bash
# Bouwt een zelfstandige Weerbot.app met PyInstaller: Python en alle onderdelen
# zitten erin, dus op de Mac van de gebruiker hoeft niets geïnstalleerd te worden.
#
# Gebruik (op een Mac, of in GitHub Actions):
#   bash mac/build_standalone.sh
# Uitvoer:
#   dist/standalone/Weerbot.app
#   dist/Weerbot-standalone-mac.zip
#
# Let op: de app werkt alleen op hetzelfde soort Mac als waarop hij gebouwd is
# (Apple Silicon M1/M2/M3/M4 of Intel).
#
# Omgevingsvariabelen (optioneel):
#   PYTHON            Python om mee te bouwen (standaard python3)
#   PYINSTALLER_SPEC  pip-specificatie voor PyInstaller (standaard "pyinstaller>=6,<7")

VERSION="1.0"
BUNDLE_ID="local.weerbot.app"

info() { printf '%s\n' "$*"; }
warn() { printf 'Let op: %s\n' "$*" >&2; }
die() { printf 'FOUT: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

SCRIPT_DIR=$(cd -P "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd) || die "kan scriptmap niet bepalen"
PROJECT_DIR=$(cd -P "$SCRIPT_DIR/.." && pwd) || die "kan projectmap niet bepalen"
PYTHON="${PYTHON:-python3}"
PYINSTALLER_SPEC="${PYINSTALLER_SPEC:-pyinstaller>=6,<7}"
OUT_DIR="$PROJECT_DIR/dist/standalone"
APP="$OUT_DIR/Weerbot.app"
ZIP="$PROJECT_DIR/dist/Weerbot-standalone-mac.zip"

info "Zelfstandige Weerbot.app bouwen"
info "  projectmap: $PROJECT_DIR"

# ------------------------------------------------------------ controles ---

have "$PYTHON" || die "Python niet gevonden ($PYTHON). Zet PYTHON=/pad/naar/python3."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10 of nieuwer is nodig om te bouwen ($PYTHON is te oud)."
[ -f "$PROJECT_DIR/requirements.txt" ] || die "requirements.txt niet gevonden"
[ -f "$PROJECT_DIR/weatherbot/app.py" ] || die "weatherbot/app.py niet gevonden"
[ -d "$PROJECT_DIR/weatherbot/static" ] || die "weatherbot/static/ niet gevonden (dashboardbestanden)"
[ -f "$SCRIPT_DIR/pyi_entry.py" ] || die "mac/pyi_entry.py niet gevonden"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/weerbot-build.XXXXXX") || die "kan tijdelijke map niet maken"
trap 'rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------- venv ---

info "Tijdelijke Python-omgeving maken..."
"$PYTHON" -m venv "$WORK/venv" || die "venv maken mislukt"
VPY="$WORK/venv/bin/python"
"$VPY" -m pip install --disable-pip-version-check --quiet --upgrade pip || warn "pip bijwerken mislukt"
info "Onderdelen en PyInstaller installeren..."
"$VPY" -m pip install --disable-pip-version-check --progress-bar off \
    -r "$PROJECT_DIR/requirements.txt" "$PYINSTALLER_SPEC" || die "pip install mislukt"

# -------------------------------------------------------------- pictogram ---

ICON_ARGS=""
ICON_PNG="$SCRIPT_DIR/icon.png"
if [ ! -f "$ICON_PNG" ]; then
    "$VPY" "$SCRIPT_DIR/make_icon.py" "$ICON_PNG" >/dev/null || warn "pictogram tekenen mislukt"
fi
if [ -f "$ICON_PNG" ]; then
    bash "$SCRIPT_DIR/make_icns.sh" "$ICON_PNG" "$WORK/Weerbot.icns"
    case $? in
        0) ICON_ARGS="$WORK/Weerbot.icns" ;;
        3) warn "sips/iconutil niet gevonden (geen Mac?): bouwen zonder pictogram." ;;
        *) warn "pictogram maken mislukt: bouwen zonder pictogram." ;;
    esac
fi

# ----------------------------------------------------------- PyInstaller ---

info "PyInstaller draaien..."
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR" || die "kan $OUT_DIR niet maken"
(
    cd "$PROJECT_DIR" || exit 1
    "$VPY" -m PyInstaller \
        --noconfirm --clean \
        --windowed \
        --name Weerbot \
        --osx-bundle-identifier "$BUNDLE_ID" \
        ${ICON_ARGS:+--icon "$ICON_ARGS"} \
        --distpath "$OUT_DIR" \
        --workpath "$WORK/build" \
        --specpath "$WORK/spec" \
        --paths "$PROJECT_DIR" \
        --add-data "$PROJECT_DIR/weatherbot/static:weatherbot/static" \
        --collect-submodules weatherbot \
        --collect-data tzdata \
        --collect-submodules py_clob_client \
        --hidden-import weatherbot.app \
        "$SCRIPT_DIR/pyi_entry.py"
) || die "PyInstaller is mislukt"

# ---------------------------------------------------------- Info.plist ---

set_plist() { # sleutel type waarde
    plist="$APP/Contents/Info.plist"
    if [ -x /usr/libexec/PlistBuddy ]; then
        /usr/libexec/PlistBuddy -c "Set :$1 $3" "$plist" >/dev/null 2>&1 \
            || /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$plist" >/dev/null \
            || die "kan $1 niet in Info.plist zetten"
    else
        "$VPY" - "$plist" "$1" "$2" "$3" <<'PYEOF' || die "kan $1 niet in Info.plist zetten"
import plistlib, sys
path, key, kind, value = sys.argv[1:5]
with open(path, "rb") as fh:
    data = plistlib.load(fh)
data[key] = (value == "true") if kind == "bool" else value
with open(path, "wb") as fh:
    plistlib.dump(data, fh)
PYEOF
    fi
}

if [ -d "$APP" ]; then
    set_plist LSUIElement bool true
    set_plist CFBundleDisplayName string Weerbot
    set_plist CFBundleShortVersionString string "$VERSION"
    set_plist CFBundleVersion string "$VERSION"
    set_plist LSMinimumSystemVersion string 11.0
    set_plist NSHighResolutionCapable bool true
    info "  Info.plist bijgewerkt (LSUIElement: geen Dock-icoon, het dashboard draait in de browser)"
    BIN="$APP/Contents/MacOS/Weerbot"
else
    warn "Geen Weerbot.app gemaakt (dit is geen Mac); alleen de map $OUT_DIR/Weerbot is getest."
    BIN="$OUT_DIR/Weerbot/Weerbot"
fi

# --------------------------------------------------------- ondertekenen ---

if [ -d "$APP" ]; then
    if have xattr; then
        xattr -cr "$APP" 2>/dev/null || true
    fi
    if have codesign; then
        codesign --force --deep -s - "$APP" || die "ad-hoc ondertekenen mislukt"
        codesign --verify --deep --strict "$APP" || warn "codesign --verify meldt een probleem; de app start meestal toch."
        info "  ondertekend (ad-hoc)"
    else
        warn "codesign niet gevonden: app niet ondertekend."
    fi
fi

# ----------------------------------------------------------- zelftest ---

info "Zelftest van de gebouwde app..."
WEERBOT_SELFTEST=1 WEATHERBOT_HOME="$WORK/selftest-home" "$BIN" || die "zelftest mislukt: de bundel is niet compleet"

# ------------------------------------------------------------------ zip ---

if [ -d "$APP" ]; then
    rm -f "$ZIP"
    if have ditto; then
        ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP" || die "zip maken mislukt"
    elif have zip; then
        (cd "$OUT_DIR" && zip -q -r -y "$ZIP" "Weerbot.app") || die "zip maken mislukt"
    else
        die "ditto en zip niet gevonden"
    fi
    info ""
    info "Klaar!"
    info "  app: $APP"
    info "  zip: $ZIP"
    info "Pak de zip uit en sleep Weerbot.app naar de map Apps. Eerste keer openen:"
    info "rechtermuisknop > Open, of Systeeminstellingen > Privacy en beveiliging > 'Open toch'."
else
    info ""
    info "Klaar (testbuild, geen Mac): $OUT_DIR/Weerbot"
fi
