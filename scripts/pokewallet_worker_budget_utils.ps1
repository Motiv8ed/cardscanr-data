$ErrorActionPreference = 'Stop'

function Get-EnvIntOrDefault {
    param(
        [string]$Name,
        [int]$DefaultValue
    )

    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $DefaultValue
    }

    $parsed = 0
    if ([int]::TryParse($raw.Trim(), [ref]$parsed) -and $parsed -gt 0) {
        return $parsed
    }

    return $DefaultValue
}

function Get-EnvIntFromNamesOrDefault {
    param(
        [string[]]$Names,
        [int]$DefaultValue
    )

    foreach ($name in $Names) {
        $resolved = Get-EnvIntOrDefault -Name $name -DefaultValue -1
        if ($resolved -gt 0) {
            return $resolved
        }
    }

    return $DefaultValue
}

function Get-EnvBoolFromNamesOrDefault {
    param(
        [string[]]$Names,
        [bool]$DefaultValue
    )

    foreach ($name in $Names) {
        $raw = [Environment]::GetEnvironmentVariable($name)
        if ([string]::IsNullOrWhiteSpace($raw)) {
            continue
        }
        $text = $raw.Trim().ToLowerInvariant()
        return $text -in @('1', 'true', 'yes', 'y', 'on')
    }

    return $DefaultValue
}

function Get-EnvBoolOrDefault {
    param(
        [string]$Name,
        [bool]$DefaultValue
    )

    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $DefaultValue
    }

    $text = $raw.Trim().ToLowerInvariant()
    return $text -in @('1', 'true', 'yes', 'y', 'on')
}

function Convert-ToUtcDateTime {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }

    $dto = [datetimeoffset]::MinValue
    if ([datetimeoffset]::TryParse($Value, [ref]$dto)) {
        return $dto.UtcDateTime
    }

    return $null
}

function Get-NestedValue {
    param(
        [object]$InputObject,
        [string[]]$PathSegments
    )

    $current = $InputObject
    foreach ($segment in $PathSegments) {
        if ($null -eq $current) {
            return $null
        }

        $matched = $null
        if ($current -is [System.Collections.IDictionary]) {
            foreach ($key in $current.Keys) {
                if ([string]::Equals([string]$key, $segment, [System.StringComparison]::InvariantCultureIgnoreCase)) {
                    $matched = $current[$key]
                    break
                }
            }
        }
        elseif ($current.PSObject -and $current.PSObject.Properties) {
            foreach ($prop in $current.PSObject.Properties) {
                if ([string]::Equals([string]$prop.Name, $segment, [System.StringComparison]::InvariantCultureIgnoreCase)) {
                    $matched = $prop.Value
                    break
                }
            }
        }

        if ($null -eq $matched) {
            return $null
        }
        $current = $matched
    }

    return $current
}

function Get-FirstIntFromCandidatePaths {
    param(
        [object]$InputObject,
        [string[]]$Candidates
    )

    foreach ($candidate in $Candidates) {
        $segments = $candidate.Split('.')
        $value = Get-NestedValue -InputObject $InputObject -PathSegments $segments
        if ($null -eq $value) {
            continue
        }

        $parsed = 0
        if ([int]::TryParse([string]$value, [ref]$parsed) -and $parsed -ge 0) {
            return $parsed
        }
    }

    return $null
}

