param(
    [string]$ProjectRoot,
    [string]$Version
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
$updateDir = Join-Path $project "UPDATE"
$zipPath = Join-Path $updateDir "$packageName.zip"
$shaPath = "$zipPath.sha256.txt"
$releaseVersionAsset = "ambulance-return-version.txt"
$releaseZipAsset = "ambulance-return-public-package.zip"
$releaseShaAsset = "ambulance-return-public-package.zip.sha256.txt"
$releaseUpdaterAsset = "update_package.ps1"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stageRoot = Join-Path (Join-Path $project "tmp") "public-duty-package-$stamp"
$stagePackageDir = Join-Path $stageRoot $packageName

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
    $skipDirs = @("artifacts", "logs", "tmp", "temp", "cache", ".cache", "local_data", "__pycache__", ".pytest_cache")
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
    "repair_update_package.ps1"
)) {
    Assert-PackageFile -RelativePath $file
}
foreach ($dir in @("ambulance_bot", "templates")) {
    Assert-PackageDirectory -RelativePath $dir
}

$Version | Set-Content -LiteralPath (Join-Path $packageDir "VERSION.txt") -Encoding UTF8
$Version | Set-Content -LiteralPath (Join-Path $updateDir "VERSION.txt") -Encoding UTF8

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
    pause
    exit /b 1
  )
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0repair_update_package.ps1"
  if errorlevel 1 (
    echo.
    echo [ERROR] Could not repair update_package.ps1.
    pause
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
  pause
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

Write-PackageText -RelativePath "update_package.ps1" -Text @'
$ErrorActionPreference = "Stop"

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$localVersionPath = Join-Path $packageDir "VERSION.txt"
$releaseBaseUrl = if ($env:AMBULANCE_RETURN_RELEASE_BASE_URL) { $env:AMBULANCE_RETURN_RELEASE_BASE_URL.TrimEnd("/") } else { "" }
$downloadCacheKey = Get-Date -Format "yyyyMMddHHmmss"
$latestReleaseApiUrl = "https://api.github.com/repos/seaflun/ambulance-return-bot/releases/latest"
$backupRoot = Join-Path $env:LOCALAPPDATA "AmbulanceReturnBot"
$backupDir = Join-Path $backupRoot "update_backups"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tempDir = Join-Path $env:TEMP "AmbulanceReturnBotUpdate-$stamp"
$zipPath = Join-Path $tempDir "package.zip"
$extractDir = Join-Path $tempDir "extract"

function Get-TextFromUrl {
    param([string]$Url)
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -MaximumRedirection 5
    if ($response.Content -is [byte[]]) {
        $text = [System.Text.Encoding]::UTF8.GetString($response.Content)
    } else {
        $text = [string]$response.Content
    }
    return $text.Trim().TrimStart([char]0xFEFF)
}

function Test-VersionText {
    param(
        [string]$Version,
        [switch]$AllowZero
    )

    if ($AllowZero -and $Version -eq "0") {
        return $true
    }
    return $Version -match "^\d{4}\.\d{2}\.\d{2}\.\d{4}$"
}

function Get-Sha256FromText {
    param([string]$Text)
    $firstToken = ($Text.Trim().TrimStart([char]0xFEFF) -split "\s+")[0]
    if ($firstToken -notmatch "^[0-9a-fA-F]{64}$") {
        throw "Remote SHA256 file has an invalid hash: $firstToken"
    }
    return $firstToken.ToLowerInvariant()
}

function Add-DownloadCacheBust {
    param([string]$Url)
    $separator = if ($Url.Contains("?")) { "&" } else { "?" }
    return "$Url${separator}cachebust=$downloadCacheKey"
}

function Get-ReleaseAssetUrl {
    param(
        [object]$Release,
        [string]$Name
    )

    $asset = $Release.assets | Where-Object { $_.name -eq $Name } | Select-Object -First 1
    if (-not $asset -or -not $asset.browser_download_url) {
        throw "Latest GitHub release is missing asset: $Name"
    }
    return [string]$asset.browser_download_url
}

function Resolve-RemoteDownloadUrls {
    if (-not [string]::IsNullOrWhiteSpace($releaseBaseUrl)) {
        return [pscustomobject]@{
            Version = Add-DownloadCacheBust "$releaseBaseUrl/ambulance-return-version.txt"
            Zip = Add-DownloadCacheBust "$releaseBaseUrl/ambulance-return-public-package.zip"
            Sha256 = Add-DownloadCacheBust "$releaseBaseUrl/ambulance-return-public-package.zip.sha256.txt"
        }
    }

    $release = Invoke-RestMethod -Uri $latestReleaseApiUrl -UseBasicParsing -Headers @{
        "Accept" = "application/vnd.github+json"
        "User-Agent" = "AmbulanceReturnBotUpdater"
    }
    Write-Host "Latest release: $($release.tag_name)"
    return [pscustomobject]@{
        Version = Add-DownloadCacheBust (Get-ReleaseAssetUrl -Release $release -Name "ambulance-return-version.txt")
        Zip = Add-DownloadCacheBust (Get-ReleaseAssetUrl -Release $release -Name "ambulance-return-public-package.zip")
        Sha256 = Add-DownloadCacheBust (Get-ReleaseAssetUrl -Release $release -Name "ambulance-return-public-package.zip.sha256.txt")
    }
}

function Copy-UpdateTree {
    param(
        [string]$SourceDir,
        [string]$DestDir
    )

    $skipDirs = @("logs", "runtime_outputs", "tmp", "temp", "cache", ".cache", "local_data", "snapshots", "__pycache__", "artifacts")
    $alwaysSkipFiles = @(".env", "update_urls.json", "UPDATE_PACKAGE.bat")
    $slash = [string][char]92
    $sourceRoot = $SourceDir.TrimEnd([char]92) + $slash

    Get-ChildItem -LiteralPath $SourceDir -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($sourceRoot.Length)
        $parts = $relative -split "[\\/]"
        if ($parts | Where-Object { $skipDirs -contains $_ }) {
            return
        }
        if ($alwaysSkipFiles -contains $_.Name) {
            Write-Host "Preserved local file: $($_.Name)"
            return
        }

        $target = Join-Path $DestDir $relative
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        Write-Host "Updated: $relative"
    }
}

