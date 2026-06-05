import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
