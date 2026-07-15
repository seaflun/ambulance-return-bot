param(
    [switch]$WhatIf,
    [switch]$SkipScheduledTask
)

$ErrorActionPreference = "Stop"

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $packageDir "RUN_WORKER_GUI_WINPYTHON.vbs"
$taskName = "AmbulanceReturnWorker"
$watchdogTaskName = "AmbulanceReturnWorkerWatchdog"
$shortcutName = "AmbulanceReturnWorker.lnk"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"
$watchdogScript = Join-Path $packageDir "WORKER_SELF_RECOVERY.ps1"
$watchdogPowerShell = if ([string]::IsNullOrWhiteSpace($env:WINDIR)) {
    "powershell.exe"
} else {
    Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
}
if ($watchdogPowerShell -ne "powershell.exe" -and -not (Test-Path -LiteralPath $watchdogPowerShell -PathType Leaf)) {
    $watchdogPowerShell = "powershell.exe"
}
$watchdogArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$watchdogScript`""
$startupDisabledValues = @("0", "false", "no", "off")

function Get-PackageEnvValue {
    param([string]$Name)

    $envPath = Join-Path $packageDir ".env"
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        return ""
    }
    foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -eq 2 -and $parts[0].Trim() -eq $Name) {
            return $parts[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function StartupLauncherEnabled {
    $value = $env:WORKER_STARTUP_LAUNCHER_ENABLED
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = Get-PackageEnvValue -Name "WORKER_STARTUP_LAUNCHER_ENABLED"
    }
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $true
    }
    return -not ($startupDisabledValues -contains $value.Trim().ToLowerInvariant())
}

function Get-StartupDir {
    $startupDir = [Environment]::GetFolderPath("Startup")
    if ([string]::IsNullOrWhiteSpace($startupDir)) {
        $startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
    }
    return $startupDir
}

function Remove-StartupFolderShortcut {
    $shortcutPath = Join-Path (Get-StartupDir) $shortcutName
    if ($WhatIf) {
        Write-Host "Would remove startup folder shortcut: $shortcutPath"
        return
    }
    if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
        Remove-Item -LiteralPath $shortcutPath -Force
        Write-Host "Removed startup folder shortcut: $shortcutPath"
    } else {
        Write-Host "Startup folder shortcut already absent: $shortcutPath"
    }
}

function Disable-StartupLaunchers {
    Remove-StartupFolderShortcut
    foreach ($scheduledTaskName in @($taskName, $watchdogTaskName)) {
        if ($WhatIf) {
            Write-Host "Would unregister scheduled task: $scheduledTaskName"
        } else {
            Unregister-ScheduledTask -TaskName $scheduledTaskName -Confirm:$false -ErrorAction SilentlyContinue
            Write-Host "Scheduled task disabled or absent: $scheduledTaskName"
        }
    }
    Write-Host "Startup launcher disabled by WORKER_STARTUP_LAUNCHER_ENABLED=false"
}

if (-not (StartupLauncherEnabled)) {
    Disable-StartupLaunchers
    exit 0
}

function Install-StartupFolderShortcut {
    $startupDir = Get-StartupDir
    $shortcutPath = Join-Path $startupDir $shortcutName

    if ($WhatIf) {
        Write-Host "Would install startup folder shortcut: $shortcutPath"
        Write-Host "Target: $target"
        return
    }

    if (-not (Test-Path -LiteralPath $startupDir -PathType Container)) {
        New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $wscript
    $shortcut.Arguments = "`"$target`""
    $shortcut.WorkingDirectory = $packageDir
    $shortcut.WindowStyle = 7
    $shortcut.Description = "SinpoSmart - 救護Worker GUI"
    $shortcut.Save()
    Write-Host "Installed startup folder shortcut: $shortcutPath"
    Write-Host "Target: $target"
}

if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Cannot find startup target: $target"
}

function New-WatchdogTask {
    $action = New-ScheduledTaskAction -Execute $watchdogPowerShell -Argument $watchdogArguments
    $trigger = New-ScheduledTaskTrigger `
        -Once `
        -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    return New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
}

function Install-WatchdogTask {
    param([object]$Task)

    if (-not (Test-Path -LiteralPath $watchdogScript -PathType Leaf)) {
        $message = "Could not install watchdog task because its script is missing: $watchdogScript"
        Write-Warning $message
        return $message
    }
    try {
        Register-ScheduledTask -TaskName $watchdogTaskName -InputObject $Task -Force | Out-Null
        Write-Host "Installed watchdog task: $watchdogTaskName"
        return ""
    } catch {
        $message = "Could not install watchdog task; startup folder shortcut is installed instead. $($_.Exception.Message)"
        Write-Warning $message
        return $message
    }
}

if (-not $SkipScheduledTask) {
    $action = New-ScheduledTaskAction -Execute $wscript -Argument "`"$target`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    try {
        $trigger.Delay = "PT1M"
    } catch {
        Write-Warning "Could not set startup delay; the task will start immediately after logon."
    }
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
}
$watchdogTask = New-WatchdogTask

if ($WhatIf) {
    if ($SkipScheduledTask) {
        Write-Host "Would skip scheduled task refresh: $taskName"
    } else {
        Write-Host "Would install scheduled task: $taskName"
    }
    Write-Host "User: $currentUser"
    Write-Host "Target: $target"
    Write-Host "Would install watchdog task: $watchdogTaskName"
    Write-Host "User: $currentUser"
    Write-Host "Action: $watchdogPowerShell $watchdogArguments"
    Install-StartupFolderShortcut
    exit 0
}

Install-StartupFolderShortcut
if ($SkipScheduledTask) {
    Write-Host "Skipped scheduled task refresh: $taskName"
} else {
    try {
        Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
        Write-Host "Installed scheduled task: $taskName"
    } catch {
        Write-Warning "Could not install scheduled task; startup folder shortcut is installed instead. $($_.Exception.Message)"
    }
}
$watchdogInstallWarning = Install-WatchdogTask -Task $watchdogTask
Write-Host "User: $currentUser"
Write-Host "Target: $target"
Write-Host "Watchdog action: $watchdogPowerShell $watchdogArguments"
if (-not [string]::IsNullOrWhiteSpace([string]$watchdogInstallWarning)) {
    Write-Warning "watchdog_install_warning: $watchdogInstallWarning"
    exit 2
}
exit 0
