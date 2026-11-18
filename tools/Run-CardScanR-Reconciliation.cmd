@echo off
setlocal
cd /d "%~dp0"
echo CardScanR: Reconcile multilingual metadata with local worldwide staging (READ ONLY)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-CardScanR-Reconciliation.ps1"
set "CODE=%ERRORLEVEL%"
echo.
echo Finished with exit code %CODE%.
pause
exit /b %CODE%
