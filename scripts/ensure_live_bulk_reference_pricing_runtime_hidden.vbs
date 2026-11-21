' Hidden launcher for Task Scheduler — no visible console window.
' CardScanR hourly bulk/reference pricing sync (run-once). Does not change sync behaviour.
Option Explicit
Dim fso, sh, scriptDir, ps1, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(scriptDir, "ensure_live_bulk_reference_pricing_runtime.ps1")
cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """ -Action run-once"
' Wait=True so Task Scheduler tracks completion and IgnoreNew can block overlap.
sh.Run cmd, 0, True