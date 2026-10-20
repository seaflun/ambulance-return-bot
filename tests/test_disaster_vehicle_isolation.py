import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import worker
from ambulance_bot import desktop_fast_runner
from ambulance_bot.desktop_fast_runner import DesktopFastRunner
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_cancellation import TaskCancellationError
from ambulance_bot.task_store import JsonTaskStore


class DisasterVehicleIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = JsonTaskStore(self.root / "tasks")
        self.request = AmbulanceReturnRequest.from_dict(dict(
            task_id="disaster-isolation", created_at="2026-10-20T17:37:29", service_type="disaster",
            vehicle="新坡11", driver="甲", case_time="1636", return_time="1713",
            vehicle_entries=[dict(vehicle="新坡11", driver="甲", mileage="35444"),
                             dict(vehicle="新坡15", driver="乙", mileage="25400")]))
        self.store.create(self.request)

    @staticmethod
    def audit(_site_key, _request):
        return "登入帳號：里程=任務司機，任務司機，甲 - shared-account"

    def results(self):
        return self.store.get(self.request.task_id)["site_statuses"]["vehicle_mileage"].get("vehicle_results", {})

    def run_mode(self, mode, outcomes):
        actions, progress_snapshots, posts, login_drivers = [], [], [], []

        def action(request, *_args, **kwargs):
            actions.append(request.vehicle)
            login_drivers.append(kwargs["login_request"].driver)
            kwargs["progress"]("填寫里程")
            progress_snapshots.append((request.vehicle, copy.deepcopy(self.results())))
            outcome = outcomes[request.vehicle]
            if isinstance(outcome, BaseException):
                raise outcome
            return worker.make_site_result("vehicle_mileage", "里程", outcome,
                                           "車輛里程紀錄已存在" if request.vehicle == "新坡11" else "各車獨立填寫")

        def post(_server, task_id, status, detail, **kwargs):
            posts.append((status, detail, kwargs))
            self.store.update_site_result(task_id, worker.make_site_result(
                "vehicle_mileage", "里程", status, detail),
                vehicle_key=kwargs.get("vehicle_key", ""), vehicle_label=kwargs.get("vehicle_label", ""))

        target = worker if mode == "worker" else desktop_fast_runner
        with patch.object(target, "run_vehicle_mileage_task", side_effect=action), \
             patch.object(target, "login_audit_for_site", side_effect=self.audit):
            if mode == "worker":
                with patch.object(worker, "post_status", side_effect=post):
                    result = worker.run_vehicle_task(
                        "http://nas", "worker", self.request.to_dict(), self.root,
                        vehicle_results=self.results(), update_overall=False)
            else:
                runner = DesktopFastRunner(self.root, store=self.store)
                runner.start_site(self.request.task_id, "vehicle_mileage")
                self.assertTrue(runner.wait_for_idle())
                result = self.store.get(self.request.task_id)["site_statuses"]["vehicle_mileage"]
        return result, actions, progress_snapshots, posts, login_drivers

    def check_existing_first_and_new_second(self, mode):
        result, actions, snapshots, posts, logins = self.run_mode(mode, dict(
            新坡11="vehicle_mileage_saved", 新坡15="vehicle_mileage_saved"))
        self.assertEqual(["新坡11", "新坡15"], actions)
        self.assertEqual(["甲", "甲"], logins)
        second_snapshot = snapshots[1][1]
        self.assertEqual("vehicle_mileage_saved", second_snapshot["新坡11"]["status"])
        self.assertEqual("vehicle_mileage_running", second_snapshot["新坡15"]["status"])
        self.assertEqual("vehicle_mileage_saved", self.results()["新坡11"]["status"])
        self.assertEqual("vehicle_mileage_saved", self.results()["新坡15"]["status"])
        if mode == "worker":
            progress = [entry for entry in posts if "填寫里程" in entry[1]]
            self.assertEqual(["新坡11", "新坡15"], [entry[2].get("vehicle_key") for entry in progress])
            self.assertEqual(["新坡11", "新坡15"], [entry[2].get("vehicle_label") for entry in progress])

    def test_worker_existing_first_and_new_second_keep_independent_progress_and_saved_results(self):
        self.check_existing_first_and_new_second("worker")

    def test_desktop_existing_first_and_new_second_keep_independent_progress_and_saved_results(self):
        self.check_existing_first_and_new_second("desktop")

    def check_first_does_not_block_second(self, mode, first_outcome):
        result, actions, _snapshots, _posts, logins = self.run_mode(mode, {
            "新坡11": first_outcome, "新坡15": "vehicle_mileage_saved"})
        self.assertEqual(["新坡11", "新坡15"], actions)
        self.assertEqual(["甲", "甲"], logins)
        expected = first_outcome if isinstance(first_outcome, str) else "vehicle_mileage_failed"
        self.assertEqual(expected, self.results()["新坡11"]["status"])
        self.assertEqual("vehicle_mileage_saved", self.results()["新坡15"]["status"])
        self.assertEqual(expected, result["status"] if isinstance(result, dict) else result.status)

    def test_worker_first_failure_does_not_block_second(self):
        self.check_first_does_not_block_second("worker", RuntimeError("第一車普通錯誤"))

    def test_desktop_first_failure_does_not_block_second(self):
        self.check_first_does_not_block_second("desktop", RuntimeError("第一車普通錯誤"))

    def test_worker_first_failed_result_does_not_block_second(self):
        self.check_first_does_not_block_second("worker", "vehicle_mileage_failed")

    def test_desktop_first_failed_result_does_not_block_second(self):
        self.check_first_does_not_block_second("desktop", "vehicle_mileage_failed")

    def test_worker_first_waiting_confirmation_does_not_block_second(self):
        self.check_first_does_not_block_second("worker", "vehicle_mileage_waiting_confirmation")

    def test_desktop_first_waiting_confirmation_does_not_block_second(self):
        self.check_first_does_not_block_second("desktop", "vehicle_mileage_waiting_confirmation")

    def check_retry_only_failed_second(self, mode):
        self.run_mode(mode, dict(新坡11="vehicle_mileage_saved", 新坡15="vehicle_mileage_failed"))
        before_first = copy.deepcopy(self.results()["新坡11"])
        self.assertEqual("vehicle_mileage_failed",
                         self.store.get(self.request.task_id)["site_statuses"]["vehicle_mileage"]["status"])
        _, actions, snapshots, _posts, logins = self.run_mode(mode, dict(新坡15="vehicle_mileage_saved"))
        self.assertEqual(["新坡15"], actions)
        self.assertEqual(["甲"], logins)
        self.assertEqual(before_first, snapshots[0][1]["新坡11"])
        self.assertEqual(before_first, self.results()["新坡11"])

    def test_worker_retry_preserves_first_success_and_runs_only_second_failure(self):
        self.check_retry_only_failed_second("worker")

    def test_desktop_retry_preserves_first_success_and_runs_only_second_failure(self):
        self.check_retry_only_failed_second("desktop")

    def test_worker_explicit_cancellation_stops_the_second_vehicle(self):
        with self.assertRaises(TaskCancellationError):
            self.run_mode("worker", dict(新坡11=TaskCancellationError("使用者取消")))
        self.assertNotIn("新坡15", self.results())

    def test_desktop_explicit_cancellation_stops_the_second_vehicle(self):
        self.run_mode("desktop", dict(新坡11=TaskCancellationError("使用者取消")))
        self.assertNotIn("新坡15", self.results())
