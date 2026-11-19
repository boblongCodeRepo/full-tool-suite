"""Hotspot Watch (Device Watch > VACUUM tab) - how much data does a gadget send, and to whom?

A robot vacuum (or camera, TV, plug...) cannot be watched from another PC on the same Wi-Fi: its traffic never passes
this machine. So this PC becomes its Wi-Fi: Windows' Mobile Hotspot is switched on, the gadget is paired to it, and
every packet it sends crosses this PC on the way to the internet.

* Volume (no admin needed): the hotspot's own network adapter has byte counters. Bytes RECEIVED on it are what the
  gadgets uploaded, bytes SENT are what they downloaded. Sampled every 5 s into one-minute buckets.
* Alerts: more than N MB uploaded in M minutes, or more than N MB in 24 hours -> an ALERT in Device Watch's event log
  plus a Windows notification.
* Keep-alive: Windows switches the hotspot off after a reboot (and sometimes when idle); while "keep on" is set the
  monitor switches it back on so the gadget does not drop offline.
* Destinations (needs admin once): a small elevated helper (this exe with --hs-sniff, started at log-on by a scheduled
  task) reads packet HEADERS on the hotspot adapter through a raw socket: which gadget, which remote address, how many
  bytes each way, and the domain names it asks the hotspot's DNS for. No payload is stored.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

IS_WIN = sys.platform == "win32"
DATA_DIR = Path(os.environ.get("MT_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MarketTerminal")
HS_DIR = DATA_DIR / "device"
HS_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = HS_DIR / "hotspot.db"
STOP_FLAG = HS_DIR / "hotspot-sniff.stop"
RESTART_FLAG = HS_DIR / "hotspot-sniff.restart"
TASK = "DeviceWatch hotspot capture"
HOTSPOT_IP = "192.168.137.1"  # Windows Internet Connection Sharing always uses this address for the hotspot
SAMPLE_SECS = 5
MB = 1048576
NO_WINDOW = 0x08000000
_CN_NAMES = {"aceville": "Tencent Cloud", "tencent": "Tencent Cloud", "myqcloud": "Tencent Cloud", "qcloud": "Tencent Cloud", "aliyun": "Alibaba Cloud",
             "alibaba": "Alibaba Cloud", "alicdn": "Alibaba Cloud", "huawei": "Huawei Cloud", "baidu": "Baidu Cloud",
             "bytedance": "ByteDance", "volces": "ByteDance", "xiaomi": "Xiaomi", "mi.com": "Xiaomi", "jdcloud": "JD Cloud",
             "ucloud": "UCloud", "kingsoft": "Kingsoft Cloud", "ksyun": "Kingsoft Cloud"}


def _cn_label(hit):
    return _CN_NAMES.get(hit.lower(), hit)


_CN_CLOUD = re.compile(r"aceville|tencent|myqcloud|qcloud|aliyun|alibaba|alicdn|huawei|baidu|bytedance|volces|xiaomi|mi\.com|jdcloud|ucloud|kingsoft|ksyun", re.I)
DEFAULTS = {"keep_on": False, "burst_mb": 25.0, "burst_min": 10.0, "day_mb": 200.0, "notify": True, "newdest": True, "learn_h": 24.0,
            "push": ""}  # ntfy topic for alerts on your phone; empty = off (nothing is ever sent)
PUSH_URL = "https://ntfy.sh/"
PUSH_EVERY = 120
_PUSH = {"last": 0.0, "err": None, "sent": 0}


# ----------------------------------------------------------------------------------------------- storage
def _db():
    c = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    with _db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS vol(ts INT PRIMARY KEY, up INT, down INT);
        CREATE TABLE IF NOT EXISTS flows(day TEXT, client TEXT, rip TEXT, rport INT, proto TEXT, up INT, down INT, pkts INT,
            first REAL, last REAL, PRIMARY KEY(day, client, rip, rport, proto));
        CREATE INDEX IF NOT EXISTS flows_last ON flows(last);
        CREATE TABLE IF NOT EXISTS fmin(ts INT, client TEXT, rip TEXT, up INT, PRIMARY KEY(ts, client, rip));
        CREATE TABLE IF NOT EXISTS names(name TEXT, ip TEXT, client TEXT, first REAL, last REAL, n INT,
            PRIMARY KEY(name, ip, client));
        CREATE TABLE IF NOT EXISTS clients(mac TEXT PRIMARY KEY, ip TEXT, host TEXT, first REAL, last REAL);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS known(client TEXT, dest TEXT, first REAL, ip TEXT, label TEXT, PRIMARY KEY(client, dest));
        """)


def _meta(k, default=None):
    with _db() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def _set_meta(k, v):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


def _cfg():
    try:
        saved = json.loads(_meta("cfg") or "{}")
    except ValueError:
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def _save_cfg(**changes):
    cfg = {**_cfg(), **changes}
    _set_meta("cfg", json.dumps(cfg))
    return cfg


# ----------------------------------------------------------------------------------------------- Windows hotspot
# The same switch as Settings > Network > Mobile hotspot, through the WinRT tethering API. No admin needed.
HS_PS = r"""
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime] | Out-Null
[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null
$ext = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 }
$asOp = ($ext | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asAct = ($ext | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
function AwaitOp($op, $type) { $t = $asOp.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(30000) | Out-Null; $t.Result }
function AwaitAct($a) { $t = $asAct.Invoke($null, @($a)); $t.Wait(30000) | Out-Null }
$out = [ordered]@{}
$p = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if (-not $p) { $out.error = "This PC has no internet connection to share"; $out | ConvertTo-Json -Depth 5; exit }
$m = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($p)
$cfg = $m.GetCurrentAccessPointConfiguration()
$resT = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]
if ($Do -eq "start") {
    # robot vacuums and most smart-home gadgets are 2.4 GHz only
    try { if ($cfg.IsBandSupported('TwoPointFourGigahertz') -and "$($cfg.Band)" -ne 'TwoPointFourGigahertz') { $cfg.Band = 'TwoPointFourGigahertz'; AwaitAct ($m.ConfigureAccessPointAsync($cfg)) } } catch { $out.band_error = $_.Exception.Message }
    if ("$($m.TetheringOperationalState)" -ne 'On') { $r = AwaitOp ($m.StartTetheringAsync()) $resT; $out.result = "$($r.Status)"; $out.detail = "$($r.AdditionalErrorMessage)" }
} elseif ($Do -eq "stop") {
    if ("$($m.TetheringOperationalState)" -ne 'Off') { $r = AwaitOp ($m.StopTetheringAsync()) $resT; $out.result = "$($r.Status)"; $out.detail = "$($r.AdditionalErrorMessage)" }
}
$cfg = $m.GetCurrentAccessPointConfiguration()
$out.uplink = $p.ProfileName
$out.state = "$($m.TetheringOperationalState)"
$out.ssid = $cfg.Ssid; $out.pass = $cfg.Passphrase; $out.band = "$($cfg.Band)"
$out.max = $m.MaxClientCount
$arp = @{}
try { Get-NetNeighbor -AddressFamily IPv4 -ErrorAction Stop | Where-Object { $_.IPAddress -like '192.168.137.*' } | ForEach-Object { $arp[($_.LinkLayerAddress -replace '-', ':').ToLower()] = $_.IPAddress } } catch { }
$out.clients = @($m.GetTetheringClients() | ForEach-Object { $mac = $_.MacAddress.ToLower(); [ordered]@{ mac = $mac; host = (@($_.HostNames | ForEach-Object { $_.DisplayName }) -join ', '); ip = "$($arp[$mac])" } })
$out | ConvertTo-Json -Depth 5
"""


