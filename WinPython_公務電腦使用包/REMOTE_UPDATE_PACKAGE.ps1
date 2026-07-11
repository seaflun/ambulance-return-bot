param(
    [Parameter(Mandatory = $true)]
    [string]$RequestId
)

$ErrorActionPreference = "Stop"

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$updaterPath = Join-Path $packageDir "update_package.ps1"
$versionPath = Join-Path $packageDir "VERSION.txt"
$resultDir = Join-Path $env:LOCALAPPDATA "AmbulanceReturnBot"
$resultPath = Join-Path $resultDir "remote_update_result.json"
$tempResultPath = "$resultPath.tmp"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Get-PackageVersion {
    if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
        return "0"
    }
    return (Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
}

$beforeVersion = Get-PackageVersion
$installedVersion = $beforeVersion
$status = "failed"
$detail = "Remote update did not complete."
$exitCode = 1

try {
    if (-not (Test-Path -LiteralPath $updaterPath -PathType Leaf)) {
        throw "Updater not found: $updaterPath"
    }
    $process = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        ('"' + $updaterPath + '"')
    ) -WorkingDirectory $packageDir -WindowStyle Hidden -Wait -PassThru
    $exitCode = [int]$process.ExitCode
    $installedVersion = Get-PackageVersion
    if ($exitCode -ne 0) {
        $status = "failed"
        $detail = "Remote update failed with updater exit code $exitCode."
    } elseif ($installedVersion -eq $beforeVersion) {
        $status = "up_to_date"
        $detail = "Public PC is already up to date: $installedVersion."
    } else {
        $status = "completed"
        $detail = "Remote update completed: $beforeVersion -> $installedVersion."
    }
} catch {
    $status = "failed"
    $detail = "Remote update failed: $($_.Exception.Message)"
    $exitCode = 1
    $installedVersion = Get-PackageVersion
} finally {
    New-Item -ItemType Directory -Path $resultDir -Force | Out-Null
    $payload = [ordered]@{
        request_id = $RequestId
        status = $status
        detail = $detail
        before_version = $beforeVersion
        installed_version = $installedVersion
        exit_code = $exitCode
        completed_at = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
    }
    $json = $payload | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($tempResultPath, $json, $utf8NoBom)
    Move-Item -LiteralPath $tempResultPath -Destination $resultPath -Force
}

exit $exitCode
