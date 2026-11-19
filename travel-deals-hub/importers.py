"""Turns text copied from the Amex or Delta experiences pages (Ctrl+A, Ctrl+C) into event rows.

Pages change, so this is a best-effort reader: the app shows the rows in an editable table before saving.
"""
from __future__ import annotations

import datetime as dt
import re

MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
         r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
DATE_RE = re.compile(rf"\b{MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*[-–]\s*(?:{MONTH}\.?\s+)?\d{{1,2}})?"
                     rf"(?:,?\s+20\d\d)?\b|\b\d{{1,2}}/\d{{1,2}}/(?:20)?\d\d\b", re.I)
PRICE_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\s*(?:miles|points|pts)\b|\b\d+(?:\.\d)?k\s*(?:miles|points)\b|"
                      r"\$\s?\d[\d,]*(?:\.\d\d)?", re.I)
KIND_RE = re.compile(r"\b(presale|reserved tickets?|reserved|auction|current bid|buy now|raffle|sweepstakes|"
                     r"redeem|instant purchase|sold out|waitlist)\b", re.I)
BOILER_RE = re.compile(
    r"^(get tickets|buy tickets|learn more|view details|see details|details|bid now|place (?:a )?bid|bid|"
    r"sign in|log ?in|log ?out|menu|filter|filters|sort(?: by)?|search|load more|show more|view all|see all|"
    r"terms apply|terms|share|favorite|add to calendar|set reminder|remind me|close|back|next|previous|"
    r"home|benefits|help|faq|contact us|skip to (?:main )?content|.*cookie.*|.*privacy.*|.*©.*|"
    r"ends? in .*|time (?:left|remaining).*|starts? in .*|"
    r"(?:\d+\s*(?:d|h|m|s|days?|hours?|minutes?|seconds?)\s*){1,4}(?:left|remaining)?)$", re.I)
CITY_HINT = re.compile(r",\s*[A-Z]{2}\b|,\s*[A-Z][a-z]+|\b(arena|stadium|center|centre|theater|theatre|hall|"
                       r"park|club|field|garden|amphitheater|ballpark|venue|room|hotel|resort|bowl)\b", re.I)

COLUMNS = ["name", "date", "date_iso", "venue", "kind", "price", "link"]


def to_iso(date_text: str) -> str:
    if not date_text:
        return ""
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", date_text.strip())
    t = re.split(r"\s*[-–]\s*", t)[0]
    t = re.sub(r"\s+", " ", t.replace(",", " ").replace(".", " ")).strip()
    t = re.sub(r"\bSept\b", "Sep", t, flags=re.I)
    today = dt.date.today()
    year = re.search(r"20\d\d", date_text)
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    if not re.search(r"20\d\d", t):
        t = f"{t} {year.group(0) if year else today.year}"
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            d = dt.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
        if not year and d < today - dt.timedelta(days=60):
            d = d.replace(year=d.year + 1)
        return d.isoformat()
    return ""


def _is_name(line: str) -> bool:
    return (4 <= len(line) <= 140 and not BOILER_RE.match(line) and not DATE_RE.fullmatch(line)
            and not PRICE_RE.fullmatch(line) and not (KIND_RE.search(line) and len(line) <= 30)
            and sum(c.isalpha() for c in line) >= 4)


def parse_pasted(text: str, program: str) -> list[dict]:
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    events: dict[int, dict] = {}
    for i, line in enumerate(lines):
        if len(line) > 160:
            continue
        has_date = bool(DATE_RE.search(line))
        has_price = bool(PRICE_RE.search(line)) and program == "delta"
        if not (has_date or has_price):
            continue
        name_idx = None
        if _is_name(line) and not has_date and not has_price:
            name_idx = i
        for j in range(i - 1, max(-1, i - 5), -1):
            if _is_name(lines[j]) and not DATE_RE.search(lines[j]) and not PRICE_RE.search(lines[j]):
                name_idx = j
                break
        if name_idx is None:
            continue
        ev = events.setdefault(name_idx, {c: "" for c in COLUMNS} | {"name": lines[name_idx]})
        window = lines[name_idx: i + 3]
        if has_price and not ev["price"]:
            ev["price"] = PRICE_RE.search(line).group(0)
        if has_date and not ev["date"]:
            ev["date"] = DATE_RE.search(line).group(0)
            ev["date_iso"] = to_iso(ev["date"])
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt and not ev["venue"] and _is_name(nxt) and CITY_HINT.search(nxt) and not DATE_RE.search(nxt):
                ev["venue"] = nxt
        if not ev["price"]:
            for w in window:
                p = PRICE_RE.search(w)
                if p:
                    ev["price"] = p.group(0)
                    break
        if not ev["kind"]:
            for w in window:
                k = KIND_RE.search(w)
                if k:
                    ev["kind"] = k.group(0).title()
                    break
    out, seen = [], set()
    for ev in events.values():
        key = (ev["name"].lower(), ev["date"])
        if key not in seen:
            seen.add(key)
            out.append(ev)
    return out
