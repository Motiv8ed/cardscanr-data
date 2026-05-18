param(
    [string]$RepoRoot = ""
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

$config = Read-JsonFile -Path $configPath
$status = Read-JsonFile -Path $statusPath
$state = Read-JsonFile -Path $statePath
$diag = Read-JsonFile -Path $diagPath

$workerConfig = $null
if ($null -ne $config) {
    $workerConfig = $config.fullCatalogueWorker
}

$lockPath = if ($null -ne $workerConfig) { Join-Path $RepoRoot ([string]$workerConfig.lockPath) } else { Join-Path $RepoRoot '.pokewallet_catalog_worker.lock' }
$logPath = if ($null -ne $workerConfig) { Join-Path $RepoRoot ([string]$workerConfig.logPath) } else { Join-Path $RepoRoot 'logs\pokewallet_catalog_worker.log' }

$pidValue = if ($null -ne $status -and $null -ne $status.pid) { [int]$status.pid } else { 0 }
$running = $false
if ($pidValue -gt 0) {
    $running = Test-ProcessAlive -ProcessId $pidValue
}
if (-not (Test-Path $lockPath)) {
    $running = $false
}

$interval = if ($null -ne $status -and $null -ne $status.intervalMinutes) {
    [int]$status.intervalMinutes
}
elseif ($null -ne $workerConfig -and $null -ne $workerConfig.intervalMinutes) {
    [int]$workerConfig.intervalMinutes
}
else {
    75
}

Write-Host 'CardScanR Pokewallet Catalogue Worker'
Write-Host ''
Write-Host ("Status: {0}" -f ($(if ($running) { 'Running' } else { 'Stopped' })))
Write-Host ("PID: {0}" -f ($(if ($pidValue -gt 0) { $pidValue } else { 'None' })))
Write-Host ("Interval: {0} minutes" -f $interval)
Write-Host ("Last cycle: {0}" -f (Format-Value $(if ($null -ne $status) { $status.lastCycleFinishedAtUtc } else { $null })))
Write-Host ("Next cycle: {0}" -f (Format-Value $(if ($running -and $null -ne $status) { $status.nextCycleAtUtc } else { $null })))
Write-Host ("Last result: {0}" -f (Format-Value $(if ($null -ne $status) { $status.lastStatus } else { $null })))
Write-Host ("Last commit: {0}" -f (Format-Value $(if ($null -ne $status) { $status.lastCommit } else { $null })))
Write-Host ("Cards written total: {0}" -f (Format-Value $(if ($null -ne $state) { $state.cardsWrittenTotal } else { $null })))
Write-Host ("Requests attempted total: {0}" -f (Format-Value $(if ($null -ne $state) { $state.requestsAttemptedTotal } else { $null })))
Write-Host ("Requests succeeded total: {0}" -f (Format-Value $(if ($null -ne $state) { $state.requestsSucceededTotal } else { $null })))
Write-Host ("Requests failed total: {0}" -f (Format-Value $(if ($null -ne $state) { $state.requestsFailedTotal } else { $null })))
Write-Host ("Languages completed: {0}" -f (Format-Languages $(if ($null -ne $state) { $state.languagesCompleted } else { $null })))
Write-Host ("Latest diagnostic status: {0}" -f (Format-Value $(if ($null -ne $diag) { $diag.status } else { $null })))
Write-Host ("Log path: {0}" -f $logPath)
