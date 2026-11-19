"""Backends for the companion apps.

Portfolio Lab  (/lab/)    - backtests, risk analytics, efficient frontier, Monte Carlo, historical stress tests
Signal Monitor (/signal/) - alternative "open source" data: Wikipedia attention & edit activity, news-headline
                            sentiment, Hacker News mentions, GDELT global tone - analysed against price.

All sources are free and keyless: Yahoo Finance (yfinance), FRED, Wikimedia REST API, MediaWiki API,
Google News RSS, Hacker News (Algolia) API, GDELT DOC 2.0 API.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import feedparser
import numpy as np
import pandas as pd
import requests
import yfinance as yf

from data import CACHE, POOL, SESSION, _f, fred, gnews, safe

DATA_DIR = Path(os.environ.get("MT_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MarketTerminal")
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _series(s, nd=4, step=1):
    return [_f(x, nd) for x in s.values[::step]]


def _labels(idx, step=1):
    return [d.strftime("%Y-%m-%d") for d in idx[::step]]


def _step(n, target=1200):
    return max(1, math.ceil(n / target))


# =========================================================================== PORTFOLIO LAB
PERIOD_YEARS = {"1Y": 1, "3Y": 3, "5Y": 5, "10Y": 10, "20Y": 20, "MAX": None}
EPISODES = [
    ("2008 Financial Crisis", "2007-10-09", "2009-03-09"),
    ("2011 US Downgrade / Euro Crisis", "2011-04-29", "2011-10-03"),
    ("2015-16 China & Oil Slump", "2015-05-21", "2016-02-11"),
    ("Q4 2018 Fed Selloff", "2018-09-20", "2018-12-24"),
    ("COVID Crash", "2020-02-19", "2020-03-23"),
    ("COVID Rebound", "2020-03-23", "2020-08-31"),
    ("2022 Inflation & Rate Shock", "2022-01-03", "2022-10-12"),
    ("2025 Tariff Shock", "2025-02-19", "2025-04-08"),
]


def _prices(syms):
    key = "labpx:" + ",".join(sorted(syms))

    def load():
        df = yf.download(syms, period="max", interval="1d", auto_adjust=True, progress=False,
                         group_by="column", threads=True)
        c = df["Close"]
        if not hasattr(c, "columns"):
            c = c.to_frame(syms[0])
        idx = pd.to_datetime(c.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        c.index = idx.normalize()
        return c[~c.index.duplicated(keep="last")]

    return CACHE.get(key, 3600, load)


def _parse_holdings(s):
    d = {}
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        t, _, w = part.partition(":")
        t = t.strip().upper()
        try:
            w = float(w) if w.strip() else 1.0
        except ValueError:
            w = 0.0
        if t and w > 0:
            d[t] = d.get(t, 0.0) + w
    tot = sum(d.values())
    if not d or tot <= 0:
        raise ValueError("Add at least one holding with a positive weight")
    if len(d) > 25:
        raise ValueError("Maximum 25 holdings")
    return {t: w / tot for t, w in d.items()}


def _simulate(rets, w, periods, amt):
    """Daily simulation with optional calendar rebalancing. rets: (T, N) simple returns."""
    vals = np.empty(len(rets))
    pos = w * amt
    vals[0] = amt
    for i in range(1, len(rets)):
        pos = pos * (1 + rets[i])
        if periods is not None and periods[i] != periods[i - 1]:
            pos = pos.sum() * w
        vals[i] = pos.sum()
    return vals


def _perf(s, rf, b=None):
    """Performance statistics for a value series s (and optional benchmark value series b)."""
    r = s.pct_change().dropna()
    yrs = max((s.index[-1] - s.index[0]).days / 365.25, 1e-9)
    tot = s.iloc[-1] / s.iloc[0] - 1
    vol = r.std() * math.sqrt(252)
    ann = r.mean() * 252
    dd = s / s.cummax() - 1
    under = (dd < 0).astype(int)
    # longest drawdown (calendar days from peak to recovery or today)
    longest, start = 0, None
    for d, u in zip(dd.index, under.values):
        if u and start is None:
            start = d
        elif not u and start is not None:
            longest = max(longest, (d - start).days)
            start = None
    if start is not None:
        longest = max(longest, (dd.index[-1] - start).days)
    neg = r[r < 0]
    down = neg.std() * math.sqrt(252) if len(neg) > 2 else None
    q5 = np.percentile(r, 5) if len(r) > 20 else None
    m = s.resample("ME").last().pct_change().dropna()
    cagr = (1 + tot) ** (1 / yrs) - 1 if yrs >= 0.95 else None
    out = {
        "total": _f(tot * 100, 2), "cagr": _f(cagr * 100, 2) if cagr is not None else None,
        "vol": _f(vol * 100, 2), "sharpe": _f((ann - rf) / vol, 2) if vol else None,
        "sortino": _f((ann - rf) / down, 2) if down else None,
        "mdd": _f(dd.min() * 100, 2), "cur_dd": _f(dd.iloc[-1] * 100, 2), "longest_dd": longest,
        "calmar": _f(cagr / abs(dd.min()), 2) if cagr is not None and dd.min() < 0 else None,
        "var95": _f(-q5 * 100, 2) if q5 is not None else None,
        "cvar95": _f(-r[r <= q5].mean() * 100, 2) if q5 is not None else None,
        "best_m": _f(m.max() * 100, 2) if len(m) else None, "worst_m": _f(m.min() * 100, 2) if len(m) else None,
        "pos_m": _f((m > 0).mean() * 100, 1) if len(m) else None,
        "best_d": _f(r.max() * 100, 2), "worst_d": _f(r.min() * 100, 2),
        "end": _f(s.iloc[-1], 2),
    }
    if b is not None:
        br = b.pct_change().reindex(r.index)
        ok = br.notna()
        rr, bb = r[ok], br[ok]
        if len(rr) > 20 and bb.var() > 0:
            beta = rr.cov(bb) / bb.var()
            bann = bb.mean() * 252
            te = (rr - bb).std() * math.sqrt(252)
            bm = b.resample("ME").last().pct_change().dropna()
            mm = m.reindex(bm.index).dropna()
            bm = bm.reindex(mm.index)
            upc = mm[bm > 0].mean() / bm[bm > 0].mean() * 100 if (bm > 0).any() else None
            dnc = mm[bm < 0].mean() / bm[bm < 0].mean() * 100 if (bm < 0).any() else None
            out.update(beta=_f(beta, 2), alpha=_f((ann - (rf + beta * (bann - rf))) * 100, 2),
                       corr=_f(rr.corr(bb), 2), te=_f(te * 100, 2),
                       ir=_f((ann - bann) / te, 2) if te else None,
                       up_cap=_f(upc, 1), down_cap=_f(dnc, 1))
    return out


def _frontier(rets, names, rf, cur_w, n=4000):
    mu = rets.mean().values * 252
    cov = rets.cov().values * 252
    k = len(names)
    rng = np.random.default_rng(7)
    W = rng.dirichlet(np.ones(k) * (0.6 if k > 4 else 1.0), n)
    W = np.vstack([W, np.eye(k), cur_w[None, :]])
    ret = W @ mu
    vol = np.sqrt(np.einsum("ij,jk,ik->i", W, cov, W))
    sh = (ret - rf) / np.where(vol > 0, vol, np.nan)
    i_ms, i_mv = int(np.nanargmax(sh)), int(np.argmin(vol))

    def pt(i):
        return {"ret": _f(ret[i] * 100, 2), "vol": _f(vol[i] * 100, 2), "sharpe": _f(sh[i], 2),
                "w": {names[j]: _f(W[i, j] * 100, 1) for j in range(k) if W[i, j] >= 0.005}}

    take = np.linspace(0, n - 1, min(n, 1500)).astype(int)
    return {"cloud": [[_f(vol[i] * 100, 2), _f(ret[i] * 100, 2), _f(sh[i], 2)] for i in take],
            "assets": [{"sym": names[j], "vol": _f(vol[n + j] * 100, 2), "ret": _f(ret[n + j] * 100, 2)} for j in range(k)],
            "current": pt(len(W) - 1), "max_sharpe": pt(i_ms), "min_vol": pt(i_mv)}


def _monte_carlo(port, years, amt, contrib, paths=2000):
    m = port.resample("ME").last().pct_change().dropna().values
    if len(m) < 12:
        return {"error": "Need at least 12 months of history for Monte Carlo"}
    months, block = int(years * 12), 3
    rng = np.random.default_rng(11)
    nb = math.ceil(months / block)
    starts = rng.integers(0, max(1, len(m) - block + 1), size=(paths, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(paths, -1)[:, :months]
    idx = np.minimum(idx, len(m) - 1)
    R = m[idx]
    V = np.empty((paths, months + 1))
    V[:, 0] = amt
    for t in range(months):
        V[:, t + 1] = V[:, t] * (1 + R[:, t]) + contrib
    pct = np.percentile(V, [5, 25, 50, 75, 95], axis=0)
    invested = amt + contrib * months
    end = V[:, -1]
    step = 1 if months <= 120 else 3
    return {"months": list(range(0, months + 1, step)),
            "bands": {k: [_f(x, 0) for x in pct[i][::step]] for i, k in enumerate(["p5", "p25", "p50", "p75", "p95"])},
            "invested": _f(invested, 0), "median_end": _f(np.median(end), 0), "p5_end": _f(pct[0][-1], 0),
            "p95_end": _f(pct[4][-1], 0), "prob_loss": _f((end < invested).mean() * 100, 1),
            "prob_double": _f((end >= 2 * invested).mean() * 100, 1),
            "hist_months": len(m), "paths": paths, "years": years}


def _stress(full, W, bench):
    out = []
    for name, a, b in EPISODES:
        a, b = pd.Timestamp(a), pd.Timestamp(b)
        if a > full.index[-1]:
            continue
        win = full[(full.index >= a) & (full.index <= b)]
        if len(win) < 2:
            continue
        cov, tot = 0.0, 0.0
        for s, w in W.items():
            col = win[s].dropna() if s in win else pd.Series(dtype=float)
            if len(col) >= 2 and col.index[0] <= win.index[0] + pd.Timedelta(days=5):
                tot += w * (col.iloc[-1] / col.iloc[0] - 1)
                cov += w
        bret = None
        if bench in win:
            bc = win[bench].dropna()
            if len(bc) >= 2 and bc.index[0] <= win.index[0] + pd.Timedelta(days=5):
                bret = _f((bc.iloc[-1] / bc.iloc[0] - 1) * 100, 2)
        out.append({"name": name, "start": a.strftime("%Y-%m-%d"), "end": b.strftime("%Y-%m-%d"),
                    "port": _f(tot / cov * 100, 2) if cov > 0 else None, "coverage": _f(cov * 100, 0), "bench": bret})
    return out


def lab_backtest(q):
    W = _parse_holdings(q.get("h", ""))
    bench = (q.get("bench") or "SPY").upper().strip()
    period = q.get("period", "5Y")
    rebal = q.get("rebal", "Q")
    amt = float(q.get("amount") or 10000)
    contrib = float(q.get("contrib") or 0)
    horizon = max(1, min(40, int(float(q.get("horizon") or 10))))
    syms = list(dict.fromkeys(list(W) + [bench]))
    full = _prices(syms)
    missing = [s for s in syms if s not in full.columns or full[s].dropna().empty]
    bad = [s for s in W if s in missing]
    if bad:
        raise ValueError("No price history for: " + ", ".join(bad))
    yrs = PERIOD_YEARS.get(period, 5)
    px = full if not yrs else full[full.index >= full.index[-1] - pd.DateOffset(years=yrs)]
    hold = px[list(W)].dropna()
    if len(hold) < 60:
        raise ValueError("Not enough overlapping history between the holdings (need ~3 months)")
    first_valid = {s: full[s].first_valid_index() for s in W}
    limiter = max(first_valid, key=lambda s: first_valid[s])
    dates = hold.index
    rets = hold.pct_change().fillna(0.0).values
    w = np.array([W[s] for s in hold.columns])
    per = {"M": dates.to_period("M"), "Q": dates.to_period("Q"), "A": dates.to_period("Y")}.get(rebal)
    port = pd.Series(_simulate(rets, w, None if per is None else np.asarray(per.astype(str)), amt), index=dates)

    b = None
    if bench not in missing:
        bs = px[bench].reindex(dates).ffill()
        if bs.notna().sum() > 20:
            fv = bs.first_valid_index()
            b = bs / bs.loc[fv] * amt
            b[b.index < fv] = np.nan

    rf = 0.0
    try:
        tb = [v for d, v in fred("DTB3") if d >= dates[0].strftime("%Y-%m-%d")]
        rf = (sum(tb) / len(tb)) / 100 if tb else 0.0
    except Exception:  # noqa: BLE001
        pass

    stats = _perf(port, rf, b.dropna() if b is not None else None)
    bstats = _perf(b.dropna(), rf) if b is not None and b.notna().sum() > 60 else None
    dd = port / port.cummax() - 1
    bdd = (b / b.cummax() - 1) if b is not None else None

    # calendar-year and monthly returns
    ye = port.resample("YE").last()
    base = pd.concat([pd.Series([port.iloc[0]], index=[port.index[0] - pd.Timedelta(days=1)]), ye])
    yr = base.pct_change().dropna()
    byr = None
    if b is not None:
        bye = b.resample("YE").last()
        bbase = pd.concat([pd.Series([b.dropna().iloc[0]], index=[b.dropna().index[0] - pd.Timedelta(days=1)]), bye])
        byr = bbase.pct_change().reindex(yr.index)
    me = port.resample("ME").last()
    mret = pd.concat([pd.Series([port.iloc[0]], index=[port.index[0] - pd.Timedelta(days=1)]), me]).pct_change().dropna()
    monthly = {}
    for d, v in mret.items():
        monthly.setdefault(str(d.year), [None] * 12)[d.month - 1] = _f(v * 100, 2)

    # rolling
    roll_ret = port / port.shift(252) - 1
    roll_vol = port.pct_change().rolling(63).std() * math.sqrt(252)

    # holdings analytics
    hr = hold.pct_change().dropna()
    pr = port.pct_change().dropna()
    cov = hr.cov().values * 252
    pvar = float(w @ cov @ w)
    rc = (w * (cov @ w)) / pvar if pvar > 0 else np.zeros_like(w)
    holdings = []
    for j, s in enumerate(hold.columns):
        sp = hold[s]
        hs = _perf(sp / sp.iloc[0] * 100, rf)
        holdings.append({"sym": s, "weight": _f(w[j] * 100, 2), "risk": _f(rc[j] * 100, 2),
                         "cagr": hs["cagr"], "total": hs["total"], "vol": hs["vol"], "mdd": hs["mdd"], "sharpe": hs["sharpe"],
                         "corr": _f(hr[s].corr(pr), 2), "since": first_valid[s].strftime("%Y-%m-%d")})
    cm = hr.corr()

    step = _step(len(dates))
    out = {
        "weights": {s: _f(v * 100, 2) for s, v in W.items()}, "bench": bench if b is not None else None,
        "period": period, "rebal": rebal, "amount": amt, "rf": _f(rf * 100, 2),
        "start": dates[0].strftime("%Y-%m-%d"), "end": dates[-1].strftime("%Y-%m-%d"),
        "limiter": limiter if yrs is None or first_valid[limiter] > full.index[-1] - pd.DateOffset(years=yrs) else None,
        "labels": _labels(dates, step), "port": _series(port, 2, step),
        "bench_series": _series(b, 2, step) if b is not None else None,
        "dd": _series(dd * 100, 2, step), "bench_dd": _series(bdd * 100, 2, step) if bdd is not None else None,
        "roll_ret": _series(roll_ret * 100, 2, step), "roll_vol": _series(roll_vol * 100, 2, step),
        "stats": stats, "bench_stats": bstats,
        "years": [{"year": d.year, "port": _f(v * 100, 2), "bench": _f(byr.loc[d] * 100, 2) if byr is not None else None}
                  for d, v in yr.items()],
        "monthly": monthly, "holdings": holdings,
        "corr": {"syms": list(cm.columns)[:15], "m": [[_f(cm.iloc[i, j], 2) for j in range(min(15, len(cm)))] for i in range(min(15, len(cm)))]},
        "stress": _stress(full, W, bench),
        "mc": safe(_monte_carlo, port, horizon, amt, contrib),
    }
    if len(W) >= 2:
        out["frontier"] = safe(_frontier, hr, list(hold.columns), rf, w)
    return out


# =========================================================================== SIGNAL MONITOR
WIKI = requests.Session()
WIKI.headers.update({"User-Agent": "MarketTerminalSignalMonitor/1.0 (local desktop research app)"})
_SETTINGS_FILE = DATA_DIR / "signal_settings.json"


def get_settings():
    try:
        return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _apply_ua():
    c = (get_settings().get("contact") or "").strip()
    WIKI.headers["User-Agent"] = "MarketTerminalSignalMonitor/1.0 (local desktop research app" + (f"; {c})" if c else ")")


def set_settings(contact=None):
    s = get_settings()
    if contact is not None:
        s["contact"] = contact.strip()[:120]
    _SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")
    _apply_ua()
    _WIKI_COOL[0] = 0.0
    return {"contact": s.get("contact", "")}


class RateLimited(RuntimeError):
    def __init__(self, secs):
        super().__init__(f"Wikimedia rate limit - retry in {int(secs)}s")
        self.secs = secs


_WIKI_GATE = threading.Semaphore(2)
_WIKI_PACE = threading.Lock()
_WIKI_LAST = [0.0]
_WIKI_COOL = [0.0]  # epoch until which we must not call Wikimedia


def wiki_cooldown():
    return max(0.0, _WIKI_COOL[0] - time.time())


_apply_ua()


def _wget(url, **kw):
    """GET against Wikimedia: <=2 in flight, paced, and fail-fast during a rate-limit cooldown.
    (Wikimedia allows only ~10 quick requests from clients without contact info in their User-Agent.)"""
    cd = wiki_cooldown()
    if cd > 0:
        raise RateLimited(cd)
    with _WIKI_GATE:
        with _WIKI_PACE:
            wait = 0.4 - (time.time() - _WIKI_LAST[0])
            if wait > 0:
                time.sleep(wait)
            _WIKI_LAST[0] = time.time()
        r = WIKI.get(url, timeout=20, **kw)
    if r.status_code == 429:
        try:
            ra = float(r.headers.get("retry-after") or 0)
        except ValueError:
            ra = 0
        _WIKI_COOL[0] = time.time() + max(ra, 45.0)
        raise RateLimited(wiki_cooldown())
    r.raise_for_status()
    return r


_TITLE_FILE = DATA_DIR / "wiki_titles.json"
_TLOCK = threading.Lock()
_GDELT_LOCK = threading.Lock()
_GDELT_LAST = [0.0]


def _titles():
    try:
        return json.loads(_TITLE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_title(sym, title):
    with _TLOCK:
        m = _titles()
        m[sym] = title
        _TITLE_FILE.write_text(json.dumps(m, indent=1), encoding="utf-8")


_SUFFIX = re.compile(r"[,\s]+(inc\.?|incorporated|corporation|corp\.?|co\.?|company|ltd\.?|limited|plc|n\.v\.|s\.a\.|ag|se|"
                     r"holdings?|group|class [a-z]|& co\.?|usd|the|and|&)$", re.I)


def clean_name(n):
    n = (n or "").strip()
    for _ in range(4):
        n2 = _SUFFIX.sub("", n).strip(" ,.")
        if n2 == n:
            break
        n = n2
    return n


def _info(sym):
    def load():
        try:
            return yf.Ticker(sym).info or {}
        except Exception:  # noqa: BLE001
            return {}
    return CACHE.get("rinfo:" + sym, 900, load)


def resolve(sym):
    sym = sym.upper().strip()
    i = _info(sym)
    name = i.get("longName") or i.get("shortName") or sym
    title = _titles().get(sym)
    if not title:
        try:
            # Search with the FULL legal name ("Tesla, Inc." not "Tesla" -> avoids Nikola Tesla, the fruit
            # "Apple", the writing system "Alphabet"...) and prefer a hit whose snippet reads like a company.
            r = _wget("https://en.wikipedia.org/w/api.php", params={
                "action": "query", "list": "search", "srsearch": name if name != sym else sym,
                "format": "json", "srlimit": 5})
            hits = r.json().get("query", {}).get("search", [])
            biz = re.compile(r"compan|corporat|\binc\b|multinational|conglomerate|\bbank|manufactur|holding|"
                             r"\bfirm\b|retailer|insurer|exchange-traded|\bfund\b|cryptocurrenc|stock market index|"
                             r"pharmaceutical|airline|automaker|semiconductor|software|technology", re.I)
            pick = next((h for h in hits if biz.search(re.sub(r"<[^>]+>", "", h.get("snippet", "")))), None)
            title = (pick or (hits[0] if hits else {})).get("title")
        except RateLimited:
            raise
        except Exception:  # noqa: BLE001
            title = None
        if title:
            _save_title(sym, title)
    return {"sym": sym, "name": name, "short": clean_name(name), "title": title, "type": i.get("quoteType"),
            "sector": i.get("sector"), "industry": i.get("industry")}


_VIEW_FILE = DATA_DIR / "wiki_views_cache.json"
_VLOCK = threading.Lock()


def wiki_views(title, days=400):
    """Daily pageviews (~400 days), cached on disk for 12h - the data only updates once a day."""
    with _VLOCK:
        try:
            disk = json.loads(_VIEW_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            disk = {}
    hit = disk.get(title)
    if hit and time.time() - hit["t"] < 43200:
        s = pd.Series(hit["v"], index=pd.date_range(hit["start"], periods=len(hit["v"]), freq="D"), dtype=float)
    else:
        end = dt.date.today() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=400)
        u = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"
             f"{quote(title.replace(' ', '_'), safe='')}/daily/{start:%Y%m%d}/{end:%Y%m%d}")
        items = _wget(u).json().get("items", [])
        s = pd.Series({pd.Timestamp(x["timestamp"][:8]): x["views"] for x in items}, dtype=float)
        if s.empty:
            raise ValueError(f"No pageview data for '{title}'")
        s = s.asfreq("D").fillna(0)
        with _VLOCK:
            try:
                disk = json.loads(_VIEW_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                disk = {}
            disk[title] = {"t": time.time(), "start": s.index[0].strftime("%Y-%m-%d"), "v": [int(x) for x in s.values]}
            if len(disk) > 400:  # keep the file bounded
                for k in sorted(disk, key=lambda k: disk[k]["t"])[:len(disk) - 400]:
                    disk.pop(k, None)
            _VIEW_FILE.write_text(json.dumps(disk), encoding="utf-8")
    return s[s.index >= s.index[-1] - pd.Timedelta(days=days)]


def wiki_edits(title, days=180):
    def load():
        start = dt.datetime.utcnow() - dt.timedelta(days=days)
        stamps, cont = [], {}
        for _ in range(6):
            p = {"action": "query", "prop": "revisions", "titles": title, "rvprop": "timestamp", "rvlimit": 500,
                 "rvend": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "format": "json", **cont}
            j = _wget("https://en.wikipedia.org/w/api.php", params=p).json()
            for pg in j.get("query", {}).get("pages", {}).values():
                stamps += [x["timestamp"][:10] for x in pg.get("revisions", [])]
            if "continue" not in j:
                break
            cont = j["continue"]
        s = pd.Series(1, index=pd.to_datetime(stamps)).groupby(level=0).sum() if stamps else pd.Series(dtype=float)
        idx = pd.date_range(start.date(), dt.date.today(), freq="D")
        return s.reindex(idx, fill_value=0)
    return CACHE.get(f"we:{title}:{days}", 21600, load)


# ---- headline sentiment (compact finance lexicon, Loughran-McDonald style)
POS = set("""beat beats beating surge surges surged surging soar soars soared soaring jump jumps jumped rally rallies rallied
rallying gain gains gained rise rises rose rising climb climbs climbed record records upgrade upgrades upgraded outperform
outperforms outperformed strong stronger strongest growth grow grows grew growing profit profits profitable boost boosts
boosted win wins won winning approval approved approves breakthrough bullish upbeat optimistic optimism exceed exceeds
exceeded tops topped expands expansion expand raise raises raised higher highs positive recover recovers recovery rebound
rebounds rebounded success successful innovative innovation launch launches launched partnership dividend buyback
buybacks accelerate accelerates accelerating robust solid momentum best leading leader award upside opportunity
opportunities benefit benefits improve improves improved improvement milestone skyrocket skyrockets boom booming outpace
outpaces outpaced wins strength strengthens resilient resilience lifts lifted""".split())
NEG = set("""miss misses missed fall falls fell falling drop drops dropped plunge plunges plunged slump slumps slumped tumble
tumbles tumbled sink sinks sank decline declines declined declining loss losses lose loses lost losing weak weaker weakness
downgrade downgrades downgraded underperform underperforms cut cuts cutting layoff layoffs lawsuit lawsuits sue sues sued
probe probes investigation investigations fraud scandal recall recalls warning warns warned risk risks risky concern
concerns fear fears worry worries worried bearish selloff sell-off crash crashes crashed bankruptcy bankrupt default
defaults fined penalty penalties ban banned bans halt halted halts delay delays delayed slowdown slows slowing lower lows
negative struggle struggles struggling crisis volatile volatility uncertainty uncertain tariff tariffs shortage shortages
glitch outage breach hack hacked resign resigns resigned ousted fired downturn recession plummet plummets plummeted slide
slides slid sinking disappoint disappoints disappointing disappointed antitrust sanctions threat threatens threatened
collapse collapses collapsed worst trouble troubled problem problems fail fails failed failure slashes slashed slash
tanks tanked tank sluggish headwinds headwind downbeat pessimistic""".split())
NEGATORS = {"not", "no", "never", "without", "fails", "isn't", "doesn't", "won't", "didn't"}


def sentiment(text):
    toks = re.findall(r"[a-z][a-z'\-]*", (text or "").lower())
    p = n = 0
    for k, t in enumerate(toks):
        s = 1 if t in POS else -1 if t in NEG else 0
        if s and k and toks[k - 1] in NEGATORS:
            s = -s
        p += s > 0
        n += s < 0
    return (p - n) / max(1, p + n), p, n


def news_sentiment(name, sym):
    q = f'"{name}"' if name else sym
    url = gnews(f"{q} when:30d")

    def load():
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        p = feedparser.parse(r.content)
        items = []
        for e in p.entries[:100]:
            title = (e.get("title") or "").strip()
            source = ""
            if " - " in title:
                title, source = title.rsplit(" - ", 1)
            t = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(t) if t else 0
            sc, pp, nn = sentiment(title)
            items.append({"title": title, "source": source, "link": e.get("link"), "ts": ts, "score": round(sc, 3),
                          "pos": pp, "neg": nn})
        items.sort(key=lambda x: x["ts"], reverse=True)
        return items

    items = CACHE.get("sent:" + url, 1800, load)
    by_day = {}
    for it in items:
        if not it["ts"]:
            continue
        d = dt.datetime.utcfromtimestamp(it["ts"]).strftime("%Y-%m-%d")
        by_day.setdefault(d, []).append(it["score"])
    days = sorted(by_day)
    src = {}
    for it in items:
        src[it["source"] or "?"] = src.get(it["source"] or "?", 0) + 1
    scored = [x for x in items if x["pos"] or x["neg"]]
    avg = sum(x["score"] for x in scored) / len(scored) if scored else None
    return {"query": q, "n": len(items), "scored": len(scored), "avg": _f(avg, 3),
            "pos_share": _f(sum(1 for x in scored if x["score"] > 0) / len(scored) * 100, 1) if scored else None,
            "neg_share": _f(sum(1 for x in scored if x["score"] < 0) / len(scored) * 100, 1) if scored else None,
            "days": days, "count": [len(by_day[d]) for d in days],
            "score": [_f(sum(by_day[d]) / len(by_day[d]), 3) for d in days],
            "sources": sorted(src.items(), key=lambda x: -x[1])[:12],
            "top_pos": sorted([x for x in scored if x["score"] > 0], key=lambda x: (-x["score"], -x["pos"]))[:8],
            "top_neg": sorted([x for x in scored if x["score"] < 0], key=lambda x: (x["score"], -x["neg"]))[:8],
            "items": items[:60]}


def hn_mentions(term, weeks=52):
    term = (term or "").strip()

    def load():
        now = int(time.time())
        week = 7 * 86400

        def count(k):
            a, b = now - (k + 1) * week, now - k * week
            r = SESSION.get("https://hn.algolia.com/api/v1/search", params={
                "query": term, "tags": "story", "hitsPerPage": 0,
                "numericFilters": f"created_at_i>{a},created_at_i<={b}"}, timeout=20)
            r.raise_for_status()
            return b, r.json().get("nbHits", 0)

        futs = [POOL.submit(count, k) for k in range(weeks)]
        pts = sorted(f.result() for f in futs)
        r = SESSION.get("https://hn.algolia.com/api/v1/search", params={
            "query": term, "tags": "story", "hitsPerPage": 12,
            "numericFilters": f"created_at_i>{now - 90 * 86400}"}, timeout=20)
        r.raise_for_status()
        top = [{"title": h.get("title"), "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                "hn": f"https://news.ycombinator.com/item?id={h.get('objectID')}", "points": h.get("points"),
                "comments": h.get("num_comments"), "ts": h.get("created_at_i")} for h in r.json().get("hits", [])]
        return {"term": term, "weeks": [dt.datetime.utcfromtimestamp(b).strftime("%Y-%m-%d") for b, _ in pts],
                "count": [c for _, c in pts], "top": top}

    return CACHE.get("hn:" + term.lower(), 21600, load)


def gdelt(term):
    term = (term or "").strip()

    def call(mode):
        with _GDELT_LOCK:
            wait = 5.5 - (time.time() - _GDELT_LAST[0])
            if wait > 0:
                time.sleep(wait)
            r = WIKI.get("https://api.gdeltproject.org/api/v2/doc/doc",
                         params={"query": f'"{term}"', "mode": mode, "format": "json", "timespan": "3m"}, timeout=30)
            _GDELT_LAST[0] = time.time()
        if r.status_code == 429:
            raise RuntimeError("GDELT is rate-limiting this network right now - try again later")
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            raise RuntimeError("GDELT returned no data for this query") from e

    def load():
        out = {"term": term}
        for mode, key in (("timelinevolraw", "vol"), ("timelinetone", "tone")):
            j = call(mode)
            tl = (j.get("timeline") or [{}])[0].get("data", [])
            out[key] = {"dates": [x["date"][:8] for x in tl], "values": [_f(x.get("value"), 3) for x in tl]}
        return out

    return CACHE.get("gdelt:" + term.lower(), 21600, load)


def _price(sym, period="1y"):
    def load():
        h = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=True)
        h = h.dropna(subset=["Close"])
        idx = pd.to_datetime(h.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        h.index = idx.normalize()
        return h
    return CACHE.get(f"sigpx:{sym}:{period}", 900, load)


def _zscores(views, win=60):
    """Attention z-score: log views, weekday-adjusted (Wikipedia traffic dips every weekend),
    vs the trailing `win`-day mean/std (excluding today)."""
    wd = views.index.dayofweek
    f = views.groupby(wd).transform("mean") / max(views.mean(), 1e-9)
    lv = np.log1p(views / f.replace(0, np.nan).fillna(1))
    mu = lv.rolling(win, min_periods=20).mean().shift(1)
    sd = lv.rolling(win, min_periods=20).std().shift(1)
    return (lv - mu) / sd.replace(0, np.nan)


def signal_overview(sym, title=None):
    sym = sym.upper().strip()
    rl = None
    try:
        meta = resolve(sym)
    except RateLimited as e:
        i = _info(sym)
        nm = i.get("longName") or i.get("shortName") or sym
        meta, rl = {"sym": sym, "name": nm, "short": clean_name(nm), "title": None, "type": i.get("quoteType"),
                    "sector": i.get("sector"), "industry": i.get("industry")}, e
    if title:
        meta["title"] = title
        _save_title(sym, title)
    out = {"meta": meta}
    h = _price(sym, "1y")
    if h.empty:
        raise ValueError(f"No price data for {sym}")
    close, vol = h["Close"], h["Volume"]
    ret = close.pct_change()
    out["price"] = {"labels": _labels(close.index), "close": _series(close, 4), "volume": _series(vol, 0)}
    if not meta["title"]:
        if rl:
            out["views_error"], out["retry"] = str(rl), int(rl.secs) + 1
        else:
            out["views_error"] = "No Wikipedia article found - set one manually"
        return out
    try:
        v = wiki_views(meta["title"], 400)
    except RateLimited as e:
        out["views_error"], out["retry"] = str(e), int(e.secs) + 1
        return out
    except Exception as e:  # noqa: BLE001
        out["views_error"] = f"Wikipedia pageviews unavailable: {e}"
        return out
    z = _zscores(v)
    vv = v[v.index >= close.index[0]]
    zz = z[z.index >= close.index[0]]
    out["views"] = {"labels": _labels(vv.index), "views": _series(vv, 0), "z": _series(zz, 2),
                    "ma30": _series(vv.rolling(30, min_periods=5).mean(), 0)}
    last = v.iloc[-1]
    base = v.iloc[-61:-1]
    out["stats"] = {"last": _f(last, 0), "last_date": v.index[-1].strftime("%Y-%m-%d"), "z": _f(z.iloc[-1], 2),
                    "avg60": _f(base.mean(), 0), "avg7": _f(v.iloc[-7:].mean(), 0),
                    "ratio7_60": _f(v.iloc[-7:].mean() / base.mean(), 2) if base.mean() else None,
                    "peak": _f(vv.max(), 0), "peak_date": vv.idxmax().strftime("%Y-%m-%d") if len(vv) else None,
                    "total_1y": _f(vv.sum(), 0)}
    # align attention with trading days
    zt = z.reindex(close.index)
    absr = ret.abs()
    spikes = []
    for d in zt[zt > 2.5].index:
        i = close.index.get_loc(d)
        fwd = close.iloc[min(i + 5, len(close) - 1)] / close.iloc[i] - 1 if i < len(close) - 1 else None
        spikes.append({"date": d.strftime("%Y-%m-%d"), "views": _f(v.get(d), 0), "z": _f(zt.loc[d], 2),
                       "ret": _f(ret.iloc[i] * 100, 2), "fwd5": _f(fwd * 100, 2) if fwd is not None else None,
                       "volx": _f(vol.iloc[i] / vol.iloc[max(0, i - 20):i].mean(), 2) if i > 5 else None})
    out["spikes"] = spikes[::-1][:25]
    lags = list(range(-5, 6))
    ll_abs, ll_ret = [], []
    for k in lags:
        ll_abs.append(_f(zt.corr(absr.shift(-k)), 3))
        ll_ret.append(_f(zt.corr(ret.shift(-k)), 3))
    out["leadlag"] = {"lags": lags, "abs": ll_abs, "ret": ll_ret,
                      "vol_corr": _f(np.log1p(v.reindex(close.index)).corr(np.log1p(vol)), 3)}
    # spike-day vs normal-day comparison
    if spikes:
        sd = [s["ret"] for s in spikes if s["ret"] is not None]
        out["spike_stats"] = {"n": len(spikes), "avg_abs": _f(np.mean([abs(x) for x in sd]), 2) if sd else None,
                              "normal_abs": _f(absr[zt <= 2.5].mean() * 100, 2),
                              "avg_fwd5": _f(np.mean([s["fwd5"] for s in spikes if s["fwd5"] is not None]), 2)
                              if any(s["fwd5"] is not None for s in spikes) else None}
    e = safe(wiki_edits, meta["title"], 180)
    if isinstance(e, pd.Series):
        out["edits"] = {"labels": _labels(e.index), "count": [int(x) for x in e.values], "total": int(e.sum()),
                        "last30": int(e.iloc[-30:].sum())}
    return out


SCAN_DEFAULT = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "LLY", "NFLX", "AMD", "PLTR",
                "COIN", "BA", "INTC", "DIS", "NKE", "PFE", "XOM", "WMT", "COST", "UNH", "SMCI", "MSTR"]
SCAN_MAX = 150

# Preset universes for the Attention Scanner: key -> (label, tickers)
UNIVERSES = {
    "default": ("Watchlist + Popular", SCAN_DEFAULT),
    "mega": ("Mega Caps", "AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO BRK-B JPM LLY V MA WMT XOM UNH ORCL COST HD PG JNJ NFLX "
                          "BAC ABBV KO CRM AMD PLTR CVX MRK".split()),
    "sp100": ("S&P 100", "AAPL ABBV ABT ACN ADBE AIG AMD AMGN AMT AMZN AVGO AXP BA BAC BK BKNG BLK BMY BRK-B C CAT CHTR CL "
                         "CMCSA COF COP COST CRM CSCO CVS CVX DE DHR DIS DUK EMR F FDX GD GE GILD GM GOOGL GS HD HON IBM INTC "
                         "INTU ISRG JNJ JPM KO LIN LLY LMT LOW MA MCD MDLZ MDT MET META MMM MO MRK MS MSFT NEE NFLX NKE NOW "
                         "NVDA ORCL PEP PFE PG PLTR PM PYPL QCOM RTX SBUX SCHW SO SPG T TGT TMO TMUS TSLA TXN UBER UNH UNP UPS "
                         "USB V VZ WFC WMT XOM".split()),
    "tech": ("Tech & AI", "NVDA MSFT AAPL GOOGL META AMZN ORCL CRM NOW ADBE INTU IBM PLTR SNOW DDOG MDB CRWD PANW ZS NET "
                          "SHOP UBER ABNB DELL HPE ANET SMCI AI PATH TEAM WDAY APP RBLX U".split()),
    "semis": ("Semiconductors", "NVDA AMD AVGO TSM ARM INTC QCOM MU MRVL TXN ADI NXPI ON MCHP LRCX AMAT KLAC ASML SMCI "
                                "GFS WOLF MPWR SWKS COHR".split()),
    "retail": ("Retail Favorites", "GME AMC TSLA PLTR SOFI HOOD RIVN LCID NIO MARA RIOT COIN MSTR RDDT DJT CVNA UPST AFRM "
                                   "BB OPEN CHWY PLUG SPCE RKLB ASTS IONQ RGTI QUBT SOUN BBAI HIMS CELH".split()),
    "crypto": ("Crypto", "BTC-USD ETH-USD SOL-USD XRP-USD DOGE-USD ADA-USD COIN MSTR MARA RIOT CLSK HUT HOOD".split()),
    "fin": ("Banks & Financials", "JPM BAC WFC C GS MS SCHW BLK AXP V MA PYPL COF USB PNC TFC BK SOFI HOOD KKR BX APO "
                                  "ALLY MET".split()),
    "health": ("Healthcare & Biotech", "LLY NVO UNH JNJ ABBV MRK PFE AMGN GILD BMY REGN VRTX MRNA BNTX ISRG TMO DHR ABT "
                                       "MDT CVS CI HUM HIMS".split()),
    "energy": ("Energy & Materials", "XOM CVX COP OXY SLB HAL EOG DVN MPC PSX VLO FCX NEM CCJ OKLO SMR VST CEG NEE ENPH "
                                     "FSLR".split()),
    "consumer": ("Consumer & Retail", "AMZN WMT COST TGT HD LOW NKE LULU SBUX MCD CMG YUM KO PEP PG CL EL DIS NFLX SPOT "
                                      "ABNB BKNG MAR DAL UAL CCL F GM TSLA".split()),
    "defense": ("Defense & Aerospace", "LMT RTX NOC GD LHX BA HII LDOS KTOS AVAV PLTR RKLB TXT HWM GE".split()),
    "china": ("China ADRs", "BABA PDD JD BIDU NIO LI XPEV BILI NTES TME YUMC TCOM ZTO VIPS FUTU BEKE".split()),
}


def universes():
    return {"max": SCAN_MAX, "lists": [{"key": k, "label": lbl, "syms": v} for k, (lbl, v) in UNIVERSES.items()]}


def scan_one(sym):
    """One attention-scanner row. The front end calls this per symbol so rows fill in progressively
    and rate-limited rows can be retried after the cooldown."""
    s = sym.strip().upper()
    try:
        m = resolve(s)
        if not m["title"]:
            return {"sym": s, "name": m["short"], "sector": m.get("sector"), "error": "no Wikipedia article"}
        v = wiki_views(m["title"], 120)
        z = _zscores(v)
        base = v.iloc[-61:-1]
        zz = z.iloc[-30:]
        return {"sym": s, "name": m["short"], "title": m["title"], "sector": m.get("sector") or m.get("type"),
                "last": _f(v.iloc[-1], 0), "z": _f(z.iloc[-1], 2),
                "zmax7": _f(z.iloc[-7:].max(), 2), "z7ago": _f(z.iloc[-8], 2) if len(z) > 8 else None,
                "ratio": _f(v.iloc[-7:].mean() / base.mean(), 2) if base.mean() else None,
                "avg60": _f(base.mean(), 0), "spikes30": int((zz > 2.5).sum()),
                "spark": _series(v.iloc[-60:], 0), "date": v.index[-1].strftime("%Y-%m-%d")}
    except RateLimited as e:
        return {"sym": s, "error": str(e), "retry": int(e.secs) + 1}
    except Exception as e:  # noqa: BLE001
        return {"sym": s, "error": str(e)[:90]}


def scan_prices(syms):
    """Price context for scanner rows in one batched download: 1D / 5D / 1M returns and volume vs 20-day average."""
    syms = list(dict.fromkeys(s.strip().upper() for s in syms if s.strip()))[:SCAN_MAX]
    if not syms:
        return {}

    def load():
        df = yf.download(syms, period="3mo", interval="1d", auto_adjust=True, progress=False,
                         group_by="column", threads=True)
        out = {}
        for s in syms:
            try:
                c = (df["Close"][s] if len(syms) > 1 else df["Close"].squeeze()).dropna()
                v = (df["Volume"][s] if len(syms) > 1 else df["Volume"].squeeze()).reindex(c.index)
                if len(c) < 6:
                    continue
                r = lambda k: _f((c.iloc[-1] / c.iloc[-1 - k] - 1) * 100, 2) if len(c) > k else None  # noqa: E731
                va = v.iloc[-21:-1].mean()
                out[s] = {"px": _f(c.iloc[-1], 2), "r1": r(1), "r5": r(5), "r21": r(21),
                          "volx": _f(v.iloc[-1] / va, 2) if va and va > 0 else None}
            except Exception:  # noqa: BLE001
                continue
        return out

    return CACHE.get("scanpx:" + ",".join(sorted(syms)), 900, load)
