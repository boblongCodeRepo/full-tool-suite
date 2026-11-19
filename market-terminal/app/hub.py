"""Hub: the services every app in the suite shares.

  * a small server-side store for things that used to live only in one browser window's localStorage (watchlist,
    price alerts, holdings, saved auction searches, watched lots, watch terms, settings), so background jobs can
    act on them while no window is open
  * notifications: a log, a Windows balloon, and (only if you switch it on) a push to your phone through ntfy
  * the sentinel: one background thread that checks price alerts, forwards serious Device Watch alerts, runs saved
    auction searches, warns when watched lots are about to end, watches Influence Watch terms and Wikipedia
    attention surges, records the daily value of your holdings, snapshots the Influence Watch databases and builds
    the daily digest

The sentinel runs whenever the local server runs: while any app window is open, or all the time with Device Watch's
always-on logging (`--collect`).
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("MT_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MarketTerminal")
HUB_DIR = DATA_DIR / "hub"
HUB_DIR.mkdir(parents=True, exist_ok=True)
IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000
_LOCK = threading.RLock()
_CACHE = {}

SYM_RX = re.compile(r"[A-Z0-9^=.\-]{1,15}")
ALERT_OPS = {"above": "Price above", "below": "Price below", "up": "Day move >= +%", "down": "Day move <= -%"}
SETTINGS = {  # key -> (default, cleaner)
    "toast_on": (True, bool),
    "ntfy_on": (False, bool),
    "ntfy_server": ("https://ntfy.sh", lambda v: str(v).strip().rstrip("/")[:200]),
    "ntfy_topic": ("", lambda v: re.sub(r"[^A-Za-z0-9_\-]", "", str(v))[:64]),
    "push_min": (1, lambda v: max(0, min(2, int(v)))),          # lowest level that goes to the phone
    "push_device": (True, bool),                                 # forward serious Device Watch alerts
    "digest_on": (True, bool),                                   # build the daily digest (kept as brief history)
    "digest_push": (False, bool),                                # ...and send it to the phone
    "digest_time": ("07:30", lambda v: v if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(v)) else "07:30"),
    "sig_watch": (False, bool),                                  # Wikipedia attention surges on the watchlist
    "auc_min_score": (70, lambda v: max(0, min(100, int(v)))),
    "vacuum_match": ("roborock", lambda v: str(v).strip().lower()[:40]),
    "ledger_port": (8780, lambda v: max(1, min(65535, int(v)))),
}


# ----------------------------------------------------------------------------------------------- store
def _file(k):
    return HUB_DIR / f"{k}.json"


def _load(k, default=None):
    with _LOCK:
        if k in _CACHE:
            return _CACHE[k]
        try:
            v = json.loads(_file(k).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            v = default
        _CACHE[k] = v
        return v


def _save(k, v):
    with _LOCK:
        tmp = _file(k).with_suffix(".tmp")
        tmp.write_text(json.dumps(v), encoding="utf-8")
        os.replace(tmp, _file(k))   # a crash mid-write leaves the old file, never half a file
        _CACHE[k] = v
    return v


def _sym(s):
    s = str(s or "").strip().upper()
    if not SYM_RX.fullmatch(s):
        raise ValueError(f"Not a ticker: {s[:20]!r}")
    return s


def _num(v, what="number"):
    try:
        f = float(str(v).replace(",", "").replace("$", "").replace("%", ""))
    except (TypeError, ValueError):
        raise ValueError(f"Not a {what}: {str(v)[:20]!r}") from None
    if f != f or abs(f) == float("inf"):
        raise ValueError(f"Not a {what}")
    return f


def _clean_watchlist(v):
    return list(dict.fromkeys(_sym(s) for s in v))[:100]


def _clean_holdings(v):
    out = []
    for h in list(v)[:200]:
        qty = _num(h.get("qty"), "quantity")
        if qty == 0:
            continue
        out.append({"sym": _sym(h.get("sym")), "qty": qty, "cost": max(0.0, _num(h.get("cost") or 0, "cost")),
                    "acct": str(h.get("acct") or "").strip()[:40]})
    return out


def _clean_searches(v):
    out = []
    for s in list(v)[:12]:
        q = str(s.get("q") or "").strip()[:80]
        prof = str(s.get("profile") or "").strip()[:20]
        if not q and not prof:
            continue
        out.append({"id": re.sub(r"[^a-z0-9]+", "-", (q or prof).lower()).strip("-")[:40] + ("-q" if q else "-p"),
                    "q": q, "profile": prof, "min_score": max(0, min(100, int(s.get("min_score") or 70))),
                    "max_price": _num(s["max_price"]) if s.get("max_price") not in (None, "") else None,
                    "states": [x for x in re.split(r"[\s,]+", str(s.get("states") or "").upper()) if re.fullmatch(r"[A-Z]{2}", x)][:20]})
    return list({s["id"]: s for s in out}.values())


_LOT_KEYS = ("id", "src", "title", "url", "img", "price", "bids", "ends", "city", "state", "seller", "score", "why", "risk", "saved", "buynow", "snip")


def _clean_lots(v):
    out = {}
    for k, it in list(dict(v).items())[:300]:
        if isinstance(it, dict) and it.get("id"):
            out[str(k)[:120]] = {f: it[f] for f in _LOT_KEYS if f in it}
    return out


def _clean_terms(v):
    out = []
    for t in list(v)[:30]:
        term = re.sub(r"\s+", " ", str(t.get("term") if isinstance(t, dict) else t)).strip()[:60]
        if len(term) >= 3:
            out.append({"term": term})
    return list({t["term"].lower(): t for t in out}.values())


def _clean_settings(v):
    cur = settings()
    for k, val in dict(v).items():
        if k in SETTINGS:
            cur[k] = SETTINGS[k][1](val)
    return cur


CLEAN = {"watchlist": _clean_watchlist, "holdings": _clean_holdings, "auc_searches": _clean_searches,
         "auc_watch": _clean_lots, "iw_watches": _clean_terms, "settings": _clean_settings}


def settings():
    saved = _load("settings") or {}
    return {k: saved.get(k, d) for k, (d, _) in SETTINGS.items()}


def get(q):
    """GET /api/hub/get?k=watchlist  ->  {"k": ..., "v": ...}  (v is null until something has been saved)"""
    k = q.get("k", "")
    if k == "settings":
        return {"k": k, "v": settings()}
    if k not in CLEAN:
        raise ValueError("unknown key")
    return {"k": k, "v": _load(k)}


def put(q):
    """POST /api/hub/set  {"k": "watchlist", "v": [...]}"""
    k = q.get("k", "")
    if k not in CLEAN:
        raise ValueError("unknown key")
    v = q.get("v")
    if isinstance(v, str):
        v = json.loads(v)
    with _LOCK:
        return {"k": k, "v": _save(k, CLEAN[k](v))}


# ----------------------------------------------------------------------------------------------- notifications
def _toast(title, text):
    if not IS_WIN:
        return
    q = lambda s: str(s).replace("'", "''").replace("\r", " ").replace("\n", " ")[:240]  # noqa: E731
    script = ("Add-Type -AssemblyName System.Windows.Forms, System.Drawing; $n = New-Object System.Windows.Forms.NotifyIcon; "
              "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
              f"$n.ShowBalloonTip(20000, '{q(title)}', '{q(text)}', 'Info'); Start-Sleep 15; $n.Dispose()")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
                         creationflags=NO_WINDOW if IS_WIN else 0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _push(title, text, level, url=""):
    """Send to the phone through ntfy. Returns "" on success, else the reason it did not go."""
    s = settings()
    if not s["ntfy_on"]:
        return "phone notifications are off"
    if not s["ntfy_topic"]:
        return "no ntfy topic set"
    if not re.match(r"https://", s["ntfy_server"]):
        return "the ntfy server address must start with https://"
    body = {"topic": s["ntfy_topic"], "title": title[:120], "message": text[:1500], "priority": (2, 3, 4)[max(0, min(2, level))]}
    if re.match(r"https://", url or ""):
        body["click"] = url
    req = urllib.request.Request(s["ntfy_server"], data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "MarketTerminal-Hub/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return "" if r.status < 300 else f"ntfy answered {r.status}"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:160]


def notify(kind, title, text, level=1, url="", toast=True, push=True):
    """level: 0 info, 1 worth knowing, 2 act on it. Always logged; balloon and phone depend on settings."""
    s = settings()
    sent = ""
    if push and s["ntfy_on"] and level >= s["push_min"]:
        sent = _push(title, text, level, url) or "ok"
    if toast and s["toast_on"]:
        _toast(title, text)
    with _LOCK:
        log = _load("notify_log") or []
        log.insert(0, {"ts": time.time(), "kind": kind, "level": level, "title": title, "text": text, "url": url, "push": sent})
        _save("notify_log", log[:300])


def notify_api(q):
    """GET  /api/hub/notify            -> settings + recent notifications + what the sentinel last did
       POST /api/hub/notify_set {...}   -> save settings
       POST /api/hub/notify_test        -> send a test notification"""
    return {"settings": settings(), "log": (_load("notify_log") or [])[: int(q.get("n") or 60)],
            "jobs": {k: {x: v[x] for x in ("last", "err", "every", "note")} for k, v in _JOBS.items()}, "running": _RUN["on"]}


def notify_set(q):
    v = q.get("v") if "v" in q else {k: q[k] for k in q if k in SETTINGS}
    if isinstance(v, str):
        v = json.loads(v)
    for k in ("toast_on", "ntfy_on", "push_device", "digest_on", "digest_push", "sig_watch"):
        if k in v and isinstance(v[k], str):
            v[k] = v[k].lower() in ("1", "true", "on", "yes")
    with _LOCK:
        return {"settings": _save("settings", _clean_settings(v))}


def notify_test(q):
    s = settings()
    why = _push("Market Terminal test", "Phone notifications from your apps are working.", 1) if s["ntfy_on"] else "phone notifications are off"
    if s["toast_on"]:
        _toast("Market Terminal test", "Desktop notifications are working.")
    return {"push": why or "ok", "toast": s["toast_on"]}


# ----------------------------------------------------------------------------------------------- price alerts
def alerts(q):
    """One endpoint, small operations, so a window never overwrites a hit the sentinel just recorded.
       op: (none)=list | add | del | rearm | clear (triggered) | seen | import (a localStorage list, once)"""
    op = q.get("op", "")
    with _LOCK:
        al = _load("alerts")
        fresh = al is None
        al = al or []
        if op == "add":
            if q.get("opk") not in ALERT_OPS:
                raise ValueError("unknown condition")
            if len(al) >= 200:
                raise ValueError("Too many alerts (limit 200)")
            al.append({"id": f"{int(time.time() * 1000):x}{len(al):02x}", "sym": _sym(q.get("sym")), "op": q["opk"],
                       "val": _num(q.get("val"), "level"), "note": str(q.get("note") or "").strip()[:200], "created": time.time() * 1000})
        elif op == "del":
            al = [a for a in al if a["id"] != q.get("id")]
        elif op == "rearm":
            for a in al:
                if a["id"] == q.get("id"):
                    for k in ("hit", "hitPx", "seen"):
                        a.pop(k, None)
        elif op == "clear":
            al = [a for a in al if not a.get("hit")]
        elif op == "seen":
            for a in al:
                if a.get("hit"):
                    a["seen"] = True
        elif op == "import" and fresh:
            old = q.get("v")
            old = json.loads(old) if isinstance(old, str) else (old or [])
            for i, a in enumerate(list(old)[:200]):
                try:
                    if a.get("op") in ALERT_OPS:
                        al.append({"id": f"{int(a.get('created') or 0):x}{i:02x}", "sym": _sym(a.get("sym")), "op": a["op"], "val": _num(a.get("val")),
                                   "note": str(a.get("note") or "")[:200], "created": a.get("created") or time.time() * 1000,
                                   **({"hit": a["hit"], "hitPx": a.get("hitPx"), "seen": bool(a.get("seen"))} if a.get("hit") else {})})
                except ValueError:
                    continue
        if op or fresh:
            _save("alerts", al)
        return {"alerts": al, "imported": fresh, "server": True}


def _alert_hit(a, q):
    last, prev = (q or {}).get("last"), (q or {}).get("prev")
    if last is None:
        return False
    p = (last / prev - 1) * 100 if prev else None
    if a["op"] == "above":
        return last >= a["val"]
    if a["op"] == "below":
        return last <= a["val"]
    if a["op"] == "up":
        return p is not None and p >= a["val"]
    if a["op"] == "down":
        return p is not None and p <= -abs(a["val"])
    return False


def _job_alerts():
    live = [a for a in (_load("alerts") or []) if not a.get("hit")]
    if not live:
        return "no active alerts"
    import data  # noqa: PLC0415
    qs = data.quotes(sorted({a["sym"] for a in live})[:60])
    fired = []
    with _LOCK:
        al = _load("alerts") or []
        for a in al:
            if a.get("hit") or not _alert_hit(a, qs.get(a["sym"])):
                continue
            x = qs[a["sym"]]
            a["hit"], a["hitPx"] = time.time() * 1000, x["last"]
            fired.append((a, x))
        if fired:
            _save("alerts", al)
    for a, x in fired:
        p = (x["last"] / x["prev"] - 1) * 100 if x.get("prev") else 0.0
        lvl = f"{a['val']:g}%" if a["op"] in ("up", "down") else f"{a['val']:,.2f}"
        notify("price", f"Price alert: {a['sym']}", f"{a['sym']} {ALERT_OPS[a['op']].lower()} {lvl}: now {x['last']:,.2f} ({p:+.2f}% today)"
               + (f". {a['note']}" if a.get("note") else ""), 2)
    return f"{len(live)} active, {len(fired)} triggered"


# ----------------------------------------------------------------------------------------------- holdings
def _holdings_view():
    hs = _load("holdings") or []
    syms = sorted({h["sym"] for h in hs if h["sym"] != "CASH"})
    qs = {}
    if syms:
        import data  # noqa: PLC0415
        qs = data.quotes(syms)
    rows, tot_v, tot_c, tot_d, tot_prev = [], 0.0, 0.0, 0.0, 0.0
    for h in hs:
        x = {"last": 1.0, "prev": 1.0} if h["sym"] == "CASH" else qs.get(h["sym"]) or {}
        last, prev = x.get("last"), x.get("prev")
        val = h["qty"] * last if last is not None else None
        basis = h["qty"] * h["cost"] if h["cost"] else None
        day = h["qty"] * (last - prev) if last is not None and prev is not None else None
        rows.append({**h, "last": last, "prev": prev, "value": val, "basis": basis, "day": day,
                     "pl": val - basis if val is not None and basis else None,
                     "pl_pct": (val / basis - 1) * 100 if val is not None and basis else None,
                     "m1": x.get("m1"), "ytd": x.get("ytd"), "hi52": x.get("hi52"), "lo52": x.get("lo52"),
                     "spark": (x.get("spark") or [])[-30:], "asof": x.get("asof")})
        if val is not None:
            tot_v += val
            tot_c += basis if basis else val     # a position without a cost counts at market value in the totals
            tot_d += day or 0.0
            tot_prev += val - (day or 0.0)
    for r in rows:
        r["weight"] = r["value"] / tot_v * 100 if r["value"] is not None and tot_v else None
    rows.sort(key=lambda r: -(r["value"] or 0))
    return {"rows": rows, "value": tot_v, "basis": tot_c, "day": tot_d, "day_pct": tot_d / tot_prev * 100 if tot_prev else None,
            "pl": tot_v - tot_c, "pl_pct": (tot_v / tot_c - 1) * 100 if tot_c else None,
            "missing": [r["sym"] for r in rows if r["value"] is None]}


def port(q):
    """GET /api/port -> positions with live prices, totals and the recorded daily value history."""
    v = _holdings_view()
    v["hist"] = _load("port_hist") or []
    return v


def _job_port():
    if not (_load("holdings") or []):
        return "no holdings"
    v = _holdings_view()
    if not v["value"] or v["missing"]:
        return "prices incomplete, skipped"
    day = date.today().isoformat()
    with _LOCK:
        hist = [h for h in (_load("port_hist") or []) if h[0] != day]
        hist.append([day, round(v["value"], 2), round(v["basis"], 2)])
        _save("port_hist", hist[-1500:])
    return f"value {v['value']:,.0f}"


# ----------------------------------------------------------------------------------------------- Device Watch -> phone
def _job_device():
    s = settings()
    if not (s["ntfy_on"] and s["push_device"]):
        return "forwarding off"
    import device  # noqa: PLC0415
    st = _load("state") or {}
    last = st.get("dev_ts")
    if last is None:  # first run: start from now, never replay the old log to the phone
        with _LOCK:
            _save("state", {**(_load("state") or {}), "dev_ts": time.time()})
        return "started"
    with device._db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS events(ts REAL, level INT, kind TEXT, msg TEXT, ref TEXT)")
        rows = [dict(r) for r in c.execute("SELECT ts, level, kind, msg, ref FROM events WHERE ts > ? AND level >= 2 ORDER BY ts LIMIT 40", (last,))]
    if not rows:
        return "nothing new"
    recent = st.get("dev_sent") or {}
    now, sent = time.time(), 0
    recent = {k: t for k, t in recent.items() if now - t < 6 * 3600}
    for r in rows:
        key = f"{r['kind']}|{r['ref'] or r['msg'][:60]}"
        if key in recent or sent >= 5:   # the same thing once per 6 hours, and never a flood
            continue
        recent[key] = now
        sent += 1
        notify("device", "Device Watch alert", r["msg"], 2, toast=False)
    with _LOCK:
        _save("state", {**(_load("state") or {}), "dev_ts": rows[-1]["ts"], "dev_sent": recent})
    return f"{len(rows)} new, {sent} sent"


# ----------------------------------------------------------------------------------------------- auctions
def _lot_ok(it, s):
    return (it.get("score", 0) >= s["min_score"] and (s["max_price"] is None or it.get("price") is None or it["price"] <= s["max_price"])
            and (not s["states"] or (it.get("state") or "").upper() in s["states"]) and not it.get("risk"))


def _fmt_left(secs):
    return f"{int(secs // 3600)}h {int(secs % 3600 // 60)}m" if secs >= 3600 else f"{max(1, int(secs // 60))}m"


def _job_auctions():
    searches = _load("auc_searches") or []
    lots = _load("auc_watch") or {}
    if not searches and not lots:
        return "nothing saved"
    import auctions  # noqa: PLC0415
    seen = _load("auc_seen") or {}
    news = _load("auc_new") or {}
    found = 0
    for s in searches:
        res = auctions.search({"q": s["q"]} if s["q"] else {"profile": s["profile"]})
        good = [it for it in res["items"] if _lot_ok(it, s)]
        first = s["id"] not in seen
        old = set(seen.get(s["id"]) or [])
        new = [it for it in good if it["id"] not in old]
        seen[s["id"]] = list({*old, *(it["id"] for it in res["items"])})[-4000:]
        if first or not new:
            continue
        found += len(new)
        news[s["id"]] = ([{k: it.get(k) for k in _LOT_KEYS if k in it} | {"found": time.time()} for it in new] + (news.get(s["id"]) or []))[:40]
        top = new[:3]
        what = f"\"{s['q']}\"" if s["q"] else s["profile"]
        notify("auction", f"{len(new)} new auction lot{'s' if len(new) > 1 else ''} for {what}",
               "; ".join(f"{it['title'][:70]} (${it['price']:,.0f}, score {it['score']})" if it.get("price") is not None else f"{it['title'][:70]} (score {it['score']})" for it in top)
               + (f" and {len(new) - 3} more" if len(new) > 3 else ""), 1, url=top[0].get("url") or "")
    live = {s["id"] for s in searches}
    with _LOCK:
        _save("auc_seen", {k: v for k, v in seen.items() if k in live})
        _save("auc_new", {k: v for k, v in news.items() if k in live})
    # watched lots about to end
    st = _load("state") or {}
    warned = set(st.get("auc_warned") or [])
    now, ending = time.time(), 0
    for it in lots.values():
        left = (it.get("ends") or 0) - now
        if 0 < left < 2 * 3600 and it["id"] not in warned:
            warned.add(it["id"])
            ending += 1
            notify("auction", f"Ending in {_fmt_left(left)}: {str(it.get('title'))[:80]}",
                   (f"Last seen at ${it['price']:,.0f}" if isinstance(it.get("price"), (int, float)) else "Open the listing for the price")
                   + (f", {it['bids']} bids" if it.get("bids") is not None else "") + f" on {it.get('src')}.", 2, url=it.get("url") or "")
    with _LOCK:
        _save("state", {**(_load("state") or {}), "auc_warned": [i for i in warned if i in lots][-500:]})
    return f"{len(searches)} searches, {found} new lots, {ending} ending soon"


def auc_new(q):
    """GET /api/hub/auc_new -> new lots the saved searches found, per search (cleared with ?clear=id)."""
    with _LOCK:
        news = _load("auc_new") or {}
        if q.get("clear"):
            news.pop(q["clear"], None)
            _save("auc_new", news)
    return {"searches": _load("auc_searches") or [], "new": news, "job": {x: _JOBS["auctions"][x] for x in ("last", "err", "note")}}


# ----------------------------------------------------------------------------------------------- Influence Watch terms
def _job_iw():
    terms = _load("iw_watches") or []
    if not terms:
        return "no watch terms"
    import watch  # noqa: PLC0415
    watch.ensure()
    st = _load("state") or {}
    marks = st.get("iw_marks") or {}
    hits_log = _load("iw_hits") or {}
    now, total = time.time(), 0
    for t in terms:
        key = t["term"].lower()
        since = marks.get(key)
        marks[key] = now
        if since is None:
            continue
        like = "%" + key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = watch._q("SELECT src, grp, cc, title, url, ts FROM items WHERE fetched > ? AND (lower(title) LIKE ? ESCAPE '\\' "
                        "OR lower(summary) LIKE ? ESCAPE '\\') ORDER BY ts DESC LIMIT 60", (since, like, like))
        if not rows:
            continue
        total += len(rows)
        state = sum(1 for r in rows if r[1] == "state")
        hits_log[key] = ([{"src": r[0], "grp": r[1], "cc": r[2], "title": r[3], "url": r[4], "ts": r[5]} for r in rows] + (hits_log.get(key) or []))[:60]
        notify("influence", f"{len(rows)} new item{'s' if len(rows) > 1 else ''} mention \"{t['term']}\"",
               (f"{state} from state media. " if state else "") + "Latest: " + str(rows[0][3])[:160], 1, url=rows[0][4] or "")
    live = {t["term"].lower() for t in terms}
    with _LOCK:
        _save("state", {**(_load("state") or {}), "iw_marks": {k: v for k, v in marks.items() if k in live}})
        _save("iw_hits", {k: v for k, v in hits_log.items() if k in live})
    return f"{len(terms)} terms, {total} new items"


def iw_hits(q):
    return {"terms": _load("iw_watches") or [], "hits": _load("iw_hits") or {}, "job": {x: _JOBS["influence"][x] for x in ("last", "err", "note")}}


# ----------------------------------------------------------------------------------------------- Signal Monitor surges
def _job_signal():
    if not settings()["sig_watch"]:
        return "off"
    wl = [s for s in (_load("watchlist") or []) if not re.search(r"[=^]", s)][:30]
    if not wl:
        return "watchlist is empty"
    import apps  # noqa: PLC0415
    st = _load("state") or {}
    told = st.get("sig_told") or {}
    today, n = date.today().isoformat(), 0
    for s in wl:
        r = apps.scan_one(s)
        if r.get("retry"):
            return f"Wikimedia rate limit after {n} tickers; will retry"
        n += 1
        z = r.get("z")
        if z is not None and z >= 2.5 and told.get(s) != today:
            told[s] = today
            notify("signal", f"Attention surge: {s}", f"Wikipedia page views for {r.get('name') or s} are {z:.1f} standard deviations above normal "
                   f"({r.get('last'):,.0f} views vs a usual {r.get('avg60'):,.0f}).", 1)
        time.sleep(1.0)
    with _LOCK:
        _save("state", {**(_load("state") or {}), "sig_told": {k: v for k, v in told.items() if v == today}})
    return f"{n} tickers checked"


# ----------------------------------------------------------------------------------------------- database snapshots
def _snapshot(src, keep=3):
    """Consistent copy of a live SQLite file (SQLite's own backup API), checked, newest `keep` kept."""
    if not src.is_file():
        return None
    out_dir = src.parent / "snapshots"
    out_dir.mkdir(exist_ok=True)
    dst = out_dir / f"{src.stem}-{date.today():%Y%m%d}.db"
    if dst.exists():
        return True
    tmp = dst.with_suffix(".tmp")
    ok = False
    try:
        a = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
        b = sqlite3.connect(tmp)
        try:
            a.backup(b)
            ok = b.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            a.close()
            b.close()
    except sqlite3.Error:
        ok = False
    if ok:
        os.replace(tmp, dst)
        for old in sorted(out_dir.glob(f"{src.stem}-*.db"))[:-keep]:
            old.unlink(missing_ok=True)
    else:
        tmp.unlink(missing_ok=True)
    return ok


def _job_snapshots():
    res = []
    for name in ("archive.db", "money.db"):
        ok = _snapshot(DATA_DIR / "influence" / name)
        if ok is None:
            continue
        res.append(f"{name} {'ok' if ok else 'FAILED'}")
        if not ok:
            st = _load("state") or {}
            if st.get("snap_warned") != date.today().isoformat():
                with _LOCK:
                    _save("state", {**(_load("state") or {}), "snap_warned": date.today().isoformat()})
                good = sorted((DATA_DIR / "influence" / "snapshots").glob(f"{Path(name).stem}-*.db"))
                notify("health", f"Influence Watch: {name} failed its integrity check",
                       "The database could not be copied cleanly, which usually means it is damaged. "
                       + (f"The last good snapshot is {good[-1].name}." if good else "There is no earlier snapshot."), 2)
    return ", ".join(res) or "no databases yet"


# ----------------------------------------------------------------------------------------------- Home Ledger bridge
def ledger(q=None):
    """Home Ledger is a separate server on this PC. It answers a summary to requests from this PC only."""
    port_ = settings()["ledger_port"]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port_}/api/local/summary", timeout=2.5) as r:
            return {"up": True, "url": f"http://127.0.0.1:{port_}", **json.loads(r.read())}
    except Exception as e:  # noqa: BLE001
        return {"up": False, "url": f"http://127.0.0.1:{port_}", "why": f"{type(e).__name__}"}


