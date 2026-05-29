param(
    [object]$Markets = "AU",
    [int]$MaxEnqueues = 2,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

if ($Markets -is [array]) {
    $marketsArg = (($Markets | ForEach-Object { "$_".Trim() }) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ","
} else {
    $marketsArg = "$Markets".Trim()
    if ($marketsArg -notlike "*,*" -and $marketsArg -match "\s+") {
        $marketsArg = (($marketsArg -split "\s+") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ","
    }
}

$env:MARKET_LOOKUP_PROVIDER = "ebay_browser"
$env:ENABLE_EBAY_REAL_LOOKUP = "true"
$env:LIVE_EBAY_SCHEDULER_MARKETS = $marketsArg
$env:LIVE_EBAY_SCHEDULER_MAX_ENQUEUES_PER_RUN = [string]$MaxEnqueues
$env:LIVE_EBAY_SCHEDULER_DRY_RUN = if ($DryRun) { "true" } else { "false" }

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @(
    "scripts/smoke_ebay_browser_live_scheduler.py",
    "--markets", $marketsArg,
    "--max-enqueues", [string]$MaxEnqueues
)
if ($DryRun) {
    $argsList += "--dry-run"
} else {
    $argsList += "--real-enqueue"
}

Write-Host "[market-engine] Running guarded live eBay scheduler smoke. DryRun=$($DryRun.IsPresent) Markets=$marketsArg MaxEnqueues=$MaxEnqueues"
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "smoke_ebay_browser_live_scheduler.py failed with exit code $LASTEXITCODE"
}

try {
    $bundleScript = Join-Path $repoRoot "scripts\create_market_engine_upload_bundle.ps1"
    & $bundleScript -Kind ebay_browser_live_scheduler -Output "reports\chatgpt_uploads\ebay_browser_live_scheduler_latest.zip"
    if ($LASTEXITCODE -ne 0) {
        throw "bundle script exited with code $LASTEXITCODE"
    }
} catch {
    Write-Warning "Upload bundle creation failed: $($_.Exception.Message)"
    Write-Host "Create it manually with:"
    Write-Host ".\scripts\create_market_engine_upload_bundle.ps1 -Kind ebay_browser_live_scheduler -Output reports\chatgpt_uploads\ebay_browser_live_scheduler_latest.zip"
}
