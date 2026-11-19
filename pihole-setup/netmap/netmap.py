#!/usr/bin/env python3
"""
Network Map - a web page listing every device on your home network.

Runs on the Raspberry Pi next to Pi-hole. Every few minutes it:
  * sweeps the LAN with arp-scan (finds devices even if they never use DNS)
  * reads Pi-hole's own device table (names, makers, DNS lookup counts)
and serves the results at http://<pi>:8081

Standard library only. Run as root (arp-scan and Pi-hole's database need it):
  sudo python3 netmap.py
Try it anywhere with fake data:
  python3 netmap.py --demo --db demo.db
"""
import argparse
import csv
import io
import json
import os
import random
import re
import socket
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FTL_DB = "/etc/pihole/pihole-FTL.db"
OUI_FILE = "/usr/share/arp-scan/ieee-oui.txt"
OFFLINE_AFTER = 15 * 60          # not seen for this long -> offline
EVENT_KEEP_DAYS = 60
RDNS_RETRY = 3600                # re-try reverse DNS for nameless devices hourly

ARP_LINE = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3})\s+((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})\s*(.*)$")
MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")


# ----------------------------------------------------------------- helpers
def is_private_mac(mac):
    """Phones/laptops with 'private Wi-Fi address' set the locally-administered bit."""
    return bool(int(mac[1], 16) & 2)


def clean_vendor(v):
    v = re.sub(r"\s*\(DUP: \d+\)\s*$", "", (v or "").strip())
    return "" if not v or v.startswith("(Unknown") else v


_oui = None


def oui_vendor(mac):
    global _oui
    if _oui is None:
        _oui = {}
        try:
            with open(OUI_FILE, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t", 1)
                    if len(parts) == 2 and len(parts[0]) == 6 and not line.startswith("#"):
                        _oui.setdefault(parts[0].upper(), parts[1].strip())
        except OSError:
            pass
    return _oui.get(mac.replace(":", "")[:6].upper(), "")


def run(cmd, timeout=90):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


# ----------------------------------------------------------------- discovery
def default_iface():
    r = run(["ip", "-4", "route", "show", "default"], 5)
    m = re.search(r"\bdev\s+(\S+)", r.stdout) if r else None
    return m.group(1) if m else None


def self_device(iface):
    try:
        mac = Path(f"/sys/class/net/{iface}/address").read_text().strip().lower()
    except OSError:
        return None
    r = run(["ip", "-4", "-o", "addr", "show", "dev", iface], 5)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout) if r else None
    return (mac, m.group(1)) if m and MAC_RE.match(mac) else None


def parse_arp_scan(text):
    found = {}
    for line in text.splitlines():
        m = ARP_LINE.match(line.strip())
        if m:
            found.setdefault(m.group(2).lower(), (m.group(1), clean_vendor(m.group(3))))
    return found


def scan_arp(iface):
    cmd = ["arp-scan", "--localnet", "--retry=2"] + (["--interface", iface] if iface else [])
    r = run(cmd)
    if r is None:
        return {}, "arp-scan is missing or timed out"
    err = None if r.returncode == 0 else (r.stderr.strip() or f"arp-scan exit {r.returncode}")[:200]
    return parse_arp_scan(r.stdout), err


def parse_ip_neigh(text):
    """Only entries the kernel has confirmed recently."""
    found = {}
    for line in text.splitlines():
        p = line.split()
        if "lladdr" in p and p[-1] in ("REACHABLE", "DELAY", "PROBE"):
            mac = p[p.index("lladdr") + 1].lower()
            if MAC_RE.match(mac):
                found[mac] = (p[0], "")
    return found


