param(
    [switch]$Once,
    [int]$MaxCycles = 0,
    [int]$PollSeconds = 0
)

$ErrorActionPreference = "Stop"

$requiredVars = @("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
$missing = @()
foreach ($name in $requiredVars) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        $missing += $name
    }
}
if ($missing.Count -gt 0) {
    throw ("Missing required environment variables: " + ($missing -join ", "))
}

$provider = [Environment]::GetEnvironmentVariable("MARKET_LOOKUP_PROVIDER")
if (-not [string]::IsNullOrWhiteSpace($provider) -and $provider.ToLowerInvariant() -ne "mock") {
    throw "MARKET_LOOKUP_PROVIDER must be 'mock' when set. Current value: '$provider'"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @("workers/market_price_scheduler.py")
if ($Once) { $argsList += "--once" }
if ($MaxCycles -gt 0) { $argsList += @("--max-cycles", [string]$MaxCycles) }
if ($PollSeconds -gt 0) { $argsList += @("--poll-seconds", [string]$PollSeconds) }

Write-Host "[market-scheduler] Running market price refresh scheduler..."
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "market_price_scheduler.py failed with exit code $LASTEXITCODE"
}
