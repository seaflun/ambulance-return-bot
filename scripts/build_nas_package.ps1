param(
    [string]$ProjectRoot,
    [string]$OutputDir,
    [string]$Version,
    [string]$SourceDir,
    [switch]$BuildLockAlreadyHeld
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

function Read-VersionText {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing version file: $Path"
    }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
}

function Assert-VersionEquals {
    param(
        [string]$Label,
        [string]$Actual,
        [string]$Expected
    )

    if ($Actual -ne $Expected) {
        throw "$Label version mismatch. Expected $Expected but got $Actual."
    }
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

function Restore-NasOutput {
    param([object]$State)

    if ($null -eq $State) {
        return
    }
    $errors = @()
    if ($State.Published) {
        try {
            if (Test-Path -LiteralPath $State.FinalOutput) {
                Remove-SafeDirectory -Path $State.FinalOutput -ExpectedRoot $State.CleanRoot
            }
            $State.Published = $false
        } catch {
            $errors += "remove published NAS output $($State.FinalOutput): $($_.Exception.Message)"
        }
    }
    if ($State.HadExisting -and -not $State.Published) {
        try {
            if (-not (Test-Path -LiteralPath $State.RollbackOutput -PathType Container)) {
                throw "backup is missing"
            }
            Move-Item -LiteralPath $State.RollbackOutput -Destination $State.FinalOutput -Force
            $State.HadExisting = $false
        } catch {
            $errors += "restore NAS backup $($State.RollbackOutput): $($_.Exception.Message)"
        }
    }
    if ($errors.Count -gt 0) {
        throw "NAS rollback incomplete: $($errors -join '; ')"
    }
}

function Publish-NasStage {
    param(
        [string]$StageOutput,
        [string]$FinalOutput,
        [string]$RollbackOutput,
        [string]$CleanRoot,
        [object]$State
    )

    if (-not (Test-Path -LiteralPath $StageOutput -PathType Container)) {
        throw "Missing staged NAS output: $StageOutput"
    }
    if ((Test-Path -LiteralPath $FinalOutput) -and -not (Test-Path -LiteralPath $FinalOutput -PathType Container)) {
        throw "NAS output target is not a directory: $FinalOutput"
    }
    $State.Attempted = $true
    if (Test-Path -LiteralPath $FinalOutput -PathType Container) {
        Move-Item -LiteralPath $FinalOutput -Destination $RollbackOutput -Force
        $State.HadExisting = $true
    }
    Move-Item -LiteralPath $StageOutput -Destination $FinalOutput -Force
    $State.Published = $true
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

$buildLockStream = $null
if (-not $BuildLockAlreadyHeld) {
    $buildLockStream = Enter-PackageBuildLock -LockPath (Join-Path (Join-Path $project "UPDATE") ".package-build.lock")
}

try {
if ([string]::IsNullOrWhiteSpace($SourceDir)) {
    $publicDutyDir = Find-PublicDutyPackage
} else {
    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
        throw "Missing explicit NAS package source directory: $SourceDir"
    }
    $publicDutyDir = (Resolve-Path -LiteralPath $SourceDir).Path
}
$sourceVersionPath = Join-Path $publicDutyDir "VERSION.txt"
$sourceVersion = Read-VersionText -Path $sourceVersionPath
if ($sourceVersion -notmatch "^\d{4}\.\d{2}\.\d{2}\.\d{4}$") {
    throw "Source VERSION.txt has an invalid version: $sourceVersion"
}
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = $sourceVersion
} elseif ($Version -notmatch "^\d{4}\.\d{2}\.\d{2}\.\d{4}$") {
    throw "Version must use yyyy.MM.dd.HHmm format. Got: $Version"
}
Assert-VersionEquals -Label "Source VERSION.txt" -Actual $sourceVersion -Expected $Version

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path (Join-Path $project "UPDATE") $nasPackageName
}

