param(
    [string]$Market = "AU",
    [string]$Currency = "AUD",
    [Parameter(Mandatory = $true)][string]$CardName,
    [Parameter(Mandatory = $true)][string]$CollectorNumber,
    [Parameter(Mandatory = $true)][string]$SetName,
    [string]$SetCode = "",
    [string]$Language = "en",
    [string]$Variant = "raw",
    [string]$Condition = "raw"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

$provider = [Environment]::GetEnvironmentVariable("MARKET_LOOKUP_PROVIDER")
$enabled = [Environment]::GetEnvironmentVariable("ENABLE_EBAY_REAL_LOOKUP")
if ([string]::IsNullOrWhiteSpace($provider) -or $provider.ToLowerInvariant() -ne "ebay_browser") {
    throw "MARKET_LOOKUP_PROVIDER must be 'ebay_browser' for the debug provider."
}
if ([string]::IsNullOrWhiteSpace($enabled) -or $enabled.ToLowerInvariant() -ne "true") {
    throw "ENABLE_EBAY_REAL_LOOKUP must be 'true' for the debug provider."
}

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @(
    "scripts/debug_ebay_browser_provider.py",
    "--market", $Market,
    "--currency", $Currency,
    "--card-name", $CardName,
    "--collector-number", $CollectorNumber,
    "--set-name", $SetName,
    "--language", $Language,
    "--variant", $Variant,
    "--condition", $Condition
)
if (-not [string]::IsNullOrWhiteSpace($SetCode)) {
    $argsList += @("--set-code", $SetCode)
}

Write-Host "[market-engine] Running one local eBay browser provider lookup..."
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "debug_ebay_browser_provider.py failed with exit code $LASTEXITCODE"
}
