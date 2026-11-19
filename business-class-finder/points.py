"""Award (points) data for Business Class Finder.

These are APPROXIMATE one-way business-class saver prices from the programs' published award charts, as of
2025. Airlines change charts and many now price dynamically, and award seats must actually be available. The app
labels everything here as an estimate and links to each program's award search. Edit freely: miles are
[low, high] one-way, taxes are [low, high] USD one-way.

Zones: "Europe", "NAsia" (Japan, Korea), "SAsia" (rest of Asia).
"""
import re

CARDS = {
    "amex": "Amex Membership Rewards",
    "chase": "Chase Ultimate Rewards",
    "capone": "Capital One Miles",
    "citi": "Citi ThankYou Points",
    "bilt": "Bilt Rewards",
}

ALLIANCES = {
    "star": ["united", "lufthansa", "swiss", "austrian", "brussels", "ana", "singapore airlines", "eva air", "turkish",
             "asiana", "tap", "lot", "air canada", "air india", "thai", "ethiopian", "air new zealand", "egyptair",
             "shenzhen", "air china", "croatia", "aegean", "copa", "avianca"],
    "oneworld": ["american", "british airways", "iberia", "japan airlines", "jal", "cathay", "qatar", "finnair",
                 "alaska", "malaysia", "royal jordanian", "srilankan", "qantas", "fiji", "royal air maroc"],
    "skyteam": ["delta", "air france", "klm", "korean air", "virgin atlantic", "ita airways", "china airlines",
                "vietnam airlines", "garuda", "saudia", "aeromexico", "sas", "scandinavian", "china eastern",
                "xiamen", "kenya airways", "middle east airlines", "tarom", "air europa"],
}

