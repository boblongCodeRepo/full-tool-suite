@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up Python packages...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
)
start "" http://localhost:8503
".venv\Scripts\python.exe" -m streamlit run app.py
