import copy
import unittest

import app as web


class MileageLifecycleDisplayTests(unittest.TestCase):
    def row(self, *, status="vehicle_mileage_running", action="公務電腦回報任務狀態", detail="", **fields):
        event = {"event_id": "lifecycle", "time": "2026-10-15T00:05:26", "status": status,
                 "action": action, "detail": detail, **fields}
        report = {"site_statuses": {"vehicle_mileage": {"status": "vehicle_mileage_saved"}},
                  "events": [event]}
        before = copy.deepcopy(report)
        row = web.public_pc_event_rows(report)[0]
        self.assertEqual(before, report)
        self.assertEqual(status, row["status"])
        return row

    def test_exact_worker_startup_is_preparation_without_claiming_login(self):
        for prefix in ("", "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，12番 測試 - tyfd10001。"):
            for status in ("vehicle_mileage_running", "manual_captcha_required"):
                with self.subTest(prefix=prefix, status=status):
                    row = self.row(status=status, detail=prefix + "公務電腦 worker 執行車輛里程：public-duty-pc")
                    self.assertEqual("里程｜準備登打｜執行中", row["action"])
                    self.assertEqual("準備登打", row["stage_group"])
                    self.assertNotIn("已登入", row["action"])

    def test_exact_legacy_requeue_is_waiting_execution_not_waiting_confirmation(self):
        detail = "後台重新送出車輛里程，已排入公務電腦處理。"
        for status in ("manual_captcha_required", "queued_for_worker"):
            with self.subTest(status=status):
                row = self.row(status=status, action="後台重新送出車輛里程", detail=detail)
                self.assertEqual("里程｜排隊等候｜等候執行", row["action"])
                self.assertEqual("排隊等候", row["stage_group"])
                self.assertEqual(detail, row["detail"])

    def test_nas_task_event_default_action_preserves_the_startup_phase(self):
        # admin_task_reports assigns status_label when a NAS task has no action.
        row = self.row(action=web.status_label("vehicle_mileage_running"),
                       detail="公務電腦 worker 執行車輛里程：public-duty-pc")
        self.assertEqual("里程｜準備登打｜執行中", row["action"])

    def test_unknown_running_or_errors_do_not_borrow_preparation_or_latest_success(self):
        startup = "公務電腦 worker 執行車輛里程：public-duty-pc"
        for changes in (
            {"detail": "未知里程步驟"},
            {"detail": "錯誤附註：" + startup},
            {"detail": startup + "，登入失敗"},
            {"detail": startup, "status": "vehicle_mileage_failed"},
            {"detail": startup, "action": "其他里程事件"},
            {"detail": startup, "failure_reason": "明確錯誤", "exception_type": "login"},
        ):
            with self.subTest(changes=changes):
                row = self.row(**changes)
                self.assertNotIn("準備登打", row["action"])
                self.assertNotIn("已完成", row["action"])
        row = self.row(detail="未知里程步驟")
        self.assertEqual("里程｜階段未判定｜執行中", row["action"])

    def test_real_captcha_and_nearby_queue_text_are_not_reclassified(self):
        queued = "後台重新送出車輛里程，已排入公務電腦處理。"
        for changes in (
            {"detail": "請完成驗證碼後重試"},
            {"detail": "錯誤附註：" + queued},
            {"detail": queued, "action": "其他里程事件"},
            {"detail": queued, "status": "vehicle_mileage_failed"},
        ):
            values = {"status": "manual_captcha_required", "action": "後台重新送出車輛里程", **changes}
            with self.subTest(changes=changes):
                row = self.row(**values)
                self.assertNotIn("排隊等候", row["action"])

    def test_other_sites_keep_their_existing_history_classification(self):
        for site_key, name in (("duty_work_log", "消防勤務工作紀錄"), ("consumables", "一站通耗材")):
            with self.subTest(site_key=site_key):
                row = self.row(status=f"{site_key}_running", action=f"公務電腦回報{name}",
                               detail="公務電腦 worker 執行車輛里程：public-duty-pc")
                self.assertNotIn("準備登打", row["action"])
                self.assertNotIn("排隊等候", row["action"])
