"""Device Watch > TRAPS > intrusion checks: things an intruder or malware changes on the way in, watched continuously.

Runs inside the elevated helper next to the decoy watcher (honey.watch_loop):
  * state_loop: every 90 s takes a snapshot of security-relevant settings and compares it with what was there before:
    Defender / firewall / auditing switched off or given exclusions (tampering), new accounts or administrators, Remote
    Desktop and remote-control tools, programs newly accepting incoming connections, hosts file / DNS / proxy / trusted
    certificates (traffic interception), new keyboard-type and storage devices, new browser extensions, and which
    programs use the camera and microphone. The first snapshot is the baseline; only later changes are reported.
  * poll_events: reads program starts (Security event 4688) and flags the known bad patterns (a document or browser
    starting a command tool, PowerShell downloading and running code, Windows tools misused to download, protection
    being switched off from the command line, unsigned programs run from temp / download folders), plus the Security
    log being cleared (1102). A flagged program is also followed (honey.start_follow).
Findings go to sentry_events in honey.db; the normal collector (alerts(), called from honey.check) turns them into
ALERTS-tab entries and Windows notifications.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import sys
import time
from pathlib import Path

import honey as H
import hotspot as _hs

STATE_SECS = 90
REMOTE_TOOLS = re.compile(r"anydesk|teamviewer|rustdesk|todesk|sunlogin|oray|splashtop|logmein|\bvnc|screenconnect|connectwise|ammyy|radmin|"
                          r"supremo|aweray|awesun|parsec|chrome remote desktop|meshcentral|mesh agent|getscreen|dwservice|zoho assist|netsupport|"
                          r"atera|ninjarmm|ninjaone|action1|simplehelp|remote utilities|ultraviewer|hoptodesk", re.I)
KNOWN_DNS = {"1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9", "149.112.112.112", "208.67.222.222", "208.67.220.220",
             "2606:4700:4700::1111", "2606:4700:4700::1001", "2001:4860:4860::8888", "2001:4860:4860::8844"}

SNAP_PS = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$ErrorActionPreference='SilentlyContinue'
$o=[ordered]@{}
$mp=Get-MpComputerStatus
if($mp){ $o.defender=[ordered]@{rt=$mp.RealTimeProtectionEnabled; av=$mp.AntivirusEnabled; tamper=$mp.IsTamperProtected; behavior=$mp.BehaviorMonitorEnabled; ioav=$mp.IoavProtectionEnabled} }
$pf=Get-MpPreference
if($pf){ $o.exclusions=@(@($pf.ExclusionPath)+@($pf.ExclusionProcess)+@($pf.ExclusionExtension)+@($pf.ExclusionIpAddress) | ?{ $_ -and $_ -notmatch '^N/A' } | %{[string]$_}) }
$o.firewall=[ordered]@{}; Get-NetFirewallProfile | %{ $o.firewall[[string]$_.Name]=([string]$_.Enabled -eq 'True') }
$a=@(auditpol /get /subcategory:"{0CCE921D-69AE-11D9-BED3-505054503030}","{0CCE9215-69AE-11D9-BED3-505054503030}","{0CCE922B-69AE-11D9-BED3-505054503030}" /r)
if($LASTEXITCODE -eq 0){ $o.audit=[ordered]@{}; $a | Select-Object -Skip 1 | ?{$_} | %{ $f=$_.Split(','); if($f.Count -ge 5){ $o.audit[$f[3]]=$f[4] } } }
$o.users=@(Get-LocalUser | ?{$_.Enabled} | %{[string]$_.Name})
$o.admins=@(Get-LocalGroupMember -SID 'S-1-5-32-544' | %{[string]$_.Name})
$o.rdp=((Get-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server').fDenyTSConnections -eq 0)
$o.remote_svcs=@(Get-Service WinRM,sshd,RemoteRegistry | ?{$_.Status -eq 'Running'} | %{[string]$_.Name})
$o.programs=@(Get-ItemProperty HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*,HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*,HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\* | ?{$_.DisplayName} | %{[string]$_.DisplayName} | Sort-Object -Unique)
$o.hosts=@(Get-Content "$env:SystemRoot\System32\drivers\etc\hosts" | %{$_.Trim()} | ?{ $_ -and -not $_.StartsWith('#') })
$o.dns=@(Get-DnsClientServerAddress | %{$_.ServerAddresses} | ?{ $_ -and $_ -notmatch '^fec0:' } | Sort-Object -Unique)
$is=Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$o.proxy="on=$([int]$is.ProxyEnable); server=$($is.ProxyServer); script=$($is.AutoConfigURL)"
$o.winhttp=((netsh winhttp show proxy) -join ' ' -replace '\s+',' ').Trim()
$auth=@{}; Get-ChildItem Cert:\LocalMachine\AuthRoot | %{ $auth[$_.Thumbprint]=1 }
$o.roots=@(Get-ChildItem Cert:\LocalMachine\Root,Cert:\CurrentUser\Root | ?{ -not $auth[$_.Thumbprint] } | %{ "$($_.Thumbprint) $($_.Subject)" } | Sort-Object -Unique)
$o.pnp=@(Get-PnpDevice -PresentOnly -Class Keyboard,DiskDrive,Mouse,Net | ?{ $_.InstanceId -match '^(USB|HID|BTH|BTHENUM|BTHLE|USBSTOR)' } | %{ "$($_.Class)|$($_.InstanceId)|$($_.FriendlyName)" })
ConvertTo-Json $o -Depth 4 -Compress
"""


