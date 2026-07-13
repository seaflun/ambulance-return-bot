# Multi-Patient Consumables Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect same-incident, same-vehicle TEMSISID patient pages and distribute each vehicle's consumables across every page so all pages are saved and confirmed.

**Architecture:** Keep the feature in the public-duty runtime source of truth. Extend consumable candidate selection to return an ordered patient-page group, add a pure deterministic allocation helper, then make the existing page writer iterate and verify each allocation. Preserve the existing per-vehicle runner boundary so two-vehicle incidents are partitioned by vehicle before patient-page distribution.

**Tech Stack:** Python 3.11+, Selenium, Flask/Jinja diagnostics, `unittest`, PowerShell package scripts.

## Global Constraints

- `WinPython_公務電腦使用包` is the runtime source of truth; do not edit generated `UPDATE\NAS包` as source.
- Candidate processing order is case evidence, actual vehicle, TEMSISID body, then numeric patient suffix.
- Different vehicles never share consumables, even when their TEMSISID body is the same.
- Normal distribution preserves each vehicle's original quantities.
- Only an allocation that would otherwise be empty may receive `桃-9吋手套-L(雙) ×1`.
- Every selected page must be saved and read back successfully before the consumables site reports success.
- Single-patient behavior stays unchanged.
- Do not stage `.env`, generated task data, screenshots, browser profiles, `.codex/`, `NAS包(舊版)/`, or unrelated untracked files.

---

## File Map

- Modify `WinPython_公務電腦使用包/consumables_login.py`: patient-page discovery, vehicle extraction, allocation, iterative page writes, detailed result text.
- Modify `WinPython_公務電腦使用包/ambulance_bot/site_diagnostics.py`: classify multi-patient failures accurately.
- Modify `tests/test_consumables_login.py`: focused unit and integration-style fake-driver coverage.
- Modify `tests/test_site_diagnostics.py`: backend guidance regression coverage.
- Modify `WinPython_公務電腦使用包/VERSION.txt` only through the public package build script.
- Generate `UPDATE/NAS包` and release assets through existing scripts; do not commit generated outputs.

---

### Task 1: Discover and Partition Patient Pages

**Files:**
- Modify: `WinPython_公務電腦使用包/consumables_login.py:289-489`
- Test: `tests/test_consumables_login.py`

**Interfaces:**
- Consumes: existing `_emm_temsis_id_from_href()`, `_vehicle_match_tokens()`, case scoring, Selenium driver navigation.
- Produces: `_patient_sid_parts(sid: str) -> tuple[str, str]`, `_consumable_detail_page_text(driver) -> str`, `_find_consumable_detail_hrefs(driver, request) -> list[str]`.
- Preserves: `_find_consumable_detail_href(driver, request) -> str` as a compatibility wrapper returning the first selected URL.

- [ ] **Step 1: Write failing patient-group tests**

Add imports for `_find_consumable_detail_hrefs`, `_patient_sid_parts`, and `_consumable_detail_vehicle_label`, then add tests that assert:

```python
def test_patient_sid_parts_uses_last_two_digits(self):
    self.assertEqual(
        _patient_sid_parts("2026071310100308031901"),
        ("20260713101003080319", "01"),
    )
    with self.assertRaisesRegex(RuntimeError, "TEMSISID.*患者序號"):
        _patient_sid_parts("20260713101003080319AA")

def test_consumable_detail_returns_all_same_vehicle_patient_pages(self):
    driver = PatientCandidateDriver(
        candidates=[
            patient_candidate("2026071310100308031901", "2026/07/13 08:05:05 桃園市中壢區月桃路一段和月山路的交叉路口"),
            patient_candidate("2026071310100308031902", "2026/07/13 08:05:05 桃園市中壢區月桃路一段和月山路的交叉路口"),
        ],
        page_text={
            "01": "出勤單位 新坡93 BSL-9230",
            "02": "出勤單位 新坡93 BSL-9230",
        },
    )
    request = patient_request(vehicle="新坡93")
    with patch("consumables_login.WebDriverWait", FakeWait), patch("consumables_login.time.sleep"):
        hrefs = _find_consumable_detail_hrefs(driver, request)
    self.assertEqual([_emm_temsis_id_from_href(href)[-2:] for href in hrefs], ["01", "02"])

def test_consumable_detail_partitions_two_vehicles_before_patients(self):
    driver = PatientCandidateDriver(
        candidates=patient_candidates("01", "02", "03", "04", "05"),
        page_text={
            "01": "出勤單位 新坡92 BXB-7593",
            "02": "出勤單位 新坡92 BXB-7593",
            "03": "出勤單位 新坡93 BSL-9230",
            "04": "出勤單位 新坡93 BSL-9230",
            "05": "出勤單位 新坡93 BSL-9230",
        },
    )
    with patch("consumables_login.WebDriverWait", FakeWait), patch("consumables_login.time.sleep"):
        hrefs_92 = _find_consumable_detail_hrefs(driver, patient_request(vehicle="新坡92"))
        hrefs_93 = _find_consumable_detail_hrefs(driver, patient_request(vehicle="新坡93"))
    self.assertEqual([_emm_temsis_id_from_href(value)[-2:] for value in hrefs_92], ["01", "02"])
    self.assertEqual([_emm_temsis_id_from_href(value)[-2:] for value in hrefs_93], ["03", "04", "05"])
```

