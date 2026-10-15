import os
import unittest
from unittest.mock import MagicMock, patch

from ambulance_bot import selenium_local as runtime
from tests.test_duty_case_pagination import _CasePageDriver


class EntrySpeedTests(unittest.TestCase):
    def test_duty_control_waits_for_late_button_without_fixed_sleep(self):
        driver = MagicMock()
        driver.execute_script.side_effect = [{"ok": False}, {"ok": True}]
        real_wait = runtime.WebDriverWait
        with patch.object(runtime, "WebDriverWait", side_effect=lambda d, t: real_wait(d, 0.1, poll_frequency=0.001)):
            runtime._click_by_text_or_id(driver, ["_btnInsert"], ["新增"])
        self.assertEqual(2, driver.execute_script.call_count)

    def test_duty_popup_can_open_after_first_window_check(self):
        real_wait = runtime.WebDriverWait
        with patch.object(runtime, "_try_switch_to_window_containing", side_effect=[False, True]), \
             patch.object(runtime, "WebDriverWait", side_effect=lambda d, t: real_wait(d, 0.1, poll_frequency=0.001)):
            runtime._switch_to_window_containing(MagicMock(), "_txtSDATE")

    def test_dry_run_low_level_save_controls_cannot_execute_browser_code(self):
        driver = MagicMock()
        with patch.dict(os.environ, {"AUTOMATION_DRY_RUN": "true"}):
            for save in (runtime._click_duty_work_log_save, runtime._click_save_control,
                         runtime._click_vehicle_mileage_save, runtime._click_disinfection_save):
                with self.subTest(save=save.__name__), self.assertRaises(runtime.WebDriverException):
                    save(driver)
            with self.assertRaises(runtime.WebDriverException):
                runtime._save_fuel_record_form(driver, MagicMock())
        driver.execute_script.assert_not_called()

    def test_explicit_dry_run_overrides_every_save_switch(self):
        with patch.dict(os.environ, {
            "AUTOMATION_DRY_RUN": "true",
            "SAVE_VEHICLE_MILEAGE": "true",
            "SAVE_FUEL_RECORD": "true",
            "SAVE_DUTY_WORK_LOG": "true",
            "SAVE_DISINFECTION_RECORD": "true",
        }):
            self.assertFalse(runtime._save_vehicle_mileage_enabled())
            self.assertFalse(runtime._save_fuel_record_enabled())
            self.assertFalse(runtime._save_duty_work_log_enabled())
            self.assertFalse(runtime._save_disinfection_record_enabled())

    def test_daily_mileage_waits_for_complete_history_before_reading(self):
        driver = MagicMock()
        order = []
        with patch.object(runtime, "_wait_for_ppe_vehicle_mileage_page", return_value=True), \
             patch.object(runtime, "_click_text_if_present"), \
             patch.object(runtime, "_select_daily_vehicle_mileage_month"), \
             patch.object(runtime, "_select_vehicle_record"), \
             patch.object(runtime, "_wait_for_vehicle_mileage_detail", create=True,
                          side_effect=lambda *a: order.append("ready")), \
             patch.object(runtime, "_extract_latest_vehicle_mileage_record",
                          side_effect=lambda *a: order.append("read") or {"mileage": "123"}), \
             patch.object(runtime.time, "sleep"):
            result = runtime._read_daily_vehicle_mileage_for_month(
                driver, {"ppe_name": "TEST-1234"}, "2026/10"
            )
        self.assertEqual(["ready", "read"], order)
        self.assertEqual("123", result["mileage"])

    def test_selected_vehicle_transfers_wait_to_detail_contract(self):
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()]
        with patch.object(runtime.time, "sleep") as sleep:
            runtime._select_vehicle_record(driver, "TEST-1234")
        driver.find_elements.return_value[0].click.assert_called_once()
        sleep.assert_not_called()

    def test_fuel_query_ready_response_does_not_sleep_first(self):
        driver = MagicMock()
        driver.execute_script.return_value = {"clicked": True}
        with patch.object(runtime, "_fuel_query_period", return_value="202610"), \
             patch.object(runtime, "_fuel_query_results_refreshed", return_value=True), \
             patch.object(runtime.time, "sleep") as sleep:
            runtime._ensure_fuel_query_period(driver, "202610")
        sleep.assert_not_called()

    def test_missing_case_on_stable_pages_is_not_repolled(self):
        driver = _CasePageDriver({1: ["20261001000100001"], 2: ["20261001000200001"]}, current_page=2)
        original_execute = driver.execute_script
        checks = []

        class PollBudgetWait:
            def __init__(self, current_driver, _timeout, **_options):
                self.driver = current_driver

            def until(self, predicate, message=""):
                # Each fake script starts Node; verify polls independently of host startup time.
                for _ in range(8):
                    result = predicate(self.driver)
                    if result:
                        return result
                raise runtime.TimeoutException(message)

        def execute(script, *args):
            if "const caseId" in script:
                checks.append(driver.current_page)
            return original_execute(script, *args)

        driver.execute_script = execute
        with patch.object(runtime, "WebDriverWait", PollBudgetWait):
            self.assertFalse(runtime._click_case_choose(driver, "20261001009900001"))
        self.assertEqual([2, 1, 2], checks)


if __name__ == "__main__":
    unittest.main()
