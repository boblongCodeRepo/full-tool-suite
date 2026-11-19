/* Portfolio analysis: classification, risk, stress tests, cash flow and educational suggestions. Runs in the browser. */
"use strict";
const THIS_YEAR = new Date().getFullYear();

/* ---------- classification */
function targetDateMix(name) {
  const m = name.match(/(?:target|retirement|freedom|lifepath|target date|index)\D{0,25}(20[2-7]\d)/i) || name.match(/\b(20[2-7][05])\s+(?:fund|trust|target)/i);
  if (!m) return null;
  const yrs = +m[1] - THIS_YEAR;
  const stock = yrs >= 25 ? 0.9 : yrs <= -7 ? 0.3 : yrs <= 0 ? 0.5 - (-yrs / 7) * 0.2 : 0.5 + (yrs / 25) * 0.4;
  return { us_stock: stock * 0.6, intl_stock: stock * 0.4, us_bond: (1 - stock) * 0.8, tips: (1 - stock) * 0.2, _year: +m[1] };
}
function classify(h) {
  const sym = h.symbol;
  // Fidelity's "Type" column holds the ACCOUNT type (Cash / Margin), not the security type: ignore those values
  const type = /^(cash|margin|ira|roth|individual|joint|taxable|brokerage)$/i.test(String(h.type).trim()) ? "" : h.type;
  const d = `${h.description} ${type}`;
  const f = FUNDS[sym];
  if (f) return { cls: f.c, er: f.er, mix: f.mix, name: f.n, how: "fund list", sector: SECTOR_ETF[sym] };
  if (sym === "CASH" || CASH_RE.test(d)) return { cls: "cash", er: 0, how: "cash" };
  const td = targetDateMix(h.description);
  if (td) return { cls: "us_stock", mix: td, er: null, how: `target-date ${td._year} fund (estimated mix)` };
  if (/\b(BTC|ETH|SOL|DOGE|XRP|ADA)\b|bitcoin|ethereum|crypto/i.test(sym + " " + d)) return { cls: "crypto", er: null, how: "name" };
  if (/option|call|put\b/i.test(h.type) || /^\w{1,5}\s?\d{6}[CP]\d+/.test(sym)) return { cls: "other", er: null, how: "options", option: true };
  if (/treasury|t-?bill|bond|note|fixed income|\bcd\b|certificate of deposit|municipal|muni|agency/i.test(d)) {
    if (/municipal|muni|tax[- ]exempt/i.test(d)) return { cls: "muni", er: null, how: "name" };
    if (/high yield|junk/i.test(d)) return { cls: "hy_bond", er: null, how: "name" };
    if (/tips|inflation/i.test(d)) return { cls: "tips", er: null, how: "name" };
    if (/bill|money/i.test(d)) return { cls: "cash", er: null, how: "name" };
    return { cls: "us_bond", er: null, how: "name" };
  }
  if (/real estate|reit/i.test(d)) return { cls: "reit", er: null, how: "name" };
  if (/gold|silver|precious/i.test(d)) return { cls: "gold", er: null, how: "name" };
  if (/emerging/i.test(d)) return { cls: "em_stock", er: null, how: "name" };
  if (/international|intl|developed|ex[- ]us|world ex|europe|pacific|eafe|global ex/i.test(d)) return { cls: "intl_stock", er: null, how: "name" };
  if (/etf|fund|index|trust|portfolio|admiral|investor|instl/i.test(d) || /^[A-Z]{4}X$/.test(sym)) return { cls: "us_stock", er: null, how: "fund (guessed US stocks)" };
  if (/equity|stock|common|ordinary|adr|^cs$|shares/i.test(d) || /^[A-Z]{1,5}([.-][A-Z])?$/.test(sym)) {
    return { cls: "single", er: 0, how: "individual stock", sector: SECTOR_OF[sym] || SECTOR_OF[sym.replace(/[.-]/, "")] || "Unknown sector" };
  }
  return { cls: "other", er: null, how: "unknown" };
}

