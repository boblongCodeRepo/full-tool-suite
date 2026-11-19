"""DC Transit Hub: one-screen kiosk display for Foggy Bottom-GWU Metro.

Combines the best of the two earlier apps:
  - big, glanceable Metro boards and dark kiosk look (DMV Streamlit)
  - buses, D80 / featured routes, live bus map, alerts, roads (Transit Pulse web app)
  - new: "leave in X min" walking countdown, elevator/escalator outages, Capital Bikeshare
The WMATA key stays on this PC (secrets.toml or WMATA_API_KEY env var) and is never sent to the browser.
"""
import json
import math
import os
import re
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

try:
    import segno  # QR codes (pure Python)
except ImportError:  # app still works without it, just no QR codes
    segno = None
from datetime import datetime, time as dtime, timedelta, timezone
from html import escape as esc
from zoneinfo import ZoneInfo

import folium
import requests
import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="DC Transit Hub", page_icon="🚇", layout="wide",
                   initial_sidebar_state="collapsed")

# ------------------------------------------------------------------ settings
TZ = ZoneInfo("America/New_York")
WMATA = "https://api.wmata.com"
HOME = {"lat": 38.90096, "lon": -77.05028, "label": "Foggy Bottom-GWU Metro"}
REFRESH_SECONDS = 30
FEATURED_ROUTES = ["D80", "D82", "A58", "C85", "D74"]
D80_STOP_IDS = ["1001265", "1001270"]  # Pennsylvania Ave NW + 24th St NW
LINE_COLORS = {"RD": "#bf2026", "BL": "#0076bf", "OR": "#da8707", "SV": "#a1a9b0",
               "YL": "#d2a81e", "GR": "#00a651"}


def foggy_direction(code, name):
    """0 = toward Virginia, 1 = toward downtown / Maryland."""
    if code[:1] in ("K", "J", "N") or (code[:1] == "C" and code[1:].isdigit() and int(code[1:]) > 4):
        return 0
    if code:
        return 1
    return 0 if any(w in name for w in ("vienna", "franconia", "ashburn", "wiehle", "dulles",
                                         "huntington", "springfield", "reston")) else 1


def dupont_direction(code, name):
    """0 = toward Shady Grove, 1 = toward Glenmont."""
    if code.startswith("A") and code[1:].isdigit() and int(code[1:]) > 3:
        return 0
    if code:
        return 1
    return 0 if any(w in name for w in ("shady", "grosvenor", "rockville", "twinbrook",
                                         "bethesda", "medical")) else 1


STATIONS = [
    {"code": "C04", "name": "Foggy Bottom-GWU", "lat": 38.900599, "lon": -77.050273,
     "lines": ["BL", "OR", "SV"], "directions": ["Toward Virginia", "Toward DC & Maryland"],
     "direction_fn": foggy_direction, "walk": 8},  # walk: default minutes from home to the platform
    {"code": "A03", "name": "Dupont Circle", "lat": 38.909499, "lon": -77.04362,
     "lines": ["RD"], "directions": ["Toward Shady Grove", "Toward Glenmont"],
     "direction_fn": dupont_direction, "walk": 11},
]
DESTINATIONS = [
    {"name": "Reagan National (DCA)", "lat": 38.8521, "lon": -77.0377},
    {"name": "Union Station", "lat": 38.8977, "lon": -77.0067},
    {"name": "Dulles (IAD)", "lat": 38.9531, "lon": -77.4565},
]


PAGES = {"transit": "Transit", "events": "DC Events", "flights": "Flight Deals", "business": "Business",
         "news": "World News", "outside": "Outside"}  # also the order they rotate in and appear in the header
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiosk_settings.json")


def load_kiosk_settings():
    """Settings that should survive restarts (the sidebar resets whenever the browser reopens)."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8-sig") as f:  # -sig: also reads files Windows tools wrote
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        if isinstance(data.get("rotate"), str):  # PowerShell can save a one-item list as a plain string
            data["rotate"] = [data["rotate"]]
        return data
    except Exception:
        return {}


def save_kiosk_settings(**changes):
    """Merge changes into the file. Written to a temp file first and swapped in, so nobody reads half a file."""
    tmp = SETTINGS_FILE + f".{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({**load_kiosk_settings(), **changes}, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass  # read-only folder: the choice just lasts for this session


def secret(name):
    try:
        value = st.secrets.get(name, "")
    except Exception:  # no secrets.toml
        value = ""
    return str(value or os.getenv(name, "")).strip()


API_KEY = secret("WMATA_API_KEY")
TOMTOM_KEY = secret("TOMTOM_API_KEY")
TICKETMASTER_KEY = secret("TICKETMASTER_API_KEY")


# ------------------------------------------------------------------ helpers
def miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def bearing(a, b):
    """Compass bearing in degrees (0 = north, 90 = east) from point a to point b, each (lat, lon)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dl) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


CARDINAL = {"NORTH": 0, "NORTHBOUND": 0, "EAST": 90, "EASTBOUND": 90,
            "SOUTH": 180, "SOUTHBOUND": 180, "WEST": 270, "WESTBOUND": 270}


def direction_arrow(text):
    word = str(text or "").strip().split(" ")[0].upper()
    return {"NORTH": "↑", "SOUTH": "↓", "EAST": "→", "WEST": "←"}.get(word, "↻" if "LOOP" in str(text).upper() else "•")


def default_walk_minutes(station):
    if station.get("walk"):
        return station["walk"]
    street_miles = miles(HOME["lat"], HOME["lon"], station["lat"], station["lon"]) * 1.25
    return round(street_miles / 3 * 60) + 2  # 3 mph walk + ~2 min to reach the platform


