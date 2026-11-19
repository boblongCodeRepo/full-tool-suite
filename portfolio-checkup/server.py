"""Portfolio Checkup - web server.

Privacy by design: visitors' spreadsheets are read and analysed entirely in their browser (static/app.js).
This server has NO upload endpoint and stores nothing about visitors. It only serves the site and public
economic data from FRED (Federal Reserve Bank of St. Louis), cached for a few hours.

Run locally:   python server.py            -> http://127.0.0.1:8770
Deploy:        set HOST=0.0.0.0 and PORT (most hosts set PORT); see README.md / Dockerfile.
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8770"))

S = requests.Session()
S.headers.update({"User-Agent": "PortfolioCheckup/1.0 (economic data for an educational site)"})

# id -> (label, transform)  transform: "level" | "yoy" (percent change vs a year earlier)
SERIES = {
    "DFF": "Federal funds rate", "DTB3": "3-month Treasury bill", "DGS2": "2-year Treasury", "DGS10": "10-year Treasury",
    "T10Y2Y": "10-year minus 2-year yield", "T10Y3M": "10-year minus 3-month yield", "CPIAUCSL": "Consumer prices (CPI)",
    "CPILFESL": "Core CPI", "UNRATE": "Unemployment rate", "BAMLH0A0HYM2": "High-yield bond spread",
    "RECPROUSM156N": "Recession probability", "MORTGAGE30US": "30-year mortgage rate", "VIXCLS": "Stock volatility (VIX)",
    "SP500": "S&P 500", "T10YIE": "10-year inflation expectations",
}
_CACHE = {"at": 0, "data": None}
_LOCK = threading.Lock()


def _fred(sid):
    r = S.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": sid}, timeout=30)
    r.raise_for_status()
    out = []
    for row in csv.reader(io.StringIO(r.text)):
        if len(row) < 2 or row[1] in ("", ".") or not row[0][:1].isdigit():
            continue
        try:
            out.append((row[0], float(row[1])))
        except ValueError:
            continue
    return out


def _at_or_before(series, iso):
    best = None
    for d, v in series:
        if d <= iso:
            best = (d, v)
        else:
            break
    return best


def econ():
    with _LOCK:
        if _CACHE["data"] and time.time() - _CACHE["at"] < 6 * 3600:
            return _CACHE["data"]
        raw, errors = {}, {}
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            futs = {sid: ex.submit(_fred, sid) for sid in SERIES}
            for sid, f in futs.items():
                try:
                    raw[sid] = f.result()
                except Exception as e:  # noqa: BLE001
                    errors[sid] = str(e)
        out = {"asof": datetime.utcnow().isoformat() + "Z", "errors": errors, "series": {}}
        for sid, s in raw.items():
            if not s:
                continue
            d, v = s[-1]
            yago = _at_or_before(s, f"{int(d[:4]) - 1}{d[4:]}")
            item = {"label": SERIES[sid], "date": d, "value": v, "year_ago": yago[1] if yago else None}
            if sid in ("CPIAUCSL", "CPILFESL") and yago:
                item["yoy"] = round((v / yago[1] - 1) * 100, 2)
            if sid == "SP500":
                hi = max(x[1] for x in s[-260 * 3:])
                item["from_high_pct"] = round((v / hi - 1) * 100, 1)
                item["high_3y"] = hi
            if sid == "UNRATE" and len(s) >= 15:  # Sahm rule: 3-mo avg minus lowest 3-mo avg of prior 12 months
                avg3 = [sum(x[1] for x in s[i - 2:i + 1]) / 3 for i in range(len(s) - 15, len(s))]
                item["sahm"] = round(avg3[-1] - min(avg3[:-1][-12:]), 2)
            if sid in ("T10Y2Y", "T10Y3M"):
                last90 = [x[1] for x in s[-65:]]
                item["inverted_days_3m"] = sum(1 for x in last90 if x < 0)
            item["history"] = [[x[0], x[1]] for x in s[-260 * 2:]] if sid in ("DGS10", "DTB3", "SP500", "T10Y2Y") else []
            out["series"][sid] = item
        _CACHE.update(at=time.time(), data=out)
        return out


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".csv": "text/csv; charset=utf-8",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".json": "application/json", ".txt": "text/plain; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "PortfolioCheckup"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/api/econ":
            try:
                return self._send(200, json.dumps(econ()).encode(), "application/json", "public, max-age=1800")
            except Exception as e:  # noqa: BLE001
                return self._send(502, json.dumps({"error": f"economic data unavailable: {e}"}).encode(), "application/json")
        if u.path == "/api/health":
            return self._send(200, b'{"ok":true}', "application/json")
        rel = u.path.lstrip("/") or "index.html"
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        return self._send(200, f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"),
                          "public, max-age=3600" if "/lib/" in u.path else "no-cache")

    def do_POST(self):  # noqa: N802  - deliberately no uploads: files are processed in the browser
        self._send(405, b"uploads are not accepted; files are processed in your browser", "text/plain")

    do_PUT = do_DELETE = do_PATCH = do_POST


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"Portfolio Checkup on http://{HOST}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
