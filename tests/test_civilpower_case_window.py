from __future__ import annotations

import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from ambulance_bot import selenium_local
import civilpower
from tests import test_civilpower


class CivilpowerCaseWindowQueryTests(unittest.TestCase):
    def request(self):
        request = test_civilpower.CivilpowerPlanTests._enabled_request()
        request.case_id = "20260819123245099"
        request.vehicle = "新坡92"
        return request

    def source_record(self, request):
        return {"record_id": "501", "case_id": request.case_id, "work_at": "1150819 12:31",
                "unit": "新坡分隊", "item": "救護", "reason": "急病",
                "description": "119案件\n返隊時間:2026/08/19 14:28:10\n地點:測試路1號",
                "status": "1.新坡92:測試甲\n3.救護義消協勤:測試義消", "personnel": "測試甲,測試乙"}

    def test_official_case_query_uses_task_credentials_and_exact_case_without_saving(self):
        request = self.request()
        expected = self.source_record(request)
        driver = mock.Mock()
        with TemporaryDirectory() as directory, ExitStack() as stack:
            create_driver = stack.enter_context(mock.patch.object(selenium_local, "_create_driver", return_value=driver))
            login = stack.enter_context(mock.patch.object(selenium_local, "_ensure_duty_login", return_value=True))
            query = stack.enter_context(mock.patch.object(selenium_local, "_query_duty_work_logs", return_value=[expected]))
            stack.enter_context(mock.patch.object(selenium_local, "_open_case_query"))
            stack.enter_context(mock.patch.object(selenium_local, "_extract_all_emergency_cases", return_value=[]))
            save = stack.enter_context(mock.patch.object(selenium_local, "_click_duty_work_log_save"))

            result = selenium_local.query_duty_case_for_civilpower(request, Path(directory))

        self.assertEqual(expected, result)
        login.assert_called_once_with(driver, request)
        save.assert_not_called()
        driver.quit.assert_called_once()
        from ambulance_bot.profile_paths import _is_generated_runtime_profile
        self.assertTrue(_is_generated_runtime_profile(create_driver.call_args.kwargs["profile_name"]))
        start, end = query.call_args.kwargs["query_range"]
        self.assertEqual(datetime(2026, 8, 18), start)
        self.assertEqual(datetime(2026, 8, 20), end)

    def test_missing_or_multiple_case_matches_do_not_use_another_case(self):
        request = self.request()
        record = self.source_record(request)
        for rows in ([], [record, dict(record, record_id="502")]):
            with self.subTest(rows=rows), TemporaryDirectory() as directory, ExitStack() as stack:
                driver = mock.Mock()
                stack.enter_context(mock.patch.object(selenium_local, "_create_driver", return_value=driver))
                stack.enter_context(mock.patch.object(selenium_local, "_ensure_duty_login", return_value=True))
                stack.enter_context(mock.patch.object(selenium_local, "_open_case_query"))
                stack.enter_context(mock.patch.object(selenium_local, "_extract_all_emergency_cases", return_value=[]))
                stack.enter_context(mock.patch.object(selenium_local, "_query_duty_work_logs", return_value=rows))
                with self.assertRaisesRegex(RuntimeError, "唯一"):
                    selenium_local.query_duty_case_for_civilpower(request, Path(directory))
                driver.quit.assert_called_once()

    def test_missing_volunteer_in_the_first_station_stops_before_civilpower_writes(self):
        request = self.request()
        record = dict(self.source_record(request), status="1.新坡92:測試甲\n3.救護義消協勤:其他義消")
        driver = mock.Mock()
        with TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch.object(selenium_local, "_create_driver", return_value=driver))
            stack.enter_context(mock.patch.object(selenium_local, "_ensure_duty_login", return_value=True))
            stack.enter_context(mock.patch.object(selenium_local, "_query_duty_work_logs", return_value=[record]))
            stack.enter_context(mock.patch.object(selenium_local, "_open_case_query"))
            stack.enter_context(mock.patch.object(selenium_local, "_extract_all_emergency_cases", return_value=[]))
            with self.assertRaisesRegex(RuntimeError, "協勤人員"):
                selenium_local.query_duty_case_for_civilpower(request, Path(directory))

    def test_resolver_uses_saved_work_log_dispatch_instead_of_report_or_requested_time(self):
        request = self.request()
        record = self.source_record(request)
        plan = civilpower.build_civilpower_task_plan(request)
        with mock.patch.object(selenium_local, "query_duty_case_for_civilpower", return_value=record):
            resolved, window = civilpower._resolve_official_case_plan(request, plan, Path("unused"))
        self.assertEqual("1231", resolved.out_time)
        self.assertEqual("1428", resolved.in_time)
        self.assertEqual(datetime(2026, 8, 19, 12, 31), window.dispatch_at)
        self.assertEqual(datetime(2026, 8, 19, 14, 28, 10), window.return_at)

    def test_resolver_does_not_guess_missing_formal_return_from_task_time(self):
        request = self.request()
        record = dict(self.source_record(request), description="119案件\n返隊時間:未填\n地點:測試路1號")
        with mock.patch.object(selenium_local, "query_duty_case_for_civilpower", return_value=record):
            with self.assertRaisesRegex(RuntimeError, "返隊時間"):
                civilpower._resolve_official_case_plan(request, civilpower.build_civilpower_task_plan(request), Path("unused"))

    def test_saved_work_log_can_cross_midnight_regardless_of_the_task_return_date(self):
        request = self.request()
        record = dict(self.source_record(request), work_at="1150819 23:55",
                      description="返隊時間:2026/08/20 00:30:21\n地點:測試路1號")
        with mock.patch.object(selenium_local, "query_duty_case_for_civilpower", return_value=record):
            plan, window = civilpower._resolve_official_case_plan(request, civilpower.build_civilpower_task_plan(request), Path("unused"))
        self.assertEqual("2026/08/20", plan.in_date)
        self.assertEqual("0030", plan.in_time)
        self.assertEqual(datetime(2026, 8, 20, 0, 30, 21), window.return_at)

    def test_civilpower_remains_after_first_station_in_the_same_dependency_chain(self):
        from ambulance_bot.desktop_fast_runner import active_site_groups

        groups = active_site_groups(self.request(), "fixture", mock.Mock())
        keys = [[key for key, _runner in group] for group in groups]
        self.assertEqual(["duty_work_log", "volunteer_assist"], keys[0])
        self.assertIn("vehicle_mileage", keys[1])
