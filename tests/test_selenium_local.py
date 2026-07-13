import json
import inspect
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import ambulance_bot.selenium_local as selenium_local_module
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.selenium_local import (
    _attach_case_form_details,
    _assert_disinfection_not_login,
    _click_disinfection_query,
    _click_disinfection_save,
    _click_fuel_card_register,
    _click_save_control,
    _click_vehicle_mileage_save,
    _create_driver,
    _disinfection_query_date,
    _duty_login_credential_attempts,
    _ensure_duty_login,
    _ensure_fuel_query_period,
    _fill_fuel_grid_record,
    _fill_vehicle_grid_values,
    _fuel_card_labels,
    _fuel_query_period,
    _id_number_from_cases_for_credential,
    lookup_synced_credential_id_number,
    _open_disinfection_detail_for_case,
    _ppe_option_names,
    _ppe_option_records_from_script,
    _ppe_option_value,
    _set_disinfection_query_date,
    _ensure_ppe_vehicle_mileage_session,
    _create_local_driver_with_retry,
    _ppe_credentials,
    _prepare_duty_work_log_form,
    _previous_case_details,
    _profile_dir,
    cleanup_stale_selenium_profiles,
    _resolve_end_mileage,
    _save_duty_work_log_enabled,
    _save_disinfection_probe_enabled,
    _save_disinfection_record_enabled,
    _save_vehicle_mileage_enabled,
    _vehicle_mileage_previous_request,
    _vehicle_mileage_driver_value,
    _vehicle_mileage_values,
    _wait_for_fuel_driver_value,
    _write_json_atomic,
    run_disinfection_task,
    run_fuel_record_task,
    run_local_selenium_task,
    run_vehicle_mileage_task,
    selenium_enabled,
)
from ambulance_bot.duty_credentials import load_synced_worker_credential, save_credential_sync_payload, save_duty_automation_credentials
from ambulance_bot.duty_credentials import DutyCredential


