/* Reference data for Portfolio Checkup. All figures are approximate, long-run, educational assumptions. */
"use strict";

/* Asset classes: long-run volatility (%/yr), a rough long-run return assumption (%/yr), bond duration (yrs), and
   approximate peak-to-trough results in four historical stress periods (%). Crypto did not exist in 2000/2008:
   those figures are hypothetical. Sources: index histories (S&P 500, MSCI EAFE/EM, Bloomberg US Aggregate,
   ICE BofA High Yield, FTSE Nareit, Bloomberg Commodity, gold, Bitcoin), rounded. */
const CLASSES = {
  us_stock:   { label: "US stocks",              color: "#3b82f6", vol: 16, ret: 6.5, s: { gfc: -55, dotcom: -49, covid: -34, y2022: -25 } },
  intl_stock: { label: "International stocks",   color: "#06b6d4", vol: 17, ret: 7.0, s: { gfc: -60, dotcom: -48, covid: -33, y2022: -27 } },
  em_stock:   { label: "Emerging-market stocks", color: "#14b8a6", vol: 22, ret: 7.5, s: { gfc: -65, dotcom: -45, covid: -32, y2022: -30 } },
  single:     { label: "Individual stocks",      color: "#8b5cf6", vol: 30, ret: 6.5, s: { gfc: -60, dotcom: -60, covid: -40, y2022: -30 } },
  us_bond:    { label: "US bonds",               color: "#22c55e", vol: 5,  ret: 4.5, dur: 6, s: { gfc: 7, dotcom: 30, covid: 1, y2022: -17 } },
  tips:       { label: "Inflation-protected bonds", color: "#84cc16", vol: 6, ret: 4.3, dur: 7, s: { gfc: -5, dotcom: 35, covid: -2, y2022: -14 } },
  muni:       { label: "Municipal bonds",        color: "#4ade80", vol: 5,  ret: 3.5, dur: 6, s: { gfc: -8, dotcom: 20, covid: -10, y2022: -12 } },
  intl_bond:  { label: "International bonds",    color: "#86efac", vol: 5,  ret: 4.0, dur: 7, s: { gfc: 3, dotcom: 20, covid: -2, y2022: -15 } },
  hy_bond:    { label: "High-yield bonds",       color: "#eab308", vol: 9,  ret: 6.0, dur: 4, s: { gfc: -33, dotcom: -10, covid: -21, y2022: -15 } },
  cash:       { label: "Cash & money market",    color: "#94a3b8", vol: 0.5, ret: 3.5, dur: 0.2, s: { gfc: 2, dotcom: 10, covid: 0, y2022: 1 } },
  reit:       { label: "Real estate (REITs)",    color: "#f97316", vol: 20, ret: 6.0, s: { gfc: -70, dotcom: 40, covid: -42, y2022: -32 } },
  commodity:  { label: "Commodities",            color: "#a16207", vol: 17, ret: 3.5, s: { gfc: -55, dotcom: -10, covid: -35, y2022: 20 } },
  gold:       { label: "Gold & precious metals", color: "#facc15", vol: 15, ret: 4.0, s: { gfc: 20, dotcom: 12, covid: -3, y2022: -10 } },
  crypto:     { label: "Crypto",                 color: "#ec4899", vol: 70, ret: null, s: { gfc: -75, dotcom: -75, covid: -50, y2022: -70 } },
  other:      { label: "Other / unclassified",   color: "#64748b", vol: 16, ret: 6.0, s: { gfc: -50, dotcom: -45, covid: -30, y2022: -20 } },
};
const STRESS = [
  { id: "gfc", name: "2008 financial crisis", when: "Oct 2007 - Mar 2009" },
  { id: "covid", name: "2020 COVID crash", when: "Feb - Mar 2020" },
  { id: "y2022", name: "2022 inflation & rate hikes", when: "Jan - Oct 2022" },
  { id: "dotcom", name: "2000-02 dot-com bust", when: "Mar 2000 - Oct 2002" },
];
const GROUP = { us_stock: "eq", intl_stock: "eq", em_stock: "eq", single: "eq", other: "eq", us_bond: "bd", tips: "bd", muni: "bd",
  intl_bond: "bd", hy_bond: "hy", cash: "cs", reit: "re", commodity: "cm", gold: "au", crypto: "cr" };
