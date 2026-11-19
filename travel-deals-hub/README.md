# Travel Deals Hub

A weekly dashboard of flight deals, hotel deals, travel trends, and United / Delta / Amex experiences.
New deals are collected every **Sunday at 7 AM**.

## Start it
Double-click **start.bat**. It opens http://localhost:8503. The first run sets up Python packages.
The app is only reachable from this PC.

## Weekly updates
- The app refreshes itself the first time it is opened after Sunday 7 AM.
- To update even when the app is closed, double-click **install-weekly.bat** once (adds a Sunday 7 AM task).
- **Refresh now** in the top-right corner pulls fresh deals at any time.

## Tabs
| Tab | What it shows |
|---|---|
| This week | Claude's top picks, fare-alert matches, your watchlist, deals ending soon, cheapest DC fares, hotels, United |
| Flights | All fare and award deals; filter by DC departures and region, search, sort by price or deadline |
| Hotels | Hotel promos, free-night offers, resort deals |
| Experiences | One calendar of United, Delta and Amex experiences by date, with a DC-area filter |
| United | Live MileagePlus Exclusives with week-over-week bid changes, plus United news |
| Delta | Your imported SkyMiles Experiences, plus Delta news |
| Amex | Your imported Amex Experiences, transfer bonuses, Amex offers and news |
| Trends | Most-mentioned destinations and brands, week-over-week change, DC fare history |
| Settings | Watchlist, fare alerts, Claude, email digest, kiosk timing |
| Sources | Which sources worked on the last update |

## Importing Amex and Delta experiences
Their sites only show events in your browser, so you bring them in yourself (about 5 seconds):
open the page, scroll to the bottom, **Ctrl+A**, **Ctrl+C**, paste into the tab's *Import experiences* box,
press **Read events**, check the table and **Save**. With Claude connected you can also upload a screenshot.

## Fare alerts and watchlist (Settings tab)
- Alerts like "Europe under $600 from DC" pop up as a Windows notification after each update.
- Watchlist places get their own "For you" section on the home tab.

## Optional extras
- **Claude top picks + screenshot reading:** run `ant auth login`, or set a Windows user environment variable
  `ANTHROPIC_API_KEY`. Uses Claude Opus 5.5 with automatic fallback if a request is declined; a few cents a week.
- **Sunday email digest:** fill in the Settings tab and store your email app password in a Windows user
  environment variable named `TRAVELHUB_SMTP_PASSWORD` (it is never written to disk by the app).
- **Kiosk screen:** http://localhost:8503/?view=kiosk is a big-type rotating view. Double-click
  **enable-kiosk-rotation.bat** to make the train-times kiosk alternate with it (from the kiosk's next start);
  **disable-kiosk-rotation.bat** turns it off.

## Your data
Settings and imported events live in `data/user.json`; weekly snapshots in `data/snapshots`.
Edit **settings.json** to add or remove feeds or change the home airports.
