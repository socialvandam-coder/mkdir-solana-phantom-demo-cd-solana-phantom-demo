# ScaleLink Postmaker

Een generator voor Instagram-posts in de huisstijl van ScaleLink (indigo `#5B4FE8`, crème, Plus Jakarta Sans, het netwerklogo).
Eén pagina zet alles klaar om te posten:

- **Caption**: Claude schrijft de caption op basis van het merkprofiel, met drie alternatieve hooks, maximaal 5 hashtags, een storytekst, alt-tekst en het beste moment om te posten. Daarna kun je de caption laten herschrijven: korter, sterkere hook, persoonlijker, duidelijkere CTA of vertaald.
- **Carrousel**: cover, 3 tot 8 inhoudsslides en een afsluitende slide met je call-to-action en "Link in bio". Download alle slides in één ZIP, of per slide.
- **Eén afbeelding**: vijf sjablonen (Statement, Cijfer, Lijst, Quote, Foto) in staand 4:5, vierkant 1:1 of story 9:16. Bij stories blijft de tekst vrij van de balken van Instagram.
- **Voorbeeld**: hoe de post er in de Instagram-feed uitziet, met swipen door de carrousel en de "meer"-afbreking van de caption.
- **Checklist**: lengte van de hook, de limiet van 2.200 tekens, geen links in de caption, maximaal 5 hashtags, alt-tekst.

## Gebruik

De pagina is gemaakt als Claude-artifact (`index.html` is de pagina-inhoud zonder `<html>`-skelet; dat voegt het platform toe).
Binnen Claude schrijft Claude de teksten (de `sample`-capability) en sla je afbeeldingen op via `downloads`.

Open je het bestand los in een browser, dan werkt de app in **sjabloonmodus**: je krijgt een opzet met `[haakjes]` om zelf in te vullen. De beeldstudio werkt dan gewoon.

Het merkprofiel, de laatste post en de instellingen worden bewaard in de browser (localStorage).
