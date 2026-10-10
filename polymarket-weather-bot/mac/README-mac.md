# Weerbot op de Mac

Weerbot draait op je Mac als een gewoon programma. Je bedient hem via een
dashboard in je browser (Safari, Chrome, ...) op het adres
**http://127.0.0.1:8765**. Dat adres werkt alleen op je eigen Mac; niemand
anders kan erbij.

Weerbot heeft geen icoon in het Dock: alles gebeurt in het browservenster.
Stoppen doe je met de knop **Afsluiten** in het dashboard.

> **Standaard handelt Weerbot niet echt.** Hij staat in *paper mode*: hij doet
> alsof en houdt bij wat hij gekocht en verkocht zou hebben, zonder geld te
> gebruiken. Echt handelen (*live*) weigert hij op plekken waar Polymarket dat
> niet toestaat. **In Nederland is Polymarket verboden**, dus daar blijft het
> bij paper mode.

Er zijn drie manieren om Weerbot te starten. Kies er één.

| Manier | Python nodig? | Wanneer kiezen |
|---|---|---|
| **A. Start Weerbot.command** | ja | het snelst: niets bouwen, gewoon dubbelklikken |
| **B. Weerbot.app bouwen** | ja | je wilt een echte app in je map Apps |
| **C. Kant-en-klare app downloaden** | nee | je wilt niets installeren (alleen Mac met Apple-chip M1 of nieuwer) |

---

## Eerst (voor A en B): Python installeren

1. Ga naar <https://www.python.org/downloads/release/python-3147/> (Python 3.14).
2. Klik onderaan bij *Files* op **macOS 64-bit universal2 installer** en open
   het gedownloade bestand. Klik steeds op *Ga door* / *Installeer*.
3. Klaar. Je hoeft verder niets met Python te doen.

Weerbot werkt met Python 3.10 tot en met 3.14. Neem **niet** de allernieuwste
Python (3.15 of hoger) die python.org bovenaan de downloadpagina zet: daarvoor
bestaat nog niet elk onderdeel van Weerbot. Een nieuwere 3.14-versie
(3.14.8, 3.14.9, ...) is ook goed.

Vergeet je dit, dan meldt Weerbot het zelf en biedt hij een knop
**Download Python** aan.

---

## A. Starten met "Start Weerbot.command"

