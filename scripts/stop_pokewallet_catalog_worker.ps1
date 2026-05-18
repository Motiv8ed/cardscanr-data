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

$config = Read-JsonFile -Path $configPath
if ($null -eq $config -or $null -eq $config.fullCatalogueWorker) {
    throw 'fullCatalogueWorker config is missing.'
}

$lockPath = Join-Path $RepoRoot ([string]$config.fullCatalogueWorker.lockPath)
$status = Read-JsonFile -Path $statusPath

$targetPid = 0
if (Test-Path $lockPath) {
    $lockData = Read-JsonFile -Path $lockPath
    if ($null -ne $lockData -and $null -ne $lockData.pid) {
        $targetPid = [int]$lockData.pid
    }
}
elseif ($null -ne $status -and $null -ne $status.pid) {
    $targetPid = [int]$status.pid
}

if ($targetPid -gt 0 -and (Test-ProcessAlive -ProcessId $targetPid)) {
    Stop-Process -Id $targetPid -Force -ErrorAction Stop
    Write-Host "Stopped catalogue worker process PID $targetPid."
}
elseif ($targetPid -gt 0) {
    Write-Host "Catalogue worker process PID $targetPid was not running."
}
else {
    Write-Host 'Catalogue worker is not running.'
}

Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue

$updatedStatus = [ordered]@{
    schemaVersion = '1.0.0'
    running = $false
    pid = $targetPid
    startedAtUtc = if ($null -ne $status) { $status.startedAtUtc } else { $null }
    lastCycleStartedAtUtc = if ($null -ne $status) { $status.lastCycleStartedAtUtc } else { $null }
    lastCycleFinishedAtUtc = if ($null -ne $status) { $status.lastCycleFinishedAtUtc } else { $null }
    nextCycleAtUtc = $null
    intervalMinutes = if ($null -ne $status -and $null -ne $status.intervalMinutes) { [int]$status.intervalMinutes } else { [int]$config.fullCatalogueWorker.intervalMinutes }
    lastStatus = 'stopped'
    lastCommit = if ($null -ne $status) { $status.lastCommit } else { $null }
    lastError = $null
}
Write-JsonFile -Path $statusPath -Payload $updatedStatus
Write-Host 'Catalogue worker status updated to stopped.'
