param(
    [int]$BatchSize = 20,
    [switch]$DryRun,
    [switch]$Commit,
    [switch]$Push,
    [switch]$AllDay,
    [int]$TargetHourlyRequests = 0,
    [int]$TargetDailyRequests = 0,
    [switch]$UntilComplete,
    [int]$MaxCycles = 0,
    [int]$CycleDelaySeconds = 20,
    [bool]$CommitChanges = $false,
    [bool]$PushChanges = $false
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if ($CommitChanges) { $Commit = $true }
if ($PushChanges) { $Push = $true }

$argsList = @('tools/run_local_price_update.py', '--batch-size', $BatchSize)
if ($DryRun) { $argsList += '--dry-run' }
if ($Commit) { $argsList += '--commit' }
if ($Push) { $argsList += '--push' }
if ($AllDay) { $argsList += '--all-day' }
if ($TargetHourlyRequests -gt 0) { $argsList += @('--target-hourly-requests', $TargetHourlyRequests) }
if ($TargetDailyRequests -gt 0) { $argsList += @('--target-daily-requests', $TargetDailyRequests) }
if ($UntilComplete) { $argsList += '--until-complete' }
if ($MaxCycles -gt 0) { $argsList += @('--max-cycles', $MaxCycles) }
if ($CycleDelaySeconds -gt 0) { $argsList += @('--cycle-delay-seconds', $CycleDelaySeconds) }

& .\.venv\Scripts\python.exe @argsList
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
