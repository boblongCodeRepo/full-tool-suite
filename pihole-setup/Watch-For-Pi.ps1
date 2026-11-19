# Watches for the Raspberry Pi to appear on the network and offers to set it up.
# Starts at logon (Startup folder shortcut) and stops for good once setup has finished.
#   -Once       do a single check, print the result, no pop-ups (for testing)
#   -TestPopup  show the "Pi found" pop-up with sample values (doesn't start setup)
param([switch]$Once, [switch]$TestPopup)

$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$done  = Join-Path $here '.setup-done'
$log   = Join-Path $here 'watcher.log'
$names = @('pihole.local', 'raspberrypi.local')
# MAC prefixes used by Raspberry Pi boards
$piOui = @('b8-27-eb', 'dc-a6-32', 'e4-5f-01', 'd8-3a-dd', '2c-cf-67', '28-cd-c1', '88-a2-9e')

function Log($m) { try { "$(Get-Date -Format s)  $m" | Add-Content -Path $log -Encoding utf8 } catch {} }

function Ask($text, $title, $buttons = 'YesNo', $icon = 'Question') {
    Add-Type -AssemblyName System.Windows.Forms
    $owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true }
    try { return [System.Windows.Forms.MessageBox]::Show($owner, $text, $title, $buttons, $icon).ToString() }
    finally { $owner.Dispose() }
}

function Resolve-Name($name) {
    try {
        $t = [Net.Dns]::GetHostAddressesAsync($name)
        if ($t.Wait(3000)) { return $t.Result | Where-Object AddressFamily -eq 'InterNetwork' | Select-Object -First 1 }
    } catch {}
    return $null
}

function Test-Ssh($ip) {
    $c = New-Object Net.Sockets.TcpClient
    try { return ($c.ConnectAsync($ip, 22).Wait(1500) -and $c.Connected) } catch { return $false } finally { $c.Dispose() }
}

function Find-Pi {
    foreach ($n in $names) {
        $a = Resolve-Name $n
        if ($a) { return [pscustomobject]@{ Name = $n; IP = $a.IPAddressToString } }
    }
    $nb = Get-NetNeighbor -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
        $mac = "$($_.LinkLayerAddress)".ToLower(); ($piOui | Where-Object { $mac.StartsWith($_) }).Count -gt 0
    } | Select-Object -First 1
    if ($nb) { return [pscustomobject]@{ Name = $nb.IPAddress; IP = $nb.IPAddress } }
    return $null
}

# A Proxmox NUC has no network name, so look for its web page (port 8006) on this PC's subnet.
$nucDir  = Join-Path (Split-Path $here) 'nuc-setup'
$nucDone = Join-Path $nucDir '.setup-done'
function Find-Nuc {
    $cfg = Get-NetIPConfiguration -ErrorAction SilentlyContinue |
           Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } | Select-Object -First 1
    if (-not $cfg) { return $null }
    $me = ($cfg.IPv4Address | Select-Object -First 1).IPAddress
    $base = ($me -split '\.')[0..2] -join '.'
    $probes = foreach ($i in 1..254) {
        $ip = "$base.$i"
        if ($ip -eq $me) { continue }
        $c = New-Object Net.Sockets.TcpClient
        [pscustomobject]@{ IP = $ip; Client = $c; Task = $c.ConnectAsync($ip, 8006) }
    }
    Start-Sleep -Milliseconds 1500
    $hit = $null
    foreach ($p in $probes) {
        if (-not $hit -and $p.Task.IsCompleted -and -not $p.Task.IsFaulted -and $p.Client.Connected) { $hit = $p.IP }
        $p.Client.Dispose()
    }
    if ($hit -and (Test-Ssh $hit)) { return $hit }
    return $null
}

function Prompt-Found($pi) {
    Ask ("Your Raspberry Pi is connected!`n`n    $($pi.Name)   ($($pi.IP))`n`n" +
         "Set it up now? It takes about 15-20 minutes and you'll mostly just press Enter.`n`n" +
         "(Choose No to be reminded again in 30 minutes.)") 'Raspberry Pi found'
}

if ($TestPopup) { "Answer: " + (Prompt-Found ([pscustomobject]@{ Name = 'pihole.local'; IP = '192.168.8.50' })); exit }

if ($Once) {
    $pi = Find-Pi
    if ($pi) { "Found $($pi.Name) at $($pi.IP); SSH open: $(Test-Ssh $pi.IP)" } else { 'No Raspberry Pi on the network yet.' }
    $nuc = Find-Nuc
    if ($nuc) { "Found Proxmox NUC at $nuc" } else { 'No Proxmox NUC on the network yet.' }
    if (Test-Path $done) { 'Setup already marked done - the watcher would exit.' }
    exit
}

# Only one watcher at a time
$mutex = New-Object System.Threading.Mutex($false, 'Local\PiSetupWatcher')
if (-not $mutex.WaitOne(0)) { exit }

Log 'watcher started'
$snoozeUntil = [datetime]::MinValue
$nucSnooze = [datetime]::MinValue
$noSshSince = $null
$hinted = $false

while (-not (Test-Path $done)) {
    if ((Get-Date) -ge $snoozeUntil) {
        $pi = Find-Pi
        if ($pi -and (Test-Ssh $pi.IP)) {
            Log "found $($pi.Name) $($pi.IP)"
            if ((Prompt-Found $pi) -eq 'Yes') {
                Start-Process powershell.exe -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', "`"$here\Send-To-Pi.ps1`"", '-PiHost', $pi.IP)
                Log 'setup launched'
                $snoozeUntil = (Get-Date).AddMinutes(45)   # ask again later only if setup didn't finish
            } else {
                $snoozeUntil = (Get-Date).AddMinutes(30)
            }
        } elseif ($pi) {
            # A Pi is there but won't accept a connection: still booting, or SSH wasn't turned on.
            if (-not $noSshSince) { $noSshSince = Get-Date }
            elseif (-not $hinted -and ((Get-Date) - $noSshSince).TotalMinutes -ge 10) {
                Log "pi at $($pi.IP) but ssh closed for 10 min"
                [void](Ask ("A Raspberry Pi is on your network ($($pi.IP)) but isn't accepting connections.`n`n" +
                    "This usually means SSH wasn't turned on when the SD card was made.`n" +
                    "Redo STEP 1 and make sure SSH is ON, then plug the Pi back in.") 'Raspberry Pi needs attention' 'OK' 'Warning')
                $hinted = $true
            }
        }
    }
    if ((Test-Path $nucDir) -and -not (Test-Path $nucDone) -and (Get-Date) -ge $nucSnooze) {
        $nuc = Find-Nuc
        if ($nuc) {
            Log "found Proxmox NUC $nuc"
            $r = Ask ("Your NUC is connected!`n`n    Proxmox at $nuc`n`n" +
                      "Set it up now? It takes 20-40 minutes and you'll mostly just press Enter.`n`n" +
                      "(Choose No to be reminded again in 30 minutes.)") 'NUC found'
            if ($r -eq 'Yes') {
                Start-Process powershell.exe -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', "`"$nucDir\Setup-NUC.ps1`"", '-NucHost', $nuc)
                Log 'NUC setup launched'
                $nucSnooze = (Get-Date).AddMinutes(60)
            } else {
                $nucSnooze = (Get-Date).AddMinutes(30)
            }
        }
    }
    Start-Sleep -Seconds 60
}
Log 'setup done - watcher exiting'
