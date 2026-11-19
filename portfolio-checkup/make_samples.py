"""Writes realistic (fictional) sample exports into static/samples/ in each institution's usual layout."""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

OUT = Path(__file__).resolve().parent / "static" / "samples"
OUT.mkdir(parents=True, exist_ok=True)
random.seed(11)
usd = lambda v: ("-" if v < 0 else "") + "${:,.2f}".format(abs(v))  # noqa: E731

# --- Schwab positions (title row, "Cash & Cash Investments" and "Account Total" rows)
rows = [("AAPL", "APPLE INC", 310, 228.40, 21500), ("NVDA", "NVIDIA CORP", 600, 182.10, 14200), ("MSFT", "MICROSOFT CORP", 85, 498.20, 24100),
        ("VTI", "VANGUARD TOTAL STOCK MARKET ETF", 220, 318.70, 52000), ("QQQ", "INVESCO QQQ TRUST", 60, 602.30, 25500),
        ("TSLA", "TESLA INC", 40, 401.50, 19800), ("ARKK", "ARK INNOVATION ETF", 150, 71.20, 13900)]
with open(OUT / "schwab_positions.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f, quoting=csv.QUOTE_ALL)
    w.writerow(["Positions for account Individual ...321 as of 09:41 AM ET, 11/09/2026"] + [""] * 15)
    w.writerow(["Symbol", "Description", "Qty (Quantity)", "Price", "Price Chng $ (Price Change $)", "Price Chng % (Price Change %)", "Mkt Val (Market Value)",
                "Day Chng $ (Day Change $)", "Day Chng % (Day Change %)", "Cost Basis", "Gain $ (Gain/Loss $)", "Gain % (Gain/Loss %)", "Reinvest?",
                "Reinvest Capital Gains?", "% of Acct (% of Account)", "Security Type"])
    tot = 0
    for s, d, q, p, cost in rows:
        mv = q * p
        tot += mv
        typ = "ETFs & Closed End Funds" if s in ("VTI", "QQQ", "ARKK") else "Equity"
        w.writerow([s, d, f"{q:,}", f"${p:,.2f}", "$1.12", "0.5%", usd(mv), "$120.00", "0.4%", usd(cost), usd(mv - cost), f"{(mv / cost - 1) * 100:.2f}%", "No", "N/A", "", typ])
    w.writerow(["Cash & Cash Investments", "--", "--", "--", "--", "--", usd(18450.22), "--", "--", "--", "--", "--", "--", "--", "", "Cash and Money Market"])
    w.writerow(["Account Total", "--", "--", "--", "--", "--", usd(tot + 18450.22), "--", "--", "--", "--", "--", "--", "--", "100%", "--"])

# --- Fidelity positions (Type = account type, SPAXX** core position, disclaimer footer)
fid = [("FXAIX", "FIDELITY 500 INDEX FUND", 410.2, 236.10, 61000), ("FFFHX", "FIDELITY FREEDOM 2050 FUND", 2900, 17.85, 41000),
       ("AGTHX", "AMERICAN FUNDS GROWTH FUND OF AMERICA CL A", 520, 82.40, 30000), ("FXNAX", "FIDELITY US BOND INDEX FUND", 2100, 10.42, 23500),
       ("VNQ", "VANGUARD REAL ESTATE ETF", 120, 91.30, 11800)]
with open(OUT / "fidelity_positions.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Account Number", "Account Name", "Symbol", "Description", "Quantity", "Last Price", "Last Price Change", "Current Value", "Today's Gain/Loss Dollar",
                "Today's Gain/Loss Percent", "Total Gain/Loss Dollar", "Total Gain/Loss Percent", "Percent Of Account", "Cost Basis Total", "Average Cost Basis", "Type"])
    w.writerow(["Z12345678", "ROTH IRA", "SPAXX**", "HELD IN MONEY MARKET", "", "", "", usd(6240.10), "", "", "", "", "3.1%", "", "", "Cash"])
    for s, d, q, p, cost in fid:
        mv = q * p
        w.writerow(["Z12345678", "ROTH IRA", s, d, q, usd(p), "+$0.12", usd(mv), "+$45.10", "+0.21%", usd(mv - cost), f"{(mv / cost - 1) * 100:+.2f}%", "", usd(cost), usd(cost / q), "Cash"])
    w.writerow([])
    w.writerow(['"The data and information in this spreadsheet is provided to you solely for your use and is not for distribution."'])
    w.writerow(["Date downloaded 11/09/2026 9:41 AM ET"])

# --- Merrill-style holdings workbook (.xlsx with title rows)
wb = Workbook()
ws = wb.active
ws.title = "Holdings"
ws.append(["Merrill Edge - Holdings"]); ws.append(["Account: CMA ...4471", "As of 11/07/2026"]); ws.append([])
ws.append(["Symbol", "Description", "Quantity", "Price ($)", "Value ($)", "Unrealized Gain/Loss ($)", "Cost Basis ($)"])
mer = [("JPM", "JPMORGAN CHASE & CO", 120, 298.40, 21000), ("BND", "VANGUARD TOTAL BOND MARKET ETF", 300, 73.10, 23300),
       ("912797LB1", "US TREASURY BILL 0% 03/12/2027", 15000, 0.987, 14700), ("GLD", "SPDR GOLD SHARES", 25, 365.20, 6100), ("IBIT", "ISHARES BITCOIN TRUST ETF", 260, 58.30, 17500)]
for s, d, q, p, cost in mer:
    ws.append([s, d, q, p, round(q * p, 2), round(q * p - cost, 2), cost])
ws.append(["", "Total", "", "", sum(q * p for _, _, q, p, _ in mer), "", ""])
wb.save(OUT / "merrill_holdings.xlsx")

