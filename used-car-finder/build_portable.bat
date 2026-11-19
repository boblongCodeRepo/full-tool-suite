@echo off
rem Builds ONE self-contained file: dist\UsedCarFinder.exe (no Python needed on the other PC; uses Edge/Chrome).
rem Put the "UsedCarFinder-data" folder next to the exe to carry your key, saved searches and settings with it.
cd /d "%~dp0"
.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check pyinstaller
.venv\Scripts\python.exe make_icon.py
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name UsedCarFinder --icon icon.ico --add-data "static;static" ^
  --distpath dist --workpath build app.py
if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)
echo.
echo Built: %~dp0dist\UsedCarFinder.exe