def wmata(path, **params):
    r = requests.get(WMATA + path, headers={"api_key": API_KEY}, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


@lru_cache(maxsize=1024)
def qr_uri(url):
    """QR code as an inline SVG data URI: dark modules on white so phones scan it easily."""
    if not segno or not url:
        return ""
    return segno.make(url, error="l").svg_data_uri(scale=4, border=2, dark="#0b1220", light="#ffffff")


def qr_img(url):
    uri = qr_uri(url)
    return f'<img class="qr" src="{uri}" alt="Scan to open">' if uri else ""


def show(html, unsafe_allow_html=True):
    """Render raw HTML. Newlines + indentation are removed first, because Markdown treats
    lines indented 4+ spaces as a code block and would print the HTML as text."""
    st.markdown(re.sub(r"\n\s*", " ", html.strip()), unsafe_allow_html=unsafe_allow_html)


def safe(fn, *args, default=None):
    try:
        return fn(*args)
    except Exception:
        return default


# ------------------------------------------------------------------ data (cached)
@st.cache_data(ttl=25, show_spinner=False)
def rail_predictions(codes):
    return wmata(f"/StationPrediction.svc/json/GetPrediction/{codes}").get("Trains", [])


@st.cache_data(ttl=120, show_spinner=False)
def rail_incidents():
    return wmata("/Incidents.svc/json/Incidents").get("Incidents", [])


@st.cache_data(ttl=300, show_spinner=False)
def bus_incidents():
    return wmata("/Incidents.svc/json/BusIncidents").get("BusIncidents", [])


@st.cache_data(ttl=600, show_spinner=False)
def elevator_outages(code):
    return wmata("/Incidents.svc/json/ElevatorIncidents", StationCode=code).get("ElevatorIncidents", [])


@st.cache_data(ttl=600, show_spinner=False)
def nearby_stops(radius):
    return wmata("/Bus.svc/json/jStops", Lat=HOME["lat"], Lon=HOME["lon"], Radius=radius).get("Stops", [])


@st.cache_data(ttl=25, show_spinner=False)
def stop_predictions(stop_id):
    data = wmata("/NextBusService.svc/json/jPredictions", StopID=stop_id)
    return data.get("StopName", ""), data.get("Predictions", [])


@st.cache_data(ttl=25, show_spinner=False)
def bus_positions(radius):
    return wmata("/Bus.svc/json/jBusPositions", Lat=HOME["lat"], Lon=HOME["lon"],
                 Radius=radius).get("BusPositions", [])


@st.cache_data(ttl=25, show_spinner=False)
def train_positions():
    return wmata("/TrainPositions/TrainPositions", contentType="json").get("TrainPositions", [])


@st.cache_data(ttl=86400, show_spinner=False)
def rail_stations():
    return {s["Code"]: {"lat": s["Lat"], "lon": s["Lon"], "name": s["Name"]}
            for s in wmata("/Rail.svc/json/jStations").get("Stations", [])}


@st.cache_data(ttl=86400, show_spinner=False)
def track_routes():
    """Map every track circuit to an approximate lat/lon by interpolating between the stations
    along WMATA's ordered 'standard routes'. Keyed by (line, track number)."""
    stations = rail_stations()
    routes = {}
    for route in wmata("/TrainPositions/StandardRoutes", contentType="json").get("StandardRoutes", []):
        circuits = sorted(route.get("TrackCircuits", []), key=lambda c: c.get("SeqNum", 0))
        anchors = [(i, stations[c["StationCode"]]) for i, c in enumerate(circuits)
                   if c.get("StationCode") in stations]
        if not anchors:
            continue
        points, a = [], 0
        for i in range(len(circuits)):
            while a + 1 < len(anchors) and anchors[a + 1][0] <= i:
                a += 1
            (i0, s0) = anchors[a]
            (i1, s1) = anchors[a + 1] if a + 1 < len(anchors) and i > i0 else anchors[a]
            t = (i - i0) / (i1 - i0) if i1 != i0 else 0
            points.append((s0["lat"] + (s1["lat"] - s0["lat"]) * t, s0["lon"] + (s1["lon"] - s0["lon"]) * t))
        routes[(route["LineCode"], route["TrackNum"])] = {
            "index": {c["CircuitId"]: i for i, c in enumerate(circuits)},
            "stations": {c["StationCode"]: i for i, c in enumerate(circuits) if c.get("StationCode")},
            "points": points,
        }
    return routes


def locate_train(train, routes):
    """Return ((lat, lon), heading_degrees or None) for a train, or (None, None)."""
    line, track, circuit = train.get("LineCode"), train.get("DirectionNum"), train.get("CircuitId")
    candidates = [routes.get((line, track))] + [r for (l, _), r in routes.items() if l == line]
    for r in candidates:
        if not r or circuit not in r["index"]:
            continue
        i, pts = r["index"][circuit], r["points"]
        dest = r["stations"].get(train.get("DestinationStationCode"))
        step = (1 if dest > i else -1) if dest is not None and dest != i else (1 if track == 1 else -1)
        j = min(max(i + step * 5, 0), len(pts) - 1)
        heading = bearing(pts[i], pts[j]) if pts[j] != pts[i] else None
        return pts[i], heading
    return None, None


@st.cache_data(ttl=86400, show_spinner=False)
def route_shape(route_id):
    """Street path of a bus route (both directions) from WMATA route details."""
    data = wmata("/Bus.svc/json/jRouteDetails", RouteID=route_id)
    shapes = []
    for key in ("Direction0", "Direction1"):
        d = data.get(key) or {}
        pts = [(p["Lat"], p["Lon"]) for p in sorted(d.get("Shape") or [], key=lambda p: p.get("SeqNum", 0))]
        if len(pts) > 600:  # thin very long shapes to keep the map light
            pts = pts[::len(pts) // 600 + 1] + [pts[-1]]
        if len(pts) > 1:
            shapes.append({"headsign": d.get("TripHeadsign") or d.get("DirectionText") or "", "points": pts})
    return shapes


@st.cache_data(ttl=86400, show_spinner=False)
def route_connections(route_id):
    """Metro stations a bus route passes within ~200 m of, in route order."""
    stations = rail_stations()
    shapes = route_shape(route_id)
    if not shapes:
        return []
    hits = []
    for lat, lon in shapes[0]["points"]:
        for s in stations.values():
            if abs(lat - s["lat"]) < 0.003 and abs(lon - s["lon"]) < 0.004 and \
                    miles(lat, lon, s["lat"], s["lon"]) < 0.12:
                hits.append(s["name"])
    return list(dict.fromkeys(hits))


ROUTE_PALETTE = ["#f472b6", "#22d3ee", "#facc15", "#a78bfa", "#34d399", "#fb923c",
                 "#60a5fa", "#e879f9", "#a3e635", "#fda4af"]


@st.cache_resource
def bus_memory():
    """Last seen position/heading per bus, shared across refreshes, used to work out travel direction."""
    return {}


def bus_heading(bus):
    memory = bus_memory()
    vid, pos = bus.get("VehicleID"), (bus["Lat"], bus["Lon"])
    seen = memory.get(vid)
    fallback = CARDINAL.get(str(bus.get("DirectionText", "")).upper())
    if not seen:
        memory[vid] = {"pos": pos, "heading": fallback}
        return fallback
    if miles(seen["pos"][0], seen["pos"][1], pos[0], pos[1]) > 0.01:  # moved ~50 ft+: use real movement
        seen["heading"], seen["pos"] = bearing(seen["pos"], pos), pos
    return seen["heading"] if seen["heading"] is not None else fallback


def vehicle_icon(label, color, heading, width):
    arrow = (f'<span style="display:inline-block;transform:rotate({heading:.0f}deg);margin-right:3px">▲</span>'
             if heading is not None else "")
    return folium.DivIcon(icon_size=(width, 24), icon_anchor=(width // 2, 12), html=(
        f'<div style="background:{color};color:#fff;font:800 12px sans-serif;border-radius:7px;padding:3px 4px;'
        f'width:{width}px;box-sizing:border-box;text-align:center;white-space:nowrap;border:1.5px solid #fff;'
        f'box-shadow:0 1px 4px rgba(0,0,0,.6)">{arrow}{esc(label)}</div>'))


# ------------------------------------------------------------------ DC events (page 2)
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
DC_TEAMS = [  # (label, ESPN sport path, team id)
    ("Capitals", "hockey/nhl", "wsh"), ("Wizards", "basketball/nba", "wsh"),
    ("Commanders", "football/nfl", "wsh"), ("Nationals", "baseball/mlb", "wsh"),
    ("Mystics", "basketball/wnba", "wsh"), ("D.C. United", "soccer/usa.1", "193"),
    ("Spirit", "soccer/usa.nwsl", "15362"), ("Georgetown", "basketball/mens-college-basketball", "46"),
]


@st.cache_data(ttl=600, show_spinner=False)
def team_games(sport, team_id):
    """Upcoming and live HOME games for one DC team from ESPN's public schedule feed (no key)."""
    r = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/teams/{team_id}/schedule", timeout=15)
    r.raise_for_status()
    games = []
    for e in r.json().get("events", []):
        comp = e["competitions"][0]
        state = comp["status"]["type"]["state"]  # pre / in / post
        if state == "post":
            continue
        home = next((c for c in comp["competitors"] if c["homeAway"] == "home"), {})
        away = next((c for c in comp["competitors"] if c["homeAway"] == "away"), {})
        if not (home.get("team", {}).get("abbreviation", "") == str(team_id).upper() or home.get("id") == str(team_id)):
            continue  # away game
        score = ""
        if state == "in":
            score = f'{away.get("team", {}).get("abbreviation", "")} {away.get("score", {}).get("displayValue", "")} – ' \
                    f'{home.get("team", {}).get("abbreviation", "")} {home.get("score", {}).get("displayValue", "")}'
        tv = ", ".join(b.get("media", {}).get("shortName", "") for b in comp.get("broadcasts", [])[:2]).strip(", ")
        games.append({"when": e["date"], "name": e.get("shortName") or e["name"], "live": state == "in",
                      "score": score, "venue": comp.get("venue", {}).get("fullName", ""), "tv": tv,
                      "home": home.get("team", {}).get("abbreviation", "") == str(team_id).upper()
                              or home.get("id") == str(team_id)})
    return games


@st.cache_data(ttl=3600, show_spinner=False)
def eventbrite_events(path):
    """Public Eventbrite listing page -> events from its schema.org JSON-LD (no key)."""
    import json
    html_text = requests.get(f"https://www.eventbrite.com/d/dc--washington/{path}/", headers=BROWSER_HEADERS,
                             timeout=20).text
    # Eventbrite's own popularity badges sit just before each event card's link
    signals = {}
    for m in re.finditer(r"EventCardUrgencySignal--([a-z-]+)", html_text):
        link = re.search(r'href="(https://www\.eventbrite\.com/e/[^"?]+)', html_text[m.end():m.end() + 3000])
        if link:
            signals.setdefault(link.group(1), m.group(1))
    found = {}
    rank = 0
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html_text, re.S):
        try:
            stack = [json.loads(block)]
        except ValueError:
            continue
        while stack:
            x = stack.pop(0)  # breadth-first keeps Eventbrite's page order (its relevance ranking)
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                if x.get("@type") == "Event" and x.get("name") and x.get("startDate"):
                    venue = (x.get("location") or {}).get("name", "")
                    url = x.get("url", "").split("?")[0]
                    rank += 1
                    found.setdefault((x["name"].strip().lower(), x["startDate"]), {
                        "date": x["startDate"][:10], "time": "", "title": x["name"].strip(),
                        "venue": venue.split(",")[0], "url": url, "rank": rank,
                        "hot": signals.get(url, "")})
                stack.extend(v for v in x.values() if isinstance(v, (list, dict)))
    return sorted(found.values(), key=lambda e: e["date"])


# ---- embassy events: "Beaujolais Soiree at Embassy of France: Wine & Music" -> "🏠 France · Beaujolais Soiree: Wine & Music"
_COUNTRIES = [c.replace("_", " ") for c in """Afghanistan Albania Algeria Andorra Angola Argentina Armenia Australia Austria
Azerbaijan Bahamas Bahrain Bangladesh Barbados Belarus Belgium Belize Benin Bhutan Bolivia Botswana Brazil Brunei
Bulgaria Burkina_Faso Burundi Cambodia Cameroon Canada Cape_Verde Chad Chile China Colombia Congo Costa_Rica Croatia
Cuba Cyprus Czech_Republic Czechia Denmark Djibouti Dominica Dominican_Republic Ecuador Egypt El_Salvador Estonia
Eswatini Ethiopia Fiji Finland France Gabon Gambia Georgia Germany Ghana Greece Grenada Guatemala Guinea Guyana Haiti
Honduras Hungary Iceland India Indonesia Iran Iraq Ireland Israel Italy Jamaica Japan Jordan Kazakhstan Kenya Korea
Kosovo Kuwait Kyrgyzstan Laos Latvia Lebanon Lesotho Liberia Libya Liechtenstein Lithuania Luxembourg Madagascar
Malawi Malaysia Maldives Mali Malta Mauritania Mauritius Mexico Moldova Monaco Mongolia Montenegro Morocco Mozambique
Myanmar Namibia Nepal Netherlands New_Zealand Nicaragua Niger Nigeria North_Macedonia Norway Oman Pakistan Palau Panama
Papua_New_Guinea Paraguay Peru Philippines Poland Portugal Qatar Romania Russia Rwanda Saudi_Arabia Senegal Serbia
Seychelles Sierra_Leone Singapore Slovakia Slovenia South_Africa South_Sudan Spain Sri_Lanka Sudan Suriname Sweden
Switzerland Syria Taiwan Tajikistan Tanzania Thailand Togo Trinidad_and_Tobago Tunisia Turkey Turkiye Turkmenistan
Uganda Ukraine United_Arab_Emirates United_Kingdom Uruguay Uzbekistan Venezuela Vietnam Yemen Zambia
Zimbabwe""".split()]
_DEMONYMS = {"French": "France", "German": "Germany", "Italian": "Italy", "Spanish": "Spain", "Swedish": "Sweden",
             "Norwegian": "Norway", "Danish": "Denmark", "Finnish": "Finland", "Dutch": "Netherlands",
             "Belgian": "Belgium", "Swiss": "Switzerland", "Austrian": "Austria", "Portuguese": "Portugal",
             "Greek": "Greece", "Polish": "Poland", "Czech": "Czechia", "Hungarian": "Hungary", "Irish": "Ireland",
             "British": "UK", "Icelandic": "Iceland", "Estonian": "Estonia", "Latvian": "Latvia",
             "Lithuanian": "Lithuania", "Ukrainian": "Ukraine", "Romanian": "Romania", "Bulgarian": "Bulgaria",
             "Croatian": "Croatia", "Serbian": "Serbia", "Slovak": "Slovakia", "Slovenian": "Slovenia",
             "Turkish": "Turkey", "Israeli": "Israel", "Japanese": "Japan", "Korean": "Korea", "Chinese": "China",
             "Indian": "India", "Indonesian": "Indonesia", "Thai": "Thailand", "Vietnamese": "Vietnam",
             "Filipino": "Philippines", "Australian": "Australia", "Canadian": "Canada", "Mexican": "Mexico",
             "Brazilian": "Brazil", "Argentine": "Argentina", "Chilean": "Chile", "Colombian": "Colombia",
             "Peruvian": "Peru", "Egyptian": "Egypt", "Moroccan": "Morocco", "Nigerian": "Nigeria",
             "Kenyan": "Kenya", "Ghanaian": "Ghana", "Ethiopian": "Ethiopia", "South African": "South Africa",
             "Saudi": "Saudi Arabia", "Emirati": "UAE", "Qatari": "Qatar", "Pakistani": "Pakistan"}
_SHORT_COUNTRY = {"United Kingdom": "UK", "United Arab Emirates": "UAE", "Czech Republic": "Czechia",
                  "Turkiye": "Turkey", "Trinidad and Tobago": "Trinidad & Tobago"}
_COUNTRY_KEY = {c.lower(): c for c in _COUNTRIES}
_DEMONYM_KEY = {d.lower(): c for d, c in _DEMONYMS.items()}


def _alternation(names):
    return "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in sorted(names, key=len, reverse=True))


_FORMAL = (r"(?:the\s+)?(?:(?:Federal|Islamic|Democratic|People['’]s|Socialist|Arab|Hellenic|Bolivarian|"
           r"Plurinational|Co-operative)\s+)*(?:Republic|Kingdom|Grand\s+Duchy|Sultanate|State|Principality|"
           r"Commonwealth|Federation)\s+of\s+(?:the\s+)?")
# "... at the Embassy of the Republic of X to the USA, La Maison Française"
_EMBASSY_OF = re.compile(
    r"(?:\s*(?:\bat|@|\bin)\s+(?:the\s+)?|\b(?:the\s+)?)Embassy\s+of\s+(?:the\s+)?(?:" + _FORMAL + r")?"
    r"(?P<c>" + _alternation(_COUNTRIES) + r")\b"
    r"(?:\s+(?:in|to)\s+(?:the\s+)?(?:United\s+States(?:\s+of\s+America)?|USA|U\.S\.A?\.?|Washington(?:,?\s*D\.?C\.?)?))?"
    r"(?:\s*[,-]\s*(?:La\s+)?Maison\s+Fran[cç]aise(?:\s+de\s+Washington)?)?", re.I)
# "... at the French Embassy" / "Portuguese Ambassador's Residence ..."
_DEMONYM_EMBASSY = re.compile(
    r"(?:\s*(?:\bat|@)\s+)?(?:\bthe\s+)?\b(?P<d>" + _alternation(_DEMONYMS) + r")\s+(?:Embassy\b|Ambassador['’]s\s+)", re.I)


def _embassy_country(text):
    """-> (regex match or None, country) for 'Embassy of X' / 'X-ian Embassy' wording in text."""
    m = _EMBASSY_OF.search(text or "")
    if m:
        return m, _COUNTRY_KEY.get(m.group("c").lower(), m.group("c").title())
    m = _DEMONYM_EMBASSY.search(text or "")
    if m:
        return m, _DEMONYM_KEY.get(m.group("d").lower(), "")
    return None, ""


def shorten_embassy(e):
    """Embassy events: replace the long 'at the Embassy of X' wording with a 🏠 country tag before the event name."""
    title, venue = e["title"], e.get("venue", "")
    m, country = _embassy_country(title)
    if m:
        title = title[:m.start()] + " " + title[m.end():]
    venue_country = _embassy_country(venue)[1]
    country = country or venue_country
    if not country:
        return e
    title = re.sub(r"\s+([:,;!?])", r"\1", re.sub(r"\s+", " ", title)).strip(" -–—:|,@&")
    if title.isupper():
        title = title.title()  # "LE CHALET - OPERA COMIQUE" -> "Le Chalet - Opera Comique"
    else:
        title = re.sub(r"\b[A-Z]{5,}\b", lambda w: w.group().capitalize(), title)  # "EXHIBITION TOURS:"
    if len(title.split()) < 2:
        title = f"{title or 'Event'} at the Embassy"  # "Evening at the Embassy of China" -> "Evening at the Embassy"
    # the small line under the name says which embassy (replaces the long "Embassy of X in the United States" venue)
    where = f"{_SHORT_COUNTRY.get(country, country)} embassy"
    if venue and not venue_country and not re.search(r"embass|ambassad", venue, re.I):
        where += f" · {venue}"  # keep a real street address
    return {**e, "title": f"🏠 {title}", "venue": where}


HOT_LABELS = {"going-fast": ("🔥 Going fast", 0), "almost-full": ("⚡ Almost full", 0),
              "sales-end-soon": ("⏳ Sales end soon", 1)}


def hot_score(e):
    return HOT_LABELS.get(e.get("hot", ""), ("", 2))[1]


def parse_cost(text):
    """'Free' -> (0, 0); '$25; $20 members' -> (20, 25); anything else -> None."""
    text = (text or "").replace("\xa0", " ")
    amounts = [float(a.replace(",", "")) for a in re.findall(r"\$\s?([\d,]+(?:\.\d\d)?)", text)]
    if amounts:
        return (min(amounts), max(amounts))
    return (0.0, 0.0) if "free" in text.lower() else None


@st.cache_resource
def price_memory():
    """Eventbrite ticket prices by event URL -> ((low, high) or None, fetched_at). Survives refreshes."""
    return {}


def _fetch_eventbrite_price(url):
    try:
        page = requests.get(url, headers=BROWSER_HEADERS, timeout=12).text
    except Exception:
        return None
    if re.search(r'"isFree":\s*true', page):
        return (0.0, 0.0)
    m = re.search(r'"lowPrice":"([\d.]+)","highPrice":"([\d.]+)"', page)
    return (float(m.group(1)), float(m.group(2))) if m else None


def ensure_prices(events):
    """Look up ticket prices for the Eventbrite events about to be shown (8 at a time, kept 12 h)."""
    memory, stamp = price_memory(), time.time()
    todo = list(dict.fromkeys(e["url"] for e in events if "eventbrite.com/e/" in e.get("url", "")
                              and (e["url"] not in memory or stamp - memory[e["url"]][1] > 43200)))
    if todo:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for url, price in zip(todo, pool.map(_fetch_eventbrite_price, todo)):
                memory[url] = (price, stamp)
    for e in events:
        if e.get("url") in memory:
            e["price"] = memory[e["url"]][0]
    return events


@st.cache_data(ttl=3600, show_spinner=False)
def smithsonian_events():
    """Smithsonian museums' public events RSS (Trumba). Skips 'ongoing' exhibitions."""
    import html as html_lib
    import xml.etree.ElementTree as ET
    r = requests.get("https://www.trumba.com/calendars/smithsonian-events.rss", timeout=20)
    r.raise_for_status()
    events = []
    for item in ET.fromstring(r.content).iter("item"):
        desc = html_lib.unescape(item.findtext("description") or "")
        first = re.split(r"<br\s*/?>", desc)[0].strip()
        if first.lower().startswith("ongoing"):
            continue
        cat = item.findtext("category") or ""  # "2026/11/08 (Sun)"
        if not re.match(r"\d{4}/\d\d/\d\d", cat):
            continue
        time_txt = re.sub(r"^.*?\d{4},?\s*", "", first).replace("\xa0", " ")
        venue = re.search(r"Venue</b>:\s*([^<]+)", desc)
        cost = re.search(r"Cost</b>:\s*([^<]+)", desc)
        events.append({"price": parse_cost(cost.group(1)) if cost else None,
                       "date": cat[:10].replace("/", "-"), "time": time_txt[:30],
                       "title": html_lib.unescape(item.findtext("title") or ""),
                       "venue": venue.group(1).strip().replace("\xa0", " ") if venue else "Smithsonian",
                       "url": item.findtext("link") or ""})
    return events


@st.cache_data(ttl=3600, show_spinner=False)
def ticketmaster_music(key):
    """Optional: big-venue concerts (Anthem, Capital One Arena...) - needs a free Ticketmaster key."""
    r = requests.get("https://app.ticketmaster.com/discovery/v2/events.json", timeout=15, params={
        "apikey": key, "latlong": f"{HOME['lat']},{HOME['lon']}", "radius": 8, "unit": "miles",
        "classificationName": "music", "sort": "date,asc", "size": 30})
    r.raise_for_status()
    out = []
    for e in r.json().get("_embedded", {}).get("events", []):
        start = e.get("dates", {}).get("start", {})
        venue = (e.get("_embedded", {}).get("venues") or [{}])[0].get("name", "")
        t = start.get("localTime", "")
        pr = (e.get("priceRanges") or [{}])[0]
        out.append({"price": (float(pr["min"]), float(pr.get("max", pr["min"]))) if "min" in pr else None,
                    "date": start.get("localDate", ""), "title": e.get("name", ""), "venue": venue, "url": e.get("url", ""),
                    "time": datetime.strptime(t, "%H:%M:%S").strftime("%I:%M %p").lstrip("0") if t else ""})
    return out


# ------------------------------------------------------------------ flight deals (page 3)
DC_AIRPORT_WORDS = ("washington", "baltimore", "dca", "iad", "bwi", "dulles", "reagan")
EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍]")


def _rss_items(url, pages=1):
    """Lenient RSS reader (regex, so a slightly broken feed still works)."""
    import html as html_lib
    items = []
    for page in range(1, pages + 1):
        text = requests.get(url + (f"?paged={page}" if page > 1 else ""), headers=BROWSER_HEADERS, timeout=20).text
        for block in re.findall(r"<item>(.*?)</item>", text, re.S):
            def field(tag):
                m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
                return html_lib.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""
            items.append({"title": field("title"), "link": field("link"), "date": field("pubDate"),
                          "category": field("category")})
    return items


def _posted(pub_date):
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(pub_date).astimezone(TZ)
    except Exception:
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def us_flight_deals():
    """The Flight Deal: 'Airline: Origin – Destination. $390 (Basic Economy) / ...' (no key)."""
    deals = []
    for it in _rss_items("https://www.theflightdeal.com/feed/", pages=6):  # ~2 weeks of deals
        m = re.match(r"^(?P<air>[^:]{2,40}):\s*(?P<route>.+?)\.\s*\$(?P<price>[\d,]+)(?P<rest>.*)$", it["title"])
        if not m:
            continue
        parts = [p.strip() for p in re.split(r"\s+[–-]\s+", m.group("route"))]
        origin = it["category"] or (parts[0] if parts else "")
        dest = re.sub(r"\s*\(and vice versa\)", "", parts[-1]) if len(parts) > 1 else m.group("route")
        cabin = re.search(r"\((Basic Economy|Economy|Premium Economy|Business|First)[^)]*\)", m.group("rest"))
        deals.append({"airline": m.group("air").strip(), "origin": origin, "dest": dest,
                      "price": f'${m.group("price")}', "cabin": cabin.group(1) if cabin else "",
                      "both_ways": "vice versa" in m.group("route"), "posted": _posted(it["date"]),
                      "link": it["link"], "title": it["title"]})
    return deals


NORTH_AMERICA = ("usa", "u.s.", "united states", "new york", "nyc", "washington", "boston", "chicago",
                 "los angeles", "san francisco", "miami", "seattle", "dallas", "houston", "atlanta", "newark",
                 "denver", "philadelphia", "orlando", "las vegas", "toronto", "vancouver", "montreal", "canada")


@st.cache_data(ttl=3600, show_spinner=False)
def world_flight_deals(feed="https://www.fly4free.com/feed/"):
    """Fly4free deals (flights only; hotel-only offers skipped). No key.
    The business-class page passes Fly4free's business-class feed instead."""
    deals = []
    for it in _rss_items(feed):
        title = re.sub(r"\s{2,}", " ", EMOJI.sub("", it["title"])).strip(" !")
        if not re.search(r"flight|fly|round-?trip|return|business|class", title, re.I):
            continue
        price = re.search(r"(?:for|from)\s+((?:[€£$]\s?[\d,.]+)|(?:[\d,.]+\s?(?:€|EUR|USD|PLN|£)))", title, re.I)
        route = re.search(r"from\s+(.+?)\s+to\s+(.+?)(?:\s+(?:for|from|with)\s|$)", title, re.I)
        deals.append({"title": title, "price": price.group(1).replace(" ", "") if price else "",
                      "origin": route.group(1) if route else "", "dest": route.group(2) if route else "",
                      "posted": _posted(it["date"]), "link": it["link"]})
    return deals


