from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import shutil
import unittest
from unittest.mock import Mock, patch

import consumables_login
from ambulance_bot import selenium_local as runtime
from ambulance_bot.models import AmbulanceReturnRequest, VehicleEntry
from tests.test_duty_case_pagination import _CasePageDriver


class PortalAuditRecordTests(unittest.TestCase):
    def request(self, vehicles=("新坡92", "新坡93")):
        return AmbulanceReturnRequest(
            task_id="portal-audit", created_at=datetime(2026, 10, 24), raw_text="",
            case_id="20261024080319001", vehicle=vehicles[0], two_vehicle=len(vehicles) > 1,
            vehicle_entries=[VehicleEntry(vehicle=vehicle, driver="測試駕駛") for vehicle in vehicles],
        )

    def record(self, status):
        return {"record_id": "123", "status": status, "unit": "新坡分隊"}

    def test_partial_two_vehicle_record_waits_for_confirmation_without_writes(self):
        record = self.record("新坡92司機:測試甲")
        driver = Mock()
        with patch.object(runtime, "_ensure_duty_login", return_value=True), \
             patch.object(runtime, "_query_duty_work_logs", return_value=[record]), \
             patch.object(runtime, "_click_by_text_or_id") as click, \
             patch.object(runtime, "_click_duty_work_log_save") as save:
            result = runtime._prepare_duty_work_log_form(
                driver, self.request(), Path("."), Path("summary.txt"),
            )
        self.assertEqual("duty_work_log_waiting_confirmation", result.status)
        self.assertIn("新坡93", result.detail)
        self.assertIn("123", result.detail)
        click.assert_not_called()
        save.assert_not_called()

    def test_complete_two_vehicle_and_single_vehicle_records_remain_verified(self):
        for vehicles, status in [(("新坡92", "新坡93"), "新坡92:甲、新坡93:乙"),
                                 (("新坡92",), "新坡92:甲")]:
            with self.subTest(vehicles=vehicles), patch.object(
                runtime, "_query_duty_work_logs", return_value=[self.record(status)],
            ):
                runtime._verify_saved_duty_work_log(Mock(), self.request(vehicles))

    def test_synced_acs_identifier_is_not_written_to_stdout(self):
        sentinel = "SYNTHETIC_ID_SENTINEL"
        sink = StringIO()
        with patch.object(runtime, "lookup_synced_credential_id_number", return_value=SimpleNamespace(
            ok=True, id_number=sentinel, status="id_number_saved",
        )), redirect_stdout(sink):
            consumables_login._lookup_synced_credential_id_number_for_acs()
        self.assertNotIn(sentinel, sink.getvalue())
        self.assertIn("saved", sink.getvalue())