/* ---------- helpers */
const sum = (a) => a.reduce((x, y) => x + y, 0);
const pct = (x, tot) => (tot ? (100 * x) / tot : 0);
const money = (v, d = 0) => (v == null || !isFinite(v) ? "--" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));
const pctf = (v, d = 1) => (v == null || !isFinite(v) ? "--" : v.toFixed(d) + "%");
const monthKey = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;

/* ---------- main */
function analyze({ holdings, transactions, bank, profile, econ, overrides }) {
  const R = { findings: [], profile };
  const E = (econ && econ.series) || {};
  const ev = (id, k = "value") => (E[id] ? E[id][k] : null);

  /* Holdings -> exposures */
  const H = holdings.map((h) => {
    const c = classify(h);
    const ov = overrides[h.symbol + "|" + h.description];
    if (ov) { c.cls = ov; c.mix = null; c.how = "set by you"; if (ov !== "single") c.sector = null; }
    return { ...h, ...c };
  });
  const total = sum(H.map((h) => h.value));
  R.total = total;
  R.holdings = H.sort((a, b) => b.value - a.value);
  const exp = {};
  for (const h of H) {
    const mix = h.mix ? Object.entries(h.mix).filter(([k]) => !k.startsWith("_")) : [[h.cls, 1]];
    for (const [k, w] of mix) exp[k] = (exp[k] || 0) + h.value * w;
  }
  R.alloc = Object.entries(exp).filter(([, v]) => Math.abs(v) > 0.5).map(([k, v]) => ({ cls: k, value: v, pct: pct(v, total) })).sort((a, b) => b.value - a.value);
  const share = (...ks) => sum(ks.map((k) => exp[k] || 0)) / (total || 1) * 100;
  R.stockPct = share("us_stock", "intl_stock", "em_stock", "single", "other");
  R.bondPct = share("us_bond", "tips", "muni", "intl_bond", "hy_bond");
  R.cashPct = share("cash");
  R.altPct = share("reit", "commodity", "gold", "crypto");
  const eqTotal = sum(["us_stock", "intl_stock", "em_stock", "single"].map((k) => exp[k] || 0));
  R.intlShareOfStocks = eqTotal ? ((exp.intl_stock || 0) + (exp.em_stock || 0)) / eqTotal * 100 : 0;

  /* Volatility: each individual stock is its own position, funds pooled by class */
  const pos = [];
  for (const h of H) {
    if (h.cls === "single" && !h.mix) pos.push({ w: h.value / total, vol: CLASSES.single.vol, g: "eq", single: true });
    else (h.mix ? Object.entries(h.mix).filter(([k]) => !k.startsWith("_")) : [[h.cls, 1]]).forEach(([k, w]) => pos.push({ w: (h.value * w) / total, vol: CLASSES[k].vol, g: GROUP[k], single: false, k }));
  }
  let varp = 0;
  for (const a of pos) for (const b of pos) {
    const rho = a === b ? 1 : a.single && b.single ? 0.45 : corr(a.g, b.g);
    varp += a.w * b.w * a.vol * b.vol * rho;
  }
  R.vol = Math.sqrt(Math.max(varp, 0));
  R.riskScore = Math.max(1, Math.min(10, Math.round(R.vol <= 2 ? 1 : R.vol <= 4 ? 2 : R.vol <= 6 ? 3 : R.vol <= 8 ? 4 : R.vol <= 10 ? 5 : R.vol <= 12 ? 6 : R.vol <= 14 ? 7 : R.vol <= 17 ? 8 : R.vol <= 22 ? 9 : 10)));
  const tbill = ev("DTB3");
  const retOf = (k) => (k === "cash" && tbill != null ? tbill : CLASSES[k].ret);
  const known = R.alloc.filter((a) => retOf(a.cls) != null);
  R.expReturn = known.length ? sum(known.map((a) => a.pct * retOf(a.cls))) / sum(known.map((a) => a.pct)) : null;
  R.badYear = R.expReturn != null ? R.expReturn - 1.65 * R.vol : null; // roughly a 1-in-20 bad year
  R.stress = STRESS.map((s) => {
    let loss = 0;
    for (const h of H) (h.mix ? Object.entries(h.mix).filter(([k]) => !k.startsWith("_")) : [[h.cls, 1]]).forEach(([k, w]) => (loss += h.value * w * CLASSES[k].s[s.id] / 100));
    return { ...s, pct: (loss / total) * 100, usd: loss };
  });

  /* Fees */
  const withEr = H.filter((h) => h.er != null);
  const feeUsd = sum(withEr.map((h) => (h.value * h.er) / 100));
  const unknownFeeVal = sum(H.filter((h) => h.er == null && !["cash", "single"].includes(h.cls)).map((h) => h.value));
  const erBase = sum(withEr.filter((h) => h.cls !== "single").map((h) => h.value));
  R.fees = { usd: feeUsd, pct: erBase ? (feeUsd / erBase) * 100 : 0, unknownPct: pct(unknownFeeVal, total) };

  /* Concentration & sectors */
  const bySym = {};
  for (const h of H) if (h.cls === "single") bySym[h.symbol] = (bySym[h.symbol] || 0) + h.value;
  R.singles = Object.entries(bySym).map(([s, v]) => ({ symbol: s, value: v, pct: pct(v, total), sector: SECTOR_OF[s] || "Unknown sector" })).sort((a, b) => b.value - a.value);
  const sec = {};
  for (const h of H) if (h.cls === "single" || h.sector) { const k = h.sector || "Unknown sector"; sec[k] = (sec[k] || 0) + h.value; }
  R.sectors = Object.entries(sec).map(([k, v]) => ({ sector: k, value: v, pctOfStocks: pct(v, eqTotal), pct: pct(v, total) })).sort((a, b) => b.value - a.value);
  R.top10 = sum(R.holdings.slice(0, 10).map((h) => h.value)) / (total || 1) * 100;

  /* Bonds & rates */
  const bondVal = sum(["us_bond", "tips", "muni", "intl_bond", "hy_bond"].map((k) => exp[k] || 0));
  R.duration = bondVal ? sum(["us_bond", "tips", "muni", "intl_bond", "hy_bond"].map((k) => (exp[k] || 0) * CLASSES[k].dur)) / bondVal : 0;
  R.rateShock = -bondVal * R.duration / 100; // +1% rates

  /* Unrealized gains/losses */
  R.losers = H.filter((h) => h.gain != null && h.gain <= -1000).sort((a, b) => a.gain - b.gain);
  R.bigGains = H.filter((h) => h.gain != null && h.cls === "single" && h.gain >= 10000 && h.value / total >= 0.05).sort((a, b) => b.gain - a.gain);
  R.unclassifiedPct = share("other");

  /* Trades */
  if (transactions.length) {
    const T = transactions;
    const dates = T.map((t) => t.date.getTime());
    const months = Math.max(1, (Math.max(...dates) - Math.min(...dates)) / (30.44 * 86400000));
    const trades = T.filter((t) => t.kind === "buy" || t.kind === "sell");
    const traded = sum(trades.map((t) => Math.abs(t.amount || 0)));
    R.trades = {
      months, count: trades.length, perMonth: trades.length / months, traded,
      turnover: total ? (traded / 2 / total) * (12 / months) * 100 : null,
      fees: sum(T.map((t) => t.fees)) + sum(T.filter((t) => t.kind === "fee").map((t) => Math.abs(t.amount || 0))),
      dividends: sum(T.filter((t) => t.kind === "dividend" || t.kind === "interest").map((t) => Math.abs(t.amount || 0))),
      deposits: sum(T.filter((t) => t.kind === "deposit").map((t) => Math.abs(t.amount || 0))),
      withdrawals: sum(T.filter((t) => t.kind === "withdrawal").map((t) => Math.abs(t.amount || 0))),
      bySymbol: Object.entries(trades.reduce((m, t) => ((m[t.symbol] = (m[t.symbol] || 0) + 1), m), {})).sort((a, b) => b[1] - a[1]).slice(0, 8),
      from: new Date(Math.min(...dates)), to: new Date(Math.max(...dates)),
    };
  }

  /* Bank / cash flow */
  if (bank.length) R.cashflow = cashflow(bank);
  const monthlySpend = profile.monthlySpend || (R.cashflow && R.cashflow.avgSpend) || null;
  const liquid = (exp.cash || 0) + ((R.cashflow && R.cashflow.lastBalance) || 0);
  R.runwayMonths = monthlySpend ? liquid / monthlySpend : null;

  /* Targets from age & comfort */
  const age = profile.age, comfort = profile.comfort || 3;
  R.targetStock = age ? Math.max(20, Math.min(95, 110 - age + (comfort - 3) * 10)) : null;
  R.comfortVol = [0, 5, 8, 11, 14, 17][comfort];

  buildFindings(R, { exp, total, econ: E, ev, profile, monthlySpend, tbill });
  return R;
}

