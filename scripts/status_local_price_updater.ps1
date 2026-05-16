param(
    [switch]$ShowLogs,
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

function Get-AestTimeZone {
    try {
        return [System.TimeZoneInfo]::FindSystemTimeZoneById('E. Australia Standard Time')
    }
    catch {
        return $null
    }
}

function Format-DateAest {
    param(
        [string]$Value,
        [System.TimeZoneInfo]$AestTimeZone
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return 'None'
    }

    try {
        $parsed = [datetimeoffset]::Parse($Value)
        if ($AestTimeZone) {
            $converted = [System.TimeZoneInfo]::ConvertTime($parsed, $AestTimeZone)
            return $converted.ToString('dd/MM/yyyy h:mm tt') + ' AEST'
        }
        return $parsed.LocalDateTime.ToString('dd/MM/yyyy h:mm tt') + ' Local'
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

    Write-Host ('{0,-12} {1}' -f ($Label + ':'), $Value)
}

function Get-PhaseLabel {
    param([string]$Phase)

    $phaseValue = ''
    if (-not [string]::IsNullOrWhiteSpace($Phase)) {
        $phaseValue = $Phase.ToLowerInvariant()
    }

    switch ($phaseValue) {
        'sleeping' { return 'Sleeping' }
        'starting' { return 'Starting' }
        'pulling' { return 'Pulling latest' }
        'updating' { return 'Updating prices' }
        'validating' { return 'Validating' }
        'committing' { return 'Committing' }
        'pushing' { return 'Pushing' }
        'error' { return 'Error' }
        'stopped' { return 'Stopped' }
        default { return 'Unknown' }
    }
}

function Truncate-SetList {
    param([object[]]$SetIds)

    if (-not $SetIds -or $SetIds.Count -eq 0) {
        return 'None'
    }

    $maxItems = 10
    $head = @($SetIds | Select-Object -First $maxItems)
    $joined = ($head -join ', ')
    if ($SetIds.Count -gt $maxItems) {
        return "$joined..."
    }
    return $joined
}

$lockData = Read-JsonFile -Path $lockPath
$statusData = Read-JsonFile -Path $statusPath
$resultData = Read-JsonFile -Path $resultPath
$aestTimeZone = Get-AestTimeZone

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
$rawError = if ($statusData -and $statusData.lastError) { [string]$statusData.lastError } elseif ($resultData -and $resultData.error) { [string]$resultData.error } else { $null }
$rawWarning = if ($statusData -and $statusData.PSObject.Properties.Name -contains 'lastWarning' -and $statusData.lastWarning) { [string]$statusData.lastWarning } else { $null }
$lastWarning = $rawWarning
$lastError = $rawError
if (-not $lastWarning -and $lastError -and $lastError -like 'Skipped cycle*') {
    $lastWarning = $lastError
    $lastError = $null
}

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

$statusLabel = if ($running) { 'RUNNING' } else { 'STOPPED' }
$currentLabel = Get-PhaseLabel -Phase $phase

if (-not $running) {
    $nextAction = 'Start updater'
}
elseif ($phase -eq 'sleeping') {
    if ($timeRemainingSeconds -ne $null) {
        $nextAction = "Update prices in $(Format-DurationValue -Seconds $timeRemainingSeconds)"
    }
    else {
        $nextAction = 'Update prices soon'
    }
}
else {
    $nextAction = 'Finish current update'
}

Write-Host 'CardScanR Price Updater'
Write-Host '======================='
Write-Host ''
Write-DashboardLine -Label 'Status' -Value $statusLabel
Write-DashboardLine -Label 'Current' -Value $currentLabel
Write-DashboardLine -Label 'Next action' -Value $nextAction
Write-DashboardLine -Label 'Next update' -Value ($(if ($phase -eq 'sleeping' -and $nextRunUtc) { Format-DateAest -Value $nextRunUtc -AestTimeZone $aestTimeZone } else { 'None' }))

Write-Host ''
Write-DashboardLine -Label 'Last success' -Value (Format-DateAest -Value $lastUpdateUtc -AestTimeZone $aestTimeZone)
Write-DashboardLine -Label 'Last push' -Value (Format-DateAest -Value $lastPushUtc -AestTimeZone $aestTimeZone)
Write-DashboardLine -Label 'Last commit' -Value ($(if ($lastCommitHash) { $lastCommitHash } else { 'None' }))
Write-DashboardLine -Label 'Last duration' -Value ($(if ($lastDurationSeconds -ne $null) { Format-DurationValue -Seconds $lastDurationSeconds } else { 'Unknown' }))

if ($running -and $phase -ne 'sleeping' -and $phase -ne 'stopped') {
    Write-DashboardLine -Label 'Elapsed' -Value ($(if ($currentUpdateElapsedSeconds -ne $null) { Format-DurationValue -Seconds $currentUpdateElapsedSeconds } else { 'None' }))
    Write-DashboardLine -Label 'Est. finish' -Value ($(if ($estimatedFinishUtc) { (Format-DateAest -Value $estimatedFinishUtc -AestTimeZone $aestTimeZone) + ' (estimate)' } else { 'None' }))
}

Write-Host ''
Write-DashboardLine -Label 'Batch' -Value ($(if ($batchSize -ne 'Unknown' -and $intervalMinutes -ne 'Unknown') { "$batchSize sets every $intervalMinutes minutes" } else { 'Unknown' }))
Write-DashboardLine -Label 'Last sets' -Value (Truncate-SetList -SetIds $lastSets)

Write-Host ''
Write-DashboardLine -Label 'Warning' -Value ($(if ($lastWarning) { $lastWarning } else { 'None' }))
Write-DashboardLine -Label 'Error' -Value ($(if ($lastError) { $lastError } else { 'None' }))

if ($ShowLogs) {
    Write-Host ''
    Write-Host 'Recent logs, may include old resolved messages:'
    if (Test-Path $logPath) {
        Get-Content -Path $logPath -Tail 20
    }
    else {
        Write-Host "No log file found at $logPath"
    }
}

Write-Host ''
Write-Host 'Commands:'
Write-Host '  Watch: .\scripts\watch_local_price_updater.ps1'
Write-Host '  Logs:  Get-Content .\logs\local_price_updater.log -Tail 80 -Wait'
Write-Host '  Stop:  .\scripts\stop_local_price_updater.ps1'
