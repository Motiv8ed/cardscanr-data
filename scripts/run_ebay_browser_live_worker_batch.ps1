param(
    [object]$Markets = "AU",
    [int]$MaxJobs = 1,
    [int]$PauseBetweenJobsSeconds = 20,
    [switch]$ForceRefresh,
    [string]$CardName = "Charizard ex",
    [string]$CollectorNumber = "125/197",
    [string]$SetName = "Obsidian Flames",
    [string]$SetCode = "sv03",
    [string]$Condition = "raw",
    [string]$Variant = "raw"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

$writeConfirm = [Environment]::GetEnvironmentVariable("CONFIRM_LIVE_EBAY_WRITE")
if ([string]::IsNullOrWhiteSpace($writeConfirm) -or $writeConfirm.ToLowerInvariant() -ne "true") {
    throw "CONFIRM_LIVE_EBAY_WRITE=true is required for the live worker batch."
}
$workerConfirm = [Environment]::GetEnvironmentVariable("CONFIRM_LIVE_EBAY_WORKER")
if ([string]::IsNullOrWhiteSpace($workerConfirm) -or $workerConfirm.ToLowerInvariant() -ne "true") {
    throw "CONFIRM_LIVE_EBAY_WORKER=true is required for the live worker batch."
}

if ($Markets -is [array]) {
    $marketsArg = (($Markets | ForEach-Object { "$_".Trim() }) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ","
} else {
    $marketsArg = "$Markets".Trim()
    if ($marketsArg -notlike "*,*" -and $marketsArg -match "\s+") {
        $marketsArg = (($marketsArg -split "\s+") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ","
    }
}

$profileDir = Join-Path $repoRoot ".browser_profiles\cardscanr"
$env:MARKET_LOOKUP_PROVIDER = "ebay_browser"
$env:ENABLE_EBAY_REAL_LOOKUP = "true"
$env:EBAY_BROWSER_ENGINE = "chrome"
$env:EBAY_BROWSER_CHANNEL = "chrome"
$env:EBAY_BROWSER_PROFILE_NAME = "cardscanr"
$env:EBAY_BROWSER_USER_DATA_DIR = $profileDir
$env:EBAY_MARKET_SCOPE = "marketplace"

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @(
    "scripts/smoke_ebay_browser_live_worker_batch.py",
    "--markets", $marketsArg,
    "--max-jobs", [string]$MaxJobs,
    "--pause-between-jobs-seconds", [string]$PauseBetweenJobsSeconds,
    "--card-name", $CardName,
    "--collector-number", $CollectorNumber,
    "--set-name", $SetName,
    "--set-code", $SetCode,
    "--condition", $Condition,
    "--variant", $Variant
)
if ($ForceRefresh) {
    $argsList += "--force-refresh"
}

Write-Host "[market-engine] Running controlled live eBay worker batch with Chrome profile: $profileDir"
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "smoke_ebay_browser_live_worker_batch.py failed with exit code $LASTEXITCODE"
}

try {
    $bundleScript = Join-Path $repoRoot "scripts\create_market_engine_upload_bundle.ps1"
    & $bundleScript -Kind ebay_browser_live_worker_batch -Output "reports\chatgpt_uploads\ebay_browser_live_worker_batch_latest.zip"
    if ($LASTEXITCODE -ne 0) {
        throw "bundle script exited with code $LASTEXITCODE"
    }
} catch {
    Write-Warning "Upload bundle creation failed: $($_.Exception.Message)"
    Write-Host "Create it manually with:"
    Write-Host ".\scripts\create_market_engine_upload_bundle.ps1 -Kind ebay_browser_live_worker_batch -Output reports\chatgpt_uploads\ebay_browser_live_worker_batch_latest.zip"
}
