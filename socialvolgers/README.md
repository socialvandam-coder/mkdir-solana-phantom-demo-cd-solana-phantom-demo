# SocialVolgers

Statische website voor **socialvolgers.nl**: een webshop voor volgers, likes en views op Instagram, TikTok, YouTube, Facebook, Spotify en X.

Er is geen build-stap nodig. Open `index.html` in je browser of zet de map `socialvolgers/` op een willekeurige webhost (Netlify, Vercel, GitHub Pages, een eigen server).

## Pagina's

| Bestand | Inhoud |
| --- | --- |
| `index.html` | Homepagina: hero, diensten per platform, hoe het werkt, voordelen, reviews, FAQ |
| `dienst.html` | Bestelpagina per dienst, bijv. `dienst.html?p=instagram&s=volgers` |
| `winkelwagen.html` | Winkelwagen en afrekenen |
| `contact.html` | Contactformulier (opent het mailprogramma van de bezoeker) |
| `voorwaarden.html`, `privacy.html` | Concepten van de juridische teksten |

## Aanpassen

Alles wat je regelmatig wilt wijzigen staat in `js/data.js`:

- `CONFIG`: e-mailadres, KvK- en btw-nummer, adres en demo-modus
- `PLATFORMS`: platforms, diensten, pakketten en prijzen (`[aantal, prijs]`), optioneel een eigen `inputLabel` voor het invulveld
- `REVIEWS`: reviews op de homepagina
- `FAQ`: veelgestelde vragen

Kleuren en lettertype staan bovenaan `css/style.css`.

## Voordat de site live gaat

1. **Betaalprovider koppelen.** Afrekenen staat nu in demo-modus. Koppel Mollie of Stripe via een kleine server en roep die aan in `startPayment()` in `js/app.js`. Zet daarna `demoMode` op `false`.
2. **Leverancier koppelen.** Na betaling moet de bestelling naar je leverancier (bijv. de API van een SMM-panel) worden doorgestuurd. Dat hoort ook op de server, zodat je API-sleutel geheim blijft.
3. **Echte reviews plaatsen.** De reviews in `data.js` zijn voorbeelden. Verzonnen reviews tonen als echt is in de EU verboden.
4. **Bedrijfsgegevens en voorwaarden.** Vul KvK, btw en adres in en laat de voorwaarden en het privacybeleid controleren.
