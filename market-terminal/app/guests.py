"""Device Watch > GUESTS: the guest Wi-Fi's log, sent to this PC by the guest router.

The guest network runs on its own router (set up with tools/guest-router/guest-setup.sh). Guests see a terms page and
tap Accept before they get internet access, and the router sends its system log over the home network to this PC
(UDP syslog, port 5514). From it this keeps:

* consents     - which device accepted the terms, and when: the record that shows guests were told about the logging
* devices      - hardware address, the name the device gave, the address it was handed
* lookups      - every site name a guest device looked up (DNS), with the time
* connections  - every new connection a guest device opened: destination address, port, protocol, time

Lines about the household's own devices are dropped: only devices on the guest network are kept. Every kept line is
also stored word for word in a hash chain (each line's fingerprint covers the one before it), so an export can show the
log was not edited afterwards. Kept KEEP_DAYS days. Nothing leaves this PC unless you export it.
"""
from __future__ import annotations

import collections
import csv
import hashlib
import ipaddress
import json
import re
import socket
import sqlite3
import threading
import time
from pathlib import Path

import hotspot as _hs  # data folder, event log and Windows notifications are shared

DB_FILE = _hs.HS_DIR / "guests.db"
PORT = 5514
KEEP_DAYS = 90
DNS_DEDUP = 60        # the same device looking up the same name within this many seconds is one lookup
CONN_DEDUP = 600      # the same device opening the same destination + port within this many seconds is one row
P2P_WINDOW = 600      # BitTorrent check: distinct peers on unusual ports in this window...
P2P_PEERS = 40        # ...this many or more looks like file-sharing
FLAG_EVERY = 6 * 3600
COMMON_PORTS = {53, 80, 123, 443, 853, 993, 995, 465, 587, 5222, 5223, 5228, 3478, 3479, 3480, 3481, 19302, 19305}
PIRACY = re.compile(r"(^|\.)(tracker|announce|bt\d?|dht)\.|torrent|thepiratebay|piratebay|1337x|\byts\b|yts\.|rarbg|nyaa\.|eztv|"
                    r"limetorrents|torlock|magnetdl|kickass|katcr|zooqle|fitgirl", re.I)

MAC = r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})"
RX_AUTH = re.compile(r"guestwifi\S*\s+binauth\s+(\S+)\s+" + MAC, re.I)
RX_SETUP = re.compile(r"guestwifi\S*\s+setup complete", re.I)
RX_DHCP = re.compile(r"DHCPACK\(([^)]+)\)\s+(\d+\.\d+\.\d+\.\d+)\s+" + MAC + r"(?:\s+(\S+))?", re.I)
RX_DNS = re.compile(r"query\[(\w+)\]\s+(\S+)\s+from\s+(\d+\.\d+\.\d+\.\d+)")
RX_CONN = re.compile(r"GUESTNEW\b.*?\bSRC=(\S+)\s+DST=(\S+).*?\bPROTO=(\w+)(?:.*?\bDPT=(\d+))?")

AUTH_WORDS = {"auth_client": "accepted the terms", "client_auth": "accepted the terms", "ndsctl_auth": "let in by the router admin",
              "client_deauth": "logged out", "idle_deauth": "left (idle)", "timeout_deauth": "session ended (24 h)",
              "ndsctl_deauth": "removed by the router admin", "shutdown_deauth": "router restarted"}

_S = {"started": None, "listening": False, "err": None, "last": None, "lines": 0, "kept": 0, "ignored": {}, "router": None, "setup_seen": None}
_LEASE: dict[str, tuple[str, float]] = {}       # guest ip -> (mac, when handed out)
_NETS: set[str] = set()                         # "a.b.c" prefixes of guest addresses
_SEEN: dict[tuple, float] = {}                  # dedup keys -> last time
_P2P: dict[str, collections.deque] = {}         # mac -> (time, peer) on unusual ports
_FLAGGED: dict[tuple, float] = {}
_LOCK = threading.Lock()


