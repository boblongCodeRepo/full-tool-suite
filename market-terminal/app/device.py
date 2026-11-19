"""Device Watch (/device/) - what is this PC talking to, and what did the manufacturer put on it?

Built for a MeLE mini PC the owner does not fully trust, but works on any Windows 10/11 machine.

* Collector: a background thread samples every TCP/UDP socket every few seconds (psutil), records which program talked
  to which remote address, the PC's total upload/download, listening ports and the Windows DNS cache, into SQLite.
  It runs whenever the local server runs; "always-on" mode (a Startup-folder shortcut that starts the server headless
  with --collect) keeps it logging around the clock.
* Enrichment: every public IP is looked up by RDAP straight at the regional internet registry (ARIN/RIPE/APNIC/...,
  chosen from IANA's bootstrap file) - who owns the network and in which country. Every program that touches the
  network has its Authenticode signature checked (who signed the exe, or nobody).
* Audit: one-shot checks of the things an OEM can plant: the WPBT firmware table (BIOS-injected software), OEM
  drivers, non-Microsoft services / scheduled tasks / startup entries, root certificates, hosts file, proxy and DNS
  settings, remote-access tools, local accounts.

Limits (shown in the app): traffic contents are encrypted, so this shows WHO / WHEN / HOW OFTEN, not WHAT. Windows has
no per-program byte counter without admin, so bytes are whole-PC. Anything below Windows (BIOS, Intel ME) can only be
seen from another device - the router.
"""
from __future__ import annotations

import base64
import ctypes
import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import requests

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
DATA_DIR = Path(os.environ.get("MT_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MarketTerminal")
DEV_DIR = DATA_DIR / "device"
DEV_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DEV_DIR / "netlog.db"
SAMPLE_SECS = 5
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flash from the windowed exe

from urllib.parse import urlparse  # noqa: E402

RDAP_HOSTS = {"rdap.arin.net", "rdap.db.ripe.net", "rdap.apnic.net", "rdap.lacnic.net", "rdap.afrinic.net"}
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "DeviceWatch/1.0 (local network monitor)", "Accept": "application/rdap+json, application/json"})

# ----------------------------------------------------------------------------------------------- classification
WATCH_COUNTRIES = {"CN": "China", "HK": "Hong Kong", "MO": "Macau"}
CHINA_ORGS = re.compile(
    r"alibaba|aliyun|alicloud|tencent|baidu|huawei|chinanet|china ?telecom|china ?unicom|china ?mobile|cmnet|cernet|"
    r"bytedance|byteplus|douyin|tiktok|xiaomi|kingsoft|qihoo|\b360\b|zte\b|netease|sina\b|sohu|jd\.com|jingdong|"
    r"ucloud|qcloud|wangsu|chinacache|shenzhen|beijing|shanghai|guangzhou|hangzhou|mele\b|"
    r"aceville|tencent cloud|alibaba\.com singapore|huawei (international|cloud)|byteplus|lemon inc|"
    r"cdnetworks|baishan|kuaishou|zenlayer|dpg technology|ctg ?global|cmi\b|china mobile international", re.I)
KNOWN_ORGS = [
    ("Microsoft", r"microsoft|msft|azure|bing|skype|linkedin|github"),
    ("Google", r"google|youtube|1e100"),
    ("Amazon / AWS", r"amazon|aws|a2z|cloudfront"),
    ("Cloudflare", r"cloudflare"),
    ("Akamai", r"akamai|linode"),
    ("Fastly", r"fastly"),
    ("Apple", r"apple"),
    ("Meta", r"facebook|meta platforms"),
    ("Yahoo", r"yahoo|oath\b"),
    ("Intel", r"intel\b|intel corp"),
    ("Edgio / Limelight", r"edgecast|edgio|limelight"),
    ("Your ISP", r"comcast|charter|spectrum|verizon|at&t|att-|cox comm|t-mobile|frontier|centurylink|lumen"),
]
_KNOWN = [(n, re.compile(p, re.I)) for n, p in KNOWN_ORGS]
SAFE_SIGNERS = re.compile(r"microsoft|apple|software signing|intel|realtek|google|mozilla|python software|anaconda|node\.?js|openjs|github|"
                          r"valve|nvidia|advanced micro|amd\b|apple|adobe|oracle|dropbox|zoom|brave|opera|"
                          r"jetbrains|docker|git for windows|the git|senary|synaptics|conexant|qualcomm|mediatek", re.I)
REMOTE_TOOLS = re.compile(r"anydesk|teamviewer|rustdesk|todesk|sunlogin|oray|splashtop|logmein|vnc|screenconnect|"
                          r"connectwise|ammyy|radmin|supremo|aweray|awesun|parsec|chrome remote desktop|meshcentral|"
                          r"getscreen|dwservice|zoho assist|netsupport", re.I)
CHINA_DNS = {"114.114.114.114", "114.114.115.115", "223.5.5.5", "223.6.6.6", "180.76.76.76", "119.29.29.29",
             "182.254.116.116", "1.2.4.8", "210.2.4.8", "117.50.10.10", "52.80.52.52"}


def _ip_kind(ip):
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return "invalid"
    if a.is_loopback:
        return "loopback"
    if a.is_multicast:
        return "multicast"
    if a.is_link_local:
        return "link-local"
    if a.is_private or getattr(a, "is_site_local", False):
        return "lan"
    if a.is_unspecified or a.is_reserved:
        return "special"
    return "public"


def _org_cat(org):
    for n, rx in _KNOWN:
        if org and rx.search(org):
            return n
    return None


def classify(info, proc="", exe="", signer=""):
    """-> (category, flag_level 0/1/2, reasons[])"""
    reasons, lvl = [], 0
    org = (info or {}).get("org") or ""
    cc = (info or {}).get("country") or ""
    cat = _org_cat(org) or ""
    if cc in WATCH_COUNTRIES:
        reasons.append(f"Network registered in {WATCH_COUNTRIES[cc]}")
        lvl = 2
    if org and CHINA_ORGS.search(org):
        reasons.append(f"Chinese-linked network owner: {org}")
        lvl = 2
    hay = f"{proc} {exe} {signer}"
    if re.search(r"\bmele\b|melee?pc|shenzhen", hay, re.I):
        reasons.append("Program from MeLE / a Shenzhen vendor")
        lvl = 2
    if signer in ("NotSigned", "unsigned"):
        reasons.append("Program is not digitally signed")
        lvl = max(lvl, 1)
    elif signer and signer.startswith("BAD:"):
        reasons.append("Program signature is INVALID (" + signer[4:] + ")")
        lvl = 2
    if REMOTE_TOOLS.search(hay):
        reasons.append("Remote-access tool")
        lvl = max(lvl, 1)
    return cat or ("Other" if org else ""), lvl, reasons


# ----------------------------------------------------------------------------------------------- database
_DB_LOCK = threading.Lock()


def _db():
    c = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _db_ok():
    try:
        c = sqlite3.connect(DB_FILE, timeout=30)
        try:
            return c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            c.close()
    except sqlite3.DatabaseError:
        return False


