import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import worker as runtime
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_cancellation import TaskCancellationError


class WorkerVehicleLoginAuditTests(unittest.TestCase):
    def task(self):
        return {
            "task_id": "vehicle-login-audit", "created_at": "2026-10-20T17:37:29",
            "service_type": "disaster", "vehicle": "新坡11", "driver": "甲",
            "vehicle_entries": [
                {"vehicle": "新坡11", "driver": "甲", "mileage": "35444"},
                {"vehicle": "新坡15", "driver": "乙", "mileage": "25400"},
            ],
        }

    @staticmethod
    def audit(site_key, request):
        return f"登入帳號：{site_key}=任務司機，任務司機，{request.driver} - account-{request.driver}"

    def run_site(self, site_key, *, completed=None, service_type="disaster"):
        task = self.task()
        task["service_type"] = service_type
        task["two_vehicle"] = service_type == "ems"
        if site_key == "fuel_record":
            for entry in task["vehicle_entries"]:
                entry["fuel_record"] = dict(enabled=True, date="20261020", time="1715",
                                            driver=entry["driver"], quantity="20", unit_price="30")
        raw_name = "run_vehicle_mileage_task" if site_key == "vehicle_mileage" else "run_fuel_record_task"
        entrypoint = runtime.run_vehicle_task if site_key == "vehicle_mileage" else runtime.run_fuel_worker_task
        actions = []

        def action(request, *_args, **kwargs):
            actions.append(request.vehicle)
            kwargs["progress"](f"填寫-{request.vehicle}")
            return runtime.make_site_result(site_key, site_key, f"{site_key}_saved", "已回查")

        with patch.object(runtime, "login_audit_for_site", side_effect=self.audit), \
             patch.object(runtime, raw_name, side_effect=action), \
             patch.object(runtime, "post_status") as post:
            result = entrypoint("http://nas", "worker", task, Path("artifacts"),
                                vehicle_results=completed, update_overall=False)
        return result, actions, post.call_args_list

    def check_vehicle_accounts(self, site_key, *, service_type="disaster"):
        result, actions, posts = self.run_site(site_key, service_type=service_type)
        self.assertEqual(["新坡11", "新坡15"], actions)
        shared_mileage = site_key == "vehicle_mileage" and service_type == "disaster"
        for vehicle, driver in (("新坡11", "甲"), ("新坡15", "甲" if shared_mileage else "乙")):
            with self.subTest(vehicle=vehicle):
                vehicle_posts = [call for call in posts if call.kwargs.get("vehicle_key") == vehicle
                                 and call.args[2] == f"{site_key}_saved"]
                self.assertEqual(1, len(vehicle_posts))
                self.assertIn(f"account-{driver}", vehicle_posts[0].args[3])
                other = "乙" if driver == "甲" else "甲"
                self.assertNotIn(f"account-{other}", vehicle_posts[0].args[3])
                progress = [call for call in posts if f"填寫-{vehicle}" in call.args[3]]
                self.assertEqual(1, len(progress))
                self.assertIn(f"account-{driver}", progress[0].args[3])
        self.assertIn("新坡11: 登入帳號：", result.detail)
        self.assertIn("新坡15: 登入帳號：", result.detail)
        self.assertIn("account-甲", result.detail)
        if shared_mileage:
            self.assertNotIn("account-乙", result.detail)
        else:
            self.assertIn("account-乙", result.detail)

    def test_disaster_mileage_reports_primary_driver_for_all_vehicles(self):
        self.check_vehicle_accounts("vehicle_mileage")

    def test_ems_mileage_reports_each_vehicle_driver_in_progress_and_result(self):
        self.check_vehicle_accounts("vehicle_mileage", service_type="ems")

    def test_fuel_reports_each_vehicle_driver_in_progress_and_result(self):
        self.check_vehicle_accounts("fuel_record")

    def test_saved_vehicle_keeps_checkpoint_and_only_failed_vehicle_is_run(self):
        completed = {"新坡11": {"status": "vehicle_mileage_saved", "detail": "原帳號與儲存證據"}}
        original = copy.deepcopy(completed)
        result, actions, posts = self.run_site("vehicle_mileage", completed=completed)
        self.assertEqual(["新坡15"], actions)
        self.assertEqual(original, completed)
        self.assertFalse(any(call.kwargs.get("vehicle_key") == "新坡11" for call in posts))
        self.assertIn("新坡11: 已完成，略過", result.detail)
        self.assertIn("account-甲", result.detail)
        self.assertNotIn("account-乙", result.detail)

    def test_shared_session_sites_keep_their_shared_login_account(self):
        request = AmbulanceReturnRequest.from_dict(self.task())
        for site_key in ("disinfection", "consumables"):
            with self.subTest(site_key=site_key), \
                 patch.object(runtime, "post_status") as post, \
                 patch.object(runtime, "login_audit_for_site", side_effect=AssertionError("shared session must retain its account")):
                result = runtime._run_worker_per_vehicle_site(
                    "http://nas", request, site_key, site_key,
                    lambda *_args: runtime.make_site_result(site_key, site_key, f"{site_key}_saved", "已回查"),
                    login_audit="登入帳號：共用登入帳號")
            self.assertEqual(2, result.detail.count("登入帳號：共用登入帳號"))
            self.assertEqual(2, post.call_count)

    def test_cancellation_is_not_converted_to_a_vehicle_failure(self):
        request = AmbulanceReturnRequest.from_dict(self.task())
        with patch.object(runtime, "post_status") as post:
            with self.assertRaises(TaskCancellationError):
                runtime._run_worker_per_vehicle_site(
                    "http://nas", request, "vehicle_mileage", "里程",
                    lambda *_args: (_ for _ in ()).throw(TaskCancellationError("cancelled")),
                    login_audit=self.audit("vehicle_mileage", request))
        post.assert_not_called()
