"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v == null ? "" : "$" + Math.round(v).toLocaleString());
const CATS = [["all", "All"], ["sports", "Sports"], ["concerts", "Concerts"], ["theater", "Theater & shows"], ["comedy", "Comedy"], ["family", "Family"], ["other", "Other"]];
const WHEN = [["tonight", "Tonight"], ["tomorrow", "Tomorrow"], ["weekend", "This weekend"], ["3", "Next 3 days"], ["all", "All"]];
const UI = loadUI();
let ST = null, EV = null, lastAlerts = "";

function loadUI() {
  const d = { when: "all", cat: "all", q: "", sort: "deal", priced: false };
  try { return Object.assign(d, JSON.parse(localStorage.getItem("lmt-ui") || "{}"), { q: "" }); } catch { return d; }
}
function saveUI() { try { localStorage.setItem("lmt-ui", JSON.stringify(UI)); } catch { /* ignore */ } }

async function api(path, params = {}) {
  const u = new URL(path, location.href);
  for (const [k, v] of Object.entries(params)) u.searchParams.set(k, v);
  const r = await fetch(u);
  const j = await r.json().catch(() => ({ error: "Bad response" }));
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, ms = 3000) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add("hidden"), ms);
}
function modal(html) {
  const m = $("#modal"); $(".dlg", m).innerHTML = `<button class="x" onclick="closeModal()">&times;</button>` + html;
  m.classList.remove("hidden"); $(".shade", m).onclick = closeModal;
}
function closeModal() { $("#modal").classList.add("hidden"); }

// ------------------------------------------------------------ dates
const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
function dayLabel(local) {
  if (!local) return "Date TBA";
  const today = new Date(), tom = new Date(Date.now() + 864e5);
  const d = local.slice(0, 10);
  if (d === ymd(today)) return "Tonight";
  if (d === ymd(tom)) return "Tomorrow";
  const dt = new Date(local.slice(0, 10) + "T12:00:00");
  return dt.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}
