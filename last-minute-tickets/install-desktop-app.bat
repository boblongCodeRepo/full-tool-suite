@echo off
rem Sets up Last-Minute Tickets and adds its icon to the Desktop and Start Menu.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setting up Python packages - one time only...
  python -m venv .venv
)
.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check requests pillow
.venv\Scripts\python.exe make_icon.py
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "foreach ($f in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {" ^
  "  $s = $ws.CreateShortcut($f + '\Last-Minute Tickets.lnk');" ^
  "  $s.TargetPath = '%~dp0.venv\Scripts\pythonw.exe';" ^
  "  $s.Arguments = '\"%~dp0app.py\"';" ^
  "  $s.WorkingDirectory = '%~dp0';" ^
  "  $s.IconLocation = '%~dp0icon.ico,0';" ^
  "  $s.Description = 'Last-minute tickets near you: cheapest seats, price drops, Gametime / SeatGeek / Ticketmaster';" ^
  "  $s.Save(); Write-Output ('Created ' + $s.FullName) }"
echo.
echo Done. Double-click "Last-Minute Tickets" on the Desktop.
