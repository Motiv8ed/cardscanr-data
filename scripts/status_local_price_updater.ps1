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
$publicPricesStatusPath = Join-Path $RepoRoot 'public\v1\prices\status.json'

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

    Write-Host ('{0,-18} {1}' -f ($Label + ':'), $Value)
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
$publicPricesStatus = Read-JsonFile -Path $publicPricesStatusPath
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

$publicEnStatus = $null
if ($publicPricesStatus -and $publicPricesStatus.languages -and $publicPricesStatus.languages.en) {
    $publicEnStatus = $publicPricesStatus.languages.en
}

$publicEnStaleness = if ($publicEnStatus -and $publicEnStatus.staleness -and $publicEnStatus.staleness.status) { [string]$publicEnStatus.staleness.status } else { 'unavailable' }
$publicEnAgeSeconds = if ($publicEnStatus -and $publicEnStatus.staleness -and $publicEnStatus.staleness.ageSeconds -ne $null) { [int]$publicEnStatus.staleness.ageSeconds } else { $null }
$publicEnSetCount = if ($publicEnStatus -and $publicEnStatus.currentPriceSetFileCount -ne $null) { [int]$publicEnStatus.currentPriceSetFileCount } else { 0 }
$publicEnRecordCount = if ($publicEnStatus -and $publicEnStatus.currentPriceRecordCount -ne $null) { [int]$publicEnStatus.currentPriceRecordCount } else { 0 }
$publicEnNextExpectedUtc = if ($publicEnStatus -and $publicEnStatus.nextExpectedPriceUpdateAtUtc) { [string]$publicEnStatus.nextExpectedPriceUpdateAtUtc } else { $null }
$publicEnIntervalMinutes = if ($publicEnStatus -and $publicEnStatus.expectedUpdateIntervalMinutes -ne $null) { [int]$publicEnStatus.expectedUpdateIntervalMinutes } else { $null }
$publicEnRotationHours = if ($publicEnStatus -and $publicEnStatus.fullRotationEstimatedHours -ne $null) { [int]$publicEnStatus.fullRotationEstimatedHours } else { $null }
$publicEnLastUpdateUtc = if ($publicEnStatus -and $publicEnStatus.lastSuccessfulPriceUpdateAtUtc) { [string]$publicEnStatus.lastSuccessfulPriceUpdateAtUtc } else { $lastUpdateUtc }
$publicEnLastPushUtc = if ($publicEnStatus -and $publicEnStatus.lastSuccessfulPushAtUtc) { [string]$publicEnStatus.lastSuccessfulPushAtUtc } else { $lastPushUtc }
$publicEnAgeText = if ($publicEnAgeSeconds -ne $null) { Format-DurationValue -Seconds $publicEnAgeSeconds } else { 'Unknown' }
$publicEnFreshnessLabel = switch ($publicEnStaleness) {
    'fresh' { 'Fresh' }
    'stale' { 'Stale' }
    'very_stale' { 'Very stale' }
    default { 'Unavailable' }
}

$statusLabel = if ($running) { 'RUNNING' } else { 'STOPPED' }
$currentLabel = Get-PhaseLabel -Phase $phase
$nextUpdateCycleText = 'None'
$timeUntilUpdateText = 'None'
$nextPushText = 'None'
$currentUpdateText = 'None'
$elapsedText = 'None'
$estimatedFinishText = 'None'

