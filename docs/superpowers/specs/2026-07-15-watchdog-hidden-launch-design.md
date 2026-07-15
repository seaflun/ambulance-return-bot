# Public-Duty Watchdog Hidden Launch Design

**Status:** Requested by the user on 2026-07-15.

## Root Cause

The public-duty `AmbulanceReturnWorkerWatchdog` task runs every minute as the interactive user.  Its scheduled-task action starts `powershell.exe` directly with `-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ...`, but does not request a hidden window.  This is the only recurring automatic launch path that lacks hidden-window handling: the GUI VBS launcher, remote-update subprocess, update restart, and watchdog recovery child launchers already hide their windows.

## Chosen Fix

Add `-WindowStyle Hidden` to the watchdog PowerShell argument string in the source installer and the generated public-package installer template.  The task name, frequency, user, run level, recovery logic, main logon task, startup shortcut, and manual update launcher remain unchanged.

## Scope and Safety

Only the existing `AmbulanceReturnWorkerWatchdog` task for the logged-in public-duty user is re-registered when the package refreshes.  No global Task Scheduler setting, other task, other Windows user, or intentionally interactive `UPDATE_PACKAGE.bat` behavior changes.

## Acceptance

Focused tests assert that source and generated installer use the hidden WindowStyle flag while preserving the watchdog task contract.  After remote deployment, the update installer refreshes the watchdog task and the public-duty operator should no longer see the recurring console flash.
