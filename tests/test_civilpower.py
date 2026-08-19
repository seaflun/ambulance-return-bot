from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
