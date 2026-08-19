from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


class CivilpowerPlanTests(unittest.TestCase):
    @staticmethod
    def _enabled_request() -> object:
        from ambulance_bot.models import AmbulanceReturnRequest

        return AmbulanceReturnRequest(
            task_id="civilpower-run",
            created_at=datetime(2026, 8, 19, 9, 0),
            raw_text="",
            case_id="EMS-20260819-01",
            case_date="2026-08-19",
            case_time="1232",
            return_date="2026-08-19",
            return_time="1434",
            case_address="桃園市大園區航城路二段936巷111號",
            volunteer_assist=True,
            volunteer_assist_member_id="civilpower-member",
            volunteer_assist_member_name="測試義消",
            volunteer_assist_member_title="隊員",
            volunteer_assist_member_unit="大園救護分隊",
        )

    def test_task_plan_uses_precise_case_and_return_times_and_volunteer_status_line(self):
        from ambulance_bot.models import AmbulanceReturnRequest
        from civilpower import build_civilpower_task_plan

        request = AmbulanceReturnRequest(
            task_id="civilpower-plan",
            created_at=datetime(2026, 8, 19, 9, 0),
            raw_text="",
            case_id="EMS-20260819-01",
            case_date="2026-08-19",
            case_time="1232",
            return_date="2026-08-19",
            return_time="1434",
            case_address="桃園市大園區航城路二段936巷111號",
            case_reason="急病",
            volunteer_assist=True,
            volunteer_assist_member_id="civilpower-member",
            volunteer_assist_member_name="測試義消",
            volunteer_assist_member_title="隊員",
            volunteer_assist_member_unit="大園救護分隊",
        )

        plan = build_civilpower_task_plan(request)

        self.assertEqual("2026/08/19", plan.out_date)
        self.assertEqual("1232", plan.out_time)
        self.assertEqual("2026/08/19", plan.in_date)
        self.assertEqual("1434", plan.in_time)
        self.assertEqual("大園救護分隊", plan.home_unit)
        self.assertEqual("新坡分隊", plan.serve_unit)
        self.assertEqual("救護出勤", plan.out_reason)
        self.assertEqual("救護返隊", plan.in_reason)
        self.assertEqual("3.救護義消協勤:測試義消", plan.duty_status_line)
        self.assertEqual("EMS-20260819-01", plan.case_id)

    def test_task_plan_refuses_unselected_volunteer_or_invalid_times(self):
        from ambulance_bot.models import AmbulanceReturnRequest
        from civilpower import build_civilpower_task_plan

        disabled = AmbulanceReturnRequest(
            task_id="civilpower-disabled",
            created_at=datetime(2026, 8, 19, 9, 0),
            raw_text="",
            case_date="2026-08-19",
            case_time="1232",
            return_time="1434",
        )
        with self.assertRaisesRegex(ValueError, "義消協勤"):
            build_civilpower_task_plan(disabled)

        invalid_time = AmbulanceReturnRequest.from_dict(
            {
                **disabled.to_dict(),
                "volunteer_assist": True,
                "volunteer_assist_member_id": "civilpower-member",
                "volunteer_assist_member_name": "測試義消",
                "case_time": "1260",
            }
        )
        with self.assertRaisesRegex(ValueError, "出勤時間"):
            build_civilpower_task_plan(invalid_time)

    def test_runner_reports_failed_site_result_when_the_enabled_request_is_invalid(self):
        from ambulance_bot.models import AmbulanceReturnRequest
        from civilpower import run_civilpower_task

        request = AmbulanceReturnRequest(
            task_id="civilpower-invalid",
            created_at=datetime(2026, 8, 19, 9, 0),
            raw_text="",
            case_date="2026-08-19",
            case_time="1232",
            return_time="1434",
            volunteer_assist=True,
        )

        with TemporaryDirectory() as temporary_directory:
            result = run_civilpower_task(request, Path(temporary_directory))

        self.assertEqual("volunteer_assist_failed", result.status)
        self.assertIn("未選擇 NAS 名冊", result.detail)

    def test_runner_verifies_out_in_then_work_log_before_reporting_saved(self):
        from civilpower import IN_STATUS, OUT_STATUS, run_civilpower_task

        checkpoints: list[str] = []

        def ensure_io(_driver, _plan, status, _checkpoint, **_kwargs):
            checkpoints.append(status)

        def ensure_work_log(_driver, _request, _plan, _checkpoint, **_kwargs):
            checkpoints.append("work_log")

        with TemporaryDirectory() as temporary_directory, mock.patch("civilpower._ensure_io_record", side_effect=ensure_io), mock.patch(
            "civilpower._ensure_work_log", side_effect=ensure_work_log
        ):
            result = run_civilpower_task(self._enabled_request(), Path(temporary_directory), driver=object())

        self.assertEqual([OUT_STATUS, IN_STATUS, "work_log"], checkpoints)
        self.assertEqual("volunteer_assist_saved", result.status)

    def test_runner_does_not_report_saved_when_work_log_verification_fails(self):
        from civilpower import run_civilpower_task

        with TemporaryDirectory() as temporary_directory, mock.patch("civilpower._ensure_io_record"), mock.patch(
            "civilpower._ensure_work_log", side_effect=RuntimeError("工作紀錄簿回查失敗")
        ):
            result = run_civilpower_task(self._enabled_request(), Path(temporary_directory), driver=object())

        self.assertEqual("volunteer_assist_failed", result.status)
        self.assertIn("工作紀錄簿回查失敗", result.detail)

    def test_roster_query_reads_the_first_three_columns_before_the_action_column(self):
        from civilpower import FIREMAN_URL, query_civilpower_roster

        class Cell:
            def __init__(self, text: str):
                self.text = text

        class Row:
            def __init__(self, *cells: str):
                self.cells = [Cell(cell) for cell in cells]

        rows = [
            Row("單位", "職稱", "姓名", "動作"),
            Row("大園救護分隊", "隊員", "可選人員", ""),
            Row("大園救護分隊", "技術顧問", "排除人員", ""),
            Row("其他分隊", "隊員", "其他人員", ""),
        ]
        driver = mock.Mock()
        wait = object()

        with mock.patch("civilpower.WebDriverWait", return_value=wait), mock.patch(
            "civilpower._wait_for_civilpower_page"
        ), mock.patch("civilpower._select_option_containing"), mock.patch("civilpower._click"), mock.patch(
            "civilpower._wait_for_rows"
        ), mock.patch("civilpower._table_rows", return_value=rows), mock.patch(
            "civilpower._row_cells", side_effect=lambda row: row.cells
        ):
            members = query_civilpower_roster(driver)

        driver.get.assert_called_once_with(FIREMAN_URL)
        self.assertEqual(["可選人員"], [member["name"] for member in members])

    def test_oa_other_menu_opens_civilpower_sso_in_its_new_tab(self):
        from civilpower import open_civilpower_from_oa_dashboard

        class FakeDriver:
            def __init__(self):
                self.window_handles = ["oa"]
                self.switch_to = mock.Mock()

        class FakeEntry:
            def __init__(self, driver):
                self.driver = driver

            def click(self):
                self.driver.window_handles.append("civilpower")

        class FakeWait:
            def __init__(self, driver):
                self.driver = driver

            def until(self, predicate):
                result = predicate(self.driver)
                if not result:
                    raise AssertionError("expected the menu action to complete")
                return result

        driver = FakeDriver()
        entry = FakeEntry(driver)
        wait = FakeWait(driver)

        with mock.patch("civilpower._click") as click, mock.patch(
            "civilpower.EC.element_to_be_clickable", return_value=lambda _driver: entry
        ), mock.patch("civilpower._wait_for_civilpower_page"):
            open_civilpower_from_oa_dashboard(driver, wait)

        click.assert_called_once_with(wait, "#moduleBox_other")
        driver.switch_to.window.assert_called_once_with("civilpower")

    def test_failed_roster_refresh_is_due_immediately_after_worker_restart(self):
        from civilpower import civilpower_roster_refresh_due

        with TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "civilpower" / "roster_refresh.json"
            report_path.parent.mkdir()
            report_path.write_text(
                json.dumps(
                    {
                        "status": "civilpower_roster_failed",
                        "attempted_at": "2026-08-19T09:15:06",
                    }
                ),
                encoding="utf-8",
            )

            due = civilpower_roster_refresh_due(
                Path(temporary_directory),
                now=datetime(2026, 8, 19, 9, 16),
            )

        self.assertTrue(due)


if __name__ == "__main__":
    unittest.main()