def _init():
    H._init_db()
    with H._db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sentry_state(key TEXT PRIMARY KEY, value TEXT, changed REAL);
        CREATE TABLE IF NOT EXISTS sentry_seen(kind TEXT, item TEXT, first_seen REAL, PRIMARY KEY(kind, item));
        CREATE TABLE IF NOT EXISTS sentry_events(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, level INT, title TEXT, detail TEXT, alerted INT DEFAULT 0);
        """)


def _event(kind, level, title, detail="", ts=None):
    with H._db() as c:
        c.execute("INSERT INTO sentry_events(ts, kind, level, title, detail) VALUES(?,?,?,?,?)", (ts or time.time(), kind, level, title[:300], str(detail)[:1500]))


def _new(kind, items, now):
    """Items of this kind never seen before (and remember them). Things that go away stay remembered: the same USB stick
    or DNS server coming back is not news."""
    out = []
    with H._db() as c:
        seen = {r[0] for r in c.execute("SELECT item FROM sentry_seen WHERE kind=?", (kind,))}
        for it in dict.fromkeys(str(i) for i in items if i):
            if it not in seen:
                c.execute("INSERT OR IGNORE INTO sentry_seen VALUES(?,?,?)", (kind, it, now))
                out.append(it)
    return out


def _changed(key, value, now):
    """-> previous value if this setting changed since last time, else None. First sighting is not a change."""
    value = json.dumps(value) if not isinstance(value, str) else value
    with H._db() as c:
        r = c.execute("SELECT value FROM sentry_state WHERE key=?", (key,)).fetchone()
        if r is None or r[0] != value:
            c.execute("INSERT OR REPLACE INTO sentry_state VALUES(?,?,?)", (key, value, now))
        return r[0] if r is not None and r[0] != value else None


# ----------------------------------------------------------------------------------------------- things read directly
def _listeners():
    """Programs accepting incoming network connections (not just from this PC itself). -> {exe: [ports]}"""
    out = {}
    try:
        import psutil  # noqa: PLC0415
        for cn in psutil.net_connections(kind="tcp"):
            if cn.status != psutil.CONN_LISTEN or not cn.laddr or cn.laddr.ip.startswith("127.") or cn.laddr.ip == "::1":
                continue
            try:
                exe = psutil.Process(cn.pid).exe() if cn.pid else ""
            except (psutil.Error, OSError):
                exe = ""
            out.setdefault(exe or f"(system, pid {cn.pid})", set()).add(cn.laddr.port)
    except Exception:  # noqa: BLE001
        return None
    return {k: sorted(v) for k, v in out.items()}


def _extensions():
    """Installed browser extensions. -> {"Edge|<id>": name}"""
    local = Path(os.environ.get("LOCALAPPDATA") or H.HOME / "AppData" / "Local")
    out = {}
    for browser, root in (("Edge", local / "Microsoft" / "Edge" / "User Data"), ("Chrome", local / "Google" / "Chrome" / "User Data"),
                          ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data")):
        try:
            profiles = [p for p in root.iterdir() if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile "))]
        except OSError:
            continue
        for prof in profiles:
            try:
                ids = [d for d in (prof / "Extensions").iterdir() if d.is_dir() and len(d.name) == 32]
            except OSError:
                continue
            for d in ids:
                name = ""
                try:
                    ver = sorted(v for v in d.iterdir() if v.is_dir())[-1]
                    man = json.loads((ver / "manifest.json").read_text(encoding="utf-8-sig", errors="replace"))
                    name = str(man.get("name") or "")
                    m = re.fullmatch(r"__MSG_(.+)__", name)
                    if m:  # the name is in a translation file
                        for loc in (man.get("default_locale") or "en", "en", "en_US"):
                            f = ver / "_locales" / loc / "messages.json"
                            if f.is_file():
                                msgs = {k.lower(): v for k, v in json.loads(f.read_text(encoding="utf-8-sig", errors="replace")).items()}
                                name = str((msgs.get(m.group(1).lower()) or {}).get("message") or name)
                                break
                except (OSError, ValueError, IndexError):
                    pass
                out[f"{browser}|{d.name}"] = name or d.name
    ff = H.APPDATA / "Mozilla" / "Firefox" / "Profiles"
    try:
        for prof in ff.iterdir():
            try:
                for a in json.loads((prof / "extensions.json").read_text(encoding="utf-8", errors="replace")).get("addons", []):
                    if a.get("type") == "extension" and a.get("location") != "app-builtin" and not str(a.get("id", "")).endswith("@mozilla.org"):
                        out[f"Firefox|{a.get('id')}"] = str((a.get("defaultLocale") or {}).get("name") or a.get("id"))
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


def _capability_use():
    """Which programs have used the camera / microphone, from Windows' own privacy record. -> {"webcam|<app>": (start, in_use)}"""
    out = {}
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return out
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
    unix = lambda ft: ft / 1e7 - 11644473600 if ft else 0  # noqa: E731

    def walk(cap, sub, nonpkg):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{cap}{sub}") as k:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    if name == "NonPackaged":
                        continue
                    try:
                        with winreg.OpenKey(k, name) as a:
                            start = winreg.QueryValueEx(a, "LastUsedTimeStart")[0]
                            stop = winreg.QueryValueEx(a, "LastUsedTimeStop")[0]
                    except OSError:
                        continue
                    if start:
                        out[f"{cap}|{name.replace('#', chr(92)) if nonpkg else name}"] = (unix(start), stop == 0)
        except OSError:
            pass

    for cap in ("webcam", "microphone"):
        walk(cap, "", False)
        walk(cap, r"\NonPackaged", True)
    return out


