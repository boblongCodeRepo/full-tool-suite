"""Data layer for Market Terminal.

Sources (all free, no API keys):
  * Yahoo Finance via yfinance  - equities, ETFs, indices, futures, FX, crypto, company data
  * FRED (St. Louis Fed)        - Treasury curve, ICE BofA credit spreads, macro indicators
  * RSS feeds                   - CNBC, MarketWatch, Yahoo Finance, Federal Reserve,
                                  Crunchbase News, Google News topic searches
"""
from __future__ import annotations

import calendar
import csv
import datetime as dt
import io
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

import os

import feedparser
import requests
import yfinance as yf

try:
    _yf_cache = os.path.join(os.environ.get("MT_DATA_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "MarketTerminal"), "yf-cache")
    os.makedirs(_yf_cache, exist_ok=True)
    yf.set_tz_cache_location(_yf_cache)
except Exception:  # noqa: BLE001
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "*/*"})
# FRED rejects spoofed browser user-agents; use a plain, honest client identity.
FRED_SESSION = requests.Session()
FRED_SESSION.headers.update({"User-Agent": "MarketTerminal/1.0 (python-requests)"})
POOL = ThreadPoolExecutor(max_workers=16)
FRED_GATE = threading.Semaphore(6)  # be polite to FRED


# --------------------------------------------------------------------------- cache
class TTLCache:
    def __init__(self):
        self._d: dict = {}
        self._locks: dict = {}
        self._g = threading.Lock()

    def get(self, key, ttl, fn):
        now = time.time()
        hit = self._d.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        with self._g:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            hit = self._d.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            try:
                val = fn()
            except Exception:
                if hit:  # serve stale data rather than nothing
                    return hit[1]
                raise
            self._d[key] = (time.time(), val)
            return val


CACHE = TTLCache()


def _f(x, nd=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


def safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- universes
OVERVIEW = [
    ("US Equity Indices", "px", [
        ("^GSPC", "S&P 500"), ("^DJI", "Dow Jones"), ("^IXIC", "Nasdaq Comp"),
        ("^RUT", "Russell 2000"), ("^VIX", "VIX Volatility")]),
    ("Global Equity Indices", "px", [
        ("^STOXX50E", "Euro Stoxx 50"), ("^FTSE", "FTSE 100"), ("^GDAXI", "DAX"),
        ("^FCHI", "CAC 40"), ("^N225", "Nikkei 225"), ("^HSI", "Hang Seng"),
        ("000001.SS", "Shanghai Comp"), ("^BSESN", "India Sensex")]),
    ("US Treasury Yields", "yld", [
        ("^IRX", "13-Week Bill"), ("^FVX", "5-Year Note"), ("^TNX", "10-Year Note"),
        ("^TYX", "30-Year Bond")]),
    ("Commodities", "px", [
        ("CL=F", "WTI Crude"), ("BZ=F", "Brent Crude"), ("NG=F", "Natural Gas"),
        ("GC=F", "Gold"), ("SI=F", "Silver"), ("HG=F", "Copper"), ("ZC=F", "Corn")]),
    ("Currencies", "px", [
        ("DX-Y.NYB", "US Dollar Index"), ("EURUSD=X", "EUR/USD"), ("JPY=X", "USD/JPY"),
        ("GBPUSD=X", "GBP/USD"), ("CNY=X", "USD/CNY"), ("CAD=X", "USD/CAD")]),
    ("Crypto", "px", [("BTC-USD", "Bitcoin"), ("ETH-USD", "Ether"), ("SOL-USD", "Solana")]),
]

EQUITIES = [
    ("Broad Market & Factors", "px", [
        ("SPY", "S&P 500 ETF"), ("QQQ", "Nasdaq 100 ETF"), ("IWM", "Russell 2000 ETF"),
        ("DIA", "Dow ETF"), ("RSP", "S&P 500 Equal Wt"), ("MTUM", "Momentum Factor"),
        ("VLUE", "Value Factor"), ("QUAL", "Quality Factor"), ("USMV", "Min Volatility")]),
    ("S&P Sectors", "px", [
        ("XLK", "Technology"), ("XLC", "Communication"), ("XLY", "Cons Discretionary"),
        ("XLF", "Financials"), ("XLV", "Health Care"), ("XLI", "Industrials"),
        ("XLE", "Energy"), ("XLB", "Materials"), ("XLP", "Cons Staples"),
        ("XLU", "Utilities"), ("XLRE", "Real Estate")]),
    ("Mega Caps", "px", [
        ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "Nvidia"), ("AMZN", "Amazon"),
        ("GOOGL", "Alphabet"), ("META", "Meta"), ("TSLA", "Tesla"), ("AVGO", "Broadcom"),
        ("BRK-B", "Berkshire"), ("JPM", "JPMorgan"), ("LLY", "Eli Lilly"), ("V", "Visa"),
        ("XOM", "Exxon"), ("WMT", "Walmart")]),
    ("International Equity", "px", [
        ("EFA", "Developed ex-US"), ("EEM", "Emerging Mkts"), ("VGK", "Europe"),
        ("EWJ", "Japan"), ("FXI", "China Large Cap"), ("INDA", "India"),
        ("EWZ", "Brazil"), ("EWU", "United Kingdom")]),
]

