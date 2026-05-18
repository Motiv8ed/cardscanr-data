param(
    [string]$RepoRoot = "",
    [int]$MaxRequests = 0,
    [switch]$AllowWhenWorkerLocked
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
Set-Location $RepoRoot

$configPath = Join-Path $RepoRoot 'data\pokewallet_catalog_config.json'
$diagPath = Join-Path $RepoRoot 'public\v1\diagnostics\pokewallet-catalog-foundation-latest.json'
$pythonPath = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $pythonPath)) {
    $pythonPath = 'python'
}

function Get-UtcIso {
    return ([datetime]::UtcNow).ToString('yyyy-MM-ddTHH:mm:ssZ')
}

function Read-JsonFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $null
    }

    try {
        return (Get-Content -Path $Path -Raw -Encoding UTF8 -ErrorAction Stop) | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Write-CycleLog {
    param([string]$Message)

    Write-Host "$(Get-UtcIso) $Message"
}

function Invoke-RepoCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-CycleLog ("RUN {0} {1}" -f $FilePath, ($Arguments -join ' '))
    $output = @(& $FilePath @Arguments 2>&1)
    foreach ($line in $output) {
        Write-Host $line
    }

    return [pscustomobject]@{
        ExitCode = [int]$LASTEXITCODE
        Output = $output
    }
}

function Get-GitStatusLines {
    return @(git -C $RepoRoot status --porcelain)
}

function Get-GitStatusPath {
    param([string]$StatusLine)

    if ($StatusLine.Length -lt 4) {
        return ''
    }

    $path = $StatusLine.Substring(3).Trim()
    if ($path.Contains(' -> ')) {
        $parts = $path.Split(@(' -> '), [System.StringSplitOptions]::None)
        $path = $parts[$parts.Length - 1]
    }
    return ($path -replace '/', '\')
}

function Test-ProcessAlive {
    param([int]$ProcessId)

    try {
        $null = Get-Process -Id $ProcessId -ErrorAction Stop
        return $true
    }
    catch {
        return $false
    }
}

function Test-AllowedDirtyPath {
    param([string]$Path)

    $normalized = $Path.Trim()
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return $true
    }

    $exact = @(
        'data\pokewallet_catalog_full_state.json',
        'data\pokewallet_catalog_worker_status.json',
        'public\v1\diagnostics\pokewallet-catalog-foundation-latest.json',
        'public\v1\index.json',
        'public\v1\api-manifest.json',
        'public\v1\api-notes.json',
        'public\v1\schemas.json',
        '.pokewallet_catalog_worker.lock'
    )
    if ($exact -contains $normalized) {
        return $true
    }
    return $normalized.StartsWith('public\v1\provider-catalog\pokewallet\')
}

function Stop-WithStatus {
    param(
        [string]$Status,
        [string]$Message,
        [int]$ExitCode = 1
    )

    Write-CycleLog $Message
    Write-Host "WORKER_CYCLE_STATUS=$Status"
    Write-Host 'WORKER_CYCLE_COMMIT='
    Write-Host "WORKER_CYCLE_MESSAGE=$Message"
    exit $ExitCode
}

$config = Read-JsonFile -Path $configPath
if ($null -eq $config) {
    Stop-WithStatus -Status 'error' -Message 'Could not read Pokewallet catalogue config.'
}

$workerConfig = $config.fullCatalogueWorker
if ($null -eq $workerConfig) {
    Stop-WithStatus -Status 'error' -Message 'fullCatalogueWorker config is missing.'
}

if ($MaxRequests -le 0) {
    $MaxRequests = [int]($workerConfig.maxRequestsPerCycle)
}
if ($MaxRequests -le 0) {
    $MaxRequests = 80
}

$lockPath = Join-Path $RepoRoot ([string]$workerConfig.lockPath)
if (-not $AllowWhenWorkerLocked -and (Test-Path $lockPath)) {
    $lockData = Read-JsonFile -Path $lockPath
    $existingPid = 0
    if ($null -ne $lockData -and $null -ne $lockData.pid) {
        $existingPid = [int]$lockData.pid
    }
    if ($existingPid -gt 0 -and (Test-ProcessAlive -ProcessId $existingPid)) {
        Stop-WithStatus -Status 'worker_running' -Message "Worker already appears to be running as PID $existingPid."
    }
}

$statusLines = Get-GitStatusLines
$unrelated = @()
foreach ($line in $statusLines) {
    $path = Get-GitStatusPath -StatusLine $line
    if (-not (Test-AllowedDirtyPath -Path $path)) {
        $unrelated += $line
    }
}
if ($unrelated.Count -gt 0) {
    Write-CycleLog 'Unrelated uncommitted changes are present. This cycle will not run.'
    foreach ($line in $unrelated) {
        Write-Host $line
    }
    Stop-WithStatus -Status 'dirty_worktree' -Message 'Stopped before provider calls because unrelated files are dirty.'
}

$exportArgs = @(
    'tools\build_pokewallet_catalog_foundation.py',
    '--full-catalogue',
    '--all-languages',
    '--max-requests',
    [string]$MaxRequests,
    '--resume'
)
$exportResult = Invoke-RepoCommand -FilePath $pythonPath -Arguments $exportArgs
if ($exportResult.ExitCode -ne 0) {
    Stop-WithStatus -Status 'export_failed' -Message "Catalogue exporter failed with exit code $($exportResult.ExitCode)."
}

$diag = Read-JsonFile -Path $diagPath
if ($null -eq $diag) {
    Stop-WithStatus -Status 'error' -Message 'Catalogue diagnostics were not written.'
}

if ([string]$diag.status -eq 'rate_limited') {
    Write-CycleLog 'Provider returned rate limit status. This cycle will stop without commit.'
    Write-Host 'WORKER_CYCLE_STATUS=rate_limited'
    Write-Host 'WORKER_CYCLE_COMMIT='
    Write-Host 'WORKER_CYCLE_MESSAGE=Provider returned rate limit status.'
    exit 0
}

if ([bool]$workerConfig.validateAfterCycle) {
    $validateResult = Invoke-RepoCommand -FilePath $pythonPath -Arguments @('tools\validate_cache.py')
    if ($validateResult.ExitCode -ne 0) {
        Stop-WithStatus -Status 'validation_failed' -Message "Validation failed with exit code $($validateResult.ExitCode)."
    }
}

$changedLines = Get-GitStatusLines
if ($changedLines.Count -eq 0) {
    Write-CycleLog 'No changes after catalogue cycle.'
    Write-Host 'WORKER_CYCLE_STATUS=no_changes'
    Write-Host 'WORKER_CYCLE_COMMIT='
    Write-Host 'WORKER_CYCLE_MESSAGE=no changes'
    exit 0
}

$stagePaths = @(
    'data\pokewallet_catalog_full_state.json',
    'public\v1\provider-catalog\pokewallet',
    'public\v1\diagnostics\pokewallet-catalog-foundation-latest.json',
    'public\v1\index.json',
    'public\v1\api-manifest.json',
    'public\v1\api-notes.json',
    'public\v1\schemas.json'
)

foreach ($path in $stagePaths) {
    if (Test-Path (Join-Path $RepoRoot $path)) {
        git -C $RepoRoot add -- $path
        if ($LASTEXITCODE -ne 0) {
            Stop-WithStatus -Status 'git_stage_failed' -Message "Failed to stage $path."
        }
    }
}

git -C $RepoRoot diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-CycleLog 'No expected catalogue changes were staged.'
    Write-Host 'WORKER_CYCLE_STATUS=no_changes'
    Write-Host 'WORKER_CYCLE_COMMIT='
    Write-Host 'WORKER_CYCLE_MESSAGE=no expected catalogue changes'
    exit 0
}

