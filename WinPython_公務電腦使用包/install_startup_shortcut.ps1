param(
    [switch]$WhatIf,
    [switch]$SkipScheduledTask
)

$ErrorActionPreference = "Stop"

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $packageDir "RUN_WORKER_GUI_WINPYTHON.vbs"
$taskName = "AmbulanceReturnWorker"
$shortcutName = "AmbulanceReturnWorker.lnk"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"
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
    if ($WhatIf) {
        Write-Host "Would unregister scheduled task: $taskName"
    } else {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Scheduled task disabled or absent: $taskName"
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

if ($WhatIf) {
    if ($SkipScheduledTask) {
        Write-Host "Would skip scheduled task refresh: $taskName"
    } else {
        Write-Host "Would install scheduled task: $taskName"
    }
    Write-Host "User: $currentUser"
    Write-Host "Target: $target"
    Install-StartupFolderShortcut
    exit 0
}

Install-StartupFolderShortcut
if ($SkipScheduledTask) {
    Write-Host "Skipped scheduled task refresh: $taskName"
    Write-Host "User: $currentUser"
    Write-Host "Target: $target"
    exit 0
}
try {
    Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
    Write-Host "Installed scheduled task: $taskName"
} catch {
    Write-Warning "Could not install scheduled task; startup folder shortcut is installed instead. $($_.Exception.Message)"
}
Write-Host "User: $currentUser"
Write-Host "Target: $target"
exit 0
