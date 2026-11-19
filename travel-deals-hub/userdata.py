"""Your own settings and imported data, kept in data/user.json (never overwritten by the weekly update)."""
from __future__ import annotations

import json
import pathlib
import threading

PATH = pathlib.Path(__file__).resolve().parent / "data" / "user.json"
_lock = threading.Lock()

DEFAULTS: dict = {
    "watchlist": ["Tokyo", "Lisbon", "Hawaii", "Iceland"],
    "fare_alerts": [
        {"name": "Europe under $600 from DC", "where": "Europe", "max_price": 600, "from_home": True},
        {"name": "Caribbean under $350 from DC", "where": "Caribbean", "max_price": 350, "from_home": True},
    ],
    "alert_toast": True,
    "email": {"enabled": False, "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 587,
              "username": "", "to": ""},
    "ai_summary": True,
    "imported": {"amex": [], "delta": []},
    "imported_at": {"amex": "", "delta": ""},
    "alerted_links": [],
    "kiosk": {"train_seconds": 120, "travel_seconds": 45},
}


def load() -> dict:
    with _lock:
        try:
            data = json.loads(PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
    merged = json.loads(json.dumps(DEFAULTS))
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def save(data: dict) -> None:
    with _lock:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(PATH)


def update(fn) -> dict:
    data = load()
    fn(data)
    save(data)
    return data
