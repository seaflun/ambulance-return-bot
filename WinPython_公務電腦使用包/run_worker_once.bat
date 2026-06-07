@echo off
setlocal
cd /d "%~dp0"
set WORKER_RUN_ONCE=true
py -u worker.py
pause
