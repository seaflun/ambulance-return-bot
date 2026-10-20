import html
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import app as app_module
from ambulance_bot.models import AmbulanceReturnRequest, FuelRecord
from ambulance_bot.task_store import JsonTaskStore


class LanLoginVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = JsonTaskStore(Path(self.tmp.name) / "tasks")
        for name, value in (("store", self.store), ("artifacts_dir", Path(self.tmp.name))):
            replacement = patch.object(app_module, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        environment = patch.dict(os.environ, {"PUBLIC_PC_REPORT_ENABLED": "false",
            "WORKER_SERVER_URL": "", "WORKER_TOKEN": "", "REMOTE_UPDATE_ADMIN_TOKEN": ""})
        environment.start()
        self.addCleanup(environment.stop)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def task(self, **changes):
        values = dict(task_id="lan-login", created_at=datetime.now(), raw_text="",
                      vehicle="新坡95", driver="任務司機", case_reason="急病")
        values.update(changes)
        task = AmbulanceReturnRequest(**values)
        self.store.create(task)
        return task

    def set_site(self, task, site_key, detail, vehicle_key=""):
        self.store.update_site_result(task.task_id, app_module.SiteAutomationResult(
            site_key, site_key, f"{site_key}_saved", detail), vehicle_key=vehicle_key)

    def body(self, path):
        response = self.client.get(path)
        self.assertEqual(200, response.status_code)
        return html.unescape(response.get_data(as_text=True))

    def test_nas_admin_shows_accounts_from_worker_audit_without_local_credentials(self):
        task = self.task()
        self.set_site(task, "duty_work_log", "登入帳號：工作=任務司機 > 出勤人員 > 同步帳號，任務司機，12番 測試甲 - tyfd10001。勤務完成。")
        self.set_site(task, "vehicle_mileage", "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，出勤人員，7番 測試乙 - tyfd10002。里程完成。")
        before = self.store.get(task.task_id)
        with patch.object(app_module, "site_login_account_summaries", side_effect=AssertionError("NAS must not guess credentials")):
            body = self.body("/admin/ems")
        self.assertIn('aria-label="各站登入帳號"', body)
        self.assertIn("12番 測試甲 - tyfd10001（任務司機）", body)
        self.assertIn("7番 測試乙 - tyfd10002（出勤人員）", body)
        self.assertIn("尚未回報", body)
        self.assertEqual(before, self.store.get(task.task_id))

    def test_task_cards_show_reported_accounts_and_mask_identity_numbers(self):
        task = self.task()
        self.set_site(task, "consumables", "登入帳號：耗材=同步帳號 > 任務司機 > 出勤人員，同步帳號，8番 測試丙 - A123456789。耗材完成。")
        body = self.body(f"/tasks/{task.task_id}")
        self.assertIn('aria-label="耗材登入帳號"', body)
        self.assertIn("8番 測試丙 - A123***789（同步帳號）", body)
        self.assertNotIn("A123456789", body)

    def test_each_disaster_vehicle_keeps_its_reported_mileage_account(self):
        task = self.task(service_type="disaster", vehicle="新坡11", vehicle_entries=[
            {"vehicle": "新坡11", "driver": "甲"}, {"vehicle": "新坡15", "driver": "乙"}])
        self.set_site(task, "vehicle_mileage", "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，12番 測試甲 - tyfd10001。完成。", "新坡11")
        self.set_site(task, "vehicle_mileage", "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，7番 測試乙 - tyfd10002。完成。", "新坡15")
        for route in ("/admin/disaster", f"/tasks/{task.task_id}"):
            with self.subTest(route=route):
                body = self.body(route)
                self.assertIn("新坡11：12番 測試甲 - tyfd10001（任務司機）", body)
                self.assertIn("新坡15：7番 測試乙 - tyfd10002（任務司機）", body)

    def test_latest_attempt_account_survives_skip_and_overrides_old_summary(self):
        task = self.task()
        self.set_site(task, "vehicle_mileage", "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，同步帳號，8番 新帳號 - tyfd10003。完成。")
        self.set_site(task, "vehicle_mileage", "已完成，略過")
        payload = self.store.get(task.task_id)
        payload["site_login_accounts"] = {"vehicle_mileage": "1番 舊帳號 - tyfd00001（任務司機）"}
        self.store.save_payload(task.task_id, payload)
        body = self.body("/admin/ems")
        self.assertIn("8番 新帳號 - tyfd10003（同步帳號）", body)
        self.assertNotIn("1番 舊帳號 - tyfd00001", body)

    def test_unreported_sites_keep_trusted_summary_or_show_placeholder(self):
        task = self.task()
        payload = self.store.get(task.task_id)
        payload["site_login_accounts"] = {"duty_work_log": "8番 已回報 - tyfd10003（任務司機優先）"}
        self.store.save_payload(task.task_id, payload)
        body = self.body("/admin/ems")
        self.assertIn("8番 已回報 - tyfd10003（任務司機）", body)
        self.assertEqual(3, body.count('class="login-account-value">尚未回報'))

    def test_combined_mileage_card_shows_mileage_and_fuel_accounts(self):
        task = self.task(fuel_record=FuelRecord(enabled=True))
        self.set_site(task, "vehicle_mileage", "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，12番 里程帳號 - tyfd10001。完成。")
        self.set_site(task, "fuel_record", "登入帳號：加油=任務司機 > 出勤人員 > 同步帳號，同步帳號，7番 加油帳號 - tyfd10002。完成。")
        body = self.body(f"/tasks/{task.task_id}")
        self.assertIn('aria-label="里程登入帳號"', body)
        self.assertIn('aria-label="加油登入帳號"', body)
        self.assertIn("12番 里程帳號 - tyfd10001（任務司機）", body)
        self.assertIn("7番 加油帳號 - tyfd10002（同步帳號）", body)

    def test_queued_task_refreshes_unknown_account_then_renders_worker_report(self):
        task = self.task()
        self.store.queue_for_worker(task.task_id)
        before = self.body(f"/tasks/{task.task_id}")
        self.assertIn("登入帳號：尚未回報", before)
        self.assertIn("window.setTimeout(() => window.location.reload(), 1500)", before)
        self.store.update_site_result(task.task_id, app_module.SiteAutomationResult(
            "vehicle_mileage", "里程", "vehicle_mileage_running",
            "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，12番 剛回報 - tyfd10001。登入里程系統。"))
        after = self.body(f"/tasks/{task.task_id}")
        self.assertIn("登入帳號：12番 剛回報 - tyfd10001（任務司機）", after)
        self.assertIn("window.setTimeout(() => window.location.reload(), 1500)", after)

    def test_current_station_account_is_not_hidden_by_completed_vehicle_results(self):
        task = self.task(service_type="disaster", vehicle="新坡11", vehicle_entries=[
            {"vehicle": "新坡11", "driver": "甲"}, {"vehicle": "新坡15", "driver": "乙"}])
        self.set_site(task, "vehicle_mileage", "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，12番 已完成車 - tyfd10001。完成。", "新坡11")
        self.store.update_site_result(task.task_id, app_module.SiteAutomationResult(
            "vehicle_mileage", "里程", "vehicle_mileage_running",
            "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，7番 執行中 - tyfd10002。填寫里程。"))
        for route in ("/admin/disaster", f"/tasks/{task.task_id}"):
            with self.subTest(route=route):
                body = self.body(route)
                self.assertIn("12番 已完成車 - tyfd10001（任務司機）", body)
                self.assertIn("目前：7番 執行中 - tyfd10002（任務司機）", body)
                self.assertNotIn("新坡15：7番 執行中", body)

    def test_unknown_vehicle_account_is_not_replaced_by_another_vehicles_account(self):
        task = self.task(service_type="disaster", vehicle="新坡11", vehicle_entries=[
            {"vehicle": "新坡11", "driver": "甲"}, {"vehicle": "新坡15", "driver": "乙"}])
        self.set_site(task, "vehicle_mileage", "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，12番 有回報 - tyfd10001。完成。", "新坡11")
        self.set_site(task, "vehicle_mileage", "已完成，略過", "新坡15")
        for route in ("/admin/disaster", f"/tasks/{task.task_id}"):
            with self.subTest(route=route):
                body = self.body(route)
                self.assertIn("新坡11：12番 有回報 - tyfd10001（任務司機）", body)
                self.assertIn("新坡15：尚未回報", body)
