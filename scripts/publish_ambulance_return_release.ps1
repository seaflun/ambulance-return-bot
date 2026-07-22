param(
    [string]$Repository = "seaflun/ambulance-return-bot",
    [string]$Version
)

$ErrorActionPreference = "Stop"

$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$updateDir = Join-Path $project "UPDATE"
$versionPath = Join-Path $updateDir "ambulance-return-version.txt"
$zipPath = Join-Path $updateDir "ambulance-return-public-package.zip"
$shaPath = Join-Path $updateDir "ambulance-return-public-package.zip.sha256.txt"
$updaterPath = Join-Path $updateDir "update_package.ps1"

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = (Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
}
if ($Version -notmatch "^\d{4}\.\d{2}\.\d{2}\.\d{4}$") {
    throw "Version must use yyyy.MM.dd.HHmm format. Got: $Version"
}
foreach ($path in @($versionPath, $zipPath, $shaPath, $updaterPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing release asset: $path"
    }
}

$stageDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ambulance-return-release-" + $Version)
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
$stagedVersionPath = Join-Path $stageDir "ambulance-return-version.txt"
$stagedZipPath = Join-Path $stageDir "ambulance-return-public-package.zip"
$stagedShaPath = Join-Path $stageDir "ambulance-return-public-package.zip.sha256.txt"
$stagedUpdaterPath = Join-Path $stageDir "update_package.ps1"
Copy-Item -LiteralPath $versionPath -Destination $stagedVersionPath -Force
Copy-Item -LiteralPath $zipPath -Destination $stagedZipPath -Force
Copy-Item -LiteralPath $shaPath -Destination $stagedShaPath -Force
Copy-Item -LiteralPath $updaterPath -Destination $stagedUpdaterPath -Force

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    throw "GitHub CLI gh is not installed. Install gh and run 'gh auth login' first."
}

$tag = "ambulance-return-$Version"
gh release create $tag $stagedVersionPath $stagedZipPath $stagedShaPath $stagedUpdaterPath --repo $Repository --title $tag --notes "SinpoSmart - 救災救護Worker 公務電腦使用包 $Version"
