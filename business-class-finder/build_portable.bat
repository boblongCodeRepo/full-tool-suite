@echo off
rem Builds ONE self-contained file: dist\BusinessClassFinder.exe (no Python needed on the other PC; needs Edge or Chrome,
rem which Windows 10/11 have). Put the "BusinessClassFinder-data" folder next to the exe to carry your key,
rem settings and price history with it; otherwise the exe keeps them in %%LOCALAPPDATA%%\BusinessClassFinder.
cd /d "%~dp0"
.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check pyinstaller
.venv\Scripts\python.exe make_icon.py
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name BusinessClassFinder --icon icon.ico --add-data "static;static" ^
  --distpath dist --workpath build app.py
if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)
echo.
echo Built: %~dp0dist\BusinessClassFinder.exe