def _app_name(app):
    return Path(app).name if "\\" in app else app.split("_")[0]


# ----------------------------------------------------------------------------------------------- snapshot + compare
def state_scan(now):
    out, _err = _hs._ps(SNAP_PS, 150)
    s = json.loads(out.strip() or "{}")
    if not s.get("users"):
        raise RuntimeError("the settings snapshot came back empty")
    first = not H._meta("sentry_baselined")
    ev = (lambda *a, **k: None) if first else _event
    bait = (H._meta("bait_user") or "").lower()
    arr = lambda x: x if isinstance(x, list) else ([] if x is None else [x])  # noqa: E731
    try:  # the hotspot's own "block this address" lines (list kept where only an administrator can write) are not a hijack
        ours = _hs.block_lines()
        s["hosts"] = [x for x in arr(s.get("hosts")) if " ".join(str(x).split()) not in ours]
    except Exception:  # noqa: BLE001
        pass

    # --- tampering with protection
    names = {"rt": "real-time protection", "av": "antivirus", "tamper": "tamper protection", "behavior": "behaviour monitoring", "ioav": "download scanning"}
    for k, v in (s.get("defender") or {}).items():
        if v is not None and _changed(f"defender.{k}", bool(v), now) is not None:
            ev("tamper", 2 if not v else 1, f"Microsoft Defender {names.get(k, k)} was switched {'ON again' if v else 'OFF'}",
               "" if v else "Switching protection off is usually the first thing malware does. If you did not do this, treat it as an intrusion.")
    if "exclusions" in s:
        for x in _new("exclusion", arr(s["exclusions"]), now):
            ev("tamper", 2, f"Defender was told to ignore: {x}", "An exclusion means nothing in that folder / of that program is scanned. Malware adds one for itself.")
    for k, v in (s.get("firewall") or {}).items():
        if _changed(f"firewall.{k}", bool(v), now) is not None:
            ev("tamper", 2 if not v else 1, f"Windows Firewall ({k} networks) was switched {'ON again' if v else 'OFF'}")
    what = {H.FS_AUDIT: "file access (the decoy files depend on this)", H.LOGON_AUDIT: "log-ons", H.PROC_AUDIT: "program starts (following depends on this)"}
    for guid, v in (s.get("audit") or {}).items():
        was = _changed(f"audit.{guid}", v, now)
        if was is not None:
            off = "Success" not in v
            ev("tamper", 2 if off else 1, f"Windows {'stopped' if off else 'changed how it is'} recording {what.get(guid.upper(), guid)}", f"Was: {was}. Now: {v}.")

    # --- new ways in
    for u in _new("user", arr(s.get("users")), now):
        if u.lower() != bait:
            ev("access", 2, f"New Windows account: {u}", "A new account that can sign in to this PC appeared.")
    for u in _new("admin", arr(s.get("admins")), now):
        ev("access", 2, f"New administrator: {u}", "This account was added to the Administrators group, giving it full control of the PC.")
    if s.get("rdp") is not None and _changed("rdp", bool(s["rdp"]), now) is not None:
        ev("access", 2 if s["rdp"] else 1, f"Remote Desktop was switched {'ON' if s['rdp'] else 'off'}",
           "Other computers can now sign in to this PC and see its screen." if s["rdp"] else "")
    svc = {"WinRM": "Windows Remote Management (remote commands)", "sshd": "SSH server (remote log-in)", "RemoteRegistry": "Remote Registry (remote settings changes)"}
    cur = arr(s.get("remote_svcs"))
    was = _changed("remote_svcs", sorted(cur), now)
    if was is not None:
        for x in set(cur) - set(json.loads(was)):
            ev("access", 2, f"{svc.get(x, x)} was started", "This Windows service lets other computers control this one.")
    for x in _new("remote_tool", [p for p in arr(s.get("programs")) if REMOTE_TOOLS.search(p)], now):
        ev("access", 2, f"Remote-control software installed: {x}", "This lets someone else see the screen and control the PC over the internet.")
    lst = _listeners()
    if lst is not None:
        for exe in _new("listener", lst, now):
            sysexe = exe.lower().startswith((os.environ.get("SystemRoot", r"C:\Windows").lower() + "\\", "(system"))
            ev("access", 1 if sysexe else 2, f"{Path(exe).name} started accepting incoming network connections",
               f"{exe}, port{'s' if len(lst[exe]) > 1 else ''} {', '.join(str(p) for p in lst[exe][:8])}. Other devices on the network can now connect to it.")

    # --- traffic interception
    for x in _new("hosts", arr(s.get("hosts")), now):
        ev("intercept", 2, f"New line in the hosts file: {x}", "The hosts file overrides where a website name leads. Malware uses it to redirect sites or block security updates.")
    for x in _new("dns", arr(s.get("dns")), now):
        try:
            ip = ipaddress.ip_address(x.split("%")[0])
            local = ip.is_private or ip.is_link_local
        except ValueError:
            local = False
        ev("intercept", 1 if local or x in KNOWN_DNS else 2, f"New DNS server: {x}",
           "Your router (normal after joining a network)." if local else "A well-known public DNS service." if x in KNOWN_DNS
           else "The DNS server decides where every website name leads, so whoever runs it can redirect you. Check that you set this.")
    for key, label in (("proxy", "Proxy setting"), ("winhttp", "System proxy setting")):
        if s.get(key) is not None:
            was = _changed(key, s[key], now)
            if was is not None:
                on = "on=1" in s[key] or "script=h" in s[key] or ("Proxy Server" in s[key] and key == "winhttp")
                ev("intercept", 2 if on else 1, f"{label} changed", f"Now: {s[key]}. Before: {was}." + (" A proxy sees all web traffic passing through it." if on else ""))
    for x in _new("root", arr(s.get("roots")), now):
        ev("intercept", 2, f"New trusted certificate: {x.split(' ', 1)[-1][:150]}",
           f"Thumbprint {x.split(' ', 1)[0]}. Whoever owns a trusted root certificate can impersonate any secure website to this PC. "
           "Security software and some company tools add one; so does interception malware.")

    # --- devices and browser extensions
    for x in _new("pnp", arr(s.get("pnp")), now):
        cls, inst, name = (x.split("|", 2) + ["", ""])[:3]
        if cls == "Keyboard":
            ev("device", 2, f"New keyboard-type device: {name or inst}", f"{inst}. Fine if you just plugged in a keyboard, dongle or headset. "
               "If not: malicious USB sticks pretend to be keyboards and type commands.")
        elif cls == "Net":
            ev("device", 2, f"New network adapter plugged in: {name or inst}", f"{inst}. A USB network adapter can reroute this PC's traffic through itself.")
        else:
            ev("device", 1, f"New {'storage device' if cls == 'DiskDrive' else 'mouse-type device'}: {name or inst}", inst)
    exts = _extensions()
    for x in _new("extension", exts, now):
        ev("device", 2, f"New {x.split('|')[0]} extension: {exts[x]}", f"Extension id {x.split('|')[1]}. Extensions can read and change every page you visit. Remove it if you did not install it.")

    # --- camera / microphone
    for key, (start, live) in _capability_use().items():
        cap, app = key.split("|", 1)
        was = _changed(f"cap.{key}", str(int(start)), now)
        thing = "camera" if cap == "webcam" else "microphone"
        if _new(f"cap_{cap}", [app], now):
            ev("camera", 2, f"{_app_name(app)} used the {thing} for the first time", f"{app}{' (using it right now)' if live else ''}", start)
        elif was is not None:
            ev("camera", 1, f"{_app_name(app)} used the {thing}", f"{app}{' (using it right now)' if live else ''}", start)

    with H._db() as c:
        seen = dict(c.execute("SELECT kind, COUNT(*) FROM sentry_seen GROUP BY kind").fetchall())
    d = s.get("defender") or {}
    H._set_meta("sentry_snap", json.dumps({
        "defender_rt": d.get("rt"), "defender_tamper": d.get("tamper"), "firewall": s.get("firewall"), "exclusions": len(arr(s.get("exclusions"))) if "exclusions" in s else None,
        "audit_ok": all("Success" in v for v in (s.get("audit") or {}).values()) if s.get("audit") else None,
        "users": arr(s.get("users")), "admins": arr(s.get("admins")), "rdp": s.get("rdp"), "remote_svcs": cur,
        "listeners": len(lst or {}), "dns": arr(s.get("dns")), "proxy_on": "on=1" in (s.get("proxy") or ""), "hosts": len(arr(s.get("hosts"))),
        "roots": len(arr(s.get("roots"))), "devices": seen.get("pnp", 0), "extensions": len(exts),
        "cams": seen.get("cap_webcam", 0), "mics": seen.get("cap_microphone", 0)}))
    H._set_meta("sentry_scan", now)
    if first:
        H._set_meta("sentry_baselined", now)


