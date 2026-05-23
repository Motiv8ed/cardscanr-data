param(
    [switch]$OpenFolder,
    [switch]$IncludeLargeReports,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @("tools/export_chatgpt_report.py")
if ($IncludeLargeReports) { $argsList += "--include-large-reports" }
if ($NoZip) { $argsList += "--no-zip" }

Write-Host "[export] Generating ChatGPT upload report..."
& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    throw "export_chatgpt_report.py failed with exit code $LASTEXITCODE"
}

$exportDir = Join-Path $repoRoot "reports\chatgpt_exports"
Write-Host ""
Write-Host "[export] Export folder: $exportDir"

if ($OpenFolder) {
    if (Test-Path $exportDir) {
        Write-Host "[export] Opening export folder in Explorer..."
        explorer.exe $exportDir
    }
    else {
        Write-Host "[export] Export folder not found: $exportDir"
    }
}

Write-Host "[export] Done. Upload the .zip (or .md) from the folder above to ChatGPT."
