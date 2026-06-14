import os
import tempfile
import unittest
from pathlib import Path

from ambulance_bot.duty_credentials import save_duty_automation_credentials
from ambulance_bot.models import AmbulanceReturnRequest
from consumables_login import (
    _assert_consumable_rows_match,
    _case_id_sid_fragments,
    _consumable_sid_score,
    _emm_temsis_id_from_href,
    _load_acs_credentials,
    _wait_for_consumable_detail_page,
    save_consumables_record_enabled,
)


class ConsumablesLoginTests(unittest.TestCase):
    def test_assert_consumable_rows_match_allows_expected_rows(self):
        class FakeDriver:
            def execute_script(self, script):
                return [
                    {"itemId": "821", "quantity": "2"},
                    {"itemId": "816", "quantity": "02"},
                ]

        _assert_consumable_rows_match(
            FakeDriver(),
            [{"itemId": "816", "quantity": "2"}, {"itemId": "821", "quantity": "2"}],
            "耗材儲存前",
        )

    def test_assert_consumable_rows_match_rejects_extra_duplicate_row(self):
        class FakeDriver:
            def execute_script(self, script):
                return [
                    {"itemId": "816", "quantity": "2"},
                    {"itemId": "816", "quantity": "2"},
                    {"itemId": "821", "quantity": "2"},
                ]

        with self.assertRaisesRegex(RuntimeError, "停止儲存"):
            _assert_consumable_rows_match(
                FakeDriver(),
                [{"itemId": "816", "quantity": "2"}, {"itemId": "821", "quantity": "2"}],
                "耗材儲存前",
            )

    def test_save_consumables_record_flag_defaults_on(self):
        previous = os.environ.get("SAVE_CONSUMABLES_RECORD")
        try:
            os.environ.pop("SAVE_CONSUMABLES_RECORD", None)
            self.assertTrue(save_consumables_record_enabled())
            os.environ["SAVE_CONSUMABLES_RECORD"] = "0"
            self.assertFalse(save_consumables_record_enabled())
            os.environ["SAVE_CONSUMABLES_RECORD"] = "yes"
            self.assertTrue(save_consumables_record_enabled())
        finally:
            if previous is None:
                os.environ.pop("SAVE_CONSUMABLES_RECORD", None)
            else:
                os.environ["SAVE_CONSUMABLES_RECORD"] = previous

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

    def test_consumable_detail_wait_fails_when_session_returns_to_sso(self):
        class FakeElement:
            text = ""

        class FakeDriver:
            current_url = "https://nfaemsap3.nfa.gov.tw/SSO/login"

            def find_elements(self, by, value):
                if value == "verificationCode":
                    return [object()]
                return []

            def find_element(self, by, value):
                return FakeElement()

        class FakeWait:
            def until(self, predicate):
                return predicate(FakeDriver())

        self.assertFalse(_wait_for_consumable_detail_page(FakeDriver(), FakeWait()))

    def test_load_acs_credentials_uses_selected_synced_id_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_account = os.environ.get("ACS_ACCOUNT")
            previous_password = os.environ.get("ACS_PASSWORD")
            os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
            os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
            os.environ["ACS_ACCOUNT"] = "A123456789"
            os.environ["ACS_PASSWORD"] = "env-secret"
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
                        },
                        {
                            "actor_no": "9",
                            "user_id": "tyfd00009",
                            "password": "selected-secret",
                            "display_name": "9番 測試員",
                            "name": "測試員",
                            "id_number": "C123456789",
                        }
                    ],
                    last_selected="9",
                )
                request = AmbulanceReturnRequest(
                    task_id="task-1",
                    created_at=__import__("datetime").datetime.now(),
                    raw_text="",
                    personnel_accounts=["tyfd01510"],
                )

                self.assertEqual(_load_acs_credentials(request), ("C123456789", "selected-secret"))
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
                if previous_account is None:
                    os.environ.pop("ACS_ACCOUNT", None)
                else:
                    os.environ["ACS_ACCOUNT"] = previous_account
                if previous_password is None:
                    os.environ.pop("ACS_PASSWORD", None)
                else:
                    os.environ["ACS_PASSWORD"] = previous_password

    def test_load_acs_credentials_ignores_legacy_env_override(self):
        previous_account = os.environ.get("ACS_ACCOUNT")
        previous_password = os.environ.get("ACS_PASSWORD")
        previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        os.environ["ACS_ACCOUNT"] = "A123456789"
        os.environ["ACS_PASSWORD"] = "env-secret"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
            os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
            try:
                with self.assertRaisesRegex(RuntimeError, "同步含身分證字號"):
                    _load_acs_credentials()
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
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
