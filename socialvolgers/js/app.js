/* SocialVolgers — gedeelde code voor alle pagina's. Leest de catalogus uit data.js. */

const ICONS = {
  instagram: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="17.3" cy="6.7" r="1.2" fill="currentColor"/></svg>',
  tiktok: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3v11.5a3.5 3.5 0 1 1-3.5-3.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/><path d="M14 3c.4 2.8 2.2 4.6 5 5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>',
  youtube: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="5" width="20" height="14" rx="4" fill="none" stroke="currentColor" stroke-width="2"/><path d="M10 9l5 3-5 3z" fill="currentColor"/></svg>',
  facebook: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M13 21v-7h2.5l.4-3H13V9.4c0-.9.3-1.4 1.5-1.4H16V5.3c-.3 0-1.2-.1-2.2-.1-2.2 0-3.6 1.3-3.6 3.7V11H8v3h2.2v7" fill="currentColor"/></svg>',
  spotify: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M7 9.5c3.5-1 7.3-.6 10 1M7.7 12.8c2.9-.8 5.8-.4 8 .9M8.4 15.8c2.2-.5 4.3-.3 6 .7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  x: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4l16 16M20 4L4 20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>',
  cart: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 4h2l2.4 11h11l2-8H6.3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="9" cy="19.5" r="1.5" fill="currentColor"/><circle cx="17" cy="19.5" r="1.5" fill="currentColor"/></svg>',
  menu: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  star: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.8l-5.2 2.8 1-5.8-4.3-4.1 5.9-.8z" fill="currentColor"/></svg>',
  logo: '<svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="url(#sv-logo-grad)"/><defs><linearGradient id="sv-logo-grad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6C3BFF"/><stop offset="1" stop-color="#FF4D8D"/></linearGradient></defs><path d="M9 20.5l5-5 3.5 3.5L23 12.5" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M18.5 12.5H23V17" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

/* ---------- Hulpfuncties ---------- */

const euro = new Intl.NumberFormat('nl-NL', { style: 'currency', currency: CONFIG.currency });
const number = new Intl.NumberFormat('nl-NL');

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function serviceUrl(platformKey, serviceKey) {
  return `dienst.html?p=${encodeURIComponent(platformKey)}&s=${encodeURIComponent(serviceKey)}`;
}

function getService(platformKey, serviceKey) {
  const platform = PLATFORMS[platformKey];
  const service = platform && platform.services[serviceKey];
  return service ? { platform, service } : null;
}

function startingPrice(service) {
  return Math.min(...service.packages.map(([, price]) => price));
}

/* ---------- Winkelwagen (localStorage, met fallback in geheugen) ---------- */

const Cart = (() => {
  const KEY = 'socialvolgers_cart';
  let memory = [];

  function read() {
    try {
      const raw = localStorage.getItem(KEY);
      return raw ? JSON.parse(raw) : memory;
    } catch (e) {
      return memory;
    }
  }

  function write(items) {
    memory = items;
    try {
      localStorage.setItem(KEY, JSON.stringify(items));
    } catch (e) {
      /* opslag geblokkeerd: we houden de winkelwagen in het geheugen */
    }
    updateBadge();
  }

  // Alleen items die nog in de catalogus staan tellen mee, zodat prijswijzigingen in data.js altijd gelden.
  function items() {
    return read().filter((item) => {
      const found = getService(item.p, item.s);
      return found && found.service.packages.some(([qty]) => qty === item.qty);
    }).map((item) => {
      const { platform, service } = getService(item.p, item.s);
      const price = service.packages.find(([qty]) => qty === item.qty)[1];
      return { ...item, price, label: service.label, unit: service.unit, platformName: platform.name };
    });
  }

  function add(entry) {
    const list = read();
    list.push({ id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6), ...entry });
    write(list);
  }

  function remove(id) {
    write(read().filter((item) => item.id !== id));
  }

  function clear() {
    write([]);
  }

  function total() {
    return items().reduce((sum, item) => sum + item.price, 0);
  }

  return { items, add, remove, clear, total };
})();

function updateBadge() {
  const badge = document.querySelector('[data-cart-count]');
  if (!badge) return;
  const count = Cart.items().length;
  badge.textContent = count;
  badge.hidden = count === 0;
}

/* ---------- Header en footer ---------- */

