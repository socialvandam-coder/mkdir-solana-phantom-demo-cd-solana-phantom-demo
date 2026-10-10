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
# Het startprogramma van de app is een klein AppleScript-programma (gemaakt met
# osacompile, dat op elke Mac staat). Dat is een echt Mac-programma voor zowel
# Apple-chip als Intel, dus macOS vraagt niet om Rosetta. Het start
# Contents/Resources/launcher.sh en stopt daarna weer.
# Zonder osacompile (bijvoorbeeld op Linux, om te testen) wordt een
# eenvoudige noodversie gebouwd met een shell-script als startprogramma.
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
PLIST="$CONTENTS/Info.plist"
ZIP="$DIST/Weerbot-mac.zip"

info "Weerbot.app bouwen"
info "  projectmap: $PROJECT_DIR"

# ------------------------------------------------------------ controles ---

[ -d "$PROJECT_DIR/weatherbot" ] || die "map weatherbot/ niet gevonden in $PROJECT_DIR"
[ -f "$PROJECT_DIR/requirements.txt" ] || die "requirements.txt niet gevonden in $PROJECT_DIR"
[ -f "$PROJECT_DIR/weatherbot/app.py" ] || die "weatherbot/app.py niet gevonden: de app zou niet kunnen starten"
[ -d "$PROJECT_DIR/weatherbot/static" ] || die "weatherbot/static/ niet gevonden: het dashboard zou geen pagina's hebben"
[ -f "$SCRIPT_DIR/launcher.sh" ] || die "mac/launcher.sh niet gevonden"
bash -n "$SCRIPT_DIR/launcher.sh" || die "mac/launcher.sh bevat een syntaxfout"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/weerbot-app.XXXXXX") || die "kan tijdelijke map niet maken"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DIST" || die "kan $DIST niet maken"
rm -rf "$APP" || die "kan oude $APP niet verwijderen"

# ------------------------------------------------------------- bundel ---

# AppleScript-startprogramma. "do shell script" wacht tot launcher.sh klaar is;
# die start Weerbot los en stopt zodra het dashboard antwoordt (of na een
# foutmelding). "try" voorkomt een Engelse AppleScript-foutmelding: de
# starter toont zelf al een Nederlandse.
cat >"$WORK/Weerbot.applescript" <<'EOF' || die "kan het AppleScript niet schrijven"
on run
	set appPath to POSIX path of (path to me)
	if appPath does not end with "/" then set appPath to appPath & "/"
	set launcher to appPath & "Contents/Resources/launcher.sh"
	try
		with timeout of 86400 seconds
			do shell script "/bin/bash " & quoted form of launcher & " </dev/null >/dev/null 2>&1"
		end timeout
	end try
end run
EOF

# Sleutel in Info.plist zetten (alleen nodig voor de osacompile-versie).
plist_set() { # sleutel type waarde   (type: string, bool of xml)
    plutil -replace "$1" -"$2" "$3" "$PLIST" || die "kan $1 niet in Info.plist zetten"
}

write_script_plist() {
    cat >"$PLIST" <<EOF || die "kan Info.plist niet schrijven"
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
	<key>LSArchitecturePriority</key>
	<array>
		<string>arm64</string>
		<string>x86_64</string>
	</array>
	<key>LSUIElement</key>
	<true/>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>LSApplicationCategoryType</key>
	<string>public.app-category.finance</string>
</dict>
</plist>
EOF
}

NATIVE=0
if have osacompile; then
    osacompile -o "$APP" "$WORK/Weerbot.applescript" || die "osacompile kon het startprogramma niet maken"
    [ -f "$PLIST" ] || die "osacompile maakte geen Info.plist"
    have plutil || die "plutil niet gevonden"
    plist_set CFBundleIdentifier string "$BUNDLE_ID"
    plist_set CFBundleName string Weerbot
    plist_set CFBundleDisplayName string Weerbot
    plist_set CFBundleDevelopmentRegion string nl
    plist_set CFBundleShortVersionString string "$VERSION"
    plist_set CFBundleVersion string "$VERSION"
    plist_set LSMinimumSystemVersion string 11.0
    plist_set LSUIElement bool YES
    plist_set NSHighResolutionCapable bool YES
    plist_set LSApplicationCategoryType string public.app-category.finance
    plist_set LSArchitecturePriority xml '<array><string>arm64</string><string>x86_64</string></array>'
    EXE=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$PLIST" 2>/dev/null)
    [ -n "$EXE" ] || EXE=applet
    [ -x "$CONTENTS/MacOS/$EXE" ] || die "startprogramma $CONTENTS/MacOS/$EXE ontbreekt"
    NATIVE=1
    info "  startprogramma: AppleScript ($EXE, Apple-chip en Intel)"
