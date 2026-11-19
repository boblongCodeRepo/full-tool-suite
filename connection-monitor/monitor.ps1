# Outgoing connection monitor.
# Polls the TCP connection table only; it never reads or walks the file system beyond its own log folder.
# Logs every new (program, remote address, port) once per day to logs\connections-<date>.csv
# and writes anything suspicious to logs\ALERTS.log.
#
#   -IntervalSeconds  how often to poll (default 3). Connections shorter than this can be missed.
#   -NoLookup         skip the country lookup (it sends each new remote IP to rdap.org, the public IP registry).

param(
    [int]$IntervalSeconds = 3,
    [switch]$NoLookup,
    [int]$RunSeconds = 0
)

$ErrorActionPreference = 'SilentlyContinue'
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir  = Join-Path $here 'logs'
$geoFile = Join-Path $here 'ip-cache.json'
New-Item -ItemType Directory -Force $logDir | Out-Null

$mutex = New-Object System.Threading.Mutex($false, 'Global\ConnectionMonitorTod')
if (-not $mutex.WaitOne(0)) { exit }

$alertCountries = @('CN', 'HK', 'MO')
$weirdCountries = @('RU', 'KP', 'IR', 'BY')
$normalPorts    = @(80, 443, 53, 853, 123, 993, 995, 465, 587, 22, 3478, 5228, 7680)
$lolbins        = @('powershell', 'pwsh', 'cmd', 'rundll32', 'regsvr32', 'mshta', 'wscript', 'cscript',
                    'certutil', 'bitsadmin', 'msbuild', 'installutil', 'regasm', 'regsvcs', 'msiexec', 'wmic', 'curl', 'ftp', 'nc', 'ncat')
$systemNames    = @('svchost', 'lsass', 'services', 'wininit', 'winlogon', 'csrss', 'smss', 'explorer', 'spoolsv', 'taskhostw', 'dllhost')
$browsers       = @('msedge', 'msedgewebview2', 'chrome', 'firefox', 'brave', 'opera')

$knownFile = Join-Path $here 'known-programs.txt'
if (-not (Test-Path $knownFile)) { "# learning started $(Get-Date -Format o)" | Set-Content $knownFile -Encoding utf8 }
$learnUntil = (Get-Item $knownFile).CreationTime.AddDays(1)
$known = @{}
Get-Content $knownFile | Where-Object { $_ -and $_ -notlike '#*' } | ForEach-Object { $known[$_] = $true }
$burst = @{}

$geo = @{}
if (Test-Path $geoFile) {
    (Get-Content $geoFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $geo[$_.Name] = $_.Value }
}
$procCache = @{}
$sigCache  = @{}
$seen      = @{}
$seenDay   = (Get-Date).Date
$alertLog  = Join-Path $logDir 'ALERTS.log'

function Test-PrivateIp([string]$ip) {
    $ip -match '^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|127\.|fe80:|fc|fd|::1$|0\.0\.0\.0|::$)'
}

function Get-IpInfo([string]$ip) {
    if (Test-PrivateIp $ip) { return [pscustomobject]@{ Country = 'LAN'; Org = 'local network' } }
    if ($geo.ContainsKey($ip)) { return $geo[$ip] }
    if ($NoLookup) { return [pscustomobject]@{ Country = '?'; Org = '' } }
    $info = [pscustomobject]@{ Country = '?'; Org = '' }
    try {
        $r = Invoke-RestMethod "https://rdap.org/ip/$ip" -TimeoutSec 6 -UseBasicParsing
        $org = $r.name
        $ent = $r.entities | Where-Object { $_.roles -contains 'registrant' } | Select-Object -First 1
        if ($ent -and $ent.vcardArray) {
            $fn = ($ent.vcardArray[1] | Where-Object { $_[0] -eq 'fn' } | Select-Object -First 1)
            if ($fn) { $org = $fn[3] }
        }
        $cc = $r.country -as [string]
        if (-not $cc) {
            foreach ($e in @($r.entities) + @($r.entities | ForEach-Object { $_.entities })) {
                $adr = $e.vcardArray[1] | Where-Object { $_[0] -eq 'adr' } | Select-Object -First 1
                if ($adr -and $adr[1].cc) { $cc = $adr[1].cc; break }
                if ($adr -and $adr[1].label) {
                    $last = (($adr[1].label -split "`n") | Select-Object -Last 1).Trim()
                    $map = @{ 'United States' = 'US'; 'Canada' = 'CA'; 'China' = 'CN'; 'Hong Kong' = 'HK' }
                    if ($map.ContainsKey($last)) { $cc = $map[$last]; break }
                }
            }
        }
        if (-not $cc -and $r.port43 -eq 'whois.arin.net') { $cc = 'US' }
        $info = [pscustomobject]@{ Country = if ($cc) { $cc.ToUpper() } else { '?' }; Org = $org }
    } catch { }
    $geo[$ip] = $info
    $geo | ConvertTo-Json -Depth 3 | Set-Content $geoFile -Encoding utf8
    return $info
}