class SeleniumLocalTests(unittest.TestCase):
    def test_ppe_option_records_decode_unicode_driver_names(self):
        source = 'dataSource: [{"DeptSeq":null,"Value":"2448","Text":"\\u90ED\\u570B\\u5075"}]'

        options = _ppe_option_records_from_script(source)

        self.assertEqual(_ppe_option_value(options, "郭國偵"), "2448")

    def test_ppe_option_value_accepts_reordered_fields_and_normalized_whitespace(self):
        options = [{"Text": " 郭  國偵 ", "DeptSeq": None, "Value": "2448"}]

        self.assertEqual(_ppe_option_value(options, "郭 國偵"), "2448")

    def test_ppe_option_value_rejects_partial_name_and_zero_id(self):
        self.assertEqual(_ppe_option_value([{"Text": "郭國偵", "Value": "2448"}], "郭國"), "")
        self.assertEqual(_ppe_option_value([{"Text": "郭國偵", "Value": "0"}], "郭國偵"), "")

    def test_ppe_option_names_are_unique_and_bounded(self):
        options = [
            {"Text": "郭國偵", "Value": "2448"},
            {"DriverName": "郭國偵", "Driver": "2448"},
            {"Name": "陳俊翰", "Id": "2481"},
        ]

        self.assertEqual(_ppe_option_names(options, limit=1), ["郭國偵"])

    def test_wait_for_fuel_driver_value_decodes_unicode_options(self):
        class FakeDriver:
            def execute_script(self, _script: str):
                return 'dataSource: [{"Value":"2448","Text":"\\u90ED\\u570B\\u5075"}]'

        self.assertEqual(_wait_for_fuel_driver_value(FakeDriver(), "郭國偵", timeout=0), "2448")

    def test_wait_for_fuel_driver_value_reports_requested_and_candidates(self):
        class FakeDriver:
            def execute_script(self, _script: str):
                return (
                    'dataSource: ['
                    '{"Value":"2448","Text":"\\u90ED\\u570B\\u5075"},'
                    '{"Value":"2481","Text":"\\u9673\\u4FCA\\u7FF0"}'
                    ']'
                )

        with self.assertRaisesRegex(
            selenium_local_module.WebDriverException,
            "missing fuel driver: requested=林志偉; candidates=郭國偵,陳俊翰",
        ):
            _wait_for_fuel_driver_value(FakeDriver(), "林志偉", timeout=0)

    def test_fill_fuel_grid_uses_resolved_driver_without_existing_rows(self):
        class FakeDriver:
            def __init__(self):
                self.driver_value = ""

            def execute_script(self, script: str, *args):
                if "return Array.from(document.scripts)" in script:
                    return 'dataSource: [{"Value":"2448","Text":"\\u90ED\\u570B\\u5075"}]'
                self.driver_value = str(args[1])
                return {"ok": True}

        request = AmbulanceReturnRequest.from_dict(
            {
                "vehicle": "新坡92",
                "driver": "郭國偵",
                "fuel_record": {
                    "enabled": True,
                    "date": "20260713",
                    "time": "1320",
                    "driver": "郭國偵",
                    "product": "超級柴油",
                    "quantity": "42.122",
                    "unit_price": "30.3",
                },
            }
        )
        driver = FakeDriver()

        _fill_fuel_grid_record(driver, request)

        self.assertEqual(driver.driver_value, "2448")

    def test_vehicle_mileage_driver_value_uses_exact_option(self):
        class FakeDriver:
            def execute_script(self, _script: str, _row_index: int):
                return {
                    "options": [
                        {"Text": "郭國偵", "Value": "2448"},
                        {"Text": "郭國", "Value": "9999"},
                    ],
                    "row_driver": "8888",
                    "row_driver_name": "其他人",
                }

        self.assertEqual(_vehicle_mileage_driver_value(FakeDriver(), "郭國偵"), "2448")

    def test_vehicle_mileage_driver_value_rejects_different_existing_row(self):
        class FakeDriver:
            def execute_script(self, _script: str, _row_index: int):
                return {
                    "options": [{"Text": "陳俊翰", "Value": "2481"}],
                    "row_driver": "8888",
                    "row_driver_name": "其他人",
                }

        with self.assertRaisesRegex(
            selenium_local_module.WebDriverException,
            "missing vehicle mileage driver: requested=郭國偵; candidates=陳俊翰",
        ):
            _vehicle_mileage_driver_value(FakeDriver(), "郭國偵")

    def test_fill_vehicle_grid_passes_exact_driver_id(self):
        class FakeDriver:
            def __init__(self):
                self.driver_value = ""

            def execute_script(self, script: str, *args):
                if "row_driver_name" in script:
                    return {
                        "options": [{"Text": "郭國偵", "Value": "2448"}],
                        "row_driver": "",
                        "row_driver_name": "",
                    }
                self.driver_value = str(args[2])
                return []

        driver = FakeDriver()
        values = {
            "開始日期": "20260713",
            "開始時間": "1101",
            "結束日期": "20260713",
            "結束時間": "1320",
            "開始里程": "20828",
            "結束里程": "20910",
            "事由": "急病",
            "前往地點": "新華路一段886號",
            "駕駛人": "郭國偵",
        }

        _fill_vehicle_grid_values(driver, values)

        self.assertEqual(driver.driver_value, "2448")

    def test_selenium_enabled_default_and_false(self):
        os.environ.pop("USE_LOCAL_SELENIUM", None)
        self.assertTrue(selenium_enabled())
        os.environ["USE_LOCAL_SELENIUM"] = "false"
        self.assertFalse(selenium_enabled())

    def test_save_flags_read_environment(self):
        previous_vehicle = os.environ.get("SAVE_VEHICLE_MILEAGE")
        previous_duty = os.environ.get("SAVE_DUTY_WORK_LOG")
        previous_disinfection = os.environ.get("SAVE_DISINFECTION_RECORD")
        previous_probe = os.environ.get("SAVE_DISINFECTION_PROBE")
        try:
            os.environ["SAVE_VEHICLE_MILEAGE"] = "true"
            os.environ["SAVE_DUTY_WORK_LOG"] = "yes"
            os.environ["SAVE_DISINFECTION_RECORD"] = "1"
            os.environ["SAVE_DISINFECTION_PROBE"] = "1"
            self.assertTrue(_save_vehicle_mileage_enabled())
            self.assertTrue(_save_duty_work_log_enabled())
            self.assertTrue(_save_disinfection_record_enabled())
            self.assertTrue(_save_disinfection_probe_enabled())
            os.environ.pop("SAVE_VEHICLE_MILEAGE", None)
            os.environ.pop("SAVE_DUTY_WORK_LOG", None)
            os.environ.pop("SAVE_DISINFECTION_RECORD", None)
            os.environ.pop("SAVE_DISINFECTION_PROBE", None)
            self.assertTrue(_save_vehicle_mileage_enabled())
            self.assertTrue(_save_duty_work_log_enabled())
            self.assertTrue(_save_disinfection_record_enabled())
            self.assertFalse(_save_disinfection_probe_enabled())
            os.environ["SAVE_VEHICLE_MILEAGE"] = "false"
            os.environ["SAVE_DUTY_WORK_LOG"] = "0"
            os.environ["SAVE_DISINFECTION_RECORD"] = "0"
            os.environ["SAVE_DISINFECTION_PROBE"] = "0"
            self.assertFalse(_save_vehicle_mileage_enabled())
            self.assertFalse(_save_duty_work_log_enabled())
            self.assertFalse(_save_disinfection_record_enabled())
            self.assertFalse(_save_disinfection_probe_enabled())
        finally:
            if previous_vehicle is None:
                os.environ.pop("SAVE_VEHICLE_MILEAGE", None)
            else:
                os.environ["SAVE_VEHICLE_MILEAGE"] = previous_vehicle
            if previous_duty is None:
                os.environ.pop("SAVE_DUTY_WORK_LOG", None)
            else:
                os.environ["SAVE_DUTY_WORK_LOG"] = previous_duty
            if previous_disinfection is None:
                os.environ.pop("SAVE_DISINFECTION_RECORD", None)
            else:
                os.environ["SAVE_DISINFECTION_RECORD"] = previous_disinfection
            if previous_probe is None:
                os.environ.pop("SAVE_DISINFECTION_PROBE", None)
            else:
                os.environ["SAVE_DISINFECTION_PROBE"] = previous_probe

    def test_vehicle_mileage_message_does_not_use_attempted_confirm_wording(self):
        source = Path(selenium_local_module.__file__).read_text(encoding="utf-8")

        self.assertNotIn("\\u5617\\u8a66\\u78ba\\u8a8d", source)
        self.assertIn("\\u6309\\u4e0b\\u78ba\\u8a8d", source)
        self.assertIn("\\u672a\\u5075\\u6e2c\\u5230\\u78ba\\u8a8d\\u8996\\u7a97", source)

    def test_vehicle_mileage_runs_even_without_fuel_record(self):
        request = AmbulanceReturnRequest(
            task_id="task-mileage-no-fuel",
            created_at=datetime.now(),
            raw_text="",
            vehicle="\u65b0\u576191",
            driver="\u694a\u4ef2\u8c6a",
            mileage="143635",
        )
        opened: dict[str, object] = {}

        def fake_open(driver, opened_request, output_dir, update_context=None):
            opened["request"] = opened_request
            return "mileage ok"

        class FakeDriver:
            page_source = ""

            def implicitly_wait(self, seconds):
                self.wait_seconds = seconds

        original_open = selenium_local_module._open_vehicle_mileage_page
        original_save_enabled = selenium_local_module._save_vehicle_mileage_enabled
        original_apply_tile = selenium_local_module.apply_tile
        original_set_window = selenium_local_module._set_window_size_if_enabled
        try:
            selenium_local_module._open_vehicle_mileage_page = fake_open
            selenium_local_module._save_vehicle_mileage_enabled = lambda: True
            selenium_local_module.apply_tile = lambda _driver, _tile_name: None
            selenium_local_module._set_window_size_if_enabled = lambda _driver, _site_key: None
            with tempfile.TemporaryDirectory() as tmp:
                result = selenium_local_module.run_vehicle_mileage_task(
                    request,
                    Path(tmp),
                    existing_driver=FakeDriver(),
                    use_session_lock=False,
                )
        finally:
            selenium_local_module._open_vehicle_mileage_page = original_open
            selenium_local_module._save_vehicle_mileage_enabled = original_save_enabled
            selenium_local_module.apply_tile = original_apply_tile
            selenium_local_module._set_window_size_if_enabled = original_set_window

        self.assertEqual(result.status, "vehicle_mileage_saved")
        self.assertEqual(result.detail, "mileage ok")
        self.assertIs(opened["request"], request)

    def test_resolve_end_mileage_accepts_delta(self):
        self.assertEqual(_resolve_end_mileage("123400", "+50"), "123450")
        self.assertEqual(_resolve_end_mileage("123400", "123456"), "123456")

    def test_vehicle_mileage_previous_request_reads_update_context(self):
        previous = {
            "task_id": "task-old",
            "created_at": "2026-06-15T08:00:00",
            "raw_text": "",
            "vehicle": "\u65b0\u576191",
            "mileage": "142842",
        }

        request = _vehicle_mileage_previous_request({"previous_task": previous})

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.vehicle, "\u65b0\u576191")
        self.assertEqual(request.mileage, "142842")

    def test_vehicle_mileage_values_keep_existing_start_mileage_for_update(self):
        request = AmbulanceReturnRequest(
            task_id="task-update",
            created_at=datetime(2026, 6, 15, 8, 0),
            raw_text="",
            case_date="2026/06/15",
            case_time="0830",
            return_date="2026/06/15",
            return_time="0910",
            mileage="142900",
            case_address="\u6843\u5712\u5e02\u89c0\u97f3\u5340",
            driver="\u66fe\u5f65\u7db8",
        )

        values = _vehicle_mileage_values(request, "142842")

        self.assertEqual(values["\u958b\u59cb\u91cc\u7a0b"], "142842")
        self.assertEqual(values["\u7d50\u675f\u91cc\u7a0b"], "142900")
        self.assertEqual(values["\u958b\u59cb\u6642\u9593"], "0830")

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

    def test_set_disinfection_query_date_does_not_dispatch_field_events(self):
        class FakeDriver:
            script = ""
            args = ()

            def execute_script(self, script: str, *args):
                self.script = script
                self.args = args
                return [True, True]

        driver = FakeDriver()

        _set_disinfection_query_date(driver, "2026-06-06")

        self.assertEqual(driver.args, ("2026-06-06 00:00:00", "2026-06-06 23:59:59"))
        self.assertNotIn("dispatchEvent", driver.script)

    def test_click_disinfection_query_uses_real_button_id(self):
        class FakeDriver:
            script = ""

            def execute_script(self, script: str):
                self.script = script
                return True

        driver = FakeDriver()

        self.assertTrue(_click_disinfection_query(driver))
        self.assertIn("_btnQuery", driver.script)

    def test_open_disinfection_detail_prefers_matching_vehicle_when_times_match(self):
        class FakeDriver:
            def __init__(self):
                self.clicked: list[int] = []

            def execute_script(self, script: str, *args):
                if "querySelectorAll('tr')" in script and "tr, index" in script:
                    return [
                        {"index": 0, "text": "2026/06/02 01:16:52 \u65b0\u576191"},
                        {"index": 1, "text": "2026/06/02 01:16:52 \u65b0\u576192"},
                    ]
                if args and isinstance(args[0], int):
                    self.clicked.append(args[0])
                    return True
                self.clicked.append(0)
                return True

        driver = FakeDriver()

        self.assertTrue(_open_disinfection_detail_for_case(driver, "0116", "\u65b0\u576192"))
        self.assertEqual(driver.clicked, [1])

    def test_assert_disinfection_not_login_raises_on_login_page(self):
        class FakeDriver:
            current_url = "https://emsdt.tyfd.gov.tw/EmmWeb/login"
            page_source = ""

        with self.assertRaises(Exception) as context:
            _assert_disinfection_not_login(FakeDriver(), "entry")

        self.assertIn("entry", str(context.exception))

    def test_site_task_defaults_do_not_use_legacy_chrome_profile(self):
        self.assertEqual(inspect.signature(run_local_selenium_task).parameters["profile_name"].default, "duty_work_log_profile")
        self.assertEqual(inspect.signature(run_vehicle_mileage_task).parameters["profile_name"].default, "vehicle_mileage_profile")
        self.assertEqual(inspect.signature(run_fuel_record_task).parameters["profile_name"].default, "fuel_record_profile")
        self.assertEqual(inspect.signature(run_disinfection_task).parameters["profile_name"].default, "disinfection_profile")

    def test_named_profile_uses_configured_runtime_profile_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("CHROME_PROFILE_DIR")
            previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            try:
                os.environ.pop("CHROME_PROFILE_DIR", None)
                os.environ["SELENIUM_PROFILE_ROOT"] = str(Path(tmp) / "runtime_profiles")

                self.assertEqual(_profile_dir("duty_work_log_profile"), Path(tmp) / "runtime_profiles" / "duty_work_log_profile")
                self.assertEqual(_profile_dir("vehicle_mileage_profile_task1"), Path(tmp) / "runtime_profiles" / "vehicle_mileage_profile_task1")
            finally:
                if previous is None:
                    os.environ.pop("CHROME_PROFILE_DIR", None)
                else:
                    os.environ["CHROME_PROFILE_DIR"] = previous
                if previous_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_root

    def test_legacy_chrome_profile_dir_is_only_used_as_profile_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_profile = os.environ.get("CHROME_PROFILE_DIR")
            previous_root = os.environ.get("AMBULANCE_TEST_PROFILE_ROOT")
            previous_runtime_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            try:
                os.environ["AMBULANCE_TEST_PROFILE_ROOT"] = tmp
                os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                os.environ["CHROME_PROFILE_DIR"] = r"%AMBULANCE_TEST_PROFILE_ROOT%\legacy_chrome_data"

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
                if previous_runtime_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_runtime_root

    def test_cleanup_stale_selenium_profiles_removes_only_generated_old_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_generated = [
                root / "chrome_profile",
                root / "case_lookup_profile_123",
                root / "disinfection_profile_task1",
            ]
            keep_unknown = root / "notes"
            keep_recent = root / "vehicle_mileage_profile_recent"
            keep_locked = root / "consumables_profile_old"
            keep_locked_main = root / "chrome_profile_locked"
            for path in [*old_generated, keep_unknown, keep_recent, keep_locked, keep_locked_main]:
                path.mkdir()
                (path / "cache.dat").write_text("x", encoding="utf-8")
            (keep_locked / "SingletonLock").write_text("", encoding="utf-8")
            (keep_locked_main / "SingletonLock").write_text("", encoding="utf-8")
            old_time = time.time() - 7200
            for path in [*old_generated, keep_unknown, keep_locked, keep_locked_main]:
                os.utime(path / "cache.dat", (old_time, old_time))
                os.utime(path, (old_time, old_time))

            removed = cleanup_stale_selenium_profiles(root, max_age_hours=1)

            self.assertEqual({path.name for path in removed}, {"chrome_profile", "case_lookup_profile_123", "disinfection_profile_task1"})
            for path in old_generated:
                self.assertFalse(path.exists())
            self.assertTrue(keep_unknown.exists())
            self.assertTrue(keep_recent.exists())
            self.assertTrue(keep_locked.exists())
            self.assertTrue(keep_locked_main.exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locked_main = root / "chrome_profile"
            locked_main.mkdir()
            (locked_main / "cache.dat").write_text("x", encoding="utf-8")
            (locked_main / "SingletonLock").write_text("", encoding="utf-8")
            old_time = time.time() - 7200
            os.utime(locked_main / "cache.dat", (old_time, old_time))
            os.utime(locked_main, (old_time, old_time))

            removed = cleanup_stale_selenium_profiles(root, max_age_hours=1)

            self.assertEqual(removed, [])
            self.assertTrue(locked_main.exists())

    def test_create_driver_cleans_stale_profiles_before_starting_chrome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            root.mkdir()
            old_profile = root / "case_lookup_profile_123"
            old_profile.mkdir()
            (old_profile / "cache.dat").write_text("x", encoding="utf-8")
            old_time = time.time() - 7200
            os.utime(old_profile / "cache.dat", (old_time, old_time))
            os.utime(old_profile, (old_time, old_time))
            previous_profile = os.environ.get("CHROME_PROFILE_DIR")
            previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            previous_max_age = os.environ.get("SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS")
            original_create = selenium_local_module._create_local_driver_with_retry

            class FakeDriver:
                def set_page_load_timeout(self, timeout):
                    self.page_timeout = timeout

                def set_script_timeout(self, timeout):
                    self.script_timeout = timeout

            try:
                os.environ.pop("CHROME_PROFILE_DIR", None)
                os.environ["SELENIUM_PROFILE_ROOT"] = str(root)
                os.environ["SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS"] = "1"
                selenium_local_module._create_local_driver_with_retry = lambda options: FakeDriver()

                driver = _create_driver(Path(tmp), profile_name="disinfection_profile_task-new", headless=True)
            finally:
                selenium_local_module._create_local_driver_with_retry = original_create
                if previous_profile is None:
                    os.environ.pop("CHROME_PROFILE_DIR", None)
                else:
                    os.environ["CHROME_PROFILE_DIR"] = previous_profile
                if previous_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_root
                if previous_max_age is None:
                    os.environ.pop("SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS", None)
                else:
                    os.environ["SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS"] = previous_max_age

            self.assertIsNotNone(driver)
            self.assertFalse(old_profile.exists())
            self.assertTrue((root / "disinfection_profile_task-new").exists())

    def test_create_driver_schedules_auto_close_for_local_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            previous_profile = os.environ.get("CHROME_PROFILE_DIR")
            previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            previous_delay = os.environ.get("WORKER_BROWSER_AUTO_CLOSE_SECONDS")
            original_create = selenium_local_module._create_local_driver_with_retry
            original_schedule = selenium_local_module.schedule_driver_auto_close
            scheduled = []

            class FakeDriver:
                def set_page_load_timeout(self, timeout):
                    self.page_timeout = timeout

                def set_script_timeout(self, timeout):
                    self.script_timeout = timeout

            try:
                os.environ.pop("CHROME_PROFILE_DIR", None)
                os.environ["SELENIUM_PROFILE_ROOT"] = str(root)
                os.environ["WORKER_BROWSER_AUTO_CLOSE_SECONDS"] = "600"
                selenium_local_module._create_local_driver_with_retry = lambda options: FakeDriver()
                selenium_local_module.schedule_driver_auto_close = lambda driver, label="Chrome": scheduled.append((driver, label))

                driver = _create_driver(Path(tmp), profile_name="vehicle_mileage_profile_task1", headless=False)
            finally:
                selenium_local_module._create_local_driver_with_retry = original_create
                selenium_local_module.schedule_driver_auto_close = original_schedule
                if previous_profile is None:
                    os.environ.pop("CHROME_PROFILE_DIR", None)
                else:
                    os.environ["CHROME_PROFILE_DIR"] = previous_profile
                if previous_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_root
                if previous_delay is None:
                    os.environ.pop("WORKER_BROWSER_AUTO_CLOSE_SECONDS", None)
                else:
                    os.environ["WORKER_BROWSER_AUTO_CLOSE_SECONDS"] = previous_delay

            self.assertEqual(scheduled, [(driver, "vehicle_mileage_profile_task1")])

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

    def test_ensure_ppe_vehicle_mileage_session_retries_when_login_returns_to_login_page(self):
        class FakeInput:
            def __init__(self):
                self.values: list[str] = []
                self.history: list[str] = []

            def clear(self):
                self.values.clear()

            def send_keys(self, value: str):
                self.values.append(value)
                self.history.append(value)

        class FakeDriver:
            def __init__(self):
                self.get_calls: list[str] = []
                self.inputs = {"Account": FakeInput(), "Password": FakeInput()}

            def get(self, url: str):
                self.get_calls.append(url)

            def find_element(self, by, value: str):
                return self.inputs[value]

        original_credentials = selenium_local_module._ppe_credential_attempts
        original_wait = selenium_local_module._wait_for_ppe_vehicle_mileage_page
        original_wait_login = selenium_local_module._wait_for_ppe_login_result
        original_is_login = selenium_local_module._is_ppe_login_page
        original_click = selenium_local_module._click_ppe_login
        original_sleep = selenium_local_module.time.sleep
        try:
            wait_results = iter([False, True])
            click_count = {"value": 0}
            selenium_local_module._ppe_credential_attempts = lambda request=None: [("tyfd00008", "pass")]
            selenium_local_module._wait_for_ppe_vehicle_mileage_page = lambda driver, timeout=12: next(wait_results)
            selenium_local_module._wait_for_ppe_login_result = lambda driver, timeout=12: True
            selenium_local_module._is_ppe_login_page = lambda driver: True
            selenium_local_module._click_ppe_login = lambda driver: click_count.__setitem__("value", click_count["value"] + 1)
            selenium_local_module.time.sleep = lambda seconds: None

            driver = FakeDriver()
            result = _ensure_ppe_vehicle_mileage_session(driver)
        finally:
            selenium_local_module._ppe_credential_attempts = original_credentials
            selenium_local_module._wait_for_ppe_vehicle_mileage_page = original_wait
            selenium_local_module._wait_for_ppe_login_result = original_wait_login
            selenium_local_module._is_ppe_login_page = original_is_login
            selenium_local_module._click_ppe_login = original_click
            selenium_local_module.time.sleep = original_sleep

        self.assertTrue(result)
        self.assertEqual(len(driver.get_calls), 2)
        self.assertEqual(click_count["value"], 1)
        self.assertEqual(driver.inputs["Account"].values, ["tyfd00008"])
        self.assertEqual(driver.inputs["Password"].values, ["pass"])

    def test_ppe_session_tries_driver_personnel_then_synced_account(self):
        class FakeInput:
            def __init__(self):
                self.values: list[str] = []
                self.history: list[str] = []

            def clear(self):
                self.values.clear()

            def send_keys(self, value: str):
                self.values.append(value)
                self.history.append(value)

        class FakeDriver:
            def __init__(self):
                self.get_calls: list[str] = []
                self.inputs = {"Account": FakeInput(), "Password": FakeInput()}

            def get(self, url: str):
                self.get_calls.append(url)

            def find_element(self, by, value: str):
                return self.inputs[value]

        previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        request = AmbulanceReturnRequest(
            task_id="task-ppe-fallback",
            created_at=datetime.now(),
            raw_text="",
            driver="司機甲",
            personnel=["司機甲", "出勤乙"],
            personnel_accounts=["tyfd01111", "tyfd03333"],
        )
        original_wait = selenium_local_module._wait_for_ppe_vehicle_mileage_page
        original_wait_login = selenium_local_module._wait_for_ppe_login_result
        original_is_login = selenium_local_module._is_ppe_login_page
        original_click = selenium_local_module._click_ppe_login
        original_sleep = selenium_local_module.time.sleep
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_duty_automation_credentials(
                    [
                        {"actor_no": "11", "name": "司機甲", "user_id": "tyfd01111", "password": "driver-pass"},
                        {"actor_no": "33", "name": "出勤乙", "user_id": "tyfd03333", "password": "personnel-pass"},
                        {"actor_no": "22", "name": "同步員", "user_id": "tyfd02222", "password": "synced-pass"},
                    ],
                    last_selected="tyfd02222",
                )
                page_checks = iter([False, False, False, False, False, True])
                login_results = iter([False, False, True])
                selenium_local_module._wait_for_ppe_vehicle_mileage_page = lambda driver, timeout=12: next(page_checks)
                selenium_local_module._wait_for_ppe_login_result = lambda driver, timeout=12: next(login_results)
                selenium_local_module._is_ppe_login_page = lambda driver: True
                selenium_local_module._click_ppe_login = lambda driver: None
                selenium_local_module.time.sleep = lambda seconds: None

                driver = FakeDriver()
                result = _ensure_ppe_vehicle_mileage_session(driver, request)
        finally:
            selenium_local_module._wait_for_ppe_vehicle_mileage_page = original_wait
            selenium_local_module._wait_for_ppe_login_result = original_wait_login
            selenium_local_module._is_ppe_login_page = original_is_login
            selenium_local_module._click_ppe_login = original_click
            selenium_local_module.time.sleep = original_sleep
            if previous_path is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
            if previous_override is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertTrue(result)
        self.assertEqual(driver.inputs["Account"].history, ["tyfd01111", "tyfd03333", "tyfd02222"])
        self.assertEqual(driver.inputs["Password"].history, ["driver-pass", "personnel-pass", "synced-pass"])
        self.assertEqual(
            driver.get_calls,
            [
                "https://ppe.tyfd.gov.tw/CarRecord/List",
                "https://ppe.tyfd.gov.tw/CarRecord/List",
                "https://ppe.tyfd.gov.tw/CarRecord/List",
                "https://ppe.tyfd.gov.tw/CarRecord/List",
            ],
        )

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
            selenium_local_module._ensure_duty_login = lambda driver, preferred_user_ids=None: False
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

    def test_headless_driver_uses_default_arg_when_env_is_blank(self):
        class FakeDriver:
            def set_page_load_timeout(self, seconds: int) -> None:
                pass

            def set_script_timeout(self, seconds: int) -> None:
                pass

        calls = []
        original_create = selenium_local_module._create_local_driver_with_retry
        original_headless_arg = os.environ.get("SELENIUM_HEADLESS_ARG")
        try:
            os.environ["SELENIUM_HEADLESS_ARG"] = " "
            selenium_local_module._create_local_driver_with_retry = lambda options: calls.append(options) or FakeDriver()

            with tempfile.TemporaryDirectory() as tmp:
                selenium_local_module._create_driver(Path(tmp), profile_name="case_lookup_profile_test", headless=True)
        finally:
            selenium_local_module._create_local_driver_with_retry = original_create
            if original_headless_arg is None:
                os.environ.pop("SELENIUM_HEADLESS_ARG", None)
            else:
                os.environ["SELENIUM_HEADLESS_ARG"] = original_headless_arg

        arguments = list(getattr(calls[0], "arguments", []))
        self.assertIn("--headless=new", arguments)
        self.assertNotIn("", arguments)

    def test_headless_driver_keeps_headless_when_env_arg_is_not_headless(self):
        class FakeDriver:
            def set_page_load_timeout(self, seconds: int) -> None:
                pass

            def set_script_timeout(self, seconds: int) -> None:
                pass

        calls = []
        original_create = selenium_local_module._create_local_driver_with_retry
        original_headless_arg = os.environ.get("SELENIUM_HEADLESS_ARG")
        try:
            os.environ["SELENIUM_HEADLESS_ARG"] = "--disable-gpu"
            selenium_local_module._create_local_driver_with_retry = lambda options: calls.append(options) or FakeDriver()

            with tempfile.TemporaryDirectory() as tmp:
                selenium_local_module._create_driver(Path(tmp), profile_name="case_lookup_profile_test", headless=True)
        finally:
            selenium_local_module._create_local_driver_with_retry = original_create
            if original_headless_arg is None:
                os.environ.pop("SELENIUM_HEADLESS_ARG", None)
            else:
                os.environ["SELENIUM_HEADLESS_ARG"] = original_headless_arg

        arguments = list(getattr(calls[0], "arguments", []))
        self.assertIn("--headless=new", arguments)
        self.assertIn("--disable-gpu", arguments)

    def test_local_driver_retries_when_chrome_is_not_reachable(self):
        class FakeDriver:
            pass

        calls: dict[str, object] = {"count": 0, "sleep": []}
        original_webdriver_chrome = selenium_local_module.webdriver.Chrome
        original_sleep = selenium_local_module.time.sleep
        original_cleanup = selenium_local_module.cleanup_worker_chrome_residue
        original_attempts = os.environ.get("SELENIUM_LOCAL_SESSION_ATTEMPTS")
        cleanups = []
        options = object()
        try:
            os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = "2"

            def fake_chrome(options=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise selenium_local_module.WebDriverException("session not created: from chrome not reachable")
                return FakeDriver()

            selenium_local_module.webdriver.Chrome = fake_chrome
            selenium_local_module.time.sleep = lambda seconds: calls["sleep"].append(seconds)
            selenium_local_module.cleanup_worker_chrome_residue = lambda opts, label="Chrome": cleanups.append((opts, label)) or 1

            result = _create_local_driver_with_retry(options)
        finally:
            selenium_local_module.webdriver.Chrome = original_webdriver_chrome
            selenium_local_module.time.sleep = original_sleep
            selenium_local_module.cleanup_worker_chrome_residue = original_cleanup
            if original_attempts is None:
                os.environ.pop("SELENIUM_LOCAL_SESSION_ATTEMPTS", None)
            else:
                os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = original_attempts

        self.assertIsInstance(result, FakeDriver)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(calls["sleep"], [2])
        self.assertEqual(cleanups, [(options, "local selenium")])

    def test_local_driver_cleans_failed_user_data_dir_before_retry(self):
        class FakeDriver:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            calls: dict[str, object] = {"count": 0, "sleep": []}
            profile_cleanups: list[tuple[Path, ...]] = []
            user_data_dir = Path(tmp) / "profiles" / "case_lookup_profile_test"
            options = selenium_local_module.Options()
            options.add_argument(f"--user-data-dir={user_data_dir}")
            original_webdriver_chrome = selenium_local_module.webdriver.Chrome
            original_sleep = selenium_local_module.time.sleep
            original_cleanup = selenium_local_module.cleanup_worker_chrome_residue
            original_profile_cleanup = selenium_local_module.cleanup_runtime_profiles_for_startup_failure
            original_attempts = os.environ.get("SELENIUM_LOCAL_SESSION_ATTEMPTS")
            try:
                os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = "2"

                def fake_chrome(options=None):
                    calls["count"] += 1
                    if calls["count"] == 1:
                        raise selenium_local_module.WebDriverException("session not created: from chrome not reachable")
                    return FakeDriver()

                selenium_local_module.webdriver.Chrome = fake_chrome
                selenium_local_module.time.sleep = lambda seconds: calls["sleep"].append(seconds)
                selenium_local_module.cleanup_worker_chrome_residue = lambda opts, label="Chrome": 0
                selenium_local_module.cleanup_runtime_profiles_for_startup_failure = (
                    lambda dirs: profile_cleanups.append(tuple(Path(path) for path in dirs)) or []
                )

                result = _create_local_driver_with_retry(options)
            finally:
                selenium_local_module.webdriver.Chrome = original_webdriver_chrome
                selenium_local_module.time.sleep = original_sleep
                selenium_local_module.cleanup_worker_chrome_residue = original_cleanup
                selenium_local_module.cleanup_runtime_profiles_for_startup_failure = original_profile_cleanup
                if original_attempts is None:
                    os.environ.pop("SELENIUM_LOCAL_SESSION_ATTEMPTS", None)
                else:
                    os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = original_attempts

        self.assertIsInstance(result, FakeDriver)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(profile_cleanups, [(user_data_dir,)])

    def test_local_driver_recovers_when_chrome_start_times_out(self):
        class FakeDriver:
            pass

        calls: dict[str, object] = {"count": 0, "sleep": []}
        original_webdriver_chrome = selenium_local_module.webdriver.Chrome
        original_sleep = selenium_local_module.time.sleep
        original_cleanup = selenium_local_module.cleanup_worker_chrome_residue
        original_attempts = os.environ.get("SELENIUM_LOCAL_SESSION_ATTEMPTS")
        original_timeout = os.environ.get("SELENIUM_CHROME_START_TIMEOUT_SECONDS")
        cleanups = []
        options = object()
        try:
            os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = "2"
            os.environ["SELENIUM_CHROME_START_TIMEOUT_SECONDS"] = "0.01"

            def fake_chrome(options=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    original_sleep(0.05)
                return FakeDriver()

            selenium_local_module.webdriver.Chrome = fake_chrome
            selenium_local_module.time.sleep = lambda seconds: calls["sleep"].append(seconds)
            selenium_local_module.cleanup_worker_chrome_residue = lambda opts, label="Chrome": cleanups.append((opts, label)) or 0

            result = _create_local_driver_with_retry(options)
        finally:
            selenium_local_module.webdriver.Chrome = original_webdriver_chrome
            selenium_local_module.time.sleep = original_sleep
            selenium_local_module.cleanup_worker_chrome_residue = original_cleanup
            if original_attempts is None:
                os.environ.pop("SELENIUM_LOCAL_SESSION_ATTEMPTS", None)
            else:
                os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = original_attempts
            if original_timeout is None:
                os.environ.pop("SELENIUM_CHROME_START_TIMEOUT_SECONDS", None)
            else:
                os.environ["SELENIUM_CHROME_START_TIMEOUT_SECONDS"] = original_timeout

        self.assertIsInstance(result, FakeDriver)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(cleanups, [(options, "local selenium")])

    def test_local_driver_retries_oserror_invalid_argument(self):
        class FakeDriver:
            pass

        calls: dict[str, object] = {"count": 0, "sleep": []}
        original_webdriver_chrome = selenium_local_module.webdriver.Chrome
        original_sleep = selenium_local_module.time.sleep
        original_cleanup = selenium_local_module.cleanup_worker_chrome_residue
        original_attempts = os.environ.get("SELENIUM_LOCAL_SESSION_ATTEMPTS")
        cleanups = []
        options = object()
        try:
            os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = "2"

            def fake_chrome(options=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError(22, "Invalid argument")
                return FakeDriver()

            selenium_local_module.webdriver.Chrome = fake_chrome
            selenium_local_module.time.sleep = lambda seconds: calls["sleep"].append(seconds)
            selenium_local_module.cleanup_worker_chrome_residue = lambda opts, label="Chrome": cleanups.append((opts, label)) or 1

            result = _create_local_driver_with_retry(options)
        finally:
            selenium_local_module.webdriver.Chrome = original_webdriver_chrome
            selenium_local_module.time.sleep = original_sleep
            selenium_local_module.cleanup_worker_chrome_residue = original_cleanup
            if original_attempts is None:
                os.environ.pop("SELENIUM_LOCAL_SESSION_ATTEMPTS", None)
            else:
                os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = original_attempts

        self.assertIsInstance(result, FakeDriver)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(calls["sleep"], [2])
        self.assertEqual(cleanups, [(options, "local selenium")])

    def test_local_chrome_startup_error_treats_no_space_left_as_retryable(self):
        self.assertTrue(
            selenium_local_module._is_local_chrome_startup_error(
                OSError(28, "No space left on device")
            )
        )

    def test_case_lookup_closes_driver_after_success(self):
        class FakeDriver:
            def implicitly_wait(self, seconds: int) -> None:
                pass

        calls: dict[str, object] = {"released": False}
        original_create_driver = selenium_local_module._create_driver
        original_acquire = selenium_local_module._acquire_selenium_session
        original_release = selenium_local_module._release_selenium_session
        original_quit = selenium_local_module._quit_driver
        original_ensure_login = selenium_local_module._ensure_duty_login
        original_set_window = selenium_local_module._set_window_size_if_enabled
        original_open_case_query = selenium_local_module._open_case_query
        original_extract_cases = selenium_local_module._extract_all_emergency_cases
        original_attach_details = selenium_local_module._attach_case_form_details
        original_save_artifacts = selenium_local_module._save_artifacts
        try:
            fake_driver = FakeDriver()

            def fake_create_driver(*args, **kwargs):
                calls["create_kwargs"] = kwargs
                return fake_driver

            selenium_local_module._create_driver = fake_create_driver
            selenium_local_module._acquire_selenium_session = lambda reason: True
            selenium_local_module._release_selenium_session = lambda reason: calls.__setitem__("released", True)
            selenium_local_module._quit_driver = lambda driver: calls.__setitem__("quit_driver", driver)
            selenium_local_module._ensure_duty_login = lambda driver, preferred_user_ids=None: True
            selenium_local_module._set_window_size_if_enabled = lambda *args, **kwargs: None
            selenium_local_module._open_case_query = lambda *args, **kwargs: None
            selenium_local_module._extract_all_emergency_cases = lambda driver: [{"case_id": "case-1"}]
            selenium_local_module._attach_case_form_details = lambda driver, cases, artifacts_dir, previous_cases, deadline=None: cases
            selenium_local_module._save_artifacts = lambda *args, **kwargs: None

            with tempfile.TemporaryDirectory() as tmp:
                result = selenium_local_module.query_duty_emergency_cases(Path(tmp), lookup_range="24h")
        finally:
            selenium_local_module._create_driver = original_create_driver
            selenium_local_module._acquire_selenium_session = original_acquire
            selenium_local_module._release_selenium_session = original_release
            selenium_local_module._quit_driver = original_quit
            selenium_local_module._ensure_duty_login = original_ensure_login
            selenium_local_module._set_window_size_if_enabled = original_set_window
            selenium_local_module._open_case_query = original_open_case_query
            selenium_local_module._extract_all_emergency_cases = original_extract_cases
            selenium_local_module._attach_case_form_details = original_attach_details
            selenium_local_module._save_artifacts = original_save_artifacts

        self.assertEqual(result.status, "cases_loaded")
        self.assertEqual(result.detail, "已查到 1 筆 24 小時內案件，並讀取出勤人員。")
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

    def test_site_specific_save_controls_use_real_button_signatures(self):
        class FakeDriver:
            def __init__(self, result: bool):
                self.result = result
                self.scripts: list[str] = []

            def execute_script(self, script: str):
                self.scripts.append(script)
                return self.result

        mileage_driver = FakeDriver(True)
        disinfection_driver = FakeDriver(True)

        self.assertTrue(_click_vehicle_mileage_save(mileage_driver))
        self.assertTrue(_click_disinfection_save(disinfection_driver))
        self.assertIn("SaveData", mileage_driver.scripts[0])
        self.assertIn("_btnSave", disinfection_driver.scripts[0])

    def test_fuel_record_save_never_uses_submit_action(self):
        source = Path(selenium_local_module.__file__).read_text(encoding="utf-8")

        self.assertIn("SaveData('save')", source)
        self.assertNotIn("SaveData('submit')", source)

    def test_fuel_card_register_uses_plate_first_and_clicks_register_button(self):
        class FakeDriver:
            def __init__(self):
                self.labels = None

            def execute_script(self, script: str, labels: list[str]):
                self.labels = labels
                self.script = script
                return {"clicked": True, "rowMatched": True}

        driver = FakeDriver()

        self.assertEqual(_fuel_card_labels("新坡91"), ["BGV-2310", "新坡91"])
        _click_fuel_card_register(driver, _fuel_card_labels("新坡91"))

        self.assertEqual(driver.labels, ["BGV-2310", "新坡91"])
        self.assertIn("登錄", driver.script)
        self.assertIn("送出審核", driver.script)

    def test_fuel_query_period_switches_to_task_month(self):
        class FakeDriver:
            def __init__(self):
                self.period = "2026/06"
                self.scripts: list[str] = []
                self.clicked_query = False

            def execute_script(self, script: str, *args):
                self.scripts.append(script)
                if "FuelUseYM" in script and "clickQuery" in script:
                    self.period = args[0]
                    self.clicked_query = True
                    return {"changed": True, "clicked": True, "value": self.period}
                if "FuelUseYM" in script:
                    return self.period
                return None

        driver = FakeDriver()

        self.assertEqual(_ensure_fuel_query_period(driver, "2026/07"), "2026/07")
        self.assertTrue(driver.clicked_query)

    def test_fuel_query_period_keeps_matching_month(self):
        class FakeDriver:
            def __init__(self):
                self.period = "2026/07"
                self.scripts: list[str] = []

            def execute_script(self, script: str, *args):
                self.scripts.append(script)
                return self.period

        driver = FakeDriver()

        self.assertEqual(_ensure_fuel_query_period(driver, "2026/07"), "2026/07")
        self.assertEqual(_fuel_query_period(driver), "2026/07")

    def test_fuel_record_detail_page_rejects_query_page(self):
        class FakeDriver:
            def __init__(self, path: str, ready: bool = True):
                self.path = path
                self.ready = ready
                self.script = ""

            def execute_script(self, script: str):
                self.script = script
                return self.path == "/FUC04100/Detail" and self.ready

        query_driver = FakeDriver("/FUC04100/Query")
        detail_driver = FakeDriver("/FUC04100/Detail")
        loading_detail_driver = FakeDriver("/FUC04100/Detail", ready=False)

        self.assertFalse(selenium_local_module._is_ppe_fuel_record_detail_page(query_driver))
        self.assertTrue(selenium_local_module._is_ppe_fuel_record_detail_page(detail_driver))
        self.assertFalse(selenium_local_module._is_ppe_fuel_record_detail_page(loading_detail_driver))
        self.assertIn("/FUC04100/Detail", detail_driver.script)

    def test_fuel_register_waits_for_detail_page_before_filling_grid(self):
        source = Path(selenium_local_module.__file__).read_text(encoding="utf-8")

        self.assertIn("if not _wait_for_ppe_fuel_record_detail_page(driver, timeout=12):", source)

    def test_duty_work_log_login_uses_personnel_accounts(self):
        class FakeDriver:
            pass

        captured: dict[str, object] = {}
        original_ensure_login = selenium_local_module._ensure_duty_login
        original_save_artifacts = selenium_local_module._save_artifacts
        try:
            selenium_local_module._ensure_duty_login = (
                lambda driver, preferred_user_ids=None: captured.__setitem__("preferred", preferred_user_ids) or False
            )
            selenium_local_module._save_artifacts = lambda *args, **kwargs: None
            request = AmbulanceReturnRequest(
                task_id="task-1",
                created_at=datetime(2026, 6, 7, 1, 0),
                raw_text="",
                driver="Bob",
                personnel=["Alice", "Bob", "Carol"],
                personnel_accounts=["B123017532", "tyfd00008", "tyfd00009"],
            )

            result = _prepare_duty_work_log_form(FakeDriver(), request, Path("."), Path("task.txt"))
        finally:
            selenium_local_module._ensure_duty_login = original_ensure_login
            selenium_local_module._save_artifacts = original_save_artifacts

        self.assertEqual(result.status, "needs_duty_login")
        self.assertEqual(captured["preferred"], ["tyfd00008", "B123017532", "tyfd00009"])

    def test_duty_login_credential_attempts_use_driver_personnel_then_synced_account(self):
        previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_duty_automation_credentials(
                    [
                        {"actor_no": "1", "name": "Alice", "user_id": "tyfd00001", "password": "pass1"},
                        {"actor_no": "2", "name": "Bob", "user_id": "tyfd00002", "password": "pass2"},
                        {"actor_no": "3", "name": "Carol", "user_id": "tyfd00003", "password": "pass3"},
                        {"actor_no": "8", "name": "Sync", "user_id": "tyfd01510", "password": "pass8"},
                    ],
                    last_selected="tyfd01510",
                )
                request = AmbulanceReturnRequest(
                    task_id="task-1",
                    created_at=datetime(2026, 6, 7, 1, 0),
                    raw_text="",
                    driver="Bob",
                    personnel=["Alice", "Bob", "Carol"],
                    personnel_accounts=["tyfd00001", "tyfd00002", "tyfd00003"],
                )

                attempts = _duty_login_credential_attempts(request.duty_login_account_candidates)
        finally:
            if previous_path is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
            if previous_override is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertEqual([credential.user_id for credential in attempts], ["tyfd00002", "tyfd00001", "tyfd00003", "tyfd01510"])

    def test_case_lookup_login_uses_latest_synced_duty_account_before_fixed_sync_account(self):
        class FakeDriver:
            def implicitly_wait(self, seconds):
                self.wait_seconds = seconds

        previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        previous_account = os.environ.get("DUTY_ACCOUNT")
        previous_password = os.environ.get("DUTY_PASSWORD")
        captured: dict[str, object] = {}
        original_acquire = selenium_local_module._acquire_selenium_session
        original_release = selenium_local_module._release_selenium_session
        original_create_driver = selenium_local_module._create_driver
        original_timeouts = selenium_local_module._set_case_lookup_driver_timeouts
        original_window = selenium_local_module._set_window_size_if_enabled
        original_ensure_login = selenium_local_module._ensure_duty_login
        original_login_error = selenium_local_module._login_error_text
        original_save_artifacts = selenium_local_module._save_artifacts
        original_quit_driver = selenium_local_module._quit_driver
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_credential_sync_payload(
                    {
                        "actor_no": "12",
                        "user_id": "tyfd01212",
                        "accounts": [
                            {"actor_no": "19", "name": "司機甲", "user_id": "tyfd01919", "password": "pass19"},
                            {"actor_no": "12", "name": "出勤乙", "user_id": "tyfd01212", "password": "pass12"},
                            {"actor_no": "8", "name": "固定同步", "user_id": "tyfd01510", "password": "pass8"},
                        ],
                    }
                )
                selenium_local_module._acquire_selenium_session = lambda owner: True
                selenium_local_module._release_selenium_session = lambda owner: None
                selenium_local_module._create_driver = lambda *args, **kwargs: FakeDriver()
                selenium_local_module._set_case_lookup_driver_timeouts = lambda driver: None
                selenium_local_module._set_window_size_if_enabled = lambda driver, site_key: None
                selenium_local_module._login_error_text = lambda driver: ""
                selenium_local_module._save_artifacts = lambda *args, **kwargs: None
                selenium_local_module._quit_driver = lambda driver: None

                def fake_ensure_login(driver, preferred_user_ids=None):
                    captured["preferred"] = preferred_user_ids
                    return False

                selenium_local_module._ensure_duty_login = fake_ensure_login

                result = selenium_local_module.query_duty_emergency_cases(Path(tmp), lookup_range="24h")
        finally:
            selenium_local_module._acquire_selenium_session = original_acquire
            selenium_local_module._release_selenium_session = original_release
            selenium_local_module._create_driver = original_create_driver
            selenium_local_module._set_case_lookup_driver_timeouts = original_timeouts
            selenium_local_module._set_window_size_if_enabled = original_window
            selenium_local_module._ensure_duty_login = original_ensure_login
            selenium_local_module._login_error_text = original_login_error
            selenium_local_module._save_artifacts = original_save_artifacts
            selenium_local_module._quit_driver = original_quit_driver
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

        self.assertEqual(captured["preferred"], ["tyfd01212", "tyfd01510"])
        self.assertEqual(result.status, "needs_duty_login")

    def test_id_number_from_cases_matches_synced_actor_and_name(self):
        credential = DutyCredential(
            user_id="tyfd01510",
            password="pass8",
            actor_no="8",
            name="曾彥綸",
        )
        cases = [
            {
                "personnel_hidden_raw": "21番 張家和 S124774209\n8番 曾彥綸 B123017532",
                "personnel_raw": "張家和、曾彥綸",
            }
        ]

        self.assertEqual(_id_number_from_cases_for_credential(cases, credential), "B123017532")

    def test_lookup_synced_credential_id_number_updates_saved_account_from_case_lookup(self):
        previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        original_query = selenium_local_module.query_duty_emergency_cases
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_duty_automation_credentials(
                    [
                        {
                            "actor_no": "8",
                            "name": "曾彥綸",
                            "user_id": "tyfd01510",
                            "password": "pass8",
                        }
                    ],
                    last_selected="8",
                )
                selenium_local_module.query_duty_emergency_cases = lambda artifacts_dir, lookup_range="24h": SimpleNamespace(
                    ok=True,
                    status="cases_loaded",
                    detail="loaded",
                    cases=[
                        {
                            "personnel_hidden_raw": "8番 曾彥綸 B123017532",
                            "personnel_raw": "曾彥綸",
                        }
                    ],
                    path=Path(tmp) / "cases" / "latest.json",
                )

                result = lookup_synced_credential_id_number(Path(tmp))
                selected = load_synced_worker_credential()
        finally:
            selenium_local_module.query_duty_emergency_cases = original_query
            if previous_path is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
            if previous_override is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertTrue(result.ok)
        self.assertEqual(result.id_number, "B123017532")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.id_number, "B123017532")

    def test_duty_login_tries_next_candidate_after_failure(self):
        class FakeDriver:
            def get(self, url):
                self.url = url

        attempted: list[str] = []
        original_attempts = selenium_local_module._duty_login_credential_attempts
        original_attempt_login = selenium_local_module._attempt_duty_login
        original_looks_logged_in = selenium_local_module._looks_logged_in
        original_sleep = selenium_local_module.time.sleep
        try:
            selenium_local_module._looks_logged_in = lambda driver: False
            selenium_local_module.time.sleep = lambda seconds: None
            selenium_local_module._duty_login_credential_attempts = lambda preferred_user_ids=None: [
                DutyCredential("driver", "driver-pass"),
                DutyCredential("other", "other-pass"),
                DutyCredential("sync", "sync-pass"),
            ]
            selenium_local_module._attempt_duty_login = (
                lambda driver, credential: attempted.append(credential.user_id) or credential.user_id == "sync"
            )

            result = _ensure_duty_login(FakeDriver(), ["driver", "other"])
        finally:
            selenium_local_module._duty_login_credential_attempts = original_attempts
            selenium_local_module._attempt_duty_login = original_attempt_login
            selenium_local_module._looks_logged_in = original_looks_logged_in
            selenium_local_module.time.sleep = original_sleep

        self.assertTrue(result)
        self.assertEqual(attempted, ["driver", "other", "sync"])

    def test_extract_emergency_cases_includes_fire_cases(self):
        class FakeDriver:
            def execute_script(self, script):
                self.script = script
                return [{"case_id": "20260610170000001", "category": "火災", "reason": "火災"}]

        driver = FakeDriver()
        cases = selenium_local_module._extract_emergency_cases(driver)

        self.assertEqual(cases[0]["category"], "火災")
        self.assertEqual(cases[0]["reason"], "火災")
        self.assertIn("includes('火災')", driver.script)

    def test_extract_emergency_cases_includes_salvaged_body_as_drowning(self):
        class FakeDriver:
            def execute_script(self, script):
                self.script = script
                return [{"case_id": "20260710170000001", "category": "其他-打撈浮屍", "reason": "溺水"}]

        driver = FakeDriver()
        cases = selenium_local_module._extract_emergency_cases(driver)

        self.assertEqual(cases[0]["category"], "其他-打撈浮屍")
        self.assertEqual(cases[0]["reason"], "溺水")
        self.assertIn("includes('其他-打撈浮屍')", driver.script)
        self.assertIn("isSalvagedBody ? '溺水'", driver.script)

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
