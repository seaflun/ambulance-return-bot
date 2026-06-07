import json
import tempfile
import unittest
from pathlib import Path

from ambulance_bot.duty_credentials import (
    list_saved_duty_automation_credentials,
    load_saved_duty_automation_credential,
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


if __name__ == "__main__":
    unittest.main()
