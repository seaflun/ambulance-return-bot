import contextlib
import base64
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
import worker as worker_module
from ambulance_bot.models import AmbulanceReturnRequest
from tests import test_web_app as web_fixtures


class LanWorkerDispatchTests(unittest.TestCase):
    def assert_dispatched_sites(self, service, *, fuel=False, volunteer=False):
        request = AmbulanceReturnRequest.from_dict({
            "task_id": f"lan-dispatch-{service}-{fuel}-{volunteer}", "created_at": datetime.now().isoformat(),
            "service_type": service, "vehicle": "新坡11" if service == "disaster" else "新坡92",
            "fuel_record": {"enabled": fuel, "date": "20261020", "time": "1010", "quantity": "10", "unit_price": "30"},
            "volunteer_assist": volunteer,
        })
        expected = request.active_site_keys()
        payload = {"task": request.to_dict(), "site_statuses": {
            key: {"status": "not_started" if key in expected else "not_applicable"}
            for key in app_module.VALID_SITE_KEYS}}
        calls = []

        def run_site(key):
            def run(*_args, **_kwargs):
                calls.append(key)
                payload["site_statuses"][key]["status"] = key + "_saved"
                return SimpleNamespace(ok=True, status=key + "_saved", detail="isolated")
            return run

        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch.object(worker_module, "fetch_task_payload", return_value=payload))
            patches.enter_context(mock.patch.object(worker_module, "post_status"))
            patches.enter_context(mock.patch.object(worker_module, "ensure_ems_record_folders", return_value=[]))
            patches.enter_context(mock.patch.object(worker_module, "maximize_worker_site_windows"))
            patches.enter_context(mock.patch.object(worker_module, "_start_worker_claim_heartbeat", return_value=lambda: None))
            for key, name in (("duty_work_log", "run_task"), ("vehicle_mileage", "run_vehicle_task"),
                              ("fuel_record", "run_fuel_worker_task"), ("consumables", "run_consumables_worker_task"),
                              ("disinfection", "run_disinfection_worker_task"), ("volunteer_assist", "run_volunteer_assist_worker_task")):
                patches.enter_context(mock.patch.object(worker_module, name, side_effect=run_site(key)))
            worker_module.run_queued_task("http://isolated.invalid", "test-worker", request.to_dict(), Path(tmp))
        self.assertCountEqual(expected, calls)
        if fuel:
            self.assertLess(calls.index("vehicle_mileage"), calls.index("fuel_record"))
        if volunteer:
            self.assertLess(calls.index("duty_work_log"), calls.index("volunteer_assist"))

    def test_disaster_queues_only_work_and_mileage(self):
        self.assert_dispatched_sites("disaster")

    def test_disaster_preserves_optional_fuel_and_its_order(self):
        self.assert_dispatched_sites("disaster", fuel=True)

    def test_ems_preserves_four_sites(self):
        self.assert_dispatched_sites("ems")

    def test_ems_preserves_six_sites_and_dependencies(self):
        self.assert_dispatched_sites("ems", fuel=True, volunteer=True)