const CORR = { // correlation between groups (long-run, rounded)
  eq: { eq: 0.85, bd: 0.1, hy: 0.65, cs: 0, re: 0.75, cm: 0.35, au: 0.05, cr: 0.4 },
  bd: { bd: 0.85, hy: 0.35, cs: 0.1, re: 0.3, cm: 0, au: 0.3, cr: 0.05 },
  hy: { hy: 0.9, cs: 0, re: 0.6, cm: 0.4, au: 0.1, cr: 0.3 },
  cs: { cs: 1, re: 0, cm: 0, au: 0, cr: 0 },
  re: { re: 0.9, cm: 0.3, au: 0.1, cr: 0.3 },
  cm: { cm: 0.9, au: 0.4, cr: 0.2 }, au: { au: 1, cr: 0.15 }, cr: { cr: 0.9 },
};
const corr = (a, b) => (CORR[a] && CORR[a][b] !== undefined ? CORR[a][b] : CORR[b][a]);

/* Funds & ETFs: ticker -> [asset class, expense ratio %, name, optional {split} for allocation funds]. */
const F = (c, er, n, mix) => ({ c, er, n, mix });
const FUNDS = {
  // US total market / large cap
  VTI: F("us_stock", 0.03, "Vanguard Total Stock Market ETF"), VTSAX: F("us_stock", 0.04, "Vanguard Total Stock Market Index Admiral"),
  ITOT: F("us_stock", 0.03, "iShares Core S&P Total US Stock"), SCHB: F("us_stock", 0.03, "Schwab US Broad Market ETF"),
  SWTSX: F("us_stock", 0.03, "Schwab Total Stock Market Index"), FSKAX: F("us_stock", 0.015, "Fidelity Total Market Index"),
  FZROX: F("us_stock", 0, "Fidelity ZERO Total Market Index"), VOO: F("us_stock", 0.03, "Vanguard S&P 500 ETF"),
  VFIAX: F("us_stock", 0.04, "Vanguard 500 Index Admiral"), SPY: F("us_stock", 0.0945, "SPDR S&P 500 ETF"),
  IVV: F("us_stock", 0.03, "iShares Core S&P 500"), SPLG: F("us_stock", 0.02, "SPDR Portfolio S&P 500"),
  FXAIX: F("us_stock", 0.015, "Fidelity 500 Index"), SWPPX: F("us_stock", 0.02, "Schwab S&P 500 Index"),
  SCHX: F("us_stock", 0.03, "Schwab US Large-Cap ETF"), VV: F("us_stock", 0.04, "Vanguard Large-Cap ETF"),
  QQQ: F("us_stock", 0.2, "Invesco QQQ (Nasdaq-100)"), QQQM: F("us_stock", 0.15, "Invesco Nasdaq 100 ETF"),
  VUG: F("us_stock", 0.04, "Vanguard Growth ETF"), VTV: F("us_stock", 0.04, "Vanguard Value ETF"), SCHG: F("us_stock", 0.04, "Schwab US Large-Cap Growth"),
  SCHD: F("us_stock", 0.06, "Schwab US Dividend Equity"), VIG: F("us_stock", 0.05, "Vanguard Dividend Appreciation"),
  VYM: F("us_stock", 0.06, "Vanguard High Dividend Yield"), DIA: F("us_stock", 0.16, "SPDR Dow Jones Industrial Average"),
  RSP: F("us_stock", 0.2, "Invesco S&P 500 Equal Weight"), MGK: F("us_stock", 0.07, "Vanguard Mega Cap Growth"),
  IWF: F("us_stock", 0.19, "iShares Russell 1000 Growth"), IWD: F("us_stock", 0.19, "iShares Russell 1000 Value"), IWB: F("us_stock", 0.15, "iShares Russell 1000"),
  // mid / small
  VO: F("us_stock", 0.04, "Vanguard Mid-Cap ETF"), IJH: F("us_stock", 0.05, "iShares Core S&P Mid-Cap"), VB: F("us_stock", 0.05, "Vanguard Small-Cap ETF"),
  IJR: F("us_stock", 0.06, "iShares Core S&P Small-Cap"), IWM: F("us_stock", 0.19, "iShares Russell 2000"), VBR: F("us_stock", 0.07, "Vanguard Small-Cap Value"),
  AVUV: F("us_stock", 0.25, "Avantis US Small Cap Value"), VXF: F("us_stock", 0.05, "Vanguard Extended Market"), FSMDX: F("us_stock", 0.025, "Fidelity Mid Cap Index"),
  // sector ETFs (counted as US stocks, sector noted)
  XLK: F("us_stock", 0.08, "Technology Select Sector SPDR"), VGT: F("us_stock", 0.09, "Vanguard Information Technology"),
  XLF: F("us_stock", 0.08, "Financial Select Sector SPDR"), XLE: F("us_stock", 0.08, "Energy Select Sector SPDR"),
  XLV: F("us_stock", 0.08, "Health Care Select Sector SPDR"), XLY: F("us_stock", 0.08, "Consumer Discretionary SPDR"),
  XLP: F("us_stock", 0.08, "Consumer Staples SPDR"), XLI: F("us_stock", 0.08, "Industrial Select Sector SPDR"),
  XLU: F("us_stock", 0.08, "Utilities Select Sector SPDR"), XLC: F("us_stock", 0.08, "Communication Services SPDR"),
  SMH: F("us_stock", 0.35, "VanEck Semiconductor ETF"), SOXX: F("us_stock", 0.35, "iShares Semiconductor ETF"),
  ARKK: F("us_stock", 0.75, "ARK Innovation ETF"), TQQQ: F("us_stock", 0.84, "ProShares UltraPro QQQ (3x leveraged)"),
  SQQQ: F("us_stock", 0.95, "ProShares UltraPro Short QQQ (3x inverse)"), SSO: F("us_stock", 0.89, "ProShares Ultra S&P 500 (2x)"),
  JEPI: F("us_stock", 0.35, "JPMorgan Equity Premium Income"), JEPQ: F("us_stock", 0.35, "JPMorgan Nasdaq Equity Premium Income"),
  // international
  VXUS: F("intl_stock", 0.05, "Vanguard Total International Stock ETF"), VTIAX: F("intl_stock", 0.09, "Vanguard Total Intl Stock Index Admiral"),
  IXUS: F("intl_stock", 0.07, "iShares Core MSCI Total Intl Stock"), VEA: F("intl_stock", 0.03, "Vanguard Developed Markets ETF"),
  IEFA: F("intl_stock", 0.07, "iShares Core MSCI EAFE"), EFA: F("intl_stock", 0.32, "iShares MSCI EAFE"), SCHF: F("intl_stock", 0.03, "Schwab International Equity"),
  FTIHX: F("intl_stock", 0.06, "Fidelity Total International Index"), FZILX: F("intl_stock", 0, "Fidelity ZERO International Index"),
  SWISX: F("intl_stock", 0.06, "Schwab International Index"), VEU: F("intl_stock", 0.04, "Vanguard FTSE All-World ex-US"),
  VGK: F("intl_stock", 0.06, "Vanguard FTSE Europe"), EWJ: F("intl_stock", 0.5, "iShares MSCI Japan"), VT: F("us_stock", 0.06, "Vanguard Total World Stock", { us_stock: 0.62, intl_stock: 0.38 }),
  ACWI: F("us_stock", 0.32, "iShares MSCI ACWI", { us_stock: 0.64, intl_stock: 0.36 }),
  VWO: F("em_stock", 0.07, "Vanguard Emerging Markets ETF"), IEMG: F("em_stock", 0.09, "iShares Core MSCI Emerging Markets"),
  EEM: F("em_stock", 0.7, "iShares MSCI Emerging Markets"), SCHE: F("em_stock", 0.07, "Schwab Emerging Markets Equity"),
  FXI: F("em_stock", 0.74, "iShares China Large-Cap"), INDA: F("em_stock", 0.61, "iShares MSCI India"), VEMAX: F("em_stock", 0.13, "Vanguard Emerging Markets Index Admiral"),
  // bonds
  BND: F("us_bond", 0.03, "Vanguard Total Bond Market ETF"), VBTLX: F("us_bond", 0.04, "Vanguard Total Bond Market Index Admiral"),
  AGG: F("us_bond", 0.03, "iShares Core US Aggregate Bond"), SCHZ: F("us_bond", 0.03, "Schwab US Aggregate Bond"),
  FXNAX: F("us_bond", 0.025, "Fidelity US Bond Index"), SWAGX: F("us_bond", 0.04, "Schwab US Aggregate Bond Index"),
  BIV: F("us_bond", 0.03, "Vanguard Intermediate-Term Bond"), BSV: F("us_bond", 0.03, "Vanguard Short-Term Bond"),
  VGIT: F("us_bond", 0.03, "Vanguard Intermediate-Term Treasury"), VGSH: F("us_bond", 0.03, "Vanguard Short-Term Treasury"),
  VGLT: F("us_bond", 0.03, "Vanguard Long-Term Treasury"), SHY: F("us_bond", 0.15, "iShares 1-3 Year Treasury"),
  IEF: F("us_bond", 0.15, "iShares 7-10 Year Treasury"), TLT: F("us_bond", 0.15, "iShares 20+ Year Treasury"),
  GOVT: F("us_bond", 0.05, "iShares US Treasury Bond"), LQD: F("us_bond", 0.14, "iShares Investment Grade Corporate"),
  VCIT: F("us_bond", 0.04, "Vanguard Intermediate-Term Corporate"), VCSH: F("us_bond", 0.04, "Vanguard Short-Term Corporate"),
  BIL: F("cash", 0.1356, "SPDR 1-3 Month T-Bill"), SGOV: F("cash", 0.09, "iShares 0-3 Month Treasury"), USFR: F("cash", 0.15, "WisdomTree Floating Rate Treasury"),
  TIP: F("tips", 0.18, "iShares TIPS Bond"), SCHP: F("tips", 0.03, "Schwab US TIPS"), VTIP: F("tips", 0.04, "Vanguard Short-Term TIPS"), VAIPX: F("tips", 0.1, "Vanguard Inflation-Protected Securities Admiral"),
  MUB: F("muni", 0.05, "iShares National Muni Bond"), VTEB: F("muni", 0.03, "Vanguard Tax-Exempt Bond"), VWIUX: F("muni", 0.09, "Vanguard Intermediate-Term Tax-Exempt Admiral"),
  BNDX: F("intl_bond", 0.07, "Vanguard Total International Bond"), IAGG: F("intl_bond", 0.07, "iShares Core International Aggregate Bond"),
  HYG: F("hy_bond", 0.49, "iShares iBoxx High Yield Corporate"), JNK: F("hy_bond", 0.4, "SPDR Bloomberg High Yield"), USHY: F("hy_bond", 0.08, "iShares Broad USD High Yield"),
  EMB: F("hy_bond", 0.39, "iShares JP Morgan USD Emerging Markets Bond"),
  // real estate, commodities, gold, crypto
  VNQ: F("reit", 0.13, "Vanguard Real Estate ETF"), SCHH: F("reit", 0.07, "Schwab US REIT"), IYR: F("reit", 0.39, "iShares US Real Estate"), XLRE: F("reit", 0.08, "Real Estate Select Sector SPDR"),
  GLD: F("gold", 0.4, "SPDR Gold Shares"), IAU: F("gold", 0.25, "iShares Gold Trust"), GLDM: F("gold", 0.1, "SPDR Gold MiniShares"), SLV: F("gold", 0.5, "iShares Silver Trust"),
  DBC: F("commodity", 0.85, "Invesco DB Commodity Index"), PDBC: F("commodity", 0.59, "Invesco Optimum Yield Diversified Commodity"), USO: F("commodity", 0.6, "United States Oil Fund"),
  IBIT: F("crypto", 0.25, "iShares Bitcoin Trust"), FBTC: F("crypto", 0.25, "Fidelity Wise Origin Bitcoin"), GBTC: F("crypto", 1.5, "Grayscale Bitcoin Trust"),
  BITO: F("crypto", 0.95, "ProShares Bitcoin Strategy"), ETHA: F("crypto", 0.25, "iShares Ethereum Trust"), BTC: F("crypto", 0, "Bitcoin"), ETH: F("crypto", 0, "Ethereum"),
  // balanced / allocation funds (split)
  VBIAX: F("us_stock", 0.07, "Vanguard Balanced Index Admiral", { us_stock: 0.6, us_bond: 0.4 }),
  VWELX: F("us_stock", 0.25, "Vanguard Wellington", { us_stock: 0.65, us_bond: 0.35 }), VWINX: F("us_stock", 0.23, "Vanguard Wellesley Income", { us_stock: 0.38, us_bond: 0.62 }),
  AOR: F("us_stock", 0.15, "iShares Core 60/40 Balanced", { us_stock: 0.37, intl_stock: 0.23, us_bond: 0.4 }), AOA: F("us_stock", 0.15, "iShares Core 80/20 Aggressive", { us_stock: 0.47, intl_stock: 0.33, us_bond: 0.2 }),
  AOM: F("us_stock", 0.15, "iShares Core 40/60 Moderate", { us_stock: 0.25, intl_stock: 0.15, us_bond: 0.6 }),
  // popular actively managed funds (approximate expense ratios, A shares where applicable)
  AGTHX: F("us_stock", 0.61, "American Funds Growth Fund of America A"), AIVSX: F("us_stock", 0.58, "American Funds Investment Co of America A"),
  ANWPX: F("us_stock", 0.73, "American Funds New Perspective A", { us_stock: 0.55, intl_stock: 0.45 }), AEPGX: F("intl_stock", 0.82, "American Funds EuroPacific Growth A"),
  ABALX: F("us_stock", 0.56, "American Funds American Balanced A", { us_stock: 0.6, us_bond: 0.4 }), FCNTX: F("us_stock", 0.39, "Fidelity Contrafund"),
  FBGRX: F("us_stock", 0.48, "Fidelity Blue Chip Growth"), FMAGX: F("us_stock", 0.52, "Fidelity Magellan"), PRGFX: F("us_stock", 0.63, "T. Rowe Price Growth Stock"),
  TRBCX: F("us_stock", 0.69, "T. Rowe Price Blue Chip Growth"), DODGX: F("us_stock", 0.51, "Dodge & Cox Stock"), PTTRX: F("us_bond", 0.47, "PIMCO Total Return Instl"),
  DODIX: F("us_bond", 0.41, "Dodge & Cox Income"), VPMAX: F("us_stock", 0.31, "Vanguard PRIMECAP Admiral"), VWUAX: F("us_stock", 0.25, "Vanguard US Growth Admiral"),
  // money market / sweep
  SPAXX: F("cash", 0.42, "Fidelity Government Money Market"), FDRXX: F("cash", 0.37, "Fidelity Government Cash Reserves"), FZFXX: F("cash", 0.42, "Fidelity Treasury Money Market"),
  VMFXX: F("cash", 0.11, "Vanguard Federal Money Market"), VMRXX: F("cash", 0.1, "Vanguard Cash Reserves Federal MM"), SWVXX: F("cash", 0.34, "Schwab Value Advantage Money"),
  SNVXX: F("cash", 0.34, "Schwab Government Money"), SNSXX: F("cash", 0.34, "Schwab US Treasury Money"), FCASH: F("cash", 0, "Fidelity cash"),
};
const CASH_RE = /money market|cash|sweep|settlement fund|fdic|deposit program|bank deposit|core position|treasury money|government money|cash reserves/i;

