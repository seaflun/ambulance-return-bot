# NAS 車損入口與消毒帳號順序 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 NAS 首頁提供車輛損害管理外連入口，精簡救護後台事件，並讓每台救護車的消毒登打依司機、出勤人員、同步帳號的順序登入。

**Architecture:** NAS 首頁與救護後台只改 Jinja 模板。消毒模組建立每車的具來源標示登入候選清單，桌面 runner 在每個車輛明細中各自開啟登入流程與專用 Chrome profile，再將既有消毒任務函式接到已登入的 driver。

**Tech Stack:** Python 3、Flask/Jinja2、Selenium、unittest、PowerShell NAS package build。

## Global Constraints

- Canonical source is `WinPython_公務電腦使用包`; `UPDATE\NAS包` is generated only.
- Do not add credentials, cookies, profiles, task data, or `.env` files to Git or packages.
- Preserve existing verification-code retry count (`MAX_LOGIN_ATTEMPTS`) for each credential.
- Keep disinfection form submission and manual-confirmation rules unchanged.
- Use a new tab with `rel="noopener noreferrer"` for the external vehicle-damage portal.

---

### Task 1: NAS entry and event display

**Files:**
- Modify: `tests/test_web_app.py:1337-1355,2030-2085`
- Modify: `WinPython_公務電腦使用包/templates/nas_home.html:127-139`
- Modify: `WinPython_公務電腦使用包/templates/admin_public_pc.html:406-415`

**Interfaces:**
- Consumes: Flask NAS home route and public-PC report fixture.
- Produces: A fourth external NAS entry and event markup without the report-source-account row.

- [ ] **Step 1: Write the failing web assertions**

```python
self.assertIn("車輛損害管理", body)
self.assertIn(
    'href="https://sinposmart-vehicle-damage-portal.sinpo666.workers.dev"',
    body,
)
self.assertIn('target="_blank"', body)
self.assertIn('rel="noopener noreferrer"', body)

self.assertNotIn("回報來源帳號：8番 曾彥綸 - tyfd01510", body)
self.assertIn("五站登打成功", body)
self.assertIn("本機快速執行完成。", body)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `py -m unittest tests.test_web_app.WebAppTests.test_nas_index_shows_entry_buttons_only tests.test_web_app.WebAppTests.test_admin_public_pc_receives_and_lists_local_task_events -v`

Expected: the new entry assertion fails and the removed event-row assertion fails because the current template still contains the row.

- [ ] **Step 3: Make the minimal template changes**

Add this NAS card after the existing entry cards:

```html
<a class="entry-link" href="https://sinposmart-vehicle-damage-portal.sinpo666.workers.dev" target="_blank" rel="noopener noreferrer">
  <span>車輛損害管理</span>
  <span class="entry-arrow">進入</span>
</a>
```

Delete only the `event_operator` assignment and the `event-operator` element that renders `回報來源帳號`; retain `event.action` and the optional `event.detail` block.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `py -m unittest tests.test_web_app.WebAppTests.test_nas_index_shows_entry_buttons_only tests.test_web_app.WebAppTests.test_admin_public_pc_receives_and_lists_local_task_events -v`

Expected: both tests pass.

- [ ] **Step 5: Commit the independently testable UI change**

```powershell
git add -- tests/test_web_app.py WinPython_公務電腦使用包/templates/nas_home.html WinPython_公務電腦使用包/templates/admin_public_pc.html
git commit -m "feat: add vehicle damage NAS entry"
```

### Task 2: Per-vehicle disinfection credential ordering

**Files:**
- Create: `tests/test_disinfect.py`
- Modify: `WinPython_公務電腦使用包/disinfect.py:16,43-87`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/login_audit.py:18-83`
- Modify: `tests/test_login_audit.py:86-178`

