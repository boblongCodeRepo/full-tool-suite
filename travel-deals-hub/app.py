"""Travel Deals Hub - weekly flight, hotel and loyalty-experience deals (refreshes every Sunday)."""
from __future__ import annotations

import datetime as dt
import html
import json
import pathlib
import re

import altair as alt
import pandas as pd
import streamlit as st

import ai
import alerts
import fetch
import importers
import notify
import userdata

REFRESH_HOUR = 7  # Sunday, local time
HOME = fetch.SETTINGS.get("home_label", "Home")
DELTA_URL = "https://www.delta.com/us/en/skymiles/how-to-use-miles/skymiles-experiences"
AMEX_URL = "https://www.americanexpress.com/en-us/benefits/membership/experiences/"
UNITED_URL = "https://exclusives.mileageplus.com/us/en"
DC_AREA_RE = re.compile(r"\b(District of Columbia|Washington|D\.C\.|Virginia|Maryland|Baltimore|Arlington|"
                        r"Alexandria|Bethesda|Annapolis|Capital One Arena|Nationals Park|Northwest Stadium|"
                        r"FedEx Field|Kennedy Center|Wolf Trap)\b", re.I)
PROGRAM_NAMES = {"amex": "Amex Experiences", "delta": "Delta SkyMiles Experiences"}
KIOSK = st.query_params.get("view") == "kiosk"

