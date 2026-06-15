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
  echo Put WinPython next to this package, or set WINPYTHON_DIR to the WinPython folder.
  pause
  exit /b 1
)

"%PYTHON_EXE%" -m pip install --upgrade pip
"%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
"%PYTHON_EXE%" "%~dp0check_environment.py"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_startup_shortcut.ps1"
if errorlevel 1 (
  echo [WARN] Could not install startup scheduled task. You can still start with RUN_WORKER_GUI_WINPYTHON.vbs.
)
pause