def _ps(script, timeout=90):
    # progress bars (e.g. from Set-Acl, module loading) otherwise arrive on stderr as "#< CLIXML" and look like errors
    enc = base64.b64encode(("$ProgressPreference = 'SilentlyContinue'; " + script).encode("utf-16-le")).decode()
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                       capture_output=True, timeout=timeout, creationflags=NO_WINDOW if IS_WIN else 0)
    return r.stdout.decode("utf-8", "replace").lstrip("﻿"), r.stderr.decode("utf-8", "replace")


_S = {"started": None, "on": False, "nic": None, "rate": (0.0, 0.0), "err": None, "hs": None, "hs_t": 0.0, "hs_err": None}
_HS_LOCK = threading.Lock()
_STARTED = threading.Event()


def _hotspot(do="status"):
    """Run one hotspot command; remembers the answer (name, password, connected devices) for the status call."""
    if not IS_WIN:
        raise RuntimeError("The hotspot monitor needs Windows 10/11")
    with _HS_LOCK:
        out, err = _ps(f"$Do = '{do}'\n" + HS_PS)
        i = out.find("{")
        if i < 0:
            _S["hs_err"] = (err.strip().splitlines() or ["no answer from Windows"])[0][:300]
            raise RuntimeError("Windows hotspot control failed: " + _S["hs_err"])
        d = json.loads(out[i:])
        if d.get("error"):
            _S["hs_err"] = d["error"]
            raise RuntimeError(d["error"])
        c = d.get("clients")
        d["clients"] = c if isinstance(c, list) else [c] if c else []  # PowerShell 5 unwraps one-item lists
        for cl in d["clients"]:  # Windows lists the address among the "host names": keep real names only
            cl["host"] = ", ".join(h for h in (x.strip() for x in (cl.get("host") or "").split(",")) if h and not re.fullmatch(r"[\d.]+|[0-9a-fA-F:]*:[0-9a-fA-F:]*", h))
        _S["hs"], _S["hs_t"], _S["hs_err"] = d, time.time(), None
        _remember_clients(d["clients"])
        if d.get("result") and d["result"] != "Success":
            raise RuntimeError(f"Windows could not {do} the hotspot: {d['result']} {d.get('detail') or ''}".strip())
        return d


def _remember_clients(clients):
    now = time.time()
    new = []
    with _db() as c:
        for cl in clients:
            mac = (cl.get("mac") or "").lower()
            if not mac:
                continue
            cur = c.execute("UPDATE clients SET last=?, ip=CASE WHEN ?<>'' THEN ? ELSE ip END, host=CASE WHEN ?<>'' THEN ? ELSE host END WHERE mac=?",
                            (now, cl.get("ip") or "", cl.get("ip") or "", cl.get("host") or "", cl.get("host") or "", mac))
            if cur.rowcount == 0:
                c.execute("INSERT INTO clients VALUES(?,?,?,?,?)", (mac, cl.get("ip") or "", cl.get("host") or "", now, now))
                new.append(cl)
    for cl in new:
        msg = (f"New device joined the hotspot: {cl.get('host') or 'unnamed device'} ({cl.get('mac')}, {cl.get('ip') or 'no address yet'}). "
               "Only devices that know the hotspot password can join. If you did not just connect something, change the hotspot password.")
        _event(2, "hotspot-device", msg, cl.get("mac") or "")
        if _cfg()["notify"]:
            _toast("Device Watch: new device on the hotspot", msg[:240])


def _nic():
    """(adapter name, address) of the running hotspot, or (None, None)."""
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET and a.address == HOTSPOT_IP and name in stats and stats[name].isup:
                return name, a.address
    return None, None


# ----------------------------------------------------------------------------------------------- alerts
def _event(level, kind, msg, ref=""):
    """Into Device Watch's own event log, so it shows on the ALERTS tab."""
    try:
        import device  # noqa: PLC0415
        with device._DB_LOCK, device._db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS events(ts REAL, level INT, kind TEXT, msg TEXT, ref TEXT)")
            c.execute("INSERT INTO events VALUES(?,?,?,?,?)", (time.time(), level, kind, msg, ref))
    except Exception as e:  # noqa: BLE001
        print(f"hotspot: could not log event ({e}): {msg}")


def _push(title, force=False):
    """Phone alert through ntfy.sh, only if you set a topic. Sends the alert's TITLE only (e.g. "Device Watch: possible
    hidden tracker"), never the details, addresses or names: those stay on this PC."""
    topic = (_cfg().get("push") or "").strip()
    now = time.time()
    if not topic or (not force and now - _PUSH["last"] < PUSH_EVERY):
        return
    _PUSH["last"] = now

    def send():
        try:
            import requests  # noqa: PLC0415
            r = requests.post(PUSH_URL + topic, data=(title + " - open Device Watch on the PC for details.").encode("utf-8"),
                              headers={"Title": "Device Watch", "Priority": "high", "Tags": "warning"}, timeout=20)
            r.raise_for_status()
            _PUSH.update(err=None, sent=_PUSH["sent"] + 1)
        except Exception as e:  # noqa: BLE001
            _PUSH["err"] = str(e)[:160]

    threading.Thread(target=send, daemon=True).start()


def push_test(q):
    if not (_cfg().get("push") or "").strip():
        raise ValueError("Set a topic name first")
    _push("Device Watch: test alert. Phone alerts are working", force=True)
    return {"ok": True}


def _toast(title, text):
    try:
        _push(title)
    except Exception:  # noqa: BLE001
        pass
    if not IS_WIN:
        return
    q = lambda s: s.replace("'", "''")  # noqa: E731
    script = ("Add-Type -AssemblyName System.Windows.Forms, System.Drawing; $n = New-Object System.Windows.Forms.NotifyIcon; "
              "$n.Icon = [System.Drawing.SystemIcons]::Warning; $n.Visible = $true; "
              f"$n.ShowBalloonTip(20000, '{q(title)}', '{q(text)}', 'Warning'); Start-Sleep 15; $n.Dispose()")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
                         creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _mb(n):
    return f"{n / MB:,.1f} MB" if n >= MB / 10 else f"{n / 1024:,.0f} KB"


def _who():
    hs = _S.get("hs") or {}
    names = [c.get("host") or c.get("mac") for c in hs.get("clients") or []]
    return ", ".join(n for n in names if n) or "A device on the hotspot"


def _sums(now, cfg):
    with _db() as c:
        burst = c.execute("SELECT COALESCE(SUM(up),0) FROM vol WHERE ts >= ?", (now - cfg["burst_min"] * 60,)).fetchone()[0]
        day = c.execute("SELECT COALESCE(SUM(up),0), COALESCE(SUM(down),0) FROM vol WHERE ts >= ?", (now - 86400,)).fetchone()
    return burst, day[0], day[1]


def _check_alerts(now):
    cfg = _cfg()
    burst, day, _ = _sums(now, cfg)
    if burst >= cfg["burst_mb"] * MB and now - float(_meta("alert_burst") or 0) > 3600:
        _set_meta("alert_burst", now)
        msg = f"{_who()} uploaded {_mb(burst)} in the last {cfg['burst_min']:g} minutes (your limit: {cfg['burst_mb']:g} MB)"
        _event(2, "hotspot-upload", msg, "burst")
        if cfg["notify"]:
            _toast("Device Watch: large upload", msg)
    if day >= cfg["day_mb"] * MB and now - float(_meta("alert_day") or 0) > 86400:
        _set_meta("alert_day", now)
        msg = f"{_who()} uploaded {_mb(day)} in the last 24 hours (your limit: {cfg['day_mb']:g} MB)"
        _event(2, "hotspot-upload", msg, "day")
        if cfg["notify"]:
            _toast("Device Watch: large upload today", msg)
    try:
        _check_newdest(now, cfg)
    except Exception as e:  # noqa: BLE001  (never let this stop the byte counting)
        print(f"hotspot: new-destination check failed: {e}")


