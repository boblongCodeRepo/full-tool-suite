/* Reads Excel/CSV exports in the browser and turns them into holdings / transactions / bank rows. Nothing is uploaded. */
"use strict";

const norm = (s) => String(s ?? "").toLowerCase().replace(/[^a-z0-9%$#]/g, "");
const COLS = {
  symbol: ["symbol", "ticker", "symbolcusip", "securitysymbol", "instrument", "tickersymbol", "secsymbol"],
  description: ["description", "securitydescription", "investmentname", "name", "security", "securityname", "holding", "fundname", "investment", "assetname"],
  quantity: ["quantity", "qty", "shares", "qtyquantity", "units", "shareslotquantity", "quantityheld", "sharesquantity"],
  price: ["price", "lastprice", "shareprice", "lastprice$", "currentprice", "price$", "closingprice", "marketprice", "pricepershare"],
  value: ["marketvalue", "mktvalmarketvalue", "currentvalue", "value", "totalvalue", "value$", "marketvalue$", "endingvalue", "endingmarketvalue", "mktval", "balance$"],
  cost: ["costbasis", "costbasistotal", "totalcost", "cost", "costbasis$", "totalcostbasis", "adjustedcostbasis", "originalcost"],
  costper: ["pricepaid$", "pricepaid", "averagecostbasis", "avgcost", "unitcost", "averagecost", "costpershare"],
  gain: ["gainloss$", "totalgainlossdollar", "totalgain$", "unrealizedgainloss", "gainloss", "unrealizedgl", "totalgainloss$",
    "gain$gainloss$", "unrealizedgainloss$", "gain$", "unrealizedgain$", "unrealizedgainlossamount"],
  type: ["securitytype", "type", "assetclass", "assettype", "investmenttype", "producttype", "securityclass", "category"],
  account: ["accountname", "accountnumber", "account", "accounttype", "acct"],
  date: ["date", "tradedate", "activitydate", "transactiondate", "rundate", "settlementdate", "postingdate", "postdate", "transdate", "processdate"],
  action: ["action", "transactiontype", "transcode", "activity", "activitytype", "transaction", "type", "details"],
  amount: ["amount", "amount$", "netamount", "totalamount", "amountusd", "value"],
  fees: ["fees", "commission", "feescomm", "fees$", "commissions", "feesandcommissions", "commission$"],
  debit: ["debit", "withdrawal", "withdrawals", "debitamount", "moneyout", "charges"],
  credit: ["credit", "deposit", "deposits", "creditamount", "moneyin", "payments"],
  balance: ["balance", "runningbalance", "availablebalance"],
  category: ["category", "spendingcategory", "merchantcategory"],
  payee: ["description", "payee", "merchant", "details", "memo", "transactiondescription", "originaldescription", "name"],
};
const ALL_SYNS = new Set(Object.values(COLS).flat());

function parseNum(v) {
  if (typeof v === "number") return isFinite(v) ? v : null;
  if (v == null) return null;
  let s = String(v).trim();
  if (!s || /^(--|n\/a|na|-)$/i.test(s)) return null;
  const neg = /^\(.*\)$/.test(s) || /^-/.test(s) || /-$/.test(s);
  s = s.replace(/[()$,%\s+]/g, "").replace(/-/g, "");
  const n = parseFloat(s);
  return isFinite(n) ? (neg ? -n : n) : null;
}
function parseDate(v) {
  if (v instanceof Date && !isNaN(v)) return v;
  if (typeof v === "number" && v > 20000 && v < 80000) return new Date(Math.round((v - 25569) * 86400000)); // Excel serial
  const s = String(v ?? "").trim().split(/\s+as of\s+/i)[0];
  let m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (m) return new Date(+m[1], +m[2] - 1, +m[3]);
  m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})/);
  if (m) return new Date(m[3].length === 2 ? 2000 + +m[3] : +m[3], +m[1] - 1, +m[2]);
  const d = new Date(s);
  return isNaN(d) ? null : d;
}

/* Returns [{name, rows: [[...]...]}] for every sheet (CSV = one sheet). */
async function readFile(file) {
  const buf = await file.arrayBuffer();
  const wb = XLSX.read(buf, { type: "array", cellDates: true, raw: false });
  return wb.SheetNames.map((n) => ({ name: n, rows: XLSX.utils.sheet_to_json(wb.Sheets[n], { header: 1, raw: true, defval: "", blankrows: false }) }));
}

function findHeader(rows) {
  let best = { idx: -1, score: 0 };
  for (let i = 0; i < Math.min(rows.length, 40); i++) {
    const cells = rows[i].map(norm).filter(Boolean);
    const score = cells.filter((c) => ALL_SYNS.has(c)).length;
    if (score > best.score) best = { idx: i, score };
  }
  return best.score >= 2 ? best.idx : -1;
}
function mapColumns(header) {
  const h = header.map(norm), used = new Set(), map = {};
  const order = ["symbol", "quantity", "price", "value", "cost", "costper", "gain", "date", "debit", "credit", "balance", "fees", "category", "account", "type", "action", "amount", "description", "payee"];
  for (const key of order) {
    for (const syn of COLS[key]) {
      const i = h.findIndex((x, j) => x === syn && !used.has(j));
      if (i >= 0) { map[key] = i; if (!["description", "payee"].includes(key) || map.description === undefined) used.add(i); break; }
    }
  }
  if (map.payee === undefined && map.description !== undefined) map.payee = map.description;
  return map;
}
function detectKind(map) {
  const has = (k) => map[k] !== undefined;
  if (has("date") && (has("symbol") || has("quantity")) && (has("action") || has("amount") || has("price"))) return "transactions";
  if ((has("value") || (has("quantity") && has("price"))) && (has("symbol") || has("description"))) return "holdings";
  if (has("date") && (has("amount") || has("debit") || has("credit"))) return "bank";
  return "unknown";
}

