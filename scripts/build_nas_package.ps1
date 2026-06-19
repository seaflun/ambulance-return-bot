param(
    [string]$ProjectRoot,
    [string]$OutputDir
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}

$project = (Resolve-Path -LiteralPath $ProjectRoot).Path
$nasPackageName = "NAS" + [string][char]0x5305

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

function Find-PublicDutyPackage {
    $matches = @(
        Get-ChildItem -LiteralPath $project -Directory -Force |
            Where-Object {
                $_.Name -like "WinPython_*" -and
                (Test-Path -LiteralPath (Join-Path $_.FullName "app.py") -PathType Leaf) -and
                (Test-Path -LiteralPath (Join-Path $_.FullName "worker_gui.py") -PathType Leaf) -and
                (Test-Path -LiteralPath (Join-Path $_.FullName "ambulance_bot") -PathType Container)
            } |
            Sort-Object FullName
    )
    if ($matches.Count -eq 0) {
        throw "Could not find the WinPython public-duty source package."
    }
    if ($matches.Count -gt 1) {
        throw "Found multiple WinPython source packages: $($matches.FullName -join ', ')"
    }
    return $matches[0].FullName
}

function Copy-FileToOutput {
    param(
        [string]$Source,
        [string]$RelativePath
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Missing NAS package source file: $Source"
    }
    $target = Join-Path $output $RelativePath
    $targetDir = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $Source -Destination $target -Force
}

function Copy-DirectoryToOutput {
    param(
        [string]$SourceDir,
        [string]$RelativePath
    )

    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
        throw "Missing NAS package source directory: $SourceDir"
    }

    $skipDirs = @("__pycache__", ".pytest_cache", "artifacts", "logs", "tmp", "temp", "cache", ".cache", "runtime_outputs", "snapshots")
    $skipFiles = @(".env", "update_urls.json")
    $sourceRoot = (Resolve-FullPath -Path $SourceDir).TrimEnd([char]92) + [string][char]92
    Get-ChildItem -LiteralPath $SourceDir -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($sourceRoot.Length)
        $parts = $relative -split "[\\/]"
        if ($parts | Where-Object { $skipDirs -contains $_ }) {
            return
        }
        if ($skipFiles -contains $_.Name) {
            return
        }
        if ($_.Name -match "\.pyc$|\.pyo$|\.pyd$|\.log$") {
            return
        }
        Copy-FileToOutput -Source $_.FullName -RelativePath (Join-Path $RelativePath $relative)
    }
}

function Write-OutputText {
    param(
        [string]$RelativePath,
        [string]$Text
    )

    $target = Join-Path $output $RelativePath
    $targetDir = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }
    $Text.TrimStart() | Set-Content -LiteralPath $target -Encoding UTF8
}

$publicDutyDir = Find-PublicDutyPackage

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path (Join-Path $project "UPDATE") $nasPackageName
}

$output = Resolve-FullPath -Path $OutputDir
Assert-UnderPath -Path $output -Root $project
if ($output.TrimEnd([char]92).Equals((Resolve-FullPath -Path $publicDutyDir).TrimEnd([char]92), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDir must not be the public-duty source package."
}

$cleanRoot = Join-Path $project "UPDATE"
if (-not (Test-Path -LiteralPath $cleanRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $cleanRoot -Force | Out-Null
}
Remove-SafeDirectory -Path $output -ExpectedRoot $cleanRoot
New-Item -ItemType Directory -Path $output -Force | Out-Null

foreach ($file in @(
    "app.py",
    "worker.py",
    "consumables_login.py",
    "disinfect.py",
    "requirements.txt",
    ".env.example",
    "VERSION.txt"
)) {
    $source = Join-Path $publicDutyDir $file
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        Copy-FileToOutput -Source $source -RelativePath $file
    }
}

foreach ($dir in @("ambulance_bot", "templates")) {
    Copy-DirectoryToOutput -SourceDir (Join-Path $publicDutyDir $dir) -RelativePath $dir
}

$compose = Join-Path $project "compose.nas.yml"
if (Test-Path -LiteralPath $compose -PathType Leaf) {
    Copy-FileToOutput -Source $compose -RelativePath "compose.nas.yml"
}

$docs = Join-Path $project "docs"
if (Test-Path -LiteralPath $docs -PathType Container) {
    Copy-DirectoryToOutput -SourceDir $docs -RelativePath "docs"
}

Write-OutputText -RelativePath "README.md" -Text @'
# NAS deployment package

This folder is generated. Do not edit it as source.

Source of truth:

- Shared runtime: WinPython public-duty package
- Build command: powershell -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1
- Default output: UPDATE\NAS package

Deploy the generated contents to /docker/ambulance_return_bot/, keep the NAS .env already on the NAS, then restart the ambulance_return_bot stack in DSM Container Manager.
'@

Write-OutputText -RelativePath "NAS_DEPLOY.txt" -Text @'
NAS package generated from the public-duty runtime source.

1. Run from the project root:
   powershell -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1
2. Upload the generated UPDATE NAS package contents to:
   /docker/ambulance_return_bot/
3. Keep the NAS .env file on the NAS. This generated package does not include .env.
4. Restart the ambulance_return_bot stack in DSM Container Manager.
'@

[PSCustomObject]@{
    Source = $publicDutyDir
    Output = $output
    Files = (Get-ChildItem -LiteralPath $output -Recurse -File | Measure-Object).Count
} | Format-List
