# Public PC Remote Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a NAS-triggered, pull-based public-PC update command that waits for all ambulance work and Windows user activity to stop, runs invisibly, reports the result, and keeps the existing manual update button unchanged.

**Architecture:** The NAS Flask app persists one idempotent remote-update command under `artifacts/public_pc` and exposes Worker-Token-protected fetch/status endpoints. The existing Worker loop checks that command before claiming work, defers while task locks, case lookup, or recent Windows input exist, then launches a hidden PowerShell wrapper around the existing updater. The wrapper writes a durable result under `%LOCALAPPDATA%`, and the restarted Worker reports it to NAS.

**Tech Stack:** Python 3, Flask, unittest, urllib, Windows ctypes, PowerShell, Jinja2, existing GitHub Release updater.

## Global Constraints

- Ambulance tasks and automated entry always have priority over remote update.
- Require 120 seconds of Windows input inactivity by default; configure with `REMOTE_UPDATE_IDLE_SECONDS`.
- Remote update must show no console window, popup, or focus-stealing UI.
- Preserve the existing Worker GUI `檢查更新` button and `UPDATE_PACKAGE.bat` behavior.
- Use the existing `X-Worker-Token`; do not open an inbound port on the public PC.
- Do not delete any user-owned workspace file.
- Follow test-first red-green cycles for every production behavior.

---

### Task 1: NAS command persistence and authenticated API

**Files:**
- Modify: `tests/test_web_app.py`
- Modify: `WinPython_公務電腦使用包/app.py`

**Interfaces:**
- Produces: `remote_update_command_file() -> Path`
- Produces: `read_remote_update_command() -> dict`
- Produces: `create_remote_update_command() -> tuple[dict, bool]`
- Produces: `update_remote_update_command(request_id: str, data: dict) -> dict`
- Produces: `POST /admin/public-pc/remote-update`
- Produces: `GET /worker/remote-update?worker_id=...&package_version=...`
- Produces: `POST /worker/remote-update/<request_id>/status`

- [ ] **Step 1: Write failing Flask tests**

Add tests that POST the admin route twice and assert the same active UUID is retained; fetch with the Worker token and assert the full command schema; reject missing tokens with 403; post `waiting_busy`, `waiting_idle`, `updating`, `completed`, and `failed`; reject unknown IDs and statuses; and expire an active command after `REMOTE_UPDATE_STALE_SECONDS`.

```python
def test_remote_update_command_is_idempotent_and_worker_authenticated(self):
    os.environ["WORKER_TOKEN"] = "test-token"
    first = self.client.post("/admin/public-pc/remote-update")
    second = self.client.post("/admin/public-pc/remote-update")
    self.assertEqual(first.status_code, 302)
    self.assertEqual(second.status_code, 302)
    command = app_module.read_remote_update_command()
    request_id = command["request_id"]
    self.assertEqual(command["status"], "pending")
    self.assertEqual(self.client.get("/worker/remote-update").status_code, 403)
    response = self.client.get(
        "/worker/remote-update?worker_id=PC-01&package_version=2026.07.10.1950",
        headers={"X-Worker-Token": "test-token"},
    )
    self.assertEqual(response.get_json()["command"]["request_id"], request_id)
    self.assertEqual(app_module.read_remote_update_command()["worker_id"], "PC-01")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m unittest tests.test_web_app.WebAppTests.test_remote_update_command_is_idempotent_and_worker_authenticated -v`

Expected: FAIL because `/admin/public-pc/remote-update` and `read_remote_update_command` do not exist.

- [ ] **Step 3: Implement atomic command state and routes**

Use the existing `write_json_atomic` helper and `_public_pc_report_lock`. Active states are `pending`, `waiting_busy`, `waiting_idle`, and `updating`; terminal states are `completed`, `up_to_date`, `failed`, and `timed_out`. Only an active, non-stale command is returned to the Worker.

```python
REMOTE_UPDATE_ACTIVE_STATUSES = {"pending", "waiting_busy", "waiting_idle", "updating"}
REMOTE_UPDATE_TERMINAL_STATUSES = {"completed", "up_to_date", "failed", "timed_out"}

def remote_update_command_file() -> Path:
    return artifacts_dir / "public_pc" / "remote_update.json"

def create_remote_update_command() -> tuple[dict, bool]:
    with _public_pc_report_lock:
        current = read_remote_update_command_unlocked()
        if remote_update_command_is_active(current):
            return current, False
        now = datetime.now().isoformat(timespec="seconds")
        command = {
            "request_id": str(uuid4()),
            "status": "pending",
            "requested_at": now,
            "updated_at": now,
            "worker_id": "",
            "before_version": "",
            "installed_version": "",
            "detail": "等待公務電腦接收更新命令。",
        }
        write_json_atomic(remote_update_command_file(), command)
        return command, True
```

- [ ] **Step 4: Run all new Flask command tests and verify GREEN**

