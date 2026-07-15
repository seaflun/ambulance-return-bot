# Public-Duty Watchdog Hidden Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the recurring public-duty watchdog PowerShell console from appearing without changing any other startup behavior.

**Architecture:** Retain the existing interactive scheduled task and add PowerShell's hidden-window argument solely to its argument string.  Keep the build script's embedded installer text byte-for-byte equivalent in behavior.

**Tech Stack:** PowerShell 5.1, Windows Task Scheduler, unittest source/template contract tests.

## Global Constraints

- Change only `AmbulanceReturnWorkerWatchdog`; do not alter `AmbulanceReturnWorker`, startup shortcuts, or manual update UI.
- Keep the watchdog cadence, user, run level, script path, and `IgnoreNew` setting unchanged.
- Source installer and generated public-package installer must contain the same hidden action behavior.

---

### Task 1: Hide only the recurring watchdog PowerShell host

**Files:**

- Modify: `WinPython_公務電腦使用包/install_startup_shortcut.ps1:16-24`
- Modify: `scripts/build_public_duty_package.ps1:452-462`
- Test: `tests/test_worker_gui.py:252-263,275-292`

**Interfaces:**

- `$watchdogArguments` retains the current script path and execution policy arguments and gains `-WindowStyle Hidden`.
- `New-WatchdogTask` continues to register the exact existing task name and principal.

- [ ] **Step 1: Write a failing installer contract test**

  Assert that both source and generated installer template include the exact watchdog argument fragment `-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File` and retain `AmbulanceReturnWorkerWatchdog` plus `-MultipleInstances IgnoreNew`.

- [ ] **Step 2: Run it to verify RED**

  Run:

  ```powershell
  C:\Users\seafl\AppData\Local\Python\pythoncore-3.14-64\python.exe -m unittest tests.test_worker_gui.WorkerGuiEnvTests.test_startup_installer_and_public_package_template_define_watchdog -v
  ```

  Expected: FAIL because the WindowStyle fragment is absent.

- [ ] **Step 3: Make the minimal source/template edit**

  Add `-WindowStyle Hidden` only within `$watchdogArguments` in both locations.  Do not alter the scheduled-task action executable, trigger, principal, main task, or unrelated update launchers.

- [ ] **Step 4: Run focused GREEN checks**

  Run the source/template test, the Windows `-WhatIf` installer test, and PowerShell AST parsing for the source installer and build script.  Expected: all pass and `WhatIf` reports a hidden watchdog action.

- [ ] **Step 5: Commit the focused change**

  ```powershell
  git add -- WinPython_公務電腦使用包/install_startup_shortcut.ps1 scripts/build_public_duty_package.ps1 tests/test_worker_gui.py docs/superpowers/specs/2026-07-15-watchdog-hidden-launch-design.md docs/superpowers/plans/2026-07-15-watchdog-hidden-launch.md
  git commit -m "fix: hide public-duty watchdog console"
  ```

