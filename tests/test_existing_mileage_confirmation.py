import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as web
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.site_diagnostics import diagnostic_payload, merge_diagnostic_fields
from ambulance_bot.task_store import JsonTaskStore, mileage_overlap_confirmation_available


EXISTING_CONFLICT = (
    "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，7番 測試乙 - tyfd10002。"
    "車輛里程操作失敗：Message: 相同案件時間已有不同內容的里程紀錄，停止補登。"
    "新坡15 紀錄 190015（2026/10/20 16:36）；返隊時間：既有 2026/10/20 17:28、"
    "本案 2026/10/20 17:13；前往地點：既有 新生路552巷、本案 桃園市觀音區新生路552巷。"
)


class ExistingMileageDiagnosticTests(unittest.TestCase):
    def test_existing_content_conflict_has_specific_diagnosis_before_generic_login_or_validation(self):
        diagnostic = diagnostic_payload("vehicle_mileage", "vehicle_mileage_failed", EXISTING_CONFLICT)
        self.assertEqual("mileage_existing_conflict", diagnostic["exception_type"])
        self.assertEqual("核對前後里程", diagnostic["failure_stage"])
        self.assertIn("既有", diagnostic["failure_reason"])
        self.assertIn("人工", diagnostic["next_action"])
        self.assertIn("PPE", diagnostic["next_action"])
        self.assertIn("不要", diagnostic["next_action"])

    def test_specific_diagnosis_replaces_legacy_unknown_fields(self):
        merged = merge_diagnostic_fields(dict(
            key="vehicle_mileage", status="vehicle_mileage_failed", detail=EXISTING_CONFLICT,
            exception_type="unknown", failure_stage="開啟車輛里程", failure_reason="舊通用錯誤",
            next_action="查看畫面"))
        self.assertEqual("mileage_existing_conflict", merged["exception_type"])
        self.assertEqual("核對前後里程", merged["failure_stage"])
        self.assertNotEqual("舊通用錯誤", merged["failure_reason"])

    def test_confirmation_availability_requires_exact_mileage_failure_and_matching_detail(self):
        self.assertTrue(mileage_overlap_confirmation_available("vehicle_mileage", dict(
            status="vehicle_mileage_failed", detail=EXISTING_CONFLICT)))
        for site_key, status, detail in (
            ("fuel_record", "vehicle_mileage_failed", EXISTING_CONFLICT),
            ("vehicle_mileage", "vehicle_mileage_saved", EXISTING_CONFLICT),
            ("vehicle_mileage", "vehicle_mileage_running", EXISTING_CONFLICT),
            ("vehicle_mileage", "vehicle_mileage_waiting_confirmation", EXISTING_CONFLICT),
            ("vehicle_mileage", "vehicle_mileage_failed", "查詢逾時"),
        ):
            with self.subTest(site=site_key, status=status, detail=detail):
                self.assertFalse(mileage_overlap_confirmation_available(site_key, dict(
                    status=status, detail=detail, exception_type="mileage_existing_conflict")))


class ExistingMileageConfirmationWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = JsonTaskStore(Path(self.tmp.name) / "tasks")
        for target, value in (("store", self.store), ("artifacts_dir", Path(self.tmp.name))):
            replacement = patch.object(web, target, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        environment = patch.dict(os.environ, {
            "PUBLIC_PC_REPORT_ENABLED": "false", "WORKER_SERVER_URL": "",
            "WORKER_TOKEN": "test-existing-mileage-confirmation-token", "REMOTE_UPDATE_ADMIN_TOKEN": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("report_public_pc_task_event", "sync_public_pc_report_retry_task"):
            replacement = patch.object(web, name)
            replacement.start()
            self.addCleanup(replacement.stop)
        web.app.config.update(TESTING=True)
        self.client = web.app.test_client()
        self.task = AmbulanceReturnRequest.from_dict(dict(
            task_id="existing-mileage-confirmation", created_at="2026-10-20T17:37:29",
            service_type="disaster", vehicle="新坡11", case_time="1636", return_time="1713",
            vehicle_entries=[dict(vehicle="新坡11", mileage="35444"), dict(vehicle="新坡15", mileage="25400")]))
        self.store.create(self.task)
        for vehicle, detail in (("新坡11", "案件與既有用車時間重疊，停止補登。"), ("新坡15", EXISTING_CONFLICT)):
            self.store.update_site_result(self.task.task_id, web.SiteAutomationResult(
                "vehicle_mileage", "里程", "vehicle_mileage_failed", detail), vehicle_key=vehicle)
        self.endpoint = f"/tasks/{self.task.task_id}/sites/vehicle_mileage/complete"

    def data(self, vehicle="新坡15", site="vehicle_mileage", **changes):
        data = dict(vehicle_key=vehicle, confirmed_existing_mileage="1",
                    confirmation_token=web.site_manual_complete_token(self.task.task_id, site, vehicle))
        data.update(changes)
        return data

    def test_existing_conflict_form_requires_acknowledgment_and_valid_token_before_exact_vehicle_completion(self):
        for route in (f"/tasks/{self.task.task_id}", "/admin/disaster"):
            with self.subTest(route=route):
                body = self.client.get(route).get_data(as_text=True)
                self.assertIn("確認新坡15既有里程", body)
                self.assertIn('name="confirmed_existing_mileage" value="1" required', body)
        before = self.store.get(self.task.task_id)
        self.assertEqual(409, self.client.post(self.endpoint, data=self.data(confirmed_existing_mileage="")).status_code)
        self.assertEqual(403, self.client.post(self.endpoint, data=self.data(confirmation_token="bad")).status_code)
        self.assertEqual(before, self.store.get(self.task.task_id))
        self.assertEqual(302, self.client.post(self.endpoint, data=self.data()).status_code)
        completed = self.store.get(self.task.task_id)
        old_site, new_site = before["site_statuses"]["vehicle_mileage"], completed["site_statuses"]["vehicle_mileage"]
        self.assertEqual("completed_by_user", new_site["vehicle_results"]["新坡15"]["status"])
        self.assertEqual(old_site["vehicle_results"]["新坡11"], new_site["vehicle_results"]["新坡11"])
        self.assertEqual(before["task"], completed["task"])
        self.assertIn("未新增或儲存", new_site["vehicle_results"]["新坡15"]["detail"])
        evidence_fields = ("attempt_id", "time", "status", "detail", "vehicle_key",
                           "failure_stage", "failure_reason", "next_action", "exception_type")
        old_attempts = before["site_attempts"]["vehicle_mileage"]
        new_attempts = completed["site_attempts"]["vehicle_mileage"]
        self.assertEqual(len(old_attempts) + 1, len(new_attempts))
        for old, new in zip(old_attempts, new_attempts):
            self.assertEqual({key: old.get(key) for key in evidence_fields},
                             {key: new.get(key) for key in evidence_fields})
        self.assertEqual("", new_site["vehicle_results"]["新坡15"]["exception_type"])
        self.assertEqual(409, self.client.post(self.endpoint, data=self.data()).status_code)
        self.assertEqual(completed, self.store.get(self.task.task_id))

    def test_confirmation_rejects_queued_running_unknown_vehicle_and_whole_station(self):
        before = self.store.get(self.task.task_id)
        for status in ("queued", "claimed"):
            with self.subTest(queue_status=status):
                payload = copy.deepcopy(before)
                payload["worker_queue"]["status"] = status
                self.store.save_payload(self.task.task_id, payload)
                self.assertEqual(409, self.client.post(self.endpoint, data=self.data()).status_code)
                self.assertEqual(payload, self.store.get(self.task.task_id))
        payload = copy.deepcopy(before)
        payload["overall_status"] = "desktop_fast_running"
        self.store.save_payload(self.task.task_id, payload)
        self.assertEqual(409, self.client.post(self.endpoint, data=self.data()).status_code)
        self.store.save_payload(self.task.task_id, before)
        for vehicle in ("", "不存在"):
            with self.subTest(vehicle=vehicle):
                self.assertEqual(409, self.client.post(self.endpoint, data=self.data(vehicle=vehicle)).status_code)
        fuel_endpoint = self.endpoint.replace("vehicle_mileage", "fuel_record")
        self.assertEqual(409, self.client.post(fuel_endpoint, data=self.data(site="fuel_record")).status_code)
        self.assertEqual(before, self.store.get(self.task.task_id))
