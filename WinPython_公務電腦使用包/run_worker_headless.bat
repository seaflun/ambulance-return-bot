@echo off
setlocal
cd /d "%~dp0"

for /f "usebackq delims=" %%F in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0find_winpython.ps1"`) do (
  set "PYTHON_EXE=%%F"
  goto :found_python
)

:found_python
if not defined PYTHON_EXE (
  echo [ERROR] Cannot find WinPython python.exe.
  echo Run SETUP_WINPYTHON.bat first, or set WINPYTHON_DIR to the WinPython folder.
  exit /b 1
)

set WORKER_RUN_ONCE=false
set WORKER_RUNTIME_MODE=headless
"%PYTHON_EXE%" -u "%~dp0worker.py"
exit /b %ERRORLEVEL%
