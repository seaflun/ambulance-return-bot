[CmdletBinding()]
param(
    [switch]$WhatIf,
    [string]$ProcessSnapshotPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$script:watchdogScriptPath = [System.IO.Path]::GetFullPath($MyInvocation.MyCommand.Path)
$script:packageDir = Split-Path -Parent $script:watchdogScriptPath
$script:remoteUpdateScriptPath = Join-Path $script:packageDir "REMOTE_UPDATE_PACKAGE.ps1"
$script:guiLauncherPath = Join-Path $script:packageDir "RUN_WORKER_GUI_WINPYTHON.vbs"
$stateBase = if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { $env:TEMP } else { $env:LOCALAPPDATA }
$script:stateDir = Join-Path $stateBase "AmbulanceReturnBot"
$script:heartbeatPath = Join-Path $script:stateDir "worker_heartbeat.json"
$script:activityPath = Join-Path $script:stateDir "worker_activity.json"
$script:activeMarkerPath = Join-Path $script:stateDir "remote_update_active.json"
$script:recoveryStatePath = Join-Path $script:stateDir "self_recovery.json"
$script:updateLockPath = Join-Path $script:stateDir "package-update.lock"
$script:transactionDir = Join-Path $script:stateDir "update_transactions"
$script:heartbeatMaxAgeSeconds = 120
$script:activityMaxAgeSeconds = 120
$script:updateMarkerMaxAgeSeconds = 600
$script:recoveryWindowSeconds = 600
$script:maxDestructiveRecoveries = 3
$script:staleRecoveryWaitSeconds = 10
$script:staleRecoveryPollMilliseconds = 200
$script:remoteUpdatePhases = @(
    "discovering_runtime",
    "installing",
    "validating",
    "committing",
    "rolling_back",
    "restarting"
)

function Test-HasProperty {
    param(
        [object]$Object,
        [string]$Name
    )

    if ($null -eq $Object) {
        return $false
    }
    if ($Object -is [System.Collections.IDictionary]) {
        return $Object.Contains($Name)
    }
    return $null -ne $Object.PSObject.Properties[$Name]
}

function Get-PropertyValue {
    param(
        [object]$Object,
        [string]$Name
    )

    if (-not (Test-HasProperty -Object $Object -Name $Name)) {
        return $null
    }
    if ($Object -is [System.Collections.IDictionary]) {
        return $Object[$Name]
    }
    return $Object.PSObject.Properties[$Name].Value
}

function Get-NormalizedFullPath {
    param([object]$Path)

    $text = [string]$Path
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    try {
        return [System.IO.Path]::GetFullPath($text).TrimEnd([char[]]@([char]92, [char]47))
    } catch {
        return $null
    }
}

function Test-PathEquals {
    param(
        [object]$Left,
        [object]$Right
    )

    $leftPath = Get-NormalizedFullPath -Path $Left
    $rightPath = Get-NormalizedFullPath -Path $Right
    if ($null -eq $leftPath -or $null -eq $rightPath) {
        return $false
    }
    return $leftPath.Equals($rightPath, [System.StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-Int64OrNull {
    param([object]$Value)

    [long]$parsed = 0
    if ([long]::TryParse(
        [string]$Value,
        [System.Globalization.NumberStyles]::Integer,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsed
    )) {
        return $parsed
    }
    return $null
}

function ConvertTo-UtcDateTime {
    param([object]$Value)

    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    try {
        return [DateTimeOffset]::Parse(
            $text,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        ).UtcDateTime
    } catch {
        try {
            return [System.Management.ManagementDateTimeConverter]::ToDateTime($text).ToUniversalTime()
        } catch {
            return $null
        }
    }
}

function Get-AgeSeconds {
    param([object]$Timestamp)

    $date = ConvertTo-UtcDateTime -Value $Timestamp
    if ($null -eq $date) {
        return $null
    }
    $age = ([DateTime]::UtcNow - $date).TotalSeconds
    if ($age -lt -5) {
        return $null
    }
    return $age
}

function Get-PackageIdentity {
    $normalized = (Get-NormalizedFullPath -Path $script:packageDir).ToLowerInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($bytes)
    } finally {
        $sha.Dispose()
    }
    return (($hash | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 32)
}

$script:normalizedPackageDir = Get-NormalizedFullPath -Path $script:packageDir
$script:normalizedRemoteUpdateScriptPath = Get-NormalizedFullPath -Path $script:remoteUpdateScriptPath
$script:packageIdentity = Get-PackageIdentity

function New-Decision {
    param(
        [string]$Decision,
        [string]$Reason,
        [object]$MatchedOwner = $null,
        [object[]]$Actions = @()
    )

    return [PSCustomObject][ordered]@{
        decision = $Decision
        reason = $Reason
        matched_owner = $MatchedOwner
        proposed_actions = @($Actions)
    }
}

function New-OwnerSummary {
    param(
        [string]$Kind,
        [long]$ProcessId
    )

    return [PSCustomObject][ordered]@{
        kind = $Kind
        pid = $ProcessId
    }
}

function Read-JsonDocument {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [PSCustomObject]@{ Present = $false; Valid = $true; Value = $null }
    }
    try {
        $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
        if ([string]::IsNullOrWhiteSpace($raw)) {
            throw "JSON document is empty."
        }
        $value = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $value) {
            throw "JSON document is null."
        }
        return [PSCustomObject]@{ Present = $true; Valid = $true; Value = $value }
    } catch {
        return [PSCustomObject]@{ Present = $true; Valid = $false; Value = $null }
    }
}

function Get-WatchdogProcesses {
    if ($WhatIf) {
        if ([string]::IsNullOrWhiteSpace($ProcessSnapshotPath)) {
            return [PSCustomObject]@{
                Success = $false
                Reason = "snapshot_missing"
                Processes = @()
            }
        }
        $snapshot = Read-JsonDocument -Path $ProcessSnapshotPath
        if (-not $snapshot.Present -or -not $snapshot.Valid) {
            return [PSCustomObject]@{
                Success = $false
                Reason = "snapshot_invalid"
                Processes = @()
            }
        }
        $reportedError = [string](Get-PropertyValue -Object $snapshot.Value -Name "error")
        if (-not [string]::IsNullOrWhiteSpace($reportedError)) {
            return [PSCustomObject]@{
                Success = $false
                Reason = "snapshot_reported_error"
                Processes = @()
            }
        }
        if (-not (Test-HasProperty -Object $snapshot.Value -Name "processes")) {
            return [PSCustomObject]@{
                Success = $false
                Reason = "snapshot_processes_missing"
                Processes = @()
            }
        }
        $rawProcesses = Get-PropertyValue -Object $snapshot.Value -Name "processes"
        if ($rawProcesses -is [string]) {
            return [PSCustomObject]@{
                Success = $false
                Reason = "snapshot_processes_invalid"
                Processes = @()
            }
        }
        $processes = @($rawProcesses | Where-Object { $null -ne $_ })
        return [PSCustomObject]@{
            Success = $true
            Reason = ""
            Processes = $processes
        }
    }

    try {
        $processes = @(
            Get-CimInstance -ClassName Win32_Process -OperationTimeoutSec 5 -ErrorAction Stop |
                ForEach-Object {
                    [PSCustomObject]@{
                        ProcessId = [long]$_.ProcessId
                        Name = [string]$_.Name
                        CommandLine = [string]$_.CommandLine
                        CreationDate = [string]$_.CreationDate
                    }
                }
        )
        return [PSCustomObject]@{
            Success = $true
            Reason = ""
            Processes = $processes
        }
    } catch {
        return [PSCustomObject]@{
            Success = $false
            Reason = "cim_unavailable"
            Processes = @()
        }
    }
}

function Get-ProcessById {
    param(
        [object[]]$Processes,
        [long]$ProcessId
    )

    foreach ($process in @($Processes)) {
        $candidatePid = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $process -Name "ProcessId")
        if ($null -ne $candidatePid -and $candidatePid -eq $ProcessId) {
            return $process
        }
    }
    return $null
}

function Test-ProcessName {
    param(
        [object]$Process,
        [string[]]$AllowedNames
    )

    $name = [System.IO.Path]::GetFileName([string](Get-PropertyValue -Object $Process -Name "Name"))
    foreach ($allowed in $AllowedNames) {
        if ($name.Equals($allowed, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Test-ExplicitPythonScriptInvocation {
    param(
        [object]$CommandLine,
        [string]$ExpectedPath
    )

    $normalizedPath = Get-NormalizedFullPath -Path $ExpectedPath
    if ($null -eq $normalizedPath) {
        return $false
    }
    $command = ([string]$CommandLine).Replace("/", "\\")
    $pythonExecutable = '(?:"[^"]*python(?:w)?\.exe"|[^\s"]*python(?:w)?\.exe)'
    $safeFlags = '(?:(?:-B|-E|-I|-O|-OO|-q|-s|-S|-u|-v)\s+)*'
    $pattern = '^\s*' + $pythonExecutable + '\s+' + $safeFlags + '"?' + [regex]::Escape($normalizedPath) + '"?(?=\s|$)'
    return [regex]::IsMatch($command, $pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
}

function Test-ExplicitPowerShellFileInvocation {
    param(
        [object]$CommandLine,
        [string]$ExpectedPath,
        [string]$RequestId
    )

    $normalizedPath = Get-NormalizedFullPath -Path $ExpectedPath
    if ($null -eq $normalizedPath -or $RequestId -notmatch "^[A-Za-z0-9._-]{1,80}$") {
        return $false
    }
    $command = ([string]$CommandLine).Replace("/", "\\")
    $commandModePattern = '(?i)(^|\s)-(?:Command|c|EncodedCommand|ec)(?=\s|:|$)'
    if ([regex]::IsMatch($command, $commandModePattern)) {
        return $false
    }
    $filePattern = '(?i)(^|\s)-File(?:\s+|=)"?' + [regex]::Escape($normalizedPath) + '"?(?=\s|$)'
    $requestPattern = '(?i)(^|\s)-RequestId(?:\s+|=)"?' + [regex]::Escape($RequestId) + '"?(?=\s|$)'
    return [regex]::IsMatch($command, $filePattern) -and [regex]::IsMatch($command, $requestPattern)
}

function Get-ProcessCreationUtc {
    param([object]$Process)

    return ConvertTo-UtcDateTime -Value (Get-PropertyValue -Object $Process -Name "CreationDate")
}

function Get-ProcessCreationUnixMs {
    param([object]$Process)

    $createdAt = Get-ProcessCreationUtc -Process $Process
    if ($null -eq $createdAt) {
        return $null
    }
    $epoch = [DateTime]::SpecifyKind([DateTime]::new(1970, 1, 1), [DateTimeKind]::Utc)
    return [long][Math]::Floor(($createdAt - $epoch).TotalMilliseconds)
}

function Get-HeartbeatStatus {
    $document = Read-JsonDocument -Path $script:heartbeatPath
    if (-not $document.Present) {
        return [PSCustomObject]@{
            Present = $false
            Valid = $true
            Fresh = $false
            Stale = $false
            Payload = $null
        }
    }
    if (-not $document.Valid) {
        return [PSCustomObject]@{
            Present = $true
            Valid = $false
            Fresh = $false
            Stale = $false
            Payload = $null
        }
    }
    $payload = $document.Value
    $heartbeatProcessId = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $payload -Name "pid")
    $packagePath = Get-PropertyValue -Object $payload -Name "package_path"
    $age = Get-AgeSeconds -Timestamp (Get-PropertyValue -Object $payload -Name "observed_at")
    if ($null -eq $heartbeatProcessId -or $heartbeatProcessId -le 0 -or -not (Test-PathEquals -Left $packagePath -Right $script:packageDir) -or $null -eq $age) {
        return [PSCustomObject]@{
            Present = $true
            Valid = $false
            Fresh = $false
            Stale = $false
            Payload = $null
        }
    }
    return [PSCustomObject]@{
        Present = $true
        Valid = $true
        Fresh = $age -ge 0 -and $age -le $script:heartbeatMaxAgeSeconds
        Stale = $age -gt $script:heartbeatMaxAgeSeconds
        Payload = $payload
    }
}

function Get-ActivityStatus {
    $document = Read-JsonDocument -Path $script:activityPath
    if (-not $document.Present) {
        return [PSCustomObject]@{ Present = $false; Valid = $true; Fresh = $false }
    }
    if (-not $document.Valid) {
        return [PSCustomObject]@{ Present = $true; Valid = $false; Fresh = $false }
    }
    $payload = $document.Value
    $activity = [string](Get-PropertyValue -Object $payload -Name "activity")
    $owner = [string](Get-PropertyValue -Object $payload -Name "owner")
    $age = Get-AgeSeconds -Timestamp (Get-PropertyValue -Object $payload -Name "updated_at")
    if ([string]::IsNullOrWhiteSpace($activity) -or [string]::IsNullOrWhiteSpace($owner) -or $null -eq $age) {
        return [PSCustomObject]@{ Present = $true; Valid = $false; Fresh = $false }
    }
    return [PSCustomObject]@{
        Present = $true
        Valid = $true
        Fresh = $age -ge 0 -and $age -le $script:activityMaxAgeSeconds
    }
}

function Get-MarkerStatus {
    $document = Read-JsonDocument -Path $script:activeMarkerPath
    if (-not $document.Present) {
        return [PSCustomObject]@{
            Present = $false
            Valid = $true
            Mode = "none"
            Fresh = $false
            Stale = $false
            Payload = $null
        }
    }
    if (-not $document.Valid) {
        return [PSCustomObject]@{
            Present = $true
            Valid = $false
            Mode = "invalid"
            Fresh = $false
            Stale = $false
            Payload = $null
        }
    }

    $marker = $document.Value
    $requestId = [string](Get-PropertyValue -Object $marker -Name "request_id")
    $ownerPid = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $marker -Name "owner_pid")
    $ownerNonce = [string](Get-PropertyValue -Object $marker -Name "owner_nonce")
    $ownerStarted = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $marker -Name "owner_started_unix_ms")
    $scriptPath = Get-PropertyValue -Object $marker -Name "script_path"
    $packagePath = Get-PropertyValue -Object $marker -Name "package_path"
    $phase = [string](Get-PropertyValue -Object $marker -Name "phase")
    $phaseStartedAt = ConvertTo-UtcDateTime -Value (Get-PropertyValue -Object $marker -Name "phase_started_at")
    $phaseUpdatedAt = ConvertTo-UtcDateTime -Value (Get-PropertyValue -Object $marker -Name "phase_updated_at")
    $phaseStartedAge = Get-AgeSeconds -Timestamp (Get-PropertyValue -Object $marker -Name "phase_started_at")
    $age = Get-AgeSeconds -Timestamp (Get-PropertyValue -Object $marker -Name "phase_updated_at")
    $strict = (
        $requestId -match "^[A-Za-z0-9._-]{1,80}$" -and
        $null -ne $ownerPid -and $ownerPid -gt 0 -and
        $ownerNonce -match "^[A-Za-z0-9._-]{1,128}$" -and
        $null -ne $ownerStarted -and $ownerStarted -gt 0 -and
        (Test-PathEquals -Left $scriptPath -Right $script:remoteUpdateScriptPath) -and
        (Test-PathEquals -Left $packagePath -Right $script:packageDir) -and
        ($script:remoteUpdatePhases -contains $phase) -and
        $null -ne $phaseStartedAt -and
        $null -ne $phaseUpdatedAt -and
        $null -ne $phaseStartedAge -and
        $phaseStartedAge -ge 0 -and
        $phaseStartedAt -le $phaseUpdatedAt -and
        $null -ne $age -and
        $age -ge 0
    )
    if ($strict) {
        return [PSCustomObject]@{
            Present = $true
            Valid = $true
            Mode = "strict"
            Fresh = $age -ge 0 -and $age -le $script:updateMarkerMaxAgeSeconds
            Stale = $age -gt $script:updateMarkerMaxAgeSeconds
            Payload = $marker
        }
    }

    # Older markers never authorize recovery.  They can only suppress a restart
    # briefly after their owning package updater is proven by PID, creation time,
    # and the exact package wrapper command line.
    $legacyPid = $ownerPid
    if ($null -eq $legacyPid) {
        $legacyPid = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $marker -Name "pid")
    }
    $legacyStarted = ConvertTo-UtcDateTime -Value (Get-PropertyValue -Object $marker -Name "started_at_utc")
    if ($null -eq $legacyStarted) {
        $legacyStarted = ConvertTo-UtcDateTime -Value (Get-PropertyValue -Object $marker -Name "created_at_utc")
    }
    $legacyValid = (
        $requestId -match "^[A-Za-z0-9._-]{1,80}$" -and
        $null -ne $legacyPid -and $legacyPid -gt 0 -and
        $null -ne $legacyStarted -and
        $null -ne $age
    )
    if ($legacyValid) {
        return [PSCustomObject]@{
            Present = $true
            Valid = $true
            Mode = "legacy"
            Fresh = $age -ge 0 -and $age -le $script:updateMarkerMaxAgeSeconds
            Stale = $age -gt $script:updateMarkerMaxAgeSeconds
            Payload = $marker
        }
    }
    return [PSCustomObject]@{
        Present = $true
        Valid = $false
        Mode = "invalid"
        Fresh = $false
        Stale = $false
        Payload = $null
    }
}

function Get-ExactWorkerOwner {
    param(
        [object]$HeartbeatPayload,
        [object[]]$Processes
    )

    if ($null -eq $HeartbeatPayload) {
        return $null
    }
    $workerProcessId = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $HeartbeatPayload -Name "pid")
    $expectedStartedAt = ConvertTo-UtcDateTime -Value (Get-PropertyValue -Object $HeartbeatPayload -Name "process_started_at")
    if ($null -eq $workerProcessId -or $workerProcessId -le 0 -or $null -eq $expectedStartedAt) {
        return $null
    }
    $process = Get-ProcessById -Processes $Processes -ProcessId $workerProcessId
    if ($null -eq $process -or -not (Test-ProcessName -Process $process -AllowedNames @("python.exe", "pythonw.exe"))) {
        return $null
    }
    $guiPath = Join-Path $script:packageDir "worker_gui.py"
    $workerPath = Join-Path $script:packageDir "worker.py"
    $commandLine = Get-PropertyValue -Object $process -Name "CommandLine"
    if (-not (Test-ExplicitPythonScriptInvocation -CommandLine $commandLine -ExpectedPath $guiPath) -and
        -not (Test-ExplicitPythonScriptInvocation -CommandLine $commandLine -ExpectedPath $workerPath)) {
        return $null
    }
    $createdAt = Get-ProcessCreationUtc -Process $process
    $epoch = [DateTime]::SpecifyKind([DateTime]::new(1970, 1, 1), [DateTimeKind]::Utc)
    $expectedStartUnixMs = [long][Math]::Floor(($expectedStartedAt - $epoch).TotalMilliseconds)
    $actualStartUnixMs = Get-ProcessCreationUnixMs -Process $process
    if ($null -eq $createdAt -or $null -eq $actualStartUnixMs -or $actualStartUnixMs -ne $expectedStartUnixMs) {
        return $null
    }
    return [PSCustomObject]@{
        Summary = New-OwnerSummary -Kind "worker" -ProcessId $workerProcessId
        Process = $process
        CreatedAt = $createdAt
    }
}

function Get-ExactUpdaterOwner {
    param(
        [object]$MarkerStatus,
        [object[]]$Processes
    )

    if ($MarkerStatus.Mode -notin @("strict", "legacy") -or $null -eq $MarkerStatus.Payload) {
        return $null
    }
    $marker = $MarkerStatus.Payload
    $updaterProcessId = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $marker -Name "owner_pid")
    if ($null -eq $updaterProcessId) {
        $updaterProcessId = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $marker -Name "pid")
    }
    $requestId = [string](Get-PropertyValue -Object $marker -Name "request_id")
    if ($null -eq $updaterProcessId -or $updaterProcessId -le 0 -or $requestId -notmatch "^[A-Za-z0-9._-]{1,80}$") {
        return $null
    }
    $process = Get-ProcessById -Processes $Processes -ProcessId $updaterProcessId
    if ($null -eq $process -or -not (Test-ProcessName -Process $process -AllowedNames @("powershell.exe", "pwsh.exe"))) {
        return $null
    }
    $commandLine = Get-PropertyValue -Object $process -Name "CommandLine"
    if (-not (Test-ExplicitPowerShellFileInvocation -CommandLine $commandLine -ExpectedPath $script:remoteUpdateScriptPath -RequestId $requestId)) {
        return $null
    }
    $createdAt = Get-ProcessCreationUtc -Process $process
    if ($null -eq $createdAt) {
        return $null
    }
    if ($MarkerStatus.Mode -eq "strict") {
        $expectedStart = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $marker -Name "owner_started_unix_ms")
        $actualStart = Get-ProcessCreationUnixMs -Process $process
        if ($null -eq $expectedStart -or $null -eq $actualStart -or [Math]::Abs($expectedStart - $actualStart) -gt 2) {
            return $null
        }
        # Win32_Process does not expose the update nonce.  Require the marker-bound
        # transaction record to bind that nonce to this exact marker before
        # treating an updater as a package owner.
        $nonceTransaction = Get-SafeRecoveryTransaction -Marker $marker
        if (-not $nonceTransaction.Valid) {
            return $null
        }
    } else {
        $expectedStart = ConvertTo-UtcDateTime -Value (Get-PropertyValue -Object $marker -Name "started_at_utc")
        if ($null -eq $expectedStart -or [Math]::Abs(($createdAt - $expectedStart).TotalSeconds) -gt 2) {
            return $null
        }
    }
    return [PSCustomObject]@{
        Summary = New-OwnerSummary -Kind "updater" -ProcessId $updaterProcessId
        Process = $process
        CreatedAt = $createdAt
    }
}

