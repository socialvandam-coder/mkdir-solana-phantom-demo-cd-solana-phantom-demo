# ScaleLink Postmaker

Een LinkedIn-postgenerator in de huisstijl van ScaleLink (indigo `#5B4FE8`, crème, Plus Jakarta Sans, het netwerklogo).
Eén pagina zet alles klaar om te posten:

- **Tekst**: Claude schrijft de post op basis van het merkprofiel, met drie alternatieve hooks, hashtags, een eerste reactie voor je link, alt-tekst en het beste moment om te posten. Daarna kun je de post laten herschrijven: korter, sterkere hook, persoonlijker, duidelijkere CTA of vertaald.
- **Afbeelding**: vijf sjablonen (Statement, Cijfer, Lijst, Quote, Foto), drie formaten (staand 4:5, vierkant, liggend) en vier huisstijlthema's plus eigen kleuren. Download als PNG of kopieer direct.
- **Voorbeeld**: hoe de post er in de LinkedIn-feed uitziet, inclusief de "meer"-afbreking.
- **Checklist**: lengte van de hook, tekenlimiet, link in de eerste reactie, hashtags, alt-tekst.

## Gebruik

De pagina is gemaakt als Claude-artifact (`index.html` is de pagina-inhoud zonder `<html>`-skelet; dat voegt het platform toe).
Binnen Claude schrijft Claude de teksten (de `sample`-capability) en sla je afbeeldingen op via `downloads`.

Open je het bestand los in een browser, dan werkt de app in **sjabloonmodus**: je krijgt een opzet met `[haakjes]` om zelf in te vullen. De afbeeldingsstudio werkt dan gewoon.

Het merkprofiel, de laatste post en de instellingen worden bewaard in de browser (localStorage).
