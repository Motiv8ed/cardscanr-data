param(
    [int]$BatchSize = 20,
    [int]$IntervalMinutes = 60,
    [string]$RepoRoot = ""
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$logsDir = Join-Path $RepoRoot 'logs'
$logPath = Join-Path $logsDir 'local_price_updater.log'
$statusPath = Join-Path $logsDir 'local_price_updater_status.json'
$resultPath = Join-Path $logsDir 'local_price_update_last_result.json'
$lockPath = Join-Path $RepoRoot '.local_updater.lock'
$pythonPath = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$updaterScript = Join-Path $RepoRoot 'tools\run_local_price_update.py'

if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
}

function Get-UtcIso {
    param([datetime]$Value = ([datetime]::UtcNow))

    return $Value.ToString('yyyy-MM-ddTHH:mm:ssZ')
}

function Get-LocalIso {
    param([datetime]$Value = (Get-Date))

    return $Value.ToString('yyyy-MM-ddTHH:mm:ssK')
}

function Write-JsonAtomic {
    param(
        [string]$Path,
        [object]$Payload
    )

    $tmpPath = "$Path.tmp"
    $json = $Payload | ConvertTo-Json -Depth 8
    Set-Content -Path $tmpPath -Value $json -Encoding UTF8
    Move-Item -Path $tmpPath -Destination $Path -Force
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

function Write-LoopLog {
    param(
        [string]$Level,
        [string]$Message
    )

    $timestamp = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
    $line = "$timestamp [$Level] $Message"
    Add-Content -Path $logPath -Value $line -Encoding UTF8
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

function Get-SecondsUntil {
    param([string]$UtcIso)

    if ([string]::IsNullOrWhiteSpace($UtcIso)) {
        return $null
    }

    try {
        $target = [datetime]::Parse($UtcIso).ToUniversalTime()
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

function Update-DerivedStatusFields {
    if (-not [string]::IsNullOrWhiteSpace($script:statusState.nextRunAtUtc)) {
        $script:statusState.nextRunInSeconds = Get-SecondsUntil -UtcIso $script:statusState.nextRunAtUtc
    }
    else {
        $script:statusState.nextRunInSeconds = $null
    }

    if (-not [string]::IsNullOrWhiteSpace($script:statusState.currentUpdateStartedAtUtc)) {
        try {
            $started = [datetime]::Parse($script:statusState.currentUpdateStartedAtUtc).ToUniversalTime()
            $elapsed = [int][Math]::Floor(([datetime]::UtcNow - $started).TotalSeconds)
            if ($elapsed -lt 0) {
                $elapsed = 0
            }
            $script:statusState.currentUpdateElapsedSeconds = $elapsed

            if ($script:statusState.lastCycleDurationSeconds) {
                $estimatedFinish = $started.AddSeconds([int]$script:statusState.lastCycleDurationSeconds)
                $script:statusState.estimatedCurrentUpdateFinishAtUtc = Get-UtcIso -Value $estimatedFinish
            }
            else {
                $script:statusState.estimatedCurrentUpdateFinishAtUtc = $null
            }
        }
        catch {
            $script:statusState.currentUpdateElapsedSeconds = $null
            $script:statusState.estimatedCurrentUpdateFinishAtUtc = $null
        }
    }
    else {
        $script:statusState.currentUpdateElapsedSeconds = $null
        $script:statusState.estimatedCurrentUpdateFinishAtUtc = $null
    }
}

function Write-Status {
    Update-DerivedStatusFields
    Write-JsonAtomic -Path $statusPath -Payload $script:statusState
}

function Set-StatusValues {
    param([hashtable]$Values)

    foreach ($key in $Values.Keys) {
        $script:statusState[$key] = $Values[$key]
    }
    Write-Status
}

function Set-Phase {
    param(
        [string]$Phase,
        [string]$ErrorMessage = $null,
        [int]$ExitCode = 0
    )

    $updates = @{
        currentPhase = $Phase
        lastExitCode = $ExitCode
    }
    if ($null -ne $ErrorMessage) {
        $updates.lastError = $ErrorMessage
    }
    Set-StatusValues -Values $updates
}

function Read-LastResult {
    $result = Read-JsonFile -Path $resultPath
    if ($null -eq $result) {
        return $null
    }
    return $result
}

function Update-StatusFromResult {
    param([object]$Result)

    if ($null -eq $Result) {
        return
    }

    $lastBatchSetIds = @()
    if ($Result.updatedSetIds -and $Result.updatedSetIds.Count -gt 0) {
        $lastBatchSetIds = @($Result.updatedSetIds)
    }
    elseif ($Result.plannedSetIds -and $Result.plannedSetIds.Count -gt 0) {
        $lastBatchSetIds = @($Result.plannedSetIds)
    }

    $updates = @{
        lastCycleFinishedAtUtc = $Result.finishedAtUtc
        lastCycleDurationSeconds = $Result.durationSeconds
        lastExitCode = [int]$Result.exitCode
        lastError = $Result.error
        lastBatchSetIds = $lastBatchSetIds
        currentUpdateStartedAtUtc = $null
        currentUpdateElapsedSeconds = $null
        estimatedCurrentUpdateFinishAtUtc = $null
    }

    if ([int]$Result.exitCode -eq 0) {
        $updates.lastSuccessfulUpdateAtUtc = $Result.finishedAtUtc
    }
    if ($Result.commitHash) {
        $updates.lastCommitHash = $Result.commitHash
    }
    if ($Result.pushSucceeded) {
        $updates.lastSuccessfulPushAtUtc = $Result.finishedAtUtc
    }

    Set-StatusValues -Values $updates
}

function Get-LockData {
    if (-not (Test-Path $lockPath)) {
        return $null
    }

    try {
        $raw = Get-Content -Path $lockPath -Raw -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $null
        }
        return $raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Acquire-Lock {
    $existing = Get-LockData
    if ($null -ne $existing -and $existing.PSObject.Properties.Name -contains 'pid') {
        $existingPid = 0
        try {
            $existingPid = [int]$existing.pid
        }
        catch {
            $existingPid = 0
        }

        if ($existingPid -gt 0 -and (Test-ProcessAlive -ProcessId $existingPid)) {
            Write-Host "Updater already running (PID $existingPid)."
            return $false
        }

        Write-LoopLog -Level 'WARN' -Message "Removing stale lock file."
        Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    }
    elseif (Test-Path $lockPath) {
        Write-LoopLog -Level 'WARN' -Message "Removing unreadable lock file."
        Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    }

    try {
        $stream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $writer = New-Object System.IO.StreamWriter($stream)
        $lockData = @{
            pid = $PID
            startedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
            repoRoot = $RepoRoot
            batchSize = [Math]::Max(1, $BatchSize)
            intervalMinutes = [Math]::Max(1, $IntervalMinutes)
        } | ConvertTo-Json -Compress
        $writer.Write($lockData)
        $writer.Flush()
        $writer.Dispose()
        $stream.Dispose()
        return $true
    }
    catch {
        Write-Host "Failed to acquire lock file at $lockPath"
        return $false
    }
}

$BatchSize = [Math]::Max(1, $BatchSize)
$IntervalMinutes = [Math]::Max(1, $IntervalMinutes)
$intervalSeconds = $IntervalMinutes * 60

$previousStatus = Read-JsonFile -Path $statusPath
$previousResult = Read-LastResult
$initialLastBatchSetIds = @()
if ($previousStatus -and $previousStatus.lastBatchSetIds) {
    $initialLastBatchSetIds = @($previousStatus.lastBatchSetIds)
}
elseif ($previousResult -and $previousResult.updatedSetIds) {
    $initialLastBatchSetIds = @($previousResult.updatedSetIds)
}
elseif ($previousResult -and $previousResult.plannedSetIds) {
    $initialLastBatchSetIds = @($previousResult.plannedSetIds)
}

$script:statusState = [ordered]@{
    schemaVersion = '1.0.0'
    isRunning = $true
    pid = $PID
    repoRoot = $RepoRoot
    startedAtLocal = Get-LocalIso
    startedAtUtc = Get-UtcIso
    currentPhase = 'starting'
    cycleNumber = 0
    batchSize = $BatchSize
    intervalMinutes = $IntervalMinutes
    lastCycleStartedAtUtc = if ($previousStatus) { $previousStatus.lastCycleStartedAtUtc } else { $null }
    lastCycleFinishedAtUtc = if ($previousStatus) { $previousStatus.lastCycleFinishedAtUtc } else { $null }
    lastCycleDurationSeconds = if ($previousStatus) { $previousStatus.lastCycleDurationSeconds } else { $null }
    lastSuccessfulUpdateAtUtc = if ($previousStatus -and $previousStatus.lastSuccessfulUpdateAtUtc) { $previousStatus.lastSuccessfulUpdateAtUtc } elseif ($previousResult) { $previousResult.finishedAtUtc } else { $null }
    lastSuccessfulPushAtUtc = if ($previousStatus) { $previousStatus.lastSuccessfulPushAtUtc } else { $null }
    lastCommitHash = if ($previousStatus) { $previousStatus.lastCommitHash } elseif ($previousResult) { $previousResult.commitHash } else { $null }
    lastBatchSetIds = $initialLastBatchSetIds
    nextRunAtUtc = $null
    nextRunInSeconds = $null
    currentUpdateStartedAtUtc = $null
    currentUpdateElapsedSeconds = $null
    estimatedCurrentUpdateFinishAtUtc = $null
    lastError = if ($previousStatus) { $previousStatus.lastError } else { $null }
    lastExitCode = if ($previousStatus) { $previousStatus.lastExitCode } else { 0 }
}

if (-not (Acquire-Lock)) {
    exit 1
}

Write-Status
Write-LoopLog -Level 'INFO' -Message "Updater loop started. BatchSize=$BatchSize IntervalMinutes=$IntervalMinutes RepoRoot=$RepoRoot"

try {
    while ($true) {
        try {
            $script:statusState.cycleNumber = [int]$script:statusState.cycleNumber + 1
            Set-Location $RepoRoot
            Set-StatusValues -Values @{
                currentPhase = 'starting'
                lastCycleStartedAtUtc = Get-UtcIso
                nextRunAtUtc = $null
                nextRunInSeconds = $null
                currentUpdateStartedAtUtc = $null
                currentUpdateElapsedSeconds = $null
                estimatedCurrentUpdateFinishAtUtc = $null
                lastError = $null
                lastExitCode = 0
            }

            $preStatusOutput = git status --porcelain
            if ($LASTEXITCODE -ne 0) {
                Set-Phase -Phase 'error' -ErrorMessage 'git status failed; skipping cycle.' -ExitCode $LASTEXITCODE
                Write-LoopLog -Level 'ERROR' -Message "git status failed; skipping cycle."
            }
            elseif (-not [string]::IsNullOrWhiteSpace(($preStatusOutput | Out-String).Trim())) {
                Write-LoopLog -Level 'WARN' -Message "Uncommitted changes detected before cycle; skipping update."
                Set-StatusValues -Values @{
                    currentPhase = 'sleeping'
                    lastCycleFinishedAtUtc = Get-UtcIso
                    lastError = 'Skipped cycle because uncommitted changes were present before update.'
                    lastExitCode = 0
                }
            }
            else {
                Set-Phase -Phase 'pulling'
                $pullOutput = git pull --ff-only 2>&1
                foreach ($line in $pullOutput) {
                    if (-not [string]::IsNullOrWhiteSpace("$line")) {
                        Write-LoopLog -Level 'INFO' -Message "git pull: $line"
                    }
                }

                if ($LASTEXITCODE -ne 0) {
                    Set-Phase -Phase 'error' -ErrorMessage 'git pull --ff-only failed; skipping cycle.' -ExitCode $LASTEXITCODE
                    Write-LoopLog -Level 'ERROR' -Message "git pull --ff-only failed; skipping cycle."
                }
                elseif (-not (Test-Path $pythonPath)) {
                    Set-Phase -Phase 'error' -ErrorMessage "Python interpreter missing at $pythonPath; skipping cycle." -ExitCode 1
                    Write-LoopLog -Level 'ERROR' -Message "Python interpreter missing at $pythonPath; skipping cycle."
                }
                else {
                    Set-StatusValues -Values @{
                        currentPhase = 'updating'
                        currentUpdateStartedAtUtc = Get-UtcIso
                        lastError = $null
                        lastExitCode = 0
                    }
                    Write-LoopLog -Level 'INFO' -Message "Starting update cycle."
                    & $pythonPath $updaterScript --batch-size $BatchSize --commit --push 2>&1 | ForEach-Object {
                        $line = "$($_)"
                        if ([string]::IsNullOrWhiteSpace($line)) {
                            return
                        }

                        if ($line.StartsWith('CARDSCANR_PHASE ')) {
                            $phase = $line.Substring('CARDSCANR_PHASE '.Length).Trim().ToLowerInvariant()
                            if ($phase -in @('updating', 'validating', 'committing', 'pushing')) {
                                Set-StatusValues -Values @{
                                    currentPhase = $phase
                                    lastError = $null
                                    lastExitCode = 0
                                }
                            }
                            return
                        }

                        Write-LoopLog -Level 'INFO' -Message "updater: $line"
                        Write-Status
                    }

                    $updaterExitCode = $LASTEXITCODE
                    $lastResult = Read-LastResult
                    if ($lastResult) {
                        Update-StatusFromResult -Result $lastResult
                    }

                    if ($updaterExitCode -ne 0) {
                        if (-not $lastResult -or -not $lastResult.error) {
                            Set-Phase -Phase 'error' -ErrorMessage "Updater exited with code $updaterExitCode." -ExitCode $updaterExitCode
                        }
                        else {
                            Set-Phase -Phase 'error' -ErrorMessage $lastResult.error -ExitCode $updaterExitCode
                        }
                        Write-LoopLog -Level 'ERROR' -Message "Updater exited with code $updaterExitCode."
                    }
                    else {
                        Write-LoopLog -Level 'INFO' -Message "Update cycle completed successfully."
                        Set-StatusValues -Values @{
                            currentPhase = 'sleeping'
                            lastExitCode = 0
                            lastError = $null
                        }
                    }
                }
            }
        }
        catch {
            Set-Phase -Phase 'error' -ErrorMessage $_.Exception.Message -ExitCode 1
            Write-LoopLog -Level 'ERROR' -Message "Unhandled cycle error: $($_.Exception.Message)"
        }

        $nextRunAt = [datetime]::UtcNow.AddMinutes($IntervalMinutes)
        Set-StatusValues -Values @{
            currentPhase = 'sleeping'
            nextRunAtUtc = Get-UtcIso -Value $nextRunAt
            lastCycleFinishedAtUtc = if ($script:statusState.lastCycleFinishedAtUtc) { $script:statusState.lastCycleFinishedAtUtc } else { Get-UtcIso }
            currentUpdateStartedAtUtc = $null
        }
        Write-LoopLog -Level 'INFO' -Message "Sleeping for $IntervalMinutes minute(s)."
        Start-Sleep -Seconds $intervalSeconds
    }
}
finally {
    Set-StatusValues -Values @{
        isRunning = $false
        currentPhase = 'stopped'
        nextRunAtUtc = $null
        nextRunInSeconds = $null
        currentUpdateStartedAtUtc = $null
        currentUpdateElapsedSeconds = $null
        estimatedCurrentUpdateFinishAtUtc = $null
    }
    if (Test-Path $lockPath) {
        Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    }
    Write-LoopLog -Level 'INFO' -Message "Updater loop stopped."
}
