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

function Format-ProcessArgument {
    param([string]$Value)
    if ($null -eq $Value) { return '""' }
    if ($Value -match '[\s"]') {
        return '"' + ($Value.Replace('"', '\"')) + '"'
    }
    return $Value
}

function Add-RuntimeLogText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyString()][string]$Text
    )
    if ([string]::IsNullOrEmpty($Text)) { return }
    if (-not $Text.EndsWith("`n")) {
        $Text += "`r`n"
    }
    # Match an existing Windows PowerShell `>>` log (UTF-16 LE BOM). New logs
    # are UTF-8 without a BOM so Notepad can open a fresh err.log.
    $encoding = New-Object System.Text.UTF8Encoding $false
    if (Test-Path -LiteralPath $Path) {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $bom = New-Object byte[] 2
            $read = $stream.Read($bom, 0, 2)
            if ($read -ge 2 -and $bom[0] -eq 255 -and $bom[1] -eq 254) {
                $encoding = New-Object System.Text.UnicodeEncoding $false, $false
            }
        } finally {
            $stream.Dispose()
        }
    }
    [System.IO.File]::AppendAllText($Path, $Text, $encoding)
}

function Invoke-LoggedPython {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath
    )
    # Same Windows PowerShell 5.1 NativeCommandError bug as the bulk wrapper:
    # $ErrorActionPreference=Stop plus `2>>` drops stderr and forces exit 1.
    #
    # Verify under powershell.exe 5.1 (not pwsh 7), from a copy of this repo:
    #   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\ensure_ecb_fx_refresh_runtime.ps1 -Action simulate-stderr
    # Expected: exit code 3, temp err.log contains "Traceback: simulated",
    # temp out.log contains "stdout line".
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $PythonPath
    $psi.WorkingDirectory = (Get-Location).Path
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $quoted = foreach ($arg in $ArgumentList) { Format-ProcessArgument $arg }
    $psi.Arguments = ($quoted -join ' ')
    # powershell.exe 5.1 / .NET Framework has no ProcessStartInfo.StandardOutputEncoding.
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    [void]$proc.WaitForExit()
    $stdoutText = [string]$stdoutTask.GetAwaiter().GetResult()
    $stderrText = [string]$stderrTask.GetAwaiter().GetResult()
    $exitCode = $proc.ExitCode
    if ($null -eq $exitCode) { $exitCode = 1 }
    $proc.Dispose()
    Add-RuntimeLogText -Path $StdoutPath -Text $stdoutText
    Add-RuntimeLogText -Path $StderrPath -Text $stderrText
    return $exitCode
}

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
