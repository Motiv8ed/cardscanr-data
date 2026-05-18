param(
    [string]$RepoRoot = "",
    [string]$TaskName = "CardScanR PokéWallet Catalogue Worker"
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$configPath = Join-Path $RepoRoot 'data\pokewallet_catalog_config.json'
$statusPath = Join-Path $RepoRoot 'data\pokewallet_catalog_worker_status.json'
$statePath = Join-Path $RepoRoot 'data\pokewallet_catalog_full_state.json'
$diagPath = Join-Path $RepoRoot 'public\v1\diagnostics\pokewallet-catalog-foundation-latest.json'
$workerLockPath = Join-Path $RepoRoot '.pokewallet_catalog_worker.lock'
$cycleLockPath = Join-Path $RepoRoot '.pokewallet_catalog_cycle.lock'

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

function Format-Value {
    param($Value)

    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        return 'None'
    }
    return [string]$Value
}

function Format-Languages {
    param($LanguagesCompleted)

    if ($null -eq $LanguagesCompleted) {
        return 'None'
    }

    $parts = @()
    foreach ($prop in $LanguagesCompleted.PSObject.Properties | Sort-Object Name) {
        $parts += ("{0}={1}" -f $prop.Name, $prop.Value)
    }
    if ($parts.Count -eq 0) {
        return 'None'
    }
    return ($parts -join ', ')
}

function Get-LockStatus {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return [pscustomobject]@{
            Exists = $false
            Pid = 0
            Alive = $false
            Stale = $false
        }
    }

    $data = Read-JsonFile -Path $Path
    $pidValue = 0
    if ($null -ne $data -and $null -ne $data.pid) {
        $pidValue = [int]$data.pid
    }
    $alive = $pidValue -gt 0 -and (Test-ProcessAlive -ProcessId $pidValue)
    return [pscustomobject]@{
        Exists = $true
        Pid = $pidValue
        Alive = $alive
        Stale = -not $alive
    }
}

$config = Read-JsonFile -Path $configPath
$status = Read-JsonFile -Path $statusPath
$state = Read-JsonFile -Path $statePath
$diag = Read-JsonFile -Path $diagPath

$workerConfig = if ($null -ne $config) { $config.fullCatalogueWorker } else { $null }
$logPath = if ($null -ne $workerConfig -and -not [string]::IsNullOrWhiteSpace([string]$workerConfig.logPath)) {
    Join-Path $RepoRoot ([string]$workerConfig.logPath)
}
else {
    Join-Path $RepoRoot 'logs\pokewallet_catalog_worker.log'
}

$interval = if ($null -ne $workerConfig -and $null -ne $workerConfig.intervalMinutes) {
    [int]$workerConfig.intervalMinutes
}
elseif ($null -ne $status -and $null -ne $status.intervalMinutes) {
    [int]$status.intervalMinutes
}
else {
    75
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskExists = $null -ne $task
$taskEnabled = $false
$taskInfo = $null
if ($taskExists) {
    $taskEnabled = $task.State -ne 'Disabled'
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
}

$cycleLock = Get-LockStatus -Path $cycleLockPath
$workerLock = Get-LockStatus -Path $workerLockPath
$currentlyRunning = ($taskExists -and $task.State -eq 'Running') -or $cycleLock.Alive

Write-Host 'CardScanR PokéWallet Catalogue Worker'
Write-Host ''
Write-Host ("Scheduled task exists: {0}" -f ($(if ($taskExists) { 'yes' } else { 'no' })))
Write-Host ("Scheduled task enabled: {0}" -f ($(if ($taskEnabled) { 'yes' } else { 'no' })))
Write-Host ("Scheduled task state: {0}" -f (Format-Value $(if ($taskExists) { $task.State } else { $null })))
Write-Host ("Last run time: {0}" -f (Format-Value $(if ($null -ne $taskInfo) { $taskInfo.LastRunTime } else { $null })))
Write-Host ("Next run time: {0}" -f (Format-Value $(if ($null -ne $taskInfo) { $taskInfo.NextRunTime } else { $null })))
Write-Host ("Last task result: {0}" -f (Format-Value $(if ($null -ne $taskInfo) { $taskInfo.LastTaskResult } else { $null })))
Write-Host ''
Write-Host ("Worker status: {0}" -f ($(if ($currentlyRunning) { 'Running' } else { 'Stopped' })))
Write-Host ("Interval: {0} minutes" -f $interval)
Write-Host ("Last cycle started: {0}" -f (Format-Value $(if ($null -ne $status) { $status.lastCycleStartedAtUtc } else { $null })))
Write-Host ("Last cycle finished: {0}" -f (Format-Value $(if ($null -ne $status) { $status.lastCycleFinishedAtUtc } else { $null })))
Write-Host ("Worker last result: {0}" -f (Format-Value $(if ($null -ne $status) { $status.lastStatus } else { $null })))
Write-Host ("Worker last commit: {0}" -f (Format-Value $(if ($null -ne $status) { $status.lastCommit } else { $null })))
Write-Host ("Worker last error: {0}" -f (Format-Value $(if ($null -ne $status) { $status.lastError } else { $null })))
Write-Host ''
Write-Host ("Cards written total: {0}" -f (Format-Value $(if ($null -ne $state) { $state.cardsWrittenTotal } else { $null })))
Write-Host ("Requests attempted total: {0}" -f (Format-Value $(if ($null -ne $state) { $state.requestsAttemptedTotal } else { $null })))
Write-Host ("Requests succeeded total: {0}" -f (Format-Value $(if ($null -ne $state) { $state.requestsSucceededTotal } else { $null })))
Write-Host ("Requests failed total: {0}" -f (Format-Value $(if ($null -ne $state) { $state.requestsFailedTotal } else { $null })))
Write-Host ("Languages completed: {0}" -f (Format-Languages $(if ($null -ne $state) { $state.languagesCompleted } else { $null })))
Write-Host ("Latest diagnostic status: {0}" -f (Format-Value $(if ($null -ne $diag) { $diag.status } else { $null })))
Write-Host ("Log path: {0}" -f $logPath)

if ($cycleLock.Stale) {
    Write-Host ''
    Write-Host ("Warning: stale cycle lock exists for PID {0}." -f (Format-Value $cycleLock.Pid))
}
if ($workerLock.Stale) {
    Write-Host ''
    Write-Host ("Warning: stale legacy worker lock exists for PID {0}." -f (Format-Value $workerLock.Pid))
}
