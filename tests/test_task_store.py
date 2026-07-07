import tempfile
import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path

from ambulance_bot.adapters import SiteAutomationResult
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_store import JsonTaskStore


class JsonTaskStoreTests(unittest.TestCase):
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

            store.mark_site_completed("task-1", "vehicle_mileage")
            completed = store.get("task-1")
            self.assertEqual(completed["site_statuses"]["vehicle_mileage"]["status"], "completed_by_user")
            self.assertEqual(completed["site_attempts"]["vehicle_mileage"][-1]["status"], "completed_by_user")

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

    def test_cleanup_removes_old_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="old-task", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            payload["updated_at"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
            store.path_for("old-task").write_text(__import__("json").dumps(payload), encoding="utf-8")

            self.assertEqual(store.list_recent(), [])
            self.assertFalse((Path(tmp) / "old-task.json").exists())

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
            payload["updated_at"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
            store.path_for("done-history-task").write_text(__import__("json").dumps(payload), encoding="utf-8")

            self.assertEqual(len(store.list_recent()), 1)
            self.assertTrue((Path(tmp) / "done-history-task.json").exists())

    def test_cleanup_removes_fully_done_tasks_after_history_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="done-expired-task", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site in payload["site_statuses"].values():
                site["status"] = "completed_by_user"
            payload["updated_at"] = (datetime.now() - timedelta(days=15)).isoformat(timespec="seconds")
            store.path_for("done-expired-task").write_text(__import__("json").dumps(payload), encoding="utf-8")

            self.assertEqual(store.list_recent(), [])
            self.assertFalse((Path(tmp) / "done-expired-task.json").exists())


if __name__ == "__main__":
    unittest.main()
