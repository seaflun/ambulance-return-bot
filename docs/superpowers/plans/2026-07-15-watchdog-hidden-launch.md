# Public-Duty Watchdog Hidden Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the recurring public-duty watchdog PowerShell console from appearing without changing any other startup behavior.

**Architecture:** Retain the existing interactive scheduled task but change only its executable boundary: Task Scheduler starts `wscript.exe`, which runs a package-local VBS wrapper. The wrapper starts the existing recovery PowerShell script with a hidden window. Keep the source installer and build script's embedded installer equivalent.

**Tech Stack:** PowerShell 5.1, Windows Task Scheduler, unittest source/template contract tests.

## Global Constraints

- Change only `AmbulanceReturnWorkerWatchdog`; do not alter `AmbulanceReturnWorker`, startup shortcuts, or manual update UI.
- Keep the watchdog cadence, user, run level, recovery script path, and `IgnoreNew` setting unchanged.
- Source installer and generated public-package installer must both use the VBS action and package the same wrapper.

---

### Task 1: Hide only the recurring watchdog PowerShell host

**Files:**

- Create: `WinPython_公務電腦使用包/RUN_WORKER_WATCHDOG.vbs`
- Modify: `WinPython_公務電腦使用包/install_startup_shortcut.ps1:14-24,127-160,184-213`
- Modify: `scripts/build_public_duty_package.ps1:285-303,452-462,565-650,681-687`
- Test: `tests/test_worker_gui.py:252-279`

**Interfaces:**

- `RUN_WORKER_WATCHDOG.vbs` resolves its own directory and starts `WORKER_SELF_RECOVERY.ps1` via `Shell.Run command, 0, False`.
- `New-WatchdogTask` continues to register the exact existing task name and principal, but its action executes `wscript.exe` with the VBS path.

- [ ] **Step 1: Write a failing installer contract test**

  Assert that the VBS source file exists, both source and generated installer templates define `RUN_WORKER_WATCHDOG.vbs`, their task action executes `wscript.exe`, and they retain `AmbulanceReturnWorkerWatchdog` plus `-MultipleInstances IgnoreNew`.

- [ ] **Step 2: Run it to verify RED**

  Run:

  ```powershell
  C:\Users\seafl\AppData\Local\Python\pythoncore-3.14-64\python.exe -m unittest tests.test_worker_gui.WorkerGuiEnvTests.test_startup_installer_and_public_package_template_define_watchdog -v
  ```

  Expected: FAIL because the VBS launcher is absent.

- [ ] **Step 3: Make the minimal source/template edit**

  Add the eight-line VBS launcher and make source/template watchdog task actions call it through `wscript.exe`. Preserve the trigger, principal, main task, and unrelated update launchers.

- [ ] **Step 4: Run focused GREEN checks**

  Run the source/template test, the Windows `-WhatIf` installer test, and PowerShell AST parsing for the source installer and build script. Expected: all pass and `WhatIf` reports a `wscript.exe` watchdog action.

- [ ] **Step 5: Commit the focused change**

  ```powershell
  git add -- WinPython_公務電腦使用包/install_startup_shortcut.ps1 scripts/build_public_duty_package.ps1 tests/test_worker_gui.py docs/superpowers/specs/2026-07-15-watchdog-hidden-launch-design.md docs/superpowers/plans/2026-07-15-watchdog-hidden-launch.md
  git commit -m "fix: hide public-duty watchdog console"
  ```
