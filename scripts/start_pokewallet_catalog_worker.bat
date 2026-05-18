@echo off
chcp 65001 >nul
cd /d D:\cardscanr-data
echo Starting CardScanR PokéWallet Catalogue Worker in a new PowerShell window...
echo Logs: logs\pokewallet_catalog_worker.log
echo Status: data\pokewallet_catalog_worker_status.json
start "CardScanR PokéWallet Catalogue Worker" powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit -Command "$Host.UI.RawUI.WindowTitle='CardScanR PokéWallet Catalogue Worker'; & 'D:\cardscanr-data\scripts\run_pokewallet_catalog_worker_loop.ps1'"