Run: `python -m unittest tests.test_web_app.WebAppTests -v`

Expected: all `WebAppTests` pass.

- [ ] **Step 5: Commit the NAS command API**

Run: `git add tests/test_web_app.py WinPython_公務電腦使用包/app.py && git commit -m "Add NAS remote update command API"`

---

### Task 2: Worker safety gate, hidden launcher, and durable result reporting

**Files:**
- Modify: `tests/test_worker.py`
- Modify: `WinPython_公務電腦使用包/worker.py`
- Create: `WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1`
- Modify: `WinPython_公務電腦使用包/.env.example`

**Interfaces:**
- Consumes: Task 1 Worker endpoints and command schema.
- Produces: `windows_user_idle_seconds() -> float`
- Produces: `remote_update_busy_reason(artifacts_dir: Path) -> str`
- Produces: `maybe_run_remote_update(server_url: str, worker_id: str, artifacts_dir: Path) -> bool`
- Produces: `%LOCALAPPDATA%/AmbulanceReturnBot/remote_update_result.json`

- [ ] **Step 1: Write failing Worker behavior tests**

Add tests proving manual task event, task lock, active case lookup, and Windows idle time under 120 seconds each defer without launching; an idle Worker posts `updating` before launching exactly once; a completed local result is posted and marked `reported_at`; and the main loop calls remote update before task claiming.

```python
def test_remote_update_waits_for_windows_idle_without_launching(self):
    launches = []
    statuses = []
    command = {"request_id": "update-1", "status": "pending"}
    with tempfile.TemporaryDirectory() as tmp:
        result = worker_module.maybe_run_remote_update(
            "http://nas",
            "PC-01",
            Path(tmp),
            fetch_command=lambda *_: command,
            post_command_status=lambda *args: statuses.append(args),
            idle_seconds=lambda: 30.0,
            launch_update=lambda *_: launches.append(True),
        )
    self.assertFalse(result)
    self.assertEqual(launches, [])
    self.assertEqual(statuses[-1][2], "waiting_idle")
```

- [ ] **Step 2: Run focused Worker tests and verify RED**

Run: `python -m unittest tests.test_worker.WorkerTests.test_remote_update_waits_for_windows_idle_without_launching -v`

Expected: FAIL because `maybe_run_remote_update` does not exist.

- [ ] **Step 3: Implement the Worker safety gate**

Use optional injected callables only at the orchestration boundary so tests exercise real state decisions. Read the case lookup request at `artifacts/cases/request.json`; treat `case_lookup_requested` as busy. On Windows, call `GetLastInputInfo`; on non-Windows return infinity.

```python
def remote_update_busy_reason(artifacts_dir: Path) -> str:
    if MANUAL_TASK_ACTIVE.is_set() or manual_task_lock_active(artifacts_dir):
        return "勤務登打仍在執行。"
    request_path = artifacts_dir / "cases" / "request.json"
    request_payload = read_json_file(request_path)
    if request_payload.get("status") == "case_lookup_requested":
        return "案件查詢仍在執行。"
    return ""
```

In `main()`, report any prior result, evaluate the remote command, and only then run credential sync, case lookup, or claim the next task. Return to the loop after launching so no new task can start.

- [ ] **Step 4: Add the hidden PowerShell wrapper**

The wrapper starts `update_package.ps1` in a separate hidden PowerShell process, waits for its exit code, compares `VERSION.txt` before and after, and atomically writes a complete JSON result. It must not modify `UPDATE_PACKAGE.bat`.

```powershell
param([Parameter(Mandatory = $true)][string]$RequestId)
$ErrorActionPreference = "Stop"
$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$resultDir = Join-Path $env:LOCALAPPDATA "AmbulanceReturnBot"
$resultPath = Join-Path $resultDir "remote_update_result.json"
$beforeVersion = (Get-Content (Join-Path $packageDir "VERSION.txt") -Raw -Encoding UTF8).Trim()
$process = Start-Process powershell -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
    "-File", ('"' + (Join-Path $packageDir "update_package.ps1") + '"')
) -WindowStyle Hidden -Wait -PassThru
$afterVersion = (Get-Content (Join-Path $packageDir "VERSION.txt") -Raw -Encoding UTF8).Trim()
$status = if ($process.ExitCode -ne 0) { "failed" } elseif ($afterVersion -eq $beforeVersion) { "up_to_date" } else { "completed" }
```

- [ ] **Step 5: Run Worker tests and verify GREEN**

Run: `python -m unittest tests.test_worker -v`

Expected: all Worker tests pass and no real updater process starts.

- [ ] **Step 6: Parse-check the PowerShell files**

Run: `powershell -NoProfile -Command "$errors=$null;$tokens=$null;[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1'),[ref]$tokens,[ref]$errors)|Out-Null;if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}"`

Expected: exit code 0 with no parse errors.