1. Pak de projectmap uit (bijvoorbeeld in je map *Documenten*).
2. Open de map `polymarket-weather-bot` en daarin de map `mac`.
3. Dubbelklik op **Start Weerbot.command**.
   - Zegt macOS dat het bestand niet geopend kan worden? Zie
     [De eerste keer openen](#de-eerste-keer-openen-gatekeeper).
4. Er opent een Terminal-venster. **De eerste keer** installeert Weerbot zijn
   onderdelen; dat duurt ongeveer een minuut. Daarna opent het dashboard
   vanzelf in je browser.
5. **Laat het Terminal-venster open** zolang Weerbot draait. Venster sluiten =
   Weerbot stoppen.

---

## B. Weerbot.app bouwen

Hiermee maak je een echte app die je in je map *Apps* zet en daarna gewoon
met een dubbelklik start (ook vanuit het Dock of Launchpad).

1. Open **Terminal** (map Apps of Programma's → Hulpprogramma's → Terminal,
   of zoek met Cmd+Spatie naar "Terminal").
2. Typ `bash ` (met een spatie erachter), sleep het bestand
   `mac/build_app.sh` uit Finder naar het Terminal-venster, typ daarna
   ` --install` en druk op Enter. Het ziet er ongeveer zo uit:

   ```bash
   bash /Users/jouwnaam/Documenten/polymarket-weather-bot/mac/build_app.sh --install
   ```

3. Het script maakt `dist/Weerbot.app` in de projectmap en zet een kopie in
   je eigen map **Apps** (thuismap → Apps). Zonder `--install` blijft de app
   alleen in `dist/` staan; je kunt hem dan zelf naar *Apps* slepen.
   Er komt ook een zip (`dist/Weerbot-mac.zip`) om te bewaren of te delen.
4. Dubbelklik op **Weerbot**. De eerste keer verschijnt een venster dat
   Weerbot wordt geïnstalleerd (ongeveer een minuut). Daarna opent het
   dashboard. Dubbelklik je later nog eens op Weerbot terwijl hij al draait,
   dan opent alleen het dashboard opnieuw.

De app gebruikt de Python die je hierboven installeerde. Na een update van de
projectbestanden draai je stap 2 gewoon opnieuw.

---

## C. Kant-en-klare app downloaden (zonder Python)

GitHub bouwt automatisch een complete Weerbot.app waarin Python al zit.

1. Open (ingelogd op GitHub) deze pagina:
   <https://github.com/socialvandam-coder/mkdir-solana-phantom-demo-cd-solana-phantom-demo/actions/workflows/build-mac.yml>
2. Klik op de bovenste (nieuwste) groene run.
   - Er is nog geen run? Klik rechts op **Run workflow** en daarna nog eens op
     de groene knop **Run workflow**. Wacht ongeveer 10 minuten en ververs de
     pagina.
3. Download onderaan bij **Artifacts** het bestand **Weerbot-standalone-mac**.
   - Staat er bij de run geen **Artifacts**, of staat er *expired*? Downloads
     verlopen na 90 dagen. Klik dan op **Run workflow** zoals bij stap 2 en
     wacht ongeveer 10 minuten.
4. Dubbelklik het gedownloade zip-bestand. Daarin zit
   `Weerbot-standalone-mac.zip`: dubbelklik ook dat (het is twee keer
   ingepakt). Nu heb je **Weerbot.app**.
5. Sleep **Weerbot.app** naar je map **Apps** en dubbelklik erop.

Deze versie werkt op Macs met een Apple-chip (M1, M2, M3, M4 en nieuwer).
Heb je een oudere Mac met Intel-chip, gebruik dan manier A of B.
(Welke chip je hebt: Apple-menu linksboven → *Over deze Mac*.)

---

## De eerste keer openen (Gatekeeper)

Weerbot komt niet uit de App Store en is niet bij Apple geregistreerd. Daarom
waarschuwt macOS de eerste keer, bijvoorbeeld *"Weerbot kan niet worden
geopend omdat de ontwikkelaar niet kan worden geverifieerd"*. Dat los je zo op:

1. **Rechtermuisknop → Open**: klik met de rechtermuisknop (of houd Control
   ingedrukt en klik) op Weerbot (of op *Start Weerbot.command*) en kies
   **Open**. Klik in het venster dat volgt nogmaals op **Open**.
2. Werkt dat niet (op nieuwere macOS-versies kan dat): probeer eerst gewoon te
   openen, en ga daarna naar **Systeeminstellingen → Privacy en beveiliging**.
   Scroll naar beneden; daar staat een melding over Weerbot met de knop
   **Open toch** (Engels: *Open Anyway*). Klik erop en bevestig met je
   wachtwoord of Touch ID. Die knop staat er ongeveer een uur nadat je
   Weerbot probeerde te openen.
3. Zegt macOS dat de app "beschadigd" is? Open Terminal en typ (pas het pad
   aan als de app ergens anders staat):

   ```bash
   xattr -dr com.apple.quarantine ~/Applications/Weerbot.app /Applications/Weerbot.app 2>/dev/null
   ```

Dit hoeft maar één keer; daarna start Weerbot gewoon met een dubbelklik.

Vraagt Terminal om toegang tot je map *Documenten* of *Downloads*? Klik op
**Sta toe**; anders kan Weerbot zijn eigen bestanden niet lezen.

---

## Dagelijks gebruik

- **Starten:** dubbelklik op Weerbot (of *Start Weerbot.command*).
- **Dashboard weer openen** als je het browservenster per ongeluk hebt
  gesloten: Weerbot blijft op de achtergrond doorwerken.
  - Manier A en B: dubbelklik nog eens op *Start Weerbot.command* of op
    Weerbot. Dan opent alleen het dashboard opnieuw.
  - Manier C: nog eens dubbelklikken doet niets zolang Weerbot draait (macOS
    start een app die al draait niet nog een keer). Ga in je browser naar
    **http://127.0.0.1:8765**. Maak er de eerste keer een bladwijzer van.
- **Stoppen:** knop **Afsluiten** in het dashboard. Bij manier A kun je ook
  het Terminal-venster sluiten.
- **Instellingen** (bedragen, steden, ...) pas je aan in het dashboard; ze
  worden bewaard in het bestand `.env` in de datamap (zie hieronder). Een
  eventuele wallet-sleutel voor live handelen bewaart Weerbot in de
  macOS-sleutelhanger, niet in een gewoon bestand.
- Zolang Weerbot draait, voorkomt hij dat je Mac vanzelf in sluimerstand gaat.
  Doe je de klep van een MacBook dicht, dan slaapt hij toch (en stopt de bot
  tijdelijk).

---

## Waar staan de gegevens?

| Wat | Waar |
|---|---|
| Instellingen (`.env`), logboeken (`logs/weerbot.log`), Python-omgeving (`venv/`, alleen bij A en B) | `~/Library/Application Support/Weerbot` |
| Logboek van het opstarten (`launcher.log`, bij app C: `app.log`) | `~/Library/Logs/Weerbot` |

`~` betekent je thuismap. Deze mappen zijn standaard verborgen. Zo open je ze:
in Finder kies je **Ga → Ga naar map...** (Cmd+Shift+G) en plak je het pad,
bijvoorbeeld `~/Library/Application Support/Weerbot`.

Gaat er iets mis bij het starten, dan staat de uitleg in
`~/Library/Logs/Weerbot/launcher.log` (bij manier C: `app.log` in dezelfde
map). Weerbot toont bij een fout ook een knop **Toon logbestand**.

---

## Weerbot verwijderen

1. Stop Weerbot (knop **Afsluiten** in het dashboard).
2. Gooi **Weerbot.app** in de prullenmand (uit je map *Apps* of
   thuismap → *Apps*). Bij manier A gooi je de projectmap weg.
3. Gooi ook deze twee mappen weg (via **Ga → Ga naar map...**):
   - `~/Library/Application Support/Weerbot` (instellingen, logboeken en de
     Python-omgeving; **let op:** hier staat ook je `.env` met eventuele
     sleutels)
   - `~/Library/Logs/Weerbot`
4. Heb je ooit een wallet-sleutel ingevuld? Open dan **Sleutelhangertoegang**
   (Cmd+Spatie, typ "Sleutelhanger"), zoek op **Weerbot wallet key** en
   verwijder dat item. Of typ in Terminal:
   `security delete-generic-password -s "Weerbot wallet key" -a weerbot`
5. Leeg de prullenmand.

Python zelf mag je gewoon laten staan.

---

## Problemen oplossen

| Probleem | Oplossing |
|---|---|
| Melding "Weerbot heeft Python nodig" | Installeer Python 3.14 (zie boven) en start opnieuw. |
| Melding "Python 3.15 is te nieuw" (of een hoger nummer) | Installeer daarnaast Python 3.14 (zie boven) en start opnieuw. De nieuwe Python mag blijven staan. |
| "Het installeren van de onderdelen is mislukt" | Lees de uitleg in het venster: controleer je internetverbinding, of start gewoon opnieuw (Weerbot maakt dan een nieuwe Python-omgeving). Details in `launcher.log`. |
| "Weerbot is onverwacht gestopt" | Klik op **Toon logbestand**; onderaan staat de oorzaak. Vaak is het een instelling in `.env` (gebruik een punt in getallen: `2.5`, niet `2,5`). |
| Dubbelklikken doet niets zichtbaars | Weerbot draait waarschijnlijk al: open **http://127.0.0.1:8765** in je browser. Gebeurt er dan nog niets, dan is hij misschien meteen gestopt: kijk in `launcher.log` (bij manier C: `app.log`). |
| Dashboard staat niet op 8765 | Was poort 8765 bezet door een ander programma, dan kiest Weerbot zelf de volgende vrije poort (8766, 8767, ...). Het juiste adres staat in `~/Library/Application Support/Weerbot/logs/weerbot.log`. |
| Weerbot opnieuw laten installeren | Verwijder de map `~/Library/Application Support/Weerbot/venv` en start opnieuw. |

## Voor techneuten

- `mac/launcher.sh` zoekt Python in deze volgorde: `$WEERBOT_PYTHON`,
  `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`,
  `/Library/Frameworks/Python.framework/Versions/3.14 … 3.10/bin/python3`,
  daarna `python3` in het PATH (`/usr/bin/python3` alleen als de Command Line
  Tools zijn geïnstalleerd). De eerste met versie 3.10 t/m 3.14 wint; een
  nieuwere Python wordt alleen gebruikt als er niets anders is. Het maakt een
  venv in de datamap en installeert `requirements.txt` (alleen kant-en-klare
  wheels, `--only-binary=:all:`) opnieuw zodra dat bestand verandert. Het
  stempel in de venv bevat ook processor en Python-versie
  (`arm64-py3.14`); past dat niet, dan wordt de venv opnieuw gemaakt.
- Daarna start het `python -m weatherbot.app` (opties `--port N` en
  `--no-browser` worden doorgegeven, `-psn_…` van Finder niet), met de datamap
  als werkmap. Draait er al een Weerbot (antwoord op `/api/ping` op poort
  N t/m N+9), dan opent het alleen het dashboard.
- In Terminal blijft het script draaien zolang Weerbot draait. Als app start
  het Weerbot in een eigen sessie, wacht tot `/api/ping` antwoordt en stopt
  dan. De vergrendeling `.starter.lock` in de datamap hoort daarna bij het
  Weerbot-proces, zodat er nooit twee tegelijk draaien.
- Het startprogramma van Weerbot.app (manier B) is een AppleScript-programma
  gemaakt met `osacompile` (Apple-chip en Intel, dus geen Rosetta nodig) dat
  `Contents/Resources/launcher.sh` start.
- `mac/build_standalone.sh` bouwt de app van manier C met PyInstaller; de
  GitHub-workflow staat in `.github/workflows/build-mac.yml`.
- `mac/make_icon.py` tekent `mac/icon.png` opnieuw (alleen standaard-Python).
