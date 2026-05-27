<#
.SYNOPSIS
    Loads Supabase environment variables from a local JSON file for the current process.
.DESCRIPTION
    Reads supabase_env.local.json (or a specified file), sets SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY for the current session.
    Does NOT print or log secret values. Fails if required keys are missing. Optionally sets SUPABASE_ANON_KEY if present.
.PARAMETER EnvFile
    Path to the local JSON config file (default: supabase_env.local.json)
.EXAMPLE
    . scripts/load_supabase_env.ps1 supabase_env.local.json
#>
param(
    [string]$EnvFile = "supabase_env.local.json"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $EnvFile)) {
    throw "Supabase config file not found: $EnvFile"
}

try {
    $config = Get-Content -Raw -Path $EnvFile | ConvertFrom-Json
} catch {
    throw "Supabase config file is not valid JSON: $EnvFile"
}

if (-not $config.SUPABASE_URL) {
    throw "SUPABASE_URL missing in $EnvFile"
}
if (-not $config.SUPABASE_SERVICE_ROLE_KEY) {
    throw "SUPABASE_SERVICE_ROLE_KEY missing in $EnvFile"
}

# Set for current process
[System.Environment]::SetEnvironmentVariable("SUPABASE_URL", $config.SUPABASE_URL, "Process")
[System.Environment]::SetEnvironmentVariable("SUPABASE_SERVICE_ROLE_KEY", $config.SUPABASE_SERVICE_ROLE_KEY, "Process")
if ($config.SUPABASE_ANON_KEY) {
    [System.Environment]::SetEnvironmentVariable("SUPABASE_ANON_KEY", $config.SUPABASE_ANON_KEY, "Process")
}

Write-Host "Supabase environment loaded for this session. (Secrets not shown.)"
