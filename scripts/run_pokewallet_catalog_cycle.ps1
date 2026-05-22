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
$cycleReportPath = Join-Path $RepoRoot 'reports\latest_pokewallet_worker_cycle.json'
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

function Get-ProviderSummary {
    $providerStatusPath = Join-Path $RepoRoot 'public\v1\provider-catalog\pokewallet\status.json'
    $providerManifestPath = Join-Path $RepoRoot 'public\v1\provider-catalog\pokewallet\cards-manifest.json'
    $status = Read-JsonFile -Path $providerStatusPath
    $manifest = Read-JsonFile -Path $providerManifestPath

    $cardsByLanguage = [ordered]@{}
    $setFilesByLanguage = [ordered]@{}
    if ($null -ne $status -and $null -ne $status.languages) {
        $langProps = $status.languages.PSObject.Properties | Sort-Object Name
        foreach ($prop in $langProps) {
            $language = [string]$prop.Name
            $payload = $prop.Value
            $cardsByLanguage[$language] = if ($null -ne $payload -and $null -ne $payload.cardCount) { [int]$payload.cardCount } else { 0 }
            $setFilesByLanguage[$language] = if ($null -ne $payload -and $null -ne $payload.setFileCount) { [int]$payload.setFileCount } else { 0 }
        }
    }

    $totalCards = 0
    foreach ($value in $cardsByLanguage.Values) {
        $totalCards += [int]$value
    }
    $totalSetFiles = 0
    foreach ($value in $setFilesByLanguage.Values) {
        $totalSetFiles += [int]$value
    }
    if ($null -ne $manifest) {
        if ($null -ne $manifest.totalCards) {
            $totalCards = [int]$manifest.totalCards
        }
        if ($null -ne $manifest.totalSetFiles) {
            $totalSetFiles = [int]$manifest.totalSetFiles
        }
    }

    return [ordered]@{
        cardsByLanguage = $cardsByLanguage
        setFilesByLanguage = $setFilesByLanguage
        totalCards = $totalCards
        totalSetFiles = $totalSetFiles
    }
}

function Get-ImageManifestRecordCount {
    $manifestPath = Join-Path $RepoRoot 'public\v1\images\cards-manifest.json'
    $manifest = Read-JsonFile -Path $manifestPath
    if ($null -eq $manifest) {
        return 0
    }
    if ($null -ne $manifest.recordCount) {
        return [int]$manifest.recordCount
    }
    if ($manifest.records -is [System.Collections.IEnumerable]) {
        return @($manifest.records).Count
    }
    return 0
}

function Get-StagedPathList {
    $lines = @(git -C $RepoRoot diff --cached --name-only --)
    return @($lines | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
}

function Get-StagedNumstat {
    $insertions = 0
    $deletions = 0
    $lines = @(git -C $RepoRoot diff --cached --numstat --)
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        $parts = [regex]::Split($line.Trim(), '\s+')
        if ($parts.Length -lt 2) {
            continue
        }
        if ($parts[0] -match '^\d+$') {
            $insertions += [int]$parts[0]
        }
        if ($parts[1] -match '^\d+$') {
            $deletions += [int]$parts[1]
        }
    }
    return [ordered]@{
        insertions = $insertions
        deletions = $deletions
    }
}

function Convert-LanguageTotalsToText {
    param($LanguageTotals)

    if ($null -eq $LanguageTotals) {
        return 'none'
    }
    $parts = @()
    foreach ($item in ($LanguageTotals.GetEnumerator() | Sort-Object Name)) {
        $parts += ('{0} {1}' -f [string]$item.Key, [int]$item.Value)
    }
    if ($parts.Count -eq 0) {
        return 'none'
    }
    return ($parts -join ', ')
}

function Build-CycleCommitMessage {
    param(
        [string]$TargetLanguage,
        $Diag,
        $ProviderAfter,
        [string]$ValidationResult
    )

    $validationTag = if ($ValidationResult -eq 'passed') { 'val ok' } else { 'val skipped' }

    $cardsWritten = [ordered]@{}
    if ($null -ne $Diag -and $null -ne $Diag.cardsWrittenByLanguage) {
        foreach ($prop in ($Diag.cardsWrittenByLanguage.PSObject.Properties | Sort-Object Name)) {
            $cardsWritten[[string]$prop.Name] = [int]$prop.Value
        }
    }

    $deltaParts = @()
    $totalDelta = 0
    foreach ($prop in ($cardsWritten.GetEnumerator() | Sort-Object Name)) {
        $value = [int]$prop.Value
        $totalDelta += $value
        if ($value -gt 0) {
            $deltaParts += ('{0} +{1}' -f [string]$prop.Name, $value)
        }
    }

    $totalsText = 'none'
    if ($null -ne $ProviderAfter -and $null -ne $ProviderAfter.cardsByLanguage) {
        $totalsText = Convert-LanguageTotalsToText -LanguageTotals $ProviderAfter.cardsByLanguage
    }

    if ($totalDelta -gt 0) {
        $deltaText = if ($deltaParts.Count -gt 0) { $deltaParts -join ', ' } else { ('+{0} cards' -f $totalDelta) }
        return ('Update PokéWallet {0}: {1}; totals {2}; {3}' -f $TargetLanguage, $deltaText, $totalsText, $validationTag)
    }

    return ('Refresh PokéWallet summaries ({0}): no card-count changes; totals {1}; {2}' -f $TargetLanguage, $totalsText, $validationTag)
}