function Get-SafeRecoveryTransaction {
    param([object]$Marker)

    $unsafe = [PSCustomObject]@{ Valid = $false; Path = $null }
    $candidatePath = Get-NormalizedFullPath -Path (Get-PropertyValue -Object $Marker -Name "transaction_path")
    $transactionRoot = Get-NormalizedFullPath -Path $script:transactionDir
    if ($null -eq $candidatePath -or $null -eq $transactionRoot) {
        return $unsafe
    }
    $prefix = $script:packageIdentity + "-"
    if (-not $candidatePath.StartsWith($transactionRoot + [string][char]92, [System.StringComparison]::OrdinalIgnoreCase) -or
        [System.IO.Path]::GetExtension($candidatePath) -ne ".json" -or
        -not ([System.IO.Path]::GetFileName($candidatePath)).StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) {
        return $unsafe
    }
    $transaction = Read-JsonDocument -Path $candidatePath
    if (-not $transaction.Present -or -not $transaction.Valid) {
        return $unsafe
    }
    $payload = $transaction.Value
    $schemaVersion = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $payload -Name "schema_version")
    $markerPid = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $Marker -Name "owner_pid")
    $transactionPid = ConvertTo-Int64OrNull -Value (Get-PropertyValue -Object $payload -Name "owner_pid")
    $matches = (
        $schemaVersion -eq 2 -and
        [string](Get-PropertyValue -Object $payload -Name "phase") -eq "prepared" -and
        [string](Get-PropertyValue -Object $payload -Name "package_id") -eq $script:packageIdentity -and
        (Test-PathEquals -Left (Get-PropertyValue -Object $payload -Name "package_dir") -Right $script:packageDir) -and
        [string](Get-PropertyValue -Object $payload -Name "request_id") -eq [string](Get-PropertyValue -Object $Marker -Name "request_id") -and
        $null -ne $markerPid -and $markerPid -eq $transactionPid -and
        [string](Get-PropertyValue -Object $payload -Name "owner_nonce") -eq [string](Get-PropertyValue -Object $Marker -Name "owner_nonce")
    )
    if (-not $matches) {
        return $unsafe
    }
    return [PSCustomObject]@{ Valid = $true; Path = $candidatePath }
}