function Resolve-PokewalletBudgetSettings {
    param([object]$WorkerConfig)

    $providerHourly = 100
    $providerDaily = 1000
    $workerHourly = 90
    $workerDaily = 900
    $safetyBuffer = 10

    if ($null -ne $WorkerConfig) {
        if ($null -ne $WorkerConfig.providerPlanRequestsPerHour) {
            $providerHourly = [int]$WorkerConfig.providerPlanRequestsPerHour
        }
        if ($null -ne $WorkerConfig.providerPlanRequestsPerDay) {
            $providerDaily = [int]$WorkerConfig.providerPlanRequestsPerDay
        }
        if ($null -ne $WorkerConfig.maxRequestsPerHour) {
            $workerHourly = [int]$WorkerConfig.maxRequestsPerHour
        }
        if ($null -ne $WorkerConfig.maxRequestsPerDay) {
            $workerDaily = [int]$WorkerConfig.maxRequestsPerDay
        }
        if ($null -ne $WorkerConfig.requestSafetyBuffer) {
            $safetyBuffer = [int]$WorkerConfig.requestSafetyBuffer
        }
    }

    $providerHourly = Get-EnvIntFromNamesOrDefault -Names @('CARDSCANR_PROVIDER_PLAN_REQUESTS_PER_HOUR', 'POKEWALLET_PROVIDER_PLAN_REQUESTS_PER_HOUR') -DefaultValue $providerHourly
    $providerDaily = Get-EnvIntFromNamesOrDefault -Names @('CARDSCANR_PROVIDER_PLAN_REQUESTS_PER_DAY', 'POKEWALLET_PROVIDER_PLAN_REQUESTS_PER_DAY') -DefaultValue $providerDaily
    $workerHourly = Get-EnvIntFromNamesOrDefault -Names @('CARDSCANR_MAX_REQUESTS_PER_HOUR', 'POKEWALLET_MAX_REQUESTS_PER_HOUR') -DefaultValue $workerHourly
    $workerDaily = Get-EnvIntFromNamesOrDefault -Names @('CARDSCANR_MAX_REQUESTS_PER_DAY', 'POKEWALLET_MAX_REQUESTS_PER_DAY') -DefaultValue $workerDaily
    $safetyBuffer = Get-EnvIntFromNamesOrDefault -Names @('CARDSCANR_REQUEST_SAFETY_BUFFER', 'POKEWALLET_REQUEST_SAFETY_BUFFER') -DefaultValue $safetyBuffer

    $providerHourlySafe = [Math]::Max(1, $providerHourly - $safetyBuffer)
    $providerDailySafe = [Math]::Max(1, $providerDaily - $safetyBuffer)

    $hourlyTarget = [Math]::Max(1, [Math]::Min($workerHourly, $providerHourlySafe))
    $dailyTarget = [Math]::Max(1, [Math]::Min($workerDaily, $providerDailySafe))

    $usageEndpointUrl = ''
    if ($null -ne $WorkerConfig -and -not [string]::IsNullOrWhiteSpace([string]$WorkerConfig.usageEndpointUrl)) {
        $usageEndpointUrl = [string]$WorkerConfig.usageEndpointUrl
    }
    $usageEndpointUrlEnv = [Environment]::GetEnvironmentVariable('CARDSCANR_USAGE_ENDPOINT_URL')
    if ([string]::IsNullOrWhiteSpace($usageEndpointUrlEnv)) {
        $usageEndpointUrlEnv = [Environment]::GetEnvironmentVariable('POKEWALLET_USAGE_ENDPOINT_URL')
    }
    if (-not [string]::IsNullOrWhiteSpace($usageEndpointUrlEnv)) {
        $usageEndpointUrl = $usageEndpointUrlEnv.Trim()
    }

    $usageEndpointEnabled = $false
    if ($null -ne $WorkerConfig -and $null -ne $WorkerConfig.usageEndpointEnabled) {
        $usageEndpointEnabled = [bool]$WorkerConfig.usageEndpointEnabled
    }
    $usageEndpointEnabled = Get-EnvBoolFromNamesOrDefault -Names @('CARDSCANR_USAGE_ENDPOINT_ENABLED', 'POKEWALLET_USAGE_ENDPOINT_ENABLED') -DefaultValue $usageEndpointEnabled

    if ([string]::IsNullOrWhiteSpace($usageEndpointUrl)) {
        $usageEndpointEnabled = $false
    }

    $usageEndpointTimeoutSeconds = 8
    if ($null -ne $WorkerConfig -and $null -ne $WorkerConfig.usageEndpointTimeoutSeconds) {
        $usageEndpointTimeoutSeconds = [int]$WorkerConfig.usageEndpointTimeoutSeconds
    }

    return [pscustomobject]@{
        ProviderPlanPerHour = $providerHourly
        ProviderPlanPerDay = $providerDaily
        HourlyTarget = $hourlyTarget
        DailyTarget = $dailyTarget
        SafetyBuffer = $safetyBuffer
        UsageEndpointEnabled = $usageEndpointEnabled
        UsageEndpointUrl = $usageEndpointUrl
        UsageEndpointTimeoutSeconds = [Math]::Max(2, $usageEndpointTimeoutSeconds)
    }
}

