"""Last-Minute Tickets - concerts, games, shows and comedy near you in the next few days, with the cheapest resale
price (SeatGeek), face value and on-sale status (Ticketmaster), price drops tracked over time, and one-click
Gametime / SeatGeek / Ticketmaster links.

Double-click the Desktop icon (or run `python app.py`): it opens in its own window and stops about a minute after
the window closes. `--server` only serves (testing), `--port N` changes the port.
Keys are stored in data/settings.json on this PC and only sent to api.seatgeek.com / app.ticketmaster.com.
Gametime has no public API (and its terms forbid automated access), so it is linked, not searched.
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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
STATIC = Path(getattr(sys, "_MEIPASS", HERE)) / "static"


def _data_dir():
    if FROZEN:
        local = HERE / "LastMinuteTickets-data"
        if local.is_dir():
            return local
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LastMinuteTickets"
    return HERE / "data"


DATA = _data_dir()
DATA.mkdir(parents=True, exist_ok=True)
APP_ID = "LastMinuteTickets"
PORT = 8769
for _i, _a in enumerate(sys.argv):
    if _a == "--port" and _i + 1 < len(sys.argv):
        PORT = int(sys.argv[_i + 1])
if sys.stdout is None or sys.stderr is None:
    _log = open(DATA / "log.txt", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

S = requests.Session()
S.headers.update({"User-Agent": "LastMinuteTickets/1.0 (personal desktop app)"})
SG = "https://api.seatgeek.com/2/events"
TM = "https://app.ticketmaster.com/discovery/v2/events.json"

PLACES = {
    "dc": ("Washington, DC", 38.9007, -77.0369), "bal": ("Baltimore", 39.2904, -76.6122),
    "phl": ("Philadelphia", 39.9526, -75.1652), "nyc": ("New York", 40.7505, -73.9934),
    "bos": ("Boston", 42.3601, -71.0589), "chi": ("Chicago", 41.8781, -87.6298),
    "atl": ("Atlanta", 33.7490, -84.3880), "mia": ("Miami", 25.7617, -80.1918),
    "nash": ("Nashville", 36.1627, -86.7816), "lv": ("Las Vegas", 36.1147, -115.1728),
    "la": ("Los Angeles", 34.0430, -118.2673), "sf": ("San Francisco", 37.7749, -122.4194),
}

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


DEFAULTS = {"tm_key": "", "sg_id": "", "sg_secret": "", "place": "dc", "custom": None, "radius": 25, "days": 7, "auto": 30}


def settings():
    st = dict(DEFAULTS)
    st.update(_load("settings.json", {}))
    return st


def where(st):
    if st.get("place") == "zip" and st.get("custom"):
        c = st["custom"]
        return c["name"], c["lat"], c["lon"]
    return PLACES.get(st.get("place"), PLACES["dc"])


def _count(src, n=1):
    with _LOCK:
        u = _load("usage.json", {})
        d = date.today().isoformat()
        if u.get("day") != d:
            u = {"day": d, "sg": 0, "tm": 0}
        u[src] = u.get(src, 0) + n
        _save("usage.json", u)


def usage():
    u = _load("usage.json", {})
    if u.get("day") != date.today().isoformat():
        return {"sg": 0, "tm": 0}
    return {"sg": u.get("sg", 0), "tm": u.get("tm", 0)}


class UserError(Exception):
    pass


# ------------------------------------------------------------------ helpers
JUNK = re.compile(r"\b(parking|park pass|upgrade|add[- ]on|gift card|shuttle|tailgate pass|premium (seating|club) access|"
                  r"suite rental|locker|merch(andise)? (bundle|package)|fast lane|early entry)\b", re.I)
STOP = {"the", "at", "vs", "and", "a", "of", "in", "live", "tour", "tickets", "with", "presents", "an", "on", "v",
        "featuring", "feat", "ft", "for", "night", "show", "concert", "2026", "2027", "dc", "washington"}


def _toks(s):
    return {w for w in re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split() if w not in STOP and len(w) > 1}


def _utc(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _local(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


SPORT_WORDS = ("sports", "nba", "nfl", "nhl", "mlb", "ncaa", "mls", "nwsl", "wnba", "soccer", "wrestling", "boxing",
               "mma", "tennis", "golf", "hockey", "football", "basketball", "baseball", "racing", "horse", "lacrosse",
               "volleyball", "rodeo", "monster_truck", "motocross")


def _cat_sg(tax, typ):
    s = " ".join(tax + [typ or ""]).lower()
    if "comedy" in s:
        return "comedy"
    if any(w in s for w in SPORT_WORDS):
        return "sports"
    if "concert" in s or "music_festival" in s or "festival" in s:
        return "concerts"
    if any(w in s for w in ("theater", "broadway", "classical", "opera", "dance", "cirque", "ballet")):
        return "theater"
    if "family" in s:
        return "family"
    return "other"


def _cat_tm(seg, gen):
    seg, gen = (seg or "").lower(), (gen or "").lower()
    if seg == "sports":
        return "sports"
    if seg == "music":
        return "concerts"
    if "comedy" in gen:
        return "comedy"
    if "family" in gen or "children" in gen:
        return "family"
    if seg.startswith("arts"):
        return "theater"
    return "other"


# ------------------------------------------------------------------ SeatGeek
def _sg_fetch(st, lat, lon, start, end):
    p = {"client_id": st["sg_id"], "lat": lat, "lon": lon, "range": f"{int(st['radius'])}mi",
         "datetime_utc.gte": start.strftime("%Y-%m-%dT%H:%M:%S"), "datetime_utc.lte": end.strftime("%Y-%m-%dT%H:%M:%S"),
         "per_page": 100, "sort": "datetime_utc.asc"}
    if st.get("sg_secret"):
        p["client_secret"] = st["sg_secret"]
    out = []
    for page in range(1, 7):
        try:
            r = S.get(SG, params=dict(p, page=page), timeout=40)
        except Exception as e:  # noqa: BLE001
            raise UserError(f"Could not reach SeatGeek: {e}") from e
        _count("sg")
        if r.status_code in (401, 403):
            raise UserError("SeatGeek rejected the client ID. Check it in Settings.")
        if r.status_code == 429:
            raise UserError("SeatGeek says too many requests. Try again in a while.")
        if r.status_code >= 400:
            raise UserError(f"SeatGeek error {r.status_code}: {r.text[:200]}")
        j = r.json()
        evs = j.get("events") or []
        out += evs
        if not evs or page * 100 >= ((j.get("meta") or {}).get("total") or 0):
            break
    return out


def _sg_event(e):
    s = e.get("stats") or {}
    v = e.get("venue") or {}
    perf = e.get("performers") or []
    tax = [t.get("name") or "" for t in e.get("taxonomies") or []]
    img = next((p.get("image") for p in perf if p.get("image")), None)
    utc = e.get("datetime_utc")
    return {"title": e.get("title") or e.get("short_title"), "utc": (utc + "Z") if utc and not utc.endswith("Z") else utc,
            "local": e.get("datetime_local"), "tbd": bool(e.get("time_tbd") or e.get("datetime_tbd")),
            "venue": v.get("name"), "city": v.get("city"), "cat": _cat_sg(tax, e.get("type")),
            "performers": [p.get("name") for p in perf if p.get("name")][:4], "image": img,
            "sg": {"id": e.get("id"), "url": e.get("url"), "low": s.get("lowest_price"), "low_deal": s.get("lowest_price_good_deals"),
                   "avg": s.get("average_price"), "median": s.get("median_price"), "high": s.get("highest_price"),
                   "n": s.get("listing_count") if s.get("listing_count") is not None else s.get("visible_listing_count"),
                   "score": e.get("score")}}


# ------------------------------------------------------------------ Ticketmaster
def _tm_fetch(st, lat, lon, start, end):
    p = {"apikey": st["tm_key"], "latlong": f"{lat},{lon}", "radius": int(st["radius"]), "unit": "miles",
         "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "size": 200, "sort": "date,asc", "locale": "*"}
    out = []
    for page in range(0, 4):
        try:
            r = S.get(TM, params=dict(p, page=page), timeout=40)
        except Exception as e:  # noqa: BLE001
            raise UserError(f"Could not reach Ticketmaster: {e}") from e
        _count("tm")
        if r.status_code in (401, 403):
            raise UserError("Ticketmaster rejected the API key. Check it in Settings.")
        if r.status_code == 429:
            raise UserError("Ticketmaster says the daily limit was reached (5,000 a day).")
        if r.status_code >= 400:
            raise UserError(f"Ticketmaster error {r.status_code}: {r.text[:200]}")
        j = r.json()
        out += (j.get("_embedded") or {}).get("events") or []
        pg = j.get("page") or {}
        if page + 1 >= (pg.get("totalPages") or 0) or (page + 1) * 200 >= 1000:
            break
        time.sleep(0.25)  # 5 requests a second at most
    return out


def _tm_event(e):
    d = e.get("dates") or {}
    s = d.get("start") or {}
    emb = e.get("_embedded") or {}
    v = (emb.get("venues") or [{}])[0]
    pr = e.get("priceRanges") or []
    lo = min((x["min"] for x in pr if x.get("min")), default=None)
    hi = max((x["max"] for x in pr if x.get("max")), default=None)
    cl = (e.get("classifications") or [{}])[0]
    imgs = sorted((i for i in e.get("images") or [] if i.get("ratio") == "16_9" and i.get("url")),
                  key=lambda i: abs((i.get("width") or 0) - 640))
    return {"title": e.get("name"), "utc": s.get("dateTime"),
            "local": f"{s.get('localDate')}T{s.get('localTime') or '00:00:00'}" if s.get("localDate") else None,
            "tbd": bool(s.get("timeTBA") or s.get("noSpecificTime") or not s.get("localTime")),
            "venue": v.get("name"), "city": (v.get("city") or {}).get("name"),
            "cat": _cat_tm((cl.get("segment") or {}).get("name"), (cl.get("genre") or {}).get("name")),
            "performers": [a.get("name") for a in emb.get("attractions") or [] if a.get("name")][:4],
            "image": imgs[0]["url"] if imgs else None,
            "tm": {"id": e.get("id"), "url": e.get("url"), "lo": lo, "hi": hi, "status": (d.get("status") or {}).get("code")}}


# ------------------------------------------------------------------ merge
def _same(a, b):
    """0..1: how sure we are that a SeatGeek event and a Ticketmaster event are the same show."""
    if not a.get("local") or not b.get("local") or a["local"][:10] != b["local"][:10]:
        return 0
    la, lb = _local(a["local"]), _local(b["local"])
    if la and lb and not a["tbd"] and not b["tbd"] and abs((la - lb).total_seconds()) > 100 * 60:
        return 0
    va, vb = _toks(a["venue"]), _toks(b["venue"])
    vs = len(va & vb) / max(1, min(len(va), len(vb))) if va and vb else 0
    na = _toks(a["title"] + " " + " ".join(a["performers"]))
    nb = _toks(b["title"] + " " + " ".join(b["performers"]))
    ns = len(na & nb) / max(1, min(len(na), len(nb))) if na and nb else 0
    if vs >= 0.5 and ns >= 0.3:
        return (vs + ns) / 2
    if ns >= 0.8 and vs > 0:
        return ns * 0.8
    return 0


def merge(sg, tm):
    out = []
    used = set()
    for a in sg:
        best, bi = 0.0, None
        for i, b in enumerate(tm):
            if i in used:
                continue
            sc = _same(a, b)
            if sc > best:
                best, bi = sc, i
        x = dict(a)
        if bi is not None:
            used.add(bi)
            b = tm[bi]
            x["tm"] = b["tm"]
            x["image"] = b["image"] or a["image"]
            if x["cat"] == "other":
                x["cat"] = b["cat"]
        x["key"] = f"sg{a['sg']['id']}"
        out.append(x)
    for i, b in enumerate(tm):
        if i not in used:
            x = dict(b)
            x["key"] = f"tm{b['tm']['id']}"
            out.append(x)
    return out


# ------------------------------------------------------------------ price history + deal flags
def _record(events):
    h = _load("prices.json", {})
    now = time.time()
    for e in events:
        low = (e.get("sg") or {}).get("low")
        if low is None:
            continue
        rec = h.setdefault(e["key"], {"t": e["title"], "utc": e["utc"], "h": []})
        rec["utc"] = e["utc"]
        pts = rec["h"]
        n = (e.get("sg") or {}).get("n")
        if not pts or pts[-1][1] != low or now - pts[-1][0] > 3 * 3600:
            pts.append([round(now), low, n])
            del pts[:-200]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    h = {k: v for k, v in h.items() if (_utc(v.get("utc")) or cutoff) >= cutoff}
    _save("prices.json", h)
    return h


def annotate(events, hist, watch):
    now = time.time()
    nowdt = datetime.now(timezone.utc)
    for e in events:
        sg, tm = e.get("sg") or {}, e.get("tm") or {}
        low = sg.get("low")
        pts = (hist.get(e["key"]) or {}).get("h") or []
        e["hist"] = [[p[0], p[1]] for p in pts[-60:]]
        start = _utc(e.get("utc"))
        e["hours"] = round((start - nowdt).total_seconds() / 3600, 1) if start else None
        flags, score = [], 0.0
        if low is not None and pts:
            recent = [p[1] for p in pts if now - p[0] <= 48 * 3600] or [pts[-1][1]]
            peak = max(recent)
            first = pts[0][1]
            e["first_low"] = first
            e["first_at"] = pts[0][0]
            if peak > low:
                e["drop_pct"] = round((peak - low) / peak * 100)
                e["drop_from"] = peak
                if e["drop_pct"] >= 10:
                    flags.append(["g", f"Down {e['drop_pct']}% (was ${peak:,.0f})"])
                    score += e["drop_pct"]
            elif low > first * 1.1:
                flags.append(["r", f"Up {round((low - first) / first * 100)}% since first seen"])
        if low is not None and tm.get("lo") and low < tm["lo"]:
            e["under_face"] = round((tm["lo"] - low) / tm["lo"] * 100)
            flags.append(["g", f"{e['under_face']}% under face value"])
            score += e["under_face"]
        if low is not None and sg.get("median") and low <= 0.4 * sg["median"]:
            flags.append(["g", "Far below the typical seat"])
            score += 5
        if sg.get("n") is not None and 0 < sg["n"] <= 15:
            flags.append(["a", f"Only {sg['n']} listings left"])
        st = tm.get("status")
        if st in ("cancelled", "canceled", "postponed", "rescheduled"):
            flags.append(["r", st.capitalize()])
            score -= 100
        elif st == "offsale" and not sg:
            flags.append(["a", "Not on sale at Ticketmaster"])
        if e["hours"] is not None and 0 <= e["hours"] <= 30 and low is not None:
            score += 10
        e["flags"] = flags
        e["deal_score"] = round(score, 1)
        w = watch.get(e["key"])
        if w:
            e["watch"] = w
            tgt = w.get("target")
            e["alert"] = bool(low is not None and ((tgt and low <= tgt) or (not tgt and w.get("low_at_add") and low < w["low_at_add"])))
        q = e["performers"][0] if e.get("performers") else e["title"]
        e["gametime"] = "https://gametime.co/search?q=" + quote(q or "")
    return events


# ------------------------------------------------------------------ check / read
_CHECK_LOCK = threading.Lock()


def check(q=None):
    st = settings()
    if not st["sg_id"] and not st["tm_key"]:
        raise UserError("Add a SeatGeek client ID or a Ticketmaster key first (Settings).")
    if not _CHECK_LOCK.acquire(blocking=False):
        raise UserError("A check is already running.")
    try:
        name, lat, lon = where(st)
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        end = datetime.now(timezone.utc) + timedelta(days=int(st["days"]))
        errors, sg, tm = [], [], []
        with ThreadPoolExecutor(2) as ex:
            fs = {}
            if st["sg_id"]:
                fs["SeatGeek"] = ex.submit(_sg_fetch, st, lat, lon, start, end)
            if st["tm_key"]:
                fs["Ticketmaster"] = ex.submit(_tm_fetch, st, lat, lon, start, end)
            for src, f in fs.items():
                try:
                    raw = f.result()
                    if src == "SeatGeek":
                        sg = [x for x in map(_sg_event, raw) if x["title"] and not JUNK.search(x["title"])]
                    else:
                        tm = [x for x in map(_tm_event, raw) if x["title"] and not JUNK.search(x["title"])]
                except UserError as e:
                    errors.append(f"{src}: {e}")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{src}: {type(e).__name__}: {e}")
        if errors and not sg and not tm:
            raise UserError(" / ".join(errors))
        evs = merge(sg, tm)
        _record(evs)
        snap = {"at": time.time(), "where": name, "radius": st["radius"], "days": st["days"], "events": evs,
                "errors": errors, "sources": {"sg": len(sg), "tm": len(tm)}}
        _save("results.json", snap)
        return events({})
    finally:
        _CHECK_LOCK.release()


def events(q):
    snap = _load("results.json", None)
    if not snap:
        return {"events": [], "at": None, "usage": usage()}
    hist = _load("prices.json", {})
    watch = _load("watch.json", {})
    nowdt = datetime.now(timezone.utc) - timedelta(hours=3)
    evs = [e for e in snap["events"] if (_utc(e.get("utc")) or nowdt) >= nowdt]
    annotate(evs, hist, watch)
    snap["events"] = evs
    snap["alerts"] = [e["key"] for e in evs if e.get("alert")]
    snap["usage"] = usage()
    snap["checking"] = _CHECK_LOCK.locked()
    return snap


def set_watch(q):
    key = q.get("key") or ""
    w = _load("watch.json", {})
    if q.get("on") == "0":
        w.pop(key, None)
    else:
        snap = _load("results.json", {}) or {}
        ev = next((e for e in snap.get("events") or [] if e["key"] == key), None)
        if not ev and key not in w:
            raise UserError("Event not found - press Check now.")
        cur = w.get(key) or {"title": ev["title"], "utc": ev["utc"], "added": time.time(),
                             "low_at_add": (ev.get("sg") or {}).get("low")}
        t = (q.get("target") or "").strip().lstrip("$")
        cur["target"] = float(t) if t else None
        w[key] = cur
    _save("watch.json", w)
    return events({})


# ------------------------------------------------------------------ settings / state
def _hint(k):
    return (k[:3] + "..." + k[-3:]) if len(k) > 8 else ("set" if k else "")


def state(q):
    st = settings()
    name, lat, lon = where(st)
    return {"settings": {"has_tm": bool(st["tm_key"]), "has_sg": bool(st["sg_id"]), "tm_hint": _hint(st["tm_key"]),
                         "sg_hint": _hint(st["sg_id"]), "has_sg_secret": bool(st["sg_secret"]), "place": st["place"],
                         "where": name, "zip": (st.get("custom") or {}).get("zip", ""), "radius": st["radius"],
                         "days": st["days"], "auto": st["auto"]},
            "places": [[k, v[0]] for k, v in PLACES.items()], "usage": usage()}


def save_settings(q):
    st = _load("settings.json", {})
    for k in ("tm_key", "sg_id", "sg_secret"):
        if k in q:
            st[k] = q[k].strip()
    if q.get("zip"):
        z = q["zip"].strip()
        if not (len(z) == 5 and z.isdigit()):
            raise UserError("ZIP code should be 5 digits.")
        try:
            j = S.get(f"https://api.zippopotam.us/us/{z}", timeout=20).json()
            p = j["places"][0]
        except Exception as e:  # noqa: BLE001
            raise UserError(f"Could not find ZIP {z}.") from e
        st["custom"] = {"zip": z, "name": f"{p['place name']}, {p['state abbreviation']} {z}",
                        "lat": float(p["latitude"]), "lon": float(p["longitude"])}
        st["place"] = "zip"
    elif q.get("place") in PLACES:
        st["place"] = q["place"]
    if "radius" in q:
        st["radius"] = max(5, min(100, int(q["radius"] or 25)))
    if "days" in q:
        st["days"] = max(1, min(14, int(q["days"] or 7)))
    if "auto" in q:
        st["auto"] = int(q["auto"]) if q["auto"] in ("0", "15", "30", "60") else 30
    moved = any(k in q for k in ("zip", "place", "radius", "days"))
    _save("settings.json", st)
    if moved:
        try:
            (DATA / "results.json").unlink()
        except FileNotFoundError:
            pass
    return state({})


def test_keys(q):
    st = settings()
    now = datetime.now(timezone.utc)
    out = {}
    if st["sg_id"]:
        try:
            p = {"client_id": st["sg_id"], "per_page": 1}
            if st["sg_secret"]:
                p["client_secret"] = st["sg_secret"]
            r = S.get(SG, params=p, timeout=30)
            _count("sg")
            out["sg"] = "OK" if r.ok else f"rejected ({r.status_code})"
        except Exception as e:  # noqa: BLE001
            out["sg"] = f"could not connect: {e}"
    if st["tm_key"]:
        try:
            r = S.get(TM, params={"apikey": st["tm_key"], "size": 1, "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ")}, timeout=30)
            _count("tm")
            out["tm"] = "OK" if r.ok else f"rejected ({r.status_code})"
        except Exception as e:  # noqa: BLE001
            out["tm"] = f"could not connect: {e}"
    return out


# ------------------------------------------------------------------ background checks
LAST_PING = time.time()


def _auto_loop():
    while True:
        time.sleep(60)
        try:
            st = settings()
            if not st["auto"] or not (st["sg_id"] or st["tm_key"]) or time.time() - LAST_PING > 300:
                continue
            snap = _load("results.json", None) or {}
            if time.time() - (snap.get("at") or 0) >= int(st["auto"]) * 60:
                check()
        except Exception as e:  # noqa: BLE001
            print("auto check failed:", e)


# ------------------------------------------------------------------ server
ROUTES = {"/api/state": state, "/api/settings": save_settings, "/api/events": events, "/api/check": check,
          "/api/watch": set_watch, "/api/test_keys": test_keys, "/api/ping": lambda q: {"ok": True, "app": APP_ID}}
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
            except (UserError, ValueError) as e:
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


def main():
    url = f"http://127.0.0.1:{PORT}/"
    if already_running():
        if "--server" not in sys.argv:
            open_window(url)
        return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=_auto_loop, daemon=True).start()
    if "--server" in sys.argv:
        print("Serving on", url)
        while True:
            time.sleep(3600)
    proc = open_window(url)
    if proc is not None:
        proc.wait()
    grace = time.time() + 45
    while time.time() < grace or time.time() - LAST_PING < 60:
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
            ctypes.windll.user32.MessageBoxW(0, f"Last-Minute Tickets failed to start:\n{e}", "Last-Minute Tickets", 0x10)
        except Exception:  # noqa: BLE001
            pass
