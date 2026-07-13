param(
    [string]$ProjectRoot,
    [string]$Version,
    [string]$OutputDir,
    [switch]$SkipSourceVersionUpdate,
    [switch]$BuildLockAlreadyHeld
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = Get-Date -Format "yyyy.MM.dd.HHmm"
}
if ($Version -notmatch "^\d{4}\.\d{2}\.\d{2}\.\d{4}$") {
    throw "Version must use yyyy.MM.dd.HHmm format. Got: $Version"
}

$project = (Resolve-Path -LiteralPath $ProjectRoot).Path
$packageName = "WinPython_公務電腦使用包"
$packageDir = Join-Path $project $packageName
$updateDir = if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    Join-Path $project "UPDATE"
} else {
    [System.IO.Path]::GetFullPath($OutputDir)
}
$finalZipPath = Join-Path $updateDir "$packageName.zip"
$finalShaPath = "$finalZipPath.sha256.txt"
$releaseVersionAsset = "ambulance-return-version.txt"
$releaseZipAsset = "ambulance-return-public-package.zip"
$releaseShaAsset = "ambulance-return-public-package.zip.sha256.txt"
$releaseUpdaterAsset = "update_package.ps1"
$manifestName = "UPDATE_MANIFEST.json"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stageRoot = Join-Path (Join-Path $project "tmp") "public-duty-package-$stamp-$([guid]::NewGuid().ToString('N'))"
$stagePackageDir = Join-Path $stageRoot $packageName
$assetStageDir = Join-Path $stageRoot "assets"
$publishRollbackDir = Join-Path $stageRoot "publish-rollback"

