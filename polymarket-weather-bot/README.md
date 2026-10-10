# Polymarket weerbot (HighTempTation-strategie)

Deze bot kopieert de strategie van [HighTempTation](https://polymarket.com/@hightemptation) op de
dagelijkse temperatuurmarkten van Polymarket, op kleine schaal.

## Hoe de strategie werkt

Elke markt "Highest/Lowest temperature in [stad] on [datum]" is opgedeeld in vakken (bijv. 16°C, 17°C,
18°C). De uitslag komt van één vliegveldstation (bijv. Parijs = Le Bourget, LFPB), via NOAA.

- Het **dagmaximum** kan alleen stijgen. Zodra een meting boven een vak uitkomt, kan dat vak niet meer
  winnen → **koop NO**.
- Het **dagminimum** kan alleen dalen. Zodra een meting onder een vak uitkomt → **koop NO**.
- Is het "X or higher"-vak al bereikt (of "X or below" bij minimum) → **koop YES**.
- De bot koopt alleen de goedkope verkooporders die nog niet zijn bijgewerkt (NO tussen ~0,60 en 0,98) en
  verkoopt een paar seconden later voor **0,99**.

Er wordt dus niets voorspeld: een vak wordt alleen gekocht als de gemeten temperatuur het al uitsluit.

| Wat HighTempTation doet (uit zijn data)           | In de bot                                  |
|---------------------------------------------------|--------------------------------------------|
| Koopt NO op vakken die al gepasseerd zijn          | `strategy.py` → `find_signals`             |
| Reageert op nieuwe METAR-luchthavenmetingen        | `metar.py` (aviationweather.gov of NOAA)   |
| Veegt alle goedkope asks in één order op           | `orderbook.py` → `plan_sweep` + FAK-order  |
| Verkoopt 4-6 s later voor 0,99                    | `SELL_DELAY_SECONDS=5`, `SELL_PRICE=0.99`  |
| YES op "X or higher/below" als dat zeker is        | `ENABLE_YES_BETS=true`                     |
| Max ~0,98 betalen                                  | `MAX_BUY_PRICE=0.98`                       |

**Niet gekopieerd:** zijn kansinzetten laat op de middag op YES voor een exact vak ("het maximum is
binnen"). Daar zit een eigen voorspelmodel achter dat niet uit zijn data te halen is. Zijn exacte databron
en snelheid zijn ook onbekend; de bot gebruikt de openbare METAR-feeds.

## Installeren

Nodig: Python 3.10 of nieuer, op een computer die blijft aanstaan (eigen pc of een VPS).

```bash
cd polymarket-weather-bot
python -m venv .venv
# Windows: .venv\Scripts\activate     Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
```

## Stap 1 - verbindingen testen

```bash
python -m weatherbot.main --check
```

Je ziet of Polymarket handelen toestaat vanaf jouw verbinding, hoeveel markten er nu lopen, welke
stations, en per stad hoeveel vakken al dood zijn.

## Stap 2 - nep-handelen (paper)

```bash
python -m weatherbot.main
```

Standaard staat `MODE=paper`: de bot doet alles behalve echte orders plaatsen. Hij rekent met het echte
orderboek op het moment van het signaal. Alles wordt gelogd in `logs/journal-*.jsonl`.

Paper-winst is een **bovengrens**: in het echt kunnen snellere bots de goedkope orders eerder wegkapen.

## Stap 3 - resultaat bekijken en vergelijken met HighTempTation

```bash
python tools/report.py
python tools/compare_hightemptation.py
```

`compare_hightemptation.py` haalt zijn echte trades op over dezelfde periode en laat zien:
- welk deel van zijn vakken de bot ook vond;
- of de bot eerder of later was dan hij (in seconden);
- welke vakken de bot miste (dan klopt meestal station, afronding of tijdzone niet).

Draai minstens een paar dagen paper voordat je aan live denkt.

## Stap 4 - echt handelen (live)

Alleen mogelijk waar Polymarket handelen toestaat. De bot controleert dit zelf bij het starten en elk uur
via Polymarket's eigen geoblock-check, en weigert live te draaien als handelen niet is toegestaan.
**In Nederland is Polymarket sinds februari 2026 verboden** (Kansspelautoriteit), net als o.a. in België,
Duitsland, Frankrijk, Italië en het VK.

Waar het wel mag:
1. Maak een Polymarket-account en zet er een klein bedrag USDC op.
2. Vul in `.env`: `PRIVATE_KEY` (Settings → Export private key), `FUNDER` (je 0x-adres van je profiel),
   `SIGNATURE_TYPE` (1 = e-mail-login, 2 = MetaMask e.d.).
3. Zet `MODE=live` en draai eerst `--check`; die laat je USDC-saldo zien.

Gekochte shares die niet voor 0,99 verkocht raken, lopen door tot de uitslag. Winnende shares moet je
daarna zelf claimen in de Polymarket-app.

## Instellingen (`.env`)

| Instelling            | Standaard | Betekenis                                                          |
|-----------------------|-----------|--------------------------------------------------------------------|
| `MAX_BUY_PRICE`       | 0.98      | hoogste prijs die de bot betaalt                                   |
| `SELL_PRICE`          | 0.99      | doorverkoopprijs                                                   |
| `MIN_EDGE`            | 0.005     | minimale winst per share na geschatte fees                         |
| `SAFETY_MARGIN`       | 0         | extra graden voorbij het vak. 0 = zoals hij, 1 = veiliger          |
| `MAX_USDC_PER_TRADE`  | 10        | max per aankoop                                                    |
| `MAX_USDC_PER_MARKET` | 20        | max per vak                                                        |
| `MAX_OPEN_USDC`       | 50        | max totaal tegelijk in posities                                    |
| `DAILY_LOSS_LIMIT`    | 20        | stopt met kopen na dit verlies op een dag                          |
| `METAR_SOURCE`        | aviationweather | of `tgftp` (NOAA-bestanden per station, vaak iets sneller)   |
| `CITIES`              | (alles)   | bijv. `London,Paris,NYC`                                           |

## Risico's

- **Snelheid:** andere bots (o.a. fastestclick) slaan in dezelfde seconde toe. Een bot op een gewone pc
  is waarschijnlijk trager dan HighTempTation.
- **Data wijkt af van de uitslag:** zijn grootste verliezen (−$1.900 Panama City, −$1.500 Toronto en
  Kuala Lumpur) kwamen doordat de live meting anders was dan wat NOAA officieel registreerde.
  `SAFETY_MARGIN=1` verkleint dat risico ten koste van minder trades.
- **°F-markten (VS):** NOAA rekent om van °C naar °F. De bot neemt alleen vakken die bij elke mogelijke
  afronding dood zijn.
- **Fees:** weermarkten rekenen een taker-fee. De bot schat die mee (`FEE_RATE`), de echte fee wordt door
  Polymarket's bibliotheek automatisch toegepast.
- **Hong Kong** wordt overgeslagen: die markt beslist op de Hong Kong Observatory, niet op een METAR.

## Tests

```bash
python -m unittest discover -s tests -v
```