function Read-WorkerLedger {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return [pscustomobject]@{
            schemaVersion = '1.0.0'
            entries = @()
            updatedAtUtc = $null
        }
    }

    try {
        $parsed = (Get-Content -Path $Path -Raw -Encoding UTF8 -ErrorAction Stop) | ConvertFrom-Json
    }
    catch {
        return [pscustomobject]@{
            schemaVersion = '1.0.0'
            entries = @()
            updatedAtUtc = $null
        }
    }

    if ($null -eq $parsed -or $null -eq $parsed.entries) {
        $parsed = [pscustomobject]@{
            schemaVersion = '1.0.0'
            entries = @()
            updatedAtUtc = $null
        }
    }

    return $parsed
}

function Write-WorkerLedger {
    param(
        [string]$Path,
        [object]$Ledger
    )

    $parent = Split-Path -Parent $Path
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }

    $payload = [ordered]@{
        schemaVersion = '1.0.0'
        updatedAtUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        entries = @($Ledger.entries)
    }

    $tmpPath = "$Path.tmp"
    $json = $payload | ConvertTo-Json -Depth 8
    Set-Content -Path $tmpPath -Value $json -Encoding UTF8
    Move-Item -Path $tmpPath -Destination $Path -Force
}

function Add-LedgerEntry {
    param(
        [object]$Ledger,
        [datetime]$TimestampUtc,
        [int]$Requests,
        [string]$Source,
        [string]$Status
    )

    if ($Requests -lt 0) {
        $Requests = 0
    }

    $entry = [ordered]@{
        timestampUtc = $TimestampUtc.ToString('yyyy-MM-ddTHH:mm:ssZ')
        requests = $Requests
        source = $Source
        status = $Status
    }

    $entries = @($Ledger.entries)
    $entries += [pscustomobject]$entry

    $pruned = @()
    $cutoff = $TimestampUtc.AddDays(-2)
    foreach ($item in $entries) {
        $ts = Convert-ToUtcDateTime -Value ([string]$item.timestampUtc)
        if ($null -eq $ts) {
            continue
        }
        if ($ts -lt $cutoff) {
            continue
        }
        $pruned += $item
    }

    $Ledger.entries = $pruned
    return $Ledger
}

function Get-LedgerUsageSnapshot {
    param(
        [object]$Ledger,
        [datetime]$NowUtc
    )

    $windowStart = $NowUtc.AddHours(-1)
    $nextDailyResetUtc = [datetime]::SpecifyKind($NowUtc.Date.AddDays(1), [System.DateTimeKind]::Utc)

    $hourlyUsed = 0
    $dailyUsed = 0
    $oldestHourly = $null

    foreach ($item in @($Ledger.entries)) {
        $ts = Convert-ToUtcDateTime -Value ([string]$item.timestampUtc)
        if ($null -eq $ts) {
            continue
        }
        $requests = 0
        [int]::TryParse([string]$item.requests, [ref]$requests) | Out-Null
        if ($requests -lt 0) {
            $requests = 0
        }

        if ($ts -ge $windowStart) {
            $hourlyUsed += $requests
            if ($null -eq $oldestHourly -or $ts -lt $oldestHourly) {
                $oldestHourly = $ts
            }
        }
        if ($ts -ge $NowUtc.Date) {
            $dailyUsed += $requests
        }
    }

    $nextHourlyResetUtc = if ($null -ne $oldestHourly) { $oldestHourly.AddHours(1) } else { $NowUtc }

    return [pscustomobject]@{
        Source = 'ledger'
        HourlyUsed = $hourlyUsed
        DailyUsed = $dailyUsed
        NextHourlyResetUtc = $nextHourlyResetUtc
        NextDailyResetUtc = $nextDailyResetUtc
    }
}

