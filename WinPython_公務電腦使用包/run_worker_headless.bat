@echo off
setlocal
cd /d "%~dp0"
set WORKER_RUN_ONCE=false
set WORKER_RUNTIME_MODE=headless
py -u "%~dp0worker.py"
pause
