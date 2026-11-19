/* Used Car Deal Finder */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (v) => (typeof v === "number" ? "$" + Math.round(v).toLocaleString("en-US") : "--");
const num = (v) => (typeof v === "number" ? Math.round(v).toLocaleString("en-US") : "--");
const enc = encodeURIComponent;
async function api(path) {
  const r = await fetch(path);
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, ms = 4500) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.add("hidden"), ms);
}
const store = (k, v) => { try { if (v === undefined) return JSON.parse(localStorage.getItem(k)); localStorage.setItem(k, JSON.stringify(v)); } catch (e) { return null; } return v; };

const BODY = ["SUV", "Pickup", "Sedan", "Hatchback", "Coupe", "Convertible", "Wagon", "Minivan"];
const MILES = [["", "Any"], ["20000", "20,000"], ["30000", "30,000"], ["50000", "50,000"], ["75000", "75,000"], ["100000", "100,000"], ["150000", "150,000"]];
const RATE = { great: "Great deal", good: "Good deal", fair: "Fair price", high: "Above market", suspicious: "Unusually low" };
let ST = null, RES = null, sortBy = "deal", onlyDeals = false;
let F = Object.assign({ body: "", make: "", model: "", year_min: "", year_max: "", miles_max: "", price_min: "", price_max: "",
  drivetrain: "", fuel: "", trans: "", one_owner: false, clean: false, zip: "", radius: 100 }, store("ucf.filters") || {});