else
    warn "osacompile niet gevonden (geen Mac?): noodversie met een shell-script als startprogramma."
    warn "Zo'n app vraagt op een Mac met Apple-chip om Rosetta; bouw de app daarom op een Mac."
    mkdir -p "$CONTENTS/MacOS" "$RES" || die "kan $APP niet maken"
    write_script_plist
    printf 'APPL????' >"$CONTENTS/PkgInfo" || die "kan PkgInfo niet schrijven"
    cat >"$CONTENTS/MacOS/Weerbot" <<'EOF' || die "kan het startprogramma niet schrijven"
#!/bin/bash
# Noodstarter (app gebouwd zonder osacompile): start de echte starter.
here=$(cd -P "$(dirname "$0")/../Resources" && pwd) || exit 1
exec /bin/bash "$here/launcher.sh" "$@"
EOF
    chmod 755 "$CONTENTS/MacOS/Weerbot" || die "kan het startprogramma niet uitvoerbaar maken"
fi
if have plutil; then
    plutil -lint "$PLIST" >/dev/null || die "Info.plist is ongeldig"
fi

mkdir -p "$RES/app" || die "kan $RES/app niet maken"
cp "$SCRIPT_DIR/launcher.sh" "$RES/launcher.sh" || die "kan launcher niet kopiëren"
chmod 755 "$RES/launcher.sh" || die "kan launcher niet uitvoerbaar maken"

# Programmabestanden (nooit je eigen .env meenemen: daar kan je sleutel in staan).
cp -R "$PROJECT_DIR/weatherbot" "$RES/app/weatherbot" || die "kan weatherbot/ niet kopiëren"
cp "$PROJECT_DIR/requirements.txt" "$RES/app/requirements.txt" || die "kan requirements.txt niet kopiëren"
if [ -f "$PROJECT_DIR/.env.example" ]; then
    cp "$PROJECT_DIR/.env.example" "$RES/app/.env.example" || die "kan .env.example niet kopiëren"
fi
find "$RES/app" -type d -name __pycache__ -prune -exec rm -rf {} + || die "kan __pycache__ niet opruimen"
find "$RES/app" -type f \( -name '*.pyc' -o -name '.DS_Store' -o -name '.env' \) -exec rm -f {} + \
    || die "kan tijdelijke bestanden niet opruimen"

# -------------------------------------------------------------- pictogram ---

ICON_PNG="$SCRIPT_DIR/icon.png"
if [ ! -f "$ICON_PNG" ] && have python3; then
    python3 "$SCRIPT_DIR/make_icon.py" "$ICON_PNG" >/dev/null 2>&1 || true
fi
if [ -f "$ICON_PNG" ]; then
    if bash "$SCRIPT_DIR/make_icns.sh" "$ICON_PNG" "$RES/Weerbot.icns"; then
        icon_rc=0
    else
        icon_rc=$?
    fi
    case $icon_rc in
        0)
            if [ "$NATIVE" = 1 ]; then
                # Ons pictogram in plaats van dat van AppleScript.
                plist_set CFBundleIconFile string Weerbot
                plutil -remove CFBundleIconName "$PLIST" >/dev/null 2>&1 || true
                rm -f "$RES/Assets.car"
            fi
            info "  pictogram: Weerbot.icns gemaakt"
            ;;
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
    elif [ "$NATIVE" = 1 ]; then
        # Een programma met een kapotte handtekening start niet op een Apple-chip.
        die "ondertekenen met codesign is mislukt"
    else
        warn "ondertekenen met codesign is mislukt; de app werkt meestal toch."
    fi
elif [ "$NATIVE" = 1 ]; then
    die "codesign niet gevonden"
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
info "  2. De eerste keer installeert Weerbot zijn onderdelen (ongeveer een minuut, je ziet"
info "     een venster met uitleg). Daarna opent het dashboard in je browser: http://127.0.0.1:8765"
info "     Later nog eens op Weerbot dubbelklikken opent het dashboard opnieuw."
info "  3. Waarschuwt macOS dat de app niet geopend kan worden? Klik dan met de rechtermuisknop"
info "     (of Control-klik) op Weerbot en kies 'Open'. Lukt dat niet: Systeeminstellingen >"
info "     Privacy en beveiliging > 'Open toch'."
info "  4. Stoppen doe je met de knop 'Afsluiten' in het dashboard."
info ""
info "Nodig op de Mac: Python 3.10 tot en met 3.14 (liefst 3.14 van python.org)."
info "Ontbreekt Python, dan toont Weerbot een knop om het te downloaden."
info "Logboek: ~/Library/Logs/Weerbot/launcher.log"
