param(
    [string]$Languages = "en,jp",
    [int]$SampleLimit = 0,
    [string]$OutputDir = "reports"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

python tools/image_pipeline.py report --languages $Languages --output-dir $OutputDir $(if ($SampleLimit -gt 0) { @("--sample-limit", "$SampleLimit") })
