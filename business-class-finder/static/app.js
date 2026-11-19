/* Business Class Finder */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (v) => (typeof v === "number" ? "$" + Math.round(v).toLocaleString("en-US") : "--");
const enc = encodeURIComponent;
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
function ago(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : d + " days ago";
}
const niceDate = (iso) => (iso ? new Date(iso + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" }) : "");
async function api(path) {
  const r = await fetch(path);
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, ms = 4000) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.add("hidden"), ms);
}

let ST = null;
let showAll = false;
const LVL = { low: ["low", "Good price"], typical: ["typical", "Typical"], high: ["high", "High"] };
/* Google's verdict/range describe the cheapest business fare overall (often a recliner seat). Use it as-is only when
   the headline (lie-flat) fare IS that fare; otherwise rate the headline price against Google's range ourselves. */
function levelFor(r) {
  const ins = (r && r.insights) || {};
  if (!r || !r.lowest) return null;
  if (!r.cheapest_any || r.cheapest_any >= r.lowest) return ins.level || null;
  if (!ins.typical) return null;
  return r.lowest < ins.typical[0] ? "low" : r.lowest <= ins.typical[1] ? "typical" : "high";
}
const sameAsCheapest = (r) => !r.cheapest_any || r.cheapest_any >= r.lowest;
const stopsTxt = (n) => (n === 0 ? "nonstop" : plural(n, "stop"));

async function load() {
  ST = await api("/api/state");
  render();
}

