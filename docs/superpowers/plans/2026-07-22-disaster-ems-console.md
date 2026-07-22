# 救災救護台改版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留既有救護登打流程的前提下，新增救災表單、N 車三站登打、救災行車紀錄器資料夾、救災車設定，以及 NAS 救災／救護後台篩選。

**Architecture:** 沿用同一個 Flask 任務中心、JSON 任務儲存與公務電腦 Worker，以 `service_type=ems|disaster` 決定表單、驗證、有效站台與摘要。救護維持現有固定一／二車介面；救災使用獨立模板和一般化 `vehicle_entries`。資料夾命名集中到單一純函式模組，讓頁面預覽與實際建立共用同一規則。

**Tech Stack:** Python 3.12、Flask/Jinja2、dataclasses、vanilla JavaScript、Selenium、`unittest`、PowerShell 建置腳本。

## Global Constraints

- 權威來源只修改 `WinPython_公務電腦使用包`；不直接修改產生的 `UPDATE\NAS包`。
- 不刪除檔案，不清除或覆蓋工作區既有未提交變更。
- 救護介面僅允許新增入口分流、NAS 資料夾預覽與加油總價。
- 救災至少一車且支援 N 車；工作紀錄一案一筆，里程與加油逐車執行。
- 工作概述沿用消防勤務系統自動帶入，不自行產生、預覽或覆寫。
- 轄內A2 顯示名稱映射到 NAS 既有 `A2`，不得建立 `轄內A2` 平行目錄。
- 既有 NAS 資料夾與影片不得改名、搬移、合併、覆寫或刪除。
- 所有新行為先寫失敗測試並確認預期失敗，再寫最小實作。

---

### Task 1: Service-aware request model and disaster vehicle settings

**Files:**
- Create: `WinPython_公務電腦使用包/ambulance_bot/disaster_settings.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/models.py:242-563,676-744`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `DISASTER_REASON_OPTIONS`, `DISASTER_ACTION_PACKAGES`, `DisasterVehicleRecord`, `load_disaster_vehicle_records()`, `save_disaster_vehicle_record()`, `delete_disaster_vehicle_record()`.
- Produces: `AmbulanceReturnRequest.service_type`, disaster work-record fields, generalized `effective_vehicle_entries()`, `active_site_keys()` and `request_from_disaster_form()`.

- [ ] **Step 1: Write failing model and settings tests**

```python
def test_disaster_form_parses_n_vehicle_entries(self):
    request = request_from_disaster_form(MultiDict([
        ("case_id", "CASE-1"), ("case_date", "2026/07/22"),
        ("case_time", "1207"), ("return_time", "1300"),
        ("case_address", "桃園市觀音區金華路31號"),
        ("vehicle", "新坡11"), ("driver", "甲"), ("mileage", "100"),
        ("vehicle", "新坡15"), ("driver", "乙"), ("mileage", "200"),
    ]))
    self.assertEqual("disaster", request.service_type)
    self.assertEqual(["新坡11", "新坡15"], [item.vehicle for item in request.vehicle_entries])
    self.assertEqual(["duty_work_log", "vehicle_mileage"], request.active_site_keys())
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `py -m unittest tests.test_models -v`

Expected: fail because disaster constants, settings and parser do not exist.

- [ ] **Step 3: Add minimal service-aware fields and disaster settings persistence**

```python
@dataclass(slots=True)
class AmbulanceReturnRequest:
    service_type: str = "ems"
    duty_item: str = ""
    summary_type: str = ""
    commander: str = ""
    action_note: str = ""
    recorder_category: str = ""
    recorder_subcategory: str = ""

def active_site_keys(self) -> list[str]:
    keys = ["duty_work_log", "vehicle_mileage"]
    if self.has_fuel_record():
        keys.append("fuel_record")
    if self.service_type == "ems":
        keys.extend(["consumables", "disinfection"])
    return keys
