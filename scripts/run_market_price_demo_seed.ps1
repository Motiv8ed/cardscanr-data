<#
.SYNOPSIS
    Phase 4A: Seed market-aware demo data for the CardScanR Market Price Engine.

.DESCRIPTION
    Wraps scripts/seed_market_price_demo_data.py.  Requires SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY, and MARKET_LOOKUP_PROVIDER=mock (default).

.PARAMETER Markets
    Comma-separated market codes to seed.  Default: AU,US,GB,CA.

.PARAMETER Cards
    Card set filter: smoke, classic, all.  Default: smoke,classic.

.PARAMETER EnqueueOnly
    Enqueue refresh jobs but do not process them with the mock worker.

.PARAMETER Process
    Run the mock worker after enqueueing to produce cache/snapshot/evidence rows.

.PARAMETER MaxJobs
    Maximum number of jobs for the worker run.  Default: 50.

.PARAMETER DryRun
    Show seed plan without making any DB writes.

.EXAMPLE
    .\scripts\run_market_price_demo_seed.ps1 -DryRun

.EXAMPLE
    .\scripts\run_market_price_demo_seed.ps1 -Markets AU,US,GB,CA -Cards smoke -EnqueueOnly

.EXAMPLE
    .\scripts\run_market_price_demo_seed.ps1 -Markets AU,US,GB,CA -Cards all -Process -MaxJobs 50
#>
param(
    [string]$Markets = "AU,US,GB,CA",
    [string]$Cards = "smoke,classic",
    [switch]$EnqueueOnly,
    [switch]$Process,
    [int]$MaxJobs = 50,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not $DryRun) {
    $requiredVars = @("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
    $missing = @()
    foreach ($name in $requiredVars) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            $missing += $name
        }
    }
    $provider = [Environment]::GetEnvironmentVariable("MARKET_LOOKUP_PROVIDER")
    if ([string]::IsNullOrWhiteSpace($provider)) { $provider = "mock" }
    if ($provider.ToLowerInvariant() -ne "mock") {
        throw "MARKET_LOOKUP_PROVIDER must be 'mock'. Current value: '$provider'"
    }
    if ($missing.Count -gt 0) {
        throw ("Missing required environment variables: " + ($missing -join ", "))
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$scriptArgs = @(
    "scripts/seed_market_price_demo_data.py",
    "--markets", $Markets,
    "--cards", $Cards,
    "--max-jobs", $MaxJobs
)
if ($EnqueueOnly) { $scriptArgs += "--enqueue-only" }
if ($Process)     { $scriptArgs += "--process" }
if ($DryRun)      { $scriptArgs += "--dry-run" }

Write-Host "[demo-seed] Running seed script: $pythonPath $($scriptArgs -join ' ')"
& $pythonPath @scriptArgs
if ($LASTEXITCODE -ne 0) {
    throw "seed_market_price_demo_data.py failed with exit code $LASTEXITCODE"
}