GOVT_ETFS = [("Treasury & Rates ETFs", "px", [
    ("BIL", "T-Bills 1-3M"), ("SHY", "Treasury 1-3Y"), ("IEI", "Treasury 3-7Y"),
    ("IEF", "Treasury 7-10Y"), ("TLT", "Treasury 20Y+"), ("TIP", "TIPS"),
    ("GOVT", "US Treasury Broad"), ("MUB", "Municipal Bonds"), ("BNDX", "Intl Govt (hedged)")])]

CORP_ETFS = [("Credit ETFs", "px", [
    ("LQD", "IG Corporate"), ("VCSH", "IG Short-Term"), ("VCIT", "IG Interm-Term"),
    ("VCLT", "IG Long-Term"), ("HYG", "High Yield"), ("JNK", "High Yield (SPDR)"),
    ("ANGL", "Fallen Angels"), ("BKLN", "Senior Loans"), ("EMB", "EM USD Sovereign"),
    ("CWB", "Convertibles"), ("AGG", "US Aggregate")])]

PRIVATE_EQ = [
    ("Alternative Asset Managers", "px", [
        ("BX", "Blackstone"), ("KKR", "KKR"), ("APO", "Apollo"), ("ARES", "Ares"),
        ("CG", "Carlyle"), ("TPG", "TPG"), ("BAM", "Brookfield AM"), ("OWL", "Blue Owl"),
        ("HLNE", "Hamilton Lane"), ("STEP", "StepStone")]),
    ("Private Credit / BDCs", "px", [
        ("BIZD", "BDC Index ETF"), ("ARCC", "Ares Capital"), ("BXSL", "Blackstone Sec Lend"),
        ("OBDC", "Blue Owl Capital"), ("FSK", "FS KKR"), ("MAIN", "Main Street"),
        ("HTGC", "Hercules (Venture)"), ("GBDC", "Golub BDC")]),
    ("Listed PE / Exit Market Proxies", "px", [
        ("PSP", "Listed Private Equity"), ("IPO", "Renaissance IPO ETF"),
        ("ARKK", "ARK Innovation (growth)"), ("XBI", "Biotech (VC-heavy)")]),
]

# --------------------------------------------------------------------------- yahoo quotes

def _dl(symbols, threads=True):
    df = yf.download(symbols, period="1y", interval="1d", progress=False,
                     auto_adjust=False, threads=threads, group_by="column")
    close = df["Close"]
    try:
        vol = df["Volume"]
    except KeyError:
        vol = None
    if not hasattr(close, "columns"):
        close = close.to_frame(symbols[0])
        vol = vol.to_frame(symbols[0]) if vol is not None else None
    return close, vol


def _download_closes(symbols):
    close, vol = _dl(symbols)
    # Retry any symbols that failed in the batch (transient Yahoo / cache-lock errors).
    missing = [s for s in symbols if s not in close.columns or close[s].dropna().empty]
    if missing:
        try:
            c2, v2 = _dl(missing, threads=False)
            for s in missing:
                if s in c2.columns and not c2[s].dropna().empty:
                    close[s] = c2[s]
                    if vol is not None and v2 is not None and s in v2.columns:
                        vol[s] = v2[s]
        except Exception:  # noqa: BLE001
            pass
    return close, vol