def parse_pihole(con):
    info = {}
    rows = con.execute(
        """SELECT n.hwaddr, n.macVendor, n.firstSeen, n.lastQuery, n.numQueries,
                  a.ip, a.name, a.lastSeen
           FROM network n LEFT JOIN network_addresses a ON a.network_id = n.id""").fetchall()
    for hw, vendor, first, lastq, numq, ip, name, last in rows:
        mac = (hw or "").lower()
        if not MAC_RE.match(mac) or mac == "00:00:00:00:00:00":
            continue
        d = info.setdefault(mac, {"vendor": clean_vendor(vendor), "first_seen": first or None,
                                  "last_query": lastq or None, "queries": numq or 0,
                                  "ip": None, "ip_seen": -1, "name": None, "name_seen": -1})
        last = last or 0
        if ip and last >= d["ip_seen"]:
            d["ip"], d["ip_seen"] = ip, last
        if name and last >= d["name_seen"]:
            d["name"], d["name_seen"] = name, last
    return info


def read_pihole(path=FTL_DB):
    if not os.path.exists(path):
        return {}, None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            return parse_pihole(con), None
        finally:
            con.close()
    except sqlite3.Error as e:
        return {}, f"Pi-hole database: {e}"


_rdns_pool = ThreadPoolExecutor(max_workers=8)


def _ptr(ip):
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return None
    return None if name in (ip, "pi.hole") else name


def reverse_dns(ips, budget=8):
    futs = {_rdns_pool.submit(_ptr, ip): ip for ip in ips}
    done, _ = wait(futs, timeout=budget)
    return {futs[f]: f.result() for f in done if f.result()}