PROGRAMS = [
    {"id": "aeroplan", "name": "Air Canada Aeroplan", "alliances": ["star"], "airlines": ["etihad", "emirates"],
     "miles": {"Europe": [60000, 70000], "NAsia": [75000, 87500], "SAsia": [87500, 105000]}, "taxes": [60, 200],
     "cards": ["amex", "chase", "capone", "bilt"], "url": "https://www.aircanada.com/aeroplan/redeem/",
     "note": "Distance-based chart; usually no fuel surcharges on United, ANA, Turkish or TAP. Small booking fee on partner flights."},
    {"id": "united", "name": "United MileagePlus", "alliances": ["star"], "airlines": [],
     "miles": {"Europe": [80000, 88000], "NAsia": [80000, 88000], "SAsia": [80000, 110000]}, "taxes": [6, 150],
     "cards": ["chase", "bilt"], "url": "https://www.united.com/en/us/book-flight/united-reservations",
     "note": "Saver partner awards. United's own flights are priced dynamically and are often much higher."},
    {"id": "lifemiles", "name": "Avianca LifeMiles", "alliances": ["star"], "airlines": [],
     "miles": {"Europe": [63000, 78000], "NAsia": [75000, 90000], "SAsia": [78000, 105000]}, "taxes": [50, 200],
     "cards": ["amex", "citi", "capone"], "url": "https://www.lifemiles.com/",
     "note": "No fuel surcharges. Frequent points-purchase sales."},
    {"id": "ana", "name": "ANA Mileage Club", "alliances": ["star"], "airlines": [], "round_trip_only": True,
     "miles": {"Europe": [44000, 49500], "NAsia": [37500, 45000], "SAsia": [45000, 55000]}, "taxes": [100, 450],
     "cards": ["amex"], "url": "https://www.ana.co.jp/en/us/amc/",
     "note": "Round trips only (shown here as half the round-trip price). One of the best values for business class, especially on ANA itself."},
    {"id": "aa", "name": "American AAdvantage", "alliances": ["oneworld"], "airlines": [],
     "miles": {"Europe": [57500, 57500], "NAsia": [60000, 60000], "SAsia": [70000, 70000]}, "taxes": [6, 600],
     "cards": ["citi"], "url": "https://www.aa.com/booking/find-flights?awardBooking=true",
     "note": "Partner saver chart. Great on Japan Airlines, Cathay, Qatar and Finnair; avoid British Airways flights (large fuel surcharges)."},
    {"id": "iberia", "name": "Iberia Plus (Avios)", "alliances": [], "airlines": ["iberia"],
     "miles": {"Europe": [34000, 50000]}, "taxes": [150, 300],
     "cards": ["amex", "chase", "capone", "bilt"], "url": "https://www.iberia.com/us/iberia-plus/",
     "note": "Famous sweet spot: Iberia's own business class from the US East Coast and Chicago to Madrid, about 34k off-peak."},
    {"id": "ba", "name": "British Airways Club (Avios)", "alliances": [], "airlines": ["british airways"],
     "miles": {"Europe": [50000, 75000]}, "taxes": [300, 800],
     "cards": ["amex", "chase", "capone", "bilt"], "url": "https://www.britishairways.com/travel/redeem/execclub/",
     "note": "Off-peak pricing helps, but British Airways' own flights carry high fees."},
    {"id": "aerlingus", "name": "Aer Lingus AerClub (Avios)", "alliances": [], "airlines": ["aer lingus"],
     "miles": {"Europe": [50000, 75000]}, "taxes": [100, 250],
     "cards": ["amex", "chase", "capone", "bilt"], "url": "https://www.aerlingus.com/aerclub/",
     "note": "Off-peak business to Dublin (and onward) with much lower fees than British Airways."},
    {"id": "flyingblue", "name": "Air France-KLM Flying Blue", "alliances": ["skyteam"], "airlines": [],
     "miles": {"Europe": [50000, 100000], "NAsia": [70000, 150000], "SAsia": [75000, 150000]}, "taxes": [200, 450],
     "cards": ["amex", "chase", "citi", "capone", "bilt"], "url": "https://wwws.airfrance.us/en/flying-blue",
     "note": "Dynamic pricing. Monthly Promo Rewards cut some routes 25-50%."},
    {"id": "virgin", "name": "Virgin Atlantic Flying Club", "alliances": [], "airlines": ["virgin atlantic", "delta", "air france", "klm", "ana"],
     "miles": {"Europe": [47500, 67500], "NAsia": [52500, 72500]}, "taxes": [100, 900],
     "cards": ["amex", "chase", "citi", "capone", "bilt"], "url": "https://www.virginatlantic.com/flying-club",
     "note": "Sweet spot: ANA business class to Japan. Virgin's own flights to London carry high fees; Delta flights are dynamic."},
    {"id": "delta", "name": "Delta SkyMiles", "alliances": ["skyteam"], "airlines": [],
     "miles": {"Europe": [150000, 350000], "NAsia": [150000, 350000], "SAsia": [150000, 350000]}, "taxes": [6, 450],
     "cards": ["amex"], "url": "https://www.delta.com/flight-search/book-a-flight",
     "note": "Fully dynamic and usually poor value for business class. Try Flying Blue or Virgin for Delta flights."},
    {"id": "krisflyer", "name": "Singapore KrisFlyer", "alliances": [], "airlines": ["singapore airlines"],
     "miles": {"SAsia": [99000, 140000], "Europe": [61000, 90000]}, "taxes": [60, 200],
     "cards": ["amex", "chase", "citi", "capone", "bilt"], "url": "https://www.singaporeair.com/en_UK/us/ppsclub-krisflyer/",
     "note": "The only practical way to get Singapore Airlines' own business class saver seats."},
    {"id": "asiamiles", "name": "Cathay Pacific Asia Miles", "alliances": [], "airlines": ["cathay"],
     "miles": {"SAsia": [85000, 110000]}, "taxes": [60, 250],
     "cards": ["amex", "citi", "capone", "bilt"], "url": "https://www.cathaypacific.com/cx/en_US/membership/asia-miles.html",
     "note": "Cathay's own flights to Hong Kong and onward."},
]

