import os
import tempfile
import unittest
import uuid
from pathlib import Path

import worker_gui
from ambulance_bot.duty_credentials import DutyCredential


class WorkerGuiEnvTests(unittest.TestCase):
    def test_task_row_values_formats_payload(self):
        task_id, values = worker_gui.task_row_values(
            {
                "overall_status": "queued_for_worker",
                "task": {
                    "task_id": "task-1",
                    "vehicle": "新坡91",
                    "driver": "曾彥綸",
                    "case_time": "1420",
                    "return_time": "1505",
                    "case_address": "桃園市觀音區",
                },
            }
        )

        self.assertEqual(task_id, "task-1")
        self.assertEqual(values, ("新坡91", "曾彥綸", "1420/1505", "桃園市觀音區"))

    def test_initial_worker_server_prefers_lan_for_known_urls(self):
        self.assertEqual(worker_gui.initial_worker_server_url(""), worker_gui.NAS_LAN_URL)
        self.assertEqual(worker_gui.initial_worker_server_url(worker_gui.NAS_TAILSCALE_URL), worker_gui.NAS_LAN_URL)
        self.assertEqual(worker_gui.initial_worker_server_url("http://example.test:8080"), "http://example.test:8080")

    def test_choose_worker_server_falls_back_to_tailscale(self):
        selected, mode = worker_gui.choose_worker_server(lambda url: url == worker_gui.NAS_TAILSCALE_URL)

        self.assertEqual(selected, worker_gui.NAS_TAILSCALE_URL)
        self.assertEqual(mode, "tailscale")

    def test_local_web_url_uses_desktop_port(self):
        old_host = os.environ.get("DESKTOP_WEB_HOST")
        old_port = os.environ.get("DESKTOP_WEB_PORT")
        try:
            os.environ["DESKTOP_WEB_HOST"] = "127.0.0.1"
            os.environ["DESKTOP_WEB_PORT"] = "8099"

            self.assertEqual(worker_gui.local_web_url(), "http://127.0.0.1:8099/app")
        finally:
            if old_host is None:
                os.environ.pop("DESKTOP_WEB_HOST", None)
            else:
                os.environ["DESKTOP_WEB_HOST"] = old_host
            if old_port is None:
                os.environ.pop("DESKTOP_WEB_PORT", None)
            else:
                os.environ["DESKTOP_WEB_PORT"] = old_port

    def test_local_web_process_env_forces_fast_mode_auto(self):
        old_fast_mode = os.environ.get("DESKTOP_FAST_MODE")
        try:
            os.environ["DESKTOP_FAST_MODE"] = "0"

            env = worker_gui.local_web_process_env()
        finally:
            if old_fast_mode is None:
                os.environ.pop("DESKTOP_FAST_MODE", None)
            else:
                os.environ["DESKTOP_FAST_MODE"] = old_fast_mode

        self.assertEqual(env["DESKTOP_FAST_MODE"], "auto")

    @unittest.skipIf(os.name != "nt", "Windows mutex only runs on Windows")
    def test_single_instance_lock_blocks_duplicate(self):
        name = f"Local\\AmbulanceReturnBotWorkerGuiTest-{uuid.uuid4()}"

        self.assertTrue(worker_gui.acquire_single_instance_lock(name))
        try:
            self.assertFalse(worker_gui.acquire_single_instance_lock(name))
        finally:
            worker_gui.release_single_instance_lock()

        self.assertTrue(worker_gui.acquire_single_instance_lock(name))
        worker_gui.release_single_instance_lock()

    def test_find_update_launcher_prefers_package_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_launcher = root / "UPDATE_PACKAGE.bat"
            nested_dir = root / "WinPython_公務電腦使用包"
            nested_dir.mkdir()
            nested_launcher = nested_dir / "UPDATE_PACKAGE.bat"
            package_launcher.write_text("@echo off\n", encoding="utf-8")
            nested_launcher.write_text("@echo off\n", encoding="utf-8")

            self.assertEqual(worker_gui.find_update_launcher(root), package_launcher)

    def test_find_update_launcher_uses_nested_package_when_running_from_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested_dir = root / "WinPython_公務電腦使用包"
            nested_dir.mkdir()
            nested_launcher = nested_dir / "UPDATE_PACKAGE.bat"
            nested_launcher.write_text("@echo off\n", encoding="utf-8")

            self.assertEqual(worker_gui.find_update_launcher(root), nested_launcher)

    def test_credential_choice_label_uses_display_name(self):
        credential = DutyCredential(user_id="user1", password="pass", actor_no="8", display_name="8番 王小明")

        self.assertEqual(worker_gui.credential_choice_label(credential), "8番 王小明 - user1")

    def test_credential_sync_accounts_from_payload_accepts_accounts_array(self):
        payload = {
            "sync_code": "ABC",
            "accounts": [
                {
                    "actor_no": "8",
                    "user_id": "user8",
                    "password": "pass8",
                    "display_name": "8番 曾彥綸",
                    "name": "曾彥綸",
                    "id_number": "B123017532",
                },
                {"actor_no": "9", "user_id": "user9", "password": "pass9"},
            ],
            "actor_no": "8",
            "user_id": "user8",
        }

        accounts = worker_gui.credential_sync_accounts_from_payload(payload)
        selected = worker_gui.select_credential_sync_account(accounts, payload)

        self.assertEqual(len(accounts), 2)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["user_id"], "user8")
        self.assertEqual(selected["id_number"], "B123017532")

    def test_credential_sync_accounts_from_payload_keeps_legacy_single_account(self):
        payload = {
            "sync_code": "ABC",
            "actor_no": "8",
            "user_id": "legacy-user",
            "password": "legacy-pass",
        }

        accounts = worker_gui.credential_sync_accounts_from_payload(payload)

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["user_id"], "legacy-user")

    def test_save_credential_sync_payload_saves_from_imported_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                payload = {
                    "accounts": [
                        {
                            "actor_no": "8",
                            "user_id": "user8",
                            "password": "pass8",
                            "display_name": "8番 測試員",
                            "id_number": "B123017532",
                        },
                        {"actor_no": "9", "user_id": "user9", "password": "pass9"},
                    ],
                    "actor_no": "8",
                }

                result = worker_gui.save_credential_sync_payload(payload)
                path_exists = result[2].exists() if result is not None else False
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

        self.assertIsNotNone(result)
        assert result is not None
        user_id, password, path, count = result
        self.assertEqual(user_id, "user8")
        self.assertEqual(password, "pass8")
        self.assertEqual(count, 2)
        self.assertTrue(path_exists)


if __name__ == "__main__":
    unittest.main()
