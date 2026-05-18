param(
    [string]$RepoRoot = "",
    [int]$IntervalMinutes = 75,
    [int]$MaxRequests = 0
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
Set-Location $RepoRoot

$configPath = Join-Path $RepoRoot 'data\pokewallet_catalog_config.json'
$statusPath = Join-Path $RepoRoot 'data\pokewallet_catalog_worker_status.json'
$workerLockPath = Join-Path $RepoRoot '.pokewallet_catalog_worker.lock'
$cycleScript = Join-Path $RepoRoot 'scripts\run_pokewallet_catalog_cycle.ps1'
$script:logPath = Join-Path $RepoRoot 'logs\pokewallet_catalog_worker.log'
$script:lockAcquired = $false
$script:startedAtUtc = $null

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

function Write-WorkerLog {
    param([string]$Message)

    $parent = Split-Path -Parent $script:logPath
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    $line = "$(Get-UtcIso) $Message"
    Write-Host $line
    Add-Content -Path $script:logPath -Value $line -Encoding UTF8
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
        return $null
    }
    return $config.fullCatalogueWorker
}

function Write-Status {
    param(
        [bool]$Running,
        [string]$LastStatus,
        [string]$LastCycleStartedAtUtc,
        [string]$LastCycleFinishedAtUtc,
        [string]$NextCycleAtUtc,
        [string]$LastCommit,
        [string]$LastError
    )

    $payload = [ordered]@{
        schemaVersion = '1.0.0'
        running = $Running
        pid = $PID
        startedAtUtc = $script:startedAtUtc
        lastCycleStartedAtUtc = $LastCycleStartedAtUtc
        lastCycleFinishedAtUtc = $LastCycleFinishedAtUtc
        nextCycleAtUtc = $NextCycleAtUtc
        intervalMinutes = $IntervalMinutes
        lastStatus = $LastStatus
        lastCommit = $LastCommit
        lastError = $LastError
    }
    Write-JsonFile -Path $statusPath -Payload $payload
}

function Acquire-WorkerLock {
    if (Test-Path $workerLockPath) {
        $lockData = Read-JsonFile -Path $workerLockPath
        $existingPid = 0
        if ($null -ne $lockData -and $null -ne $lockData.pid) {
            $existingPid = [int]$lockData.pid
        }
        if ($existingPid -gt 0 -and (Test-ProcessAlive -ProcessId $existingPid)) {
            Write-Host "Manual worker loop is already running as PID $existingPid."
            exit 0
        }
        Write-WorkerLog "Removing stale worker lock for PID $existingPid."
        Remove-Item -Path $workerLockPath -Force -ErrorAction SilentlyContinue
    }

    $script:startedAtUtc = Get-UtcIso
    Write-JsonFile -Path $workerLockPath -Payload ([ordered]@{
        schemaVersion = '1.0.0'
        pid = $PID
        startedAtUtc = $script:startedAtUtc
        intervalMinutes = $IntervalMinutes
        maxRequestsPerCycle = $MaxRequests
    })
    $script:lockAcquired = $true
}

function Invoke-Cycle {
    $args = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $cycleScript,
        '-RepoRoot',
        $RepoRoot
    )
    if ($MaxRequests -gt 0) {
        $args += @('-MaxRequests', [string]$MaxRequests)
    }

    Write-WorkerLog 'Starting catalogue cycle.'
    $output = @(& powershell.exe @args 2>&1)
    foreach ($line in $output) {
        Write-Host $line
        Add-Content -Path $script:logPath -Value ([string]$line) -Encoding UTF8
    }

    $status = 'unknown'
    $commit = $null
    $message = $null
    foreach ($line in $output) {
        $text = [string]$line
        if ($text.StartsWith('WORKER_CYCLE_STATUS=')) {
            $status = $text.Substring('WORKER_CYCLE_STATUS='.Length)
        }
        elseif ($text.StartsWith('WORKER_CYCLE_COMMIT=')) {
            $value = $text.Substring('WORKER_CYCLE_COMMIT='.Length)
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                $commit = $value
            }
        }
        elseif ($text.StartsWith('WORKER_CYCLE_MESSAGE=')) {
            $message = $text.Substring('WORKER_CYCLE_MESSAGE='.Length)
        }
    }

    return [pscustomobject]@{
        ExitCode = [int]$LASTEXITCODE
        Status = $status
        Commit = $commit
        Message = $message
    }
}

