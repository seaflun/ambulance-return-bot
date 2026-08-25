import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import disinfect
from ambulance_bot.duty_credentials import save_duty_automation_credentials
from ambulance_bot.models import AmbulanceReturnRequest


class DisinfectionCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = {
            key: os.environ.get(key)
            for key in ("DUTY_SAVED_LOGIN_PATH", "DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        }
        os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(self.tmp.name) / "saved_login.json")
        os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_credential_attempts_prioritize_explicit_on_duty_before_task_people(self):
        save_duty_automation_credentials(
            [
                {"actor_no": "7", "name": "值班人員", "user_id": "tyfd00007", "password": "pw"},
                {"actor_no": "21", "name": "張家和", "user_id": "tyfd01317", "password": "pw"},
                {"actor_no": "12", "name": "王昱勛", "user_id": "tyfd01987", "password": "pw"},
            ],
            last_selected="tyfd01317",
            last_synced="tyfd00007",
        )
        request = AmbulanceReturnRequest(
            task_id="task-disinfection-login",
            created_at=datetime.now(),
            raw_text="",
            driver="王昱勛",
            personnel=["張家和", "王昱勛"],
            personnel_accounts=["tyfd01317", "tyfd01987"],
        )

        attempts = disinfect._disinfection_credential_attempts(request)

        self.assertEqual(
            [(credential.user_id, source) for credential, source in attempts],
            [
                ("tyfd00007", "值班人員"),
                ("tyfd01987", "任務司機"),
                ("tyfd01317", "出勤人員"),
            ],
        )

    def test_credential_attempts_append_selected_sync_account_after_personnel(self):
        save_duty_automation_credentials(
            [
                {"actor_no": "21", "name": "張家和", "user_id": "tyfd01317", "password": "pw"},
                {"actor_no": "12", "name": "王昱勛", "user_id": "tyfd01987", "password": "pw"},
                {"actor_no": "99", "name": "同步備援", "user_id": "tyfd09999", "password": "pw"},
            ],
            last_selected="tyfd09999",
        )
        request = AmbulanceReturnRequest(
            task_id="task-disinfection-sync-fallback",
            created_at=datetime.now(),
            raw_text="",
            driver="王昱勛",
            personnel=["張家和", "王昱勛"],
            personnel_accounts=["tyfd01317", "tyfd01987"],
        )

        attempts = disinfect._disinfection_credential_attempts(request)

        self.assertEqual(
            [(credential.user_id, source) for credential, source in attempts],
            [
                ("tyfd01987", "任務司機"),
                ("tyfd01317", "出勤人員"),
                ("tyfd09999", "同步帳號"),
            ],
        )

    def test_login_error_page_is_not_treated_as_logged_in(self):
        class ErrorPageDriver:
            current_url = "https://emsdt.tyfd.gov.tw/EmmWeb/Error.aspx"

            def execute_script(self, script):
                if "return document.body ? document.body.innerText : '';" in script:
                    return "帳號、密碼、驗證碼錯誤或帳號不存在!"
                return True

        self.assertFalse(disinfect._is_logged_in(ErrorPageDriver()))

    def test_login_failure_captures_task_and_vehicle_evidence_and_closes_browser_by_default(self):
        class FakeDriver:
            def __init__(self):
                self.quit_calls = 0

            def set_page_load_timeout(self, _seconds):
                pass

            def set_script_timeout(self, _seconds):
                pass

            def get(self, _url):
                pass

            def quit(self):
                self.quit_calls += 1

        request = AmbulanceReturnRequest(
            task_id="disinfection-login",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        credential = SimpleNamespace(user_id="worker", password="secret")
        driver = FakeDriver()
        with patch.object(
            disinfect,
            "_disinfection_credential_attempts",
            return_value=[(credential, "同步帳號")],
        ), patch.object(
            disinfect,
            "create_chrome_driver_with_retry",
            return_value=driver,
        ) as create_driver, patch.object(
            disinfect,
            "apply_tile",
        ), patch.object(
            disinfect,
            "_login_once",
            side_effect=RuntimeError("Chrome not reachable"),
        ), patch.object(
            disinfect,
            "capture_failure_artifacts",
            return_value={"category": "chrome_unresponsive", "reason": "Chrome 無回應"},
        ) as capture:
            with self.assertRaisesRegex(RuntimeError, "browser_failure:chrome_unresponsive"):
                disinfect.login_and_get_driver(
                    request=request,
                    artifacts_dir=Path(self.tmp.name),
                )

        capture.assert_called_once()
        self.assertEqual(capture.call_args.args[2], request.task_id)
        self.assertEqual(capture.call_args.args[3], "disinfection")
        self.assertEqual(capture.call_args.kwargs["vehicle"], request.vehicle)
        self.assertTrue(create_driver.call_args.kwargs["fresh_session"])
        self.assertEqual(driver.quit_calls, 1)

    def test_login_failure_preserves_evidence_when_browser_quit_fails(self):
        class FakeDriver:
            def set_page_load_timeout(self, _seconds):
                pass

            def set_script_timeout(self, _seconds):
                pass

            def get(self, _url):
                pass

            def quit(self):
                raise RuntimeError("quit failed")

        request = AmbulanceReturnRequest(
            task_id="disinfection-login-quit-failure",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        credential = SimpleNamespace(user_id="worker", password="secret")
        with patch.dict(
            os.environ, {"DISINFECTION_CLOSE_BROWSER_ON_LOGIN_FAILURE": "true"}
        ), patch.object(
            disinfect,
            "_disinfection_credential_attempts",
            return_value=[(credential, "同步帳號")],
        ), patch.object(
            disinfect,
            "create_chrome_driver_with_retry",
            return_value=FakeDriver(),
        ), patch.object(
            disinfect,
            "apply_tile",
        ), patch.object(
            disinfect,
            "_login_once",
            side_effect=RuntimeError("Chrome not reachable"),
        ), patch.object(
            disinfect,
            "capture_failure_artifacts",
            return_value={"category": "chrome_unresponsive", "reason": "Chrome 無回應"},
        ):
            with self.assertRaisesRegex(RuntimeError, "browser_failure:chrome_unresponsive"):
                disinfect.login_and_get_driver(
                    request=request,
                    artifacts_dir=Path(self.tmp.name),
                )


if __name__ == "__main__":
    unittest.main()
