import os
import unittest

from selenium.common.exceptions import WebDriverException

import ambulance_bot.chrome_startup as chrome_startup


class ChromeStartupTests(unittest.TestCase):
    def test_retries_devtools_active_port_startup_error(self):
        previous_attempts = os.environ.get("SELENIUM_CHROME_START_ATTEMPTS")
        previous_delay = os.environ.get("SELENIUM_CHROME_RETRY_DELAY_SECONDS")
        original_chrome = chrome_startup.webdriver.Chrome
        original_sleep = chrome_startup.time.sleep
        calls = {"count": 0}
        try:
            os.environ["SELENIUM_CHROME_START_ATTEMPTS"] = "2"
            os.environ["SELENIUM_CHROME_RETRY_DELAY_SECONDS"] = "0"

            def fake_chrome(options=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise WebDriverException("session not created: DevToolsActivePort file doesn't exist")
                return object()

            chrome_startup.webdriver.Chrome = fake_chrome
            chrome_startup.time.sleep = lambda seconds: None

            driver = chrome_startup.create_chrome_driver_with_retry(object(), "緊急救護消毒")
        finally:
            chrome_startup.webdriver.Chrome = original_chrome
            chrome_startup.time.sleep = original_sleep
            if previous_attempts is None:
                os.environ.pop("SELENIUM_CHROME_START_ATTEMPTS", None)
            else:
                os.environ["SELENIUM_CHROME_START_ATTEMPTS"] = previous_attempts
            if previous_delay is None:
                os.environ.pop("SELENIUM_CHROME_RETRY_DELAY_SECONDS", None)
            else:
                os.environ["SELENIUM_CHROME_RETRY_DELAY_SECONDS"] = previous_delay

        self.assertIsNotNone(driver)
        self.assertEqual(calls["count"], 2)

    def test_final_startup_error_is_short_and_readable(self):
        previous_attempts = os.environ.get("SELENIUM_CHROME_START_ATTEMPTS")
        previous_delay = os.environ.get("SELENIUM_CHROME_RETRY_DELAY_SECONDS")
        original_chrome = chrome_startup.webdriver.Chrome
        original_sleep = chrome_startup.time.sleep
        try:
            os.environ["SELENIUM_CHROME_START_ATTEMPTS"] = "1"
            os.environ["SELENIUM_CHROME_RETRY_DELAY_SECONDS"] = "0"
            chrome_startup.webdriver.Chrome = lambda options=None: (_ for _ in ()).throw(
                WebDriverException("session not created: Chrome failed to start: crashed.\nStacktrace: long")
            )
            chrome_startup.time.sleep = lambda seconds: None

            with self.assertRaises(WebDriverException) as ctx:
                chrome_startup.create_chrome_driver_with_retry(object(), "緊急救護消毒")
        finally:
            chrome_startup.webdriver.Chrome = original_chrome
            chrome_startup.time.sleep = original_sleep
            if previous_attempts is None:
                os.environ.pop("SELENIUM_CHROME_START_ATTEMPTS", None)
            else:
                os.environ["SELENIUM_CHROME_START_ATTEMPTS"] = previous_attempts
            if previous_delay is None:
                os.environ.pop("SELENIUM_CHROME_RETRY_DELAY_SECONDS", None)
            else:
                os.environ["SELENIUM_CHROME_RETRY_DELAY_SECONDS"] = previous_delay

        self.assertIn("緊急救護消毒 Chrome 啟動失敗，已重試 1 次", str(ctx.exception))
        self.assertIn("Chrome failed to start", str(ctx.exception))
        self.assertNotIn("Stacktrace: long", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
