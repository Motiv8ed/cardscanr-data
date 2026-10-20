# Ensure daily ECB FX refresh for CardScanR international pricing.
[CmdletBinding()]
param(
    [ValidateSet("start", "status", "stop", "run-once", "simulate-stderr")]
    [string]$Action = "start",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# simulate-stderr is a local exit-code check and must not load supabase_env.local.json.
if ($Action -ne "simulate-stderr") {
    $envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
    if (Test-Path $envLoader) {
        . $envLoader
    }
}

$stateDir = Join-Path $repoRoot "reports\runtime"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$taskName = "CardScanR-EcbFxRefresh"
$pythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonPath) {
    throw "python not found on PATH"
}

. (Join-Path $PSScriptRoot "runtime_python_logging.ps1")

# Shared logging helpers: scripts/runtime_python_logging.ps1
# Verify under powershell.exe 5.1:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\ensure_ecb_fx_refresh_runtime.ps1 -Action simulate-stderr
# Expected: exit code 3, temp err.log contains "Traceback: simulated".


switch ($Action) {
    "status" {
        Write-Host "[ecb-fx] task=$(schtasks.exe /Query /TN $taskName 2>$null | Out-String)"
        $cachePath = Join-Path $stateDir "ecb_fx_rates.json"
        if (Test-Path $cachePath) {
            Get-Content -Raw -Path $cachePath
        }
        exit 0
    }
    "stop" {
        schtasks.exe /Delete /TN $taskName /F 2>$null | Out-Null
        Write-Host "[ecb-fx] Removed scheduled task $taskName"
        exit 0
    }
    "run-once" {
        $argList = @("tools/refresh_ecb_fx_rates.py")
        if ($Force) { $argList += "--force" }
        $stdout = Join-Path $stateDir "ecb_fx_refresh.out.log"
        $stderr = Join-Path $stateDir "ecb_fx_refresh.err.log"
        $code = Invoke-LoggedPython -PythonPath $pythonPath -ArgumentList $argList -StdoutPath $stdout -StderrPath $stderr
        exit $code
    }
    "simulate-stderr" {
        $simDir = Join-Path ([System.IO.Path]::GetTempPath()) "cardscanr-fx-wrapper-stderr-sim"
        New-Item -ItemType Directory -Force -Path $simDir | Out-Null
        $stdout = Join-Path $simDir "out.log"
        $stderr = Join-Path $simDir "err.log"
        Remove-Item -Force -LiteralPath $stdout, $stderr -ErrorAction SilentlyContinue
        $code = Invoke-LoggedPython -PythonPath $pythonPath -ArgumentList @(
            "-c",
            "import sys; print('stdout line', flush=True); sys.stderr.write('Traceback: simulated\n'); raise SystemExit(3)"
        ) -StdoutPath $stdout -StderrPath $stderr
        Write-Host "[ecb-fx] simulate-stderr exit=$code stdout=$stdout stderr=$stderr"
        if (Test-Path -LiteralPath $stderr) {
            Write-Host (Get-Content -Raw -LiteralPath $stderr)
        }
        exit $code
    }
    default {
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $scriptPath = Join-Path $repoRoot "scripts\ensure_ecb_fx_refresh_runtime.ps1"
        $actionArg = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Action run-once"
        $taskAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArg -WorkingDirectory $repoRoot
        # Daily morning refresh; weekends reuse latest ECB working-day rate after a successful check.
        $taskTrigger = New-ScheduledTaskTrigger -Daily -At 6:15AM
        $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
        if ($existing) {
            Set-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings | Out-Null
            Write-Host "[ecb-fx] Updated scheduled task $taskName (daily)"
        } else {
            Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Description "CardScanR daily ECB FX reference-rate refresh" | Out-Null
            Write-Host "[ecb-fx] Registered scheduled task $taskName (daily)"
        }
    }
}
