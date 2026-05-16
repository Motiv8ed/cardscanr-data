param(
    [int]$BatchSize = 10,
    [switch]$DryRun,
    [switch]$Commit,
    [switch]$Push
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$argsList = @('tools/run_local_price_update.py', '--batch-size', $BatchSize)
if ($DryRun) { $argsList += '--dry-run' }
if ($Commit) { $argsList += '--commit' }
if ($Push) { $argsList += '--push' }

& .\.venv\Scripts\python.exe @argsList
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
