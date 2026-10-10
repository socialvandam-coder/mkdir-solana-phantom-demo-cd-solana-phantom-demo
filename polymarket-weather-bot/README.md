# Weerbot - Polymarket-weerbot (HighTempTation-strategie)

Deze bot kopieert de strategie van [HighTempTation](https://polymarket.com/@hightemptation) op de
dagelijkse temperatuurmarkten van Polymarket. Hij draait als **Mac-programma** met een dashboard in je
browser, of vanuit de terminal.

## Snel starten op de Mac

Zie [`mac/README-mac.md`](mac/README-mac.md) voor alle details. Kort:

1. Pak de map uit en dubbelklik **`mac/Start Weerbot.command`**
   (eerste keer: rechtermuisknop → *Open*). Of bouw een echte app met `mac/build_app.sh` → `Weerbot.app`.
2. De eerste keer installeert hij zichzelf (± een minuut). Daarna opent het dashboard in je browser op
   `http://127.0.0.1:8765`.
3. Kies onder **Instellingen** je **startbedrag**. De bot begint in **oefenmodus** (nepgeld).

Je gegevens (instellingen, saldo, logboek) staan in `~/Library/Application Support/Weerbot`.

## Het dashboard

- **Vandaag**: hoeveel procent de bot vandaag heeft gepakt, en hoeveel dollar.
- **Saldo** en **sinds start**: je bedrag nu en de groei sinds je startbedrag.
- **Volgende inzet**: hoe groot de volgende aankoop maximaal is. Die groeit mee met je saldo.
- **Saldo-verloop**: grafiek van je saldo (vandaag / 7 dagen / alles), met je startbedrag als lijn.
- **Winst per dag**: staafgrafiek van de laatste 14 dagen.
- **Wat hij vandaag pakte**: elke afgesloten trade met inzet, resultaat, procent en hoe lang hij liep.
- **Open posities**: per positie de aankoopprijs, het bod nu en een label: *verkoop staat op 0,99*,
  *bevestigd*, *ONBEVESTIGD* (rood: na 30 s nog niet verkocht) of *wacht op uitslag*.
- **Markten nu** (per stad de gemeten max/min en hoeveel vakken al "dood" zijn), **Laatste signalen**.
- **Gezondheid**: komt de weerdata binnen, zijn de markten geladen, wanneer was de laatste ronde.
- **Vergelijk met HighTempTation**: welke vakken hij kocht terwijl jouw bot draaide, en of jouw bot ze
  ook vond (eerder of later).
- **Instellingen**: startbedrag, percentages, risico, live-account (private key gaat in je
  Mac-sleutelhanger).

## Hoe hij groeit (zoals HighTempTation)

Elke aankoop is een **percentage van je huidige saldo** (standaard max 5% per aankoop). Wint de bot,
dan wordt het saldo groter en dus ook de volgende inzet. Verlies maakt de inzet weer kleiner. Het saldo
wordt bewaard, dus na herstarten gaat hij verder waar hij was. Groei wordt vooral beperkt door hoeveel
goedkope orders er liggen, niet door een hoger percentage.

Beveiligingen: max per vak per dag (`MARKET_PCT`), max per markt in nog niet verkochte posities
(`EVENT_PCT`), max totaal tegelijk in posities (`OPEN_PCT`), dollarlimieten (`MAX_TRADE_USDC`,
`MAX_EVENT_USDC`), een daglimiet voor verlies (`DAILY_LOSS_PCT`, open posities tellen mee) en een
noodrem bij 15% daling vanaf je hoogste stand (`MAX_DRAWDOWN_PCT`).

## Knoppen en automatische pauzes

- **Start / Pauzeer kopen**: pauzeren stopt alleen het **kopen**. Open posities worden gewoon verder
  verkocht en afgehandeld.
- **Noodstop**: koopt niets meer tot je weer op **Start** drukt. De verkooporders op 0,99 blijven staan,
  want dat is de uitgang.
- **Afsluiten** stopt alles. Staan er nog posities open, dan vraagt hij eerst of je het zeker weet: die
  worden pas weer verkocht als de app draait.
- De bot **pauzeert zelf** (en geeft een Mac-melding) bij: dagverlies (tot morgen), 15% onder je hoogste
  stand (tot jij op Start drukt), een verkoop die na 30 seconden nog niet gelukt is (30 minuten), 5 fouten
  bij kopen/verkopen binnen 10 minuten (30 minuten), storing in de weerdata (tot die weer binnenkomt) en,
  bij live, als Polymarket handelen niet toestaat of de klok van je Mac meer dan 10 seconden afwijkt.
- **Het belangrijkste alarm**: normaal is een aankoop binnen enkele seconden doorverkocht voor 0,99.
  Lukt dat na 30 seconden niet, dan koopt hij de rest van die dag niets meer in die markt, haalt hij de
  meting opnieuw op bij de andere weerbron (en kijkt of er een correctie is). Blijkt het vak toch niet
  dood, dan verkoopt hij meteen (nooit onder de helft van de aankoopprijs). Anders blijft de 0,99-order
  gewoon staan. Er is bewust geen stop-loss op prijs: bij HighTempTation had die meer gekost dan opgeleverd.
- **Nooit bijkopen als de prijs instort**: hij koopt alleen binnen 10 minuten na de meting, en een
  tweede aankoop binnen een minuut mag hooguit 0,03 goedkoper zijn dan de eerste.

## De strategie

Elke markt "Highest/Lowest temperature in [stad] on [datum]" is opgedeeld in vakken (bijv. 16°C, 17°C).
De uitslag komt van één vliegveldstation (bijv. Parijs = Le Bourget, LFPB), via NOAA.

- Het **dagmaximum** kan alleen stijgen: zodra een meting boven een vak uitkomt, kan dat vak niet meer
  winnen → **koop NO**. Het **dagminimum** kan alleen dalen → zelfde in spiegelbeeld.
- Is "X or higher" (of "X or below") al bereikt → **koop YES**.
- Hij koopt alleen de goedkope, nog niet bijgewerkte verkooporders (NO tot ~0,98) en verkoopt een paar
  seconden later voor **0,99**.

| Wat HighTempTation doet (uit zijn data)           | In de bot                                  |
|---------------------------------------------------|--------------------------------------------|
| Koopt NO op vakken die al gepasseerd zijn          | `strategy.py` → `find_signals`             |
| Reageert op nieuwe METAR-luchthavenmetingen        | `metar.py` (aviationweather.gov of NOAA)   |
| Veegt alle goedkope asks in één order op           | `orderbook.py` → `plan_sweep` + FAK-order  |
| Verkoopt 4-6 s later voor 0,99                    | `SELL_DELAY_SECONDS=5`, `SELL_PRICE=0.99`  |
| Inzet groeit met zijn kapitaal                     | `bankroll.py` (procent van saldo)          |

**Niet gekopieerd:** zijn kansinzetten laat op de middag op YES voor een exact vak ("het maximum is
binnen"); daar zit een eigen voorspelmodel achter. Zijn exacte databron en snelheid zijn onbekend.

## Oefenen, dan pas live

- **Oefenen (paper)** is de standaard: alles behalve echte orders. Gerekend met het echte orderboek op
  het moment van het signaal. Oefenwinst is een **bovengrens**: snellere bots kunnen je in het echt voor zijn.
- **Live** kan alleen waar Polymarket handelen toestaat. De bot vraagt het zelf na bij Polymarket (bij
  starten, elke 10 minuten en na een slaapstand, ook in oefenmodus) en weigert anders. Wordt handelen
  tijdens het draaien geblokkeerd, dan stopt hij met kopen, maar open posities worden gewoon verder
  verkocht. **In Nederland is Polymarket sinds februari 2026 verboden** (Kansspelautoriteit, volgens
  nieuwsberichten; controleer de lijst van verboden landen op de site van Polymarket), net als o.a. in
  België, Duitsland, Frankrijk, Italië en het VK. Vanuit Nederland blijft de bot daarom altijd in
  oefenmodus (`HARD_BLOCK_COUNTRIES=NL`).
- **De blokkade omzeilen** (bijvoorbeeld met een VPN) is tegen de regels van Polymarket: je kunt je
  account en je geld kwijtraken. Weerbot helpt daar niet bij.

## Terminal (zonder app)

```bash
cd polymarket-weather-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m weatherbot.main --check     # verbindingen testen
python -m weatherbot.app              # dashboard (opent de browser)
python -m weatherbot.main             # of: alleen de bot in de terminal
python tools/report.py                # samenvatting uit het logboek
python tools/compare_hightemptation.py
```

## Instellingen

| Instelling        | Standaard | Betekenis                                                      |
|-------------------|-----------|----------------------------------------------------------------|
| `START_AMOUNT`    | 100       | startbedrag (alleen de eerste keer; daarna telt het opgeslagen saldo) |
| `TRADE_PCT`       | 5         | max per aankoop, % van saldo. Een foute meting kost bijna de hele aankoop; bij 10% is de kans op 30% daling ~13%, bij 5% ~0,1% |
| `MARKET_PCT`      | 8         | max per vak per dag: één aankoop plus één snelle bijkoop       |
| `EVENT_PCT`       | 10        | max per markt in nog niet verkochte posities: één foute meting raakt vaak 2-3 vakken |
| `OPEN_PCT`        | 30        | max tegelijk in posities: hij had nooit meer dan 3-4 nieuwe aankopen binnen een minuut |
| `DAILY_LOSS_PCT`  | 6         | stopt die dag met kopen na dit verlies (open posities tellen mee): één fout vak plus wat extra |
| `MAX_DRAWDOWN_PCT`| 15        | stopt met kopen na deze daling vanaf de hoogste stand, tot jij op Start drukt |
| `MAX_TRADE_USDC`  | 250       | max $ per aankoop: posities van $1.000+ verloren 10× vaker meer dan de helft |
| `MAX_EVENT_USDC`  | 500       | max $ per markt in nog niet verkochte posities; pas verhogen na 2 weken live zonder problemen |
| `MAX_BUY_PRICE`   | 0.975     | hoogste aankoopprijs: 0,975-0,98 verdiende maar 1,4%; 0,98 is het maximum |
| `SELL_PRICE`      | 0.99      | doorverkoopprijs                                               |
| `MIN_EDGE`        | 0.005     | minimale winst per share na geschatte fees                     |
| `SAFETY_MARGIN`   | 0         | extra graden voorbij het vak (0 = zoals hij, 1 = veiliger)     |
| `METAR_SOURCE`    | aviationweather | of `tgftp` (NOAA-bestanden per station)                  |
| `POLL_SECONDS`    | 10        | hoe vaak hij nieuwe metingen ophaalt                           |
| `CITIES`          | (alles)   | bijv. `London,Paris,NYC`                                       |

Polymarket's minimum is 5 shares per order (± $5); bij 5% per aankoop is dat een startbedrag van
minstens ± $110.

## Risico's

- **Snelheid:** andere bots slaan in dezelfde seconde toe.
- **Data wijkt af van de uitslag:** zijn grootste verliezen kwamen doordat de live meting anders was dan
  wat NOAA officieel registreerde. `SAFETY_MARGIN=1` verkleint dat risico.
- **°F-markten (VS):** de bot neemt alleen vakken die bij elke mogelijke afronding dood zijn.
- **Fees:** weermarkten rekenen een taker-fee; de bot schat die mee.
- **Hong Kong** wordt overgeslagen (beslist op de Hong Kong Observatory, niet op een METAR).
- **Slaapstand:** de app houdt je Mac wakker zolang hij draait, maar met het deksel dicht slaapt de Mac
  toch. Open posities worden na herstarten verder afgehandeld.

## Tests

```bash
python -m unittest discover -s tests -v
```
