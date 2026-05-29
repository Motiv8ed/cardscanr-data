param()

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# Load Supabase env if not already set
$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

$requiredVars = @("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
$missing = @()
foreach ($name in $requiredVars) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        $missing += $name
    }
}

$provider = [Environment]::GetEnvironmentVariable("MARKET_LOOKUP_PROVIDER")
if ([string]::IsNullOrWhiteSpace($provider)) {
    $provider = "mock"
}
if ($provider.ToLowerInvariant() -ne "mock") {
    throw "MARKET_LOOKUP_PROVIDER must be 'mock' for smoke tests. Current value: '$provider'"
}

if ($missing.Count -gt 0) {
    throw ("Missing required environment variables: " + ($missing -join ", "))
}

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

Write-Host "[market-engine-smoke] Running smoke test against Supabase in mock provider mode..."
& $pythonPath "scripts/smoke_market_price_engine.py"
if ($LASTEXITCODE -ne 0) {
    throw "smoke_market_price_engine.py failed with exit code $LASTEXITCODE"
}

try {
    $bundleScript = Join-Path $repoRoot "scripts\create_market_engine_upload_bundle.ps1"
    & $bundleScript -Kind market_price_engine_smoke
    if ($LASTEXITCODE -ne 0) {
        throw "bundle script exited with code $LASTEXITCODE"
    }
} catch {
    Write-Warning "Upload bundle creation failed: $($_.Exception.Message)"
    Write-Host "Create it manually with:"
    Write-Host ".\scripts\create_market_engine_upload_bundle.ps1 -Kind market_price_engine_smoke"
}
