# Runs INSIDE the Windows VM from the "MARKETAPPS" CD drive (via Install-Apps.cmd).
# Copies the Market Terminal + your data to C:\MarketTerminal, makes shortcuts,
# and installs the Proxmox guest tools (better mouse/screen, clean shutdowns).
$ErrorActionPreference = 'Stop'
$src  = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = 'C:\MarketTerminal'

Write-Host "Installing Market Terminal to $dest ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item "$src\MarketTerminal.exe" $dest -Force

$data = Join-Path $dest 'MarketTerminal-data'
if (Test-Path "$src\MarketTerminal-data") {
    if (Test-Path $data) {
        $a = Read-Host "Your data is already on this PC. Replace it with the copy on the disc? [y/N]"
        if ($a -match '^[Yy]') { Remove-Item $data -Recurse -Force; Copy-Item "$src\MarketTerminal-data" $data -Recurse }
    } else {
        Copy-Item "$src\MarketTerminal-data" $data -Recurse
    }
}
# Files copied from a CD are read-only; the apps need to write their data.
Get-ChildItem $dest -Recurse -File | ForEach-Object { $_.IsReadOnly = $false }

if (Test-Path "$src\icons") { Copy-Item "$src\icons" $dest -Recurse -Force }
# Same list as tools\make_shortcut.ps1 in the project: name = (arguments, icon)
$apps = [ordered]@{
    'Launchpad'        = @('--app launch',   'launch.ico')
    'Market Terminal'  = @('',               '')
    'Portfolio Lab'    = @('--app lab',      'lab.ico')
    'Signal Monitor'   = @('--app signal',   'signal.ico')
    'Influence Watch'  = @('--app watch',    'watch.ico')
    'Follow the Money' = @('--app money',    'money.ico')
    'Device Watch'     = @('--app device',   'device.ico')
    'Auction Finder'   = @('--app auctions', 'auctions.ico')
    'DC Events'        = @('--app events',   'events.ico')
    'Morning Brief'    = @('--app brief',    'brief.ico')
}
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Market Terminal'
New-Item -ItemType Directory -Force $menu | Out-Null
foreach ($name in $apps.Keys) {
    foreach ($dir in $desktop, $menu) {
        $s = $shell.CreateShortcut((Join-Path $dir "$name.lnk"))
        $s.TargetPath = "$dest\MarketTerminal.exe"
        $s.Arguments = $apps[$name][0]
        $s.WorkingDirectory = $dest
        $ico = Join-Path $dest "icons\$($apps[$name][1])"
        $s.IconLocation = if ($apps[$name][1] -and (Test-Path $ico)) { $ico } else { "$dest\MarketTerminal.exe,0" }
        $s.Save()
    }
}
Write-Host "Shortcuts added to the desktop and Start menu." -ForegroundColor Green

# Proxmox/VirtIO guest tools from the virtio-win CD, if it's attached
$tools = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root 'virtio-win-guest-tools.exe' } |
         Where-Object { Test-Path $_ } | Select-Object -First 1
if ($tools -and -not (Get-Service QEMU-GA -ErrorAction SilentlyContinue)) {
    Write-Host "Installing guest tools (a window may flash)..." -ForegroundColor Cyan
    try { Start-Process $tools -ArgumentList '/install', '/passive', '/norestart' -Wait; Write-Host "Guest tools installed." -ForegroundColor Green }
    catch { Write-Host "Guest tools didn't install - you can run $tools by hand later." -ForegroundColor Yellow }
}

Write-Host "`nDone! Open 'Market Terminal' from the desktop." -ForegroundColor Green
Write-Host "Note: Device Watch here watches this Windows VM. For the whole house, use the Network Map."
Read-Host "Press Enter to close"
