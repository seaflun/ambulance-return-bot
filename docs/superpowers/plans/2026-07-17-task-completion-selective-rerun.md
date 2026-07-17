# Task Completion and Selective Rerun Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make local App, NAS admin, and Worker use one atomic four/five-site completion result, hide all entry buttons only when every active site is complete, and rerun only the sites and vehicles affected by later edits.

**Architecture:** Put the authoritative completion snapshot and completion reconciliation in `ambulance_bot.task_store`. Put normalized edit comparison in a focused `ambulance_bot.task_edit_impact` module, persist only the pending field/site/vehicle summary, and retain existing per-site `update_context` for Selenium updates. Local runners, Worker code, App pages, and NAS reports consume those shared functions instead of inferring task completion from the last operation.

**Tech Stack:** Python 3, Flask/Jinja, JSON task store guarded by `threading.RLock`, `unittest`, PowerShell package scripts, Tkinter Worker GUI.

## Global Constraints

- Work from `I:\我的雲端硬碟\專案\救護返隊小幫手\ambulance_return_bot`.
- `WinPython_公務電腦使用包` is the runtime source of truth. Do not edit root compatibility launchers or generated `UPDATE\NAS包` as source.
- The current working tree already contains user changes in `app.py`, `task_store.py`, tests, launchers, and version files. Inspect and preserve those changes before every edit; stage only files belonging to the current task.
- NAS runs Flask/task APIs only. Selenium and browser work remain on the public-duty Windows PC.
- Do not alter CAPTCHA, final-submit, credential, profile, cookie, or `.env` boundaries.
- A site is complete only for `completed_by_user`, an existing explicit success status, or `*_saved`.
- `not_started`, running, failed, waiting-confirmation, needs-update, malformed, or missing site data never count as complete.
- Unconfigured fuel means four active sites. Enabled fuel, or a fuel record waiting for manual cleanup, means five active sites.
- A single-site success remains an operation event. It may upgrade the task to four/five-site completion only through the shared reconciliation function.
- All task-store mutations that can change completion must reconcile while holding the existing store lock.
- Tests use `py -m unittest`; do not add pytest as a dependency.

## Execution Preflight

Before Task 1, capture the existing ownership boundary:

```powershell
git status --short --branch
git diff --name-status
git diff -- `
  'WinPython_公務電腦使用包/ambulance_bot/task_store.py' `
  'WinPython_公務電腦使用包/app.py' `
  'tests/test_task_store.py' `
  'tests/test_web_app.py' `
  'tests/test_worker.py' `
  'tests/test_worker_gui.py'
```

The listed files are already dirty in the current workspace. Treat every preflight hunk as user-owned. If a planned hunk overlaps one of them and its intent cannot be preserved unambiguously, stop before editing that hunk and ask the user; do not reset, checkout, stash, or silently include it.

Each task’s `git add -- <paths>` command is valid only after confirming all unstaged hunks in those paths belong to that task. When a path still contains unrelated preflight hunks, use `git add -p -- <path>` for the intended hunks, add new files by exact path, and inspect `git diff --cached` before committing.

---

### Task 1: Authoritative completion snapshot and atomic reconciliation