- [ ] **Step 7: Commit Worker remote update execution**

Run: `git add tests/test_worker.py WinPython_公務電腦使用包/worker.py WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1 WinPython_公務電腦使用包/.env.example && git commit -m "Run public PC updates safely in background"`

---

### Task 3: NAS admin status card and preserved manual button

**Files:**
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_worker_gui.py`
- Modify: `WinPython_公務電腦使用包/templates/admin_public_pc.html`
- Verify unchanged behavior: `WinPython_公務電腦使用包/worker_gui.py`

**Interfaces:**
- Consumes: `remote_update` context from Task 1.
- Produces: POST form to `/admin/public-pc/remote-update` and status labels for every command state.

- [ ] **Step 1: Write failing admin-page tests**

Assert the NAS page shows `遠端更新公務電腦`, the confirmation form, installed version, Worker ID, timestamp, and localized states. Set `PUBLIC_PC_REPORT_ENABLED=true` and assert the local public-PC page does not show the remote trigger. Keep the existing GUI launcher test asserting `find_update_launcher()` resolves `UPDATE_PACKAGE.bat`.

```python
def test_admin_public_pc_shows_remote_update_card_only_on_nas(self):
    os.environ.pop("PUBLIC_PC_REPORT_ENABLED", None)
    body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))
    self.assertIn("遠端更新公務電腦", body)
    self.assertIn('action="/admin/public-pc/remote-update"', body)
    os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
    local_body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))
    self.assertNotIn("遠端更新公務電腦", local_body)
```

- [ ] **Step 2: Run the focused page test and verify RED**

Run: `python -m unittest tests.test_web_app.WebAppTests.test_admin_public_pc_shows_remote_update_card_only_on_nas -v`

Expected: FAIL because the card does not exist.

- [ ] **Step 3: Add the responsive status card**

Render one POST button when no active command exists. While active, disable duplicate submission and show the current status. Terminal states allow another command. Add a browser confirmation string explaining that the PC waits for all work and 120 seconds of inactivity.

- [ ] **Step 4: Run page and GUI regression tests**

Run: `python -m unittest tests.test_web_app.WebAppTests tests.test_worker_gui -v`

Expected: all tests pass, including existing manual update launcher tests.

- [ ] **Step 5: Commit the admin UI**

Run: `git add tests/test_web_app.py tests/test_worker_gui.py WinPython_公務電腦使用包/templates/admin_public_pc.html && git commit -m "Show remote update status in NAS admin"`

---

### Task 4: Full verification, package, release, and deployment

**Files:**
- Modify: `WinPython_公務電腦使用包/VERSION.txt`
- Generated: `UPDATE/公務電腦使用包/*`
- Generated: `UPDATE/NAS/*`

**Interfaces:**
- Consumes: all preceding tasks.
- Produces: a versioned GitHub Release and synchronized NAS deployment.

- [ ] **Step 1: Run source validation**

Run: `python -m compileall WinPython_公務電腦使用包`

Run: `python -m unittest discover -s tests -v`

Expected: compile exit code 0 and all tests pass with 0 failures and 0 errors.

- [ ] **Step 2: Build the public-duty and NAS packages**

Use release version `2026.07.11.1548`, update `VERSION.txt`, then run the repository build scripts using that exact version.

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_public_duty_package.ps1 -Version 2026.07.11.1548`

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_nas_package.ps1`

Expected: both commands exit 0 and the public package contains `REMOTE_UPDATE_PACKAGE.ps1`.

- [ ] **Step 3: Commit and push the release**

Stage only intentional source, test, version, and generated package files. Preserve unrelated untracked `.codex/`, `NAS包(舊版)/`, and `docs/superpowers/plans/2026-06-29-nas-entry-implementation.md`.

Run: `git commit -m "Release ambulance worker 2026.07.11.1548"`

Run: `git push origin master`

- [ ] **Step 4: Publish and verify GitHub assets**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/publish_ambulance_return_release.ps1 -Version 2026.07.11.1548`

Re-download both `latest/download` and direct-tag ZIP/SHA assets to `%TEMP%`; compare SHA256 with the published checksum and inspect the ZIP for `worker.py`, `REMOTE_UPDATE_PACKAGE.ps1`, `update_package.ps1`, and `VERSION.txt`.

- [ ] **Step 5: Deploy and verify NAS**

Deploy the generated NAS package with the established project workflow, restart `ambulance-app-1`, then verify source hashes equal `UPDATE/NAS` hashes equal `\\100.114.126.58\docker\ambulance_return_bot` hashes. Verify `/status` reports the new version and expected runtime fingerprint.

- [ ] **Step 6: Verify the running public-PC package**

Launch the Worker GUI for visible runtime inspection if the public PC is reachable. Confirm the manual update button remains, the Worker process restarts minimized, and a NAS test command reaches a non-disruptive waiting or terminal state without displaying a console window.
