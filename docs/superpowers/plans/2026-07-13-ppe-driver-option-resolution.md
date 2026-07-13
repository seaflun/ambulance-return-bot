# PPE Driver Option Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve PPE driver IDs from exact decoded option data for fuel and vehicle-mileage entry, and report missing driver options accurately instead of as login failures.

**Architecture:** Add small pure-Python option parsing and exact-match helpers in `selenium_local.py`. Fuel entry parses valid JSON option objects from page scripts and waits for the requested driver; mileage entry reads decoded `window.driverList`, validates the requested driver ID, and passes that ID into the Kendo row update. `site_diagnostics.py` classifies these failures before generic login text.

**Tech Stack:** Python 3, Selenium, Kendo UI page JavaScript, `unittest`, PowerShell packaging scripts.

## Global Constraints

- `WinPython_公務電腦使用包` remains the public-duty runtime source of truth.
- Do not edit generated `UPDATE/NAS包` as source; rebuild it with `scripts/build_nas_package.ps1`.
- Driver names use whitespace-normalized exact matching only; no partial or substring matching.
- Empty or zero driver IDs are invalid and must stop save.
- Keep credentials, Chrome profiles, artifacts, screenshots, logs, and `.env` out of commits and release zips.
- Preserve the protected-site human-in-the-loop boundary.
- A release is not a live installation until the NAS/public-PC version report proves deployment.

---

### Task 1: Parse and match PPE driver options

**Files:**
- Modify: `WinPython_公務電腦使用包/ambulance_bot/selenium_local.py`
- Test: `tests/test_selenium_local.py`

**Interfaces:**
- Produces: `_ppe_option_records_from_script(script_text: str) -> list[dict[str, object]]`
- Produces: `_ppe_option_value(options: object, requested_name: str) -> str`
- Produces: `_ppe_option_names(options: object, limit: int = 8) -> list[str]`
- Consumes: JSON objects with name fields `Text`, `Name`, `DriverName`, `UserName`, `EmpName` and ID fields `Value`, `Id`, `UserId`, `EmpId`, `Code`, `Driver`.

- [ ] **Step 1: Write failing parser and matcher tests**

Add imports for `_ppe_option_names`, `_ppe_option_records_from_script`, and `_ppe_option_value`. Add tests that verify Unicode escapes decode, `Text/Value` order does not matter, whitespace-normalized exact matches succeed, partial names fail, zero IDs fail, and candidate names are bounded.

```python
def test_ppe_option_records_decode_unicode_driver_names(self):
    source = 'dataSource: [{"DeptSeq":null,"Value":"2448","Text":"\\u90ED\\u570B\\u5075"}]'
    options = _ppe_option_records_from_script(source)
    self.assertEqual(_ppe_option_value(options, "郭國偵"), "2448")

def test_ppe_option_value_accepts_reordered_fields_and_normalized_whitespace(self):
    options = [{"Text": " 郭  國偵 ", "DeptSeq": None, "Value": "2448"}]
    self.assertEqual(_ppe_option_value(options, "郭 國偵"), "2448")

def test_ppe_option_value_rejects_partial_name_and_zero_id(self):
    self.assertEqual(_ppe_option_value([{"Text": "郭國偵", "Value": "2448"}], "郭國"), "")
    self.assertEqual(_ppe_option_value([{"Text": "郭國偵", "Value": "0"}], "郭國偵"), "")
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `py -m unittest tests.test_selenium_local.SeleniumLocalTests.test_ppe_option_records_decode_unicode_driver_names tests.test_selenium_local.SeleniumLocalTests.test_ppe_option_value_accepts_reordered_fields_and_normalized_whitespace tests.test_selenium_local.SeleniumLocalTests.test_ppe_option_value_rejects_partial_name_and_zero_id -v`

Expected: import errors because the helpers do not exist.

- [ ] **Step 3: Implement the pure option helpers**

Use `re.finditer(r"\{[^{}]*\}", script_text)` to locate simple object literals, keep fragments containing quoted name and ID fields, and decode valid JSON with `json.loads`. Normalize names with `" ".join(str(value or "").split())`. Return the first exact valid match and reject `""`/`"0"` IDs.

```python
_PPE_OPTION_NAME_FIELDS = ("Text", "Name", "DriverName", "UserName", "EmpName")
_PPE_OPTION_ID_FIELDS = ("Value", "Id", "UserId", "EmpId", "Code", "Driver")

