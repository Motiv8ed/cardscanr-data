param(
    [string]$Market = "AU",
    [string]$Currency = "AUD",
    [string]$CardName = "Charizard ex",
    [string]$CollectorNumber = "125/197",
    [string]$SetName = "Obsidian Flames",
    [string]$SetCode = "sv03",
    [string]$Condition = "raw",
    [string]$Variant = "raw",
    [switch]$ForceRefresh
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

$confirm = [Environment]::GetEnvironmentVariable("CONFIRM_LIVE_EBAY_WRITE")
if ([string]::IsNullOrWhiteSpace($confirm) -or $confirm.ToLowerInvariant() -ne "true") {
    throw "CONFIRM_LIVE_EBAY_WRITE=true is required for the live write smoke."
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
    "scripts/smoke_ebay_browser_live_write.py",
    "--market", $Market,
    "--currency", $Currency,
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

Write-Host "[market-engine] Running one-card live eBay write smoke with Chrome profile: $profileDir"
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "smoke_ebay_browser_live_write.py failed with exit code $LASTEXITCODE"
}

try {
    $bundleScript = Join-Path $repoRoot "scripts\create_market_engine_upload_bundle.ps1"
    & $bundleScript -Kind ebay_browser_live_write_smoke
    if ($LASTEXITCODE -ne 0) {
        throw "bundle script exited with code $LASTEXITCODE"
    }
} catch {
    Write-Warning "Upload bundle creation failed: $($_.Exception.Message)"
    Write-Host "Create it manually with:"
    Write-Host ".\scripts\create_market_engine_upload_bundle.ps1 -Kind ebay_browser_live_write_smoke"
}