function renderHeader() {
  const el = document.getElementById('site-header');
  if (!el) return;
  const platformLinks = Object.entries(PLATFORMS).map(([key, p]) => {
    const firstService = Object.keys(p.services)[0];
    return `<a href="${serviceUrl(key, firstService)}" class="nav-platform" style="--brand:${p.color}">${ICONS[key]}<span>${p.name}</span></a>`;
  }).join('');

  el.innerHTML = `
    <div class="container header-inner">
      <a href="index.html" class="logo" aria-label="${CONFIG.brand} home">${ICONS.logo}<span>Social<strong>Volgers</strong></span></a>
      <nav class="main-nav" id="main-nav" aria-label="Hoofdmenu">
        <div class="nav-dropdown">
          <button type="button" class="nav-link" aria-expanded="false" data-dropdown>Diensten</button>
          <div class="dropdown-panel">${platformLinks}</div>
        </div>
        <a class="nav-link" href="index.html#hoe-het-werkt">Hoe het werkt</a>
        <a class="nav-link" href="index.html#reviews">Reviews</a>
        <a class="nav-link" href="index.html#faq">FAQ</a>
        <a class="nav-link" href="contact.html">Contact</a>
      </nav>
      <div class="header-actions">
        <a href="winkelwagen.html" class="cart-button" aria-label="Winkelwagen">${ICONS.cart}<span class="cart-count" data-cart-count hidden>0</span></a>
        <button type="button" class="menu-toggle" aria-label="Menu openen" aria-controls="main-nav" aria-expanded="false">${ICONS.menu}</button>
      </div>
    </div>`;

  const toggle = el.querySelector('.menu-toggle');
  const nav = el.querySelector('#main-nav');
  toggle.addEventListener('click', () => {
    const open = nav.classList.toggle('open');
    toggle.setAttribute('aria-expanded', String(open));
  });

  const dropdownButton = el.querySelector('[data-dropdown]');
  dropdownButton.addEventListener('click', () => {
    const open = dropdownButton.parentElement.classList.toggle('open');
    dropdownButton.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('click', (event) => {
    if (!dropdownButton.parentElement.contains(event.target)) {
      dropdownButton.parentElement.classList.remove('open');
      dropdownButton.setAttribute('aria-expanded', 'false');
    }
  });

  updateBadge();
}

function renderFooter() {
  const el = document.getElementById('site-footer');
  if (!el) return;
  const serviceLinks = Object.entries(PLATFORMS).map(([key, p]) => {
    const firstService = Object.keys(p.services)[0];
    return `<li><a href="${serviceUrl(key, firstService)}">${p.name}</a></li>`;
  }).join('');

  el.innerHTML = `
    <div class="container footer-grid">
      <div>
        <a href="index.html" class="logo logo-light">${ICONS.logo}<span>Social<strong>Volgers</strong></span></a>
        <p class="footer-text">Groei op social media met duidelijke pakketten, veilige betalingen en persoonlijke support.</p>
        <div class="payment-badges" aria-label="Betaalmethoden">
          <span>iDEAL</span><span>Bancontact</span><span>Creditcard</span><span>PayPal</span>
        </div>
      </div>
      <div>
        <h3>Diensten</h3>
        <ul>${serviceLinks}</ul>
      </div>
      <div>
        <h3>Informatie</h3>
        <ul>
          <li><a href="index.html#hoe-het-werkt">Hoe het werkt</a></li>
          <li><a href="index.html#faq">Veelgestelde vragen</a></li>
          <li><a href="voorwaarden.html">Algemene voorwaarden</a></li>
          <li><a href="privacy.html">Privacybeleid</a></li>
        </ul>
      </div>
      <div>
        <h3>Contact</h3>
        <ul>
          <li><a href="mailto:${CONFIG.email}">${CONFIG.email}</a></li>
          <li><a href="contact.html">Contactformulier</a></li>
          <li>KvK: ${CONFIG.kvk}</li>
          <li>Btw: ${CONFIG.btw}</li>
        </ul>
      </div>
    </div>
    <div class="container footer-bottom">
      <p>&copy; ${new Date().getFullYear()} ${CONFIG.brand} &middot; ${CONFIG.domain}</p>
      <p>${CONFIG.brand} is niet verbonden aan Instagram, TikTok, YouTube, Facebook, Spotify of X.</p>
    </div>`;
}

/* ---------- Homepagina ---------- */

function renderPlatformTabs() {
  const tabs = document.getElementById('platform-tabs');
  const panel = document.getElementById('platform-services');
  if (!tabs || !panel) return;

  const keys = Object.keys(PLATFORMS);
  tabs.innerHTML = keys.map((key, i) => {
    const p = PLATFORMS[key];
    return `<button type="button" role="tab" class="platform-tab" style="--brand:${p.color}" aria-selected="${i === 0}" data-platform="${key}">${ICONS[key]}<span>${p.name}</span></button>`;
  }).join('');

  function show(key) {
    const p = PLATFORMS[key];
    tabs.querySelectorAll('.platform-tab').forEach((tab) => tab.setAttribute('aria-selected', String(tab.dataset.platform === key)));
    panel.innerHTML = Object.entries(p.services).map(([serviceKey, s]) => `
      <a class="service-card" href="${serviceUrl(key, serviceKey)}" style="--brand:${p.color}">
        <span class="service-icon">${ICONS[key]}</span>
        <h3>${s.label}</h3>
        <p>${s.description}</p>
        <span class="service-price">vanaf <strong>${euro.format(startingPrice(s))}</strong></span>
        <span class="btn btn-small">Bekijk pakketten</span>
      </a>`).join('');
  }

  tabs.addEventListener('click', (event) => {
    const tab = event.target.closest('.platform-tab');
    if (tab) show(tab.dataset.platform);
  });
  show(keys[0]);
}

function renderHeroPlatforms() {
  const el = document.getElementById('hero-platforms');
  if (!el) return;
  el.innerHTML = Object.entries(PLATFORMS).map(([key, p]) => {
    const firstService = Object.keys(p.services)[0];
    return `<a href="${serviceUrl(key, firstService)}" class="hero-platform" style="--brand:${p.color}" aria-label="${p.name}">${ICONS[key]}</a>`;
  }).join('');
}

function renderReviews() {
  const el = document.getElementById('review-list');
  if (!el) return;
  el.innerHTML = REVIEWS.map((r) => `
    <figure class="review-card">
      <div class="stars" aria-label="${r.stars} van 5 sterren">${ICONS.star.repeat(r.stars)}</div>
      <blockquote>${escapeHtml(r.text)}</blockquote>
      <figcaption><strong>${escapeHtml(r.name)}</strong> &middot; ${escapeHtml(r.platform)}</figcaption>
    </figure>`).join('');
}

function renderFaq() {
  const el = document.getElementById('faq-list');
  if (!el) return;
  el.innerHTML = FAQ.map((item) => `
    <details class="faq-item">
      <summary>${escapeHtml(item.q)}</summary>
      <p>${escapeHtml(item.a)}</p>
    </details>`).join('');
}

/* ---------- Dienstpagina ---------- */

function renderServicePage() {
  const root = document.getElementById('service-page');
  if (!root) return;

  const params = new URLSearchParams(location.search);
  const platformKey = PLATFORMS[params.get('p')] ? params.get('p') : 'instagram';
  const platform = PLATFORMS[platformKey];
  const serviceKey = platform.services[params.get('s')] ? params.get('s') : Object.keys(platform.services)[0];
  const service = platform.services[serviceKey];

  document.title = `${service.label} kopen | ${CONFIG.brand}`;
  root.style.setProperty('--brand', platform.color);

  const serviceTabs = Object.entries(platform.services).map(([key, s]) =>
    `<a href="${serviceUrl(platformKey, key)}" class="chip" aria-current="${key === serviceKey ? 'page' : 'false'}">${s.unit.charAt(0).toUpperCase() + s.unit.slice(1)}</a>`
  ).join('');

  const inputLabel = service.inputLabel || (service.type === 'username'
    ? `${platform.name} gebruikersnaam`
    : `Link naar je ${platform.name}-${service.unit === 'paginalikes' ? 'pagina' : 'post of video'}`);
  const inputPlaceholder = service.type === 'username' ? '@jouwgebruikersnaam' : 'https://...';

  // Het middelste pakket markeren we als populairste keuze.
  const popularIndex = Math.floor(service.packages.length / 2);

  root.innerHTML = `
    <section class="service-hero">
      <div class="container">
        <p class="breadcrumb"><a href="index.html">Home</a> / ${platform.name} / ${service.label}</p>
        <div class="service-heading">
          <span class="service-icon large">${ICONS[platformKey]}</span>
          <div>
            <h1>${service.label} kopen</h1>
            <p>${service.description}</p>
          </div>
        </div>
        <div class="chips" aria-label="Andere ${platform.name}-diensten">${serviceTabs}</div>
      </div>
    </section>
    <section class="container order-layout">
      <div>
        <h2 class="step-title"><span>1</span> Kies je pakket</h2>
        <div class="package-grid" role="radiogroup" aria-label="Pakketten">
          ${service.packages.map(([qty, price], i) => `
            <button type="button" role="radio" class="package${i === popularIndex ? ' popular' : ''}" aria-checked="${i === popularIndex}" data-qty="${qty}" data-price="${price}">
              ${i === popularIndex ? '<span class="package-badge">Populair</span>' : ''}
              <span class="package-qty">${number.format(qty)}</span>
              <span class="package-unit">${service.unit}</span>
              <span class="package-price">${euro.format(price)}</span>
            </button>`).join('')}
        </div>

        <h2 class="step-title"><span>2</span> Vul je gegevens in</h2>
        <form id="order-form" class="card form" novalidate>
          <label for="target">${inputLabel}</label>
          <input id="target" name="target" type="text" placeholder="${inputPlaceholder}" autocomplete="off" required>
          <p class="hint">We vragen nooit om je wachtwoord. Zorg dat je account openbaar is tijdens de levering.</p>
          <p class="form-error" id="target-error" hidden></p>
        </form>
      </div>

      <aside class="card summary">
        <h2>Overzicht</h2>
        <dl>
          <div><dt>Dienst</dt><dd>${service.label}</dd></div>
          <div><dt>Aantal</dt><dd data-summary-qty></dd></div>
          <div class="summary-total"><dt>Totaal</dt><dd data-summary-price></dd></div>
        </dl>
        <button type="submit" form="order-form" class="btn btn-block">In winkelwagen</button>
        <ul class="checklist">
          <li>${ICONS.check} Geen wachtwoord nodig</li>
          <li>${ICONS.check} Start meestal binnen enkele uren</li>
          <li>${ICONS.check} Geleidelijke levering</li>
          <li>${ICONS.check} Veilig betalen met iDEAL</li>
        </ul>
        <p class="added-message" id="added-message" hidden>Toegevoegd! <a href="winkelwagen.html">Naar winkelwagen &rarr;</a></p>
      </aside>
    </section>`;

  let selected = service.packages[popularIndex];
  const qtyEl = root.querySelector('[data-summary-qty]');
  const priceEl = root.querySelector('[data-summary-price]');

  function updateSummary() {
    qtyEl.textContent = `${number.format(selected[0])} ${service.unit}`;
    priceEl.textContent = euro.format(selected[1]);
  }
  updateSummary();

  root.querySelector('.package-grid').addEventListener('click', (event) => {
    const button = event.target.closest('.package');
    if (!button) return;
    root.querySelectorAll('.package').forEach((b) => b.setAttribute('aria-checked', String(b === button)));
    selected = [Number(button.dataset.qty), Number(button.dataset.price)];
    updateSummary();
  });

  const form = root.querySelector('#order-form');
  const input = root.querySelector('#target');
  const error = root.querySelector('#target-error');
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = input.value.trim();
    let message = '';
    if (!value) {
      message = service.type === 'username' ? 'Vul je gebruikersnaam in.' : 'Vul de link in.';
    } else if (service.type === 'link' && !/^https?:\/\/\S+\.\S+/.test(value)) {
      message = 'Vul een geldige link in die begint met https://';
    }
    error.textContent = message;
    error.hidden = !message;
    input.setAttribute('aria-invalid', String(Boolean(message)));
    if (message) {
      input.focus();
      return;
    }
    Cart.add({ p: platformKey, s: serviceKey, qty: selected[0], target: value });
    root.querySelector('#added-message').hidden = false;
  });
}