# ----------------------------------------------------------------------------------------------- storage
def _db():
    c = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    with _db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS raw(id INTEGER PRIMARY KEY, ts REAL, src TEXT, line TEXT, h TEXT);
        CREATE INDEX IF NOT EXISTS raw_ts ON raw(ts);
        CREATE TABLE IF NOT EXISTS consent(ts REAL, mac TEXT, ip TEXT, what TEXT);
        CREATE INDEX IF NOT EXISTS consent_mac ON consent(mac, ts);
        CREATE TABLE IF NOT EXISTS dev(mac TEXT PRIMARY KEY, ip TEXT, host TEXT, first REAL, last REAL, mine INT DEFAULT 0, note TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS lease(ts REAL, mac TEXT, ip TEXT, host TEXT);
        CREATE INDEX IF NOT EXISTS lease_ip ON lease(ip, ts);
        CREATE TABLE IF NOT EXISTS dns(ts REAL, mac TEXT, ip TEXT, name TEXT, qtype TEXT);
        CREATE INDEX IF NOT EXISTS dns_ts ON dns(ts);
        CREATE INDEX IF NOT EXISTS dns_mac ON dns(mac, ts);
        CREATE TABLE IF NOT EXISTS conn(ts REAL, mac TEXT, ip TEXT, dst TEXT, proto TEXT, dport INT);
        CREATE INDEX IF NOT EXISTS conn_ts ON conn(ts);
        CREATE INDEX IF NOT EXISTS conn_mac ON conn(mac, ts);
        CREATE INDEX IF NOT EXISTS conn_dst ON conn(dst, ts);
        CREATE TABLE IF NOT EXISTS flag(ts REAL, mac TEXT, kind TEXT, msg TEXT);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)


