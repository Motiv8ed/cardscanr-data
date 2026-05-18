param(
    [string]$RepoRoot = "",
    [int]$IntervalMinutes = 0,
    [int]$MaxRequests = 0,
    [switch]$WorkerLoop
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$configPath = Join-Path $RepoRoot 'data\pokewallet_catalog_config.json'
$statusPath = Join-Path $RepoRoot 'data\pokewallet_catalog_worker_status.json'
$cycleScript = Join-Path $RepoRoot 'scripts\run_pokewallet_catalog_cycle.ps1'

function Get-UtcIso {
    param([datetime]$Value = ([datetime]::UtcNow))
    return $Value.ToString('yyyy-MM-ddTHH:mm:ssZ')
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

function Get-WorkerConfig {
    $config = Read-JsonFile -Path $configPath
    if ($null -eq $config -or $null -eq $config.fullCatalogueWorker) {
        throw 'fullCatalogueWorker config is missing.'
    }
    return $config.fullCatalogueWorker
}

function New-StatusObject {
    param(
        [bool]$Running,
        $PidValue,
        [string]$StartedAtUtc,
        [string]$LastCycleStartedAtUtc,
        [string]$LastCycleFinishedAtUtc,
        [string]$NextCycleAtUtc,
        [int]$Interval,
        [string]$LastStatus,
        [string]$LastCommit,
        [string]$LastError
    )

    return [ordered]@{
        schemaVersion = '1.0.0'
        running = $Running
        pid = $PidValue
        startedAtUtc = $StartedAtUtc
        lastCycleStartedAtUtc = $LastCycleStartedAtUtc
        lastCycleFinishedAtUtc = $LastCycleFinishedAtUtc
        nextCycleAtUtc = $NextCycleAtUtc
        intervalMinutes = $Interval
        lastStatus = $LastStatus
        lastCommit = $LastCommit
        lastError = $LastError
    }
}

function Write-WorkerLog {
    param(
        [string]$Path,
        [string]$Message
    )

    $parent = Split-Path -Parent $Path
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    Add-Content -Path $Path -Value "$(Get-UtcIso) $Message" -Encoding UTF8
}

$workerConfig = Get-WorkerConfig
if ($IntervalMinutes -le 0) {
    $IntervalMinutes = [int]$workerConfig.intervalMinutes
}
if ($IntervalMinutes -le 0) {
    $IntervalMinutes = 75
}
if ($MaxRequests -le 0) {
    $MaxRequests = [int]$workerConfig.maxRequestsPerCycle
}
if ($MaxRequests -le 0) {
    $MaxRequests = 80
}

$lockPath = Join-Path $RepoRoot ([string]$workerConfig.lockPath)
$logPath = Join-Path $RepoRoot ([string]$workerConfig.logPath)
$stopOnRateLimit = [bool]$workerConfig.stopOnRateLimit

if ($WorkerLoop) {
    $startedAt = Get-UtcIso
    $lockPayload = [ordered]@{
        schemaVersion = '1.0.0'
        pid = $PID
        startedAtUtc = $startedAt
        intervalMinutes = $IntervalMinutes
        maxRequestsPerCycle = $MaxRequests
    }
    Write-JsonFile -Path $lockPath -Payload $lockPayload
    Write-JsonFile -Path $statusPath -Payload (New-StatusObject -Running $true -PidValue $PID -StartedAtUtc $startedAt -LastCycleStartedAtUtc $null -LastCycleFinishedAtUtc $null -NextCycleAtUtc $startedAt -Interval $IntervalMinutes -LastStatus 'starting' -LastCommit $null -LastError $null)
    Write-WorkerLog -Path $logPath -Message "Worker loop started. PID=$PID intervalMinutes=$IntervalMinutes maxRequests=$MaxRequests"

    while ($true) {
        $cycleStarted = Get-UtcIso
        Write-JsonFile -Path $statusPath -Payload (New-StatusObject -Running $true -PidValue $PID -StartedAtUtc $startedAt -LastCycleStartedAtUtc $cycleStarted -LastCycleFinishedAtUtc $null -NextCycleAtUtc $null -Interval $IntervalMinutes -LastStatus 'running_cycle' -LastCommit $null -LastError $null)
        Write-WorkerLog -Path $logPath -Message 'Starting catalogue cycle.'

        $cycleOutput = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $cycleScript -RepoRoot $RepoRoot -MaxRequests $MaxRequests -AllowWhenWorkerLocked 2>&1)
        foreach ($line in $cycleOutput) {
            Add-Content -Path $logPath -Value $line -Encoding UTF8
        }

        $cycleStatus = 'unknown'
        $cycleCommit = $null
        $cycleMessage = $null
        foreach ($line in $cycleOutput) {
            $text = [string]$line
            if ($text.StartsWith('WORKER_CYCLE_STATUS=')) {
                $cycleStatus = $text.Substring('WORKER_CYCLE_STATUS='.Length)
            }
            elseif ($text.StartsWith('WORKER_CYCLE_COMMIT=')) {
                $value = $text.Substring('WORKER_CYCLE_COMMIT='.Length)
                if (-not [string]::IsNullOrWhiteSpace($value)) {
                    $cycleCommit = $value
                }
            }
            elseif ($text.StartsWith('WORKER_CYCLE_MESSAGE=')) {
                $cycleMessage = $text.Substring('WORKER_CYCLE_MESSAGE='.Length)
            }
        }

        $cycleFinished = Get-UtcIso
        if ($stopOnRateLimit -and $cycleStatus -eq 'rate_limited') {
            Write-WorkerLog -Path $logPath -Message 'Stopping worker because the provider returned rate limit status.'
            Write-JsonFile -Path $statusPath -Payload (New-StatusObject -Running $false -PidValue $PID -StartedAtUtc $startedAt -LastCycleStartedAtUtc $cycleStarted -LastCycleFinishedAtUtc $cycleFinished -NextCycleAtUtc $null -Interval $IntervalMinutes -LastStatus $cycleStatus -LastCommit $cycleCommit -LastError $cycleMessage)
            Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
            break
        }

        $nextCycle = Get-UtcIso -Value ([datetime]::UtcNow.AddMinutes($IntervalMinutes))
        $lastError = $null
        if ($cycleStatus -notin @('ok', 'no_changes')) {
            $lastError = $cycleMessage
        }
        Write-JsonFile -Path $statusPath -Payload (New-StatusObject -Running $true -PidValue $PID -StartedAtUtc $startedAt -LastCycleStartedAtUtc $cycleStarted -LastCycleFinishedAtUtc $cycleFinished -NextCycleAtUtc $nextCycle -Interval $IntervalMinutes -LastStatus $cycleStatus -LastCommit $cycleCommit -LastError $lastError)
        Write-WorkerLog -Path $logPath -Message "Cycle finished with status=$cycleStatus nextCycleAtUtc=$nextCycle"

        Start-Sleep -Seconds ([Math]::Max(1, $IntervalMinutes * 60))
    }
    exit 0
}

if (Test-Path $lockPath) {
    $lockData = Read-JsonFile -Path $lockPath
    $existingPid = 0
    if ($null -ne $lockData -and $null -ne $lockData.pid) {
        $existingPid = [int]$lockData.pid
    }
    if ($existingPid -gt 0 -and (Test-ProcessAlive -ProcessId $existingPid)) {
        Write-Host "Catalogue worker is already running (PID $existingPid)."
        exit 1
    }
    Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    Write-Host 'Removed stale catalogue worker lock file.'
}

$argList = @(
    '-NoProfile',
    '-ExecutionPolicy',
    'Bypass',
    '-WindowStyle',
    'Hidden',
    '-File',
    $PSCommandPath,
    '-RepoRoot',
    $RepoRoot,
    '-IntervalMinutes',
    [string]$IntervalMinutes,
    '-MaxRequests',
    [string]$MaxRequests,
    '-WorkerLoop'
)

$process = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WindowStyle Hidden -PassThru
$startedAtParent = Get-UtcIso
Write-JsonFile -Path $lockPath -Payload ([ordered]@{
    schemaVersion = '1.0.0'
    pid = $process.Id
    startedAtUtc = $startedAtParent
    intervalMinutes = $IntervalMinutes
    maxRequestsPerCycle = $MaxRequests
})
Write-JsonFile -Path $statusPath -Payload (New-StatusObject -Running $true -PidValue $process.Id -StartedAtUtc $startedAtParent -LastCycleStartedAtUtc $null -LastCycleFinishedAtUtc $null -NextCycleAtUtc $startedAtParent -Interval $IntervalMinutes -LastStatus 'starting' -LastCommit $null -LastError $null)

Write-Host "Catalogue worker started in background. PID: $($process.Id)"
Write-Host "Interval: $IntervalMinutes minutes"
Write-Host "Max requests per cycle: $MaxRequests"
Write-Host "Logs: $logPath"