function Resolve-FullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-UnderPath {
    param(
        [string]$Path,
        [string]$Root
    )
    $fullPath = Resolve-FullPath -Path $Path
    $fullRoot = (Resolve-FullPath -Path $Root).TrimEnd([char]92) + [string][char]92
    if (-not ($fullPath + [string][char]92).StartsWith($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside expected root. Path=$fullPath Root=$fullRoot"
    }
}

function Remove-SafeDirectory {
    param(
        [string]$Path,
        [string]$ExpectedRoot
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    Assert-UnderPath -Path $Path -Root $ExpectedRoot
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Enter-PackageBuildLock {
    param([string]$LockPath)

    $lockDir = Split-Path -Parent $LockPath
    New-Item -ItemType Directory -Path $lockDir -Force | Out-Null
    try {
        $stream = [System.IO.File]::Open(
            $LockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        throw "Another package build is already in progress: $LockPath"
    }
    try {
        $metadata = [System.Text.Encoding]::UTF8.GetBytes("pid=$PID started_utc=$([DateTime]::UtcNow.ToString('o'))")
        $stream.SetLength(0)
        $stream.Write($metadata, 0, $metadata.Length)
        $stream.Flush($true)
        return $stream
    } catch {
        $stream.Dispose()
        throw
    }
}

function Restore-PublishedFiles {
    param([object[]]$Entries)

    $errors = @()
    $reverseEntries = @($Entries)
    [array]::Reverse($reverseEntries)
    foreach ($entry in $reverseEntries) {
        if ($entry.Published) {
            try {
                if (Test-Path -LiteralPath $entry.Target -PathType Leaf) {
                    Remove-Item -LiteralPath $entry.Target -Force
                }
                $entry.Published = $false
            } catch {
                $errors += "remove published target $($entry.Target): $($_.Exception.Message)"
            }
        }
        if ($entry.HadExisting -and -not $entry.Published) {
            try {
                if (-not (Test-Path -LiteralPath $entry.Backup -PathType Leaf)) {
                    throw "backup is missing"
                }
                Move-Item -LiteralPath $entry.Backup -Destination $entry.Target -Force
                $entry.HadExisting = $false
            } catch {
                $errors += "restore backup $($entry.Backup): $($_.Exception.Message)"
            }
        }
        try {
            if (Test-Path -LiteralPath $entry.Temp -PathType Leaf) {
                Remove-Item -LiteralPath $entry.Temp -Force
            }
        } catch {
            $errors += "remove publish temp $($entry.Temp): $($_.Exception.Message)"
        }
    }
    if ($errors.Count -gt 0) {
        throw "Public package rollback incomplete: $($errors -join '; ')"
    }
}

function Publish-StagedFiles {
    param(
        [object[]]$Mappings,
        [string]$RollbackRoot,
        [object]$State
    )

    New-Item -ItemType Directory -Path $RollbackRoot -Force | Out-Null
    $entries = @()
    $index = 0
    $State.Attempted = $true
    foreach ($mapping in $Mappings) {
        if (-not (Test-Path -LiteralPath $mapping.Source -PathType Leaf)) {
            throw "Missing staged release asset: $($mapping.Source)"
        }
        if ((Test-Path -LiteralPath $mapping.Target) -and -not (Test-Path -LiteralPath $mapping.Target -PathType Leaf)) {
            throw "Release asset target is not a file: $($mapping.Target)"
        }
        $targetDir = Split-Path -Parent $mapping.Target
        if (-not (Test-Path -LiteralPath $targetDir -PathType Container)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }
        $temp = Join-Path $targetDir (".{0}.publish-{1}.tmp" -f (Split-Path -Leaf $mapping.Target), [guid]::NewGuid().ToString("N"))
        $backup = Join-Path $RollbackRoot ("{0:D3}.bak" -f $index)
        Copy-Item -LiteralPath $mapping.Source -Destination $temp -Force
        $entries += [PSCustomObject]@{
            Target = $mapping.Target
            Temp = $temp
            Backup = $backup
            HadExisting = $false
            Published = $false
        }
        $State.Entries = @($entries)
        $index += 1
    }

    foreach ($entry in $entries) {
        if (Test-Path -LiteralPath $entry.Target -PathType Leaf) {
            Move-Item -LiteralPath $entry.Target -Destination $entry.Backup -Force
            $entry.HadExisting = $true
        }
        Move-Item -LiteralPath $entry.Temp -Destination $entry.Target -Force
        $entry.Published = $true
    }
}

function Write-PackageText {
    param(
        [string]$RelativePath,
        [string]$Text,
        [string]$Encoding = "UTF8"
    )
    $target = Join-Path $packageDir $RelativePath
    $targetDir = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir | Out-Null
    }
    $Text.TrimStart() | Set-Content -LiteralPath $target -Encoding $Encoding
}

function Copy-ZipStage {
    param(
        [string]$SourceDir,
        [string]$DestDir
    )
    $skipDirs = @(
        ".git", "artifacts", "logs", "runtime_outputs", "tmp", "temp", "cache", ".cache",
        "local_data", "snapshots", "__pycache__", ".pytest_cache", "chrome_profile",
        "profiles", "runtime_profiles", "selenium_profiles", "browser_profiles"
    )
    $skipFiles = @(".env", "update_urls.json")
    $sourceRoot = (Resolve-FullPath -Path $SourceDir).TrimEnd([char]92) + [string][char]92
    Get-ChildItem -LiteralPath $SourceDir -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($sourceRoot.Length)
        $parts = $relative -split "[\\/]"
        if ($parts | Where-Object { $_ -in $skipDirs }) {
            return
        }
        if ($skipFiles -contains $_.Name) {
            return
        }
        if ($_.Name -match "\.pyc$|\.pyo$|\.pyd$|\.log$") {
            return
        }
        $target = Join-Path $DestDir $relative
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
}

function Write-UpdateManifest {
    param([string]$StagePackageDir)

    $stageRoot = (Resolve-FullPath -Path $StagePackageDir).TrimEnd([char]92) + [string][char]92
    $managedFiles = @(
        Get-ChildItem -LiteralPath $StagePackageDir -Recurse -File -Force |
            ForEach-Object {
                $relative = $_.FullName.Substring($stageRoot.Length) -replace "\\", "/"
                if ($_.Name -in @(".env", "update_urls.json", "UPDATE_PACKAGE.bat")) {
                    return
                }
                $relative
            }
    )
    $managedFiles += $manifestName
    $managedFiles = @($managedFiles | Sort-Object -Unique)
    if ($managedFiles.Count -eq 0 -or $managedFiles.Count -gt 10000) {
        throw "Invalid update manifest file count: $($managedFiles.Count)"
    }
    $payload = [ordered]@{
        schema_version = 1
        files = $managedFiles
    }
    $payload | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $StagePackageDir $manifestName) -Encoding UTF8
}

function Assert-PackageFile {
    param([string]$RelativePath)
    $path = Join-Path $packageDir $RelativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing public-duty package source file: $RelativePath"
    }
}

function Assert-PackageDirectory {
    param([string]$RelativePath)
    $path = Join-Path $packageDir $RelativePath
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "Missing public-duty package source directory: $RelativePath"
    }
}