# ------------------------------------------------------------------ hotels
# Hotel points are mostly priced dynamically, so instead of guessing an award price the app shows the BREAK-EVEN:
# "points are a good deal if the hotel wants fewer than (cash price / typical value) points a night".
# cpp = typical value of one program point in US cents (rounded, widely published valuations); cards = transfer
# ratio from each card program (1 card point -> N hotel points). brands = regexes matched against the hotel's name.
HOTEL_PROGRAMS = [
    {"id": "hyatt", "name": "World of Hyatt", "cpp": 1.7, "cards": {"chase": 1, "bilt": 1},
     "brands": [r"park hyatt", r"grand hyatt", r"hyatt", r"\bandaz\b", r"\balila\b", r"\bthompson\b", r"\bmiraval\b",
                r"caption by hyatt", r"unbound collection", r"destination by hyatt", r"\bjdv\b"],
     "url": "https://world.hyatt.com/content/gp/en/rewards/free-nights-upgrades.html",
     "note": "Fixed award chart: 3,500 to 45,000 points a night depending on the hotel's category, so luxury Hyatts are often the best points deal in travel."},
    {"id": "marriott", "name": "Marriott Bonvoy", "cpp": 0.8, "cards": {"amex": 1, "chase": 1, "bilt": 1},
     "brands": [r"ritz[- ]carlton", r"st\.? regis", r"\bjw marriott", r"marriott", r"^w\b", r"\bw hotel", r"\bedition\b",
                r"luxury collection", r"\bwestin\b", r"\bsheraton\b", r"le m[eé]ridien", r"autograph collection",
                r"\brenaissance\b", r"\bcourtyard\b", r"\baloft\b", r"\bmoxy\b", r"tribute portfolio", r"four points",
                r"ac hotel", r"design hotels"],
     "url": "https://www.marriott.com/loyalty/redeem.mi",
     "note": "Dynamic pricing. Book 5 award nights and pay for only 4."},
    {"id": "hilton", "name": "Hilton Honors", "cpp": 0.5, "cards": {"amex": 2},
     "brands": [r"waldorf astoria", r"\bconrad\b", r"\blxr\b", r"\bhilton\b", r"doubletree", r"canopy by hilton",
                r"curio collection", r"tapestry collection", r"embassy suites", r"hampton by hilton", r"motto by hilton", r"\bsignia\b"],
     "url": "https://www.hilton.com/en/hilton-honors/points/",
     "note": "Dynamic pricing, but standard rooms never cost more than the hotel's 'standard' award price. Elites get the 5th night free. Amex transfers 1:2."},
    {"id": "ihg", "name": "IHG One Rewards", "cpp": 0.6, "cards": {"chase": 1, "bilt": 1},
     "brands": [r"intercontinental", r"six senses", r"\bregent\b", r"\bkimpton\b", r"hotel indigo", r"crowne plaza",
                r"holiday inn", r"\bvoco\b", r"vignette collection", r"\bhualuxe\b", r"even hotel"],
     "url": "https://www.ihg.com/rewardsclub/us/en/redeem-rewards",
     "note": "Dynamic pricing. IHG credit-card holders get the 4th award night free."},
    {"id": "accor", "name": "Accor Live Limitless", "cpp": 2.1, "fixed": True, "cards": {"capone": 0.5, "citi": 0.5, "bilt": 0.67},
     "brands": [r"\braffles\b", r"\bfairmont\b", r"\bsofitel\b", r"\bpullman\b", r"\bswiss[oô]tel\b", r"\bmgallery\b",
                r"\bnovotel\b", r"\bmercure\b", r"\bibis\b", r"m[oö]venpick", r"banyan tree", r"\bso/", r"25hours",
                r"\bmondrian\b", r"\bsls\b", r"\bdelano\b", r"\brixos\b", r"\bpeppers\b"],
     "url": "https://all.accor.com/loyalty-program/user/points.en.shtml",
     "note": "Points are worth a fixed 2 euro cents each (2,000 points = 40 euros off any stay), so they never beat cash. Transfers from Capital One and Citi are 2:1, which makes them poor value."},
]
_HOTEL_RX = [(p["id"], [re.compile(b, re.I) for b in p["brands"]]) for p in HOTEL_PROGRAMS]


def hotel_program(name):
    """Hotel name -> loyalty program id ('' if independent or unknown)."""
    for pid, rxs in _HOTEL_RX:
        if any(r.search(name or "") for r in rxs):
            return pid
    return ""


ZONES = {"TYO": "NAsia", "SEL": "NAsia", "OSA": "NAsia"}  # everything else in Asia -> SAsia, Europe -> Europe


def zone(dest_code, region):
    return "Europe" if region == "Europe" else ZONES.get(dest_code, "SAsia")


def payload():
    return {"cards": CARDS, "alliances": ALLIANCES, "programs": PROGRAMS, "zones": ZONES,
            "hotel_programs": [{k: v for k, v in p.items() if k != "brands"} for p in HOTEL_PROGRAMS]}
