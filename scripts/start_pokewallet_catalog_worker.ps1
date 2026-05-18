param(
    [string]$RepoRoot = "",
    [int]$IntervalMinutes = 0,
    [switch]$RunNow,
    [string]$TaskName = "CardScanR PokéWallet Catalogue Worker"
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$installScript = Join-Path $RepoRoot 'scripts\install_pokewallet_catalog_scheduled_task.ps1'
if (-not (Test-Path $installScript)) {
    throw "Install script not found: $installScript"
}

$installArgs = @(
    '-NoProfile',
    '-ExecutionPolicy',
    'Bypass',
    '-File',
    $installScript,
    '-RepoRoot',
    $RepoRoot,
    '-TaskName',
    $TaskName
)
if ($IntervalMinutes -gt 0) {
    $installArgs += @('-IntervalMinutes', [string]$IntervalMinutes)
}

& powershell.exe @installArgs
if ($LASTEXITCODE -ne 0) {
    throw "Scheduled task install failed with exit code $LASTEXITCODE."
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
Enable-ScheduledTask -TaskName $TaskName | Out-Null

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host 'Started one immediate scheduled task run.'
}

$taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ''
Write-Host 'PokéWallet catalogue worker is scheduled.'
Write-Host ("Task: {0}" -f $TaskName)
Write-Host ("Enabled: {0}" -f ($task.State -ne 'Disabled'))
Write-Host ("Next run: {0}" -f $taskInfo.NextRunTime)
Write-Host 'Status command: .\scripts\status_pokewallet_catalog_worker.ps1'
