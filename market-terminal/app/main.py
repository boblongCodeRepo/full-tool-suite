"""Market Terminal - launcher + local API server.

Double-click the built MarketTerminal.exe (or run `python app/main.py`).
Starts a local server on 127.0.0.1 and opens the terminal in a chromeless
Edge/Chrome app window. Closing the window shuts the server down.
"""
from __future__ import annotations

import json
import math
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_NAME = "MarketTerminal"
PORT = 8765
for _i, _a in enumerate(sys.argv):  # --port N (dev/testing alongside an installed copy)
    if _a == "--port" and _i + 1 < len(sys.argv):
        PORT = int(sys.argv[_i + 1])

def _data_dir():
    """Portable mode: if a 'MarketTerminal-data' folder sits next to the exe, keep everything there
    (watchlist, portfolios, alerts, caches travel with the USB stick). Otherwise use %LOCALAPPDATA%."""
    if getattr(sys, "frozen", False):
        local = Path(sys.executable).resolve().parent / "MarketTerminal-data"
        if local.is_dir():
            return local
    elif "WindowsApps" in (getattr(sys, "base_prefix", "") + sys.executable):
        # Running from source on Microsoft Store Python. Store Python redirects writes under %LOCALAPPDATA% to a
        # hidden per-package copy, so a dev run would see a half-real, half-copied view of the installed app's
        # databases - and has corrupted them when both ran at once. Dev runs get their own folder instead.
        return Path(__file__).resolve().parent.parent / "dev-data"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


DATA_DIR = _data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["MT_DATA_DIR"] = str(DATA_DIR)  # read by data.py / apps.py

