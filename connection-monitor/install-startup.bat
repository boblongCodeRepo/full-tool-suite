@echo off
set "DIR=%~dp0"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Connection Monitor.lnk'); $s.TargetPath='powershell.exe'; $s.Arguments='-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%DIR%monitor.ps1\"'; $s.WorkingDirectory='%DIR%'; $s.WindowStyle=7; $s.Save()"
start "" powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%DIR%monitor.ps1"
echo Connection Monitor installed and started. Logs: %DIR%logs
pause
