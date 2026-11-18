# Offline, read-only CardScanR catalogue reconciliation.
# Does not install software, alter staging, change Windows tasks or publish anything.
[CmdletBinding()]
param(
    [string]$Database = 'D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $root 'metadata-source.zip'
$reconciler = Join-Path $root 'reconcile_tcgdex_metadata_batch.py'
if (-not (Test-Path -LiteralPath $source -PathType Leaf) -or -not (Test-Path -LiteralPath $reconciler -PathType Leaf)) {
    throw 'Portable package incomplete. Keep all extracted files together.'
}
$python = @('C:\Python314\python.exe', 'python.exe') |
    Where-Object {
        if ([System.IO.Path]::IsPathRooted($_)) { Test-Path -LiteralPath $_ -PathType Leaf }
        else { [bool](Get-Command $_ -ErrorAction SilentlyContinue) }
    } |
    Select-Object -First 1
if (-not $python) { throw 'Python not found on this PC.' }
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
    throw "Worldwide staging SQLite file not found at expected path: $Database. No changes made."
}
$wal = $Database + '-wal'
if ((Test-Path -LiteralPath $wal -PathType Leaf) -and (Get-Item -LiteralPath $wal).Length -gt 0) {
    throw 'Active staging SQLite WAL detected. No changes made. A checkpointed copy is required for safe immutable reconciliation.'
}
$output = Join-Path $env:LOCALAPPDATA ('CardScanR\CatalogueReconciliation\' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
Write-Host 'CardScanR worldwide catalogue: read-only exact metadata matching' -ForegroundColor Cyan
Write-Host 'Input: verified TCGdex provider snapshot; no image files'
Write-Host 'Database: read-only; canonical IDs and variants will never be updated'
Write-Host "Review output: $output"
& $python $reconciler --archive $source --database $Database --output $output
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Host "Reconciliation stopped with exit code $exitCode. No production changes." -ForegroundColor Yellow
    exit $exitCode
}
Write-Host ''
Write-Host 'PASS: read-only reconciliation report created.' -ForegroundColor Green
Write-Host "Safe report: $(Join-Path $output 'report.json')"
Write-Host "Private review detail: $(Join-Path $output 'reconciliation.jsonl')"
Write-Host 'Send ONLY report.json to ChatGPT; no database or secrets needed.'
exit 0
