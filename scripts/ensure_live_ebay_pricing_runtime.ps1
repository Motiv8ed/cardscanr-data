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

function Stop-Component {
    param(
        [Parameter(Mandatory = $true)][string]$Needle,
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $procs = @(Get-MatchingPythonProcesses -CommandNeedle $Needle)
    foreach ($proc in $procs) {
        Write-Host "[runtime] Stopping $Label PID=$($proc.ProcessId)"
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
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

    Set-LiveEbayWorkerEnvironment -ProfilePath $profilePath -Headless $true
    $env:MARKET_WORKER_ALLOWED_MARKETS = "AU,US,GB,CA"
    $env:MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS = "NONE"
    $env:MARKET_WORKER_CONCURRENCY = "1"
    $env:MARKET_WORKER_MAX_JOBS_PER_RUN = "1"
    $env:MARKET_WORKER_POLL_SECONDS = "30"

    $stdout = Join-Path $stateDir "live_ebay_worker.out.log"
    $stderr = Join-Path $stateDir "live_ebay_worker.err.log"
    $argList = @(
        "workers/market_price_worker.py",
        "--max-jobs", "1",
        "--poll-seconds", "30"
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
    $env:MARKET_SCHEDULER_MAX_KEYS_PER_RUN = "25"
    $env:MARKET_SCHEDULER_MAX_ENQUEUES_PER_RUN = "2"
    $env:MARKET_SCHEDULER_POLL_SECONDS = "900"
    $env:MARKET_SCHEDULER_DRY_RUN = "false"
    $env:MARKET_SCHEDULER_INCLUDE_MISSING_CACHE = "true"
    $env:MARKET_SCHEDULER_INCLUDE_STALE_CACHE = "true"

    $stdout = Join-Path $stateDir "live_ebay_scheduler.out.log"
    $stderr = Join-Path $stateDir "live_ebay_scheduler.err.log"
    $argList = @(
        "workers/market_price_scheduler.py",
        "--poll-seconds", "900"
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
        maxEnqueuesPerRun = 2
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

if ($Stop) {
    if ($Component -in @("worker", "both")) {
        Stop-Component -Needle "workers/market_price_worker.py" -StatePath $workerStatePath -Label "worker"
    }
    if ($Component -in @("scheduler", "both")) {
        Stop-Component -Needle "workers/market_price_scheduler.py" -StatePath $schedulerStatePath -Label "scheduler"
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
Show-Status
