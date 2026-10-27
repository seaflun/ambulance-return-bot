from __future__ import annotations

import unittest
from unittest import mock

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException

from civilpower import _wait_for_oa_login_result


class CivilpowerLoginTests(unittest.TestCase):
    def wait_for_states(self, states):
        driver = mock.Mock()
        driver.execute_script.side_effect = states
        wait = mock.Mock()
        outcomes = []

        def until(predicate):
            for _state in states:
                result = predicate(driver)
                outcomes.append(bool(result))
                if result:
                    return result
            raise TimeoutException()

        wait.until.side_effect = until
        return driver, wait, outcomes

    def test_defers_password_notice_once_and_waits_for_dashboard(self):
        defer_button = mock.Mock()
        notice = {"loginPage": False, "passwordNotice": True, "deferButton": defer_button,
                  "dashboardReady": True, "error": ""}
        loading = {"loginPage": False, "passwordNotice": False, "dashboardReady": False, "error": ""}
        dashboard = {**loading, "dashboardReady": True}
        driver, wait, outcomes = self.wait_for_states([notice, notice, loading, dashboard])

        _wait_for_oa_login_result(driver, wait)

        defer_button.click.assert_called_once_with()
        self.assertEqual([False, False, False, True], outcomes)

    def test_form_disappearing_is_not_success_without_visible_dashboard(self):
        driver, wait, outcomes = self.wait_for_states([
            {"loginPage": True, "dashboardReady": True},
            {"loginPage": False, "dashboardReady": False},
            {"loginPage": False, "dashboardReady": True},
        ])

        _wait_for_oa_login_result(driver, wait)

        self.assertEqual([False, False, True], outcomes)

    def test_forced_password_change_without_defer_button_is_explicit_failure(self):
        change_button = mock.Mock()
        driver, wait, outcomes = self.wait_for_states([
            {"loginPage": False, "passwordNotice": True, "dashboardReady": True,
             "deferButton": None, "changeButton": change_button},
        ])

        with self.assertRaisesRegex(RuntimeError, "未提供可用的「暫不變更」.*人工處理"):
            _wait_for_oa_login_result(driver, wait)

        self.assertEqual([False], outcomes)
        change_button.click.assert_not_called()

    def test_notice_remaining_after_click_is_not_success_or_repeated_click(self):
        defer_button = mock.Mock()
        notice = {"loginPage": False, "passwordNotice": True, "deferButton": defer_button,
                  "dashboardReady": True}
        driver, wait, outcomes = self.wait_for_states([notice, notice, notice])

        with self.assertRaisesRegex(RuntimeError, "已點選「暫不變更」.*提醒尚未消失"):
            _wait_for_oa_login_result(driver, wait)

        self.assertEqual([False, False, False], outcomes)
        defer_button.click.assert_called_once_with()

    def test_dismissed_notice_still_requires_dashboard_readiness(self):
        defer_button = mock.Mock()
        driver, wait, outcomes = self.wait_for_states([
            {"loginPage": False, "passwordNotice": True, "deferButton": defer_button},
            {"loginPage": False, "passwordNotice": False, "dashboardReady": False},
        ])

        with self.assertRaisesRegex(RuntimeError, "登入首頁未就緒"):
            _wait_for_oa_login_result(driver, wait)

        self.assertEqual([False, False], outcomes)

    def test_waits_until_defer_button_is_enabled(self):
        defer_button = mock.Mock()
        defer_button.is_enabled.side_effect = [False, True]
        notice = {"loginPage": False, "passwordNotice": True, "deferButton": defer_button}
        driver, wait, outcomes = self.wait_for_states([
            notice, notice, {"loginPage": False, "dashboardReady": True},
        ])

        _wait_for_oa_login_result(driver, wait)

        defer_button.click.assert_called_once_with()
        self.assertEqual([False, False, True], outcomes)

    def test_replaced_defer_button_is_rechecked_after_stale_element(self):
        stale_button = mock.Mock()
        stale_button.click.side_effect = StaleElementReferenceException()
        current_button = mock.Mock()
        driver, wait, outcomes = self.wait_for_states([
            {"loginPage": False, "passwordNotice": True, "deferButton": stale_button},
            {"loginPage": False, "passwordNotice": True, "deferButton": current_button},
            {"loginPage": False, "dashboardReady": True},
        ])

        _wait_for_oa_login_result(driver, wait)

        current_button.click.assert_called_once_with()
        self.assertEqual([False, False, True], outcomes)

    def test_disabled_defer_button_never_reports_success(self):
        defer_button = mock.Mock()
        defer_button.is_enabled.return_value = False
        driver, wait, outcomes = self.wait_for_states([
            {"loginPage": False, "passwordNotice": True, "deferButton": defer_button},
        ])

        with self.assertRaisesRegex(RuntimeError, "未提供可用的「暫不變更」"):
            _wait_for_oa_login_result(driver, wait)

        defer_button.click.assert_not_called()
        self.assertEqual([False], outcomes)

    def test_unreadable_page_state_is_not_assumed_authenticated(self):
        driver, wait, outcomes = self.wait_for_states([None, {}, {"loginPage": False}])

        with self.assertRaises(TimeoutException):
            _wait_for_oa_login_result(driver, wait)

        self.assertEqual([False, False, False], outcomes)


if __name__ == "__main__":
    unittest.main()