def _normalize_ppe_option_name(value: object) -> str:
    return " ".join(str(value or "").split())

def _ppe_option_records_from_script(script_text: str) -> list[dict[str, object]]:
    records = []
    for match in re.finditer(r"\{[^{}]*\}", str(script_text or "")):
        fragment = match.group(0)
        if not any(f'"{field}"' in fragment for field in _PPE_OPTION_NAME_FIELDS):
            continue
        if not any(f'"{field}"' in fragment for field in _PPE_OPTION_ID_FIELDS):
            continue
        try:
            record = json.loads(fragment)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records
```

- [ ] **Step 4: Run targeted tests and verify GREEN**

Run the Task 1 command again. Expected: all listed tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- 'WinPython_公務電腦使用包/ambulance_bot/selenium_local.py' 'tests/test_selenium_local.py'
git commit -m "fix: parse PPE driver option data"
```

### Task 2: Use exact driver IDs in fuel and mileage entry

**Files:**
- Modify: `WinPython_公務電腦使用包/ambulance_bot/selenium_local.py`
- Test: `tests/test_selenium_local.py`

**Interfaces:**
- Consumes: Task 1 `_ppe_option_records_from_script`, `_ppe_option_value`, and `_ppe_option_names`.
- Produces: `_wait_for_fuel_driver_value(driver, driver_name: str, timeout: float = 12) -> str`.
- Produces: `_vehicle_mileage_driver_value(driver, driver_name: str, row_index: int = 0) -> str`.
- Changes: `_fill_fuel_grid_record` receives a resolved driver ID instead of searching source text or unrelated existing rows.
- Changes: `_fill_vehicle_grid_values` resolves an exact driver ID before setting the Kendo row.

- [ ] **Step 1: Write failing fuel-resolution tests**

Create a fake driver whose script source contains `{"Value":"2448","Text":"\\u90ED\\u570B\\u5075"}` and whose grid has only a new row. Assert `_wait_for_fuel_driver_value(driver, "郭國偵", timeout=0)` returns `"2448"`. Add a missing-driver test asserting the exception names the requested driver and candidate names.

- [ ] **Step 2: Run fuel tests and verify RED**

Run:

```powershell
py -m unittest tests.test_selenium_local.SeleniumLocalTests.test_wait_for_fuel_driver_value_decodes_unicode_options tests.test_selenium_local.SeleniumLocalTests.test_wait_for_fuel_driver_value_reports_requested_and_candidates -v
```

Expected: failure because `_wait_for_fuel_driver_value` does not exist and `_fill_fuel_grid_record` still uses the old lookup.

- [ ] **Step 3: Implement condition-based fuel resolution**

Poll `document.scripts` until `_ppe_option_value` returns a valid ID or the timeout expires. On timeout raise:

```python
raise WebDriverException(
    f"missing fuel driver: requested={driver_name}; candidates={','.join(candidate_names) or 'none'}"
)
```

Pass the resolved ID as a Selenium script argument and set `Driver` directly. Remove the source-text regex and existing-row dependency from `_fill_fuel_grid_record`.

- [ ] **Step 4: Run fuel tests and verify GREEN**

Run the exact Step 2 command again. Expected: both tests pass.

- [ ] **Step 5: Write failing vehicle-mileage tests**

Add tests with a fake `window.driverList` result that verify an exact name resolves to its ID and a different existing row driver is not reused. Add a missing-driver test expecting `missing vehicle mileage driver`.

- [ ] **Step 6: Run mileage tests and verify RED**

Run:

```powershell
py -m unittest tests.test_selenium_local.SeleniumLocalTests.test_vehicle_mileage_driver_value_uses_exact_option tests.test_selenium_local.SeleniumLocalTests.test_vehicle_mileage_driver_value_rejects_different_existing_row -v
```

Expected: failures because current code uses `JSON.stringify(...).includes(...)` and does not fail closed on missing IDs.

- [ ] **Step 7: Implement exact mileage driver resolution**

Read a context object containing `window.driverList`, the selected row's `Driver`, and `DriverName`. Resolve with `_ppe_option_value`; allow the row ID only when normalized row name exactly equals the requested name and the ID is valid. Pass the ID into the fill script and include `Driver` in validation.

- [ ] **Step 8: Run mileage and selenium tests**

Run: `py -m unittest tests.test_selenium_local -v`

Expected: all Selenium-local tests pass.

- [ ] **Step 9: Commit Task 2**

