# Offline read-only probe against the owner-provided, SHA-verified 480-row report.
# No admin, no installs, no SQLite writes, no uploads, no catalogue publication.
[CmdletBinding()]
param(
  [string]$Database = 'D:\CardScanR_Data\worldwide_runtime\staging\worldwide-catalogue.sqlite'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($name in @('audit_source_ids_readonly.py', 'report.json', 'reconciliation.jsonl')) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $name) -PathType Leaf)) {
        throw "Missing package file: $name. Keep all extracted files together."
    }
}
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
    throw "Staging DB missing at $Database; no changes."
}
$wal = "$Database-wal"
if ((Test-Path -LiteralPath $wal -PathType Leaf) -and
    (Get-Item -LiteralPath $wal).Length -gt 0) {
    throw "Active SQLite WAL; use a checkpointed copy. No changes."
}
$python = @('C:\Python314\python.exe', 'python.exe', 'py.exe') |
    Where-Object {
      if ([System.IO.Path]::IsPathRooted($_)) { Test-Path -LiteralPath $_ -PathType Leaf }
      else { [bool](Get-Command $_ -ErrorAction SilentlyContinue) }
    } | Select-Object -First 1
if (-not $python) {
    throw "Python was not found on PATH. No changes."
}
$dest = Join-Path $env:LOCALAPPDATA (
    'CardScanR\CatalogueSourceAudit\' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
Write-Host 'CardScanR provider source audit: 480 pre-matched entries, read-only' -ForegroundColor Cyan
Write-Host 'Identifies stored provider IDs only, not the current app catalogue version.'
& $python (Join-Path $root 'audit_source_ids_readonly.py') --report (Join-Path $root 'report.json') --reconciliation (Join-Path $root 'reconciliation.jsonl') --database $Database --output $dest
if ($LASTEXITCODE -ne 0) {
    throw "Source audit failed safely with exit code $LASTEXITCODE."
}
Write-Host "PASS: $(Join-Path $dest 'SAFE-Provider-ID-Audit.json')" -ForegroundColor Green
Write-Host 'Share only SAFE-Provider-ID-Audit.json, not the 3 GB database.'