Define the small test helpers in `tests/test_consumables_login.py`; the fake driver must return candidate dictionaries for the list-page JavaScript, navigate by URL, and return the configured detail text for both body and form-control extraction.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
py -m unittest tests.test_consumables_login.ConsumablesLoginTests.test_patient_sid_parts_uses_last_two_digits tests.test_consumables_login.ConsumablesLoginTests.test_consumable_detail_returns_all_same_vehicle_patient_pages tests.test_consumables_login.ConsumablesLoginTests.test_consumable_detail_partitions_two_vehicles_before_patients -v
```

Expected: import errors or missing-function failures for the three new interfaces.

- [ ] **Step 3: Implement ordered same-vehicle group selection**

Add a strict suffix parser:

```python
def _patient_sid_parts(sid: str) -> tuple[str, str]:
    value = str(sid or "").strip()
    if len(value) < 3 or not value[-2:].isdigit():
        raise RuntimeError(f"TEMSISID 無法辨識患者序號：{value or '空白'}")
    return value[:-2], value[-2:]
```

Expand vehicle extraction so disabled inputs and selected options are included:

```python
def _consumable_detail_page_text(driver: webdriver.Chrome) -> str:
    parts: list[str] = []
    try:
        parts.append(driver.find_element(By.TAG_NAME, "body").text)
    except Exception:
        pass
    try:
        controls = driver.execute_script(
            """
            return Array.from(document.querySelectorAll('input, select, textarea')).flatMap((node) => {
                const values = [node.value || ''];
                if (node.tagName === 'SELECT' && node.selectedIndex >= 0) {
                    values.push(node.options[node.selectedIndex].text || '');
                }
                return values;
            }).join(' ');
            """
        )
        parts.append(str(controls or ""))
    except Exception:
        pass
    return " ".join(part for part in parts if part)
```

Change `_consumable_detail_vehicle_label()` and detail-page matching to use this combined text. Extract the existing candidate scoring loop into `_scored_consumable_candidates()` returning `(score, href, text, sid)` tuples. Implement `_find_consumable_detail_hrefs()` with these rules:

1. Keep only existing strong case-evidence candidates.
2. Prefer vehicle tokens already visible in list rows; otherwise inspect every candidate detail page.
3. For multiple matches, parse patient suffixes and group by SID body.
4. Choose the single group with the highest candidate score; equal best scores across multiple SID bodies raise `RuntimeError("同案耗材存在多組無法唯一辨識的 TEMSISID")`.
5. Sort the selected group by numeric suffix.
6. If there is exactly one scored candidate, preserve the existing single-candidate fallback.
7. Make `_find_consumable_detail_href()` return `_find_consumable_detail_hrefs(...)[0]`.

- [ ] **Step 4: Run consumable selection tests and verify GREEN**

Run:

```powershell
py -m unittest tests.test_consumables_login -v
```

Expected: all legacy and new selection tests pass.

- [ ] **Step 5: Commit candidate discovery**

```powershell
git add -- 'WinPython_公務電腦使用包/consumables_login.py' 'tests/test_consumables_login.py'
git commit -m "feat: detect multi-patient consumable pages"
```

---

### Task 2: Allocate and Save Every Patient Page

**Files:**
- Modify: `WinPython_公務電腦使用包/consumables_login.py:84-111`
- Test: `tests/test_consumables_login.py`

**Interfaces:**
- Consumes: `_find_consumable_detail_hrefs()`, `AmbulanceReturnRequest`, existing clear/inject/save/readback functions.
- Produces: `_distribute_consumables(consumables: dict[str, int], page_count: int) -> list[dict[str, int]]`, `_write_current_consumable_page(driver, wait, request) -> str`.

- [ ] **Step 1: Write failing allocation tests**

```python
def test_distribute_consumables_splits_remainder_to_lower_suffix(self):
    self.assertEqual(
        _distribute_consumables({"桃-9吋手套-L(雙)": 3, "桃-口罩(片)": 3}, 2),
        [
            {"桃-9吋手套-L(雙)": 2, "桃-口罩(片)": 2},
            {"桃-9吋手套-L(雙)": 1, "桃-口罩(片)": 1},
        ],
    )

