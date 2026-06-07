import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import ambulance_bot.selenium_local as selenium_local_module
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.selenium_local import (
    _attach_case_form_details,
    _click_save_control,
    _disinfection_query_date,
    _ppe_credentials,
    _previous_case_details,
    _profile_dir,
    _resolve_end_mileage,
    _save_disinfection_record_enabled,
    _save_vehicle_mileage_enabled,
    _write_json_atomic,
    selenium_enabled,
)
from ambulance_bot.duty_credentials import save_duty_automation_credentials


class SeleniumLocalTests(unittest.TestCase):
    def test_selenium_enabled_default_and_false(self):
        os.environ.pop("USE_LOCAL_SELENIUM", None)
        self.assertTrue(selenium_enabled())
        os.environ["USE_LOCAL_SELENIUM"] = "false"
        self.assertFalse(selenium_enabled())

    def test_save_flags_read_environment(self):
        previous_vehicle = os.environ.get("SAVE_VEHICLE_MILEAGE")
        previous_disinfection = os.environ.get("SAVE_DISINFECTION_RECORD")
        try:
            os.environ["SAVE_VEHICLE_MILEAGE"] = "true"
            os.environ["SAVE_DISINFECTION_RECORD"] = "1"
            self.assertTrue(_save_vehicle_mileage_enabled())
            self.assertTrue(_save_disinfection_record_enabled())
        finally:
            if previous_vehicle is None:
                os.environ.pop("SAVE_VEHICLE_MILEAGE", None)
            else:
                os.environ["SAVE_VEHICLE_MILEAGE"] = previous_vehicle
            if previous_disinfection is None:
                os.environ.pop("SAVE_DISINFECTION_RECORD", None)
            else:
                os.environ["SAVE_DISINFECTION_RECORD"] = previous_disinfection

    def test_resolve_end_mileage_accepts_delta(self):
        self.assertEqual(_resolve_end_mileage("123400", "+50"), "123450")
        self.assertEqual(_resolve_end_mileage("123400", "123456"), "123456")

    def test_disinfection_query_date_uses_case_date(self):
        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=datetime(2026, 6, 7, 1, 0),
            raw_text="",
            case_date="1150606",
            case_time="2350",
            return_time="0010",
        )

        self.assertEqual(_disinfection_query_date(request), "2026-06-06")

    def test_named_profile_uses_sibling_of_configured_profile_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("CHROME_PROFILE_DIR")
            try:
                os.environ["CHROME_PROFILE_DIR"] = str(Path(tmp) / "chrome_profile")

                self.assertEqual(_profile_dir("chrome_profile"), Path(tmp) / "chrome_profile")
                self.assertEqual(_profile_dir("vehicle_mileage_profile_task1"), Path(tmp) / "vehicle_mileage_profile_task1")
            finally:
                if previous is None:
                    os.environ.pop("CHROME_PROFILE_DIR", None)
                else:
                    os.environ["CHROME_PROFILE_DIR"] = previous

    def test_configured_profile_dir_expands_environment_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_profile = os.environ.get("CHROME_PROFILE_DIR")
            previous_root = os.environ.get("AMBULANCE_TEST_PROFILE_ROOT")
            try:
                os.environ["AMBULANCE_TEST_PROFILE_ROOT"] = tmp
                os.environ["CHROME_PROFILE_DIR"] = r"%AMBULANCE_TEST_PROFILE_ROOT%\chrome_profile"

                self.assertEqual(_profile_dir("chrome_profile"), Path(tmp) / "chrome_profile")
                self.assertEqual(_profile_dir("disinfection_profile_task1"), Path(tmp) / "disinfection_profile_task1")
            finally:
                if previous_profile is None:
                    os.environ.pop("CHROME_PROFILE_DIR", None)
                else:
                    os.environ["CHROME_PROFILE_DIR"] = previous_profile
                if previous_root is None:
                    os.environ.pop("AMBULANCE_TEST_PROFILE_ROOT", None)
                else:
                    os.environ["AMBULANCE_TEST_PROFILE_ROOT"] = previous_root

    def test_ppe_credentials_prefers_synced_worker_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                os.environ["DUTY_ACCOUNT"] = "env-user"
                os.environ["DUTY_PASSWORD"] = "env-pass"
                save_duty_automation_credentials(
                    [{"actor_no": "8", "user_id": "tyfd00008", "password": "synced-pass"}],
                    last_selected="tyfd00008",
                )

                self.assertEqual(_ppe_credentials(), ("tyfd00008", "synced-pass"))
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
                if previous_account is None:
                    os.environ.pop("DUTY_ACCOUNT", None)
                else:
                    os.environ["DUTY_ACCOUNT"] = previous_account
                if previous_password is None:
                    os.environ.pop("DUTY_PASSWORD", None)
                else:
                    os.environ["DUTY_PASSWORD"] = previous_password

    def test_case_lookup_runs_headless_and_closes_driver_after_login_failure(self):
        class FakeDriver:
            def implicitly_wait(self, seconds: int) -> None:
                pass

        calls: dict[str, object] = {"released": False}
        original_create_driver = selenium_local_module._create_driver
        original_acquire = selenium_local_module._acquire_selenium_session
        original_release = selenium_local_module._release_selenium_session
        original_quit = selenium_local_module._quit_driver
        original_ensure_login = selenium_local_module._ensure_duty_login
        original_save_artifacts = selenium_local_module._save_artifacts
        original_login_error = selenium_local_module._login_error_text
        try:
            fake_driver = FakeDriver()

            def fake_create_driver(*args, **kwargs):
                calls["create_kwargs"] = kwargs
                return fake_driver

            selenium_local_module._create_driver = fake_create_driver
            selenium_local_module._acquire_selenium_session = lambda reason: True
            selenium_local_module._release_selenium_session = lambda reason: calls.__setitem__("released", True)
            selenium_local_module._quit_driver = lambda driver: calls.__setitem__("quit_driver", driver)
            selenium_local_module._ensure_duty_login = lambda driver: False
            selenium_local_module._save_artifacts = lambda *args, **kwargs: None
            selenium_local_module._login_error_text = lambda driver: "login failed"

            with tempfile.TemporaryDirectory() as tmp:
                result = selenium_local_module.query_duty_emergency_cases(Path(tmp), lookup_range="24h")
        finally:
            selenium_local_module._create_driver = original_create_driver
            selenium_local_module._acquire_selenium_session = original_acquire
            selenium_local_module._release_selenium_session = original_release
            selenium_local_module._quit_driver = original_quit
            selenium_local_module._ensure_duty_login = original_ensure_login
            selenium_local_module._save_artifacts = original_save_artifacts
            selenium_local_module._login_error_text = original_login_error

        self.assertEqual(result.status, "duty_login_failed")
        self.assertIs(calls["quit_driver"], fake_driver)
        self.assertTrue(calls["released"])
        self.assertTrue(calls["create_kwargs"]["headless"])

    def test_click_save_control_uses_script_result(self):
        class FakeDriver:
            def __init__(self, result: bool):
                self.result = result
                self.script = ""

            def execute_script(self, script: str):
                self.script = script
                return self.result

        success_driver = FakeDriver(True)
        failed_driver = FakeDriver(False)

        self.assertTrue(_click_save_control(success_driver))
        self.assertFalse(_click_save_control(failed_driver))
        self.assertIn("btnsave", success_driver.script.lower())
        self.assertIn("submit", success_driver.script.lower())

    def test_attach_case_form_details_reuses_cached_personnel(self):
        cases = [{"case_id": "20260603080000001", "address": "新坡分隊"}]
        previous = {
            "20260603080000001": {
                "case_id": "20260603080000001",
                "personnel": ["曾彥綸"],
                "personnel_raw": "曾彥綸",
                "case_date": "1150603",
            }
        }

        result = _attach_case_form_details(None, cases, artifacts_dir=None, previous_cases=previous)

        self.assertEqual(result[0]["personnel"], ["曾彥綸"])
        self.assertEqual(result[0]["case_date"], "1150603")
        self.assertEqual(result[0]["detail_status"], "case_detail_cached")

    def test_write_json_atomic_and_previous_case_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            _write_json_atomic(
                path=path,
                payload={"cases": [{"case_id": "20260603080000001", "personnel": ["曾彥綸"]}]},
            )

            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["cases"][0]["case_id"], "20260603080000001")
            details = _previous_case_details(path)
            self.assertEqual(details["20260603080000001"]["personnel"], ["曾彥綸"])


if __name__ == "__main__":
    unittest.main()
