/*
 * SocialVolgers — catalogus en instellingen.
 * Pas hier prijzen, pakketten en bedrijfsgegevens aan; de hele site leest uit dit bestand.
 */

const CONFIG = {
  brand: 'SocialVolgers',
  domain: 'socialvolgers.nl',
  email: 'info@socialvolgers.nl',
  // Vul je eigen bedrijfsgegevens in voordat de site live gaat.
  kvk: '[KvK-nummer]',
  btw: '[btw-nummer]',
  address: '[Straat 1, 1234 AB Plaats]',
  currency: 'EUR',
  // Zolang er geen betaalprovider (bijv. Mollie of Stripe) is gekoppeld, blijft de site in demo-modus.
  demoMode: true,
};

// type 'username' = we vragen een gebruikersnaam, 'link' = we vragen een link naar een post/video.
const PLATFORMS = {
  instagram: {
    name: 'Instagram',
    color: '#E1306C',
    intro: 'Laat je profiel, posts en reels sneller groeien.',
    services: {
      volgers: {
        label: 'Instagram volgers',
        unit: 'volgers',
        type: 'username',
        description: 'Meer volgers op je Instagram-profiel, geleidelijk geleverd zodat de groei natuurlijk oogt.',
        packages: [[100, 2.49], [250, 4.49], [500, 6.99], [1000, 11.99], [2500, 24.99], [5000, 44.99], [10000, 79.99]],
      },
      likes: {
        label: 'Instagram likes',
        unit: 'likes',
        type: 'link',
        description: 'Geef een post of reel een duwtje met extra likes.',
        packages: [[100, 1.49], [250, 2.99], [500, 4.99], [1000, 7.99], [2500, 16.99], [5000, 29.99]],
      },
      views: {
        label: 'Instagram views',
        unit: 'views',
        type: 'link',
        description: 'Meer weergaven op je reels en video\'s.',
        packages: [[1000, 1.99], [2500, 3.99], [5000, 5.99], [10000, 9.99], [25000, 19.99], [50000, 34.99]],
      },
      reacties: {
        label: 'Instagram reacties',
        unit: 'reacties',
        type: 'link',
        description: 'Reacties onder je post zorgen voor een levendige tijdlijn.',
        packages: [[10, 2.99], [25, 5.99], [50, 9.99], [100, 17.99]],
      },
    },
  },
  tiktok: {
    name: 'TikTok',
    color: '#111111',
    intro: 'Vergroot je bereik op de For You-pagina.',
    services: {
      volgers: {
        label: 'TikTok volgers',
        unit: 'volgers',
        type: 'username',
        description: 'Bouw sneller een TikTok-account op met extra volgers.',
        packages: [[100, 2.99], [250, 5.49], [500, 8.99], [1000, 14.99], [2500, 32.99], [5000, 59.99]],
      },
      likes: {
        label: 'TikTok likes',
        unit: 'likes',
        type: 'link',
        description: 'Extra likes op je TikTok-video.',
        packages: [[100, 1.49], [250, 2.99], [500, 4.99], [1000, 8.49], [2500, 18.99], [5000, 32.99]],
      },
      views: {
        label: 'TikTok views',
        unit: 'views',
        type: 'link',
        description: 'Meer weergaven op je TikTok-video.',
        packages: [[1000, 0.99], [5000, 2.99], [10000, 4.99], [25000, 9.99], [50000, 17.99], [100000, 29.99]],
      },
      shares: {
        label: 'TikTok shares',
        unit: 'shares',
        type: 'link',
        description: 'Laat je video vaker delen.',
        packages: [[100, 1.99], [250, 3.99], [500, 6.99], [1000, 11.99]],
      },
    },
  },
  youtube: {
    name: 'YouTube',
    color: '#FF0000',
    intro: 'Meer abonnees en kijkers voor je kanaal.',
    services: {
      abonnees: {
        label: 'YouTube abonnees',
        unit: 'abonnees',
        type: 'username',
        inputLabel: 'YouTube kanaalnaam of kanaallink',
        description: 'Extra abonnees op je YouTube-kanaal. Vul je kanaalnaam of kanaallink in.',
        packages: [[100, 6.99], [250, 14.99], [500, 26.99], [1000, 49.99], [2500, 114.99]],
      },
      views: {
        label: 'YouTube views',
        unit: 'views',
        type: 'link',
        description: 'Meer weergaven op je YouTube-video of Short.',
        packages: [[1000, 3.99], [2500, 8.99], [5000, 15.99], [10000, 27.99], [25000, 64.99]],
      },
      likes: {
        label: 'YouTube likes',
        unit: 'likes',
        type: 'link',
        description: 'Extra likes op je YouTube-video.',
        packages: [[100, 2.99], [250, 5.99], [500, 10.99], [1000, 18.99]],
      },
    },
  },
  facebook: {
    name: 'Facebook',
    color: '#1877F2',
    intro: 'Geef je pagina en berichten meer bereik.',
    services: {
      paginalikes: {
        label: 'Facebook paginalikes',
        unit: 'paginalikes',
        type: 'link',
        description: 'Meer likes en volgers voor je Facebook-pagina.',
        packages: [[100, 3.99], [250, 7.99], [500, 13.99], [1000, 24.99], [2500, 54.99]],
      },
      postlikes: {
        label: 'Facebook berichtlikes',
        unit: 'likes',
        type: 'link',
        description: 'Extra likes op een Facebook-bericht.',
        packages: [[100, 2.49], [250, 4.99], [500, 8.99], [1000, 15.99]],
      },
    },
  },
  spotify: {
    name: 'Spotify',
    color: '#1DB954',
    intro: 'Laat je muziek vaker ontdekken.',
    services: {
      streams: {
        label: 'Spotify streams',
        unit: 'streams',
        type: 'link',
        description: 'Meer streams op je nummer. Vul de link naar het nummer in.',
        packages: [[1000, 3.99], [2500, 8.49], [5000, 14.99], [10000, 26.99], [25000, 59.99]],
      },
      volgers: {
        label: 'Spotify volgers',
        unit: 'volgers',
        type: 'link',
        description: 'Extra volgers voor je artiestenprofiel of playlist.',
        packages: [[100, 2.99], [250, 5.99], [500, 9.99], [1000, 17.99]],
      },
    },
  },
  x: {
    name: 'X (Twitter)',
    color: '#000000',
    intro: 'Meer volgers en interactie op X.',
    services: {
      volgers: {
        label: 'X volgers',
        unit: 'volgers',
        type: 'username',
        description: 'Extra volgers op je X-profiel.',
        packages: [[100, 3.99], [250, 7.99], [500, 13.99], [1000, 24.99]],
      },
      likes: {
        label: 'X likes',
        unit: 'likes',
        type: 'link',
        description: 'Meer likes op je post.',
        packages: [[100, 2.49], [250, 4.99], [500, 8.99], [1000, 15.99]],
      },
    },
  },
};