# Windowed (no-console) builds have no stdout/stderr; send output to a log file.
if sys.stdout is None or sys.stderr is None:
    _log = open(DATA_DIR / "log.txt", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
STATIC = BASE / "static"

LAST_PING = time.time()

# Heavy imports (pandas / yfinance) load in the background so the window appears instantly.
_MODS = {}
_READY = threading.Event()


def _load_data():
    import data as d  # noqa: PLC0415
    import research as r  # noqa: PLC0415
    import apps as a  # noqa: PLC0415
    import watch as w  # noqa: PLC0415
    import trades as t  # noqa: PLC0415
    import auctions as au  # noqa: PLC0415
    import events as ev  # noqa: PLC0415
    _MODS.update(data=d, research=r, apps=a, watch=w, trades=t, auctions=au, events=ev)
    _READY.set()


def _load_device():
    """Device Watch is light (psutil + sqlite) and starts its collector as soon as the server is up."""
    try:
        import device as dv  # noqa: PLC0415
        _MODS["device"] = dv
        dv.start_collector()
        import hotspot as hs  # noqa: PLC0415
        _MODS["hotspot"] = hs
        hs.start_monitor()
        try:
            import bluetooth as bt  # noqa: PLC0415
            _MODS["bluetooth"] = bt
            bt.start()
        except Exception:  # noqa: BLE001  (no Bluetooth support must not take Device Watch down)
            import traceback
            traceback.print_exc()
        try:
            import guard as gd  # noqa: PLC0415
            _MODS["guard"] = gd
            gd.start()
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        try:
            import guests as gs  # noqa: PLC0415
            _MODS["guests"] = gs
            gs.start()
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        import honey  # noqa: PLC0415
        _MODS["honey"] = honey
        honey.start()
        import launchpad  # noqa: PLC0415
        _MODS["launchpad"] = launchpad
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
    finally:
        _DEV_READY.set()


_DEV_READY = threading.Event()


class _Lazy:
    def __init__(self, mod):
        self._mod = mod

    def __getattr__(self, name):
        ev = _DEV_READY if self._mod in ("device", "hotspot", "bluetooth", "honey", "launchpad", "guard", "guests") else _READY
        if not ev.wait(120) or self._mod not in _MODS:
            raise RuntimeError(f"{self._mod} module failed to load")
        return getattr(_MODS[self._mod], name)


data = _Lazy("data")
research = _Lazy("research")
apps = _Lazy("apps")
watch = _Lazy("watch")
trades = _Lazy("trades")
device = _Lazy("device")
hotspot = _Lazy("hotspot")
bluetooth = _Lazy("bluetooth")
guard = _Lazy("guard")
guests = _Lazy("guests")
honey = _Lazy("honey")
launchpad = _Lazy("launchpad")
auctions = _Lazy("auctions")
events = _Lazy("events")

# The apps served by this one local server: name -> url path
APPS = {"terminal": "/", "lab": "/lab/", "signal": "/signal/", "watch": "/watch/", "money": "/money/", "device": "/device/", "auctions": "/auctions/",
        "events": "/events/", "brief": "/brief/", "launch": "/launch/"}


_SV = [0.0, ""]


def _static_version():
    """Fingerprint of the page files (newest mtime), so open windows can reload themselves after an update."""
    if time.time() - _SV[0] > 10:
        try:
            _SV[1] = str(int(max(f.stat().st_mtime for f in STATIC.rglob("*") if f.suffix in (".js", ".css", ".html"))))
        except ValueError:
            _SV[1] = ""
        _SV[0] = time.time()
    return _SV[1]


def _syms(s):
    return [x.strip().upper() for x in (s or "").split(",") if x.strip()]


def _clean(o):
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


ROUTES = {
    "/api/overview": lambda q: data.page_overview(),
    "/api/equities": lambda q: data.page_equities(),
    "/api/govt": lambda q: data.page_govt(),
    "/api/corp": lambda q: data.page_corp(),
    "/api/private": lambda q: data.page_private(),
    "/api/econ": lambda q: data.page_econ(),
    "/api/news": lambda q: data.news(q.get("topic", "top")),
    "/api/security": lambda q: data.security(q["symbol"], q.get("range", "1Y")),
    "/api/quotes": lambda q: data.quotes([s.strip().upper() for s in q["symbols"].split(",") if s.strip()][:60]),
    "/api/fred": lambda q: data.fred_chart(q["id"], q.get("tf", "lvl"), float(q.get("years", 10))),
    "/api/search": lambda q: data.search(q["q"]),
    # security functions
    "/api/gp": lambda q: research.gp(q["symbol"], q.get("range", "1Y")),
    "/api/fa": lambda q: research.fa(q["symbol"], q.get("period", "annual")),
    "/api/ern": lambda q: research.ern(q["symbol"]),
    "/api/anr": lambda q: research.anr(q["symbol"]),
    "/api/omon": lambda q: research.options(q["symbol"], q.get("exp")),
    "/api/hds": lambda q: research.hds(q["symbol"]),
    "/api/dvd": lambda q: research.dvd(q["symbol"]),
    "/api/rv": lambda q: research.rv(q["symbol"], q.get("extra", "")),
    # global tools
    "/api/comp": lambda q: research.comp(_syms(q["symbols"]), q.get("range", "1Y")),
    "/api/cal": lambda q: research.calendar(int(q.get("days", 14))),
    "/api/next_earnings": lambda q: research.next_earnings(_syms(q["symbols"])),
    "/api/eqs": lambda q: research.eqs(q),
    "/api/ext": lambda q: research.ext_page(q.get("watch", "")),
    "/api/ext_quotes": lambda q: research.ext_quotes(_syms(q["symbols"])),
    "/api/ext_chart": lambda q: research.ext_chart(q["symbol"]),
    "/api/ping": lambda q: {"ok": True, "app": APP_NAME, "v": _static_version()},
    # Portfolio Lab
    "/api/lab/backtest": lambda q: apps.lab_backtest(q),
    # Signal Monitor
    "/api/sig/overview": lambda q: apps.signal_overview(q["symbol"], q.get("title") or None),
    "/api/sig/news": lambda q: apps.news_sentiment(q.get("name", ""), q.get("symbol", "")),
    "/api/sig/hn": lambda q: apps.hn_mentions(q["q"]),
    "/api/sig/gdelt": lambda q: apps.gdelt(q["q"]),
    "/api/sig/scan1": lambda q: apps.scan_one(q["symbol"]),
    "/api/sig/scanpx": lambda q: apps.scan_prices(_syms(q["symbols"])),
    "/api/sig/universes": lambda q: apps.universes(),
    "/api/sig/settings": lambda q: apps.set_settings(q["contact"]) if "contact" in q else {"contact": apps.get_settings().get("contact", ""), "cooldown": apps.wiki_cooldown()},
    # Influence Watch
    "/api/iw/wire": lambda q: watch.wire(q),
    "/api/iw/stories": lambda q: watch.stories(q),
    "/api/iw/framing": lambda q: watch.framing(q),
    "/api/iw/echoes": lambda q: watch.echoes(q),
    "/api/iw/profile": lambda q: watch.profile(q),
    "/api/iw/person": lambda q: watch.person(q),
    "/api/iw/fec": lambda q: watch.fec(q),
    "/api/iw/lda_country": lambda q: watch.lda_country(q),
    "/api/iw/lda_client": lambda q: watch.lda_client(q),
    "/api/iw/fara_summary": lambda q: watch.fara_summary(q),
    "/api/iw/fara_country": lambda q: watch.fara_country(q),
    "/api/iw/fara_search": lambda q: watch.fara_search(q),
    "/api/iw/gov": lambda q: watch.gov(q),
    "/api/iw/status": lambda q: watch.status(q),
    "/api/iw/settings": lambda q: watch.settings(q),
    "/api/iw/refresh": lambda q: watch.refresh(),
    "/api/iw/congress": lambda q: trades.congress(q),
    "/api/iw/insiders": lambda q: trades.insiders(q),
    "/api/iw/insider_company": lambda q: trades.insider_company(q),
    "/api/iw/contracts": lambda q: trades.contracts(q),
    "/api/iw/contracts_search": lambda q: trades.contracts_search(q),
    "/api/iw/money_highlights": lambda q: trades.highlights(q),
    "/api/iw/moves": lambda q: trades.moves(q),
    # Device Watch
    "/api/dev/status": lambda q: device.status(q),
    "/api/dev/live": lambda q: device.live(q),
    "/api/dev/history": lambda q: device.history(q),
    "/api/dev/traffic": lambda q: device.traffic(q),
    "/api/dev/dns": lambda q: device.dns(q),
    "/api/dev/events": lambda q: device.events(q),
    "/api/dev/listening": lambda q: device.listening(q),
    "/api/dev/audit": lambda q: device.audit(q),
    "/api/dev/block": lambda q: device.block(q),
    "/api/dev/netblock": lambda q: device.netblock(q),
    "/api/dev/netblocks": lambda q: device.netblocks(q),
    "/api/dev/always_on": lambda q: device.set_always_on(q),
    "/api/dev/clear": lambda q: device.clear(q),
    # Device Watch > VACUUM: gadgets connected through this PC's Wi-Fi hotspot
    "/api/dev/hs/status": lambda q: hotspot.status(q),
    "/api/dev/hs/volume": lambda q: hotspot.volume(q),
    "/api/dev/hs/flows": lambda q: hotspot.flows(q),
    "/api/dev/hs/insights": lambda q: hotspot.insights(q),
    "/api/dev/hs/start": lambda q: hotspot.start(q),
    "/api/dev/hs/stop": lambda q: hotspot.stop(q),
    "/api/dev/hs/settings": lambda q: hotspot.settings(q),
    "/api/dev/hs/sniff": lambda q: hotspot.sniff(q),
    "/api/dev/hs/forget": lambda q: hotspot.forget(q),
    "/api/dev/hs/block": lambda q: hotspot.block(q),
    # Device Watch > BLUETOOTH: hidden-tracker detector
    "/api/dev/bt/status": lambda q: bluetooth.status(q),
    "/api/dev/bt/radio": lambda q: bluetooth.radio(q),
    "/api/dev/bt/mine": lambda q: bluetooth.mine(q),
    "/api/dev/bt/find": lambda q: bluetooth.find(q),
    "/api/dev/bt/presence": lambda q: bluetooth.presence(q),
    "/api/dev/bt/quiet": lambda q: bluetooth.quiet(q),
    "/api/dev/bt/away": lambda q: bluetooth.set_away(q),
    "/api/dev/bt/visitors": lambda q: bluetooth.visitors(q),
    "/api/dev/use": lambda q: guard.use_status(q),
    "/api/dev/push_test": lambda q: hotspot.push_test(q),
    # Device Watch > WIFI: networks in range; AUDIT: firmware fingerprint + opening a Windows settings page
    "/api/dev/wifi": lambda q: guard.wifi(q),
    "/api/dev/wifi/ok": lambda q: guard.wifi_ok(q),
    "/api/dev/wifi/log": lambda q: guard.wifi_log(q),
    "/api/dev/wifi/ap": lambda q: guard.wifi_ap(q),
    "/api/dev/wifi/cameras": lambda q: guard.camera_search(q),
    "/api/dev/wifi/find": lambda q: guard.wifi_find(q),
    "/api/dev/boot": lambda q: guard.boot(q),
    "/api/dev/setting": lambda q: guard.open_setting(q),
    # Device Watch > WIFI: attacks on the network (router impersonation, Wi-Fi drops, fake access points); CELL: Rayhunter
    "/api/dev/net": lambda q: guard.net_status(q),
    "/api/dev/net/fakeap": lambda q: guard.karma_test(q),
    "/api/dev/cell": lambda q: guard.rayhunter(q),
    "/api/dev/cell/set": lambda q: guard.rayhunter_set(q),
    "/api/dev/cell/prepare": lambda q: guard.rh_prepare(q),
    "/api/dev/cell/auto": lambda q: guard.rh_auto(q),
    "/api/dev/cell/install": lambda q: guard.rh_install(q),
    # Device Watch > INTRUDER: tripwires, webcam photos, what someone did while you were away
    "/api/dev/intruder": lambda q: guard.intruder(q),
    "/api/dev/intruder/snap": lambda q: guard.snap_test(q),
    # Device Watch > GUESTS: the guest Wi-Fi router's log (terms acceptances, lookups, connections)
    "/api/dev/guests/status": lambda q: guests.status(q),
    "/api/dev/guests/device": lambda q: guests.device(q),
    "/api/dev/guests/lookup": lambda q: guests.lookup(q),
    "/api/dev/guests/mine": lambda q: guests.mine(q),
    "/api/dev/guests/settings": lambda q: guests.settings(q),
    "/api/dev/guests/verify": lambda q: guests.verify(q),
    "/api/dev/guests/export": lambda q: guests.export(q),
    # Device Watch > TRAPS: decoy files
    "/api/dev/honey/status": lambda q: honey.status(q),
    "/api/dev/honey/setup": lambda q: honey.setup(q),
    "/api/dev/honey/remove": lambda q: honey.remove(q),
    "/api/dev/honey/review": lambda q: honey.review(q),
    "/api/dev/honey/clear": lambda q: honey.clear(q),
    "/api/dev/honey/canary": lambda q: honey.canary(q),
    # Launchpad: home screen for every app + entertainment
    "/api/launch/summary": lambda q: launchpad.summary(q),
    "/api/launch/start": lambda q: launchpad.start(q),
    "/api/launch/today": lambda q: launchpad.today(q),
    # Auction Finder
    "/api/auc/profiles": lambda q: auctions.profiles(q),
    "/api/auc/search": lambda q: auctions.search(q),
    # DC Events
    "/api/evt/list": lambda q: events.listing(q),
    "/api/evt/refresh": lambda q: events.refresh(q),
}


# ---- request guard ------------------------------------------------------------------------------------------
# The server only listens on 127.0.0.1, but a web page open in any browser on this PC can still make that browser
# send requests here. Three checks keep other sites out:
#  * Host must be our own address        -> defeats DNS rebinding (evil.com re-pointed at 127.0.0.1 to read the API)
#  * Origin / Sec-Fetch-Site must be ours -> other sites cannot call the API at all
#  * endpoints that CHANGE something must come from our own pages (same-origin fetch), never from a link, an
#    <img> tag or a typed address -> no other page can wipe the network log, switch off logging or raise a
#    firewall prompt
_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
_ORIGINS = {"http://" + h for h in _HOSTS}
_ACTIONS = {"/api/dev/block", "/api/dev/netblock","/api/dev/always_on", "/api/dev/clear",
            "/api/dev/hs/start", "/api/dev/hs/stop", "/api/dev/hs/settings", "/api/dev/hs/sniff", "/api/dev/hs/forget", "/api/dev/hs/block",
            "/api/dev/bt/radio", "/api/dev/bt/mine", "/api/dev/honey/setup", "/api/dev/honey/remove", "/api/launch/start",
            "/api/dev/honey/review", "/api/dev/honey/clear", "/api/dev/honey/canary",
            "/api/dev/bt/quiet", "/api/dev/wifi/ok", "/api/dev/setting", "/api/dev/bt/away", "/api/dev/push_test",
            "/api/dev/net/fakeap", "/api/dev/cell/set", "/api/dev/intruder/snap", "/api/dev/cell/prepare", "/api/dev/cell/auto", "/api/dev/cell/install", "/api/dev/snap/upload",
            "/api/iw/settings", "/api/iw/refresh", "/api/sig/settings", "/api/evt/refresh"}
# Pages may only run script files shipped with the app: an injected <script> or onclick= (say, from a hostile feed
# headline or network-owner name that slipped past escaping) will not execute, and scripts can only talk to us.
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https: http:; "
        "connect-src 'self'; media-src 'self' https:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _refusal(headers, path):
    """Why this request must be refused, or None if it is fine."""
    if (headers.get("Host") or "").lower() not in _HOSTS:
        return "unknown host"
    origin = headers.get("Origin")
    if origin and origin not in _ORIGINS:
        return "cross-site request"
    if not path.startswith("/api/"):
        return None  # pages and their files: opening them from a link is fine (framing is blocked by header)
    site = headers.get("Sec-Fetch-Site")  # set by the browser itself; page scripts cannot forge it
    if site and site not in ("same-origin", "none"):
        return "cross-site request"
    if path in _ACTIONS:
        if site:
            ok = site == "same-origin"
        else:  # browser too old to send Sec-Fetch-Site: fall back to the Referer
            ok = any((headers.get("Referer") or "").startswith(o + "/") for o in _ORIGINS)
        if not ok:
            return "this action can only be started from inside the app"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "MarketTerminal"
    sys_version = ""

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        """Only one thing is ever sent here: the webcam photo from the hidden INTRUDER page (with a one-time code)."""
        u = urlparse(self.path)
        why = _refusal(self.headers, u.path)
        if why or u.path != "/api/dev/snap/upload":
            return self._send(403, json.dumps({"error": "refused: " + (why or "not accepted")}).encode(), "application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 5_000_000:
            return self._send(413, b'{"error":"too big"}', "application/json")
        body = self.rfile.read(n) if n else b""
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            res, code = guard.snap_upload(q, body), 200
        except Exception as e:  # noqa: BLE001
            res, code = {"error": f"{type(e).__name__}: {e}"}, 400
        return self._send(code, json.dumps(res).encode(), "application/json")

    def do_GET(self):  # noqa: N802
        global LAST_PING
        u = urlparse(self.path)
        why = _refusal(self.headers, u.path)
        if why:
            return self._send(403, json.dumps({"error": "refused: " + why}).encode(), "application/json")
        if u.path.startswith("/api/"):
            LAST_PING = time.time()
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            fn = ROUTES.get(u.path)
            if not fn:
                return self._send(404, b'{"error":"not found"}', "application/json")
            try:
                res = fn(q)
                code = 200
            except Exception as e:  # noqa: BLE001
                res, code = {"error": f"{type(e).__name__}: {e}"}, 500
            body = json.dumps(_clean(res), allow_nan=False).encode()
            return self._send(code, body, "application/json")
        if u.path.rstrip("/") in ("/lab", "/signal", "/watch", "/money", "/device", "/auctions", "/events", "/brief", "/launch") and not u.path.endswith("/"):
            self.send_response(301)
            self.send_header("Location", u.path + "/")
            self.end_headers()
            return None
        rel = u.path.lstrip("/")
        if not rel or rel.endswith("/"):
            rel += "index.html"
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        return self._send(200, f.read_bytes(), ctype)


def already_running():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=2) as r:
            return APP_NAME in r.read().decode()
    except Exception:  # noqa: BLE001
        return False


def find_browser():
    """A Chromium browser that supports --app windows (Edge, Chrome, Brave, Chromium). Safari/Firefox can't do
    chromeless app windows, so without one of these the app opens in the default browser instead."""
    cands = []
    if sys.platform == "win32":
        for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                     os.environ.get("LOCALAPPDATA")):
            if root:
                cands += [Path(root) / "Microsoft/Edge/Application/msedge.exe",
                          Path(root) / "Google/Chrome/Application/chrome.exe"]
    elif sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            cands += [root / "Google Chrome.app/Contents/MacOS/Google Chrome",
                      root / "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                      root / "Brave Browser.app/Contents/MacOS/Brave Browser",
                      root / "Chromium.app/Contents/MacOS/Chromium"]
    else:
        import shutil  # noqa: PLC0415
        cands += [Path(p) for p in (shutil.which(n) for n in ("google-chrome", "chromium", "chromium-browser",
                                                               "microsoft-edge", "brave-browser")) if p]
    for c in cands:
        if c.is_file():
            return str(c)
    return None