$buildLockStream = $null
if (-not $BuildLockAlreadyHeld) {
    $buildLockStream = Enter-PackageBuildLock -LockPath (Join-Path (Join-Path $project "UPDATE") ".package-build.lock")
}

try {
if (-not (Test-Path -LiteralPath $packageDir -PathType Container)) {
    throw "Missing public-duty package source directory: $packageDir"
}
New-Item -ItemType Directory -Path $updateDir -Force | Out-Null

foreach ($file in @(
    "app.py",
    "worker_gui.py",
    "worker.py",
    "consumables_login.py",
    "disinfect.py",
    "requirements.txt",
    ".env.example",
    "run_worker_forever.bat",
    "run_worker_forever.vbs",
    "run_worker_headless.bat",
    "run_worker_once.bat",
    "repair_update_package.ps1",
    "UPDATE_PACKAGE.ps1",
    "REMOTE_UPDATE_PACKAGE.ps1"
)) {
    Assert-PackageFile -RelativePath $file
}
foreach ($dir in @("ambulance_bot", "templates")) {
    Assert-PackageDirectory -RelativePath $dir
}

Write-PackageText -RelativePath "README_公務電腦.txt" -Text @'
# SinpoSmart - 救護Worker 公務電腦使用包

## 第一次設定

1. 把 `.env.example` 複製成 `.env`。
2. 填入 `WORKER_TOKEN`，要和 NAS 的值相同。
3. `SELENIUM_PROFILE_ROOT` 預設使用 `%LOCALAPPDATA%\ambulance_return_bot`，不要放到 Google Drive 資料夾；`chrome_profile` 只是舊快取資料，不是四站登打必要條件。
4. 舊 runtime profiles 預設超過 4 小時且未被 Chrome 使用時會清理；登打網頁預設 10 分鐘後自動關閉。
5. Worker GUI 預設啟動後自動縮到系統匣；若要停用可設定 `WORKER_GUI_START_MINIMIZED=false`。
6. 執行 `SETUP_WINPYTHON.bat` 安裝套件、檢查環境，並建立登入後自動啟動工作排程。
7. 平常用 `RUN_WORKER_GUI_WINPYTHON.vbs` 啟動，沒有黑色命令列視窗。

## GitHub 更新

1. 管理端更新專案後執行 `scripts\build_public_duty_package.ps1`。
2. 預設更新來源是 `https://github.com/seaflun/ambulance-return-bot/releases/latest/download`。
3. 建立 SinpoSmart - 救護Worker 專用 GitHub Release，並上傳 `ambulance-return-version.txt`、`ambulance-return-public-package.zip`、`ambulance-return-public-package.zip.sha256.txt`、`update_package.ps1`。
4. 之後按 `UPDATE_PACKAGE.bat` 即可從 GitHub latest release 比對版本、下載 zip、驗證 sha256、備份後更新。
5. 若 GitHub repo 名稱不同，可在 `.env` 設定 `AMBULANCE_RETURN_RELEASE_BASE_URL` 覆蓋下載來源。

`.env`、logs、artifacts、runtime profiles 都不會被更新 zip 覆蓋。
'@

Write-PackageText -RelativePath "find_winpython.ps1" -Text @'
param(
    [switch]$Windowed
)

$exeName = if ($Windowed) { "pythonw.exe" } else { "python.exe" }
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function First-PythonInTree {
    param([string]$Root)

    if ([string]::IsNullOrWhiteSpace($Root) -or -not (Test-Path -LiteralPath $Root)) {
        return $null
    }

    Get-ChildItem -LiteralPath $Root -Filter $exeName -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName |
        Select-Object -First 1
}

function First-PythonUnderWinPythonFolders {
    param([string]$Root)

    if ([string]::IsNullOrWhiteSpace($Root) -or -not (Test-Path -LiteralPath $Root)) {
        return $null
    }

    foreach ($folder in Get-ChildItem -LiteralPath $Root -Directory -Filter "WinPython*" -ErrorAction SilentlyContinue | Sort-Object FullName) {
        $found = First-PythonInTree -Root $folder.FullName
        if ($found) {
            return $found
        }
    }

    return $null
}

$directRoots = @()
if ($env:WINPYTHON_DIR) {
    $directRoots += $env:WINPYTHON_DIR
}
$directRoots += $projectDir

$folderRoots = @()
$folderRoots += $projectDir
$folderRoots += Split-Path -Parent $projectDir
$folderRoots += Join-Path $env:USERPROFILE "Desktop"
$folderRoots += Join-Path $env:USERPROFILE "Downloads"
$folderRoots += "C:\"
$folderRoots += "D:\"
$folderRoots += "G:\"

foreach ($root in $directRoots | Where-Object { $_ } | Select-Object -Unique) {
    $direct = First-PythonInTree -Root $root
    if ($direct -and $direct.FullName -match "WinPython|python-\d") {
        Write-Output $direct.FullName
        exit 0
    }
}

foreach ($root in $folderRoots | Where-Object { $_ } | Select-Object -Unique) {
    $fromWinPythonFolder = First-PythonUnderWinPythonFolders -Root $root
    if ($fromWinPythonFolder) {
        Write-Output $fromWinPythonFolder.FullName
        exit 0
    }
}

$pathCommand = Get-Command $exeName -ErrorAction SilentlyContinue
if ($pathCommand) {
    Write-Output $pathCommand.Source
    exit 0
}

exit 1
'@

Write-PackageText -RelativePath "SETUP_WINPYTHON.bat" -Text @'
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
'@

Write-PackageText -RelativePath "install_startup_shortcut.ps1" -Text @'
param(
    [switch]$WhatIf,
    [switch]$SkipScheduledTask
)

$ErrorActionPreference = "Stop"

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $packageDir "RUN_WORKER_GUI_WINPYTHON.vbs"
$taskName = "AmbulanceReturnWorker"
$shortcutName = "AmbulanceReturnWorker.lnk"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"
$startupDisabledValues = @("0", "false", "no", "off")

function Get-PackageEnvValue {
    param([string]$Name)

    $envPath = Join-Path $packageDir ".env"
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        return ""
    }
    foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -eq 2 -and $parts[0].Trim() -eq $Name) {
            return $parts[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function StartupLauncherEnabled {
    $value = $env:WORKER_STARTUP_LAUNCHER_ENABLED
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = Get-PackageEnvValue -Name "WORKER_STARTUP_LAUNCHER_ENABLED"
    }
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $true
    }
    return -not ($startupDisabledValues -contains $value.Trim().ToLowerInvariant())
}

