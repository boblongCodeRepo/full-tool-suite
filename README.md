# Home Apps

A collection of small desktop and home-server apps built for a Windows mini PC in Washington, DC.
Each folder is a separate project with its own README or start script.

| Folder | What it is |
|---|---|
| [market-terminal](market-terminal) | Bloomberg-style market terminal (FastAPI + plain JS) with companion apps: **Device Watch** (network, Wi-Fi, Bluetooth, hotspot and intrusion monitoring for this PC), Influence Watch, events, auctions, morning brief, and a home-finance ledger. Built into a Windows app with PyInstaller (`build.bat`). Also holds `tools/guest-router`, a guest Wi-Fi captive portal + logging kit for an OpenWrt / GL.iNet router. |
| [transit-hub](transit-hub) | DC Transit Hub: a kiosk screen with Metro trains, buses, traffic, weather, news and DC sports. The Streamlit app is in `Transit Hub/`; the top level is the older static web version (Docker + nginx). |
| [dmv-streamlit](dmv-streamlit) | Earlier single-page Streamlit board for DC Metro and weather. |
| [pihole-setup](pihole-setup) | Scripts to set up Pi-hole on a Raspberry Pi, plus `netmap`, a small network map page. |
| [nuc-setup](nuc-setup) | Proxmox setup for an Intel NUC: Pi-hole and ledger containers, a Windows VM for the apps, and the guest-router kit. |
| [business-class-finder](business-class-finder) | Business-class round-trip prices from DC, New York and Chicago to Europe and Asia (Google Flights data via SerpApi). |
| [used-car-finder](used-car-finder) | Searches used cars and shows which listings are priced below similar cars (MarketCheck listings, NHTSA safety data). |
| [last-minute-tickets](last-minute-tickets) | Games, concerts, theater and comedy near DC over the next few days, with the cheapest seats. |
| [travel-deals-hub](travel-deals-hub) | Weekly Streamlit dashboard of flight and hotel deals, travel trends, and airline / Amex experiences. |
| [portfolio-checkup](portfolio-checkup) | Drop in brokerage or bank spreadsheets and get a plain-English portfolio risk review. |
| [connection-monitor](connection-monitor) | PowerShell script that logs every new outgoing connection (program, address, port) and flags suspicious ones. |

## Running

Most Python apps need Python 3.12+ and their own virtual environment:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

then run the `start.bat` / `build.bat` in that folder.

## API keys

No keys are included. Apps that need one (WMATA, TomTom, Ticketmaster, FEC, SerpApi...) ask for it in their
settings screen or read it from an environment variable / `.streamlit/secrets.toml`, which is git-ignored.

## Disclaimer

The security and guest Wi-Fi tools are for monitoring **your own** PC and network. Logging other people's
network activity has legal requirements that differ by place; read `market-terminal/tools/guest-router/README.md`
before using that kit.
