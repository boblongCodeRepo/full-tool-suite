"""Device Watch > TRAPS: decoy files that raise an alert when anything opens, changes or deletes them.

Set-up (administrator, once): creates a few files that thieves and info-stealing malware go looking for (a password
manager database, a passwords list, bank details, a crypto wallet and recovery phrase), tells Windows to audit every
read / write / delete of exactly those files, and turns on Windows' "File System" audit category.

Watching: Windows writes event 4663 to the Security log, which only an administrator can read, so the always-on
elevated hotspot helper polls it (watch_loop) and stores hits in honey.db. The normal collector turns new hits into
ALERTS-tab entries and Windows notifications, and on its own also notices a decoy that was deleted or changed
(size / date, read without opening the file, so it does not trip the audit itself).
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import random
import re
import select
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import hotspot as _hs

DB_FILE = _hs.HS_DIR / "honey.db"
HOME = Path(os.environ.get("USERPROFILE") or Path.home())
APPDATA = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
FS_AUDIT = "{0CCE921D-69AE-11D9-BED3-505054503030}"  # audit subcategory "File System" (GUID: language independent)
EVT_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
# programs whose reads are routine, not someone looking: logged but no alert
ROUTINE = {"msmpeng.exe": "Microsoft Defender scan", "mpdefendercoreservice.exe": "Microsoft Defender",
           "searchprotocolhost.exe": "Windows Search indexer", "searchindexer.exe": "Windows Search indexer",
           "searchfilterhost.exe": "Windows Search indexer"}
FRIENDLY = {"notepad.exe": "Notepad", "explorer.exe": "Windows Explorer (copying, or the preview pane)", "excel.exe": "Excel",
            "winword.exe": "Word", "keepass.exe": "KeePass", "keepassxc.exe": "KeePassXC", "powershell.exe": "PowerShell",
            "pwsh.exe": "PowerShell", "cmd.exe": "Command Prompt", "python.exe": "Python", "pythonw.exe": "Python",
            "msedge.exe": "Edge", "chrome.exe": "Chrome", "firefox.exe": "Firefox", "code.exe": "VS Code", "robocopy.exe": "Robocopy",
            "onedrive.exe": "OneDrive", "system": "Windows kernel (System)"}
MASK = ((0x1, "read"), (0x2, "write"), (0x4, "append"), (0x10000, "delete"), (0x40000, "change permissions"), (0x80000, "take ownership"))


def _decoys():
    return [
        {"path": HOME / "Documents" / "Passwords.kdbx", "what": "Password manager database (KeePass)", "make": _kdbx},
        # in a subfolder, not on the Desktop: file pickers (Edge, upload dialogs) open on the Desktop and preview files there,
        # which reads them. Nothing opens Documents\Personal unless someone is going through folders looking for things.
        {"path": HOME / "Documents" / "Personal" / "passwords.txt", "what": "Passwords list", "make": _pw_txt},
        {"path": HOME / "Documents" / "Bank accounts 2026.csv", "what": "Bank account details", "make": _bank_csv},
        {"path": HOME / "Documents" / "Crypto wallet recovery phrase.txt", "what": "Crypto wallet recovery phrase", "make": _seed_txt},
        {"path": APPDATA / "Bitcoin" / "wallet.dat", "what": "Bitcoin wallet (where Bitcoin Core keeps it)", "make": _wallet_dat, "own": APPDATA / "Bitcoin"},
        # the fixed checklist info-stealers work through. "own": the program's folder; if it is already there and not ours,
        # the real program is installed and the decoy is skipped (see setup_main)
        {"path": HOME / ".ssh" / "id_rsa", "what": "SSH private key (log-in key for servers)", "make": _ssh_key, "own": HOME / ".ssh"},
        {"path": HOME / ".aws" / "credentials", "what": "Amazon cloud access keys", "make": _aws, "own": HOME / ".aws"},
        {"path": APPDATA / "Exodus" / "exodus.wallet" / "seed.seco", "what": "Exodus crypto wallet", "make": _blob, "own": APPDATA / "Exodus"},
        {"path": APPDATA / "discord" / "Local Storage" / "leveldb" / "000005.ldb", "what": "Discord log-in token", "make": _discord, "own": APPDATA / "discord"},
        {"path": APPDATA / "Telegram Desktop" / "tdata" / "key_datas", "what": "Telegram session", "make": _tdata, "own": APPDATA / "Telegram Desktop"},
        {"path": APPDATA / "FileZilla" / "recentservers.xml", "what": "Saved FTP log-ins (FileZilla)", "make": _filezilla, "own": APPDATA / "FileZilla"},
    ]


def _b64(r, n):
    return base64.b64encode(r.randbytes(n)).decode()


def _ssh_key(r):
    body = base64.b64encode(b"openssh-key-v1\x00" + r.randbytes(r.randint(1300, 1400))).decode()
    lines = "\n".join(body[i:i + 70] for i in range(0, len(body), 70))
    return f"-----BEGIN OPENSSH PRIVATE KEY-----\n{lines}\n-----END OPENSSH PRIVATE KEY-----\n".encode()


def _aws_text(key, secret, region="us-east-1"):
    return f"[default]\naws_access_key_id = {key}\naws_secret_access_key = {secret}\nregion = {region}\n".encode()


def _canary():
    """The reporting decoy keys the user pasted in (see canary()), or None."""
    try:
        c = json.loads(_meta("canary_aws") or "null")
    except (ValueError, sqlite3.Error):
        return None
    return c if isinstance(c, dict) and c.get("key") and c.get("secret") else None


def _aws(r):
    c = _canary()
    if c:  # keys that report when someone tries them: keep them when the decoy is made again
        return _aws_text(c["key"], c["secret"], c.get("region") or "us-east-1")
    key = "AKIA" + "".join(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16))
    return _aws_text(key, _b64(r, 30))


def _blob(r):
    return r.randbytes(r.randint(600, 1200))


def _discord(r):
    token = f"{_b64(r, 18)}.{_b64(r, 4)[:6]}.{_b64(r, 28)[:38]}".replace("+", "-").replace("/", "_")
    return r.randbytes(900) + b'\x01token"' + token.encode() + b'"' + r.randbytes(1400)


def _tdata(r):
    return b"TDF$" + r.randbytes(r.randint(380, 420))


# made-up servers in the FileZilla decoy (neither exists): a look-up of either means someone is trying its log-ins
# (watched by guard.names_tick)
HONEY_HOSTS = ("ftp.todsite.net", "files.homebackup.org")


def _filezilla(r):
    srv = "".join(f"\t\t<Server>\r\n\t\t\t<Host>{h}</Host>\r\n\t\t\t<Port>21</Port>\r\n\t\t\t<Protocol>0</Protocol>\r\n\t\t\t<User>{u}</User>\r\n"
                  f"\t\t\t<Pass encoding=\"base64\">{_b64(r, 9)}</Pass>\r\n\t\t\t<Logontype>1</Logontype>\r\n\t\t</Server>\r\n"
                  for h, u in zip(HONEY_HOSTS, ("user", "backup")))
    return ('<?xml version="1.0" encoding="UTF-8"?>\r\n<FileZilla3 version="3.66.5" platform="windows">\r\n\t<RecentServers>\r\n'
            + srv + "\t</RecentServers>\r\n</FileZilla3>\r\n").encode()


# ----------------------------------------------------------------------------------------------- decoy contents
def _kdbx(r):
    head = bytes.fromhex("03d9a29a67fb4bb500000400")  # KeePass 2 signatures + KDBX 4.0 version
    return head + r.randbytes(r.randint(3500, 6500))


def _wallet_dat(r):
    b = bytearray(r.randbytes(16384 * 2))
    b[12:16] = bytes.fromhex("62310500")  # Berkeley DB btree magic, as in a real wallet.dat
    return bytes(b)


def _pw_txt(r):
    w = ["Summer", "Maple", "Harbor", "Falcon", "Copper", "River", "Velvet", "Granite"]
    p = lambda: f"{r.choice(w)}{r.choice(w)}{r.randint(10, 99)}{r.choice('!#$%&*')}"  # noqa: E731
    return ("email (gmail)  -  user.backup  /  " + p() + "\r\n"
            "bank (online)  -  user 48120377  /  " + p() + "\r\n"
            "amazon  -  same email  /  " + p() + "\r\n"
            "router admin  -  admin  /  " + p() + "\r\n"
            "work vpn  -  vpnuser  /  " + p() + "\r\n"
            "coinbase  -  2fa backup codes in Documents\r\n").encode()


def _bank_csv(r):
    rows = ["Bank,Type,Routing,Account,Online login,Notes"]
    for bank, kind in (("Chase", "Checking"), ("Chase", "Savings"), ("Capital One", "Credit card"), ("Fidelity", "Brokerage")):
        rows.append(f"{bank},{kind},0{r.randint(21000000, 99999999)},{r.randint(10 ** 9, 10 ** 10 - 1)},user{r.randint(10, 99)},")
    return ("\r\n".join(rows) + "\r\n").encode()


def _seed_txt(r):
    words = ("anchor orbit velvet pilot ladder echo marble sunset quarry violin harbor cactus tunnel ribbon glacier "
             "falcon meadow copper lantern basket shadow timber canyon wallet").split()
    return ("Ledger backup - DO NOT LOSE\r\n" + " ".join(r.sample(words, 24)) + "\r\n").encode()


# ----------------------------------------------------------------------------------------------- storage
def _db():
    c = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    with _db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS decoys(path TEXT PRIMARY KEY, what TEXT, created REAL, size INT, mtime REAL, state TEXT DEFAULT 'ok', changed REAL);
        CREATE TABLE IF NOT EXISTS hits(rid INT PRIMARY KEY, ts REAL, path TEXT, process TEXT, pid INT, user TEXT, access TEXT,
            routine TEXT, alerted INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)
        c.executescript("""
        CREATE TABLE IF NOT EXISTS stores(path TEXT PRIMARY KEY, browser TEXT, allow TEXT, armed REAL);
        CREATE TABLE IF NOT EXISTS probes(id INTEGER PRIMARY KEY, ts REAL, src TEXT, sport INT, port INT, data TEXT, mac TEXT,
            local INT DEFAULT 0, alerted INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS autoruns(kind TEXT, name TEXT, value TEXT, file TEXT, first_seen REAL, PRIMARY KEY(kind, name));
        CREATE TABLE IF NOT EXISTS autorun_events(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, name TEXT, value TEXT, old TEXT, file TEXT,
            change TEXT, sig TEXT, signer TEXT, sha256 TEXT, level INT, note TEXT);
        CREATE TABLE IF NOT EXISTS follows(id INTEGER PRIMARY KEY, ts REAL, until REAL, ended REAL, pid INT, process TEXT, user TEXT, trap TEXT,
            state TEXT, summary TEXT, alerted INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS follow_events(id INTEGER PRIMARY KEY, fid INT, ts REAL, type TEXT, what TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS follow_events_fid ON follow_events(fid, ts);
        CREATE TABLE IF NOT EXISTS logons(rid INT PRIMARY KEY, ts REAL, user TEXT, src TEXT, host TEXT, ltype INT, reason TEXT, process TEXT,
            ok INT DEFAULT 0, bait INT DEFAULT 0, remote INT DEFAULT 0, alerted INT DEFAULT 0);
        """)
        if "details" not in {r[1] for r in c.execute("PRAGMA table_info(hits)")}:
            c.execute("ALTER TABLE hits ADD COLUMN details TEXT")  # JSON snapshot of the program, taken when the hit was seen
        if "reviewed" not in {r[1] for r in c.execute("PRAGMA table_info(hits)")}:
            c.execute("ALTER TABLE hits ADD COLUMN reviewed REAL")  # when the user marked it "that was me"


def _meta(k, default=None):
    with _db() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def _set_meta(k, v):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


def _run(args, timeout=60):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=_hs.NO_WINDOW)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


# ----------------------------------------------------------------------------------------------- set-up (elevated)
def _sacl(path):
    q = str(path).replace("'", "''")
    # only the audit list is written (not owner / permissions, as Set-Acl does): re-running set-up on an existing decoy
    # must not itself count as "changed permissions, took ownership" of it
    script = (f"$p = '{q}'; $sid = New-Object System.Security.Principal.SecurityIdentifier 'S-1-1-0'; "
              "$a = [System.IO.File]::GetAccessControl($p, 'Audit'); $a.PurgeAuditRules($sid); "
              "$a.AddAuditRule((New-Object System.Security.AccessControl.FileSystemAuditRule($sid, "
              "[System.Security.AccessControl.FileSystemRights]'ReadData, WriteData, AppendData, Delete, ChangePermissions, TakeOwnership', "
              "'Success, Failure'))); [System.IO.File]::SetAccessControl($p, $a)")
    _out, err = _hs._ps(script, 60)
    return err.strip()


LOGON_AUDIT = "{0CCE9215-69AE-11D9-BED3-505054503030}"  # audit subcategory "Logon"
DNS_LOG = "Microsoft-Windows-DNS-Client/Operational"
EDGE_POLICY = r"HKLM\SOFTWARE\Policies\Microsoft\Edge"
FW_RULE = "DeviceWatch trap ports"
BAIT_USER = "admin"
# fake services: ports that attackers and spreading malware probe first, and that nothing on this PC really offers
TRAP_PORTS = {21: "FTP file server", 22: "SSH remote log-in", 23: "Telnet remote log-in", 1433: "Microsoft SQL database",
              3306: "MySQL database", 3389: "Remote Desktop", 5900: "VNC remote control", 2222: "SSH remote log-in (second port)",
              5555: "Android debug bridge (phones / TV boxes)"}


def _stores():
    """The real files where browsers keep saved passwords, cookies and the key that unlocks them: what info-stealers copy.
    -> [(path, browser, [folders whose programs may read it])]"""
    local = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")
    pf = [Path(os.environ.get(k) or d) for k, d in (("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
    out = []
    for name, root, dirs in (("Edge", local / "Microsoft" / "Edge" / "User Data", [p / "Microsoft" / "Edge" for p in pf]),
                             ("Chrome", local / "Google" / "Chrome" / "User Data", [p / "Google" for p in pf] + [local / "Google" / "Chrome" / "Application"]),
                             ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data",
                              [p / "BraveSoftware" for p in pf] + [local / "BraveSoftware" / "Brave-Browser" / "Application"])):
        if not root.is_dir():
            continue
        files = [root / "Local State"]
        for prof in root.iterdir():
            if prof.is_dir() and (prof.name == "Default" or prof.name.startswith("Profile ")):
                files += [prof / "Login Data", prof / "Web Data", prof / "Network" / "Cookies"]
        out += [(f, name, dirs) for f in files if f.is_file()]
    ff = APPDATA / "Mozilla" / "Firefox" / "Profiles"
    if ff.is_dir():
        for prof in ff.iterdir():
            out += [(f, "Firefox", [p / "Mozilla Firefox" for p in pf]) for f in (prof / "logins.json", prof / "key4.db", prof / "cookies.sqlite") if f.is_file()]
    return out


def _store_sacl(paths, arm=True):
    """Add (or remove) a 'record every read' audit rule on real browser files. Only the audit list is touched, never who
    may open the file. Returns error lines."""
    if not paths:
        return []
    lst = ",".join("'" + str(p).replace("'", "''") + "'" for p in paths)
    change = ("$has = $false; foreach ($r in $a.GetAuditRules($true, $false, [System.Security.Principal.SecurityIdentifier])) "
              "{ if (([int]$r.FileSystemRights -band 1) -and $r.IdentityReference.Value -eq 'S-1-1-0') { $has = $true } }; "
              "if (-not $has) { $a.AddAuditRule((New-Object System.Security.AccessControl.FileSystemAuditRule($sid, 'ReadData', 'Success'))); "
              "[System.IO.File]::SetAccessControl($p, $a) }") if arm else "$a.PurgeAuditRules($sid); [System.IO.File]::SetAccessControl($p, $a)"
    script = (f"$sid = New-Object System.Security.Principal.SecurityIdentifier 'S-1-1-0'; foreach ($p in @({lst})) {{ "
              "if (-not (Test-Path -LiteralPath $p)) { continue }; "
              "try { $a = [System.IO.File]::GetAccessControl($p, 'Audit'); " + change + " } "
              "catch { \"ERR $([System.IO.Path]::GetFileName($p)): $($_.Exception.Message)\" } }")
    out, _err = _hs._ps(script, 90)
    return [ln.strip()[4:160] for ln in out.splitlines() if ln.startswith("ERR ")]


def _arm_stores():
    """(Re)apply auditing to the browser files. Runs at set-up and every few minutes in the helper, because browsers
    replace some of these files (which drops the audit rule) and new browser profiles appear."""
    found = _stores()
    errs = _store_sacl([f for f, _b, _d in found])
    now = time.time()
    with _db() as c:
        c.execute("DELETE FROM stores")
        for f, browser, dirs in found:
            c.execute("INSERT OR REPLACE INTO stores VALUES(?,?,?,?)", (str(f), browser, json.dumps([str(d).lower() for d in dirs]), now))
    return errs


def _bait_account():
    """A local account called 'admin' with a random password nobody knows and no group membership (so it cannot sign in
    at the keyboard and is not on the sign-in screen). Any log-on attempt against it is someone guessing."""
    ours = _meta("bait_user") == BAIT_USER
    script = (f"$n = '{BAIT_USER}'; if (Get-LocalUser -Name $n -ErrorAction SilentlyContinue) {{ 'exists' }} else {{ "
              "$b = New-Object byte[] 30; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); "
              "$pw = ConvertTo-SecureString ([Convert]::ToBase64String($b) + 'aZ9!') -AsPlainText -Force; "
              "New-LocalUser -Name $n -Password $pw -FullName 'Admin' -Description 'Local administrator' -PasswordNeverExpires "
              "-UserMayNotChangePassword -AccountNeverExpires | Out-Null; if ($?) { 'created' } }")
    out, err = _hs._ps(script, 60)
    if "created" in out:
        _set_meta("bait_user", BAIT_USER)
        return ""
    if "exists" in out:
        return "" if ours else f"an account called '{BAIT_USER}' already exists, so no bait account was made (failed log-ons are still watched)"
    return f"bait account: {(err or out).strip()[:150]}"


def _remove_elevated():
    """`--honey-remove` (administrator): undo what needs admin rights: bait account, firewall rule, browser-file auditing."""
    _init_db()
    if not ctypes.windll.shell32.IsUserAnAdmin():
        return
    for k in ("stores_on", "ports_on", "autoruns_on"):
        _set_meta(k, "0")
    try:
        with _db() as c:
            paths = [r[0] for r in c.execute("SELECT path FROM stores")]
            c.execute("DELETE FROM stores")
        _store_sacl(paths, arm=False)
    except Exception as e:  # noqa: BLE001
        print(f"honey: {e}")
    script = f"Remove-NetFirewallRule -DisplayName '{FW_RULE}' -ErrorAction SilentlyContinue; "
    if _meta("bait_user"):
        script += f"Remove-LocalUser -Name '{_meta('bait_user')}' -ErrorAction SilentlyContinue"
    _hs._ps(script, 60)
    _set_meta("bait_user", "")
    _set_meta("follow_on", "0")
    if _meta("proc_audit_was") == "0":  # we switched program-start logging on: switch it back off
        _run(["auditpol", "/set", f"/subcategory:{PROC_AUDIT}", "/success:disable"])
        _run(["reg", "delete", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\Audit", "/v", "ProcessCreationIncludeCmdLine_Enabled", "/f"])
    if _meta("dnslog_on") == "1":
        _run(["wevtutil", "sl", DNS_LOG, "/e:false"])
        if _meta("edge_dns_was") == "none":  # we set them: give Edge its own (encrypted) look-ups back
            _run(["reg", "delete", EDGE_POLICY, "/v", "BuiltInDnsClientEnabled", "/f"])
            _run(["reg", "delete", EDGE_POLICY, "/v", "DnsOverHttpsMode", "/f"])
        _set_meta("dnslog_on", "0")
    with _db() as c:
        c.execute("DELETE FROM meta WHERE k IN ('proc_audit_was', 'edge_dns_was')")
    _set_meta("cleanup_needed", "0")


def setup_main(argv):
    """`MarketTerminal.exe --honey-setup` / `--honey-remove` (run as administrator)."""
    _init_db()
    if "--honey-remove" in argv:
        _remove_elevated()
        return
    if not ctypes.windll.shell32.IsUserAnAdmin():
        _set_meta("setup_err", "set-up was started without administrator rights")
        return
    errs = []
    rc, out, err = _run(["auditpol", "/set", f"/subcategory:{FS_AUDIT}", "/success:enable", "/failure:enable"])
    if rc:
        errs.append(f"auditpol: {(err or out).strip()[:150]}")
    r = random.Random()
    now = time.time()
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES('setup_busy', ?)", (str(now),))
    # decoys from an older layout (e.g. passwords.txt on the Desktop): forget them first so deleting them is not a hit
    want = {str(d["path"]).lower() for d in _decoys()}
    with _db() as c:
        old = [r[0] for r in c.execute("SELECT path FROM decoys") if r[0].lower() not in want]
        for p in old:
            c.execute("DELETE FROM decoys WHERE path=?", (p,))
    for p in old:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError as e:
            errs.append(f"old decoy {Path(p).name}: {e}")
    with _db() as c:
        known = {r[0].lower() for r in c.execute("SELECT path FROM decoys")}
    skipped = []
    for d in _decoys():
        p = d["path"]
        # never adopt something we did not create: REMOVE DECOYS deletes whatever is in the decoys table
        if str(p).lower() not in known and (p.exists() or (d.get("own") and d["own"].exists())):
            skipped.append(d["what"])
            continue
        try:
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(d["make"](r))
                old = now - r.randint(40, 400) * 86400 - r.randint(0, 86400)  # look like they've been there a while
                os.utime(p, (old, old))
            _run(["attrib", "+I", str(p)])  # not content-indexed: Windows Search will not read it
            e = _sacl(p)
            if e:
                errs.append(f"{p.name}: {e[:150]}")
            st = p.stat()
            with _db() as c:
                c.execute("INSERT OR REPLACE INTO decoys VALUES(?,?,?,?,?, 'ok', NULL)", (str(p), d["what"], now, st.st_size, st.st_mtime))
        except OSError as e:
            errs.append(f"{p.name}: {e}")
    _set_meta("skipped", "; ".join(skipped))
    # --- the other traps
    try:
        _set_meta("stores_on", "1")
        errs += _arm_stores()
    except Exception as e:  # noqa: BLE001
        errs.append(f"browser password stores: {str(e)[:150]}")
    rc, out, err = _run(["auditpol", "/set", f"/subcategory:{LOGON_AUDIT}", "/success:enable", "/failure:enable"])
    if rc:
        errs.append(f"auditpol (log-ons): {(err or out).strip()[:150]}")
    try:
        e = _bait_account()
        if e:
            errs.append(e)
    except Exception as e:  # noqa: BLE001
        errs.append(f"bait account: {str(e)[:150]}")
    try:
        exe = str(_hs.PROTECTED_DIR / Path(sys.executable).name) if getattr(sys, "frozen", False) else _hs._helper_cmd()[0]
        _out, err = _hs._ps(f"Remove-NetFirewallRule -DisplayName '{FW_RULE}' -ErrorAction SilentlyContinue; "
                            f"New-NetFirewallRule -DisplayName '{FW_RULE}' -Description 'Device Watch TRAPS: fake services that only record who connects' "
                            f"-Direction Inbound -Action Allow -Protocol TCP -LocalPort {','.join(str(p) for p in TRAP_PORTS)} "
                            f"-Program '{exe.replace(chr(39), chr(39) * 2)}' -Profile Any | Out-Null", 60)
        if err.strip():
            errs.append(f"firewall rule for the trap ports: {err.strip()[:150]}")
        _set_meta("ports_on", "1")
    except Exception as e:  # noqa: BLE001
        errs.append(f"trap ports: {str(e)[:150]}")
    _set_meta("autoruns_on", "1")
    # following: Windows records every program start with its command line (event 4688); a bigger Security log keeps
    # that, and the file trail of a followed program, from being overwritten too soon
    try:
        _rc, out, _e = _run(["auditpol", "/get", f"/subcategory:{PROC_AUDIT}"])
        if _meta("proc_audit_was") is None:
            _set_meta("proc_audit_was", "1" if "Success" in out else "0")
        rc, out, err = _run(["auditpol", "/set", f"/subcategory:{PROC_AUDIT}", "/success:enable"])
        if rc:
            errs.append(f"auditpol (program starts): {(err or out).strip()[:150]}")
        _run(["reg", "add", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\Audit", "/v", "ProcessCreationIncludeCmdLine_Enabled",
              "/t", "REG_DWORD", "/d", "1", "/f"])
        _run(["wevtutil", "sl", "Security", "/ms:134217728"])  # 128 MB
        _set_meta("follow_on", "1")
    except Exception as e:  # noqa: BLE001
        errs.append(f"following: {str(e)[:150]}")
    _set_meta("sentry_on", "1")
    # the DNS tab: Windows keeps a log of every name looked up through it, once switched on. Edge looks names up by
    # itself (encrypted) unless told to use Windows, so that is set as an Edge policy; undone by --honey-remove
    try:
        _run(["wevtutil", "sl", DNS_LOG, "/e:false"])
        _run(["wevtutil", "sl", DNS_LOG, "/ms:33554432"])  # 32 MB
        rc, out, err = _run(["wevtutil", "sl", DNS_LOG, "/e:true"])
        if rc:
            errs.append(f"domain look-up log: {(err or out).strip()[:150]}")
        if _meta("edge_dns_was") is None:  # only undo later what was not already set by someone else
            rc1, _o, _e = _run(["reg", "query", EDGE_POLICY, "/v", "BuiltInDnsClientEnabled"])
            rc2, _o, _e = _run(["reg", "query", EDGE_POLICY, "/v", "DnsOverHttpsMode"])
            _set_meta("edge_dns_was", "none" if rc1 and rc2 else "set")
        _run(["reg", "add", EDGE_POLICY, "/v", "BuiltInDnsClientEnabled", "/t", "REG_DWORD", "/d", "0", "/f"])
        _run(["reg", "add", EDGE_POLICY, "/v", "DnsOverHttpsMode", "/t", "REG_SZ", "/d", "off", "/f"])
        _set_meta("dnslog_on", "1")
    except Exception as e:  # noqa: BLE001
        errs.append(f"domain look-up log: {str(e)[:150]}")
    _set_meta("cleanup_needed", "0")
    _set_meta("setup_err", "; ".join(errs))
    _set_meta("setup_done", now)
    _set_meta("setup_busy", "0")
    # the watcher lives in the elevated helper: (re)install it so the protected copy has this code
    _hs.sniff_main(["--hs-sniff", "--install"])


def remove(q):
    """Delete the decoys (no admin needed: they belong to the user). Hits caused by this are not alerted."""
    _init_db()
    _set_meta("removing", time.time())
    gone = []
    keep = {HOME, APPDATA, HOME / "Documents", HOME / "Desktop", HOME / "AppData", APPDATA.parent / "Local"}
    with _db() as c:
        for r in c.execute("SELECT path FROM decoys").fetchall():
            p = Path(r[0])
            try:
                p.unlink(missing_ok=True)
                gone.append(p.name)
                d = p.parent  # the folders made for the decoy (Bitcoin, .ssh, discord\Local Storage\leveldb, ...) if now empty
                while d not in keep and HOME in d.parents and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
            except OSError:
                pass
        c.execute("DELETE FROM decoys")
    # the other traps stop being watched straight away; undoing the bait account, firewall rule and browser-file
    # auditing needs administrator rights
    for k in ("stores_on", "ports_on", "autoruns_on", "follow_on", "sentry_on"):
        _set_meta(k, "0")
    _set_meta("cleanup_needed", "1")
    cleaned = False
    if _hs.IS_WIN:
        exe, args = _hs._helper_cmd()
        cleaned = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args.replace("--hs-sniff", "--honey-remove"), str(Path(exe).parent), 0) > 32
    return {"removed": gone, "cleanup_started": cleaned}


def review(q):
    """UI: mark a hit as explained ("that was me"), or undo that. Covers every event of the same open (same file,
    program and process), since one open is logged as several."""
    _init_db()
    rid = int(q.get("rid") or 0)
    on = q.get("on", "1") == "1"
    with _db() as c:
        h = c.execute("SELECT path, process, pid FROM hits WHERE rid=?", (rid,)).fetchone()
        if not h:
            raise ValueError("That entry is no longer in the list")
        c.execute("UPDATE hits SET reviewed=? WHERE path=? AND process IS ? AND pid IS ?", (time.time() if on else None, h[0], h[1], h[2]))
    return {"ok": True}


def clear(q):
    """UI: empty the list of touches. what=reviewed (default) removes only reviewed and routine rows; what=all removes
    every row and the finished "followed" timelines."""
    _init_db()
    with _db() as c:
        if q.get("what") == "all":
            n = c.execute("DELETE FROM hits").rowcount
            done = [r[0] for r in c.execute("SELECT id FROM follows WHERE state != 'following'")]
            c.executemany("DELETE FROM follow_events WHERE fid=?", [(i,) for i in done])
            c.executemany("DELETE FROM follows WHERE id=?", [(i,) for i in done])
        else:
            n = c.execute("DELETE FROM hits WHERE reviewed IS NOT NULL OR routine != ''").rowcount
    return {"removed": n}


_AWS_KEY = re.compile(r"\b((?:AKIA|ASIA)[A-Z0-9]{16})\b")
_AWS_SECRET = re.compile(r"aws_secret_access_key\s*=\s*([A-Za-z0-9/+=]{40})\b|(?<![A-Za-z0-9/+=])([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])")
_AWS_REGION = re.compile(r"region\s*=\s*([a-z]{2}(?:-[a-z]+)+-\d)\b")


def canary(q):
    """UI: put decoy Amazon keys that report when used (made by the user at a canary-token service) into the
    .aws\\credentials decoy. text = the block the service shows; off=1 goes back to random keys."""
    _init_db()
    path = next(d["path"] for d in _decoys() if d["make"] is _aws)
    with _db() as c:
        ours = c.execute("SELECT 1 FROM decoys WHERE lower(path)=?", (str(path).lower(),)).fetchone()
    if not ours:
        raise ValueError("The Amazon keys decoy is not in place (set the decoys up first; it is skipped if you really use Amazon cloud tools)")
    if q.get("off") == "1":
        with _db() as c:
            c.execute("DELETE FROM meta WHERE k='canary_aws'")
        data = _aws(random.Random())
    else:
        text = q.get("text") or ""
        k, s = _AWS_KEY.search(text), _AWS_SECRET.search(text)
        if not k or not s:
            raise ValueError("Could not find the keys in that text. Paste the whole block the service shows: a line with aws_access_key_id "
                             "(starts AKIA) and a line with aws_secret_access_key (40 characters).")
        reg = _AWS_REGION.search(text)
        cur = {"key": k.group(1), "secret": s.group(1) or s.group(2), "region": reg.group(1) if reg else "us-east-1", "ts": time.time()}
        _set_meta("canary_aws", json.dumps(cur))
        data = _aws_text(cur["key"], cur["secret"], cur["region"])
    _set_meta("canary_write", time.time())  # the write below is ours: check() labels it as routine
    st = path.stat()
    with open(path, "r+b") as f:  # in place: replacing the file would drop the audit rule that makes it a trap
        f.write(data)
        f.truncate()
    os.utime(path, (st.st_atime, st.st_mtime))  # keep its "been here a while" date
    st = path.stat()
    with _db() as c:
        c.execute("UPDATE decoys SET size=?, mtime=?, state='ok', changed=NULL WHERE lower(path)=?", (st.st_size, st.st_mtime, str(path).lower()))
    return {"ok": True}


def setup(q):
    """UI button: start the set-up with a UAC prompt."""
    if not _hs.IS_WIN:
        raise RuntimeError("Needs Windows")
    _init_db()
    exe, args = _hs._helper_cmd()
    args = args.replace("--hs-sniff", "--honey-setup")
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, str(Path(exe).parent), 0)
    if rc <= 32:
        raise RuntimeError("Windows did not start the set-up (administrator prompt declined?)")
    return {"ok": True}


# ----------------------------------------------------------------------------------------------- watcher (elevated helper)
def _last_record():
    rc, out, _err = _run(["wevtutil", "qe", "Security", "/c:1", "/rd:true", "/f:xml"])
    if rc or not out.strip():
        return 0
    try:
        return int(ET.fromstring(out.strip()).find("e:System/e:EventRecordID", EVT_NS).text)
    except (ET.ParseError, AttributeError, ValueError):
        return 0


def _parse(xml_text):
    """Events from wevtutil's /f:xml output -> list of dicts."""
    out = []
    try:
        root = ET.fromstring("<Events>" + xml_text + "</Events>")
    except ET.ParseError:
        return out
    for ev in root.findall("e:Event", EVT_NS):
        try:
            rid = int(ev.find("e:System/e:EventRecordID", EVT_NS).text)
            t = ev.find("e:System/e:TimeCreated", EVT_NS).get("SystemTime")
            ts = datetime.fromisoformat(t.replace("Z", "+00:00")[:26] + "+00:00" if "." in t else t.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            continue
        data = {d.get("Name"): (d.text or "") for d in ev.findall("e:EventData/e:Data", EVT_NS)}
        out.append({"rid": rid, "ts": ts, **data})
    return out


def _access(mask_hex):
    try:
        m = int(mask_hex, 16)
    except (TypeError, ValueError):
        return mask_hex or ""
    return ", ".join(n for b, n in MASK if m & b) or mask_hex


LOGON_TYPES = {2: "at the keyboard", 3: "over the network (file share)", 4: "a scheduled task", 5: "a Windows service",
               7: "at the keyboard (after unlocking)", 8: "over the network (plain-text log-on)", 9: "run-as with other credentials",
               10: "REMOTE DESKTOP", 11: "at the keyboard (cached log-on)", 0: "the Windows system"}


def _hex(v):
    v = v or ""
    try:
        return int(v, 16) if v.lower().startswith("0x") else int(v or 0)
    except ValueError:
        return 0


def _proc_details(pid, proc, logon_id):
    """Snapshot of the program that touched a decoy, taken within ~10 s while it is (usually) still running:
    command line, who started it, whether its exe is signed and by whom, its SHA-256, where it is connected to,
    and how its account was logged on (keyboard / Remote Desktop / network / service)."""
    q = lambda s: str(s).replace("'", "''")  # noqa: E731
    script = f"""$ErrorActionPreference = 'SilentlyContinue'; $id = {int(pid)}; $want = '{q(proc)}'; $o = [ordered]@{{}}
$p = Get-CimInstance Win32_Process -Filter "ProcessId=$id"
if ($p -and $want -and $p.ExecutablePath -ieq $want) {{
  $o.alive = $true; $o.cmd = $p.CommandLine; $o.started = $p.CreationDate.ToString('s')
  $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)"
  if ($pp -and $pp.CreationDate -le $p.CreationDate) {{
    $o.parent = $pp.Name; $o.parent_path = $pp.ExecutablePath; $o.parent_cmd = $pp.CommandLine
    $gp = Get-CimInstance Win32_Process -Filter "ProcessId=$($pp.ParentProcessId)"
    if ($gp -and $gp.CreationDate -le $pp.CreationDate) {{ $o.grandparent = $gp.Name }}
  }} else {{ $o.parent = "(pid $($p.ParentProcessId), already exited)" }}
  $chain = @(); $cur = $p; $n = 0; $via = $null
  if ($p.Name -ieq 'claude.exe') {{ $via = $p.ExecutablePath }}
  while ($cur -and $n -lt 8) {{
    $par = Get-CimInstance Win32_Process -Filter "ProcessId=$($cur.ParentProcessId)"
    if (-not $par -or $par.CreationDate -gt $cur.CreationDate) {{ break }}
    $chain += $par.Name
    if (-not $via -and $par.Name -ieq 'claude.exe') {{ $via = $par.ExecutablePath }}
    $cur = $par; $n++
  }}
  $o.chain = $chain
  if ($via) {{ $o.via_path = $via; $vs = Get-AuthenticodeSignature -LiteralPath $via; $o.via_sig = [string]$vs.Status
    if ($vs.SignerCertificate) {{ $o.via_signer = $vs.SignerCertificate.GetNameInfo('SimpleName', $false) }} }}
  $o.net =@(Get-NetTCPConnection -OwningProcess $id -State Established | Where-Object {{ $_.RemoteAddress -notmatch '^(127\\.|::1$|0\\.0\\.0\\.0)' }} |
    Select-Object -First 8 | ForEach-Object {{ "$($_.RemoteAddress):$($_.RemotePort)" }})
}} else {{ $o.alive = $false }}
if ($want -and (Test-Path -LiteralPath $want)) {{
  $s = Get-AuthenticodeSignature -LiteralPath $want; $o.sig = [string]$s.Status
  if ($s.SignerCertificate) {{ $o.signer = $s.SignerCertificate.GetNameInfo('SimpleName', $false) }}
  $o.sha256 = (Get-FileHash -LiteralPath $want -Algorithm SHA256).Hash
  $o.exe_created = (Get-Item -LiteralPath $want).CreationTime.ToString('s')
}}
$ls = Get-CimInstance Win32_LogonSession -Filter "LogonId='{int(logon_id)}'"
if ($ls) {{ $o.logon_type = [int]$ls.LogonType }}
$o | ConvertTo-Json -Compress -Depth 3"""
    try:
        out, _err = _hs._ps(script, 45)
        d = json.loads(out.strip() or "{}")
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        return {"err": str(e)[:150]}
    if "logon_type" in d:
        d["logon"] = LOGON_TYPES.get(d["logon_type"], f"log-on type {d['logon_type']}")
    elif int(logon_id) == 0x3E7:
        d["logon"] = "the Windows system"
    return _via(d, proc)


def _via(d, proc):
    """Was the program started through Claude Code (the AI assistant in the editor)? Decided by the signature on the
    claude.exe in its chain, never by the name alone: anything can call itself claude.exe."""
    if not d.get("via_path") and Path(proc or "").name.lower() == "claude.exe":  # it was claude.exe itself, already gone
        d["via_path"], d["via_sig"], d["via_signer"] = proc, d.get("sig"), d.get("signer")
    if d.get("via_path"):
        ok = d.get("via_sig") == "Valid" and "anthropic" in (d.get("via_signer") or "").lower()
        d["via"] = "Claude Code" if ok else ""
        d["via_fake"] = not ok
    return d


def poll_once(last):
    """Read new 4663 events after record `last`; store the ones about decoys. Returns the new `last`."""
    with _db() as c:
        paths = {r[0].lower(): r[0] for r in c.execute("SELECT path FROM decoys")}
        allow = {}  # real browser files: reads by the browser's own programs are normal and not stored
        if _meta("stores_on") == "1":
            for r in c.execute("SELECT path, allow FROM stores"):
                paths[r[0].lower()] = r[0]
                allow[r[0]] = tuple(json.loads(r[1] or "[]"))
    rc, out, err = _run(["wevtutil", "qe", "Security", f"/q:*[System[(EventID=4663) and (EventRecordID>{last})]]", "/f:xml", "/c:2000"], 120)
    if rc:
        _set_meta("watch_err", (err or out).strip()[:200])
        return last
    evs = _parse(out)
    todo = {}  # (pid, process, logon id) -> rids: one snapshot per program, however many events one open produced
    follow = []
    with _db() as c:
        for e in evs:
            last = max(last, e["rid"])
            p = paths.get((e.get("ObjectName") or "").lower())
            if not p:
                if _FOLLOWS:  # not a trap, but read by a program that is being followed: part of its trail
                    pid, name = _hex(e.get("ProcessId")), e.get("ObjectName") or ""
                    for fid, f in list(_FOLLOWS.items()):
                        if pid in f["pids"] and name and name not in f["files"] and len(f["files"]) < FOLLOW_FILES:
                            f["files"].add(name)
                            c.execute("INSERT INTO follow_events(fid, ts, type, what, detail) VALUES(?,?,?,?,?)",
                                      (fid, e["ts"], "file", name, f"{_access(e.get('AccessMask'))} by {Path(e.get('ProcessName') or '').name}"))
                continue
            proc = e.get("ProcessName") or ""
            if p in allow and (not (_hex(e.get("AccessMask")) & 1) or any(proc.lower().startswith(d + "\\") for d in allow[p])):
                continue
            base = Path(proc).name.lower() if proc else "system"
            user = "\\".join(x for x in (e.get("SubjectDomainName"), e.get("SubjectUserName")) if x)
            pid = _hex(e.get("ProcessId"))
            cur = c.execute("INSERT OR IGNORE INTO hits(rid, ts, path, process, pid, user, access, routine) VALUES(?,?,?,?,?,?,?,?)",
                            (e["rid"], e["ts"], p, proc, pid, user, _access(e.get("AccessMask")), ROUTINE.get(base, "")))
            if cur.rowcount and base not in ROUTINE:
                if (pid, proc, _hex(e.get("SubjectLogonId"))) not in todo and base != Path(sys.executable).name.lower():
                    follow.append((pid, proc, user, p, e["ts"]))
                todo.setdefault((pid, proc, _hex(e.get("SubjectLogonId"))), []).append(e["rid"])
    for f in follow:  # first, so as little as possible of what it does next is missed
        try:
            start_follow(*f)
        except Exception as e:  # noqa: BLE001
            _set_meta("watch_err", f"could not follow: {str(e)[:180]}")
    for (pid, proc, logon), rids in todo.items():
        d = json.dumps(_proc_details(pid, proc, logon))
        with _db() as c:
            c.executemany("UPDATE hits SET details=? WHERE rid=?", [(d, r) for r in rids])
    _set_meta("watch_err", "")
    return last


# ----------------------------------------------------------------------------------------------- following (elevated helper)
# When a trap is hit, the program that did it is followed for a while: what it connects to, what it starts, what else
# it reads. Recorded as a timeline in follows / follow_events.
PROC_AUDIT = "{0CCE922B-69AE-11D9-BED3-505054503030}"  # audit subcategory "Process Creation" (event 4688)
FOLLOW_SECS = 600
FOLLOW_MAX = 3  # at once
FOLLOW_FILES = 2000  # distinct files recorded per follow
_FOLLOWS = {}  # follow id -> {"pids": set, "files": set, "until": ts}
_FLOCK = threading.Lock()
_AUDIT_USERS = [0]  # follows currently needing the personal folders audited
_ALOCK = threading.Lock()
_ARMED = [False]


def _fev(fid, typ, what, detail="", ts=None):
    with _db() as c:
        c.execute("INSERT INTO follow_events(fid, ts, type, what, detail) VALUES(?,?,?,?,?)", (fid, ts or time.time(), typ, str(what)[:500], str(detail)[:1500]))


def _follow_dirs(arm, entries=None):
    """Audit every read in the user's personal folders (Documents, Desktop, Downloads, Pictures) while a follow runs.
    Sub-folders with thousands of files (code projects) are skipped: switching auditing on for them takes too long.
    -> (armed [[path, kind]], skipped [path], errors [str])"""
    q = lambda s: "'" + str(s).replace("'", "''") + "'"  # noqa: E731
    head = ("$sid = New-Object System.Security.Principal.SecurityIdentifier 'S-1-1-0'; "
            "$full = New-Object System.Security.AccessControl.FileSystemAuditRule($sid, 'ReadData', 'ContainerInherit, ObjectInherit', 'None', 'Success'); "
            "$flat = New-Object System.Security.AccessControl.FileSystemAuditRule($sid, 'ReadData', 'ObjectInherit', 'NoPropagateInherit', 'Success'); "
            "function Chg($p, $r, $k, $add) { try { $a = [System.IO.Directory]::GetAccessControl($p, 'Audit'); "
            "if ($add) { $a.AddAuditRule($r) } else { $a.RemoveAuditRuleSpecific($r) }; [System.IO.Directory]::SetAccessControl($p, $a); \"OK$k $p\" } "
            "catch { \"ERR $p : $($_.Exception.Message)\" } }; ")
    if arm:
        roots = ",".join(q(HOME / d) for d in ("Documents", "Downloads", "Pictures", "Desktop"))
        body = (f"foreach ($root in @({roots})) {{ if (-not (Test-Path -LiteralPath $root)) {{ continue }}; Chg $root $flat 'F' $true; "
                "foreach ($d in [System.IO.Directory]::GetDirectories($root)) { "
                "if ([System.IO.File]::GetAttributes($d) -band [System.IO.FileAttributes]::ReparsePoint) { continue }; "
                "$n = 0; try { foreach ($e in [System.IO.Directory]::EnumerateFileSystemEntries($d, '*', 'AllDirectories')) { $n++; if ($n -gt 3000) { break } } } catch {}; "
                "if ($n -gt 3000) { \"BIG $d\" } else { Chg $d $full 'R' $true } } }")
    else:
        body = "; ".join(f"Chg {q(p)} ${'flat' if k == 'F' else 'full'} '{k}' $false" for p, k in (entries or []))
        if not body:
            return [], [], []
    out, _err = _hs._ps(head + body, 900)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return ([[ln[4:], ln[2]] for ln in lines if ln.startswith(("OKF ", "OKR "))], [ln[4:] for ln in lines if ln.startswith("BIG ")],
            [ln[4:200] for ln in lines if ln.startswith("ERR ")])


def _follow_audit(on, fid=None):
    """Reference-counted: the folders are audited while at least one follow runs. Switching on and off is done one at a
    time, and always towards what the count says right now, so a short follow cannot leave auditing on behind it."""
    with _FLOCK:
        _AUDIT_USERS[0] = max(0, _AUDIT_USERS[0] + (1 if on else -1))
    with _ALOCK:
        want = _AUDIT_USERS[0] > 0
        try:
            if want and not _ARMED[0]:
                t0 = time.time()
                _set_meta("follow_dirs", "[]")
                _set_meta("follow_sacl", "1")
                armed, skipped, errs = _follow_dirs(True)
                _set_meta("follow_dirs", json.dumps(armed))
                _ARMED[0] = True
                if fid:
                    _fev(fid, "info", f"File tracking switched on {time.time() - t0:.0f} s after the trap was noticed" if armed
                         else "File tracking could not be switched on",
                         ("Reads in Documents, Desktop, Downloads and Pictures are listed from here on." if armed else "")
                         + (" Not tracked (too many files to switch on quickly): " + ", ".join(Path(s).name for s in skipped) + "." if skipped else "")
                         + (" Problems: " + "; ".join(errs[:3]) if errs else ""))
            elif not want and _ARMED[0]:
                _follow_dirs(False, json.loads(_meta("follow_dirs") or "[]"))
                _set_meta("follow_sacl", "0")
                _ARMED[0] = False
            elif want and fid:
                _fev(fid, "info", "File tracking was already on (another program is being followed)")
        except Exception as e:  # noqa: BLE001
            _set_meta("watch_err", f"file tracking: {str(e)[:180]}")


def _proc_events(last, back=0):
    """Program starts from the Security log (4688): after record `last`, or the last `back` seconds. -> (events, new last)"""
    q = (f"*[System[(EventID=4688) and TimeCreated[timediff(@SystemTime) <= {int(back * 1000)}]]]" if back
         else f"*[System[(EventID=4688) and (EventRecordID>{last})]]")
    rc, out, _err = _run(["wevtutil", "qe", "Security", f"/q:{q}", "/f:xml", "/c:8000"], 120)
    evs = _parse(out) if not rc else []
    return evs, max([last] + [e["rid"] for e in evs])


def start_follow(pid, proc, user, trap, ts):
    if not pid or pid == 4 or _meta("follow_on") == "0":
        return
    with _FLOCK:
        if len(_FOLLOWS) >= FOLLOW_MAX or any(pid in f["pids"] for f in _FOLLOWS.values()):
            return
        now = time.time()
        with _db() as c:
            fid = c.execute("INSERT INTO follows(ts, until, pid, process, user, trap, state) VALUES(?,?,?,?,?,?, 'following')",
                            (ts, now + FOLLOW_SECS, pid, proc, user, trap)).lastrowid
        _FOLLOWS[fid] = {"pids": {pid}, "files": set(), "until": now + FOLLOW_SECS}
    threading.Thread(target=_follow, args=(fid, pid, proc, ts), daemon=True, name=f"honey-follow-{fid}").start()


def _follow(fid, pid, proc, t0):
    st = _FOLLOWS[fid]
    names = {pid: Path(proc).name if proc else f"pid {pid}"}
    seen_net, children, io0, io1 = set(), set(), {}, {}
    last_rid, n, gone, exited = 0, 0, None, False
    threading.Thread(target=_follow_audit, args=(True, fid), daemon=True).start()
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        psutil = None
    try:
        while time.time() < st["until"]:
            alive = False
            if psutil:
                for p in list(st["pids"]):
                    try:
                        pr = psutil.Process(p)
                        if p == pid and proc and pr.exe().lower() != proc.lower():
                            continue  # the number was reused by another program
                        alive = True
                        w = pr.io_counters().write_bytes
                        io0.setdefault(p, w)
                        io1[p] = w
                        for ch in pr.children():
                            if ch.pid not in st["pids"]:
                                st["pids"].add(ch.pid)
                                children.add(ch.pid)
                                names[ch.pid] = ch.name()
                                try:
                                    cmd = " ".join(ch.cmdline())
                                except psutil.Error:
                                    cmd = ""
                                _fev(fid, "proc", ch.name(), f"{cmd} (started by {names.get(p, p)})", ch.create_time())
                    except (psutil.Error, OSError):
                        pass
                try:
                    for cn in psutil.net_connections(kind="inet"):
                        if cn.pid in st["pids"] and cn.raddr and cn.raddr.ip not in ("127.0.0.1", "::1", "0.0.0.0", "::"):
                            key = (cn.pid, cn.raddr.ip, cn.raddr.port)
                            if key not in seen_net:
                                seen_net.add(key)
                                _fev(fid, "net", f"{cn.raddr.ip}:{cn.raddr.port}", f"connection opened by {names.get(cn.pid, cn.pid)}")
                except (psutil.Error, OSError):
                    pass
            if n % 5 == 0:  # program starts from the Security log: catches ones too short-lived to be seen above
                evs, last_rid = _proc_events(last_rid, back=1800 if n == 0 else 0)
                made = {_hex(e.get("NewProcessId")): e for e in evs} if n == 0 else {}
                if n == 0:  # how the program itself came to run, up to four steps back
                    cur, chain = pid, []
                    while cur in made and len(chain) < 4 and made[cur]["ts"] <= t0 + 5:
                        e = made[cur]
                        chain.append(e)
                        cur = _hex(e.get("ProcessId"))
                    for e in chain:
                        _fev(fid, "origin", Path(e.get("NewProcessName") or "?").name,
                             f"{e.get('CommandLine') or e.get('NewProcessName') or ''} (started by {Path(e.get('ParentProcessName') or '').name or 'pid ' + str(_hex(e.get('ProcessId')))})", e["ts"])
                for e in evs:
                    new, by = _hex(e.get("NewProcessId")), _hex(e.get("ProcessId"))
                    if by in st["pids"] and new not in children and new != pid and (n > 0 or e["ts"] >= t0 - 1800):
                        if by == pid and proc and (e.get("ParentProcessName") or proc).lower() != proc.lower():
                            continue
                        st["pids"].add(new)
                        children.add(new)
                        names[new] = Path(e.get("NewProcessName") or "?").name
                        _fev(fid, "proc", names[new], f"{e.get('CommandLine') or e.get('NewProcessName') or ''} (started by {names.get(by, by)})", e["ts"])
            if psutil and not alive:
                if not exited:
                    exited = True
                    _fev(fid, "exit", "The program (and everything it started) has closed")
                gone = gone or time.time()
                if time.time() - gone > 30:  # a little longer, for events still on their way into the log
                    break
            else:
                gone, exited = None, False
            n += 1
            time.sleep(2)
    except Exception as e:  # noqa: BLE001
        _fev(fid, "info", "Following stopped early", str(e)[:300])
    finally:
        with _FLOCK:
            _FOLLOWS.pop(fid, None)
        wrote = sum(max(0, io1[p] - io0[p]) for p in io1)
        with _db() as c:
            cnt = dict(c.execute("SELECT type, COUNT(*) FROM follow_events WHERE fid=? GROUP BY type", (fid,)).fetchall())
            dests = [r[0] for r in c.execute("SELECT DISTINCT what FROM follow_events WHERE fid=? AND type='net' LIMIT 6", (fid,))]
            c.execute("UPDATE follows SET state='done', ended=?, summary=? WHERE id=?",
                      (time.time(), json.dumps({"files": cnt.get("file", 0), "procs": cnt.get("proc", 0), "nets": cnt.get("net", 0),
                                                "dests": dests, "wrote": wrote}), fid))
        _follow_audit(False)


LOGON_WHY = {"0xc0000064": "no such account", "0xc000006a": "wrong password", "0xc0000072": "account is disabled",
             "0xc0000234": "account is locked out", "0xc000015b": "account may not log on this way", "0xc000006d": "wrong name or password",
             "0xc000006e": "account restriction", "0xc0000070": "not allowed from that computer", "0xc0000071": "password expired"}
LOCAL_SRC = ("", "-", "127.0.0.1", "::1")


def poll_logons(last):
    """Failed log-ons (event 4625), plus any successful log-on (4624) to the bait account. Returns the new `last`."""
    bait = (_meta("bait_user") or "").lower()
    q = f"*[System[(EventID=4625) and (EventRecordID>{last})]]"
    if bait:
        q = (f"*[System[(EventID=4625 or EventID=4624) and (EventRecordID>{last})] and "
             f"(System[(EventID=4625)] or EventData[Data[@Name='TargetUserName']='{bait}'])]")
    rc, out, err = _run(["wevtutil", "qe", "Security", f"/q:{q}", "/f:xml", "/c:300"])
    if rc:
        _set_meta("watch_err", "log-on watch: " + (err or out).strip()[:180])
        return last
    with _db() as c:
        for e in _parse(out):
            last = max(last, e["rid"])
            user = e.get("TargetUserName") or ""
            ok = 1 if "FailureReason" not in e else 0
            if ok and user.lower() != bait:
                continue
            src = (e.get("IpAddress") or "").strip()
            why = "LOGGED ON" if ok else LOGON_WHY.get((e.get("SubStatus") or "").lower()) or LOGON_WHY.get((e.get("Status") or "").lower()) or "failed"
            c.execute("INSERT OR IGNORE INTO logons(rid, ts, user, src, host, ltype, reason, process, ok, bait, remote) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (e["rid"], e["ts"], user, "" if src == "-" else src, (e.get("WorkstationName") or "").strip("-"), _hex(e.get("LogonType")), why,
                       (e.get("ProcessName") or "").strip("-"), ok, 1 if bait and user.lower() == bait else 0, 0 if src in LOCAL_SRC else 1))
    return last


def port_loop():
    """Fake services (elevated helper): listen on the trap ports, note who connects and the first bytes they send, hang
    up. Nothing is ever sent back and nothing sent in is interpreted."""
    socks, seen, local = {}, {}, set()
    while True:
        try:
            on = _meta("ports_on") == "1"
            if on and not socks:
                failed = []
                for port in TRAP_PORTS:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                        s.bind(("0.0.0.0", port))
                        s.listen(8)
                        socks[s] = port
                    except OSError:  # something real already uses the port: leave it alone
                        s.close()
                        failed.append(port)
                _set_meta("ports_open", ",".join(str(p) for p in sorted(socks.values())))
                _set_meta("ports_failed", ",".join(str(p) for p in failed))
                try:
                    local = set(socket.gethostbyname_ex(socket.gethostname())[2])
                except OSError:
                    local = set()
            elif not on and socks:
                for s in socks:
                    s.close()
                socks = {}
                _set_meta("ports_open", "")
            if not socks:
                time.sleep(10)
                continue
            _set_meta("ports_hb", time.time())
            ready, _w, _x = select.select(list(socks), [], [], 5)
            for s in ready:
                conn, addr = s.accept()
                data = b""
                try:
                    conn.settimeout(1.5)
                    data = conn.recv(200)
                except OSError:
                    pass
                finally:
                    conn.close()
                minute = int(time.time() // 60)
                seen = {k: v for k, v in seen.items() if k[1] == minute}
                seen[(addr[0], minute)] = seen.get((addr[0], minute), 0) + 1
                if seen[(addr[0], minute)] > 20:  # a port scanner hammering away: 20 rows a minute says enough
                    continue
                text = "".join(ch if " " <= ch <= "~" else "." for ch in data.decode("latin-1"))[:120]
                with _db() as c:
                    c.execute("INSERT INTO probes(ts, src, sport, port, data, local) VALUES(?,?,?,?,?,?)",
                              (time.time(), addr[0], addr[1], socks[s], text, 1 if addr[0].startswith("127.") or addr[0] in local else 0))
            _set_meta("ports_err", "")
        except Exception as e:  # noqa: BLE001
            _set_meta("ports_err", str(e)[:200])
            time.sleep(10)


def watch_loop():
    """Runs inside the elevated helper for as long as it lives."""
    _init_db()
    threading.Thread(target=port_loop, daemon=True, name="honey-ports").start()
    try:  # a helper that was stopped in the middle of following something: finish its records, switch folder auditing off
        with _db() as c:
            c.execute("UPDATE follows SET state='done', ended=? WHERE state='following'", (time.time(),))
        if _meta("follow_sacl") == "1":
            _follow_dirs(False, json.loads(_meta("follow_dirs") or "[]"))
            _set_meta("follow_sacl", "0")
    except Exception as e:  # noqa: BLE001
        _set_meta("watch_err", f"file tracking clean-up: {str(e)[:180]}")
    last = int(_meta("last_rid") or 0) or _last_record()
    last_l = int(_meta("last_logon_rid") or 0) or last
    sentry = None
    try:  # intrusion checks: settings snapshots in their own thread, program starts polled below
        import sentry  # noqa: PLC0415
        threading.Thread(target=sentry.state_loop, daemon=True, name="sentry-state").start()
    except Exception as e:  # noqa: BLE001
        _set_meta("sentry_err", f"did not start: {str(e)[:180]}")
    last_s = int(_meta("last_sentry_rid") or 0) or _last_record()
    n = 0
    while True:
        try:
            last = poll_once(last)
            _set_meta("last_rid", last)
            last_l = poll_logons(last_l)
            _set_meta("last_logon_rid", last_l)
            if sentry:
                last_s = sentry.poll_events(last_s)
                _set_meta("last_sentry_rid", last_s)
            _set_meta("watch_hb", time.time())
            if n % 30 == 0:  # every ~5 minutes: is Windows still auditing? are the browser files still armed?
                rc, out, _err = _run(["auditpol", "/get", f"/subcategory:{FS_AUDIT}"])
                _set_meta("audit_on", "1" if ("Success" in out and rc == 0) else "0")
                if _meta("stores_on") == "1":
                    _set_meta("stores_err", "; ".join(_arm_stores())[:300])
        except Exception as e:  # noqa: BLE001
            _set_meta("watch_err", str(e)[:200])
        n += 1
        time.sleep(3 if _FOLLOWS else 10)  # following something: read its file trail from the log more often


# ----------------------------------------------------------------------------------------------- collector side (normal user)
def _label(proc):
    base = Path(proc).name.lower() if proc else "system"
    return FRIENDLY.get(base) or ROUTINE.get(base) or Path(proc).name or "unknown program"


def _detail_text(raw):
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return ""
    bits = []
    if d.get("logon"):
        bits.append(f"session: {d['logon']}")
    if d.get("via"):
        bits.append(f"started through {d['via']} (the AI assistant; its program is signed by {d.get('via_signer')})")
    elif d.get("via_fake"):
        bits.append(f"started through a program CALLING ITSELF claude.exe that is not signed by Anthropic ({d.get('via_path')})")
    if d.get("parent"):
        bits.append(f"started by {d['parent']}" + (f" (itself started by {d['grandparent']})" if d.get("grandparent") else ""))
    if d.get("sig"):
        bits.append(f"signed by {d['signer']}" if d["sig"] == "Valid" and d.get("signer") else f"NOT validly signed ({d['sig']})")
    if d.get("net"):
        bits.append("connected to " + ", ".join(d["net"][:4]))
    if d.get("cmd"):
        bits.append(f"command line: {d['cmd'][:200]}")
    if d.get("sha256"):
        bits.append(f"SHA-256 {d['sha256']}")
    return (". " + "; ".join(bits)) if bits else ""


def check(now):
    """New hits -> alerts; decoys that vanished or changed -> alerts (works even without auditing)."""
    _init_db()
    removing = now - float(_meta("removing") or 0) < 120 or now - float(_meta("setup_busy") or 0) < 300
    done = float(_meta("setup_done") or 0)
    with _db() as c:
        if done:  # set-up used to re-apply auditing with Set-Acl, which logged itself against the decoys: not someone looking
            c.execute("UPDATE hits SET routine='Device Watch set-up (REPAIR DECOYS)', alerted=1 WHERE routine='' AND ts BETWEEN ? AND ? "
                      "AND lower(process) LIKE '%\\powershell.exe' AND access LIKE 'change permissions%'", (done - 180, done + 10))
        cw = float(_meta("canary_write") or 0)
        if cw:  # the app itself rewriting the Amazon keys decoy (reporting keys pasted in on the TRAPS page)
            c.execute("UPDATE hits SET routine='Device Watch (decoy keys updated)', alerted=1 WHERE routine='' AND ts BETWEEN ? AND ? "
                      "AND lower(process) LIKE ? AND lower(path) LIKE '%\\.aws\\credentials'",
                      (cw - 5, cw + 30, "%\\" + Path(sys.executable).name.lower()))
        # wait (up to a minute) for the watcher's snapshot of the program, so the alert can say what it was
        ready = "alerted=0 AND (details IS NOT NULL OR routine != '' OR ts < ?)"
        new = [dict(r) for r in c.execute(f"SELECT * FROM hits WHERE {ready} ORDER BY ts", (now - 60,))]
        c.execute(f"UPDATE hits SET alerted=1 WHERE {ready}", (now - 60,))
        changes = []
        for d in c.execute("SELECT * FROM decoys").fetchall():
            p = Path(d["path"])
            if now - cw < 20 and d["path"].lower().endswith("\\.aws\\credentials"):
                continue  # being rewritten by canary() right now
            try:
                st = p.stat()
                state = "ok" if (st.st_size == d["size"] and abs(st.st_mtime - d["mtime"]) < 2) else "changed"
            except FileNotFoundError:
                state = "missing"
            except OSError:
                continue
            if state != d["state"]:
                c.execute("UPDATE decoys SET state=?, changed=? WHERE path=?", (state, now, d["path"]))
                if state != "ok":
                    changes.append((p, state))
    mine = {str(Path(sys.executable).resolve()).lower(), str(_hs.PROTECTED_DIR / Path(sys.executable).name).lower()}
    with _db() as c:
        stores = {r[0]: r[1] for r in c.execute("SELECT path, browser FROM stores")}
    groups = {}  # one open fires several events: one alert per decoy + program instance (per browser + program for the real stores)
    for h in new:
        if h["routine"] or (removing and (h["process"] or "").lower() in mine):
            continue
        browser = stores.get(h["path"])
        g = groups.setdefault((browser or h["path"], h["process"], h["pid"]), dict(h, accesses=[], files=[], browser=browser))
        for a in (h["access"] or "opened").split(", "):
            if a not in g["accesses"]:
                g["accesses"].append(a)
        if Path(h["path"]).name not in g["files"]:
            g["files"].append(Path(h["path"]).name)
        g["details"] = g["details"] or h["details"]
    for h in groups.values():
        who = f"{_label(h['process'])} ({h['process'] or 'unknown'}) as {h['user'] or 'unknown user'}{_detail_text(h['details'])}"
        if h["browser"]:
            msg = (f"{h['browser']}'s saved passwords / cookies ({', '.join(h['files'])}) were read by {who}. "
                   f"Only {h['browser']} itself should read these files: this is what password-stealing malware does.")
            title = f"Device Watch: {h['browser']} password files read by another program"
        else:
            msg = f"Decoy file {h['files'][0]} was accessed ({', '.join(h['accesses'])}) by {who}"
            title = "Device Watch: decoy file touched"
        _hs._event(2, "honey", msg, h["path"])
        if _hs._cfg()["notify"]:
            _hs._toast(title, msg)
    for p, state in changes:
        if removing:
            continue
        msg = f"Decoy file {p.name} was {'deleted' if state == 'missing' else 'changed'} ({p})"
        _hs._event(2, "honey", msg, str(p))
        if _hs._cfg()["notify"]:
            _hs._toast("Device Watch: decoy file " + ("deleted" if state == "missing" else "changed"), msg)
    _check_probes(now)
    _check_logons(now)
    _check_follows()
    import sentry  # noqa: PLC0415
    sentry.alerts(now)


def _size(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def _is_path(trap):
    """Follows start from a decoy file (a path) or from an intrusion-check alert (its title, e.g. a program run from Temp)."""
    return bool(re.match(r"^[a-z]:\\|^\\\\", trap or "", re.I))


def _check_follows():
    """A finished follow -> one summary alert (the timeline itself is on the TRAPS page)."""
    with _db() as c:
        done = [dict(r) for r in c.execute("SELECT * FROM follows WHERE state='done' AND alerted=0")]
        c.execute("UPDATE follows SET alerted=1 WHERE state='done' AND alerted=0")
    for f in done:
        try:
            s = json.loads(f["summary"] or "{}")
        except ValueError:
            s = {}
        if not (s.get("files") or s.get("procs") or s.get("nets")):
            continue  # nothing beyond the trap hit itself, which already raised its own alert
        trap = f["trap"] or ""
        why = f"after it touched {Path(trap).name}" if _is_path(trap) else f"after it was flagged ({trap})"
        msg = (f"Followed {_label(f['process'])} (pid {f['pid']}) {why}: it read {s.get('files', 0)} other file(s), "
               f"started {s.get('procs', 0)} program(s) and opened {s.get('nets', 0)} internet connection(s)"
               + (f" ({', '.join(s['dests'])})" if s.get("dests") else "")
               + (f", writing {_size(s['wrote'])} in all (files and network together)" if s.get("wrote") else "")
               + ". The full timeline is on the TRAPS page.")
        _hs._event(2, "honey", msg, f["process"] or "")
        if _hs._cfg()["notify"]:
            _hs._toast("Device Watch: what the program did next", msg)


def _mac(ip):
    try:
        _rc, out, _err = _run(["arp", "-a", ip], 10)
    except (subprocess.SubprocessError, OSError):
        return ""
    m = re.search(r"([0-9a-f]{2}(?:-[0-9a-f]{2}){5})", out, re.I)
    return m.group(1).replace("-", ":").lower() if m else ""


def _check_probes(now):
    """Connections to the fake services -> one alert per device per hour (every connection stays in the table)."""
    with _db() as c:
        new = [dict(r) for r in c.execute("SELECT * FROM probes WHERE alerted=0 ORDER BY ts")]
        if not new:
            return
        c.execute("UPDATE probes SET alerted=1 WHERE alerted=0")
        by_src = {}
        for p in new:
            if not p["local"]:
                by_src.setdefault(p["src"], []).append(p)
        for src, rows in by_src.items():
            mac = _mac(src)
            if mac:
                c.execute("UPDATE probes SET mac=? WHERE src=? AND mac IS NULL", (mac, src))
            if c.execute("SELECT 1 FROM probes WHERE src=? AND alerted=2 AND ts>?", (src, now - 3600)).fetchone():
                continue
            c.execute("UPDATE probes SET alerted=2 WHERE id=?", (rows[0]["id"],))
            ports = sorted({r["port"] for r in rows})
            sent = next((r["data"] for r in rows if r["data"]), "")
            msg = (f"A device on your network ({src}{', network card ' + mac if mac else ''}) tried to connect to "
                   f"{', '.join(f'{p} ({TRAP_PORTS.get(p, chr(63))})' for p in ports)} on this PC, {len(rows)} time{'s' if len(rows) > 1 else ''}. "
                   "Nothing real runs on these ports, so it was looking for a way in"
                   + (f". It sent: {sent[:80]}" if sent else "."))
            _hs._event(2, "honey", msg, src)
            if _hs._cfg()["notify"]:
                _hs._toast("Device Watch: something probed this PC", msg)


def _check_logons(now):
    """Log-on attempts on the bait account, or failed log-ons from another machine -> one alert per account + source per hour."""
    with _db() as c:
        new = [dict(r) for r in c.execute("SELECT * FROM logons WHERE alerted=0 ORDER BY ts")]
        if not new:
            return
        c.execute("UPDATE logons SET alerted=1 WHERE alerted=0")
        if any(not g["remote"] and not g["ok"] for g in new):   # wrong password typed at this PC: webcam photo (INTRUDER tab)
            try:
                import guard  # noqa: PLC0415
                guard.intruder_photo("wrong password typed at the PC", now)
            except Exception:  # noqa: BLE001
                pass
        groups = {}
        for g in new:
            if g["bait"] or g["remote"] or g["ok"]:
                groups.setdefault((g["user"].lower(), g["src"]), []).append(g)
        for (user, src), rows in groups.items():
            ok = any(r["ok"] for r in rows)
            if not ok and c.execute("SELECT 1 FROM logons WHERE lower(user)=? AND src=? AND alerted=2 AND ts>?", (user, src, now - 3600)).fetchone():
                continue
            c.execute("UPDATE logons SET alerted=2 WHERE rid=?", (rows[0]["rid"],))
            r = rows[0]
            frm = f"from {src}{' (' + r['host'] + ')' if r['host'] else ''}" if r["remote"] else "from this PC itself"
            if ok:
                msg = f"SOMEONE LOGGED ON to the bait account '{r['user']}' {frm}. Nobody knows its password, so treat this PC as compromised."
            elif r["bait"]:
                msg = (f"{len(rows)} log-on attempt{'s' if len(rows) > 1 else ''} on the bait account '{r['user']}' {frm} ({r['reason']}). "
                       "Nothing legitimate uses this account: someone or something is guessing passwords.")
            else:
                msg = (f"{len(rows)} failed log-on{'s' if len(rows) > 1 else ''} as '{r['user']}' {frm} ({r['reason']}). "
                       "Another machine is trying to get into this PC.")
            if r["process"]:
                msg += f" Program: {r['process']}."
            _hs._event(2, "honey", msg, src or r["user"])
            if _hs._cfg()["notify"]:
                _hs._toast("Device Watch: log-on attempt", msg)


# ----------------------------------------------------------------------------------------------- start-up watch (normal user)
AUTORUN_PS = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$ErrorActionPreference='SilentlyContinue'
function ExePath($cmd){ if(-not $cmd){return ''}; $c=[Environment]::ExpandEnvironmentVariables(([string]$cmd).Trim())
  if($c.StartsWith('"')){ return $c.Split('"')[1] }; $m=[regex]::Match($c,'^(.+?\.(exe|dll|sys|com|bat|cmd|ps1|vbs|js))',1); if($m.Success){return $m.Groups[1].Value}; return $c.Split(' ')[0] }
$o=New-Object System.Collections.ArrayList
function Add($k,$n,$v,$f){ [void]$o.Add([ordered]@{kind=$k; name=[string]$n; value=[string]$v; file=[string]$f}) }
$runs=@('HKLM:\Software\Microsoft\Windows\CurrentVersion\Run','HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run',
  'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce','HKLM:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run',
  'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run','HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce','HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run')
foreach($k in $runs){ $i=Get-ItemProperty $k; if($i){ $i.PSObject.Properties | ?{$_.Name -notlike 'PS*'} | %{ Add 'run' ($k+'\'+$_.Name) $_.Value (ExePath $_.Value) } } }
$w=Get-ItemProperty 'HKLM:\Software\Microsoft\Windows NT\CurrentVersion\Winlogon'
foreach($n in 'Shell','Userinit'){ Add 'winlogon' $n $w.$n (ExePath (([string]$w.$n).Split(',')[0])) }
$sh=New-Object -ComObject WScript.Shell
foreach($d in @([Environment]::GetFolderPath('Startup'),[Environment]::GetFolderPath('CommonStartup'))){ Get-ChildItem -LiteralPath $d -File | ?{$_.Name -ne 'desktop.ini'} | %{
  if($_.Extension -eq '.lnk'){ $l=$sh.CreateShortcut($_.FullName); Add 'startup' $_.FullName ($l.TargetPath+' '+$l.Arguments).Trim() $l.TargetPath }
  else { Add 'startup' $_.FullName ("$($_.Length) bytes, changed "+$_.LastWriteTime.ToString('s')) $_.FullName } } }
Get-ScheduledTask | %{ $a=@($_.Actions | ?{$_.Execute}); Add 'task' ($_.TaskPath+$_.TaskName) ((@($a | %{ ($_.Execute+' '+$_.Arguments).Trim() }) -join ' | ')) $(if($a.Count){ExePath $a[0].Execute}else{''}) }
Get-CimInstance Win32_Service | ?{ $_.Name -notmatch '_[0-9a-f]{3,8}$' } | %{ Add 'service' $_.Name $_.PathName (ExePath $_.PathName) }
ConvertTo-Json @($o) -Depth 3 -Compress
"""
AUTORUN_KIND = {"run": "Start-up registry entry", "winlogon": "Windows log-on setting", "startup": "Startup folder item",
                "task": "Scheduled task", "service": "Windows service"}
# Windows' own tools that run whatever they are told: an entry that starts one of these is running a script or command
SCRIPT_HOSTS = {"powershell.exe", "pwsh.exe", "cmd.exe", "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "regsvr32.exe",
                "msiexec.exe", "bitsadmin.exe", "certutil.exe", "conhost.exe", "forfiles.exe", "python.exe", "pythonw.exe", "curl.exe"}


def _sig_info(path, timeout=45, with_hash=True):
    """-> {sig, signer, sha256}; {} when the check itself failed (e.g. timed out on a very large installer)."""
    if not path:
        return {}
    q = str(path).replace("'", "''")
    script = (f"$ErrorActionPreference = 'SilentlyContinue'; $p = '{q}'; $o = [ordered]@{{}}; "
              "if (-not (Test-Path -LiteralPath $p)) { $c = Get-Command $p -CommandType Application | Select-Object -First 1; if ($c) { $p = $c.Source } }; "
              "if (Test-Path -LiteralPath $p) { $s = Get-AuthenticodeSignature -LiteralPath $p; $o.sig = [string]$s.Status; "
              "if ($s.SignerCertificate) { $o.signer = $s.SignerCertificate.GetNameInfo('SimpleName', $false) }; "
              + ("$o.sha256 = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash " if with_hash else "")
              + "} else { $o.sig = 'Missing' }; $o | ConvertTo-Json -Compress")
    try:
        out, _err = _hs._ps(script, timeout)
        return json.loads(out.strip() or "{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}


# Windows' own "tips and suggestions" notifications: it re-creates these per-user tasks (a built-in handler, no program
# file) every time it schedules a tip, so a new one is routine.
SOFTLANDING = re.compile(r"^\\SoftLanding\\S-1-5-21-[\d-]+\\(SoftLanding|ActionCenter)(Trigger|RefreshForExpiration|Deferral)Task-[\w{}-]+$")


def autorun_scan(now):
    """Everything set to start by itself (start-up registry entries, Startup folders, scheduled tasks, services): the first
    scan is the baseline; later, anything new or changed is an alert. Malware has to add itself here to survive a restart."""
    out, _err = _hs._ps(AUTORUN_PS, 180)
    items = json.loads(out.strip() or "[]")
    cur = {(i["kind"], i["name"]): i for i in (items if isinstance(items, list) else [items])}
    if len(cur) < 30:  # a scan that failed half-way must not look like everything changed
        raise RuntimeError(f"start-up scan returned only {len(cur)} items")
    first = not _meta("autoruns_baselined")
    events = []
    with _db() as c:
        old = {(r["kind"], r["name"]): r for r in c.execute("SELECT * FROM autoruns").fetchall()}
        for k, i in cur.items():
            o = old.get(k)
            if o is None:
                c.execute("INSERT INTO autoruns VALUES(?,?,?,?,?)", (i["kind"], i["name"], i["value"], i["file"], now))
                events.append(("new", i, ""))
            elif (o["value"] or "") != (i["value"] or ""):
                c.execute("UPDATE autoruns SET value=?, file=? WHERE kind=? AND name=?", (i["value"], i["file"], *k))
                events.append(("changed", i, o["value"] or ""))
        # entries that disappeared stay remembered: programs such as Edge add and remove their own start-up entry all the
        # time, and the same entry coming back unchanged is not news
    _set_meta("autoruns_scan", now)
    if first:
        _set_meta("autoruns_baselined", now)
        return
    loud = 0
    for change, i, was in events[:40]:
        s = _sig_info(i["file"])
        base = Path(i["file"] or "").name.lower()
        ms = s.get("sig") == "Valid" and "microsoft" in (s.get("signer") or "").lower()
        own = "devicewatch" in i["name"].lower().replace(" ", "") or "marketterminal" in (i["value"] or "").lower()
        note = ""
        if base in SCRIPT_HOSTS:
            note = "runs a script or command through a Windows tool, the way malware often hides: read the command"
        elif s.get("sig") and s.get("sig") != "Valid":
            note = "the program is not validly signed" if s["sig"] != "Missing" else "the program file does not exist (yet)"
        # quiet: this app's own entries, and Windows' own tasks/services pointing at Microsoft-signed programs (updates do this)
        quiet = own or (ms and base not in SCRIPT_HOSTS and (i["kind"] == "service" or i["name"].startswith("\\Microsoft\\"))) \
            or (i["kind"] == "task" and not i["value"] and bool(SOFTLANDING.match(i["name"])))
        level = 1 if quiet else 2
        with _db() as c:
            c.execute("INSERT INTO autorun_events(ts, kind, name, value, old, file, change, sig, signer, sha256, level, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (now, i["kind"], i["name"], i["value"], was, i["file"], change, s.get("sig") or "", s.get("signer") or "", s.get("sha256") or "", level, note))
        signed = f"signed by {s['signer']}" if s.get("sig") == "Valid" and s.get("signer") else f"signature: {s.get('sig') or 'unknown'}"
        kind = AUTORUN_KIND.get(i["kind"], i["kind"])
        msg = (f"{'New' if change == 'new' else 'Changed'} {kind if kind.startswith('Windows') else kind[0].lower() + kind[1:]}: {i['name']} now runs:{i['value'] or '(built-in Windows component)'} "
               f"({signed}){'. Before: ' + was if was else ''}{'. Note: ' + note if note else ''}")
        _hs._event(level, "honey", msg, i["name"])
        if level == 2 and _hs._cfg()["notify"]:
            loud += 1
            if loud <= 3:
                _hs._toast("Device Watch: something new starts with Windows", msg)


def _collector():
    next_scan = time.time() + 45
    while True:
        try:
            check(time.time())
        except Exception as e:  # noqa: BLE001
            print(f"honey: {e}")
        if time.time() >= next_scan:
            next_scan = time.time() + 300
            try:
                if _meta("autoruns_on") == "1":
                    autorun_scan(time.time())
                    _set_meta("autoruns_err", "")
            except Exception as e:  # noqa: BLE001
                _set_meta("autoruns_err", str(e)[:200])
        time.sleep(15)


_STARTED = []


def start():
    if _STARTED:
        return
    _STARTED.append(1)
    _init_db()
    threading.Thread(target=_collector, daemon=True, name="honey").start()


def status(q):
    _init_db()
    now = time.time()
    with _db() as c:
        decoys = [dict(r) for r in c.execute("SELECT * FROM decoys ORDER BY path")]
        hits = [dict(r) for r in c.execute("SELECT * FROM hits ORDER BY ts DESC LIMIT 200")]
        meta = {r[0]: r[1] for r in c.execute("SELECT k, v FROM meta")}
    last = {}
    for h in hits:
        if not h["routine"] and not h.get("reviewed"):
            last.setdefault(h["path"], h["ts"])
    for d in decoys:
        d["name"] = Path(d["path"]).name
        d["folder"] = str(Path(d["path"]).parent)
        d["last_touch"] = last.get(d["path"])
    for h in hits:
        h["name"] = Path(h["path"]).name
        h["program"] = _label(h["process"])
        try:
            h["details"] = json.loads(h.get("details") or "null")
        except ValueError:
            h["details"] = None
    with _db() as c:
        stores = [dict(r) for r in c.execute("SELECT path, browser, armed FROM stores ORDER BY path")]
        probes = [dict(r) for r in c.execute("SELECT * FROM probes ORDER BY ts DESC LIMIT 150")]
        logons = [dict(r) for r in c.execute("SELECT * FROM logons ORDER BY ts DESC LIMIT 150")]
        aevents = [dict(r) for r in c.execute("SELECT * FROM autorun_events ORDER BY ts DESC, id DESC LIMIT 100")]
        acount = c.execute("SELECT COUNT(*) FROM autoruns").fetchone()[0]
        follows = [dict(r) for r in c.execute("SELECT * FROM follows ORDER BY ts DESC LIMIT 15")]
        for f in follows:
            f["events"] = [dict(r) for r in c.execute("SELECT ts, type, what, detail FROM follow_events WHERE fid=? ORDER BY ts, id LIMIT 600", (f["id"],))]
            f["total"] = c.execute("SELECT COUNT(*) FROM follow_events WHERE fid=?", (f["id"],)).fetchone()[0]
    for f in follows:
        f["program"] = _label(f["process"])
        f["trap_name"] = Path(f["trap"] or "").name if _is_path(f["trap"]) else (f["trap"] or "")
        f["flagged"] = not _is_path(f["trap"])
        try:
            f["summary"] = json.loads(f["summary"] or "null")
        except ValueError:
            f["summary"] = None
    browser_of = {s["path"]: s["browser"] for s in stores}
    for s in stores:
        s["name"] = Path(s["path"]).name
        s["folder"] = str(Path(s["path"]).parent)
        s["last_touch"] = last.get(s["path"])
    for h in hits:
        h["browser"] = browser_of.get(h["path"]) or ""
    for p in probes:
        p["service"] = TRAP_PORTS.get(p["port"], "")
    for e in aevents:
        e["kind_name"] = AUTORUN_KIND.get(e["kind"], e["kind"])
    num = lambda s: [int(x) for x in (s or "").split(",") if x]  # noqa: E731
    extra = {
        "stores": stores, "stores_on": meta.get("stores_on") == "1", "stores_err": meta.get("stores_err") or "",
        "ports": {"on": meta.get("ports_on") == "1", "open": num(meta.get("ports_open")), "failed": num(meta.get("ports_failed")),
                  "listening": now - float(meta.get("ports_hb") or 0) < 60, "err": meta.get("ports_err") or "",
                  "names": {str(k): v for k, v in TRAP_PORTS.items()}},
        "probes": probes,
        "autoruns": {"on": meta.get("autoruns_on") == "1", "count": acount, "baselined": float(meta.get("autoruns_baselined") or 0),
                     "scan": float(meta.get("autoruns_scan") or 0), "err": meta.get("autoruns_err") or "", "events": aevents},
        "logons": logons, "bait_user": meta.get("bait_user") or "",
        "follows": follows, "follow_on": meta.get("follow_on") == "1", "follow_secs": FOLLOW_SECS,
        "skipped": meta.get("skipped") or "", "cleanup_needed": meta.get("cleanup_needed") == "1",
    }
    cn = _canary()
    extra["canary"] = {"set": bool(cn), "key": (cn["key"][:4] + "…" + cn["key"][-4:]) if cn else "", "ts": (cn or {}).get("ts") or 0,
                       "possible": any(d["name"] == "credentials" for d in decoys)}
    try:
        import sentry  # noqa: PLC0415
        extra["sentry"] = sentry.status()
    except Exception as e:  # noqa: BLE001
        extra["sentry"] = {"on": False, "err": str(e)[:200], "events": [], "snap": None}
    hb = float(meta.get("watch_hb") or 0)
    return {**extra, "set_up": bool(decoys), "decoys": decoys, "hits": hits, "watching": now - hb < 90, "watch_hb": hb,
            "audit_on": meta.get("audit_on") == "1", "setup_err": meta.get("setup_err") or "", "watch_err": meta.get("watch_err") or "",
            "setup_done": float(meta.get("setup_done") or 0), "busy": now - float(meta.get("setup_busy") or 0) < 300}
