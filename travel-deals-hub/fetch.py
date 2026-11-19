"""Collects this week's travel deals and saves them as a weekly snapshot.

Run directly (the Sunday scheduled task does this) or call collect() from the app.
Snapshots live in data/snapshots/<week Sunday>.json; re-running in the same week overwrites that week.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import pathlib
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import feedparser

HERE = pathlib.Path(__file__).resolve().parent
SNAP_DIR = HERE / "data" / "snapshots"
SETTINGS = json.loads((HERE / "settings.json").read_text(encoding="utf-8"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36 TravelDealsHub/1.0")

TAG_PATTERNS = {
    "flight": r"\b(air ?fares?|fares?|round[- ]?trip|one[- ]way|nonstop|flights? (?:to|from)|error fare|"
              r"award (?:sale|space|availability)|saver awards?|business class|first class|premium economy|"
              r"companion (?:fare|certificate|pass)|basic economy|airfare sale|fare sale)\b",
    "hotel": r"\b(hotels?|resorts?|marriott|bonvoy|hilton|hyatt|ihg|wyndham|choice privileges|accor|"
             r"four seasons|ritz|st\.? regis|waldorf|conrad|park hyatt|free nights?|per night|"
             r"suite upgrade|airbnb|vrbo|all[- ]inclusive)\b",
    "united": r"\b(united(?! states| kingdom| arab)|mileageplus|polaris)\b",
    "delta": r"\b(delta|skymiles|sky club|delta one)\b",
    "amex": r"\b(amex|american express|membership rewards|centurion|platinum card)\b",
    "experience": r"\b(experiences?|exclusives?|presales?|auctions?|tickets?|concerts?|"
                  r"once[- ]in[- ]a[- ]lifetime|vip|suite)\b",
}
TAG_RE = {k: re.compile(v, re.I) for k, v in TAG_PATTERNS.items()}
DEAL_RE = re.compile(r"(\$\s?\d|\d+\s?%|\b(deals?|sale|off|bonus|promo(?:tion)?|offers?|discount|cheap (?:flights?|fares?)|"
                     r"under \$|from \d|free nights?|\d+x|double|triple|save|error fare|flash|award sale|"
                     r"roundtrip|round-trip|each way|book by)\b)", re.I)
NOT_DEAL_RE = re.compile(r"\b(review|reviews|trip report|lawsuit|strike|crash|dies|arrested|opinion|"
                         r"million|billion)\b", re.I)
TRAVEL_TAGS = {"flight", "hotel", "united", "delta", "amex"}

DESTINATIONS = [
    "Tokyo", "Osaka", "Japan", "Seoul", "Korea", "Taipei", "Bangkok", "Thailand", "Singapore", "Bali", "Vietnam",
    "Hanoi", "Ho Chi Minh", "Manila", "Philippines", "Hong Kong", "India", "Delhi", "Mumbai", "Maldives", "Sri Lanka",
    "Sydney", "Melbourne", "Australia", "New Zealand", "Auckland", "Fiji", "Tahiti",
    "London", "Paris", "Rome", "Milan", "Venice", "Florence", "Italy", "Madrid", "Barcelona", "Spain", "Lisbon",
    "Porto", "Portugal", "Dublin", "Ireland", "Edinburgh", "Scotland", "Amsterdam", "Brussels", "Frankfurt", "Munich",
    "Berlin", "Germany", "Zurich", "Switzerland", "Vienna", "Prague", "Budapest", "Copenhagen", "Stockholm", "Oslo",
    "Helsinki", "Iceland", "Reykjavik", "Athens", "Greece", "Santorini", "Istanbul", "Turkey", "Croatia", "Dubrovnik",
    "Dubai", "Abu Dhabi", "Doha", "Qatar", "Israel", "Tel Aviv", "Egypt", "Cairo", "Morocco", "Marrakech",
    "South Africa", "Cape Town", "Kenya", "Nairobi", "Tanzania",
    "Cancun", "Mexico City", "Mexico", "Cabo", "Puerto Vallarta", "Tulum", "Costa Rica", "Panama", "Belize",
    "Guatemala", "Colombia", "Bogota", "Medellin", "Cartagena", "Peru", "Lima", "Cusco", "Chile", "Santiago",
    "Buenos Aires", "Argentina", "Brazil", "Rio", "Sao Paulo", "Ecuador", "Galapagos",
    "Caribbean", "Bahamas", "Nassau", "Jamaica", "Aruba", "Puerto Rico", "San Juan", "Dominican Republic",
    "Punta Cana", "St. Thomas", "St. Lucia", "Barbados", "Turks and Caicos", "Bermuda", "Curacao", "Grand Cayman",
    "Hawaii", "Honolulu", "Maui", "Kauai", "Alaska", "Las Vegas", "Orlando", "Miami", "Fort Lauderdale", "Tampa",
    "New York", "Boston", "Chicago", "Denver", "Los Angeles", "San Francisco", "Seattle", "San Diego", "Phoenix",
    "Austin", "Dallas", "Houston", "Nashville", "New Orleans", "Atlanta", "Salt Lake City", "Portland",
    "Charleston", "Savannah", "Key West", "Canada", "Toronto", "Vancouver", "Montreal", "Banff",
]
REGIONS = {
    "Europe": ["Europe", "London", "Paris", "Rome", "Milan", "Venice", "Florence", "Italy", "Madrid", "Barcelona",
               "Spain", "Lisbon", "Porto", "Portugal", "Dublin", "Ireland", "Edinburgh", "Scotland", "Amsterdam",
               "Brussels", "Frankfurt", "Munich", "Berlin", "Germany", "Zurich", "Switzerland", "Vienna", "Prague",
               "Budapest", "Copenhagen", "Stockholm", "Oslo", "Helsinki", "Iceland", "Reykjavik", "Athens", "Greece",
               "Santorini", "Istanbul", "Croatia", "Dubrovnik", "Warsaw", "Poland", "Malaga", "France", "UK",
               "England", "Belgium", "Netherlands", "Denmark", "Norway", "Sweden", "Finland", "Austria", "Hungary"],
    "Asia": ["Asia", "Tokyo", "Osaka", "Japan", "Seoul", "Korea", "Taipei", "Taiwan", "Bangkok", "Thailand",
             "Singapore", "Bali", "Indonesia", "Vietnam", "Hanoi", "Ho Chi Minh", "Manila", "Philippines", "Cebu",
             "Hong Kong", "India", "Delhi", "Mumbai", "Hyderabad", "China", "Surabaya", "Maldives", "Sri Lanka"],
    "Caribbean": ["Caribbean", "Bahamas", "Nassau", "Jamaica", "Montego Bay", "Aruba", "Puerto Rico", "San Juan",
                  "Dominican Republic", "Punta Cana", "St. Thomas", "St. Lucia", "Barbados", "Turks and Caicos",
                  "Bermuda", "Curacao", "Grand Cayman", "Saint Vincent", "Antigua", "Virgin Islands"],
    "Latin America": ["Mexico", "Cancun", "Cabo", "Puerto Vallarta", "Tulum", "Costa Rica", "Liberia", "Panama",
                      "Belize", "Guatemala", "Colombia", "Bogota", "Medellin", "Cartagena", "Peru", "Lima", "Cusco",
                      "Chile", "Santiago", "Buenos Aires", "Argentina", "Brazil", "Rio", "Sao Paulo", "Ecuador"],
    "Hawaii": ["Hawaii", "Honolulu", "Maui", "Kauai", "Kona", "Lihue"],
    "Africa & Middle East": ["Africa", "Morocco", "Casablanca", "Marrakech", "Tangiers", "Egypt", "Cairo",
                             "South Africa", "Cape Town", "Kenya", "Nairobi", "Tanzania", "Dubai", "Abu Dhabi",
                             "Doha", "Qatar", "Israel", "Tel Aviv", "Jordan"],
    "Oceania": ["Australia", "Sydney", "Melbourne", "New Zealand", "Auckland", "Fiji", "Tahiti"],
}

PROGRAMS = ["United", "Delta", "American Airlines", "Southwest", "JetBlue", "Alaska Airlines", "Air Canada",
            "British Airways", "Air France", "Lufthansa", "Emirates", "Qatar Airways", "Singapore Airlines",
            "Amex", "Chase", "Capital One", "Citi", "Bilt", "Marriott", "Hilton", "Hyatt", "IHG"]


def http_get(url: str, timeout: int = 25, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def week_sunday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def parse_money(text: str) -> int | None:
    vals = [int(m.replace(",", "")) for m in re.findall(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?", text)]
    vals = [v for v in vals if 9 <= v <= 20000]
    return min(vals) if vals else None


def parse_miles(text: str) -> int | None:
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d)?)\s*(k)?\s*(?:miles|points|skymiles|avios)\b", text, re.I)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(n * 1000) if m.group(2) else int(n)


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
DEADLINE_RE = re.compile(
    r"\b(?:through|thru|until|till|ends?|expires?|expiring|valid (?:through|until)|book by|by|before|deadline)\s+"
    r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?"
    r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>20\d\d))?", re.I)
DEADLINE_NUM_RE = re.compile(r"\b(?:through|thru|until|ends?|expires?|by)\s+(?P<m>\d{1,2})/(?P<d>\d{1,2})"
                             r"(?:/(?P<y>\d{2,4}))?", re.I)
TONIGHT_RE = re.compile(r"\b(ends? (?:tonight|today)|today only|last (?:day|chance)|final day|hurry)\b", re.I)
TOMORROW_RE = re.compile(r"\bends? tomorrow\b", re.I)
EXPIRED_RE = re.compile(r"^\(?expired\)?|\(expired\)", re.I)


def parse_deadline(text: str, published: dt.datetime) -> tuple[str | None, bool]:
    """Returns (deadline as YYYY-MM-DD or None, already expired)."""
    if EXPIRED_RE.search(text):
        return None, True
    pub = published.astimezone().date()
    found: dt.date | None = None
    # "for travel through March 10" is a travel window, not the deadline to book
    m = next((x for x in DEADLINE_RE.finditer(text)
              if not re.search(r"travel|flights?|fly|stay|departures?", text[max(0, x.start() - 30):x.start()], re.I)),
             None)
    if m:
        mon = MONTHS[m["mon"][:3].lower()]
        year = int(m["year"]) if m["year"] else pub.year
        try:
            found = dt.date(year, mon, int(m["day"]))
            if not m["year"] and found < pub - dt.timedelta(days=30):
                found = dt.date(year + 1, mon, int(m["day"]))
        except ValueError:
            found = None
    if not found:
        n = DEADLINE_NUM_RE.search(text)
        if n:
            y = int(n["y"]) if n["y"] else pub.year
            y = y + 2000 if y < 100 else y
            try:
                found = dt.date(y, int(n["m"]), int(n["d"]))
                if not n["y"] and found < pub - dt.timedelta(days=30):
                    found = dt.date(y + 1, int(n["m"]), int(n["d"]))
            except ValueError:
                found = None
    if not found and TOMORROW_RE.search(text):
        found = pub + dt.timedelta(days=1)
    if not found and TONIGHT_RE.search(text):
        found = pub
    return (found.isoformat() if found else None), False


TRANSFER_RE = re.compile(r"\btransfer(?:s|ring)? (?:bonus|bonuses)\b|\b\d{2,3}% (?:transfer )?bonus\b.*\btransfer|"
                         r"\btransfer\b.*\b\d{2,3}% bonus\b|\bbonus (?:when|on) (?:you )?transfer", re.I)
TRANSFER_FROM = {"Amex MR": r"\b(amex|american express|membership rewards)\b",
                 "Chase UR": r"\b(chase|ultimate rewards)\b", "Citi TY": r"\b(citi|thankyou)\b",
                 "Capital One": r"\bcapital one\b", "Bilt": r"\bbilt\b", "Wells Fargo": r"\bwells fargo\b"}
TRANSFER_TO = ["Virgin Atlantic", "Virgin Red", "Flying Blue", "Air France", "KLM", "British Airways", "Avios",
               "Iberia", "Aer Lingus", "Aeroplan", "Air Canada", "Avianca", "LifeMiles", "Delta", "JetBlue",
               "Cathay", "Asia Miles", "Emirates", "Etihad", "Qantas", "Singapore", "KrisFlyer", "ANA", "Hilton",
               "Marriott", "Hyatt", "IHG", "Choice", "Wyndham", "Accor", "Qatar", "Turkish", "Air India",
               "Southwest", "Finnair", "Atmos", "Alaska", "Aeromexico", "Hawaiian", "United"]


def parse_transfer(title: str) -> dict | None:
    if not TRANSFER_RE.search(title):
        return None
    pct = re.search(r"(\d{2,3})%", title)
    return {
        "from": [k for k, rx in TRANSFER_FROM.items() if re.search(rx, title, re.I)],
        "to": [p for p in TRANSFER_TO if re.search(rf"\b{re.escape(p)}\b", title, re.I)],
        "pct": int(pct.group(1)) if pct else None,
    }


STOP = set("this that with from your have will what when which their there about after into more than they them "
           "been were also just only over very some most best deal deals sale new get now how why the and for are "
           "can you all its it's out off".split())


def signature(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9$%]+", title.lower()) if len(w) >= 4 and w not in STOP}


def merge_duplicates(items: list[dict]) -> list[dict]:
    """Collapses the same deal reported by several blogs into one item with 'also' links."""
    kept: list[dict] = []
    for it in items:
        sig = signature(it["title"])
        twin = None
        for k in kept:
            if k["source"] == it["source"]:
                continue
            same_fare = (it.get("dest") and it.get("dest") == k.get("dest")
                         and it.get("price_usd") and it.get("price_usd") == k.get("price_usd"))
            shared = sig & k["_sig"]
            if same_fare or (len(shared) >= 4 and len(shared) / max(1, len(sig | k["_sig"])) >= 0.5):
                twin = k
                break
        if twin:
            twin.setdefault("also", []).append({"source": it["source"], "link": it["link"], "title": it["title"]})
            twin["tags"] = sorted(set(twin["tags"]) | set(it["tags"]))
            for f in ("price_usd", "miles", "deadline", "transfer"):
                if not twin.get(f) and it.get(f):
                    twin[f] = it[f]
            twin["home"] = twin.get("home") or it.get("home")
        else:
            it["_sig"] = sig
            kept.append(it)
    for k in kept:
        k.pop("_sig", None)
        k.setdefault("also", [])
    return kept


def region_of(text: str) -> list[str]:
    return [r for r, places in REGIONS.items() if any(re.search(rf"\b{re.escape(p)}\b", text, re.I) for p in places)]


FLIGHT_DEAL_RE = re.compile(
    r"^(?P<airline>[^:]{2,60}):\s*(?P<origin>.+?)\s+[–—-]\s+(?P<dest>.+?)\.\s+\$(?P<price>[\d,]+)")


def build_item(entry, feed: dict, now: dt.datetime) -> dict | None:
    title = clean(entry.get("title", ""))
    link = entry.get("link", "")
    if not title or not link.startswith(("http://", "https://")):
        return None
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    published = dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else now
    if (now - published).days > SETTINGS["keep_days"]:
        return None
    summary = clean(entry.get("summary", ""))[:280]

    tags = {k for k, rx in TAG_RE.items() if rx.search(title)}
    is_deal = bool(DEAL_RE.search(title)) and not NOT_DEAL_RE.search(title)
    if not is_deal:
        tags -= {"flight", "hotel"}
    tags.update(feed.get("always", []))
    if feed.get("travel_only") and not (tags & TRAVEL_TAGS):
        return None
    if not tags:
        tags.add("news")

    item = {
        "title": title, "link": link, "source": feed["name"], "published": published.isoformat(),
        "summary": summary, "tags": sorted(tags), "price_usd": parse_money(title),
        "miles": parse_miles(title), "origin": None, "dest": None, "airline": None,
    }
    m = FLIGHT_DEAL_RE.match(title)
    if m:
        item.update(airline=m["airline"].strip(), origin=m["origin"].strip(), dest=m["dest"].strip(),
                    price_usd=int(m["price"].replace(",", "")))
        item["tags"] = sorted(set(item["tags"]) | {"flight"})
    probe = item["origin"] or title
    item["home"] = any(re.search(rf"\b{re.escape(k)}", probe, re.I) for k in SETTINGS["home_keywords"])
    item["deadline"], item["expired"] = parse_deadline(f"{title} {summary}", published)
    item["transfer"] = parse_transfer(title)
    if item["transfer"]:
        item["tags"] = sorted(set(item["tags"]) - {"news"} | {"transfer"})
    item["regions"] = region_of(item["dest"] or title)
    return item


def fetch_feed(feed: dict, now: dt.datetime) -> tuple[dict, list[dict]]:
    try:
        parsed = feedparser.parse(http_get(feed["url"], accept="application/rss+xml, application/xml, */*"))
        items = [i for i in (build_item(e, feed, now) for e in parsed.entries) if i]
        return {"name": feed["name"], "ok": True, "count": len(items), "error": ""}, items
    except Exception as e:  # one bad source must not sink the weekly run
        return {"name": feed["name"], "ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"[:200]}, []


CARD_SPLIT = re.compile(r'<div class="exclusive-content">')


def parse_united(page: str) -> list[dict]:
    out = []
    chunks = CARD_SPLIT.split(page)
    for prev, chunk in zip(chunks, chunks[1:]):
        title_m = re.search(r'<a href="(/Listing/Details/[^"]+)" class="exclusive-title">\s*(.*?)\s*</a>', chunk, re.S)
        if not title_m:
            continue
        info = [clean(x) for x in re.findall(r'<div class="exclusive-info-text">(.*?)</div>', chunk, re.S)]
        label = re.search(r'<p class="bid-title">(.*?)</p>', chunk, re.S)
        miles = re.search(r'<div class="bid-miles">(.*?)</div>', chunk, re.S)
        bids = re.search(r'<div class="bid-number">(.*?)</div>', chunk, re.S)
        tail = prev[-2500:]
        img = re.findall(r"<img src='(/Content/listingImages/[^']+)'", tail)
        badges = [clean(b) for b in re.findall(r'<div class="exclusive-(?:category|ending-soon)">(.*?)</div>', tail, re.S)]
        miles_txt = clean(miles.group(1)) if miles else ""
        out.append({
            "title": clean(title_m.group(2)),
            "link": "https://exclusives.mileageplus.com" + title_m.group(1),
            "date": info[0] if info else "",
            "location": info[1] if len(info) > 1 else "",
            "label": clean(label.group(1)).rstrip(":") if label else "",
            "miles_text": miles_txt,
            "miles": int(re.sub(r"\D", "", miles_txt)) if re.search(r"\d", miles_txt) else None,
            "bids": clean(bids.group(1)) if bids else "",
            "badges": badges,
            "image": "https://exclusives.mileageplus.com" + img[-1] if img else "",
        })
    return out


def fetch_united() -> tuple[dict, list[dict]]:
    seen, listings, errors = set(), [], []
    for url in SETTINGS["united_exclusives_pages"]:
        try:
            page = http_get(url, accept="text/html").decode("utf-8", "replace")
            for l in parse_united(page):
                if l["link"] not in seen:
                    seen.add(l["link"])
                    listings.append(l)
        except Exception as e:
            errors.append(f"{url.rsplit('/', 1)[-1]}: {type(e).__name__}")
        time.sleep(1)  # be polite: one page at a time
    status = {"name": "United MileagePlus Exclusives", "ok": bool(listings), "count": len(listings),
              "error": "; ".join(errors)[:200]}
    return status, listings


def count_mentions(items: list[dict], words: list[str]) -> dict[str, int]:
    counts = {}
    for w in words:
        rx = re.compile(rf"\b{re.escape(w)}\b", re.I)
        n = sum(1 for i in items if rx.search(i["dest"] or i["title"]))
        if n:
            counts[w] = n
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def collect(verbose: bool = False, run_hooks: bool = True) -> pathlib.Path:
    now = dt.datetime.now(dt.timezone.utc)
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda f: fetch_feed(f, now), SETTINGS["feeds"]))
    sources = [s for s, _ in results]
    seen, items = set(), []
    for _, its in results:
        for i in its:
            if i["link"] not in seen:
                seen.add(i["link"])
                items.append(i)
    items.sort(key=lambda i: i["published"], reverse=True)
    items = merge_duplicates(items)

    u_status, united = fetch_united()
    sources.append(u_status)

    week_items = [i for i in items if (now - dt.datetime.fromisoformat(i["published"])).days < 7]
    local_now = dt.datetime.now()
    snap = {
        "fetched_at": local_now.isoformat(timespec="seconds"),
        "week_of": week_sunday(local_now.date()).isoformat(),
        "sources": sources,
        "items": items,
        "united_exclusives": united,
        "destinations": count_mentions(week_items, DESTINATIONS),
        "programs": count_mentions(week_items, PROGRAMS),
    }
    path = save_snapshot(snap)
    if verbose:
        for s in sources:
            print(f"{'OK ' if s['ok'] else 'ERR'} {s['name']:<32} {s['count']:>3}  {s['error']}")
        print(f"Saved {len(items)} items, {len(united)} United listings -> {path}")
    if run_hooks:
        after_collect(snap, verbose)
    return path


def save_snapshot(snap: dict) -> pathlib.Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / f"{snap['week_of']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def after_collect(snap: dict, verbose: bool = False) -> None:
    """Fare alerts, the weekly AI summary and the email digest. Each step fails on its own."""
    import alerts
    import userdata

    user = userdata.load()
    try:
        new_matches = alerts.notify_new(snap, user)
        if verbose:
            print(f"Fare alerts: {len(new_matches)} new match(es)")
    except Exception as e:
        new_matches = []
        if verbose:
            print("Fare alerts failed:", e)
    import ai
    if user.get("ai_summary") and ai.has_credentials():
        try:
            snap["ai_summary"] = ai.weekly_summary(snap, userdata.load())
            snap.pop("ai_summary_error", None)
        except Exception as e:
            snap["ai_summary_error"] = f"{type(e).__name__}: {e}"[:300]
        save_snapshot(snap)
        if verbose:
            print("AI summary:", "ok" if snap.get("ai_summary") else snap.get("ai_summary_error"))
    try:
        import notify
        sent = notify.send_weekly_digest(snap, userdata.load(), new_matches)
        if verbose and sent:
            print("Email digest sent")
    except Exception as e:
        if verbose:
            print("Email digest failed:", e)


if __name__ == "__main__":
    collect(verbose=sys.stdout is not None)
    sys.exit(0)
