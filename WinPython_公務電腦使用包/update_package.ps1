$ErrorActionPreference = "Stop"

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$localVersionPath = Join-Path $packageDir "VERSION.txt"
$releaseBaseUrl = if ($env:AMBULANCE_RETURN_RELEASE_BASE_URL) { $env:AMBULANCE_RETURN_RELEASE_BASE_URL.TrimEnd("/") } else { "" }
$downloadCacheKey = Get-Date -Format "yyyyMMddHHmmss"
$latestReleaseApiUrl = "https://api.github.com/repos/seaflun/ambulance-return-bot/releases/latest"
$manifestName = "UPDATE_MANIFEST.json"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$runId = [guid]::NewGuid().ToString("N")
$tempDir = Join-Path $env:TEMP "AmbulanceReturnBotUpdate-$stamp-$runId"
$zipPath = Join-Path $tempDir "package.zip"
$extractDir = Join-Path $tempDir "extract"
$rollbackDir = Join-Path $tempDir "rollback"
$restartManagedByCaller = $env:AMBULANCE_SKIP_WORKER_RESTART -match "^(?i:1|true|yes|on)$"
$workerGuiWasRunning = $false
$workerHeadlessWasRunning = $false
$workerStopped = $false
$replacementAttempted = $false
$updateCommitted = $false
$rollbackComplete = $false
$deferredTransactionPrepared = $false
$deferredTransactionPath = [string]$env:AMBULANCE_UPDATE_TRANSACTION_PATH
$deferredTransactionAction = [string]$env:AMBULANCE_UPDATE_TRANSACTION_ACTION
$restartGuiIntent = $env:AMBULANCE_RESTART_GUI_INTENT -match "^(?i:1|true|yes|on)$"
$restartHeadlessIntent = $env:AMBULANCE_RESTART_HEADLESS_INTENT -match "^(?i:1|true|yes|on)$"
$backedUpFiles = @()
$stoppedProcessIds = @()
$updateOwnerPid = 0
$updateOwnerNonce = ""
$watchdogInstallWarning = ""
Remove-Item Env:AMBULANCE_WATCHDOG_INSTALL_WARNING -ErrorAction SilentlyContinue

function Enter-UpdateLock {
    $lockBase = if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { $env:TEMP } else { $env:LOCALAPPDATA }
    $lockDir = Join-Path $lockBase "AmbulanceReturnBot"
    $lockPath = Join-Path $lockDir "package-update.lock"
    New-Item -ItemType Directory -Path $lockDir -Force | Out-Null
    try {
        $stream = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        throw "Another package update is already in progress: $lockPath"
    }
    try {
        $metadata = [System.Text.Encoding]::UTF8.GetBytes("pid=$PID started_utc=$([DateTime]::UtcNow.ToString('o')) package=$packageDir")
        $stream.SetLength(0)
        $stream.Write($metadata, 0, $metadata.Length)
        $stream.Flush($true)
        return $stream
    } catch {
        $stream.Dispose()
        throw
    }
}

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

function ConvertTo-SafeUpdatePath {
    param(
        [string]$RootDir,
        [string]$RelativePath
    )

    if ([string]::IsNullOrWhiteSpace($RelativePath)) {
        throw "Update manifest contains an empty path."
    }
    $normalized = $RelativePath.Trim().Replace([char]47, [char]92)
    $parts = @($normalized -split "[\\/]")
    if (
        [System.IO.Path]::IsPathRooted($normalized) -or
        $normalized.Contains(":") -or
        $parts.Count -eq 0 -or
        $parts -contains "" -or
        $parts -contains "." -or
        $parts -contains ".."
    ) {
        throw "Unsafe update package path: $RelativePath"
    }

    $safeRelative = $parts -join ([string][char]92)
    $root = [System.IO.Path]::GetFullPath($RootDir).TrimEnd([char]92) + [string][char]92
    $full = [System.IO.Path]::GetFullPath((Join-Path $RootDir $safeRelative))
    if (-not $full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Update package path escapes its root: $RelativePath"
    }
    return $safeRelative
}

function Test-ProtectedUpdatePath {
    param([string]$RelativePath)

    $parts = @($RelativePath -split "[\\/]")
    $protectedDirs = @(
        ".git", "logs", "runtime_outputs", "tmp", "temp", "cache", ".cache",
        "local_data", "snapshots", "__pycache__", "artifacts", "chrome_profile",
        "profiles", "runtime_profiles", "selenium_profiles", "browser_profiles"
    )
    $protectedFiles = @(".env", "update_urls.json", "UPDATE_PACKAGE.bat")
    if ($parts | Where-Object { $protectedDirs -contains $_ }) {
        return $true
    }
    return $protectedFiles -contains $parts[-1]
}

function Assert-NoReparseUpdateParent {
    param(
        [string]$RootDir,
        [string]$RelativePath
    )

    $root = [System.IO.Path]::GetFullPath($RootDir).TrimEnd([char]92)
    $current = Split-Path -Parent ([System.IO.Path]::GetFullPath((Join-Path $RootDir $RelativePath)))
    while ($current -and -not $current.Equals($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $current -PathType Container) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to update through a reparse-point directory: $RelativePath"
            }
        }
        $parent = Split-Path -Parent $current
        if ($parent -eq $current) {
            throw "Could not validate update target path: $RelativePath"
        }
        $current = $parent
    }
}

function Read-UpdateManifest {
    param(
        [string]$RootDir,
        [switch]$RequireFiles
    )

    $manifestPath = Join-Path $RootDir $manifestName
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Update package is missing $manifestName."
    }
    try {
        $payload = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Update manifest is not valid JSON: $($_.Exception.Message)"
    }
    if ([int]$payload.schema_version -ne 1) {
        throw "Update manifest has an unsupported schema version."
    }
    $rawFiles = @($payload.files)
    if ($rawFiles.Count -eq 0 -or $rawFiles.Count -gt 10000) {
        throw "Update manifest file count is invalid: $($rawFiles.Count)"
    }

    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $safePaths = @()
    foreach ($rawPath in $rawFiles) {
        $relative = ConvertTo-SafeUpdatePath -RootDir $RootDir -RelativePath ([string]$rawPath)
        if (Test-ProtectedUpdatePath -RelativePath $relative) {
            throw "Update manifest contains a protected path: $relative"
        }
        if (-not $seen.Add($relative)) {
            throw "Update manifest contains a duplicate path: $relative"
        }
        if ($RequireFiles -and -not (Test-Path -LiteralPath (Join-Path $RootDir $relative) -PathType Leaf)) {
            throw "Update manifest references a missing file: $relative"
        }
        $safePaths += $relative
    }
    if (-not $seen.Contains($manifestName)) {
        throw "Update manifest must manage itself."
    }
    return $safePaths
}

