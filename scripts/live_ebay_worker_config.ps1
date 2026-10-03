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
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_ENABLED", "true", "Process")
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_ENGINE", "chrome", "Process")
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_CHANNEL", "chrome", "Process")
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_PROFILE_NAME", "cardscanr", "Process")
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_USER_DATA_DIR", $ProfilePath, "Process")
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_HEADLESS", $Headless.ToString().ToLowerInvariant(), "Process")
    # Intentional production mode for AU sold (current eBay behaviour rejects headless sold UI).
    $mode = if ($Headless) { "headless" } else { "headed" }
    [Environment]::SetEnvironmentVariable("EBAY_BROWSER_MODE", $mode, "Process")
    [Environment]::SetEnvironmentVariable("EBAY_MARKET_SCOPE", "marketplace", "Process")
    [Environment]::SetEnvironmentVariable("CONFIRM_LIVE_EBAY_WORKER", "true", "Process")
    [Environment]::SetEnvironmentVariable("MARKET_WORKER_CONCURRENCY", "1", "Process")
    # Never silently price AU jobs from US/UK/CA (or vice versa).
    [Environment]::SetEnvironmentVariable("MARKET_EBAY_FALLBACK_MARKETPLACES", "", "Process")
    # All four proven browser markets. Challenge deferral is empty unless explicitly set.
    [Environment]::SetEnvironmentVariable("MARKET_WORKER_ALLOWED_MARKETS", "AU,US,GB,CA", "Process")
    [Environment]::SetEnvironmentVariable("MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS", "NONE", "Process")
    # Persistent-context reuse has been flaky (TargetClosedError) on this host; keep serial launches.
    if (-not $env:EBAY_BROWSER_REUSE_CONTEXT) {
        [Environment]::SetEnvironmentVariable("EBAY_BROWSER_REUSE_CONTEXT", "false", "Process")
    }
    # Daily owned-card pricing flag must be set explicitly / via persistent flag.
    # Do not default to full enable (2026-09-27 precedence incident).
    if (-not $env:OWNED_DAILY_FULL_ENABLE) {
        [Environment]::SetEnvironmentVariable("OWNED_DAILY_FULL_ENABLE", "false", "Process")
    }
    if (-not $env:OWNED_DAILY_MAX_ENQUEUE) {
        [Environment]::SetEnvironmentVariable("OWNED_DAILY_MAX_ENQUEUE", "25", "Process")
    }
}

function Write-LiveEbayWorkerConfigSummary {
    param(
        [Parameter(Mandatory = $true)][string]$ProfilePath,
        [Parameter(Mandatory = $true)][bool]$Headless,
        [Parameter(Mandatory = $true)][int]$PollSeconds,
        [Parameter(Mandatory = $true)][int]$MaxJobs
    )

    $mode = if ($Headless) { "headless" } else { "headed" }
    Write-Host "[live-ebay-worker] Safe local worker config"
    Write-Host "  provider=ebay_browser"
    Write-Host "  realLookupEnabled=true"
    Write-Host "  chromeEngine=chrome"
    Write-Host "  chromeChannel=chrome"
    Write-Host "  chromeProfileName=cardscanr"
    Write-Host "  chromeProfilePath=$ProfilePath"
    Write-Host "  EBAY_BROWSER_MODE=$mode"
    Write-Host "  chromeHeadless=$($Headless.ToString().ToLowerInvariant())"
    Write-Host "  pollSeconds=$PollSeconds"
    Write-Host "  maxJobsPerCycle=$MaxJobs"
    Write-Host "  concurrency=1"
    Write-Host "  schedulerStarted=false"
    Write-Host "  forceRefresh=false"
    Write-Host "  supabaseSecrets=<not shown>"
}

function Get-OwnedDailyFullEnableFlagPath {
    param([Parameter(Mandatory = $true)][string]$StateDir)
    return (Join-Path $StateDir "owned_daily_full_enable.flag")
}

function Read-OwnedDailyFullEnableFlag {
    param([Parameter(Mandatory = $true)][string]$StateDir)

    $flagPath = Get-OwnedDailyFullEnableFlagPath -StateDir $StateDir
    if (-not (Test-Path $flagPath)) {
        return $false
    }
    $raw = (Get-Content -Raw -Path $flagPath).Trim().ToLowerInvariant()
    return ($raw -in @("1", "true", "yes", "on"))
}

function Set-OwnedDailyFullEnableFlag {
    <#
    .SYNOPSIS
      Sole writer for OWNED_DAILY_FULL_ENABLE persistence.
      Ensure/runtime scripts must READ this flag; they must never invent true.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][bool]$Enabled,
        [string]$Reason = "explicit_control_plane"
    )

    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    $flagPath = Get-OwnedDailyFullEnableFlagPath -StateDir $StateDir
    $value = $Enabled.ToString().ToLowerInvariant()
    Set-Content -Path $flagPath -Value $value -Encoding ascii -NoNewline
    $auditPath = Join-Path $StateDir "owned_daily_full_enable_audit.jsonl"
    $line = (@{
        atUtc = (Get-Date).ToUniversalTime().ToString("o")
        value = $value
        reason = $Reason
    } | ConvertTo-Json -Compress)
    Add-Content -Path $auditPath -Value $line -Encoding utf8
    return $flagPath
}

function Write-LiveEbayRuntimeConfig {
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][string]$ProfilePath,
        [Parameter(Mandatory = $true)][bool]$Headless,
        [Parameter(Mandatory = $true)][bool]$OwnedDailyFullEnable,
        [int]$OwnedDailyMaxEnqueue = 50
    )

    $mode = if ($Headless) { "headless" } else { "headed" }
    # Mirror only. Never rewrite owned_daily_full_enable.flag here — that file is
    # the sole persistent authority and is mutated only via Set-OwnedDailyFullEnableFlag.
    $payload = @{
        generatedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
        EBAY_BROWSER_MODE = $mode
        EBAY_BROWSER_HEADLESS = $Headless.ToString().ToLowerInvariant()
        EBAY_BROWSER_USER_DATA_DIR = $ProfilePath
        EBAY_BROWSER_PROFILE_NAME = "cardscanr"
        EBAY_BROWSER_REUSE_CONTEXT = if ($env:EBAY_BROWSER_REUSE_CONTEXT) { $env:EBAY_BROWSER_REUSE_CONTEXT } else { "false" }
        EBAY_BROWSER_MAX_CONCURRENCY = "1"
        OWNED_DAILY_FULL_ENABLE = $OwnedDailyFullEnable.ToString().ToLowerInvariant()
        OWNED_DAILY_FULL_ENABLE_AUTHORITY = "reports/runtime/owned_daily_full_enable.flag"
        OWNED_DAILY_MAX_ENQUEUE = [string]$OwnedDailyMaxEnqueue
        notes = @(
            "AU sold currently requires headed Chrome on the CardScanR profile.",
            "Prefer homepage/search then Sold/Completed UI filters; sold deep-links may return SORRY.",
            "Single profile only; concurrency=1 to avoid profile lock fights.",
            "OWNED_DAILY_FULL_ENABLE is read-only mirrored from owned_daily_full_enable.flag."
        )
    }
    $path = Join-Path $StateDir "live_ebay_runtime_config.json"
    ($payload | ConvertTo-Json -Depth 6) | Set-Content -Path $path -Encoding utf8
    Set-Content -Path (Join-Path $StateDir "ebay_browser_mode.txt") -Value $mode -Encoding ascii -NoNewline
    return $path
}
