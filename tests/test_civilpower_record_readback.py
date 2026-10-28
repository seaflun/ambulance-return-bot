from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import civilpower


class CivilpowerRecordReadbackTests(unittest.TestCase):
    def test_case_io_lookup_uses_typed_event_time_and_accepts_manual_reason(self):
        from tests import test_civilpower_manual_flow

        fixture = test_civilpower_manual_flow.CivilpowerManualFlowTests()
        fixture.setUp()
        old = replace(fixture.inbound, at=datetime(2026, 8, 19, 12, 30))
        with mock.patch("civilpower._query_civilpower_records", return_value=[old]):
            self.assertFalse(civilpower._case_io_record_exists(object(), fixture.plan, civilpower.IN_STATUS, fixture.window))
        with mock.patch("civilpower._query_civilpower_records", return_value=[fixture.inbound]):
            self.assertTrue(civilpower._case_io_record_exists(object(), fixture.plan, civilpower.IN_STATUS, fixture.window))

    def test_typed_table_requires_headers_and_reads_formal_record_id(self):
        headers = ["", "時間", "服勤單位", "所屬單位", "姓名", "職稱", "狀態", "事由", "值班人員", "簽核人員"]
        values = ["", "115/10/28 21:40", "新坡分隊", "大園救護分隊", "測試義消", "隊員", "出", "救護", "原登打人", ""]
        element = mock.Mock()
        element.find_elements.return_value = [SimpleNamespace(get_attribute=lambda key: "123" if key == "value" else None)]
        snapshot = {"rows": [{"cells": headers}, {"cells": values, "element": element}], "pages": []}
        driver = mock.Mock()
        with mock.patch("civilpower._table_page_snapshot", return_value=snapshot), mock.patch("civilpower._pagination_links", return_value=[]):
            records = civilpower._read_typed_record_pages(driver, kind="io")
        self.assertEqual("123", records[0].record_id)
        self.assertEqual(datetime(2026, 10, 28, 21, 40), records[0].at)

    def test_missing_table_columns_cannot_be_interpreted_as_no_records(self):
        driver = mock.Mock()
        with mock.patch("civilpower._table_page_snapshot", return_value={"rows": [{"cells": ["時間", "姓名"]}], "pages": []}):
            with self.assertRaisesRegex(RuntimeError, "欄位"):
                civilpower._read_typed_record_pages(driver, kind="io")

    def test_io_uses_event_time_and_explicit_columns_instead_of_modified_time(self):
        row = civilpower._io_record_from_columns({
            "時間": "115/10/28 21:40", "服勤單位": "新坡分隊", "所屬單位": "大園救護分隊",
            "姓名": "測試義消", "職稱": "隊員", "狀態": "出", "事由": "出勤",
            "值班人員": "原登打人", "簽核人員": "主管", "最後修改時間": "115/10/28 21:58",
        }, "io-1")
        self.assertEqual(datetime(2026, 10, 28, 21, 40), row.at)
        self.assertEqual("原登打人", row.operator)
        self.assertTrue(row.signed)

    def test_work_log_reads_case_time_volunteers_and_each_labeled_time_separately(self):
        row = civilpower._work_record_from_columns({
            "時間": "115/10/28 21:39", "協勤單位": "新坡分隊", "協勤人員": "測試義消、其他義消", "服勤項目": "緊急救護",
            "工作概述": "返隊時間:2026/10/28 22:28:47 地點:測試路1號 協勤時間(出) : 2026/10/28 21:40 協勤時間(入) : 2026/10/28 22:28",
            "處理情形": "1.新坡92:測試甲 3.救護義消協勤:測試義消", "建檔者": "原登打人",
            "最後修改時間": "115/10/29 00:18",
        }, "work-1")
        self.assertEqual(datetime(2026, 10, 28, 21, 39), row.at)
        self.assertEqual(datetime(2026, 10, 28, 22, 28, 47), row.official_return_at)
        self.assertEqual(datetime(2026, 10, 28, 21, 40), row.io_out_at)
        self.assertEqual(datetime(2026, 10, 28, 22, 28), row.io_in_at)
        self.assertEqual(("測試義消", "其他義消"), row.volunteers)
        self.assertEqual("work-1", row.record_id)

    def test_absent_labels_cannot_steal_another_time_from_the_summary(self):
        row = civilpower._work_record_from_columns({
            "時間": "115/10/28 21:39", "協勤單位": "新坡分隊", "協勤人員": "測試義消", "服勤項目": "緊急救護",
            "工作概述": "返隊時間:未填 協勤時間(出) : 2026/10/28 21:40 協勤時間(入) : 未填",
            "處理情形": "", "建檔者": "原登打人",
        }, "work-1")
        self.assertIsNone(row.official_return_at)
        self.assertIsNone(row.io_in_at)

    def test_localized_timestamp_reader_keeps_full_date_and_seconds(self):
        examples = {
            "1151028 21:40": datetime(2026, 10, 28, 21, 40),
            "115/10/28 21:40": datetime(2026, 10, 28, 21, 40),
            "2026/10/28 下午 1:40:47": datetime(2026, 10, 28, 13, 40, 47),
            "2026/10/29 上午 12:18": datetime(2026, 10, 29, 0, 18),
            "2026-10-28T13:40:00+00:00": datetime(2026, 10, 28, 21, 40),
        }
        for raw, expected in examples.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, civilpower._record_datetime(raw))
        for raw in ("115/02/30 21:40", "2026/10/28 下午 13:40", "2026/10/28 21:40:99", "未填"):
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                civilpower._record_datetime(raw)


