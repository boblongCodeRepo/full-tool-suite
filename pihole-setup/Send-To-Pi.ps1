# Run on your PC after the Pi is plugged in and booted (~2 min after power-on).
# Copies setup-pihole.sh to the Pi and runs it.
#   Right-click > Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File .\Send-To-Pi.ps1
param(
    [string]$PiHost = "pihole.local",
    [string]$User   = "pi"
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "setup-pihole.sh"

$u = Read-Host "Pi username you set in Imager [$User]"
if ($u) { $User = $u }
$h = Read-Host "Pi hostname or IP [$PiHost]"
if ($h) { $PiHost = $h }

Write-Host "`nLooking for $PiHost ..." -ForegroundColor Cyan
if (-not (Test-Connection -ComputerName $PiHost -Count 2 -Quiet)) {
    Write-Host "Can't reach $PiHost." -ForegroundColor Yellow
    Write-Host " - Wait another minute; first boot is slow."
    Write-Host " - Check the Ethernet cable and that the Pi's green light flickers."
    Write-Host " - Or find the Pi's IP in your router's device list and re-run with that IP."
    Read-Host "Press Enter to exit"; exit 1
}

Write-Host "Found it. Copying setup files (enter the Pi password when asked)..." -ForegroundColor Cyan
scp -r $script (Join-Path $here "netmap") "${User}@${PiHost}:~/"
if ($LASTEXITCODE -ne 0) { Write-Host "Copy failed." -ForegroundColor Red; Read-Host "Press Enter to exit"; exit 1 }

Write-Host "Running setup on the Pi (enter the password again)..." -ForegroundColor Cyan
ssh -t "${User}@${PiHost}" "bash ~/setup-pihole.sh"
$setupOk = ($LASTEXITCODE -eq 0)

Write-Host "`nCopying the backup back to this PC..." -ForegroundColor Cyan
$bk = Join-Path $here "backups"
New-Item -ItemType Directory -Force $bk | Out-Null
scp "${User}@${PiHost}:~/pihole-backups/*.zip" $bk
Write-Host "Backups folder: $bk"

if ($setupOk) {
    # Turn off the "Raspberry Pi found" pop-up for good
    Set-Content -Path (Join-Path $here ".setup-done") -Value (Get-Date -Format s)
    Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) "Raspberry Pi setup watcher.lnk") -ErrorAction SilentlyContinue
    Read-Host "`nAll done! Press Enter to close"
} else {
    Write-Host "`nSetup didn't finish. That's OK - run 'STEP 3 - Set up the Pi' again and it picks up where it left off." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
}
