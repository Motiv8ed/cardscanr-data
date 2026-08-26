' Hidden launcher for Task Scheduler — no visible console window.
' CardScanR live eBay ensure (worker + scheduler). Does not change pricing behavior.
Option Explicit
Dim sh, ps1, cmd
ps1 = "D:\cardscanr-data\scripts\ensure_live_ebay_pricing_runtime.ps1"
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """ -Component both"
' Wait=True so Task Scheduler tracks completion and IgnoreNew can block overlap.
sh.Run cmd, 0, True
