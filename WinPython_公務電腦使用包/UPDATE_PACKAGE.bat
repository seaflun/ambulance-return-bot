@echo off
setlocal
cd /d "%~dp0"

echo Ambulance return worker package updater
echo Package: %CD%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_package.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] Update failed.
  pause
  exit /b 1
)

echo.
echo [OK] Update check completed.
pause
