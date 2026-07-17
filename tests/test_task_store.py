import tempfile
import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path

from ambulance_bot.adapters import SiteAutomationResult
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_store import (
    JsonTaskStore,
    SiteCompletionConflictError,
    WorkerClaimConflictError,
    task_completion_snapshot,
    worker_claim_lease_is_active,
)


class JsonTaskStoreTests(unittest.TestCase):
    def test_completion_snapshot_requires_all_four_active_sites(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="snapshot-four-sites",
                created_at=datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            payload = store.create(request)
            for site_key in ("duty_work_log", "vehicle_mileage", "consumables"):
                payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"
            payload["overall_status"] = "desktop_fast_completed"

            snapshot = task_completion_snapshot(payload)

            self.assertEqual(
                snapshot["active_site_keys"],
                [
                    "duty_work_log",
                    "vehicle_mileage",
                    "consumables",
                    "disinfection",
                ],
            )
            self.assertEqual(snapshot["site_count_label"], "四站")
            self.assertEqual(snapshot["completed_count"], 3)
            self.assertEqual(snapshot["remaining_site_keys"], ["disinfection"])
            self.assertFalse(snapshot["all_complete"])

    def test_single_site_terminal_status_cannot_complete_incomplete_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="single-site-not-global",
                created_at=datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            store.create(request)
            store.update_site_result(
                request.task_id,
                SiteAutomationResult(
                    "disinfection",
                    "緊急救護消毒",
                    "disinfection_saved",
                    "saved",
                ),
            )

            updated = store.set_overall_status(
                request.task_id,
                "site_run_completed",
                "單站登打完成：消毒。",
            )

            self.assertEqual(updated["overall_status"], "site_run_completed")
            self.assertFalse(task_completion_snapshot(updated)["all_complete"])
            self.assertEqual(updated["worker_queue"]["status"], "idle")

    def test_last_site_result_atomically_completes_task_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="last-site-upgrade",
                created_at=datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            payload = store.create(request)
            for site_key in ("duty_work_log", "vehicle_mileage", "consumables"):
                payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"
            store.save_payload(request.task_id, payload)

            first = store.update_site_result(
                request.task_id,
                SiteAutomationResult(
                    "disinfection",
                    "緊急救護消毒",
                    "disinfection_saved",
                    "saved",
                ),
            )
            second = store.set_overall_status(
                request.task_id,
                "desktop_fast_completed",
                "重複完成回報。",
            )

            completion_events = [
                event
                for event in second["events"]
                if event.get("status") == "desktop_fast_completed"
            ]
            self.assertEqual(first["overall_status"], "desktop_fast_completed")
            self.assertEqual(second["overall_status"], "desktop_fast_completed")
            self.assertEqual(len(completion_events), 1)

    def test_completion_snapshot_requires_fuel_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "snapshot-five-sites",
                    "created_at": datetime.now().isoformat(),
                    "vehicle": "新坡92",
                    "fuel_record": {
                        "enabled": True,
                        "date": "2026-07-17",
                        "time": "1200",
                        "driver": "包華先",
                        "product": "柴油",
                        "quantity": "30",
                        "unit_price": "30",
                    },
                }
            )
            payload = store.create(request)
            for site_key in (
                "duty_work_log",
                "vehicle_mileage",
                "consumables",
                "disinfection",
            ):
                payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"

            snapshot = task_completion_snapshot(payload)

            self.assertEqual(snapshot["site_count_label"], "五站")
            self.assertEqual(snapshot["total_count"], 5)
            self.assertEqual(snapshot["remaining_site_keys"], ["fuel_record"])
            self.assertFalse(snapshot["all_complete"])

    def _create_legacy_silent_save_task(
        self,
        store: JsonTaskStore,
        task_id: str = "legacy-silent-save",
    ) -> dict:
        request = AmbulanceReturnRequest(
            task_id=task_id,
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        payload = store.create(request)
        payload["overall_status"] = "desktop_fast_completed_with_errors"
        payload["worker_queue"].update(status="idle", last_error="舊版失敗回報。")
        legacy_results = {
            "duty_work_log": (
                "duty_work_log_waiting_confirmation",
                "登入帳號：工作=任務司機優先，8番測試。"
                "waiting_confirmation: 已按下儲存，但未收到儲存成功回應；請人工確認。",
            ),
            "vehicle_mileage": (
                "vehicle_mileage_waiting_confirmation",
                "waiting_confirmation: 已填寫車輛里程並按下儲存；"
                "未偵測到確認視窗，尚未確認伺服器已儲存。",
            ),
            "consumables": (
                "consumables_failed",
                "耗材儲存未取得明確成功回應：未出現確認訊息",
            ),
            "disinfection": (
                "disinfection_waiting_confirmation",
                "waiting_confirmation: disinfection items updated=1; save response not confirmed.",
            ),
        }
        for site_key, (status, detail) in legacy_results.items():
            site = payload["site_statuses"][site_key]
            site.update(
                status=status,
                detail=detail,
                update_context={"legacy": True},
                failure_stage="儲存",
                failure_reason="舊版沒有成功提示",
                next_action="人工確認",
                exception_type="LegacySilentSave",
            )
        payload["site_attempts"]["duty_work_log"] = [
            {
                "attempt_id": "legacy-attempt",
                "time": "2026-07-13T10:18:00",
                "status": "duty_work_log_waiting_confirmation",
                "detail": legacy_results["duty_work_log"][1],
                "site_name": "工作",
                "vehicle_key": "",
            }
        ]
        payload["events"].append(
            {
                "time": "2026-07-13T10:18:01",
                "status": "desktop_fast_completed_with_errors",
                "detail": "舊版回報。",
            }
        )
        store.save_payload(task_id, payload)
        return store.get(task_id)

    def test_reconcile_legacy_silent_save_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            before = self._create_legacy_silent_save_task(store)
            original_events = list(before["events"])
            original_attempts = json.loads(json.dumps(before["site_attempts"], ensure_ascii=False))

            updated, changed = store.reconcile_legacy_silent_save_results("legacy-silent-save")

            self.assertTrue(changed)
            self.assertEqual(updated["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")
            self.assertEqual(updated["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
            self.assertEqual(updated["site_statuses"]["consumables"]["status"], "consumables_saved")
            self.assertEqual(updated["site_statuses"]["disinfection"]["status"], "disinfection_saved")
            self.assertEqual(updated["site_statuses"]["fuel_record"]["status"], "not_started")
            self.assertEqual(updated["overall_status"], "desktop_fast_completed")
            self.assertEqual(updated["worker_queue"]["status"], "completed")
            self.assertEqual(updated["worker_queue"]["last_error"], "")
            self.assertTrue(updated["worker_queue"]["completed_at"])
            for site_key in ("duty_work_log", "vehicle_mileage", "consumables", "disinfection"):
                site = updated["site_statuses"][site_key]
                self.assertNotIn("update_context", site)
                for field in ("failure_stage", "failure_reason", "next_action", "exception_type"):
                    self.assertEqual(site[field], "")
            self.assertEqual(updated["events"][: len(original_events)], original_events)
            self.assertEqual(updated["site_attempts"], original_attempts)
            reconciliation_events = [
                event for event in updated["events"] if event.get("status") == "legacy_silent_save_reconciled"
            ]
            self.assertEqual(len(reconciliation_events), 1)
            for site_name in ("工作", "里程", "耗材", "消毒"):
                self.assertIn(site_name, reconciliation_events[0]["detail"])

            path = store.path_for("legacy-silent-save")
            file_after_first_run = path.read_text(encoding="utf-8")
            event_count_after_first_run = len(updated["events"])
            second, second_changed = store.reconcile_legacy_silent_save_results("legacy-silent-save")

            self.assertFalse(second_changed)
            self.assertEqual(len(second["events"]), event_count_after_first_run)
            self.assertEqual(path.read_text(encoding="utf-8"), file_after_first_run)

    def test_reconcile_vehicle_mileage_update_prompt_after_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="mileage-confirmed-prompt",
                created_at=datetime.now(),
                raw_text="",
                vehicle="新坡93",
            )
            payload = store.create(request)
            payload["site_statuses"]["vehicle_mileage"].update(
                status="vehicle_mileage_waiting_confirmation",
                detail=(
                    "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，"
                    "任務司機，13番 葉宗哲 - tyfd02031。"
                    "waiting_confirmation: vehicle mileage save response not recognized: "
                    "目前的里程數：54745 更新後里程數：54773 是否更新？"
                ),
            )
            store.save_payload(request.task_id, payload)

            updated, changed = store.reconcile_legacy_silent_save_results(request.task_id)

            self.assertTrue(changed)
            self.assertEqual(
                updated["site_statuses"]["vehicle_mileage"]["status"],
                "vehicle_mileage_saved",
            )
            self.assertNotIn(
                "waiting_confirmation",
                updated["site_statuses"]["vehicle_mileage"]["detail"],
            )

    def test_reconcile_vehicle_mileage_multiline_prompt_from_nas_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="mileage-confirmed-multiline-prompt",
                created_at=datetime.now(),
                raw_text="",
                vehicle="新坡93",
            )
            payload = store.create(request)
            payload["site_statuses"]["vehicle_mileage"].update(
                status="vehicle_mileage_waiting_confirmation",
                detail=(
                    "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，"
                    "任務司機，13番 葉宗哲 - tyfd02031。"
                    "waiting_confirmation: vehicle mileage save response not recognized: "
                    "目前的里程數：54745\n更新後里程數：54773\n是否更新？"
                ),
            )
            store.save_payload(request.task_id, payload)

            updated, changed = store.reconcile_legacy_silent_save_results(request.task_id)

            self.assertTrue(changed)
            self.assertEqual(
                updated["site_statuses"]["vehicle_mileage"]["status"],
                "vehicle_mileage_saved",
            )

    def test_reconcile_single_vehicle_mileage_multiline_prompt_with_explicit_null_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="mileage-confirmed-null-results",
                created_at=datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            payload = store.create(request)
            site = payload["site_statuses"]["vehicle_mileage"]
            site.update(
                status="vehicle_mileage_waiting_confirmation",
                detail=(
                    "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，"
                    "任務司機，6番 吳宗耕 - tyfd01471。"
                    "waiting_confirmation: vehicle mileage save response not recognized: "
                    "目前的里程數：20968\n更新後里程數：20998\n是否更新？"
                ),
                vehicle_results=None,
                failure_stage="儲存",
                failure_reason="尚未完成儲存確認",
                next_action="人工確認",
                exception_type="WaitingConfirmation",
            )
            payload["site_statuses"]["duty_work_log"]["status"] = "duty_work_log_saved"
            payload["site_statuses"]["consumables"]["status"] = "consumables_saved"
            payload["site_statuses"]["disinfection"]["status"] = "disinfection_saved"
            payload["overall_status"] = "desktop_fast_completed_with_errors"
            payload["worker_queue"].update(status="idle", last_error="里程待確認")
            store.save_payload(request.task_id, payload)

            updated, changed = store.reconcile_legacy_silent_save_results(request.task_id)

            self.assertTrue(changed)
            updated_site = updated["site_statuses"]["vehicle_mileage"]
            self.assertEqual(updated_site["status"], "vehicle_mileage_saved")
            self.assertIsNone(updated_site["vehicle_results"])
            self.assertNotIn("waiting_confirmation", updated_site["detail"])
            for field in ("failure_stage", "failure_reason", "next_action", "exception_type"):
                self.assertEqual(updated_site[field], "")
            self.assertEqual(updated["overall_status"], "desktop_fast_completed")
            self.assertEqual(updated["worker_queue"]["status"], "completed")
            self.assertEqual(updated["worker_queue"]["last_error"], "")

    def test_reconcile_multivehicle_mileage_prompt_with_explicit_null_results_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "mileage-ambiguous-null-results",
                    "created_at": datetime.now().isoformat(),
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92"},
                        {"vehicle": "新坡93"},
                    ],
                }
            )
            payload = store.create(request)
            site = payload["site_statuses"]["vehicle_mileage"]
            site.update(
                status="vehicle_mileage_waiting_confirmation",
                detail=(
                    "waiting_confirmation: vehicle mileage save response not recognized: "
                    "目前的里程數：20968\n更新後里程數：20998\n是否更新？"
                ),
                vehicle_results=None,
            )
            store.save_payload(request.task_id, payload)
            before = store.path_for(request.task_id).read_text(encoding="utf-8")

            unchanged, changed = store.reconcile_legacy_silent_save_results(request.task_id)

            self.assertFalse(changed)
            self.assertEqual(
                unchanged["site_statuses"]["vehicle_mileage"]["status"],
                "vehicle_mileage_waiting_confirmation",
            )
            self.assertEqual(store.path_for(request.task_id).read_text(encoding="utf-8"), before)

    def test_reconcile_two_vehicle_flag_with_one_null_mileage_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "mileage-ambiguous-one-entry-null-results",
                    "created_at": datetime.now().isoformat(),
                    "two_vehicle": True,
                    "vehicle_entries": [{"vehicle": "新坡92"}],
                }
            )
            payload = store.create(request)
            site = payload["site_statuses"]["vehicle_mileage"]
            site.update(
                status="vehicle_mileage_waiting_confirmation",
                detail=(
                    "waiting_confirmation: vehicle mileage save response not recognized: "
                    "目前的里程數：20968\n更新後里程數：20998\n是否更新？"
                ),
                vehicle_results=None,
            )
            store.save_payload(request.task_id, payload)
            before = store.path_for(request.task_id).read_text(encoding="utf-8")

            unchanged, changed = store.reconcile_legacy_silent_save_results(request.task_id)

            self.assertFalse(changed)
            self.assertEqual(
                unchanged["site_statuses"]["vehicle_mileage"]["status"],
                "vehicle_mileage_waiting_confirmation",
            )
            self.assertEqual(store.path_for(request.task_id).read_text(encoding="utf-8"), before)

    def test_reconcile_vehicle_mileage_update_prompt_in_vehicle_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="mileage-confirmed-vehicle-result",
                created_at=datetime.now(),
                raw_text="",
                vehicle="新坡93",
            )
            payload = store.create(request)
            result_detail = (
                "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，"
                "任務司機，13番 葉宗哲 - tyfd02031。"
                "waiting_confirmation: vehicle mileage save response not recognized: "
                "目前的里程數：54745 更新後里程數：54773 是否更新？"
            )
            site = payload["site_statuses"]["vehicle_mileage"]
            site.update(
                status="vehicle_mileage_waiting_confirmation",
                detail=f"新坡93: {result_detail}",
                failure_stage="儲存",
                failure_reason="尚未完成儲存確認",
                next_action="人工確認",
                exception_type="WaitingConfirmation",
            )
            site["vehicle_results"] = {
                "新坡93": {
                    "status": "vehicle_mileage_waiting_confirmation",
                    "detail": result_detail,
                    "vehicle_label": "新坡93",
                    "failure_stage": "儲存",
                    "failure_reason": "尚未完成儲存確認",
                    "next_action": "人工確認",
                    "exception_type": "WaitingConfirmation",
                }
            }
            payload["site_statuses"]["duty_work_log"]["status"] = "duty_work_log_saved"
            payload["site_statuses"]["consumables"]["status"] = "consumables_saved"
            payload["site_statuses"]["disinfection"]["status"] = "disinfection_saved"
            payload["overall_status"] = "desktop_fast_completed_with_errors"
            payload["worker_queue"].update(status="idle", last_error="里程待確認")
            store.save_payload(request.task_id, payload)

            updated, changed = store.reconcile_legacy_silent_save_results(request.task_id)

            self.assertTrue(changed)
            updated_site = updated["site_statuses"]["vehicle_mileage"]
            self.assertEqual(updated_site["status"], "vehicle_mileage_saved")
            self.assertEqual(updated_site["vehicle_results"]["新坡93"]["status"], "vehicle_mileage_saved")
            self.assertNotIn("waiting_confirmation", updated_site["detail"])
            for field in ("failure_stage", "failure_reason", "next_action", "exception_type"):
                self.assertEqual(updated_site[field], "")
                self.assertEqual(updated_site["vehicle_results"]["新坡93"][field], "")
            self.assertEqual(updated["overall_status"], "desktop_fast_completed")
            self.assertEqual(updated["worker_queue"]["status"], "completed")
            self.assertEqual(updated["worker_queue"]["last_error"], "")

    def test_reconcile_vehicle_mileage_prompt_preserves_other_vehicle_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "mileage-confirmed-mixed-results",
                    "created_at": datetime.now().isoformat(),
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92"},
                        {"vehicle": "新坡93"},
                    ],
                }
            )
            payload = store.create(request)
            confirmed_detail = (
                "waiting_confirmation: vehicle mileage save response not recognized: "
                "目前的里程數：20968 更新後里程數：20998 是否更新？"
            )
            site = payload["site_statuses"]["vehicle_mileage"]
            site.update(
                status="vehicle_mileage_waiting_confirmation",
                detail="新坡92: 等待確認 | 新坡93: 登入失敗",
                failure_stage="儲存",
                failure_reason="尚未完成儲存確認",
                next_action="人工確認",
                exception_type="WaitingConfirmation",
            )
            site["vehicle_results"] = {
                "新坡92": {
                    "status": "vehicle_mileage_waiting_confirmation",
                    "detail": confirmed_detail,
                    "vehicle_label": "新坡92",
                },
                "新坡93": {
                    "status": "vehicle_mileage_failed",
                    "detail": "PPE session returned to login page",
                    "vehicle_label": "新坡93",
                    "failure_stage": "登入",
                    "failure_reason": "登入頁面",
                    "next_action": "重新登入",
                    "exception_type": "WebDriverException",
                },
            }
            store.save_payload(request.task_id, payload)

            updated, changed = store.reconcile_legacy_silent_save_results(request.task_id)

            self.assertTrue(changed)
            updated_site = updated["site_statuses"]["vehicle_mileage"]
            self.assertEqual(updated_site["vehicle_results"]["新坡92"]["status"], "vehicle_mileage_saved")
            self.assertEqual(updated_site["vehicle_results"]["新坡93"]["status"], "vehicle_mileage_failed")
            self.assertEqual(updated_site["status"], "vehicle_mileage_failed")
            self.assertEqual(updated_site["failure_stage"], "登入")
            self.assertEqual(updated_site["failure_reason"], "登入頁面")
            self.assertEqual(updated_site["next_action"], "重新登入")
            self.assertEqual(updated_site["exception_type"], "WebDriverException")

    def test_reconcile_vehicle_mileage_prompt_rejects_malformed_expected_vehicle_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "mileage-confirmed-malformed-results",
                    "created_at": datetime.now().isoformat(),
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92"},
                        {"vehicle": "新坡93"},
                    ],
                }
            )
            payload = store.create(request)
            site = payload["site_statuses"]["vehicle_mileage"]
            site.update(status="vehicle_mileage_waiting_confirmation", detail="損壞的舊資料")
            site["vehicle_results"] = {
                "新坡92": {
                    "status": "vehicle_mileage_waiting_confirmation",
                    "detail": (
                        "waiting_confirmation: vehicle mileage save response not recognized: "
                        "目前的里程數：20968 更新後里程數：20998 是否更新？"
                    ),
                },
                "新坡93": [["status", "vehicle_mileage_saved"]],
            }
            store.save_payload(request.task_id, payload)
            before = store.path_for(request.task_id).read_text(encoding="utf-8")

            unchanged, changed = store.reconcile_legacy_silent_save_results(request.task_id)

            self.assertFalse(changed)
            self.assertEqual(
                unchanged["site_statuses"]["vehicle_mileage"]["status"],
                "vehicle_mileage_waiting_confirmation",
            )
            self.assertEqual(store.path_for(request.task_id).read_text(encoding="utf-8"), before)

    def test_reconcile_legacy_silent_save_results_rejects_near_matches(self):
        duty_detail = "waiting_confirmation: 已按下儲存，但未收到儲存成功回應；請人工確認。"
        cases = (
            ("status-mismatch", "duty_work_log", "duty_work_log_failed", duty_detail, None),
            ("detail-mismatch", "duty_work_log", "duty_work_log_waiting_confirmation", duty_detail[:-1], None),
            (
                "arbitrary-prefix",
                "duty_work_log",
                "duty_work_log_waiting_confirmation",
                f"任意前綴。{duty_detail}",
                None,
            ),
            (
                "disinfection-zero",
                "disinfection",
                "disinfection_waiting_confirmation",
                "waiting_confirmation: disinfection items updated=0; save response not confirmed.",
                None,
            ),
            (
                "disinfection-nonnumeric",
                "disinfection",
                "disinfection_waiting_confirmation",
                "waiting_confirmation: disinfection items updated=many; save response not confirmed.",
                None,
            ),
            ("missing-driver", "duty_work_log", "duty_work_log_failed", "找不到任務司機。", None),
            ("vehicle-mismatch", "consumables", "consumables_failed", "耗材頁車輛候選不符。", None),
            (
                "timeout",
                "vehicle_mileage",
                "vehicle_mileage_waiting_confirmation",
                "等待儲存回應逾時。",
                None,
            ),
            (
                "vehicle-confirmation-extra-error",
                "vehicle_mileage",
                "vehicle_mileage_waiting_confirmation",
                "waiting_confirmation: vehicle mileage save response not recognized: "
                "目前的里程數：54745 更新後里程數：54773 是否更新？ 儲存失敗",
                None,
            ),
            (
                "vehicle-results",
                "duty_work_log",
                "duty_work_log_waiting_confirmation",
                duty_detail,
                {"新坡92": {"status": "duty_work_log_waiting_confirmation", "detail": duty_detail}},
            ),
            (
                "vehicle-result-confirmation-extra-error",
                "vehicle_mileage",
                "vehicle_mileage_waiting_confirmation",
                "新坡93: 待確認",
                {
                    "新坡93": {
                        "status": "vehicle_mileage_waiting_confirmation",
                        "detail": (
                            "waiting_confirmation: vehicle mileage save response not recognized: "
                            "目前的里程數：54745 更新後里程數：54773 是否更新？ 儲存失敗"
                        ),
                    }
                },
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            for index, (label, site_key, status, detail, vehicle_results) in enumerate(cases):
                with self.subTest(label=label):
                    task_id = f"legacy-reject-{index}"
                    request = AmbulanceReturnRequest(
                        task_id=task_id,
                        created_at=datetime.now(),
                        raw_text="",
                        vehicle="新坡92",
                    )
                    payload = store.create(request)
                    site = payload["site_statuses"][site_key]
                    site.update(status=status, detail=detail)
                    if vehicle_results is not None:
                        site["vehicle_results"] = vehicle_results
                    store.save_payload(task_id, payload)
                    before = store.path_for(task_id).read_text(encoding="utf-8")

                    unchanged, changed = store.reconcile_legacy_silent_save_results(task_id)

                    self.assertFalse(changed)
                    self.assertEqual(unchanged["site_statuses"][site_key]["status"], status)
                    self.assertEqual(store.path_for(task_id).read_text(encoding="utf-8"), before)

    def test_reconcile_legacy_silent_save_results_keeps_explicit_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="legacy-mixed",
                created_at=datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            payload = store.create(request)
            payload["overall_status"] = "desktop_fast_completed_with_errors"
            payload["site_statuses"]["duty_work_log"].update(
                status="duty_work_log_waiting_confirmation",
                detail="waiting_confirmation: 已按下儲存，但未收到儲存成功回應；請人工確認。",
            )
            payload["site_statuses"]["consumables"].update(
                status="consumables_failed",
                detail="耗材頁車輛候選不符：新坡92。",
            )
            store.save_payload(request.task_id, payload)

            updated, changed = store.reconcile_legacy_silent_save_results(request.task_id)

            self.assertTrue(changed)
            self.assertEqual(updated["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")
            self.assertEqual(updated["site_statuses"]["consumables"]["status"], "consumables_failed")
            self.assertEqual(updated["overall_status"], "desktop_fast_completed_with_errors")
            self.assertEqual(
                len([event for event in updated["events"] if event.get("status") == "legacy_silent_save_reconciled"]),
                1,
            )

    def test_reconcile_legacy_silent_save_results_rejects_unsafe_audit_and_null_vehicle_results(self):
        duty_detail = "waiting_confirmation: 已按下儲存，但未收到儲存成功回應；請人工確認。"
        cases = (
            (
                "multi-sentence-audit",
                f"登入帳號：工作=8番測試。找不到任務司機。{duty_detail}",
                "missing",
            ),
            ("explicit-null-vehicle-results", duty_detail, None),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            for index, (label, detail, vehicle_results) in enumerate(cases):
                with self.subTest(label=label):
                    task_id = f"legacy-unsafe-{index}"
                    request = AmbulanceReturnRequest(
                        task_id=task_id,
                        created_at=datetime.now(),
                        raw_text="",
                        vehicle="新坡92",
                    )
                    payload = store.create(request)
                    site = payload["site_statuses"]["duty_work_log"]
                    site.update(status="duty_work_log_waiting_confirmation", detail=detail)
                    if vehicle_results != "missing":
                        site["vehicle_results"] = vehicle_results
                    store.save_payload(task_id, payload)
                    before = store.path_for(task_id).read_text(encoding="utf-8")

                    unchanged, changed = store.reconcile_legacy_silent_save_results(task_id)

                    self.assertFalse(changed)
                    self.assertEqual(
                        unchanged["site_statuses"]["duty_work_log"]["status"],
                        "duty_work_log_waiting_confirmation",
                    )
                    self.assertEqual(store.path_for(task_id).read_text(encoding="utf-8"), before)

    def test_reconcile_legacy_silent_save_report_marker_is_stable_until_enqueued(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            self._create_legacy_silent_save_task(store, task_id="legacy-report-marker")

            reconciled, changed = store.reconcile_legacy_silent_save_results("legacy-report-marker")
            marker = reconciled["legacy_silent_save_report"]

            self.assertTrue(changed)
            self.assertTrue(marker["pending"])
            self.assertTrue(marker["event_id"])
            second, second_changed = store.reconcile_legacy_silent_save_results("legacy-report-marker")
            self.assertFalse(second_changed)
            self.assertEqual(second["legacy_silent_save_report"], marker)

            acknowledged, acknowledgement_changed = store.mark_legacy_silent_save_report_enqueued(
                "legacy-report-marker",
                marker["event_id"],
            )
            self.assertTrue(acknowledgement_changed)
            self.assertFalse(acknowledged["legacy_silent_save_report"]["pending"])
            self.assertTrue(acknowledged["legacy_silent_save_report"]["enqueued_at"])

            file_after_ack = store.path_for("legacy-report-marker").read_text(encoding="utf-8")
            duplicate, duplicate_changed = store.mark_legacy_silent_save_report_enqueued(
                "legacy-report-marker",
                marker["event_id"],
            )
            self.assertFalse(duplicate_changed)
            self.assertFalse(duplicate["legacy_silent_save_report"]["pending"])
            self.assertEqual(
                store.path_for("legacy-report-marker").read_text(encoding="utf-8"),
                file_after_ack,
            )

    def test_pending_reconciliation_report_does_not_block_operational_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="pending-report-operational-completion",
                created_at=datetime.now(),
                raw_text="",
            )
            payload = store.create(request)
            for site in payload["site_statuses"].values():
                site["status"] = "completed_by_user"
            payload["site_statuses"]["duty_work_log"].update(
                status="duty_work_log_waiting_confirmation",
                detail="waiting_confirmation",
            )
            payload["overall_status"] = "desktop_fast_completed_with_errors"
            payload["legacy_silent_save_report"] = {
                "event_id": "pending-operational-event",
                "pending": True,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "enqueued_at": "",
            }
            store.save_payload(request.task_id, payload)

            completed = store.mark_site_completed(request.task_id, "duty_work_log")

            self.assertEqual(completed["overall_status"], "desktop_fast_completed")
            self.assertTrue(completed["legacy_silent_save_report"]["pending"])

    def test_task_id_cannot_escape_tasks_directory_with_windows_backslashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JsonTaskStore(root / "tasks")
            cases_dir = root / "cases"
            cases_dir.mkdir()
            neighbor = cases_dir / "latest.json"
            neighbor.write_text('{"secret": true}', encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                store.get(r"..\cases\latest")
            with self.assertRaises(FileNotFoundError):
                store.delete(r"..\cases\latest")
            self.assertEqual(neighbor.read_text(encoding="utf-8"), '{"secret": true}')

    def test_create_and_update_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="task-1",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="91A1",
            )

            payload = store.create(request)
            self.assertEqual(payload["overall_status"], "created")
            self.assertEqual(payload["worker_queue"]["status"], "idle")
            self.assertEqual(
                list(payload["site_statuses"]),
                ["duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"],
            )
            self.assertTrue((Path(tmp) / "task-1.json").exists())

            store.update_site_result(
                "task-1",
                SiteAutomationResult("vehicle_mileage", "車輛里程", "prefill_ready", "ready"),
            )
            updated = store.get("task-1")
            self.assertEqual(updated["site_statuses"]["vehicle_mileage"]["status"], "prefill_ready")
            self.assertEqual(len(updated["site_attempts"]["vehicle_mileage"]), 1)
            self.assertEqual(updated["site_attempts"]["vehicle_mileage"][0]["status"], "prefill_ready")

            pending = store.get("task-1")
            pending["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_waiting_confirmation"
            store.save_payload("task-1", pending)
            store.mark_site_completed("task-1", "vehicle_mileage")
            completed = store.get("task-1")
            self.assertEqual(completed["site_statuses"]["vehicle_mileage"]["status"], "completed_by_user")
            self.assertEqual(completed["site_attempts"]["vehicle_mileage"][-1]["status"], "completed_by_user")

    def test_manual_site_completion_rejects_non_waiting_status_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-not-waiting", created_at=datetime.now(), raw_text="")
            store.create(request)

            with self.assertRaises(SiteCompletionConflictError):
                store.mark_site_completed(request.task_id, "vehicle_mileage")

            self.assertEqual(store.get(request.task_id)["site_statuses"]["vehicle_mileage"]["status"], "not_started")

    def test_manual_site_completion_finishes_overall_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="task-manual-finish",
                created_at=datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            payload = store.create(request)
            for site_key, status in {
                "duty_work_log": "duty_work_log_saved",
                "vehicle_mileage": "local_pc_ready",
                "consumables": "consumables_saved",
                "disinfection": "disinfection_saved",
            }.items():
                payload["site_statuses"][site_key].update(status=status, detail="test")
            store.save_payload(request.task_id, payload)
            store.queue_for_worker(request.task_id)
            claimed = store.claim_next_for_worker("worker-a")
            self.assertIsNotNone(claimed)
            payload = store.get(request.task_id)
            payload["site_statuses"]["vehicle_mileage"].update(
                status="vehicle_mileage_waiting_confirmation",
                detail="test",
            )
            store.save_payload(request.task_id, payload)
            failed = store.set_overall_status(
                request.task_id,
                "desktop_fast_completed_with_errors",
                "舊失敗。",
            )
            self.assertEqual(failed["worker_queue"]["status"], "completed")
            self.assertEqual(failed["worker_queue"]["last_error"], "舊失敗。")

            completed = store.mark_site_completed(request.task_id, "vehicle_mileage")

            self.assertEqual(completed["site_statuses"]["vehicle_mileage"]["status"], "completed_by_user")
            self.assertEqual(completed["overall_status"], "desktop_fast_completed")
            self.assertEqual(completed["worker_queue"]["status"], "completed")
            self.assertEqual(completed["worker_queue"]["lease_expires_at"], "")
            self.assertEqual(completed["worker_queue"]["last_error"], "")

    def test_manual_site_completion_keeps_overall_failure_when_site_still_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="task-manual-mixed",
                created_at=datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            payload = store.create(request)
            payload["overall_status"] = "desktop_fast_completed_with_errors"
            for site_key, status in {
                "duty_work_log": "duty_work_log_saved",
                "vehicle_mileage": "vehicle_mileage_waiting_confirmation",
                "consumables": "consumables_failed",
                "disinfection": "disinfection_saved",
            }.items():
                payload["site_statuses"][site_key].update(status=status, detail="test")
            store.save_payload(request.task_id, payload)

            completed = store.mark_site_completed(request.task_id, "vehicle_mileage")

            self.assertEqual(completed["site_statuses"]["vehicle_mileage"]["status"], "completed_by_user")
            self.assertEqual(completed["site_statuses"]["consumables"]["status"], "consumables_failed")
            self.assertEqual(completed["overall_status"], "desktop_fast_completed_with_errors")

    def test_worker_queue_state_tracks_queue_claim_and_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-queue", created_at=datetime.now(), raw_text="")
            store.create(request)

            queued = store.queue_for_worker("task-queue")
            self.assertEqual(queued["worker_queue"]["status"], "queued")
            self.assertTrue(queued["worker_queue"]["queued_at"])

            claimed = store.claim_next_for_worker("worker-a")
            assert claimed is not None
            self.assertEqual(claimed["worker_queue"]["status"], "claimed")
            self.assertEqual(claimed["worker_queue"]["worker_id"], "worker-a")
            self.assertTrue(claimed["worker_queue"]["claimed_at"])

            completed = store.set_overall_status("task-queue", "desktop_fast_completed", "done")
            self.assertEqual(completed["worker_queue"]["status"], "completed")
            self.assertTrue(completed["worker_queue"]["completed_at"])

    def test_worker_claim_reclaims_task_after_lease_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-lease", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.queue_for_worker("task-lease")
            first = store.claim_next_for_worker("worker-a")
            assert first is not None
            first["worker_queue"]["lease_expires_at"] = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            store.save_payload("task-lease", first)

            reclaimed = store.claim_next_for_worker("worker-b")

            self.assertIsNotNone(reclaimed)
            assert reclaimed is not None
            self.assertEqual(reclaimed["worker_queue"]["worker_id"], "worker-b")
            self.assertEqual(reclaimed["worker_queue"]["status"], "claimed")
            self.assertEqual(reclaimed["worker_queue"]["claim_attempt"], "2")
            self.assertTrue(reclaimed["worker_queue"]["lease_expires_at"])

    def test_worker_claim_reclaims_legacy_claim_without_attempt_as_second_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-legacy-lease", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.queue_for_worker(request.task_id)
            first = store.claim_next_for_worker("worker-a")
            assert first is not None
            first["worker_queue"].pop("claim_attempt", None)
            first["worker_queue"]["lease_expires_at"] = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            store.save_payload(request.task_id, first)

            reclaimed = store.claim_next_for_worker("worker-b")

            self.assertIsNotNone(reclaimed)
            assert reclaimed is not None
            self.assertEqual(reclaimed["worker_queue"]["worker_id"], "worker-b")
            self.assertEqual(reclaimed["worker_queue"]["claim_attempt"], "2")

    def test_claim_specific_task_creates_fenced_claim_and_is_idempotent_for_same_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-manual-claim", created_at=datetime.now(), raw_text="")
            store.create(request)

            first = store.claim_task_for_worker(request.task_id, "worker-a")
            second = store.claim_task_for_worker(request.task_id, "worker-a")

            self.assertEqual(first["worker_queue"]["status"], "claimed")
            self.assertTrue(first["worker_queue"]["claim_id"])
            self.assertEqual(second["worker_queue"]["claim_id"], first["worker_queue"]["claim_id"])
            self.assertEqual(second["worker_queue"]["claim_attempt"], "1")

    def test_claim_specific_task_rejects_fully_completed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-already-complete", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site_key, site in payload["site_statuses"].items():
                if site_key != "fuel_record":
                    site["status"] = "completed_by_user"
            store.save_payload(request.task_id, payload)

            with self.assertRaises(WorkerClaimConflictError) as raised:
                store.claim_task_for_worker(request.task_id, "worker-a")

            self.assertEqual(raised.exception.code, "task_already_completed")

    def test_claim_specific_task_allows_failed_or_needs_update_task(self):
        for status in ("vehicle_mileage_failed", "vehicle_mileage_needs_update"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                store = JsonTaskStore(Path(tmp))
                request = AmbulanceReturnRequest(task_id=f"task-retry-{status}", created_at=datetime.now(), raw_text="")
                payload = store.create(request)
                payload["site_statuses"]["vehicle_mileage"]["status"] = status
                store.save_payload(request.task_id, payload)

                claimed = store.claim_task_for_worker(request.task_id, "worker-a")

                self.assertEqual(claimed["worker_queue"]["status"], "claimed")

    def test_waiting_confirmation_blocks_queue_and_manual_claim_for_every_site(self):
        waiting_statuses = (
            ("duty_work_log", "duty_work_log_waiting_confirmation"),
            ("vehicle_mileage", "vehicle_mileage_waiting_confirmation"),
            ("fuel_record", "fuel_record_waiting_confirmation"),
            ("consumables", "consumables_waiting_confirmation"),
            ("disinfection", "disinfection_waiting_confirmation"),
        )
        for action in ("queue", "manual_claim"):
            for site_key, status in waiting_statuses:
                with self.subTest(action=action, site_key=site_key), tempfile.TemporaryDirectory() as tmp:
                    store = JsonTaskStore(Path(tmp))
                    request = AmbulanceReturnRequest(
                        task_id=f"task-waiting-{action}-{site_key}",
                        created_at=datetime.now(),
                        raw_text="",
                    )
                    payload = store.create(request)
                    payload["site_statuses"][site_key]["status"] = status
                    store.save_payload(request.task_id, payload)

                    with self.assertRaises(WorkerClaimConflictError) as raised:
                        if action == "queue":
                            store.queue_for_worker(request.task_id)
                        else:
                            store.claim_task_for_worker(request.task_id, "worker-a")

                    self.assertEqual(raised.exception.code, "manual_confirmation_required")
                    self.assertEqual(store.get(request.task_id)["worker_queue"]["status"], "idle")

    def test_auto_claim_skips_legacy_waiting_confirmation_for_every_site(self):
        waiting_statuses = (
            ("duty_work_log", "duty_work_log_waiting_confirmation"),
            ("vehicle_mileage", "vehicle_mileage_waiting_confirmation"),
            ("fuel_record", "fuel_record_waiting_confirmation"),
            ("consumables", "consumables_waiting_confirmation"),
            ("disinfection", "disinfection_waiting_confirmation"),
        )
        for site_key, status in waiting_statuses:
            with self.subTest(site_key=site_key), tempfile.TemporaryDirectory() as tmp:
                store = JsonTaskStore(Path(tmp))
                waiting_request = AmbulanceReturnRequest(
                    task_id=f"task-legacy-waiting-{site_key}",
                    created_at=datetime.now(),
                    raw_text="",
                )
                waiting_payload = store.create(waiting_request)
                waiting_payload["site_statuses"][site_key]["status"] = status
                waiting_payload["overall_status"] = "queued_for_worker"
                waiting_payload["worker_queue"].update(status="queued", queue_id="legacy-waiting")
                store.save_payload(waiting_request.task_id, waiting_payload)

                runnable_request = AmbulanceReturnRequest(
                    task_id=f"task-runnable-after-{site_key}",
                    created_at=datetime.now(),
                    raw_text="",
                )
                store.create(runnable_request)
                store.queue_for_worker(runnable_request.task_id)

                claimed = store.claim_next_for_worker("worker-a")

                self.assertIsNotNone(claimed)
                assert claimed is not None
                self.assertEqual(claimed["task"]["task_id"], runnable_request.task_id)
                self.assertEqual(store.get(waiting_request.task_id)["worker_queue"]["status"], "queued")

    def test_manual_confirmation_allows_queueing_remaining_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-confirm-then-queue", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            payload["site_statuses"]["vehicle_mileage"].update(
                status="vehicle_mileage_waiting_confirmation",
                detail="請先在官方頁確認。",
            )
            store.save_payload(request.task_id, payload)

            confirmed = store.mark_site_completed(request.task_id, "vehicle_mileage")
            queued = store.queue_for_worker(request.task_id)

            self.assertEqual(confirmed["site_statuses"]["vehicle_mileage"]["status"], "completed_by_user")
            self.assertEqual(queued["worker_queue"]["status"], "queued")

    def test_claim_specific_task_rejects_active_other_worker_but_reclaims_expired_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-manual-conflict", created_at=datetime.now(), raw_text="")
            store.create(request)
            first = store.claim_task_for_worker(request.task_id, "worker-a")

            with self.assertRaises(WorkerClaimConflictError):
                store.claim_task_for_worker(request.task_id, "worker-b")

            first["worker_queue"]["lease_expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")
            store.save_payload(request.task_id, first)
            reclaimed = store.claim_task_for_worker(request.task_id, "worker-b")
            self.assertEqual(reclaimed["worker_queue"]["worker_id"], "worker-b")
            self.assertEqual(reclaimed["worker_queue"]["claim_attempt"], "2")

    def test_claim_specific_task_reclaims_legacy_claim_without_attempt_as_second_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="task-legacy-manual-conflict",
                created_at=datetime.now(),
                raw_text="",
            )
            store.create(request)
            first = store.claim_task_for_worker(request.task_id, "worker-a")
            first["worker_queue"].pop("claim_attempt", None)
            first["worker_queue"]["lease_expires_at"] = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            store.save_payload(request.task_id, first)

            reclaimed = store.claim_task_for_worker(request.task_id, "worker-b")

            self.assertEqual(reclaimed["worker_queue"]["worker_id"], "worker-b")
            self.assertEqual(reclaimed["worker_queue"]["claim_attempt"], "2")

    def test_worker_claim_lease_active_requires_claimed_status_and_future_valid_expiry(self):
        future = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
        past = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")

        self.assertTrue(worker_claim_lease_is_active({"worker_queue": {"status": "claimed", "lease_expires_at": future}}))
        self.assertFalse(worker_claim_lease_is_active({"worker_queue": {"status": "claimed", "lease_expires_at": past}}))
        self.assertFalse(worker_claim_lease_is_active({"worker_queue": {"status": "queued", "lease_expires_at": future}}))
        self.assertFalse(worker_claim_lease_is_active({"worker_queue": {"status": "claimed", "lease_expires_at": "bad"}}))

    def test_worker_running_status_keeps_claim_lease_reclaimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-running-lease", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.queue_for_worker("task-running-lease")
            store.claim_next_for_worker("worker-a")

            running = store.set_overall_status("task-running-lease", "worker_running", "running")

            self.assertEqual(running["worker_queue"]["status"], "claimed")
            self.assertTrue(running["worker_queue"]["lease_expires_at"])
            running["worker_queue"]["lease_expires_at"] = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            store.save_payload("task-running-lease", running)
            reclaimed = store.claim_next_for_worker("worker-b")
            self.assertIsNotNone(reclaimed)

    def test_expire_stale_running_sites_keeps_old_site_while_worker_claim_lease_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-active-old-site", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.queue_for_worker(request.task_id)
            payload = store.claim_next_for_worker("worker-a")
            assert payload is not None
            site = payload["site_statuses"]["vehicle_mileage"]
            site["status"] = "vehicle_mileage_running"
            site["updated_at"] = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
            store.save_payload(request.task_id, payload)

            unchanged = store.expire_stale_running_sites(request.task_id, 600, "stale")

            self.assertEqual(unchanged["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_running")
            self.assertEqual(unchanged["worker_queue"]["status"], "claimed")

    def test_claim_quarantines_corrupt_json_and_continues_to_valid_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            corrupt = store.path_for("000-corrupt")
            corrupt.write_text("{broken", encoding="utf-8")
            request = AmbulanceReturnRequest(task_id="task-valid", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.queue_for_worker("task-valid")

            try:
                claimed = store.claim_next_for_worker("worker-a")
            except json.JSONDecodeError as exc:
                self.fail(f"corrupt task JSON must be quarantined instead of crashing claim: {exc}")

            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["task"]["task_id"], "task-valid")
            self.assertFalse(corrupt.exists())
            self.assertEqual(len(list((Path(tmp) / "quarantine").glob("000-corrupt-*.json.corrupt"))), 1)

    def test_claim_quarantines_task_with_unsafe_embedded_task_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            bad_request = AmbulanceReturnRequest(task_id="000-malicious", created_at=datetime.now(), raw_text="")
            bad_payload = store.create(bad_request)
            bad_payload = store.queue_for_worker("000-malicious")
            bad_payload["task"]["task_id"] = r"..\cases\latest"
            bad_path = store.path_for("000-malicious")
            bad_path.write_text(json.dumps(bad_payload), encoding="utf-8")
            valid_request = AmbulanceReturnRequest(task_id="task-valid", created_at=datetime.now(), raw_text="")
            store.create(valid_request)
            store.queue_for_worker("task-valid")

            claimed = store.claim_next_for_worker("worker-a")

            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["task"]["task_id"], "task-valid")
            self.assertFalse(bad_path.exists())
            self.assertEqual(len(list((Path(tmp) / "quarantine").glob("000-malicious-*.json.corrupt"))), 1)

    def test_list_recent_quarantines_corrupt_json_and_returns_valid_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-valid", created_at=datetime.now(), raw_text="")
            store.create(request)
            corrupt = store.path_for("zzz-corrupt")
            corrupt.write_text("not-json", encoding="utf-8")

            try:
                recent = store.list_recent()
            except json.JSONDecodeError as exc:
                self.fail(f"corrupt task JSON must be quarantined instead of crashing list: {exc}")

            self.assertEqual([item["task"]["task_id"] for item in recent], ["task-valid"])
            self.assertFalse(corrupt.exists())
            self.assertEqual(len(list((Path(tmp) / "quarantine").glob("zzz-corrupt-*.json.corrupt"))), 1)

    def test_list_recent_quarantines_task_with_unsafe_embedded_task_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="unsafe-list", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            payload["task"]["task_id"] = r"..\cases\latest"
            unsafe_path = store.path_for("unsafe-list")
            unsafe_path.write_text(json.dumps(payload), encoding="utf-8")

            recent = store.list_recent()

            self.assertEqual(recent, [])
            self.assertFalse(unsafe_path.exists())
            self.assertEqual(len(list((Path(tmp) / "quarantine").glob("unsafe-list-*.json.corrupt"))), 1)

    def test_site_attempts_preserve_retry_history_per_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-retry", created_at=datetime.now(), raw_text="")
            store.create(request)

            store.update_site_result(
                "task-retry",
                SiteAutomationResult("disinfection", "消毒", "disinfection_failed", "login failed"),
            )
            store.update_site_result(
                "task-retry",
                SiteAutomationResult("disinfection", "消毒", "disinfection_saved", "retry ok"),
            )

            payload = store.get("task-retry")
            attempts = payload["site_attempts"]["disinfection"]
            self.assertEqual([item["status"] for item in attempts], ["disinfection_failed", "disinfection_saved"])
            self.assertEqual(attempts[0]["detail"], "login failed")
            self.assertEqual(attempts[1]["detail"], "retry ok")

    def test_abort_running_task_marks_running_sites_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-abort", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.set_overall_status("task-abort", "desktop_fast_running", "running")
            store.update_site_result(
                "task-abort",
                SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_running", "running"),
            )

            aborted = store.abort_running_task("task-abort", "使用者中止登打。")

            self.assertEqual(aborted["overall_status"], "desktop_fast_completed_with_errors")
            self.assertEqual(aborted["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_failed")
            self.assertEqual(aborted["site_statuses"]["vehicle_mileage"]["detail"], "使用者中止登打。")
            self.assertEqual(aborted["site_attempts"]["vehicle_mileage"][-1]["status"], "vehicle_mileage_failed")
            self.assertEqual(aborted["events"][-1]["status"], "desktop_fast_completed_with_errors")

    def test_abort_running_task_rejects_reassigned_claim_generation_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-abort-reassigned", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            payload["worker_queue"].update(
                {
                    "status": "claimed",
                    "claim_id": "claim-b",
                    "worker_id": "worker-b",
                }
            )
            payload["overall_status"] = "desktop_fast_running"
            payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_running"
            store.save_payload(request.task_id, payload)

            with self.assertRaises(WorkerClaimConflictError) as raised:
                store.abort_running_task(
                    request.task_id,
                    execution_lease_active=True,
                    expected_claim_id="claim-a",
                )

            self.assertEqual(raised.exception.code, "worker_claim_conflict")
            current = store.get(request.task_id)
            self.assertEqual(current["worker_queue"]["status"], "claimed")
            self.assertEqual(current["worker_queue"]["claim_id"], "claim-b")
            self.assertEqual(current["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_running")

    def test_requeue_issues_new_queue_generation_and_stale_abort_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-requeue-generation", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued_a = store.queue_for_worker(request.task_id)
            queue_a = queued_a["worker_queue"]["queue_id"]
            queued_b = store.queue_for_worker(request.task_id)
            queue_b = queued_b["worker_queue"]["queue_id"]

            self.assertTrue(queue_a)
            self.assertTrue(queue_b)
            self.assertNotEqual(queue_a, queue_b)
            with self.assertRaises(WorkerClaimConflictError) as raised:
                store.abort_running_task(
                    request.task_id,
                    expected_claim_id="",
                    expected_queue_id=queue_a,
                )

            self.assertEqual(raised.exception.code, "worker_claim_conflict")
            current = store.get(request.task_id)
            self.assertEqual(current["worker_queue"]["status"], "queued")
            self.assertEqual(current["worker_queue"]["queue_id"], queue_b)

    def test_queue_generation_is_preserved_when_claimed_and_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp), claim_lease_seconds=60)
            request = AmbulanceReturnRequest(task_id="task-queue-generation-claim", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued = store.queue_for_worker(request.task_id)
            queue_id = queued["worker_queue"]["queue_id"]
            claimed = store.claim_next_for_worker("worker-a")
            assert claimed is not None
            self.assertEqual(claimed["worker_queue"]["queue_id"], queue_id)
            claimed["worker_queue"]["lease_expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat(
                timespec="seconds"
            )
            store.save_payload(request.task_id, claimed)

            reclaimed = store.claim_next_for_worker("worker-b")
            assert reclaimed is not None
            self.assertEqual(reclaimed["worker_queue"]["queue_id"], queue_id)

    def test_requeue_preserves_monotonic_claim_attempt_across_generations(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-requeue-attempt", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued_a = store.queue_for_worker(request.task_id)
            claimed_a = store.claim_task_for_worker(request.task_id, "worker-a")
            self.assertEqual(claimed_a["worker_queue"]["claim_attempt"], "1")
            store.abort_running_task(
                request.task_id,
                expected_claim_id=claimed_a["worker_queue"]["claim_id"],
                expected_queue_id=queued_a["worker_queue"]["queue_id"],
            )

            store.queue_for_worker(request.task_id)
            claimed_b = store.claim_task_for_worker(request.task_id, "worker-b")

            self.assertEqual(claimed_b["worker_queue"]["claim_attempt"], "2")

    def test_requeue_infers_prior_attempt_from_legacy_completed_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-legacy-requeue", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued_a = store.queue_for_worker(request.task_id)
            claimed_a = store.claim_task_for_worker(request.task_id, "worker-a")
            completed_a = store.abort_running_task(
                request.task_id,
                expected_claim_id=claimed_a["worker_queue"]["claim_id"],
                expected_queue_id=queued_a["worker_queue"]["queue_id"],
            )
            completed_a["worker_queue"].pop("claim_attempt", None)
            store.save_payload(request.task_id, completed_a)

            store.queue_for_worker(request.task_id)
            claimed_b = store.claim_task_for_worker(request.task_id, "worker-b")

            self.assertEqual(claimed_b["worker_queue"]["claim_attempt"], "2")

    def test_requeue_after_never_claimed_abort_keeps_first_claim_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-never-claimed-requeue", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued = store.queue_for_worker(request.task_id)
            store.abort_running_task(
                request.task_id,
                expected_claim_id="",
                expected_queue_id=queued["worker_queue"]["queue_id"],
            )

            store.queue_for_worker(request.task_id)
            first_claim = store.claim_task_for_worker(request.task_id, "worker-a")

            self.assertEqual(first_claim["worker_queue"]["claim_attempt"], "1")

    def test_task_edit_preserves_monotonic_claim_attempt_for_next_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-edit-attempt", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued_a = store.queue_for_worker(request.task_id)
            claimed_a = store.claim_task_for_worker(request.task_id, "worker-a")
            store.abort_running_task(
                request.task_id,
                expected_claim_id=claimed_a["worker_queue"]["claim_id"],
                expected_queue_id=queued_a["worker_queue"]["queue_id"],
            )

            store.update_task(
                request.task_id,
                request,
                changed_site_keys={"vehicle_mileage"},
            )
            idle = store.get(request.task_id)
            self.assertEqual(idle["worker_queue"]["claim_attempt"], "1")
            store.queue_for_worker(request.task_id)
            claimed_b = store.claim_task_for_worker(request.task_id, "worker-b")

            self.assertEqual(claimed_b["worker_queue"]["claim_attempt"], "2")

    def test_task_edit_infers_prior_attempt_from_legacy_completed_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-legacy-edit-attempt", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued_a = store.queue_for_worker(request.task_id)
            claimed_a = store.claim_task_for_worker(request.task_id, "worker-a")
            completed_a = store.abort_running_task(
                request.task_id,
                expected_claim_id=claimed_a["worker_queue"]["claim_id"],
                expected_queue_id=queued_a["worker_queue"]["queue_id"],
            )
            completed_a["worker_queue"].pop("claim_attempt", None)
            store.save_payload(request.task_id, completed_a)

            store.update_task(
                request.task_id,
                request,
                changed_site_keys={"vehicle_mileage"},
            )
            store.queue_for_worker(request.task_id)
            claimed_b = store.claim_task_for_worker(request.task_id, "worker-b")

            self.assertEqual(claimed_b["worker_queue"]["claim_attempt"], "2")

    def test_expire_stale_running_sites_marks_old_running_site_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-stale", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.set_overall_status("task-stale", "desktop_fast_running", "running")
            store.update_site_result(
                "task-stale",
                SiteAutomationResult("consumables", "一站通耗材", "consumables_running", "running"),
            )
            path = store.path_for("task-stale")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["site_statuses"]["consumables"]["updated_at"] = (
                datetime.now() - timedelta(minutes=11)
            ).isoformat(timespec="seconds")
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            expired = store.expire_stale_running_sites(
                "task-stale",
                600,
                "登打流程超過 10 分鐘未回報，已自動中止。",
            )

            self.assertEqual(expired["overall_status"], "desktop_fast_completed_with_errors")
            self.assertEqual(expired["site_statuses"]["consumables"]["status"], "consumables_failed")
            self.assertEqual(expired["site_statuses"]["consumables"]["detail"], "登打流程超過 10 分鐘未回報，已自動中止。")
            self.assertEqual(expired["site_attempts"]["consumables"][-1]["status"], "consumables_failed")
            self.assertEqual(expired["events"][-1]["status"], "desktop_fast_completed_with_errors")

    def test_site_result_records_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-diag", created_at=datetime.now(), raw_text="")
            store.create(request)

            store.update_site_result(
                "task-diag",
                SiteAutomationResult("consumables", "一站通耗材", "consumables_failed", "SSO login failed"),
            )

            payload = store.get("task-diag")
            site = payload["site_statuses"]["consumables"]
            attempt = payload["site_attempts"]["consumables"][0]
            self.assertEqual(site["failure_stage"], "登入一站通")
            self.assertIn("登入", site["failure_reason"])
            self.assertEqual(attempt["failure_stage"], "登入一站通")
            self.assertIn("驗證碼", attempt["next_action"])

    def test_worker_queue_state_reads_legacy_overall_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            legacy_payload = {
                "task": {"task_id": "legacy"},
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "overall_status": "queued_for_worker",
                "site_statuses": {},
                "events": [],
            }
            store.path_for("legacy").write_text(__import__("json").dumps(legacy_payload, ensure_ascii=False), encoding="utf-8")

            claimed = store.claim_next_for_worker("worker-b")
            assert claimed is not None
            self.assertEqual(claimed["worker_queue"]["status"], "claimed")
            self.assertEqual(claimed["worker_queue"]["worker_id"], "worker-b")

    def test_delete_removes_task_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="delete-me", created_at=datetime.now(), raw_text="")
            store.create(request)

            store.delete("delete-me")

            self.assertFalse((Path(tmp) / "delete-me.json").exists())
            with self.assertRaises(FileNotFoundError):
                store.get("delete-me")

    def test_cleanup_keeps_old_unfinished_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="old-task", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            payload["updated_at"] = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
            store.path_for("old-task").write_text(__import__("json").dumps(payload), encoding="utf-8")

            recent = store.list_recent()

            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["task"]["task_id"], "old-task")
            self.assertTrue((Path(tmp) / "old-task.json").exists())

    def test_cleanup_keeps_fully_done_tasks_until_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="done-task", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site in payload["site_statuses"].values():
                site["status"] = "completed_by_user"
            store.save_payload("done-task", payload)

            self.assertEqual(len(store.list_recent()), 1)
            self.assertTrue((Path(tmp) / "done-task.json").exists())

    def test_cleanup_keeps_fully_done_tasks_for_mileage_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="done-history-task", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site in payload["site_statuses"].values():
                site["status"] = "completed_by_user"
            payload["updated_at"] = (datetime.now() - timedelta(days=6, hours=23)).isoformat(timespec="seconds")
            store.path_for("done-history-task").write_text(__import__("json").dumps(payload), encoding="utf-8")

            self.assertEqual(len(store.list_recent()), 1)
            self.assertTrue((Path(tmp) / "done-history-task.json").exists())

    def test_cleanup_treats_unselected_fuel_site_as_done_for_four_site_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="four-site-done", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site_key, site in payload["site_statuses"].items():
                if site_key != "fuel_record":
                    site["status"] = "completed_by_user"
            payload["updated_at"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
            store.path_for("four-site-done").write_text(json.dumps(payload), encoding="utf-8")

            recent = store.list_recent()

            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["task"]["task_id"], "four-site-done")

    def test_task_edit_clears_stale_per_vehicle_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-edit", created_at=datetime.now(), raw_text="", vehicle="新坡91")
            payload = store.create(request)
            payload["site_statuses"]["consumables"]["vehicle_results"] = {
                "新坡91": {"status": "consumables_saved", "detail": "old", "updated_at": "old"}
            }
            payload["site_statuses"]["disinfection"]["vehicle_results"] = {
                "新坡91": {"status": "disinfection_failed", "detail": "old", "updated_at": "old"}
            }
            store.save_payload("task-edit", payload)

            updated = store.update_task("task-edit", request)

            for site in updated["site_statuses"].values():
                self.assertNotIn("vehicle_results", site)

    def test_multi_vehicle_edit_preserves_unchanged_vehicle_checkpoint_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-multi-edit",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "driver": "甲", "mileage": "101", "return_time": "0900"},
                        {"vehicle": "新坡93", "driver": "乙", "mileage": "202", "return_time": "0910"},
                    ],
                }
            )
            payload = store.create(previous)
            payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_saved"
            payload["site_statuses"]["vehicle_mileage"]["vehicle_results"] = {
                "新坡92": {"status": "vehicle_mileage_saved", "detail": "first saved"},
                "新坡93": {"status": "vehicle_mileage_saved", "detail": "second saved"},
            }
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["vehicle_entries"][1]["mileage"] = "210"
            current = AmbulanceReturnRequest.from_dict(current_payload)

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"vehicle_mileage"},
                site_update_contexts={
                    "vehicle_mileage": {
                        "previous_task": previous.to_dict(),
                        "current_task": current.to_dict(),
                    }
                },
            )

            self.assertIn("vehicle_results", updated["site_statuses"]["vehicle_mileage"])
            results = updated["site_statuses"]["vehicle_mileage"]["vehicle_results"]
            self.assertEqual(list(results), ["新坡92"])
            self.assertEqual(results["新坡92"]["status"], "vehicle_mileage_saved")
            self.assertNotIn("新坡93", results)

    def test_multi_vehicle_duty_edit_preserves_only_unchanged_vehicle_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-multi-duty-edit",
                    "created_at": "2026-07-13T08:00:00",
                    "case_reason": "急病",
                    "work_note": "返隊完成。",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "driver": "甲", "patient_summary": "男一名"},
                        {"vehicle": "新坡93", "driver": "乙", "patient_summary": "女一名"},
                    ],
                }
            )
            payload = store.create(previous)
            duty_site = payload["site_statuses"]["duty_work_log"]
            duty_site["status"] = "duty_work_log_saved"
            duty_site["vehicle_results"] = {
                "新坡92": {"status": "duty_work_log_saved", "detail": "first saved"},
                "新坡93": {"status": "duty_work_log_saved", "detail": "second saved"},
            }
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["vehicle_entries"][1]["patient_summary"] = "女二名"
            current = AmbulanceReturnRequest.from_dict(current_payload)
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"duty_work_log"},
                site_update_contexts={"duty_work_log": context},
            )

            updated_site = updated["site_statuses"]["duty_work_log"]
            self.assertEqual(updated_site["status"], "duty_work_log_needs_update")
            self.assertEqual(list(updated_site["vehicle_results"]), ["新坡92"])
            self.assertEqual(updated_site["update_context"], context)

    def test_enabling_previously_unselected_second_vehicle_fuel_marks_site_for_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-enable-second-fuel",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "fuel_record": {"enabled": False}},
                        {"vehicle": "新坡93", "fuel_record": {"enabled": False}},
                    ],
                }
            )
            store.create(previous)
            current_payload = previous.to_dict()
            current_payload["vehicle_entries"][1]["fuel_record"] = {
                "enabled": True,
                "date": "20260713",
                "time": "0915",
                "driver": "乙",
                "quantity": "40",
                "unit_price": "30",
            }
            current = AmbulanceReturnRequest.from_dict(current_payload)
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"fuel_record"},
                site_update_contexts={"fuel_record": context},
            )

            fuel_site = updated["site_statuses"]["fuel_record"]
            self.assertEqual(fuel_site["status"], "fuel_record_needs_update")
            self.assertEqual(fuel_site["update_context"], context)

    def test_disabling_saved_fuel_requires_visible_manual_record_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-disable-fuel",
                    "created_at": "2026-07-13T08:00:00",
                    "vehicle": "新坡92",
                    "fuel_record": {
                        "enabled": True,
                        "date": "20260713",
                        "time": "0915",
                        "driver": "甲",
                        "quantity": "40",
                        "unit_price": "30",
                    },
                }
            )
            payload = store.create(previous)
            payload["site_statuses"]["fuel_record"]["status"] = "fuel_record_saved"
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["fuel_record"]["enabled"] = False
            current = AmbulanceReturnRequest.from_dict(current_payload)
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"fuel_record"},
                site_update_contexts={"fuel_record": context},
            )

            fuel_site = updated["site_statuses"]["fuel_record"]
            self.assertEqual(fuel_site["status"], "fuel_record_waiting_confirmation")
            self.assertIn("人工刪除", fuel_site["detail"])
            self.assertEqual(fuel_site["update_context"], context)
            self.assertFalse(store._is_fully_done(updated))

    def test_removing_second_vehicle_requires_manual_cleanup_for_existing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-remove-second-vehicle",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "fuel_record": {"enabled": False}},
                        {
                            "vehicle": "新坡93",
                            "fuel_record": {
                                "enabled": True,
                                "date": "20260713",
                                "time": "0915",
                                "quantity": "40",
                                "unit_price": "30",
                            },
                        },
                    ],
                }
            )
            payload = store.create(previous)
            for site_key in ("duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"):
                site = payload["site_statuses"][site_key]
                site["status"] = f"{site_key}_saved"
                site["vehicle_results"] = {
                    "新坡92": {"status": f"{site_key}_saved", "detail": "first"},
                    "新坡93": {"status": f"{site_key}_saved", "detail": "second"},
                }
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["two_vehicle"] = False
            current_payload["vehicle"] = "新坡92"
            current_payload["fuel_record"] = {"enabled": False}
            current_payload["vehicle_entries"] = []
            current = AmbulanceReturnRequest.from_dict(current_payload)
            changed_sites = {"duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"}
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys=changed_sites,
                site_update_contexts={site_key: context for site_key in changed_sites},
            )

            for site_key in changed_sites:
                site = updated["site_statuses"][site_key]
                self.assertEqual(site["status"], f"{site_key}_waiting_confirmation")
                self.assertIn("新坡93", site["detail"])
                self.assertIn("人工刪除", site["detail"])
                self.assertIn("所有現行車輛", site["detail"])
                self.assertNotIn("vehicle_results", site)

    def test_replacing_second_vehicle_requires_delete_old_and_manually_create_current_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-replace-second-vehicle",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [{"vehicle": "新坡92"}, {"vehicle": "新坡93"}],
                }
            )
            payload = store.create(previous)
            site = payload["site_statuses"]["consumables"]
            site["status"] = "consumables_saved"
            site["vehicle_results"] = {
                "新坡92": {"status": "consumables_saved"},
                "新坡93": {"status": "consumables_saved"},
            }
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["vehicle_entries"][1]["vehicle"] = "新坡94"
            current = AmbulanceReturnRequest.from_dict(current_payload)
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"consumables"},
                site_update_contexts={"consumables": context},
            )

            updated_site = updated["site_statuses"]["consumables"]
            self.assertEqual(updated_site["status"], "consumables_waiting_confirmation")
            self.assertIn("新坡93", updated_site["detail"])
            self.assertIn("所有現行車輛", updated_site["detail"])

    def test_failed_partial_vehicle_site_keeps_update_context_after_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-partial-edit",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "driver": "甲", "mileage": "101", "return_time": "0900"},
                        {"vehicle": "新坡93", "driver": "乙", "mileage": "202", "return_time": "0910"},
                    ],
                }
            )
            payload = store.create(previous)
            site = payload["site_statuses"]["vehicle_mileage"]
            site["status"] = "vehicle_mileage_failed"
            site["vehicle_results"] = {
                "新坡92": {"status": "vehicle_mileage_saved", "detail": "first saved"},
                "新坡93": {"status": "vehicle_mileage_failed", "detail": "second failed"},
            }
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["vehicle_entries"][1]["mileage"] = "210"
            current = AmbulanceReturnRequest.from_dict(current_payload)
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"vehicle_mileage"},
                site_update_contexts={"vehicle_mileage": context},
            )

            updated_site = updated["site_statuses"]["vehicle_mileage"]
            self.assertEqual(updated_site["update_context"], context)
            self.assertEqual(updated_site["status"], "vehicle_mileage_needs_update")
            self.assertEqual(list(updated_site["vehicle_results"]), ["新坡92"])

    def test_vehicle_waiting_confirmation_takes_priority_over_other_vehicle_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-wait-and-fail",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92"},
                        {"vehicle": "新坡93"},
                    ],
                }
            )
            store.create(request)
            store.update_site_result(
                request.task_id,
                SiteAutomationResult(
                    "vehicle_mileage",
                    "車輛里程",
                    "vehicle_mileage_waiting_confirmation",
                    "92 wait",
                ),
                vehicle_key="新坡92",
            )
            updated = store.update_site_result(
                request.task_id,
                SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_failed", "93 failed"),
                vehicle_key="新坡93",
            )

            self.assertEqual(
                updated["site_statuses"]["vehicle_mileage"]["status"],
                "vehicle_mileage_waiting_confirmation",
            )

            confirmed = store.mark_site_completed(request.task_id, "vehicle_mileage", vehicle_key="新坡92")
            site = confirmed["site_statuses"]["vehicle_mileage"]
            self.assertEqual(site["vehicle_results"]["新坡92"]["status"], "completed_by_user")
            self.assertEqual(site["vehicle_results"]["新坡93"]["status"], "vehicle_mileage_failed")
            self.assertEqual(site["status"], "vehicle_mileage_failed")

    def test_whole_site_confirmation_cannot_hide_failed_or_missing_vehicle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-no-whole-confirm",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [{"vehicle": "新坡92"}, {"vehicle": "新坡93"}],
                }
            )
            payload = store.create(request)
            site = payload["site_statuses"]["vehicle_mileage"]
            site["status"] = "vehicle_mileage_waiting_confirmation"
            site["vehicle_results"] = {
                "新坡92": {"status": "vehicle_mileage_waiting_confirmation", "detail": "wait"},
                "新坡93": {"status": "vehicle_mileage_failed", "detail": "failed"},
            }
            store.save_payload(request.task_id, payload)

            with self.assertRaises(SiteCompletionConflictError):
                store.mark_site_completed(request.task_id, "vehicle_mileage")

            unchanged = store.get(request.task_id)["site_statuses"]["vehicle_mileage"]
            self.assertEqual(unchanged["vehicle_results"]["新坡93"]["status"], "vehicle_mileage_failed")

    def test_fuel_vehicle_aggregate_waits_only_for_enabled_vehicle_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-fuel-enabled-vehicles",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {
                            "vehicle": "新坡92",
                            "fuel_record": {"enabled": True, "date": "20260713", "time": "0915"},
                        },
                        {
                            "vehicle": "新坡93",
                            "fuel_record": {"enabled": False},
                        },
                    ],
                }
            )
            store.create(request)

            updated = store.update_site_result(
                request.task_id,
                SiteAutomationResult("fuel_record", "登打加油紀錄", "fuel_record_saved", "92 saved"),
                vehicle_key="新坡92",
            )

            site = updated["site_statuses"]["fuel_record"]
            self.assertEqual(site["status"], "fuel_record_saved")
            self.assertNotIn("新坡93: 等待回報", site["detail"])

    def test_cleanup_removes_fully_done_tasks_after_seven_day_history_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="done-expired-task", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site in payload["site_statuses"].values():
                site["status"] = "completed_by_user"
            payload["updated_at"] = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
            store.path_for("done-expired-task").write_text(__import__("json").dumps(payload), encoding="utf-8")

            self.assertEqual(store.list_recent(), [])
            self.assertFalse((Path(tmp) / "done-expired-task.json").exists())

    def test_cleanup_keeps_expired_completed_task_with_pending_reconciliation_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="done-expired-report-pending",
                created_at=datetime.now(),
                raw_text="",
            )
            payload = store.create(request)
            for site in payload["site_statuses"].values():
                site["status"] = "completed_by_user"
            payload["legacy_silent_save_report"] = {
                "event_id": "stable-pending-event",
                "pending": True,
                "created_at": (datetime.now() - timedelta(days=15)).isoformat(timespec="seconds"),
                "enqueued_at": "",
            }
            payload["updated_at"] = (datetime.now() - timedelta(days=15)).isoformat(timespec="seconds")
            store.path_for(request.task_id).write_text(json.dumps(payload), encoding="utf-8")

            recent = store.list_recent()

            self.assertEqual([item["task"]["task_id"] for item in recent], [request.task_id])
            self.assertTrue(store.path_for(request.task_id).exists())


if __name__ == "__main__":
    unittest.main()
