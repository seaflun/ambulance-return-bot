# Recent Task Vehicle Title Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Append one or two task vehicle names to each recent-task title on the ambulance entry page.

**Architecture:** Keep the shared `task_title()` output unchanged and compose the page-specific vehicle suffix in `new_task.html`. Reuse `task_vehicle_display_entries()` so legacy single-vehicle tasks and current two-vehicle tasks follow the same normalized data path.

**Tech Stack:** Python 3, Flask, Jinja2, `unittest`

## Global Constraints

- Single-vehicle tasks display only the primary vehicle.
- Tasks with `two_vehicle` enabled display both vehicles in task order, separated by `、`.
- Missing vehicle names do not produce an empty suffix.
- Preserve unrelated worktree changes and do not delete files.
- Do not commit unless the user explicitly requests a commit.

---

### Task 1: Add recent-task vehicle title coverage

**Files:**
- Modify: `tests/test_web_app.py:1627`

**Interfaces:**
- Consumes: `WebAppTests.valid_task_data(**overrides)`, `app_module.request_from_form(form)`, `TaskStore.create(request)`, and `GET /app`.
- Produces: Regression coverage for the exact single-vehicle and two-vehicle recent-task anchor text.

- [ ] **Step 1: Write the failing test**

```python
    def test_app_page_recent_task_titles_show_one_or_two_vehicles(self):
        address = "桃園市觀音區崙坪三路126號1樓(OHCA-N)"
        single = self.store.create(
            app_module.request_from_form(
                self.valid_task_data(
                    case_id="case-single-vehicle-title",
                    case_reason="空跑",
                    case_address=address,
                    vehicle="新坡92",
                )
            )
        )
        double = self.store.create(
            app_module.request_from_form(
                self.valid_task_data(
                    case_id="case-two-vehicle-title",
                    case_reason="空跑",
                    case_address=address,
                    vehicle="新坡92",
                    two_vehicle="1",
                    vehicle_2="新坡93",
                    driver_2="陳小華",
                    mileage_2="200",
                    return_time_2="1130",
                    patient_summary_2="女一名",
                    consumables_2="桃-口罩(片)=2",
                )
            )
        )

        body = html.unescape(self.client.get("/app").data.decode("utf-8"))

        title = f"緊急救護-空跑 - {address}"
        self.assertIn(
            f'<a class="recent-title" href="/tasks/{single["task"]["task_id"]}">{title} - 新坡92</a>',
            body,
        )
        self.assertIn(
            f'<a class="recent-title" href="/tasks/{double["task"]["task_id"]}">{title} - 新坡92、新坡93</a>',
            body,
        )
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```powershell
py -m unittest tests.test_web_app.WebAppTests.test_app_page_recent_task_titles_show_one_or_two_vehicles -v
```

Expected: `FAIL` because the recent-task anchor still ends after the address.

### Task 2: Append normalized vehicles in the recent-task template

**Files:**
- Modify: `WinPython_公務電腦使用包/templates/new_task.html:742`
- Test: `tests/test_web_app.py`

**Interfaces:**
- Consumes: Jinja global `task_vehicle_display_entries(task)`, where each returned entry provides a `vehicle` value.
- Produces: Recent-task anchor text ending in ` - 車輛` for one vehicle or ` - 車輛一、車輛二` for two vehicles.

- [ ] **Step 1: Write the minimal template implementation**

```jinja2
              <a class="recent-title" href="/tasks/{{ item.task.task_id }}">{{ task_title(item.task) }}{% for entry in task_vehicle_display_entries(item.task) if entry.vehicle %}{{ ' - ' if loop.first else '、' }}{{ entry.vehicle }}{% endfor %}</a>
```

- [ ] **Step 2: Run the focused test to verify GREEN**

Run:

```powershell
py -m unittest tests.test_web_app.WebAppTests.test_app_page_recent_task_titles_show_one_or_two_vehicles -v
```

Expected: `OK` with 1 test passing.

- [ ] **Step 3: Run the existing adjacent recent-task test**

Run:

```powershell
py -m unittest tests.test_web_app.WebAppTests.test_app_page_recent_task_does_not_show_delete_button tests.test_web_app.WebAppTests.test_app_page_recent_tasks_keeps_only_completed_last_48_hours -v
```

Expected: `OK` with 2 tests passing.

- [ ] **Step 4: Run the full web test module**

Run:

```powershell
py -m unittest tests.test_web_app -v
```

Expected: all tests pass with no failures or errors.

- [ ] **Step 5: Review the scoped diff**

Run:

```powershell
git diff --check
git diff -- tests/test_web_app.py WinPython_公務電腦使用包/templates/new_task.html
```

Expected: no whitespace errors; only the new test and the recent-title template line change are present.
