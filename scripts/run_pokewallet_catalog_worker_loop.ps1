param(
    [string]$RepoRoot = "",
    [int]$IntervalMinutes = 75,
    [int]$MaxRequests = 0,
    [switch]$UntilComplete
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
Set-Location $RepoRoot

$budgetUtilsPath = Join-Path $RepoRoot 'scripts\pokewallet_worker_budget_utils.ps1'
if (-not (Test-Path $budgetUtilsPath)) {
    throw "Budget utils script not found: $budgetUtilsPath"
}
. $budgetUtilsPath

$configPath = Join-Path $RepoRoot 'data\pokewallet_catalog_config.json'
$statusPath = Join-Path $RepoRoot 'data\pokewallet_catalog_worker_status.json'
$budgetLedgerPath = Join-Path $RepoRoot 'data\pokewallet_catalog_worker_rate_ledger.json'
$providerStatusPath = Join-Path $RepoRoot 'public\v1\provider-catalog\pokewallet\status.json'
$providerManifestPath = Join-Path $RepoRoot 'public\v1\provider-catalog\pokewallet\cards-manifest.json'
$statePath = Join-Path $RepoRoot 'data\pokewallet_catalog_full_state.json'
$workerLockPath = Join-Path $RepoRoot '.pokewallet_catalog_worker.lock'
$cycleScript = Join-Path $RepoRoot 'scripts\run_pokewallet_catalog_cycle.ps1'
$script:logPath = Join-Path $RepoRoot 'logs\pokewallet_catalog_worker.log'
$script:lockAcquired = $false
$script:startedAtUtc = $null
$script:mode = if ($UntilComplete) { 'untilComplete' } else { 'loop' }

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
        [string]$LastError,
        [string]$CurrentPriorityLanguage,
        [string]$NextLanguageToProcess,
        [string[]]$LanguagePriority,
        [object]$Progress,
        [object]$Budget
    )

    $cardsByLanguage = @{}
    $setFilesByLanguage = @{}
    $languagesCompleted = @{}
    $totalCards = 0
    $totalSetFiles = 0
    $binaryImagesStored = $false
    $imageStorageMode = 'provider_reference_only'
    $budgetSource = 'ledger'
    $hourlyUsedEstimate = 0
    $dailyUsedEstimate = 0
    $hourlyTarget = 0
    $dailyTarget = 0
    $hourlyRemaining = 0
    $dailyRemaining = 0
    $nextWaitReason = 'none'

    if ($null -ne $Progress) {
        $cardsByLanguage = $Progress.CardsByLanguage
        $setFilesByLanguage = $Progress.SetFilesByLanguage
        $languagesCompleted = $Progress.LanguagesCompleted
        $totalCards = $Progress.TotalCards
        $totalSetFiles = $Progress.TotalSetFiles
        $binaryImagesStored = $Progress.BinaryImagesStored
        $imageStorageMode = $Progress.ImageStorageMode
    }

    if ($null -ne $Budget) {
        $budgetSource = [string]$Budget.UsageSource
        $hourlyUsedEstimate = [int]$Budget.HourlyUsed
        $dailyUsedEstimate = [int]$Budget.DailyUsed
        $hourlyTarget = [int]$Budget.HourlyTarget
        $dailyTarget = [int]$Budget.DailyTarget
        $hourlyRemaining = [int]$Budget.HourlyRemaining
        $dailyRemaining = [int]$Budget.DailyRemaining
        if (-not [string]::IsNullOrWhiteSpace([string]$Budget.WaitReason)) {
            $nextWaitReason = [string]$Budget.WaitReason
        }
    }

    $payload = [ordered]@{
        schemaVersion = '1.1.0'
        running = $Running
        pid = $PID
        startedAtUtc = $script:startedAtUtc
        lastCycleStartedAtUtc = $LastCycleStartedAtUtc
        lastCycleFinishedAtUtc = $LastCycleFinishedAtUtc
        nextCycleAtUtc = $NextCycleAtUtc
        intervalMinutes = $IntervalMinutes
        mode = $script:mode
        currentPriorityLanguage = $CurrentPriorityLanguage
        nextLanguageToProcess = $NextLanguageToProcess
        languagePriority = $LanguagePriority
        lastStatus = $LastStatus
        lastCommit = $LastCommit
        lastError = $LastError
        cardsByLanguage = $cardsByLanguage
        setFilesByLanguage = $setFilesByLanguage
        languagesCompleted = $languagesCompleted
        totalCards = $totalCards
        totalSetFiles = $totalSetFiles
        binaryImagesStored = $binaryImagesStored
        imageStorageMode = $imageStorageMode
        budgetSource = $budgetSource
        hourlyUsedEstimate = $hourlyUsedEstimate
        dailyUsedEstimate = $dailyUsedEstimate
        hourlyTarget = $hourlyTarget
        dailyTarget = $dailyTarget
        hourlyRemaining = $hourlyRemaining
        dailyRemaining = $dailyRemaining
        nextWaitReason = $nextWaitReason
    }
    Write-JsonFile -Path $statusPath -Payload $payload
}