```

- [ ] **Step 4: Run model tests and verify GREEN**

Run: `py -m unittest tests.test_models -v`

Expected: all model tests pass, including legacy EMS parsing.

- [ ] **Step 5: Commit the task**

```powershell
git add -- tests/test_models.py 'WinPython_公務電腦使用包/ambulance_bot/models.py' 'WinPython_公務電腦使用包/ambulance_bot/disaster_settings.py'
git commit -m "feat: add disaster request model"
```

### Task 2: Shared EMS preview and disaster recorder-folder planner

**Files:**
- Create: `WinPython_公務電腦使用包/ambulance_bot/record_folders.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/desktop_fast_runner.py:40,447-453,918-933,1064-1072`
- Modify: `.env.example`
- Modify: `WinPython_公務電腦使用包/.env.example`
- Modify: `compose.nas.yml`
- Test: `tests/test_record_folders.py`
- Test: `tests/test_desktop_fast_runner.py`

**Interfaces:**
- Produces: `ems_record_relative_paths(request)`, `ensure_ems_record_folders(request, root)`.
- Produces: `disaster_folder_plan(request, root)`, `ensure_disaster_record_folders(request, root)` returning per-vehicle path/result records.
- Consumes: disaster vehicle recorder codes and request category/subcategory/reason.

- [ ] **Step 1: Write failing pure-path tests**

```python
def test_disaster_other_case_uses_roc_year_subcategory_and_each_vehicle(self):
    plan = disaster_folder_plan(request, Path("X:/records"))
    self.assertEqual(
        [
            Path("X:/records/115年/轄內其他案件/破門/202607211207桃園市觀音區金華路31號(破門)-11"),
            Path("X:/records/115年/轄內其他案件/破門/202607211207桃園市觀音區金華路31號(破門)-15"),
        ],
        [item.path for item in plan],
    )
```

- [ ] **Step 2: Verify RED**

Run: `py -m unittest tests.test_record_folders -v`

Expected: import fails because `record_folders` does not exist.

- [ ] **Step 3: Implement deterministic naming, sanitization and no-overwrite creation**

```python
def recorder_category_directory(value: str) -> str:
    return "A2" if value == "轄內A2" else value

def ensure_folder(path: Path) -> str:
    if path.exists() and not path.is_dir():
        raise RecordFolderError(f"同名物件不是資料夾：{path}")
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    return "reused" if existed else "created"
```

- [ ] **Step 4: Move existing EMS folder creation to the shared module**

Keep the actual EMS path `西元年\月份\MMDDHHmm-車號` and children `1`, `2`, `車` unchanged. The desktop runner must call the shared function used by the page preview.

- [ ] **Step 5: Configure NAS mount without hard-coding generated output**

Set `DISASTER_RECORD_ROOT=/data/disaster-records` in NAS compose and mount `/volume1/nas/搶救災害硬碟/救災行車紀錄器:/data/disaster-records`. Windows/local fallback remains `\\100.114.126.58\nas\搶救災害硬碟\救災行車紀錄器`.

- [ ] **Step 6: Verify GREEN**

Run: `py -m unittest tests.test_record_folders tests.test_desktop_fast_runner -v`

Expected: folder planner and existing EMS runner tests pass.

- [ ] **Step 7: Commit the task**

```powershell
git add -- tests/test_record_folders.py tests/test_desktop_fast_runner.py compose.nas.yml .env.example 'WinPython_公務電腦使用包/.env.example' 'WinPython_公務電腦使用包/ambulance_bot/record_folders.py' 'WinPython_公務電腦使用包/ambulance_bot/desktop_fast_runner.py'
git commit -m "feat: plan and create recorder folders"
```

### Task 3: Entry selector, EMS compatibility changes, names and fuel totals

**Files:**
- Create: `WinPython_公務電腦使用包/templates/task_entry.html`
- Modify: `WinPython_公務電腦使用包/app.py:196-288,3348-3417`
- Modify: `WinPython_公務電腦使用包/templates/new_task.html:1-719,755-1100`
- Modify: `WinPython_公務電腦使用包/templates/nas_home.html`
- Modify: `WinPython_公務電腦使用包/templates/admin_public_pc.html:1-330`
- Modify: `WinPython_公務電腦使用包/worker_gui.py`
- Modify: `WinPython_公務電腦使用包/worker.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_worker_gui.py`

**Interfaces:**
- Produces routes `/app`, `/app/ems`, `/app/disaster`.
- Consumes `ems_record_relative_paths()` for preview.

- [ ] **Step 1: Write failing route, naming and rendered-HTML tests**

Assert `/app` contains the two entry cards, `/app/ems` contains the unchanged EMS form plus recorder preview and fuel totals, and admin/GUI labels contain `SinpoSmart - 救災救護Worker`.

- [ ] **Step 2: Verify RED**

Run: `py -m unittest tests.test_web_app tests.test_worker_gui -v`

Expected: new routes, labels and preview markers are absent.

- [ ] **Step 3: Implement the selector and preserve EMS form behavior**

```python
@app.get("/app")
def task_entry():
    return render_template("task_entry.html")

@app.get("/app/ems")
def new_task():
    return render_ems_task_form()
