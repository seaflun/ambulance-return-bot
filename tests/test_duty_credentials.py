import json
import os
import tempfile
import unittest
from pathlib import Path

from ambulance_bot.duty_credentials import (
    list_saved_duty_automation_credentials,
    load_duty_credential,
    load_saved_duty_automation_credential,
    load_synced_worker_credential,
    save_duty_automation_credentials,
    save_duty_automation_credential,
)


class DutyCredentialTests(unittest.TestCase):
    def test_loads_legacy_saved_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            path.write_text(
                json.dumps({"actor_no": "8", "user_id": "user", "password": "pass"}),
                encoding="utf-8",
            )

            credential = load_saved_duty_automation_credential(path)

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "user")
        self.assertEqual(credential.password, "pass")
        self.assertEqual(credential.actor_no, "8")

    def test_loads_last_selected_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            path.write_text(
                json.dumps(
                    {
                        "last_selected": "user2",
                        "accounts": [
                            {"actor_no": "8", "user_id": "user1", "password": "pass1"},
                            {"actor_no": "12", "user_id": "user2", "password": "pass2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            credential = load_saved_duty_automation_credential(path)

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "user2")
        self.assertEqual(credential.password, "pass2")

    def test_lists_synced_accounts_with_last_selected_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            path.write_text(
                json.dumps(
                    {
                        "last_selected": "user2",
                        "accounts": [
                            {"actor_no": "8", "user_id": "user1", "password": "pass1"},
                            {"actor_no": "12", "user_id": "user2", "password": "pass2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            credentials = list_saved_duty_automation_credentials(path)

        self.assertEqual([item.user_id for item in credentials], ["user2", "user1"])
        self.assertEqual(credentials[0].password, "pass2")

    def test_saves_synced_credential_for_local_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"

            saved_path = save_duty_automation_credential(
                "synced-user",
                "synced-pass",
                actor_no="15",
                display_name="15番 synced-user",
                name="測試員",
                id_number="B123017532",
                path=path,
            )
            credential = load_saved_duty_automation_credential(saved_path)

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "synced-user")
        self.assertEqual(credential.password, "synced-pass")
        self.assertEqual(credential.actor_no, "15")
        self.assertEqual(credential.name, "測試員")
        self.assertEqual(credential.id_number, "B123017532")

    def test_saves_multiple_synced_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"

            saved_path = save_duty_automation_credentials(
                [
                    {
                        "actor_no": "8",
                        "user_id": "user8",
                        "password": "pass8",
                        "display_name": "8番 曾彥綸",
                        "name": "曾彥綸",
                        "id_number": "B123017532",
                    },
                    {
                        "actor_no": "9",
                        "user_id": "user9",
                        "password": "pass9",
                        "display_name": "9番 某某",
                        "name": "某某",
                        "id_number": "",
                    },
                ],
                last_selected="user8",
                path=path,
            )
            credentials = list_saved_duty_automation_credentials(saved_path)

        self.assertEqual([item.user_id for item in credentials], ["user8", "user9"])
        self.assertEqual(credentials[0].display_name, "8番 曾彥綸")
        self.assertEqual(credentials[0].name, "曾彥綸")
        self.assertEqual(credentials[0].id_number, "B123017532")

    def test_saves_synced_credential_without_duplicate_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"

            save_duty_automation_credential("synced-user", "old-pass", actor_no="15", path=path)
            save_duty_automation_credential("synced-user", "new-pass", actor_no="15", path=path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            credential = load_saved_duty_automation_credential(path)

        self.assertEqual(len(payload["accounts"]), 1)
        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.password, "new-pass")

    def test_load_duty_credential_prefers_synced_account_over_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(path)
                os.environ["DUTY_ACCOUNT"] = "env-user"
                os.environ["DUTY_PASSWORD"] = "env-pass"
                save_duty_automation_credentials(
                    [
                        {"actor_no": "8", "user_id": "synced-user", "password": "synced-pass"},
                    ],
                    last_selected="synced-user",
                    path=path,
                )

                credential = load_duty_credential()
                selected = load_synced_worker_credential()
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_account is None:
                    os.environ.pop("DUTY_ACCOUNT", None)
                else:
                    os.environ["DUTY_ACCOUNT"] = previous_account
                if previous_password is None:
                    os.environ.pop("DUTY_PASSWORD", None)
                else:
                    os.environ["DUTY_PASSWORD"] = previous_password

        self.assertIsNotNone(credential)
        self.assertIsNotNone(selected)
        assert credential is not None
        assert selected is not None
        self.assertEqual(credential.user_id, "synced-user")
        self.assertEqual(selected.user_id, "synced-user")

    def test_load_duty_credential_prefers_saved_personnel_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(path)
                save_duty_automation_credentials(
                    [
                        {"actor_no": "8", "user_id": "tyfd00008", "password": "personnel-pass"},
                        {"actor_no": "15", "user_id": "tyfd01510", "password": "selected-pass"},
                    ],
                    last_selected="tyfd01510",
                    path=path,
                )

                credential = load_duty_credential(["tyfd00008"])
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "tyfd00008")
        self.assertEqual(credential.password, "personnel-pass")


if __name__ == "__main__":
    unittest.main()
