# Ambulance Admin Retention and Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every rescue backend case for seven days, add reliable result filters, support the salvaged-body category, improve entry details and NAS navigation, and make the public-PC updater close without a keypress.

**Architecture:** Keep the public-duty runtime package as the source of truth. Harden the existing JSON report store with locked read-modify-write, backup recovery, and time-based retention; keep UI changes within the existing Flask routes and Jinja templates. Rebuild generated NAS and public-duty outputs after source tests pass.

**Tech Stack:** Python 3.12, Flask, Jinja2, Selenium JavaScript extraction, PowerShell/batch packaging, `unittest`.

## Global Constraints

- Do not delete user files.
- Edit `WinPython_公務電腦使用包` as source; generate `UPDATE\NAS包` with the build script.
- All public-PC report statuses are retained for seven days.
- `其他-打撈浮屍` writes duty item `救護` and reason `溺水`.
- NAS `/app` shows the home link; local `/app` does not.
- Updater success and failure paths must not wait for input.
- Preserve existing formatting and make only request-scoped changes.

---

### Task 1: Harden rescue backend report persistence and filtering

**Files:**
- Modify: `WinPython_公務電腦使用包/app.py:76-77, 442-445, 794-873`
- Modify: `WinPython_公務電腦使用包/templates/admin_public_pc.html:69-210, 229-315`
- Test: `tests/test_web_app.py`

**Interfaces:**
- Produces: `public_pc_report_backup_file() -> Path`, `public_pc_reports(now: datetime | None = None) -> list[dict]`, `public_pc_report_result(report: dict) -> str`, and route query `result=success|failed`.
- Consumes: existing `effective_task_status()` and `status_class()`.

- [ ] **Step 1: Write failing persistence tests**

Add tests that write one report at eight days old and two reports within seven days, assert only the two recent reports are returned, and assert 101 recent reports are all retained. Add a corrupt-main/valid-backup test and a concurrent-upsert test proving unique task IDs are not lost.

- [ ] **Step 2: Verify persistence tests fail**

Run: `py -m unittest tests.test_web_app.WebAppTests.test_public_pc_reports_keep_all_statuses_for_seven_days tests.test_web_app.WebAppTests.test_public_pc_reports_do_not_truncate_recent_history tests.test_web_app.WebAppTests.test_public_pc_reports_recover_from_backup tests.test_web_app.WebAppTests.test_public_pc_report_concurrent_upserts_keep_all_tasks -v`

Expected: FAIL because seven-day pruning, backup recovery, and serialized writes do not exist and recent history is capped at 100.

- [ ] **Step 3: Implement locked seven-day storage**

Add a report lock, retention constant, backup-path helper, strict payload reader, timestamp filter, and locked upsert. Remove `reports[:100]`. Write the same valid payload atomically to the main and backup files. If an existing main and backup are both unreadable, raise instead of returning an empty mutable history.

- [ ] **Step 4: Verify persistence tests pass**

Run the four tests from Step 2. Expected: PASS.

- [ ] **Step 5: Write failing filter tests**

Create complete, partially failed, and running reports. Assert `/admin/public-pc?result=success` shows only complete, `?result=failed` shows only partially failed, and the unfiltered page shows all three plus filter controls and counts.

- [ ] **Step 6: Verify filter tests fail**

Run: `py -m unittest tests.test_web_app.WebAppTests.test_admin_public_pc_filters_success_and_failed_reports -v`

Expected: FAIL because the route ignores `result` and the template has no filters.

- [ ] **Step 7: Implement server-side filters**

Classify each report through `status_class(effective_task_status(report))`; map `complete` to `success`, `failed` to `failed`, and other states to `other`. Pass filtered reports, active filter, and counts to the template. Add links for all, success, and failed above the list.

- [ ] **Step 8: Run web tests**

Run: `py -m unittest tests.test_web_app -v`. Expected: PASS.

### Task 2: Add NAS-only home navigation

**Files:**
- Modify: `WinPython_公務電腦使用包/app.py:2172-2224`
- Modify: `WinPython_公務電腦使用包/templates/new_task.html:342-349`
- Test: `tests/test_web_app.py:582-594`

**Interfaces:**
- Produces: `show_nas_home_button() -> bool` available to Jinja.
- Consumes: `request_is_local_host()`.

- [ ] **Step 1: Write failing local/NAS rendering assertions**

Assert a NAS-host `/app` response contains `<a ... href="/">返回首頁</a>` and a localhost response does not contain that link.

- [ ] **Step 2: Verify the test fails**

Run: `py -m unittest tests.test_web_app.WebAppTests.test_nas_app_page_shows_home_button_only_on_nas -v`. Expected: FAIL because NAS `/app` has no home link.

- [ ] **Step 3: Add the helper and conditional link**

Return `not request_is_local_host()` from `show_nas_home_button`; register it in `template_helpers`; render the home link in the existing header actions only when true.

- [ ] **Step 4: Run targeted web test**

Run the test from Step 2. Expected: PASS.

### Task 3: Import `其他-打撈浮屍` as rescue/drowning

**Files:**
- Modify: `WinPython_公務電腦使用包/ambulance_bot/selenium_local.py:3142-3171`
- Modify: `WinPython_公務電腦使用包/app.py:2283-2288`
- Test: `tests/test_selenium_local.py:1348-1360`
- Test: `tests/test_web_app.py:425-559`

**Interfaces:**
- Produces: extracted row `{category: "其他-打撈浮屍", reason: "溺水"}`.
- Consumes: existing duty form behavior that always selects `救護` and fills `request.case_reason`.

- [ ] **Step 1: Write failing extraction and form tests**

