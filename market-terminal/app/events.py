"""DC Events (/events/) - festive / holiday events around Washington, DC.

Source (public, no account needed):
  * AllEvents.in - city category pages (allevents.in/washington/<category>). Each page lists ~15 event cards
                   (title, next date + time, venue, price) and a JSON-LD block with address, coordinates,
                   end date and organizer for most of them. We read the cards and merge in the JSON-LD.

Crawling is deliberately gentle: one request at a time, ~1.5 s apart, only the listing pages (never the event pages),
results cached on disk for 6 hours. A full crawl is ~25-35 requests and takes about a minute; it runs in the
background and the page shows progress.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import re
import threading
import time
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36 DCEvents/1.0 (personal use)"}
S = requests.Session()
S.headers.update(UA)
DATA_DIR = Path(os.environ.get("MT_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MarketTerminal")
CACHE_FILE = DATA_DIR / "events_cache.json"
TTL = 6 * 3600
DELAY = 1.5          # seconds between requests to allevents.in
BASE = "https://allevents.in/washington/"

# category slug -> (label, how many pages to read at most). Paging stops early once a page adds nothing new.
CATEGORIES = {
    "christmas": ("Christmas", 4),
    "holiday": ("Holiday", 4),
    "christmas-lights": ("Holiday lights", 2),
    "christmas-markets": ("Holiday markets", 2),
    "holiday-markets": ("Holiday markets", 2),
    "christmas-carols": ("Carols & choirs", 2),
    "christmas-parades": ("Parades", 1),
    "pictures-with-santa": ("Santa", 2),
    "breakfast-with-santa": ("Santa", 1),
    "christmas-wreath-workshops": ("Workshops", 1),
    "ice-skating": ("Ice skating", 1),
    "winter": ("Winter", 2),
    "thanksgiving": ("Thanksgiving", 2),
    "hanukkah": ("Hanukkah", 2),
    "kwanzaa": ("Kwanzaa", 1),
    "new-years-eve": ("New Year's Eve", 2),
    "new-year": ("New Year's Eve", 2),
}

# Themes shown as tabs. An event gets a theme from the category page it was found on and/or words in its title.
THEMES = {
    "christmas": ("Christmas", r"christmas|xmas|noel|nutcracker|santa|claus|elf\b|grinch|carol|jingle|yule|tree lighting|mistletoe|north pole|wreath"),
    "lights": ("Lights", r"\blights?\b|illuminat|glow|lantern|zoolights|enchant|aglow|luminar|sparkle"),
    "markets": ("Markets", r"market|bazaar|craft fair|holiday shop|pop-?up shop|makers|artisan|vendors?\b|gift fair"),
    "santa": ("Santa & kids", r"santa|claus|elf\b|elves|kids|children|family|storytime|story time|north pole|reindeer"),
    "music": ("Music & shows", r"carol|choir|chorus|concert|symphony|orchestra|messiah|nutcracker|ballet|christmas carol|"
                               r"cabaret|show|theatre|theater|musical|sing-?along|handel|organ|brass|jazz|drag"),
    "hanukkah": ("Hanukkah", r"hanukkah|chanukah|chanukkah|menorah|latke|dreidel"),
    "kwanzaa": ("Kwanzaa", r"kwanzaa"),
    "newyear": ("New Year's", r"new year|nye\b|countdown|midnight|2027"),
    "thanksgiving": ("Thanksgiving", r"thanksgiving|turkey trot|friendsgiving|gobble"),
    "skating": ("Skating", r"skat|ice rink"),
    "party": ("Holiday parties", r"holiday|festive|ugly sweater|winter ?wonderland|seasonal|merry|tinsel|gingerbread|eggnog|cocoa|"
                                 r"snowflake|sleigh"),
}
_SLUG_THEME = {"christmas": "christmas", "christmas-lights": "lights", "christmas-markets": "markets", "holiday-markets": "markets",
               "christmas-carols": "music", "christmas-parades": "christmas", "pictures-with-santa": "santa",
               "breakfast-with-santa": "santa", "christmas-wreath-workshops": "christmas", "ice-skating": "skating",
               "thanksgiving": "thanksgiving", "hanukkah": "hanukkah", "kwanzaa": "kwanzaa", "new-years-eve": "newyear",
               "new-year": "newyear"}
_THEME_RX = {k: re.compile(v[1], re.I) for k, v in THEMES.items()}

# AllEvents city slugs inside the DC metro (events in other cities sometimes show up on the Washington pages).
METRO_SLUGS = {"washington", "washington-dc", "arlington", "alexandria", "bethesda", "silver-spring", "rockville", "chevy-chase",
               "takoma-park", "hyattsville", "college-park", "national-harbor", "oxon-hill", "fort-washington", "falls-church",
               "mclean", "tysons", "vienna", "fairfax", "reston", "herndon", "annandale", "springfield", "gaithersburg",
               "bowie", "greenbelt", "laurel", "upper-marlboro", "capitol-heights", "landover", "largo", "mount-rainier",
               "kensington", "wheaton", "potomac", "merrifield", "chantilly", "lorton", "woodbridge", "germantown",
               "columbia", "leesburg", "ashburn", "sterling", "manassas", "annapolis"}
DC = (38.8977, -77.0365)
MAX_MILES = 35

_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_LOCK = threading.Lock()
_STATE = {"running": False, "done": 0, "total": 0, "current": "", "error": None}
_DATA = {"updated": 0, "items": [], "pages": {}}


def _txt(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _miles(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _parse_when(s, today):
    """'Thu, 19 Nov, 2026 - 07:30 PM' or 'Mon, 16 Nov • 04:30 PM + 57 more' -> ('2026-11-19T19:30', more_dates)."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*,?\s*(\d{4})?(?:\D+?(\d{1,2}):(\d{2})\s*([AP]M))?", s or "", re.I)
    if not m:
        return None, 0
    day, mon, year = int(m.group(1)), _MON.get(m.group(2).lower()[:3]), m.group(3)
    if not mon:
        return None, 0
    if year:
        y = int(year)
    else:  # no year shown: it's the next occurrence, so roll into next year if the date is well in the past
        y = today.year
        try:
            if dt.date(y, mon, day) < today - dt.timedelta(days=60):
                y += 1
        except ValueError:
            return None, 0
    hh = mm = None
    if m.group(4):
        hh, mm = int(m.group(4)) % 12 + (12 if m.group(6).upper() == "PM" else 0), int(m.group(5))
    try:
        d = dt.datetime(y, mon, day, hh or 0, mm or 0)
    except ValueError:
        return None, 0
    more = re.search(r"\+\s*(\d+)\s*more", s or "")
    return (d.strftime("%Y-%m-%dT%H:%M") if hh is not None else d.strftime("%Y-%m-%d")), int(more.group(1)) if more else 0


