param(
    [int]$RefreshSeconds = 30,
    [switch]$Once,
    [switch]$ShowLogs,
    [string]$RepoRoot = ""
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
$RefreshSeconds = [Math]::Max(1, $RefreshSeconds)
$statusScript = Join-Path $RepoRoot 'scripts\status_local_price_updater.ps1'

function Invoke-StatusSnapshot {
    if ($ShowLogs) {
        & $statusScript -RepoRoot $RepoRoot -ShowLogs
    }
    else {
        & $statusScript -RepoRoot $RepoRoot
    }
}

if ($Once) {
    Invoke-StatusSnapshot
    exit 0
}

try {
    while ($true) {
        Clear-Host
        Invoke-StatusSnapshot
        Write-Host ''
        Write-Host "Auto-refreshing every $RefreshSeconds seconds. Press Ctrl+C to exit."
        Start-Sleep -Seconds $RefreshSeconds
    }
}
catch [System.Management.Automation.PipelineStoppedException] {
}
catch {
    if ($_.Exception.Message -notmatch 'Pipeline has been stopped') {
        throw
    }
}