def state_loop():
    _init()
    time.sleep(20)
    while True:
        try:
            if H._meta("sentry_on") == "1":
                state_scan(time.time())
                H._set_meta("sentry_err", "")
        except Exception as e:  # noqa: BLE001
            H._set_meta("sentry_err", str(e)[:200])
        time.sleep(STATE_SECS)


# ----------------------------------------------------------------------------------------------- suspicious program starts
SHELLS = {"powershell.exe", "pwsh.exe", "cmd.exe", "wscript.exe", "cscript.exe", "mshta.exe", "regsvr32.exe", "rundll32.exe", "bitsadmin.exe", "certutil.exe"}
DOC_APPS = {"winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe", "onenote.exe", "mspub.exe", "msaccess.exe", "visio.exe", "acrord32.exe",
            "acrobat.exe", "foxitreader.exe", "foxitpdfreader.exe", "thunderbird.exe", "wordpad.exe", "hh.exe"}
BROWSERS = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "iexplore.exe"}
TAMPER_RX = re.compile(
    r"(set|add)-mppreference|disablerealtimemonitoring|disableantispyware|\bsc(\.exe)?\s+(stop|delete|config)\s+(windefend|sense|wdnissvc|mpssvc|eventlog)|"
    r"netsh(\.exe)?\s+(advfirewall|firewall)\s+set\s+\w+\s+state\s+off|set-netfirewallprofile.*-enabled\s+\$?false|"
    r"auditpol(\.exe)?\s+/(clear|remove)|auditpol(\.exe)?\s+/set\b.*:disable|wevtutil(\.exe)?\s+(cl|clear-log)\b|clear-eventlog|"
    r"vssadmin(\.exe)?\s+delete\s+shadows|wmic(\.exe)?\s+shadowcopy\s+delete|wbadmin(\.exe)?\s+delete|bcdedit.*(recoveryenabled\s+no|ignoreallfailures)", re.I)