function Get-LanguagePriority {
    param($Config)

    $default = @('zh', 'jp', 'en')
    if ($null -eq $Config -or $null -eq $Config.languagePriority) {
        return $default
    }

    $values = @()
    foreach ($item in $Config.languagePriority) {
        $text = [string]$item
        if (-not [string]::IsNullOrWhiteSpace($text)) {
            $values += $text.Trim().ToLowerInvariant()
        }
    }

    $ordered = @()
    foreach ($item in $values) {
        if ($ordered -notcontains $item) {
            $ordered += $item
        }
    }
    foreach ($item in $default) {
        if ($ordered -notcontains $item) {
            $ordered += $item
        }
    }
    return $ordered
}

function Get-CatalogProgress {
    $statusJson = Read-JsonFile -Path $providerStatusPath
    $manifestJson = Read-JsonFile -Path $providerManifestPath
    $stateJson = Read-JsonFile -Path $statePath

    $cardsByLanguage = @{}
    $setFilesByLanguage = @{}
    $languagesCompleted = @{}
    $incomplete = @()

    if ($null -ne $statusJson -and $null -ne $statusJson.languages) {
        foreach ($prop in $statusJson.languages.PSObject.Properties) {
            $language = [string]$prop.Name
            $value = $prop.Value
            $setCount = [int]($value.setFileCount)
            $cardCount = [int]($value.cardCount)
            $complete = [bool]($value.complete)
            $available = [bool]($value.available)
            $cardsByLanguage[$language] = $cardCount
            $setFilesByLanguage[$language] = $setCount
            if ($available -and -not $complete) {
                $incomplete += $language
            }
        }
    }

    if ($null -ne $stateJson -and $null -ne $stateJson.languagesCompleted) {
        foreach ($prop in $stateJson.languagesCompleted.PSObject.Properties) {
            $languagesCompleted[[string]$prop.Name] = [bool]$prop.Value
        }
    }

    $totalCards = 0
    $totalSetFiles = 0
    if ($null -ne $manifestJson) {
        $totalCards = [int]($manifestJson.totalCards)
        $totalSetFiles = [int]($manifestJson.totalSetFiles)
    }

    $binaryImagesStored = $false
    $imageStorageMode = 'provider_reference_only'
    if ($null -ne $statusJson) {
        if ($null -ne $statusJson.binaryImagesStored) {
            $binaryImagesStored = [bool]$statusJson.binaryImagesStored
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$statusJson.imageStorageMode)) {
            $imageStorageMode = [string]$statusJson.imageStorageMode
        }
    }

    return [pscustomobject]@{
        IncompleteLanguages = $incomplete
        CardsByLanguage = $cardsByLanguage
        SetFilesByLanguage = $setFilesByLanguage
        LanguagesCompleted = $languagesCompleted
        TotalCards = $totalCards
        TotalSetFiles = $totalSetFiles
        BinaryImagesStored = $binaryImagesStored
        ImageStorageMode = $imageStorageMode
        IsComplete = ($incomplete.Count -eq 0 -and $totalSetFiles -gt 0)
    }
}

