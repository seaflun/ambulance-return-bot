param(
    [Parameter(Mandatory = $true)]
    [string]$RequestId,
    [ValidateSet("unknown", "gui", "headless")]
    [string]$CallerRuntime = "unknown",
    [string]$RecoverTransactionPath = ""
)

$ErrorActionPreference = "Stop"

$scriptPath = [System.IO.Path]::GetFullPath($MyInvocation.MyCommand.Path)
$packageDir = Split-Path -Parent $scriptPath
$updaterPath = Join-Path $packageDir "update_package.ps1"
$versionPath = Join-Path $packageDir "VERSION.txt"
$stateBase = if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { $env:TEMP } else { $env:LOCALAPPDATA }
$resultDir = Join-Path $stateBase "AmbulanceReturnBot"
$uniqueResultDir = Join-Path $resultDir "remote_update_results"
$transactionDir = Join-Path $resultDir "update_transactions"
$runId = [guid]::NewGuid().ToString("N")
$safeRequestId = [regex]::Replace($RequestId, "[^A-Za-z0-9._-]", "_")
if ([string]::IsNullOrWhiteSpace($safeRequestId)) {
    $safeRequestId = "request"
}
if ($safeRequestId.Length -gt 80) {
    $safeRequestId = $safeRequestId.Substring(0, 80)
}
$resultPath = Join-Path $uniqueResultDir "$safeRequestId-$runId.json"
$tempResultPath = Join-Path $uniqueResultDir ".$safeRequestId-$runId.tmp"
$compatibilityResultPath = Join-Path $resultDir "remote_update_result.json"
$compatibilityTempPath = Join-Path $resultDir ".remote_update_result.$runId.tmp"
$activeMarkerPath = Join-Path $resultDir "remote_update_active.json"
$activeMarkerTempPath = Join-Path $resultDir ".remote_update_active.$runId.tmp"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$remoteUpdatePhases = @("discovering_runtime", "installing", "validating", "committing", "rolling_back", "restarting")

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

function Resolve-TransactionPath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Update transaction path is missing."
    }
    $rootFull = [System.IO.Path]::GetFullPath($transactionDir).TrimEnd([char]92)
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $prefix = (Get-PackageIdentity) + "-"
    if (-not $pathFull.StartsWith($rootFull + [string][char]92, [System.StringComparison]::OrdinalIgnoreCase) -or
        [System.IO.Path]::GetExtension($pathFull) -ne ".json" -or
        -not (Split-Path -Leaf $pathFull).StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe update transaction path: $Path"
    }
    return $pathFull
}

function New-TransactionPath {
    New-Item -ItemType Directory -Path $transactionDir -Force | Out-Null
    return Join-Path $transactionDir ("{0}-{1}-{2}.json" -f (Get-PackageIdentity), $safeRequestId, $runId)
}

function Get-PackageVersion {
    if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
        return "0"
    }
    return (Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
}

function Enter-UpdateLock {
    $lockPath = Join-Path $resultDir "package-update.lock"
    New-Item -ItemType Directory -Path $resultDir -Force | Out-Null
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
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Cannot restart worker GUI because launcher is missing: $launcher"
    }
    Start-Process -FilePath "wscript.exe" -ArgumentList ('"' + $launcher + '"') -WorkingDirectory $packageDir -WindowStyle Hidden | Out-Null
}

function Start-WorkerHeadless {
    if ((Get-WorkerRuntimeState).Headless) {
        return
    }
    $launcher = Join-Path $packageDir "run_worker_headless.bat"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Cannot restart headless worker because launcher is missing: $launcher"
    }
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", ('"' + $launcher + '"') -WorkingDirectory $packageDir -WindowStyle Hidden | Out-Null
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
            [string]$payload.version -eq (Get-PackageVersion) -and
            [System.IO.Path]::GetFullPath([string]$payload.transaction_path).Equals(
                [System.IO.Path]::GetFullPath($TransactionPath),
                [System.StringComparison]::OrdinalIgnoreCase
            )
        )
    } catch {
        return $false
    }
}

