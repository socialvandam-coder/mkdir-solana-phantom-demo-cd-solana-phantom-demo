# GrowthSourced Instagram-video's: vaste kwaliteitsstandaard

Goedgekeurd door de eigenaar op 10 november 2026 ("echt perfect, deze kwaliteit zoals bij Apple moet blijven").
Elke nieuwe video voldoet hieraan. Voorbeelden: advertentie 28 (`growthsourced-video/premium-template.html`) en advertentie 29 (`growthsourced-video/premium-volger-tot-klant.html`, met een grote iPhone die onderaan buiten beeld loopt, schermovergangen en een chatgesprek) en advertentie 30 (`growthsourced-video/premium-jouw-sleutels.html`, veiligheid: inlogscherm, een slot dat dichtklikt en een glazen instellingenkaart) en advertentie 31 (`growthsourced-video/premium-doelgroep.html`, targeting: een doelgroepscherm op de iPhone en een glazen kaart "Wie reageert het best?"), advertentie 32 (`growthsourced-video/premium-maandrapport.html`, maandrapport in Apple Gezondheid-stijl op de iPhone en grote keynote-cijfers) en advertentie 33 (`growthsourced-video/premium-rekensom.html`, glazen rekenkaart: 200 volgers via advertenties €1.600 tegenover €249,99, besparing €1.350,01 per maand en €16.125,12 per jaar).

## Formaat en techniek
- Altijd postformaat 4:5: 1080 × 1350, MP4 (H.264 high, yuv420p, bt709), **60 fps**, stille AAC-track. Geen Stories of 9:16 meer.
- Hoge kwaliteit: elk frame op 2× renderen (deviceScaleFactor 2) en met lanczos terugschalen; x264 CRF 12, preset slow, tune animation, aq-mode 3.
- Lengte rond de 28 seconden, rustig tempo (een scène duurt 4 tot 8 seconden; nooit te snel).
- Renderen: in `growthsourced-video/` met `FPS=60 HQ=1 ./batch60.sh pagina.html Bestandsnaam`. De pagina zet `window.DURATION` en een deterministische `window.render(t)`.
- Eerst controlebeelden maken (`node render.js stills map 1.0 2.5 ...`) en die bekijken, pas daarna de video renderen.

## Premiumstijl (Apple-achtig)
- Achtergrond #FBFBFD; tekst #1D1D1F, #6E6E73 en #86868B; accent indigo #5B4FE8.
- Accentregels als gradienttekst (#4539C9 → #7C71F7 → #A79FFF) die langzaam glanst.
- Lettertype Plus Jakarta Sans; koppen gewicht 700, letter-spacing -0.045em, groot en kort.
- Koppen verschijnen per regel met een gemaskeerde slide-up (easeOutExpo); bijteksten worden van wazig naar scherp.
- Tekst moet altijd perfect staan (na feedback "teksten staan niet goed / niet goed uitgelijnd"):
  - gradienttekst krijgt ruimte onder de regel (`padding-bottom:0.2em; margin-bottom:-0.2em`), anders vallen de onderkanten van g, j, p en y weg;
  - elke kopregel blijft op één regel (`white-space:nowrap`) en past binnen 960 px; anders de letters kleiner maken, nooit laten afbreken;
  - koppen staan los van de telefoon en van kaarten, met duidelijke witruimte;
  - controleer elke kop op ware grootte (uitsnede op 100 %) voordat de video wordt gerenderd.
- Eén boodschap per scène, veel witruimte. Overgangen: wazig wegvagen met een lichte schaalbeweging, of de camera duwt de telefoon in.
- Realistische iPhone (titanium rand, dynamic island, statusbalk 9:41) die langzaam in 3D draait, met zachte schaduw en glans.
- Glazen kaarten (wit 60–72 %, backdrop-filter blur) boven zachte indigo-gloed. Meldingen netjes gestapeld náást de telefoon, nooit over het scherm.
- Variant op verzoek ("andere achtergrond, zelfde Apple-stijl"): zwarte "Pro"-versie, advertentie 34 (`growthsourced-video/premium-zwart-meer-dan-een-getal.html`): achtergrond #000, witte koppen, lichtere indigo-gradient, titanium iPhone in donkere modus, donkere glazen kaarten, zachte indigo-gloed. Laat een nieuwe scène pas beginnen als de vorige helemaal weg is.
- Eindkaart: logotegel, "GrowthSourced", tagline, "€249,99 per maand.", "Maandelijks opzegbaar.", "Excl. btw · eenmalig €75 opstartkosten" en een "Link in bio"-pill.

## Merk en feiten (alleen claims van de website)
- Naam: GrowthSourced (voorheen ScaleLink). Logo: indigo tegel met wit netwerk-icoon (drie verbonden cirkels).
- Prijs: vaste €249,99 per maand plus eenmalig €75 opstartkosten, excl. btw; maandelijks opzegbaar (één maand opzegtermijn).
- Groei: 200 tot 400+ relevante volgers per maand bij de meeste zakelijke accounts. 4,8 sterren, 200+ tevreden opdrachtgevers.
- Organisch: echte profielen uit de doelgroep, geen nepvolgers, geen advertenties. Eén keer inloggen; maandrapport (voorbeeld: +412 in vier weken).
- Jij blijft posten en voert zelf het gesprek met nieuwe volgers; GrowthSourced regelt de groei.
- Voorbeeldcijfers altijd markeren als "Voorbeeld". Geen echte merknamen of personen.
- Altijd eindigen met "Link in bio", nooit met "Plan een vrijblijvende kennismaking".

## Afgewezen (niet meer maken)
- TV-shopstijl, game/arcade, whiteboard, de plant-video, de feestdagenvideo en het teamfilmpje.
- Afwijkende donkere stijlen en te snelle animaties.
- Abstracte wolken van gekleurde bolletjes of confetti-achtige visuals (advertentie 31, "heel lelijk"). Blijf bij de goedgekeurde bouwstenen: realistische iPhone-schermen, glazen kaarten, grote typografie.

## Waar alles staat
- Website (bron voor alle teksten): https://claude.ai/artifact/8EkPhdFe7GBM2dDcB5kKau
- Videopagina met alle video's: https://claude.ai/artifact/MvpcCxKRqg9TcCUDMp3Zp7