def _meta(c, k, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def _set_meta(c, k, v):
    c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


def _cfg():
    with _db() as c:
        try:
            return {"router_ip": "", "guest_if": "guest", **json.loads(_meta(c, "cfg") or "{}")}
        except ValueError:
            return {"router_ip": "", "guest_if": "guest"}


def _gateway():
    """This PC's default gateway: the router, when the PC is plugged into the guest router's own network."""
    if not _hs.IS_WIN:
        return ""
    try:
        out, _err = _hs._ps("(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop | Sort-Object RouteMetric, InterfaceMetric | "
                            "Select-Object -First 1).NextHop", timeout=30)
        ip = out.strip()
        ipaddress.ip_address(ip)
        return ip
    except Exception:  # noqa: BLE001
        return ""


def _hash(prev, ts, line):
    return hashlib.sha256(f"{prev}|{ts:.3f}|{line}".encode("utf-8", "replace")).hexdigest()


# ----------------------------------------------------------------------------------------------- reading the log
def _guest_ip(ip):
    return ip in _LEASE or ip.rsplit(".", 1)[0] in _NETS


def _mac_for(c, ip, ts):
    if ip in _LEASE:
        return _LEASE[ip][0]
    r = c.execute("SELECT mac FROM lease WHERE ip=? AND ts<=? ORDER BY ts DESC LIMIT 1", (ip, ts + 5)).fetchone()
    return r[0] if r else ""


def _dedup(key, now, secs):
    last = _SEEN.get(key)
    if last and now - last < secs:
        return True
    _SEEN[key] = now
    return False


def _flag(c, now, mac, kind, msg, level):
    if now - _FLAGGED.get((mac, kind), 0) < FLAG_EVERY:
        return
    _FLAGGED[(mac, kind)] = now
    mine = c.execute("SELECT mine FROM dev WHERE mac=?", (mac,)).fetchone()
    if mine and mine[0]:
        return
    c.execute("INSERT INTO flag VALUES(?,?,?,?)", (now, mac, kind, msg))
    _hs._event(level, "guest-" + kind, msg, mac)
    if level >= 2 and _hs._cfg()["notify"]:
        _hs._toast("Device Watch: guest Wi-Fi", msg[:240])


def _touch(c, mac, ip, host, now):
    c.execute("""INSERT INTO dev(mac, ip, host, first, last) VALUES(?,?,?,?,?) ON CONFLICT(mac) DO UPDATE SET last=excluded.last,
                 ip=CASE WHEN excluded.ip<>'' THEN excluded.ip ELSE ip END, host=CASE WHEN excluded.host<>'' THEN excluded.host ELSE host END""",
              (mac, ip or "", host or "", now, now))


def _line(c, now, src, line, prev):
    """Handle one syslog line. Returns the new chain head if the line was kept, else None."""
    cfg_if = _S.get("guest_if") or "guest"
    m = RX_DHCP.search(line)
    if m:
        iface, ip, mac, host = m.group(1), m.group(2), m.group(3).lower(), (m.group(4) or "")
        if cfg_if not in iface:
            return None
        _LEASE[ip] = (mac, now)
        _NETS.add(ip.rsplit(".", 1)[0])
        c.execute("INSERT INTO lease VALUES(?,?,?,?)", (now, mac, ip, host))
        _touch(c, mac, ip, host, now)
    elif (m := RX_AUTH.search(line)):
        what, mac = m.group(1), m.group(2).lower()
        ip = next((i for i, (mm, _t) in _LEASE.items() if mm == mac), "")
        c.execute("INSERT INTO consent VALUES(?,?,?,?)", (now, mac, ip, what))
        new = not c.execute("SELECT 1 FROM dev WHERE mac=?", (mac,)).fetchone()
        _touch(c, mac, ip, "", now)
        if what in ("auth_client", "client_auth") and new:
            host = c.execute("SELECT host FROM dev WHERE mac=?", (mac,)).fetchone()[0]
            _hs._event(0, "guest-join", f"New guest device accepted the Wi-Fi terms: {host or 'unnamed device'} ({mac})", mac)
    elif (m := RX_DNS.search(line)):
        qtype, name, ip = m.group(1), m.group(2).lower().rstrip("."), m.group(3)
        if not _guest_ip(ip) or _dedup(("d", ip, name), now, DNS_DEDUP):
            return None
        mac = _mac_for(c, ip, now)
        c.execute("INSERT INTO dns VALUES(?,?,?,?,?)", (now, mac, ip, name, qtype))
        if mac:
            _touch(c, mac, ip, "", now)
        if PIRACY.search(name):
            _flag(c, now, mac or ip, "piracy", f"Guest device {_label(c, mac, ip)} looked up {name}, a file-sharing / piracy site. "
                  "If this keeps happening, block the device in the router (Clients > Block).", 2)
    elif (m := RX_CONN.search(line)):
        ip, dst, proto, dport = m.group(1), m.group(2), m.group(3).upper(), int(m.group(4) or 0)
        if _dedup(("c", ip, dst, dport), now, CONN_DEDUP):
            return None
        mac = _mac_for(c, ip, now)
        c.execute("INSERT INTO conn VALUES(?,?,?,?,?,?)", (now, mac, ip, dst, proto, dport))
        if mac:
            _touch(c, mac, ip, "", now)
        if dport > 1024 and dport not in COMMON_PORTS:
            q = _P2P.setdefault(mac or ip, collections.deque())
            q.append((now, dst))
            while q and now - q[0][0] > P2P_WINDOW:
                q.popleft()
            peers = len({d for _t, d in q})
            if peers >= P2P_PEERS:
                _flag(c, now, mac or ip, "p2p", f"Guest device {_label(c, mac, ip)} connected to {peers} different addresses on unusual ports in "
                      f"{P2P_WINDOW // 60} minutes. That is what BitTorrent and other file-sharing look like. Copyright notices for this would "
                      "come to you: consider blocking the device in the router (Clients > Block).", 2)
    elif RX_SETUP.search(line):
        _S["setup_seen"] = now
    else:
        return None
    h = _hash(prev, now, line)
    c.execute("INSERT INTO raw(ts, src, line, h) VALUES(?,?,?,?)", (now, src, line, h))
    return h


def _label(c, mac, ip):
    r = c.execute("SELECT host FROM dev WHERE mac=?", (mac,)).fetchone() if mac else None
    return f"{(r and r[0]) or 'unnamed'} ({mac or ip})"


def _listen():
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", PORT))
            sock.settimeout(2.0)
            _S.update(listening=True, err=None)
            c = _db()
            prev = _meta(c, "chain_head") or "start"
            pending, last_commit, last_prune = 0, time.time(), 0.0
            while True:
                try:
                    data, (src, _p) = sock.recvfrom(8192)
                except socket.timeout:
                    data, src = None, None
                now = time.time()
                if data:
                    cfg = _S["cfg"]
                    want = cfg.get("router_ip") or _S["router"]
                    if want and src != want:
                        _S["ignored"][src] = _S["ignored"].get(src, 0) + 1
                    else:
                        _S["lines"] += 1
                        _S["last"] = now
                        line = re.sub(r"^<\d+>", "", data.decode("utf-8", "replace")).strip()
                        try:
                            with _LOCK:
                                h = _line(c, now, src, line, prev)
                        except Exception as e:  # noqa: BLE001  (one odd line must never stop the log)
                            _S["err"], h = f"could not read a log line: {e}", None
                        if h:
                            prev = h
                            _S["kept"] += 1
                            pending += 1
                if pending and (pending >= 200 or now - last_commit >= 2):
                    _set_meta(c, "chain_head", prev)
                    c.commit()
                    pending, last_commit = 0, now
                if now - last_prune > 3600:
                    last_prune = now
                    with _LOCK:
                        _prune(c, now)
                    c.commit()
                    _S["cfg"] = _cfg()
                    threading.Thread(target=lambda: _S.update(router=_gateway() or _S["router"]), daemon=True).start()  # new router plugged in?
        except OSError as e:
            _S.update(listening=False, err=f"could not listen on UDP port {PORT}: {e}")
            time.sleep(30)
        except Exception as e:  # noqa: BLE001
            _S.update(listening=False, err=f"guest log stopped: {e}")
            time.sleep(10)
        finally:
            if sock:
                sock.close()


def _prune(c, now):
    cut = now - KEEP_DAYS * 86400
    last = c.execute("SELECT h FROM raw WHERE ts < ? ORDER BY id DESC LIMIT 1", (cut,)).fetchone()
    if last:
        _set_meta(c, "chain_anchor", last[0])  # verification starts after the last deleted line
    for t in ("raw", "consent", "lease", "dns", "conn", "flag"):
        c.execute(f"DELETE FROM {t} WHERE ts < ?", (cut,))
    c.execute("DELETE FROM dev WHERE last < ? AND mine=0", (cut,))
    for k in [k for k, t in _SEEN.items() if now - t > CONN_DEDUP]:
        _SEEN.pop(k, None)


def start():
    if _S["started"]:
        return
    _S["started"] = time.time()
    _hs._init_db()  # shared settings (notifications on/off) live there
    _init_db()
    cfg = _S["cfg"] = _cfg()
    _S["guest_if"] = cfg.get("guest_if") or "guest"
    with _db() as c:  # guest addresses handed out in the last day, so lookups can be matched to devices straight away
        for ip, mac, ts in c.execute("SELECT ip, mac, MAX(ts) FROM lease WHERE ts > ? GROUP BY ip", (time.time() - 86400,)):
            _LEASE[ip] = (mac, ts)
            _NETS.add(ip.rsplit(".", 1)[0])
    threading.Thread(target=_listen, daemon=True, name="guests").start()
    threading.Thread(target=lambda: _S.update(router=_gateway()), daemon=True).start()


# ----------------------------------------------------------------------------------------------- API
def _rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args)]


