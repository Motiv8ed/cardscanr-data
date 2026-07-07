param(
    [string]$Languages = "en,jp",
    [int]$SampleLimit = 0,
    [string]$SetId = "",
    [string]$OutputDir = "reports"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

. "$PSScriptRoot\load_supabase_env.ps1"

$argsList = @("import", "--languages", $Languages, "--execute", "--output-dir", $OutputDir)
if ($SampleLimit -gt 0) {
    $argsList += @("--sample-limit", "$SampleLimit")
}
if ($SetId) {
    $argsList += @("--set-id", $SetId)
}

python tools/image_pipeline.py @argsList
