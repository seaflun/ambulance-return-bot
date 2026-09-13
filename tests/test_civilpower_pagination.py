from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from selenium.common.exceptions import TimeoutException

import civilpower
from tests import test_civilpower


class PageLink:
    def __init__(self, browser, step):
        self.browser, self.step = browser, step

    def is_displayed(self):
        return True

    def click(self):
        if not self.browser.stuck:
            self.browser.page += self.step
            self.browser.visited.append(self.browser.page)


class PagedResults:
    def __init__(self, pages, *, stuck=False):
        self.pages, self.page, self.stuck = pages, 0, stuck
        self.visited = [0]

    def find_elements(self, by, selector):
        if "rel='next'" in selector and self.page + 1 < len(self.pages):
            return [PageLink(self, 1)]
        if "rel='prev'" in selector and self.page > 0:
            return [PageLink(self, -1)]
        if ".active" in selector:
            return [SimpleNamespace(text=str(self.page + 1))]
        return []

    def execute_script(self, script):
        return False


class ImmediateWait:
    def __init__(self, browser):
        self.browser = browser

    def until(self, predicate):
        result = predicate(self.browser)
        if not result:
            raise TimeoutException("page did not change")
        return result


class CivilpowerPaginationTests(unittest.TestCase):
    def setUp(self):
        self.request = test_civilpower.CivilpowerPlanTests._enabled_request()
        self.plan = civilpower.build_civilpower_task_plan(self.request)
        self.row = SimpleNamespace(text="測試義消 大園救護分隊 新坡分隊 救護出勤 115/08/19 12:32")

    def query_context(self, browser):
        stack = ExitStack()
        for name in ("_open_io_work_log", "_set_if_present", "_select_option_containing_if_present", "_click_if_present"):
            stack.enter_context(mock.patch("civilpower." + name))
        stack.enter_context(mock.patch("civilpower._open_work_log_form", return_value=ImmediateWait(browser)))
        stack.enter_context(mock.patch("civilpower.WebDriverWait", return_value=ImmediateWait(browser)))
        stack.enter_context(mock.patch("civilpower._wait_for_io_query_result_grid", return_value=True))
        stack.enter_context(mock.patch("civilpower._table_rows", side_effect=lambda _: browser.pages[browser.page]))
        return stack

    def test_existing_out_record_on_later_page_prevents_add(self):
        browser = PagedResults([[], [self.row], []])
        with self.query_context(browser), mock.patch("civilpower._click") as click:
            self.assertFalse(civilpower._ensure_io_record(
                browser, self.plan, civilpower.OUT_STATUS, {}, cancel_check=None
            ))
        click.assert_not_called()
        self.assertEqual(browser.page, 1)

    def test_existing_work_record_on_later_page_prevents_add(self):
        row = SimpleNamespace(text=f"測試義消 {self.plan.case_address} 115/08/19 12:32")
        browser = PagedResults([[], [row]])
        with self.query_context(browser), mock.patch("civilpower._click") as click:
            self.assertEqual(civilpower._ensure_work_log(
                browser, self.request, self.plan, {}, cancel_check=None
            ), self.plan)
        click.assert_not_called()

    def test_dialog_finds_later_page_and_leaves_selected_row_current(self):
        browser = PagedResults([[], [self.row], []])
        with self.query_context(browser):
            row = civilpower._wait_for_dialog_row(ImmediateWait(browser), browser, ["測試義消", "2026/08/19 12:32"])
        self.assertIs(row, self.row)
        self.assertEqual(browser.page, 1)

    def test_duplicate_match_across_pages_stops_selection(self):
        browser = PagedResults([[self.row], [self.row]])
        with self.query_context(browser):
            with self.assertRaisesRegex(civilpower._AmbiguousSelectionError, "多筆"):
                civilpower._wait_for_dialog_row(ImmediateWait(browser), browser, ["測試義消"])

    def test_failed_page_change_never_falls_through_to_add(self):
        browser = PagedResults([[], [self.row]], stuck=True)
        with self.query_context(browser), mock.patch("civilpower._click") as click:
            with self.assertRaisesRegex(RuntimeError, "翻頁"):
                civilpower._ensure_io_record(browser, self.plan, civilpower.OUT_STATUS, {}, cancel_check=None)
        click.assert_not_called()

    def test_no_match_checks_all_pages_and_returns_to_original_page(self):
        browser = PagedResults([[], [], []])
        with self.query_context(browser):
            self.assertFalse(civilpower._find_io_record(browser, self.plan, civilpower.OUT_STATUS))
        self.assertEqual(browser.page, 0)
        self.assertEqual([0, 1, 2, 1, 0], browser.visited)

    def test_incomplete_case_search_cannot_fall_back_to_weaker_identity(self):
        browser = mock.Mock()
        browser.find_element.return_value.is_selected.return_value = True
        with mock.patch("civilpower._set_input"), mock.patch("civilpower._open_selection_dialog"), mock.patch(
            "civilpower._wait_for_dialog_row", side_effect=civilpower._IncompleteQueryError("翻頁失敗")
        ) as find, mock.patch("civilpower._click_dialog_row_action") as click:
            with self.assertRaisesRegex(civilpower._IncompleteQueryError, "翻頁失敗"):
                civilpower._import_work_log_case(browser, ImmediateWait(browser), self.plan)
        self.assertEqual(1, find.call_count)
        click.assert_not_called()