/* ---------- Winkelwagen-pagina ---------- */

function renderCartPage() {
  const root = document.getElementById('cart-page');
  if (!root) return;

  function draw() {
    const items = Cart.items();
    if (items.length === 0) {
      root.innerHTML = `
        <div class="card empty-cart">
          <h2>Je winkelwagen is leeg</h2>
          <p>Kies een dienst om te beginnen.</p>
          <a href="index.html#diensten" class="btn">Bekijk diensten</a>
        </div>`;
      return;
    }

    root.innerHTML = `
      <div class="order-layout">
        <div class="card">
          <ul class="cart-list">
            ${items.map((item) => `
              <li>
                <div>
                  <strong>${number.format(item.qty)} ${escapeHtml(item.label.toLowerCase())}</strong>
                  <span class="muted">${escapeHtml(item.target)}</span>
                </div>
                <div class="cart-line-end">
                  <span>${euro.format(item.price)}</span>
                  <button type="button" class="link-button" data-remove="${item.id}">Verwijderen</button>
                </div>
              </li>`).join('')}
          </ul>
        </div>
        <form class="card summary form" id="checkout-form" novalidate>
          <h2>Afrekenen</h2>
          <label for="email">E-mailadres</label>
          <input id="email" name="email" type="email" placeholder="naam@voorbeeld.nl" autocomplete="email" required>
          <fieldset class="payment-methods">
            <legend>Betaalmethode</legend>
            ${['iDEAL', 'Bancontact', 'Creditcard', 'PayPal'].map((m, i) => `
              <label class="radio"><input type="radio" name="method" value="${m}" ${i === 0 ? 'checked' : ''}> ${m}</label>`).join('')}
          </fieldset>
          <label class="checkbox"><input type="checkbox" name="terms" required> Ik ga akkoord met de <a href="voorwaarden.html">algemene voorwaarden</a></label>
          <div class="summary-total"><span>Totaal</span><strong>${euro.format(Cart.total())}</strong></div>
          <p class="form-error" id="checkout-error" hidden></p>
          <button type="submit" class="btn btn-block">Betalen</button>
          <p class="hint">Prijzen zijn inclusief btw.</p>
        </form>
      </div>`;

    root.querySelectorAll('[data-remove]').forEach((button) => {
      button.addEventListener('click', () => {
        Cart.remove(button.dataset.remove);
        draw();
      });
    });

    root.querySelector('#checkout-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const error = form.querySelector('#checkout-error');
      let message = '';
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email.value.trim())) message = 'Vul een geldig e-mailadres in.';
      else if (!form.terms.checked) message = 'Ga akkoord met de algemene voorwaarden om verder te gaan.';
      error.textContent = message;
      error.hidden = !message;
      if (message) return;

      startPayment({
        email: form.email.value.trim(),
        method: form.method.value,
        items: Cart.items(),
        total: Cart.total(),
      });
    });
  }

  draw();
}