$cleanRoot = Join-Path $project "UPDATE"
if (-not (Test-Path -LiteralPath $cleanRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $cleanRoot -Force | Out-Null
}
$cleanRootFull = (Resolve-FullPath -Path $cleanRoot).TrimEnd([char]92)
$finalOutput = Resolve-FullPath -Path $OutputDir
Assert-UnderPath -Path $finalOutput -Root $cleanRoot
if ($finalOutput.TrimEnd([char]92).Equals($cleanRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDir must be a child directory of UPDATE, not UPDATE itself."
}
if ($finalOutput.TrimEnd([char]92).Equals((Resolve-FullPath -Path $publicDutyDir).TrimEnd([char]92), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDir must not be the public-duty source package."
}

$outputParent = Split-Path -Parent $finalOutput
$outputLeaf = Split-Path -Leaf $finalOutput
$nonce = [guid]::NewGuid().ToString("N")
$nasStageDir = Join-Path $outputParent ".$outputLeaf.build-$nonce.tmp"
$nasRollbackDir = Join-Path $outputParent ".$outputLeaf.rollback-$nonce.tmp"
$output = $nasStageDir
$publishState = [PSCustomObject]@{
    FinalOutput = $finalOutput
    RollbackOutput = $nasRollbackDir
    CleanRoot = $cleanRoot
    Attempted = $false
    RollbackComplete = $false
    HadExisting = $false
    Published = $false
}
$publishSucceeded = $false

try {
    Remove-SafeDirectory -Path $nasStageDir -ExpectedRoot $cleanRoot
    Remove-SafeDirectory -Path $nasRollbackDir -ExpectedRoot $cleanRoot
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
    Copy-FileToOutput -Source $source -RelativePath $file
}

foreach ($dir in @("ambulance_bot", "templates", "static")) {
    Copy-DirectoryToOutput -SourceDir (Join-Path $publicDutyDir $dir) -RelativePath $dir
}

$compose = Join-Path $project "compose.nas.yml"
if (Test-Path -LiteralPath $compose -PathType Leaf) {
    Copy-FileToOutput -Source $compose -RelativePath "compose.nas.yml"
    Copy-FileToOutput -Source $compose -RelativePath "compose.yaml"
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

    $nasVersion = Read-VersionText -Path (Join-Path $output "VERSION.txt")
    Assert-VersionEquals -Label "NAS VERSION.txt" -Actual $nasVersion -Expected $Version

    Publish-NasStage `
        -StageOutput $nasStageDir `
        -FinalOutput $finalOutput `
        -RollbackOutput $nasRollbackDir `
        -CleanRoot $cleanRoot `
        -State $publishState
    $nasVersion = Read-VersionText -Path (Join-Path $finalOutput "VERSION.txt")
    Assert-VersionEquals -Label "Published NAS VERSION.txt" -Actual $nasVersion -Expected $Version
    $publishSucceeded = $true
} catch {
    $publishError = $_
    if ($publishState.Attempted) {
        try {
            Restore-NasOutput -State $publishState
            $publishState.RollbackComplete = $true
        } catch {
            throw "NAS package publish failed and rollback is incomplete. Recovery files: $nasRollbackDir. Publish error: $($publishError.Exception.Message). Rollback error: $($_.Exception.Message)"
        }
    }
    throw
} finally {
    Remove-SafeDirectory -Path $nasStageDir -ExpectedRoot $cleanRoot
    if ($publishSucceeded) {
        Remove-SafeDirectory -Path $nasRollbackDir -ExpectedRoot $cleanRoot
    } elseif ($publishState.Attempted -and -not $publishState.RollbackComplete) {
        Write-Warning "NAS package rollback was incomplete; recovery files were preserved at $nasRollbackDir"
    }
}

[PSCustomObject]@{
    Version = $Version
    Source = $publicDutyDir
    Output = $finalOutput
    Files = (Get-ChildItem -LiteralPath $finalOutput -Recurse -File | Measure-Object).Count
} | Format-List
} finally {
    if ($null -ne $buildLockStream) {
        $buildLockStream.Dispose()
    }
}
