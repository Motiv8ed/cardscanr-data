[CmdletBinding()]
param(
    [ValidateSet("worker", "scheduler", "both", "status")]
    [string]$Component = "both",
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

. (Join-Path $repoRoot "scripts\live_ebay_worker_config.ps1")
$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

$stateDir = Join-Path $repoRoot "reports\runtime"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$workerStatePath = Join-Path $stateDir "live_ebay_worker.pid.json"
$schedulerStatePath = Join-Path $stateDir "live_ebay_scheduler.pid.json"
$profilePath = Assert-SafeLiveEbayProfilePath -ProfilePath (Join-Path $repoRoot ".browser_profiles\cardscanr")
$pythonPath = Resolve-CardScanRPythonPath -RepoRoot $repoRoot

function Get-MatchingPythonProcesses {
    param([Parameter(Mandatory = $true)][string]$CommandNeedle)
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -and ($_.CommandLine -like "*$CommandNeedle*") }
}

function Read-StateFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    try {
        return Get-Content -Raw -Path $Path | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Write-StateFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Payload
    )
    ($Payload | ConvertTo-Json -Depth 6) | Set-Content -Path $Path -Encoding utf8
}

function Write-StopIntent {
    param(
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$Reason
    )
    $path = Join-Path $stateDir ("{0}_stop_intent.json" -f $Component)
    Write-StateFile -Path $path -Payload @{
        component = $Component
        pid = $ProcessId
        reason = $Reason
        requestedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    }
}

