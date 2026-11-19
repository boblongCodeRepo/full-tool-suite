"""Launchpad: one home screen for every app, with live status on each tile, plus an entertainment corner
(internet radio, games, today's picture / article / on-this-day from Wikipedia).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from datetime import date
from pathlib import Path

import hotspot as _hs

LP_DIR = _hs.DATA_DIR / "launch"
LP_DIR.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": "MarketTerminal-Launchpad/1.0 (personal desktop app)"}
DESKTOP = Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"
EXTERNAL = {
    "transit": {"port": 8502, "dir": DESKTOP / "website for mini pc with train times" / "Transit Hub"},
    "travel": {"port": 8503, "dir": DESKTOP / "Travel Deals Hub"},
}
_TODAY = {"day": None, "data": None}
_TODAY_LOCK = threading.Lock()


def _up(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=1.5) as r:
            return r.read().decode().strip() == "ok"
    except Exception:  # noqa: BLE001
        return False


def _safe(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def summary(q):
    """Everything the launchpad tiles show, gathered in one call."""
    now = time.time()
    out = {}

    def alerts():
        import device  # noqa: PLC0415
        with device._db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS events(ts REAL, level INT, kind TEXT, msg TEXT, ref TEXT)")
            rows = [dict(r) for r in c.execute("SELECT ts, level, kind, msg FROM events WHERE ts > ? ORDER BY ts DESC", (now - 86400,))]
        serious = [r for r in rows if r["level"] >= 2]
        return {"n": len(serious), "all": len(rows), "latest": serious[0] if serious else None}

    def vacuum():
        s = _hs.status({})
        vac = next((c for c in s.get("clients") or [] if "roborock" in (c.get("host") or "").lower()), None)
        return {"hotspot": s.get("on"), "online": bool(vac and vac.get("online")), "name": (vac or {}).get("host") or "",
                "up_day": s.get("up_day"), "tracking": s.get("sniff")}

    def bt():
        import bluetooth  # noqa: PLC0415
        s = bluetooth.status({})
        t = s.get("trackers") or []
        return {"radio": s.get("radio"), "scanning": s.get("scanning"), "in_range": s.get("now"),
                "trackers": len(t), "suspicious": sum(1 for x in t if x["verdict"] == "suspicious")}

    def traps():
        import honey  # noqa: PLC0415
        s = honey.status({})
        d = s.get("decoys") or []
        return {"set_up": s.get("set_up"), "decoys": len(d), "intact": sum(1 for x in d if x["state"] == "ok"),
                "touched": sum(1 for h in s.get("hits") or [] if not h["routine"]), "watching": s.get("watching")}

    out["alerts"] = _safe(alerts)
    out["vacuum"] = _safe(vacuum)
    out["bluetooth"] = _safe(bt)
    out["traps"] = _safe(traps)
    out["external"] = {k: {"up": _up(v["port"]), "url": f"http://localhost:{v['port']}", "installed": v["dir"].is_dir()} for k, v in EXTERNAL.items()}
    return out


def start(q):
    """Start the DC Transit Hub (in a window) or the Travel Deals Hub. Both are separate apps on this PC."""
    app = q.get("app")
    ext = EXTERNAL.get(app)
    if not ext:
        raise ValueError("unknown app")
    d = ext["dir"]
    if not d.is_dir():
        raise RuntimeError(f"{d} not found")
    if _up(ext["port"]):
        return {"ok": True, "already": True}
    if app == "transit":  # the same thing its own "DC Transit Hub" desktop icon does
        subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
                          "-File", str(d / "set-kiosk-mode.ps1"), "window"], cwd=str(d), creationflags=_hs.NO_WINDOW)
    else:
        py = d / ".venv" / "Scripts" / "python.exe"
        if not py.is_file():
            raise RuntimeError("The Travel Deals Hub has not been set up yet (run its start.bat once)")
        subprocess.Popen([str(py), "-m", "streamlit", "run", "app.py", "--server.headless", "true"], cwd=str(d),
                         creationflags=_hs.NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}


# ----------------------------------------------------------------------------------------------- today on Wikipedia
def _fetch_today(d):
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{d:%Y/%m/%d}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        j = json.loads(r.read())
    wiki = lambda t: "https://en.wikipedia.org/wiki/" + (t or "").replace(" ", "_")  # noqa: E731
    im = j.get("image") or {}
    tfa = j.get("tfa") or {}
    return {
        "date": d.isoformat(),
        "picture": {"title": (im.get("title") or "").removeprefix("File:").rsplit(".", 1)[0],
                    "thumb": (im.get("thumbnail") or {}).get("source"),
                    "desc": ((im.get("description") or {}).get("text") or "").strip(),
                    "url": im.get("file_page") or ""} if im else None,
        "article": {"title": tfa.get("normalizedtitle"), "extract": tfa.get("extract"),
                    "thumb": (tfa.get("thumbnail") or {}).get("source"),
                    "url": ((tfa.get("content_urls") or {}).get("desktop") or {}).get("page") or wiki(tfa.get("normalizedtitle"))} if tfa else None,
        "onthisday": [{"year": e.get("year"), "text": e.get("text"),
                       "url": ((((e.get("pages") or [{}])[0].get("content_urls") or {}).get("desktop") or {}).get("page")) or ""}
                      for e in (j.get("onthisday") or [])[:8]],
        "mostread": [{"title": a.get("normalizedtitle"), "views": a.get("views"),
                      "url": ((a.get("content_urls") or {}).get("desktop") or {}).get("page") or wiki(a.get("normalizedtitle"))}
                     for a in ((j.get("mostread") or {}).get("articles") or []) if a.get("normalizedtitle") not in ("Main Page",)][:10],
    }


def today(q):
    d = date.today()
    f = LP_DIR / "today.json"
    with _TODAY_LOCK:
        if _TODAY["day"] == d and _TODAY["data"]:
            return _TODAY["data"]
        try:
            cached = json.loads(f.read_text(encoding="utf-8"))
            if cached.get("date") == d.isoformat():
                _TODAY.update(day=d, data=cached)
                return cached
        except (OSError, ValueError):
            cached = None
        try:
            data = _fetch_today(d)
        except Exception as e:  # noqa: BLE001
            if cached:
                return {**cached, "stale": f"showing {cached.get('date')}: {e}"}
            raise RuntimeError(f"Wikipedia did not answer: {e}") from e
        f.write_text(json.dumps(data), encoding="utf-8")
        _TODAY.update(day=d, data=data)
        return data
