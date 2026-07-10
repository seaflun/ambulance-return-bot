@echo off
setlocal
if /I "%~1"=="--minimized" goto run_update
start "" /min "%~f0" --minimized
exit /b 0

:run_update
cd /d "%~dp0"

echo SinpoSmart Ambulance Worker package updater
echo Package: %CD%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$path = Join-Path (Get-Location) 'update_package.ps1'; $tokens = $null; $errors = $null; [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) | Out-Null; if ($errors.Count) { $errors | ForEach-Object { Write-Host ('[WARN] Updater parse error: ' + $_.Message) }; exit 1 }"
if errorlevel 1 (
  echo.
  echo [WARN] update_package.ps1 is broken. Trying self repair...
  if not exist "%~dp0repair_update_package.ps1" (
    echo [ERROR] repair_update_package.ps1 is missing.
    exit /b 1
  )
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0repair_update_package.ps1"
  if errorlevel 1 (
    echo.
    echo [ERROR] Could not repair update_package.ps1.
    exit /b 1
  )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_package.ps1"
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
set "UPDATE_EXIT=%ERRORLEVEL%"
if not "%UPDATE_EXIT%"=="0" goto update_failed

echo.
echo [OK] Update check completed.
exit /b 0

:update_failed
if errorlevel 1 (
  echo.
  echo [ERROR] Update failed.
  exit /b 1
)
