param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("ebay_browser_market_matrix", "ebay_browser_debug", "ebay_browser_live_write_smoke", "ebay_browser_live_worker_batch", "market_price_engine_smoke")]
    [string]$Kind,
    [switch]$IncludeHtml,
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @(
    "scripts/create_market_engine_upload_bundle.py",
    "--kind", $Kind
)
if ($IncludeHtml) {
    $argsList += "--include-html"
}
if (-not [string]::IsNullOrWhiteSpace($Output)) {
    $argsList += @("--output", $Output)
}

$bundlePath = & $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "create_market_engine_upload_bundle.py failed with exit code $LASTEXITCODE"
}

Write-Host "Upload this file to ChatGPT:"
Write-Host $bundlePath
