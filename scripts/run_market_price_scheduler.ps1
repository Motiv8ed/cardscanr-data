param(
    [switch]$Once,
    [int]$MaxCycles = 0,
    [int]$PollSeconds = 0,
    [int]$MaxKeys = 0,
    [int]$MaxEnqueues = 0,
    [string]$AllowedMarkets = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

if ($MaxKeys -gt 0) {
    $env:MARKET_SCHEDULER_MAX_KEYS_PER_RUN = [string]$MaxKeys
}
if ($MaxEnqueues -gt 0) {
    $env:MARKET_SCHEDULER_MAX_ENQUEUES_PER_RUN = [string]$MaxEnqueues
}
if (-not [string]::IsNullOrWhiteSpace($AllowedMarkets)) {
    $env:MARKET_SCHEDULER_ALLOWED_MARKETS = $AllowedMarkets
}
if ($DryRun) {
    $env:MARKET_SCHEDULER_DRY_RUN = "true"
}
if ($PollSeconds -gt 0) {
    $env:MARKET_SCHEDULER_POLL_SECONDS = [string]$PollSeconds
}

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @("workers/market_price_scheduler.py")
if ($Once) { $argsList += "--once" }
if ($MaxCycles -gt 0) { $argsList += @("--max-cycles", [string]$MaxCycles) }
if ($PollSeconds -gt 0) { $argsList += @("--poll-seconds", [string]$PollSeconds) }

Write-Host "[market-scheduler] Running market price scheduler..."
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "market_price_scheduler.py failed with exit code $LASTEXITCODE"
}