**Interfaces:**
- Consumes: `AmbulanceReturnRequest.driver_duty_login_account_candidates`, `personnel_duty_login_account_candidates`, `load_duty_credential`, and `load_synced_worker_credential`.
- Produces: `login_and_get_driver(request=...)` that tries driver credentials, then other personnel, then the selected sync credential; `disinfection_login_summary(request)` and `disinfection_login_audit(request)` describe the same priority.

- [ ] **Step 1: Write failing credential-order tests**

```python
request = AmbulanceReturnRequest(
    task_id="task-disinfection-login",
    created_at=datetime.now(), raw_text="", driver="王昱勛",
    personnel=["張家和", "王昱勛"],
    personnel_accounts=["tyfd01317", "tyfd01987"],
)
attempts = disinfect._disinfection_credential_attempts(request)
self.assertEqual(
    [(credential.user_id, source) for credential, source in attempts],
    [("tyfd01987", "任務司機"), ("tyfd01317", "出勤人員")],
)
```

Add a second test whose selected synced account is not in the task and assert it is appended as `("tyfd09999", "同步帳號")`. Add login-audit assertions that the disinfection summary is labelled `任務司機` when that account is available and `出勤人員` when the driver account is unavailable.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `py -m unittest tests.test_disinfect tests.test_login_audit -v`

Expected: import or attribute failure because `_disinfection_credential_attempts` and request-aware disinfection audit do not yet exist.

- [ ] **Step 3: Implement the minimal ordered credential helper**

In `disinfect.py`, accept `request: AmbulanceReturnRequest | None = None` in `login_and_get_driver`. Build credentials with this shape:

```python
def _disinfection_credential_attempts(request: AmbulanceReturnRequest | None) -> list[tuple[DutyCredential, str]]:
    attempts: list[tuple[DutyCredential, str]] = []
    if request is not None:
        _append_disinfection_credentials(attempts, request.driver_duty_login_account_candidates, "任務司機")
        _append_disinfection_credentials(attempts, request.personnel_duty_login_account_candidates, "出勤人員")
    synced = load_synced_worker_credential()
    if synced is not None:
        attempts.append((synced, "同步帳號"))
    return _dedupe_disinfection_credentials(attempts)
```

For each `(credential, source)`, perform the current `MAX_LOGIN_ATTEMPTS` loop before moving to the next candidate. Prefix each saved failure detail with `source` and a masked account; do not include a password. If no candidate exists, keep the existing missing-sync error behavior.

Update `login_audit_for_site`, `site_login_account_summaries`, `disinfection_login_summary`, and `disinfection_login_audit` to receive the request and select the same first available source using the existing `_ppe_login_credential_choice(request)` helper.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `py -m unittest tests.test_disinfect tests.test_login_audit -v`

Expected: credential ordering, source labels, masking, and existing login-audit tests pass.

- [ ] **Step 5: Commit the credential-selection change**

```powershell
git add -- tests/test_disinfect.py tests/test_login_audit.py WinPython_公務電腦使用包/disinfect.py WinPython_公務電腦使用包/ambulance_bot/login_audit.py
git commit -m "feat: prioritize driver account for disinfection"
```

### Task 3: Run disinfection separately for each vehicle

