param(
    [int]$RefreshSeconds = 10,
    [string]$RepoRoot = ""
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
$RefreshSeconds = [Math]::Max(1, $RefreshSeconds)
$statusScript = Join-Path $RepoRoot 'scripts\status_local_price_updater.ps1'

try {
    while ($true) {
        Clear-Host
        & $statusScript -RepoRoot $RepoRoot
        Write-Host ''
        Write-Host "Refreshing every $RefreshSeconds second(s). Press Ctrl+C to exit."
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
