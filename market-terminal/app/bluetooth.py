"""Device Watch > BLUETOOTH: hidden-tracker detector.

Listens to Bluetooth Low Energy advertisements (listen-only: it never pairs or connects) and recognises location
trackers: Apple AirTags and other Find My accessories, Tile, Samsung SmartTag, Chipolo, Pebblebee, Google Find My
Device network tags, and trackers using the cross-platform DULT standard.

This PC does not move, so it cannot tell "following you" the way a phone can. What it can tell is "a tracker that has
been separated from its owner has been sitting close to this PC for a long time" - which is what a tracker hidden in
a bag, coat or car that you brought home looks like. Neighbours' tags whose owners are at home broadcast "near owner"
and are not flagged.

Formats follow the open-source AirGuard app (TU Darmstadt SEEMOO lab) and the IETF DULT accessory-protocol draft.
"""
from __future__ import annotations

import asyncio
import collections
import sqlite3
import statistics
import threading
import time

import hotspot as _hs  # data folder, event log and Windows notifications are shared with the hotspot monitor

DB_FILE = _hs.HS_DIR / "bluetooth.db"
NEAR_DBM = -70           # median signal at least this strong = in or next to the room
PRESENT_MIN = 40         # minutes heard in the last hour before a separated tracker counts as "staying"
ALERT_EVERY = 12 * 3600  # re-alert about the same tracker at most twice a day

APPLE = 0x004C
APPLE_FINDMY = {0: "Apple device (iPhone, iPad or Mac)", 1: "AirTag", 2: "Find My tag (non-Apple)", 3: "AirPods"}
APPLE_OTHER = {0x02: "iBeacon", 0x05: "AirDrop", 0x07: "AirPods / Beats", 0x09: "AirPlay device", 0x0C: "Apple device",
               0x0F: "Apple device", 0x10: "iPhone, iPad, Mac or Watch", 0x13: "Apple device", 0x1C: "Apple device"}
COMPANIES = {0x004C: "Apple", 0x0075: "Samsung", 0x0006: "Microsoft", 0x00E0: "Google", 0x0065: "HP", 0x00C4: "LG",
             0x0059: "Nordic Semiconductor", 0x038F: "Xiaomi", 0x0087: "Garmin", 0x0157: "Amazfit", 0x0171: "Amazon",
             0x02E5: "Espressif", 0x0131: "Cypress", 0x000F: "Broadcom", 0x0002: "Intel", 0x001D: "Qualcomm",
             0x0499: "Ruuvi", 0x0822: "Adafruit", 0x05A7: "Sonos", 0x0310: "SGL Italia", 0x065A: "Marshall"}
SERVICES = {"fe2c": "Google Fast Pair", "fe9f": "Google", "feaa": "Google", "fd5a": "Samsung", "fd69": "Samsung",
            "feed": "Tile", "feec": "Tile", "fe33": "Chipolo", "fa25": "Pebblebee", "fe78": "HP", "feb9": "LG",
            "fe07": "Sonos", "fe0f": "Philips Hue", "fe95": "Xiaomi", "fd6f": "Exposure notification"}

_S = {"radio": "unknown", "scanning": False, "err": None, "loop": None, "radio_obj": None, "started": None, "heard": 0}
_LIVE: dict[str, collections.deque] = {}  # address -> every (time, signal) reading of the last few minutes, for the finder
_AGG: dict[str, dict] = {}   # address -> what was heard since the last flush (only touched on the scanner thread)


