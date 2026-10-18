# Ensure recurring bulk/reference pricing sync runtime on Windows.
[CmdletBinding()]
param(
    [ValidateSet("start", "status", "stop", "run-once", "simulate-stderr")]
    [string]$Action = "start",
    [switch]$DryRun
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
$statePath = Join-Path $stateDir "bulk_reference_sync.pid.json"
$taskName = "CardScanR-BulkReferencePricingSync"
$pythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonPath) {
    throw "python not found on PATH"
}

function Get-BulkSyncProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -and ($_.CommandLine -like "*bulk_reference_price_sync.py*") }
}

function Write-StateFile {
    param([Parameter(Mandatory = $true)]$Payload)
    ($Payload | ConvertTo-Json -Depth 6) | Set-Content -Path $statePath -Encoding utf8
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
    # Windows PowerShell 5.1 with $ErrorActionPreference=Stop turns the first
    # native stderr line into a terminating NativeCommandError. The old
    # `2>> err.log` then stays empty and $LASTEXITCODE is replaced by 1.
    # Redirect the pipes on the process so PowerShell never sees stderr.
    #
    # Verify under powershell.exe 5.1 (not pwsh 7), from a copy of this repo.
    # This does not read pricing data and does not register a scheduled task:
    #   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\ensure_live_bulk_reference_pricing_runtime.ps1 -Action simulate-stderr
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
    # powershell.exe 5.1 runs on .NET Framework, whose ProcessStartInfo has no
    # StandardOutputEncoding. ASCII tracebacks still round-trip; this keeps
    # Python on UTF-8 when the pipe decoder is a superset of ASCII.
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
        $procs = @(Get-BulkSyncProcesses)
        $state = if (Test-Path $statePath) { Get-Content -Raw -Path $statePath | ConvertFrom-Json } else { $null }
        Write-Host "[bulk-runtime] running=$($procs.Count) task=$(schtasks.exe /Query /TN $taskName 2>$null | Out-String)"
        if ($state) { $state | ConvertTo-Json -Depth 6 }
        exit 0
    }
    "stop" {
        foreach ($proc in @(Get-BulkSyncProcesses)) {
            Write-Host "[bulk-runtime] stopping PID=$($proc.ProcessId)"
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path $statePath) { Remove-Item -Force $statePath }
        schtasks.exe /Delete /TN $taskName /F 2>$null | Out-Null
        exit 0
    }
    "run-once" {
        $argList = @("workers/bulk_reference_price_sync.py")
        if ($DryRun) { $argList += "--dry-run" }
        $stdout = Join-Path $stateDir "bulk_reference_sync.out.log"
        $stderr = Join-Path $stateDir "bulk_reference_sync.err.log"
        $code = Invoke-LoggedPython -PythonPath $pythonPath -ArgumentList $argList -StdoutPath $stdout -StderrPath $stderr
        exit $code
    }
    "simulate-stderr" {
        $simDir = Join-Path ([System.IO.Path]::GetTempPath()) "cardscanr-bulk-wrapper-stderr-sim"
        New-Item -ItemType Directory -Force -Path $simDir | Out-Null
        $stdout = Join-Path $simDir "out.log"
        $stderr = Join-Path $simDir "err.log"
        Remove-Item -Force -LiteralPath $stdout, $stderr -ErrorAction SilentlyContinue
        $code = Invoke-LoggedPython -PythonPath $pythonPath -ArgumentList @(
            "-c",
            "import sys; print('stdout line', flush=True); sys.stderr.write('Traceback: simulated\n'); raise SystemExit(3)"
        ) -StdoutPath $stdout -StderrPath $stderr
        Write-Host "[bulk-runtime] simulate-stderr exit=$code stdout=$stdout stderr=$stderr"
        if (Test-Path -LiteralPath $stderr) {
            Write-Host (Get-Content -Raw -LiteralPath $stderr)
        }
        exit $code
    }
    default {
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $scriptPath = Join-Path $repoRoot "scripts\ensure_live_bulk_reference_pricing_runtime.ps1"
        $actionArg = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Action run-once"
        $taskAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArg -WorkingDirectory $repoRoot
        # Finite repetition duration required by Task Scheduler (MaxValue XML is rejected).
        $taskTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
            -RepetitionInterval (New-TimeSpan -Hours 1) `
            -RepetitionDuration (New-TimeSpan -Days 365)
        $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
        if ($existing) {
            Set-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings | Out-Null
            Write-Host "[bulk-runtime] Updated scheduled task $taskName (hourly, ignore overlap)"
        } else {
            Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Description "CardScanR hourly bulk/reference pricing sync" | Out-Null
            Write-Host "[bulk-runtime] Registered scheduled task $taskName (hourly)"
        }
        $created = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        Write-StateFile -Payload @{
            component = "bulk_reference_sync"
            taskName = $taskName
            cadenceHours = 1
            registeredAtUtc = (Get-Date).ToUniversalTime().ToString("o")
            status = "scheduled"
            taskState = [string]$created.State
        }
        Write-Host "[bulk-runtime] task=$($created.TaskName) state=$($created.State)"
    }
}
