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
$statusPath = Join-Path $RepoRoot 'logs\local_price_updater_status.json'
$resultPath = Join-Path $RepoRoot 'logs\local_price_update_last_result.json'

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

function Read-JsonFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $null
    }

    try {
        return (Get-Content -Path $Path -Raw -ErrorAction Stop) | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Format-DateValue {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return 'None'
    }

    try {
        return ([datetime]::Parse($Value).ToLocalTime()).ToString('yyyy-MM-dd HH:mm:ss')
    }
    catch {
        return $Value
    }
}

function Format-DurationValue {
    param($Seconds)

    if ($null -eq $Seconds) {
        return 'Unknown'
    }

    $total = [int]$Seconds
    if ($total -lt 0) {
        $total = 0
    }

    $span = [TimeSpan]::FromSeconds($total)
    if ($span.TotalHours -ge 1) {
        return '{0}h {1}m {2}s' -f [int]$span.TotalHours, $span.Minutes, $span.Seconds
    }
    if ($span.TotalMinutes -ge 1) {
        return '{0}m {1}s' -f [int]$span.TotalMinutes, $span.Seconds
    }
    return '{0}s' -f $span.Seconds
}

function Get-SecondsUntil {
    param([string]$UtcValue)

    if ([string]::IsNullOrWhiteSpace($UtcValue)) {
        return $null
    }

    try {
        $target = [datetime]::Parse($UtcValue).ToUniversalTime()
        $seconds = [int][Math]::Floor(($target - [datetime]::UtcNow).TotalSeconds)
        if ($seconds -lt 0) {
            return 0
        }
        return $seconds
    }
    catch {
        return $null
    }
}

function Get-ElapsedSince {
    param([string]$UtcValue)

    if ([string]::IsNullOrWhiteSpace($UtcValue)) {
        return $null
    }

    try {
        $started = [datetime]::Parse($UtcValue).ToUniversalTime()
        $seconds = [int][Math]::Floor(([datetime]::UtcNow - $started).TotalSeconds)
        if ($seconds -lt 0) {
            return 0
        }
        return $seconds
    }
    catch {
        return $null
    }
}

function Write-DashboardLine {
    param(
        [string]$Label,
        [string]$Value
    )

    Write-Host ('{0,-16} {1}' -f ($Label + ':'), $Value)
}

$lockData = Read-JsonFile -Path $lockPath
$statusData = Read-JsonFile -Path $statusPath
$resultData = Read-JsonFile -Path $resultPath

$running = $false
$displayPid = $null
if ($lockData -and $lockData.pid) {
    $displayPid = [int]$lockData.pid
    if ($displayPid -gt 0 -and (Test-ProcessAlive -ProcessId $displayPid)) {
        $running = $true
    }
}
elseif ($statusData -and $statusData.pid) {
    $displayPid = [int]$statusData.pid
    if ($displayPid -gt 0 -and (Test-ProcessAlive -ProcessId $displayPid)) {
        $running = $true
    }
}

$phase = 'stopped'
if ($statusData -and $statusData.currentPhase) {
    $phase = [string]$statusData.currentPhase
}
if (-not $running -and $phase -ne 'stopped') {
    $phase = 'stopped'
}

$batchSize = if ($statusData -and $statusData.batchSize) { [string]$statusData.batchSize } elseif ($lockData -and $lockData.batchSize) { [string]$lockData.batchSize } else { 'Unknown' }
$intervalMinutes = if ($statusData -and $statusData.intervalMinutes) { [string]$statusData.intervalMinutes } elseif ($lockData -and $lockData.intervalMinutes) { [string]$lockData.intervalMinutes } else { 'Unknown' }
$lastUpdateUtc = if ($statusData -and $statusData.lastSuccessfulUpdateAtUtc) { $statusData.lastSuccessfulUpdateAtUtc } elseif ($resultData) { $resultData.finishedAtUtc } else { $null }
$lastPushUtc = if ($statusData -and $statusData.lastSuccessfulPushAtUtc) { $statusData.lastSuccessfulPushAtUtc } elseif ($resultData -and $resultData.pushSucceeded) { $resultData.finishedAtUtc } else { $null }
$lastCommitHash = if ($statusData -and $statusData.lastCommitHash) { [string]$statusData.lastCommitHash } elseif ($resultData) { [string]$resultData.commitHash } else { $null }
$lastDurationSeconds = if ($statusData -and $statusData.lastCycleDurationSeconds) { [int]$statusData.lastCycleDurationSeconds } elseif ($resultData) { [int]$resultData.durationSeconds } else { $null }
$nextRunUtc = if ($running -and $statusData) { $statusData.nextRunAtUtc } else { $null }
$timeRemainingSeconds = if ($running -and $statusData) { Get-SecondsUntil -UtcValue $nextRunUtc } else { $null }
$currentUpdateStartedUtc = if ($statusData) { $statusData.currentUpdateStartedAtUtc } else { $null }
$currentUpdateElapsedSeconds = if ($statusData -and $statusData.currentUpdateStartedAtUtc) { Get-ElapsedSince -UtcValue $statusData.currentUpdateStartedAtUtc } else { $null }
$estimatedFinishUtc = if ($statusData) { $statusData.estimatedCurrentUpdateFinishAtUtc } else { $null }
$lastError = if ($statusData -and $statusData.lastError) { [string]$statusData.lastError } elseif ($resultData -and $resultData.error) { [string]$resultData.error } else { $null }
$lastSets = @()
if ($statusData -and $statusData.lastBatchSetIds) {
    $lastSets = @($statusData.lastBatchSetIds)
}
elseif ($resultData -and $resultData.updatedSetIds -and $resultData.updatedSetIds.Count -gt 0) {
    $lastSets = @($resultData.updatedSetIds)
}
elseif ($resultData -and $resultData.plannedSetIds) {
    $lastSets = @($resultData.plannedSetIds)
}