def quotes(symbols: list[str]) -> dict:
    symbols = list(dict.fromkeys(symbols))
    key = "q:" + ",".join(sorted(symbols))

    def load():
        close, vol = _download_closes(symbols)
        out = {}
        for s in symbols:
            if s not in close.columns:
                continue
            ser = close[s].dropna()
            if len(ser) < 2:
                continue
            last, prev = float(ser.iloc[-1]), float(ser.iloc[-2])

            def back(n):
                return float(ser.iloc[-1 - n]) if len(ser) > n else None

            yr = ser.index[-1].year
            prior = ser[ser.index.year < yr]
            ytd_base = float(prior.iloc[-1]) if len(prior) else float(ser.iloc[0])
            v = None
            if vol is not None and s in vol.columns:
                vs = vol[s].dropna()
                v = _f(vs.iloc[-1], 0) if len(vs) else None
            out[s] = {
                "last": _f(last), "prev": _f(prev),
                "w1": _f(back(5)), "m1": _f(back(21)), "ytd": _f(ytd_base),
                "y1": _f(float(ser.iloc[0])),
                "hi52": _f(ser.max()), "lo52": _f(ser.min()),
                "vol": v,
                "asof": ser.index[-1].strftime("%Y-%m-%d"),
                "spark": [_f(x, 3) for x in ser.iloc[-66:].tolist()],
            }
        return out

    return CACHE.get(key, 60, load)


def _rows(groups):
    syms = [s for _, _, items in groups for s, _ in items]
    q = quotes(syms)
    out = []
    for title, kind, items in groups:
        rows = []
        for s, name in items:
            d = q.get(s)
            if not d:
                rows.append({"sym": s, "name": name, "missing": True})
                continue
            rows.append({"sym": s, "name": name, "kind": kind, **d})
        out.append({"title": title, "kind": kind, "rows": rows})
    return out


def movers():
    def load():
        res = {}
        for key, label in (("day_gainers", "Top Gainers"), ("day_losers", "Top Losers"),
                           ("most_actives", "Most Active")):
            try:
                r = yf.screen(key, count=10)
                rows = []
                for qd in r.get("quotes", [])[:10]:
                    rows.append({
                        "sym": qd.get("symbol"),
                        "name": qd.get("shortName") or qd.get("longName") or "",
                        "last": _f(qd.get("regularMarketPrice")),
                        "chg": _f(qd.get("regularMarketChangePercent"), 2),
                        "vol": _f(qd.get("regularMarketVolume"), 0),
                        "mcap": _f(qd.get("marketCap"), 0),
                    })
                res[key] = {"title": label, "rows": rows}
            except Exception as e:  # noqa: BLE001
                res[key] = {"title": label, "rows": [], "error": str(e)}
        return res
    return CACHE.get("movers", 120, load)


# --------------------------------------------------------------------------- FRED

def fred(sid: str) -> list[tuple[str, float]]:
    def load():
        start = (dt.date.today() - dt.timedelta(days=365 * 12)).isoformat()
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
        with FRED_GATE:
            r = FRED_SESSION.get(url, timeout=25)
        r.raise_for_status()
        rd = csv.reader(io.StringIO(r.text))
        next(rd, None)
        out = []
        for row in rd:
            if len(row) < 2 or row[1] in (".", ""):
                continue
            try:
                out.append((row[0], float(row[1])))
            except ValueError:
                pass
        if not out:
            raise ValueError(f"no data for {sid}")
        return out
    return CACHE.get("fred:" + sid, 3600, load)


def fred_many(ids):
    futs = {i: POOL.submit(safe, fred, i) for i in ids}
    return {i: f.result() for i, f in futs.items()}


def _per_year(series):
    """Approximate observations per year from date spacing."""
    if len(series) < 3:
        return 12
    d0 = dt.date.fromisoformat(series[-3][0])
    d1 = dt.date.fromisoformat(series[-1][0])
    gap = (d1 - d0).days / 2
    if gap > 80:
        return 4
    if gap > 20:
        return 12
    if gap > 5:
        return 52
    return 252


def transform(series, tf):
    if tf == "lvl" or not series:
        return series
    n = _per_year(series)
    out = []
    for i in range(len(series)):
        d, v = series[i]
        if tf == "yoy":
            if i - n < 0:
                continue
            b = series[i - n][1]
            if b:
                out.append((d, (v / b - 1) * 100))
        elif tf == "mom":
            if i == 0:
                continue
            b = series[i - 1][1]
            if b:
                out.append((d, (v / b - 1) * 100))
        elif tf == "diff":
            if i == 0:
                continue
            out.append((d, v - series[i - 1][1]))
    return out


def value_ago(series, days):
    """Value at or before `days` ago."""
    if not series:
        return None
    target = dt.date.fromisoformat(series[-1][0]) - dt.timedelta(days=days)
    for d, v in reversed(series):
        if dt.date.fromisoformat(d) <= target:
            return v
    return None


