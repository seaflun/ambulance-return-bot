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
        original_cleanup = chrome_startup.cleanup_worker_chrome_residue
        calls = {"count": 0}
        cleanups = []
        options = object()
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
            chrome_startup.cleanup_worker_chrome_residue = lambda options, label="Chrome": cleanups.append((options, label)) or 2

            driver = chrome_startup.create_chrome_driver_with_retry(options, "緊急救護消毒")
        finally:
            chrome_startup.webdriver.Chrome = original_chrome
            chrome_startup.time.sleep = original_sleep
            chrome_startup.cleanup_worker_chrome_residue = original_cleanup
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
        self.assertEqual(cleanups, [(options, "緊急救護消毒")])

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

    def test_cleanup_worker_chrome_residue_targets_worker_profile_and_port_only(self):
        class FakeOptions:
            arguments = [
                r"--user-data-dir=C:\Users\User\AppData\Local\ambulance_return_bot\chrome_profile",
                "--remote-debugging-port=9223",
            ]

        processes = [
            {
                "ProcessId": 10,
                "ParentProcessId": 1,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir=C:\Users\User\AppData\Local\Google\Chrome\User Data',
            },
            {
                "ProcessId": 11,
                "ParentProcessId": 1,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir=C:\Users\User\AppData\Local\ambulance_return_bot\chrome_profile',
            },
            {
                "ProcessId": 12,
                "ParentProcessId": 11,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --type=renderer',
            },
            {
                "ProcessId": 13,
                "ParentProcessId": 1,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9223',
            },
            {
                "ProcessId": 14,
                "ParentProcessId": 1,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9333',
            },
            {
                "ProcessId": 15,
                "ParentProcessId": 1,
                "Name": "chromedriver.exe",
                "CommandLine": r'"C:\tools\chromedriver.exe" --port=51342',
            },
        ]
        original_list = chrome_startup._list_chrome_processes
        original_terminate = chrome_startup._terminate_process
        killed = []
        try:
            chrome_startup._list_chrome_processes = lambda: processes
            chrome_startup._terminate_process = lambda pid: killed.append(pid) or True

            count = chrome_startup.cleanup_worker_chrome_residue(FakeOptions(), "測試")
        finally:
            chrome_startup._list_chrome_processes = original_list
            chrome_startup._terminate_process = original_terminate

        self.assertEqual(count, 4)
        self.assertEqual(set(killed), {11, 12, 13, 15})
        self.assertNotIn(10, killed)
        self.assertNotIn(14, killed)


if __name__ == "__main__":
    unittest.main()