function Get-StartupDir {
    $startupDir = [Environment]::GetFolderPath("Startup")
    if ([string]::IsNullOrWhiteSpace($startupDir)) {
        $startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
    }
    return $startupDir
}

function Remove-StartupFolderShortcut {
    $shortcutPath = Join-Path (Get-StartupDir) $shortcutName
    if ($WhatIf) {
        Write-Host "Would remove startup folder shortcut: $shortcutPath"
        return
    }
    if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
        Remove-Item -LiteralPath $shortcutPath -Force
        Write-Host "Removed startup folder shortcut: $shortcutPath"
    } else {
        Write-Host "Startup folder shortcut already absent: $shortcutPath"
    }
}

function Disable-StartupLaunchers {
    Remove-StartupFolderShortcut
    if ($WhatIf) {
        Write-Host "Would unregister scheduled task: $taskName"
    } else {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Scheduled task disabled or absent: $taskName"
    }
    Write-Host "Startup launcher disabled by WORKER_STARTUP_LAUNCHER_ENABLED=false"
}

if (-not (StartupLauncherEnabled)) {
    Disable-StartupLaunchers
    exit 0
}

function Install-StartupFolderShortcut {
    $startupDir = Get-StartupDir
    $shortcutPath = Join-Path $startupDir $shortcutName

    if ($WhatIf) {
        Write-Host "Would install startup folder shortcut: $shortcutPath"
        Write-Host "Target: $target"
        return
    }

    if (-not (Test-Path -LiteralPath $startupDir -PathType Container)) {
        New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $wscript
    $shortcut.Arguments = "`"$target`""
    $shortcut.WorkingDirectory = $packageDir
    $shortcut.WindowStyle = 7
    $shortcut.Description = "SinpoSmart - 救護Worker GUI"
    $shortcut.Save()
    Write-Host "Installed startup folder shortcut: $shortcutPath"
    Write-Host "Target: $target"
}

if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Cannot find startup target: $target"
}

if (-not $SkipScheduledTask) {
    $action = New-ScheduledTaskAction -Execute $wscript -Argument "`"$target`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    try {
        $trigger.Delay = "PT1M"
    } catch {
        Write-Warning "Could not set startup delay; the task will start immediately after logon."
    }
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
}

