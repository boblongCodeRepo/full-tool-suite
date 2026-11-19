@echo off
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Connection Monitor.lnk" 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.CommandLine -like '*ConnectionMonitor\monitor.ps1*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Connection Monitor removed and stopped.
pause
