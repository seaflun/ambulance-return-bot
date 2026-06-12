import os
import tempfile
import unittest
from pathlib import Path

from ambulance_bot.duty_credentials import save_duty_automation_credentials
from ambulance_bot.login_audit import (
    consumables_login_audit,
    duty_work_log_login_audit,
    mask_login_account,
    site_login_account_summaries,
    vehicle_mileage_login_audit,
)
from ambulance_bot.models import AmbulanceReturnRequest


class LoginAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = {
            key: os.environ.get(key)
            for key in (
                "DUTY_SAVED_LOGIN_PATH",
                "DUTY_SAVED_LOGIN_PATH_OVERRIDE",
                "ACS_ACCOUNT",
                "ACS_PASSWORD",
                "PPE_ACCOUNT",
                "PPE_PASSWORD",
                "DUTY_ACCOUNT",
                "DUTY_PASSWORD",
            )
        }
        os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(self.tmp.name) / "saved_login.json")
        os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
        for key in ("ACS_ACCOUNT", "ACS_PASSWORD", "PPE_ACCOUNT", "PPE_PASSWORD", "DUTY_ACCOUNT", "DUTY_PASSWORD"):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_duty_work_log_audit_uses_driver_candidate(self):
        save_duty_automation_credentials(
            [
                {"actor_no": "21", "name": "張家和", "user_id": "tyfd01317", "password": "pw"},
                {"actor_no": "12", "name": "王昱勛", "user_id": "tyfd01987", "password": "pw"},
            ],
            last_selected="tyfd01317",
        )
        request = AmbulanceReturnRequest(
            task_id="task-audit",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            driver="王昱勛",
            personnel=["張家和", "王昱勛"],
            personnel_accounts=["tyfd01317", "tyfd01987"],
        )

        audit = duty_work_log_login_audit(request)

        self.assertIn("工作=任務司機優先", audit)
        self.assertIn("12番 王昱勛 - tyfd01987", audit)
        self.assertNotIn("21番 張家和 - tyfd01317", audit)

    def test_consumables_audit_masks_id_number(self):
        os.environ["ACS_ACCOUNT"] = "A123456789"
        os.environ["ACS_PASSWORD"] = "env-secret"
        save_duty_automation_credentials(
            [{"actor_no": "21", "name": "張家和", "user_id": "tyfd01317", "id_number": "S124774209", "password": "pw"}],
            last_selected="tyfd01317",
        )

        audit = consumables_login_audit()

        self.assertIn("耗材=公務電腦同步帳號", audit)
        self.assertIn("21番 張家和 - S124***209", audit)
        self.assertNotIn("S124774209", audit)
        self.assertNotIn("ACS 環境設定", audit)
        self.assertNotIn("A123456789", audit)

    def test_vehicle_mileage_audit_uses_synced_worker_account(self):
        save_duty_automation_credentials(
            [{"actor_no": "21", "name": "張家和", "user_id": "tyfd01317", "password": "pw"}],
            last_selected="tyfd01317",
        )

        audit = vehicle_mileage_login_audit()

        self.assertIn("里程=公務電腦同步帳號", audit)
        self.assertIn("21番 張家和 - tyfd01317", audit)

    def test_mask_login_account_only_masks_id_number_style(self):
        self.assertEqual(mask_login_account("S124774209"), "S124***209")
        self.assertEqual(mask_login_account("tyfd01317"), "tyfd01317")

    def test_site_login_account_summaries_lists_all_sites(self):
        save_duty_automation_credentials(
            [
                {"actor_no": "21", "name": "張家和", "user_id": "tyfd01317", "id_number": "S124774209", "password": "pw"},
                {"actor_no": "12", "name": "王昱勛", "user_id": "tyfd01987", "password": "pw"},
            ],
            last_selected="tyfd01317",
        )
        request = AmbulanceReturnRequest(
            task_id="task-summary",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            driver="王昱勛",
            personnel=["張家和", "王昱勛"],
            personnel_accounts=["tyfd01317", "tyfd01987"],
        )

        summaries = site_login_account_summaries(request)

        self.assertEqual(summaries["duty_work_log"], "12番 王昱勛 - tyfd01987（任務司機優先）")
        self.assertEqual(summaries["vehicle_mileage"], "21番 張家和 - tyfd01317（同步帳號）")
        self.assertEqual(summaries["disinfection"], "21番 張家和 - tyfd01317（同步帳號）")
        self.assertEqual(summaries["consumables"], "21番 張家和 - S124***209（同步帳號）")

    def test_site_login_account_summaries_can_match_driver_name_without_case_account(self):
        save_duty_automation_credentials(
            [
                {"actor_no": "21", "name": "張家和", "user_id": "tyfd01317", "id_number": "S124774209", "password": "pw"},
                {"actor_no": "12", "name": "王昱勛", "user_id": "tyfd01987", "password": "pw"},
            ],
            last_selected="tyfd01317",
        )
        request = AmbulanceReturnRequest(
            task_id="task-driver-name",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            driver="王昱勛",
            personnel=[],
            personnel_accounts=[],
        )

        summaries = site_login_account_summaries(request)

        self.assertEqual(summaries["duty_work_log"], "12番 王昱勛 - tyfd01987（任務司機優先）")
        self.assertEqual(summaries["vehicle_mileage"], "21番 張家和 - tyfd01317（同步帳號）")


if __name__ == "__main__":
    unittest.main()
