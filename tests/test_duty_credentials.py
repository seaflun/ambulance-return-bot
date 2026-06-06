import json
import tempfile
import unittest
from pathlib import Path

from ambulance_bot.duty_credentials import load_saved_duty_automation_credential


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


if __name__ == "__main__":
    unittest.main()
