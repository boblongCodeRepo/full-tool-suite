"""Security research functions (Bloomberg-style DES sub-functions) and global tools.

Per-security:  GP (chart + studies), FA (financials), ERN (earnings/estimates), ANR (analysts),
               OMON (options), HDS (holders/insiders/fund holdings), DVD (dividends), RV (peers)
Global:        COMP (multi-security comparison), EVTS (earnings/economic/IPO calendar), EQS (screener)

All data from Yahoo Finance via yfinance (no API keys).
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd
import yfinance as yf
from yfinance import EquityQuery as Q

from data import CACHE, POOL, _f, quotes, safe


def _sym(s):
    return (s or "").upper().strip()


def _date(x):
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return pd.Timestamp(x).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return str(x)


def _str(x):
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return str(x)


def _info(sym):
    def load():
        try:
            return yf.Ticker(sym).info or {}
        except Exception:  # noqa: BLE001
            return {}
    return CACHE.get("rinfo:" + sym, 900, load)


def _frame_rows(df, drop=("currency", "methodology")):
    """DataFrame indexed by period -> list of dicts of numeric columns."""
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for idx, r in df.iterrows():
        d = {"period": str(idx)}
        for c in df.columns:
            if c in drop:
                continue
            d[c] = _f(r[c])
        out.append(d)
    return out


# =========================================================================== GP: chart + studies
GP_SPEC = {"1D": ("5d", "5m"), "5D": ("1mo", "30m"), "1M": ("2y", "1d"), "3M": ("2y", "1d"),
           "6M": ("2y", "1d"), "YTD": ("2y", "1d"), "1Y": ("2y", "1d"), "2Y": ("5y", "1d"),
           "5Y": ("10y", "1wk"), "MAX": ("max", "1mo")}
GP_OFFSETS = {"1M": pd.DateOffset(months=1), "3M": pd.DateOffset(months=3), "6M": pd.DateOffset(months=6),
              "1Y": pd.DateOffset(years=1), "2Y": pd.DateOffset(years=2), "5Y": pd.DateOffset(years=5)}


def gp(sym, rng="1Y"):
    """Price history with technical studies. Fetches extra look-back so long moving
    averages are populated from the first visible bar."""
    sym = _sym(sym)
    rng = rng if rng in GP_SPEC else "1Y"
    period, interval = GP_SPEC[rng]

    def load():
        h = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=False)
        h = h.dropna(subset=["Close"])
        if h.empty:
            raise ValueError(f"No price data for '{sym}'")
        c = h["Close"]
        mid = c.rolling(20).mean()
        sd = c.rolling(20).std()
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rsi = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))
        macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        sig = macd.ewm(span=9, adjust=False).mean()
        st = pd.DataFrame({
            "open": h["Open"], "high": h["High"], "low": h["Low"], "close": c,
            "volume": h["Volume"] if "Volume" in h else 0,
            "sma20": mid, "sma50": c.rolling(50).mean(), "sma200": c.rolling(200).mean(),
            "bbu": mid + 2 * sd, "bbl": mid - 2 * sd, "rsi": rsi,
            "macd": macd, "signal": sig, "hist": macd - sig,
        })
        idx = st.index
        prev_close = None
        if rng == "1D":
            last_day = idx[-1].date()
            mask = [d == last_day for d in idx.date]
            before = st[[not m for m in mask]]
            prev_close = _f(before["close"].iloc[-1]) if len(before) else None
        elif rng == "5D":
            days = set(sorted(set(idx.date))[-5:])
            mask = [d in days for d in idx.date]
        elif rng == "YTD":
            mask = idx.year == idx[-1].year
        elif rng == "MAX":
            mask = [True] * len(idx)
        else:
            mask = idx >= (idx[-1] - GP_OFFSETS[rng])
        st = st[mask]
        fmt = "%m-%d %H:%M" if interval.endswith("m") else "%Y-%m-%d"
        out = {"symbol": sym, "range": rng, "interval": interval, "prev_close": prev_close,
               "labels": [i.strftime(fmt) for i in st.index]}
        for col in st.columns:
            nd = 0 if col == "volume" else 4
            out[col] = [_f(x, nd) for x in st[col]]
        return out

    return CACHE.get(f"gp:{sym}:{rng}", 60 if rng in ("1D", "5D") else 300, load)


# =========================================================================== FA: financial statements
FA_ROWS = {
    "income": [
        ("Total Revenue", "Revenue", "money"), ("Cost Of Revenue", "Cost of Revenue", "money"),
        ("Gross Profit", "Gross Profit", "money"), ("Research And Development", "R&D", "money"),
        ("Selling General And Administration", "SG&A", "money"), ("Operating Expense", "Operating Expenses", "money"),
        ("Operating Income", "Operating Income", "money"), ("EBITDA", "EBITDA", "money"),
        ("Interest Expense", "Interest Expense", "money"), ("Pretax Income", "Pretax Income", "money"),
        ("Tax Provision", "Income Tax", "money"), ("Net Income", "Net Income", "money"),
        ("Diluted EPS", "Diluted EPS", "eps"), ("Diluted Average Shares", "Diluted Shares", "shares")],
    "balance": [
        ("Cash And Cash Equivalents", "Cash & Equivalents", "money"),
        ("Cash Cash Equivalents And Short Term Investments", "Cash + ST Investments", "money"),
        ("Accounts Receivable", "Receivables", "money"), ("Inventory", "Inventory", "money"),
        ("Current Assets", "Total Current Assets", "money"), ("Net PPE", "Net PP&E", "money"),
        ("Goodwill And Other Intangible Assets", "Goodwill & Intangibles", "money"),
        ("Total Assets", "Total Assets", "money"), ("Accounts Payable", "Accounts Payable", "money"),
        ("Current Debt", "Short-Term Debt", "money"), ("Current Liabilities", "Total Current Liabilities", "money"),
        ("Long Term Debt", "Long-Term Debt", "money"), ("Total Debt", "Total Debt", "money"),
        ("Net Debt", "Net Debt", "money"),
        ("Total Liabilities Net Minority Interest", "Total Liabilities", "money"),
        ("Stockholders Equity", "Shareholders' Equity", "money"), ("Retained Earnings", "Retained Earnings", "money"),
        ("Working Capital", "Working Capital", "money"), ("Ordinary Shares Number", "Shares Outstanding", "shares")],
    "cash": [
        ("Operating Cash Flow", "Operating Cash Flow", "money"), ("Capital Expenditure", "Capital Expenditure", "money"),
        ("Free Cash Flow", "Free Cash Flow", "money"), ("Depreciation And Amortization", "D&A", "money"),
        ("Stock Based Compensation", "Stock-Based Comp", "money"),
        ("Change In Working Capital", "Change in Working Capital", "money"),
        ("Investing Cash Flow", "Investing Cash Flow", "money"), ("Purchase Of Business", "Acquisitions", "money"),
        ("Financing Cash Flow", "Financing Cash Flow", "money"), ("Repurchase Of Capital Stock", "Buybacks", "money"),
        ("Cash Dividends Paid", "Dividends Paid", "money"), ("Net Issuance Payments Of Debt", "Net Debt Issuance", "money"),
        ("Changes In Cash", "Net Change in Cash", "money")],
}
FA_ATTR = {"annual": {"income": "income_stmt", "balance": "balance_sheet", "cash": "cash_flow"},
           "quarterly": {"income": "quarterly_income_stmt", "balance": "quarterly_balance_sheet",
                         "cash": "quarterly_cash_flow"}}


def _fa_cols(df):
    if df is None or df.empty:
        return []
    counts = {c: int(df[c].notna().sum()) for c in df.columns}
    best = max(counts.values()) if counts else 0
    # drop sparse periods (Yahoo often returns a near-empty oldest year)
    return sorted(c for c, n in counts.items() if n >= 3 and n >= 0.4 * best)


def _fa_rows(df, spec, cols):
    if df is None or df.empty or not cols:
        return []
    rows = []
    for key, label, kind in spec:
        if key in df.index:
            vals = [_f(df.loc[key, c] if not isinstance(df.loc[key, c], pd.Series) else df.loc[key, c].iloc[0])
                    for c in cols]
            if any(v is not None for v in vals):
                rows.append({"name": label, "kind": kind, "values": vals})
    if len(rows) < 4:  # non-standard layout (banks, insurers): show every line item
        rows = []
        for key in df.index:
            vals = [_f(df.loc[key, c]) if not isinstance(df.loc[key, c], pd.Series) else None for c in cols]
            if any(v is not None for v in vals):
                rows.append({"name": str(key), "kind": "eps" if "EPS" in str(key) else "money", "values": vals})
    return rows


def _fa_ratios(o, quarterly):
    per = o["income"]["periods"]

    def g(st, name):
        return next((r["values"] for r in o[st]["rows"] if r["name"] == name), None)

    def aligned(st, name):
        vals = g(st, name)
        if not vals:
            return None
        m = dict(zip(o[st]["periods"], vals))
        return [m.get(p) for p in per]

    rev = g("income", "Revenue")
    if not rev or not per:
        return {"periods": per, "rows": []}

    def margin(vals):
        if not vals:
            return None
        return [_f(v / r * 100, 2) if v is not None and r else None for v, r in zip(vals, rev)]

    lag = 4 if quarterly else 1

    def growth(vals):
        if not vals:
            return None
        out = []
        for i in range(len(vals)):
            a, b = vals[i], vals[i - lag] if i >= lag else None
            out.append(_f((a / b - 1) * 100, 2) if a is not None and b not in (None, 0) and b > 0 else None)
        return out

    def ratio(num, den, mult=1.0):
        if not num or not den:
            return None
        return [_f(a / b * mult, 2) if a is not None and b not in (None, 0) else None for a, b in zip(num, den)]

    sfx = " (YoY)" if quarterly else ""
    rows = [
        ("Revenue Growth" + sfx, growth(rev), "pct"),
        ("Gross Margin", margin(g("income", "Gross Profit")), "pct"),
        ("Operating Margin", margin(g("income", "Operating Income")), "pct"),
        ("EBITDA Margin", margin(g("income", "EBITDA")), "pct"),
        ("Net Margin", margin(g("income", "Net Income")), "pct"),
        ("FCF Margin", margin(aligned("cash", "Free Cash Flow")), "pct"),
        ("EPS Growth" + sfx, growth(g("income", "Diluted EPS")), "pct"),
    ]
    if not quarterly:
        eq = aligned("balance", "Shareholders' Equity")
        rows += [
            ("Return on Equity", ratio(g("income", "Net Income"), eq, 100), "pct"),
            ("Debt / Equity", ratio(aligned("balance", "Total Debt"), eq), "x"),
            ("Net Debt / EBITDA", ratio(aligned("balance", "Net Debt"), g("income", "EBITDA")), "x"),
        ]
    return {"periods": per, "rows": [{"name": n, "kind": k, "values": v} for n, v, k in rows
                                     if v and any(x is not None for x in v)]}


def fa(sym, period="annual"):
    sym = _sym(sym)
    period = "quarterly" if period == "quarterly" else "annual"

    def load():
        t = yf.Ticker(sym)
        out = {"symbol": sym, "period": period}
        for name, attr in FA_ATTR[period].items():
            try:
                df = getattr(t, attr)
            except Exception:  # noqa: BLE001
                df = None
            cols = _fa_cols(df)
            out[name] = {"periods": [c.strftime("%Y-%m-%d") for c in cols], "rows": _fa_rows(df, FA_ROWS[name], cols)}
        if not any(out[k]["rows"] for k in ("income", "balance", "cash")):
            raise ValueError(f"No financial statements available for {sym}")
        out["ratios"] = _fa_ratios(out, period == "quarterly")
        return out

    return CACHE.get(f"fa:{sym}:{period}", 3600, load)


# =========================================================================== ERN: earnings & estimates
PERIOD_NAMES = {"0q": "Current Qtr", "+1q": "Next Qtr", "0y": "Current FY", "+1y": "Next FY"}


def ern(sym):
    sym = _sym(sym)

    def load():
        t = yf.Ticker(sym)

        def hist():
            df = t.get_earnings_dates(limit=24)
            if df is None or df.empty:
                return []
            return [{"date": i.strftime("%Y-%m-%d"), "time": i.strftime("%H:%M"),
                     "est": _f(r.get("EPS Estimate")), "act": _f(r.get("Reported EPS")),
                     "surprise": _f(r.get("Surprise(%)"), 2)} for i, r in df.iterrows()]

        def est(attr):
            rows = _frame_rows(getattr(t, attr))
            for r in rows:
                r["label"] = PERIOD_NAMES.get(r["period"], r["period"])
            return rows

        def cal():
            c = t.calendar or {}
            return {"next": [_date(d) for d in (c.get("Earnings Date") or [])],
                    "eps_avg": _f(c.get("Earnings Average")), "eps_low": _f(c.get("Earnings Low")),
                    "eps_high": _f(c.get("Earnings High")), "rev_avg": _f(c.get("Revenue Average")),
                    "rev_low": _f(c.get("Revenue Low")), "rev_high": _f(c.get("Revenue High")),
                    "ex_div": _date(c.get("Ex-Dividend Date")), "div_date": _date(c.get("Dividend Date"))}

        return {"symbol": sym, "history": safe(hist), "eps_est": safe(est, "earnings_estimate"),
                "rev_est": safe(est, "revenue_estimate"), "trend": safe(est, "eps_trend"),
                "revisions": safe(est, "eps_revisions"), "growth": safe(est, "growth_estimates"),
                "calendar": safe(cal)}

    return CACHE.get("ern:" + sym, 1800, load)


# =========================================================================== ANR: analyst recommendations
def anr(sym):
    sym = _sym(sym)

    def load():
        t = yf.Ticker(sym)

        def recs():
            df = t.recommendations
            if df is None or df.empty:
                return []
            keys = ("strongBuy", "buy", "hold", "sell", "strongSell")
            return [{"period": r["period"], **{k: int(r[k]) for k in keys if k in r and pd.notna(r[k])}}
                    for _, r in df.iterrows()]

        def actions():
            df = t.upgrades_downgrades
            if df is None or df.empty:
                return []
            df = df.sort_index(ascending=False).head(80)
            return [{"date": i.strftime("%Y-%m-%d"), "firm": _str(r.get("Firm")), "to": _str(r.get("ToGrade")),
                     "from": _str(r.get("FromGrade")), "action": _str(r.get("Action")),
                     "pt_action": _str(r.get("priceTargetAction")), "pt": _f(r.get("currentPriceTarget")),
                     "pt_prior": _f(r.get("priorPriceTarget"))} for i, r in df.iterrows()]

        i = _info(sym)
        return {"symbol": sym,
                "targets": safe(lambda: {k: _f(v) for k, v in (t.analyst_price_targets or {}).items()}),
                "recs": safe(recs), "actions": safe(actions),
                "rating_mean": _f(i.get("recommendationMean"), 2), "rating_key": i.get("recommendationKey"),
                "n": i.get("numberOfAnalystOpinions")}

    return CACHE.get("anr:" + sym, 1800, load)


# =========================================================================== OMON: options monitor
def options(sym, exp=None):
    sym = _sym(sym)
    exps = CACHE.get("optexp:" + sym, 900, lambda: list(yf.Ticker(sym).options or []))
    if not exps:
        raise ValueError(f"No listed options for {sym}")
    if exp not in exps:
        today = dt.date.today()
        exp = next((e for e in exps if (dt.date.fromisoformat(e) - today).days >= 5), exps[0])

    def load():
        t = yf.Ticker(sym)
        ch = t.option_chain(exp)
        spot = _f((getattr(ch, "underlying", None) or {}).get("regularMarketPrice"))
        if spot is None:
            try:
                spot = _f(t.fast_info.get("lastPrice"))
            except Exception:  # noqa: BLE001
                spot = None

        def side(df):
            m = {}
            for _, r in df.iterrows():
                iv = r.get("impliedVolatility")
                m[float(r["strike"])] = {
                    "bid": _f(r.get("bid")), "ask": _f(r.get("ask")), "last": _f(r.get("lastPrice")),
                    "chg": _f(r.get("change")), "vol": _f(r.get("volume"), 0), "oi": _f(r.get("openInterest"), 0),
                    "iv": _f(iv * 100, 2) if iv is not None and pd.notna(iv) else None,
                    "itm": bool(r.get("inTheMoney"))}
            return m

        calls, puts = side(ch.calls), side(ch.puts)
        strikes = sorted(set(calls) | set(puts))

        def tot(m, f):
            return sum((v.get(f) or 0) for v in m.values())

        def mid(o):
            if not o:
                return None
            if o["bid"] and o["ask"] and o["ask"] >= o["bid"] > 0:
                return (o["bid"] + o["ask"]) / 2
            return o["last"]

        atm = min(strikes, key=lambda k: abs(k - spot)) if spot and strikes else None
        c_atm, p_atm = calls.get(atm), puts.get(atm)
        straddle = (mid(c_atm) or 0) + (mid(p_atm) or 0) if (mid(c_atm) or mid(p_atm)) else None
        ivs = [o["iv"] for o in (c_atm, p_atm) if o and o["iv"]]
        # max pain: strike that minimises total intrinsic value paid to option holders
        max_pain = None
        if strikes:
            def pain(s):
                return (sum(max(0.0, s - k) * (calls[k]["oi"] or 0) for k in calls)
                        + sum(max(0.0, k - s) * (puts[k]["oi"] or 0) for k in puts))
            max_pain = min(strikes, key=pain)
        smile = []
        if spot:
            for k in strikes:
                o = puts.get(k) if k < spot else calls.get(k)
                if o and o["iv"] and o["iv"] > 1 and (o["bid"] or 0) > 0 and abs(k / spot - 1) < 0.4:
                    smile.append({"strike": k, "iv": o["iv"]})
        cv, pv, co, po = tot(calls, "vol"), tot(puts, "vol"), tot(calls, "oi"), tot(puts, "oi")
        return {
            "symbol": sym, "expiry": exp, "expiries": exps, "spot": spot, "atm": atm,
            "dte": (dt.date.fromisoformat(exp) - dt.date.today()).days,
            "rows": [{"strike": k, "c": calls.get(k), "p": puts.get(k)} for k in strikes],
            "summary": {"call_vol": cv, "put_vol": pv, "call_oi": co, "put_oi": po,
                        "pc_vol": _f(pv / cv, 2) if cv else None, "pc_oi": _f(po / co, 2) if co else None,
                        "atm_iv": _f(sum(ivs) / len(ivs), 2) if ivs else None,
                        "straddle": _f(straddle, 2), "move_pct": _f(straddle / spot * 100, 2) if straddle and spot else None,
                        "max_pain": max_pain},
            "smile": smile,
        }

    return CACHE.get(f"opt:{sym}:{exp}", 120, load)


# =========================================================================== HDS: holders
def _holders(df):
    if df is None or df.empty:
        return []
    return [{"date": _date(r.get("Date Reported")), "holder": _str(r.get("Holder")), "pct": _f(r.get("pctHeld"), 5),
             "shares": _f(r.get("Shares"), 0), "value": _f(r.get("Value"), 0), "chg": _f(r.get("pctChange"), 5)}
            for _, r in df.iterrows()]


def hds(sym):
    sym = _sym(sym)

    def load():
        t = yf.Ticker(sym)
        qt = (_info(sym).get("quoteType") or "").upper()

        def major():
            df = t.major_holders
            return {} if df is None or df.empty else {str(i): _f(r.iloc[0], 5) for i, r in df.iterrows()}

        def insider():
            df = t.insider_transactions
            if df is None or df.empty:
                return []
            return [{"date": _date(r.get("Start Date")), "insider": _str(r.get("Insider")),
                     "position": _str(r.get("Position")), "text": _str(r.get("Text")) or _str(r.get("Transaction")),
                     "shares": _f(r.get("Shares"), 0), "value": _f(r.get("Value"), 0),
                     "own": _str(r.get("Ownership"))} for _, r in df.head(60).iterrows()]

        def insider_sum():
            df = t.insider_purchases
            if df is None or df.empty:
                return []
            cols = list(df.columns)
            return [{"label": _str(r[cols[0]]), "shares": _f(r[cols[1]], 0) if len(cols) > 1 else None,
                     "trans": _f(r[cols[2]], 0) if len(cols) > 2 else None} for _, r in df.iterrows()]

        def fund():
            fd = t.funds_data
            top = []
            try:
                th = fd.top_holdings
                top = [{"sym": str(i), "name": _str(r.get("Name")), "pct": _f(r.get("Holding Percent"), 5)}
                       for i, r in th.iterrows()]
            except Exception:  # noqa: BLE001
                pass
            sw = {}
            try:
                sw = {k: _f(v, 5) for k, v in (fd.sector_weightings or {}).items()}
            except Exception:  # noqa: BLE001
                pass
            ac = {}
            try:
                ac = {k: _f(v, 5) for k, v in (fd.asset_classes or {}).items()}
            except Exception:  # noqa: BLE001
                pass
            return {"top": top, "sectors": sw, "assets": ac}

        out = {"symbol": sym, "quoteType": qt}
        if qt in ("ETF", "MUTUALFUND"):
            out["fund"] = safe(fund)
        out.update(major=safe(major), inst=safe(lambda: _holders(t.institutional_holders)),
                   funds=safe(lambda: _holders(t.mutualfund_holders)), insider=safe(insider),
                   insider_sum=safe(insider_sum))
        return out

    return CACHE.get("hds:" + sym, 3600, load)


# =========================================================================== DVD: dividends
def dvd(sym):
    sym = _sym(sym)

    def load():
        t = yf.Ticker(sym)
        s = t.dividends
        hist, annual = [], []
        if s is not None and len(s):
            s = s[s > 0]
            hist = [{"date": i.strftime("%Y-%m-%d"), "amt": _f(v, 4)} for i, v in s.items()][::-1]
            by = s.groupby(s.index.year)
            annual = [{"year": int(y), "total": _f(g.sum(), 4), "count": int(g.count())} for y, g in by]
        this_year = dt.date.today().year
        full = [a for a in annual if a["year"] < this_year]

        def cagr(n):
            if len(full) <= n:
                return None
            a, b = full[-1]["total"], full[-1 - n]["total"]
            return _f(((a / b) ** (1 / n) - 1) * 100, 2) if a and b else None

        streak = 0  # consecutive years of annual dividend increases
        for i in range(len(full) - 1, 0, -1):
            if full[i]["total"] and full[i - 1]["total"] and full[i]["total"] > full[i - 1]["total"] * 1.0001:
                streak += 1
            else:
                break
        splits = []
        try:
            sp = t.splits
            splits = [{"date": i.strftime("%Y-%m-%d"), "ratio": _f(v, 4)} for i, v in sp.items() if v][::-1]
        except Exception:  # noqa: BLE001
            pass
        i = _info(sym)
        cal = safe(lambda: t.calendar or {})
        cal = cal if isinstance(cal, dict) and "error" not in cal else {}

        def epoch_date(v):
            try:
                return dt.datetime.fromtimestamp(int(v), dt.timezone.utc).strftime("%Y-%m-%d") if v else None
            except (TypeError, ValueError, OSError):
                return None

        return {
            "symbol": sym, "history": hist, "annual": annual, "splits": splits,
            "stats": {
                "rate": _f(i.get("dividendRate")), "yield": _f(i.get("dividendYield")),
                "trail_rate": _f(i.get("trailingAnnualDividendRate")),
                "trail_yield": _f((i.get("trailingAnnualDividendYield") or 0) * 100) if i.get("trailingAnnualDividendYield") else None,
                "payout": _f(i.get("payoutRatio")), "avg5y": _f(i.get("fiveYearAvgDividendYield")),
                "last": _f(i.get("lastDividendValue")),
                "ex_date": _date(cal.get("Ex-Dividend Date")) or epoch_date(i.get("exDividendDate")),
                "pay_date": _date(cal.get("Dividend Date")),
                "freq": full[-1]["count"] if full else None,
                "g1": cagr(1), "g3": cagr(3), "g5": cagr(5), "g10": cagr(10), "streak": streak,
            },
        }

    return CACHE.get("dvd:" + sym, 3600, load)


# =========================================================================== RV: relative value / peers
RV_FIELDS = ["marketCap", "enterpriseValue", "trailingPE", "forwardPE", "priceToBook", "priceToSalesTrailing12Months",
             "enterpriseToEbitda", "trailingPegRatio", "grossMargins", "operatingMargins", "profitMargins",
             "returnOnEquity", "revenueGrowth", "earningsGrowth", "dividendYield", "beta", "debtToEquity",
             "recommendationMean"]


def rv(sym, extra=""):
    sym = _sym(sym)
    extra = [_sym(x) for x in (extra or "").split(",") if x.strip()]
    i = _info(sym)
    key = i.get("industryKey")
    peers = []
    if key:
        peers = safe(CACHE.get, "ind:" + key, 86400, lambda: [str(s) for s in yf.Industry(key).top_companies.index[:15]])
        peers = peers if isinstance(peers, list) else []

    syms = list(dict.fromkeys([sym] + extra + [p for p in peers if p != sym]))[:22]

    def load():
        futs = {s: POOL.submit(_info, s) for s in syms}
        infos = {s: f.result() for s, f in futs.items()}
        q = safe(quotes, syms)
        q = q if isinstance(q, dict) and "error" not in q else {}
        rows = []
        for s in syms:
            inf = infos.get(s) or {}
            if s != sym and s not in extra and not inf.get("marketCap"):
                continue
            qd = q.get(s, {})
            last = qd.get("last") or inf.get("regularMarketPrice") or inf.get("currentPrice")

            def ch(base):
                return _f((last / base - 1) * 100, 2) if last and base else None

            row = {"sym": s, "name": inf.get("shortName") or inf.get("longName") or s,
                   "subject": s == sym, "custom": s in extra, "last": _f(last),
                   "chg": ch(qd.get("prev")), "ytd": ch(qd.get("ytd")), "y1": ch(qd.get("y1"))}
            for k in RV_FIELDS:
                row[k] = _f(inf.get(k))
            rows.append(row)
        rows.sort(key=lambda r: (not r["subject"], -(r["marketCap"] or 0)))
        return {"symbol": sym, "industry": i.get("industry"), "sector": i.get("sector"), "rows": rows}

    return CACHE.get(f"rv:{','.join(syms)}", 900, load)


# =========================================================================== COMP: multi-security comparison
COMP_RANGES = {"1M": ("1mo", "1d"), "3M": ("3mo", "1d"), "6M": ("6mo", "1d"), "YTD": ("ytd", "1d"),
               "1Y": ("1y", "1d"), "2Y": ("2y", "1d"), "5Y": ("5y", "1wk"), "10Y": ("10y", "1wk")}


def comp(symbols, rng="1Y"):
    syms = list(dict.fromkeys(_sym(s) for s in symbols if s and s.strip()))[:10]
    if not syms:
        raise ValueError("Enter at least one ticker")
    rng = rng if rng in COMP_RANGES else "1Y"
    period, interval = COMP_RANGES[rng]

    def load():
        df = yf.download(syms, period=period, interval=interval, progress=False, auto_adjust=True,
                         group_by="column", threads=True)
        close = df["Close"]
        if not hasattr(close, "columns"):
            close = close.to_frame(syms[0])
        avail = [s for s in syms if s in close.columns and close[s].notna().sum() > 2]
        if not avail:
            raise ValueError("No price history for: " + ", ".join(syms))
        close = close[avail]
        base = avail[0]
        # Align to the first security's trading calendar (e.g. crypto trades weekends).
        close = close[close[base].notna()].ffill().dropna()
        if len(close) < 3:
            raise ValueError("Not enough overlapping history")
        reb = close / close.iloc[0] * 100
        rets = close.pct_change().dropna()
        ann = 252 if interval == "1d" else 52
        yrs = max((close.index[-1] - close.index[0]).days / 365.25, 1e-9)
        bvar = rets[base].var()
        stats = []
        for s in avail:
            r, px = rets[s], close[s]
            sd = r.std()
            stats.append({
                "sym": s, "last": _f(px.iloc[-1]), "ret": _f((px.iloc[-1] / px.iloc[0] - 1) * 100, 2),
                "cagr": _f(((px.iloc[-1] / px.iloc[0]) ** (1 / yrs) - 1) * 100, 2) if yrs >= 0.95 else None,
                "vol": _f(sd * math.sqrt(ann) * 100, 2),
                "mdd": _f((px / px.cummax() - 1).min() * 100, 2),
                "sharpe": _f(r.mean() * ann / (sd * math.sqrt(ann)), 2) if sd else None,
                "beta": _f(r.cov(rets[base]) / bvar, 2) if bvar else None,
                "corr": _f(r.corr(rets[base]), 2),
                "hi": _f(px.max()), "lo": _f(px.min()),
            })
        cm = rets.corr()
        fmt = "%Y-%m-%d"
        return {"symbols": avail, "missing": [s for s in syms if s not in avail], "range": rng,
                "labels": [i.strftime(fmt) for i in close.index],
                "series": {s: [_f(x, 3) for x in reb[s]] for s in avail},
                "stats": stats, "corr": [[_f(cm.loc[a, b], 2) for b in avail] for a in avail]}

    return CACHE.get(f"comp:{','.join(syms)}:{rng}", 300, load)


# =========================================================================== EVTS: calendars
def _pages(fn, pages=3, size=100, **kw):
    frames = []
    for n in range(pages):
        df = fn(limit=size, offset=n * size, force=True, **kw)
        if df is None or df.empty:
            break
        frames.append(df)
        if len(df) < size:
            break
    return pd.concat(frames) if frames else pd.DataFrame()


def _ts(x):
    try:
        return int(pd.Timestamp(x).timestamp()) if x is not None and not pd.isna(x) else None
    except (TypeError, ValueError):
        return None


def calendar(days=14):
    days = max(1, min(int(days), 45))

    def load():
        start = dt.date.today()
        end = start + dt.timedelta(days=days)
        c = yf.Calendars(start=start, end=end)

        def earn():
            df = _pages(c.get_earnings_calendar, market_cap=2e9, filter_most_active=False)
            rows = [{"sym": str(i), "company": _str(r.get("Company")), "mcap": _f(r.get("Marketcap"), 0),
                     "event": _str(r.get("Event Name")), "ts": _ts(r.get("Event Start Date")),
                     "timing": _str(r.get("Timing")), "est": _f(r.get("EPS Estimate")),
                     "act": _f(r.get("Reported EPS")), "surprise": _f(r.get("Surprise(%)"), 2)}
                    for i, r in df.iterrows()]
            # OTC foreign ordinaries (5 letters ending in F) duplicate the company's ADR line - drop them
            rows = [r for r in rows if not (len(r["sym"]) == 5 and r["sym"].endswith("F") and r["sym"].isalpha())]
            seen, out = set(), []
            for r in sorted(rows, key=lambda r: (r["ts"] or 0, -(r["mcap"] or 0))):
                if (r["sym"], r["ts"]) not in seen:
                    seen.add((r["sym"], r["ts"]))
                    out.append(r)
            return out

        def econ():
            df = _pages(c.get_economic_events_calendar, pages=5)
            rows = [{"event": str(i).rstrip("* ").strip(), "region": _str(r.get("Region")), "ts": _ts(r.get("Event Time")),
                     "for": _str(r.get("For")), "actual": _f(r.get("Actual")), "expected": _f(r.get("Expected")),
                     "last": _f(r.get("Last")), "revised": _f(r.get("Revised"))} for i, r in df.iterrows()]
            return sorted(rows, key=lambda r: r["ts"] or 0)

        def ipo():
            df = _pages(c.get_ipo_info_calendar, pages=1)
            return [{"sym": str(i), "company": _str(r.get("Company")), "exchange": _str(r.get("Exchange")),
                     "date": _date(r.get("Date")), "lo": _f(r.get("Price From")), "hi": _f(r.get("Price To")),
                     "price": _f(r.get("Price")), "shares": _f(r.get("Shares"), 0), "action": _str(r.get("Action"))}
                    for i, r in df.iterrows()]

        return {"start": start.isoformat(), "end": end.isoformat(), "days": days,
                "earnings": safe(earn), "econ": safe(econ), "ipo": safe(ipo)}

    return CACHE.get(f"cal:{days}", 1800, load)


def next_earnings(symbols):
    syms = list(dict.fromkeys(_sym(s) for s in symbols if s.strip()))[:40]

    def one(s):
        def load():
            c = yf.Ticker(s).calendar or {}
            ed = c.get("Earnings Date") or []
            return {"sym": s, "date": _date(ed[0]) if ed else None, "eps": _f(c.get("Earnings Average")),
                    "rev": _f(c.get("Revenue Average")), "ex_div": _date(c.get("Ex-Dividend Date"))}
        try:
            return CACHE.get("nexte:" + s, 21600, load)
        except Exception:  # noqa: BLE001
            return {"sym": s, "date": None}

    futs = [POOL.submit(one, s) for s in syms]
    rows = [f.result() for f in futs]
    return sorted(rows, key=lambda r: r.get("date") or "9999")


# =========================================================================== EXT: pre-market / after-hours
EXT_GROUPS = [
    ("Index Futures (trade ~23h)", ["ES=F", "NQ=F", "YM=F", "RTY=F", "ZN=F", "CL=F", "GC=F", "BTC-USD"]),
    ("Index & Sector ETFs", ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "SMH", "TLT", "HYG"]),
    ("Mega Caps", ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "BRK-B", "JPM", "LLY", "V",
                   "NFLX", "AMD", "PLTR", "COST", "WMT", "XOM", "ORCL", "UNH"]),
]


def _yq(symbols):
    """Yahoo v7 quote: includes pre/post-market fields and market state."""
    from yfinance.data import YfData  # noqa: PLC0415
    out = {}
    for k in range(0, len(symbols), 50):
        chunk = symbols[k:k + 50]
        j = YfData().get_raw_json("https://query1.finance.yahoo.com/v7/finance/quote",
                                  params={"symbols": ",".join(chunk), "formatted": "false"})
        for r in (j.get("quoteResponse") or {}).get("result") or []:
            out[r["symbol"]] = r
    return out


def _ext_row(r):
    st = r.get("marketState") or ""
    reg, prev = _f(r.get("regularMarketPrice")), _f(r.get("regularMarketPreviousClose"))
    pre, pre_c = _f(r.get("preMarketPrice")), _f(r.get("preMarketChangePercent"), 3)
    post, post_c = _f(r.get("postMarketPrice")), _f(r.get("postMarketChangePercent"), 3)
    reg_c = _f(r.get("regularMarketChangePercent"), 3)
    # the "live" quote: whichever session is current
    if st == "PRE" and pre:
        live, live_c, sess = pre, pre_c, "PRE"
    elif st in ("POST", "POSTPOST", "CLOSED", "PREPRE") and post:
        live, live_c, sess = post, post_c, "POST"
    else:
        live, live_c, sess = reg, reg_c, "REG"
    return {"sym": r.get("symbol"), "name": r.get("shortName") or r.get("longName") or r.get("symbol"),
            "state": st, "type": r.get("quoteType"), "reg": reg, "prev": prev, "reg_chg": reg_c,
            "pre": pre, "pre_chg": pre_c, "pre_time": r.get("preMarketTime"),
            "post": post, "post_chg": post_c, "post_time": r.get("postMarketTime"),
            "live": live, "live_chg": live_c, "session": sess,
            "vol": _f(r.get("regularMarketVolume"), 0), "mcap": _f(r.get("marketCap"), 0),
            "earn_ts": r.get("earningsTimestamp")}


def ext_quotes(symbols):
    syms = list(dict.fromkeys(_sym(s) for s in symbols if s and s.strip()))[:150]
    if not syms:
        return {}
    q = CACHE.get("yq:" + ",".join(sorted(syms)), 30, lambda: _yq(syms))
    return {s: _ext_row(q[s]) for s in syms if s in q}


def ext_page(watch=""):
    wl = [s for s in (watch or "").split(",") if s.strip()]
    allsyms = [s for _, g in EXT_GROUPS for s in g] + wl
    q = ext_quotes(allsyms)
    groups = [{"title": t, "rows": [q[s] for s in g if s in q]} for t, g in EXT_GROUPS]
    if wl:
        groups.append({"title": "My Watchlist", "rows": [q[_sym(s)] for s in wl if _sym(s) in q]})

    def movers():
        uni = []
        for off in (0, 250):
            r = yf.screen(Q("and", [Q("eq", ["region", "us"]), Q("gte", ["intradaymarketcap", 2e9])]),
                          size=250, offset=off, sortField="intradaymarketcap", sortAsc=False)
            uni += [x["symbol"] for x in r.get("quotes", [])]
        for p in ("most_actives", "day_gainers", "day_losers"):
            try:
                uni += [x["symbol"] for x in yf.screen(p, count=100).get("quotes", [])]
            except Exception:  # noqa: BLE001
                pass
        return list(dict.fromkeys(uni))

    uni = safe(CACHE.get, "ext:universe", 1800, movers)
    mv = {}
    if isinstance(uni, list) and uni:
        qq = CACHE.get("ext:uq", 60, lambda: _yq(uni))
        rows = [_ext_row(v) for v in qq.values() if (v.get("quoteType") == "EQUITY")]
        for key, label in (("pre_chg", "pre"), ("post_chg", "post")):
            have = [x for x in rows if isinstance(x.get(key), (int, float)) and x.get(label)]
            have.sort(key=lambda x: x[key])
            mv[label] = {"up": have[::-1][:15], "down": have[:15], "n": len(have)}
    state = (q.get("SPY") or q.get("AAPL") or {}).get("state")
    return {"state": state, "groups": groups, "movers": mv, "universe": len(uni) if isinstance(uni, list) else 0}


def ext_chart(sym):
    sym = _sym(sym)

    def load():
        h = yf.Ticker(sym).history(period="5d", interval="5m", prepost=True, auto_adjust=False)
        h = h.dropna(subset=["Close"])
        if h.empty:
            raise ValueError(f"No intraday data for {sym}")
        sess = []
        for t in h.index:
            m = t.hour * 60 + t.minute
            sess.append("pre" if m < 570 else "post" if m >= 960 else "reg")
        return {"symbol": sym, "labels": [t.strftime("%a %H:%M") for t in h.index],
                "close": [_f(x) for x in h["Close"]], "volume": [_f(x, 0) for x in h["Volume"]], "session": sess}

    return CACHE.get("extc:" + sym, 60, load)


# =========================================================================== EQS: equity screener
EQS_PRESETS = {
    "undervalued_large_caps": "Undervalued Large Caps", "undervalued_growth_stocks": "Undervalued Growth",
    "growth_technology_stocks": "Growth Technology", "aggressive_small_caps": "Aggressive Small Caps",
    "small_cap_gainers": "Small Cap Gainers", "most_shorted_stocks": "Most Shorted",
    "day_gainers": "Day Gainers", "day_losers": "Day Losers", "most_actives": "Most Active",
}
EQS_SORTS = {"mcap": "intradaymarketcap", "chg": "percentchange", "pe": "peratio.lasttwelvemonths",
             "dy": "forward_dividend_yield", "vol": "dayvolume", "y1": "fiftytwowkpercentchange"}


def eqs(q):
    preset = q.get("preset") or ""

    def num(k):
        try:
            v = q.get(k)
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    def load():
        if preset in EQS_PRESETS:
            r = yf.screen(preset, count=100)
        else:
            parts = [Q("eq", ["region", (q.get("region") or "us").lower()])]
            if q.get("sector"):
                parts.append(Q("eq", ["sector", q["sector"]]))
            for k, field, op in (("mcap_min", "intradaymarketcap", "gte"), ("mcap_max", "intradaymarketcap", "lte"),
                                 ("pe_min", "peratio.lasttwelvemonths", "gte"),
                                 ("pe_max", "peratio.lasttwelvemonths", "lte"),
                                 ("dy_min", "forward_dividend_yield", "gte"),
                                 ("growth_min", "totalrevenues1yrgrowth.lasttwelvemonths", "gte"),
                                 ("chg_min", "percentchange", "gte"), ("chg_max", "percentchange", "lte"),
                                 ("price_min", "intradayprice", "gte"), ("beta_max", "beta", "lte")):
                v = num(k)
                if v is not None:
                    parts.append(Q(op, [field, v]))
            query = Q("and", parts) if len(parts) > 1 else parts[0]
            sort = EQS_SORTS.get(q.get("sort") or "mcap", "intradaymarketcap")
            r = yf.screen(query, size=100, sortField=sort, sortAsc=q.get("asc") == "1")
        rows = []
        for d in r.get("quotes", []):
            # OTC lines usually duplicate a primary listing (e.g. ASMLF vs ASML) - skip them
            if "OTC" in str(d.get("fullExchangeName") or d.get("exchange") or "").upper() or d.get("exchange") == "PNK":
                continue
            rows.append({
                "sym": d.get("symbol"), "name": d.get("shortName") or d.get("longName") or "",
                "last": _f(d.get("regularMarketPrice")), "chg": _f(d.get("regularMarketChangePercent"), 2),
                "mcap": _f(d.get("marketCap"), 0), "pe": _f(d.get("trailingPE"), 2), "fpe": _f(d.get("forwardPE"), 2),
                "pb": _f(d.get("priceToBook"), 2), "dy": _f(d.get("dividendYield"), 2),
                "y1": _f(d.get("fiftyTwoWeekChangePercent"), 2), "vol": _f(d.get("regularMarketVolume"), 0),
                "avgvol": _f(d.get("averageDailyVolume3Month"), 0), "rating": d.get("averageAnalystRating"),
                "exch": d.get("fullExchangeName") or d.get("exchange"),
            })
        return {"total": r.get("total"), "title": EQS_PRESETS.get(preset, "Custom Screen"), "rows": rows}

    key = "eqs:" + "&".join(f"{k}={v}" for k, v in sorted(q.items()))
    return CACHE.get(key, 300, load)