function Get-RecoveryHistory {
    $document = Read-JsonDocument -Path $script:recoveryStatePath
    if (-not $document.Present) {
        return [PSCustomObject]@{ Valid = $true; Entries = @() }
    }
    if (-not $document.Valid -or -not (Test-HasProperty -Object $document.Value -Name "destructive_recoveries")) {
        return [PSCustomObject]@{ Valid = $false; Entries = @() }
    }
    $rawEntries = Get-PropertyValue -Object $document.Value -Name "destructive_recoveries"
    if ($rawEntries -is [string]) {
        return [PSCustomObject]@{ Valid = $false; Entries = @() }
    }
    $entries = @()
    foreach ($entry in @($rawEntries)) {
        $age = Get-AgeSeconds -Timestamp $entry
        if ($null -eq $age) {
            return [PSCustomObject]@{ Valid = $false; Entries = @() }
        }
        if ($age -ge 0 -and $age -le $script:recoveryWindowSeconds) {
            $entries += [string]$entry
        }
    }
    return [PSCustomObject]@{ Valid = $true; Entries = @($entries) }
}

function Reserve-RecoverySlot {
    param([object[]]$ExistingEntries)

    try {
        New-Item -ItemType Directory -Path $script:stateDir -Force | Out-Null
        $entries = @($ExistingEntries) + @([DateTime]::UtcNow.ToString("o"))
        $payload = [ordered]@{ destructive_recoveries = @($entries) }
        $tempPath = $script:recoveryStatePath + "." + [guid]::NewGuid().ToString("N") + ".tmp"
        try {
            [System.IO.File]::WriteAllText(
                $tempPath,
                ($payload | ConvertTo-Json -Compress),
                (New-Object System.Text.UTF8Encoding($false))
            )
            Move-Item -LiteralPath $tempPath -Destination $script:recoveryStatePath -Force
        } finally {
            Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        }
        return $true
    } catch {
        return $false
    }
}

