param()

$ErrorActionPreference = "Stop"

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

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

Write-Host "[market-engine-smoke] Running smoke test against Supabase in mock provider mode..."
& $pythonPath "scripts/smoke_market_price_engine.py"
if ($LASTEXITCODE -ne 0) {
    throw "smoke_market_price_engine.py failed with exit code $LASTEXITCODE"
}
