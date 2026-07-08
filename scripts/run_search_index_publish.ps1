param(
    [string]$OutputDir = "public/v1/catalog/pokemon/search",
    [string]$Config = "",
    [switch]$Execute,
    [switch]$SkipTests,
    [switch]$SkipLiveVerification
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$argsList = @("tools/publish_search_index.py", "--output-dir", $OutputDir)
if ($Config) {
    $argsList += @("--config", $Config)
}
if ($Execute) {
    $argsList += "--execute"
} else {
    $argsList += "--dry-run"
}
if ($SkipTests) {
    $argsList += "--skip-tests"
}
if ($SkipLiveVerification) {
    $argsList += "--skip-live-verification"
}

python @argsList
exit $LASTEXITCODE