function Get-NextLanguage {
    param(
        [object]$Progress,
        [string[]]$Priority
    )

    if ($null -eq $Progress -or $Progress.IncompleteLanguages.Count -eq 0) {
        return $null
    }

    foreach ($language in $Priority) {
        if ($Progress.IncompleteLanguages -contains $language) {
            return $language
        }
    }

    return ($Progress.IncompleteLanguages | Sort-Object | Select-Object -First 1)
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
    param(
        [string]$Language,
        [int]$MaxRequestsForCycle
    )

    $args = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $cycleScript,
        '-RepoRoot',
        $RepoRoot
    )
    if ($MaxRequestsForCycle -gt 0) {
        $args += @('-MaxRequests', [string]$MaxRequestsForCycle)
    }
    if (-not [string]::IsNullOrWhiteSpace($Language)) {
        $args += @('-Language', $Language)
    }
    else {
        $args += '-AllLanguages'
    }

    $target = if ([string]::IsNullOrWhiteSpace($Language)) { 'all' } else { $Language }
    Write-WorkerLog "Starting catalogue cycle (targetLanguage=$target maxRequests=$MaxRequestsForCycle)."
    $stdoutPath = Join-Path $env:TEMP ("cardscanr-worker-cycle-{0}.out" -f ([guid]::NewGuid().ToString('N')))
    $stderrPath = Join-Path $env:TEMP ("cardscanr-worker-cycle-{0}.err" -f ([guid]::NewGuid().ToString('N')))
    $argumentList = (($args | ForEach-Object { Convert-ToProcessArgument -Value ([string]$_) }) -join ' ')
    try {
        $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $argumentList -WorkingDirectory $RepoRoot -NoNewWindow -PassThru -Wait -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $output = @()
        if (Test-Path $stdoutPath) {
            $output += @(Get-Content -Path $stdoutPath -Encoding UTF8)
        }
        if (Test-Path $stderrPath) {
            $output += @(Get-Content -Path $stderrPath -Encoding UTF8)
        }
        foreach ($line in $output) {
            Write-Host $line
            Add-Content -Path $script:logPath -Value ([string]$line) -Encoding UTF8
        }
        $exitCode = [int]$process.ExitCode
    }
    finally {
        Remove-Item -Path $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
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
        ExitCode = $exitCode
        Status = $status
        Commit = $commit
        Message = $message
    }
}

function Wait-UntilNextCycle {
    param(
        [datetime]$NextCycle,
        [string]$Reason = 'scheduled_interval'
    )

    while ([datetime]::UtcNow -lt $NextCycle) {
        $remaining = [int][Math]::Ceiling(($NextCycle - [datetime]::UtcNow).TotalSeconds)
        if ($remaining -lt 0) {
            return
        }
        $sleepSeconds = [Math]::Min(60, [Math]::Max(1, $remaining))
        Write-Host ("Next cycle in {0} minute(s). Reason={1}. Press Ctrl+C to stop." -f ([Math]::Ceiling($remaining / 60), $Reason)
        )
        Start-Sleep -Seconds $sleepSeconds
    }
}

function Get-DiagnosticsRequestsAttempted {
    $diag = Read-JsonFile -Path (Join-Path $RepoRoot 'public\v1\diagnostics\pokewallet-catalog-foundation-latest.json')
    if ($null -eq $diag -or $null -eq $diag.requestsAttempted) {
        return 0
    }
    return [int]$diag.requestsAttempted
}

function Get-BudgetDecisionForNow {
    param(
        [object]$Settings,
        [object]$Ledger,
        [string]$ApiKey
    )

    $nowUtc = (Get-Date).ToUniversalTime()
    $live = Try-GetLiveUsageSnapshot -Settings $Settings -ApiKey $ApiKey
    $usage = if ($null -ne $live) { $live } else { Get-LedgerUsageSnapshot -Ledger $Ledger -NowUtc $nowUtc }
    return Get-BudgetDecision -Settings $Settings -UsageSnapshot $usage -NowUtc $nowUtc
}

