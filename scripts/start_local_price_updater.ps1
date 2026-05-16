param(
    [int]$BatchSize = 10,
    [int]$IntervalMinutes = 120,
    [string]$RepoRoot = ""
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$lockPath = Join-Path $RepoRoot '.local_updater.lock'
$logPath = Join-Path $RepoRoot 'logs\local_price_updater.log'
$loopScript = Join-Path $RepoRoot 'scripts\local_price_updater_loop.ps1'

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

if (Test-Path $lockPath) {
    try {
        $lockData = (Get-Content -Path $lockPath -Raw) | ConvertFrom-Json
        $existingPid = [int]$lockData.pid
        if ($existingPid -gt 0 -and (Test-ProcessAlive -ProcessId $existingPid)) {
            Write-Host "Updater is already running (PID $existingPid)."
            exit 1
        }

        Write-Host "Removing stale lock file."
        Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    }
    catch {
        Write-Host "Lock file exists but is unreadable. Remove $lockPath and try again."
        exit 1
    }
}

$BatchSize = [Math]::Max(1, $BatchSize)
$IntervalMinutes = [Math]::Max(1, $IntervalMinutes)

$argList = @(
    '-NoProfile',
    '-ExecutionPolicy',
    'Bypass',
    '-WindowStyle',
    'Hidden',
    '-File',
    $loopScript,
    '-BatchSize',
    $BatchSize,
    '-IntervalMinutes',
    $IntervalMinutes,
    '-RepoRoot',
    $RepoRoot
)

$process = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WindowStyle Hidden -PassThru

Write-Host "Updater started in background. PID: $($process.Id)"
Write-Host "Logs: $logPath"
