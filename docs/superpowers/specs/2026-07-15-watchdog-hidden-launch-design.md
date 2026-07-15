# Public-Duty Watchdog Hidden Launch Design

**Status:** User-authorized repair, reconfirmed by the 2026-07-16 Windows Terminal screenshot.

## Root Cause

The public-duty `AmbulanceReturnWorkerWatchdog` task runs every minute as the interactive user. Its scheduled-task action starts `powershell.exe` directly. Even with `-WindowStyle Hidden`, the target Windows configuration hosts that console in Windows Terminal, which is visibly shown in the operator's screenshot. The login-only main Worker task already uses `wscript.exe` and is not the periodic source.

## Chosen Fix

Add a package-local `RUN_WORKER_WATCHDOG.vbs`. The watchdog task starts `wscript.exe` with that VBS; the VBS resolves its own package directory, invokes the existing recovery PowerShell script with the current arguments, and uses `Shell.Run ... , 0, False` to suppress the host window. The task name, frequency, user, run level, recovery logic, main logon task, startup shortcut, and manual update launcher remain unchanged.

## Scope and Safety

Only the existing `AmbulanceReturnWorkerWatchdog` task for the logged-in public-duty user is re-registered when the package refreshes. The installer fails closed if the recovery script or VBS launcher is missing. No global Task Scheduler setting, other task, other Windows user, or intentionally interactive `UPDATE_PACKAGE.bat` behavior changes.

## Acceptance

Focused tests assert that the source installer and generated public-package installer use `wscript.exe`, preserve the watchdog contract, package the VBS, and no longer define a direct PowerShell watchdog action. After remote deployment, the update installer refreshes the watchdog task and the public-duty operator should no longer see the recurring Terminal window.
