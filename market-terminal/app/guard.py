"""Device Watch > WIFI (networks in range) and the firmware / boot fingerprint shown on AUDIT.

WIFI: every minute, the list of Wi-Fi access points Windows can hear (name, hardware address, security, signal).
Nothing is sent and nothing is joined. Every access point heard in the last 90 days is kept (the LOG), with an hourly signal history, a
rough distance worked out from its signal strength and frequency, the maker of its hardware (from the first half of its
hardware address) and whatever it says about itself in its broadcasts (many routers, printers and cameras announce
their maker, model and device name for WPS set-up). It flags
  * an access point using the name of YOUR network that was not there before (an "evil twin" set up so devices join it),
  * names that small cameras broadcast while they are in set-up mode (the kind sold as hidden cameras), and
  * a new access point that appears close by (a phone hotspot or travel router that was not here before).
Windows only gives this list to programs when Location services are switched on, so the tab asks for that.

FINGERPRINT: the BIOS version plus the boot measurements kept by the security chip (TPM). The chip records a hash of the
firmware code, plug-in firmware, boot settings, boot loader and Secure Boot keys at every start, and software inside
Windows cannot rewrite them. A value never seen before means something below Windows changed.
"""
from __future__ import annotations

import collections
import ctypes
import json
import math
import re
import sqlite3
import statistics
import struct
import subprocess
import sys
import threading
import time
import urllib.request

import hotspot as _hs  # data folder, event log and Windows notifications are shared with the hotspot monitor

IS_WIN = sys.platform == "win32"
DB_FILE = _hs.HS_DIR / "guard.db"
SCAN_SECS = 60
SCAN_WAIT = 4             # seconds between asking the adapter for a fresh scan and reading the results
KEEP_DAYS = 90            # access points not heard for this long are dropped from the log, with their hourly history
# Distance from signal strength (log-distance model). Only a rough guide: walls, floors, antennas and how strongly
# each access point transmits all move it, so the API also gives a likely range around the estimate.
TX_DBM = 17               # typical home router transmit power, used when the access point does not announce its own
RX_LOSS = 6               # antenna / case losses on a PC's Wi-Fi adapter
PATH_N = 3.0              # how fast Wi-Fi fades indoors (2 = open air, 3 = a house, 4 = several walls / floors)
DIST_SPREAD = 1.8         # likely range = estimate / 1.8 ... estimate x 1.8
MANUF_URL = "https://www.wireshark.org/download/automated/data/manuf"   # maker of every hardware address block (IEEE list)
MANUF_FILE = _hs.HS_DIR / "manuf.txt"
MANUF_MAX_AGE = 60 * 86400
LEARN_SECS = 6 * 3600     # access points first heard in this long after the first scan are "already here", not "new"
CLOSE_PCT = 75            # a new access point at least this strong is "close by"
LOCATION_URI = "ms-settings:privacy-location"

# Names broadcast by cheap camera modules while they wait to be set up. Mostly the generic chips inside "nanny cams",
# clocks, chargers and smoke-detector cameras; the second list is ordinary home cameras from known brands.
SPYCAM = re.compile(r"^(MV\d{5,}|HDWiFiCam|IPC[-_]\w+|IPC\d{4,}|IP[-_ ]?CAM|A9[-_]\w+|LookCam|BVCAM|V380|HiCam|WiFi[-_ ]?Cam|DGK-|DGO-|"
                    r"ACCQ\d+|CLOUDCAM|Care-AP|HIDVCAM|JXLCAM|P2PCam|CamHi|IPC365|YCC365|MINI[-_ ]?CAM|SPY[-_ ]?CAM|NannyCam|"
                    r"ESP32[-_ ]?CAM|TOPK|AIS[-_ ]?CAM|X5[-_]\w{6,}|DVR[-_]\w+|WIFICAMERA)", re.I)
HOMECAM = re.compile(r"^(EZVIZ_|Reolink_|Wyze_?Setup|WyzeCam|blink-|Ring-\w{6}|Ring Setup|TAPO_C|Tapo_Cam|Arlo|eufy ?Cam|YI[-_ ]?Cam|"
                     r"Amcrest|Nest-?Cam|Foscam|Dahua|HIK-?|Hikvision|IMOU|SimpliSafe|MERCUSYS_Cam)", re.I)

_W = {"started": None, "state": "starting", "err": None, "last": None, "conn": {}, "n": 0}


# ----------------------------------------------------------------------------------------------- storage
def _db():
    c = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    with _db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS ap(bssid TEXT PRIMARY KEY, ssid TEXT, auth TEXT, band TEXT, chan TEXT, first REAL, last REAL,
            n INT, sig_last INT, sig_best INT, flag TEXT DEFAULT '', why TEXT DEFAULT '', ok INT DEFAULT 0);
        CREATE INDEX IF NOT EXISTS ap_last ON ap(last);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS ap_hour(bssid TEXT, hour INT, n INT, rmin INT, rmax INT, rsum INT, PRIMARY KEY(bssid, hour));
        CREATE INDEX IF NOT EXISTS ap_hour_h ON ap_hour(hour);
        CREATE TABLE IF NOT EXISTS rh_warn(k TEXT PRIMARY KEY, entry TEXT, ts REAL, sev TEXT, msg TEXT, found REAL);
        CREATE TABLE IF NOT EXISTS net_log(ts REAL, kind TEXT, msg TEXT);
        CREATE TABLE IF NOT EXISTS snaps(file TEXT PRIMARY KEY, ts REAL, reason TEXT);
        CREATE TABLE IF NOT EXISTS away_log(sid REAL, ts REAL, kind TEXT, what TEXT);
        CREATE INDEX IF NOT EXISTS away_log_sid ON away_log(sid);
        CREATE TABLE IF NOT EXISTS trip(ts REAL, kind TEXT, msg TEXT);
        """)
        have = {r[1] for r in c.execute("PRAGMA table_info(ap)")}
        for col, typ in AP_COLS:
            if col not in have:
                c.execute(f"ALTER TABLE ap ADD COLUMN {col} {typ}")


# Columns added for the LOG (older databases get them on start-up)
AP_COLS = (("rssi_last", "INT"), ("rssi_best", "INT"), ("freq", "INT"), ("tx", "INT"), ("dist", "REAL"), ("dist_min", "REAL"),
           ("radio", "TEXT DEFAULT ''"), ("enc", "TEXT DEFAULT ''"), ("stations", "INT"), ("util", "INT"), ("country", "TEXT DEFAULT ''"),
           ("wps", "TEXT DEFAULT ''"))


def _meta(k, default=None):
    with _db() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def _set_meta(k, v):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


TITLES = {"pc-use": "Device Watch: PC used while you were away", "firmware": "Device Watch: firmware fingerprint changed",
          "wifi-camera": "Device Watch: possible hidden-camera Wi-Fi", "wifi-twin": "Device Watch: copy of your Wi-Fi network",
          "net-router": "Device Watch: something is pretending to be your router", "net-drops": "Device Watch: Wi-Fi keeps being knocked off",
          "net-fakeap": "Device Watch: fake Wi-Fi access point nearby", "cell": "Device Watch: Rayhunter saw a possible fake phone tower",
          "trip-typing": "Device Watch: superhuman typing (USB attack?)", "trip-clipboard": "Device Watch: copied account number was swapped",
          "trip-name": "Device Watch: someone used the decoy log-ins"}


def _alert(level, kind, msg, ref=""):
    _hs._event(level, kind, msg, ref)
    if level >= 2:
        try:
            if _hs._cfg()["notify"]:
                _hs._toast(TITLES.get(kind, "Device Watch: alert"), msg[:240])
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------------------------------- Wi-Fi scan
def _netsh(*args):
    r = subprocess.run(["netsh", "wlan", *args], capture_output=True, timeout=30, creationflags=_hs.NO_WINDOW if IS_WIN else 0)
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def parse_networks(text):
    """`netsh wlan show networks mode=bssid` -> [{ssid, bssid, auth, enc, sig, band, chan, radio, stations, util}]
    (one row per access point; stations / util only when the access point advertises its load)."""
    out, ssid, auth, enc, cur = [], "", "", "", None
    for raw in text.splitlines():
        m = re.match(r"\s*(.+?)\s*:\s?(.*)$", raw)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if re.fullmatch(r"SSID \d+", key):
            ssid, auth, enc, cur = val, "", "", None
        elif re.fullmatch(r"BSSID \d+", key):
            cur = {"ssid": ssid, "bssid": val.lower(), "auth": auth, "enc": enc, "sig": None, "band": "", "chan": "", "radio": "",
                   "stations": None, "util": None}
            out.append(cur)
        elif key == "Authentication":
            auth = val
        elif key == "Encryption":
            enc = val
        elif cur is not None and key == "Signal":
            cur["sig"] = int(val.rstrip("%")) if val.rstrip("%").isdigit() else None
        elif cur is not None and key == "Band":
            cur["band"] = val
        elif cur is not None and key == "Channel":
            cur["chan"] = val
        elif cur is not None and key == "Radio type":
            cur["radio"] = val
        elif cur is not None and key == "Connected Stations":
            cur["stations"] = int(val) if val.isdigit() else None
        elif cur is not None and key == "Channel Utilization":
            u = re.search(r"\((\d+)\s*%\)", val)
            cur["util"] = int(u.group(1)) if u else None
    return [a for a in out if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", a["bssid"])]


# ----------------------------------------------------------------------------------------------- native Wi-Fi API
# netsh only gives a 0-100 "quality"; the Wi-Fi API gives the signal in dBm, the exact frequency and the raw
# broadcast (information elements), and can ask the adapter for a fresh scan instead of reading a stale list.
class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]


class _IFACE(ctypes.Structure):  # WLAN_INTERFACE_INFO
    _fields_ = [("guid", _GUID), ("desc", ctypes.c_wchar * 256), ("state", ctypes.c_int)]


class _BSS(ctypes.Structure):  # WLAN_BSS_ENTRY (DOT11_SSID and WLAN_RATE_SET written out flat; same layout)
    _fields_ = [("ssid_len", ctypes.c_ulong), ("ssid", ctypes.c_ubyte * 32), ("phy", ctypes.c_ulong), ("bssid", ctypes.c_ubyte * 6),
                ("bss_type", ctypes.c_int), ("phy_type", ctypes.c_int), ("rssi", ctypes.c_long), ("quality", ctypes.c_ulong),
                ("in_reg", ctypes.c_ubyte), ("beacon", ctypes.c_ushort), ("ts", ctypes.c_ulonglong), ("host_ts", ctypes.c_ulonglong),
                ("cap", ctypes.c_ushort), ("freq_khz", ctypes.c_ulong), ("rate_len", ctypes.c_ulong), ("rates", ctypes.c_ushort * 126),
                ("ie_off", ctypes.c_ulong), ("ie_size", ctypes.c_ulong)]


_WL = {"lib": None, "h": None}
_NL = threading.Lock()          # one Wi-Fi API call at a time (the minute scan, a camera search and the finder share the handle)
_SCAN_LOCK = threading.Lock()   # one full scan + log update at a time
WPS_KEYS = {0x1021: "maker", 0x1023: "model", 0x1024: "model_no", 0x1011: "name"}


def _wlan():
    if _WL["h"] is None:
        lib = ctypes.WinDLL("wlanapi.dll")
        h, ver = ctypes.c_void_p(), ctypes.c_ulong()
        rc = lib.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(h))
        if rc:
            raise OSError(rc, "WlanOpenHandle failed")
        _WL.update(lib=lib, h=h)
    return _WL["lib"], _WL["h"]


def _ifaces(lib, h):
    p = ctypes.c_void_p()
    rc = lib.WlanEnumInterfaces(h, None, ctypes.byref(p))
    if rc:
        raise OSError(rc, "WlanEnumInterfaces failed")
    try:
        n = ctypes.c_ulong.from_address(p.value).value
        return [_GUID.from_buffer_copy(ctypes.string_at(p.value + 8 + i * ctypes.sizeof(_IFACE), ctypes.sizeof(_GUID))) for i in range(n)]
    finally:
        lib.WlanFreeMemory(p)


def _txt(b):
    s = b.decode("utf-8", "replace")
    return "".join(ch for ch in s if ch.isprintable() and ch != "�").strip()[:64]


def parse_ies(ies):
    """Raw beacon information elements -> {tx (announced transmit power, dBm), country, wps: {maker, model, model_no, name}}."""
    info = {"tx": None, "country": "", "wps": {}}
    i = 0
    while i + 2 <= len(ies):
        eid, ln = ies[i], ies[i + 1]
        body = ies[i + 2:i + 2 + ln]
        i += 2 + ln
        if len(body) < ln:
            break
        if eid == 7 and ln >= 2 and body[:2].isalpha():          # Country
            info["country"] = body[:2].decode("ascii").upper()
        elif eid == 35 and ln >= 1:                               # TPC report: the power this frame was sent at
            tx = struct.unpack("b", body[:1])[0]
            if 0 < tx <= 30:
                info["tx"] = tx
        elif eid == 221 and body[:4] == b"\x00\x50\xf2\x04":      # Wi-Fi Protected Setup: maker / model / device name
            j = 4
            while j + 4 <= len(body):
                t, n = struct.unpack(">HH", body[j:j + 4])
                v = body[j + 4:j + 4 + n]
                j += 4 + n
                if t in WPS_KEYS and v:
                    s = _txt(v)
                    if s and s.strip("0 ") and s.lower() not in ("n/a", "none", "unknown", "default"):
                        info["wps"][WPS_KEYS[t]] = s
    return info


def native_scan(wait=SCAN_WAIT):
    """bssid -> {ssid, rssi (dBm), freq (MHz), tx, country, wps}. Raises PermissionError when Location is off."""
    with _NL:
        lib, h = _wlan()
        guids = _ifaces(lib, h)
        for g in guids:
            lib.WlanScan(h, ctypes.byref(g), None, None, None)
    if guids and wait:
        time.sleep(wait)
    with _NL:
        return _read_bss(lib, h, guids)


def _read_bss(lib, h, guids):
    out, esz = {}, ctypes.sizeof(_BSS)
    for g in guids:
        p = ctypes.c_void_p()
        rc = lib.WlanGetNetworkBssList(h, ctypes.byref(g), None, 3, 0, None, ctypes.byref(p))
        if rc == 5:
            raise PermissionError("Location services are off")
        if rc or not p.value:
            continue
        try:
            n = ctypes.c_ulong.from_address(p.value + 4).value
            for i in range(n):
                addr = p.value + 8 + i * esz
                e = _BSS.from_address(addr)
                bssid = ":".join(f"{b:02x}" for b in e.bssid)
                if bssid in out and out[bssid]["rssi"] >= e.rssi:
                    continue
                ies = ctypes.string_at(addr + e.ie_off, e.ie_size) if e.ie_size and e.ie_size < 8192 else b""
                out[bssid] = {"ssid": _txt(bytes(e.ssid[:min(e.ssid_len, 32)])), "rssi": int(e.rssi), "freq": int(e.freq_khz // 1000), **parse_ies(ies)}
        finally:
            lib.WlanFreeMemory(p)
    return out


# ----------------------------------------------------------------------------------------------- distance + maker
def pct_to_dbm(p):
    """Windows' 0-100 signal quality is linear from -100 dBm (0) to -50 dBm (100)."""
    return None if p is None else round(p / 2 - 100)