def _set_aside_db(why):
    """A damaged log (crash / power cut mid-write) must not stop monitoring: keep it for inspection, start a new one."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suf in ("", "-wal", "-shm"):
        p = Path(str(DB_FILE) + suf)
        if p.exists():
            try:
                p.replace(Path(str(DB_FILE) + f".broken-{stamp}{suf}"))
            except OSError:
                pass
    print(f"netlog.db: {why} - moved aside, started a new log")
    _STATE["err"] = None


def _init_db():
    if DB_FILE.exists() and not _db_ok():
        _set_aside_db("damaged")
    with _DB_LOCK, _db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS conns(proc TEXT, exe TEXT, rip TEXT, rport INT, proto TEXT, first REAL, last REAL,
            samples INT, PRIMARY KEY(exe, rip, rport, proto));
        CREATE INDEX IF NOT EXISTS conns_last ON conns(last);
        CREATE INDEX IF NOT EXISTS conns_rip ON conns(rip);
        CREATE TABLE IF NOT EXISTS traffic(ts REAL PRIMARY KEY, sent INT, recv INT, nconn INT, nproc INT);
        CREATE TABLE IF NOT EXISTS dns(name TEXT, ip TEXT, first REAL, last REAL, PRIMARY KEY(name, ip));
        CREATE INDEX IF NOT EXISTS dns_ip ON dns(ip);
        CREATE TABLE IF NOT EXISTS ipinfo(ip TEXT PRIMARY KEY, org TEXT, country TEXT, net TEXT, rdns TEXT, src TEXT, t REAL);
        CREATE TABLE IF NOT EXISTS sigs(exe TEXT PRIMARY KEY, signer TEXT, status TEXT, company TEXT, t REAL);
        CREATE TABLE IF NOT EXISTS listen(proc TEXT, exe TEXT, lip TEXT, lport INT, proto TEXT, first REAL, last REAL,
            PRIMARY KEY(exe, lip, lport, proto));
        CREATE TABLE IF NOT EXISTS events(ts REAL, level INT, kind TEXT, msg TEXT, ref TEXT);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)


# ----------------------------------------------------------------------------------------------- collector
_STATE = {"started": None, "samples": 0, "last": None, "err": None, "live": [], "rate": (0.0, 0.0), "partial": False}
_PROC_CACHE = {}  # pid -> (create_time, name, exe, user)
_PENDING_IPS = set()
_PENDING_EXES = set()
_ENRICH_EV = threading.Event()
_STARTED = threading.Event()


def _proc(pid):
    if not pid:
        return ("System Idle" if pid == 0 else "?", "", "")
    try:
        p = psutil.Process(pid)
        ct = p.create_time()
        hit = _PROC_CACHE.get(pid)
        if hit and hit[0] == ct:
            return hit[1:]
        name = p.name()
        try:
            exe = p.exe()
        except (psutil.AccessDenied, psutil.ZombieProcess):
            exe = ""
        try:
            user = p.username()
        except (psutil.AccessDenied, psutil.ZombieProcess, KeyError):
            user = ""
        _PROC_CACHE[pid] = (ct, name, exe, user)
        return name, exe, user
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return ("pid " + str(pid), "", "")


class _Conn:
    __slots__ = ("type", "laddr", "raddr", "status", "pid")

    def __init__(self, k, pid):
        self.type, self.laddr, self.raddr, self.status, self.pid = k.type, k.laddr, k.raddr, k.status, pid


def _all_conns():
    """Every inet socket with its pid. macOS only lets root list everyone's sockets at once; without root, fall back
    to asking each process the user owns (other users' and system daemons' sockets are then not visible)."""
    try:
        conns = psutil.net_connections("inet")
        _STATE["partial"] = False
        return conns
    except psutil.AccessDenied:
        pass
    out = []
    for p in psutil.process_iter(["pid"]):
        try:
            get = getattr(p, "net_connections", None) or p.connections
            out += [_Conn(k, p.pid) for k in get("inet")]
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            continue
    _STATE["partial"] = True
    return out


def _sample(c, known_exes, known_ips, prev_io):
    now = time.time()
    live, listen = [], []
    conns = _all_conns()
    for k in conns:
        proto = "TCP" if k.type == socket.SOCK_STREAM else "UDP"
        name, exe, user = _proc(k.pid)
        if k.status == psutil.CONN_LISTEN or (proto == "UDP" and not k.raddr):
            if k.laddr and _ip_kind(k.laddr.ip) != "loopback":
                listen.append((name, exe or name, k.laddr.ip, k.laddr.port, proto))
            continue
        if not k.raddr or not k.pid:  # pid 0 = closed (TIME_WAIT) sockets Windows no longer ties to a program
            continue
        rip = k.raddr.ip
        kind = _ip_kind(rip)
        if kind in ("loopback", "invalid"):
            continue
        live.append({"pid": k.pid, "proc": name, "exe": exe, "user": user, "proto": proto, "lport": k.laddr.port if k.laddr else None,
                     "rip": rip, "rport": k.raddr.port, "status": k.status, "kind": kind})
    seen = {}
    for x in live:
        seen[(x["exe"] or x["proc"], x["rip"], x["rport"], x["proto"])] = x["proc"]
    for (exe, rip, rport, proto), name in seen.items():
        cur = c.execute("UPDATE conns SET last=?, samples=samples+1, proc=? WHERE exe=? AND rip=? AND rport=? AND proto=?",
                        (now, name, exe, rip, rport, proto))
        if cur.rowcount == 0:
            c.execute("INSERT INTO conns VALUES(?,?,?,?,?,?,?,1)", (name, exe, rip, rport, proto, now, now))
            if rip not in known_ips and _ip_kind(rip) == "public":
                _PENDING_IPS.add(rip)
        if exe and exe not in known_exes and os.path.isabs(exe):
            known_exes.add(exe)
            _PENDING_EXES.add(exe)
            c.execute("INSERT INTO events VALUES(?,?,?,?,?)", (now, 0, "new-program", f"{name} used the network for the first time", exe))
    for name, exe, lip, lport, proto in set(listen):
        cur = c.execute("UPDATE listen SET last=?, proc=? WHERE exe=? AND lip=? AND lport=? AND proto=?", (now, name, exe, lip, lport, proto))
        if cur.rowcount == 0:
            c.execute("INSERT INTO listen VALUES(?,?,?,?,?,?,?)", (name, exe, lip, lport, proto, now, now))
    io = psutil.net_io_counters()
    if prev_io[0] is not None:
        dt_ = max(now - prev_io[1], 1e-6)
        ds, dr = max(0, io.bytes_sent - prev_io[0].bytes_sent), max(0, io.bytes_recv - prev_io[0].bytes_recv)
        c.execute("INSERT OR REPLACE INTO traffic VALUES(?,?,?,?,?)", (now, ds, dr, len(live), len({x['pid'] for x in live})))
        _STATE["rate"] = (ds / dt_, dr / dt_)
    prev_io[0], prev_io[1] = io, now
    _STATE["live"] = live
    if _PENDING_IPS or _PENDING_EXES:
        _ENRICH_EV.set()


DNS_LOG = "Microsoft-Windows-DNS-Client/Operational"
_DNS_EV = re.compile(r"<Event[ >].*?</Event>", re.S)
_DNS_SKIP = (".arpa", ".local", ".mshome.net", ".localdomain", ".lan", ".home")


def _dns_events(xml_text, host=""):
    """Windows DNS-client log (event 3008, "query completed") -> (highest record id, [(name, ip, time)])."""
    import html  # noqa: PLC0415
    import ipaddress  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415
    top, out = 0, []
    host = host.lower()
    for ev in _DNS_EV.findall(xml_text):
        m = re.search(r"<EventRecordID>(\d+)</EventRecordID>", ev)
        if m:
            top = max(top, int(m.group(1)))
        d = {k: html.unescape(v) for k, v in re.findall(r"<Data Name=['\"](\w+)['\"]>(.*?)</Data>", ev, re.S)}
        name = (d.get("QueryName") or "").strip().lower().rstrip(".")
        if d.get("QueryStatus") != "0" or d.get("QueryType") not in ("1", "28") or "." not in name or name.endswith(_DNS_SKIP):
            continue
        if name.startswith("wpad.") or (host and name.startswith(host + ".")):  # Windows trying its own name on the provider's domain
            continue
        ts = time.time()
        m = re.search(r"SystemTime=['\"]([^'\"]+)['\"]", ev)
        if m:
            try:
                s = re.sub(r"(\.\d{6})\d*", r"\1", m.group(1).replace("Z", "+00:00"))
                ts = datetime.fromisoformat(s).timestamp()
            except ValueError:
                pass
        for part in (d.get("QueryResults") or "").split(";"):
            part = part.strip()
            if not part or part.startswith("type:"):  # "type: 5 alias.example.com" = an alias on the way, not an address
                continue
            if part.lower().startswith("::ffff:") and "." in part:
                part = part[7:]
            try:
                ip = ipaddress.ip_address(part)
            except ValueError:
                continue
            if not (ip.is_loopback or ip.is_unspecified):
                out.append((name, str(ip), ts))
    return top, out


def _dns_log(c):
    """Every name any program on this PC resolved through Windows, from the DNS-client log. The log is switched on by
    TRAPS > REPAIR DECOYS (needs admin once); reading it does not need admin. Returns False if it gave nothing."""
    row = c.execute("SELECT v FROM meta WHERE k='dns_rid'").fetchone()
    last = int(row[0]) if row else 0
    try:
        r = subprocess.run(["wevtutil", "qe", DNS_LOG, f"/q:*[System[(EventID=3008) and (EventRecordID>{last})]]", "/f:xml", "/c:4000"],
                           capture_output=True, timeout=40, creationflags=NO_WINDOW)
    except Exception:  # noqa: BLE001
        return False
    if r.returncode:
        return False
    top, rows = _dns_events(r.stdout.decode("utf-8", "replace"), os.environ.get("COMPUTERNAME", ""))
    for n, ip, ts in rows:
        cur = c.execute("UPDATE dns SET last=MAX(last, ?) WHERE name=? AND ip=?", (ts, n, ip))
        if cur.rowcount == 0:
            c.execute("INSERT INTO dns VALUES(?,?,?,?)", (n, ip, ts, ts))
    if top > last:
        c.execute("INSERT OR REPLACE INTO meta VALUES('dns_rid', ?)", (str(top),))
        c.execute("INSERT OR REPLACE INTO meta VALUES('dns_log_t', ?)", (str(time.time()),))
    return top > last


def _dns_snapshot(c):
    if not IS_WIN:  # macOS / Linux don't expose a readable resolver cache
        return
    _dns_log(c)
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-DnsClientCache | Where-Object { $_.Type -in 1,28,5 } | "
          "Select-Object Entry, Data | ConvertTo-Json -Compress")
    try:
        out = _ps(ps, 40)
    except Exception:  # noqa: BLE001
        return
    if not out.strip():
        return
    try:
        j = json.loads(out)
    except ValueError:
        return
    now = time.time()
    for e in j if isinstance(j, list) else [j]:
        n, ip = (e.get("Entry") or "").lower(), (e.get("Data") or "")
        if not n or not ip:
            continue
        cur = c.execute("UPDATE dns SET last=? WHERE name=? AND ip=?", (now, n, ip))
        if cur.rowcount == 0:
            c.execute("INSERT INTO dns VALUES(?,?,?,?)", (n, ip, now, now))


def _collector():
    _init_db()
    with _db() as c:
        # clean-ups: un-attributable closed sockets, and owners that came back as a maintainer handle / error
        c.execute("DELETE FROM conns WHERE proc IN ('System Idle', '?')")
        c.execute("DELETE FROM ipinfo WHERE org LIKE '%-MNT' OR org LIKE 'MNT-%' OR org = '' OR src LIKE 'error%'")
        known_exes = {r[0] for r in c.execute("SELECT exe FROM sigs")}
        known_ips = {r[0] for r in c.execute("SELECT ip FROM ipinfo")}
        # anything recorded but never enriched (e.g. the app was closed mid-way)
        _PENDING_IPS.update(r[0] for r in c.execute("SELECT DISTINCT rip FROM conns WHERE rip NOT IN (SELECT ip FROM ipinfo)")
                            if _ip_kind(r[0]) == "public")
        _PENDING_EXES.update(r[0] for r in c.execute("SELECT DISTINCT exe FROM conns WHERE exe NOT IN (SELECT exe FROM sigs)")
                             if r[0] and os.path.isabs(r[0]))
        known_exes |= set(_PENDING_EXES)
    _STATE["started"] = time.time()
    threading.Thread(target=_enricher, daemon=True).start()
    prev_io = [None, 0.0]
    last_dns = last_prune = 0.0
    while True:
        t0 = time.time()
        try:
            with _DB_LOCK, _db() as c:
                _sample(c, known_exes, known_ips, prev_io)
                if t0 - last_dns > 30:
                    last_dns = t0
                    _dns_snapshot(c)
                if t0 - last_prune > 3600:
                    last_prune = t0
                    c.execute("DELETE FROM traffic WHERE ts < ?", (t0 - 90 * 86400,))
                    c.execute("DELETE FROM events WHERE ts < ?", (t0 - 180 * 86400,))
            _STATE["samples"] += 1
            _STATE["last"] = time.time()
            _STATE["err"] = None
        except sqlite3.DatabaseError as e:
            _STATE["err"] = f"{type(e).__name__}: {e}"
            if "malformed" in str(e) or "not a database" in str(e):
                _set_aside_db(str(e))
                _init_db()  # takes _DB_LOCK itself; the failed `with` block above has already released it
        except Exception as e:  # noqa: BLE001
            _STATE["err"] = f"{type(e).__name__}: {e}"
        time.sleep(max(0.5, SAMPLE_SECS - (time.time() - t0)))


def start_collector():
    if _STARTED.is_set():
        return
    _STARTED.set()
    threading.Thread(target=_collector, daemon=True, name="device-collector").start()


# ----------------------------------------------------------------------------------------------- enrichment
_BOOT = {}


def _bootstrap():
    """IANA RDAP bootstrap: which registry serves which address block."""
    if _BOOT:
        return _BOOT
    f = DEV_DIR / "rdap_bootstrap.json"
    data = None
    if f.is_file() and time.time() - f.stat().st_mtime < 30 * 86400:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            data = None
    if not data:
        data = {}
        for v in ("ipv4", "ipv6"):
            r = HTTP.get(f"https://data.iana.org/rdap/{v}.json", timeout=20)
            r.raise_for_status()
            data[v] = r.json()["services"]
        f.write_text(json.dumps(data), encoding="utf-8")
    for v in ("ipv4", "ipv6"):
        _BOOT[v] = [(ipaddress.ip_network(net), urls) for nets, urls in data[v] for net in nets]
    return _BOOT


def _vcard_field(ent, key):
    for item in (ent.get("vcardArray") or [None, []])[1]:
        if item and item[0] == key:
            return item
    return None


def _rdap(ip):
    a = ipaddress.ip_address(ip.split("%")[0])
    boot = _bootstrap()["ipv4" if a.version == 4 else "ipv6"]
    base = None
    best = -1
    for net, urls in boot:
        if a.version == net.version and a in net and net.prefixlen > best:
            best, base = net.prefixlen, next((u for u in urls if u.startswith("https://")), None)
    # Only the five regional registries, only over HTTPS (requests checks the certificate is valid, unexpired and
    # issued for that exact name; a lookup that fails the check is not made).
    if not base or urlparse(base).hostname not in RDAP_HOSTS:
        return {}
    r = HTTP.get(base.rstrip("/") + "/ip/" + str(a), timeout=20, verify=True, allow_redirects=False)
    if r.is_redirect and urlparse(r.headers.get("Location", "")).scheme == "https" and urlparse(r.headers["Location"]).hostname in RDAP_HOSTS:
        r = HTTP.get(r.headers["Location"], timeout=20, verify=True, allow_redirects=False)
    if r.status_code == 429:
        raise RuntimeError("rate limited")
    r.raise_for_status()
    j = r.json()
    cc = j.get("country")
    # Candidate owners, best first: ORG-* objects / vcard kind "org" with the registrant role; never maintainer
    # (…-MNT) or person/role handles, which RIPE and APNIC also list as "registrant".
    cands = []

    def visit(ent, depth):
        nonlocal cc
        roles = ent.get("roles") or []
        handle = (ent.get("handle") or "").upper()
        fn = _vcard_field(ent, "fn")
        kind = _vcard_field(ent, "kind")
        adr = _vcard_field(ent, "adr")
        if adr and not cc:
            lbl = (adr[1] or {}).get("label", "") if isinstance(adr[1], dict) else ""
            cc = {"UNITED STATES": "US", "CHINA": "CN", "HONG KONG": "HK"}.get(lbl.strip().split("\n")[-1].upper())
            if not cc and isinstance(adr[3], list) and adr[3] and isinstance(adr[3][-1], str) and len(adr[3][-1]) == 2:
                cc = adr[3][-1].upper()
        if fn and fn[3]:
            mnt = "MNT" in handle or fn[3].upper().endswith("-MNT") or fn[3].upper().startswith("MNT-") \
                or bool(re.search(r"abuse|\brole\b|\bnoc\b|hostmaster|admin-c|tech-c", fn[3], re.I))
            score = (3 if handle.startswith("ORG-") else 0) + (2 if kind and kind[3] == "org" else 0) \
                + (2 if "registrant" in roles else 0) - (10 if mnt else 0) - (3 if kind and kind[3] == "individual" else 0) - depth
            cands.append((score, fn[3].strip()))
        for sub in ent.get("entities") or []:
            visit(sub, depth + 1)

    for ent in j.get("entities") or []:
        visit(ent, 0)
    cands.sort(key=lambda x: -x[0])
    org = cands[0][1] if cands and cands[0][0] > -5 else ""
    name = j.get("name") or ""
    rem = " ".join(x.get("description", [""])[0] if isinstance(x.get("description"), list) and x.get("description") else ""
                   for x in j.get("remarks") or [])
    out = {"org": (org or rem[:80] or name or "").strip(), "country": (cc or "").upper()[:2], "net": name,
           "src": base.split("/")[2]}
    _range_add(j.get("startAddress"), j.get("endAddress"), out)
    return out


# Owner blocks already looked up, so the next address in the same block needs no registry call. Only for big
# companies that run their own address blocks without handing parts to customers in the registry (Microsoft, Google,
# Apple, ...): for anyone else a different address in the block could belong to a sub-customer, so we still ask.
_REUSE_OWNERS = re.compile(r"microsoft|google|apple inc|akamai|cloudflare|fastly|facebook|meta platforms|amazon", re.I)
_RANGE_FILE = DEV_DIR / "owner_blocks.json"
_RANGES = []  # [(version, lo int, hi int, info)]
_RANGE_LOCK = threading.Lock()


def _ranges():
    if not _RANGES and _RANGE_FILE.is_file():
        try:
            for v, lo, hi, info in json.loads(_RANGE_FILE.read_text(encoding="utf-8")):
                _RANGES.append((v, int(lo), int(hi), info))
        except (ValueError, TypeError):
            _RANGES.clear()
    return _RANGES


def _range_add(start, end, info):
    if not (start and end and info.get("org")) or not _REUSE_OWNERS.search(info["org"]) or CHINA_ORGS.search(info["org"]):
        return
    try:
        lo, hi = ipaddress.ip_address(start), ipaddress.ip_address(end)
    except ValueError:
        return
    if lo.version != hi.version or int(hi) < int(lo):
        return
    with _RANGE_LOCK:
        rs = _ranges()
        if any(v == lo.version and a == int(lo) and b == int(hi) for v, a, b, _ in rs):
            return
        rs.append((lo.version, int(lo), int(hi), dict(info, src=info["src"] + " (same block)")))
        try:
            _RANGE_FILE.write_text(json.dumps([[v, str(a), str(b), i] for v, a, b, i in rs]), encoding="utf-8")
        except OSError:
            pass


def _range_hit(ip):
    """Owner info from an already-looked-up block containing this address (the narrowest one), or None."""
    a = ipaddress.ip_address(ip.split("%")[0])
    n = int(a)
    with _RANGE_LOCK:
        hits = [(hi - lo, info) for v, lo, hi, info in _ranges() if v == a.version and lo <= n <= hi]
    return min(hits, key=lambda h: h[0])[1] if hits else None


def _run(cmd, timeout=30):
    """Run a command (macOS / Linux helpers) -> (returncode, stdout+stderr text). Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def _codesign(path):
    """macOS code signature -> (signer, status, team). Signer = the first Authority, e.g. 'Google LLC' or 'Apple'."""
    rc, out = _run(["codesign", "-dv", "--verbose=2", path], 20)
    if "not signed at all" in out:
        return "NotSigned", "NotSigned", ""
    auth = re.search(r"^Authority=(.+)$", out, re.M)
    team = re.search(r"^TeamIdentifier=(.+)$", out, re.M)
    if rc != 0 or not auth:
        return ("BAD:" + (out.strip().splitlines() or ["unreadable"])[-1][:60]), "Invalid", ""
    who = auth.group(1).strip()
    who = re.sub(r"^(Developer ID Application|Apple Mac OS Application Signing|Apple Development|Mac Developer):\s*", "", who)
    who = re.sub(r"\s*\([A-Z0-9]{10}\)$", "", who)
    if who == "Software Signing":
        who = "Apple"
    vrc, vout = _run(["codesign", "--verify", "--deep", "--strict", path], 60)
    if vrc != 0 and "invalid" in vout.lower():
        return "BAD:" + vout.strip().splitlines()[-1][:60], "Invalid", ""
    return who, "Valid", (team.group(1).strip() if team else "")