function timeLabel(e) {
  if (e.tbd || !e.local) return "Time TBA";
  const [h, m] = e.local.slice(11, 16).split(":").map(Number);
  return `${((h + 11) % 12) + 1}:${pad(m)} ${h < 12 ? "am" : "pm"}`;
}
function untilLabel(h) {
  if (h == null) return "";
  if (h < 0) return "started";
  if (h < 1) return `in ${Math.max(1, Math.round(h * 60))} min`;
  if (h < 24) return `in ${Math.round(h)} h`;
  return "";
}
function ago(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function inWhen(e) {
  const d = (e.local || "").slice(0, 10);
  const today = new Date();
  switch (UI.when) {
    case "tonight": return d === ymd(today);
    case "tomorrow": return d === ymd(new Date(Date.now() + 864e5));
    case "3": return e.hours != null && e.hours <= 72;
    case "weekend": {
      const days = [];
      for (let i = 0; i < 7; i++) {
        const x = new Date(Date.now() + i * 864e5), w = x.getDay();
        if (w === 5 || w === 6 || w === 0) days.push(ymd(x));
        if (w === 0 && days.length) break;
      }
      return days.includes(d);
    }
    default: return true;
  }
}

// ------------------------------------------------------------ state
async function loadState() {
  ST = await api("/api/state");
  const s = ST.settings;
  $("#sub").textContent = `${s.where} · within ${s.radius} miles · next ${s.days} day${s.days > 1 ? "s" : ""}`;
  renderSetup();
}
async function loadEvents(quiet) {
  try {
    EV = await api("/api/events");
  } catch (e) {
    if (!quiet) toast(e.message);
    return;
  }
  render();
}
function renderStatus() {
  const s = ST?.settings || {};
  const u = EV?.usage || ST?.usage || {};
  const bits = [];
  if (EV?.checking) bits.push(`<span class="spin"></span> checking...`);
  else if (EV?.at) bits.push(`Checked <b>${ago(EV.at)}</b>`);
  if (s.auto && (s.has_sg || s.has_tm)) bits.push(`auto every ${s.auto} min`);
  if (s.has_sg) bits.push(`SeatGeek ${u.sg || 0} today`);
  if (s.has_tm) bits.push(`Ticketmaster ${u.tm || 0}/5,000 today`);
  $("#status").innerHTML = bits.join(" · ");
}

function renderSetup() {
  const s = ST.settings, el = $("#setup");
  if (s.has_sg && s.has_tm) { el.classList.add("hidden"); return; }
  const need = !s.has_sg && !s.has_tm;
  el.classList.remove("hidden");
  el.innerHTML = `<div class="card">
    <b>${need ? "One-time setup: add at least one free key" : "Optional: add the other key for more"}</b>
    <p class="muted" style="margin:6px 0 10px">Both are free and take a couple of minutes. SeatGeek gives the live resale prices (the cheapest seat right now)
    used for price drops; Ticketmaster adds face value, on-sale status and events SeatGeek misses.</p>
    <div class="two">
      <div><h3>SeatGeek ${s.has_sg ? "&#10003;" : ""}</h3><ol>
        <li>Sign in or create a free account at <a href="https://seatgeek.com/account/develop" target="_blank">seatgeek.com/account/develop</a></li>
        <li>Create an app (any name, e.g. "Last-Minute Tickets")</li>
        <li>Copy the <b>Client ID</b> into Settings</li></ol></div>
      <div><h3>Ticketmaster ${s.has_tm ? "&#10003;" : ""}</h3><ol>
        <li>Register at <a href="https://developer-acct.ticketmaster.com/user/register" target="_blank">developer.ticketmaster.com</a></li>
        <li>Open <b>My Apps</b> (your default app is created for you)</li>
        <li>Copy the <b>Consumer Key</b> into Settings</li></ol></div>
    </div>
    <button class="primary" onclick="openSettings()">Open Settings</button>
    <span class="muted" style="margin-left:10px">Gametime has no public API, so every event gets a one-click Gametime button instead.</span>
  </div>`;
}

// ------------------------------------------------------------ rendering
function spark(h) {
  if (!h || h.length < 2) return "";
  const xs = h.map((p) => p[0]), ys = h.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const W = 90, H = 22, X = (x) => ((x - x0) / (x1 - x0 || 1)) * (W - 4) + 2, Y = (y) => H - 3 - ((y - y0) / (y1 - y0 || 1)) * (H - 6);
  let d = "", prev = null;
  for (const [x, y] of h) { d += prev == null ? `M${X(x)},${Y(y)}` : `H${X(x)}V${Y(y)}`; prev = y; }
  d += `H${W - 2}`;
  const col = ys[ys.length - 1] < ys[0] ? "#3fb950" : ys[ys.length - 1] > ys[0] ? "#f85149" : "#6b7785";
  return `<svg class="spark" viewBox="0 0 ${W} ${H}"><title>Cheapest seat since first seen: ${money(ys[0])} to ${money(ys[ys.length - 1])}</title><path d="${d}" fill="none" stroke="${col}" stroke-width="1.6"/></svg>`;
}
function filtered() {
  const q = UI.q.trim().toLowerCase();
  let list = (EV?.events || []).filter((e) => inWhen(e) && (UI.cat === "all" || e.cat === UI.cat));
  if (UI.priced) list = list.filter((e) => e.sg?.low != null || e.tm?.lo != null);
  if (q) list = list.filter((e) => [e.title, e.venue, ...(e.performers || [])].join(" ").toLowerCase().includes(q));
  const low = (e) => e.sg?.low ?? e.tm?.lo ?? 1e9;
  const t = (e) => e.utc || "9999";
  const sorters = {
    deal: (a, b) => b.deal_score - a.deal_score || t(a).localeCompare(t(b)),
    soon: (a, b) => t(a).localeCompare(t(b)),
    cheap: (a, b) => low(a) - low(b),
    drop: (a, b) => (b.drop_pct || 0) - (a.drop_pct || 0) || t(a).localeCompare(t(b)),
  };
  return list.sort(sorters[UI.sort] || sorters.soon);
}
function toolbar() {
  const counts = {};
  for (const e of EV?.events || []) if (inWhen(e)) counts[e.cat] = (counts[e.cat] || 0) + 1;
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  $("#toolbar").innerHTML = `
    <div class="seg">${WHEN.map(([k, l]) => `<button class="${UI.when === k ? "on" : ""}" data-when="${k}">${l}</button>`).join("")}</div>
    <div class="chips">${CATS.filter(([k]) => k === "all" || counts[k]).map(([k, l]) => `<span class="chip ${UI.cat === k ? "on" : ""}" data-cat="${k}">${l} <i class="faint">${k === "all" ? total : counts[k]}</i></span>`).join("")}</div>
    <div class="spacer"></div>
    <label class="ck"><input type="checkbox" id="priced" ${UI.priced ? "checked" : ""}> Only with prices</label>
    <select id="sort">${[["deal", "Best deals first"], ["soon", "Soonest first"], ["cheap", "Cheapest first"], ["drop", "Biggest price drop"]].map(([k, l]) => `<option value="${k}" ${UI.sort === k ? "selected" : ""}>${l}</option>`).join("")}</select>
    <input type="search" id="q" placeholder="Search team, artist, venue" value="${esc(UI.q)}">`;
  $("#toolbar").querySelectorAll("[data-when]").forEach((b) => (b.onclick = () => { UI.when = b.dataset.when; saveUI(); render(); }));
  $("#toolbar").querySelectorAll("[data-cat]").forEach((b) => (b.onclick = () => { UI.cat = b.dataset.cat; saveUI(); render(); }));
  $("#priced").onchange = (ev) => { UI.priced = ev.target.checked; saveUI(); render(); };
  $("#sort").onchange = (ev) => { UI.sort = ev.target.value; saveUI(); render(); };
  $("#q").oninput = (ev) => { UI.q = ev.target.value; renderList(); renderDeals(); };
}
const TMS = { onsale: "on sale, no price listed", offsale: "not on sale (try resale)", cancelled: "cancelled", canceled: "cancelled", postponed: "postponed", rescheduled: "rescheduled" };
function priceBlock(e) {
  const sg = e.sg || {}, tm = e.tm || {};
  let h = "";
  if (sg.low != null) h += `<b>${money(sg.low)}</b> <span>SeatGeek${sg.n ? ` · ${sg.n} listing${sg.n > 1 ? "s" : ""}` : ""}</span>${spark(e.hist)}`;
  else if (e.sg) h += `<span>SeatGeek: no listings now</span>`;
  if (tm.lo != null) h += `<span class="face">Face value ${money(tm.lo)}${tm.hi && tm.hi !== tm.lo ? "–" + money(tm.hi) : ""} (Ticketmaster)</span>`;
  else if (e.tm) h += `<span class="face">Ticketmaster: ${esc(TMS[tm.status] || tm.status || "listed")}</span>`;
  return h;
}
function actions(e) {
  return `<a class="btn gt" href="${esc(e.gametime)}" target="_blank" title="Opens Gametime's search for this event">Gametime</a>
    ${e.sg?.url ? `<a class="btn sg" href="${esc(e.sg.url)}" target="_blank">SeatGeek</a>` : ""}
    ${e.tm?.url ? `<a class="btn tm" href="${esc(e.tm.url)}" target="_blank">Ticketmaster</a>` : ""}
    <button class="star ${e.watch ? "on" : ""}" title="${e.watch ? "Watching" + (e.watch.target ? " for " + money(e.watch.target) : "") : "Watch the price"}" onclick="watchDlg('${esc(e.key)}')">${e.watch ? "&#9733;" : "&#9734;"}</button>`;
}
function row(e) {
  const tags = (e.flags || []).map(([c, t]) => `<span class="tag ${c}">${esc(t)}</span>`).join("");
  const catName = (CATS.find(([k]) => k === e.cat) || [, "Other"])[1];
  const soon = untilLabel(e.hours);
  return `<div class="ev ${e.deal_score >= 10 ? "hot" : ""} ${e.alert ? "alert" : ""}" id="ev-${esc(e.key)}">
    <div class="when"><b>${esc(timeLabel(e))}</b><span class="${soon ? "soon" : ""}">${esc(soon || dayLabel(e.local))}</span></div>
    <div class="th" style="${e.image ? `background-image:url('${esc(e.image)}')` : ""}"></div>
    <div class="nm"><b>${esc(e.title)}</b><div class="v">${esc(e.venue || "")}${e.city ? ", " + esc(e.city) : ""}</div>
      <div class="tags"><span class="tag c">${esc(catName)}</span>${e.watch ? `<span class="tag a">Watching${e.watch.target ? " for " + money(e.watch.target) : ""}</span>` : ""}${tags}</div></div>
    <div class="pr">${priceBlock(e)}</div>
    <div class="act">${actions(e)}</div></div>`;
}
function renderDeals() {
  const el = $("#deals");
  const list = filtered().filter((e) => e.deal_score > 0 && e.sg?.low != null && !e.flags.some((f) => f[0] === "r")).sort((a, b) => b.deal_score - a.deal_score).slice(0, 8);
  if (!list.length || UI.q) { el.innerHTML = ""; return; }
  el.innerHTML = `<h2 class="sec">Best deals right now</h2><div class="deals">${list.map((e) => `
    <div class="dc" onclick="jump('${esc(e.key)}')">
      ${e.image ? `<div class="ph" style="background-image:url('${esc(e.image)}')"></div>` : ""}
      <div class="bd"><div class="t">${esc(e.title)}</div>
      <div class="w">${esc(dayLabel(e.local))} ${esc(timeLabel(e))} · ${esc(e.venue || "")}</div>
      <div><span class="p">${money(e.sg.low)}</span> <span class="muted">cheapest seat</span></div>
      <div class="tags" style="display:flex;flex-wrap:wrap;gap:4px">${(e.flags || []).filter((f) => f[0] === "g").map(([c, t]) => `<span class="tag ${c}">${esc(t)}</span>`).join("")}</div></div></div>`).join("")}</div>`;
}
function renderList() {
  const el = $("#list");
  if (!EV?.at) {
    const s = ST?.settings || {};
    el.innerHTML = `<div class="empty-state">${s.has_sg || s.has_tm ? (EV?.checking ? '<span class="spin"></span> Finding events...' : "Press <b>Check now</b> to find events.") : "Add a key in Settings to start."}</div>`;
    return;
  }
  const list = filtered();
  let h = (EV.errors || []).map((x) => `<div class="err">${esc(x)}</div>`).join("");
  h += `<h2 class="sec">${list.length} event${list.length === 1 ? "" : "s"}</h2>`;
  if (!list.length) h += `<div class="empty-state">Nothing matches. Try another day or type.</div>`;
  else if (UI.sort === "soon") {
    let cur = null;
    for (const e of list) {
      const d = dayLabel(e.local);
      if (d !== cur) { h += `<div class="day">${esc(d)}</div>`; cur = d; }
      h += row(e);
    }
  } else h += list.map(row).join("");
  el.innerHTML = h;
}
function renderAlerts() {
  const el = $("#alerts");
  const hits = (EV?.events || []).filter((e) => e.alert);
  if (!hits.length) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.innerHTML = `<div class="card">&#128276; <b>Price alert:</b> ${hits.map((e) => `<a href="#" onclick="jump('${esc(e.key)}');return false">${esc(e.title)}</a> is now <b>${money(e.sg.low)}</b>${e.watch.target ? ` (your target ${money(e.watch.target)})` : ` (was ${money(e.watch.low_at_add)} when you started watching)`}`).join("; ")}</div>`;
  const sig = hits.map((e) => e.key + e.sg.low).join();
  if (sig !== lastAlerts) {
    lastAlerts = sig;
    document.title = "(" + hits.length + ") Last-Minute Tickets";
    try { if (Notification.permission === "granted") new Notification("Ticket price alert", { body: hits.map((e) => `${e.title}: ${money(e.sg.low)}`).join("\n") }); } catch { /* ignore */ }
  }
}
function render() {
  renderStatus(); toolbar(); renderAlerts(); renderDeals(); renderList();
}
function jump(key) {
  UI.q = ""; UI.cat = "all"; UI.when = "all"; UI.priced = false; render();
  const el = document.getElementById("ev-" + key);
  if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1700); }
}