st.set_page_config(page_title="Travel Deals Hub", page_icon="✈️", layout="wide",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
:root { --bg:#0b1220; --panel:#111a2e; --panel2:#16213a; --line:#24304a; --ink:#e5e7eb; --ink2:#a9b1c2;
        --muted:#7c869b; --accent:#f59e0b; --blue:#3987e5; --good:#34d399; --warn:#fb7185; }
.block-container { padding-top: 1.4rem; max-width: 1400px; }
.hdr { display:flex; flex-wrap:wrap; align-items:baseline; gap:.4rem 1.2rem; margin-bottom:.4rem; }
.hdr h1 { font-size:2rem; margin:0; padding:0; }
.hdr .sub { color:var(--ink2); font-size:.95rem; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin:.6rem 0 1.2rem; }
.tile { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
.tile .v { font-size:2rem; font-weight:700; color:var(--ink); line-height:1.1; }
.tile .l { color:var(--ink2); font-size:.85rem; margin-top:4px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:12px; margin-bottom:1rem; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px;
        display:flex; flex-direction:column; gap:8px; transition:border-color .15s, background .15s; }
.card:hover { border-color:var(--accent); background:var(--panel2); }
.card .img { width:100%; aspect-ratio:16/8; object-fit:cover; border-radius:8px; background:var(--panel2); }
.card a.t { font-weight:600; font-size:1rem; line-height:1.35; color:var(--ink) !important; text-decoration:none; }
.card a.t:hover { text-decoration:underline; }
.card .m { color:var(--muted); font-size:.8rem; }
.card .m a { color:var(--ink2) !important; }
.card .s { color:var(--ink2); font-size:.85rem; line-height:1.4; }
.row { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.price { background:rgba(245,158,11,.15); color:var(--accent); border:1px solid rgba(245,158,11,.4);
         border-radius:999px; padding:2px 10px; font-weight:700; font-size:.9rem; }
.chip { background:var(--panel2); color:var(--ink2); border:1px solid var(--line); border-radius:999px;
        padding:2px 9px; font-size:.75rem; }
.chip.home { color:var(--good); border-color:rgba(52,211,153,.45); }
.chip.soon { color:var(--warn); border-color:rgba(251,113,133,.5); }
.chip.up { color:var(--warn); } .chip.down { color:var(--good); }
.sect { font-size:1.15rem; font-weight:700; margin:1rem 0 .5rem; }
.note { color:var(--muted); font-size:.85rem; }
.empty { color:var(--muted); padding:.6rem 0 1rem; }
.picks { background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--accent);
         border-radius:12px; padding:12px 18px; }
.month { font-weight:700; color:var(--accent); margin:1rem 0 .4rem; }
.ev { display:grid; grid-template-columns:110px 1fr auto; gap:12px; padding:8px 0; border-bottom:1px solid var(--line); }
.ev .d { color:var(--ink2); font-weight:600; }
.ev a { color:var(--ink) !important; font-weight:600; text-decoration:none; }
.kiosk h1 { font-size:3.2rem !important; }
.kiosk .big { font-size:1.6rem; line-height:1.5; }
.kiosk .kcard { background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:18px 22px; margin:10px 0; }
.kiosk .kprice { color:var(--accent); font-weight:800; font-size:2rem; margin-right:14px; }
</style>
""", unsafe_allow_html=True)


# ---------- data ----------
def snapshot_files() -> list[pathlib.Path]:
    return sorted(fetch.SNAP_DIR.glob("*.json")) if fetch.SNAP_DIR.exists() else []


@st.cache_data(show_spinner=False)
def load_snapshot(path: str, mtime: float) -> dict:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def load(path: pathlib.Path) -> dict:
    return load_snapshot(str(path), path.stat().st_mtime)


def last_due_time(now: dt.datetime) -> dt.datetime:
    due = dt.datetime.combine(fetch.week_sunday(now.date()), dt.time(REFRESH_HOUR))
    return due if now >= due else due - dt.timedelta(days=7)


def run_collect():
    with st.spinner("Collecting this week's deals from all sources... (about a minute)"):
        fetch.collect()
    st.cache_data.clear()


files = snapshot_files()
latest = load(files[-1]) if files else None
if latest is None or dt.datetime.fromisoformat(latest["fetched_at"]) < last_due_time(dt.datetime.now()):
    run_collect()
    files = snapshot_files()
    latest = load(files[-1])
previous = load(files[-2]) if len(files) > 1 else None
history = [load(f) for f in files[-12:]]
user = userdata.load()

items = latest["items"]
live = [i for i in items if not i.get("expired")]
united = latest["united_exclusives"]
today = dt.date.today()


def tagged(tag: str, pool=None) -> list[dict]:
    return [i for i in (pool if pool is not None else live) if tag in i["tags"]]


flights, hotels = tagged("flight"), tagged("hotel")
home_flights = [i for i in flights if i.get("home")]
ending_soon = sorted([i for i in live if i.get("deadline") and set(i["tags"]) - {"news"} and
                      today <= dt.date.fromisoformat(i["deadline"]) <= today + dt.timedelta(days=7)],
                     key=lambda i: i["deadline"])
fare_hits = alerts.fare_matches(live, user["fare_alerts"])
watch_hits = alerts.watch_matches(live, user["watchlist"])


# ---------- rendering helpers ----------
def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def safe_url(u: str) -> str:
    return esc(u) if str(u).startswith(("https://", "http://")) else "#"


def nice_date(iso: str) -> str:
    d = dt.datetime.fromisoformat(iso).astimezone()
    return d.strftime("%a %b ") + str(d.day)


def deadline_chip(iso: str | None) -> str:
    if not iso:
        return ""
    d = dt.date.fromisoformat(iso)
    days = (d - today).days
    if days < 0:
        return '<span class="chip">ended</span>'
    label = "ends today" if days == 0 else "ends tomorrow" if days == 1 else f"ends {d.strftime('%b')} {d.day}"
    return f'<span class="chip{" soon" if days <= 3 else ""}">{label}</span>'


def item_card(i: dict, show_summary: bool = True, note: str = "") -> str:
    chips = []
    if i.get("price_usd"):
        chips.append(f'<span class="price">${i["price_usd"]:,}</span>')
    elif i.get("miles"):
        chips.append(f'<span class="price">{i["miles"]:,} pts</span>')
    if i.get("transfer") and i["transfer"].get("pct"):
        chips.append(f'<span class="price">{i["transfer"]["pct"]}% bonus</span>')
    chips.append(deadline_chip(i.get("deadline")))
    if i.get("home"):
        chips.append(f'<span class="chip home">from {esc(HOME)}</span>')
    if note:
        chips.append(f'<span class="chip home">{esc(note)}</span>')
    for t in i["tags"]:
        if t not in ("news",):
            chips.append(f'<span class="chip">{esc(t)}</span>')
    summ = (f'<div class="s">{esc(i["summary"][:180])}{"…" if len(i["summary"]) > 180 else ""}</div>'
            if show_summary and i.get("summary") else "")
    also = ""
    if i.get("also"):
        links = ", ".join(f'<a href="{safe_url(a["link"])}" target="_blank" rel="noopener noreferrer">'
                          f'{esc(a["source"])}</a>' for a in i["also"])
        also = f' · also on {links}'
    return (f'<div class="card"><a class="t" href="{safe_url(i["link"])}" target="_blank" '
            f'rel="noopener noreferrer">{esc(i["title"])}</a>{summ}<div class="row">{"".join(chips)}</div>'
            f'<div class="m">{esc(i["source"])} · {esc(nice_date(i["published"]))}{also}</div></div>')


def bid_history() -> dict[str, list[tuple[str, int]]]:
    out: dict[str, list[tuple[str, int]]] = {}
    for snap in history:
        for u in snap.get("united_exclusives", []):
            if u.get("miles") is not None:
                out.setdefault(u["link"], []).append((snap["week_of"], u["miles"]))
    return out


BIDS = bid_history()


def united_card(u: dict) -> str:
    img = f'<img class="img" src="{safe_url(u["image"])}" alt="" loading="lazy">' if u.get("image") else ""
    chips = [f'<span class="chip">{esc(b)}</span>' for b in u.get("badges", [])]
    if DC_AREA_RE.search(u.get("location", "")):
        chips.append(f'<span class="chip home">near {esc(HOME)}</span>')
    hist = BIDS.get(u["link"], [])
    if len(hist) >= 2 and hist[-1][1] != hist[-2][1]:
        diff = hist[-1][1] - hist[-2][1]
        chips.append(f'<span class="chip {"up" if diff > 0 else "down"}">{"▲" if diff > 0 else "▼"} '
                     f'{abs(diff):,} since last week</span>')
    price = f'<span class="price">{esc(u["miles_text"])}</span>' if u.get("miles_text") else ""
    bids = f' · {esc(u["bids"])}' if u.get("bids") else ""
    return (f'<div class="card">{img}<a class="t" href="{safe_url(u["link"])}" target="_blank" '
            f'rel="noopener noreferrer">{esc(u["title"])}</a>'
            f'<div class="m">{esc(u["date"])} · {esc(u["location"])}</div>'
            f'<div class="row"><span class="m">{esc(u.get("label"))}</span>{price}<span class="m">{bids}</span></div>'
            f'<div class="row">{"".join(chips)}</div></div>')


def event_card(e: dict, program: str) -> str:
    when = e.get("date") or (e.get("date_iso") or "")
    title = (f'<a class="t" href="{safe_url(e["link"])}" target="_blank" rel="noopener noreferrer">{esc(e["name"])}</a>'
             if str(e.get("link", "")).startswith("http") else f'<div class="t" style="font-weight:600">{esc(e["name"])}</div>')
    chips = []
    if e.get("price"):
        chips.append(f'<span class="price">{esc(e["price"])}</span>')
    if e.get("kind"):
        chips.append(f'<span class="chip">{esc(e["kind"])}</span>')
    if DC_AREA_RE.search(e.get("venue", "")):
        chips.append(f'<span class="chip home">near {esc(HOME)}</span>')
    return (f'<div class="card">{title}<div class="m">{esc(when)}{" · " if e.get("venue") else ""}'
            f'{esc(e.get("venue"))} · {esc(program)}</div><div class="row">{"".join(chips)}</div></div>')


def grid(cards: list[str], empty: str = "Nothing this week.") -> None:
    if cards:
        st.markdown(f'<div class="grid">{"".join(cards)}</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="empty">{esc(empty)}</div>', unsafe_allow_html=True)


def section(title: str) -> None:
    st.markdown(f'<div class="sect">{esc(title)}</div>', unsafe_allow_html=True)


def note(text: str) -> None:
    st.markdown(f'<div class="note">{esc(text)}</div>', unsafe_allow_html=True)


def search_filter(rows: list[dict], q: str, fields=("title", "summary", "source")) -> list[dict]:
    q = q.strip().lower()
    if not q:
        return rows
    return [r for r in rows if any(q in str(r.get(f, "")).lower() for f in fields)]


def bar_chart(counts: dict, label: str, top: int = 15):
    if not counts:
        st.markdown('<div class="empty">Not enough mentions yet.</div>', unsafe_allow_html=True)
        return
    df = pd.DataFrame(list(counts.items())[:top], columns=[label, "Mentions"])
    chart = (alt.Chart(df)
             .mark_bar(color="#3987e5", cornerRadiusEnd=4, size=16)
             .encode(x=alt.X("Mentions:Q", title="Mentions in deal headlines this week",
                             axis=alt.Axis(tickMinStep=1, format="d", gridColor="#24304a", labelColor="#a9b1c2",
                                           titleColor="#a9b1c2", domain=False)),
                     y=alt.Y(f"{label}:N", sort="-x", title=None,
                             axis=alt.Axis(labelColor="#e5e7eb", labelFontSize=13, ticks=False, domain=False)),
                     tooltip=[alt.Tooltip(f"{label}:N"), alt.Tooltip("Mentions:Q")])
             .properties(height=28 * len(df) + 30, background="transparent")
             .configure_view(stroke=None))
    st.altair_chart(chart, width="stretch")


def all_events() -> list[dict]:
    """United listings plus imported Amex/Delta events, normalised for the calendar."""
    out = []
    for u in united:
        out.append({"name": u["title"], "date": u["date"], "date_iso": importers.to_iso(u["date"]),
                    "venue": u["location"], "kind": u.get("label", ""), "price": u.get("miles_text", ""),
                    "link": u["link"], "program": "United"})
    for key, label in (("amex", "Amex"), ("delta", "Delta")):
        for e in user["imported"].get(key, []):
            out.append({**{c: e.get(c, "") for c in importers.COLUMNS}, "program": label})
    return out


def picks_block() -> None:
    if latest.get("ai_summary"):
        lines = []
        for line in latest["ai_summary"].splitlines():
            line = esc(line)
            line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
            line = re.sub(r"\((https?://[^)\s]+)\)", r'(<a href="\1" target="_blank" rel="noopener noreferrer">open</a>)', line)
            lines.append(line)
        st.markdown('<div class="picks">' + "<br>".join(lines) + "</div>", unsafe_allow_html=True)
    elif latest.get("ai_summary_error"):
        note("Top picks could not be written this week: " + latest["ai_summary_error"])
    else:
        note("Top picks appear here once Claude is connected (see Settings).")


# ---------- kiosk view ----------
if KIOSK:
    st.markdown("<style>header, [data-testid='stToolbar'] {display:none !important}"
                ".block-container{padding-top:1rem}</style>", unsafe_allow_html=True)

    def kiosk_panels() -> list[tuple[str, list[str]]]:
        def line(i):
            price = f'<span class="kprice">${i["price_usd"]:,}</span>' if i.get("price_usd") else ""
            return f'<div class="kcard big">{price}{esc(i["title"])}</div>'
        panels = []
        cheap = sorted([i for i in home_flights if i.get("price_usd")], key=lambda i: i["price_usd"])[:5]
        if not cheap:
            cheap = sorted([i for i in flights if i.get("price_usd")], key=lambda i: i["price_usd"])[:5]
        panels.append((f"Cheapest flights{' from ' + HOME if any(i.get('home') for i in cheap) else ''}",
                       [line(i) for i in cheap]))
        if fare_hits:
            panels.append(("Fare alerts", [line(i) for _, i in fare_hits[:5]]))
        if watch_hits:
            panels.append(("Your watchlist", [line(i) for _, i in watch_hits[:5]]))
        if ending_soon:
            panels.append(("Ending this week", [line(i) for i in ending_soon[:5]]))
        if united:
            panels.append(("United experiences", [
                f'<div class="kcard big"><span class="kprice">{esc(u["miles_text"])}</span>{esc(u["title"])}'
                f'<div class="note">{esc(u["date"])} · {esc(u["location"])}</div></div>' for u in united[:5]]))
        return [p for p in panels if p[1]]

    @st.fragment(run_every=dt.timedelta(seconds=15))
    def rotate():
        panels = kiosk_panels()
        n = st.session_state.get("kiosk_i", 0) % max(1, len(panels))
        st.session_state["kiosk_i"] = n + 1
        title, rows = panels[n] if panels else ("Travel Deals Hub", ["<div class='big'>No deals yet.</div>"])
        st.markdown(f'<div class="kiosk"><h1>✈️ {esc(title)}</h1>{"".join(rows)}'
                    f'<div class="note">Week of {esc(latest["week_of"])} · {n + 1} of {len(panels)}</div></div>',
                    unsafe_allow_html=True)

    rotate()
    st.stop()


# ---------- header ----------
fetched = dt.datetime.fromisoformat(latest["fetched_at"])
week_of = dt.date.fromisoformat(latest["week_of"])
next_due = last_due_time(dt.datetime.now()) + dt.timedelta(days=7)
hc1, hc2 = st.columns([5, 1])
with hc1:
    st.markdown(
        f'<div class="hdr"><h1>✈️ Travel Deals Hub</h1>'
        f'<span class="sub">Week of {week_of.strftime("%B")} {week_of.day} · collected '
        f'{fetched.strftime("%a %I:%M %p").replace(" 0", " ")} · next update Sunday '
        f'{next_due.strftime("%b")} {next_due.day}</span></div>', unsafe_allow_html=True)
with hc2:
    if st.button("Refresh now", width="stretch"):
        run_collect()
        st.rerun()

tabs = st.tabs(["This week", "Flights", "Hotels", "Experiences", "United", "Delta", "Amex", "Trends",
                "Settings", "Sources"])

# ---------- This week ----------
with tabs[0]:
    st.markdown(
        '<div class="tiles">'
        f'<div class="tile"><div class="v">{len(flights)}</div><div class="l">Flight deals</div></div>'
        f'<div class="tile"><div class="v">{len(home_flights)}</div><div class="l">Flights from {esc(HOME)}</div></div>'
        f'<div class="tile"><div class="v">{len(fare_hits)}</div><div class="l">Fare alert matches</div></div>'
        f'<div class="tile"><div class="v">{len(watch_hits)}</div><div class="l">On your watchlist</div></div>'
        f'<div class="tile"><div class="v">{len(ending_soon)}</div><div class="l">Ending this week</div></div>'
        f'<div class="tile"><div class="v">{len(united)}</div><div class="l">United Exclusives live</div></div>'
        '</div>', unsafe_allow_html=True)

    section("Claude's top picks this week")
    picks_block()

    if fare_hits:
        section("Fare alerts")
        grid([item_card(i, False, note=r["name"]) for r, i in fare_hits[:6]])

    section("For you (watchlist)")
    grid([item_card(i, False, note=p) for p, i in watch_hits[:6]],
         "Nothing on your watchlist this week. Add places in Settings.")

    section("Ending soon")
    grid([item_card(i, False) for i in ending_soon[:6]], "No deadlines found in this week's deals.")

    section(f"Cheapest flights from {HOME}")
    cheap = sorted([i for i in home_flights if i.get("price_usd")], key=lambda i: i["price_usd"])[:6]
    grid([item_card(i, False) for i in cheap], f"No priced fares from {HOME} this week.")
    if len(cheap) < 4:
        section("Cheapest flights from anywhere in the US")
        anywhere = sorted([i for i in flights if i.get("price_usd") and not i.get("home")],
                          key=lambda i: i["price_usd"])[:6]
        grid([item_card(i, False) for i in anywhere])

    section("Newest hotel deals")
    grid([item_card(i, False) for i in hotels[:6]])

    section("United MileagePlus Exclusives")
    near = [u for u in united if DC_AREA_RE.search(u.get("location", ""))]
    grid([united_card(u) for u in (near + [u for u in united if u not in near])[:4]],
         "United's listings could not be loaded this week.")

    section("Trending destinations")
    top = list(latest["destinations"].items())[:12]
    st.markdown('<div class="row">' + "".join(
        f'<span class="chip" style="font-size:.9rem;padding:4px 12px">{esc(k)} · {v}</span>' for k, v in top)
        + "</div>", unsafe_allow_html=True)

# ---------- Flights ----------
with tabs[1]:
    c1, c2, c3, c4 = st.columns([1.1, 1.3, 2, 1.1])
    only_home = c1.toggle(f"Only from {HOME}", value=False)
    region = c2.selectbox("Region", ["All regions"] + list(fetch.REGIONS), label_visibility="collapsed")
    q = c3.text_input("Search flights", placeholder="e.g. Tokyo, United, business class", label_visibility="collapsed")
    sort = c4.selectbox("Sort", ["Newest", "Cheapest", "Ending soonest"], label_visibility="collapsed")
    rows = home_flights if only_home else flights
    if region != "All regions":
        rows = [i for i in rows if region in i.get("regions", [])]
    rows = search_filter(rows, q, ("title", "summary", "source", "dest", "origin"))
    if sort == "Cheapest":
        rows = sorted(rows, key=lambda i: (i.get("price_usd") is None, i.get("price_usd") or 0))
    elif sort == "Ending soonest":
        rows = sorted(rows, key=lambda i: (i.get("deadline") is None, i.get("deadline") or ""))
    note(f"{len(rows)} deals · fares and award sales from the last {fetch.SETTINGS['keep_days']} days")
    grid([item_card(i) for i in rows])

# ---------- Hotels ----------
with tabs[2]:
    c1, c2 = st.columns([3, 1])
    q = c1.text_input("Search hotels", placeholder="e.g. Hyatt, Hawaii, free night", label_visibility="collapsed")
    sort = c2.selectbox("Sort hotels", ["Newest", "Ending soonest"], label_visibility="collapsed")
    rows = search_filter(hotels, q)
    if sort == "Ending soonest":
        rows = sorted(rows, key=lambda i: (i.get("deadline") is None, i.get("deadline") or ""))
    note(f"{len(rows)} hotel deals and promos")
    grid([item_card(i) for i in rows])

# ---------- Experiences calendar ----------
with tabs[3]:
    evs = all_events()
    c1, c2 = st.columns([3, 1])
    progs = c1.multiselect("Programs", ["United", "Delta", "Amex"], default=["United", "Delta", "Amex"])
    near_only = c2.toggle(f"Only near {HOME}", value=False)
    evs = [e for e in evs if e["program"] in progs]
    if near_only:
        evs = [e for e in evs if DC_AREA_RE.search(f"{e.get('venue', '')} {e.get('name', '')}")]
    dated = sorted([e for e in evs if e.get("date_iso")], key=lambda e: e["date_iso"])
    undated = [e for e in evs if not e.get("date_iso")]
    if not user["imported"]["amex"] and not user["imported"]["delta"]:
        note("Amex and Delta events appear here after you import them on their tabs.")
    month = None
    out = []
    for e in dated:
        d = dt.date.fromisoformat(e["date_iso"])
        if d < today - dt.timedelta(days=1):
            continue
        m = d.strftime("%B %Y")
        if m != month:
            out.append(f'<div class="month">{esc(m)}</div>')
            month = m
        name = (f'<a href="{safe_url(e["link"])}" target="_blank" rel="noopener noreferrer">{esc(e["name"])}</a>'
                if str(e.get("link", "")).startswith("http") else esc(e["name"]))
        near_chip = f' <span class="chip home">near {esc(HOME)}</span>' if DC_AREA_RE.search(e.get("venue", "")) else ""
        out.append(f'<div class="ev"><div class="d">{d.strftime("%a %b")} {d.day}</div>'
                   f'<div>{name}<div class="note">{esc(e.get("venue"))}{near_chip}</div></div>'
                   f'<div class="row"><span class="chip">{esc(e["program"])}</span>'
                   f'{"<span class=price>" + esc(e["price"]) + "</span>" if e.get("price") else ""}</div></div>')
    st.markdown("".join(out) or '<div class="empty">No upcoming dated experiences.</div>', unsafe_allow_html=True)
    if undated:
        section("No date listed")
        grid([event_card(e, e["program"]) for e in undated])

# ---------- United ----------
with tabs[4]:
    st.link_button("Open MileagePlus Exclusives", UNITED_URL)
    c1, c2 = st.columns([3, 1])
    kinds = sorted({u["label"] for u in united if u.get("label")})
    pick = c1.segmented_control("Listing type", ["All"] + kinds, default="All") if kinds else "All"
    u_near = c2.toggle(f"Only near {HOME}", value=False, key="united_near")
    rows = united if pick in (None, "All") else [u for u in united if u["label"] == pick]
    if u_near:
        rows = [u for u in rows if DC_AREA_RE.search(u.get("location", ""))]
    section(f"Live experiences ({len(rows)})")
    grid([united_card(u) for u in rows], "No listings match.")
    tracked = {link: h for link, h in BIDS.items() if len(h) >= 2}
    section("Bid history")
    if tracked:
        names = {u["link"]: u["title"] for snap in history for u in snap.get("united_exclusives", [])}
        df = pd.DataFrame([{"Experience": names.get(l, l), **{w: f"{m:,}" for w, m in h}} for l, h in tracked.items()])
        st.dataframe(df, hide_index=True, width="stretch")
    else:
        note("Week-by-week bids appear after the second Sunday update.")
    section("United deals & news")
    grid([item_card(i) for i in tagged("united")])


# ---------- Delta / Amex import panels ----------
def import_panel(key: str) -> None:
    program = PROGRAM_NAMES[key]
    saved = user["imported"].get(key, [])
    when = user["imported_at"].get(key, "")
    section(f"{program} ({len(saved)} saved{', imported ' + when if when else ''})")
    grid([event_card(e, program) for e in saved],
         f"No {program} saved yet. Use 'Import experiences' below.")
    with st.expander("Import experiences", expanded=not saved):
        st.markdown(
            f"1. Open the {program} page (button above) in your browser{' and sign in' if key == 'delta' else ''}.\n"
            "2. Scroll to the bottom so every event loads, then press **Ctrl+A** and **Ctrl+C**.\n"
            "3. Paste below and press **Read events**. Check the table, fix anything, then **Save**.")
        has_ai = ai.has_credentials()
        text = st.text_area("Paste the page text here", height=160, key=f"paste_{key}")
        shot = st.file_uploader("…or a screenshot (needs Claude)", type=["png", "jpg", "jpeg", "webp"],
                                key=f"shot_{key}", disabled=not has_ai)
        use_ai = st.checkbox("Use Claude to read it (more accurate, costs a few cents)", value=has_ai and bool(shot),
                             disabled=not has_ai, key=f"ai_{key}")
        if not has_ai:
            note("Claude reading is off: no Anthropic credentials found. See Settings.")
        if st.button("Read events", key=f"read_{key}", type="primary"):
            try:
                if use_ai or shot:
                    img = (shot.getvalue(), shot.type) if shot else None
                    with st.spinner("Claude is reading the events..."):
                        rows = ai.extract_events(program, text=text or None, image=img)
                else:
                    rows = importers.parse_pasted(text, key)
                st.session_state[f"rows_{key}"] = rows
            except Exception as e:
                st.error(f"Could not read the events: {e}")
        rows = st.session_state.get(f"rows_{key}")
        if rows is not None:
            note(f"Found {len(rows)} events. Edit, add or delete rows, then save.")
            df = pd.DataFrame(rows, columns=importers.COLUMNS)
            edited = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch", key=f"ed_{key}",
                                    column_config={"link": st.column_config.LinkColumn("link"),
                                                   "date_iso": st.column_config.TextColumn("date (YYYY-MM-DD)")})
            c1, c2 = st.columns(2)
            if c1.button("Save (replaces saved list)", key=f"save_{key}"):
                clean = [{c: str(r.get(c) or "") for c in importers.COLUMNS}
                         for r in edited.fillna("").to_dict("records") if str(r.get("name") or "").strip()]
                for r in clean:
                    if not r["date_iso"] and r["date"]:
                        r["date_iso"] = importers.to_iso(r["date"])

                def put(d):
                    d["imported"][key] = clean
                    d["imported_at"][key] = dt.datetime.now().strftime("%b %d %I:%M %p")
                userdata.update(put)
                st.session_state.pop(f"rows_{key}", None)
                st.rerun()
            if c2.button("Discard", key=f"discard_{key}"):
                st.session_state.pop(f"rows_{key}", None)
                st.rerun()
    if saved and st.button(f"Clear saved {program}", key=f"clear_{key}"):
        userdata.update(lambda d: d["imported"].__setitem__(key, []))
        st.rerun()


with tabs[5]:
    st.link_button("Open SkyMiles Experiences", DELTA_URL)
    import_panel("delta")
    d = tagged("delta")
    exp = [i for i in d if "experience" in i["tags"]]
    if exp:
        section("Delta experiences in the news")
        grid([item_card(i) for i in exp])
    section("Delta deals & news")
    grid([item_card(i) for i in d if i not in exp])

with tabs[6]:
    st.link_button("Open Amex Experiences", AMEX_URL)
    import_panel("amex")
    section("Transfer bonuses")
    tb = [i for i in live if i.get("transfer")]
    tb.sort(key=lambda i: ("Amex MR" not in i["transfer"]["from"], -(i["transfer"].get("pct") or 0)))
    if tb:
        df = pd.DataFrame([{"From": ", ".join(i["transfer"]["from"]) or "?",
                            "To": ", ".join(i["transfer"]["to"]) or "see post",
                            "Bonus": f'{i["transfer"]["pct"]}%' if i["transfer"].get("pct") else "",
                            "Ends": i.get("deadline") or "", "Source": i["source"], "Link": i["link"]} for i in tb])
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"Link": st.column_config.LinkColumn("Link", display_text="open")})
    else:
        note("No transfer bonuses mentioned this week.")
    a = tagged("amex")
    exp = [i for i in a if "experience" in i["tags"]]
    if exp:
        section("Amex experiences in the news")
        grid([item_card(i) for i in exp])
    section("Amex offers & news")
    grid([item_card(i) for i in a if i not in exp and not i.get("transfer")])

# ---------- Trends ----------
with tabs[7]:
    c1, c2 = st.columns(2)
    with c1:
        section("Most-mentioned destinations")
        bar_chart(latest["destinations"], "Destination")
    with c2:
        section("Most-mentioned airlines, cards & hotel brands")
        bar_chart(latest["programs"], "Brand")

    section("Change since last week")
    if previous:
        prev_d = previous["destinations"]
        names = list(dict.fromkeys(list(latest["destinations"])[:20] + list(prev_d)[:20]))
        df = pd.DataFrame({
            "Destination": names,
            "This week": [latest["destinations"].get(n, 0) for n in names],
            "Last week": [prev_d.get(n, 0) for n in names],
        })
        df["Change"] = df["This week"] - df["Last week"]
        df["Trend"] = df["Change"].map(lambda c: f"▲ up {c}" if c > 0 else (f"▼ down {-c}" if c < 0 else "– same"))
        st.dataframe(df.sort_values(["Change", "This week"], ascending=False), hide_index=True, width="stretch")
    else:
        note("Week-over-week changes appear after the second Sunday update.")

    section(f"Lowest fare from {HOME} by destination, week by week")
    rows = []
    for snap in history:
        for i in snap["items"]:
            if i.get("home") and i.get("dest") and i.get("price_usd"):
                rows.append({"Week of": snap["week_of"], "Destination": i["dest"], "Price": i["price_usd"]})
    if rows:
        fares = (pd.DataFrame(rows).groupby(["Destination", "Week of"])["Price"].min().unstack("Week of")
                 .sort_index(axis=1))
        fares = fares.map(lambda v: "" if pd.isna(v) else f"${int(v):,}")
        st.dataframe(fares, width="stretch")
    else:
        note(f"No priced fares from {HOME} yet.")

# ---------- Settings ----------
with tabs[8]:
    section("Destination watchlist")
    options = sorted(set(list(fetch.REGIONS) + fetch.DESTINATIONS + user["watchlist"]))
    wl = st.multiselect("Places and regions you care about", options, default=user["watchlist"])
    extra = st.text_input("Add places not in the list (comma-separated)", placeholder="e.g. Kyoto, Azores")
    if st.button("Save watchlist"):
        new = wl + [x.strip() for x in extra.split(",") if x.strip() and x.strip() not in wl]
        userdata.update(lambda d: d.__setitem__("watchlist", new))
        st.rerun()

    section("Fare alerts")
    note("'Where' can be a region (" + ", ".join(fetch.REGIONS) + "), a city or country, or 'Anywhere'. "
         "New matches pop up as a Windows notification after each update.")
    fa = pd.DataFrame(user["fare_alerts"], columns=["name", "where", "max_price", "from_home"])
    fa_edit = st.data_editor(fa, num_rows="dynamic", hide_index=True, width="stretch", key="fare_alerts",
                             column_config={
                                 "name": st.column_config.TextColumn("Alert name", required=True),
                                 "where": st.column_config.TextColumn("Where"),
                                 "max_price": st.column_config.NumberColumn("Max price ($)", min_value=0, step=25),
                                 "from_home": st.column_config.CheckboxColumn(f"Only from {HOME}"),
                             })
    c1, c2, c3 = st.columns(3)
    toast_on = c1.toggle("Windows pop-up for new matches", value=user["alert_toast"])
    if c2.button("Save fare alerts"):
        rules = []
        for r in fa_edit.to_dict("records"):
            if str(r.get("name") or "").strip():
                mp = r.get("max_price")
                rules.append({"name": str(r["name"]).strip(), "where": str(r.get("where") or "").strip(),
                              "max_price": int(mp) if mp is not None and not pd.isna(mp) else None,
                              "from_home": bool(r.get("from_home"))})

        def put_rules(d):
            d["fare_alerts"] = rules
            d["alert_toast"] = toast_on
        userdata.update(put_rules)
        st.rerun()
    if c3.button("Send a test pop-up"):
        st.toast("Sent - it should appear in the corner of the screen within a minute."
                 if notify.toast("Travel Deals Hub", "Test notification - alerts are working.")
                 else "Windows notification failed.")

    section("Claude (top picks and screenshot reading)")
    if ai.has_credentials():
        st.success(f"Connected. Uses {ai.MODEL} with automatic fallback if a request is declined. "
                   "Weekly top picks cost roughly a few cents.")
    else:
        st.info("Not connected. To turn on top picks and screenshot reading, either run `ant auth login`, or "
                "set a Windows user environment variable named ANTHROPIC_API_KEY with your key from "
                "console.anthropic.com, then restart the app.")
    ai_on = st.toggle("Write Claude's top picks after each update", value=user["ai_summary"])
    c1, c2 = st.columns(2)
    if c1.button("Save Claude setting"):
        userdata.update(lambda d: d.__setitem__("ai_summary", ai_on))
        st.rerun()
    if c2.button("Write top picks now", disabled=not ai.has_credentials()):
        try:
            with st.spinner("Claude is choosing this week's top picks..."):
                latest["ai_summary"] = ai.weekly_summary(latest, user)
                latest.pop("ai_summary_error", None)
                fetch.save_snapshot(latest)
            st.cache_data.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Could not write top picks: {e}")

    section("Sunday email digest (optional)")
    em = user["email"]
    e1, e2 = st.columns(2)
    em_on = e1.toggle("Email me a digest after each Sunday update", value=em["enabled"])
    em_user = e1.text_input("Send from (email login)", value=em["username"])
    em_to = e1.text_input("Send to", value=em["to"])
    em_host = e2.text_input("SMTP server", value=em["smtp_host"])
    em_port = e2.number_input("SMTP port", value=int(em["smtp_port"]), step=1)
    note(f"For safety the password is not stored here. Create an app password with your email provider and save it "
         f"as a Windows user environment variable named {notify.PASSWORD_ENV}. "
         f"Status: {'found' if __import__('os').environ.get(notify.PASSWORD_ENV) else 'not set'}.")
    c1, c2 = st.columns(2)
    if c1.button("Save email settings"):
        userdata.update(lambda d: d.__setitem__("email", {"enabled": em_on, "username": em_user.strip(),
                                                          "to": em_to.strip(), "smtp_host": em_host.strip(),
                                                          "smtp_port": int(em_port)}))
        st.rerun()
    if c2.button("Send a test digest now"):
        try:
            ok = notify.send_weekly_digest(latest, userdata.load(), fare_hits, force=True)
            st.toast("Digest sent." if ok else "Not sent: turn it on, fill both addresses and set the password variable.")
        except Exception as e:
            st.error(f"Email failed: {e}")

    section("Kiosk screen")
    note("Big-type rotating view for the mini PC screen: http://localhost:8503/?view=kiosk. To alternate it with "
         "the train times on the kiosk, double-click enable-kiosk-rotation.bat in this folder "
         "(disable-kiosk-rotation.bat turns it off). It takes effect the next time the kiosk starts.")
    k1, k2, k3 = st.columns(3)
    ts = k1.number_input("Seconds on train times", value=int(user["kiosk"]["train_seconds"]), min_value=15, step=15)
    vs = k2.number_input("Seconds on travel deals", value=int(user["kiosk"]["travel_seconds"]), min_value=15, step=15)
    if k3.button("Save kiosk timing"):
        userdata.update(lambda d: d.__setitem__("kiosk", {"train_seconds": int(ts), "travel_seconds": int(vs)}))
        st.rerun()

# ---------- Sources ----------
with tabs[9]:
    note("Where the deals come from. Each source is read once per weekly update.")
    st.dataframe(pd.DataFrame([{"Source": s["name"], "Status": "OK" if s["ok"] else "Failed",
                                "Items": s["count"], "Problem": s["error"]} for s in latest["sources"]]),
                 hide_index=True, width="stretch")
    merged = sum(1 for i in items if i.get("also"))
    note(f"{merged} deals were reported by more than one blog and merged. Snapshots kept: {len(files)} weeks.")
