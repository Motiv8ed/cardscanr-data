param(
    [int]$BatchSize = 10,
    [int]$IntervalMinutes = 120,
    [string]$RepoRoot = ""
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

$logsDir = Join-Path $RepoRoot 'logs'
$logPath = Join-Path $logsDir 'local_price_updater.log'
$lockPath = Join-Path $RepoRoot '.local_updater.lock'
$pythonPath = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$updaterScript = Join-Path $RepoRoot 'tools\run_local_price_update.py'

if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
}

function Write-LoopLog {
    param(
        [string]$Level,
        [string]$Message
    )

    $timestamp = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
    $line = "$timestamp [$Level] $Message"
    Add-Content -Path $logPath -Value $line -Encoding UTF8
}

function Test-ProcessAlive {
    param([int]$ProcessId)

    try {
        $null = Get-Process -Id $ProcessId -ErrorAction Stop
        return $true
    }
    catch {
        return $false
    }
}

function Get-LockData {
    if (-not (Test-Path $lockPath)) {
        return $null
    }

    try {
        $raw = Get-Content -Path $lockPath -Raw -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $null
        }
        return $raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Acquire-Lock {
    $existing = Get-LockData
    if ($null -ne $existing -and $existing.PSObject.Properties.Name -contains 'pid') {
        $existingPid = 0
        try {
            $existingPid = [int]$existing.pid
        }
        catch {
            $existingPid = 0
        }

        if ($existingPid -gt 0 -and (Test-ProcessAlive -ProcessId $existingPid)) {
            Write-Host "Updater already running (PID $existingPid)."
            return $false
        }

        Write-LoopLog -Level 'WARN' -Message "Removing stale lock file."
        Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    }
    elseif (Test-Path $lockPath) {
        Write-LoopLog -Level 'WARN' -Message "Removing unreadable lock file."
        Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    }

    try {
        $stream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $writer = New-Object System.IO.StreamWriter($stream)
        $lockData = @{
            pid = $PID
            startedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
            repoRoot = $RepoRoot
            batchSize = [Math]::Max(1, $BatchSize)
            intervalMinutes = [Math]::Max(1, $IntervalMinutes)
        } | ConvertTo-Json -Compress
        $writer.Write($lockData)
        $writer.Flush()
        $writer.Dispose()
        $stream.Dispose()
        return $true
    }
    catch {
        Write-Host "Failed to acquire lock file at $lockPath"
        return $false
    }
}

$BatchSize = [Math]::Max(1, $BatchSize)
$IntervalMinutes = [Math]::Max(1, $IntervalMinutes)
$intervalSeconds = $IntervalMinutes * 60

if (-not (Acquire-Lock)) {
    exit 1
}

Write-LoopLog -Level 'INFO' -Message "Updater loop started. BatchSize=$BatchSize IntervalMinutes=$IntervalMinutes RepoRoot=$RepoRoot"

try {
    while ($true) {
        try {
            Set-Location $RepoRoot

            $preStatusOutput = git status --porcelain
            if ($LASTEXITCODE -ne 0) {
                Write-LoopLog -Level 'ERROR' -Message "git status failed; skipping cycle."
            }
            elseif (-not [string]::IsNullOrWhiteSpace(($preStatusOutput | Out-String).Trim())) {
                Write-LoopLog -Level 'WARN' -Message "Uncommitted changes detected before cycle; skipping update."
            }
            else {
                $pullOutput = git pull --ff-only 2>&1
                foreach ($line in $pullOutput) {
                    if (-not [string]::IsNullOrWhiteSpace("$line")) {
                        Write-LoopLog -Level 'INFO' -Message "git pull: $line"
                    }
                }

                if ($LASTEXITCODE -ne 0) {
                    Write-LoopLog -Level 'ERROR' -Message "git pull --ff-only failed; skipping cycle."
                }
                elseif (-not (Test-Path $pythonPath)) {
                    Write-LoopLog -Level 'ERROR' -Message "Python interpreter missing at $pythonPath; skipping cycle."
                }
                else {
                    Write-LoopLog -Level 'INFO' -Message "Starting update cycle."
                    $updaterOutput = & $pythonPath $updaterScript --batch-size $BatchSize --commit --push 2>&1
                    foreach ($line in $updaterOutput) {
                        if (-not [string]::IsNullOrWhiteSpace("$line")) {
                            Write-LoopLog -Level 'INFO' -Message "updater: $line"
                        }
                    }

                    if ($LASTEXITCODE -ne 0) {
                        Write-LoopLog -Level 'ERROR' -Message "Updater exited with code $LASTEXITCODE."
                    }
                    else {
                        Write-LoopLog -Level 'INFO' -Message "Update cycle completed successfully."
                    }
                }
            }
        }
        catch {
            Write-LoopLog -Level 'ERROR' -Message "Unhandled cycle error: $($_.Exception.Message)"
        }

        Write-LoopLog -Level 'INFO' -Message "Sleeping for $IntervalMinutes minute(s)."
        Start-Sleep -Seconds $intervalSeconds
    }
}
finally {
    if (Test-Path $lockPath) {
        Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
    }
    Write-LoopLog -Level 'INFO' -Message "Updater loop stopped."
}
