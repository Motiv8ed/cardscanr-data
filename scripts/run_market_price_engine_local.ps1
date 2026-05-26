param(
    [int]$Cycles = 1,
    [int]$PollSeconds = 0,
    [int]$SchedulerMaxKeys = 100,
    [int]$SchedulerMaxEnqueues = 50,
    [int]$WorkerMaxJobs = 50,
    [switch]$DryRun,
    [string]$ReportsDir = "reports"
)

$ErrorActionPreference = "Stop"

# Mock-safety check — fail fast if a live provider is configured
$provider = [Environment]::GetEnvironmentVariable("MARKET_LOOKUP_PROVIDER")
if (-not [string]::IsNullOrWhiteSpace($provider) -and $provider.ToLowerInvariant() -ne "mock") {
    throw "MARKET_LOOKUP_PROVIDER must be 'mock' when set. Current value: '$provider'. Phase 4B supports mock-only execution."
}

# Warn (not block) if Supabase env vars are missing and this is a live (non-dry) run
if (-not $DryRun) {
    $missingVars = @()
    foreach ($name in @("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            $missingVars += $name
        }
    }
    if ($missingVars.Count -gt 0) {
        Write-Warning ("Missing required env vars for live Supabase run: " + ($missingVars -join ", ") + ". Use -DryRun or set the vars.")
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @(
    "workers/market_price_engine_local.py",
    "--cycles", [string]$Cycles,
    "--scheduler-max-keys", [string]$SchedulerMaxKeys,
    "--scheduler-max-enqueues", [string]$SchedulerMaxEnqueues,
    "--worker-max-jobs", [string]$WorkerMaxJobs,
    "--reports-dir", $ReportsDir
)

if ($PollSeconds -gt 0) {
    $argsList += @("--poll-seconds", [string]$PollSeconds)
}
if ($DryRun) {
    $argsList += "--dry-run"
}

Write-Host "[market-engine-local] Starting local market price engine runner..."
Write-Host "[market-engine-local] Cycles=$Cycles DryRun=$DryRun PollSeconds=$PollSeconds SchedulerMaxKeys=$SchedulerMaxKeys SchedulerMaxEnqueues=$SchedulerMaxEnqueues WorkerMaxJobs=$WorkerMaxJobs"

& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "market_price_engine_local.py exited with code $LASTEXITCODE"
}

Write-Host "[market-engine-local] Done. Reports written to $ReportsDir/"
