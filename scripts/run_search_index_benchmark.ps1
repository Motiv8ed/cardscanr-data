param(
    [string]$OutputDir = "public/v1/catalog/pokemon/search"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
python tools/benchmark_search_index.py --output-dir $OutputDir
exit $LASTEXITCODE
