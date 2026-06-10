import html
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path

import app as app_module
import ambulance_bot.selenium_local as selenium_local_module
from ambulance_bot.selenium_local import DutyCaseLookupResult
from ambulance_bot.task_runner import TaskRunner
from ambulance_bot.task_store import JsonTaskStore


class FakeDesktopRunner:
    def __init__(self, store):
        self.store = store
        self.started: list[str] = []
        self.started_sites: list[tuple[str, str]] = []

    def start_existing(self, task_id: str) -> str:
        self.started.append(task_id)
        self.store.set_overall_status(task_id, "desktop_fast_running", "本機快速執行已啟動。")
        return task_id

    def start_site(self, task_id: str, site_key: str) -> str:
        self.started_sites.append((task_id, site_key))
        self.store.set_overall_status(task_id, "desktop_fast_running", f"{site_key} running")
        return task_id

    def wait_for_idle(self, timeout_seconds: float = 5.0) -> bool:
        return True


class WebAppTests(unittest.TestCase):
    def setUp(self):
        os.environ["OPEN_LOCAL_BROWSER_ON_RUN"] = "false"
        os.environ["USE_LOCAL_SELENIUM"] = "false"
        self.tmp = tempfile.TemporaryDirectory()
        self.original_worker_token = os.environ.get("WORKER_TOKEN")
        self.original_desktop_fast_mode = os.environ.get("DESKTOP_FAST_MODE")
        self.original_task_execution_mode = os.environ.get("TASK_EXECUTION_MODE")
        self.original_public_pc_report_enabled = os.environ.get("PUBLIC_PC_REPORT_ENABLED")
        self.original_start_local_case_lookup = app_module.start_local_case_lookup
        self.original_local_host_candidates = app_module.local_host_candidates
        self.original_query_duty_emergency_cases = selenium_local_module.query_duty_emergency_cases
        os.environ["WORKER_TOKEN"] = ""
        os.environ["DESKTOP_FAST_MODE"] = "0"
        os.environ["TASK_EXECUTION_MODE"] = "worker_queue"
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "false"
        self.original_artifacts_dir = app_module.artifacts_dir
        app_module.artifacts_dir = Path(self.tmp.name)
        self.store = JsonTaskStore(Path(self.tmp.name) / "tasks")
        app_module.store = self.store
        app_module.runner = TaskRunner(Path(self.tmp.name), store=self.store)
        app_module.desktop_runner = FakeDesktopRunner(self.store)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.runner.wait_for_idle()
        app_module.desktop_runner.wait_for_idle()
        app_module.artifacts_dir = self.original_artifacts_dir
        if self.original_worker_token is None:
            os.environ.pop("WORKER_TOKEN", None)
        else:
            os.environ["WORKER_TOKEN"] = self.original_worker_token
        if self.original_desktop_fast_mode is None:
            os.environ.pop("DESKTOP_FAST_MODE", None)
        else:
            os.environ["DESKTOP_FAST_MODE"] = self.original_desktop_fast_mode
        if self.original_task_execution_mode is None:
            os.environ.pop("TASK_EXECUTION_MODE", None)
        else:
            os.environ["TASK_EXECUTION_MODE"] = self.original_task_execution_mode
        if self.original_public_pc_report_enabled is None:
            os.environ.pop("PUBLIC_PC_REPORT_ENABLED", None)
        else:
            os.environ["PUBLIC_PC_REPORT_ENABLED"] = self.original_public_pc_report_enabled
        app_module.start_local_case_lookup = self.original_start_local_case_lookup
        app_module.local_host_candidates = self.original_local_host_candidates
        selenium_local_module.query_duty_emergency_cases = self.original_query_duty_emergency_cases
        self.tmp.cleanup()

    def valid_task_data(self, **overrides):
        data = {
            "vehicle": "\u65b0\u576191",
            "driver": "\u66fe\u5f65\u7db8",
            "mileage": "12345",
            "case_date": "2026-06-07",
            "case_time": "1024",
            "return_date": "2026-06-07",
            "return_time": "1119",
            "case_reason": "\u6025\u75c5",
            "patient_summary": "\u7537\u4e00\u540d",
        }
        data.update(overrides)
        return data

    def test_app_page_loads(self):
        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertIn("救護返隊小幫手", body)
        self.assertIn("救護車設定", body)
        self.assertNotIn('href="/admin/public-pc"', body)
        self.assertNotIn("公務後台", body)
        self.assertIn('id="task-form" autocomplete="off" novalidate', body)
        self.assertIn("\u65b0\u576191", body)
        self.assertIn(">\u5433\u5b97\u8015</option>", body)
        self.assertNotIn("6 : \u5433\u5b97\u8015", body)
        self.assertIn('value="\u7121"', body)
        self.assertNotIn('placeholder="1420"', body)
        self.assertNotIn('placeholder="1505"', body)
        self.assertNotIn('placeholder="12345"', body)
        self.assertIn(">\u8acb\u9078\u64c7</option>", body)
        self.assertIn("查詢案件", body)
        self.assertNotIn("查詢24小時案件", body)
        self.assertNotIn('button.textContent = "查詢中"', body)
        self.assertIn(".form-section-divider { border-top: 1px solid var(--line); margin-top: 18px; padding-top: 18px; }", body)
        self.assertIn('<section class="consumables form-section-divider">', body)
        self.assertIn('<label class="form-section-divider">消毒項目</label>', body)
        self.assertIn('name="case_address"', body)
        self.assertNotIn('name="work_note"', body)
        self.assertIn("const defaultConsumables = {};", body)
        self.assertNotIn(" checked", body)
        self.assertIn("main { max-width: 1080px;", body)
        self.assertIn(".check-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));", body)
        self.assertIn(".case-card button { min-width: 76px; min-height: 44px;", body)
        self.assertIn('#task-form > button[type="submit"] { width: 100%; min-height: 54px;', body)
        self.assertIn("repeating-linear-gradient", body)

    def test_nas_app_page_shows_public_pc_admin_button(self):
        response = self.client.get("/app", headers={"Host": "100.114.126.58:8080"})

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertIn("救護車設定", body)
        self.assertIn('href="/admin/public-pc"', body)
        self.assertIn("公務後台", body)
        self.assertIn('class="header-actions"', body)

    def test_app_page_recent_task_does_not_show_delete_button(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data(), follow_redirects=False)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        app_response = self.client.get("/app")
        body = html.unescape(app_response.data.decode("utf-8"))
        self.assertIn(f'href="/tasks/{task_id}"', body)
        self.assertNotIn(f'action="/tasks/{task_id}/delete"', body)
        self.assertNotIn('aria-label="刪除案件"', body)

    def test_edit_page_hides_clear_button(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data(case_address="桃園市觀音區"), follow_redirects=False)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}/edit")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("救護返隊小幫手-編輯狀態", body)
        self.assertNotIn('formaction="/cases/clear"', body)
        self.assertNotIn(">清除</button>", body)

    def test_consumable_quantity_spinner_is_hidden(self):
        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn(".consumable-qty::-webkit-inner-spin-button", body)
        self.assertIn("appearance: textfield", body)

    def test_create_task_writes_json_and_redirects(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                case_time="1420",
                return_time="1505",
                case_address="\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                case_reason="\u6025\u75c5",
                patient_summary="\u7537\u4e00\u540d",
                consumables="\u53e3\u7f69=2,\u624b\u5957=2",
            ),
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        tasks = self.store.list_recent()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task"]["vehicle"], "\u65b0\u576191")
        self.assertEqual(tasks[0]["task"]["case_time"], "1420")
        self.assertEqual(tasks[0]["task"]["case_address"], "\u6843\u5712\u5e02\u89c0\u97f3\u5340")
        self.assertEqual(tasks[0]["task"]["case_reason"], "\u6025\u75c5")

    def test_create_task_requires_vehicle_driver_mileage_return_time_and_patient(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(vehicle="", driver="", mileage="", return_time="", patient_summary=""),
            follow_redirects=False,
        )
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_recent(), [])
        self.assertIn('<div class="form-errors" role="alert">', body)
        expected_order = ["請填寫返隊時間", "請選擇出動車輛", "請選擇司機", "請選擇傷病患", "請填寫里程"]
        positions = [body.index(message) for message in expected_order]
        self.assertEqual(positions, sorted(positions))

    def test_admin_vehicle_create_adds_vehicle_option(self):
        page = self.client.get("/admin/vehicles")
        page_body = html.unescape(page.data.decode("utf-8"))
        self.assertIn("救護車設定", page_body)
        self.assertIn("救護車代號", page_body)
        self.assertIn("返回首頁", page_body)
        self.assertIn('<button type="submit">新增</button>', page_body)
        self.assertIn("目前車輛", page_body)
        self.assertIn('<div class="vehicle-label">救護車代號</div>', page_body)
        self.assertIn("車牌號碼", page_body)
        self.assertIn("header-row", page_body)
        self.assertNotIn("新增或更新", page_body)
        self.assertNotIn("返回 APP", page_body)
        self.assertNotIn('placeholder="新坡95"', page_body)
        self.assertNotIn('placeholder="BPE-5951"', page_body)

        response = self.client.post(
            "/admin/vehicles",
            data={"label": "新坡96", "ppe_name": "BPE-5960"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        settings_path = Path(self.tmp.name) / "settings" / "vehicles.json"
        self.assertIn("新坡96", settings_path.read_text(encoding="utf-8"))
        app_response = self.client.get("/app")
        body = html.unescape(app_response.data.decode("utf-8"))
        self.assertIn('<option value="新坡96">新坡96</option>', body)
        self.assertIn("BPE-5960", html.unescape(response.data.decode("utf-8")))

    def test_admin_vehicle_delete_removes_custom_vehicle_only(self):
        response = self.client.post("/admin/vehicles/delete", data={"label": "新坡95"}, follow_redirects=False)
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("已刪除 新坡95", body)
        app_response = self.client.get("/app")
        self.assertNotIn("新坡95", html.unescape(app_response.data.decode("utf-8")))

        builtin_response = self.client.post("/admin/vehicles/delete", data={"label": "新坡91"}, follow_redirects=False)
        builtin_body = html.unescape(builtin_response.data.decode("utf-8"))

        self.assertEqual(builtin_response.status_code, 400)
        self.assertIn("內建救護車不能刪除", builtin_body)
        self.assertIn("新坡91", builtin_body)

    def test_admin_public_pc_receives_and_lists_local_task_events(self):
        response = self.client.post(
            "/worker/public-pc-task-events",
            json={
                "event_id": "evt-1",
                "task_id": "local-task-1",
                "task": {
                    "task_id": "local-task-1",
                    "case_reason": "急病",
                    "case_address": "桃園市觀音區中山路",
                    "vehicle": "新坡91",
                },
                "user": "8番 曾彥綸 - tyfd01510",
                "worker_id": "public-duty-pc",
                "action": "四站登打成功",
                "status": "desktop_fast_completed",
                "detail": "本機快速執行完成。",
                "overall_status": "desktop_fast_completed",
                "site_statuses": {
                    "duty_work_log": {"status": "duty_work_log_saved"},
                    "vehicle_mileage": {"status": "vehicle_mileage_saved"},
                    "disinfection": {"status": "disinfection_saved"},
                    "consumables": {"status": "consumables_saved"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ack_id"], "evt-1")
        page = self.client.get("/admin/public-pc")
        body = html.unescape(page.data.decode("utf-8"))
        self.assertIn("公務電腦後台", body)
        self.assertIn("8番 曾彥綸 - tyfd01510", body)
        self.assertIn("緊急救護-急病 - 桃園市觀音區中山路", body)
        self.assertIn("四站登打成功", body)

    def test_admin_public_pc_deduplicates_same_event_id(self):
        payload = {
            "event_id": "evt-dedupe-1",
            "task_id": "local-task-2",
            "task": {
                "task_id": "local-task-2",
                "case_reason": "?亦?",
                "case_address": "獢?撣??喳?",
            },
            "action": "???餅???",
            "status": "desktop_fast_completed",
        }

        first = self.client.post("/worker/public-pc-task-events", json=payload)
        second = self.client.post("/worker/public-pc-task-events", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        reports = app_module.public_pc_reports()
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(reports[0]["events"]), 1)
        self.assertEqual(reports[0]["events"][0]["event_id"], "evt-dedupe-1")

    def test_public_pc_report_is_queued_on_failure_and_flushed_on_next_success(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        os.environ["PUBLIC_PC_REPORT_SERVER_URL"] = "http://nas.test"
        original_post = app_module._post_public_pc_report
        sent_payloads: list[dict] = []
        try:
            calls = {"count": 0}

            def fake_post(server_url: str, payload: dict) -> dict:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise urllib.error.URLError("offline")
                sent_payloads.append(payload)
                return {"ack_id": payload["event_id"]}

            app_module._post_public_pc_report = fake_post

            task_payload = {
                "task": {"task_id": "task-1", "case_reason": "急病", "case_address": "桃園市"},
                "overall_status": "created",
                "site_statuses": {},
                "events": [{"status": "created", "detail": "任務已建立。"}],
                "created_at": "2026-06-09T00:00:00",
            }
            app_module.report_public_pc_task_event(task_payload, "建立任務")
            self.assertTrue(app_module.public_pc_pending_report_file().exists())

            task_payload["events"].append({"status": "desktop_fast_running", "detail": "本機快速執行已啟動。"})
            app_module.report_public_pc_task_event(task_payload, "按下四站登打")
        finally:
            app_module._post_public_pc_report = original_post
            os.environ.pop("PUBLIC_PC_REPORT_SERVER_URL", None)

        self.assertEqual(len(sent_payloads), 2)
        self.assertFalse(app_module.public_pc_pending_report_file().exists())
        self.assertEqual(sent_payloads[0]["action"], "建立任務")
        self.assertEqual(sent_payloads[1]["action"], "按下四站登打")

        self.assertNotEqual(sent_payloads[0]["event_id"], sent_payloads[1]["event_id"])

    def test_public_pc_report_keeps_only_unacked_entries_pending(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        os.environ["PUBLIC_PC_REPORT_SERVER_URL"] = "http://nas.test"
        original_post = app_module._post_public_pc_report
        sent_payloads: list[dict] = []
        try:
            app_module._write_pending_public_pc_reports(
                [
                    {"event_id": "evt-old-1", "task_id": "task-old-1", "action": "old-1"},
                    {"event_id": "evt-old-2", "task_id": "task-old-2", "action": "old-2"},
                ]
            )

            def fake_post(server_url: str, payload: dict) -> dict:
                sent_payloads.append(payload)
                if payload["event_id"] == "evt-old-1":
                    return {"ack_id": "evt-old-1"}
                if payload["event_id"] == "evt-old-2":
                    return {"ack_id": "wrong-ack"}
                return {"ack_id": payload["event_id"]}

            app_module._post_public_pc_report = fake_post

            task_payload = {
                "task": {"task_id": "task-new", "case_reason": "急病", "case_address": "桃園市觀音區"},
                "overall_status": "created",
                "site_statuses": {},
                "events": [{"status": "created", "detail": "建立"}],
                "created_at": "2026-06-09T00:00:00",
            }
            app_module.report_public_pc_task_event(task_payload, "建立任務")
        finally:
            app_module._post_public_pc_report = original_post
            os.environ.pop("PUBLIC_PC_REPORT_SERVER_URL", None)

        self.assertEqual([item["event_id"] for item in sent_payloads], ["evt-old-1", "evt-old-2"])
        remaining = app_module._load_pending_public_pc_reports()
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0]["event_id"], "evt-old-2")
        self.assertEqual(remaining[0]["action"], "old-2")
        self.assertEqual(remaining[1]["task_id"], "task-new")
        self.assertEqual(remaining[1]["action"], "建立任務")

    def test_public_pc_report_removes_acked_entries_when_later_send_fails(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        os.environ["PUBLIC_PC_REPORT_SERVER_URL"] = "http://nas.test"
        original_post = app_module._post_public_pc_report
        sent_payloads: list[dict] = []
        try:
            app_module._write_pending_public_pc_reports(
                [
                    {"event_id": "evt-acked", "task_id": "task-old-1", "action": "acked"},
                    {"event_id": "evt-fails", "task_id": "task-old-2", "action": "fails"},
                ]
            )

            def fake_post(server_url: str, payload: dict) -> dict:
                sent_payloads.append(payload)
                if payload["event_id"] == "evt-acked":
                    return {"ack_id": "evt-acked"}
                raise urllib.error.URLError("offline after first ack")

            app_module._post_public_pc_report = fake_post

            task_payload = {
                "task": {"task_id": "task-new", "case_reason": "急病", "case_address": "桃園市觀音區"},
                "overall_status": "created",
                "site_statuses": {},
                "events": [{"status": "created", "detail": "建立"}],
                "created_at": "2026-06-09T00:00:00",
            }
            app_module.report_public_pc_task_event(task_payload, "建立任務")
        finally:
            app_module._post_public_pc_report = original_post
            os.environ.pop("PUBLIC_PC_REPORT_SERVER_URL", None)

        self.assertEqual([item["event_id"] for item in sent_payloads], ["evt-acked", "evt-fails"])
        remaining = app_module._load_pending_public_pc_reports()
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0]["event_id"], "evt-fails")
        self.assertEqual(remaining[0]["action"], "fails")
        self.assertEqual(remaining[1]["task_id"], "task-new")
        self.assertEqual(remaining[1]["action"], "建立任務")

    def test_create_task_rejects_return_datetime_before_case_datetime(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                case_date="2026-06-08",
                case_time="1024",
                return_date="2026-06-08",
                return_time="0950",
            ),
            follow_redirects=False,
        )
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_recent(), [])
        self.assertIn("返隊日期時間不能早於案件日期時間", body)

    def test_create_task_allows_next_day_return_datetime(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                case_date="2026-06-08",
                case_time="2350",
                return_date="2026-06-09",
                return_time="0010",
            ),
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)

    def test_query_cases_redirects_to_app(self):
        response = self.client.post("/cases/query", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/app")
        request_payload = app_module.read_case_lookup_request()
        self.assertEqual(request_payload["status"], "case_lookup_requested")
        self.assertEqual(request_payload["lookup_range"], "24h")

    def test_query_cases_forces_24h_range(self):
        response = self.client.post("/cases/query", data={"lookup_range": "legacy-range"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        request_payload = app_module.read_case_lookup_request()
        self.assertEqual(request_payload["lookup_range"], "24h")

    def test_app_page_auto_refreshes_while_case_lookup_is_running(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "detail": "已查到 2 筆24小時內案件，並讀取出勤人員。",
                "lookup_range": "24h",
                "cases": [{"case_id": "old-case"}],
            },
        )
        app_module.write_case_lookup_request("24h")

        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("window.location.reload()", body)
        self.assertIn("lookup-status is-visible", body)
        self.assertIn("disabled>查詢案件</button>", body)
        self.assertIn("正在查詢最近 24 小時案件，請稍候。", body)
        self.assertNotIn("已查到 2 筆", body)

    def test_app_page_shows_empty_case_lookup_result(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "detail": "loaded",
                "lookup_range": "24h",
                "cases": [],
            },
        )
        app_module.write_json_atomic(
            cases_dir / "request.json",
            {
                "status": "case_lookup_completed",
                "lookup_range": "24h",
                "case_count": 0,
            },
        )

        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn('class="lookup-message is-empty"', body)
        self.assertIn("查詢完成，最近 24 小時沒有找到案件。", body)
        self.assertNotIn("window.location.reload()", body)

    def test_app_page_shows_loaded_case_lookup_result_message(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "detail": "已查到 2 筆前 24 小時的緊急救護案件，並預先讀取服勤人員。",
                "lookup_range": "24h",
                "cases": [
                    {"case_id": "case-1", "address": "桃園市觀音區", "personnel": ["王小明"]},
                    {"case_id": "case-2", "address": "桃園市新屋區"},
                ],
            },
        )

        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("已查到 2 筆24小時內案件，並讀取出勤人員。", body)
        self.assertNotIn("緊急救護案件", body)
        self.assertIn("出勤人員：王小明", body)
        self.assertNotIn("服勤人員：王小明", body)
        self.assertLess(body.index("已查到 2 筆"), body.index('<div class="case-list">'))

    def test_mobile_layout_keeps_header_action_compact_and_stacks_time_fields(self):
        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("header { align-items: center; flex-direction: row; }", body)
        self.assertIn(".header-actions .button { flex: 0 0 auto;", body)
        self.assertIn(".lookup-form { display: grid; grid-template-columns: 1fr; gap: 8px; width: 100%; }", body)
        self.assertIn(".time-field { grid-template-columns: 1fr; }", body)
        self.assertIn('.return-time-field input[type="date"] { grid-column: 1 / -1; }', body)

    def test_localhost_query_cases_starts_local_lookup_when_fast_mode_auto(self):
        calls = []
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        app_module.start_local_case_lookup = lambda lookup_range: calls.append(lookup_range)

        response = self.client.post("/cases/query", data={"lookup_range": "legacy-range"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(calls, ["24h"])

    def test_local_ip_query_cases_starts_local_lookup_when_fast_mode_auto(self):
        calls = []
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        app_module.local_host_candidates = lambda: {"192.168.50.23"}
        app_module.start_local_case_lookup = lambda lookup_range: calls.append(lookup_range)

        response = self.client.post(
            "/cases/query",
            base_url="http://192.168.50.23:8091",
            data={"lookup_range": "24h"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(calls, ["24h"])

    def test_query_cases_does_not_start_local_lookup_when_fast_mode_disabled(self):
        calls = []
        os.environ["DESKTOP_FAST_MODE"] = "0"
        app_module.start_local_case_lookup = lambda lookup_range: calls.append(lookup_range)

        response = self.client.post("/cases/query", data={"lookup_range": "legacy-range"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(calls, [])

    def test_run_local_case_lookup_writes_cases_and_completes_request(self):
        def fake_query(artifacts_dir: Path, lookup_range: str = "24h") -> DutyCaseLookupResult:
            cases = [{"case_id": "case-1", "address": "addr"}]
            payload = {
                "status": "cases_loaded",
                "detail": "loaded",
                "updated_at": "2026-06-07T20:00:00",
                "cases": cases,
            }
            path = artifacts_dir / "cases" / "latest.json"
            app_module.write_json_atomic(path, payload)
            return DutyCaseLookupResult(True, "cases_loaded", "loaded", cases, path)

        selenium_local_module.query_duty_emergency_cases = fake_query
        app_module.write_case_lookup_request("24h")

        app_module.run_local_case_lookup("24h")

        latest = app_module.read_case_lookup()
        self.assertEqual(latest["source"], "local_public_duty_pc")
        self.assertEqual(latest["case_count"], 1)
        self.assertEqual(latest["cases"][0]["case_id"], "case-1")
        completed = app_module.read_case_lookup_request()
        self.assertEqual(completed["status"], "case_lookup_completed")

    def test_app_page_does_not_query_cases(self):
        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)

    def test_case_display_extracts_address_from_description_and_hides_empty_return_time(self):
        case = {
            "category": "\u7dca\u6025\u6551\u8b77-\u5275\u50b7",
            "description": "119\u6848\u4ef6\n\u7dca\u6025\u6551\u8b77\n\u8fd4\u968a\u6642\u9593:\n\u5730\u9ede:\u6843\u5712\u5e02\u89c0\u97f3\u5340\u798f\u5c71\u8def\u4e8c\u6bb5790\u5df7100\u5f049\u865f",
            "case_date": "1150607",
            "case_time_hhmm": "1024",
            "return_time_hhmm": "",
        }

        self.assertEqual(
            app_module.display_case_title(case),
            "\u7dca\u6025\u6551\u8b77-\u5275\u50b7 - \u6843\u5712\u5e02\u89c0\u97f3\u5340\u798f\u5c71\u8def\u4e8c\u6bb5790\u5df7100\u5f049\u865f",
        )
        self.assertEqual(app_module.case_time_range(case), "06/07 1024")

    def test_event_detail_text_keeps_event_log_short(self):
        event = {"status": "vehicle_mileage_saved", "detail": "\u8eca\u8f1b\u91cc\u7a0b: \u5df2\u5efa\u7acb\u5f88\u9577\u7684\u8aaa\u660e"}

        self.assertEqual(app_module.event_detail_text(event), "\u5df2\u5b8c\u6210")

    def test_visible_events_keeps_latest_event_per_site(self):
        events = [
            {"status": "disinfection_failed", "detail": "\u7dca\u6025\u6551\u8b77\u6d88\u6bd2: old", "time": "1"},
            {"status": "desktop_fast_completed_with_errors", "detail": "overall", "time": "2"},
            {"status": "disinfection_saved", "detail": "\u7dca\u6025\u6551\u8b77\u6d88\u6bd2: new", "time": "3"},
        ]

        visible = app_module.visible_events(events)

        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["status"], "disinfection_saved")

    def test_effective_task_status_prefers_waiting_site(self):
        payload = {
            "overall_status": "duty_work_log_saved",
            "site_statuses": {
                "vehicle_mileage": {"status": "vehicle_mileage_saved"},
                "consumables": {"status": "manual_captcha_required"},
            },
        }

        self.assertEqual(app_module.effective_task_status(payload), "manual_captcha_required")

    def test_worker_case_lookup_request_and_cases_post(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        self.client.post("/cases/query", data={"lookup_range": "24h"}, follow_redirects=False)

        denied = self.client.get("/worker/case-lookup-request")
        self.assertEqual(denied.status_code, 403)

        request_response = self.client.get("/worker/case-lookup-request", headers={"X-Worker-Token": "test-token"})
        self.assertEqual(request_response.status_code, 200)
        request_payload = request_response.get_json()
        self.assertEqual(request_payload["request"]["lookup_range"], "24h")

        cases_response = self.client.post(
            "/worker/cases",
            headers={"X-Worker-Token": "test-token"},
            json={
                "status": "cases_loaded",
                "detail": "loaded",
                "lookup_range": "24h",
                "case_hash": "abc123",
                "cases": [{"case_id": "1", "address": "addr"}],
            },
        )
        self.assertEqual(cases_response.status_code, 200)
        latest = app_module.read_case_lookup()
        self.assertEqual(latest["case_hash"], "abc123")
        self.assertEqual(latest["cases"][0]["case_id"], "1")
        completed = app_module.read_case_lookup_request()
        self.assertEqual(completed["status"], "case_lookup_completed")

    def test_worker_tasks_api_requires_token_and_returns_recent_tasks(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        denied = self.client.get("/worker/tasks")
        self.assertEqual(denied.status_code, 403)

        list_response = self.client.get("/worker/tasks", headers={"X-Worker-Token": "test-token"})
        self.assertEqual(list_response.status_code, 200)
        list_payload = list_response.get_json()
        self.assertEqual(list_payload["tasks"][0]["task"]["task_id"], task_id)

        task_response = self.client.get(f"/worker/tasks/{task_id}", headers={"X-Worker-Token": "test-token"})
        self.assertEqual(task_response.status_code, 200)
        self.assertEqual(task_response.get_json()["task"]["driver"], "\u66fe\u5f65\u7db8")

    def test_import_case_redirects_to_app(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        (cases_dir / "latest.json").write_text(
            """
            {
              "status": "cases_loaded",
              "updated_at": "2026-06-03T08:00:00",
              "cases": [
                {
                  "case_id": "20260602090556012",
                  "address": "桃園市觀音區",
                  "case_time_hhmm": "0905",
                  "personnel": ["吳宗耕", "楊弘宇"]
                }
              ]
            }
            """,
            encoding="utf-8",
        )

        response = self.client.post("/cases/import", data={"case_id": "20260602090556012"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/app#task-form")
        imported_response = self.client.get("/app")
        imported_body = html.unescape(imported_response.data.decode("utf-8"))
        self.assertIn("0905", imported_body)
        self.assertIn(" checked", imported_body)
        self.assertIn('formaction="/cases/clear"', imported_body)
        self.assertEqual(app_module.read_selected_case(), {})

        refreshed_response = self.client.get("/app")
        refreshed_body = html.unescape(refreshed_response.data.decode("utf-8"))
        self.assertNotIn('value="0905"', refreshed_body)
        self.assertNotIn('value="桃園市觀音區"', refreshed_body)
        self.assertNotIn(" checked", refreshed_body)

        clear_response = self.client.post("/cases/clear", follow_redirects=False)
        self.assertEqual(clear_response.status_code, 302)
        self.assertEqual(clear_response.headers["Location"], "/app")
        self.assertEqual(app_module.read_selected_case(), {})
        cleared_response = self.client.get("/app")
        cleared_body = html.unescape(cleared_response.data.decode("utf-8"))
        self.assertNotIn('value="0905"', cleared_body)
        self.assertNotIn('value="桃園市觀音區"', cleared_body)
        self.assertNotIn(" checked", cleared_body)
        self.assertIn("const defaultConsumables = {};", cleared_body)

        self.client.post("/cases/import", data={"case_id": "20260602090556012"}, follow_redirects=False)
        self.client.post("/tasks", data=self.valid_task_data(), follow_redirects=False)
        self.assertEqual(app_module.read_selected_case(), {})

    def test_task_detail_run_and_manual_complete(self):
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                case_address="\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                case_reason="\u8eca\u798d",
                case_time="1420",
                return_time="1505",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        detail_response = self.client.get(f"/tasks/{task_id}")
        self.assertEqual(detail_response.status_code, 200)
        detail_body = html.unescape(detail_response.data.decode("utf-8"))
        self.assertEqual(detail_body.count("\u55ae\u7368\u767b\u6253"), 0)
        self.assertIn("四站登打啟動", detail_body)
        self.assertNotIn("送到公務電腦", detail_body)
        self.assertIn("main { max-width: 1080px;", detail_body)
        self.assertIn("repeating-linear-gradient", detail_body)

        run_response = self.client.post(f"/tasks/{task_id}/run", follow_redirects=False)
        self.assertEqual(run_response.status_code, 302)
        app_module.runner.wait_for_idle()

        complete_response = self.client.post(
            f"/tasks/{task_id}/sites/vehicle_mileage/complete",
            follow_redirects=False,
        )
        self.assertEqual(complete_response.status_code, 302)
        payload = self.store.get(task_id)
        self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "completed_by_user")

    def test_single_site_buttons_show_for_failed_and_blocked_unfinished_sites(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "disinfection",
                "\u7dca\u6025\u6551\u8b77\u6d88\u6bd2",
                "disinfection_failed",
                "login failed",
            ),
        )

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(body.count("\u55ae\u7368\u767b\u6253"), 2)
        self.assertIn(f"/tasks/{task_id}/sites/disinfection/run", body)
        self.assertIn(f"/tasks/{task_id}/sites/consumables/run", body)
        disinfection_card = body[body.index("<h3>\u6d88\u6bd2</h3>") : body.index("<h3>\u8017\u6750</h3>")]
        self.assertLess(disinfection_card.index("\u55ae\u7368\u767b\u6253"), disinfection_card.index("\u5931\u6557"))

    def test_task_detail_auto_refreshes_while_running(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.set_overall_status(task_id, "desktop_fast_running", "running")

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("window.location.reload()", body)
        self.assertIn("1500", body)

    def test_localhost_single_site_run_uses_desktop_fast_runner(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.post(
            f"/tasks/{task_id}/sites/disinfection/run",
            base_url="http://127.0.0.1:8080",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started_sites, [(task_id, "disinfection")])
        self.assertEqual(self.store.get(task_id)["overall_status"], "desktop_fast_running")

    def test_remote_single_site_run_does_not_call_desktop_runner(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.post(
            f"/tasks/{task_id}/sites/disinfection/run",
            base_url="http://100.114.126.58:8080",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started_sites, [])
        self.assertEqual(self.store.get(task_id)["overall_status"], "desktop_fast_unavailable")

    def test_task_detail_shows_chinese_statuses_without_raw_statuses(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "vehicle_mileage",
                "車輛里程",
                "local_pc_ready",
                "已建立本機電腦操作任務",
            ),
        )

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("待確認", body)
        self.assertNotIn("local_pc_ready", body)
        self.assertNotIn("https://ppe.tyfd.gov.tw", body)
        task_section = body.split('aria-label="任務內容"', 1)[1]
        task_section_head = task_section.split('<div class="task-submit">', 1)[0]
        self.assertIn("任務內容", task_section_head)
        self.assertNotIn("待確認", task_section_head)

    def test_task_detail_header_hides_meta_and_keeps_run_button_in_content(self):
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                vehicle="\u65b0\u576192",
                driver="\u5305\u83ef\u5148",
                mileage="200",
                case_time="1633",
                case_date="2026-06-06",
                return_time="1700",
                return_date="2026-06-06",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))
        header = body.split('aria-label="任務內容"', 1)[0]

        self.assertIn("返回首頁", header)
        self.assertNotIn("回到上一頁", header)
        self.assertNotIn("06/06 1633", header)
        self.assertNotIn("\u65b0\u576192 / \u5305\u83ef\u5148", header)
        self.assertNotIn("\u9001\u5230\u516c\u52d9\u96fb\u8166", header)
        self.assertLess(body.index("\u56db\u7ad9\u767b\u6253\u555f\u52d5"), body.index("\u8fd4\u56de\u7de8\u8f2f"))

    def test_task_edit_updates_existing_task_and_resets_sites(self):
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(mileage="100", consumables="\u53e3\u7f69=2"),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_saved", "done"),
        )

        edit_response = self.client.get(f"/tasks/{task_id}/edit")
        edit_body = html.unescape(edit_response.data.decode("utf-8"))
        self.assertEqual(edit_response.status_code, 200)
        self.assertIn("救護返隊小幫手-編輯狀態", edit_body)
        self.assertNotIn("勤務案件", edit_body)
        self.assertNotIn("救護車設定", edit_body)
        self.assertNotIn("公務後台", edit_body)
        self.assertIn("儲存修改", edit_body)
        self.assertIn('value="100"', edit_body)

        update_response = self.client.post(
            f"/tasks/{task_id}/edit",
            data={
                "vehicle": "\u65b0\u576192",
                "driver": "\u5305\u83ef\u5148",
                "mileage": "200",
                "case_time": "1024",
                "return_time": "1119",
                "case_reason": "\u8eca\u798d",
                "patient_summary": "\u5973\u4e00\u540d",
                "consumables": "\u624b\u5957=1",
            },
            follow_redirects=False,
        )
        payload = self.store.get(task_id)

        self.assertEqual(update_response.status_code, 302)
        self.assertEqual(update_response.headers["Location"], f"/tasks/{task_id}")
        self.assertEqual(payload["task"]["vehicle"], "\u65b0\u576192")
        self.assertEqual(payload["task"]["mileage"], "200")
        self.assertEqual(payload["task"]["consumables"], {"\u624b\u5957": 1})
        self.assertEqual(payload["overall_status"], "created")
        self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "not_started")

    def test_task_detail_card_order_is_work_mileage_disinfection_consumables(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertLess(body.index("<h3>\u5de5\u4f5c</h3>"), body.index("<h3>\u91cc\u7a0b</h3>"))
        self.assertLess(body.index("<h3>\u91cc\u7a0b</h3>"), body.index("<h3>\u6d88\u6bd2</h3>"))
        self.assertLess(body.index("<h3>\u6d88\u6bd2</h3>"), body.index("<h3>\u8017\u6750</h3>"))
        work = body[body.index("<h3>\u5de5\u4f5c</h3>") : body.index("<h3>\u91cc\u7a0b</h3>")]
        self.assertLess(work.index("\u5730\u5740"), work.index("\u4e8b\u7531"))
        self.assertLess(work.index("\u4e8b\u7531"), work.index("\u8eca\u8f1b"))
        self.assertLess(work.index("\u8eca\u8f1b"), work.index("\u53f8\u6a5f"))
        self.assertLess(work.index("\u53f8\u6a5f"), work.index("\u50b7\u75c5\u60a3"))
        mileage = body[body.index("<h3>\u91cc\u7a0b</h3>") : body.index("<h3>\u6d88\u6bd2</h3>")]
        self.assertLess(mileage.index(">\u8eca\u8f1b</span>"), mileage.index(">\u51fa\u52d5</span>"))
        self.assertLess(mileage.index(">\u51fa\u52d5</span>"), mileage.index(">\u8fd4\u968a</span>"))
        self.assertLess(mileage.index(">\u8fd4\u968a</span>"), mileage.index(">\u91cc\u7a0b</span>"))
        self.assertLess(mileage.index(">\u91cc\u7a0b</span>"), mileage.index(">\u53f8\u6a5f</span>"))

    def test_task_detail_lists_four_site_stage_checks(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("四站階段檢查", body)
        self.assertIn("登入勤務系統", body)
        self.assertIn("登入 PPE", body)
        self.assertIn("登入消毒系統", body)
        self.assertIn("登入一站通", body)
        self.assertIn("未執行", body)
        self.assertNotIn("未開始", body)
        self.assertNotIn("工作：未執行", body)
        self.assertNotIn("里程：未執行", body)
        self.assertNotIn("消毒：未執行", body)
        self.assertNotIn("耗材：未執行", body)

    def test_run_queues_task_for_worker_and_worker_updates_status(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        run_response = self.client.post(f"/tasks/{task_id}/run", follow_redirects=False)
        self.assertEqual(run_response.status_code, 302)
        queued = self.store.get(task_id)
        self.assertEqual(queued["overall_status"], "queued_for_worker")

        next_response = self.client.get("/worker/next-task?worker_id=test-worker")
        self.assertEqual(next_response.status_code, 200)
        next_payload = next_response.get_json()
        self.assertEqual(next_payload["task"]["task_id"], task_id)

        status_response = self.client.post(
            f"/worker/tasks/{task_id}/status",
            json={
                "status": "duty_work_log_saved",
                "detail": "saved",
                "site_key": "duty_work_log",
                "site_name": "\u6d88\u9632\u52e4\u52d9\u5de5\u4f5c\u7d00\u9304",
            },
        )
        self.assertEqual(status_response.status_code, 200)
        updated = self.store.get(task_id)
        self.assertEqual(updated["overall_status"], "claimed_by_worker")
        self.assertEqual(updated["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")

    def test_worker_site_status_can_update_overall_when_explicitly_requested(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.client.post(f"/tasks/{task_id}/run", follow_redirects=False)
        self.client.get("/worker/next-task?worker_id=test-worker")

        status_response = self.client.post(
            f"/worker/tasks/{task_id}/status",
            json={
                "status": "duty_work_log_saved",
                "detail": "saved",
                "site_key": "duty_work_log",
                "site_name": "\u6d88\u9632\u52e4\u52d9\u5de5\u4f5c\u7d00\u9304",
                "overall_status": "desktop_fast_completed",
                "overall_detail": "四站登打完成。",
            },
        )
        self.assertEqual(status_response.status_code, 200)
        updated = self.store.get(task_id)
        self.assertEqual(updated["overall_status"], "desktop_fast_completed")
        self.assertEqual(updated["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")

    def test_localhost_run_uses_desktop_fast_mode_when_auto(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.post(f"/tasks/{task_id}/run", base_url="http://127.0.0.1:8080", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started, [task_id])
        self.assertEqual(self.store.get(task_id)["overall_status"], "desktop_fast_running")

    def test_remote_host_run_queues_for_worker_when_auto(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.post(f"/tasks/{task_id}/run", base_url="http://100.114.126.58:8080", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started, [])
        self.assertEqual(self.store.get(task_id)["overall_status"], "queued_for_worker")

    def test_remote_create_queues_for_worker_and_hides_entry_controls(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(),
            base_url="http://100.114.126.58:8080",
            follow_redirects=False,
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started, [])
        self.assertEqual(self.store.get(task_id)["overall_status"], "queued_for_worker")

        detail_response = self.client.get(f"/tasks/{task_id}", base_url="http://100.114.126.58:8080")
        body = html.unescape(detail_response.data.decode("utf-8"))
        self.assertNotIn("四站登打啟動", body)
        self.assertNotIn("單獨登打", body)
        self.assertIn("返回編輯", body)

    def test_desktop_fast_mode_environment_overrides_host(self):
        os.environ["DESKTOP_FAST_MODE"] = "1"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        fast_task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        self.client.post(f"/tasks/{fast_task_id}/run", base_url="http://100.114.126.58:8080", follow_redirects=False)

        os.environ["DESKTOP_FAST_MODE"] = "0"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        queued_task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.client.post(f"/tasks/{queued_task_id}/run", base_url="http://127.0.0.1:8080", follow_redirects=False)

        self.assertEqual(app_module.desktop_runner.started, [fast_task_id])
        self.assertEqual(self.store.get(fast_task_id)["overall_status"], "desktop_fast_running")
        self.assertEqual(self.store.get(queued_task_id)["overall_status"], "queued_for_worker")


if __name__ == "__main__":
    unittest.main()
