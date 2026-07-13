# Ambulance Worker Stability Hardening Design

## Objective

Eliminate every confirmed worker, NAS task-center, Selenium selection, persistence, concurrency, and update-path defect found in the 2026-07-13 audit without changing the operator's intended workflow.

## Safety boundaries

- `WinPython_公務電腦使用包` remains the runtime source of truth. `UPDATE/NAS包` is generated only after source verification.
- Protected-site operations are exercised only with mocks, temporary artifacts, or deterministic helper tests. No production record is submitted during development.
- Existing user behavior remains unchanged where it is intentional, including supplementing otherwise empty multi-patient consumables pages with one pair of gloves.
- Ambiguous case, vehicle, or save outcomes fail closed and display a readable confirmation-needed state instead of claiming success.
- Credentials, task data, browser profiles, and runtime artifacts remain untracked and outside release archives.

## Design

### 1. Exact record selection and multi-vehicle execution

The background `worker_queue` path will use the same per-vehicle expansion model as the desktop runner. Mileage, disinfection, fuel, and consumables will retain independent per-vehicle results so one vehicle cannot mask another. Editing a completed multi-vehicle task invalidates stale vehicle-level results.

Duty cases will be matched by exact case ID first, then date, time, and normalized address. A fallback may be used only when it yields one unambiguous candidate. Consumables and disinfection will never accept a candidate whose explicit vehicle differs from the requested vehicle. Same-vehicle multi-patient pages remain valid and are distributed using the approved allocation rule.

### 2. Verified persistence and reliable status delivery

Work log, mileage, fuel, and disinfection saves will require a positive server/UI confirmation or a deterministic readback of the saved values. A click without confirmation returns a confirmation-needed status. Partial disinfection-item updates are rejected.

Worker status POSTs will be retryable and durably spooled when the NAS is temporarily unreachable. Spool writes are atomic and serialized. Replayed terminal statuses are idempotent, preventing a successful portal write from appearing failed and being repeated by the operator.

### 3. Task-state correctness and recovery

Active tasks cannot be edited. Queue claims carry an expiring lease and can be reclaimed after a worker crash. Status updates validate the active claim/revision where applicable. Corrupt task JSON files are quarantined and skipped so one damaged file cannot stop task listing or claiming. Four-site tasks that intentionally omit fuel are considered fully complete for retention.

Case lookup requests receive request IDs. Worker results complete only the matching request, and failure statuses remain failures rather than being displayed as an empty successful query.

### 4. Security and concurrent storage

The generic artifact route will expose only intended diagnostic PNG/HTML files and never credentials, tasks, reports, settings, or case state. Credential and vehicle-setting writes become atomic.

SinpoSmart daily event updates and public-PC pending reports use shared locks plus unique atomic temporary files. Sinpo event deduplication retains all merged event IDs, and timezone-aware timestamps are normalized to Asia/Taipei before applying the 08:00 fire-day boundary.

### 5. Browser, lock, and update lifecycle

Chrome retry cleanup targets only the failed worker session's browser/profile/process tree and does not terminate healthy parallel ChromeDriver sessions. Manual-task locks are owner-safe and refreshed while work is active.

Remote update commands are atomically claimed by one worker. Package update stages and validates files before stopping the worker, restores the backup after a failed replacement, and restarts a previously running worker in all outcomes. One version value is generated once and passed to both public and NAS builds, followed by parity assertions.

## Verification

Each defect receives a regression test that is observed failing before production code changes. Completion requires targeted suites, all unit tests, Python compilation, PowerShell parser/build checks, package builds, version/SHA parity, a clean intentional diff, commit/push, release publication, and remote asset readback. Live NAS/worker deployment is performed only if the configured authenticated update path is available and can be verified safely.
