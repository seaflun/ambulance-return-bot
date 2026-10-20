import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import app as app_module
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_store import JsonTaskStore


class SiteTimingReportIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = JsonTaskStore(Path(self.tmp.name) / "tasks")
        for name, value in (("store", self.store), ("artifacts_dir", Path(self.tmp.name))):
            replacement = patch.object(app_module, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        environment = patch.dict(os.environ, {"PUBLIC_PC_REPORT_ENABLED": "false",
            "WORKER_SERVER_URL": "", "WORKER_TOKEN": "timing-test-token", "REMOTE_UPDATE_ADMIN_TOKEN": ""})
        environment.start()
        self.addCleanup(environment.stop)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.task = AmbulanceReturnRequest(
            task_id="site-timing-identity", created_at=datetime.now(), raw_text="",
            service_type="disaster", vehicle="新坡11", driver="任務司機", case_reason="火警",
            vehicle_entries=[{"vehicle": "新坡11", "driver": "甲"},
                             {"vehicle": "新坡15", "driver": "乙"}],
        )
        self.store.create(self.task)
        payload = self.store.get(self.task.task_id)
        payload["task_source"] = app_module.PUBLIC_PC_REPORT_RETRY_SOURCE
        self.store.save_payload(self.task.task_id, payload)
        app_module.upsert_public_pc_report({
            "task_id": self.task.task_id, "task": payload["task"],
            "site_statuses": payload["site_statuses"], "status": "created",
        })
        self.store.queue_for_worker(self.task.task_id, run_site_key="vehicle_mileage")
        claimed = self.store.claim_task_for_worker(self.task.task_id, "timing-worker")
        self.claim_id = claimed["worker_queue"]["claim_id"]

    def post_status(self, event_id, status, vehicle_key=""):
        body = {
            "status_event_id": event_id, "status": status, "detail": "測試回報",
            "site_key": "vehicle_mileage", "site_name": "車輛里程",
            "worker_id": "timing-worker", "claim_id": self.claim_id,
        }
        if vehicle_key:
            body.update(vehicle_key=vehicle_key, vehicle_label=vehicle_key)
        response = self.client.post(
            f"/worker/tasks/{self.task.task_id}/status",
            headers={"X-Worker-Token": "timing-test-token"}, json=body,
        )
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        report = app_module.public_pc_report_for_task(self.task.task_id)
        return next(event for event in report["events"]
                    if event["event_id"] == f"retry-status:{event_id}")

    def test_worker_vehicle_status_preserves_identity_in_retry_report_events(self):
        for index, (vehicle_key, state) in enumerate((
            ("新坡11", "running"), ("新坡11", "saved"),
            ("新坡15", "running"), ("新坡15", "failed"),
        )):
            with self.subTest(vehicle_key=vehicle_key, state=state):
                event = self.post_status(f"vehicle-{index}", f"vehicle_mileage_{state}", vehicle_key)
                self.assertEqual(vehicle_key, event.get("vehicle_key"))
                self.assertEqual(f"vehicle_mileage_{state}", event["status"])

    def test_whole_site_summary_does_not_inherit_the_previous_vehicle_identity(self):
        self.post_status("vehicle-result", "vehicle_mileage_saved", "新坡11")
        event = self.post_status("site-summary", "vehicle_mileage_saved")
        self.assertFalse(event.get("vehicle_key"))
        self.assertEqual("vehicle_mileage_saved", event["status"])


if __name__ == "__main__":
    unittest.main()