Write-Host 'CardScanR Local Price Updater'
Write-Host '============================='
Write-DashboardLine -Label 'Running' -Value ($(if ($running) { 'YES' } else { 'NO' }))
Write-DashboardLine -Label 'PID' -Value ($(if ($displayPid) { [string]$displayPid } else { 'None' }))
Write-DashboardLine -Label 'Phase' -Value $phase
Write-DashboardLine -Label 'State' -Value ($(if ($phase -eq 'sleeping') { 'sleeping' } elseif ($running) { 'updating' } else { 'stopped' }))
Write-DashboardLine -Label 'Batch size' -Value $batchSize
Write-DashboardLine -Label 'Interval' -Value ($(if ($intervalMinutes -ne 'Unknown') { "$intervalMinutes minutes" } else { 'Unknown' }))
Write-DashboardLine -Label 'Started' -Value ($(if ($statusData) { Format-DateValue -Value $statusData.startedAtUtc } else { 'Unknown' }))
Write-DashboardLine -Label 'Cycle number' -Value ($(if ($statusData) { [string]$statusData.cycleNumber } else { 'Unknown' }))
Write-Host ''
Write-DashboardLine -Label 'Update start' -Value ($(if ($currentUpdateStartedUtc) { Format-DateValue -Value $currentUpdateStartedUtc } else { 'None' }))
Write-DashboardLine -Label 'Update elapsed' -Value ($(if ($currentUpdateElapsedSeconds -ne $null) { Format-DurationValue -Seconds $currentUpdateElapsedSeconds } else { 'None' }))
Write-DashboardLine -Label 'Est. finish' -Value ($(if ($estimatedFinishUtc) { (Format-DateValue -Value $estimatedFinishUtc) + ' (estimate)' } else { 'None' }))
Write-DashboardLine -Label 'Last update' -Value (Format-DateValue -Value $lastUpdateUtc)
Write-DashboardLine -Label 'Last push' -Value (Format-DateValue -Value $lastPushUtc)
Write-DashboardLine -Label 'Last commit' -Value ($(if ($lastCommitHash) { $lastCommitHash } else { 'None' }))
Write-DashboardLine -Label 'Last duration' -Value ($(if ($lastDurationSeconds -ne $null) { Format-DurationValue -Seconds $lastDurationSeconds } else { 'Unknown' }))
Write-Host ''
Write-DashboardLine -Label 'Next update' -Value ($(if ($nextRunUtc) { Format-DateValue -Value $nextRunUtc } else { 'None' }))
Write-DashboardLine -Label 'Time remaining' -Value ($(if ($timeRemainingSeconds -ne $null) { Format-DurationValue -Seconds $timeRemainingSeconds } else { 'None' }))
Write-Host ''
Write-Host 'Last sets:'
if ($lastSets.Count -gt 0) {
    foreach ($setId in $lastSets) {
        Write-Host "- $setId"
    }
}
else {
    Write-Host 'None'
}

Write-Host ''
Write-Host 'Last error:'
if ($lastError) {
    Write-Host $lastError
}
else {
    Write-Host 'None'
}

Write-Host ''
Write-Host 'Recent logs:'
if (Test-Path $logPath) {
    Get-Content -Path $logPath -Tail 20
}
else {
    Write-Host "No log file found at $logPath"
}

Write-Host ''
Write-Host 'Useful commands:'
Write-Host '.\scripts\start_local_price_updater.ps1'
Write-Host '.\scripts\stop_local_price_updater.ps1'
Write-Host '.\scripts\watch_local_price_updater.ps1'
Write-Host 'Get-Content .\logs\local_price_updater.log -Tail 80 -Wait'
