param(
    [object]$Markets = "AU,US,GB,CA",
    [string]$CardName = "Charizard ex",
    [string]$CollectorNumber = "125/197",
    [string]$SetName = "Obsidian Flames",
    [int]$MaxResults = 30,
    [int]$PauseBetweenMarketsSeconds = 20,
    [switch]$Headed
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

$profileDir = Join-Path $repoRoot ".browser_profiles\cardscanr"
$env:MARKET_LOOKUP_PROVIDER = "ebay_browser"
$env:ENABLE_EBAY_REAL_LOOKUP = "true"
$env:EBAY_BROWSER_ENGINE = "chrome"
$env:EBAY_BROWSER_CHANNEL = "chrome"
$env:EBAY_BROWSER_PROFILE_NAME = "cardscanr"
$env:EBAY_BROWSER_USER_DATA_DIR = $profileDir
$env:EBAY_MARKET_SCOPE = "marketplace"
$env:EBAY_BROWSER_HEADLESS = if ($Headed) { "false" } else { "true" }

if ($Markets -is [array]) {
    $marketsArg = (($Markets | ForEach-Object { "$_".Trim() }) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ","
} else {
    $marketsArg = "$Markets".Trim()
    if ($marketsArg -notlike "*,*" -and $marketsArg -match "\s+") {
        $marketsArg = (($marketsArg -split "\s+") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ","
    }
}

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @(
    "scripts/debug_ebay_browser_market_matrix.py",
    "--markets", $marketsArg,
    "--card-name", $CardName,
    "--collector-number", $CollectorNumber,
    "--set-name", $SetName,
    "--max-results", [string]$MaxResults,
    "--pause-between-markets-seconds", [string]$PauseBetweenMarketsSeconds
)
if ($Headed) {
    $argsList += "--headed"
}

Write-Host "[market-engine] Running eBay browser market matrix with Chrome profile: $profileDir"
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "debug_ebay_browser_market_matrix.py failed with exit code $LASTEXITCODE"
}

try {
    $bundleScript = Join-Path $repoRoot "scripts\create_market_engine_upload_bundle.ps1"
    & $bundleScript -Kind ebay_browser_market_matrix -Output "reports\chatgpt_uploads\ebay_browser_market_matrix_latest.zip"
    if ($LASTEXITCODE -ne 0) {
        throw "bundle script exited with code $LASTEXITCODE"
    }
} catch {
    Write-Warning "Upload bundle creation failed: $($_.Exception.Message)"
    Write-Host "Create it manually with:"
    Write-Host ".\scripts\create_market_engine_upload_bundle.ps1 -Kind ebay_browser_market_matrix -Output reports\chatgpt_uploads\ebay_browser_market_matrix_latest.zip"
}
