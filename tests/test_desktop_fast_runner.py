import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ambulance_bot.desktop_fast_runner import DEFAULT_RECORD_ROOT, DesktopFastRunner
from ambulance_bot.manual_task_lock import manual_task_lock_path
from ambulance_bot.models import AmbulanceReturnRequest, FuelRecord, request_from_form
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
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                return_value=SimpleNamespace(status="fuel_record_saved", detail="fuel ok"),
            ) as fuel_mock, patch(
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
            self.assertFalse(manual_task_lock_path(Path(tmp)).exists())
            self.assertEqual(payload["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
            self.assertEqual(payload["site_statuses"]["fuel_record"]["status"], "not_started")
            self.assertEqual(payload["site_statuses"]["disinfection"]["status"], "disinfection_saved")
            self.assertEqual(payload["site_statuses"]["consumables"]["status"], "consumables_saved")
            duty_mock.assert_called_once()
            mileage_mock.assert_called_once()
            fuel_mock.assert_not_called()
            self.assertEqual(mileage_mock.call_args.kwargs["profile_name"], "vehicle_mileage_profile_task_1")
            self.assertTrue(mileage_mock.call_args.kwargs["force_new_driver"])
            disinfection_login_mock.assert_called_once()
            disinfection_mock.assert_called_once()
            self.assertIs(disinfection_mock.call_args.kwargs["existing_driver"], disinfection_login_mock.return_value)
            acs_login_mock.assert_called_once()
            consumables_mock.assert_called_once()

    def test_runs_fuel_site_when_fuel_record_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-fuel",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
                fuel_record=FuelRecord(enabled=True, date="20260627", time="1250", quantity="20.5", unit_price="30.1"),
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_local_selenium_task",
                return_value=SimpleNamespace(status="duty_work_log_saved", detail="duty ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                return_value=SimpleNamespace(status="vehicle_mileage_saved", detail="mileage ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                return_value=SimpleNamespace(status="fuel_record_saved", detail="fuel ok"),
            ) as fuel_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(status="disinfection_saved", detail="disinfection ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
                return_value=Mock(name="driver"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.open_consumable_record_for_task",
                return_value="consumables ok",
            ):
                runner.start_existing("task-fuel")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-fuel")
            self.assertEqual(payload["site_statuses"]["fuel_record"]["status"], "fuel_record_saved")
            fuel_mock.assert_called_once()

    def test_continues_to_disinfection_when_consumables_fails(self):
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
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                return_value=SimpleNamespace(ok=True, status="fuel_record_saved", detail="fuel ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ) as disinfection_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(
                    ok=True,
                    status="disinfection_saved",
                    detail="disinfection ok",
                ),
            ) as disinfection_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
                return_value=Mock(name="driver"),
            ) as acs_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.open_consumable_record_for_task",
                side_effect=RuntimeError("耗材系統需要重新登入或驗證碼"),
            ) as consumables_mock:
                runner.start_existing("task-2")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-2")
            self.assertEqual(payload["overall_status"], "desktop_fast_completed_with_errors")
            self.assertIn("已略過失敗站並接續後續站別", payload["events"][-1]["detail"])
            self.assertEqual(payload["site_statuses"]["consumables"]["status"], "consumables_failed")
            self.assertEqual(payload["site_statuses"]["disinfection"]["status"], "disinfection_saved")
            acs_login_mock.assert_called_once()
            consumables_mock.assert_called_once()
            disinfection_login_mock.assert_called_once()
            disinfection_mock.assert_called_once()

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
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                return_value=SimpleNamespace(ok=True, status="fuel_record_saved", detail="fuel skipped"),
            ) as fuel_mock, patch(
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

    def test_mileage_fuel_single_site_continues_to_other_unfinished_pair_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-mileage-fuel",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="\u65b0\u576193",
                fuel_record=FuelRecord(enabled=True, date="20260707", time="1240", quantity="35.0", unit_price="30.3"),
            )
            store.create(request)
            store.update_site_result(
                "task-mileage-fuel",
                SimpleNamespace(key="vehicle_mileage", name="\u8eca\u8f1b\u91cc\u7a0b", status="vehicle_mileage_failed", detail="retry"),
            )
            store.update_site_result(
                "task-mileage-fuel",
                SimpleNamespace(key="fuel_record", name="\u767b\u6253\u52a0\u6cb9\u7d00\u9304", status="fuel_record_failed", detail="retry"),
            )
            runner = DesktopFastRunner(Path(tmp), store=store)
            calls = []

            def fuel_result(*args, **kwargs):
                calls.append("fuel_record")
                return SimpleNamespace(ok=True, status="fuel_record_saved", detail="fuel ok")

            def mileage_result(*args, **kwargs):
                calls.append("vehicle_mileage")
                return SimpleNamespace(ok=True, status="vehicle_mileage_saved", detail="mileage ok")

            with patch(
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                side_effect=fuel_result,
            ) as fuel_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                side_effect=mileage_result,
            ) as mileage_mock:
                runner.start_site("task-mileage-fuel", "fuel_record")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-mileage-fuel")
            self.assertEqual(calls, ["fuel_record", "vehicle_mileage"])
            self.assertEqual(payload["overall_status"], "desktop_fast_completed")
            self.assertEqual(payload["site_statuses"]["fuel_record"]["status"], "fuel_record_saved")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
            fuel_mock.assert_called_once()
            mileage_mock.assert_called_once()

    def test_four_site_run_skips_completed_sites_and_resumes_at_failed_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-4",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            store.update_site_result(
                "task-4",
                SimpleNamespace(key="duty_work_log", name="消防勤務工作紀錄", status="duty_work_log_saved", detail="done"),
            )
            store.update_site_result(
                "task-4",
                SimpleNamespace(key="vehicle_mileage", name="車輛里程", status="vehicle_mileage_saved", detail="done"),
            )
            store.update_site_result(
                "task-4",
                SimpleNamespace(key="disinfection", name="緊急救護消毒", status="disinfection_failed", detail="retry"),
            )
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_local_selenium_task",
            ) as duty_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
            ) as mileage_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                return_value=SimpleNamespace(ok=True, status="fuel_record_saved", detail="fuel skipped"),
            ) as fuel_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ) as disinfection_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(ok=True, status="disinfection_saved", detail="disinfection ok"),
            ) as disinfection_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
                return_value=Mock(name="driver"),
            ) as acs_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.open_consumable_record_for_task",
                return_value="consumables ok",
            ) as consumables_mock:
                runner.start_existing("task-4")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-4")
            self.assertEqual(payload["overall_status"], "desktop_fast_completed")
            self.assertEqual(payload["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
            self.assertEqual(payload["site_statuses"]["fuel_record"]["status"], "not_started")
            self.assertEqual(payload["site_statuses"]["disinfection"]["status"], "disinfection_saved")
            self.assertEqual(payload["site_statuses"]["consumables"]["status"], "consumables_saved")
            duty_mock.assert_not_called()
            mileage_mock.assert_not_called()
            fuel_mock.assert_not_called()
            disinfection_login_mock.assert_called_once()
            disinfection_mock.assert_called_once()
            acs_login_mock.assert_called_once()
            consumables_mock.assert_called_once()

    def test_vehicle_single_site_passes_update_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-update",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="\u65b0\u576191",
                mileage="200",
            )
            store.create(request)
            payload = store.get("task-update")
            context = {"previous_task": {**request.to_dict(), "mileage": "100"}, "current_task": request.to_dict()}
            payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_needs_update"
            payload["site_statuses"]["vehicle_mileage"]["update_context"] = context
            store.save_payload("task-update", payload)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                return_value=SimpleNamespace(ok=True, status="vehicle_mileage_saved", detail="mileage ok"),
            ) as mileage_mock:
                runner.start_site("task-update", "vehicle_mileage")
                self.assertTrue(runner.wait_for_idle())

            mileage_mock.assert_called_once()
            self.assertEqual(mileage_mock.call_args.kwargs["update_context"], context)

    def test_two_vehicle_site_rerun_skips_saved_vehicle_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = request_from_form(
                {
                    "two_vehicle": "1",
                    "vehicle": "\u65b0\u576191",
                    "driver": "\u66fe\u5f65\u7db8",
                    "return_time": "0200",
                    "mileage": "101",
                    "patient_summary": "\u7537\u4e00\u540d",
                    "consumables": "\u53e3\u7f69=2",
                    "vehicle_2": "\u65b0\u576192",
                    "driver_2": "\u9673\u5c0f\u660e",
                    "return_time_2": "0210",
                    "mileage_2": "202",
                    "patient_summary_2": "\u7121",
                    "consumables_2": "\u624b\u5957=2",
                }
            )
            request.task_id = "task-two-vehicle"
            store.create(request)
            payload = store.get("task-two-vehicle")
            payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_failed"
            payload["site_statuses"]["vehicle_mileage"]["vehicle_results"] = {
                "\u65b0\u576191": {"status": "vehicle_mileage_saved", "detail": "first done"}
            }
            store.save_payload("task-two-vehicle", payload)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                return_value=SimpleNamespace(ok=True, status="vehicle_mileage_saved", detail="second ok"),
            ) as mileage_mock:
                runner.start_site("task-two-vehicle", "vehicle_mileage")
                self.assertTrue(runner.wait_for_idle())

            mileage_mock.assert_called_once()
            self.assertEqual(mileage_mock.call_args.args[0].vehicle, "\u65b0\u576192")
            payload = store.get("task-two-vehicle")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
            self.assertEqual(
                payload["site_statuses"]["vehicle_mileage"]["vehicle_results"]["\u65b0\u576191"]["status"],
                "vehicle_mileage_saved",
            )
            self.assertEqual(
                payload["site_statuses"]["vehicle_mileage"]["vehicle_results"]["\u65b0\u576192"]["status"],
                "vehicle_mileage_saved",
            )

    def test_record_folders_ignore_env_and_use_existing_nas_record_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = DesktopFastRunner(Path(tmp))
            request = request_from_form(
                {
                    "case_date": "2026-06-02",
                    "case_time": "1010",
                    "vehicle": "\u65b0\u576191",
                }
            )

            with patch.dict("os.environ", {"AMBULANCE_RECORD_ROOT": r"W:\救護硬碟\救護登錄器及行車紀錄器"}), patch("pathlib.Path.mkdir"):
                detail = runner._ensure_record_folders(request)

            self.assertIn(str(DEFAULT_RECORD_ROOT), detail)
            self.assertIn("\u6551\u8b77\u5bc6\u9304\u5668\u53ca\u884c\u8eca\u7d00\u9304\u5668", detail)
            self.assertNotIn("\u6551\u8b77\u767b\u9304\u5668", detail)

    def test_runner_blocks_second_start_for_same_task_while_first_run_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-active",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="\u65b0\u576191",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch("ambulance_bot.desktop_fast_runner.threading.Thread") as thread_mock:
                runner.start_site("task-active", "consumables")
                runner.start_site("task-active", "vehicle_mileage")
                runner.start_existing("task-active")

            self.assertEqual(thread_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
