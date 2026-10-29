import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ambulance_bot import failure_evidence
from ambulance_bot.site_diagnostics import diagnostic_payload
from tests.test_failure_evidence import _FakeDriver


class NetworkErrorCaptureTests(unittest.TestCase):
    def capture(self, driver):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            failure_evidence, "probe_browser_runtime",
            return_value={"chromedriver_alive": True, "devtools_reachable": True},
        ):
            return failure_evidence.capture_failure_artifacts(
                driver, Path(directory), "anonymous-network", "disinfection",
                exception=RuntimeError("timed out receiving message from renderer"),
            )

    def test_nested_error_code_overrides_renderer_timeout_without_scanning_script_examples(self):
        driver = _FakeDriver(page_source=(
            '<html><script>const example="ERR_NAME_NOT_RESOLVED"</script>'
            '<div class="error-code"><span>ERR_CONNECTION_RESET</span></div></html>'
        ))
        result = self.capture(driver)
        self.assertEqual("network_connection", result["category"])
        self.assertEqual("ERR_CONNECTION_RESET", result["network_error"])
        detail = failure_evidence.augment_failure_detail("消毒紀錄登入失敗", result)
        self.assertIn("ERR_CONNECTION_RESET", detail)
        self.assertEqual("network_connection", diagnostic_payload("disinfection", "disinfection_failed", detail)["exception_type"])

    def test_cdp_reads_the_error_code_when_webdriver_cannot_read_html(self):
        class Driver(_FakeDriver):
            def __init__(self):
                super().__init__()
                del self.page_source

            def __getattr__(self, name):
                if name == "page_source":
                    raise RuntimeError("HTML read timed out")
                raise AttributeError(name)

            def execute_cdp_cmd(self, command, parameters):
                if command == "Page.captureScreenshot":
                    raise RuntimeError("use ordinary screenshot")
                if command == "Runtime.evaluate":
                    self.asserted_timeout = parameters["timeout"]
                    return {"result": {"value": "ERR_CONNECTION_RESET"}}
                raise AssertionError(command)

        driver = Driver()
        result = self.capture(driver)
        self.assertEqual("network_connection", result["category"])
        self.assertEqual("ERR_CONNECTION_RESET", result["network_error"])
        self.assertIn("HTML read timed out", result["html_error"])
        self.assertLessEqual(driver.asserted_timeout, 1000)

    def test_an_unrelated_page_script_does_not_prove_a_network_error(self):
        result = self.capture(_FakeDriver(page_source='<html><script>const example="ERR_CONNECTION_RESET"</script></html>'))
        self.assertEqual("web_renderer_timeout", result["category"])

    def test_void_elements_and_script_samples_cannot_leak_into_an_empty_error_code(self):
        result = self.capture(_FakeDriver(page_source=(
            '<div class="error-code"><br><script>const sample="ERR_CONNECTION_RESET"</script></div>'
            '<p>Example ERR_NAME_NOT_RESOLVED</p>'
        )))
        self.assertEqual("web_renderer_timeout", result["category"])

    def test_a_live_probe_failure_keeps_the_uncertainty_and_original_diagnosis(self):
        class Driver(_FakeDriver):
            def execute_cdp_cmd(self, command, parameters):
                raise RuntimeError("DevTools observation unavailable")

        result = self.capture(Driver())
        self.assertEqual("web_renderer_timeout", result["category"])
        self.assertIn("DevTools observation unavailable", result["network_error_probe_error"])

    def test_network_code_has_priority_over_a_stale_renderer_marker(self):
        result = diagnostic_payload("disinfection", "disinfection_failed",
                                    "ERR_CONNECTION_RESET [browser_failure:web_renderer_timeout]")
        self.assertEqual("network_connection", result["exception_type"])

    def test_raw_network_error_codes_are_recognized_even_without_a_browser_marker(self):
        for code in ["ERR_CONNECTION_RESET", "ERR_CONNECTION_REFUSED", "ERR_NAME_NOT_RESOLVED",
                     "ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED", "ERR_ADDRESS_UNREACHABLE"]:
            with self.subTest(code=code):
                diagnosis = diagnostic_payload("disinfection", "disinfection_failed", f"消毒登入失敗 net::{code}")
                self.assertEqual("network_connection", diagnosis["exception_type"])
                self.assertIn(code, diagnosis["failure_reason"])
