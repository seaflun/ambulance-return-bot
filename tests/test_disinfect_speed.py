import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import disinfect


class DisinfectionLoginStateTests(unittest.TestCase):
    def driver(self, **overrides):
        snapshot = {
            "url": "https://emsdt.tyfd.gov.tw/EmmWeb/ActionControlServlet",
            "ready": True,
            "login_form": False,
            "text": "",
            "authenticated_frame": False,
        }
        snapshot.update(overrides)
        return SimpleNamespace(execute_script=Mock(return_value=snapshot))

    def test_explicit_login_error_ends_wait_immediately(self):
        driver = self.driver(text="帳號、密碼、驗證碼錯誤或帳號不存在!")

        with self.assertRaisesRegex(RuntimeError, "帳號|密碼|驗證碼"):
            disinfect.wait_until_logged_in(driver, timeout=0)

        self.assertEqual(driver.execute_script.call_count, 1)

    def test_each_credential_error_is_terminal(self):
        for message in ("驗證碼錯誤", "帳號不存在", "密碼不正確"):
            with self.subTest(message=message):
                self.assertEqual(
                    disinfect._disinfection_login_state(self.driver(text=message)),
                    "error",
                )

    def test_expected_url_without_authenticated_content_remains_pending(self):
        self.assertEqual(disinfect._disinfection_login_state(self.driver()), "pending")

    def test_authenticated_shell_is_positive_login_evidence(self):
        self.assertEqual(
            disinfect._disinfection_login_state(self.driver(authenticated_frame=True)),
            "success",
        )

    def test_positive_content_after_login_is_accepted(self):
        for text in ("報表系統", "消毒紀錄", "登出", "功能選單"):
            with self.subTest(text=text):
                self.assertTrue(disinfect._is_logged_in(self.driver(text=text)))

    def test_login_form_and_loading_document_remain_pending(self):
        for overrides in (
            {"login_form": True, "text": "消毒紀錄"},
            {"ready": False, "authenticated_frame": True},
            {"url": "about:blank", "text": "功能選單"},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(
                    disinfect._disinfection_login_state(self.driver(**overrides)),
                    "pending",
                )

    def test_wait_accepts_successful_state_without_extra_poll(self):
        driver = self.driver(text="功能選單")

        disinfect.wait_until_logged_in(driver, timeout=0)

        self.assertEqual(driver.execute_script.call_count, 1)

    def test_failed_attempts_preserve_three_retries_and_credential_order(self):
        credentials = [
            (SimpleNamespace(user_id="first", password="first-password"), "任務司機"),
            (SimpleNamespace(user_id="second", password="second-password"), "同步帳號"),
        ]
        driver = Mock()
        with patch.object(disinfect, "_disinfection_credential_attempts", return_value=credentials), patch.object(
            disinfect, "create_chrome_driver_with_retry", return_value=driver
        ), patch.object(disinfect, "apply_tile"), patch.object(
            disinfect, "_login_once"
        ) as login_once, patch.object(
            disinfect, "wait_until_logged_in", side_effect=[RuntimeError("驗證碼錯誤")] * 3 + [None]
        ), patch.object(disinfect, "_is_logged_in", return_value=True):
            result = disinfect.login_and_get_driver()

        self.assertIs(result, driver)
        self.assertEqual(
            [(call.args[1], call.args[3]) for call in login_once.call_args_list],
            [("first", 1), ("first", 2), ("first", 3), ("second", 1)],
        )


if __name__ == "__main__":
    unittest.main()