def _describe(ip, name, info):
    """(key, label, lvl) for one destination. The key is the domain name when one is known, so a service that
    rotates through many addresses (Amazon) counts once; otherwise the registered owner; otherwise the address."""
    import device  # noqa: PLC0415
    d = device._dest(ip, info, {ip: name} if name else {})
    org = d.get("org") or ""
    hay = f"{org} {(info.get(ip) or {}).get('net') or ''} {name}"
    m = _CN_CLOUD.search(hay)
    lvl = d.get("lvl") or 0
    if m:
        org, lvl = _cn_label(m.group(0)), max(lvl, 1)
    if name:
        key = "name:" + name.lower()
    elif org:
        key = "org:" + org.lower()
    else:
        key = "ip:" + ip
    where = ", ".join(x for x in (org, d.get("country") or "") if x)
    label = f"{name or ip}" + (f" ({where})" if where else "")
    if not name:
        label += " — contacted by raw address, no domain lookup seen"
    return key, label, lvl


def _check_newdest(now, cfg):
    """Alert the first time any hotspot device sends data to a destination it has never used before."""
    import device  # noqa: PLC0415
    with _db() as c:
        rows = c.execute("SELECT client, rip, MIN(first) first, SUM(up) up FROM flows WHERE last >= ? AND up > 0 GROUP BY client, rip",
                         (now - 2 * 86400,)).fetchall()
        if not rows:
            return
        names = {}
        for r in c.execute("SELECT ip, name FROM names WHERE ip <> '' ORDER BY last"):
            names[r["ip"]] = r["name"]
        cl = {r["ip"]: r for r in c.execute("SELECT * FROM clients WHERE ip <> '' ORDER BY last")}
        seeded = _meta("newdest_seeded")
    with device._db() as dc:
        info, _sigs, _dn = device._maps(dc)
    fresh = []
    with _db() as c:
        for r in rows:
            ip = r["rip"]
            if device._ip_kind(ip) != "public":
                continue
            name = names.get(ip) or ""
            if not name and ip not in info and now - r["first"] < 600:
                continue  # owner lookup still pending: wait so the same server is not keyed twice
            key, label, lvl = _describe(ip, name, info)
            who = cl.get(r["client"])
            cid = (who["mac"] if who else "") or r["client"]
            if c.execute("SELECT 1 FROM known WHERE client=? AND dest=?", (cid, key)).fetchone():
                continue
            c.execute("INSERT INTO known VALUES(?,?,?,?,?)", (cid, key, r["first"], ip, label))
            learning = (not seeded) or (who is not None and now - who["first"] < cfg["learn_h"] * 3600)
            if not learning and cfg["newdest"]:
                fresh.append(((who["host"] if who else "") or r["client"], label, lvl, r["up"], ip))
    if not seeded:
        _set_meta("newdest_seeded", now)
        return
    for host, label, lvl, up, ip in fresh:
        msg = f"{host} contacted a new destination: {label} — {_mb(up)} sent"
        _event(2 if lvl else 1, "hotspot-newdest", msg, ip)
    if fresh and cfg["notify"] and now - float(_meta("alert_newdest") or 0) > 600:
        _set_meta("alert_newdest", now)
        host, label = fresh[0][0], fresh[0][1]
        more = f" (+{len(fresh) - 1} more)" if len(fresh) > 1 else ""
        _toast("Device Watch: new destination", f"{host} contacted {label}{more}")