def _signatures(exes):
    """Signature check for a batch of executables -> {exe: (signer, status, company)}"""
    if not exes:
        return {}
    if IS_MAC:
        with ThreadPoolExecutor(4) as ex:
            return dict(zip(exes, ex.map(_codesign, exes)))
    if not IS_WIN:
        return {e: ("", "Unknown", "") for e in exes}
    arr = "@(" + ",".join("'" + e.replace("'", "''") + "'" for e in exes) + ")"
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $o=@(); foreach($f in " + arr + "){ try { $s=Get-AuthenticodeSignature -LiteralPath $f -ErrorAction Stop;"
          " $v=(Get-Item -LiteralPath $f -ErrorAction SilentlyContinue).VersionInfo;"
          " $o+=[pscustomobject]@{f=$f; st=[string]$s.Status; sub=$(if($s.SignerCertificate){$s.SignerCertificate.Subject}else{''}); co=$v.CompanyName} }"
          " catch { $o+=[pscustomobject]@{f=$f; st='Missing'; sub=''; co=''} } }; ConvertTo-Json @($o) -Compress")
    out = {}
    j = json.loads(_ps(ps, 180) or "[]")
    for x in j if isinstance(j, list) else [j]:
        sub = x.get("sub") or ""
        m = re.search(r"O=\"?([^,\"]+)", sub) or re.search(r"CN=\"?([^,\"]+)", sub)
        signer = m.group(1).strip() if m else ""
        st = x.get("st") or ""
        if st == "Valid":
            val = signer or "signed"
        elif st == "NotSigned":
            val = "NotSigned"
        else:
            val = "BAD:" + st
        out[x["f"]] = (val, st, (x.get("co") or "").strip())
    return out


def _enricher():
    while True:
        _ENRICH_EV.wait(30)
        _ENRICH_EV.clear()
        # signatures first: fast, local
        if _PENDING_EXES:
            batch = list(_PENDING_EXES)[:40]
            try:
                res = _signatures(batch)
            except Exception:  # noqa: BLE001
                res = {}
            with _DB_LOCK, _db() as c:
                for e in batch:
                    signer, st, co = res.get(e, ("", "Unknown", ""))
                    c.execute("INSERT OR REPLACE INTO sigs VALUES(?,?,?,?,?)", (e, signer, st, co, time.time()))
                    _PENDING_EXES.discard(e)
                    if signer == "NotSigned" or signer.startswith("BAD:"):
                        c.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                                  (time.time(), 1 if signer == "NotSigned" else 2, "unsigned",
                                   f"{Path(e).name} talks to the network and is {'NOT signed' if signer == 'NotSigned' else 'signed INVALIDLY'}", e))
            if _PENDING_EXES:
                _ENRICH_EV.set()
        # IP owners: a registry call + reverse DNS each, 4 at a time
        batch = list(_PENDING_IPS)[:24]

        def look(ip):
            try:
                info = _range_hit(ip) or _rdap(ip)
            except Exception as e:  # noqa: BLE001
                info = {"org": "", "country": "", "net": "", "src": "error: " + str(e)[:60]}
            try:
                rd = socket.gethostbyaddr(ip.split("%")[0])[0]
            except Exception:  # noqa: BLE001
                rd = ""
            return ip, info, rd

        with ThreadPoolExecutor(4) as ex:
            looked = list(ex.map(look, batch))
        for ip, info, rd in looked:
            with _DB_LOCK, _db() as c:
                c.execute("INSERT OR REPLACE INTO ipinfo VALUES(?,?,?,?,?,?,?)",
                          (ip, info.get("org", ""), info.get("country", ""), info.get("net", ""), rd, info.get("src", ""), time.time()))
                _, lvl, why = classify(info)
                pr = c.execute("SELECT DISTINCT proc, exe FROM conns WHERE rip=?", (ip,)).fetchall() if lvl >= 2 else []
                if pr:  # only this PC's own connections; addresses looked up for hotspot gadgets are reported on the VACUUM tab
                    own = all(_own(r[1]) for r in pr)
                    c.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                              (time.time(), 1 if own else 2, "watch-country",
                               f"{', '.join(r[0] for r in pr) or '?'} connected to {ip} - {'; '.join(why)}"
                               + (" (expected: Influence Watch news feeds)" if own else ""), ip))
            _PENDING_IPS.discard(ip)
        time.sleep(1.0)
        if _PENDING_IPS:
            _ENRICH_EV.set()


# ----------------------------------------------------------------------------------------------- helpers
def _ps(script, timeout=120):
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                       capture_output=True, timeout=timeout, creationflags=NO_WINDOW if sys.platform == "win32" else 0)
    return r.stdout.decode("utf-8", "replace").lstrip("\ufeff")


def _maps(c):
    info = {r["ip"]: dict(r) for r in c.execute("SELECT * FROM ipinfo")}
    sigs = {r["exe"]: dict(r) for r in c.execute("SELECT * FROM sigs")}
    names = {}
    for r in c.execute("SELECT ip, name FROM dns ORDER BY last DESC"):
        names.setdefault(r["ip"], r["name"])
    return info, sigs, names


def _own(exe):
    """This suite itself: Influence Watch deliberately downloads Chinese / Russian / Iranian state-media feeds."""
    e = (exe or "").lower()
    return "marketterminal" in e or (not getattr(sys, "frozen", False) and e == sys.executable.lower())


def _own_adjust(exe, dests):
    if _own(exe):
        for d in dests:
            if d["lvl"] >= 2:
                d["lvl"] = 1
                d["why"] = list(d["why"]) + ["Expected: this is Market Terminal / Influence Watch fetching state-media news feeds"]


def _dest(ip, info, names):
    i = info.get(ip) or {}
    kind = _ip_kind(ip)
    host = names.get(ip) or i.get("rdns") or ""
    cat, lvl, why = classify(i)
    if kind == "lan":
        cat = "Local network"
    elif kind in ("link-local", "multicast", "special"):
        cat = "Local / broadcast"
    elif not i:
        cat = "looking up..."
    if host and not cat or cat == "Other":
        c2 = _org_cat(host)
        cat = c2 or cat
    return {"ip": ip, "host": host, "org": i.get("org") or "", "country": i.get("country") or "", "cat": cat, "lvl": lvl, "why": why,
            "kind": kind}


# ----------------------------------------------------------------------------------------------- API
def status(q=None):
    start_collector()
    with _db() as c:
        first = c.execute("SELECT MIN(ts) FROM traffic").fetchone()[0]
        n_dest = c.execute("SELECT COUNT(DISTINCT rip) FROM conns").fetchone()[0]
        n_prog = c.execute("SELECT COUNT(DISTINCT exe) FROM conns").fetchone()[0]
        # coverage: share of the last 24h with samples (gaps = PC off or app closed)
        ts = [r[0] for r in c.execute("SELECT ts FROM traffic WHERE ts > ? ORDER BY ts", (time.time() - 86400,))]
        r15 = _rolling15(c)
    cov = 0.0
    for a, b in zip(ts, ts[1:]):
        cov += min(b - a, SAMPLE_SECS * 3)
    up, down = _STATE["rate"]
    return {"running": _STATE["started"] is not None, "since": first, "samples": _STATE["samples"], "last": _STATE["last"],
            "err": _STATE["err"], "dests": n_dest, "programs": n_prog, "cov24": round(min(100.0, cov / 864), 1),
            "pending_ips": len(_PENDING_IPS), "pending_exes": len(_PENDING_EXES), "up_bps": up, "down_bps": down, "r15": r15,
            "always_on": _startup_link().is_file(), "admin": _is_admin(), "interval": SAMPLE_SECS,
            "os": sys.platform, "partial": _STATE["partial"], "log": str(DB_FILE), "can_block": IS_WIN}


