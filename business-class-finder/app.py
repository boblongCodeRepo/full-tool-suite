"""Business Class Finder - business-class round-trip prices from Washington DC, New York and Chicago to Europe and
Asia, using Google Flights data through SerpApi (free account, limited searches per month).

Double-click the Desktop icon (or run `python app.py`). It starts a small local server and opens the app in its own
window; closing the window stops it about a minute later. `--server` just serves (for testing), `--port N` changes
the port.

Your SerpApi key is stored in data/settings.json on this PC and only ever sent to serpapi.com.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import feedparser
import requests

import points

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
STATIC = Path(getattr(sys, "_MEIPASS", HERE)) / "static"


def _data_dir():
    """Portable exe: keep settings/prices in 'BusinessClassFinder-data' next to the exe (travels on a USB stick).
    Otherwise the app folder's data directory."""
    if FROZEN:
        local = HERE / "BusinessClassFinder-data"
        if local.is_dir():
            return local
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "BusinessClassFinder"
    return HERE / "data"


DATA = _data_dir()
DATA.mkdir(parents=True, exist_ok=True)
APP_ID = "BusinessClassFinder"
PORT = 8767
for _i, _a in enumerate(sys.argv):
    if _a == "--port" and _i + 1 < len(sys.argv):
        PORT = int(sys.argv[_i + 1])

