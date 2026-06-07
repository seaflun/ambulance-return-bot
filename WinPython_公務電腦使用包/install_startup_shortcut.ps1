$ErrorActionPreference = "Stop"

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $packageDir "RUN_WORKER_GUI_WINPYTHON.vbs"
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "救護回程 Worker.lnk"

if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Cannot find startup target: $target"
}
if (-not (Test-Path -LiteralPath $startupDir -PathType Container)) {
    throw "Cannot find Startup folder: $startupDir"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $packageDir
$shortcut.Description = "救護回程 Worker"
$shortcut.Save()
Write-Host "Installed startup shortcut: $shortcutPath"
