import html
import os
import tempfile
import unittest
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
        self.original_start_local_case_lookup = app_module.start_local_case_lookup
        self.original_local_host_candidates = app_module.local_host_candidates
        self.original_query_duty_emergency_cases = selenium_local_module.query_duty_emergency_cases
        os.environ["WORKER_TOKEN"] = ""
        os.environ["DESKTOP_FAST_MODE"] = "0"
        os.environ["TASK_EXECUTION_MODE"] = "worker_queue"
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
        self.assertIn("救護返隊登打事項", body)
        self.assertIn("\u65b0\u576191", body)
        self.assertIn(">\u5433\u5b97\u8015</option>", body)
        self.assertNotIn("6 : \u5433\u5b97\u8015", body)
        self.assertIn('value="\u7121"', body)
        self.assertNotIn('placeholder="1420"', body)
        self.assertNotIn('placeholder="1505"', body)
        self.assertNotIn('placeholder="12345"', body)
        self.assertIn(">\u8acb\u9078\u64c7</option>", body)
        self.assertIn("查詢前 24 小時", body)
        self.assertIn('name="case_address"', body)
        self.assertNotIn('name="work_note"', body)
        self.assertIn("const defaultConsumables = {};", body)
        self.assertNotIn(" checked", body)

    def test_app_page_recent_task_can_be_deleted(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data(), follow_redirects=False)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        app_response = self.client.get("/app")
        body = html.unescape(app_response.data.decode("utf-8"))
        self.assertIn(f'action="/tasks/{task_id}/delete"', body)
        self.assertIn('aria-label="刪除案件"', body)

        delete_response = self.client.post(f"/tasks/{task_id}/delete", follow_redirects=False)

        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(delete_response.headers["Location"], "/app")
        self.assertEqual(self.store.list_recent(), [])

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
        self.assertIn("請選擇出動車輛", body)
        self.assertIn("請選擇司機", body)
        self.assertIn("請填寫里程", body)
        self.assertIn("請填寫返隊時間", body)
        self.assertIn("請選擇傷病患", body)

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

    def test_query_cases_accepts_24h_range(self):
        response = self.client.post("/cases/query", data={"lookup_range": "24h"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        request_payload = app_module.read_case_lookup_request()
        self.assertEqual(request_payload["lookup_range"], "24h")

    def test_localhost_query_cases_starts_local_lookup_when_fast_mode_auto(self):
        calls = []
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        app_module.start_local_case_lookup = lambda lookup_range: calls.append(lookup_range)

        response = self.client.post("/cases/query", data={"lookup_range": "6h"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(calls, ["6h"])

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

        response = self.client.post("/cases/query", data={"lookup_range": "6h"}, follow_redirects=False)

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
        self.assertEqual(response.headers["Location"], "/app")
        imported_response = self.client.get("/app")
        imported_body = html.unescape(imported_response.data.decode("utf-8"))
        self.assertIn("0905", imported_body)
        self.assertIn(" checked", imported_body)
        self.assertEqual(app_module.read_selected_case().get("case_id"), "20260602090556012")

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

    def test_single_site_button_only_shows_after_site_failure(self):
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

        self.assertEqual(body.count("\u55ae\u7368\u767b\u6253"), 1)
        self.assertIn(f"/tasks/{task_id}/sites/disinfection/run", body)

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
        task_section = body.split('<section aria-label="任務內容">', 1)[1]
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
        header = body.split('<section aria-label="', 1)[0]

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
        self.assertEqual(updated["overall_status"], "duty_work_log_saved")
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