$commitHash = ''
if ([bool]$workerConfig.commitAfterCycle) {
    $commitMessage = [string]$workerConfig.commitMessage
    if ([string]::IsNullOrWhiteSpace($commitMessage)) {
        $commitMessage = 'Expand Pokewallet provider catalogue export'
    }
    $commitResult = Invoke-RepoCommand -FilePath 'git' -Arguments @('-C', $RepoRoot, 'commit', '-m', $commitMessage)
    if ($commitResult.ExitCode -ne 0) {
        Stop-WithStatus -Status 'git_commit_failed' -Message "Git commit failed with exit code $($commitResult.ExitCode)."
    }
    $commitHash = (git -C $RepoRoot rev-parse --short HEAD).Trim()

    if ([bool]$workerConfig.pushAfterCycle) {
        $pushResult = Invoke-RepoCommand -FilePath 'git' -Arguments @('-C', $RepoRoot, 'push')
        if ($pushResult.ExitCode -ne 0) {
            Stop-WithStatus -Status 'git_push_failed' -Message "Git push failed with exit code $($pushResult.ExitCode)."
        }
    }
}
else {
    Write-CycleLog 'commitAfterCycle is false; changes are staged but not committed.'
}

Write-CycleLog 'Catalogue cycle completed.'
Write-Host 'WORKER_CYCLE_STATUS=ok'
Write-Host "WORKER_CYCLE_COMMIT=$commitHash"
Write-Host 'WORKER_CYCLE_MESSAGE=cycle completed'