def _since(q, default_days=7):
    days = max(1, min(int(q.get("days") or default_days), KEEP_DAYS))
    return time.time() - days * 86400, days


def status(q=None):
    q = q or {}
    _init_db()
    start_ts, days = _since(q)
    now = time.time()
    day0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    with _db() as c:
        devs = _rows(c, "SELECT * FROM dev WHERE last >= ? ORDER BY last DESC", (start_ts,))
        for d in devs:
            m = d["mac"]
            d["accepted"] = (c.execute("SELECT MAX(ts) FROM consent WHERE mac=? AND what IN ('auth_client','client_auth','ndsctl_auth')", (m,)).fetchone()[0])
            d["accepts"] = c.execute("SELECT COUNT(*) FROM consent WHERE mac=? AND what IN ('auth_client','client_auth','ndsctl_auth') AND ts >= ?",
                                     (m, start_ts)).fetchone()[0]
            d["lookups"] = c.execute("SELECT COUNT(*) FROM dns WHERE mac=? AND ts >= ?", (m, start_ts)).fetchone()[0]
            d["conns"] = c.execute("SELECT COUNT(*) FROM conn WHERE mac=? AND ts >= ?", (m, start_ts)).fetchone()[0]
            d["flags"] = c.execute("SELECT COUNT(*) FROM flag WHERE mac=? AND ts >= ?", (m, start_ts)).fetchone()[0]
            d["online"] = now - d["last"] < 600
        consents = _rows(c, "SELECT c.ts, c.mac, c.ip, c.what, d.host FROM consent c LEFT JOIN dev d ON d.mac=c.mac ORDER BY c.ts DESC LIMIT 40")
        for r in consents:
            r["what"] = AUTH_WORDS.get(r["what"], r["what"])
        flags = _rows(c, "SELECT f.ts, f.mac, f.kind, f.msg FROM flag f WHERE f.ts >= ? ORDER BY f.ts DESC LIMIT 30", (start_ts,))
        today = {"guests": c.execute("SELECT COUNT(DISTINCT mac) FROM dns WHERE ts >= ? AND mac<>''", (day0,)).fetchone()[0],
                 "accepts": c.execute("SELECT COUNT(*) FROM consent WHERE ts >= ? AND what IN ('auth_client','client_auth')", (day0,)).fetchone()[0],
                 "lookups": c.execute("SELECT COUNT(*) FROM dns WHERE ts >= ?", (day0,)).fetchone()[0],
                 "conns": c.execute("SELECT COUNT(*) FROM conn WHERE ts >= ?", (day0,)).fetchone()[0]}
        first = c.execute("SELECT MIN(ts) FROM raw").fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM raw").fetchone()[0]
    cfg = _S.get("cfg") or _cfg()
    return {"listening": _S["listening"], "err": _S["err"], "port": PORT, "last": _S["last"], "lines": _S["lines"], "kept": _S["kept"],
            "router": cfg.get("router_ip") or _S["router"] or "", "router_auto": not cfg.get("router_ip"), "ignored": _S["ignored"],
            "setup_seen": _S["setup_seen"], "devices": devs, "consents": consents, "flags": flags, "today": today, "days": days,
            "first": first, "total": total, "keep_days": KEEP_DAYS, "guest_nets": sorted(n + ".x" for n in _NETS)}