**Files:**
- Modify: `tests/test_desktop_fast_runner.py:20-49`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/desktop_fast_runner.py:808-848`

**Interfaces:**
- Consumes: `login_disinfection_and_get_driver(request=vehicle_request, profile_name=...)` from Task 2 and `_run_per_vehicle_site`.
- Produces: one login and one dedicated profile per vehicle entry, passed only to that vehicle's `run_disinfection_task` call.

- [ ] **Step 1: Write the failing two-vehicle runner test**

```python
request = request_from_form({
    "two_vehicle": "1", "vehicle": "新坡91", "driver": "甲司機",
    "vehicle_2": "新坡92", "driver_2": "乙司機",
    "case_date": "2026-07-20", "case_time": "1000",
    "return_date": "2026-07-20", "return_time": "1100",
    "mileage": "100", "mileage_2": "200",
    "patient_summary": "男一名", "patient_summary_2": "女一名",
})
```

Patch `login_disinfection_and_get_driver` to return two unique mock drivers and patch `run_disinfection_task` to return successful `SiteAutomationResult` values. Assert login is called twice with the requests for `甲司機` then `乙司機`; assert each `run_disinfection_task` receives its matching `existing_driver` and a different profile name.

- [ ] **Step 2: Run the runner test and verify RED**

Run: `py -m unittest tests.test_desktop_fast_runner.DesktopFastRunnerTests.test_disinfection_logs_in_per_vehicle_with_its_driver -v`

Expected: failure because current code logs in once before iterating both vehicles.

- [ ] **Step 3: Move login inside the per-vehicle action**

Replace the pre-loop login with an action that logs in for its own `vehicle_request`:

```python
def run_one(vehicle_request, index):
    profile_name = f"disinfection_profile_{profile_suffix}_{index}"
    driver = login_disinfection_and_get_driver(
        request=vehicle_request,
        profile_name=profile_name,
        tile_name="disinfection",
    )
    return run_disinfection_task(
        vehicle_request, self.artifacts_dir, existing_driver=driver,
        profile_name=profile_name, use_session_lock=False, tile_name="disinfection",
        force_new_driver=True,
        update_context=self._vehicle_site_update_context(request.task_id, "disinfection", vehicle_request, index),
        cancel_check=self._cancel_check(request.task_id),
    )
```

Use `run_one(request, 1)` for a single vehicle and pass `run_one` directly to `_run_per_vehicle_site` for two vehicles. Keep preflight validation before any login.

- [ ] **Step 4: Run runner tests and verify GREEN**

Run: `py -m unittest tests.test_desktop_fast_runner -v`

Expected: all desktop runner tests pass, including the new per-vehicle login test.

- [ ] **Step 5: Commit the per-vehicle execution change**

```powershell
git add -- tests/test_desktop_fast_runner.py WinPython_公務電腦使用包/ambulance_bot/desktop_fast_runner.py
git commit -m "fix: login per vehicle for disinfection"
```

### Task 4: Verify, package, deploy, and read back

**Files:**
- Generated: `UPDATE/NAS包/**` and `UPDATE/*.zip` through package scripts only.

**Interfaces:**
- Consumes: committed source from Tasks 1-3.
- Produces: matching source/generated/live NAS templates and an updated public-duty Worker package.

- [ ] **Step 1: Run regression checks**

Run:

```powershell
py -m unittest tests.test_web_app tests.test_disinfect tests.test_login_audit tests.test_desktop_fast_runner -v
py -m unittest discover -s tests
git diff --check
```

Expected: all tests pass and `git diff --check` is empty.

- [ ] **Step 2: Build both deployment packages**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_public_duty_package.ps1
```

Expected: generated NAS package and public-duty ZIP include the changed templates and runtime source.

- [ ] **Step 3: Commit source and push the feature branch**

Run:

```powershell
git status --short
git push -u origin HEAD
```

Expected: only the task commits are pushed; generated artifacts, credentials, and runtime data remain unstaged.

- [ ] **Step 4: Deploy NAS and restart Flask**

Copy only generated `UPDATE\NAS包` contents to `\\100.114.126.58\docker\ambulance_return_bot`, preserving `.env` and unrelated NAS runtime files. Restart `ambulance-app-1` with the restricted SSH command, then check `http://100.114.126.58:8080/status`.

Expected: HTTP 200 and the generated `nas_home.html` hash equals the live NAS template hash.

- [ ] **Step 5: Publish/update the public-duty package and request Worker remote update**

Use the existing release script to publish the new package version, re-download the release ZIP and SHA256 for verification, then issue one authenticated public-PC remote-update command. Read `remote_update.json` and `worker_heartbeat.json` until the command is `completed` and the Worker reports the published version.

Expected: public-duty Worker restarts with the new version and a fresh, verified LAN heartbeat.