function Wait-UntilNextCycle {
    param([datetime]$NextCycle)

    while ([datetime]::UtcNow -lt $NextCycle) {
        $remaining = [int][Math]::Ceiling(($NextCycle - [datetime]::UtcNow).TotalSeconds)
        if ($remaining -lt 0) {
            return
        }
        $sleepSeconds = [Math]::Min(60, [Math]::Max(1, $remaining))
        Write-Host ("Next cycle in {0} minute(s). Press Ctrl+C to stop." -f ([Math]::Ceiling($remaining / 60)))
        Start-Sleep -Seconds $sleepSeconds
    }
}

try {
    $config = Get-WorkerConfig
    if ($null -ne $config) {
        if ($IntervalMinutes -le 0 -and $null -ne $config.intervalMinutes) {
            $IntervalMinutes = [int]$config.intervalMinutes
        }
        if ($MaxRequests -le 0 -and $null -ne $config.maxRequestsPerCycle) {
            $MaxRequests = [int]$config.maxRequestsPerCycle
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$config.logPath)) {
            $script:logPath = Join-Path $RepoRoot ([string]$config.logPath)
        }
    }
    if ($IntervalMinutes -le 0) {
        $IntervalMinutes = 75
    }
    if ($MaxRequests -le 0) {
        $MaxRequests = 80
    }

    Acquire-WorkerLock
    Write-WorkerLog "Manual worker loop started. PID=$PID intervalMinutes=$IntervalMinutes maxRequests=$MaxRequests"
    Write-Status -Running $true -LastStatus 'starting' -LastCycleStartedAtUtc $null -LastCycleFinishedAtUtc $null -NextCycleAtUtc (Get-UtcIso) -LastCommit $null -LastError $null

    while ($true) {
        $cycleStartedAt = Get-UtcIso
        Write-Status -Running $true -LastStatus 'running_cycle' -LastCycleStartedAtUtc $cycleStartedAt -LastCycleFinishedAtUtc $null -NextCycleAtUtc $null -LastCommit $null -LastError $null

        $cycle = Invoke-Cycle
        $cycleFinishedAt = Get-UtcIso
        $lastError = $null
        if ($cycle.Status -notin @('ok', 'no_changes')) {
            $lastError = $cycle.Message
        }

        if ($cycle.Status -eq 'rate_limited') {
            Write-WorkerLog 'Stopping manual worker because the provider returned rate limit status.'
            Write-Status -Running $false -LastStatus 'rate_limited' -LastCycleStartedAtUtc $cycleStartedAt -LastCycleFinishedAtUtc $cycleFinishedAt -NextCycleAtUtc $null -LastCommit $cycle.Commit -LastError $cycle.Message
            break
        }

        if ($cycle.ExitCode -ne 0 -or $cycle.Status -in @('dirty_worktree', 'validation_failed', 'export_failed', 'git_commit_failed', 'git_push_failed', 'error')) {
            Write-WorkerLog "Stopping manual worker after cycle status=$($cycle.Status)."
            Write-Status -Running $false -LastStatus $cycle.Status -LastCycleStartedAtUtc $cycleStartedAt -LastCycleFinishedAtUtc $cycleFinishedAt -NextCycleAtUtc $null -LastCommit $cycle.Commit -LastError $lastError
            break
        }

        $nextCycle = [datetime]::UtcNow.AddMinutes($IntervalMinutes)
        $nextCycleAtUtc = Get-UtcIso -Value $nextCycle
        Write-WorkerLog "Cycle finished with status=$($cycle.Status) nextCycleAtUtc=$nextCycleAtUtc"
        Write-Status -Running $true -LastStatus $cycle.Status -LastCycleStartedAtUtc $cycleStartedAt -LastCycleFinishedAtUtc $cycleFinishedAt -NextCycleAtUtc $nextCycleAtUtc -LastCommit $cycle.Commit -LastError $lastError
        Wait-UntilNextCycle -NextCycle $nextCycle
    }
}
finally {
    if ($script:lockAcquired) {
        Remove-Item -Path $workerLockPath -Force -ErrorAction SilentlyContinue
        $existing = Read-JsonFile -Path $statusPath
        Write-Status -Running $false -LastStatus $(if ($null -ne $existing) { [string]$existing.lastStatus } else { 'stopped' }) -LastCycleStartedAtUtc $(if ($null -ne $existing) { [string]$existing.lastCycleStartedAtUtc } else { $null }) -LastCycleFinishedAtUtc $(if ($null -ne $existing) { [string]$existing.lastCycleFinishedAtUtc } else { $null }) -NextCycleAtUtc $null -LastCommit $(if ($null -ne $existing) { [string]$existing.lastCommit } else { $null }) -LastError $(if ($null -ne $existing) { [string]$existing.lastError } else { $null })
        Write-WorkerLog 'Manual worker loop stopped.'
    }
}
