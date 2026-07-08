param(
    [string]$Config = "cloudflare_env.local.json",
    [string]$ProjectName = "cardscanr-cache",
    [string]$Branch = "main",
    [switch]$InspectOnly
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$argsList = @(
    "tools/deploy_pages_contract.py",
    "--config", $Config,
    "--project-name", $ProjectName,
    "--branch", $Branch
)
if ($InspectOnly) {
    $argsList += "--inspect-only"
}

python @argsList
exit $LASTEXITCODE