def open_window(url):
    """Returns a Popen to wait on, or None if we fell back to the default browser."""
    exe = find_browser()
    if not exe:
        webbrowser.open(url)
        return None
    prof = DATA_DIR / "window-profile"
    args = [exe, f"--app={url}", f"--user-data-dir={prof}", "--window-size=1600,950",
            "--no-first-run", "--no-default-browser-check",
            # These windows only show pages served by this PC, so turn off the browser's own background calls home
            # (component/safe-browsing updates, usage + crash reports, sync, pings, shopping/"discover" features).
            "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--disable-domain-reliability", "--disable-client-side-phishing-detection", "--disable-breakpad",
            "--metrics-recording-only", "--no-pings", "--no-service-autorun", "--safebrowsing-disable-auto-update",
            "--disable-features=Translate,msEdgeShopping,msShowDiscover,msEdgeCollections,msImplicitSignin,"
            "AutofillServerCommunication,OptimizationHints,MediaRouter,DialMediaRouteProvider,SpareRendererForSitePerProcess"]
    return subprocess.Popen(args)


def which_app():
    """--app terminal|lab|signal, or inferred from the exe name (e.g. a copy renamed PortfolioLab.exe)."""
    for i, a in enumerate(sys.argv):
        if a == "--app" and i + 1 < len(sys.argv):
            return sys.argv[i + 1].lower()
        if a.startswith("--app="):
            return a.split("=", 1)[1].lower()
    exe = Path(sys.argv[0]).stem.lower()
    if "launch" in exe:
        return "launch"
    if "brief" in exe or "morning" in exe:
        return "brief"
    if "lab" in exe or "portfolio" in exe:
        return "lab"
    if "signal" in exe:
        return "signal"
    if "device" in exe:
        return "device"
    if "auction" in exe:
        return "auctions"
    if "event" in exe:
        return "events"
    if "influence" in exe or "watch" in exe:
        return "watch"
    return "terminal"