function Write-CycleReport {
    param(
        [string]$StartedAtUtc,
        [string]$FinishedAtUtc,
        [string]$Status,
        [string]$Commit,
        [string]$TargetLanguage,
        [int]$RequestsUsed,
        [string[]]$FilesChanged,
        $Numstat,
        $ProviderBefore,
        $ProviderAfter,
        [int]$ImageManifestRecordCount,
        [string]$ValidationResult,
        [string]$PushResult,
        [string]$Message
    )

    $payload = [ordered]@{
        schemaVersion = '1.0.0'
        startedAtUtc = $StartedAtUtc
        finishedAtUtc = $FinishedAtUtc
        status = $Status
        message = $Message
        commit = $Commit
        targetLanguage = $TargetLanguage
        requestsUsed = $RequestsUsed
        filesChangedCount = @($FilesChanged).Count
        filesChanged = @($FilesChanged)
        insertions = [int]$Numstat.insertions
        deletions = [int]$Numstat.deletions
        providerBefore = $ProviderBefore
        providerAfter = $ProviderAfter
        imageManifestRecordCount = $ImageManifestRecordCount
        validationResult = $ValidationResult
        pushResult = $PushResult
        generatedAtUtc = Get-UtcIso
    }
    Write-JsonFile -Path $cycleReportPath -Payload $payload
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
        'data\scheduled_price_refresh_state.json.tmp',
        'reports\latest_pokewallet_worker_cycle.json',
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
    try {
        if ($null -ne $script:cycleReportContext) {
            $script:cycleReportContext.status = $Status
            $script:cycleReportContext.message = $Message
            $script:cycleReportContext.finishedAtUtc = $finishedAt
            if (-not [string]::IsNullOrWhiteSpace($Commit)) {
                $script:cycleReportContext.commit = $Commit
            }
            Write-CycleReport -StartedAtUtc $script:cycleReportContext.startedAtUtc -FinishedAtUtc $script:cycleReportContext.finishedAtUtc -Status $script:cycleReportContext.status -Commit $script:cycleReportContext.commit -TargetLanguage $script:cycleReportContext.targetLanguage -RequestsUsed $script:cycleReportContext.requestsUsed -FilesChanged $script:cycleReportContext.filesChanged -Numstat $script:cycleReportContext.numstat -ProviderBefore $script:cycleReportContext.providerBefore -ProviderAfter $script:cycleReportContext.providerAfter -ImageManifestRecordCount $script:cycleReportContext.imageManifestRecordCount -ValidationResult $script:cycleReportContext.validationResult -PushResult $script:cycleReportContext.pushResult -Message $script:cycleReportContext.message
        }
    }
    catch {
        Write-CycleLog "Failed to write cycle report: $($_.Exception.Message)"
    }
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

    $script:cycleReportContext = [ordered]@{
        startedAtUtc = $null
        finishedAtUtc = $null
        status = 'starting'
        message = ''
        commit = ''
        targetLanguage = $targetLanguage
        requestsUsed = 0
        filesChanged = @()
        numstat = [ordered]@{ insertions = 0; deletions = 0 }
        providerBefore = $null
        providerAfter = $null
        imageManifestRecordCount = 0
        validationResult = 'skipped'
        pushResult = 'not_attempted'
    }

    Acquire-CycleLock
    $cycleStarted = Get-UtcIso
    $script:cycleReportContext.startedAtUtc = $cycleStarted
    Update-WorkerStatus -LastStatus 'running_cycle' -LastCycleStartedAtUtc $cycleStarted -LastCycleFinishedAtUtc $null -LastCommit $null -LastError $null -IntervalMinutes $intervalMinutes -Mode 'once' -CurrentPriorityLanguage $targetLanguage
    $script:cycleReportContext.providerBefore = Get-ProviderSummary

    # --- Pre-cycle sync: fast-forward if local is behind remote and nothing unpushed ---
    Write-CycleLog 'Fetching origin to detect remote changes before provider calls.'
    git -C $RepoRoot fetch origin 2>&1 | ForEach-Object { Write-OutputLog $_ }
    $localHead  = (git -C $RepoRoot rev-parse HEAD).Trim()
    $remoteHead = (git -C $RepoRoot rev-parse 'origin/main' 2>$null).Trim()
    if ($LASTEXITCODE -eq 0 -and $localHead -ne $remoteHead) {
        $mergeBase = (git -C $RepoRoot merge-base HEAD 'origin/main' 2>$null).Trim()
        if ($mergeBase -eq $localHead) {
            # Local is strictly behind; no unpushed commits – safe to fast-forward.
            Write-CycleLog 'Local branch is behind origin/main with no local-only commits; fast-forwarding before cycle.'
            git -C $RepoRoot merge --ff-only origin/main 2>&1 | ForEach-Object { Write-OutputLog $_ }
            if ($LASTEXITCODE -ne 0) {
                Stop-WithStatus -Status 'sync_failed' -Message 'Pre-cycle fast-forward failed unexpectedly.' -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
            }
        }
        else {
            Write-CycleLog 'Local branch has unpushed commits and remote has moved; proceeding – push will use rebase-on-failure retry.'
        }
    }
    # --- End pre-cycle sync ---

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
            $script:cycleReportContext.validationResult = 'failed'
            Stop-WithStatus -Status 'validation_failed' -Message "Validation failed with exit code $($validateResult.ExitCode)." -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
        }
        $script:cycleReportContext.validationResult = 'passed'
    }

    $script:cycleReportContext.providerAfter = Get-ProviderSummary
    $script:cycleReportContext.imageManifestRecordCount = Get-ImageManifestRecordCount
    $script:cycleReportContext.requestsUsed = if ($null -ne $diag -and $null -ne $diag.requestsAttempted) { [int]$diag.requestsAttempted } else { 0 }

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

    $script:cycleReportContext.filesChanged = Get-StagedPathList
    $script:cycleReportContext.numstat = Get-StagedNumstat
    $commitHash = ''
    if ([bool]$workerConfig.commitAfterCycle) {
        $commitMessage = Build-CycleCommitMessage -TargetLanguage $targetLanguage -Diag $diag -ProviderAfter $script:cycleReportContext.providerAfter -ValidationResult $script:cycleReportContext.validationResult
        if ([string]::IsNullOrWhiteSpace($commitMessage)) {
            $commitMessage = 'Update PokéWallet provider catalogue data'
        }
        $commitResult = Invoke-RepoCommand -FilePath 'git' -Arguments @('-C', $RepoRoot, 'commit', '-m', $commitMessage)
        if ($commitResult.ExitCode -ne 0) {
            Stop-WithStatus -Status 'git_commit_failed' -Message "Git commit failed with exit code $($commitResult.ExitCode)." -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
        }
        $commitHash = (git -C $RepoRoot rev-parse --short HEAD).Trim()
        $script:cycleReportContext.commit = $commitHash

        if ([bool]$workerConfig.pushAfterCycle) {
            $pushResult = Invoke-RepoCommand -FilePath 'git' -Arguments @('-C', $RepoRoot, 'push')
            if ($pushResult.ExitCode -ne 0) {
                $script:cycleReportContext.pushResult = 'retrying_after_failure'
                # Push rejected – remote may have moved since we committed.
                # Attempt a safe fetch+rebase and retry once before giving up.
                Write-CycleLog 'Push failed; attempting fetch + rebase onto origin/main and retrying push once.'
                git -C $RepoRoot fetch origin 2>&1 | ForEach-Object { Write-OutputLog $_ }
                git -C $RepoRoot rebase origin/main 2>&1 | ForEach-Object { Write-OutputLog $_ }
                if ($LASTEXITCODE -ne 0) {
                    Write-CycleLog 'Rebase after failed push has conflicts. Aborting rebase – manual resolution required.'
                    git -C $RepoRoot rebase --abort 2>&1 | Out-Null
                    Stop-WithStatus -Status 'git_push_failed' -Message 'Push failed and rebase produced conflicts; manual resolution required. Do NOT force-push.' -Commit $commitHash -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
                }
                $commitHash = (git -C $RepoRoot rev-parse --short HEAD).Trim()
                $pushRetry = Invoke-RepoCommand -FilePath 'git' -Arguments @('-C', $RepoRoot, 'push')
                if ($pushRetry.ExitCode -ne 0) {
                    Stop-WithStatus -Status 'git_push_failed' -Message "Git push failed after rebase retry (exit code $($pushRetry.ExitCode)). Do NOT force-push; check remote state." -Commit $commitHash -StartedAtUtc $cycleStarted -IntervalMinutes $intervalMinutes
                }
                Write-CycleLog 'Push succeeded after rebase retry.'
                $script:cycleReportContext.pushResult = 'succeeded_after_retry'
            }
            else {
                $script:cycleReportContext.pushResult = 'succeeded'
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
