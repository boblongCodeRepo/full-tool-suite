# Business Class Finder

Business-class round-trip prices from **Washington DC** (IAD, DCA, BWI), **New York** (JFK, EWR, LGA) and
**Chicago** (ORD, MDW) to cities in **Europe** and **Asia**, using Google Flights data through SerpApi.

## Start
Double-click **Business Class Finder** on the Desktop. (Re-create the icon any time with `install-desktop-app.bat`.)
It opens in its own window; closing the window stops it.

## One-time setup
1. Create a free account at https://serpapi.com/users/sign_up
2. Copy your API key from https://serpapi.com/manage-api-key
3. Paste it into the app (setup box, or Settings) and press Save.

The free SerpApi plan has a limited number of searches per month; the app shows how many you have left.
Each route (e.g. Chicago to Tokyo) costs 1 search. The default 6 destinations x 3 cities = 18 searches per check.

## What you see
- **Best prices right now**: the cheapest routes to Europe and to Asia, with Google's verdict (good / typical / high).
- **Price board**: every city pair; click a price for the flight options, Google's typical price range, and the
  last two months of prices. Each route also has one-click Google Flights / Kayak searches (no key needed).
- **Business-class sales & deals in the news** from deal sites.
- **Hotels** (in each route's details): 4-5 star hotels in the city centre for your trip dates, rated 4.0+, from
  Google Hotels. Shows nightly and total price, Google's "deal" flag, a whole-trip total (flight + the cheapest hotel
  rated 4.3+), and for Hyatt, Marriott, Hilton, IHG and Accor hotels the **points break-even**: "points beat cash if
  it's under N points a night", converted to your card points. Each hotel search costs 1 SerpApi search and is kept
  for 24 hours; it only runs when you press "Find hotels". Hotel programs and point values are in `points.py`.

## Settings
Dates (a fixed departure date, or "about 6 weeks out"), nights away, nonstop-only, which cities to track, and
an automatic weekly check. Settings, prices and history live in `data\`. Your key is only sent to serpapi.com.