def live(q=None):
    start_collector()
    with _db() as c:
        info, sigs, names = _maps(c)
    procs = {}
    for x in _STATE["live"]:
        key = x["exe"] or x["proc"]
        p = procs.setdefault(key, {"proc": x["proc"], "exe": x["exe"], "user": x["user"], "pids": set(), "conns": [], "lvl": 0})
        p["pids"].add(x["pid"])
        d = _dest(x["rip"], info, names)
        d.update(rport=x["rport"], proto=x["proto"], status=x["status"])
        p["conns"].append(d)
    out = []
    for p in procs.values():
        s = sigs.get(p["exe"]) or {}
        signer = s.get("signer") or ""
        _, plvl, pwhy = classify({}, p["proc"], p["exe"], signer)
        _own_adjust(p["exe"], p["conns"])
        if _own(p["exe"]) and pwhy == ["Program is not digitally signed"]:
            plvl, pwhy = 0, ["Your own Market Terminal build (self-built, so unsigned)"]
        lvl = max([plvl] + [d["lvl"] for d in p["conns"]])
        out.append({"proc": p["proc"], "exe": p["exe"], "user": p["user"], "pids": sorted(p["pids"]), "signer": signer,
                    "company": s.get("company") or "", "lvl": lvl, "why": pwhy, "n": len(p["conns"]),
                    "conns": sorted(p["conns"], key=lambda d: (-d["lvl"], d["cat"], d["ip"]))})
    out.sort(key=lambda p: (-p["lvl"], -p["n"]))
    up, down = _STATE["rate"]
    return {"procs": out, "up_bps": up, "down_bps": down, "t": _STATE["last"]}


def history(q):
    """Every destination ever contacted (within `hours`), grouped by program."""
    start_collector()
    hours = float(q.get("hours") or 24 * 7)
    since = time.time() - hours * 3600
    with _db() as c:
        info, sigs, names = _maps(c)
        rows = [dict(r) for r in c.execute("SELECT * FROM conns WHERE last >= ? ORDER BY last DESC", (since,))]
    by = {}
    for r in rows:
        d = _dest(r["rip"], info, names)
        k = r["exe"] or r["proc"]
        p = by.setdefault(k, {"proc": r["proc"], "exe": r["exe"], "dests": {}, "first": r["first"], "last": r["last"], "samples": 0})
        p["first"], p["last"] = min(p["first"], r["first"]), max(p["last"], r["last"])
        p["samples"] += r["samples"]
        e = p["dests"].setdefault(r["rip"], {**d, "ports": set(), "first": r["first"], "last": r["last"], "samples": 0})
        e["ports"].add(f"{r['proto']}/{r['rport']}")
        e["first"], e["last"] = min(e["first"], r["first"]), max(e["last"], r["last"])
        e["samples"] += r["samples"]
    progs, orgs, countries, cats = [], {}, {}, {}
    for p in by.values():
        s = sigs.get(p["exe"]) or {}
        _, plvl, pwhy = classify({}, p["proc"], p["exe"], s.get("signer") or "")
        dl = list(p["dests"].values())
        _own_adjust(p["exe"], dl)
        if _own(p["exe"]) and plvl == 1 and pwhy == ["Program is not digitally signed"]:
            plvl, pwhy = 0, ["Your own Market Terminal build (self-built, so unsigned)"]
        for e in dl:
            e["ports"] = sorted(e["ports"])
            if e["kind"] == "public":
                ok = e["org"] or "(unknown)"
                o = orgs.setdefault(ok, {"org": ok, "country": e["country"], "cat": e["cat"], "lvl": e["lvl"], "ips": set(), "progs": set(), "samples": 0})
                o["ips"].add(e["ip"]); o["progs"].add(p["proc"]); o["samples"] += e["samples"]
                o["lvl"] = max(o["lvl"], e["lvl"])
                countries[e["country"] or "??"] = countries.get(e["country"] or "??", 0) + e["samples"]
            cats[e["cat"] or "?"] = cats.get(e["cat"] or "?", 0) + e["samples"]
        dl.sort(key=lambda e: (-e["lvl"], -e["samples"]))
        progs.append({"proc": p["proc"], "exe": p["exe"], "signer": s.get("signer") or "", "company": s.get("company") or "",
                      "lvl": max([plvl] + [e["lvl"] for e in dl]), "why": pwhy, "first": p["first"], "last": p["last"],
                      "samples": p["samples"], "ndest": len(dl), "dests": dl[:300]})
    progs.sort(key=lambda p: (-p["lvl"], -p["samples"]))
    ol = sorted(({**o, "ips": len(o["ips"]), "progs": sorted(o["progs"])} for o in orgs.values()), key=lambda o: (-o["lvl"], -o["samples"]))
    return {"hours": hours, "programs": progs, "orgs": ol[:400],
            "countries": sorted(countries.items(), key=lambda x: -x[1]), "cats": sorted(cats.items(), key=lambda x: -x[1]),
            "flagged": sum(1 for o in ol if o["lvl"] >= 2)}


