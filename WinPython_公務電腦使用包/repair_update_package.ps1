$ErrorActionPreference = "Stop"

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $packageDir "update_package.ps1"
$releaseBaseUrl = if ($env:AMBULANCE_RETURN_RELEASE_BASE_URL) { $env:AMBULANCE_RETURN_RELEASE_BASE_URL.TrimEnd("/") } else { "" }
$latestReleaseApiUrl = "https://api.github.com/repos/seaflun/ambulance-return-bot/releases/latest"
$downloadCacheKey = Get-Date -Format "yyyyMMddHHmmss"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tempDir = Join-Path $env:TEMP "AmbulanceReturnBotUpdaterRepair-$stamp"

function Add-DownloadCacheBust {
    param([string]$Url)
    $separator = if ($Url.Contains("?")) { "&" } else { "?" }
    return "$Url${separator}cachebust=$downloadCacheKey"
}

function Test-PowerShellFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Write-Warning "PowerShell file not found: $Path"
        return $false
    }
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count -eq 0) {
        return $true
    }
    foreach ($errorItem in $errors) {
        Write-Warning "Parse error in ${Path}: $($errorItem.Message)"
    }
    return $false
}

function Get-LatestRelease {
    Invoke-RestMethod -Uri $latestReleaseApiUrl -UseBasicParsing -Headers @{
        "Accept" = "application/vnd.github+json"
        "User-Agent" = "AmbulanceReturnBotUpdaterRepair"
    }
}

function Get-ReleaseAssetUrl {
    param(
        [object]$Release,
        [string]$Name
    )
    $asset = $Release.assets | Where-Object { $_.name -eq $Name } | Select-Object -First 1
    if (-not $asset -or -not $asset.browser_download_url) {
        return ""
    }
    return [string]$asset.browser_download_url
}

function Invoke-DownloadFile {
    param(
        [string]$Url,
        [string]$Destination
    )
    Invoke-WebRequest -Uri (Add-DownloadCacheBust $Url) -OutFile $Destination -UseBasicParsing -MaximumRedirection 5
}

function Install-CandidateUpdater {
    param([string]$CandidatePath)
    if (-not (Test-PowerShellFile -Path $CandidatePath)) {
        return $false
    }
    Copy-Item -LiteralPath $CandidatePath -Destination $target -Force
    if (-not (Test-PowerShellFile -Path $target)) {
        throw "Repaired updater still has parse errors: $target"
    }
    Write-Host "Repaired updater: $target"
    return $true
}

try {
    if (Test-PowerShellFile -Path $target) {
        Write-Host "Updater script parse OK: $target"
        exit 0
    }

    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    $standalonePath = Join-Path $tempDir "update_package.ps1"
    $release = $null

    try {
        if (-not [string]::IsNullOrWhiteSpace($releaseBaseUrl)) {
            $standaloneUrl = "$releaseBaseUrl/update_package.ps1"
        } else {
            $release = Get-LatestRelease
            Write-Host "Latest release: $($release.tag_name)"
            $standaloneUrl = Get-ReleaseAssetUrl -Release $release -Name "update_package.ps1"
        }
        if (-not [string]::IsNullOrWhiteSpace($standaloneUrl)) {
            Write-Host "Downloading standalone updater..."
            Invoke-DownloadFile -Url $standaloneUrl -Destination $standalonePath
            if (Install-CandidateUpdater -CandidatePath $standalonePath) {
                exit 0
            }
        }
    } catch {
        Write-Warning "Standalone updater repair failed: $($_.Exception.Message)"
    }

    $zipPath = Join-Path $tempDir "package.zip"
    $extractDir = Join-Path $tempDir "extract"
    if (-not [string]::IsNullOrWhiteSpace($releaseBaseUrl)) {
        $zipUrl = "$releaseBaseUrl/ambulance-return-public-package.zip"
    } else {
        if ($null -eq $release) {
            $release = Get-LatestRelease
            Write-Host "Latest release: $($release.tag_name)"
        }
        $zipUrl = Get-ReleaseAssetUrl -Release $release -Name "ambulance-return-public-package.zip"
    }
    if ([string]::IsNullOrWhiteSpace($zipUrl)) {
        throw "Could not locate update package zip in the latest release."
    }
    Write-Host "Downloading update package to repair updater..."
    Invoke-DownloadFile -Url $zipUrl -Destination $zipPath
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
    $candidate = Get-ChildItem -LiteralPath $extractDir -Recurse -Filter "update_package.ps1" -File |
        Where-Object { $_.FullName -match "WinPython_" } |
        Select-Object -First 1
    if (-not $candidate) {
        throw "Update package zip did not contain update_package.ps1."
    }
    if (-not (Install-CandidateUpdater -CandidatePath $candidate.FullName)) {
        throw "Downloaded updater has parse errors."
    }
} finally {
    try {
        if (Test-Path -LiteralPath $tempDir) {
            Remove-Item -LiteralPath $tempDir -Recurse -Force
        }
    } catch {
        Write-Warning "Could not remove repair temp folder: $tempDir"
    }
}