# ----------------------------------------------------------------------------------------------- monitor thread
def _monitor():
    _init_db()
    _S["started"] = time.time()
    prev = None  # (adapter, counters, time)
    last_clients = last_keep = last_alert = last_prune = 0.0
    while True:
        t0 = time.time()
        try:
            nic, _ip = _nic()
            _S["on"], _S["nic"] = bool(nic), nic
            if nic:
                io = psutil.net_io_counters(pernic=True).get(nic)
                if io and prev and prev[0] == nic:
                    up, down = max(0, io.bytes_recv - prev[1].bytes_recv), max(0, io.bytes_sent - prev[1].bytes_sent)
                    dt = max(t0 - prev[2], 1e-6)
                    _S["rate"] = (up / dt, down / dt)
                    if up or down:
                        with _db() as c:
                            c.execute("INSERT INTO vol VALUES(?,?,?) ON CONFLICT(ts) DO UPDATE SET up=up+excluded.up, down=down+excluded.down",
                                      (int(t0 // 60) * 60, up, down))
                prev = (nic, io, t0) if io else None
                if t0 - last_clients > 60:
                    last_clients = t0
                    try:
                        _hotspot("status")
                    except Exception:  # noqa: BLE001  (kept in _S["hs_err"]; counting bytes must go on)
                        pass
            else:
                prev = None
                _S["rate"] = (0.0, 0.0)
                if _S["hs"] and _S["hs"].get("state") == "On":
                    _S["hs"] = dict(_S["hs"], state="Off", clients=[])
                if _cfg()["keep_on"] and t0 - last_keep > 90 and t0 - _S["started"] > 20:
                    last_keep = t0
                    try:
                        _hotspot("start")
                    except Exception:  # noqa: BLE001
                        pass
            if t0 - last_alert > 30:
                last_alert = t0
                _check_alerts(t0)
            if t0 - last_prune > 3600:
                last_prune = t0
                with _db() as c:
                    c.execute("DELETE FROM vol WHERE ts < ?", (t0 - 90 * 86400,))
                    c.execute("DELETE FROM flows WHERE last < ?", (t0 - 90 * 86400,))
                    c.execute("DELETE FROM names WHERE last < ?", (t0 - 90 * 86400,))
                    c.execute("DELETE FROM fmin WHERE ts < ?", (t0 - 30 * 86400,))
            _S["err"] = None
        except Exception as e:  # noqa: BLE001
            _S["err"] = f"{type(e).__name__}: {e}"
        time.sleep(max(0.5, SAMPLE_SECS - (time.time() - t0)))


def start_monitor():
    if _STARTED.is_set() or not IS_WIN:
        return
    _STARTED.set()
    threading.Thread(target=_monitor, daemon=True, name="hotspot-monitor").start()


# ----------------------------------------------------------------------------------------------- API
def _sniff_state():
    hb = float(_meta("sniff_hb") or 0)
    return {"sniff": time.time() - hb < 20, "sniff_want": _meta("sniff_want") == "1",
            "sniff_err": _meta("sniff_err") or _meta("pktmon_err") or ""}


def status(q=None):
    if not IS_WIN:
        return {"supported": False}
    start_monitor()
    _init_db()
    if _S["hs"] is None and not _S["hs_err"]:
        try:
            _hotspot("status")
        except Exception:  # noqa: BLE001
            pass
    now = time.time()
    cfg = _cfg()
    burst, day_up, day_down = _sums(now, cfg)
    hs = _S["hs"] or {}
    online = {(c.get("mac") or "").lower() for c in hs.get("clients") or []} if _S["on"] else set()
    with _db() as c:
        clients = [dict(r) for r in c.execute("SELECT * FROM clients ORDER BY last DESC")]
        since = c.execute("SELECT MIN(ts) FROM vol").fetchone()[0]
    for cl in clients:
        cl["online"] = cl["mac"] in online
    alerts = []
    try:
        import device  # noqa: PLC0415
        with device._db() as c:
            alerts = [dict(r) for r in c.execute("SELECT ts, level, msg FROM events WHERE kind IN ('hotspot-upload', 'hotspot-newdest') AND ts > ? ORDER BY ts DESC LIMIT 20",
                                                 (now - 7 * 86400,))]
    except Exception:  # noqa: BLE001
        pass
    up, down = _S["rate"]
    return {"supported": True, "on": _S["on"], "ssid": hs.get("ssid") or "", "pass": hs.get("pass") or "", "band": hs.get("band") or "",
            "uplink": hs.get("uplink") or "", "clients": clients, "online": len(online), "since": since,
            "up_bps": up, "down_bps": down, "up_burst": burst, "up_day": day_up, "down_day": day_down,
            "over_burst": burst >= cfg["burst_mb"] * MB, "over_day": day_up >= cfg["day_mb"] * MB,
            "cfg": cfg, "alerts": alerts, "err": _S["err"] or _S["hs_err"], **_sniff_state()}


def forget(q):
    """Take a device off the Devices list. Its traffic history stays; if it joins the hotspot again it is listed again
    (and reported as a new device)."""
    _init_db()
    mac = (q.get("mac") or "").lower()
    with _db() as c:
        c.execute("DELETE FROM clients WHERE mac=?", (mac,))
    return status()


def start(q=None):
    _init_db()
    _save_cfg(keep_on=True)
    _hotspot("start")
    time.sleep(2)  # the adapter takes a moment to come up
    _S["on"] = bool(_nic()[0])
    return status()


def stop(q=None):
    _init_db()
    _save_cfg(keep_on=False)
    _hotspot("stop")
    _S["on"] = False
    return status()


def settings(q):
    _init_db()
    ch = {}
    for k, lo, hi in (("burst_mb", 0.1, 100000), ("burst_min", 1, 1440), ("day_mb", 0.1, 1000000)):
        if q.get(k) not in (None, ""):
            v = float(q[k])
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo:g} and {hi:g}")
            ch[k] = v
    if q.get("learn_h") not in (None, ""):
        v = float(q["learn_h"])
        if not 0 <= v <= 720:
            raise ValueError("learn_h must be between 0 and 720")
        ch["learn_h"] = v
    for k in ("notify", "newdest"):
        if k in q:
            ch[k] = q[k] == "1"
    if "push" in q:
        t = (q.get("push") or "").strip()
        if t and not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", t):
            raise ValueError("The topic name must be 12-64 letters, digits, - or _ (make it long and hard to guess: anyone who knows it can read your alerts)")
        ch["push"] = t
    return {"cfg": _save_cfg(**ch), "push_err": _PUSH["err"], "push_sent": _PUSH["sent"]}


def volume(q):
    _init_db()
    hours = float(q.get("hours") or 24)
    bucket = 120 if hours <= 6 else 600 if hours <= 24 else 3600 if hours <= 168 else 21600
    now = time.time()
    start_ = int((now - hours * 3600) // bucket) * bucket
    with _db() as c:
        rows = {r[0]: (r[1], r[2]) for r in c.execute(
            f"SELECT CAST(ts/{bucket} AS INT)*{bucket} AS b, SUM(up), SUM(down) FROM vol WHERE ts >= ? GROUP BY b", (start_,))}
    ts = list(range(start_, int(now) + 1, bucket))
    return {"bucket": bucket, "t": ts, "sent": [rows.get(t, (0, 0))[0] for t in ts], "recv": [rows.get(t, (0, 0))[1] for t in ts],
            "tot_sent": sum(v[0] for v in rows.values()), "tot_recv": sum(v[1] for v in rows.values())}


def flows(q):
    """Where the hotspot devices sent data (needs the elevated capture helper to have been running)."""
    _init_db()
    import device  # noqa: PLC0415
    hours = float(q.get("hours") or 24)
    since = time.time() - hours * 3600
    with _db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT client, rip, SUM(up) up, SUM(down) down, SUM(pkts) pkts, MIN(first) first, MAX(last) last, "
            "GROUP_CONCAT(DISTINCT proto || '/' || rport) ports FROM flows WHERE last >= ? GROUP BY client, rip ORDER BY up DESC", (since,))]
        names = {}
        for r in c.execute("SELECT name, ip FROM names WHERE ip <> '' ORDER BY last"):
            names[r["ip"]] = r["name"]
        asked = [dict(r) for r in c.execute(
            "SELECT name, client, MIN(first) first, MAX(last) last, SUM(CASE WHEN ip='' THEN n ELSE 0 END) n, "
            # this PC's own look-ups on the hotspot adapter (client = the hotspot address) are not a device's
            "GROUP_CONCAT(DISTINCT ip) ips FROM names WHERE last >= ? AND client <> ? GROUP BY name, client ORDER BY last DESC LIMIT 500",
            (since, HOTSPOT_IP))]
        cl = {r["ip"]: (r["host"] or r["mac"]) for r in c.execute("SELECT * FROM clients WHERE ip <> ''")}
    with device._db() as c:
        info, _sigs, dnames = device._maps(c)
    pend = False

    def look(ip, host=""):
        """Owner / country / flag of one address; queues it for an RDAP lookup if it is new."""
        nonlocal pend
        if ip not in info and device._ip_kind(ip) == "public":
            device._PENDING_IPS.add(ip)
            pend = True
        d = device._dest(ip, info, {**dnames, **names} if not host else {ip: host})
        hay = f"{d['org']} {(info.get(ip) or {}).get('net') or ''} {d['host']}"
        if _CN_CLOUD.search(hay):  # a Chinese company's cloud, even when the servers sit in the US
            # the address registry often lists a contact person ("James Tian") instead of the company
            d["org"] = _cn_label(_CN_CLOUD.search(hay).group(0))
            if d["lvl"] < 2:
                d["lvl"] = 1
                d["why"] = list(d["why"]) + ["Hosted on a Chinese company's cloud (" + d["org"] + ")"]
                d["cat"] = "Chinese cloud provider"
        return d

    for a in asked:
        ips = [i for i in (a.pop("ips") or "").split(",") if i]
        a["ips"] = ips[:8]
        d = look(ips[0], a["name"]) if ips else {"org": "", "country": "", "cat": "", "lvl": 0, "why": []}
        if _CN_CLOUD.search(a["name"]) and d["lvl"] < 1:
            d["lvl"], d["why"] = 1, ["Domain of a Chinese company's cloud"]
            d["org"] = _cn_label(_CN_CLOUD.search(a["name"]).group(0))
        a.update(org=d["org"], country=d["country"], cat=d["cat"], lvl=d["lvl"], why=d["why"])
    dests = []
    for r in rows:
        d = look(r["rip"])
        if d["kind"] == "public" and r["rip"] not in names and r["up"]:
            # no DNS answer ever pointed here: the address is built into the firmware (or came from its own cloud)
            d["lvl"] = max(d["lvl"], 1)
            d["why"] = list(d["why"]) + ["Contacted by raw IP address: no domain lookup for it was seen (or it was looked up before tracking began)"]
        d.update(client=cl.get(r["client"]) or r["client"], up=r["up"], down=r["down"], pkts=r["pkts"], first=r["first"], last=r["last"],
                 ports=sorted(set((r["ports"] or "").split(",")))[:8])
        dests.append(d)
    if pend:
        device.start_collector()
        device._ENRICH_EV.set()
    orgs = {}
    for d in dests:
        key = d["org"] or d["host"] or d["ip"]
        o = orgs.setdefault(key, {"org": key, "country": d["country"], "cat": d["cat"], "lvl": 0, "why": [], "up": 0, "down": 0, "hosts": set(),
                                  "clients": set(), "last": 0})
        o["up"] += d["up"]
        o["down"] += d["down"]
        o["lvl"] = max(o["lvl"], d["lvl"])
        o["why"] = sorted(set(o["why"]) | set(d["why"]))
        o["hosts"].add(d["host"] or d["ip"])
        o["clients"].add(d["client"])
        o["last"] = max(o["last"], d["last"])
    olist = sorted(orgs.values(), key=lambda o: -o["up"])
    for o in olist:
        o["hosts"] = sorted(o["hosts"])[:12]
        o["clients"] = sorted(o["clients"])
    try:
        blocks = blocks_status()
    except Exception:  # noqa: BLE001
        blocks = []
    blocked = {b["name"] for b in blocks}
    for a in asked:
        a["client"] = cl.get(a["client"]) or a["client"]
        a["blocked"] = a["name"] in blocked
    return {"orgs": olist, "dests": dests[:500], "names": asked, "flagged": sum(1 for o in olist if o["lvl"] >= 2),
            "blocks": blocks, "block_err": _meta("block_err") or "", **_sniff_state()}


def insights(q):
    """Idle baseline, bursts of upload (a cleaning run, a camera session, something unexplained) and what happens overnight."""
    _init_db()
    import device  # noqa: PLC0415
    days = min(float(q.get("days") or 7), 30)
    now = time.time()
    since = now - days * 86400
    with _db() as c:
        vol = [tuple(r) for r in c.execute("SELECT ts, up, down FROM vol WHERE ts >= ? ORDER BY ts", (since,))]
        fm = [tuple(r) for r in c.execute("SELECT ts, rip, up FROM fmin WHERE ts >= ? ORDER BY ts", (since,))]
        names = {r["ip"]: r["name"] for r in c.execute("SELECT name, ip FROM names WHERE ip <> '' ORDER BY last")}
        first_client = c.execute("SELECT MIN(first) FROM clients").fetchone()[0]
    with device._db() as c:
        info = {r["ip"]: dict(r) for r in c.execute("SELECT * FROM ipinfo")}
    ups = sorted(u for _t, u, _d in vol if u)
    base = ups[len(ups) // 2] if ups else 0  # a typical minute in which anything was sent at all
    thr = min(max(base * 5, 50 * 1024), 2 * MB)  # capped: a device that streams all day must still show up as bursts
    bursts, cur = [], None
    for ts, up, down in vol:
        if up >= thr:
            if cur and ts - cur["end"] <= 180:
                cur.update(end=ts, up=cur["up"] + up, down=cur["down"] + down, peak=max(cur["peak"], up))
            else:
                cur = {"start": ts, "end": ts, "up": up, "down": down, "peak": up}
                bursts.append(cur)
    for b in bursts:
        to = {}
        for ts, rip, up in fm:
            if b["start"] - 60 <= ts <= b["end"] + 60:
                to[rip] = to.get(rip, 0) + up
        b["to"] = [{"ip": ip, "host": names.get(ip) or "",
                    "org": (_cn_label(_CN_CLOUD.search(f"{(info.get(ip) or {}).get('net') or ''} {names.get(ip) or ''}").group(0))
                            if _CN_CLOUD.search(f"{(info.get(ip) or {}).get('net') or ''} {names.get(ip) or ''}")
                            else (info.get(ip) or {}).get("org") or ""),
                    "country": (info.get(ip) or {}).get("country") or "", "up": up}
                   for ip, up in sorted(to.items(), key=lambda x: -x[1])[:4]]
        b["end"] += 60
    nights = {}
    for ts, up, down in vol:
        lt = time.localtime(ts)
        if lt.tm_hour < 6:
            n = nights.setdefault(time.strftime("%Y-%m-%d", lt), {"up": 0, "down": 0, "peak": 0, "minutes": 0})
            n["up"] += up
            n["down"] += down
            n["peak"] = max(n["peak"], up)
            n["minutes"] += 1 if up else 0
    return {"baseline": base, "threshold": thr, "bursts": bursts[::-1][:60], "first_client": first_client,
            "nights": [dict(v, date=k) for k, v in sorted(nights.items(), reverse=True)]}


def _helper_cmd():
    """(program, arguments) that run this app as the capture helper."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--hs-sniff"
    py = Path(sys.executable)
    pw = py.with_name("pythonw.exe")
    return str(pw if pw.is_file() else py), f'"{Path(__file__).resolve().parent / "main.py"}" --hs-sniff'


def sniff(q):
    """Turn destination tracking on (UAC prompt: starts the elevated helper and its log-on task) or off."""
    if not IS_WIN:
        raise RuntimeError("Needs Windows")
    _init_db()
    if q.get("on") == "1":
        STOP_FLAG.unlink(missing_ok=True)
        _set_meta("sniff_err", "")
        exe, args = _helper_cmd()
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args + " --install", str(Path(exe).parent), 0)
        if rc <= 32:
            raise RuntimeError("Windows did not start the capture helper (administrator prompt declined?)")
        return {"ok": True}
    STOP_FLAG.write_text("stop", encoding="utf-8")  # the helper sees this within seconds, removes its task and exits
    return {"ok": True}


# ----------------------------------------------------------------------------------------------- capture helper (elevated)
def _dns_name(p, off):
    labels, end, jumped, hops = [], off, False, 0
    while True:
        n = p[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:  # compression pointer
            if not jumped:
                end = off + 2
            off = ((n & 0x3F) << 8) | p[off + 1]
            jumped = True
            hops += 1
            if hops > 10:
                raise ValueError("pointer loop")
            continue
        labels.append(p[off + 1:off + 1 + n].decode("ascii", "replace"))
        off += 1 + n
    return ".".join(labels).lower(), (end if jumped else off)


def _dns_parse(p):
    """DNS message -> (name asked for, [IPv4 answers]). Answers are empty for a query."""
    try:
        if int.from_bytes(p[4:6], "big") < 1:
            return None, []
        name, off = _dns_name(p, 12)
        off += 4
        ips = []
        if p[2] & 0x80:
            for _ in range(min(int.from_bytes(p[6:8], "big"), 30)):
                _n, off = _dns_name(p, off)
                typ, _cls, _ttl, rdl = struct.unpack("!HHIH", p[off:off + 10])
                off += 10
                if typ == 1 and rdl == 4:
                    ips.append(socket.inet_ntoa(p[off:off + 4]))
                off += rdl
        return name, ips
    except Exception:  # noqa: BLE001
        return None, []


_CAP_LOCK = threading.Lock()  # the raw-socket loop and the pktmon reader both add to the same tables


def _account(src, dst, sp, dp, proto, n, me, net3, flows_):
    """Add one packet (addresses as 4 raw bytes) to the per-destination byte counts."""
    s_in, d_in = src[:3] == net3, dst[:3] == net3
    if s_in and not d_in and src != me:
        if dst[0] >= 224:  # multicast / broadcast chatter never leaves the hotspot
            return
        key, i = (src, dst, dp, proto), 0
    elif d_in and not s_in and dst != me and dst[3] != 255:
        key, i = (dst, src, sp, proto), 1
    else:
        return
    with _CAP_LOCK:
        f = flows_.setdefault(key, [0, 0, 0])
        f[i] += n
        f[2] += 1


def _packet(data, me, net3, flows_, names_, count=True):
    """One IPv4 packet from the raw socket. Only headers are looked at. Windows hands a raw socket the packets
    addressed to this PC (the gadget's DNS questions) but NOT the ones it merely forwards, so byte counts per
    destination come from pktmon instead (count=False) whenever that is running."""
    if len(data) < 20 or data[0] >> 4 != 4:
        return
    ihl = (data[0] & 15) * 4
    proto = data[9]
    src, dst = data[12:16], data[16:20]
    n = max(len(data), int.from_bytes(data[2:4], "big"))
    s_in, d_in = src[:3] == net3, dst[:3] == net3
    sp = dp = 0
    if proto in (6, 17) and not (int.from_bytes(data[6:8], "big") & 0x1FFF) and len(data) >= ihl + 4:
        sp, dp = struct.unpack("!HH", data[ihl:ihl + 4])
    if proto == 17 and ((dp == 53 and dst == me and s_in) or (sp == 53 and src == me and d_in)):
        name, ips = _dns_parse(data[ihl + 8:])
        if name:
            client = src if dp == 53 else dst
            with _CAP_LOCK:
                for ip in ips or [""]:
                    k = (name, ip, client)
                    names_[k] = names_.get(k, 0) + 1
    elif count:
        _account(src, dst, sp, dp, proto, n, me, net3, flows_)


# pktmon (built into Windows 10/11) sees every frame on every adapter, forwarded ones included. Its real-time mode
# prints two lines per packet: a header with "OriginalSize N", then a tcpdump-style line "IP.port > IP.port: ...".
_PM_SIZE = re.compile(r"OriginalSize (\d+)")
_PM_TS = re.compile(r"(\d\d):(\d\d):(\d\d\.\d+)")
_PM_IP = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})(?:\.(\d{1,5}))? > (\d{1,3}(?:\.\d{1,3}){3})(?:\.(\d{1,5}))?:(.*)")
_PM = {"proc": None, "lines": 0, "pkts": 0}


def _pm_line(line, st, me, net3, flows_):
    m = _PM_SIZE.search(line)
    if m:
        st["size"] = int(m.group(1))
        t = _PM_TS.match(line)
        st["ts"] = int(t.group(1)) * 3600 + int(t.group(2)) * 60 + float(t.group(3)) if t else None
        return
    m = _PM_IP.search(line)
    if not m or not st.get("size"):
        return
    if st.get("ts") is not None:
        # A frame from the gadget is logged twice within microseconds, under different packet ids: once by the Wi-Fi
        # card and once by the hotspot's virtual adapter on top of it. Identical text that close together = same frame.
        seen = st.setdefault("seen", {})
        prev = seen.get(m.group(0))
        seen[m.group(0)] = st["ts"]
        if len(seen) > 4000:
            seen.clear()
        if prev is not None and 0 <= st["ts"] - prev < 0.005:
            st["size"] = 0
            return
    rest = m.group(5)
    proto = 6 if ("Flags [" in rest or "tcp" in rest.lower()) else 17 if "UDP" in rest.upper() else 1 if "ICMP" in rest.upper() else 0
    try:
        src, dst = socket.inet_aton(m.group(1)), socket.inet_aton(m.group(3))
    except OSError:
        return
    _account(src, dst, int(m.group(2) or 0), int(m.group(4) or 0), proto, st["size"], me, net3, flows_)
    st["size"] = 0  # one count per packet, even if it is logged again further along
    _PM["pkts"] += 1


def _pm_run(*args):
    try:
        r = subprocess.run(["pktmon", *args], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def _pm_reader(ip, flows_):
    me = socket.inet_aton(ip)
    net3 = me[:3]
    _pm_run("stop")
    _pm_run("filter", "remove")
    rc, out = _pm_run("filter", "add", "DeviceWatchHotspot", "-i", ip.rsplit(".", 1)[0] + ".0/24")
    _set_meta("pktmon_filter", f"{rc} {out[:200]}")  # without the filter it still works, just reads this PC's own packets too
    try:
        p = subprocess.Popen(["pktmon", "start", "--capture", "--comp", "nics", "--pkt-size", "96", "-m", "real-time"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    except OSError as e:
        _set_meta("pktmon_err", f"could not start pktmon: {e}")
        return
    _PM.update(proc=p, lines=0, pkts=0)
    st = {}
    sample = []
    for raw in p.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        _PM["lines"] += 1
        if len(sample) < 120:  # what pktmon actually printed, for troubleshooting (headers only, no contents)
            sample.append(line)
            if len(sample) in (10, 40, 120):
                try:
                    (HS_DIR / "pktmon-sample.txt").write_text("\n".join(sample), encoding="utf-8")
                except OSError:
                    pass
        try:
            _pm_line(line, st, me, net3, flows_)
        except Exception:  # noqa: BLE001
            pass
    if _PM["proc"] is p:  # ended by itself, not by _pm_stop
        _set_meta("pktmon_err", "pktmon stopped: " + " | ".join(sample[:3])[:300])
        _PM["proc"] = None


def _pm_stop():
    p, _PM["proc"] = _PM["proc"], None
    _pm_run("stop")
    if p is not None:
        try:
            p.kill()
        except OSError:
            pass


def _flush(flows_, names_):
    now = time.time()
    day = time.strftime("%Y-%m-%d")
    pn = {6: "TCP", 17: "UDP", 1: "ICMP"}
    with _CAP_LOCK:
        fl, nm = dict(flows_), dict(names_)
        flows_.clear()
        names_.clear()
    with _db() as c:
        for (client, rip, port, proto), (up, down, pk) in fl.items():
            c.execute("INSERT INTO flows VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(day, client, rip, rport, proto) DO UPDATE SET "
                      "up=up+excluded.up, down=down+excluded.down, pkts=pkts+excluded.pkts, last=excluded.last",
                      (day, socket.inet_ntoa(client), socket.inet_ntoa(rip), port, pn.get(proto, str(proto)), up, down, pk, now, now))
        per_min = {}
        for (client, rip, _port, _proto), (up, _down, _pk) in fl.items():
            if up:
                k = (socket.inet_ntoa(client), socket.inet_ntoa(rip))
                per_min[k] = per_min.get(k, 0) + up
        for (client, rip), up in per_min.items():  # minute-by-minute, so a burst can be tied to where it went
            c.execute("INSERT INTO fmin VALUES(?,?,?,?) ON CONFLICT(ts, client, rip) DO UPDATE SET up=up+excluded.up",
                      (int(now // 60) * 60, client, rip, up))
        for (name, ip, client), n in nm.items():
            c.execute("INSERT INTO names VALUES(?,?,?,?,?,?) ON CONFLICT(name, ip, client) DO UPDATE SET last=excluded.last, n=n+excluded.n",
                      (name, ip, socket.inet_ntoa(client), now, now, n))
        c.execute("INSERT OR REPLACE INTO meta VALUES('sniff_hb', ?)", (str(now),))
        c.execute("INSERT OR REPLACE INTO meta VALUES('pktmon', ?)", (json.dumps({"on": _PM["proc"] is not None, "lines": _PM["lines"], "pkts": _PM["pkts"]}),))
    try:
        _block_watch([(name, ip, client) for (name, ip, client) in nm])
    except Exception as e:  # noqa: BLE001
        _set_meta("block_err", f"helper could not extend a block: {str(e)[:200]}")


def _capture(sock, ip):
    me = socket.inet_aton(ip)
    net3 = me[:3]
    flows_, names_ = {}, {}
    _set_meta("pktmon_err", "")
    threading.Thread(target=_pm_reader, args=(ip, flows_), daemon=True, name="pktmon-reader").start()
    last = started = time.time()
    try:
        while True:
            try:
                # if pktmon could not start, fall back to counting what the raw socket does see
                _packet(sock.recv(65535), me, net3, flows_, names_, count=_PM["proc"] is None and time.time() - started > 15)
            except socket.timeout:
                pass
            except OSError:
                return
            if time.time() - last >= 5:
                last = time.time()
                _flush(flows_, names_)
                if STOP_FLAG.exists() or RESTART_FLAG.exists() or _nic()[1] != ip:
                    return
    finally:
        _pm_stop()
        _flush(flows_, names_)


PROTECTED_DIR = Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / "DeviceWatch"


def _protect():
    """Copy the built app into Program Files and return the copy's exe. The log-on task runs as administrator, so the
    program it starts must sit where only an administrator can change it; the build folder on the Desktop is writable
    by the normal account, which would let anything running as that account swap in its own code and gain admin."""
    import shutil  # noqa: PLC0415
    src = Path(sys.executable).resolve().parent
    if src == PROTECTED_DIR.resolve():
        return Path(sys.executable)
    tmp = PROTECTED_DIR.with_name(PROTECTED_DIR.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    old = PROTECTED_DIR.with_name(PROTECTED_DIR.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if PROTECTED_DIR.exists():
        PROTECTED_DIR.rename(old)
    tmp.rename(PROTECTED_DIR)
    shutil.rmtree(old, ignore_errors=True)
    return PROTECTED_DIR / Path(sys.executable).name


def _task(install):
    if install:
        exe, args = _helper_cmd()
        if getattr(sys, "frozen", False):
            exe = str(_protect())
        q = lambda s: s.replace("'", "''")  # noqa: E731
        script = (f"$a = New-ScheduledTaskAction -Execute '{q(exe)}' -Argument '{q(args)}' -WorkingDirectory '{q(str(Path(exe).parent))}'; "
                  "$u = [Security.Principal.WindowsIdentity]::GetCurrent().Name; "
                  "$t = New-ScheduledTaskTrigger -AtLogOn -User $u; "
                  "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
                  "$p = New-ScheduledTaskPrincipal -UserId $u -RunLevel Highest -LogonType Interactive; "
                  f"Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null")
    else:
        script = f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false"
    _out, err = _ps(script, 60)
    return err.strip()


# ----------------------------------------------------------------------------------------------- blocking a destination
# A hotspot device can be stopped from reaching one domain name. Two layers, both set by an elevated one-off the user
# approves (UAC) each time:
#   1. a hosts-file line, so the hotspot's own name service answers "0.0.0.0" for the name;
#   2. a dead-end route for the addresses the name is known to lead to (covers a device that remembered the address).
# The list lives where only an administrator can write, because the always-on helper acts on it: when it sees the name
# resolve to an address it has not dead-ended yet, it adds that route.
BLOCK_FILE = PROTECTED_DIR.with_name("DeviceWatchData") / "blocks.json"
HOSTS_FILE = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "drivers" / "etc" / "hosts"
BLOCK_TAG = "# DeviceWatch block"
_NAME_OK = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


def _blocks():
    try:
        d = json.loads(BLOCK_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _blocks_save(d):
    BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BLOCK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, BLOCK_FILE)


def block_lines():
    """The hosts-file lines that are ours (the intrusion checks must not report them as a hijack)."""
    return {f"0.0.0.0 {n} {BLOCK_TAG}" for n in _blocks()}


def _public(ip):
    import ipaddress  # noqa: PLC0415
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _route(ip, add):
    """Dead-end one address (a route into the loopback adapter, kept across restarts) or undo that. Returns an error text."""
    if not _public(ip):
        return "not a public address"
    if add:
        s = ("$ErrorActionPreference='Stop'; $i=(Get-NetIPInterface -AddressFamily IPv4 | ?{$_.InterfaceAlias -like 'Loopback*'} | select -First 1).ifIndex; "
             f"if(-not (Get-NetRoute -DestinationPrefix '{ip}/32' -InterfaceIndex $i -ErrorAction SilentlyContinue)){{ "
             f"try {{ New-NetRoute -DestinationPrefix '{ip}/32' -InterfaceIndex $i -NextHop 0.0.0.0 -RouteMetric 1 | Out-Null }} "
             f"catch {{ $r = route -p add {ip} mask 255.255.255.255 0.0.0.0 if $i metric 1 2>&1; if($LASTEXITCODE -ne 0 -or \"$r\" -notmatch 'OK'){{ throw \"$_ / $r\" }} }} }}")
    else:
        s = (f"Get-NetRoute -DestinationPrefix '{ip}/32' -ErrorAction SilentlyContinue | ?{{$_.InterfaceAlias -like 'Loopback*'}} | "
             "Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue")
    _out, err = _ps(s, 60)
    return err.strip()[:200]


def _sent_to(ips):
    """{address: [bytes, packets]} the hotspot devices have sent to these addresses, all time."""
    ips = list(ips)
    if not ips:
        return {}
    with _db() as c:
        return {r[0]: [r[1] or 0, r[2] or 0] for r in c.execute(
            f"SELECT rip, SUM(up), SUM(pkts) FROM flows WHERE rip IN ({','.join('?' * len(ips))}) GROUP BY rip", ips)}


def block_main(argv):
    """`MarketTerminal.exe --hs-block <name>` / `--hs-unblock <name>` (run as administrator)."""
    _init_db()
    on = "--hs-block" in argv
    try:
        name = argv[argv.index("--hs-block" if on else "--hs-unblock") + 1].strip().lower().rstrip(".")
    except IndexError:
        name = ""
    if not ctypes.windll.shell32.IsUserAnAdmin():
        _set_meta("block_err", "the block was started without administrator rights")
        return
    if not _NAME_OK.match(name):
        _set_meta("block_err", "that is not a domain name")
        return
    errs = []
    blocks = _blocks()
    line = f"0.0.0.0 {name} {BLOCK_TAG}"
    try:
        text = HOSTS_FILE.read_bytes().decode("utf-8", "surrogateescape") if HOSTS_FILE.exists() else ""
        keep = [x for x in text.splitlines() if x.strip() != line]
        if on:
            keep.append(line)
        HOSTS_FILE.write_bytes(("\r\n".join(keep) + "\r\n").encode("utf-8", "surrogateescape"))
    except OSError as e:
        errs.append(f"hosts file: {e}")
    if on:
        with _db() as c:
            mine = {r[0] for r in c.execute("SELECT DISTINCT ip FROM names WHERE name=? AND ip <> ''", (name,))}
            other = {}
            for r in c.execute("SELECT DISTINCT name, ip FROM names WHERE name <> ? AND ip <> ''", (name,)):
                if r[1] in mine and r[0] not in blocks:
                    other.setdefault(r[1], r[0])
        # an address that another, unblocked name also leads to is left alone: dead-ending it would break that too
        ips = sorted(ip for ip in mine if ip not in other and _public(ip))
        base, routed = _sent_to(ips), []
        for ip in ips:
            e = _route(ip, True)
            if e:
                errs.append(f"route for {ip}: {e}")
            else:
                routed.append(ip)
        blocks[name] = {"ts": time.time(), "ips": routed, "base": {ip: base.get(ip, [0, 0]) for ip in routed},
                        "shared": sorted(ip for ip in mine if ip in other)}
    else:
        b = blocks.pop(name, None) or {}
        still = {ip for x in blocks.values() for ip in x.get("ips") or []}
        for ip in b.get("ips") or []:
            if ip not in still:
                _route(ip, False)
    try:
        _blocks_save(blocks)
    except OSError as e:
        errs.append(f"could not save the block list: {e}")
    try:
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except Exception:  # noqa: BLE001
        pass
    _set_meta("block_err", "; ".join(errs)[:400])
    _set_meta("block_done", time.time())


_BLK = {"t": 0.0, "blocks": {}, "seen": {}}  # helper: the block list (re-read now and then) and address -> names seen answering


def _block_watch(nm):
    """Helper, after each flush: a blocked name that resolved to an address not dead-ended yet gets its route now."""
    now = time.time()
    if now - _BLK["t"] > 20:
        _BLK["t"], _BLK["blocks"] = now, _blocks()
    seen, blocks = _BLK["seen"], _BLK["blocks"]
    for (name, ip, _client) in nm:
        if ip and (ip in seen or len(seen) < 20000):
            seen.setdefault(ip, set()).add(name)
    if not blocks:
        return
    changed = False
    for (name, ip, _client) in nm:
        b = blocks.get(name)
        if not b or not ip or ip in (b.get("ips") or []) or not _public(ip) or len(b.get("ips") or []) >= 64:
            continue
        if not seen.get(ip, set()) <= set(blocks):  # also the address of something that is not blocked
            continue
        if not _route(ip, True):
            b.setdefault("ips", []).append(ip)
            b.setdefault("base", {})[ip] = _sent_to([ip]).get(ip, [0, 0])
            b["leak"] = now  # the name still resolved to a real address: the hosts-file layer is not what is stopping it
            changed = True
    if changed:
        fresh = _blocks()  # only names still blocked: never bring back one that was unblocked meanwhile
        for n in list(fresh):
            if n in blocks:
                fresh[n] = blocks[n]
        _blocks_save(fresh)


def block(q):
    """UI: block (on=1) or unblock a domain name for the hotspot devices. One UAC prompt."""
    if not IS_WIN:
        raise RuntimeError("Needs Windows")
    _init_db()
    name = (q.get("name") or "").strip().lower().rstrip(".")
    if not _NAME_OK.match(name):
        raise ValueError("That is not a domain name")
    flag = "--hs-block" if q.get("on", "1") == "1" else "--hs-unblock"
    _set_meta("block_err", "")
    exe, args = _helper_cmd()
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args.replace("--hs-sniff", f"{flag} {name}"), str(Path(exe).parent), 0)
    if rc <= 32:
        raise RuntimeError("Windows did not make the change (administrator prompt declined?)")
    return {"ok": True}


def blocks_status():
    """The blocked names, with what has been seen since: is the device still trying, and is anything getting through."""
    out = []
    for name, b in sorted(_blocks().items()):
        ips, base, ts = b.get("ips") or [], b.get("base") or {}, float(b.get("ts") or 0)
        cur = _sent_to(ips)
        up = sum(max(0, cur.get(ip, [0, 0])[0] - (base.get(ip) or [0, 0])[0]) for ip in ips)
        pk = sum(max(0, cur.get(ip, [0, 0])[1] - (base.get(ip) or [0, 0])[1]) for ip in ips)
        with _db() as c:
            asked = c.execute("SELECT MAX(last) FROM names WHERE name=? AND client <> ?", (name, HOTSPOT_IP)).fetchone()[0] or 0
            sunk = c.execute("SELECT MAX(last) FROM names WHERE name=? AND ip='0.0.0.0' AND client <> ?", (name, HOTSPOT_IP)).fetchone()[0] or 0
            real = c.execute("SELECT MAX(last) FROM names WHERE name=? AND ip NOT IN ('', '0.0.0.0') AND client <> ?", (name, HOTSPOT_IP)).fetchone()[0] or 0
        # a refused connection is only the opening packet, repeated (about 70 bytes each); a real exchange averages far more
        if pk and up / pk > 110:
            state = "through"
        elif pk or sunk > ts:
            state = "blocked"
        elif asked > ts:
            state = "asked"
        else:
            state = "quiet"
        out.append({"name": name, "ts": ts, "ips": ips, "shared": b.get("shared") or [], "up": up, "pkts": pk, "state": state,
                    "asked": asked if asked > ts else 0, "hosts_works": sunk > ts, "real_answer": real if real > ts + 60 else 0})
    return out


def sniff_main(argv):
    """Entry point of the elevated helper: `MarketTerminal.exe --hs-sniff [--install]`."""
    if not IS_WIN:
        return
    _init_db()
    if not ctypes.windll.shell32.IsUserAnAdmin():
        _set_meta("sniff_err", "the capture helper was started without administrator rights")
        return
    k32 = ctypes.windll.kernel32
    name = "Global\\DeviceWatchHotspotCapture"
    if "--install" in argv:  # an install replaces the files the running helper runs from: ask it to step aside first
        h = k32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
        if h:
            k32.CloseHandle(h)
            RESTART_FLAG.write_text("install", encoding="utf-8")
            for _ in range(40):
                time.sleep(1)
                h = k32.OpenMutexW(0x00100000, False, name)
                if not h:
                    break
                k32.CloseHandle(h)
            time.sleep(2)  # let pktmon and the old process wind down before its files are replaced
    mutex = k32.CreateMutexW(None, False, name)
    if k32.GetLastError() == 183:  # already running
        return
    if "--install" in argv:
        try:
            err = _task(True)
        except OSError as e:
            err = f"could not copy the helper into {PROTECTED_DIR}: {e}"
        _set_meta("sniff_err", ("could not create the log-on task: " + err[:200]) if err else "")
        if getattr(sys, "frozen", False) and Path(sys.executable).resolve().parent != PROTECTED_DIR.resolve() \
                and (PROTECTED_DIR / Path(sys.executable).name).is_file():
            # hand over to the protected copy now rather than at the next log-on
            _set_meta("sniff_want", "1")
            ctypes.windll.kernel32.CloseHandle(mutex)
            _ps(f"Start-ScheduledTask -TaskName '{TASK}'", 30)
            return
    _set_meta("sniff_want", "1")
    RESTART_FLAG.unlink(missing_ok=True)
    try:  # decoy-file watcher (Device Watch > TRAPS): reading the Security log needs this process's admin rights
        import honey  # noqa: PLC0415
        threading.Thread(target=honey.watch_loop, daemon=True, name="honey-watch").start()
    except Exception as e:  # noqa: BLE001
        _set_meta("sniff_err", f"decoy watcher did not start: {e}")
    while True:
        if RESTART_FLAG.exists():  # an app update: step aside, keep the log-on task (it starts the new version)
            RESTART_FLAG.unlink(missing_ok=True)
            _set_meta("sniff_hb", "0")
            return
        if STOP_FLAG.exists():
            _task(False)
            _set_meta("sniff_want", "0")
            _set_meta("sniff_hb", "0")
            STOP_FLAG.unlink(missing_ok=True)
            return
        _nicname, ip = _nic()
        if not ip:  # hotspot is off: stay alive (heartbeat), wait for it
            _set_meta("sniff_hb", time.time())
            time.sleep(5)
            continue
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
            sock.bind((ip, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
            sock.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)
            sock.settimeout(2)
            _set_meta("sniff_err", "")
            _capture(sock, ip)
        except OSError as e:
            _set_meta("sniff_err", f"could not listen on the hotspot adapter: {e}")
            time.sleep(10)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
