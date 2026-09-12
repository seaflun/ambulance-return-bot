from __future__ import annotations

import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import civilpower
from tests import test_civilpower


class CivilpowerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.request = test_civilpower.CivilpowerPlanTests._enabled_request()
        self.plan = civilpower.build_civilpower_task_plan(self.request)

    def query_context(self, rows, *, confirmed=True):
        stack = ExitStack()
        for name in ("_open_work_log_form", "_open_io_work_log", "_set_if_present",
                     "_select_option_containing_if_present", "_click_if_present"):
            stack.enter_context(mock.patch("civilpower." + name))
        stack.enter_context(mock.patch("civilpower._io_result_grid_signature", return_value="old"))
        stack.enter_context(mock.patch("civilpower._io_result_grid_sentinel", return_value=None))
        stack.enter_context(mock.patch("civilpower._wait_for_io_query_result_grid", return_value=confirmed))
        stack.enter_context(mock.patch("civilpower._table_rows", return_value=rows))
        return stack

    def test_import_accepts_unpadded_hour_and_minute_fields(self):
        for hour, minute in [("9", "5"), ("0", "0"), ("23", "9"), ("9", "15")]:
            with self.subTest(hour=hour, minute=minute):
                hhmm = f"{int(hour):02d}{int(minute):02d}"
                plan = replace(self.plan, out_time=hhmm, in_time=hhmm)
                values = {
                    "#txt_AddDisDate": plan.out_date,
                    "#txt_AddDisHour": hour,
                    "#txt_AddDisMin": minute,
                    "#txt_AddBackDate": plan.in_date,
                    "#txt_AddBackHour": hour,
                    "#txt_AddBackMin": minute,
                    "#txt_AddStat": plan.duty_status_line,
                }
                with mock.patch("civilpower._control_value", side_effect=lambda _, key: values[key]):
                    imported = civilpower._assert_imported_work_log_values(object(), plan)
                self.assertEqual(hhmm, imported.in_time)

    def test_import_rejects_missing_or_invalid_individual_time_fields(self):
        for hour, minute in [("", "0905"), ("0905", ""), ("24", "00"), ("9", "60"), ("-1", "05")]:
            with self.subTest(hour=hour, minute=minute):
                values = {
                    "#txt_AddDisDate": self.plan.out_date,
                    "#txt_AddDisHour": hour,
                    "#txt_AddDisMin": minute,
                    "#txt_AddStat": self.plan.duty_status_line,
                    "#txt_AddBackDate": self.plan.in_date,
                    "#txt_AddBackHour": "14",
                    "#txt_AddBackMin": "34",
                }
                with mock.patch("civilpower._control_value", side_effect=lambda _, key: values[key]):
                    with self.assertRaisesRegex(RuntimeError, "派遣時間"):
                        civilpower._assert_imported_work_log_values(object(), self.plan)

    def test_form_readback_accepts_only_equivalent_numeric_values(self):
        self.assertTrue(civilpower._same_value("9", "09"))
        self.assertTrue(civilpower._same_value("0", "00"))
        self.assertFalse(civilpower._same_value("19", "09"))
        self.assertFalse(civilpower._same_value("", "00"))
        self.assertFalse(civilpower._same_value("9x", "09"))

    def test_row_matching_never_uses_date_or_address_digits_as_a_time(self):
        rows = [
            SimpleNamespace(text="測試義消 2026/09/05 16:20"),
            SimpleNamespace(text="測試義消 觀音區甲路0905號 16:20"),
            SimpleNamespace(text="測試義消 2026/09/05 9:5"),
        ]
        with mock.patch("civilpower._table_rows", return_value=rows):
            self.assertEqual([rows[2]], civilpower._matching_table_rows(object(), ["測試義消", "0905"]))
        self.assertFalse(civilpower._token_matches("桃園市大園區乙路147號", "桃園市觀音區甲路147號"))
        self.assertFalse(civilpower._token_matches("202609120905170150", "20260912090517015"))

    def test_work_log_recheck_requires_case_datetime_for_address_fallback(self):
        wrong = SimpleNamespace(text=f"{self.plan.member_name} {self.plan.case_address} 2026/08/19 07:00 OTHER-CASE")
        correct = SimpleNamespace(text=f"{self.plan.member_name} {self.plan.case_address} 2026/8/19 12:32")
        with self.query_context([wrong]):
            self.assertFalse(civilpower._find_work_log_record(object(), self.plan))
        with self.query_context([wrong, correct]):
            self.assertTrue(civilpower._find_work_log_record(object(), self.plan))

    def test_io_lookup_rejects_wrong_date_and_does_not_mix_two_datetimes(self):
        rows = [
            SimpleNamespace(text=f"測試義消 大園救護分隊 新坡分隊 救護出勤 2026/08/18 12:32"),
            SimpleNamespace(text=f"測試義消 大園救護分隊 新坡分隊 救護出勤 2026/08/19 07:00 2026/08/18 12:32"),
        ]
        with self.query_context(rows):
            self.assertFalse(civilpower._find_io_record(object(), self.plan, civilpower.OUT_STATUS))

    def test_existing_row_cannot_confirm_an_unchanged_query_grid(self):
        from selenium.common.exceptions import TimeoutException

        class OneCheckWait:
            def until(self, predicate):
                if predicate(object()):
                    return True
                raise TimeoutException("query still pending")

        with mock.patch("civilpower.WebDriverWait", return_value=OneCheckWait()), mock.patch(
            "civilpower._io_result_grid_signature", return_value="unchanged"
        ), mock.patch("civilpower._matching_table_rows", return_value=[object()]):
            self.assertFalse(civilpower._wait_for_io_query_result_grid(object(), ["1232"], "unchanged"))

    def test_unconfirmed_work_log_query_cannot_start_an_add(self):
        with self.query_context([], confirmed=False), mock.patch("civilpower._click") as click:
            with self.assertRaisesRegex(RuntimeError, "查詢結果未完成更新"):
                civilpower._ensure_work_log(object(), self.request, self.plan, {}, cancel_check=None)
        click.assert_not_called()

    def test_unconfirmed_io_query_cannot_reuse_an_existing_row(self):
        row = SimpleNamespace(text="測試義消 大園救護分隊 新坡分隊 救護出勤 2026/08/19 12:32")
        with self.query_context([row], confirmed=False):
            with self.assertRaisesRegex(RuntimeError, "查詢結果未完成更新"):
                civilpower._find_io_record(object(), self.plan, civilpower.OUT_STATUS, require_query_confirmation=True)

    def test_out_submit_persists_original_account_before_click_and_survives_failed_readback(self):
        import json

        def login(**kwargs):
            kwargs["on_login_success"]("original-operator")
            return object()

        with TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            path = civilpower._task_checkpoint_path(root, self.plan.task_id)
            observed = []

            def click(_wait, selector):
                if selector == "#btn_IOWorkLogAdd":
                    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                    observed.append(payload)

            stack.enter_context(mock.patch("civilpower.login_civilpower_and_get_driver", side_effect=login))
            stack.enter_context(mock.patch("civilpower._find_io_record", side_effect=[False, False]))
            stack.enter_context(mock.patch("civilpower._click", side_effect=click))
            for name in ("_wait_visible", "_select_jqx_combobox", "_wait_for_io_form_dependencies",
                         "_select_io_person", "_set_input", "_select_option_containing",
                         "_wait_for_io_record_form_values", "_wait_after_save", "mark_driver_operation_active"):
                stack.enter_context(mock.patch("civilpower." + name))
            stack.enter_context(mock.patch("civilpower.capture_failure_artifacts", return_value={}))
            result = civilpower.run_civilpower_task(self.request, root)

            self.assertEqual("volunteer_assist_failed", result.status)
            self.assertEqual(1, len(observed))
            self.assertEqual("original-operator", observed[0].get("civilpower_login_account"))
            self.assertEqual("pending_verification", observed[0].get("out_save_state"))
            checkpoint = civilpower._load_task_checkpoint(root, self.plan)
            self.assertEqual("original-operator", checkpoint.get("civilpower_login_account"))

    def test_retry_uses_persisted_original_account_and_finishes_existing_out_record(self):
        checkpoint = {
            "fingerprint": civilpower._plan_fingerprint(self.plan),
            "civilpower_login_account": "original-operator",
            "out_save_state": "pending_verification",
        }

        def login(**kwargs):
            self.assertEqual("original-operator", kwargs["required_user_id"])
            kwargs["on_login_success"]("original-operator")
            return object()

        with TemporaryDirectory() as directory, mock.patch("civilpower._load_task_checkpoint", return_value=checkpoint), mock.patch(
            "civilpower.login_civilpower_and_get_driver", side_effect=login
        ), mock.patch("civilpower._find_io_record", return_value=True), mock.patch(
            "civilpower._ensure_correct_in_io_record"
        ) as inbound, mock.patch("civilpower._ensure_work_log", return_value=self.plan), mock.patch(
            "civilpower.mark_driver_operation_active"
        ):
            result = civilpower.run_civilpower_task(self.request, Path(directory))
        self.assertEqual("volunteer_assist_saved", result.status)
        self.assertTrue(inbound.call_args.kwargs["can_create_in_record"])
        self.assertEqual("verified", checkpoint.get("out_save_state"))

    def test_work_log_recheck_does_not_accept_a_conflicting_visible_case_id(self):
        plan = replace(self.plan, case_id="20260819123200001")
        row = SimpleNamespace(text=f"測試義消 {plan.case_address} 2026/08/19 12:32 20260819123200002")
        with self.query_context([row]):
            self.assertFalse(civilpower._find_work_log_record(object(), plan))

    def test_earlier_official_dispatch_time_is_saved_and_used_for_retry_lookup(self):
        checkpoint = {}
        values = {
            "#txt_AddDisDate": self.plan.out_date,
            "#txt_AddDisHour": "12", "#txt_AddDisMin": "31",
            "#txt_AddBackDate": self.plan.in_date,
            "#txt_AddBackHour": "14", "#txt_AddBackMin": "34",
            "#txt_AddStat": self.plan.duty_status_line,
        }
        saved_checkpoints = []
        row = SimpleNamespace(text=f"測試義消 {self.plan.case_address} 2026/08/19 12:31")
        query_rows = []

        def click(_wait, selector):
            if selector == "#btn_WorkLogAdd":
                query_rows.append(row)

        with self.query_context(query_rows), ExitStack() as stack:
            for name in ("_wait_visible", "_wait_for_work_log_add_controls", "_select_out_io_record_for_work_log",
                         "_import_work_log_case", "_wait_after_save"):
                stack.enter_context(mock.patch("civilpower." + name))
            stack.enter_context(mock.patch("civilpower._control_value", side_effect=lambda _, key: values[key]))
            stack.enter_context(mock.patch("civilpower._click", side_effect=click))
            civilpower._ensure_work_log(
                object(), self.request, self.plan, checkpoint, cancel_check=None,
                before_save=lambda: saved_checkpoints.append(dict(checkpoint)),
            )
            with mock.patch("civilpower._click") as retry_click:
                civilpower._ensure_work_log(object(), self.request, self.plan, checkpoint, cancel_check=None)
            retry_click.assert_not_called()
        self.assertEqual("1231", saved_checkpoints[0]["civilpower_dispatch_time"])
        self.assertEqual("1232", self.plan.out_time)

    def test_query_waits_for_pending_ajax_after_the_grid_changes(self):
        polls = []
        driver = mock.Mock()

        def ajax_state(_script):
            polls.append(True)
            return len(polls) == 1

        class PollingWait:
            def until(self, predicate):
                for _ in range(3):
                    if predicate(driver):
                        return True
                raise AssertionError("query did not settle")

        driver.execute_script.side_effect = ajax_state
        with mock.patch("civilpower.WebDriverWait", return_value=PollingWait()), mock.patch(
            "civilpower._io_result_grid_signature", return_value="changed"
        ):
            self.assertTrue(civilpower._wait_for_io_query_result_grid(driver, [], "old"))
        self.assertEqual(2, len(polls))

    def test_ambiguous_case_id_does_not_fall_back_to_a_weaker_match(self):
        from selenium.webdriver.support.ui import WebDriverWait

        rows = [SimpleNamespace(text=self.plan.case_id), SimpleNamespace(text=self.plan.case_id)]
        checkbox = mock.Mock()
        checkbox.is_selected.return_value = True
        driver = mock.Mock()
        driver.find_element.return_value = checkbox
        with mock.patch("civilpower._set_input"), mock.patch(
            "civilpower._open_selection_dialog", return_value=object()
        ), mock.patch("civilpower._table_rows", return_value=rows) as table_rows, mock.patch(
            "civilpower._click_dialog_row_action"
        ) as click:
            with self.assertRaisesRegex(RuntimeError, "多筆"):
                civilpower._import_work_log_case(driver, WebDriverWait(driver, 0), self.plan)
        self.assertEqual(1, table_rows.call_count)
        click.assert_not_called()
