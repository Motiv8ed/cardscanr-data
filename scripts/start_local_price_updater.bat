@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start_local_price_updater.ps1" -BatchSize 10 -IntervalMinutes 120
endlocal
