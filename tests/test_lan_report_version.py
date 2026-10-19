import copy
from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

import app as app_module
import worker as worker_module
from ambulance_bot.status_outbox import WorkerStatusOutbox
from tests import test_lan_parity as fixtures


class LanReportVersionTests(unittest.TestCase):
    setUp = fixtures.LanFailureEvidenceTests.setUp
    original = fixtures.LanFailureEvidenceTests.original
    entry = fixtures.LanFailureEvidenceTests.entry
    claim_task = fixtures.LanFailureEvidenceTests.claim_task

    def send(self, version, event_id="version-1", **extra):
        return self.client.post(f"/worker/tasks/{self.task_id}/status", headers=self.headers, json={
            "status": "worker_running", "detail": "隔離測試", "worker_id": "evidence-worker",
            "claim_id": self.claim_id, "status_event_id": event_id, "package_version": version, **extra,
        }, **self.original)

    def test_worker_outbox_nas_task_events_and_admin_use_actual_report_version(self):
        self.claim_task()
        sent = []

        def transport(_server, task_id, body):
            sent.append(copy.deepcopy(body))
            response = self.client.post(f"/worker/tasks/{task_id}/status", headers=self.headers,
                                        json=body, **self.original)
            self.assertEqual(200, response.status_code)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARTIFACTS_DIR": tmp}), \
                mock.patch.object(worker_module, "current_package_version", return_value="2026.10.20.0150"), \
                mock.patch.object(worker_module, "_send_worker_status", side_effect=transport):
            worker_module.post_status("http://isolated.invalid", self.task_id, "worker_running", "隔離回報",
                                      worker_id="evidence-worker", claim_id=self.claim_id)
        self.assertEqual("2026.10.20.0150", sent[0].get("package_version"))
        payload = self.store.get(self.task_id)
        self.assertEqual("2026.10.20.0150", payload.get("package_version"))
        self.assertEqual("2026.10.20.0150", payload["events"][-1].get("package_version"))
        body = self.client.get("/admin/ems", **self.original).get_data(as_text=True)
        self.assertIn("最後任務回報版本：2026.10.20.0150", body)

    def test_duplicate_and_stale_claim_cannot_replace_recorded_version(self):
        self.claim_task()
        self.assertEqual(200, self.send("2026.10.20.0150").status_code)
        replay = self.send("wrong-version")
        self.assertTrue(replay.json["duplicate"])
        before = (self.store.tasks_dir / f"{self.task_id}.json").read_bytes()
        stale = self.send("wrong-version", event_id="version-stale", claim_id="stale-claim")
        self.assertEqual(409, stale.status_code)
        self.assertEqual(before, (self.store.tasks_dir / f"{self.task_id}.json").read_bytes())
        self.assertEqual("2026.10.20.0150", self.store.get(self.task_id).get("package_version"))

    def test_legacy_missing_or_invalid_version_does_not_lose_status_or_existing_version(self):
        self.claim_task()
        self.send("2026.10.20.0150")
        for index, value in enumerate((None, "", {"version": "bad"}, "bad\nversion", "v" * 129)):
            with self.subTest(value=value):
                response = self.send(value, event_id=f"legacy-{index}")
                self.assertEqual(200, response.status_code)
                self.assertEqual("2026.10.20.0150", response.json["payload"].get("package_version"))

    def test_latest_unknown_lan_task_does_not_reuse_older_local_task_version(self):
        self.claim_task()
        old = (datetime.now() - timedelta(hours=2)).isoformat()
        app_module.write_json_atomic(app_module.public_pc_report_file(), {"tasks": [{
            "task_id": "old-local-version", "task": {"task_id": "old-local-version", "service_type": "ems"},
            "title": "隔離舊本機紀錄", "package_version": "2026.10.19.1548", "updated_at": old,
        }]})
        reports = app_module.admin_task_reports()
        view = app_module.worker_heartbeat_admin_view(reports)
        self.assertEqual("", view["last_task_report_version"])
        self.assertGreater(view["last_task_report_at"], old)
        body = self.client.get("/admin/ems", **self.original).get_data(as_text=True)
        self.assertIn("最後任務回報版本：未回報", body)
        self.assertNotIn("最後任務回報版本：2026.10.19.1548", body)

    def test_legacy_report_retry_uses_new_worker_version_and_keeps_old_event(self):
        self.claim_task()
        payload = self.store.get(self.task_id)
        payload["task_source"] = app_module.PUBLIC_PC_REPORT_RETRY_SOURCE
        self.store.save_payload(self.task_id, payload)
        app_module.upsert_public_pc_report({"event_id": "original-version", "task_id": self.task_id,
            "task": payload["task"], "title": "隔離本機案件重試", "operator": "隔離原操作者",
            "package_version": "2026.10.19.1548", "status": "created"})
        self.assertEqual(200, self.send("2026.10.20.0150").status_code)
        report = app_module.public_pc_report_for_task(self.task_id)
        self.assertEqual("2026.10.20.0150", report["package_version"])
        self.assertEqual("隔離原操作者", report["operator"])
        self.assertTrue(any(e.get("package_version") == "2026.10.19.1548" for e in report["events"]))


class LanVersionOutboxTests(unittest.TestCase):
    def test_deferred_status_keeps_version_from_enqueue_after_worker_update(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARTIFACTS_DIR": tmp,
                "WORKER_STATUS_POST_RETRIES": "1", "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "0"}), \
                mock.patch.object(worker_module, "current_package_version", return_value="2026.10.19.2340"), \
                mock.patch.object(worker_module, "_send_worker_status", side_effect=urllib.error.URLError("offline")):
            worker_module.post_status("http://isolated.invalid", "old-outbox-task", "worker_running", "隔離離線回報")
            outbox = WorkerStatusOutbox(Path(tmp) / "worker_status_outbox")
            pending = outbox.pending()
            body = pending[0]["payload"]["body"]
            self.assertEqual("2026.10.19.2340", body.get("package_version"))
            with mock.patch.object(worker_module, "current_package_version", return_value="2026.10.20.0150"):
                self.assertEqual("2026.10.19.2340", outbox.pending()[0]["payload"]["body"].get("package_version"))


if __name__ == "__main__":
    unittest.main()
