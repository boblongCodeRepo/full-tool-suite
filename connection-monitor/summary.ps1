# Summarise a day's connection log.  Usage:  powershell -File summary.ps1 [yyyy-MM-dd]
param([string]$Date = (Get-Date -Format 'yyyy-MM-dd'))
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$csv  = Join-Path $here "logs\connections-$Date.csv"
if (-not (Test-Path $csv)) { "No log for $Date"; exit }
$rows = Import-Csv $csv

"`n=== Connections on $Date by destination country ==="
$rows | Group-Object Country | Sort-Object Count -Descending | Format-Table Count, Name -AutoSize

"=== By program ==="
$rows | Group-Object Program | Sort-Object Count -Descending | Format-Table Count, Name -AutoSize

"=== Destinations in China / Hong Kong / Macau ==="
$cn = $rows | Where-Object { $_.Country -in 'CN', 'HK', 'MO' }
if ($cn) { $cn | Format-Table Time, Program, RemoteIP, Port, Host, Owner -AutoSize -Wrap } else { "None.`n" }

"=== Programs without a valid signature ==="
$us = $rows | Where-Object { $_.Signature -notlike 'Valid*' } | Sort-Object Path -Unique
if ($us) { $us | Format-Table Program, Signature, Path -AutoSize -Wrap } else { "None.`n" }

$alerts = Join-Path $here 'logs\ALERTS.log'
"=== Alerts ==="
if (Test-Path $alerts) { Get-Content $alerts -Tail 30 } else { "No alerts ever logged." }
