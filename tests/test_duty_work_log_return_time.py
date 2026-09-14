import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from ambulance_bot import selenium_local
from ambulance_bot.models import AmbulanceReturnRequest


class DutyWorkLogReturnTimeTests(unittest.TestCase):
    def setUp(self):
        self.request = AmbulanceReturnRequest(
            task_id="return-time-check",
            created_at=datetime(2026, 9, 14, 10, 21),
            raw_text="",
            case_date="2026/09/14",
            case_time="0851",
            return_date="2026/09/14",
            return_time="1020",
        )

    def test_repairs_invalid_import_without_changing_other_summary_lines(self):
        for value in ("0001/01/01 00:00:00", "2026/02/30 10:18:41", "2026/09/14 25:00:00", "unknown", ""):
            with self.subTest(value=value):
                summary = f"119案件\n緊急救護\n返隊時間:{value}\n地點:測試地址\n原始備註"
                result = selenium_local._work_log_summary_with_return_time(
                    summary, self.request.return_time_description_line,
                )
                self.assertEqual(
                    "119案件\n緊急救護\n返隊時間:2026/09/14 10:20:00\n地點:測試地址\n原始備註",
                    result,
                )

    def test_preserves_valid_official_return_time_including_seconds(self):
        summary = "119案件\r\n緊急救護\r\n返隊時間:2026/09/14 10:18:41\r\n地點:測試地址"
        self.assertEqual(summary, selenium_local._work_log_summary_with_return_time(
            summary, self.request.return_time_description_line,
        ))

    def test_repairs_fullwidth_colon_and_keeps_line_format(self):
        summary = "119案件\r\n緊急救護\r\n  返隊時間： 0001/01/01 00:00:00\r\n地點:測試地址\r\n"
        self.assertEqual(
            summary.replace("0001/01/01 00:00:00", "2026/09/14 10:20:00"),
            selenium_local._work_log_summary_with_return_time(summary, self.request.return_time_description_line),
        )

    def test_keeps_valid_midnight_and_next_day_return_time(self):
        summary = "119案件\n緊急救護\n返隊時間:2026/09/15 00:00:00\n地點:測試地址"
        self.assertEqual(summary, selenium_local._work_log_summary_with_return_time(
            summary, self.request.return_time_description_line,
        ))

    def test_repair_requires_a_valid_task_return_time(self):
        for replacement in ("", "返隊時間:0001/01/01 00:00:00", "返隊時間:2026/09/14 25:00:00"):
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ValueError, "返隊時間"):
                selenium_local._work_log_summary_with_return_time(
                    "119案件\n緊急救護\n返隊時間:0001/01/01 00:00:00", replacement,
                )

    def test_adds_missing_return_line_using_task_return_date(self):
        self.request.return_date = "2026/09/15"
        self.request.return_time = "0010"
        self.assertEqual(
            "119案件\n緊急救護\n返隊時間:2026/09/15 00:10:00\n地點:測試地址",
            selenium_local._work_log_summary_with_return_time(
                "119案件\n緊急救護\n地點:測試地址", self.request.return_time_description_line,
            ),
        )

    def test_rejects_multiple_return_lines_instead_of_guessing(self):
        with self.assertRaisesRegex(ValueError, "多筆返隊時間"):
            selenium_local._work_log_summary_with_return_time(
                "返隊時間:2026/09/14 10:18:41\n返隊時間:0001/01/01 00:00:00",
                self.request.return_time_description_line,
            )

    def test_fills_invalid_return_and_reads_back_actual_form_value(self):
        original = "119案件\n緊急救護\n返隊時間:0001/01/01 00:00:00\n地點:測試地址"
        corrected = original.replace("0001/01/01 00:00:00", "2026/09/14 10:20:00")
        driver = Mock()
        driver.execute_script.side_effect = [original, True, corrected]
        self.assertTrue(selenium_local._fill_duty_work_log_return_time(driver, self.request))
        self.assertEqual((original, corrected), driver.execute_script.call_args_list[1].args[1:])

    def test_rejects_write_that_did_not_survive_form_readback(self):
        original = "119案件\n緊急救護\n返隊時間:0001/01/01 00:00:00"
        driver = Mock()
        driver.execute_script.side_effect = [original, True, original]
        self.assertFalse(selenium_local._fill_duty_work_log_return_time(driver, self.request))

    def test_rejects_missing_unwritable_or_changed_form(self):
        original = "119案件\n緊急救護\n返隊時間:0001/01/01 00:00:00"
        for responses in ([None], [original, False]):
            with self.subTest(responses=responses):
                driver = Mock()
                driver.execute_script.side_effect = responses
                self.assertFalse(selenium_local._fill_duty_work_log_return_time(driver, self.request))

    def test_existing_valid_return_does_not_write_the_form(self):
        driver = Mock()
        driver.execute_script.return_value = "119案件\n緊急救護\n返隊時間:2026/09/14 10:18:41"
        self.assertTrue(selenium_local._fill_duty_work_log_return_time(driver, self.request))
        driver.execute_script.assert_called_once()

    def test_invalid_import_without_task_time_cannot_pass_verification(self):
        self.request.return_time = ""
        driver = Mock()
        driver.execute_script.return_value = "119案件\n緊急救護\n返隊時間:0001/01/01 00:00:00"
        self.assertFalse(selenium_local._fill_duty_work_log_return_time(driver, self.request))
        driver.execute_script.assert_called_once()

    def test_invalid_return_time_stops_save_when_correction_cannot_be_verified(self):
        driver = Mock()
        with patch.object(selenium_local, "_ensure_duty_login", return_value=True), patch.object(
            selenium_local, "_click_by_text_or_id"
        ), patch.object(selenium_local, "_switch_to_window_containing"), patch.object(
            selenium_local, "_set_case_query_date_range"
        ), patch.object(selenium_local, "_click_query_if_present"), patch.object(
            selenium_local, "_extract_all_emergency_cases", return_value=[]
        ), patch.object(selenium_local, "_match_case_for_request", return_value={"case_id": "test-case"}), patch.object(
            selenium_local, "_click_case_choose", return_value=True
        ), patch.object(selenium_local, "_switch_to_work_log_form_for_case"), patch.object(
            selenium_local, "_fill_duty_work_log_values", return_value=["工作概述返隊時間"]
        ), patch.object(selenium_local, "_save_artifacts"), patch.object(
            selenium_local, "_save_duty_work_log_enabled", return_value=True
        ), patch.object(selenium_local, "_click_duty_work_log_save") as save, patch.object(selenium_local.time, "sleep"):
            result = selenium_local._prepare_duty_work_log_form(
                driver, self.request, Path("artifacts"), Path("summary.txt"),
            )
        save.assert_not_called()
        self.assertEqual("duty_work_log_prefill_partial", result.status)
        self.assertIn("工作概述返隊時間", result.detail)


if __name__ == "__main__":
    unittest.main()