@unittest.skipUnless(shutil.which("node"), "Node.js required for case-page script tests")
class PortalAuditPaginationTests(unittest.TestCase):
    first = "20261024000100001"
    second = "20261024000200001"

    def waits(self, seconds=0.8):
        real_wait = runtime.WebDriverWait
        return patch.object(runtime, "WebDriverWait", side_effect=lambda d, t, **kw:
                            real_wait(d, seconds, poll_frequency=0.001))

    def test_slow_page_is_read_only_after_its_document_reloads(self):
        driver = _CasePageDriver({1: [self.first], 2: [self.second]})
        reads = []

        def read(current):
            reads.append((current.current_page, current.loading))
            return [{"case_id": case_id} for case_id in current.pages[current.current_page]]

        with self.waits(), patch.object(runtime, "_extract_emergency_cases", side_effect=read), \
             patch.object(runtime.time, "sleep"):
            result = runtime._extract_all_emergency_cases(driver)
        self.assertEqual([self.first, self.second], [case["case_id"] for case in result])
        self.assertEqual([(1, False), (2, False)], reads)
        self.assertEqual([2], driver.navigation)

    def test_missing_next_page_cannot_return_partial_success(self):
        driver = _CasePageDriver({1: [self.first], 2: [self.second]})
        with self.waits(), patch.object(runtime, "_extract_emergency_cases", return_value=[{"case_id": self.first}]), \
             patch.object(runtime, "_click_next_page_if_present", return_value=False), \
             self.assertRaises(runtime.WebDriverException):
            runtime._extract_all_emergency_cases(driver)

    def test_initial_query_requires_a_new_document_even_when_old_rows_look_ready(self):
        driver = _CasePageDriver({1: [self.first]})
        with self.waits(0.05), patch.object(runtime, "_click_by_text_or_id"), \
             self.assertRaises(runtime.TimeoutException):
            runtime._click_query_if_present(driver)

    def test_failed_query_click_is_not_swallowed(self):
        driver = _CasePageDriver({1: [self.first]})
        with patch.object(runtime, "_click_by_text_or_id", side_effect=runtime.WebDriverException("query failed")), \
             self.assertRaises(runtime.WebDriverException):
            runtime._click_query_if_present(driver)

    def test_successful_query_waits_for_new_complete_first_page(self):
        driver = _CasePageDriver({1: [self.first]})

        def submit(*args):
            driver.pending_page = 1
            driver.loading = True

        with self.waits(), patch.object(runtime, "_click_by_text_or_id", side_effect=submit):
            runtime._click_query_if_present(driver)
        self.assertEqual(1, driver.generation)
        self.assertFalse(driver.loading)

    def test_official_empty_query_is_valid_but_incomplete_or_failed_results_are_not(self):
        driver = _CasePageDriver({1: []}, response_text="QUY-500:查無資料")
        with self.waits(), patch.object(runtime, "_extract_emergency_cases", return_value=[]):
            self.assertEqual([], runtime._extract_all_emergency_cases(driver))
        for response in ("QUY-000:查詢完成", "QUY-500:查詢失敗 共 0 筆", "共 0 筆",
                         "QUY-500:查無資料 共 1 筆"):
            driver = _CasePageDriver({1: []}, response_text=response)
            with self.subTest(response=response), self.waits(0.05), self.assertRaises(runtime.TimeoutException):
                runtime._extract_all_emergency_cases(driver)

    def test_reloaded_document_with_wrong_page_cannot_return_partial_success(self):
        driver = _CasePageDriver({1: [self.first], 2: [self.second]})

        def wrong_page(current):
            current.pending_page = 1
            current.loading = True
            return True

        with self.waits(0.1), patch.object(runtime, "_extract_emergency_cases", return_value=[{"case_id": self.first}]), \
             patch.object(runtime, "_click_next_page_if_present", side_effect=wrong_page), \
             self.assertRaises(runtime.TimeoutException):
            runtime._extract_all_emergency_cases(driver)

    def test_result_count_changing_between_pages_cannot_return_partial_success(self):
        driver = _CasePageDriver({1: [self.first], 2: [self.second]})
        execute = driver.execute_script

        def changing_count(script, *args):
            if driver.current_page == 2:
                driver.response_text = "QUY-000:查詢完成 共 30 筆"
            return execute(script, *args)

        driver.execute_script = changing_count
        with self.waits(), patch.object(runtime, "_extract_emergency_cases", return_value=[{"case_id": self.first}]), \
             self.assertRaisesRegex(runtime.WebDriverException, "總筆數已變動"):
            runtime._extract_all_emergency_cases(driver)

    def test_query_timeout_cannot_publish_cases_loaded(self):
        driver = _CasePageDriver({1: [self.first]})
        with TemporaryDirectory() as directory, self.waits(0.05), \
             patch.object(runtime, "_acquire_selenium_session", return_value=False), \
             patch.object(runtime, "_create_driver", return_value=driver), \
             patch.object(runtime, "_ensure_duty_login", return_value=True), \
             patch.object(runtime, "_set_case_lookup_driver_timeouts"), \
             patch.object(runtime, "_set_window_size_if_enabled"), \
             patch.object(runtime, "_open_case_query", side_effect=lambda current, **kwargs:
                          runtime._click_query_if_present(current)), \
             patch.object(runtime, "_click_by_text_or_id"), \
             patch.object(runtime, "_quit_driver"):
            driver.implicitly_wait = Mock()
            result = runtime.query_duty_emergency_cases(Path(directory))
        self.assertFalse(result.ok)
        self.assertEqual("case_lookup_timeout", result.status)
        self.assertEqual([], result.cases)
