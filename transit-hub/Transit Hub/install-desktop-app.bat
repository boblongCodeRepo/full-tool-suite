@echo off
rem Adds a "DC Transit Hub" app icon to the Desktop and Start Menu.
rem Clicking it opens the board in its own window (starting the app in the background first if needed).
rem It does not change start-up behaviour; install-kiosk.bat handles that.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" make_icon.py
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "foreach ($f in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {" ^
  "  $s = $ws.CreateShortcut($f + '\DC Transit Hub.lnk');" ^
  "  $s.TargetPath = 'powershell.exe';" ^
  "  $s.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%~dp0set-kiosk-mode.ps1\" window';" ^
  "  $s.WorkingDirectory = '%~dp0';" ^
  "  $s.WindowStyle = 7;" ^
  "  $s.IconLocation = '%~dp0transit.ico,0';" ^
  "  $s.Description = 'DC Transit Hub - trains, buses, weather, DC events and more';" ^
  "  $s.Save(); Write-Output ('Created ' + $s.FullName) }"
echo.
echo Done. Double-click "DC Transit Hub" on the Desktop to open the board.