function Get-ProcInfo([int]$procId) {
    if ($procCache.ContainsKey($procId)) { return $procCache[$procId] }
    $p = Get-Process -Id $procId
    $path = $p.Path
    if (-not $path) {
        $path = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId").ExecutablePath
    }
    $signer = 'unknown'
    if ($path) {
        if (-not $sigCache.ContainsKey($path)) {
            $s = Get-AuthenticodeSignature $path
            $name = ($s.SignerCertificate.Subject -replace '^CN=("?)([^,"]+).*', '$2')
            $sigCache[$path] = if ($s.Status -eq 'Valid') { "Valid: $name" }
                               elseif ($path -like 'C:\Program Files\WindowsApps\*') { 'Valid: Store app (package-signed)' }
                               else { "$($s.Status)" }
        }
        $signer = $sigCache[$path]
    } elseif ($procId -eq 4) { $signer = 'Valid: Windows kernel' }
    $info = [pscustomobject]@{ Name = $p.ProcessName; Path = $path; Signer = $signer }
    $procCache[$procId] = $info
    return $info
}

function Get-HostName([string]$ip, $dnsMap) {
    if ($dnsMap.ContainsKey($ip)) { return $dnsMap[$ip] }
    try {
        $task = [System.Net.Dns]::GetHostEntryAsync($ip)
        if ($task.Wait(1500)) { return $task.Result.HostName }
    } catch { }
    return ''
}

$stopAt = if ($RunSeconds -gt 0) { (Get-Date).AddSeconds($RunSeconds) } else { [datetime]::MaxValue }
while ((Get-Date) -lt $stopAt) {
    $now = Get-Date
    if ($now.Date -ne $seenDay) { $seen = @{}; $procCache = @{}; $seenDay = $now.Date }
    $csv = Join-Path $logDir ("connections-{0:yyyy-MM-dd}.csv" -f $now)

    $conns = Get-NetTCPConnection -State Established, SynSent |
        Where-Object { $_.RemoteAddress -notmatch '^(127\.|::1$|0\.0\.0\.0$|::$)' -and $_.OwningProcess -ne $PID }

    $new = foreach ($c in $conns) {
        $info = Get-ProcInfo $c.OwningProcess
        $key = "$($info.Path)|$($c.RemoteAddress)|$($c.RemotePort)"
        if (-not $seen.ContainsKey($key)) { $seen[$key] = $true; [pscustomobject]@{ C = $c; P = $info } }
    }

    if ($new) {
        $dnsMap = @{}
        Get-DnsClientCache | Where-Object { $_.Type -in 1, 28 -and $_.Data } | ForEach-Object { $dnsMap[$_.Data] = $_.Entry }

        foreach ($n in $new) {
            $ip   = $n.C.RemoteAddress
            $ipi  = Get-IpInfo $ip
            $row  = [pscustomobject]@{
                Time       = $now.ToString('yyyy-MM-dd HH:mm:ss')
                Program    = $n.P.Name
                PID        = $n.C.OwningProcess
                RemoteIP   = $ip
                Port       = $n.C.RemotePort
                Host       = Get-HostName $ip $dnsMap
                Country    = $ipi.Country
                Owner      = $ipi.Org
                State      = $n.C.State
                Signature  = $n.P.Signer
                Path       = $n.P.Path
            }
            $row | Export-Csv $csv -Append -NoTypeInformation -Encoding utf8

            $alert = @(); $weird = @()
            $isLan = $row.Country -eq 'LAN'
            $prog  = $row.Program.ToLower()

            if ($alertCountries -contains $row.Country) { $alert += "destination is in $($row.Country)" }
            if ($weirdCountries -contains $row.Country) { $weird += "destination is in $($row.Country)" }
            if ($row.Signature -notlike 'Valid*' -and $row.Path) { $weird += "program is not validly signed ($($row.Signature))" }
            if ($row.Path -match '\\(Temp|Downloads|AppData\\Roaming|ProgramData|Users\\Public)\\' -and $row.Signature -notlike 'Valid*') {
                $alert += 'unsigned program running from a Temp/Downloads/Roaming/Public folder'
            }
            if ($systemNames -contains $prog -and $row.Path -and $row.Path -notmatch '^C:\\Windows\\(System32|SysWOW64|explorer\.exe)') {
                $alert += "fake system process: $($row.Program) running from the wrong folder"
            }
            if ($lolbins -contains $prog -and -not $isLan) { $weird += "Windows tool often abused by malware ($($row.Program)) is connecting to the internet" }
            if (-not $isLan -and $normalPorts -notcontains [int]$row.Port) { $weird += "unusual port $($row.Port)" }
            if (-not $isLan -and -not $row.Host -and $row.Path -and $row.Signature -notlike 'Valid*') { $weird += 'unsigned program connecting to a bare IP address with no hostname' }
            if ($row.Path -and -not $known.ContainsKey($row.Path)) {
                $known[$row.Path] = $true
                $row.Path | Add-Content $knownFile -Encoding utf8
                if ((Get-Date) -gt $learnUntil) { $weird += 'first time this program has ever gone online' }
            }
            if (-not $isLan) {
                $k = "$($n.C.OwningProcess)"
                $burst[$k] = @(@($burst[$k]) + $now | Where-Object { $_ -and ($now - $_).TotalSeconds -lt 60 })
                if ($burst[$k].Count -eq 40 -and $browsers -notcontains $prog) { $alert += 'contacted 40+ new addresses within a minute (possible scanning or data spraying)' }
            }

            $dest = "$($row.Program) -> $ip`:$($row.Port) [$($row.Host)] $($row.Country) $($row.Owner)"
            if ($alert) { "$($row.Time)  ALERT  $dest  |  $(($alert + $weird) -join '; ')  |  $($row.Path)" | Out-File $alertLog -Append -Encoding utf8 }
            elseif ($weird) { "$($row.Time)  WEIRD  $dest  |  $($weird -join '; ')  |  $($row.Path)" | Out-File $alertLog -Append -Encoding utf8 }
        }
    }
    Start-Sleep $IntervalSeconds
}
