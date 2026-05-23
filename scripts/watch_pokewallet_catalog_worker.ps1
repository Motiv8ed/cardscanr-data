param(
    [string]$RepoRoot = "",
    [int]$IntervalSeconds = 30
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

if ($IntervalSeconds -lt 5) {
    $IntervalSeconds = 5
}

$statusScript = Join-Path $RepoRoot 'scripts\status_pokewallet_catalog_worker.ps1'
if (-not (Test-Path $statusScript)) {
    throw "Status script not found: $statusScript"
}

Write-Host "Watching CardScanR Pokewallet catalogue worker every $IntervalSeconds seconds. Press Ctrl+C to stop."

while ($true) {
    Write-Host ''
    Write-Host ("[{0}] worker status" -f ([datetime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')))
    & powershell -NoProfile -ExecutionPolicy Bypass -File $statusScript -RepoRoot $RepoRoot
    Start-Sleep -Seconds $IntervalSeconds
}
