"""Fare alerts and the destination watchlist."""
from __future__ import annotations

import re

import fetch


def _place_rx(place: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(place.strip())}\b", re.I)


def where_matches(item: dict, where: str) -> bool:
    where = (where or "").strip()
    if not where or where.lower() in ("anywhere", "any"):
        return True
    if where in fetch.REGIONS:
        return where in item.get("regions", [])
    return bool(_place_rx(where).search(f"{item.get('dest') or ''} {item['title']}"))


def rule_matches(item: dict, rule: dict) -> bool:
    if "flight" not in item["tags"] or item.get("expired"):
        return False
    if rule.get("from_home") and not item.get("home"):
        return False
    max_price = rule.get("max_price")
    if max_price:
        if not item.get("price_usd") or item["price_usd"] > max_price:
            return False
    return where_matches(item, rule.get("where", ""))


def fare_matches(items: list[dict], rules: list[dict]) -> list[tuple[dict, dict]]:
    out = []
    for item in items:
        for rule in rules:
            if rule_matches(item, rule):
                out.append((rule, item))
                break
    return out


def watch_matches(items: list[dict], watchlist: list[str]) -> list[tuple[str, dict]]:
    out = []
    for item in items:
        if item.get("expired"):
            continue
        text = f"{item.get('dest') or ''} {item['title']}"
        for place in watchlist:
            if not place.strip():
                continue
            if (place in fetch.REGIONS and place in item.get("regions", [])) or _place_rx(place).search(text):
                out.append((place, item))
                break
    return out


def notify_new(snap: dict, user: dict) -> list[tuple[dict, dict]]:
    """Pops up a Windows notification for fare-alert matches not already announced."""
    import notify
    import userdata

    matches = fare_matches(snap["items"], user.get("fare_alerts", []))
    seen = set(user.get("alerted_links", []))
    new = [(r, i) for r, i in matches if i["link"] not in seen]
    if not new:
        return []
    if user.get("alert_toast", True):
        first_rule, first = new[0]
        price = f"${first['price_usd']:,} " if first.get("price_usd") else ""
        more = f" (+{len(new) - 1} more)" if len(new) > 1 else ""
        notify.toast(f"Fare alert: {first_rule['name']}", f"{price}{first['title'][:120]}{more}")

    def mark(d):
        d["alerted_links"] = (d.get("alerted_links", []) + [i["link"] for _, i in new])[-500:]
    userdata.update(mark)
    return new
