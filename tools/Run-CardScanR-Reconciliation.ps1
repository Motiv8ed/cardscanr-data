# CardScanR read-only metadata reconciliation, with bounded D: drive discovery.
# No install, admin privileges, scheduled-task, production or SQLite writes.
[CmdletBinding()]
param(
    [string]$Database = '',
    [int]$MaximumDirectories = 1200,
    [int]$MaximumDepth = 4
)
$ErrorActionPreference = 'Stop'
if ($MaximumDirectories -lt 1 -or $MaximumDirectories -gt 2500 -or
    $MaximumDepth -lt 0 -or $MaximumDepth -gt 6) {
    throw 'Search bounds invalid; directories 1..2500 and depth 0..6 only.'
}
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $root 'metadata-source.zip'
$reconciler = Join-Path $root 'reconcile_tcgdex_metadata_batch.py'
if (-not (Test-Path -LiteralPath $source -PathType Leaf) -or
    -not (Test-Path -LiteralPath $reconciler -PathType Leaf)) {
    throw 'Portable package incomplete. Keep all extracted files together.'
}

function Find-LocalStagingSnapshots {
    param([int]$MaxDirectories, [int]$MaxDepth)
    $dirs = New-Object System.Collections.ArrayList
    $known = @(
        'D:\CardScanR_worldwide_runtime_20260802',
        'D:\CardScanR_Archive',
        'D:\CardScanR',
        'D:\CardScanR-Data',
        'D:\CardScanR_worldwide',
        'D:\CardScanR_Archive_Exports',
        'D:\Motiv8ed_AI_Bridge'
    )
    foreach ($path in $known) {
        if (Test-Path -LiteralPath $path -PathType Container) {
            [void]$dirs.Add((Get-Item -LiteralPath $path))
        }
    }
    if (Test-Path -LiteralPath 'D:\' -PathType Container) {
        $top = @(Get-ChildItem -LiteralPath 'D:\' -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '(?i)cardscanr|worldwide|catalog|archive|motiv8ed|m8cardscanr|^data$' })
        foreach ($item in $top) { [void]$dirs.Add($item) }
    }
    $dedup = @{}
    $queue = New-Object System.Collections.Queue
    $roots = @()
    foreach ($entry in $dirs) {
        $key = $entry.FullName.TrimEnd('\').ToLowerInvariant()
        if (-not $dedup.ContainsKey($key)) {
            $dedup[$key] = $true
            $roots += $entry.FullName
            $queue.Enqueue(@{ Path = $entry.FullName; Depth = 0 })
        }
    }
    $found = New-Object System.Collections.ArrayList
    $foundKeys = @{}
    $scanned = 0
    $truncated = $false
    $ignore = @('.git', 'node_modules', '.venv', 'venv', '.dart_tool',
                'build', '.gradle', '.next', '.cache', 'tmp', 'temp')
    while ($queue.Count -gt 0) {
        if ($scanned -ge $MaxDirectories) { $truncated = $true; break }
        $entry = $queue.Dequeue()
        $scanned++
        $children = @(Get-ChildItem -LiteralPath $entry.Path -Force -ErrorAction SilentlyContinue)
        foreach ($child in $children) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                continue
            }
            if (-not $child.PSIsContainer) {
                if ($child.Name -match '(?i)^worldwide[-_]catalogue[.]sqlite$') {
                    $key = $child.FullName.ToLowerInvariant()
                    if (-not $foundKeys.ContainsKey($key)) {
                        $foundKeys[$key] = $true
                        [void]$found.Add([pscustomobject]@{
                            Path = $child.FullName
                            Bytes = $child.Length
                            ModifiedLocal = $child.LastWriteTime.ToString('s')
                        })
                    }
                }
            } elseif ($entry.Depth -lt $MaxDepth -and
                      $child.Name -notin $ignore) {
                $queue.Enqueue(@{ Path = $child.FullName; Depth = ($entry.Depth + 1) })
            }
        }
    }
    return [pscustomobject]@{
        SearchedRoots = @($roots)
        CheckedDirectories = $scanned
        MaxDirectories = $MaxDirectories
        MaxDepth = $MaxDepth
        Truncated = $truncated
        Databases = @($found.ToArray())
    }
}

