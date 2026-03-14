@echo off
setlocal
cd /d "%~dp0"
start "ERPNext Sync" cmd /k python erpnext_sync.py
endlocal
