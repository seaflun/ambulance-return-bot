import tempfile
import unittest
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
            self.assertTrue((Path(tmp) / "task-1.json").exists())

            store.update_site_result(
                "task-1",
                SiteAutomationResult("vehicle_mileage", "車輛里程", "prefill_ready", "ready"),
            )
            updated = store.get("task-1")
            self.assertEqual(updated["site_statuses"]["vehicle_mileage"]["status"], "prefill_ready")

            store.mark_site_completed("task-1", "vehicle_mileage")
            completed = store.get("task-1")
            self.assertEqual(completed["site_statuses"]["vehicle_mileage"]["status"], "completed_by_user")

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


if __name__ == "__main__":
    unittest.main()
