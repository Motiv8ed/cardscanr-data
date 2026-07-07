param(
    [string]$Languages = "en,jp",
    [int]$SampleLimit = 100,
    [string]$OutputDir = "reports",
    [switch]$Execute
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

. "$PSScriptRoot\load_supabase_env.ps1"

$argsList = @("verify", "--languages", $Languages, "--sample-limit", "$SampleLimit", "--output-dir", $OutputDir)
if ($Execute) {
    $argsList += "--execute"
}

python tools/image_pipeline.py @argsList
