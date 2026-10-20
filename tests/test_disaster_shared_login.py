from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import worker
from ambulance_bot import desktop_fast_runner, duty_credentials, selenium_local
from ambulance_bot.desktop_fast_runner import DesktopFastRunner
from ambulance_bot.login_audit import login_audit_for_site
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_store import JsonTaskStore


def request(service_type="disaster"):
    return AmbulanceReturnRequest.from_dict({
        "task_id": "disaster-shared-login", "created_at": "2026-10-20T17:37:29",
        "service_type": service_type, "two_vehicle": service_type == "ems",
        "vehicle": "新坡15", "driver": "乙",
        "personnel": ["甲", "乙"], "personnel_accounts": ["TYFD-A", "TYFD-B"],
        "case_date": "2026/10/20", "case_time": "1636", "return_time": "1713",
        "vehicle_entries": [
            {"vehicle": "新坡11", "driver": "甲", "mileage": "35444", "return_time": "1713"},
            {"vehicle": "新坡15", "driver": "乙", "mileage": "25400", "return_time": "1728"},
        ],
    })


class DisasterSharedLoginTests(unittest.TestCase):
    def test_work_credentials_and_audit_use_first_vehicle_without_changing_record_driver(self):
        task = request()
        original = task.to_dict()
        credentials = {
            "TYFD-A": duty_credentials.DutyCredential(user_id="TYFD-A", password="test-a", name="甲"),
            "TYFD-B": duty_credentials.DutyCredential(user_id="TYFD-B", password="test-b", name="乙"),
        }
        with patch.object(duty_credentials, "load_duty_credential",
                          side_effect=lambda preferred, **kwargs: credentials.get(preferred[0])), \
             patch.object(duty_credentials, "load_saved_duty_work_credential", return_value=None):
            attempts = selenium_local._duty_login_credential_attempts(task)
            audit = login_audit_for_site("duty_work_log", task)
        self.assertEqual("TYFD-A", attempts[0].user_id)
        self.assertIn("TYFD-A", audit)
        self.assertNotIn("TYFD-B", audit)
        self.assertEqual(original, task.to_dict())

    def test_ems_work_keeps_its_existing_parent_driver(self):
        task = request("ems")
        self.assertEqual(["TYFD-B", "乙"], task.driver_duty_login_account_candidates)

    def test_mileage_login_request_does_not_replace_vehicle_form_fields(self):
        first, second = request().vehicle_requests()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(selenium_local, "_ensure_ppe_vehicle_mileage_session", return_value=True) as login, \
             patch.object(selenium_local, "_prepare_vehicle_mileage_form", return_value="未按儲存") as fill, \
             patch.object(selenium_local, "_save_artifacts"):
            selenium_local._open_vehicle_mileage_page(
                Mock(), second, Path(tmp), login_request=first)
        self.assertIs(first, login.call_args.args[1])
        self.assertIs(second, fill.call_args.args[1])
        self.assertEqual(("新坡15", "乙", "25400", "1728"),
                         (second.vehicle, second.driver, second.mileage, second.return_time))

    def test_public_mileage_runner_forwards_login_request_separately(self):
        first, second = request().vehicle_requests()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(selenium_local, "_open_vehicle_mileage_page", return_value="未按儲存") as open_page, \
             patch.object(selenium_local, "apply_tile"), \
             patch.object(selenium_local, "_set_window_size_if_enabled"), \
             patch.object(selenium_local, "mark_driver_operation_active"):
            selenium_local.run_vehicle_mileage_task(
                second, Path(tmp), existing_driver=Mock(), use_session_lock=False, login_request=first)
        self.assertIs(second, open_page.call_args.args[1])
        self.assertIs(first, open_page.call_args.kwargs["login_request"])

    def test_worker_reuses_first_vehicle_login_even_when_primary_vehicle_is_skipped(self):
        for skip_first in (False, True):
            with self.subTest(skip_first=skip_first), \
                 patch.object(worker, "post_status"), \
                 patch.object(worker, "run_vehicle_mileage_task",
                              return_value=SimpleNamespace(status="vehicle_mileage_saved", detail="已回查")) as run:
                completed = {"新坡11": {"status": "vehicle_mileage_saved", "detail": "已回查"}} if skip_first else {}
                worker.run_vehicle_task("http://nas", "worker", request().to_dict(), Path("artifacts"),
                                        vehicle_results=completed, update_overall=False)
            self.assertEqual(["乙"] if skip_first else ["甲", "乙"],
                             [call.args[0].driver for call in run.call_args_list])
            self.assertEqual(["甲"] * run.call_count,
                             [call.kwargs["login_request"].driver for call in run.call_args_list])
            self.assertTrue(run.call_args_list[0].kwargs["force_new_driver"])

    def test_ems_mileage_keeps_each_vehicle_login_without_shared_override(self):
        with patch.object(worker, "post_status"), \
             patch.object(worker, "run_vehicle_mileage_task",
                          return_value=SimpleNamespace(status="vehicle_mileage_saved", detail="已回查")) as run:
            worker.run_vehicle_task("http://nas", "worker", request("ems").to_dict(), Path("artifacts"),
                                    update_overall=False)
        self.assertEqual(["甲", "乙"], [call.args[0].driver for call in run.call_args_list])
        self.assertTrue(all("login_request" not in call.kwargs for call in run.call_args_list))

    def test_desktop_reuses_first_vehicle_login_even_when_primary_vehicle_is_skipped(self):
        for skip_first in (False, True):
            with self.subTest(skip_first=skip_first), tempfile.TemporaryDirectory() as tmp:
                store = JsonTaskStore(Path(tmp) / "tasks")
                task = request()
                store.create(task)
                if skip_first:
                    payload = store.get(task.task_id)
                    payload["site_statuses"]["vehicle_mileage"]["vehicle_results"] = {
                        "新坡11": {"status": "vehicle_mileage_saved", "detail": "已回查"}}
                    store.save_payload(task.task_id, payload)
                runner = DesktopFastRunner(Path(tmp), store=store)
                with patch.object(desktop_fast_runner, "run_vehicle_mileage_task",
                                  return_value=SimpleNamespace(status="vehicle_mileage_saved", detail="已回查")) as run:
                    runner.start_site(task.task_id, "vehicle_mileage")
                    self.assertTrue(runner.wait_for_idle())
                self.assertEqual(["乙"] if skip_first else ["甲", "乙"],
                                 [call.args[0].driver for call in run.call_args_list])
                self.assertEqual(["甲"] * run.call_count,
                                 [call.kwargs["login_request"].driver for call in run.call_args_list])


if __name__ == "__main__":
    unittest.main()