try {
    $rootConfig = Read-JsonFile -Path $configPath
    $config = if ($null -ne $rootConfig) { $rootConfig.fullCatalogueWorker } else { $null }

    if ($null -eq $config) {
        throw 'fullCatalogueWorker config is missing from data/pokewallet_catalog_config.json'
    }

    if ($IntervalMinutes -le 0 -and $null -ne $config.intervalMinutes) {
        $IntervalMinutes = [int]$config.intervalMinutes
    }
    if ($MaxRequests -le 0 -and $null -ne $config.maxRequestsPerCycle) {
        $MaxRequests = [int]$config.maxRequestsPerCycle
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$config.logPath)) {
        $script:logPath = Join-Path $RepoRoot ([string]$config.logPath)
    }
    if ($MaxRequests -le 0) {
        $MaxRequests = 80
    }

    $untilCompleteFromEnv = Get-EnvBoolOrDefault -Name 'POKEWALLET_WORKER_UNTIL_COMPLETE' -DefaultValue $false
    if ($untilCompleteFromEnv) {
        $UntilComplete = $true
        $script:mode = 'untilComplete'
    }

    $languagePriority = Get-LanguagePriority -Config $config
    $apiKeyEnvName = if ($null -ne $rootConfig -and -not [string]::IsNullOrWhiteSpace([string]$rootConfig.apiKeyEnv)) { [string]$rootConfig.apiKeyEnv } else { 'POKEWALLET_API_KEY' }
    $apiKey = [Environment]::GetEnvironmentVariable($apiKeyEnvName)

    $budgetSettings = Resolve-PokewalletBudgetSettings -WorkerConfig $config
    $ledger = Read-WorkerLedger -Path $budgetLedgerPath
    $minCycleGapSeconds = if ($null -ne $config.minCycleGapSeconds) { [int]$config.minCycleGapSeconds } else { 15 }
    if ($minCycleGapSeconds -lt 5) {
        $minCycleGapSeconds = 5
    }

    Acquire-WorkerLock
    Write-WorkerLog "Manual worker loop started. PID=$PID mode=$($script:mode) maxRequestsPerCycle=$MaxRequests languagePriority=$($languagePriority -join ',') hourlyTarget=$($budgetSettings.HourlyTarget) dailyTarget=$($budgetSettings.DailyTarget) budgetSource=$(if ($budgetSettings.UsageEndpointEnabled) { 'live+ledger-fallback' } else { 'ledger' })"
    $initialProgress = Get-CatalogProgress
    $initialBudget = Get-BudgetDecisionForNow -Settings $budgetSettings -Ledger $ledger -ApiKey $apiKey
    $initialLanguage = if ($UntilComplete) { Get-NextLanguage -Progress $initialProgress -Priority $languagePriority } else { $null }
    Write-Status -Running $true -LastStatus 'starting' -LastCycleStartedAtUtc $null -LastCycleFinishedAtUtc $null -NextCycleAtUtc (Get-UtcIso) -LastCommit $null -LastError $null -CurrentPriorityLanguage $(if ($null -ne $initialLanguage) { $initialLanguage } else { 'all' }) -NextLanguageToProcess $(if ($null -ne $initialLanguage) { $initialLanguage } else { 'all' }) -LanguagePriority $languagePriority -Progress $initialProgress -Budget $initialBudget

    while ($true) {
        $progressBefore = Get-CatalogProgress
        $budgetBefore = Get-BudgetDecisionForNow -Settings $budgetSettings -Ledger $ledger -ApiKey $apiKey
        $nextLanguage = $null

        if ($UntilComplete) {
            if ($progressBefore.IsComplete) {
                Write-WorkerLog 'All available provider catalogue languages are complete. Stopping until-complete loop.'
                $now = Get-UtcIso
                Write-Status -Running $false -LastStatus 'complete' -LastCycleStartedAtUtc $now -LastCycleFinishedAtUtc $now -NextCycleAtUtc $null -LastCommit $null -LastError $null -CurrentPriorityLanguage 'none' -NextLanguageToProcess 'none' -LanguagePriority $languagePriority -Progress $progressBefore -Budget $budgetBefore
                break
            }
            $nextLanguage = Get-NextLanguage -Progress $progressBefore -Priority $languagePriority
            if ([string]::IsNullOrWhiteSpace($nextLanguage)) {
                Write-WorkerLog 'No incomplete language was selected. Stopping loop safely.'
                $now = Get-UtcIso
                Write-Status -Running $false -LastStatus 'stopped' -LastCycleStartedAtUtc $now -LastCycleFinishedAtUtc $now -NextCycleAtUtc $null -LastCommit $null -LastError 'No incomplete language selected.' -CurrentPriorityLanguage 'none' -NextLanguageToProcess 'none' -LanguagePriority $languagePriority -Progress $progressBefore -Budget $budgetBefore
                break
            }
        }

        if ($budgetBefore.DailyRemaining -le 0) {
            Write-WorkerLog "Daily budget exhausted (used=$($budgetBefore.DailyUsed) target=$($budgetBefore.DailyTarget)). Stopping until next daily reset at $($budgetBefore.NextDailyResetUtc.ToString('yyyy-MM-ddTHH:mm:ssZ'))."
            $now = Get-UtcIso
            Write-Status -Running $false -LastStatus 'daily_budget_exhausted' -LastCycleStartedAtUtc $now -LastCycleFinishedAtUtc $now -NextCycleAtUtc $null -LastCommit $null -LastError 'Daily request budget exhausted.' -CurrentPriorityLanguage 'none' -NextLanguageToProcess 'none' -LanguagePriority $languagePriority -Progress $progressBefore -Budget $budgetBefore
            break
        }

        if ($budgetBefore.HourlyRemaining -le 0) {
            $nextCycle = (Get-Date).ToUniversalTime().AddSeconds([Math]::Max(1, $budgetBefore.WaitSeconds))
            $nextCycleAtUtc = Get-UtcIso -Value $nextCycle
            Write-WorkerLog "Hourly budget exhausted (used=$($budgetBefore.HourlyUsed) target=$($budgetBefore.HourlyTarget)). Waiting until $nextCycleAtUtc."
            Write-Status -Running $true -LastStatus 'waiting_budget' -LastCycleStartedAtUtc $null -LastCycleFinishedAtUtc $null -NextCycleAtUtc $nextCycleAtUtc -LastCommit $null -LastError $null -CurrentPriorityLanguage $(if ($UntilComplete) { $nextLanguage } else { 'all' }) -NextLanguageToProcess $(if ($UntilComplete) { $nextLanguage } else { 'all' }) -LanguagePriority $languagePriority -Progress $progressBefore -Budget $budgetBefore
            Wait-UntilNextCycle -NextCycle $nextCycle -Reason 'hourly_budget_exhausted'
            continue
        }

        $cycleMaxRequests = [Math]::Min($MaxRequests, [Math]::Min($budgetBefore.HourlyRemaining, $budgetBefore.DailyRemaining))
        if ($cycleMaxRequests -le 0) {
            $nextCycle = (Get-Date).ToUniversalTime().AddSeconds(30)
            Write-Status -Running $true -LastStatus 'waiting_budget' -LastCycleStartedAtUtc $null -LastCycleFinishedAtUtc $null -NextCycleAtUtc (Get-UtcIso -Value $nextCycle) -LastCommit $null -LastError $null -CurrentPriorityLanguage $(if ($UntilComplete) { $nextLanguage } else { 'all' }) -NextLanguageToProcess $(if ($UntilComplete) { $nextLanguage } else { 'all' }) -LanguagePriority $languagePriority -Progress $progressBefore -Budget $budgetBefore
            Wait-UntilNextCycle -NextCycle $nextCycle -Reason 'budget_unavailable'
            continue
        }

        $cycleStartedDate = (Get-Date).ToUniversalTime()
        $cycleStartedAt = Get-UtcIso -Value $cycleStartedDate
        $currentTarget = if ($UntilComplete) { $nextLanguage } else { 'all' }
        Write-Status -Running $true -LastStatus 'running_cycle' -LastCycleStartedAtUtc $cycleStartedAt -LastCycleFinishedAtUtc $null -NextCycleAtUtc $null -LastCommit $null -LastError $null -CurrentPriorityLanguage $currentTarget -NextLanguageToProcess $currentTarget -LanguagePriority $languagePriority -Progress $progressBefore -Budget $budgetBefore

        $cycle = Invoke-Cycle -Language $nextLanguage -MaxRequestsForCycle $cycleMaxRequests
        $cycleFinishedDate = (Get-Date).ToUniversalTime()
        $cycleFinishedAt = Get-UtcIso -Value $cycleFinishedDate
        $lastError = $null
        if ($cycle.Status -notin @('ok', 'no_changes', 'partial', 'complete')) {
            $lastError = $cycle.Message
        }

        $requestsThisCycle = Get-DiagnosticsRequestsAttempted
        $ledger = Add-LedgerEntry -Ledger $ledger -TimestampUtc $cycleFinishedDate -Requests $requestsThisCycle -Source 'cycle' -Status $cycle.Status
        Write-WorkerLedger -Path $budgetLedgerPath -Ledger $ledger

        $budgetAfter = Get-BudgetDecisionForNow -Settings $budgetSettings -Ledger $ledger -ApiKey $apiKey
        $progressAfter = Get-CatalogProgress

        Write-WorkerLog "Budget cycle summary: requestsUsedThisCycle=$requestsThisCycle hourlyUsed=$($budgetAfter.HourlyUsed)/$($budgetAfter.HourlyTarget) dailyUsed=$($budgetAfter.DailyUsed)/$($budgetAfter.DailyTarget) hourlyRemaining=$($budgetAfter.HourlyRemaining) dailyRemaining=$($budgetAfter.DailyRemaining) usageSource=$($budgetAfter.UsageSource)"

        if ($cycle.Status -eq 'rate_limited') {
            Write-WorkerLog 'Stopping manual worker because the provider returned rate limit status.'
            Write-Status -Running $false -LastStatus 'rate_limited' -LastCycleStartedAtUtc $cycleStartedAt -LastCycleFinishedAtUtc $cycleFinishedAt -NextCycleAtUtc $null -LastCommit $cycle.Commit -LastError $cycle.Message -CurrentPriorityLanguage $(if ($null -ne $currentTarget) { $currentTarget } else { 'all' }) -NextLanguageToProcess 'none' -LanguagePriority $languagePriority -Progress $progressAfter -Budget $budgetAfter
            break
        }

        if ($cycle.ExitCode -ne 0 -or $cycle.Status -in @('dirty_worktree', 'validation_failed', 'export_failed', 'git_commit_failed', 'git_push_failed', 'git_stage_failed', 'error')) {
            Write-WorkerLog "Stopping manual worker after cycle status=$($cycle.Status)."
            Write-Status -Running $false -LastStatus $cycle.Status -LastCycleStartedAtUtc $cycleStartedAt -LastCycleFinishedAtUtc $cycleFinishedAt -NextCycleAtUtc $null -LastCommit $cycle.Commit -LastError $lastError -CurrentPriorityLanguage $(if ($null -ne $currentTarget) { $currentTarget } else { 'all' }) -NextLanguageToProcess 'none' -LanguagePriority $languagePriority -Progress $progressAfter -Budget $budgetAfter
            break
        }

        if ($UntilComplete -and $progressAfter.IsComplete) {
            Write-WorkerLog 'All available provider catalogue languages are complete. Stopping until-complete loop.'
            Write-Status -Running $false -LastStatus 'complete' -LastCycleStartedAtUtc $cycleStartedAt -LastCycleFinishedAtUtc $cycleFinishedAt -NextCycleAtUtc $null -LastCommit $cycle.Commit -LastError $null -CurrentPriorityLanguage 'none' -NextLanguageToProcess 'none' -LanguagePriority $languagePriority -Progress $progressAfter -Budget $budgetAfter
            break
        }

        if ($budgetAfter.DailyRemaining -le 0) {
            Write-WorkerLog "Daily budget reached after cycle. Stopping until next UTC day reset at $($budgetAfter.NextDailyResetUtc.ToString('yyyy-MM-ddTHH:mm:ssZ'))."
            Write-Status -Running $false -LastStatus 'daily_budget_exhausted' -LastCycleStartedAtUtc $cycleStartedAt -LastCycleFinishedAtUtc $cycleFinishedAt -NextCycleAtUtc $null -LastCommit $cycle.Commit -LastError 'Daily request budget exhausted.' -CurrentPriorityLanguage $(if ($null -ne $currentTarget) { $currentTarget } else { 'all' }) -NextLanguageToProcess 'none' -LanguagePriority $languagePriority -Progress $progressAfter -Budget $budgetAfter
            break
        }

        $waitReason = 'pacing'
        $waitSeconds = Get-PacingWaitSeconds -RequestsUsedThisCycle $requestsThisCycle -CycleStartedUtc $cycleStartedDate -CycleFinishedUtc $cycleFinishedDate -HourlyTarget $budgetSettings.HourlyTarget
        if ($budgetAfter.HourlyRemaining -le 0) {
            $waitReason = 'hourly_budget_exhausted'
            $waitSeconds = [Math]::Max(1, $budgetAfter.WaitSeconds)
        }
        elseif ($waitSeconds -le 0) {
            $waitReason = 'min_cycle_gap'
            $waitSeconds = $minCycleGapSeconds
        }

        $nextCycle = (Get-Date).ToUniversalTime().AddSeconds($waitSeconds)
        $nextCycleAtUtc = Get-UtcIso -Value $nextCycle
        $upcomingLanguage = if ($UntilComplete) { Get-NextLanguage -Progress $progressAfter -Priority $languagePriority } else { $null }

        Write-WorkerLog "Next wait reason=$waitReason waitSeconds=$waitSeconds nextCycleAtUtc=$nextCycleAtUtc"
        Write-Status -Running $true -LastStatus $cycle.Status -LastCycleStartedAtUtc $cycleStartedAt -LastCycleFinishedAtUtc $cycleFinishedAt -NextCycleAtUtc $nextCycleAtUtc -LastCommit $cycle.Commit -LastError $lastError -CurrentPriorityLanguage $(if ($null -ne $currentTarget) { $currentTarget } else { 'all' }) -NextLanguageToProcess $(if ($null -ne $upcomingLanguage) { $upcomingLanguage } else { if ($UntilComplete) { 'none' } else { 'all' } }) -LanguagePriority $languagePriority -Progress $progressAfter -Budget $budgetAfter
        Wait-UntilNextCycle -NextCycle $nextCycle -Reason $waitReason
    }
}
finally {
    if ($script:lockAcquired) {
        Remove-Item -Path $workerLockPath -Force -ErrorAction SilentlyContinue
        $existing = Read-JsonFile -Path $statusPath
        $progressFinal = Get-CatalogProgress
        $priorityFinal = Get-LanguagePriority -Config (Get-WorkerConfig)
        $ledgerFinal = Read-WorkerLedger -Path $budgetLedgerPath
        $rootFinal = Read-JsonFile -Path $configPath
        $configFinal = if ($null -ne $rootFinal) { $rootFinal.fullCatalogueWorker } else { $null }
        $settingsFinal = Resolve-PokewalletBudgetSettings -WorkerConfig $configFinal
        $apiKeyEnvFinal = if ($null -ne $rootFinal -and -not [string]::IsNullOrWhiteSpace([string]$rootFinal.apiKeyEnv)) { [string]$rootFinal.apiKeyEnv } else { 'POKEWALLET_API_KEY' }
        $apiKeyFinal = [Environment]::GetEnvironmentVariable($apiKeyEnvFinal)
        $budgetFinal = Get-BudgetDecisionForNow -Settings $settingsFinal -Ledger $ledgerFinal -ApiKey $apiKeyFinal
        Write-Status -Running $false -LastStatus $(if ($null -ne $existing) { [string]$existing.lastStatus } else { 'stopped' }) -LastCycleStartedAtUtc $(if ($null -ne $existing) { [string]$existing.lastCycleStartedAtUtc } else { $null }) -LastCycleFinishedAtUtc $(if ($null -ne $existing) { [string]$existing.lastCycleFinishedAtUtc } else { $null }) -NextCycleAtUtc $null -LastCommit $(if ($null -ne $existing) { [string]$existing.lastCommit } else { $null }) -LastError $(if ($null -ne $existing) { [string]$existing.lastError } else { $null }) -CurrentPriorityLanguage $(if ($null -ne $existing -and -not [string]::IsNullOrWhiteSpace([string]$existing.currentPriorityLanguage)) { [string]$existing.currentPriorityLanguage } else { 'none' }) -NextLanguageToProcess $(if ($null -ne $existing -and -not [string]::IsNullOrWhiteSpace([string]$existing.nextLanguageToProcess)) { [string]$existing.nextLanguageToProcess } else { if ($UntilComplete) { 'none' } else { 'all' } }) -LanguagePriority $priorityFinal -Progress $progressFinal -Budget $budgetFinal
        Write-WorkerLog 'Manual worker loop stopped.'
    }
}
