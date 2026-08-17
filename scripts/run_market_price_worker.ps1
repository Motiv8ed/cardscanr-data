param(
    [switch]$Once,
    [int]$MaxCycles = 0,
    [int]$MaxJobs = 0,
    [int]$PollSeconds = 0
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envLoader = Join-Path $repoRoot "scripts\load_supabase_env.ps1"
if (Test-Path $envLoader) {
    . $envLoader
}

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
}

$stopIntentPath = Join-Path $repoRoot "reports\runtime\worker_stop_intent.json"

function Read-StopIntent {
    if (-not (Test-Path $stopIntentPath)) { return $null }
    try {
        return Get-Content -Raw -Path $stopIntentPath | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Clear-StopIntent {
    if (Test-Path $stopIntentPath) {
        Remove-Item -Force $stopIntentPath -ErrorAction SilentlyContinue
    }
}

function Test-SupervisedOrConsoleExit {
    param([Parameter(Mandatory = $true)][int]$ExitCode)

    # Windows TerminateProcess / Stop-Process -Force commonly surfaces as -1
    # (unsigned 0xFFFFFFFF). Ctrl+C often surfaces as STATUS_CONTROL_C_EXIT.
    $benignCodes = @(
        -1,
        [int]0xFFFFFFFF,
        -1073741510, # 0xC000013A STATUS_CONTROL_C_EXIT
        3221225786   # unsigned form of STATUS_CONTROL_C_EXIT
    )
    if ($benignCodes -contains $ExitCode) {
        return $true
    }
    $intent = Read-StopIntent
    if ($null -ne $intent) {
        return $true
    }
    return $false
}

$argsList = @("workers/market_price_worker.py")
if ($Once) { $argsList += "--once" }
if ($MaxCycles -gt 0) { $argsList += @("--max-cycles", [string]$MaxCycles) }
if ($MaxJobs -gt 0) { $argsList += @("--max-jobs", [string]$MaxJobs) }
if ($PollSeconds -gt 0) { $argsList += @("--poll-seconds", [string]$PollSeconds) }

Write-Host "[market-engine] Running market price worker..."
& $pythonPath @argsList
$exitCode = $LASTEXITCODE
if ($null -eq $exitCode) { $exitCode = 0 }

if ($exitCode -eq 0) {
    Clear-StopIntent
    return
}

$intent = Read-StopIntent
if (Test-SupervisedOrConsoleExit -ExitCode ([int]$exitCode)) {
    $reason = if ($intent -and $intent.reason) { [string]$intent.reason } else { "supervised_or_console_termination" }
    Write-Host "[market-engine] Worker stopped intentionally/supervised (exit=$exitCode reason=$reason). Not treating as a pricing crash."
    Clear-StopIntent
    exit 0
}

Clear-StopIntent
throw "market_price_worker.py failed with exit code $exitCode"