function Stop-Component {
    param(
        [Parameter(Mandatory = $true)][string]$Needle,
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$Label,
        [string]$Reason = "ensure_runtime_stop"
    )
    $procs = @(Get-MatchingPythonProcesses -CommandNeedle $Needle)
    foreach ($proc in $procs) {
        Write-Host "[runtime] Stopping $Label PID=$($proc.ProcessId) reason=$Reason"
        $intentName = if ($Label -eq "worker") { "worker" } else { "scheduler" }
        Write-StopIntent -Component $intentName -ProcessId ([int]$proc.ProcessId) -Reason $Reason
        # Prefer a non-force stop first so wrappers can observe a cleaner exit when possible.
        try {
            Stop-Process -Id $proc.ProcessId -ErrorAction Stop
        } catch {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 400
        if (Get-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue) {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path $StatePath) {
        Remove-Item -Force $StatePath
    }
}

function Ensure-Worker {
    $existing = @(Get-MatchingPythonProcesses -CommandNeedle "workers/market_price_worker.py")
    if ($existing.Count -gt 0) {
        $pidValue = $existing[0].ProcessId
        Write-Host "[runtime] Worker already healthy PID=$pidValue"
        Write-StateFile -Path $workerStatePath -Payload @{
            component = "worker"
            pid = $pidValue
            checkedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
            status = "already_running"
        }
        return
    }

    # ebay.com.au rejects sold deep-links and also rejects the same Sold/Completed
    # UI filter navigation under headless Chrome (SORRY error page). Headed works.
    # Production mode is intentionally headed (EBAY_BROWSER_MODE=headed).
    Set-LiveEbayWorkerEnvironment -ProfilePath $profilePath -Headless $false
    $env:EBAY_BROWSER_HEADLESS = "false"
    $env:EBAY_BROWSER_MODE = "headed"
    $env:MARKET_WORKER_ALLOWED_MARKETS = "AU,US,GB,CA"
    $env:MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS = "NONE"
    $env:MARKET_WORKER_CONCURRENCY = "1"
    $env:MARKET_WORKER_MAX_JOBS_PER_RUN = "1"
    $env:MARKET_WORKER_POLL_SECONDS = "5"
    # Keep single-browser safety; throughput gains come from fuller queue + less idle poll.
    $env:EBAY_BROWSER_MAX_CONCURRENCY = "1"
    # Prefer non-reuse launches; persistent reuse has TargetClosedError flakes on this host.
    $env:EBAY_BROWSER_REUSE_CONTEXT = "false"
    $env:EBAY_BROWSER_RECYCLE_AFTER_NAVIGATIONS = "20"
    # Owned-daily enable is controlled solely by owned_daily_full_enable.flag.
    # Ensure must never invent or persist true.
    $fullEnable = Read-OwnedDailyFullEnableFlag -StateDir $stateDir
    $env:OWNED_DAILY_FULL_ENABLE = if ($fullEnable) { "true" } else { "false" }
    if (-not $env:OWNED_DAILY_MAX_ENQUEUE) { $env:OWNED_DAILY_MAX_ENQUEUE = "25" }
    Write-LiveEbayRuntimeConfig -StateDir $stateDir -ProfilePath $profilePath -Headless $false -OwnedDailyFullEnable $fullEnable -OwnedDailyMaxEnqueue ([int]$env:OWNED_DAILY_MAX_ENQUEUE) | Out-Null
    Write-LiveEbayWorkerConfigSummary -ProfilePath $profilePath -Headless $false -PollSeconds 5 -MaxJobs 1

    $stdout = Join-Path $stateDir "live_ebay_worker.out.log"
    $stderr = Join-Path $stateDir "live_ebay_worker.err.log"
    $env:PYTHONUNBUFFERED = "1"
    $argList = @(
        "-u",
        "workers/market_price_worker.py",
        "--max-jobs", "1",
        "--poll-seconds", "5"
    )
    $proc = Start-Process -FilePath $pythonPath -ArgumentList $argList `
        -WorkingDirectory $repoRoot -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
        -PassThru
    Write-StateFile -Path $workerStatePath -Payload @{
        component = "worker"
        pid = $proc.Id
        startedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
        status = "started"
        stdoutLog = $stdout
        stderrLog = $stderr
    }
    Write-Host "[runtime] Started worker PID=$($proc.Id)"
}

function Ensure-Scheduler {
    $existing = @(Get-MatchingPythonProcesses -CommandNeedle "workers/market_price_scheduler.py")
    if ($existing.Count -gt 0) {
        $pidValue = $existing[0].ProcessId
        Write-Host "[runtime] Scheduler already healthy PID=$pidValue"
        Write-StateFile -Path $schedulerStatePath -Payload @{
            component = "scheduler"
            pid = $pidValue
            checkedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
            status = "already_running"
        }
        return
    }

    $env:MARKET_SCHEDULER_ALLOWED_MARKETS = "AU,US,GB,CA"
    $env:MARKET_SCHEDULER_MAX_KEYS_PER_RUN = "50"
    $env:MARKET_SCHEDULER_MAX_ENQUEUES_PER_RUN = "10"
    $env:MARKET_SCHEDULER_QUEUE_LOW_WATERMARK = "4"
    $env:MARKET_SCHEDULER_QUEUE_HIGH_WATERMARK = "12"
    $env:MARKET_SCHEDULER_POLL_SECONDS = "60"
    $env:MARKET_SCHEDULER_DRY_RUN = "false"
    $env:MARKET_SCHEDULER_INCLUDE_MISSING_CACHE = "true"
    $env:MARKET_SCHEDULER_INCLUDE_STALE_CACHE = "true"
    # Once-daily owned pass runs inside this scheduler process only when flag is true.
    $fullEnable = Read-OwnedDailyFullEnableFlag -StateDir $stateDir
    $env:OWNED_DAILY_FULL_ENABLE = if ($fullEnable) { "true" } else { "false" }
    if (-not $env:OWNED_DAILY_MAX_ENQUEUE) { $env:OWNED_DAILY_MAX_ENQUEUE = "25" }
    $env:OWNED_DAILY_SYNC_KEYS = "true"
    # Scheduler shares the same persistent runtime config (headed; enable from flag).
    if (-not $env:EBAY_BROWSER_MODE) { $env:EBAY_BROWSER_MODE = "headed" }
    if (-not $env:EBAY_BROWSER_HEADLESS) { $env:EBAY_BROWSER_HEADLESS = "false" }
    Write-LiveEbayRuntimeConfig -StateDir $stateDir -ProfilePath $profilePath -Headless $false -OwnedDailyFullEnable $fullEnable -OwnedDailyMaxEnqueue ([int]$env:OWNED_DAILY_MAX_ENQUEUE) | Out-Null

    $stdout = Join-Path $stateDir "live_ebay_scheduler.out.log"
    $stderr = Join-Path $stateDir "live_ebay_scheduler.err.log"
    $env:PYTHONUNBUFFERED = "1"
    $argList = @(
        "-u",
        "workers/market_price_scheduler.py",
        "--poll-seconds", "60"
    )
    $proc = Start-Process -FilePath $pythonPath -ArgumentList $argList `
        -WorkingDirectory $repoRoot -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
        -PassThru
    Write-StateFile -Path $schedulerStatePath -Payload @{
        component = "scheduler"
        pid = $proc.Id
        startedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
        status = "started"
        stdoutLog = $stdout
        stderrLog = $stderr
        allowedMarkets = "AU,US,GB,CA"
        maxEnqueuesPerRun = 10
        queueLowWatermark = 4
        queueHighWatermark = 12
        pollSeconds = 60
    }
    Write-Host "[runtime] Started scheduler PID=$($proc.Id)"
}

function Show-Status {
    $workerProcs = @(Get-MatchingPythonProcesses -CommandNeedle "workers/market_price_worker.py")
    $schedulerProcs = @(Get-MatchingPythonProcesses -CommandNeedle "workers/market_price_scheduler.py")
    [pscustomobject]@{
        workerRunning = ($workerProcs.Count -gt 0)
        workerPids = @($workerProcs | ForEach-Object { $_.ProcessId })
        schedulerRunning = ($schedulerProcs.Count -gt 0)
        schedulerPids = @($schedulerProcs | ForEach-Object { $_.ProcessId })
        workerState = Read-StateFile -Path $workerStatePath
        schedulerState = Read-StateFile -Path $schedulerStatePath
    } | ConvertTo-Json -Depth 6
}

function Write-HealthReportBestEffort {
    try {
        $healthScript = Join-Path $repoRoot "scripts\report_ebay_pricing_health.py"
        if (Test-Path $healthScript) {
            & $pythonPath $healthScript | Out-Null
        }
    } catch {
        Write-Host "[runtime] health report skipped: $($_.Exception.Message)"
    }
}

if ($Stop) {
    if ($Component -in @("worker", "both")) {
        Stop-Component -Needle "workers/market_price_worker.py" -StatePath $workerStatePath -Label "worker" -Reason "ensure_runtime_stop"
    }
    if ($Component -in @("scheduler", "both")) {
        Stop-Component -Needle "workers/market_price_scheduler.py" -StatePath $schedulerStatePath -Label "scheduler" -Reason "ensure_runtime_stop"
    }
    Show-Status
    return
}

if ($Component -eq "status") {
    Show-Status
    return
}

if ($Component -in @("worker", "both")) {
    Ensure-Worker
}
if ($Component -in @("scheduler", "both")) {
    Ensure-Scheduler
}
if ($Component -in @("worker", "scheduler", "both", "status")) {
    Write-HealthReportBestEffort
}
Show-Status
