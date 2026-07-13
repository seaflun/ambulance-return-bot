# Ambulance Worker Stability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use test-driven-development for every behavior change. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all confirmed data-integrity, security, recovery, concurrency, and updater defects in the ambulance worker and NAS task center.

**Architecture:** Harden the existing WinPython source-of-truth in three isolated domains: Selenium/worker execution, NAS task state/security, and persistent storage/update lifecycle. Preserve existing public interfaces where safe, add narrow helpers for leases, outboxes, and atomic writes, then regenerate both deliverables from one explicit version.

**Tech Stack:** Python 3, Flask, Waitress, Selenium, unittest, PowerShell, GitHub CLI.

## Global Constraints

- Edit runtime source only under `WinPython_公務電腦使用包`; never hand-edit generated `UPDATE/NAS包`.
- Never submit a protected-site production record during verification.
- Preserve the approved rule that an otherwise empty patient consumables page receives gloves quantity 1.
- Fail closed on ambiguous case/vehicle selection and unverified saves.
- Do not expose credentials, task JSON, reports, settings, or case state through `/artifacts`.
- Keep user Chrome sessions and healthy parallel worker sessions running during targeted retry cleanup.
- Every production behavior change must be preceded by a regression test that fails for the expected reason.

---

### Task 1: Selenium selection, multi-vehicle execution, and save verification

**Files:**
- Modify: `WinPython_公務電腦使用包/worker.py`
- Modify: `WinPython_公務電腦使用包/consumables_login.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/selenium_local.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/chrome_startup.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/desktop_fast_runner.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/manual_task_lock.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/site_diagnostics.py`
- Test: `tests/test_worker.py`, `tests/test_consumables_login.py`, `tests/test_selenium_local.py`, `tests/test_chrome_startup.py`, `tests/test_desktop_fast_runner.py`

**Interfaces:**
- Consume: `AmbulanceReturnRequest.vehicle_requests()` and existing `SiteAutomationResult` status model.
- Produce: per-vehicle worker execution/results, fail-closed selectors, confirmation-aware saves, owner-safe manual locks, and session-scoped Chrome cleanup.

- [ ] Write focused tests reproducing first-vehicle-only execution, stale vehicle-result reuse after edit, case ID/date omission, single-candidate vehicle mismatch, disinfection mismatch/partial update/timeouts, click-only false saves, and parallel ChromeDriver termination.
- [ ] Run each targeted test module and record expected failures before implementation.
- [ ] Implement the smallest per-vehicle loop and aggregation compatible with current worker status APIs.
- [ ] Make selectors reject ambiguity and explicit vehicle mismatch while preserving same-vehicle multi-patient behavior.
- [ ] Require positive confirmation/readback for saved statuses and return confirmation-needed for indeterminate outcomes.
- [ ] Limit Chrome cleanup and manual-task locks to the owning worker session/task.
- [ ] Run all Task 1 tests until green and save exact commands/results.

### Task 2: NAS security, task leases, corrupt-file recovery, and request correlation

**Files:**
- Modify: `WinPython_公務電腦使用包/app.py`
- Modify: `WinPython_公務電腦使用包/templates/task_detail.html`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_store.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/models.py`
- Test: `tests/test_web_app.py`, `tests/test_task_store.py`, `tests/test_models.py`

**Interfaces:**
- Consume: existing worker token, task queue state, diagnostic artifact links, case-lookup request/result APIs.
- Produce: diagnostic-only artifact serving, active-edit rejection, reclaimable claims, corrupt-file quarantine, accurate retention, atomic vehicle settings, and request-ID-correlated case lookup.

- [ ] Add failing tests for unauthenticated credential artifact access, active edit races, claim-without-reclaim, corrupt JSON list/claim crashes, four-site retention, case-lookup failure-as-success, stale lookup completion, and concurrent vehicle setting writes.
- [ ] Run targeted tests and record expected failures.
- [ ] Restrict `/artifacts` to generated Selenium PNG/HTML diagnostic files.
- [ ] Reject active edits and invalidate completed vehicle-level results when an allowed edit changes task data.
- [ ] Add claim expiry/reclaim semantics and quarantine malformed task files without deleting evidence.
- [ ] Correlate lookup requests/results with request IDs and preserve failure state.
- [ ] Make vehicle settings atomic and serialized; treat intentionally skipped fuel as complete.
- [ ] Run all Task 2 tests until green and save exact commands/results.

### Task 3: Concurrent persistence, reliable status outbox, and update transaction

**Files:**
- Modify: `WinPython_公務電腦使用包/app.py` only after Task 2 integration
- Modify: `WinPython_公務電腦使用包/worker.py` only after Task 1 integration
- Modify: `WinPython_公務電腦使用包/ambulance_bot/sinposmart_backend.py`
- Modify: `WinPython_公務電腦使用包/UPDATE_PACKAGE.ps1`
- Modify: `WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1`
- Modify: `scripts/build_public_duty_package.ps1`
- Modify: `scripts/build_nas_package.ps1`
- Modify or create: release/build orchestration script under `scripts/`
- Test: `tests/test_sinposmart_backend.py`, `tests/test_web_app.py`, `tests/test_worker.py`, update/build script tests

**Interfaces:**
- Consume: worker status endpoint, public-PC pending event file, Sinpo event API, remote update state machine, package version files.
- Produce: serialized/atomic stores, durable idempotent worker status delivery, single-worker update claims, rollback/restart-safe updates, and one-version dual builds.

- [ ] Add failing concurrency tests for Sinpo writes, merged-event retry, timezone fire-day handling, public-PC pending writes, final-status delivery failure, and dual remote-update claims.
- [ ] Add failing script tests for replacement failure rollback/restart and version drift between builds.
- [ ] Run targeted tests and record expected failures.
- [ ] Add shared locking and unique atomic temp files; retain merged event IDs and normalize aware datetimes to Asia/Taipei.
- [ ] Serialize public-PC pending writes and add an atomic worker status outbox with retry/replay.
- [ ] Atomically claim remote updates and validate worker ownership for status transitions.
- [ ] Stage updates before stop, restore on failure, and always restore the prior running state.
- [ ] Generate one version once, pass it explicitly to both builds, and assert source/NAS/zip parity.
- [ ] Run all Task 3 tests until green and save exact commands/results.

### Task 4: Integration, release, and deployment verification

**Files:**
- Modify only files required by review findings.
- Generate: `UPDATE/NAS包` and public release assets.

**Interfaces:**
- Consume: completed Tasks 1-3 and repository release scripts.
- Produce: reviewed source commit, generated packages, GitHub release, parity evidence, and verified live version where authenticated deployment is available.

- [ ] Review the complete diff for source/generated boundary violations, secrets, unrelated edits, duplicated logic, and status compatibility.
- [ ] Run `py -m py_compile` across compatibility launchers and WinPython runtime.
- [ ] Run `py -m unittest discover -s tests -v` and require zero failures.
- [ ] Run PowerShell parser checks, `git diff --check`, and package build tests.
- [ ] Generate one explicit timestamp version, build public and NAS packages with it, and verify every `VERSION.txt` and SHA256.
- [ ] Commit only intentional source/tests/docs/scripts, push `master`, publish a new GitHub release, then download and verify all release assets.
- [ ] If authenticated deployment is available, update NAS/public worker and verify reported versions plus HTTP health; otherwise state exactly which deployment remains manual.
