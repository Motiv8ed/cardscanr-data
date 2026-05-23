param(
    [switch]$NoFetch,
    [switch]$UntilComplete,
    [int]$MaxRequestsPerHour = 0,
    [int]$MaxRequestsPerDay = 0,
    [string]$Languages = "en,jp",
    [switch]$IncludeZh,
    [switch]$BuildAppCatalogue,
    [switch]$SkipAppCatalogue,
    [switch]$BuildImages,
    [switch]$SkipImages,
    [switch]$DownloadImages,
    [switch]$BuildPrices,
    [switch]$SkipPrices,
    [switch]$BuildHistory,
    [switch]$SkipHistory,
    [switch]$Validate,
    [switch]$SkipValidate,
    [switch]$Commit,
    [switch]$DryRun,
    [switch]$ExportChatGPTReport
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $pythonPath)) {
    $pythonPath = 'python'
}

$argsList = @('tools/run_full_data_pipeline.py', '--languages', $Languages)

if ($NoFetch) { $argsList += '--no-fetch' }
if ($UntilComplete) { $argsList += '--until-complete' }
if ($MaxRequestsPerHour -gt 0) { $argsList += @('--max-requests-per-hour', [string]$MaxRequestsPerHour) }
if ($MaxRequestsPerDay -gt 0) { $argsList += @('--max-requests-per-day', [string]$MaxRequestsPerDay) }
if ($IncludeZh) { $argsList += '--include-zh' }
if ($SkipAppCatalogue -or ($PSBoundParameters.ContainsKey('BuildAppCatalogue') -and -not $BuildAppCatalogue)) { $argsList += '--skip-app-catalogue' }
if ($SkipImages -or ($PSBoundParameters.ContainsKey('BuildImages') -and -not $BuildImages)) { $argsList += '--skip-images' }
if ($DownloadImages) { $argsList += '--download-images' }
if ($SkipPrices -or ($PSBoundParameters.ContainsKey('BuildPrices') -and -not $BuildPrices)) { $argsList += '--skip-prices' }
if ($SkipHistory -or ($PSBoundParameters.ContainsKey('BuildHistory') -and -not $BuildHistory)) { $argsList += '--skip-history' }
if ($SkipValidate -or ($PSBoundParameters.ContainsKey('Validate') -and -not $Validate)) { $argsList += '--skip-validate' }
if ($Commit) { $argsList += '--commit' }
if ($DryRun) { $argsList += '--dry-run' }

& $pythonPath @argsList
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if ($ExportChatGPTReport) {
    Write-Host "[pipeline] Generating ChatGPT upload report..."
    & $pythonPath "tools/export_chatgpt_report.py"
}