if sys.stdout is None or sys.stderr is None:  # pythonw: no console
    _log = open(DATA / "log.txt", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"})

# ------------------------------------------------------------------ places
ORIGINS = {
    "DC": {"name": "Washington DC", "airports": "IAD,DCA,BWI", "kayak": "WAS"},
    "NYC": {"name": "New York", "airports": "JFK,EWR,LGA", "kayak": "NYC"},
    "CHI": {"name": "Chicago", "airports": "ORD,MDW", "kayak": "CHI"},
}
_D = [  # code (also Kayak city code), city, country, region, airports searched, tracked by default
    ("LON", "London", "United Kingdom", "Europe", "LHR,LGW", True),
    ("PAR", "Paris", "France", "Europe", "CDG,ORY", True),
    ("ROM", "Rome", "Italy", "Europe", "FCO", True),
    ("FRA", "Frankfurt", "Germany", "Europe", "FRA", False),
    ("MUC", "Munich", "Germany", "Europe", "MUC", False),
    ("AMS", "Amsterdam", "Netherlands", "Europe", "AMS", False),
    ("MIL", "Milan", "Italy", "Europe", "MXP,LIN", False),
    ("MAD", "Madrid", "Spain", "Europe", "MAD", False),
    ("BCN", "Barcelona", "Spain", "Europe", "BCN", False),
    ("LIS", "Lisbon", "Portugal", "Europe", "LIS", False),
    ("DUB", "Dublin", "Ireland", "Europe", "DUB", False),
    ("ZRH", "Zurich", "Switzerland", "Europe", "ZRH", False),
    ("ATH", "Athens", "Greece", "Europe", "ATH", False),
    ("IST", "Istanbul", "Turkiye", "Europe", "IST", False),
    ("TYO", "Tokyo", "Japan", "Asia", "HND,NRT", True),
    ("SEL", "Seoul", "South Korea", "Asia", "ICN", True),
    ("SIN", "Singapore", "Singapore", "Asia", "SIN", True),
    ("HKG", "Hong Kong", "Hong Kong", "Asia", "HKG", False),
    ("TPE", "Taipei", "Taiwan", "Asia", "TPE", False),
    ("BKK", "Bangkok", "Thailand", "Asia", "BKK", False),
    ("OSA", "Osaka", "Japan", "Asia", "KIX", False),
    ("MNL", "Manila", "Philippines", "Asia", "MNL", False),
    ("SGN", "Ho Chi Minh City", "Vietnam", "Asia", "SGN", False),
    ("DEL", "Delhi", "India", "Asia", "DEL", False),
    ("BOM", "Mumbai", "India", "Asia", "BOM", False),
    ("SHA", "Shanghai", "China", "Asia", "PVG", False),
    ("DPS", "Bali", "Indonesia", "Asia", "DPS", False),
]
DESTS = {c: {"code": c, "city": city, "country": ctry, "region": reg, "airports": ap, "default": dflt}
         for c, city, ctry, reg, ap, dflt in _D}

# ------------------------------------------------------------------ storage
_LOCK = threading.RLock()


def _load(name, default):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _save(name, obj):
    with _LOCK:
        tmp = DATA / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
        tmp.replace(DATA / name)


DEFAULTS = {"api_key": "", "when": "soonest", "depart": "", "nights": 7, "stops": 0, "auto_weekly": True,
            "origins": list(ORIGINS), "tracked": [c for c, d in DESTS.items() if d["default"]], "last_full_check": 0,
            "cards": list(points.CARDS), "flat_only": True, "hotel_cls": "45"}
NOTICE_DAYS = 10  # business days of notice before departure


def _us_holidays(y):
    """US federal holidays (observed) for year y."""
    def nth(month, weekday, n):  # n-th weekday of month (n=-1: last)
        if n > 0:
            d = date(y, month, 1)
            return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
        d = date(y, month + 1, 1) - timedelta(days=1) if month < 12 else date(y, 12, 31)
        return d - timedelta(days=(d.weekday() - weekday) % 7)

    def observed(d):
        return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d
    return {observed(date(y, 1, 1)), nth(1, 0, 3), nth(2, 0, 3), nth(5, 0, -1), observed(date(y, 6, 19)),
            observed(date(y, 7, 4)), nth(9, 0, 1), nth(10, 0, 2), observed(date(y, 11, 11)), nth(11, 3, 4),
            observed(date(y, 12, 25))}


def earliest_departure(today=None):
    """The date that is NOTICE_DAYS business days (Mon-Fri, excluding US federal holidays) after today."""
    d = today or date.today()
    hol = _us_holidays(d.year) | _us_holidays(d.year + 1)
    n = 0
    while n < NOTICE_DAYS:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in hol:
            n += 1
    return d


def settings():
    st = dict(DEFAULTS)
    st.update(_load("settings.json", {}))
    return st


def trip_dates(st):
    """when = 'soonest' (first Tue/Wed at least 10 business days out), 'later' (a Tuesday ~6 weeks out),
    or 'date' (a fixed date, moved up to the earliest allowed date if it is too soon)."""
    earliest = earliest_departure()
    dep = None
    if st.get("when") == "date" and st.get("depart"):
        try:
            dep = date.fromisoformat(st["depart"])
        except ValueError:
            dep = None
    if dep is None:
        if st.get("when") == "later":
            dep = date.today() + timedelta(days=42)
            dep += timedelta(days=(1 - dep.weekday()) % 7)  # Tuesday
        else:
            dep = earliest
            while dep.weekday() not in (1, 2):  # mid-week fares are usually lower
                dep += timedelta(days=1)
    dep = max(dep, earliest)
    ret = dep + timedelta(days=max(1, int(st.get("nights") or 7)))
    return dep.isoformat(), ret.isoformat()


def links(o, d, dep, ret):
    O, D = ORIGINS[o], DESTS[d]
    q = f"Flights from {O['name']} to {D['city']} on {dep} through {ret} business class"
    return {"google": "https://www.google.com/travel/flights?q=" + quote_plus(q) + "&curr=USD",
            "kayak": f"https://www.kayak.com/flights/{O['kayak']}-{d}/{dep}/{ret}/business?sort=price_a"}


# ------------------------------------------------------------------ SerpApi
class SearchError(Exception):
    pass


def account():
    st = settings()
    if not st["api_key"]:
        return {"key": False}
    try:
        r = S.get("https://serpapi.com/account.json", params={"api_key": st["api_key"]}, timeout=20)
        j = r.json()
    except Exception as e:  # noqa: BLE001
        return {"key": True, "error": f"Could not reach SerpApi: {e}"}
    if j.get("error"):
        return {"key": True, "error": j["error"]}
    return {"key": True, "left": j.get("total_searches_left", j.get("plan_searches_left")),
            "used": j.get("this_month_usage"), "per_month": j.get("searches_per_month"), "plan": j.get("plan_name")}


def _mins(m):
    return f"{m // 60}h {m % 60:02d}m" if isinstance(m, int) else ""


def _option(f):
    legs = f.get("flights") or []
    if not legs:
        return None
    exts = [x.lower() for lg in legs for x in (lg.get("extensions") or [])]
    airlines = []
    for lg in legs:
        a = lg.get("airline")
        if a and a not in airlines:
            airlines.append(a)
    return {
        "price": f.get("price"),
        "airlines": airlines,
        "logo": f.get("airline_logo") or legs[0].get("airline_logo"),
        "stops": len(legs) - 1,
        "via": [lo.get("id") or lo.get("name") for lo in (f.get("layovers") or [])],
        "layover_mins": [lo.get("duration") for lo in (f.get("layovers") or [])],
        "duration": _mins(f.get("total_duration")),
        "mins": f.get("total_duration") if isinstance(f.get("total_duration"), int) else None,
        "from": (legs[0].get("departure_airport") or {}).get("id"),
        "to": (legs[-1].get("arrival_airport") or {}).get("id"),
        "dep_time": (legs[0].get("departure_airport") or {}).get("time"),
        "arr_time": (legs[-1].get("arrival_airport") or {}).get("time"),
        "flight_numbers": [lg.get("flight_number") for lg in legs if lg.get("flight_number")],
        "aircraft": [re.sub(r"\s+Passenger$", "", lg.get("airplane")) for lg in legs if lg.get("airplane")],
        "lie_flat": any("lie flat" in x or "lie-flat" in x or "flat bed" in x for x in exts),
        "overnight": any(lg.get("overnight") for lg in legs),
        "extensions": sorted({x for lg in legs for x in (lg.get("extensions") or [])})[:8],
    }


def search(o, d, st=None):
    st = st or settings()
    if not st["api_key"]:
        raise SearchError("Add your free SerpApi key in Settings first.")
    dep, ret = trip_dates(st)
    params = {"engine": "google_flights", "departure_id": ORIGINS[o]["airports"], "arrival_id": DESTS[d]["airports"],
              "outbound_date": dep, "return_date": ret, "type": "1", "travel_class": "3", "adults": "1",
              "currency": "USD", "hl": "en", "gl": "us", "api_key": st["api_key"]}
    if st.get("stops"):
        params["stops"] = str(st["stops"])
    try:
        r = S.get("https://serpapi.com/search.json", params=params, timeout=120)
        j = r.json()
    except Exception as e:  # noqa: BLE001
        raise SearchError(f"Could not reach SerpApi: {e}") from e
    key = f"{o}|{d}"
    rec = {"checked": time.time(), "depart": dep, "return": ret, "stops_pref": st.get("stops", 0),
           "links": links(o, d, dep, ret), "error": None, "options": [], "lowest": None, "insights": {}}
    err = j.get("error")
    if err and "hasn't returned any results" not in err:
        if re.search(r"run out of searches|plan|invalid api key|api key", err, re.I):
            raise SearchError(err)
        rec["error"] = err
    else:
        opts, seen = [], set()
        for x in (_option(f) for f in (j.get("best_flights") or []) + (j.get("other_flights") or [])):
            sig = x and (tuple(x["airlines"]), x["price"], x["dep_time"], x["arr_time"])
            if x and x["price"] and sig not in seen:  # same flights at the same price, different connection numbers
                seen.add(sig)
                opts.append(x)
        opts.sort(key=lambda x: x["price"])
        # Headline = cheapest SENSIBLE option: not more than ~1.7x the fastest trip time, and lie-flat if wanted
        # (the very cheapest "business" fares are often recliner seats, e.g. Icelandair Saga, or odd routings).
        fastest = min((x["mins"] for x in opts if x["mins"]), default=None)
        sensible = [x for x in opts if not fastest or not x["mins"] or x["mins"] <= fastest * 1.7]
        flat = [x for x in sensible if x["lie_flat"]]
        pick = (flat[0] if flat else None) if st.get("flat_only", True) else (sensible[0] if sensible else None)
        if pick is None and not st.get("flat_only", True):
            pick = opts[0] if opts else None
        rest = [x for x in opts if x is not pick]
        for x in opts:
            x["sensible"] = x in sensible
        pi = j.get("price_insights") or {}
        rec.update(options=([pick] if pick else []) + rest[:11], lowest=pick["price"] if pick else None,
                   cheapest_any=opts[0]["price"] if opts else None, cheapest_any_flat=bool(opts and opts[0]["lie_flat"]), insights={
            "lowest": pi.get("lowest_price"), "level": pi.get("price_level"),
            "typical": pi.get("typical_price_range"), "history": pi.get("price_history")})
        if (j.get("search_metadata") or {}).get("google_flights_url"):
            rec["links"]["google"] = j["search_metadata"]["google_flights_url"]
        if not opts:
            rec["error"] = "No business-class flights found for these dates."
        elif not pick:
            rec["error"] = f"No lie-flat option found (cheapest business-class fare was ${opts[0]['price']:,} in a recliner seat)."
    with _LOCK:
        res = _load("results.json", {})
        res[key] = rec
        _save("results.json", res)
        if rec["lowest"]:
            hist = _load("history.json", {})
            hist.setdefault(key, []).append([int(rec["checked"]), rec["lowest"], dep])
            hist[key] = hist[key][-200:]
            _save("history.json", hist)
    return rec


# ------------------------------------------------------------------ hotels (SerpApi google_hotels; 1 search each)
HOTEL_CLASSES = {"5": "5", "45": "4,5", "all": ""}
# where to look: "central <city>" unless the useful area has another name (plain "hotels in London" returns airport
# and suburban hotels)
HOTEL_AREA = {"DPS": "Seminyak, Bali", "MNL": "Makati, Manila", "SGN": "District 1, Ho Chi Minh City", "BOM": "South Mumbai",
              "DEL": "central New Delhi", "OSA": "Umeda, Osaka", "SHA": "the Bund, Shanghai"}
HOTEL_TTL = 24 * 3600


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _hotel(p, nights):
    rate, total = p.get("rate_per_night") or {}, p.get("total_rate") or {}
    night = _num(rate.get("extracted_lowest"))
    srcs = []
    for x in p.get("prices") or []:
        v = _num((x.get("rate_per_night") or {}).get("extracted_lowest"))
        if x.get("source") and v:
            srcs.append({"source": x["source"], "night": v, "official": bool(x.get("official"))})
    near = []
    for n in (p.get("nearby_places") or [])[:3]:
        t = (n.get("transportations") or [{}])[0]
        near.append(f"{n.get('name')}: {t.get('duration', '')} {(t.get('type') or '').lower()}".strip())
    imgs = p.get("images") or []
    return {"name": p.get("name"), "link": p.get("link") or "", "stars": p.get("extracted_hotel_class"),
            "rating": _num(p.get("overall_rating")), "reviews": p.get("reviews"), "location": _num(p.get("location_rating")),
            "night": night, "night_before_tax": _num(rate.get("extracted_before_taxes_fees")),
            "total": _num(total.get("extracted_lowest")) or (night * nights if night else None),
            "deal": p.get("deal") or "", "deal_label": p.get("deal_description") or "",
            "photo": imgs[0].get("thumbnail") if imgs else "", "amenities": (p.get("amenities") or [])[:8],
            "free_cancel": any("free cancellation" in str(x).lower() for x in (p.get("amenities") or [])),
            "near": near, "sources": sorted(srcs, key=lambda s: s["night"])[:4],
            "program": points.hotel_program(p.get("name", "")), "type": p.get("type") or "hotel"}


def hotels(q):
    """Cached hotel results for a destination and the current trip dates; q['search']=1 spends one SerpApi search."""
    st = settings()
    d = q.get("d", "")
    if d not in DESTS:
        return {"error": "Unknown destination"}
    dep, ret = trip_dates(st)
    cls = q.get("cls") if q.get("cls") in HOTEL_CLASSES else st.get("hotel_cls", "45")
    key = f"{d}|{dep}|{ret}|{cls}"
    cache = _load("hotels.json", {})
    hit = cache.get(key)
    nights = (date.fromisoformat(ret) - date.fromisoformat(dep)).days
    base = {"d": d, "check_in": dep, "check_out": ret, "nights": nights, "cls": cls,
            "google": "https://www.google.com/travel/hotels/" + quote_plus(DESTS[d]["city"]) + f"?q={quote_plus(DESTS[d]['city'] + ' hotels')}&checkin={dep}&checkout={ret}"}
    if not q.get("search"):
        if hit:
            return {**base, **hit, "cached": True}
        others = [k for k in cache if k.startswith(d + "|")]
        return {**base, "cached": False, "hotels": [], "older": bool(others)}
    if not st["api_key"]:
        return {"error": "Add your free SerpApi key in Settings first."}
    D = DESTS[d]
    area = HOTEL_AREA.get(d) or f"central {D['city']}, {D['country']}"
    params = {"engine": "google_hotels", "q": f"hotels in {area}", "check_in_date": dep,
              "check_out_date": ret, "adults": "1", "currency": "USD", "gl": "us", "hl": "en", "rating": "8",
              "api_key": st["api_key"]}
    if HOTEL_CLASSES[cls]:
        params["hotel_class"] = HOTEL_CLASSES[cls]
    try:
        j = S.get("https://serpapi.com/search.json", params=params, timeout=120).json()
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not reach SerpApi: {e}"}
    if j.get("error") and "hasn't returned any results" not in j["error"]:
        return {"error": j["error"]}
    hs = [_hotel(p, nights) for p in (j.get("properties") or []) if (p.get("type") or "hotel") == "hotel"]
    hs = [h for h in hs if h["name"] and h["night"]]
    rec = {"checked": time.time(), "hotels": hs,
           "google": (j.get("search_metadata") or {}).get("google_hotels_url") or base["google"]}
    with _LOCK:
        cache = _load("hotels.json", {})
        cache[key] = rec
        for k in sorted(cache, key=lambda k: cache[k].get("checked", 0))[:-60]:   # keep the 60 newest searches
            cache.pop(k, None)
        _save("hotels.json", cache)
    return {**base, **rec, "cached": True}


JOB = {"running": False, "done": 0, "total": 0, "current": "", "errors": [], "started": 0, "finished": 0, "stopped": ""}


def _run_all(routes):
    st = settings()
    JOB.update(running=True, done=0, total=len(routes), errors=[], started=time.time(), stopped="", current="")
    try:
        for o, d in routes:
            JOB["current"] = f"{ORIGINS[o]['name']} to {DESTS[d]['city']}"
            try:
                rec = search(o, d, st)
                if rec.get("error"):
                    JOB["errors"].append(f"{JOB['current']}: {rec['error']}")
            except SearchError as e:
                JOB["stopped"] = str(e)
                break
            JOB["done"] += 1
            time.sleep(0.5)
        if not JOB["stopped"]:
            st2 = _load("settings.json", {})
            st2["last_full_check"] = time.time()
            _save("settings.json", st2)
    finally:
        JOB.update(running=False, current="", finished=time.time())


def check_all(q):
    if JOB["running"]:
        return dict(JOB)
    st = settings()
    if not st["api_key"]:
        raise SearchError("Add your free SerpApi key in Settings first.")
    routes = [(o, d) for d in st["tracked"] if d in DESTS for o in st["origins"] if o in ORIGINS]
    threading.Thread(target=_run_all, args=(routes,), daemon=True).start()
    time.sleep(0.2)
    return dict(JOB)


def check_one(q):
    o, d = q.get("o"), q.get("d")
    if o not in ORIGINS or d not in DESTS:
        raise ValueError("unknown route")
    return search(o, d)


# ------------------------------------------------------------------ deals in the news (no key needed)
BIZ = re.compile(r"business[- ]class|lie-?flat|polaris|delta one|club world|qsuite|premium cabin", re.I)
HOME = re.compile(r"washington|\bd\.?c\.?\b|dulles|\biad\b|baltimore|\bbwi\b|new york|\bnyc\b|\bjfk\b|newark|\bewr\b|"
                  r"chicago|o.hare|\bord\b|\bus\b|\busa\b|u\.s\.|united states|american|north america", re.I)
AWAY = re.compile(r"europe|asia|london|paris|rome|frankfurt|munich|amsterdam|madrid|lisbon|dublin|zurich|athens|istanbul|"
                  r"milan|barcelona|tokyo|japan|seoul|korea|singapore|hong kong|taipei|bangkok|manila|vietnam|india|china|"
                  r"bali|lufthansa|air france|klm|british airways|iberia|tap|turkish|swiss|ita airways|aer lingus|"
                  r"virgin atlantic|ana\b|jal\b|japan airlines|korean air|asiana|eva air|cathay|singapore airlines|"
                  r"starlux|qatar|emirates|etihad|united|delta|american airlines", re.I)
SALEISH = re.compile(r"sale|deal|fare|\$\s?\d|from \$|cheap|discount|price|award|miles|points", re.I)
DEAL_FEEDS = [
    ("Google News", "https://news.google.com/rss/search?q=%22business+class%22+(sale+OR+deal+OR+fares)+(Europe+OR+Asia)+when:21d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News", "https://news.google.com/rss/search?q=%22business+class%22+(%22New+York%22+OR+Chicago+OR+Washington)+(sale+OR+deal+OR+fare)+when:30d&hl=en-US&gl=US&ceid=US:en"),
    ("Fly4free", "https://www.fly4free.com/flight-deals/business-class/feed/"),
    ("The Flight Deal", "https://www.theflightdeal.com/feed/"),
    ("Travel-Dealz", "https://travel-dealz.com/category/flight/business-class/feed/"),
    ("Thrifty Traveler", "https://thriftytraveler.com/feed/"),
    ("One Mile at a Time", "https://onemileatatime.com/feed/"),
    ("View from the Wing", "https://viewfromthewing.com/feed/"),
]
_DEALS = {"at": 0, "items": []}


def deals(q):
    if time.time() - _DEALS["at"] < 3 * 3600 and _DEALS["items"]:
        return {"items": _DEALS["items"]}
    out, seen = [], set()

    def one(src_url):
        src, url = src_url
        try:
            fp = feedparser.parse(S.get(url, timeout=20).content)
        except Exception:  # noqa: BLE001
            return []
        rows = []
        for e in fp.entries[:60]:
            t = re.sub(r"\s+", " ", e.get("title") or "").strip()
            body = re.sub(r"<[^>]+>", " ", e.get("summary") or "")[:1500]
            text = t + " " + (body if src == "The Flight Deal" else "")
            if not (BIZ.search(text) and SALEISH.search(t) and AWAY.search(t)) or re.search(r"expired", t, re.I):
                continue
            if src in ("Fly4free", "Travel-Dealz") and not HOME.search(t):
                continue  # those two are mostly European departures
            tsp = e.get("published_parsed")
            ts = time.mktime(tsp) if tsp else None
            if ts and time.time() - ts > 60 * 86400:
                continue
            outlet = src
            if src == "Google News" and " - " in t:
                t, outlet = t.rsplit(" - ", 1)
            rows.append({"title": t, "url": e.get("link"), "source": outlet, "ts": ts,
                         "price": (re.search(r"[$€£]\s?[\d,]{3,6}", t) or [None])[0]})
        return rows

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        for rows in ex.map(one, DEAL_FEEDS):
            for r in rows:
                k = r["title"].lower()[:70]
                if k not in seen:
                    seen.add(k)
                    out.append(r)
    out.sort(key=lambda r: r["ts"] or 0, reverse=True)
    _DEALS.update(at=time.time(), items=out[:30])
    return {"items": _DEALS["items"]}


# ------------------------------------------------------------------ API
def state(q):
    st = settings()
    dep, ret = trip_dates(st)
    res = _load("results.json", {})
    hist = _load("history.json", {})
    routes = {}
    for d in DESTS:
        for o in ORIGINS:
            k = f"{o}|{d}"
            r = res.get(k)
            if r and (r.get("depart") != dep or r.get("return") != ret):
                r = dict(r, stale_dates=True)  # checked for other dates
            routes[k] = {"result": r, "links": links(o, d, dep, ret), "history": hist.get(k, [])[-60:]}
    return {"origins": ORIGINS, "dests": DESTS, "routes": routes, "job": dict(JOB), "points": points.payload(),
            "zones": {c: points.zone(c, d["region"]) for c, d in DESTS.items()},
            "settings": {"has_key": bool(st["api_key"]), "key_hint": (st["api_key"][:4] + "..." + st["api_key"][-4:]) if len(st["api_key"]) > 10 else "",
                         "when": st["when"], "depart_fixed": st["depart"], "depart": dep, "return": ret, "nights": st["nights"],
                         "stops": st["stops"], "earliest": earliest_departure().isoformat(), "notice_days": NOTICE_DAYS,
                         "auto_weekly": st["auto_weekly"], "origins": st["origins"], "tracked": st["tracked"],
                         "cards": st["cards"], "flat_only": st["flat_only"], "last_full_check": st["last_full_check"],
                         "hotel_cls": st.get("hotel_cls", "45")},
            "hotels_cached": sorted({k.split("|")[0] for k, v in _load("hotels.json", {}).items()
                                     if k.split("|")[1:3] == [dep, ret]})}


def save_settings(q):
    st = _load("settings.json", {})
    if "api_key" in q:
        st["api_key"] = q["api_key"].strip()
    if "depart" in q:
        st["depart"] = q["depart"].strip()
    if "when" in q and q["when"] in ("soonest", "later", "date"):
        st["when"] = q["when"]
    if "cards" in q:
        st["cards"] = [c for c in q["cards"].split(",") if c in points.CARDS]
    if "nights" in q:
        st["nights"] = max(1, min(60, int(q["nights"] or 7)))
    if "stops" in q:
        st["stops"] = int(q["stops"]) if q["stops"] in ("0", "1", "2", "3") else 0
    if "auto_weekly" in q:
        st["auto_weekly"] = q["auto_weekly"] in ("1", "true")
    if "flat_only" in q:
        st["flat_only"] = q["flat_only"] in ("1", "true")
    if q.get("hotel_cls") in HOTEL_CLASSES:
        st["hotel_cls"] = q["hotel_cls"]
    if "tracked" in q:
        st["tracked"] = [c for c in q["tracked"].split(",") if c in DESTS]
    if "origins" in q:
        st["origins"] = [c for c in q["origins"].split(",") if c in ORIGINS] or list(ORIGINS)
    _save("settings.json", st)
    return state({})


LAST_PING = time.time()
ROUTES = {
    "/api/state": state,
    "/api/settings": save_settings,
    "/api/account": lambda q: account(),
    "/api/check": check_one,
    "/api/check_all": check_all,
    "/api/job": lambda q: dict(JOB),
    "/api/deals": deals,
    "/api/hotels": hotels,
    "/api/ping": lambda q: {"ok": True, "app": APP_ID},
}
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        global LAST_PING
        u = urlparse(self.path)
        if u.path.startswith("/api/"):
            LAST_PING = time.time()
            fn = ROUTES.get(u.path)
            if not fn:
                return self._send(404, b'{"error":"not found"}', "application/json")
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            try:
                res, code = fn(q), 200
            except (SearchError, ValueError) as e:
                res, code = {"error": str(e)}, 400
            except Exception as e:  # noqa: BLE001
                res, code = {"error": f"{type(e).__name__}: {e}"}, 500
            return self._send(code, json.dumps(res).encode(), "application/json")
        rel = u.path.lstrip("/") or "index.html"
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        return self._send(200, f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"))


def already_running():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=2) as r:
            return APP_ID in r.read().decode()
    except Exception:  # noqa: BLE001
        return False


def _browsers():
    """Chromium browsers that can open a chromeless --app window (Windows, macOS, Linux)."""
    if sys.platform == "win32":
        return [Path(root) / exe for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                                              os.environ.get("LOCALAPPDATA")) if root
                for exe in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe")]
    if sys.platform == "darwin":
        return [root / app for root in (Path("/Applications"), Path.home() / "Applications")
                for app in ("Google Chrome.app/Contents/MacOS/Google Chrome", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                            "Brave Browser.app/Contents/MacOS/Brave Browser", "Chromium.app/Contents/MacOS/Chromium")]
    import shutil  # noqa: PLC0415
    return [Path(p) for p in (shutil.which(n) for n in ("google-chrome", "chromium", "microsoft-edge", "brave-browser")) if p]


def open_window(url):
    for p in _browsers():
        if p.is_file():
            return subprocess.Popen([str(p), f"--app={url}", f"--user-data-dir={DATA / 'window-profile'}",
                                     "--window-size=1500,950", "--no-first-run", "--no-default-browser-check"])
    webbrowser.open(url)  # no Chromium browser: a normal tab in the default browser (Safari, Firefox...)
    return None


def _auto_weekly():
    st = settings()
    if st["api_key"] and st["auto_weekly"] and time.time() - (st["last_full_check"] or 0) > 7 * 86400:
        try:
            check_all({})
        except Exception:  # noqa: BLE001
            pass


def main():
    url = f"http://127.0.0.1:{PORT}/"
    if already_running():
        if "--server" not in sys.argv:
            open_window(url)
        return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Timer(3, _auto_weekly).start()
    if "--server" in sys.argv:
        print("Serving on", url)
        while True:
            time.sleep(3600)
    proc = open_window(url)
    if proc is not None:
        proc.wait()
    grace = time.time() + 45
    while time.time() < grace or time.time() - LAST_PING < 60 or JOB["running"]:
        time.sleep(5)
    srv.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        import traceback
        (DATA / "crash.txt").write_text(traceback.format_exc(), encoding="utf-8")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"Business Class Finder failed to start:\n{e}", "Business Class Finder", 0x10)
        except Exception:  # noqa: BLE001
            pass
