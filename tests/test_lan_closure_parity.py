import copy
from datetime import datetime, timedelta
import os
import unittest
from unittest import mock

import app as app_module
from ambulance_bot.adapters import default_adapters
from tests import test_web_app as web_fixtures


class LanClosureParityTests(unittest.TestCase):
    entry = {"base_url": "http://10.30.65.30:8091", "environ_overrides": {"SERVER_PORT": "8091"}}
    original = {"base_url": "http://10.30.65.30:8080"}

    def setUp(self):
        environment = mock.patch.dict(os.environ, {"TASK_ENTRY_PORT": "8091", "TASK_ENTRY_HOSTS": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = web_fixtures.WebAppTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        self.store = self.fixture.store

    def create_ems(self, **changes):
        response = self.client.post("/tasks", data=self.fixture.valid_task_data(**changes), **self.entry)
        self.assertEqual(302, response.status_code)
        return response.headers["Location"]

    def test_entry_ems_preserves_confirmation_edit_and_explicit_queue_sequence(self):
        location = self.create_ems()
        task_id = location.rsplit("/", 1)[-1]
        payload = self.store.get(task_id)
        self.assertEqual("idle", payload["worker_queue"]["status"])
        self.assertEqual("worker_queue", payload["auto_start"]["mode"])
        self.assertEqual(timedelta(minutes=10), datetime.fromisoformat(payload["auto_start"]["due_at"])
                         - datetime.fromisoformat(payload["created_at"]))
        before = self.client.get(location, **self.entry).get_data(as_text=True)
        self.assertIn("是否已結案?", before)
        self.assertIn("返回編輯", before)
        self.assertNotIn("四站登打啟動", before)

        confirmed = self.client.post(location + "/confirm-case-closed", **self.entry)
        self.assertEqual(302, confirmed.status_code)
        self.assertEqual("idle", self.store.get(task_id)["worker_queue"]["status"])
        page = self.client.get(location, **self.entry).get_data(as_text=True)
        self.assertNotIn("是否已結案?", page)
        self.assertIn("四站登打啟動", page)
        self.assertEqual(200, self.client.get(location + "/edit", **self.entry).status_code)
        self.assertEqual(302, self.client.post(location + "/run", **self.entry).status_code)
        queued = self.store.get(task_id)
        self.assertEqual("queued", queued["worker_queue"]["status"])
        self.assertEqual("cancelled", queued["auto_start"]["status"])
        active = self.client.get(location, **self.entry).get_data(as_text=True)
        self.assertIn("中止登打", active)
        self.assertNotIn("是否已結案?", active)
        self.assertEqual([], app_module.desktop_runner.started)

    def test_entry_disaster_preserves_explicit_start_without_ems_confirmation(self):
        data = {
            "case_id": "ISOLATED-FIRE", "case_date": "2026/10/20", "case_time": "0900",
            "return_time": "1000", "case_address": "隔離測試地址", "summary_type": "火災",
            "case_reason": "一般(集合)住宅", "personnel": "甲,乙", "commander": "乙",
            "action_note": "現場待命", "recorder_category": "轄內A3", "vehicle": "新坡11",
            "driver": "甲", "vehicle_return_time": "1000", "mileage": "100",
        }
        with mock.patch.object(app_module, "ensure_disaster_media_folders", return_value=[]):
            response = self.client.post("/tasks/disaster", data=data, **self.entry)
        self.assertEqual(302, response.status_code)
        location = response.headers["Location"]
        task_id = location.rsplit("/", 1)[-1]
        self.assertEqual("idle", self.store.get(task_id)["worker_queue"]["status"])
        page = self.client.get(location, **self.entry).get_data(as_text=True)
        self.assertIn("二站登打啟動", page)
        self.assertNotIn("是否已結案?", page)
        self.assertEqual(302, self.client.post(location + "/run", **self.entry).status_code)
        payload = self.store.get(task_id)
        self.assertEqual("queued", payload["worker_queue"]["status"])
        self.assertEqual("not_applicable", payload["site_statuses"]["consumables"]["status"])

    def test_pending_entry_waits_for_start_and_ten_minute_fallback_queues_only_once(self):
        location = self.create_ems()
        task_id = location.rsplit("/", 1)[-1]
        self.assertIsNone(self.store.claim_next_for_worker("isolated"))
        app_module.run_due_task_auto_starts()
        self.assertEqual("idle", self.store.get(task_id)["worker_queue"]["status"])
        payload = self.store.get(task_id)
        payload["auto_start"]["due_at"] = (datetime.now() - timedelta(seconds=1)).isoformat()
        self.store.save_payload(task_id, payload)
        app_module.run_due_task_auto_starts()
        queued = self.store.get(task_id)
        app_module.run_due_task_auto_starts()
        self.assertEqual(queued, self.store.get(task_id))
        self.assertEqual("started", queued["auto_start"]["status"])
        self.assertFalse(queued.get("ems_case_closed_confirmed"))
        self.assertEqual(task_id, self.store.claim_next_for_worker("isolated")["task"]["task_id"])
        self.assertEqual([], app_module.desktop_runner.started)

    def test_queue_preparation_never_claims_captcha_or_manual_entry_is_required(self):
        request = app_module.request_from_form(self.fixture.valid_task_data())
        self.store.create(request)
        app_module.queue_task_for_worker(request.task_id)
        payload = self.store.get(request.task_id)
        for key in request.active_site_keys():
            site = payload["site_statuses"][key]
            self.assertEqual("not_started", site["status"])
            self.assertNotIn("人工登入", site["detail"])
            self.assertNotIn("第一版", site["detail"])
        self.assertEqual(["created", "queued_for_worker"], [row["status"] for row in payload["events"]])

    def test_legacy_preparation_is_explained_without_rewriting_history_or_hiding_real_captcha(self):
        request = app_module.request_from_form(self.fixture.valid_task_data())
        payload = self.store.create(request)
        for adapter in default_adapters():
            if adapter.key in request.active_site_keys():
                self.store.update_site_result(request.task_id, adapter.run(request))
        self.store.queue_for_worker(request.task_id)
        payload = self.store.get(request.task_id)
        payload["worker_queue"]["status"] = "completed"
        for key in request.active_site_keys():
            payload["site_statuses"][key].update(status=key + "_saved", detail="已回查核對儲存內容。",
                                                failure_stage="", failure_reason="", next_action="", exception_type="")
        payload["overall_status"] = "desktop_fast_completed"
        payload["events"].append({"time": "2026-10-20T06:01:00", "status": "manual_captcha_required",
                                  "action": "公務電腦回報消毒", "detail": "登入頁驗證碼尚未完成，請人工處理。",
                                  "failure_stage": "登入消毒系統"})
        self.store.save_payload(request.task_id, payload)
        path = self.store.tasks_dir / (request.task_id + ".json")
        before = path.read_bytes()
        response = self.client.get("/admin/ems", **self.original)
        self.assertEqual(200, response.status_code)
        body = response.get_data(as_text=True)
        self.assertNotIn("第一版不自動送出", body)
        self.assertNotIn("此站有驗證碼；本機電腦瀏覽器開啟後", body)
        self.assertIn("排隊前準備", body)
        self.assertIn("登入頁驗證碼尚未完成，請人工處理。", body)
        self.assertEqual(before, path.read_bytes())
        original = copy.deepcopy(payload)
        rows = app_module.public_pc_event_rows({**payload, "task_id": request.task_id})
        self.assertEqual(4, sum(row.get("action") == "排隊前準備" for row in rows))
        self.assertEqual(original, payload)

    def test_old_manual_workflow_without_worker_queue_keeps_original_captcha_instructions(self):
        request = app_module.request_from_form(self.fixture.valid_task_data())
        event = {"status": "manual_captcha_required", "detail": next(
            adapter for adapter in default_adapters() if adapter.key == "consumables").run(request).detail}
        event["detail"] = "一站通耗材: " + event["detail"]
        rows = app_module.public_pc_event_rows({"task": request.to_dict(), "events": [event]})
        self.assertEqual(1, len(rows))
        self.assertIn("此站有驗證碼", rows[0]["detail"])
        self.assertNotEqual("排隊前準備", rows[0].get("action"))


if __name__ == "__main__":
    unittest.main()
