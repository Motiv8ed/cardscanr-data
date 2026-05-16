param(
    [string]$RepoRoot = ""
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$lockPath = Join-Path $RepoRoot '.local_updater.lock'

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

if (-not (Test-Path $lockPath)) {
    Write-Host 'Updater is not running (no lock file found).'
    exit 0
}

$lockRaw = Get-Content -Path $lockPath -Raw -ErrorAction SilentlyContinue
if ([string]::IsNullOrWhiteSpace($lockRaw)) {
    Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    Write-Host 'Removed empty stale lock file.'
    exit 0
}

try {
    $lockData = $lockRaw | ConvertFrom-Json
}
catch {
    Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    Write-Host 'Removed unreadable stale lock file.'
    exit 0
}

$targetPid = 0
try {
    $targetPid = [int]$lockData.pid
}
catch {
    $targetPid = 0
}

if ($targetPid -le 0) {
    Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    Write-Host 'Removed lock file with invalid PID.'
    exit 0
}

if (Test-ProcessAlive -ProcessId $targetPid) {
    Stop-Process -Id $targetPid -Force -ErrorAction Stop
    Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped updater process PID $targetPid and removed lock file."
    exit 0
}

Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
Write-Host "Updater process PID $targetPid was not running; removed stale lock file."