class CivilpowerSaveReplyTests(unittest.TestCase):
    def test_business_rejections_are_reported_instead_of_clicking_behind_the_popup(self):
        from selenium.common.exceptions import TimeoutException

        class ImmediateWait:
            def until(self, predicate):
                if predicate(driver):
                    return True
                raise TimeoutException("still open")

        for message in ("測試義消尚未出勤，無法新增入勤資料。", "出入時間未位於案件派遣返隊時間內，請於出入登記簿修正。", "未新增成功，請重試。"):
            popup = SimpleNamespace(text=message, is_displayed=lambda: True)
            driver = mock.Mock()
            driver.find_elements.return_value = [popup]
            driver.find_element.return_value.text = message
            with self.subTest(message=message), mock.patch("civilpower._element_displayed", return_value=True), mock.patch("civilpower._dismiss_save_success_dialog") as dismiss:
                with self.assertRaisesRegex(RuntimeError, message):
                    civilpower._wait_after_save(driver, ImmediateWait(), "#jqxAddWindow")
                dismiss.assert_not_called()

    def test_an_open_form_without_a_success_reply_cannot_count_as_accepted(self):
        from selenium.common.exceptions import TimeoutException

        wait = SimpleNamespace(until=mock.Mock(side_effect=TimeoutException("still open")))
        driver = mock.Mock()
        driver.find_elements.return_value = []
        driver.find_element.return_value.text = "新增出入紀錄"
        with mock.patch("civilpower._element_displayed", return_value=True), mock.patch("civilpower._dismiss_save_success_dialog"):
            with self.assertRaisesRegex(RuntimeError, "尚未確認"):
                civilpower._wait_after_save(driver, wait, "#jqxAddWindow")


class CivilpowerTypedPaginationTests(unittest.TestCase):
    def page(self, record_id, time):
        headers = ["", "時間", "服勤單位", "所屬單位", "姓名", "職稱", "狀態"]
        values = ["", f"115/10/28 {time}", "新坡分隊", "大園救護分隊", "測試義消", "隊員", "出"]
        element = mock.Mock()
        element.find_elements.return_value = [SimpleNamespace(get_attribute=lambda key: record_id if key == "value" else None)]
        return {"rows": [{"cells": headers}, {"cells": values, "element": element}], "pages": []}

    def context(self, pages, *, mutate_on_return=False):
        from contextlib import ExitStack

        stack = ExitStack()
        self.index = 0
        self.returning = False
        self.visited = [0]

        def move(_driver, _wait, direction):
            self.index += 1 if direction == "next" else -1
            self.returning = self.returning or direction == "prev"
            self.visited.append(self.index)

        def snapshot(_grid):
            if mutate_on_return and self.returning and self.index == 0:
                return self.page("999", "21:41")
            return pages[self.index]

        stack.enter_context(mock.patch("civilpower._table_page_snapshot", side_effect=snapshot))
        stack.enter_context(mock.patch("civilpower._pagination_links", side_effect=lambda _driver, direction: [object()] if direction == "next" and self.index < len(pages)-1 else []))
        stack.enter_context(mock.patch("civilpower._change_result_page", side_effect=move))
        return stack

    def test_collects_all_pages_and_checks_the_original_pages_before_using_them(self):
        with self.context([self.page("101", "19:15"), self.page("102", "21:40"), self.page("103", "22:49")]):
            records = civilpower._read_typed_record_pages(mock.Mock(), kind="io")
        self.assertEqual(["101", "102", "103"], [row.record_id for row in records])
        self.assertEqual([0, 1, 2, 1, 0], self.visited)

    def test_page_changes_and_duplicate_ids_do_not_become_missing_record_results(self):
        with self.context([self.page("101", "19:15"), self.page("102", "21:40")], mutate_on_return=True):
            with self.assertRaisesRegex(RuntimeError, "資料已變動"):
                civilpower._read_typed_record_pages(mock.Mock(), kind="io")
        with self.context([self.page("101", "19:15"), self.page("101", "21:40")]):
            with self.assertRaisesRegex(RuntimeError, "重複紀錄"):
                civilpower._read_typed_record_pages(mock.Mock(), kind="io")


