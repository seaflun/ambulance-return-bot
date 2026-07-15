@echo off
setlocal
cd /d "%~dp0"
set WORKER_RUN_ONCE=false
wscript.exe "%~dp0RUN_WORKER_GUI_WINPYTHON.vbs"
exit /b %ERRORLEVEL%