**Files:**
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_store.py:73-80`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_store.py:510-668`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_store.py:684-737`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_store.py:930-1015`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_store.py:1239-1264`
- Test: `tests/test_task_store.py`

**Interfaces:**
- Produces: `task_completion_snapshot(payload: dict[str, Any]) -> dict[str, Any]`
- Produces: `JsonTaskStore._reconcile_completion_payload(payload, *, finalize_queue: bool, detail: str = "") -> dict[str, Any]`
- Produces: `site_run_completed` as a terminal execution status that is not a completed-task status.
- Consumes: `SITE_DEFINITIONS`, `AmbulanceReturnRequest`, `SUCCESS_SITE_STATUSES`, and existing worker queue helpers.

- [ ] **Step 1: Write failing snapshot and reconciliation tests**

Add these tests to `JsonTaskStoreTests`:

```python
def test_completion_snapshot_requires_all_four_active_sites(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonTaskStore(Path(tmp))
        request = AmbulanceReturnRequest(
            task_id="snapshot-four-sites",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        payload = store.create(request)
        for site_key in ("duty_work_log", "vehicle_mileage", "consumables"):
            payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"
        payload["overall_status"] = "desktop_fast_completed"

        snapshot = task_completion_snapshot(payload)

        self.assertEqual(snapshot["active_site_keys"], [
            "duty_work_log",
            "vehicle_mileage",
            "consumables",
            "disinfection",
        ])
        self.assertEqual(snapshot["site_count_label"], "四站")
        self.assertEqual(snapshot["completed_count"], 3)
        self.assertEqual(snapshot["remaining_site_keys"], ["disinfection"])
        self.assertFalse(snapshot["all_complete"])

def test_single_site_terminal_status_cannot_complete_incomplete_task(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonTaskStore(Path(tmp))
        request = AmbulanceReturnRequest(
            task_id="single-site-not-global",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        store.create(request)
        store.update_site_result(
            request.task_id,
            SiteAutomationResult(
                "disinfection",
                "緊急救護消毒",
                "disinfection_saved",
                "saved",
            ),
        )

        updated = store.set_overall_status(
            request.task_id,
            "site_run_completed",
            "單站登打完成：消毒。",
        )

        self.assertEqual(updated["overall_status"], "site_run_completed")
        self.assertFalse(task_completion_snapshot(updated)["all_complete"])
        self.assertEqual(updated["worker_queue"]["status"], "idle")

def test_last_site_result_atomically_completes_task_once(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonTaskStore(Path(tmp))
        request = AmbulanceReturnRequest(
            task_id="last-site-upgrade",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        payload = store.create(request)
        for site_key in ("duty_work_log", "vehicle_mileage", "consumables"):
            payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"
        store.save_payload(request.task_id, payload)

        first = store.update_site_result(
            request.task_id,
            SiteAutomationResult(
                "disinfection",
                "緊急救護消毒",
                "disinfection_saved",
                "saved",
            ),
        )
        second = store.set_overall_status(
            request.task_id,
            "desktop_fast_completed",
            "重複完成回報。",
        )

        completion_events = [
            event
            for event in second["events"]
            if event.get("status") == "desktop_fast_completed"
        ]
        self.assertEqual(first["overall_status"], "desktop_fast_completed")
        self.assertEqual(second["overall_status"], "desktop_fast_completed")
        self.assertEqual(len(completion_events), 1)

def test_completion_snapshot_requires_fuel_when_enabled(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonTaskStore(Path(tmp))
        request = AmbulanceReturnRequest.from_dict({
            "task_id": "snapshot-five-sites",
            "created_at": datetime.now().isoformat(),
            "vehicle": "新坡92",
            "fuel_record": {
                "enabled": True,
                "date": "2026-07-17",
                "time": "1200",
                "driver": "包華先",
                "product": "柴油",
                "quantity": "30",
                "unit_price": "30",
            },
        })
        payload = store.create(request)
        for site_key in ("duty_work_log", "vehicle_mileage", "consumables", "disinfection"):
            payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"

        snapshot = task_completion_snapshot(payload)

        self.assertEqual(snapshot["site_count_label"], "五站")
        self.assertEqual(snapshot["total_count"], 5)
        self.assertEqual(snapshot["remaining_site_keys"], ["fuel_record"])
        self.assertFalse(snapshot["all_complete"])
```

Also import the new helper:

```python
from ambulance_bot.task_store import (
    JsonTaskStore,
    SiteCompletionConflictError,
    WorkerClaimConflictError,
    task_completion_snapshot,
    worker_claim_lease_is_active,
)
```

- [ ] **Step 2: Run the four tests and verify they fail**

Run:

```powershell
py -m unittest `
  tests.test_task_store.JsonTaskStoreTests.test_completion_snapshot_requires_all_four_active_sites `
  tests.test_task_store.JsonTaskStoreTests.test_single_site_terminal_status_cannot_complete_incomplete_task `
  tests.test_task_store.JsonTaskStoreTests.test_last_site_result_atomically_completes_task_once `
  tests.test_task_store.JsonTaskStoreTests.test_completion_snapshot_requires_fuel_when_enabled -v
```

Expected: failure because `task_completion_snapshot` and `site_run_completed` reconciliation do not exist.

- [ ] **Step 3: Add the pure completion snapshot**

Add the following helpers near `SUCCESS_SITE_STATUSES`:

```python
SITE_RUN_ORDER = tuple(site.key for site in SITE_DEFINITIONS)


def site_status_is_complete(status: object) -> bool:
    value = str(status or "").strip()
    return value in SUCCESS_SITE_STATUSES or value.endswith("_saved")


def task_completion_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    site_statuses = payload.get("site_statuses")
    valid_sites = isinstance(site_statuses, dict)
    statuses = dict(site_statuses or {}) if valid_sites else {}
    valid_task = True
    try:
        request = AmbulanceReturnRequest.from_dict(dict(payload.get("task") or {}))
        has_fuel_record = request.has_fuel_record()
    except (AttributeError, KeyError, TypeError, ValueError):
        valid_task = False
        has_fuel_record = False

    fuel_status = str(dict(statuses.get("fuel_record") or {}).get("status") or "")
    fuel_cleanup_pending = "waiting_confirmation" in fuel_status
    active_site_keys = [
        site_key
        for site_key in SITE_RUN_ORDER
        if site_key != "fuel_record" or has_fuel_record or fuel_cleanup_pending
    ]
    completed_site_keys: list[str] = []
    remaining_site_keys: list[str] = []
    failed_site_keys: list[str] = []
    waiting_site_keys: list[str] = []
    needs_update_site_keys: list[str] = []
    running_site_keys: list[str] = []

    for site_key in active_site_keys:
        site = statuses.get(site_key)
        status = str(site.get("status") or "") if isinstance(site, dict) else ""
        if site_status_is_complete(status):
            completed_site_keys.append(site_key)
            continue
        remaining_site_keys.append(site_key)
        if "failed" in status or "error" in status:
            failed_site_keys.append(site_key)
        if status.endswith("_needs_update"):
            needs_update_site_keys.append(site_key)
            waiting_site_keys.append(site_key)
        elif "waiting_confirmation" in status:
            waiting_site_keys.append(site_key)
        if "running" in status:
            running_site_keys.append(site_key)

    total_count = len(active_site_keys)
    all_complete = (
        valid_task
        and valid_sites
        and total_count > 0
        and len(completed_site_keys) == total_count
    )
    return {
        "active_site_keys": active_site_keys,
        "site_count_label": "五站" if len(active_site_keys) == 5 else "四站",
        "total_count": total_count,
        "completed_count": len(completed_site_keys),
        "completed_site_keys": completed_site_keys,
        "remaining_site_keys": remaining_site_keys,
        "failed_site_keys": failed_site_keys,
        "waiting_site_keys": waiting_site_keys,
        "needs_update_site_keys": needs_update_site_keys,
        "running_site_keys": running_site_keys,
        "all_complete": all_complete,
    }
```

- [ ] **Step 4: Add locked reconciliation without prematurely ending an active Worker claim**

Add this method to `JsonTaskStore` and make `_is_fully_done()` delegate to the snapshot:

```python
def _reconcile_completion_payload(
    self,
    payload: dict[str, Any],
    *,
    finalize_queue: bool,
    detail: str = "",
) -> dict[str, Any]:
    snapshot = task_completion_snapshot(payload)
    current_status = str(payload.get("overall_status") or "")
    if snapshot["all_complete"]:
        if current_status != "desktop_fast_completed":
            completion_detail = detail or f"{snapshot['site_count_label']}登打完成。"
            payload["overall_status"] = "desktop_fast_completed"
            self.add_event_to_payload(
                payload,
                "desktop_fast_completed",
                completion_detail,
            )
        if finalize_queue:
            queue_state = worker_queue_state(payload)
            queue_state["status"] = "completed"
            queue_state["completed_at"] = queue_state.get("completed_at") or now_text()
            queue_state["lease_expires_at"] = ""
            queue_state["last_error"] = ""
            payload["worker_queue"] = queue_state
        return snapshot

    if current_status == "desktop_fast_completed":
        if snapshot["needs_update_site_keys"]:
            payload["overall_status"] = "task_updated_needs_site_update"
        elif snapshot["failed_site_keys"]:
            payload["overall_status"] = "desktop_fast_completed_with_errors"
        else:
            payload["overall_status"] = "site_run_completed"
    return snapshot


def _is_fully_done(self, payload: dict[str, Any]) -> bool:
    return bool(task_completion_snapshot(payload)["all_complete"])
```

Update `worker_queue_overall_status_is_terminal()`:

```python
def worker_queue_overall_status_is_terminal(status: str) -> bool:
    value = str(status or "").strip().lower()
    return (
        value.startswith("desktop_fast_completed")
        or value in {"failed", "worker_failed", "site_run_completed"}
    )
```

- [ ] **Step 5: Call reconciliation from every completion-changing mutation**

Apply these exact rules:

```python
# update_site_result(): after adding the site event and before save
self._reconcile_completion_payload(
    payload,
    finalize_queue=_payload is None,
    detail=f"{result.name}完成後，所有有效站別皆已完成。",
)

# apply_worker_status(): after optional result and optional overall status
self._reconcile_completion_payload(
    payload,
    finalize_queue=bool(overall_status),
    detail=overall_detail,
)

# mark_site_completed(): replace the direct _is_fully_done block
self._reconcile_completion_payload(
    payload,
    finalize_queue=True,
    detail="各站皆已完成；人工確認後更新任務狀態。",
)

# reconcile_legacy_silent_save_results(): replace its direct completion block
self._reconcile_completion_payload(
    payload,
    finalize_queue=True,
    detail=detail,
)
```

In `set_overall_status()`, let an already-complete snapshot win before applying the requested status. This prevents a later `site_run_completed` call from downgrading and re-upgrading the task, which would create a duplicate completion event:

```python
snapshot = task_completion_snapshot(payload)
if snapshot["all_complete"]:
    self._reconcile_completion_payload(
        payload,
        finalize_queue=worker_queue_overall_status_is_terminal(status),
        detail=detail,
    )
else:
    effective_status = status
    effective_detail = detail
    if status == "desktop_fast_completed":
        effective_status = "site_run_completed"
        effective_detail = detail or "單次執行已結束，但任務尚未全部完成。"
    self._apply_overall_status_to_payload(
        payload,
        effective_status,
        effective_detail,
    )
    self._reconcile_completion_payload(
        payload,
        finalize_queue=worker_queue_overall_status_is_terminal(effective_status),
        detail=detail,
    )
```

Replace the mutation part of `apply_worker_status()` after claim and duplicate-event validation with:

```python
if result is not None:
    self.update_site_result(
        task_id,
        result,
        vehicle_key=vehicle_key,
        vehicle_label=vehicle_label,
        _payload=payload,
        _save=False,
    )
snapshot = task_completion_snapshot(payload)
if overall_status and not snapshot["all_complete"]:
    effective_status = overall_status
    effective_detail = overall_detail
    if overall_status == "desktop_fast_completed":
        effective_status = "site_run_completed"
        effective_detail = overall_detail or "單次執行已結束，但任務尚未全部完成。"
    self._apply_overall_status_to_payload(
        payload,
        effective_status,
        effective_detail,
    )
self._reconcile_completion_payload(
    payload,
    finalize_queue=bool(overall_status),
    detail=overall_detail,
)
self._remember_status_event_id(payload, event_id)
self.save_payload(task_id, payload)
return payload, False
```

This writes the site result first, recomputes the snapshot, and does not apply a second success-like overall status when the task is already fully complete. Therefore a final site result plus a later Worker terminal post produces exactly one `desktop_fast_completed` transition event.

- [ ] **Step 6: Run the focused and full task-store tests**

Run:

```powershell
py -m unittest tests.test_task_store -v
```

Expected: all `tests.test_task_store` tests pass with no duplicate completion event.

- [ ] **Step 7: Commit Task 1**

```powershell
git add -- `
  'WinPython_公務電腦使用包/ambulance_bot/task_store.py' `
  'tests/test_task_store.py'
git diff --cached --check
git commit -m "fix: reconcile task completion from site results"
```

---

### Task 2: Structured field, site, and vehicle edit impact

**Files:**
- Create: `WinPython_公務電腦使用包/ambulance_bot/task_edit_impact.py`
- Modify: `WinPython_公務電腦使用包/app.py:334-345`
- Modify: `WinPython_公務電腦使用包/app.py:2407-2495`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_store.py:133-258`
- Test: `tests/test_task_edit_impact.py`
- Test: `tests/test_task_store.py:1436-1518`
- Test: `tests/test_web_app.py:5063-5473`

**Interfaces:**
- Produces: `analyze_task_edit(previous_task: dict, current_task: dict) -> dict[str, Any]`
- Produces: `changed_site_keys(impact: dict) -> set[str]`
- Produces persisted `payload["pending_edit_impact"]` with `changed_labels`, `affected_sites`, and `site_summaries`.
- Consumes: existing per-site `update_context`, `_prune_changed_vehicle_results()`, and `_unchanged_vehicle_checkpoint_keys()`.

- [ ] **Step 1: Create failing focused impact tests**

Create `tests/test_task_edit_impact.py`:

```python
import unittest

from ambulance_bot.task_edit_impact import analyze_task_edit, changed_site_keys


class TaskEditImpactTests(unittest.TestCase):
    def test_consumables_only_affects_consumables(self):
        previous = {
            "vehicle": "新坡92",
            "consumables": {"口罩": 2},
        }
        current = {
            "vehicle": "新坡92",
            "consumables": {"手套": 1},
        }

        impact = analyze_task_edit(previous, current)

        self.assertEqual(impact["changed_labels"], ["耗材"])
        self.assertEqual(changed_site_keys(impact), {"consumables"})
        self.assertEqual(impact["site_summaries"], ["耗材"])

    def test_second_vehicle_mileage_targets_only_second_vehicle_mileage(self):
        previous = {
            "two_vehicle": True,
            "vehicle_entries": [
                {"vehicle": "新坡92", "mileage": "100"},
                {"vehicle": "新坡93", "mileage": "200"},
            ],
        }
        current = {
            "two_vehicle": True,
            "vehicle_entries": [
                {"vehicle": "新坡92", "mileage": "100"},
                {"vehicle": "新坡93", "mileage": "220"},
            ],
        }

        impact = analyze_task_edit(previous, current)

        self.assertEqual(impact["changed_labels"], ["第 2 車里程"])
        self.assertEqual(changed_site_keys(impact), {"vehicle_mileage"})
        self.assertEqual(
            impact["affected_sites"]["vehicle_mileage"]["vehicle_keys"],
            ["新坡93"],
        )
        self.assertEqual(
            impact["site_summaries"],
            ["里程（只重登第 2 車）"],
        )

    def test_case_address_affects_work_and_mileage(self):
        impact = analyze_task_edit(
            {"case_address": "桃園市觀音區 A 路"},
            {"case_address": "桃園市觀音區 B 路"},
        )

        self.assertEqual(
            changed_site_keys(impact),
            {"duty_work_log", "vehicle_mileage"},
        )

    def test_driver_affects_work_mileage_and_enabled_fuel(self):
        previous = {
            "vehicle": "新坡92",
            "driver": "包華先",
            "fuel_record": {"enabled": True},
        }
        current = {
            "vehicle": "新坡92",
            "driver": "陳小明",
            "fuel_record": {"enabled": True},
        }

        impact = analyze_task_edit(previous, current)

        self.assertEqual(
            changed_site_keys(impact),
            {"duty_work_log", "vehicle_mileage", "fuel_record"},
        )

    def test_normalized_equivalent_values_create_no_impact(self):
        previous = {
            "vehicle": "新坡92",
            "consumables": {"口罩": 2, "手套": 1},
            "disinfection_items": ["車內", "擔架"],
        }
        current = {
            "vehicle": " 新坡92 ",
            "consumables": {"手套": "1", "口罩": "2"},
            "disinfection_items": ["擔架", "車內"],
        }

        impact = analyze_task_edit(previous, current)

        self.assertEqual(impact["changed_labels"], [])
        self.assertEqual(impact["affected_sites"], {})
        self.assertEqual(impact["site_summaries"], [])

    def test_adding_second_vehicle_affects_only_the_added_vehicle_sites(self):
        previous = {
            "vehicle_entries": [
                {"vehicle": "新坡92", "driver": "王小明", "mileage": "100"},
            ],
        }
        current = {
            "two_vehicle": True,
            "vehicle_entries": [
                {"vehicle": "新坡92", "driver": "王小明", "mileage": "100"},
                {"vehicle": "新坡93", "driver": "陳小華", "mileage": "200"},
            ],
        }

        impact = analyze_task_edit(previous, current)

        self.assertEqual(
            changed_site_keys(impact),
            {"duty_work_log", "vehicle_mileage", "consumables", "disinfection"},
        )
        for site_key in changed_site_keys(impact):
            self.assertEqual(
                impact["affected_sites"][site_key]["vehicle_keys"],
                ["新坡93"],
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new test module and verify import failure**

Run:

```powershell
py -m unittest tests.test_task_edit_impact -v
```

Expected: import failure because `ambulance_bot.task_edit_impact` does not exist.

- [ ] **Step 3: Implement one normalized impact analyzer**

Create `task_edit_impact.py` with these public constants and output shape:

```python
from __future__ import annotations

from itertools import zip_longest
from typing import Any


SITE_LABELS = {
    "duty_work_log": "工作",
    "vehicle_mileage": "里程",
    "fuel_record": "加油",
    "consumables": "耗材",
    "disinfection": "消毒",
}

COMMON_FIELD_RULES = {
    "case_date": ("案件日期", {"duty_work_log", "vehicle_mileage", "consumables", "disinfection"}),
    "case_time": ("案件時間", {"duty_work_log", "vehicle_mileage", "consumables", "disinfection"}),
    "case_address": ("案件地址", {"duty_work_log", "vehicle_mileage"}),
    "case_reason": ("出勤原因", {"duty_work_log"}),
    "work_note": ("工作備註", {"duty_work_log"}),
}

VEHICLE_FIELD_RULES = {
    "vehicle": ("車號", {"duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"}),
    "driver": ("駕駛", {"duty_work_log", "vehicle_mileage", "fuel_record"}),
    "mileage": ("里程", {"vehicle_mileage"}),
    "return_date": ("返隊日期", {"vehicle_mileage"}),
    "return_time": ("返隊時間", {"vehicle_mileage"}),
    "patient_summary": ("傷病患摘要", {"duty_work_log"}),
    "fuel_record": ("加油資料", {"fuel_record"}),
    "consumables": ("耗材", {"consumables"}),
    "disinfection": ("消毒方式", {"disinfection"}),
    "disinfection_items": ("消毒項目", {"disinfection"}),
}


def normalize_edit_value(value: object) -> object:
    if isinstance(value, dict):
        if "enabled" in value:
            return tuple(
                sorted(
                    (str(key).strip(), normalize_edit_value(item))
                    for key, item in value.items()
                    if str(key).strip()
                )
            )
        normalized_items: list[tuple[str, int]] = []
        for key, item in value.items():
            name = str(key).strip()
            try:
                quantity = int(item)
            except (TypeError, ValueError):
                quantity = 0
            if name and quantity > 0:
                normalized_items.append((name, quantity))
        return tuple(sorted(normalized_items))
    if isinstance(value, list):
        return tuple(sorted(str(item).strip() for item in value if str(item).strip()))
    if isinstance(value, bool):
        return value
    return str(value or "").strip()


def _vehicle_entries(task: dict[str, Any]) -> list[dict[str, Any]]:
    entries = task.get("vehicle_entries")
    if isinstance(entries, list) and entries:
        return [dict(item or {}) for item in entries if isinstance(item, dict)]
    return [dict(task)]


def _fuel_enabled(entry: dict[str, Any]) -> bool:
    fuel = entry.get("fuel_record")
    return bool(dict(fuel or {}).get("enabled")) if isinstance(fuel, dict) else False


def analyze_task_edit(
    previous_task: dict[str, Any],
    current_task: dict[str, Any],
) -> dict[str, Any]:
    changed_fields: list[dict[str, str]] = []
    affected_sites: dict[str, dict[str, Any]] = {}

    def record(
        *,
        key: str,
        label: str,
        site_keys: set[str],
        vehicle_key: str = "",
        vehicle_label: str = "",
        fuel_active: bool = True,
    ) -> None:
        changed_fields.append({"key": key, "label": label})
        for site_key in site_keys:
            if site_key == "fuel_record" and not fuel_active:
                continue
            site = affected_sites.setdefault(
                site_key,
                {
                    "site_key": site_key,
                    "site_label": SITE_LABELS[site_key],
                    "vehicle_keys": [],
                    "vehicle_labels": [],
                    "field_labels": [],
                },
            )
            if vehicle_key and vehicle_key not in site["vehicle_keys"]:
                site["vehicle_keys"].append(vehicle_key)
            if vehicle_label and vehicle_label not in site["vehicle_labels"]:
                site["vehicle_labels"].append(vehicle_label)
            if label not in site["field_labels"]:
                site["field_labels"].append(label)

    for field_name, (label, site_keys) in COMMON_FIELD_RULES.items():
        if normalize_edit_value(previous_task.get(field_name)) != normalize_edit_value(current_task.get(field_name)):
            record(key=field_name, label=label, site_keys=site_keys)

    previous_entries = _vehicle_entries(previous_task)
    current_entries = _vehicle_entries(current_task)
    multiple = max(len(previous_entries), len(current_entries)) > 1
    for index, pair in enumerate(
        zip_longest(previous_entries, current_entries, fillvalue={}),
        start=1,
    ):
        previous_entry, current_entry = pair
        vehicle_label = f"第 {index} 車" if multiple else ""
        vehicle_key = str(
            current_entry.get("vehicle")
            or previous_entry.get("vehicle")
            or vehicle_label
        ).strip()
        fuel_active = _fuel_enabled(previous_entry) or _fuel_enabled(current_entry)
        for field_name, (field_label, site_keys) in VEHICLE_FIELD_RULES.items():
            if normalize_edit_value(previous_entry.get(field_name)) == normalize_edit_value(current_entry.get(field_name)):
                continue
            label = f"{vehicle_label}{field_label}" if vehicle_label else field_label
            record(
                key=f"vehicle_entries.{index - 1}.{field_name}",
                label=label,
                site_keys=site_keys,
                vehicle_key=vehicle_key,
                vehicle_label=vehicle_label,
                fuel_active=fuel_active,
            )

    changed_labels = list(dict.fromkeys(item["label"] for item in changed_fields))
    site_summaries: list[str] = []
    for site in affected_sites.values():
        labels = list(site["vehicle_labels"])
        if len(labels) == 1:
            site_summaries.append(f"{site['site_label']}（只重登{labels[0]}）")
        else:
            site_summaries.append(str(site["site_label"]))
    return {
        "changed_fields": changed_fields,
        "changed_labels": changed_labels,
        "affected_sites": affected_sites,
        "site_summaries": site_summaries,
    }


def changed_site_keys(impact: dict[str, Any]) -> set[str]:
    affected_sites = impact.get("affected_sites")
    return set(affected_sites) if isinstance(affected_sites, dict) else set()
```

- [ ] **Step 4: Route editing through the structured analyzer**

In `app.py`, replace the independent mapping in `changed_sites_for_task_edit()`:

```python
from ambulance_bot.task_edit_impact import analyze_task_edit, changed_site_keys


def changed_sites_for_task_edit(previous_task: dict, current_task: dict) -> set[str]:
    return changed_site_keys(analyze_task_edit(previous_task, current_task))
```

In the edit POST route:

```python
previous_task = dict(previous_payload.get("task") or {})
current_task = task_request.to_dict()
edit_impact = analyze_task_edit(previous_task, current_task)
changed_site_keys_for_edit = changed_site_keys(edit_impact)
site_update_contexts = site_update_contexts_for_task_edit(
    previous_task,
    current_task,
    changed_site_keys_for_edit,
)
payload = store.update_task(
    task_id,
    task_request,
    changed_site_keys=changed_site_keys_for_edit,
    site_update_contexts=site_update_contexts,
    edit_impact=edit_impact,
)
```

Rename the local variable so it does not shadow the imported `changed_site_keys()` function.

- [ ] **Step 5: Persist and resolve only pending impacts**

Extend `JsonTaskStore.update_task()`:

```python
def update_task(
    self,
    task_id: str,
    request: AmbulanceReturnRequest,
    changed_site_keys: set[str] | None = None,
    site_update_contexts: dict[str, dict[str, object]] | None = None,
    edit_impact: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

Add this merger above `JsonTaskStore` so repeated edits retain unresolved earlier impacts:

```python
def merge_pending_edit_impacts(
    existing: dict[str, Any],
    current: dict[str, Any],
    updated_sites: list[str],
) -> dict[str, Any]:
    merged_changed_labels = list(existing.get("changed_labels") or [])
    for label in current.get("changed_labels") or []:
        if label not in merged_changed_labels:
            merged_changed_labels.append(label)
    merged_sites = {
        str(site_key): dict(site)
        for site_key, site in dict(existing.get("affected_sites") or {}).items()
        if isinstance(site, dict)
    }
    current_sites = dict(current.get("affected_sites") or {})
    for site_key in updated_sites:
        site = current_sites.get(site_key)
        if isinstance(site, dict):
            merged_sites[site_key] = dict(site)
    site_summaries = [
        (
            f"{site['site_label']}（只重登{site['vehicle_labels'][0]}）"
            if len(site.get("vehicle_labels") or []) == 1
            else str(site.get("site_label") or site_key)
        )
        for site_key, site in merged_sites.items()
    ]
    return {
        "changed_fields": list(existing.get("changed_fields") or [])
        + list(current.get("changed_fields") or []),
        "changed_labels": merged_changed_labels,
        "affected_sites": merged_sites,
        "site_summaries": site_summaries,
    }
```

After `_mark_changed_sites_for_update()`:

```python
impact = dict(edit_impact or {})
existing_impact = (
    dict(payload.get("pending_edit_impact") or {})
    if isinstance(payload.get("pending_edit_impact"), dict)
    else {}
)
if updated_sites and impact.get("affected_sites"):
    pending_impact = merge_pending_edit_impacts(
        existing_impact,
        impact,
        updated_sites,
    )
    payload["pending_edit_impact"] = pending_impact
    changed_text = "、".join(impact.get("changed_labels") or [])
    site_text = "、".join(
        str(
            dict(impact.get("affected_sites") or {})
            .get(site_key, {})
            .get("site_label")
            or site_key
        )
        for site_key in updated_sites
    )
    self.add_event_to_payload(
        payload,
        "task_updated",
        f"任務內容已修改：{changed_text}；需重新登打：{site_text}。",
    )
```

Remove the unconditional generic `task_updated` event. If normalized data has no new impact, retain both the prior overall status and any existing `pending_edit_impact`; do not add a false modification event. A second real edit merges with unresolved earlier sites instead of erasing them.

Add and call this resolver after a site becomes complete:

```python
def _resolve_pending_edit_site(
    self,
    payload: dict[str, Any],
    site_key: str,
) -> None:
    impact = payload.get("pending_edit_impact")
    if not isinstance(impact, dict):
        return
    affected = impact.get("affected_sites")
    if not isinstance(affected, dict) or site_key not in affected:
        return
    site = dict(payload.get("site_statuses", {}).get(site_key) or {})
    if not site_status_is_complete(site.get("status")):
        return
    remaining = dict(affected)
    remaining.pop(site_key, None)
    if not remaining:
        payload.pop("pending_edit_impact", None)
        return
    impact["affected_sites"] = remaining
    impact["site_summaries"] = [
        (
            f"{item['site_label']}（只重登{item['vehicle_labels'][0]}）"
            if len(item.get("vehicle_labels") or []) == 1
            else str(item.get("site_label") or key)
        )
        for key, item in remaining.items()
        if isinstance(item, dict)
    ]
    payload["pending_edit_impact"] = impact
```

Call `_resolve_pending_edit_site(payload, result.key)` in `update_site_result()` after the aggregate site status is computed, and call it from `mark_site_completed()` after manual completion.

Add this regression to `tests/test_task_store.py`:

```python
def test_repeated_edit_impact_keeps_unresolved_earlier_site(self):
    existing = {
        "changed_fields": [{"key": "mileage", "label": "里程"}],
        "changed_labels": ["里程"],
        "affected_sites": {
            "vehicle_mileage": {
                "site_key": "vehicle_mileage",
                "site_label": "里程",
                "vehicle_keys": ["新坡92"],
                "vehicle_labels": [],
                "field_labels": ["里程"],
            },
        },
    }
    current = {
        "changed_fields": [{"key": "consumables", "label": "耗材"}],
        "changed_labels": ["耗材"],
        "affected_sites": {
            "consumables": {
                "site_key": "consumables",
                "site_label": "耗材",
                "vehicle_keys": ["新坡92"],
                "vehicle_labels": [],
                "field_labels": ["耗材"],
            },
        },
    }

    merged = merge_pending_edit_impacts(existing, current, ["consumables"])

    self.assertEqual(merged["changed_labels"], ["里程", "耗材"])
    self.assertEqual(
        list(merged["affected_sites"]),
        ["vehicle_mileage", "consumables"],
    )
```

Import `merge_pending_edit_impacts` from `ambulance_bot.task_store` in the test module.

- [ ] **Step 6: Run impact, store, and edit-route tests**

Run:

```powershell
py -m unittest `
  tests.test_task_edit_impact `
  tests.test_task_store `
  tests.test_web_app.WebAppTests.test_task_edit_consumables_only_preserves_other_completed_sites `
  tests.test_web_app.WebAppTests.test_task_edit_second_vehicle_fields_mark_saved_sites_for_update -v
```

Expected: all selected tests pass; first-vehicle checkpoints remain present when only the second vehicle changes.

- [ ] **Step 7: Commit Task 2**

```powershell
git add -- `
  'WinPython_公務電腦使用包/ambulance_bot/task_edit_impact.py' `
  'WinPython_公務電腦使用包/ambulance_bot/task_store.py' `
  'WinPython_公務電腦使用包/app.py' `
  'tests/test_task_edit_impact.py' `
  'tests/test_task_store.py' `
  'tests/test_web_app.py'
git diff --cached --check
git commit -m "feat: track selective reruns after task edits"
```

---

### Task 3: App task page and recent-task completion display

**Files:**
- Modify: `WinPython_公務電腦使用包/app.py:2672-2822`
- Modify: `WinPython_公務電腦使用包/app.py:2910-2924`
- Modify: `WinPython_公務電腦使用包/app.py:3180-3230`
- Modify: `WinPython_公務電腦使用包/templates/task_detail.html:350-376`
- Modify: `WinPython_公務電腦使用包/templates/new_task.html:735-749`
- Test: `tests/test_web_app.py:1616-1695`
- Test: `tests/test_web_app.py:4296-4345`
- Test: `tests/test_web_app.py:5037-5189`

**Interfaces:**
- Consumes: `task_completion_snapshot(payload)`.
- Consumes: `payload["pending_edit_impact"]`.
- Produces: `task_completion_label(payload) -> str`.
- Produces: task-page success and edit-impact summaries.

- [ ] **Step 1: Write failing page tests**

Add these tests to `WebAppTests`:

```python
def test_completed_task_hides_all_run_buttons_and_shows_four_site_completion(self):
    create_response = self.client.post("/tasks", data=self.valid_task_data())
    task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
    for site_key, name in (
        ("duty_work_log", "工作"),
        ("vehicle_mileage", "里程"),
        ("consumables", "耗材"),
        ("disinfection", "消毒"),
    ):
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                site_key,
                name,
                f"{site_key}_saved",
                "saved",
            ),
        )

    body = html.unescape(self.client.get(f"/tasks/{task_id}").get_data(as_text=True))

    self.assertIn("✓ 四站登打完成", body)
    self.assertNotIn("四站登打啟動", body)
    self.assertNotIn("單獨登打", body)
    self.assertIn("返回編輯", body)

def test_completed_task_edit_shows_changed_field_site_and_vehicle(self):
    create_response = self.client.post(
        "/tasks",
        data=self.valid_task_data(
            two_vehicle="1",
            vehicle_2="新坡93",
            driver_2="陳小明",
            mileage_2="200",
        ),
    )
    task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
    payload = self.store.get(task_id)
    for site_key in ("duty_work_log", "vehicle_mileage", "consumables", "disinfection"):
        payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"
    payload["site_statuses"]["vehicle_mileage"]["vehicle_results"] = {
        "新坡91": {"status": "vehicle_mileage_saved", "detail": "first"},
        "新坡93": {"status": "vehicle_mileage_saved", "detail": "second"},
    }
    self.store.save_payload(task_id, payload)
    update_data = self.valid_task_data(
        two_vehicle="1",
        vehicle_2="新坡93",
        driver_2="陳小明",
        mileage_2="220",
    )

    self.client.post(f"/tasks/{task_id}/edit", data=update_data)
    body = html.unescape(self.client.get(f"/tasks/{task_id}").get_data(as_text=True))

    self.assertIn("任務資料已修改", body)
    self.assertIn("已修改：第 2 車里程", body)
    self.assertIn("需重新登打：里程（只重登第 2 車）", body)
    self.assertIn("更新里程", body)
    self.assertIn("四站登打啟動", body)
    self.assertEqual(
        list(self.store.get(task_id)["site_statuses"]["vehicle_mileage"]["vehicle_results"]),
        ["新坡91"],
    )

def test_recent_task_uses_four_site_completion_label(self):
    create_response = self.client.post("/tasks", data=self.valid_task_data())
    task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
    payload = self.store.get(task_id)
    for site_key in ("duty_work_log", "vehicle_mileage", "consumables", "disinfection"):
        payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"
    payload["overall_status"] = "site_run_completed"
    self.store.save_payload(task_id, payload)

    body = html.unescape(self.client.get("/app").get_data(as_text=True))

    self.assertIn("四站登打完成", body)
    self.assertIn('class="status complete">完成</span>', body)
```

- [ ] **Step 2: Run the page tests and verify current output fails**

Run:

```powershell
py -m unittest `
  tests.test_web_app.WebAppTests.test_completed_task_hides_all_run_buttons_and_shows_four_site_completion `
  tests.test_web_app.WebAppTests.test_completed_task_edit_shows_changed_field_site_and_vehicle `
  tests.test_web_app.WebAppTests.test_recent_task_uses_four_site_completion_label -v
```

Expected: at least the completion banner, hidden full-run button, and edit-impact summary assertions fail.

- [ ] **Step 3: Make App status helpers consume the shared snapshot**

Import `task_completion_snapshot` from `ambulance_bot.task_store`.

Replace `effective_task_status()` with:

```python
def effective_task_status(payload: dict) -> str:
    snapshot = task_completion_snapshot(payload)
    if snapshot["running_site_keys"]:
        return "desktop_fast_running"
    if snapshot["failed_site_keys"]:
        return "failed"
    if snapshot["needs_update_site_keys"]:
        return "site_needs_update"
    site_statuses = dict(payload.get("site_statuses") or {})
    if any(
        site_waits_for_confirmation(
            str(dict(site_statuses.get(site_key) or {}).get("status") or "")
        )
        for site_key in snapshot["waiting_site_keys"]
    ):
        return "manual_confirmation_required"
    if snapshot["waiting_site_keys"]:
        return "manual_captcha_required"
    if snapshot["all_complete"]:
        return "desktop_fast_completed"
    return str(payload.get("overall_status") or "")
```

Map the new execution-terminal status to a safe incomplete presentation:

```python
# status_label()
if value == "site_run_completed":
    return "部分完成"

# status_class()
if value == "site_run_completed":
    return "waiting"
```

Replace `task_progress_summary()` counting with:

```python
def task_progress_summary(payload: dict) -> str:
    snapshot = task_completion_snapshot(payload)
    completed_count = int(snapshot["completed_count"])
    total_count = int(snapshot["total_count"])
    if snapshot["all_complete"]:
        return f"{snapshot['site_count_label']}登打完成"
    if snapshot["running_site_keys"]:
        site_key = snapshot["running_site_keys"][0]
        return f"已完成 {completed_count}/{total_count}；目前：{SITE_SHORT_NAMES[site_key]}執行中"
    if snapshot["needs_update_site_keys"]:
        names = "、".join(SITE_SHORT_NAMES[key] for key in snapshot["needs_update_site_keys"])
        return f"已完成 {completed_count}/{total_count}；需更新：{names}"
    if snapshot["waiting_site_keys"]:
        site_key = snapshot["waiting_site_keys"][0]
        return f"已完成 {completed_count}/{total_count}；待確認：{SITE_SHORT_NAMES[site_key]}"
    if len(snapshot["failed_site_keys"]) == 1:
        site_key = snapshot["failed_site_keys"][0]
        return f"已完成 {completed_count}/{total_count}；失敗：{SITE_SHORT_NAMES[site_key]}"
    if snapshot["failed_site_keys"]:
        return f"已完成 {completed_count}/{total_count}；{len(snapshot['failed_site_keys'])} 站失敗"
    return f"已完成 {completed_count}/{total_count}；尚未開始"
```

Expose `task_completion_snapshot` in Jinja globals.

- [ ] **Step 4: Render the completion and edit-impact banners and gate buttons**

At the start of the task section in `task_detail.html`:

```jinja2
{% set completion = task_completion_snapshot(payload) %}
{% set edit_impact = payload.pending_edit_impact or {} %}
{% if completion.all_complete %}
  <div class="completion-banner">✓ {{ completion.site_count_label }}登打完成</div>
{% elif edit_impact %}
  <div class="edit-impact-banner">
    <strong>任務資料已修改</strong>
    <div>已修改：{{ edit_impact.changed_labels | join('、') }}</div>
    <div>需重新登打：{{ edit_impact.site_summaries | join('、') }}</div>
  </div>
{% endif %}
```

Change the full-run button condition:

```jinja2
{% if
  show_task_entry_controls()
  and not task_active
  and not completion.all_complete
  and not task_has_waiting_confirmation(payload.site_statuses)
%}
```

Add `and not completion.all_complete` to each single-site button condition. Keep the edit link visible when the task is complete.

Add compact CSS using the page’s existing colors:

```css
.completion-banner,
.edit-impact-banner {
  margin-bottom: 14px;
  padding: 12px 14px;
  border-radius: 10px;
  line-height: 1.6;
}
.completion-banner {
  color: var(--complete);
  background: var(--complete-bg);
  font-weight: 800;
}
.edit-impact-banner {
  color: var(--waiting);
  background: var(--waiting-bg);
}
```

- [ ] **Step 5: Run all web tests**

Run:

```powershell
py -m unittest tests.test_web_app -v
```

Expected: all web/template/API tests pass; completed tasks show no entry button.

- [ ] **Step 6: Commit Task 3**

```powershell
git add -- `
  'WinPython_公務電腦使用包/app.py' `
  'WinPython_公務電腦使用包/templates/task_detail.html' `
  'WinPython_公務電腦使用包/templates/new_task.html' `
  'tests/test_web_app.py'
git diff --cached --check
git commit -m "feat: show authoritative task completion in app"
```

---

### Task 4: Local multi-site and single-site runner semantics

**Files:**
- Modify: `WinPython_公務電腦使用包/ambulance_bot/desktop_fast_runner.py:430-576`
- Test: `tests/test_desktop_fast_runner.py:615-754`

**Interfaces:**
- Consumes: task-store atomic reconciliation from Task 1.
- Produces: single-site operation events without falsely setting the task complete.
- Preserves: full-run skip behavior and fuel/mileage paired sequence.

- [ ] **Step 1: Write failing runner regression tests**

Add:

```python
def test_single_site_success_does_not_complete_task_with_other_sites_unstarted(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonTaskStore(Path(tmp) / "tasks")
        request = AmbulanceReturnRequest(
            task_id="single-site-incomplete",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        store.create(request)
        actions = []
        runner = DesktopFastRunner(
            Path(tmp),
            store=store,
            event_callback=lambda payload, action: actions.append(
                (payload["overall_status"], action)
            ),
        )

        with patch(
            "ambulance_bot.desktop_fast_runner.run_disinfection_task",
            return_value=SimpleNamespace(
                ok=True,
                status="disinfection_saved",
                detail="saved",
            ),
        ):
            runner.start_site(request.task_id, "disinfection")
            self.assertTrue(runner.wait_for_idle())

        payload = store.get(request.task_id)
        self.assertEqual(payload["overall_status"], "site_run_completed")
        self.assertEqual(actions[-1][1], "單站登打成功：消毒")
        self.assertFalse(task_completion_snapshot(payload)["all_complete"])

def test_two_failed_sites_then_two_single_site_retries_finish_four_sites(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonTaskStore(Path(tmp) / "tasks")
        request = AmbulanceReturnRequest(
            task_id="two-retries-finish",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        payload = store.create(request)
        payload["site_statuses"]["duty_work_log"]["status"] = "duty_work_log_saved"
        payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_saved"
        payload["site_statuses"]["consumables"]["status"] = "consumables_failed"
        payload["site_statuses"]["disinfection"]["status"] = "disinfection_failed"
        payload["overall_status"] = "desktop_fast_completed_with_errors"
        store.save_payload(request.task_id, payload)
        runner = DesktopFastRunner(Path(tmp), store=store)

        with patch(
            "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
            return_value=SimpleNamespace(),
        ), patch(
            "ambulance_bot.desktop_fast_runner.open_consumable_record_for_task",
            return_value="saved",
        ), patch(
            "ambulance_bot.desktop_fast_runner.save_consumables_record_enabled",
            return_value=True,
        ), patch(
            "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
            return_value=SimpleNamespace(),
        ), patch(
            "ambulance_bot.desktop_fast_runner.run_disinfection_task",
            return_value=SimpleNamespace(
                ok=True,
                status="disinfection_saved",
                detail="saved",
            ),
        ):
            runner.start_site(request.task_id, "consumables")
            self.assertTrue(runner.wait_for_idle())
            self.assertFalse(task_completion_snapshot(store.get(request.task_id))["all_complete"])
            runner.start_site(request.task_id, "disinfection")
            self.assertTrue(runner.wait_for_idle())

        completed = store.get(request.task_id)
        self.assertTrue(task_completion_snapshot(completed)["all_complete"])
        self.assertEqual(completed["overall_status"], "desktop_fast_completed")
```

Import `task_completion_snapshot` in the test module.

- [ ] **Step 2: Run the runner regressions and verify failure**

Run:

```powershell
py -m unittest `
  tests.test_desktop_fast_runner.DesktopFastRunnerTests.test_single_site_success_does_not_complete_task_with_other_sites_unstarted `
  tests.test_desktop_fast_runner.DesktopFastRunnerTests.test_two_failed_sites_then_two_single_site_retries_finish_four_sites -v
```

Expected: the first test observes the current premature `desktop_fast_completed`.

- [ ] **Step 3: Stop single-site execution from writing global success**

Replace the successful branch in `_run_single_site()`:

```python
self._set_overall_status_owned(
    task_id,
    "site_run_completed",
    f"單站登打完成：{SITE_NAMES[site_key]}。",
)
self._notify(task_id, f"單站登打成功：{SITE_NAMES[site_key]}")
```

For an unconfigured fuel single-site request, use `desktop_fast_unavailable`, not `desktop_fast_completed`.

Keep `_set_overall_status_owned()` and its manual execution-lease enforcement unchanged. Keep the operation callback exactly `單站登打成功：站別`; do not emit a second fake full-run action. The callback payload already contains the reconciled four/five-site overall state.

- [ ] **Step 4: Run the entire local runner test module**

Run:

```powershell
py -m unittest tests.test_desktop_fast_runner -v
```

Expected: all local runner tests pass, including resume-at-failed-site and fuel/mileage pairing.

- [ ] **Step 5: Commit Task 4**

```powershell
git add -- `
  'WinPython_公務電腦使用包/ambulance_bot/desktop_fast_runner.py' `
  'tests/test_desktop_fast_runner.py'
git diff --cached --check
git commit -m "fix: separate single-site success from task completion"
```

---

### Task 5: NAS public-PC admin main status and success classification

**Files:**
- Modify: `WinPython_公務電腦使用包/app.py:625-653`
- Modify: `WinPython_公務電腦使用包/app.py:1407-1477`
- Modify: `WinPython_公務電腦使用包/app.py:1688-1716`
- Modify: `WinPython_公務電腦使用包/templates/admin_public_pc.html:307-378`
- Test: `tests/test_web_app.py:1944-2087`
- Test: `tests/test_web_app.py:2629-2962`

**Interfaces:**
- Consumes: `task_completion_snapshot(report)`.
- Produces: `task_completion_label(payload) -> str`.
- Produces: NAS card main state independent from `last_action`.
- Preserves: full historical events and event deduplication.

- [ ] **Step 1: Write failing NAS admin tests**

Add:

```python
def test_admin_main_status_uses_full_completion_not_last_single_site_action(self):
    task = app_module.request_from_form(self.valid_task_data()).to_dict()
    site_statuses = {
        site_key: {
            "status": f"{site_key}_saved",
            "detail": "saved",
        }
        for site_key in ("duty_work_log", "vehicle_mileage", "consumables", "disinfection")
    }
    site_statuses["fuel_record"] = {"status": "not_started", "detail": ""}
    app_module.upsert_public_pc_report({
        "event_id": "single-final-event",
        "task_id": task["task_id"],
        "task": task,
        "title": "最後單站補打",
        "action": "單站補打成功：消毒",
        "status": "disinfection_saved",
        "overall_status": "desktop_fast_completed",
        "site_statuses": site_statuses,
    })

    body = html.unescape(self.client.get("/admin/public-pc").get_data(as_text=True))

    self.assertIn("四站登打完成", body)
    self.assertIn("單站補打成功：消毒", body)

def test_admin_success_filter_excludes_premature_overall_success(self):
    task = app_module.request_from_form(self.valid_task_data()).to_dict()
    app_module.upsert_public_pc_report({
        "event_id": "premature-success",
        "task_id": task["task_id"],
        "task": task,
        "title": "只有消毒完成",
        "action": "單站補打成功：消毒",
        "status": "desktop_fast_completed",
        "overall_status": "desktop_fast_completed",
        "site_statuses": {
            "duty_work_log": {"status": "not_started"},
            "vehicle_mileage": {"status": "not_started"},
            "fuel_record": {"status": "not_started"},
            "consumables": {"status": "not_started"},
            "disinfection": {"status": "disinfection_saved"},
        },
    })

    success_body = html.unescape(
        self.client.get("/admin/public-pc?result=success").get_data(as_text=True)
    )

    self.assertNotIn("只有消毒完成", success_body)
```

- [ ] **Step 2: Run the NAS tests and verify the last-action mismatch**

Run:

```powershell
py -m unittest `
  tests.test_web_app.WebAppTests.test_admin_main_status_uses_full_completion_not_last_single_site_action `
  tests.test_web_app.WebAppTests.test_admin_success_filter_excludes_premature_overall_success -v
```

Expected: the main card still shows the last action, or the premature report enters the success filter.

- [ ] **Step 3: Derive NAS main state and filter from the snapshot**

Add:

```python
def task_completion_label(payload: dict) -> str:
    snapshot = task_completion_snapshot(payload)
    if snapshot["all_complete"]:
        return f"{snapshot['site_count_label']}登打完成"
    return task_progress_summary(payload)


def public_pc_report_result(report: dict) -> str:
    snapshot = task_completion_snapshot(report)
    if snapshot["all_complete"]:
        return "success"
    if snapshot["failed_site_keys"]:
        return "failed"
    return "pending"
```

Add `task_completion_label` to Jinja globals.

Include the computed snapshot when emitting a public-PC report for diagnostics and forward compatibility:

```python
"completion": task_completion_snapshot(payload),
```

Persist `completion` in the report payload:

```python
"completion": task_completion_snapshot({
    "task": task,
    "site_statuses": (
        data.get("site_statuses")
        if isinstance(data.get("site_statuses"), dict)
        else existing.get("site_statuses", {})
    ),
}),
```

Always recompute current labels and filters from `task` plus `site_statuses`; never trust a stale transmitted boolean.

- [ ] **Step 4: Change only the NAS card main badge**

Replace:

```jinja2
<span class="status {{ status_class(item.overall_status) }}">
  {{ item.last_action or status_label(item.overall_status) }}
</span>
```

with:

```jinja2
<span class="status {{ status_class(effective_task_status(item)) }}">
  {{ task_completion_label(item) }}
</span>
```

Keep `event.action` unchanged in the expanded event list so `單站補打成功：消毒` remains visible.

- [ ] **Step 5: Run all web tests**

Run:

```powershell
py -m unittest tests.test_web_app -v
```

Expected: all web tests pass; success filtering requires `all_complete`.

- [ ] **Step 6: Commit Task 5**

```powershell
git add -- `
  'WinPython_公務電腦使用包/app.py' `
  'WinPython_公務電腦使用包/templates/admin_public_pc.html' `
  'tests/test_web_app.py'
git diff --cached --check
git commit -m "fix: show aggregate completion in NAS admin"
```

---

### Task 6: Worker terminal status and completion log

**Files:**
- Modify: `WinPython_公務電腦使用包/worker.py:1229-1282`
- Modify: `WinPython_公務電腦使用包/worker.py:1285-1476`
- Modify: `WinPython_公務電腦使用包/worker.py:1724-2127`
- Modify: `WinPython_公務電腦使用包/worker_gui.py:1426-1498`
- Test: `tests/test_worker.py:2146-2829`
- Test: `tests/test_worker_gui.py:566-632`

**Interfaces:**
- Consumes: `task_completion_snapshot(payload)`.
- Produces: `worker_completion_log_line(payload, task_id) -> str`.
- Produces: `post_site_terminal_status(server_url: str, task_id: str, result_status: str, detail: str) -> None` using `site_run_completed` for successful single-site work.
- Produces GUI/stdout line `四站｜完成｜任務ID` or `五站｜完成｜任務ID` only when the fetched store payload is fully complete.

- [ ] **Step 1: Write failing Worker helper tests**

Add to `WorkerTests`:

```python
def test_worker_completion_log_line_requires_all_sites(self):
    payload = {
        "task": {"task_id": "worker-partial", "vehicle": "新坡92"},
        "site_statuses": {
            "duty_work_log": {"status": "not_started"},
            "vehicle_mileage": {"status": "not_started"},
            "fuel_record": {"status": "not_started"},
            "consumables": {"status": "not_started"},
            "disinfection": {"status": "disinfection_saved"},
        },
    }

    line = worker_module.worker_completion_log_line(payload, "worker-partial")

    self.assertEqual(line, "")

def test_worker_completion_log_line_uses_four_site_label(self):
    payload = {
        "task": {"task_id": "worker-complete", "vehicle": "新坡92"},
        "site_statuses": {
            "duty_work_log": {"status": "duty_work_log_saved"},
            "vehicle_mileage": {"status": "vehicle_mileage_saved"},
            "fuel_record": {"status": "not_started"},
            "consumables": {"status": "consumables_saved"},
            "disinfection": {"status": "disinfection_saved"},
        },
    }

    line = worker_module.worker_completion_log_line(payload, "worker-complete")

    self.assertEqual(line, "四站｜完成｜worker-complete")

def test_worker_completion_log_line_uses_five_site_label_when_fuel_is_enabled(self):
    payload = {
        "task": {
            "task_id": "worker-five-sites",
            "vehicle": "新坡92",
            "fuel_record": {"enabled": True},
        },
        "site_statuses": {
            "duty_work_log": {"status": "duty_work_log_saved"},
            "vehicle_mileage": {"status": "vehicle_mileage_saved"},
            "fuel_record": {"status": "fuel_record_saved"},
            "consumables": {"status": "consumables_saved"},
            "disinfection": {"status": "disinfection_saved"},
        },
    }

    line = worker_module.worker_completion_log_line(payload, "worker-five-sites")

    self.assertEqual(line, "五站｜完成｜worker-five-sites")

def test_successful_single_site_worker_posts_site_run_completed(self):
    task = AmbulanceReturnRequest(
        task_id="worker-single-site",
        created_at=__import__("datetime").datetime(2026, 7, 17, 12, 0),
        raw_text="",
        vehicle="新坡92",
    ).to_dict()
    result = SimpleNamespace(
        status="duty_work_log_saved",
        detail="saved",
        key="duty_work_log",
        name="工作",
    )
    posts = []
    with mock.patch.object(worker_module, "run_local_selenium_task", return_value=result), \
         mock.patch.object(
             worker_module,
             "post_status",
             side_effect=lambda _url, _task_id, status, _detail, **_kwargs: posts.append(status),
         ), \
         mock.patch.object(
             worker_module,
             "print_worker_completion_if_reached",
             return_value="",
         ):
        worker_module.run_task("http://nas", "worker-a", task, Path("artifacts"))

    self.assertEqual(posts[-1], "site_run_completed")
    self.assertNotIn("desktop_fast_completed", posts)
```

- [ ] **Step 2: Run Worker tests and verify missing helper/premature status**

Run:

```powershell
py -m unittest `
  tests.test_worker.WorkerTests.test_worker_completion_log_line_requires_all_sites `
  tests.test_worker.WorkerTests.test_worker_completion_log_line_uses_four_site_label `
  tests.test_worker.WorkerTests.test_worker_completion_log_line_uses_five_site_label_when_fuel_is_enabled `
  tests.test_worker.WorkerTests.test_successful_single_site_worker_posts_site_run_completed -v
```

Expected: missing helper and current `desktop_fast_completed` assertion failure.

- [ ] **Step 3: Add shared Worker terminal and log helpers**

Import `task_completion_snapshot` and add:

```python
def worker_completion_log_line(payload: dict[str, object], task_id: str) -> str:
    snapshot = task_completion_snapshot(dict(payload or {}))
    if not snapshot["all_complete"]:
        return ""
    return f"{snapshot['site_count_label']}｜完成｜{task_id}"


def print_worker_completion_if_reached(server_url: str, task_id: str) -> str:
    payload = fetch_task_payload(server_url, task_id)
    line = worker_completion_log_line(payload or {}, task_id)
    if line:
        print(line, flush=True)
    return line


def post_site_terminal_status(
    server_url: str,
    task_id: str,
    result_status: str,
    detail: str,
) -> None:
    blocked = _status_blocks_progress(result_status)
    post_status(
        server_url,
        task_id,
        "desktop_fast_completed_with_errors" if blocked else "site_run_completed",
        detail,
    )
    if not blocked:
        print_worker_completion_if_reached(server_url, task_id)
```

For consumables call the helper with `result.status`; `_status_blocks_progress()` already handles failed and waiting states.

- [ ] **Step 4: Replace five single-site global-success posts**

In `run_task()`, `run_vehicle_task()`, `run_fuel_worker_task()`, `run_disinfection_worker_task()`, and `run_consumables_worker_task()`, replace each `if update_overall:` block with:

```python
if update_overall:
    post_site_terminal_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
    )
```

This leaves each site result post unchanged and preserves error status delivery.

After the successful final post in `_run_all_sites_task_impl()`:

```python
post_status(
    server_url,
    request.task_id,
    "desktop_fast_completed",
    f"公務電腦 worker {site_count_label}登打完成。",
)
print_worker_completion_if_reached(server_url, request.task_id)
```

- [ ] **Step 5: Make manual Worker GUI display the reconciled completion line**

After the terminal post in `_run_selected_all_sites_with_lease()`:

```python
payload = worker.fetch_task_payload(server_url, task_id) or {}
completion_line = worker.worker_completion_log_line(payload, task_id)
if completion_line:
    self.log_queue.put(completion_line)
else:
    self.log_queue.put(f"{site_count_label}登打流程結束：{task_id}")
```

Do not construct a completion line from `blocked_site == ""`; only fetched site statuses may authorize it.

- [ ] **Step 6: Add Worker GUI success and partial tests**

Extend the existing manual multi-site tests:

```python
def test_manual_all_sites_logs_four_site_completion_from_fetched_payload(self):
    task = {
        "task_id": "gui-complete",
        "created_at": "2026-07-17T12:00:00",
        "vehicle": "新坡92",
    }
    complete_payload = {
        "task": task,
        "worker_queue": {
            "status": "claimed",
            "claim_id": "claim-complete",
            "worker_id": "PC-01",
        },
        "site_statuses": {
            "duty_work_log": {"status": "duty_work_log_saved"},
            "vehicle_mileage": {"status": "vehicle_mileage_saved"},
            "fuel_record": {"status": "not_started"},
            "consumables": {"status": "consumables_saved"},
            "disinfection": {"status": "disinfection_saved"},
        },
    }
    gui = self._manual_gui_stub(
        _run_selected_task_background=mock.Mock(),
        _run_selected_vehicle_mileage_background=mock.Mock(),
        _run_selected_fuel_record_background=mock.Mock(),
        _run_selected_consumables_background=mock.Mock(),
        _run_selected_disinfection_background=mock.Mock(),
    )
    event = __import__("threading").Event()
    posts = []
    with mock.patch.object(
        worker_gui.worker,
        "begin_manual_task_execution",
        return_value=event,
    ), mock.patch.object(
        worker_gui.worker,
        "end_manual_task_execution",
    ), mock.patch.object(
        worker_gui.worker,
        "_start_worker_claim_heartbeat",
        return_value=lambda: None,
    ), mock.patch.object(
        worker_gui.worker,
        "claim_task",
        return_value=task,
    ), mock.patch.object(
        worker_gui.worker,
        "fetch_task_payload",
        return_value=complete_payload,
    ), mock.patch.object(
        worker_gui.worker,
        "post_status",
        side_effect=lambda _server, _task, status, _detail, **_kwargs: posts.append(status),
    ):
        worker_gui.WorkerGui._run_selected_all_sites_background(
            gui,
            task["task_id"],
        )

    logs = []
    while True:
        try:
            logs.append(gui.log_queue.get_nowait())
        except queue.Empty:
            break
    self.assertEqual(posts[-1], "desktop_fast_completed")
    self.assertIn("四站｜完成｜gui-complete", logs)

def test_manual_all_sites_does_not_log_completion_for_partial_payload(self):
    partial_payload = {
        "task": {"task_id": "gui-partial", "vehicle": "新坡92"},
        "site_statuses": {
            "duty_work_log": {"status": "duty_work_log_saved"},
            "vehicle_mileage": {"status": "vehicle_mileage_saved"},
            "fuel_record": {"status": "not_started"},
            "consumables": {"status": "consumables_failed"},
            "disinfection": {"status": "not_started"},
        },
    }
    line = worker.worker_completion_log_line(partial_payload, "gui-partial")
    self.assertEqual(line, "")
```

- [ ] **Step 7: Run Worker and Worker GUI tests**

Run:

```powershell
py -m unittest tests.test_worker tests.test_worker_gui -v
```

Expected: all Worker tests pass; no single-site path posts global success.

- [ ] **Step 8: Commit Task 6**

```powershell
git add -- `
  'WinPython_公務電腦使用包/worker.py' `
  'WinPython_公務電腦使用包/worker_gui.py' `
  'tests/test_worker.py' `
  'tests/test_worker_gui.py'
git diff --cached --check
git commit -m "fix: log worker completion from aggregate state"
```

---

### Task 7: Full verification, package parity, release, and deployment

**Files:**
- Modify through package build: `WinPython_公務電腦使用包/VERSION.txt`
- Generate: `UPDATE/NAS包/`
- Generate: `UPDATE/ambulance-return-version.txt`
- Generate: `UPDATE/ambulance-return-public-package.zip`
- Generate: `UPDATE/ambulance-return-public-package.zip.sha256.txt`

**Interfaces:**
- Consumes all previous tasks.
- Produces matching public-duty and NAS packages from the same source.
- Produces remote release version, ZIP version, and SHA256 parity evidence.

- [ ] **Step 1: Review the final diff against the approved specification**

Run:

```powershell
git status --short --branch
git diff --check
git diff -- `
  'WinPython_公務電腦使用包/ambulance_bot/task_store.py' `
  'WinPython_公務電腦使用包/ambulance_bot/task_edit_impact.py' `
  'WinPython_公務電腦使用包/ambulance_bot/desktop_fast_runner.py' `
  'WinPython_公務電腦使用包/app.py' `
  'WinPython_公務電腦使用包/worker.py' `
  'WinPython_公務電腦使用包/worker_gui.py' `
  'WinPython_公務電腦使用包/templates/task_detail.html' `
  'WinPython_公務電腦使用包/templates/new_task.html' `
  'WinPython_公務電腦使用包/templates/admin_public_pc.html' `
  'tests'
```

Expected: no whitespace errors; no generated NAS source edits; unrelated pre-existing changes remain identifiable and unstaged.

- [ ] **Step 2: Run targeted test modules**

```powershell
py -m unittest `
  tests.test_task_edit_impact `
  tests.test_task_store `
  tests.test_web_app `
  tests.test_desktop_fast_runner `
  tests.test_worker `
  tests.test_worker_gui -v
```

Expected: zero failures and zero errors.

- [ ] **Step 3: Compile compatibility files and runtime modules**

```powershell
$files = @(
  'app.py',
  'worker.py',
  'worker_gui.py',
  'consumables_login.py',
  'disinfect.py',
  '_runtime_loader.py'
) + (
  Get-ChildItem -LiteralPath 'WinPython_公務電腦使用包\ambulance_bot' -Filter '*.py' |
    ForEach-Object { $_.FullName }
)
py -m py_compile @files
```

Expected: exit code 0 with no traceback.

- [ ] **Step 4: Run the full test suite**

```powershell
py -m unittest discover -s tests -v
```

Expected: zero failures and zero errors.

- [ ] **Step 5: Build both packages with one version**

```powershell
$releaseVersion = Get-Date -Format 'yyyy.MM.dd.HHmm'
powershell -ExecutionPolicy Bypass -File scripts\build_all_packages.ps1 -Version $releaseVersion
```

Expected: `WinPython_公務電腦使用包\VERSION.txt`, `UPDATE\VERSION.txt`, `UPDATE\NAS包\VERSION.txt`, both ZIP-internal version files, and `UPDATE\ambulance-return-version.txt` all equal `$releaseVersion`; SHA256 verification succeeds inside the script.

- [ ] **Step 6: Commit the generated version change without generated package output**

```powershell
git add -- 'WinPython_公務電腦使用包/VERSION.txt'
git diff --cached --check
git commit -m "chore: release ambulance worker $releaseVersion"
```

Do not stage `UPDATE\NAS包`, release ZIP files, task JSON, logs, screenshots, credentials, or `.env`.

- [ ] **Step 7: Reconcile the branch with the remote before publishing**

```powershell
git status --short --branch
$dirty = git status --porcelain
if ($dirty) {
  throw "Release gate: user-owned working-tree changes remain. Resolve their ownership before rebase or push."
}
git fetch origin
git rebase origin/master
```

Expected: the release gate confirms a clean tree, then the rebase completes. The current workspace is dirty at plan-writing time, so execution must explicitly resolve the ownership of those pre-existing changes before this step. If any conflict occurs, stop the rebase at the conflict and preserve both the user’s existing changes and the task-completion implementation; do not use `git reset --hard`.

- [ ] **Step 8: Push and publish the release tag**

```powershell
$releaseVersion = (
  Get-Content -LiteralPath 'WinPython_公務電腦使用包\VERSION.txt' -Raw -Encoding utf8
).Trim().TrimStart([char]0xFEFF)
$tag = "ambulance-return-$releaseVersion"
git push origin master
git tag -a $tag -m $tag
git push origin $tag
$runId = gh run list `
  --workflow release.yml `
  --limit 1 `
  --json databaseId `
  --jq '.[0].databaseId'
if (-not $runId) { throw "Release workflow run was not found" }
gh run watch $runId --exit-status
```

Expected: branch push succeeds and the `Release public computer package` workflow for `$tag` finishes successfully before remote assets are downloaded.

- [ ] **Step 9: Verify GitHub release version, ZIP content, and SHA256**

```powershell
$verifyDir = Join-Path $env:TEMP ("ambulance-release-verify-" + $releaseVersion)
New-Item -ItemType Directory -Path $verifyDir -Force | Out-Null
gh release download $tag `
  --repo seaflun/ambulance-return-bot `
  --dir $verifyDir `
  --pattern 'ambulance-return-version.txt' `
  --pattern 'ambulance-return-public-package.zip' `
  --pattern 'ambulance-return-public-package.zip.sha256.txt'
$remoteVersion = (
  Get-Content -LiteralPath (Join-Path $verifyDir 'ambulance-return-version.txt') -Raw -Encoding utf8
).Trim().TrimStart([char]0xFEFF)
$zipPath = Join-Path $verifyDir 'ambulance-return-public-package.zip'
$expectedHash = (
  Get-Content -LiteralPath (Join-Path $verifyDir 'ambulance-return-public-package.zip.sha256.txt') -Raw -Encoding utf8
).Trim().Split()[0].ToLowerInvariant()
$actualHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$extractDir = Join-Path $verifyDir 'zip'
Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
$zipVersion = (
  Get-Content -LiteralPath (
    Join-Path $extractDir 'WinPython_公務電腦使用包\VERSION.txt'
  ) -Raw -Encoding utf8
).Trim().TrimStart([char]0xFEFF)
if ($remoteVersion -ne $releaseVersion) { throw "Remote version mismatch" }
if ($zipVersion -ne $releaseVersion) { throw "ZIP version mismatch" }
if ($actualHash -ne $expectedHash) { throw "Remote SHA256 mismatch" }
```

Expected: all three checks complete without throwing.

- [ ] **Step 10: Deploy NAS output and restart the Flask container**

Copy the complete contents of `UPDATE\NAS包\` to `/docker/ambulance_return_bot/` through DSM File Station, replacing files with matching names but preserving NAS `.env`, task JSON, reports, and runtime artifacts.

Then run:

```powershell
ssh -i "$env:USERPROFILE\.ssh\id_ed25519_ambulance_nas" `
  -o IdentitiesOnly=yes `
  -o BatchMode=yes `
  -o ConnectTimeout=8 `
  codex_restart@100.114.126.58 `
  "sudo -n /usr/local/bin/docker restart ambulance-app-1"
Start-Sleep -Seconds 15
Invoke-RestMethod -Uri 'http://100.114.126.58:8080/status' -TimeoutSec 20
ssh -i "$env:USERPROFILE\.ssh\id_ed25519_ambulance_nas" `
  -o IdentitiesOnly=yes `
  -o BatchMode=yes `
  -o ConnectTimeout=8 `
  codex_restart@100.114.126.58 `
  "sudo -n /usr/local/bin/docker ps --format '{{.Names}} {{.Status}}'"
```

Expected: `/status` succeeds and `ambulance-app-1` reports a healthy running state.

- [ ] **Step 11: Restart the local Worker and verify the App**

```powershell
$procs = Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -and
  $_.CommandLine -match 'ambulance_return_bot|worker_gui.py|worker.py|app.py|救護返隊小幫手' -and
  $_.Name -notmatch 'powershell'
}
foreach ($process in $procs) {
  Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
Start-Process -FilePath 'wscript.exe' -ArgumentList (
  '"I:\我的雲端硬碟\專案\救護返隊小幫手\ambulance_return_bot\WinPython_公務電腦使用包\run_worker_forever.vbs"'
)
Start-Sleep -Seconds 7
Invoke-WebRequest -Uri 'http://127.0.0.1:8090/app' -UseBasicParsing -TimeoutSec 5
```

Expected: local App returns HTTP 200 and the Worker GUI uses the new package version.

- [ ] **Step 12: Perform three live acceptance cases**

Use disposable test tasks and verify:

1. Four active sites all succeed: App, NAS admin, and Worker show `四站登打完成`; all entry buttons disappear.
2. Two sites fail, then each is retried individually: the first retry remains partial; the second retry changes the task to `四站登打完成`; the operation history still says `單站補打成功：站別`.
3. A completed two-vehicle task has only the second vehicle mileage edited: App shows `第 2 車里程` and `里程（只重登第 2 車）`; the first vehicle checkpoint is retained; after the second vehicle is saved, completion returns and buttons disappear.

Delete only disposable test tasks through the normal task deletion route after recording the results. Do not delete runtime directories or task storage recursively.
