from __future__ import annotations

import json
import unittest
from dataclasses import replace
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
        self.assertEqual("急病", plan.case_reason)

    def test_work_log_import_accepts_case_dispatch_time_that_precedes_volunteer_out_time(self):
        from civilpower import CivilpowerTaskPlan, _assert_imported_work_log_values

        plan = CivilpowerTaskPlan(
            task_id="civilpower-historical-time-gap",
            case_id="EMS-20260817-01",
            case_address="桃園市觀音區中山路二段791號",
            member_id="member-1",
            member_name="江尚諭",
            member_title="隊員",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/17",
            out_time="1157",
            in_date="2026/08/17",
            in_time="1214",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="3.救護義消協勤:江尚諭",
        )
        values = {
            "#txt_AddDisDate": "2026/8/17",
            "#txt_AddDisHour": "11",
            "#txt_AddDisMin": "56",
            "#txt_AddBackDate": "2026/8/17",
            "#txt_AddBackHour": "12",
            "#txt_AddBackMin": "14",
            "#txt_AddStat": "1.新坡92:劉家誠\n2.男1名\n3.救護義消協勤:江尚諭",
        }

        with mock.patch("civilpower._control_value", side_effect=lambda _driver, selector: values[selector]):
            _assert_imported_work_log_values(object(), plan)

    def test_work_log_import_requires_the_exact_case_return_minute(self):
        from civilpower import CivilpowerTaskPlan, _assert_imported_work_log_values

        plan = CivilpowerTaskPlan(
            task_id="civilpower-return-minute-gap",
            case_id="20260826103603012",
            case_address="桃園市觀音區東大路147巷21號",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/26",
            out_time="1036",
            in_date="2026/08/26",
            in_time="1147",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="3.救護義消協勤:張贊鏡",
            case_reason="創傷",
        )
        values = {
            "#txt_AddDisDate": "2026/8/26",
            "#txt_AddDisHour": "10",
            "#txt_AddDisMin": "36",
            "#txt_AddBackDate": "2026/8/26",
            "#txt_AddBackHour": "11",
            "#txt_AddBackMin": "46",
            "#txt_AddStat": "3.救護義消協勤:張贊鏡",
        }

        with mock.patch("civilpower._control_value", side_effect=lambda _driver, selector: values[selector]):
            with self.assertRaisesRegex(RuntimeError, "#txt_AddBackMin"):
                _assert_imported_work_log_values(object(), plan)

        values["#txt_AddBackMin"] = "47"
        with mock.patch("civilpower._control_value", side_effect=lambda _driver, selector: values[selector]):
            _assert_imported_work_log_values(object(), plan)

    def test_work_log_import_keeps_volunteer_status_line_required(self):
        from civilpower import CivilpowerTaskPlan, _assert_imported_work_log_values

        plan = CivilpowerTaskPlan(
            task_id="civilpower-status-required",
            case_id="EMS-20260817-01",
            case_address="桃園市觀音區中山路二段791號",
            member_id="member-1",
            member_name="江尚諭",
            member_title="隊員",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/17",
            out_time="1157",
            in_date="2026/08/17",
            in_time="1214",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="3.救護義消協勤:江尚諭",
        )
        values = {
            "#txt_AddDisDate": "2026/8/17",
            "#txt_AddDisHour": "11",
            "#txt_AddDisMin": "56",
            "#txt_AddBackDate": "2026/8/17",
            "#txt_AddBackHour": "12",
            "#txt_AddBackMin": "14",
            "#txt_AddStat": "1.新坡92:劉家誠\n2.男1名",
        }

        with mock.patch("civilpower._control_value", side_effect=lambda _driver, selector: values[selector]):
            with self.assertRaisesRegex(RuntimeError, "救護義消協勤:江尚諭"):
                _assert_imported_work_log_values(object(), plan)

    def test_work_log_recheck_uses_unique_member_and_dispatch_time_as_the_last_safe_fallback(self):
        from civilpower import CivilpowerTaskPlan, _find_work_log_record

        plan = CivilpowerTaskPlan(
            task_id="civilpower-recheck-fallback",
            case_id="20260826103603012",
            case_address="桃園市觀音區東大路147巷21號",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/26",
            out_time="1036",
            in_date="2026/08/26",
            in_time="1147",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="3.救護義消協勤:張贊鏡",
            case_reason="創傷",
        )
        driver = mock.Mock()
        wait = mock.Mock()
        row = mock.Mock()

        with mock.patch("civilpower._open_work_log_form", return_value=wait), mock.patch(
            "civilpower._set_if_present"
        ), mock.patch("civilpower._click_if_present"), mock.patch(
            "civilpower._matching_table_rows",
            side_effect=[[], [], [], [], [row]],
        ) as matching_rows:
            self.assertTrue(_find_work_log_record(driver, plan))

        self.assertEqual(
            [
                mock.call(driver, ["張贊鏡", "20260826103603012"]),
                mock.call(driver, ["張贊鏡", "桃園市觀音區東大路147巷21號"]),
                mock.call(driver, ["張贊鏡", "創傷", "1036"]),
                mock.call(driver, ["張贊鏡", "救護出勤", "1036"]),
                mock.call(driver, ["張贊鏡", "1036"]),
            ],
            matching_rows.call_args_list,
        )

    def test_work_log_recheck_waits_for_delayed_query_result_after_save(self):
        from civilpower import CivilpowerTaskPlan, _find_work_log_record

        plan = CivilpowerTaskPlan(
            task_id="civilpower-recheck-delayed-query",
            case_id="20260826103603012",
            case_address="桃園市觀音區東大路147巷21號",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/26",
            out_time="1036",
            in_date="2026/08/26",
            in_time="1147",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="3.救護義消協勤:張贊鏡",
            case_reason="創傷",
        )
        driver = mock.Mock()
        wait = mock.Mock()
        row = mock.Mock()
        query_rows = [[], [], [], [], [], [], [], [], [], [row]]

        def wait_until(predicate):
            for _attempt in range(2):
                result = predicate(driver)
                if result:
                    return result
            raise AssertionError("等待中的查詢沒有重新檢查工作紀錄")

        wait.until.side_effect = wait_until
        with mock.patch("civilpower._open_work_log_form", return_value=wait), mock.patch(
            "civilpower._set_if_present"
        ), mock.patch("civilpower._click_if_present"), mock.patch(
            "civilpower._matching_table_rows",
            side_effect=query_rows,
        ) as matching_rows:
            self.assertTrue(_find_work_log_record(driver, plan, wait_for_match=True))

        self.assertEqual(10, matching_rows.call_count)
        self.assertEqual(1, wait.until.call_count)

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

    def test_civilpower_login_prioritizes_on_duty_credential(self):
        from ambulance_bot.duty_credentials import LOGIN_POLICY_CIVILPOWER, DutyCredential
        from civilpower import login_civilpower_and_get_driver

        driver = mock.Mock()
        on_duty = DutyCredential("on-duty", "on-duty-password")
        task_driver = DutyCredential("task-driver", "task-driver-password")
        request = self._enabled_request()
        with mock.patch(
            "civilpower.task_login_credential_attempts",
            return_value=[(on_duty, "值班人員"), (task_driver, "任務司機")],
        ) as credential_attempts, mock.patch(
            "civilpower.create_chrome_driver_with_retry",
            return_value=driver,
        ), mock.patch(
            "civilpower.apply_tile",
        ), mock.patch(
            "civilpower._login_once",
        ) as login_once, mock.patch(
            "civilpower.open_civilpower_from_oa_dashboard",
        ):
            result = login_civilpower_and_get_driver(request=request)

        self.assertIs(result, driver)
        credential_attempts.assert_called_once_with(
            request,
            duty_password=False,
            login_policy=LOGIN_POLICY_CIVILPOWER,
        )
        login_once.assert_called_once_with(driver, "on-duty", "on-duty-password", 1)

    def test_civilpower_repair_login_uses_only_the_original_account(self):
        from ambulance_bot.duty_credentials import DutyCredential
        from civilpower import login_civilpower_and_get_driver

        driver = mock.Mock()
        original = DutyCredential("original-entry", "original-password")
        logged_in_accounts: list[str] = []

        with mock.patch("civilpower.load_duty_credential", return_value=original) as load_credential, mock.patch(
            "civilpower.task_login_credential_attempts"
        ) as credential_attempts, mock.patch(
            "civilpower.create_chrome_driver_with_retry",
            return_value=driver,
        ), mock.patch("civilpower.apply_tile"), mock.patch("civilpower._login_once") as login_once, mock.patch(
            "civilpower.open_civilpower_from_oa_dashboard"
        ):
            result = login_civilpower_and_get_driver(
                required_user_id="original-entry",
                on_login_success=logged_in_accounts.append,
            )

        self.assertIs(result, driver)
        load_credential.assert_called_once_with(
            ["original-entry"],
            allow_default=False,
            duty_password=False,
        )
        credential_attempts.assert_not_called()
        login_once.assert_called_once_with(driver, "original-entry", "original-password", 1)
        self.assertEqual(["original-entry"], logged_in_accounts)

    def test_runner_verifies_out_in_then_work_log_before_reporting_saved(self):
        from civilpower import IN_STATUS, OUT_STATUS, run_civilpower_task

        checkpoints: list[str] = []

        def ensure_io(_driver, _plan, status, _checkpoint, **_kwargs):
            checkpoints.append(status)
            return status == OUT_STATUS

        def ensure_correct_in(_driver, _plan, _original_plan, _checkpoint, **_kwargs):
            checkpoints.append(IN_STATUS)

        def ensure_work_log(_driver, _request, _plan, _checkpoint, **_kwargs):
            checkpoints.append("work_log")

        with TemporaryDirectory() as temporary_directory, mock.patch("civilpower._ensure_io_record", side_effect=ensure_io), mock.patch(
            "civilpower._ensure_correct_in_io_record", side_effect=ensure_correct_in
        ), mock.patch(
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

    def test_runner_reports_case_import_stage_for_work_log_timeout(self):
        from selenium.common.exceptions import TimeoutException

        from civilpower import run_civilpower_task

        with TemporaryDirectory() as temporary_directory, mock.patch("civilpower._ensure_io_record"), mock.patch(
            "civilpower._ensure_work_log", side_effect=TimeoutException()
        ), mock.patch("civilpower.capture_failure_artifacts", return_value={}):
            result = run_civilpower_task(self._enabled_request(), Path(temporary_directory), driver=mock.Mock())

        self.assertEqual("volunteer_assist_failed", result.status)
        self.assertEqual("案件代入", result.failure_stage)
        self.assertEqual("TimeoutException", result.exception_type)
        self.assertIn("案件代入", result.detail)
        self.assertIn("案件代入", result.failure_reason)
        self.assertIn("單獨重跑", result.next_action)

    def test_selection_dialog_retries_once_with_short_wait(self):
        from selenium.common.exceptions import TimeoutException

        from civilpower import _open_selection_dialog

        driver = object()
        initial_wait = object()
        retry_wait = object()
        dialog = object()

        with mock.patch("civilpower._click") as click, mock.patch(
            "civilpower._visible_dialog", side_effect=[TimeoutException(), dialog]
        ) as visible_dialog, mock.patch("civilpower.WebDriverWait", return_value=retry_wait) as webdriver_wait:
            actual = _open_selection_dialog(driver, initial_wait, "#btn_CaseSlt", "案件代入")

        self.assertIs(dialog, actual)
        self.assertEqual(
            [mock.call(initial_wait, "#btn_CaseSlt"), mock.call(initial_wait, "#btn_CaseSlt")],
            click.call_args_list,
        )
        webdriver_wait.assert_called_once_with(driver, 5)
        self.assertEqual(
            [mock.call(driver, initial_wait), mock.call(driver, retry_wait)],
            visible_dialog.call_args_list,
        )

    def test_visible_dialog_prefers_the_requested_window_title_over_the_outer_form(self):
        from civilpower import _visible_dialog

        driver = mock.Mock()
        wait = mock.Mock()
        case_dialog = mock.Mock()
        outer_form = mock.Mock()
        case_dialog.is_displayed.return_value = True
        outer_form.is_displayed.return_value = True
        case_header = mock.Mock()
        case_header.text = "案件選取"
        outer_header = mock.Mock()
        outer_header.text = "工作紀錄簿新增"
        case_dialog.find_elements.return_value = [case_header]
        outer_form.find_elements.return_value = [outer_header]
        driver.find_elements.return_value = [case_dialog, outer_form]
        wait.until.side_effect = lambda condition: condition(driver)

        actual = _visible_dialog(driver, wait, title_text="案件選取")

        self.assertIs(case_dialog, actual)

    def test_selection_dialog_prefers_the_window_opened_after_the_trigger(self):
        from civilpower import _open_selection_dialog

        driver = mock.Mock()
        wait = mock.Mock()
        outer_form = mock.Mock()
        selection_dialog = mock.Mock()
        outer_form.id = "outer-form"
        selection_dialog.id = "selection-dialog"
        outer_form.is_displayed.return_value = True
        selection_dialog.is_displayed.return_value = True
        dialogs = [outer_form]
        driver.find_elements.side_effect = lambda *_args: dialogs
        wait.until.side_effect = lambda condition: condition(driver)

        def open_selection(*_args) -> None:
            dialogs[:] = [selection_dialog, outer_form]

        with mock.patch("civilpower._click_selection_trigger", side_effect=open_selection):
            actual = _open_selection_dialog(driver, wait, "#btn_Test", "測試選取")

        self.assertIs(selection_dialog, actual)

    def test_visible_dialog_waits_for_a_new_window_instead_of_returning_the_outer_form(self):
        from civilpower import _visible_dialog

        driver = mock.Mock()
        wait = mock.Mock()
        outer_form = mock.Mock()
        outer_form.id = "outer-form"
        outer_form.is_displayed.return_value = True
        driver.find_elements.return_value = [outer_form]
        wait.until.side_effect = lambda condition: condition(driver)

        actual = _visible_dialog(driver, wait, previous_dialog_ids={"outer-form"})

        self.assertFalse(actual)

    def test_work_log_page_reloads_once_when_required_controls_are_missing(self):
        from selenium.common.exceptions import TimeoutException

        from civilpower import WORK_LOG_URL, _open_work_log_form

        driver = mock.Mock()
        initial_wait = object()
        retry_wait = object()
        action_wait = object()

        with mock.patch(
            "civilpower.WebDriverWait",
            side_effect=[initial_wait, retry_wait, action_wait],
        ) as webdriver_wait, mock.patch("civilpower._wait_for_civilpower_page"), mock.patch(
            "civilpower._wait_for_work_log_controls",
            side_effect=[TimeoutException(), None],
        ) as wait_for_controls:
            actual = _open_work_log_form(driver)

        self.assertIs(action_wait, actual)
        driver.get.assert_called_once_with(WORK_LOG_URL)
        driver.refresh.assert_called_once_with()
        self.assertEqual(
            [mock.call(driver, 10), mock.call(driver, 5), mock.call(driver, 15)],
            webdriver_wait.call_args_list,
        )
        self.assertEqual(
            [mock.call(driver, initial_wait), mock.call(driver, retry_wait)],
            wait_for_controls.call_args_list,
        )

    def test_selection_dialog_names_a_trigger_that_never_becomes_clickable(self):
        from selenium.common.exceptions import TimeoutException

        from civilpower import _open_selection_dialog

        driver = mock.Mock()
        driver.current_url = "https://civilpower.tyfd.gov.tw/TYCC/Home/WorkLog"
        with mock.patch("civilpower._click", side_effect=TimeoutException()), mock.patch(
            "civilpower._visible_dialog"
        ) as visible_dialog:
            with self.assertRaisesRegex(TimeoutException, "救護出勤登記選取按鈕未就緒"):
                _open_selection_dialog(driver, object(), "#btn_AddSltIOWorkLog", "救護出勤登記")

        visible_dialog.assert_not_called()

    def test_timeout_detail_keeps_compact_work_log_page_context(self):
        from selenium.common.exceptions import TimeoutException

        from civilpower import _civilpower_failure_detail

        detail = _civilpower_failure_detail(
            "選取救護出勤登記",
            TimeoutException("工作紀錄簿頁面未完整載入（缺少 #btn_AddSltIOWorkLog）。"),
        )

        self.assertIn("選取救護出勤登記", detail)
        self.assertIn("工作紀錄簿頁面未完整載入", detail)

    def test_work_log_opens_new_form_before_selecting_out_record(self):
        from civilpower import _ensure_work_log, build_civilpower_task_plan

        request = self._enabled_request()
        plan = build_civilpower_task_plan(request)
        steps: list[str] = []
        wait = object()

        with mock.patch("civilpower._find_work_log_record", side_effect=[False, True]), mock.patch(
            "civilpower.WebDriverWait", return_value=wait
        ), mock.patch(
            "civilpower._click",
            side_effect=lambda _wait, selector: steps.append(f"click:{selector}"),
        ), mock.patch(
            "civilpower._wait_visible",
            side_effect=lambda _wait, selector: steps.append(f"visible:{selector}"),
        ), mock.patch(
            "civilpower._wait_for_work_log_add_controls",
            side_effect=lambda _driver, _wait: steps.append("add-controls"),
        ), mock.patch(
            "civilpower._select_out_io_record_for_work_log",
            side_effect=lambda _driver, _wait, _plan: steps.append("select-out"),
        ), mock.patch(
            "civilpower._import_work_log_case",
            side_effect=lambda _driver, _wait, _plan: steps.append("import-case"),
        ), mock.patch(
            "civilpower._assert_imported_work_log_values",
            side_effect=lambda _driver, current_plan, **_kwargs: steps.append("verify-case") or current_plan,
        ), mock.patch(
            "civilpower._wait_after_save",
            side_effect=lambda _driver, _wait, selector: steps.append(f"saved:{selector}"),
        ):
            _ensure_work_log(
                object(),
                request,
                plan,
                {},
                cancel_check=None,
                progress=steps.append,
            )

        self.assertEqual(
            [
                "查詢既有工作紀錄",
                "開啟工作紀錄簿新增表單",
                "click:#btn_Add",
                "visible:#jqxAddWindow",
                "add-controls",
                "選取救護出勤登記",
                "select-out",
                "案件代入",
                "import-case",
                "驗證案件代入",
                "verify-case",
                "儲存工作紀錄",
                "click:#btn_WorkLogAdd",
                "saved:#jqxAddWindow",
                "工作紀錄回查",
            ],
            steps,
        )

    def test_work_log_corrects_the_existing_in_record_before_saving(self):
        from civilpower import _ensure_work_log, build_civilpower_task_plan

        request = self._enabled_request()
        manual_plan = build_civilpower_task_plan(request)
        official_plan = replace(manual_plan, in_time="1433")
        wait = object()
        reconciled: list[object] = []

        with mock.patch("civilpower._find_work_log_record", side_effect=[False, False, True]), mock.patch(
            "civilpower.WebDriverWait", return_value=wait
        ), mock.patch("civilpower._click") as click, mock.patch("civilpower._wait_visible"), mock.patch(
            "civilpower._wait_for_work_log_add_controls"
        ), mock.patch("civilpower._select_out_io_record_for_work_log"), mock.patch(
            "civilpower._import_work_log_case"
        ) as import_case, mock.patch(
            "civilpower._read_imported_work_log_plan",
            side_effect=[official_plan, official_plan],
        ), mock.patch("civilpower._wait_after_save"):
            result = _ensure_work_log(
                object(),
                request,
                manual_plan,
                {},
                cancel_check=None,
                reconcile_inbound=reconciled.append,
            )

        self.assertIs(official_plan, result)
        self.assertEqual([official_plan], reconciled)
        self.assertEqual(2, import_case.call_count)
        self.assertEqual([mock.call(wait, "#btn_WorkLogAdd")], [call for call in click.call_args_list if call.args[1] == "#btn_WorkLogAdd"])

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
        ), mock.patch(
            "civilpower._open_next_civilpower_roster_page", return_value=False
        ):
            members = query_civilpower_roster(driver)

        driver.get.assert_called_once_with(FIREMAN_URL)
        self.assertEqual(["可選人員"], [member["name"] for member in members])

    def test_roster_query_combines_all_pagination_pages_before_filtering(self):
        from civilpower import query_civilpower_roster

        page_one = [
            {"unit": "大園救護分隊", "title": "隊員", "name": "第一頁人員"},
            {"unit": "大園救護分隊", "title": "技術顧問", "name": "排除人員"},
        ]
        page_two = [
            {"unit": "大園救護分隊", "title": "隊員", "name": "第二頁人員"},
        ]
        driver = mock.Mock()
        wait = object()

        with mock.patch("civilpower.WebDriverWait", return_value=wait), mock.patch(
            "civilpower._wait_for_civilpower_page"
        ), mock.patch("civilpower._select_option_containing"), mock.patch("civilpower._click"), mock.patch(
            "civilpower._wait_for_rows"
        ), mock.patch(
            "civilpower._read_civilpower_roster_page", side_effect=[page_one, page_two]
        ), mock.patch(
            "civilpower._open_next_civilpower_roster_page", side_effect=[True, False]
        ):
            members = query_civilpower_roster(driver)

        self.assertEqual(["第一頁人員", "第二頁人員"], [member["name"] for member in members])

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

    def test_loaded_roster_without_pagination_confirmation_is_refreshed_after_update(self):
        from civilpower import civilpower_roster_refresh_due

        with TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "civilpower" / "roster_refresh.json"
            report_path.parent.mkdir()
            report_path.write_text(
                json.dumps(
                    {
                        "status": "civilpower_roster_loaded",
                        "attempted_at": "2026-08-19T10:13:09",
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                civilpower_roster_refresh_due(
                    Path(temporary_directory),
                    now=datetime(2026, 8, 19, 10, 14),
                )
            )

            report_path.write_text(
                json.dumps(
                    {
                        "status": "civilpower_roster_loaded",
                        "attempted_at": "2026-08-19T10:13:09",
                        "pagination_complete": True,
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(
                civilpower_roster_refresh_due(
                    Path(temporary_directory),
                    now=datetime(2026, 8, 19, 10, 14),
                )
            )

    def test_successful_roster_refresh_confirms_pagination_completion(self):
        from civilpower import refresh_civilpower_roster

        with TemporaryDirectory() as temporary_directory, mock.patch(
            "civilpower.query_civilpower_roster",
            return_value=[{"id": "member-1", "name": "完整名冊人員"}],
        ):
            report = refresh_civilpower_roster(Path(temporary_directory), driver=object())

        self.assertEqual("civilpower_roster_loaded", report["status"])
        self.assertIs(True, report["pagination_complete"])

    def test_jqx_option_wait_retries_until_the_linked_option_is_ready(self):
        from civilpower import _wait_for_jqx_combobox_option

        class PollingWait:
            def __init__(self, driver):
                self.driver = driver
                self.calls = 0

            def until(self, predicate):
                for _ in range(3):
                    self.calls += 1
                    result = predicate(self.driver)
                    if result:
                        return result
                raise AssertionError("linked option never became ready")

        driver = mock.Mock()
        driver.execute_script.side_effect = [False, True]
        wait = PollingWait(driver)

        _wait_for_jqx_combobox_option(driver, wait, "#txt_AddServeUnit", "新坡分隊")

        self.assertEqual(2, wait.calls)

    def test_jqx_option_signature_treats_replaced_widget_as_not_ready(self):
        from selenium.common.exceptions import StaleElementReferenceException

        from civilpower import _jqx_combobox_option_signature

        driver = mock.Mock()
        driver.find_element.return_value = mock.Mock()
        driver.execute_script.side_effect = StaleElementReferenceException("widget was replaced")

        self.assertIsNone(_jqx_combobox_option_signature(driver, "#txt_AddServeUnit"))

    def test_jqx_option_ready_treats_replaced_widget_as_not_ready(self):
        from selenium.common.exceptions import StaleElementReferenceException

        from civilpower import _jqx_combobox_option_ready

        driver = mock.Mock()
        driver.find_element.return_value = mock.Mock()
        driver.execute_script.side_effect = StaleElementReferenceException("widget was replaced")

        self.assertIs(False, _jqx_combobox_option_ready(driver, "#txt_AddServeUnit", "新坡分隊"))

    def test_jqx_selection_reacquires_widget_after_replacement(self):
        from selenium.common.exceptions import StaleElementReferenceException

        from civilpower import _select_jqx_combobox

        class PollingWait:
            def __init__(self, driver):
                self.driver = driver
                self.calls = 0

            def until(self, predicate):
                for _ in range(3):
                    self.calls += 1
                    result = predicate(self.driver)
                    if result:
                        return result
                raise AssertionError("replacement widget never became selectable")

        driver = mock.Mock()
        driver.find_element.return_value = mock.Mock()
        driver.execute_script.side_effect = [StaleElementReferenceException("widget was replaced"), True]
        wait = PollingWait(driver)

        with mock.patch("civilpower._wait_for_jqx_combobox_option"), mock.patch(
            "civilpower._control_value", return_value="新坡分隊"
        ):
            _select_jqx_combobox(driver, wait, "#txt_AddServeUnit", "新坡分隊")

        self.assertEqual(3, wait.calls)
        self.assertEqual(2, driver.execute_script.call_count)

    def test_io_dependencies_wait_for_rebuilt_serve_unit_options(self):
        from civilpower import _wait_for_io_form_dependencies

        class Plan:
            serve_unit = "新坡分隊"

        class PollingWait:
            def __init__(self, driver):
                self.driver = driver
                self.calls = 0

            def until(self, predicate):
                for _ in range(3):
                    self.calls += 1
                    result = predicate(self.driver)
                    if result:
                        return result
                raise AssertionError("serve unit options never rebuilt")

        previous_signature = (
            ("大園分隊", "大園分隊"),
            ("新坡分隊", "新坡分隊"),
            ("草漯分隊", "草漯分隊"),
        )
        rebuilt_signature = (
            ("大園分隊", "大園分隊"),
            ("新坡分隊", "新坡分隊"),
        )
        driver = mock.Mock()
        control = mock.Mock()
        control.is_displayed.return_value = True
        control.is_enabled.return_value = True
        driver.find_element.return_value = control
        driver.execute_script.side_effect = [list(previous_signature), list(rebuilt_signature)]
        wait = PollingWait(driver)

        _wait_for_io_form_dependencies(
            driver,
            wait,
            Plan(),
            previous_serve_unit_signature=previous_signature,
        )

        self.assertEqual(3, wait.calls)
        self.assertEqual(2, driver.execute_script.call_count)

    def test_io_person_dialog_waits_for_correct_dialog_and_unique_row(self):
        from civilpower import _wait_for_io_person_dialog_row

        class PollingWait:
            def __init__(self, driver):
                self.driver = driver
                self.calls = 0

            def until(self, predicate):
                for _ in range(3):
                    self.calls += 1
                    result = predicate(self.driver)
                    if result:
                        return result
                raise AssertionError("person selection row never became ready")

        driver = mock.Mock()
        dialog = mock.Mock()
        dialog.is_displayed.return_value = True
        row = mock.Mock()
        driver.find_elements.side_effect = [[], [dialog]]
        wait = PollingWait(driver)

        with mock.patch("civilpower._matching_table_rows", return_value=[row]):
            actual_dialog, actual_row = _wait_for_io_person_dialog_row(
                driver,
                wait,
                ["大園救護分隊", "張贊鏡", "小隊長"],
            )

        self.assertIs(dialog, actual_dialog)
        self.assertIs(row, actual_row)
        self.assertEqual(2, wait.calls)

    def test_selection_dialog_waits_for_async_matching_row_before_clicking(self):
        from civilpower import _select_dialog_row

        class PollingWait:
            def __init__(self, driver):
                self.driver = driver
                self.calls = 0

            def until(self, predicate):
                for _ in range(3):
                    self.calls += 1
                    result = predicate(self.driver)
                    if result:
                        return result
                raise AssertionError("selection row never became ready")

        driver = object()
        dialog = object()
        row = mock.Mock()
        wait = PollingWait(driver)

        with mock.patch("civilpower._matching_table_rows", side_effect=[[], [row]]), mock.patch(
            "civilpower._click_dialog_row"
        ) as click_row:
            _select_dialog_row(
                driver,
                wait,
                dialog,
                ["張贊鏡", "大園救護分隊", "救護出勤", "1000"],
            )

        click_row.assert_called_once_with(driver, row)
        self.assertEqual(2, wait.calls)

    def test_case_import_uses_matching_row_select_action_without_confirm_dialog(self):
        from civilpower import CivilpowerTaskPlan, _import_work_log_case

        plan = CivilpowerTaskPlan(
            task_id="civilpower-case-row-action",
            case_id="EMS-20260826-01",
            case_address="桃園市大園區測試路1號",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/26",
            out_time="1000",
            in_date="2026/08/26",
            in_time="1021",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="3.救護義消協勤:張贊鏡",
        )
        driver = mock.Mock()
        wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.is_selected.return_value = True
        wait.until.return_value = checkbox
        dialog = mock.Mock()
        row = mock.Mock()

        with mock.patch("civilpower._set_input"), mock.patch(
            "civilpower._open_selection_dialog", return_value=dialog
        ), mock.patch("civilpower._wait_for_dialog_row", return_value=row) as wait_for_row, mock.patch(
            "civilpower._click_dialog_row_action", return_value=True
        ) as click_action, mock.patch("civilpower._wait_for_dialog_close") as wait_for_close, mock.patch(
            "civilpower._confirm_dialog"
        ) as confirm:
            _import_work_log_case(driver, wait, plan)

        wait_for_row.assert_called_once_with(wait, dialog, ["EMS-20260826-01"])
        click_action.assert_called_once_with(driver, row, "選取")
        wait_for_close.assert_called_once_with(wait, dialog)
        confirm.assert_not_called()

    def test_case_import_uses_reason_and_dispatch_time_when_identifiers_are_not_visible(self):
        from civilpower import CivilpowerTaskPlan, _import_work_log_case

        plan = CivilpowerTaskPlan(
            task_id="civilpower-case-reason-time",
            case_id="20260826103603012",
            case_address="桃園市觀音區東大路147巷21號",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/26",
            out_time="1036",
            in_date="2026/08/26",
            in_time="1147",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="3.救護義消協勤:張贊鏡",
            case_reason="創傷",
        )
        driver = mock.Mock()
        wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.is_selected.return_value = True
        wait.until.return_value = checkbox
        dialog = mock.Mock()
        row = mock.Mock()

        with mock.patch("civilpower._set_input"), mock.patch(
            "civilpower._open_selection_dialog", return_value=dialog
        ), mock.patch(
            "civilpower._wait_for_dialog_row",
            side_effect=[RuntimeError("id missing"), RuntimeError("address missing"), row],
        ) as wait_for_row, mock.patch(
            "civilpower._click_dialog_row_action", return_value=True
        ), mock.patch("civilpower._wait_for_dialog_close"), mock.patch("civilpower._confirm_dialog"):
            _import_work_log_case(driver, wait, plan)

        self.assertEqual(
            [
                mock.call(wait, dialog, ["20260826103603012"]),
                mock.call(wait, dialog, ["桃園市觀音區東大路147巷21號"]),
                mock.call(wait, dialog, ["創傷", "1036"]),
            ],
            wait_for_row.call_args_list,
        )

    def test_dialog_row_action_clicks_the_visible_exact_select_button(self):
        from civilpower import _click_dialog_row_action

        driver = mock.Mock()
        row = mock.Mock()
        select_button = mock.Mock()
        select_button.get_attribute.side_effect = lambda name: {"value": "選取"}.get(name, "")
        select_button.text = ""
        select_button.is_displayed.return_value = True
        select_button.is_enabled.return_value = True
        row.find_elements.return_value = [select_button]

        self.assertTrue(_click_dialog_row_action(driver, row, "選取"))

        select_button.click.assert_called_once()

    def test_dialog_row_dispatches_mouse_events_when_native_click_is_blocked(self):
        from civilpower import _click_dialog_row

        driver = mock.Mock()
        row = mock.Mock()
        row.find_elements.return_value = []
        row.click.side_effect = RuntimeError("click intercepted")
        driver.execute_script.return_value = True

        _click_dialog_row(driver, row)

        driver.execute_script.assert_called_once()
        self.assertIs(row, driver.execute_script.call_args.args[1])

    def test_confirm_dialog_accepts_an_already_closed_selection_window(self):
        from civilpower import _confirm_dialog

        driver = mock.Mock()
        dialog = mock.Mock()
        dialog.is_displayed.return_value = False

        _confirm_dialog(driver, mock.Mock(), dialog)

        driver.find_elements.assert_not_called()

    def test_confirm_dialog_accepts_visible_role_button_with_confirm_selection_title(self):
        from civilpower import _confirm_dialog

        class ImmediateWait:
            def until(self, predicate):
                return predicate(driver)

        driver = mock.Mock()
        dialog = mock.Mock()
        dialog.is_displayed.return_value = True
        candidate = mock.Mock()
        candidate.get_attribute.side_effect = lambda name: {"value": "", "title": "確認選取"}.get(name, "")
        candidate.text = ""
        candidate.is_displayed.return_value = True
        candidate.is_enabled.return_value = True
        driver.find_elements.return_value = [candidate]

        _confirm_dialog(driver, ImmediateWait(), dialog)

        candidate.click.assert_called_once()
        selector = driver.find_elements.call_args.args[1]
        self.assertIn("[role='button']", selector)

    def test_wait_after_save_confirms_a_visible_success_dialog(self):
        from civilpower import _wait_after_save

        class ImmediateWait:
            def until(self, predicate):
                return predicate(driver)

        driver = mock.Mock()

        with mock.patch("civilpower._element_displayed", return_value=False), mock.patch(
            "civilpower._dismiss_save_success_dialog"
        ) as dismiss:
            _wait_after_save(driver, ImmediateWait(), "#jqxAddWindow")

        dismiss.assert_called_once_with(driver, mock.ANY)

    def test_success_dialog_clicks_visible_exact_confirm_button(self):
        from civilpower import _dismiss_save_success_dialog

        driver = mock.Mock()
        wait = mock.Mock()
        dialog = mock.Mock()
        confirm_button = mock.Mock()
        confirm_button.get_attribute.side_effect = lambda name: {"value": "確定"}.get(name, "")
        confirm_button.text = ""
        confirm_button.is_displayed.return_value = True
        confirm_button.is_enabled.return_value = True
        dialog.find_elements.return_value = [confirm_button]

        with mock.patch("civilpower._visible_save_success_dialog", return_value=dialog), mock.patch(
            "civilpower._wait_for_dialog_close"
        ) as wait_for_close:
            _dismiss_save_success_dialog(driver, wait)

        confirm_button.click.assert_called_once()
        wait_for_close.assert_called_once_with(wait, dialog)

    def test_io_person_selection_waits_for_person_value_after_confirming_dialog(self):
        from civilpower import CivilpowerTaskPlan, _select_io_person

        plan = CivilpowerTaskPlan(
            task_id="civilpower-person-wait",
            case_id="",
            case_address="",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/19",
            out_time="1500",
            in_date="2026/08/19",
            in_time="",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="",
        )
        driver = mock.Mock()
        wait = object()
        dialog = mock.Mock()
        row = mock.Mock()

        with mock.patch("civilpower._click") as click, mock.patch(
            "civilpower._wait_for_io_person_dialog_row",
            return_value=(dialog, row),
        ) as wait_for_row, mock.patch("civilpower._click_dialog_row") as click_row, mock.patch(
            "civilpower._confirm_dialog"
        ) as confirm, mock.patch("civilpower._wait_for_io_person_value") as wait_for_value:
            _select_io_person(driver, wait, plan)

        click.assert_called_once_with(wait, "#btn_AddSltMan")
        wait_for_row.assert_called_once_with(
            driver,
            wait,
            ["大園救護分隊", "張贊鏡", "小隊長"],
        )
        click_row.assert_called_once_with(driver, row)
        confirm.assert_called_once_with(driver, wait, dialog)
        wait_for_value.assert_called_once_with(driver, wait, "張贊鏡")

    def test_io_record_reapplies_linked_fields_after_person_selection_before_save(self):
        from civilpower import CivilpowerTaskPlan, OUT_STATUS, _ensure_io_record

        plan = CivilpowerTaskPlan(
            task_id="civilpower-reapply-fields",
            case_id="",
            case_address="",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/19",
            out_time="1500",
            in_date="2026/08/19",
            in_time="",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="",
        )
        steps: list[str] = []

        with mock.patch("civilpower._find_io_record", side_effect=[False, True]) as find_io_record, mock.patch(
            "civilpower._open_io_work_log"
        ) as open_io_work_log, mock.patch(
            "civilpower._click", side_effect=lambda _wait, selector: steps.append(f"click:{selector}")
        ), mock.patch(
            "civilpower._wait_visible", side_effect=lambda _wait, selector: steps.append(f"visible:{selector}")
        ), mock.patch(
            "civilpower._wait_for_jqx_combobox_option_signature",
            side_effect=lambda _driver, _wait, _selector: steps.append("serve-unit-initialized") or (),
        ), mock.patch(
            "civilpower._select_jqx_combobox",
            side_effect=lambda _driver, _wait, selector, value: steps.append(f"combo:{selector}={value}"),
        ), mock.patch(
            "civilpower._wait_for_io_form_dependencies",
            side_effect=lambda _driver, _wait, _plan, **_kwargs: steps.append("dependencies"),
        ), mock.patch(
            "civilpower._select_io_person", side_effect=lambda _driver, _wait, _plan: steps.append("person")
        ), mock.patch(
            "civilpower._set_input", side_effect=lambda _wait, selector, value: steps.append(f"input:{selector}={value}")
        ), mock.patch(
            "civilpower._select_option_containing",
            side_effect=lambda _wait, selector, value: steps.append(f"option:{selector}={value}"),
        ), mock.patch(
            "civilpower._wait_for_io_record_form_values", side_effect=lambda _driver, _wait, _plan, _status: steps.append("verify")
        ), mock.patch(
            "civilpower._wait_after_save", side_effect=lambda _driver, _wait, selector: steps.append(f"saved:{selector}")
        ):
            checkpoint: dict[str, object] = {}
            _ensure_io_record(object(), plan, OUT_STATUS, checkpoint, cancel_check=None)

        self.assertEqual(
            [
                "click:#btn_Add",
                "visible:#jqxAddWindow",
                "serve-unit-initialized",
                "combo:#txt_AddUnit=大園救護分隊",
                "dependencies",
                "combo:#txt_AddServeUnit=新坡分隊",
                "person",
                "input:#txt_AddLogDate=2026/08/19",
                "input:#txt_AddLogHour=15",
                "input:#txt_AddLogMin=00",
                "combo:#txt_AddServeUnit=新坡分隊",
                "option:#ddl_AddIO=出",
                "input:#txt_AddReason=救護出勤",
                "verify",
                "click:#btn_IOWorkLogAdd",
                "saved:#jqxAddWindow",
            ],
            steps,
        )
        open_io_work_log.assert_not_called()
        self.assertTrue(checkpoint["out_verified"])
        find_io_record.assert_has_calls(
            [
                mock.call(mock.ANY, plan, OUT_STATUS, wait_for_match=False),
                mock.call(mock.ANY, plan, OUT_STATUS, wait_for_match=True, reload_page=False),
            ]
        )

    def test_in_record_edits_the_existing_manual_return_time_before_adding_another(self):
        from civilpower import CivilpowerTaskPlan, _ensure_correct_in_io_record

        manual_plan = CivilpowerTaskPlan(
            task_id="civilpower-correct-return",
            case_id="20260826103603012",
            case_address="桃園市觀音區東大路147巷21號",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/26",
            out_time="1036",
            in_date="2026/08/26",
            in_time="1147",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="3.救護義消協勤:張贊鏡",
        )
        official_plan = replace(manual_plan, in_time="1146")
        driver = object()
        original_row = object()
        corrected_row = object()

        with mock.patch(
            "civilpower._find_io_record_row",
            side_effect=[None, original_row, corrected_row],
        ) as find_row, mock.patch("civilpower._edit_io_record_time") as edit_time, mock.patch(
            "civilpower._ensure_io_record"
        ) as ensure_io:
            checkpoint: dict[str, object] = {}
            _ensure_correct_in_io_record(
                driver,
                official_plan,
                manual_plan,
                checkpoint,
                cancel_check=None,
                can_create_in_record=True,
            )

        edit_time.assert_called_once_with(driver, original_row, official_plan, cancel_check=None)
        ensure_io.assert_not_called()
        self.assertTrue(checkpoint["in_verified"])
        self.assertEqual(
            [
                mock.call(mock.ANY, official_plan, "入", wait_for_match=False, raise_on_timeout=True),
                mock.call(
                    mock.ANY,
                    manual_plan,
                    "入",
                    wait_for_match=False,
                    reload_page=False,
                    raise_on_timeout=True,
                ),
                mock.call(
                    mock.ANY,
                    official_plan,
                    "入",
                    wait_for_match=True,
                    reload_page=False,
                    raise_on_timeout=True,
                ),
            ],
            find_row.call_args_list,
        )

    def test_in_record_without_the_original_account_is_not_added_after_a_prior_out_record(self):
        from civilpower import CivilpowerTaskPlan, _ensure_correct_in_io_record

        manual_plan = CivilpowerTaskPlan(
            task_id="civilpower-missing-original-account",
            case_id="20260826103603012",
            case_address="桃園市觀音區東大路147巷21號",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/26",
            out_time="1036",
            in_date="2026/08/26",
            in_time="1147",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="3.救護義消協勤:張贊鏡",
        )
        official_plan = replace(manual_plan, in_time="1146")

        with mock.patch("civilpower._find_io_record_row", side_effect=[None, None]), mock.patch(
            "civilpower._ensure_io_record"
        ) as ensure_io:
            with self.assertRaisesRegex(RuntimeError, "原登打帳號"):
                _ensure_correct_in_io_record(
                    object(),
                    official_plan,
                    manual_plan,
                    {},
                    cancel_check=None,
                    can_create_in_record=False,
                )

        ensure_io.assert_not_called()

    def test_in_record_edit_changes_only_the_return_date_and_time_fields(self):
        from civilpower import CivilpowerTaskPlan, _edit_io_record_time

        plan = CivilpowerTaskPlan(
            task_id="civilpower-edit-return-time",
            case_id="20260826103603012",
            case_address="桃園市觀音區東大路147巷21號",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/26",
            out_time="1036",
            in_date="2026/08/26",
            in_time="1146",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="3.救護義消協勤:張贊鏡",
        )
        wait = object()

        with mock.patch("civilpower._click_dialog_row_action", return_value=True), mock.patch(
            "civilpower.WebDriverWait", return_value=wait
        ), mock.patch("civilpower._wait_visible"), mock.patch("civilpower._set_input") as set_input, mock.patch(
            "civilpower._select_jqx_combobox"
        ) as select_combobox, mock.patch("civilpower._wait_for_io_edit_form_values"), mock.patch(
            "civilpower._click"
        ) as click, mock.patch("civilpower._wait_after_save"):
            _edit_io_record_time(object(), object(), plan, cancel_check=None)

        self.assertEqual(
            [
                mock.call(wait, "#txt_EditLogDate", "2026/08/26"),
                mock.call(wait, "#txt_EditLogHour", "11"),
                mock.call(wait, "#txt_EditLogMin", "46"),
            ],
            set_input.call_args_list,
        )
        select_combobox.assert_called_once_with(mock.ANY, wait, "#txt_EditServeUnit", "新坡分隊")
        click.assert_called_once_with(wait, "#btn_IOWorkLogEdit")

    def test_io_record_lookup_waits_for_matching_row_when_requested(self):
        from civilpower import CivilpowerTaskPlan, OUT_STATUS, _find_io_record

        plan = CivilpowerTaskPlan(
            task_id="civilpower-delayed-query",
            case_id="",
            case_address="",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/25",
            out_time="2041",
            in_date="2026/08/25",
            in_time="2210",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="",
        )
        driver = object()
        row = object()

        class PollingWait:
            def until(self, condition):
                for _ in range(2):
                    result = condition(driver)
                    if result:
                        return result
                raise AssertionError("等待中的民力查詢未找到資料")

        with mock.patch("civilpower._open_io_work_log"), mock.patch(
            "civilpower.WebDriverWait", return_value=PollingWait()
        ), mock.patch("civilpower._set_if_present"), mock.patch(
            "civilpower._select_option_containing_if_present"
        ), mock.patch("civilpower._click_if_present"), mock.patch(
            "civilpower._matching_table_rows", side_effect=[[], [row]]
        ):
            self.assertTrue(_find_io_record(driver, plan, OUT_STATUS, wait_for_match=True))

    def test_io_record_lookup_waits_for_query_refresh_before_scanning_rows(self):
        from civilpower import CivilpowerTaskPlan, OUT_STATUS, _find_io_record

        plan = CivilpowerTaskPlan(
            task_id="civilpower-query-refresh",
            case_id="",
            case_address="",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/25",
            out_time="2041",
            in_date="2026/08/25",
            in_time="2210",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="",
        )
        driver = object()
        wait = mock.Mock()
        row = object()

        with mock.patch("civilpower._open_io_work_log"), mock.patch(
            "civilpower.WebDriverWait", return_value=wait
        ), mock.patch("civilpower._set_if_present"), mock.patch(
            "civilpower._select_option_containing_if_present"
        ), mock.patch("civilpower._arm_io_query_refresh_marker", return_value="query-marker"), mock.patch(
            "civilpower._click_if_present"
        ), mock.patch("civilpower._wait_for_io_query_refresh") as wait_for_refresh, mock.patch(
            "civilpower._matching_table_rows", return_value=[row]
        ):
            self.assertTrue(_find_io_record(driver, plan, OUT_STATUS))

        wait_for_refresh.assert_called_once_with(driver, wait, "query-marker")

    def test_io_query_refresh_waits_until_the_result_marker_is_replaced(self):
        from civilpower import _arm_io_query_refresh_marker, _wait_for_io_query_refresh

        class PollingWait:
            def until(self, condition):
                for _ in range(2):
                    result = condition(driver)
                    if result:
                        return result
                raise AssertionError("查詢結果未替換標記")

        driver = mock.Mock()
        driver.execute_script.return_value = True
        driver.find_elements.side_effect = [[object()], []]

        marker_id = _arm_io_query_refresh_marker(driver)
        _wait_for_io_query_refresh(driver, PollingWait(), marker_id)

        self.assertTrue(marker_id.startswith("civilpower-query-"))
        self.assertEqual(2, driver.find_elements.call_count)

    def test_matching_table_rows_reacquires_rows_after_table_refresh(self):
        from selenium.common.exceptions import StaleElementReferenceException

        from civilpower import _matching_table_rows

        class Row:
            def __init__(
                self,
                text: str,
                *,
                stale_on_text: bool = False,
                stale_on_display: bool = False,
            ) -> None:
                self.id = ""
                self._text = text
                self._stale_on_text = stale_on_text
                self._stale_on_display = stale_on_display

            def is_displayed(self) -> bool:
                if self._stale_on_display:
                    raise StaleElementReferenceException("table was refreshed")
                return True

            def find_elements(self, *_args) -> list[object]:
                return [object()]

            @property
            def text(self) -> str:
                if self._stale_on_text:
                    raise StaleElementReferenceException("table was refreshed")
                return self._text

        matching_row = Row("張贊鏡 救護出勤 1036")
        for stale_row in (
            Row("張贊鏡 救護出勤 1036", stale_on_text=True),
            Row("張贊鏡 救護出勤 1036", stale_on_display=True),
        ):
            with self.subTest(stale_row=stale_row):
                driver = mock.Mock()
                driver.find_elements.side_effect = [[stale_row], [matching_row]]

                self.assertEqual([matching_row], _matching_table_rows(driver, ["張贊鏡", "救護出勤", "1036"]))

    def test_io_record_lookup_timeout_is_not_a_matching_record(self):
        from selenium.common.exceptions import TimeoutException

        from civilpower import CivilpowerTaskPlan, OUT_STATUS, _find_io_record

        plan = CivilpowerTaskPlan(
            task_id="civilpower-query-timeout",
            case_id="",
            case_address="",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/25",
            out_time="2041",
            in_date="2026/08/25",
            in_time="2210",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="",
        )
        wait = mock.Mock()
        wait.until.side_effect = TimeoutException()

        with mock.patch("civilpower._open_io_work_log"), mock.patch(
            "civilpower.WebDriverWait", return_value=wait
        ), mock.patch("civilpower._set_if_present"), mock.patch(
            "civilpower._select_option_containing_if_present"
        ), mock.patch("civilpower._click_if_present"), mock.patch(
            "civilpower._matching_table_rows", return_value=[]
        ):
            self.assertFalse(_find_io_record(object(), plan, OUT_STATUS, wait_for_match=True))
            with self.assertRaisesRegex(RuntimeError, "查詢逾時"):
                _find_io_record(
                    object(),
                    plan,
                    OUT_STATUS,
                    wait_for_match=True,
                    raise_on_timeout=True,
                )

    def test_io_record_lookup_reuses_open_list_page_after_save(self):
        from civilpower import CivilpowerTaskPlan, OUT_STATUS, _find_io_record

        plan = CivilpowerTaskPlan(
            task_id="civilpower-reuse-io-list",
            case_id="",
            case_address="",
            member_id="member-1",
            member_name="張贊鏡",
            member_title="小隊長",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            out_date="2026/08/19",
            out_time="1500",
            in_date="2026/08/19",
            in_time="",
            out_reason="救護出勤",
            in_reason="救護返隊",
            duty_status_line="",
        )
        driver = mock.Mock()
        row = mock.Mock()

        with mock.patch("civilpower._open_io_work_log") as open_io_work_log, mock.patch(
            "civilpower._set_if_present"
        ), mock.patch("civilpower._select_option_containing_if_present"), mock.patch(
            "civilpower._click_if_present"
        ), mock.patch(
            "civilpower._arm_io_query_refresh_marker", return_value=""
        ), mock.patch("civilpower._matching_table_rows", return_value=[row]):
            self.assertTrue(_find_io_record(driver, plan, OUT_STATUS, reload_page=False))

        open_io_work_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