function Get-ObsoleteManagedPaths {
    param(
        [string]$InstalledDir,
        [string[]]$NewManagedPaths
    )

    $manifestPath = Join-Path $InstalledDir $manifestName
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        Write-Host "No previous update manifest; no installed files will be removed."
        return @()
    }
    try {
        $oldManagedPaths = @(Read-UpdateManifest -RootDir $InstalledDir)
    } catch {
        Write-Warning "Previous update manifest is invalid; no installed files will be removed. $($_.Exception.Message)"
        return @()
    }

    $newSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($relative in $NewManagedPaths) {
        [void]$newSet.Add($relative)
    }
    return @($oldManagedPaths | Where-Object { -not $newSet.Contains($_) })
}

function Remove-ManagedFiles {
    param(
        [string]$DestDir,
        [string[]]$RelativePaths
    )

    foreach ($rawPath in $RelativePaths) {
        $relative = ConvertTo-SafeUpdatePath -RootDir $DestDir -RelativePath $rawPath
        if (Test-ProtectedUpdatePath -RelativePath $relative) {
            throw "Refusing to remove a protected update path: $relative"
        }
        Assert-NoReparseUpdateParent -RootDir $DestDir -RelativePath $relative
        $target = Join-Path $DestDir $relative
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            Remove-Item -LiteralPath $target -Force
            Write-Host "Removed obsolete managed file: $relative"
        }
    }
}

function Get-UpdateRelativePaths {
    param([string]$SourceDir)
    $slash = [string][char]92
    $sourceRoot = $SourceDir.TrimEnd([char]92) + $slash

    Get-ChildItem -LiteralPath $SourceDir -Recurse -File -Force | ForEach-Object {
        $relative = ConvertTo-SafeUpdatePath -RootDir $SourceDir -RelativePath $_.FullName.Substring($sourceRoot.Length)
        if (Test-ProtectedUpdatePath -RelativePath $relative) {
            Write-Host "Preserved local file: $($_.Name)"
            return
        }
        $relative
    }
}

function Copy-UpdateTree {
    param(
        [string]$SourceDir,
        [string]$DestDir
    )

    foreach ($relative in @(Get-UpdateRelativePaths -SourceDir $SourceDir)) {
        $source = Join-Path $SourceDir $relative
        Assert-NoReparseUpdateParent -RootDir $DestDir -RelativePath $relative
        $target = Join-Path $DestDir $relative
        if ((Test-Path -LiteralPath $target) -and -not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "Update target exists but is not a file: $relative"
        }
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir | Out-Null
        }
        Copy-Item -LiteralPath $source -Destination $target -Force
        Write-Host "Updated: $relative"
    }
}

function Backup-UpdateTree {
    param(
        [string]$SourceDir,
        [string]$BackupDir,
        [string[]]$RelativePaths
    )

    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    $records = @()
    foreach ($relative in $RelativePaths) {
        $relative = ConvertTo-SafeUpdatePath -RootDir $SourceDir -RelativePath $relative
        if (Test-ProtectedUpdatePath -RelativePath $relative) {
            throw "Refusing to back up a protected update path: $relative"
        }
        Assert-NoReparseUpdateParent -RootDir $SourceDir -RelativePath $relative
        $source = Join-Path $SourceDir $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            continue
        }
        $backup = Join-Path $BackupDir $relative
        $backupParent = Split-Path -Parent $backup
        if (-not (Test-Path -LiteralPath $backupParent)) {
            New-Item -ItemType Directory -Path $backupParent -Force | Out-Null
        }
        Copy-Item -LiteralPath $source -Destination $backup -Force
        $sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
        $backupHash = (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($sourceHash -ne $backupHash) {
            throw "Rollback backup hash mismatch: $relative"
        }
        $records += [PSCustomObject]@{
            path = $relative
            sha256 = $backupHash
        }
    }
    return @($records)
}

function Assert-RollbackBackupSet {
    param(
        [string]$BackupDir,
        [string]$DestDir,
        [string[]]$RelativePaths,
        [object[]]$BackedUpFiles
    )

    if (-not (Test-Path -LiteralPath $BackupDir -PathType Container)) {
        throw "Rollback backup directory is missing: $BackupDir"
    }
    $relativeSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($rawRelative in $RelativePaths) {
        $relative = ConvertTo-SafeUpdatePath -RootDir $DestDir -RelativePath $rawRelative
        if (Test-ProtectedUpdatePath -RelativePath $relative) {
            throw "Rollback backup set contains a protected path: $relative"
        }
        if (-not $relativeSet.Add($relative)) {
            throw "Rollback backup set contains a duplicate restore path: $relative"
        }
        Assert-NoReparseUpdateParent -RootDir $DestDir -RelativePath $relative
        $target = Join-Path $DestDir $relative
        if ((Test-Path -LiteralPath $target) -and -not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "Restore target exists but is not a file: $relative"
        }
    }

    $backedUpSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($record in @($BackedUpFiles)) {
        $relative = ConvertTo-SafeUpdatePath -RootDir $DestDir -RelativePath ([string]$record.path)
        $expectedHash = ([string]$record.sha256).Trim().ToLowerInvariant()
        if (-not $relativeSet.Contains($relative)) {
            throw "Rollback backup record is outside the restore set: $relative"
        }
        if (-not $backedUpSet.Add($relative)) {
            throw "Rollback backup set contains a duplicate backup path: $relative"
        }
        if ($expectedHash -notmatch "^[0-9a-f]{64}$") {
            throw "Rollback backup record has an invalid SHA256: $relative"
        }
        $backup = Join-Path $BackupDir $relative
        if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) {
            throw "Rollback backup file is missing: $relative"
        }
        $actualHash = (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) {
            throw "Rollback backup hash mismatch: $relative"
        }
    }
    return [PSCustomObject]@{ BackedUpPaths = $backedUpSet }
}

