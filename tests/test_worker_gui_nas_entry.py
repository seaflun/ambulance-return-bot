import os
import queue
import unittest
from types import SimpleNamespace
from unittest import mock

import worker_gui
from ambulance_bot import worker_routes


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class WorkerGuiNasEntryTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"TASK_ENTRY_URL": "", "DESKTOP_WEB_OPEN_BROWSER": "false"})
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def gui_stub():
        gui = SimpleNamespace(
            local_web_url=FakeVar("http://127.0.0.1:8090/task-entry"),
            local_web_status=FakeVar(),
            local_web_process=None,
            log_queue=queue.Queue(),
            _log=mock.Mock(),
            _local_web_status=mock.Mock(return_value=None),
            _local_web_status_matches=mock.Mock(return_value=False),
            _local_web_status_same_package=mock.Mock(return_value=False),
            _open_local_web_app=mock.Mock(),
            _restart_worker=mock.Mock(),
            _start_local_web_log_reader=mock.Mock(),
            _terminate_local_web_process_for_repair=mock.Mock(return_value=False),
        )
        gui.after = lambda delay, callback: callback()
        gui._start_local_web_app = lambda **kwargs: worker_gui.WorkerGui._start_local_web_app(gui, **kwargs)
        gui._log_local_status_for_repair = lambda: worker_gui.WorkerGui._log_local_status_for_repair(gui)
        gui._finish_startup = lambda choice, settings: worker_gui.WorkerGui._finish_startup(gui, choice, settings)
        return gui

    @staticmethod
    def verified_choice():
        return worker_routes.RouteChoice(
            "http://nas:8080", "", "lan", "verified",
            "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc", "both_paths_match", "builtin",
        )

    def settings_response(self, state="configured", url="https://entry.example.test/"):
        return {
            "ok": True,
            "instance_id": self.verified_choice().instance_id,
            "task_entry": {"state": state, "url": url, "detail": "NAS 登打設定"},
        }

    def test_url_configuration_accepts_complete_http_and_https_urls(self):
        for value in ("http://nas:8080/task-entry", "https://entry.example.test/", " https://entry.example.test/task-entry "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TASK_ENTRY_URL": value}):
                self.assertEqual(worker_gui.task_entry_url(), value.strip())

    def test_url_configuration_rejects_unsafe_or_incomplete_urls(self):
        for value in ("javascript:alert(1)", "file:///tmp/task-entry", "//nas/task-entry", "https://", "https://nas:bad/", "http://user:secret@nas/", "http://nas/a b", "http://nas\\evil/"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TASK_ENTRY_URL": value}):
                with self.assertRaisesRegex(ValueError, "TASK_ENTRY_URL"):
                    worker_gui.task_entry_url()

    def test_empty_configuration_retains_local_entry_url(self):
        with mock.patch.dict(os.environ, {"DESKTOP_WEB_HOST": "127.0.0.1", "DESKTOP_WEB_PORT": "8099"}):
            self.assertEqual(worker_gui.task_entry_url(), "")
            self.assertEqual(worker_gui.local_web_url(), "http://127.0.0.1:8099/task-entry")

    def test_nas_startup_displays_url_without_local_probe_or_process(self):
        gui = self.gui_stub()
        with mock.patch.dict(os.environ, {"TASK_ENTRY_URL": "https://entry.example.test/"}), mock.patch.object(worker_gui.subprocess, "Popen") as popen:
            gui._start_local_web_app()
        self.assertEqual(gui.local_web_url.get(), "https://entry.example.test/")
        self.assertIn("NAS", gui.local_web_status.get())
        gui._local_web_status.assert_not_called()
        gui._open_local_web_app.assert_not_called()
        self.assertEqual(popen.call_count, 0)

    def test_nas_startup_can_open_browser_without_starting_flask(self):
        gui = self.gui_stub()
        with mock.patch.dict(os.environ, {"TASK_ENTRY_URL": "http://nas/task-entry", "DESKTOP_WEB_OPEN_BROWSER": "true"}), mock.patch.object(worker_gui.subprocess, "Popen") as popen:
            gui._start_local_web_app()
        gui._open_local_web_app.assert_called_once()
        self.assertEqual(popen.call_count, 0)

    def test_nas_button_opens_even_when_automatic_browser_open_is_disabled(self):
        gui = self.gui_stub()
        with mock.patch.dict(os.environ, {"TASK_ENTRY_URL": "https://entry.example.test/"}), mock.patch.object(worker_gui.subprocess, "Popen") as popen:
            gui._start_local_web_app(open_browser=True)
        gui._open_local_web_app.assert_called_once()
        self.assertEqual(popen.call_count, 0)

    def test_invalid_setting_reports_error_without_local_fallback(self):
        gui = self.gui_stub()
        with mock.patch.dict(os.environ, {"TASK_ENTRY_URL": "javascript:alert(1)"}), mock.patch.object(worker_gui.subprocess, "Popen") as popen:
            gui._start_local_web_app()
        self.assertIn("TASK_ENTRY_URL", gui.local_web_status.get())
        self.assertIn("錯誤", gui.local_web_status.get())
        gui._local_web_status.assert_not_called()
        gui._open_local_web_app.assert_not_called()
        self.assertEqual(popen.call_count, 0)

    def test_nas_browser_open_uses_configured_url_instead_of_edited_field(self):
        gui = self.gui_stub()
        gui.local_web_url.set("javascript:alert(1)")
        with mock.patch.dict(os.environ, {"TASK_ENTRY_URL": "https://entry.example.test/"}), mock.patch.object(worker_gui, "chrome_executable_path", return_value=None), mock.patch.object(worker_gui.webbrowser, "open_new_tab") as browser:
            worker_gui.WorkerGui._open_local_web_app(gui)
        browser.assert_called_once_with("https://entry.example.test/")

    def test_invalid_setting_cannot_be_opened_directly(self):
        gui = self.gui_stub()
        with mock.patch.dict(os.environ, {"TASK_ENTRY_URL": "file:///tmp/task-entry"}), mock.patch.object(worker_gui.subprocess, "Popen") as popen, mock.patch.object(worker_gui.webbrowser, "open_new_tab") as browser:
            worker_gui.WorkerGui._open_local_web_app(gui)
        self.assertIn("TASK_ENTRY_URL", gui.local_web_status.get())
        self.assertEqual(popen.call_count, 0)
        browser.assert_not_called()

    def test_legacy_in_process_flask_start_is_blocked_for_any_nas_setting(self):
        for value in ("https://entry.example.test/", "javascript:alert(1)"):
            with self.subTest(value=value):
                gui = self.gui_stub()
                with mock.patch.dict(os.environ, {"TASK_ENTRY_URL": value}), mock.patch("app.run_web_app") as run_web_app:
                    worker_gui.WorkerGui._run_local_web_app(gui)
                run_web_app.assert_not_called()

    def test_chrome_repair_restarts_worker_without_local_server_or_false_8090_warning(self):
        for value in ("https://entry.example.test/", "javascript:alert(1)"):
            with self.subTest(value=value):
                gui = self.gui_stub()
                with mock.patch.dict(os.environ, {"TASK_ENTRY_URL": value}), mock.patch.object(worker_gui, "cleanup_worker_chrome_residue", return_value=0), mock.patch.object(worker_gui, "purge_worker_chrome_profiles", return_value=[]), mock.patch.object(worker_gui, "relaunch_worker_gui", return_value=False), mock.patch.object(worker_gui.subprocess, "Popen") as popen:
                    worker_gui.WorkerGui._repair_worker_chrome_background(gui)
                gui._restart_worker.assert_called_once()
                gui._local_web_status.assert_not_called()
                self.assertEqual(popen.call_count, 0)
                logs = "\n".join(gui.log_queue.queue)
                self.assertIn("NAS", logs)
                self.assertNotIn("8090", logs)

    def test_local_mode_still_starts_local_flask_subprocess(self):
        gui = self.gui_stub()
        process = SimpleNamespace(poll=lambda: None)
        with mock.patch.object(worker_gui.subprocess, "Popen", return_value=process) as popen:
            gui._start_local_web_app()
        self.assertIs(gui.local_web_process, process)
        self.assertTrue(popen.call_args.args[0][-1].endswith("app.py"))

    def test_invalid_entry_setting_does_not_stop_worker_startup(self):
        gui = self.gui_stub()
        start_entry = worker_gui.WorkerGui._start_local_web_app
        gui._start_local_web_app = lambda: start_entry(gui)
        gui._start_worker_with_default_server = lambda: (gui._restart_worker(), gui._start_local_web_app())
        gui.mainloop = mock.Mock()
        with mock.patch.dict(os.environ, {"TASK_ENTRY_URL": "javascript:alert(1)"}), mock.patch.object(worker_gui, "acquire_single_instance_lock", return_value=True), mock.patch.object(worker_gui, "release_single_instance_lock"), mock.patch.object(worker_gui, "WorkerGui", return_value=gui), mock.patch.object(worker_gui.subprocess, "Popen") as popen:
            worker_gui.main()
        self.assertIn("設定錯誤", gui.local_web_status.get())
        gui._restart_worker.assert_called_once()
        gui.mainloop.assert_called_once()
        self.assertEqual(popen.call_count, 0)

    def test_discovery_reads_only_the_verified_nas_endpoint(self):
        choice = self.verified_choice()
        with mock.patch.object(worker_gui.worker, "request_json", return_value=self.settings_response()) as request:
            settings = worker_gui.discover_task_entry_settings(choice)
        self.assertEqual(settings["state"], "configured")
        self.assertEqual(settings["url"], "https://entry.example.test/")
        request.assert_called_once_with("http://nas:8080/worker/task-entry-settings")

    def test_unverified_nas_settings_are_not_requested(self):
        choice = self.verified_choice()
        for identity_status, instance_id in (("unverified", choice.instance_id), ("verified", ""), ("verified", "not-a-uuid")):
            unverified = SimpleNamespace(primary_url=choice.primary_url, identity_status=identity_status, instance_id=instance_id)
            with self.subTest(identity_status=identity_status, instance_id=instance_id), mock.patch.object(worker_gui.worker, "request_json") as request:
                settings = worker_gui.discover_task_entry_settings(unverified)
            self.assertEqual(settings["state"], "disabled")
            request.assert_not_called()

    def test_discovered_url_is_used_by_manual_open_and_chrome_repair(self):
        gui = self.gui_stub()
        gui._discovered_task_entry = self.settings_response()["task_entry"]
        with mock.patch.object(worker_gui, "cleanup_worker_chrome_residue", return_value=0), mock.patch.object(worker_gui, "purge_worker_chrome_profiles", return_value=[]), mock.patch.object(worker_gui, "relaunch_worker_gui", return_value=False), mock.patch.object(worker_gui.subprocess, "Popen") as popen:
            gui._start_local_web_app(open_browser=True)
            worker_gui.WorkerGui._repair_worker_chrome_background(gui)
        self.assertEqual(gui.local_web_url.get(), "https://entry.example.test/")
        gui._open_local_web_app.assert_called_once()
        gui._restart_worker.assert_called_once()
        gui._local_web_status.assert_not_called()
        self.assertEqual(popen.call_count, 0)
        self.assertNotIn("8090", "\n".join(gui.log_queue.queue))

    def test_discovered_browser_open_uses_verified_url(self):
        gui = self.gui_stub()
        gui._discovered_task_entry = self.settings_response()["task_entry"]
        gui.local_web_url.set("javascript:alert(1)")
        with mock.patch.object(worker_gui, "chrome_executable_path", return_value=None), mock.patch.object(worker_gui.webbrowser, "open_new_tab") as browser:
            worker_gui.WorkerGui._open_local_web_app(gui)
        browser.assert_called_once_with("https://entry.example.test/")

    def test_disabled_or_unavailable_discovery_keeps_local_fallback(self):
        for response, error in ((self.settings_response("disabled", ""), None), (None, RuntimeError("HTTP 404: Not Found")), (None, TimeoutError("timed out"))):
            with self.subTest(error=error):
                with mock.patch.object(worker_gui.worker, "request_json", return_value=response, side_effect=error):
                    settings = worker_gui.discover_task_entry_settings(self.verified_choice())
                self.assertEqual(settings["state"], "disabled")
                gui = self.gui_stub()
                gui._discovered_task_entry = settings
                with mock.patch.object(worker_gui.subprocess, "Popen") as popen:
                    gui._start_local_web_app()
                self.assertEqual(popen.call_count, 1)

    def test_invalid_discovery_blocks_local_fallback(self):
        invalid_responses = [
            self.settings_response("invalid", ""),
            self.settings_response("configured", "javascript:alert(1)"),
            self.settings_response("configured", ""),
            self.settings_response("unknown", ""),
            {"ok": True, "instance_id": self.verified_choice().instance_id, "task_entry": []},
        ]
        for response in invalid_responses:
            with self.subTest(response=response), mock.patch.object(worker_gui.worker, "request_json", return_value=response):
                settings = worker_gui.discover_task_entry_settings(self.verified_choice())
                self.assertEqual(settings["state"], "invalid")
                gui = self.gui_stub()
                gui._discovered_task_entry = settings
                with mock.patch.object(worker_gui.subprocess, "Popen") as popen:
                    gui._start_local_web_app()
                self.assertIn("錯誤", gui.local_web_status.get())
                self.assertEqual(popen.call_count, 0)

    def test_mismatched_instance_rejects_url_but_still_starts_worker(self):
        response = self.settings_response()
        response["instance_id"] = "8f07200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        with mock.patch.object(worker_gui.worker, "request_json", return_value=response):
            settings = worker_gui.discover_task_entry_settings(self.verified_choice())
        self.assertEqual(settings["state"], "invalid")
        self.assertIn("身分", settings["detail"])
        self.assertEqual(settings["url"], "")
        gui = self.gui_stub()
        gui._apply_server_choice = mock.Mock()
        with mock.patch.object(worker_gui.subprocess, "Popen") as popen:
            gui._finish_startup(self.verified_choice(), settings)
        gui._apply_server_choice.assert_called_once_with(self.verified_choice(), start_worker=True)
        self.assertIn("錯誤", gui.local_web_status.get())
        self.assertEqual(popen.call_count, 0)

    def test_local_override_skips_discovery_and_takes_precedence(self):
        for url in ("https://manual-entry.example.test/", "javascript:alert(1)"):
            with self.subTest(url=url), mock.patch.dict(os.environ, {"TASK_ENTRY_URL": url}), mock.patch.object(worker_gui.worker, "request_json") as request:
                worker_gui.discover_task_entry_settings(self.verified_choice())
                gui = self.gui_stub()
                gui._discovered_task_entry = {"state": "invalid", "url": "", "detail": "NAS 設定錯誤"}
                with mock.patch.object(worker_gui.subprocess, "Popen") as popen:
                    gui._start_local_web_app()
                request.assert_not_called()
                self.assertEqual(popen.call_count, 0)
                if url.startswith("https:"):
                    self.assertEqual(gui.local_web_url.get(), url)
                else:
                    self.assertIn("錯誤", gui.local_web_status.get())

    def test_startup_waits_for_discovery_before_starting_entry(self):
        gui = self.gui_stub()
        gui._task_entry_discovery_pending = True
        gui._fetch_worker_server_identity = mock.Mock()
        callbacks = []
        events = []
        gui.after = lambda delay, callback: callbacks.append(callback)
        gui._apply_server_choice = lambda choice, start_worker: events.append("worker")

        def choose(*args, **kwargs):
            events.append("choice")
            return self.verified_choice()

        def request(url):
            events.append("discovery")
            return self.settings_response()

        with mock.patch.object(worker_gui, "choose_worker_server", side_effect=choose), mock.patch.object(worker_gui.worker, "request_json", side_effect=request), mock.patch.object(worker_gui.subprocess, "Popen") as popen:
            worker_gui.WorkerGui._start_worker_with_default_server_background(gui, "http://nas:8080", True)
            self.assertEqual(events, ["choice", "discovery"])
            self.assertEqual(popen.call_count, 0)
            callbacks[0]()
        self.assertEqual(events, ["choice", "discovery", "worker"])
        self.assertEqual(gui.local_web_url.get(), "https://entry.example.test/")
        self.assertFalse(gui._task_entry_discovery_pending)
        self.assertEqual(popen.call_count, 0)

    def test_metadata_failure_still_starts_worker_and_local_fallback(self):
        for error in (RuntimeError("HTTP 404: Not Found"), TimeoutError("timed out")):
            with self.subTest(error=error):
                gui = self.gui_stub()
                gui._task_entry_discovery_pending = True
                gui._fetch_worker_server_identity = mock.Mock()
                gui._apply_server_choice = mock.Mock()
                with mock.patch.object(worker_gui, "choose_worker_server", return_value=self.verified_choice()), mock.patch.object(worker_gui.worker, "request_json", side_effect=error), mock.patch.object(worker_gui.subprocess, "Popen") as popen:
                    worker_gui.WorkerGui._start_worker_with_default_server_background(gui, "http://nas:8080", True)
                gui._apply_server_choice.assert_called_once_with(self.verified_choice(), start_worker=True)
                self.assertFalse(gui._task_entry_discovery_pending)
                self.assertEqual(popen.call_count, 1)

    def test_entry_button_during_discovery_does_not_start_local_web(self):
        gui = self.gui_stub()
        gui._task_entry_discovery_pending = True
        with mock.patch.object(worker_gui.subprocess, "Popen") as popen:
            gui._start_local_web_app(open_browser=True)
        self.assertIn("確認", gui.local_web_status.get())
        self.assertEqual(popen.call_count, 0)

    def test_main_schedules_worker_discovery_without_early_local_web(self):
        gui = SimpleNamespace(_start_worker_with_default_server=mock.Mock(), _start_local_web_app=mock.Mock(), after=mock.Mock(), mainloop=mock.Mock())
        with mock.patch.object(worker_gui, "acquire_single_instance_lock", return_value=True), mock.patch.object(worker_gui, "release_single_instance_lock"), mock.patch.object(worker_gui, "WorkerGui", return_value=gui):
            worker_gui.main()
        self.assertEqual(gui.after.call_count, 1)
        self.assertIs(gui.after.call_args.args[1], gui._start_worker_with_default_server)
        gui._start_local_web_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