DL_RX = re.compile(r"downloadstring|downloadfile|downloaddata|invoke-webrequest|\biwr\b|invoke-restmethod|\birm\b|net\.webclient|start-bitstransfer|"
                   r"\b(curl|wget)(\.exe)?\s[^|]*https?://", re.I)
RUN_RX = re.compile(r"invoke-expression|\biex\b|frombase64string|reflection\.assembly|\|\s*(iex|powershell|pwsh|cmd)\b", re.I)
HIDDEN_RX = re.compile(r"-w(in(dowstyle)?)?\s+(h(idden)?|1)\b", re.I)
ENC_RX = re.compile(r"-e(?:c|nc|ncodedcommand)?\s+([A-Za-z0-9+/=]{24,})", re.I)
LOLBIN = {"certutil.exe": re.compile(r"-urlcache|-decode|-decodehex|-verifyctl", re.I), "bitsadmin.exe": re.compile(r"/transfer|/addfile", re.I),
          "mshta.exe": re.compile(r"https?:|javascript:|vbscript:", re.I), "regsvr32.exe": re.compile(r"/i:\s*https?:|scrobj", re.I),
          "rundll32.exe": re.compile(r"javascript:|https?://", re.I)}
TEMP_RX = re.compile(r"\\appdata\\local\\temp\\|\\windows\\temp\\|\\downloads\\|\\users\\public\\|\\\$recycle\.bin\\", re.I)
_RECENT = {}  # (rule, program, parent, command hash) -> time: the same thing is reported once per 6 hours


