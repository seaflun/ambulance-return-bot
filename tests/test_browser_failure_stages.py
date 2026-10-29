import tempfile
from copy import deepcopy
import unittest
from datetime import datetime
from pathlib import Path

import app as app_module
from ambulance_bot.adapters import SiteAutomationResult
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.site_diagnostics import browser_failure_stage_from_events, merge_diagnostic_fields
from ambulance_bot.task_store import JsonTaskStore


class BrowserFailureStageTests(unittest.TestCase):
    def test_malformed_legacy_events_do_not_break_failure_reporting(self):
        self.assertEqual("階段未判定", browser_failure_stage_from_events(
            "vehicle_mileage", "vehicle_mileage_failed", "[browser_failure:network_connection]", [None, "old"]))

    def test_historical_failures_use_their_own_round_without_changing_stored_events(self):
        events = [
            {"time": "2026-10-29T18:00:00", "status": "queued_for_worker"},
            {"time": "2026-10-29T18:00:01", "status": "vehicle_mileage_running",
             "detail": "公務電腦 worker 車輛里程：登入 PPE"},
            {"time": "2026-10-29T18:00:02", "status": "vehicle_mileage_failed",
             "detail": "網路中斷 [browser_failure:network_connection]", "failure_stage": "開啟車輛里程"},
            {"time": "2026-10-29T18:01:00", "status": "queued_for_worker"},
            {"time": "2026-10-29T18:01:01", "status": "vehicle_mileage_running",
             "detail": "公務電腦 worker 車輛里程：儲存"},
            {"time": "2026-10-29T18:01:02", "status": "vehicle_mileage_failed",
             "detail": "網路中斷 [browser_failure:network_connection]", "failure_stage": "開啟車輛里程"},
            {"time": "2026-10-29T18:02:00", "status": "queued_for_worker"},
            {"time": "2026-10-29T18:02:01", "status": "vehicle_mileage_failed",
             "detail": "網路中斷 [browser_failure:network_connection]", "failure_stage": "開啟車輛里程"},
        ]
        events[2]["failure_sites"] = {"vehicle_mileage": {
            "key": "vehicle_mileage", "status": "vehicle_mileage_failed", "detail": events[2]["detail"],
            "failure_stage": "開啟車輛里程",
        }}
        report = {"events": list(reversed(events))}
        original = deepcopy(report)
        projected = app_module.public_pc_report_display_events(report, chronological=True)
        failures = [event for event in projected if event.get("status") == "vehicle_mileage_failed"]
        self.assertEqual(["登入 PPE", "儲存", "階段未判定"], [event["failure_stage"] for event in failures])
        self.assertEqual("登入 PPE", failures[0]["failure_sites"]["vehicle_mileage"]["failure_stage"])
        report["site_statuses"] = {"vehicle_mileage": {
            "key": "vehicle_mileage", "status": "vehicle_mileage_failed", "detail": events[-1]["detail"],
            "failure_stage": "開啟車輛里程",
        }}
        view = app_module.task_failure_stage_view(report)
        self.assertEqual("階段未判定", view["site_statuses"]["vehicle_mileage"]["failure_stage"])
        self.assertEqual("開啟車輛里程", report["site_statuses"]["vehicle_mileage"]["failure_stage"])
        self.assertEqual(original["events"], report["events"])

    def test_a_new_round_does_not_borrow_stale_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonTaskStore(Path(directory))
            request = AmbulanceReturnRequest.from_dict({"task_id": "round-boundary", "vehicle": "新坡92"})
            store.create(request)
            store.update_site_result(request.task_id, SiteAutomationResult(
                "vehicle_mileage", "車輛里程", "vehicle_mileage_running", "公務電腦 worker 車輛里程：儲存"))
            store.queue_for_worker(request.task_id)
            payload = store.update_site_result(request.task_id, SiteAutomationResult(
                "vehicle_mileage", "車輛里程", "vehicle_mileage_failed", "[browser_failure:network_connection]"))
            self.assertEqual("階段未判定", payload["site_statuses"]["vehicle_mileage"]["failure_stage"])

    def test_a_vehicle_failure_uses_only_that_vehicles_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonTaskStore(Path(directory))
            request = AmbulanceReturnRequest.from_dict({
                "task_id": "vehicle-boundary", "service_type": "disaster",
                "vehicle_entries": [{"vehicle": "新坡11"}, {"vehicle": "新坡15"}],
            })
            store.create(request)
            for vehicle, stage in [("新坡11", "登入 PPE"), ("新坡15", "儲存")]:
                store.update_site_result(request.task_id, SiteAutomationResult(
                    "vehicle_mileage", "車輛里程", "vehicle_mileage_running", f"公務電腦 worker 車輛里程：{stage}"),
                    vehicle_key=vehicle)
            payload = store.update_site_result(request.task_id, SiteAutomationResult(
                "vehicle_mileage", "車輛里程", "vehicle_mileage_failed", "[browser_failure:network_connection]"),
                vehicle_key="新坡11")
            self.assertEqual("登入 PPE", payload["site_statuses"]["vehicle_mileage"]["vehicle_results"]["新坡11"]["failure_stage"])

    def test_failure_retains_the_actual_last_progress_in_the_result_history_and_ui(self):
        for key, name, stage in [
            ("vehicle_mileage", "車輛里程", "登入 PPE"),
            ("disinfection", "緊急救護消毒", "登入消毒系統"),
            ("consumables", "一站通耗材", "儲存"),
        ]:
            with self.subTest(site=key), tempfile.TemporaryDirectory() as directory:
                store = JsonTaskStore(Path(directory))
                request = AmbulanceReturnRequest.from_dict({
                    "task_id": "network-stage", "created_at": datetime.now().isoformat(),
                    "service_type": "ems", "vehicle": "新坡92",
                })
                store.create(request)
                store.update_site_result(request.task_id, SiteAutomationResult(
                    key, name, f"{key}_running", f"公務電腦 worker {name}：{stage}"))
                payload = store.update_site_result(request.task_id, SiteAutomationResult(
                    key, name, f"{key}_failed", "網站連線中斷 [browser_failure:network_connection]"))
                site = payload["site_statuses"][key]
                self.assertEqual(stage, site["failure_stage"])
                self.assertEqual(stage, payload["site_attempts"][key][-1]["failure_stage"])
                self.assertEqual(stage, payload["events"][-1]["failure_stage"])
                rows = app_module.site_stage_rows({key: site}, key)
                self.assertEqual([stage], [row["name"] for row in rows if row["class"] == "failed"])

    def test_display_preserves_a_confirmed_stage_while_refreshing_the_network_reason(self):
        site = {"key": "vehicle_mileage", "status": "vehicle_mileage_failed",
                "detail": "網路中斷 [browser_failure:network_connection]", "failure_stage": "登入 PPE",
                "failure_reason": "舊原因", "exception_type": "RuntimeError"}
        diagnosis = merge_diagnostic_fields(site)
        self.assertEqual("登入 PPE", diagnosis["failure_stage"])
        self.assertEqual("network_connection", diagnosis["exception_type"])
        self.assertNotEqual("舊原因", diagnosis["failure_reason"])

    def test_missing_progress_does_not_mark_chrome_or_login_as_the_failure_or_passed(self):
        key = "consumables"
        site = {"key": key, "status": "consumables_failed",
                "detail": "網路中斷 [browser_failure:network_connection]"}
        self.assertEqual("階段未判定", merge_diagnostic_fields(site)["failure_stage"])
        rows = app_module.site_stage_rows({key: site}, key)
        self.assertFalse(any(row["class"] == "complete" for row in rows))
        self.assertEqual(["階段未判定"], [row["name"] for row in rows if row["class"] == "failed"])
        active = app_module.site_active_stage_summary({key: site}, key)
        self.assertEqual("階段未判定", active["name"])