def main():
    if "--hs-sniff" in sys.argv:  # elevated helper for Device Watch > VACUUM: logs hotspot packet headers, no window
        import hotspot as hs  # noqa: PLC0415
        hs.sniff_main(sys.argv)
        return
    if "--hs-block" in sys.argv or "--hs-unblock" in sys.argv:  # elevated one-off for Device Watch > VACUUM: block one address for hotspot devices
        import hotspot as hs  # noqa: PLC0415
        hs.block_main(sys.argv)
        return
    if "--honey-setup" in sys.argv or "--honey-remove" in sys.argv:  # elevated one-offs for Device Watch > TRAPS: set the traps up / undo them
        import honey  # noqa: PLC0415
        honey.setup_main(sys.argv)
        return
    url =f"http://127.0.0.1:{PORT}" + APPS.get(which_app(), "/")
    collect = "--collect" in sys.argv  # Device Watch always-on: headless server + network logger, never exits
    if already_running():
        if "--server" in sys.argv or collect:
            print(f"Port {PORT} is already in use by another Market Terminal - use --port to pick another")
            return
        open_window(url)
        return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=_load_device, daemon=True).start()
    threading.Thread(target=_load_data, daemon=True).start()
    if "--server" in sys.argv or collect:  # dev mode / always-on collector: just serve
        print(f"Serving on {url}")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    proc = open_window(url)
    if proc is not None:
        proc.wait()
    # Every app window pings /api/ping every 20s. Keep serving while any window is open
    # (Terminal, Portfolio Lab and Signal Monitor share this server); stop ~1 min after the last closes.
    grace = time.time() + 45
    while time.time() < grace or time.time() - LAST_PING < 60:
        time.sleep(5)
    srv.shutdown()


if __name__ == "__main__":
    try:
        socket.setdefaulttimeout(30)
        main()
    except Exception as e:  # noqa: BLE001
        import traceback
        (DATA_DIR / "crash.txt").write_text(traceback.format_exc(), encoding="utf-8")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"Market Terminal failed to start:\n{e}", APP_NAME, 0x10)
        except Exception:  # noqa: BLE001
            pass