if ($WhatIf) {
    if ($SkipScheduledTask) {
        Write-Host "Would skip scheduled task refresh: $taskName"
    } else {
        Write-Host "Would install scheduled task: $taskName"
    }
    Write-Host "User: $currentUser"
    Write-Host "Target: $target"
    Install-StartupFolderShortcut
    exit 0
}

Install-StartupFolderShortcut
if ($SkipScheduledTask) {
    Write-Host "Skipped scheduled task refresh: $taskName"
    Write-Host "User: $currentUser"
    Write-Host "Target: $target"
    exit 0
}
try {
    Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
    Write-Host "Installed scheduled task: $taskName"
} catch {
    Write-Warning "Could not install scheduled task; startup folder shortcut is installed instead. $($_.Exception.Message)"
}
Write-Host "User: $currentUser"
Write-Host "Target: $target"
exit 0
'@

Write-PackageText -RelativePath "RUN_WORKER_GUI_WINPYTHON.bat" -Text @'
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
'@

Write-PackageText -RelativePath "RUN_WORKER_GUI_WINPYTHON.vbs" -Encoding "ASCII" -Text @'
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir
shell.Run """" & scriptDir & "\RUN_WORKER_GUI_WINPYTHON.bat""", 0, False
'@

Write-PackageText -RelativePath "UPDATE_PACKAGE.bat" -Encoding "ASCII" -Text @'
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
'@

Write-PackageText -RelativePath "check_environment.py" -Text @'
# -*- coding: utf-8 -*-
"""Quick environment check for the SinpoSmart ambulance public-duty worker."""

from __future__ import annotations

import importlib.util
import platform
import sys
import tkinter as tk

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


REQUIRED_MODULES = [
    "dotenv",
    "selenium",
    "ddddocr",
    "PIL",
    "pystray",
]


def ok(message: str) -> None:
    print(f"[OK] {message}")


def fail(message: str) -> None:
    print(f"[FAIL] {message}")
    raise SystemExit(1)


