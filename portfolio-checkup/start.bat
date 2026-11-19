@echo off
rem Runs Portfolio Checkup on this PC and opens it in your browser (http://127.0.0.1:8770). Close this window to stop.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
)
start "" http://127.0.0.1:8770/
.venv\Scripts\python.exe server.py
