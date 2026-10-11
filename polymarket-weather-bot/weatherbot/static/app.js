"use strict";
// Weerbot dashboard. All data is inserted with textContent (never innerHTML).

const TOKEN = document.querySelector('meta[name="weerbot-token"]').content;
const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";
let STATE = null;
let RANGE = "all";
let SETTINGS = null;
let pollTimer = null;

// ---------- formatting ----------
const nf2 = new Intl.NumberFormat("nl-NL", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nf0 = new Intl.NumberFormat("nl-NL", { maximumFractionDigits: 0 });
const nf3 = new Intl.NumberFormat("nl-NL", { minimumFractionDigits: 2, maximumFractionDigits: 3 });
function money(v, signed = false) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  const s = "$" + nf2.format(Math.abs(v));
  if (signed) return (v > 0 ? "+" : v < 0 ? "−" : "") + s;
  return (v < 0 ? "−" : "") + s;
}
function pct(v, digits = 2) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  const f = new Intl.NumberFormat("nl-NL", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return (v > 0 ? "+" : v < 0 ? "−" : "") + f.format(Math.abs(v)) + "%";
}
function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
}
function dateTime(ts) {
  return new Date(ts * 1000).toLocaleString("nl-NL", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function ago(sec) {
  if (sec === null || sec === undefined) return "–";
  if (sec < 60) return Math.round(sec) + " s";
  if (sec < 3600) return Math.round(sec / 60) + " min";
  return nf0.format(sec / 3600) + " u";
}
function moneyShort(v) {
  // $100 instead of $100,00 for round amounts
  if (v === null || v === undefined || isNaN(v)) return "–";
  return Math.abs(v - Math.round(v)) < 0.005 ? (v < 0 ? "−" : "") + "$" + nf0.format(Math.abs(v)) : money(v);
}
function hm(sec) {
  const m = Math.round(sec / 60);
  return `${Math.floor(m / 60)}u${String(m % 60).padStart(2, "0")}`;
}
function deltaShort(d) {
  const a = Math.abs(d);
  const t = a < 60 ? `${Math.round(a)} s` : a < 3600 ? `${Math.round(a / 60)} min` : `${nf0.format(a / 3600)} u`;
  return d > 0 ? "+" + t : d < 0 ? "−" + t : "0 s";
}
function deltaText(d) {
  if (d === null || d === undefined) return "";
  return d > 0 ? `${deltaShort(d)} later` : d < 0 ? `${deltaShort(d).slice(1)} eerder` : "tegelijk";
}
function dirClass(v) { return v > 0 ? "up" : v < 0 ? "down" : ""; }
function arrow(v) { return v > 0 ? "▲ " : v < 0 ? "▼ " : ""; }
const KIND = { high: "Max", low: "Min" };
const HOW = { sold: "verkocht", resolved_win: "uitslag (gewonnen)", resolved_loss: "uitslag (verloren)", exit: "verkocht na controle" };

// ---------- DOM helpers ----------
function el(tag, attrs = {}, text) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v; else n.setAttribute(k, v);
  }
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}
function fillTable(table, headers, rows, emptyText) {
  table.replaceChildren();
  const thead = el("thead"); const tr = el("tr");
  headers.forEach((h) => tr.appendChild(el("th", h.num ? { class: "num" } : {}, h.label)));
  thead.appendChild(tr); table.appendChild(thead);
  const tbody = el("tbody");
  if (!rows.length) {
    const r = el("tr", { class: "empty-row" });
    r.appendChild(el("td", { colspan: String(headers.length) }, emptyText));
    tbody.appendChild(r);
  }
  rows.forEach((cells) => {
    const r = el("tr");
    cells.forEach((c, i) => {
      const td = el("td", headers[i].num ? { class: "num" } : {});
      if (c && typeof c === "object" && "text" in c) {
        td.textContent = c.text;
        if (c.cls) td.className = (td.className + " " + c.cls).trim();
      } else if (c instanceof Node) td.appendChild(c);
      else td.textContent = c === null || c === undefined ? "–" : String(c);
      r.appendChild(td);
    });
    tbody.appendChild(r);
  });
  table.appendChild(tbody);
}

// ---------- API ----------
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Weerbot-Token": TOKEN },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) throw new Error((data && data.error) || ("fout " + r.status));
  return data;
}

// ---------- tooltip ----------
const tip = $("tooltip");
function showTip(x, y, strong, line2, keyed) {
  tip.replaceChildren();
  const s = el("strong");
  if (keyed) s.appendChild(el("span", { class: "key" }));
  s.appendChild(document.createTextNode(strong));
  tip.appendChild(s);
  if (line2) tip.appendChild(el("div", { class: "muted" }, line2));
  tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let left = x + 14, top = y - h - 10;
  if (left + w > window.innerWidth - 8) left = x - w - 14;
  if (top < 8) top = y + 14;
  tip.style.left = left + "px"; tip.style.top = top + "px";
}
function hideTip() { tip.hidden = true; }

// ---------- scales ----------
function niceTicks(lo, hi, count) {
  if (hi === lo) { hi = lo + 1; lo = lo - 1; }
  const span = hi - lo;
  const step0 = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const err = step0 / mag;
  const step = (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1) * mag;
  const start = Math.floor(lo / step) * step;
  const end = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = start; v <= end + step / 2; v += step) ticks.push(Math.round(v / step) * step);
  return ticks;
}
function compactMoney(v) {
  const a = Math.abs(v);
  if (a >= 1e6) return "$" + nf2.format(v / 1e6) + "M";
  if (a >= 1e4) return "$" + nf0.format(v / 1e3) + "K";
  if (a >= 100) return "$" + nf0.format(v);
  return "$" + nf2.format(v);
}

// ---------- balance chart (step line: the balance only changes when a trade closes) ----------
function balanceSeries(state) {
  const hist = (state.history || []).map((p) => [p[0], p[1]]);
  const now = state.now;
  let from = -Infinity;
  if (RANGE === "today") { const d = new Date(); d.setHours(0, 0, 0, 0); from = d.getTime() / 1000; }
  if (RANGE === "7d") from = now - 7 * 86400;
  let pts = hist.filter((p) => p[0] >= from);
  const before = hist.filter((p) => p[0] < from);
  const startVal = before.length ? before[before.length - 1][1] : (hist.length ? hist[0][1] : state.bank.balance);
  if (isFinite(from)) pts.unshift([from, startVal]);
  // 'now' = the current value, open positions included (the same number as 'Saldo' in Vandaag)
  pts.push([now, state.bank.equity !== undefined ? state.bank.equity : state.bank.balance]);
  return pts;
}

