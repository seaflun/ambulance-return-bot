@echo off
setlocal
cd /d "%~dp0"

for /f "usebackq delims=" %%F in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0find_winpython.ps1" -Windowed`) do (
  set "PYTHONW_EXE=%%F"
  goto :found_pythonw
)

:found_pythonw
if not defined PYTHONW_EXE (
  echo [ERROR] Cannot find WinPython pythonw.exe.
  echo Run SETUP_WINPYTHON.bat first, or set WINPYTHON_DIR to the WinPython folder.
  pause
  exit /b 1
)

set WORKER_RUN_ONCE=false
start "" "%PYTHONW_EXE%" "%~dp0worker_gui.py"
