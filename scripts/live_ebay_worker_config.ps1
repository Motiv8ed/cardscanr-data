function Resolve-CardScanRPythonPath {
    param([Parameter(Mandatory = $true)][string]$RepoRoot)

    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }
    return "python"
}

function Assert-SafeLiveEbayProfilePath {
    param([Parameter(Mandatory = $true)][string]$ProfilePath)

    if ([string]::IsNullOrWhiteSpace($ProfilePath)) {
        throw "Chrome profile path must not be empty."
    }

    $fullPath = [System.IO.Path]::GetFullPath($ProfilePath)
    $normalized = $fullPath.Replace("/", "\").TrimEnd("\")
    if ($normalized -match "(?i)\\AppData\\Local\\Google\\Chrome\\User Data(?:\\|$)") {
        throw "Refusing personal Chrome profile path: $fullPath. Use the dedicated CardScanR profile under .browser_profiles\cardscanr."
    }
    return $fullPath
}

function Set-LiveEbayWorkerEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$ProfilePath,
        [Parameter(Mandatory = $true)][bool]$Headless
    )

    [Environment]::SetEnvironmentVariable("MARKET_LOOKUP_PROVIDER", "ebay_browser", "Process")
    [Environment]::SetEnvironmentVariable("ENABLE_EBAY_REAL_LOOKUP", "true", "Process")
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_ENGINE", "chrome", "Process")
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_CHANNEL", "chrome", "Process")
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_PROFILE_NAME", "cardscanr", "Process")
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_USER_DATA_DIR", $ProfilePath, "Process")
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_HEADLESS", $Headless.ToString().ToLowerInvariant(), "Process")
    [Environment]::SetEnvironmentVariable("EBAY_MARKET_SCOPE", "marketplace", "Process")
    [Environment]::SetEnvironmentVariable("CONFIRM_LIVE_EBAY_WORKER", "true", "Process")
    [Environment]::SetEnvironmentVariable("MARKET_WORKER_CONCURRENCY", "1", "Process")
}

function Write-LiveEbayWorkerConfigSummary {
    param(
        [Parameter(Mandatory = $true)][string]$ProfilePath,
        [Parameter(Mandatory = $true)][bool]$Headless,
        [Parameter(Mandatory = $true)][int]$PollSeconds,
        [Parameter(Mandatory = $true)][int]$MaxJobs
    )

    Write-Host "[live-ebay-worker] Safe local worker config"
    Write-Host "  provider=ebay_browser"
    Write-Host "  realLookupEnabled=true"
    Write-Host "  chromeEngine=chrome"
    Write-Host "  chromeChannel=chrome"
    Write-Host "  chromeProfileName=cardscanr"
    Write-Host "  chromeProfilePath=$ProfilePath"
    Write-Host "  chromeHeadless=$($Headless.ToString().ToLowerInvariant())"
    Write-Host "  pollSeconds=$PollSeconds"
    Write-Host "  maxJobsPerCycle=$MaxJobs"
    Write-Host "  concurrency=1"
    Write-Host "  schedulerStarted=false"
    Write-Host "  forceRefresh=false"
    Write-Host "  supabaseSecrets=<not shown>"
}