class LanAdminHistoryTests(unittest.TestCase):
    original = {"base_url": "http://10.30.65.30:8080"}
    entry = {"base_url": "http://10.30.65.30:8091", "environ_overrides": {"SERVER_PORT": "8091"}}

    def setUp(self):
        environment = mock.patch.dict(os.environ, {"TASK_ENTRY_HOSTS": "", "TASK_ENTRY_PORT": "8091"})
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = web_fixtures.WebAppTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        self.store = self.fixture.store

    def create_historical(self, service):
        task = app_module.request_from_form(self.fixture.valid_task_data()).to_dict()
        task.update(task_id="historical-lan-" + service, service_type=service,
                    created_at=(datetime.now() - timedelta(days=3)).isoformat())
        request = AmbulanceReturnRequest.from_dict(task)
        payload = self.store.create(request)
        for key in request.active_site_keys():
            payload["site_statuses"][key]["status"] = key + "_saved"
        payload.update(overall_status="desktop_fast_completed", updated_at=task["created_at"], created_at=task["created_at"])
        app_module.write_json_atomic(self.store.tasks_dir / f"{request.task_id}.json", payload)
        return request.task_id

    def rendered_reports(self, path):
        with mock.patch.object(app_module, "render_template", wraps=app_module.render_template) as render:
            response = self.client.get(path, **self.original)
        self.assertEqual(200, response.status_code)
        return render.call_args.kwargs, response.get_data(as_text=True)

    def test_existing_lan_tasks_appear_by_service_beyond_recent_form_window(self):
        ems_id = self.create_historical("ems")
        disaster_id = self.create_historical("disaster")
        before = {path: path.read_bytes() for path in self.store.tasks_dir.glob("*.json")}
        self.assertLess(datetime.fromisoformat(self.store.get(ems_id)["updated_at"]), datetime.now() - timedelta(hours=48))
        for path, expected_id in (("/admin/ems", ems_id), ("/admin/disaster", disaster_id)):
            context, body = self.rendered_reports(path)
            self.assertEqual([expected_id], [item["task_id"] for item in context["reports"]])
            self.assertEqual({"all": 1, "success": 1, "failed": 0}, context["report_counts"])
            self.assertNotIn("同步帳號：", body)
            self.assertIn("各站登打狀態", body)
        context, _body = self.rendered_reports("/admin/public-pc")
        self.assertCountEqual([ems_id, disaster_id], [item["task_id"] for item in context["reports"]])
        self.assertEqual(before, {path: path.read_bytes() for path in self.store.tasks_dir.glob("*.json")})
        self.assertFalse(app_module.public_pc_report_file().exists())

    def test_local_report_duplicate_is_counted_once_and_keeps_original_metadata(self):
        task_id = self.create_historical("ems")
        payload = self.store.get(task_id)
        app_module.upsert_public_pc_report({"task_id": task_id, "task": payload["task"],
            "site_statuses": payload["site_statuses"], "status": "desktop_fast_completed",
            "title": "原本機標題", "operator": "原操作者", "package_version": "2026.10.19.2340"})
        context, _body = self.rendered_reports("/admin/ems")
        self.assertEqual(1, context["report_counts"]["all"])
        self.assertEqual("原操作者", context["reports"][0]["operator"])
        self.assertEqual("原本機標題", context["reports"][0]["title"])

    def test_worker_reply_and_single_site_retry_are_visible_in_same_lan_history(self):
        os.environ["WORKER_TOKEN"] = "0123456789abcdef0123456789abcdef"
        headers = {"X-Worker-Token": os.environ["WORKER_TOKEN"]}
        created = self.client.post("/tasks", data=self.fixture.valid_task_data(), **self.entry)
        task_id = created.headers["Location"].rsplit("/", 1)[-1]
        self.client.post(created.headers["Location"] + "/run", **self.entry)
        claim = self.client.get("/worker/next-task?worker_id=audit", headers=headers, **self.original).json["payload"]
        posted = self.client.post(f"/worker/tasks/{task_id}/status", headers=headers, json={
            "site_key": "disinfection", "status": "disinfection_failed", "detail": "隔離登入失敗",
            "claim_id": claim["worker_queue"]["claim_id"], "worker_id": "audit",
            "overall_status": "desktop_fast_completed_with_errors", "failure_stage": "登入消毒系統"}, **self.original)
        self.assertEqual(200, posted.status_code)
        context, body = self.rendered_reports("/admin/ems?result=failed")
        self.assertEqual([task_id], [item["task_id"] for item in context["reports"]])
        self.assertEqual("隔離登入失敗", context["reports"][0]["site_statuses"]["disinfection"]["detail"])
        self.assertIn("登入消毒系統", body)
        retried = self.client.post(f"/tasks/{task_id}/sites/disinfection/run", data={"return_service": "ems"}, **self.original)
        self.assertEqual(302, retried.status_code)
        self.assertEqual("/admin/ems", retried.headers["Location"])
        self.assertEqual("disinfection", self.store.get(task_id)["worker_queue"]["run_site_key"])
        context, _body = self.rendered_reports("/admin/ems")
        self.assertEqual([task_id], [item["task_id"] for item in context["reports"]])

    def test_entry_port_still_blocks_admin_history(self):
        self.create_historical("ems")
        self.assertEqual(403, self.client.get("/admin/ems", **self.entry).status_code)

    def test_all_historical_lan_tasks_are_listed_without_a_ten_task_cap(self):
        task_id = self.create_historical("ems")
        payload = self.store.get(task_id)
        for index in range(12):
            other = copy.deepcopy(payload)
            other["task"]["task_id"] = f"older-lan-{index}"
            self.store.save_payload(other["task"]["task_id"], other)
        context, _body = self.rendered_reports("/admin/ems")
        self.assertEqual(13, context["report_counts"]["all"])

    def test_local_reporting_mode_keeps_existing_admin_source(self):
        self.create_historical("ems")
        with mock.patch.dict(os.environ, {"PUBLIC_PC_REPORT_ENABLED": "true"}):
            context, _body = self.rendered_reports("/admin/ems")
        self.assertEqual([], context["reports"])

    def test_expired_task_is_not_resurrected_or_deleted_by_history_view(self):
        task_id = self.create_historical("ems")
        path = self.store.tasks_dir / f"{task_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["updated_at"] = (datetime.now() - timedelta(days=15)).isoformat()
        app_module.write_json_atomic(path, payload)
        before = path.read_bytes()
        context, _body = self.rendered_reports("/admin/ems")
        self.assertEqual([], context["reports"])
        self.assertEqual(before, path.read_bytes())