def _price(block):
    t = _txt(block)
    if not t:
        return None, None
    if re.search(r"\bfree\b", t, re.I):
        return 0.0, "Free"
    m = re.search(r"([A-Z]{3})\s*([\d,.]+)", t)
    if m:
        try:
            return float(m.group(2).replace(",", "")), f"{'$' if m.group(1) == 'USD' else m.group(1) + ' '}{m.group(2)}"
        except ValueError:
            pass
    return None, t[:30]


def _jsonld(page):
    """eid -> JSON-LD Event dict."""
    out = {}
    for blob in re.findall(r'<script type="application/ld\+json">([\s\S]*?)</script>', page):
        try:
            j = json.loads(blob)
        except ValueError:
            continue
        for e in j if isinstance(j, list) else [j]:
            if isinstance(e, dict) and e.get("@type") == "Event" and e.get("url"):
                m = re.search(r"/(\d{6,})/?$", e["url"])
                if m:
                    out[m.group(1)] = e
    return out


def parse_page(page, slug, today=None):
    today = today or dt.date.today()
    ld = _jsonld(page)
    items = []
    # each card runs from one <li class="event-card ..."> to the next
    starts = [m.start() for m in re.finditer(r'<li class="event-card', page)]
    for i, st in enumerate(starts):
        card = page[st: starts[i + 1] if i + 1 < len(starts) else st + 8000]
        eid = re.search(r'data-eid="(\d+)"', card)
        link = re.search(r'data-link="(https://allevents\.in/[^"]+)"', card)
        if not eid or not link:
            continue
        eid = eid.group(1)
        name = re.search(r'data-name="([^"]*)"', card)
        title = html.unescape(name.group(1)) if name else _txt((re.search(r"<h3>([\s\S]*?)</h3>", card) or [None, ""])[1])
        img = re.search(r"background:url\((https://[^)]+)\)", card)
        when_s = _txt((re.search(r'<div class="date"[^>]*>([\s\S]*?)</div>', card) or [None, ""])[1])
        venue = _txt((re.search(r'<div class="location[^"]*"[^>]*>([\s\S]*?)</div>', card) or [None, ""])[1])
        pblock = (re.search(r'<div class="price-container">([\s\S]*?)</div>\s*</div>', card) or [None, ""])[1]
        price, price_s = _price(pblock)
        start, more = _parse_when(when_s, today)
        city_slug = link.group(1).split("/")[3]
        e = ld.get(eid) or {}
        loc = e.get("location") or {}
        addr = loc.get("address") or {}
        geo = loc.get("geo") or {}
        try:
            lat, lon = float(geo.get("latitude")), float(geo.get("longitude"))
        except (TypeError, ValueError):
            lat = lon = None
        org = e.get("organizer") or []
        org = org[0] if isinstance(org, list) and org else org if isinstance(org, dict) else {}
        if not start and e.get("startDate"):
            start = str(e["startDate"])[:16]
        items.append({
            "id": eid, "title": title.strip(), "url": link.group(1), "img": img.group(1) if img else e.get("image"),
            "start": start, "end": str(e.get("endDate") or "")[:16] or None, "more": more, "when_text": when_s,
            "venue": venue or loc.get("name") or "", "address": addr.get("streetAddress") or "",
            "region": addr.get("addressRegion") or "", "city": addr.get("addressLocality") or "", "city_slug": city_slug,
            "lat": lat, "lon": lon, "price": price, "price_text": price_s,
            "organizer": html.unescape(org.get("name") or ""),
            "online": "Online" in str(e.get("eventAttendanceMode") or ""),
            "slugs": [slug],
        })
    return items


