# Last-Minute Tickets

Games, concerts, theater and comedy near you over the next few days (Washington, DC by default), with the cheapest
seat right now, face value, price drops, and one-click **Gametime**, **SeatGeek** and **Ticketmaster** buttons.

## Start
Double-click **Last-Minute Tickets** on the Desktop. (Re-create the icon any time with `install-desktop-app.bat`.)
It opens in its own window; closing the window stops it.

## One-time setup (free keys, add one or both in Settings)
- **SeatGeek Client ID**: https://seatgeek.com/account/develop, create an app, copy the Client ID.
  Gives live resale prices: the cheapest seat and number of listings, used for price drops.
- **Ticketmaster Consumer Key**: https://developer-acct.ticketmaster.com/user/register, then My Apps.
  Adds face value, on-sale / cancelled / postponed status, and events SeatGeek doesn't list. 5,000 calls a day.
- **Gametime** has no public API and its terms forbid automated access, so each event has a Gametime button that
  opens Gametime's own search for it (its last-minute prices are often the lowest; compare there).

## What you see
- **Best deals right now**: biggest price drops, seats under face value, cheap seats starting soon.
- Every event with the cheapest seat (SeatGeek, fees included), a price-history sparkline, face value
  (Ticketmaster) and tags: *Down 29% (was $41)*, *47% under face value*, *Only 8 listings left*, *Cancelled*.
- Filter by Tonight / Tomorrow / This weekend / Next 3 days, by type, or search a team, artist or venue.
- **Watch** (star) an event with a target price: alerts show at the top and as a Windows notification.
- Re-checks every 30 minutes while open (change in Settings, along with city or ZIP, radius and days ahead).
  A check uses about 2-8 calls in total.

Keys, watch list and price history live in `data\` on this PC. Keys are only sent to SeatGeek and Ticketmaster.
`build_portable.bat` makes one portable `dist\LastMinuteTickets.exe`; put a `LastMinuteTickets-data` folder next
to it to carry your keys and history.
