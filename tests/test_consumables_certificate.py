import hashlib
import importlib
from pathlib import Path
import ssl
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import consumables_login
from selenium.common.exceptions import InsecureCertificateException
from ambulance_bot.failure_evidence import classify_browser_failure
from ambulance_bot.site_diagnostics import make_site_result


class ConsumablesCertificateTests(unittest.TestCase):
    def login(self, driver):
        with patch.object(consumables_login, "_acs_credential_attempts", return_value=[]), patch.object(
            consumables_login, "_fill_login_form"
        ) as fill, patch.object(consumables_login, "_wait_until_sso_login_finished"), patch.object(
            consumables_login, "_sso_login_succeeded", return_value=True
        ):
            self.fill = fill
            consumables_login._login_sso(driver, "synthetic-account", "synthetic-password", None, None)
        return fill

    def test_certificate_repair_precedes_credentials_and_rechecks_the_page(self):
        driver = Mock()
        driver.execute_script.side_effect = ["NET::ERR_CERT_AUTHORITY_INVALID", ""]
        with patch.object(consumables_login, "repair_consumables_certificate_chain", create=True, return_value=True) as repair:
            fill = self.login(driver)
        repair.assert_called_once_with()
        self.assertEqual(driver.get.call_args_list, [((consumables_login.SSO_URL,),), ((consumables_login.SSO_URL,),)])
        fill.assert_called_once()

    def test_persistent_certificate_warning_stops_before_sending_credentials(self):
        driver = Mock()
        driver.execute_script.return_value = "NET::ERR_CERT_AUTHORITY_INVALID"
        with patch.object(consumables_login, "repair_consumables_certificate_chain", create=True, return_value=True) as repair:
            with self.assertRaisesRegex(RuntimeError, "ERR_CERT_AUTHORITY_INVALID"):
                self.login(driver)
        repair.assert_called_once_with()
        self.assertEqual(driver.get.call_count, 2)
        self.fill.assert_not_called()

    def test_webdriver_certificate_navigation_error_can_use_the_same_safe_repair(self):
        driver = Mock()
        driver.get.side_effect = [InsecureCertificateException("insecure certificate"), None]
        driver.execute_script.side_effect = ["NET::ERR_CERT_AUTHORITY_INVALID", ""]
        with patch.object(consumables_login, "repair_consumables_certificate_chain", return_value=True) as repair:
            fill = self.login(driver)
        repair.assert_called_once_with()
        fill.assert_called_once()

    def test_expired_or_wrong_host_certificates_are_never_repaired_or_bypassed(self):
        for code in ("ERR_CERT_DATE_INVALID", "ERR_CERT_COMMON_NAME_INVALID"):
            with self.subTest(code=code):
                driver = Mock()
                driver.execute_script.return_value = code
                with patch.object(consumables_login, "repair_consumables_certificate_chain", create=True) as repair:
                    with self.assertRaisesRegex(RuntimeError, code):
                        self.login(driver)
                repair.assert_not_called()
                self.fill.assert_not_called()

    def test_normal_login_never_changes_the_certificate_store(self):
        driver = Mock()
        driver.execute_script.return_value = ""
        with patch.object(consumables_login, "repair_consumables_certificate_chain") as repair:
            fill = self.login(driver)
        repair.assert_not_called()
        self.assertEqual(driver.get.call_count, 1)
        fill.assert_called_once()

    def test_failed_store_repair_stops_without_submitting_credentials(self):
        driver = Mock()
        driver.execute_script.return_value = "NET::ERR_CERT_AUTHORITY_INVALID"
        with patch.object(consumables_login, "repair_consumables_certificate_chain", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "ERR_CERT_AUTHORITY_INVALID"):
                self.login(driver)
        self.fill.assert_not_called()

    def test_certificate_failure_has_specific_nas_diagnostics(self):
        diagnosis = classify_browser_failure(
            RuntimeError("net::ERR_CERT_AUTHORITY_INVALID"),
            {"chromedriver_alive": True, "devtools_reachable": True},
        )
        self.assertEqual(diagnosis["category"], "certificate_error")
        result = make_site_result(
            "consumables", "一站通耗材", "consumables_failed",
            "net::ERR_CERT_AUTHORITY_INVALID [execution_stage:consumables:登入一站通]",
        )
        self.assertEqual(result.failure_stage, "登入一站通")
        self.assertIn("憑證", result.failure_reason)
        self.assertIn("中繼", result.next_action)

    def test_only_pinned_public_intermediate_is_added_to_current_user_ca_store(self):
        module = importlib.import_module("ambulance_bot.consumables_certificate")
        cert = Path(module.CERTIFICATE_PATH).read_text(encoding="ascii")
        self.assertEqual(hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest(), module.CERTIFICATE_SHA256)
        with patch.object(module.sys, "platform", "win32"), patch.object(
            module.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            self.assertTrue(module.repair_consumables_certificate_chain())
        self.assertEqual(run.call_args.args[0][:4], ["certutil.exe", "-user", "-addstore", "CA"])
        self.assertEqual(run.call_args.args[0][4], str(module.CERTIFICATE_PATH))
        self.assertLessEqual(run.call_args.kwargs["timeout"], 10)

    def test_tampered_certificate_is_rejected_before_changing_any_store(self):
        module = importlib.import_module("ambulance_bot.consumables_certificate")
        with patch.object(module.sys, "platform", "win32"), patch.object(
            module, "CERTIFICATE_SHA256", "0" * 64
        ), patch.object(module.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "指紋"):
                module.repair_consumables_certificate_chain()
        run.assert_not_called()