Assert the extraction JavaScript includes `其他-打撈浮屍` and maps it to `溺水`. Import such a case with four personnel and assert the page selects `溺水` and offers two-vehicle entry.

- [ ] **Step 2: Verify the tests fail**

Run: `py -m unittest tests.test_selenium_local.SeleniumLocalTests.test_extract_emergency_cases_includes_salvaged_body_as_drowning tests.test_web_app.WebAppTests.test_imported_salvaged_body_case_is_treated_as_ambulance_drowning -v`.

Expected: FAIL because the category is filtered out and is not considered an ambulance case.

- [ ] **Step 3: Extend extraction and classification**

Include rows containing `其他-打撈浮屍`, select that cell as the category, map its reason to `溺水`, and allow `selected_case_is_ambulance_case()` to recognize the category.

- [ ] **Step 4: Run Selenium and web targeted tests**

Run the two tests from Step 2 plus `py -m unittest tests.test_selenium_local.SeleniumLocalTests.test_extract_emergency_cases_includes_fire_cases -v`. Expected: PASS.

### Task 4: Correct rescue backend detail label and time order

**Files:**
- Modify: `WinPython_公務電腦使用包/app.py:1841-1898`
- Modify: `WinPython_公務電腦使用包/templates/admin_public_pc.html:248-256`
- Test: `tests/test_web_app.py:846-988`

**Interfaces:**
- Extends each `task_vehicle_display_entries()` item with `case_time`.
- Keeps multiple labels `1車`, `2車`; changes the single label to `登打明細`.

- [ ] **Step 1: Write failing single/two-vehicle display tests**

Assert a single vehicle shows `登打明細` and the sequence `車輛 / 傷病患 / 出勤 0830 / 返隊 0910 / 司機`. Assert both vehicle rows use the common case time and their own return times.

- [ ] **Step 2: Verify the display tests fail**

Run: `py -m unittest tests.test_web_app.WebAppTests.test_admin_public_pc_receives_and_lists_local_task_events tests.test_web_app.WebAppTests.test_admin_public_pc_shows_two_vehicle_task_entries -v`. Expected: FAIL on the new assertions.

- [ ] **Step 3: Extend display data and template order**

Populate normalized `case_time` from the vehicle entry or parent task; set the single label to `登打明細`; insert labeled departure and return times immediately before driver.

- [ ] **Step 4: Run display tests**

Run the tests from Step 2. Expected: PASS.

### Task 5: Remove updater keypress waits

**Files:**
- Modify: `scripts/build_public_duty_package.ps1:471-519`
- Modify: `WinPython_公務電腦使用包/UPDATE_PACKAGE.bat:1-47`
- Test: `tests/test_worker_gui.py:506-516`

**Interfaces:**
- Preserves existing updater exit codes and self-repair flow.
- Produces no `pause` command in `UPDATE_PACKAGE.bat`.

- [ ] **Step 1: Strengthen the failing launcher test**

Assert the entire launcher contains no line matching `pause`, while retaining minimized startup, self-repair, and nonzero exits.

- [ ] **Step 2: Verify the test fails**

Run: `py -m unittest tests.test_worker_gui.WorkerGuiEnvTests.test_update_launcher_self_repairs_parse_broken_updater -v`. Expected: FAIL because three `pause` commands remain.

- [ ] **Step 3: Remove pause from source and generated launcher**

Delete only the three `pause` lines from the PowerShell here-string and tracked package batch file. Preserve indentation and all `exit /b` lines.

- [ ] **Step 4: Run GUI tests**

Run: `py -m unittest tests.test_worker_gui -v`. Expected: PASS.

### Task 6: Verify, package, deploy, and publish

**Files:**
- Generated: `UPDATE/NAS包/**`
- Generated: `UPDATE/ambulance-return-version.txt`
- Generated: `UPDATE/ambulance-return-public-package.zip`
- Generated: `UPDATE/ambulance-return-public-package.zip.sha256.txt`

**Interfaces:**
- Produces a live NAS runtime and a GitHub public-duty update release with matching versions and hashes.

- [ ] **Step 1: Compile and run the complete suite**

Run the repository compile command from `AGENTS.md`, then `py -m unittest discover -s tests -v`. Expected: 0 failures.

- [ ] **Step 2: Check diff integrity**

Run: `git diff --check` and `git status --short`. Expected: no whitespace errors and no secrets/artifacts staged.

- [ ] **Step 3: Build both outputs**

Run: `powershell -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1` and `powershell -ExecutionPolicy Bypass -File scripts\build_public_duty_package.ps1`. Expected: both scripts exit 0 with a new shared version.

- [ ] **Step 4: Verify generated content**

Assert source and `UPDATE\NAS包` hashes match for `app.py`, changed templates, and changed Python modules. Inspect the zip to confirm its `VERSION.txt`, no-pause launcher, salvaged-body mapping, and seven-day backend code.

- [ ] **Step 5: Commit and push intentionally**

Stage only the changed source, tests, design, and plan files. Commit with a request-scoped message and push `origin master`. Do not stage `.env`, artifacts, tmp, old NAS package, or generated release outputs.

- [ ] **Step 6: Deploy and restart NAS**

Copy generated NAS files over the live NAS project without deleting or mirroring `artifacts`; restart `ambulance-app-1` through the restricted SSH command; wait for healthy `/status`.

- [ ] **Step 7: Verify live NAS behavior**

Check source/NAS/live hashes, `/status` version, NAS `/app` home link, rescue backend filter links, and that the existing live report plus backup remain present.

- [ ] **Step 8: Publish and read back GitHub release**

Run `scripts\publish_ambulance_return_release.ps1`. Download latest and direct-tag version, zip, and SHA files to a temporary directory. Assert remote version, zip internal version, local SHA, latest SHA, and direct-tag SHA all match.

