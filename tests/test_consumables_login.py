import os
import tempfile
import unittest
from pathlib import Path

from ambulance_bot.duty_credentials import save_duty_automation_credentials
from ambulance_bot.models import AmbulanceReturnRequest
from consumables_login import _case_id_sid_fragments, _consumable_sid_score, _emm_temsis_id_from_href, _load_acs_credentials


class ConsumablesLoginTests(unittest.TestCase):
    def test_case_id_fragments_match_consumable_sid(self):
        self.assertEqual(
            _case_id_sid_fragments("20260602011652012"),
            ["20260602011652012", "20260602011652", "011652012", "011652"],
        )
        self.assertEqual(_consumable_sid_score("20260602011652012", "20260602101003011652"), 10)
        self.assertEqual(_consumable_sid_score("20260606163336003", "2026060610100316333603"), 10)
        self.assertEqual(_consumable_sid_score("20260606163336003", "20260606163336003"), 20)

    def test_extracts_emm_temsis_id_from_href(self):
        href = "/ACS/ACS15002?emmTemsisid=2026060210100301165202"
        self.assertEqual(_emm_temsis_id_from_href(href), "2026060210100301165202")

    def test_load_acs_credentials_uses_synced_id_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_account = os.environ.pop("ACS_ACCOUNT", None)
            previous_password = os.environ.pop("ACS_PASSWORD", None)
            os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
            try:
                save_duty_automation_credentials(
                    [
                        {
                            "actor_no": "8",
                            "user_id": "tyfd01510",
                            "password": "secret",
                            "display_name": "8番 曾彥綸",
                            "name": "曾彥綸",
                            "id_number": "B123017532",
                        }
                    ],
                    last_selected="8",
                )
                request = AmbulanceReturnRequest(
                    task_id="task-1",
                    created_at=__import__("datetime").datetime.now(),
                    raw_text="",
                    personnel_accounts=["tyfd01510"],
                )

                self.assertEqual(_load_acs_credentials(request), ("B123017532", "secret"))
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_account is not None:
                    os.environ["ACS_ACCOUNT"] = previous_account
                if previous_password is not None:
                    os.environ["ACS_PASSWORD"] = previous_password

    def test_load_acs_credentials_keeps_env_override(self):
        previous_account = os.environ.get("ACS_ACCOUNT")
        previous_password = os.environ.get("ACS_PASSWORD")
        os.environ["ACS_ACCOUNT"] = "A123456789"
        os.environ["ACS_PASSWORD"] = "env-secret"
        try:
            self.assertEqual(_load_acs_credentials(), ("A123456789", "env-secret"))
        finally:
            if previous_account is None:
                os.environ.pop("ACS_ACCOUNT", None)
            else:
                os.environ["ACS_ACCOUNT"] = previous_account
            if previous_password is None:
                os.environ.pop("ACS_PASSWORD", None)
            else:
                os.environ["ACS_PASSWORD"] = previous_password


if __name__ == "__main__":
    unittest.main()