/* ---------------- points */
const kfmt = (m) => (m >= 1000 ? Math.round(m / 1000) + "k" : String(m));
const wordRe = {};
const hasAirline = (name, alias) => (wordRe[alias] = wordRe[alias] || new RegExp(`\\b${alias.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i")).test(name);
function programsFor(airlines, zone, cash) {
  const P = ST.points, mine = ST.settings.cards || [];
  const out = [];
  for (const p of P.programs) {
    const m = p.miles[zone];
    if (!m || !airlines.length) continue;
    const ok = airlines.every((a) => p.airlines.some((x) => hasAirline(a, x)) || p.alliances.some((al) => P.alliances[al].some((x) => hasAirline(a, x))));
    if (!ok) continue;
    const milesRT = m[0] * 2, milesRThi = m[1] * 2, taxesRT = ((p.taxes[0] + p.taxes[1]) / 2) * 2;
    const cpp = cash ? Math.max(0, ((cash - taxesRT) / milesRT) * 100) : null;
    const via = p.cards.filter((c) => mine.includes(c));
    out.push({ p, milesRT, milesRThi, taxesRT, cpp, via, reachable: via.length > 0 });
  }
  out.sort((a, b) => (b.reachable - a.reachable) || (b.cpp - a.cpp));
  return out;
}
const cppRate = (c) => (c >= 3 ? ["great", "Excellent use of points"] : c >= 2 ? ["good", "Good use of points"] : c >= 1.3 ? ["ok", "OK"] : ["poor", "Better to pay cash"]);
function bestPoints(k) {
  const [, d] = k.split("|");
  const r = ST.routes[k].result;
  if (!r || !r.options || !r.options.length) return null;
  const op = r.options[0];
  return programsFor(op.airlines, ST.zones[d], op.price).find((x) => x.reachable) || null;
}

function render() {
  const s = ST.settings;
  const whenTxt = s.when === "date" ? "" : s.when === "later" ? " <span class='muted'>(about 6 weeks out)</span>" : ` <span class='muted'>(soonest with ${s.notice_days} business days' notice)</span>`;
  $("#trip").innerHTML = `Round trip <b>${niceDate(s.depart)}</b> &rarr; <b>${niceDate(s.return)}</b> &middot; ${plural(s.nights, "night")}${whenTxt} &middot; change`;
  $("#check-all").disabled = !s.has_key || ST.job.running;
  renderSetup();
  renderBest();
  renderBoard();
}

/* ---------------- setup (no key yet) */
function renderSetup() {
  const box = $("#setup");
  if (ST.settings.has_key) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.innerHTML = `<div class="card"><h2 style="margin-top:0">One-time setup: get live prices (free, about 2 minutes)</h2>
    <ol>
      <li>Create a free account at <a href="https://serpapi.com/users/sign_up" target="_blank" rel="noopener">serpapi.com</a>. The free plan includes a set number of searches each month.</li>
      <li>After signing in, copy <b>Your Private API Key</b> from <a href="https://serpapi.com/manage-api-key" target="_blank" rel="noopener">serpapi.com/manage-api-key</a>.</li>
      <li>Paste it here and press Save:</li></ol>
    <div class="row"><input id="key-in" type="password" placeholder="paste your SerpApi key"><button class="primary" id="key-save">Save key</button></div>
    <div class="muted" style="font-size:12.5px">Your key stays on this PC and is only sent to SerpApi. Until then, the buttons below open a business-class search on Google Flights or Kayak for each route.</div></div>`;
  $("#key-save").onclick = async () => {
    const v = $("#key-in").value.trim();
    if (!v) return;
    ST = await api(`/api/settings?api_key=${enc(v)}`);
    const a = await refreshCredits();
    if (a && a.error) { toast("SerpApi says: " + a.error, 7000); return; }
    toast("Key saved. Press “Check all prices” to get today's prices.");
    render();
  };
}

/* ---------------- best right now */
function priced() {
  const out = [];
  for (const [k, v] of Object.entries(ST.routes)) {
    const r = v.result;
    if (!r || !r.lowest || r.stale_dates) continue;
    const [o, d] = k.split("|");
    out.push({ o, d, r, k });
  }
  return out;
}
function interesting(P) {
  const rows = [];
  for (const x of P) {
    const op = x.r.options[0] || {}, ins = x.r.insights || {}, bp = bestPoints(x.k);
    const sameRegion = P.filter((y) => ST.dests[y.d].region === ST.dests[x.d].region).map((y) => y.r.lowest).sort((a, b) => a - b);
    const median = sameRegion[Math.floor(sameRegion.length / 2)];
    let score = 0;
    const why = [];
    const lvl = levelFor(x.r);
    const from = (b) => b.via.map((c) => ST.points.cards[c].split(" ")[0]).join("/");
    if (lvl === "low") { score += 3; why.push(`<b>good price</b>${ins.typical ? ` (business on this route usually ${usd(ins.typical[0])}&ndash;${usd(ins.typical[1])})` : ""}`); }
    else if (lvl === "typical") score += 1;
    if (bp && bp.cpp >= 5) { score += 2; why.push(`<b>standout points value</b>: ~${kfmt(bp.milesRT)} points round trip with ${esc(bp.p.name)}, from ${esc(from(bp))} (${bp.cpp.toFixed(1)}&cent; per point)`); }
    else if (bp && bp.cpp >= 4) { score += 1; why.push(`strong points value: ~${kfmt(bp.milesRT)} with ${esc(bp.p.name)}, from ${esc(from(bp))} (${bp.cpp.toFixed(1)}&cent;)`); }
    if (op.stops === 0 && op.lie_flat) { score += 1; why.push("nonstop with lie-flat seats"); }
    if (median && x.r.lowest < median * 0.9) { score += 1; why.push(`${Math.round(100 - (100 * x.r.lowest) / median)}% below the typical ${ST.dests[x.d].region} fare on this board`); }
    if (score >= 2) rows.push({ ...x, score, why, op, bp });
  }
  rows.sort((a, b) => b.score - a.score || a.r.lowest - b.r.lowest);
  const perProg = {}, picked = [];
  for (const x of rows) { // keep the list varied: at most two trips per points program
    const id = x.bp && x.bp.cpp >= 4 ? x.bp.p.id : "-";
    if (id !== "-" && (perProg[id] = (perProg[id] || 0) + 1) > 2) continue;
    picked.push(x);
    if (picked.length === 6) break;
  }
  return picked;
}
function renderBest() {
  const box = $("#best");
  const P = priced();
  if (!P.length) { box.innerHTML = ""; return; }
  const I = interesting(P);
  const intHTML = `<h2>Most interesting trips <small>leaving ${niceDate(ST.settings.depart)}; good prices, strong points value, nonstop lie-flat</small></h2>
    <div class="card"><ul class="ilist">${I.length ? I.map((x) => `<li data-k="${x.k}"><span class="bp">${usd(x.r.lowest)}</span><span><b>${esc(ST.origins[x.o].name)} &rarr; ${esc(ST.dests[x.d].city)}</b> <span class="muted">on ${esc((x.op.airlines || []).join(" + "))}, ${stopsTxt(x.op.stops)}</span>
      <div class="why">${x.why.join(" &middot; ")}</div></span></li>`).join("") : `<li class="muted">Nothing stands out yet. Check more routes or different dates.</li>`}</ul></div>`;
  const col = (region) => {
    const rows = P.filter((x) => ST.dests[x.d].region === region).sort((a, b) => a.r.lowest - b.r.lowest).slice(0, 4);
    if (!rows.length) return `<div class="card"><h2 style="margin-top:0">${region}</h2><div class="muted">No prices checked yet.</div></div>`;
    return `<div class="card"><h2 style="margin-top:0">Cheapest to ${region}</h2><ul>${rows.map((x) => {
      const op = x.r.options[0] || {}, ins = x.r.insights || {}, lv = LVL[levelFor(x.r)];
      const typ = ins.typical && sameAsCheapest(x.r) ? `usually ${usd(ins.typical[0])}&ndash;${usd(ins.typical[1])}` : x.r.cheapest_any < x.r.lowest ? `recliner seats from ${usd(x.r.cheapest_any)}` : "";
      return `<li data-k="${x.k}"><span class="bp">${usd(x.r.lowest)}</span><span><b>${esc(ST.origins[x.o].name)} &rarr; ${esc(ST.dests[x.d].city)}</b>${lv ? `<span class="lvl ${lv[0]}">${lv[1]}</span>` : ""}
        <div class="why">${esc((op.airlines || []).join(" + "))} &middot; ${stopsTxt(op.stops)}${op.lie_flat ? " &middot; lie-flat seat" : ""}${typ ? " &middot; " + typ : ""}</div></span></li>`;
    }).join("")}</ul></div>`;
  };
  box.innerHTML = `${intHTML}<h2>Cheapest right now <small>click one for flight details and points options</small></h2><div class="best">${col("Europe")}${col("Asia")}</div>`;
  $$("#best li[data-k]").forEach((li) => (li.onclick = () => openRoute(li.dataset.k)));
}

/* ---------------- price board */
function cell(o, d) {
  const k = `${o}|${d}`, v = ST.routes[k], r = v.result;
  const hasKey = ST.settings.has_key;
  const searchLinks = `<a href="${esc(v.links.google)}" target="_blank" rel="noopener">Google Flights</a> &middot; <a href="${esc(v.links.kayak)}" target="_blank" rel="noopener">Kayak</a>`;
  const checkBtn = hasKey ? `<button class="small chk" data-k="${k}">Check price</button>` : "";
  if (r && r.lowest) {
    const op = r.options[0] || {}, lv = LVL[levelFor(r)], bp = bestPoints(k);
    return `<div class="cellp" data-k="${k}"><span class="price ${r.stale_dates ? "old" : ""}">${usd(r.lowest)}</span>${!r.stale_dates && lv ? `<span class="lvl ${lv[0]}">${lv[1]}</span>` : ""}
      <div class="meta">${esc((op.airlines || []).slice(0, 2).join(" + "))} &middot; ${stopsTxt(op.stops)}${op.lie_flat ? ` <span class="flat">&#9679; lie-flat</span>` : ""}</div>
      ${bp ? `<div class="meta">or ~${kfmt(bp.milesRT)} pts <span class="cpp ${cppRate(bp.cpp)[0]}">${bp.cpp.toFixed(1)}&cent;/pt</span></div>` : ""}
      <div class="meta faint">${r.stale_dates ? `for other dates (${niceDate(r.depart)}) &middot; ` : ""}checked ${ago(r.checked)}</div></div>`;
  }
  if (r && r.error) return `<div class="nores">${esc(r.error)}</div><div class="meta">${searchLinks}</div>`;
  return `<div class="meta">${checkBtn} ${hasKey ? "" : "Search: "}${searchLinks}</div>`;
}
function renderBoard() {
  const s = ST.settings;
  const origins = Object.keys(ST.origins).filter((o) => s.origins.includes(o));
  const section = (region) => {
    const ds = Object.values(ST.dests).filter((d) => d.region === region && (showAll || s.tracked.includes(d.code)));
    if (!ds.length) return "";
    return `<h2>${region}</h2><table class="board"><thead><tr><th style="width:18%">To</th>${origins.map((o) => `<th>From ${esc(ST.origins[o].name)}</th>`).join("")}</tr></thead>
      <tbody>${ds.map((d) => `<tr><td class="city"><b>${esc(d.city)}</b><small>${esc(d.country)} &middot; ${esc(d.airports.replace(/,/g, "/"))}</small></td>${origins.map((o) => `<td>${cell(o, d.code)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  };
  const nTracked = s.tracked.length * origins.length;
  $("#board").innerHTML = `<h2>Price board <small>${showAll ? "all destinations" : `${plural(s.tracked.length, "destination")} you track, ${plural(nTracked, "search")} per full check`}</small>
      <button class="small" id="toggle-all">${showAll ? "Show only tracked" : "Show all destinations"}</button></h2>
    ${section("Europe")}${section("Asia")}`;
  $("#toggle-all").onclick = () => { showAll = !showAll; renderBoard(); };
  $$(".cellp[data-k]").forEach((c) => (c.onclick = () => openRoute(c.dataset.k)));
  $$("button.chk").forEach((b) => (b.onclick = () => checkOne(b.dataset.k, b)));
}

async function checkOne(k, btn) {
  const [o, d] = k.split("|");
  if (btn) { btn.disabled = true; btn.textContent = "Checking..."; }
  try {
    await api(`/api/check?o=${o}&d=${d}`);
    await load();
    refreshCredits();
  } catch (e) {
    toast(e.message, 7000);
    if (btn) { btn.disabled = false; btn.textContent = "Check price"; }
  }
}

/* ---------------- route details */
function histSVG(hist, typical, current) {
  const pts = (hist || []).filter((p) => Array.isArray(p) && typeof p[1] === "number");
  if (pts.length < 2) return "";
  const W = 700, H = 160, pad = 34;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]).concat(typical || [], current ? [current] : []);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys) * 0.95, y1 = Math.max(...ys) * 1.05;
  const X = (x) => pad + ((x - x0) / (x1 - x0 || 1)) * (W - pad - 8), Y = (y) => H - 20 - ((y - y0) / (y1 - y0 || 1)) * (H - 30);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
  const band = typical ? `<rect x="${pad}" width="${W - pad - 8}" y="${Y(typical[1])}" height="${Math.max(1, Y(typical[0]) - Y(typical[1]))}" fill="#334155" opacity=".45"/>` : "";
  const lab = (t) => new Date(t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  return `<svg class="hist" viewBox="0 0 ${W} ${H}">${band}
    <text x="2" y="${Y(y1) + 10}" fill="#94a3b8" font-size="11">${usd(y1)}</text><text x="2" y="${Y(y0)}" fill="#94a3b8" font-size="11">${usd(y0)}</text>
    <path d="${line}" fill="none" stroke="#f59e0b" stroke-width="2"/>
    <text x="${pad}" y="${H - 4}" fill="#94a3b8" font-size="11">${lab(x0)}</text><text x="${W - 60}" y="${H - 4}" fill="#94a3b8" font-size="11">${lab(x1)}</text></svg>`;
}
function openRoute(k) {
  const [o, d] = k.split("|");
  const v = ST.routes[k], r = v.result, O = ST.origins[o], D = ST.dests[d];
  const ins = (r && r.insights) || {};
  const lv = LVL[levelFor(r)];
  let rangeHTML = "";
  if (ins.typical && r.lowest) {
    const [a, b] = ins.typical, lo = Math.min(a, r.lowest) * 0.85, hi = Math.max(b, r.lowest) * 1.1, pos = (x) => (100 * (x - lo)) / (hi - lo);
    rangeHTML = `<div class="range"><i style="left:${pos(r.lowest)}%"></i><span style="left:${pos(r.lowest)}%">${usd(r.lowest)} lie-flat</span></div>
      <div class="muted" style="font-size:12.5px">Google says business class on this route usually costs <b>${usd(a)}&ndash;${usd(b)}</b> for these dates${sameAsCheapest(r) ? "" : ", a range that includes cheaper recliner-seat fares"}.${lv ? ` This lie-flat price is <span class="lvl ${lv[0]}">${lv[1]}</span>${sameAsCheapest(r) ? " (Google's rating)" : " against that range"}.` : ""}</div>`;
  }
  const opts = r && r.options ? r.options : [];
  const myHist = (v.history || []).map((h) => [h[0], h[1]]);
  const CARDS = ST.points.cards;
  let ptsHTML = "";
  if (opts.length) {
    const op = opts[0], progs = programsFor(op.airlines, ST.zones[d], op.price);
    ptsHTML = `<h2>Pay with points instead? <small>for the cheapest option: ${esc(op.airlines.join(" + "))}, ${usd(op.price)}</small></h2>
      ${progs.length ? `<table class="pts"><thead><tr><th>Program</th><th>Points, round trip</th><th>Taxes &amp; fees</th><th>Value per point</th><th>Your points that transfer</th></tr></thead><tbody>
      ${progs.map((x) => { const rt = cppRate(x.cpp); return `<tr class="${x.reachable ? "" : "dim"}"><td><a href="${esc(x.p.url)}" target="_blank" rel="noopener"><b>${esc(x.p.name)}</b></a><div class="faint">${esc(x.p.note)}</div></td>
        <td>${kfmt(x.milesRT)}${x.milesRThi > x.milesRT ? `&ndash;${kfmt(x.milesRThi)}` : ""}</td><td>~${usd(x.taxesRT)}</td>
        <td><span class="cpp ${rt[0]}">${x.cpp.toFixed(1)}&cent;</span><div class="faint">${rt[1]}</div></td>
        <td>${x.via.length ? x.via.map((c) => esc(CARDS[c].replace(/ (Membership Rewards|Ultimate Rewards|Miles|ThankYou Points|Rewards)$/, ""))).join(", ") : `<span class="faint">none of yours</span>`}</td></tr>`; }).join("")}</tbody></table>`
      : `<div class="muted">No mileage program we track books ${esc(op.airlines.join(" + "))} on one ticket. Check the other options below, or the airline's own program.</div>`}
      <div class="note-warn">Points prices are <b>estimates from published award charts</b> (saver level); the seat must have award space on your date. <b>Find the award seat first, then transfer points</b>: transfers can't be undone. "Value per point" = (cash price &minus; award taxes) &divide; points; above 2&cent; is good, above 3&cent; is excellent.</div>`;
  }
  $("#drawer aside").innerHTML = `<button class="x" id="dx">&times;</button>
    <h2 style="margin-top:0;font-size:20px">${esc(O.name)} &rarr; ${esc(D.city)}</h2>
    <div class="muted">Business class &middot; round trip ${niceDate(r ? r.depart : ST.settings.depart)} &rarr; ${niceDate(r ? r.return : ST.settings.return)} &middot; 1 adult${r ? ` &middot; checked ${ago(r.checked)}` : ""}</div>
    <div class="row"><a href="${esc((r && r.links && r.links.google) || v.links.google)}" target="_blank" rel="noopener"><button class="primary">Open in Google Flights &#8599;</button></a>
      <a href="${esc(v.links.kayak)}" target="_blank" rel="noopener"><button>Compare on Kayak &#8599;</button></a>
      ${ST.settings.has_key ? `<button id="recheck">Check again (1 search)</button>` : ""}</div>
    ${r && r.stale_dates ? `<div class="card" style="border-color:#f59e0b">These prices were checked for ${niceDate(r.depart)}, not your current dates. Press "Check again" to update.</div>` : ""}
    ${rangeHTML}
    ${ptsHTML}
    <h2>Where to stay in ${esc(D.city)} <small>${niceDate(ST.settings.depart)} &rarr; ${niceDate(ST.settings.return)}, central ${esc(D.city)}, guest rating 4.0+</small></h2>
    <div id="hotels-box"><div class="muted">Loading...</div></div>
    ${ins.history && ins.history.length > 1 ? `<h2>Price over the last two months <small>Google Flights; shaded = typical range</small></h2>${histSVG(ins.history, ins.typical, r.lowest)}` : ""}
    ${myHist.length > 1 ? `<h2>Prices this app has seen <small>each check</small></h2>${histSVG(myHist, null, null)}` : ""}
    <h2>Cheapest options <small>price is the round trip; times shown are the outbound flight</small></h2>
    ${r && r.cheapest_any && r.lowest && r.cheapest_any < r.lowest ? `<div class="muted" style="font-size:12.5px">The headline price is the cheapest <b>lie-flat</b> option with a sensible routing. Cheaper "business" fares from ${usd(r.cheapest_any)} have recliner seats or long detours; they're listed below.</div>` : ""}
    ${opts.length ? opts.map((x) => `<div class="opt">${x.logo ? `<img src="${esc(x.logo)}" alt="">` : "<span></span>"}
      <div><b>${esc(x.airlines.join(" + "))}</b>${x.lie_flat ? ` <span class="flat">&#9679; lie-flat seat</span>` : ` <span class="faint">recliner seat</span>`}${x.sensible === false ? ` <span class="faint">&middot; long routing</span>` : ""}
        <div class="meta"><b>${esc((x.dep_time || "").slice(11))}</b> ${esc(x.from)} &rarr; <b>${esc((x.arr_time || "").slice(11))}</b> ${esc(x.to)}${x.overnight ? " (+1)" : ""} &middot; ${esc(x.duration)} &middot; ${stopsTxt(x.stops)}${x.via.length ? " via " + esc(x.via.join(", ")) : ""}</div>
        <div class="meta faint">${esc(x.flight_numbers.join(", "))}${x.aircraft.length ? " &middot; " + esc(x.aircraft.join(", ")) : ""}</div></div>
      <div class="op">${usd(x.price)}</div></div>`).join("") : `<div class="muted">${r && r.error ? esc(r.error) : "Not checked yet."}</div>`}`;
  $("#drawer").classList.remove("hidden");
  $("#dx").onclick = closeDrawer;
  $("#drawer .shade").onclick = closeDrawer;
  if ($("#recheck")) $("#recheck").onclick = async () => { $("#recheck").disabled = true; $("#recheck").textContent = "Checking..."; await checkOne(k); openRoute(k); };
  loadHotels(k);
}
const closeDrawer = () => $("#drawer").classList.add("hidden");

/* ---------------- hotels */
const HCLS = [["45", "4 & 5 star"], ["5", "5 star only"], ["all", "Any"]];
let hotelSort = "value";
function hotelPoints(h) {
  const P = (ST.points.hotel_programs || []).find((p) => p.id === h.program);
  if (!P || !h.night) return null;
  const be = Math.round(h.night / (P.cpp / 100) / 500) * 500; // break-even points per night
  const mine = ST.settings.cards || [];
  const via = Object.entries(P.cards).filter(([c]) => mine.includes(c)).map(([c, ratio]) => ({ card: ST.points.cards[c].split(" ")[0], pts: Math.round(be / ratio / 500) * 500, ratio }));
  let verdict = "";
  if (P.fixed) verdict = `Points are worth a fixed amount here, so using them is the same as paying cash.`;
  else if (P.id === "hyatt") verdict = be > 45000 ? `<b class="g">Almost certainly a great points deal</b>: no Hyatt costs more than 45k a night on the standard chart.` : be > 25000 ? `<b class="g">Likely good value</b> if it's a category 1-6 hotel (up to 25-29k a night).` : `Cash is fine here; check the hotel's category before using points.`;
  return { P, be, via, verdict };
}
function hotelRow(h, nights) {
  const hp = hotelPoints(h);
  const stars = h.stars ? "&#9733;".repeat(h.stars) : "";
  return `<div class="hotel">${h.photo ? `<img src="${esc(h.photo)}" alt="" loading="lazy">` : "<span></span>"}
    <div><b>${h.link ? `<a href="${esc(h.link)}" target="_blank" rel="noopener">${esc(h.name)}</a>` : esc(h.name)}</b> <span class="stars">${stars}</span>
      ${h.deal_label ? ` <span class="lvl low" title="${esc(h.deal)}">${esc(h.deal_label)}${h.deal ? `: ${esc(h.deal.replace(" than usual", ""))}` : ""}</span>` : ""}
      <div class="meta">${h.rating ? `<b>${h.rating.toFixed(1)}</b>/5 from ${(h.reviews || 0).toLocaleString("en-US")} reviews` : ""}${h.location ? ` &middot; location ${h.location.toFixed(1)}` : ""}${h.near.length ? ` &middot; ${esc(h.near.slice(0, 2).join(" · "))}` : ""}</div>
      ${hp ? `<div class="meta"><span class="prog">${esc(hp.P.name)}</span> Points beat cash if it's under <b>${kfmt(hp.be)} points a night</b>${hp.via.length ? ` (${hp.via.map((v) => `${kfmt(v.pts)} ${esc(v.card)}${v.ratio !== 1 ? ` at ${v.ratio < 1 ? Math.round((1 / v.ratio) * 10) / 10 + ":1" : "1:" + v.ratio}` : ""}`).join(", ")})` : ` <span class="faint">(none of your cards transfer here)</span>`}. ${hp.verdict}</div>` : ""}</div>
    <div class="op">${usd(h.night)}<small>a night</small><small class="faint">${usd(h.total)} for ${plural(nights, "night")}</small></div></div>`;
}
function sortHotels(hs) {
  const v = (h) => (h.rating || 3.5) * 100 - h.night / 6 + (h.deal_label === "Great Deal" ? 25 : h.deal_label ? 12 : 0);
  const by = { value: (a, b) => v(b) - v(a), price: (a, b) => a.night - b.night, rating: (a, b) => (b.rating || 0) - (a.rating || 0) || a.night - b.night };
  return hs.slice().sort(by[hotelSort]);
}
async function loadHotels(k, search) {
  const [, d] = k.split("|");
  const box = $("#hotels-box");
  if (!box) return;
  const cls = ST.settings.hotel_cls || "45";
  if (search) box.innerHTML = `<div class="muted">Searching Google Hotels...</div>`;
  let h;
  try { h = await api(`/api/hotels?d=${d}&cls=${cls}${search ? "&search=1" : ""}`); } catch (e) { box.innerHTML = `<div class="nores">${esc(e.message)}</div>`; return; }
  if (!$("#hotels-box")) return;
  const clsSeg = `<span class="seg">${HCLS.map(([v, l]) => `<button class="small ${v === cls ? "on" : ""}" data-cls="${v}">${l}</button>`).join("")}</span>`;
  if (!h.cached) {
    box.innerHTML = `<div class="row">${clsSeg}${ST.settings.has_key ? `<button class="primary" id="h-go">Find hotels for these dates (1 search)</button>` : ""}
      <a href="${esc(h.google)}" target="_blank" rel="noopener"><button>Open Google Hotels &#8599;</button></a></div>
      <div class="muted" style="font-size:12.5px">Shows prices for ${plural(h.nights, "night")}, which hotels let you pay with points, and when points beat cash.</div>`;
  } else {
    const r = ST.routes[k].result, list = sortHotels(h.hotels);
    const good = h.hotels.filter((x) => (x.rating || 0) >= 4.3).sort((a, b) => a.total - b.total)[0];
    const trip = r && r.lowest && good ? `<div class="card tripsum">Whole trip: flight <b>${usd(r.lowest)}</b> + ${plural(h.nights, "night")} at ${esc(good.name)} (${good.rating.toFixed(1)}&#9733;) <b>${usd(good.total)}</b> = <b class="big">${usd(r.lowest + good.total)}</b> <span class="faint">cheapest hotel here rated 4.3+${r.stale_dates ? `; flight price is from ${niceDate(r.depart)}` : ""}</span></div>` : "";
    box.innerHTML = `${trip}<div class="row">${clsSeg}<span class="seg">${[["value", "Best value"], ["price", "Cheapest"], ["rating", "Best rated"]].map(([v, l]) => `<button class="small ${v === hotelSort ? "on" : ""}" data-sort="${v}">${l}</button>`).join("")}</span>
        <span class="faint">${plural(h.hotels.length, "hotel")} &middot; checked ${ago(h.checked)}</span>
        ${ST.settings.has_key ? `<button class="small" id="h-go">Check again (1 search)</button>` : ""}<a href="${esc(h.google)}" target="_blank" rel="noopener"><button class="small">Google Hotels &#8599;</button></a></div>
      ${list.length ? list.map((x) => hotelRow(x, h.nights)).join("") : `<div class="muted">No hotels found.</div>`}
      <div class="note-warn">Prices are per room per night including taxes, from Google Hotels. "Points beat cash" uses typical point values (Hyatt 1.7&cent;, Marriott 0.8&cent;, IHG 0.6&cent;, Hilton 0.5&cent;): if the hotel's own site asks for fewer points than that, use points. Check the award price on the hotel's site <b>before</b> transferring; transfers can't be undone.</div>`;
  }
  $$("#hotels-box [data-cls]").forEach((b) => (b.onclick = async () => {
    ST = await api(`/api/settings?hotel_cls=${b.dataset.cls}`);
    loadHotels(k);
  }));
  $$("#hotels-box [data-sort]").forEach((b) => (b.onclick = () => { hotelSort = b.dataset.sort; loadHotels(k); }));
  if ($("#h-go")) $("#h-go").onclick = async () => { await loadHotels(k, true); refreshCredits(); };
}

/* ---------------- check all */
async function checkAll() {
  const s = ST.settings;
  const n = s.tracked.length * s.origins.length;
  if (!confirm(`Check prices for ${plural(n, "route")}? This uses ${plural(n, "search")} from your SerpApi allowance.`)) return;
  try { ST.job = await api("/api/check_all"); } catch (e) { toast(e.message, 7000); return; }
  pollJob();
}
async function pollJob() {
  let lastDone = -1;
  $("#check-all").disabled = true;
  const p = $("#progress");
  p.classList.remove("hidden");
  for (;;) {
    let j;
    try { j = await api("/api/job"); } catch (e) { break; }
    $("#progress i").style.width = (j.total ? (100 * j.done) / j.total : 0) + "%";
    $("#progress span").textContent = j.running ? `Checking ${j.current}... (${j.done} of ${j.total})` : "";
    if (j.done !== lastDone) { lastDone = j.done; await load(); }
    if (!j.running) {
      p.classList.add("hidden");
      if (j.stopped) toast("Stopped: " + j.stopped, 9000);
      else toast(`Done: checked ${plural(j.done, "route")}${j.errors.length ? `, ${j.errors.length} with no results` : ""}.`);
      await load();
      refreshCredits();
      break;
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  $("#check-all").disabled = !ST.settings.has_key;
}

/* ---------------- settings */
function openSettings() {
  const s = ST.settings;
  const regions = ["Europe", "Asia"];
  $("#settings .dlg").innerHTML = `<button class="x" id="sx">&times;</button><h2 style="margin-top:0">Settings</h2>
    <label class="f">SerpApi key ${s.has_key ? `<span class="muted">(saved: ${esc(s.key_hint)})</span>` : ""}</label>
    <input id="s-key" type="password" style="width:100%" placeholder="${s.has_key ? "leave blank to keep the saved key" : "paste your SerpApi key"}">
    <label class="f">When do you want to leave? (always at least ${s.notice_days} business days from today: ${niceDate(s.earliest)} or later)</label>
    <div class="checks" style="grid-template-columns:repeat(3,auto)">
      <label><input type="radio" name="s-when" value="soonest" ${s.when === "soonest" ? "checked" : ""}> As soon as possible</label>
      <label><input type="radio" name="s-when" value="later" ${s.when === "later" ? "checked" : ""}> About 6 weeks out</label>
      <label><input type="radio" name="s-when" value="date" ${s.when === "date" ? "checked" : ""}> On a date: <input id="s-dep" type="date" min="${s.earliest}" value="${esc(s.depart_fixed)}"></label>
    </div>
    <div class="grid2">
      <div><label class="f">Nights away</label><input id="s-n" type="number" min="1" max="60" value="${s.nights}" style="width:90px"></div>
      <div><label class="f">Stops</label><select id="s-stops">${[["0", "Any number of stops"], ["1", "Nonstop only"], ["2", "Up to 1 stop"], ["3", "Up to 2 stops"]].map(([v, l]) => `<option value="${v}" ${String(s.stops) === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div><label class="f">Flying from</label><div class="checks" style="grid-template-columns:repeat(3,auto)">${Object.entries(ST.origins).map(([k, o]) => `<label><input type="checkbox" class="s-o" value="${k}" ${s.origins.includes(k) ? "checked" : ""}>${esc(o.name)}</label>`).join("")}</div></div>
    </div>
    ${regions.map((rg) => `<label class="f">${rg}: destinations to track</label><div class="checks">${Object.values(ST.dests).filter((d) => d.region === rg).map((d) => `<label><input type="checkbox" class="s-d" value="${d.code}" ${s.tracked.includes(d.code) ? "checked" : ""}>${esc(d.city)}</label>`).join("")}</div>`).join("")}
    <label class="f"><input type="checkbox" id="s-flat" ${s.flat_only !== false ? "checked" : ""}> Lie-flat seats only for the headline price (skip recliner "business" such as Icelandair Saga, and long detours)</label>
    <label class="f">Credit-card points you have (used for the cash vs. points comparison)</label>
    <div class="checks">${Object.entries(ST.points.cards).map(([k, n]) => `<label><input type="checkbox" class="s-c" value="${k}" ${(s.cards || []).includes(k) ? "checked" : ""}>${esc(n)}</label>`).join("")}</div>
    <label class="f"><input type="checkbox" id="s-auto" ${s.auto_weekly ? "checked" : ""}> Check all tracked prices automatically once a week (when the app is opened)</label>
    <div class="row" style="justify-content:space-between;margin-top:16px"><span class="muted" id="s-cost"></span><button class="primary" id="s-save">Save</button></div>`;
  const cost = () => { const n = $$(".s-d:checked").length * $$(".s-o:checked").length; $("#s-cost").textContent = `A full check will use ${plural(n, "search")}${$("#s-auto").checked ? `, about ${n * 4} a month with weekly checks` : ""}.`; };
  $$(".s-d, .s-o, #s-auto").forEach((x) => (x.onchange = cost));
  cost();
  $("#settings").classList.remove("hidden");
  const close = () => $("#settings").classList.add("hidden");
  $("#sx").onclick = close;
  $("#settings .shade").onclick = close;
  $("#s-save").onclick = async () => {
    const when = ($("input[name=s-when]:checked") || {}).value || "soonest";
    if (when === "date" && !$("#s-dep").value) { toast("Pick a departure date, or choose “As soon as possible”."); return; }
    if (when === "date" && $("#s-dep").value < s.earliest) { toast(`The earliest departure is ${niceDate(s.earliest)} (${s.notice_days} business days' notice).`); return; }
    const q = new URLSearchParams({
      when, cards: $$(".s-c:checked").map((x) => x.value).join(","), flat_only: $("#s-flat").checked ? "1" : "0",
      depart: $("#s-dep").value, nights: $("#s-n").value, stops: $("#s-stops").value, auto_weekly: $("#s-auto").checked ? "1" : "0",
      tracked: $$(".s-d:checked").map((x) => x.value).join(","), origins: $$(".s-o:checked").map((x) => x.value).join(","),
    });
    const key = $("#s-key").value.trim();
    if (key) q.set("api_key", key);
    try { ST = await api("/api/settings?" + q.toString()); } catch (e) { toast(e.message); return; }
    close();
    render();
    if (key) refreshCredits();
    toast("Saved.");
  };
}

/* ---------------- credits & deals */
async function refreshCredits() {
  let a;
  try { a = await api("/api/account"); } catch (e) { a = { error: e.message }; }
  const el = $("#credits");
  if (!a.key) el.innerHTML = `<span class="muted">No SerpApi key yet</span>`;
  else if (a.error) el.innerHTML = `<span style="color:var(--red)">SerpApi: ${esc(a.error)}</span>`;
  else el.innerHTML = `<b>${a.left ?? "?"}</b> searches left${a.per_month ? ` <span class="faint">of ${a.per_month}/month</span>` : ""}`;
  return a;
}
async function loadDeals() {
  let d;
  try { d = await api("/api/deals"); } catch (e) { return; }
  const items = d.items || [];
  $("#deals").innerHTML = `<h2>Business-class sales &amp; deals in the news <small>from deal sites and travel news, last 60 days</small></h2>
    <div class="card deals">${items.length ? `<ul>${items.map((x) => `<li><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a><small>${esc(x.source)}${x.ts ? " &middot; " + ago(x.ts) : ""}</small></li>`).join("")}</ul>` : `<div class="muted">No business-class sales from the US to Europe or Asia in the news right now.</div>`}</div>`;
}

/* ---------------- wiring */
$("#open-settings").onclick = openSettings;
$("#trip").onclick = openSettings;
$("#check-all").onclick = checkAll;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDrawer(); $("#settings").classList.add("hidden"); } });
setInterval(() => fetch("/api/ping").catch(() => {}), 20000);

(async () => {
  try { await load(); } catch (e) { document.querySelector("main").innerHTML = `<div class="card">Could not start: ${esc(e.message)}</div>`; return; }
  refreshCredits();
  loadDeals();
  if (ST.job.running) pollJob();
  const m = location.hash.match(/^#route=(\w+)\|(\w+)/); // deep link, e.g. #route=DC|TYO
  if (m && ST.routes[`${m[1]}|${m[2]}`]) openRoute(`${m[1]}|${m[2]}`);
})();
