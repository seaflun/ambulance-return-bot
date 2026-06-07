import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ambulance_bot.desktop_fast_runner import DesktopFastRunner
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_store import JsonTaskStore


class DesktopFastRunnerTests(unittest.TestCase):
    def test_runs_four_sites_and_writes_local_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-1",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_local_selenium_task",
                return_value=SimpleNamespace(status="duty_work_log_saved", detail="duty ok"),
            ) as duty_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                return_value=SimpleNamespace(status="vehicle_mileage_saved", detail="mileage ok"),
            ) as mileage_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ) as disinfection_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(status="disinfection_saved", detail="disinfection ok"),
            ) as disinfection_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
                return_value=Mock(name="driver"),
            ) as acs_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.open_consumable_record_for_task",
                return_value="consumables ok",
            ) as consumables_mock:
                runner.start_existing("task-1")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-1")
            self.assertEqual(payload["overall_status"], "desktop_fast_completed")
            self.assertEqual(payload["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
            self.assertEqual(payload["site_statuses"]["disinfection"]["status"], "disinfection_saved")
            self.assertEqual(payload["site_statuses"]["consumables"]["status"], "consumables_saved")
            duty_mock.assert_called_once()
            mileage_mock.assert_called_once()
            self.assertEqual(mileage_mock.call_args.kwargs["profile_name"], "vehicle_mileage_profile_task_1")
            self.assertTrue(mileage_mock.call_args.kwargs["force_new_driver"])
            disinfection_login_mock.assert_called_once()
            disinfection_mock.assert_called_once()
            self.assertIs(disinfection_mock.call_args.kwargs["existing_driver"], disinfection_login_mock.return_value)
            acs_login_mock.assert_called_once()
            consumables_mock.assert_called_once()

    def test_stops_before_consumables_when_disinfection_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-2",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_local_selenium_task",
                return_value=SimpleNamespace(ok=True, status="duty_work_log_saved", detail="duty ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                return_value=SimpleNamespace(ok=True, status="vehicle_mileage_saved", detail="mileage ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(
                    ok=False,
                    status="disinfection_failed",
                    detail="消毒系統需要重新登入或驗證碼",
                ),
            ), patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
            ) as acs_login_mock:
                runner.start_existing("task-2")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-2")
            self.assertEqual(payload["overall_status"], "desktop_fast_completed_with_errors")
            self.assertIn("耗材未開啟", payload["events"][-1]["detail"])
            self.assertEqual(payload["site_statuses"]["disinfection"]["status"], "disinfection_failed")
            self.assertEqual(payload["site_statuses"]["consumables"]["status"], "not_started")
            acs_login_mock.assert_not_called()

    def test_single_site_runs_only_requested_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-3",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="?啣91",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_local_selenium_task",
            ) as duty_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
            ) as mileage_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ) as disinfection_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(ok=True, status="disinfection_saved", detail="disinfection ok"),
            ) as disinfection_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
            ) as acs_login_mock:
                runner.start_site("task-3", "disinfection")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-3")
            self.assertEqual(payload["overall_status"], "desktop_fast_completed")
            self.assertEqual(payload["site_statuses"]["disinfection"]["status"], "disinfection_saved")
            self.assertEqual(payload["site_statuses"]["duty_work_log"]["status"], "not_started")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "not_started")
            self.assertEqual(payload["site_statuses"]["consumables"]["status"], "not_started")
            duty_mock.assert_not_called()
            mileage_mock.assert_not_called()
            disinfection_login_mock.assert_called_once()
            disinfection_mock.assert_called_once()
            acs_login_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
