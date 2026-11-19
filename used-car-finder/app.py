"""Used Car Deal Finder - search used cars by type, make/model, years and mileage (nationwide or near a ZIP), and see
which listings are priced below similar cars. Listings: MarketCheck API (free key). Safety data: NHTSA (free, no key).

Double-click the Desktop icon (or run `python app.py`): it opens in its own window and stops about a minute after
the window closes. `--server` only serves (testing), `--port N` changes the port.
Your MarketCheck key is stored in data/settings.json on this PC and only sent to api.marketcheck.com.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urlparse

import requests

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
STATIC = Path(getattr(sys, "_MEIPASS", HERE)) / "static"


def _data_dir():
    if FROZEN:
        local = HERE / "UsedCarFinder-data"
        if local.is_dir():
            return local
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "UsedCarFinder"
    return HERE / "data"


DATA = _data_dir()
DATA.mkdir(parents=True, exist_ok=True)
APP_ID = "UsedCarFinder"
PORT = 8768
for _i, _a in enumerate(sys.argv):
    if _a == "--port" and _i + 1 < len(sys.argv):
        PORT = int(sys.argv[_i + 1])
if sys.stdout is None or sys.stderr is None:
    _log = open(DATA / "log.txt", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

S = requests.Session()
S.headers.update({"User-Agent": "UsedCarFinder/1.0 (personal desktop app)"})
MC = "https://api.marketcheck.com/v2/search/car/active"
THIS_YEAR = date.today().year

MAKES = ["Acura", "Alfa Romeo", "Audi", "BMW", "Buick", "Cadillac", "Chevrolet", "Chrysler", "Dodge", "Fiat", "Ford",
         "Genesis", "GMC", "Honda", "Hyundai", "Infiniti", "Jaguar", "Jeep", "Kia", "Land Rover", "Lexus", "Lincoln",
         "Lucid", "Maserati", "Mazda", "Mercedes-Benz", "MINI", "Mitsubishi", "Nissan", "Polestar", "Porsche", "Ram",
         "Rivian", "Subaru", "Tesla", "Toyota", "Volkswagen", "Volvo"]
FUEL = {"gas": "Combustion", "hybrid": "HEV,MHEV", "phev": "PHEV", "ev": "BEV"}

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


DEFAULTS = {"api_key": "", "zip": "", "radius": 100, "pages": 2}


def settings():
    st = dict(DEFAULTS)
    st.update(_load("settings.json", {}))
    return st


def _count_call(n=1):
    with _LOCK:
        u = _load("usage.json", {})
        m = date.today().strftime("%Y-%m")
        if u.get("month") != m:
            u = {"month": m, "calls": 0}
        u["calls"] += n
        _save("usage.json", u)


def usage():
    u = _load("usage.json", {})
    return u.get("calls", 0) if u.get("month") == date.today().strftime("%Y-%m") else 0


class UserError(Exception):
    pass


# ------------------------------------------------------------------ MarketCheck search
def _params(f, st):
    p = {"api_key": st["api_key"], "car_type": "used", "photo_links": "true", "rows": 50,
         "stats": "price,miles", "include_non_vin_listings": "false"}
    if f.get("body"):
        p["body_type"] = f["body"]
    if f.get("make"):
        p["make"] = f["make"]
    if f.get("model"):
        p["model"] = f["model"]
    if f.get("trim"):
        p["trim"] = f["trim"]
    y0 = int(f.get("year_min") or 1990)
    y1 = int(f.get("year_max") or THIS_YEAR + 1)
    p["year_range"] = f"{min(y0, y1)}-{max(y0, y1)}"
    if f.get("miles_max"):
        p["miles_range"] = f"0-{int(f['miles_max'])}"
    if f.get("price_min") or f.get("price_max"):
        p["price_range"] = f"{int(f.get('price_min') or 0)}-{int(f.get('price_max') or 1000000)}"
    else:
        p["price_range"] = "1000-1000000"  # drops $0 / placeholder prices
    if f.get("drivetrain"):
        p["drivetrain"] = f["drivetrain"]
    if f.get("fuel") in FUEL:
        p["powertrain_type"] = FUEL[f["fuel"]]
    if f.get("trans"):
        p["transmission"] = f["trans"]
    if f.get("one_owner") in ("1", "true", True):
        p["carfax_1_owner"] = "true"
    if f.get("clean") in ("1", "true", True):
        p["carfax_clean_title"] = "true"
    if f.get("color"):
        p["base_ext_color"] = f["color"]
    zip_ = (f.get("zip") or "").strip()
    if zip_:
        p["zip"] = zip_
        p["radius"] = int(f.get("radius") or st.get("radius") or 100)
    return p


def _mc(params):
    try:
        r = S.get(MC, params=params, timeout=60)
    except Exception as e:  # noqa: BLE001
        raise UserError(f"Could not reach MarketCheck: {e}") from e
    _count_call()
    if r.status_code in (401, 403):
        raise UserError("MarketCheck rejected the API key. Check it in Settings.")
    if r.status_code == 429:
        raise UserError("MarketCheck says the request limit was reached. Try again later or check your plan.")
    if r.status_code == 422:
        return {"num_found": 0, "listings": []}
    if r.status_code >= 400:
        raise UserError(f"MarketCheck error {r.status_code}: {r.text[:200]}")
    return r.json()


def _listing(x):
    b = x.get("build") or {}
    d = x.get("dealer") or {}
    m = x.get("media") or {}
    photos = m.get("photo_links") or m.get("photo_links_cached") or []
    return {
        "id": x.get("id"), "vin": x.get("vin"), "heading": x.get("heading"), "price": x.get("price"),
        "miles": x.get("miles"), "msrp": x.get("msrp"), "url": x.get("vdp_url"), "photo": photos[0] if photos else None,
        "photos": photos[:8], "dom": x.get("dom"), "dom_active": x.get("dom_active"), "dist": x.get("dist"),
        "color": x.get("exterior_color"), "one_owner": bool(x.get("carfax_1_owner")), "clean": bool(x.get("carfax_clean_title")),
        "certified": bool(x.get("is_certified")), "first_seen": x.get("first_seen_at"),
        "price_change_pct": x.get("price_change_percent"), "ref_price": x.get("ref_price"), "source": x.get("source"),
        "dealer": {k: d.get(k) for k in ("name", "city", "state", "zip", "phone", "website", "dealer_type")},
        "build": {k: b.get(k) for k in ("year", "make", "model", "trim", "body_type", "drivetrain", "transmission",
                                        "engine", "powertrain_type", "fuel_type", "city_mpg", "highway_mpg")},
    }


# ------------------------------------------------------------------ deal scoring
def _solve(A, y):
    """Least squares via normal equations (tiny systems, pure Python)."""
    n = len(A[0])
    M = [[sum(r[i] * r[j] for r in A) for j in range(n)] + [sum(r[i] * v for r, v in zip(A, y))] for i in range(n)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-9:
            return None
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] for i in range(n)]


def score(listings):
    """Expected price for each car from similar cars (same make + model): log(price) ~ year + miles.
    deal_pct > 0 means cheaper than expected."""
    groups = {}
    for L in listings:
        b = L["build"]
        if L["price"] and L["price"] >= 1000 and b.get("make") and b.get("model") and b.get("year") and L["miles"] is not None:
            groups.setdefault((b["make"], b["model"]), []).append(L)
    for (mk, md), g in groups.items():
        med = median(x["price"] for x in g)
        fit = [x for x in g if 0.35 * med <= x["price"] <= 2.5 * med]  # ignore obvious data errors in the fit
        coef, cols, method = None, None, None
        if len(fit) >= 10:
            ym = sum(x["build"]["year"] for x in fit) / len(fit)
            mm = sum(x["miles"] for x in fit) / len(fit)
            for cols in (("year", "miles"), ("miles",), ("year",)):
                A = [[1.0] + ([x["build"]["year"] - ym] if "year" in cols else []) + ([(x["miles"] - mm) / 10000] if "miles" in cols else []) for x in fit]
                coef = _solve(A, [math.log(x["price"]) for x in fit])
                if coef:
                    method = f"compared with {len(fit)} similar {mk} {md}s, adjusted for " + " and ".join("model year" if c == "year" else "mileage" for c in cols)
                    break
        for x in g:
            if coef:
                row = [1.0] + ([x["build"]["year"] - ym] if "year" in cols else []) + ([(x["miles"] - mm) / 10000] if "miles" in cols else [])
                exp_ = math.exp(sum(a * b for a, b in zip(coef, row)))
            elif len(g) >= 5:
                same = [y["price"] for y in g if abs(y["build"]["year"] - x["build"]["year"]) <= 1]
                exp_ = median(same) if len(same) >= 4 else med
                method = f"compared with the median of {len(same) if len(same) >= 4 else len(g)} similar {mk} {md}s (too few for a mileage adjustment)"
            else:
                continue
            x["expected"] = round(exp_, -1)
            x["deal_usd"] = round(exp_ - x["price"])
            x["deal_pct"] = round((exp_ - x["price"]) / exp_ * 100, 1)
            x["basis"] = method
    for L in listings:
        p = L.get("deal_pct")
        L["rating"] = (None if p is None else "suspicious" if p >= 30 else "great" if p >= 12 else "good" if p >= 6
                       else "fair" if p >= -4 else "high")
    return listings


def search(q):
    st = settings()
    if not st["api_key"]:
        raise UserError("Add your free MarketCheck API key first (setup box or Settings).")
    f = json.loads(q["f"]) if q.get("f") else q
    p = _params(f, st)
    pages = max(1, min(4, int(st.get("pages") or 2)))
    first = _mc(dict(p, start=0, facets="make|0|25|1,model|0|25|1,body_type|0|15|1,year|0|30|1"))
    raw = list(first.get("listings") or [])
    found = first.get("num_found") or 0
    for i in range(1, pages):
        if i * 50 >= found:
            break
        raw += _mc(dict(p, start=i * 50, rows=50)).get("listings") or []
    seen, ls = set(), []
    for x in raw:
        k = x.get("vin") or x.get("id")
        if k and k not in seen:
            seen.add(k)
            ls.append(_listing(x))
    score(ls)
    stats = first.get("stats") or {}
    facets = {k: v for k, v in (first.get("facets") or {}).items()}
    return {"num_found": found, "listings": ls, "stats": {k: {kk: (stats.get(k) or {}).get(kk) for kk in ("min", "max", "mean", "median", "count")} for k in ("price", "miles")},
            "facets": facets, "filters": f, "where": f"within {p['radius']} miles of {p['zip']}" if p.get("zip") else "nationwide",
            "calls_this_month": usage(), "at": time.time()}


# ------------------------------------------------------------------ saved searches
def saved(q):
    return {"saved": _load("saved.json", [])}


def save_search(q):
    f = json.loads(q["f"])
    name = (q.get("name") or "").strip() or "My search"
    lst = _load("saved.json", [])
    lst.append({"id": str(int(time.time() * 1000)), "name": name[:80], "filters": f, "created": time.time(), "last_run": None, "seen": [], "last_new": 0})
    _save("saved.json", lst)
    return {"saved": lst}


def delete_search(q):
    lst = [s for s in _load("saved.json", []) if s["id"] != q.get("id")]
    _save("saved.json", lst)
    return {"saved": lst}


def run_saved(q):
    lst = _load("saved.json", [])
    s = next((x for x in lst if x["id"] == q.get("id")), None)
    if not s:
        raise UserError("Saved search not found.")
    res = search({"f": json.dumps(s["filters"])})
    old = set(s.get("seen") or [])
    for L in res["listings"]:
        L["new"] = bool(old) and (L["vin"] or L["id"]) not in old
    s["last_new"] = sum(1 for L in res["listings"] if L.get("new"))
    s["seen"] = list(old | {L["vin"] or L["id"] for L in res["listings"]})[-3000:]
    s["last_run"] = time.time()
    _save("saved.json", lst)
    res["saved_id"] = s["id"]
    res["saved_name"] = s["name"]
    res["new_count"] = s["last_new"]
    return res


# ------------------------------------------------------------------ NHTSA (free)
_CACHE = {}


def _cached(key, ttl, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _CACHE[key] = (time.time(), v)
    return v


def models(q):
    make = (q.get("make") or "").strip()
    if not make:
        return {"models": []}

    def load():
        r = S.get(f"https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMake/{requests.utils.quote(make)}", params={"format": "json"}, timeout=30)
        names = sorted({x["Model_Name"].strip() for x in r.json().get("Results", []) if x.get("Model_Name")})
        return {"models": names}
    return _cached(("models", make.lower()), 86400, load)


def research(q):
    make, model, year = q.get("make", ""), q.get("model", ""), str(q.get("year", ""))
    if not (make and model and year):
        raise UserError("make, model and year needed")

    def load():
        out = {"make": make, "model": model, "year": year}
        try:
            r = S.get("https://api.nhtsa.gov/recalls/recallsByVehicle", params={"make": make, "model": model, "modelYear": year}, timeout=30)
            rec = r.json().get("results") or []
            out["recalls"] = [{"component": x.get("Component"), "summary": (x.get("Summary") or "")[:400], "remedy": (x.get("Remedy") or "")[:300],
                               "date": x.get("ReportReceivedDate"), "campaign": x.get("NHTSACampaignNumber")} for x in rec]
        except Exception as e:  # noqa: BLE001
            out["recalls_error"] = str(e)
        try:
            r = S.get("https://api.nhtsa.gov/complaints/complaintsByVehicle", params={"make": make, "model": model, "modelYear": year}, timeout=30)
            cs = r.json().get("results") or []
            comp = {}
            for x in cs:
                # areas are joined by "," without a space; names themselves may contain ", " ("FUEL SYSTEM, GASOLINE")
                for c in re.split(r",(?! )", x.get("components") or ""):
                    c = c.strip()
                    if c and c != "UNKNOWN OR OTHER":
                        comp[c] = comp.get(c, 0) + 1
            out["complaints"] = {"total": len(cs), "crashes": sum(1 for x in cs if x.get("crash")), "fires": sum(1 for x in cs if x.get("fire")),
                                 "injuries": sum(int(x.get("numberOfInjuries") or 0) for x in cs),
                                 "top": sorted(comp.items(), key=lambda kv: -kv[1])[:6],
                                 "examples": [(x.get("components"), (x.get("summary") or "")[:300]) for x in cs[:4]]}
        except Exception as e:  # noqa: BLE001
            out["complaints_error"] = str(e)
        return out
    return _cached(("research", make.lower(), model.lower(), year), 86400, load)


# ------------------------------------------------------------------ settings / state
def state(q):
    st = settings()
    k = st["api_key"]
    return {"settings": {"has_key": bool(k), "key_hint": (k[:4] + "..." + k[-4:]) if len(k) > 10 else "", "zip": st["zip"],
                         "radius": st["radius"], "pages": st["pages"]},
            "makes": MAKES, "calls_this_month": usage(), "saved": _load("saved.json", []), "this_year": THIS_YEAR}


def save_settings(q):
    st = _load("settings.json", {})
    if "api_key" in q:
        st["api_key"] = q["api_key"].strip()
    if "zip" in q:
        z = q["zip"].strip()
        if z and not (len(z) == 5 and z.isdigit()):
            raise UserError("ZIP code should be 5 digits.")
        st["zip"] = z
    if "radius" in q:
        st["radius"] = max(10, min(500, int(q["radius"] or 100)))
    if "pages" in q:
        st["pages"] = max(1, min(4, int(q["pages"] or 2)))
    _save("settings.json", st)
    return state({})


def test_key(q):
    st = settings()
    if not st["api_key"]:
        raise UserError("No key saved.")
    j = _mc({"api_key": st["api_key"], "car_type": "used", "rows": 1})
    return {"ok": True, "num_found": j.get("num_found")}


# ------------------------------------------------------------------ server
LAST_PING = time.time()
ROUTES = {"/api/state": state, "/api/settings": save_settings, "/api/search": search, "/api/models": models,
          "/api/research": research, "/api/saved": saved, "/api/save": save_search, "/api/delete": delete_search,
          "/api/run_saved": run_saved, "/api/test_key": test_key, "/api/ping": lambda q: {"ok": True, "app": APP_ID}}
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
            ctypes.windll.user32.MessageBoxW(0, f"Used Car Deal Finder failed to start:\n{e}", "Used Car Deal Finder", 0x10)
        except Exception:  # noqa: BLE001
            pass
