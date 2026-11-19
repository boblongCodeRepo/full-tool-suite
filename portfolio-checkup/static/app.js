/* Portfolio Checkup - page logic. All processing happens here, in the visitor's browser. */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TABLES = []; // {id, file, sheet, rows, t (analysed), kind}
let ECON = null, OVERRIDES = {}, LAST = null;
const KIND_LABEL = { holdings: "Holdings", transactions: "Trades & activity", bank: "Bank / card transactions", unknown: "Not recognised", ignore: "Ignore" };
const FIELD_LABEL = { holdings: ["symbol", "description", "quantity", "price", "value", "cost", "costper", "gain", "type", "account"],
  transactions: ["date", "action", "symbol", "description", "quantity", "price", "amount", "fees"], bank: ["date", "payee", "amount", "debit", "credit", "category", "balance"] };

/* ---------------- files */
async function addFiles(files) {
  for (const f of files) {
    try {
      const sheets = await readFile(f);
      for (const s of sheets) {
        if (!s.rows.length) continue;
        const t = analyseTable(s.rows);
        TABLES.push({ id: Math.random().toString(36).slice(2), file: f.name, sheet: sheets.length > 1 ? s.name : "", rows: s.rows, t, kind: t.kind === "unknown" ? "ignore" : t.kind });
      }
    } catch (e) { alert(`Couldn't read ${f.name}: ${e.message}`); }
  }
  renderFiles();
}
function renderFiles() {
  $("#files").innerHTML = TABLES.map((x) => {
    const n = x.t.data.length;
    const fields = FIELD_LABEL[x.kind] || [];
    const opt = (k) => `<select data-id="${x.id}" data-field="${k}"><option value="">(none)</option>${x.t.header.map((h, i) => `<option value="${i}" ${x.t.map[k] === i ? "selected" : ""}>${esc(h || "column " + (i + 1))}</option>`).join("")}</select>`;
    return `<div class="file"><div class="hd"><b title="${esc(x.file)}">${esc(x.file)}${x.sheet ? " &middot; " + esc(x.sheet) : ""}</b>
        <span class="kind ${x.kind}">${KIND_LABEL[x.kind]}</span>
        <select data-kind="${x.id}">${["holdings", "transactions", "bank", "ignore"].map((k) => `<option value="${k}" ${x.kind === k ? "selected" : ""}>${KIND_LABEL[k]}</option>`).join("")}</select>
        <button class="x" data-del="${x.id}" title="Remove">&times;</button></div>
      <div class="muted small">${n} rows${x.t.asof ? ` &middot; as of ${esc(x.t.asof)}` : ""}${x.t.kind === "unknown" ? " &middot; couldn't find a header row; pick the type and columns below" : ""}</div>
      ${fields.length ? `<details><summary>Check which columns were used</summary><div class="maptbl">${fields.map((k) => `<label>${k}${opt(k)}</label>`).join("")}</div>
        <div class="preview"><table><tr>${x.t.header.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>${x.t.data.slice(0, 4).map((r) => `<tr>${x.t.header.map((_, i) => `<td>${esc(r[i] instanceof Date ? r[i].toLocaleDateString() : r[i])}</td>`).join("")}</tr>`).join("")}</table></div></details>` : ""}</div>`;
  }).join("");
  $$("select[data-kind]").forEach((s) => (s.onchange = () => { const x = TABLES.find((t) => t.id === s.dataset.kind); x.kind = s.value; renderFiles(); }));
  $$("select[data-field]").forEach((s) => (s.onchange = () => { const x = TABLES.find((t) => t.id === s.dataset.id); if (s.value === "") delete x.t.map[s.dataset.field]; else x.t.map[s.dataset.field] = +s.value; }));
  $$("button[data-del]").forEach((b) => (b.onclick = () => { TABLES.splice(TABLES.findIndex((t) => t.id === b.dataset.del), 1); renderFiles(); }));
  const usable = TABLES.filter((t) => t.kind !== "ignore");
  $("#go").disabled = !usable.length;
  $("#go-note").textContent = usable.length ? `${usable.length} file${usable.length > 1 ? "s" : ""} ready` : "";
}

/* ---------------- profile */
function profile() {
  return { age: +$("#p-age").value || null, horizon: +$("#p-hor").value || null, monthlySpend: +$("#p-spend").value || null,
    comfort: +(($("#p-comfort .on") || {}).dataset || {}).v || 3 };
}
function loadProfile() {
  try {
    const p = JSON.parse(localStorage.getItem("pc.profile") || "null");
    if (!p) return;
    if (p.age) $("#p-age").value = p.age;
    if (p.horizon) $("#p-hor").value = p.horizon;
    if (p.monthlySpend) $("#p-spend").value = p.monthlySpend;
    $$("#p-comfort button").forEach((b) => b.classList.toggle("on", +b.dataset.v === p.comfort));
    $("#p-remember").checked = true;
  } catch (e) { /* ignore */ }
}

/* ---------------- run */
async function run() {
  const P = profile();
  try { if ($("#p-remember").checked) localStorage.setItem("pc.profile", JSON.stringify(P)); else localStorage.removeItem("pc.profile"); } catch (e) { /* ignore */ }
  $("#go").disabled = true; $("#go-note").textContent = "Analysing...";
  if (!ECON) { try { const r = await fetch("api/econ"); if (r.ok) ECON = await r.json(); } catch (e) { ECON = null; } }
  const holdings = [], transactions = [], bank = [];
  for (const x of TABLES) {
    const src = x.file + (x.sheet ? " / " + x.sheet : "");
    if (x.kind === "holdings") holdings.push(...toHoldings(x.t, src));
    if (x.kind === "transactions") transactions.push(...toTransactions(x.t, src));
    if (x.kind === "bank") bank.push(...toBank(x.t, src));
  }
  if (!holdings.length && !transactions.length && !bank.length) { alert("No usable rows were found. Open “Check which columns were used” to fix the column choices."); $("#go").disabled = false; return; }
  LAST = { holdings, transactions, bank, profile: P, econ: ECON };
  report();
  $("#go").disabled = false; $("#go-note").textContent = "";
}
function report() {
  const R = analyze({ ...LAST, overrides: OVERRIDES });
  $("#report").innerHTML = renderReport(R);
  $("#report").classList.remove("hidden");
  $$("#report select[data-ov]").forEach((s) => (s.onchange = () => { OVERRIDES[s.dataset.ov] = s.value; report(); }));
  $("#print").onclick = () => window.print();
  $("#again").onclick = () => { $("#start").scrollIntoView({ behavior: "smooth" }); };
  $("#report").scrollIntoView({ behavior: "smooth" });
}

/* ---------------- report rendering */
const m0 = (v) => money(v, 0);
function donut(alloc) {
  let a0 = -Math.PI / 2;
  const R = 90, r = 56, C = 100;
  const arcs = alloc.filter((a) => a.pct > 0.2).map((a) => {
    const ang = (Math.max(a.pct, 0) / 100) * Math.PI * 2, a1 = a0 + ang, large = ang > Math.PI ? 1 : 0;
    const p = (rad, ang2) => `${C + rad * Math.cos(ang2)},${C + rad * Math.sin(ang2)}`;
    const d = ang >= Math.PI * 2 - 0.001 ? `M${C},${C - R}A${R},${R} 0 1 1 ${C - 0.01},${C - R}L${C - 0.01},${C - r}A${r},${r} 0 1 0 ${C},${C - r}Z`
      : `M${p(R, a0)}A${R},${R} 0 ${large} 1 ${p(R, a1)}L${p(r, a1)}A${r},${r} 0 ${large} 0 ${p(r, a0)}Z`;
    a0 = a1;
    return `<path d="${d}" fill="${CLASSES[a.cls].color}"><title>${CLASSES[a.cls].label}: ${a.pct.toFixed(1)}%</title></path>`;
  }).join("");
  return `<svg viewBox="0 0 200 200">${arcs}</svg>`;
}
function econCards(E) {
  if (!E || !E.series) return `<p class="muted">Economic data couldn't be loaded right now.</p>`;
  const S = E.series, v = (id) => (S[id] ? S[id].value : null);
  const card = (label, val, text, cls = "") => `<div class="ec"><span>${label}</span><b class="${cls}">${val}</b><p>${text}</p></div>`;
  const out = [];
  if (v("DTB3") != null) out.push(card("Cash yield (3-month T-bill)", pctf(v("DTB3"), 2), `What safe cash earns now. ${S.CPIAUCSL && S.CPIAUCSL.yoy != null ? (v("DTB3") > S.CPIAUCSL.yoy ? "Above inflation, so cash is gaining slightly in real terms." : "Below inflation, so cash is losing buying power.") : ""}`));
  if (S.CPIAUCSL && S.CPIAUCSL.yoy != null) out.push(card("Inflation (CPI, past year)", pctf(S.CPIAUCSL.yoy, 1), S.CPIAUCSL.yoy >= 3 ? "Above the Fed's 2% goal: erodes cash and regular bonds." : "Near the Fed's 2% goal.", S.CPIAUCSL.yoy >= 3 ? "neg" : ""));
  if (v("DGS10") != null) out.push(card("10-year Treasury yield", pctf(v("DGS10"), 2), `Sets the tone for mortgages and bond returns.${S.DGS10.year_ago != null ? ` ${v("DGS10") > S.DGS10.year_ago ? "Up" : "Down"} from ${pctf(S.DGS10.year_ago, 2)} a year ago.` : ""}`));
  if (v("T10Y3M") != null) out.push(card("Yield curve (10y minus 3m)", (v("T10Y3M") > 0 ? "+" : "") + v("T10Y3M").toFixed(2) + " pts", v("T10Y3M") < 0 ? "Inverted: historically an early recession warning (with a long, variable lag)." : "Normal (upward sloping).", v("T10Y3M") < 0 ? "neg" : "pos"));
  if (v("UNRATE") != null) out.push(card("Unemployment", pctf(v("UNRATE"), 1), S.UNRATE.sahm != null ? `Sahm-rule reading ${S.UNRATE.sahm.toFixed(2)}: ${S.UNRATE.sahm >= 0.5 ? "at the level that has signalled recessions." : "below the 0.5 recession signal."}` : "", S.UNRATE.sahm >= 0.5 ? "neg" : ""));
  if (v("RECPROUSM156N") != null) out.push(card("Recession probability (model)", pctf(v("RECPROUSM156N"), v("RECPROUSM156N") < 10 ? 1 : 0), "Smoothed probability from the Chauvet-Piger model (monthly, lags a few months).", v("RECPROUSM156N") >= 30 ? "neg" : "pos"));
  if (v("BAMLH0A0HYM2") != null) out.push(card("High-yield bond spread", pctf(v("BAMLH0A0HYM2"), 2), v("BAMLH0A0HYM2") < 3.5 ? "Tight: markets see little default risk (calm, but less cushion)." : v("BAMLH0A0HYM2") > 5 ? "Wide: markets are worried about defaults." : "Around average."));
  if (S.SP500 && S.SP500.from_high_pct != null) out.push(card("S&P 500 vs 3-year high", pctf(S.SP500.from_high_pct, 1), S.SP500.from_high_pct > -5 ? "Near its highs." : S.SP500.from_high_pct > -20 ? "In a pullback." : "In a bear market (20%+ below the high).", S.SP500.from_high_pct <= -10 ? "neg" : ""));
  if (v("VIXCLS") != null) out.push(card("Market fear gauge (VIX)", v("VIXCLS").toFixed(1), v("VIXCLS") >= 25 ? "Elevated: big daily swings expected." : "Calm to normal."));
  if (v("MORTGAGE30US") != null) out.push(card("30-year mortgage rate", pctf(v("MORTGAGE30US"), 2), "Matters if you're buying, refinancing or deciding whether to prepay a mortgage."));
  return `<div class="econ">${out.join("")}</div><p class="faint small">Source: FRED (Federal Reserve Bank of St. Louis), as of ${esc((E.asof || "").slice(0, 10))}.</p>`;
}
function renderReport(R) {
  const P = R.profile, total = R.total;
  const cls = Object.keys(CLASSES);
  const comfortWord = ["", "very low", "low", "medium", "high", "very high"][P.comfort || 3];
  const kp = (label, val, sub, extra = "") => `<div class="kpi"><span>${label}</span><b>${val}</b><small>${sub}</small>${extra}</div>`;
  const kpis = [
    total ? kp("Portfolio value", m0(total), `${R.holdings.length} holdings`) : "",
    total ? kp("Risk level", `${R.riskScore}/10`, `your comfort: ${comfortWord}`, `<div class="meter"><i style="left:calc(${(R.riskScore - 1) * 11.1}% - 2px)"></i><u style="left:calc(${Math.min(100, (R.comfortVol / 25) * 100)}% - 1px)" title="your comfort"></u></div>`) : "",
    total ? kp("Typical yearly swing", `&plusmn;${pctf(R.vol, 0)}`, `about &plusmn;${m0((R.vol * total) / 100)}`) : "",
    total && R.badYear != null ? kp("A bad year (1 in 20)", R.badYear < 0 ? `${m0((R.badYear * total) / 100)}` : "gain", `${pctf(R.badYear, 0)} in a rough year`) : "",
    total ? kp("Fund fees per year", m0(R.fees.usd), `${pctf(R.fees.pct, 2)} average on funds`) : "",
    R.runwayMonths != null ? kp("Cash runway", `${R.runwayMonths.toFixed(1)} mo`, "months of spending in cash") : R.cashflow ? kp("Monthly spending", m0(R.cashflow.avgSpend), "average, transfers excluded") : "",
  ].join("");
  const highN = R.findings.filter((f) => f.level === "high").length, medN = R.findings.filter((f) => f.level === "medium").length;
  const opt = (h) => `<select data-ov="${esc(h.symbol + "|" + h.description)}">${cls.map((k) => `<option value="${k}" ${h.cls === k ? "selected" : ""}>${CLASSES[k].label}</option>`).join("")}</select>`;
  const stressMax = Math.max(...R.stress.map((s) => Math.abs(s.pct)), 1);
  const cf = R.cashflow;
  const maxM = cf ? Math.max(...cf.months.map((m) => Math.max(m.income, m.spend)), 1) : 1;
  return `
  <div class="rep-head"><div><h1>Your portfolio checkup</h1><div class="muted">${highN ? `<b class="neg">${highN} important</b>, ` : ""}${medN} worth reviewing, ${R.findings.length - highN - medN} for your information &middot; ${new Date().toLocaleDateString()}</div></div>
    <div class="noprint"><button id="again">Change files or answers</button> <button id="print" class="primary">Save as PDF / print</button></div></div>
  ${total ? `<div class="kpis">${kpis}</div>` : `<div class="kpis">${kpis}</div>`}

  <h2 class="sec">What we noticed <small>educational observations, most important first</small></h2>
  ${R.findings.map((f) => `<div class="find ${f.level}"><h3><span class="lv">${f.level === "high" ? "Important" : f.level === "medium" ? "Review" : "FYI"}</span>${esc(f.title)}</h3><p>${f.text}</p>${f.why ? `<p class="why">${f.why}</p>` : ""}</div>`).join("") || `<p class="muted">Nothing stands out. Nice.</p>`}

  ${total ? `
  <div class="cols">
    <div><h2 class="sec">What you own</h2><div class="card donut">${donut(R.alloc)}<table class="t">${R.alloc.map((a) => `<tr><td><span class="sw" style="background:${CLASSES[a.cls].color}"></span>${CLASSES[a.cls].label}</td><td class="r">${pctf(a.pct, 1)}</td><td class="r">${m0(a.value)}</td></tr>`).join("")}</table></div>
      ${R.targetStock != null ? `<p class="muted small" style="margin-top:10px">Stocks / bonds / cash today:</p>
      <div class="mixbar"><div style="width:${R.stockPct}%;background:#3b82f6">${R.stockPct >= 8 ? "Stocks " + pctf(R.stockPct, 0) : ""}</div><div style="width:${R.bondPct}%;background:#22c55e">${R.bondPct >= 8 ? "Bonds " + pctf(R.bondPct, 0) : ""}</div><div style="width:${R.cashPct}%;background:#94a3b8">${R.cashPct >= 8 ? "Cash " + pctf(R.cashPct, 0) : ""}</div><div style="width:${R.altPct}%;background:#f97316"></div></div>
      <p class="muted small">Common guideline for you: about <b>${pctf(R.targetStock, 0)} stocks</b> (age ${P.age}, ${comfortWord} risk comfort).</p>` : `<p class="muted small">Add your age above for a guideline mix.</p>`}</div>
    <div><h2 class="sec">If history repeated <small>estimated change in value</small></h2><div class="card"><div class="stress">${R.stress.map((s) => `<div><b>${s.name}</b><br><small class="muted">${s.when}</small></div>
        <div class="b"><i style="${s.pct < 0 ? `right:50%;width:${(Math.abs(s.pct) / stressMax) * 50}%;background:#ef4444` : `left:50%;width:${(s.pct / stressMax) * 50}%;background:#22c55e`}"></i></div>
        <div class="r ${s.pct < 0 ? "neg" : "pos"}"><b>${pctf(s.pct, 0)}</b><br><small>${m0(s.usd)}</small></div>`).join("")}</div>
        <p class="muted small">Approximate peak-to-trough results of each asset type in those periods, applied to today's holdings. Real results depend on the specific funds. Crypto figures for 2000/2008 are hypothetical.</p></div>
      ${R.singles.length ? `<h2 class="sec">Individual stocks</h2><table class="t"><tr><th>Stock</th><th>Sector</th><th class="r">% of total</th><th class="r">Value</th></tr>${R.singles.slice(0, 12).map((s) => `<tr><td><b>${esc(s.symbol)}</b></td><td>${esc(s.sector)}</td><td class="r ${s.pct >= 10 ? "neg" : ""}">${pctf(s.pct, 1)}</td><td class="r">${m0(s.value)}</td></tr>`).join("")}</table>` : ""}
    </div>
  </div>

  <h2 class="sec">Holdings <small>change a type if we guessed wrong; the report updates</small></h2>
  <table class="t"><tr><th>Holding</th><th>Type</th><th class="r">Value</th><th class="r">% of total</th><th class="r">Fee</th><th class="r">Gain / loss</th></tr>
    ${R.holdings.map((h) => `<tr><td><b>${esc(h.symbol === "CASH" ? "Cash" : h.symbol)}</b> <span class="muted small">${esc(h.name || h.description).slice(0, 60)}</span>${h.mix ? `<br><small class="faint">${esc(h.how)}</small>` : ""}</td><td>${opt(h)}</td><td class="r">${m0(h.value)}</td><td class="r">${pctf((h.value / total) * 100, 1)}</td>
      <td class="r">${h.er != null ? pctf(h.er, 2) : "<span class='faint'>?</span>"}</td><td class="r ${h.gain < 0 ? "neg" : h.gain > 0 ? "pos" : ""}">${h.gain != null ? m0(h.gain) : ""}</td></tr>`).join("")}</table>` : ""}

  <h2 class="sec">The economy right now <small>and what it can mean for you</small></h2>
  ${econCards(LAST.econ)}

  ${R.trades ? `<h2 class="sec">Your trading <small>${R.trades.from.toLocaleDateString()} - ${R.trades.to.toLocaleDateString()}</small></h2>
  <div class="kpis">${kp("Trades", R.trades.count, `${R.trades.perMonth.toFixed(1)} per month`)}${kp("Amount traded", m0(R.trades.traded), R.trades.turnover != null ? `~${pctf(R.trades.turnover, 0)} turnover a year` : "")}
    ${kp("Fees & commissions", m0(R.trades.fees), "in this history")}${kp("Dividends & interest", m0(R.trades.dividends), "received")}${kp("Money added", m0(R.trades.deposits), "deposits")}${kp("Money taken out", m0(R.trades.withdrawals), "withdrawals")}</div>
  ${R.trades.bySymbol.length ? `<p class="muted small">Most traded: ${R.trades.bySymbol.map(([s, n]) => `${esc(s || "?")} (${n})`).join(", ")}</p>` : ""}` : ""}

  ${cf ? `<h2 class="sec">Cash flow <small>from your bank / card files; transfers between your own accounts excluded</small></h2>
  <div class="cols"><div class="card"><b>Money in vs out by month</b><div class="bars">${cf.months.map((m) => `<div title="${m.month}: in ${m0(m.income)}, out ${m0(m.spend)}"><i style="height:${(m.income / maxM) * 100}%;background:#22c55e"></i><i style="height:${(m.spend / maxM) * 100}%;background:#ef4444"></i><small>${m.month.slice(5)}</small></div>`).join("")}</div>
      <p class="muted small" style="margin-top:26px"><span class="sw" style="background:#22c55e"></span>in <span class="sw" style="background:#ef4444;margin-left:10px"></span>out &middot; average in ${m0(cf.avgIncome)}, out ${m0(cf.avgSpend)} a month${cf.savingsRate != null ? `, saving ${pctf(cf.savingsRate, 0)}` : ""}</p></div>
    <div class="card"><b>Where the money goes</b><table class="t">${cf.categories.slice(0, 10).map(([c, v]) => `<tr><td>${esc(c)}</td><td class="r">${m0(v / Math.max(1, cf.months.length))}/mo</td></tr>`).join("")}</table>
      ${cf.recurring.length ? `<p style="margin-top:12px"><b>Recurring charges</b> <span class="muted small">(~${m0(cf.recurringTotal)}/month)</span></p><table class="t">${cf.recurring.slice(0, 10).map((r) => `<tr><td>${esc(r.merchant)}</td><td class="r">${m0(r.typical)}</td><td class="r muted small">${r.months} months</td></tr>`).join("")}</table>` : ""}</div></div>` : ""}

  <div class="disclaimer"><b>Important:</b> this is general educational information based on the files you provided and simplified long-run assumptions (volatility, correlations and historical stress periods). It isn't personalised investment, tax or legal advice, and it can't see your full situation (other accounts, debts, taxes, goals). Past market behaviour doesn't guarantee future results. Consider a fee-only fiduciary financial planner for advice tailored to you. Your files were processed in this browser only and have not been uploaded.</div>`;
}

/* ---------------- samples */
async function loadSamples() {
  const names = ["schwab_positions.csv", "fidelity_positions.csv", "robinhood_activity.csv", "chase_checking.csv", "amex_card.csv", "merrill_holdings.xlsx"];
  const files = [];
  for (const n of names) {
    const r = await fetch("samples/" + n);
    if (r.ok) files.push(new File([await r.blob()], n));
  }
  await addFiles(files);
  if (!$("#p-age").value) { $("#p-age").value = 41; $("#p-hor").value = "20"; }
}

/* ---------------- wiring */
const drop = $("#drop");
$("#file-in").onchange = (e) => { addFiles([...e.target.files]); e.target.value = ""; };
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => addFiles([...e.dataTransfer.files]));
$$("#p-comfort button").forEach((b) => (b.onclick = () => { $$("#p-comfort button").forEach((x) => x.classList.toggle("on", x === b)); }));
$("#go").onclick = run;
$("#sample").onclick = loadSamples;
loadProfile();
fetch("api/econ").then((r) => (r.ok ? r.json() : null)).then((j) => { ECON = j; }).catch(() => {});
if (location.hash === "#demo") loadSamples().then(run); // used for screenshots / demos