/* Individual US stocks -> sector (largest companies; others are "Unknown sector"). */
const SECTORS = {
  Technology: "AAPL MSFT NVDA AVGO ORCL CRM ADBE AMD CSCO ACN IBM INTU TXN QCOM NOW AMAT MU LRCX ADI KLAC SNPS CDNS PANW CRWD ANET INTC PLTR APH MSI FTNT WDAY ADSK DELL HPQ HPE SMCI ARM MRVL SHOP TSM ASML SAP SNOW NET DDOG MDB ZS TEAM UBER APP",
  Communication: "GOOGL GOOG META NFLX DIS CMCSA T VZ TMUS CHTR EA TTWO WBD SPOT RDDT PINS SNAP",
  "Consumer discretionary": "AMZN TSLA HD MCD LOW NKE SBUX TJX BKNG CMG ORLY AZO MAR HLT GM F ROST YUM LULU DHI LEN RCL CCL ABNB DASH EBAY ETSY RIVN LCID",
  "Consumer staples": "WMT PG COST KO PEP PM MO MDLZ CL TGT KHC GIS KMB STZ KR SYY HSY KDP MNST EL",
  "Health care": "LLY UNH JNJ ABBV MRK TMO ABT PFE AMGN DHR ISRG BMY GILD VRTX CVS MDT SYK ELV CI REGN ZTS BSX HCA MRNA BIIB HUM NVO",
  Financials: "BRK.B BRK-B BRKB JPM V MA BAC WFC GS MS SCHW AXP BLK C SPGI CB PGR MMC PYPL COF USB PNC ICE CME AON MET AIG TRV HOOD SOFI COIN",
  Industrials: "GE CAT RTX HON UNP BA LMT DE UPS ADP ETN GD NOC WM ITW CSX NSC FDX EMR PH MMM JCI CARR LUV DAL UAL AAL GEV",
  Energy: "XOM CVX COP EOG SLB OXY MPC PSX VLO WMB KMI OKE HAL DVN BKR FANG",
  Utilities: "NEE SO DUK CEG AEP SRE D EXC XEL PCG ED VST",
  "Real estate": "PLD AMT EQIX WELL SPG O PSA CCI DLR VICI",
  Materials: "LIN SHW APD ECL FCX NEM NUE DOW DD CTVA",
};
const SECTOR_OF = {};
for (const [sec, list] of Object.entries(SECTORS)) list.split(/\s+/).forEach((t) => (SECTOR_OF[t] = sec));
const SECTOR_ETF = { XLK: "Technology", VGT: "Technology", SMH: "Technology", SOXX: "Technology", XLF: "Financials", XLE: "Energy", XLV: "Health care",
  XLY: "Consumer discretionary", XLP: "Consumer staples", XLI: "Industrials", XLU: "Utilities", XLC: "Communication" };
/* Cheaper index alternatives by class (for fee suggestions). */
const CHEAP = { us_stock: "a total-market or S&P 500 index fund (about 0.02-0.05%)", intl_stock: "a total international index fund (about 0.05-0.10%)",
  em_stock: "an emerging-markets index fund (about 0.07-0.15%)", us_bond: "a total bond market index fund (about 0.03-0.05%)", intl_bond: "an international bond index fund (about 0.07%)",
  tips: "a TIPS index fund (about 0.03-0.05%)", muni: "a municipal bond index fund (about 0.03-0.05%)", hy_bond: "a broad high-yield index fund (about 0.08-0.2%)",
  reit: "a REIT index fund (about 0.07-0.13%)", gold: "a low-cost gold ETF (about 0.1%)", crypto: "a spot bitcoin ETF (about 0.15-0.25%)" };