def main() -> int:
    if sys.version_info < (3, 11):
        fail(f"Python version is {platform.python_version()}; Python 3.11+ is required.")
    ok(f"Python {platform.python_version()}")

    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        fail(f"Missing Python packages: {', '.join(missing)}. Run SETUP_WINPYTHON.bat.")
    ok("Required Python packages are installed.")

    try:
        root = tk.Tk()
        root.withdraw()
        root.destroy()
    except Exception as exc:
        fail(f"Tkinter GUI is unavailable: {exc}")
    ok("Tkinter GUI is available.")

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--window-size=1280,900")
    try:
        driver = webdriver.Chrome(options=options)
        driver.get("about:blank")
        driver.quit()
    except Exception as exc:
        fail(f"Chrome / ChromeDriver test failed: {exc}")
    ok("Chrome / ChromeDriver can start.")

    ok("Environment check passed. Start the worker with RUN_WORKER_GUI_WINPYTHON.vbs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'@

# UPDATE_PACKAGE.ps1 is maintained as runtime source so builds cannot overwrite
# rollback/restart logic. AMBULANCE_SKIP_WORKER_RESTART and Start-WorkerGui
# remain part of that updater contract for REMOTE_UPDATE_PACKAGE.ps1.
Assert-PackageFile -RelativePath "UPDATE_PACKAGE.ps1"

$publishCompleted = $false
$publishState = [PSCustomObject]@{
    Attempted = $false
    RollbackComplete = $false
    Entries = @()
}
try {
    Remove-SafeDirectory -Path $stageRoot -ExpectedRoot (Join-Path $project "tmp")
    New-Item -ItemType Directory -Path $stagePackageDir -Force | Out-Null
    New-Item -ItemType Directory -Path $assetStageDir -Force | Out-Null
    Copy-ZipStage -SourceDir $packageDir -DestDir $stagePackageDir
    $Version | Set-Content -LiteralPath (Join-Path $stagePackageDir "VERSION.txt") -Encoding UTF8
    Write-UpdateManifest -StagePackageDir $stagePackageDir

    $zipPath = Join-Path $assetStageDir "$packageName.zip"
    $shaPath = "$zipPath.sha256.txt"
    $releaseVersionPath = Join-Path $assetStageDir $releaseVersionAsset
    $releaseZipPath = Join-Path $assetStageDir $releaseZipAsset
    $releaseShaPath = Join-Path $assetStageDir $releaseShaAsset
    $releaseUpdaterPath = Join-Path $assetStageDir $releaseUpdaterAsset
    $updateVersionStagePath = Join-Path $assetStageDir "VERSION.txt"
    $sourceVersionStagePath = Join-Path $assetStageDir "SOURCE_VERSION.txt"

    Compress-Archive -LiteralPath $stagePackageDir -DestinationPath $zipPath -Force
    $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $packageName.zip" | Set-Content -LiteralPath $shaPath -Encoding UTF8
    $Version | Set-Content -LiteralPath $releaseVersionPath -Encoding UTF8
    Copy-Item -LiteralPath $zipPath -Destination $releaseZipPath -Force
    "$hash  $releaseZipAsset" | Set-Content -LiteralPath $releaseShaPath -Encoding UTF8
    Copy-Item -LiteralPath (Join-Path $packageDir "update_package.ps1") -Destination $releaseUpdaterPath -Force
    $Version | Set-Content -LiteralPath $updateVersionStagePath -Encoding UTF8
    $Version | Set-Content -LiteralPath $sourceVersionStagePath -Encoding UTF8

    if ((Get-FileHash -LiteralPath $releaseZipPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) {
        throw "Staged public/release zip byte hash mismatch."
    }

    $mappings = @(
        [PSCustomObject]@{ Source = $zipPath; Target = $finalZipPath },
        [PSCustomObject]@{ Source = $shaPath; Target = $finalShaPath },
        [PSCustomObject]@{ Source = $releaseVersionPath; Target = (Join-Path $updateDir $releaseVersionAsset) },
        [PSCustomObject]@{ Source = $releaseZipPath; Target = (Join-Path $updateDir $releaseZipAsset) },
        [PSCustomObject]@{ Source = $releaseShaPath; Target = (Join-Path $updateDir $releaseShaAsset) },
        [PSCustomObject]@{ Source = $releaseUpdaterPath; Target = (Join-Path $updateDir $releaseUpdaterAsset) },
        [PSCustomObject]@{ Source = $updateVersionStagePath; Target = (Join-Path $updateDir "VERSION.txt") }
    )
    if (-not $SkipSourceVersionUpdate) {
        $mappings += [PSCustomObject]@{ Source = $sourceVersionStagePath; Target = (Join-Path $packageDir "VERSION.txt") }
    }
    Publish-StagedFiles -Mappings $mappings -RollbackRoot $publishRollbackDir -State $publishState
    $publishCompleted = $true
} catch {
    $publishError = $_
    if ($publishState.Attempted) {
        try {
            Restore-PublishedFiles -Entries $publishState.Entries
            $publishState.RollbackComplete = $true
        } catch {
            throw "Public package publish failed and rollback is incomplete. Recovery files: $publishRollbackDir. Publish error: $($publishError.Exception.Message). Rollback error: $($_.Exception.Message)"
        }
    }
    throw
} finally {
    if ($publishState.Attempted -and -not $publishCompleted -and -not $publishState.RollbackComplete) {
        Write-Warning "Public package rollback was incomplete; recovery files were preserved at $publishRollbackDir"
    } else {
        Remove-SafeDirectory -Path $stageRoot -ExpectedRoot (Join-Path $project "tmp")
    }
}

[PSCustomObject]@{
    Version = $Version
    PackageDir = $packageDir
    UpdateDir = $updateDir
    Zip = $finalZipPath
    Sha256 = (Get-Content -LiteralPath $finalShaPath -Raw -Encoding UTF8).Trim()
    ReleaseAssets = @($releaseVersionAsset, $releaseZipAsset, $releaseShaAsset, $releaseUpdaterAsset) -join ", "
} | Format-List
} finally {
    if ($null -ne $buildLockStream) {
        $buildLockStream.Dispose()
    }
}
