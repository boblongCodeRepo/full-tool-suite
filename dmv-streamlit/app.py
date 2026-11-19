import os
import re
import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ------------------ PAGE CONFIG ------------------
st.set_page_config(page_title="DC Metro Smart Board", layout="wide")
st.markdown("""
<style>
html, body, [class*="css"]  {
    background-color: #0f172a !important;
    color: white !important;
}

.block-container {
    padding-top: 0rem;
    padding-left: 1rem;
    padding-right: 1rem;
}

/* Weather animations */
@keyframes rainDrop {
  0% { transform: translateY(-10px); opacity: 0; }
  50% { opacity: 1; }
  100% { transform: translateY(10px); opacity: 0; }
}

.wx-icon {
    display: inline-block;
    margin-right: 8px;
}

.wx-rain { animation: rainDrop 1s infinite; color:#22c55e; }
.wx-thunder { animation: shake 0.5s infinite; color:#f59e0b; }

@keyframes shake {
  0% { transform: translate(0px,0px); }
  25% { transform: translate(2px,-2px); }
  50% { transform: translate(-2px,2px); }
  75% { transform: translate(2px,2px); }
  100% { transform: translate(0px,0px); }
}

/* Alerts banner */
.alert-banner {
    width:100%;
    background:#ef4444;
    color:white;
    font-weight:700;
    text-align:center;
    padding:12px 0;
    margin-bottom:12px;
    border-radius:6px;
}
</style>
""", unsafe_allow_html=True)

WMATA_BASE = "https://api.wmata.com"

# ------------------ HELPERS ------------------
def wmata_get(path: str, api_key: str, params=None):
    if params is None: params = {}
    headers = {"api_key": api_key}
    r = requests.get(WMATA_BASE + path, headers=headers, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"WMATA error {r.status_code}: {r.text[:200]}")
    return r.json()

def _norm(s):
    s = "" if s is None else str(s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

# ------------------ WEATHER ------------------
@st.cache_data(ttl=600)
def get_weather(lat, lon):
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "current_weather": True,
            "minutely_15": "precipitation",
            "hourly": "temperature_2m,precipitation_probability,weathercode",
            "temperature_unit": "fahrenheit",
            "timezone": "America/New_York",
            "forecast_days": 1,
        },
        timeout=20
    )
    return r.json()

# ------------------ WMATA ------------------
@st.cache_data(ttl=3600)
def get_rail_stations(api_key):
    data = wmata_get("/Rail.svc/json/jStations", api_key)
    return pd.DataFrame(data.get("Stations", []))

@st.cache_data(ttl=20)
def get_rail_predictions(api_key, station_code):
    return wmata_get(f"/StationPrediction.svc/json/GetPrediction/{station_code}", api_key)

@st.cache_data(ttl=300)
def get_alerts(api_key):
    data = wmata_get("/Incidents.svc/json/Incidents", api_key)
    return data.get("Incidents", [])

# ------------------ SIDEBAR ------------------
st.title("🚇 DC Metro Smart Board")
with st.sidebar:
    st.header("Settings")
    api_key = st.text_input("WMATA API Key", value=os.getenv("WMATA_API_KEY",""), type="password")
if not api_key:
    st.warning("Enter your WMATA API key in the sidebar.")
    st.stop()
# ------------------ WEATHER BLOCK ------------------
LAT, LON = 38.9007, -77.0503  # Foggy Bottom coords
st_autorefresh(interval=5*60*1000, key="weather_refresh")

wx = get_weather(LAT, LON)

current = wx.get("current_weather", {})
hourly = wx.get("hourly", {})

if current and hourly and hourly.get("time"):

    # ---- CURRENT CONDITIONS ----
    now_temp = current.get("temperature")
    now_code = current.get("weathercode")

    # ---- HOURLY FORECAST ----
    times = hourly["time"][:6]
    temps = hourly["temperature_2m"][:6]
    pops = hourly["precipitation_probability"][:6]
    codes = hourly["weathercode"][:6]

    now_pop = pops[0] if pops else 0
    max_rain = int(max(pops)) if pops else 0
    hours = [t.split("T")[1][:5] for t in times]

    # ---- MINUTELY RAIN CHECK ----
    minutely = wx.get("minutely_15", {})
    minute_precip = minutely.get("precipitation", [])

    is_raining_now = False
    if minute_precip:
        # check current 15-min block
        is_raining_now = minute_precip[0] > 0

    # ---- RAIN DISPLAY LOGIC ----
    if is_raining_now:
        rain_text = "Raining now"
        rain_color = "#3b82f6"
    else:
        rain_text = f"Rain chance {int(now_pop)}%"
        if max_rain >= 50:
            rain_color = "#ef4444"
        elif max_rain >= 30:
            rain_color = "#f97316"
        else:
            rain_color = "#22c55e"

    # ---- ICON FUNCTION ----
    def anim_icon(code):
        if code == 0: return '<span class="wx-icon">☀</span>'
        if code in [1,2]: return '<span class="wx-icon">⛅</span>'
        if code == 3: return '<span class="wx-icon">☁</span>'
        if code in [45,48]: return '<span class="wx-icon">🌫</span>'
        if code in [51,53,55,61,63,65,80,81,82]: return '<span class="wx-icon wx-rain">🌧</span>'
        if code in [71,73,75,77]: return '<span class="wx-icon">❄</span>'
        if code in [95,96,99]: return '<span class="wx-icon wx-thunder">⛈</span>'
        return '<span class="wx-icon">☁</span>'

    forecast_strip = "   ".join(
        f"{anim_icon(c)} {int(t)}°" for c, t in zip(codes, temps)
    )

    hour_strip = "   ".join(hours)

    weather_html = f"""
    <div style="padding:30px; background:#111827; border-radius:6px; margin-bottom:20px;">
        <div style="font-size:64px; font-weight:900;">
            {anim_icon(now_code)} {now_temp:.0f}°F
        </div>
        <div style="font-size:22px; font-weight:700; margin-top:10px; color:{rain_color};">
            {rain_text} · Max next 6h {max_rain}%
        </div>
        <div style="font-size:24px; font-weight:700; margin-top:16px;">
            {forecast_strip}
        </div>
        <div style="font-size:16px; opacity:0.6; margin-top:4px;">
            {hour_strip}
        </div>
    </div>
    """
    st.markdown(weather_html, unsafe_allow_html=True)