class LanFailureEvidenceTests(unittest.TestCase):
    setUp = LanAdminHistoryTests.setUp
    rendered_reports = LanAdminHistoryTests.rendered_reports
    original = LanAdminHistoryTests.original
    entry = LanAdminHistoryTests.entry

    def claim_task(self):
        os.environ["WORKER_TOKEN"] = "0123456789abcdef0123456789abcdef"
        self.headers = {"X-Worker-Token": os.environ["WORKER_TOKEN"]}
        created = self.client.post("/tasks", data=self.fixture.valid_task_data(), **self.entry)
        self.task_id = created.headers["Location"].rsplit("/", 1)[-1]
        self.client.post(created.headers["Location"] + "/run", **self.entry)
        claimed = self.client.get("/worker/next-task?worker_id=evidence-worker", headers=self.headers, **self.original)
        self.claim_id = claimed.json["payload"]["worker_queue"]["claim_id"]

    def test_worker_failure_png_reaches_nas_history_and_survives_success_and_replay(self):
        self.claim_task()
        png = b"\x89PNG\r\n\x1a\nsynthetic-worker-failure"
        sent = []

        def transport(_server, task_id, body):
            sent.append(copy.deepcopy(body))
            response = self.client.post(f"/worker/tasks/{task_id}/status", headers=self.headers, json=body, **self.original)
            self.assertEqual(200, response.status_code)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARTIFACTS_DIR": tmp}), \
                mock.patch.object(worker_module, "_send_worker_status", side_effect=transport):
            root = Path(tmp) / "selenium"
            root.mkdir()
            screenshot = root / "failure.png"
            screenshot.write_bytes(png)
            (root / "failure.json").write_text(json.dumps({"task_id": self.task_id, "site_key": "disinfection",
                "vehicle": "新坡91", "captured_at": datetime.now().isoformat(), "screenshot_path": str(screenshot)}), encoding="utf-8")
            worker_module.post_status("http://isolated.invalid", self.task_id, "disinfection_failed", "隔離錯誤",
                site_key="disinfection", failure_stage="登入消毒系統", claim_id=self.claim_id, worker_id="evidence-worker")
            failure = sent[-1]
            self.assertEqual(png, base64.b64decode(failure["failure_evidence"]["disinfection"]["screenshots"][0]["content_base64"]))
            payload = self.store.get(self.task_id)
            url = payload["site_statuses"]["disinfection"]["failure_screenshots"][0]["url"]
            with self.client.get(url, **self.original) as response:
                self.assertEqual(png, response.data)
            self.assertEqual(403, self.client.get(url, **self.entry).status_code)
            replay = self.client.post(f"/worker/tasks/{self.task_id}/status", headers=self.headers, json=failure, **self.original)
            self.assertTrue(replay.json["duplicate"])
            worker_module.post_status("http://isolated.invalid", self.task_id, "disinfection_saved", "隔離成功",
                site_key="disinfection", claim_id=self.claim_id, worker_id="evidence-worker")
        context, body = self.rendered_reports("/admin/ems")
        history = app_module.public_pc_site_failure_history(context["reports"][0], "disinfection")
        self.assertEqual(1, len(history))
        self.assertEqual(url, history[0]["failure_screenshots"][0]["url"])
        self.assertFalse(history[0]["current"])
        self.assertIn(url, body)

    def test_stale_claim_cannot_store_failure_screenshot(self):
        self.claim_task()
        response = self.client.post(f"/worker/tasks/{self.task_id}/status", headers=self.headers, json={
            "site_key": "disinfection", "status": "disinfection_failed", "claim_id": "stale-claim", "worker_id": "evidence-worker",
            "failure_evidence": {"disinfection": {"screenshots": [{"content_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nstale").decode()}]}}}, **self.original)
        self.assertEqual(409, response.status_code)
        self.assertFalse(app_module.public_pc_failure_screenshot_dir().exists())

    def test_invalid_png_is_reported_without_losing_failure_status(self):
        self.claim_task()
        response = self.client.post(f"/worker/tasks/{self.task_id}/status", headers=self.headers, json={
            "site_key": "disinfection", "status": "disinfection_failed", "claim_id": self.claim_id, "worker_id": "evidence-worker",
            "failure_evidence": {"disinfection": {"screenshots": [{"content_base64": base64.b64encode(b"not PNG").decode()}]}}}, **self.original)
        self.assertEqual(200, response.status_code)
        site = self.store.get(self.task_id)["site_statuses"]["disinfection"]
        self.assertEqual("disinfection_failed", site["status"])
        self.assertIn("不是有效 PNG", site["failure_screenshot_error"])
        self.assertEqual([], site["failure_screenshots"])

    def test_screenshot_disk_error_keeps_worker_status_and_diagnostics(self):
        self.claim_task()
        with mock.patch("ambulance_bot.task_store.store_task_failure_evidence", side_effect=OSError("disk full")):
            response = self.client.post(f"/worker/tasks/{self.task_id}/status", headers=self.headers, json={
                "site_key": "disinfection", "status": "disinfection_failed", "claim_id": self.claim_id, "worker_id": "evidence-worker",
                "failure_evidence": {"disinfection": {"screenshot_error": "no image"}}, "failure_stage": "登入消毒系統"}, **self.original)
        self.assertEqual(200, response.status_code)
        site = self.store.get(self.task_id)["site_statuses"]["disinfection"]
        self.assertEqual("disinfection_failed", site["status"])
        self.assertIn("NAS 無法保存", site["failure_screenshot_error"])
        self.assertEqual("登入消毒系統", site["failure_stage"])
