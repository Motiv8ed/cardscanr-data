param(
    [switch]$Once,
    [int]$MaxCycles = 0,
    [int]$PollSeconds = 0,
    [int]$MaxEnqueue = 0,
    [switch]$DryRun,
    [switch]$FullEnable,
    [switch]$NoSyncKeys
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

if ($MaxEnqueue -gt 0) {
    $env:OWNED_DAILY_MAX_ENQUEUE = [string]$MaxEnqueue
}
if ($DryRun) {
    $env:OWNED_DAILY_DRY_RUN = "true"
}
if ($FullEnable) {
    $env:OWNED_DAILY_FULL_ENABLE = "true"
}
if ($NoSyncKeys) {
    $env:OWNED_DAILY_SYNC_KEYS = "false"
}
if ($PollSeconds -gt 0) {
    $env:OWNED_DAILY_POLL_SECONDS = [string]$PollSeconds
}

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @("workers/owned_daily_price_scheduler.py")
if ($Once) { $argsList += "--once" }
if ($MaxCycles -gt 0) { $argsList += @("--max-cycles", [string]$MaxCycles) }
if ($PollSeconds -gt 0) { $argsList += @("--poll-seconds", [string]$PollSeconds) }
if ($DryRun) { $argsList += "--dry-run" }
if ($MaxEnqueue -gt 0) { $argsList += @("--max-enqueue", [string]$MaxEnqueue) }
if ($FullEnable) { $argsList += "--full-enable" }
if ($NoSyncKeys) { $argsList += "--no-sync-keys" }

Write-Host "[owned-daily-scheduler] Running owned daily price scheduler..."
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "owned_daily_price_scheduler.py failed with exit code $LASTEXITCODE"
}