$diagFolder = Join-Path $env:LOCALAPPDATA ('CardScanR\CatalogueReconciliation\' +
    (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
Write-Host 'CardScanR worldwide catalogue: exact read-only reconciliation' -ForegroundColor Cyan
Write-Host 'Searching only targeted CardScanR-related folders on D:, not the whole drive.'
$discovery = $null
if (-not $Database) {
    $discovery = Find-LocalStagingSnapshots -MaxDirectories $MaximumDirectories -MaxDepth $MaximumDepth
    $matches = @($discovery.Databases)
    New-Item -ItemType Directory -Path $diagFolder -Force | Out-Null
    $diagnostic = Join-Path $diagFolder 'SAFE-Database-Locator.json'
    @{
        CheckedAtLocal = (Get-Date).ToString('s')
        Status = if ($matches.Count -eq 0) { 'DATABASE_NOT_LOCATED' }
                 elseif ($matches.Count -eq 1) { 'ONE_CANDIDATE_FOUND' }
                 else { 'MULTIPLE_CANDIDATES_FOUND' }
        Search = $discovery
        DatabaseContentRead = $false
        FilesDeletedOrModified = 0
    } | ConvertTo-Json -Depth 7 |
        Set-Content -LiteralPath $diagnostic -Encoding UTF8
    if ($matches.Count -ne 1 -or $discovery.Truncated) {
        Write-Host "No unambiguous staging snapshot found (candidates: $($matches.Count); bounded scan truncated: $($discovery.Truncated))." -ForegroundColor Yellow
        Write-Host "Safe report: $diagnostic"
        Write-Host 'Please share ONLY SAFE-Database-Locator.json. No database uploads.'
        exit 2
    }
    $Database = $matches[0].Path
} else {
    if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
        throw "Explicit database path does not exist: $Database"
    }
}
$wal = $Database + '-wal'
if ((Test-Path -LiteralPath $wal -PathType Leaf) -and
    (Get-Item -LiteralPath $wal).Length -gt 0) {
    Write-Host 'Active SQLite WAL found: stopping to avoid an inconsistent immutable snapshot.' -ForegroundColor Yellow
    Write-Host 'Use a checkpointed copy of the staging DB; no changes made.'
    exit 3
}
$python = @('C:\Python314\python.exe', 'python.exe', 'py.exe') |
    Where-Object {
        if ([System.IO.Path]::IsPathRooted($_)) {
            Test-Path -LiteralPath $_ -PathType Leaf
        } else {
            [bool](Get-Command $_ -ErrorAction SilentlyContinue)
        }
    } | Select-Object -First 1
if (-not $python) {
    throw 'Python not found on this PC. SAFE locator saved; no changes made.'
}
New-Item -ItemType Directory -Path $diagFolder -Force | Out-Null
Write-Host "Found staging candidate: $Database" -ForegroundColor Green
Write-Host "Last modified: $((Get-Item -LiteralPath $Database).LastWriteTime)"
Write-Host "Review output: $diagFolder"
$verifiedReview = Join-Path $diagFolder "verified-review"
& $python $reconciler --archive $source --database $Database --output $verifiedReview
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Host "Reconciliation stopped ($exitCode). No DB or production changes." -ForegroundColor Yellow
    exit $exitCode
}
Write-Host 'PASS: exact read-only reconciliation report created.' -ForegroundColor Green
Write-Host "Safe report: $(Join-Path $verifiedReview 'report.json')"
Write-Host 'Send ONLY report.json to ChatGPT. Do not upload the database.'
exit 0