/* ------------------------------------------------ filters panel */
function renderFilters() {
  const y = ST.this_year, years = [];
  for (let i = y + 1; i >= 1995; i--) years.push(i);
  const sel = (id, opts, cur) => `<select id="${id}">${opts.map(([v, l]) => `<option value="${esc(v)}" ${String(cur) === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  $("#filters").innerHTML = `
    <div class="fld"><label class="l">Type of car (pick any)</label><div class="chips" id="f-body">${BODY.map((b) => `<span class="chip ${F.body.split(",").includes(b) ? "on" : ""}" data-v="${b}">${b}</span>`).join("")}</div></div>
    <div class="fld"><label class="l">Make</label>${sel("f-make", [["", "Any make"], ...ST.makes.map((m) => [m, m])], F.make)}</div>
    <div class="fld"><label class="l">Model</label><input id="f-model" list="models" placeholder="${F.make ? "Any " + esc(F.make) + " model" : "Pick a make first (optional)"}" value="${esc(F.model)}"><datalist id="models"></datalist></div>
    <div class="fld"><label class="l">Years</label><div class="row2">${sel("f-y0", [["", "Oldest"], ...years.map((v) => [v, "from " + v])], F.year_min)}${sel("f-y1", [["", "Newest"], ...years.map((v) => [v, "to " + v])], F.year_max)}</div></div>
    <div class="fld"><label class="l">Maximum miles</label>${sel("f-miles", MILES, F.miles_max)}</div>
    <div class="fld"><label class="l">Price</label><div class="row2"><input id="f-p0" type="number" min="0" step="1000" placeholder="min $" value="${esc(F.price_min)}"><input id="f-p1" type="number" min="0" step="1000" placeholder="max $" value="${esc(F.price_max)}"></div></div>
    <div class="fld"><label class="l">Drive</label>${sel("f-drive", [["", "Any"], ["4WD,AWD", "4WD / AWD"], ["FWD", "Front-wheel drive"], ["RWD", "Rear-wheel drive"]], F.drivetrain)}</div>
    <div class="fld"><label class="l">Engine</label>${sel("f-fuel", [["", "Any"], ["gas", "Gas"], ["hybrid", "Hybrid"], ["phev", "Plug-in hybrid"], ["ev", "Electric"]], F.fuel)}</div>
    <div class="fld"><label class="l">Transmission</label>${sel("f-trans", [["", "Any"], ["Automatic", "Automatic"], ["Manual", "Manual"]], F.trans)}</div>
    <div class="fld checks"><label><input type="checkbox" id="f-1o" ${F.one_owner ? "checked" : ""}> One owner only</label><label><input type="checkbox" id="f-ct" ${F.clean ? "checked" : ""}> Clean title only</label></div>
    <div class="fld"><label class="l">Where</label><div class="checks">
      <label><input type="radio" name="f-where" value="all" ${F.zip ? "" : "checked"}> Nationwide</label>
      <label><input type="radio" name="f-where" value="near" ${F.zip ? "checked" : ""}> Near ZIP</label></div>
      <div class="row2" id="f-near" style="${F.zip ? "" : "display:none"}"><input id="f-zip" placeholder="ZIP" maxlength="5" value="${esc(F.zip || ST.settings.zip)}">${sel("f-rad", [[25, "25 mi"], [50, "50 mi"], [100, "100 mi"], [200, "200 mi"], [500, "500 mi"]], F.radius || 100)}</div></div>
    <button class="primary" id="f-go" style="width:100%">Find cars</button>
    <div class="row2" style="margin-top:8px"><button class="small" id="f-save">&#9733; Save search</button><button class="small" id="f-reset">Reset</button></div>
    <h3 style="margin-top:18px">Saved searches</h3><div id="saved"></div>`;
  $$("#f-body .chip").forEach((c) => (c.onclick = () => c.classList.toggle("on")));
  $("#f-make").onchange = () => loadModels($("#f-make").value);
  $$("input[name=f-where]").forEach((r) => (r.onchange = () => { $("#f-near").style.display = $("input[name=f-where]:checked").value === "near" ? "" : "none"; }));
  $("#f-go").onclick = () => runSearch();
  $("#f-save").onclick = saveSearch;
  $("#f-reset").onclick = () => { F = { body: "", make: "", model: "", year_min: "", year_max: "", miles_max: "", price_min: "", price_max: "", drivetrain: "", fuel: "", trans: "", one_owner: false, clean: false, zip: "", radius: 100 }; store("ucf.filters", F); renderFilters(); };
  $("#f-model").onkeydown = (e) => { if (e.key === "Enter") runSearch(); };
  if (F.make) loadModels(F.make);
  renderSaved();
}
function readFilters() {
  const near = ($("input[name=f-where]:checked") || {}).value === "near";
  F = {
    body: $$("#f-body .chip.on").map((c) => c.dataset.v).join(","), make: $("#f-make").value, model: $("#f-model").value.trim(),
    year_min: $("#f-y0").value, year_max: $("#f-y1").value, miles_max: $("#f-miles").value,
    price_min: $("#f-p0").value, price_max: $("#f-p1").value, drivetrain: $("#f-drive").value, fuel: $("#f-fuel").value,
    trans: $("#f-trans").value, one_owner: $("#f-1o").checked, clean: $("#f-ct").checked,
    zip: near ? $("#f-zip").value.trim() : "", radius: +$("#f-rad").value || 100,
  };
  store("ucf.filters", F);
  return F;
}
async function loadModels(make) {
  const dl = $("#models");
  if (!dl) return;
  dl.innerHTML = "";
  if (!make) return;
  try { const j = await api(`/api/models?make=${enc(make)}`); dl.innerHTML = j.models.map((m) => `<option value="${esc(m)}">`).join(""); } catch (e) { /* suggestions are optional */ }
}
function describe(f) {
  const parts = [];
  if (f.year_min || f.year_max) parts.push(`${f.year_min || "any"}&ndash;${f.year_max || "newest"}`);
  parts.push([f.make, f.model].filter(Boolean).join(" ") || (f.body ? f.body.split(",").join(" / ") : "any car"));
  if (f.make && f.body) parts.push(`(${f.body.split(",").join("/")})`);
  if (f.miles_max) parts.push(`under ${num(+f.miles_max)} miles`);
  if (f.price_max) parts.push(`under ${usd(+f.price_max)}`);
  return parts.join(" ");
}

/* ------------------------------------------------ search */
async function runSearch(savedId) {
  if (!ST.settings.has_key) { toast("Add your free MarketCheck key first (box at the top)."); return; }
  const f = savedId ? null : readFilters();
  if (f && f.zip && !/^\d{5}$/.test(f.zip)) { toast("ZIP code should be 5 digits."); return; }
  $("#results").innerHTML = `<div class="loading">Searching dealer listings${f && f.zip ? " near " + esc(f.zip) : " nationwide"}...</div>`;
  try {
    RES = savedId ? await api(`/api/run_saved?id=${enc(savedId)}`) : await api(`/api/search?f=${enc(JSON.stringify(f))}`);
  } catch (e) { $("#results").innerHTML = `<div class="card" style="border-color:var(--red)">${esc(e.message)}</div>`; return; }
  ST.calls_this_month = RES.calls_this_month;
  renderUsage();
  if (savedId) { ST.saved = (await api("/api/saved")).saved; renderSaved(); }
  renderResults();
}
function sorted(list) {
  const L = list.filter((x) => !onlyDeals || x.rating === "great" || x.rating === "good");
  const by = {
    deal: (a, b) => (b.deal_pct ?? -999) - (a.deal_pct ?? -999),
    price: (a, b) => (a.price ?? 1e9) - (b.price ?? 1e9),
    miles: (a, b) => (a.miles ?? 1e9) - (b.miles ?? 1e9),
    year: (a, b) => (b.build.year || 0) - (a.build.year || 0) || (a.miles ?? 0) - (b.miles ?? 0),
    lot: (a, b) => (b.dom_active ?? b.dom ?? 0) - (a.dom_active ?? a.dom ?? 0),
    near: (a, b) => (a.dist ?? 1e9) - (b.dist ?? 1e9),
  }[sortBy];
  return L.sort(by);
}
function scatter(list) {
  const pts = list.filter((x) => typeof x.price === "number" && typeof x.miles === "number");
  if (pts.length < 3) return "";
  const W = 1000, H = 260, L = 70, B = 34;
  const mx = Math.max(...pts.map((p) => p.miles)) * 1.05 || 1, p0 = Math.min(...pts.map((p) => p.price)) * 0.92, p1 = Math.max(...pts.map((p) => p.price)) * 1.05;
  const X = (m) => L + (m / mx) * (W - L - 16), Y = (p) => H - B - ((p - p0) / (p1 - p0 || 1)) * (H - B - 14);
  const col = { great: "#3fb950", good: "#7ee787", fair: "#8b949e", high: "#f85149", suspicious: "#d29922" };
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => `<text x="${X(mx * t)}" y="${H - 12}" fill="#6b7785" font-size="12" text-anchor="middle">${num(mx * t)} mi</text>`).join("");
  const yt = [0, 0.5, 1].map((t) => { const v = p0 + (p1 - p0) * t; return `<text x="${L - 8}" y="${Y(v) + 4}" fill="#6b7785" font-size="12" text-anchor="end">${usd(v)}</text>`; }).join("");
  return `<svg class="scatter" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${ticks}${yt}
    ${pts.map((p) => `<circle cx="${X(p.miles).toFixed(1)}" cy="${Y(p.price).toFixed(1)}" r="5" fill="${col[p.rating] || "#58a6ff"}" opacity=".85"><title>${esc(p.heading)}: ${usd(p.price)}, ${num(p.miles)} mi${p.rating ? " - " + RATE[p.rating] : ""}</title></circle>`).join("")}</svg>
    <div class="faint" style="font-size:12px;margin:4px 2px 0">Each dot is a car: further left = fewer miles, lower = cheaper. <span style="color:#3fb950">Green</span> = priced below similar cars, <span style="color:#f85149">red</span> = above.</div>`;
}
function dealLine(x) {
  if (x.deal_pct == null) return `<span class="faint">Not enough similar cars to judge the price</span>`;
  if (x.rating === "suspicious") return `<span class="deal pos">${usd(x.deal_usd)} below similar cars (${Math.round(x.deal_pct)}%). <b>Check the history</b>: prices this low can mean accident damage or a branded title.</span>`;
  return x.deal_usd >= 0 ? `<span class="deal pos">${usd(x.deal_usd)} below similar cars (${x.deal_pct.toFixed(0)}%)</span>` : `<span class="deal neg">${usd(-x.deal_usd)} above similar cars</span>`;
}
function carCard(x, i) {
  const b = x.build, d = x.dealer;
  const lot = x.dom_active ?? x.dom;
  const tags = [];
  if (x.one_owner) tags.push(`<span class="tag g">1 owner</span>`);
  if (x.clean) tags.push(`<span class="tag g">clean title</span>`);
  if (x.certified) tags.push(`<span class="tag g">certified pre-owned</span>`);
  if (lot >= 60) tags.push(`<span class="tag a" title="Cars that sit longer are often easier to negotiate on">${lot} days on lot: room to negotiate</span>`);
  if (typeof x.ref_price === "number" && x.ref_price > x.price) tags.push(`<span class="tag a" title="Was ${usd(x.ref_price)}">price cut ${usd(x.ref_price - x.price)}</span>`);
  if (b.drivetrain) tags.push(`<span class="tag">${esc(b.drivetrain)}</span>`);
  if (b.city_mpg) tags.push(`<span class="tag">${b.city_mpg}/${b.highway_mpg || "?"} mpg</span>`);
  return `<div class="car">
    <div class="ph ${x.photo ? "" : "nophoto"}" style="${x.photo ? `background-image:url('${esc(x.photo)}')` : ""}"><span class="rt rate ${x.rating || "none"}">${x.rating ? RATE[x.rating] : "No comparison"}</span>${x.new ? `<span class="new badge-new">NEW</span>` : ""}</div>
    <div class="bd"><h4>${esc(x.heading || `${b.year} ${b.make} ${b.model} ${b.trim || ""}`)}</h4>
      <div class="pr"><b>${usd(x.price)}</b><span>${num(x.miles)} miles</span></div>
      <div>${dealLine(x)}</div>
      <div class="meta">${[d.name, [d.city, d.state].filter(Boolean).join(", ")].filter(Boolean).map(esc).join(" &middot; ")}${typeof x.dist === "number" ? ` &middot; ${Math.round(x.dist)} mi away` : ""}${x.color ? ` &middot; ${esc(x.color)}` : ""}</div>
      <div class="tags">${tags.join("")}</div>
      <div class="act">${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener"><button class="small primary">View listing &#8599;</button></a>` : ""}<button class="small" data-r="${i}">Recalls &amp; complaints</button></div></div></div>`;
}
function renderResults() {
  const r = RES, ps = r.stats.price || {}, ms = r.stats.miles || {};
  const list = sorted(r.listings.slice());
  const deals = r.listings.filter((x) => x.rating === "great" || x.rating === "good").length;
  const mix = (r.facets.model || []).slice(0, 14);
  $("#results").innerHTML = `
    <div class="summary">${r.saved_name ? `<b>${esc(r.saved_name)}</b>: ` : ""}<b>${num(r.num_found)}</b> matching cars ${esc(r.where)}${r.new_count ? `, <span class="badge-new">${r.new_count} new since last time</span>` : ""}.
      ${ps.median ? `Typical price <b>${usd(ps.median)}</b>` : ""}${ms.median ? `, typical mileage <b>${num(ms.median)}</b>` : ""}.
      Showing ${r.listings.length} of them${r.num_found > r.listings.length ? " (the most recently listed)" : ""}; <b>${deals}</b> look like good deals.</div>
    ${scatter(r.listings)}
    ${mix.length > 1 ? `<div class="mix">${mix.map((m) => `<span class="chip" data-model="${esc(m.item)}">${esc(m.item)}<i>${num(m.count)}</i></span>`).join("")}</div>` : ""}
    <div class="toolbar"><span class="muted">Sort:</span><span class="seg" id="sort">${[["deal", "Best deals"], ["price", "Lowest price"], ["miles", "Fewest miles"], ["year", "Newest"], ["lot", "Longest on lot"], ...(r.filters && r.filters.zip ? [["near", "Closest"]] : [])].map(([v, l]) => `<button data-v="${v}" class="${sortBy === v ? "on" : ""}">${l}</button>`).join("")}</span>
      <label class="muted" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="only-deals" ${onlyDeals ? "checked" : ""}> Good deals only</label></div>
    ${list.length ? `<div class="grid">${list.map((x) => carCard(x, r.listings.indexOf(x))).join("")}</div>` : `<div class="card muted">No cars match. Try more miles, a wider price range, or nationwide.</div>`}
    <p class="foot">Deal ratings compare each car with similar cars of the same make and model in these results, adjusted for model year and mileage. They don't know about options, condition or accident history, so always check a vehicle history report and get an inspection. Listings from dealer websites via MarketCheck; safety data from NHTSA.</p>`;
  $$("#sort button").forEach((b) => (b.onclick = () => { sortBy = b.dataset.v; renderResults(); }));
  $("#only-deals").onchange = (e) => { onlyDeals = e.target.checked; renderResults(); };
  $$("button[data-r]").forEach((b) => (b.onclick = () => openResearch(r.listings[+b.dataset.r])));
  $$(".mix .chip").forEach((c) => (c.onclick = () => { $("#f-model").value = c.dataset.model; runSearch(); }));
}

/* ------------------------------------------------ research (NHTSA) */
async function openResearch(x) {
  const b = x.build;
  const box = $("#modal .dlg");
  box.innerHTML = `<button class="x" id="mx">&times;</button><h2>${b.year} ${esc(b.make)} ${esc(b.model)}: safety record</h2><div class="loading">Checking NHTSA...</div>`;
  $("#modal").classList.remove("hidden");
  const close = () => $("#modal").classList.add("hidden");
  $("#mx").onclick = close; $("#modal .shade").onclick = close;
  let d;
  try { d = await api(`/api/research?make=${enc(b.make)}&model=${enc(b.model)}&year=${b.year}`); } catch (e) { box.querySelector(".loading").textContent = e.message; return; }
  const c = d.complaints || {};
  box.innerHTML = `<button class="x" id="mx">&times;</button><h2>${b.year} ${esc(b.make)} ${esc(b.model)}: safety record</h2>
    <p class="muted">From the US government's National Highway Traffic Safety Administration (NHTSA), for every ${b.year} ${esc(b.model)}, not just this car.
    ${x.vin ? `To see if <b>this car</b> still has an open recall, check its VIN at <a href="https://www.nhtsa.gov/recalls?vin=${esc(x.vin)}" target="_blank" rel="noopener">nhtsa.gov/recalls</a>.` : ""}</p>
    <h3>${(d.recalls || []).length} recall${(d.recalls || []).length === 1 ? "" : "s"}</h3>
    ${(d.recalls || []).length ? `<ul>${d.recalls.map((r) => `<li><b>${esc(r.component)}</b> <span class="faint">(${esc(r.date || "")})</span><div class="muted" style="font-size:12.5px">${esc(r.summary)}</div></li>`).join("")}</ul>` : `<p class="muted">No recalls on record.</p>`}
    <h3>${num(c.total || 0)} owner complaints</h3>
    ${c.total ? `<p class="muted">${c.crashes || 0} involved a crash, ${c.fires || 0} a fire, ${c.injuries || 0} injuries reported. Most common problem areas:</p>
      <ul>${(c.top || []).map(([k, v]) => `<li>${esc(k)}: ${v}</li>`).join("")}</ul>` : `<p class="muted">No complaints on record.</p>`}
    <p class="faint" style="font-size:12px">Complaint counts depend on how many were sold; compare years of the same model rather than different models.</p>`;
  $("#mx").onclick = close;
}

/* ------------------------------------------------ saved searches */
function renderSaved() {
  const el = $("#saved");
  if (!el) return;
  const S = ST.saved || [];
  el.innerHTML = S.length ? S.map((s) => `<div class="saved-item"><span class="nm" data-id="${s.id}" title="Run this search">${esc(s.name)}</span>${s.last_new ? `<span class="badge-new">${s.last_new} new</span>` : ""}<button class="small" data-del="${s.id}" title="Delete">&times;</button></div>`).join("")
    : `<div class="faint" style="font-size:12.5px">Save a search to re-run it later and see which cars are new.</div>`;
  $$("#saved .nm").forEach((n) => (n.onclick = () => runSearch(n.dataset.id)));
  $$("#saved button[data-del]").forEach((b) => (b.onclick = async () => { ST.saved = (await api(`/api/delete?id=${enc(b.dataset.del)}`)).saved; renderSaved(); }));
}
async function saveSearch() {
  const f = readFilters();
  const name = prompt("Name this search:", describe(f).replace(/&ndash;/g, "-"));
  if (name === null) return;
  ST.saved = (await api(`/api/save?name=${enc(name)}&f=${enc(JSON.stringify(f))}`)).saved;
  renderSaved();
  toast("Saved. Click it any time to re-run and see what's new.");
}

/* ------------------------------------------------ setup & settings */
function renderSetup() {
  const box = $("#setup");
  if (ST.settings.has_key) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.innerHTML = `<div class="card"><h2 style="margin-top:0">One-time setup: connect to car listings (free, about 2 minutes)</h2>
    <ol><li>Create a free developer account at <a href="https://developers.marketcheck.com/sign-up" target="_blank" rel="noopener">developers.marketcheck.com</a>.</li>
      <li>Copy your <b>API key</b> from your MarketCheck dashboard.</li>
      <li>Paste it here and press Save:</li></ol>
    <div style="display:flex;gap:10px;max-width:560px"><input id="key-in" type="password" placeholder="paste your MarketCheck API key"><button class="primary" id="key-save">Save key</button></div>
    <div class="muted" style="font-size:12.5px;margin-top:8px">The key stays on this PC and is only sent to MarketCheck. Each search uses about ${ST.settings.pages} requests from your free allowance.</div></div>`;
  $("#key-save").onclick = async () => {
    const v = $("#key-in").value.trim();
    if (!v) return;
    ST = await api(`/api/settings?api_key=${enc(v)}`);
    try { const t = await api("/api/test_key"); toast(`Connected: ${num(t.num_found)} used cars listed right now.`); } catch (e) { toast(e.message, 8000); }
    renderSetup(); renderUsage();
  };
}
function renderUsage() { $("#usage").innerHTML = ST.settings.has_key ? `<b>${num(ST.calls_this_month)}</b> MarketCheck requests this month` : `<span class="muted">Not connected yet</span>`; }
function openSettings() {
  const s = ST.settings, box = $("#modal .dlg");
  box.innerHTML = `<button class="x" id="mx">&times;</button><h2>Settings</h2>
    <label class="muted">MarketCheck API key ${s.has_key ? `(saved: ${esc(s.key_hint)})` : ""}</label><input id="s-key" type="password" placeholder="${s.has_key ? "leave blank to keep it" : "paste your key"}" style="margin:4px 0 14px">
    <label class="muted">Home ZIP (pre-filled when you pick "Near ZIP")</label><input id="s-zip" maxlength="5" value="${esc(s.zip)}" style="margin:4px 0 14px;width:120px">
    <label class="muted">Cars to compare per search (more = better deal ratings, more requests)</label>
    <select id="s-pages" style="margin:4px 0 14px;width:auto">${[[1, "50 cars (1 request)"], [2, "100 cars (2 requests)"], [3, "150 cars (3 requests)"], [4, "200 cars (4 requests)"]].map(([v, l]) => `<option value="${v}" ${+s.pages === v ? "selected" : ""}>${l}</option>`).join("")}</select>
    <div style="text-align:right"><button class="primary" id="s-save">Save</button></div>`;
  $("#modal").classList.remove("hidden");
  const close = () => $("#modal").classList.add("hidden");
  $("#mx").onclick = close; $("#modal .shade").onclick = close;
  $("#s-save").onclick = async () => {
    const q = new URLSearchParams({ zip: $("#s-zip").value.trim(), pages: $("#s-pages").value });
    if ($("#s-key").value.trim()) q.set("api_key", $("#s-key").value.trim());
    try { ST = await api("/api/settings?" + q); } catch (e) { toast(e.message); return; }
    close(); renderSetup(); renderUsage(); toast("Saved.");
  };
}

$("#open-settings").onclick = openSettings;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").classList.add("hidden"); });
setInterval(() => fetch("/api/ping").catch(() => {}), 20000);
(async () => {
  try { ST = await api("/api/state"); } catch (e) { $("#results").innerHTML = `<div class="card">Could not start: ${esc(e.message)}</div>`; return; }
  const m = location.hash.match(/^#f=(.+)$/); // shareable search link: #f=<url-encoded JSON filters>
  if (m) { try { F = Object.assign(F, JSON.parse(decodeURIComponent(m[1]))); store("ucf.filters", F); } catch (e) { /* ignore bad links */ } }
  renderFilters(); renderSetup(); renderUsage();
  if (m && ST.settings.has_key) runSearch();
})();
