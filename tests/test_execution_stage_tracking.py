import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import app
from ambulance_bot import selenium_local, site_diagnostics
from ambulance_bot.adapters import SiteAutomationResult
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_cancellation import TaskCancellationError
from ambulance_bot.task_store import JsonTaskStore


class ExecutionStageTrackingTests(unittest.TestCase):
    def test_runtime_stage_has_priority_over_old_stage_words_in_the_error_message(self):
        @site_diagnostics.track_site_execution("vehicle_mileage")
        def run(progress=None):
            return SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_failed",
                                        "公務電腦 worker 車輛里程：登入 PPE 設定錯誤")

        result = run()
        rows = app.site_stage_rows({"vehicle_mileage": {
            "key": result.key, "status": result.status, "detail": result.detail,
            "failure_stage": result.failure_stage,
        }}, "vehicle_mileage")
        self.assertEqual(["檢查執行設定"], [row["name"] for row in rows if row["class"] == "failed"])

    def test_progress_delivery_exception_still_carries_the_local_execution_step(self):
        @site_diagnostics.track_site_execution("vehicle_mileage")
        def run(progress=None):
            progress("登入 PPE")

        def disconnected(_stage):
            raise ConnectionError("NAS progress delivery disconnected")

        try:
            run(progress=disconnected)
        except ConnectionError as exc:
            result = site_diagnostics.make_site_result(
                "vehicle_mileage", "車輛里程", "vehicle_mileage_failed", str(exc), exc)
        else:
            self.fail("Expected the delivery exception")
        self.assertEqual("登入 PPE", result.failure_stage)

    def test_real_runner_reports_preflight_failure_without_any_delivered_progress(self):
        request = AmbulanceReturnRequest.from_dict({"task_id": "preflight", "vehicle": "新坡92"})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            selenium_local, "_task_text", side_effect=RuntimeError("settings unavailable")
        ), mock.patch.object(selenium_local, "_create_driver") as create:
            try:
                selenium_local.run_vehicle_mileage_task(request, Path(directory))
            except RuntimeError as exc:
                result = site_diagnostics.make_site_result(
                    "vehicle_mileage", "車輛里程", "vehicle_mileage_failed", str(exc), exc)
            else:
                self.fail("The preflight must fail")
        self.assertEqual("檢查執行設定", result.failure_stage)
        create.assert_not_called()

    def test_failure_carries_the_local_stage_when_progress_never_reaches_the_store(self):
        @site_diagnostics.track_site_execution("vehicle_mileage")
        def run(progress=None):
            progress("登入 PPE")
            return SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_failed",
                                        "ERR_CONNECTION_RESET [browser_failure:network_connection]")

        with tempfile.TemporaryDirectory() as directory:
            store = JsonTaskStore(Path(directory))
            request = AmbulanceReturnRequest.from_dict({"task_id": "lost-progress", "vehicle": "新坡92"})
            store.create(request)
            result = run(progress=lambda _stage: None)
            payload = store.update_site_result(request.task_id, result)
        self.assertEqual("登入 PPE", result.failure_stage)
        self.assertEqual("登入 PPE", payload["site_statuses"]["vehicle_mileage"]["failure_stage"])

    def test_each_parallel_execution_keeps_its_own_stage(self):
        @site_diagnostics.track_site_execution("vehicle_mileage")
        def run(stage, progress=None):
            progress(stage)
            return SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_failed", "ERR_CONNECTION_RESET")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ["登入 PPE", "儲存"]))
        self.assertEqual(["登入 PPE", "儲存"], [result.failure_stage for result in results])

    def test_success_and_cancellation_keep_their_original_behavior(self):
        success = SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_saved", "saved")

        @site_diagnostics.track_site_execution("vehicle_mileage")
        def run(cancel=False, progress=None):
            progress("儲存")
            if cancel:
                raise TaskCancellationError("cancel")
            return success

        self.assertIs(success, run())
        with self.assertRaises(TaskCancellationError):
            run(cancel=True)

    def test_missing_legacy_data_does_not_create_a_placeholder_or_guessed_stage(self):
        site = {"key": "vehicle_mileage", "status": "vehicle_mileage_failed",
                "detail": "[browser_failure:network_connection]", "failure_stage": "階段未判定"}
        self.assertEqual("", site_diagnostics.merge_diagnostic_fields(site)["failure_stage"])
        self.assertEqual([], app.site_stage_rows({"vehicle_mileage": site}, "vehicle_mileage"))
        self.assertEqual([], app.site_stage_summary_rows({"vehicle_mileage": site}, "vehicle_mileage"))
        rows = app.public_pc_event_rows({"events": [{
            "time": "2026-10-29T18:00:00", "status": "vehicle_mileage_failed",
            "detail": site["detail"], "failure_stage": "階段未判定",
        }]})
        self.assertNotIn("階段未判定", str(rows))
