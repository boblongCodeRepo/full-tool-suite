@echo off
rem Collect new deals every Sunday at 7:00 AM, even if the app is closed.
rem (The app also refreshes itself on first open after Sunday 7 AM.)
cd /d "%~dp0"
schtasks /Create /F /TN "Travel Deals Hub weekly update" /SC WEEKLY /D SUN /ST 07:00 ^
  /TR "\"%~dp0.venv\Scripts\pythonw.exe\" \"%~dp0fetch.py\""
echo.
echo Weekly Sunday 7 AM update installed. Remove it with:
echo   schtasks /Delete /TN "Travel Deals Hub weekly update" /F
pause