```powershell
git add -- 'WinPython_公務電腦使用包/ambulance_bot/selenium_local.py' 'tests/test_selenium_local.py'
git commit -m "fix: resolve PPE driver IDs before vehicle entry"
```

### Task 3: Correct backend diagnostics for driver lookup failures

**Files:**
- Modify: `WinPython_公務電腦使用包/ambulance_bot/site_diagnostics.py`
- Test: `tests/test_site_diagnostics.py`

**Interfaces:**
- Produces diagnostic category `ppe_driver`.
- Consumes failure phrases `missing driver`, `missing fuel driver`, and `missing vehicle mileage driver`.

- [ ] **Step 1: Write failing diagnostic tests**

Add one fuel case containing the real login-audit prefix plus `missing driver`, and one mileage case containing `missing vehicle mileage driver`. Assert `exception_type == "ppe_driver"`, the stage is the site's fill stage, the reason mentions the driver list, and the reason is not a login failure.

- [ ] **Step 2: Run diagnostic tests and verify RED**

Run: `py -m unittest tests.test_site_diagnostics.SiteDiagnosticsTests.test_fuel_missing_driver_is_not_login_failure tests.test_site_diagnostics.SiteDiagnosticsTests.test_mileage_missing_driver_stops_at_fill_stage -v`

Expected: current classifier reports `login`.

- [ ] **Step 3: Implement `ppe_driver` classification**

Classify the driver phrases before generic login detection. Return the fill stage per site, reason `PPE 駕駛清單找不到指定人員或有效代碼。`, and next action instructing the operator to verify the PPE personnel list and retry only that site.

- [ ] **Step 4: Run diagnostic and targeted suites**

Run:

```powershell
py -m unittest tests.test_site_diagnostics -v
py -m unittest tests.test_selenium_local -v
```

Expected: both suites pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add -- 'WinPython_公務電腦使用包/ambulance_bot/site_diagnostics.py' 'tests/test_site_diagnostics.py'
git commit -m "fix: explain PPE driver lookup failures"
```

### Task 4: Verify, version, package, publish, and read back

**Files:**
- Modify: `WinPython_公務電腦使用包/VERSION.txt`
- Generated, not committed: `UPDATE/NAS包`, `UPDATE/ambulance-return-public-package.zip`, version and checksum assets.

**Interfaces:**
- Consumes all earlier tasks.
- Produces a GitHub release and parity evidence.

- [ ] **Step 1: Run compile and full test verification**

```powershell
$files = @('app.py','worker.py','worker_gui.py','consumables_login.py','disinfect.py','_runtime_loader.py') + (Get-ChildItem 'WinPython_公務電腦使用包/ambulance_bot' -Filter *.py | ForEach-Object FullName)
py -m py_compile @files
py -m unittest discover -s tests -v
git diff --check
```

Expected: compile succeeds, all tests pass, and `git diff --check` is empty.

- [ ] **Step 2: Update the package version and rebuild both deliverables**

Set `WinPython_公務電腦使用包/VERSION.txt` to the current minute, then run:

```powershell
$version = Get-Date -Format 'yyyy.MM.dd.HHmm'
[IO.File]::WriteAllText((Resolve-Path 'WinPython_公務電腦使用包/VERSION.txt'), "$version`r`n", [Text.UTF8Encoding]::new($false))
powershell -ExecutionPolicy Bypass -File scripts/build_nas_package.ps1
powershell -ExecutionPolicy Bypass -File scripts/build_public_duty_package.ps1
```

- [ ] **Step 3: Re-run full verification after packaging**

Run the Step 1 commands again. Expected: all pass.

- [ ] **Step 4: Commit and push release source**

```powershell
git add -- 'WinPython_公務電腦使用包/VERSION.txt'
git commit -m "release: ambulance worker $version"
git push origin HEAD
```

- [ ] **Step 5: Publish the release**

Run: `powershell -ExecutionPolicy Bypass -File scripts/publish_ambulance_return_release.ps1`

- [ ] **Step 6: Verify remote parity**

Download the release version file, zip, and published checksum. Confirm the remote version equals the zip-internal `VERSION.txt`, and the downloaded zip SHA256 equals the published SHA256.

- [ ] **Step 7: Verify live runtime boundaries**

Read `http://100.114.126.58:8080/status` and `http://100.114.126.58:8080/admin/public-pc`. Report NAS version, public-PC reported version, whether the Worker was restarted, and whether the new release is merely published or actually installed.