# ----------------------------------------------------------------------------------------------- storage
def _db():
    c = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    _hs._init_db()  # shared settings (notifications on/off) live there
    with _db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS dev(addr TEXT PRIMARY KEY, first REAL, last REAL, n INT, rssi_last INT, rssi_best INT,
            name TEXT, vendor TEXT, kind TEXT, tracker TEXT, state TEXT, battery TEXT, mine INT DEFAULT 0, alerted REAL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS dev_last ON dev(last);
        CREATE TABLE IF NOT EXISTS heard(ts INT, addr TEXT, rssi INT, PRIMARY KEY(ts, addr));
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS presence(ts INT PRIMARY KEY, total INT, near INT, close INT);
        CREATE TABLE IF NOT EXISTS sightings(key TEXT, day TEXT, best INT, PRIMARY KEY(key, day));
        CREATE TABLE IF NOT EXISTS named(name TEXT, day TEXT, first REAL, last REAL, best INT, near_min INT DEFAULT 0, kind TEXT, vendor TEXT,
            PRIMARY KEY(name, day));
        """)


# ----------------------------------------------------------------------------------------------- watch hours / away
def away():
    with _db() as c:
        r = c.execute("SELECT v FROM meta WHERE k='away'").fetchone()
    return bool(r and r[0] == "1")


def set_away(q):
    _init_db()
    on = q.get("on") == "1"
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES('away', ?)", ("1" if on else "0",))
        c.execute("INSERT OR REPLACE INTO meta VALUES('away_since', ?)", (str(time.time()) if on else "",))
    _hs._event(0, "away", "Away mode switched " + ("ON: anyone using the PC or coming close is reported, at any hour" if on else "off"))
    return {"ok": True, "away": on}


def watching(ts=None):
    """True while nobody should be near the PC: during the quiet hours, or any time away mode is on."""
    return away() or _in_quiet(ts or time.time(), _quiet())


# ----------------------------------------------------------------------------------------------- named-device visitor log
# Devices that broadcast a name (earbuds, headphones, watches, fitness bands, speakers) keep it when their address
# changes, so they can be recognised when they come back - and often belong to a person. Logged per day.
NAMED_ALERT_EVERY = 3600
_NAMED = {"alerted": {}}


def _named_flush(c, now, agg):
    day = _day(now)
    fresh = []
    oldest = c.execute("SELECT MIN(first) FROM named").fetchone()[0]
    learning = not oldest or now - oldest < 2 * 86400  # first 2 days: learn the household's own devices, no alerts
    for a, g in agg.items():
        n = (g["name"] or "").strip()
        if len(n) < 3 or not g["rssi"]:
            continue
        best = max(g["rssi"])
        i = g["info"] or {}
        near = 1 if best >= NEAR_DBM else 0
        known = c.execute("SELECT 1 FROM named WHERE name=? LIMIT 1", (n,)).fetchone()
        c.execute("""INSERT INTO named(name, day, first, last, best, near_min, kind, vendor) VALUES(?,?,?,?,?,?,?,?)
                     ON CONFLICT(name, day) DO UPDATE SET last=excluded.last, best=MAX(best, excluded.best), near_min=near_min+excluded.near_min,
                     kind=CASE WHEN excluded.kind<>'' THEN excluded.kind ELSE kind END, vendor=CASE WHEN excluded.vendor<>'' THEN excluded.vendor ELSE vendor END""",
                  (n, day, g["first"], g["last"], best, near, i.get("kind", ""), i.get("vendor", "")))
        before = c.execute("SELECT COUNT(*) FROM named WHERE name=? AND day<>?", (n, day)).fetchone()[0]
        if not learning and best >= CLOSE_DBM and not before and now - _NAMED["alerted"].get(n, 0) > NAMED_ALERT_EVERY:
            fresh.append((n, best, not known))
    return fresh


def _named_alerts(now, fresh):
    if not fresh or not watching(now):
        return
    for n, best, _first in fresh:
        _NAMED["alerted"][n] = now
        why = "away mode is on" if away() else "during your quiet hours"
        msg = (f"A Bluetooth device never seen here before came within a couple of metres of the PC {why}: \"{n}\" (signal {best} dBm). "
               "Named devices are usually earbuds, headphones or a watch, which people carry.")
        _hs._event(2, "bt-visitor", msg, n)
        if _hs._cfg()["notify"]:
            _hs._toast("Device Watch: unknown device close to the PC", msg[:240])


def visitors(q):
    """Named devices heard in the last N days, one row per name."""
    _init_db()
    days = max(1, min(int(q.get("days") or 30), KEEP_DAYS))
    now = time.time()
    today = _day(now)
    with _db() as c:
        rows = c.execute("SELECT name, day, first, last, best, near_min, kind, vendor FROM named WHERE day >= ? ORDER BY day",
                         (_day(now - days * 86400),)).fetchall()
        first_log = c.execute("SELECT MIN(day) FROM named").fetchone()[0]
    by = {}
    for n, day, first, last, best, near_min, kind, vendor in rows:
        v = by.setdefault(n, {"name": n, "days": [], "first": first, "last": last, "best": best, "near_min": 0, "kind": kind or "", "vendor": vendor or "",
                              "night": 0})
        v["days"].append(day)
        v["first"], v["last"], v["best"] = min(v["first"], first), max(v["last"], last), max(v["best"] or -999, best or -999)
        v["near_min"] += near_min or 0
        v["kind"], v["vendor"] = kind or v["kind"], vendor or v["vendor"]
    qh = _quiet()
    for v in by.values():
        v["ndays"] = len(v["days"])
        v["new"] = v["days"] == [today] and bool(first_log) and first_log < today
        v["here_now"] = now - v["last"] < 180
        v["near"] = proximity(v["best"])
        v["night"] = _in_quiet(v["last"], qh)
    out = sorted(by.values(), key=lambda v: -v["last"])
    try:
        import device  # noqa: PLC0415
        with device._db() as dc:
            alerts = [dict(r) for r in dc.execute("SELECT ts, level, msg FROM events WHERE kind='bt-visitor' AND ts > ? ORDER BY ts DESC LIMIT 20",
                                                  (now - 14 * 86400,))]
    except Exception:  # noqa: BLE001
        alerts = []
    return {"rows": out[:300], "days": days, "log_since": first_log, "today": today, "alerts": alerts, "away": away(),
            "near_dbm": NEAR_DBM, "close_dbm": CLOSE_DBM}


# ----------------------------------------------------------------------------------------------- seen before?
# One row per device per day it was heard, kept 90 days. A device is recognised by its address, and also by the name it
# broadcasts (if any), because a name like "Living Room TV" or "Galaxy Watch5 (A1B2)" stays the same when the address
# changes. Phones change their address every few minutes and broadcast no name, so a phone cannot be recognised this way.
KEEP_DAYS = 90


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _backfill(c):
    """Once: seed the sightings (and the named-device log) from what the device list still remembers."""
    if not c.execute("SELECT 1 FROM meta WHERE k='named_seeded'").fetchone():
        for name, first, last, best, kind, vendor in c.execute("SELECT name, first, last, rssi_best, kind, vendor FROM dev WHERE length(trim(name)) >= 3").fetchall():
            c.execute("INSERT OR IGNORE INTO named(name, day, first, last, best, near_min, kind, vendor) VALUES(?,?,?,?,?,0,?,?)",
                      (name.strip(), _day(last), first, last, best, kind or "", vendor or ""))
        c.execute("INSERT OR REPLACE INTO meta VALUES('named_seeded', '1')")
    if c.execute("SELECT 1 FROM meta WHERE k='sightings_seeded'").fetchone():
        return
    for a, name, first, last, best in c.execute("SELECT addr, name, first, last, rssi_best FROM dev").fetchall():
        for ts in {first, last}:
            for k in _seen_keys(a, name or ""):
                c.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,?)", (k, _day(ts), best))
    c.execute("INSERT OR REPLACE INTO meta VALUES('sightings_seeded', '1')")


def _seen_keys(addr, name):
    return [addr] + (["name:" + name.strip().lower()] if name and len(name.strip()) >= 3 else [])


def seen_before(c, devs, now):
    """Adds to each device: days it was heard before today (last 90 days), and the first such day."""
    today = _day(now)
    keys = {k for d in devs for k in _seen_keys(d["addr"], d.get("name") or "")}
    hist = {}
    for i in range(0, len(keys), 400):
        chunk = list(keys)[i:i + 400]
        for k, day in c.execute(f"SELECT key, day FROM sightings WHERE day < ? AND key IN ({','.join('?' * len(chunk))})", (today, *chunk)):
            hist.setdefault(k, set()).add(day)
    for d in devs:
        days, by = set(), ""
        for k in _seen_keys(d["addr"], d.get("name") or ""):
            if hist.get(k):
                days |= hist[k]
                by = by or ("name" if k.startswith("name:") else "address")
        d["days_before"] = len(days)
        d["first_day"] = min(days) if days else None
        d["seen_by"] = by


# ----------------------------------------------------------------------------------------------- recognising trackers
def _u16(uuid: str) -> str:
    """'0000feed-0000-1000-8000-00805f9b34fb' -> 'feed' (Bluetooth SIG short form); anything else unchanged."""
    u = (uuid or "").lower()
    return u[4:8] if u.startswith("0000") and u.endswith("-0000-1000-8000-00805f9b34fb") else u


def classify(mf: dict, svc: list, sd: dict, name: str = "") -> dict:
    """What a Bluetooth advertisement is. mf: company id -> bytes; svc: service UUIDs; sd: service UUID -> bytes."""
    out = {"vendor": "", "kind": "", "tracker": "", "state": "", "battery": ""}
    sdk = {_u16(k): bytes(v) for k, v in (sd or {}).items()}
    svcs = {_u16(s) for s in (svc or [])} | set(sdk)
    a = bytes(mf.get(APPLE) or b"")
    if len(a) >= 3 and a[0] == 0x12:
        # Find My "offline finding" frame. Length 0x19 = full key = separated from its owner's devices;
        # the short form (0x02) is sent while the owner's iPhone is nearby. Status byte bits 5:4 = device type.
        t = (a[2] & 0x30) >> 4
        out.update(vendor="Apple", kind=APPLE_FINDMY[t], state="separated" if a[1] == 0x19 else "near owner",
                   battery=("full", "medium", "low", "very low")[(a[2] >> 6) & 3])
        if t:
            out["tracker"] = APPLE_FINDMY[t]
    elif a:
        out.update(vendor="Apple", kind=APPLE_OTHER.get(a[0], "Apple device"))
    g = sdk.get("feaa") or b""
    if g[:1] in (b"\x40", b"\x41"):  # Google Find My Device network frame (other FEAA frames are Eddystone beacons)
        out.update(vendor=out["vendor"] or "Google network", tracker="Google Find My Device tag or phone",
                   state="separated" if g[0] & 1 else "near owner")
    for u, label in (("feed", "Tile"), ("feec", "Tile"), ("fd5a", "Samsung SmartTag"), ("fe33", "Chipolo"), ("fa25", "Pebblebee")):
        if u in svcs and not out["tracker"]:
            out.update(vendor=out["vendor"] or label.split()[0], tracker=label)
    d = sdk.get("fcb2") or b""
    if len(d) >= 2:  # IETF DULT: byte 0 network id, byte 1 bit 0 = near-owner mode
        out["tracker"] = out["tracker"] or (out["kind"] if out["vendor"] == "Apple" else "Tracker (cross-platform standard)")
        out["state"] = out["state"] or ("near owner" if d[1] & 1 else "separated")
    if not out["vendor"]:
        for cid in mf or {}:
            if cid in COMPANIES:
                out["vendor"] = COMPANIES[cid]
                break
        else:
            out["vendor"] = next((SERVICES[s] for s in svcs if s in SERVICES), "")
    if not out["kind"]:
        n = (name or "").lower()
        out["kind"] = ("TV" if "tv" in n else "speaker / headphones" if any(x in n for x in ("bar", "jbl", "bose", "acton", "buds", "sony", "beats"))
                       else "printer" if any(x in n for x in ("print", "officejet", "laserjet", "tr8500", "envy")) else "")
    return out


def proximity(rssi):
    if rssi is None:
        return ""
    return "very close" if rssi >= -60 else "close" if rssi >= NEAR_DBM else "nearby" if rssi >= -82 else "far"


# ----------------------------------------------------------------------------------------------- scanner (own thread)
def _cb(device, adv):
    now = time.time()
    a = device.address
    g = _AGG.get(a)
    if g is None:
        g = _AGG[a] = {"first": now, "rssi": [], "name": "", "info": None, "n": 0}
    g["last"] = now
    g["n"] += 1
    if adv.rssi is not None and adv.rssi > -127:
        g["rssi"].append(adv.rssi)
        live = _LIVE.get(a)
        if live is None:
            live = _LIVE[a] = collections.deque(maxlen=240)
        live.append((now, adv.rssi))
    if adv.local_name:
        g["name"] = adv.local_name
    info = classify(adv.manufacturer_data, adv.service_uuids, adv.service_data, adv.local_name or g["name"])
    if info["tracker"] or g["info"] is None or (not g["info"]["tracker"] and info["vendor"]):
        g["info"] = info
    _S["heard"] += 1


def _flush(now):
    for a in [a for a, d in list(_LIVE.items()) if not d or now - d[-1][0] > 900]:
        _LIVE.pop(a, None)
    minute = int(now // 60) * 60
    agg = dict(_AGG)
    _AGG.clear()
    _presence(minute, agg)
    if not agg:
        return
    with _db() as c:
        fresh = _named_flush(c, now, agg)
    _named_alerts(now, fresh)
    with _db() as c:
        for a, g in agg.items():
            i = g["info"] or {}
            r = g["rssi"]
            last, best = (r[-1], max(r)) if r else (None, None)
            c.execute("""INSERT INTO dev(addr, first, last, n, rssi_last, rssi_best, name, vendor, kind, tracker, state, battery)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(addr) DO UPDATE SET last=excluded.last, n=n+excluded.n,
                         rssi_last=COALESCE(excluded.rssi_last, rssi_last), rssi_best=MAX(COALESCE(rssi_best, -999), COALESCE(excluded.rssi_best, -999)),
                         name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE name END,
                         vendor=CASE WHEN excluded.vendor<>'' THEN excluded.vendor ELSE vendor END,
                         kind=CASE WHEN excluded.kind<>'' THEN excluded.kind ELSE kind END,
                         tracker=CASE WHEN excluded.tracker<>'' THEN excluded.tracker ELSE tracker END,
                         state=CASE WHEN excluded.state<>'' THEN excluded.state ELSE state END,
                         battery=CASE WHEN excluded.battery<>'' THEN excluded.battery ELSE battery END""",
                      (a, g["first"], g["last"], g["n"], last, best, g["name"], i.get("vendor", ""), i.get("kind", ""),
                       i.get("tracker", ""), i.get("state", ""), i.get("battery", "")))
            for k in _seen_keys(a, g["name"]):
                c.execute("INSERT INTO sightings VALUES(?,?,?) ON CONFLICT(key, day) DO UPDATE SET best=MAX(COALESCE(best, -999), COALESCE(excluded.best, -999))",
                          (k, _day(now), best))
            if i.get("tracker") and best is not None:  # minute-by-minute presence, for trackers only
                c.execute("INSERT INTO heard VALUES(?,?,?) ON CONFLICT(ts, addr) DO UPDATE SET rssi=MAX(rssi, excluded.rssi)", (minute, a, best))


# ----------------------------------------------------------------------------------------------- presence log
# How many Bluetooth devices are near this PC, minute by minute. Phones, watches and earbuds are carried by people, so
# the count of devices in the same room is a rough "is somebody here" signal. Addresses change every few minutes, so
# this counts devices; it cannot say whose they are.
CLOSE_DBM = -60            # this strong = within a couple of metres
QUIET_DEFAULT = (1, 5)     # hours of the night when nobody is normally near the PC
PRES_ALERT_EVERY = 6 * 3600
_PRES = {"minute": 0, "all": set(), "near": set(), "close": set(), "streak": 0, "alerted": 0.0}


def _quiet():
    with _db() as c:
        r = c.execute("SELECT v FROM meta WHERE k='quiet'").fetchone()
    try:
        a, b = (int(x) for x in (r[0] if r else "").split("-"))
        return (a % 24, b % 24)
    except ValueError:
        return QUIET_DEFAULT


def _in_quiet(ts, q):
    h = time.localtime(ts).tm_hour
    a, b = q
    return a != b and ((a <= h < b) if a < b else (h >= a or h < b))


def _presence(minute, agg):
    p = _PRES
    if p["minute"] and minute != p["minute"] and _S["scanning"]:
        row = (p["minute"], len(p["all"]), len(p["near"]), len(p["close"]))
        with _db() as c:
            c.execute("INSERT OR REPLACE INTO presence VALUES(?,?,?,?)", row)
        try:
            _presence_alert(*row)
        except Exception as e:  # noqa: BLE001
            print(f"bluetooth presence: {e}")
    if minute != p["minute"]:
        p.update(minute=minute, all=set(), near=set(), close=set())
    for a, g in agg.items():
        if not g["rssi"]:
            continue
        best = max(g["rssi"])
        p["all"].add(a)
        if best >= NEAR_DBM:
            p["near"].add(a)
        if best >= CLOSE_DBM:
            p["close"].add(a)


def _usual(c, ts, days=14):
    """The most devices that were close at this hour of the day on earlier days -> (max, days with data)."""
    hour = time.localtime(ts).tm_hour
    day0 = int(ts // 86400)
    best, seen_days = 0, set()
    for t, n in c.execute("SELECT ts, close FROM presence WHERE ts >= ? AND ts < ?", (ts - days * 86400, ts - 12 * 3600)):
        if time.localtime(t).tm_hour == hour and int(t // 86400) != day0:
            seen_days.add(int(t // 86400))
            best = max(best, n)
    return best, len(seen_days)


def _presence_alert(minute, total, near, close):
    """In the quiet hours: more devices within a couple of metres than on any earlier night at this hour, for 5 minutes."""
    p = _PRES
    if not watching(minute):
        p["streak"] = 0
        return
    with _db() as c:
        usual, days = _usual(c, minute)
    if days < 3 or close <= usual:
        p["streak"] = 0
        return
    p["streak"] += 1
    if p["streak"] >= 5 and minute - p["alerted"] > PRES_ALERT_EVERY:
        p["alerted"] = minute
        msg = (f"{close} Bluetooth device{'s' if close != 1 else ''} within a couple of metres of this PC for 5 minutes during your quiet hours; "
               f"the most at this hour on the previous {days} nights was {usual}. Phones, watches and earbuds are carried by people, "
               "so this can mean somebody is near the PC. It can also be a new gadget, or a device you left next to it.")
        _hs._event(1, "bt-presence", msg)
        if _hs._cfg()["notify"]:
            _hs._toast("Device Watch: unusual presence near the PC", msg[:240])


def presence(q):
    """Chart data: the busiest minute in each bucket, for the last N hours."""
    _init_db()
    hours = max(1.0, min(float(q.get("hours") or 48), 24 * 14))
    now = time.time()
    bucket = 600 if hours <= 48 else 1800 if hours <= 24 * 7 else 3600
    start = int((now - hours * 3600) // bucket) * bucket
    by = {}
    with _db() as c:
        for ts, total, near, close in c.execute("SELECT ts, total, near, close FROM presence WHERE ts >= ?", (start,)):
            b = by.setdefault(ts // bucket * bucket, [0, 0, 0])
            b[0], b[1], b[2] = max(b[0], total), max(b[1], near), max(b[2], close)
        n = c.execute("SELECT COUNT(*) FROM presence").fetchone()[0]
        first = c.execute("SELECT MIN(ts) FROM presence").fetchone()[0]
    ts = list(range(start, int(now) + 1, bucket))
    qh = _quiet()
    try:
        import device  # noqa: PLC0415
        with device._db() as dc:
            alerts = [dict(r) for r in dc.execute("SELECT ts, level, msg FROM events WHERE kind='bt-presence' AND ts > ? ORDER BY ts DESC LIMIT 20",
                                                  (now - 14 * 86400,))]
    except Exception:  # noqa: BLE001
        alerts = []
    return {"t": ts, "total": [(by.get(t) or [None] * 3)[0] for t in ts], "near": [(by.get(t) or [None] * 3)[1] for t in ts],
            "close": [(by.get(t) or [None] * 3)[2] for t in ts], "quiet": [1 if _in_quiet(t, qh) else 0 for t in ts], "bucket": bucket,
            "quiet_from": qh[0], "quiet_to": qh[1], "minutes": n, "first": first, "alerts": alerts,
            "now": {"total": len(_PRES["all"]), "near": len(_PRES["near"]), "close": len(_PRES["close"])},
            "near_dbm": NEAR_DBM, "close_dbm": CLOSE_DBM}


def quiet(q):
    _init_db()
    a, b = int(q.get("from", QUIET_DEFAULT[0])) % 24, int(q.get("to", QUIET_DEFAULT[1])) % 24
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES('quiet', ?)", (f"{a}-{b}",))
    return {"ok": True, "quiet_from": a, "quiet_to": b}


def _prune(now):
    with _db() as c:
        c.execute("DELETE FROM presence WHERE ts < ?", (now - 30 * 86400,))
        c.execute("DELETE FROM sightings WHERE day < ?", (_day(now - KEEP_DAYS * 86400),))
        c.execute("DELETE FROM named WHERE day < ?", (_day(now - KEEP_DAYS * 86400),))
        c.execute("DELETE FROM dev WHERE tracker='' AND last < ?", (now - 86400,))
        c.execute("DELETE FROM dev WHERE last < ?", (now - 14 * 86400,))
        c.execute("DELETE FROM heard WHERE ts < ?", (now - 7 * 86400,))


async def _radio_state():
    from winrt.windows.devices.radios import Radio, RadioKind  # noqa: PLC0415
    if _S["radio_obj"] is None:
        for r in await Radio.get_radios_async():
            if r.kind == RadioKind.BLUETOOTH:
                _S["radio_obj"] = r
                break
    r = _S["radio_obj"]
    return "none" if r is None else r.state.name.lower()   # on / off / disabled / unknown


async def _set_radio(on):
    from winrt.windows.devices.radios import Radio, RadioState  # noqa: PLC0415
    await Radio.request_access_async()
    if await _radio_state() == "none":
        raise RuntimeError("This PC has no Bluetooth radio")
    res = await _S["radio_obj"].set_state_async(RadioState.ON if on else RadioState.OFF)
    return res.name


async def _main():
    from bleak import BleakScanner  # noqa: PLC0415
    _S["loop"] = asyncio.get_running_loop()
    scanner = None
    last_prune = 0.0
    while True:
        now = time.time()
        try:
            _S["radio"] = await _radio_state()
        except Exception as e:  # noqa: BLE001
            _S["radio"], _S["err"] = "unknown", f"could not read the Bluetooth radio: {e}"
        if _S["radio"] == "on" and scanner is None:
            try:
                scanner = BleakScanner(_cb, scanning_mode="active")
                await scanner.start()
                _S.update(scanning=True, err=None)
            except Exception as e:  # noqa: BLE001
                scanner = None
                _S.update(scanning=False, err=f"could not start listening: {e}")
        elif _S["radio"] != "on" and scanner is not None:
            try:
                await scanner.stop()
            except Exception:  # noqa: BLE001
                pass
            scanner = None
            _S["scanning"] = False
        try:
            _flush(now)
            _check_alerts(now)
            if now - last_prune > 3600:
                last_prune = now
                _prune(now)
        except Exception as e:  # noqa: BLE001
            print(f"bluetooth: {e}")
        await asyncio.sleep(15)


def _thread():
    while True:
        try:
            asyncio.run(_main())
        except Exception as e:  # noqa: BLE001  (e.g. the Bluetooth stack restarting): try again shortly
            _S.update(scanning=False, err=f"listener stopped: {e}", loop=None, radio_obj=None)
            time.sleep(30)


def start():
    if _S["started"]:
        return
    _S["started"] = time.time()
    _init_db()
    try:
        with _db() as c:
            _backfill(c)
    except Exception as e:  # noqa: BLE001
        print(f"bluetooth: sightings backfill: {e}")
    threading.Thread(target=_thread, daemon=True, name="bluetooth").start()


# ----------------------------------------------------------------------------------------------- assessment + alerts
def _assess(c, now):
    """Trackers heard in the last 24 h, with how long they have stayed nearby and a verdict."""
    rows = [dict(r) for r in c.execute("SELECT * FROM dev WHERE tracker<>'' AND last >= ? AND first <= ? ORDER BY last DESC", (now - 86400, now))]
    hr = int(now // 60) * 60 - 3600
    for d in rows:
        pts = c.execute("SELECT ts, rssi FROM heard WHERE addr=? AND ts >= ? AND ts <= ?", (d["addr"], hr, now)).fetchall()
        d["minutes_1h"] = len(pts)
        d["minutes_24h"] = c.execute("SELECT COUNT(*) FROM heard WHERE addr=? AND ts >= ? AND ts <= ?", (d["addr"], now - 86400, now)).fetchone()[0]
        d["rssi_med"] = int(statistics.median([p[1] for p in pts])) if pts else d["rssi_last"]
        d["near"] = proximity(d["rssi_med"])
        d["online"] = now - d["last"] < 120
        sep = d["state"] == "separated"
        unknown_state = not d["state"]
        staying = d["minutes_1h"] >= PRESENT_MIN and (d["rssi_med"] or -999) >= NEAR_DBM
        if d["mine"]:
            d["verdict"], d["why"] = "mine", "You marked this as yours."
        elif sep and staying:
            d["verdict"], d["why"] = "suspicious", (f"Separated from its owner and has stayed {proximity(d['rssi_med'])} to this PC for "
                                                     f"{d['minutes_1h']} of the last 60 minutes.")
        elif unknown_state and staying:
            d["verdict"], d["why"] = "review", ("This kind of tracker does not say whether its owner is nearby. It has stayed close for "
                                                 f"{d['minutes_1h']} of the last 60 minutes - fine if it is yours or a housemate's.")
        elif sep:
            d["verdict"], d["why"] = "watch", "Separated from its owner, but not close enough or not here long enough to worry about."
        else:
            d["verdict"], d["why"] = "ok", "Its owner's phone is nearby (normal for neighbours' and your own devices)."
    _inherit_mine(c, rows, now)
    _chains(c, rows, hr)
    order = {"suspicious": 0, "review": 1, "watch": 2, "ok": 3, "mine": 4}
    rows.sort(key=lambda d: (order[d["verdict"]], -(d["rssi_med"] or -999)))
    return rows


CHAIN_GAP = 15 * 60  # a separated tag that changes its address goes quiet for at most this long in between


def _inherit_mine(c, rows, now):
    """"This is mine" follows your tag when it changes its address: a separated tag of the same kind and battery level,
    with similar signal, that starts within 15 minutes of a tag you marked going quiet (and never overlaps it) is
    taken to be the same tag, and is marked yours too."""
    sep = [d for d in rows if d["state"] == "separated" and d["tracker"]]
    if not any(d["mine"] for d in sep):
        return
    mins = {d["addr"]: [r[0] for r in c.execute("SELECT ts FROM heard WHERE addr=? AND ts >= ? AND ts <= ? ORDER BY ts",
                                                 (d["addr"], now - 86400, now))] for d in sep}
    sep = sorted((d for d in sep if mins[d["addr"]]), key=lambda d: mins[d["addr"]][0])  # oldest first, so it carries forward
    for d in sep:
        if d["mine"]:
            continue
        start = mins[d["addr"]][0]
        for p in sep:
            if (p["mine"] and p is not d and p["tracker"] == d["tracker"] and p["battery"] == d["battery"]
                    and abs((p["rssi_med"] or -999) - (d["rssi_med"] or -999)) <= 10
                    and 0 <= start - mins[p["addr"]][-1] <= CHAIN_GAP and not (set(mins[p["addr"]]) & set(mins[d["addr"]]))):
                d["mine"] = 1
                d["verdict"], d["why"] = "mine", f"Your tag under a new address (it follows on from {p['addr']}, which you marked as yours)."
                c.execute("UPDATE dev SET mine=1 WHERE addr=?", (d["addr"],))
                break


def _chains(c, rows, hr):
    """A separated Find My tag changes its Bluetooth address from time to time, so one tag sitting nearby shows up as a
    string of "different" tags, each heard for only part of the hour. Join up separated tags of the same kind and battery
    level, with similar signal, that follow each other without overlapping, and judge the string as one tag."""
    sep = [d for d in rows if d["state"] == "separated" and not d["mine"] and (d["rssi_med"] or -999) >= NEAR_DBM - 5]
    if len(sep) < 2:
        return
    mins = {d["addr"]: [r[0] for r in c.execute("SELECT ts FROM heard WHERE addr=? AND ts >= ? AND ts <= ? ORDER BY ts",
                                                 (d["addr"], hr - 3 * 3600, hr + 3600))] for d in sep}
    sep = [d for d in sep if mins[d["addr"]]]
    for d in sorted(sep, key=lambda d: -mins[d["addr"]][-1]):    # newest first
        if d["verdict"] == "suspicious" or d.get("chain"):
            continue
        chain, cur = [d], d
        while True:
            start = mins[cur["addr"]][0]
            prev = [p for p in sep if p not in chain and p["tracker"] == d["tracker"] and p["battery"] == d["battery"]
                    and abs((p["rssi_med"] or -999) - (d["rssi_med"] or -999)) <= 8
                    and 0 <= start - mins[p["addr"]][-1] <= CHAIN_GAP and not (set(mins[p["addr"]]) & set(mins[cur["addr"]]))]
            if not prev:
                break
            cur = max(prev, key=lambda p: mins[p["addr"]][-1])
            chain.append(cur)
        if len(chain) < 2:
            continue
        in_hour = len({t for p in chain for t in mins[p["addr"]] if t >= hr})
        span = (mins[d["addr"]][-1] - mins[chain[-1]["addr"]][0]) / 60 + 1
        d["chain"] = [p["addr"] for p in chain]
        d["chain_alerted"] = max((p["alerted"] or 0) for p in chain)
        if in_hour >= PRESENT_MIN:
            d["verdict"], d["why"] = "suspicious", (
                f"Looks like one tag changing its address: {len(chain)} separated {d['tracker']} addresses in a row, same battery level and signal, "
                f"never heard at the same time. Together they stayed {proximity(d['rssi_med'])} to this PC for {in_hour} of the last 60 minutes "
                f"({span:.0f} minutes in all).")
            d["minutes_1h"] = in_hour


def _check_alerts(now):
    with _db() as c:
        bad = [d for d in _assess(c, now) if d["verdict"] == "suspicious"
               and now - max(d["alerted"] or 0, d.get("chain_alerted") or 0) > ALERT_EVERY]
        for d in bad:
            for a in d.get("chain") or [d["addr"]]:
                c.execute("UPDATE dev SET alerted=? WHERE addr=?", (now, a))
    for d in bad:
        msg = (f"Possible hidden tracker: {d['tracker']} separated from its owner has stayed {d['near']} to this PC for "
               f"{d['minutes_1h']} of the last 60 minutes ({d['addr']}, signal {d['rssi_med']} dBm)"
               + (f". It changed its address {len(d['chain']) - 1} time(s) along the way, which is what one tag does over time" if d.get("chain") else ""))
        _hs._event(2, "bt-tracker", msg, d["addr"])
        if _hs._cfg()["notify"]:
            _hs._toast("Device Watch: possible hidden tracker", msg)


# ----------------------------------------------------------------------------------------------- API
def _run_in_loop(coro, timeout=20):
    loop = _S["loop"]
    if loop is None:
        raise RuntimeError("The Bluetooth listener is not running yet; try again in a few seconds")
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def status(q):
    _init_db()
    now = time.time()
    with _db() as c:
        trackers = _assess(c, now)
        devs = [dict(r) for r in c.execute("SELECT addr, first, last, rssi_last, rssi_best, name, vendor, kind, tracker, state FROM dev "
                                           "WHERE last >= ? ORDER BY rssi_last DESC", (now - 180,))]
        seen_before(c, devs + trackers, now)
        first_log = c.execute("SELECT MIN(day) FROM sightings").fetchone()[0]
    for d in devs:
        d["near"] = proximity(d["rssi_last"])
    by = {}
    for d in devs:
        k = d["vendor"] or "Unknown maker"
        by[k] = by.get(k, 0) + 1
    try:
        import device  # noqa: PLC0415
        with device._db() as dc:
            alerts = [dict(r) for r in dc.execute("SELECT ts, level, msg FROM events WHERE kind='bt-tracker' AND ts > ? ORDER BY ts DESC LIMIT 20",
                                                  (now - 7 * 86400,))]
    except Exception:  # noqa: BLE001
        alerts = []
    return {"radio": _S["radio"], "scanning": _S["scanning"], "err": _S["err"], "since": _S["started"],
            "now": len(devs), "by_vendor": sorted(by.items(), key=lambda x: -x[1]), "devices": devs[:80],
            "trackers": trackers, "alerts": alerts, "near_dbm": NEAR_DBM, "present_min": PRESENT_MIN,
            "seen_since": first_log, "today": _day(now),
            "new_close": sum(1 for d in devs if not d["days_before"] and (d["rssi_last"] or -999) >= NEAR_DBM),
            "known_close": sum(1 for d in devs if d["days_before"] and (d["rssi_last"] or -999) >= NEAR_DBM)}


TX_1M = -60    # typical signal from a tag one metre away
PATH_LOSS = 2.5  # how fast the signal fades indoors (2 = open air, 3+ = through walls)


def find(q):
    """Live signal of one device, for the finder: every reading from the last minute, a smoothed level, a rough distance
    and whether it is getting stronger. Signal strength is a poor ruler (bodies, walls and the tag's angle all change
    it), so the distance is a range, and "warmer / colder" is the part to trust."""
    addr = (q.get("addr") or "").upper()
    now = time.time()
    pts = [(t, r) for t, r in list(_LIVE.get(addr) or ()) if now - t <= 60]
    with _db() as c:
        d = c.execute("SELECT name, vendor, kind, tracker, state FROM dev WHERE addr=?", (addr,)).fetchone()
    out = {"addr": addr, "label": (d["tracker"] or d["name"] or d["kind"] or d["vendor"] or "") if d else "", "state": d["state"] if d else "",
           "scanning": _S["scanning"], "pts": [[round(t - now, 1), r] for t, r in pts], "age": round(now - pts[-1][0], 1) if pts else None,
           "rssi": None, "trend": "", "dist": None}
    recent = [r for t, r in pts if now - t <= 5] or [r for _t, r in pts[-2:] if pts and now - pts[-1][0] <= 12]
    if recent:
        level = statistics.median(recent)
        before = [r for t, r in pts if 5 < now - t <= 14]
        diff = level - statistics.median(before) if before else 0
        m = 10 ** ((TX_1M - level) / (10 * PATH_LOSS))
        out.update(rssi=round(level), near=proximity(level), trend="warmer" if diff >= 3 else "colder" if diff <= -3 else "steady",
                   dist=[round(m / 1.8, 1), round(m * 1.8, 1)], best=max(r for _t, r in pts))
    return out


def radio(q):
    on = q.get("on") == "1"
    res = _run_in_loop(_set_radio(on))
    if res != "ALLOWED":
        raise RuntimeError(f"Windows did not allow turning Bluetooth {'on' if on else 'off'} ({res})")
    return {"ok": True}


def mine(q):
    _init_db()
    addr = (q.get("addr") or "").upper()
    if not addr:
        raise ValueError("addr missing")
    with _db() as c:
        cur = c.execute("UPDATE dev SET mine=? WHERE addr=?", (1 if q.get("on", "1") == "1" else 0, addr))
    if not cur.rowcount:
        raise ValueError("unknown device")
    return {"ok": True}
