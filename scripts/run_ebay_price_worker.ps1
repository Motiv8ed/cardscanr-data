[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Headed,
    [switch]$Headless,
    [ValidateRange(1, 86400)][int]$PollSeconds = 5,
    [ValidateRange(1, 100)][int]$MaxJobs = 1,
    [switch]$Once,
    [switch]$DryRun,
    [switch]$SkipConfigCheck,
    [string]$ProfilePath = ""
)

$ErrorActionPreference = "Stop"

if ($Headed -and $Headless) {
    throw "Choose either -Headed or -Headless, not both."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host ""
Write-Host "CardScanR eBay price worker"
Write-Host "============================"
Write-Host "Run this script when the app should process queued eBay price updates from scans or manual refreshes."
Write-Host "It keeps the live eBay worker local, guarded, and limited to one job at a time by default."
Write-Host ""

$checkScript = Join-Path $repoRoot "scripts\check_live_ebay_worker_config.ps1"
if (-not $SkipConfigCheck) {
    if (-not (Test-Path $checkScript)) {
        throw "Config check script not found: $checkScript"
    }

    Write-Host "[ebay-price-worker] Checking local Chrome/Playwright/Supabase setup first..."
    $checkArgs = @()
    if ($Headed) { $checkArgs += "-Headed" }
    if ($Headless) { $checkArgs += "-Headless" }
    if (-not [string]::IsNullOrWhiteSpace($ProfilePath)) {
        $checkArgs += @("-ProfilePath", $ProfilePath)
    }
    & $checkScript @checkArgs
    Write-Host ""
}

$workerScript = Join-Path $repoRoot "scripts\start_live_ebay_worker.ps1"
if (-not (Test-Path $workerScript)) {
    throw "Live eBay worker script not found: $workerScript"
}

$workerArgs = @{
    PollSeconds = $PollSeconds
    MaxJobs = $MaxJobs
}
if ($Headed) { $workerArgs.Headed = $true }
if ($Headless) { $workerArgs.Headless = $true }
if ($Once) { $workerArgs.Once = $true }
if ($DryRun) { $workerArgs.DryRun = $true }
if (-not [string]::IsNullOrWhiteSpace($ProfilePath)) {
    $workerArgs.ProfilePath = $ProfilePath
}
if ($WhatIfPreference) {
    $workerArgs.WhatIf = $true
}

Write-Host "[ebay-price-worker] Starting guarded live eBay worker entrypoint..."
& $workerScript @workerArgs
