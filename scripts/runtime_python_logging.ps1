# Shared Windows PowerShell 5.1 helpers for CardScanR Python runtime wrappers.
# Dot-source from ensure_*_runtime.ps1 scripts. Do not run this file directly.

function Format-ProcessArgument {
    param([string]$Value)
    if ($null -eq $Value) { return '""' }
    if ($Value -match '[\s"]') {
        return '"' + ($Value.Replace('"', '\"')) + '"'
    }
    return $Value
}

function Add-RuntimeLogText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyString()][string]$Text
    )
    if ([string]::IsNullOrEmpty($Text)) { return }
    if (-not $Text.EndsWith("`n")) {
        $Text += "`r`n"
    }
    # Match an existing Windows PowerShell `>>` log (UTF-16 LE BOM). New logs
    # are UTF-8 without a BOM so Notepad can open a fresh err.log.
    $encoding = New-Object System.Text.UTF8Encoding $false
    if (Test-Path -LiteralPath $Path) {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $bom = New-Object byte[] 2
            $read = $stream.Read($bom, 0, 2)
            if ($read -ge 2 -and $bom[0] -eq 255 -and $bom[1] -eq 254) {
                $encoding = New-Object System.Text.UnicodeEncoding $false, $false
            }
        } finally {
            $stream.Dispose()
        }
    }
    [System.IO.File]::AppendAllText($Path, $Text, $encoding)
}

function Invoke-LoggedPython {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath
    )
    # Windows PowerShell 5.1 with $ErrorActionPreference=Stop turns the first
    # native stderr line into a terminating NativeCommandError. Redirect the
    # pipes on the process so PowerShell never sees stderr.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $PythonPath
    $psi.WorkingDirectory = (Get-Location).Path
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $quoted = foreach ($arg in $ArgumentList) { Format-ProcessArgument $arg }
    $psi.Arguments = ($quoted -join ' ')
    # Python writes UTF-8; decode both redirected pipes as UTF-8. Available on
    # .NET Framework / Windows PowerShell 5.1 ProcessStartInfo.
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $psi.StandardOutputEncoding = $utf8
    $psi.StandardErrorEncoding = $utf8
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    [void]$proc.WaitForExit()
    $stdoutText = [string]$stdoutTask.GetAwaiter().GetResult()
    $stderrText = [string]$stderrTask.GetAwaiter().GetResult()
    $exitCode = $proc.ExitCode
    if ($null -eq $exitCode) { $exitCode = 1 }
    $proc.Dispose()
    Add-RuntimeLogText -Path $StdoutPath -Text $stdoutText
    Add-RuntimeLogText -Path $StderrPath -Text $stderrText
    return $exitCode
}
