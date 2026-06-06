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
        self.assertIn("\u6551\u8b77\u56de\u7a0b\u767b\u6253", body)
        self.assertIn("\u65b0\u576191", body)
        self.assertIn(">\u5433\u5b97\u8015</option>", body)
        self.assertNotIn("6 : \u5433\u5b97\u8015", body)
        self.assertIn('value="\u7121"', body)
        self.assertIn('placeholder="1420"', body)
        self.assertIn("\u67e5\u8a62\u6700\u8fd1 6 \u5c0f\u6642", body)
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
        self.assertEqual(request_payload["lookup_range"], "6h")

    def test_query_cases_accepts_today_range(self):
        response = self.client.post("/cases/query", data={"lookup_range": "today"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        request_payload = app_module.read_case_lookup_request()
        self.assertEqual(request_payload["lookup_range"], "today")

    def test_app_page_does_not_query_cases(self):
        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)

    def test_worker_case_lookup_request_and_cases_post(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        self.client.post("/cases/query", data={"lookup_range": "today"}, follow_redirects=False)

        denied = self.client.get("/worker/case-lookup-request")
        self.assertEqual(denied.status_code, 403)

        request_response = self.client.get("/worker/case-lookup-request", headers={"X-Worker-Token": "test-token"})
        self.assertEqual(request_response.status_code, 200)
        request_payload = request_response.get_json()
        self.assertEqual(request_payload["request"]["lookup_range"], "today")

        cases_response = self.client.post(
            "/worker/cases",
            headers={"X-Worker-Token": "test-token"},
            json={
                "status": "cases_loaded",
                "detail": "loaded",
                "lookup_range": "today",
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
        selected = app_module.read_selected_case()
        self.assertEqual(selected["case_id"], "20260602090556012")
        self.assertEqual(selected["person_options"], [("吳宗耕", "吳宗耕"), ("楊弘宇", "楊弘宇")])

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
                "status": "duty_work_log_prefilled",
                "detail": "prefilled",
                "site_key": "duty_work_log",
                "site_name": "\u6d88\u9632\u52e4\u52d9\u5de5\u4f5c\u7d00\u9304",
            },
        )
        self.assertEqual(status_response.status_code, 200)
        updated = self.store.get(task_id)
        self.assertEqual(updated["overall_status"], "duty_work_log_prefilled")
        self.assertEqual(updated["site_statuses"]["duty_work_log"]["status"], "duty_work_log_prefilled")


if __name__ == "__main__":
    unittest.main()