def chan_freq(band, chan):
    try:
        c = int(str(chan).split()[0])
    except (ValueError, IndexError):
        return None
    if str(band).startswith("6"):
        return 5950 + 5 * c
    if c == 14:
        return 2484
    return 2407 + 5 * c if c < 14 else 5000 + 5 * c


def distance(rssi, freq=None, tx=None):
    """Rough metres from signal strength: free-space loss at one metre for this frequency, then PATH_N fall-off."""
    if rssi is None:
        return None
    at_1m = (tx or TX_DBM) - (20 * math.log10(freq or 2437) - 27.55) - RX_LOSS
    return round(max(0.5, 10 ** ((at_1m - rssi) / (10 * PATH_N))), 1)


def dist_word(m):
    if m is None:
        return ""
    return ("same room" if m < 4 else "next room" if m < 9 else "same building / next door" if m < 20
            else "neighbours" if m < 45 else "down the street")


_OUI = {"map": None, "busy": False}


def _load_manuf():
    """Read the maker list (downloading it first if missing or over two months old)."""
    try:
        old = not MANUF_FILE.exists() or time.time() - MANUF_FILE.stat().st_mtime > MANUF_MAX_AGE
        if old:
            req = urllib.request.Request(MANUF_URL, headers={"User-Agent": "DeviceWatch/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if len(data) > 500_000:
                tmp = MANUF_FILE.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(MANUF_FILE)
    except Exception as e:  # noqa: BLE001
        print(f"guard: maker list download: {e}")
    m = {}
    try:
        for line in MANUF_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line[0] == "#":
                continue
            cols = [x.strip() for x in line.split("\t")]
            if len(cols) < 2:
                continue
            pre, _, bits = cols[0].partition("/")
            hx = pre.replace(":", "").replace("-", "").lower()
            key = hx[:int(bits) // 4] if bits.isdigit() else hx[:6]
            m[key] = cols[2] if len(cols) > 2 and cols[2] else cols[1]
    except OSError:
        return
    _OUI["map"] = m


def maker(bssid):
    """(maker name, guessed). A made-up (locally administered) address gets a guess from the same address with that bit
    cleared: routers make their guest / mesh addresses that way, but phone hotspots use random ones, so it is only a guess."""
    m = _OUI["map"]
    if not m:
        return "", False
    hx = bssid.replace(":", "").lower()
    for k in (9, 7, 6):
        if hx[:k] in m:
            return m[hx[:k]], False
    if _private_addr(bssid):
        hx = f"{int(hx[:2], 16) & ~2:02x}" + hx[2:]
        for k in (9, 7, 6):
            if hx[:k] in m:
                return m[hx[:k]], True
    return "", False


def parse_interface(text):
    """`netsh wlan show interfaces` -> {ssid, bssid} of the network this PC is joined to (empty if none)."""
    d = {}
    for raw in text.splitlines():
        m = re.match(r"\s*(.+?)\s*:\s?(.*)$", raw)
        if m:
            d.setdefault(m.group(1).strip(), m.group(2).strip())
    if (d.get("State") or "").lower() != "connected":
        return {}
    return {"ssid": d.get("SSID", ""), "bssid": (d.get("AP BSSID") or d.get("BSSID") or "").lower()}


def _private_addr(bssid):
    """Locally-administered hardware address: made up by software, as phone hotspots, guest networks and extenders do."""
    try:
        return bool(int(bssid[:2], 16) & 2)
    except ValueError:
        return False


def _open(auth):
    return (auth or "").strip().lower() in ("open", "none")


def _judge(a, mine_ssids, known_mine, learning):
    """Flag + reason + alert level for an access point heard for the first time."""
    ssid = a["ssid"]
    if ssid and SPYCAM.match(ssid):
        return "camera", ("This is the kind of name a small generic Wi-Fi camera broadcasts while it waits to be set up "
                          "(the modules inside clock, charger and smoke-detector cameras). Stronger signal = closer."), 2
    if ssid and HOMECAM.match(ssid):
        return "homecam", "A home security camera or doorbell in set-up mode. Normal if you or a neighbour are installing one.", 1
    if ssid and ssid in mine_ssids and not learning:
        if _open(a["auth"]) and any(not _open(x) for x in known_mine.get(ssid, [])):
            return "twin", (f"An access point is using your network's name \"{ssid}\" but with NO password. Your devices may join it "
                            "automatically and everything they send would pass through it. This is how an \"evil twin\" attack looks."), 2
        return "twin?", (f"A new access point is using your network's name \"{ssid}\". Normal if you added a mesh unit or extender, "
                         "or if this is a shared / guest network with several access points. If not, something is imitating it."), 1
    if not learning and (a["sig"] or 0) >= CLOSE_PCT:
        what = "a phone hotspot, travel router or gadget" if _private_addr(a["bssid"]) else "a router or access point"
        return "new", f"A new Wi-Fi network appeared close to this PC ({a['sig']}% signal). Likely {what} that was not here before.", 1
    return "", "", 0


_RECENT = collections.defaultdict(lambda: collections.deque(maxlen=5))  # bssid -> last few dBm readings (smooths distance)


def _enrich(a, nat):
    """Add dBm, frequency, smoothed distance and broadcast details to one netsh row."""
    n = nat.get(a["bssid"]) or {}
    a["rssi"] = n.get("rssi") if n.get("rssi") is not None else pct_to_dbm(a["sig"])
    a["freq"] = n.get("freq") or chan_freq(a["band"], a["chan"])
    a["tx"] = n.get("tx")
    a["country"] = n.get("country") or ""
    a["wps"] = json.dumps(n["wps"]) if n.get("wps") else ""
    if not a["ssid"] and n.get("ssid"):
        a["ssid"] = n["ssid"]
    if a["rssi"] is not None:
        _RECENT[a["bssid"]].append(a["rssi"])
    med = statistics.median(_RECENT[a["bssid"]]) if _RECENT[a["bssid"]] else None
    a["dist"] = distance(med, a["freq"], a["tx"])


def _scan(now):
    try:
        nat = native_scan() if IS_WIN else {}
        _W["native"] = bool(nat)
    except Exception:  # noqa: BLE001  (no Wi-Fi API, or Location off: netsh below explains which)
        nat = {}
        _W["native"] = False
    text = _netsh("show", "networks", "mode=bssid")
    low = text.lower()
    if "location permission" in low or "location services" in low:
        _W.update(state="location", err=None)
        return
    if "no wireless interface" in low or "is not running" in low:
        _W.update(state="none", err=None)
        return
    aps = parse_networks(text)
    if not aps and "ssid" not in low:
        _W.update(state="error", err=" ".join(text.split())[:200] or "netsh returned nothing")
        return
    for a in aps:
        _enrich(a, nat)
    conn = parse_interface(_netsh("show", "interfaces"))
    start = float(_meta("wifi_first") or 0)
    if not start:
        start = now
        _set_meta("wifi_first", now)
    learning = now - start < LEARN_SECS
    try:
        mine = set(json.loads(_meta("my_ssids") or "[]"))
    except ValueError:
        mine = set()
    if conn.get("ssid") and conn["ssid"] not in mine:
        mine.add(conn["ssid"])
        _set_meta("my_ssids", json.dumps(sorted(mine)))
    events = []
    with _db() as c:
        known_mine = {}
        for r in c.execute("SELECT ssid, auth FROM ap WHERE ssid<>''"):
            if r["ssid"] in mine:
                known_mine.setdefault(r["ssid"], []).append(r["auth"])
        seen = {r[0] for r in c.execute("SELECT bssid FROM ap")}
        hour = int(now // 3600)
        for a in aps:
            if a["bssid"] not in seen:
                trusted = a["bssid"] == conn.get("bssid")
                flag, why, lvl = ("", "", 0) if trusted else _judge(a, mine, known_mine, learning)
                c.execute("""INSERT INTO ap(bssid, ssid, auth, band, chan, first, last, n, sig_last, sig_best, flag, why, ok,
                             rssi_last, rssi_best, freq, tx, dist, dist_min, radio, enc, stations, util, country, wps)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (a["bssid"], a["ssid"], a["auth"], a["band"], a["chan"], now, now, 1, a["sig"], a["sig"], flag, why, 1 if trusted else 0,
                           a["rssi"], a["rssi"], a["freq"], a["tx"], a["dist"], a["dist"], a["radio"], a["enc"], a["stations"], a["util"],
                           a["country"], a["wps"]))
                seen.add(a["bssid"])
                if lvl:
                    events.append((lvl, flag, a, why))
            else:
                c.execute("""UPDATE ap SET last=:now, n=n+1, sig_last=:sig, sig_best=MAX(COALESCE(sig_best, 0), COALESCE(:sig, 0)),
                             auth=:auth, band=:band, chan=:chan, ssid=CASE WHEN :ssid<>'' THEN :ssid ELSE ssid END,
                             rssi_last=:rssi,
                             rssi_best=CASE WHEN :rssi IS NULL THEN rssi_best WHEN rssi_best IS NULL OR :rssi > rssi_best THEN :rssi ELSE rssi_best END,
                             freq=:freq, tx=COALESCE(:tx, tx), dist=:dist,
                             dist_min=CASE WHEN :dist IS NULL THEN dist_min WHEN dist_min IS NULL OR :dist < dist_min THEN :dist ELSE dist_min END,
                             radio=:radio, enc=:enc, stations=:stations, util=:util,
                             country=CASE WHEN :country<>'' THEN :country ELSE country END,
                             wps=CASE WHEN :wps<>'' THEN :wps ELSE wps END WHERE bssid=:bssid""",
                          {**{k: a[k] for k in ("sig", "auth", "band", "chan", "ssid", "rssi", "freq", "tx", "dist", "radio", "enc",
                                                "stations", "util", "country", "wps", "bssid")}, "now": now})
            if a["rssi"] is not None:
                c.execute("""INSERT INTO ap_hour VALUES(?,?,1,?,?,?) ON CONFLICT(bssid, hour) DO UPDATE SET n=n+1,
                             rmin=MIN(rmin, excluded.rmin), rmax=MAX(rmax, excluded.rmax), rsum=rsum+excluded.rsum""",
                          (a["bssid"], hour, a["rssi"], a["rssi"], a["rssi"]))
        c.execute("DELETE FROM ap_hour WHERE hour < ?", (hour - KEEP_DAYS * 24,))
        c.execute("DELETE FROM ap WHERE last < ?", (now - KEEP_DAYS * 86400,))
    for lvl, flag, a, why in events:
        title = {"camera": "Possible hidden-camera Wi-Fi", "homecam": "Camera in set-up mode", "twin": "Copy of your Wi-Fi network (no password)",
                 "twin?": "New access point using your Wi-Fi name", "new": "New Wi-Fi network close by"}[flag]
        far = f", roughly {a['dist']:g} m away" if a.get("dist") else ""
        _alert(lvl, "wifi-" + flag.rstrip("?"), f"{title}: \"{a['ssid'] or '(hidden name)'}\" ({a['bssid']}, {a['sig']}% signal{far}, {a['auth'] or '?'}). {why}", a["bssid"])
    _W.update(state="ok", err=None, last=now, conn=conn, n=len(aps))


def _wifi_loop():
    while True:
        try:
            with _SCAN_LOCK:
                _scan(time.time())
        except Exception as e:  # noqa: BLE001
            _W.update(state="error", err=f"{type(e).__name__}: {e}")
        time.sleep(SCAN_SECS if _W["state"] == "ok" else 20)


def wifi(q=None):
    _init_db()
    now = time.time()
    with _db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM ap WHERE last >= ? OR (flag<>'' AND ok=0 AND last >= ?) ORDER BY sig_last DESC",
                                           (now - 600, now - 14 * 86400))]
        total = c.execute("SELECT COUNT(*) FROM ap").fetchone()[0]
    start = float(_meta("wifi_first") or 0)
    conn = _W["conn"] or {}
    rows = [_out(r, now, conn) for r in rows]
    try:
        import device  # noqa: PLC0415
        with device._db() as dc:
            alerts = [dict(r) for r in dc.execute("SELECT ts, level, msg FROM events WHERE kind LIKE 'wifi-%' AND ts > ? ORDER BY ts DESC LIMIT 30",
                                                  (now - 14 * 86400,))]
    except Exception:  # noqa: BLE001
        alerts = []
    return {"state": _W["state"], "err": _W["err"], "last": _W["last"], "conn": conn, "aps": rows, "total": total, "alerts": alerts,
            "learning": bool(start) and now - start < LEARN_SECS, "learn_until": start + LEARN_SECS if start else None,
            "learn_hours": LEARN_SECS / 3600, "close_pct": CLOSE_PCT, "native": _W.get("native", False), "makers": bool(_OUI["map"])}


def _out(r, now, conn):
    """One stored access point -> what the page shows (adds maker, distance words and range, flags)."""
    r = dict(r)
    r["now"] = now - r["last"] < 180
    r["private"] = _private_addr(r["bssid"])
    r["open"] = _open(r["auth"])
    r["mine"] = r["bssid"] == (conn or {}).get("bssid")
    r["maker"], r["maker_guess"] = maker(r["bssid"])
    try:
        r["wps"] = json.loads(r.get("wps") or "{}")
    except ValueError:
        r["wps"] = {}
    d = r.get("dist")
    r["dist_lo"], r["dist_hi"] = (round(d / DIST_SPREAD, 1), round(d * DIST_SPREAD, 1)) if d else (None, None)
    r["dist_word"] = dist_word(d)
    r["closest_word"] = dist_word(r.get("dist_min"))
    return r


def wifi_log(q=None):
    """Every access point ever heard (newest first), for the LOG table and CSV export."""
    _init_db()
    now = time.time()
    with _db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM ap ORDER BY last DESC LIMIT 5000")]
        hours = {r[0]: r[1] for r in c.execute("SELECT bssid, COUNT(*) FROM ap_hour GROUP BY bssid")}
    conn = _W["conn"] or {}
    out = []
    for r in rows:
        o = _out(r, now, conn)
        o["hours"] = hours.get(r["bssid"], 0)
        out.append(o)
    return {"aps": out, "total": len(out), "keep_days": KEEP_DAYS, "scan_secs": SCAN_SECS}


def wifi_ap(q):
    """One access point: everything stored, plus its hourly signal over the last N days and which hours of the day it is around."""
    _init_db()
    bssid = (q.get("bssid") or "").lower()
    days = max(1, min(KEEP_DAYS, int(q.get("days") or 14)))
    now = time.time()
    with _db() as c:
        r = c.execute("SELECT * FROM ap WHERE bssid=?", (bssid,)).fetchone()
        if not r:
            raise ValueError("unknown access point")
        hrs = c.execute("SELECT hour, n, rmin, rmax, rsum FROM ap_hour WHERE bssid=? AND hour >= ? ORDER BY hour",
                        (bssid, int(now // 3600) - days * 24)).fetchall()
        same = [dict(x) for x in c.execute("SELECT bssid, band, chan, last FROM ap WHERE ssid=? AND ssid<>'' AND bssid<>? ORDER BY last DESC LIMIT 20",
                                            (r["ssid"], bssid))]
    o = _out(r, now, _W["conn"] or {})
    hist, by_hod = [], collections.defaultdict(int)
    for h, n, lo, hi, s in hrs:
        avg = s / n if n else None
        hist.append({"ts": h * 3600, "avg": round(avg, 1) if avg is not None else None, "min": lo, "max": hi, "n": n,
                     "dist": distance(avg, o.get("freq"), o.get("tx"))})
        by_hod[time.localtime(h * 3600).tm_hour] += 1
    o.update(hist=hist, days=days, hod=[by_hod.get(i, 0) for i in range(24)], same_name=same,
             model={"tx": TX_DBM, "rx_loss": RX_LOSS, "n": PATH_N, "spread": DIST_SPREAD})
    return o


# ----------------------------------------------------------------------------------------------- camera search
# Brands that make Wi-Fi cameras / doorbells / baby monitors (matched against the hardware maker and what the device says
# about itself), and chip makers whose Wi-Fi chips are inside most cheap no-name cameras (and many other gadgets).
CAM_MAKERS = re.compile(r"hikvision|dahua|ezviz|uniview|axis comm|hanwha|vivotek|foscam|amcrest|reolink|wyze|arlo|immedia|blink|ring llc|"
                        r"eufy|anker innov|imou|lorex|swann|annke|xiongmai|yi technology|xiaoyi|qihoo|nest labs|canary connect|simplisafe|"
                        r"zmodo|sricam|vstarcam|wansview|escam|ctronics|jooan|hiseeu|tenvis|geeni|merkury|tapo|owlet|nanit|vtech", re.I)
CAM_CHIPS = re.compile(r"espressif|beken|ingenic|anyka|fullhan|goke|sigmastar|hisilicon|shenzhen bilian|tuya|grain media|altobeam|"
                       r"amlogic|ssv ?tech|south silicon", re.I)
CAM_WORDS = re.compile(r"\b(ip ?cam\w*|ipc\w*|nvr|dvr|camera|cam|webcam|doorbell|baby ?monitor|spy)\b", re.I)


def _cam_judge(ssid, mk, wps):
    """-> (level 0-3, [reasons]) for one access point or network device."""
    why, lvl = [], 0
    said = " ".join((wps or {}).values())
    if ssid and SPYCAM.match(ssid):
        lvl = max(lvl, 3)
        why.append("its name is one cheap hidden cameras broadcast while waiting to be set up")
    elif ssid and HOMECAM.match(ssid):
        lvl = max(lvl, 2)
        why.append("its name is one home cameras / doorbells use in set-up mode")
    if said and (CAM_WORDS.search(said) or CAM_MAKERS.search(said)):
        lvl = max(lvl, 3)
        why.append(f"it describes itself as \"{said[:80]}\"")
    if mk and CAM_MAKERS.search(mk):
        lvl = max(lvl, 2)
        why.append(f"its hardware is made by {mk}, a camera maker")
    elif mk and CAM_CHIPS.search(mk):
        lvl = max(lvl, 1)
        why.append(f"its Wi-Fi chip is by {mk}, used in most no-name cameras (and in plugs, bulbs and other gadgets)")
    if ssid and CAM_WORDS.search(ssid) and lvl < 2:
        lvl = 2
        why.append("its network name mentions a camera")
    return lvl, why


def _arp():
    """This PC's own address cache: devices it has talked to on the networks it is joined to (read only, sends nothing)."""
    r = subprocess.run(["arp", "-a"], capture_output=True, timeout=20, creationflags=_hs.NO_WINDOW if IS_WIN else 0)
    out, net = [], ""
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        m = re.match(r"\s*Interface:\s*([\d.]+)", line)
        if m:
            net = m.group(1)
            continue
        m = re.match(r"\s*([\d.]+)\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})\s+(\w+)", line, re.I)
        if not m:
            continue
        ip, mac = m.group(1), m.group(2).lower().replace("-", ":")
        first = int(ip.split(".")[0])
        if mac == "ff:ff:ff:ff:ff:ff" or mac.startswith("01:00:5e") or first >= 224 or ip.endswith(".255"):
            continue
        out.append({"ip": ip, "mac": mac, "via": net})
    return out


def camera_search(q=None):
    """Fresh Wi-Fi scan now, then every access point in range and every device in this PC's address cache, scored for
    signs of being a camera, closest first."""
    _init_db()
    t0 = time.time()
    with _SCAN_LOCK:
        _scan(t0)
    if _W["state"] != "ok":
        return {"state": _W["state"], "err": _W["err"], "aps": [], "lan": [], "took": round(time.time() - t0, 1)}
    with _db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM ap WHERE last >= ?", (t0 - 5,))]
    conn = _W["conn"] or {}
    aps = []
    for r in rows:
        o = _out(r, time.time(), conn)
        o["cam"], o["cam_why"] = _cam_judge(o["ssid"], o["maker"] if not o["maker_guess"] else "", o["wps"])
        aps.append(o)
    aps.sort(key=lambda a: (-a["cam"], a["dist"] if a["dist"] is not None else 1e9))
    lan = []
    try:
        for d in _arp():
            mk, guess = maker(d["mac"])
            lvl, why = _cam_judge("", mk if not guess else "", {})
            lan.append({**d, "maker": mk, "maker_guess": guess, "private": _private_addr(d["mac"]), "cam": lvl, "cam_why": why})
    except Exception as e:  # noqa: BLE001
        lan = [{"error": str(e)}]
    lan.sort(key=lambda d: -d.get("cam", 0))
    return {"state": "ok", "aps": aps, "lan": lan, "took": round(time.time() - t0, 1), "conn": conn, "native": _W.get("native", False)}


_LIVE = collections.defaultdict(lambda: collections.deque(maxlen=120))  # bssid -> (time, dBm) readings for the finder


def wifi_find(q):
    """Live signal of one access point for the finder. Each call asks the adapter for a fresh scan and reads the newest
    results (from the scan before, a few seconds old). Stronger = closer; warmer / colder is the part to trust."""
    bssid = (q.get("bssid") or "").lower()
    now = time.time()
    rssi, freq, tx = None, None, None
    try:
        hit = native_scan(wait=0).get(bssid) if IS_WIN else None
        if hit:
            rssi, freq, tx = hit["rssi"], hit["freq"], hit["tx"]
    except Exception:  # noqa: BLE001
        for a in parse_networks(_netsh("show", "networks", "mode=bssid")):
            if a["bssid"] == bssid:
                rssi, freq = pct_to_dbm(a["sig"]), chan_freq(a["band"], a["chan"])
    live = _LIVE[bssid]
    if rssi is not None and (not live or live[-1][1] != rssi or now - live[-1][0] > 8):
        live.append((now, rssi))
    pts = [(t, r) for t, r in live if now - t <= 120]
    out = {"bssid": bssid, "pts": [[round(t - now, 1), r] for t, r in pts], "age": round(now - pts[-1][0], 1) if pts else None,
           "rssi": None, "trend": "", "dist": None, "best": max((r for _t, r in pts), default=None)}
    recent = [r for t, r in pts if now - t <= 8]
    if recent:
        level = statistics.median(recent)
        before = [r for t, r in pts if 8 < now - t <= 25]
        diff = level - statistics.median(before) if before else 0
        if freq is None:
            with _db() as c:
                row = c.execute("SELECT freq, tx FROM ap WHERE bssid=?", (bssid,)).fetchone()
            freq, tx = (row["freq"], row["tx"]) if row else (None, None)
        d = distance(level, freq, tx)
        out.update(rssi=round(level), trend="warmer" if diff >= 3 else "colder" if diff <= -3 else "steady",
                   dist=d, dist_lo=round(d / DIST_SPREAD, 1), dist_hi=round(d * DIST_SPREAD, 1), word=dist_word(d))
    return out


def wifi_ok(q):
    """Mark a flagged access point as known / fine (or undo)."""
    _init_db()
    bssid = (q.get("bssid") or "").lower()
    with _db() as c:
        cur = c.execute("UPDATE ap SET ok=? WHERE bssid=?", (1 if q.get("on", "1") == "1" else 0, bssid))
    if not cur.rowcount:
        raise ValueError("unknown access point")
    return {"ok": True}


# Settings pages the app may open (fixed list: nothing typed by a page is ever run).
SETTINGS = {
    "location": LOCATION_URI,
    "coreisolation": "windowsdefender://coreisolation",
    "ransomware": "windowsdefender://ransomwareprotection",
    "appbrowser": "windowsdefender://appbrowser",
    "virus": "windowsdefender://threatsettings",
    "encryption": "ms-settings:deviceencryption",
    "update": "ms-settings:windowsupdate",
    "accounts": "ms-settings:otherusers",
    "signin": "ms-settings:signinoptions",
    "remote": "ms-settings:remotedesktop",
    "sharing": "ms-settings:network-advancedsettings",
    "autoplay": "ms-settings:autoplay",
    "camera": "ms-settings:privacy-webcam",
    "uac": "useraccountcontrolsettings.exe",
}


def open_setting(q):
    import os  # noqa: PLC0415
    target = SETTINGS.get(q.get("k") or "")
    if not target or not IS_WIN:
        raise ValueError("unknown setting")
    os.startfile(target)  # noqa: S606
    return {"ok": True}


# ----------------------------------------------------------------------------------------------- firmware / boot fingerprint
PCRS = {0: "firmware code (the BIOS itself)", 1: "BIOS settings", 2: "firmware of plug-in hardware", 3: "plug-in hardware settings",
        4: "boot loader", 7: "Secure Boot keys and on/off state"}
_B = {"last": None, "err": None, "cur": None}


def _pcr_read(indexes):
    """SHA-256 boot measurements straight from the TPM (read-only command; needs no administrator rights)."""
    tbs = ctypes.WinDLL("tbs.dll")

    class Params(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("flags", ctypes.c_uint32)]

    ctx = ctypes.c_void_p()
    rc = tbs.Tbsi_Context_Create(ctypes.byref(Params(2, 4)), ctypes.byref(ctx))  # version 2, TPM 2.0
    if rc:
        raise RuntimeError(f"no usable TPM 2.0 security chip (code {rc & 0xffffffff:#x})")
    out = {}
    try:
        for i in indexes:
            bm = bytearray(3)
            bm[i // 8] |= 1 << (i % 8)
            body = struct.pack(">IHB", 1, 0x000B, 3) + bytes(bm)          # one selection: SHA-256 bank, 3-byte bitmap
            cmd = struct.pack(">HII", 0x8001, 10 + len(body), 0x17E) + body  # TPM2_PCR_Read
            buf = (ctypes.c_ubyte * 512)()
            n = ctypes.c_uint32(512)
            rc = tbs.Tbsip_Submit_Command(ctx, 0, 200, cmd, len(cmd), buf, ctypes.byref(n))
            raw = bytes(buf[:n.value])
            if rc or len(raw) < 10 or struct.unpack(">I", raw[6:10])[0]:
                continue
            off = 14
            cnt = struct.unpack(">I", raw[off:off + 4])[0]
            off += 4 + cnt * 6
            if struct.unpack(">I", raw[off:off + 4])[0] < 1:
                continue
            ln = struct.unpack(">H", raw[off + 4:off + 6])[0]
            out[i] = raw[off + 6:off + 6 + ln].hex()
    finally:
        tbs.Tbsip_Context_Close(ctx)
    return out


def _bios():
    import winreg  # noqa: PLC0415
    out = {}
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as k:
        for name, key in (("BIOSVendor", "vendor"), ("BIOSVersion", "version"), ("BIOSReleaseDate", "date"), ("SystemProductName", "model")):
            try:
                out[key] = str(winreg.QueryValueEx(k, name)[0])
            except OSError:
                out[key] = ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\SecureBoot\State") as k:
            out["secureboot"] = int(winreg.QueryValueEx(k, "UEFISecureBootEnabled")[0])
    except OSError:
        out["secureboot"] = None
    return out


def boot_check(now=None):
    """Compare this start-up's fingerprint with every one seen before; log anything never seen."""
    now = now or time.time()
    _init_db()
    cur = _bios()
    try:
        cur["pcr"] = {str(k): v for k, v in _pcr_read(list(PCRS)).items()}
        cur["tpm"] = bool(cur["pcr"])
    except Exception as e:  # noqa: BLE001
        cur["pcr"], cur["tpm"], cur["tpm_err"] = {}, False, str(e)
    try:
        st = json.loads(_meta("boot_fp") or "{}")
    except ValueError:
        st = {}
    first = not st
    seen = st.setdefault("seen", {})          # pcr index -> [every value ever measured]
    prev = st.get("last") or {}
    msgs = []
    if not first:
        bv = f"{cur.get('vendor')} {cur.get('version')} ({cur.get('date')})"
        pv = f"{prev.get('vendor')} {prev.get('version')} ({prev.get('date')})"
        bios_changed = bv != pv
        if bios_changed:
            msgs.append((2, f"The BIOS version changed: was {pv}, now {bv}. Expected only if you installed a BIOS update yourself."))
        if prev.get("secureboot") != cur.get("secureboot") and cur.get("secureboot") is not None:
            msgs.append((2 if not cur["secureboot"] else 1, f"Secure Boot was switched {'ON' if cur['secureboot'] else 'OFF'} in the BIOS."))
        for i, label in PCRS.items():
            v = cur["pcr"].get(str(i))
            if not v or v in seen.get(str(i), []) or not seen.get(str(i)):
                continue
            if i == 0:
                msgs.append((2, "The firmware code measured by the security chip changed" + (" (this goes with the BIOS version change above)." if bios_changed else
                                 ", but the BIOS version number did not. Firmware should only change when you update the BIOS. "
                                 "If you did not, treat this PC as tampered with below Windows.")))
            elif i == 4:
                msgs.append((0, "The boot loader measurement is one not seen before. Normal after a Windows update, or the first time the PC "
                                "starts from hibernation / fast start-up instead of a full start."))
            elif i == 7:
                msgs.append((1, "The Secure Boot key list changed. Windows updates do this a few times a year (blocking old boot loaders); "
                                "it also happens if someone changes Secure Boot keys in the BIOS."))
            else:
                msgs.append((1, f"The security chip's measurement of {label} changed. Expected if you changed BIOS settings or "
                                "added / removed hardware; otherwise somebody else did."))
    for i in PCRS:
        v = cur["pcr"].get(str(i))
        if v and v not in seen.setdefault(str(i), []):
            seen[str(i)].append(v)
            seen[str(i)] = seen[str(i)][-12:]
    st["last"] = {k: cur.get(k) for k in ("vendor", "version", "date", "model", "secureboot")}
    st.setdefault("since", now)
    st["checked"] = now
    _set_meta("boot_fp", json.dumps(st))
    for lvl, m in msgs:
        _alert(lvl, "firmware", "Firmware fingerprint: " + m)
    _B.update(last=now, err=cur.get("tpm_err"), cur=cur)
    return st


def _boot_loop():
    while True:
        try:
            boot_check()
        except Exception as e:  # noqa: BLE001
            _B["err"] = f"{type(e).__name__}: {e}"
        time.sleep(6 * 3600)


def boot(q=None):
    _init_db()
    if _B["cur"] is None or (q or {}).get("refresh"):
        boot_check()
    try:
        st = json.loads(_meta("boot_fp") or "{}")
    except ValueError:
        st = {}
    cur = _B["cur"] or {}
    now = time.time()
    try:
        import device  # noqa: PLC0415
        with device._db() as dc:
            changes = [dict(r) for r in dc.execute("SELECT ts, level, msg FROM events WHERE kind='firmware' ORDER BY ts DESC LIMIT 20")]
    except Exception:  # noqa: BLE001
        changes = []
    try:
        import psutil  # noqa: PLC0415
        booted = psutil.boot_time()
    except Exception:  # noqa: BLE001
        booted = None
    return {"tpm": cur.get("tpm", False), "err": _B["err"], "bios": cur, "since": st.get("since"), "checked": st.get("checked"), "booted": booted,
            "pcrs": [{"i": i, "label": label, "value": (cur.get("pcr") or {}).get(str(i), ""), "variants": len((st.get("seen") or {}).get(str(i), []))}
                     for i, label in PCRS.items()],
            "changes": changes, "days": (now - st["since"]) / 86400 if st.get("since") else 0}


# ----------------------------------------------------------------------------------------------- PC used while you're away
# Reads, every 10 seconds, whether the screen is locked (the lock screen program is running) and how long since the last
# key press or mouse move. During the quiet hours, or any time away mode is on (both set on the BLUETOOTH tab), the PC
# being unlocked, or keyboard / mouse use after 15 quiet minutes, is an alert. Wrong passwords are already reported from
# the Windows security log by the TRAPS watcher.
IDLE_WAKE = 15 * 60
USE_ALERT_EVERY = 30 * 60
_U = {"locked": None, "idle": 0.0, "alerted": 0.0, "log": []}


def _idle_secs():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0


def _locked():
    import psutil  # noqa: PLC0415
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() == "logonui.exe":
            return True
    return False


def _near_now():
    try:
        import bluetooth as bt  # noqa: PLC0415
        p = bt._PRES
        return f" Bluetooth devices near the PC right now: {len(p['close'])} within a couple of metres, {len(p['near'])} in or next to the room."
    except Exception:  # noqa: BLE001
        return ""


def _use_tick(now):
    import bluetooth as bt  # noqa: PLC0415
    locked, idle = _locked(), _idle_secs()
    was_locked, was_idle = _U["locked"], _U["idle"]
    _U["locked"], _U["idle"] = locked, idle
    if was_locked is None:
        return
    what = ""
    if was_locked and not locked:
        what = "The PC was unlocked"
    elif not locked and was_idle >= IDLE_WAKE and idle < 30:
        what = f"Someone used the keyboard or mouse after {int(was_idle // 60)} minutes of nobody touching the PC"
    if not what:
        return
    t = time.strftime("%I:%M %p", time.localtime(now)).lstrip("0")
    _U["log"] = ([{"ts": now, "what": what}] + _U["log"])[:50]
    if not bt.watching(now):
        return
    why = "away mode is on" if bt.away() else "during your quiet hours"
    lvl = 2 if now - _U["alerted"] > USE_ALERT_EVERY else 1
    if lvl == 2:
        _U["alerted"] = now
    _alert(lvl, "pc-use", f"{what} at {t}, {why}.{_near_now()} If this was not you, someone has physical access to the PC. "
                          "Photos and a record of what they do are on the INTRUDER tab.")
    away_record_start(now, what)
    intruder_photo(what.split(" after")[0].lower(), now)


def _use_loop():
    while True:
        try:
            _use_tick(time.time())
        except Exception as e:  # noqa: BLE001
            print(f"guard: use watch: {e}")
        time.sleep(10)


def use_status(q=None):
    try:
        import device  # noqa: PLC0415
        with device._db() as dc:
            ev = [dict(r) for r in dc.execute("SELECT ts, level, msg FROM events WHERE kind IN ('pc-use', 'away', 'bt-visitor', 'bt-presence', 'hotspot-device') "
                                              "AND ts > ? ORDER BY ts DESC LIMIT 40", (time.time() - 14 * 86400,))]
    except Exception:  # noqa: BLE001
        ev = []
    return {"locked": _U["locked"], "idle": _U["idle"], "events": ev}


# ----------------------------------------------------------------------------------------------- attacks on the network
# Three checks for someone attacking the network this PC is on (all read-only except the fake-network test, which sends
# a Wi-Fi "is this network here?" question for a made-up name; it never joins anything and does not drop the connection):
#  * ROUTER: the hardware address behind the router's IP. Someone on the network who wants to read or change this PC's
#    traffic tells it "I am the router" (ARP spoofing). That shows up as the router's address changing, flipping back
#    and forth, or being the same as another device's.
#  * DROPS: Windows logs every Wi-Fi disconnect with a reason. Repeated drops "by the driver" are what a deauthentication
#    attack looks like (knocking devices off so they reconnect, often to an evil twin). This PC's own hotspot adapter is ignored.
#  * FAKE ACCESS POINT: a "Pineapple" / karma device answers for ANY network name a device asks about. The PC asks for a
#    random name that cannot exist; anything that answers is lying.
GW_SECS = 30
NET_SECS = 60
DROP_WINDOW = 30 * 60
DROP_ALERT = 3                # unexplained drops within DROP_WINDOW = alert
FLIP_SECS = 10 * 60           # router address changing back within this long = flip-flopping
KARMA_EVERY = 30 * 60
KARMA_ROUNDS = 3
DROP_OK = re.compile(r"by the user|temporary-disconnect|sleep|hibernat|power|radio|turned off|switched off|disabled|removed|"
                     r"service|policy|roam|another network|connecting to|profile|explicit", re.I)
_N = {"gw": [], "gw_err": None, "gw_last": None, "drops": [], "drop_err": None, "drop_last": None, "drop_alerted": 0.0,
      "karma": None, "karma_busy": False, "karma_next": 0.0, "spoof_alerted": {}}


def _netlog(now, kind, msg):
    with _db() as c:
        c.execute("INSERT INTO net_log VALUES(?,?,?)", (now, kind, msg))
        c.execute("DELETE FROM net_log WHERE ts < ?", (now - KEEP_DAYS * 86400,))


def parse_routes(text):
    """`route print -4 0.0.0.0` -> [(gateway IP, this PC's address on that network)]"""
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+\d+", line)
        if m and (m.group(1), m.group(2)) not in out:
            out.append((m.group(1), m.group(2)))
    return out


def _if_name(local_ip):
    try:
        import psutil  # noqa: PLC0415
        for name, addrs in psutil.net_if_addrs().items():
            if any(a.address == local_ip for a in addrs):
                return name
    except Exception:  # noqa: BLE001
        pass
    return ""


def _net_key(gw, local):
    name = _if_name(local)
    wifi = re.search(r"wi-?fi|wlan|wireless", name, re.I)
    net = ((_W.get("conn") or {}).get("ssid") or name) if wifi else (name or "wired")
    return f"{gw}|{net}", net, bool(wifi)


def gw_tick(now, routes=None, arp=None):
    """Check the router's hardware address on every network with a way to the internet."""
    if routes is None:
        r = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True, timeout=20, creationflags=_hs.NO_WINDOW if IS_WIN else 0)
        routes = parse_routes(r.stdout.decode("utf-8", "replace"))
    arp = _arp() if arp is None else arp
    try:
        known = json.loads(_meta("gw_known") or "{}")
    except ValueError:
        known = {}
    out = []
    for gw, local in routes:
        key, net, wifi = _net_key(gw, local)
        here = [d for d in arp if d["via"] == local]
        mac = next((d["mac"] for d in here if d["ip"] == gw), "")
        k = known.setdefault(key, {"macs": [], "first": now, "cur": "", "changes": []})
        twins = sorted({d["ip"] for d in here if mac and d["mac"] == mac and d["ip"] != gw})
        state, msg = "ok", ""
        if not mac:
            state = "unknown"
        elif not k["macs"]:
            k["macs"].append(mac)
            k["cur"] = mac
            _netlog(now, "router", f"Learned the router on {net}: {gw} is {mac}.")
        elif mac != k["cur"]:
            last = k["changes"][-1]["ts"] if k["changes"] else 0
            k["changes"] = (k["changes"] + [{"ts": now, "from": k["cur"], "to": mac}])[-20:]
            if mac not in k["macs"]:
                state = "changed"
                k["macs"] = (k["macs"] + [mac])[-10:]
                msg = (f"The router on {net} ({gw}) now answers from a different hardware address: was {k['cur'] or '?'}, now {mac}. "
                       "Normal only if the router was just replaced or reset. Otherwise another device on the network is claiming to be "
                       "the router so this PC's traffic goes through it (ARP spoofing).")
            elif now - last < FLIP_SECS:
                state = "flip"
                msg = (f"The router's hardware address on {net} ({gw}) is switching back and forth ({k['cur']} / {mac}). "
                       "This is what it looks like when another device keeps claiming to be the router (ARP spoofing).")
            k["cur"] = mac
        if twins:
            state = "spoof"
            msg = (f"On {net}, the router ({gw}) and {', '.join(twins)} have the same hardware address ({mac}). "
                   "Two addresses on one device usually means that device is pretending to be the router (ARP spoofing).")
        if msg:
            _netlog(now, "router", msg)
            if now - _N["spoof_alerted"].get(key, 0) > 3600:
                _N["spoof_alerted"][key] = now
                _alert(2, "net-router", msg, gw)
        mk, guess = maker(mac) if mac else ("", False)
        out.append({"gw": gw, "local": local, "net": net, "wifi": wifi, "mac": mac, "maker": mk if not guess else "", "state": state,
                    "known": k["macs"], "since": k["first"], "changes": k["changes"][-5:], "twins": twins})
    _set_meta("gw_known", json.dumps(known))
    _N.update(gw=out, gw_err=None, gw_last=now)
    return out


def _evt_xml(text):
    """wevtutil /f:xml output -> [{ts, ...Data fields}]"""
    out = []
    for ev in re.findall(r"<Event[ >].*?</Event>", text, re.S):
        m = re.search(r"SystemTime='([^']+)'|SystemTime=\"([^\"]+)\"", ev)
        if not m:
            continue
        st = (m.group(1) or m.group(2)).rstrip("Z")
        st = re.sub(r"(\.\d{6})\d*", r"\1", st)
        try:
            import datetime  # noqa: PLC0415
            ts = datetime.datetime.fromisoformat(st).replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
        d = {"ts": ts}
        for name, val in re.findall(r"<Data Name='([^']+)'>([^<]*)</Data>", ev) + re.findall(r"<Data Name=\"([^\"]+)\">([^<]*)</Data>", ev):
            d[name] = val
        out.append(d)
    return out


def drops(days=7):
    """Wi-Fi disconnects in the last N days (newest first), each marked explained / unexplained."""
    q = f"*[System[(EventID=8003) and TimeCreated[timediff(@SystemTime) <= {int(days * 86400000)}]]]"
    r = subprocess.run(["wevtutil", "qe", "Microsoft-Windows-WLAN-AutoConfig/Operational", f"/q:{q}", "/f:xml", "/c:500", "/rd:true"],
                       capture_output=True, timeout=30, creationflags=_hs.NO_WINDOW if IS_WIN else 0)
    out = []
    for d in _evt_xml(r.stdout.decode("utf-8", "replace")):
        if "direct" in (d.get("InterfaceDescription") or "").lower() or (d.get("ProfileName") or "").startswith("WFD_"):
            continue  # this PC's own hotspot / Wi-Fi Direct adapter
        reason = d.get("Reason") or ""
        out.append({"ts": d["ts"], "ssid": d.get("SSID", ""), "reason": reason, "code": d.get("ReasonCode", ""),
                    "explained": bool(DROP_OK.search(reason))})
    return out


def drops_tick(now):
    ds = drops(days=1)
    _N.update(drop_err=None, drop_last=now)
    seen_to = float(_meta("drop_seen") or 0)
    for d in reversed(ds):
        if d["ts"] > seen_to and not d["explained"]:
            _netlog(d["ts"], "drop", f"Wi-Fi \"{d['ssid']}\" dropped: {d['reason']}")
    if ds:
        _set_meta("drop_seen", max(d["ts"] for d in ds))
    bad = [d for d in ds if not d["explained"] and now - d["ts"] <= DROP_WINDOW]
    if len(bad) >= DROP_ALERT and now - _N["drop_alerted"] > 3600:
        _N["drop_alerted"] = now
        _alert(2, "net-drops", f"This PC's Wi-Fi was knocked off {len(bad)} times in the last {DROP_WINDOW // 60} minutes "
                               f"(\"{bad[0]['ssid']}\": {bad[0]['reason']}). Repeated unexplained drops are what a deauthentication attack looks like: "
                               "someone forcing devices off the network, often so they reconnect to a fake copy of it. Check the WIFI tab for a "
                               "copy of your network. A failing router or interference can also cause this.")


class _SSID(ctypes.Structure):  # DOT11_SSID
    _fields_ = [("len", ctypes.c_ulong), ("ssid", ctypes.c_ubyte * 32)]


def karma_test(q=None, rounds=KARMA_ROUNDS):
    """Ask for a random network name that cannot exist. Anything that answers is a fake access point (karma / Pineapple)."""
    import secrets  # noqa: PLC0415
    if _N["karma_busy"]:
        return {"busy": True, **(_N["karma"] or {})}
    _N["karma_busy"] = True
    now = time.time()
    name = "dw-" + secrets.token_hex(6)
    res = {"ts": now, "ssid": name, "rounds": 0, "hits": [], "state": "ok", "err": None}
    try:
        if not IS_WIN:
            raise OSError("Windows only")
        s = _SSID(len(name), (ctypes.c_ubyte * 32)(*name.encode()))
        for _ in range(rounds):
            with _NL:
                lib, h = _wlan()
                guids = _ifaces(lib, h)
                if not guids:
                    res["state"] = "none"
                    break
                for g in guids:
                    lib.WlanScan(h, ctypes.byref(g), ctypes.byref(s), None, None)   # directed probe for the made-up name
            time.sleep(4)
            with _NL:
                found = _read_bss(lib, h, guids)
            res["rounds"] += 1
            for bssid, a in found.items():
                if a["ssid"] == name and bssid not in [x["bssid"] for x in res["hits"]]:
                    mk, guess = maker(bssid)
                    res["hits"].append({"bssid": bssid, "rssi": a["rssi"], "dist": distance(a["rssi"], a["freq"], a["tx"]), "maker": mk if not guess else ""})
    except PermissionError:
        res["state"] = "location"
    except Exception as e:  # noqa: BLE001
        res.update(state="error", err=f"{type(e).__name__}: {e}")
    finally:
        _N["karma_busy"] = False
    if res["hits"]:
        h = res["hits"][0]
        msg = (f"A Wi-Fi device answered to a made-up network name (\"{name}\") that does not exist: {h['bssid']}"
               f"{', roughly ' + format(h['dist'], 'g') + ' m away' if h.get('dist') else ''}. Only fake access points (\"Pineapple\" / karma "
               "devices) do this: they pretend to be any network your devices have joined before, to get them to connect through it. "
               "Turn off auto-join for open networks on your phone and laptop.")
        _netlog(now, "fakeap", msg)
        _alert(2, "net-fakeap", msg, h["bssid"])
    elif res["state"] == "ok":
        _netlog(now, "fakeap-ok", f"Fake access point test: nothing answered to \"{name}\" ({res['rounds']} tries).")
    _N["karma"] = res
    try:
        hist = json.loads(_meta("karma_hist") or "[]")
    except ValueError:
        hist = []
    _set_meta("karma_hist", json.dumps(([{k: res[k] for k in ("ts", "state", "hits", "rounds")}] + hist)[:30]))
    return res


def _net_loop():
    last_drops = 0.0
    while True:
        now = time.time()
        try:
            gw_tick(now)
        except Exception as e:  # noqa: BLE001
            _N["gw_err"] = f"{type(e).__name__}: {e}"
        if now - last_drops >= NET_SECS:
            last_drops = now
            try:
                drops_tick(now)
            except Exception as e:  # noqa: BLE001
                _N["drop_err"] = f"{type(e).__name__}: {e}"
        if now >= _N["karma_next"] and _W.get("state") in ("ok", "location"):
            _N["karma_next"] = now + KARMA_EVERY
            try:
                if (_W.get("conn") or {}).get("ssid") or _W.get("state") == "ok":
                    karma_test()
            except Exception as e:  # noqa: BLE001
                print(f"guard: fake access point test: {e}")
        time.sleep(GW_SECS)


def net_status(q=None):
    _init_db()
    now = time.time()
    try:
        ds = drops(days=7)
    except Exception as e:  # noqa: BLE001
        ds, _N["drop_err"] = [], str(e)
    with _db() as c:
        log = [dict(r) for r in c.execute("SELECT ts, kind, msg FROM net_log WHERE kind<>'fakeap-ok' ORDER BY ts DESC LIMIT 40")]
    try:
        hist = json.loads(_meta("karma_hist") or "[]")
    except ValueError:
        hist = []
    return {"gw": _N["gw"], "gw_err": _N["gw_err"], "gw_last": _N["gw_last"],
            "drops": ds[:60], "drops_bad": sum(1 for d in ds if not d["explained"]), "drops_total": len(ds), "drop_err": _N["drop_err"],
            "drop_alert": DROP_ALERT, "drop_window": DROP_WINDOW // 60,
            "karma": _N["karma"], "karma_busy": _N["karma_busy"], "karma_hist": hist[:10], "karma_every": KARMA_EVERY // 60,
            "karma_next": _N["karma_next"], "log": log, "now": now}


# ----------------------------------------------------------------------------------------------- Rayhunter (fake phone towers)
# EFF's Rayhunter runs on a cheap mobile hotspot and reads its phone chip's own logs for the tricks fake phone towers
# (IMSI catchers / "Stingrays") use. It has a small web page with no password; this reads its warnings once a minute
# and turns them into Device Watch alerts. Only GET requests, only to the private address the user typed in.
RH_SECS = 60
RH_PRIVATE = re.compile(r"^http://(192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|127\.0\.0\.1)(:\d{1,5})?$")
RH_LEVEL = {"High": 2, "Medium": 2, "Low": 1}
_R = {"state": "off", "err": None, "last": None, "ok_at": None, "recording": None, "entry": None, "stats": None, "entries": 0}
_RH_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never through a proxy: it is on the local network


def _rh_url():
    return _meta("rayhunter_url") or ""


def _rh_get(url, path, timeout=8):
    with _RH_OPEN.open(urllib.request.Request(url + path, headers={"User-Agent": "DeviceWatch/1.0"}), timeout=timeout) as r:
        return r.read(8_000_000).decode("utf-8", "replace")


def parse_report(text):
    """Rayhunter analysis report (one JSON object per line) -> [{ts, sev, msg}] for every warning (not 'Informational')."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or "analyzers" in row:
            continue
        ts = row.get("packet_timestamp")
        for ev in row.get("events") or []:
            if isinstance(ev, dict) and ev.get("event_type") and ev.get("event_type") != "Informational":
                out.append({"ts": ts, "sev": str(ev["event_type"]), "msg": str(ev.get("message") or "")[:400]})
    return out


def _iso_ts(s):
    import datetime  # noqa: PLC0415
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def rh_tick(now):
    url = _rh_url()
    if not url:
        _R.update(state="off", err=None)
        return
    _R["last"] = now
    try:
        man = json.loads(_rh_get(url, "/api/qmdl-manifest"))
    except Exception as e:  # noqa: BLE001
        _R.update(state="unreachable", err=f"{type(e).__name__}: {e}"[:200])
        return
    cur = man.get("current_entry")
    entries = [e for e in (man.get("entries") or []) if isinstance(e, dict) and e.get("name")]
    _R.update(state="ok", err=None, ok_at=now, recording=bool(cur), entry=cur, entries=len(entries) + (1 if cur else 0))
    try:
        _R["stats"] = json.loads(_rh_get(url, "/api/system-stats"))
    except Exception:  # noqa: BLE001
        pass
    try:
        done = set(json.loads(_meta("rh_done") or "[]"))
    except ValueError:
        done = set()
    todo = []
    if cur and cur.get("name"):
        todo.append((cur["name"], "live"))
    for e in sorted(entries, key=lambda e: str(e["name"]), reverse=True)[:10]:
        if e["name"] not in done:
            todo.append((e["name"], e["name"]))
    new = []
    with _db() as c:
        for name, path in todo:
            try:
                warns = parse_report(_rh_get(url, f"/api/analysis-report/{urllib.request.quote(path)}", timeout=20))
            except Exception:  # noqa: BLE001
                continue
            for w in warns:
                k = f"{name}|{w['ts']}|{w['msg']}"
                ts = _iso_ts(w["ts"]) or now
                if c.execute("INSERT OR IGNORE INTO rh_warn VALUES(?,?,?,?,?,?)", (k, name, ts, w["sev"], w["msg"], now)).rowcount:
                    new.append({**w, "t": ts})
            if path != "live":
                done.add(name)
    _set_meta("rh_done", json.dumps(sorted(done)[-300:]))
    if new:
        worst = max(new, key=lambda w: RH_LEVEL.get(w["sev"], 1))
        lvl = RH_LEVEL.get(worst["sev"], 1)
        _alert(lvl, "cell", f"Rayhunter reported {len(new)} new warning{'s' if len(new) > 1 else ''} (worst: {worst['sev']}): {worst['msg']}. "
                            "A phone tower near the hotspot did something fake towers (IMSI catchers / Stingrays) do. Rayhunter's own advice: "
                            "note the time and place; a single Low warning is often a real network misbehaving.")


def _rh_loop():
    while True:
        try:
            rh_tick(time.time())
        except Exception as e:  # noqa: BLE001
            _R.update(state="error", err=f"{type(e).__name__}: {e}")
        time.sleep(RH_SECS)


def rayhunter(q=None):
    _init_db()
    with _db() as c:
        warns = [dict(r) for r in c.execute("SELECT entry, ts, sev, msg, found FROM rh_warn ORDER BY ts DESC LIMIT 100")]
        counts = {r[0]: r[1] for r in c.execute("SELECT sev, COUNT(*) FROM rh_warn GROUP BY sev")}
    st = _R.get("stats") or {}
    return {"url": _rh_url(), "state": _R["state"], "err": _R["err"], "last": _R["last"], "ok_at": _R["ok_at"], "recording": _R["recording"],
            "entry": _R["entry"], "entries": _R["entries"], "battery": st.get("battery_status"), "disk": st.get("disk_stats"),
            "warnings": warns, "counts": counts, "every": RH_SECS, "setup": orbic_status()}


def rayhunter_set(q):
    url = (q.get("url") or "").strip().rstrip("/")
    if url and not url.startswith("http"):
        url = "http://" + url
    if url and not RH_PRIVATE.match(url):
        raise ValueError("Use the hotspot's local address, like http://192.168.1.1:8080 (a private address on your own network).")
    _set_meta("rayhunter_url", url)
    _R.update(state="off" if not url else "starting", err=None, recording=None, entry=None, stats=None)
    if url:
        rh_tick(time.time())
    return rayhunter()


# ----------------------------------------------------------------------------------------------- tripwires (INTRUDER tab)
# All in this program, as the signed-in user:
#  * TYPING: Windows' raw keyboard feed (the same one games use; it never slows typing down) gives the time of every key
#    press and which keyboard it came from. 20 presses from one hardware keyboard in about 0.3 s is faster than any
#    person can type: that is a "BadUSB" stick pretending to be a keyboard. Only the timing is looked at; which keys
#    were pressed is never stored or shown. Software typing (password managers, on-screen keyboard) is not counted.
#  * CLIPBOARD: clipboard-hijacking malware swaps a copied crypto address or bank account number for the attacker's own
#    a moment after you copy it. Windows says each time the clipboard changes; a copied address replaced by a different
#    one of the same kind within 2 seconds is an alert. Only those patterns are looked at; nothing is stored.
#  * DECOY NAMES: the decoy FileZilla file lists two made-up servers. Nobody but someone who read that file can know the
#    names, so any look-up of them in Windows' look-up log means the decoy's log-ins are being tried.
#  * PHOTO: when the PC is used while you are away, or someone types a wrong password at it, Edge (hidden, with its own
#    profile) opens a local page that takes two webcam photos and hands them back here. Stored only on this PC.
#  * RECORD: from that moment, for as long as someone is using the PC (and 30 minutes after), a log of every window
#    they look at, program they start and file they open.
TYPE_N = 20               # key presses in a burst
TYPE_SPAN_MS = 300        # ... within this long (about 65 per second; fast people manage 10-15)
TYPE_EVERY = 600
CLIP_SWAP_SECS = 2.0
CLIP_KINDS = (("Bitcoin address", re.compile(r"(bc1[ac-hj-np-z02-9]{25,87}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})")),
              ("Ethereum / token address", re.compile(r"0x[0-9a-fA-F]{40}")),
              ("Litecoin address", re.compile(r"(ltc1[ac-hj-np-z02-9]{25,87}|[LM][a-km-zA-HJ-NP-Z1-9]{26,33})")),
              ("Tron address", re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")),
              ("Monero address", re.compile(r"[48][0-9AB][1-9A-HJ-NP-Za-km-z]{93}")),
              ("bank account number (IBAN)", re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")),
              ("Solana address", re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")))
NAMES_SECS = 60
SNAP_DIR = _hs.HS_DIR / "intruder"
SNAP_KEEP = 300
SNAP_GAP = 120            # at most one photo session per 2 minutes
AWAY_IDLE_END = 30 * 60
AWAY_MAX = 4 * 3600
APP_PORT = 8765
SNAP_EXTRA = []           # extra browser switches (tests use Edge's built-in fake camera)
_T = {"raw": False, "raw_err": None, "clip": False, "clip_err": None, "kbd_new": 0.0, "typing_alerted": 0.0, "names_err": None,
      "names_last": None, "clip_prev": None, "devs": {}}
_KEYS = collections.deque(maxlen=200)   # (ms, device handle, key code) of hardware key presses, memory only, for the burst check


def _trip(now, kind, msg):
    with _db() as c:
        c.execute("INSERT INTO trip VALUES(?,?,?)", (now, kind, msg))
        c.execute("DELETE FROM trip WHERE ts < ?", (now - KEEP_DAYS * 86400,))


def classify_clip(text):
    t = (text or "").strip()
    if not t or len(t) > 120 or any(ch.isspace() for ch in t):
        return None
    for name, rx in CLIP_KINDS:
        if rx.fullmatch(t):
            return name
    return None


def clip_check(now, text, owner):
    """One clipboard change. -> alert message if it looks like a swapped address, else None."""
    kind = classify_clip(text)
    prev = _T["clip_prev"]
    _T["clip_prev"] = (now, kind, text, owner) if kind else None
    if not kind or not prev or prev[1] != kind or prev[2] == text or now - prev[0] > CLIP_SWAP_SECS:
        return None
    mask = lambda s: f"{s[:6]}...{s[-4:]}"  # noqa: E731
    who = f" The new one was put there by {owner}." if owner else ""
    was = f" (the first copy came from {prev[3]})" if prev[3] and prev[3] != owner else ""
    msg = (f"{'An' if kind[0] in 'AEIOU' else 'A'} {kind} you copied ({mask(prev[2])}){was} was replaced {now - prev[0]:.1f} s later by a different one ({mask(text)}).{who} "
           "This is what clipboard-hijacking malware does so payments go to the attacker. Check the address before you paste and send anything, "
           "and run a full Microsoft Defender scan. (If you simply copied two addresses one after the other, ignore this.)")
    _trip(now, "clipboard", msg)
    _alert(2, "trip-clipboard", msg)
    return msg


def typing_check(now_ms=None, now=None):
    """Look for a burst of hardware key presses no person could type. -> alert message or None."""
    keys = list(_KEYS)
    if len(keys) < TYPE_N:
        return None
    now = now or time.time()
    by_dev = collections.defaultdict(list)
    for t, dev, vk in keys:
        by_dev[dev].append((t, vk))
    for dev, ks in by_dev.items():
        for i in range(len(ks) - TYPE_N + 1):
            win = ks[i:i + TYPE_N]
            span = (win[-1][0] - win[0][0]) & 0xFFFFFFFF
            if span <= TYPE_SPAN_MS and len({vk for _t, vk in win}) >= 6:
                _KEYS.clear()
                if now - _T["typing_alerted"] < TYPE_EVERY:
                    return None
                _T["typing_alerted"] = now
                name = _T["devs"].get(dev) or "an unnamed keyboard device"
                fresh = " A new keyboard-type device was plugged in just before this." if now - _T["kbd_new"] < 900 else ""
                msg = (f"{TYPE_N} key presses arrived in {span / 1000:.2f} s ({TYPE_N * 1000 // max(span, 1)} per second) from {name}. "
                       f"Nobody types that fast: a USB stick pretending to be a keyboard (\"BadUSB\" / \"Rubber Ducky\") types commands this way.{fresh} "
                       "Unplug any USB device you do not recognise. A barcode scanner also types this fast.")
                _trip(now, "typing", msg)
                _alert(2, "trip-typing", msg, name)
                intruder_photo("superhuman typing", now)
                away_record_start(now, "superhuman typing")
                return msg
    return None


def note_keyboard(now):
    """Called when the helper reports a new keyboard-type device."""
    _T["kbd_new"] = now


def _dev_name(user32, h):
    from ctypes import wintypes as wt  # noqa: PLC0415
    n = wt.UINT(0)
    user32.GetRawInputDeviceInfoW(h, 0x20000007, None, ctypes.byref(n))   # RIDI_DEVICENAME
    if not n.value:
        return ""
    buf = ctypes.create_unicode_buffer(n.value + 1)
    user32.GetRawInputDeviceInfoW(h, 0x20000007, buf, ctypes.byref(n))
    m = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", buf.value, re.I)
    bus = "Bluetooth" if "BTH" in buf.value.upper() or "{00001124" in buf.value else "USB" if "HID#" in buf.value.upper() else "built-in"
    return f"{bus} keyboard device (vendor {m.group(1).upper()}, product {m.group(2).upper()})" if m else f"{bus} keyboard device"


def _read_clip(user32, k32, hwnd):
    """Clipboard text if it is short (an address), else None. Retries if another program has it open."""
    if not user32.IsClipboardFormatAvailable(13):
        return None
    for _ in range(5):
        if user32.OpenClipboard(hwnd):
            break
        time.sleep(0.03)
    else:
        return None
    try:
        h = user32.GetClipboardData(13)   # CF_UNICODETEXT
        if not h or k32.GlobalSize(h) > 512:
            return None
        p = k32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _clip_owner(user32):
    try:
        from ctypes import wintypes as wt  # noqa: PLC0415
        import psutil  # noqa: PLC0415
        hw = user32.GetClipboardOwner()
        if not hw:
            return ""
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hw, ctypes.byref(pid))
        return psutil.Process(pid.value).name() if pid.value else ""
    except Exception:  # noqa: BLE001
        return ""


def _raw_loop():
    """Hidden message window: raw keyboard timing + clipboard changes. Messages are queued to this thread, so nothing
    here can slow down typing or copying anywhere else."""
    from ctypes import wintypes as wt  # noqa: PLC0415
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("proc", WNDPROC), ("cls", ctypes.c_int), ("wnd", ctypes.c_int), ("inst", wt.HINSTANCE),
                    ("icon", wt.HICON), ("cursor", wt.HANDLE), ("bg", wt.HBRUSH), ("menu", wt.LPCWSTR), ("name", wt.LPCWSTR)]

    class RID(ctypes.Structure):   # RAWINPUTDEVICE
        _fields_ = [("page", wt.USHORT), ("usage", wt.USHORT), ("flags", wt.DWORD), ("hwnd", wt.HWND)]

    class HDR(ctypes.Structure):   # RAWINPUTHEADER
        _fields_ = [("type", wt.DWORD), ("size", wt.DWORD), ("dev", wt.HANDLE), ("wp", wt.WPARAM)]

    class KBD(ctypes.Structure):   # RAWKEYBOARD
        _fields_ = [("make", wt.USHORT), ("flags", wt.USHORT), ("res", wt.USHORT), ("vk", wt.USHORT), ("msg", wt.UINT), ("extra", wt.ULONG)]

    class RAW(ctypes.Structure):
        _fields_ = [("hdr", HDR), ("kbd", KBD)]

    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.CreateWindowExW.restype = wt.HWND
    user32.GetRawInputData.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID, ctypes.POINTER(wt.UINT), wt.UINT]
    user32.GetRawInputDeviceInfoW.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID, ctypes.POINTER(wt.UINT)]
    user32.GetClipboardData.restype = wt.HANDLE
    user32.GetClipboardOwner.restype = wt.HWND
    user32.OpenClipboard.argtypes = [wt.HWND]
    k32.GlobalLock.argtypes = [wt.HGLOBAL]
    k32.GlobalLock.restype = wt.LPVOID
    k32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    k32.GlobalSize.argtypes = [wt.HGLOBAL]
    k32.GlobalSize.restype = ctypes.c_size_t
    k32.GetModuleHandleW.restype = wt.HMODULE
    hwnd_box = [None]

    def proc(hwnd, msg, wp, lp):
        try:
            if msg == 0x00FF:   # WM_INPUT
                raw = RAW()
                n = wt.UINT(ctypes.sizeof(raw))
                if user32.GetRawInputData(lp, 0x10000003, ctypes.byref(raw), ctypes.byref(n), ctypes.sizeof(HDR)) != 0xFFFFFFFF \
                        and raw.hdr.type == 1 and raw.hdr.dev and not (raw.kbd.flags & 1):   # keyboard, hardware, key down
                    dev = int(raw.hdr.dev)
                    if dev not in _T["devs"]:
                        _T["devs"][dev] = _dev_name(user32, raw.hdr.dev)
                    _KEYS.append((user32.GetMessageTime() & 0xFFFFFFFF, dev, raw.kbd.vk))
            elif msg == 0x031D:  # WM_CLIPBOARDUPDATE
                text = _read_clip(user32, k32, hwnd)
                clip_check(time.time(), text, _clip_owner(user32) if classify_clip(text) else "")
        except Exception as e:  # noqa: BLE001
            print(f"guard: raw input: {e}")
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    cb = WNDPROC(proc)
    _T["cb"] = cb   # keep the callback alive
    inst = k32.GetModuleHandleW(None)
    wc = WNDCLASSW(0, cb, 0, 0, inst, None, None, None, None, "DeviceWatchTripwires")
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "DeviceWatchTripwires", "DeviceWatch", 0, 0, 0, 0, 0, wt.HWND(-3), None, inst, None)  # message-only
    if not hwnd:
        _T["raw_err"] = f"could not create the listening window (error {ctypes.get_last_error()})"
        return
    hwnd_box[0] = hwnd
    rid = RID(0x01, 0x06, 0x00000100, hwnd)   # generic desktop / keyboard, RIDEV_INPUTSINK (also when not in front)
    if user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RID)):
        _T["raw"] = True
    else:
        _T["raw_err"] = f"keyboard timing unavailable (error {ctypes.get_last_error()})"
    if user32.AddClipboardFormatListener(hwnd):
        _T["clip"] = True
    else:
        _T["clip_err"] = f"clipboard watch unavailable (error {ctypes.get_last_error()})"
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def _typing_loop():
    while True:
        try:
            typing_check()
        except Exception as e:  # noqa: BLE001
            print(f"guard: typing check: {e}")
        time.sleep(1)


# --- decoy server names
def _honey_hosts():
    try:
        import honey  # noqa: PLC0415
        return tuple(honey.HONEY_HOSTS)
    except Exception:  # noqa: BLE001
        return ("ftp.todsite.net", "files.homebackup.org")


def names_tick(now):
    log = "Microsoft-Windows-DNS-Client/Operational"
    run = lambda *a: subprocess.run(["wevtutil", "qe", log, *a], capture_output=True, timeout=40,  # noqa: E731
                                    creationflags=_hs.NO_WINDOW if IS_WIN else 0).stdout.decode("utf-8", "replace")
    top = re.findall(r"<EventRecordID>(\d+)</EventRecordID>", run("/c:1", "/rd:true", "/f:xml"))
    if not top:
        _T.update(names_err="Windows' look-up log is empty or switched off (TRAPS > REPAIR DECOYS switches it on)", names_last=now)
        return
    top = int(top[0])
    last = int(_meta("names_rid") or 0)
    if not last:   # first run: start from now
        _set_meta("names_rid", top)
        _T.update(names_err=None, names_last=now)
        return
    hosts = _honey_hosts()
    cond = " or ".join(f"Data[@Name='QueryName']='{h}'" for h in hosts)
    hits = run(f"/q:*[System[(EventRecordID>{last}) and (EventRecordID<={top})]] and *[EventData[{cond}]]", "/f:xml", "/c:50")
    found = sorted({m.lower().rstrip(".") for m in re.findall(r"<Data Name=['\"]QueryName['\"]>([^<]+)</Data>", hits)})
    _set_meta("names_rid", top)
    _T.update(names_err=None, names_last=now)
    if found:
        msg = (f"Something on this PC looked up {', '.join(found)}: a made-up server name that exists only inside the decoy FileZilla file "
               "(saved FTP log-ins). Only someone, or some malware, that read that decoy could know it, and it is now trying the log-ins. "
               "Check the TRAPS tab for who opened the file.")
        _trip(now, "name", msg)
        _alert(2, "trip-name", msg, found[0])


def _names_loop():
    while True:
        try:
            names_tick(time.time())
        except Exception as e:  # noqa: BLE001
            _T["names_err"] = f"{type(e).__name__}: {e}"
        time.sleep(NAMES_SECS)


# --- intruder photo
_S = {"token": None, "until": 0.0, "proc": None, "last": 0.0, "reason": "", "got": 0, "err": None, "cams": None, "cams_t": 0.0}


def _edge():
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if root:
            for p in (Path(root) / "Microsoft/Edge/Application/msedge.exe", Path(root) / "Google/Chrome/Application/chrome.exe"):
                if p.is_file():
                    return str(p)
    return None


def cameras():
    """Webcams Windows knows about (cached 5 minutes)."""
    now = time.time()
    if _S["cams"] is None or now - _S["cams_t"] > 300:
        try:
            out, _e = _hs._ps("Get-PnpDevice -PresentOnly -Class Camera,Image -ErrorAction SilentlyContinue | ?{$_.Status -eq 'OK'} | %{$_.FriendlyName}", 30)
            _S["cams"] = [x.strip() for x in out.splitlines() if x.strip()]
        except Exception:  # noqa: BLE001
            _S["cams"] = []
        _S["cams_t"] = now
    return _S["cams"]


def _snap_stop():
    p = _S.get("proc")
    if p and p.poll() is None:
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=15, creationflags=_hs.NO_WINDOW)
        except Exception:  # noqa: BLE001
            pass
    _S.update(token=None, proc=None)


def intruder_photo(reason, now=None, force=False):
    """Take two webcam photos (hidden Edge window -> local page -> back to snap_upload). False if skipped."""
    import secrets  # noqa: PLC0415
    now = now or time.time()
    if not IS_WIN or (not force and now - _S["last"] < SNAP_GAP) or (_S["proc"] and _S["proc"].poll() is None):
        return False
    if not cameras():
        _S.update(err="no webcam connected", last=now)
        return False
    exe = _edge()
    if not exe:
        _S["err"] = "Edge not found"
        return False
    token = secrets.token_urlsafe(24)
    prof = _hs.HS_DIR / "snap-profile"
    _S.update(token=token, until=now + 45, last=now, reason=reason, got=0, err=None)
    args = [exe, "--headless=new", f"--user-data-dir={prof}", "--use-fake-ui-for-media-stream", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-breakpad", "--no-pings",
            "--metrics-recording-only", "--window-size=1280,720", *SNAP_EXTRA, f"http://127.0.0.1:{APP_PORT}/device/snap.html#{token}"]
    _S["proc"] = subprocess.Popen(args, creationflags=_hs.NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Timer(50, lambda: _S["token"] == token and _snap_stop()).start()
    return True


def snap_upload(q, body):
    """POST from the snap page: a JPEG (or ?err= why the camera did not work)."""
    now = time.time()
    if not _S["token"] or q.get("t") != _S["token"] or now > _S["until"]:
        raise ValueError("no photo was asked for")
    if q.get("err"):
        _S["err"] = str(q["err"])[:200]
        _snap_stop()
        return {"ok": False}
    if not body.startswith(b"\xff\xd8") or len(body) > 5_000_000:
        raise ValueError("not a photo")
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    _S["got"] += 1
    name = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{_S['got']}.jpg"
    (SNAP_DIR / name).write_bytes(body)
    _init_db()
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO snaps VALUES(?,?,?)", (name, now, _S["reason"]))
        old = [r[0] for r in c.execute("SELECT file FROM snaps ORDER BY ts DESC LIMIT -1 OFFSET ?", (SNAP_KEEP,))]
        for f in old:
            c.execute("DELETE FROM snaps WHERE file=?", (f,))
    for f in old:
        try:
            (SNAP_DIR / f).unlink(missing_ok=True)
        except OSError:
            pass
    if _S["got"] >= 2:
        _snap_stop()
    return {"ok": True}


# --- what someone did while you were away
_A = {"sid": None, "until": 0.0, "start": 0.0, "fg": None, "pids": set(), "recent": set()}


def _recent_dir():
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Microsoft" / "Windows" / "Recent"


def _alog(now, kind, what):
    with _db() as c:
        c.execute("INSERT INTO away_log VALUES(?,?,?,?)", (_A["sid"], now, kind, what[:300]))


def away_record_start(now, reason):
    import psutil  # noqa: PLC0415
    _init_db()
    if _A["sid"]:
        _A["until"] = max(_A["until"], now + AWAY_IDLE_END)
        return
    _A.update(sid=now, start=now, until=now + AWAY_IDLE_END, fg=None, pids=set(psutil.pids()))
    try:
        _A["recent"] = {p.name for p in _recent_dir().glob("*.lnk")}
    except OSError:
        _A["recent"] = set()
    _alog(now, "start", reason)
    with _db() as c:
        c.execute("DELETE FROM away_log WHERE ts < ?", (now - KEEP_DAYS * 86400,))


def _fg_window():
    from ctypes import wintypes as wt  # noqa: PLC0415
    import psutil  # noqa: PLC0415
    user32 = ctypes.windll.user32
    hw = user32.GetForegroundWindow()
    if not hw:
        return None
    n = user32.GetWindowTextLengthW(hw)
    buf = ctypes.create_unicode_buffer(min(n, 300) + 1)
    user32.GetWindowTextW(hw, buf, len(buf))
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hw, ctypes.byref(pid))
    try:
        exe = psutil.Process(pid.value).name()
    except Exception:  # noqa: BLE001
        exe = "?"
    return f"{exe}: {buf.value}" if buf.value else exe


def away_tick(now):
    import getpass  # noqa: PLC0415
    import psutil  # noqa: PLC0415
    if not _A["sid"]:
        return
    if _idle_secs() < 60:
        _A["until"] = max(_A["until"], now + AWAY_IDLE_END)
    if now > _A["until"] or now - _A["start"] > AWAY_MAX:
        _alog(now, "end", "Recording stopped (nobody touched the PC for 30 minutes)" if now > _A["until"] else "Recording stopped after 4 hours")
        _A["sid"] = None
        return
    if _locked():
        fg = "(lock screen)"
    else:
        fg = _fg_window()
    if fg and fg != _A["fg"]:
        _A["fg"] = fg
        _alog(now, "window", fg)
    me = getpass.getuser().lower()
    pids = set(psutil.pids())
    for pid in pids - _A["pids"]:
        try:
            p = psutil.Process(pid)
            user = (p.username() or "").split("\\")[-1].lower()
            if user == me:
                _alog(now, "program", f"{p.name()}  ({p.exe()})")
        except Exception:  # noqa: BLE001
            pass
    _A["pids"] = pids
    try:
        cur = {p.name for p in _recent_dir().glob("*.lnk")}
        for name in sorted(cur - _A["recent"]):
            _alog(now, "file", name[:-4])
        _A["recent"] = cur
    except OSError:
        pass


def _away_loop():
    while True:
        try:
            away_tick(time.time())
        except Exception as e:  # noqa: BLE001
            print(f"guard: away record: {e}")
        time.sleep(3)


def intruder(q=None):
    """Everything for the INTRUDER tab."""
    import base64  # noqa: PLC0415
    _init_db()
    now = time.time()
    with _db() as c:
        snaps = [dict(r) for r in c.execute("SELECT file, ts, reason FROM snaps ORDER BY ts DESC LIMIT 12")]
        sids = [r[0] for r in c.execute("SELECT DISTINCT sid FROM away_log WHERE sid IS NOT NULL ORDER BY sid DESC LIMIT 10")]
        sessions = []
        for sid in sids:
            rows = [dict(r) for r in c.execute("SELECT ts, kind, what FROM away_log WHERE sid=? ORDER BY ts LIMIT 400", (sid,))]
            sessions.append({"sid": sid, "rows": rows, "live": sid == _A["sid"]})
        trips = [dict(r) for r in c.execute("SELECT ts, kind, msg FROM trip ORDER BY ts DESC LIMIT 30")]
    for s in snaps:
        try:
            s["data"] = "data:image/jpeg;base64," + base64.b64encode((SNAP_DIR / s["file"]).read_bytes()).decode()
        except OSError:
            s["data"] = ""
    try:
        import device  # noqa: PLC0415
        with device._db() as dc:
            cam = [dict(r) for r in dc.execute("SELECT ts, level, msg FROM events WHERE kind='honey' AND (msg LIKE 'Camera%' OR msg LIKE '%keyboard-type%') "
                                                "ORDER BY ts DESC LIMIT 20")]
    except Exception:  # noqa: BLE001
        cam = []
    try:
        import honey  # noqa: PLC0415
        hs = honey.status({})
        ports, bait = hs.get("ports") or {}, hs.get("bait_user")
        probes = [p for p in hs.get("probes") or [] if not p.get("routine")]
        logons = hs.get("logons") or []
    except Exception:  # noqa: BLE001
        ports, bait, probes, logons = {}, None, [], []
    return {"now": now, "raw": _T["raw"], "raw_err": _T["raw_err"], "clip": _T["clip"], "clip_err": _T["clip_err"],
            "names": list(_honey_hosts()), "names_err": _T["names_err"], "names_last": _T["names_last"],
            "cams": cameras(), "snap_err": _S["err"], "snap_busy": bool(_S["proc"] and _S["proc"].poll() is None), "snaps": snaps,
            "recording": bool(_A["sid"]), "sessions": sessions, "trips": trips, "cam_events": cam,
            "ports": ports, "bait": bait, "probes": probes[:10], "logons": logons[:10], "type_n": TYPE_N, "type_ms": TYPE_SPAN_MS}


def snap_test(q=None):
    if not cameras():
        return {"ok": False, "err": "No webcam is connected, so there is nothing to take a photo with."}
    return {"ok": intruder_photo("test photo", force=True)}


# ----------------------------------------------------------------------------------------------- Rayhunter set-up (CELL tab)
# PREPARE (once, any time): download EFF's official Windows release from GitHub and check it against the SHA-256 that
# GitHub publishes for it, then unpack installer.exe. Then, every 10 seconds, look for an Orbic RC400L plugged in by USB
# (Qualcomm USB ids from Rayhunter's own installer) or answering as the router of the network this PC is on. When one is
# found: if Rayhunter already runs on it, connect the CELL tab; if not and automatic install is on (with the hotspot's
# admin password saved), run EFF's network installer once (`installer orbic --admin-password ...`, never the USB
# installer, which EFF warns can brick the device on Windows). Never aimed at the home router: the target must not be a
# router this PC already knows.
RH_API = "https://api.github.com/repos/EFForg/rayhunter/releases/latest"
RH_DIR = _hs.HS_DIR / "rayhunter"
ORBIC_USB = re.compile(r"USB\\VID_05C6&PID_F6(01|22|26)", re.I)
ORBIC_IP = "192.168.1.1"
INSTALL_RETRY = 30 * 60
_O = {"state": "idle", "usb": False, "web": False, "rh_up": False, "busy": False, "log": [], "last_try": 0.0, "seen": 0.0, "notified": 0.0}


def _olog(msg):
    _O["log"] = (_O["log"] + [{"ts": time.time(), "msg": msg}])[-60:]


def _dpapi(data, protect=True):
    """Windows DPAPI: encrypt / decrypt for this Windows user only (keeps the hotspot password out of plain text)."""
    from ctypes import wintypes as wt  # noqa: PLC0415

    class BLOB(ctypes.Structure):
        _fields_ = [("n", wt.DWORD), ("p", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise OSError("Windows could not " + ("encrypt" if protect else "decrypt") + " the password")
    try:
        return ctypes.string_at(dst.p, dst.n)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.p)


def _orbic_pw():
    import base64  # noqa: PLC0415
    v = _meta("orbic_pw") or ""
    if not v:
        return ""
    try:
        return _dpapi(base64.b64decode(v), protect=False).decode()
    except Exception:  # noqa: BLE001
        return ""


def _installer():
    p = _meta("rh_installer") or ""
    from pathlib import Path  # noqa: PLC0415
    return p if p and Path(p).is_file() else ""


def _usb_present():
    cm = ctypes.WinDLL("cfgmgr32")
    n, flags = ctypes.c_ulong(), 0x1 | 0x100   # only USB devices that are plugged in now
    if cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(n), "USB", flags) or not n.value:
        return []
    buf = ctypes.create_unicode_buffer(n.value)
    if cm.CM_Get_Device_ID_ListW("USB", buf, n, flags):
        return []
    return [s for s in buf[:n.value].split("\0") if s]


def _http(url, timeout=4, limit=200_000):
    with _RH_OPEN.open(urllib.request.Request(url, headers={"User-Agent": "DeviceWatch/1.0"}), timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "replace")


def _not_home(mac):
    """True if this router address is the Orbic (remembered once identified) or not a router this PC already knew
    (watched for over an hour before)."""
    if mac and mac == _meta("orbic_mac"):
        return True
    try:
        known = json.loads(_meta("gw_known") or "{}")
    except ValueError:
        known = {}
    home = {m for k in known.values() for m in k.get("macs", []) if k.get("first", 0) < time.time() - 3600}
    return bool(mac) and mac not in home


def rh_prepare(q=None):
    """Download + verify + unpack EFF's official Windows release."""
    import hashlib  # noqa: PLC0415
    import zipfile  # noqa: PLC0415
    if _O["busy"]:
        raise ValueError("busy, try again in a minute")
    _O["busy"] = True
    try:
        rel = json.loads(_http(RH_API, timeout=20, limit=2_000_000))
        tag = rel.get("tag_name", "")
        asset = next((a for a in rel.get("assets") or [] if re.fullmatch(r"rayhunter-v[\d.]+-windows-x86_64\.zip", a.get("name", ""))), None)
        if not asset:
            raise RuntimeError(f"release {tag} has no Windows download")
        want = (asset.get("digest") or "").removeprefix("sha256:").lower()
        if not want:   # older releases: the .sha256 file published next to it
            side = next((a for a in rel["assets"] if a.get("name") == asset["name"] + ".sha256"), None)
            if side:
                want = _http(side["browser_download_url"], timeout=20).split()[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", want or ""):
            raise RuntimeError("GitHub did not publish a checksum for the download, so it was not used")
        if not asset["browser_download_url"].startswith("https://github.com/EFForg/rayhunter/releases/download/"):
            raise RuntimeError("unexpected download address")
        _olog(f"Downloading Rayhunter {tag} from EFF's GitHub ({asset.get('size', 0) // 1_000_000} MB)...")
        RH_DIR.mkdir(parents=True, exist_ok=True)
        z = RH_DIR / asset["name"]
        h = hashlib.sha256()
        with _RH_OPEN.open(urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "DeviceWatch/1.0"}), timeout=60) as r, open(z, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != want:
            z.unlink(missing_ok=True)
            raise RuntimeError("the download did not match GitHub's checksum and was deleted")
        dest = RH_DIR / tag
        with zipfile.ZipFile(z) as zf:
            for m in zf.namelist():
                if m.startswith(("/", "\\")) or ".." in m.replace("\\", "/").split("/"):
                    raise RuntimeError("unsafe file name inside the download")
            zf.extractall(dest)
        exe = next(iter(sorted(dest.rglob("installer.exe"))), None)
        if not exe:
            raise RuntimeError("installer.exe was not in the download")
        _set_meta("rh_installer", str(exe))
        _set_meta("rh_version", tag)
        _olog(f"Ready: Rayhunter {tag} downloaded and checked (SHA-256 matches GitHub). Plug in the Orbic whenever you like.")
        return orbic_status()
    except Exception as e:  # noqa: BLE001
        _olog(f"Download failed: {e}")
        raise
    finally:
        _O["busy"] = False


def rh_auto(q):
    """Save the hotspot's admin password (encrypted for this Windows user) and the automatic-install switch."""
    import base64  # noqa: PLC0415
    if "pw" in q:
        pw = q.get("pw") or ""
        if len(pw) > 128 or any(ord(ch) < 32 for ch in pw):
            raise ValueError("that does not look like a hotspot password")
        _set_meta("orbic_pw", base64.b64encode(_dpapi(pw.encode())).decode() if pw else "")
    if "auto" in q:
        _set_meta("rh_auto", "1" if q.get("auto") == "1" else "0")
    return orbic_status()


def _run_installer(pw):
    exe = _installer()
    _O.update(busy=True, state="installing", last_try=time.time())
    _olog("Installing Rayhunter on the Orbic (EFF's network installer). Do not unplug it; it reboots at the end.")
    try:
        p = subprocess.Popen([exe, "orbic", "--admin-password", pw], cwd=str(Path_(exe).parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, creationflags=_hs.NO_WINDOW)
        t0 = time.time()
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip().replace(pw, "****")
            if line:
                _olog("installer: " + line[:200])
            if time.time() - t0 > 900:
                p.kill()
                _olog("Stopped: the installer took more than 15 minutes.")
                break
        rc = p.wait(timeout=30)
        ok = rc == 0 and any("complete" in x["msg"].lower() or "done" in x["msg"].lower() for x in _O["log"][-15:])
        if not ok:
            _O["state"] = "failed"
            _olog(f"The installer stopped with an error (code {rc}). Nothing else was changed. Check the hotspot's password, then press INSTALL NOW to try again.")
            _alert(1, "cell", "Rayhunter install on the Orbic did not finish. See the CELL tab.")
            return False
        _olog("Installed. Waiting for the hotspot to restart...")
        for _ in range(24):
            time.sleep(10)
            try:
                if "disk_stats" in _http(f"http://{ORBIC_IP}:8080/api/system-stats"):
                    break
            except Exception:  # noqa: BLE001
                pass
        _set_meta("rayhunter_url", f"http://{ORBIC_IP}:8080")
        _O["state"] = "installed"
        _olog("Rayhunter is running on the Orbic and connected to the CELL tab.")
        _alert(2, "cell", "Rayhunter is installed on the Orbic hotspot and connected. Its warnings will show on the CELL tab and in ALERTS.")
        rh_tick(time.time())
        return True
    except Exception as e:  # noqa: BLE001
        _O["state"] = "failed"
        _olog(f"Install failed: {e}")
        return False
    finally:
        _O["busy"] = False


def Path_(p):  # noqa: N802
    from pathlib import Path  # noqa: PLC0415
    return Path(p)


def orbic_tick(now):
    usb = any(ORBIC_USB.match(d) for d in _usb_present()) if IS_WIN else False
    gws = {g["gw"]: g for g in _N.get("gw") or []}
    gw = gws.get(ORBIC_IP)
    foreign = bool(gw and _not_home(gw.get("mac")))   # 192.168.1.1 on this network is NOT the home router
    web = rh_up = False
    if usb or foreign:
        try:
            rh_up = "disk_stats" in _http(f"http://{ORBIC_IP}:8080/api/system-stats", timeout=3)
        except Exception:  # noqa: BLE001
            pass
        if not rh_up and foreign:
            try:
                web = bool(re.search(r"orbic|rc400", _http(f"http://{ORBIC_IP}/", timeout=3), re.I))
            except Exception:  # noqa: BLE001
                pass
    found = usb or web or rh_up
    _O.update(usb=usb, web=web, rh_up=rh_up)
    if foreign and (web or rh_up or usb) and gw.get("mac") != _meta("orbic_mac"):
        _set_meta("orbic_mac", gw["mac"])
    if not found:
        if _O["state"] in ("found", "waiting-network"):
            _O["state"] = "idle"
        return
    _O["seen"] = now
    if rh_up:
        if _rh_url() != f"http://{ORBIC_IP}:8080" and not _rh_url():
            _set_meta("rayhunter_url", f"http://{ORBIC_IP}:8080")
            _olog("Found Rayhunter already running on the Orbic: connected the CELL tab.")
        if _O["state"] != "installing":
            _O["state"] = "installed"
        return
    if _O["busy"]:
        return
    reachable = foreign and (web or usb)
    if not reachable:
        _O["state"] = "waiting-network"
        if now - _O["notified"] > 3600:
            _O["notified"] = now
            _olog("Orbic hotspot plugged in. To install, this PC needs to reach it: join its Wi-Fi (name and password on its label / screen).")
            _alert(1, "cell", "Orbic hotspot detected. Join its Wi-Fi from this PC so Rayhunter can be installed (CELL tab).")
        return
    _O["state"] = "found"
    pw = _orbic_pw()
    if _meta("rh_auto") == "1" and pw and _installer() and now - _O["last_try"] > INSTALL_RETRY:
        _O.update(last_try=now, busy=True)
        threading.Thread(target=_run_installer, args=(pw,), daemon=True, name="rayhunter-install").start()
    elif now - _O["notified"] > 3600:
        _O["notified"] = now
        missing = "press DOWNLOAD RAYHUNTER" if not _installer() else "enter the hotspot's admin password" if not pw else "press INSTALL NOW"
        _olog(f"Orbic hotspot found and reachable. To install: {missing} on the CELL tab.")
        _alert(1, "cell", f"Orbic hotspot found. To install Rayhunter: {missing} on the CELL tab.")


def _orbic_loop():
    while True:
        try:
            orbic_tick(time.time())
        except Exception as e:  # noqa: BLE001
            print(f"guard: orbic watch: {e}")
        time.sleep(10)


def rh_install(q=None):
    pw = (q or {}).get("pw") or _orbic_pw()
    if not _installer():
        raise ValueError("Download Rayhunter first (DOWNLOAD RAYHUNTER).")
    if not pw:
        raise ValueError("Enter the hotspot's admin password first. On a Verizon Orbic it is the same as its Wi-Fi password.")
    if _O["busy"]:
        raise ValueError("already busy")
    gw = {g["gw"]: g for g in _N.get("gw") or []}.get(ORBIC_IP)
    if not gw or not gw.get("mac") or not _not_home(gw["mac"]):
        raise ValueError(f"This PC is not connected to the Orbic: {ORBIC_IP} is your home router. Join the Orbic's Wi-Fi first.")
    threading.Thread(target=_run_installer, args=(pw,), daemon=True, name="rayhunter-install").start()
    time.sleep(0.5)
    return orbic_status()


def orbic_status(q=None):
    return {"state": _O["state"], "usb": _O["usb"], "web": _O["web"], "rh_up": _O["rh_up"], "busy": _O["busy"], "log": _O["log"][-25:],
            "installer": bool(_installer()), "version": _meta("rh_version") or "", "pw_saved": bool(_meta("orbic_pw")),
            "auto": _meta("rh_auto") == "1", "seen": _O["seen"]}


def start():
    if _W["started"] or not IS_WIN:
        return
    _W["started"] = time.time()
    _init_db()
    threading.Thread(target=_load_manuf, daemon=True, name="wifi-makers").start()
    threading.Thread(target=_wifi_loop, daemon=True, name="wifi-scan").start()
    threading.Thread(target=_boot_loop, daemon=True, name="boot-fingerprint").start()
    threading.Thread(target=_use_loop, daemon=True, name="pc-use").start()
    _N["karma_next"] = time.time() + 120     # first fake access point test two minutes after start (after the first Wi-Fi scans)
    threading.Thread(target=_net_loop, daemon=True, name="net-attacks").start()
    threading.Thread(target=_rh_loop, daemon=True, name="rayhunter").start()
    threading.Thread(target=_raw_loop, daemon=True, name="tripwire-input").start()
    threading.Thread(target=_typing_loop, daemon=True, name="tripwire-typing").start()
    threading.Thread(target=_names_loop, daemon=True, name="tripwire-names").start()
    threading.Thread(target=_away_loop, daemon=True, name="away-record").start()
    threading.Thread(target=_orbic_loop, daemon=True, name="orbic-watch").start()
