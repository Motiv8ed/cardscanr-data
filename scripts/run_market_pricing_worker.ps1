param(
    [ValidateSet("AU", "US", "GB", "CA", "EU")][string]$Market = "AU",
    [ValidateSet("en", "jp")][string]$Language = "en",
    [string]$Game = "pokemon",
    [int]$MaxJobs = 25,
    [ValidateSet("mock", "manual")][string]$Provider = "mock",
    [switch]$DryRun,
    [switch]$Write,
    [switch]$CommitSafeReport,
    [string]$CardId,
    [string]$SetId,
    [switch]$QueryOnly,
    [string]$ManualSourcePath = "data/manual_market_prices/sample_market_sold_listings.json",
    [string]$Condition = "near_mint",
    [string]$Variant = "raw",
    [switch]$Graded
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @(
    "tools/market_pricing_worker.py",
    "--market", $Market,
    "--language", $Language,
    "--game", $Game,
    "--max-jobs", [string]$MaxJobs,
    "--provider", $Provider,
    "--manual-source-path", $ManualSourcePath,
    "--condition", $Condition,
    "--variant", $Variant
)

if ($DryRun) { $argsList += "--dry-run" }
if ($Write) { $argsList += "--write" }
if ($CommitSafeReport) { $argsList += "--commit-safe-report" }
if ($CardId) { $argsList += @("--card-id", $CardId) }
if ($SetId) { $argsList += @("--set-id", $SetId) }
if ($QueryOnly) { $argsList += "--query-only" }
if ($Graded) { $argsList += "--graded" }

Write-Host "[worker] Running market pricing worker foundation..."
Write-Host ("  market={0} language={1} provider={2} dryRun={3} write={4}" -f $Market, $Language, $Provider, $(if ($DryRun) { 'yes' } else { 'no' }), $(if ($Write) { 'yes' } else { 'no' }))

& $pythonPath "-u" @argsList
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    throw "market_pricing_worker.py failed with exit code $exitCode"
}