```

Add JS-derived fuel total fields and render EMS folder names immediately above the submit button. Do not alter EMS field ordering or one/two-vehicle interaction.

- [ ] **Step 4: Verify GREEN**

Run: `py -m unittest tests.test_web_app tests.test_worker_gui -v`

Expected: focused web/GUI tests pass.

- [ ] **Step 5: Commit the task**

```powershell
git add -- tests/test_web_app.py tests/test_worker_gui.py 'WinPython_公務電腦使用包/app.py' 'WinPython_公務電腦使用包/worker.py' 'WinPython_公務電腦使用包/worker_gui.py' 'WinPython_公務電腦使用包/templates/task_entry.html' 'WinPython_公務電腦使用包/templates/new_task.html' 'WinPython_公務電腦使用包/templates/nas_home.html' 'WinPython_公務電腦使用包/templates/admin_public_pc.html'
git commit -m "feat: add disaster and EMS entry selector"
```

### Task 4: Disaster form, validation, duplicate prevention and vehicle settings UI

**Files:**
- Create: `WinPython_公務電腦使用包/templates/disaster_task.html`
- Create: `WinPython_公務電腦使用包/templates/admin_disaster_vehicles.html`
- Modify: `WinPython_公務電腦使用包/app.py:203-370,637-748,3438-3680`
- Test: `tests/test_web_app.py`

**Interfaces:**
- Produces POST `/tasks/disaster`, GET/POST `/admin/disaster-vehicles`.
- Consumes `request_from_disaster_form()`, `validate_disaster_task_form()`, `ensure_disaster_record_folders()`.

- [ ] **Step 1: Write failing form/validation tests**

Cover the approved layout order, hidden personnel, 17 reasons, N vehicle JSON form payload, commander membership, recorder category order, `轄內其他案件` subcategory, duplicate `case_id`, and failure-before-queue when a folder cannot be created.

- [ ] **Step 2: Verify RED**

Run: `py -m unittest tests.test_web_app -v`

Expected: disaster routes and template are missing.

- [ ] **Step 3: Implement the independent disaster template**

The form order is address; case/return times with now button; case type/reason; recorder classification; vehicle cards; work record. Vehicle cards are submitted as aligned repeated fields and can be added/removed with JavaScript. Processing packages append editable phrases and update the two-line preview.

- [ ] **Step 4: Implement create validation and source-case duplicate guard**

```python
def existing_disaster_task_for_case(case_id: str) -> dict | None:
    for payload in store.list_recent(limit=100000):
        task = dict(payload.get("task") or {})
        if task.get("service_type") == "disaster" and task.get("case_id") == case_id:
            return payload
    return None
```

Create/verify recorder folders before `store.create()` and queueing. Reuse existing directories; render an actionable error for any failed path.

- [ ] **Step 5: Verify GREEN**

Run: `py -m unittest tests.test_web_app -v`

Expected: disaster form, settings, validation, dedupe and folder gating tests pass; EMS web tests remain green.

- [ ] **Step 6: Commit the task**

```powershell
git add -- tests/test_web_app.py 'WinPython_公務電腦使用包/app.py' 'WinPython_公務電腦使用包/templates/disaster_task.html' 'WinPython_公務電腦使用包/templates/admin_disaster_vehicles.html'
git commit -m "feat: add disaster task entry"
```

### Task 5: Service-aware task lifecycle and NAS admin filtering

**Files:**
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_store.py:102-168,281-301,1805-1821`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/adapters.py:27-135`
- Modify: `WinPython_公務電腦使用包/app.py:647-675,2779-3093`
- Modify: `WinPython_公務電腦使用包/templates/admin_public_pc.html:317-428`
- Modify: `WinPython_公務電腦使用包/templates/task_detail.html`
- Test: `tests/test_task_store.py`
- Test: `tests/test_web_app.py`

**Interfaces:**
- Consumes `request.active_site_keys()` for initial status, completion, queue preparation and rerun rules.
- Produces combined `service=all|disaster|ems` and `result=all|success|failed` filters.

- [ ] **Step 1: Write failing lifecycle/filter tests**

Assert a disaster task completes with work-log + N mileage + enabled fuel only, inactive EMS sites cannot block it, selective rerun skips successful vehicles, and admin filters cross-compose while preserving the current card skeleton.

- [ ] **Step 2: Verify RED**

Run: `py -m unittest tests.test_task_store tests.test_web_app -v`

- [ ] **Step 3: Make status initialization and completion service-aware**

```python
def initial_site_statuses(request=None):
    active = set(request.active_site_keys()) if request else {site.key for site in SITE_DEFINITIONS}
    return {site.key: site_status(site, "not_started" if site.key in active else "not_applicable") for site in SITE_DEFINITIONS}
