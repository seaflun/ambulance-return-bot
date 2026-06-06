@echo off
setlocal
cd /d "%~dp0"
set WORKER_RUN_ONCE=false
py -u worker_panel.py
pause