# ----------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
  mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT DEFAULT '', vendor TEXT DEFAULT '',
  nickname TEXT DEFAULT '', notes TEXT DEFAULT '', trusted INTEGER DEFAULT 0,
  is_new INTEGER DEFAULT 0, online INTEGER DEFAULT 0,
  first_seen REAL, last_seen REAL, queries INTEGER DEFAULT 0, last_query REAL,
  rdns_checked REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, mac TEXT, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path, offline_after=OFFLINE_AFTER):
        self.offline_after = offline_after
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.con.executescript(SCHEMA)

    # -- meta
    def _meta(self, key):
        r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def _set_meta(self, key, value):
        self.con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(value)))

    def _event(self, mac, kind, detail, ts):
        self.con.execute("INSERT INTO events(ts, mac, kind, detail) VALUES (?,?,?,?)",
                         (ts, mac, kind, detail))

    # -- scanning
    def apply_scan(self, seen, pihole, now, self_dev=None):
        """seen: mac -> (ip, vendor) from active scanning. pihole: mac -> info dict."""
        seen = dict(seen)
        with self.lock, self.con:
            c = self.con
            baseline_done = self._meta("baseline") is not None
            # A device that made a DNS query recently is online, even if it ignored the ARP sweep.
            for mac, d in pihole.items():
                if mac not in seen and d["ip"] and d["last_query"] and now - d["last_query"] < self.offline_after:
                    seen[mac] = (d["ip"], "")
            rows = {r["mac"]: r for r in c.execute("SELECT * FROM devices")}
            for mac, (ip, vendor) in seen.items():
                ph = pihole.get(mac, {})
                vendor = vendor or ph.get("vendor") or oui_vendor(mac)
                host = ph.get("name") or ""
                r = rows.get(mac)
                if r is None:
                    c.execute("""INSERT INTO devices(mac, ip, hostname, vendor, is_new, online, first_seen, last_seen)
                                 VALUES (?,?,?,?,?,1,?,?)""",
                              (mac, ip, host, vendor, 1 if baseline_done else 0, now, now))
                    if baseline_done:
                        self._event(mac, "new", f"{ip} {vendor}".strip(), now)
                    continue
                if r["ip"] and r["ip"] != ip:
                    self._event(mac, "ip", f"{r['ip']} -> {ip}", now)
                if not r["online"]:
                    self._event(mac, "online", ip, now)
                c.execute("""UPDATE devices SET ip=?, online=1, last_seen=?,
                               vendor=CASE WHEN vendor='' THEN ? ELSE vendor END,
                               hostname=CASE WHEN ?<>'' THEN ? ELSE hostname END
                             WHERE mac=?""", (ip, now, vendor, host, host, mac))
            # Devices Pi-hole remembers that we haven't seen: add them as history (offline, not "new").
            for mac, d in pihole.items():
                if mac in seen or mac in rows:
                    continue
                last = max(d["ip_seen"], d["last_query"] or 0) or d["first_seen"] or now
                c.execute("""INSERT INTO devices(mac, ip, hostname, vendor, online, first_seen, last_seen)
                             VALUES (?,?,?,?,0,?,?)""",
                          (mac, d["ip"], d["name"] or "", d["vendor"] or oui_vendor(mac),
                           d["first_seen"] or last, last))
            for mac, d in pihole.items():
                c.execute("""UPDATE devices SET queries=?, last_query=?,
                               hostname=CASE WHEN ?<>'' THEN ? ELSE hostname END WHERE mac=?""",
                          (d["queries"], d["last_query"], d["name"] or "", d["name"] or "", mac))
            for mac, r in rows.items():
                if r["online"] and mac not in seen and now - (r["last_seen"] or 0) >= self.offline_after:
                    c.execute("UPDATE devices SET online=0 WHERE mac=?", (mac,))
                    self._event(mac, "offline", r["ip"] or "", now)
            if self_dev:
                self._set_meta("self_mac", self_dev[0])
                c.execute("UPDATE devices SET hostname=? WHERE mac=? AND hostname=''",
                          (socket.gethostname(), self_dev[0]))
            if not baseline_done:
                self._set_meta("baseline", now)
                n = c.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
                self._event("", "baseline",
                            f"First scan: found {n} devices. Anything that joins from now on is marked NEW.", now)
            c.execute("DELETE FROM events WHERE ts < ?", (now - EVENT_KEEP_DAYS * 86400,))

    def pending_rdns(self, now):
        with self.lock:
            rows = self.con.execute(
                """SELECT ip FROM devices WHERE online=1 AND hostname='' AND ip IS NOT NULL
                   AND rdns_checked < ?""", (now - RDNS_RETRY,)).fetchall()
        return [r["ip"] for r in rows]

    def save_rdns(self, ips, names, now):
        with self.lock, self.con:
            for ip in ips:
                self.con.execute(
                    """UPDATE devices SET rdns_checked=?,
                         hostname=CASE WHEN hostname='' THEN ? ELSE hostname END WHERE ip=?""",
                    (now, names.get(ip, ""), ip))

    # -- reads / edits for the web page
    def devices(self):
        with self.lock:
            self_mac = self._meta("self_mac")
            rows = self.con.execute("SELECT * FROM devices").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("rdns_checked", None)
            d["private_mac"] = is_private_mac(d["mac"])
            d["is_self"] = d["mac"] == self_mac
            out.append(d)
        return out

    def events(self, limit):
        with self.lock:
            rows = self.con.execute(
                """SELECT e.ts, e.mac, e.kind, e.detail, d.nickname, d.hostname, d.vendor
                   FROM events e LEFT JOIN devices d ON d.mac = e.mac
                   ORDER BY e.id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def update_device(self, mac, fields):
        sets, vals = [], []
        if "nickname" in fields:
            sets.append("nickname=?"); vals.append(str(fields["nickname"]).strip()[:64])
        if "notes" in fields:
            sets.append("notes=?"); vals.append(str(fields["notes"]).strip()[:500])
        if "trusted" in fields:
            t = 1 if fields["trusted"] else 0
            sets.append("trusted=?"); vals.append(t)
            if t:
                sets.append("is_new=0")
        if not sets:
            return True
        with self.lock, self.con:
            cur = self.con.execute(f"UPDATE devices SET {', '.join(sets)} WHERE mac=?", (*vals, mac))
            return cur.rowcount > 0

    def forget(self, mac):
        with self.lock, self.con:
            return self.con.execute("DELETE FROM devices WHERE mac=?", (mac,)).rowcount > 0


# ----------------------------------------------------------------- scanner
class Scanner:
    def __init__(self, store, interval, iface=None, demo=None):
        self.store, self.interval, self.iface, self.demo = store, interval, iface, demo
        self.busy, self.last_scan, self.error = False, None, None
        self.wake = threading.Event()

    def status(self):
        return {"busy": self.busy, "last_scan": self.last_scan, "interval": self.interval,
                "error": self.error, "demo": bool(self.demo)}

    def loop(self):
        while True:
            self.scan()
            self.wake.wait(self.interval)
            self.wake.clear()

    def scan(self):
        self.busy = True
        try:
            if self.demo:
                self.demo.tick(self.store)
                self.error = None
            else:
                self._real_scan()
        except Exception as e:  # keep the service alive; show the problem on the page
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.busy = False
            self.last_scan = time.time()

    def _real_scan(self):
        iface = self.iface or default_iface()
        seen, err_arp = scan_arp(iface)
        r = run(["ip", "-4", "neigh", "show"] + (["dev", iface] if iface else []), 5)
        if r:
            for mac, v in parse_ip_neigh(r.stdout).items():
                seen.setdefault(mac, v)
        me = self_device(iface) if iface else None
        if me:
            seen.setdefault(me[0], (me[1], ""))
        pihole, err_ph = read_pihole()
        now = time.time()
        self.store.apply_scan(seen, pihole, now, me)
        ips = self.store.pending_rdns(now)
        if ips:
            self.store.save_rdns(ips, reverse_dns(ips), now)
        self.error = "; ".join(e for e in (err_arp, err_ph) if e) or None


# ----------------------------------------------------------------- demo data
class Demo:
    # hostname, maker, private MAC, nickname, reviewed, always on, DNS lookups/tick
    DEVICES = [
        ("GL-MT6000", "GL Technologies (Hong Kong) Limited", False, "Router", True, True, 40),
        ("pihole", "Raspberry Pi Trading Ltd", False, "", True, True, 5),
        ("MELE-PC", "Intel Corporate", False, "MeLE mini PC", True, True, 300),
        ("", "", True, "", False, False, 120),
        ("Galaxy-S24", "", True, "", False, False, 90),
        ("LGwebOSTV", "LG Electronics", False, "Living room TV", True, False, 200),
        ("", "Amazon Technologies Inc.", False, "", False, True, 30),
        ("HP-LaserJet", "HP Inc.", False, "Printer", True, True, 1),
        ("", "Espressif Inc.", False, "", False, True, 0),
        ("Nintendo-Switch", "Nintendo Co.,Ltd", False, "", False, False, 15),
        ("", "Roku, Inc.", False, "", False, False, 60),
        ("", "TP-LINK TECHNOLOGIES CO.,LTD.", False, "", False, True, 0),
    ]
    LATE = ("", "Tuya Smart Inc.", False, "", False, True, 8)   # joins on the 2nd scan -> NEW

    def __init__(self):
        rnd = random.Random(7)
        self.rnd = random.Random()
        self.devs = []
        for i, spec in enumerate(self.DEVICES + [self.LATE]):
            b = [rnd.randrange(256) for _ in range(6)]
            b[0] = (b[0] & 0xF0) | (0x2 if spec[2] else 0x0)
            mac = ":".join(f"{x:02x}" for x in b)
            ip = f"192.168.8.{1 if i == 0 else 2 if i == 1 else 20 + i * 3}"
            self.devs.append({"spec": spec, "mac": mac, "ip": ip, "q": rnd.randrange(50, 5000) if spec[6] else 0})
        self.ticks = 0

    def tick(self, store):
        now = time.time()
        devs = self.devs[:-1] if self.ticks == 0 else self.devs
        seen, pihole = {}, {}
        for d in devs:
            host, vendor, _, _, _, always, qpt = d["spec"]
            on = always or self.rnd.random() < 0.7
            d["q"] += qpt * (1 if on else 0)
            if on:
                seen[d["mac"]] = (d["ip"], vendor)
            if qpt:
                pihole[d["mac"]] = {"vendor": vendor, "first_seen": None, "last_query": None,
                                    "queries": d["q"], "ip": d["ip"], "ip_seen": 0,
                                    "name": host or None, "name_seen": 0}
        store.apply_scan(seen, pihole, now, (self.devs[1]["mac"], self.devs[1]["ip"]))
        if self.ticks == 0:
            for d in self.devs[:-1]:
                _, _, _, nick, trusted, _, _ = d["spec"]
                if nick or trusted:
                    store.update_device(d["mac"], {"nickname": nick, "trusted": trusted})
        self.ticks += 1


# ----------------------------------------------------------------- web
def _csv_cell(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else ""


class Handler(BaseHTTPRequestHandler):
    server_version = "NetMap/1.0"
    store = scanner = page = None

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._send(200, self.page, "text/html; charset=utf-8")
        elif u.path == "/api/devices":
            self._send(200, {"devices": self.store.devices(), "scan": self.scanner.status(), "now": time.time()})
        elif u.path == "/api/events":
            try:
                limit = max(1, min(1000, int(parse_qs(u.query).get("limit", ["200"])[0])))
            except ValueError:
                limit = 200
            self._send(200, {"events": self.store.events(limit)})
        elif u.path == "/devices.csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["name", "hostname", "ip", "mac", "maker", "online", "reviewed",
                        "first_seen", "last_seen", "dns_lookups", "notes"])
            for d in sorted(self.store.devices(), key=lambda d: d["mac"]):
                w.writerow([_csv_cell(x) for x in (
                    d["nickname"], d["hostname"], d["ip"], d["mac"], d["vendor"],
                    "yes" if d["online"] else "no", "yes" if d["trusted"] else "no",
                    _iso(d["first_seen"]), _iso(d["last_seen"]), d["queries"], d["notes"])])
            self._send(200, buf.getvalue(), "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="network-devices.csv"'})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        # Requiring a JSON content type means other websites can't post here without a CORS preflight,
        # which this server never approves.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._send(415, {"error": "expected application/json"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > 10000:
            return self._send(413, {"error": "bad length"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "bad json"})
        path = urlparse(self.path).path
        if path == "/api/scan":
            self.scanner.wake.set()
            return self._send(202, {"ok": True})
        mac = str(data.get("mac", "")).lower()
        if not MAC_RE.match(mac):
            return self._send(400, {"error": "bad mac"})
        if path == "/api/device":
            fields = {k: data[k] for k in ("nickname", "notes", "trusted") if k in data}
            ok = self.store.update_device(mac, fields)
        elif path == "/api/forget":
            ok = self.store.forget(mac)
        else:
            return self._send(404, {"error": "not found"})
        self._send(200 if ok else 404, {"ok": ok})


def main():
    ap = argparse.ArgumentParser(description="Network Map web service")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--db", default="/var/lib/netmap/netmap.db")
    ap.add_argument("--interval", type=int, default=None, help="seconds between scans (default 180, demo 20)")
    ap.add_argument("--iface", default=os.environ.get("NETMAP_IFACE"), help="network interface (default: auto)")
    ap.add_argument("--demo", action="store_true", help="fake devices, no real scanning")
    args = ap.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.db, offline_after=0 if args.demo else OFFLINE_AFTER)
    interval = args.interval or (20 if args.demo else 180)
    scanner = Scanner(store, interval, args.iface, Demo() if args.demo else None)

    Handler.store, Handler.scanner = store, scanner
    Handler.page = Path(__file__).with_name("netmap.html").read_bytes()
    threading.Thread(target=scanner.loop, daemon=True).start()

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Network Map on http://{args.bind}:{args.port}  (db: {args.db}{', DEMO' if args.demo else ''})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