/*
 * Reviews: dit zijn VOORBEELDEN om de opmaak te tonen. Vervang ze door echte klantreviews
 * (of koppel een reviewdienst) voordat de site live gaat. Verzonnen reviews tonen als echt
 * is in de EU verboden.
 */
const REVIEWS = [
  { name: 'Voorbeeldklant', platform: 'Instagram', stars: 5, text: 'Plaats hier een echte review van een klant over de levering en de service.' },
  { name: 'Voorbeeldklant', platform: 'TikTok', stars: 5, text: 'Plaats hier een echte review. Reviews worden automatisch in deze kaarten getoond.' },
  { name: 'Voorbeeldklant', platform: 'YouTube', stars: 4, text: 'Plaats hier een echte review. Het aantal sterren pas je aan in js/data.js.' },
];

const FAQ = [
  {
    q: 'Hoe werkt een bestelling?',
    a: 'Kies een platform en dienst, selecteer een pakket en vul je gebruikersnaam of de link naar je post in. Na het afrekenen gaan we direct voor je aan de slag.',
  },
  {
    q: 'Heb je mijn wachtwoord nodig?',
    a: 'Nee. We vragen nooit om je wachtwoord. Alleen je gebruikersnaam of een link naar je post of video is genoeg.',
  },
  {
    q: 'Moet mijn account openbaar zijn?',
    a: 'Ja. Zet je account op openbaar tijdens de levering, anders kunnen we de bestelling niet uitvoeren. Daarna kun je het weer privé maken.',
  },
  {
    q: 'Hoe snel wordt mijn bestelling geleverd?',
    a: 'De meeste bestellingen starten binnen enkele uren. We leveren geleidelijk, zodat de groei er natuurlijk uitziet. Grote pakketten kunnen enkele dagen duren.',
  },
  {
    q: 'Welke betaalmethoden kan ik gebruiken?',
    a: 'Je kunt betalen met iDEAL, Bancontact, creditcard en PayPal.',
  },
  {
    q: 'Wat als er iets misgaat met mijn bestelling?',
    a: 'Neem contact op via het contactformulier of per e-mail. We kijken je bestelling na en zoeken samen naar een oplossing.',
  },
  {
    q: 'Is dit toegestaan door de platforms?',
    a: 'Sociale platforms raden het kopen van volgers en interactie af en kunnen het bereik van accounts beperken. Lees de voorwaarden van het platform en bestel bewust.',
  },
];