# ----------------------------------------------------------------------------------------------- daily digest
def _digest_lines():
    lines = []
    import data  # noqa: PLC0415
    idx = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^TNX", "10-year yield"), ("BTC-USD", "Bitcoin")]
    wl = [s for s in (_load("watchlist") or [])][:40]
    try:
        qs = data.quotes([s for s, _ in idx] + wl)
    except Exception as e:  # noqa: BLE001
        qs = {}
        lines.append(f"Markets: prices unavailable ({type(e).__name__}).")
    bits = []
    for s, name in idx:
        x = qs.get(s)
        if x and x.get("prev"):
            bits.append(f"{name} {x['last']:,.2f}{'%' if s == '^TNX' else ''} ({(x['last'] - x['prev']) * 100:+.0f} bp)" if s == "^TNX"
                        else f"{name} {x['last']:,.0f} ({(x['last'] / x['prev'] - 1) * 100:+.2f}%)")
    if bits:
        lines.append("Markets: " + ", ".join(bits) + ".")
    movers = sorted(((s, (qs[s]["last"] / qs[s]["prev"] - 1) * 100) for s in wl if qs.get(s) and qs[s].get("prev")), key=lambda x: -abs(x[1]))
    big = [f"{s} {p:+.1f}%" for s, p in movers if abs(p) >= 2][:5]
    if wl:
        lines.append("Watchlist: " + (", ".join(big) if big else "nothing moved 2% or more") + ".")
    try:
        v = _holdings_view()
        if v["rows"]:
            lines.append(f"Holdings: ${v['value']:,.0f}, {v['day']:+,.0f} on the day"
                         + (f" ({v['day_pct']:+.2f}%)" if v["day_pct"] is not None else "") + f", {v['pl']:+,.0f} overall.")
    except Exception:  # noqa: BLE001
        pass
    al = _load("alerts") or []
    hit = [a for a in al if a.get("hit") and time.time() * 1000 - a["hit"] < 86400e3]
    if hit:
        lines.append("Price alerts triggered in the last day: " + ", ".join(sorted({a["sym"] for a in hit})) + ".")
    try:
        import device  # noqa: PLC0415
        with device._db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS events(ts REAL, level INT, kind TEXT, msg TEXT, ref TEXT)")
            n = c.execute("SELECT COUNT(*) FROM events WHERE ts > ? AND level >= 2", (time.time() - 86400,)).fetchone()[0]
        lines.append(f"This PC: {n} Device Watch alert{'s' if n != 1 else ''} in the last 24 hours." if n else "This PC: no Device Watch alerts in the last 24 hours.")
    except Exception:  # noqa: BLE001
        pass
    lg = ledger()
    if lg.get("up") and lg.get("ready"):
        due = lg.get("bills") or []
        week = [b for b in due if b["days"] <= 7]
        if week:
            lines.append("Bills due within a week: " + ", ".join(f"{b['name']} ${b['amount'] / 100:,.0f} ({'overdue' if b['days'] < 0 else 'today' if b['days'] == 0 else 'in ' + str(b['days']) + 'd'})" for b in week[:6]) + ".")
        else:
            lines.append("Bills: nothing due in the next week.")
    lots = [it for it in (_load("auc_watch") or {}).values() if 0 < (it.get("ends") or 0) - time.time() < 86400]
    if lots:
        lines.append("Watched auction lots ending today: " + "; ".join(str(it.get("title"))[:50] for it in lots[:4]) + ".")
    return lines


