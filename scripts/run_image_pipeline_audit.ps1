param(
    [string]$Languages = "en,jp",
    [int]$SampleLimit = 0,
    [string]$OutputDir = "reports"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

. "$PSScriptRoot\load_supabase_env.ps1"

$argsList = @("audit", "--languages", $Languages, "--output-dir", $OutputDir)
if ($SampleLimit -gt 0) {
    $argsList += @("--sample-limit", "$SampleLimit")
}

python tools/image_pipeline.py @argsList