function Try-GetLiveUsageSnapshot {
    param(
        [object]$Settings,
        [string]$ApiKey
    )

    if (-not $Settings.UsageEndpointEnabled -or [string]::IsNullOrWhiteSpace($Settings.UsageEndpointUrl)) {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        return $null
    }

    try {
        $headers = @{
            'X-API-Key' = $ApiKey
            'Accept' = 'application/json'
        }
        $response = Invoke-RestMethod -Method Get -Uri $Settings.UsageEndpointUrl -Headers $headers -TimeoutSec $Settings.UsageEndpointTimeoutSeconds -ErrorAction Stop

        $hourlyUsed = Get-FirstIntFromCandidatePaths -InputObject $response -Candidates @(
            'hourlyUsed',
            'hour.used',
            'hour.usedRequests',
            'usage.hour.used',
            'usage.hourly.used',
            'limits.hour.used',
            'requests.hour.used'
        )
        $dailyUsed = Get-FirstIntFromCandidatePaths -InputObject $response -Candidates @(
            'dailyUsed',
            'day.used',
            'usage.day.used',
            'usage.daily.used',
            'limits.day.used',
            'requests.day.used'
        )

        if ($null -eq $hourlyUsed -or $null -eq $dailyUsed) {
            return $null
        }

        $nowUtc = (Get-Date).ToUniversalTime()
        $hourUtc = [datetime]::SpecifyKind((Get-Date -Date $nowUtc -Minute 0 -Second 0).AddHours(1), [System.DateTimeKind]::Utc)
        $dayUtc = [datetime]::SpecifyKind($nowUtc.Date.AddDays(1), [System.DateTimeKind]::Utc)

        return [pscustomobject]@{
            Source = 'live'
            HourlyUsed = $hourlyUsed
            DailyUsed = $dailyUsed
            NextHourlyResetUtc = $hourUtc
            NextDailyResetUtc = $dayUtc
        }
    }
    catch {
        return $null
    }
}

function Get-BudgetDecision {
    param(
        [object]$Settings,
        [object]$UsageSnapshot,
        [datetime]$NowUtc
    )

    $hourlyRemaining = [Math]::Max(0, $Settings.HourlyTarget - [int]$UsageSnapshot.HourlyUsed)
    $dailyRemaining = [Math]::Max(0, $Settings.DailyTarget - [int]$UsageSnapshot.DailyUsed)

    $waitReason = 'none'
    $waitSeconds = 0

    if ($dailyRemaining -le 0) {
        $waitReason = 'daily_budget_exhausted'
        $waitSeconds = [Math]::Max(1, [int][Math]::Ceiling(($UsageSnapshot.NextDailyResetUtc - $NowUtc).TotalSeconds))
    }
    elseif ($hourlyRemaining -le 0) {
        $waitReason = 'hourly_budget_exhausted'
        $waitSeconds = [Math]::Max(1, [int][Math]::Ceiling(($UsageSnapshot.NextHourlyResetUtc - $NowUtc).TotalSeconds))
    }

    return [pscustomobject]@{
        UsageSource = $UsageSnapshot.Source
        HourlyUsed = [int]$UsageSnapshot.HourlyUsed
        DailyUsed = [int]$UsageSnapshot.DailyUsed
        HourlyTarget = [int]$Settings.HourlyTarget
        DailyTarget = [int]$Settings.DailyTarget
        HourlyRemaining = $hourlyRemaining
        DailyRemaining = $dailyRemaining
        WaitReason = $waitReason
        WaitSeconds = $waitSeconds
        NextHourlyResetUtc = $UsageSnapshot.NextHourlyResetUtc
        NextDailyResetUtc = $UsageSnapshot.NextDailyResetUtc
    }
}

function Get-PacingWaitSeconds {
    param(
        [int]$RequestsUsedThisCycle,
        [datetime]$CycleStartedUtc,
        [datetime]$CycleFinishedUtc,
        [int]$HourlyTarget
    )

    if ($RequestsUsedThisCycle -le 0 -or $HourlyTarget -le 0) {
        return 0
    }

    $requiredSeconds = [int][Math]::Ceiling(($RequestsUsedThisCycle / [double]$HourlyTarget) * 3600.0)
    $elapsedSeconds = [int][Math]::Ceiling(($CycleFinishedUtc - $CycleStartedUtc).TotalSeconds)
    return [Math]::Max(0, $requiredSeconds - $elapsedSeconds)
}
