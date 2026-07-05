import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import ambulance_bot.duty_credentials as duty_credentials_module
from ambulance_bot.duty_credentials import (
    credential_sync_accounts_from_payload,
    decrypt_dpapi,
    encrypt_dpapi,
    list_saved_duty_automation_credentials,
    load_duty_credential,
    load_saved_duty_automation_credential,
    load_synced_worker_credential,
    saved_login_path,
    save_credential_sync_payload,
    save_duty_automation_credentials,
    save_duty_automation_credential,
    select_credential_sync_account,
    set_last_selected_duty_automation_credential,
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

    def test_credential_sync_accounts_from_payload_selects_requested_account(self):
        payload = {
            "user_id": "user9",
            "accounts": [
                {"actor_no": "8", "user_id": "user8", "password": "pass8"},
                {"actor_no": "9", "user_id": "user9", "password": "pass9"},
                {"actor_no": "10", "user_id": "missing-pass"},
            ],
        }

        accounts = credential_sync_accounts_from_payload(payload)
        selected = select_credential_sync_account(accounts, payload)

        self.assertEqual([item["user_id"] for item in accounts], ["user8", "user9"])
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["user_id"], "user9")

    def test_save_credential_sync_payload_keeps_stable_synced_account_in_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                payload = {
                    "actor_no": "9",
                    "accounts": [
                        {"actor_no": "8", "user_id": "user8", "password": "pass8"},
                        {"actor_no": "9", "user_id": "user9", "password": "pass9"},
                    ],
                }

                result = save_credential_sync_payload(payload, path=path)
                credential = load_synced_worker_credential(path)
                synced_env_account = os.environ.get("DUTY_ACCOUNT")
                synced_env_password = os.environ.get("DUTY_PASSWORD")
            finally:
                if previous_account is None:
                    os.environ.pop("DUTY_ACCOUNT", None)
                else:
                    os.environ["DUTY_ACCOUNT"] = previous_account
                if previous_password is None:
                    os.environ.pop("DUTY_PASSWORD", None)
                else:
                    os.environ["DUTY_PASSWORD"] = previous_password

        self.assertIsNotNone(result)
        assert result is not None
        user_id, password, saved_path, count = result
        self.assertEqual(user_id, "user8")
        self.assertEqual(password, "pass8")
        self.assertEqual(saved_path, path)
        self.assertEqual(count, 2)
        self.assertEqual(synced_env_account, "user8")
        self.assertEqual(synced_env_password, "pass8")
        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "user8")
        self.assertEqual(credential.password, "pass8")

    def test_single_incoming_account_does_not_replace_existing_synced_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                save_duty_automation_credentials(
                    [{"actor_no": "8", "user_id": "user8", "password": "pass8"}],
                    last_selected="user8",
                    path=path,
                )
                result = save_credential_sync_payload(
                    {"actor_no": "9", "user_id": "user9", "password": "pass9"},
                    path=path,
                )
                credential = load_synced_worker_credential(path)
                saved_accounts = list_saved_duty_automation_credentials(path=path)
                synced_env_account = os.environ.get("DUTY_ACCOUNT")
            finally:
                if previous_account is None:
                    os.environ.pop("DUTY_ACCOUNT", None)
                else:
                    os.environ["DUTY_ACCOUNT"] = previous_account
                if previous_password is None:
                    os.environ.pop("DUTY_PASSWORD", None)
                else:
                    os.environ["DUTY_PASSWORD"] = previous_password

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[0], "user8")
        self.assertEqual(synced_env_account, "user8")
        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "user8")
        self.assertEqual({item.user_id for item in saved_accounts}, {"user8", "user9"})

    def test_single_incoming_non_actor_8_without_existing_account_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                result = save_credential_sync_payload(
                    {"actor_no": "9", "user_id": "user9", "password": "pass9"},
                    path=path,
                )
                credential = load_synced_worker_credential(path)
                synced_env_account = os.environ.get("DUTY_ACCOUNT")
            finally:
                if previous_account is None:
                    os.environ.pop("DUTY_ACCOUNT", None)
                else:
                    os.environ["DUTY_ACCOUNT"] = previous_account
                if previous_password is None:
                    os.environ.pop("DUTY_PASSWORD", None)
                else:
                    os.environ["DUTY_PASSWORD"] = previous_password

        self.assertIsNone(result)
        self.assertIsNone(credential)
        self.assertNotEqual(synced_env_account, "user9")

    def test_saves_synced_credential_derives_name_without_repeating_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"

            save_duty_automation_credentials(
                [
                    {"actor_no": "8", "user_id": "tyfd01510", "password": "pass", "display_name": "8番 tyfd01510"},
                    {"actor_no": "9", "user_id": "user9", "password": "pass", "display_name": "9番 測試員"},
                ],
                path=path,
            )
            credentials = list_saved_duty_automation_credentials(path)

        self.assertEqual(credentials[0].name, "")
        self.assertEqual(credentials[1].name, "測試員")

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

    @unittest.skipIf(os.name != "nt", "Windows DPAPI fallback only runs on Windows")
    def test_dpapi_falls_back_when_win32crypt_is_missing(self):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "win32crypt":
                raise ImportError("missing win32crypt")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", fake_import):
            encrypted = encrypt_dpapi("secret-pass")
            decrypted = decrypt_dpapi(encrypted)

        self.assertTrue(encrypted)
        self.assertEqual(decrypted, "secret-pass")

    def test_saved_login_path_ignores_env_without_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_local = os.environ.get("LOCALAPPDATA")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "stale" / "saved_login.json")
                os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                os.environ["LOCALAPPDATA"] = str(Path(tmp) / "local")

                expected = (
                    Path(duty_credentials_module.__file__).resolve().parents[1]
                    / "local_data"
                    / "DutyAutomation"
                    / "saved_login.json"
                )
                self.assertEqual(saved_login_path(), expected)
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
                if previous_local is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = previous_local

    def test_default_saved_login_migrates_legacy_localappdata_account_on_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            package_path = Path(tmp) / "package" / "local_data" / "DutyAutomation" / "saved_login.json"
            legacy_path = Path(tmp) / "local" / "DutyAutomation" / "saved_login.json"
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_text(
                json.dumps(
                    {
                        "last_selected": "user8",
                        "accounts": [
                            {"actor_no": "8", "user_id": "user8", "password": "pass8"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_local = os.environ.get("LOCALAPPDATA")
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                os.environ["LOCALAPPDATA"] = str(Path(tmp) / "local")
                with patch.object(duty_credentials_module, "default_saved_login_path", return_value=package_path):
                    loaded = duty_credentials_module.load_synced_worker_credential()
                    result = duty_credentials_module.save_credential_sync_payload(
                        {"actor_no": "9", "user_id": "user9", "password": "pass9"}
                    )
                    migrated = duty_credentials_module.load_synced_worker_credential()
                    saved_accounts = duty_credentials_module.list_saved_duty_automation_credentials()
                    package_path_exists = package_path.exists()
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
                if previous_local is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = previous_local
                if previous_account is None:
                    os.environ.pop("DUTY_ACCOUNT", None)
                else:
                    os.environ["DUTY_ACCOUNT"] = previous_account
                if previous_password is None:
                    os.environ.pop("DUTY_PASSWORD", None)
                else:
                    os.environ["DUTY_PASSWORD"] = previous_password

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.user_id, "user8")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[0], "user8")
        self.assertEqual(result[2], package_path)
        self.assertTrue(package_path_exists)
        self.assertIsNotNone(migrated)
        assert migrated is not None
        self.assertEqual(migrated.user_id, "user8")
        self.assertEqual({item.user_id for item in saved_accounts}, {"user8", "user9"})

    def test_saved_login_path_override_expands_environment_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_root = os.environ.get("AMBULANCE_TEST_LOGIN_ROOT")
            try:
                os.environ["AMBULANCE_TEST_LOGIN_ROOT"] = tmp
                os.environ["DUTY_SAVED_LOGIN_PATH"] = r"%AMBULANCE_TEST_LOGIN_ROOT%\DutyAutomation\saved_login.json"
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"

                self.assertEqual(saved_login_path(), Path(tmp) / "DutyAutomation" / "saved_login.json")
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
                if previous_root is None:
                    os.environ.pop("AMBULANCE_TEST_LOGIN_ROOT", None)
                else:
                    os.environ["AMBULANCE_TEST_LOGIN_ROOT"] = previous_root

    def test_load_duty_credential_prefers_synced_account_over_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(path)
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
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
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
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
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(path)
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
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
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "tyfd00008")
        self.assertEqual(credential.password, "personnel-pass")

    def test_load_duty_credential_respects_preferred_order_over_last_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(path)
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_duty_automation_credentials(
                    [
                        {"actor_no": "21", "name": "張家和", "user_id": "tyfd01317", "password": "selected-pass"},
                        {"actor_no": "12", "name": "王昱勛", "user_id": "tyfd01987", "password": "driver-pass"},
                    ],
                    last_selected="tyfd01317",
                    path=path,
                )

                credential = load_duty_credential(["tyfd01987", "tyfd01317"])
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "tyfd01987")
        self.assertEqual(credential.name, "王昱勛")

    def test_load_duty_credential_can_match_driver_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(path)
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_duty_automation_credentials(
                    [
                        {"actor_no": "8", "name": "曾彥綸", "user_id": "tyfd01510", "password": "selected-pass"},
                        {"actor_no": "12", "name": "王昱勛", "user_id": "tyfd01987", "password": "driver-pass"},
                    ],
                    last_selected="tyfd01510",
                    path=path,
                )

                credential = load_duty_credential(["王昱勛"])
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "tyfd01987")
        self.assertEqual(credential.name, "王昱勛")

    def test_load_duty_credential_prefers_personnel_id_before_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(path)
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_duty_automation_credentials(
                    [
                        {
                            "actor_no": "8",
                            "user_id": "tyfd00008",
                            "password": "personnel-pass",
                            "id_number": "B123017532",
                        },
                        {"actor_no": "15", "user_id": "tyfd01510", "password": "fallback-pass"},
                    ],
                    last_selected="tyfd01510",
                    path=path,
                )

                credential = load_duty_credential(["B123017532"], fallback_user_id="tyfd01510")
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "tyfd00008")
        self.assertEqual(credential.password, "personnel-pass")

    def test_duty_credential_uses_separate_work_password_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(path)
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_duty_automation_credentials(
                    [
                        {
                            "actor_no": "8",
                            "user_id": "tyfd01510",
                            "password": "portal-pass",
                            "duty_password": "work-pass",
                        }
                    ],
                    last_selected="tyfd01510",
                    path=path,
                )

                portal_credential = load_synced_worker_credential()
                duty_credential = load_duty_credential()
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertIsNotNone(portal_credential)
        self.assertIsNotNone(duty_credential)
        assert portal_credential is not None
        assert duty_credential is not None
        self.assertEqual(portal_credential.password, "portal-pass")
        self.assertEqual(duty_credential.password, "work-pass")

    def test_set_last_selected_changes_synced_worker_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            save_duty_automation_credentials(
                [
                    {"actor_no": "8", "user_id": "user8", "password": "pass8"},
                    {"actor_no": "9", "user_id": "user9", "password": "pass9"},
                ],
                last_selected="user8",
                path=path,
            )

            set_last_selected_duty_automation_credential("user9", path=path)
            credential = load_synced_worker_credential(path)

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.user_id, "user9")


if __name__ == "__main__":
    unittest.main()