function Restore-UpdateTree {
    param(
        [string]$BackupDir,
        [string]$DestDir,
        [string[]]$RelativePaths,
        [object[]]$BackedUpFiles
    )

    # Validate the complete recovery set before mutating any installed file.
    $validated = Assert-RollbackBackupSet `
        -BackupDir $BackupDir `
        -DestDir $DestDir `
        -RelativePaths $RelativePaths `
        -BackedUpFiles $BackedUpFiles
    $errors = @()
    foreach ($rawRelative in $RelativePaths) {
        try {
            $relative = ConvertTo-SafeUpdatePath -RootDir $DestDir -RelativePath $rawRelative
            if (Test-ProtectedUpdatePath -RelativePath $relative) {
                throw "Refusing to restore a protected update path: $relative"
            }
            Assert-NoReparseUpdateParent -RootDir $DestDir -RelativePath $relative
            $target = Join-Path $DestDir $relative
            $backup = Join-Path $BackupDir $relative
            if ($validated.BackedUpPaths.Contains($relative)) {
                if ((Test-Path -LiteralPath $target) -and -not (Test-Path -LiteralPath $target -PathType Leaf)) {
                    throw "Restore target exists but is not a file: $relative"
                }
                $targetDir = Split-Path -Parent $target
                if (-not (Test-Path -LiteralPath $targetDir)) {
                    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
                }
                Copy-Item -LiteralPath $backup -Destination $target -Force
            } elseif (Test-Path -LiteralPath $target -PathType Leaf) {
                Remove-Item -LiteralPath $target -Force
            }
        } catch {
            $errors += "$rawRelative`: $($_.Exception.Message)"
        }
    }
    if ($errors.Count -gt 0) {
        throw "Update rollback incomplete: $($errors -join '; ')"
    }
    foreach ($rawRelative in $RelativePaths) {
        $relative = ConvertTo-SafeUpdatePath -RootDir $DestDir -RelativePath $rawRelative
        $target = Join-Path $DestDir $relative
        if ($validated.BackedUpPaths.Contains($relative)) {
            $record = @($BackedUpFiles | Where-Object { ([string]$_.path).Equals($relative, [System.StringComparison]::OrdinalIgnoreCase) })[0]
            if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
                throw "Restored file is missing: $relative"
            }
            $restoredHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($restoredHash -ne ([string]$record.sha256).ToLowerInvariant()) {
                throw "Restored file hash mismatch: $relative"
            }
        } elseif (Test-Path -LiteralPath $target) {
            throw "Rollback could not remove newly installed path: $relative"
        }
    }
}

function Assert-InstalledUpdateTree {
    param(
        [string]$SourceDir,
        [string]$DestDir,
        [string[]]$NewManagedPaths,
        [string[]]$ObsoleteManagedPaths
    )

    foreach ($rawRelative in $NewManagedPaths) {
        $relative = ConvertTo-SafeUpdatePath -RootDir $DestDir -RelativePath $rawRelative
        Assert-NoReparseUpdateParent -RootDir $SourceDir -RelativePath $relative
        Assert-NoReparseUpdateParent -RootDir $DestDir -RelativePath $relative
        $source = Join-Path $SourceDir $relative
        $target = Join-Path $DestDir $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Staged update file is missing during verification: $relative"
        }
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "Installed update file is missing during verification: $relative"
        }
        $sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
        $targetHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        if ($sourceHash -ne $targetHash) {
            throw "Installed update hash mismatch: $relative"
        }
    }
    foreach ($rawRelative in $ObsoleteManagedPaths) {
        $relative = ConvertTo-SafeUpdatePath -RootDir $DestDir -RelativePath $rawRelative
        Assert-NoReparseUpdateParent -RootDir $DestDir -RelativePath $relative
        if (Test-Path -LiteralPath (Join-Path $DestDir $relative) -PathType Leaf) {
            throw "Obsolete managed file still exists after update: $relative"
        }
    }
}

function Get-UpdateStateRoot {
    $stateBase = if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { $env:TEMP } else { $env:LOCALAPPDATA }
    return Join-Path $stateBase "AmbulanceReturnBot"
}

function Get-PackageIdentity {
    $normalized = [System.IO.Path]::GetFullPath($packageDir).TrimEnd([char]92).ToLowerInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($bytes)
    } finally {
        $sha.Dispose()
    }
    return (($hash | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 32)
}

function Get-DeferredTransactionRoot {
    return Join-Path (Get-UpdateStateRoot) "update_transactions"
}

function Resolve-DeferredTransactionPath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Deferred update transaction path is missing."
    }
    $transactionRoot = Get-DeferredTransactionRoot
    $rootFull = [System.IO.Path]::GetFullPath($transactionRoot).TrimEnd([char]92)
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $expectedPrefix = (Get-PackageIdentity) + "-"
    if (-not $pathFull.StartsWith($rootFull + [string][char]92, [System.StringComparison]::OrdinalIgnoreCase) -or
        [System.IO.Path]::GetExtension($pathFull) -ne ".json" -or
        -not (Split-Path -Leaf $pathFull).StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe deferred update transaction path: $Path"
    }
    return $pathFull
}

function New-DeferredTransactionPath {
    $transactionRoot = Get-DeferredTransactionRoot
    New-Item -ItemType Directory -Path $transactionRoot -Force | Out-Null
    return Join-Path $transactionRoot ("{0}-{1}.json" -f (Get-PackageIdentity), [guid]::NewGuid().ToString("N"))
}

function Resolve-DeferredTempPath {
    param([string]$Path)

    $tempRoot = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd([char]92)
    $pathFull = [System.IO.Path]::GetFullPath($Path).TrimEnd([char]92)
    if (-not $pathFull.StartsWith($tempRoot + [string][char]92, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $pathFull) -notlike "AmbulanceReturnBotUpdate-*") {
        throw "Unsafe deferred update temporary path: $Path"
    }
    return $pathFull
}