def _job_digest():
    s = settings()
    if not s["digest_on"]:
        return "off"
    today = date.today().isoformat()
    briefs = _load("briefs") or []
    if briefs and briefs[0]["date"] == today:
        return "already built today"
    if datetime.now().strftime("%H:%M") < s["digest_time"]:
        return f"waiting for {s['digest_time']}"
    lines = _digest_lines()
    if not lines:
        return "nothing to say"
    with _LOCK:
        _save("briefs", ([{"date": today, "built": time.time(), "lines": lines}] + [b for b in briefs if b["date"] != today])[:120])
    if s["digest_push"]:
        notify("digest", f"Morning brief, {date.today():%a %b %d}", "\n".join(lines), 1, toast=False)
    return f"built, {len(lines)} lines"


def briefs(q):
    """GET /api/hub/briefs -> past daily digests, newest first (?now=1 also returns one built on the spot)."""
    out = {"briefs": _load("briefs") or [], "settings": {k: settings()[k] for k in ("digest_on", "digest_push", "digest_time")}}
    if q.get("now"):
        out["now"] = _digest_lines()
    return out


# ----------------------------------------------------------------------------------------------- sentinel
_JOBS = {  # name -> how often (s), first run after start-up (s)
    "alerts": {"fn": _job_alerts, "every": 60, "delay": 20},
    "device": {"fn": _job_device, "every": 30, "delay": 30},
    "holdings": {"fn": _job_port, "every": 3600, "delay": 90},
    "auctions": {"fn": _job_auctions, "every": 3600, "delay": 240},
    "influence": {"fn": _job_iw, "every": 900, "delay": 150},
    "signal": {"fn": _job_signal, "every": 6 * 3600, "delay": 600},
    "snapshots": {"fn": _job_snapshots, "every": 6 * 3600, "delay": 900},
    "digest": {"fn": _job_digest, "every": 300, "delay": 120},
}
for _j in _JOBS.values():
    _j.update(last=0.0, err="", note="", due=0.0)
_RUN = {"on": False}
_START = threading.Lock()


def run_job(name):
    j = _JOBS[name]
    try:
        j["note"], j["err"] = str(j["fn"]() or ""), ""
    except Exception as e:  # noqa: BLE001  (one failing job must never stop the others)
        j["err"] = f"{type(e).__name__}: {e}"[:200]
    j["last"] = time.time()
    return j


def _loop():
    t0 = time.time()
    for j in _JOBS.values():
        j["due"] = t0 + j["delay"]
    while True:
        now = time.time()
        for name, j in _JOBS.items():
            if now >= j["due"]:
                run_job(name)
                j["due"] = time.time() + j["every"]
        time.sleep(5)


def start():
    with _START:
        if not _RUN["on"]:
            _RUN["on"] = True
            threading.Thread(target=_loop, daemon=True, name="hub-sentinel").start()


def run(q):
    """POST /api/hub/run?job=auctions -> run one sentinel job now (the apps' CHECK NOW buttons)."""
    name = q.get("job", "")
    if name not in _JOBS:
        raise ValueError("unknown job")
    j = run_job(name)
    return {"job": name, "note": j["note"], "err": j["err"], "last": j["last"]}
