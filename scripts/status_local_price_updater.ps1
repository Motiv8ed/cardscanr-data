param(
    [string]$RepoRoot = ""
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$lockPath = Join-Path $RepoRoot '.local_updater.lock'
$logPath = Join-Path $RepoRoot 'logs\local_price_updater.log'

function Test-ProcessAlive {
    param([int]$ProcessId)

    try {
        $null = Get-Process -Id $ProcessId -ErrorAction Stop
        return $true
    }
    catch {
        return $false
    }
}

Write-Host "Repo: $RepoRoot"

if (Test-Path $lockPath) {
    try {
        $lockData = (Get-Content -Path $lockPath -Raw) | ConvertFrom-Json
        $lockPid = [int]$lockData.pid
        if ($lockPid -gt 0 -and (Test-ProcessAlive -ProcessId $lockPid)) {
            Write-Host "Status: running"
            Write-Host "PID: $lockPid"
        }
        else {
            Write-Host 'Status: not running (stale lock file present)'
            if ($lockPid -gt 0) {
                Write-Host "Stale PID: $lockPid"
            }
        }
    }
    catch {
        Write-Host 'Status: unknown (lock file unreadable)'
    }
}
else {
    Write-Host 'Status: not running'
}

if (Test-Path $logPath) {
    Write-Host "`nLast 20 log lines:"
    Get-Content -Path $logPath -Tail 20
}
else {
    Write-Host "`nNo log file found at $logPath"
}

Write-Host "`nUseful commands:"
Write-Host '.\scripts\start_local_price_updater.ps1'
Write-Host '.\scripts\stop_local_price_updater.ps1'
Write-Host 'Get-Content .\logs\local_price_updater.log -Tail 100'