def judge(name, parent, cmd, path):
    """-> (rule key, title, explanation) for a program start that matches a known attack pattern, else None."""
    text = cmd or ""
    m = ENC_RX.search(text) if name in ("powershell.exe", "pwsh.exe") else None
    decoded = ""
    if m:
        try:
            decoded = base64.b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)).decode("utf-16-le", "ignore")
        except ValueError:
            pass
    full = text + " " + decoded
    if TAMPER_RX.search(full):
        return "tamper-cmd", "A command tried to switch off protection or destroy backups / logs", "This is what ransomware and intruders run first."
    if name in SHELLS and DL_RX.search(full) and (RUN_RX.search(full) or HIDDEN_RX.search(full)):
        return "download-run", f"{name} downloaded something from the internet and ran it" + (" in a hidden window" if HIDDEN_RX.search(full) else ""), \
            "The standard way malware installs itself. Also used by some legitimate one-line installers: fine only if you ran it yourself."
    if name in LOLBIN and LOLBIN[name].search(text):
        return "lolbin", f"The Windows tool {name} was used to fetch or unpack something", "Attackers misuse this built-in tool because security software trusts it."
    if parent in DOC_APPS and name in SHELLS:
        return "doc-shell", f"{parent} started {name}", "A document or e-mail program starting a command tool is how malicious attachments run their code."
    if parent in BROWSERS and name in SHELLS and not re.search(r"chrome-extension://|moz-extension://|--parent-window=|nativemessaging", text, re.I):
        return "browser-shell", f"{parent} started {name}", "A browser starting a command tool (other than for an extension's helper program) is how web-page exploits run their code."
    if TEMP_RX.search(path or "") and (path or "").lower().endswith(".exe"):
        return "temp-exe", f"{name} was run from a temporary or downloads folder", ""
    return None


