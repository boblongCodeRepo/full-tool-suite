# Sets up the NUC after Proxmox is installed on it. Run on this PC.
#   Double-click "STEP 3 - Set up the NUC", or it opens from the "NUC found" pop-up.
param([string]$NucHost)

$ErrorActionPreference = 'Continue'
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = Split-Path $here
$pihole  = Join-Path $desktop 'pihole-setup'
$ledger  = Join-Path $desktop 'bloomberg app\homefinance'
$isoDir  = '/var/lib/vz/template/iso'

function Fail($m) { Write-Host "`n$m" -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }

foreach ($p in "$pihole\setup-pihole.sh", "$pihole\netmap\netmap.py", "$ledger\server.py", "$here\proxmox\nuc-setup.sh") {
    if (-not (Test-Path $p)) { Fail "Missing file: $p" }
}

# ---------------------------------------------------------------- find the NUC
Write-Host "NUC SETUP" -ForegroundColor Green
Write-Host "After Proxmox installs, the NUC's screen shows an address like https://192.168.8.10:8006"
$h = Read-Host "NUC address (just the numbers, e.g. 192.168.8.10)$(if ($NucHost) { " [$NucHost]" })"
if ($h) { $NucHost = $h.Trim() -replace '^https?://', '' -replace ':\d+.*$', '' }
if (-not $NucHost) { Fail "No address entered." }

$c = New-Object Net.Sockets.TcpClient
$open = try { $c.ConnectAsync($NucHost, 22).Wait(3000) -and $c.Connected } catch { $false } finally { $c.Dispose() }
if (-not $open) { Fail "Can't reach $NucHost. Check the NUC is on, plugged into the router, and the address is right." }

# ---------------------------------------------------------------- SSH key (so you type the password once)
Step "Connecting (you'll type the Proxmox root password once)"
$key = Join-Path $env:USERPROFILE '.ssh\nuc_ed25519'
if (-not (Test-Path $key)) {
    New-Item -ItemType Directory -Force (Split-Path $key) | Out-Null
    ssh-keygen -q -t ed25519 -N '""' -C 'nuc-setup' -f $key
}
$ssh = @('-i', $key, '-o', 'StrictHostKeyChecking=accept-new')
ssh @ssh -o BatchMode=yes -o ConnectTimeout=5 "root@$NucHost" true 2>$null
if ($LASTEXITCODE -ne 0) {
    Get-Content "$key.pub" | ssh -o StrictHostKeyChecking=accept-new "root@$NucHost" `
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys"
    ssh @ssh -o BatchMode=yes "root@$NucHost" true 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "Couldn't log in to the NUC. Check the root password you chose when installing Proxmox." }
}
Write-Host "Connected." -ForegroundColor Green

# ---------------------------------------------------------------- copy the setup files
Step "Copying setup files"
$stage = Join-Path $env:TEMP 'nuc-stage\nuc-setup'
Remove-Item (Split-Path $stage) -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$stage\pihole\netmap", "$stage\ledger\static" | Out-Null
Copy-Item "$here\proxmox\nuc-setup.sh" $stage
Copy-Item "$pihole\setup-pihole.sh" "$stage\pihole"
Copy-Item "$pihole\netmap\netmap.py", "$pihole\netmap\netmap.html", "$pihole\netmap\install-netmap.sh" "$stage\pihole\netmap"
Copy-Item "$ledger\server.py" "$stage\ledger"
Copy-Item "$ledger\static\index.html", "$ledger\static\app.js" "$stage\ledger\static"
# Linux needs LF line endings
Get-ChildItem $stage -Recurse -Include *.sh, *.py, *.html, *.js | ForEach-Object {
    $t = [IO.File]::ReadAllText($_.FullName); [IO.File]::WriteAllText($_.FullName, $t.Replace("`r`n", "`n"))
}
scp @ssh -r -q $stage "root@${NucHost}:/root/"
if ($LASTEXITCODE -ne 0) { Fail "Copying files failed." }

# ---------------------------------------------------------------- apps disc
$a = Read-Host "`nInclude the Market Terminal + your data for the Windows VM? [Y/n]"
if ($a -notmatch '^[Nn]') {
    Step "Building the apps disc (about a minute)"
    $appsIso = Join-Path $here 'downloads\market-apps.iso'
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$here\Make-AppsDisc.ps1" -Out $appsIso
    if (Test-Path $appsIso) {
        Write-Host "Uploading apps disc..."
        scp @ssh $appsIso "root@${NucHost}:$isoDir/market-apps.iso"
    }
}

# ---------------------------------------------------------------- Windows installer
$remoteWin = ssh @ssh "root@$NucHost" "ls $isoDir 2>/dev/null | grep -iE '^win.*\.iso$' | head -1"
if ($remoteWin) {
    Write-Host "`nWindows installer already on the NUC: $remoteWin"
} else {
    $win = Get-ChildItem (Join-Path $env:USERPROFILE 'Downloads'), (Join-Path $here 'downloads') -Filter 'Win*.iso' -ErrorAction SilentlyContinue |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($win) {
        Step "Uploading the Windows installer ($([math]::Round($win.Length/1GB,1)) GB - several minutes)"
        $name = ($win.Name -replace '[^A-Za-z0-9._-]', '_')
        scp @ssh $win.FullName "root@${NucHost}:$isoDir/$name"
    } else {
        Write-Host "`nNo Windows 11 installer (Win11_*.iso) in Downloads - the Windows VM will be skipped for now." -ForegroundColor Yellow
        Write-Host "Get it from microsoft.com/software-download/windows11 and run this again later."
    }
}

# ---------------------------------------------------------------- run it on the NUC
Step "Running setup on the NUC (20-40 minutes; mostly press Enter)"
ssh @ssh -t "root@$NucHost" "bash /root/nuc-setup/nuc-setup.sh"
$ok = ($LASTEXITCODE -eq 0)
Remove-Item (Split-Path $stage) -Recurse -Force -ErrorAction SilentlyContinue

if ($ok) {
    Set-Content (Join-Path $here '.setup-done') (Get-Date -Format s)
    # Pi-hole now lives on the NUC, so the "Raspberry Pi found" pop-up isn't needed either
    if (Test-Path $pihole) { Set-Content (Join-Path $pihole '.setup-done') "NUC $(Get-Date -Format s)" }
    Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'Raspberry Pi setup watcher.lnk') -ErrorAction SilentlyContinue
    Write-Host "`nAll done! Open https://${NucHost}:8006 to see everything." -ForegroundColor Green
    Write-Host "(Your browser will warn about the certificate the first time - that's normal for Proxmox. Click Advanced > Continue.)"
} else {
    Write-Host "`nSetup didn't finish. That's OK - run 'STEP 3 - Set up the NUC' again; it skips what's already done." -ForegroundColor Yellow
}
Read-Host "Press Enter to close"