// ------------------------------------------------------------ actions
async function checkNow() {
  const b = $("#check");
  b.disabled = true; b.innerHTML = '<span class="spin"></span> Checking';
  if (EV) { EV.checking = true; renderStatus(); renderList(); }
  try { EV = await api("/api/check"); render(); toast(`Found ${EV.events.length} events`); }
  catch (e) { toast(e.message, 6000); await loadEvents(true); }
  finally { b.disabled = false; b.textContent = "Check now"; }
}
function watchDlg(key) {
  const e = (EV?.events || []).find((x) => x.key === key);
  if (!e) return;
  modal(`<h2>Watch the price</h2><p><b>${esc(e.title)}</b><br><span class="muted">${esc(dayLabel(e.local))} ${esc(timeLabel(e))} · ${esc(e.venue || "")}</span></p>
    <p>Cheapest seat now: <b>${e.sg?.low != null ? money(e.sg.low) : "not available (SeatGeek has no listings for this one)"}</b></p>
    <div class="fld"><label class="l">Alert me when the cheapest seat is at or below</label><input id="w-t" type="number" min="1" step="1" placeholder="Any drop" value="${e.watch?.target ?? ""}"></div>
    <p class="muted" style="font-size:13px">Leave blank to be alerted on any drop. Alerts show at the top of this window (and as a Windows notification if you allow it) while the app is open; it re-checks every ${ST.settings.auto || "-"} minutes.</p>
    <button class="primary" id="w-s">${e.watch ? "Update" : "Watch"}</button> ${e.watch ? '<button id="w-x">Stop watching</button>' : ""}`);
  $("#w-s").onclick = async () => {
    try { EV = await api("/api/watch", { key, target: $("#w-t").value }); closeModal(); render(); toast("Watching"); } catch (er) { toast(er.message); }
    try { if (Notification.permission === "default") Notification.requestPermission(); } catch { /* ignore */ }
  };
  if (e.watch) $("#w-x").onclick = async () => { EV = await api("/api/watch", { key, on: "0" }); closeModal(); render(); };
}
function openSettings() {
  const s = ST.settings;
  modal(`<h2>Settings</h2>
    <div class="keyrow">
      <div class="fld"><label class="l">SeatGeek Client ID ${s.has_sg ? "(saved: " + esc(s.sg_hint) + ")" : ""}</label><input id="s-sg" placeholder="${s.has_sg ? "leave blank to keep" : "paste Client ID"}"><small><a href="https://seatgeek.com/account/develop" target="_blank">Get one free</a> · Client Secret not needed</small></div>
      <div class="fld"><label class="l">Ticketmaster Consumer Key ${s.has_tm ? "(saved: " + esc(s.tm_hint) + ")" : ""}</label><input id="s-tm" placeholder="${s.has_tm ? "leave blank to keep" : "paste Consumer Key"}"><small><a href="https://developer-acct.ticketmaster.com/user/register" target="_blank">Get one free</a> · 5,000 calls a day</small></div>
    </div>
    <div class="row3">
      <div class="fld"><label class="l">City</label><select id="s-pl">${ST.places.map(([k, n]) => `<option value="${k}" ${s.place === k ? "selected" : ""}>${esc(n)}</option>`).join("")}<option value="zip" ${s.place === "zip" ? "selected" : ""}>ZIP code...</option></select></div>
      <div class="fld"><label class="l">ZIP code (optional)</label><input id="s-zip" maxlength="5" value="${esc(s.zip)}" placeholder="e.g. 20037"></div>
      <div class="fld"><label class="l">Within</label><select id="s-r">${[10, 15, 25, 40, 60, 100].map((r) => `<option value="${r}" ${+s.radius === r ? "selected" : ""}>${r} miles</option>`).join("")}</select></div>
    </div>
    <div class="row3">
      <div class="fld"><label class="l">Look ahead</label><select id="s-d">${[1, 2, 3, 7, 14].map((d) => `<option value="${d}" ${+s.days === d ? "selected" : ""}>${d === 1 ? "Today + tonight" : d + " days"}</option>`).join("")}</select></div>
      <div class="fld"><label class="l">Re-check automatically</label><select id="s-a">${[[0, "Off"], [15, "Every 15 min"], [30, "Every 30 min"], [60, "Every hour"]].map(([v, l]) => `<option value="${v}" ${+s.auto === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
    </div>
    <p class="muted" style="font-size:13px">Keys are stored on this PC only and sent only to SeatGeek / Ticketmaster. Auto checks run while this window is open.</p>
    <button class="primary" id="s-save">Save</button> <button id="s-test">Test keys</button> <span id="s-msg" class="muted"></span>`);
  $("#s-save").onclick = async () => {
    const p = { place: $("#s-pl").value, radius: $("#s-r").value, days: $("#s-d").value, auto: $("#s-a").value };
    if ($("#s-sg").value.trim()) p.sg_id = $("#s-sg").value.trim();
    if ($("#s-tm").value.trim()) p.tm_key = $("#s-tm").value.trim();
    const z = $("#s-zip").value.trim();
    if (p.place === "zip" || (z && z !== s.zip)) { if (!z) { $("#s-msg").textContent = "Enter a ZIP code."; return; } p.zip = z; delete p.place; }
    const moved = p.zip || p.place !== s.place || +p.radius !== +s.radius || +p.days !== +s.days;
    const newKey = p.sg_id || p.tm_key;
    try {
      await api("/api/settings", p); await loadState(); closeModal(); toast("Saved");
      if (moved || newKey) { await loadEvents(true); if (ST.settings.has_sg || ST.settings.has_tm) checkNow(); }
      else renderStatus();
    } catch (e) { $("#s-msg").textContent = e.message; }
  };
  $("#s-test").onclick = async () => {
    $("#s-msg").innerHTML = '<span class="spin"></span>';
    try {
      const r = await api("/api/test_keys");
      $("#s-msg").textContent = [r.sg && "SeatGeek: " + r.sg, r.tm && "Ticketmaster: " + r.tm].filter(Boolean).join(" · ") || "No keys saved yet.";
    } catch (e) { $("#s-msg").textContent = e.message; }
  };
}

$("#check").onclick = checkNow;
$("#open-settings").onclick = openSettings;
(async () => {
  await loadState();
  await loadEvents();
  if (!EV?.at && (ST.settings.has_sg || ST.settings.has_tm)) checkNow();
  setInterval(() => loadEvents(true), 60000);
})();
