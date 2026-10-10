#!/bin/bash
# Dubbelklik dit bestand in Finder om Weerbot te starten, zonder eerst een app te bouwen.
# Het dashboard opent daarna in je browser. Laat dit Terminal-venster open zolang
# Weerbot draait: venster sluiten = Weerbot stoppen.

HERE=$(cd -P "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd) || exit 1
WEERBOT_APP_DIR=$(cd -P "$HERE/.." && pwd) || exit 1
export WEERBOT_APP_DIR

printf '\n  Weerbot wordt gestart...\n'
printf '  Laat dit venster open zolang Weerbot draait (sluiten = stoppen).\n'
printf '  Stoppen kan ook met de knop "Afsluiten" in het dashboard.\n\n'

/bin/bash "$HERE/launcher.sh" "$@"
status=$?

if [ "$status" -ne 0 ]; then
    printf '\n  Er ging iets mis (code %s).\n' "$status"
    printf '  Het logbestand staat in ~/Library/Logs/Weerbot/launcher.log\n\n'
    if [ -t 0 ]; then
        printf '  Druk op Enter om dit venster te sluiten. '
        read -r _
    fi
fi
exit "$status"