function drawBalance(state) {
  const box = $("chart-balance");
  const W = box.clientWidth || 600, H = box.clientHeight || 240;
  const m = { l: 60, r: 18, t: 14, b: 26 };
  const pts = balanceSeries(state);
  const start = state.bank.start_amount;
  box.replaceChildren();
  if (pts.length < 2 || pts[pts.length - 1][0] - pts[0][0] <= 0) {
    box.appendChild(el("div", { class: "empty" }, "Nog geen trades: de grafiek vult zich zodra er trades zijn afgesloten."));
    return;
  }
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]).concat([start]);
  let yLo = Math.min(...ys), yHi = Math.max(...ys);
  const pad = Math.max((yHi - yLo) * 0.12, start * 0.005, 0.5);
  yLo -= pad; yHi += pad;
  const ticks = niceTicks(yLo, yHi, 4);
  yLo = Math.min(yLo, ticks[0]); yHi = Math.max(yHi, ticks[ticks.length - 1]);
  const x0 = xs[0], x1 = xs[xs.length - 1];
  const X = (t) => m.l + (t - x0) / (x1 - x0) * (W - m.l - m.r);
  const Y = (v) => m.t + (1 - (v - yLo) / (yHi - yLo)) * (H - m.t - m.b);

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Saldo over tijd" });
  const axis = svg("g", { class: "axis" });
  ticks.forEach((v) => {
    if (v < yLo - 1e-9 || v > yHi + 1e-9) return;
    root.appendChild(svg("line", { class: "gridline", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    const t = svg("text", { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end" }); t.textContent = compactMoney(v); axis.appendChild(t);
  });
  const span = x1 - x0;
  const nX = Math.max(2, Math.min(6, Math.floor((W - m.l - m.r) / 110)));
  for (let i = 0; i <= nX; i++) {
    const ts = x0 + span * i / nX;
    const t = svg("text", { x: X(ts), y: H - 6, "text-anchor": i === 0 ? "start" : i === nX ? "end" : "middle" });
    t.textContent = span > 2 * 86400 ? new Date(ts * 1000).toLocaleDateString("nl-NL", { day: "numeric", month: "short" }) : clock(ts);
    axis.appendChild(t);
  }
  root.appendChild(axis);

  // start reference line
  root.appendChild(svg("line", { class: "refline", x1: m.l, x2: W - m.r, y1: Y(start), y2: Y(start) }));
  const rl = svg("text", { class: "reflabel", x: m.l + 6, y: Y(start) - 6 }); rl.textContent = "start " + money(start); root.appendChild(rl);

  // step path
  let d = `M${X(pts[0][0])},${Y(pts[0][1])}`;
  for (let i = 1; i < pts.length; i++) d += `H${X(pts[i][0])}V${Y(pts[i][1])}`;
  const area = d + `V${H - m.b}H${X(pts[0][0])}Z`;
  root.appendChild(svg("path", { class: "area", d: area }));
  root.appendChild(svg("path", { class: "line", d }));
  const last = pts[pts.length - 1];
  root.appendChild(svg("circle", { class: "enddot", cx: X(last[0]), cy: Y(last[1]), r: 4 }));
  const el2 = svg("text", { class: "endlabel", x: X(last[0]) - 8, y: Y(last[1]) - 10, "text-anchor": "end" });
  el2.textContent = money(last[1]); root.appendChild(el2);

  // crosshair + tooltip
  const cross = svg("line", { class: "crosshair", y1: m.t, y2: H - m.b, visibility: "hidden" });
  const hoverDot = svg("circle", { class: "enddot", r: 4, visibility: "hidden" });
  root.appendChild(cross); root.appendChild(hoverDot);
  const hit = svg("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent", tabindex: "0" });
  function at(ts) {
    let v = pts[0][1];
    for (const p of pts) { if (p[0] <= ts) v = p[1]; else break; }
    return v;
  }
  function move(clientX, clientY) {
    const r = root.getBoundingClientRect();
    const sx = (clientX - r.left) * (W / r.width);
    const ts = Math.max(x0, Math.min(x1, x0 + (sx - m.l) / (W - m.l - m.r) * (x1 - x0)));
    const v = at(ts);
    cross.setAttribute("x1", X(ts)); cross.setAttribute("x2", X(ts)); cross.setAttribute("visibility", "visible");
    hoverDot.setAttribute("cx", X(ts)); hoverDot.setAttribute("cy", Y(v)); hoverDot.setAttribute("visibility", "visible");
    showTip(clientX, clientY, money(v), dateTime(ts) + " · " + pct((v - start) / start * 100) + " t.o.v. start", true);
  }
  hit.addEventListener("pointermove", (e) => move(e.clientX, e.clientY));
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hoverDot.setAttribute("visibility", "hidden"); hideTip(); });
  hit.addEventListener("focus", () => { const r = root.getBoundingClientRect(); move(r.right - 20, r.top + 30); });
  hit.addEventListener("blur", hideTip);
  root.appendChild(hit);
  box.appendChild(root);
}

// ---------- daily bars ----------
function roundedBar(x, y0, y1, w, r) {
  // one rounded data-end (away from the baseline y0), square at the baseline
  const up = y1 < y0;
  const h = Math.abs(y1 - y0);
  r = Math.min(r, h, w / 2);
  if (up) return `M${x},${y0}V${y1 + r}Q${x},${y1} ${x + r},${y1}H${x + w - r}Q${x + w},${y1} ${x + w},${y1 + r}V${y0}Z`;
  return `M${x},${y0}V${y1 - r}Q${x},${y1} ${x + r},${y1}H${x + w - r}Q${x + w},${y1} ${x + w},${y1 - r}V${y0}Z`;
}
function drawDaily(state) {
  const box = $("chart-daily");
  const data = state.daily || [];
  const W = box.clientWidth || 600, H = box.clientHeight || 200;
  const m = { l: 60, r: 12, t: 12, b: 26 };
  box.replaceChildren();
  const vals = data.map((d) => d.pnl);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  if (lo === 0 && hi === 0) { hi = 1; lo = -1; }
  const ticks = niceTicks(lo, hi, 3);
  lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
  const Y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  const band = (W - m.l - m.r) / data.length;
  const bw = Math.min(24, band * 0.6);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Winst per dag" });
  const axis = svg("g", { class: "axis" });
  ticks.forEach((v) => {
    root.appendChild(svg("line", { class: v === 0 ? "baseline" : "gridline", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    const t = svg("text", { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end" }); t.textContent = compactMoney(v); axis.appendChild(t);
  });
  data.forEach((d, i) => {
    const cx = m.l + band * i + band / 2;
    const label = new Date(d.day + "T12:00:00").toLocaleDateString("nl-NL", { weekday: "short", day: "numeric" });
    const every = band < 44 ? 2 : 1;
    if ((i % every === 0 && !(every > 1 && i === data.length - 2)) || i === data.length - 1) {
      const t = svg("text", { x: cx, y: H - 6, "text-anchor": "middle" });
      t.textContent = i === data.length - 1 ? "vandaag" : label; axis.appendChild(t);
    }
    const hitR = svg("rect", { class: "bar-hit", x: cx - band / 2, y: m.t, width: band, height: H - m.t - m.b, tabindex: "0" });
    const tipText = () => [money(d.pnl, true), (i === data.length - 1 ? "vandaag" : label)];
    hitR.addEventListener("pointermove", (e) => { const [a, b] = tipText(); showTip(e.clientX, e.clientY, a, b); });
    hitR.addEventListener("pointerleave", hideTip);
    hitR.addEventListener("focus", () => { const r = hitR.getBoundingClientRect(); const [a, b] = tipText(); showTip(r.left + r.width / 2, r.top + 20, a, b); });
    hitR.addEventListener("blur", hideTip);
    root.appendChild(hitR);
    if (Math.abs(d.pnl) > 1e-9) {
      root.appendChild(svg("path", { class: d.pnl > 0 ? "bar-pos" : "bar-neg", d: roundedBar(cx - bw / 2, Y(0), Y(d.pnl), bw, 4), "pointer-events": "none" }));
    }
  });
  root.appendChild(axis);
  box.appendChild(root);

  fillTable($("daily-table"), [{ label: "Dag" }, { label: "Winst", num: true }],
    data.slice().reverse().map((d) => [d.day, { text: money(d.pnl, true), cls: dirClass(d.pnl) }]), "Geen gegevens");
}

// ---------- status bar 'Werkt alles?' ----------
const LEVEL = { critical: 3, warning: 2, good: 1, "": 0 };
const UP_REASON = { slept: " (Mac sliep)", closed: " (bot stond uit)", stalled: " (bot keek niet)" };
function statusChips(state) {
  const h = state.health || {};
  const src = h.sources || {};
  const chips = [];
  {
    // the bot loop: red when it did nothing for 3 rounds
    let lvl, long, short;
    if (state.status === "error") { lvl = "critical"; long = "Bot: fout"; short = "Bot fout"; }
    else if (!state.loop_alive) { lvl = "critical"; long = "Bot: staat stil"; short = "Bot uit"; }
    else if (!h.last_step) { lvl = "warning"; long = "Bot: start op…"; short = "Bot start"; }
    else {
      const age = Math.max(0, state.now - h.last_step);
      const limit = 3 * ((state.settings && state.settings.poll_seconds) || 10);
      lvl = age > limit ? "critical" : "good";
      long = age > limit ? `Bot: hangt (${ago(age)} niets gedaan)` : `Bot: draait (${ago(age)} geleden)`;
      short = `Bot ${ago(age)}`;
    }
    chips.push({ lvl, long, short });
  }
  {
    // weather: share of watched stations with a fresh report
    const st = h.stations || [];
    const fails = (src.weer && src.weer.consecutive_fail) || 0;
    let lvl = "", long = "Weerdata: laden…", short = "Weer …";
    if (st.length && (h.metar_last_ok || fails)) {
      const fresh = st.filter((x) => !x.stale).length, ratio = fresh / st.length;
      lvl = ratio < 0.5 || fails >= 3 ? "critical" : ratio < 0.9 ? "warning" : "good";
      long = fails >= 3 ? `Weerdata: storing (${fresh}/${st.length} vers)` : `Weerdata: ${fresh}/${st.length} stations vers`;
      short = `Weer ${fresh}/${st.length}`;
    } else if (fails >= 3) { lvl = "critical"; long = "Weerdata: storing"; short = "Weer ✗"; }
    else if (h.markets_ok && h.last_step && !st.length) { long = "Weerdata: nu geen markten"; short = "Weer –"; }
    chips.push({ lvl, long, short });
  }
  {
    // Polymarket: the market list and the order books
    const m = src.markten || {}, b = src.orderboek || {};
    const worst = Math.max(m.consecutive_fail || 0, b.consecutive_fail || 0);
    let lvl = "", long = "Polymarket: laden…";
    if (worst >= 3) { lvl = "critical"; long = "Polymarket: storing"; }
    else if (worst >= 1) { lvl = "warning"; long = "Polymarket: hapert"; }
    else if (m.last_ok_ts) { lvl = "good"; long = "Polymarket: OK"; }
    chips.push({ lvl, long, short: "Polymarket" });
  }
  {
    // live trading: grey in paper mode (nothing at stake), red in live mode when not allowed
    const g = h.geoblock, live = state.mode === "live";
    let lvl = "", long = "Live: nog niet gecontroleerd", short = "Live ?";
    if (g) {
      const where = g.country ? ` (${g.country})` : "";
      if (g.live_allowed) { lvl = live ? "good" : ""; long = `Live: toegestaan${where}`; short = "Live ja"; }
      else { lvl = live ? "critical" : ""; long = `Live: niet toegestaan${where}`; short = "Live nee"; }
    }
    chips.push({ lvl, long, short });
  }
  return chips;
}

function renderStatusBar(state) {
  const chips = statusChips(state);
  const box = $("sb-chips");
  // update the chips in place: a tap that spans a refresh must still land on the same button
  if (box.children.length !== chips.length) {
    box.replaceChildren();
    chips.forEach(() => {
      const b = el("button", { type: "button", class: "schip" });
      b.appendChild(el("span", { class: "dot" }));
      b.appendChild(el("span", { class: "long" }));
      b.appendChild(el("span", { class: "short", "aria-hidden": "true" }));
      b.addEventListener("click", openHealth);
      box.appendChild(b);
    });
  }
  let worst = "";
  chips.forEach((c, i) => {
    if (LEVEL[c.lvl] > LEVEL[worst]) worst = c.lvl;
    const b = box.children[i];
    b.className = "schip " + c.lvl;
    b.title = c.long + " · tik voor details";
    b.children[0].className = "dot " + c.lvl;
    b.children[1].textContent = c.long;
    b.children[2].textContent = c.short;
  });
  const dot = $("sb-dot");
  dot.className = "bigdot " + worst;
  dot.setAttribute("aria-label", worst === "critical" ? "Werkt alles? Nee, er is een storing"
    : worst === "warning" ? "Werkt alles? Bijna, iets hapert" : worst === "good" ? "Werkt alles? Ja" : "Werkt alles? Nog onbekend");
  const up = (state.health || {}).uptime;
  const line = $("sb-uptime");
  if (up && up.pct !== null && up.pct !== undefined) {
    const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
    const from = up.since * 1000 > midnight.getTime() + 60000 ? `Sinds ${clock(up.since)}` : "Vandaag";
    const pctTxt = up.missed_s < 60 ? "100" : nf0.format(Math.min(99, Math.floor(up.pct)));
    let text = `${from} ${pctTxt}% actief`;
    if (up.missed_s >= 60) text += ` · ${hm(up.missed_s)} gemist` + (UP_REASON[up.reason] || "");
    line.textContent = text;
    line.className = "sb-uptime" + (up.missed_s >= 60 && up.pct < 90 ? " warn" : "");
    line.hidden = false;
  } else line.hidden = true;
}

function setStatus(state) {
  $("mode").textContent = state.mode === "live" ? "LIVE · echt geld" : "Oefenen · nepgeld";
  renderStatusBar(state);
  setControls(state);

  const banner = $("banner");
  const msgs = [];
  if (state.error) msgs.push(["critical", state.error]);
  const h = state.health || {};
  // one missed round is normal on the internet; only warn when it keeps failing (~30 s+)
  if (h.metar_ok === false && (h.metar_fail_streak || 0) >= 3) msgs.push(["warning", "Weerdata komt al " + h.metar_fail_streak + " rondes niet binnen (beide bronnen): " + (h.metar_error || "onbekende fout")]);
  if (h.markets_ok === false) msgs.push(["warning", "Polymarket-markten konden niet geladen worden: " + (h.markets_error || "")]);
  if (h.power && h.power.on_battery) msgs.push(["warning", "Mac op batterij: sluit de lader aan, anders kan de Mac gaan slapen en stopt de bot."]);
  banner.replaceChildren();
  if (msgs.length) {
    msgs.forEach(([cls, text]) => {
      const row = el("div"); row.appendChild(el("span", { class: "dot " + cls })); row.appendChild(document.createTextNode(" " + text));
      banner.appendChild(row);
    });
    banner.hidden = false;
  } else banner.hidden = true;
}

// One toggle: 'Start' (loop off, or buying paused) / 'Pauzeer kopen' (buying). Selling never stops.
function setControls(state) {
  const running = state.status === "running";
  const halt = state.halt || { on: false };
  const buying = running && !halt.on;
  const btn = $("btn-start");
  btn.textContent = buying ? "Pauzeer kopen" : "Start";
  btn.dataset.action = !running ? "start" : buying ? "pause" : "resume";
  // only reasons that lift themselves left (weather data, access, daily loss): nothing to press
  btn.disabled = running && halt.on && !halt.can_resume;
  btn.title = btn.disabled ? "Gaat vanzelf verder zodra het probleem weg is" : "";
  $("btn-halt").disabled = (halt.reasons || []).some((r) => r.reason === "noodstop");

  const line = $("halt-line");
  if (halt.on) {
    const labels = (halt.reasons || []).map((r) => r.label);
    let text = `Gepauzeerd sinds ${clock(halt.since)} - ${labels.join(", ")}`;
    if (halt.can_resume) text += ". Druk op Start om weer te kopen.";
    else if (halt.auto_resume_at) text += `. Gaat vanzelf verder om ${clock(halt.auto_resume_at)}.`;
    else text += ". Gaat vanzelf verder als het probleem weg is.";
    line.replaceChildren(el("span", { class: "dot " + (halt.by === "user" ? "warning" : "critical") }),
      document.createTextNode(" " + text));
    if (state.loop_alive) line.appendChild(el("span", { class: "hide-phone" }, " Open posities worden gewoon verkocht."));
    line.hidden = false;
  } else line.hidden = true;

  const open = (state.positions || []).length;
  const stall = $("stall-banner");
  if (!state.loop_alive && open > 0) {
    stall.textContent = open === 1
      ? "Bot staat stil: 1 open positie wordt nu niet verkocht of gecontroleerd. Druk op Start."
      : `Bot staat stil: ${open} open posities worden nu niet verkocht of gecontroleerd. Druk op Start.`;
    stall.hidden = false;
  } else stall.hidden = true;
}

// ---------- open positions ----------
let SHOW_ALL_POS = false;
const POS_STATE = {
  selling: ["", (p) => `verkoop staat op ${nf2.format(p.sell_price)}`],
  confirmed: ["good", () => "bevestigd"],
  unconfirmed: ["critical", (p) => `ONBEVESTIGD ${ago(p.age_s)}`],
  waiting: ["warning", () => "wacht op uitslag"],
  exiting: ["critical", () => "verkoopt nu (meting klopte niet)"],
};
function renderPositions(state) {
  const ps = (state.positions || []).slice().sort((a, b) =>
    (a.state === "unconfirmed" || a.state === "exiting" ? 0 : 1) - (b.state === "unconfirmed" || b.state === "exiting" ? 0 : 1) || a.age_s - b.age_s);
  const cost = ps.reduce((sum, p) => sum + (p.cost || 0), 0);
  $("pos-head").textContent = `Open: ${ps.length}` + (ps.length ? ` · ${money(cost)} vast` : "");
  const phone = window.matchMedia("(max-width: 520px)").matches;
  const shown = SHOW_ALL_POS || !phone ? ps : ps.slice(0, 3);
  const ul = $("pos-list"); ul.replaceChildren();
  if (!ps.length) ul.appendChild(el("li", { class: "pos-empty" }, "Geen open posities."));
  shown.forEach((p) => {
    const li = el("li", { class: "pos-row" });
    const what = `${p.city} ${(KIND[p.kind] || "").toLowerCase()} ${p.bucket} · ${p.outcome}`;
    const bid = p.bid === null || p.bid === undefined ? "geen bod" : "bod nu " + nf3.format(p.bid);
    li.appendChild(el("span", { class: "pos-what" }, what));
    li.appendChild(el("span", { class: "pos-nums" }, `gekocht ${nf3.format(p.avg)} · ${bid} · ${ago(p.age_s)}`));
    const [cls, txt] = POS_STATE[p.state] || ["", () => p.state];
    li.appendChild(el("span", { class: "chip " + cls }, txt(p)));
    ul.appendChild(li);
  });
  const more = $("pos-more");
  more.hidden = !phone || ps.length <= 3;
  more.textContent = SHOW_ALL_POS ? "minder" : `meer (${ps.length - 3})`;
}

// ---------- 'Vandaag': equity incl. open positions ----------
function renderToday(state) {
  const b = state.bank, t = state.today || {};
  const live = state.mode === "live";
  const tp = $("today-pct");
  const rounded = Math.round((b.today_pct || 0) * 10) / 10;
  tp.textContent = pct(b.today_pct, 1) + " vandaag";
  tp.className = "hero-value " + dirClass(rounded);
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const since = b.started_at * 1000 > midnight.getTime() ? clock(b.started_at) : "00:00";
  $("today-since").textContent = "sinds " + since + " · open posities tellen mee";
  const n = b.today_trades || 0;
  $("today-line2").textContent = [money(b.today_equity_pnl, true), `${n} ${n === 1 ? "trade" : "trades"}`,
    `${b.today_wins || 0} gewonnen`, `${b.today_losses || 0} verloren`, `kosten ${money(b.fees_today || 0)}`].join(" · ");
  const parts = [`Saldo ${money(b.equity)} (start ${moneyShort(b.start_amount)} · ${pct(b.total_equity_pct, 1)} totaal)`];
  if (b.open_cost > 0.005) parts.push(`In posities ${money(b.open_cost)} (nu ${money(b.open_value)} waard)`);
  parts.push(`Volgende aankoop max ${money(b.next_trade_size)}`);
  const box = $("today-line3"); box.replaceChildren();
  // the dot sticks to the part before it (no-break space), so a wrapped line never starts with '·'
  parts.forEach((x, i) => {
    const span = el("span", {}, x);
    if (i < parts.length - 1) span.appendChild(el("span", { class: "sep", "aria-hidden": "true" }, "\u00a0·"));
    box.appendChild(span);
  });
  const badge = $("money-badge");
  badge.textContent = live ? "ECHT GELD" : "OEFENGELD - bovengrens";
  badge.className = "badge " + (live ? "live" : "paper");
  badge.title = live ? "De bot handelt met echt geld" : "Oefenen vult elke order tegen het echte orderboek; in het echt is een snellere bot je soms voor";
  const stop = $("today-stop");
  stop.hidden = !b.stopped_for_today;
  stop.textContent = b.stopped_for_today ? "Dagverlies-stop actief: de bot koopt vandaag niets meer. Open posities worden gewoon verkocht." : "";
  const norm = $("today-normal");
  if (t.normal_range) {
    norm.textContent = `normaal voor jouw bot: ${pct(t.normal_range[0], 1)} tot ${pct(t.normal_range[1], 1)} per dag`;
    norm.hidden = false;
  } else norm.hidden = true;
  $("today-note").textContent = live ? ""
    : "Oefengeld is een bovengrens: in het echt pakt een snellere bot soms eerst de goedkope orders.";
}

// ---------- 'Vandaag gezien': from dead bucket to sold, and why not ----------
function renderFunnel(state) {
  const f = (state.today && state.today.funnel) || { signalen: 0, met_goedkope_orders: 0, gekocht: 0, verkocht: 0, skips: {} };
  const steps = [[f.signalen, "vakken dood"], [f.met_goedkope_orders, "met goedkope orders"], [f.gekocht, "gekocht"], [f.verkocht, "verkocht"]];
  const ol = $("funnel"); ol.replaceChildren();
  ol.setAttribute("aria-label", steps.map(([v, l]) => `${v} ${l}`).join(", "));
  steps.forEach(([v, l]) => {
    const li = el("li");
    li.appendChild(el("strong", {}, nf0.format(v || 0)));
    li.appendChild(el("span", {}, l));
    ol.appendChild(li);
  });
  const labels = state.skip_labels || {};
  const entries = Object.entries(f.skips || {}).sort((a, b) => b[1] - a[1]);
  const ul = $("skips"); ul.replaceChildren();
  if (!entries.length) ul.appendChild(el("li", { class: "muted" }, f.signalen ? "Nog niets overgeslagen." : "Nog geen dode vakken vandaag."));
  entries.slice(0, 3).forEach(([reason, count]) => {
    const li = el("li");
    li.appendChild(el("span", {}, labels[reason] || reason));
    li.appendChild(el("span", { class: "count" }, nf0.format(count)));
    ul.appendChild(li);
  });
  if (entries.length > 3) {
    const rest = entries.slice(3).reduce((a, e) => a + e[1], 0);
    ul.appendChild(el("li", { class: "muted" }, `en ${rest} om ${entries.length - 3 === 1 ? "één andere reden" : (entries.length - 3) + " andere redenen"}`));
  }
}

// ---------- 'HighTempTation vandaag' ----------
const HTT_ICON = {
  gekocht: ["good", () => "✓", () => "ook gekocht"],
  gezien_te_laat: ["warning", (r) => "⏱ " + (r.delta_s === null ? "" : deltaShort(r.delta_s)), () => "gezien, maar goedkope orders al weg"],
  gezien_overgeslagen: ["", (r) => r.delta_s === null ? "–" : deltaShort(r.delta_s), (r, labels) => r.reason ? "gezien, niet gekocht: " + (labels[r.reason] || r.reason).toLowerCase() : "gezien, (nog) niet gekocht"],
  niet_gezien: ["critical", () => "✗", () => "niet gezien (station, afronding of tijdzone?)"],
};
function renderHtt(state) {
  const r = state.htt;
  const sum = $("htt-summary"), list = $("htt-list"), missed = $("htt-missed"), upd = $("htt-updated");
  list.replaceChildren();
  missed.hidden = true;
  upd.textContent = "";
  if (!r) { sum.textContent = "Wordt opgehaald…"; return; }
  if (!r.ok) {
    sum.textContent = "Kon zijn trades niet ophalen (" + (r.error || "onbekende fout") + "). Over 2 minuten opnieuw.";
    return;
  }
  const updated = r.error_at && r.error_at > (r.updated_at || 0) ? " · laatste poging mislukt" : "";
  upd.textContent = "Bijgewerkt " + ago(Math.max(0, state.now - r.updated_at)) + " geleden" + updated;
  if (!r.his_n) { sum.textContent = "HighTempTation heeft vandaag nog niets gekocht."; return; }
  const parts = [`${r.his_n} ${r.his_n === 1 ? "vak" : "vakken"}`, `jouw bot zag ${r.seen_n}`, `eerder dan hij ${r.first_n}×`, `gekocht ${r.bought_n}`];
  if (r.median_delta_s !== null && r.median_delta_s !== undefined) parts.push("mediaan " + deltaText(r.median_delta_s));
  sum.textContent = parts.join(" · ");
  missed.textContent = `Geschat gemist: ${money(r.missed_est)}`;
  missed.hidden = false;
  const labels = state.skip_labels || {};
  (r.rows || []).slice(0, 5).forEach((row) => {
    const [cls, icon, why] = HTT_ICON[row.status] || ["", () => "?", () => row.status];
    const li = el("li");
    li.appendChild(el("span", { class: "htt-icon " + cls }, icon(row)));
    const body = el("span", { class: "htt-body" });
    body.appendChild(el("span", { class: "htt-what" }, row.label || row.title));
    body.appendChild(el("span", { class: "htt-sub" }, `hij ${clock(row.his_ts)} · ${nf3.format(row.his_price)} · ${money(row.his_usdc)} · ${why(row, labels)}`));
    li.appendChild(body);
    list.appendChild(li);
  });
}

function render(state) {
  STATE = state;
  setStatus(state);
  renderToday(state);
  renderFunnel(state);
  renderHtt(state);

  drawBalance(state);
  drawDaily(state);

  // trades today
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const closes = (state.trades || []).filter((r) => r.type === "close" && r.ts * 1000 >= midnight.getTime());
  $("trades-hint").textContent = closes.length ? `${closes.length} afgesloten` : "";
  fillTable($("trades-table"),
    [{ label: "Tijd" }, { label: "Stad" }, { label: "Markt" }, { label: "Vak" }, { label: "Kant" },
     { label: "Inzet", num: true }, { label: "Resultaat", num: true }, { label: "%", num: true }, { label: "Duur", num: true }, { label: "Hoe" }],
    closes.map((r) => [clock(r.ts), r.city, KIND[r.kind] || "", r.bucket, el("span", { class: "tag" }, r.outcome),
      money(r.cost), { text: money(r.pnl, true), cls: dirClass(r.pnl) }, { text: pct(r.pct), cls: dirClass(r.pct) },
      ago(r.held_s), HOW[r.how] || r.how]),
    "Vandaag nog niets afgesloten. Zodra een meting een vak onmogelijk maakt en er goedkope orders liggen, koopt de bot.");

  renderPositions(state);

  // health
  const h = state.health || {};
  const now = state.now;
  const items = [];
  const metarAge = h.metar_at ? now - h.metar_at : null;
  items.push(["Bot", state.status === "running" ? "good" : state.status === "error" ? "critical" : "", state.status === "running" ? `draait sinds ${state.started_at ? clock(state.started_at) : "–"}` : (state.status === "error" ? "fout" : "gestopt")]);
  {
    const streak = h.metar_fail_streak || 0, fails = h.metar_failures_1h || 0;
    let cls = "", text = "nog niet opgehaald";
    if (h.metar_ok) {
      cls = "good";
      text = `ok · ${ago(metarAge)} geleden` + (h.metar_source && h.metar_primary && h.metar_source !== h.metar_primary ? ` · via reservebron (${h.metar_source})` : "");
    } else if (h.metar_ok === false) {
      cls = streak >= 3 ? "critical" : "warning";
      text = streak >= 3 ? `storing · ${streak} rondes mislukt` : "even geen antwoord · volgende ronde opnieuw";
    }
    if (fails) text += ` · ${fails}× korte storing afgelopen uur (vanzelf hersteld)`;
    items.push(["Weerdata", cls, text]);
  }
  items.push(["Markten", h.markets_ok ? "good" : h.markets_ok === false ? "critical" : "", h.markets_ok ? `${(state.events || []).length} actief` : h.markets_ok === false ? "fout" : "nog niet geladen"]);
  {
    const b = (h.sources || {}).orderboek || {};
    const fails = b.consecutive_fail || 0;
    let cls = "", text = "nog niet nodig gehad";
    if (fails) { cls = fails >= 3 ? "critical" : "warning"; text = `${fails}× achter elkaar mislukt` + (b.last_error ? ` · ${b.last_error}` : ""); }
    else if (b.last_ok_ts) { cls = "good"; text = `ok · ${ago(now - b.last_ok_ts)} geleden`; }
    items.push(["Orderboek", cls, text]);
  }
  if (h.uptime && h.uptime.pct !== null && h.uptime.pct !== undefined) {
    const u = h.uptime;
    const parts = [`${hm(u.active_s)} actief`];
    if (u.missed_s >= 60) parts.push(`${hm(u.missed_s)} gemist`);
    if (u.slept_s >= 60) parts.push(`Mac sliep ${hm(u.slept_s)}`);
    if (u.closed_s >= 60) parts.push(`bot stond ${hm(u.closed_s)} uit`);
    items.push(["Vandaag", u.missed_s < 60 ? "good" : u.pct >= 90 ? "warning" : "critical", parts.join(" · ")]);
  }
  items.push(["Laatste ronde", h.last_step && now - h.last_step < 60 ? "good" : h.last_step ? "warning" : "", h.last_step ? `${ago(now - h.last_step)} geleden · ${h.step_ms} ms` : "–"]);
  {
    // every station has its own report moments; around them the bot looks every 2 s
    const hot = h.hot_stations || [];
    const every = (state.settings && state.settings.hot_poll_seconds) || 2;
    let cls = "", text = "wacht op het volgende meetmoment";
    if (h.hot_ok === false) { cls = "warning"; text = `snelle bron (NOAA) geeft geen antwoord · nu elke ${nf0.format(state.settings.poll_seconds)} s via de gewone bron`; }
    else if (hot.length) {
      cls = "good";
      const names = hot.slice(0, 4).join(", ") + (hot.length > 4 ? " …" : "");
      text = `${hot.length === 1 ? "1 station" : hot.length + " stations"} nu elke ${every} s, hun meting komt eraan (${names})`;
    }
    items.push(["Snel kijken", cls, text]);
  }
  if (h.clock_skew_s !== null && h.clock_skew_s !== undefined) {
    const s = h.clock_skew_s, off = Math.abs(s);
    const cls = off > 10 ? (state.mode === "live" ? "critical" : "warning") : off > 2 ? "warning" : "good";
    const text = off <= 2 ? "loopt gelijk met Polymarket"
      : `loopt ${nf0.format(off)} s ${s > 0 ? "voor" : "achter"} · zet in Systeeminstellingen 'Datum en tijd automatisch' aan` + (off > 10 && state.mode === "live" ? " · kopen gepauzeerd" : "");
    items.push(["Klok", cls, text]);
  }
  if (h.power) {
    const p = h.power;
    items.push(["Stroom", p.on_battery ? "warning" : "good", p.on_battery
      ? "Mac op batterij" + (p.percent !== null && p.percent !== undefined ? ` (${p.percent}%)` : "") + " · sluit de lader aan"
      : "aan de lader"]);
  }
  if (h.last_wake && now - h.last_wake.to < 86400) {
    const w = h.last_wake;
    items.push(["Onderbreking", now - w.to < 3600 ? "warning" : "", `${w.slept ? "Mac sliep" : "bot keek niet"} van ${clock(w.from)} tot ${clock(w.to)}`]);
  }
  if (h.geoblock) {
    const g = h.geoblock, where = g.country || "?";
    // grey in paper mode (nothing at stake), red when live trading is not possible in live mode
    const cls = g.live_allowed ? "good" : state.mode === "live" ? "critical" : "";
    items.push(["Live", cls, g.live_allowed ? `toegestaan (${where})` : `niet toegestaan (${where})` + (state.mode === "live" ? " · kopen gepauzeerd" : " · alleen oefenmodus")]);
  }
  const ul = $("health"); ul.replaceChildren();
  items.forEach(([what, cls, text]) => {
    const li = el("li"); li.appendChild(el("span", { class: "dot " + cls })); li.appendChild(el("span", { class: "what" }, what)); li.appendChild(el("span", {}, text));
    ul.appendChild(li);
  });
  renderStations(h);

  renderEvents(state.events || []);

  const result = (s) => {
    if (s.result === "bought") return el("span", { class: "chip good" }, "gekocht");
    if (s.result_label) return el("span", { class: "muted" }, s.result_label);
    return el("span", { class: "muted" }, "–");
  };
  fillTable($("signals-table"),
    [{ label: "Tijd" }, { label: "Stad" }, { label: "Vak" }, { label: "Kant" }, { label: "Resultaat" }, { label: "Waarom dood" }, { label: "Vertraging", num: true }],
    (state.signals || []).slice(0, 25).map((s) => [clock(s.ts), `${s.city} ${KIND[s.kind] || ""}`, s.bucket, el("span", { class: "tag" }, s.outcome), result(s), s.reason, ago(s.lag_s)]),
    "Nog geen signalen.");

  $("logs").textContent = (state.logs || []).map((l) => `${clock(l.ts)} ${l.level === "INFO" ? "" : l.level + " "}${l.msg}`).join("\n");
}

// ---------- details sheet: per station, last errors ----------
const ERR_WHAT = { weer: "Weerdata", markten: "Markten", orderboek: "Orderboek", "snel kijken": "Snel kijken", bot: "Bot", buy: "Kopen", cancel: "Annuleren" };
function renderStations(h) {
  const rows = (h.stations || []).slice().sort((a, b) => (b.stale ? 1 : 0) - (a.stale ? 1 : 0) || a.station.localeCompare(b.station));
  const kinds = (k) => (k || []).map((x) => (KIND[x] || x).toLowerCase()).join("/");
  fillTable($("stations-table"),
    [{ label: "Station" }, { label: "Stad" }, { label: "Laatste meting", num: true }, { label: "Leeftijd", num: true },
     { label: "Ritme", num: true }, { label: "Gezien na meettijd", num: true }],
    rows.map((r) => {
      // the freshness dot sits next to the station code, so it shows without scrolling on a phone
      const st = el("span", { title: r.stale ? "geen verse meting" : "vers" });
      st.appendChild(el("span", { class: "dot " + (r.stale ? "warning" : "good") }));
      st.appendChild(document.createTextNode(" " + r.station));
      return [st, `${(r.cities || []).join(", ")} ${kinds(r.kinds)}`.trim(),
        r.last_temp_c === null || r.last_temp_c === undefined ? "–" : `${r.last_temp_c}°C`,
        r.age_s === null ? "–" : ago(r.age_s),
        `${nf0.format(r.cadence_min)} min`, r.publish_delay_s_p50 === null ? "–" : `${r.publish_delay_s_p50} s`];
    }),
    "Nog geen stations: de bot laadt eerst de markten.");
  const d = h.seen_delay_s_p50;
  $("seen-delay").textContent = d === null || d === undefined
    ? "Nog geen nieuwe meting live gezien (dat gebeurt rond het eigen meetmoment van elk station). HighTempTation koopt gemiddeld ~80 s na de meettijd."
    : `Jouw bot ziet een nieuwe meting meestal ${d} s na de meettijd (HighTempTation koopt gemiddeld ~80 s na de meettijd).`;
  const ul = $("errors-list"); ul.replaceChildren();
  const errs = h.errors || [];
  if (!errs.length) ul.appendChild(el("li", { class: "muted" }, "Geen fouten."));
  errs.slice(0, 5).forEach((e) => {
    const li = el("li");
    li.appendChild(el("span", { class: "when" }, clock(e.ts)));
    li.appendChild(el("span", { class: "what" }, ERR_WHAT[e.what] || e.what));
    li.appendChild(el("span", { class: "text" }, e.text));
    ul.appendChild(li);
  });
}
function openHealth() {
  const d = $("health-dialog");
  if (!d.open) d.showModal();
}

let SHOW_ALL_EVENTS = false;
function renderEvents(events) {
  // one row per city: max and min side by side
  // today's and tomorrow's market of a city are separate rows (tomorrow shows from 23:30 local)
  const byCity = new Map();
  events.forEach((e) => {
    const k = e.city + "|" + e.station + "|" + (e.day || "");
    if (!byCity.has(k)) byCity.set(k, { city: e.city, station: e.station, local_time: e.local_time, tomorrow: !!e.tomorrow, high: null, low: null, age: null, ev: e });
    const row = byCity.get(k);
    row[e.kind] = e;
    if (e.obs_age_min !== null && (row.age === null || e.obs_age_min < row.age)) row.age = e.obs_age_min;
  });
  let rows = Array.from(byCity.values());
  // busiest first: most dead/certain buckets, then cities with fresh data, then A-Z
  rows.sort((a, b) => {
    const da = (a.high ? a.high.dead : 0) + (a.low ? a.low.dead : 0);
    const db = (b.high ? b.high.dead : 0) + (b.low ? b.low.dead : 0);
    if (db !== da) return db - da;
    if ((a.age === null) !== (b.age === null)) return a.age === null ? 1 : -1;
    return a.city.localeCompare(b.city) || (a.tomorrow ? 1 : 0) - (b.tomorrow ? 1 : 0);
  });
  const total = rows.length;
  if (!SHOW_ALL_EVENTS) rows = rows.slice(0, 12);
  const cell = (e) => {
    if (!e) return "–";
    const span = el("span");
    span.appendChild(document.createTextNode(e.observed === null ? "geen meting" : `${e.observed}°${e.unit}`));
    span.appendChild(el("span", { class: "cell-sub" }, ` · ${e.dead}/${e.buckets} dood`));
    return span;
  };
  const city = (r) => {
    if (!r.tomorrow) return r.city;
    const span = el("span"); span.appendChild(document.createTextNode(r.city + " "));
    span.appendChild(el("span", { class: "tag", title: "De meting van 00:00 komt vaak een paar minuten eerder uit" }, "morgen"));
    return span;
  };
  const when = (e) => {
    const span = el("span");
    const mins = (e.report_minutes || []).map((m) => ":" + String(m).padStart(2, "0"));
    const list = mins.length > 1 ? mins.slice(0, -1).join(", ") + " en " + mins[mins.length - 1] : mins.join("");
    span.appendChild(document.createTextNode(list ? "rond " + list : "–"));
    span.appendChild(el("span", { class: "cell-sub" }, e.schedule === "learned" ? " · gemeten" : " · geschat"));
    if (e.hot) {
      span.appendChild(document.createTextNode(" "));
      span.appendChild(el("span", { class: "chip good", title: "Deze meting komt eraan: de bot kijkt nu elke 2 seconden" }, "nu"));
    }
    return span;
  };
  const delay = (e) => {
    if (e.delay_s === null || e.delay_s === undefined) return "–";
    const d = Math.round(e.delay_s);
    return d >= 0 ? `${d} s` : `${-d} s eerder`;
  };
  fillTable($("events-table"),
    [{ label: "Stad" }, { label: "Station" }, { label: "Lokale tijd" }, { label: "Max", num: true },
     { label: "Min", num: true }, { label: "Laatste meting", num: true }, { label: "Nieuwe meting" },
     { label: "Gezien na meettijd", num: true }],
    rows.map((r) => {
      const fresh = r.age === null ? "" : r.age <= 75 ? "good" : "warning";
      const age = el("span"); age.appendChild(el("span", { class: "dot " + fresh }));
      age.appendChild(document.createTextNode(" " + (r.age === null ? "–" : nf0.format(r.age) + " min")));
      return [city(r), r.station, r.local_time, cell(r.high), cell(r.low), age, when(r.ev), delay(r.ev)];
    }),
    "Nog geen markten geladen.");
  const more = $("events-more");
  more.hidden = total <= 12;
  more.textContent = SHOW_ALL_EVENTS ? "Toon minder" : `Toon alle ${total} steden`;
}

async function poll() {
  clearTimeout(pollTimer);
  try {
    render(await api("/api/state"));
  } catch (e) {
    const banner = $("banner");
    banner.replaceChildren(el("span", { class: "dot critical" }), document.createTextNode(" Geen verbinding met de bot. Is Weerbot afgesloten?"));
    banner.hidden = false;
  }
  pollTimer = setTimeout(poll, document.hidden ? 10000 : 2000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

// ---------- controls ----------
$("btn-start").addEventListener("click", async () => {
  const btn = $("btn-start");
  const action = btn.dataset.action || "start";
  btn.disabled = true;
  try {
    if (action === "pause") await api("/api/pause", { on: true });
    else {
      const r = await api(action === "resume" ? "/api/resume" : "/api/start", {});
      if (!r.ok && r.error) alert(r.error);
    }
  } catch (e) { alert(e.message); }
  poll();
});
$("btn-halt").addEventListener("click", async () => {
  if (!confirm("Noodstop: de bot koopt niets meer tot je op Start drukt.\n\nOpen posities blijven te koop staan voor 0,99 en worden gewoon verder verkocht. Doorgaan?")) return;
  try { await api("/api/halt", {}); } catch (e) { alert(e.message); }
  poll();
});
$("pos-more").addEventListener("click", () => { SHOW_ALL_POS = !SHOW_ALL_POS; if (STATE) renderPositions(STATE); });
$("sb-uptime").addEventListener("click", openHealth);
$("btn-close-health").addEventListener("click", () => $("health-dialog").close());
$("btn-quit").addEventListener("click", async () => {
  const open = STATE ? (STATE.positions || []).length : 0;
  const question = open > 0
    ? `Er ${open === 1 ? "staat 1 positie" : `staan ${open} posities`} open; die worden pas verkocht als de app weer draait. Toch afsluiten?`
    : "Weerbot afsluiten? De bot stopt dan met handelen.";
  if (!confirm(question)) return;
  try { await api("/api/shutdown", {}); } catch (e) { /* already gone */ }
  clearTimeout(pollTimer);
  document.body.replaceChildren(el("main", {}, "Weerbot is afgesloten. Je kunt dit venster sluiten."));
});
document.querySelectorAll("#range button").forEach((btn) => btn.addEventListener("click", () => {
  RANGE = btn.dataset.range;
  document.querySelectorAll("#range button").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
  if (STATE) drawBalance(STATE);
}));
let resizeTimer = null;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (STATE) { drawBalance(STATE); drawDaily(STATE); } }, 150); });

$("events-more").addEventListener("click", () => { SHOW_ALL_EVENTS = !SHOW_ALL_EVENTS; if (STATE) renderEvents(STATE.events || []); });

$("btn-compare").addEventListener("click", async () => {
  const out = $("compare-out"); const btn = $("btn-compare");
  btn.disabled = true; out.textContent = "Bezig met ophalen van zijn trades…";
  try {
    const { result: r } = await api("/api/compare", {});
    out.replaceChildren();
    if (!r.ok) { out.textContent = r.message; return; }
    if (!r.his_buckets) { out.textContent = "HighTempTation heeft niets gekocht in de periode dat jouw bot draaide."; return; }
    const lines = [
      `In deze periode kocht hij ${r.his_buckets} vakken (${money(r.his_usdc)}).`,
      `Jouw bot vond er ${r.matched} van (${r.match_pct}%). Bij ${r.bot_saw_cheap_asks} lagen er ook nog goedkope orders, ${r.bot_bought_too}× kocht jouw bot mee.`,
      r.median_delta_s === null ? "" : `Jouw bot was ${r.bot_first}× eerder en ${r.bot_later}× later; mediaan verschil ${r.median_delta_s > 0 ? "+" : ""}${r.median_delta_s} s (min = jouw bot eerst).`,
    ].filter(Boolean);
    lines.forEach((l) => out.appendChild(el("p", {}, l)));
    if (r.missed && r.missed.length) {
      const d = el("details"); d.appendChild(el("summary", {}, `Gemiste vakken (${r.missed.length})`));
      const ul = el("ul"); r.missed.forEach((m) => ul.appendChild(el("li", {}, `${m.title} — ${m.outcome} @ ${m.price}`)));
      d.appendChild(ul); out.appendChild(d);
    }
  } catch (e) { out.textContent = "Mislukt: " + e.message; }
  finally { btn.disabled = false; }
});

// ---------- settings dialog ----------
function buildFields(s) {
  const fs = $("fields");
  fs.querySelectorAll(".field").forEach((n) => n.remove());
  s.fields.forEach((f) => {
    if (f.key === "FUNDER" || f.key === "SIGNATURE_TYPE") return; // shown under "Live account"
    fs.appendChild(fieldRow(f, s.values[f.key]));
  });
  const live = $("live-fieldset");
  live.querySelectorAll(".field.dyn").forEach((n) => n.remove());
  s.fields.filter((f) => f.key === "FUNDER" || f.key === "SIGNATURE_TYPE").forEach((f) => {
    const row = fieldRow(f, s.values[f.key]); row.classList.add("dyn"); live.appendChild(row);
  });
  const blocked = $("live-blocked");
  blocked.hidden = !!s.live_allowed;
  blocked.textContent = s.live_allowed ? "" : (s.live_block_text || "Live handelen is vanaf hier niet toegestaan. Alleen oefenmodus.");
  $("pk-help").textContent = s.private_key_set
    ? "Opgeslagen" + (s.keychain ? " in je Mac-sleutelhanger" : "") + ". Laat leeg om zo te houden."
    : "Alleen nodig voor live. Wordt " + (s.keychain ? "in je Mac-sleutelhanger" : "lokaal") + " bewaard en nooit meer getoond.";
  $("private-key").value = "";
  applyChecks(s.checks || {});
  if (s.min_start_amount) {
    $("start-amount").min = String(s.min_start_amount);
    $("min-start").textContent = `Minimaal ${moneyShort(s.min_start_amount)} bij ${nf2.format(Number(s.values.TRADE_PCT)).replace(/,00$/, "")}% per aankoop: Polymarket verkoopt minstens 5 shares per order.`;
  }
}

// green = advised, amber = allowed but risky, red = not allowed; plus what it means in $ right now
function applyChecks(checks) {
  Object.entries(checks).forEach(([key, c]) => {
    const input = document.querySelector(`#settings-form [data-key="${key}"]`);
    const line = $("c-" + key);
    if (!input || !line) return;
    input.classList.remove("lvl-good", "lvl-warn", "lvl-bad");
    if (c.level) input.classList.add("lvl-" + c.level);
    line.className = "conseq" + (c.level ? " " + c.level : "");
    let text = c.error || c.text || "";
    if (!c.error && c.level === "warn" && c.safe) text += (text ? " · " : "") + c.safe;
    line.textContent = text;
    line.hidden = !text;
  });
}
function currentValues() {
  const values = {};
  document.querySelectorAll("#settings-form [data-key]").forEach((i) => {
    values[i.dataset.key] = i.dataset.kind === "bool" ? (i.checked ? "true" : "false") : i.value;
  });
  return values;
}
let checkTimer = null;
function scheduleCheck() {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(async () => {
    try {
      const r = await api("/api/settings/check", { values: currentValues() });
      applyChecks(r.checks || {});
    } catch (e) { /* the save button reports problems */ }
  }, 250);
}
function fieldRow(f, value) {
  const row = el("div", { class: "field" });
  const id = "f-" + f.key;
  row.appendChild(el("label", { for: id }, f.label));
  let input;
  if (f.kind === "choice") {
    input = el("select", { id });
    f.choices.forEach((c) => {
      const o = el("option", { value: c }, c === "paper" ? "paper (oefenen)" : c === "live" ? "live (echt geld)" : c);
      if (f.key === "MODE" && c === "live" && SETTINGS && !SETTINGS.live_allowed && value !== "live") o.disabled = true;
      input.appendChild(o);
    });
    input.value = value;
  } else if (f.kind === "bool") {
    input = el("input", { id, type: "checkbox" }); input.checked = value === "true";
  } else {
    input = el("input", { id, type: f.kind === "text" ? "text" : "number" });
    if (f.kind !== "text") { input.step = f.kind === "int" ? "1" : "any"; if (f.lo !== null) input.min = f.lo; if (f.hi !== null) input.max = f.hi; }
    input.value = value;
  }
  input.dataset.key = f.key; input.dataset.kind = f.kind;
  row.appendChild(input);
  if (f.kind === "float" || f.kind === "int") {
    input.addEventListener("input", scheduleCheck);
    row.appendChild(el("div", { class: "conseq", id: "c-" + f.key, "aria-live": "polite", hidden: "" }));
  }
  const help = f.key === "MODE" && SETTINGS && !SETTINGS.live_allowed ? (SETTINGS.live_block_text || f.help) : f.help;
  if (help) row.appendChild(el("div", { class: "help" }, help));
  return row;
}
$("btn-settings").addEventListener("click", async () => {
  $("settings-msg").textContent = ""; $("bank-msg").textContent = ""; $("check-out").textContent = "";
  try {
    SETTINGS = await api("/api/settings");
    buildFields(SETTINGS);
    if (STATE) $("start-amount").value = STATE.bank.start_amount;
    $("settings-dialog").showModal();
  } catch (e) { alert(e.message); }
});
$("btn-close-settings").addEventListener("click", () => $("settings-dialog").close());
$("btn-save").addEventListener("click", async () => {
  const msg = $("settings-msg");
  const values = currentValues();
  if (values.MODE === "live" && SETTINGS && SETTINGS.values.MODE !== "live" &&
      !confirm("Live = echt geld. De bot handelt alleen als Polymarket dat vanaf jouw locatie toestaat. Doorgaan?")) return;
  const body = { values };
  const pk = $("private-key").value.trim();
  if (pk) body.private_key = pk;
  msg.className = "msg"; msg.textContent = "Opslaan…";
  try {
    SETTINGS = await api("/api/settings", body);
    buildFields(SETTINGS);
    msg.className = "msg ok"; msg.textContent = "Opgeslagen";
    poll();
  } catch (e) { msg.className = "msg error"; msg.textContent = e.message; }
});
$("btn-reset-bank").addEventListener("click", async () => {
  const msg = $("bank-msg");
  const v = $("start-amount").value;
  if (!confirm(`Opnieuw beginnen met $${v}? De grafiek en de winst tot nu toe worden gewist.`)) return;
  try {
    await api("/api/bankroll", { start_amount: v });
    msg.className = "msg ok"; msg.textContent = "Nieuw startbedrag ingesteld";
    poll();
  } catch (e) { msg.className = "msg error"; msg.textContent = e.message; }
});
$("btn-check").addEventListener("click", async () => {
  const out = $("check-out"); const btn = $("btn-check");
  btn.disabled = true; out.textContent = "Bezig (kan 10-20 seconden duren)…";
  try {
    const { result: r } = await api("/api/check", {});
    const lines = [];
    lines.push(`Live handelen: ${r.geoblock.live_allowed ? "toegestaan" : "niet toegestaan, alleen oefenmodus"} (${r.geoblock.country || r.geoblock.error || "?"})`);
    lines.push(`Markten: ${r.markets && r.markets.ok ? r.markets.count + " actief" : "fout: " + (r.markets && r.markets.error)}`);
    lines.push(`Weerdata (${r.weather ? r.weather.source : "?"}): ${r.weather && r.weather.ok ? "ok, " + r.weather.reports + " metingen" : "fout: " + (r.weather && r.weather.error)}`);
    if (r.account) lines.push(`Live account: ${r.account.ok ? "ok, saldo $" + r.account.usdc : "fout: " + r.account.error}`);
    out.replaceChildren(); lines.forEach((l) => out.appendChild(el("div", {}, l)));
  } catch (e) { out.textContent = "Mislukt: " + e.message; }
  finally { btn.disabled = false; }
});

poll();
