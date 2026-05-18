param(
    [string]$RepoRoot = "",
    [int]$IntervalMinutes = 0,
    [string]$TaskName = "CardScanR PokéWallet Catalogue Worker"
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$configPath = Join-Path $RepoRoot 'data\pokewallet_catalog_config.json'
$cycleScript = Join-Path $RepoRoot 'scripts\run_pokewallet_catalog_cycle.ps1'

function Read-JsonFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $null
    }
    try {
        return (Get-Content -Path $Path -Raw -Encoding UTF8 -ErrorAction Stop) | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

$config = Read-JsonFile -Path $configPath
if ($null -eq $config -or $null -eq $config.fullCatalogueWorker) {
    throw 'fullCatalogueWorker config is missing.'
}

if ($IntervalMinutes -le 0) {
    $IntervalMinutes = [int]$config.fullCatalogueWorker.intervalMinutes
}
if ($IntervalMinutes -le 0) {
    $IntervalMinutes = 75
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $cycleScript) `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Runs the CardScanR Pokewallet provider catalogue export cycle every $IntervalMinutes minutes." `
    -Force | Out-Null

Enable-ScheduledTask -TaskName $TaskName | Out-Null

$taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
Write-Host "Scheduled task installed: $TaskName"
Write-Host "Interval: $IntervalMinutes minutes"
if ($null -ne $taskInfo) {
    Write-Host "Next run: $($taskInfo.NextRunTime)"
}
