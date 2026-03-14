@echo off
setlocal
cd /d "%~dp0"
start "ERPNext Sync" cmd /c python gui.py
endlocal
