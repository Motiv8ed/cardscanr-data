param(
    [ValidateSet("jp")][string]$Language = "jp",
    [int]$MaxNewSetsPerCycle = 20,
    [switch]$UntilComplete,
    [switch]$Commit,
    [switch]$Push,
    [switch]$Validate,
    [switch]$ExportChatGPTReport,
    [switch]$SleepWhenBudgetBlocked,
    [int]$PollSeconds = 300,
    [int]$MaxCycles = 0,
    [switch]$StopAfterDailyBudget,
    [switch]$DryRunOnly,
    [switch]$NoPush,
    [switch]$SkipGitSync,
    [switch]$ResetBudgetLedger
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$argsList = @(
    "tools/run_pokewallet_missing_price_worker.py",
    "--language", $Language,
    "--max-new-sets-per-cycle", [string]$MaxNewSetsPerCycle,
    "--poll-seconds", [string]$PollSeconds
)

if ($UntilComplete) { $argsList += "--until-complete" }
if ($Commit) { $argsList += "--commit" }
if ($Push) { $argsList += "--push" }
if ($Validate) { $argsList += "--validate" }
if ($ExportChatGPTReport) { $argsList += "--export-chatgpt-report" }
if ($SleepWhenBudgetBlocked) { $argsList += "--sleep-when-budget-blocked" }
if ($MaxCycles -gt 0) { $argsList += @("--max-cycles", [string]$MaxCycles) }
if ($StopAfterDailyBudget) { $argsList += "--stop-after-daily-budget" }
if ($DryRunOnly) { $argsList += "--dry-run-only" }
if ($NoPush) { $argsList += "--no-push" }
if ($SkipGitSync) { $argsList += "--skip-git-sync" }
if ($ResetBudgetLedger) { $argsList += "--reset-budget-ledger" }

Write-Host "[worker] Running PokeWallet missing-price worker..."
Write-Host "[worker] Settings:"
Write-Host ("  language={0}" -f $Language)
Write-Host ("  maxNewSetsPerCycle={0}" -f $MaxNewSetsPerCycle)
Write-Host ("  untilComplete={0}" -f ($(if ($UntilComplete) { 'yes' } else { 'no' })))
Write-Host ("  commit={0}" -f ($(if ($Commit) { 'yes' } else { 'no' })))
Write-Host ("  push={0}" -f ($(if ($Push -and -not $NoPush) { 'yes' } else { 'no' })))
Write-Host ("  validate={0}" -f ($(if ($Validate) { 'yes' } else { 'no' })))
Write-Host ("  sleepWhenBudgetBlocked={0}" -f ($(if ($SleepWhenBudgetBlocked) { 'yes' } else { 'no' })))
Write-Host ("  pollSeconds={0}" -f $PollSeconds)
Write-Host ("  skipGitSync={0}" -f ($(if ($SkipGitSync) { 'yes' } else { 'no' })))
Write-Host ("  resetBudgetLedger={0}" -f ($(if ($ResetBudgetLedger) { 'yes' } else { 'no' })))

& $pythonPath "-u" @argsList
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    throw "run_pokewallet_missing_price_worker.py failed with exit code $exitCode"
}
