@echo off
chcp 65001 >nul
cd /d D:\cardscanr-data
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\stop_pokewallet_catalog_worker.ps1
pause