if (-not $running -or $phase -eq 'stopped') {
    $nextUpdateCycleText = 'Updater is stopped'
    $timeUntilUpdateText = 'None'
    $nextPushText = 'None until updater is started'
    $currentUpdateText = 'Stopped'
}
elseif ($phase -eq 'sleeping') {
    $currentUpdateText = 'Sleeping'
    $nextUpdateCycleText = if ($nextRunUtc) { Format-DateAest -Value $nextRunUtc -AestTimeZone $aestTimeZone } else { 'Unknown' }
    $timeUntilUpdateText = if ($timeRemainingSeconds -ne $null) { Format-DurationValue -Seconds $timeRemainingSeconds } else { 'Unknown' }
    $nextPushText = 'After next successful update'
}
elseif ($phase -eq 'pushing') {
    $currentUpdateText = 'Pushing to GitHub'
    $elapsedText = if ($currentUpdateElapsedSeconds -ne $null) { Format-DurationValue -Seconds $currentUpdateElapsedSeconds } else { 'Unknown' }
    $estimatedFinishText = if ($estimatedFinishUtc) { (Format-DateAest -Value $estimatedFinishUtc -AestTimeZone $aestTimeZone) + ' (estimate)' } else { 'None' }
    $nextUpdateCycleText = 'After current cycle completes'
    $timeUntilUpdateText = 'In progress'
    $nextPushText = 'In progress'
}
elseif ($phase -in @('updating', 'pulling', 'validating', 'committing', 'starting')) {
    $currentUpdateText = 'Running'
    $elapsedText = if ($currentUpdateElapsedSeconds -ne $null) { Format-DurationValue -Seconds $currentUpdateElapsedSeconds } else { 'Unknown' }
    $estimatedFinishText = if ($estimatedFinishUtc) { (Format-DateAest -Value $estimatedFinishUtc -AestTimeZone $aestTimeZone) + ' (estimate)' } else { 'None' }
    $nextUpdateCycleText = 'After current cycle completes'
    $timeUntilUpdateText = 'In progress'
    $nextPushText = 'After validation succeeds'
}
else {
    $currentUpdateText = $currentLabel
    $nextUpdateCycleText = 'After current cycle completes'
    $timeUntilUpdateText = 'In progress'
    $nextPushText = 'After current cycle completes'
}

Write-Host 'CardScanR Price Updater'
Write-Host '======================='
Write-Host ''
Write-DashboardLine -Label 'Status' -Value $statusLabel
Write-DashboardLine -Label 'Current state' -Value $currentLabel
Write-DashboardLine -Label 'Batch size' -Value ($(if ($batchSize -ne 'Unknown') { "$batchSize sets" } else { 'Unknown' }))
Write-DashboardLine -Label 'Interval' -Value ($(if ($intervalMinutes -ne 'Unknown') { "$intervalMinutes minutes" } else { 'Unknown' }))

Write-Host ''
Write-DashboardLine -Label 'EN freshness' -Value $publicEnFreshnessLabel
Write-DashboardLine -Label 'EN age' -Value $publicEnAgeText
Write-DashboardLine -Label 'EN files' -Value ("$publicEnSetCount sets, $publicEnRecordCount records")
Write-DashboardLine -Label 'EN next expected' -Value ($(if ($publicEnNextExpectedUtc) { Format-DateAest -Value $publicEnNextExpectedUtc -AestTimeZone $aestTimeZone } else { 'Unknown' }))
Write-DashboardLine -Label 'EN cadence' -Value ($(if ($publicEnIntervalMinutes -ne $null) { "$publicEnIntervalMinutes minutes" } else { 'Unknown' }))
Write-DashboardLine -Label 'EN rotation' -Value ($(if ($publicEnRotationHours -ne $null) { "$publicEnRotationHours hours" } else { 'Unknown' }))

Write-Host ''
Write-DashboardLine -Label 'Next update cycle' -Value $nextUpdateCycleText
Write-DashboardLine -Label 'Time until update' -Value $timeUntilUpdateText
Write-DashboardLine -Label 'Next push' -Value $nextPushText

if ($running -and $phase -ne 'sleeping' -and $phase -ne 'stopped') {
    Write-DashboardLine -Label 'Current update' -Value $currentUpdateText
    Write-DashboardLine -Label 'Elapsed' -Value $elapsedText
    Write-DashboardLine -Label 'Estimated finish' -Value $estimatedFinishText
}

Write-Host ''
Write-DashboardLine -Label 'Last success' -Value (Format-DateAest -Value $publicEnLastUpdateUtc -AestTimeZone $aestTimeZone)
Write-DashboardLine -Label 'Last push' -Value (Format-DateAest -Value $publicEnLastPushUtc -AestTimeZone $aestTimeZone)
Write-DashboardLine -Label 'Last commit' -Value ($(if ($lastCommitHash) { $lastCommitHash } else { 'None' }))
Write-DashboardLine -Label 'Last duration' -Value ($(if ($lastDurationSeconds -ne $null) { Format-DurationValue -Seconds $lastDurationSeconds } else { 'Unknown' }))

Write-Host ''
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
