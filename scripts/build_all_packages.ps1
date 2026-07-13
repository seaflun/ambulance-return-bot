param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$ProjectRoot
)

$ErrorActionPreference = "Stop"

if ($Version -notmatch "^\d{4}\.\d{2}\.\d{2}\.\d{4}$") {
    throw "Version must use yyyy.MM.dd.HHmm format. Got: $Version"
}
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}

$project = (Resolve-Path -LiteralPath $ProjectRoot).Path
$publicBuild = Join-Path $PSScriptRoot "build_public_duty_package.ps1"
$nasBuild = Join-Path $PSScriptRoot "build_nas_package.ps1"
$packageName = "WinPython_" + [string][char]0x516c + [string][char]0x52d9 + [string][char]0x96fb + [string][char]0x8166 + [string][char]0x4f7f + [string][char]0x7528 + [string][char]0x5305
$nasPackageName = "NAS" + [string][char]0x5305
$packageDir = Join-Path $project $packageName
$updateDir = Join-Path $project "UPDATE"

function Read-VersionText {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing version file: $Path"
    }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
}

function Read-ZipVersion {
    param(
        [string]$Path,
        [string]$ExpectedPackageName
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing package zip: $Path"
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $expectedEntry = "$ExpectedPackageName/VERSION.txt"
        $entries = @($archive.Entries | Where-Object { ($_.FullName -replace "\\", "/") -eq $expectedEntry })
        if ($entries.Count -ne 1) {
            throw "Package zip must contain exactly one $expectedEntry entry. Found: $($entries.Count)"
        }
        $stream = $entries[0].Open()
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8, $true)
        try {
            return $reader.ReadToEnd().Trim().TrimStart([char]0xFEFF)
        } finally {
            $reader.Dispose()
            $stream.Dispose()
        }
    } finally {
        $archive.Dispose()
    }
}

function Read-Sha256Text {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing SHA256 file: $Path"
    }
    $text = (Get-Content -LiteralPath $Path -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
    $hash = ($text -split "\s+")[0].ToLowerInvariant()
    if ($hash -notmatch "^[0-9a-f]{64}$") {
        throw "Invalid SHA256 file: $Path"
    }
    return $hash
}

function Assert-FileSha256 {
    param(
        [string]$Label,
        [string]$FilePath,
        [string]$Sha256Path
    )

    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        throw "Missing file for SHA256 verification: $FilePath"
    }
    $expected = Read-Sha256Text -Path $Sha256Path
    $actual = (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "$Label SHA256 mismatch. Expected $expected but got $actual."
    }
    return $actual
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

function Resolve-FullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
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

