"""Auction Finder (/auctions/) - police, government-surplus and local auctions in one place, ranked for what
might be interesting: cars, tech, antiques & collectibles, jewelry & coins, tools & equipment.

Sources (all public, no account needed):
  * PropertyRoom.com   - police departments' seized / unclaimed property (listing pages are plain HTML)
  * GSA Auctions       - US federal surplus & seized property (official API at api.gsa.gov; uses the api.data.gov key
                         from Influence Watch settings if one is saved, otherwise the shared DEMO_KEY)
  * Public Surplus     - city / county / school / police surplus (search result pages)
  * HiBid              - thousands of local auctioneers: estate, police, municipal, antiques (public GraphQL search)

"Interesting" is a transparent heuristic score (0-100) with the reasons shown: valuable keywords for the category,
few/no bids close to the end, price far under the auctioneer's estimate, classic or late-model vehicles, low miles,
recent tech - minus risk words (for parts, no title, does not run, untested...). It is a shortlist, not an appraisal.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36 AuctionFinder/1.0 (personal research)"}
S = requests.Session()
S.headers.update(UA)
POOL = ThreadPoolExecutor(8)    # page fetches
OUTER = ThreadPoolExecutor(8)   # one task per source per search (kept separate so sources never starve their own fetches)
_HOST = {h: threading.Semaphore(3) for h in ("pr", "gsa", "ps", "hb")}
_CACHE, _CLOCK = {}, threading.Lock()
TTL = 20 * 60


def _cached(key, ttl, fn):
    with _CLOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _CLOCK:
        _CACHE[key] = (time.time(), val)
        if len(_CACHE) > 400:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
                _CACHE.pop(k, None)
    return val


def _txt(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _money(s):
    try:
        return float(re.sub(r"[^\d.]", "", s or "") or "nan")
    except ValueError:
        return None


# ----------------------------------------------------------------------------------------------- profiles
PROFILES = {
    "cars": {
        "label": "Cars & Vehicles",
        "pr": ["vehicles_cars-trucks-vans", "vehicles_municipal-vehicles", "vehicles_motorcycles-and-mopeds", "vehicles_atvs-and-dirt-bikes"],
        "hb": ["car", "truck", "suv", "motorcycle", "police vehicle", "impound", "classic car"],
        "ps": ["vehicle", "truck", "sedan", "suv", "pickup", "police car"],
        "gsa": r"\b(sedan|truck|pickup|suv|van|vehicle|car|jeep|tahoe|explorer|f-?150|f-?250|silverado|ram \d|camry|malibu|"
               r"fusion|impala|charger|durango|motorcycle|harley|bus|ambulance|trailer|forklift|tractor)\b",
    },
    "tech": {
        "label": "Tech & Electronics",
        "pr": ["computers", "electronics_cell-phones", "electronics_cameras-and-photo", "electronics_gaming-systems-and-games",
               "electronics_iphone-and-ipod", "electronics_home-and-pro-audio-video"],
        "hb": ["laptop", "macbook", "iphone", "ipad", "graphics card", "camera lens", "drone"],
        "ps": ["laptop", "computer", "ipad", "server", "monitor", "camera"],
        "gsa": r"\b(laptop|desktop|computer|cpu|monitor|server|printer|ipad|iphone|tablet|camera|network|switch|router|"
               r"macbook|imac|mac pro|surface|thinkpad|dell|hp |lenovo|gpu|projector|television|tv|radio|drone|electronic)\b",
    },
    "antiques": {
        "label": "Antiques & Collectibles",
        "pr": ["collectibles", "fine-art", "coins_coins", "coins_exonumia-tokens-medals-other-collectibles", "musical-instruments"],
        "hb": ["antique", "vintage", "victorian", "oil painting", "sterling", "estate", "civil war", "first edition", "pottery"],
        "ps": ["antique", "vintage", "painting", "collectible"],
        "gsa": r"\b(antique|vintage|art|painting|sculpture|furniture|clock|piano|instrument|collect)\w*",
    },
    "jewelry": {
        "label": "Jewelry, Watches & Coins",
        "pr": ["jewelry_fine-jewelry", "jewelry_diamond-jewelry", "coins_precious-metal", "watches_mens-watches", "coins_coins", "coins_paper-money"],
        "hb": ["gold", "silver bullion", "rolex", "diamond ring", "morgan dollar", "14k"],
        "ps": ["jewelry", "gold", "watch", "coin"],
        "gsa": r"\b(jewel|gold|silver|watch|coin|diamond|bullion)\w*",
    },
    "tools": {
        "label": "Tools & Equipment",
        "pr": ["tools-and-equipment_power-tools", "tools-and-equipment_hand-tools", "vehicles_heavy-equipment-and-trailers", "tools-and-equipment_lawn-and-garden"],
        "hb": ["generator", "tractor", "welder", "snap-on", "milwaukee", "excavator"],
        "ps": ["equipment", "generator", "tractor", "mower", "tools"],
        "gsa": r"\b(generator|tractor|forklift|welder|compressor|mower|excavator|loader|tool|saw|drill|lathe|pump|equipment)\w*",
    },
    "police": {
        "label": "Police & Seized Only",
        "pr": ["steal-of-the-day", "one-hour-auctions", "all"],
        "hb": ["police", "seized", "impound", "forfeiture", "sheriff", "police auction"],
        "ps": ["police", "sheriff", "impound", "seized"],
        "gsa": r".",  # filtered by agency below
        "gsa_agency": r"customs|border|marshal|justice|homeland|drug enforcement|treasury|secret service|immigration|fbi|alcohol",
    },
}

VALUE = {
    "cars": r"\b(toyota|honda|lexus|tesla|porsche|corvette|mustang|camaro|bmw|mercedes|audi|subaru|tacoma|tundra|4runner|"
            r"wrangler|land cruiser|f-?150|silverado|sierra|ram 1500|harley|hybrid|4x4|awd|diesel|low miles|one owner|runs and drives|"
            r"runs & drives|clean title)\b",
    "tech": r"\b(macbook (pro|air)|imac|mac (mini|studio|pro)|m[1-4] (pro|max|chip)?|iphone 1[3-9]|ipad pro|apple watch|airpods|"
            r"rtx ?[2-5]0\d0|radeon|ryzen [79]|core i[79]|xeon|thinkpad|surface pro|playstation 5|ps5|xbox series|nintendo switch|"
            r"canon eos|nikon z|sony a7|gopro|dji|oled|4k|nvme|ssd|poweredge|synology)\b",
    "antiques": r"\b(sterling|\.925|14k|18k|10k|gold|tiffany|lalique|steuben|meissen|rookwood|roseville|moorcroft|wedgwood|"
                r"royal doulton|faberge|victorian|edwardian|georgian|art deco|art nouveau|civil war|wwii|ww2|first edition|signed|"
                r"original oil|oil on canvas|bronze|carved|hand.?painted|morgan|peace dollar|proof|mint state|ms ?6\d|pcgs|ngc|"
                r"cast iron|primitive|stoneware|crock|quilt|rolex|omega|patek|cartier|hermes|louis vuitton)\b",
    "jewelry": r"\b(rolex|omega|cartier|patek|breitling|tag heuer|tiffany|14k|18k|22k|24k|\.999|\.925|sterling|platinum|carat|"
               r"\d+(\.\d+)? ?ct|diamond|sapphire|ruby|emerald|gold bar|silver bar|bullion|morgan|peace dollar|eagle|krugerrand|"
               r"pcgs|ngc|proof)\b",
    "tools": r"\b(snap-?on|mac tools|matco|milwaukee|dewalt|makita|festool|hilti|lincoln|miller|hobart|kubota|john deere|"
             r"bobcat|caterpillar|cat |honda|generac|ingersoll|stihl|husqvarna|low hours)\b",
}
VALUE["police"] = "|".join(VALUE.values()).replace(")\\b|\\b(", "|")
RISK = (r"\b(for parts|parts only|parts/repair|salvage|no title|without title|bill of sale only|does not run|doesn'?t run|"
        r"non-?running|not running|won'?t start|blown|seized engine|engine seized|flood|fire damage|cracked|broken|damaged|"
        r"missing|locked|icloud|activation lock|bios lock|password protected|untested|not tested|as-?is|no key|no keys|"
        r"no charger|no hard drive|no hdd|no ssd|replica|reproduction|costume|faux|plated|gold tone|silver tone|overlay|"
        r"diamonique|cubic zirconia|\bcz\b|simulated|lab.?created|fashion jewelry|filled|vermeil|print on canvas|giclee)\b")
# accessories that merely mention a valuable product ("case for MacBook Pro", "band for Rolex")
ACCESSORY = (r"\b(case|cover|sleeve|skin|screen protector|tempered glass|protector|charger|charging|cable|adapter|dongle|"
             r"compatible with|compatible for|replacement|keyboard cover|stand|mount|holder|strap|band for|watch band|box only|"
             r"empty box|manual only|decal|sticker|poster|toy|keychain|battery|batteries|privacy|magcase|bag|backpack|tripod|"
             r"lens cap|remote control|calendar|for (macbook|iphone|ipad|airpods|rolex|tesla|corvette))\b")
ACCESSORY_OK = {"tools", "cars"}  # "charger", "cover", "mount" are normal words for these
OLD_TECH = r"\b(windows (xp|vista|7)|core 2|pentium|celeron|crt|vhs|dvd player|iphone [5-8]\b|ipad (2|3|4|mini 2)|ddr2)\b"


def score(it, profile):
    """-> (0-100, [reasons], [risks])"""
    title = it["title"].lower()
    text = f"{title} {it.get('desc') or ''}".lower()
    s, why, risk = 35, [], []
    now = time.time()
    left = (it["ends"] - now) / 3600 if it.get("ends") else None
    accessory = profile not in ACCESSORY_OK and re.search(ACCESSORY, title)
    if accessory:
        s -= 25
        risk.append("Accessory: " + accessory.group(0))
    else:
        vals = sorted({m.group(0).strip() for m in re.finditer(VALUE.get(profile, VALUE["police"]), text)})[:4]
        if vals:
            # a valuable word in the TITLE counts double vs one buried in the description
            s += min(24, sum(8 if v in title else 4 for v in vals))
            why.append("Valuable: " + ", ".join(vals))
    bids = it.get("bids")
    if bids == 0 and left is not None and 0 < left < 48:
        s += 15
        why.append("No bids yet, ends soon")
    elif bids is not None and bids <= 2 and left is not None and 0 < left < 24:
        s += 10
        why.append("Few bids, ends soon")
    elif bids is not None and bids >= 25:
        why.append(f"Bidding war ({bids} bids)")
    lo = it.get("est_lo")
    if lo and lo >= 50 and not accessory:
        if bids and it.get("price") and it["price"] < 0.5 * lo:
            s += 18
            why.append(f"Bid ${it['price']:,.0f} vs estimate ${lo:,.0f}+")
        elif not bids and (it.get("minbid") or 0) < 0.3 * lo:
            s += 6
            why.append(f"Opens far below estimate (${lo:,.0f}+)")
    if profile in ("cars", "police") or re.search(r"\b(sedan|truck|suv|pickup|coupe|motorcycle|vehicle)\b", text):
        y = [int(m) for m in re.findall(r"\b(19[3-9]\d|20[0-3]\d)\b", it["title"])]
        if y:
            yr = y[0]
            if yr < 1985:
                s += 12
                why.append(f"Classic ({yr})")
            elif yr >= dt.date.today().year - 8:
                s += 10
                why.append(f"Late model ({yr})")
        m = re.search(r"\b(\d{1,3}(?:,\d{3})|\d{4,6})\s*(?:miles|mi\b|mileage)", text) or re.search(r"(?:miles|mileage|odometer)[:\s]*(\d{1,3}(?:,\d{3})|\d{4,6})", text)
        if m:
            mi = int(m.group(1).replace(",", ""))
            if mi < 100000:
                s += 8
                why.append(f"{mi:,} miles")
            elif mi > 200000:
                s -= 6
                risk.append(f"{mi:,} miles")
    if profile in ("tech", "police") and re.search(OLD_TECH, text):
        s -= 10
        risk.append("Old tech")
    if it["src"] in ("PropertyRoom", "GSA") or re.search(r"\b(police|sheriff|seized|forfeit|impound)\w*", text):
        why.append("Police / seized / gov't surplus")
        s += 3 if profile == "police" else 0
    if it.get("ship"):
        why.append("Shipping offered")
        s += 2
    rk = sorted({m.group(0) for m in re.finditer(RISK, text)})[:3]
    if rk:
        s -= 12 + 6 * (len(rk) - 1)
        risk += rk
    return max(0, min(100, s)), why, risk


# ----------------------------------------------------------------------------------------------- sources
def _pr_page(path):
    with _HOST["pr"]:
        r = S.get("https://www.propertyroom.com" + path, timeout=30)
    r.raise_for_status()
    out = []
    for m in re.finditer(r'<a href="(/l/[^"]+)" class="listing-card"[\s\S]*?</a>', r.text):
        c = m.group(0)
        img = re.search(r'<img[^>]+src="([^"]+)"', c)
        end = re.search(r'data-end="([^"]+)"', c)
        bids = re.search(r'listing-bids">\s*(\d+)', c)
        title = re.search(r'listing-title">([\s\S]*?)</div>', c)
        try:
            ends = dt.datetime.fromisoformat(end.group(1)[:19]).replace(tzinfo=dt.timezone.utc).timestamp() if end else None
        except ValueError:
            ends = None
        out.append({"src": "PropertyRoom", "id": "pr" + m.group(1).rsplit("/", 1)[-1], "title": _txt(title.group(1) if title else ""),
                    "price": _money(re.search(r'listing-price">([^<]*)', c).group(1)) if 'listing-price">' in c else None,
                    "bids": int(bids.group(1)) if bids else 0, "ends": ends, "url": "https://www.propertyroom.com" + m.group(1),
                    "img": img.group(1) if img else "", "state": "", "city": "", "seller": "Police dept. (PropertyRoom)",
                    "cat": path.split("/")[2].split("?")[0].replace("_", " / ").replace("-", " ")})
    return out


def src_propertyroom(prof, q):
    paths = [f"/s/{quote(q)}"] if q else [f"/c/{c}?sort=closingsoon" for c in prof["pr"]]
    pages = [p + ("&" if "?" in p else "?") + f"page={n}" for p in paths for n in (1, 2)]
    out = []
    for res in POOL.map(lambda p: _cached("pr:" + p, TTL, lambda: _pr_page(p)), pages):
        out += res
    return out


def _gsa_all():
    key = "DEMO_KEY"
    try:
        import watch  # noqa: PLC0415 - reuse the api.data.gov key saved in Influence Watch
        key = watch.get_settings().get("fec_key") or key
    except Exception:  # noqa: BLE001
        pass
    with _HOST["gsa"]:
        r = S.get("https://api.gsa.gov/assets/gsaauctions/v2/auctions", params={"api_key": key, "format": "JSON"}, timeout=90)
    if r.status_code == 429:
        raise RuntimeError("GSA rate limit (shared DEMO_KEY) - add a free api.data.gov key in Influence Watch > ABOUT")
    r.raise_for_status()
    out = []
    for x in r.json().get("Results") or []:
        try:
            ends = dt.datetime.strptime(x.get("aucEndDt") or "", "%Y-%m-%d").replace(hour=23, tzinfo=dt.timezone(dt.timedelta(hours=-5))).timestamp()
        except ValueError:
            ends = None
        out.append({"src": "GSA", "id": f"gsa{x.get('saleNo')}-{x.get('lotNo')}", "title": (x.get("itemName") or "").strip(),
                    # drop GSA's standard boilerplate ("parts may be missing", removal rules) - it isn't about this lot
                    "desc": re.split(r"Inspection and Removal|Disclaimer|Condition & Markings|Condition &amp; Markings",
                                     _txt(x.get("lotInfo")))[0][:1500], "price": x.get("highBidAmount") or 0.0, "bids": x.get("biddersCount") or 0,
                    "ends": ends, "url": x.get("itemDescURL") or "https://www.gsaauctions.gov", "img": x.get("imageURL") or "",
                    "state": (x.get("propertyState") or "").strip(), "city": (x.get("propertyCity") or "").strip().title(),
                    "seller": x.get("agencyName") or "GSA", "cat": "federal surplus", "status": x.get("auctionStatus"),
                    "reserve": x.get("reserve")})
    return out


def src_gsa(prof, q):
    items = _cached("gsa:all", 30 * 60, _gsa_all)
    rx = re.compile(re.escape(q) if q else prof["gsa"], re.I)
    ag = re.compile(prof["gsa_agency"], re.I) if prof.get("gsa_agency") and not q else None
    return [dict(i) for i in items if rx.search(i["title"]) and (not ag or ag.search(i["seller"]))]


def _ps_search(term):
    with _HOST["ps"]:
        r = S.get("https://www.publicsurplus.com/sms/browse/search", params={"posting": "y", "keyWord": term}, timeout=30)
    r.raise_for_status()
    out = []
    for part in r.text.split('class="auction-item" id="')[1:]:
        aid = re.match(r"(\d+)", part)
        if not aid:
            continue
        aid = aid.group(1)
        title = re.search(r'title="#\d+ - ([^"]+)"', part)
        price = re.search(r'id="val_\d+searchGrid">\s*([^<]+)<', part)
        st = re.search(r'auction-item-state">\s*([A-Z]{2})\s*<', part)
        img = re.search(r'<img[^>]+src="(https://[^"]+)"', part)
        end = re.search(r"updateTimeLeftSpan\(timeLeftInfoMap,\s*\d+,\s*\"[^\"]*\",\s*\d+,\s*(\d+)", part)
        out.append({"src": "Public Surplus", "id": "ps" + aid, "title": html.unescape(title.group(1)).strip() if title else f"Auction {aid}",
                    "price": _money(price.group(1)) if price else None, "bids": None,
                    "ends": int(end.group(1)) / 1000 if end else None, "url": f"https://www.publicsurplus.com/sms/auction/view?auc={aid}",
                    "img": img.group(1) if img else "", "state": st.group(1) if st else "", "city": "",
                    "seller": "Public agency (Public Surplus)", "cat": term})
    return out


def src_publicsurplus(prof, q):
    out = []
    for res in POOL.map(lambda t: _cached("ps:" + t, TTL, lambda: _ps_search(t)), [q] if q else prof["ps"]):
        out += res
    return out


HB_Q = """query($q:String,$p:Int!,$n:Int!){lotSearch(input:{searchText:$q,status:OPEN,countAsView:false,hideGoogle:true},pageNumber:$p,pageLength:$n,sortDirection:DESC){
pagedResults{totalCount results{id lead description estimate shippingOffered featuredPicture{thumbnailLocation} category{categoryName}
lotState{highBid bidCount minBid buyNow timeLeftSeconds isClosed}
auction{id eventName eventCity eventState auctioneer{name}}}}}}"""


def _hb_search(term):
    with _HOST["hb"]:
        r = S.post("https://hibid.com/graphql", json={"query": HB_Q, "variables": {"q": term, "p": 1, "n": 100}}, timeout=40)
    r.raise_for_status()
    j = r.json()
    if j.get("errors"):
        raise RuntimeError("HiBid: " + str(j["errors"][0].get("message"))[:120])
    now = time.time()
    out = []
    for x in ((j.get("data") or {}).get("lotSearch") or {}).get("pagedResults", {}).get("results") or []:
        st = x.get("lotState") or {}
        if st.get("isClosed"):
            continue
        a = x.get("auction") or {}
        est = re.findall(r"[\d,]+(?:\.\d+)?", x.get("estimate") or "")
        lo = _money(est[0]) if est else None
        slug = re.sub(r"[^a-z0-9]+", "-", (x.get("lead") or "lot").lower()).strip("-")[:80] or "lot"
        cats = x.get("category") or []
        out.append({"src": "HiBid", "id": f"hb{x['id']}", "title": _txt(x.get("lead")), "desc": _txt(x.get("description"))[:1500],
                    "price": st.get("highBid") or 0.0, "minbid": st.get("minBid"), "bids": st.get("bidCount") or 0,
                    "ends": now + st["timeLeftSeconds"] if st.get("timeLeftSeconds") else None,
                    "url": f"https://hibid.com/lot/{x['id']}/{slug}", "img": (x.get("featuredPicture") or {}).get("thumbnailLocation") or "",
                    "state": a.get("eventState") or "", "city": (a.get("eventCity") or "").title(),
                    "seller": ((a.get("auctioneer") or {}).get("name") or "") + (f" - {a['eventName']}" if a.get("eventName") else ""),
                    "cat": cats[0]["categoryName"] if cats else term, "est_lo": lo if lo and lo > 0 else None,
                    "ship": bool(x.get("shippingOffered")), "buynow": st.get("buyNow") or None})
    return out


def src_hibid(prof, q):
    out = []
    for res in POOL.map(lambda t: _cached("hb:" + t, TTL, lambda: _hb_search(t)), [q] if q else prof["hb"]):
        out += res
    return out


SOURCES = {"PropertyRoom": src_propertyroom, "GSA": src_gsa, "Public Surplus": src_publicsurplus, "HiBid": src_hibid}


# ----------------------------------------------------------------------------------------------- API
def profiles(q=None):
    return {"profiles": [{"key": k, "label": v["label"]} for k, v in PROFILES.items()], "sources": list(SOURCES)}


def search(q):
    pk = q.get("profile") or "cars"
    prof = PROFILES.get(pk) or PROFILES["cars"]
    term = (q.get("q") or "").strip()[:80]
    want = [s for s in (q.get("sources") or ",".join(SOURCES)).split(",") if s in SOURCES]
    t0 = time.time()
    futs = {name: OUTER.submit(SOURCES[name], prof, term) for name in want}
    items, status = [], {}
    for name, f in futs.items():
        try:
            got = f.result(timeout=120)
            status[name] = {"ok": True, "n": len(got)}
            items += got
        except Exception as e:  # noqa: BLE001
            status[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    seen, out = set(), []
    now = time.time()
    for it in items:
        if it["id"] in seen or (it.get("ends") and it["ends"] < now - 60):
            continue
        seen.add(it["id"])
        it = dict(it)  # cached source rows stay untouched
        it["score"], it["why"], it["risk"] = score(it, pk if not term else "police")
        it["snip"] = (it.pop("desc", None) or "")[:260]
        out.append(it)
    out.sort(key=lambda i: -i["score"])
    states = {}
    for it in out:
        if it.get("state"):
            states[it["state"]] = states.get(it["state"], 0) + 1
    return {"profile": pk, "q": term, "items": out[:1500], "total": len(out), "status": status,
            "states": sorted(states.items(), key=lambda x: -x[1]), "secs": round(time.time() - t0, 1)}
