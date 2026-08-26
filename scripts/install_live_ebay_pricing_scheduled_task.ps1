[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$ensureScript = Join-Path $repoRoot "scripts\ensure_live_ebay_pricing_runtime.ps1"
if (-not (Test-Path $ensureScript)) {
    throw "Missing runtime script: $ensureScript"
}

$taskName = "CardScanR-LiveEbayPricingRuntime"

function Remove-ExistingTask {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "[runtime] Removed scheduled task $taskName"
    }
}

if ($Remove) {
    Remove-ExistingTask
    return
}

if (-not $PSCmdlet.ShouldProcess($taskName, "Register CardScanR live eBay pricing runtime task")) {
    return
}

Remove-ExistingTask

# Hidden VBS → hidden PowerShell. Task Scheduler Interactive + powershell.exe still flashes a console.
$vbsLauncher = Join-Path $repoRoot "scripts\ensure_live_ebay_pricing_runtime_hidden.vbs"
if (-not (Test-Path $vbsLauncher)) {
    throw "Missing hidden launcher: $vbsLauncher"
}

$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "//B //Nologo `"$vbsLauncher`"" `
    -WorkingDirectory $repoRoot

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 365)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -Hidden

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger @($logonTrigger, $repeatTrigger) `
        -Settings $settings `
        -Principal $principal `
        -Description "Ensures CardScanR live eBay worker + AU/US scheduler are running (single-instance, hidden)." | Out-Null
} catch {
    Write-Host "[runtime] Register-ScheduledTask failed: $($_.Exception.Message)"
    Write-Host "[runtime] Falling back to schtasks.exe current-user registration..."
    $tr = "wscript.exe //B //Nologo `"$vbsLauncher`""
    # At logon for current user
    & schtasks.exe /Create /TN $taskName /TR $tr /SC ONLOGON /RL LIMITED /F | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to register scheduled task $taskName"
    }
}

$created = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
Write-Host "[runtime] Registered scheduled task $($created.TaskPath)$($created.TaskName) state=$($created.State)"
Write-Host "[runtime] Starts at logon; ensure script also re-checks when invoked every 15 minutes if triggers allow."
Write-Host "[runtime] Disable with: .\scripts\install_live_ebay_pricing_scheduled_task.ps1 -Remove"
Write-Host "[runtime] Manual stop: .\scripts\ensure_live_ebay_pricing_runtime.ps1 -Stop"
