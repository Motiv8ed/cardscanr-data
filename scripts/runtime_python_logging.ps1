# Shared Windows PowerShell 5.1 helpers for CardScanR Python runtime wrappers.
# Dot-source from ensure_*_runtime.ps1 scripts. Do not run this file directly.

function Format-ProcessArgument {
    param(
        [AllowEmptyString()]
        [AllowNull()]
        [string]$Value
    )
    if ($null -eq $Value) { $Value = '' }
    # Empty arguments must be emitted as "" or ProcessStartInfo drops them.
    $needsQuotes = ($Value.Length -eq 0) -or ($Value -match '[\s"]')
    if (-not $needsQuotes) {
        return $Value
    }
    # Windows CommandLineToArgvW rules: escape quotes, and double backslashes
    # that immediately precede a quote (including the closing quote).
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashCount = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq [char]'\' ) {
            $backslashCount++
            continue
        }
        if ($ch -eq [char]'"') {
            if ($backslashCount -gt 0) {
                [void]$sb.Append([string]::new([char]'\', $backslashCount * 2))
                $backslashCount = 0
            }
            [void]$sb.Append('\"')
            continue
        }
        if ($backslashCount -gt 0) {
            [void]$sb.Append([string]::new([char]'\', $backslashCount))
            $backslashCount = 0
        }
        [void]$sb.Append($ch)
    }
    if ($backslashCount -gt 0) {
        [void]$sb.Append([string]::new([char]'\', $backslashCount * 2))
    }
    [void]$sb.Append('"')
    return $sb.ToString()
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

    # Drain both pipes on the main thread while the worker runs so a long or
    # stalled sync still leaves inspectable logs. ReadLineAsync is on .NET 4.5+
    # StreamReader (Windows PowerShell 5.1); avoids event-handler closure issues.
    $stdoutPending = $proc.StandardOutput.ReadLineAsync()
    $stderrPending = $proc.StandardError.ReadLineAsync()
    $stdoutDone = $false
    $stderrDone = $false
    while (-not ($stdoutDone -and $stderrDone)) {
        $wait = New-Object System.Collections.Generic.List[System.Threading.Tasks.Task]
        if (-not $stdoutDone) { [void]$wait.Add($stdoutPending) }
        if (-not $stderrDone) { [void]$wait.Add($stderrPending) }
        if ($wait.Count -eq 0) { break }
        [void][System.Threading.Tasks.Task]::WaitAny($wait.ToArray(), 200)

        if (-not $stdoutDone -and $stdoutPending.IsCompleted) {
            $line = $stdoutPending.GetAwaiter().GetResult()
            if ($null -eq $line) {
                $stdoutDone = $true
            } else {
                Add-RuntimeLogText -Path $StdoutPath -Text ($line + "`r`n")
                $stdoutPending = $proc.StandardOutput.ReadLineAsync()
            }
        }
        if (-not $stderrDone -and $stderrPending.IsCompleted) {
            $line = $stderrPending.GetAwaiter().GetResult()
            if ($null -eq $line) {
                $stderrDone = $true
            } else {
                Add-RuntimeLogText -Path $StderrPath -Text ($line + "`r`n")
                $stderrPending = $proc.StandardError.ReadLineAsync()
            }
        }
    }
    [void]$proc.WaitForExit()

    $exitCode = $proc.ExitCode
    if ($null -eq $exitCode) { $exitCode = 1 }
    $proc.Dispose()
    return $exitCode
}