# --- Robinhood activity (Trans Codes, parentheses for negatives)
start = date(2026, 5, 4)
with open(OUT / "robinhood_activity.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f, quoting=csv.QUOTE_ALL)
    w.writerow(["Activity Date", "Process Date", "Settle Date", "Instrument", "Description", "Trans Code", "Quantity", "Price", "Amount"])
    d = start
    while d < date(2026, 11, 7):
        if d.weekday() < 5:
            for _ in range(random.choice([0, 0, 1, 1, 2])):
                sym = random.choice(["TSLA", "NVDA", "PLTR", "AMD", "SOFI", "SPY"])
                px = {"TSLA": 390, "NVDA": 175, "PLTR": 160, "AMD": 210, "SOFI": 24, "SPY": 690}[sym] * random.uniform(0.9, 1.1)
                qty = random.choice([1, 2, 3, 5, 10])
                code = random.choice(["Buy", "Sell"])
                amt = qty * px
                w.writerow([d.strftime("%m/%d/%Y")] * 3 + [sym, f"{sym} order", code, qty, f"${px:,.2f}", f"(${amt:,.2f})" if code == "Buy" else f"${amt:,.2f}"])
            if d.day == 15:
                w.writerow([d.strftime("%m/%d/%Y")] * 3 + ["", "ACH Deposit", "ACH", "", "", "$1,000.00"])
            if d.day in (1, 2) and d.weekday() < 5:
                w.writerow([d.strftime("%m/%d/%Y")] * 3 + ["SPY", "Cash Div: R/D 2026 P/D 2026", "CDIV", "", "", "$41.20"])
                w.writerow([d.strftime("%m/%d/%Y")] * 3 + ["", "Gold Subscription Fee", "GOLD", "", "", "($5.00)"])
        d += timedelta(days=1)

# --- Chase checking (Details/Posting Date/Description/Amount/Type/Balance)
bal = 9200.0
lines = []
d = date(2026, 7, 1)
while d <= date(2026, 11, 5):
    if d.day in (1,):
        lines.append((d, "DEBIT", "ZELLE PAYMENT TO RIVERVIEW APTS RENT", -2650.00, "QUICKPAY_DEBIT"))
    if d.weekday() == 4 and (d - date(2026, 7, 3)).days % 14 == 0:
        lines.append((d, "CREDIT", "ACME CORP PAYROLL PPD ID: 999", 3480.55, "ACH_CREDIT"))
    if d.day == 5:
        lines.append((d, "DEBIT", "PEPCO BILL PAY", -round(random.uniform(90, 160), 2), "ACH_DEBIT"))
        lines.append((d, "DEBIT", "VERIZON WIRELESS PAYMENTS", -85.00, "ACH_DEBIT"))
    if d.day == 8:
        lines.append((d, "DEBIT", "AMERICAN EXPRESS ACH PMT", -round(random.uniform(1300, 1900), 2), "ACH_DEBIT"))
        lines.append((d, "DEBIT", "ONLINE TRANSFER TO SAV ...7781", -500.00, "ACCT_XFER"))
    if d.day == 12:
        lines.append((d, "DEBIT", "GEICO AUTO INSURANCE", -142.30, "ACH_DEBIT"))
    if d.day == 15:
        lines.append((d, "DEBIT", "ROBINHOOD DEBITS", -1000.00, "ACH_DEBIT"))
    if random.random() < 0.25:
        m = random.choice(["SAFEWAY #1234", "CVS/PHARMACY #0412", "SHELL OIL 5741", "WMATA SMARTRIP", "TARGET T-1234"])
        lines.append((d, "DEBIT", m, -round(random.uniform(12, 140), 2), "DEBIT_CARD"))
    d += timedelta(days=1)
with open(OUT / "chase_checking.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Details", "Posting Date", "Description", "Amount", "Type", "Balance", "Check or Slip #"])
    for d, det, desc, amt, typ in sorted(lines, reverse=True):
        pass
    out = []
    for d, det, desc, amt, typ in sorted(lines):
        bal += amt
        out.append([det, d.strftime("%m/%d/%Y"), desc, f"{amt:.2f}", typ, f"{bal:.2f}", ""])
    for r in reversed(out):
        w.writerow(r)

# --- Amex card (charges positive, payments negative)
with open(OUT / "amex_card.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Description", "Amount"])
    d = date(2026, 7, 1)
    while d <= date(2026, 11, 5):
        if d.day == 3:
            w.writerow([d.strftime("%m/%d/%Y"), "NETFLIX.COM", "17.99"])
            w.writerow([d.strftime("%m/%d/%Y"), "SPOTIFY USA", "11.99"])
            w.writerow([d.strftime("%m/%d/%Y"), "APPLE.COM/BILL", "9.99"])
            w.writerow([d.strftime("%m/%d/%Y"), "PLANET FITNESS", "24.99"])
        if d.day == 9:
            w.writerow([d.strftime("%m/%d/%Y"), "AUTOPAY PAYMENT - THANK YOU", f"-{random.uniform(1300, 1900):.2f}"])
        if random.random() < 0.5:
            m, lo, hi = random.choice([("WHOLE FOODS MARKET", 40, 180), ("TRADER JOE'S #658", 30, 110), ("STARBUCKS STORE 1234", 5, 14),
                                       ("DOORDASH*CHIPOTLE", 18, 45), ("UBER *TRIP", 12, 38), ("AMAZON.COM*2K4", 15, 120), ("SWEETGREEN DUPONT", 14, 22)])
            w.writerow([d.strftime("%m/%d/%Y"), m, f"{random.uniform(lo, hi):.2f}"])
        d += timedelta(days=1)
print("samples written:", sorted(p.name for p in OUT.iterdir()))
