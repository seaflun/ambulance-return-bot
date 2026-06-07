@echo off
setlocal
cd /d "%~dp0"
set WORKER_RUN_ONCE=false
start "" pyw -3 worker_gui.py
exit /b