function Get-WorkerPackageProcesses {
    $packagePath = [System.IO.Path]::GetFullPath($packageDir)
    Get-CimInstance Win32_Process |
        Where-Object {
            $commandLine = [string]$_.CommandLine
            $commandLine -and
            ($commandLine -match "worker_gui\.py|app\.py") -and
            (
                $commandLine.IndexOf($packagePath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
                $commandLine -match "ambulance_return_bot|WinPython_"
            )
        }
}

function Stop-WorkerPackageProcesses {
    $processes = @(Get-WorkerPackageProcesses)
    foreach ($process in $processes) {
        Write-Host "Stopping running worker process: $($process.ProcessId) $($process.Name)"
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($processes.Count -gt 0) {
        Start-Sleep -Seconds 2
    }
}

function Start-WorkerGui {
    $launcher = Join-Path $packageDir "RUN_WORKER_GUI_WINPYTHON.vbs"
    if (Test-Path -LiteralPath $launcher -PathType Leaf) {
        Write-Host "Restarting worker GUI..."
        Start-Process -FilePath "wscript.exe" -ArgumentList "`"$launcher`"" -WorkingDirectory $packageDir | Out-Null
    } else {
        Write-Warning "Cannot restart worker GUI because launcher is missing: $launcher"
    }
}

function Install-StartupLaunchers {
    $installer = Join-Path $packageDir "install_startup_shortcut.ps1"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        Write-Warning "Cannot refresh startup launcher because installer is missing: $installer"
        return
    }
    Write-Host "Refreshing startup launcher..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -SkipScheduledTask
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Startup launcher refresh exited with code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $localVersionPath)) {
    "0" | Set-Content -LiteralPath $localVersionPath -Encoding UTF8
}

$localVersion = (Get-Content -LiteralPath $localVersionPath -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
$remoteUrls = Resolve-RemoteDownloadUrls
$remoteVersionUrl = $remoteUrls.Version
$remoteZipUrl = $remoteUrls.Zip
$remoteSha256Url = $remoteUrls.Sha256
$remoteVersion = Get-TextFromUrl -Url $remoteVersionUrl
$remoteSha256 = Get-Sha256FromText -Text (Get-TextFromUrl -Url $remoteSha256Url)

if (-not (Test-VersionText -Version $localVersion -AllowZero)) {
    throw "Local VERSION.txt has an invalid version: $localVersion"
}
if (-not (Test-VersionText -Version $remoteVersion)) {
    throw "Remote VERSION.txt has an invalid version: $remoteVersion"
}

Write-Host "Local version : $localVersion"
Write-Host "Remote version: $remoteVersion"

if ([string]::CompareOrdinal($remoteVersion, $localVersion) -le 0) {
    Write-Host "Already up to date."
    exit 0
}

try {
    New-Item -ItemType Directory -Path $tempDir | Out-Null
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

    Write-Host "Downloading update package..."
    Invoke-WebRequest -Uri $remoteZipUrl -OutFile $zipPath -UseBasicParsing -MaximumRedirection 5

    if (-not (Test-Path -LiteralPath $zipPath) -or (Get-Item -LiteralPath $zipPath).Length -lt 1024) {
        throw "Downloaded package is missing or too small."
    }
    $downloadedSha256 = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($downloadedSha256 -ne $remoteSha256) {
        throw "Downloaded package SHA256 mismatch. Expected $remoteSha256 but got $downloadedSha256."
    }

    Stop-WorkerPackageProcesses

    $backupZip = Join-Path $backupDir "AmbulanceReturnBot-package-backup-$stamp.zip"
    Write-Host "Creating backup: $backupZip"
    Compress-Archive -LiteralPath (Join-Path $packageDir "*") -DestinationPath $backupZip -Force

    New-Item -ItemType Directory -Path $extractDir | Out-Null
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force

    $sourceDir = Get-ChildItem -LiteralPath $extractDir -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "worker_gui.py") } |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $sourceDir -and (Test-Path -LiteralPath (Join-Path $extractDir "worker_gui.py"))) {
        $sourceDir = $extractDir
    }
    if (-not $sourceDir -or -not (Test-Path -LiteralPath $sourceDir -PathType Container)) {
        throw "Update zip does not contain a valid worker package folder."
    }

    $packageVersionPath = Join-Path $sourceDir "VERSION.txt"
    if (-not (Test-Path -LiteralPath $packageVersionPath -PathType Leaf)) {
        throw "Update zip does not contain VERSION.txt."
    }
    $packageVersion = (Get-Content -LiteralPath $packageVersionPath -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
    if (-not (Test-VersionText -Version $packageVersion)) {
        throw "Update zip VERSION.txt has an invalid version: $packageVersion"
    }
    if ($packageVersion -ne $remoteVersion) {
        throw "Update version mismatch. Remote VERSION.txt is $remoteVersion but package VERSION.txt is $packageVersion."
    }

    Copy-UpdateTree -SourceDir $sourceDir -DestDir $packageDir
    $packageVersion | Set-Content -LiteralPath $localVersionPath -Encoding UTF8

    Write-Host "Update completed."
    Install-StartupLaunchers
    Start-WorkerGui
} finally {
    try {
        if (Test-Path -LiteralPath $tempDir) {
            Remove-Item -LiteralPath $tempDir -Recurse -Force
        }
    } catch {
        Write-Warning "Could not remove temporary update folder: $tempDir"
    }
}
'@

try {
    Remove-SafeDirectory -Path $stageRoot -ExpectedRoot (Join-Path $project "tmp")
    New-Item -ItemType Directory -Path $stagePackageDir -Force | Out-Null
    Copy-ZipStage -SourceDir $packageDir -DestDir $stagePackageDir

    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -LiteralPath $stagePackageDir -DestinationPath $zipPath -Force
    $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $packageName.zip" | Set-Content -LiteralPath $shaPath -Encoding UTF8
    $releaseVersionPath = Join-Path $updateDir $releaseVersionAsset
    $releaseZipPath = Join-Path $updateDir $releaseZipAsset
    $releaseShaPath = Join-Path $updateDir $releaseShaAsset
    $releaseUpdaterPath = Join-Path $updateDir $releaseUpdaterAsset
    $Version | Set-Content -LiteralPath $releaseVersionPath -Encoding UTF8
    Copy-Item -LiteralPath $zipPath -Destination $releaseZipPath -Force
    "$hash  $releaseZipAsset" | Set-Content -LiteralPath $releaseShaPath -Encoding UTF8
    Copy-Item -LiteralPath (Join-Path $packageDir "update_package.ps1") -Destination $releaseUpdaterPath -Force
} finally {
    Remove-SafeDirectory -Path $stageRoot -ExpectedRoot (Join-Path $project "tmp")
}

[PSCustomObject]@{
    Version = $Version
    PackageDir = $packageDir
    UpdateDir = $updateDir
    Zip = $zipPath
    Sha256 = (Get-Content -LiteralPath $shaPath -Raw -Encoding UTF8).Trim()
    ReleaseAssets = @($releaseVersionAsset, $releaseZipAsset, $releaseShaAsset, $releaseUpdaterAsset) -join ", "
} | Format-List
