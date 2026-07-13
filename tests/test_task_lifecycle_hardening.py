import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from ambulance_bot.adapters import SiteAutomationResult
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_store import (
    JsonTaskStore,
    TaskActiveError,
    WorkerClaimConflictError,
)


class TaskLifecycleHardeningTests(unittest.TestCase):
    @staticmethod
    def _request(task_id: str) -> AmbulanceReturnRequest:
        return AmbulanceReturnRequest(
            task_id=task_id,
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡91",
        )

    @staticmethod
    def _age_payload(store: JsonTaskStore, task_id: str, *, hours: int = 25) -> None:
        path = store.path_for(task_id)
        payload = store.get(task_id)
        payload["updated_at"] = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _complete_active_sites(store: JsonTaskStore, task_id: str) -> None:
        payload = store.get(task_id)
        for site_key in ("duty_work_log", "vehicle_mileage", "consumables", "disinfection"):
            payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"
        payload["overall_status"] = "desktop_fast_completed"
        store.save_payload(task_id, payload)

    def test_cleanup_never_deletes_unfinished_tasks_after_one_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            for task_id, status in (
                ("queued-old", "queued_for_worker"),
                ("waiting-old", "manual_confirmation_required"),
                ("failed-old", "desktop_fast_completed_with_errors"),
                ("needs-update-old", "task_updated_needs_site_update"),
            ):
                store.create(self._request(task_id))
                payload = store.get(task_id)
                payload["overall_status"] = status
                if task_id == "queued-old":
                    payload["worker_queue"]["status"] = "queued"
                elif task_id == "waiting-old":
                    payload["site_statuses"]["consumables"]["status"] = "consumables_waiting_confirmation"
                elif task_id == "failed-old":
                    payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_failed"
                else:
                    payload["site_statuses"]["duty_work_log"]["status"] = "duty_work_log_needs_update"
                store.save_payload(task_id, payload)
                self._age_payload(store, task_id)

            store.cleanup()

            self.assertEqual(
                {path.stem for path in Path(tmp).glob("*.json")},
                {"queued-old", "waiting-old", "failed-old", "needs-update-old"},
            )

    def test_worker_can_claim_queued_task_after_public_pc_was_offline_over_one_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            store.create(self._request("queued-after-outage"))
            store.queue_for_worker("queued-after-outage")
            self._age_payload(store, "queued-after-outage")

            claimed = store.claim_next_for_worker("PC-01")

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["task"]["task_id"], "queued-after-outage")

    def test_delete_rejects_task_with_active_worker_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            store.create(self._request("active-delete"))
            store.queue_for_worker("active-delete")
            self.assertIsNotNone(store.claim_next_for_worker("PC-01"))

            with self.assertRaises(TaskActiveError):
                store.delete("active-delete")

            self.assertTrue(store.path_for("active-delete").exists())

    def test_fully_completed_task_cannot_be_queued_again_but_failed_task_can(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            store.create(self._request("already-complete"))
            self._complete_active_sites(store, "already-complete")

            with self.assertRaises(WorkerClaimConflictError) as caught:
                store.queue_for_worker("already-complete")
            self.assertEqual(caught.exception.code, "task_already_completed")

            store.create(self._request("retry-failed"))
            failed = store.get("retry-failed")
            failed["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_failed"
            failed["overall_status"] = "desktop_fast_completed_with_errors"
            store.save_payload("retry-failed", failed)

            queued = store.queue_for_worker("retry-failed")
            self.assertEqual(queued["worker_queue"]["status"], "queued")

    def test_requeue_cannot_clear_an_active_worker_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            store.create(self._request("active-requeue"))
            store.queue_for_worker("active-requeue")
            claimed = store.claim_next_for_worker("PC-01")
            assert claimed is not None
            claim_id = claimed["worker_queue"]["claim_id"]

            with self.assertRaises(WorkerClaimConflictError) as caught:
                store.queue_for_worker("active-requeue")

            self.assertEqual(caught.exception.code, "worker_claim_conflict")
            unchanged = store.get("active-requeue")["worker_queue"]
            self.assertEqual(unchanged["status"], "claimed")
            self.assertEqual(unchanged["claim_id"], claim_id)
            self.assertEqual(unchanged["worker_id"], "PC-01")

    def test_inactive_task_cannot_be_aborted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            store.create(self._request("inactive-abort"))

            with self.assertRaises(WorkerClaimConflictError) as caught:
                store.abort_running_task("inactive-abort")

            self.assertEqual(caught.exception.code, "task_not_active")
            self.assertEqual(store.get("inactive-abort")["overall_status"], "created")

    def test_manual_vehicle_completion_is_sticky_against_same_claim_late_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = self._request("sticky-manual-confirmation")
            store.create(request)
            store.queue_for_worker(request.task_id)
            claimed = store.claim_next_for_worker("PC-01")
            assert claimed is not None
            claim_id = claimed["worker_queue"]["claim_id"]
            identity = {"claim_id": claim_id, "worker_id": "PC-01"}
            store.apply_worker_status(
                request.task_id,
                result=SiteAutomationResult(
                    "vehicle_mileage",
                    "車輛里程",
                    "vehicle_mileage_waiting_confirmation",
                    "請人工確認",
                ),
                vehicle_key="新坡91",
                status_event_id="waiting-event",
                **identity,
            )
            store.mark_site_completed(request.task_id, "vehicle_mileage", vehicle_key="新坡91")

            store.apply_worker_status(
                request.task_id,
                result=SiteAutomationResult(
                    "vehicle_mileage",
                    "車輛里程",
                    "vehicle_mileage_failed",
                    "舊執行緒的延遲回報",
                ),
                vehicle_key="新坡91",
                status_event_id="late-event",
                **identity,
            )

            site = store.get(request.task_id)["site_statuses"]["vehicle_mileage"]
            self.assertEqual(site["vehicle_results"]["新坡91"]["status"], "completed_by_user")
            self.assertEqual(site["status"], "vehicle_mileage_saved")
            self.assertNotIn("舊執行緒的延遲回報", site["detail"])

    def test_confirming_one_vehicle_preserves_other_vehicle_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "partial-confirm",
                    "created_at": datetime.now().isoformat(),
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "driver": "甲", "mileage": "100"},
                        {"vehicle": "新坡93", "driver": "乙", "mileage": "200"},
                    ],
                }
            )
            store.create(request)
            payload = store.get(request.task_id)
            site = payload["site_statuses"]["vehicle_mileage"]
            site["status"] = "vehicle_mileage_waiting_confirmation"
            site["vehicle_results"] = {
                "新坡92": {
                    "status": "vehicle_mileage_waiting_confirmation",
                    "detail": "請人工確認92",
                    "failure_stage": "",
                    "failure_reason": "",
                    "next_action": "人工確認",
                    "exception_type": "",
                },
                "新坡93": {
                    "status": "vehicle_mileage_failed",
                    "detail": "93登入失敗",
                    "failure_stage": "登入",
                    "failure_reason": "驗證碼錯誤",
                    "next_action": "重新登入93",
                    "exception_type": "WebDriverException",
                },
            }
            store.save_payload(request.task_id, payload)

            updated = store.mark_site_completed(request.task_id, "vehicle_mileage", vehicle_key="新坡92")

            updated_site = updated["site_statuses"]["vehicle_mileage"]
            self.assertEqual(updated_site["status"], "vehicle_mileage_failed")
            self.assertEqual(updated_site["failure_stage"], "登入")
            self.assertEqual(updated_site["failure_reason"], "驗證碼錯誤")
            self.assertEqual(updated_site["next_action"], "重新登入93")
            self.assertEqual(updated_site["exception_type"], "WebDriverException")


if __name__ == "__main__":
    unittest.main()
