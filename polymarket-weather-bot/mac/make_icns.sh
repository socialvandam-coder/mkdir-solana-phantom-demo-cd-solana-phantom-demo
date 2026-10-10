#!/bin/bash
# Maakt een macOS-pictogram (.icns) van een vierkante PNG (liefst 1024x1024)
# met de ingebouwde macOS-tools sips en iconutil.
#
# Gebruik:  bash mac/make_icns.sh mac/icon.png uit/Weerbot.icns
# Exitcode: 0 = gelukt, 3 = sips/iconutil niet aanwezig (overgeslagen), 1 = fout.

src="$1"
out="$2"
if [ -z "$src" ] || [ -z "$out" ]; then
    echo "Gebruik: bash make_icns.sh <icon.png> <uit.icns>" >&2
    exit 1
fi
if [ ! -f "$src" ]; then
    echo "Pictogram niet gevonden: $src" >&2
    exit 1
fi
if ! command -v sips >/dev/null 2>&1 || ! command -v iconutil >/dev/null 2>&1; then
    exit 3
fi

tmp=$(mktemp -d "${TMPDIR:-/tmp}/weerbot-icon.XXXXXX") || exit 1
trap 'rm -rf "$tmp"' EXIT
set_dir="$tmp/Weerbot.iconset"
mkdir "$set_dir" || exit 1

for size in 16 32 128 256 512; do
    double=$((size * 2))
    sips -z "$size" "$size" "$src" --out "$set_dir/icon_${size}x${size}.png" >/dev/null || exit 1
    sips -z "$double" "$double" "$src" --out "$set_dir/icon_${size}x${size}@2x.png" >/dev/null || exit 1
done

mkdir -p "$(dirname "$out")" || exit 1
iconutil -c icns "$set_dir" -o "$out" || exit 1
exit 0