def device(q):
    """One guest device: when it accepted the terms, its busiest lookups and its latest activity."""
    mac = (q.get("mac") or "").lower()
    start_ts, days = _since(q)
    with _db() as c:
        d = c.execute("SELECT * FROM dev WHERE mac=?", (mac,)).fetchone()
        if not d:
            raise ValueError("unknown device")
        out = dict(d)
        out["consents"] = _rows(c, "SELECT ts, ip, what FROM consent WHERE mac=? ORDER BY ts DESC LIMIT 50", (mac,))
        for r in out["consents"]:
            r["what"] = AUTH_WORDS.get(r["what"], r["what"])
        out["top"] = _rows(c, "SELECT name, COUNT(*) n, MAX(ts) last FROM dns WHERE mac=? AND ts >= ? GROUP BY name ORDER BY n DESC LIMIT 40",
                           (mac, start_ts))
        out["recent"] = _rows(c, "SELECT ts, name FROM dns WHERE mac=? ORDER BY ts DESC LIMIT 60", (mac,))
        out["ports"] = _rows(c, "SELECT proto, dport, COUNT(*) n, COUNT(DISTINCT dst) dsts FROM conn WHERE mac=? AND ts >= ? GROUP BY proto, dport "
                                "ORDER BY n DESC LIMIT 20", (mac, start_ts))
        out["flags"] = _rows(c, "SELECT ts, kind, msg FROM flag WHERE mac=? ORDER BY ts DESC LIMIT 20", (mac,))
    out["days"] = days
    return out


def _when(q):
    s = (q.get("at") or "").strip()
    for f in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(s, f))
        except ValueError:
            pass
    raise ValueError("Give the date and time, e.g. 2026-11-20T21:15 (your local time)")


