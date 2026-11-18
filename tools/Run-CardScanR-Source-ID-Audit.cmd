@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-CardScanR-Source-ID-Audit.ps1"
echo.
echo Completed with exit code %ERRORLEVEL%.
pause
