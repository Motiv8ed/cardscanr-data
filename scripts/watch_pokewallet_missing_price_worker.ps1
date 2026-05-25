param(
    [int]$RefreshSeconds = 15
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$workerLatest = Join-Path $repoRoot "reports\pokewallet_missing_price_worker_latest.json"
$importLatest = Join-Path $repoRoot "reports\pokewallet_price_import_latest.json"
$workerRuns = Join-Path $repoRoot "reports\pokewallet_missing_price_worker_runs.jsonl"

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return $null
    }
    try {
        return Get-Content -Path $Path -Raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

while ($true) {
    Clear-Host
    Write-Host ("[{0}] PokeWallet worker watch" -f ((Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")))
    Write-Host ""

    $worker = Read-JsonFile -Path $workerLatest
    Write-Host "Worker latest summary:"
    if ($null -eq $worker) {
        Write-Host "  (missing or unreadable)"
    }
    else {
        Write-Host ("  status={0} stopReason={1}" -f $worker.status, $worker.stopReason)
        Write-Host ("  cyclesAttempted={0} cyclesCompleted={1} cyclesBlockedByBudget={2}" -f $worker.cyclesAttempted, $worker.cyclesCompleted, $worker.cyclesBlockedByBudget)
        Write-Host ("  totalApiRequests={0} totalImportedRecords={1}" -f $worker.totalApiRequests, $worker.totalImportedRecords)
        Write-Host ("  beforeJpPriceCount={0} afterJpPriceCount={1}" -f $worker.beforeJpPriceCount, $worker.afterJpPriceCount)
        Write-Host ("  beforeJpPriceFileCount={0} afterJpPriceFileCount={1}" -f $worker.beforeJpPriceFileCount, $worker.afterJpPriceFileCount)
        if ($worker.lastSelectedSetIds) {
            $ids = @($worker.lastSelectedSetIds | Select-Object -First 8)
            Write-Host ("  lastSelectedSetIds={0}" -f ($ids -join ","))
        }
        Write-Host ("  startedAtUtc={0} finishedAtUtc={1}" -f $worker.startedAtUtc, $worker.finishedAtUtc)
    }

    Write-Host ""
    $import = Read-JsonFile -Path $importLatest
    Write-Host "Importer latest summary:"
    if ($null -eq $import) {
        Write-Host "  (missing or unreadable)"
    }
    else {
        Write-Host ("  status={0} budgetDecision={1}" -f $import.status, $import.budgetDecision)
        Write-Host ("  apiRequestsUsed={0} importedRecords={1} endpointFailures={2}" -f $import.apiRequestsUsed, $import.importedRecords, $import.endpointFailures)
        Write-Host ("  hourlyUsed={0} hourlyRemaining={1} dailyUsed={2} dailyRemaining={3}" -f $import.hourlyUsed, $import.hourlyRemaining, $import.dailyUsed, $import.dailyRemaining)
        Write-Host ("  finishedAtUtc={0}" -f $import.finishedAtUtc)
        if ($import.selectedSetIds) {
            $ids = @($import.selectedSetIds | Select-Object -First 8)
            Write-Host ("  selectedSetIds={0}" -f ($ids -join ","))
        }
    }

    Write-Host ""
    Write-Host "Git status --short:"
    $gitStatus = git status --short
    if (-not $gitStatus) {
        Write-Host "  clean"
    }
    else {
        $gitStatus | ForEach-Object { Write-Host ("  {0}" -f $_) }
    }

    Write-Host ""
    Write-Host "Last 5 worker run entries (jsonl):"
    if (-not (Test-Path $workerRuns)) {
        Write-Host "  (missing)"
    }
    else {
        $tail = Get-Content -Path $workerRuns -Tail 5
        foreach ($line in $tail) {
            try {
                $entry = $line | ConvertFrom-Json
                Write-Host ("  {0} status={1} stopReason={2} cycles={3}/{4} imported={5}" -f $entry.finishedAtUtc, $entry.status, $entry.stopReason, $entry.cyclesCompleted, $entry.cyclesAttempted, $entry.totalImportedRecords)
            }
            catch {
                Write-Host ("  {0}" -f $line)
            }
        }
    }

    Write-Host ""
    Write-Host "Matching worker/importer processes:"
    $matches = Get-CimInstance Win32_Process |
        Where-Object {
            ($_.Name -match "python") -and
            ($_.CommandLine -match "run_pokewallet_missing_price_worker.py|import_pokewallet_set_prices.py")
        } |
        Select-Object ProcessId, Name, CommandLine

    if (-not $matches) {
        Write-Host "  none"
    }
    else {
        foreach ($proc in $matches) {
            Write-Host ("  pid={0} name={1}" -f $proc.ProcessId, $proc.Name)
            Write-Host ("    {0}" -f $proc.CommandLine)
        }
    }

    Write-Host ""
    Write-Host ("Refreshing in {0}s (Ctrl+C to stop)..." -f $RefreshSeconds)
    Start-Sleep -Seconds ([Math]::Max(1, $RefreshSeconds))
}
