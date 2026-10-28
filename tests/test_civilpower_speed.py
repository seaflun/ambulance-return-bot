from __future__ import annotations

import os
import json
import shutil
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import civilpower
from tests import test_civilpower
from tests import test_civilpower_pagination as pagination


class CivilpowerSpeedTests(unittest.TestCase):
    def setUp(self):
        self.request = test_civilpower.CivilpowerPlanTests._enabled_request()
        self.plan = civilpower.build_civilpower_task_plan(self.request)

    def test_batch_lookup_uses_visible_dom_text_without_reading_each_element(self):
        row = mock.Mock()
        driver = mock.Mock()
        driver.execute_script.return_value = {
            "rows": [{"element": row, "text": "測試義消 115/08/19 下午 12:32", "visible": True}],
            "pages": ["1"],
        }
        matched = civilpower._matching_table_rows(driver, ["測試義消", "2026/08/19 12:32"])
        self.assertEqual([row], matched)
        driver.find_elements.assert_not_called()
        row.is_displayed.assert_not_called()
        row.find_elements.assert_not_called()
        self.assertEqual(1, driver.execute_script.call_count)

    def test_roster_batch_uses_cell_snapshot_without_element_round_trips(self):
        row = mock.Mock()
        driver = mock.Mock()
        driver.execute_script.return_value = {
            "rows": [{"element": row, "text": "大園救護分隊 小隊長 測試義消", "visible": True,
                      "cells": ["大園救護分隊", "小隊長", "測試義消"]}],
            "pages": [],
        }
        self.assertEqual([{"unit": "大園救護分隊", "title": "小隊長", "name": "測試義消"}],
                         civilpower._read_civilpower_roster_page(driver))
        driver.find_elements.assert_not_called()
        row.find_elements.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the DOM snapshot")
    def test_dom_snapshot_keeps_hidden_cell_index_with_empty_text(self):
        driver = mock.Mock()
        driver.execute_script.return_value = False
        civilpower._table_page_snapshot(driver)
        source = driver.execute_script.call_args.args[0]
        harness = r"""
const shown = {display: 'table-cell', visibility: 'visible', opacity: '1'};
const cell = (text, hidden) => ({innerText: text, parentElement: null,
    getClientRects: () => hidden ? [] : [{}],
    style: hidden ? {...shown, display: 'none'} : shown});
const cells = [cell('大園救護分隊', false), cell('不應讀取的隱藏文字', true), cell('測試義消', false)];
const row = {innerText: '大園救護分隊 測試義消', parentElement: null, style: shown,
    getClientRects: () => [{}], querySelectorAll: () => cells};
cells.forEach(cell => {cell.parentElement = row;});
global.document = {querySelectorAll: selector => selector === '.pagination .active' ? [] : [row]};
global.getComputedStyle = element => element.style;
const result = new Function(process.argv[1])(null);
process.stdout.write(JSON.stringify(result.rows[0].cells));
"""
        result = subprocess.run([shutil.which("node"), "-e", harness, source],
                                capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(["大園救護分隊", "", "測試義消"], json.loads(result.stdout))

    def test_work_log_checks_all_identity_candidates_with_one_pagination_scan(self):
        row = SimpleNamespace(text=f"測試義消 {self.plan.case_address} 115/08/19 12:32")
        browser = pagination.PagedResults([[], [row], []])
        with pagination.CivilpowerPaginationTests().query_context(browser):
            self.assertTrue(civilpower._find_work_log_record(browser, self.plan))
        self.assertEqual([0, 1, 2, 1], browser.visited)
        self.assertEqual(1, browser.page)

    def test_dry_run_stops_outbound_form_before_any_save_or_checkpoint(self):
        from datetime import datetime, timedelta

        start = datetime.strptime(self.plan.out_date + self.plan.out_time, "%Y/%m/%d%H%M")
        end = datetime.strptime(self.plan.in_date + self.plan.in_time, "%Y/%m/%d%H%M")
        window = civilpower.CivilpowerCaseWindow(self.plan.case_id, start, end)
        duty = civilpower.CivilpowerIoRecord("1", start - timedelta(hours=1), self.plan.serve_unit,
                                            self.plan.home_unit, self.plan.member_name, self.plan.member_title, "服勤")
        with mock.patch.dict(os.environ, {"SAVE_CIVILPOWER_RECORD": "false"}), TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch("civilpower._resolve_official_case_plan", return_value=(self.plan, window)))
            stack.enter_context(mock.patch("civilpower._query_civilpower_records", side_effect=lambda *args, **kwargs: [duty] if kwargs["kind"] == "io" else []))
            for name in ("mark_driver_operation_active", "_wait_visible", "_select_jqx_combobox",
                         "_wait_for_io_form_dependencies", "_select_io_person", "_set_input",
                         "_select_option_containing", "_wait_for_io_record_form_values"):
                stack.enter_context(mock.patch("civilpower." + name))
            stack.enter_context(mock.patch("civilpower._find_io_record", return_value=False))
            click = stack.enter_context(mock.patch("civilpower._click"))
            checkpoint = stack.enter_context(mock.patch("civilpower._save_task_checkpoint"))
            evidence = stack.enter_context(mock.patch("civilpower.capture_failure_artifacts"))
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_prefilled", result.status)
        self.assertEqual([mock.call(mock.ANY, "#btn_Add")], click.call_args_list)
        checkpoint.assert_not_called()
        evidence.assert_not_called()

    def test_dry_run_blocks_inbound_edit_before_submit(self):
        with mock.patch.dict(os.environ, {"SAVE_CIVILPOWER_RECORD": "false"}), ExitStack() as stack:
            stack.enter_context(mock.patch("civilpower._click_dialog_row_action", return_value=True))
            for name in ("_wait_visible", "_set_input", "_select_jqx_combobox", "_wait_for_io_edit_form_values", "_wait_after_save"):
                stack.enter_context(mock.patch("civilpower." + name))
            click = stack.enter_context(mock.patch("civilpower._click"))
            with self.assertRaisesRegex(RuntimeError, "不儲存"):
                civilpower._edit_io_record_time(object(), object(), self.plan, cancel_check=None)
        click.assert_not_called()

    def test_dry_run_blocks_work_log_before_submit(self):
        checkpoint = {}
        with mock.patch.dict(os.environ, {"SAVE_CIVILPOWER_RECORD": "false"}), ExitStack() as stack:
            stack.enter_context(mock.patch("civilpower._find_work_log_record", return_value=False))
            for name in ("_wait_visible", "_wait_for_work_log_add_controls", "_select_out_io_record_for_work_log",
                         "_import_work_log_case", "_wait_after_save"):
                stack.enter_context(mock.patch("civilpower." + name))
            stack.enter_context(mock.patch("civilpower._assert_imported_work_log_values", return_value=self.plan))
            click = stack.enter_context(mock.patch("civilpower._click"))
            before_save = mock.Mock()
            with self.assertRaisesRegex(RuntimeError, "不儲存"):
                civilpower._ensure_work_log(object(), self.request, self.plan, checkpoint,
                                           cancel_check=None, before_save=before_save)
        self.assertEqual([mock.call(mock.ANY, "#btn_Add")], click.call_args_list)
        before_save.assert_not_called()
        self.assertNotIn("work_log_save_state", checkpoint)

    def test_global_dry_run_takes_priority_over_enabled_site_save(self):
        with mock.patch.dict(os.environ, {"AUTOMATION_DRY_RUN": "true", "SAVE_CIVILPOWER_RECORD": "true"}):
            self.assertFalse(civilpower._civilpower_save_enabled())
            with self.assertRaisesRegex(RuntimeError, "不儲存"):
                civilpower._require_civilpower_save_enabled("出入登記")

    def test_dry_run_of_existing_records_never_marks_task_saved_or_persists_checkpoint(self):
        with mock.patch.dict(os.environ, {"AUTOMATION_DRY_RUN": "true"}), TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch("civilpower.mark_driver_operation_active"))
            stack.enter_context(mock.patch("civilpower._reconcile_civilpower_task", return_value=(self.plan, "已確認既有紀錄")))
            checkpoint = stack.enter_context(mock.patch("civilpower._save_task_checkpoint"))
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_prefilled", result.status)
        checkpoint.assert_not_called()

    def test_login_reports_explicit_rejection_without_waiting_for_success_timeout(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {"loginPage": True, "error": "驗證碼錯誤"}
        with self.assertRaisesRegex(RuntimeError, "驗證碼錯誤"):
            civilpower._wait_for_oa_login_result(driver, pagination.ImmediateWait(driver))

    def test_changed_page_during_return_is_still_rejected(self):
        row = SimpleNamespace(text="測試義消")
        browser = pagination.PagedResults([[row], []])
        original_click = browser.find_elements

        def links(by, selector):
            result = original_click(by, selector)
            if "rel='prev'" in selector and result:
                original = result[0].click

                def mutate():
                    original()
                    browser.pages[0] = [SimpleNamespace(text="已變動")]

                result[0].click = mutate
            return result

        browser.find_elements = links
        with pagination.CivilpowerPaginationTests().query_context(browser):
            with self.assertRaisesRegex(civilpower._IncompleteQueryError, "資料已變動"):
                civilpower._find_paginated_table_rows(browser, pagination.ImmediateWait(browser), ["測試義消"])
