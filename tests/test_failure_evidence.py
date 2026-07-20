import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ambulance_bot.failure_evidence import (
    augment_failure_detail,
    capture_failure_artifacts,
    classify_browser_failure,
    probe_browser_runtime,
)


class _FakeProcess:
    def __init__(self, return_code=None):
        self.return_code = return_code

    def poll(self):
        return self.return_code


class _FakeDriver:
    def __init__(self, *, process_return_code=None, screenshot_error=None, page_source="<html>stuck</html>"):
        self.service = SimpleNamespace(process=_FakeProcess(process_return_code))
        self.capabilities = {
            "browserVersion": "150.0.7871.127",
            "goog:chromeOptions": {"debuggerAddress": "127.0.0.1:9222"},
        }
        self.screenshot_error = screenshot_error
        self.page_source = page_source

    def save_screenshot(self, path):
        if self.screenshot_error:
            raise self.screenshot_error
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfailure")
        return True


class FailureEvidenceTests(unittest.TestCase):
    def test_renderer_timeout_with_live_devtools_is_webpage_stall(self):
        diagnosis = classify_browser_failure(
            RuntimeError("timeout: Timed out receiving message from renderer: 45.000"),
            {"chromedriver_alive": True, "devtools_reachable": True},
        )

        self.assertEqual(diagnosis["category"], "web_renderer_timeout")
        self.assertIn("網頁", diagnosis["reason"])
        self.assertNotIn("ChromeDriver 已結束", diagnosis["reason"])

    def test_renderer_timeout_without_devtools_is_chrome_unresponsive(self):
        diagnosis = classify_browser_failure(
            RuntimeError("timeout: Timed out receiving message from renderer: -0.012"),
            {"chromedriver_alive": True, "devtools_reachable": False},
        )

        self.assertEqual(diagnosis["category"], "chrome_unresponsive")
        self.assertIn("Chrome", diagnosis["reason"])

    def test_ended_chromedriver_takes_priority(self):
        diagnosis = classify_browser_failure(
            RuntimeError("invalid session id"),
            {"chromedriver_alive": False, "devtools_reachable": False},
        )

        self.assertEqual(diagnosis["category"], "chromedriver_ended")

    def test_ordinary_timeout_with_live_devtools_is_page_timeout(self):
        diagnosis = classify_browser_failure(
            TimeoutError("waiting for save button timed out"),
            {"chromedriver_alive": True, "devtools_reachable": True},
        )

        self.assertEqual(diagnosis["category"], "web_page_timeout")

    def test_probe_uses_process_and_devtools_endpoint(self):
        driver = _FakeDriver()
        response = SimpleNamespace(__enter__=lambda value: value, __exit__=lambda *args: None)

        with patch("ambulance_bot.failure_evidence.urlopen", return_value=response) as open_url:
            probe = probe_browser_runtime(driver)

        self.assertTrue(probe["chromedriver_alive"])
        self.assertTrue(probe["devtools_reachable"])
        self.assertEqual(probe["chrome_version"], "150.0.7871.127")
        open_url.assert_called_once()
        self.assertIn("/json/version", open_url.call_args.args[0])

    def test_capture_writes_screenshot_before_html_and_json_metadata(self):
        driver = _FakeDriver()
        error = RuntimeError("timeout: Timed out receiving message from renderer: 45.000")

        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "ambulance_bot.failure_evidence.probe_browser_runtime",
                return_value={
                    "chromedriver_alive": True,
                    "devtools_reachable": True,
                    "chrome_version": "150.0.7871.127",
                },
            ):
                evidence = capture_failure_artifacts(
                    driver,
                    Path(tmp),
                    "task/unsafe",
                    "vehicle_mileage",
                    vehicle="新坡92",
                    exception=error,
                )

            screenshot = Path(evidence["screenshot_path"])
            html = Path(evidence["html_path"])
            metadata = Path(evidence["metadata_path"])
            self.assertTrue(screenshot.is_file())
            self.assertTrue(html.is_file())
            self.assertTrue(metadata.is_file())
            self.assertNotIn("/", screenshot.name)
            self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["category"], "web_renderer_timeout")
            self.assertIn("[browser_failure:web_renderer_timeout]", augment_failure_detail("failed", evidence))

    def test_capture_records_screenshot_error_without_hiding_diagnosis(self):
        driver = _FakeDriver(screenshot_error=RuntimeError("Chrome not reachable"))

        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "ambulance_bot.failure_evidence.probe_browser_runtime",
                return_value={
                    "chromedriver_alive": True,
                    "devtools_reachable": False,
                    "chrome_version": "150.0.7871.127",
                },
            ):
                evidence = capture_failure_artifacts(
                    driver,
                    Path(tmp),
                    "task-2",
                    "disinfection",
                    exception=RuntimeError("disconnected: not connected to DevTools"),
                )

            self.assertEqual(evidence["category"], "chrome_unresponsive")
            self.assertEqual(evidence["screenshot_path"], "")
            self.assertIn("Chrome not reachable", evidence["screenshot_error"])
            metadata = json.loads(Path(evidence["metadata_path"]).read_text(encoding="utf-8"))
            self.assertIn("Chrome not reachable", metadata["screenshot_error"])


if __name__ == "__main__":
    unittest.main()