# ------------------ ALERTS ------------------
alerts = get_alerts(api_key)
stations_of_interest = ["Foggy Bottom–GWU","Dupont Circle"]
relevant_alerts = []
for alert in alerts:
    desc = alert.get("Description", "")
    stations = alert.get("Stations", [])
    if any(s in stations_of_interest for s in stations):
        relevant_alerts.append(desc)

for a in relevant_alerts:
    st.markdown(f'<div class="alert-banner">{a}</div>', unsafe_allow_html=True)

# ------------------ WMATA TRAIN STATIONS ------------------
rail_df = get_rail_stations(api_key)
def find_station(name_contains,fallback):
    m = rail_df[rail_df["Name"].str.contains(name_contains, case=False, na=False)]
    return m.iloc[0]["Code"] if not m.empty else fallback

foggy_code = find_station("Foggy Bottom","C04")
dupont_code = find_station("Dupont","A03")

def foggy_bucket(dest):
    d=_norm(dest)
    toward_terms=["new carrollton","largo","downtown largo","new crlton","newcrlton"]
    if any(t in d for t in toward_terms): return "Toward New Carrollton / Largo"
    return "Other direction"

def dupont_bucket(dest):
    d=_norm(dest)
    if "shady" in d: return "Toward Shady Grove"
    if "glenmont" in d: return "Toward Glenmont"
    return "Unknown"

def build_df(trains,bucket_fn):
    if not trains: return pd.DataFrame()
    df=pd.DataFrame(trains)
    if "Min" in df.columns: df["ETA_min"]=pd.to_numeric(df["Min"],errors="coerce")
    else: df["ETA_min"]=pd.NA
    if "DestinationName" in df.columns: df["DirectionGroup"]=df["DestinationName"].apply(bucket_fn)
    else: df["DirectionGroup"]="Unknown"
    return df.sort_values("ETA_min", na_position="last")

def line_badge(line):
    colors={"RD":"#d32f2f","BL":"#1976d2","OR":"#f57c00","SV":"#9e9e9e","GR":"#388e3c","YL":"#fbc02d"}
    code=(line or "").upper()
    c=colors.get(code,"#444")
    return f"<span style='background:{c};color:white;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700'>{code}</span>"

def fmt_next_eta(df):
    if df.empty: return "—"
    return str(df.iloc[0].get("Min","—"))

def render_board(df,max_rows=6):
    if df.empty: st.info("No trains right now."); return
    for _,r in df.head(max_rows).iterrows():
        mins=str(r.get("Min","—"))
        dest=str(r.get("DestinationName",""))
        line=str(r.get("Line",""))
        car=str(r.get("Car",""))
        grp=str(r.get("Group",""))
        st.markdown(f"""
        <div style="
            display:flex; justify-content:space-between; align-items:center;
            padding:10px 14px; border-radius:14px;
            background: rgba(255,255,255,0.03);
            border:1px solid rgba(255,255,255,0.10);
            margin:6px 0;">
            <div style="display:flex; gap:14px; align-items:center;">
                <div style="font-size:26px;font-weight:800;min-width:55px;text-align:right;">
                    {mins}
                </div>
                <div>
                    <div style="font-weight:700;">{dest}</div>
                    <div style="font-size:12px;opacity:0.75;">
                        Cars: {car or "—"} · Platform: {grp or "—"}
                    </div>
                </div>
            </div>
            <div>{line_badge(line)}</div>
        </div>
        """, unsafe_allow_html=True)

# ------------------ DISPLAY TRAINS ------------------
st_autorefresh(interval=60*1000, key="refresh")
col1,col2=st.columns(2)

foggy=get_rail_predictions(api_key,foggy_code)
dupont=get_rail_predictions(api_key,dupont_code)

foggy_df=build_df(foggy.get("Trains",[]),foggy_bucket)
dupont_df=build_df(dupont.get("Trains",[]),dupont_bucket)

# Foggy Bottom
with col1:
    st.markdown(f"## Foggy Bottom–GWU ({foggy_code})")
    left=foggy_df[foggy_df["DirectionGroup"]=="Other direction"]
    right=foggy_df[foggy_df["DirectionGroup"]=="Toward New Carrollton / Largo"]
    c1,c2=st.columns(2)
    with c1:
        st.markdown("### Other direction")
        st.metric("Next train",fmt_next_eta(left))
        render_board(left)
    with c2:
        st.markdown("### Toward New Carrollton / Largo")
        st.metric("Next train",fmt_next_eta(right))
        render_board(right)

# Dupont Circle
with col2:
    st.markdown(f"## Dupont Circle ({dupont_code})")
    left=dupont_df[dupont_df["DirectionGroup"]=="Toward Shady Grove"]
    right=dupont_df[dupont_df["DirectionGroup"]=="Toward Glenmont"]
    d1,d2=st.columns(2)
    with d1:
        st.markdown("### Toward Shady Grove")
        st.metric("Next train",fmt_next_eta(left))
        render_board(left)
    with d2:
        st.markdown("### Toward Glenmont")
        st.metric("Next train",fmt_next_eta(right))
        render_board(right)

st.caption("Auto-refreshing every 60 seconds")
