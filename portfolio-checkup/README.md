# Portfolio Checkup

A free, private portfolio risk review. Visitors drop in the spreadsheets their bank or broker gives them
(holdings, trade history, bank and credit-card transactions; Excel or CSV) and get a plain-English report:
risk level vs. their stated comfort, concentration, estimated yearly swings, "what if 2008 / 2020 / 2022 happened
again", fees, interest-rate and inflation exposure, cash runway, trading and cash-flow patterns, and educational
suggestions in the context of today's economy (FRED data).

## Privacy design (important for a public site)
- **Files never leave the visitor's device.** Spreadsheets are read with SheetJS and analysed in the browser
  (`static/parse.js`, `static/analyze.js`). The server has **no upload endpoint**: POST/PUT/DELETE return 405.
- The server stores nothing about visitors (no accounts, no database, no analytics, no cookies).
  The only browser storage is optional: the visitor's age/horizon/comfort answers if they tick "Remember".
- Strict Content-Security-Policy (`script-src 'self'`, `connect-src 'self'`): the page can't send data anywhere else.
- All scripts are self-hosted (`static/lib/xlsx.full.min.js` is SheetJS 0.20.3 from cdn.sheetjs.com).

## Run on this PC
Double-click `start.bat` (opens http://127.0.0.1:8770). Or: `python server.py`.
"Try it with sample files" loads fictional exports in Schwab, Fidelity, Merrill (.xlsx), Robinhood, Chase and Amex
formats from `static/samples/` (regenerate with `python make_samples.py`). `#demo` in the URL runs the demo directly.
`node test_node.js .` runs the parser + analysis on the samples without a browser.

## Put it online
It's one stateless Python process (stdlib + `requests`). Any host that runs a container works:
- **Render / Railway / Fly.io / Google Cloud Run / Azure Container Apps**: deploy this folder with the `Dockerfile`
  (listens on `$PORT`, default 8080). Put a domain on it; HTTPS is provided by the host.
- **A VPS**: `docker build -t portfolio-checkup . && docker run -p 8080:8080 portfolio-checkup` behind Caddy or nginx for HTTPS.
Environment: `HOST` (default 127.0.0.1; the Dockerfile sets 0.0.0.0) and `PORT`.
The FRED data is fetched server-side and cached for 6 hours (`/api/econ`); FRED's public CSV needs no key.

## Before launching publicly: checklist
- **Legal review.** The site gives *educational, general* observations and repeatedly says it isn't advice, but
  wording that looks like personalised recommendations to the public can trigger investment-adviser rules
  (SEC/state registration). Have a securities lawyer review the copy, disclaimers and a Terms of Use / Privacy Policy.
- **Privacy policy** stating that files are processed locally and not collected (true by design here; keep it true:
  don't add analytics scripts that could read page content).
- **Accuracy notes.** Asset-class volatilities, correlations and stress-period returns in `static/data.js` are rounded
  long-run assumptions; fund expense ratios change. Review them yearly. Unknown funds are flagged, not guessed silently.
- **Data licences.** FRED data is free with attribution (shown in the footer); some FRED series have source-specific
  terms (e.g. ICE BofA spreads); check them for commercial use. SheetJS Community Edition is Apache-2.0.

## Files
| File | What it does |
|------|--------------|
| `server.py` | Static files + `/api/econ` (FRED), security headers, rejects uploads |
| `static/parse.js` | Finds header rows, maps columns (Schwab, Fidelity, Vanguard, E*TRADE, Merrill, JPM, Robinhood, Webull, banks), detects holdings / trades / bank files |
| `static/analyze.js` | Classification, allocation, volatility model, stress tests, fees, rates, cash flow, findings |
| `static/data.js` | ~170 funds (class, fee), large-cap sectors, asset-class assumptions, stress periods |
| `static/app.js` | Upload UI, column fixing, report rendering |