def poll_events(last):
    """Program starts (4688) and log clearing (1102) after record `last`. Returns the new `last`."""
    rc, out, _err = H._run(["wevtutil", "qe", "Security", f"/q:*[System[(EventID=4688 or EventID=1102) and (EventRecordID>{last})]]", "/f:xml", "/c:3000"], 120)
    if rc or H._meta("sentry_on") != "1":
        return last if rc else max([last] + [e["rid"] for e in H._parse(out)])
    ours = Path(sys.executable).name.lower()
    now = time.time()
    for k in [k for k, t in _RECENT.items() if now - t > 6 * 3600]:
        del _RECENT[k]
    for e in H._parse(out):
        last = max(last, e["rid"])
        if "NewProcessName" not in e:  # 1102
            _event("tamper", 2, "The Windows Security log was cleared", f"By {e.get('SubjectUserName') or 'unknown'}. Intruders clear the log to erase their tracks; "
                   "the trap records already saved by Device Watch are not affected.", e["ts"])
            continue
        path = e.get("NewProcessName") or ""
        name, parent, cmd = Path(path).name.lower(), Path(e.get("ParentProcessName") or "").name.lower(), e.get("CommandLine") or ""
        if parent == ours or name == ours:
            continue  # this app's own helper commands
        v = judge(name, parent, cmd, path)
        if not v:
            continue
        rule, title, why = v
        key = (rule, name, parent, hashlib.md5(cmd.encode("utf-8", "replace")).hexdigest() if rule != "temp-exe" else path.lower())
        if key in _RECENT:
            continue
        _RECENT[key] = now
        extra = ""
        if rule == "temp-exe":
            if not _new("tempexe", [path.lower()], now):
                continue
            # signature first, on its own: hashing a several-hundred-MB installer in the same call used to run out of time,
            # which left no answer at all and reported a genuine signed updater as "not validly signed (unknown)"
            s = H._sig_info(path, 120, with_hash=False) or H._sig_info(path, 180, with_hash=False)
            if s.get("sig") == "Valid":
                continue  # a signed installer unpacking itself: normal
            sha = (H._sig_info(path, 300) or {}).get("sha256") if s.get("sig") != "Missing" else None
            if not s:
                extra = "Its signature could not be checked (the check failed or took too long), so it is reported to be on the safe side. "
            elif s["sig"] == "Missing":
                extra = "The program file was already gone when it was checked, so its signature is unknown. "
            else:
                extra = f"It is not validly signed ({s['sig']}). "
            extra += f"SHA-256 {sha or 'n/a'}. Installers do unpack to temp folders, but so does malware. "
        m = ENC_RX.search(cmd)
        shown = cmd[:700]
        if m and rule in ("download-run", "tamper-cmd"):
            try:
                shown += "  [decoded: " + base64.b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)).decode("utf-16-le", "ignore")[:500] + "]"
            except ValueError:
                pass
        user = "\\".join(x for x in (e.get("SubjectDomainName"), e.get("SubjectUserName")) if x)
        _event("process", 2, title, f"{extra}{why} Command: {shown} | Program: {path} | Started by: {e.get('ParentProcessName') or 'unknown'} | Account: {user}", e["ts"])
        try:
            H.start_follow(H._hex(e.get("NewProcessId")), path, user, title, e["ts"])
        except Exception:  # noqa: BLE001
            pass
    return last


