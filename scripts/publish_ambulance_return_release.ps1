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

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = (Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
}
if ($Version -notmatch "^\d{4}\.\d{2}\.\d{2}\.\d{4}$") {
    throw "Version must use yyyy.MM.dd.HHmm format. Got: $Version"
}
foreach ($path in @($versionPath, $zipPath, $shaPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing release asset: $path"
    }
}

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    throw "GitHub CLI gh is not installed. Install gh and run 'gh auth login' first."
}

$tag = "ambulance-return-$Version"
gh release create $tag $versionPath $zipPath $shaPath --repo $Repository --title $tag --notes "救護返隊公務電腦使用包 $Version"