def fred_chart(sid, tf="lvl", years=10):
    s = transform(fred(sid), tf)
    cutoff = (dt.date.today() - dt.timedelta(days=int(365.25 * years))).isoformat()
    s = [(d, v) for d, v in s if d >= cutoff]
    if len(s) > 1500:  # thin daily data for charting
        step = math.ceil(len(s) / 1500)
        s = s[::step] + ([s[-1]] if (len(s) - 1) % step else [])
    return {"id": sid, "tf": tf, "labels": [d for d, _ in s], "values": [_f(v, 3) for _, v in s]}


# --------------------------------------------------------------------------- RSS news

def gnews(q):
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"


FEEDS = {
    "top": [
        ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("Yahoo Finance", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EDJI,%5EIXIC&region=US&lang=en-US"),
        ("Google News", "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"),
    ],
    "equities": [
        ("CNBC Investing", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
        ("Google News", gnews("stock market when:2d")),
        ("Google News", gnews("earnings results stock when:3d")),
    ],
    "govt": [
        ("Google News", gnews("treasury yields when:3d")),
        ("Google News", gnews("bond market Fed rates when:3d")),
    ],
    "credit": [
        ("Google News", gnews("corporate bonds when:7d")),
        ("Google News", gnews("credit spreads OR \"high-yield bonds\" OR \"junk bonds\" when:7d")),
        ("Google News", gnews("\"leveraged loans\" OR \"bond issuance\" when:7d")),
    ],
    "private": [
        ("Crunchbase News", "https://news.crunchbase.com/feed/"),
        ("Google News", gnews("\"private equity\" deal when:7d")),
        ("Google News", gnews("\"private credit\" when:7d")),
        ("Google News", gnews("\"venture capital\" funding round when:7d")),
    ],
    "econ": [
        ("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
        ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("Google News", gnews("economic outlook forecast when:7d")),
        ("Google News", gnews("IMF OR OECD OR \"World Bank\" growth outlook when:14d")),
    ],
}


def _feed(src, url):
    def load():
        r = SESSION.get(url, timeout=15)
        r.raise_for_status()
        p = feedparser.parse(r.content)
        items = []
        for e in p.entries[:40]:
            title = (e.get("title") or "").strip()
            source = src
            if "news.google.com" in url and " - " in title:
                title, source = title.rsplit(" - ", 1)
            t = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(t) if t else 0
            items.append({"title": title, "link": e.get("link"), "source": source, "ts": ts})
        return items
    return CACHE.get("feed:" + url, 600, load)


def news(topic):
    feeds = FEEDS.get(topic, FEEDS["top"])
    futs = [POOL.submit(safe, _feed, s, u) for s, u in feeds]
    seen, items = set(), []
    stale = time.time() - 45 * 86400
    for f in futs:
        res = f.result()
        if isinstance(res, dict):
            continue
        for it in res:
            k = it["title"].lower()[:80]
            if k in seen or not it["title"] or (it["ts"] and it["ts"] < stale):
                continue
            seen.add(k)
            items.append(it)
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:60]


# --------------------------------------------------------------------------- pages

TENORS = [("1M", "DGS1MO"), ("3M", "DGS3MO"), ("6M", "DGS6MO"), ("1Y", "DGS1"),
          ("2Y", "DGS2"), ("3Y", "DGS3"), ("5Y", "DGS5"), ("7Y", "DGS7"),
          ("10Y", "DGS10"), ("20Y", "DGS20"), ("30Y", "DGS30")]

GLOBAL10 = [("Germany", "IRLTLT01DEM156N"), ("United Kingdom", "IRLTLT01GBM156N"),
            ("Japan", "IRLTLT01JPM156N"), ("Italy", "IRLTLT01ITM156N"),
            ("France", "IRLTLT01FRM156N"), ("Canada", "IRLTLT01CAM156N"),
            ("Australia", "IRLTLT01AUM156N")]

RATES_TABLE = [
    ("Fed Funds Target (Upper)", "DFEDTARU", "%"), ("Effective Fed Funds", "DFF", "%"),
    ("SOFR", "SOFR", "%"), ("10Y-2Y Spread", "T10Y2Y", "%"), ("10Y-3M Spread", "T10Y3M", "%"),
    ("10Y Real Yield (TIPS)", "DFII10", "%"), ("10Y Breakeven Inflation", "T10YIE", "%"),
    ("5Y5Y Fwd Inflation", "T5YIFR", "%"), ("30Y Mortgage Rate", "MORTGAGE30US", "%"),
]

CREDIT = [
    ("IG Corporate OAS", "BAMLC0A0CM"), ("AAA OAS", "BAMLC0A1CAAA"), ("AA OAS", "BAMLC0A2CAA"),
    ("A OAS", "BAMLC0A3CA"), ("BBB OAS", "BAMLC0A4CBBB"), ("High Yield OAS", "BAMLH0A0HYM2"),
    ("BB OAS", "BAMLH0A1HYBB"), ("B OAS", "BAMLH0A2HYB"), ("CCC & Lower OAS", "BAMLH0A3HYC"),
    ("EM Corporate OAS", "BAMLEMCBPIOAS"),
]
CREDIT_YIELDS = [("IG Effective Yield", "BAMLC0A0CMEY"), ("HY Effective Yield", "BAMLH0A0HYM2EY"),
                 ("BBB Effective Yield", "BAMLC0A4CBBBEY"), ("CCC Effective Yield", "BAMLH0A3HYCEY")]

PRIVATE_MACRO = [
    ("Banks Tightening C&I Loans (net %)", "DRTSCILM", "lvl", "%"),
    ("Banks Tightening Small-Firm Loans (net %)", "DRTSCIS", "lvl", "%"),
    ("C&I Loans Outstanding YoY", "BUSLOANS", "yoy", "%"),
    ("Leveraged Loan Proxy: HY OAS", "BAMLH0A0HYM2", "lvl", "%"),
    ("Financial Conditions (NFCI)", "NFCI", "lvl", ""),
]

ECON = [
    # name, id, transform, unit, category
    ("Real GDP Growth (QoQ SAAR)", "A191RL1Q225SBEA", "lvl", "%", "Growth"),
    ("Atlanta Fed GDPNow", "GDPNOW", "lvl", "%", "Growth"),
    ("Industrial Production YoY", "INDPRO", "yoy", "%", "Growth"),
    ("Retail Sales MoM", "RSAFS", "mom", "%", "Growth"),
    ("Housing Starts (k, SAAR)", "HOUST", "lvl", "k", "Growth"),
    ("UMich Consumer Sentiment", "UMCSENT", "lvl", "", "Growth"),
    ("CPI YoY", "CPIAUCSL", "yoy", "%", "Inflation"),
    ("Core CPI YoY", "CPILFESL", "yoy", "%", "Inflation"),
    ("Core PCE YoY (Fed target 2%)", "PCEPILFE", "yoy", "%", "Inflation"),
    ("PPI Final Demand YoY", "PPIFIS", "yoy", "%", "Inflation"),
    ("Avg Hourly Earnings YoY", "CES0500000003", "yoy", "%", "Inflation"),
    ("Unemployment Rate", "UNRATE", "lvl", "%", "Labor"),
    ("Nonfarm Payrolls (MoM, k)", "PAYEMS", "diff", "k", "Labor"),
    ("Initial Jobless Claims", "ICSA", "lvl", "", "Labor"),
    ("Job Openings (JOLTS, k)", "JTSJOL", "lvl", "k", "Labor"),
    ("Sahm Rule Indicator", "SAHMREALTIME", "lvl", "", "Labor"),
    ("Effective Fed Funds", "DFF", "lvl", "%", "Policy & Money"),
    ("M2 Money Supply YoY", "M2SL", "yoy", "%", "Policy & Money"),
    ("Fed Balance Sheet ($bn)", "WALCL", "lvl", "$bn", "Policy & Money"),
    ("Recession Probability", "RECPROUSM156N", "lvl", "%", "Risk"),
    ("Chicago Fed NFCI", "NFCI", "lvl", "", "Risk"),
    ("10Y-3M Yield Spread", "T10Y3M", "lvl", "%", "Risk"),
]


def _latest(series, tf):
    s = transform(series, tf) if not isinstance(series, dict) else []
    if not s:
        return None
    last_d, last_v = s[-1]
    prev = s[-2][1] if len(s) > 1 else None
    yago = value_ago(s, 365)
    return {"date": last_d, "value": _f(last_v, 3), "prev": _f(prev, 3), "yago": _f(yago, 3),
            "spark": [_f(v, 3) for _, v in s[-36:]]}


def page_overview():
    return {"groups": safe(_rows, OVERVIEW)}


def page_equities():
    return {"groups": safe(_rows, EQUITIES), "movers": safe(movers)}


def page_govt():
    ids = [i for _, i in TENORS] + [i for _, i, _ in RATES_TABLE] + [i for _, i in GLOBAL10]
    data = fred_many(ids)
    curve = {"tenors": [t for t, _ in TENORS], "now": [], "w1": [], "m1": [], "y1": [], "date": None}
    for _, i in TENORS:
        s = data.get(i)
        if isinstance(s, dict) or not s:
            for k in ("now", "w1", "m1", "y1"):
                curve[k].append(None)
            continue
        curve["now"].append(_f(s[-1][1], 3))
        curve["w1"].append(_f(value_ago(s, 7), 3))
        curve["m1"].append(_f(value_ago(s, 30), 3))
        curve["y1"].append(_f(value_ago(s, 365), 3))
        curve["date"] = max(curve["date"] or "", s[-1][0])
    tenor_rows = []
    for t, i in TENORS:
        s = data.get(i)
        if isinstance(s, dict) or not s:
            continue
        now = s[-1][1]
        tenor_rows.append({"name": f"UST {t}", "id": i, "date": s[-1][0], "value": _f(now, 3),
                           "d1": _f((now - s[-2][1]) * 100, 1) if len(s) > 1 else None,
                           "w1": _f((now - (value_ago(s, 7) or now)) * 100, 1),
                           "m1": _f((now - (value_ago(s, 30) or now)) * 100, 1),
                           "y1": _f((now - (value_ago(s, 365) or now)) * 100, 1),
                           "spark": [_f(v, 3) for _, v in s[-66:]]})
    rates = []
    for name, i, unit in RATES_TABLE:
        lt = _latest(data.get(i), "lvl")
        if lt:
            rates.append({"name": name, "id": i, "unit": unit, **lt})
    glob = []
    for name, i in GLOBAL10:
        lt = _latest(data.get(i), "lvl")
        if lt:
            glob.append({"name": name, "id": i, "unit": "%", **lt})
    return {"curve": curve, "tenors": tenor_rows, "rates": rates, "global": glob,
            "etfs": safe(_rows, GOVT_ETFS)}


def page_corp():
    ids = [i for _, i in CREDIT] + [i for _, i in CREDIT_YIELDS]
    data = fred_many(ids)
    rows = []
    for name, i in CREDIT + CREDIT_YIELDS:
        s = data.get(i)
        if isinstance(s, dict) or not s:
            continue
        now = s[-1][1]
        vals = [v for _, v in s]
        last5 = [v for d, v in s if d >= (dt.date.today() - dt.timedelta(days=365 * 5)).isoformat()]
        pct = None
        if last5:
            pct = sum(1 for v in last5 if v <= now) / len(last5) * 100
        yrs = (dt.date.fromisoformat(s[-1][0]) - dt.date.fromisoformat(
            next(d for d, _ in s if d >= (dt.date.today() - dt.timedelta(days=365 * 5)).isoformat()))).days / 365.25
        rows.append({"name": name, "id": i, "date": s[-1][0], "value": _f(now, 3), "pct_years": _f(yrs, 1),
                     "kind": "yield" if i.endswith("EY") else "oas",
                     "d1": _f((now - s[-2][1]) * 100, 0) if len(s) > 1 else None,
                     "w1": _f((now - (value_ago(s, 7) or now)) * 100, 0),
                     "m1": _f((now - (value_ago(s, 30) or now)) * 100, 0),
                     "y1": _f((now - (value_ago(s, 365) or now)) * 100, 0),
                     "lo": _f(min(vals), 2), "hi": _f(max(vals), 2), "pct5y": _f(pct, 0),
                     "spark": [_f(v, 3) for _, v in s[-130:]]})
    return {"credit": rows, "etfs": safe(_rows, CORP_ETFS)}


def page_private():
    data = fred_many([i for _, i, _, _ in PRIVATE_MACRO])
    macro = []
    for name, i, tf, unit in PRIVATE_MACRO:
        lt = _latest(data.get(i), tf)
        if lt:
            macro.append({"name": name, "id": i, "tf": tf, "unit": unit, **lt})
    return {"groups": safe(_rows, PRIVATE_EQ), "macro": macro}


def _signals(ind):
    """Rule-based macro read. ind: id -> latest dict."""
    out = []

    def add(name, value, status, text):
        out.append({"name": name, "value": value, "status": status, "text": text})

    v = (ind.get("T10Y3M") or {}).get("value")
    if v is not None:
        st = "red" if v < 0 else ("amber" if v < 0.5 else "green")
        add("Yield Curve (10Y-3M)", f"{v:+.2f}%", st,
            "Inverted - historically a leading recession signal" if v < 0 else
            ("Flat - late-cycle / transition" if v < 0.5 else "Positively sloped - normal expansion shape"))
    v = (ind.get("SAHMREALTIME") or {}).get("value")
    if v is not None:
        st = "red" if v >= 0.5 else ("amber" if v >= 0.3 else "green")
        add("Sahm Rule", f"{v:.2f}", st,
            "Triggered (>=0.50) - labor market consistent with recession" if v >= 0.5 else
            ("Rising unemployment - watch closely" if v >= 0.3 else "Labor market stable"))
    v = (ind.get("PCEPILFE") or {}).get("value")
    if v is not None:
        st = "red" if v > 3 else ("amber" if v > 2.4 else "green")
        add("Core PCE Inflation", f"{v:.2f}%", st,
            "Well above Fed's 2% target" if v > 3 else
            ("Modestly above target" if v > 2.4 else "Near Fed's 2% target"))
    v = (ind.get("GDPNOW") or {}).get("value")
    if v is not None:
        st = "red" if v < 0 else ("amber" if v < 1.5 else "green")
        add("GDPNow (current qtr)", f"{v:.1f}%", st,
            "Contraction tracking" if v < 0 else ("Below-trend growth" if v < 1.5 else "At/above-trend growth"))
    v = (ind.get("BAMLH0A0HYM2") or {}).get("value")
    if v is not None:
        st = "red" if v > 6 else ("amber" if v > 4.5 else "green")
        add("High-Yield Spreads", f"{v:.2f}%", st,
            "Credit stress - risk-off pricing" if v > 6 else
            ("Elevated - some caution in credit" if v > 4.5 else "Tight - credit markets wide open"))
    v = (ind.get("NFCI") or {}).get("value")
    if v is not None:
        st = "red" if v > 0.3 else ("amber" if v > 0 else "green")
        add("Financial Conditions", f"{v:+.2f}", st,
            "Tighter than average" if v > 0 else "Looser than average - supportive")
    ff = (ind.get("DFF") or {}).get("value")
    core = (ind.get("PCEPILFE") or {}).get("value")
    if ff is not None and core is not None:
        r = ff - core
        st = "amber" if r > 1.5 else ("green" if r > 0 else "amber")
        add("Real Fed Funds", f"{r:+.2f}%", st,
            "Restrictive policy stance" if r > 1.5 else
            ("Near neutral" if r > 0 else "Accommodative - negative real rates"))
    v = (ind.get("RECPROUSM156N") or {}).get("value")
    if v is not None:
        st = "red" if v > 30 else ("amber" if v > 10 else "green")
        add("Recession Probability", f"{v:.1f}%", st,
            "High model-implied odds" if v > 30 else ("Non-trivial risk" if v > 10 else "Low model-implied odds"))
    reds = sum(1 for s in out if s["status"] == "red")
    greens = sum(1 for s in out if s["status"] == "green")
    if reds >= 3:
        regime = ("Late-cycle / downturn risk elevated", "red")
    elif greens >= len(out) - 2:
        regime = ("Expansion - macro backdrop broadly supportive", "green")
    else:
        regime = ("Mixed signals - mid/late-cycle, data-dependent", "amber")
    return {"signals": out, "regime": regime[0], "regime_status": regime[1],
            "reds": reds, "greens": greens, "total": len(out)}


def page_econ():
    ids = list(dict.fromkeys([i for _, i, _, _, _ in ECON] + ["BAMLH0A0HYM2"]))
    data = fred_many(ids)
    rows, latest = [], {}
    for name, i, tf, unit, cat in ECON:
        lt = _latest(data.get(i), tf)
        if not lt:
            rows.append({"name": name, "id": i, "tf": tf, "unit": unit, "cat": cat, "missing": True})
            continue
        if i == "WALCL":  # millions -> billions
            for k in ("value", "prev", "yago"):
                if lt[k] is not None:
                    lt[k] = _f(lt[k] / 1000, 1)
            lt["spark"] = [_f(x / 1000, 1) if x is not None else None for x in lt["spark"]]
        rows.append({"name": name, "id": i, "tf": tf, "unit": unit, "cat": cat, **lt})
        latest[i] = lt
    hy = _latest(data.get("BAMLH0A0HYM2"), "lvl")
    if hy:
        latest["BAMLH0A0HYM2"] = hy
    return {"indicators": rows, "outlook": _signals(latest)}


# --------------------------------------------------------------------------- single security

RANGES = {"1D": ("1d", "5m"), "5D": ("5d", "30m"), "1M": ("1mo", "1d"), "6M": ("6mo", "1d"),
          "YTD": ("ytd", "1d"), "1Y": ("1y", "1d"), "5Y": ("5y", "1wk"), "MAX": ("max", "1mo")}

INFO_KEYS = ["longName", "shortName", "quoteType", "exchange", "currency", "sector", "industry",
             "country", "website", "fullTimeEmployees", "longBusinessSummary", "marketCap",
             "enterpriseValue", "trailingPE", "forwardPE", "pegRatio", "priceToBook",
             "priceToSalesTrailing12Months", "enterpriseToEbitda", "dividendYield", "payoutRatio",
             "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fiftyDayAverage", "twoHundredDayAverage",
             "averageVolume", "sharesOutstanding", "shortPercentOfFloat", "totalRevenue",
             "revenueGrowth", "earningsGrowth", "grossMargins", "operatingMargins", "profitMargins",
             "returnOnEquity", "ebitda", "freeCashflow", "totalCash", "totalDebt", "debtToEquity",
             "currentPrice", "regularMarketPrice", "previousClose", "regularMarketChangePercent",
             "targetMeanPrice", "targetHighPrice", "targetLowPrice", "recommendationKey",
             "numberOfAnalystOpinions", "trailingEps", "forwardEps", "totalAssets", "yield",
             "netExpenseRatio", "category", "fundFamily", "ytdReturn", "threeYearAverageReturn"]


def security(sym, rng="1Y"):
    sym = sym.upper().strip()
    period, interval = RANGES.get(rng, RANGES["1Y"])

    def hist():
        t = yf.Ticker(sym)
        h = t.history(period=period, interval=interval, auto_adjust=False)
        if h.empty and rng == "1D":
            h = t.history(period="5d", interval="5m", auto_adjust=False)
            if not h.empty:
                h = h[h.index.date == h.index[-1].date()]
        if h.empty:
            raise ValueError(f"No price data for '{sym}'")
        fmt = "%m-%d %H:%M" if interval.endswith("m") else "%Y-%m-%d"
        return {"labels": [i.strftime(fmt) for i in h.index],
                "close": [_f(x) for x in h["Close"]],
                "volume": [_f(x, 0) for x in h["Volume"]] if "Volume" in h else []}

    def info():
        t = yf.Ticker(sym)
        try:
            raw = t.info or {}
        except Exception:  # noqa: BLE001
            raw = {}
        d = {k: raw.get(k) for k in INFO_KEYS if raw.get(k) is not None}
        try:
            fi = t.fast_info
            d.setdefault("regularMarketPrice", _f(fi.get("lastPrice")))
            d.setdefault("previousClose", _f(fi.get("previousClose")))
            d.setdefault("currency", fi.get("currency"))
        except Exception:  # noqa: BLE001
            pass
        nws = []
        try:
            for n in (t.news or [])[:12]:
                c = n.get("content") or n
                title = c.get("title")
                link = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
                        or c.get("link"))
                src = (c.get("provider") or {}).get("displayName") or c.get("publisher") or ""
                ts = c.get("providerPublishTime") or 0
                if not ts and c.get("pubDate"):
                    try:
                        ts = int(dt.datetime.fromisoformat(c["pubDate"].replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        ts = 0
                if title:
                    nws.append({"title": title, "link": link, "source": src, "ts": ts})
        except Exception:  # noqa: BLE001
            pass
        return {"info": d, "news": nws}

    h = CACHE.get(f"hist:{sym}:{rng}", 60 if rng in ("1D", "5D") else 300, hist)
    i = safe(CACHE.get, f"info:{sym}", 900, info)
    return {"symbol": sym, "range": rng, "history": h,
            "info": (i or {}).get("info", {}), "news": (i or {}).get("news", [])}


def search(q):
    def load():
        s = yf.Search(q, max_results=8, news_count=0)
        return [{"sym": x.get("symbol"), "name": x.get("shortname") or x.get("longname") or "",
                 "type": x.get("quoteType") or "", "exch": x.get("exchDisp") or x.get("exchange") or ""}
                for x in (s.quotes or []) if x.get("symbol")]
    return CACHE.get("search:" + q.lower(), 3600, load)
