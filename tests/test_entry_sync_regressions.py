import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
import worker as worker_module
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.record_folders import RecordFolderError, confirmed_record_folders, ensure_ems_record_folders
from ambulance_bot.task_store import JsonTaskStore
from tests import test_web_app as web_fixtures


class SharedMileageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = web_fixtures.WebAppTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def report(self, task_id, time, mileage):
        task = AmbulanceReturnRequest(task_id=task_id, created_at=datetime(2026, 10, 19, 19),
                                      raw_text="", vehicle="新坡95", case_date="2026/10/19",
                                      case_time=time, mileage=mileage).to_dict()
        return {"task_id": task_id, "task": task, "created_at": "2026-10-19T19:00:00",
                "site_statuses": {"vehicle_mileage": {"status": "vehicle_mileage_saved"}}}

    def write_reports(self, reports):
        app_module.write_json_atomic(app_module.public_pc_report_file(), {"tasks": reports})

    def test_first_nas_entry_includes_today_local_reports_and_preserves_earlier_case_reference(self):
        self.write_reports([self.report("later", "1758", "4886"), self.report("early", "0748", "4800")])
        path = app_module.public_pc_report_file()
        before = path.read_bytes()
        self.assertEqual("4886", app_module.last_vehicle_mileages()["新坡95"])
        self.assertEqual("4800", app_module.last_vehicle_mileages(before=datetime(2026, 10, 19, 12))["新坡95"])
        with app_module.app.test_request_context():
            history = app_module.vehicle_mileage_hint_history()["新坡95"]
        self.assertIn({"time": "202610190748", "mileage": "4800"}, history)
        self.assertEqual(before, path.read_bytes(), "Mileage reads must not prune or rewrite reports")

    def test_live_endpoint_observes_other_browser_new_case_without_exposing_case_data(self):
        for port in ("8080", "8091"):
            isolated_store = JsonTaskStore(Path(self.fixture.tmp.name) / port)
            with self.subTest(port=port), mock.patch.dict(os.environ, {"TASK_ENTRY_PORT": "8091"}), mock.patch.object(
                app_module, "store", isolated_store
            ):
                options = {"headers": {"Host": "nas.test:" + port}, "environ_overrides": {"SERVER_PORT": port}}
                self.write_reports([self.report("local", "1758", "4886")])
                first = self.fixture.client.get("/api/vehicle-mileages", **options)
                self.assertEqual(200, first.status_code)
                self.assertIn("no-store", first.headers["Cache-Control"])
                self.assertEqual("4886", first.json["mileages"]["新坡95"])
                isolated_store.create(AmbulanceReturnRequest.from_dict(self.report("nas", "2127", "4919")["task"]))
                isolated_store.update_site_result("nas", app_module.SiteAutomationResult(
                    "vehicle_mileage", "里程", "vehicle_mileage_saved", "里程已回查確認"))
                current = self.fixture.client.get("/api/vehicle-mileages", **options)
                self.assertEqual("4919", current.json["mileages"]["新坡95"])
                self.assertEqual("202610192127", current.json["times"]["新坡95"])
                self.assertEqual({"mileages", "times", "history"}, set(current.json))
                self.assertNotIn("task_id", json.dumps(current.json))

    def test_current_nas_task_wins_over_stale_report_with_same_task_id(self):
        self.write_reports([self.report("same", "1758", "4886")])
        self.fixture.store.create(AmbulanceReturnRequest.from_dict(self.report("same", "1758", "4890")["task"]))
        self.fixture.store.update_site_result("same", app_module.SiteAutomationResult(
            "vehicle_mileage", "里程", "vehicle_mileage_saved", "里程已回查確認"))
        self.assertEqual("4890", app_module.last_vehicle_mileages()["新坡95"])

    def test_mileage_polling_never_cleans_up_or_quarantines_task_files(self):
        path = self.fixture.store.tasks_dir / "isolated-malformed.json"
        path.write_text("{", encoding="utf-8")
        with mock.patch.object(self.fixture.store, "cleanup") as cleanup, mock.patch.object(
            self.fixture.store, "_read_payload_or_quarantine", side_effect=AssertionError("polling must only read")
        ) as quarantine:
            response = self.fixture.client.get("/api/vehicle-mileages")
        self.assertEqual(200, response.status_code)
        cleanup.assert_not_called()
        quarantine.assert_not_called()
        self.assertEqual("{", path.read_text(encoding="utf-8"))