function Get-UpdateLockStatus {
    if (-not (Test-Path -LiteralPath $script:updateLockPath -PathType Leaf)) {
        return [PSCustomObject]@{ Safe = $true; Held = $false }
    }
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $script:updateLockPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::None
        )
        return [PSCustomObject]@{ Safe = $true; Held = $false }
    } catch [System.IO.IOException] {
        return [PSCustomObject]@{ Safe = $true; Held = $true }
    } catch {
        return [PSCustomObject]@{ Safe = $false; Held = $false }
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Wait-ForExactProcessExit {
    param(
        [long]$ProcessId,
        [datetime]$ExpectedCreatedAt
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($script:staleRecoveryWaitSeconds)
    do {
        $snapshot = Get-WatchdogProcesses
        if (-not $snapshot.Success) {
            return [PSCustomObject]@{ Safe = $false; Exited = $false }
        }
        $process = Get-ProcessById -Processes $snapshot.Processes -ProcessId $ProcessId
        if ($null -eq $process) {
            return [PSCustomObject]@{ Safe = $true; Exited = $true }
        }
        $createdAt = Get-ProcessCreationUtc -Process $process
        if ($null -eq $createdAt -or [Math]::Abs(($createdAt - $ExpectedCreatedAt).TotalSeconds) -gt 2) {
            return [PSCustomObject]@{ Safe = $false; Exited = $false }
        }
        Start-Sleep -Milliseconds $script:staleRecoveryPollMilliseconds
    } while ([DateTime]::UtcNow -lt $deadline)
    return [PSCustomObject]@{ Safe = $true; Exited = $false }
}

function Wait-ForUpdateLockRelease {
    $deadline = [DateTime]::UtcNow.AddSeconds($script:staleRecoveryWaitSeconds)
    do {
        $status = Get-UpdateLockStatus
        if (-not $status.Safe) {
            return [PSCustomObject]@{ Safe = $false; Released = $false }
        }
        if (-not $status.Held) {
            return [PSCustomObject]@{ Safe = $true; Released = $true }
        }
        Start-Sleep -Milliseconds $script:staleRecoveryPollMilliseconds
    } while ([DateTime]::UtcNow -lt $deadline)
    return [PSCustomObject]@{ Safe = $true; Released = $false }
}

function Get-RateLimitDecision {
    param(
        [string]$Decision,
        [string]$Reason,
        [object]$MatchedOwner,
        [object]$Action
    )

    $history = Get-RecoveryHistory
    if (-not $history.Valid) {
        return New-Decision -Decision "fail_closed" -Reason "recovery_history_invalid" -MatchedOwner $MatchedOwner
    }
    if (@($history.Entries).Count -ge $script:maxDestructiveRecoveries) {
        return New-Decision -Decision "recovery_rate_limited" -Reason "destructive_recovery_limit" -MatchedOwner $MatchedOwner
    }
    if ($WhatIf) {
        return New-Decision -Decision $Decision -Reason $Reason -MatchedOwner $MatchedOwner -Actions @($Action)
    }
    return $null
}

function Invoke-WorkerRestart {
    param(
        [object]$HeartbeatStatus,
        [object]$Owner,
        [object]$Action
    )

    $preflight = Get-RateLimitDecision -Decision "restart_stale_worker" -Reason "exact_worker_heartbeat_stale" -MatchedOwner $Owner.Summary -Action $Action
    if ($null -ne $preflight) {
        return $preflight
    }
    if (-not (Test-Path -LiteralPath $script:guiLauncherPath -PathType Leaf)) {
        return New-Decision -Decision "fail_closed" -Reason "gui_launcher_missing" -MatchedOwner $Owner.Summary
    }
    $lockStatus = Get-UpdateLockStatus
    if (-not $lockStatus.Safe) {
        return New-Decision -Decision "fail_closed" -Reason "update_lock_unverifiable" -MatchedOwner $Owner.Summary
    }
    if ($lockStatus.Held) {
        return New-Decision -Decision "update_lock_held" -Reason "package_update_in_progress" -MatchedOwner $Owner.Summary
    }
    try {
        $liveSnapshot = Get-WatchdogProcesses
        if (-not $liveSnapshot.Success) {
            return New-Decision -Decision "fail_closed" -Reason "cim_recheck_failed" -MatchedOwner $Owner.Summary
        }
        $liveOwner = Get-ExactWorkerOwner -HeartbeatPayload $HeartbeatStatus.Payload -Processes $liveSnapshot.Processes
        if ($null -eq $liveOwner -or $liveOwner.Summary.pid -ne $Owner.Summary.pid -or
            [Math]::Abs(($liveOwner.CreatedAt - $Owner.CreatedAt).TotalSeconds) -gt 2) {
            return New-Decision -Decision "fail_closed" -Reason "worker_identity_changed" -MatchedOwner $Owner.Summary
        }
        $liveProcess = Get-Process -Id $liveOwner.Summary.pid -ErrorAction Stop
        $liveStart = $liveProcess.StartTime.ToUniversalTime()
        if ([Math]::Abs(($liveStart - $liveOwner.CreatedAt).TotalSeconds) -gt 2) {
            return New-Decision -Decision "fail_closed" -Reason "worker_identity_changed" -MatchedOwner $Owner.Summary
        }
        $finalLockStatus = Get-UpdateLockStatus
        if (-not $finalLockStatus.Safe) {
            return New-Decision -Decision "fail_closed" -Reason "update_lock_unverifiable" -MatchedOwner $Owner.Summary
        }
        if ($finalLockStatus.Held) {
            return New-Decision -Decision "update_lock_held" -Reason "package_update_in_progress" -MatchedOwner $Owner.Summary
        }
        $history = Get-RecoveryHistory
        if (-not $history.Valid) {
            return New-Decision -Decision "fail_closed" -Reason "recovery_history_invalid" -MatchedOwner $Owner.Summary
        }
        if (@($history.Entries).Count -ge $script:maxDestructiveRecoveries) {
            return New-Decision -Decision "recovery_rate_limited" -Reason "destructive_recovery_limit" -MatchedOwner $Owner.Summary
        }
        if (-not (Reserve-RecoverySlot -ExistingEntries $history.Entries)) {
            return New-Decision -Decision "fail_closed" -Reason "recovery_slot_unavailable" -MatchedOwner $Owner.Summary
        }
        Stop-Process -InputObject $liveProcess -Force -ErrorAction Stop
        Start-Process -FilePath "wscript.exe" -ArgumentList ('"{0}"' -f $script:guiLauncherPath) -WorkingDirectory $script:packageDir -WindowStyle Hidden | Out-Null
        return New-Decision -Decision "restart_stale_worker" -Reason "exact_worker_heartbeat_stale" -MatchedOwner $Owner.Summary -Actions @($Action)
    } catch {
        return New-Decision -Decision "fail_closed" -Reason "worker_restart_failed" -MatchedOwner $Owner.Summary
    }
}

function Invoke-StaleUpdateRecovery {
    param(
        [object]$Owner,
        [object]$MarkerStatus,
        [object]$Transaction,
        [object]$Action,
        [bool]$ExpectedOwnerLockHeld = $false
    )

    $Marker = $MarkerStatus.Payload
    $preflight = Get-RateLimitDecision -Decision "recover_stale_update" -Reason "exact_updater_phase_stale" -MatchedOwner $Owner.Summary -Action $Action
    if ($null -ne $preflight) {
        return $preflight
    }
    if (-not (Test-Path -LiteralPath $script:remoteUpdateScriptPath -PathType Leaf)) {
        return New-Decision -Decision "fail_closed" -Reason "update_recovery_script_missing" -MatchedOwner $Owner.Summary
    }
    $lockStatus = Get-UpdateLockStatus
    if (-not $lockStatus.Safe) {
        return New-Decision -Decision "fail_closed" -Reason "update_lock_unverifiable" -MatchedOwner $Owner.Summary
    }
    if ($lockStatus.Held -and -not $ExpectedOwnerLockHeld) {
        return New-Decision -Decision "update_lock_held" -Reason "package_update_in_progress" -MatchedOwner $Owner.Summary
    }
    try {
        $liveMarkerStatus = Get-MarkerStatus
        if (-not $liveMarkerStatus.Valid -or $liveMarkerStatus.Mode -ne "strict" -or -not $liveMarkerStatus.Stale) {
            return New-Decision -Decision "fail_closed" -Reason "update_owner_state_changed" -MatchedOwner $Owner.Summary
        }
        $liveSnapshot = Get-WatchdogProcesses
        if (-not $liveSnapshot.Success) {
            return New-Decision -Decision "fail_closed" -Reason "cim_recheck_failed" -MatchedOwner $Owner.Summary
        }
        $liveOwner = Get-ExactUpdaterOwner -MarkerStatus $liveMarkerStatus -Processes $liveSnapshot.Processes
        if ($null -eq $liveOwner -or $liveOwner.Summary.pid -ne $Owner.Summary.pid -or
            [Math]::Abs(($liveOwner.CreatedAt - $Owner.CreatedAt).TotalSeconds) -gt 2) {
            return New-Decision -Decision "fail_closed" -Reason "updater_identity_changed" -MatchedOwner $Owner.Summary
        }
        $liveTransaction = Get-SafeRecoveryTransaction -Marker $liveMarkerStatus.Payload
        if (-not $liveTransaction.Valid -or -not (Test-PathEquals -Left $liveTransaction.Path -Right $Transaction.Path)) {
            return New-Decision -Decision "fail_closed" -Reason "update_transaction_changed" -MatchedOwner $Owner.Summary
        }
        $liveProcess = Get-Process -Id $liveOwner.Summary.pid -ErrorAction Stop
        $liveStart = $liveProcess.StartTime.ToUniversalTime()
        if ([Math]::Abs(($liveStart - $liveOwner.CreatedAt).TotalSeconds) -gt 2) {
            return New-Decision -Decision "fail_closed" -Reason "updater_identity_changed" -MatchedOwner $Owner.Summary
        }
        $finalLockStatus = Get-UpdateLockStatus
        if (-not $finalLockStatus.Safe) {
            return New-Decision -Decision "fail_closed" -Reason "update_lock_unverifiable" -MatchedOwner $Owner.Summary
        }
        if ($finalLockStatus.Held -and -not $ExpectedOwnerLockHeld) {
            return New-Decision -Decision "update_lock_held" -Reason "package_update_in_progress" -MatchedOwner $Owner.Summary
        }
        $history = Get-RecoveryHistory
        if (-not $history.Valid) {
            return New-Decision -Decision "fail_closed" -Reason "recovery_history_invalid" -MatchedOwner $Owner.Summary
        }
        if (@($history.Entries).Count -ge $script:maxDestructiveRecoveries) {
            return New-Decision -Decision "recovery_rate_limited" -Reason "destructive_recovery_limit" -MatchedOwner $Owner.Summary
        }
        if (-not (Reserve-RecoverySlot -ExistingEntries $history.Entries)) {
            return New-Decision -Decision "fail_closed" -Reason "recovery_slot_unavailable" -MatchedOwner $Owner.Summary
        }
        Stop-Process -InputObject $liveProcess -Force -ErrorAction Stop
        $processExit = Wait-ForExactProcessExit -ProcessId $liveOwner.Summary.pid -ExpectedCreatedAt $liveOwner.CreatedAt
        if (-not $processExit.Safe) {
            return New-Decision -Decision "fail_closed" -Reason "updater_exit_unverifiable" -MatchedOwner $Owner.Summary
        }
        if (-not $processExit.Exited) {
            return New-Decision -Decision "fail_closed" -Reason "updater_exit_timeout" -MatchedOwner $Owner.Summary
        }
        $lockRelease = Wait-ForUpdateLockRelease
        if (-not $lockRelease.Safe) {
            return New-Decision -Decision "fail_closed" -Reason "update_lock_unverifiable" -MatchedOwner $Owner.Summary
        }
        if (-not $lockRelease.Released) {
            return New-Decision -Decision "fail_closed" -Reason "update_lock_release_timeout" -MatchedOwner $Owner.Summary
        }
        $requestId = [string](Get-PropertyValue -Object $liveMarkerStatus.Payload -Name "request_id")
        $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -RequestId "{1}" -RecoverTransactionPath "{2}"' -f `
            $script:remoteUpdateScriptPath, $requestId, $liveTransaction.Path
        Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WorkingDirectory $script:packageDir -WindowStyle Hidden | Out-Null
        return New-Decision -Decision "recover_stale_update" -Reason "exact_updater_phase_stale" -MatchedOwner $Owner.Summary -Actions @($Action)
    } catch {
        return New-Decision -Decision "fail_closed" -Reason "update_recovery_start_failed" -MatchedOwner $Owner.Summary
    }
}

function Invoke-Watchdog {
    $mutex = $null
    $ownsMutex = $false
    try {
        $mutexName = "Local\AmbulanceReturnWorkerWatchdog"
        if ($WhatIf) {
            $mutexName = "$mutexName-WhatIf-$PID"
        }
        $mutex = New-Object System.Threading.Mutex($false, $mutexName)
        try {
            $ownsMutex = $mutex.WaitOne(0)
        } catch [System.Threading.AbandonedMutexException] {
            $ownsMutex = $true
        }
        if (-not $ownsMutex) {
            return New-Decision -Decision "already_running" -Reason "watchdog_mutex_held"
        }

        $snapshot = Get-WatchdogProcesses
        if (-not $snapshot.Success) {
            return New-Decision -Decision "fail_closed" -Reason $snapshot.Reason
        }
        $lockStatus = Get-UpdateLockStatus
        if (-not $lockStatus.Safe) {
            return New-Decision -Decision "fail_closed" -Reason "update_lock_unverifiable"
        }
        $expectedStaleUpdateLock = $false
        if ($lockStatus.Held) {
            $lockHeartbeat = Get-HeartbeatStatus
            $lockActivity = Get-ActivityStatus
            $lockMarker = Get-MarkerStatus
            if ($lockHeartbeat.Valid -and $lockHeartbeat.Stale -and
                $lockActivity.Valid -and -not $lockActivity.Fresh -and
                $lockMarker.Valid -and $lockMarker.Mode -eq "strict" -and $lockMarker.Stale) {
                $lockOwner = Get-ExactUpdaterOwner -MarkerStatus $lockMarker -Processes $snapshot.Processes
                if ($null -ne $lockOwner) {
                    $lockTransaction = Get-SafeRecoveryTransaction -Marker $lockMarker.Payload
                    $expectedStaleUpdateLock = $lockTransaction.Valid
                }
            }
            if (-not $expectedStaleUpdateLock) {
                return New-Decision -Decision "update_lock_held" -Reason "package_update_in_progress"
            }
        }

        $heartbeat = Get-HeartbeatStatus
        if (-not $heartbeat.Valid) {
            return New-Decision -Decision "fail_closed" -Reason "heartbeat_invalid"
        }
        if ($heartbeat.Fresh) {
            return New-Decision -Decision "no_action" -Reason "heartbeat_fresh"
        }

        $activity = Get-ActivityStatus
        if (-not $activity.Valid) {
            return New-Decision -Decision "fail_closed" -Reason "activity_invalid"
        }
        if ($activity.Fresh) {
            return New-Decision -Decision "no_action_busy" -Reason "activity_fresh"
        }

        $marker = Get-MarkerStatus
        if (-not $marker.Valid) {
            return New-Decision -Decision "fail_closed" -Reason "update_marker_invalid"
        }
        if ($marker.Present) {
            $updaterOwner = Get-ExactUpdaterOwner -MarkerStatus $marker -Processes $snapshot.Processes
            if ($marker.Mode -eq "legacy" -and $marker.Fresh -and $null -ne $updaterOwner) {
                return New-Decision -Decision "healthy_update" -Reason "legacy_update_grace" -MatchedOwner $updaterOwner.Summary
            }
            if ($marker.Mode -eq "strict" -and $marker.Fresh -and $null -ne $updaterOwner) {
                return New-Decision -Decision "healthy_update" -Reason "owner_and_phase_verified" -MatchedOwner $updaterOwner.Summary
            }
            if ($marker.Mode -eq "strict" -and $marker.Stale -and $null -ne $updaterOwner) {
                $transaction = Get-SafeRecoveryTransaction -Marker $marker.Payload
                if (-not $transaction.Valid) {
                    return New-Decision -Decision "fail_closed" -Reason "update_transaction_invalid" -MatchedOwner $updaterOwner.Summary
                }
                $action = [PSCustomObject][ordered]@{
                    kind = "recover_stale_update"
                    pid = $updaterOwner.Summary.pid
                    request_id = [string](Get-PropertyValue -Object $marker.Payload -Name "request_id")
                }
                return Invoke-StaleUpdateRecovery `
                    -Owner $updaterOwner `
                    -MarkerStatus $marker `
                    -Transaction $transaction `
                    -Action $action `
                    -ExpectedOwnerLockHeld $expectedStaleUpdateLock
            }
            return New-Decision -Decision "identity_uncertain" -Reason "update_owner_not_verified"
        }

        if ($heartbeat.Stale) {
            $workerOwner = Get-ExactWorkerOwner -HeartbeatPayload $heartbeat.Payload -Processes $snapshot.Processes
            if ($null -ne $workerOwner) {
                $action = [PSCustomObject][ordered]@{
                    kind = "restart_gui"
                    pid = $workerOwner.Summary.pid
                }
                return Invoke-WorkerRestart -HeartbeatStatus $heartbeat -Owner $workerOwner -Action $action
            }
        }
        return New-Decision -Decision "identity_uncertain" -Reason "no_exact_package_owner"
    } catch {
        return New-Decision -Decision "fail_closed" -Reason "watchdog_exception"
    } finally {
        if ($null -ne $mutex) {
            if ($ownsMutex) {
                try {
                    $mutex.ReleaseMutex()
                } catch {
                }
            }
            $mutex.Dispose()
        }
    }
}

if (-not $WhatIf -and -not [string]::IsNullOrWhiteSpace($ProcessSnapshotPath)) {
    [Console]::Error.WriteLine("ProcessSnapshotPath is supported only with -WhatIf.")
    exit 2
}

$result = Invoke-Watchdog
if ($WhatIf) {
    [Console]::Out.WriteLine(($result | ConvertTo-Json -Compress -Depth 6))
}
