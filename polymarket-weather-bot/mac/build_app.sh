#!/bin/bash
# Bouwt Weerbot.app (en een zip om te delen) uit deze projectmap.
#
# Gebruik (in Terminal, vanuit elke map):
#   bash mac/build_app.sh             -> dist/Weerbot.app en dist/Weerbot-mac.zip
#   bash mac/build_app.sh --install   -> idem, en kopieert de app naar ~/Applications
#
# De app bevat de programmabestanden; Python zelf moet op de Mac staan
# (de app helpt bij het downloaden als het ontbreekt). Wil je een app zonder
# Python-installatie, gebruik dan mac/build_standalone.sh.
#
# Geschreven voor bash 3.2 en BSD-tools (standaard op macOS).

VERSION="1.0"
BUNDLE_ID="local.weerbot.app"

info() { printf '%s\n' "$*"; }
warn() { printf 'Let op: %s\n' "$*" >&2; }
die() { printf 'FOUT: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'EOF'
Gebruik: bash mac/build_app.sh [--install]

  --install   kopieer Weerbot.app na het bouwen naar ~/Applications
  -h, --help  deze uitleg
EOF
}

INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=1 ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "onbekende optie: $arg" ;;
    esac
done

SCRIPT_DIR=$(cd -P "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd) || die "kan scriptmap niet bepalen"
PROJECT_DIR=$(cd -P "$SCRIPT_DIR/.." && pwd) || die "kan projectmap niet bepalen"

DIST="$PROJECT_DIR/dist"
APP="$DIST/Weerbot.app"
CONTENTS="$APP/Contents"
RES="$CONTENTS/Resources"
ZIP="$DIST/Weerbot-mac.zip"

info "Weerbot.app bouwen"
info "  projectmap: $PROJECT_DIR"

# ------------------------------------------------------------ controles ---

[ -d "$PROJECT_DIR/weatherbot" ] || die "map weatherbot/ niet gevonden in $PROJECT_DIR"
[ -f "$PROJECT_DIR/requirements.txt" ] || die "requirements.txt niet gevonden in $PROJECT_DIR"
[ -f "$SCRIPT_DIR/launcher.sh" ] || die "mac/launcher.sh niet gevonden"
bash -n "$SCRIPT_DIR/launcher.sh" || die "mac/launcher.sh bevat een syntaxfout"
[ -f "$PROJECT_DIR/weatherbot/app.py" ] || warn "weatherbot/app.py ontbreekt: de app kan dan niet starten."
[ -d "$PROJECT_DIR/weatherbot/static" ] || warn "weatherbot/static/ ontbreekt: het dashboard heeft dan geen pagina's."

# ------------------------------------------------------------- bundel ---

rm -rf "$APP" || die "kan oude $APP niet verwijderen"
mkdir -p "$CONTENTS/MacOS" "$RES/app" || die "kan $APP niet maken"

cat >"$CONTENTS/Info.plist" <<EOF || die "kan Info.plist niet schrijven"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key>
	<string>nl</string>
	<key>CFBundleName</key>
	<string>Weerbot</string>
	<key>CFBundleDisplayName</key>
	<string>Weerbot</string>
	<key>CFBundleIdentifier</key>
	<string>${BUNDLE_ID}</string>
	<key>CFBundleExecutable</key>
	<string>Weerbot</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleSignature</key>
	<string>????</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleShortVersionString</key>
	<string>${VERSION}</string>
	<key>CFBundleVersion</key>
	<string>${VERSION}</string>
	<key>CFBundleIconFile</key>
	<string>Weerbot</string>
	<key>LSMinimumSystemVersion</key>
	<string>11.0</string>
	<key>LSUIElement</key>
	<true/>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>LSApplicationCategoryType</key>
	<string>public.app-category.finance</string>
</dict>
</plist>
EOF
printf 'APPL????' >"$CONTENTS/PkgInfo"
if have plutil; then
    plutil -lint "$CONTENTS/Info.plist" >/dev/null || die "Info.plist is ongeldig"
fi

cp "$SCRIPT_DIR/launcher.sh" "$CONTENTS/MacOS/Weerbot" || die "kan launcher niet kopiëren"
chmod 755 "$CONTENTS/MacOS/Weerbot"

# Programmabestanden (nooit je eigen .env meenemen: daar kan je sleutel in staan).
cp -R "$PROJECT_DIR/weatherbot" "$RES/app/weatherbot" || die "kan weatherbot/ niet kopiëren"
cp "$PROJECT_DIR/requirements.txt" "$RES/app/requirements.txt" || die "kan requirements.txt niet kopiëren"
if [ -f "$PROJECT_DIR/.env.example" ]; then
    cp "$PROJECT_DIR/.env.example" "$RES/app/.env.example"
fi
find "$RES/app" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$RES/app" -type f \( -name '*.pyc' -o -name '.DS_Store' -o -name '.env' \) -exec rm -f {} +

# -------------------------------------------------------------- pictogram ---

ICON_PNG="$SCRIPT_DIR/icon.png"
if [ ! -f "$ICON_PNG" ] && have python3; then
    python3 "$SCRIPT_DIR/make_icon.py" "$ICON_PNG" >/dev/null 2>&1 || true
fi
if [ -f "$ICON_PNG" ]; then
    bash "$SCRIPT_DIR/make_icns.sh" "$ICON_PNG" "$RES/Weerbot.icns"
    case $? in
        0) info "  pictogram: Weerbot.icns gemaakt" ;;
        3) warn "sips/iconutil niet gevonden (geen Mac?): pictogram overgeslagen." ;;
        *) warn "pictogram maken mislukt: pictogram overgeslagen." ;;
    esac