```

- [ ] **Step 4: Add service filter and disaster summary to the existing card skeleton**

Disaster cards show case time/address/reason/commander and each vehicle's mileage/fuel state. They retain worker/version/update/error guidance/screenshots/events used by EMS cards.

- [ ] **Step 5: Verify GREEN and commit**

Run: `py -m unittest tests.test_task_store tests.test_web_app -v`

```powershell
git add -- tests/test_task_store.py tests/test_web_app.py 'WinPython_公務電腦使用包/ambulance_bot/task_store.py' 'WinPython_公務電腦使用包/ambulance_bot/adapters.py' 'WinPython_公務電腦使用包/app.py' 'WinPython_公務電腦使用包/templates/admin_public_pc.html' 'WinPython_公務電腦使用包/templates/task_detail.html'
git commit -m "feat: make task lifecycle service aware"
```

### Task 6: Disaster Worker execution and login priority

**Files:**
- Modify: `WinPython_公務電腦使用包/ambulance_bot/login_audit.py:13-177`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/desktop_fast_runner.py:43-101,444-483,578-811`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/selenium_local.py:1120-1225,1413-1592`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_runner.py:60-90`
- Test: `tests/test_login_audit.py`
- Test: `tests/test_desktop_fast_runner.py`
- Test: `tests/test_selenium_local.py`

**Interfaces:**
- Consumes disaster `duty_status_text`, active sites and per-vehicle request expansion.
- Produces work-log login order 15 driver, 11 driver, other personnel, synced account; mileage/fuel order current driver, other personnel, synced account.

- [ ] **Step 1: Write failing priority, site-selection and work-log-fill tests**

Assert disaster runs only two or three sites, uses N vehicles, fills duty item/reason/processing without overwriting `_areDescription`, and keeps the system-supplied return line when present.

- [ ] **Step 2: Verify RED**

Run: `py -m unittest tests.test_login_audit tests.test_desktop_fast_runner tests.test_selenium_local -v`

- [ ] **Step 3: Implement disaster account order and active site groups**

```python
if request.service_type == "disaster":
    return [work_log_group, mileage_and_optional_fuel_group]
return [work_log_group, mileage_and_optional_fuel_group, consumables_group, disinfection_group]
```

- [ ] **Step 4: Branch only the work-log values**

For disaster use `request.duty_item`, `request.case_reason`, and `request.disaster_processing_text`. Keep `patchReturnLine()` unchanged so it fills only a blank system return line and never rewrites a nonblank overview.

- [ ] **Step 5: Verify GREEN and commit**

Run: `py -m unittest tests.test_login_audit tests.test_desktop_fast_runner tests.test_selenium_local -v`

```powershell
git add -- tests/test_login_audit.py tests/test_desktop_fast_runner.py tests/test_selenium_local.py 'WinPython_公務電腦使用包/ambulance_bot/login_audit.py' 'WinPython_公務電腦使用包/ambulance_bot/desktop_fast_runner.py' 'WinPython_公務電腦使用包/ambulance_bot/selenium_local.py' 'WinPython_公務電腦使用包/ambulance_bot/task_runner.py'
git commit -m "feat: execute disaster three-site tasks"
```

### Task 7: Full regression, package build and visual acceptance

**Files:**
- Modify only if tests expose a requirement gap: files already listed above.
- Verify: canonical source, tests, generated packages and rendered pages.

- [ ] **Step 1: Run syntax compilation**

Run the canonical `py -m py_compile` command from the project skill.

Expected: exit 0.

- [ ] **Step 2: Run complete regression suite**

Run: `py -m unittest discover -s tests -v`

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 3: Render and inspect the three entry pages**

Open `/app`, `/app/ems`, `/app/disaster`, then verify desktop/mobile layout, approved field order, N-vehicle controls, processing preview, recorder preview and fuel totals. Save screenshots under untracked `artifacts/` only.

- [ ] **Step 4: Build both deliverables**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1
powershell -ExecutionPolicy Bypass -File scripts\build_public_duty_package.ps1
```

Expected: both scripts exit 0; generated output remains unstaged.

- [ ] **Step 5: Verify source/generated parity and working-tree scope**

Run `git diff --check`, inspect `git status --short`, compare canonical and generated hashes for changed runtime files, and confirm no `.env`, artifact, task JSON, screenshot, generated NAS package or unrelated pre-existing change is staged.

- [ ] **Step 6: Final implementation commit**

Stage only files belonging to this feature and commit with:

```powershell
git commit -m "feat: add disaster and EMS console"
```

- [ ] **Step 7: Report operational boundary**

State explicitly whether NAS was deployed and whether the Worker was restarted. Source/build completion must not be described as live deployment unless `/status`, deployed hashes and Worker heartbeat confirm it.