# ----------------------------------------------------------------------------------------------- collector side (normal user)
KIND = {"tamper": "Protection tampering", "access": "New way in", "intercept": "Traffic interception", "device": "Devices and extensions",
        "camera": "Camera / microphone", "process": "Suspicious program start"}


def alerts(now):
    """New findings -> ALERTS tab + notification. Also: the helper itself going quiet while the traps are meant to be on."""
    _init()
    with H._db() as c:
        new = [dict(r) for r in c.execute("SELECT * FROM sentry_events WHERE alerted=0 ORDER BY ts")]
        c.execute("UPDATE sentry_events SET alerted=1 WHERE alerted=0")
    loud = 0
    try:
        import bluetooth as _bt  # noqa: PLC0415
        away = _bt.watching(now)
    except Exception:  # noqa: BLE001
        away = False
    for e in new:
        msg = e["title"] + (". " + e["detail"] if e["detail"] else "")
        if e["kind"] == "camera" and away and e["level"] < 2:   # camera / microphone used while nobody should be at the PC
            e["level"] = 2
            msg = "While you were away: " + msg
        if e["kind"] == "device" and e["title"].startswith("New keyboard"):
            try:
                import guard  # noqa: PLC0415
                guard.note_keyboard(now)
            except Exception:  # noqa: BLE001
                pass
        _hs._event(e["level"], "honey", f"{KIND.get(e['kind'], e['kind'])}: {msg}", e["kind"])
        if e["level"] >= 2 and _hs._cfg()["notify"]:
            loud += 1
            if loud <= 3:
                _hs._toast(f"Device Watch: {KIND.get(e['kind'], e['kind']).lower()}", msg)
    if H._meta("sentry_on") == "1":
        hb = float(H._meta("watch_hb") or 0)
        quiet = hb and now - hb > 600 and now - float(H._meta("setup_done") or 0) > 900
        if quiet and H._meta("helper_quiet") != "1":
            H._set_meta("helper_quiet", "1")
            msg = ("The background helper that runs the traps has stopped (no sign of life for 10 minutes). Until it runs again nothing is being watched. "
                   "It starts at log-on; REPAIR DECOYS on the TRAPS tab restarts it. If you did not stop it, something else did.")
            _hs._event(2, "honey", "Protection tampering: " + msg, "helper")
            if _hs._cfg()["notify"]:
                _hs._toast("Device Watch: the trap watcher has stopped", msg)
        elif not quiet and H._meta("helper_quiet") == "1":
            H._set_meta("helper_quiet", "0")


def status():
    _init()
    with H._db() as c:
        events = [dict(r) for r in c.execute("SELECT id, ts, kind, level, title, detail FROM sentry_events ORDER BY ts DESC, id DESC LIMIT 150")]
        meta = {r[0]: r[1] for r in c.execute("SELECT k, v FROM meta WHERE k LIKE 'sentry_%'")}
    for e in events:
        e["kind_name"] = KIND.get(e["kind"], e["kind"])
    try:
        snap = json.loads(meta.get("sentry_snap") or "null")
    except ValueError:
        snap = None
    return {"on": meta.get("sentry_on") == "1", "baselined": float(meta.get("sentry_baselined") or 0), "scan": float(meta.get("sentry_scan") or 0),
            "err": meta.get("sentry_err") or "", "snap": snap, "events": events}