def test_distribute_consumables_adds_one_glove_only_to_empty_pages(self):
    self.assertEqual(
        _distribute_consumables({"桃-口罩(片)": 2}, 5),
        [
            {"桃-口罩(片)": 1},
            {"桃-口罩(片)": 1},
            {"桃-9吋手套-L(雙)": 1},
            {"桃-9吋手套-L(雙)": 1},
            {"桃-9吋手套-L(雙)": 1},
        ],
    )
```

Add an integration-style fake-driver test that patches `_find_consumable_detail_hrefs()` to return `01` and `02`, patches the current-page writer to record each request's consumables, and asserts both allocations are written in suffix order and the returned message mentions both suffixes.

- [ ] **Step 2: Run allocation tests and verify RED**

Run:

```powershell
py -m unittest tests.test_consumables_login.ConsumablesLoginTests.test_distribute_consumables_splits_remainder_to_lower_suffix tests.test_consumables_login.ConsumablesLoginTests.test_distribute_consumables_adds_one_glove_only_to_empty_pages -v
```

Expected: `_distribute_consumables` is missing.

- [ ] **Step 3: Implement deterministic allocation**

```python
SUPPLEMENTAL_GLOVE_NAME = "桃-9吋手套-L(雙)"


def _distribute_consumables(consumables: dict[str, int], page_count: int) -> list[dict[str, int]]:
    if page_count < 1:
        raise ValueError("page_count must be at least 1")
    allocations = [dict() for _ in range(page_count)]
    for name, raw_quantity in consumables.items():
        quantity = int(raw_quantity or 0)
        if not name or quantity <= 0:
            continue
        base, remainder = divmod(quantity, page_count)
        for index in range(page_count):
            assigned = base + (1 if index < remainder else 0)
            if assigned > 0:
                allocations[index][name] = assigned
    for allocation in allocations:
        if not allocation:
            allocation[SUPPLEMENTAL_GLOVE_NAME] = 1
    return allocations
```

- [ ] **Step 4: Refactor the writer to iterate patient pages**

Import `replace` from `dataclasses`. Extract the existing current-page clear/fill/save/readback block into `_write_current_consumable_page()`. Update `open_consumable_record_for_task()` to:

1. Open the maintenance list and obtain ordered hrefs.
2. Reject multi-page operation when `SAVE_CONSUMABLES_RECORD` is disabled because navigating to the next page would discard an unsubmitted prior page.
3. Compute one allocation per href.
4. Navigate to each href, wait for the detail page, confirm its actual vehicle equals the request vehicle, then call the current-page writer with `replace(request, consumables=allocation)`.
5. Track completed suffixes and page summaries.
6. On failure, raise a message beginning `同案多患者耗材分配／確認失敗` and include successful suffixes, failed suffix, and the original exception.
7. For one href, preserve the existing success wording.
8. For multiple hrefs, return wording such as `辨識新坡93同案2位患者；01填入5件、02填入5件，兩頁均已儲存確認。` and append an explicit note for every supplemental glove page.

- [ ] **Step 5: Run all consumable tests and verify GREEN**

```powershell
py -m unittest tests.test_consumables_login tests.test_desktop_fast_runner -v
```

Expected: all tests pass, including the existing one-vehicle and two-vehicle runner cases.

- [ ] **Step 6: Commit distribution and page writes**

```powershell
git add -- 'WinPython_公務電腦使用包/consumables_login.py' 'tests/test_consumables_login.py'
git commit -m "feat: distribute consumables across patient pages"
```

---

### Task 3: Report Accurate Multi-Patient Diagnostics

**Files:**
- Modify: `WinPython_公務電腦使用包/ambulance_bot/site_diagnostics.py:113-286`
- Test: `tests/test_site_diagnostics.py`

**Interfaces:**
- Consumes: failure prefix emitted by Task 2.
- Produces: diagnostic category `multi_patient_consumables` with a consumable-specific stage, reason, and next action.

- [ ] **Step 1: Write the failing diagnostic test**

```python
def test_multi_patient_consumables_failure_is_not_button_error(self):
    payload = diagnostic_payload(
        "consumables",
        "consumables_failed",
        "同案多患者耗材分配／確認失敗：成功=01；失敗=02；耗材儲存後讀回不一致",
    )
    self.assertEqual(payload["failure_stage"], "同案多患者耗材確認")
    self.assertEqual(payload["exception_type"], "multi_patient_consumables")
    self.assertIn("多患者", payload["failure_reason"])
    self.assertIn("患者序號", payload["next_action"])
    self.assertNotIn("按鈕", payload["failure_reason"])
