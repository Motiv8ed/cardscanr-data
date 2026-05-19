param(
    [string]$RepoRoot = "",
    [int]$MaxRequests = 0,
    [string]$Language = "",
    [switch]$AllLanguages
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
Set-Location $RepoRoot

$configPath = Join-Path $RepoRoot 'data\pokewallet_catalog_config.json'
$diagPath = Join-Path $RepoRoot 'public\v1\diagnostics\pokewallet-catalog-foundation-latest.json'
$statusPath = Join-Path $RepoRoot 'data\pokewallet_catalog_worker_status.json'
$cycleLockPath = Join-Path $RepoRoot '.pokewallet_catalog_cycle.lock'
$pythonPath = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $pythonPath)) {
    $pythonPath = 'python'
}

$script:cycleLockAcquired = $false
$script:logPath = Join-Path $RepoRoot 'logs\pokewallet_catalog_worker.log'

function Convert-ToProcessArgument {
    param([string]$Value)

    if ($null -eq $Value) {
        return '""'
    }
    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

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

function Write-CycleLog {
    param([string]$Message)

    $line = "$(Get-UtcIso) $Message"
    Write-Host $line
    $parent = Split-Path -Parent $script:logPath
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    Add-Content -Path $script:logPath -Value $line -Encoding UTF8
}

function Write-OutputLog {
    param($Line)

    Write-Host $Line
    Add-Content -Path $script:logPath -Value ([string]$Line) -Encoding UTF8
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

function Get-GitStatusLines {
    return @(git -C $RepoRoot status --porcelain)
}

function Get-GitStatusPath {
    param([string]$StatusLine)

    if ($StatusLine.Length -lt 4) {
        return ''
    }

    $path = $StatusLine.Substring(3).Trim()
    if ($path.Contains(' -> ')) {
        $parts = $path.Split(@(' -> '), [System.StringSplitOptions]::None)
        $path = $parts[$parts.Length - 1]
    }
    return ($path -replace '/', '\')
}

function Test-AllowedDirtyPath {
    param([string]$Path)

    $normalized = $Path.Trim()
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return $true
    }

    $exact = @(
        'data\pokewallet_catalog_full_state.json',
        'public\v1\diagnostics\pokewallet-catalog-foundation-latest.json',
        'public\v1\index.json',
        'public\v1\api-manifest.json',
        'public\v1\api-notes.json',
        'public\v1\schemas.json'
    )
    if ($exact -contains $normalized) {
        return $true
    }
    return $normalized.StartsWith('public\v1\provider-catalog\pokewallet\')
}

function Update-WorkerStatus {
    param(
        [string]$LastStatus,
        [string]$LastCycleStartedAtUtc,
        [string]$LastCycleFinishedAtUtc,
        [string]$LastCommit,
        [string]$LastError,
        [int]$IntervalMinutes,
        [string]$Mode = 'once',
        [string]$CurrentPriorityLanguage = 'all'
    )

    $existing = Read-JsonFile -Path $statusPath
    $startedAt = if ($null -ne $existing -and -not [string]::IsNullOrWhiteSpace([string]$existing.startedAtUtc)) {
        [string]$existing.startedAtUtc
    }
    else {
        Get-UtcIso
    }
    $payload = [ordered]@{
        schemaVersion = '1.0.0'
        running = $false
        pid = $PID
        startedAtUtc = $startedAt
        lastCycleStartedAtUtc = $LastCycleStartedAtUtc
        lastCycleFinishedAtUtc = $LastCycleFinishedAtUtc
        nextCycleAtUtc = $null
        intervalMinutes = $IntervalMinutes
        mode = $Mode
        currentPriorityLanguage = $CurrentPriorityLanguage
        nextLanguageToProcess = $null
        lastStatus = $LastStatus
        lastCommit = $LastCommit
        lastError = $LastError
    }
    Write-JsonFile -Path $statusPath -Payload $payload
}

function Stop-WithStatus {
    param(
        [string]$Status,
        [string]$Message,
        [int]$ExitCode = 1,
        [string]$Commit = '',
        [int]$IntervalMinutes = 75,
        [string]$StartedAtUtc = ''
    )

    $finishedAt = Get-UtcIso
    Write-CycleLog $Message
    Update-WorkerStatus -LastStatus $Status -LastCycleStartedAtUtc $StartedAtUtc -LastCycleFinishedAtUtc $finishedAt -LastCommit $Commit -LastError $(if ($ExitCode -eq 0) { $null } else { $Message }) -IntervalMinutes $IntervalMinutes
    Write-Host "WORKER_CYCLE_STATUS=$Status"
    Write-Host "WORKER_CYCLE_COMMIT=$Commit"
    Write-Host "WORKER_CYCLE_MESSAGE=$Message"
    exit $ExitCode
}

function Invoke-RepoCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-CycleLog ("RUN {0} {1}" -f $FilePath, ($Arguments -join ' '))
    $stdoutPath = Join-Path $env:TEMP ("cardscanr-cycle-command-{0}.out" -f ([guid]::NewGuid().ToString('N')))
    $stderrPath = Join-Path $env:TEMP ("cardscanr-cycle-command-{0}.err" -f ([guid]::NewGuid().ToString('N')))
    $argumentList = (($Arguments | ForEach-Object { Convert-ToProcessArgument -Value ([string]$_) }) -join ' ')
    try {
        $process = Start-Process -FilePath $FilePath -ArgumentList $argumentList -WorkingDirectory $RepoRoot -NoNewWindow -PassThru -Wait -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $output = @()
        if (Test-Path $stdoutPath) {
            $output += @(Get-Content -Path $stdoutPath -Encoding UTF8)
        }
        if (Test-Path $stderrPath) {
            $output += @(Get-Content -Path $stderrPath -Encoding UTF8)
        }
        foreach ($line in $output) {
            Write-OutputLog $line
        }
        $exitCode = [int]$process.ExitCode
    }
    finally {
        Remove-Item -Path $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
    }
}

function Acquire-CycleLock {
    if (Test-Path $cycleLockPath) {
        $lockData = Read-JsonFile -Path $cycleLockPath
        $existingPid = 0
        if ($null -ne $lockData -and $null -ne $lockData.pid) {
            $existingPid = [int]$lockData.pid
        }
        if ($existingPid -gt 0 -and (Test-ProcessAlive -ProcessId $existingPid)) {
            Write-CycleLog "Cycle already running as PID $existingPid."
            Write-Host 'WORKER_CYCLE_STATUS=cycle_already_running'
            Write-Host 'WORKER_CYCLE_COMMIT='
            Write-Host 'WORKER_CYCLE_MESSAGE=cycle already running'
            exit 0
        }
        Write-CycleLog 'Removing stale cycle lock file.'
        Remove-Item -Path $cycleLockPath -Force -ErrorAction SilentlyContinue
    }

    Write-JsonFile -Path $cycleLockPath -Payload ([ordered]@{
        schemaVersion = '1.0.0'
        pid = $PID
        startedAtUtc = Get-UtcIso
    })
    $script:cycleLockAcquired = $true
}

try {
    $config = Read-JsonFile -Path $configPath
    if ($null -eq $config) {
        Stop-WithStatus -Status 'error' -Message 'Could not read Pokewallet catalogue config.'
    }

    $workerConfig = $config.fullCatalogueWorker
    if ($null -eq $workerConfig) {
        Stop-WithStatus -Status 'error' -Message 'fullCatalogueWorker config is missing.'
    }

    if (-not [string]::IsNullOrWhiteSpace([string]$workerConfig.logPath)) {
        $script:logPath = Join-Path $RepoRoot ([string]$workerConfig.logPath)
    }

    if ($MaxRequests -le 0) {
        $MaxRequests = [int]($workerConfig.maxRequestsPerCycle)
    }
    if ($MaxRequests -le 0) {
        $MaxRequests = 80
    }

    $intervalMinutes = [int]($workerConfig.intervalMinutes)
    if ($intervalMinutes -le 0) {
        $intervalMinutes = 75
    }

    if (-not [string]::IsNullOrWhiteSpace($Language)) {
        $Language = $Language.Trim().ToLowerInvariant()
        if ($Language -eq 'all') {
            $AllLanguages = $true
            $Language = ''
        }
    }

    if (-not $AllLanguages -and [string]::IsNullOrWhiteSpace($Language)) {
        $AllLanguages = $true
    }

    if (-not $AllLanguages) {
        $allowedLanguages = @('en', 'jp', 'zh', 'kr', 'zh-cn', 'zh-tw')
        if ($allowedLanguages -notcontains $Language) {
            Stop-WithStatus -Status 'error' -Message "Unsupported language '$Language'. Allowed values: en, jp, zh, kr, zh-cn, zh-tw, or all." -StartedAtUtc (Get-UtcIso) -IntervalMinutes $intervalMinutes
        }
    }

    $targetLanguage = if ($AllLanguages) { 'all' } else { $Language }

    Acquire-CycleLock
    $cycleStarted = Get-UtcIso
    Update-WorkerStatus -LastStatus 'running_cycle' -LastCycleStartedAtUtc $cycleStarted -LastCycleFinishedAtUtc $null -LastCommit $null -LastError $null -IntervalMinutes $intervalMinutes -Mode 'once' -CurrentPriorityLanguage $targetLanguage

    $statusLines = Get-GitStatusLines
    $unrelated = @()
    foreach ($line in $statusLines) {
        $path = Get-GitStatusPath -StatusLine $line
        if (-not (Test-AllowedDirtyPath -Path $path)) {
            $unrelated += $line
        }
    }
    if ($unrelated.Count -gt 0) {
        Write-CycleLog 'Unrelated uncommitted changes are present. This cycle will not run.'
        foreach ($line in $unrelated) {
            Write-OutputLog $line
        }
        Stop-WithStatus -Status 'dirty_worktree' -Message 'Stopped before provider calls because unrelated files are dirty.' -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
    }

    $exportArgs = @(
        'tools\build_pokewallet_catalog_foundation.py',
        '--full-catalogue',
        '--max-requests',
        [string]$MaxRequests,
        '--resume'
    )
    if ($AllLanguages) {
        $exportArgs += '--all-languages'
    }
    else {
        $exportArgs += @('--language', $Language)
    }
    $exportResult = Invoke-RepoCommand -FilePath $pythonPath -Arguments $exportArgs
    if ($exportResult.ExitCode -ne 0) {
        Stop-WithStatus -Status 'export_failed' -Message "Catalogue exporter failed with exit code $($exportResult.ExitCode)." -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
    }

    $diag = Read-JsonFile -Path $diagPath
    if ($null -eq $diag) {
        Stop-WithStatus -Status 'error' -Message 'Catalogue diagnostics were not written.' -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
    }

    if ([string]$diag.status -eq 'rate_limited') {
        Stop-WithStatus -Status 'rate_limited' -Message 'Provider returned rate limit status.' -ExitCode 0 -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
    }

    if ([string]$diag.status -eq 'complete') {
        Write-CycleLog 'Exporter reported complete status for selected scope.'
    }

    if ([bool]$workerConfig.validateAfterCycle) {
        $validateResult = Invoke-RepoCommand -FilePath $pythonPath -Arguments @('tools\validate_cache.py')
        if ($validateResult.ExitCode -ne 0) {
            Stop-WithStatus -Status 'validation_failed' -Message "Validation failed with exit code $($validateResult.ExitCode)." -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
        }
    }

    $changedLines = Get-GitStatusLines
    if ($changedLines.Count -eq 0) {
        Stop-WithStatus -Status 'no_changes' -Message 'no changes' -ExitCode 0 -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
    }

    $stagePaths = @(
        'data\pokewallet_catalog_full_state.json',
        'public\v1\provider-catalog\pokewallet',
        'public\v1\diagnostics\pokewallet-catalog-foundation-latest.json',
        'public\v1\index.json',
        'public\v1\api-manifest.json',
        'public\v1\api-notes.json',
        'public\v1\schemas.json'
    )

    foreach ($path in $stagePaths) {
        if (Test-Path (Join-Path $RepoRoot $path)) {
            git -C $RepoRoot add -- $path
            if ($LASTEXITCODE -ne 0) {
                Stop-WithStatus -Status 'git_stage_failed' -Message "Failed to stage $path." -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
            }
        }
    }

    git -C $RepoRoot diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Stop-WithStatus -Status 'no_changes' -Message 'no expected catalogue changes' -ExitCode 0 -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
    }

    $commitHash = ''
    if ([bool]$workerConfig.commitAfterCycle) {
        $commitMessage = [string]$workerConfig.commitMessage
        if ([string]::IsNullOrWhiteSpace($commitMessage) -or $commitMessage.Contains('PokÃ©Wallet')) {
            $commitMessage = 'Expand PokéWallet provider catalogue export'
        }
        $commitResult = Invoke-RepoCommand -FilePath 'git' -Arguments @('-C', $RepoRoot, 'commit', '-m', $commitMessage)
        if ($commitResult.ExitCode -ne 0) {
            Stop-WithStatus -Status 'git_commit_failed' -Message "Git commit failed with exit code $($commitResult.ExitCode)." -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
        }
        $commitHash = (git -C $RepoRoot rev-parse --short HEAD).Trim()

        if ([bool]$workerConfig.pushAfterCycle) {
            $pushResult = Invoke-RepoCommand -FilePath 'git' -Arguments @('-C', $RepoRoot, 'push')
            if ($pushResult.ExitCode -ne 0) {
                Stop-WithStatus -Status 'git_push_failed' -Message "Git push failed with exit code $($pushResult.ExitCode)." -Commit $commitHash -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
            }
        }
    }
    else {
        Write-CycleLog 'commitAfterCycle is false; changes are staged but not committed.'
    }

    $finalStatus = switch ([string]$diag.status) {
        'complete' { 'complete' }
        'partial' { 'partial' }
        default { 'ok' }
    }
    Stop-WithStatus -Status $finalStatus -Message 'cycle completed' -ExitCode 0 -Commit $commitHash -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
}
finally {
    if ($script:cycleLockAcquired) {
        Remove-Item -Path $cycleLockPath -Force -ErrorAction SilentlyContinue
    }
}