def lookup(q):
    """Who was on the guest Wi-Fi around a moment (for answering a complaint): each guest device active within the
    window, when it last accepted the terms, and, if a remote address / port was given, its connections to it."""
    at = _when(q)
    win = max(1, min(int(q.get("minutes") or 15), 24 * 60)) * 60
    dst = (q.get("dst") or "").strip()
    port = int(q.get("port") or 0)
    a, b = at - win, at + win
    with _db() as c:
        macs = {r[0] for r in c.execute("SELECT mac FROM dns WHERE ts BETWEEN ? AND ? AND mac<>'' UNION SELECT mac FROM conn WHERE ts BETWEEN ? AND ? "
                                        "AND mac<>'' UNION SELECT mac FROM consent WHERE ts BETWEEN ? AND ?", (a, b, a, b, a, b))}
        match = []
        if dst or port:
            sql = "SELECT ts, mac, ip, dst, proto, dport FROM conn WHERE ts BETWEEN ? AND ?"
            args = [a, b]
            if dst:
                sql += " AND dst=?"
                args.append(dst)
            if port:
                sql += " AND dport=?"
                args.append(port)
            match = _rows(c, sql + " ORDER BY ts LIMIT 500", args)
            macs |= {m["mac"] for m in match if m["mac"]}
        out = []
        for mac in macs:
            d = dict(c.execute("SELECT * FROM dev WHERE mac=?", (mac,)).fetchone() or {"mac": mac, "host": "", "mine": 0})
            d["accepted"] = c.execute("SELECT MAX(ts) FROM consent WHERE mac=? AND ts <= ? AND what IN ('auth_client','client_auth','ndsctl_auth')",
                                      (mac, b)).fetchone()[0]
            d["lookups"] = c.execute("SELECT COUNT(*) FROM dns WHERE mac=? AND ts BETWEEN ? AND ?", (mac, a, b)).fetchone()[0]
            d["conns"] = c.execute("SELECT COUNT(*) FROM conn WHERE mac=? AND ts BETWEEN ? AND ?", (mac, a, b)).fetchone()[0]
            d["sample"] = [r[0] for r in c.execute("SELECT DISTINCT name FROM dns WHERE mac=? AND ts BETWEEN ? AND ? LIMIT 12", (mac, a, b))]
            d["hits"] = sum(1 for m in match if m["mac"] == mac)
            out.append(d)
        covered = c.execute("SELECT COUNT(*) FROM raw WHERE ts BETWEEN ? AND ?", (a - 3600, b + 3600)).fetchone()[0]
    out.sort(key=lambda d: (-d["hits"], -(d["lookups"] + d["conns"])))
    return {"at": at, "minutes": win // 60, "devices": out, "matches": match[:100], "logged_nearby": covered}


def mine(q):
    mac = (q.get("mac") or "").lower()
    with _db() as c:
        cur = c.execute("UPDATE dev SET mine=? WHERE mac=?", (1 if q.get("on", "1") == "1" else 0, mac))
    if not cur.rowcount:
        raise ValueError("unknown device")
    return {"ok": True}


def settings(q):
    ip = (q.get("router_ip") or "").strip()
    if ip:
        ipaddress.ip_address(ip)
    with _db() as c:
        cfg = {**_cfg(), "router_ip": ip}
        _set_meta(c, "cfg", json.dumps(cfg))
    _S["cfg"] = cfg
    _S["ignored"] = {}
    return {"ok": True, "router_ip": ip}


def verify(q=None):
    """Recompute the fingerprint chain over every kept line. Any edited, inserted or removed line breaks it."""
    with _LOCK, _db() as c:
        prev = _meta(c, "chain_anchor") or "start"
        n, bad = 0, None
        for rid, ts, line, h in c.execute("SELECT id, ts, line, h FROM raw ORDER BY id"):
            if _hash(prev, ts, line) != h:
                bad = rid
                break
            prev, n = h, n + 1
        head = _meta(c, "chain_head")
    ok = bad is None and (n == 0 or head is None or prev == head)
    return {"ok": ok, "checked": n, "bad_id": bad, "head": prev}


def export(q):
    """Write the records for a date range (optionally one device) to a new folder in Downloads."""
    a = time.mktime(time.strptime(q.get("from") or time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    b = time.mktime(time.strptime(q.get("to") or time.strftime("%Y-%m-%d"), "%Y-%m-%d")) + 86400
    mac = (q.get("mac") or "").lower()
    out = Path.home() / "Downloads" / time.strftime("GuestWiFi-evidence-%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    iso = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))  # noqa: E731
    tz = time.strftime("%z")
    fm = " AND mac=?" if mac else ""
    args = (a, b, mac) if mac else (a, b)
    counts = {}
    with _db() as c:
        for name, sql, cols in (
                ("acceptances", f"SELECT ts, mac, ip, what FROM consent WHERE ts >= ? AND ts < ?{fm} ORDER BY ts", ("time", "device_mac", "ip", "event")),
                ("lookups", f"SELECT ts, mac, ip, name, qtype FROM dns WHERE ts >= ? AND ts < ?{fm} ORDER BY ts", ("time", "device_mac", "ip", "name", "type")),
                ("connections", f"SELECT ts, mac, ip, dst, proto, dport FROM conn WHERE ts >= ? AND ts < ?{fm} ORDER BY ts",
                 ("time", "device_mac", "ip", "destination", "protocol", "port")),
                ("addresses-handed-out", f"SELECT ts, mac, ip, host FROM lease WHERE ts >= ? AND ts < ?{fm} ORDER BY ts", ("time", "device_mac", "ip", "device_name"))):
            rows = c.execute(sql, args).fetchall()
            counts[name] = len(rows)
            with open(out / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for r in rows:
                    w.writerow([iso(r[0]) + " " + tz, *r[1:]])
        devs = c.execute("SELECT mac, host, first, last FROM dev" + (" WHERE mac=?" if mac else ""), (mac,) if mac else ()).fetchall()
        with open(out / "devices.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(("device_mac", "device_name", "first_seen", "last_seen"))
            for r in devs:
                w.writerow((r[0], r[1], iso(r[2]), iso(r[3])))
        with open(out / "raw-router-log.txt", "w", encoding="utf-8") as f:
            f.write("# Every guest-network line received from the router, word for word, in the order received.\n"
                    "# fingerprint = SHA-256 of (previous fingerprint | time | line). Changing, adding or removing any line breaks the chain.\n")
            prev = c.execute("SELECT h FROM raw WHERE ts < ? ORDER BY id DESC LIMIT 1", (a,)).fetchone()
            f.write(f"# chain continues from fingerprint: {prev[0] if prev else _meta(c, 'chain_anchor') or 'start'}\n")
            for rid, ts, src, line, h in c.execute("SELECT id, ts, src, line, h FROM raw WHERE ts >= ? AND ts < ? ORDER BY id", (a, b)):
                f.write(f"{rid}\t{ts:.3f}\t{iso(ts)} {tz}\tfrom {src}\t{h}\t{line}\n")
    v = verify()
    (out / "README.txt").write_text(
        f"Guest Wi-Fi records, {time.strftime('%Y-%m-%d', time.localtime(a))} to {time.strftime('%Y-%m-%d', time.localtime(b - 1))}"
        + (f", device {mac}" if mac else ", all guest devices") + f".\nExported {iso(time.time())} {tz} by Device Watch.\n\n"
        "Every guest had to accept the network's terms of use (which say activity is logged) before getting internet access;\n"
        "acceptances.csv lists each acceptance. Times are this PC's local time (UTC offset shown). Content of communications\n"
        "(messages, pages, files) was never recorded: only device addresses, site names looked up, and connection addresses/ports.\n\n"
        + "\n".join(f"{k}: {n} rows" for k, n in counts.items())
        + f"\n\nIntegrity check of the whole log at export: {'PASSED' if v['ok'] else 'FAILED at line ' + str(v['bad_id'])} ({v['checked']} lines checked).\n",
        encoding="utf-8")
    return {"ok": True, "path": str(out), "counts": counts, "verified": v["ok"]}
