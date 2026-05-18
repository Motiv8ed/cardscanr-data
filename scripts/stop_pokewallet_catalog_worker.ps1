param(
    [string]$RepoRoot = "",
    [string]$TaskName = "CardScanR PokéWallet Catalogue Worker"
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$statusPath = Join-Path $RepoRoot 'data\pokewallet_catalog_worker_status.json'
$workerLockPath = Join-Path $RepoRoot '.pokewallet_catalog_worker.lock'
$cycleLockPath = Join-Path $RepoRoot '.pokewallet_catalog_cycle.lock'
$uninstallScript = Join-Path $RepoRoot 'scripts\uninstall_pokewallet_catalog_scheduled_task.ps1'

function Get-UtcIso {
    return ([datetime]::UtcNow).ToString('yyyy-MM-ddTHH:mm:ssZ')
}

function Read-JsonFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $null
    }
    try {
        return (Get-Content -Path $Path -Raw -Encoding UTF8 -ErrorAction Stop) | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Write-JsonFile {
    param(
        [string]$Path,
        [object]$Payload
    )

    $parent = Split-Path -Parent $Path
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    $tmpPath = "$Path.tmp"
    $json = $Payload | ConvertTo-Json -Depth 10
    Set-Content -Path $tmpPath -Value $json -Encoding UTF8
    Move-Item -Path $tmpPath -Destination $Path -Force
}

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

function Stop-LockProcess {
    param(
        [string]$Path,
        [string]$Label
    )

    $lockData = Read-JsonFile -Path $Path
    $targetPid = 0
    if ($null -ne $lockData -and $null -ne $lockData.pid) {
        $targetPid = [int]$lockData.pid
    }

    if ($targetPid -gt 0 -and (Test-ProcessAlive -ProcessId $targetPid)) {
        Stop-Process -Id $targetPid -Force -ErrorAction Stop
        Write-Host ("Stopped {0} process PID {1}." -f $Label, $targetPid)
    }
    elseif ($targetPid -gt 0) {
        Write-Host ("Removed stale {0} lock for dead PID {1}." -f $Label, $targetPid)
    }

    Remove-Item -Path $Path -Force -ErrorAction SilentlyContinue
}

if (Test-Path $uninstallScript) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uninstallScript -TaskName $TaskName
    if ($LASTEXITCODE -ne 0) {
        throw "Scheduled task uninstall failed with exit code $LASTEXITCODE."
    }
}
else {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task: $TaskName"
    }
}

if (Test-Path $cycleLockPath) {
    Stop-LockProcess -Path $cycleLockPath -Label 'catalogue cycle'
}
if (Test-Path $workerLockPath) {
    Stop-LockProcess -Path $workerLockPath -Label 'legacy catalogue worker'
}

$existing = Read-JsonFile -Path $statusPath
$payload = [ordered]@{
    schemaVersion = '1.0.0'
    running = $false
    pid = $null
    startedAtUtc = if ($null -ne $existing) { $existing.startedAtUtc } else { $null }
    lastCycleStartedAtUtc = if ($null -ne $existing) { $existing.lastCycleStartedAtUtc } else { $null }
    lastCycleFinishedAtUtc = if ($null -ne $existing) { $existing.lastCycleFinishedAtUtc } else { Get-UtcIso }
    nextCycleAtUtc = $null
    intervalMinutes = if ($null -ne $existing -and $null -ne $existing.intervalMinutes) { [int]$existing.intervalMinutes } else { 75 }
    lastStatus = 'stopped'
    lastCommit = if ($null -ne $existing) { $existing.lastCommit } else { $null }
    lastError = $null
}
Write-JsonFile -Path $statusPath -Payload $payload
Write-Host 'Catalogue worker status updated to stopped.'
