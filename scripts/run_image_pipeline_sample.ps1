param(
    [string]$Languages = "en,jp",
    [int]$SampleLimit = 100,
    [string]$SetId = "",
    [string]$OutputDir = "reports",
    [switch]$Execute
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

. "$PSScriptRoot\load_supabase_env.ps1"

$argsList = @("sample", "--languages", $Languages, "--sample-limit", "$SampleLimit", "--output-dir", $OutputDir)
if ($SetId) {
    $argsList += @("--set-id", $SetId)
}
if ($Execute) {
    $argsList += "--execute"
}

python tools/image_pipeline.py @argsList