function Write-DeferredUpdateTransaction {
    param(
        [string]$TransactionPath,
        [string]$PreviousVersion,
        [string]$NewVersion,
        [string[]]$RelativePaths,
        [object[]]$BackedUpFiles,
        [bool]$WorkerGuiWasRunning,
        [bool]$WorkerHeadlessWasRunning,
        [string]$RequestId
    )

    $safeTransactionPath = Resolve-DeferredTransactionPath -Path $TransactionPath
    $transactionDir = Split-Path -Parent $safeTransactionPath
    New-Item -ItemType Directory -Path $transactionDir -Force | Out-Null
    $safeTempDir = Resolve-DeferredTempPath -Path $tempDir
    [void](Assert-RollbackBackupSet `
        -BackupDir $rollbackDir `
        -DestDir $packageDir `
        -RelativePaths $RelativePaths `
        -BackedUpFiles $BackedUpFiles)
    $ownerPid = 0
    [void][int]::TryParse([string]$env:AMBULANCE_UPDATE_OWNER_PID, [ref]$ownerPid)
    if ($ownerPid -le 0) {
        $ownerPid = $PID
        $env:AMBULANCE_UPDATE_OWNER_PID = [string]$ownerPid
    }
    $ownerNonce = [string]$env:AMBULANCE_UPDATE_OWNER_NONCE
    if ([string]::IsNullOrWhiteSpace($ownerNonce)) {
        $ownerNonce = $runId
        $env:AMBULANCE_UPDATE_OWNER_NONCE = $ownerNonce
    }
    $script:updateOwnerPid = $ownerPid
    $script:updateOwnerNonce = $ownerNonce
    $payload = [ordered]@{
        schema_version = 2
        phase = "prepared"
        package_id = Get-PackageIdentity
        package_dir = [System.IO.Path]::GetFullPath($packageDir)
        temp_dir = $safeTempDir
        rollback_dir = [System.IO.Path]::GetFullPath($rollbackDir)
        relative_paths = @($RelativePaths)
        backed_up_files = @($BackedUpFiles | ForEach-Object {
            [ordered]@{
                path = [string]$_.path
                sha256 = ([string]$_.sha256).ToLowerInvariant()
            }
        })
        previous_version = $PreviousVersion
        new_version = $NewVersion
        worker_gui_was_running = $WorkerGuiWasRunning
        worker_headless_was_running = $WorkerHeadlessWasRunning
        request_id = $RequestId
        owner_pid = $ownerPid
        owner_nonce = $ownerNonce
        owner_heartbeat_path = $safeTransactionPath + ".owner.heartbeat"
        created_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $transactionTemp = Join-Path $transactionDir (".{0}.{1}.tmp" -f (Split-Path -Leaf $safeTransactionPath), [guid]::NewGuid().ToString("N"))
    try {
        $json = $payload | ConvertTo-Json -Depth 6
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $stream = [System.IO.File]::Open(
            $transactionTemp,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        try {
            $bytes = $utf8NoBom.GetBytes($json)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        } finally {
            $stream.Dispose()
        }
        Move-Item -LiteralPath $transactionTemp -Destination $safeTransactionPath -Force
    } finally {
        if (Test-Path -LiteralPath $transactionTemp -PathType Leaf) {
            Remove-Item -LiteralPath $transactionTemp -Force -ErrorAction SilentlyContinue
        }
    }
}

function Read-DeferredUpdateTransaction {
    param([string]$TransactionPath)

    $safeTransactionPath = Resolve-DeferredTransactionPath -Path $TransactionPath
    if (-not (Test-Path -LiteralPath $safeTransactionPath -PathType Leaf)) {
        throw "Deferred update transaction file is missing: $safeTransactionPath"
    }
    try {
        $payload = Get-Content -LiteralPath $safeTransactionPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Deferred update transaction is invalid JSON: $($_.Exception.Message)"
    }
    if ([int]$payload.schema_version -ne 2 -or [string]$payload.phase -ne "prepared") {
        throw "Deferred update transaction schema is unsupported."
    }
    if ([string]$payload.package_id -ne (Get-PackageIdentity)) {
        throw "Deferred update transaction package identity is invalid."
    }
    $expectedPackage = [System.IO.Path]::GetFullPath($packageDir).TrimEnd([char]92)
    $payloadPackage = [System.IO.Path]::GetFullPath([string]$payload.package_dir).TrimEnd([char]92)
    if (-not $payloadPackage.Equals($expectedPackage, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Deferred update transaction belongs to a different package."
    }
    $safeTempDir = Resolve-DeferredTempPath -Path ([string]$payload.temp_dir)
    $safeRollbackDir = [System.IO.Path]::GetFullPath([string]$payload.rollback_dir).TrimEnd([char]92)
    $expectedRollbackDir = (Join-Path $safeTempDir "rollback").TrimEnd([char]92)
    if (-not $safeRollbackDir.Equals($expectedRollbackDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Deferred update rollback directory is invalid."
    }
    $relativePaths = @($payload.relative_paths)
    if ($relativePaths.Count -eq 0 -or $relativePaths.Count -gt 10000) {
        throw "Deferred update transaction path count is invalid."
    }
    $safeRelativePaths = @()
    $relativeSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($rawRelative in $relativePaths) {
        $relative = ConvertTo-SafeUpdatePath -RootDir $packageDir -RelativePath ([string]$rawRelative)
        if (Test-ProtectedUpdatePath -RelativePath $relative) {
            throw "Deferred update transaction contains a protected path: $relative"
        }
        if (-not $relativeSet.Add($relative)) {
            throw "Deferred update transaction contains a duplicate path: $relative"
        }
        $safeRelativePaths += $relative
    }
    $safeBackedUpFiles = @()
    $backedUpSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($record in @($payload.backed_up_files)) {
        $relative = ConvertTo-SafeUpdatePath -RootDir $packageDir -RelativePath ([string]$record.path)
        $sha256 = ([string]$record.sha256).Trim().ToLowerInvariant()
        if (-not $relativeSet.Contains($relative) -or -not $backedUpSet.Add($relative)) {
            throw "Deferred update transaction contains an invalid backup record: $relative"
        }
        if ($sha256 -notmatch "^[0-9a-f]{64}$") {
            throw "Deferred update transaction contains an invalid backup hash: $relative"
        }
        $safeBackedUpFiles += [PSCustomObject]@{ path = $relative; sha256 = $sha256 }
    }
    $previousVersion = [string]$payload.previous_version
    $newVersion = [string]$payload.new_version
    if (-not (Test-VersionText -Version $previousVersion -AllowZero) -or -not (Test-VersionText -Version $newVersion)) {
        throw "Deferred update transaction contains an invalid version."
    }
    $ownerPid = [int]$payload.owner_pid
    $ownerNonce = [string]$payload.owner_nonce
    if ($ownerPid -lt 0 -or $ownerNonce.Length -gt 128 -or ($ownerNonce -and $ownerNonce -notmatch "^[A-Za-z0-9._-]+$")) {
        throw "Deferred update transaction contains invalid owner metadata."
    }
    if ([string]$payload.owner_heartbeat_path -ne ($safeTransactionPath + ".owner.heartbeat")) {
        throw "Deferred update transaction contains an invalid heartbeat path."
    }
    return [PSCustomObject]@{
        TransactionPath = $safeTransactionPath
        TempDir = $safeTempDir
        RollbackDir = $safeRollbackDir
        RelativePaths = $safeRelativePaths
        BackedUpFiles = $safeBackedUpFiles
        PreviousVersion = $previousVersion
        NewVersion = $newVersion
        WorkerGuiWasRunning = [bool]$payload.worker_gui_was_running
        WorkerHeadlessWasRunning = [bool]$payload.worker_headless_was_running
        RequestId = [string]$payload.request_id
        OwnerPid = $ownerPid
        OwnerNonce = $ownerNonce
    }
}

function Complete-DeferredUpdateTransaction {
    param([object]$Transaction)

    $safeTempDir = Resolve-DeferredTempPath -Path $Transaction.TempDir
    # The marker is the commit/rollback authority. Retire it before deleting
    # recovery data so a crash can never leave a live marker pointing at an
    # empty rollback directory.
    if (Test-Path -LiteralPath $Transaction.TransactionPath -PathType Leaf) {
        Remove-Item -LiteralPath $Transaction.TransactionPath -Force
    }
    Get-ChildItem -LiteralPath (Split-Path -Parent $Transaction.TransactionPath) -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like ((Split-Path -Leaf $Transaction.TransactionPath) + ".probe-*.ready") } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath ($Transaction.TransactionPath + ".owner.heartbeat") -Force -ErrorAction SilentlyContinue
    try {
        if (Test-Path -LiteralPath $safeTempDir -PathType Container) {
            Remove-Item -LiteralPath $safeTempDir -Recurse -Force
        }
    } catch {
        Write-Warning "Committed update left a removable temporary folder: $safeTempDir"
    }
}

function Invoke-DeferredUpdateRollback {
    param(
        [string]$TransactionPath,
        [switch]$RestartRecordedRuntimes
    )

    $transaction = Read-DeferredUpdateTransaction -TransactionPath $TransactionPath
    try {
        $partialState = Get-WorkerRuntimeState
        $stoppedIds = @()
        if (@($partialState.Processes).Count -gt 0) {
            $stoppedIds = @(Stop-WorkerPackageProcesses -Processes @($partialState.Processes))
        }
        Restore-UpdateTree `
            -BackupDir $transaction.RollbackDir `
            -DestDir $packageDir `
            -RelativePaths $transaction.RelativePaths `
            -BackedUpFiles $transaction.BackedUpFiles
        $restoredVersion = if (Test-Path -LiteralPath $localVersionPath -PathType Leaf) {
            (Get-Content -LiteralPath $localVersionPath -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
        } else {
            ""
        }
        if ($restoredVersion -ne $transaction.PreviousVersion) {
            throw "Restored VERSION.txt mismatch. Expected $($transaction.PreviousVersion) but got $restoredVersion."
        }
        Complete-DeferredUpdateTransaction -Transaction $transaction
        Remove-Item Env:AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH -ErrorAction SilentlyContinue
        if ($RestartRecordedRuntimes) {
            Restart-WorkerRuntimes `
                -StartGui $transaction.WorkerGuiWasRunning `
                -StartHeadless $transaction.WorkerHeadlessWasRunning `
                -ExcludedProcessIds $stoppedIds
        }
    } catch {
        throw "Deferred update rollback failed. Recovery files: $($transaction.RollbackDir). $($_.Exception.Message)"
    }
}

function Get-PendingDeferredTransactionPaths {
    $transactionRoot = Get-DeferredTransactionRoot
    if (-not (Test-Path -LiteralPath $transactionRoot -PathType Container)) {
        return @()
    }
    $prefix = (Get-PackageIdentity) + "-"
    return @(
        Get-ChildItem -LiteralPath $transactionRoot -File -Filter "$prefix*.json" -Force |
            ForEach-Object { $_.FullName }
    )
}

function Recover-PendingDeferredUpdate {
    $pendingPaths = @(Get-PendingDeferredTransactionPaths)
    if ($pendingPaths.Count -gt 1) {
        throw "Multiple pending update transactions require manual recovery: $($pendingPaths -join ', ')"
    }
    if ($pendingPaths.Count -eq 1) {
        Write-Warning "Recovering interrupted update transaction before checking the remote version: $($pendingPaths[0])"
        Invoke-DeferredUpdateRollback `
            -TransactionPath $pendingPaths[0] `
            -RestartRecordedRuntimes:(-not $restartManagedByCaller)
    }
}

function Get-WorkerPackageProcesses {
    $packagePath = [System.IO.Path]::GetFullPath($packageDir).TrimEnd([char]92) + [string][char]92
    $allProcesses = @(
        Get-CimInstance Win32_Process `
            -Property ProcessId,ParentProcessId,Name,CommandLine `
            -OperationTimeoutSec 5 `
            -ErrorAction Stop
    )
    $directProcesses = @(
        $allProcesses | Where-Object {
            $commandLine = [string]$_.CommandLine
            $commandLine -and
            $commandLine.IndexOf($packagePath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            (
                $commandLine -match "(?<![A-Za-z0-9_])(?:worker_gui|worker|app)\.py(?![A-Za-z0-9_])" -or
                $commandLine -match "(?<![A-Za-z0-9_])run_worker_headless\.bat(?![A-Za-z0-9_])"
            )
        }
    )

    # Older packages launched worker.py by relative path. The persistent cmd.exe
    # still names this package's absolute headless launcher, so only its worker.py
    # descendants are included. Other packages and updater descendants are ignored.
    $headlessLauncherIds = @(
        $directProcesses |
            Where-Object { ([string]$_.CommandLine) -match "(?<![A-Za-z0-9_])run_worker_headless\.bat(?![A-Za-z0-9_])" } |
            ForEach-Object { [int]$_.ProcessId }
    )
    $descendantIds = @($headlessLauncherIds)
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($process in $allProcesses) {
            if ($descendantIds -contains [int]$process.ParentProcessId -and $descendantIds -notcontains [int]$process.ProcessId) {
                $descendantIds += [int]$process.ProcessId
                $changed = $true
            }
        }
    }
    $legacyHeadlessWorkers = @(
        $allProcesses | Where-Object {
            $descendantIds -contains [int]$_.ProcessId -and
            ([string]$_.CommandLine) -match "(?<![A-Za-z0-9_])worker\.py(?![A-Za-z0-9_])"
        }
    )
    return @(($directProcesses + $legacyHeadlessWorkers) | Group-Object ProcessId | ForEach-Object { $_.Group[0] })
}

function Get-WorkerRuntimeState {
    $processes = @(Get-WorkerPackageProcesses)
    $guiProcesses = @($processes | Where-Object { ([string]$_.CommandLine) -match "(?<![A-Za-z0-9_])worker_gui\.py(?![A-Za-z0-9_])" })
    $headlessProcesses = @($processes | Where-Object { ([string]$_.CommandLine) -match "(?<![A-Za-z0-9_])worker\.py(?![A-Za-z0-9_])" })
    return [pscustomobject]@{
        Processes = $processes
        GuiProcesses = $guiProcesses
        HeadlessProcesses = $headlessProcesses
        Gui = $guiProcesses.Count -gt 0
        Headless = $headlessProcesses.Count -gt 0
    }
}

function Stop-WorkerPackageProcesses {
    param([object[]]$Processes)

    $processes = @($Processes)
    $targetIds = @($processes | ForEach-Object { [int]$_.ProcessId } | Sort-Object -Unique)
    foreach ($process in $processes) {
        Write-Host "Stopping running worker process: $($process.ProcessId) $($process.Name)"
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        } catch {
            if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) {
                throw "Could not stop worker process $($process.ProcessId): $($_.Exception.Message)"
            }
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $remaining = @($targetIds | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        $unexpected = @((Get-WorkerPackageProcesses) | Where-Object { $targetIds -notcontains [int]$_.ProcessId })
        if ($remaining.Count -eq 0 -and $unexpected.Count -eq 0) {
            return @($targetIds)
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    $remainingIds = @($remaining + ($unexpected | ForEach-Object { [int]$_.ProcessId }) | Sort-Object -Unique)
    throw "Worker processes did not stop within 15 seconds: $($remainingIds -join ', ')"
}

function Start-WorkerGui {
    if ((Get-WorkerRuntimeState).Gui) {
        return
    }
    $launcher = Join-Path $packageDir "RUN_WORKER_GUI_WINPYTHON.vbs"
    if (Test-Path -LiteralPath $launcher -PathType Leaf) {
        Write-Host "Restarting worker GUI..."
        Start-Process -FilePath "wscript.exe" -ArgumentList ('"' + $launcher + '"') -WorkingDirectory $packageDir -WindowStyle Hidden | Out-Null
    } else {
        throw "Cannot restart worker GUI because launcher is missing: $launcher"
    }
}

function Start-WorkerHeadless {
    if ((Get-WorkerRuntimeState).Headless) {
        return
    }
    $launcher = Join-Path $packageDir "run_worker_headless.bat"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Cannot restart headless worker because launcher is missing: $launcher"
    }
    Write-Host "Restarting headless worker..."
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", ('"' + $launcher + '"') -WorkingDirectory $packageDir -WindowStyle Hidden | Out-Null
}

function Get-InstalledPackageVersion {
    if (-not (Test-Path -LiteralPath $localVersionPath -PathType Leaf)) {
        return "0"
    }
    return (Get-Content -LiteralPath $localVersionPath -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
}

function Write-UpdateOwnerHeartbeat {
    param([string]$TransactionPath)

    $safeTransactionPath = Resolve-DeferredTransactionPath -Path $TransactionPath
    $heartbeatPath = $safeTransactionPath + ".owner.heartbeat"
    $heartbeatTemp = $heartbeatPath + "." + [guid]::NewGuid().ToString("N") + ".tmp"
    $payload = [ordered]@{
        owner_pid = $script:updateOwnerPid
        owner_nonce = $script:updateOwnerNonce
        updated_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    try {
        [System.IO.File]::WriteAllText($heartbeatTemp, ($payload | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $heartbeatTemp -Destination $heartbeatPath -Force
    } finally {
        Remove-Item -LiteralPath $heartbeatTemp -Force -ErrorAction SilentlyContinue
    }
}

function Suspend-UpdateControlEnvironmentForProbe {
    param([string]$ProbeTransactionPath)

    $names = @(
        "AMBULANCE_SKIP_WORKER_RESTART",
        "AMBULANCE_UPDATE_LOCK_HELD",
        "AMBULANCE_UPDATE_TRANSACTION_PATH",
        "AMBULANCE_UPDATE_TRANSACTION_ACTION",
        "AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH",
        "AMBULANCE_UPDATE_REQUEST_ID",
        "AMBULANCE_RESTART_GUI_INTENT",
        "AMBULANCE_RESTART_HEADLESS_INTENT",
        "AMBULANCE_UPDATE_OWNER_PID",
        "AMBULANCE_UPDATE_OWNER_NONCE"
    )
    $snapshot = @{}
    foreach ($name in $names) {
        $value = [System.Environment]::GetEnvironmentVariable($name, [System.EnvironmentVariableTarget]::Process)
        if ($null -ne $value) {
            $snapshot[$name] = $value
        }
        [System.Environment]::SetEnvironmentVariable($name, $null, [System.EnvironmentVariableTarget]::Process)
    }
    [System.Environment]::SetEnvironmentVariable(
        "AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH",
        $ProbeTransactionPath,
        [System.EnvironmentVariableTarget]::Process
    )
    return $snapshot
}

function Restore-UpdateControlEnvironment {
    param([hashtable]$Snapshot)

    $names = @(
        "AMBULANCE_SKIP_WORKER_RESTART",
        "AMBULANCE_UPDATE_LOCK_HELD",
        "AMBULANCE_UPDATE_TRANSACTION_PATH",
        "AMBULANCE_UPDATE_TRANSACTION_ACTION",
        "AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH",
        "AMBULANCE_UPDATE_REQUEST_ID",
        "AMBULANCE_RESTART_GUI_INTENT",
        "AMBULANCE_RESTART_HEADLESS_INTENT",
        "AMBULANCE_UPDATE_OWNER_PID",
        "AMBULANCE_UPDATE_OWNER_NONCE"
    )
    foreach ($name in $names) {
        [System.Environment]::SetEnvironmentVariable($name, $null, [System.EnvironmentVariableTarget]::Process)
    }
    foreach ($entry in $Snapshot.GetEnumerator()) {
        [System.Environment]::SetEnvironmentVariable(
            [string]$entry.Key,
            [string]$entry.Value,
            [System.EnvironmentVariableTarget]::Process
        )
    }
}

function Test-WorkerProbeReady {
    param(
        [object]$Process,
        [string]$RuntimeKind,
        [string]$TransactionPath
    )

    $readyPath = "{0}.probe-{1}.ready" -f $TransactionPath, $Process.ProcessId
    if (-not (Test-Path -LiteralPath $readyPath -PathType Leaf)) {
        return $false
    }
    try {
        $payload = Get-Content -LiteralPath $readyPath -Raw -Encoding UTF8 | ConvertFrom-Json
        return (
            [int]$payload.pid -eq [int]$Process.ProcessId -and
            [string]$payload.runtime_kind -eq $RuntimeKind -and
            [string]$payload.version -eq (Get-InstalledPackageVersion) -and
            [System.IO.Path]::GetFullPath([string]$payload.transaction_path).Equals(
                [System.IO.Path]::GetFullPath($TransactionPath),
                [System.StringComparison]::OrdinalIgnoreCase
            )
        )
    } catch {
        return $false
    }
}

function Wait-WorkerRuntime {
    param(
        [bool]$RequireGui,
        [bool]$RequireHeadless,
        [int]$TimeoutSeconds = 30,
        [int[]]$ExcludedProcessIds = @(),
        [string]$ProbeTransactionPath = ""
    )

    if (-not $RequireGui -and -not $RequireHeadless) {
        return
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $readySince = $null
    $readyProcessKey = ""
    do {
        if (-not [string]::IsNullOrWhiteSpace($ProbeTransactionPath)) {
            Write-UpdateOwnerHeartbeat -TransactionPath $ProbeTransactionPath
        }
        $state = Get-WorkerRuntimeState
        $guiCandidates = @($state.GuiProcesses | Where-Object { $ExcludedProcessIds -notcontains [int]$_.ProcessId })
        $headlessCandidates = @($state.HeadlessProcesses | Where-Object { $ExcludedProcessIds -notcontains [int]$_.ProcessId })
        $guiReady = (-not $RequireGui) -or $guiCandidates.Count -gt 0
        $headlessReady = (-not $RequireHeadless) -or $headlessCandidates.Count -gt 0
        if (-not [string]::IsNullOrWhiteSpace($ProbeTransactionPath)) {
            if ($RequireGui) {
                $guiReady = @($guiCandidates | Where-Object {
                    Test-WorkerProbeReady -Process $_ -RuntimeKind "gui" -TransactionPath $ProbeTransactionPath
                }).Count -gt 0
            }
            if ($RequireHeadless) {
                $headlessReady = @($headlessCandidates | Where-Object {
                    Test-WorkerProbeReady -Process $_ -RuntimeKind "headless" -TransactionPath $ProbeTransactionPath
                }).Count -gt 0
            }
        }
        $processKey = @(
            if ($RequireGui) { $guiCandidates | ForEach-Object { "g$($_.ProcessId)" } }
            if ($RequireHeadless) { $headlessCandidates | ForEach-Object { "h$($_.ProcessId)" } }
        ) | Sort-Object
        $processKey = $processKey -join ","
        if ($guiReady -and $headlessReady) {
            if ($null -eq $readySince -or $processKey -ne $readyProcessKey) {
                $readySince = [DateTime]::UtcNow
                $readyProcessKey = $processKey
            } elseif (([DateTime]::UtcNow - $readySince).TotalSeconds -ge 2) {
                Write-Host "Worker runtime health check passed after a stable startup window."
                return $state
            }
        } else {
            $readySince = $null
            $readyProcessKey = ""
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Worker runtime health check timed out. Gui=$RequireGui Headless=$RequireHeadless"
}

function Restart-WorkerRuntimes {
    param(
        [bool]$StartGui,
        [bool]$StartHeadless,
        [int[]]$ExcludedProcessIds = @(),
        [string]$ProbeTransactionPath = ""
    )

    if ($StartHeadless) {
        Start-WorkerHeadless
    }
    if ($StartGui) {
        Start-WorkerGui
    }
    return Wait-WorkerRuntime `
        -RequireGui $StartGui `
        -RequireHeadless $StartHeadless `
        -ExcludedProcessIds $ExcludedProcessIds `
        -ProbeTransactionPath $ProbeTransactionPath
}

function Install-StartupLaunchers {
    $installer = Join-Path $packageDir "install_startup_shortcut.ps1"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        $message = "Cannot refresh startup launcher because installer is missing: $installer"
        Write-Warning $message
        return $message
    }
    Write-Host "Refreshing startup launcher and watchdog..."
    $installerExitCode = $null
    $installerException = $null
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $installer -SkipScheduledTask 2>&1 | Out-Host
        $installerExitCode = $LASTEXITCODE
    } catch {
        $installerException = $_.Exception
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($null -ne $installerException) {
        $message = "Startup launcher and watchdog refresh could not start: $($installerException.Message)"
        Write-Warning $message
        return $message
    }
    if ($null -eq $installerExitCode) {
        $message = "Startup launcher and watchdog refresh could not report an exit code."
        Write-Warning $message
        return $message
    }
    if ($installerExitCode -ne 0) {
        $message = "Startup launcher and watchdog refresh exited with code $installerExitCode"
        Write-Warning $message
        return $message
    }
    return ""
}

$updateLockStream = $null
if ($env:AMBULANCE_UPDATE_LOCK_HELD -notmatch "^(?i:1|true|yes|on)$") {
    $updateLockStream = Enter-UpdateLock
}

try {
if (-not [string]::IsNullOrWhiteSpace($deferredTransactionAction)) {
    if ([string]::IsNullOrWhiteSpace($deferredTransactionPath)) {
        throw "AMBULANCE_UPDATE_TRANSACTION_PATH is required for transaction actions."
    }
    switch ($deferredTransactionAction.Trim().ToLowerInvariant()) {
        "rollback" {
            Invoke-DeferredUpdateRollback -TransactionPath $deferredTransactionPath
            Write-Host "Deferred update rollback completed."
        }
        "finalize" {
            $transaction = Read-DeferredUpdateTransaction -TransactionPath $deferredTransactionPath
            Complete-DeferredUpdateTransaction -Transaction $transaction
            Write-Host "Deferred update transaction finalized."
        }
        default {
            throw "Unsupported deferred update transaction action: $deferredTransactionAction"
        }
    }
    return
}
Recover-PendingDeferredUpdate
if ([string]::IsNullOrWhiteSpace($deferredTransactionPath)) {
    $deferredTransactionPath = New-DeferredTransactionPath
} else {
    $deferredTransactionPath = Resolve-DeferredTransactionPath -Path $deferredTransactionPath
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
    return
}

try {
    New-Item -ItemType Directory -Path $tempDir | Out-Null

    Write-Host "Downloading update package..."
    Invoke-WebRequest -Uri $remoteZipUrl -OutFile $zipPath -UseBasicParsing -MaximumRedirection 5

    if (-not (Test-Path -LiteralPath $zipPath) -or (Get-Item -LiteralPath $zipPath).Length -lt 1024) {
        throw "Downloaded package is missing or too small."
    }
    $downloadedSha256 = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($downloadedSha256 -ne $remoteSha256) {
        throw "Downloaded package SHA256 mismatch. Expected $remoteSha256 but got $downloadedSha256."
    }

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

    $newManagedPaths = @(Read-UpdateManifest -RootDir $sourceDir -RequireFiles)
    $relativePaths = @(Get-UpdateRelativePaths -SourceDir $sourceDir)
    if ($newManagedPaths.Count -eq 0 -or $relativePaths.Count -eq 0) {
        throw "Update zip does not contain any replaceable package files."
    }
    $manifestSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($relative in $newManagedPaths) {
        [void]$manifestSet.Add($relative)
    }
    $unexpectedPaths = @($relativePaths | Where-Object { -not $manifestSet.Contains($_) })
    if ($unexpectedPaths.Count -gt 0 -or $manifestSet.Count -ne $relativePaths.Count) {
        throw "Update manifest does not exactly match the replaceable package files."
    }

    $obsoleteManagedPaths = @(Get-ObsoleteManagedPaths -InstalledDir $packageDir -NewManagedPaths $newManagedPaths)
    $rollbackPaths = @(($newManagedPaths + $obsoleteManagedPaths) | Sort-Object -Unique)
    $backedUpFiles = @(Backup-UpdateTree -SourceDir $packageDir -BackupDir $rollbackDir -RelativePaths $rollbackPaths)

    $runtimeState = Get-WorkerRuntimeState
    $workerProcesses = @($runtimeState.Processes)
    $workerGuiWasRunning = [bool]$runtimeState.Gui -or $restartGuiIntent
    $workerHeadlessWasRunning = [bool]$runtimeState.Headless -or $restartHeadlessIntent

    Write-DeferredUpdateTransaction `
        -TransactionPath $deferredTransactionPath `
        -PreviousVersion $localVersion `
        -NewVersion $packageVersion `
        -RelativePaths $rollbackPaths `
        -BackedUpFiles $backedUpFiles `
        -WorkerGuiWasRunning $workerGuiWasRunning `
        -WorkerHeadlessWasRunning $workerHeadlessWasRunning `
        -RequestId ([string]$env:AMBULANCE_UPDATE_REQUEST_ID)
    $deferredTransactionPrepared = $true

    $replacementAttempted = $true
    try {
        if ($workerProcesses.Count -gt 0) {
            $stoppedProcessIds = @(Stop-WorkerPackageProcesses -Processes $workerProcesses)
            $workerStopped = $true
        }
        Copy-UpdateTree -SourceDir $sourceDir -DestDir $packageDir
        Remove-ManagedFiles -DestDir $packageDir -RelativePaths $obsoleteManagedPaths
        $installedVersion = Get-InstalledPackageVersion
        if ($installedVersion -ne $packageVersion) {
            throw "Installed VERSION.txt mismatch. Expected $packageVersion but got $installedVersion."
        }
        Assert-InstalledUpdateTree `
            -SourceDir $sourceDir `
            -DestDir $packageDir `
            -NewManagedPaths $newManagedPaths `
            -ObsoleteManagedPaths $obsoleteManagedPaths
        $watchdogInstallWarning = Install-StartupLaunchers
        if (-not [string]::IsNullOrWhiteSpace([string]$watchdogInstallWarning)) {
            $env:AMBULANCE_WATCHDOG_INSTALL_WARNING = [string]$watchdogInstallWarning
        } else {
            Remove-Item Env:AMBULANCE_WATCHDOG_INSTALL_WARNING -ErrorAction SilentlyContinue
        }
        $updateCommitted = $true
    } catch {
        $replacementError = $_.Exception
        Write-Warning "Update replacement failed; restoring the previous package. $($replacementError.Message)"
        try {
            Restore-UpdateTree `
                -BackupDir $rollbackDir `
                -DestDir $packageDir `
                -RelativePaths $rollbackPaths `
                -BackedUpFiles $backedUpFiles
            $transaction = Read-DeferredUpdateTransaction -TransactionPath $deferredTransactionPath
            Complete-DeferredUpdateTransaction -Transaction $transaction
            $deferredTransactionPrepared = $false
            $rollbackComplete = $true
        } catch {
            throw "Update replacement failed and rollback also failed. Recovery files: $rollbackDir. Replacement: $($replacementError.Message) Rollback: $($_.Exception.Message)"
        }
        throw $replacementError
    }

    Write-Host "Update completed."
} finally {
    $restartFailure = $null
    $runtimePackageSafe = (-not $replacementAttempted) -or $updateCommitted -or $rollbackComplete
    if (-not $restartManagedByCaller -and $runtimePackageSafe) {
        try {
            if ($workerStopped -or $workerGuiWasRunning -or $workerHeadlessWasRunning) {
                $probePath = ""
                if ($updateCommitted -and $deferredTransactionPrepared) {
                    $probePath = $deferredTransactionPath
                    $env:AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH = $probePath
                    Write-UpdateOwnerHeartbeat -TransactionPath $probePath
                }
                if ($probePath) {
                    $probeEnvironment = Suspend-UpdateControlEnvironmentForProbe -ProbeTransactionPath $probePath
                    try {
                        [void](Restart-WorkerRuntimes `
                            -StartGui $workerGuiWasRunning `
                            -StartHeadless $workerHeadlessWasRunning `
                            -ExcludedProcessIds $stoppedProcessIds `
                            -ProbeTransactionPath $probePath)
                    } finally {
                        Restore-UpdateControlEnvironment -Snapshot $probeEnvironment
                    }
                } else {
                    [void](Restart-WorkerRuntimes `
                        -StartGui $workerGuiWasRunning `
                        -StartHeadless $workerHeadlessWasRunning `
                        -ExcludedProcessIds $stoppedProcessIds)
                }
            }
            if ($updateCommitted -and $deferredTransactionPrepared) {
                $transaction = Read-DeferredUpdateTransaction -TransactionPath $deferredTransactionPath
                Complete-DeferredUpdateTransaction -Transaction $transaction
                Remove-Item Env:AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH -ErrorAction SilentlyContinue
                $deferredTransactionPrepared = $false
            }
        } catch {
            $restartFailure = $_.Exception
            if ($updateCommitted) {
                Write-Warning "Updated worker runtime failed its health check; restoring the previous package. $($restartFailure.Message)"
                try {
                    Invoke-DeferredUpdateRollback `
                        -TransactionPath $deferredTransactionPath `
                        -RestartRecordedRuntimes
                    $rollbackComplete = $true
                    $updateCommitted = $false
                    $deferredTransactionPrepared = $false
                } catch {
                    $rollbackComplete = $false
                    $restartFailure = [System.InvalidOperationException]::new(
                        "Updated runtime failed and rollback/restart was incomplete. Recovery files: $rollbackDir. Runtime: $($restartFailure.Message) Rollback: $($_.Exception.Message)"
                    )
                }
            }
        }
    }

    $preserveRecovery = $replacementAttempted -and -not $updateCommitted -and -not $rollbackComplete
    $preserveDeferredCommit = $deferredTransactionPrepared
    if ($preserveRecovery) {
        Write-Warning "Update rollback was incomplete; recovery files were preserved at $rollbackDir"
    } elseif ($preserveDeferredCommit) {
        Write-Host "Deferred update recovery retained until runtime health is confirmed: $deferredTransactionPath"
    } else {
        try {
            if (Test-Path -LiteralPath $tempDir) {
                Remove-Item -LiteralPath $tempDir -Recurse -Force
            }
        } catch {
            Write-Warning "Could not remove temporary update folder: $tempDir"
        }
    }
    if ($null -ne $restartFailure) {
        throw $restartFailure
    }
}
} finally {
    if ($null -ne $updateLockStream) {
        $updateLockStream.Dispose()
    }
}