/*
 * Koppel hier je betaalprovider. Stuur de bestelling naar je eigen server, die bij
 * Mollie/Stripe een betaling aanmaakt en de klant doorstuurt naar de betaalpagina.
 */
function startPayment(order) {
  const root = document.getElementById('cart-page');
  if (CONFIG.demoMode) {
    root.innerHTML = `
      <div class="card empty-cart">
        <h2>Demo-modus</h2>
        <p>Er is nog geen betaalprovider gekoppeld. Deze bestelling van <strong>${euro.format(order.total)}</strong> via ${escapeHtml(order.method)} is daarom niet afgerekend.</p>
        <p class="muted">Koppel Mollie of Stripe in <code>js/app.js</code> (functie <code>startPayment</code>) en zet <code>demoMode</code> uit in <code>js/data.js</code>.</p>
        <a href="winkelwagen.html" class="btn">Terug naar winkelwagen</a>
      </div>`;
    return;
  }
  // Voorbeeld: fetch('/api/betaling', { method: 'POST', body: JSON.stringify(order) }) en daarna doorsturen.
}

/* ---------- Contactformulier ---------- */

function initContactForm() {
  const form = document.getElementById('contact-form');
  if (!form) return;
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const subject = encodeURIComponent(`Vraag via ${CONFIG.domain}${form.order.value ? ` (bestelling ${form.order.value})` : ''}`);
    const body = encodeURIComponent(`${form.message.value}\n\n${form.name.value}\n${form.email.value}`);
    location.href = `mailto:${CONFIG.email}?subject=${subject}&body=${body}`;
  });
}

/* ---------- Start ---------- */

document.addEventListener('DOMContentLoaded', () => {
  renderHeader();
  renderFooter();
  renderHeroPlatforms();
  renderPlatformTabs();
  renderReviews();
  renderFaq();
  renderServicePage();
  renderCartPage();
  initContactForm();
  document.querySelectorAll('[data-email]').forEach((el) => {
    el.textContent = CONFIG.email;
    if (el.tagName === 'A') el.href = `mailto:${CONFIG.email}`;
  });
});
