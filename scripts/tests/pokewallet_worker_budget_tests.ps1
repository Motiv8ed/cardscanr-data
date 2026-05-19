$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $repoRoot 'scripts\pokewallet_worker_budget_utils.ps1')

function Assert-Equal {
    param(
        $Expected,
        $Actual,
        [string]$Message
    )

    if ($Expected -ne $Actual) {
        throw "Assertion failed: $Message. expected=$Expected actual=$Actual"
    }
}

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

$settings = [pscustomobject]@{
    ProviderPlanPerHour = 100
    ProviderPlanPerDay = 1000
    HourlyTarget = 90
    DailyTarget = 900
    SafetyBuffer = 10
    UsageEndpointEnabled = $false
    UsageEndpointUrl = ''
    UsageEndpointTimeoutSeconds = 8
}

$now = [datetimeoffset]::Parse('2026-05-20T10:00:00Z').UtcDateTime
$ledger = [pscustomobject]@{
    schemaVersion = '1.0.0'
    entries = @(
        [pscustomobject]@{ timestampUtc = '2026-05-20T09:20:00Z'; requests = 30; source = 'cycle'; status = 'ok' },
        [pscustomobject]@{ timestampUtc = '2026-05-20T09:40:00Z'; requests = 20; source = 'cycle'; status = 'ok' },
        [pscustomobject]@{ timestampUtc = '2026-05-20T08:10:00Z'; requests = 40; source = 'cycle'; status = 'ok' }
    )
}

$usage = Get-LedgerUsageSnapshot -Ledger $ledger -NowUtc $now
Assert-Equal 50 $usage.HourlyUsed 'hourly usage sums only last 60 minutes'
Assert-Equal 90 $usage.DailyUsed 'daily usage sums all entries for UTC day'

$decision = Get-BudgetDecision -Settings $settings -UsageSnapshot $usage -NowUtc $now
Assert-Equal 40 $decision.HourlyRemaining 'hourly remaining budget'
Assert-Equal 810 $decision.DailyRemaining 'daily remaining budget'
Assert-Equal 'none' $decision.WaitReason 'no wait when budget available'

$pacing = Get-PacingWaitSeconds -RequestsUsedThisCycle 45 -CycleStartedUtc $now -CycleFinishedUtc $now.AddMinutes(20) -HourlyTarget 90
Assert-Equal 600 $pacing 'pace wait aligns to 90 requests per hour target'

$usageHourlyExhausted = [pscustomobject]@{
    Source = 'ledger'
    HourlyUsed = 90
    DailyUsed = 400
    NextHourlyResetUtc = $now.AddMinutes(10)
    NextDailyResetUtc = $now.Date.AddDays(1)
}
$decisionHourlyExhausted = Get-BudgetDecision -Settings $settings -UsageSnapshot $usageHourlyExhausted -NowUtc $now
Assert-Equal 'hourly_budget_exhausted' $decisionHourlyExhausted.WaitReason 'hourly exhausted reason'
Assert-True ($decisionHourlyExhausted.WaitSeconds -ge 600) 'hourly exhausted wait is at least remaining minutes'

$usageDailyExhausted = [pscustomobject]@{
    Source = 'ledger'
    HourlyUsed = 5
    DailyUsed = 900
    NextHourlyResetUtc = $now.AddMinutes(1)
    NextDailyResetUtc = $now.Date.AddDays(1)
}
$decisionDailyExhausted = Get-BudgetDecision -Settings $settings -UsageSnapshot $usageDailyExhausted -NowUtc $now
Assert-Equal 'daily_budget_exhausted' $decisionDailyExhausted.WaitReason 'daily exhausted reason'
Assert-True ($decisionDailyExhausted.WaitSeconds -gt 0) 'daily exhausted wait is positive'

Write-Host 'All pokewallet worker budget tests passed.'