class QueuedRecordFolderTests(unittest.TestCase):
    def run_queue(self, folder_root, *, service="ems", folder_error=None, two_vehicles=False):
        store = JsonTaskStore(folder_root / "tasks")
        request = AmbulanceReturnRequest(task_id="isolated-queued-folders", created_at=datetime(2026, 10, 19),
                                         raw_text="", service_type=service, case_date="2026/10/19",
                                         case_time="2127", vehicle="新坡95", driver="甲", mileage="4919")
        if two_vehicles:
            request = AmbulanceReturnRequest.from_dict({**request.to_dict(), "two_vehicle": True, "vehicle_entries": [
                {"vehicle": "新坡95", "driver": "甲", "mileage": "4919"},
                {"vehicle": "新坡92", "driver": "乙", "mileage": "26020"}]})
        store.create(request)
        calls = []

        def run_site(site_key, status):
            def run(*args, **kwargs):
                calls.append(site_key)
                return SimpleNamespace(ok=True, status=status, detail="isolated result")
            return run

        def post(_server, task_id, status, detail, **kwargs):
            store.set_overall_status(task_id, status, detail)

        create = lambda task: ensure_ems_record_folders(task, folder_root / "records")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(worker_module, "fetch_task_payload", side_effect=lambda *args: store.get(request.task_id)))
            stack.enter_context(mock.patch.object(worker_module, "post_status", side_effect=post))
            stack.enter_context(mock.patch.object(worker_module, "maximize_worker_site_windows"))
            stack.enter_context(mock.patch.object(worker_module, "print_worker_completion_if_reached"))
            folders = stack.enter_context(mock.patch.object(worker_module, "ensure_ems_record_folders", create=True,
                                                           side_effect=folder_error or create))
            for name, site_key, status in [
                ("run_task", "duty_work_log", "duty_work_log_saved"),
                ("run_vehicle_task", "vehicle_mileage", "vehicle_mileage_saved"),
                ("run_consumables_worker_task", "consumables", "consumables_saved"),
                ("run_disinfection_worker_task", "disinfection", "disinfection_saved"),
            ]:
                stack.enter_context(mock.patch.object(worker_module, name, side_effect=run_site(site_key, status)))
            result = worker_module._run_all_sites_task_impl("http://isolated", "isolated-worker", request.to_dict(),
                                                           folder_root, cancellation_event=threading.Event())
        return result, calls, store.get(request.task_id), folders.call_count

    def test_queued_ems_creates_and_reports_folders_and_reuses_existing_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for attempt in range(2):
                result, calls, payload, count = self.run_queue(root)
                self.assertEqual(1, count)
                self.assertEqual(4, len(calls))
                self.assertIsNotNone(result)
                self.assertEqual([str(root / "records/2026/10月/10192127-95").replace("\\", "/")],
                                 confirmed_record_folders(payload))
                self.assertTrue(all((root / "records/2026/10月/10192127-95" / child).is_dir() for child in ("1", "2", "車")))

    def test_folder_failure_stops_before_official_sites_and_never_reports_folder_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls, payload, count = self.run_queue(Path(tmp), folder_error=PermissionError("isolated share unavailable"))
        self.assertEqual(1, count)
        self.assertEqual([], calls)
        self.assertIn("資料夾", result.detail)
        self.assertEqual([], confirmed_record_folders(payload))
        self.assertNotEqual("desktop_fast_completed", payload["overall_status"])

    def test_two_vehicle_queue_creates_both_original_folder_names_and_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, calls, _, count = self.run_queue(root, two_vehicles=True)
            self.assertEqual(1, count)
            self.assertEqual(4, len(calls))
            for vehicle in ("95", "92"):
                self.assertTrue(all((root / f"records/2026/10月/10192127-{vehicle}" / child).is_dir()
                                    for child in ("1", "2", "車")))

    def test_folder_creation_is_not_success_until_each_child_directory_is_verified(self):
        request = AmbulanceReturnRequest(task_id="isolated-verify", created_at=datetime(2026, 10, 19), raw_text="",
                                         case_date="2026/10/19", case_time="2127", vehicle="新坡95")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Path, "mkdir"):
            with self.assertRaises(RecordFolderError):
                ensure_ems_record_folders(request, Path(tmp) / "never-created")

    def test_disaster_folders_remain_owned_by_nas(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, count = self.run_queue(Path(tmp), service="disaster")
        self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