/* Table -> {kind, map, header, headerIdx, rows(data), sample} */
function analyseTable(rows, forcedKind) {
  const hi = findHeader(rows);
  if (hi < 0) return { kind: "unknown", map: {}, header: [], data: [] };
  const header = rows[hi].map((x) => String(x ?? "").trim());
  const map = mapColumns(header);
  const data = rows.slice(hi + 1).filter((r) => r.some((c) => String(c ?? "").trim() !== ""));
  const asof = rows.slice(0, hi).map((r) => r.join(" ")).join(" ").match(/as of\s+([0-9/,: -]+\w*)/i);
  return { kind: forcedKind || detectKind(map), map, header, data, asof: asof ? asof[1].trim() : "" };
}

const SKIP_ROW = /^(account\s+)?totals?\b|^grand total|pending activity|^--$|^subtotal|positions total|^total\s/i;
function cleanSymbol(s) {
  return String(s ?? "").trim().toUpperCase().replace(/\*+$/, "").replace(/\s+/g, " ");
}

function toHoldings(t, src) {
  const m = t.map, out = [];
  const g = (r, k) => (m[k] === undefined ? "" : r[m[k]]);
  for (const r of t.data) {
    const symRaw = String(g(r, "symbol") ?? "").trim(), desc = String(g(r, "description") ?? "").trim();
    const label = symRaw || desc;
    if (!label || SKIP_ROW.test(label) || SKIP_ROW.test(desc)) continue;
    const qty = parseNum(g(r, "quantity")), price = parseNum(g(r, "price"));
    let value = parseNum(g(r, "value"));
    if (value == null && qty != null && price != null) value = qty * price;
    if (value == null || Math.abs(value) < 0.005) continue;
    let cost = parseNum(g(r, "cost"));
    const cper = parseNum(g(r, "costper"));
    if (cost == null && cper != null && qty != null) cost = cper * qty;
    let gain = parseNum(g(r, "gain"));
    if (gain == null && cost != null) gain = value - cost;
    const isCashRow = /cash\s*&|cash and|^cash$|money market|sweep|core position/i.test(symRaw + " " + desc);
    out.push({ symbol: isCashRow && !FUNDS[cleanSymbol(symRaw)] ? "CASH" : cleanSymbol(symRaw), description: desc, quantity: qty, price, value, cost, gain,
      type: String(g(r, "type") ?? ""), account: String(g(r, "account") ?? "") || src, source: src });
  }
  return out;
}
function classifyAction(s) {
  s = String(s || "").toLowerCase();
  if (/reinvest/.test(s)) return "reinvest";
  if (/\bbuy\b|bought|purchase|\bbto\b/.test(s)) return "buy";
  if (/\bsell\b|sold|\bstc\b|redemption/.test(s)) return "sell";
  if (/div|cdiv|qualified/.test(s)) return "dividend";
  if (/interest|\bint\b/.test(s)) return "interest";
  if (/fee|commission|adr fee|\bgold\b/.test(s)) return "fee";
  if (/deposit|contribution|ach\s*in|transfer\s*in|journal.*in|electronic funds transfer received|\bach\b|\brtp\b|wire in/.test(s)) return "deposit";
  if (/withdraw|ach\s*out|transfer\s*out|disbursement|wire out|journal.*out/.test(s)) return "withdrawal";
  return "other";
}
function toTransactions(t, src) {
  const m = t.map, out = [];
  const g = (r, k) => (m[k] === undefined ? "" : r[m[k]]);
  for (const r of t.data) {
    const d = parseDate(g(r, "date"));
    if (!d) continue;
    const actionText = [g(r, "action"), m.description !== m.action ? g(r, "description") : ""].join(" ");
    const qty = parseNum(g(r, "quantity")), price = parseNum(g(r, "price"));
    let amount = parseNum(g(r, "amount"));
    const kind = classifyAction(actionText);
    if (amount == null && qty != null && price != null) amount = (kind === "buy" ? -1 : 1) * Math.abs(qty * price);
    out.push({ date: d, kind, action: String(g(r, "action") || "").trim(), symbol: cleanSymbol(g(r, "symbol")), quantity: qty, price, amount,
      fees: Math.abs(parseNum(g(r, "fees")) || 0), description: String(g(r, "description") || "").trim(), source: src });
  }
  return out;
}
function toBank(t, src) {
  const m = t.map, out = [];
  const g = (r, k) => (m[k] === undefined ? "" : r[m[k]]);
  for (const r of t.data) {
    const d = parseDate(g(r, "date"));
    if (!d) continue;
    let amount = parseNum(g(r, "amount"));
    if (amount == null) {
      const dr = parseNum(g(r, "debit")), cr = parseNum(g(r, "credit"));
      if (dr == null && cr == null) continue;
      amount = (cr ? Math.abs(cr) : 0) - (dr ? Math.abs(dr) : 0);
    }
    out.push({ date: d, amount, payee: String(g(r, "payee") || "").trim(), category: String(g(r, "category") || "").trim(),
      balance: parseNum(g(r, "balance")), source: src });
  }
  return out;
}
