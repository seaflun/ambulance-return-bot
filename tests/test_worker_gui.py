import os
import tempfile
import unittest
import uuid
from pathlib import Path

import worker_gui
from ambulance_bot.duty_credentials import DutyCredential, load_synced_worker_credential


class WorkerGuiEnvTests(unittest.TestCase):
    def test_gui_theme_uses_pastel_orange_white_and_deep_navy(self):
        self.assertEqual(worker_gui.GUI_THEME["bg"], "#fff7ef")
        self.assertEqual(worker_gui.GUI_THEME["surface"], "#ffffff")
        self.assertEqual(worker_gui.GUI_THEME["accent"], "#f08a4b")
        self.assertEqual(worker_gui.GUI_THEME["ink"], "#10233f")

    def test_worker_gui_status_and_card_label_backgrounds_match_root(self):
        source = Path(worker_gui.__file__).read_text(encoding="utf-8")

        self.assertIn("class WorkerGui(ctk.CTk):", source)
        self.assertIn('self.title("SinpoSmart - 救護Worker")', source)
        self.assertIn('text="SinpoSmart - 救護Worker"', source)
        self.assertNotIn('text="救護回程小幫手"', source)
        self.assertIn('self.configure(fg_color=theme["bg"])', source)
        self.assertIn('fg_color=theme["status_bg"]', source)
        self.assertIn("corner_radius=14", source)
        self.assertIn("ctk.CTkTextbox", source)
        self.assertIn('self._card(top_area, "本地伺服器")', source)
        self.assertIn('self._card(top_area, "NAS伺服器")', source)
        self.assertIn('uniform="main_cards"', source)
        self.assertIn('uniform="main_card_rows"', source)
        self.assertIn('nas.grid(row=0, column=1', source)
        self.assertIn('credentials.grid(row=1, column=0', source)
        self.assertIn('font=("Consolas", 11)', source)
        self.assertIn("self.connection_status", source)
        self.assertIn("self.local_web_status", source)
        self.assertIn('fg_color=GUI_THEME["accent"]', source)
        self.assertIn('button_color=theme["accent"]', source)
        self.assertIn("self.credential_combo.grid(row=1, column=1", source)
        self.assertIn("credentials.rowconfigure(2, weight=1)", source)
        self.assertIn("version_card.rowconfigure(2, weight=1)", source)
        self.assertIn('self._button(credentials, "匯入同步", self._import_credential_sync_file, "primary").grid(row=3', source)
        self.assertIn('self._button(version_card, "檢查更新", self._check_for_updates, "soft").grid(row=3', source)
        self.assertIn("columnspan=2", source)
        self.assertIn('self._hint(version_card, "", textvariable=self.package_version).grid(row=1, column=1', source)
        self.assertIn("服務狀態：可使用", source)
        self.assertIn("目前連線：內網", source)
        self.assertIn("目前連線：Tailscale", source)
        self.assertNotIn('"本機快速網頁"', source)
        self.assertNotIn('"NAS Worker"', source)
        self.assertNotIn("按一次會確認服務", source)
        self.assertNotIn("textvariable=self.connection_summary, wraplength=260", source)
        self.assertNotIn("textvariable=self.credential_sync_status", source)

    def test_worker_gui_source_has_no_question_mark_mojibake(self):
        source = Path(worker_gui.__file__).read_text(encoding="utf-8")

        self.assertNotIn("????", source)
        self.assertIn("消毒紀錄完成：", source)
        self.assertIn("消毒紀錄沒有回傳結果：", source)

    def test_worker_gui_default_geometry_is_compact(self):
        source = Path(worker_gui.__file__).read_text(encoding="utf-8")

        self.assertIn('self.geometry("680x760")', source)
        self.assertIn('self.minsize(600, 680)', source)

    def test_worker_case_lookup_output_is_gui_readable(self):
        self.assertEqual(
            worker_gui.format_worker_output_line("[worker] scheduled case lookup range=24h"),
            "案件查詢｜背景查詢｜24h",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[worker] case lookup posted count=2"),
            "案件查詢｜已送出｜2 筆",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[worker] case lookup result status=cases_loaded count=3 detail=完成"),
            "案件查詢｜完成｜已查到 3 筆",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[case_lookup] step=rows_loaded range=24h count=3"),
            "案件查詢｜已讀取案件列表｜3 筆",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[case_lookup] step=read_detail index=2/5 case_id=20260618000000001"),
            "案件查詢｜讀取單筆案件詳情｜2/5",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[case_lookup] query requested host=localhost range=24h mode=desktop_fast"),
            "案件查詢｜本機端按下查詢｜24h，desktop_fast",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[case_lookup] query requested host=localhost source=������ range=24h mode=desktop_fast"),
            "案件查詢｜本機端按下查詢｜24h，desktop_fast",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[case_lookup] query requested host=100.114.126.58:8080 source=NAS端 range=24h mode=worker_queue"),
            "案件查詢｜NAS端按下查詢｜24h，worker_queue",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[worker] manual case lookup requested range=24h source=NAS端"),
            "案件查詢｜NAS端按下查詢｜24h",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[worker] loop error: <urlopen error timed out>"),
            "連線｜NAS逾時｜等待下次重試",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line(
                "[worker] loop error: NAS worker API 拒絕連線（HTTP 403）：WORKER_TOKEN 未設定或與 NAS 不一致"
            ),
            "連線｜授權失敗｜WORKER_TOKEN 未設定或不一致，請同步 NAS 與公務電腦 .env 後重啟 worker",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[selenium] waiting for session lock: work-log"),
            "",
        )
        self.assertEqual(
            worker_gui.format_worker_output_line("[selenium] creating local chrome session attempt 1/2"),
            "",
        )

    def test_gui_log_message_uses_single_compact_format(self):
        self.assertEqual(
            worker_gui.format_gui_log_message("面板已啟動。", now="12:34:56"),
            "12:34:56｜系統｜面板已啟動",
        )
        self.assertEqual(
            worker_gui.format_gui_log_message("12:00:00 [worker] scheduled case lookup range=24h", now="12:34:56"),
            "12:34:56｜案件查詢｜背景查詢｜24h",
        )

    def test_current_package_version_prefers_root_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "WinPython_公務電腦使用包").mkdir()
            (root / "UPDATE").mkdir()
            (root / "WinPython_公務電腦使用包" / "VERSION.txt").write_text("2026.01.01.0001", encoding="utf-8")
            (root / "UPDATE" / "VERSION.txt").write_text("2026.01.01.0002", encoding="utf-8")

            self.assertEqual(worker_gui.current_package_version(root), "2026.01.01.0001")

            (root / "VERSION.txt").write_text("2026.01.01.0003", encoding="utf-8")
            self.assertEqual(worker_gui.current_package_version(root), "2026.01.01.0003")

    def test_local_web_process_output_is_piped_to_gui_log(self):
        source = Path(worker_gui.__file__).read_text(encoding="utf-8")

        self.assertIn("stdout=subprocess.PIPE", source)
        self.assertIn("stderr=subprocess.STDOUT", source)
        self.assertIn("self._start_local_web_log_reader(self.local_web_process)", source)

    def test_queue_text_writer_sends_complete_lines_to_log_queue(self):
        log_queue = worker_gui.queue.Queue()
        writer = worker_gui.QueueTextWriter(log_queue)

        writer.write("[worker] scheduled case lookup range=24h\npartial")
        writer.write(" line\n")
        writer.write("[selenium] acquired session lock: work-log\n")

        self.assertEqual(log_queue.get_nowait(), "案件查詢｜背景查詢｜24h")
        self.assertEqual(log_queue.get_nowait(), "partial line")
        self.assertTrue(log_queue.empty())

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

    def test_chrome_executable_path_prefers_configured_chrome(self):
        old_chrome_path = os.environ.get("CHROME_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                chrome = Path(tmp) / "chrome.exe"
                chrome.write_text("", encoding="utf-8")
                os.environ["CHROME_PATH"] = str(chrome)

                self.assertEqual(worker_gui.chrome_executable_path(), chrome)
        finally:
            if old_chrome_path is None:
                os.environ.pop("CHROME_PATH", None)
            else:
                os.environ["CHROME_PATH"] = old_chrome_path

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
        self.assertEqual(env["PUBLIC_PC_REPORT_ENABLED"], "true")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_gui_site_helpers_follow_desktop_fast_rules(self):
        self.assertTrue(worker_gui._gui_site_is_complete("consumables_saved"))
        self.assertTrue(worker_gui._gui_site_is_complete("completed_by_user"))
        self.assertFalse(worker_gui._gui_site_is_complete("consumables_running"))
        self.assertTrue(worker_gui._gui_site_blocks_next("disinfection_failed"))
        self.assertTrue(worker_gui._gui_site_blocks_next("needs_duty_login"))
        self.assertFalse(worker_gui._gui_site_blocks_next("vehicle_mileage_saved"))

    def test_worker_restart_enables_auto_claim_tasks(self):
        source = Path(worker_gui.__file__).read_text(encoding="utf-8")
        self.assertIn('os.environ["WORKER_AUTO_CLAIM_TASKS"] = "true"', source)

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

    def test_update_launcher_self_repairs_parse_broken_updater(self):
        package_dir = Path("WinPython_公務電腦使用包")
        launcher = (package_dir / "UPDATE_PACKAGE.bat").read_text(encoding="ascii")
        repair_script = (package_dir / "repair_update_package.ps1").read_text(encoding="utf-8")

        self.assertIn("[System.Management.Automation.Language.Parser]::ParseFile", launcher)
        self.assertIn("repair_update_package.ps1", launcher)
        self.assertIn("update_package.ps1", repair_script)
        self.assertIn("ambulance-return-public-package.zip", repair_script)
        self.assertIn("Get-LatestRelease", repair_script)

    def test_build_public_package_publishes_standalone_updater(self):
        source = Path("scripts/build_public_duty_package.ps1").read_text(encoding="utf-8")

        self.assertIn('$releaseUpdaterAsset = "update_package.ps1"', source)
        self.assertIn('Copy-Item -LiteralPath (Join-Path $packageDir "update_package.ps1")', source)
        self.assertIn("@($releaseVersionAsset, $releaseZipAsset, $releaseShaAsset, $releaseUpdaterAsset)", source)

    def test_credential_choice_label_uses_display_name(self):
        credential = DutyCredential(user_id="user1", password="pass", actor_no="8", display_name="8番 王小明")

        self.assertEqual(worker_gui.credential_choice_label(credential), "8番 王小明 - user1")

    def test_credential_choice_label_does_not_repeat_account_as_name(self):
        credential = DutyCredential(user_id="tyfd01510", password="pass", actor_no="8", display_name="8番 tyfd01510")

        self.assertEqual(worker_gui.credential_choice_label(credential), "8番 未填姓名 - tyfd01510")

    def test_credential_choice_label_marks_missing_display_parts(self):
        credential = DutyCredential(user_id="user1", password="pass")

        self.assertEqual(worker_gui.credential_choice_label(credential), "未填番號 未填姓名 - user1")

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

    def test_save_credential_sync_payload_keeps_synced_account_on_actor_8(self):
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
                        {"actor_no": "8", "user_id": "user8", "password": "pass8"},
                        {"actor_no": "9", "user_id": "user9", "password": "pass9"},
                    ],
                    "actor_no": "9",
                }

                result = worker_gui.save_credential_sync_payload(payload)
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

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[0], "user9")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.user_id, "user8")

    def test_persist_selected_saved_credential_updates_last_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                worker_gui.save_credential_sync_payload(
                    {
                        "accounts": [
                            {"actor_no": "8", "user_id": "user8", "password": "pass8"},
                            {"actor_no": "9", "user_id": "user9", "password": "pass9"},
                        ],
                        "actor_no": "8",
                    }
                )

                worker_gui.persist_selected_saved_credential(DutyCredential(user_id="user9", password="pass9", actor_no="9"))
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

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.user_id, "user9")


if __name__ == "__main__":
    unittest.main()
