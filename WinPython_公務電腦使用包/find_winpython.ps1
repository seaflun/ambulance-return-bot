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
    $isExplicitWinPythonRoot = $root -eq $env:WINPYTHON_DIR
    if ($direct -and ($isExplicitWinPythonRoot -or $direct.FullName -match "WinPython|python-\d")) {
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