function cashflow(rows) {
  /* Some card exports show purchases as positive numbers: flip those files. */
  const bySrc = {};
  rows.forEach((r) => (bySrc[r.source] = bySrc[r.source] || []).push(r));
  const all = [];
  for (const list of Object.values(bySrc)) {
    const pos = list.filter((r) => r.amount > 0).length;
    const payNeg = list.some((r) => r.amount < 0 && /payment|thank you|autopay/i.test(r.payee));
    const flip = pos / list.length > 0.7 && payNeg;
    list.forEach((r) => all.push(flip ? { ...r, amount: -r.amount } : r));
  }
  // moves between your own accounts (incl. paying off a card whose charges are in another file) aren't spending
  const XFER = /transfer|xfer|payment thank you|autopay|credit card payment|card payment|online payment|to savings|from savings|zelle.*(self|own)|brokerage|vanguard|fidelity|schwab|robinhood|e\*?trade|investment|\bpmt\b|american express|\bamex\b|discover (card|bank)|capital one|citi card|card services|epay|crd pmt|cc payment/i;
  const INCOME = /payroll|salary|direct dep|dir dep|paycheck|ach credit.*(pay|sal)|employer|irs treas|tax ref/i;
  const CAT = [["Housing", /rent|mortgage|hoa|property mgmt|apartments?/i], ["Groceries", /safeway|giant|whole foods|trader joe|kroger|costco|aldi|wegmans|harris teeter|publix|grocery|market/i],
    ["Dining", /restaurant|cafe|coffee|starbucks|doordash|uber\s*eats|grubhub|chipotle|mcdonald|pizza|sweetgreen|bar\b|grill|kitchen/i],
    ["Transport", /uber|lyft|metro|wmata|smartrip|shell|exxon|chevron|sunoco|bp\b|parking|toll|ez-?pass|gas station/i],
    ["Utilities & phone", /pepco|dominion|bge|washington gas|verizon|comcast|xfinity|at&t|t-mobile|water|electric|internet/i],
    ["Subscriptions", /netflix|spotify|hulu|disney\+?|apple\.com|icloud|youtube|hbo|\bmax\b|adobe|microsoft|peloton|gym|fitness|patreon|audible|prime video|amazon prime/i],
    ["Shopping", /amazon|target|walmart|best buy|ebay|etsy|home depot|lowe'?s|ikea|macy|nordstrom/i],
    ["Travel", /airline|united air|delta air|american air|southwest|jetblue|marriott|hilton|hyatt|airbnb|expedia|hotel/i],
    ["Health", /pharmacy|cvs|walgreens|doctor|dental|medical|hospital|clinic|vision/i], ["Insurance", /insurance|geico|state farm|allstate|progressive/i]];
  const months = {}, cats = {}, merchants = {};
  let income = 0, spend = 0, lastBal = null, lastDate = 0;
  for (const r of all) {
    const mk = monthKey(r.date);
    months[mk] = months[mk] || { income: 0, spend: 0 };
    if (r.balance != null && r.date.getTime() >= lastDate) { lastBal = r.balance; lastDate = r.date.getTime(); }
    if (XFER.test(r.payee) && !INCOME.test(r.payee)) continue;
    if (r.amount > 0) { months[mk].income += r.amount; income += r.amount; continue; }
    const amt = -r.amount;
    months[mk].spend += amt; spend += amt;
    const cat = (r.category && !/^(debit|credit|sale|purchase)$/i.test(r.category)) ? r.category : (CAT.find(([, re]) => re.test(r.payee)) || ["Other"])[0];
    cats[cat] = (cats[cat] || 0) + amt;
    const merch = r.payee.toLowerCase().replace(/[#*]?\d[\d\s#*-]*/g, " ").replace(/[.*/_]+/g, " ").replace(/\b(pos|debit|purchase|card|ach|recurring|payment|payments|online|www|com|bill|usa|inc|llc)\b/g, " ").replace(/\s+/g, " ").trim().split(" ").slice(0, 3).join(" ");
    if (merch) (merchants[merch] = merchants[merch] || []).push({ mk, amt, cat });
  }
  const mkeys = Object.keys(months).sort();
  const full = mkeys.length > 2 ? mkeys.slice(1, -1) : mkeys; // drop partial first/last months when possible
  const avgSpend = sum(full.map((k) => months[k].spend)) / (full.length || 1);
  const avgIncome = sum(full.map((k) => months[k].income)) / (full.length || 1);
  const recurring = Object.entries(merchants).map(([m, xs]) => {
    const ms = new Set(xs.map((x) => x.mk));
    const amts = xs.map((x) => x.amt).sort((a, b) => a - b), med = amts[Math.floor(amts.length / 2)];
    const steady = amts.filter((a) => Math.abs(a - med) <= Math.max(2, med * 0.15)).length / amts.length;
    return { merchant: m, months: ms.size, typical: med, steady, cat: xs[0].cat };
  }).filter((x) => x.months >= 3 && x.steady >= 0.7 && x.months >= mkeys.length * 0.6).sort((a, b) => b.typical - a.typical);
  const subs = recurring.filter((r) => r.typical < 100 && !["Housing", "Utilities & phone", "Insurance", "Dining", "Groceries", "Transport", "Shopping"].includes(r.cat));
  return { months: mkeys.map((k) => ({ month: k, ...months[k] })), avgSpend, avgIncome, savingsRate: avgIncome ? ((avgIncome - avgSpend) / avgIncome) * 100 : null,
    categories: Object.entries(cats).sort((a, b) => b[1] - a[1]), recurring: recurring.slice(0, 20), recurringTotal: sum(recurring.map((r) => r.typical)),
    subs, subsTotal: sum(subs.map((r) => r.typical)), lastBalance: lastBal };
}

/* ---------- findings (educational, not personal advice) */
function buildFindings(R, c) {
  const F = R.findings, add = (level, title, text, why) => F.push({ level, title, text, why });
  const { total, ev, profile } = c;
  if (!total) return;
  // concentration
  for (const s of R.singles.slice(0, 5)) {
    if (s.pct >= 10) add("high", `${s.symbol} is ${pctf(s.pct, 0)} of everything`, `One company at this size means a single bad event (earnings miss, lawsuit, fraud) can move your whole net worth. ${CLASSES.single.vol}%-a-year swings are normal for one stock, about twice the market.`, "Many planners keep any single stock under 5-10% of a portfolio, especially employer stock.");
    else if (s.pct >= 5) add("medium", `${s.symbol} is ${pctf(s.pct, 0)} of the portfolio`, "A large single-company position. Fine if deliberate, but it adds risk that diversification removes for free.", "Common rule of thumb: no single stock over 5-10%.");
  }
  const topSec = R.sectors.find((s) => s.sector !== "Unknown sector");
  if (topSec && topSec.pctOfStocks >= 35 && topSec.value > total * 0.1) add("medium", `${topSec.sector} is ${pctf(topSec.pctOfStocks, 0)} of your stocks`, `Counting individual stocks and sector funds only (broad index funds also hold some). The whole US market is about 30% technology; a much larger tilt makes results depend on one industry.`, "Sector concentration magnified losses in 2000-02 (tech) and 2008 (financials).");
  // mix vs guideline
  if (R.targetStock != null) {
    const gap = R.stockPct - R.targetStock;
    if (Math.abs(gap) >= 15) add("medium", gap > 0 ? `More in stocks than a typical mix for your age and comfort` : `Less in stocks than a typical mix for your age and comfort`,
      `You hold about ${pctf(R.stockPct, 0)} in stocks; a common guideline for age ${profile.age} and your risk comfort is around ${pctf(R.targetStock, 0)}. ${gap > 0 ? "That means bigger swings, especially if you need the money within a few years." : "That may mean slower growth, a real cost over long periods because of inflation."}`,
      "Guideline used: 110 minus your age, plus or minus 10 points per step of risk comfort. It's a starting point, not a rule.");
  }
  // risk vs comfort
  if (R.vol > R.comfortVol + 3) add("high", "Riskier than the comfort level you chose", `Estimated yearly swings of about ±${pctf(R.vol, 0)} (risk ${R.riskScore}/10). In a bad year (roughly 1 in 20) this mix could fall around ${pctf(Math.abs(Math.min(0, R.badYear)), 0)}, about ${money(Math.abs(Math.min(0, R.badYear)) * total / 100)}.`, "People most often lose money by selling in a panic during a fall they didn't expect. Matching risk to comfort helps you stay invested.");
  else if (R.vol < R.comfortVol - 5 && (profile.horizon || 10) >= 10) add("info", "More cautious than the comfort level you chose", `Estimated swings of about ±${pctf(R.vol, 0)} a year. With a ${profile.horizon || "10+"}-year horizon and your stated comfort, you could accept more growth assets.`, "Over long periods, very cautious mixes often trail inflation after taxes.");
  // fees
  if (R.fees.pct >= 0.4) {
    const drag20 = total * (Math.pow(1.06, 20) - Math.pow(1.06 - R.fees.pct / 100 + 0.0005, 20));
    const pricey = R.holdings.filter((h) => h.er != null && h.er >= 0.5).slice(0, 6);
    add("medium", `Fund fees average ${pctf(R.fees.pct, 2)} a year`, `About ${money(R.fees.usd)} a year. Over 20 years at 6% growth, the gap versus low-cost index funds (~0.05%) could reach roughly ${money(drag20)}. ${pricey.length ? "Highest-cost holdings: " + pricey.map((h) => `${h.symbol || h.description} (${h.er}%)${CHEAP[h.cls] ? `; a common alternative is ${CHEAP[h.cls]}` : ""}`).join("; ") + "." : ""}`, "Fees are one of the few things investors fully control, and research consistently links lower costs to better net returns.");
  }
  else {
    const pricey = R.holdings.filter((h) => h.er != null && h.er >= 0.5 && h.value >= 10000);
    if (pricey.length) add("info", "Some funds are expensive", pricey.slice(0, 6).map((h) => `${h.symbol || h.description} costs ${h.er}% a year (${money((h.value * h.er) / 100)})${CHEAP[h.cls] ? `; a common alternative is ${CHEAP[h.cls]}` : ""}`).join(". ") + ".", "Check for sales charges or taxes before switching, especially in taxable accounts.");
  }
  if (R.fees.unknownPct >= 15) add("info", "Some fund fees are unknown", `${pctf(R.fees.unknownPct, 0)} of the portfolio is in funds this tool doesn't recognise, so fees for those aren't included. Check each fund's expense ratio on its fact sheet.`, "");
  // cash
  const tb = c.tbill, cpi = ev("CPIAUCSL", "yoy");
  if (R.cashPct >= 15 && (profile.age || 40) < 60 && (profile.horizon || 10) >= 5) add("info", `${pctf(R.cashPct, 0)} is in cash`, `Cash is earning about ${tb != null ? pctf(tb, 1) : "the T-bill rate"}${cpi != null ? ` while prices are rising ${pctf(cpi, 1)} a year` : ""}. Beyond an emergency fund and money you need soon, large cash balances tend to lose ground to inflation over time.`, "If the cash is sitting in a low-rate sweep account, a Treasury money market fund often pays more.");
  if (R.runwayMonths != null && R.runwayMonths < 3) add("high", "Emergency fund looks thin", `Cash on hand covers about ${R.runwayMonths.toFixed(1)} months of spending. A common target is 3-6 months (more if income is irregular).`, "An emergency fund prevents selling investments at a bad time or taking on high-interest debt.");
  // rates
  if (R.duration > 0 && R.rateShock < -1000) add("info", "Interest-rate sensitivity", `Your bonds have an average duration of about ${R.duration.toFixed(1)} years: a 1% rise in rates would lower them roughly ${money(Math.abs(R.rateShock))}${ev("DGS10") != null ? `; with the 10-year Treasury at ${pctf(ev("DGS10"), 2)}, new bond money also earns more than it did in 2020-21` : ""}.`, "Longer-duration bonds swing more with rates; shorter ones swing less but lock in yields for less time.");
  // inflation
  const realShare = sum(["tips", "reit", "commodity", "gold"].map((k) => c.exp[k] || 0)) / total * 100;
  if (cpi != null && cpi >= 3 && realShare < 5 && R.bondPct >= 20) add("info", "Little inflation protection in the bond side", `Inflation is running ${pctf(cpi, 1)} and under ${pctf(realShare, 0)} of the portfolio is in inflation-linked assets (TIPS, real estate, commodities, gold).`, "TIPS adjust with inflation; regular bonds lose purchasing power when inflation surprises upward, as in 2022.");
  // economy
  const rec = ev("RECPROUSM156N"), sahm = E_(c.econ, "UNRATE", "sahm"), inv = E_(c.econ, "T10Y3M", "inverted_days_3m"), hy = ev("BAMLH0A0HYM2");
  const warn = [];
  if (rec != null && rec >= 30) warn.push(`a ${pctf(rec, 0)} model recession probability`);
  if (sahm != null && sahm >= 0.5) warn.push(`the Sahm-rule recession signal (${sahm.toFixed(2)})`);
  if (inv != null && inv >= 40) warn.push("an inverted 10-year/3-month yield curve");
  if (hy != null && hy >= 5) warn.push(`wide high-yield credit spreads (${pctf(hy, 1)})`);
  if (warn.length) add((profile.horizon || 10) <= 3 ? "high" : "medium", "Economic warning signs are flashing", `Current data shows ${warn.join(", ")}. ${(profile.horizon || 10) <= 3 ? "Because you need this money within about " + profile.horizon + " years, check that near-term needs are not in volatile assets." : "Recessions often bring stock declines of 20-35%; check the stress-test section to see what that would mean in dollars."}`, "Signals are imperfect and timing the market is very hard; the main use is checking you could ride out a downturn.");
  else if (rec != null || sahm != null) add("info", "Economy: no major recession signals right now", `Recession probability ${rec != null ? pctf(rec, rec < 10 ? 1 : 0) : "n/a"}, Sahm rule ${sahm != null ? sahm.toFixed(2) : "n/a"} (0.5+ is the warning level), yield curve ${inv ? "partly inverted recently" : "not inverted"}.`, "Downturns can still start without warning; diversification is the main defence.");
  const ddown = E_(c.econ, "SP500", "from_high_pct");
  if (ddown != null && ddown <= -15 && R.stockPct >= 40) add("info", `Stocks are ${pctf(Math.abs(ddown), 0)} below their recent high`, "Falls like this are normal (they happen every few years). Historically, people who kept investing through declines recovered faster than those who sold.", "Rebalancing back to your target mix after a fall means buying stocks while they're cheaper.");
  if (hy != null && hy < 3.5 && (c.exp.hy_bond || 0) / total > 0.1) add("info", "High-yield bonds are priced for good times", `Spreads are tight at ${pctf(hy, 1)}: investors are being paid little extra for default risk, and ${pctf((c.exp.hy_bond / total) * 100, 0)} of the portfolio is in high-yield debt.`, "High-yield bonds tend to fall alongside stocks in recessions (they lost about a third in 2008).");
  // diversification & specials
  if (R.stockPct >= 40 && R.intlShareOfStocks < 10) add("info", "Almost no international stocks", `Only ${pctf(R.intlShareOfStocks, 0)} of your stocks are outside the US; non-US companies are about 35-40% of world stock market value.`, "International stocks don't always help, but they have led for long stretches (e.g. 2000-2010), which diversifies country risk.");
  const crypto = (c.exp.crypto || 0) / total * 100;
  if (crypto >= 15) add("high", `Crypto is ${pctf(crypto, 0)} of the portfolio`, "Crypto has fallen 70-80% several times (2018, 2022). At this size a crash would badly dent total wealth.", "Many advisers who allow crypto suggest single-digit percentages at most.");
  else if (crypto >= 5) add("medium", `Crypto is ${pctf(crypto, 0)} of the portfolio`, "A meaningful bet on a very volatile asset (swings of 70%+ a year are common).", "");
  const lev = R.holdings.filter((h) => /leveraged|ultra|\b[23]x\b|inverse|short qqq/i.test((h.name || "") + " " + h.description));
  if (lev.length) add("high", "Leveraged or inverse funds held", `${lev.map((h) => h.symbol).join(", ")}: these reset daily and can lose money even when the market ends up where it started. They're designed for short-term trading.`, "Long-term returns of 2x/3x funds can differ wildly from 2x/3x the index.");
  if (R.unclassifiedPct >= 10) add("info", "Some holdings couldn't be identified", `${pctf(R.unclassifiedPct, 0)} of the value is "Other / unclassified". Set the right type in the holdings table to make the risk numbers more accurate.`, "");
  // tax
  if (R.losers.length) add("info", "Possible tax-loss harvesting (taxable accounts only)", `Holdings with losses of $1,000+: ${R.losers.slice(0, 6).map((h) => `${h.symbol || h.description} (${money(h.gain)})`).join(", ")}. In a taxable account, selling at a loss can offset gains and up to $3,000 of income a year.`, "Buying the same or a substantially identical security within 30 days triggers the wash-sale rule. Not relevant inside IRAs/401(k)s.");
  if (R.bigGains.length) add("info", "Large gains in concentrated positions", `${R.bigGains.slice(0, 4).map((h) => `${h.symbol} (+${money(h.gain)})`).join(", ")}. Reducing a concentrated winner in a taxable account triggers capital gains; spreading sales over tax years, gifting shares, or donating appreciated stock are common ways to manage that.`, "");
  // trades
  if (R.trades) {
    if (R.trades.perMonth >= 10) add("medium", "Very active trading", `About ${R.trades.perMonth.toFixed(0)} trades a month${R.trades.turnover != null ? ` (roughly ${pctf(R.trades.turnover, 0)} of the portfolio turned over per year)` : ""}. Short-term gains are taxed as ordinary income in taxable accounts, and frequent trading is linked to lower returns in most studies.`, "");
    if (R.trades.fees >= 200) add("info", `${money(R.trades.fees)} in trading fees and commissions`, `Over ${R.trades.months.toFixed(0)} months of history.`, "Most large brokers now offer $0 commissions on US stocks and ETFs.");
  }
  // cash flow
  if (R.cashflow) {
    const cf = R.cashflow;
    if (cf.savingsRate != null && cf.avgIncome > 0) {
      if (cf.savingsRate < 10) add("medium", `Saving about ${pctf(Math.max(cf.savingsRate, 0), 0)} of income`, `Average monthly income ${money(cf.avgIncome)} vs spending ${money(cf.avgSpend)} (transfers excluded). A common goal is 15-20% for retirement plus emergency savings.`, "");
      else add("info", `Saving about ${pctf(cf.savingsRate, 0)} of income`, `Average monthly income ${money(cf.avgIncome)} vs spending ${money(cf.avgSpend)}.`, "");
    }
    if (cf.subs.length >= 2) add("info", `${money(cf.subsTotal, 2)} a month in subscriptions and memberships`, `Charges that repeat every month: ${cf.subs.slice(0, 8).map((r) => `${r.merchant} (${money(r.typical, 2)})`).join(", ")}. That's about ${money(cf.subsTotal * 12)} a year; worth a quick review for ones you no longer use.`, "");
  }
  const order = { high: 0, medium: 1, info: 2 };
  F.sort((a, b) => order[a.level] - order[b.level]);
}
const E_ = (E, id, k) => (E && E[id] ? E[id][k] : null);