function Write-UpdateOwnerHeartbeat {
    param([string]$TransactionPath)

    $safeTransactionPath = Resolve-TransactionPath -Path $TransactionPath
    $heartbeatPath = $safeTransactionPath + ".owner.heartbeat"
    $heartbeatTemp = $heartbeatPath + "." + [guid]::NewGuid().ToString("N") + ".tmp"
    $payload = [ordered]@{
        owner_pid = $PID
        owner_nonce = $runId
        updated_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    try {
        [System.IO.File]::WriteAllText($heartbeatTemp, ($payload | ConvertTo-Json -Compress), $utf8NoBom)
        Move-Item -LiteralPath $heartbeatTemp -Destination $heartbeatPath -Force
    } finally {
        Remove-Item -LiteralPath $heartbeatTemp -Force -ErrorAction SilentlyContinue
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
        return Get-WorkerRuntimeState
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $readySince = $null
    $readyProcessKey = ""
    do {
        if (-not [string]::IsNullOrWhiteSpace($ProbeTransactionPath)) {
            Write-UpdateOwnerHeartbeat -TransactionPath $ProbeTransactionPath
            Set-RemoteUpdatePhase -Phase "validating" -TransactionPath $ProbeTransactionPath
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

function Restart-WorkerRuntimesFresh {
    param(
        [bool]$StartGui,
        [bool]$StartHeadless,
        [int[]]$ExcludedProcessIds = @()
    )

    $currentState = Get-WorkerRuntimeState
    $stoppedIds = @()
    if (@($currentState.Processes).Count -gt 0) {
        $stoppedIds = @(Stop-WorkerPackageProcesses -Processes @($currentState.Processes))
    }
    $allExcluded = @(($ExcludedProcessIds + $stoppedIds) | Sort-Object -Unique)
    return Restart-WorkerRuntimes `
        -StartGui $StartGui `
        -StartHeadless $StartHeadless `
        -ExcludedProcessIds $allExcluded
}

function Remove-ExpiredRemoteUpdateResults {
    if (-not (Test-Path -LiteralPath $uniqueResultDir -PathType Container)) {
        return
    }
    $cutoff = [DateTime]::UtcNow.AddDays(-7)
    $currentPaths = @(
        [System.IO.Path]::GetFullPath($resultPath),
        [System.IO.Path]::GetFullPath($tempResultPath)
    )
    Get-ChildItem -LiteralPath $uniqueResultDir -File -Force | Where-Object {
        $_.LastWriteTimeUtc -lt $cutoff -and
        $_.Extension -in @(".json", ".tmp") -and
        $currentPaths -notcontains [System.IO.Path]::GetFullPath($_.FullName)
    } | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
    }
}

function Write-RemoteUpdateResult {
    New-Item -ItemType Directory -Path $resultDir -Force | Out-Null
    New-Item -ItemType Directory -Path $uniqueResultDir -Force | Out-Null
    Remove-ExpiredRemoteUpdateResults
    $payload = [ordered]@{
        request_id = $RequestId
        status = $status
        detail = $detail
        before_version = $beforeVersion
        installed_version = $installedVersion
        exit_code = $exitCode
        completed_at = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
    }
    if (-not [string]::IsNullOrWhiteSpace($watchdogInstallWarning)) {
        $payload["watchdog_install_warning"] = $watchdogInstallWarning
    }
    $json = $payload | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($tempResultPath, $json, $utf8NoBom)
    Move-Item -LiteralPath $tempResultPath -Destination $resultPath -Force
    [System.IO.File]::WriteAllText($compatibilityTempPath, $json, $utf8NoBom)
    Move-Item -LiteralPath $compatibilityTempPath -Destination $compatibilityResultPath -Force
}

function Protect-WorkerFromStaleSuccessResult {
    if (-not (Test-Path -LiteralPath $compatibilityResultPath -PathType Leaf)) {
        return $true
    }
    try {
        $payload = Get-Content -LiteralPath $compatibilityResultPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $belongsToRequest = [string]$payload.request_id -eq $RequestId
        $isStaleSuccess = [string]$payload.status -in @("completed", "up_to_date")
        if (-not $belongsToRequest -or -not $isStaleSuccess) {
            return $true
        }
        $quarantinePath = Join-Path $uniqueResultDir ("stale-success-{0}-{1}.json" -f $safeRequestId, [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $uniqueResultDir -Force | Out-Null
        Move-Item -LiteralPath $compatibilityResultPath -Destination $quarantinePath -Force
        return -not (Test-Path -LiteralPath $compatibilityResultPath)
    } catch {
        Write-Warning "Could not quarantine stale remote update success result: $($_.Exception.Message)"
        return $false
    }
}

function Get-RemoteUpdateOwnerStartedUnixMs {
    $ownerProcess = [System.Diagnostics.Process]::GetCurrentProcess()
    $unixEpoch = [DateTime]::SpecifyKind([DateTime]"1970-01-01", [DateTimeKind]::Utc)
    return [long](($ownerProcess.StartTime.ToUniversalTime() - $unixEpoch).TotalMilliseconds)
}

function Test-RemoteUpdateMarkerIdentity {
    param([object]$Payload)

    try {
        $markerScriptValue = $Payload.script_path
        $markerPackageValue = $Payload.package_path
        if ($markerScriptValue -isnot [string] -or
            $markerPackageValue -isnot [string] -or
            [string]::IsNullOrWhiteSpace($markerScriptValue) -or
            [string]::IsNullOrWhiteSpace($markerPackageValue) -or
            -not [System.IO.Path]::IsPathRooted($markerScriptValue) -or
            -not [System.IO.Path]::IsPathRooted($markerPackageValue)) {
            return $false
        }
        $markerScriptPath = [System.IO.Path]::GetFullPath($markerScriptValue).TrimEnd([char]92)
        $markerPackagePath = [System.IO.Path]::GetFullPath($markerPackageValue).TrimEnd([char]92)
        $expectedScriptPath = $scriptPath.TrimEnd([char]92)
        $expectedPackagePath = [System.IO.Path]::GetFullPath($packageDir).TrimEnd([char]92)
        return (
            [string]$Payload.request_id -eq $RequestId -and
            [int]$Payload.owner_pid -eq $PID -and
            [long]$Payload.owner_started_unix_ms -eq (Get-RemoteUpdateOwnerStartedUnixMs) -and
            [string]$Payload.owner_nonce -eq $runId -and
            $markerScriptPath.Equals($expectedScriptPath, [System.StringComparison]::OrdinalIgnoreCase) -and
            $markerPackagePath.Equals($expectedPackagePath, [System.StringComparison]::OrdinalIgnoreCase)
        )
    } catch {
        return $false
    }
}

function Write-RemoteUpdateMarkerPayload {
    param([System.Collections.IDictionary]$Payload)

    New-Item -ItemType Directory -Path $resultDir -Force | Out-Null
    $markerTempPath = $activeMarkerPath + "." + [guid]::NewGuid().ToString("N") + ".tmp"
    try {
        [System.IO.File]::WriteAllText($markerTempPath, ($Payload | ConvertTo-Json -Compress), $utf8NoBom)
        Move-Item -LiteralPath $markerTempPath -Destination $activeMarkerPath -Force
    } finally {
        Remove-Item -LiteralPath $markerTempPath -Force -ErrorAction SilentlyContinue
    }
}

function Write-RemoteUpdateActiveMarker {
    $now = [DateTime]::UtcNow.ToString("o")
    $payload = [ordered]@{
        request_id = $RequestId
        owner_pid = $PID
        owner_nonce = $runId
        owner_started_unix_ms = Get-RemoteUpdateOwnerStartedUnixMs
        script_path = $scriptPath
        package_path = [System.IO.Path]::GetFullPath($packageDir)
        transaction_path = ""
        phase = "discovering_runtime"
        phase_started_at = $now
        phase_updated_at = $now
        started_at_utc = $now
    }
    Write-RemoteUpdateMarkerPayload -Payload $payload
}

function Set-RemoteUpdatePhase {
    param(
        [ValidateSet("discovering_runtime", "installing", "validating", "committing", "rolling_back", "restarting")]
        [string]$Phase,
        [string]$TransactionPath = ""
    )

    if (-not ($remoteUpdatePhases -contains $Phase)) {
        throw "Unsupported remote update phase: $Phase"
    }
    if (-not (Test-Path -LiteralPath $activeMarkerPath -PathType Leaf)) {
        throw "Remote update active marker is missing."
    }
    try {
        $existing = Get-Content -LiteralPath $activeMarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Remote update active marker is unreadable: $($_.Exception.Message)"
    }
    if (-not (Test-RemoteUpdateMarkerIdentity -Payload $existing)) {
        throw "Remote update active marker does not belong to this update run."
    }
    $safeTransactionPath = ""
    if (-not [string]::IsNullOrWhiteSpace($TransactionPath)) {
        $safeTransactionPath = Resolve-TransactionPath -Path $TransactionPath
    }
    $now = [DateTime]::UtcNow.ToString("o")
    $phaseStartedAt = if ([string]$existing.phase -eq $Phase -and -not [string]::IsNullOrWhiteSpace([string]$existing.phase_started_at)) {
        [string]$existing.phase_started_at
    } else {
        $now
    }
    $payload = [ordered]@{
        request_id = $RequestId
        owner_pid = $PID
        owner_nonce = $runId
        owner_started_unix_ms = Get-RemoteUpdateOwnerStartedUnixMs
        script_path = $scriptPath
        package_path = [System.IO.Path]::GetFullPath($packageDir)
        transaction_path = $safeTransactionPath
        phase = $Phase
        phase_started_at = $phaseStartedAt
        phase_updated_at = $now
        started_at_utc = [string]$existing.started_at_utc
    }
    Write-RemoteUpdateMarkerPayload -Payload $payload
}

function Remove-RemoteUpdateActiveMarker {
    Remove-Item -LiteralPath $activeMarkerTempPath -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $activeMarkerPath -PathType Leaf)) {
        return $true
    }
    try {
        $payload = Get-Content -LiteralPath $activeMarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (Test-RemoteUpdateMarkerIdentity -Payload $payload) {
            Remove-Item -LiteralPath $activeMarkerPath -Force
        }
        return -not (Test-Path -LiteralPath $activeMarkerPath)
    } catch {
        Write-Warning "Could not remove remote update active marker: $($_.Exception.Message)"
        return $false
    }
}

function Invoke-UpdateTransactionAction {
    param(
        [ValidateSet("rollback", "finalize")]
        [string]$Action,
        [string]$TransactionPath
    )

    $env:AMBULANCE_UPDATE_TRANSACTION_PATH = Resolve-TransactionPath -Path $TransactionPath
    $env:AMBULANCE_UPDATE_TRANSACTION_ACTION = $Action
    try {
        & $updaterPath
    } finally {
        Remove-Item Env:AMBULANCE_UPDATE_TRANSACTION_ACTION -ErrorAction SilentlyContinue
    }
}

function Get-TransactionRuntimeIntent {
    param([string]$TransactionPath)

    $safePath = Resolve-TransactionPath -Path $TransactionPath
    try {
        $payload = Get-Content -LiteralPath $safePath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Pending update transaction is unreadable: $($_.Exception.Message)"
    }
    $expectedPackage = [System.IO.Path]::GetFullPath($packageDir).TrimEnd([char]92)
    $payloadPackage = [System.IO.Path]::GetFullPath([string]$payload.package_dir).TrimEnd([char]92)
    if ([int]$payload.schema_version -ne 2 -or
        [string]$payload.phase -ne "prepared" -or
        [string]$payload.package_id -ne (Get-PackageIdentity) -or
        -not $payloadPackage.Equals($expectedPackage, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Pending update transaction does not belong to this package."
    }
    return [PSCustomObject]@{
        Gui = [bool]$payload.worker_gui_was_running
        Headless = [bool]$payload.worker_headless_was_running
    }
}

function Clear-UpdateEnvironment {
    Remove-Item Env:AMBULANCE_SKIP_WORKER_RESTART -ErrorAction SilentlyContinue
    Remove-Item Env:AMBULANCE_UPDATE_LOCK_HELD -ErrorAction SilentlyContinue
    Remove-Item Env:AMBULANCE_UPDATE_TRANSACTION_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:AMBULANCE_UPDATE_TRANSACTION_ACTION -ErrorAction SilentlyContinue
    Remove-Item Env:AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:AMBULANCE_UPDATE_REQUEST_ID -ErrorAction SilentlyContinue
    Remove-Item Env:AMBULANCE_RESTART_GUI_INTENT -ErrorAction SilentlyContinue
    Remove-Item Env:AMBULANCE_RESTART_HEADLESS_INTENT -ErrorAction SilentlyContinue
    Remove-Item Env:AMBULANCE_UPDATE_OWNER_PID -ErrorAction SilentlyContinue
    Remove-Item Env:AMBULANCE_UPDATE_OWNER_NONCE -ErrorAction SilentlyContinue
    Remove-Item Env:AMBULANCE_WATCHDOG_INSTALL_WARNING -ErrorAction SilentlyContinue
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

    Clear-UpdateEnvironment
    foreach ($entry in $Snapshot.GetEnumerator()) {
        [System.Environment]::SetEnvironmentVariable(
            [string]$entry.Key,
            [string]$entry.Value,
            [System.EnvironmentVariableTarget]::Process
        )
    }
}

$beforeVersion = Get-PackageVersion
$installedVersion = $beforeVersion
$status = "failed"
$detail = "Remote update did not complete."
$exitCode = 1
$workerGuiWasRunning = $CallerRuntime -eq "gui"
$workerHeadlessWasRunning = $CallerRuntime -eq "headless"
$initialProcessIds = @()
$transactionPath = ""
$transactionFinalized = $false
$runtimePackageSafe = $false
$updateLockStream = $null
$ownsUpdateLock = $false
$activeMarkerWritten = $false
$watchdogInstallWarning = ""

try {
    $updateLockStream = Enter-UpdateLock
    $ownsUpdateLock = $true
    Write-RemoteUpdateActiveMarker
    $activeMarkerWritten = $true
    $runtimeState = Get-WorkerRuntimeState
    $workerGuiWasRunning = $workerGuiWasRunning -or [bool]$runtimeState.Gui
    $workerHeadlessWasRunning = $workerHeadlessWasRunning -or [bool]$runtimeState.Headless
    $initialProcessIds = @($runtimeState.Processes | ForEach-Object { [int]$_.ProcessId } | Sort-Object -Unique)
    if (-not (Test-Path -LiteralPath $updaterPath -PathType Leaf)) {
        throw "Updater not found: $updaterPath"
    }
    if ([string]::IsNullOrWhiteSpace($RecoverTransactionPath) -and
        -not $workerGuiWasRunning -and
        -not $workerHeadlessWasRunning) {
        throw "Remote update cannot validate because the caller runtime mode is unknown."
    }

    $env:AMBULANCE_SKIP_WORKER_RESTART = "true"
    $env:AMBULANCE_UPDATE_LOCK_HELD = "true"
    $env:AMBULANCE_UPDATE_REQUEST_ID = $RequestId
    $env:AMBULANCE_UPDATE_OWNER_PID = [string]$PID
    $env:AMBULANCE_UPDATE_OWNER_NONCE = $runId
    Remove-Item Env:AMBULANCE_WATCHDOG_INSTALL_WARNING -ErrorAction SilentlyContinue

    if (-not [string]::IsNullOrWhiteSpace($RecoverTransactionPath)) {
        $transactionPath = Resolve-TransactionPath -Path $RecoverTransactionPath
        $intent = Get-TransactionRuntimeIntent -TransactionPath $transactionPath
        $workerGuiWasRunning = $workerGuiWasRunning -or $intent.Gui
        $workerHeadlessWasRunning = $workerHeadlessWasRunning -or $intent.Headless
        Set-RemoteUpdatePhase -Phase "rolling_back" -TransactionPath $transactionPath
        Invoke-UpdateTransactionAction -Action "rollback" -TransactionPath $transactionPath
        $runtimePackageSafe = $true
        $installedVersion = Get-PackageVersion
        $status = "failed"
        $detail = "Interrupted remote update was rolled back safely: $beforeVersion -> $installedVersion."
        $exitCode = 1
        Write-RemoteUpdateResult
        Set-RemoteUpdatePhase -Phase "restarting" -TransactionPath $transactionPath
        if (-not (Remove-RemoteUpdateActiveMarker)) {
            throw "Could not retire the remote update active marker before restarting the worker."
        }
        $activeMarkerWritten = $false
        Clear-UpdateEnvironment
        [void](Restart-WorkerRuntimesFresh `
            -StartGui $workerGuiWasRunning `
            -StartHeadless $workerHeadlessWasRunning `
            -ExcludedProcessIds $initialProcessIds)
    } else {
        $transactionPath = New-TransactionPath
        Set-RemoteUpdatePhase -Phase "installing" -TransactionPath $transactionPath
        $env:AMBULANCE_UPDATE_TRANSACTION_PATH = $transactionPath
        $env:AMBULANCE_RESTART_GUI_INTENT = if ($workerGuiWasRunning) { "true" } else { "false" }
        $env:AMBULANCE_RESTART_HEADLESS_INTENT = if ($workerHeadlessWasRunning) { "true" } else { "false" }

        & $updaterPath
        $watchdogInstallWarning = [string]$env:AMBULANCE_WATCHDOG_INSTALL_WARNING
        $installedVersion = Get-PackageVersion
        if ($installedVersion -eq $beforeVersion) {
            $runtimePackageSafe = $true
            $status = "up_to_date"
            $detail = "Public PC is already up to date: $installedVersion."
            $exitCode = 0
            Set-RemoteUpdatePhase -Phase "committing" -TransactionPath $transactionPath
            Write-RemoteUpdateResult
            Set-RemoteUpdatePhase -Phase "restarting" -TransactionPath $transactionPath
            if (-not (Remove-RemoteUpdateActiveMarker)) {
                throw "Could not retire the remote update active marker before restarting the worker."
            }
            $activeMarkerWritten = $false
            Clear-UpdateEnvironment
            [void](Restart-WorkerRuntimesFresh `
                -StartGui $workerGuiWasRunning `
                -StartHeadless $workerHeadlessWasRunning `
                -ExcludedProcessIds $initialProcessIds)
        } else {
            if (-not (Test-Path -LiteralPath $transactionPath -PathType Leaf)) {
                throw "Updater changed VERSION.txt without retaining a recovery transaction."
            }
            $status = "validating"
            $detail = "Remote update installed; validating the new worker runtime before commit."
            $exitCode = 0
            Set-RemoteUpdatePhase -Phase "validating" -TransactionPath $transactionPath
            Write-RemoteUpdateResult

            Write-UpdateOwnerHeartbeat -TransactionPath $transactionPath
            $probeEnvironment = Suspend-UpdateControlEnvironmentForProbe -ProbeTransactionPath $transactionPath
            try {
                Set-RemoteUpdatePhase -Phase "restarting" -TransactionPath $transactionPath
                [void](Restart-WorkerRuntimes -StartGui $workerGuiWasRunning -StartHeadless $workerHeadlessWasRunning -ExcludedProcessIds $initialProcessIds -ProbeTransactionPath $transactionPath)
            } finally {
                Restore-UpdateControlEnvironment -Snapshot $probeEnvironment
            }

            $status = "completed"
            $detail = "Remote update completed: $beforeVersion -> $installedVersion."
            $exitCode = 0
            Set-RemoteUpdatePhase -Phase "committing" -TransactionPath $transactionPath
            Write-RemoteUpdateResult
            Invoke-UpdateTransactionAction -Action "finalize" -TransactionPath $transactionPath
            $transactionFinalized = $true
            $runtimePackageSafe = $true
            if (-not (Remove-RemoteUpdateActiveMarker)) {
                throw "Could not retire the remote update active marker before committing the worker."
            }
            $activeMarkerWritten = $false
        }
    }
} catch {
    $failure = $_.Exception.Message
    $rollbackSucceeded = $false
    if ($ownsUpdateLock -and
        -not $transactionFinalized -and
        -not [string]::IsNullOrWhiteSpace($transactionPath) -and
        (Test-Path -LiteralPath $transactionPath -PathType Leaf)) {
        try {
            Set-RemoteUpdatePhase -Phase "rolling_back" -TransactionPath $transactionPath
            Invoke-UpdateTransactionAction -Action "rollback" -TransactionPath $transactionPath
            $rollbackSucceeded = $true
            $runtimePackageSafe = $true
        } catch {
            $failure = "$failure Rollback failed: $($_.Exception.Message)"
        }
    }
    $installedVersion = Get-PackageVersion
    $status = "failed"
    $detail = "Remote update failed: $failure"
    $exitCode = 1
    $resultWriteFailure = $null
    try {
        Write-RemoteUpdateResult
    } catch {
        $resultWriteFailure = $_.Exception
    } finally {
        Clear-UpdateEnvironment
    }
    $runtimeRestartFailure = $null
    $resultSafeForWorker = $true
    $failureResultDurable = $null -eq $resultWriteFailure
    if ($null -ne $resultWriteFailure) {
        $resultSafeForWorker = Protect-WorkerFromStaleSuccessResult
        $detail = "$detail Result write failed: $($resultWriteFailure.Message)"
        try {
            Write-RemoteUpdateResult
            $failureResultDurable = $true
            $resultSafeForWorker = $true
        } catch {
            Write-Warning "Could not write remote update failure result: $($_.Exception.Message)"
        }
    }
    $noPendingTransaction = (
        [string]::IsNullOrWhiteSpace($transactionPath) -or
        -not (Test-Path -LiteralPath $transactionPath -PathType Leaf)
    )
    $runtimeRestartAllowed = $ownsUpdateLock -and (
        $runtimePackageSafe -or
        $transactionFinalized -or
        $rollbackSucceeded -or
        ($noPendingTransaction -and $installedVersion -eq $beforeVersion)
    )
    $activeMarkerRetired = -not $activeMarkerWritten
    if ($runtimeRestartAllowed -and $resultSafeForWorker -and $failureResultDurable -and $activeMarkerWritten) {
        Set-RemoteUpdatePhase -Phase "restarting" -TransactionPath $transactionPath
        $activeMarkerRetired = Remove-RemoteUpdateActiveMarker
        if ($activeMarkerRetired) {
            $activeMarkerWritten = $false
        }
    }
    if ($runtimeRestartAllowed -and $resultSafeForWorker -and $failureResultDurable -and $activeMarkerRetired) {
        try {
            [void](Restart-WorkerRuntimesFresh `
                -StartGui $workerGuiWasRunning `
                -StartHeadless $workerHeadlessWasRunning `
                -ExcludedProcessIds $initialProcessIds)
        } catch {
            $runtimeRestartFailure = $_.Exception
        }
    }
    if ($null -ne $runtimeRestartFailure) {
        $detail = "$detail Runtime restart failed: $($runtimeRestartFailure.Message)"
        try {
            Write-RemoteUpdateResult
        } catch {
            Write-Warning "Could not write remote update failure result: $($_.Exception.Message)"
        }
    }
} finally {
    Clear-UpdateEnvironment
    if ($activeMarkerWritten) {
        [void](Remove-RemoteUpdateActiveMarker)
    }
    if ($null -ne $updateLockStream) {
        $updateLockStream.Dispose()
    }
}

exit $exitCode
