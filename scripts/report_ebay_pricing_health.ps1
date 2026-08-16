param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

& $pythonPath "scripts/report_ebay_pricing_health.py"
exit $LASTEXITCODE
