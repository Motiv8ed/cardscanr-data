param(
    [switch]$Once,
    [int]$MaxCycles = 0,
    [int]$MaxJobs = 0,
    [int]$PollSeconds = 0
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @("workers/market_price_worker.py")
if ($Once) { $argsList += "--once" }
if ($MaxCycles -gt 0) { $argsList += @("--max-cycles", [string]$MaxCycles) }
if ($MaxJobs -gt 0) { $argsList += @("--max-jobs", [string]$MaxJobs) }
if ($PollSeconds -gt 0) { $argsList += @("--poll-seconds", [string]$PollSeconds) }

Write-Host "[market-engine] Running mock market price worker..."
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "market_price_worker.py failed with exit code $LASTEXITCODE"
}
