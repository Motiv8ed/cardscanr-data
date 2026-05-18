param(
    [string]$RepoRoot = "",
    [int]$IntervalMinutes = 75,
    [switch]$Foreground,
    [switch]$Background
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$loopScript = Join-Path $RepoRoot 'scripts\run_pokewallet_catalog_worker_loop.ps1'
$logPath = Join-Path $RepoRoot 'logs\pokewallet_catalog_worker.log'
$statusPath = Join-Path $RepoRoot 'data\pokewallet_catalog_worker_status.json'

if (-not (Test-Path $loopScript)) {
    throw "Worker loop script not found: $loopScript"
}

if ($Background -and $Foreground) {
    throw 'Choose either -Foreground or -Background, not both.'
}

if ($IntervalMinutes -le 0) {
    $IntervalMinutes = 75
}

$args = @(
    '-NoProfile',
    '-ExecutionPolicy',
    'Bypass',
    '-File',
    $loopScript,
    '-RepoRoot',
    $RepoRoot,
    '-IntervalMinutes',
    [string]$IntervalMinutes
)

if ($Background) {
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $args -WindowStyle Normal -PassThru
    Write-Host "Started manual PokéWallet catalogue worker loop in a new window. PID: $($process.Id)"
}
else {
    Write-Host 'Starting manual PokéWallet catalogue worker loop in this window.'
    Write-Host 'Press Ctrl+C in this window, or run the stop script from another terminal.'
}

Write-Host ("Interval: {0} minutes" -f $IntervalMinutes)
Write-Host ("Log path: {0}" -f $logPath)
Write-Host ("Status file: {0}" -f $statusPath)
Write-Host 'Status command: .\scripts\status_pokewallet_catalog_worker.ps1'
Write-Host 'Stop command: .\scripts\stop_pokewallet_catalog_worker.ps1'

if (-not $Background) {
    Write-Host ''
    & powershell.exe @args
    exit $LASTEXITCODE
}
