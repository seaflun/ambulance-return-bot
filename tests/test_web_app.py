import html
import os
import tempfile
import unittest
from pathlib import Path

import app as app_module
from ambulance_bot.task_runner import TaskRunner
from ambulance_bot.task_store import JsonTaskStore


class WebAppTests(unittest.TestCase):
    def setUp(self):
        os.environ["OPEN_LOCAL_BROWSER_ON_RUN"] = "false"
        os.environ["USE_LOCAL_SELENIUM"] = "false"
        self.tmp = tempfile.TemporaryDirectory()
        self.original_worker_token = os.environ.get("WORKER_TOKEN")
        os.environ["WORKER_TOKEN"] = ""
        self.original_artifacts_dir = app_module.artifacts_dir
        app_module.artifacts_dir = Path(self.tmp.name)
        self.store = JsonTaskStore(Path(self.tmp.name) / "tasks")
        app_module.store = self.store
        app_module.runner = TaskRunner(Path(self.tmp.name), store=self.store)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.runner.wait_for_idle()
        app_module.artifacts_dir = self.original_artifacts_dir
        if self.original_worker_token is None:
            os.environ.pop("WORKER_TOKEN", None)
        else:
            os.environ["WORKER_TOKEN"] = self.original_worker_token
        self.tmp.cleanup()

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

    def test_create_task_writes_json_and_redirects(self):
        response = self.client.post(
            "/tasks",
            data={
                "vehicle": "\u65b0\u576191",
                "driver": "\u66fe\u5f65\u7db8",
                "mileage": "12345",
                "case_time": "1420",
                "return_time": "1505",
                "case_address": "\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                "case_reason": "\u6025\u75c5",
                "patient_summary": "\u7537\u4e00\u540d",
                "consumables": "\u53e3\u7f69=2,\u624b\u5957=2",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        tasks = self.store.list_recent()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task"]["vehicle"], "\u65b0\u576191")
        self.assertEqual(tasks[0]["task"]["case_time"], "1420")
        self.assertEqual(tasks[0]["task"]["case_address"], "\u6843\u5712\u5e02\u89c0\u97f3\u5340")
        self.assertEqual(tasks[0]["task"]["case_reason"], "\u6025\u75c5")

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
        create_response = self.client.post("/tasks", data={"vehicle": "\u65b0\u576191", "driver": "\u66fe\u5f65\u7db8"})
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
        self.assertEqual(app_module.read_selected_case(), {})

        refreshed_response = self.client.get("/app")
        refreshed_body = html.unescape(refreshed_response.data.decode("utf-8"))
        self.assertNotIn('value="0905"', refreshed_body)

    def test_task_detail_run_and_manual_complete(self):
        create_response = self.client.post("/tasks", data={"vehicle": "\u65b0\u576191"})
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        detail_response = self.client.get(f"/tasks/{task_id}")
        self.assertEqual(detail_response.status_code, 200)

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

    def test_task_detail_shows_chinese_statuses_without_raw_statuses(self):
        create_response = self.client.post("/tasks", data={"vehicle": "\u65b0\u576191"})
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

    def test_task_detail_header_hides_meta_and_keeps_run_button_in_content(self):
        create_response = self.client.post(
            "/tasks",
            data={
                "vehicle": "\u65b0\u576192",
                "driver": "\u5305\u83ef\u5148",
                "case_time": "1633",
                "case_date": "2026-06-06",
            },
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))
        header = body.split('<section aria-label="', 1)[0]

        self.assertNotIn("06/06 1633", header)
        self.assertNotIn("\u65b0\u576192 / \u5305\u83ef\u5148", header)
        self.assertNotIn("\u9001\u5230\u516c\u52d9\u96fb\u8166", header)

    def test_task_detail_card_order_is_work_mileage_disinfection_consumables(self):
        create_response = self.client.post("/tasks", data={"vehicle": "\u65b0\u576191"})
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertLess(body.index("<h3>\u5de5\u4f5c</h3>"), body.index("<h3>\u91cc\u7a0b</h3>"))
        self.assertLess(body.index("<h3>\u91cc\u7a0b</h3>"), body.index("<h3>\u6d88\u6bd2</h3>"))
        self.assertLess(body.index("<h3>\u6d88\u6bd2</h3>"), body.index("<h3>\u8017\u6750</h3>"))

    def test_run_queues_task_for_worker_and_worker_updates_status(self):
        create_response = self.client.post("/tasks", data={"vehicle": "\u65b0\u576191"})
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


if __name__ == "__main__":
    unittest.main()