function Assert-ChildPath {
    param(
        [string]$Path,
        [string]$Root
    )

    $fullPath = (Resolve-FullPath -Path $Path).TrimEnd([char]92)
    $fullRoot = (Resolve-FullPath -Path $Root).TrimEnd([char]92)
    $prefix = $fullRoot + [string][char]92
    if ($fullPath.Equals($fullRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Expected a child path. Path=$fullPath Root=$fullRoot"
    }
}

function Remove-StagedPath {
    param(
        [string]$Path,
        [string]$ExpectedRoot
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    Assert-ChildPath -Path $Path -Root $ExpectedRoot
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Assert-BuildOutputs {
    param(
        [string]$BaseUpdateDir,
        [string]$SourceVersionPath,
        [string]$NasDir
    )

    $sourceVersion = Read-VersionText -Path $SourceVersionPath
    $updateVersion = Read-VersionText -Path (Join-Path $BaseUpdateDir "VERSION.txt")
    $nasVersion = Read-VersionText -Path (Join-Path $NasDir "VERSION.txt")
    $releaseVersion = Read-VersionText -Path (Join-Path $BaseUpdateDir "ambulance-return-version.txt")
    $publicZip = Join-Path $BaseUpdateDir "$packageName.zip"
    $releaseZip = Join-Path $BaseUpdateDir "ambulance-return-public-package.zip"
    $publicSha256Path = "$publicZip.sha256.txt"
    $releaseSha256Path = Join-Path $BaseUpdateDir "ambulance-return-public-package.zip.sha256.txt"
    $publicZipVersion = Read-ZipVersion -Path $publicZip -ExpectedPackageName $packageName
    $releaseZipVersion = Read-ZipVersion -Path $releaseZip -ExpectedPackageName $packageName
    $publicZipSha256 = Assert-FileSha256 -Label "Public package zip" -FilePath $publicZip -Sha256Path $publicSha256Path
    $releaseZipSha256 = Assert-FileSha256 -Label "Release package zip" -FilePath $releaseZip -Sha256Path $releaseSha256Path
    if ($publicZipSha256 -ne $releaseZipSha256) {
        throw "Public/release zip byte hash mismatch. Public=$publicZipSha256 Release=$releaseZipSha256"
    }

    Assert-VersionEquals -Label "Source VERSION.txt" -Actual $sourceVersion -Expected $Version
    Assert-VersionEquals -Label "UPDATE VERSION.txt" -Actual $updateVersion -Expected $Version
    Assert-VersionEquals -Label "NAS VERSION.txt" -Actual $nasVersion -Expected $Version
    Assert-VersionEquals -Label "Release version asset" -Actual $releaseVersion -Expected $Version
    Assert-VersionEquals -Label "Public package zip VERSION.txt" -Actual $publicZipVersion -Expected $Version
    Assert-VersionEquals -Label "Release package zip VERSION.txt" -Actual $releaseZipVersion -Expected $Version

    return [PSCustomObject]@{
        SourceVersion = $sourceVersion
        NasVersion = $nasVersion
        PublicZipVersion = $publicZipVersion
        ReleaseZipVersion = $releaseZipVersion
        PublicZipSha256 = $publicZipSha256
        ReleaseZipSha256 = $releaseZipSha256
        PublicZip = $publicZip
        ReleaseZip = $releaseZip
    }
}

function Restore-PublishedBuild {
    param([object[]]$Entries)

    $errors = @()
    $reverseEntries = @($Entries)
    [array]::Reverse($reverseEntries)
    foreach ($entry in $reverseEntries) {
        if ($entry.Published) {
            try {
                if (Test-Path -LiteralPath $entry.Target) {
                    if ($entry.Kind -eq "Directory") {
                        Remove-Item -LiteralPath $entry.Target -Recurse -Force
                    } else {
                        Remove-Item -LiteralPath $entry.Target -Force
                    }
                }
                $entry.Published = $false
            } catch {
                $errors += "remove published target $($entry.Target): $($_.Exception.Message)"
            }
        }
        if ($entry.HadExisting -and -not $entry.Published) {
            try {
                if (-not (Test-Path -LiteralPath $entry.Backup)) {
                    throw "backup is missing"
                }
                Move-Item -LiteralPath $entry.Backup -Destination $entry.Target -Force
                $entry.HadExisting = $false
            } catch {
                $errors += "restore backup $($entry.Backup): $($_.Exception.Message)"
            }
        }
        try {
            if (Test-Path -LiteralPath $entry.Temp) {
                if ($entry.Kind -eq "Directory") {
                    Remove-Item -LiteralPath $entry.Temp -Recurse -Force
                } else {
                    Remove-Item -LiteralPath $entry.Temp -Force
                }
            }
        } catch {
            $errors += "remove publish temp $($entry.Temp): $($_.Exception.Message)"
        }
    }
    if ($errors.Count -gt 0) {
        throw "Package rollback incomplete: $($errors -join '; ')"
    }
}

function Publish-StagedBuild {
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
        $isDirectory = $mapping.Kind -eq "Directory"
        $sourceType = if ($isDirectory) { "Container" } else { "Leaf" }
        if (-not (Test-Path -LiteralPath $mapping.Source -PathType $sourceType)) {
            throw "Missing staged build input: $($mapping.Source)"
        }
        Assert-ChildPath -Path $mapping.Target -Root $project
        $targetDir = Split-Path -Parent $mapping.Target
        if (-not (Test-Path -LiteralPath $targetDir -PathType Container)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }
        $temp = Join-Path $targetDir (".{0}.publish-{1}.tmp" -f (Split-Path -Leaf $mapping.Target), [guid]::NewGuid().ToString("N"))
        $backup = Join-Path $RollbackRoot ("{0:D3}.bak" -f $index)
        if ($isDirectory) {
            Copy-Item -LiteralPath $mapping.Source -Destination $temp -Recurse -Force
        } else {
            Copy-Item -LiteralPath $mapping.Source -Destination $temp -Force
        }
        $entries += [PSCustomObject]@{
            Kind = $mapping.Kind
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
        if (Test-Path -LiteralPath $entry.Target) {
            Move-Item -LiteralPath $entry.Target -Destination $entry.Backup -Force
            $entry.HadExisting = $true
        }
        Move-Item -LiteralPath $entry.Temp -Destination $entry.Target -Force
        $entry.Published = $true
    }
}

New-Item -ItemType Directory -Path $updateDir -Force | Out-Null
$buildLockStream = Enter-PackageBuildLock -LockPath (Join-Path $updateDir ".package-build.lock")
try {
$stageRoot = Join-Path $updateDir "package-build-stage-$([guid]::NewGuid().ToString('N'))"
$stagePublicDir = Join-Path $stageRoot "public"
$stageExtractDir = Join-Path $stageRoot "extracted"
$stageNasDir = Join-Path $stageRoot "nas"
$stageRollbackDir = Join-Path $stageRoot "publish-rollback"
$stagePublicZip = Join-Path $stagePublicDir "$packageName.zip"
$stagePublicPackageDir = Join-Path $stageExtractDir $packageName
$finalNasDir = Join-Path $updateDir $nasPackageName
$published = $false
$publishState = [PSCustomObject]@{
    Attempted = $false
    RollbackComplete = $false
    Entries = @()
}

try {
    New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
    & $publicBuild -ProjectRoot $project -Version $Version -OutputDir $stagePublicDir -SkipSourceVersionUpdate -BuildLockAlreadyHeld
    Expand-Archive -LiteralPath $stagePublicZip -DestinationPath $stageExtractDir -Force
    if (-not (Test-Path -LiteralPath $stagePublicPackageDir -PathType Container)) {
        throw "Staged public package did not extract to the expected directory: $stagePublicPackageDir"
    }
    & $nasBuild -ProjectRoot $project -Version $Version -SourceDir $stagePublicPackageDir -OutputDir $stageNasDir -BuildLockAlreadyHeld

    $null = Assert-BuildOutputs `
        -BaseUpdateDir $stagePublicDir `
        -SourceVersionPath (Join-Path $stagePublicPackageDir "VERSION.txt") `
        -NasDir $stageNasDir

    $mappings = @(
        [PSCustomObject]@{ Kind = "File"; Source = (Join-Path $stagePublicDir "$packageName.zip"); Target = (Join-Path $updateDir "$packageName.zip") },
        [PSCustomObject]@{ Kind = "File"; Source = (Join-Path $stagePublicDir "$packageName.zip.sha256.txt"); Target = (Join-Path $updateDir "$packageName.zip.sha256.txt") },
        [PSCustomObject]@{ Kind = "File"; Source = (Join-Path $stagePublicDir "ambulance-return-version.txt"); Target = (Join-Path $updateDir "ambulance-return-version.txt") },
        [PSCustomObject]@{ Kind = "File"; Source = (Join-Path $stagePublicDir "ambulance-return-public-package.zip"); Target = (Join-Path $updateDir "ambulance-return-public-package.zip") },
        [PSCustomObject]@{ Kind = "File"; Source = (Join-Path $stagePublicDir "ambulance-return-public-package.zip.sha256.txt"); Target = (Join-Path $updateDir "ambulance-return-public-package.zip.sha256.txt") },
        [PSCustomObject]@{ Kind = "File"; Source = (Join-Path $stagePublicDir "update_package.ps1"); Target = (Join-Path $updateDir "update_package.ps1") },
        [PSCustomObject]@{ Kind = "File"; Source = (Join-Path $stagePublicDir "VERSION.txt"); Target = (Join-Path $updateDir "VERSION.txt") },
        [PSCustomObject]@{ Kind = "File"; Source = (Join-Path $stagePublicDir "VERSION.txt"); Target = (Join-Path $packageDir "VERSION.txt") },
        [PSCustomObject]@{ Kind = "Directory"; Source = $stageNasDir; Target = $finalNasDir }
    )
    Publish-StagedBuild -Mappings $mappings -RollbackRoot $stageRollbackDir -State $publishState

    $verification = Assert-BuildOutputs `
        -BaseUpdateDir $updateDir `
        -SourceVersionPath (Join-Path $packageDir "VERSION.txt") `
        -NasDir $finalNasDir
    $published = $true
} catch {
    $publishError = $_
    if ($publishState.Attempted -and -not $published) {
        try {
            Restore-PublishedBuild -Entries $publishState.Entries
            $publishState.RollbackComplete = $true
        } catch {
            throw "Package publish failed and rollback is incomplete. Recovery files: $stageRollbackDir. Publish error: $($publishError.Exception.Message). Rollback error: $($_.Exception.Message)"
        }
    }
    throw
} finally {
    if ($publishState.Attempted -and -not $published -and -not $publishState.RollbackComplete) {
        Write-Warning "Package rollback was incomplete; recovery files were preserved at $stageRollbackDir"
    } else {
        Remove-StagedPath -Path $stageRoot -ExpectedRoot $updateDir
    }
}

[PSCustomObject]@{
    Version = $Version
    SourceVersion = $verification.SourceVersion
    NasVersion = $verification.NasVersion
    PublicZipVersion = $verification.PublicZipVersion
    ReleaseZipVersion = $verification.ReleaseZipVersion
    PublicZipSha256 = $verification.PublicZipSha256
    ReleaseZipSha256 = $verification.ReleaseZipSha256
    PublicZip = $verification.PublicZip
    ReleaseZip = $verification.ReleaseZip
} | Format-List
} finally {
    $buildLockStream.Dispose()
}
