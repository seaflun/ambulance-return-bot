import os
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options

import ambulance_bot.chrome_startup as chrome_startup


class ChromeStartupTests(unittest.TestCase):
    def test_cleanup_only_targets_chromedriver_that_owns_matching_worker_chrome(self):
        class FakeOptions:
            arguments = [r"--user-data-dir=C:\runtime\profiles\vehicle_mileage_profile_task_a"]

        processes = [
            {
                "ProcessId": 100,
                "ParentProcessId": 1,
                "Name": "chromedriver.exe",
                "CommandLine": r'"C:\tools\chromedriver.exe" --port=51000',
            },
            {
                "ProcessId": 101,
                "ParentProcessId": 100,
                "Name": "chrome.exe",
                "CommandLine": r'chrome.exe --user-data-dir=C:\runtime\profiles\vehicle_mileage_profile_task_a',
            },
            {
                "ProcessId": 200,
                "ParentProcessId": 1,
                "Name": "chromedriver.exe",
                "CommandLine": r'"C:\tools\chromedriver.exe" --port=52000',
            },
            {
                "ProcessId": 201,
                "ParentProcessId": 200,
                "Name": "chrome.exe",
                "CommandLine": r'chrome.exe --user-data-dir=C:\runtime\profiles\disinfection_profile_task_b',
            },
        ]
        killed: list[int] = []
        original_list = chrome_startup._list_chrome_processes
        original_terminate = chrome_startup._terminate_process
        try:
            chrome_startup._list_chrome_processes = lambda: processes
            chrome_startup._terminate_process = lambda process_id: killed.append(process_id) or True

            chrome_startup.cleanup_worker_chrome_residue(FakeOptions(), "mileage retry")
        finally:
            chrome_startup._list_chrome_processes = original_list
            chrome_startup._terminate_process = original_terminate

        self.assertEqual(set(killed), {100, 101})
        self.assertNotIn(200, killed)
        self.assertNotIn(201, killed)
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

    def test_retry_cleans_failed_user_data_dir(self):
        with TemporaryDirectory() as tmp:
            previous_attempts = os.environ.get("SELENIUM_CHROME_START_ATTEMPTS")
            previous_delay = os.environ.get("SELENIUM_CHROME_RETRY_DELAY_SECONDS")
            original_chrome = chrome_startup.webdriver.Chrome
            original_sleep = chrome_startup.time.sleep
            original_cleanup = chrome_startup.cleanup_worker_chrome_residue
            original_profile_cleanup = chrome_startup.cleanup_runtime_profiles_for_startup_failure
            calls = {"count": 0}
            profile_cleanups: list[tuple[Path, ...]] = []
            user_data_dir = Path(tmp) / "profiles" / "consumables_profile_test"
            options = Options()
            options.add_argument(f"--user-data-dir={user_data_dir}")
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
                chrome_startup.cleanup_worker_chrome_residue = lambda options, label="Chrome": 0
                chrome_startup.cleanup_runtime_profiles_for_startup_failure = (
                    lambda dirs: profile_cleanups.append(tuple(Path(path) for path in dirs)) or []
                )

                driver = chrome_startup.create_chrome_driver_with_retry(options, "consumables")
            finally:
                chrome_startup.webdriver.Chrome = original_chrome
                chrome_startup.time.sleep = original_sleep
                chrome_startup.cleanup_worker_chrome_residue = original_cleanup
                chrome_startup.cleanup_runtime_profiles_for_startup_failure = original_profile_cleanup
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
        self.assertEqual(profile_cleanups, [(user_data_dir,)])

    def test_retry_recovers_when_chrome_start_times_out(self):
        previous_attempts = os.environ.get("SELENIUM_CHROME_START_ATTEMPTS")
        previous_delay = os.environ.get("SELENIUM_CHROME_RETRY_DELAY_SECONDS")
        previous_timeout = os.environ.get("SELENIUM_CHROME_START_TIMEOUT_SECONDS")
        original_chrome = chrome_startup.webdriver.Chrome
        original_sleep = chrome_startup.time.sleep
        original_cleanup = chrome_startup.cleanup_worker_chrome_residue
        calls = {"count": 0}
        cleanups = []
        options = object()
        try:
            os.environ["SELENIUM_CHROME_START_ATTEMPTS"] = "2"
            os.environ["SELENIUM_CHROME_RETRY_DELAY_SECONDS"] = "0"
            os.environ["SELENIUM_CHROME_START_TIMEOUT_SECONDS"] = "0.01"

            def fake_chrome(options=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    chrome_startup.time.sleep(0.05)
                return object()

            chrome_startup.webdriver.Chrome = fake_chrome
            chrome_startup.time.sleep = lambda seconds: None if seconds == 0 else original_sleep(seconds)
            chrome_startup.cleanup_worker_chrome_residue = lambda options, label="Chrome": cleanups.append((options, label)) or 0

            driver = chrome_startup.create_chrome_driver_with_retry(options, "case lookup")
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
            if previous_timeout is None:
                os.environ.pop("SELENIUM_CHROME_START_TIMEOUT_SECONDS", None)
            else:
                os.environ["SELENIUM_CHROME_START_TIMEOUT_SECONDS"] = previous_timeout

        self.assertIsNotNone(driver)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(cleanups, [(options, "case lookup")])

    def test_retries_oserror_invalid_argument_startup_error(self):
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
                    raise OSError(22, "Invalid argument")
                return object()

            chrome_startup.webdriver.Chrome = fake_chrome
            chrome_startup.time.sleep = lambda seconds: None
            chrome_startup.cleanup_worker_chrome_residue = lambda options, label="Chrome": cleanups.append((options, label)) or 2

            driver = chrome_startup.create_chrome_driver_with_retry(options, "consumables")
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
        self.assertEqual(cleanups, [(options, "consumables")])

    def test_retry_continues_when_failed_profile_cleanup_raises_oserror(self):
        previous_attempts = os.environ.get("SELENIUM_CHROME_START_ATTEMPTS")
        previous_delay = os.environ.get("SELENIUM_CHROME_RETRY_DELAY_SECONDS")
        original_chrome = chrome_startup.webdriver.Chrome
        original_sleep = chrome_startup.time.sleep
        original_cleanup = chrome_startup.cleanup_worker_chrome_residue
        original_profile_cleanup = chrome_startup.cleanup_runtime_profiles_for_startup_failure
        calls = {"count": 0}
        try:
            os.environ["SELENIUM_CHROME_START_ATTEMPTS"] = "2"
            os.environ["SELENIUM_CHROME_RETRY_DELAY_SECONDS"] = "0"

            def fake_chrome(options=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError(22, "Invalid argument")
                return object()

            chrome_startup.webdriver.Chrome = fake_chrome
            chrome_startup.time.sleep = lambda seconds: None
            chrome_startup.cleanup_worker_chrome_residue = lambda options, label="Chrome": 0
            chrome_startup.cleanup_runtime_profiles_for_startup_failure = (
                lambda paths: (_ for _ in ()).throw(OSError(22, "Invalid argument"))
            )

            driver = chrome_startup.create_chrome_driver_with_retry(object(), "consumables")
        finally:
            chrome_startup.webdriver.Chrome = original_chrome
            chrome_startup.time.sleep = original_sleep
            chrome_startup.cleanup_worker_chrome_residue = original_cleanup
            chrome_startup.cleanup_runtime_profiles_for_startup_failure = original_profile_cleanup
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

    def test_no_space_left_is_startup_error(self):
        self.assertTrue(chrome_startup._is_chrome_startup_error(OSError(28, "No space left on device")))

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
            {
                "ProcessId": 16,
                "ParentProcessId": 1,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir=C:\Users\User\AppData\Local\ambulance_return_bot\case_lookup_profile_123',
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

        self.assertEqual(count, 3)
        self.assertEqual(set(killed), {11, 12, 13})
        self.assertNotIn(10, killed)
        self.assertNotIn(14, killed)
        self.assertNotIn(15, killed)
        self.assertNotIn(16, killed)

    def test_repair_cleanup_targets_generated_case_lookup_profiles(self):
        class FakeOptions:
            arguments = []

        processes = [
            {
                "ProcessId": 20,
                "ParentProcessId": 1,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir=C:\Users\User\AppData\Local\Google\Chrome\User Data',
            },
            {
                "ProcessId": 21,
                "ParentProcessId": 1,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir=C:\Users\User\AppData\Local\ambulance_return_bot\case_lookup_profile_1783392215',
            },
            {
                "ProcessId": 22,
                "ParentProcessId": 21,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --type=renderer',
            },
            {
                "ProcessId": 23,
                "ParentProcessId": 1,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir=C:\Users\User\AppData\Local\ambulance_return_bot\vehicle_mileage_profile_task1',
            },
            {
                "ProcessId": 24,
                "ParentProcessId": 1,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir=C:\Users\User\AppData\Local\other_app\case_lookup_profile_1783392215',
            },
            {
                "ProcessId": 25,
                "ParentProcessId": 1,
                "Name": "chrome.exe",
                "CommandLine": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir=C:\Users\User\AppData\Local\ambulance_return_bot\unrelated_profile',
            },
            {
                "ProcessId": 26,
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

            count = chrome_startup.cleanup_worker_chrome_residue(
                FakeOptions(),
                "worker repair",
                include_generated_profiles=True,
                profile_root=r"C:\Users\User\AppData\Local\ambulance_return_bot",
            )
        finally:
            chrome_startup._list_chrome_processes = original_list
            chrome_startup._terminate_process = original_terminate

        self.assertEqual(count, 3)
        self.assertEqual(set(killed), {21, 22, 23})
        self.assertNotIn(20, killed)
        self.assertNotIn(24, killed)
        self.assertNotIn(25, killed)
        self.assertNotIn(26, killed)

    def test_create_chrome_driver_with_retry_schedules_auto_close_after_default_delay(self):
        previous_delay = os.environ.get("WORKER_BROWSER_AUTO_CLOSE_SECONDS")
        original_chrome = chrome_startup.webdriver.Chrome
        original_timer = chrome_startup.threading.Timer
        timers = []

        class FakeDriver:
            quit_called = False

            def quit(self):
                self.quit_called = True

        class FakeTimer:
            daemon = False

            def __init__(self, seconds, callback):
                self.seconds = seconds
                self.callback = callback
                self.started = False
                timers.append(self)

            def start(self):
                self.started = True

        try:
            os.environ.pop("WORKER_BROWSER_AUTO_CLOSE_SECONDS", None)
            chrome_startup.webdriver.Chrome = lambda options=None: FakeDriver()
            chrome_startup.threading.Timer = FakeTimer

            driver = chrome_startup.create_chrome_driver_with_retry(object(), "auto close")
        finally:
            chrome_startup.webdriver.Chrome = original_chrome
            chrome_startup.threading.Timer = original_timer
            if previous_delay is None:
                os.environ.pop("WORKER_BROWSER_AUTO_CLOSE_SECONDS", None)
            else:
                os.environ["WORKER_BROWSER_AUTO_CLOSE_SECONDS"] = previous_delay

        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0].seconds, 600)
        self.assertTrue(timers[0].daemon)
        self.assertTrue(timers[0].started)
        timers[0].callback()
        self.assertTrue(driver.quit_called)

    def test_auto_close_can_be_disabled(self):
        previous_delay = os.environ.get("WORKER_BROWSER_AUTO_CLOSE_SECONDS")
        original_chrome = chrome_startup.webdriver.Chrome
        original_timer = chrome_startup.threading.Timer
        timers = []

        try:
            os.environ["WORKER_BROWSER_AUTO_CLOSE_SECONDS"] = "0"
            chrome_startup.webdriver.Chrome = lambda options=None: object()
            chrome_startup.threading.Timer = lambda seconds, callback: timers.append((seconds, callback))

            driver = chrome_startup.create_chrome_driver_with_retry(object(), "disabled auto close")
        finally:
            chrome_startup.webdriver.Chrome = original_chrome
            chrome_startup.threading.Timer = original_timer
            if previous_delay is None:
                os.environ.pop("WORKER_BROWSER_AUTO_CLOSE_SECONDS", None)
            else:
                os.environ["WORKER_BROWSER_AUTO_CLOSE_SECONDS"] = previous_delay

        self.assertIsNotNone(driver)
        self.assertEqual(timers, [])

    def test_auto_close_defers_while_worker_operation_is_active(self):
        timers = []

        class FakeDriver:
            quit_called = False
            _ambulance_operation_active = True

            def quit(self):
                self.quit_called = True

        class FakeTimer:
            daemon = False

            def __init__(self, seconds, callback):
                self.seconds = seconds
                self.callback = callback
                timers.append(self)

            def start(self):
                pass

        driver = FakeDriver()
        with mock.patch.dict(os.environ, {"WORKER_BROWSER_AUTO_CLOSE_SECONDS": "1"}), mock.patch.object(
            chrome_startup.threading, "Timer", FakeTimer
        ):
            chrome_startup.schedule_driver_auto_close(driver, "active")
            timers[0].callback()
            self.assertFalse(driver.quit_called)
            self.assertEqual(len(timers), 2)
            driver._ambulance_operation_active = False
            timers[1].callback()

        self.assertTrue(driver.quit_called)


if __name__ == "__main__":
    unittest.main()
