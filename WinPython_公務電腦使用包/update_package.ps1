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
    if ($env:AMBULANCE_SKIP_WORKER_RESTART -notmatch "^(?i:1|true|yes|on)$") {
        Start-WorkerGui
    }
} finally {
    try {
        if (Test-Path -LiteralPath $tempDir) {
            Remove-Item -LiteralPath $tempDir -Recurse -Force
        }
    } catch {
        Write-Warning "Could not remove temporary update folder: $tempDir"
    }
}
