# Packs the Market Terminal (portable build) + your data into downloads\market-apps.iso,
# a disc image the Windows VM sees as a CD drive. Setup-NUC.ps1 runs this for you.
param([string]$Out)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Out) { $Out = Join-Path $here 'downloads\market-apps.iso' }
$project = Join-Path (Split-Path $here) 'bloomberg app'
$exe  = Join-Path $project 'dist\portable\MarketTerminal.exe'
$data = Join-Path $env:LOCALAPPDATA 'MarketTerminal'

if (-not (Test-Path $exe)) { throw "Portable build not found: $exe  (run build_portable.bat in the project first)" }
$installed = Join-Path $project 'dist\MarketTerminal\MarketTerminal.exe'
if ((Test-Path $installed) -and (Get-Item $installed).LastWriteTime -gt (Get-Item $exe).LastWriteTime.AddHours(1)) {
    Write-Host "Note: the portable build is older than your installed build. Run build_portable.bat for the newest version." -ForegroundColor Yellow
}

$stage = Join-Path $env:TEMP 'market-apps-stage'
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item $exe $stage
Copy-Item (Join-Path $here 'windows\Install-Apps.cmd'), (Join-Path $here 'windows\Install-Apps.ps1') $stage
$icons = Join-Path $project 'app\icons'
if (Test-Path $icons) { New-Item -ItemType Directory -Force "$stage\icons" | Out-Null; Copy-Item "$icons\*.ico" "$stage\icons" }
if (Test-Path $data) {
    # Skip this PC's Device Watch history (it's about this PC, not the VM) and re-downloadable caches.
    # /R:0 /W:0: files locked by a running app are skipped instead of hanging.
    robocopy $data (Join-Path $stage 'MarketTerminal-data') /E /R:0 /W:0 /NFL /NDL /NJH /NJS /NP `
        /XD (Join-Path $data 'device') (Join-Path $data 'yf-cache') 'Cache' 'Code Cache' 'GPUCache' 'CacheStorage' 'ScriptCache' 'Crashpad' `
        /XF '*.broken-*' 'BrowserMetrics*' | Out-Null
}
@"
MARKET TERMINAL APPS DISC
Inside the Windows VM, double-click Install-Apps.cmd and click Yes.
Built $(Get-Date -Format 'yyyy-MM-dd HH:mm') from $env:COMPUTERNAME.
"@ | Set-Content (Join-Path $stage 'README.txt') -Encoding utf8

function New-IsoFile([string]$Source, [string]$Path, [string]$Label) {
    if (-not ('IsoWriter' -as [type])) {
        $cp = New-Object CodeDom.Compiler.CompilerParameters
        $cp.CompilerOptions = '/unsafe'
        Add-Type -CompilerParameters $cp -TypeDefinition @'
public class IsoWriter {
  public unsafe static void Write(string path, object stream, int blockSize, int totalBlocks) {
    int read = 0; byte[] buf = new byte[blockSize]; var p = (System.IntPtr)(&read);
    var input = (System.Runtime.InteropServices.ComTypes.IStream)stream;
    using (var output = System.IO.File.Create(path)) {
      while (totalBlocks-- > 0) { input.Read(buf, blockSize, p); output.Write(buf, 0, read); }
    }
  }
}
'@
    }
    $fs = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
    $fs.ChooseImageDefaultsForMediaType(13)   # 13 = generic disk: no CD/DVD size limit
    $fs.FileSystemsToCreate = 4               # UDF: long names, big files
    $fs.VolumeName = $Label
    $fs.Root.AddTree($Source, $false)
    $img = $fs.CreateResultImage()
    $stream = $img.ImageStream
    try { [IsoWriter]::Write($Path, $stream, $img.BlockSize, $img.TotalBlocks) }
    finally {
        # IMAPI keeps every source file open until its COM objects are released
        foreach ($o in $stream, $img, $fs) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($o) }
        [GC]::Collect(); [GC]::WaitForPendingFinalizers()
    }
}

New-Item -ItemType Directory -Force (Split-Path $Out) | Out-Null
Write-Host "Building apps disc..." -ForegroundColor Cyan
New-IsoFile -Source $stage -Path $Out -Label 'MARKETAPPS'
Remove-Item $stage -Recurse -Force
Write-Host ("Apps disc ready: {0}  ({1:N0} MB)" -f $Out, ((Get-Item $Out).Length / 1MB)) -ForegroundColor Green
