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

    def test_credential_attempts_put_driver_before_other_personnel(self):
        save_duty_automation_credentials(
            [
                {"actor_no": "21", "name": "張家和", "user_id": "tyfd01317", "password": "pw"},
                {"actor_no": "12", "name": "王昱勛", "user_id": "tyfd01987", "password": "pw"},
            ],
            last_selected="tyfd01317",
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
            [("tyfd01987", "任務司機"), ("tyfd01317", "出勤人員")],
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

    def test_login_failure_captures_task_and_vehicle_evidence_before_optional_quit(self):
        class FakeDriver:
            def set_page_load_timeout(self, _seconds):
                pass

            def set_script_timeout(self, _seconds):
                pass

            def get(self, _url):
                pass

            def quit(self):
                pass

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


if __name__ == "__main__":
    unittest.main()
