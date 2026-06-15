param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $packageDir "RUN_WORKER_GUI_WINPYTHON.vbs"
$taskName = "救護回程 Worker"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Cannot find startup target: $target"
}

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$target`"" -WorkingDirectory $packageDir
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

if ($WhatIf) {
    Write-Host "Would install scheduled task: $taskName"
    Write-Host "User: $currentUser"
    Write-Host "Target: $target"
    exit 0
}

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Write-Host "Installed scheduled task: $taskName"
Write-Host "User: $currentUser"
Write-Host "Target: $target"
