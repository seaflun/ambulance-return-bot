import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import worker as worker_module
from ambulance_bot.manual_task_lock import manual_task_lock_active, manual_task_lock_path, set_manual_task_lock
from ambulance_bot.selenium_local import DutyCaseLookupResult


class WorkerTests(unittest.TestCase):
    def test_hash_cases_is_stable_for_same_content(self):
        left = [{"case_id": "1", "address": "A"}, {"case_id": "2", "address": "B"}]
        right = [{"address": "A", "case_id": "1"}, {"address": "B", "case_id": "2"}]

        self.assertEqual(worker_module.hash_cases(left), worker_module.hash_cases(right))

    def test_manual_task_lock_defaults_to_ten_minute_expiry(self):
        original_max_age = os.environ.get("MANUAL_TASK_LOCK_MAX_AGE_SECONDS")
        try:
            os.environ.pop("MANUAL_TASK_LOCK_MAX_AGE_SECONDS", None)
            with tempfile.TemporaryDirectory() as tmp:
                artifacts_dir = Path(tmp)
                set_manual_task_lock(artifacts_dir, "test")
                lock_path = manual_task_lock_path(artifacts_dir)
                old_time = time.time() - 601
                os.utime(lock_path, (old_time, old_time))

                self.assertFalse(manual_task_lock_active(artifacts_dir))
                self.assertFalse(lock_path.exists())
        finally:
            if original_max_age is None:
                os.environ.pop("MANUAL_TASK_LOCK_MAX_AGE_SECONDS", None)
            else:
                os.environ["MANUAL_TASK_LOCK_MAX_AGE_SECONDS"] = original_max_age

    def test_remote_update_waits_for_windows_idle_without_launching(self):
        launches: list[str] = []
        statuses: list[tuple[str, str, str, str]] = []
        command = {"request_id": "update-1", "status": "pending"}

        with tempfile.TemporaryDirectory() as tmp:
            started = worker_module.maybe_run_remote_update(
                "http://nas",
                "PC-01",
                Path(tmp),
                fetch_command=lambda *_: command,
                post_command_status=lambda *args, **_kwargs: statuses.append(args),
                idle_seconds=lambda: 30.0,
                launch_update=lambda request_id: launches.append(request_id),
            )

        self.assertFalse(started)
        self.assertEqual(launches, [])
        self.assertEqual(statuses[-1][2], "waiting_idle")
        self.assertIn("120 秒", statuses[-1][3])

    def test_remote_update_waits_for_cross_process_task_lock(self):
        launches: list[str] = []
        statuses: list[tuple[str, str, str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            set_manual_task_lock(artifacts_dir, "active-task")

            started = worker_module.maybe_run_remote_update(
                "http://nas",
                "PC-01",
                artifacts_dir,
                fetch_command=lambda *_: {"request_id": "update-locked", "status": "pending"},
                post_command_status=lambda *args, **_kwargs: statuses.append(args),
                idle_seconds=lambda: 999.0,
                launch_update=lambda request_id: launches.append(request_id),
            )

        self.assertFalse(started)
        self.assertEqual(launches, [])
        self.assertEqual(statuses[-1][2], "waiting_busy")
        self.assertIn("勤務登打", statuses[-1][3])

    def test_remote_update_waits_for_in_process_manual_task(self):
        launches: list[str] = []
        statuses: list[tuple[str, str, str, str]] = []
        worker_module.MANUAL_TASK_ACTIVE.set()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                started = worker_module.maybe_run_remote_update(
                    "http://nas",
                    "PC-01",
                    Path(tmp),
                    fetch_command=lambda *_: {"request_id": "update-manual", "status": "pending"},
                    post_command_status=lambda *args, **_kwargs: statuses.append(args),
                    idle_seconds=lambda: 999.0,
                    launch_update=lambda request_id: launches.append(request_id),
                )
        finally:
            worker_module.MANUAL_TASK_ACTIVE.clear()

        self.assertFalse(started)
        self.assertEqual(launches, [])
        self.assertEqual(statuses[-1][2], "waiting_busy")

    def test_remote_update_waits_for_active_case_lookup(self):
        launches: list[str] = []
        statuses: list[tuple[str, str, str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            request_path = artifacts_dir / "cases" / "request.json"
            request_path.parent.mkdir(parents=True, exist_ok=True)
            request_path.write_text(json.dumps({"status": "case_lookup_requested"}), encoding="utf-8")

            started = worker_module.maybe_run_remote_update(
                "http://nas",
                "PC-01",
                artifacts_dir,
                fetch_command=lambda *_: {"request_id": "update-lookup", "status": "pending"},
                post_command_status=lambda *args, **_kwargs: statuses.append(args),
                idle_seconds=lambda: 999.0,
                launch_update=lambda request_id: launches.append(request_id),
            )

        self.assertFalse(started)
        self.assertEqual(launches, [])
        self.assertEqual(statuses[-1][2], "waiting_busy")
        self.assertIn("案件查詢", statuses[-1][3])

    def test_remote_update_does_not_relaunch_updating_command(self):
        launches: list[str] = []
        statuses: list[tuple[str, str, str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            started = worker_module.maybe_run_remote_update(
                "http://nas",
                "PC-01",
                Path(tmp),
                fetch_command=lambda *_: {"request_id": "update-running", "status": "updating"},
                post_command_status=lambda *args, **_kwargs: statuses.append(args),
                idle_seconds=lambda: 999.0,
                launch_update=lambda request_id: launches.append(request_id),
            )

        self.assertFalse(started)
        self.assertEqual(launches, [])
        self.assertEqual(statuses, [])

    def test_remote_update_reports_failed_when_hidden_launcher_cannot_start(self):
        statuses: list[tuple[str, str, str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            started = worker_module.maybe_run_remote_update(
                "http://nas",
                "PC-01",
                Path(tmp),
                fetch_command=lambda *_: {"request_id": "update-launch-fail", "status": "pending"},
                post_command_status=lambda *args, **_kwargs: statuses.append(args),
                idle_seconds=lambda: 999.0,
                launch_update=lambda _request_id: (_ for _ in ()).throw(OSError("cannot launch")),
            )

        self.assertFalse(started)
        self.assertEqual([item[2] for item in statuses], ["updating", "failed"])
        self.assertIn("cannot launch", statuses[-1][3])

    def test_windows_user_idle_seconds_uses_tick_difference(self):
        idle = worker_module.windows_user_idle_seconds(
            last_input_tick=lambda: 30_000,
            current_tick=lambda: 150_000,
        )

        self.assertEqual(idle, 120.0)

    def test_fetch_remote_update_command_sends_worker_identity_and_version(self):
        captured_urls: list[str] = []
        original_request_json = worker_module.request_json
        original_package_version = worker_module.current_package_version
        try:
            worker_module.request_json = lambda url: captured_urls.append(url) or {
                "ok": True,
                "command": {"request_id": "update-fetch", "status": "pending"},
            }
            worker_module.current_package_version = lambda: "2026.07.10.1950"

            command = worker_module.fetch_remote_update_command("http://nas", "PC 01")
        finally:
            worker_module.request_json = original_request_json
            worker_module.current_package_version = original_package_version

        query = worker_module.urllib.parse.parse_qs(worker_module.urllib.parse.urlparse(captured_urls[0]).query)
        self.assertEqual(command["request_id"], "update-fetch")
        self.assertEqual(query["worker_id"], ["PC 01"])
        self.assertEqual(query["package_version"], ["2026.07.10.1950"])

    def test_fetch_remote_update_command_tolerates_old_nas_without_endpoint(self):
        original_request_json = worker_module.request_json
        try:
            worker_module.request_json = lambda _url: (_ for _ in ()).throw(
                RuntimeError("NAS worker API 回應 HTTP 404：NOT FOUND")
            )

            command = worker_module.fetch_remote_update_command("http://old-nas", "PC-01")
        finally:
            worker_module.request_json = original_request_json

        self.assertIsNone(command)

    def test_post_remote_update_status_sends_complete_json(self):
        captured: dict[str, object] = {}
        original_urlopen = worker_module.urllib.request.urlopen

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        try:
            worker_module.urllib.request.urlopen = fake_urlopen
            worker_module.post_remote_update_status(
                "http://nas",
                "update-status",
                "completed",
                "遠端更新完成。",
                worker_id="PC-01",
                before_version="2026.07.10.1950",
                installed_version="2026.07.11.1548",
                exit_code=0,
            )
        finally:
            worker_module.urllib.request.urlopen = original_urlopen

        self.assertTrue(str(captured["url"]).endswith("/worker/remote-update/update-status/status"))
        self.assertEqual(
            captured["payload"],
            {
                "status": "completed",
                "detail": "遠端更新完成。",
                "worker_id": "PC-01",
                "before_version": "2026.07.10.1950",
                "installed_version": "2026.07.11.1548",
                "exit_code": 0,
            },
        )

    def test_launch_remote_update_uses_hidden_powershell_wrapper(self):
        calls: list[tuple[list[str], dict[str, object]]] = []
        with tempfile.TemporaryDirectory() as tmp:
            package_dir = Path(tmp)
            wrapper = package_dir / "REMOTE_UPDATE_PACKAGE.ps1"
            wrapper.write_text("param([string]$RequestId)", encoding="utf-8")

            worker_module.launch_remote_update(
                "update-hidden",
                package_dir=package_dir,
                popen=lambda args, **kwargs: calls.append((args, kwargs)),
            )

        args, kwargs = calls[0]
        self.assertIn("-WindowStyle", args)
        self.assertIn("Hidden", args)
        self.assertIn(str(wrapper), args)
        self.assertIn("update-hidden", args)
        self.assertEqual(kwargs["cwd"], package_dir)
        self.assertEqual(
            int(kwargs["creationflags"]) & int(getattr(worker_module.subprocess, "CREATE_NO_WINDOW", 0)),
            int(getattr(worker_module.subprocess, "CREATE_NO_WINDOW", 0)),
        )

    def test_report_remote_update_result_posts_once_and_marks_reported(self):
        posts: list[tuple[str, str, str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                path = worker_module.remote_update_result_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "request_id": "update-result",
                            "status": "completed",
                            "detail": "遠端更新完成。",
                            "before_version": "2026.07.10.1950",
                            "installed_version": "2026.07.11.1548",
                            "exit_code": 0,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                first = worker_module.report_remote_update_result(
                    "http://nas",
                    "PC-01",
                    post_command_status=lambda *args, **_kwargs: posts.append(args),
                    reported_at=lambda: "2026-07-11T15:55:00",
                )
                second = worker_module.report_remote_update_result(
                    "http://nas",
                    "PC-01",
                    post_command_status=lambda *args, **_kwargs: posts.append(args),
                    reported_at=lambda: "2026-07-11T15:56:00",
                )
                saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "update-result")
        self.assertEqual(posts[0][2], "completed")
        self.assertEqual(saved["reported_at"], "2026-07-11T15:55:00")

    def test_remote_update_wrapper_runs_updater_hidden_and_records_result(self):
        wrapper = Path(worker_module.__file__).with_name("REMOTE_UPDATE_PACKAGE.ps1")

        self.assertTrue(wrapper.exists())
        source = wrapper.read_text(encoding="utf-8")
        self.assertIn("Start-Process", source)
        self.assertIn("-WindowStyle Hidden", source)
        self.assertIn("update_package.ps1", source)
        self.assertIn("remote_update_result.json", source)
        self.assertIn('"up_to_date"', source)
        self.assertIn('"completed"', source)
        self.assertIn('"failed"', source)
        self.assertIn("Move-Item", source)
        self.assertTrue(source.isascii())

    def test_remote_update_idle_setting_is_documented_for_public_package(self):
        env_example = Path(worker_module.__file__).with_name(".env.example").read_text(encoding="utf-8")

        self.assertIn("REMOTE_UPDATE_IDLE_SECONDS=120", env_example)

    def test_main_reports_result_and_checks_remote_update_before_other_work(self):
        env_keys = ["WORKER_RUN_ONCE", "WORKER_AUTO_CLAIM_TASKS"]
        previous_env = {key: worker_module.os.environ.get(key) for key in env_keys}
        original_report = worker_module.report_remote_update_result
        original_remote = worker_module.maybe_run_remote_update
        original_sync = worker_module.maybe_run_credential_sync
        original_lookup = worker_module.maybe_run_case_lookup
        calls: list[str] = []
        try:
            worker_module.os.environ["WORKER_RUN_ONCE"] = "true"
            worker_module.os.environ["WORKER_AUTO_CLAIM_TASKS"] = "false"
            worker_module.report_remote_update_result = lambda *_args, **_kwargs: calls.append("result") or False
            worker_module.maybe_run_remote_update = lambda *_args, **_kwargs: calls.append("remote") or True
            worker_module.maybe_run_credential_sync = lambda *_args, **_kwargs: calls.append("credential")
            worker_module.maybe_run_case_lookup = (
                lambda _server_url, _artifacts_dir, last_lookup_at, last_case_hash, _interval_seconds:
                calls.append("lookup") or (last_lookup_at, last_case_hash)
            )

            worker_module.main()
        finally:
            worker_module.report_remote_update_result = original_report
            worker_module.maybe_run_remote_update = original_remote
            worker_module.maybe_run_credential_sync = original_sync
            worker_module.maybe_run_case_lookup = original_lookup
            for key, value in previous_env.items():
                if value is None:
                    worker_module.os.environ.pop(key, None)
                else:
                    worker_module.os.environ[key] = value

        self.assertEqual(calls, ["result", "remote"])

    def test_main_defaults_scheduled_case_lookup_to_thirty_minutes(self):
        env_keys = ["CASE_LOOKUP_INTERVAL_SECONDS", "WORKER_RUN_ONCE", "WORKER_AUTO_CLAIM_TASKS"]
        previous_env = {key: worker_module.os.environ.get(key) for key in env_keys}
        original_report = worker_module.report_remote_update_result
        original_remote = worker_module.maybe_run_remote_update
        original_sync = worker_module.maybe_run_credential_sync
        original_lookup = worker_module.maybe_run_case_lookup
        intervals: list[int] = []
        try:
            worker_module.os.environ.pop("CASE_LOOKUP_INTERVAL_SECONDS", None)
            worker_module.os.environ["WORKER_RUN_ONCE"] = "true"
            worker_module.os.environ["WORKER_AUTO_CLAIM_TASKS"] = "false"
            worker_module.report_remote_update_result = lambda *_args, **_kwargs: False
            worker_module.maybe_run_remote_update = lambda *_args, **_kwargs: False
            worker_module.maybe_run_credential_sync = lambda server_url: None
            worker_module.maybe_run_case_lookup = (
                lambda server_url, artifacts_dir, last_lookup_at, last_case_hash, interval_seconds:
                intervals.append(interval_seconds) or (last_lookup_at, last_case_hash)
            )

            worker_module.main()
        finally:
            worker_module.report_remote_update_result = original_report
            worker_module.maybe_run_remote_update = original_remote
            worker_module.maybe_run_credential_sync = original_sync
            worker_module.maybe_run_case_lookup = original_lookup
            for key, value in previous_env.items():
                if value is None:
                    worker_module.os.environ.pop(key, None)
                else:
                    worker_module.os.environ[key] = value

        self.assertEqual(intervals, [1800])

    def test_main_clamps_short_scheduled_case_lookup_interval_to_thirty_minutes(self):
        env_keys = ["CASE_LOOKUP_INTERVAL_SECONDS", "WORKER_RUN_ONCE", "WORKER_AUTO_CLAIM_TASKS"]
        previous_env = {key: worker_module.os.environ.get(key) for key in env_keys}
        original_report = worker_module.report_remote_update_result
        original_remote = worker_module.maybe_run_remote_update
        original_sync = worker_module.maybe_run_credential_sync
        original_lookup = worker_module.maybe_run_case_lookup
        intervals: list[int] = []
        try:
            worker_module.os.environ["CASE_LOOKUP_INTERVAL_SECONDS"] = "300"
            worker_module.os.environ["WORKER_RUN_ONCE"] = "true"
            worker_module.os.environ["WORKER_AUTO_CLAIM_TASKS"] = "false"
            worker_module.report_remote_update_result = lambda *_args, **_kwargs: False
            worker_module.maybe_run_remote_update = lambda *_args, **_kwargs: False
            worker_module.maybe_run_credential_sync = lambda server_url: None
            worker_module.maybe_run_case_lookup = (
                lambda server_url, artifacts_dir, last_lookup_at, last_case_hash, interval_seconds:
                intervals.append(interval_seconds) or (last_lookup_at, last_case_hash)
            )

            worker_module.main()
        finally:
            worker_module.report_remote_update_result = original_report
            worker_module.maybe_run_remote_update = original_remote
            worker_module.maybe_run_credential_sync = original_sync
            worker_module.maybe_run_case_lookup = original_lookup
            for key, value in previous_env.items():
                if value is None:
                    worker_module.os.environ.pop(key, None)
                else:
                    worker_module.os.environ[key] = value

        self.assertEqual(intervals, [1800])

    def test_post_status_adds_site_failure_diagnostics(self):
        original_urlopen = worker_module.urllib.request.urlopen
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        try:
            worker_module.urllib.request.urlopen = fake_urlopen
            worker_module.post_status(
                "http://nas",
                "task-1",
                "consumables_failed",
                "SSO login failed",
                site_key="consumables",
                site_name="一站通耗材",
            )
        finally:
            worker_module.urllib.request.urlopen = original_urlopen

        payload = captured["payload"]
        self.assertEqual(payload["failure_stage"], "登入一站通")
        self.assertIn("登入", payload["failure_reason"])
        self.assertIn("驗證碼", payload["next_action"])

    def test_scheduled_lookup_skips_unchanged_cases(self):
        calls = {"posts": 0}
        original_fetch = worker_module.fetch_case_lookup_request
        original_query = worker_module.query_duty_emergency_cases
        original_post = worker_module.post_cases
        try:
            cases = [{"case_id": "1"}]
            case_hash = worker_module.hash_cases(cases)
            worker_module.fetch_case_lookup_request = lambda server_url: None
            worker_module.query_duty_emergency_cases = lambda artifacts_dir, lookup_range="24h": DutyCaseLookupResult(
                True,
                "cases_loaded",
                "loaded",
                cases,
                artifacts_dir / "cases" / "latest.json",
            )
            worker_module.post_cases = lambda *args, **kwargs: calls.__setitem__("posts", calls["posts"] + 1)

            with tempfile.TemporaryDirectory() as tmp:
                last_lookup_at, last_case_hash = worker_module.maybe_run_case_lookup(
                    "http://nas",
                    Path(tmp),
                    0,
                    case_hash,
                    300,
                )
        finally:
            worker_module.fetch_case_lookup_request = original_fetch
            worker_module.query_duty_emergency_cases = original_query
            worker_module.post_cases = original_post

        self.assertGreater(last_lookup_at, 0)
        self.assertEqual(last_case_hash, case_hash)
        self.assertEqual(calls["posts"], 0)

    def test_manual_lookup_posts_even_when_cases_unchanged(self):
        calls = {"posts": 0, "lookup_range": ""}
        original_fetch = worker_module.fetch_case_lookup_request
        original_query = worker_module.query_duty_emergency_cases
        original_post = worker_module.post_cases
        try:
            cases = [{"case_id": "1"}]
            case_hash = worker_module.hash_cases(cases)
            worker_module.fetch_case_lookup_request = lambda server_url: {"lookup_range": "legacy-range"}
            def fake_query(artifacts_dir, lookup_range="24h"):
                calls["lookup_range"] = lookup_range
                return DutyCaseLookupResult(
                    True,
                    "cases_loaded",
                    "loaded",
                    cases,
                    artifacts_dir / "cases" / "latest.json",
                )
            worker_module.query_duty_emergency_cases = fake_query
            worker_module.post_cases = lambda *args, **kwargs: calls.__setitem__("posts", calls["posts"] + 1)

            with tempfile.TemporaryDirectory() as tmp:
                _, last_case_hash = worker_module.maybe_run_case_lookup(
                    "http://nas",
                    Path(tmp),
                    0,
                    case_hash,
                    300,
                )
        finally:
            worker_module.fetch_case_lookup_request = original_fetch
            worker_module.query_duty_emergency_cases = original_query
            worker_module.post_cases = original_post

        self.assertEqual(last_case_hash, case_hash)
        self.assertEqual(calls["posts"], 1)
        self.assertEqual(calls["lookup_range"], "24h")

    def test_maybe_run_credential_sync_saves_payload_and_acks(self):
        payload = {
            "accounts": [
                {"actor_no": "8", "user_id": "user8", "password": "secret-pass"},
            ]
        }
        saved_payloads: list[dict] = []
        acks: list[tuple[str, str, str, str]] = []
        original_fetch = worker_module.fetch_credential_sync_request
        original_save = worker_module.save_credential_sync_payload
        original_ack = worker_module.ack_credential_sync_request
        try:
            worker_module.fetch_credential_sync_request = lambda server_url: {
                "request_id": "sync-test-1",
                "payload": payload,
            }

            def fake_save(sync_payload):
                saved_payloads.append(sync_payload)
                return "user8", "secret-pass", Path("saved_login.json"), 1

            worker_module.save_credential_sync_payload = fake_save
            worker_module.ack_credential_sync_request = (
                lambda server_url, request_id, status, detail: acks.append((server_url, request_id, status, detail))
            )

            worker_module.maybe_run_credential_sync("http://nas")
        finally:
            worker_module.fetch_credential_sync_request = original_fetch
            worker_module.save_credential_sync_payload = original_save
            worker_module.ack_credential_sync_request = original_ack

        self.assertEqual(saved_payloads, [payload])
        self.assertEqual(acks[0][0], "http://nas")
        self.assertEqual(acks[0][1], "sync-test-1")
        self.assertEqual(acks[0][2], "saved")
        self.assertIn("user8", acks[0][3])
        self.assertNotIn("secret-pass", acks[0][3])

    def test_scheduled_lookup_skips_when_previous_lookup_waits_for_login(self):
        original_fetch = worker_module.fetch_case_lookup_request
        original_query = worker_module.query_duty_emergency_cases
        try:
            worker_module.fetch_case_lookup_request = lambda server_url: None
            worker_module.query_duty_emergency_cases = lambda artifacts_dir, lookup_range="24h": self.fail(
                "scheduled lookup should skip while waiting for login"
            )
            with tempfile.TemporaryDirectory() as tmp:
                cases_dir = Path(tmp) / "cases"
                cases_dir.mkdir()
                (cases_dir / "latest.json").write_text(
                    json.dumps({"status": "duty_login_failed", "cases": []}),
                    encoding="utf-8",
                )
                last_lookup_at, last_case_hash = worker_module.maybe_run_case_lookup(
                    "http://nas",
                    Path(tmp),
                    0,
                    "",
                    300,
                )
        finally:
            worker_module.fetch_case_lookup_request = original_fetch
            worker_module.query_duty_emergency_cases = original_query

        self.assertGreater(last_lookup_at, 0)
        self.assertEqual(last_case_hash, "")

    def test_case_lookup_skips_when_cross_process_manual_lock_is_active(self):
        original_fetch = worker_module.fetch_case_lookup_request
        original_query = worker_module.query_duty_emergency_cases
        try:
            worker_module.fetch_case_lookup_request = lambda server_url: None
            worker_module.query_duty_emergency_cases = lambda artifacts_dir, lookup_range="24h": self.fail(
                "case lookup should skip while manual task lock is active"
            )
            with tempfile.TemporaryDirectory() as tmp:
                artifacts_dir = Path(tmp)
                set_manual_task_lock(artifacts_dir, "test")
                last_lookup_at, last_case_hash = worker_module.maybe_run_case_lookup(
                    "http://nas",
                    artifacts_dir,
                    0,
                    "",
                    300,
                )
        finally:
            worker_module.fetch_case_lookup_request = original_fetch
            worker_module.query_duty_emergency_cases = original_query

        self.assertEqual(last_lookup_at, 0)
        self.assertEqual(last_case_hash, "")

    def test_worker_api_403_message_points_to_worker_token(self):
        error = SimpleNamespace(code=403, reason="Forbidden")

        message = worker_module.worker_api_error_message(error)

        self.assertIn("HTTP 403", message)
        self.assertIn("WORKER_TOKEN", message)
        self.assertIn("同步 NAS 與公務電腦 .env", message)

    def test_single_site_workers_use_site_profile_defaults(self):
        original_run_duty = worker_module.run_local_selenium_task
        original_run_vehicle = worker_module.run_vehicle_mileage_task
        original_run_fuel = worker_module.run_fuel_record_task
        original_run_disinfection = worker_module.run_disinfection_task
        original_post_status = worker_module.post_status
        profile_names: dict[str, str] = {}

        def record_site(site_key: str, status: str, detail: str):
            def _run(*args, **kwargs):
                profile_names[site_key] = kwargs["profile_name"]
                return SimpleNamespace(ok=True, status=status, detail=detail)

            return _run

        try:
            worker_module.run_local_selenium_task = record_site("duty_work_log", "duty_work_log_saved", "duty ok")
            worker_module.run_vehicle_mileage_task = record_site("vehicle_mileage", "vehicle_mileage_saved", "mileage ok")
            worker_module.run_fuel_record_task = record_site("fuel_record", "fuel_record_saved", "fuel ok")
            worker_module.run_disinfection_task = record_site("disinfection", "disinfection_saved", "disinfection ok")
            worker_module.post_status = lambda *args, **kwargs: None

            base_task = {"task_id": "task-default-profile", "created_at": "2026-06-09T00:00:00"}
            fuel_task = {
                **base_task,
                "task_id": "task-default-profile-fuel",
                "fuel_record": {"enabled": True, "date": "20260627", "time": "1250", "quantity": "20.5", "unit_price": "30.1"},
            }

            worker_module.run_task("http://nas", "worker-a", base_task, Path("artifacts"))
            worker_module.run_vehicle_task("http://nas", "worker-a", base_task, Path("artifacts"))
            worker_module.run_fuel_worker_task("http://nas", "worker-a", fuel_task, Path("artifacts"))
            worker_module.run_disinfection_worker_task("http://nas", "worker-a", base_task, Path("artifacts"))
        finally:
            worker_module.run_local_selenium_task = original_run_duty
            worker_module.run_vehicle_mileage_task = original_run_vehicle
            worker_module.run_fuel_record_task = original_run_fuel
            worker_module.run_disinfection_task = original_run_disinfection
            worker_module.post_status = original_post_status

        self.assertEqual(
            profile_names,
            {
                "duty_work_log": "duty_work_log_profile",
                "vehicle_mileage": "vehicle_mileage_profile",
                "fuel_record": "fuel_record_profile",
                "disinfection": "disinfection_profile",
            },
        )

    def test_run_task_posts_site_failure_when_selenium_raises(self):
        original_run = worker_module.run_local_selenium_task
        original_post_status = worker_module.post_status
        statuses: list[tuple[str, str, str]] = []
        try:
            worker_module.run_local_selenium_task = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("chrome crashed"))
            worker_module.post_status = lambda server_url, task_id, status, detail, **kwargs: statuses.append(
                (status, detail, kwargs.get("site_key", ""))
            )

            result = worker_module.run_task(
                "http://nas",
                "worker-a",
                {"task_id": "task-work-fail", "created_at": "2026-06-09T00:00:00"},
                Path("artifacts"),
            )
        finally:
            worker_module.run_local_selenium_task = original_run
            worker_module.post_status = original_post_status

        self.assertEqual(result.status, "duty_work_log_failed")
        self.assertIn("工作紀錄操作失敗：chrome crashed", result.detail)
        self.assertIn(("duty_work_log_failed", result.detail, "duty_work_log"), statuses)
        self.assertEqual(statuses[-1][0], "desktop_fast_completed_with_errors")

    def test_run_vehicle_posts_site_failure_when_selenium_raises(self):
        original_run = worker_module.run_vehicle_mileage_task
        original_post_status = worker_module.post_status
        statuses: list[tuple[str, str, str]] = []
        try:
            worker_module.run_vehicle_mileage_task = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("button missing"))
            worker_module.post_status = lambda server_url, task_id, status, detail, **kwargs: statuses.append(
                (status, detail, kwargs.get("site_key", ""))
            )

            result = worker_module.run_vehicle_task(
                "http://nas",
                "worker-a",
                {"task_id": "task-mileage-fail", "created_at": "2026-06-09T00:00:00"},
                Path("artifacts"),
            )
        finally:
            worker_module.run_vehicle_mileage_task = original_run
            worker_module.post_status = original_post_status

        self.assertEqual(result.status, "vehicle_mileage_failed")
        self.assertIn("車輛里程操作失敗：button missing", result.detail)
        self.assertIn(("vehicle_mileage_failed", result.detail, "vehicle_mileage"), statuses)
        self.assertEqual(statuses[-1][0], "desktop_fast_completed_with_errors")

    def test_run_disinfection_posts_site_failure_when_selenium_raises(self):
        original_run = worker_module.run_disinfection_task
        original_post_status = worker_module.post_status
        statuses: list[tuple[str, str, str]] = []
        try:
            worker_module.run_disinfection_task = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("query failed"))
            worker_module.post_status = lambda server_url, task_id, status, detail, **kwargs: statuses.append(
                (status, detail, kwargs.get("site_key", ""))
            )

            result = worker_module.run_disinfection_worker_task(
                "http://nas",
                "worker-a",
                {"task_id": "task-disinfection-fail", "created_at": "2026-06-09T00:00:00"},
                Path("artifacts"),
            )
        finally:
            worker_module.run_disinfection_task = original_run
            worker_module.post_status = original_post_status

        self.assertEqual(result.status, "disinfection_failed")
        self.assertIn("消毒紀錄操作失敗：query failed", result.detail)
        self.assertIn(("disinfection_failed", result.detail, "disinfection"), statuses)
        self.assertEqual(statuses[-1][0], "desktop_fast_completed_with_errors")

    def test_auto_claim_run_all_sites_runs_four_sites_and_sets_final_status(self):
        original_fetch_payload = worker_module.fetch_task_payload
        original_run_task = worker_module.run_task
        original_run_vehicle = worker_module.run_vehicle_task
        original_run_fuel = worker_module.run_fuel_worker_task
        original_run_disinfection = worker_module.run_disinfection_worker_task
        original_run_consumables = worker_module.run_consumables_worker_task
        original_post_status = worker_module.post_status
        calls: list[str] = []
        statuses: list[tuple[str, str, str]] = []
        try:
            worker_module.fetch_task_payload = lambda server_url, task_id: {"site_statuses": {}}
            worker_module.run_task = lambda *args, **kwargs: calls.append("duty_work_log") or SimpleNamespace(
                ok=True, status="duty_work_log_saved", detail="duty ok"
            )
            worker_module.run_vehicle_task = lambda *args, **kwargs: calls.append("vehicle_mileage") or SimpleNamespace(
                ok=True, status="vehicle_mileage_saved", detail="mileage ok"
            )
            worker_module.run_fuel_worker_task = lambda *args, **kwargs: calls.append("fuel_record") or SimpleNamespace(
                ok=True, status="fuel_record_saved", detail="fuel ok"
            )
            worker_module.run_disinfection_worker_task = lambda *args, **kwargs: calls.append("disinfection") or SimpleNamespace(
                ok=True, status="disinfection_saved", detail="disinfection ok"
            )
            worker_module.run_consumables_worker_task = lambda *args, **kwargs: calls.append("consumables") or SimpleNamespace(
                ok=True, status="consumables_saved", detail="consumables ok"
            )
            worker_module.post_status = lambda server_url, task_id, status, detail, **kwargs: statuses.append(
                (status, detail, kwargs.get("site_key", ""))
            )

            result = worker_module.run_all_sites_task(
                "http://nas",
                "worker-a",
                {"task_id": "task-1", "created_at": "2026-06-09T00:00:00"},
                Path("artifacts"),
            )
        finally:
            worker_module.fetch_task_payload = original_fetch_payload
            worker_module.run_task = original_run_task
            worker_module.run_vehicle_task = original_run_vehicle
            worker_module.run_fuel_worker_task = original_run_fuel
            worker_module.run_disinfection_worker_task = original_run_disinfection
            worker_module.run_consumables_worker_task = original_run_consumables
            worker_module.post_status = original_post_status

        self.assertCountEqual(calls, ["duty_work_log", "vehicle_mileage", "consumables", "disinfection"])
        self.assertIn(result.status, {"duty_work_log_saved", "vehicle_mileage_saved", "consumables_saved", "disinfection_saved"})
        self.assertEqual(statuses[-1][0], "desktop_fast_completed")
        self.assertEqual(statuses[-1][2], "")

    def test_auto_claim_run_all_sites_runs_fuel_when_enabled(self):
        original_fetch_payload = worker_module.fetch_task_payload
        original_run_task = worker_module.run_task
        original_run_vehicle = worker_module.run_vehicle_task
        original_run_fuel = worker_module.run_fuel_worker_task
        original_run_disinfection = worker_module.run_disinfection_worker_task
        original_run_consumables = worker_module.run_consumables_worker_task
        original_post_status = worker_module.post_status
        calls: list[str] = []
        try:
            task = {
                "task_id": "task-fuel",
                "created_at": "2026-06-09T00:00:00",
                "fuel_record": {"enabled": True, "date": "20260627", "time": "1250", "quantity": "20.5", "unit_price": "30.1"},
            }
            worker_module.fetch_task_payload = lambda server_url, task_id: {"task": task, "site_statuses": {}}
            worker_module.run_task = lambda *args, **kwargs: calls.append("duty_work_log") or SimpleNamespace(
                ok=True, status="duty_work_log_saved", detail="duty ok"
            )
            worker_module.run_vehicle_task = lambda *args, **kwargs: calls.append("vehicle_mileage") or SimpleNamespace(
                ok=True, status="vehicle_mileage_saved", detail="mileage ok"
            )
            worker_module.run_fuel_worker_task = lambda *args, **kwargs: calls.append("fuel_record") or SimpleNamespace(
                ok=True, status="fuel_record_saved", detail="fuel ok"
            )
            worker_module.run_consumables_worker_task = lambda *args, **kwargs: calls.append("consumables") or SimpleNamespace(
                ok=True, status="consumables_saved", detail="consumables ok"
            )
            worker_module.run_disinfection_worker_task = lambda *args, **kwargs: calls.append("disinfection") or SimpleNamespace(
                ok=True, status="disinfection_saved", detail="disinfection ok"
            )
            worker_module.post_status = lambda *args, **kwargs: None

            worker_module.run_all_sites_task("http://nas", "worker-a", task, Path("artifacts"))
        finally:
            worker_module.fetch_task_payload = original_fetch_payload
            worker_module.run_task = original_run_task
            worker_module.run_vehicle_task = original_run_vehicle
            worker_module.run_fuel_worker_task = original_run_fuel
            worker_module.run_disinfection_worker_task = original_run_disinfection
            worker_module.run_consumables_worker_task = original_run_consumables
            worker_module.post_status = original_post_status

        self.assertCountEqual(calls, ["duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"])

    def test_auto_claim_run_all_sites_limits_parallel_groups_to_two_and_keeps_mileage_fuel_sequential(self):
        original_fetch_payload = worker_module.fetch_task_payload
        original_run_task = worker_module.run_task
        original_run_vehicle = worker_module.run_vehicle_task
        original_run_fuel = worker_module.run_fuel_worker_task
        original_run_disinfection = worker_module.run_disinfection_worker_task
        original_run_consumables = worker_module.run_consumables_worker_task
        original_post_status = worker_module.post_status
        task = {
            "task_id": "task-parallel",
            "created_at": "2026-06-09T00:00:00",
            "fuel_record": {"enabled": True, "date": "20260627", "time": "1250", "quantity": "20.5", "unit_price": "30.1"},
        }
        active = 0
        peak = 0
        intervals: dict[str, dict[str, float]] = {}
        lock = threading.Lock()

        def run_site(site_key: str):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                intervals.setdefault(site_key, {})["start"] = time.perf_counter()
            time.sleep(0.05)
            with lock:
                intervals[site_key]["end"] = time.perf_counter()
                active -= 1
            return SimpleNamespace(ok=True, status=f"{site_key}_saved", detail=f"{site_key} ok")

        try:
            worker_module.fetch_task_payload = lambda server_url, task_id: {"task": task, "site_statuses": {}}
            worker_module.run_task = lambda *args, **kwargs: run_site("duty_work_log")
            worker_module.run_vehicle_task = lambda *args, **kwargs: run_site("vehicle_mileage")
            worker_module.run_fuel_worker_task = lambda *args, **kwargs: run_site("fuel_record")
            worker_module.run_consumables_worker_task = lambda *args, **kwargs: run_site("consumables")
            worker_module.run_disinfection_worker_task = lambda *args, **kwargs: run_site("disinfection")
            worker_module.post_status = lambda *args, **kwargs: None

            result = worker_module.run_all_sites_task("http://nas", "worker-a", task, Path("artifacts"))
        finally:
            worker_module.fetch_task_payload = original_fetch_payload
            worker_module.run_task = original_run_task
            worker_module.run_vehicle_task = original_run_vehicle
            worker_module.run_fuel_worker_task = original_run_fuel
            worker_module.run_disinfection_worker_task = original_run_disinfection
            worker_module.run_consumables_worker_task = original_run_consumables
            worker_module.post_status = original_post_status

        self.assertEqual(peak, 2)
        self.assertLessEqual(intervals["vehicle_mileage"]["end"], intervals["fuel_record"]["start"])
        self.assertIn(result.status, {"duty_work_log_saved", "fuel_record_saved", "consumables_saved", "disinfection_saved"})

    def test_auto_claim_run_all_sites_continues_after_site_failure(self):
        original_fetch_payload = worker_module.fetch_task_payload
        original_run_task = worker_module.run_task
        original_run_vehicle = worker_module.run_vehicle_task
        original_run_fuel = worker_module.run_fuel_worker_task
        original_run_disinfection = worker_module.run_disinfection_worker_task
        original_run_consumables = worker_module.run_consumables_worker_task
        original_post_status = worker_module.post_status
        calls: list[str] = []
        statuses: list[tuple[str, str, str]] = []
        try:
            worker_module.fetch_task_payload = lambda server_url, task_id: {"site_statuses": {}}
            worker_module.run_task = lambda *args, **kwargs: calls.append("duty_work_log") or SimpleNamespace(
                ok=True, status="duty_work_log_saved", detail="duty ok"
            )
            worker_module.run_vehicle_task = lambda *args, **kwargs: calls.append("vehicle_mileage") or SimpleNamespace(
                ok=True, status="vehicle_mileage_saved", detail="mileage ok"
            )
            worker_module.run_fuel_worker_task = lambda *args, **kwargs: calls.append("fuel_record") or SimpleNamespace(
                ok=True, status="fuel_record_saved", detail="fuel ok"
            )
            worker_module.run_consumables_worker_task = lambda *args, **kwargs: calls.append("consumables") or SimpleNamespace(
                ok=False, status="consumables_failed", detail="login failed"
            )
            worker_module.run_disinfection_worker_task = lambda *args, **kwargs: calls.append("disinfection") or SimpleNamespace(
                ok=True, status="disinfection_saved", detail="disinfection ok"
            )
            worker_module.post_status = lambda server_url, task_id, status, detail, **kwargs: statuses.append(
                (status, detail, kwargs.get("site_key", ""))
            )

            result = worker_module.run_all_sites_task(
                "http://nas",
                "worker-a",
                {"task_id": "task-2", "created_at": "2026-06-09T00:00:00"},
                Path("artifacts"),
            )
        finally:
            worker_module.fetch_task_payload = original_fetch_payload
            worker_module.run_task = original_run_task
            worker_module.run_vehicle_task = original_run_vehicle
            worker_module.run_fuel_worker_task = original_run_fuel
            worker_module.run_disinfection_worker_task = original_run_disinfection
            worker_module.run_consumables_worker_task = original_run_consumables
            worker_module.post_status = original_post_status

        self.assertEqual(calls, ["duty_work_log", "vehicle_mileage", "consumables", "disinfection"])
        self.assertEqual(result.status, "consumables_failed")
        self.assertEqual(statuses[-1][0], "desktop_fast_completed_with_errors")
        self.assertIn("1 站失敗", statuses[-1][1])
        self.assertIn("接續後續站別", statuses[-1][1])
        self.assertEqual(statuses[-1][2], "")

    def test_run_fuel_worker_task_skips_without_posting_status_when_not_enabled(self):
        original_run_fuel = worker_module.run_fuel_record_task
        original_post_status = worker_module.post_status
        posts: list[str] = []
        try:
            worker_module.run_fuel_record_task = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fuel should not run"))
            worker_module.post_status = lambda server_url, task_id, status, detail, **kwargs: posts.append(status)

            result = worker_module.run_fuel_worker_task(
                "http://nas",
                "worker-a",
                {"task_id": "task-no-fuel", "created_at": "2026-06-09T00:00:00"},
                Path("artifacts"),
            )
        finally:
            worker_module.run_fuel_record_task = original_run_fuel
            worker_module.post_status = original_post_status

        self.assertEqual(result.status, "fuel_record_skipped")
        self.assertEqual(posts, [])

    def test_auto_claim_run_all_sites_marks_error_when_status_fetch_fails(self):
        original_fetch_payload = worker_module.fetch_task_payload
        original_run_task = worker_module.run_task
        original_post_status = worker_module.post_status
        statuses: list[tuple[str, str, str]] = []
        try:
            worker_module.fetch_task_payload = lambda server_url, task_id: (_ for _ in ()).throw(RuntimeError("NAS timeout"))
            worker_module.run_task = lambda *args, **kwargs: self.fail("site runner should not start when status fetch fails")
            worker_module.post_status = lambda server_url, task_id, status, detail, **kwargs: statuses.append(
                (status, detail, kwargs.get("site_key", ""))
            )

            result = worker_module.run_all_sites_task(
                "http://nas",
                "worker-a",
                {"task_id": "task-fetch-fail", "created_at": "2026-06-09T00:00:00"},
                Path("artifacts"),
            )
        finally:
            worker_module.fetch_task_payload = original_fetch_payload
            worker_module.run_task = original_run_task
            worker_module.post_status = original_post_status

        self.assertEqual(result.status, "duty_work_log_failed")
        self.assertIn("讀取任務狀態失敗", result.detail)
        self.assertIn("NAS timeout", result.detail)
        self.assertEqual(statuses[-1][0], "desktop_fast_completed_with_errors")
        self.assertIn("五站流程已停止", statuses[-1][1])

    def test_auto_claim_run_all_sites_marks_error_when_status_payload_is_missing(self):
        original_fetch_payload = worker_module.fetch_task_payload
        original_run_task = worker_module.run_task
        original_post_status = worker_module.post_status
        statuses: list[tuple[str, str, str]] = []
        try:
            worker_module.fetch_task_payload = lambda server_url, task_id: None
            worker_module.run_task = lambda *args, **kwargs: self.fail("site runner should not start without task payload")
            worker_module.post_status = lambda server_url, task_id, status, detail, **kwargs: statuses.append(
                (status, detail, kwargs.get("site_key", ""))
            )

            result = worker_module.run_all_sites_task(
                "http://nas",
                "worker-a",
                {"task_id": "task-fetch-empty", "created_at": "2026-06-09T00:00:00"},
                Path("artifacts"),
            )
        finally:
            worker_module.fetch_task_payload = original_fetch_payload
            worker_module.run_task = original_run_task
            worker_module.post_status = original_post_status

        self.assertEqual(result.status, "duty_work_log_failed")
        self.assertIn("NAS 未回傳任務內容", result.detail)
        self.assertEqual(statuses[-1][0], "desktop_fast_completed_with_errors")
        self.assertIn("NAS 未回傳任務內容", statuses[-1][1])


if __name__ == "__main__":
    unittest.main()
