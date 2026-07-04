<#
.SYNOPSIS
    Loads Supabase environment variables for the current PowerShell process.
.DESCRIPTION
    Uses existing SUPABASE_URL and SUPABASE_SECRET_KEY values when present.
    Otherwise reads supabase_env.local.json, or another supplied JSON file, and
    sets SUPABASE_URL, SUPABASE_SECRET_KEY, the deprecated compatibility
    SUPABASE_SERVICE_ROLE_KEY value, and optional SUPABASE_ANON_KEY.
    Secret values are never printed.
#>
param(
    [string]$EnvFile = "supabase_env.local.json"
)

$ErrorActionPreference = "Stop"

$existingUrl = [Environment]::GetEnvironmentVariable("SUPABASE_URL", "Process")
if ([string]::IsNullOrWhiteSpace($existingUrl)) {
    $existingUrl = [Environment]::GetEnvironmentVariable("SUPABASE_URL", "User")
}
$existingServiceKey = [Environment]::GetEnvironmentVariable("SUPABASE_SECRET_KEY", "Process")
if ([string]::IsNullOrWhiteSpace($existingServiceKey)) {
    $existingServiceKey = [Environment]::GetEnvironmentVariable("SUPABASE_SECRET_KEY", "User")
}
if ([string]::IsNullOrWhiteSpace($existingServiceKey)) {
    $existingServiceKey = [Environment]::GetEnvironmentVariable("SUPABASE_SERVICE_ROLE_KEY", "Process")
}
if ([string]::IsNullOrWhiteSpace($existingServiceKey)) {
    $existingServiceKey = [Environment]::GetEnvironmentVariable("SUPABASE_SERVICE_ROLE_KEY", "User")
}

if (-not [string]::IsNullOrWhiteSpace($existingUrl) -and -not [string]::IsNullOrWhiteSpace($existingServiceKey)) {
    [Environment]::SetEnvironmentVariable("SUPABASE_URL", $existingUrl, "Process")
    [Environment]::SetEnvironmentVariable("SUPABASE_SECRET_KEY", $existingServiceKey, "Process")
    [Environment]::SetEnvironmentVariable("SUPABASE_SERVICE_ROLE_KEY", $existingServiceKey, "Process")
    Write-Host "Supabase environment already set for this session. (Secrets not shown.)"
    return
}

if (-not (Test-Path $EnvFile)) {
    throw "Supabase env vars are not set and config file was not found: $EnvFile"
}

try {
    $config = Get-Content -Raw -Path $EnvFile | ConvertFrom-Json
} catch {
    throw "Supabase config file is not valid JSON: $EnvFile"
}

if (-not $config.SUPABASE_URL) {
    throw "SUPABASE_URL missing in $EnvFile"
}
$configuredSecret = $config.SUPABASE_SECRET_KEY
if (-not $configuredSecret) {
    $configuredSecret = $config.SUPABASE_SERVICE_ROLE_KEY
}
if (-not $configuredSecret) {
    throw "SUPABASE_SECRET_KEY missing in $EnvFile"
}

[Environment]::SetEnvironmentVariable("SUPABASE_URL", $config.SUPABASE_URL, "Process")
[Environment]::SetEnvironmentVariable("SUPABASE_SECRET_KEY", $configuredSecret, "Process")
[Environment]::SetEnvironmentVariable("SUPABASE_SERVICE_ROLE_KEY", $configuredSecret, "Process")
if ($config.SUPABASE_ANON_KEY) {
    [Environment]::SetEnvironmentVariable("SUPABASE_ANON_KEY", $config.SUPABASE_ANON_KEY, "Process")
}

Write-Host "Supabase environment loaded for this session. (Secrets not shown.)"
