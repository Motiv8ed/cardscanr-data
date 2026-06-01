[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Headed,
    [switch]$Headless,
    [ValidateRange(1, 86400)][int]$PollSeconds = 5,
    [ValidateRange(1, 100)][int]$MaxJobs = 1,
    [switch]$Once,
    [switch]$DryRun,
    [string]$ProfilePath = ""
)

$ErrorActionPreference = "Stop"

if ($Headed -and $Headless) {
    throw "Choose either -Headed or -Headless, not both."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
. (Join-Path $repoRoot "scripts\live_ebay_worker_config.ps1")

if ([string]::IsNullOrWhiteSpace($ProfilePath)) {
    $ProfilePath = Join-Path $repoRoot ".browser_profiles\cardscanr"
}
$safeProfilePath = Assert-SafeLiveEbayProfilePath -ProfilePath $ProfilePath
$useHeadless = -not $Headed

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (-not (Test-Path $envLoader)) {
    throw "Supabase env loader not found: $envLoader"
}
. $envLoader

Set-LiveEbayWorkerEnvironment -ProfilePath $safeProfilePath -Headless $useHeadless
Write-LiveEbayWorkerConfigSummary -ProfilePath $safeProfilePath -Headless $useHeadless -PollSeconds $PollSeconds -MaxJobs $MaxJobs

if ($DryRun -or $WhatIfPreference) {
    Write-Host "[live-ebay-worker] Dry run only. Worker was not started and no jobs were claimed."
    return
}

New-Item -ItemType Directory -Force -Path $safeProfilePath | Out-Null
$workerScript = Join-Path $repoRoot "scripts\run_market_price_worker.ps1"
$workerArgs = @{
    PollSeconds = $PollSeconds
    MaxJobs = $MaxJobs
}
if ($Once) {
    $workerArgs.Once = $true
}

if ($PSCmdlet.ShouldProcess("queued Supabase market price jobs", "Start conservative local live eBay worker")) {
    Write-Host "[live-ebay-worker] Starting worker. Stop it with Ctrl+C."
    & $workerScript @workerArgs
}