else
    warn "mac/icon.png ontbreekt: pictogram overgeslagen."
fi

# ------------------------------------------------- quarantaine + ondertekenen ---

if have xattr; then
    xattr -cr "$APP" 2>/dev/null || warn "xattr -cr gaf een fout (niet erg)."
else
    warn "xattr niet gevonden: quarantaine-vlag niet verwijderd."
fi
if have codesign; then
    if codesign --force --deep -s - "$APP" >/dev/null 2>&1; then
        info "  ondertekend (ad-hoc)"
    else
        warn "ondertekenen met codesign is mislukt; de app werkt meestal toch."
    fi
else
    warn "codesign niet gevonden: app niet ondertekend."
fi

# ------------------------------------------------------------------- zip ---

rm -f "$ZIP"
if have ditto; then
    ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP" || warn "zip maken mislukt."
elif have zip; then
    (cd "$DIST" && zip -q -r -y "$(basename "$ZIP")" "Weerbot.app") || warn "zip maken mislukt."
else
    warn "ditto en zip niet gevonden: geen zip-bestand gemaakt."
fi

# --------------------------------------------------------------- installeren ---

FINAL="$APP"
if [ "$INSTALL" = 1 ]; then
    DEST_DIR="$HOME/Applications"
    mkdir -p "$DEST_DIR" || die "kan $DEST_DIR niet maken"
    rm -rf "$DEST_DIR/Weerbot.app" || die "kan oude $DEST_DIR/Weerbot.app niet verwijderen"
    if have ditto; then
        ditto "$APP" "$DEST_DIR/Weerbot.app" || die "kopiëren naar $DEST_DIR mislukt"
    else
        cp -R "$APP" "$DEST_DIR/Weerbot.app" || die "kopiëren naar $DEST_DIR mislukt"
    fi
    FINAL="$DEST_DIR/Weerbot.app"
fi

# ------------------------------------------------------------- uitleg ---

info ""
info "Klaar! De app staat hier:"
info "  $FINAL"
if [ -f "$ZIP" ]; then
    info "Zip om te delen of te bewaren:"
    info "  $ZIP"
fi
info ""
info "Volgende stappen:"
if [ "$INSTALL" = 1 ]; then
    info "  1. Open in Finder je thuismap > Apps (Applications) en dubbelklik op Weerbot."
    info "     Tip: sleep Weerbot naar het Dock om hem later makkelijk te starten."
else
    info "  1. Sleep Weerbot.app naar de map Apps (of draai dit script opnieuw met --install)"
    info "     en dubbelklik erop."
fi
info "  2. De eerste keer installeert Weerbot zijn onderdelen (ongeveer een minuut, je krijgt een"
info "     melding). Daarna opent het dashboard in je browser: http://127.0.0.1:8765"
info "  3. Waarschuwt macOS dat de app niet geopend kan worden? Klik dan met de rechtermuisknop"
info "     (of Control-klik) op Weerbot en kies 'Open'. Lukt dat niet: Systeeminstellingen >"
info "     Privacy en beveiliging > 'Toch openen'."
info "  4. Stoppen doe je met de knop 'Afsluiten' in het dashboard."
info ""
info "Nodig op de Mac: Python 3.10 of nieuwer (liefst 3.12 van python.org)."
info "Ontbreekt Python, dan toont Weerbot een knop om het te downloaden."
info "Logboek: ~/Library/Logs/Weerbot/launcher.log"
