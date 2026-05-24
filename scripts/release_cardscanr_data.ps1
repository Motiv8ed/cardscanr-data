param(
    [switch]$Push,
    [switch]$DryRun,
    [switch]$IncludeDocs,
    [switch]$IncludeReports,
    [string]$Languages = "en,jp",
    [switch]$IncludeZh,
    [switch]$ExportChatGPTReport
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "[release] $Label"
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Normalize-GitPath {
    param([string]$Path)

    $normalized = ($Path -replace "\\", "/").Trim()
    if ($normalized -match "^.+ -> (.+)$") {
        return ($Matches[1]).Trim()
    }
    return $normalized
}

function Get-PathBlockReason {
    param(
        [string]$Path,
        [bool]$AllowDocs,
        [bool]$AllowReports
    )

    if (-not $Path) {
        return "empty"
    }

    $lower = $Path.ToLowerInvariant()

    if ($lower.StartsWith(".cache/") -or $lower.Contains("/.cache/") -or $lower.EndsWith(".cache")) {
        return ".cache"
    }
    if ($lower.EndsWith(".tmp") -or $lower.Contains("/.tmp/")) {
        return ".tmp"
    }
    if ($lower.StartsWith("logs/")) {
        return "runtime_logs"
    }
    if ($lower.StartsWith(".github/")) {
        return "out_of_release_scope"
    }
    if ($lower -match "(?i)(secret|password|credential|private[_-]?key|token|api[_-]?key)") {
        return "secret_like_path"
    }
    if ($lower -match "(?i)\.(env|pem|p12|pfx|key)$") {
        return "secret_like_extension"
    }
    if ($lower.StartsWith("reports/") -and ($lower -like "reports/latest_full_data_pipeline.*" -or $lower -like "reports/latest_pokewallet_worker_cycle.*")) {
        return "runtime_report"
    }
    if ($lower -eq "data/pokewallet_price_request_ledger.json") {
        return "runtime_budget_ledger"
    }
    if ($lower -match "(?i)\.(png|jpg|jpeg|webp|gif)$") {
        return "local_image_binary"
    }

    if ($lower.StartsWith("public/v1/")) {
        return ""
    }
    if ($lower.StartsWith("data/") -and $lower.EndsWith(".json")) {
        return ""
    }
    if ($AllowDocs -and $lower.StartsWith("docs/")) {
        return ""
    }
    if ($AllowReports -and $lower.StartsWith("reports/")) {
        return ""
    }

    return "not_in_allowed_release_paths"
}

Write-Host "[release] Repository status (before release):"
git status --short
git status -sb

Invoke-CheckedCommand -Label "report_data_health" -Exe $pythonPath -Arguments @("tools/report_data_health.py")
Invoke-CheckedCommand -Label "report_dataset_coverage" -Exe $pythonPath -Arguments @("tools/report_dataset_coverage.py")
Invoke-CheckedCommand -Label "report_provider_to_app_gap" -Exe $pythonPath -Arguments @("tools/report_provider_to_app_gap.py", "--summary-only")
Invoke-CheckedCommand -Label "validate_cache" -Exe $pythonPath -Arguments @("tools/validate_cache.py")
Invoke-CheckedCommand -Label "report_en_current_price_migration" -Exe $pythonPath -Arguments @("tools/report_en_current_price_migration.py")

$summaryArgs = @("tools/release_cardscanr_data.py", "--languages", $Languages)
if ($IncludeZh) {
    $summaryArgs += "--include-zh"
}

$summaryJson = & $pythonPath @summaryArgs
if ($LASTEXITCODE -ne 0) {
    throw "release summary generation failed"
}
$summary = $summaryJson | ConvertFrom-Json

Write-Host "[release] Summary"
Write-Host ("  app catalogue by language: " + (($summary.appCatalogue.byLanguage | ConvertTo-Json -Compress)))
Write-Host ("  image manifest by language: " + (($summary.imageManifest.byLanguage | ConvertTo-Json -Compress)))
Write-Host ("  provider by language: " + (($summary.providerCatalogue.byLanguage | ConvertTo-Json -Compress)))
Write-Host ("  EN price count: " + [string]$summary.prices.en)
Write-Host ("  JP price count: " + [string]$summary.prices.jp)
Write-Host ("  blocked by reason: " + (($summary.blockedRecordsByReason | ConvertTo-Json -Compress)))
Write-Host ("  local cached image count: " + [string]$summary.localCachedImageCount)

$statusLines = @(git status --porcelain)
$changedPaths = @()
foreach ($line in $statusLines) {
    if (-not $line) { continue }
    if ($line.Length -lt 4) { continue }
    $rawPath = $line.Substring(3)
    if (-not $rawPath) { continue }
    $changedPaths += (Normalize-GitPath -Path $rawPath)
}
$changedPaths = $changedPaths | Where-Object { $_ } | Sort-Object -Unique

$stagePaths = @()
$refused = @()
foreach ($path in $changedPaths) {
    $reason = Get-PathBlockReason -Path $path -AllowDocs:$IncludeDocs -AllowReports:$IncludeReports
    if ([string]::IsNullOrEmpty($reason)) {
        $stagePaths += $path
    }
    else {
        $refused += [PSCustomObject]@{ path = $path; reason = $reason }
    }
}

Write-Host "[release] Stage candidates"
if ($stagePaths.Count -eq 0) {
    Write-Host "  none"
}
else {
    foreach ($path in $stagePaths) {
        Write-Host "  + $path"
    }
}

Write-Host "[release] Refused paths"
if ($refused.Count -eq 0) {
    Write-Host "  none"
}
else {
    foreach ($item in $refused) {
        Write-Host ("  - " + $item.path + " (" + $item.reason + ")")
    }
}

if ($DryRun) {
    Write-Host "[release] Dry-run mode enabled. Skipping stage, commit, and push."
    if ($ExportChatGPTReport) {
        Write-Host "[release] Generating ChatGPT upload report..."
        & $pythonPath "tools/export_chatgpt_report.py"
    }
    exit 0
}

if ($stagePaths.Count -eq 0) {
    Write-Host "[release] No allowed changed paths to stage."
    exit 0
}

foreach ($path in $stagePaths) {
    git add -- $path
    if ($LASTEXITCODE -ne 0) {
        throw "failed to stage $path"
    }
}

git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "[release] No meaningful staged changes after filtering."
    exit 0
}

$catalogueTotal = [int]$summary.appCatalogue.total
$imageTotal = [int]$summary.imageManifest.total
$enPrices = [int]$summary.prices.en
$jpPrices = [int]$summary.prices.jp
$commitMessage = "Update CardScanR data: catalogue $catalogueTotal, images $imageTotal, EN prices $enPrices, JP prices $jpPrices"

Write-Host "[release] Commit message: $commitMessage"
git commit -m $commitMessage
if ($LASTEXITCODE -ne 0) {
    throw "commit failed"
}

if ($Push) {
    Write-Host "[release] Pushing commit to origin/HEAD"
    git push origin HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "push failed"
    }
}
else {
    Write-Host "[release] Push skipped. Use -Push to push this release commit."
}

if ($ExportChatGPTReport) {
    Write-Host "[release] Generating ChatGPT upload report..."
    & $pythonPath "tools/export_chatgpt_report.py"
}