def _in_metro(it):
    if it["lat"] is not None and it["lon"] is not None:
        return _miles(DC, (it["lat"], it["lon"])) <= MAX_MILES
    return it["city_slug"] in METRO_SLUGS


def _finish(it, today):
    """Themes, DC-proper flag, distance; None if the event is over or out of area."""
    if it["online"] or not _in_metro(it):
        return None
    last = (it["end"] or it["start"] or "")[:10]
    if last and last < today.isoformat() and not it["more"]:
        return None
    themes = []
    for s in it["slugs"]:
        t = _SLUG_THEME.get(s)
        if t and t not in themes:
            themes.append(t)
    blob = " ".join([it["title"], it["venue"], it["organizer"]])
    for k, rx in _THEME_RX.items():
        if k not in themes and rx.search(blob):
            themes.append(k)
    it["themes"] = themes
    it["dc"] = it["region"] == "DC" or (not it["region"] and it["city_slug"] in ("washington", "washington-dc"))
    it["miles"] = round(_miles(DC, (it["lat"], it["lon"])), 1) if it["lat"] is not None else None
    it["cats"] = sorted({CATEGORIES[s][0] for s in it["slugs"] if s in CATEGORIES})
    return it


def _get(url):
    r = S.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def crawl():
    """Read every category page (sequentially, gently) and rebuild the event list."""
    with _LOCK:
        if _STATE["running"]:
            return
        _STATE.update(running=True, done=0, total=sum(p for _, p in CATEGORIES.values()), current="", error=None)
    try:
        today = dt.date.today()
        found, pages, errors = {}, {}, []
        for slug, (_label, maxp) in CATEGORIES.items():
            n_slug = 0
            for p in range(1, maxp + 1):
                url = BASE + slug + (f"?page={p}" if p > 1 else "")
                _STATE["current"] = f"{slug} p{p}"
                try:
                    got = parse_page(_get(url), slug, today)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{slug} p{p}: {type(e).__name__}: {e}"[:160])
                    got = []
                fresh = 0
                for it in got:
                    if it["id"] in found:
                        if slug not in found[it["id"]]["slugs"]:
                            found[it["id"]]["slugs"].append(slug)
                    else:
                        found[it["id"]] = it
                        fresh += 1
                n_slug += len(got)
                _STATE["done"] += 1
                time.sleep(DELAY)
                if not got or not fresh:   # empty page or nothing new: no point reading further pages
                    _STATE["done"] += maxp - p
                    break
            pages[slug] = n_slug
        items = [x for x in (_finish(it, today) for it in found.values()) if x]
        items.sort(key=lambda i: i["start"] or "9999")
        _DATA.update(updated=time.time(), items=items, pages=pages, errors=errors)
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(_DATA), encoding="utf-8")
        except OSError:
            pass
        if errors and not items:
            _STATE["error"] = errors[0]
    except Exception as e:  # noqa: BLE001
        _STATE["error"] = f"{type(e).__name__}: {e}"
    finally:
        _STATE.update(running=False, current="")


def _load_cache():
    if not _DATA["updated"] and CACHE_FILE.is_file():
        try:
            _DATA.update(json.loads(CACHE_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass


def _start():
    if not _STATE["running"]:
        threading.Thread(target=crawl, daemon=True).start()
        _STATE["running"] = True  # so the reply below already says "crawling"


# ----------------------------------------------------------------------------------------------- API
def listing(q=None):
    _load_cache()
    if not _STATE["running"] and time.time() - _DATA["updated"] > TTL:
        _start()
    today = dt.date.today().isoformat()
    items = [i for i in _DATA["items"] if (i.get("end") or i.get("start") or "9")[:10] >= today or i.get("more")]
    return {"items": items, "updated": _DATA["updated"], "pages": _DATA.get("pages", {}), "errors": _DATA.get("errors", []),
            "themes": [{"key": k, "label": v[0]} for k, v in THEMES.items()],
            "crawl": dict(_STATE)}


def refresh(q=None):
    _start()
    return {"crawl": dict(_STATE)}