class CivilpowerQueryGuardTests(unittest.TestCase):
    def test_guard_query_tab_is_closed_and_original_form_is_restored_even_on_error(self):
        driver = SimpleNamespace(current_window_handle="form", closed=[])
        driver.switch_to = SimpleNamespace(
            new_window=lambda _kind: setattr(driver, "current_window_handle", "query"),
            window=lambda handle: setattr(driver, "current_window_handle", handle),
        )
        driver.close = lambda: driver.closed.append(driver.current_window_handle)
        self.assertEqual("ok", civilpower._in_civilpower_query_tab(driver, lambda: "ok"))
        self.assertEqual("form", driver.current_window_handle)
        self.assertEqual(["query"], driver.closed)

        def fail():
            raise RuntimeError("query failed")

        with self.assertRaisesRegex(RuntimeError, "query failed"):
            civilpower._in_civilpower_query_tab(driver, fail)
        self.assertEqual("form", driver.current_window_handle)
        self.assertNotIn("form", driver.closed)

    def test_failure_to_create_a_query_tab_never_closes_the_original_form(self):
        from selenium.common.exceptions import WebDriverException

        driver = SimpleNamespace(current_window_handle="form", close=mock.Mock())
        driver.switch_to = SimpleNamespace(new_window=mock.Mock(side_effect=WebDriverException("tab unavailable")), window=mock.Mock())
        with self.assertRaises(WebDriverException):
            civilpower._in_civilpower_query_tab(driver, lambda: None)
        driver.close.assert_not_called()
        driver.switch_to.window.assert_called_once_with("form")

    def test_unconfirmed_ajax_result_cannot_become_an_empty_record_list(self):
        from contextlib import ExitStack
        from tests import test_civilpower_manual_flow

        fixture = test_civilpower_manual_flow.CivilpowerManualFlowTests()
        fixture.setUp()
        controls = {}
        driver = mock.Mock()
        driver.execute_script.return_value = []
        with ExitStack() as stack:
            stack.enter_context(mock.patch("civilpower._open_io_work_log", return_value=object()))
            stack.enter_context(mock.patch("civilpower._set_input", side_effect=lambda _wait, selector, value: controls.__setitem__(selector, value)))
            stack.enter_context(mock.patch("civilpower._control_value", side_effect=lambda _driver, selector: controls[selector]))
            stack.enter_context(mock.patch("civilpower._select_option_containing"))
            stack.enter_context(mock.patch("civilpower._io_result_grid_signature", return_value="old"))
            stack.enter_context(mock.patch("civilpower._io_result_grid_sentinel", return_value=None))
            stack.enter_context(mock.patch("civilpower._wait_for_io_query_result_grid", return_value=False))
            stack.enter_context(mock.patch("civilpower._click"))
            read = stack.enter_context(mock.patch("civilpower._read_typed_record_pages"))
            with self.assertRaisesRegex(RuntimeError, "未完成更新"):
                civilpower._query_civilpower_records(driver, fixture.plan, fixture.window, kind="io")
        read.assert_not_called()
        self.assertEqual("2026/08/18", controls["#txt_Date_S"])
        self.assertEqual("2026/08/20", controls["#txt_Date_E"])