```

- [ ] **Step 2: Run the test and verify RED**

```powershell
py -m unittest tests.test_site_diagnostics.SiteDiagnosticsTests.test_multi_patient_consumables_failure_is_not_button_error -v
```

Expected: current result is categorized as validation or element missing instead of `multi_patient_consumables`.

- [ ] **Step 3: Add the specific diagnostic category before generic matching**

Add this check before case, vehicle, element, validation, and save matching:

```python
if "同案多患者耗材分配／確認失敗" in raw_detail:
    return "multi_patient_consumables"
```

Add mappings:

```python
if category == "multi_patient_consumables":
    return "同案多患者耗材確認"
```

```python
"multi_patient_consumables": "同案多患者耗材頁的辨識、分配、儲存或讀回確認未全部完成。",
```

```python
if category == "multi_patient_consumables":
    return "依患者序號查看成功與失敗頁面；修正一站通資料後可單獨重跑耗材。"
```

- [ ] **Step 4: Run diagnostics and web tests**

```powershell
py -m unittest tests.test_site_diagnostics tests.test_web_app -v
```

Expected: all tests pass and the backend-rendered diagnostic no longer says the multi-patient error is a button mismatch.

- [ ] **Step 5: Commit diagnostics**

```powershell
git add -- 'WinPython_公務電腦使用包/ambulance_bot/site_diagnostics.py' 'tests/test_site_diagnostics.py'
git commit -m "fix: explain multi-patient consumable failures"
```

---

### Task 4: Verify, Package, Publish, and Read Back

**Files:**
- Generated: `UPDATE/NAS包/*`
- Generated: `UPDATE/ambulance-return-public-package.zip`
- Generated: `UPDATE/ambulance-return-version.txt`
- Generated: `UPDATE/ambulance-return-public-package.zip.sha256.txt`

**Interfaces:**
- Consumes: all source and tests from Tasks 1-3.
- Produces: verified source commit, generated NAS bundle, public-duty package, GitHub release, and remote parity evidence.

- [ ] **Step 1: Run focused and full verification**

```powershell
py -m unittest tests.test_consumables_login tests.test_site_diagnostics tests.test_desktop_fast_runner tests.test_worker tests.test_web_app -v
$files = @(
  'app.py', 'worker.py', 'worker_gui.py', 'consumables_login.py', 'disinfect.py', '_runtime_loader.py'
) + (Get-ChildItem -Path 'WinPython_公務電腦使用包\ambulance_bot' -Filter *.py | ForEach-Object { $_.FullName })
py -m py_compile @files
py -m unittest discover -s tests -v
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 2: Build both deployment outputs with one version**

```powershell
$version = Get-Date -Format 'yyyy.MM.dd.HHmm'
powershell -ExecutionPolicy Bypass -File scripts\build_public_duty_package.ps1 -Version $version
powershell -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1
```

Expected: public package, version asset, SHA file, and `UPDATE\NAS包` are regenerated; source and NAS `VERSION.txt` equal `$version`.

- [ ] **Step 3: Commit source version changes and push**

```powershell
git status --short
git add -- 'WinPython_公務電腦使用包/VERSION.txt'
git commit -m "release: ambulance worker $version"
git push origin HEAD
```

Expected: only the intended source version file is added to the release commit; generated assets and unrelated untracked files remain unstaged.

- [ ] **Step 4: Publish the GitHub release**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\publish_ambulance_return_release.ps1 -Version $version
```

Expected: tag `ambulance-return-$version` is created against the pushed commit with all required assets.

- [ ] **Step 5: Verify release parity**

Download the release version asset, zip, and SHA file into a repo-local `tmp/release-readback-$version` directory. Verify:

```powershell
$remoteVersion = (Get-Content -Raw -Encoding UTF8 "tmp\release-readback-$version\ambulance-return-version.txt").Trim().TrimStart([char]0xFEFF)
$calculated = (Get-FileHash "tmp\release-readback-$version\ambulance-return-public-package.zip" -Algorithm SHA256).Hash.ToLowerInvariant()
```

Expand the downloaded zip, read its internal `VERSION.txt`, parse the published SHA file, and assert all three values equal `$version` and `$calculated`.

- [ ] **Step 6: Prepare NAS deployment and report the runtime boundary**

If the established NAS deployment account can write `/docker/ambulance_return_bot/`, copy the generated `UPDATE\NAS包` contents without replacing the NAS `.env`, restart `ambulance-app-1`, then verify `http://100.114.126.58:8080/status` reports `$version`. If the available `codex_restart` account remains restart-only, stop before copying and report that the NAS bundle is built but requires deployment authority; do not pretend the NAS runtime changed.

- [ ] **Step 7: Verify the public-duty update boundary**

Confirm the release is available to the worker updater. If the public-duty PC has not yet installed it, report the published version separately from the currently running worker version. After installation, verify a single-patient case and a same-vehicle multi-patient case; for a two-vehicle test, confirm each vehicle's patient-page allocation remains isolated.