def _windows15(c, n):
    """Average upload/download rate (bytes/s) for each of the last `n` 15-minute clock windows, oldest first.
    Rate = bytes / time actually logged in the window, so a half-logged window isn't shown as half the speed."""
    w = 900
    since = (int(time.time()) // w - (n - 1)) * w
    rows = c.execute(f"SELECT CAST(ts/{w} AS INT)*{w} AS b, SUM(sent), SUM(recv), COUNT(*) FROM traffic "
                     "WHERE ts >= ? GROUP BY b ORDER BY b", (since,)).fetchall()
    have = {r[0]: r for r in rows}
    out = []
    for i in range(n):
        b = since + i * w
        r = have.get(b)
        secs = min(w, r[3] * SAMPLE_SECS) if r else 0
        out.append({"t": b, "up": r[1] / secs if r and secs else None, "down": r[2] / secs if r and secs else None,
                    "cov": round(100 * secs / w) if r else 0})
    return out


def _rolling15(c):
    """Last 15 minutes vs the 15 minutes before that (rolling, not clock-aligned)."""
    now = time.time()
    res = []
    for a, b in ((now - 900, now), (now - 1800, now - 900)):
        r = c.execute("SELECT SUM(sent), SUM(recv), COUNT(*) FROM traffic WHERE ts > ? AND ts <= ?", (a, b)).fetchone()
        secs = min(900, (r[2] or 0) * SAMPLE_SECS)
        res.append((r[0] / secs, r[1] / secs) if secs and r[0] is not None else (None, None))
    pct = lambda x, y: ((x - y) / y * 100) if x is not None and y else None  # noqa: E731
    (u, d), (pu, pd) = res
    return {"up": u, "down": d, "prev_up": pu, "prev_down": pd, "up_chg": pct(u, pu), "down_chg": pct(d, pd)}


def traffic(q):
    # either the last `hours`, or a zoomed-in range `start`..`end` (unix seconds)
    end = float(q.get("end") or time.time())
    since = float(q["start"]) if q.get("start") else end - float(q.get("hours") or 24) * 3600
    hours = max(1 / 60, (end - since) / 3600)
    # finer buckets for shorter spans: raw samples up to 1h, then 1 min / 5 min / 1 h
    bucket = SAMPLE_SECS if hours <= 1 else 60 if hours <= 6 else 300 if hours <= 48 else 3600
    with _db() as c:
        w15 = _windows15(c, 24)
        rows = c.execute(f"SELECT CAST(ts/{bucket} AS INT)*{bucket} AS b, SUM(sent), SUM(recv), AVG(nconn), MAX(nproc) "
                         "FROM traffic WHERE ts >= ? AND ts <= ? GROUP BY b ORDER BY b", (since, end)).fetchall()
        tot = c.execute("SELECT SUM(sent), SUM(recv) FROM traffic WHERE ts >= ? AND ts <= ?", (since, end)).fetchone()
        # when nobody is using the PC: night-time uploads stand out
        hourly = c.execute("SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INT) h, SUM(sent), SUM(recv), COUNT(*) "
                           "FROM traffic WHERE ts >= ? GROUP BY h", (time.time() - 30 * 86400,)).fetchall()
    hmap = {r[0]: r for r in hourly}
    return {"bucket": bucket, "start": since, "end": end, "t": [r[0] for r in rows], "sent": [r[1] for r in rows], "recv": [r[2] for r in rows],
            "nconn": [round(r[3] or 0, 1) for r in rows], "tot_sent": tot[0] or 0, "tot_recv": tot[1] or 0, "w15": w15,
            "hourly": [{"h": h, "sent": (hmap[h][1] / max(1, hmap[h][3]) * (3600 / SAMPLE_SECS)) if h in hmap else None,
                        "recv": (hmap[h][2] / max(1, hmap[h][3]) * (3600 / SAMPLE_SECS)) if h in hmap else None} for h in range(24)]}


def dns(q):
    hours = float(q.get("hours") or 24 * 7)
    with _db() as c:
        info = {r["ip"]: dict(r) for r in c.execute("SELECT * FROM ipinfo")}
        rows = [dict(r) for r in c.execute("SELECT name, GROUP_CONCAT(ip, ' ') ips, MIN(first) first, MAX(last) last FROM dns "
                                           "WHERE last >= ? GROUP BY name ORDER BY last DESC LIMIT 3000", (time.time() - hours * 3600,))]
    for r in rows:
        ips = (r["ips"] or "").split()
        r["ips"] = ips[:6]
        cat = _org_cat(r["name"])
        lvl = 2 if re.search(r"\.cn$|\.com\.cn$|baidu|qq\.com|tencent|aliyun|alibaba|alicdn|taobao|huawei|xiaomi|bytedance|"
                             r"\bmele\b|mele(pc|tech)|360\.cn|qihoo|kingsoft|bdstatic|byteimg|pstatp", r["name"], re.I) else 0
        for ip in ips:
            _, l2, _ = classify(info.get(ip))
            lvl = max(lvl, l2)
        r["cat"], r["lvl"] = cat or "", lvl
        dom = r["name"].split(".")
        r["base"] = ".".join(dom[-3:] if len(dom) > 2 and len(dom[-2]) <= 3 and len(dom[-1]) == 2 else dom[-2:])
    with _db() as c:
        t = c.execute("SELECT v FROM meta WHERE k='dns_log_t'").fetchone()
    return {"rows": rows, "flagged": sum(1 for r in rows if r["lvl"] >= 2), "log_t": float(t[0]) if t else 0}


def events(q):
    with _db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 500")]
    return {"rows": rows}


def listening(q=None):
    with _db() as c:
        sigs = {r["exe"]: dict(r) for r in c.execute("SELECT * FROM sigs")}
        rows = [dict(r) for r in c.execute("SELECT * FROM listen ORDER BY last DESC")]
    now = time.time()
    for r in rows:
        r["signer"] = (sigs.get(r["exe"]) or {}).get("signer", "")
        r["active"] = now - r["last"] < 120
        r["exposed"] = r["lip"] in ("0.0.0.0", "::")
    return {"rows": rows}


# ----------------------------------------------------------------------------------------------- audit
def _firmware():
    out = {"acpi": [], "wpbt": False, "wpbbin": os.path.isfile(r"C:\Windows\System32\wpbbin.exe")}
    try:
        k32 = ctypes.windll.kernel32
        sig = int.from_bytes(b"ACPI", "big")
        n = k32.EnumSystemFirmwareTables(sig, None, 0)
        buf = (ctypes.c_char * n)()
        k32.EnumSystemFirmwareTables(sig, buf, n)
        raw = bytes(buf)
        out["acpi"] = [raw[i:i + 4].decode("ascii", "replace") for i in range(0, len(raw), 4)]
        out["wpbt"] = "WPBT" in out["acpi"]
    except Exception as e:  # noqa: BLE001
        out["err"] = str(e)
    return out


AUDIT_PS = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$ErrorActionPreference='SilentlyContinue'
function Sig($p){ if(-not $p -or -not (Test-Path -LiteralPath $p)){ return @{st='Missing';by=''} }
  $s=Get-AuthenticodeSignature -LiteralPath $p; $sub=''; if($s.SignerCertificate){$sub=$s.SignerCertificate.Subject}
  return @{st=[string]$s.Status; by=$sub} }
function ExePath($cmd){ if(-not $cmd){return $null}; $c=[Environment]::ExpandEnvironmentVariables($cmd.Trim())
  if($c.StartsWith('"')){ return $c.Split('"')[1] }; $m=[regex]::Match($c,'^(.+?\.(exe|dll|sys|com|bat|cmd|ps1))',1); if($m.Success){return $m.Groups[1].Value}; return $c.Split(' ')[0] }
$o=[ordered]@{}
$cs=Get-CimInstance Win32_ComputerSystem; $b=Get-CimInstance Win32_BIOS; $bb=Get-CimInstance Win32_BaseBoard; $os=Get-CimInstance Win32_OperatingSystem
$o.system=[ordered]@{maker=$cs.Manufacturer; model=$cs.Model; family=$cs.SystemFamily; board=$bb.Manufacturer+' '+$bb.Product;
  bios=$b.Manufacturer+' '+$b.SMBIOSBIOSVersion; bios_date=[string]$b.ReleaseDate; serial=$b.SerialNumber; os=$os.Caption+' '+$os.Version;
  installed=[string]$os.InstallDate; cpu=(Get-CimInstance Win32_Processor).Name;
  secureboot=(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\SecureBoot\State').UEFISecureBootEnabled;
  wpbt_disabled=(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager').DisableWpbtExecution;
  telemetry=(Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection').AllowTelemetry}
$o.programs=@(Get-ItemProperty HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*,HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*,HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\* |
  ?{$_.DisplayName -and -not $_.SystemComponent} | %{ [ordered]@{name=$_.DisplayName; pub=$_.Publisher; ver=$_.DisplayVersion; date=$_.InstallDate; loc=$_.InstallLocation} })
$o.appx=@(Get-AppxPackage | ?{ -not $_.IsFramework -and $_.SignatureKind -ne 'System' -and $_.Publisher -notmatch 'Microsoft' } | %{ [ordered]@{name=$_.Name; pub=$_.Publisher; ver=[string]$_.Version} })
$o.services=@(Get-CimInstance Win32_Service | ?{ $_.PathName -and $_.PathName -notmatch 'svchost\.exe' } | %{ $p=ExePath $_.PathName; $s=Sig $p
  [ordered]@{name=$_.Name; disp=$_.DisplayName; state=$_.State; start=$_.StartMode; path=$p; acct=$_.StartName; st=$s.st; by=$s.by} })
$o.tasks=@(Get-ScheduledTask | ?{ $_.TaskPath -notlike '\Microsoft\*' } | %{ $a=($_.Actions | ?{$_.Execute} | Select -First 1); $p=$null; if($a){$p=ExePath $a.Execute}; $s=Sig $p
  [ordered]@{name=$_.TaskName; path=$_.TaskPath; state=[string]$_.State; author=$_.Author; exec=$(if($a){$a.Execute+' '+$a.Arguments}else{''}); file=$p; st=$s.st; by=$s.by;
    user=$_.Principal.UserId; trig=(@($_.Triggers | %{ $_.CimClass.CimClassName -replace 'MSFT_Task','' -replace 'Trigger','' }) -join ',')} })
$runs=@('HKLM:\Software\Microsoft\Windows\CurrentVersion\Run','HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run',
  'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run','HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce')
$st=@(); foreach($k in $runs){ $i=Get-ItemProperty $k; if($i){ $i.PSObject.Properties | ?{$_.Name -notlike 'PS*'} | %{ $p=ExePath ([string]$_.Value); $s=Sig $p
  $st+=[ordered]@{where=$k; name=$_.Name; cmd=[string]$_.Value; file=$p; st=$s.st; by=$s.by} } } }
foreach($d in @([Environment]::GetFolderPath('Startup'),[Environment]::GetFolderPath('CommonStartup'))){ Get-ChildItem -LiteralPath $d -File | ?{$_.Name -ne 'desktop.ini'} | %{
  $st+=[ordered]@{where=$d; name=$_.Name; cmd=$_.FullName; file=$_.FullName; st=''; by=''} } }
$o.startup=$st
$o.drivers=@(Get-CimInstance Win32_PnPSignedDriver | ?{ $_.DriverProviderName -and $_.DriverProviderName -notmatch '^Microsoft' } | %{
  [ordered]@{dev=$_.DeviceName; prov=$_.DriverProviderName; mfr=$_.Manufacturer; ver=$_.DriverVersion; date=[string]$_.DriverDate; signer=$_.Signer; inf=$_.InfName; cls=$_.DeviceClass} })
$o.kdrivers=@(Get-CimInstance Win32_SystemDriver | ?{ $_.PathName -and $_.PathName -notmatch '\\Windows\\System32\\(drivers|DriverStore)\\' } | %{ $p=ExePath ($_.PathName -replace '^\\\?\?\\',''); $s=Sig $p
  [ordered]@{name=$_.Name; disp=$_.DisplayName; state=$_.State; path=$p; st=$s.st; by=$s.by} })
$o.roots=@(Get-ChildItem Cert:\LocalMachine\Root | %{ [ordered]@{sub=$_.Subject; iss=$_.Issuer; from=$_.NotBefore.ToString('yyyy-MM-dd'); to=$_.NotAfter.ToString('yyyy-MM-dd'); th=$_.Thumbprint} })
$o.uroots=@(Get-ChildItem Cert:\CurrentUser\Root | ?{ -not (Test-Path ('Cert:\LocalMachine\Root\'+$_.Thumbprint)) } | %{ [ordered]@{sub=$_.Subject; from=$_.NotBefore.ToString('yyyy-MM-dd'); th=$_.Thumbprint} })
$o.hosts=@(Get-Content "$env:SystemRoot\System32\drivers\etc\hosts" | ?{ $_.Trim() -and -not $_.Trim().StartsWith('#') })
$is=Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$o.proxy=[ordered]@{enabled=$is.ProxyEnable; server=$is.ProxyServer; pac=$is.AutoConfigURL; winhttp=((netsh winhttp show proxy) -join ' ').Trim()}
$o.dns=@(Get-DnsClientServerAddress | ?{ $_.ServerAddresses } | %{ [ordered]@{ifc=$_.InterfaceAlias; fam=$(if($_.AddressFamily -eq 2){'IPv4'}else{'IPv6'}); srv=@($_.ServerAddresses)} })
$o.users=@(Get-LocalUser | %{ [ordered]@{name=$_.Name; enabled=$_.Enabled; last=[string]$_.LastLogon; desc=$_.Description; created=''} })
$o.admins=@(Get-LocalGroupMember -Group 'Administrators' | %{ [string]$_.Name })
$o.remote=[ordered]@{rdp=((Get-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server').fDenyTSConnections -eq 0);
  winrm=[string](Get-Service WinRM).Status; sshd=[string](Get-Service sshd).Status; remreg=[string](Get-Service RemoteRegistry).Status}
$mp=Get-MpComputerStatus; $o.defender=[ordered]@{on=$mp.AntivirusEnabled; rt=$mp.RealTimeProtectionEnabled; sig=[string]$mp.AntivirusSignatureLastUpdated; tamper=$mp.IsTamperProtected}
$o.fwrules=@(Get-NetFirewallRule -DisplayName 'DeviceWatch block*' | %{ $f=($_ | Get-NetFirewallApplicationFilter); [ordered]@{name=$_.DisplayName; prog=$f.Program; enabled=[string]$_.Enabled} })
$o.fwprofiles=@(Get-NetFirewallProfile | %{ [ordered]@{name=$_.Name; on=[string]$_.Enabled; out=[string]$_.DefaultOutboundAction} })
$pref=Get-MpPreference; $lsa=Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Lsa'; $pol=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
$hf=(Get-HotFix | ?{ $_.InstalledOn } | Sort-Object InstalledOn -Descending | Select-Object -First 1).InstalledOn
$o.harden=[ordered]@{
  hvci=(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity').Enabled;
  lsa=$lsa.RunAsPPL; uac=$pol.EnableLUA; uac_prompt=$pol.ConsentPromptBehaviorAdmin;
  cfa=$pref.EnableControlledFolderAccess; pua=$pref.PUAProtection; netprot=$pref.EnableNetworkProtection; cloud=$pref.MAPSReporting;
  bitlocker=(New-Object -ComObject Shell.Application).NameSpace($env:SystemDrive).Self.ExtendedProperty('System.Volume.BitLockerProtection');
  update=$(if($hf){$hf.ToString('yyyy-MM-dd')}else{''});
  autoplay_off=(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\AutoplayHandlers').DisableAutoplay;
  guest=(Get-LocalUser Guest).Enabled; me=[Security.Principal.WindowsIdentity]::GetCurrent().Name;
  shares=@(Get-CimInstance Win32_Share | ?{ $_.Type -eq 0 } | %{ $_.Name+' = '+$_.Path });
  nets=@(Get-NetConnectionProfile | %{ $_.Name+'|'+[string]$_.NetworkCategory }) }
ConvertTo-Json $o -Depth 5 -Compress
"""

_AUDIT = {"t": 0, "data": None}
_AUDIT_LOCK = threading.Lock()


def _signer_name(sub):
    m = re.search(r"O=\"?([^,\"]+)", sub or "") or re.search(r"CN=\"?([^,\"]+)", sub or "")
    return m.group(1).strip() if m else ""


def _file_verdict(st, by, path=""):
    name = _signer_name(by)
    p = (path or "").lower()
    if st == "Valid" and SAFE_SIGNERS.search(name):
        return 0, name, ""
    if st == "Valid":
        return 1, name, "Signed by a non-Microsoft publisher - check you recognise it"
    if st == "NotSigned":
        return 1 if "\\windows\\" not in p else 1, "", "Not digitally signed"
    if st in ("Missing", ""):
        return 0, "", "File not found (left-over entry)" if st == "Missing" else ""
    return 2, name, "Signature " + st


def audit(q):
    with _AUDIT_LOCK:
        if _AUDIT["data"] and time.time() - _AUDIT["t"] < 600 and not q.get("refresh"):
            return _AUDIT["data"]
        if IS_MAC:
            res = _audit_mac()
        elif IS_WIN:
            raw = json.loads(_ps(AUDIT_PS, 240) or "{}")
            res = _audit_verdicts(raw, _firmware())
        else:
            res = {"t": time.time(), "checks": [{"group": "System", "title": "Audit", "lvl": 1, "items": [],
                                                 "detail": "The audit covers Windows and macOS. Connection logging works here too."}],
                   "system": {"maker": sys.platform, "model": "", "bios": "", "os": sys.platform}, "blocks": [],
                   "score": {"alert": 0, "review": 1, "ok": 0}}
        _AUDIT.update(t=time.time(), data=res)
        return res


def _arr(x):
    return x if isinstance(x, list) else ([] if x is None else [x])


def _harden_checks(h, admins, add):
    """Windows' own protections: is each one switched on? `fix` names the Settings page the app can open for it."""
    if not isinstance(h, dict):
        return
    g = "Hardening"
    on = h.get("hvci") == 1
    add(g, "Memory integrity (core isolation)", 0 if on else 1,
        "ON: drivers are checked before they can run in the core of Windows, which blocks the usual way malware gets below antivirus." if on else
        "OFF. Turn on Memory integrity so only checked drivers can run in the core of Windows. Needs a restart; an old incompatible driver can stop it turning on.",
        fix="coreisolation")
    bl = h.get("bitlocker")
    bl_txt = {1: "ON: the disk is encrypted, so someone who takes the PC or its drive cannot read your files.",
              2: "OFF. Anyone who removes the drive, or starts the PC from a USB stick, can read every file without your password.",
              3: "Encryption is in progress.", 5: "SUSPENDED: the disk is encrypted but the key is stored unprotected until protection resumes.",
              4: "The disk is being DECRYPTED."}.get(bl, "Windows does not report the disk as protected (it may be encrypted but waiting for you to "
                                                             "finish turning encryption on). Open the setting to check.")
    add(g, "Disk encryption (BitLocker)", 0 if bl in (1, 3) else 1, bl_txt + ("" if bl in (1, 3) else " Save the recovery key somewhere off this PC when you turn it on."),
        fix="encryption")
    cfa = h.get("cfa")
    add(g, "Ransomware protection (controlled folder access)", 0 if cfa == 1 else 1,
        "ON: only trusted programs can change files in Documents, Pictures and Desktop." if cfa == 1 else
        ("In AUDIT mode: Windows logs what it would have blocked but blocks nothing. " if cfa == 2 else "OFF. ") +
        "When on, unknown programs cannot change or encrypt files in Documents, Pictures and Desktop. You will need to allow "
        "MarketTerminal.exe (and any other tool that saves there) under \"Allow an app through\", or its saves will be blocked.",
        fix="ransomware")
    pua = h.get("pua") == 1
    add(g, "Blocking unwanted apps and bad sites", 0 if pua and h.get("netprot") == 1 else 1,
        f"Potentially-unwanted-app blocking is {'on' if pua else 'OFF'}; network protection (blocks connections to known-malicious sites from any program) is "
        f"{'on' if h.get('netprot') == 1 else 'audit-only' if h.get('netprot') == 2 else 'OFF'}; cloud-delivered protection is {'on' if h.get('cloud') else 'OFF'}.",
        fix="appbrowser")
    lsa = h.get("lsa") in (1, 2)
    add(g, "Password store protection (LSA)", 0 if lsa else 1,
        "ON: other programs cannot read sign-in secrets out of Windows' memory." if lsa else
        "OFF. Turn on Local Security Authority protection (Device security > Core isolation) so password-stealing tools cannot read sign-in secrets from memory.",
        fix="coreisolation")
    uac = h.get("uac") == 1 and h.get("uac_prompt") != 0
    add(g, "Permission prompts (UAC)", 0 if uac else 2,
        "ON: programs must ask before making system-wide changes." if uac else
        "OFF or set to never notify: any program can make system-wide changes silently. Move the slider back to the default.", fix="uac")
    me = (h.get("me") or "").lower()
    is_admin = any((x or "").lower() == me for x in admins)
    add(g, "Everyday account", 1 if is_admin else 0,
        f"You are signed in as {h.get('me')}, which is an administrator. A program you run is one click (the Yes on a permission prompt) from full control. "
        "Safer: create a separate administrator account, then change this one to Standard, so system changes need that account's password."
        if is_admin else f"You are signed in as {h.get('me')}, a standard account: system changes need an administrator password.", fix="accounts")
    up = h.get("update") or ""
    try:
        age = (time.time() - time.mktime(time.strptime(up, "%Y-%m-%d"))) / 86400
    except ValueError:
        age = None
    add(g, "Windows updates", 0 if age is not None and age <= 45 else 1,
        f"Last update installed {up} ({int(age)} days ago)." + ("" if age <= 45 else " Security fixes come out monthly; check for updates.")
        if age is not None else "Could not read when the last update was installed. Check Windows Update.", fix="update")
    nets = [x.split("|") for x in _arr(h.get("nets")) if "|" in x]
    priv = [n for n, c in nets if c == "Private"]
    shares = _arr(h.get("shares"))
    add(g, "Network visibility and shared folders", 1 if shares or (priv and h.get("guest")) else 0,
        "; ".join(f"\"{n}\" is set as {c}" for n, c in nets) + ". Public = this PC hides from other devices on that network (right for any network you do not fully control). "
        + (f"Shared folders: {', '.join(shares)}. Devices on a Private network can try to open these." if shares else "No folders are shared.")
        + (" The Guest account is ENABLED; turn it off." if h.get("guest") else ""), fix="sharing")
    ap = h.get("autoplay_off") == 1
    add(g, "AutoPlay for USB sticks and discs", 0 if ap else 1,
        "OFF: nothing starts by itself when a USB stick is plugged in." if ap else
        "ON. Windows can open or offer to run things as soon as a USB stick is plugged in. Turn AutoPlay off.", fix="autoplay")


def _audit_verdicts(a, fw):
    checks = []

    def add(group, title, lvl, detail, items=None, fix=None):
        checks.append({"group": group, "title": title, "lvl": lvl, "detail": detail, "items": items or [], "fix": fix})

    sysi = a.get("system") or {}
    # --- firmware
    if fw.get("wpbt") or fw.get("wpbbin"):
        add("Firmware", "BIOS-injected software (WPBT)", 2,
            "The firmware carries a Windows Platform Binary Table: Windows runs a program supplied by the BIOS at every boot"
            + (" and wpbbin.exe is present in System32." if fw.get("wpbbin") else ".")
            + " This is how some OEMs install software that survives a clean Windows install. "
            + ("Execution is currently DISABLED by registry policy." if sysi.get("wpbt_disabled") == 1 else
               "To stop it: set HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\DisableWpbtExecution = 1 (DWORD), then reboot."))
    else:
        add("Firmware", "BIOS-injected software (WPBT)", 0,
            "No WPBT table in the firmware and no wpbbin.exe - the BIOS is not pushing a program into Windows.")
    add("Firmware", "ACPI tables", 0, "Firmware tables exposed to Windows: " + " ".join(fw.get("acpi") or []) +
        ". MSDM = embedded Windows licence key (normal). TPM2 = TPM chip. No WPBT is the important part.")
    sb = sysi.get("secureboot")
    add("Firmware", "Secure Boot", 0 if sb == 1 else 1,
        "Secure Boot is ON: only signed boot loaders can start." if sb == 1 else "Secure Boot is OFF or unknown - enable it in the BIOS setup.")
    add("Firmware", "Hardware identity", 0,
        f"{sysi.get('maker')} {sysi.get('model')} (board {sysi.get('board')}), BIOS {sysi.get('bios')} {str(sysi.get('bios_date') or '')[:10]}, "
        f"CPU {sysi.get('cpu')}. OS: {sysi.get('os')}. "
        "Note: BIOS-level code and the Intel Management Engine run below Windows; only a router-level traffic log can see them.")

    # --- software
    progs = _arr(a.get("programs"))
    odd = [p for p in progs if not SAFE_SIGNERS.search(f"{p.get('pub') or ''} {p.get('name') or ''}")]
    chinese = [p for p in progs if CHINA_ORGS.search(f"{p.get('name')} {p.get('pub')}") or re.search(r"\bmele\b", f"{p.get('name')} {p.get('pub')}", re.I)]
    add("Software", "Installed programs", 2 if chinese else (1 if odd else 0),
        f"{len(progs)} installed programs, {len(odd)} from publishers outside the common list"
        + (f"; {len(chinese)} look Chinese / MeLE-made." if chinese else "."),
        [{"lvl": 2 if p in chinese else (1 if p in odd else 0), "a": p.get("name"), "b": p.get("pub") or "(no publisher)",
          "c": p.get("ver") or "", "d": p.get("date") or ""} for p in sorted(progs, key=lambda p: (p not in chinese, p not in odd, p.get("name") or ""))])
    appx = _arr(a.get("appx"))
    if appx:
        add("Software", "Store / packaged apps (non-Microsoft)", 1 if appx else 0, f"{len(appx)} packaged apps not published by Microsoft.",
            [{"lvl": 1, "a": x.get("name"), "b": x.get("pub"), "c": x.get("ver")} for x in appx])
    rt = [p for p in progs if REMOTE_TOOLS.search(p.get("name") or "")]
    svc = _arr(a.get("services"))
    rt += [{"name": s.get("disp"), "pub": s.get("path")} for s in svc if REMOTE_TOOLS.search(f"{s.get('disp')} {s.get('path')}")]
    add("Remote access", "Remote-control software", 2 if rt else 0,
        ("Remote-control tools found: " + ", ".join(x.get("name") or "" for x in rt)) if rt else "No AnyDesk / TeamViewer / ToDesk / Sunlogin / RustDesk-type tools installed.")
    rem = a.get("remote") or {}
    on = [k for k, v in (("Remote Desktop", rem.get("rdp")), ("WinRM", rem.get("winrm") == "Running"), ("OpenSSH server", rem.get("sshd") == "Running"),
                         ("Remote Registry", rem.get("remreg") == "Running")) if v]
    add("Remote access", "Built-in remote access", 1 if on else 0, ("Enabled: " + ", ".join(on) + ". Turn off anything you don't use.") if on else
        "Remote Desktop, WinRM, OpenSSH server and Remote Registry are all off.")

    # --- autostarts
    sv = []
    for s in svc:
        lvl, who, why = _file_verdict(s.get("st"), s.get("by"), s.get("path"))
        if lvl or (who and not re.search("microsoft", who, re.I)):
            sv.append({"lvl": lvl, "a": f"{s.get('disp')} ({s.get('name')})", "b": who or why, "c": f"{s.get('state')} / {s.get('start')}", "d": s.get("path")})
    add("Autostart", "Services not built into Windows", max([x["lvl"] for x in sv] or [0]),
        f"{len(sv)} services run code not signed by Microsoft." if sv else "Every service is a Microsoft-signed Windows component.", sorted(sv, key=lambda x: -x["lvl"]))
    tk = []
    for t in _arr(a.get("tasks")):
        lvl, who, why = _file_verdict(t.get("st"), t.get("by"), t.get("file"))
        tk.append({"lvl": lvl, "a": (t.get("path") or "") + (t.get("name") or ""), "b": who or why or (t.get("author") or ""),
                   "c": f"{t.get('state')} - {t.get('trig') or 'no trigger'}", "d": t.get("exec")})
    add("Autostart", "Scheduled tasks outside \\Microsoft\\", max([x["lvl"] for x in tk] or [0]),
        f"{len(tk)} third-party scheduled tasks." if tk else "No third-party scheduled tasks.", sorted(tk, key=lambda x: -x["lvl"]))
    su = []
    for s in _arr(a.get("startup")):
        lvl, who, why = _file_verdict(s.get("st"), s.get("by"), s.get("file")) if s.get("st") else (0, "", "")
        su.append({"lvl": lvl, "a": s.get("name"), "b": who or why, "c": (s.get("where") or "").replace("HKEY_LOCAL_MACHINE", "HKLM"), "d": s.get("cmd")})
    add("Autostart", "Run-at-login entries", max([x["lvl"] for x in su] or [0]), f"{len(su)} programs start at login.", su)

    # --- drivers
    dr = _arr(a.get("drivers"))
    dl = []
    for d in dr:
        lvl = 2 if CHINA_ORGS.search(f"{d.get('prov')} {d.get('mfr')}") else (0 if SAFE_SIGNERS.search(f"{d.get('prov')} {d.get('signer')}") else 1)
        dl.append({"lvl": lvl, "a": d.get("dev"), "b": d.get("prov"), "c": d.get("ver"), "d": f"{d.get('cls') or ''} - signed by {d.get('signer') or '?'}"})
    provs = sorted({d.get("prov") for d in dr if d.get("prov")})
    add("Drivers", "Third-party device drivers", max([x["lvl"] for x in dl] or [0]),
        f"{len(dr)} non-Microsoft drivers from: {', '.join(provs)}." + (" Chip makers like Intel, AMD, Realtek, Qualcomm and MediaTek are normal; an unfamiliar or Chinese software vendor is worth a look." if dr else ""),
        sorted(dl, key=lambda x: -x["lvl"]))
    kd = []
    for d in _arr(a.get("kdrivers")):
        lvl, who, why = _file_verdict(d.get("st"), d.get("by"), d.get("path"))
        kd.append({"lvl": lvl, "a": d.get("disp") or d.get("name"), "b": who or why, "c": d.get("state"), "d": d.get("path")})
    if kd:
        add("Drivers", "Kernel drivers outside the Windows driver folders", max(x["lvl"] for x in kd), f"{len(kd)} kernel drivers load from unusual places.", kd)

    # --- network trust
    roots = _arr(a.get("roots"))
    cn_roots = [r for r in roots if re.search(r"\bC=CN\b|china|cfca|wosign|cnnic|shenzhen|beijing|shanghai|guangdong|\bmele\b", r.get("sub") or "", re.I)]
    add("Network trust", "Trusted root certificates", 2 if cn_roots else 0,
        f"{len(roots)} root certificates trusted machine-wide."
        + (f" {len(cn_roots)} are Chinese - a root certificate lets its owner impersonate ANY website to this PC." if cn_roots else
           " None issued in China. (An unexpected root is the classic way to intercept encrypted traffic.)"),
        [{"lvl": 2 if r in cn_roots else 0, "a": r.get("sub"), "b": f"{r.get('from')} to {r.get('to')}", "c": r.get("th")} for r in sorted(roots, key=lambda r: r not in cn_roots)])
    ur = _arr(a.get("uroots"))
    if ur:
        add("Network trust", "Extra roots trusted by your user only", 1, f"{len(ur)} root certificates were added for your user account.",
            [{"lvl": 1, "a": r.get("sub"), "b": r.get("from"), "c": r.get("th")} for r in ur])
    hosts = _arr(a.get("hosts"))
    add("Network trust", "Hosts file", 1 if hosts else 0, ("Custom entries (they override DNS): " + " | ".join(hosts[:12])) if hosts else "Hosts file is empty (default).")
    px = a.get("proxy") or {}
    proxied = bool(px.get("enabled")) or bool(px.get("pac")) or ("Direct access" not in (px.get("winhttp") or "Direct access"))
    add("Network trust", "Proxy settings", 2 if proxied else 0,
        f"Traffic is routed through a proxy: {px.get('server') or ''} {px.get('pac') or ''} {px.get('winhttp') or ''}" if proxied else "No proxy - traffic goes straight out.")
    dnsl = _arr(a.get("dns"))
    srvs = sorted({s for d in dnsl for s in _arr(d.get("srv"))})
    bad = [s for s in srvs if s in CHINA_DNS]
    add("Network trust", "DNS servers", 2 if bad else 0,
        "DNS servers in use: " + ", ".join(srvs) + (f". {', '.join(bad)} are CHINESE public resolvers - every website you visit is visible to them." if bad else
                                                    ". (Private 192.168.x / fe80 addresses = your router, which is normal.)"))

    # --- accounts / protection
    users = _arr(a.get("users"))
    en = [u for u in users if u.get("enabled")]
    add("Accounts", "Local user accounts", 1 if len(en) > 2 else 0, f"Enabled accounts: {', '.join(u.get('name') for u in en)}. Administrators: {', '.join(_arr(a.get('admins')))}.",
        [{"lvl": 0, "a": u.get("name"), "b": "enabled" if u.get("enabled") else "disabled", "c": u.get("last"), "d": u.get("desc")} for u in users])
    dfd = a.get("defender") or {}
    add("Protection", "Microsoft Defender", 0 if dfd.get("rt") else 2,
        f"Antivirus {'on' if dfd.get('on') else 'OFF'}, real-time protection {'on' if dfd.get('rt') else 'OFF'}, tamper protection {'on' if dfd.get('tamper') else 'off'}, "
        f"signatures from {str(dfd.get('sig') or '?')[:16]}.")
    prof = _arr(a.get("fwprofiles"))
    off = [p.get("name") for p in prof if str(p.get("on")) not in ("True", "1")]
    add("Protection", "Windows Firewall", 1 if off else 0, ("Firewall OFF for: " + ", ".join(off)) if off else "Firewall on for Domain, Private and Public networks.")
    _harden_checks(a.get("harden"), _arr(a.get("admins")), add)
    rules = _arr(a.get("fwrules"))
    order = {2: 0, 1: 1, 0: 2}
    checks.sort(key=lambda c: order[c["lvl"]])
    return {"t": time.time(), "checks": checks, "system": sysi, "blocks": rules,
            "score": {"alert": sum(c["lvl"] == 2 for c in checks), "review": sum(c["lvl"] == 1 for c in checks), "ok": sum(c["lvl"] == 0 for c in checks)}}


# ----------------------------------------------------------------------------------------------- macOS audit
def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _plist_program(p):
    import plistlib  # noqa: PLC0415
    try:
        d = plistlib.loads(Path(p).read_bytes())
    except Exception:  # noqa: BLE001
        return {}, ""
    prog = d.get("Program") or (d.get("ProgramArguments") or [""])[0]
    return d, prog


def _audit_mac():
    """Same shape as the Windows audit, using macOS's own tools (none of them need admin rights)."""
    checks = []

    def add(group, title, lvl, detail, items=None, fix=None):
        checks.append({"group": group, "title": title, "lvl": lvl, "detail": detail, "items": items or [], "fix": fix})

    def sig_verdict(path):
        if not path or not os.path.exists(path):
            return 0, "", "File not found (left-over entry)"
        who, st, team = _codesign(path)
        if who == "NotSigned":
            return 1, "", "Not code-signed"
        if who.startswith("BAD:"):
            return 2, "", "Signature INVALID: " + who[4:]
        if SAFE_SIGNERS.search(who):
            return 0, who, ""
        return (2 if CHINA_ORGS.search(who) else 1), who, "Signed by a non-Apple developer - check you recognise it"

    # --- system
    _, hw = _run(["system_profiler", "SPHardwareDataType", "SPSoftwareDataType", "-json"], 60)
    try:
        j = json.loads(hw[hw.find("{"):])
        h = (j.get("SPHardwareDataType") or [{}])[0]
        sw = (j.get("SPSoftwareDataType") or [{}])[0]
    except ValueError:
        h, sw = {}, {}
    sysi = {"maker": "Apple", "model": h.get("machine_name", "Mac") + (f" ({h.get('machine_model')})" if h.get("machine_model") else ""),
            "bios": h.get("boot_rom_version", ""), "os": sw.get("os_version", "macOS"),
            "cpu": h.get("chip_type") or h.get("cpu_type", "")}
    add("System", "Hardware identity", 0, f"{sysi['model']}, {sysi['cpu']}, firmware {sysi['bios']}, {sysi['os']}.")

    # --- built-in protections
    _, sip = _run(["csrutil", "status"])
    add("Protection", "System Integrity Protection", 0 if "enabled" in sip.lower() and "disabled" not in sip.lower() else 2,
        "SIP is on: even admin software can't modify macOS system files." if "enabled." in sip.lower() else
        "SIP is OFF or partly off - system files can be modified. Turn it back on from Recovery mode (csrutil enable). " + sip.strip()[:120])
    _, gk = _run(["spctl", "--status"])
    add("Protection", "Gatekeeper", 0 if "enabled" in gk else 2,
        "Gatekeeper is on: apps must be signed by an identified developer and notarized by Apple." if "enabled" in gk else
        "Gatekeeper is OFF - any app can run without Apple's checks. Turn it on: sudo spctl --master-enable")
    _, fv = _run(["fdesetup", "status"])
    add("Protection", "FileVault disk encryption", 0 if "On" in fv else 1, fv.strip()[:160] or "Unknown")
    _, fwg = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    add("Protection", "macOS firewall (incoming)", 0 if "enabled" in fwg.lower() else 1,
        "Firewall on." if "enabled" in fwg.lower() else "Firewall off - turn it on in System Settings > Network > Firewall. "
        "(It only filters incoming connections; to watch outgoing ones use LuLu, free from objective-see.org.)")
    _, mdm = _run(["profiles", "status", "-type", "enrollment"])
    enrolled = re.search(r"MDM enrollment:\s*Yes", mdm, re.I)
    add("Protection", "Device management (MDM)", 2 if enrolled else 0,
        ("This Mac is ENROLLED in device management: whoever runs that server can install apps, profiles and certificates. "
         "Fine for a work Mac; on a personal Mac, find out who. " + mdm.strip()[:200]) if enrolled else "Not enrolled in any device-management server.")

    # --- remote access
    on = [n for n, p in (("Remote Login (SSH)", 22), ("Screen Sharing / VNC", 5900), ("Apple Remote Desktop", 3283)) if _port_open(p)]
    add("Remote access", "Built-in remote access", 1 if on else 0,
        ("Enabled: " + ", ".join(on) + ". Turn off anything you don't use in System Settings > General > Sharing.") if on else
        "Remote Login, Screen Sharing and Remote Management are off.")
    apps = []
    for root in (Path("/Applications"), Path.home() / "Applications"):
        try:
            apps += sorted(p for p in root.iterdir() if p.suffix == ".app")
        except OSError:
            pass
    rt = [a.stem for a in apps if REMOTE_TOOLS.search(a.stem)]
    add("Remote access", "Remote-control software", 2 if rt else 0,
        ("Remote-control apps installed: " + ", ".join(rt)) if rt else "No AnyDesk / TeamViewer / ToDesk / Sunlogin / RustDesk-type apps installed.")

    # --- software
    with ThreadPoolExecutor(6) as ex:
        verdicts = list(ex.map(lambda a: sig_verdict(str(a)), apps))
    items = [{"lvl": v[0], "a": a.stem, "b": v[1] or v[2], "c": str(a.parent)} for a, v in zip(apps, verdicts)]
    items.sort(key=lambda x: (-x["lvl"], x["a"].lower()))
    add("Software", "Applications", max([i["lvl"] for i in items] or [0]),
        f"{len(apps)} apps; {sum(1 for i in items if i['lvl'])} not signed by Apple or a well-known developer.", items)

    # --- autostart: launch agents / daemons (how background software starts on a Mac)
    la = []
    for d in (Path("/Library/LaunchDaemons"), Path("/Library/LaunchAgents"), Path.home() / "Library/LaunchAgents"):
        try:
            for p in sorted(d.glob("*.plist")):
                info, prog = _plist_program(p)
                lvl, who, why = sig_verdict(prog)
                if CHINA_ORGS.search(f"{p.name} {prog}"):
                    lvl = 2
                la.append({"lvl": lvl, "a": p.name, "b": who or why, "c": str(d), "d": prog})
        except OSError:
            pass
    add("Autostart", "Launch agents & daemons (background programs)", max([x["lvl"] for x in la] or [0]),
        f"{len(la)} third-party background items start automatically. Also check System Settings > General > Login Items.",
        sorted(la, key=lambda x: -x["lvl"]))
    _, sx = _run(["systemextensionsctl", "list"])
    ext = [ln.strip() for ln in sx.splitlines() if "\t" in ln and "[activated" in ln]
    add("Autostart", "System extensions (drivers, network filters)", 1 if ext else 0,
        (f"{len(ext)} active system extensions (VPNs, security tools and drivers use these; a network extension can see all traffic).")
        if ext else "No third-party system extensions.", [{"lvl": 1, "a": e[:160], "b": ""} for e in ext])

    # --- network trust
    certs = []
    for args in (["security", "dump-trust-settings", "-d"], ["security", "dump-trust-settings"]):
        _, out = _run(args)
        certs += [m.group(1).strip() for m in re.finditer(r"^Cert \d+:\s*(.+)$", out, re.M)]
    certs = sorted(set(certs))  # a cert can be trusted at both admin and user level
    cn =[c for c in certs if re.search(r"china|cfca|wosign|cnnic|shenzhen|beijing|shanghai|guangdong|\bmele\b", c, re.I)]
    add("Network trust", "Extra trusted certificates", 2 if cn else (1 if certs else 0),
        (f"{len(certs)} certificates were given custom trust by an admin or app"
         + (f"; {len(cn)} are Chinese - a trusted root lets its owner impersonate ANY website to this Mac." if cn else
            ". Check you recognise them (VPN, antivirus and work tools add these).")) if certs else
        "No added trusted certificates - only Apple's built-in list.", [{"lvl": 2 if c in cn else 1, "a": c, "b": ""} for c in certs])
    try:
        hosts = [ln.strip() for ln in Path("/etc/hosts").read_text(errors="replace").splitlines()
                 if ln.strip() and not ln.strip().startswith("#") and not re.search(r"\b(localhost|broadcasthost)\b", ln)]
    except OSError:
        hosts = []
    add("Network trust", "Hosts file", 1 if hosts else 0, ("Custom entries (they override DNS): " + " | ".join(hosts[:12])) if hosts else "Hosts file has only the defaults.")
    _, px = _run(["scutil", "--proxy"])
    proxied = re.search(r"(HTTPEnable|HTTPSEnable|ProxyAutoConfigEnable|SOCKSEnable)\s*:\s*1", px)
    add("Network trust", "Proxy settings", 2 if proxied else 0,
        ("Traffic is routed through a proxy: " + " ".join(re.findall(r"(?:HTTPSProxy|HTTPProxy|ProxyAutoConfigURLString|SOCKSProxy)\s*:\s*(\S+)", px))) if proxied else
        "No proxy - traffic goes straight out.")
    _, dn = _run(["scutil", "--dns"])
    srvs = sorted(set(re.findall(r"nameserver\[\d+\]\s*:\s*(\S+)", dn)))
    bad = [s for s in srvs if s in CHINA_DNS]
    add("Network trust", "DNS servers", 2 if bad else 0,
        "DNS servers in use: " + (", ".join(srvs) or "unknown") + (f". {', '.join(bad)} are CHINESE public resolvers." if bad else "."))

    # --- accounts
    _, us = _run(["dscl", ".", "list", "/Users", "UniqueID"])
    users = [ln.split()[0] for ln in us.splitlines() if len(ln.split()) == 2 and ln.split()[1].isdigit() and int(ln.split()[1]) >= 500]
    _, ad = _run(["dscl", ".", "-read", "/Groups/admin", "GroupMembership"])
    admins = ad.replace("GroupMembership:", "").split()
    add("Accounts", "User accounts", 1 if len(users) > 2 else 0, f"Accounts: {', '.join(users) or '?'}. Administrators: {', '.join(admins) or '?'}.")

    order = {2: 0, 1: 1, 0: 2}
    checks.sort(key=lambda c: order[c["lvl"]])
    return {"t": time.time(), "checks": checks, "system": sysi, "blocks": [],
            "score": {"alert": sum(c["lvl"] == 2 for c in checks), "review": sum(c["lvl"] == 1 for c in checks), "ok": sum(c["lvl"] == 0 for c in checks)}}


# ----------------------------------------------------------------------------------------------- actions
def _is_admin():
    try:
        if IS_WIN:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:  # noqa: BLE001
        return False


def block(q):
    """Outbound firewall block for one program. Needs admin, so it launches an elevated PowerShell (UAC prompt)."""
    if not IS_WIN:
        raise ValueError("macOS's built-in firewall only blocks incoming connections. To stop a program sending data out, "
                         "use LuLu (free, objective-see.org) or Little Snitch.")
    exe = q.get("exe") or ""
    undo = q.get("undo") == "1"
    if not exe or not os.path.isabs(exe) or not os.path.isfile(exe):
        raise ValueError("Pick a program with a full path")
    low = exe.lower()
    if low.startswith(r"c:\windows\system32\svchost.exe") or low.endswith(("\\lsass.exe", "\\services.exe", "\\system")):
        raise ValueError("Blocking this core Windows process would break networking for everything")
    name = "DeviceWatch block - " + Path(exe).name
    esc_ = exe.replace("'", "''")
    if undo:
        cmd = f"Get-NetFirewallRule -DisplayName 'DeviceWatch block*' | ?{{ ($_ | Get-NetFirewallApplicationFilter).Program -eq '{esc_}' }} | Remove-NetFirewallRule"
    else:
        cmd = f"New-NetFirewallRule -DisplayName '{name.replace(chr(39), chr(39) * 2)}' -Direction Outbound -Action Block -Program '{esc_}' -Profile Any"
    enc = base64.b64encode(cmd.encode("utf-16-le")).decode()
    ShellExecute = ctypes.windll.shell32.ShellExecuteW
    rc = ShellExecute(None, "runas", "powershell.exe", f"-NoProfile -WindowStyle Hidden -EncodedCommand {enc}", None, 0)
    _AUDIT["t"] = 0
    if rc <= 32:
        raise RuntimeError("Windows did not run the firewall command (UAC prompt declined?)")
    return {"ok": True, "cmd": cmd, "undo": undo}


NETBLOCK = "DeviceWatch netblock - "


def _rdap_range(ip):
    """The registered block of addresses `ip` belongs to: (network name, [ip_network, ...])."""
    a = ipaddress.ip_address(ip.split("%")[0])
    if not a.is_global:
        raise ValueError("That is not a public internet address")
    boot = _bootstrap()["ipv4" if a.version == 4 else "ipv6"]
    base, best = None, -1
    for net, urls in boot:
        if a.version == net.version and a in net and net.prefixlen > best:
            best, base = net.prefixlen, next((u for u in urls if u.startswith("https")), urls[0])
    if not base:
        raise RuntimeError("No address registry is listed for that address")
    r = HTTP.get(base.rstrip("/") + "/ip/" + str(a), timeout=20)
    r.raise_for_status()
    j = r.json()
    lo, hi = ipaddress.ip_address(j["startAddress"].split("/")[0]), ipaddress.ip_address(j["endAddress"].split("/")[0])
    if lo.version != a.version or not lo <= a <= hi:
        raise RuntimeError("The registry's answer does not cover that address")
    nets = list(ipaddress.summarize_address_range(lo, hi))
    # a range this wide is a whole registry region, not one company: blocking it would cut off a slice of the internet
    if min(n.prefixlen for n in nets) < (8 if a.version == 4 else 19) or len(nets) > 40:
        raise ValueError(f"The registered range ({lo} to {hi}) is too wide to be one company's network; not blocking it")
    return (j.get("name") or "").strip(), nets


def netblock(q):
    """Outbound firewall block for a whole network (the registered range an address belongs to), for every program
    on this PC. Needs admin: launches an elevated PowerShell (UAC prompt). undo=1 with name= removes one."""
    if not IS_WIN:
        raise ValueError("Needs Windows Firewall")
    if q.get("undo") == "1":
        name = q.get("name") or ""
        if not name.startswith(NETBLOCK) or re.search(r"[`$\"\r\n]", name):
            raise ValueError("Not one of this app's network blocks")
        cmd = f"Remove-NetFirewallRule -DisplayName '{name.replace(chr(39), chr(39) * 2)}'"
    else:
        ip = (q.get("ip") or "").strip()
        label = re.sub(r"[^\w .,()&-]", "", q.get("org") or "")[:60].strip()
        net_name, nets = _rdap_range(ip)
        name = f"{NETBLOCK}{label or net_name or ip} [{nets[0]}{' +' + str(len(nets) - 1) if len(nets) > 1 else ''}]"
        addrs = ",".join(f"'{n}'" for n in nets)
        cmd = (f"Remove-NetFirewallRule -DisplayName '{name.replace(chr(39), chr(39) * 2)}' -ErrorAction SilentlyContinue; "
               f"New-NetFirewallRule -DisplayName '{name.replace(chr(39), chr(39) * 2)}' -Description 'Device Watch: no program on this PC may connect to this network' "
               f"-Direction Outbound -Action Block -RemoteAddress @({addrs}) -Profile Any")
    enc = base64.b64encode(cmd.encode("utf-16-le")).decode()
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", f"-NoProfile -WindowStyle Hidden -EncodedCommand {enc}", None, 0)
    if rc <= 32:
        raise RuntimeError("Windows did not run the firewall command (UAC prompt declined?)")
    return {"ok": True, "name": name}


def netblocks(q=None):
    """This app's network blocks, read from Windows Firewall (reading needs no admin)."""
    if not IS_WIN:
        return {"rows": []}
    out = _ps(f"@(Get-NetFirewallRule -DisplayName '{NETBLOCK}*' -ErrorAction SilentlyContinue | %{{ [ordered]@{{name=$_.DisplayName; on=([string]$_.Enabled -eq 'True'); "
              "addrs=@(($_ | Get-NetFirewallAddressFilter).RemoteAddress)}} }) | ConvertTo-Json -Depth 3 -Compress", 40)
    try:
        j = json.loads(out.strip() or "[]")
    except ValueError:
        j = []
    rows = j if isinstance(j, list) else [j]
    for r in rows:
        r["label"] = (r.get("name") or "")[len(NETBLOCK):]
        a = r.get("addrs")
        r["addrs"] = a if isinstance(a, list) else ([a] if a else [])
    return {"rows": rows}


MAC_AGENT = "com.marketterminal.devicewatch"


def _startup_link():
    if IS_MAC:
        return Path.home() / "Library" / "LaunchAgents" / f"{MAC_AGENT}.plist"
    if IS_WIN:
        return Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\Device Watch collector.lnk"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "device-watch.desktop"


def _collect_cmd():
    """The command that runs the headless server + logger: the built exe, or this Python with main.py."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--collect"]
    return [sys.executable, str(Path(__file__).resolve().parent / "main.py"), "--collect"]


def _always_on_unix(on, path):
    import plistlib  # noqa: PLC0415
    if IS_MAC:
        if not on:
            if path.is_file():
                _run(["launchctl", "unload", str(path)], 20)
                path.unlink()
            return {"always_on": False}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps({
            "Label": MAC_AGENT, "ProgramArguments": _collect_cmd(), "RunAtLoad": True, "KeepAlive": False,
            "WorkingDirectory": str(Path(__file__).resolve().parent),
            "StandardOutPath": str(DATA_DIR / "collector.log"), "StandardErrorPath": str(DATA_DIR / "collector.log")}))
        _run(["launchctl", "load", str(path)], 20)  # start now as well as at every login
        return {"always_on": path.is_file()}
    if not on:
        if path.is_file():
            path.unlink()
        return {"always_on": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Desktop Entry]\nType=Application\nName=Device Watch collector\nExec="
                    + " ".join(f'"{a}"' for a in _collect_cmd()) + "\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    return {"always_on": True}


def set_always_on(q):
    on = q.get("on") == "1"
    lnk = _startup_link()
    if not IS_WIN:
        return _always_on_unix(on, lnk)
    if not on:
        if lnk.is_file():
            lnk.unlink()
        return {"always_on": False}
    if getattr(sys, "frozen", False):
        target, args, wd = sys.executable, "--collect", str(Path(sys.executable).parent)
    else:
        py = Path(sys.executable)
        pw = py.with_name("pythonw.exe")
        target, args, wd = str(pw if pw.is_file() else py), f'"{Path(__file__).resolve().parent / "main.py"}" --collect', str(Path(__file__).resolve().parent)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + str(lnk).replace("'", "''") + "');"
          f"$s.TargetPath='{target.replace(chr(39), chr(39) * 2)}'; $s.Arguments='{args.replace(chr(39), chr(39) * 2)}';"
          f"$s.WorkingDirectory='{wd.replace(chr(39), chr(39) * 2)}'; $s.WindowStyle=7; $s.Description='Device Watch: logs network activity in the background'; $s.Save()")
    _ps(ps, 30)
    return {"always_on": lnk.is_file()}


def clear(q):
    with _DB_LOCK, _db() as c:
        for t in ("conns", "traffic", "dns", "events", "listen"):
            c.execute(f"DELETE FROM {t}")
    return {"ok": True}
