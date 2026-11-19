@echo off
rem Sets up Business Class Finder and adds its icon to the Desktop and Start Menu.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setting up Python packages - one time only...
  python -m venv .venv
)
.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check requests feedparser pillow
.venv\Scripts\python.exe make_icon.py
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "foreach ($f in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {" ^
  "  $s = $ws.CreateShortcut($f + '\Business Class Finder.lnk');" ^
  "  $s.TargetPath = '%~dp0.venv\Scripts\pythonw.exe';" ^
  "  $s.Arguments = '\"%~dp0app.py\"';" ^
  "  $s.WorkingDirectory = '%~dp0';" ^
  "  $s.IconLocation = '%~dp0icon.ico,0';" ^
  "  $s.Description = 'Business-class prices from Washington DC, New York and Chicago to Europe and Asia';" ^
  "  $s.Save(); Write-Output ('Created ' + $s.FullName) }"
echo.
echo Done. Double-click "Business Class Finder" on the Desktop.
