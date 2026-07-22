import json
import os
import queue
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import worker_gui
from ambulance_bot import worker_health, worker_routes
from ambulance_bot.duty_credentials import DutyCredential, load_synced_worker_credential


class WorkerGuiEnvTests(unittest.TestCase):
    @staticmethod
    def _startup_installer_command(*args: str) -> list[str]:
        installer = Path("WinPython_公務電腦使用包/install_startup_shortcut.ps1").resolve()
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
            *args,
        ]

    def _run_startup_installer_whatif(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["WORKER_STARTUP_LAUNCHER_ENABLED"] = "true"
        return subprocess.run(
            self._startup_installer_command("-WhatIf", *args),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    @staticmethod
    def _manual_gui_stub(**overrides):
        values = {
            "server_url": SimpleNamespace(get=lambda: "http://nas/"),
            "worker_id": SimpleNamespace(get=lambda: "PC-01"),
            "log_queue": queue.Queue(),
            "_refresh_tasks": mock.Mock(),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def _supervisor_stub(**overrides):
        values = {
            "worker_thread": SimpleNamespace(is_alive=lambda: False),
            "worker_stopped_at": None,
            "worker_exit_error": "",
            "worker_restart_times": [],
            "_worker_restart_rate_limited_reported": False,
            "worker_status": mock.Mock(),
            "log_queue": queue.Queue(),
            "after": mock.Mock(),
            "_restart_worker": mock.Mock(),
            "_log": mock.Mock(),
            "_worker_supervisor_activity_active": mock.Mock(return_value=False),
            "_worker_supervisor_update_active": mock.Mock(return_value=False),
            "_schedule_worker_supervisor": mock.Mock(),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_run_worker_records_normal_return_on_gui_thread(self):
        gui = self._supervisor_stub()
        with mock.patch.object(worker_gui.worker, "main", return_value=None):
            worker_gui.WorkerGui._run_worker(gui)

        gui.worker_status.set.assert_not_called()
        callback = gui.after.call_args.args[1]
        callback()

        self.assertIsNotNone(gui.worker_stopped_at)
        self.assertEqual(gui.worker_status.set.call_args.args[0], "已停止")
        self.assertEqual(gui.worker_exit_error, "")

    def test_run_worker_records_exception_on_gui_thread(self):
        gui = self._supervisor_stub()
        with mock.patch.object(worker_gui.worker, "main", side_effect=RuntimeError("worker crashed")):
            worker_gui.WorkerGui._run_worker(gui)

        gui.worker_status.set.assert_not_called()
        gui.after.call_args.args[1]()

        self.assertEqual(gui.worker_status.set.call_args.args[0], "已停止")
        self.assertEqual(gui.worker_exit_error, "RuntimeError: worker crashed")

    def test_run_worker_tolerates_a_closed_gui_when_queueing_exit_callback(self):
        gui = self._supervisor_stub(after=mock.Mock(side_effect=RuntimeError("main thread is not in main loop")))
        with mock.patch.object(worker_gui.worker, "main", return_value=None):
            worker_gui.WorkerGui._run_worker(gui)

        gui.worker_status.set.assert_not_called()

    def test_supervisor_restarts_only_after_safe_grace(self):
        gui = self._supervisor_stub()
        gui.worker_stopped_at = time.monotonic() - 16

        worker_gui.WorkerGui._supervise_worker_thread(gui)

        gui._restart_worker.assert_called_once()
        self.assertEqual(len(gui.worker_restart_times), 1)
        gui._schedule_worker_supervisor.assert_called_once()

    def test_supervisor_does_not_restart_during_activity_or_update(self):
        for activity, update in ((True, False), (False, True)):
            with self.subTest(activity=activity, update=update):
                gui = self._supervisor_stub()
                gui.worker_stopped_at = time.monotonic() - 16
                gui._worker_supervisor_activity_active.return_value = activity
                gui._worker_supervisor_update_active.return_value = update

                worker_gui.WorkerGui._supervise_worker_thread(gui)

                gui._restart_worker.assert_not_called()
                gui._schedule_worker_supervisor.assert_called_once()

    def test_supervisor_rate_limits_fourth_restart_in_ten_minutes(self):
        now = time.monotonic()
        gui = self._supervisor_stub()
        gui.worker_stopped_at = now - 16
        gui.worker_restart_times = [now - 100, now - 50, now - 10]

        worker_gui.WorkerGui._supervise_worker_thread(gui)

        gui._restart_worker.assert_not_called()
        self.assertIn("過多", gui.log_queue.get_nowait())

    def test_supervisor_activity_guard_uses_busy_reason_and_fresh_activity(self):
        gui = self._supervisor_stub()
        with mock.patch.object(worker_gui.worker, "remote_update_busy_reason", return_value="") as busy_reason, mock.patch.object(
            worker_health,
            "activity_is_fresh",
            return_value=True,
        ) as activity_fresh:
            active = worker_gui.WorkerGui._worker_supervisor_activity_active(gui)

        self.assertTrue(active)
        busy_reason.assert_called_once()
        activity_fresh.assert_called_once_with(120.0)

    def test_supervisor_activity_guard_checks_fresh_activity_even_with_busy_reason(self):
        gui = self._supervisor_stub()
        with mock.patch.object(worker_gui.worker, "remote_update_busy_reason", return_value="manual task active") as busy_reason, mock.patch.object(
            worker_health,
            "activity_is_fresh",
            return_value=False,
        ) as activity_fresh:
            active = worker_gui.WorkerGui._worker_supervisor_activity_active(gui)

        self.assertTrue(active)
        busy_reason.assert_called_once()
        activity_fresh.assert_called_once_with(120.0)

    def test_supervisor_update_guard_uses_exact_marker_health(self):
        gui = self._supervisor_stub()
        with mock.patch.object(worker_gui.worker, "remote_update_marker_is_healthy", return_value=True) as marker_health:
            active = worker_gui.WorkerGui._worker_supervisor_update_active(gui)

        self.assertTrue(active)
        marker_health.assert_called_once_with()

    def test_gui_theme_uses_pastel_orange_white_and_deep_navy(self):
        self.assertEqual(worker_gui.GUI_THEME["bg"], "#fff7ef")
        self.assertEqual(worker_gui.GUI_THEME["surface"], "#ffffff")
        self.assertEqual(worker_gui.GUI_THEME["accent"], "#f08a4b")
        self.assertEqual(worker_gui.GUI_THEME["ink"], "#10233f")

    def test_worker_gui_status_and_card_label_backgrounds_match_root(self):
        source = Path(worker_gui.__file__).read_text(encoding="utf-8")

        self.assertIn("class WorkerGui(ctk.CTk):", source)
        self.assertIn('self.title("SinpoSmart - 救災救護Worker")', source)
        self.assertIn('text="SinpoSmart - 救災救護Worker"', source)
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
        self.assertIn("local_actions = self._action_row(local)", source)
        self.assertIn('self._button(local_actions, "修復 Chrome", self._repair_worker_chrome, "soft").grid(row=0, column=1', source)
        self.assertIn("self.credential_combo.grid(row=2, column=0", source)
        self.assertIn("credentials.rowconfigure(3, weight=1)", source)
        self.assertIn('self._hint(credentials, "同步帳號為固定8番，請勿勾選其他帳號。", wraplength=330).grid(row=3', source)
        self.assertIn("version_card.rowconfigure(2, weight=1)", source)
        self.assertNotIn('self._import_credential_sync_file, "primary").grid(row=3', source)
        self.assertNotIn("請按「匯入同步」", source)
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

    def test_worker_gui_default_geometry_matches_current_layout(self):
        source = Path(worker_gui.__file__).read_text(encoding="utf-8")

        self.assertIn('self.geometry("820x820")', source)
        self.assertIn('self.minsize(720, 720)', source)
        self.assertIn("self.after(1500, self._auto_hide_after_startup)", source)
        self.assertIn("def _auto_hide_after_startup(self) -> None:", source)

    def test_startup_launcher_can_be_disabled_by_env(self):
        previous = os.environ.get("WORKER_STARTUP_LAUNCHER_ENABLED")
        try:
            os.environ["WORKER_STARTUP_LAUNCHER_ENABLED"] = "false"

            self.assertFalse(worker_gui.startup_launcher_enabled())
        finally:
            if previous is None:
                os.environ.pop("WORKER_STARTUP_LAUNCHER_ENABLED", None)
            else:
                os.environ["WORKER_STARTUP_LAUNCHER_ENABLED"] = previous

    def test_startup_installer_and_public_package_template_define_watchdog(self):
        installer = Path("WinPython_公務電腦使用包/install_startup_shortcut.ps1").read_text(encoding="utf-8")
        builder = Path("scripts/build_public_duty_package.ps1").read_text(encoding="utf-8")
        watchdog_launcher_path = Path("WinPython_公務電腦使用包/RUN_WORKER_WATCHDOG.vbs")

        self.assertTrue(
            watchdog_launcher_path.is_file(),
            "watchdog must use a package-local VBS launcher",
        )
        watchdog_launcher = watchdog_launcher_path.read_text(encoding="ascii")

        for source in (installer, builder):
            self.assertIn('$watchdogTaskName = "AmbulanceReturnWorkerWatchdog"', source)
            self.assertIn('$watchdogLauncher = Join-Path $packageDir "RUN_WORKER_WATCHDOG.vbs"', source)
            self.assertIn(
                '$action = New-ScheduledTaskAction -Execute $wscript -Argument "`"$watchdogLauncher`""',
                source,
            )
            self.assertIn("-MultipleInstances IgnoreNew", source)
            self.assertNotIn("New-ScheduledTaskAction -Execute $watchdogPowerShell", source)

        self.assertIn("WORKER_SELF_RECOVERY.ps1", watchdog_launcher)
        self.assertIn(
            "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File",
            watchdog_launcher,
        )
        self.assertIn("shell.Run command, 0, False", watchdog_launcher)
        self.assertIn("@($taskName, $watchdogTaskName)", installer)
        self.assertIn('"RUN_WORKER_WATCHDOG.vbs"', builder)
        self.assertIn(
            'Write-PackageText -RelativePath "RUN_WORKER_WATCHDOG.vbs" -Encoding "ASCII"',
            builder,
        )

    def test_setup_script_warns_about_incomplete_startup_and_watchdog_setup(self):
        setup = Path("WinPython_公務電腦使用包/SETUP_WINPYTHON.bat").read_text(encoding="utf-8")
        builder = Path("scripts/build_public_duty_package.ps1").read_text(encoding="utf-8")
        expected_warning = "Startup/watchdog setup is incomplete. You can still start with RUN_WORKER_GUI_WINPYTHON.vbs."

        self.assertIn(expected_warning, setup)
        self.assertIn(expected_warning, builder)
        self.assertNotIn("Could not install startup scheduled task.", setup)
        self.assertNotIn("Could not install startup scheduled task.", builder)

    @unittest.skipUnless(os.name == "nt", "Windows Task Scheduler dry-run only")
    def test_startup_installer_whatif_describes_main_and_watchdog_without_installing(self):
        result = self._run_startup_installer_whatif()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Would install scheduled task: AmbulanceReturnWorker", result.stdout)
        self.assertIn("Would install watchdog task: AmbulanceReturnWorkerWatchdog", result.stdout)
        self.assertIn("User:", result.stdout)
        self.assertIn("Action:", result.stdout)
        self.assertNotIn("Installed watchdog task:", result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows Task Scheduler dry-run only")
    def test_skip_scheduled_task_keeps_watchdog_refresh_enabled(self):
        result = self._run_startup_installer_whatif("-SkipScheduledTask")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Would skip scheduled task refresh: AmbulanceReturnWorker", result.stdout)
        self.assertIn("Would install watchdog task: AmbulanceReturnWorkerWatchdog", result.stdout)
        self.assertNotIn("Would skip watchdog task refresh", result.stdout)

    def test_worker_gui_start_minimized_defaults_on_and_can_be_disabled(self):
        previous = os.environ.get("WORKER_GUI_START_MINIMIZED")
        try:
            os.environ.pop("WORKER_GUI_START_MINIMIZED", None)
            self.assertTrue(worker_gui.worker_gui_start_minimized())

            os.environ["WORKER_GUI_START_MINIMIZED"] = "false"
            self.assertFalse(worker_gui.worker_gui_start_minimized())
        finally:
            if previous is None:
                os.environ.pop("WORKER_GUI_START_MINIMIZED", None)
            else:
                os.environ["WORKER_GUI_START_MINIMIZED"] = previous

    def test_worker_chrome_profile_dirs_only_targets_worker_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = [
                root / "worker_browser_profile",
                root / "chrome_profile",
                root / "case_lookup_profile_123",
                root / "vehicle_mileage_profile_task1",
            ]
            ignored = [
                root / "Chrome User Data",
                root / "chrome_profile.chrome_repair_20260705_120000",
                root / "unrelated_profile",
            ]
            for path in expected + ignored:
                path.mkdir()

            paths = worker_gui.worker_chrome_profile_dirs(root)

        self.assertEqual(paths, sorted(expected))

    def test_backup_worker_chrome_profiles_renames_without_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "worker_browser_profile"
            existing_backup = root / "worker_browser_profile.chrome_repair_20260705_120000"
            profile.mkdir()
            (profile / "Preferences").write_text("{}", encoding="utf-8")
            existing_backup.mkdir()

            backups = worker_gui.backup_worker_chrome_profiles(root, "20260705_120000")

            self.assertFalse(profile.exists())
            self.assertEqual(len(backups), 1)
            source, backup = backups[0]
            self.assertEqual(source, profile)
            self.assertEqual(backup, root / "worker_browser_profile.chrome_repair_20260705_120000_1")
            self.assertTrue((backup / "Preferences").exists())
            self.assertTrue(existing_backup.exists())

    def test_purge_worker_chrome_profiles_deletes_generated_profiles_and_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated_profile = root / "worker_browser_profile"
            repair_backup = root / "worker_browser_profile.chrome_repair_20260705_120000"
            normal_profile = root / "Chrome User Data"
            for path in (generated_profile, repair_backup, normal_profile):
                path.mkdir()
                (path / "Preferences").write_text("{}", encoding="utf-8")

            removed = worker_gui.purge_worker_chrome_profiles(root)

            self.assertEqual({path.name for path in removed}, {"worker_browser_profile", "worker_browser_profile.chrome_repair_20260705_120000"})
            self.assertFalse(generated_profile.exists())
            self.assertFalse(repair_backup.exists())
            self.assertTrue(normal_profile.exists())

    def test_worker_chrome_repair_options_targets_worker_browser_profile_and_debugger_port(self):
        previous_profile = os.environ.get("CHROME_PROFILE_DIR")
        previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
        previous_port = os.environ.get("WORKER_CHROME_DEBUGGER_PORT")
        try:
            os.environ.pop("CHROME_PROFILE_DIR", None)
            os.environ["SELENIUM_PROFILE_ROOT"] = r"C:\Worker\profiles"
            os.environ["WORKER_CHROME_DEBUGGER_PORT"] = "9223"

            options = worker_gui.worker_chrome_repair_options()
        finally:
            if previous_profile is None:
                os.environ.pop("CHROME_PROFILE_DIR", None)
            else:
                os.environ["CHROME_PROFILE_DIR"] = previous_profile
            if previous_root is None:
                os.environ.pop("SELENIUM_PROFILE_ROOT", None)
            else:
                os.environ["SELENIUM_PROFILE_ROOT"] = previous_root
            if previous_port is None:
                os.environ.pop("WORKER_CHROME_DEBUGGER_PORT", None)
            else:
                os.environ["WORKER_CHROME_DEBUGGER_PORT"] = previous_port

        self.assertIn(r"--user-data-dir=C:\Worker\profiles\worker_browser_profile", options.arguments)
        self.assertIn("--remote-debugging-port=9223", options.arguments)

    def test_worker_chrome_repair_includes_case_lookup_profiles(self):
        source = Path(worker_gui.__file__).read_text(encoding="utf-8")

        self.assertIn("include_generated_profiles=True", source)

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

    def test_profile_cleanup_output_is_gui_readable(self):
        self.assertEqual(
            worker_gui.format_worker_output_line(
                "[profiles] cleaned stale runtime profiles: case_lookup_profile_1, chrome_profile, worker_browser_profile"
            ),
            "Chrome 清理｜已清理舊 profile 3 個",
        )

    def test_worker_gui_does_not_open_blank_chrome_window(self):
        source = Path(worker_gui.__file__).read_text(encoding="utf-8")

        self.assertNotIn('open_url_in_worker_chrome("about:blank")', source)

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

    def test_initial_worker_server_provenance_requires_explicit_builtin_marker(self):
        self.assertEqual(worker_gui.initial_worker_server_provenance("", ""), "manual")
        self.assertEqual(worker_gui.initial_worker_server_provenance(worker_gui.NAS_LAN_URL, ""), "manual")
        self.assertEqual(worker_gui.initial_worker_server_provenance(worker_gui.NAS_TAILSCALE_URL, "manual"), "manual")
        self.assertEqual(worker_gui.initial_worker_server_provenance(worker_gui.NAS_LAN_URL, "builtin"), "builtin")
        self.assertEqual(worker_gui.initial_worker_server_provenance("http://example.test:8080", "builtin"), "manual")

    def test_choose_worker_server_prefers_verified_lan_and_rejects_mismatched_lan(self):
        def matching_identity(url: str):
            return worker_routes.ServerIdentity(url, "same", "v", "nas")

        def mismatched_identity(url: str):
            instance_id = "old-lan" if url == worker_gui.NAS_LAN_URL else "live-tail"
            return worker_routes.ServerIdentity(url, instance_id, "v", "nas")

        with mock.patch.object(worker_routes, "load_known_server_identity", return_value=""):
            matching = worker_gui.choose_worker_server("", fetch_identity=matching_identity, builtin_origin=True)
            mismatched = worker_gui.choose_worker_server("", fetch_identity=mismatched_identity, builtin_origin=True)

        self.assertEqual(matching.primary_url, worker_gui.NAS_LAN_URL)
        self.assertEqual(matching.fallback_url, worker_gui.NAS_TAILSCALE_URL)
        self.assertEqual(matching.identity_status, "verified")
        self.assertEqual(matching.provenance, "builtin")
        self.assertEqual(mismatched.primary_url, worker_gui.NAS_TAILSCALE_URL)
        self.assertEqual(mismatched.fallback_url, "")
        self.assertIn("mismatch", mismatched.diagnostic)
        self.assertEqual(mismatched.provenance, "builtin")

    def test_choose_worker_server_keeps_manual_url_unverified(self):
        manual_url = "http://manual-nas:8080"
        identity = worker_routes.ServerIdentity(manual_url, "same", "v", "nas")

        with mock.patch.object(worker_routes, "load_known_server_identity", return_value="same"):
            choice = worker_gui.choose_worker_server(manual_url, fetch_identity=lambda _url: identity)

        self.assertEqual(choice.primary_url, manual_url)
        self.assertEqual(choice.fallback_url, "")
        self.assertEqual(choice.route_name, "manual")
        self.assertEqual(choice.identity_status, "unverified")
        self.assertEqual(choice.provenance, "manual")

    def test_choose_worker_server_keeps_manual_builtin_looking_url_unverified(self):
        identity = worker_routes.ServerIdentity(worker_gui.NAS_LAN_URL, "same", "v", "nas")

        choice = worker_gui.choose_worker_server(
            worker_gui.NAS_LAN_URL,
            fetch_identity=lambda _url: identity,
            builtin_origin=False,
        )

        self.assertEqual(choice.primary_url, worker_gui.NAS_LAN_URL)
        self.assertEqual(choice.fallback_url, "")
        self.assertEqual(choice.route_name, "manual")
        self.assertEqual(choice.identity_status, "unverified")
        self.assertEqual(choice.provenance, "manual")

    def test_set_server_marks_builtin_looking_manual_input_as_manual(self):
        class FakeVar:
            def __init__(self, value: str = ""):
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        gui = object.__new__(worker_gui.WorkerGui)
        gui.server_url = FakeVar(worker_gui.NAS_LAN_URL)
        gui.connection_summary = FakeVar()
        gui.connection_status = FakeVar()
        gui._server_url_provenance = "builtin"
        gui._log = lambda _message: None
        stale_values = {
            "WORKER_SERVER_URL": worker_gui.NAS_LAN_URL,
            "WORKER_SERVER_FALLBACK_URL": worker_gui.NAS_TAILSCALE_URL,
            "WORKER_SERVER_INSTANCE_ID": "stale-instance",
            "WORKER_SERVER_IDENTITY_STATUS": "verified",
            "WORKER_SERVER_ROUTE_PROVENANCE": "builtin",
            "WORKER_SERVER_ROUTE_DIAGNOSTIC": "single_route_unverified",
        }

        with mock.patch.dict(os.environ, stale_values, clear=False):
            gui._set_server(worker_gui.NAS_LAN_URL)
            choice = worker_gui.choose_worker_server(
                gui.server_url.get(),
                fetch_identity=lambda url: worker_routes.ServerIdentity(url, "same", "v", "nas"),
                builtin_origin=gui._server_url_provenance == "builtin",
            )

            self.assertEqual(os.environ["WORKER_SERVER_ROUTE_PROVENANCE"], "manual")
            self.assertEqual(os.environ["WORKER_SERVER_FALLBACK_URL"], "")
            self.assertEqual(os.environ["WORKER_SERVER_INSTANCE_ID"], "")
            self.assertEqual(os.environ["WORKER_SERVER_IDENTITY_STATUS"], "unverified")
            self.assertEqual(os.environ["WORKER_SERVER_ROUTE_DIAGNOSTIC"], "")
        self.assertEqual(choice.route_name, "manual")
        self.assertEqual(choice.identity_status, "unverified")
        self.assertEqual(choice.provenance, "manual")

    def test_direct_server_url_write_marks_builtin_looking_input_manual_through_worker(self):
        class TracedFakeVar:
            def __init__(self, value: str = ""):
                self.value = value
                self.callback = None

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value
                if self.callback is not None:
                    self.callback("server_url", "", "write")

            def trace_add(self, _mode: str, callback) -> None:
                self.callback = callback

        gui = object.__new__(worker_gui.WorkerGui)
        gui._server_url_provenance = "builtin"
        gui._server_url_write_guard = False
        gui.server_url = TracedFakeVar(worker_gui.NAS_LAN_URL)
        callback_method = getattr(worker_gui.WorkerGui, "_mark_server_url_manual", None)
        self.assertTrue(callable(callback_method))
        if not callable(callback_method):
            return
        callback = callback_method.__get__(gui, worker_gui.WorkerGui)
        gui.server_url.trace_add("write", callback)
        stale_values = {
            "WORKER_SERVER_URL": worker_gui.NAS_LAN_URL,
            "WORKER_SERVER_FALLBACK_URL": worker_gui.NAS_TAILSCALE_URL,
            "WORKER_SERVER_INSTANCE_ID": "stale-instance",
            "WORKER_SERVER_IDENTITY_STATUS": "verified",
            "WORKER_SERVER_ROUTE_PROVENANCE": "builtin",
            "WORKER_SERVER_ROUTE_DIAGNOSTIC": "single_route_unverified",
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp, **stale_values},
            clear=False,
        ):
            gui.server_url.set(worker_gui.NAS_LAN_URL)
            gui._apply_server_url()
            route = worker_gui.worker.worker_control_route_choice(gui.server_url.get())
            loop = worker_gui.worker.build_worker_control_loop(
                gui.server_url.get(),
                "PC-01",
                Path(tmp),
                worker_gui.worker.worker_control.WorkerRuntimeState(),
            )

            self.assertEqual(os.environ["WORKER_SERVER_ROUTE_PROVENANCE"], "manual")
            self.assertEqual(route.route_name, "manual")
            self.assertEqual(route.identity_status, "unverified")
            self.assertEqual(route.provenance, "manual")
            self.assertEqual(loop._client._bootstrap_url, "")
            self.assertEqual(loop._client._bootstrap_route_name, "")

    def test_apply_server_choice_preserves_builtin_provenance_through_write_trace(self):
        class TracedFakeVar:
            def __init__(self, value: str = ""):
                self.value = value
                self.callback = None

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value
                if self.callback is not None:
                    self.callback("server_url", "", "write")

            def trace_add(self, _mode: str, callback) -> None:
                self.callback = callback

        gui = object.__new__(worker_gui.WorkerGui)
        gui._server_url_provenance = "manual"
        gui._server_url_write_guard = False
        gui.server_url = TracedFakeVar("http://manual-nas:8080")
        callback_method = worker_gui.WorkerGui._mark_server_url_manual
        gui.server_url.trace_add("write", callback_method.__get__(gui, worker_gui.WorkerGui))
        gui.connection_summary = SimpleNamespace(set=lambda _value: None, get=lambda: "")
        gui.connection_status = SimpleNamespace(set=lambda _value: None, get=lambda: "")
        gui._log = lambda _message: None
        choice = worker_routes.RouteChoice(
            worker_gui.NAS_LAN_URL,
            "",
            "lan",
            "unverified",
            "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc",
            "single_route_unverified",
            "builtin",
        )

        with mock.patch.dict(os.environ, {"WORKER_SERVER_URL": "http://manual-nas:8080"}, clear=False):
            gui._apply_server_choice(choice)

            self.assertEqual(os.environ["WORKER_SERVER_ROUTE_PROVENANCE"], "builtin")
            self.assertEqual(gui._server_url_provenance, "builtin")

    def test_startup_identity_probe_does_not_persist_known_server_identity(self):
        choice = worker_routes.RouteChoice(
            worker_gui.NAS_LAN_URL,
            worker_gui.NAS_TAILSCALE_URL,
            "lan",
            "verified",
            "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc",
            "both_paths_match",
            "builtin",
        )
        gui = SimpleNamespace(after=mock.Mock(), _fetch_worker_server_identity=mock.Mock())

        with mock.patch.object(worker_gui, "choose_worker_server", return_value=choice), mock.patch.object(
            worker_routes,
            "remember_known_server_identity",
        ) as remember:
            worker_gui.WorkerGui._start_worker_with_default_server_background(
                gui,
                worker_gui.NAS_LAN_URL,
                True,
            )

        remember.assert_not_called()

    def test_connection_probe_does_not_persist_known_server_identity(self):
        choice = worker_routes.RouteChoice(
            worker_gui.NAS_LAN_URL,
            worker_gui.NAS_TAILSCALE_URL,
            "lan",
            "verified",
            "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc",
            "both_paths_match",
            "builtin",
        )
        gui = SimpleNamespace(after=mock.Mock(), _fetch_worker_server_identity=mock.Mock())

        with mock.patch.object(worker_gui, "choose_worker_server", return_value=choice), mock.patch.object(
            worker_routes,
            "remember_known_server_identity",
        ) as remember:
            worker_gui.WorkerGui._test_connection_background(
                gui,
                worker_gui.NAS_LAN_URL,
                True,
            )

        remember.assert_not_called()

    def test_apply_server_choice_replaces_all_route_environment_values(self):
        class FakeVar:
            def __init__(self, value: str = ""):
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        gui = object.__new__(worker_gui.WorkerGui)
        gui.server_url = FakeVar()
        gui.connection_summary = FakeVar()
        gui.connection_status = FakeVar()
        gui._log = lambda _message: None
        choice = worker_routes.RouteChoice(
            worker_gui.NAS_TAILSCALE_URL,
            "",
            "tailscale",
            "verified",
            "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc",
            "lan_instance_mismatch_tailscale_selected",
            "builtin",
        )
        stale_values = {
            "WORKER_SERVER_URL": worker_gui.NAS_LAN_URL,
            "WORKER_SERVER_FALLBACK_URL": worker_gui.NAS_TAILSCALE_URL,
            "WORKER_SERVER_INSTANCE_ID": "stale-instance",
            "WORKER_SERVER_IDENTITY_STATUS": "unverified",
            "WORKER_SERVER_ROUTE_PROVENANCE": "manual",
            "WORKER_SERVER_ROUTE_DIAGNOSTIC": "single_route_unverified",
        }

        with mock.patch.dict(os.environ, stale_values, clear=False):
            gui._apply_server_choice(choice)

            self.assertEqual(os.environ["WORKER_SERVER_URL"], worker_gui.NAS_TAILSCALE_URL)
            self.assertEqual(os.environ["WORKER_SERVER_FALLBACK_URL"], "")
            self.assertEqual(os.environ["WORKER_SERVER_INSTANCE_ID"], choice.instance_id)
            self.assertEqual(os.environ["WORKER_SERVER_IDENTITY_STATUS"], "verified")
            self.assertEqual(os.environ["WORKER_SERVER_ROUTE_PROVENANCE"], "builtin")
            self.assertEqual(os.environ["WORKER_SERVER_ROUTE_DIAGNOSTIC"], "")

    def test_apply_server_choice_explains_running_worker_uses_next_start(self):
        class FakeVar:
            def __init__(self, value: str = ""):
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        gui = object.__new__(worker_gui.WorkerGui)
        gui.server_url = FakeVar()
        gui.connection_summary = FakeVar()
        gui.connection_status = FakeVar()
        gui.worker_thread = SimpleNamespace(is_alive=lambda: True)
        gui._log = lambda _message: None
        choice = worker_routes.RouteChoice(
            worker_gui.NAS_TAILSCALE_URL,
            "",
            "tailscale",
            "verified",
            "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc",
            "lan_instance_mismatch_tailscale_selected",
        )

        gui._apply_server_choice(choice)

        self.assertIn("下次 Worker 啟動", gui.connection_status.get())

    def test_apply_server_choice_marks_offline_route_as_unavailable(self):
        class FakeVar:
            def __init__(self, value: str = ""):
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        gui = object.__new__(worker_gui.WorkerGui)
        gui.server_url = FakeVar()
        gui.connection_summary = FakeVar()
        gui.connection_status = FakeVar()
        gui._log = lambda _message: None
        choice = worker_routes.RouteChoice(
            worker_gui.NAS_LAN_URL,
            "",
            "offline",
            "unverified",
            "",
            "identity_unreachable",
        )

        gui._apply_server_choice(choice)

        self.assertIn("無法連線", gui.connection_status.get())
        self.assertNotIn("一般勤務可用", gui.connection_status.get())

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

    def test_local_web_status_match_rejects_stale_version(self):
        gui = object.__new__(worker_gui.WorkerGui)
        current_version = worker_gui.current_package_version()
        current_dir = str(Path(worker_gui.__file__).resolve().parent)

        self.assertTrue(gui._local_web_status_matches({"app_dir": current_dir, "version": current_version}))
        self.assertFalse(gui._local_web_status_matches({"app_dir": current_dir, "version": "1900.01.01.0000"}))

    def test_start_local_web_restarts_stale_same_package_status(self):
        class FakeVar:
            def __init__(self, value: str = ""):
                self.value = value

            def set(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

        class FakeProcess:
            stdout = None

            def poll(self):
                return None

        current_dir = str(Path(worker_gui.__file__).resolve().parent)
        gui = object.__new__(worker_gui.WorkerGui)
        gui.local_web_status = FakeVar()
        gui.local_web_url = FakeVar("http://127.0.0.1:8090/app")
        gui.local_web_process = None
        gui._local_web_status = lambda: {"app_dir": current_dir, "version": "1900.01.01.0000"}
        gui._log = lambda message: logs.append(message)
        gui._start_local_web_log_reader = lambda process: readers.append(process)
        gui.after = lambda delay, callback: after_calls.append((delay, callback))
        logs = []
        readers = []
        after_calls = []
        stop_calls = []
        popen_calls = []
        old_open_browser = os.environ.get("DESKTOP_WEB_OPEN_BROWSER")
        original_stop = getattr(worker_gui, "terminate_package_local_web_processes", None)
        original_popen = worker_gui.subprocess.Popen
        try:
            os.environ["DESKTOP_WEB_OPEN_BROWSER"] = "false"
            worker_gui.terminate_package_local_web_processes = lambda: stop_calls.append(True) or 1
            worker_gui.subprocess.Popen = lambda *args, **kwargs: popen_calls.append((args, kwargs)) or FakeProcess()

            worker_gui.WorkerGui._start_local_web_app(gui)
        finally:
            if original_stop is None:
                delattr(worker_gui, "terminate_package_local_web_processes")
            else:
                worker_gui.terminate_package_local_web_processes = original_stop
            worker_gui.subprocess.Popen = original_popen
            if old_open_browser is None:
                os.environ.pop("DESKTOP_WEB_OPEN_BROWSER", None)
            else:
                os.environ["DESKTOP_WEB_OPEN_BROWSER"] = old_open_browser

        self.assertEqual(stop_calls, [True])
        self.assertTrue(popen_calls)
        self.assertIs(gui.local_web_process, readers[0])

    def test_relaunch_worker_gui_uses_wscript_without_cmd_start(self):
        calls = []
        original_popen = worker_gui.subprocess.Popen
        try:
            worker_gui.subprocess.Popen = lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace()

            launched = worker_gui.relaunch_worker_gui(delay_seconds=1)
        finally:
            worker_gui.subprocess.Popen = original_popen

        self.assertTrue(launched)
        self.assertTrue(calls)
        self.assertNotEqual(str(calls[0][0][0]).lower(), "cmd")
        self.assertIn("RUN_WORKER_GUI_WINPYTHON.vbs", str(calls[0][0][-1]))

    def test_gui_site_helpers_follow_desktop_fast_rules(self):
        self.assertTrue(worker_gui._gui_site_is_complete("consumables_saved"))
        self.assertTrue(worker_gui._gui_site_is_complete("completed_by_user"))
        self.assertFalse(worker_gui._gui_site_is_complete("consumables_running"))
        self.assertTrue(worker_gui._gui_site_blocks_next("disinfection_failed"))
        self.assertTrue(worker_gui._gui_site_blocks_next("needs_duty_login"))
        self.assertTrue(worker_gui._gui_site_blocks_next("consumables_waiting_confirmation"))
        self.assertFalse(worker_gui._gui_site_blocks_next("vehicle_mileage_saved"))

    def test_manual_site_heartbeat_stop_error_still_ends_execution_lease(self):
        task = {"task_id": "task-site-stop-error", "created_at": "2026-07-13T08:00:00", "vehicle": "新坡91"}
        gui = self._manual_gui_stub()
        event = __import__("threading").Event()

        def fail_stop():
            raise RuntimeError("heartbeat stop failed")

        with mock.patch.object(
            worker_gui.worker,
            "begin_manual_task_execution",
            return_value=event,
        ), mock.patch.object(
            worker_gui.worker,
            "end_manual_task_execution",
        ) as end_execution, mock.patch.object(
            worker_gui.worker,
            "_start_worker_claim_heartbeat",
            return_value=fail_stop,
        ), mock.patch.object(
            worker_gui.worker,
            "claim_task",
            return_value=task,
        ), mock.patch.object(
            worker_gui.worker,
            "run_task",
            return_value=SimpleNamespace(status="duty_work_log_saved", detail="ok"),
        ):
            with self.assertRaisesRegex(RuntimeError, "heartbeat stop failed"):
                worker_gui.WorkerGui._run_selected_site_background_common(
                    gui,
                    "duty_work_log",
                    task["task_id"],
                    profile_name="duty_work_log_profile",
                    debugger_port=None,
                    use_session_lock=True,
                    tile_name="duty_work_log",
                    force_new_driver=False,
                    manage_manual_lock=True,
                    update_overall=None,
                    claimed_task=None,
                    cancellation_event=None,
                )

        end_execution.assert_called_once_with(task["task_id"], event, mock.ANY)

    def test_manual_all_sites_heartbeat_stop_error_still_ends_execution_lease(self):
        task = {"task_id": "task-all-stop-error", "created_at": "2026-07-13T08:00:00", "vehicle": "新坡91"}
        gui = self._manual_gui_stub(
            _run_selected_task_background=mock.Mock(),
            _run_selected_vehicle_mileage_background=mock.Mock(),
            _run_selected_fuel_record_background=mock.Mock(),
            _run_selected_consumables_background=mock.Mock(),
            _run_selected_disinfection_background=mock.Mock(),
        )
        event = __import__("threading").Event()

        def fail_stop():
            raise RuntimeError("heartbeat stop failed")

        with mock.patch.object(
            worker_gui.worker,
            "begin_manual_task_execution",
            return_value=event,
        ), mock.patch.object(
            worker_gui.worker,
            "end_manual_task_execution",
        ) as end_execution, mock.patch.object(
            worker_gui.worker,
            "_start_worker_claim_heartbeat",
            return_value=fail_stop,
        ), mock.patch.object(
            worker_gui.worker,
            "claim_task",
            return_value=task,
        ), mock.patch.object(
            worker_gui.worker,
            "_raise_if_task_cancelled",
            side_effect=worker_gui.worker.TaskCancellationError("cancelled"),
        ):
            with self.assertRaisesRegex(RuntimeError, "heartbeat stop failed"):
                worker_gui.WorkerGui._run_selected_all_sites_with_lease(gui, task["task_id"])

        end_execution.assert_called_once_with(task["task_id"], event, mock.ANY)

    def test_manual_all_sites_does_not_report_complete_when_child_returns_none(self):
        task = {"task_id": "task-child-none", "created_at": "2026-07-13T08:00:00", "vehicle": "新坡91"}
        site_keys = ("duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection")
        payload = {
            "task": task,
            "worker_queue": {"status": "claimed", "claim_id": "claim-none", "worker_id": "PC-01"},
            "site_statuses": {site_key: {"status": "not_started"} for site_key in site_keys},
        }
        duty_runner = mock.Mock(return_value=None)
        gui = self._manual_gui_stub(
            _run_selected_task_background=duty_runner,
            _run_selected_vehicle_mileage_background=mock.Mock(),
            _run_selected_fuel_record_background=mock.Mock(),
            _run_selected_consumables_background=mock.Mock(),
            _run_selected_disinfection_background=mock.Mock(),
        )
        event = __import__("threading").Event()
        posts: list[str] = []

        with mock.patch.object(worker_gui.worker, "begin_manual_task_execution", return_value=event), mock.patch.object(
            worker_gui.worker, "end_manual_task_execution"
        ), mock.patch.object(worker_gui.worker, "_start_worker_claim_heartbeat", return_value=lambda: None), mock.patch.object(
            worker_gui.worker, "claim_task", return_value=task
        ), mock.patch.object(worker_gui.worker, "fetch_task_payload", return_value=payload), mock.patch.object(
            worker_gui.worker,
            "post_status",
            side_effect=lambda _server, _task, status, _detail, **_kwargs: posts.append(status),
        ):
            worker_gui.WorkerGui._run_selected_all_sites_background(gui, task["task_id"])

        self.assertEqual(posts[-1], "desktop_fast_completed_with_errors")
        self.assertNotIn("desktop_fast_completed", posts)

    def test_manual_all_sites_waiting_confirmation_does_not_report_complete(self):
        task = {"task_id": "task-child-wait", "created_at": "2026-07-13T08:00:00", "vehicle": "新坡91"}
        site_keys = ("duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection")
        before = {
            "task": task,
            "worker_queue": {"status": "claimed", "claim_id": "claim-wait", "worker_id": "PC-01"},
            "site_statuses": {site_key: {"status": "not_started"} for site_key in site_keys},
        }
        after = json.loads(json.dumps(before))
        after["site_statuses"]["duty_work_log"]["status"] = "duty_work_log_waiting_confirmation"
        duty_runner = mock.Mock(return_value=SimpleNamespace(status="duty_work_log_waiting_confirmation"))
        gui = self._manual_gui_stub(
            _run_selected_task_background=duty_runner,
            _run_selected_vehicle_mileage_background=mock.Mock(),
            _run_selected_fuel_record_background=mock.Mock(),
            _run_selected_consumables_background=mock.Mock(),
            _run_selected_disinfection_background=mock.Mock(),
        )
        event = __import__("threading").Event()
        posts: list[str] = []

        with mock.patch.object(worker_gui.worker, "begin_manual_task_execution", return_value=event), mock.patch.object(
            worker_gui.worker, "end_manual_task_execution"
        ), mock.patch.object(worker_gui.worker, "_start_worker_claim_heartbeat", return_value=lambda: None), mock.patch.object(
            worker_gui.worker, "claim_task", return_value=task
        ), mock.patch.object(worker_gui.worker, "fetch_task_payload", side_effect=[before, after, after]), mock.patch.object(
            worker_gui.worker,
            "post_status",
            side_effect=lambda _server, _task, status, _detail, **_kwargs: posts.append(status),
        ):
            worker_gui.WorkerGui._run_selected_all_sites_background(gui, task["task_id"])

        self.assertEqual(posts[-1], "desktop_fast_completed_with_errors")
        self.assertNotIn("desktop_fast_completed", posts)

    def test_manual_all_sites_logs_four_site_completion_from_fetched_payload(self):
        task = {
            "task_id": "gui-complete",
            "created_at": "2026-07-17T12:00:00",
            "vehicle": "新坡92",
        }
        complete_payload = {
            "task": task,
            "worker_queue": {
                "status": "claimed",
                "claim_id": "claim-complete",
                "worker_id": "PC-01",
            },
            "site_statuses": {
                "duty_work_log": {"status": "duty_work_log_saved"},
                "vehicle_mileage": {"status": "vehicle_mileage_saved"},
                "fuel_record": {"status": "not_started"},
                "consumables": {"status": "consumables_saved"},
                "disinfection": {"status": "disinfection_saved"},
            },
        }
        gui = self._manual_gui_stub(
            _run_selected_task_background=mock.Mock(),
            _run_selected_vehicle_mileage_background=mock.Mock(),
            _run_selected_fuel_record_background=mock.Mock(),
            _run_selected_consumables_background=mock.Mock(),
            _run_selected_disinfection_background=mock.Mock(),
        )
        event = __import__("threading").Event()
        posts: list[str] = []
        with mock.patch.object(
            worker_gui.worker,
            "begin_manual_task_execution",
            return_value=event,
        ), mock.patch.object(
            worker_gui.worker,
            "end_manual_task_execution",
        ), mock.patch.object(
            worker_gui.worker,
            "_start_worker_claim_heartbeat",
            return_value=lambda: None,
        ), mock.patch.object(
            worker_gui.worker,
            "claim_task",
            return_value=task,
        ), mock.patch.object(
            worker_gui.worker,
            "fetch_task_payload",
            return_value=complete_payload,
        ), mock.patch.object(
            worker_gui.worker,
            "post_status",
            side_effect=lambda _server, _task, status, _detail, **_kwargs: posts.append(
                status
            ),
        ):
            worker_gui.WorkerGui._run_selected_all_sites_background(
                gui,
                task["task_id"],
            )

        logs: list[str] = []
        while True:
            try:
                logs.append(gui.log_queue.get_nowait())
            except queue.Empty:
                break
        self.assertEqual(posts[-1], "desktop_fast_completed")
        self.assertIn("四站｜完成｜gui-complete", logs)

    def test_manual_all_sites_does_not_log_completion_for_partial_payload(self):
        partial_payload = {
            "task": {"task_id": "gui-partial", "vehicle": "新坡92"},
            "site_statuses": {
                "duty_work_log": {"status": "duty_work_log_saved"},
                "vehicle_mileage": {"status": "vehicle_mileage_saved"},
                "fuel_record": {"status": "not_started"},
                "consumables": {"status": "consumables_failed"},
                "disinfection": {"status": "not_started"},
            },
        }

        line = worker_gui.worker.worker_completion_log_line(
            partial_payload,
            "gui-partial",
        )

        self.assertEqual(line, "")

    def test_manual_all_sites_completion_fetch_failure_does_not_overwrite_success(self):
        task = {
            "task_id": "gui-completion-fetch-offline",
            "created_at": "2026-07-17T12:00:00",
            "vehicle": "新坡92",
        }
        complete_payload = {
            "task": task,
            "worker_queue": {
                "status": "claimed",
                "claim_id": "claim-completion-fetch-offline",
                "worker_id": "PC-01",
            },
            "site_statuses": {
                "duty_work_log": {"status": "duty_work_log_saved"},
                "vehicle_mileage": {"status": "vehicle_mileage_saved"},
                "fuel_record": {"status": "not_started"},
                "consumables": {"status": "consumables_saved"},
                "disinfection": {"status": "disinfection_saved"},
            },
        }
        gui = self._manual_gui_stub(
            _run_selected_task_background=mock.Mock(),
            _run_selected_vehicle_mileage_background=mock.Mock(),
            _run_selected_fuel_record_background=mock.Mock(),
            _run_selected_consumables_background=mock.Mock(),
            _run_selected_disinfection_background=mock.Mock(),
        )
        event = __import__("threading").Event()
        posts: list[str] = []
        with mock.patch.object(
            worker_gui.worker,
            "begin_manual_task_execution",
            return_value=event,
        ), mock.patch.object(
            worker_gui.worker,
            "end_manual_task_execution",
        ), mock.patch.object(
            worker_gui.worker,
            "_start_worker_claim_heartbeat",
            return_value=lambda: None,
        ), mock.patch.object(
            worker_gui.worker,
            "claim_task",
            return_value=task,
        ), mock.patch.object(
            worker_gui.worker,
            "fetch_task_payload",
            side_effect=[
                complete_payload,
                complete_payload,
                complete_payload,
                complete_payload,
                OSError("NAS temporarily unavailable"),
            ],
        ), mock.patch.object(
            worker_gui.worker,
            "post_status",
            side_effect=lambda _server, _task, status, _detail, **_kwargs: posts.append(
                status
            ),
        ):
            worker_gui.WorkerGui._run_selected_all_sites_background(
                gui,
                task["task_id"],
            )

        self.assertEqual(posts[-1], "desktop_fast_completed")
        self.assertNotIn("desktop_fast_completed_with_errors", posts)

    def test_manual_site_backgrounds_claim_selected_task_instead_of_read_only_fetch(self):
        gui = self._manual_gui_stub()
        backgrounds = (
            worker_gui.WorkerGui._run_selected_task_background,
            worker_gui.WorkerGui._run_selected_vehicle_mileage_background,
            worker_gui.WorkerGui._run_selected_disinfection_background,
            worker_gui.WorkerGui._run_selected_fuel_record_background,
            worker_gui.WorkerGui._run_selected_consumables_background,
        )

        with mock.patch.object(worker_gui.worker, "claim_task", return_value=None, create=True) as claim_task, mock.patch.object(
            worker_gui.worker,
            "fetch_task",
            return_value=None,
        ) as fetch_task:
            for background in backgrounds:
                with self.subTest(background=background.__name__):
                    background(gui, "task-claim", manage_manual_lock=False)

        self.assertEqual(
            claim_task.call_args_list,
            [mock.call("http://nas", "task-claim", "PC-01")] * len(backgrounds),
        )
        fetch_task.assert_not_called()

    def test_manual_all_sites_claims_once_and_reuses_task_for_every_site(self):
        task = {
            "task_id": "task-all-sites",
            "created_at": "2026-07-13T08:00:00",
            "vehicle": "新坡91",
            "fuel_record": {"enabled": True},
        }
        site_keys = ("duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection")
        payload = {
            "task": task,
            "worker_queue": {"status": "claimed", "claim_id": "claim-1", "worker_id": "PC-01"},
            "site_statuses": {site_key: {"status": "not_started"} for site_key in site_keys},
        }
        runners = {site_key: mock.Mock(name=site_key) for site_key in site_keys}
        gui = self._manual_gui_stub(
            _run_selected_task_background=runners["duty_work_log"],
            _run_selected_vehicle_mileage_background=runners["vehicle_mileage"],
            _run_selected_fuel_record_background=runners["fuel_record"],
            _run_selected_consumables_background=runners["consumables"],
            _run_selected_disinfection_background=runners["disinfection"],
        )

        with mock.patch.object(worker_gui.worker, "claim_task", return_value=task, create=True) as claim_task, mock.patch.object(
            worker_gui.worker,
            "fetch_task_payload",
            return_value=payload,
        ), mock.patch.object(worker_gui.worker, "post_status"):
            worker_gui.WorkerGui._run_selected_all_sites_background(gui, task["task_id"])

        claim_task.assert_called_once_with("http://nas", task["task_id"], "PC-01")
        for site_key, runner in runners.items():
            with self.subTest(site_key=site_key):
                runner.assert_called_once()
                self.assertIs(runner.call_args.kwargs["claimed_task"], task)
                self.assertFalse(runner.call_args.kwargs["manage_manual_lock"])
                self.assertFalse(runner.call_args.kwargs["update_overall"])

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
        self.assertIn('if /I "%~1"=="--minimized" goto run_update', launcher)
        self.assertIn('start "" /min "%~f0" --minimized', launcher)
        self.assertIn("repair_update_package.ps1", launcher)
        self.assertNotRegex(launcher, r"\[OK\] Update check completed\.[\s\S]{0,80}pause")
        self.assertNotRegex(launcher.lower(), r"(?m)^\s*pause\s*$")
        self.assertIn("update_package.ps1", repair_script)
        self.assertIn("ambulance-return-public-package.zip", repair_script)
        self.assertIn("Get-LatestRelease", repair_script)

    def test_legacy_worker_launchers_delegate_to_winpython_launcher(self):
        package_dir = Path("WinPython_公務電腦使用包")
        batch_launcher = (package_dir / "run_worker_forever.bat").read_text(encoding="ascii")
        vbs_launcher = (package_dir / "run_worker_forever.vbs").read_text(encoding="ascii")

        for launcher in (batch_launcher, vbs_launcher):
            self.assertIn("RUN_WORKER_GUI_WINPYTHON", launcher)
            self.assertNotIn("pyw -3", launcher)

    def test_winpython_finder_honors_explicit_runtime_root_in_source_and_builder(self):
        finder = Path("WinPython_公務電腦使用包/find_winpython.ps1").read_text(encoding="utf-8")
        builder = Path("scripts/build_public_duty_package.ps1").read_text(encoding="utf-8")

        for source in (finder, builder):
            self.assertIn("$root -eq $env:WINPYTHON_DIR", source)

    def test_build_public_package_publishes_standalone_updater(self):
        source = Path("scripts/build_public_duty_package.ps1").read_text(encoding="utf-8")

        self.assertIn('$releaseUpdaterAsset = "update_package.ps1"', source)
        self.assertIn('Copy-Item -LiteralPath (Join-Path $packageDir "update_package.ps1")', source)
        self.assertIn("@($releaseVersionAsset, $releaseZipAsset, $releaseShaAsset, $releaseUpdaterAsset)", source)

    def test_manual_updater_stages_and_validates_before_stopping_worker(self):
        source = Path("WinPython_公務電腦使用包/UPDATE_PACKAGE.ps1").read_text(encoding="utf-8-sig")

        stop_marker = "Stop-WorkerPackageProcesses -Processes"
        self.assertIn(stop_marker, source)
        stop_call = source.index(stop_marker, source.index("Expand-Archive"))
        self.assertLess(source.index("Expand-Archive"), stop_call)
        self.assertLess(source.index("if ($packageVersion -ne $remoteVersion)"), stop_call)

    def test_manual_updater_rolls_back_and_restores_prior_running_state(self):
        source = Path("WinPython_公務電腦使用包/UPDATE_PACKAGE.ps1").read_text(encoding="utf-8-sig")

        self.assertIn("function Restore-UpdateTree", source)
        self.assertRegex(source, r"(?s)catch\s*\{.*Restore-UpdateTree")
        self.assertIn("$workerGuiWasRunning", source)
        self.assertIn("$workerHeadlessWasRunning", source)
        self.assertIn("$workerStopped", source)
        self.assertRegex(source, r"(?s)finally\s*\{.*if \(\$workerStopped.*Restart-WorkerRuntimes")

    def test_remote_update_wrapper_uses_unique_result_and_compatibility_file(self):
        source = Path("WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1").read_text(encoding="utf-8")

        self.assertIn("[guid]::NewGuid", source)
        self.assertIn('"remote_update_results"', source)
        self.assertIn('$compatibilityResultPath = Join-Path $resultDir "remote_update_result.json"', source)
        self.assertIn("$resultPath = Join-Path $uniqueResultDir", source)
        self.assertIn("$compatibilityTempPath", source)
        self.assertLess(source.index("Move-Item -LiteralPath $tempResultPath"), source.index("Move-Item -LiteralPath $compatibilityTempPath"))

    def test_remote_update_wrapper_prunes_only_expired_results(self):
        source = Path("WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1").read_text(encoding="utf-8")

        self.assertIn("function Remove-ExpiredRemoteUpdateResults", source)
        self.assertIn("AddDays(-7)", source)
        self.assertIn("$resultPath", source)
        self.assertIn("$tempResultPath", source)
        self.assertIn("LastWriteTimeUtc", source)
        self.assertIn("Get-ChildItem -LiteralPath $uniqueResultDir -File", source)
        self.assertNotRegex(source, r"Remove-Item[^\r\n]*-Recurse")

    def test_one_version_orchestrator_builds_and_verifies_both_packages(self):
        path = Path("scripts/build_all_packages.ps1")

        self.assertTrue(path.exists())
        source = path.read_text(encoding="utf-8")
        self.assertIn("[Parameter(Mandatory = $true)]", source)
        self.assertIn("build_public_duty_package.ps1", source)
        self.assertIn("build_nas_package.ps1", source)
        self.assertGreaterEqual(source.count("-Version $Version"), 2)
        self.assertIn("Read-ZipVersion", source)
        self.assertIn("Assert-VersionEquals", source)
        self.assertIn("Read-Sha256Text", source)
        self.assertIn("Assert-FileSha256", source)
        self.assertIn("Public/release zip byte hash", source)
        self.assertGreaterEqual(source.count("Get-FileHash"), 1)

    def test_one_version_orchestrator_stages_everything_before_transactional_publish(self):
        source = Path("scripts/build_all_packages.ps1").read_text(encoding="utf-8")

        self.assertIn("package-build-stage-", source)
        self.assertIn("-OutputDir $stagePublicDir", source)
        self.assertIn("-SkipSourceVersionUpdate", source)
        self.assertIn("-SourceDir $stagePublicPackageDir", source)
        self.assertIn("Publish-StagedBuild", source)
        self.assertRegex(source, r"(?s)catch\s*\{.*Restore-PublishedBuild")
        self.assertIn("$entry.Published = $false", source)
        self.assertIn("$entry.HadExisting = $false", source)
        self.assertIn("RollbackComplete", source)
        self.assertIn("Recovery files:", source)

    def test_one_version_orchestrator_defers_locked_post_publish_cleanup(self):
        source = Path("scripts/build_all_packages.ps1").read_text(encoding="utf-8")

        self.assertIn('$cleanupContext = "Package publish succeeded"', source)
        self.assertIn("but staged cleanup was deferred", source)
        self.assertRegex(
            source,
            r"(?s)finally\s*\{.*try\s*\{\s*Remove-StagedPath.*catch\s*\{.*Write-Warning",
        )

    def test_all_package_build_entrypoints_share_an_exclusive_process_lock(self):
        sources = {
            name: Path(f"scripts/{name}").read_text(encoding="utf-8")
            for name in (
                "build_all_packages.ps1",
                "build_public_duty_package.ps1",
                "build_nas_package.ps1",
            )
        }

        for name, source in sources.items():
            with self.subTest(script=name):
                self.assertIn(".package-build.lock", source)
                self.assertIn("FileShare]::None", source)
                self.assertIn("Another package build is already in progress", source)
        self.assertGreaterEqual(sources["build_all_packages.ps1"].count("-BuildLockAlreadyHeld"), 2)

    def test_public_builder_commits_versions_only_after_assets_and_writes_manifest_to_stage(self):
        source = Path("scripts/build_public_duty_package.ps1").read_text(encoding="utf-8")

        source_version_write = "$Version | Set-Content -LiteralPath $sourceVersionStagePath"
        update_version_write = "$Version | Set-Content -LiteralPath $updateVersionStagePath"
        archive = source.index("Compress-Archive")
        release_copy = source.index("Copy-Item -LiteralPath $zipPath -Destination $releaseZipPath")
        self.assertGreater(source.index(source_version_write), archive)
        self.assertGreater(source.index(source_version_write), release_copy)
        self.assertGreater(source.index(update_version_write), archive)
        self.assertGreater(source.index("Publish-StagedFiles -Mappings"), source.index(update_version_write))
        self.assertIn('Target = (Join-Path $packageDir "VERSION.txt")', source)
        self.assertIn('$Version | Set-Content -LiteralPath (Join-Path $stagePackageDir "VERSION.txt")', source)
        self.assertIn("Write-UpdateManifest -StagePackageDir $stagePackageDir", source)

    def test_public_builder_publishes_staged_assets_with_rollback(self):
        source = Path("scripts/build_public_duty_package.ps1").read_text(encoding="utf-8")

        self.assertIn("[string]$OutputDir", source)
        self.assertIn("[switch]$SkipSourceVersionUpdate", source)
        self.assertIn("$assetStageDir", source)
        self.assertIn("Publish-StagedFiles", source)
        self.assertRegex(source, r"(?s)catch\s*\{.*Restore-PublishedFiles")
        self.assertIn("$entry.Published = $false", source)
        self.assertIn("$entry.HadExisting = $false", source)
        self.assertIn("RollbackComplete", source)
        self.assertIn("Recovery files:", source)

    def test_nas_build_asserts_explicit_version_matches_source_and_output(self):
        source = Path("scripts/build_nas_package.ps1").read_text(encoding="utf-8")

        self.assertIn("[string]$Version", source)
        self.assertIn("Source VERSION.txt", source)
        self.assertIn("NAS VERSION.txt", source)
        self.assertIn("Assert-VersionEquals", source)

    def test_nas_builder_rejects_update_root_and_requires_core_files(self):
        source = Path("scripts/build_nas_package.ps1").read_text(encoding="utf-8")

        self.assertIn("[string]$SourceDir", source)
        self.assertIn("OutputDir must be a child directory of UPDATE", source)
        required_loop = source[source.index('foreach ($file in @(') : source.index('foreach ($dir in @(')]
        self.assertNotIn("if (Test-Path", required_loop)
        self.assertIn("Copy-FileToOutput -Source $source -RelativePath $file", required_loop)

    def test_nas_builder_stages_and_rolls_back_standalone_publish(self):
        source = Path("scripts/build_nas_package.ps1").read_text(encoding="utf-8")

        self.assertIn("$nasStageDir", source)
        self.assertIn("$nasRollbackDir", source)
        self.assertIn("Publish-NasStage", source)
        self.assertIn("Restore-NasOutput", source)
        self.assertRegex(source, r"(?s)catch\s*\{.*Restore-NasOutput")
        self.assertIn("$State.Published = $false", source)
        self.assertIn("$State.HadExisting = $false", source)
        self.assertIn('throw "backup is missing"', source)
        self.assertIn("RollbackComplete", source)
        self.assertIn("Recovery files:", source)

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
                    "actor_no": "9",
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
                    "actor_no": "9",
                }

                result = worker_gui.save_credential_sync_payload(payload)
                path_exists = result[2].exists() if result is not None else False
                saved_payload = json.loads(result[2].read_text(encoding="utf-8")) if result is not None else {}
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
        self.assertEqual(saved_payload["last_synced_user_id"], "user9")

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
                synced_env_account = os.environ.get("DUTY_ACCOUNT")
                synced_env_password = os.environ.get("DUTY_PASSWORD")
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
        self.assertEqual(result[0], "user8")
        self.assertEqual(synced_env_account, "user8")
        self.assertEqual(synced_env_password, "pass8")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.user_id, "user8")

    def test_save_credential_sync_payload_keeps_existing_synced_account_for_single_incoming_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                worker_gui.save_credential_sync_payload(
                    {"actor_no": "8", "user_id": "user8", "password": "pass8"}
                )

                result = worker_gui.save_credential_sync_payload(
                    {"actor_no": "9", "user_id": "user9", "password": "pass9"}
                )
                selected = load_synced_worker_credential()
                synced_env_account = os.environ.get("DUTY_ACCOUNT")
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
        self.assertEqual(result[0], "user8")
        self.assertEqual(synced_env_account, "user8")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.user_id, "user8")

    def test_save_credential_sync_payload_ignores_non_actor_8_without_existing_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"

                result = worker_gui.save_credential_sync_payload(
                    {"actor_no": "9", "user_id": "user9", "password": "pass9"}
                )
                selected = load_synced_worker_credential()
                synced_env_account = os.environ.get("DUTY_ACCOUNT")
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

        self.assertIsNone(result)
        self.assertIsNone(selected)
        self.assertNotEqual(synced_env_account, "user9")

    def test_selected_saved_credential_label_ignores_current_duty_account(self):
        label8 = worker_gui.credential_choice_label(DutyCredential(user_id="user8", password="pass8", actor_no="8"))
        label9 = worker_gui.credential_choice_label(DutyCredential(user_id="user9", password="pass9", actor_no="9"))
        saved = {
            label8: DutyCredential(user_id="user8", password="pass8", actor_no="8"),
            label9: DutyCredential(user_id="user9", password="pass9", actor_no="9"),
        }

        self.assertEqual(worker_gui.selected_saved_credential_label(saved), label8)

    def test_locked_sync_credentials_only_lists_actor_8(self):
        credentials = [
            DutyCredential(user_id="user8", password="pass8", actor_no="8"),
            DutyCredential(user_id="user9", password="pass9", actor_no="9"),
        ]

        locked = worker_gui.locked_sync_credentials(credentials)

        self.assertEqual([credential.user_id for credential in locked], ["user8"])

    def test_persist_selected_saved_credential_rejects_non_actor_8(self):
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

                with self.assertRaises(ValueError):
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
        self.assertEqual(selected.user_id, "user8")


if __name__ == "__main__":
    unittest.main()
