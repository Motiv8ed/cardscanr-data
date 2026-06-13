param(
    [switch]$Headed,
    [switch]$Headless,
    [string]$ProfilePath = "",
    [string]$EnvFile = "supabase_env.local.json"
)

$ErrorActionPreference = "Stop"

if ($Headed -and $Headless) {
    throw "Choose either -Headed or -Headless, not both."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$configScript = Join-Path $repoRoot "scripts\live_ebay_worker_config.ps1"
if (-not (Test-Path $configScript)) {
    throw "Live eBay worker config helper not found: $configScript"
}
. $configScript

foreach ($requiredPath in @(
    "workers\market_price_worker.py",
    "scripts\run_market_price_worker.ps1",
    "scripts\start_live_ebay_worker.ps1",
    "scripts\load_supabase_env.ps1",
    "scripts\check_live_ebay_worker_config.py"
)) {
    $resolvedPath = Join-Path $repoRoot $requiredPath
    if (-not (Test-Path $resolvedPath)) {
        throw "Required live worker entrypoint file not found: $resolvedPath"
    }
}

if ([string]::IsNullOrWhiteSpace($ProfilePath)) {
    $ProfilePath = Join-Path $repoRoot ".browser_profiles\cardscanr"
}
$safeProfilePath = Assert-SafeLiveEbayProfilePath -ProfilePath $ProfilePath
$useHeadless = -not $Headed

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
. $envLoader -EnvFile $EnvFile

Set-LiveEbayWorkerEnvironment -ProfilePath $safeProfilePath -Headless $useHeadless
New-Item -ItemType Directory -Force -Path $safeProfilePath | Out-Null
Write-LiveEbayWorkerConfigSummary -ProfilePath $safeProfilePath -Headless $useHeadless -PollSeconds 5 -MaxJobs 1
Write-Host "[live-ebay-config] Dedicated Chrome profile directory: ok"

$pythonPath = Resolve-CardScanRPythonPath -RepoRoot $repoRoot
$chromeProbe = Join-Path $repoRoot "scripts\check_live_ebay_worker_config.py"

Write-Host "[live-ebay-config] Checking env, Python deps, Playwright, and installed Chrome without opening eBay..."
& $pythonPath $chromeProbe
if ($LASTEXITCODE -ne 0) {
    throw "Live eBay config check failed with exit code $LASTEXITCODE"
}
Write-Host "[live-ebay-config] Config check complete. No eBay lookup ran and no Supabase jobs were claimed."