# ------------------------------------------------------------------ world news (page 5): data
# Each region's own press, mostly their world desks, so the page shows what every part of the world
# is choosing to cover. All plain RSS, no keys. (code, name, [(outlet, feed url)])
NEWS_REGIONS = [
    ("AM", "Americas", [("NPR", "https://feeds.npr.org/1004/rss.xml"),
                        ("CBC", "https://www.cbc.ca/webfeed/rss/rss-world"),
                        ("Mexico News Daily", "https://mexiconewsdaily.com/feed/"),
                        ("Buenos Aires Times", "https://www.batimes.com.ar/feed"),
                        ("MercoPress", "https://en.mercopress.com/rss/"),
                        ("Rio Times", "https://www.riotimesonline.com/feed/")]),
    ("EU", "Europe", [("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
                      ("DW", "https://rss.dw.com/rdf/rss-en-world"),
                      ("France 24", "https://www.france24.com/en/rss"),
                      ("Euronews", "https://www.euronews.com/rss?format=mrss&level=theme&name=news"),
                      ("Guardian", "https://www.theguardian.com/world/rss")]),
    ("ME", "Middle East", [("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
                           ("Times of Israel", "https://www.timesofisrael.com/feed/"),
                           ("Middle East Eye", "https://www.middleeasteye.net/rss")]),
    ("AF", "Africa", [("AllAfrica", "https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf"),
                      ("Premium Times", "https://www.premiumtimesng.com/feed"),
                      ("Daily Maverick", "https://www.dailymaverick.co.za/dmrss/")]),
    ("AP", "Asia-Pacific", [("SCMP", "https://www.scmp.com/rss/91/feed"),
                            ("Japan Times", "https://www.japantimes.co.jp/feed/"),
                            ("The Hindu", "https://www.thehindu.com/news/international/feeder/default.rss"),
                            ("CNA", "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6311"),
                            ("Dawn", "https://www.dawn.com/feeds/home")]),
]
NEWS_KEEP_HOURS = 48
NEWS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_archive.json")

# Themes: colours are the dark-mode categorical palette in its fixed order (CVD-checked for side-by-side use).
NEWS_THEMES = [
    ("Conflict", "#3987e5", r"war|wars|strikes?|airstrikes?|missiles?|drones?|attacks?|troops|military|army|ceasefire|"
                            r"truce|shelling|militants?|hostages?|bomb\w*|invasion|weapons?|soldiers?|navy|fighting|"
                            r"offensive|rebels?|clashes|gunmen|shooting"),
    ("Politics", "#d95926", r"elections?|votes?|voted|voters|polls?|parliament|president|prime minister|coalition|"
                            r"opposition|campaign|referendum|ministers?|government|protests?|summit|diplomat\w*|talks|"
                            r"sanctions|party|lawmakers|senate|congress"),
    ("Economy", "#199e70", r"tariffs?|trade|economy|economic|inflation|markets?|stocks?|banks?|interest rates?|oil|"
                           r"prices|jobs|gdp|budget|debt|invest\w*|exports?|imports?|growth|currency|recession|business"),
    ("Climate & disasters", "#c98500", r"climate|cop\d+|floods?|flooding|earthquake|quake|storms?|hurricane|typhoon|"
                                       r"cyclone|wildfires?|drought|heatwave|emissions|landslide|tsunami|volcano|"
                                       r"crash|collision|derail\w*"),
    ("Tech & science", "#d55181", r"ai|artificial intelligence|tech\w*|chips?|semiconductors?|cyber\w*|space|satellites?|"
                                  r"nvidia|robot\w*|crypto\w*|bitcoin|internet|starlink|science|scientists"),
    ("Health", "#008300", r"health|virus|outbreak|vaccines?|hospitals?|disease|cholera|mpox|measles|ebola|malaria|"
                          r"cancer|doctors|pandemic|world health"),
    ("Justice & rights", "#9085e9", r"courts?|trial|judge|police|arrest\w*|rights|corruption|sentenced|prison|jail\w*|"
                                    r"migrants?|refugees?|asylum|deport\w*|lawsuit|charged|abuse"),
    ("Other", "#475569", r"(?!x)x"),  # never matches: anything no theme claims
]
NEWS_THEME_RES = [re.compile(rf"\b(?:{words})\b") for _, _, words in NEWS_THEMES]

# Names written different ways count as one: demonyms, capitals used as shorthand, full names.
NEWS_ALIASES = {
    "US": ["U.S.A.", "U.S.", "USA", "United States", "American", "Americans"],
    "UK": ["U.K.", "United Kingdom", "Britain", "British"],
    "UN": ["U.N.", "United Nations"], "EU": ["European Union"], "NATO": ["Nato"],
    "WHO": ["World Health Organization"], "AI": ["artificial intelligence", "Artificial intelligence"],
    "COP31": ["Cop31", "COP 31", "Cop 31"],
    "Trump": ["Donald Trump"], "Putin": ["Vladimir Putin"],
    "Zelensky": ["Volodymyr Zelensky", "Volodymyr Zelenskyy", "Zelenskyy"],
    "Netanyahu": ["Benjamin Netanyahu"], "Xi Jinping": ["Xi"], "Modi": ["Narendra Modi"],
    "Milei": ["Javier Milei"], "Lula": ["Luiz Inacio Lula da Silva", "Lula da Silva"],
    "Sheinbaum": ["Claudia Sheinbaum"], "Starmer": ["Keir Starmer"], "Macron": ["Emmanuel Macron"],
    "Pope Leo": ["Pope Leo XIV", "Leo XIV"], "Kim Jong Un": ["Kim Jong-un", "Kim Jong-Un"],
    "Iran": ["Iranian", "Iranians", "Tehran"], "Israel": ["Israeli", "Israelis"],
    "Ukraine": ["Ukrainian", "Ukrainians", "Kyiv", "Kiev"], "Russia": ["Russian", "Russians", "Kremlin", "Moscow"],
    "China": ["Chinese", "Beijing"], "Gaza": ["Gazan", "Gazans", "Gaza Strip"], "Palestinians": ["Palestinian"],
    "Syria": ["Syrian", "Syrians", "Damascus"], "Lebanon": ["Lebanese", "Beirut"],
    "India": ["Indian", "Indians", "New Delhi"], "Pakistan": ["Pakistani", "Pakistanis", "Islamabad"],
    "Japan": ["Japanese", "Tokyo"], "Taiwan": ["Taiwanese", "Taipei"],
    "North Korea": ["North Korean", "North Koreans", "Pyongyang"], "South Korea": ["South Korean", "Seoul"],
    "Venezuela": ["Venezuelan", "Venezuelans", "Caracas"], "Mexico": ["Mexican", "Mexicans"],
    "Brazil": ["Brazilian", "Brazilians"], "Argentina": ["Argentine", "Argentinian", "Argentines"],
    "Colombia": ["Colombian"], "Chile": ["Chilean"], "Peru": ["Peruvian"], "Bolivia": ["Bolivian"],
    "Cuba": ["Cuban", "Cubans"], "Haiti": ["Haitian", "Haitians"], "Canada": ["Canadian", "Canadians", "Ottawa"],
    "Germany": ["German", "Germans", "Berlin"], "France": ["French"], "Italy": ["Italian"], "Spain": ["Spanish"],
    "Poland": ["Polish"], "Turkey": ["Turkish", "Turkiye", "Ankara"],
    "UAE": ["United Arab Emirates", "Emirati", "Emiratis"], "Abu Dhabi": [], "Egypt": ["Egyptian", "Cairo"],
    "Sudan": ["Sudanese", "Khartoum"], "Nigeria": ["Nigerian", "Nigerians", "Abuja"], "Kenya": ["Kenyan", "Nairobi"],
    "Ethiopia": ["Ethiopian"], "Congo": ["Congolese", "DR Congo", "DRC"], "Afghanistan": ["Afghan", "Afghans"],
    "Houthis": ["Houthi"], "Saudi Arabia": ["Saudi"], "South Africa": ["South African", "South Africans"],
    "Australia": ["Australian", "Australians", "Canberra"], "Philippines": ["Filipino", "Philippine", "Manila"],
    "Indonesia": ["Indonesian", "Jakarta"], "Europe": ["European", "Europeans"], "Africa": ["African", "Africans"],
    "Asia": ["Asian"], "Latin America": ["Latin American", "Latin Americans"],
    "Americas": ["North America", "North American", "South America", "South American"],
    "Strait of Hormuz": ["Hormuz"], "Hong Kong": [], "New Zealand": [], "Middle East": [], "West Bank": [],
    "White House": [], "Sri Lanka": [], "Red Sea": [], "Wall Street": [], "El Salvador": [], "New York": [],
}
NEWS_ALIAS = {}
for _canon, _variants in NEWS_ALIASES.items():
    for _v in _variants + ([_canon] if " " in _canon else []):
        NEWS_ALIAS[_v] = _canon.replace(" ", "_")  # multi-word names become one token, e.g. Hong_Kong
NEWS_ALIAS_RE = re.compile(r"(?<![\w.])(?:" + "|".join(re.escape(v) for v in sorted(NEWS_ALIAS, key=len, reverse=True))
                           + r")(?!\w)")
NEWS_CANON = {c.replace(" ", "_") for c in NEWS_ALIASES}
# Too broad to be a story on their own (they stay visible as context words on the cards)
NEWS_TOO_BROAD = {"US", "UK", "EU", "UN", "Europe", "Africa", "Asia", "Americas", "Latin_America", "Middle_East"}
NEWS_STOP = set("""
a an the and or but nor of in on at to for from by with without into onto over under after before about as is are was
were be been being has have had will would can could may might must shall should do does did done not no yes it its
this that these those he she they we you i his her their our your my me him them us who whom whose what when where
why how which than then there here up down out off new says said say tells told asks warns calls vows urges plans
live latest update updates watch video videos photo photos picture pictures opinion analysis explainer exclusive
breaking report reports news world day days week weeks month months year years today tonight tomorrow yesterday
more most less first last next one two three four five six seven eight nine ten amid over just also all any some
many much very time times man woman men women people children child top big via per mr mrs ms dr sir who's what's
ceo cfo coo chairman chairwoman spokesperson spokesman spokeswoman
how's it's after-hours inside behind against between during while despite still back again set get gets got make
makes made take takes took could've part home group groups deal end start chief head leader leaders official
officials president presidents minister ministers ministry prime chancellor king queen prince princess pope general
government governments police court courts army party parties state states national international global foreign
city capital country countries region regional nation nations north south east west northern southern eastern
western central monday tuesday wednesday thursday friday saturday sunday january february march april may june july
august september october november december jan feb mar apr jun jul aug sep sept oct nov dec bbc npr cbc dw cna scmp
guardian euronews jazeera reuters afp ap bloomberg times post daily mail press media tv podcast newsletter review
editorial letters letter column sport sports football cup league
""".split())
NEWS_TOKEN = re.compile(r"[A-Za-z0-9][\w\-]*(?:['’][A-Za-z]+)?")
NEWS_BREAK = re.compile(r"[.!?:;|\"“”()\[\]–—]+|\s-\s")


def _news_time(value):
    """Feed date (RFC 822 or ISO 8601) -> aware UTC datetime, or None."""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(value)
    except Exception:
        try:
            d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _feed_entries(url):
    """Lenient RSS / RDF / Atom reader: title, link, short summary and UTC date of each item."""
    import html as html_lib
    raw = requests.get(url, headers=BROWSER_HEADERS, timeout=20).content
    enc = re.search(rb'encoding=["\']([\w.-]+)', raw[:300])
    try:
        text = raw.decode(enc.group(1).decode() if enc else "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")

    def clean(s):  # some feeds double-escape (&amp;#8217;) and put HTML in summaries
        s = html_lib.unescape(html_lib.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", s)))
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()

    entries = []
    for block in re.findall(r"<(?:item|entry)(?:\s[^>]*)?>(.*?)</(?:item|entry)>", text, re.S):
        def field(*tags):
            for tag in tags:
                m = re.search(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", block, re.S)
                if m and m.group(1).strip():
                    return clean(m.group(1))
            return ""
        link = field("link")
        if not link:  # Atom: <link href="..."/>
            m = re.search(r'<link[^>]*href="([^"]+)"', block)
            link = m.group(1) if m else ""
        posted = _news_time(field("pubDate", "dc:date", "published", "updated"))
        title = field("title")
        if title:
            entries.append({"title": title, "link": link, "desc": field("description", "summary")[:300],
                            "posted": posted.isoformat(timespec="seconds") if posted else ""})
    return entries


@st.cache_data(ttl=900, show_spinner=False)
def news_fetch():
    """Latest headlines from every outlet, fetched in parallel. Returns (items, outlets that did not respond)."""
    jobs = [(code, name, url) for code, _, outlets in NEWS_REGIONS for name, url in outlets]

    def one(job):
        try:
            return job, _feed_entries(job[2])[:30]  # cap so one very long feed can't dominate
        except Exception:
            return job, None

    items, down = [], []
    with ThreadPoolExecutor(8) as ex:
        for (code, name, _), entries in ex.map(one, jobs):
            if not entries:
                down.append(name)
                continue
            items += [dict(e, region=code, source=name) for e in entries]
    return items, down


@st.cache_resource
def news_archive():
    """Headlines seen in the last 48 h, kept between refreshes (and restarts, via news_archive.json), so
    trends reach back further than the few hours a short feed holds."""
    try:
        with open(NEWS_FILE, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        items = {}
    return {"items": items if isinstance(items, dict) else {}, "lock": threading.Lock()}


def news_items():
    """Merge the latest fetch into the 48 h archive. Returns (rows with UTC 'posted', outlets down, now UTC)."""
    fresh, down = news_fetch()
    now_utc = datetime.now(timezone.utc)
    cutoff = (now_utc - timedelta(hours=NEWS_KEEP_HOURS)).isoformat(timespec="seconds")
    stamp = now_utc.isoformat(timespec="seconds")
    store = news_archive()
    with store["lock"]:
        items = store["items"]
        added = 0
        for it in fresh:
            key = it["link"] or it["title"].lower()
            if key not in items and (it["posted"] or stamp) >= cutoff:
                items[key] = dict(it, seen=stamp)
                added += 1
        stale = [k for k, v in items.items() if (v.get("posted") or v.get("seen", "")) < cutoff]
        for k in stale:
            del items[k]
        if added or stale:
            try:
                with open(NEWS_FILE + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(items, f)
                os.replace(NEWS_FILE + ".tmp", NEWS_FILE)
            except OSError:
                pass  # still works from memory
        rows = []
        for v in items.values():
            try:
                posted = datetime.fromisoformat(v.get("posted") or v["seen"])
            except Exception:
                continue
            rows.append(dict(v, posted=min(posted, now_utc)))  # a feed dated in the future counts as "now"
    return rows, down, now_utc


def _news_chunks(text):
    """Text -> list of phrases (split at punctuation), each a list of tokens, with aliases merged."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))  # Kraków = Krakow
    text = NEWS_ALIAS_RE.sub(lambda m: NEWS_ALIAS[m.group(0)], text)
    chunks = []
    for part in NEWS_BREAK.split(text):
        toks = [re.sub(r"['’]s?$", "", t).strip("-") for t in NEWS_TOKEN.findall(part)]
        toks = [t for t in toks if t]
        if toks:
            chunks.append(toks)
    return chunks


def _title_case(chunks):
    """True for Headlines Written Like This, where capitals say nothing about names."""
    words = [t for c in chunks for t in c[1:] if t.isalpha() and t.lower() not in NEWS_STOP]
    return len(words) >= 3 and sum(t[0].isupper() for t in words) >= .75 * len(words)


def analyze_news(rows, now_utc):
    """Find the global narratives: names (people, places, groups) that 3+ outlets in 2+ regions are covering.
    Names are learned from the text itself: a word counts as a name when it is mostly written with a capital
    letter mid-sentence."""
    parsed = [(_news_chunks(r["title"]), _news_chunks(r["desc"])) for r in rows]
    cap, low = Counter(), Counter()
    for tc, dc in parsed:
        for chunks in ((dc,) if _title_case(tc) else (tc, dc)):
            for chunk in chunks:
                for i, t in enumerate(chunk):
                    if t[0].islower():
                        low[t] += 1
                    elif i and t[0].isupper():
                        cap[t] += 1
    memo = {}

    def proper(t):
        if t not in memo:
            if t in NEWS_CANON:
                memo[t] = True
            elif t.lower() in NEWS_STOP or not t[0].isupper():
                memo[t] = False
            elif re.fullmatch(r"[A-Z][A-Z0-9]{1,5}", t):  # acronyms: NATO, G7, COP31, ANC
                memo[t] = True
            else:
                memo[t] = len(t) >= 3 and cap[t] >= 2 and cap[t] >= 2 * low[t.lower()]
        return memo[t]

    def names(chunks):
        found = set()
        for chunk in chunks:
            prev = None
            for t in chunk:
                if proper(t):
                    found.add(t)
                    if prev:
                        found.add(f"{prev} {t}")  # two-word names, e.g. "Jensen Huang"
                    prev = t
                else:
                    prev = None
        return found

    title_terms = [names(tc) for tc, _ in parsed]
    terms = [tt | names(dc) for tt, (_, dc) in zip(title_terms, parsed)]

    themes = []
    for r in rows:
        title, desc = r["title"].lower(), r["desc"].lower()
        scores = [2 * len(rx.findall(title)) + len(rx.findall(desc)) for rx in NEWS_THEME_RES]
        best = max(range(len(scores)), key=scores.__getitem__)
        themes.append(best if scores[best] else len(NEWS_THEMES) - 1)

    by_term = defaultdict(set)
    for i, ts in enumerate(terms):
        for t in ts:
            by_term[t].add(i)
    cands = []
    for t, ids in by_term.items():
        if t in NEWS_TOO_BROAD or any(p in NEWS_TOO_BROAD for p in t.split(" ")):
            continue
        regions = {rows[i]["region"] for i in ids}
        sources = {rows[i]["source"] for i in ids}
        if len(sources) >= 3 and len(regions) >= 2:
            cands.append((len(regions), len(sources), len(ids), t))
    cands.sort(reverse=True)

    # Trends compare a story's SHARE of the last 6 h of headlines with its share of the 42 h before, so feeds
    # that only hold a few hours of items (and a freshly started archive) don't make everything look "rising".
    ages_all = [(now_utc - r["posted"]).total_seconds() / 3600 for r in rows]
    all_recent = sum(a < 6 for a in ages_all) or 1
    all_before = sum(6 <= a < 48 for a in ages_all)
    narratives = []
    for _, _, _, t in cands:
        ids = by_term[t]
        # mostly the same stories as a narrative already picked (Trump + Iran, Leo + Pope Leo): fold into it
        if any(len(ids & n["ids"]) >= .5 * min(len(ids), len(n["ids"])) for n in narratives):
            continue
        label = t
        if " " not in t:  # prefer the fuller name when it covers most of the same stories
            fuller = [b for b in by_term if " " in b and t in b.split(" ") and len(by_term[b]) >= .6 * len(ids)]
            label = max(fuller, key=lambda b: len(by_term[b])) if fuller else t
        # context: the other names in these stories; two-word names win over their parts ("Abu Dhabi", not "Abu")
        used = set(label.split(" ")) | {t}
        co = Counter(x for i in ids for x in terms[i] if not set(x.split(" ")) & used)
        ctx = []
        for x, c in sorted(co.items(), key=lambda kv: (kv[1], " " in kv[0]), reverse=True):
            if c < 2 or len(ctx) == 3:
                break
            if not set(x.split(" ")) & used:
                ctx.append(x.replace("_", " "))
                used |= set(x.split(" "))
        ages = [(now_utc - rows[i]["posted"]).total_seconds() / 3600 for i in ids]
        spark = [0] * 12  # 4-hour bins over 48 h, oldest first
        for a in ages:
            if 0 <= a < 48:
                spark[11 - int(a // 4)] += 1
        recent = sum(a < 6 for a in ages)
        before = sum(6 <= a < 48 for a in ages)
        share_now, share_before = recent / all_recent, before / all_before if all_before else 0
        if all_before >= 50 and before == 0 and recent >= 3:
            trend = ("new", "✦ New")
        elif recent >= 3 and all_before and share_now >= 1.5 * share_before:
            trend = ("up", "▲ Rising")
        elif before >= 3 and share_now <= .4 * share_before:
            trend = ("down", "▼ Fading")
        else:
            trend = ("flat", "● Steady")
        narratives.append({
            "label": label.replace("_", " "), "ids": ids,
            "regions": {rows[i]["region"] for i in ids}, "sources": {rows[i]["source"] for i in ids},
            "ctx": ctx, "spark": spark, "trend": trend,
            "rep": max(ids, key=lambda i: (t in title_terms[i], rows[i]["posted"])),  # newest headline naming it
        })
        if len(narratives) == 8:
            break
    return {"narratives": narratives, "themes": themes}


@st.cache_data(ttl=600, show_spinner=False)
def weather():
    r = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": HOME["lat"], "longitude": HOME["lon"],
        "current": "temperature_2m,apparent_temperature,weather_code,precipitation,wind_speed_10m",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "daily": "sunrise,sunset,weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "America/New_York", "forecast_days": 3}, timeout=15)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=300, show_spinner=False)
def nws_alerts():
    """Active National Weather Service watches / warnings / advisories for the home address."""
    r = requests.get("https://api.weather.gov/alerts/active",
                     params={"point": f"{HOME['lat']},{HOME['lon']}"},
                     headers={"User-Agent": "DC-Transit-Hub-kiosk", "Accept": "application/geo+json"}, timeout=15)
    r.raise_for_status()
    return [f["properties"] for f in r.json().get("features", [])]


@st.cache_data(ttl=1800, show_spinner=False)
def air_quality():
    r = requests.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
        "latitude": HOME["lat"], "longitude": HOME["lon"],
        "current": "us_aqi,us_aqi_pm2_5,us_aqi_ozone,us_aqi_nitrogen_dioxide,pm2_5,ozone",
        "hourly": "us_aqi", "timezone": "America/New_York", "forecast_days": 2}, timeout=15)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=3600, show_spinner=False)
def sun_days():
    """Yesterday, today, tomorrow, day after: sunrise / sunset / daylight length / peak UV."""
    r = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": HOME["lat"], "longitude": HOME["lon"],
        "daily": "sunrise,sunset,daylight_duration,uv_index_max",
        "timezone": "America/New_York", "past_days": 1, "forecast_days": 3}, timeout=15)
    r.raise_for_status()
    return r.json()["daily"]


@st.cache_data(ttl=86400, show_spinner=False)
def bike_station_info():
    r = requests.get("https://gbfs.lyft.com/gbfs/2.3/dca-cabi/en/station_information.json", timeout=15)
    r.raise_for_status()
    return r.json()["data"]["stations"]


@st.cache_data(ttl=60, show_spinner=False)
def bike_station_status():
    r = requests.get("https://gbfs.lyft.com/gbfs/2.3/dca-cabi/en/station_status.json", timeout=15)
    r.raise_for_status()
    return {s["station_id"]: s for s in r.json()["data"]["stations"]}


@st.cache_data(ttl=600, show_spinner=False)
def tomtom_minutes(lat, lon):
    coords = f"{HOME['lat']},{HOME['lon']}:{lat},{lon}"
    r = requests.get(f"https://api.tomtom.com/routing/1/calculateRoute/{coords}/json",
                     params={"traffic": "true", "travelMode": "car", "key": TOMTOM_KEY}, timeout=10)
    r.raise_for_status()
    s = r.json()["routes"][0]["summary"]
    return round(s["travelTimeInSeconds"] / 60), round(s.get("trafficDelayInSeconds", 0) / 60)


# ------------------------------------------------------------------ formatting
def train_eta(train):
    value = str(train.get("Min", "")).strip().upper()
    if value in ("BRD", "ARR"):
        return 0
    return int(value) if value.isdigit() else None


def badge(line):
    color = LINE_COLORS.get(line, "#475569")
    return f'<span class="badge" style="background:{color}">{esc(line)}</span>'


def weather_icon(code):
    if code == 0: return "☀️"
    if code in (1, 2): return "⛅"
    if code == 3: return "☁️"
    if code in (45, 48): return "🌫️"
    if code in (71, 73, 75, 77, 85, 86): return "❄️"
    if code in (95, 96, 99): return "⛈️"
    if code is not None and code >= 51: return "🌧️"
    return "☁️"


def short_date(value):
    try:
        return datetime.fromisoformat(value).strftime("%b %-d") if os.name != "nt" else \
            datetime.fromisoformat(value).strftime("%b %#d")
    except Exception:
        return "TBD"


# ------------------------------------------------------------------ styles
show("""
<style>
/* Everything is sized from the screen height (vh) so the whole board fits on one screen,
   from a 768px laptop panel up to a 1080p+ TV. clamp(min, preferred, max). */
.block-container, [data-testid="stMainBlockContainer"] {padding: .6vh 1vw .2vh !important; max-width: 100% !important;}
header[data-testid="stHeader"] {background: transparent; height: 0; min-height: 0;}
/* Sidebar stays hidden; a "Settings" pull tab on the left edge slides it out */
[data-testid="stExpandSidebarButton"] {
  position: fixed !important; left: 0; top: 50%; transform: translateY(-50%); z-index: 999990;
  width: auto !important; height: auto !important; padding: 1.4vh .35vw !important;
  background: #1e293b !important; color: #e5e7eb !important; border: 1px solid #334155 !important;
  border-left: none !important; border-radius: 0 1vh 1vh 0 !important; opacity: .55;
  writing-mode: vertical-rl; display: flex; align-items: center; gap: .6vh; transition: opacity .2s;}
[data-testid="stExpandSidebarButton"]:hover {opacity: 1;}
[data-testid="stExpandSidebarButton"]::after {content: "⚙ Settings"; font-size: clamp(11px,1.3vh,14px); font-weight: 700;}
[data-testid="stExpandSidebarButton"] svg, [data-testid="stExpandSidebarButton"] span {display: none;}
[data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapseButton"] button {visibility: visible !important;}
#MainMenu, footer, [data-testid="stDecoration"], [data-testid="stToolbarActions"], [data-testid="stMainMenu"],
[data-testid="stAppDeployButton"], [data-testid="stStatusWidget"] {display: none !important;}
[data-testid="stVerticalBlock"] {gap: .7vh !important;}
[data-testid="stHorizontalBlock"] {gap: .8vw !important;}
.stElementContainer:has(iframe[title="streamlit_autorefresh.st_autorefresh"]) {display: none;}
[data-testid="stMarkdown"], .stMarkdown, [data-testid="stMarkdownContainer"] {margin: 0 !important;}
[data-testid="stMarkdownContainer"] > * {margin-bottom: 0 !important;}
iframe[title="st.iframe"] {height: 56vh !important; border-radius: 1.2vh;}
.stElementContainer:has(iframe[title="st.iframe"]) {height: 56vh !important;}
.top {display:flex; flex-wrap:wrap; gap:1.5vw; align-items:center; justify-content:space-between;
      background:#111a2e; border-radius:1.3vh; padding:.7vh 1.2vw .7vh 2.6vw;}
.top h1 {font-size:clamp(17px,2.5vh,30px); margin:0; padding:0; font-weight:800; line-height:1.15;}
.top .sub {opacity:.65; font-size:clamp(11px,1.3vh,14px);}
.clock {font-size:clamp(24px,4vh,46px); font-weight:800; line-height:1;}
.clock span {font-size:.4em; opacity:.7;}
.wx {display:flex; gap:1vw; align-items:center;}
.wx .now {font-size:clamp(22px,3.5vh,40px); font-weight:800; line-height:1;}
.wx .meta {font-size:clamp(11px,1.3vh,14px); opacity:.85; line-height:1.35;}
.wx .strip {display:flex; gap:.7vw; font-size:clamp(10px,1.2vh,13px); text-align:center; opacity:.9; line-height:1.25;}
.wx .strip.days {border-left:1px solid #334155; padding-left:.8vw; white-space:nowrap;}
.rain-hi {color:#f87171;} .rain-mid {color:#fb923c;} .rain-lo {color:#4ade80;}
.alert {background:#7f1d1d; border-left:6px solid #ef4444; padding:.5vh 1vw; border-radius:.8vh;
        font-weight:600; font-size:clamp(11px,1.4vh,15px);}
.notice {background:#422006; border-left:6px solid #f59e0b; padding:.4vh 1vw; border-radius:.8vh;
         font-size:clamp(10px,1.25vh,14px); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.panel {background:#111a2e; border-radius:1.3vh; padding:.8vh 1vw;}
.panel h2 {font-size:clamp(15px,2.1vh,24px); margin:0; padding:0; line-height:1.3;}
.panel h2 .hint {font-size:clamp(10px,1.25vh,13px); font-weight:400; opacity:.65; margin-left:.5vw;}
.panel h3 {font-size:clamp(13px,1.7vh,18px); margin:0 0 .6vh; padding:0;}
.panel .hint {opacity:.65; font-size:clamp(10px,1.2vh,13px); margin-bottom:.5vh;}
.dirs {display:grid; grid-template-columns:1fr 1fr; gap:.8vw; margin-top:.4vh;}
.dirs h4 {font-size:clamp(10px,1.25vh,14px); text-transform:uppercase; letter-spacing:.04em; opacity:.75;
          margin:0 0 .4vh; padding:0;}
.train {display:flex; align-items:center; gap:.7vw; padding:.45vh .6vw; border-radius:1vh;
        background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.08); margin-bottom:.45vh;}
.train .mins {font-size:clamp(17px,2.6vh,30px); font-weight:800; min-width:3.2em; text-align:right; line-height:1;}
.train .mins small {font-size:.45em; font-weight:600; opacity:.7; margin-left:2px;}
.train .dest {flex:1; min-width:0; line-height:1.2;}
.train .dest b {display:block; font-size:clamp(12px,1.55vh,17px); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.train .dest span {font-size:clamp(10px,1.15vh,13px); opacity:.75;}
.train.catch {border-color:#22c55e; background:rgba(34,197,94,.12);}
.train.catch .dest span {color:#4ade80; opacity:1; font-weight:700;}
.train.miss {opacity:.38;}
.badge {color:#fff; font-weight:800; font-size:clamp(9px,1.1vh,12px); padding:.2vh .6vw; border-radius:999px;}
.empty {opacity:.6; font-size:clamp(11px,1.3vh,14px); padding:.4vh 2px;}
.bus {display:grid; grid-template-columns:3.6em 1fr auto; gap:.6vw; align-items:center;
      padding:.4vh .5vw; border-radius:1vh; background:rgba(255,255,255,.04); margin-bottom:.4vh;
      font-size:clamp(11px,1.35vh,15px);}
.bus.featured {background:rgba(245,158,11,.12); border:1px solid rgba(245,158,11,.45);}
.route {font-weight:800; font-size:1.1em; text-align:center; background:#1e3a8a; border-radius:.7vh; padding:.2vh 0;}
.bus.featured .route {background:#b45309;}
.bus .dir .arrow {font-size:1.2em; color:#fbbf24;}
.legend {display:flex; flex-wrap:wrap; gap:.8vw; font-size:clamp(10px,1.15vh,12px); opacity:.8;}
.bus .dir {line-height:1.2; min-width:0;}
.bus .dir small {display:block; opacity:.6; font-size:.85em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.bus .dir .connects {color:#93c5fd;}
.route {color:#0b1220;}
.legend .line {display:inline-block; width:1.6em; height:0; border-top:4px solid; vertical-align:middle; margin-right:3px;}
.bus .times {font-size:1.4em; font-weight:800; white-space:nowrap;} .bus .times small {font-size:.55em; opacity:.7;}
.row {display:flex; justify-content:space-between; gap:.5vw; padding:.45vh 0;
      border-bottom:1px solid rgba(255,255,255,.07); font-size:clamp(11px,1.3vh,14px); line-height:1.3;}
.row:last-child {border-bottom:none;}
.row b {font-size:1.05em;} .row a {color:#93c5fd; text-decoration:none; margin-left:.5vw;}
[data-testid="stHeaderActionElements"], .top h1 a {display:none !important;}
/* top bar: title + weather, rotate controls (real buttons), clock */
.st-key-topbar {background:#111a2e; border-radius:1.3vh; padding:.5vh 1.2vw .5vh 2.6vw; gap:0 !important;}
.st-key-topbar .top {background:none; padding:0; border-radius:0; flex-wrap:nowrap;}
.st-key-topbar .clock {text-align:right; white-space:nowrap;}
.st-key-topbar [data-testid="stHorizontalBlock"] {gap:.6vw !important;}
.st-key-rotate_pick button, .st-key-hold_pick button {min-height:0 !important; padding:.35vh .7vw !important;
  font-size:clamp(10px,1.25vh,13px) !important; font-weight:700;}
.st-key-rotate_pick button p, .st-key-hold_pick button p {font-size:inherit !important; font-weight:700;}
.st-key-rotate_pick button, .st-key-hold_pick button {
  background:#1e293b !important; color:#94a3b8 !important; border-color:#334155 !important;}
.st-key-rotate_pick button[aria-pressed="true"], .st-key-hold_pick button[aria-pressed="true"],
.st-key-hold_pick button[aria-checked="true"], .st-key-hold_pick button[data-selected="true"] {
  background:#f59e0b !important; color:#0b1220 !important; border-color:#f59e0b !important;}
.st-key-rotate_pick [data-testid="stButtonGroup"] > div, .st-key-hold_pick [data-testid="stButtonGroup"] > div {
  flex-wrap:wrap !important; row-gap:.4vh; overflow:visible !important;}  /* narrow window: wrap, don't hide */
@media (max-width: 1500px) {
  .st-key-rotate_pick button, .st-key-hold_pick button {padding:.3vh .45vw !important; font-size:11px !important;}
  .st-key-topbar .wx .strip {gap:.45vw;}
}
.st-key-rotate_on label p {font-size:clamp(11px,1.35vh,15px) !important; font-weight:700; white-space:nowrap;}
.nowshow {font-size:clamp(9px,1.1vh,12px); opacity:.6; margin-top:.3vh; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.pages {display:flex; gap:.4vw;}
.pages b {font-size:clamp(10px,1.2vh,13px); padding:.3vh .7vw; border-radius:999px; background:#1e293b; opacity:.5;}
.pages b.on {background:#f59e0b; color:#0b1220; opacity:1;}
.evpanel {height:calc(100vh - 13vh); overflow:hidden;}
.evpanel.below-top {height:calc(100vh - 30vh);}  /* event columns sit under the auto-height top strip */
.dealpanel {height:calc(100vh - 13vh);}
.qr {height:5.2vh; width:5.2vh; min-height:40px; min-width:40px; border-radius:.4vh; flex-shrink:0;
     image-rendering:pixelated; margin-left:.3vw;}
.deal .what {flex:1;}
.scanhint {font-size:clamp(10px,1.2vh,13px); font-weight:400; opacity:.6; margin-left:1vw;}
.deal {display:flex; gap:.9vw; align-items:center; padding:.7vh .6vw; border-radius:1vh;
       background:rgba(255,255,255,.04); margin-bottom:.6vh;}
.deal .price {min-width:4.6em; font-size:clamp(16px,2.4vh,26px); font-weight:800; color:#4ade80; text-align:right;}
.deal .what {min-width:0; line-height:1.25;}
.deal .what b {display:block; font-size:clamp(12px,1.6vh,17px); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.deal .what span {display:block; font-size:clamp(10px,1.2vh,13px); opacity:.65; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.toppanel {height:auto; overflow:visible; border:1px solid rgba(244,63,94,.35); padding-bottom:.7vh;}
.topgrid .ev {padding:.45vh .5vw;}
.toppanel h3 {font-size:clamp(14px,1.9vh,20px); margin-bottom:.6vh;}
.topgrid {display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:.4vh .6vw;}
.topgrid .ev {margin:0;}
.hot-going-fast, .hot-almost-full {color:#f87171;}  /* event name in red = going fast / almost full */
.hot-sales-end-soon {color:#fbbf24;}                 /* event name in amber = sales end soon */
.foot .hot-going-fast, .foot .hot-sales-end-soon {font-weight:800;}
.evpanel h3 {font-size:clamp(15px,2.2vh,24px); margin-bottom:1vh;}
.evsub {font-size:clamp(12px,1.5vh,16px); margin:1.2vh 0 .6vh; padding:0; opacity:.85;}
.ev {display:flex; gap:.8vw; align-items:center; padding:.6vh .5vw; border-radius:1vh;
     background:rgba(255,255,255,.04); margin-bottom:.6vh;}
.ev.live {background:rgba(239,68,68,.15); border:1px solid #ef4444;}
.ev .date {min-width:4.2em; text-align:center; font-weight:800; font-size:clamp(12px,1.6vh,17px);
           border-left:4px solid; padding-left:.4vw; line-height:1.15;}
.ev .date small {display:block; font-size:.65em; opacity:.7; letter-spacing:.05em;}
.ev .what {min-width:0; line-height:1.25; flex:1; overflow:hidden; container-type:inline-size;}
/* Lines too long for the row scroll left to show the rest, pause, then start again from the beginning. 100cqw = the row's width,
   100% = the line's own width, so a line that already fits moves 0px and stays still. */
.ev .what > b, .ev .what > span {width:max-content; max-width:none !important; overflow:visible !important; text-overflow:clip !important;
     animation:evslide 12s linear infinite;}
@keyframes evslide {0%, 15% {transform:translateX(0);} 85%, 100% {transform:translateX(min(0px, calc(100cqw - 100%)));}}
.pr {font-style:normal; white-space:nowrap; font-size:clamp(10px,1.25vh,13px); font-weight:700;
     padding:.25vh .5vw; border-radius:999px; flex-shrink:0;}
.pr b {letter-spacing:-.05em;}
.pr.free {background:#14532d; color:#86efac;} .pr.low {background:#134e4a; color:#5eead4;}
.pr.mid {background:#713f12; color:#fcd34d;} .pr.high {background:#7f1d1d; color:#fca5a5;}
.ev .what b {display:block; font-size:clamp(12px,1.6vh,17px); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.ev .what span {display:block; font-size:clamp(10px,1.25vh,13px); opacity:.65; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
/* Outside page */
.outpanel {font-size:clamp(12px,1.6vh,18px); line-height:1.45;}
.outpanel .allclear {font-size:clamp(15px,2.2vh,24px); font-weight:700; color:#4ade80; margin:1vh 0;}
.outpanel .alert {border-left:5px solid; padding:.6vh .8vw; margin-bottom:1vh; background:#0b1220; border-radius:.6vh;}
.outpanel .alert .todo {margin-top:.4vh; color:#fcd34d;}
.outpanel .big {font-size:clamp(48px,10vh,120px); font-weight:800; line-height:1;}
.outpanel .big small {font-size:.3em; opacity:.7;}
.outpanel .lbl {font-size:clamp(15px,2.3vh,26px); font-weight:700; margin:.3vh 0 1vh;}
.outpanel .adv {margin-bottom:.8vh;}
.outpanel .bars {display:flex; align-items:flex-end; gap:2px; height:14vh; margin-top:2vh;}
.outpanel .bar {flex:1; height:100%; display:flex; flex-direction:column; justify-content:flex-end; position:relative;}
.outpanel .bar i {display:block; border-radius:2px 2px 0 0;}
.outpanel .bar span {position:absolute; bottom:-2.2vh; left:0; font-size:clamp(9px,1.1vh,12px); opacity:.6; white-space:nowrap;}
.outpanel .next {font-size:clamp(18px,3vh,34px); font-weight:800; color:#fbbf24; margin:.5vh 0 1.2vh;}
.outpanel .suns {display:flex; gap:2vw; font-size:clamp(20px,3.6vh,40px); font-weight:700; margin-bottom:1vh;}
.outpanel .suns small {display:block; font-size:.4em; font-weight:400; opacity:.65;}
.outpanel .uv {margin:1.2vh 0;}
.outpanel .later {opacity:.8; border-top:1px solid #334155; padding-top:1vh; margin-top:1vh;}
.foot {opacity:.45; font-size:clamp(9px,1.05vh,12px); text-align:center;}
/* page 5: world news */
.narpanel {border:1px solid rgba(57,135,229,.35);}
.narpanel h3 {font-size:clamp(14px,1.9vh,20px); margin-bottom:.6vh;}
.nargrid {display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:.6vh .6vw;}
.nar {display:flex; gap:.6vw; align-items:flex-start; background:rgba(255,255,255,.04); border-radius:1vh; padding:.6vh .6vw; min-width:0;}
.nar .nmain {flex:1; min-width:0;}
.nar .nhead {display:flex; align-items:center; gap:.5vw;}
.nar .rank {font-weight:800; opacity:.45; font-size:clamp(11px,1.4vh,15px);}
.nar .nlabel {flex:1; min-width:0; font-size:clamp(15px,2.2vh,24px); font-weight:800; line-height:1.15;
              white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.trend {font-style:normal; white-space:nowrap; font-size:clamp(10px,1.2vh,13px); font-weight:700; padding:.2vh .5vw; border-radius:999px;}
.trend.up {background:rgba(251,191,36,.16); color:#fcd34d;} .trend.new {background:rgba(244,63,94,.18); color:#fda4af;}
.trend.flat {background:rgba(148,163,184,.14); color:#cbd5e1;} .trend.down {background:rgba(148,163,184,.08); color:#94a3b8;}
.nmeta, .nctx {font-size:clamp(10px,1.2vh,13px); opacity:.85; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-top:.35vh;}
.nctx {opacity:.65;}
.rc {display:inline-block; font-style:normal; font-weight:800; font-size:.85em; padding:0 .25vw; border-radius:.4vh;
     margin-right:2px; background:rgba(255,255,255,.05); color:#475569;}
.rc.on {background:#1e3a8a; color:#e0e7ff;}
.spark {display:flex; align-items:flex-end; gap:2px; height:3vh; margin-top:.5vh;}
.spark i {flex:1; background:#3987e5; border-radius:2px 2px 0 0;}
.spark i.zero {background:rgba(255,255,255,.08);}
.sparkax {display:flex; justify-content:space-between; font-size:clamp(8px,.95vh,11px); opacity:.45; margin-top:.15vh;}
.nhl {font-size:clamp(10px,1.25vh,13px); line-height:1.25; margin-top:.35vh; display:-webkit-box; -webkit-line-clamp:2;
      -webkit-box-orient:vertical; overflow:hidden;}
.nhl span {opacity:.55;}
.newscol {height:calc(100vh - 50vh); overflow:hidden;
          -webkit-mask-image:linear-gradient(to bottom, #000 93%, transparent); mask-image:linear-gradient(to bottom, #000 93%, transparent);}
@media (max-height: 820px) {.newscol {height:calc(100vh - 53vh);}}  /* min font sizes make the top taller on 768p */
.newscol h3 {white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-bottom:.1vh;}
.newscol .srcs {font-size:clamp(9px,1.05vh,12px); opacity:.55; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-bottom:.5vh;}
.newscol .srcs s {opacity:.6;}
.tbar {display:flex; gap:2px; height:1vh; min-height:6px; margin-bottom:.4vh;}
.tbar i {border-radius:2px;}
.tcap {font-size:clamp(9px,1.1vh,12px); opacity:.8; margin-bottom:.6vh; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.sw {display:inline-block; width:.8em; height:.8em; border-radius:2px; vertical-align:-.05em; margin:0 3px 0 0;}
.hl {padding:.45vh .45vw; border-radius:.8vh; background:rgba(255,255,255,.04); margin-bottom:.45vh; line-height:1.25;}
.hl b {display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; font-size:clamp(11px,1.45vh,16px); font-weight:600;}
.hl span {display:block; font-size:clamp(9px,1.1vh,12px); opacity:.6; margin-top:.15vh; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.nref {font-style:normal; font-weight:800; color:#93c5fd; margin-right:.3vw;}
</style>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------ saved kiosk settings
# Kept in kiosk_settings.json (remembered after restarts). Every open copy of the board follows the file, and only
# a click on a control writes to it, so two open copies (kiosk window + another tab) can't overwrite each other.
_kiosk = load_kiosk_settings()
_rotate_saved = [p for p in _kiosk.get("rotate", list(PAGES)) if p in PAGES] or ["transit"]
_rotate_on_saved = _kiosk.get("rotate_on", True) is not False
_hold_saved = _kiosk.get("hold") if _kiosk.get("hold") in PAGES else "transit"
# widget key -> (file field, saved value). The top bar (rotate_on, rotate_pick, hold_pick) and the Settings panel
# (*_sb, inc_<page>) are two sets of controls for the same saved values, so they always agree.
SAVED = {
    "display_pick": ("display", "window" if _kiosk.get("display") == "window" else "fullscreen"),
    "rotate_on": ("rotate_on", _rotate_on_saved), "rotate_on_sb": ("rotate_on", _rotate_on_saved),
    "rotate_pick": ("rotate", _rotate_saved),
    "hold_pick": ("hold", _hold_saved), "hold_sb": ("hold", _hold_saved),
    **{f"inc_{p}": ("rotate", p in _rotate_saved) for p in PAGES},
}
for _key, (_field, _value) in SAVED.items():
    # first run, changed elsewhere (other control or another open copy), or the widget wasn't drawn last run
    if _key not in st.session_state or st.session_state.get("_seen_" + _key) != _value:
        st.session_state[_key] = st.session_state["_seen_" + _key] = _value


def save_setting(key, field):
    """on_change for the rotation / screen controls: write the new value to the file."""
    value = st.session_state.get(key)
    if key.startswith("inc_"):  # one page's toggle in Settings: add it to / take it out of the rotation
        saved = [p for p in load_kiosk_settings().get("rotate", list(PAGES)) if p in PAGES] or ["transit"]
        value = [p for p in PAGES if (p in saved and p != key[4:]) or (p == key[4:] and value)]
    elif key == "rotate_pick":
        value = [p for p in PAGES if p in (value or [])]  # header order
    if value in (None, []):  # nothing left to show: undo the click
        st.session_state[key] = st.session_state["_seen_" + key]
        return
    if field == "rotate_on" and not value:  # pausing: hold whatever page is on screen right now
        save_kiosk_settings(rotate_on=False, hold=st.session_state.get("_current_page", "transit"))
    else:
        save_kiosk_settings(**{field: value})
    st.session_state["_seen_" + key] = st.session_state[key]  # the other controls catch up on the rerun


# ------------------------------------------------------------------ sidebar (settings)
with st.sidebar:
    st.header("Settings")
    show_walk = st.toggle("🚶 Walking countdown", value=True,
                          help="Shows 'Leave in X min' and dims trains you can't reach on foot.")
    walk = {}
    if show_walk:  # walk-time boxes only appear while the countdown is on
        walk = {s["code"]: st.number_input(f"Walk to {s['name']} (min)", 1, 40, default_walk_minutes(s),
                                           key=f"walk_{s['code']}") for s in STATIONS}
    radius = st.select_slider("Bus stop radius", options=[300, 500, 800, 1200], value=800,
                              format_func=lambda m: f"{m} m")
    show_map = st.toggle("Show live map", value=True)
    show_routes = st.toggle("Show bus route lines", value=True)
    st.divider()
    st.subheader("📺 Pages")  # same settings as the buttons in the top bar
    st.toggle("⟳ Rotate pages", key="rotate_on_sb", on_change=save_setting, args=("rotate_on_sb", "rotate_on"),
              help="On: the pages switched on below take turns (Transit 60 s between each of the others, 30 s "
                   "each). Off: the board stays on one page.")
    if st.session_state["rotate_on_sb"]:
        st.caption("Pages in the rotation")
        for p, label in PAGES.items():
            st.toggle(label, key=f"inc_{p}", on_change=save_setting, args=(f"inc_{p}", "rotate"))
    else:
        st.radio("Page to show", list(PAGES), key="hold_sb", format_func=PAGES.get,
                 on_change=save_setting, args=("hold_sb", "hold"))
    st.divider()
    st.radio("Kiosk screen", ["fullscreen", "window"], key="display_pick", horizontal=True,
             on_change=save_setting, args=("display_pick", "display"),
             format_func={"fullscreen": "Full screen", "window": "Window"}.get,
             help="Window = a normal window you can move, resize, minimise or close, so you can use the PC. "
                  "Switches within a few seconds; Ctrl+Alt+K also toggles. A closed window stays closed: "
                  "press Ctrl+Alt+K or run toggle-kiosk.bat to reopen it.")
    st.divider()
    night_on = st.toggle("🌙 Night mode", value=True, help="Dims the screen automatically at night.")
    night_sched, night_start, night_end, night_style, night_preview = "Set hours", dtime(23), dtime(6), "Dim", False
    if night_on:  # these settings only appear while night mode is on
        night_sched = st.radio("Night mode runs", ["Set hours", "Sunset → sunrise"], horizontal=True)
        if night_sched == "Set hours":
            night_start = st.time_input("Starts at", dtime(23, 0), step=900)
            night_end = st.time_input("Ends at", dtime(6, 0), step=900)
        night_style = st.radio("Night look", ["Dim", "Clock only"], horizontal=True,
                               help="Dim = normal screen at low brightness. Clock only = near-black screen "
                                    "with a big clock and next trains (gently moves to prevent burn-in).")
        night_preview = st.toggle("Preview night mode now", value=False)
    st.caption("Key is read from .streamlit/secrets.toml (WMATA_API_KEY). "
               "Optional TOMTOM_API_KEY adds live traffic.")

tick = st_autorefresh(interval=REFRESH_SECONDS * 1000, key="tick") or 0
now = datetime.now(TZ)
# Rotate on: one entry per 30 s tick, e.g. Transit + Events + News = transit, transit, events, transit, transit, news
# Rotate off: stay on the page picked in the header.
rotation = [p for p in PAGES if p in (st.session_state["rotate_pick"] or [])] or ["transit"]
others = [p for p in rotation if p != "transit"]
if "transit" in rotation:
    cycle = [step for p in others for step in ("transit", "transit", p)] or ["transit"]
else:
    cycle = others
if st.session_state["rotate_on"]:
    page = cycle[tick % len(cycle)]
else:
    page = st.session_state["hold_pick"] or "transit"
if st.query_params.get("page") in PAGES:
    page = st.query_params["page"]  # e.g. http://localhost:8502/?page=news pins one page
st.session_state["_current_page"] = page


# Load the next pages' data in the background, so a page whose data has expired (events: hourly) is ready when
# its turn comes instead of keeping the previous page on screen while it loads.
def _prefetch(p):
    if p == "events":
        for _, sport, team_id in DC_TEAMS:
            safe(team_games, sport, team_id)
        for path in ("music--events/this-week", "concerts", "embassy", "run-club", "trivia", "comedy", "board-games",
                     "all-events", "events--this-week", "events--next-week"):
            safe(eventbrite_events, path)
        safe(smithsonian_events)
        if TICKETMASTER_KEY:
            safe(ticketmaster_music, TICKETMASTER_KEY)
    elif p == "flights":
        safe(us_flight_deals)
        safe(world_flight_deals)
    elif p == "business":
        safe(us_flight_deals)
        safe(world_flight_deals, "https://www.fly4free.com/flight-deals/business-class/feed/")
    elif p == "news":
        safe(news_fetch)
    elif p == "outside":
        safe(nws_alerts)
        safe(air_quality)
        safe(sun_days)


@st.cache_resource
def _prefetch_running():
    return {}


if st.session_state["rotate_on"] and len(cycle) > 1:
    running = _prefetch_running()
    for upcoming in {cycle[(tick + k) % len(cycle)] for k in (1, 2)} - {"transit", page}:
        if not (running.get(upcoming) and running[upcoming].is_alive()):
            running[upcoming] = threading.Thread(target=_prefetch, args=(upcoming,), daemon=True)
            running[upcoming].start()

# ------------------------------------------------------------------ header + weather
wx = safe(weather)


# ------------------------------------------------------------------ night mode (time-based)
def is_night():
    if not night_on:
        return False
    if night_preview:
        return True
    clock_now = now.time()
    if night_sched == "Sunset → sunrise":
        try:
            daily = wx["daily"]
            sunrise = datetime.fromisoformat(daily["sunrise"][0]).time()
            sunset = datetime.fromisoformat(daily["sunset"][0]).time()
            return clock_now >= sunset or clock_now < sunrise
        except Exception:
            pass  # no weather data: fall back to the set hours
    if night_start <= night_end:  # e.g. 01:00-05:00
        return night_start <= clock_now < night_end
    return clock_now >= night_start or clock_now < night_end  # wraps midnight, e.g. 23:00-06:00


NIGHT = is_night()
if NIGHT and night_style == "Clock only":
    # near-black screen: big dim clock + next trains; nudged a little each minute to avoid screen burn-in
    shift = now.minute % 6
    trains = safe(rail_predictions, ",".join(s["code"] for s in STATIONS), default=[]) or [] if API_KEY else []
    rows = []
    for s in STATIONS:
        for idx, label in enumerate(s["directions"]):
            etas = sorted(train_eta(t) for t in trains
                          if t.get("LocationCode") == s["code"] and train_eta(t) is not None
                          and t.get("DestinationName") not in ("No Passenger", "Train", "")
                          and s["direction_fn"](str(t.get("DestinationCode") or ""),
                                                str(t.get("DestinationName") or "").lower()) == idx)[:3]
            rows.append(f'<div><b>{esc(s["name"].replace("-GWU", ""))}</b> {esc(label.lower())}: '
                        f'{", ".join(f"{m} min" for m in etas) if etas else "no trains"}</div>')
    temp = f'{round(wx["current"]["temperature_2m"])}°' if wx and wx.get("current") else ""
    show(f"""
    <style>
      [data-testid="stApp"], [data-testid="stMain"] {{background:#000 !important;}}
      .night {{color:#64748b; text-align:center; padding-top:calc(22vh + {shift}vh); padding-left:{shift * 2}vw;}}
      .night .nclock {{font-size:22vh; font-weight:800; line-height:1; color:#94a3b8;}}
      .night .nclock span {{font-size:.3em;}}
      .night .ndate {{font-size:3vh; margin:1vh 0 5vh;}}
      .night .ntrains {{font-size:2.3vh; line-height:1.8;}}
    </style>
    <div class="night">
      <div class="nclock">{now.strftime("%I:%M").lstrip("0")}<span> {now.strftime("%p")}</span></div>
      <div class="ndate">{now.strftime("%A, %B")} {now.day} · {temp}</div>
      <div class="ntrains">{"".join(rows)}</div>
    </div>""")
    st.stop()
if NIGHT:  # "Dim": same screen (and rotation) at low brightness; switch Rotate off to keep Transit at night
    show("<style>[data-testid='stMain'] {filter: brightness(.35) saturate(.6);}</style>")

wx_html = '<div class="wx"><div class="meta">Weather unavailable</div></div>'
if wx and wx.get("current"):
    cur, hourly = wx["current"], wx.get("hourly", {})
    times = hourly.get("time", [])
    start = next((i for i, t in enumerate(times) if t[:13] >= cur["time"][:13]), 0)
    pops = [p or 0 for p in hourly.get("precipitation_probability", [])[start:start + 6]]
    peak = max(pops) if pops else 0
    if (cur.get("precipitation") or 0) > 0:
        rain, rain_cls = "Raining now", "rain-hi"
    elif peak >= 60:
        rain, rain_cls = f"Rain likely · {peak}% next 6h", "rain-hi"
    elif peak >= 30:
        rain, rain_cls = f"Rain possible · {peak}% next 6h", "rain-mid"
    else:
        rain, rain_cls = f"Dry · {peak}% rain next 6h", "rain-lo"
    strip = "".join(
        f"<div>{datetime.fromisoformat(t).strftime('%-I%p' if os.name != 'nt' else '%#I%p').lower()}"
        f"<br>{weather_icon(c)}<br>{round(tmp)}°</div>"
        for t, c, tmp in zip(times[start:start + 6], hourly.get("weather_code", [])[start:start + 6],
                             hourly.get("temperature_2m", [])[start:start + 6]))
    # next 2 days (index 0 is today): day name, icon + rain chance, high/low
    daily, days = wx.get("daily", {}), []
    for i in (1, 2):
        try:
            pop = (daily.get("precipitation_probability_max") or [])[i] or 0
            days.append(
                f"<div><b>{datetime.fromisoformat(daily['time'][i]).strftime('%a')}</b>"
                f"<br>{weather_icon(daily['weather_code'][i])}{f' {pop}%' if pop >= 20 else ''}"
                f"<br>{round(daily['temperature_2m_max'][i])}°/{round(daily['temperature_2m_min'][i])}°</div>")
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    days_html = f'<div class="strip days">{"".join(days)}</div>' if days else ""
    wx_html = f"""
    <div class="wx">
      <div class="now">{weather_icon(cur.get('weather_code'))} {round(cur['temperature_2m'])}°</div>
      <div class="meta">Feels {round(cur['apparent_temperature'])}° · wind {round(cur['wind_speed_10m'])} mph
        <br><span class="{rain_cls}">{esc(rain)}</span></div>
      <div class="strip">{strip}</div>
      {days_html}
    </div>"""

clock = now.strftime("%I:%M").lstrip("0")
with st.container(key="topbar"):
    c_head, c_ctrl, c_clock = st.columns([4.9, 5.3, 1.0], vertical_alignment="center")
    with c_head:
        show(f"""
        <div class="top">
          <div><h1>🚇 DC Transit Hub</h1><div class="sub">{esc(HOME['label'])} · {now.strftime('%A, %B %d')}</div></div>
          {wx_html}
        </div>""")
    with c_ctrl:
        # Rotate on: the page buttons choose which pages take turns (lit = in the rotation).
        # Rotate off: the board stays on one page; click a button to show it.
        c_toggle, c_pages = st.columns([1.35, 4.2], vertical_alignment="center")
        with c_toggle:
            st.toggle("Rotate", key="rotate_on", on_change=save_setting, args=("rotate_on", "rotate_on"))
        with c_pages:
            if st.session_state["rotate_on"]:
                st.pills("Pages to rotate", list(PAGES), key="rotate_pick", selection_mode="multi",
                         format_func=PAGES.get, on_change=save_setting, args=("rotate_pick", "rotate"),
                         label_visibility="collapsed")
                lit = [p for p in PAGES if p in (st.session_state["rotate_pick"] or [])]
                if len(lit) <= 1:
                    hint = f'Only <b>{PAGES[lit[0] if lit else "transit"]}</b> is lit, so it stays on screen · light up more to rotate'
                else:
                    hint = f'Showing <b>{PAGES[page]}</b> · {len(lit)} lit pages take turns · click to add or remove'
                show(f'<div class="nowshow">{hint}</div>')
            else:
                st.segmented_control("Page to show", list(PAGES), key="hold_pick", format_func=PAGES.get,
                                     on_change=save_setting, args=("hold_pick", "hold"),
                                     label_visibility="collapsed")
                show('<div class="nowshow">Rotation paused · click a page to show it</div>')
    with c_clock:
        show(f'<div class="clock">{clock}<span> {now.strftime("%p")}</span></div>')


# ------------------------------------------------------------------ page 2: DC events
TODAY_ISO = now.date().isoformat()
TWO_WEEKS_ISO = now.date().fromordinal(now.date().toordinal() + 14).isoformat()


def in_window(events):
    """Only events from today through the next 14 days."""
    return [e for e in events if TODAY_ISO <= e["date"] <= TWO_WEEKS_ISO]


def event_card(e, tag_color, extra_cls="", qr=False):
    d = datetime.fromisoformat(e["date"])
    day = "TODAY" if e["date"] == TODAY_ISO else d.strftime("%a").upper()
    # Eventbrite's urgency badge colours the name instead of taking up the details line (key in the page footer)
    hot = e.get("hot", "")
    hot_attr = f' class="hot-{hot}" title="{esc(HOT_LABELS[hot][0])}"' if hot in HOT_LABELS else ""
    details = esc(" · ".join(x for x in (e.get("time", ""), e.get("venue", "")) if x))
    return (f'<div class="ev {extra_cls}"><div class="date" style="border-color:{tag_color}"><small>{day}</small>'
            f'{d.strftime("%b")} {d.day}</div><div class="what"><b{hot_attr}>{esc(e["title"])}</b>'
            f'<span>{details}</span></div>{price_chip(e)}{qr_img(e.get("url")) if qr else ""}</div>')


def price_chip(e):
    """Free / $ (under $20) / $$ ($20-60) / $$$ (over $60), with the actual range."""
    price = e.get("price")
    if not price:
        return ""
    low, high = price
    if high == 0:
        return '<i class="pr free">Free</i>'
    tier = "low" if low < 20 else "mid" if low <= 60 else "high"
    signs = {"low": "$", "mid": "$$", "high": "$$$"}[tier]
    start = "Free" if low == 0 else f"${low:.0f}"
    rng = start + (f"–${high:.0f}" if low == 0 else f"–{high:.0f}" if high > low else "")
    return f'<i class="pr {tier}"><b>{signs}</b> {rng}</i>'


def event_rows(events, tag_color, limit):
    # within 2 weeks; Eventbrite's hot events ("going fast", "almost full") float up, then by date
    events = sorted(in_window(events), key=lambda e: (hot_score(e), e["date"]))[:limit]
    events.sort(key=lambda e: (hot_score(e) > 0, e["date"]))
    ensure_prices(events)
    return "".join(event_card(e, tag_color) for e in events) or '<div class="empty">Nothing listed in the next 2 weeks</div>'


if page == "events":
    sports_rows = []
    for label, sport, team_id in DC_TEAMS:
        for g in (safe(team_games, sport, team_id, default=[]) or [])[:4]:
            when = datetime.fromisoformat(g["when"].replace("Z", "+00:00")).astimezone(TZ)
            if (when.date() - now.date()).days > 14:
                continue
            sports_rows.append((when, label, g))
    sports_rows.sort(key=lambda x: (not x[2]["live"], x[0]))
    sport_html = "".join(
        f'<div class="ev{" live" if g["live"] else ""}"><div class="date" style="border-color:#ef4444">'
        f'<small>{"LIVE" if g["live"] else ("TODAY" if when.date() == now.date() else when.strftime("%a").upper())}</small>'
        f'{when.strftime("%b")} {when.day}</div><div class="what"><b>{esc(label)}: {esc(g["name"])}</b>'
        f'<span>{esc(g["score"]) + " · " if g["score"] else ""}{when.strftime("%I:%M %p").lstrip("0")} · '
        f'{"🏠 " if g["home"] else "✈ "}{esc(g["venue"])}{" · 📺 " + esc(g["tv"]) if g["tv"] else ""}</span></div></div>'
        for when, label, g in sports_rows[:15]) or '<div class="empty">No DC games in the next two weeks</div>'

    music = []
    if TICKETMASTER_KEY:
        music += safe(ticketmaster_music, TICKETMASTER_KEY, default=[]) or []
    for path in ("music--events/this-week", "concerts"):
        music += safe(eventbrite_events, path, default=[]) or []
    music_unique = sorted({e["title"].lower(): e for e in reversed(music)}.values(), key=lambda e: e["date"])
    embassy_unique = sorted({e["title"].lower(): e for e in reversed(
        [shorten_embassy(x) for x in safe(eventbrite_events, "embassy", default=[]) or []])}.values(),
        key=lambda e: e["date"])
    def still_on(e):
        """Drop today's Smithsonian events whose end time ('10 am – 5 pm') has already passed."""
        if e["date"] != now.date().isoformat():
            return True
        end = re.search(r"[–-]\s*(\d{1,2})(?::(\d\d))?\s*(am|pm)", e.get("time", ""))
        if not end:
            return True
        hour = int(end.group(1)) % 12 + (12 if end.group(3) == "pm" else 0)
        return (hour, int(end.group(2) or 0)) > (now.hour, now.minute)

    two_days = now.date().fromordinal(now.date().toordinal() + 2).isoformat()
    smithsonian = [e for e in (safe(smithsonian_events, default=[]) or []) if e["date"] <= two_days and still_on(e)]

    def unique(events):
        seen, out = set(), []
        for e in sorted(events, key=lambda e: e["date"]):
            if e["title"].lower() not in seen:
                seen.add(e["title"].lower())
                out.append(e)
        return out

    music_unique, embassy_unique = in_window(music_unique), in_window(embassy_unique)
    run_clubs = unique(safe(eventbrite_events, "run-club", default=[]) or [])
    fun = unique([e for path in ("trivia", "comedy", "board-games")
                  for e in (safe(eventbrite_events, path, default=[]) or [])])

    # Top on Eventbrite: DC's hottest events by Eventbrite's own badges, then its relevance ranking
    top_pool = {}
    for path in ("all-events", "events--this-week", "events--next-week"):
        for e in safe(eventbrite_events, path, default=[]) or []:
            top_pool.setdefault(e["title"].lower(), e)
    top = ensure_prices(sorted(in_window(top_pool.values()), key=lambda e: (hot_score(e), e["rank"]))[:8])
    if top:
        show(f'<div class="panel toppanel"><h3>🔥 Top on Eventbrite · next 2 weeks'
             f'<span class="scanhint">📱 scan a code to open the event</span></h3><div class="topgrid">'
             f'{"".join(event_card(e, "#f43f5e", qr=True) for e in top)}</div></div>')

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        show(f'<div class="panel evpanel below-top"><h3>🏟️ DC Sports</h3>{sport_html}</div>')
    with c2:
        show(f'<div class="panel evpanel below-top"><h3>🎵 Music & Concerts</h3>{event_rows(music_unique, "#a78bfa", 15)}</div>')
    with c3:
        show(f'<div class="panel evpanel below-top"><h3>🌍 Embassies & Culture</h3>'
             f'{event_rows(embassy_unique, "#22d3ee", 7)}'
             f'<h4 class="evsub">🏛️ Smithsonian · next 2 days</h4>{event_rows(smithsonian, "#facc15", 6)}</div>')
    with c4:
        show(f'<div class="panel evpanel below-top"><h3>🏃 Run Clubs & Fun</h3>'
             f'{event_rows(run_clubs, "#34d399", 6)}'
             f'<h4 class="evsub">🎲 Trivia · comedy · game nights</h4>{event_rows(fun, "#fb923c", 7)}</div>')
    show(f'<div class="foot">Name in <span class="hot-going-fast">red</span> = going fast / almost full · '
         f'<span class="hot-sales-end-soon">amber</span> = sales end soon · '
         f'Prices: <i class="pr free">Free</i> <i class="pr low">$ under $20</i> '
         f'<i class="pr mid">$$ $20–60</i> <i class="pr high">$$$ over $60</i> · '
         f'Events: ESPN, Eventbrite, Smithsonian{", Ticketmaster" if TICKETMASTER_KEY else ""}</div>')
    st.stop()


# ------------------------------------------------------------------ page 3: flight deals
def ago(posted):
    if not posted:
        return ""
    hours = (now - posted).total_seconds() / 3600
    return "just posted" if hours < 1 else f"{int(hours)}h ago" if hours < 24 else f"{int(hours // 24)}d ago"


def deal_card(price, headline, sub, link=""):
    return (f'<div class="deal"><div class="price">{esc(price) or "Deal"}</div>'
            f'<div class="what"><b>{esc(headline)}</b><span>{esc(sub)}</span></div>{qr_img(link)}</div>')


if page == "flights":
    us = safe(us_flight_deals, default=[]) or []
    world = safe(world_flight_deals, default=[]) or []
    fresh = lambda d: d["posted"] is None or (now - d["posted"]).days <= 14
    us = [d for d in us if fresh(d)]
    world = [d for d in world if fresh(d)]
    from_dc = [d for d in us if any(w in f'{d["origin"]} {d["dest"]}'.lower() for w in DC_AIRPORT_WORDS)]
    rest_us = [d for d in us if d not in from_dc]

    def us_card(d, show_origin=True):
        arrow = "⇄" if d["both_ways"] else "→"
        head = f'{d["origin"]} {arrow} {d["dest"]}' if show_origin else f'→ {d["dest"]}'
        sub = " · ".join(x for x in (d["airline"], d["cabin"], "roundtrip", ago(d["posted"])) if x)
        return deal_card(d["price"], head, sub, d["link"])

    dc_html = "".join(us_card(d) for d in from_dc[:12]) or \
        '<div class="empty">No DC deals posted in the last 2 weeks. Nearby: check the US list →</div>'
    us_html = "".join(us_card(d) for d in rest_us[:12]) or '<div class="empty">No US deals right now</div>'
    world_html = "".join(
        deal_card(d["price"], f'{d["origin"]} → {d["dest"]}' if d["origin"] else d["title"],
                  (d["title"] if d["origin"] else "") + (" · " if d["origin"] else "") + ago(d["posted"]), d["link"])
        for d in world[:12]) or '<div class="empty">No worldwide deals right now</div>'

    c1, c2, c3 = st.columns(3)
    with c1:
        show(f'<div class="panel evpanel dealpanel"><h3>✈️ From DC · DCA / IAD / BWI</h3>{dc_html}</div>')
    with c2:
        show(f'<div class="panel evpanel dealpanel"><h3>🗽 Across the US</h3>{us_html}</div>')
    with c3:
        show(f'<div class="panel evpanel dealpanel"><h3>🌍 Worldwide</h3>{world_html}</div>')
    show('<div class="foot">Fares from The Flight Deal (US, roundtrip incl. taxes) and Fly4free (worldwide, mostly '
         'from Europe) · posted in the last 2 weeks · deals sell out fast, check before booking</div>')
    st.stop()


# ------------------------------------------------------------------ page 4: business class deals
if page == "business":
    fresh = lambda d: d["posted"] is not None and (now - d["posted"]).days <= 90  # business deals are rarer
    biz = [d for d in (safe(world_flight_deals, "https://www.fly4free.com/flight-deals/business-class/feed/",
                            default=[]) or []) if fresh(d)]
    us_biz = [d for d in (safe(us_flight_deals, default=[]) or [])
              if fresh(d) and re.search(r"business|first|premium|lie-?flat|polaris|delta one", d["title"], re.I)]
    # departs from North America? (judge by the origin, not the destination)
    is_na = lambda d: any(w in (d["origin"] or d["title"]).lower() for w in NORTH_AMERICA)

    def biz_card(d):
        head = f'{d["origin"]} → {d["dest"]}' if d.get("origin") and d.get("dest") else d["title"]
        sub = d["title"] if head != d["title"] else ""
        return deal_card(d["price"], head, " · ".join(x for x in (sub, ago(d["posted"])) if x), d["link"])

    def us_biz_card(d):
        arrow = "⇄" if d["both_ways"] else "→"
        return deal_card(d["price"], f'{d["origin"]} {arrow} {d["dest"]}',
                         " · ".join(x for x in (d["airline"], d["cabin"] or "Premium cabin", ago(d["posted"])) if x),
                         d["link"])

    na_html = "".join([us_biz_card(d) for d in us_biz] + [biz_card(d) for d in biz if is_na(d)][:12]) or \
        ('<div class="empty">No business-class deals from North America in the last 90 days. '
         'The best ones usually start in Europe → see worldwide.</div>')
    world_html = "".join(biz_card(d) for d in [d for d in biz if not is_na(d)][:14]) or \
        '<div class="empty">No business-class deals posted in the last 90 days</div>'

    c1, c2 = st.columns([1, 2])
    with c1:
        show(f'<div class="panel evpanel dealpanel"><h3>💺 From North America</h3>{na_html}</div>')
    with c2:
        show(f'<div class="panel evpanel dealpanel"><h3>🥂 Worldwide business class · lie-flat deals</h3>{world_html}</div>')
    show('<div class="foot">Business &amp; premium fares from Fly4free (business-class deals) and The Flight Deal · '
         'last 90 days · roundtrip unless noted · check availability before booking</div>')
    st.stop()


# ------------------------------------------------------------------ page 6: outside (alerts, air, sun)
AQI_BANDS = [(50, "Good", "#22c55e"), (100, "Moderate", "#eab308"), (150, "Unhealthy for sensitive groups", "#f97316"),
             (200, "Unhealthy", "#ef4444"), (300, "Very unhealthy", "#a855f7"), (10 ** 6, "Hazardous", "#9f1239")]
UV_BANDS = [(2.5, "Low", "#22c55e"), (5.5, "Moderate", "#eab308"), (7.5, "High", "#f97316"),
            (10.5, "Very high", "#ef4444"), (99, "Extreme", "#a855f7")]
AQI_ADVICE = {"Good": "Great day to be outside.",
              "Moderate": "Fine for most people; unusually sensitive people may want to take it easier.",
              "Unhealthy for sensitive groups": "Asthma, heart conditions, kids and older adults: limit long outdoor effort.",
              "Unhealthy": "Everyone: cut back long or hard outdoor activity.",
              "Very unhealthy": "Avoid long outdoor activity; consider staying inside.",
              "Hazardous": "Stay inside and keep windows closed."}


def band(value, bands):
    return next((label, color) for top, label, color in bands if value <= top)


def hm(seconds):
    seconds = int(abs(seconds))
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def clock12(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


if page == "outside":
    # --- weather alerts
    alerts = safe(nws_alerts)
    if alerts is None:
        alert_html = '<div class="empty">The National Weather Service did not respond; trying again in 5 minutes.</div>'
    elif not alerts:
        alert_html = ('<div class="allclear">✅ No active watches, warnings or advisories</div>'
                      '<div class="hint">Storm, heat, cold, flood, wind and air-quality alerts from the National '
                      'Weather Service for this address show up here as soon as they are issued.</div>')
    else:
        sev_color = {"Extreme": "#ef4444", "Severe": "#ef4444", "Moderate": "#f59e0b"}
        cards = []
        for a in sorted(alerts, key=lambda a: ["Extreme", "Severe", "Moderate", "Minor"].index(a.get("severity"))
                        if a.get("severity") in ("Extreme", "Severe", "Moderate", "Minor") else 9)[:4]:
            until = a.get("ends") or a.get("expires")
            try:
                until = "until " + datetime.fromisoformat(until).astimezone(TZ).strftime("%a %I:%M %p").replace(" 0", " ")
            except Exception:
                until = ""
            desc = " ".join((a.get("description") or "").split())
            what = " ".join((a.get("instruction") or "").split())
            cards.append(
                f'<div class="alert" style="border-color:{sev_color.get(a.get("severity"), "#3b82f6")}">'
                f'<b>⚠️ {esc(a.get("event", "Alert"))}</b> <span class="hint">{esc(until)}</span>'
                f'<div>{esc(desc[:380])}{"…" if len(desc) > 380 else ""}</div>'
                + (f'<div class="todo">{esc(what[:220])}{"…" if len(what) > 220 else ""}</div>' if what else "")
                + '</div>')
        alert_html = "".join(cards)

    # --- air quality
    aq = safe(air_quality)
    air_html = '<div class="empty">Air-quality data unavailable right now.</div>'
    if aq and aq.get("current", {}).get("us_aqi") is not None:
        c = aq["current"]
        label, color = band(c["us_aqi"], AQI_BANDS)
        parts = {"Fine particles (PM2.5)": c.get("us_aqi_pm2_5"), "Ozone": c.get("us_aqi_ozone"),
                 "Nitrogen dioxide": c.get("us_aqi_nitrogen_dioxide")}
        main = max((k for k in parts if parts[k] is not None), key=lambda k: parts[k], default="")
        times, vals = aq["hourly"]["time"], aq["hourly"]["us_aqi"]
        start = next((i for i, t in enumerate(times) if t[:13] >= now.strftime("%Y-%m-%dT%H")), 0)
        nxt = [(t, v) for t, v in zip(times[start:start + 24], vals[start:start + 24]) if v is not None]
        top = max((v for _, v in nxt), default=1) or 1
        bars = "".join(
            f'<div class="bar" title="{v}"><i style="height:{max(6, v / max(top, 50) * 100):.0f}%;'
            f'background:{band(v, AQI_BANDS)[1]}"></i>'
            f'<span>{datetime.fromisoformat(t).strftime("%#I%p" if os.name == "nt" else "%-I%p").lower() if k % 4 == 0 else ""}</span></div>'
            for k, (t, v) in enumerate(nxt))
        worst = max(nxt, key=lambda x: x[1]) if nxt else None
        air_html = (
            f'<div class="big" style="color:{color}">{c["us_aqi"]}<small> AQI</small></div>'
            f'<div class="lbl" style="color:{color}">{esc(label)}</div>'
            f'<div class="adv">{esc(AQI_ADVICE[label])}</div>'
            f'<div class="hint">Main pollutant: {esc(main)}'
            + (f' · next 24h peak {worst[1]} ({band(worst[1], AQI_BANDS)[0].lower()}) around '
               f'{datetime.fromisoformat(worst[0]).strftime("%#I %p" if os.name == "nt" else "%-I %p")}' if worst else "")
            + f'</div><div class="bars">{bars}</div>')

    # --- sun + UV
    sd = safe(sun_days)
    sun_html = '<div class="empty">Sunrise / sunset data unavailable right now.</div>'
    if sd and len(sd.get("time", [])) >= 3:
        at = lambda s: datetime.fromisoformat(s).replace(tzinfo=TZ)
        rise, sset = at(sd["sunrise"][1]), at(sd["sunset"][1])
        day_len, change = sd["daylight_duration"][1], sd["daylight_duration"][1] - sd["daylight_duration"][0]
        if now < rise:
            nxt_txt = f"☀️ Sunrise in {hm((rise - now).total_seconds())}"
        elif now < sset:
            nxt_txt = f"🌇 Sunset in {hm((sset - now).total_seconds())}"
        else:
            nxt_txt = f"☀️ Sunrise in {hm((at(sd['sunrise'][2]) - now).total_seconds())}"
        golden = sset - timedelta(hours=1)
        uv = sd["uv_index_max"][1] or 0
        uv_label, uv_color = band(uv, UV_BANDS)
        later = "".join(
            f'<div><b>{datetime.fromisoformat(sd["time"][i]).strftime("%A")}</b> '
            f'{clock12(at(sd["sunrise"][i]))} – {clock12(at(sd["sunset"][i]))}'
            f' · UV {round(sd["uv_index_max"][i] or 0)}</div>'
            for i in range(2, len(sd["time"])))
        sun_html = (
            f'<div class="next">{nxt_txt}</div>'
            f'<div class="suns"><div><small>Sunrise</small>{clock12(rise)}</div>'
            f'<div><small>Sunset</small>{clock12(sset)}</div></div>'
            f'<div class="adv">{hm(day_len)} of daylight · {"+" if change >= 0 else "−"}'
            f'{int(abs(change)) // 60}m {int(abs(change)) % 60}s vs yesterday</div>'
            f'<div class="hint">📸 Golden hour {clock12(golden)} – {clock12(sset)}</div>'
            f'<div class="uv">Peak UV today <b style="color:{uv_color}">{uv:.0f} · {uv_label}</b>'
            f'{" · sunscreen if out midday" if uv >= 3 else ""}</div>'
            f'<div class="later">{later}</div>')

    c1, c2, c3 = st.columns([1.25, 1, 1])
    with c1:
        show(f'<div class="panel evpanel outpanel"><h3>🚨 Weather alerts</h3>{alert_html}</div>')
    with c2:
        show(f'<div class="panel evpanel outpanel"><h3>🌬️ Air quality</h3>{air_html}</div>')
    with c3:
        show(f'<div class="panel evpanel outpanel"><h3>🌅 Sun &amp; daylight</h3>{sun_html}</div>')
    show('<div class="foot">Alerts: National Weather Service (every 5 min) · Air quality: Open-Meteo / CAMS, '
         'US AQI (every 30 min) · Sun &amp; UV: Open-Meteo</div>')
    st.stop()


# ------------------------------------------------------------------ page 5: world news (global narratives)
def nesc(text):
    """Escape for HTML, and stop '$' starting a maths formula in Streamlit's markdown."""
    return esc(text).replace("$", "&#36;")


if page == "news":
    rows, down, now_utc = news_items()
    if not rows:
        show('<div class="panel"><div class="empty">No headlines yet: the news feeds did not respond. '
             'They are tried again every 15 minutes.</div></div>')
        st.stop()
    info = analyze_news(rows, now_utc)
    nars, themes = info["narratives"], info["themes"]
    nar_of = {}  # headline -> number of the first narrative it belongs to
    for k, n in enumerate(nars, 1):
        for i in n["ids"]:
            nar_of.setdefault(i, k)

    def nar_card(k, n):
        regs = "".join(f'<i class="rc{" on" if code in n["regions"] else ""}" title="{name}">{code}</i>'
                       for code, name, _ in NEWS_REGIONS)
        peak = max(n["spark"]) or 1
        bars = "".join(f'<i class="{"" if v else "zero"}" style="height:{max(v / peak * 100, 6):.0f}%" '
                       f'title="{(11 - b) * 4}–{(12 - b) * 4} h ago: {v} {"story" if v == 1 else "stories"}"></i>'
                       for b, v in enumerate(n["spark"]))
        rep = rows[n["rep"]]
        ctx = "with " + nesc(", ".join(n["ctx"])) if n["ctx"] else "&nbsp;"
        return (f'<div class="nar"><div class="nmain"><div class="nhead"><span class="rank">{k}</span>'
                f'<b class="nlabel">{nesc(n["label"])}</b><i class="trend {n["trend"][0]}">{n["trend"][1]}</i></div>'
                f'<div class="nmeta">{regs} {len(n["sources"])} outlets · {len(n["ids"])} stories</div>'
                f'<div class="nctx">{ctx}</div>'
                f'<div class="spark">{bars}</div><div class="sparkax"><span>48 h ago</span><span>now</span></div>'
                f'<div class="nhl">{nesc(rep["title"])} <span>· {nesc(rep["source"])} · {ago(rep["posted"])}</span></div>'
                f'</div>{qr_img(rep["link"])}</div>')

    if nars:
        show(f'<div class="panel narpanel"><h3>🌐 Global narratives · what several regions are covering at once'
             f'<span class="scanhint">📱 scan a code to read the latest story</span></h3>'
             f'<div class="nargrid">{"".join(nar_card(k, n) for k, n in enumerate(nars, 1))}</div></div>')
    else:
        show('<div class="panel narpanel"><h3>🌐 Global narratives</h3><div class="empty">No story is being covered '
             'across regions yet. This fills in as headlines come in.</div></div>')

    cols = st.columns(len(NEWS_REGIONS))
    for col, (code, name, outlets) in zip(cols, NEWS_REGIONS):
        ids = [i for i, r in enumerate(rows) if r["region"] == code]
        day = [i for i in ids if (now_utc - rows[i]["posted"]).total_seconds() < 86400]
        # theme mix of this region's last 48 h
        mix = Counter(themes[i] for i in ids)
        total = sum(mix.values()) or 1
        bar = "".join(f'<i style="flex:{mix[t]};background:{color}" title="{tname} {mix[t] * 100 / total:.0f}%"></i>'
                      for t, (tname, color, _) in enumerate(NEWS_THEMES) if mix[t])
        lead = [t for t, _ in mix.most_common() if t < len(NEWS_THEMES) - 1][:2]
        caption = " &nbsp; ".join(f'<i class="sw" style="background:{NEWS_THEMES[t][1]}"></i>'
                                  f'{NEWS_THEMES[t][0]} {mix[t] * 100 / total:.0f}%' for t in lead)
        # newest first, taking turns between outlets so one busy feed can't fill the column
        by_src = {}
        for i in sorted(day if len(day) >= 6 else ids, key=lambda i: rows[i]["posted"], reverse=True):
            by_src.setdefault(rows[i]["source"], []).append(i)
        pick = []
        while len(pick) < 10 and any(by_src.values()):
            for lst in by_src.values():
                if lst and len(pick) < 10:
                    pick.append(lst.pop(0))
        pick.sort(key=lambda i: rows[i]["posted"], reverse=True)
        heads = "".join(
            f'<div class="hl"><b>{nesc(rows[i]["title"])}</b><span>'
            f'{f"<em class=nref>#{nar_of[i]}</em>" if i in nar_of else ""}{nesc(rows[i]["source"])} · {ago(rows[i]["posted"])}'
            f'</span></div>' for i in pick) or '<div class="empty">No headlines from this region right now</div>'
        srcs = " · ".join(f"<s>{nesc(o)}</s>" if o in down else nesc(o) for o, _ in outlets)
        with col:
            show(f'<div class="panel newscol"><h3><i class="rc on">{code}</i> {name}</h3><div class="srcs">{srcs}</div>'
                 f'<div class="tbar">{bar}</div><div class="tcap">{caption or "&nbsp;"}</div>{heads}</div>')

    legend = " ".join(f'<i class="sw" style="background:{color}"></i>{tname}' for tname, color, _ in NEWS_THEMES)
    off = f" · not responding: {nesc(', '.join(down))}" if down else ""
    show(f'<div class="foot">Themes: {legend} · {len(rows)} headlines from the last 48 h · narrative = a name in 3+ '
         f'outlets across 2+ regions · ▲ rising = a bigger share of the last 6 h of headlines than of the 42 h before · #n = '
         f'part of narrative n{off}</div>')
    st.stop()

if not API_KEY:
    st.error("No WMATA API key found. Add WMATA_API_KEY to Transit Hub/.streamlit/secrets.toml, then refresh.")
    st.stop()

# ------------------------------------------------------------------ alerts + outages
our_lines = {line for s in STATIONS for line in s["lines"]}
for incident in safe(rail_incidents, default=[]):
    lines = {l.strip() for l in str(incident.get("LinesAffected", "")).split(";") if l.strip()}
    if lines & our_lines:
        show(f'<div class="alert">⚠ {" ".join(badge(l) for l in sorted(lines & our_lines))} '
                    f'{esc(incident.get("Description", ""))}</div>', unsafe_allow_html=True)

outages = []
for s in STATIONS:
    for unit in safe(elevator_outages, s["code"], default=[]):
        kind = str(unit.get("UnitType", "")).title()
        icon = "♿" if kind == "Elevator" else "↗"
        outages.append(f'{icon} <b>{kind} out at {esc(s["name"])}</b>: '
                       f'{esc(unit.get("LocationDescription", ""))} '
                       f'(back ~{short_date(unit.get("EstimatedReturnToService", ""))})')
if outages:
    show(f'<div class="notice">{" &nbsp;·&nbsp; ".join(outages)}</div>', unsafe_allow_html=True)

# ------------------------------------------------------------------ metro boards
trains_all = safe(rail_predictions, ",".join(s["code"] for s in STATIONS), default=None)


def station_board(station):
    walk_min = walk.get(station["code"])  # None when the walking countdown is switched off
    groups = [[], []]
    for t in trains_all or []:
        if t.get("LocationCode") != station["code"]:
            continue
        if t.get("DestinationName") in ("No Passenger", "Train", "ssenger", "") or train_eta(t) is None:
            continue
        idx = station["direction_fn"](str(t.get("DestinationCode") or ""),
                                      str(t.get("DestinationName") or "").lower())
        groups[idx].append(t)

    cols = []
    for label, trains in zip(station["directions"], groups):
        trains.sort(key=train_eta)
        rows, found_catchable = [], False
        for t in trains[:3]:
            eta = train_eta(t)
            raw = str(t.get("Min", "")).upper()
            mins = raw if raw in ("BRD", "ARR") else f"{eta}<small>min</small>"
            cls, note = "", ""
            if walk_min is not None:
                spare = eta - walk_min
                if spare < 0:
                    cls, note = "miss", "too soon to catch · "
                elif not found_catchable:
                    cls, found_catchable = "catch", True
                    note = ("LEAVE NOW" if spare == 0 else f"Leave in {spare} min") + " · "
                else:
                    note = f"leave in {spare} min · "
            cars = t.get("Car") or "?"
            rows.append(f'<div class="train {cls}"><div class="mins">{mins}</div>'
                        f'<div class="dest"><b>{esc(t.get("DestinationName", ""))}</b>'
                        f'<span>{esc(note)}{esc(cars)} cars</span></div>{badge(t.get("Line", ""))}</div>')
        body = "".join(rows) or '<div class="empty">No trains predicted right now</div>'
        cols.append(f"<div><h4>{esc(label)}</h4>{body}</div>")

    lines = " ".join(badge(l) for l in station["lines"])
    status = "" if trains_all is not None else "<b style='color:#f87171'>train feed unavailable</b>"
    walk_text = f"🚶 {walk_min} min walk" if walk_min is not None else ""
    hint = " · ".join(x for x in (walk_text, status) if x)
    show(f"""
    <div class="panel">
      <h2>{esc(station['name'])} {lines}<span class="hint">{hint}</span></h2>
      <div class="dirs">{''.join(cols)}</div>
    </div>""", unsafe_allow_html=True)


for col, station in zip(st.columns(len(STATIONS)), STATIONS):
    with col:
        station_board(station)

# ------------------------------------------------------------------ buses
stops = safe(nearby_stops, radius, default=[]) or []
stops = sorted(stops, key=lambda s: miles(HOME["lat"], HOME["lon"], s["Lat"], s["Lon"]))[:8]
stop_ids = list(dict.fromkeys([s["StopID"] for s in stops] + D80_STOP_IDS))

bus_groups = {}  # (route, direction) -> {"stop": name, "vehicles": {vehicle_key: minutes}}
for stop_id in stop_ids:
    stop_name, predictions = safe(stop_predictions, stop_id, default=("", [])) or ("", [])
    for p in predictions:
        key = (p.get("RouteID", ""), p.get("DirectionText", ""))
        group = bus_groups.setdefault(key, {"stop": stop_name, "vehicles": {}})
        vehicle = (p.get("VehicleID"), p.get("TripID"))
        minutes = int(p.get("Minutes", 999))
        if minutes < group["vehicles"].get(vehicle, 999):
            group["vehicles"][vehicle] = minutes

bus_rows = sorted(
    ((route, direction, g["stop"], sorted(g["vehicles"].values())[:3]) for (route, direction), g in bus_groups.items()),
    key=lambda r: (r[0] not in FEATURED_ROUTES, r[3][0] if r[3] else 999))

# one colour per route, shared by the bus list badges, the route lines and the live bus markers
map_routes = list(dict.fromkeys([r[0] for r in bus_rows] + FEATURED_ROUTES))[:10]
route_colors = {r: ROUTE_PALETTE[i % len(ROUTE_PALETTE)] for i, r in enumerate(map_routes)}

bus_alerts = []
active_routes = {r[0] for r in bus_rows} | set(FEATURED_ROUTES)
for incident in safe(bus_incidents, default=[]) or []:
    routes = set(incident.get("RoutesAffected") or [])
    if routes & active_routes:
        bus_alerts.append(f'<div class="notice">🚌 <b>{esc(", ".join(sorted(routes & active_routes)))}</b>: '
                          f'{esc(incident.get("Description", ""))}</div>')

bus_col, map_col, side_col = st.columns([1.15, 1.35, 0.9]) if show_map else (*st.columns([1.4, 1]), None)
if side_col is None:  # map hidden: put bikes/roads in the second column
    bus_col, side_col = bus_col, map_col
    map_col = None

def connects_text(route):
    stations = safe(route_connections, route, default=[]) or []
    if not stations:
        return ""
    names = [s.replace("-GWU", "").replace("Washington ", "") for s in stations]
    shown = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
    return f' <span class="connects">· 🚇 {esc(shown)}</span>'


with bus_col:
    rows = "".join(
        f'<div class="bus{" featured" if route in FEATURED_ROUTES else ""}">'
        f'<div class="route" style="background:{route_colors.get(route, "#334155")}">{esc(route)}</div>'
        f'<div class="dir"><b class="arrow">{direction_arrow(direction)}</b> {esc(direction)}'
        f'<small>{esc(stop.title())}{connects_text(route)}</small></div>'
        f'<div class="times">{" · ".join(str(m) for m in mins)}<small> min</small></div></div>'
        for route, direction, stop, mins in bus_rows[:11]) or '<div class="empty">No buses predicted nearby</div>'
    show(f'<div class="panel"><h3>🚌 Buses near you</h3>{rows}</div>{"".join(bus_alerts[:2])}',
                unsafe_allow_html=True)

# ------------------------------------------------------------------ bikeshare
bike_rows = []
info, status = safe(bike_station_info, default=[]), safe(bike_station_status, default={})
if info and status:
    docks = sorted(((miles(HOME["lat"], HOME["lon"], d["lat"], d["lon"]), d) for d in info),
                   key=lambda x: x[0])[:6]
    for distance, dock in docks:
        s = status.get(dock["station_id"], {})
        ebikes = s.get("num_ebikes_available", 0)
        classic = max(0, s.get("num_bikes_available", 0) - ebikes)
        bike_rows.append((dock, distance, classic, ebikes, s.get("num_docks_available", 0)))

# ------------------------------------------------------------------ map
if map_col is not None:
    with map_col:
        # Esri dark basemap + street-name layer: free, no API key (CARTO now demands one)
        esri = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"
        m = folium.Map(location=[HOME["lat"], HOME["lon"]], zoom_start=15, tiles=None, max_zoom=16,
                       width="100%", height="100%", control_scale=False)
        folium.TileLayer(esri.format("World_Dark_Gray_Base"), attr="Esri, HERE, Garmin, © OpenStreetMap contributors",
                         max_zoom=16, name="base").add_to(m)
        folium.TileLayer(esri.format("World_Dark_Gray_Reference"), attr="Esri", max_zoom=16, overlay=True,
                         name="labels").add_to(m)
        routes = safe(track_routes, default={}) or {}
        names = safe(rail_stations, default={}) or {}
        if show_routes:
            # Bus routes: bold street paths (dark casing underneath so they pop), same colour as the bus list badge
            for route_id, color in route_colors.items():
                shapes = safe(route_shape, route_id, default=[]) or []
                conn = ", ".join(safe(route_connections, route_id, default=[]) or []) or "no Metro stops"
                for shape in shapes:
                    folium.PolyLine(shape["points"], color="#020617", weight=9, opacity=.75).add_to(m)
                    folium.PolyLine(shape["points"], color=color, weight=5, opacity=.95,
                                    tooltip=f'{route_id} → {shape["headsign"]} · Metro: {conn}').add_to(m)
                # label each route on its line at the point nearest home (kept just off the home marker)
                pts = [p for s in shapes for p in s["points"]
                       if miles(HOME["lat"], HOME["lon"], p[0], p[1]) > 0.12]
                if pts:
                    lp = min(pts, key=lambda p: miles(HOME["lat"], HOME["lon"], p[0], p[1]))
                    folium.Marker(lp, icon=folium.DivIcon(icon_size=(40, 18), icon_anchor=(20, 9), html=(
                        f'<div style="background:#0b1220;color:{color};border:2px solid {color};border-radius:9px;'
                        f'font:800 11px sans-serif;text-align:center;width:40px;box-sizing:border-box;'
                        f'padding:1px 0">{esc(route_id)}</div>'))).add_to(m)
        for s in STATIONS:
            folium.Marker([s["lat"], s["lon"]], tooltip=s["name"], icon=folium.DivIcon(
                icon_size=(28, 28), icon_anchor=(14, 14),
                html=f'<div style="width:28px;height:28px;border-radius:50%;background:{LINE_COLORS[s["lines"][0]]};'
                     f'color:#fff;font:800 15px sans-serif;display:flex;align-items:center;justify-content:center;'
                     f'border:2px solid #fff">M</div>')).add_to(m)
        for s in stops:
            folium.CircleMarker([s["Lat"], s["Lon"]], radius=5, color="#93c5fd", fill=True, fill_opacity=.9,
                                tooltip=f'{s["Name"]} · {", ".join(s.get("Routes", []))}').add_to(m)
        for dock, _, classic, ebikes, free in bike_rows:
            folium.CircleMarker([dock["lat"], dock["lon"]], radius=6, color="#f43f5e", fill=True, fill_opacity=.85,
                                tooltip=f'Bikeshare: {dock["name"]} · {classic} bikes, {ebikes} e-bikes').add_to(m)
        # Metro trains (approximate position from track circuit) with direction arrows
        train_count = 0
        for train in safe(train_positions, default=[]) or []:
            if not train.get("LineCode") or train.get("ServiceType") not in ("Normal", None):
                continue
            pos, heading = locate_train(train, routes)
            if not pos or miles(HOME["lat"], HOME["lon"], pos[0], pos[1]) > 1.6:
                continue
            train_count += 1
            dest = names.get(train.get("DestinationStationCode"), {}).get("name", "destination not set")
            line = train["LineCode"]
            folium.Marker(pos, tooltip=f'{line} train {train.get("TrainNumber", "")} → {dest} · '
                                       f'{train.get("CarCount", "?")} cars (approx. position)',
                          icon=vehicle_icon(line, LINE_COLORS.get(line, "#475569"), heading, 46),
                          z_index_offset=500).add_to(m)

        # Live buses with direction arrows (from real movement between refreshes, else WMATA's compass direction)
        bus_count = 0
        for bus in safe(bus_positions, max(radius, 800) * 2, default=[]) or []:
            route = bus.get("RouteID", "")
            color = route_colors.get(route, "#475569")
            heading = bus_heading(bus)
            bus_count += 1
            headsign = str(bus.get("TripHeadsign", "")).title()
            folium.Marker([bus["Lat"], bus["Lon"]],
                          tooltip=f'{route} → {headsign} ({str(bus.get("DirectionText", "")).title()})',
                          icon=vehicle_icon(route, color, heading, 54), z_index_offset=400).add_to(m)

        folium.CircleMarker([HOME["lat"], HOME["lon"]], radius=9, color="#fff", weight=3, fill=True,
                            fill_color="#22c55e", fill_opacity=1, tooltip=HOME["label"]).add_to(m)
        components.html(m.get_root().render(), height=425)
        route_key = "".join(f'<span><i class="line" style="color:{c}"></i>{esc(r)}</span>'
                            for r, c in route_colors.items()) if show_routes else ""
        show(f'<div class="legend"><span>▲ = direction of travel</span>{route_key}'
             f'<span>{badge("SV")} train (approx.)</span><span><b style="color:#f43f5e">●</b> bikeshare</span>'
             f'<span>{bus_count} buses · {train_count} trains nearby</span></div>')

# ------------------------------------------------------------------ bikes + roads panel
with side_col:
    rows = "".join(
        f'<div class="row"><div><b>{esc(dock["name"])}</b><br><span style="opacity:.6">{dist:.2f} mi</span></div>'
        f'<div style="text-align:right">🚲 {classic} · ⚡ {ebikes}<br><span style="opacity:.6">{free} open docks</span></div></div>'
        for dock, dist, classic, ebikes, free in bike_rows) or '<div class="empty">Bikeshare data unavailable</div>'
    show(f'<div class="panel"><h3>🚲 Capital Bikeshare</h3>{rows}</div>', unsafe_allow_html=True)

    road_rows = []
    for d in DESTINATIONS:
        road_miles = miles(HOME["lat"], HOME["lon"], d["lat"], d["lon"]) * 1.25
        est = max(2, round(road_miles / (18 if road_miles < 8 else 35) * 60))  # city vs highway speed
        label = f"~{est} min"
        if TOMTOM_KEY:
            live = safe(tomtom_minutes, d["lat"], d["lon"])
            if live:
                label = f"{live[0]} min" + (f' <span class="rain-mid">+{live[1]} delay</span>' if live[1] >= 3 else "")
        base = f"https://www.google.com/maps/dir/?api=1&origin={HOME['lat']},{HOME['lon']}&destination={d['lat']},{d['lon']}"
        road_rows.append(f'<div class="row"><b>{esc(d["name"])}</b><div>{label}'
                         f'<a href="{base}&travelmode=driving" target="_blank">drive</a>'
                         f'<a href="{base}&travelmode=transit" target="_blank">transit</a></div></div>')
    hint = "Live traffic (TomTom)" if TOMTOM_KEY else "Estimated drive time · add a TomTom key for live traffic"
    show(f'<div class="panel"><h3>🚗 Drive times</h3><div class="hint">{hint}</div>{"".join(road_rows)}</div>',
                unsafe_allow_html=True)

show(f'<div class="foot">Updated {now.strftime("%I:%M:%S %p").lstrip("0")} · refreshes every '
            f'{REFRESH_SECONDS}s · data: WMATA, Open-Meteo, Capital Bikeshare, OpenStreetMap/CARTO</div>',
            unsafe_allow_html=True)
