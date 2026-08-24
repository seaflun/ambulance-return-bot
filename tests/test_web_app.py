import base64
import html
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from werkzeug.datastructures import MultiDict

import app as app_module
import ambulance_bot.selenium_local as selenium_local_module
from ambulance_bot.credential_envelope import open_credential_payload
from ambulance_bot.civilpower_roster import roster_member_id
from ambulance_bot.manual_task_lock import (
    clear_manual_task_lock,
    manual_task_lock_active,
    manual_task_lock_owner,
    manual_task_lock_path,
    set_manual_task_lock,
)
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.selenium_local import DutyCaseLookupResult
from ambulance_bot.task_cancellation import task_cancellation_marker_path, task_cancellation_requested
from ambulance_bot.task_runner import TaskRunner
from ambulance_bot.task_store import JsonTaskStore


class FakeDesktopRunner:
    def __init__(self, store):
        self.store = store
        self.started: list[str] = []
        self.started_sites: list[tuple[str, str]] = []

    def start_existing(self, task_id: str) -> str:
        self.started.append(task_id)
        self.store.set_overall_status(task_id, "desktop_fast_running", "本機快速執行已啟動。")
        return task_id

    def start_site(self, task_id: str, site_key: str) -> str:
        self.started_sites.append((task_id, site_key))
        self.store.set_overall_status(task_id, "desktop_fast_running", f"{site_key} running")
        return task_id

    def wait_for_idle(self, timeout_seconds: float = 5.0) -> bool:
        return True


class WebAppTests(unittest.TestCase):
    def setUp(self):
        os.environ["OPEN_LOCAL_BROWSER_ON_RUN"] = "false"
        os.environ["USE_LOCAL_SELENIUM"] = "false"
        self.tmp = tempfile.TemporaryDirectory()
        self.original_worker_token = os.environ.get("WORKER_TOKEN")
        self.original_remote_update_admin_token = os.environ.get("REMOTE_UPDATE_ADMIN_TOKEN")
        self.original_credential_sync_token = os.environ.get("CREDENTIAL_SYNC_TOKEN")
        self.original_credential_sync_ttl = os.environ.get("CREDENTIAL_SYNC_TTL_SECONDS")
        self.original_duty_saved_login_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        self.original_duty_saved_login_path_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        self.original_duty_account = os.environ.get("DUTY_ACCOUNT")
        self.original_duty_password = os.environ.get("DUTY_PASSWORD")
        self.original_desktop_fast_mode = os.environ.get("DESKTOP_FAST_MODE")
        self.original_task_execution_mode = os.environ.get("TASK_EXECUTION_MODE")
        self.original_public_pc_report_enabled = os.environ.get("PUBLIC_PC_REPORT_ENABLED")
        self.original_worker_server_url = os.environ.get("WORKER_SERVER_URL")
        self.original_start_local_case_lookup = app_module.start_local_case_lookup
        self.original_write_case_lookup_request = app_module.write_case_lookup_request
        self.original_local_host_candidates = app_module.local_host_candidates
        self.original_run_case_lookup_query = getattr(app_module, "run_case_lookup_query", None)
        self.original_cleanup_worker_chrome_residue = app_module.cleanup_worker_chrome_residue
        self.original_subprocess_run = getattr(getattr(app_module, "subprocess", subprocess), "run", subprocess.run)
        self.original_query_duty_emergency_cases = selenium_local_module.query_duty_emergency_cases
        os.environ["WORKER_TOKEN"] = ""
        os.environ["REMOTE_UPDATE_ADMIN_TOKEN"] = "test-admin-token"
        os.environ["CREDENTIAL_SYNC_TOKEN"] = ""
        os.environ.pop("CREDENTIAL_SYNC_TTL_SECONDS", None)
        os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
        os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
        os.environ.pop("DUTY_ACCOUNT", None)
        os.environ.pop("DUTY_PASSWORD", None)
        os.environ["DESKTOP_FAST_MODE"] = "0"
        os.environ["TASK_EXECUTION_MODE"] = "worker_queue"
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "false"
        os.environ["WORKER_SERVER_URL"] = ""
        self.original_artifacts_dir = app_module.artifacts_dir
        app_module.artifacts_dir = Path(self.tmp.name)
        self.store = JsonTaskStore(Path(self.tmp.name) / "tasks")
        app_module.store = self.store
        app_module.runner = TaskRunner(Path(self.tmp.name), store=self.store)
        app_module.desktop_runner = FakeDesktopRunner(self.store)
        app_module._case_lookup_start_error = ""
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def post_remote_update(self):
        return self.client.post(
            "/admin/public-pc/remote-update",
            data={
                "csrf_token": app_module.remote_update_csrf_token(),
                "admin_token": os.environ.get("REMOTE_UPDATE_ADMIN_TOKEN", ""),
            },
        )

    def post_worker_control(self, payload: dict, token: str = "test-token"):
        return self.client.post(
            "/worker/control",
            headers={"X-Worker-Token": token},
            json=payload,
        )

    def _valid_control_payload(self, *, route: dict | None = None) -> dict:
        return {
            "worker_id": "PC-01",
            "package_version": "2026.07.15.1326",
            "pid": 321,
            "process_started_at": "2026-07-15T15:00:00",
            "execution_mode": "gui",
            "package_path": "C:/Ambulance/WinPython_公務電腦使用包",
            "state": "idle",
            "activity": "",
            "busy_reason": "",
            "route": route or {"name": "lan", "identity_status": "unverified", "instance_id": ""},
        }

    def _create_legacy_public_pc_task(self, task_id: str) -> dict:
        request = AmbulanceReturnRequest(
            task_id=task_id,
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        payload = self.store.create(request)
        payload["overall_status"] = "desktop_fast_completed_with_errors"
        payload["site_statuses"]["duty_work_log"].update(
            status="duty_work_log_waiting_confirmation",
            detail="waiting_confirmation: 已按下儲存，但未收到儲存成功回應；請人工確認。",
        )
        self.store.save_payload(task_id, payload)
        return self.store.get(task_id)

    def tearDown(self):
        app_module.runner.wait_for_idle()
        app_module.desktop_runner.wait_for_idle()
        app_module.artifacts_dir = self.original_artifacts_dir
        if self.original_worker_token is None:
            os.environ.pop("WORKER_TOKEN", None)
        else:
            os.environ["WORKER_TOKEN"] = self.original_worker_token
        if self.original_remote_update_admin_token is None:
            os.environ.pop("REMOTE_UPDATE_ADMIN_TOKEN", None)
        else:
            os.environ["REMOTE_UPDATE_ADMIN_TOKEN"] = self.original_remote_update_admin_token
        if self.original_desktop_fast_mode is None:
            os.environ.pop("DESKTOP_FAST_MODE", None)
        else:
            os.environ["DESKTOP_FAST_MODE"] = self.original_desktop_fast_mode
        if self.original_task_execution_mode is None:
            os.environ.pop("TASK_EXECUTION_MODE", None)
        else:
            os.environ["TASK_EXECUTION_MODE"] = self.original_task_execution_mode
        if self.original_public_pc_report_enabled is None:
            os.environ.pop("PUBLIC_PC_REPORT_ENABLED", None)
        else:
            os.environ["PUBLIC_PC_REPORT_ENABLED"] = self.original_public_pc_report_enabled
        self._restore_env("WORKER_SERVER_URL", self.original_worker_server_url)
        app_module.start_local_case_lookup = self.original_start_local_case_lookup
        app_module.write_case_lookup_request = self.original_write_case_lookup_request
        app_module.local_host_candidates = self.original_local_host_candidates
        if self.original_run_case_lookup_query is None:
            if hasattr(app_module, "run_case_lookup_query"):
                delattr(app_module, "run_case_lookup_query")
        else:
            app_module.run_case_lookup_query = self.original_run_case_lookup_query
        app_module.cleanup_worker_chrome_residue = self.original_cleanup_worker_chrome_residue
        if hasattr(app_module, "subprocess"):
            app_module.subprocess.run = self.original_subprocess_run
        app_module._case_lookup_start_error = ""
        selenium_local_module.query_duty_emergency_cases = self.original_query_duty_emergency_cases
        self._restore_env("CREDENTIAL_SYNC_TOKEN", self.original_credential_sync_token)
        self._restore_env("CREDENTIAL_SYNC_TTL_SECONDS", self.original_credential_sync_ttl)
        self._restore_env("DUTY_SAVED_LOGIN_PATH", self.original_duty_saved_login_path)
        self._restore_env("DUTY_SAVED_LOGIN_PATH_OVERRIDE", self.original_duty_saved_login_path_override)
        self._restore_env("DUTY_ACCOUNT", self.original_duty_account)
        self._restore_env("DUTY_PASSWORD", self.original_duty_password)
        self.tmp.cleanup()

    def _restore_env(self, name: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def valid_task_data(self, **overrides):
        data = {
            "case_id": "case-test-001",
            "vehicle": "\u65b0\u576191",
            "driver": "\u66fe\u5f65\u7db8",
            "mileage": "12345",
            "case_date": "2026-06-07",
            "case_time": "1024",
            "return_date": "2026-06-07",
            "return_time": "1119",
            "case_address": "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def1\u865f",
            "case_reason": "\u6025\u75c5",
            "patient_summary": "\u7537\u4e00\u540d",
            "consumables": "\u6843-\u53e3\u7f69(\u7247)=2",
        }
        data.update(overrides)
        return data

    def import_case_for_form(self, case: dict) -> None:
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "updated_at": "2026-06-03T08:00:00",
                "cases": [case],
            },
        )
        response = self.client.post("/cases/import", data={"case_id": case["case_id"]}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)

    def credential_sync_payload(self) -> dict:
        return {
            "sync_code": "sync-test-1",
            "user_id": "user9",
            "accounts": [
                {"actor_no": "8", "name": "王小明", "user_id": "user8", "password": "pass8"},
                {"actor_no": "9", "user_id": "user9", "password": "pass9"},
            ],
        }

    def test_credential_sync_endpoint_requires_source_token(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"

        response = self.client.post("/api/credential-sync", json=self.credential_sync_payload())

        self.assertEqual(response.status_code, 403)

    def test_credential_sync_rejects_short_worker_secret_without_persisting_plaintext(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        os.environ["WORKER_TOKEN"] = "short-worker-token"

        response = self.client.post(
            "/api/credential-sync",
            json=self.credential_sync_payload(),
            headers={"X-Credential-Sync-Token": "sync-token"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "credential_sealing_unavailable")
        self.assertFalse(app_module.credential_sync_relay_file().exists())

    def test_credential_sync_default_ttl_is_fifteen_minutes(self):
        os.environ.pop("CREDENTIAL_SYNC_TTL_SECONDS", None)

        self.assertEqual(app_module.credential_sync_ttl_seconds(), 900)

    def test_credential_sync_relay_rejects_oversized_file_before_json_read(self):
        path = app_module.credential_sync_relay_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (app_module.MAX_CREDENTIAL_RELAY_FILE_BYTES + 1))

        with mock.patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("oversized relay must not be read into memory"),
        ):
            self.assertEqual(app_module.read_credential_sync_relay(), {})

        self.assertFalse(path.exists())

    def test_credential_sync_endpoint_queues_for_worker_without_local_save(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        worker_token = "0123456789abcdef0123456789abcdef"
        os.environ["WORKER_TOKEN"] = worker_token
        saved_login = Path(self.tmp.name) / "nas_should_not_save.json"
        os.environ["DUTY_SAVED_LOGIN_PATH"] = str(saved_login)
        os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"

        response = self.client.post(
            "/api/credential-sync",
            json=self.credential_sync_payload(),
            headers={"X-Credential-Sync-Token": "sync-token"},
        )
        response_body = response.data.decode("utf-8")
        relay_path = app_module.credential_sync_relay_file()
        relay_text = relay_path.read_text(encoding="utf-8")
        record = json.loads(relay_text)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("pass9", response_body)
        self.assertEqual(response.get_json()["ack_id"], "sync-test-1")
        self.assertEqual(response.get_json()["count"], 2)
        self.assertTrue(response.get_json()["queued"])
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["account_count"], 2)
        self.assertNotIn("selected_user_id", record)
        self.assertNotIn("payload", record)
        self.assertIn("sealed_payload", record)
        self.assertNotIn("pass8", relay_text)
        self.assertNotIn("pass9", relay_text)
        self.assertFalse(saved_login.exists())

        worker_response = self.client.get("/worker/credential-sync", headers={"X-Worker-Token": worker_token})
        worker_payload = worker_response.get_json()["request"]
        self.assertEqual(worker_response.status_code, 200)
        self.assertEqual(worker_payload["request_id"], "sync-test-1")
        self.assertNotIn("payload", worker_payload)
        self.assertEqual(
            open_credential_payload(worker_payload["sealed_payload"], worker_token)["accounts"][1]["password"],
            "pass9",
        )

        ack_response = self.client.post(
            "/worker/credential-sync/sync-test-1/ack",
            json={"status": "saved", "detail": "saved"},
            headers={"X-Worker-Token": worker_token},
        )
        self.assertEqual(ack_response.status_code, 200)
        self.assertFalse(relay_path.exists())

        empty_response = self.client.get("/worker/credential-sync", headers={"X-Worker-Token": worker_token})
        self.assertIsNone(empty_response.get_json()["request"])

    def test_credential_sync_failed_ack_retains_pending_request_without_plaintext(self):
        worker_token = "0123456789abcdef0123456789abcdef"
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        os.environ["WORKER_TOKEN"] = worker_token
        queued = self.client.post(
            "/api/credential-sync",
            json=self.credential_sync_payload(),
            headers={"X-Credential-Sync-Token": "sync-token"},
        )
        self.assertEqual(queued.status_code, 200)

        failed = self.client.post(
            "/worker/credential-sync/sync-test-1/ack",
            json={"status": "failed", "detail": "disk busy"},
            headers={"X-Worker-Token": worker_token},
        )
        record = app_module.read_credential_sync_relay()
        raw = app_module.credential_sync_relay_file().read_text(encoding="utf-8")

        self.assertEqual(failed.status_code, 200)
        self.assertTrue(failed.get_json()["retained"])
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(record["last_error_code"], "worker_save_failed")
        self.assertNotIn("disk busy", record["last_error"])
        self.assertNotIn("disk busy", raw)
        self.assertIn("sealed_payload", record)
        self.assertNotIn("pass8", raw)
        self.assertNotIn("pass9", raw)

    def test_credential_sync_failed_ack_does_not_extend_absolute_ttl(self):
        worker_token = "0123456789abcdef0123456789abcdef"
        os.environ["WORKER_TOKEN"] = worker_token
        created_at = datetime.now() - timedelta(seconds=600)
        app_module.write_credential_sync_relay(
            {
                "request_id": "absolute-ttl",
                "created_at": created_at.isoformat(timespec="seconds"),
                "status": "pending",
                "sealed_payload": app_module.seal_credential_payload(
                    self.credential_sync_payload(),
                    worker_token,
                ),
            }
        )

        failed = self.client.post(
            "/worker/credential-sync/absolute-ttl/ack",
            json={"status": "failed", "detail": "temporary failure"},
            headers={"X-Worker-Token": worker_token},
        )
        self.assertEqual(failed.status_code, 200)
        self.assertTrue(app_module.credential_sync_relay_file().exists())

        after_absolute_expiry = created_at.timestamp() + app_module.credential_sync_ttl_seconds() + 1
        with mock.patch.object(app_module.time, "time", return_value=after_absolute_expiry):
            self.assertEqual(app_module.read_credential_sync_relay(), {})

        self.assertFalse(app_module.credential_sync_relay_file().exists())

    def test_worker_get_migrates_legacy_plaintext_relay_to_sealed_storage(self):
        worker_token = "0123456789abcdef0123456789abcdef"
        os.environ["WORKER_TOKEN"] = worker_token
        app_module.write_credential_sync_relay(
            {
                "request_id": "legacy-sync",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "status": "pending",
                "payload": self.credential_sync_payload(),
            }
        )

        response = self.client.get(
            "/worker/credential-sync",
            headers={"X-Worker-Token": worker_token},
        )
        record = app_module.read_credential_sync_relay()
        raw = app_module.credential_sync_relay_file().read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("payload", record)
        self.assertIn("sealed_payload", record)
        self.assertNotIn("pass8", raw)
        self.assertNotIn("pass9", raw)

    def test_web_startup_migrates_legacy_plaintext_relay_before_serving(self):
        worker_token = "0123456789abcdef0123456789abcdef"
        os.environ["WORKER_TOKEN"] = worker_token
        app_module.write_credential_sync_relay(
            {
                "request_id": "legacy-at-startup",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "status": "pending",
                "payload": self.credential_sync_payload(),
            }
        )

        with mock.patch("waitress.serve") as serve:
            app_module.run_web_app(host="127.0.0.1", port=18080)

        record = app_module.read_credential_sync_relay()
        raw = app_module.credential_sync_relay_file().read_text(encoding="utf-8")
        serve.assert_called_once()
        self.assertNotIn("payload", record)
        self.assertIn("sealed_payload", record)
        self.assertNotIn("pass8", raw)

    def test_reconcile_legacy_public_pc_tasks_reports_only_changed_tasks(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        eligible = self._create_legacy_public_pc_task("legacy-eligible")
        explicit_request = AmbulanceReturnRequest(
            task_id="legacy-explicit-failure",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        explicit = self.store.create(explicit_request)
        explicit["overall_status"] = "desktop_fast_completed_with_errors"
        explicit["site_statuses"]["consumables"].update(
            status="consumables_failed",
            detail="耗材頁車輛候選不符：新坡92。",
        )
        self.store.save_payload(explicit_request.task_id, explicit)
        completed_request = AmbulanceReturnRequest(
            task_id="legacy-already-corrected",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        completed = self.store.create(completed_request)
        completed["site_statuses"]["duty_work_log"].update(
            status="duty_work_log_saved",
            detail="已儲存。",
        )
        self.store.save_payload(completed_request.task_id, completed)

        with mock.patch.object(self.store, "list_recent", wraps=self.store.list_recent) as list_recent:
            with mock.patch.object(app_module, "report_public_pc_task_event") as report:
                with contextlib.redirect_stdout(io.StringIO()):
                    changed_count = app_module.reconcile_legacy_public_pc_tasks()

        self.assertEqual(changed_count, 1)
        list_recent.assert_called_once_with(limit=500)
        report.assert_called_once()
        reported_payload, action = report.call_args.args
        self.assertEqual(reported_payload["task"]["task_id"], eligible["task"]["task_id"])
        self.assertEqual(
            reported_payload["site_statuses"]["duty_work_log"]["status"],
            "duty_work_log_saved",
        )
        self.assertEqual(action, "舊版無提示儲存狀態自動校正")
        self.assertEqual(
            self.store.get(explicit_request.task_id)["site_statuses"]["consumables"]["status"],
            "consumables_failed",
        )

    def test_reconcile_legacy_public_pc_tasks_is_idempotent_and_skips_missing_task(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        eligible = self._create_legacy_public_pc_task("legacy-idempotent")

        with mock.patch.object(
            self.store,
            "list_recent",
            side_effect=[
                [{"task": {"task_id": "missing-task"}}, eligible],
                self.store.list_recent(limit=500),
            ],
        ):
            with mock.patch.object(app_module, "report_public_pc_task_event") as report:
                with contextlib.redirect_stdout(io.StringIO()):
                    first_count = app_module.reconcile_legacy_public_pc_tasks()
                    second_count = app_module.reconcile_legacy_public_pc_tasks()

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 0)
        report.assert_called_once()

    def test_reconcile_legacy_public_pc_tasks_skips_corrupt_task_and_continues(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        corrupt = self._create_legacy_public_pc_task("legacy-corrupt-events")
        corrupt["events"] = {}
        self.store.save_payload("legacy-corrupt-events", corrupt)
        eligible = self._create_legacy_public_pc_task("legacy-after-corrupt")

        with mock.patch.object(
            self.store,
            "list_recent",
            return_value=[self.store.get("legacy-corrupt-events"), eligible],
        ):
            with mock.patch.object(app_module, "report_public_pc_task_event") as report:
                with contextlib.redirect_stdout(io.StringIO()):
                    changed_count = app_module.reconcile_legacy_public_pc_tasks()

        self.assertEqual(changed_count, 1)
        report.assert_called_once()
        self.assertEqual(report.call_args.args[0]["task"]["task_id"], "legacy-after-corrupt")
        self.assertEqual(
            self.store.get("legacy-corrupt-events")["site_statuses"]["duty_work_log"]["status"],
            "duty_work_log_waiting_confirmation",
        )

    def test_reconcile_legacy_public_pc_tasks_retries_persisted_report_marker(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        self._create_legacy_public_pc_task("legacy-report-retry")

        with mock.patch.object(
            app_module,
            "report_public_pc_task_event",
            side_effect=OSError("pending path unavailable"),
        ) as failed_report:
            with contextlib.redirect_stdout(io.StringIO()):
                first_count = app_module.reconcile_legacy_public_pc_tasks()

        after_failure = self.store.get("legacy-report-retry")
        marker = after_failure["legacy_silent_save_report"]
        self.assertEqual(first_count, 1)
        self.assertTrue(marker["pending"])
        self.assertEqual(failed_report.call_args.kwargs["event_id"], marker["event_id"])

        with mock.patch.object(app_module, "report_public_pc_task_event", return_value=True) as retry_report:
            with contextlib.redirect_stdout(io.StringIO()):
                retry_count = app_module.reconcile_legacy_public_pc_tasks()

        self.assertEqual(retry_count, 0)
        retry_report.assert_called_once()
        self.assertEqual(retry_report.call_args.kwargs["event_id"], marker["event_id"])
        self.assertFalse(self.store.get("legacy-report-retry")["legacy_silent_save_report"]["pending"])

        with mock.patch.object(app_module, "report_public_pc_task_event") as duplicate_report:
            with contextlib.redirect_stdout(io.StringIO()):
                final_count = app_module.reconcile_legacy_public_pc_tasks()
        self.assertEqual(final_count, 0)
        duplicate_report.assert_not_called()

    def test_reconcile_legacy_public_pc_tasks_survives_semantically_corrupt_cleanup_task(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        corrupt_request = AmbulanceReturnRequest(
            task_id="legacy-corrupt-fuel-site",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        corrupt = self.store.create(corrupt_request)
        corrupt["site_statuses"]["fuel_record"] = 1
        self.store.save_payload(corrupt_request.task_id, corrupt)
        self._create_legacy_public_pc_task("legacy-after-corrupt-cleanup")

        with mock.patch.object(app_module, "report_public_pc_task_event", return_value=True) as report:
            with contextlib.redirect_stdout(io.StringIO()):
                changed_count = app_module.reconcile_legacy_public_pc_tasks()

        self.assertEqual(changed_count, 1)
        report.assert_called_once()
        self.assertEqual(report.call_args.args[0]["task"]["task_id"], "legacy-after-corrupt-cleanup")
        self.assertEqual(self.store.get(corrupt_request.task_id)["site_statuses"]["fuel_record"], 1)

    def test_reconcile_legacy_public_pc_tasks_queues_report_when_nas_is_offline(self):
        self._create_legacy_public_pc_task("legacy-offline")
        with mock.patch.dict(
            os.environ,
            {
                "PUBLIC_PC_REPORT_ENABLED": "true",
                "PUBLIC_PC_REPORT_SERVER_URL": "http://nas.test",
            },
            clear=False,
        ):
            with mock.patch.object(
                app_module,
                "_post_public_pc_report",
                side_effect=urllib.error.URLError("offline"),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    changed_count = app_module.reconcile_legacy_public_pc_tasks()

        pending = app_module._load_pending_public_pc_reports()
        self.assertEqual(changed_count, 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["task_id"], "legacy-offline")
        self.assertEqual(pending[0]["action"], "舊版無提示儲存狀態自動校正")

    def test_reconcile_legacy_public_pc_tasks_flushes_durable_outbox_on_next_startup(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        os.environ["PUBLIC_PC_REPORT_SERVER_URL"] = "http://nas.test"
        self._create_legacy_public_pc_task("legacy-offline-recovery")

        with mock.patch.object(
            app_module,
            "_post_public_pc_report",
            side_effect=urllib.error.URLError("offline"),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                first_count = app_module.reconcile_legacy_public_pc_tasks()

        marker = self.store.get("legacy-offline-recovery")["legacy_silent_save_report"]
        pending = app_module._load_pending_public_pc_reports()
        self.assertEqual(first_count, 1)
        self.assertFalse(marker["pending"])
        self.assertEqual(len(pending), 1)

        sent_event_ids: list[str] = []

        def acknowledge(_server_url: str, payload: dict) -> dict:
            sent_event_ids.append(payload["event_id"])
            return {"ack_id": payload["event_id"]}

        with mock.patch.object(app_module, "_post_public_pc_report", side_effect=acknowledge):
            with contextlib.redirect_stdout(io.StringIO()):
                retry_count = app_module.reconcile_legacy_public_pc_tasks()

        self.assertEqual(retry_count, 0)
        self.assertEqual(sent_event_ids, [marker["event_id"]])
        self.assertFalse(app_module.public_pc_pending_report_file().exists())

    def test_public_pc_report_is_durable_before_non_network_send_failure(self):
        task_payload = {
            "task": {"task_id": "durable-report", "case_reason": "急病"},
            "overall_status": "desktop_fast_completed",
            "site_statuses": {},
            "events": [{"status": "legacy_silent_save_reconciled", "detail": "已校正。"}],
            "created_at": "2026-07-14T12:00:00",
        }
        with mock.patch.dict(
            os.environ,
            {
                "PUBLIC_PC_REPORT_ENABLED": "true",
                "PUBLIC_PC_REPORT_SERVER_URL": "http://nas.test",
            },
            clear=False,
        ):
            with mock.patch.object(
                app_module,
                "_post_public_pc_report",
                side_effect=UnicodeDecodeError("utf-8", b"x", 0, 1, "bad response"),
            ):
                accepted = app_module.report_public_pc_task_event(
                    task_payload,
                    "舊版無提示儲存狀態自動校正",
                    event_id="stable-reconciliation-event",
                )

        pending = app_module._load_pending_public_pc_reports()
        self.assertTrue(accepted)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event_id"], "stable-reconciliation-event")
        self.assertEqual(pending[0]["task_id"], "durable-report")
        self.assertFalse(pending[0]["completion"]["all_complete"])

    def test_public_pc_report_requires_explicit_matching_ack(self):
        task_payload = {
            "task": {"task_id": "explicit-ack", "case_reason": "急病"},
            "overall_status": "desktop_fast_completed",
            "site_statuses": {},
            "events": [{"status": "completed", "detail": "完成"}],
            "created_at": "2026-07-14T12:00:00",
        }
        with mock.patch.dict(
            os.environ,
            {
                "PUBLIC_PC_REPORT_ENABLED": "true",
                "PUBLIC_PC_REPORT_SERVER_URL": "http://nas.test",
            },
            clear=False,
        ):
            with mock.patch.object(app_module, "_post_public_pc_report", return_value={}):
                accepted = app_module.report_public_pc_task_event(
                    task_payload,
                    "舊版無提示儲存狀態自動校正",
                    event_id="explicit-ack-event",
                )

        pending = app_module._load_pending_public_pc_reports()
        self.assertTrue(accepted)
        self.assertEqual([entry["event_id"] for entry in pending], ["explicit-ack-event"])

    def test_public_pc_report_preserves_malformed_pending_file(self):
        task_payload = {
            "task": {"task_id": "strict-outbox", "case_reason": "急病"},
            "overall_status": "created",
            "site_statuses": {},
            "events": [{"status": "created", "detail": "建立"}],
            "created_at": "2026-07-14T12:00:00",
        }
        path = app_module.public_pc_pending_report_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        original = b'{"event_id":"old-event"}\nnot-json\n'
        path.write_bytes(original)

        with mock.patch.dict(
            os.environ,
            {
                "PUBLIC_PC_REPORT_ENABLED": "true",
                "PUBLIC_PC_REPORT_SERVER_URL": "http://nas.test",
            },
            clear=False,
        ):
            with mock.patch.object(app_module, "_post_public_pc_report") as post:
                with contextlib.redirect_stdout(io.StringIO()):
                    accepted = app_module.report_public_pc_task_event(
                        task_payload,
                        "建立任務",
                        event_id="strict-outbox-new-event",
                    )

        self.assertTrue(accepted)
        post.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        spool_files = list(app_module.public_pc_pending_report_spool_dir().glob("*.json"))
        self.assertEqual(len(spool_files), 1)
        self.assertEqual(json.loads(spool_files[0].read_text(encoding="utf-8"))["event_id"], "strict-outbox-new-event")

        sent_event_ids: list[str] = []

        def acknowledge(_server_url: str, payload: dict) -> dict:
            sent_event_ids.append(payload["event_id"])
            return {"ack_id": payload["event_id"]}

        with mock.patch.dict(
            os.environ,
            {
                "PUBLIC_PC_REPORT_ENABLED": "true",
                "PUBLIC_PC_REPORT_SERVER_URL": "http://nas.test",
            },
            clear=False,
        ):
            with mock.patch.object(app_module, "_post_public_pc_report", side_effect=acknowledge):
                with contextlib.redirect_stdout(io.StringIO()):
                    all_queues_flushed = app_module.flush_pending_public_pc_reports()

        self.assertFalse(all_queues_flushed)
        self.assertEqual(sent_event_ids, ["strict-outbox-new-event"])
        self.assertEqual(path.read_bytes(), original)
        retained_spool = list(app_module.public_pc_pending_report_spool_dir().glob("*.json"))
        self.assertEqual(len(retained_spool), 1)
        self.assertTrue(json.loads(retained_spool[0].read_text(encoding="utf-8"))["_spool_checkpoint_acked"])

        app_module._write_pending_public_pc_reports(
            [{"event_id": "repaired-older-event", "task_id": "strict-outbox", "action": "建立任務"}]
        )
        sent_event_ids.clear()
        with mock.patch.dict(
            os.environ,
            {
                "PUBLIC_PC_REPORT_ENABLED": "true",
                "PUBLIC_PC_REPORT_SERVER_URL": "http://nas.test",
            },
            clear=False,
        ):
            with mock.patch.object(app_module, "_post_public_pc_report", side_effect=acknowledge):
                self.assertTrue(app_module.flush_pending_public_pc_reports())

        self.assertEqual(sent_event_ids, ["repaired-older-event", "strict-outbox-new-event"])
        self.assertEqual(list(app_module.public_pc_pending_report_spool_dir().glob("*.json")), [])

    def test_public_pc_report_does_not_write_when_pending_read_fails(self):
        task_payload = {
            "task": {"task_id": "outbox-read-failure", "case_reason": "急病"},
            "overall_status": "created",
            "site_statuses": {},
            "events": [{"status": "created", "detail": "建立"}],
            "created_at": "2026-07-14T12:00:00",
        }
        with mock.patch.dict(
            os.environ,
            {
                "PUBLIC_PC_REPORT_ENABLED": "true",
                "PUBLIC_PC_REPORT_SERVER_URL": "http://nas.test",
            },
            clear=False,
        ):
            with mock.patch.object(
                app_module,
                "_load_pending_public_pc_reports",
                side_effect=OSError("temporarily locked"),
            ):
                with mock.patch.object(app_module, "_write_pending_public_pc_reports") as write_pending:
                    with contextlib.redirect_stdout(io.StringIO()):
                        accepted = app_module.report_public_pc_task_event(
                            task_payload,
                            "建立任務",
                            event_id="outbox-read-failure-event",
                        )

        self.assertTrue(accepted)
        write_pending.assert_not_called()
        spool_files = list(app_module.public_pc_pending_report_spool_dir().glob("*.json"))
        self.assertEqual(len(spool_files), 1)

        with mock.patch.dict(
            os.environ,
            {
                "PUBLIC_PC_REPORT_ENABLED": "true",
                "PUBLIC_PC_REPORT_SERVER_URL": "http://nas.test",
            },
            clear=False,
        ):
            with mock.patch.object(
                app_module,
                "_post_public_pc_report",
                side_effect=lambda _server_url, payload: {"ack_id": payload["event_id"]},
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(app_module.flush_pending_public_pc_reports())

        self.assertEqual(list(app_module.public_pc_pending_report_spool_dir().glob("*.json")), [])

    def test_public_pc_report_spools_until_server_url_becomes_available(self):
        task_payload = {
            "task": {"task_id": "server-url-later", "case_reason": "急病"},
            "overall_status": "created",
            "site_statuses": {},
            "events": [{"status": "created", "detail": "建立"}],
            "created_at": "2026-07-14T12:00:00",
        }
        with mock.patch.object(app_module, "public_pc_reporting_enabled", return_value=True):
            with mock.patch.object(app_module, "public_pc_report_server_url", return_value=""):
                accepted = app_module.report_public_pc_task_event(
                    task_payload,
                    "建立任務",
                    event_id="server-url-later-event",
                )

        self.assertTrue(accepted)
        self.assertEqual(len(list(app_module.public_pc_pending_report_spool_dir().glob("*.json"))), 1)

        with mock.patch.object(app_module, "public_pc_reporting_enabled", return_value=True):
            with mock.patch.object(app_module, "public_pc_report_server_url", return_value="http://nas.test"):
                with mock.patch.object(
                    app_module,
                    "_post_public_pc_report",
                    side_effect=lambda _server_url, payload: {"ack_id": payload["event_id"]},
                ):
                    self.assertTrue(app_module.flush_pending_public_pc_reports())

        self.assertEqual(list(app_module.public_pc_pending_report_spool_dir().glob("*.json")), [])

    def test_public_pc_spool_waits_for_older_main_outbox_event(self):
        older = {"event_id": "older-main-event", "task_id": "older", "action": "建立任務"}
        newer = {"event_id": "newer-spool-event", "task_id": "newer", "action": "修改任務"}
        app_module._write_pending_public_pc_reports([older])
        app_module._persist_public_pc_report_spool(newer)
        attempted: list[str] = []

        def defer_older(_server_url: str, payload: dict) -> dict:
            attempted.append(payload["event_id"])
            return {"ack_id": "not-the-expected-event"}

        with mock.patch.object(app_module, "public_pc_reporting_enabled", return_value=True):
            with mock.patch.object(app_module, "public_pc_report_server_url", return_value="http://nas.test"):
                with mock.patch.object(app_module, "_post_public_pc_report", side_effect=defer_older):
                    self.assertFalse(app_module.flush_pending_public_pc_reports())

        self.assertEqual(attempted, ["older-main-event"])
        self.assertEqual(len(list(app_module.public_pc_pending_report_spool_dir().glob("*.json"))), 1)

        attempted.clear()

        def acknowledge(_server_url: str, payload: dict) -> dict:
            attempted.append(payload["event_id"])
            return {"ack_id": payload["event_id"]}

        with mock.patch.object(app_module, "public_pc_reporting_enabled", return_value=True):
            with mock.patch.object(app_module, "public_pc_report_server_url", return_value="http://nas.test"):
                with mock.patch.object(app_module, "_post_public_pc_report", side_effect=acknowledge):
                    self.assertTrue(app_module.flush_pending_public_pc_reports())

        self.assertEqual(attempted, ["older-main-event", "newer-spool-event"])

    def test_new_report_does_not_overtake_older_spooled_report(self):
        older_payload = {
            "task": {"task_id": "same-task", "case_reason": "急病"},
            "overall_status": "created",
            "site_statuses": {},
            "events": [{"status": "created", "detail": "older"}],
            "created_at": "2026-07-14T12:00:00",
        }
        newer_payload = {
            **older_payload,
            "overall_status": "desktop_fast_completed",
            "events": [{"status": "completed", "detail": "newer"}],
        }
        with mock.patch.object(app_module, "public_pc_reporting_enabled", return_value=True):
            with mock.patch.object(app_module, "public_pc_report_server_url", return_value=""):
                self.assertTrue(
                    app_module.report_public_pc_task_event(
                        older_payload,
                        "建立任務",
                        event_id="older-spooled-event",
                    )
                )

        attempted: list[str] = []

        def acknowledge(_server_url: str, payload: dict) -> dict:
            attempted.append(payload["event_id"])
            return {"ack_id": payload["event_id"]}

        with mock.patch.object(app_module, "public_pc_reporting_enabled", return_value=True):
            with mock.patch.object(app_module, "public_pc_report_server_url", return_value="http://nas.test"):
                with mock.patch.object(app_module, "_post_public_pc_report", side_effect=acknowledge):
                    self.assertTrue(
                        app_module.report_public_pc_task_event(
                            newer_payload,
                            "人工確認站別完成",
                            event_id="newer-spooled-event",
                        )
                    )

        self.assertEqual(attempted, ["older-spooled-event", "newer-spooled-event"])
        self.assertEqual(list(app_module.public_pc_pending_report_spool_dir().glob("*.json")), [])

    def test_main_outbox_unlink_failure_retains_newer_spool_checkpoint(self):
        main_path = app_module.public_pc_pending_report_file()
        older = {"event_id": "unlink-older-main", "task_id": "same-task", "action": "建立任務"}
        newer = {
            "event_id": "unlink-newer-spool",
            "task_id": "same-task",
            "action": "人工確認站別完成",
            "_spool_checkpoint_acked": True,
        }
        app_module._write_pending_public_pc_reports([older])
        spool_path = app_module._persist_public_pc_report_spool(newer)
        attempted: list[str] = []

        def acknowledge(_server_url: str, payload: dict) -> dict:
            attempted.append(payload["event_id"])
            return {"ack_id": payload["event_id"]}

        original_unlink = Path.unlink
        failed_once = {"value": False}

        def fail_first_main_unlink(path: Path, *args, **kwargs):
            if path == main_path and not failed_once["value"]:
                failed_once["value"] = True
                raise PermissionError("pending outbox is temporarily locked")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(app_module, "public_pc_reporting_enabled", return_value=True):
            with mock.patch.object(app_module, "public_pc_report_server_url", return_value="http://nas.test"):
                with mock.patch.object(app_module, "_post_public_pc_report", side_effect=acknowledge):
                    with mock.patch.object(Path, "unlink", new=fail_first_main_unlink):
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertFalse(app_module.flush_pending_public_pc_reports())

        self.assertEqual(attempted, ["unlink-older-main"])
        self.assertTrue(main_path.exists())
        self.assertTrue(spool_path.exists())

        attempted.clear()
        with mock.patch.object(app_module, "public_pc_reporting_enabled", return_value=True):
            with mock.patch.object(app_module, "public_pc_report_server_url", return_value="http://nas.test"):
                with mock.patch.object(app_module, "_post_public_pc_report", side_effect=acknowledge):
                    self.assertTrue(app_module.flush_pending_public_pc_reports())

        self.assertEqual(attempted, ["unlink-older-main", "unlink-newer-spool"])
        self.assertFalse(main_path.exists())
        self.assertFalse(spool_path.exists())

    def test_start_public_pc_legacy_reconciliation_is_gated_and_daemon(self):
        with mock.patch.object(app_module.threading, "Thread") as thread_class:
            os.environ["PUBLIC_PC_REPORT_ENABLED"] = "false"
            self.assertIsNone(app_module.start_public_pc_legacy_reconciliation())
            thread_class.assert_not_called()

            os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
            thread = app_module.start_public_pc_legacy_reconciliation()

        self.assertIs(thread, thread_class.return_value)
        thread_class.assert_called_once_with(
            target=app_module.reconcile_legacy_public_pc_tasks,
            name="public-pc-legacy-reconciliation",
            daemon=True,
        )
        thread_class.return_value.start.assert_called_once_with()

    def test_start_public_pc_pending_report_flusher_is_gated_and_daemon(self):
        with mock.patch.object(app_module.threading, "Thread") as thread_class:
            os.environ["PUBLIC_PC_REPORT_ENABLED"] = "false"
            self.assertIsNone(app_module.start_public_pc_pending_report_flusher())
            thread_class.assert_not_called()

            os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
            thread = app_module.start_public_pc_pending_report_flusher()

        self.assertIs(thread, thread_class.return_value)
        thread_class.assert_called_once_with(
            target=app_module._run_public_pc_pending_report_flush_loop,
            name="public-pc-pending-report-flusher",
            daemon=True,
        )
        thread_class.return_value.start.assert_called_once_with()

    def test_public_pc_pending_report_flusher_retries_reconciliation_while_enabled(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"

        def reconcile_once() -> int:
            os.environ["PUBLIC_PC_REPORT_ENABLED"] = "false"
            return 0

        def stop_after_wait(_seconds: float) -> None:
            os.environ["PUBLIC_PC_REPORT_ENABLED"] = "false"

        with mock.patch.object(app_module, "reconcile_legacy_public_pc_tasks", side_effect=reconcile_once) as reconcile:
            with mock.patch.object(app_module.time, "sleep", side_effect=stop_after_wait) as sleep:
                app_module._run_public_pc_pending_report_flush_loop()

        sleep.assert_called_once_with(app_module.PUBLIC_PC_PENDING_REPORT_FLUSH_INTERVAL_SECONDS)
        reconcile.assert_called_once_with()

    def test_public_pc_pending_report_flusher_recovers_marker_after_spool_failure(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        os.environ["PUBLIC_PC_REPORT_SERVER_URL"] = "http://nas.test"
        self._create_legacy_public_pc_task("legacy-marker-background-retry")

        with mock.patch.object(
            app_module,
            "_persist_public_pc_report_spool",
            side_effect=OSError("spool locked"),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                first_count = app_module.reconcile_legacy_public_pc_tasks()

        first_marker = self.store.get("legacy-marker-background-retry")["legacy_silent_save_report"]
        self.assertEqual(first_count, 1)
        self.assertTrue(first_marker["pending"])

        def acknowledge(_server_url: str, payload: dict) -> dict:
            return {"ack_id": payload["event_id"]}

        with mock.patch.object(app_module, "_post_public_pc_report", side_effect=acknowledge):
            with contextlib.redirect_stdout(io.StringIO()):
                retry_count = app_module.retry_pending_public_pc_reports()

        recovered = self.store.get("legacy-marker-background-retry")
        self.assertEqual(retry_count, 0)
        self.assertFalse(recovered["legacy_silent_save_report"]["pending"])
        self.assertFalse(app_module.public_pc_pending_report_file().exists())

    def test_start_public_pc_legacy_reconciliation_worker_exception_does_not_escape(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        target_started = threading.Event()

        def fail_in_worker():
            target_started.set()
            raise RuntimeError("worker failed")

        with mock.patch.object(app_module, "reconcile_legacy_public_pc_tasks", side_effect=fail_in_worker):
            with mock.patch.object(app_module.threading, "excepthook") as excepthook:
                thread = app_module.start_public_pc_legacy_reconciliation()
                assert thread is not None
                thread.join(timeout=1)

        self.assertTrue(target_started.is_set())
        self.assertFalse(thread.is_alive())
        excepthook.assert_called_once()

    def test_web_startup_starts_legacy_reconciliation_only_for_public_pc(self):
        with mock.patch.object(app_module, "credential_sync_record_for_worker"):
            with mock.patch.object(app_module, "start_public_pc_legacy_reconciliation") as start:
                with mock.patch.object(app_module, "start_public_pc_pending_report_flusher") as start_flusher:
                    with mock.patch("waitress.serve") as serve:
                        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "false"
                        with contextlib.redirect_stdout(io.StringIO()):
                            app_module.run_web_app(host="127.0.0.1", port=18080)
                        start.assert_not_called()
                        start_flusher.assert_not_called()

                        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
                        with contextlib.redirect_stdout(io.StringIO()):
                            app_module.run_web_app(host="127.0.0.1", port=18081)

        start.assert_called_once_with()
        start_flusher.assert_called_once_with()
        self.assertEqual(serve.call_count, 2)

    def test_credential_sync_legacy_missing_created_at_preserves_original_mtime_on_failed_ack(self):
        worker_token = "0123456789abcdef0123456789abcdef"
        os.environ["WORKER_TOKEN"] = worker_token
        path = app_module.credential_sync_relay_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "request_id": "legacy-no-created-at",
                    "status": "pending",
                    "sealed_payload": app_module.seal_credential_payload(
                        self.credential_sync_payload(),
                        worker_token,
                    ),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        original_mtime = time.time() - 600
        os.utime(path, (original_mtime, original_mtime))

        failed = self.client.post(
            "/worker/credential-sync/legacy-no-created-at/ack",
            json={"status": "failed", "detail": "temporary"},
            headers={"X-Worker-Token": worker_token},
        )
        retained = app_module.read_credential_sync_relay()

        self.assertEqual(failed.status_code, 200)
        self.assertAlmostEqual(
            datetime.fromisoformat(retained["created_at"]).timestamp(),
            original_mtime,
            delta=1,
        )
        with mock.patch.object(
            app_module.time,
            "time",
            return_value=original_mtime + app_module.credential_sync_ttl_seconds() + 1,
        ):
            self.assertEqual(app_module.read_credential_sync_relay(), {})

    def test_credential_sync_ack_does_not_delete_a_newer_pending_request(self):
        os.environ["WORKER_TOKEN"] = "worker-token"
        relay_path = app_module.credential_sync_relay_file()
        app_module.write_credential_sync_relay({"request_id": "request-a", "status": "pending"})
        original_unlink = Path.unlink
        ack_at_unlink = threading.Event()
        release_ack = threading.Event()
        writer_finished = threading.Event()
        responses: list[int] = []
        errors: list[BaseException] = []

        def gated_unlink(path, *args, **kwargs):
            if Path(path) == relay_path and threading.current_thread().name == "credential-ack":
                ack_at_unlink.set()
                if not release_ack.wait(2):
                    raise TimeoutError("test did not release credential ack")
            return original_unlink(path, *args, **kwargs)

        def ack_old_request() -> None:
            try:
                with app_module.app.test_client() as client:
                    response = client.post(
                        "/worker/credential-sync/request-a/ack",
                        headers={"X-Worker-Token": "worker-token"},
                    )
                    responses.append(response.status_code)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def write_new_request() -> None:
            try:
                app_module.write_credential_sync_relay({"request_id": "request-b", "status": "pending"})
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                writer_finished.set()

        with mock.patch.object(Path, "unlink", new=gated_unlink):
            ack_thread = threading.Thread(target=ack_old_request, name="credential-ack")
            writer_thread = threading.Thread(target=write_new_request, name="credential-writer")
            ack_thread.start()
            self.assertTrue(ack_at_unlink.wait(1))
            writer_thread.start()
            writer_was_serialized = not writer_finished.wait(0.1)
            try:
                release_ack.set()
                ack_thread.join(2)
                writer_thread.join(2)
            finally:
                release_ack.set()

        self.assertTrue(writer_was_serialized)
        self.assertFalse(ack_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses, [200])
        self.assertEqual(app_module.read_credential_sync_relay().get("request_id"), "request-b")

    def test_artifact_route_only_serves_selenium_png_and_html_diagnostics(self):
        allowed_dir = app_module.artifacts_dir / "selenium"
        allowed_dir.mkdir(parents=True, exist_ok=True)
        (allowed_dir / "diagnostic.png").write_bytes(b"png")
        (allowed_dir / "diagnostic.html").write_text("<html>diagnostic</html>", encoding="utf-8")
        sensitive_paths = [
            app_module.artifacts_dir / "credential_sync" / "pending.json",
            app_module.artifacts_dir / "tasks" / "task-1.json",
            app_module.artifacts_dir / "public_pc" / "pending_events.jsonl",
            app_module.artifacts_dir / "settings" / "vehicles.json",
            app_module.artifacts_dir / "cases" / "latest.json",
            allowed_dir / "task-summary.txt",
            allowed_dir / "disinfection_probe" / "controls.json",
        ]
        for path in sensitive_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("DUMMY_SECRET", encoding="utf-8")

        png_response = self.client.get("/artifacts/selenium/diagnostic.png")
        html_response = self.client.get("/artifacts/selenium/diagnostic.html")
        self.assertEqual(png_response.status_code, 200)
        self.assertEqual(html_response.status_code, 200)
        png_response.close()
        html_response.close()
        for path in sensitive_paths:
            relative = path.relative_to(app_module.artifacts_dir).as_posix()
            response = self.client.get(f"/artifacts/{relative}")
            self.assertEqual(response.status_code, 404, relative)
            self.assertNotIn(b"DUMMY_SECRET", response.data)

    def test_app_page_loads(self):
        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(app_module.app.config["TEMPLATES_AUTO_RELOAD"])
        body = html.unescape(response.data.decode("utf-8"))
        workspace_response = self.client.get("/static/sinposmart-workspace.css")
        self.assertEqual(workspace_response.status_code, 200)
        workspace_css = workspace_response.data.decode("utf-8")
        self.assertIn("SinpoSmart - 救護Worker", body)
        self.assertIn("救護各項設定", body)
        self.assertIn('href="/admin/vehicles"', body)
        self.assertNotIn('href="/admin/public-pc"', body)
        self.assertNotIn('href="/admin/sinposmart"', body)
        self.assertNotIn("SinpoSmart - 救災救護Worker 後台", body)
        self.assertNotIn("值班後台", body)
        self.assertNotIn('id="task-form" autocomplete="off" novalidate', body)
        self.assertIn("請先從上方案件按「帶入」", body)
        self.assertNotIn("6 : \u5433\u5b97\u8015", body)
        self.assertNotIn('placeholder="1420"', body)
        self.assertNotIn('placeholder="1505"', body)
        self.assertNotIn('placeholder="12345"', body)
        self.assertNotIn('name="mileage" inputmode="numeric" pattern="[0-9]*"', body)
        self.assertNotIn('type="text" name="case_date" inputmode="numeric" autocomplete="off" placeholder="YYYY/MM/DD"', body)
        self.assertNotIn('type="text" name="return_date" id="return-date" inputmode="numeric" autocomplete="off" placeholder="YYYY/MM/DD"', body)
        self.assertNotIn('type="date" name="case_date"', body)
        self.assertIn('const categoryPlaceholder = "\u985e\u5225\u9078\u64c7";', body)
        self.assertIn('const consumablePlaceholder = "\u8acb\u9078\u64c7";', body)
        self.assertIn("查詢案件", body)
        self.assertNotIn("查詢24小時案件", body)
        self.assertNotIn('button.textContent = "查詢中"', body)
        self.assertIn(".form-section-divider { border-top: 1px solid var(--line); margin-top: 18px; padding-top: 18px; }", body)
        self.assertNotIn('<section class="consumables form-section-divider">', body)
        self.assertNotIn('<label class="form-section-divider">消毒項目</label>', body)
        self.assertNotIn('name="case_address"', body)
        self.assertNotIn('name="work_note"', body)
        self.assertIn("const defaultConsumables = {};", body)
        self.assertIn("const baselineConsumablesLoaded = false;", body)
        self.assertIn("const selectedConsumablePackages = [];", body)
        self.assertNotIn('name="consumable_packages" id="consumable-packages-value" value=""', body)
        self.assertNotIn('name="baseline_consumables_loaded" value=""', body)
        self.assertIn('consumablePackagesValue.value = Array.from(activeConsumablePackages).join(",");', body)
        self.assertIn("selectedConsumablePackages.forEach((packageKey) => {", body)
        self.assertNotIn(" checked>", body)
        self.assertIn("main { max-width: 1080px;", body)
        self.assertIn("--text-md: 17px;", body)
        self.assertIn(".check-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));", body)
        self.assertIn(".check-item input { width: 20px; height: 20px; min-height: 20px; margin: 0; transform: scale(1.35);", body)
        self.assertIn(".disinfection-grid .check-item { min-height: 46px; padding: 8px 12px;", body)
        self.assertIn(".case-card button { min-width: 88px; min-height: 50px;", body)
        self.assertIn(".case-card,\n    .recent-card {", body)
        self.assertIn(".case-title,\n    .recent-title {\n      color: var(--ink);", body)
        self.assertIn(".recent-main { display: grid; gap: 3px; min-width: 0; }", body)
        self.assertIn(".case-meta,\n    .recent-time,\n    .recent-meta,\n    .recent-progress {\n      color: var(--muted);", body)
        self.assertIn(".workspace-page--task .case-card,\n.workspace-page--task .recent-card {", workspace_css)
        self.assertIn(".workspace-page--task .case-title,\n.workspace-page--task .recent-title {", workspace_css)
        self.assertIn(".workspace-page--task .case-meta,\n.workspace-page--task .recent-time,\n.workspace-page--task .recent-progress {", workspace_css)
        self.assertIn(".workspace-page--task .recent-main > * + * {", workspace_css)
        self.assertIn(".consumable-list { display: grid; gap: 10px; align-items: start; }", body)
        self.assertIn(".consumable-row { display: grid; grid-template-columns: 38px 142px minmax(0, 1fr) 196px 50px;", body)
        self.assertIn('<span class="consumable-index"></span>', body)
        self.assertIn("function renumberConsumables()", body)
        self.assertIn(".qty-button,", body)
        self.assertIn(".icon-button { height: 48px; min-height: 48px; padding: 0; align-self: end; line-height: 1; font-size: 21px; display: inline-flex; align-items: center; justify-content: center;", body)
        self.assertIn(".qty-button { min-width: 48px; color: var(--accent); }", body)
        self.assertIn(".icon-button { width: 50px; min-width: 50px; justify-self: start; color: var(--failed); }", body)
        self.assertIn(".form-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));", body)
        self.assertIn(".form-actions button:only-child { grid-column: 1 / -1; }", body)
        self.assertIn("repeating-linear-gradient", body)
        self.assertNotIn('id="field-summary"', body)
        self.assertIn('const formErrors = [];', body)
        self.assertIn('const requiredTaskFields = [', body)
        self.assertNotIn('data-field-name="return_time"', body)
        self.assertNotIn('data-field-name="vehicle"', body)
        self.assertNotIn('data-field-name="driver"', body)
        self.assertNotIn('data-field-name="patient_summary"', body)
        self.assertNotIn('data-field-name="mileage"', body)
        self.assertIn(".field-visual.is-pending .field-error-mark", body)
        self.assertIn(".field-visual.has-error .field-error-mark", body)
        self.assertNotIn('class="field-label-title"', body)
        self.assertNotIn('class="field-error-mark" aria-hidden="true">*</span>', body)
        self.assertNotIn("background: #fffaf0", body)
        self.assertNotIn("background: #fff7f6", body)
        self.assertNotIn(".field-visual.has-error input", body)
        self.assertNotIn("field-status-text", body)
        self.assertNotIn("data-field-status-text", body)
        self.assertNotIn("待補：", body)
        self.assertNotIn("待填：", body)
        self.assertIn('setFieldState(field.name, "pending");', body)

    def test_ems_and_disaster_entry_pages_keep_24h_lookup_without_date_field(self):
        ems_body = html.unescape(self.client.get("/app").data.decode("utf-8"))
        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        for body in (ems_body, disaster_body):
            self.assertIn('action="/cases/query"', body)
            self.assertNotIn('name="lookup_date"', body)
            self.assertNotIn('class="lookup-date"', body)

    def test_lookup_submit_locks_existing_import_buttons(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "updated_at": "2026-06-03T08:00:00",
                "cases": [{"case_id": "case-1", "title": "救護", "case_time": "1010"}],
            },
        )

        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn('<form method="post" action="/cases/import">', body)
        self.assertIn('document.querySelectorAll(\'.case-card form[action="/cases/import"] button[type="submit"]\')', body)
        self.assertIn("importButton.disabled = true;", body)

    def test_lookup_submit_locks_query_button_and_prevents_reentry(self):
        for endpoint in ("/app", "/app/disaster"):
            with self.subTest(endpoint=endpoint):
                body = html.unescape(self.client.get(endpoint).data.decode("utf-8"))
                self.assertIn('let lookupSubmitted = false;', body)
                self.assertIn('if (lookupSubmitted) {', body)
                self.assertIn('event.preventDefault();', body)
                self.assertIn('lookupForm.classList.add("is-submitting");', body)
                self.assertIn('lookupForm.setAttribute("aria-busy", "true");', body)
                self.assertIn('button.setAttribute("aria-disabled", "true");', body)
                self.assertIn('button.textContent = "查詢中…";', body)

        workspace_css = self.client.get("/static/sinposmart-workspace.css").data.decode("utf-8")
        self.assertIn(".workspace-page--task .lookup-form button:disabled", workspace_css)
        self.assertIn(".workspace-page--task .lookup-form.is-submitting button", workspace_css)

    def test_query_cases_does_not_replace_active_lookup_request(self):
        first_request = app_module.write_case_lookup_request("24h", source="test", mode="worker_queue")

        response = self.client.post("/cases/query", follow_redirects=False)

        current_request = app_module.read_case_lookup_request()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(current_request["request_id"], first_request["request_id"])
        self.assertEqual(current_request["requested_at"], first_request["requested_at"])

    def test_local_index_redirects_to_task_entry(self):
        response = self.client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/task-entry")

    def test_task_entry_shows_disaster_and_ems_cards(self):
        response = self.client.get("/task-entry")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(200, response.status_code)
        self.assertIn('<html lang="zh-Hant" data-ui="task-console">', body)
        self.assertIn("<title>SinpoSmart - 救災救護Worker</title>", body)
        self.assertIn("<h1>SinpoSmart - 救災救護Worker</h1>", body)
        self.assertIn('href="/static/sinposmart-ui.css"', body)
        self.assertNotIn("<style>", body)
        self.assertIn("救災登打", body)
        self.assertIn('href="/app/disaster"', body)
        self.assertIn("救護登打", body)
        self.assertIn('href="/app"', body)
        self.assertIn("工作紀錄、車輛里程、加油紀錄、消毒記錄、救護耗材、民力系統、NAS 資料夾建立", body)
        self.assertNotIn("entry-card-action", body)

    def test_task_entry_cards_use_same_portal_card_height_as_home(self):
        body = html.unescape(self.client.get("/task-entry").data.decode("utf-8"))

        self.assertEqual(2, body.count('class="choice-card portal-card'))

    def test_shared_ui_stylesheet_supports_touch_and_accessibility_states(self):
        response = self.client.get("/static/sinposmart-ui.css")
        try:
            css = response.data.decode("utf-8")

            self.assertEqual(200, response.status_code)
            self.assertIn("--control-height: 48px", css)
            self.assertIn(".button:active", css)
            self.assertIn(".page-chrome {\n    align-items: stretch;\n    flex-direction: column;", css)
            self.assertIn(".page-chrome__actions .button {\n    width: 100%;", css)
            self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        finally:
            response.close()

    def test_mobile_headers_keep_eyebrows_visible(self):
        css = self.client.get("/static/sinposmart-ui.css").data.decode("utf-8")
        headers = {"Host": "100.114.126.58:8080"}
        ems_settings = html.unescape(
            self.client.get("/admin/vehicles", headers=headers).data.decode("utf-8")
        )
        disaster_settings = html.unescape(
            self.client.get("/admin/disaster-vehicles", headers=headers).data.decode("utf-8")
        )

        self.assertNotIn(".app-header__eyebrow {\n    display: none;", css)
        self.assertIn('<p class="page-chrome__eyebrow">救護登打設定</p>', ems_settings)
        self.assertIn('<p class="page-chrome__eyebrow">救災登打設定</p>', disaster_settings)

    def test_home_portal_cards_use_destination_colors(self):
        response = self.client.get("/static/sinposmart-ui.css")
        try:
            css = response.data.decode("utf-8")

            self.assertEqual(200, response.status_code)
            self.assertIn(".portal-card--duty {\n  --choice-color: #7657c8;", css)
            self.assertIn(".portal-card--disaster {\n  --choice-color: var(--disaster);", css)
            self.assertIn(".portal-card--ems {\n  --choice-color: var(--ems);", css)
            self.assertIn(".portal-card--entry {\n  --choice-color: var(--brand);", css)
            self.assertIn(".portal-card--vehicle {\n  --choice-color: #b46b00;", css)
        finally:
            response.close()

    def test_workspace_stylesheet_defines_shared_apple_design_surface(self):
        response = self.client.get("/static/sinposmart-workspace.css")
        try:
            css = response.data.decode("utf-8")

            self.assertEqual(200, response.status_code)
            self.assertIn(".workspace-page main", css)
            self.assertIn("max-width: 1120px", css)
            self.assertIn(".workspace-page .page-chrome", css)
            self.assertNotIn(".workspace-page main > header", css)
            self.assertIn(".workspace-page .panel", css)
            self.assertIn("--workspace-type-body: 1rem;", css)
            self.assertIn("--workspace-type-label: .9375rem;", css)
            self.assertIn(".workspace-page h3 {\n  margin: 16px 0 8px;", css)
            self.assertIn(".workspace-page .field-label-helper {", css)
            self.assertIn(
                ".workspace-page .vehicle-card-label,\n.workspace-page .vehicle-card-title {",
                css,
            )
            self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        finally:
            response.close()

    def test_workspace_pages_use_destination_colors(self):
        response = self.client.get("/static/sinposmart-workspace.css")
        try:
            css = response.data.decode("utf-8")

            self.assertEqual(200, response.status_code)
            self.assertIn("body.workspace-page--duty {\n  --accent: #7657c8;", css)
            self.assertIn("body.workspace-page--disaster-task,\nbody.workspace-page--disaster-settings {\n  --accent: #d84a3f;", css)
            self.assertIn("--page-tint: #fff4f2;", css)
            self.assertIn("body.workspace-page--ems-task,\nbody.workspace-page--ems-settings {\n  --accent: #1677d2;", css)
            self.assertIn("--page-tint: #f1f7ff;", css)
            self.assertIn("linear-gradient(180deg, var(--page-tint) 0, #f5f5f7 34rem)", css)
            self.assertIn(
                "padding: max(24px, env(safe-area-inset-top)) max(24px, env(safe-area-inset-right)) max(56px, env(safe-area-inset-bottom)) max(24px, env(safe-area-inset-left));",
                css,
            )
        finally:
            response.close()

    def test_workspace_headers_show_service_specific_eyebrows(self):
        duty_body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))
        ems_body = html.unescape(self.client.get("/app").data.decode("utf-8"))
        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn('<p class="page-chrome__eyebrow">值班任務管理</p>', duty_body)
        self.assertIn('<p class="page-chrome__eyebrow">救護勤務登打中心</p>', ems_body)
        self.assertIn('<p class="page-chrome__eyebrow">救災勤務登打中心</p>', disaster_body)

    def test_primary_workspace_pages_share_semantic_chrome_and_entry_theme(self):
        headers = {"Host": "100.114.126.58:8080"}
        pages = (
            ("/app", "ems", "救護勤務登打中心"),
            ("/app/disaster", "disaster", "救災勤務登打中心"),
            ("/admin/vehicles", "ems", "救護登打設定"),
            ("/admin/disaster-vehicles", "disaster", "救災登打設定"),
        )

        for path, accent, eyebrow in pages:
            with self.subTest(path=path):
                body = html.unescape(self.client.get(path, headers=headers).data.decode("utf-8"))
                self.assertIn(f'<header class="page-chrome" data-page-accent="{accent}">', body)
                self.assertIn(f'<p class="page-chrome__eyebrow">{eyebrow}</p>', body)

    def test_base_pages_share_page_chrome(self):
        headers = {"Host": "100.114.126.58:8080"}

        for path in ("/", "/task-entry", "/admin/public-pc"):
            with self.subTest(path=path):
                body = html.unescape(self.client.get(path, headers=headers).data.decode("utf-8"))
                self.assertIn('<header class="page-chrome" data-page-accent="brand">', body)

    def test_page_chrome_navigation_uses_one_shared_style_and_stable_scrollbar_gutter(self):
        headers = {"Host": "100.114.126.58:8080"}
        css = self.client.get("/static/sinposmart-ui.css").data.decode("utf-8")

        self.assertIn("scrollbar-gutter: stable;", css)
        self.assertIn(".page-chrome__actions .header-navigation-button {", css)
        for path in ("/task-entry", "/admin/disaster", "/admin/ems", "/admin/sinposmart"):
            with self.subTest(path=path):
                body = html.unescape(self.client.get(path, headers=headers).data.decode("utf-8"))
                self.assertIn('class="button secondary header-navigation-button"', body)

    def test_service_filtered_admin_pages_keep_their_entry_theme(self):
        headers = {"Host": "100.114.126.58:8080"}

        for path, accent in (("/admin/disaster", "disaster"), ("/admin/ems", "ems")):
            with self.subTest(path=path):
                body = html.unescape(self.client.get(path, headers=headers).data.decode("utf-8"))
                self.assertIn(f'<header class="page-chrome" data-page-accent="{accent}">', body)

    def test_page_chrome_keeps_mobile_and_accessibility_treatments(self):
        css = self.client.get("/static/sinposmart-ui.css").data.decode("utf-8")

        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn(".page-chrome {\n    align-items: stretch;\n    flex-direction: column;", css)
        self.assertIn(".page-chrome__actions .button {\n    width: 100%;", css)
        self.assertIn(".page-chrome h1 {\n  margin: 0;\n  color: var(--page-accent);", css)
        self.assertIn("@media (prefers-reduced-transparency: reduce)", css)
        self.assertIn(".page-chrome,\n  .choice-card {", css)

    def test_workspace_header_navigation_uses_shared_apple_material_style(self):
        headers = {"Host": "100.114.126.58:8080"}
        duty_body = html.unescape(self.client.get("/admin/sinposmart", headers=headers).data.decode("utf-8"))
        ems_body = html.unescape(self.client.get("/app", headers=headers).data.decode("utf-8"))
        disaster_body = html.unescape(self.client.get("/app/disaster", headers=headers).data.decode("utf-8"))
        base_css = self.client.get("/static/sinposmart-ui.css").data.decode("utf-8")
        workspace_css = self.client.get("/static/sinposmart-workspace.css").data.decode("utf-8")

        self.assertIn('class="button secondary header-navigation-button" href="/">返回首頁</a>', duty_body)
        self.assertEqual(2, ems_body.count("header-navigation-button"))
        self.assertEqual(2, disaster_body.count("header-navigation-button"))
        self.assertIn(
            ".page-chrome .page-chrome__actions .header-navigation-button {\n"
            "  min-height: var(--control-height);\n"
            "  padding: 0 18px;\n"
            "  border-radius: 999px;",
            base_css,
        )
        self.assertIn(
            ".page-chrome .page-chrome__actions .header-navigation-button {\n"
            "    min-height: 44px;\n"
            "    padding: 0 14px;",
            base_css,
        )
        self.assertNotIn(
            ".workspace-page .page-chrome__actions .header-navigation-button {",
            workspace_css,
        )
        self.assertIn(".workspace-page .page-chrome h1 {\n  margin: 0;\n  color: var(--accent);", workspace_css)
        self.assertIn(".workspace-page .page-chrome__actions .button.secondary {", workspace_css)
        self.assertIn("backdrop-filter: blur(18px) saturate(150%);", base_css)

    def test_workspace_package_controls_are_compact_and_keep_selected_state(self):
        css_response = self.client.get("/static/sinposmart-workspace.css")
        css = css_response.data.decode("utf-8")
        css_response.close()
        self.import_case_for_form(
            {
                "case_id": "package-visual-state",
                "case_date": "2026/07/24",
                "case_time": "0915",
                "return_time": "1000",
                "address": "桃園市觀音區中山路1號",
                "personnel": ["甲"],
            }
        )
        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn(".workspace-page .package-buttons button {\n  min-height: 42px;", css)
        self.assertIn("font-size: var(--workspace-type-control-small);", css)
        self.assertIn(".workspace-page .package-buttons .package-button.is-active,", css)
        self.assertIn('data-action-text="現場待命"', disaster_body)
        self.assertNotIn('data-action-text="現場待命" aria-pressed=', disaster_body)
        self.assertNotIn("syncActionPackageButtons", disaster_body)
        self.assertIn("SinpoSmart - 值班後台", html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8")))

    def test_workspace_stylesheet_separates_date_selection_and_pads_duty_cards(self):
        response = self.client.get("/static/sinposmart-workspace.css")
        try:
            css = response.data.decode("utf-8")

            self.assertEqual(200, response.status_code)
            self.assertIn(
                ".workspace-page .day-link {\n"
                "  min-height: 40px;\n"
                "  border-color: transparent;\n"
                "  background: transparent;\n"
                "  color: var(--ink);",
                css,
            )
            self.assertIn(".workspace-page--duty .section {\n  padding: 20px;", css)
            self.assertIn(".workspace-page--duty .event-title {\n  overflow-wrap: anywhere;", css)
            self.assertIn(".workspace-page--duty .section {\n    padding: 16px;", css)
        finally:
            response.close()

    def test_workspace_secondary_controls_restore_visible_foreground_colors(self):
        response = self.client.get("/static/sinposmart-workspace.css")
        try:
            css = response.data.decode("utf-8")

            self.assertEqual(200, response.status_code)
            self.assertIn(".workspace-page .clear-button,\n.workspace-page .icon-button", css)
            self.assertIn("color: var(--failed);", css)
            self.assertIn(".workspace-page .qty-button,\n.workspace-page .clock-button", css)
            self.assertIn("color: var(--accent);", css)
        finally:
            response.close()

    def test_task_entry_only_shows_back_button_on_nas(self):
        nas_body = html.unescape(
            self.client.get("/task-entry", headers={"Host": "100.114.126.58:8080"}).data.decode("utf-8")
        )
        local_body = html.unescape(
            self.client.get("/task-entry", headers={"Host": "127.0.0.1:8090"}).data.decode("utf-8")
        )

        self.assertIn('<a class="button secondary header-navigation-button" href="/">返回首頁</a>', nas_body)
        self.assertNotIn('<a class="button secondary header-navigation-button" href="/">返回首頁</a>', local_body)

    def test_task_entry_explains_that_both_workflows_create_nas_folders(self):
        body = html.unescape(self.client.get("/task-entry").data.decode("utf-8"))

        self.assertIn("工作紀錄、車輛里程、加油紀錄、NAS 資料夾建立", body)
        self.assertIn("工作紀錄、車輛里程、加油紀錄、消毒記錄、救護耗材、民力系統、NAS 資料夾建立", body)

    def test_admin_filters_and_home_links_use_apple_control_styles(self):
        task_entry_body = html.unescape(
            self.client.get("/task-entry", headers={"Host": "100.114.126.58:8080"}).data.decode("utf-8")
        )
        disaster_body = html.unescape(self.client.get("/admin/disaster").data.decode("utf-8"))
        ems_body = html.unescape(self.client.get("/admin/ems").data.decode("utf-8"))

        for body in (task_entry_body, disaster_body, ems_body):
            self.assertIn('class="button secondary header-navigation-button"', body)

        ui_response = self.client.get("/static/sinposmart-ui.css")
        admin_response = self.client.get("/static/sinposmart-admin.css")
        ui_css = ui_response.data.decode("utf-8")
        admin_css = admin_response.data.decode("utf-8")
        self.assertIn(".page-chrome__actions .button.secondary {", ui_css)
        self.assertIn("backdrop-filter: blur(18px) saturate(150%);", ui_css)
        self.assertIn(".result-filters {\n  display: flex;\n  flex-wrap: wrap;\n  width: fit-content;", admin_css)
        self.assertIn("gap: 4px;\n  margin-bottom: 14px;\n  padding: 5px;", admin_css)
        self.assertIn(".result-filter {\n  min-height: 40px;", admin_css)
        self.assertIn("border-color: transparent;\n  border-radius: 11px;\n  background: transparent;", admin_css)
        self.assertIn(
            '.result-filters[aria-label="執行結果分類"] {\n'
            "    display: grid;\n"
            "    width: 100%;\n"
            "    grid-template-columns: repeat(2, minmax(0, 1fr));",
            admin_css,
        )
        ui_response.close()
        admin_response.close()

    def test_ems_and_disaster_pages_use_service_specific_worker_titles(self):
        ems_body = html.unescape(self.client.get("/app").data.decode("utf-8"))
        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn("<title>SinpoSmart - 救護Worker</title>", ems_body)
        self.assertIn("<h1>SinpoSmart - 救護Worker</h1>", ems_body)
        self.assertIn("<title>SinpoSmart - 救災Worker</title>", disaster_body)
        self.assertIn("<h1>SinpoSmart - 救災Worker</h1>", disaster_body)

    def test_disaster_page_uses_approved_layout_and_dynamic_vehicle_cards(self):
        self.import_case_for_form(
            {
                "case_id": "fire-layout",
                "case_date": "2026/07/22",
                "case_time": "1207",
                "return_time": "1300",
                "address": "桃園市觀音區金華路31號",
                "personnel": ["甲", "乙", "丙"],
            }
        )
        response = self.client.get("/app/disaster")
        body = html.unescape(response.data.decode("utf-8"))
        workspace_css_response = self.client.get("/static/sinposmart-workspace.css")
        workspace_css = workspace_css_response.data.decode("utf-8")
        workspace_css_response.close()

        self.assertEqual(200, response.status_code)
        self.assertIn('<html lang="zh-Hant" data-ui="task-console">', body)
        self.assertNotIn('<a class="button secondary" href="/task-entry">返回上一頁</a>', body)
        self.assertLess(body.index("案件地址"), body.index("案件時間"))
        self.assertLess(body.index("案件時間"), body.index("案件類型"))
        self.assertLess(body.index("出動車輛"), body.index("工作紀錄"))
        self.assertIn("轄內A2", body)
        self.assertIn("轄內A3", body)
        self.assertIn("新增車輛", body)
        self.assertNotIn("<h3>處理情形補充</h3>", body)
        self.assertIn('處理情形補充 <small class="field-label-helper">（可以自行輸入增減）</small>', body)
        self.assertNotIn("<h3>處理情形套餐</h3>", body)
        self.assertNotIn("服勤人員</label>", body)
        self.assertNotIn('name="duty_item"', body)
        self.assertLess(body.index("指揮官"), body.index("出動車輛"))
        self.assertLess(body.index("將建立的 NAS 資料夾名稱"), body.index("行車紀錄器分類"))
        self.assertIn('class="field-visual is-pending"', body)
        self.assertIn('class="field-error-mark" aria-hidden="true">*</span>', body)
        self.assertIn('id="client-form-errors"', body)
        self.assertNotIn("救災車設定", body)
        self.assertIn('.time-field { display: grid; grid-template-columns: minmax(0, 1fr) 104px 52px;', body)
        self.assertIn('#disaster-form .grid { column-gap: 10px; row-gap: 0; }', body)
        self.assertIn('class="clock-button" id="now-time" aria-label="帶入現在時間"', body)
        self.assertIn('data-vehicle-return-now aria-label="帶入現在時間"', body)
        self.assertIn('<strong class="vehicle-card-title" data-vehicle-title>', body)
        self.assertIn(".workspace-page .vehicle-card-title {\n  color: var(--accent);", workspace_css)
        self.assertNotIn("[data-required-message=\"請填寫處理情形\"] .field-label-title", body)
        self.assertIn("return vehicle&&driver?`${vehicle}司機:${driver}`:'';", body)
        self.assertIn("const assignmentText=assignments.length?assignments.join('、'):'尚未選擇車輛／司機／指揮官';", body)
        self.assertNotIn("帶隊官:${teamLeader}", body)
        self.assertIn("card.querySelector('[data-vehicle-return-now]').addEventListener('click'", body)

    def test_disaster_processing_controls_keep_readable_text_and_setting_page_actions_horizontal(self):
        self.import_case_for_form(
            {
                "case_id": "fire-processing-controls",
                "case_date": "2026/07/22",
                "case_time": "1207",
                "address": "桃園市觀音區",
                "personnel": ["甲"],
            }
        )

        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))
        workspace_css_response = self.client.get("/static/sinposmart-workspace.css")
        workspace_css = workspace_css_response.data.decode("utf-8")
        workspace_css_response.close()
        settings_body = html.unescape(
            self.client.get("/admin/disaster-vehicles", headers={"Host": "100.114.126.58:8080"}).data.decode("utf-8")
        )

        self.assertIn(".workspace-page .package-cluster {", workspace_css)
        self.assertIn(".workspace-page .package-group-label {", workspace_css)
        self.assertIn(".workspace-page .package-buttons button {\n  min-height: 42px;", workspace_css)
        self.assertNotIn("#disaster-form .package-buttons button", disaster_body)
        self.assertNotIn("<h3>處理情形補充</h3>", disaster_body)
        self.assertNotIn("<h3>處理情形套餐</h3>", disaster_body)
        self.assertIn('<span class="package-group-label">套餐帶入</span>', disaster_body)
        self.assertIn('class="package-buttons" aria-label="處理情形套餐"', disaster_body)
        package_label_index = disaster_body.index('<span class="package-group-label">套餐帶入</span>')
        package_buttons_index = disaster_body.index('<div class="package-buttons" aria-label="處理情形套餐">')
        self.assertLess(package_label_index, package_buttons_index)
        self.assertIn('data-required-message="請填寫處理情形"', disaster_body)
        self.assertIn("處理情形補充 <small class=\"field-label-helper\">（可以自行輸入增減）</small>", disaster_body)
        self.assertIn('class="field-label-title processing-section-title">處理情形補充', disaster_body)
        self.assertIn('<h3 class="processing-section-title">處理情形預覽</h3>', disaster_body)
        self.assertIn(".workspace-page .field-label-helper {", workspace_css)
        self.assertIn(
            ".workspace-page--disaster-task .processing-section-title {\n"
            "  font-size: var(--workspace-type-subsection);\n"
            "  font-weight: 740;\n"
            "  letter-spacing: -.01em;\n"
            "  line-height: 1.35;",
            workspace_css,
        )
        self.assertIn(
            ".workspace-page--disaster-task #action-note,\n"
            ".workspace-page--disaster-task #processing-preview {\n"
            "  font-family: inherit;\n"
            "  font-size: var(--workspace-type-body);\n"
            "  font-weight: 400;\n"
            "  line-height: 1.5;",
            workspace_css,
        )
        self.assertIn("return vehicle&&driver?`${vehicle}司機:${driver}`:'';", disaster_body)
        self.assertIn("const assignmentText=assignments.length?assignments.join('、'):'尚未選擇車輛／司機／指揮官';", disaster_body)
        self.assertIn(
            "document.getElementById('processing-preview').textContent=`1.${assignmentText}\\n2.${document.getElementById('action-note').value}`;",
            disaster_body,
        )
        self.assertIn(
            ".workspace-page .package-cluster {\n"
            "  display: flex;\n"
            "  align-items: center;\n"
            "  flex-wrap: nowrap;",
            workspace_css,
        )
        self.assertIn("  flex: 1 1 0;\n  flex-wrap: wrap;", workspace_css)
        self.assertIn(
            ".workspace-page--disaster-task .package-cluster {\n  margin-bottom: 12px;",
            workspace_css,
        )
        self.assertIn("textarea { width: 100%; min-height: 240px;", settings_body)
        self.assertIn("#action-packages { min-height: 360px; }", settings_body)
        self.assertIn('id="action-packages" name="action_packages" rows="12"', settings_body)
        self.assertIn(".delete-form { display: flex; align-items: center; justify-content: flex-end; min-height: 44px;", settings_body)
        self.assertIn("white-space: nowrap;", settings_body)

    def test_disaster_page_hides_form_until_case_is_imported(self):
        body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertNotIn('id="disaster-form"', body)
        self.assertIn("請先從上方案件按「帶入」，下方輸入欄位才會開啟。", body)

    def test_disaster_page_uses_ems_lookup_bubble_and_control_dimensions(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "detail": "已查到 6 筆 24 小時內案件，並讀取出勤人員。",
                "lookup_range": "24h",
                "cases": [{"case_id": "fire-1", "title": "火警", "case_time": "1207"}],
            },
        )

        body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn('class="lookup-message" role="status"', body)
        self.assertIn("已查到 6 筆 24 小時內案件，並讀取出勤人員。", body)
        self.assertIn("#disaster-form input,", body)
        self.assertIn("#disaster-form select { min-height: 46px; padding: 10px 12px; }", body)
        self.assertIn(".lookup-form button { min-width: 148px; }", body)
        self.assertIn(".case-card button { min-width: 88px; min-height: 50px;", body)
        self.assertIn(".case-card,\n    .recent-card {", body)
        self.assertIn(".case-title,\n    .recent-title {\n      color: var(--ink);", body)
        self.assertIn(".recent-main { display: grid; gap: 3px; min-width: 0; }", body)
        self.assertIn(".case-meta,\n    .recent-time,\n    .recent-meta,\n    .recent-progress {\n      color: var(--muted);", body)

    def test_disaster_clear_and_ems_case_data_layout_use_compact_fuel_controls(self):
        self.import_case_for_form(
            {
                "case_id": "layout-adjustments",
                "case_date": "2026/07/22",
                "case_time": "1207",
                "return_time": "1300",
                "address": "桃園市觀音區金華路31號",
                "personnel": ["甲"],
            }
        )
        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))
        self.client.post(
            "/cases/import",
            data={"return_to": "ems", "case_id": "layout-adjustments"},
            follow_redirects=False,
        )
        ems_body = html.unescape(self.client.get("/app").data.decode("utf-8"))

        self.assertIn('class="address-row"', disaster_body)
        self.assertIn('formaction="/cases/clear"', disaster_body)
        self.assertIn('name="return_to" value="disaster"', disaster_body)
        self.assertIn(">清除</button>", disaster_body)
        self.assertIn('class="check-item fuel-record-toggle full"', disaster_body)
        self.assertIn(".fuel-record-toggle input { width: 18px; height: 18px; min-height: 18px;", disaster_body)

        self.assertIn('<section class="site-card" data-site-card="work-record">', ems_body)
        self.assertIn('<div class="site-card-heading">', ems_body)
        self.assertIn('<h2>案件資料</h2>', ems_body)
        self.assertIn("案件地址", ems_body)
        self.assertNotIn("案發地址", ems_body)
        self.assertNotIn("使用耗材", ems_body)
        self.assertNotIn('<section class="site-card" data-site-card="mileage">', ems_body)
        work_record_start = ems_body.index('data-site-card="work-record"')
        first_section_end = ems_body.index("</section>", work_record_start)
        self.assertLess(ems_body.index('name="mileage"', work_record_start), first_section_end)
        self.assertLess(ems_body.index('data-last-mileage-for="vehicle"', work_record_start), first_section_end)
        self.assertIn("#task-form .fuel-record-toggle input,", ems_body)
        self.assertIn("#task-form .disinfection-grid .check-item input { width: 18px; height: 18px; min-height: 18px;", ems_body)

    def test_fuel_totals_are_displayed_without_decimal_places(self):
        self.import_case_for_form(
            {
                "case_id": "fuel-total-format",
                "case_date": "2026/07/22",
                "case_time": "1207",
                "return_time": "1300",
                "address": "桃園市觀音區",
                "personnel": ["甲"],
            }
        )

        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))
        self.client.post(
            "/cases/import",
            data={"return_to": "ems", "case_id": "fuel-total-format"},
            follow_redirects=False,
        )
        ems_body = html.unescape(self.client.get("/app").data.decode("utf-8"))

        for body in (ems_body, disaster_body):
            self.assertIn("最終總價：0", body)
            self.assertNotIn("最終總價：0.00", body)
            self.assertIn("Math.round", body)

    def test_disaster_case_lookup_and_import_return_to_disaster_page(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "updated_at": "2026-07-22T12:00:00",
                "cases": [{"case_id": "fire-1", "title": "火警", "case_time": "1207"}],
            },
        )

        page = self.client.get("/app/disaster")
        body = html.unescape(page.data.decode("utf-8"))
        self.assertIn('action="/cases/query"', body)
        self.assertIn('name="return_to" value="disaster"', body)
        self.assertIn('action="/cases/import"', body)

        query = self.client.post("/cases/query", data={"return_to": "disaster"})
        self.assertEqual("/app/disaster", query.headers["Location"])

        imported = self.client.post(
            "/cases/import",
            data={"return_to": "disaster", "case_id": "fire-1"},
        )
        self.assertEqual("/app/disaster#disaster-form", imported.headers["Location"])

    def test_ems_import_uses_case_type_and_keeps_reason_selectable(self):
        cases = (
            ("緊急救護-急病", "救護"),
            ("火災-雜草火警", "火災"),
            ("災害搶救-水域搜救", "災害搶救"),
        )
        for index, (category, summary_type) in enumerate(cases, start=1):
            with self.subTest(category=category):
                self.import_case_for_form(
                    {
                        "case_id": f"duty-item-{index}",
                        "case_date": "2026/07/25",
                        "case_time_hhmm": "1153",
                        "return_time_hhmm": "1306",
                        "address": "桃園市觀音區",
                        "category": category,
                        "reason": "急病",
                        "personnel": ["甲"],
                    }
                )

                body = html.unescape(self.client.get("/app").data.decode("utf-8"))

                self.assertIn(
                    f'<option value="{summary_type}" selected>{summary_type}</option>',
                    body,
                )
                self.assertIn('<select name="summary_type" id="summary-type"', body)
                self.assertIn('name="case_reason" id="case-reason" required', body)
                self.assertNotIn('name="case_reason" id="case-reason" disabled', body)

    def test_ems_template_remains_renderable_during_controller_restart_gap(self):
        real_render_template = app_module.render_template

        def render_with_pre_case_type_controller(template_name, **context):
            context.pop("disaster_reason_options_by_type", None)
            return real_render_template(template_name, **context)

        with mock.patch.object(app_module, "render_template", side_effect=render_with_pre_case_type_controller):
            response = self.client.get("/app")

        self.assertEqual(200, response.status_code)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertIn("const reasonOptionsByType = {", body)

    def test_duty_case_not_found_is_an_actionable_failure(self):
        self.assertEqual("找不到案件", app_module.status_label("duty_case_not_found"))
        self.assertEqual("failed", app_module.status_class("duty_case_not_found"))

    def test_disaster_form_shows_full_case_date_and_type_specific_reasons(self):
        self.import_case_for_form(
            {
                "case_id": "rescue-reasons",
                "case_date": "2026/07/22",
                "case_time_hhmm": "1207",
                "return_time_hhmm": "1300",
                "address": "桃園市觀音區",
                "category": "災害搶救-輸電線路災害",
                "summary_type": "災害搶救",
                "reason": "輸電線路災害",
                "personnel": ["甲"],
            }
        )

        body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn('name="case_date" inputmode="numeric" autocomplete="off" placeholder="YYYY/MM/DD" value="2026/07/22"', body)
        self.assertIn('type="text" name="return_date" id="return-date" inputmode="numeric" autocomplete="off" placeholder="YYYY/MM/DD" value="2026/07/22"', body)
        self.assertIn('<option value="救護">救護</option>', body)
        summary_select = body[body.index('<select name="summary_type"'):]
        summary_select = summary_select[:summary_select.index("</select>")]
        self.assertNotIn('<option value="其他">其他</option>', summary_select)
        self.assertIn('<option value="輸電線路災害" selected>輸電線路災害</option>', body)
        self.assertIn('<option value="溺水">溺水</option>', body)
        self.assertIn('<option value="公用氣體及油類管路災害">公用氣體及油類管路災害</option>', body)
        self.assertIn("summaryType.addEventListener('change'", body)

    def test_disaster_import_normalizes_ems_case_type_and_keeps_lookup_reason(self):
        self.import_case_for_form(
            {
                "case_id": "ems-in-disaster",
                "case_date": "2026/07/22",
                "case_time_hhmm": "1207",
                "return_time_hhmm": "1300",
                "address": "桃園市觀音區",
                "category": "緊急救護-特殊救護事由",
                "reason": "特殊救護事由",
                "personnel": ["甲"],
            }
        )

        body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn('<option value="救護" selected>救護</option>', body)
        self.assertIn('<option value="特殊救護事由" selected>特殊救護事由</option>', body)

    def test_other_false_alarm_import_is_classified_as_disaster_rescue(self):
        self.import_case_for_form(
            {
                "case_id": "other-false-alarm",
                "case_date": "2026/07/22",
                "case_time_hhmm": "1207",
                "return_time_hhmm": "1300",
                "address": "桃園市觀音區",
                "category": "其他-誤(謊)報",
                "reason": "誤(謊)報",
                "personnel": ["甲"],
            }
        )

        body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn('<option value="災害搶救" selected>災害搶救</option>', body)
        self.assertIn('<option value="誤(謊)報" selected>誤(謊)報</option>', body)

    def test_disaster_false_alarm_uses_false_alarm_recorder_folder(self):
        self.import_case_for_form(
            {
                "case_id": "fire-false-alarm",
                "case_date": "2026/07/22",
                "case_time_hhmm": "1207",
                "return_time_hhmm": "1300",
                "address": "桃園市觀音區",
                "category": "火災-誤(謊)報",
                "summary_type": "火災",
                "reason": "誤(謊)報",
                "personnel": ["甲"],
            }
        )

        body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn('<option value="誤報">誤報</option>', body)
        self.assertIn("const localFalseAlarm=summaryType.value==='火災'&&['誤報','誤(謊)報'].includes(reason.value);", body)
        self.assertIn("const falseAlarmCategories=new Set(['轄內其他案件','支援他轄']);", body)
        self.assertIn("if(localFalseAlarm&&!falseAlarmCategories.has(category.value)) category.value='轄內其他案件';", body)
        self.assertIn("const localFalseAlarmOther=localFalseAlarm&&category.value==='轄內其他案件';", body)
        self.assertIn("subcategoryInput.value='誤報';", body)
        self.assertNotIn("\n      category.disabled=true;", body)
        self.assertIn('<select name="team_leader">', body)

    def test_disaster_false_alarm_rejects_categories_other_than_local_other_or_support(self):
        task_request = AmbulanceReturnRequest(
            task_id="false-alarm-category",
            created_at=datetime.now(),
            raw_text="",
            service_type="disaster",
            summary_type="火災",
            case_reason="誤(謊)報",
            recorder_category="轄內A3",
        )

        errors = app_module.validate_disaster_task_form(task_request)

        self.assertIn("誤(謊)報僅可選擇轄內其他案件或支援他轄", errors)

    def test_disaster_task_accepts_rescue_and_ems_case_types(self):
        base = [
            ("case_date", "2026/07/22"), ("case_time", "1207"),
            ("return_time", "1300"), ("case_address", "桃園市觀音區"),
            ("personnel", "甲"), ("commander", "甲"), ("action_note", "現場處理"),
            ("recorder_category", "轄內A3"),
            ("vehicle", "新坡11"), ("driver", "甲"), ("vehicle_return_time", "1300"), ("mileage", "100"),
        ]
        for case_id, summary_type, reason in (("RESCUE-TYPE", "災害搶救", "輸電線路災害"), ("EMS-TYPE", "救護", "特殊救護事由")):
            with self.subTest(summary_type=summary_type):
                data = MultiDict(base + [("case_id", case_id), ("summary_type", summary_type), ("case_reason", reason)])
                with mock.patch.object(app_module, "ensure_disaster_media_folders", return_value=[]):
                    response = self.client.post("/tasks/disaster", data=data, follow_redirects=False)
                self.assertEqual(302, response.status_code)

    def test_disaster_vehicle_settings_page_saves_recorder_code(self):
        nas_headers = {"Host": "100.114.126.58:8080"}
        page = self.client.get("/admin/disaster-vehicles", headers=nas_headers)
        body = html.unescape(page.data.decode("utf-8"))
        self.assertIn("救災各項設定", body)
        self.assertIn("行車紀錄器車號", body)
        self.assertNotIn('name="vehicle_type"', body)
        self.assertNotIn("現有車輛皆為內建；建立自訂車輛後才可刪除。", body)
        for retired_vehicle in ("新坡16", "新坡91", "新坡92", "新坡93"):
            self.assertNotIn(retired_vehicle, body)
        self.assertIn("main { max-width: 960px;", body)
        self.assertIn("repeating-linear-gradient", body)
        self.assertIn('class="vehicle-row header-row"', body)
        self.assertIn('class="button secondary header-navigation-button"', body)

        response = self.client.post(
            "/admin/disaster-vehicles",
            data={
                "label": "新坡71",
                "ppe_name": "FIRE-71",
                "recorder_code": "CAM71",
                "vehicle_type": "內建",
            },
            headers=nas_headers,
        )
        self.assertEqual(200, response.status_code)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertIn("新坡71", body)
        self.assertIn("CAM71", body)
        self.assertIn('action="/admin/disaster-vehicles/delete"', body)
        settings = json.loads(
            (Path(self.tmp.name) / "settings" / "disaster_vehicles.json").read_text(encoding="utf-8")
        )
        saved_vehicle = next(item for item in settings["vehicles"] if item["label"] == "新坡71")
        self.assertEqual("自訂", saved_vehicle["vehicle_type"])

        self.import_case_for_form(
            {
                "case_id": "fire-vehicle-settings",
                "case_date": "2026/07/22",
                "case_time": "1207",
                "address": "桃園市觀音區",
                "personnel": ["甲"],
            }
        )
        disaster_page = self.client.get("/app/disaster")
        self.assertIn("新坡71", html.unescape(disaster_page.data.decode("utf-8")))

    def test_vehicle_settings_pages_share_columns_and_fixed_vehicle_card_height(self):
        nas_headers = {"Host": "100.114.126.58:8080"}
        ems_body = html.unescape(self.client.get("/admin/vehicles", headers=nas_headers).data.decode("utf-8"))
        disaster_body = html.unescape(
            self.client.get("/admin/disaster-vehicles", headers=nas_headers).data.decode("utf-8")
        )
        vehicle_headers = [
            "車輛代號",
            "車牌號碼",
            "行車紀錄器車號",
            "最新里程",
            "同步狀態",
            "操作",
        ]

        self.assertIn("救護登打設定", ems_body)
        self.assertIn("救護各項設定", ems_body)
        self.assertIn("救災登打設定", disaster_body)
        self.assertIn("救災各項設定", disaster_body)
        for body in (ems_body, disaster_body):
            self.assertNotIn('name="vehicle_type"', body)
            self.assertNotIn("現有車輛皆為內建；建立自訂車輛後才可刪除。", body)
            self.assertIn("--vehicle-row-height: 68px;", body)
            self.assertIn("--vehicle-card-height: 300px;", body)
            self.assertIn(".vehicle-row:not(.header-row) { height: var(--vehicle-row-height); }", body)
            self.assertIn("grid-template-rows: repeat(3, 68px) 52px;", body)
            self.assertIn('"action action";', body)
            self.assertIn(".vehicle-sync .vehicle-value {", body)
            self.assertIn(".mileage-query-message {", body)
            self.assertNotIn('data-field="類型"', body)
            self.assertNotIn("vehicle-kind", body)
            for heading in vehicle_headers:
                self.assertIn(heading, body)
                self.assertIn(f'data-field="{heading}"', body)
        self.assertNotIn('action="/admin/vehicles/delete"', ems_body)
        self.assertNotIn('action="/admin/disaster-vehicles/delete"', disaster_body)

    def test_remote_current_vehicles_remain_built_in_when_legacy_snapshot_marks_them_custom(self):
        settings = app_module.normalize_remote_vehicle_settings(
            {
                "ems_vehicles": [
                    {"label": "新坡91", "ppe_name": "BGV-2310", "vehicle_type": "自訂"},
                    {"label": "自訂救護車", "ppe_name": "CUSTOM-EMS", "vehicle_type": "自訂"},
                ],
                "disaster_vehicles": [
                    {
                        "label": "新坡15",
                        "ppe_name": "FIRE-15",
                        "recorder_code": "15",
                        "vehicle_type": "自訂",
                    }
                ],
                "disaster_action_packages": ["現場待命"],
            }
        )

        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual("內建", settings["ems_vehicles"][0]["vehicle_type"])
        self.assertFalse(settings["ems_vehicles"][0]["is_custom"])
        self.assertEqual("自訂", settings["ems_vehicles"][1]["vehicle_type"])
        self.assertEqual("內建", settings["disaster_vehicles"][0]["vehicle_type"])

    def test_record_folder_preview_uses_shared_ems_and_disaster_rules(self):
        blank_disaster = self.client.post(
            "/api/record-folder-preview",
            data={"service_type": "disaster"},
        )
        self.assertEqual(200, blank_disaster.status_code)
        self.assertEqual([], blank_disaster.get_json()["paths"])
        self.assertIn("請先完成案件與車輛資料", blank_disaster.get_json()["detail"])

        ems = self.client.post(
            "/api/record-folder-preview",
            data={
                "service_type": "ems",
                "case_date": "2026/07/22",
                "case_time": "1207",
                "vehicle": "新坡91",
            },
        )
        self.assertEqual(["2026/7月/07221207-91"], ems.get_json()["paths"])

        disaster = self.client.post(
            "/api/record-folder-preview",
            data=MultiDict(
                [
                    ("service_type", "disaster"),
                    ("case_date", "2026/07/21"),
                    ("case_time", "1207"),
                    ("case_address", "桃園市觀音區金華路31號"),
                    ("summary_type", "火災"),
                    ("case_reason", "一般(集合)住宅"),
                    ("recorder_category", "轄內A3"),
                    ("vehicle", "新坡11"),
                    ("driver", "甲"),
                ]
            ),
        )
        self.assertEqual(
            ["115年/轄內A3/202607211207桃園市觀音區金華路31號(住宅火警)-11"],
            disaster.get_json()["paths"],
        )
        self.assertEqual(disaster.get_json()["paths"], disaster.get_json()["recorder_paths"])
        self.assertEqual([], disaster.get_json()["firecam_paths"])

        false_alarm = self.client.post(
            "/api/record-folder-preview",
            data=MultiDict(
                [
                    ("service_type", "disaster"),
                    ("case_date", "2026/07/21"),
                    ("case_time", "1207"),
                    ("case_address", "桃園市觀音區金華路31號"),
                    ("summary_type", "火災"),
                    ("case_reason", "誤(謊)報"),
                    ("recorder_category", "轄內其他案件"),
                    ("recorder_subcategory", "其他"),
                    ("vehicle", "新坡11"),
                    ("driver", "甲"),
                ]
            ),
        )
        self.assertEqual(
            ["115年/轄內其他案件/誤報/202607211207桃園市觀音區金華路31號(誤報)-11"],
            false_alarm.get_json()["paths"],
        )

        false_alarm_support = self.client.post(
            "/api/record-folder-preview",
            data=MultiDict(
                [
                    ("service_type", "disaster"),
                    ("case_date", "2026/07/21"),
                    ("case_time", "1207"),
                    ("case_address", "桃園市觀音區金華路31號"),
                    ("summary_type", "火災"),
                    ("case_reason", "誤(謊)報"),
                    ("recorder_category", "支援他轄"),
                    ("vehicle", "新坡11"),
                    ("driver", "甲"),
                ]
            ),
        )
        self.assertEqual(
            ["115年/支援他轄/202607211207桃園市觀音區金華路31號(誤報)-11"],
            false_alarm_support.get_json()["paths"],
        )

        missing_category = self.client.post(
            "/api/record-folder-preview",
            data=MultiDict(
                [
                    ("service_type", "disaster"),
                    ("case_date", "2026/07/21"),
                    ("case_time", "1207"),
                    ("case_address", "桃園市觀音區金華路31號"),
                    ("case_reason", "火災"),
                    ("vehicle", "新坡11"),
                    ("driver", "甲"),
                ]
            ),
        )
        self.assertEqual(200, missing_category.status_code)
        self.assertEqual([], missing_category.get_json()["paths"])
        self.assertEqual("請選擇行車紀錄器分類。", missing_category.get_json()["detail"])

    def test_disaster_task_detail_hides_ems_cards_and_shows_commander_and_processing(self):
        task = AmbulanceReturnRequest(
            task_id="disaster-detail",
            created_at=datetime.now(),
            raw_text="",
            service_type="disaster",
            case_date="2026/07/22",
            case_time="1207",
            return_time="1300",
            case_address="桃園市觀音區",
            case_reason="雜草(含廢棄物、墓地)",
            commander="指揮官甲",
            action_note="現場待命",
            vehicle_entries=[{"vehicle": "新坡11", "driver": "司機甲", "mileage": "100", "return_time": "1300"}],
        )
        self.store.create(task)

        body = html.unescape(self.client.get("/tasks/disaster-detail").data.decode("utf-8"))

        self.assertIn("指揮官甲", body)
        self.assertIn("現場待命", body)
        self.assertNotIn("<h3>耗材</h3>", body)
        self.assertNotIn("<h3>消毒</h3>", body)
        self.assertIn('href="/app/disaster"', body)
        self.assertNotIn("返回編輯", body)

        edit_get = self.client.get("/tasks/disaster-detail/edit")
        edit_post = self.client.post("/tasks/disaster-detail/edit", data={})
        self.assertEqual(409, edit_get.status_code)
        self.assertEqual(409, edit_post.status_code)
        self.assertIn("救災案件不使用救護編輯頁", edit_get.get_data(as_text=True))

    def test_ems_form_shows_folder_preview_and_live_fuel_total_without_changing_entry_route(self):
        self.import_case_for_form(
            {
                "case_id": "case-preview",
                "case_date": "2026/07/22",
                "case_time": "1207",
                "address": "桃園市觀音區",
                "personnel": ["甲"],
            }
        )
        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        for site_key in ("work-record", "mileage", "fuel", "consumables", "disinfection"):
            self.assertIn(f'data-site-card="{site_key}"', body)
        self.assertIn('id="record-folder-preview"', body)
        self.assertIn('class="site-card record-folder-preview"', body)
        self.assertIn('data-fuel-total', body)
        self.assertIn('/api/record-folder-preview', body)
        self.assertIn('action="/tasks"', body)
        self.assertIn('formAction.endsWith("/cases/clear")', body)
        self.assertIn('submitButton.disabled = true', body)

    def test_ems_and_disaster_time_inputs_show_hhmm_hint(self):
        self.import_case_for_form(
            {
                "case_id": "case-time-hints",
                "case_date": "2026/07/22",
                "case_time": "1207",
                "address": "獢?撣??喳?",
                "category": "緊急救護-急病",
                "personnel": ["A", "B", "C", "D"],
            }
        )

        ems_body = html.unescape(self.client.get("/app").data.decode("utf-8"))
        for field_name in ("case_time", "return_time", "fuel_time", "return_time_2", "fuel_time_2"):
            self.assertRegex(ems_body, rf'name="{field_name}"[^>]*placeholder="hhmm"')

        self.client.post(
            "/cases/import",
            data={"case_id": "case-time-hints", "return_to": "disaster"},
            follow_redirects=False,
        )
        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))
        for field_name in ("case_time", "return_time", "vehicle_return_time"):
            self.assertRegex(disaster_body, rf'name="{field_name}"[^>]*placeholder="hhmm"')

    def test_nas_index_shows_entry_buttons_only(self):
        response = self.client.get("/", headers={"Host": "100.114.126.58:8080"})

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertIn('<html lang="zh-Hant" data-ui="task-console">', body)
        self.assertIn("<title>SinpoSmart</title>", body)
        self.assertIn("<h1>SinpoSmart</h1>", body)
        self.assertIn('href="/static/sinposmart-ui.css"', body)
        self.assertIn('class="portal-grid"', body)
        self.assertEqual(5, body.count('class="choice-card portal-card'))
        self.assertNotIn("<style>", body)
        self.assertNotIn("救護返隊小幫手", body)
        self.assertIn("值班後台", body)
        self.assertIn('href="/admin/sinposmart"', body)
        self.assertIn("救災後台", body)
        self.assertIn('href="/admin/disaster"', body)
        self.assertIn("救護後台", body)
        self.assertIn('href="/admin/ems"', body)
        self.assertNotIn('href="/admin/public-pc"', body)
        self.assertIn("救災救護登打", body)
        self.assertIn('href="/task-entry"', body)
        self.assertIn("車輛損害管理", body)
        self.assertIn(
            'href="https://sinposmart-vehicle-damage-portal.sinpo666.workers.dev"',
            body,
        )
        self.assertIn('target="_blank"', body)
        self.assertIn('rel="noopener noreferrer"', body)
        self.assertNotIn("救護各項設定", body)
        self.assertNotIn("查詢案件", body)
        self.assertIn("portal-card--duty", body)
        self.assertIn("portal-card--disaster", body)
        self.assertIn("portal-card--ems", body)
        self.assertIn("portal-card--entry", body)
        self.assertIn("portal-card--vehicle", body)

    def test_vehicle_settings_links_are_shown_on_nas_pages(self):
        response = self.client.get("/app", headers={"Host": "100.114.126.58:8080"})
        disaster_response = self.client.get("/app/disaster", headers={"Host": "100.114.126.58:8080"})

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        disaster_body = html.unescape(disaster_response.data.decode("utf-8"))
        self.assertIn("SinpoSmart - 救護Worker", body)
        self.assertIn("救護各項設定", body)
        self.assertIn('href="/admin/vehicles"', body)
        self.assertIn("救災各項設定", disaster_body)
        self.assertIn('href="/admin/disaster-vehicles"', disaster_body)

    def test_nas_task_headers_keep_back_link_before_vehicle_settings(self):
        headers = {"Host": "100.114.126.58:8080"}
        ems_body = html.unescape(self.client.get("/app", headers=headers).data.decode("utf-8"))
        disaster_body = html.unescape(
            self.client.get("/app/disaster", headers=headers).data.decode("utf-8")
        )

        self.assertLess(ems_body.index("返回上一頁"), ems_body.index("救護各項設定"))
        self.assertLess(disaster_body.index("返回上一頁"), disaster_body.index("救災各項設定"))

    def test_local_task_forms_show_back_navigation_and_vehicle_settings(self):
        headers = {"Host": "127.0.0.1:8090"}
        entry_body = html.unescape(self.client.get("/task-entry", headers=headers).data.decode("utf-8"))
        ems_body = html.unescape(self.client.get("/app", headers=headers).data.decode("utf-8"))
        disaster_body = html.unescape(
            self.client.get("/app/disaster", headers=headers).data.decode("utf-8")
        )

        self.assertNotIn("返回首頁", entry_body)
        for body in (ems_body, disaster_body):
            self.assertIn('href="/task-entry">返回上一頁</a>', body)
        self.assertIn("救護各項設定", ems_body)
        self.assertIn('href="/admin/vehicles"', ems_body)
        self.assertIn("救災各項設定", disaster_body)
        self.assertIn('href="/admin/disaster-vehicles"', disaster_body)

    def test_local_vehicle_settings_routes_are_available_for_synced_edits(self):
        for path in ("/admin/vehicles", "/admin/disaster-vehicles"):
            with self.subTest(path=path):
                self.assertEqual(200, self.client.get(path).status_code)
                self.assertEqual(400, self.client.post(path, data={}).status_code)

    def test_worker_vehicle_settings_api_requires_token_and_returns_both_services(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        forbidden = self.client.get("/worker/vehicle-settings")
        response = self.client.get(
            "/worker/vehicle-settings",
            headers={"X-Worker-Token": "test-token"},
        )

        self.assertEqual(403, forbidden.status_code)
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("新坡91", [item["label"] for item in payload["ems_vehicles"]])
        self.assertIn("新坡11", [item["label"] for item in payload["disaster_vehicles"]])
        self.assertIn("現場待命", payload["disaster_action_packages"])
        self.assertRegex(payload["revision"], r"^[0-9a-f]{64}$")

    def test_worker_vehicle_settings_update_requires_matching_revision(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        current = self.client.get("/worker/vehicle-settings", headers=headers).get_json()

        updated = self.client.post(
            "/worker/vehicle-settings",
            headers=headers,
            json={
                "operation": "save_ems_vehicle",
                "label": "新坡96",
                "ppe_name": "BPE-5960",
                "revision": current["revision"],
            },
        )
        stale = self.client.post(
            "/worker/vehicle-settings",
            headers=headers,
            json={
                "operation": "save_ems_vehicle",
                "label": "新坡97",
                "ppe_name": "BPE-5970",
                "revision": current["revision"],
            },
        )

        self.assertEqual(200, updated.status_code)
        updated_payload = updated.get_json()
        self.assertIn("新坡96", [item["label"] for item in updated_payload["ems_vehicles"]])
        self.assertNotEqual(current["revision"], updated_payload["revision"])
        self.assertEqual(409, stale.status_code)
        self.assertEqual("revision_conflict", stale.get_json()["error"])

    def test_local_vehicle_setting_edit_writes_to_nas_and_keeps_nas_snapshot(self):
        os.environ["WORKER_SERVER_URL"] = "http://nas.test:8080"
        os.environ["WORKER_TOKEN"] = "test-token"
        remote_settings = {
            "ems_vehicles": [{"label": "NAS救護91", "ppe_name": "NAS-EMS", "vehicle_type": "內建"}],
            "disaster_vehicles": [
                {"label": "NAS救災11", "ppe_name": "NAS-FIRE", "recorder_code": "NAS11", "vehicle_type": "內建"}
            ],
            "disaster_action_packages": ["NAS 現場待命"],
        }
        remote_settings["revision"] = app_module.vehicle_settings_revision(remote_settings)
        remote_payload = {"ok": True, **remote_settings}
        fake_response = mock.MagicMock()
        fake_response.__enter__.return_value.read.return_value = json.dumps(remote_payload).encode("utf-8")

        with mock.patch.object(app_module.urllib.request, "urlopen", return_value=fake_response) as urlopen:
            response = self.client.post(
                "/admin/vehicles",
                headers={"Host": "127.0.0.1:8090"},
                data={
                    "label": "NAS救護91",
                    "ppe_name": "NAS-EMS",
                    "settings_revision": remote_settings["revision"],
                },
            )

        self.assertEqual(200, response.status_code)
        post_request = next(call.args[0] for call in urlopen.call_args_list if call.args[0].method == "POST")
        self.assertEqual("http://nas.test:8080/worker/vehicle-settings", post_request.full_url)
        self.assertEqual("test-token", post_request.get_header("X-worker-token"))
        self.assertEqual(
            {
                "operation": "save_ems_vehicle",
                "label": "NAS救護91",
                "ppe_name": "NAS-EMS",
                "vehicle_type": "自訂",
                "revision": remote_settings["revision"],
            },
            json.loads(post_request.data.decode("utf-8")),
        )
        self.assertIn("NAS 即時設定", html.unescape(response.data.decode("utf-8")))
        self.assertFalse(app_module.pending_vehicle_settings_commands_path().exists())

    def test_local_vehicle_setting_offline_change_waits_then_syncs_without_overwrite(self):
        os.environ["WORKER_SERVER_URL"] = "http://nas.test:8080"
        os.environ["WORKER_TOKEN"] = "test-token"
        initial_settings = {
            "ems_vehicles": [{"label": "NAS救護91", "ppe_name": "NAS-EMS", "vehicle_type": "內建"}],
            "disaster_vehicles": [
                {"label": "NAS救災11", "ppe_name": "NAS-FIRE", "recorder_code": "NAS11", "vehicle_type": "內建"}
            ],
            "disaster_action_packages": ["NAS 現場待命"],
        }
        initial_settings["revision"] = app_module.vehicle_settings_revision(initial_settings)
        app_module.cache_nas_vehicle_settings(initial_settings)

        with mock.patch.object(
            app_module.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            queued = self.client.post(
                "/admin/vehicles",
                headers={"Host": "127.0.0.1:8090"},
                data={
                    "label": "NAS救護92",
                    "ppe_name": "NAS-EMS-2",
                    "settings_revision": initial_settings["revision"],
                },
            )

        pending = app_module.read_pending_vehicle_settings_commands()
        expected_settings = app_module.apply_pending_vehicle_settings_command(initial_settings, pending[0])
        get_response = mock.MagicMock()
        get_response.__enter__.return_value.read.return_value = json.dumps({"ok": True, **initial_settings}).encode("utf-8")
        post_response = mock.MagicMock()
        post_response.__enter__.return_value.read.return_value = json.dumps({"ok": True, **expected_settings}).encode("utf-8")
        roster_response = mock.MagicMock()
        roster_response.__enter__.return_value.read.return_value = json.dumps(
            {"ok": True, "status": "civilpower_roster_failed", "members": []}
        ).encode("utf-8")
        with mock.patch.object(
            app_module.urllib.request,
            "urlopen",
            side_effect=[get_response, post_response, roster_response],
        ) as urlopen:
            recovered = self.client.get("/admin/vehicles", headers={"Host": "127.0.0.1:8090"})

        self.assertEqual(200, queued.status_code)
        self.assertIn("設定已暫存", html.unescape(queued.data.decode("utf-8")))
        self.assertEqual("pending", pending[0]["state"])
        self.assertEqual(200, recovered.status_code)
        self.assertIn("已同步 1 筆公務電腦暫存設定到 NAS", html.unescape(recovered.data.decode("utf-8")))
        self.assertEqual([], app_module.read_pending_vehicle_settings_commands())
        post_request = next(call.args[0] for call in urlopen.call_args_list if call.args[0].method == "POST")
        self.assertEqual(initial_settings["revision"], json.loads(post_request.data.decode("utf-8"))["revision"])

    def test_disaster_task_defaults_to_newpo_11_and_15_and_uses_nas_action_packages(self):
        self.import_case_for_form(
            {
                "case_id": "FIRE-DEFAULTS",
                "case_date": "2026/07/24",
                "case_time": "1437",
                "address": "桃園市觀音區廣大路102號",
                "personnel": ["甲", "乙"],
                "summary_type": "火災",
            }
        )
        app_module.save_disaster_action_packages(["先鋒搶救", "現場待命"], Path(self.tmp.name))

        response = self.client.get("/app/disaster")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn('option value="新坡11" selected', body)
        self.assertIn('option value="新坡15" selected', body)
        self.assertIn('data-action-text="先鋒搶救"', body)
        self.assertIn('name="firecam_person" value="甲"', body)

    def test_disaster_task_offers_current_vehicles_and_personnel_as_separate_packages(self):
        self.import_case_for_form(
            {
                "case_id": "FIRE-CURRENT-ASSIGNMENTS",
                "case_date": "2026/07/24",
                "case_time": "1437",
                "address": "桃園市觀音區廣大路102號",
                "personnel": ["曾彥綸", "王治任"],
                "summary_type": "火災",
                "vehicle_entries": [
                    {"vehicle": "新坡11", "driver": "曾彥綸"},
                    {"vehicle": "新坡15", "driver": "王治任"},
                ],
            }
        )

        response = self.client.get("/app/disaster")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn('id="current-vehicle-packages"', body)
        self.assertIn('data-current-vehicle-package="新坡11"', body)
        self.assertIn('data-current-vehicle-package="新坡15"', body)
        self.assertIn('id="current-personnel-packages"', body)
        self.assertIn('data-current-personnel-package="曾彥綸"', body)
        self.assertIn('data-current-personnel-package="王治任"', body)
        self.assertNotIn('data-current-assignment-package=', body)

    def test_disaster_task_renders_compact_horizontal_firecam_selector(self):
        self.import_case_for_form(
            {
                "case_id": "FIRE-FIRECAM-SELECTOR",
                "case_date": "2026/07/24",
                "case_time": "1437",
                "address": "桃園市觀音區廣大路102號",
                "personnel": ["曾彥綸", "王治任"],
                "summary_type": "火災",
            }
        )

        response = self.client.get("/app/disaster")
        body = html.unescape(response.data.decode("utf-8"))
        workspace_css_response = self.client.get("/static/sinposmart-workspace.css")
        workspace_css = workspace_css_response.data.decode("utf-8")
        workspace_css_response.close()

        self.assertIn('<div class="full firecam-selector">', body)
        self.assertNotIn('<fieldset class="full firecam-selector">', body)
        self.assertIn('.firecam-selector .check-grid { gap: 8px; margin: 0; }', body)
        self.assertIn('#disaster-form .firecam-selector .check-item input { width: 18px;', body)
        self.assertIn('.firecam-selector .check-item span { display: inline; white-space: nowrap; }', body)
        self.assertIn('<span class="field-section-label firecam-selector-title">Firecam（可複選）</span>', body)
        self.assertIn(".workspace-page .field-section-label {", workspace_css)
        self.assertNotIn('.firecam-selector-title { font-size:', body)
        self.assertIn('.firecam-selector .check-item:has(input:checked)', body)
        self.assertNotIn('.firecam-selector .check-item:has(input:checked) { border-color: var(--accent); background: var(--accent-soft); color: #8f4436; }', body)
        self.assertIn('data-folder-recorder-group', body)
        self.assertIn('data-folder-firecam-group', body)

    def test_disaster_task_shows_last_mileage_for_each_selected_vehicle(self):
        task = AmbulanceReturnRequest(
            task_id="disaster-last-mileage",
            created_at=datetime.now(),
            raw_text="",
            service_type="disaster",
            case_id="FIRE-LAST-MILEAGE",
            case_date="20260724",
            case_time="1437",
            return_time="1522",
            case_address="桃園市觀音區廣大路102號",
            vehicle_entries=[
                {"vehicle": "新坡11", "driver": "甲", "mileage": "12345", "return_time": "1522"}
            ],
        )
        self.store.create(task)
        self.import_case_for_form(
            {
                "case_id": "FIRE-NEXT-MILEAGE",
                "case_date": "2026/07/24",
                "case_time": "1600",
                "address": "桃園市觀音區",
                "personnel": ["甲", "乙"],
                "summary_type": "火災",
            }
        )

        response = self.client.get("/app/disaster")

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        compact_body = body.replace(" ", "")
        self.assertIn("上次該車輛登打的里程", body)
        self.assertIn('data-last-mileage-for="vehicle"', body)
        self.assertIn("#disaster-form .field-visual .mileage-hint { margin-top: 8px; min-height: 42px; }", body)
        self.assertNotIn("mileage-hint-panel", body)
        self.assertIn("border: 1px solid var(--line);", body)
        self.assertIn("min-height: 46px;", body)
        self.assertIn(
            json.dumps({"新坡11": "12345"}, ensure_ascii=True, separators=(",", ":")),
            compact_body,
        )

    def test_entry_forms_block_mileage_changes_outside_previous_300km_range(self):
        self.import_case_for_form(
            {
                "case_id": "EMS-MILEAGE-GUARD",
                "case_date": "2026/08/22",
                "case_time": "0900",
                "address": "桃園市觀音區",
                "personnel": ["甲"],
                "summary_type": "緊急救護",
            }
        )
        ems_body = html.unescape(self.client.get("/app").data.decode("utf-8"))
        self.import_case_for_form(
            {
                "case_id": "DISASTER-MILEAGE-GUARD",
                "case_date": "2026/08/22",
                "case_time": "0900",
                "address": "桃園市觀音區",
                "personnel": ["甲"],
                "summary_type": "火災",
            }
        )
        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        for body in (ems_body, disaster_body):
            self.assertIn("const maxVehicleMileageChangeKm = 300;", body)
            self.assertIn("function mileageChangeErrors", body)
            self.assertIn("不能小於上次登打的里程", body)
            self.assertIn("與上次登打相差不可超過", body)
            self.assertNotIn("可輸入", body)

    def test_disaster_case_import_derives_address_from_selected_case_description(self):
        self.import_case_for_form(
            {
                "case_id": "FIRE-ADDRESS",
                "case_date": "2026/07/24",
                "case_time": "1437",
                "address": "",
                "description": "119案件\n地點：桃園市觀音區廣大路102號",
                "personnel": ["甲"],
                "summary_type": "火災",
            }
        )

        response = self.client.get("/app/disaster")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn('name="case_address" value="桃園市觀音區廣大路102號"', body)

    def test_local_task_pages_fetch_vehicle_settings_from_nas_each_time(self):
        os.environ["WORKER_SERVER_URL"] = "http://nas.test:8080"
        os.environ["WORKER_TOKEN"] = "test-token"
        remote_payload = {
            "ok": True,
            "ems_vehicles": [{"label": "NAS救護91", "ppe_name": "NAS-EMS"}],
            "disaster_vehicles": [
                {"label": "NAS救災11", "ppe_name": "NAS-FIRE", "recorder_code": "NAS11"}
            ],
            "disaster_action_packages": ["NAS 現場待命"],
        }
        fake_response = mock.MagicMock()
        fake_response.__enter__.return_value.read.return_value = json.dumps(remote_payload).encode("utf-8")

        self.import_case_for_form(
            {
                "case_id": "remote-ems-settings",
                "case_date": "2026/07/22",
                "case_time": "1207",
                "address": "桃園市觀音區",
                "personnel": ["甲"],
            }
        )
        with mock.patch.object(app_module.urllib.request, "urlopen", return_value=fake_response) as urlopen:
            ems_body = html.unescape(self.client.get("/app").data.decode("utf-8"))
            self.import_case_for_form(
                {
                    "case_id": "remote-disaster-settings",
                    "case_date": "2026/07/22",
                    "case_time": "1207",
                    "address": "桃園市觀音區",
                    "personnel": ["甲"],
                }
            )
            disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn('<option value="NAS救護91">NAS救護91 | NAS-EMS</option>', ems_body)
        self.assertIn('<option value="NAS救災11">NAS救災11 | NAS-FIRE</option>', disaster_body)
        requests = [call.args[0] for call in urlopen.call_args_list]
        vehicle_settings_requests = [
            request_object
            for request_object in requests
            if request_object.full_url == "http://nas.test:8080/worker/vehicle-settings"
        ]
        roster_requests = [
            request_object
            for request_object in requests
            if request_object.full_url == "http://nas.test:8080/worker/civilpower-roster"
        ]
        self.assertEqual(2, len(vehicle_settings_requests))
        self.assertEqual(1, len(roster_requests))
        for request_object in requests:
            self.assertEqual("test-token", request_object.get_header("X-worker-token"))

    def test_app_page_includes_consumable_package_shortcuts(self):
        self.import_case_for_form(
            {
                "case_id": "case-consumable-package",
                "address": "\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                "case_time_hhmm": "0905",
            }
        )
        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        for package_key, label in [
            ("glucose", "血糖套餐"),
            ("iv", "IV套餐"),
            ("io", "IO套餐"),
            ("ecg", "心電圖套餐"),
            ("ohca", "OHCA套餐"),
            ("gauze", "4吋紗布"),
            ("disposable_bed_sheet", "拋棄床單"),
        ]:
            self.assertIn(f'data-consumable-package="{package_key}"', body)
            self.assertIn(f">{label}</button>", body)
        for consumable_name in [
            "桃-血糖試紙(片)",
            "桃-安全型採血針(支)",
            "桃-酒精棉片(片)",
            "桃-20號防回血IC針(支)",
            "桃-免針型輸液套(組)",
            "桃-透明敷料op site(片)",
            "桃-注射用-生理食鹽水500ml(包)",
            "桃-45mm拋棄式骨內血管穿刺針具(組)",
            "桃-10ml預充式導管沖洗器(支)",
            "桃-心電圖電極貼片(片)",
            "桃-拋棄式CPR回饋貼片(組)",
            "桃-成人甦醒球(組)",
            "桃-連接管-長管(條)",
            "桃-非充氣聲門上呼吸道-4號(組)",
            "桃-細菌過濾器(組)",
            "桃-4吋紗布塊(包)",
            "桃-擔架床用可丟棄式床單(件)",
        ]:
            self.assertIn(consumable_name, body)
        self.assertIn('<span class="package-group-label">套餐帶入</span>', body)
        self.assertIn('class="add-consumable-button" id="add-consumable">＋ 新增耗材</button>', body)
        self.assertIn(".consumable-row.is-package-consumable", body)
        self.assertIn('id="consumable-package-reminder"', body)
        self.assertIn('packageReminder.textContent = loadedLabels.length ? `已帶入：${loadedLabels.join("、")}` : "";', body)
        self.assertIn("const baselineConsumablesLoaded =", body)
        self.assertIn('removals: ["桃-可拋棄式耳溫槍耳套-福爾TD-1118(個)"]', body)
        self.assertIn('disinfectionItems: ["血糖機"]', body)
        self.assertIn('disinfectionItems: ["固定式氧氣組", "自動給氧機", "心臟電擊去顫器", "自動心肺復甦機"]', body)
        self.assertIn('const packageConsumableRemovals = new Map();', body)
        self.assertIn('const autoCheckedDisinfectionItems = new Map();', body)
        self.assertIn("function removeConsumableRowsByName(name)", body)
        self.assertIn("請確認針號", body)
        self.assertIn("請確認輸液", body)
        self.assertNotIn("請確認針長", body)
        self.assertIn("請確認尺寸", body)
        self.assertNotIn(">紗布套餐</button>", body)
        self.assertIn(">4吋紗布</button>", body)
        self.assertIn(">拋棄床單</button>", body)
        iv_section = body[body.index("iv: {") : body.index("io: {")]
        io_section = body[body.index("io: {") : body.index("ecg: {")]
        self.assertIn('"桃-注射用-生理食鹽水500ml(包)": "請確認輸液"', iv_section)
        self.assertNotIn('"桃-注射用-生理食鹽水500ml(包)": "請確認輸液"', io_section)

    def test_app_page_hides_two_vehicle_fields_until_eligible_case_is_imported(self):
        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertNotIn('name="two_vehicle"', body)
        self.assertNotIn('id="second-vehicle-section"', body)

    def test_ems_field_titles_share_the_same_label_size(self):
        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertIn(
            ".workspace-page--ems-task #task-form .field-label-title,\n    .workspace-page--ems-task #task-form .patient-count-option > span { font-size: var(--workspace-type-label); line-height: 1.4; }",
            body,
        )

    def test_imported_ambulance_case_with_four_personnel_shows_two_vehicle_fields(self):
        self.import_case_for_form(
            {
                "case_id": "case-two-vehicle",
                "category": "\u7dca\u6025\u6551\u8b77-\u6025\u75c5",
                "address": "\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                "case_time_hhmm": "0911",
                "personnel": ["\u7532", "\u4e59", "\u4e19", "\u4e01"],
            }
        )

        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertIn('name="two_vehicle"', body)
        self.assertIn('class="site-card two-vehicle-option-card" data-site-card="two-vehicle-option"', body)
        lookup_position = body.index('<section class="panel lookup">')
        option_position = body.index('data-site-card="two-vehicle-option"')
        work_record_position = body.index('data-site-card="work-record"')
        self.assertLess(lookup_position, option_position)
        self.assertLess(option_position, work_record_position)
        option_section = body[option_position:work_record_position]
        self.assertIn('name="two_vehicle"', option_section)
        self.assertIn('.site-card-heading { display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; }', body)
        self.assertIn('.vehicle-card-label { display: none; color: var(--running); }', body)
        self.assertIn('.workspace-page #task-form.two-vehicle-enabled .vehicle-card-label { display: inline-flex; }', body)
        self.assertIn('<h2>將建立的 NAS 資料夾名稱</h2>', body)
        self.assertIn("\u5169\u8eca\u540c\u6642\u767b\u6253", body)
        self.assertIn("\u6b64\u52fe\u9078\u70ba\u5169\u8eca\u540c\u6642\u767b\u6253\uff0c\u82e5\u9700\u5206\u958b\u767b\u6253\u5247\u4e0d\u7528\u52fe\u9078", body)
        self.assertNotIn("2\u8eca\u51fa\u52e4", body)
        self.assertEqual(5, body.count('<span class="vehicle-card-label vehicle-card-label--1">1\u8eca</span>'))
        self.assertEqual(4, body.count('<span class="vehicle-card-label vehicle-card-label--2">2\u8eca</span>'))
        self.assertLess(body.index('<span class="vehicle-card-label vehicle-card-label--1">1\u8eca</span>'), body.index('<h2>案件資料</h2>'))
        primary_field_order = [
            'name="case_time"',
            'name="return_time"',
            'name="vehicle"',
            'name="driver"',
            'name="case_reason"',
            'name="patient_summary"',
            'name="refusal_summary"',
            'name="mileage"',
        ]
        primary_field_positions = [body.index(field) for field in primary_field_order]
        self.assertEqual(primary_field_positions, sorted(primary_field_positions))
        self.assertIn("送醫人數", body)
        self.assertIn("拒送人數", body)
        self.assertEqual(2, body.count('data-patient-count="male"'))
        self.assertEqual(2, body.count('data-patient-count="female"'))
        self.assertEqual(2, body.count('data-refusal-count="male"'))
        self.assertEqual(2, body.count('data-refusal-count="female"'))
        self.assertEqual(8, body.count('class="patient-count-select"'))
        self.assertEqual(8, body.count('<span class="patient-count-unit" aria-hidden="true">人</span>'))
        self.assertIn(
            ".workspace-page--ems-task #task-form .patient-summary-field { min-width: 0; margin: 0 0 11px; color: var(--ink); font-size: var(--workspace-type-label); font-weight: 700; }",
            body,
        )
        self.assertIn(
            ".workspace-page--ems-task #task-form .patient-count-option { display: flex; align-items: center; gap: 6px; min-width: 0; color: var(--ink); font-size: var(--workspace-type-label); font-weight: 700; line-height: 1.4; }",
            body,
        )
        self.assertIn(
            ".workspace-page--ems-task #task-form label[data-field-name='mileage'] { font-size: var(--workspace-type-label); }",
            body,
        )
        self.assertIn("#task-form .patient-count-select select { width: 100%; margin-top: 0; padding-right: 42px; }", body)
        self.assertIn(".mileage-grid > .mileage-row { display: block; margin: 0; }", body)
        for count in range(6):
            self.assertIn(f'value="{count}"', body)
        self.assertIn('id="case-time-2-display"', body)
        self.assertNotIn('data-field-name="case_reason_2_display"', body)
        self.assertNotIn('id="case-reason-2-display"', body)
        second_field_order = [
            'id="case-time-2-display"',
            'name="return_time_2"',
            'name="vehicle_2"',
            'name="driver_2"',
            'name="patient_summary_2"',
            'name="refusal_summary_2"',
            'name="mileage_2"',
        ]
        second_field_positions = [body.index(field) for field in second_field_order]
        self.assertEqual(second_field_positions, sorted(second_field_positions))
        self.assertIn('name="vehicle_2"', body)
        self.assertIn('name="driver_2"', body)
        self.assertIn('name="return_time_2"', body)
        self.assertIn('name="mileage_2"', body)
        self.assertIn('name="patient_summary_2"', body)
        self.assertIn('name="refusal_summary_2"', body)
        self.assertIn('name="consumables_2"', body)
        self.assertIn('name="disinfection_items_2"', body)
        self.assertIn('data-consumable-target="1"', body)
        self.assertIn('data-consumable-target="2"', body)
        for package_key in ("glucose", "iv", "io", "ecg", "ohca", "gauze", "disposable_bed_sheet"):
            self.assertIn(f'data-consumable-package="{package_key}" data-consumable-target="2"', body)

    def test_imported_salvaged_body_case_is_treated_as_ambulance_drowning(self):
        self.import_case_for_form(
            {
                "case_id": "case-salvaged-body",
                "category": "其他-打撈浮屍",
                "reason": "溺水",
                "address": "桃園市觀音區",
                "case_time_hhmm": "0911",
                "personnel": ["甲", "乙", "丙", "丁"],
            }
        )

        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertIn('<option value="溺水" selected>溺水</option>', body)
        self.assertIn('name="two_vehicle"', body)
        self.assertIn("兩車同時登打", body)

    def test_app_page_includes_last_mileage_by_vehicle(self):
        self.store.create(
            app_module.request_from_form(
                self.valid_task_data(case_id="case-previous-primary", vehicle="\u65b0\u576191", mileage="11111")
            )
        )
        self.store.create(
            app_module.request_from_form(
                self.valid_task_data(
                    case_id="case-previous-two-vehicle",
                    vehicle="\u65b0\u576193",
                    mileage="12000",
                    two_vehicle="1",
                    vehicle_2="\u65b0\u576192",
                    driver_2="\u738b\u6631\u52db",
                    mileage_2="22222",
                    return_time_2="1130",
                    patient_summary_2="\u7121",
                )
            )
        )
        self.import_case_for_form(
            {
                "case_id": "case-last-mileage",
                "category": "\u7dca\u6025\u6551\u8b77-\u6025\u75c5",
                "address": "\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                "case_time_hhmm": "0911",
                "personnel": ["\u7532", "\u4e59", "\u4e19", "\u4e01"],
            }
        )

        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        compact_body = body.replace(" ", "")
        self.assertIn("\u4e0a\u6b21\u8a72\u8eca\u8f1b\u767b\u6253\u7684\u91cc\u7a0b", body)
        self.assertIn('data-last-mileage-for="vehicle"', body)
        self.assertIn('data-last-mileage-for="vehicle_2"', body)
        self.assertIn(".mileage-hint-panel { min-width: 0; }", body)
        self.assertIn(".mileage-hint-spacer { min-height: calc(1.45em + 6px); visibility: hidden; }", body)
        self.assertIn('"\\u65b0\\u576191":"11111"', compact_body)
        self.assertIn('"\\u65b0\\u576193":"12000"', compact_body)
        self.assertIn('"\\u65b0\\u576192":"22222"', compact_body)

    def test_imported_case_requires_more_than_three_ambulance_personnel_for_two_vehicle(self):
        self.import_case_for_form(
            {
                "case_id": "case-three-personnel",
                "category": "\u7dca\u6025\u6551\u8b77-\u6025\u75c5",
                "address": "\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                "case_time_hhmm": "0911",
                "personnel": ["\u7532", "\u4e59", "\u4e19"],
            }
        )

        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertNotIn('name="two_vehicle"', body)

    def test_imported_non_ambulance_case_hides_two_vehicle_fields(self):
        self.import_case_for_form(
            {
                "case_id": "case-fire",
                "category": "\u706b\u707d",
                "address": "\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                "case_time_hhmm": "0911",
                "personnel": ["\u7532", "\u4e59", "\u4e19", "\u4e01"],
            }
        )

        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertNotIn('name="two_vehicle"', body)

    def test_status_includes_runtime_consumable_diagnostics(self):
        response = self.client.get("/status")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("app_dir", data)
        self.assertEqual(
            data["default_consumables"],
            ["桃-口罩(片)", "桃-9吋手套-L(雙)", "桃-可拋棄式耳溫槍耳套-福爾TD-1118(個)"],
        )
        self.assertEqual(
            data["consumable_top_names"][:5],
            [
                "桃-血糖試紙(片)",
                "桃-安全型採血針(支)",
                "桃-可拋棄式耳溫槍耳套-福爾TD-1118(個)",
                "桃-心電圖電極貼片(片)",
                "桃-拋棄式CPR回饋貼片(組)",
            ],
        )

    def test_nas_app_page_shows_vehicle_settings_and_hides_other_admin_buttons(self):
        response = self.client.get("/app", headers={"Host": "100.114.126.58:8080"})

        self.assertEqual(response.status_code, 200)
        body = html.unescape(response.data.decode("utf-8"))
        self.assertIn("救護各項設定", body)
        self.assertIn('href="/admin/vehicles"', body)
        self.assertNotIn('href="/admin/public-pc"', body)
        self.assertNotIn('href="/admin/sinposmart"', body)
        self.assertNotIn("救護後台", body)
        self.assertNotIn("值班後台", body)
        self.assertIn('class="page-chrome__actions"', body)
        self.assertIn('href="/task-entry">返回上一頁</a>', body)

    def test_app_page_shows_back_navigation_on_nas_and_local_worker(self):
        nas_body = html.unescape(
            self.client.get("/app", headers={"Host": "100.114.126.58:8080"}).data.decode("utf-8")
        )
        local_body = html.unescape(
            self.client.get("/app", headers={"Host": "127.0.0.1:8090"}).data.decode("utf-8")
        )

        navigation = '<a class="button secondary header-navigation-button" href="/task-entry">返回上一頁</a>'
        self.assertIn(navigation, nas_body)
        self.assertIn(navigation, local_body)

    def test_app_page_recent_task_shows_nas_delete_button_but_local_hides_it(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data(), follow_redirects=False)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        nas_body = html.unescape(
            self.client.get("/app", headers={"Host": "100.114.126.58:8080"}).data.decode("utf-8")
        )
        local_body = html.unescape(
            self.client.get("/app", headers={"Host": "127.0.0.1:8090"}).data.decode("utf-8")
        )
        self.assertIn(f'href="/tasks/{task_id}"', nas_body)
        self.assertIn(f'action="/tasks/{task_id}/delete"', nas_body)
        self.assertIn('aria-label="刪除案件"', nas_body)
        self.assertIn('name="return_to" value="ems"', nas_body)
        self.assertNotIn(f'action="/tasks/{task_id}/delete"', local_body)
        self.assertNotIn('aria-label="刪除案件"', local_body)

    def test_app_page_recent_task_titles_show_one_or_two_vehicles(self):
        address = "桃園市觀音區崙坪三路126號1樓(OHCA-N)"
        single = self.store.create(
            app_module.request_from_form(
                self.valid_task_data(
                    case_id="case-single-vehicle-title",
                    case_reason="空跑",
                    case_address=address,
                    case_date="2026-07-26",
                    case_time="2300",
                    vehicle="新坡92",
                )
            )
        )
        double = self.store.create(
            app_module.request_from_form(
                self.valid_task_data(
                    case_id="case-two-vehicle-title",
                    case_reason="空跑",
                    case_address=address,
                    case_date="2026-07-26",
                    case_time="2300",
                    vehicle="新坡92",
                    two_vehicle="1",
                    vehicle_2="新坡93",
                    driver_2="陳小華",
                    mileage_2="200",
                    return_time_2="1130",
                    patient_summary_2="女一名",
                    consumables_2="桃-口罩(片)=2",
                )
            )
        )

        body = html.unescape(self.client.get("/app").data.decode("utf-8"))

        title = f"緊急救護-空跑 - {address}"
        self.assertIn(
            f'<a class="recent-title" href="/tasks/{single["task"]["task_id"]}">{title} - 新坡92</a>',
            body,
        )
        self.assertIn(
            f'<a class="recent-title" href="/tasks/{double["task"]["task_id"]}">{title} - 新坡92、新坡93</a>',
            body,
        )
        self.assertIn('<div class="recent-time">07/26 2300 - 06/07 1119</div>', body)

    def test_disaster_recent_task_title_uses_the_actual_case_type_and_short_timestamp(self):
        task = self.store.create(
            AmbulanceReturnRequest(
                task_id="recent-disaster-case-title",
                created_at=datetime.now(),
                raw_text="",
                service_type="disaster",
                summary_type="火災",
                case_reason="建築物火災",
                case_address="桃園市觀音區火災路1號",
                case_date="2026-07-26",
                case_time="2300",
                vehicle="新坡11",
            )
        )

        body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn(
            f'<a class="recent-title" href="/tasks/{task["task"]["task_id"]}">火災-建築物火災 - 桃園市觀音區火災路1號 - 新坡11</a>',
            body,
        )

    def test_task_entry_pages_show_only_matching_service_recent_tasks(self):
        ems = self.store.create(
            AmbulanceReturnRequest(
                task_id="recent-ems-task",
                created_at=datetime.now(),
                raw_text="",
                service_type="ems",
                vehicle="新坡91",
                case_reason="救護最近任務",
            )
        )
        disaster = self.store.create(
            AmbulanceReturnRequest(
                task_id="recent-disaster-task",
                created_at=datetime.now(),
                raw_text="",
                service_type="disaster",
                vehicle="新坡11",
                case_reason="救災最近任務",
            )
        )

        ems_body = html.unescape(self.client.get("/app").data.decode("utf-8"))
        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn("最近任務", ems_body)
        self.assertIn(f'href="/tasks/{ems["task"]["task_id"]}"', ems_body)
        self.assertNotIn(f'href="/tasks/{disaster["task"]["task_id"]}"', ems_body)
        self.assertIn("最近任務", disaster_body)
        self.assertIn(f'href="/tasks/{disaster["task"]["task_id"]}"', disaster_body)
        self.assertNotIn(f'href="/tasks/{ems["task"]["task_id"]}"', disaster_body)

    def test_task_entry_pages_hide_recent_tasks_after_case_imported(self):
        self.store.create(
            AmbulanceReturnRequest(
                task_id="recent-ems-hidden-after-import",
                created_at=datetime.now(),
                raw_text="",
                service_type="ems",
                vehicle="新坡91",
                case_reason="救護最近任務",
            )
        )
        self.store.create(
            AmbulanceReturnRequest(
                task_id="recent-disaster-hidden-after-import",
                created_at=datetime.now(),
                raw_text="",
                service_type="disaster",
                vehicle="新坡11",
                case_reason="救災最近任務",
            )
        )

        for path in ("/app", "/app/disaster"):
            with self.subTest(path=path):
                self.import_case_for_form(
                    {
                        "case_id": "case-hides-recent-tasks",
                        "case_date": "2026/08/18",
                        "case_time_hhmm": "0905",
                        "address": "桃園市觀音區",
                        "personnel": ["甲"],
                    }
                )
                body = html.unescape(self.client.get(path).data.decode("utf-8"))
                self.assertNotIn('<section class="panel recent">', body)

    def test_public_pc_recent_tasks_hide_all_statuses_after_48_hours(self):
        now = datetime.now()
        for task_id, age_hours, completed in (
            ("completed-49-hours", 49, True),
            ("completed-47-hours", 47, True),
            ("waiting-49-hours", 49, False),
        ):
            task_time = now - timedelta(hours=age_hours)
            payload = self.store.create(
                AmbulanceReturnRequest(
                    task_id=task_id,
                    created_at=task_time,
                    raw_text="",
                    vehicle="新坡91",
                )
            )
            payload["created_at"] = task_time.isoformat(timespec="seconds")
            payload["updated_at"] = task_time.isoformat(timespec="seconds")
            if completed:
                for site in payload["site_statuses"].values():
                    site["status"] = "completed_by_user"
                payload["overall_status"] = "desktop_fast_completed"
            else:
                payload["site_statuses"]["duty_work_log"]["status"] = "duty_work_log_waiting_confirmation"
                payload["overall_status"] = "desktop_fast_completed_with_errors"
            self.store.path_for(task_id).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertNotIn('href="/tasks/completed-49-hours"', body)
        self.assertIn('href="/tasks/completed-47-hours"', body)
        self.assertNotIn('href="/tasks/waiting-49-hours"', body)

    def test_nas_recent_tasks_hide_failed_statuses_after_48_hours(self):
        task_time = datetime.now() - timedelta(hours=49)
        payload = self.store.create(
            AmbulanceReturnRequest(
                task_id="nas-failed-49-hours",
                created_at=task_time,
                raw_text="",
                vehicle="TEST-48",
            )
        )
        payload["created_at"] = task_time.isoformat(timespec="seconds")
        payload["updated_at"] = task_time.isoformat(timespec="seconds")
        payload["site_statuses"]["duty_work_log"]["status"] = "duty_work_log_waiting_confirmation"
        payload["overall_status"] = "desktop_fast_completed_with_errors"
        self.store.path_for("nas-failed-49-hours").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        body = html.unescape(
            self.client.get("/app", headers={"Host": "100.114.126.58:8080"}).data.decode("utf-8")
        )

        self.assertNotIn('href="/tasks/nas-failed-49-hours"', body)

    def test_nas_recent_tasks_merge_public_pc_reports_as_read_only_by_service(self):
        fixture_store = JsonTaskStore(Path(self.tmp.name) / "public-pc-report-fixtures")
        ems_report = fixture_store.create(
            AmbulanceReturnRequest(
                task_id="public-pc-ems-recent",
                created_at=datetime.now(),
                raw_text="",
                service_type="ems",
                vehicle="新坡91",
                case_reason="公務電腦救護案件",
                case_address="桃園市觀音區救護路1號",
            )
        )
        disaster_report = fixture_store.create(
            AmbulanceReturnRequest(
                task_id="public-pc-disaster-recent",
                created_at=datetime.now(),
                raw_text="",
                service_type="disaster",
                vehicle="新坡11",
                case_reason="公務電腦救災案件",
                case_address="桃園市觀音區救災路1號",
            )
        )
        app_module.write_json_atomic(
            app_module.public_pc_report_file(),
            {"tasks": [ems_report, disaster_report]},
        )
        headers = {"Host": "100.114.126.58:8080"}

        ems_body = html.unescape(self.client.get("/app", headers=headers).data.decode("utf-8"))
        disaster_body = html.unescape(self.client.get("/app/disaster", headers=headers).data.decode("utf-8"))

        self.assertIn("公務電腦救護案件", ems_body)
        self.assertNotIn("公務電腦救災案件", ems_body)
        self.assertNotIn('href="/tasks/public-pc-ems-recent"', ems_body)
        self.assertIn("公務電腦建立", ems_body)
        self.assertIn("NAS 僅查看", ems_body)
        self.assertNotIn('action="/tasks/public-pc-ems-recent/delete"', ems_body)
        self.assertIn("公務電腦救災案件", disaster_body)
        self.assertNotIn("公務電腦救護案件", disaster_body)
        self.assertNotIn('href="/tasks/public-pc-disaster-recent"', disaster_body)
        self.assertIn("公務電腦建立", disaster_body)
        self.assertIn("NAS 僅查看", disaster_body)
        self.assertNotIn('action="/tasks/public-pc-disaster-recent/delete"', disaster_body)

    def test_nas_disaster_recent_task_delete_returns_to_disaster_page(self):
        task = self.store.create(
            AmbulanceReturnRequest(
                task_id="nas-disaster-delete",
                created_at=datetime.now(),
                raw_text="",
                service_type="disaster",
                vehicle="新坡11",
                case_reason="NAS 救災案件",
                case_address="桃園市觀音區刪除路1號",
            )
        )
        task_id = task["task"]["task_id"]
        headers = {"Host": "100.114.126.58:8080"}

        body = html.unescape(self.client.get("/app/disaster", headers=headers).data.decode("utf-8"))
        self.assertIn(f'action="/tasks/{task_id}/delete"', body)
        self.assertIn('name="return_to" value="disaster"', body)

        response = self.client.post(
            f"/tasks/{task_id}/delete",
            data={"return_to": "disaster"},
            headers=headers,
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/app/disaster")
        with self.assertRaises(FileNotFoundError):
            self.store.get(task_id)

    def test_nas_recent_tasks_prefer_nas_task_when_public_pc_report_has_same_id(self):
        task_id = "shared-recent-task"
        nas_payload = self.store.create(
            AmbulanceReturnRequest(
                task_id=task_id,
                created_at=datetime.now(),
                raw_text="",
                service_type="ems",
                vehicle="新坡91",
                case_reason="NAS 原始案件",
                case_address="桃園市觀音區去重路1號",
            )
        )
        report_payload = json.loads(json.dumps(nas_payload, ensure_ascii=False))
        report_payload["task"]["case_reason"] = "公務電腦重複案件"
        app_module.write_json_atomic(
            app_module.public_pc_report_file(),
            {"tasks": [report_payload]},
        )

        body = html.unescape(
            self.client.get("/app", headers={"Host": "100.114.126.58:8080"}).data.decode("utf-8")
        )

        self.assertEqual(1, body.count(f'href="/tasks/{task_id}"'))
        self.assertIn("NAS 原始案件", body)
        self.assertNotIn("公務電腦重複案件", body)
        self.assertIn("NAS建立", body)

    def test_task_delete_route_rejects_windows_path_traversal(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)
        neighbor = cases_dir / "latest.json"
        neighbor.write_text('{"secret": true}', encoding="utf-8")

        response = self.client.post(
            "/tasks/..%5Ccases%5Clatest/delete",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(neighbor.read_text(encoding="utf-8"), '{"secret": true}')

    def test_task_delete_route_rejects_active_worker_claim(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data(), follow_redirects=False)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        self.assertIsNotNone(self.store.claim_next_for_worker("PC-01"))

        response = self.client.post(f"/tasks/{task_id}/delete", follow_redirects=False)

        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.store.path_for(task_id).exists())

    def test_completed_task_direct_run_is_rejected_instead_of_requeued(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data(), follow_redirects=False)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        payload = self.store.get(task_id)
        for site_key in ("duty_work_log", "vehicle_mileage", "consumables", "disinfection"):
            payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"
        payload["overall_status"] = "desktop_fast_completed"
        self.store.save_payload(task_id, payload)

        response = self.client.post(
            f"/tasks/{task_id}/run",
            base_url="http://100.114.126.58:8080",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.get(task_id)["worker_queue"]["status"], "idle")

    def test_edit_page_hides_clear_button(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data(case_address="桃園市觀音區"), follow_redirects=False)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}/edit")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("SinpoSmart - 救護Worker - 編輯狀態", body)
        self.assertNotIn('formaction="/cases/clear"', body)
        self.assertNotIn(">清除</button>", body)

    def test_consumable_quantity_spinner_is_hidden(self):
        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn(".consumable-qty::-webkit-inner-spin-button", body)
        self.assertIn("appearance: textfield", body)

    def test_app_page_hides_task_form_until_case_imported(self):
        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="task-form"', body)
        self.assertIn("請先從上方案件按「帶入」", body)

    def test_create_task_requires_imported_case(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(case_id="", case_date="", case_time="", case_address=""),
            follow_redirects=False,
        )
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_recent(), [])
        self.assertIn("請先從上方案件按「帶入」", body)
        self.assertNotIn('id="task-form"', body)

    def test_create_task_prevents_duplicate_vehicle_submission(self):
        data = self.valid_task_data(case_id="EMS-DUPLICATE-1")

        first = self.client.post("/tasks", data=data, follow_redirects=False)
        second = self.client.post("/tasks", data=data, follow_redirects=False)

        self.assertEqual(302, first.status_code)
        self.assertEqual(first.headers["Location"], second.headers["Location"])
        self.assertEqual(1, len(self.store.list_recent()))

    def test_create_task_allows_same_case_for_different_vehicles(self):
        first = self.client.post(
            "/tasks",
            data=self.valid_task_data(case_id="EMS-TWO-VEHICLE-1", vehicle="新坡93"),
            follow_redirects=False,
        )
        second = self.client.post(
            "/tasks",
            data=self.valid_task_data(case_id="EMS-TWO-VEHICLE-1", vehicle="新坡92"),
            follow_redirects=False,
        )

        self.assertEqual(302, first.status_code)
        self.assertEqual(302, second.status_code)
        self.assertNotEqual(first.headers["Location"], second.headers["Location"])
        self.assertEqual(2, len(self.store.list_recent()))

    def test_create_disaster_task_builds_folders_before_storing_and_prevents_duplicate_case(self):
        data = MultiDict([
            ("case_id", "FIRE-1"), ("case_date", "2026/07/22"), ("case_time", "1207"),
            ("return_time", "1300"), ("case_address", "桃園市觀音區金華路31號"),
            ("summary_type", "火災"), ("case_reason", "一般(集合)住宅"),
            ("personnel", "甲,乙,丙"), ("personnel_accounts", "TYFD-A,TYFD-B,TYFD-C"),
            ("commander", "丙"), ("action_note", "現場待命"),
            ("recorder_category", "轄內A3"),
            ("vehicle", "新坡11"), ("driver", "甲"), ("vehicle_return_time", "1300"), ("mileage", "100"),
            ("vehicle", "新坡15"), ("driver", "乙"), ("vehicle_return_time", "1310"), ("mileage", "200"),
        ])
        folder_results = [mock.Mock(vehicle="新坡11", path=Path("X:/one"), status="created")]

        with mock.patch.object(app_module, "ensure_disaster_media_folders", return_value=folder_results) as folders:
            first = self.client.post("/tasks/disaster", data=data, follow_redirects=False)
            second = self.client.post("/tasks/disaster", data=data, follow_redirects=False)

        self.assertEqual(302, first.status_code)
        self.assertEqual(first.headers["Location"], second.headers["Location"])
        self.assertEqual(1, folders.call_count)
        payload = self.store.list_recent()[0]
        self.assertEqual("disaster", payload["task"]["service_type"])
        self.assertEqual(2, len(payload["task"]["vehicle_entries"]))

    def test_create_disaster_task_rejects_mileage_below_or_more_than_300_from_previous_entry(self):
        self.store.create(
            AmbulanceReturnRequest(
                task_id="previous-disaster-mileage",
                created_at=datetime(2026, 7, 20, 8, 0),
                raw_text="",
                service_type="disaster",
                case_id="FIRE-PREVIOUS-MILEAGE",
                case_date="2026/07/20",
                case_time="0800",
                vehicle_entries=[
                    {"vehicle": "新坡11", "driver": "甲", "mileage": "12000", "return_time": "0830"}
                ],
            )
        )

        def data_for(case_id: str, mileage: str) -> MultiDict:
            return MultiDict([
                ("case_id", case_id), ("case_date", "2026/07/22"), ("case_time", "1207"),
                ("return_time", "1300"), ("case_address", "桃園市觀音區金華路31號"),
                ("summary_type", "火災"), ("case_reason", "一般(集合)住宅"),
                ("personnel", "甲,乙"), ("personnel_accounts", "TYFD-A,TYFD-B"),
                ("commander", "乙"), ("action_note", "現場待命"), ("recorder_category", "轄內A3"),
                ("vehicle", "新坡11"), ("driver", "甲"), ("vehicle_return_time", "1300"), ("mileage", mileage),
            ])

        for mileage, expected_error in (
            ("11999", "第1車里程不能小於上次登打的里程（12000）"),
            ("12301", "第1車里程與上次登打相差不可超過 300 公里（上次 12000）"),
        ):
            with self.subTest(mileage=mileage), mock.patch.object(app_module, "ensure_disaster_media_folders", return_value=[]):
                response = self.client.post("/tasks/disaster", data=data_for(f"fire-mileage-{mileage}", mileage))

                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_error, html.unescape(response.data.decode("utf-8")))

        with mock.patch.object(app_module, "ensure_disaster_media_folders", return_value=[]):
            allowed = self.client.post("/tasks/disaster", data=data_for("fire-mileage-allowed", "12300"))

        self.assertEqual(allowed.status_code, 302)

    def test_create_disaster_task_snapshots_each_nas_mileage_system_name(self):
        data = MultiDict([
            ("case_id", "FIRE-MILEAGE-NAMES"), ("case_date", "2026/07/22"), ("case_time", "1207"),
            ("return_time", "1300"), ("case_address", "桃園市觀音區金華路31號"),
            ("summary_type", "火災"), ("case_reason", "一般(集合)住宅"),
            ("personnel", "甲,乙,丙"), ("personnel_accounts", "TYFD-A,TYFD-B,TYFD-C"),
            ("commander", "丙"), ("action_note", "現場待命"), ("recorder_category", "轄內A3"),
            ("vehicle", "新坡11"), ("driver", "甲"), ("vehicle_return_time", "1300"), ("mileage", "100"),
            ("vehicle", "新坡99"), ("driver", "乙"), ("vehicle_return_time", "1310"), ("mileage", "200"),
        ])
        vehicle_records = [
            {"label": "新坡11", "ppe_name": "KEC-2608", "recorder_code": "11"},
            {"label": "新坡99", "ppe_name": "NEW-9900", "recorder_code": "99"},
        ]

        with mock.patch.object(app_module, "ensure_disaster_media_folders", return_value=[]), mock.patch.object(
            app_module, "effective_disaster_vehicle_records", return_value=vehicle_records
        ):
            response = self.client.post("/tasks/disaster", data=data, follow_redirects=False)

        self.assertEqual(302, response.status_code)
        task = self.store.list_recent()[0]["task"]
        self.assertEqual(
            ["KEC-2608", "NEW-9900"],
            [entry["mileage_system_name"] for entry in task["vehicle_entries"]],
        )

    def test_legacy_disaster_task_is_hydrated_before_retry(self):
        task_id = "legacy-disaster-mileage-names"
        payload = self.store.create(
            AmbulanceReturnRequest(
                task_id=task_id,
                created_at=datetime.now(),
                raw_text="",
                service_type="disaster",
                vehicle="新坡11",
                vehicle_entries=[
                    {"vehicle": "新坡11", "driver": "甲"},
                    {"vehicle": "新坡15", "driver": "乙"},
                ],
            )
        )
        vehicle_records = [
            {"label": "新坡11", "ppe_name": "KEC-2608", "recorder_code": "11"},
            {"label": "新坡15", "ppe_name": "KES-5922", "recorder_code": "15"},
        ]

        with mock.patch.object(app_module, "effective_disaster_vehicle_records", return_value=vehicle_records):
            hydrated = app_module.hydrate_disaster_task_mileage_system_names(task_id, payload)

        self.assertEqual(
            ["KEC-2608", "KES-5922"],
            [entry["mileage_system_name"] for entry in hydrated["task"]["vehicle_entries"]],
        )
        self.assertEqual("KEC-2608", hydrated["task"]["mileage_system_name"])

    def test_disaster_mileage_retry_hydrates_legacy_task_before_starting(self):
        task_id = "legacy-disaster-mileage-retry"
        self.store.create(
            AmbulanceReturnRequest(
                task_id=task_id,
                created_at=datetime.now(),
                raw_text="",
                service_type="disaster",
                vehicle="新坡11",
                vehicle_entries=[{"vehicle": "新坡11", "driver": "甲"}],
            )
        )

        with mock.patch.object(
            app_module,
            "effective_disaster_vehicle_records",
            return_value=[{"label": "新坡11", "ppe_name": "KEC-2608", "recorder_code": "11"}],
        ), mock.patch.object(app_module, "effective_task_execution_mode", return_value="desktop_fast"):
            response = self.client.post(f"/tasks/{task_id}/sites/vehicle_mileage/run", follow_redirects=False)

        self.assertEqual(302, response.status_code)
        self.assertEqual([(task_id, "vehicle_mileage")], app_module.desktop_runner.started_sites)
        self.assertEqual("KEC-2608", self.store.get(task_id)["task"]["mileage_system_name"])

    def test_create_disaster_task_requires_commander_from_case_personnel(self):
        data = MultiDict([
            ("case_id", "FIRE-2"), ("case_date", "2026/07/22"), ("case_time", "1207"),
            ("return_time", "1300"), ("case_address", "桃園市觀音區金華路31號"),
            ("case_reason", "一般(集合)住宅"), ("personnel", "甲,乙"), ("commander", "丙"),
            ("action_note", "現場待命"), ("recorder_category", "轄內A3"),
            ("vehicle", "新坡11"), ("driver", "甲"), ("vehicle_return_time", "1300"), ("mileage", "100"),
        ])

        response = self.client.post("/tasks/disaster", data=data)

        self.assertEqual(400, response.status_code)
        self.assertIn("指揮官必須是本案服勤人員", html.unescape(response.data.decode("utf-8")))
        self.assertEqual([], self.store.list_recent())

    def test_create_disaster_task_requires_valid_summary_type(self):
        data = MultiDict([
            ("case_id", "FIRE-3"), ("case_date", "2026/07/22"), ("case_time", "1207"),
            ("return_time", "1300"), ("case_address", "桃園市觀音區金華路31號"),
            ("case_reason", "一般(集合)住宅"), ("personnel", "甲,乙"), ("commander", "乙"),
            ("action_note", "現場待命"), ("recorder_category", "轄內A3"),
            ("vehicle", "新坡11"), ("driver", "甲"), ("vehicle_return_time", "1300"), ("mileage", "100"),
        ])

        response = self.client.post("/tasks/disaster", data=data)

        self.assertEqual(400, response.status_code)
        self.assertIn("請選擇正確案件類型", html.unescape(response.data.decode("utf-8")))
        self.assertEqual([], self.store.list_recent())

    def test_create_task_writes_json_and_redirects(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                case_time="1420",
                return_time="1505",
                case_address="\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                case_reason="\u6025\u75c5",
                patient_summary="\u7537\u4e00\u540d",
                consumables="\u53e3\u7f69=2,\u624b\u5957=2",
            ),
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        tasks = self.store.list_recent()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task"]["vehicle"], "\u65b0\u576191")
        self.assertEqual(tasks[0]["task"]["case_time"], "1420")
        self.assertEqual(tasks[0]["task"]["case_address"], "\u6843\u5712\u5e02\u89c0\u97f3\u5340")
        self.assertEqual(tasks[0]["task"]["case_reason"], "\u6025\u75c5")

    def test_create_task_requires_vehicle_driver_mileage_return_time_and_patient(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(vehicle="", driver="", mileage="", return_time="", patient_summary=""),
            follow_redirects=False,
        )
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_recent(), [])
        self.assertIn('<div class="form-errors" role="alert">', body)
        self.assertIn('name="return_date" id="return-date" inputmode="numeric" autocomplete="off" placeholder="YYYY/MM/DD" value="2026/06/07"', body)
        self.assertIn('target.scrollIntoView({ block: "start" });', body)
        self.assertIn('const formErrors = ', body)
        self.assertIn('"請填寫返隊時間": { name: "return_time"', body)
        self.assertIn('"請選擇出動車輛": { name: "vehicle"', body)
        self.assertIn('"請選擇司機": { name: "driver"', body)
        self.assertIn('"請確認送醫／拒送人數": { name: "patient_summary"', body)
        self.assertIn('"請填寫里程": { name: "mileage"', body)
        self.assertIn('setFieldState(field.name, "error");', body)
        self.assertNotIn("錯誤：${errorMessage}", body)
        expected_order = ["請填寫返隊時間", "請選擇出動車輛", "請選擇司機", "請確認送醫／拒送人數", "請填寫里程"]
        positions = [body.index(message) for message in expected_order]
        self.assertEqual(positions, sorted(positions))

    def test_create_task_requires_consumables(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(consumables=""),
            follow_redirects=False,
        )
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_recent(), [])
        self.assertIn("請選擇耗材", body)

    def test_create_task_rejects_non_numeric_mileage(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(mileage="12A3"),
            follow_redirects=False,
        )
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_recent(), [])
        self.assertIn("里程只能輸入數字", body)
        self.assertIn('"里程只能輸入數字": { name: "mileage", message: "里程只能輸入數字" }', body)

    def test_create_task_rejects_mileage_below_or_more_than_300_from_previous_entry(self):
        self.store.create(
            AmbulanceReturnRequest(
                task_id="previous-ems-mileage",
                created_at=datetime(2026, 6, 6, 8, 0),
                raw_text="",
                vehicle="新坡91",
                mileage="12000",
                case_date="2026/06/06",
                case_time="0800",
            )
        )

        for mileage, expected_error in (
            ("11999", "里程不能小於上次登打的里程（12000）"),
            ("12301", "里程與上次登打相差不可超過 300 公里（上次 12000）"),
        ):
            with self.subTest(mileage=mileage):
                response = self.client.post(
                    "/tasks",
                    data=self.valid_task_data(case_id=f"ems-mileage-{mileage}", mileage=mileage),
                    follow_redirects=False,
                )

                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_error, html.unescape(response.data.decode("utf-8")))

        allowed = self.client.post(
            "/tasks",
            data=self.valid_task_data(case_id="ems-mileage-allowed", mileage="12300"),
            follow_redirects=False,
        )

        self.assertEqual(allowed.status_code, 302)

    def test_create_task_validation_preserves_consumable_package_state(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                vehicle="",
                consumables="桃-酒精棉片(片)=3,桃-20號防回血IC針(支)=1,桃-注射用-生理食鹽水500ml(包)=1",
                consumable_packages="iv,ohca,invalid,iv",
                baseline_consumables_loaded="1",
            ),
            follow_redirects=False,
        )
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertIn('name="consumable_packages" id="consumable-packages-value" value="iv,ohca"', body)
        self.assertIn('name="baseline_consumables_loaded" value="1"', body)
        self.assertIn('const baselineConsumablesLoaded = true;', body)
        self.assertIn('const selectedConsumablePackages = ["iv", "ohca"];', body)
        self.assertIn("桃-20號防回血IC針(支)", body)
        self.assertIn("桃-注射用-生理食鹽水500ml(包)", body)

    def test_create_task_validation_keeps_imported_personnel_driver_options(self):
        first_person = "\u5433\u5b97\u8015"
        second_person = "\u694a\u5f18\u5b87"
        unrelated_person = "\u5305\u83ef\u5148"
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                personnel=f"{first_person},{second_person}",
                driver=second_person,
                mileage="",
            ),
            follow_redirects=False,
        )
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertIn(f'<input type="hidden" name="personnel" value="{first_person},{second_person}">', body)
        self.assertIn(f'<option value="{first_person}">{first_person}</option>', body)
        self.assertIn(f'<option value="{second_person}" selected>{second_person}</option>', body)
        self.assertNotIn(f'<option value="{unrelated_person}">{unrelated_person}</option>', body)

    def test_admin_vehicle_create_adds_vehicle_option(self):
        nas_headers = {"Host": "100.114.126.58:8080"}
        page = self.client.get("/admin/vehicles", headers=nas_headers)
        page_body = html.unescape(page.data.decode("utf-8"))
        self.assertIn("救護各項設定", page_body)
        self.assertIn("車輛代號", page_body)
        self.assertIn('href="/app">返回救護登打</a>', page_body)
        self.assertIn('<button type="submit">儲存車輛</button>', page_body)
        self.assertIn("目前車輛", page_body)
        self.assertIn('<div class="vehicle-label">車輛代號</div>', page_body)
        self.assertIn("車牌號碼", page_body)
        self.assertIn("header-row", page_body)
        self.assertIn("新增或更新車輛", page_body)
        self.assertNotIn("返回 APP", page_body)
        self.assertNotIn('placeholder="新坡95"', page_body)
        self.assertNotIn('placeholder="BPE-5951"', page_body)

        response = self.client.post(
            "/admin/vehicles",
            data={"label": "新坡96", "ppe_name": "BPE-5960", "vehicle_type": "內建"},
            headers=nas_headers,
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        settings_path = Path(self.tmp.name) / "settings" / "vehicles.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        saved_vehicle = next(item for item in settings["vehicles"] if item["label"] == "新坡96")
        self.assertEqual("自訂", saved_vehicle["vehicle_type"])
        app_response = self.client.get("/app")
        body = html.unescape(app_response.data.decode("utf-8"))
        self.assertIn("請先從上方案件按「帶入」", body)
        self.import_case_for_form(
            {
                "case_id": "case-vehicle-option",
                "address": "\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                "case_time_hhmm": "0905",
            }
        )
        app_response = self.client.get("/app")
        body = html.unescape(app_response.data.decode("utf-8"))
        self.assertIn('<option value="新坡96">新坡96 | BPE-5960</option>', body)
        response_body = html.unescape(response.data.decode("utf-8"))
        self.assertIn("BPE-5960", response_body)
        self.assertIn('action="/admin/vehicles/delete"', response_body)

    def test_admin_pages_share_layout_tokens(self):
        vehicle_body = html.unescape(
            self.client.get("/admin/vehicles", headers={"Host": "100.114.126.58:8080"}).data.decode("utf-8")
        )
        disaster_vehicle_body = html.unescape(
            self.client.get(
                "/admin/disaster-vehicles", headers={"Host": "100.114.126.58:8080"}
            ).data.decode("utf-8")
        )
        public_pc_body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))
        sinposmart_body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        self.assertIn('href="/static/sinposmart-ui.css"', public_pc_body)
        self.assertIn('href="/static/sinposmart-admin.css"', public_pc_body)
        self.assertIn('class="app-shell"', public_pc_body)
        self.assertNotIn("<style>", public_pc_body)
        for body in (vehicle_body, disaster_vehicle_body, sinposmart_body):
            self.assertIn('href="/static/sinposmart-ui.css"', body)
            self.assertIn('href="/static/sinposmart-workspace.css"', body)
            self.assertIn('class="workspace-page', body)
        shared_columns = (
            "grid-template-columns: minmax(64px, .55fr) minmax(78px, .7fr) minmax(78px, .7fr) "
            "minmax(72px, .62fr) minmax(154px, 1.3fr) minmax(254px, 2.1fr);"
        )
        for body in (vehicle_body, disaster_vehicle_body):
            self.assertIn(shared_columns, body)
            self.assertIn("--vehicle-row-height: 68px;", body)
            self.assertIn("--vehicle-card-height: 300px;", body)
            self.assertIn("grid-template-rows: repeat(3, 68px) 52px;", body)
            self.assertIn("grid-template-areas:", body)
            self.assertIn("最新里程", body)
            self.assertIn("同步狀態", body)
            self.assertIn(".vehicle-sync .vehicle-value {", body)
            self.assertIn(".mileage-query-button {", body)
            self.assertNotIn('name="vehicle_type"', body)
            self.assertNotIn('data-field="類型"', body)
        self.assertIn(".delete-button { min-width: 72px; min-height: 44px;", vehicle_body)
        self.assertIn("white-space: nowrap;", vehicle_body)

        admin_css = self.client.get("/static/sinposmart-admin.css").data.decode("utf-8")
        self.assertIn('.result-filters[aria-label="執行結果分類"] {', admin_css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", admin_css)
        self.assertIn('.result-filters[aria-label="執行結果分類"] .result-filter:first-child {', admin_css)
        self.assertIn("grid-column: 1 / -1;", admin_css)

    def test_task_forms_use_shared_workspace_stylesheet(self):
        headers = {"Host": "100.114.126.58:8080"}
        for path in ("/app", "/app/disaster"):
            with self.subTest(path=path):
                body = html.unescape(self.client.get(path, headers=headers).data.decode("utf-8"))
                self.assertIn('href="/static/sinposmart-ui.css"', body)
                self.assertIn('href="/static/sinposmart-workspace.css"', body)
                self.assertIn('class="workspace-page workspace-page--task', body)

    def test_nas_admin_pages_return_to_nas_home(self):
        for path in ("/admin/public-pc", "/admin/sinposmart"):
            with self.subTest(path=path):
                body = html.unescape(
                    self.client.get(path, headers={"Host": "100.114.126.58:8080"}).data.decode("utf-8")
                )
                self.assertIn("返回首頁", body)
                self.assertIn('href="/"', body)
                self.assertNotIn('href="/app">返回首頁', body)

    def test_admin_vehicle_delete_removes_custom_vehicle_only(self):
        nas_headers = {"Host": "100.114.126.58:8080"}
        protected_response = self.client.post(
            "/admin/vehicles/delete",
            data={"label": "新坡95"},
            headers=nas_headers,
            follow_redirects=False,
        )
        protected_body = html.unescape(protected_response.data.decode("utf-8"))

        self.assertEqual(protected_response.status_code, 400)
        self.assertIn("內建救護車不能刪除", protected_body)
        self.assertIn("新坡95", protected_body)

        self.client.post(
            "/admin/vehicles",
            data={"label": "自訂救護車", "ppe_name": "CUSTOM-EMS"},
            headers=nas_headers,
            follow_redirects=False,
        )
        response = self.client.post(
            "/admin/vehicles/delete",
            data={"label": "自訂救護車"},
            headers=nas_headers,
            follow_redirects=False,
        )
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("已刪除 自訂救護車", body)
        app_response = self.client.get("/app")
        self.assertNotIn("自訂救護車", html.unescape(app_response.data.decode("utf-8")))

        builtin_response = self.client.post(
            "/admin/vehicles/delete",
            data={"label": "新坡91"},
            headers=nas_headers,
            follow_redirects=False,
        )
        builtin_body = html.unescape(builtin_response.data.decode("utf-8"))

        self.assertEqual(builtin_response.status_code, 400)
        self.assertIn("內建救護車不能刪除", builtin_body)
        self.assertIn("新坡91", builtin_body)

    def test_admin_public_pc_receives_and_lists_local_task_events(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        response = self.client.post(
            "/worker/public-pc-task-events",
            headers=worker_headers,
            json={
                "event_id": "evt-1",
                "task_id": "local-task-1",
                "task": {
                    "task_id": "local-task-1",
                    "case_reason": "急病",
                    "case_address": "桃園市觀音區中山路",
                    "vehicle": "新坡91",
                    "driver": "曾彥綸",
                    "case_time": "0830",
                    "return_time": "0910",
                    "patient_summary": "無",
                    "mileage": "54620",
                    "consumables": {"桃-口罩(片)": 2},
                    "disinfection_items": ["擦拭消毒"],
                },
                "user": "8番 曾彥綸 - tyfd01510",
                "synced_account": "8番 曾彥綸 - tyfd01510",
                "site_login_accounts": {
                    "duty_work_log": "8番 曾彥綸 - tyfd01510（任務司機優先）",
                    "vehicle_mileage": "8番 曾彥綸 - tyfd01510（司機帳號優先，失敗一次改同步帳號）",
                    "disinfection": "8番 曾彥綸 - tyfd01510（同步帳號）",
                    "consumables": "8番 曾彥綸 - C123***789（同步帳號）",
                },
                "worker_id": "public-duty-pc",
                "action": "五站登打成功",
                "status": "desktop_fast_completed",
                "detail": "本機快速執行完成。",
                "overall_status": "desktop_fast_completed",
                "site_statuses": {
                    "duty_work_log": {
                        "status": "duty_work_log_saved",
                        "detail": "工作登入帳號：任務司機優先，已保存。",
                        "updated_at": "2026-06-12T14:30:00",
                    },
                    "vehicle_mileage": {
                        "status": "vehicle_mileage_saved",
                        "detail": "里程已保存。",
                        "updated_at": "2026-06-12T14:31:00",
                    },
                    "disinfection": {"status": "disinfection_saved"},
                    "consumables": {"status": "consumables_saved"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ack_id"], "evt-1")
        page = self.client.get("/admin/public-pc")
        body = html.unescape(page.data.decode("utf-8"))
        self.assertIn("SinpoSmart - 救災救護Worker 後台", body)
        self.assertIn('<details class="task-details">', body)
        self.assertIn("<summary>完整事件</summary>", body)
        self.assertIn("任務司機：曾彥綸", body)
        self.assertIn("同步帳號：8番 曾彥綸 - tyfd01510", body)
        self.assertIn("各站登入帳號", body)
        self.assertIn("8番 曾彥綸 - tyfd01510（任務司機）", body)
        self.assertNotIn("8番 曾彥綸 - tyfd01510（出勤人員）", body)
        self.assertIn("8番 曾彥綸 - tyfd01510（同步帳號）", body)
        self.assertIn("8番 曾彥綸 - C123***789（同步帳號）", body)
        self.assertNotIn("任務司機優先", body)
        self.assertNotIn("司機帳號優先，失敗一次改同步帳號", body)
        self.assertNotIn("回報來源帳號：8番 曾彥綸 - tyfd01510", body)
        self.assertNotIn("公務電腦選取帳號：", body)
        self.assertNotIn("操作人員：", body)
        self.assertNotIn("登入規則：", body)
        self.assertNotIn("工作站登入：", body)
        self.assertNotIn("工作登入帳號：任務司機優先，已保存。", body)
        self.assertNotIn("里程已保存。", body)
        self.assertNotIn("2026-06-12T14:30:00", body)
        self.assertIn("緊急救護-急病 - 桃園市觀音區中山路", body)
        self.assertIn("四站登打成功", body)
        self.assertNotIn("五站登打成功", body)
        self.assertIn("<strong>登打明細</strong>", body)
        self.assertIn(
            "新坡91 / 無 / 出勤 0830 / 返隊 0910 / 曾彥綸 / 54620 / 桃-口罩(片) x2 / 消毒1項",
            body,
        )
        reports = app_module.public_pc_reports()
        self.assertEqual(reports[0]["operator"], "8番 曾彥綸 - tyfd01510")
        self.assertEqual(reports[0]["synced_account"], "8番 曾彥綸 - tyfd01510")
        self.assertEqual(
            reports[0]["site_login_accounts"]["duty_work_log"],
            "8番 曾彥綸 - tyfd01510（任務司機優先）",
        )
        self.assertEqual(
            reports[0]["site_login_accounts"]["consumables"],
            "8番 曾彥綸 - C123***789（同步帳號）",
        )

    def test_admin_public_pc_shows_two_vehicle_task_entries(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        response = self.client.post(
            "/worker/public-pc-task-events",
            headers=worker_headers,
            json={
                "event_id": "evt-two-vehicle",
                "task_id": "local-task-two-vehicle",
                "task": {
                    "task_id": "local-task-two-vehicle",
                    "case_reason": "\u6025\u75c5",
                    "case_address": "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def1\u865f",
                    "case_time": "1024",
                    "vehicle": "\u65b0\u576191",
                    "driver": "\u66fe\u5f65\u7db8",
                    "mileage": "12345",
                    "return_time": "1119",
                    "consumables": {"\u6843-\u53e3\u7f69(\u7247)": 2},
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {
                            "vehicle": "\u65b0\u576191",
                            "driver": "\u66fe\u5f65\u7db8",
                            "mileage": "12345",
                            "return_time": "1119",
                            "patient_summary": "\u7537\u4e00\u540d",
                            "consumables": {"\u6843-\u53e3\u7f69(\u7247)": 2},
                            "disinfection_items": ["\u64e6\u62ed\u6d88\u6bd2", "\u6d88\u6bd2\u5730\u677f"],
                        },
                        {
                            "vehicle": "\u65b0\u576192",
                            "driver": "\u738b\u6631\u52db",
                            "mileage": "23456",
                            "return_time": "1125",
                            "patient_summary": "\u7121",
                            "consumables": {"\u6843-9\u540b\u624b\u5957-L(\u96d9)": 1},
                            "disinfection_items": ["\u64e6\u62ed\u6d88\u6bd2"],
                        },
                    ],
                },
                "action": "\u5efa\u7acb\u4efb\u52d9",
                "status": "created",
            },
        )

        self.assertEqual(response.status_code, 200)
        page = self.client.get("/admin/public-pc")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("1\u8eca", body)
        self.assertIn("2\u8eca", body)
        self.assertIn("\u65b0\u576191 / \u7537 / \u51fa\u52e4 1024 / \u8fd4\u968a 1119 / \u66fe\u5f65\u7db8 / 12345 / \u6843-\u53e3\u7f69(\u7247) x2 / \u6d88\u6bd22\u9805", body)
        self.assertIn("\u65b0\u576192", body)
        self.assertIn("\u65b0\u576192 / \u7121 / \u51fa\u52e4 1024 / \u8fd4\u968a 1125 / \u738b\u6631\u52db / 23456 / \u6843-9\u540b\u624b\u5957-L(\u96d9) x1 / \u6d88\u6bd21\u9805", body)
        self.assertIn("\u738b\u6631\u52db", body)
        self.assertIn("23456", body)
        self.assertIn("\u6843-9\u540b\u624b\u5957-L(\u96d9) x1", body)

    def test_sinposmart_event_api_requires_token(self):
        response = self.client.post("/api/sinposmart/events", json={"event_id": "evt-1"})

        self.assertEqual(response.status_code, 404)

        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        forbidden = self.client.post("/api/sinposmart/events", json={"event_id": "evt-1"})

        self.assertEqual(forbidden.status_code, 403)

    def test_sinposmart_event_api_receives_and_lists_backend_events(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-sinpo-1",
                "occurred_at": f"{fire_day}T09:10:00",
                "record_type": "action_result",
                "actor_no": "8",
                "user_id": "tyfd01510",
                "display_name": "8番 曾彥綸 - tyfd01510",
                "trigger_type": "manual",
                "status": "submitted",
                "item_kind": "工作",
                "item_title": "值班交接",
                "content": "已登打值班交接。",
                "error": "",
                "target": "8番",
                "target_time": "09:10",
                "snapshot": {"actions": [{"title": "值班交接"}], "password": "secret"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ack_id"], "evt-sinpo-1")
        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("SinpoSmart 值班後台", body)
        self.assertIn(fire_day, body)
        self.assertIn("手動", body)
        self.assertIn("8番 曾彥綸", body)
        self.assertNotIn("tyfd01510", body)
        self.assertIn("值班交接", body)
        self.assertNotIn("已登打值班交接。", body)
        self.assertNotIn("secret", body)

    def test_sinposmart_admin_shows_active_unreturned_return_card(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-unreturned-return-active",
                "occurred_at": f"{fire_day}T09:10:00",
                "record_type": "unreturned_return",
                "actor_no": "11",
                "display_name": "11番 測試員",
                "trigger_type": "recovery",
                "status": "retrying",
                "item_kind": "出入",
                "item_title": "出 / 退勤",
                "target": "08番 外勤人員",
                "target_time": "09:10",
                "snapshot": {
                    "queue_id": "queue-test-active",
                    "first_paused_at": f"{fire_day}T08:00:00",
                    "last_attempt_at": f"{fire_day}T09:10:00",
                    "next_retry_at": f"{fire_day}T09:20:00",
                    "expires_at": f"{fire_day}T23:00:00",
                    "last_owner_actor_no": "11",
                    "retry_interval_minutes": 10,
                    "password": "secret",
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        self.assertIn('<details class="section" aria-label="未返隊處理歷程" open>', body)
        self.assertNotIn('<section class="summary-card unreturned-return-card"', body)
        self.assertIn("待處理 1 筆", body)
        self.assertIn("09:10｜出入｜出 / 退勤｜08番 外勤人員", body)
        self.assertIn("重新確認中", body)
        self.assertIn("間隔：10 分鐘", body)
        self.assertNotIn("原值退 未提供", body)
        self.assertNotIn("表定接班 未提供", body)
        self.assertNotIn("secret", body)

    def test_sinposmart_admin_keeps_resolved_unreturned_handoff_history_with_confirmation_source(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        headers = {"X-Credential-Sync-Token": "sync-token"}
        fire_day = datetime.now().date().isoformat()
        events = [
            {
                "event_id": "evt-unreturned-history-pending",
                "occurred_at": f"{fire_day}T10:00:00",
                "record_type": "unreturned_return",
                "actor_no": "7",
                "trigger_type": "due",
                "status": "pending",
                "item_kind": "出入",
                "item_title": "值班交接",
                "target": "7番 原值退",
                "target_time": "10:00",
                "snapshot": {
                    "queue_id": "queue-handoff-history",
                    "first_paused_at": f"{fire_day}T10:00:00",
                    "last_attempt_at": f"{fire_day}T10:00:00",
                    "handoff": {
                        "original_handoff_time": "10:00",
                        "outgoing_person": "7番 原值退",
                        "scheduled_incoming_person": "8番 表定接班",
                        "actual_incoming_person": "8番 表定接班",
                    },
                },
            },
            {
                "event_id": "evt-unreturned-history-retrying",
                "occurred_at": f"{fire_day}T10:10:00",
                "record_type": "unreturned_return",
                "actor_no": "7",
                "trigger_type": "recovery",
                "status": "retrying",
                "item_kind": "出入",
                "item_title": "值班交接",
                "target": "7番 原值退",
                "target_time": "10:00",
                "snapshot": {"queue_id": "queue-handoff-history"},
            },
            {
                "event_id": "evt-unreturned-history-resolved",
                "occurred_at": f"{fire_day}T12:41:00",
                "record_type": "unreturned_return",
                "actor_no": "7",
                "trigger_type": "manual",
                "status": "resolved",
                "item_kind": "出入",
                "item_title": "值班交接",
                "target": "7番 原值退",
                "target_time": "12:41",
                "snapshot": {
                    "queue_id": "queue-handoff-history",
                    "first_paused_at": f"{fire_day}T10:00:00",
                    "last_attempt_at": f"{fire_day}T12:41:00",
                    "handoff": {
                        "original_handoff_time": "10:00",
                        "outgoing_person": "7番 原值退",
                        "scheduled_incoming_person": "8番 表定接班",
                        "actual_incoming_person": "16番 實際接班",
                        "bridge_at": f"{fire_day}T12:41:00",
                        "skipped_scheduled_people": "8番 表定接班",
                        "actual_incoming_people": "16番 實際接班",
                    },
                },
            },
        ]
        for event in events:
            response = self.client.post("/api/sinposmart/events", headers=headers, json=event)
            self.assertEqual(response.status_code, 200)

        body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        self.assertIn("待處理 0 筆", body)
        self.assertIn("未返隊處理歷程", body)
        self.assertIn("10:00｜出入｜值班交接｜7番 原值退", body)
        self.assertIn("原排定值退：7番 原值退", body)
        self.assertIn("原排定接班：8番 表定接班", body)
        self.assertIn("實際接班：16番 實際接班", body)
        self.assertIn("略過的表定接班：8番 表定接班", body)
        self.assertIn("自動偵測未返隊", body)
        self.assertIn("自動返隊確認", body)
        self.assertIn("手動確認返隊", body)

    def test_sinposmart_admin_marks_legacy_unreturned_history_without_fake_handoff_people(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-unreturned-history-legacy",
                "occurred_at": f"{fire_day}T10:00:02",
                "record_type": "unreturned_return",
                "actor_no": "17",
                "trigger_type": "due",
                "status": "resolved",
                "snapshot": {
                    "queue_id": "queue-handoff-legacy",
                    "first_paused_at": f"{fire_day}T10:00:02",
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        self.assertIn("10:00｜未返隊｜舊版事件未提供勤務明細", body)
        self.assertIn("舊版公務電腦上傳的勤務明細不完整", body)
        self.assertNotIn("原值退 未提供", body)
        self.assertNotIn("表定接班 未提供", body)

    def test_sinposmart_admin_shows_worker_credential_sync_without_account_or_password(self):
        worker_token = "0123456789abcdef0123456789abcdef"
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        os.environ["WORKER_TOKEN"] = worker_token
        fire_day = datetime.now().date().isoformat()
        headers = {"X-Credential-Sync-Token": "sync-token"}
        login = self.client.post(
            "/api/sinposmart/events",
            headers=headers,
            json={
                "event_id": "evt-sync-card-login",
                "occurred_at": f"{fire_day}T09:10:00",
                "record_type": "login",
                "status": "ok",
                "actor_no": "8",
                "display_name": "8番 王小明",
            },
        )
        sync_payload = self.credential_sync_payload() | {
            "accounts": [
                {"actor_no": "26", "name": "楊紹文", "user_id": "user26", "password": "pass26"},
                {"actor_no": "8", "name": "王小明", "user_id": "user8", "password": "pass8"},
            ],
        }
        queued = self.client.post("/api/credential-sync", json=sync_payload, headers=headers)
        saved = self.client.post(
            "/worker/credential-sync/sync-test-1/ack",
            json={"status": "saved", "detail": "saved"},
            headers={"X-Worker-Token": worker_token},
        )

        self.assertEqual(login.status_code, 200)
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(saved.status_code, 200)
        status_text = app_module.credential_sync_status_file().read_text(encoding="utf-8")
        success_page = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))
        self.assertLess(success_page.index("登入狀態"), success_page.index("Worker 帳密同步"))
        self.assertLess(success_page.index("Worker 帳密同步"), success_page.index("背景資料比對快照"))
        self.assertLess(success_page.index("王小明"), success_page.index("楊紹文"))
        self.assertIn("王小明", success_page)
        self.assertIn("楊紹文", success_page)
        self.assertIn(f"最後登入：{fire_day}T09:10:00", success_page)
        self.assertIn("近 7 個消防日無登入紀錄", success_page)
        self.assertIn("成功", success_page)
        self.assertNotIn("user8", success_page)
        self.assertNotIn("pass8", success_page)
        self.assertNotIn("user26", success_page)
        self.assertNotIn("pass26", success_page)
        self.assertNotIn("user8", status_text)
        self.assertNotIn("pass8", status_text)
        self.assertNotIn("user26", status_text)
        self.assertNotIn("pass26", status_text)

        failed_payload = self.credential_sync_payload() | {
            "sync_code": "sync-test-2",
            "accounts": [{"actor_no": "8", "name": "李大文", "user_id": "user8b", "password": "pass8b"}],
        }
        queued_again = self.client.post("/api/credential-sync", json=failed_payload, headers=headers)
        failed = self.client.post(
            "/worker/credential-sync/sync-test-2/ack",
            json={"status": "failed", "detail": "disk busy"},
            headers={"X-Worker-Token": worker_token},
        )
        failed_page = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        self.assertEqual(queued_again.status_code, 200)
        self.assertEqual(failed.status_code, 200)
        self.assertIn("王小明", failed_page)
        self.assertIn("楊紹文", failed_page)
        self.assertIn("李大文", failed_page)
        self.assertIn("最後登入：", failed_page)
        self.assertIn("失敗", failed_page)
        self.assertNotIn("user8b", failed_page)
        self.assertNotIn("pass8b", failed_page)

    def test_sinposmart_admin_sections_are_collapsed_by_default(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-collapsed-sections",
                "occurred_at": f"{fire_day}T10:20:00",
                "record_type": "login",
                "status": "ok",
                "actor_no": "8",
                "display_name": "8番 王小明",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        for section_name in ("到點勤務", "使用工具", "登入狀態", "Worker 帳密同步", "背景資料比對快照"):
            self.assertIn(f'<details class="section" aria-label="{section_name}">', body)
        self.assertNotIn('<details class="section" aria-label="到點勤務" open>', body)
        self.assertIn('details.section > summary.section-head', body)
        self.assertIn('details.section[open] > summary.section-head::after', body)

    def test_sinposmart_admin_section_summaries_align_desktop_and_mobile_text(self):
        body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        self.assertIn("grid-template-columns: 160px minmax(0, 1fr) auto;", body)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", body)
        self.assertIn("text-align: left;", body)
        self.assertIn("justify-self: center;", body)

    def test_sinposmart_admin_lists_tool_started_events(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-tool-start",
                "occurred_at": f"{fire_day}T12:10:00",
                "record_type": "tool_action_started",
                "trigger_type": "tool_start",
                "status": "started",
                "actor_no": "8",
                "user_id": "tyfd01510",
                "display_name": "8番 王小明 - tyfd01510",
                "snapshot": {"tool_name": "duty_sheet", "tool_label": "勤務表登打"},
            },
        )

        self.assertEqual(response.status_code, 200)
        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("使用工具", body)
        self.assertIn("勤務表登打", body)
        self.assertIn("開始執行", body)
        self.assertIn("執行中", body)
        self.assertIn("8番 王小明", body)
        self.assertNotIn("tyfd01510", body)
        self.assertIn("工具", body)
        self.assertNotIn("代碼", body)
        self.assertNotIn("duty_sheet", body)
        self.assertNotIn("tool_label", body)
        self.assertNotIn('class="pause-reason"', body)

    def test_sinposmart_admin_shows_tool_failure_reason_and_safe_detail(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-tool-failure-detail",
                "occurred_at": f"{fire_day}T16:31:30",
                "record_type": "tool_action_finished",
                "trigger_type": "tool_finish",
                "status": "failed",
                "error": "Chrome session 建立失敗",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "snapshot": {
                    "tool_name": "monthly_base",
                    "tool_label": "勤務基準表登打",
                    "failure_stage": "browser_start",
                    "failure_detail": "browser_startup",
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        self.assertIn("錯誤原因：Chrome session 建立失敗", body)
        self.assertIn("失敗階段：瀏覽器啟動", body)
        self.assertIn("錯誤詳情：專用瀏覽器啟動或連線未完成", body)
        self.assertNotIn("browser_startup", body)

    def test_sinposmart_admin_combines_tool_start_finish_and_result(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        headers = {"X-Credential-Sync-Token": "sync-token"}
        fire_day = datetime.now().date().isoformat()
        for event in [
            {
                "event_id": "evt-tool-start-finish-web-1",
                "occurred_at": f"{fire_day}T16:30:52",
                "record_type": "tool_action_started",
                "trigger_type": "tool_start",
                "status": "started",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "snapshot": {"tool_name": "duty_sheet", "tool_label": "勤務表登打"},
            },
            {
                "event_id": "evt-tool-start-finish-web-2",
                "occurred_at": f"{fire_day}T16:31:30",
                "record_type": "tool_action_finished",
                "trigger_type": "tool_finish",
                "status": "completed",
                "content": "勤務表登打完成：115/06/19",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "snapshot": {"tool_name": "duty_sheet", "tool_label": "勤務表登打"},
            },
        ]:
            response = self.client.post("/api/sinposmart/events", headers=headers, json=event)
            self.assertEqual(response.status_code, 200)

        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("使用工具", body)
        self.assertIn("勤務表登打", body)
        self.assertIn("開始執行", body)
        self.assertIn("結束執行", body)
        self.assertIn("結果：勤務表登打完成：115/06/19", body)
        self.assertNotIn("duty_sheet", body)

    def test_sinposmart_admin_shows_each_rescue_video_summary_separately(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        headers = {"X-Credential-Sync-Token": "sync-token"}
        fire_day = datetime.now().date().isoformat()
        for run_id, case_count, total_count, usage_seconds in (
            ("rescue-web-run-1", 2, 5, 65),
            ("rescue-web-run-2", 1, 2, 30),
        ):
            shared_snapshot = {
                "tool_name": "rescue_video",
                "tool_label": "救護行車紀錄器",
                "run_id": run_id,
            }
            for record_type, status, snapshot in (
                ("tool_action_started", "started", shared_snapshot),
                (
                    "tool_action_finished",
                    "completed",
                    {
                        **shared_snapshot,
                        "target_date": "2026-08-24",
                        "vehicle": "92",
                        "case_count": case_count,
                        "total_count": total_count,
                        "usage_seconds": usage_seconds,
                    },
                ),
            ):
                response = self.client.post(
                    "/api/sinposmart/events",
                    headers=headers,
                    json={
                        "event_id": f"{run_id}-{record_type}",
                        "occurred_at": f"{fire_day}T16:30:52",
                        "record_type": record_type,
                        "trigger_type": "tool_start" if record_type.endswith("started") else "tool_finish",
                        "status": status,
                        "content": "救護行車紀錄器完成" if record_type.endswith("finished") else "",
                        "actor_no": "27",
                        "display_name": "27番 隊員 林宏為",
                        "snapshot": snapshot,
                    },
                )
                self.assertEqual(response.status_code, 200)

        body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        self.assertEqual(body.count('<h3 class="event-title">救護行車紀錄器</h3>'), 2)
        self.assertIn("日期：2026-08-24", body)
        self.assertIn("車輛：92", body)
        self.assertIn("案件數：2", body)
        self.assertIn("總數量：5", body)
        self.assertIn("使用時間：1 分 5 秒", body)
        self.assertIn("案件數：1", body)
        self.assertIn("總數量：2", body)
        self.assertIn("使用時間：30 秒", body)

    def test_sinposmart_admin_tool_start_shows_waiting_state_not_pause_reason(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-tool-waiting-web",
                "occurred_at": f"{fire_day}T16:30:52",
                "record_type": "tool_action_started",
                "trigger_type": "tool_start",
                "status": "started",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "snapshot": {"tool_name": "duty_sheet", "tool_label": "勤務表登打"},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        self.assertIn("勤務表登打", body)
        self.assertIn("執行中", body)
        self.assertIn("等待狀態：已收到開始執行", body)
        self.assertNotIn("暫停原因：尚未收到工具結束結果", body)

    def test_sinposmart_admin_merges_repeated_events_and_hides_raw_snapshot(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        base_payload = {
            "occurred_at": f"{fire_day}T12:10:00",
            "record_type": "action_result",
            "trigger_type": "manual",
            "status": "submitted",
            "actor_no": "8",
            "user_id": "tyfd01510",
            "display_name": "8番 王小明 - tyfd01510",
            "item_kind": "出入",
            "item_title": "休息後退勤",
            "content": "已登打休息後退勤",
            "target": "4",
            "target_time": "06:00",
            "snapshot": {"tool_name": "duty_sheet", "password": "secret"},
        }
        first = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={**base_payload, "event_id": "evt-merge-web-1"},
        )
        second = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={**base_payload, "event_id": "evt-merge-web-2", "occurred_at": f"{fire_day}T12:11:00"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("合併 2 次", body)
        self.assertNotIn("快照資料已收到", body)
        self.assertIn("8番 王小明", body)
        self.assertNotIn("tyfd01510", body)
        self.assertNotIn("duty_sheet", body)
        self.assertNotIn("secret", body)

    def test_sinposmart_admin_collapses_queue_snapshot_and_login_noise(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        headers = {"X-Credential-Sync-Token": "sync-token"}
        fire_day = datetime.now().date().isoformat()
        events = [
            {
                "event_id": "evt-login-27",
                "occurred_at": f"{fire_day}T16:30:40",
                "record_type": "login",
                "status": "ok",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為 - tyfd01027",
            },
            {
                "event_id": "evt-schedule-27",
                "occurred_at": f"{fire_day}T16:31:12",
                "record_type": "schedule_snapshot",
                "trigger_type": "schedule",
                "status": "success",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "snapshot": {"tool_name": "duty_sheet", "raw": {"code": "duty_sheet"}},
            },
            {
                "event_id": "evt-queue-27",
                "occurred_at": f"{fire_day}T18:00:00",
                "record_type": "action_queued",
                "trigger_type": "due",
                "status": "pending_write_automation",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "item_kind": "出入",
                "item_title": "值退 / 值退｜27 林宏為",
                "target": "27番 林宏為（隊員）",
                "target_time": "18:00",
            },
            {
                "event_id": "evt-result-27",
                "occurred_at": f"{fire_day}T18:00:22",
                "record_type": "action_result",
                "trigger_type": "due",
                "status": "submitted",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "item_kind": "出入",
                "item_title": "值退 / 值退｜27 林宏為",
                "target": "27番 林宏為（隊員）",
                "target_time": "18:00",
            },
        ]
        for event in events:
            response = self.client.post("/api/sinposmart/events", headers=headers, json=event)
            self.assertEqual(response.status_code, 200)

        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("到點勤務", body)
        self.assertIn("背景資料比對快照", body)
        self.assertIn("登入狀態", body)
        self.assertIn("18:00｜出入｜值退 / 值退｜27 林宏為", body)
        self.assertIn("開始送出", body)
        self.assertIn("完成結果", body)
        self.assertIn("已登打", body)
        self.assertIn("整日勤務", body)
        self.assertIn("27番 隊員 林宏為", body)
        self.assertIn("18:00｜出入｜值退 / 值退｜27 林宏為｜27番 林宏為", body)
        self.assertNotIn("27番 林宏為（隊員）", body)
        self.assertNotIn("暫停原因", body)
        self.assertNotIn("加入佇列", body)
        self.assertNotIn("pending_write_automation", body)
        self.assertNotIn("18:00｜出入｜27番 林宏為", body)
        self.assertNotIn("快照內容", body)
        self.assertNotIn("代碼", body)
        self.assertNotIn("duty_sheet", body)
        self.assertNotIn("tyfd01027", body)

    def test_sinposmart_admin_splits_schedule_snapshot_by_fire_day_scope(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day_date = datetime.now().date()
        fire_day = fire_day_date.isoformat()
        fire_day_roc = f"{fire_day_date.year - 1911:03d}{fire_day_date.month:02d}{fire_day_date.day:02d}"
        next_fire_day_date = fire_day_date + timedelta(days=1)
        next_fire_day_roc = f"{next_fire_day_date.year - 1911:03d}{next_fire_day_date.month:02d}{next_fire_day_date.day:02d}"
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-schedule-days-web",
                "occurred_at": f"{fire_day}T22:00:33",
                "fire_day": fire_day,
                "record_type": "schedule_snapshot",
                "trigger_type": "schedule",
                "status": "success",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "snapshot": {
                    "days": [
                        {"target_date": fire_day_roc, "action_count": 3},
                        {"target_date": next_fire_day_roc, "action_count": 5},
                    ]
                },
            },
        )
        self.assertEqual(response.status_code, 200)

        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("當日整日勤務", body)
        self.assertIn("隔日整日勤務", body)
        self.assertNotIn("action_count", body)

    def test_sinposmart_admin_shows_background_error_as_failed(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-background-error-web",
                "occurred_at": f"{fire_day}T19:30:00",
                "record_type": "error",
                "trigger_type": "schedule",
                "status": "failed",
                "actor_no": "5",
                "display_name": "5番 小隊長 張鴻志",
                "error": "勤務表背景更新失敗",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = html.unescape(self.client.get("/admin/sinposmart").data.decode("utf-8"))

        self.assertIn("背景資料比對快照", body)
        self.assertIn("失敗", body)
        self.assertIn("錯誤原因：勤務表背景更新失敗", body)

    def test_sinposmart_admin_waiting_event_shows_waiting_state_not_pause_reason(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-queue-only-web",
                "occurred_at": f"{fire_day}T19:00:00",
                "record_type": "action_queued",
                "trigger_type": "due",
                "status": "pending_write_automation",
                "actor_no": "5",
                "display_name": "5番 小隊長 張鴻志",
                "item_kind": "出入",
                "item_title": "值班 / 值班｜05 張鴻志",
                "target": "5番 張鴻志（小隊長）",
                "target_time": "19:00",
            },
        )

        self.assertEqual(response.status_code, 200)
        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("到點勤務", body)
        self.assertIn("開始送出", body)
        self.assertIn("等待完成回傳", body)
        self.assertIn("等待狀態：已收到開始送出", body)
        self.assertNotIn("暫停原因：尚未收到完成結果", body)
        self.assertNotIn("pending_write_automation", body)
        self.assertNotIn("加入佇列", body)

    def test_sinposmart_admin_login_section_can_show_logout(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        headers = {"X-Credential-Sync-Token": "sync-token"}
        fire_day = datetime.now().date().isoformat()
        for event in [
            {
                "event_id": "evt-login-web",
                "occurred_at": f"{fire_day}T16:30:40",
                "record_type": "login",
                "status": "ok",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為 - tyfd01027",
            },
            {
                "event_id": "evt-logout-web",
                "occurred_at": f"{fire_day}T18:05:12",
                "record_type": "logout",
                "status": "ok",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
            },
        ]:
            response = self.client.post("/api/sinposmart/events", headers=headers, json=event)
            self.assertEqual(response.status_code, 200)

        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("登入狀態", body)
        self.assertIn("27番 隊員 林宏為", body)
        self.assertIn("登出", body)
        self.assertIn("18:05:12", body)
        self.assertNotIn("tyfd01027", body)

    def test_sinposmart_admin_login_section_shows_update_logout_context(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        headers = {"X-Credential-Sync-Token": "sync-token"}
        fire_day = datetime.now().date().isoformat()
        for event in [
            {
                "event_id": "evt-update-logout-first",
                "occurred_at": f"{fire_day}T07:04:00",
                "record_type": "logout",
                "trigger_type": "manual",
                "status": "ok",
                "actor_no": "8",
                "display_name": "\u0038\u756a \u968a\u54e1 \u66fe\u5f65\u7db8",
            },
            {
                "event_id": "evt-update-logout-second",
                "occurred_at": f"{fire_day}T07:19:54",
                "record_type": "logout",
                "trigger_type": "update",
                "status": "ok",
                "actor_no": "8",
                "display_name": "\u0038\u756a \u968a\u54e1 \u66fe\u5f65\u7db8",
                "content": "\u66f4\u65b0\u524d\u767b\u51fa",
            },
            {
                "event_id": "evt-update-logout-second",
                "occurred_at": f"{fire_day}T07:20:10",
                "record_type": "logout",
                "trigger_type": "update",
                "status": "ok",
                "actor_no": "8",
                "display_name": "\u0038\u756a \u968a\u54e1 \u66fe\u5f65\u7db8",
                "content": "\u66f4\u65b0\u524d\u767b\u51fa",
            },
        ]:
            response = self.client.post("/api/sinposmart/events", headers=headers, json=event)
            self.assertEqual(response.status_code, 200)

        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("\u0038\u756a \u968a\u54e1 \u66fe\u5f65\u7db8", body)
        self.assertIn("\u66f4\u65b0\u524d\u767b\u51fa", body)
        self.assertIn("\u767b\u51fa \u00b7 \u66f4\u65b0", body)
        self.assertNotIn("\u91cd\u8907 2 \u6b21", body)

    def test_sinposmart_admin_shows_login_logout_times_and_sinposmart_version(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        original_version_info = getattr(app_module, "sinposmart_admin_version_info", None)
        app_module.sinposmart_admin_version_info = lambda _selected_day=None: {
            "label": "SinpoSmart 公務電腦",
            "version": "2026.06.19.0730",
            "detail": "GitHub latest",
        }
        try:
            headers = {"X-Credential-Sync-Token": "sync-token"}
            for event in [
                {
                    "event_id": "evt-login-logout-version-web-1",
                    "occurred_at": f"{fire_day}T16:30:40",
                    "record_type": "login",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為 - tyfd01027",
                },
                {
                    "event_id": "evt-login-logout-version-web-2",
                    "occurred_at": f"{fire_day}T18:05:12",
                    "record_type": "logout",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                },
            ]:
                response = self.client.post("/api/sinposmart/events", headers=headers, json=event)
                self.assertEqual(response.status_code, 200)

            page = self.client.get("/admin/sinposmart")
            body = html.unescape(page.data.decode("utf-8"))

            self.assertIn("系統版本", body)
            self.assertIn("SinpoSmart 公務電腦", body)
            self.assertIn("2026.06.19.0730", body)
            self.assertNotIn("救護 worker", body)
            self.assertIn("登入時間", body)
            self.assertIn("登出時間", body)
            self.assertIn("16:30:40", body)
            self.assertIn("18:05:12", body)
        finally:
            if original_version_info is None:
                delattr(app_module, "sinposmart_admin_version_info")
            else:
                app_module.sinposmart_admin_version_info = original_version_info

    def test_sinposmart_admin_prefers_reported_installed_version(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        fire_day = datetime.now().date().isoformat()
        response = self.client.post(
            "/api/sinposmart/events",
            headers={"X-Credential-Sync-Token": "sync-token"},
            json={
                "event_id": "evt-sinposmart-installed-version",
                "occurred_at": f"{fire_day}T20:00:00",
                "record_type": "login",
                "status": "ok",
                "actor_no": "5",
                "display_name": "5番 小隊長 張鴻志",
                "snapshot": {"app_version": "2026.06.18.2201-installed"},
            },
        )

        self.assertEqual(response.status_code, 200)
        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("SinpoSmart 公務電腦", body)
        self.assertIn("2026.06.18.2201-installed", body)
        self.assertIn("公務電腦已安裝", body)

    def test_sinposmart_admin_login_section_prefers_person_name_over_account(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        headers = {"X-Credential-Sync-Token": "sync-token"}
        fire_day = datetime.now().date().isoformat()
        for event in [
            {
                "event_id": "evt-login-account-web",
                "occurred_at": f"{fire_day}T11:08:39",
                "record_type": "login",
                "status": "ok",
                "actor_no": "8",
                "display_name": "8番 tyfd01510",
            },
            {
                "event_id": "evt-login-name-web",
                "occurred_at": f"{fire_day}T10:47:28",
                "record_type": "login",
                "status": "ok",
                "actor_no": "8",
                "display_name": "8番 隊員 曾彥綸",
            },
        ]:
            response = self.client.post("/api/sinposmart/events", headers=headers, json=event)
            self.assertEqual(response.status_code, 200)

        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("登入狀態", body)
        self.assertIn("8番 隊員 曾彥綸", body)
        self.assertIn("11:08:39", body)
        self.assertIn("10:47:28", body)
        self.assertNotIn("8番 tyfd01510", body)

    def test_sinposmart_backend_hides_old_fire_days(self):
        os.environ["CREDENTIAL_SYNC_TOKEN"] = "sync-token"
        headers = {"X-Credential-Sync-Token": "sync-token"}
        old_fire_day = (datetime.now().date() - timedelta(days=30)).isoformat()
        current_fire_day = datetime.now().date().isoformat()
        old_response = self.client.post(
            "/api/sinposmart/events",
            headers=headers,
            json={
                "event_id": "evt-old-hidden",
                "occurred_at": f"{old_fire_day}T09:00:00",
                "record_type": "login",
                "status": "ok",
            },
        )
        current_response = self.client.post(
            "/api/sinposmart/events",
            headers=headers,
            json={
                "event_id": "evt-current-visible",
                "occurred_at": f"{current_fire_day}T09:00:00",
                "record_type": "login",
                "status": "ok",
            },
        )

        self.assertEqual(old_response.status_code, 200)
        self.assertEqual(current_response.status_code, 200)
        page = self.client.get("/admin/sinposmart")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertNotIn(old_fire_day, body)
        self.assertIn(current_fire_day, body)

    def test_admin_public_pc_deduplicates_same_event_id(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        payload = {
            "event_id": "evt-dedupe-1",
            "task_id": "local-task-2",
            "task": {
                "task_id": "local-task-2",
                "case_reason": "?亦?",
                "case_address": "獢?撣??喳?",
            },
            "action": "???餅???",
            "status": "desktop_fast_completed",
        }

        first = self.client.post("/worker/public-pc-task-events", headers=worker_headers, json=payload)
        second = self.client.post("/worker/public-pc-task-events", headers=worker_headers, json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        reports = app_module.public_pc_reports()
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(reports[0]["events"]), 1)
        self.assertEqual(reports[0]["events"][0]["event_id"], "evt-dedupe-1")

    def test_admin_public_pc_reconciliation_reclassifies_task_and_preserves_failure_event(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        task = {
            "task_id": "legacy-reclassified-task",
            "case_reason": "急病",
            "case_address": "校正案件地址",
            "vehicle": "新坡92",
        }
        failed = self.client.post(
            "/worker/public-pc-task-events",
            headers=headers,
            json={
                "event_id": "evt-legacy-failed",
                "task_id": task["task_id"],
                "task": task,
                "action": "四站登打部分失敗",
                "status": "consumables_failed",
                "detail": "耗材儲存未取得明確成功回應：未出現確認訊息",
                "overall_status": "desktop_fast_completed_with_errors",
                "site_statuses": {
                    "duty_work_log": {"status": "duty_work_log_saved"},
                    "vehicle_mileage": {"status": "vehicle_mileage_saved"},
                    "consumables": {"status": "consumables_failed"},
                    "disinfection": {"status": "disinfection_saved"},
                },
            },
        )
        corrected = self.client.post(
            "/worker/public-pc-task-events",
            headers=headers,
            json={
                "event_id": "evt-legacy-corrected",
                "task_id": task["task_id"],
                "task": task,
                "action": "舊版無提示儲存狀態自動校正",
                "status": "legacy_silent_save_reconciled",
                "detail": "舊版無提示儲存誤判已修正：耗材。",
                "overall_status": "desktop_fast_completed",
                "site_statuses": {
                    "duty_work_log": {"status": "duty_work_log_saved"},
                    "vehicle_mileage": {"status": "vehicle_mileage_saved"},
                    "consumables": {"status": "consumables_saved"},
                    "disinfection": {"status": "disinfection_saved"},
                },
            },
        )

        reports = app_module.public_pc_reports()
        failed_page = html.unescape(self.client.get("/admin/public-pc?result=failed").get_data(as_text=True))
        success_page = html.unescape(self.client.get("/admin/public-pc?result=success").get_data(as_text=True))

        self.assertEqual(failed.status_code, 200)
        self.assertEqual(corrected.status_code, 200)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["overall_status"], "desktop_fast_completed")
        self.assertEqual(reports[0]["site_statuses"]["consumables"]["status"], "consumables_saved")
        self.assertEqual([event["event_id"] for event in reports[0]["events"]], [
            "evt-legacy-failed",
            "evt-legacy-corrected",
        ])
        self.assertNotIn("校正案件地址", failed_page)
        self.assertIn("校正案件地址", success_page)
        self.assertIn("成功案件 1", success_page)
        self.assertIn("失敗案件 0", success_page)

    def test_admin_main_status_uses_full_completion_not_last_single_site_action(self):
        task = app_module.request_from_form(self.valid_task_data()).to_dict()
        site_statuses = {
            site_key: {
                "status": f"{site_key}_saved",
                "detail": "saved",
            }
            for site_key in (
                "duty_work_log",
                "vehicle_mileage",
                "consumables",
                "disinfection",
            )
        }
        site_statuses["fuel_record"] = {"status": "not_started", "detail": ""}
        app_module.upsert_public_pc_report(
            {
                "event_id": "single-final-event",
                "task_id": task["task_id"],
                "task": task,
                "title": "最後單站補打",
                "action": "單站補打成功：消毒",
                "status": "disinfection_saved",
                "overall_status": "desktop_fast_completed",
                "site_statuses": site_statuses,
            }
        )

        body = html.unescape(
            self.client.get("/admin/public-pc").get_data(as_text=True)
        )

        self.assertIn("四站登打完成", body)
        self.assertIn("單站補打成功：消毒", body)
        report = app_module.public_pc_reports()[0]
        self.assertTrue(report["completion"]["all_complete"])
        self.assertEqual(report["completion"]["site_count_label"], "四站")

    def test_admin_success_filter_excludes_premature_overall_success(self):
        task = app_module.request_from_form(self.valid_task_data()).to_dict()
        app_module.upsert_public_pc_report(
            {
                "event_id": "premature-success",
                "task_id": task["task_id"],
                "task": task,
                "title": "只有消毒完成",
                "action": "單站補打成功：消毒",
                "status": "desktop_fast_completed",
                "overall_status": "desktop_fast_completed",
                "completion": {"all_complete": True},
                "site_statuses": {
                    "duty_work_log": {"status": "not_started"},
                    "vehicle_mileage": {"status": "not_started"},
                    "fuel_record": {"status": "not_started"},
                    "consumables": {"status": "not_started"},
                    "disinfection": {"status": "disinfection_saved"},
                },
            }
        )

        success_body = html.unescape(
            self.client.get("/admin/public-pc?result=success").get_data(
                as_text=True
            )
        )

        self.assertNotIn("只有消毒完成", success_body)
        report = app_module.public_pc_reports()[0]
        self.assertFalse(report["completion"]["all_complete"])

    def test_remote_update_command_is_idempotent_and_worker_authenticated(self):
        os.environ["WORKER_TOKEN"] = "test-token"

        first = self.post_remote_update()
        second = self.post_remote_update()

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        command = app_module.read_remote_update_command()
        request_id = command["request_id"]
        self.assertEqual(command["status"], "pending")
        self.assertEqual(self.client.get("/worker/remote-update").status_code, 403)

        response = self.client.get(
            "/worker/remote-update?worker_id=PC-01&package_version=2026.07.10.1950",
            headers={"X-Worker-Token": "test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["command"]["request_id"], request_id)
        self.assertEqual(app_module.read_remote_update_command()["worker_id"], "PC-01")
        self.assertEqual(app_module.read_remote_update_command()["before_version"], "2026.07.10.1950")

    def test_remote_update_command_is_owned_by_first_worker_that_claims_it(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        self.post_remote_update()
        headers = {"X-Worker-Token": "test-token"}

        first = self.client.get("/worker/remote-update?worker_id=PC-01", headers=headers)
        second = self.client.get("/worker/remote-update?worker_id=PC-02", headers=headers)
        request_id = first.get_json()["command"]["request_id"]
        foreign_status = self.client.post(
            f"/worker/remote-update/{request_id}/status",
            headers=headers,
            json={"status": "updating", "worker_id": "PC-02", "detail": "foreign"},
        )

        self.assertIsNone(second.get_json()["command"])
        self.assertEqual(foreign_status.status_code, 409)
        command = app_module.read_remote_update_command()
        self.assertEqual(command["worker_id"], "PC-01")
        self.assertEqual(command["status"], "pending")

    def test_remote_update_status_requires_token_and_valid_transition(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        self.post_remote_update()
        request_id = app_module.read_remote_update_command()["request_id"]
        status_url = f"/worker/remote-update/{request_id}/status"

        self.assertEqual(self.client.post(status_url, json={"status": "waiting_busy"}).status_code, 403)
        self.assertEqual(
            self.client.post(
                status_url,
                headers={"X-Worker-Token": "test-token"},
                json={"status": "unknown"},
            ).status_code,
            400,
        )

        waiting = self.client.post(
            status_url,
            headers={"X-Worker-Token": "test-token"},
            json={"status": "waiting_busy", "detail": "勤務登打仍在執行。", "worker_id": "PC-01"},
        )
        updating = self.client.post(
            status_url,
            headers={"X-Worker-Token": "test-token"},
            json={"status": "updating", "detail": "開始背景更新。", "worker_id": "PC-01"},
        )
        completed = self.client.post(
            status_url,
            headers={"X-Worker-Token": "test-token"},
            json={
                "status": "completed",
                "detail": "遠端更新完成。",
                "worker_id": "PC-01",
                "installed_version": "2026.07.11.1548",
            },
        )

        self.assertEqual(waiting.status_code, 200)
        self.assertEqual(updating.status_code, 200)
        self.assertEqual(completed.status_code, 200)
        command = app_module.read_remote_update_command()
        self.assertEqual(command["status"], "completed")
        self.assertEqual(command["installed_version"], "2026.07.11.1548")
        reopened = self.client.post(
            status_url,
            headers={"X-Worker-Token": "test-token"},
            json={"status": "waiting_idle", "detail": "late message"},
        )
        repeated = self.client.post(
            status_url,
            headers={"X-Worker-Token": "test-token"},
            json={"status": "completed", "detail": "duplicate result"},
        )
        self.assertEqual(reopened.status_code, 409)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(app_module.read_remote_update_command()["detail"], "遠端更新完成。")
        self.assertIsNone(
            self.client.get(
                "/worker/remote-update?worker_id=PC-01",
                headers={"X-Worker-Token": "test-token"},
            ).get_json()["command"]
        )
        self.assertEqual(
            self.client.post(
                "/worker/remote-update/not-current/status",
                headers={"X-Worker-Token": "test-token"},
                json={"status": "failed"},
            ).status_code,
            404,
        )

    def test_remote_update_active_command_expires_after_stale_limit(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        with mock.patch.dict(os.environ, {"REMOTE_UPDATE_STALE_SECONDS": "60"}):
            self.post_remote_update()
            command = app_module.read_remote_update_command()
            command["updated_at"] = (datetime.now() - timedelta(seconds=61)).isoformat(timespec="seconds")
            app_module.write_json_atomic(app_module.remote_update_command_file(), command)

            response = self.client.get(
                "/worker/remote-update?worker_id=PC-01",
                headers={"X-Worker-Token": "test-token"},
            )

        self.assertIsNone(response.get_json()["command"])
        self.assertEqual(app_module.read_remote_update_command()["status"], "timed_out")

    def test_worker_identity_requires_token_and_is_stable(self):
        self.assertEqual(self.client.get("/worker/identity").status_code, 403)
        os.environ["WORKER_TOKEN"] = "test-token"

        first = self.client.get("/worker/identity", headers={"X-Worker-Token": "test-token"}).get_json()
        second = self.client.get("/worker/identity", headers={"X-Worker-Token": "test-token"}).get_json()

        self.assertEqual(first["server"]["instance_id"], second["server"]["instance_id"])
        self.assertNotEqual(first["server"]["instance_id"], "")

    def test_worker_control_requires_token_and_valid_schema(self):
        self.assertEqual(self.post_worker_control({}).status_code, 403)
        os.environ["WORKER_TOKEN"] = "test-token"
        self.assertEqual(self.post_worker_control({"state": "online"}).status_code, 400)
        self.assertEqual(self.post_worker_control([]).status_code, 400)
        empty_worker = self._valid_control_payload()
        empty_worker["worker_id"] = ""
        self.assertEqual(self.post_worker_control(empty_worker).status_code, 400)
        invalid_route = self._valid_control_payload()
        invalid_route["route"] = []
        self.assertEqual(self.post_worker_control(invalid_route).status_code, 400)

        response = self.post_worker_control(
            self._valid_control_payload(
                route={"name": "lan", "identity_status": "verified", "instance_id": "will-be-replaced"}
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.get_json()["server"]["instance_id"], "will-be-replaced")

    def test_worker_control_claims_only_verified_current_instance(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        self.post_remote_update()
        server = self.client.get("/worker/identity", headers={"X-Worker-Token": "test-token"}).get_json()["server"]
        payload = self._valid_control_payload(
            route={
                "name": "tailscale",
                "identity_status": "verified",
                "instance_id": server["instance_id"],
            }
        )

        response = self.post_worker_control(payload)

        self.assertEqual(response.get_json()["command_delivery"], "claimed")
        self.assertEqual(response.get_json()["command"]["worker_id"], "PC-01")

    def test_worker_control_keeps_heartbeat_but_refuses_unverified_command_claim(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        self.post_remote_update()

        response = self.post_worker_control(
            self._valid_control_payload(
                route={"name": "lan", "identity_status": "unverified", "instance_id": ""}
            )
        )

        self.assertIsNone(response.get_json()["command"])
        self.assertEqual(response.get_json()["command_delivery"], "unverified_route")
        self.assertEqual(app_module.worker_heartbeat_admin_view([])["worker_id"], "PC-01")

    def test_worker_control_applies_safe_embedded_remote_update_status(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        self.post_remote_update()
        request_id = app_module.read_remote_update_command()["request_id"]
        server = self.client.get("/worker/identity", headers={"X-Worker-Token": "test-token"}).get_json()["server"]
        payload = self._valid_control_payload(
            route={
                "name": "tailscale",
                "identity_status": "verified",
                "instance_id": server["instance_id"],
            }
        )
        payload["remote_update"] = {
            "request_id": request_id,
            "status": "waiting_busy",
            "detail": "勤務登打仍在執行。",
        }

        response = self.post_worker_control(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["remote_update_delivery"], "updated")
        self.assertEqual(response.get_json()["command"]["status"], "waiting_busy")
        self.assertEqual(app_module.worker_heartbeat_admin_view([])["worker_id"], "PC-01")

    def test_worker_control_unverified_embedded_status_does_not_claim_command(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        self.post_remote_update()
        request_id = app_module.read_remote_update_command()["request_id"]
        payload = self._valid_control_payload()
        payload["remote_update"] = {
            "request_id": request_id,
            "status": "waiting_busy",
            "detail": "不應領取命令。",
        }

        response = self.post_worker_control(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["command_delivery"], "unverified_route")
        self.assertEqual(response.get_json()["remote_update_delivery"], "unverified_route")
        self.assertIsNone(response.get_json()["command"])
        command = app_module.read_remote_update_command()
        self.assertEqual(command["worker_id"], "")
        self.assertEqual(command["status"], "pending")
        self.assertEqual(app_module.worker_heartbeat_admin_view([])["worker_id"], "PC-01")

    def test_worker_control_keeps_heartbeat_when_embedded_status_is_stale(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        server = self.client.get("/worker/identity", headers={"X-Worker-Token": "test-token"}).get_json()["server"]
        payload = self._valid_control_payload(
            route={
                "name": "tailscale",
                "identity_status": "verified",
                "instance_id": server["instance_id"],
            }
        )
        payload["remote_update"] = {
            "request_id": "not-current",
            "status": "waiting_idle",
            "detail": "過期通知。",
        }

        response = self.post_worker_control(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["remote_update_delivery"], "not_found")
        self.assertEqual(app_module.worker_heartbeat_admin_view([])["worker_id"], "PC-01")

    def test_worker_heartbeat_admin_view_uses_server_received_45_second_threshold(self):
        now = datetime(2026, 7, 15, 16, 0, 0)
        app_module._upsert_worker_heartbeat_unlocked(self._valid_control_payload(), now - timedelta(seconds=44))

        self.assertTrue(app_module.worker_heartbeat_admin_view([], now=now)["online"])
        self.assertFalse(app_module.worker_heartbeat_admin_view([], now=now + timedelta(seconds=2))["online"])

    def test_admin_public_pc_separates_nas_heartbeat_and_task_report_versions(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        with mock.patch.object(app_module, "package_version", return_value="NAS-2026.07.15"):
            self.post_remote_update()
            self.post_worker_control(self._valid_control_payload())
            response = self.client.post(
                "/worker/public-pc-task-events",
                headers={"X-Worker-Token": "test-token"},
                json={
                    "event_id": "evt-task-report-version",
                    "task_id": "task-report-version",
                    "task": {"task_id": "task-report-version", "case_reason": "急病"},
                    "worker_id": "PC-01",
                    "package_version": "TASK-2026.07.14",
                    "action": "建立任務",
                    "status": "created",
                },
            )
            body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("NAS-2026.07.15", body)
        self.assertIn("公務電腦版本：2026.07.15.1326", body)
        self.assertIn("最後任務回報版本：TASK-2026.07.14", body)
        self.assertNotIn("目前版本：", body)

    def test_public_pc_reports_keep_all_statuses_for_fourteen_days(self):
        now = datetime(2026, 7, 10, 18, 0, 0)
        path = app_module.public_pc_report_file()
        app_module.write_json_atomic(
            path,
            {
                "tasks": [
                    {
                        "task_id": "recent-success",
                        "updated_at": (now - timedelta(days=13, hours=23)).isoformat(timespec="seconds"),
                        "overall_status": "desktop_fast_completed",
                    },
                    {
                        "task_id": "recent-failed",
                        "updated_at": (now - timedelta(days=13, hours=22)).isoformat(timespec="seconds"),
                        "overall_status": "desktop_fast_completed_with_errors",
                    },
                    {
                        "task_id": "expired-success",
                        "updated_at": (now - timedelta(days=14, minutes=1)).isoformat(timespec="seconds"),
                        "overall_status": "desktop_fast_running",
                    },
                    {
                        "task_id": "expired-failed",
                        "updated_at": (now - timedelta(days=15)).isoformat(timespec="seconds"),
                        "overall_status": "desktop_fast_completed_with_errors",
                    },
                ]
            },
        )

        reports = app_module.public_pc_reports(now=now)

        self.assertEqual([report["task_id"] for report in reports], ["recent-failed", "recent-success"])
        for report_path in (path, app_module.public_pc_report_backup_file()):
            stored = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [report["task_id"] for report in stored["tasks"]],
                ["recent-success", "recent-failed"],
            )

    def test_public_pc_reports_do_not_truncate_recent_history(self):
        for index in range(101):
            app_module.upsert_public_pc_report(
                {
                    "event_id": f"evt-recent-{index}",
                    "task_id": f"recent-{index}",
                    "task": {"task_id": f"recent-{index}", "case_reason": "急病"},
                    "status": "desktop_fast_completed",
                    "overall_status": "desktop_fast_completed",
                }
            )

        self.assertEqual(len(app_module.public_pc_reports()), 101)

    def test_public_pc_reports_recover_from_backup(self):
        now = datetime(2026, 7, 10, 18, 0, 0)
        main_path = app_module.public_pc_report_file()
        backup_path = app_module.public_pc_report_backup_file()
        main_path.parent.mkdir(parents=True, exist_ok=True)
        main_path.write_text("{broken", encoding="utf-8")
        app_module.write_json_atomic(
            backup_path,
            {
                "tasks": [
                    {
                        "task_id": "from-backup",
                        "updated_at": now.isoformat(timespec="seconds"),
                        "overall_status": "desktop_fast_completed",
                    }
                ]
            },
        )

        reports = app_module.public_pc_reports(now=now)

        self.assertEqual([report["task_id"] for report in reports], ["from-backup"])

    def test_public_pc_report_concurrent_upserts_keep_all_tasks(self):
        def insert(index: int) -> None:
            app_module.upsert_public_pc_report(
                {
                    "event_id": f"evt-concurrent-{index}",
                    "task_id": f"concurrent-{index}",
                    "task": {"task_id": f"concurrent-{index}", "case_reason": "急病"},
                    "status": "desktop_fast_completed",
                    "overall_status": "desktop_fast_completed",
                }
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(insert, range(24)))

        reports = app_module.public_pc_reports()
        self.assertEqual(len(reports), 24)
        self.assertEqual(len({report["task_id"] for report in reports}), 24)

    def test_admin_public_pc_filters_success_and_failed_reports(self):
        samples = [
            ("success-task", "成功案件樣本", "desktop_fast_completed"),
            ("failed-task", "失敗案件樣本", "desktop_fast_completed_with_errors"),
            ("running-task", "執行中案件樣本", "desktop_fast_running"),
        ]
        for task_id, reason, status in samples:
            site_statuses = {
                site_key: {"status": "not_started"}
                for site_key in (
                    "duty_work_log",
                    "vehicle_mileage",
                    "fuel_record",
                    "consumables",
                    "disinfection",
                )
            }
            if status == "desktop_fast_completed":
                for site_key in (
                    "duty_work_log",
                    "vehicle_mileage",
                    "consumables",
                    "disinfection",
                ):
                    site_statuses[site_key] = {
                        "status": f"{site_key}_saved"
                    }
            elif status == "desktop_fast_completed_with_errors":
                site_statuses["consumables"] = {
                    "status": "consumables_failed"
                }
            else:
                site_statuses["duty_work_log"] = {
                    "status": "duty_work_log_running"
                }
            app_module.upsert_public_pc_report(
                {
                    "event_id": f"evt-{task_id}",
                    "task_id": task_id,
                    "title": reason,
                    "task": {"task_id": task_id, "case_reason": reason},
                    "status": status,
                    "overall_status": status,
                    "site_statuses": site_statuses,
                }
            )

        all_body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))
        success_body = html.unescape(self.client.get("/admin/public-pc?result=success").data.decode("utf-8"))
        failed_body = html.unescape(self.client.get("/admin/public-pc?result=failed").data.decode("utf-8"))

        self.assertIn("成功案件樣本", all_body)
        self.assertIn("失敗案件樣本", all_body)
        self.assertIn("執行中案件樣本", all_body)
        self.assertIn('href="/admin/public-pc?result=success"', all_body)
        self.assertIn('href="/admin/public-pc?result=failed"', all_body)
        self.assertIn("成功案件 1", all_body)
        self.assertIn("失敗案件 1", all_body)
        self.assertIn("成功案件樣本", success_body)
        self.assertNotIn("失敗案件樣本", success_body)
        self.assertNotIn("執行中案件樣本", success_body)
        self.assertIn("失敗案件樣本", failed_body)
        self.assertNotIn("成功案件樣本", failed_body)
        self.assertNotIn("執行中案件樣本", failed_body)

    def test_admin_public_pc_service_filter_cross_composes_with_result_filter(self):
        samples = [
            ("ems-filter", "救護篩選樣本", "ems", "desktop_fast_completed", "duty_work_log_saved"),
            ("disaster-filter", "救災篩選樣本", "disaster", "desktop_fast_completed_with_errors", "duty_work_log_failed"),
        ]
        for task_id, title, service_type, overall_status, work_status in samples:
            task = {
                "task_id": task_id,
                "service_type": service_type,
                "case_reason": title,
                "case_address": "桃園市觀音區",
                "commander": "指揮官甲" if service_type == "disaster" else "",
            }
            site_statuses = {
                "duty_work_log": {"status": work_status},
                "vehicle_mileage": {"status": "vehicle_mileage_saved"},
                "fuel_record": {"status": "not_applicable"},
                "consumables": {"status": "consumables_saved" if service_type == "ems" else "not_applicable"},
                "disinfection": {"status": "disinfection_saved" if service_type == "ems" else "not_applicable"},
            }
            app_module.upsert_public_pc_report(
                {
                    "event_id": f"evt-{task_id}",
                    "task_id": task_id,
                    "title": title,
                    "task": task,
                    "overall_status": overall_status,
                    "site_statuses": site_statuses,
                }
            )

        body = html.unescape(
            self.client.get("/admin/public-pc?service=disaster&result=failed").data.decode("utf-8")
        )

        self.assertIn("SinpoSmart - 救災救護Worker 後台", body)
        self.assertIn("救災篩選樣本", body)
        self.assertNotIn("救護篩選樣本", body)
        self.assertIn("指揮官：指揮官甲", body)
        self.assertIn('href="/admin/public-pc?service=disaster&result=success"', body)
        self.assertLess(body.index("救災案件"), body.index("顯示全部"))

    def test_split_admin_pages_lock_service_and_keep_result_filter_paths(self):
        for task_id, title, service_type in (
            ("ems-split", "救護分頁樣本", "ems"),
            ("disaster-split", "救災分頁樣本", "disaster"),
        ):
            app_module.upsert_public_pc_report(
                {
                    "event_id": f"evt-{task_id}",
                    "task_id": task_id,
                    "title": title,
                    "task": {
                        "task_id": task_id,
                        "service_type": service_type,
                        "case_reason": title,
                        "case_address": "桃園市觀音區",
                    },
                    "overall_status": "desktop_fast_completed",
                    "site_statuses": {
                        "duty_work_log": {"status": "duty_work_log_saved"},
                        "vehicle_mileage": {"status": "vehicle_mileage_saved"},
                    },
                }
            )

        disaster_body = html.unescape(self.client.get("/admin/disaster").data.decode("utf-8"))
        ems_body = html.unescape(self.client.get("/admin/ems").data.decode("utf-8"))

        self.assertIn("SinpoSmart - 救災後台", disaster_body)
        self.assertIn('href="/static/sinposmart-ui.css"', disaster_body)
        self.assertIn('href="/static/sinposmart-admin.css"', disaster_body)
        self.assertNotIn("<style>", disaster_body)
        self.assertIn("救災分頁樣本", disaster_body)
        self.assertNotIn("救護分頁樣本", disaster_body)
        self.assertIn('href="/admin/disaster?result=success"', disaster_body)
        self.assertNotIn('aria-label="救災救護分類"', disaster_body)
        self.assertIn("SinpoSmart - 救護後台", ems_body)
        self.assertIn("救護分頁樣本", ems_body)
        self.assertNotIn("救災分頁樣本", ems_body)
        self.assertIn('href="/admin/ems?result=failed"', ems_body)
        self.assertNotIn('aria-label="救災救護分類"', ems_body)

    def test_split_admin_remote_update_returns_to_origin_page(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        page = html.unescape(self.client.get("/admin/disaster").data.decode("utf-8"))
        self.assertIn('name="return_service" value="disaster"', page)

        response = self.client.post(
            "/admin/public-pc/remote-update",
            data={
                "csrf_token": app_module.remote_update_csrf_token(),
                "admin_token": "test-admin-token",
                "return_service": "disaster",
            },
            follow_redirects=False,
        )

        self.assertEqual(302, response.status_code)
        self.assertEqual("/admin/disaster", response.headers["Location"])

    def test_admin_public_pc_shows_remote_update_card_only_on_nas(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "false"
        os.environ["WORKER_TOKEN"] = "test-token"
        self.post_remote_update()

        nas_body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))

        self.assertIn("遠端更新公務電腦", nas_body)
        self.assertIn('action="/admin/public-pc/remote-update"', nas_body)
        self.assertIn("等待公務電腦接收", nas_body)
        self.assertIn("勤務完成並閒置 120 秒後", nas_body)
        self.assertIn("公務電腦狀態", nas_body)
        self.assertIn('<section class="system-overview-card" aria-label="系統狀態">', nas_body)
        self.assertIn('<div class="system-overview-grid">', nas_body)

        admin_css = self.client.get("/static/sinposmart-admin.css").data.decode("utf-8")
        self.assertIn(".system-overview-card {", admin_css)
        self.assertIn(".system-overview-grid {", admin_css)
        self.assertIn(".system-overview-card .remote-update-card {", admin_css)

        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        local_body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))

        self.assertNotIn("遠端更新公務電腦", local_body)
        self.assertNotIn('action="/admin/public-pc/remote-update"', local_body)
        self.assertNotIn("公務電腦狀態", local_body)

    def test_admin_system_overview_deduplicates_matching_versions(self):
        version = "2026.07.23.0300"
        worker_health = {
            "online": True,
            "worker_id": "PUBLIC-PC",
            "last_seen_at": "2026-07-23 03:01:00",
            "package_version": version,
            "last_task_report_version": version,
            "route_label": "區網",
            "status_class": "complete",
            "status_label": "線上",
        }
        version_info = {"label": "SinpoSmart", "version": version, "detail": "NAS 後台"}

        with mock.patch.object(app_module, "worker_heartbeat_admin_view", return_value=worker_health), mock.patch.object(
            app_module, "worker_admin_version_info", return_value=version_info
        ):
            body = html.unescape(self.client.get("/admin/ems").data.decode("utf-8"))

        self.assertIn("公務電腦狀態", body)
        self.assertNotIn('class="version-card"', body)
        self.assertEqual(1, body.count(version))
        self.assertNotIn("心跳版本：", body)
        self.assertNotIn("目前版本：", body)
        self.assertNotIn("最後任務回報：", body)

    def test_admin_system_overview_only_expands_version_differences(self):
        worker_health = {
            "online": True,
            "worker_id": "PUBLIC-PC",
            "last_seen_at": "2026-07-23 03:01:00",
            "package_version": "2026.07.23.0300",
            "last_task_report_version": "2026.07.23.0250",
            "route_label": "區網",
            "status_class": "complete",
            "status_label": "線上",
        }
        version_info = {"label": "SinpoSmart", "version": "2026.07.23.0310", "detail": "NAS 後台"}

        with mock.patch.object(app_module, "worker_heartbeat_admin_view", return_value=worker_health), mock.patch.object(
            app_module, "worker_admin_version_info", return_value=version_info
        ):
            body = html.unescape(self.client.get("/admin/disaster").data.decode("utf-8"))

        self.assertIn("公務電腦版本：2026.07.23.0300", body)
        self.assertIn("NAS 後台版本：2026.07.23.0310", body)
        self.assertIn("最後任務回報版本：2026.07.23.0250", body)
        self.assertEqual(1, body.count("PUBLIC-PC"))

    def test_admin_public_pc_remote_update_post_requires_csrf_token(self):
        os.environ["WORKER_TOKEN"] = "test-token"

        rejected = self.client.post("/admin/public-pc/remote-update")
        wrong_admin = self.client.post(
            "/admin/public-pc/remote-update",
            data={"csrf_token": app_module.remote_update_csrf_token(), "admin_token": "wrong-token"},
        )
        accepted = self.post_remote_update()

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(wrong_admin.status_code, 403)
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(app_module.read_remote_update_command()["status"], "pending")

    def test_admin_public_pc_hides_remote_update_when_worker_token_is_unconfigured(self):
        os.environ["WORKER_TOKEN"] = ""

        body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))

        self.assertNotIn('<section class="remote-update-card"', body)

    def test_admin_public_pc_hides_remote_update_when_admin_token_is_unconfigured(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        os.environ["REMOTE_UPDATE_ADMIN_TOKEN"] = ""

        body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))

        self.assertNotIn('<section class="remote-update-card"', body)

    def test_admin_public_pc_remote_update_meta_wraps_on_mobile(self):
        response = self.client.get("/static/sinposmart-admin.css")
        try:
            css = response.data.decode("utf-8")
            self.assertEqual(200, response.status_code)
            self.assertIn(".remote-update-meta span {", css)
            self.assertIn("overflow-wrap: anywhere", css)
        finally:
            response.close()

    def test_admin_public_pc_shows_site_diagnostics(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        response = self.client.post(
            "/worker/public-pc-task-events",
            headers=worker_headers,
            json={
                "event_id": "evt-diag",
                "task_id": "local-task-diag",
                "task": {
                    "task_id": "local-task-diag",
                    "case_reason": "急病",
                    "case_address": "桃園市觀音區中山路",
                },
                "action": "五站登打部分失敗",
                "status": "desktop_fast_completed_with_errors",
                "overall_status": "desktop_fast_completed_with_errors",
                "site_statuses": {
                    "consumables": {
                        "key": "consumables",
                        "status": "consumables_failed",
                        "detail": "SSO login failed",
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        page = self.client.get("/admin/public-pc")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("錯誤指引", body)
        self.assertIn("未完成點", body)
        self.assertIn("登入一站通", body)
        self.assertIn("登入、帳密、SSO 或驗證碼尚未完成", body)
        self.assertIn("下一步", body)

    def test_public_pc_report_collects_only_failed_site_png_evidence(self):
        selenium_dir = Path(self.tmp.name) / "selenium"
        selenium_dir.mkdir(parents=True)
        screenshot = selenium_dir / "evidence.png"
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\nrender-timeout")
        (selenium_dir / "evidence.json").write_text(
            json.dumps(
                {
                    "task_id": "local-task-evidence",
                    "site_key": "vehicle_mileage",
                    "vehicle": "新坡92",
                    "captured_at": "2026-07-20T15:21:19+08:00",
                    "screenshot_path": str(screenshot),
                    "screenshot_error": "",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        evidence = app_module._collect_public_pc_failure_evidence(
            "local-task-evidence",
            {
                "vehicle_mileage": {"status": "vehicle_mileage_failed"},
                "fuel_record": {"status": "fuel_record_saved"},
            },
        )

        self.assertEqual(list(evidence), ["vehicle_mileage"])
        item = evidence["vehicle_mileage"]["screenshots"][0]
        self.assertEqual(item["vehicle"], "新坡92")
        self.assertEqual(base64.b64decode(item["content_base64"]), screenshot.read_bytes())
        self.assertNotIn("fuel_record", evidence)

    def test_admin_public_pc_stores_and_renders_failure_screenshot(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        png = b"\x89PNG\r\n\x1a\nbackend-failure"
        response = self.client.post(
            "/worker/public-pc-task-events",
            headers={"X-Worker-Token": "test-token"},
            json={
                "event_id": "evt-evidence",
                "task_id": "local-task-evidence",
                "task": {
                    "task_id": "local-task-evidence",
                    "case_reason": "急病",
                    "vehicle": "新坡92",
                },
                "status": "desktop_fast_completed_with_errors",
                "overall_status": "desktop_fast_completed_with_errors",
                "site_statuses": {
                    "vehicle_mileage": {
                        "key": "vehicle_mileage",
                        "status": "vehicle_mileage_failed",
                        "detail": (
                            "Timed out receiving message from renderer "
                            "[browser_failure:web_renderer_timeout]"
                        ),
                    }
                },
                "failure_evidence": {
                    "vehicle_mileage": {
                        "screenshots": [
                            {
                                "filename": "mileage.png",
                                "content_base64": base64.b64encode(png).decode("ascii"),
                                "vehicle": "新坡92",
                                "captured_at": "2026-07-20T15:21:19+08:00",
                            }
                        ],
                        "screenshot_error": "",
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        report = app_module.public_pc_reports()[0]
        site = report["site_statuses"]["vehicle_mileage"]
        image = site["failure_screenshots"][0]
        self.assertEqual(image["vehicle"], "新坡92")
        self.assertNotIn("content_base64", json.dumps(report))
        stored = list(app_module.public_pc_failure_screenshot_dir().glob("*.png"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].read_bytes(), png)

        body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))
        self.assertIn("網頁轉譯程序逾時", body)
        self.assertIn("失敗畫面", body)
        self.assertIn("新坡92", body)
        self.assertIn(image["url"], body)
        served = self.client.get(image["url"])
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.data, png)
        served.close()

        report_text = app_module.public_pc_report_file().read_text(encoding="utf-8")
        self.assertNotIn(base64.b64encode(png).decode("ascii"), report_text)

    def test_admin_failure_screenshot_keeps_the_full_image_visible(self):
        response = self.client.get("/static/sinposmart-admin.css")
        try:
            css = response.data.decode("utf-8")
            template = Path("WinPython_公務電腦使用包/templates/admin_public_pc.html").read_text(encoding="utf-8")

            self.assertIn("max-height: none;", css)
            self.assertIn("object-fit: contain;", css)
            self.assertIn("max-height: none;", template)
            self.assertIn("object-fit: contain;", template)
        finally:
            response.close()

    def test_admin_public_pc_rejects_invalid_screenshot_and_shows_capture_reason(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        response = self.client.post(
            "/worker/public-pc-task-events",
            headers={"X-Worker-Token": "test-token"},
            json={
                "event_id": "evt-bad-evidence",
                "task_id": "local-task-bad-evidence",
                "task": {"task_id": "local-task-bad-evidence", "case_reason": "急病"},
                "site_statuses": {
                    "consumables": {
                        "key": "consumables",
                        "status": "consumables_failed",
                        "detail": "Chrome not reachable [browser_failure:chrome_unresponsive]",
                    }
                },
                "failure_evidence": {
                    "consumables": {
                        "screenshots": [
                            {
                                "filename": "not-png.png",
                                "content_base64": base64.b64encode(b"not a png").decode("ascii"),
                            }
                        ],
                        "screenshot_error": "WebDriverException: Chrome not reachable",
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        site = app_module.public_pc_reports()[0]["site_statuses"]["consumables"]
        self.assertEqual(site["failure_screenshots"], [])
        self.assertIn("Chrome not reachable", site["failure_screenshot_error"])
        self.assertEqual(list(app_module.public_pc_failure_screenshot_dir().glob("*.png")), [])
        body = html.unescape(self.client.get("/admin/public-pc").data.decode("utf-8"))
        self.assertIn("截圖擷取失敗", body)
        self.assertIn("Chrome not reachable", body)

    def test_public_pc_failure_screenshot_cleanup_removes_only_expired_runtime_images(self):
        root = app_module.public_pc_failure_screenshot_dir()
        root.mkdir(parents=True)
        old = root / "old.png"
        recent = root / "recent.png"
        old.write_bytes(b"\x89PNG\r\n\x1a\nold")
        recent.write_bytes(b"\x89PNG\r\n\x1a\nrecent")
        old_time = datetime(2026, 7, 5, 12, 0).timestamp()
        recent_time = datetime(2026, 7, 6, 16, 0).timestamp()
        os.utime(old, (old_time, old_time))
        os.utime(recent, (recent_time, recent_time))

        app_module._cleanup_public_pc_failure_screenshots(datetime(2026, 7, 20, 15, 0))

        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_admin_public_pc_lists_all_task_events(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        for index in range(12):
            response = self.client.post(
                "/worker/public-pc-task-events",
                headers=worker_headers,
                json={
                    "event_id": f"evt-all-{index}",
                    "task_id": "local-task-all-events",
                    "task": {
                        "task_id": "local-task-all-events",
                        "case_reason": "急病",
                        "case_address": "桃園市觀音區中山路",
                    },
                    "action": f"事件 {index}",
                    "status": "desktop_fast_running",
                },
            )
            self.assertEqual(response.status_code, 200)

        reports = app_module.public_pc_reports()
        self.assertEqual(len(reports[0]["events"]), 12)
        page = self.client.get("/admin/public-pc")
        body = html.unescape(page.data.decode("utf-8"))
        self.assertIn("事件 0", body)
        self.assertIn("事件 11", body)

    def test_admin_public_pc_shows_worker_version(self):
        original_version_info = getattr(app_module, "worker_admin_version_info", None)
        app_module.worker_admin_version_info = lambda _reports=None: {
            "label": "SinpoSmart - 救災救護Worker",
            "version": "2026.06.19.0715",
            "detail": "目前後台",
        }
        try:
            page = self.client.get("/admin/public-pc")
            body = html.unescape(page.data.decode("utf-8"))

            self.assertIn("系統狀態", body)
            self.assertIn("公務電腦狀態", body)
            self.assertIn("NAS 後台版本：2026.06.19.0715", body)
            self.assertNotIn('class="version-card"', body)
        finally:
            if original_version_info is None:
                delattr(app_module, "worker_admin_version_info")
            else:
                app_module.worker_admin_version_info = original_version_info

    def test_admin_public_pc_shows_task_report_version_separately_from_nas(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        response = self.client.post(
            "/worker/public-pc-task-events",
            headers={"X-Worker-Token": "test-token"},
            json={
                "event_id": "evt-worker-installed-version",
                "task_id": "local-task-version",
                "task": {"task_id": "local-task-version", "case_reason": "急病"},
                "worker_id": "public-duty-pc",
                "package_version": "2026.06.19.0801-installed",
                "action": "建立任務",
                "status": "created",
            },
        )

        self.assertEqual(response.status_code, 200)
        page = self.client.get("/admin/public-pc")
        body = html.unescape(page.data.decode("utf-8"))

        self.assertIn("SinpoSmart - 救災救護Worker", body)
        self.assertIn("2026.06.19.0801-installed", body)
        self.assertIn("NAS 後台", body)
        self.assertIn("公務電腦版本：未標示", body)
        self.assertIn("最後任務回報版本：2026.06.19.0801-installed", body)

    def test_public_pc_report_is_queued_on_failure_and_flushed_on_next_success(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        os.environ["PUBLIC_PC_REPORT_SERVER_URL"] = "http://nas.test"
        original_post = app_module._post_public_pc_report
        original_current_user_label = app_module.current_public_pc_user_label
        original_site_login_accounts = app_module.public_pc_site_login_accounts
        original_package_version = app_module.package_version
        sent_payloads: list[dict] = []
        try:
            calls = {"count": 0}

            def fake_post(server_url: str, payload: dict) -> dict:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise urllib.error.URLError("offline")
                sent_payloads.append(payload)
                return {"ack_id": payload["event_id"]}

            app_module._post_public_pc_report = fake_post
            app_module.current_public_pc_user_label = lambda: "8番 曾彥綸 - tyfd01510"
            app_module.package_version = lambda: "2026.06.19.0801-local"
            app_module.public_pc_site_login_accounts = lambda task: {
                "duty_work_log": "8番 曾彥綸 - tyfd01510（任務司機優先）",
                "vehicle_mileage": "8番 曾彥綸 - tyfd01510（同步帳號）",
                "disinfection": "8番 曾彥綸 - tyfd01510（同步帳號）",
                "consumables": "8番 曾彥綸 - C123***789（同步帳號）",
            }

            task_payload = {
                "task": {"task_id": "task-1", "case_reason": "急病", "case_address": "桃園市"},
                "overall_status": "created",
                "site_statuses": {},
                "events": [{"status": "created", "detail": "任務已建立。"}],
                "created_at": "2026-06-09T00:00:00",
            }
            app_module.report_public_pc_task_event(task_payload, "建立任務")
            self.assertTrue(app_module.public_pc_pending_report_file().exists())

            task_payload["events"].append({"status": "desktop_fast_running", "detail": "本機快速執行已啟動。"})
            app_module.report_public_pc_task_event(task_payload, "按下五站登打")
        finally:
            app_module._post_public_pc_report = original_post
            app_module.current_public_pc_user_label = original_current_user_label
            app_module.public_pc_site_login_accounts = original_site_login_accounts
            app_module.package_version = original_package_version
            os.environ.pop("PUBLIC_PC_REPORT_SERVER_URL", None)

        self.assertEqual(len(sent_payloads), 2)
        self.assertFalse(app_module.public_pc_pending_report_file().exists())
        self.assertEqual(sent_payloads[0]["action"], "建立任務")
        self.assertEqual(sent_payloads[1]["action"], "按下四站登打")
        self.assertEqual(sent_payloads[0]["synced_account"], "8番 曾彥綸 - tyfd01510")
        self.assertEqual(sent_payloads[0]["package_version"], "2026.06.19.0801-local")
        self.assertEqual(
            sent_payloads[0]["site_login_accounts"]["consumables"],
            "8番 曾彥綸 - C123***789（同步帳號）",
        )

        self.assertNotEqual(sent_payloads[0]["event_id"], sent_payloads[1]["event_id"])

    def test_public_pc_report_keeps_only_unacked_entries_pending(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        os.environ["PUBLIC_PC_REPORT_SERVER_URL"] = "http://nas.test"
        original_post = app_module._post_public_pc_report
        sent_payloads: list[dict] = []
        try:
            app_module._write_pending_public_pc_reports(
                [
                    {"event_id": "evt-old-1", "task_id": "task-old-1", "action": "old-1"},
                    {"event_id": "evt-old-2", "task_id": "task-old-2", "action": "old-2"},
                ]
            )

            def fake_post(server_url: str, payload: dict) -> dict:
                sent_payloads.append(payload)
                if payload["event_id"] == "evt-old-1":
                    return {"ack_id": "evt-old-1"}
                if payload["event_id"] == "evt-old-2":
                    return {"ack_id": "wrong-ack"}
                return {"ack_id": payload["event_id"]}

            app_module._post_public_pc_report = fake_post

            task_payload = {
                "task": {"task_id": "task-new", "case_reason": "急病", "case_address": "桃園市觀音區"},
                "overall_status": "created",
                "site_statuses": {},
                "events": [{"status": "created", "detail": "建立"}],
                "created_at": "2026-06-09T00:00:00",
            }
            app_module.report_public_pc_task_event(task_payload, "建立任務")
        finally:
            app_module._post_public_pc_report = original_post
            os.environ.pop("PUBLIC_PC_REPORT_SERVER_URL", None)

        self.assertEqual([item["event_id"] for item in sent_payloads], ["evt-old-1", "evt-old-2"])
        remaining = app_module._load_pending_public_pc_reports()
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0]["event_id"], "evt-old-2")
        self.assertEqual(remaining[0]["action"], "old-2")
        self.assertEqual(remaining[1]["task_id"], "task-new")
        self.assertEqual(remaining[1]["action"], "建立任務")

    def test_concurrent_offline_public_pc_reports_are_all_retained(self):
        payloads = [
            {
                "task": {"task_id": f"task-{index}", "case_reason": "急病"},
                "overall_status": "created",
                "created_at": "2026-07-13T10:00:00",
                "events": [{"status": "created", "detail": "created"}],
            }
            for index in range(20)
        ]

        def offline(*_args, **_kwargs):
            __import__("time").sleep(0.02)
            raise urllib.error.URLError("offline")

        with mock.patch.object(app_module, "public_pc_reporting_enabled", return_value=True), mock.patch.object(
            app_module, "public_pc_report_server_url", return_value="http://nas.invalid"
        ), mock.patch.object(app_module, "_post_public_pc_report", side_effect=offline):
            with ThreadPoolExecutor(max_workers=20) as pool:
                list(pool.map(lambda payload: app_module.report_public_pc_task_event(payload, "建立任務"), payloads))

        pending = app_module._load_pending_public_pc_reports()
        self.assertEqual(len(pending), len(payloads))
        self.assertEqual({entry["task_id"] for entry in pending}, {f"task-{index}" for index in range(20)})

    def test_public_pc_report_removes_acked_entries_when_later_send_fails(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        os.environ["PUBLIC_PC_REPORT_SERVER_URL"] = "http://nas.test"
        original_post = app_module._post_public_pc_report
        sent_payloads: list[dict] = []
        try:
            app_module._write_pending_public_pc_reports(
                [
                    {"event_id": "evt-acked", "task_id": "task-old-1", "action": "acked"},
                    {"event_id": "evt-fails", "task_id": "task-old-2", "action": "fails"},
                ]
            )

            def fake_post(server_url: str, payload: dict) -> dict:
                sent_payloads.append(payload)
                if payload["event_id"] == "evt-acked":
                    return {"ack_id": "evt-acked"}
                raise urllib.error.URLError("offline after first ack")

            app_module._post_public_pc_report = fake_post

            task_payload = {
                "task": {"task_id": "task-new", "case_reason": "急病", "case_address": "桃園市觀音區"},
                "overall_status": "created",
                "site_statuses": {},
                "events": [{"status": "created", "detail": "建立"}],
                "created_at": "2026-06-09T00:00:00",
            }
            app_module.report_public_pc_task_event(task_payload, "建立任務")
        finally:
            app_module._post_public_pc_report = original_post
            os.environ.pop("PUBLIC_PC_REPORT_SERVER_URL", None)

        self.assertEqual([item["event_id"] for item in sent_payloads], ["evt-acked", "evt-fails"])
        remaining = app_module._load_pending_public_pc_reports()
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0]["event_id"], "evt-fails")
        self.assertEqual(remaining[0]["action"], "fails")
        self.assertEqual(remaining[1]["task_id"], "task-new")
        self.assertEqual(remaining[1]["action"], "建立任務")

    def test_create_task_rejects_return_datetime_before_case_datetime(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                case_date="2026-06-08",
                case_time="1024",
                return_date="2026-06-08",
                return_time="0950",
            ),
            follow_redirects=False,
        )
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_recent(), [])
        self.assertIn("返隊日期時間不能早於案件日期時間", body)

    def test_create_task_allows_next_day_return_datetime(self):
        response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                case_date="2026-06-08",
                case_time="2350",
                return_date="2026-06-09",
                return_time="0010",
            ),
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)

    def test_query_cases_redirects_to_app(self):
        response = self.client.post("/cases/query", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/app")
        request_payload = app_module.read_case_lookup_request()
        self.assertEqual(request_payload["status"], "case_lookup_requested")
        self.assertEqual(request_payload["lookup_range"], "24h")

    def test_query_cases_forces_24h_range(self):
        response = self.client.post("/cases/query", data={"lookup_range": "legacy-range"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        request_payload = app_module.read_case_lookup_request()
        self.assertEqual(request_payload["lookup_range"], "24h")

    def test_query_cases_ignores_explicit_lookup_date(self):
        response = self.client.post(
            "/cases/query",
            data={"lookup_date": "2026-08-13"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        request_payload = app_module.read_case_lookup_request()
        self.assertEqual(request_payload["lookup_range"], "24h")
        self.assertEqual(app_module.case_lookup_range_label(request_payload["lookup_range"]), "最近 24 小時")

    def test_query_cases_handles_request_write_failure(self):
        def fail_write(lookup_range: str, source: str = "", mode: str = "worker_queue") -> dict:
            raise OSError("request path denied")

        app_module.write_case_lookup_request = fail_write

        response = self.client.post("/cases/query", follow_redirects=False)
        body = html.unescape(self.client.get("/app").data.decode("utf-8"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/app")
        self.assertIn("案件查詢啟動失敗", body)
        self.assertIn("request path denied", body)

    def test_app_page_auto_refreshes_while_case_lookup_is_running(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "detail": "已查到 2 筆 24 小時內案件，並讀取出勤人員。",
                "lookup_range": "24h",
                "cases": [{"case_id": "old-case"}],
            },
        )
        app_module.write_case_lookup_request("24h")

        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("window.location.reload()", body)
        self.assertIn("lookup-status is-visible", body)
        self.assertIn('disabled aria-disabled="true">查詢中…</button>', body)
        self.assertIn("正在查詢最近 24 小時案件，請稍候。", body)
        self.assertNotIn("已查到 2 筆", body)

    def test_case_import_buttons_are_disabled_while_lookup_is_running(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {"status": "cases_loaded", "cases": [{"case_id": "lookup-lock-case"}]},
        )
        app_module.write_case_lookup_request("24h")

        for endpoint in ("/app", "/app/disaster"):
            body = html.unescape(self.client.get(endpoint).data.decode("utf-8"))
            self.assertIn('type="submit" disabled aria-disabled="true">查詢中…</button>', body)

        workspace_css = self.client.get("/static/sinposmart-workspace.css").data.decode("utf-8")
        self.assertIn(".workspace-page--task .case-card button:disabled", workspace_css)
        self.assertIn("cursor: wait;", workspace_css)

    def test_app_page_auto_refreshes_while_recent_task_is_running(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.set_overall_status(task_id, "desktop_fast_running", "五站登打中")
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("duty_work_log", "消防勤務工作紀錄", "duty_work_log_saved", "saved"),
        )
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_running", "running"),
        )

        running_response = self.client.get("/app")
        running_body = html.unescape(running_response.data.decode("utf-8"))

        self.assertIn("window.location.reload()", running_body)
        self.assertIn("taskFormDirty", running_body)
        self.assertIn("已完成 1/4；目前：里程執行中", running_body)

        self.store.set_overall_status(task_id, "desktop_fast_completed", "五站登打完成")
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_saved", "saved"),
        )
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("fuel_record", "登打加油紀錄", "fuel_record_saved", "saved"),
        )
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("disinfection", "緊急救護消毒", "disinfection_saved", "saved"),
        )
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("consumables", "一站通耗材", "consumables_saved", "saved"),
        )
        completed_response = self.client.get("/app")
        completed_body = html.unescape(completed_response.data.decode("utf-8"))

        self.assertNotIn("window.location.reload()", completed_body)
        self.assertIn("四站登打完成", completed_body)

    def test_app_page_shows_empty_case_lookup_result(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "detail": "loaded",
                "lookup_range": "24h",
                "cases": [],
            },
        )
        app_module.write_json_atomic(
            cases_dir / "request.json",
            {
                "status": "case_lookup_completed",
                "lookup_range": "24h",
                "case_count": 0,
            },
        )

        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn('class="lookup-message is-empty"', body)
        self.assertIn("查詢完成，最近 24 小時沒有找到案件。", body)
        self.assertNotIn("可以稍後再查，或直接手動輸入案件資料。", body)
        self.assertNotIn("window.location.reload()", body)

    def test_app_page_shows_loaded_case_lookup_result_message(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "detail": "已查到 2 筆前 24 小時的緊急救護案件，並預先讀取服勤人員。",
                "lookup_range": "24h",
                "cases": [
                    {"case_id": "case-1", "address": "桃園市觀音區", "personnel": ["王小明"]},
                    {"case_id": "case-2", "address": "桃園市新屋區"},
                ],
            },
        )

        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("已查到 2 筆 24 小時內案件，並讀取出勤人員。", body)
        self.assertNotIn("緊急救護案件", body)
        self.assertIn("出勤人員：王小明", body)
        self.assertNotIn("服勤人員：王小明", body)
        self.assertLess(body.index("已查到 2 筆"), body.index('<div class="case-list">'))

    def test_case_lookup_shows_established_vehicle_per_service_before_import_button(self):
        self.store.create(
            AmbulanceReturnRequest(
                task_id="ems-case-status",
                created_at=datetime.now(),
                raw_text="",
                service_type="ems",
                case_id="case-ems-status",
                case_address="桃園市觀音區救護狀態路1號",
                vehicle="新坡93",
            )
        )
        self.store.create(
            AmbulanceReturnRequest(
                task_id="disaster-case-status",
                created_at=datetime.now(),
                raw_text="",
                service_type="disaster",
                case_id="case-disaster-status",
                case_address="桃園市觀音區救災狀態路2號",
                vehicle="新坡11",
            )
        )
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "lookup_range": "24h",
                "cases": [
                    {
                        "case_id": "case-ems-status",
                        "address": "桃園市觀音區救護狀態路1號",
                    },
                    {
                        "case_id": "case-disaster-status",
                        "address": "桃園市觀音區救災狀態路2號",
                    },
                    {
                        "case_id": "case-new-status",
                        "address": "桃園市觀音區新案件路3號",
                    },
                ],
            },
        )

        ems_body = html.unescape(self.client.get("/app").data.decode("utf-8"))
        disaster_body = html.unescape(self.client.get("/app/disaster").data.decode("utf-8"))

        self.assertIn("93車已建立", ems_body)
        self.assertEqual(2, ems_body.count("尚未建立"))
        self.assertNotIn("11車已建立", ems_body)
        self.assertIn("11車已建立", disaster_body)
        self.assertEqual(2, disaster_body.count("尚未建立"))
        self.assertNotIn("93車已建立", disaster_body)
        self.assertLess(
            ems_body.index("case-entry-status"),
            ems_body.index('action="/cases/import"'),
        )

    def test_mobile_layout_keeps_header_action_compact_and_stacks_time_fields(self):
        response = self.client.get("/app")
        body = html.unescape(response.data.decode("utf-8"))

        css_response = self.client.get("/static/sinposmart-ui.css")
        css = css_response.data.decode("utf-8")
        css_response.close()
        workspace_css_response = self.client.get("/static/sinposmart-workspace.css")
        workspace_css = workspace_css_response.data.decode("utf-8")
        workspace_css_response.close()
        self.assertIn(".page-chrome {\n    align-items: stretch;\n    flex-direction: column;", css)
        self.assertIn(".page-chrome__actions .button {\n    width: 100%;", css)
        self.assertIn(".lookup-form { display: grid; grid-template-columns: 1fr; gap: 8px; width: 100%; }", body)
        self.assertIn(".time-field { grid-template-columns: 1fr; }", body)
        self.assertIn('.return-time-field input[name="return_date"] { grid-column: 1 / -1; }', body)
        self.assertIn(
            "@media (max-width: 560px) {\n"
            "  .workspace-page--ems-task .case-card {\n"
            "    grid-template-columns: minmax(0, 1fr);\n"
            "    align-items: stretch;\n"
            "  }",
            workspace_css,
        )

    def test_disaster_mobile_case_cards_stack_below_desktop_breakpoint(self):
        response = self.client.get("/app/disaster")
        body = html.unescape(response.data.decode("utf-8"))
        response.close()
        workspace_css_response = self.client.get("/static/sinposmart-workspace.css")
        workspace_css = workspace_css_response.data.decode("utf-8")
        workspace_css_response.close()

        self.assertIn('workspace-page--disaster-task', body)
        self.assertIn(
            "@media (max-width: 700px) {\n"
            "  .workspace-page--disaster-task .case-card {\n"
            "    grid-template-columns: minmax(0, 1fr);\n"
            "    align-items: stretch;\n"
            "  }",
            workspace_css,
        )

    def test_localhost_query_cases_starts_local_lookup_when_fast_mode_auto(self):
        calls = []
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        app_module.start_local_case_lookup = lambda lookup_range: calls.append(lookup_range)

        response = self.client.post("/cases/query", data={"lookup_range": "legacy-range"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(calls, ["24h"])

    def test_localhost_query_cases_handles_thread_start_failure(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"

        def fail_start(lookup_range: str) -> None:
            raise RuntimeError("cannot start local lookup")

        app_module.start_local_case_lookup = fail_start

        response = self.client.post("/cases/query", follow_redirects=False)
        body = html.unescape(self.client.get("/app").data.decode("utf-8"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("案件查詢啟動失敗", body)
        self.assertIn("cannot start local lookup", body)

    def test_query_cases_handles_local_mode_detection_failure_without_500(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"

        with mock.patch.object(app_module, "effective_task_execution_mode", side_effect=OSError(22, "Invalid argument")):
            response = self.client.post("/cases/query", follow_redirects=False)

        body = html.unescape(self.client.get("/app").data.decode("utf-8"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("案件查詢啟動失敗", body)
        self.assertIn("Invalid argument", body)

    def test_local_ip_query_cases_starts_local_lookup_when_fast_mode_auto(self):
        calls = []
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        app_module.local_host_candidates = lambda: {"192.168.50.23"}
        app_module.start_local_case_lookup = lambda lookup_range: calls.append(lookup_range)

        response = self.client.post(
            "/cases/query",
            base_url="http://192.168.50.23:8091",
            data={"lookup_range": "24h"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(calls, ["24h"])

    def test_query_cases_does_not_start_local_lookup_when_fast_mode_disabled(self):
        calls = []
        os.environ["DESKTOP_FAST_MODE"] = "0"
        app_module.start_local_case_lookup = lambda lookup_range: calls.append(lookup_range)

        response = self.client.post("/cases/query", data={"lookup_range": "legacy-range"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(calls, [])

    def test_run_local_case_lookup_writes_cases_and_completes_request(self):
        def fake_query(lookup_range: str = "24h") -> DutyCaseLookupResult:
            cases = [{"case_id": "case-1", "address": "addr"}]
            payload = {
                "status": "cases_loaded",
                "detail": "loaded",
                "updated_at": "2026-06-07T20:00:00",
                "cases": cases,
            }
            path = app_module.artifacts_dir / "cases" / "latest.json"
            app_module.write_json_atomic(path, payload)
            return DutyCaseLookupResult(True, "cases_loaded", "loaded", cases, path)

        app_module.run_case_lookup_query = fake_query
        selenium_local_module.query_duty_emergency_cases = lambda *args, **kwargs: self.fail("run_local_case_lookup should use run_case_lookup_query")
        app_module.write_case_lookup_request("24h")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            app_module.run_local_case_lookup("24h")

        latest = app_module.read_case_lookup()
        self.assertEqual(latest["source"], "local_public_duty_pc")
        self.assertEqual(latest["case_count"], 1)
        self.assertEqual(latest["cases"][0]["case_id"], "case-1")
        completed = app_module.read_case_lookup_request()
        self.assertEqual(completed["status"], "case_lookup_completed")
        self.assertIn("[worker] case lookup result status=cases_loaded count=1", output.getvalue())

    def test_run_local_case_lookup_failure_clears_running_request(self):
        def fake_query(lookup_range: str = "24h") -> DutyCaseLookupResult:
            raise RuntimeError("login window stuck")

        app_module.run_case_lookup_query = fake_query
        selenium_local_module.query_duty_emergency_cases = lambda *args, **kwargs: self.fail("run_local_case_lookup should use run_case_lookup_query")
        app_module.write_case_lookup_request("24h")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            app_module.run_local_case_lookup("24h")

        latest = app_module.read_case_lookup()
        self.assertEqual(latest["status"], "case_lookup_failed")
        self.assertEqual(latest["case_count"], 0)
        self.assertIn("login window stuck", latest["detail"])
        completed = app_module.read_case_lookup_request()
        self.assertEqual(completed["status"], "case_lookup_failed")
        self.assertIn("[worker] case lookup result status=case_lookup_failed count=0", output.getvalue())

    def test_run_local_case_lookup_non_loaded_result_clears_running_request(self):
        def fake_query(lookup_range: str = "24h") -> DutyCaseLookupResult:
            payload = {
                "status": "case_lookup_timeout",
                "detail": "timeout",
                "updated_at": "2026-06-18T10:40:00",
                "cases": [],
            }
            path = app_module.artifacts_dir / "cases" / "latest.json"
            app_module.write_json_atomic(path, payload)
            return DutyCaseLookupResult(False, "case_lookup_timeout", "timeout", [], path)

        app_module.run_case_lookup_query = fake_query
        selenium_local_module.query_duty_emergency_cases = lambda *args, **kwargs: self.fail("run_local_case_lookup should use run_case_lookup_query")
        app_module.write_case_lookup_request("24h")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            app_module.run_local_case_lookup("24h")

        latest = app_module.read_case_lookup()
        self.assertEqual(latest["status"], "case_lookup_timeout")
        self.assertEqual(latest["case_count"], 0)
        completed = app_module.read_case_lookup_request()
        self.assertEqual(completed["status"], "case_lookup_failed")
        self.assertEqual(completed["detail"], "timeout")
        self.assertIn("[worker] case lookup result status=case_lookup_timeout count=0", output.getvalue())

    def test_run_local_case_lookup_timeout_clears_running_request_and_cleans_chrome(self):
        self.assertTrue(hasattr(app_module, "CaseLookupProcessTimeout"))
        cleanups = []

        def fake_query(lookup_range: str = "24h") -> DutyCaseLookupResult:
            raise app_module.CaseLookupProcessTimeout("Chrome startup timed out")

        def fake_cleanup(options, label="Chrome", include_generated_profiles=False, profile_root=None):
            cleanups.append((options, label, include_generated_profiles, profile_root))
            return 2

        app_module.run_case_lookup_query = fake_query
        app_module.cleanup_worker_chrome_residue = fake_cleanup
        selenium_local_module.query_duty_emergency_cases = lambda *args, **kwargs: self.fail("run_local_case_lookup should use run_case_lookup_query")
        app_module.write_case_lookup_request("24h")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            app_module.run_local_case_lookup("24h")

        latest = app_module.read_case_lookup()
        self.assertEqual(latest["status"], "case_lookup_timeout")
        self.assertEqual(latest["case_count"], 0)
        self.assertIn("Chrome startup timed out", latest["detail"])
        completed = app_module.read_case_lookup_request()
        self.assertEqual(completed["status"], "case_lookup_failed")
        self.assertEqual(completed["detail"], latest["detail"])
        self.assertEqual(len(cleanups), 1)
        self.assertEqual(cleanups[0][1], "case lookup timeout")
        self.assertTrue(cleanups[0][2])
        self.assertIn("[worker] case lookup result status=case_lookup_timeout count=0", output.getvalue())

    def test_run_case_lookup_query_uses_child_process_and_reads_latest(self):
        self.assertTrue(hasattr(app_module, "run_case_lookup_query"))
        calls = []

        def fake_run(cmd, **kwargs):
            cases = [{"case_id": "case-child", "address": "addr"}]
            app_module.write_json_atomic(
                app_module.artifacts_dir / "cases" / "latest.json",
                {
                    "status": "cases_loaded",
                    "detail": "loaded by child",
                    "updated_at": "2026-07-07T11:45:00",
                    "cases": cases,
                },
            )
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout="[child] done\n", stderr="")

        app_module.subprocess.run = fake_run

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = app_module.run_case_lookup_query("24h")

        self.assertEqual(result.status, "cases_loaded")
        self.assertEqual(result.detail, "loaded by child")
        self.assertEqual(result.cases[0]["case_id"], "case-child")
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[1:4], ["-m", "ambulance_bot.case_lookup_runner", "--artifacts-dir"])
        self.assertEqual(kwargs["cwd"], str(Path(app_module.__file__).resolve().parent))
        self.assertEqual(kwargs["env"]["SELENIUM_REMOTE_URL"], "")
        self.assertEqual(kwargs["env"]["SELENIUM_DETACH"], "false")
        self.assertEqual(kwargs["env"]["SELENIUM_HEADLESS"], "true")
        self.assertEqual(kwargs["env"]["SELENIUM_HEADLESS_ARG"], "--headless=new")
        self.assertEqual(kwargs["env"]["OPEN_LOCAL_BROWSER_ON_RUN"], "false")
        self.assertGreaterEqual(kwargs["timeout"], 30)
        self.assertIn("[child] done", output.getvalue())

    def test_app_page_stale_case_lookup_request_allows_retry(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        app_module.write_json_atomic(
            cases_dir / "request.json",
            {
                "status": "case_lookup_requested",
                "lookup_range": "24h",
                "requested_at": "2000-01-01T00:00:00",
            },
        )

        response = self.client.get("/app")

        body = response.get_data(as_text=True)
        self.assertIn("案件查詢逾時", body)
        self.assertNotIn("window.location.reload()", body)
        self.assertNotIn("disabled>查詢案件</button>", body)

    def test_app_page_abandoned_local_case_lookup_request_allows_retry(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        app_module.write_json_atomic(
            cases_dir / "request.json",
            {
                "status": "case_lookup_requested",
                "lookup_range": "24h",
                "requested_at": "2026-06-18T10:35:34",
            },
        )

        response = self.client.get("/app")

        body = response.get_data(as_text=True)
        request_payload = app_module.read_case_lookup_request()
        self.assertEqual(request_payload["status"], "case_lookup_failed")
        self.assertIn("上一輪本機案件查詢已中斷", request_payload["detail"])
        self.assertNotIn("window.location.reload()", body)
        self.assertNotIn("disabled>?亥岷獢辣</button>", body)

    def test_app_page_does_not_query_cases(self):
        response = self.client.get("/app")

        self.assertEqual(response.status_code, 200)

    def test_case_display_extracts_address_from_description_and_hides_empty_return_time(self):
        case = {
            "category": "\u7dca\u6025\u6551\u8b77-\u5275\u50b7",
            "description": "119\u6848\u4ef6\n\u7dca\u6025\u6551\u8b77\n\u8fd4\u968a\u6642\u9593:\n\u5730\u9ede:\u6843\u5712\u5e02\u89c0\u97f3\u5340\u798f\u5c71\u8def\u4e8c\u6bb5790\u5df7100\u5f049\u865f",
            "case_date": "1150607",
            "case_time_hhmm": "1024",
            "return_time_hhmm": "",
        }

        self.assertEqual(
            app_module.display_case_title(case),
            "\u7dca\u6025\u6551\u8b77-\u5275\u50b7 - \u6843\u5712\u5e02\u89c0\u97f3\u5340\u798f\u5c71\u8def\u4e8c\u6bb5790\u5df7100\u5f049\u865f",
        )
        self.assertEqual(app_module.case_time_range(case), "06/07 1024")
        self.assertEqual(app_module.selected_return_date_input(case), "")
        self.assertEqual(app_module.selected_return_time_input(case), "")

    def test_case_display_hides_placeholder_return_datetime(self):
        case = {
            "category": "\u7dca\u6025\u6551\u8b77-\u6025\u75c5",
            "address": "\u6843\u5712\u5e02\u5927\u5712\u5340\u79d1\u4e94\u885722\u5df79\u865f4\u6a13",
            "case_date": "1150611",
            "case_time_hhmm": "0112",
            "return_time": "1900/01/01 00:00:00",
            "return_time_hhmm": "0000",
        }

        self.assertEqual(app_module.case_time_range(case), "06/11 0112")
        self.assertEqual(app_module.selected_return_date_input(case), "")
        self.assertEqual(app_module.selected_return_time_input(case), "")

    def test_return_date_input_preserves_submitted_date_without_return_time(self):
        case = {
            "case_date": "2026-06-07",
            "return_date": "2026-06-08",
            "return_time_hhmm": "",
        }

        self.assertEqual(app_module.selected_return_date_input(case), "2026/06/08")

    def test_event_detail_text_keeps_event_log_short(self):
        event = {"status": "vehicle_mileage_saved", "detail": "\u8eca\u8f1b\u91cc\u7a0b: \u5df2\u5efa\u7acb\u5f88\u9577\u7684\u8aaa\u660e"}

        self.assertEqual(app_module.event_detail_text(event), "\u5df2\u5b8c\u6210")

    def test_visible_events_keeps_latest_event_per_site(self):
        events = [
            {"status": "disinfection_failed", "detail": "\u7dca\u6025\u6551\u8b77\u6d88\u6bd2: old", "time": "1"},
            {"status": "desktop_fast_completed_with_errors", "detail": "overall", "time": "2"},
            {"status": "disinfection_saved", "detail": "\u7dca\u6025\u6551\u8b77\u6d88\u6bd2: new", "time": "3"},
        ]

        visible = app_module.visible_events(events)

        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["status"], "disinfection_saved")

    def test_effective_task_status_prefers_waiting_site(self):
        payload = {
            "overall_status": "duty_work_log_saved",
            "site_statuses": {
                "vehicle_mileage": {"status": "vehicle_mileage_saved"},
                "consumables": {"status": "manual_captcha_required"},
            },
        }

        self.assertEqual(app_module.effective_task_status(payload), "manual_captcha_required")

    def test_worker_case_lookup_request_and_cases_post(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        self.client.post("/cases/query", data={"lookup_range": "24h"}, follow_redirects=False)

        denied = self.client.get("/worker/case-lookup-request")
        self.assertEqual(denied.status_code, 403)

        request_response = self.client.get("/worker/case-lookup-request", headers={"X-Worker-Token": "test-token"})
        self.assertEqual(request_response.status_code, 200)
        request_payload = request_response.get_json()
        self.assertEqual(request_payload["request"]["lookup_range"], "24h")
        request_id = request_payload["request"]["request_id"]

        cases_response = self.client.post(
            "/worker/cases",
            headers={"X-Worker-Token": "test-token"},
            json={
                "status": "cases_loaded",
                "detail": "loaded",
                "lookup_range": "24h",
                "case_hash": "abc123",
                "request_id": request_id,
                "cases": [{"case_id": "1", "address": "addr"}],
            },
        )
        self.assertEqual(cases_response.status_code, 200)
        latest = app_module.read_case_lookup()
        self.assertEqual(latest["case_hash"], "abc123")
        self.assertEqual(latest["cases"][0]["case_id"], "1")
        completed = app_module.read_case_lookup_request()
        self.assertEqual(completed["status"], "case_lookup_completed")

    def test_worker_case_lookup_rejects_result_for_different_request(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        app_module.write_case_lookup_request("24h", source="NAS端", mode="worker_queue")
        current = app_module.read_case_lookup_request()

        response = self.client.post(
            "/worker/cases",
            headers={"X-Worker-Token": "test-token"},
            json={
                "request_id": "stale-request",
                "status": "cases_loaded",
                "detail": "stale",
                "cases": [{"case_id": "stale"}],
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(app_module.read_case_lookup_request()["request_id"], current["request_id"])
        self.assertEqual(app_module.read_case_lookup_request()["status"], "case_lookup_requested")
        self.assertFalse((app_module.artifacts_dir / "cases" / "latest.json").exists())

    def test_worker_cases_accepts_scheduled_push_without_active_request_or_request_id(self):
        os.environ["WORKER_TOKEN"] = "test-token"

        response = self.client.post(
            "/worker/cases",
            headers={"X-Worker-Token": "test-token"},
            json={
                "status": "cases_loaded",
                "detail": "scheduled refresh",
                "lookup_range": "24h",
                "case_hash": "scheduled-abc",
                "cases": [{"case_id": "scheduled-1", "address": "addr"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        latest = app_module.read_case_lookup()
        self.assertEqual(latest["request_id"], "")
        self.assertEqual(latest["case_hash"], "scheduled-abc")
        self.assertEqual(latest["cases"][0]["case_id"], "scheduled-1")

    def test_worker_roster_post_filters_consultants_and_populates_volunteer_assist_form(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        member_id = roster_member_id("大園救護分隊", "隊員", "測試義消")

        response = self.client.post(
            "/worker/civilpower-roster",
            headers={"X-Worker-Token": "test-token"},
            json={
                "status": "civilpower_roster_loaded",
                "detail": "loaded",
                "attempted_at": "2026-08-19T09:00:00",
                "last_success_at": "2026-08-19T09:00:00",
                "members": [
                    {"unit": "大園救護分隊", "title": "隊員", "name": "測試義消"},
                    {"unit": "大園救護分隊", "title": "顧問", "name": "排除顧問"},
                    {"unit": "大園救護分隊", "title": "技術顧問", "name": "排除技術顧問"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(1, response.get_json()["member_count"])
        roster = app_module.read_civilpower_roster()
        self.assertEqual(["測試義消"], [member["name"] for member in roster["members"]])

        app_module.write_json_atomic(
            app_module.artifacts_dir / "cases" / "selected.json",
            {
                "status": "case_imported",
                "selected_case": self.valid_task_data(),
            },
        )
        self.client.set_cookie(app_module.SELECTED_CASE_PENDING_COOKIE, "1")
        body = html.unescape(self.client.get("/app").data.decode("utf-8"))
        self.assertIn('id="volunteer-assist-toggle"', body)
        self.assertIn("<h2>民力系統</h2>", body)
        self.assertNotIn("民力系統(測試中)", body)
        self.assertIn(">救護義消協勤<", body)
        self.assertIn('id="volunteer-assist-unit" aria-label="所屬單位" disabled', body)
        self.assertIn('<option value="大園救護分隊" selected>大園救護分隊</option>', body)
        self.assertIn('name="volunteer_assist_member_id"', body)
        self.assertIn('<option value="">請選擇</option>', body)
        self.assertIn('id="volunteer-assist-required-mark"', body)
        self.assertIn('volunteerAssistMember.required = enabled;', body)
        self.assertNotIn('id="volunteer-assist-roster-status"', body)
        self.assertNotIn('data-civilpower-roster-divider', body)
        self.assertIn(f'value="{member_id}"', body)
        self.assertNotIn("排除顧問", body)
        self.assertNotIn("排除技術顧問", body)

        create = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                volunteer_assist="1",
                volunteer_assist_member_id=member_id,
            ),
            follow_redirects=False,
        )

        self.assertEqual(create.status_code, 302)
        task_id = create.headers["Location"].rstrip("/").split("/")[-1]
        task = self.store.get(task_id)["task"]
        self.assertTrue(task["volunteer_assist"])
        self.assertEqual("測試義消", task["volunteer_assist_member_name"])
        self.assertEqual("民力系統", app_module.SITE_DISPLAY_NAMES["volunteer_assist"])
        self.assertEqual("民力系統", app_module.event_site_name({"status": "volunteer_assist_saved"}))
        detail_body = html.unescape(self.client.get(f"/tasks/{task_id}").data.decode("utf-8"))
        self.assertIn("<h3>民力系統</h3>", detail_body)

    def test_worker_civilpower_roster_get_requires_token_and_returns_snapshot(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        self.client.post(
            "/worker/civilpower-roster",
            headers=headers,
            json={
                "status": "civilpower_roster_loaded",
                "attempted_at": "2026-08-20T09:00:00",
                "last_success_at": "2026-08-20T09:00:00",
                "members": [{"unit": "大園救護分隊", "title": "隊員", "name": "江尚諭"}],
            },
        )

        forbidden = self.client.get("/worker/civilpower-roster")
        response = self.client.get("/worker/civilpower-roster", headers=headers)

        self.assertEqual(403, forbidden.status_code)
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(["江尚諭"], [member["member_id"] for member in payload["members"]])

    def test_local_civilpower_roster_fetches_nas_snapshot_and_keeps_cache_when_offline(self):
        os.environ["WORKER_SERVER_URL"] = "http://nas.test:8080"
        os.environ["WORKER_TOKEN"] = "test-token"
        remote_payload = {
            "ok": True,
            "status": "civilpower_roster_loaded",
            "detail": "loaded",
            "source": "public_duty_pc_worker",
            "last_attempted_at": "2026-08-20T09:00:00",
            "last_success_at": "2026-08-20T09:00:00",
            "members": [{"unit": "大園救護分隊", "title": "隊員", "name": "江尚諭"}],
        }
        online_response = mock.MagicMock()
        online_response.__enter__.return_value.read.return_value = json.dumps(remote_payload).encode("utf-8")

        with mock.patch.object(app_module.urllib.request, "urlopen", return_value=online_response) as urlopen, mock.patch.object(
            app_module, "request_is_local_host", return_value=True
        ):
            roster = app_module.effective_civilpower_roster()

        self.assertEqual(["江尚諭"], [member["member_id"] for member in roster["members"]])
        self.assertEqual(
            "http://nas.test:8080/worker/civilpower-roster",
            urlopen.call_args.args[0].full_url,
        )
        self.assertEqual("test-token", urlopen.call_args.args[0].get_header("X-worker-token"))

        with mock.patch.object(
            app_module.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ), mock.patch.object(app_module, "request_is_local_host", return_value=True):
            fallback = app_module.effective_civilpower_roster()

        self.assertEqual(["江尚諭"], [member["member_id"] for member in fallback["members"]])

    def test_legacy_civilpower_member_id_rehydrates_by_name_after_title_change(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        os.environ["WORKER_SERVER_URL"] = ""
        self.client.post(
            "/worker/civilpower-roster",
            headers={"X-Worker-Token": "test-token"},
            json={
                "status": "civilpower_roster_loaded",
                "attempted_at": "2026-08-20T09:00:00",
                "last_success_at": "2026-08-20T09:00:00",
                "members": [{"unit": "大園救護分隊", "title": "小隊長", "name": "江尚諭"}],
            },
        )
        task_request = app_module.request_from_form(
            self.valid_task_data(
                volunteer_assist="1",
                volunteer_assist_member_id="civilpower-legacy-member",
                volunteer_assist_member_name="江尚諭",
                volunteer_assist_member_title="隊員",
                volunteer_assist_member_unit="大園救護分隊",
            )
        )

        with app_module.app.test_request_context("/"):
            app_module.hydrate_volunteer_assist_member(task_request)

        self.assertEqual("江尚諭", task_request.volunteer_assist_member_id)
        self.assertEqual("江尚諭", task_request.volunteer_assist_member_name)
        self.assertEqual("小隊長", task_request.volunteer_assist_member_title)

    def test_local_civilpower_roster_keeps_cache_when_nas_snapshot_is_empty(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        os.environ["WORKER_SERVER_URL"] = "http://nas.test:8080"
        headers = {"X-Worker-Token": "test-token"}
        self.client.post(
            "/worker/civilpower-roster",
            headers=headers,
            json={
                "status": "civilpower_roster_loaded",
                "attempted_at": "2026-08-20T09:00:00",
                "last_success_at": "2026-08-20T09:00:00",
                "members": [{"unit": "大園救護分隊", "title": "隊員", "name": "江尚諭"}],
            },
        )
        empty_response = mock.MagicMock()
        empty_response.__enter__.return_value.read.return_value = json.dumps(
            {
                "ok": True,
                "status": "civilpower_roster_failed",
                "detail": "refresh failed",
                "members": [],
            }
        ).encode("utf-8")

        with mock.patch.object(app_module.urllib.request, "urlopen", return_value=empty_response), mock.patch.object(
            app_module, "request_is_local_host", return_value=True
        ):
            roster = app_module.effective_civilpower_roster()

        self.assertEqual(["江尚諭"], [member["member_id"] for member in roster["members"]])

    def test_edit_legacy_civilpower_member_keeps_identity_data_for_name_rehydration(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        self.client.post(
            "/worker/civilpower-roster",
            headers={"X-Worker-Token": "test-token"},
            json={
                "status": "civilpower_roster_loaded",
                "attempted_at": "2026-08-20T09:00:00",
                "last_success_at": "2026-08-20T09:00:00",
                "members": [{"unit": "大園救護分隊", "title": "小隊長", "name": "江尚諭"}],
            },
        )
        task_request = app_module.request_from_form(self.valid_task_data())
        task_request.task_id = "legacy-civilpower-edit"
        payload = self.store.create(task_request)
        task = payload["task"]
        task.update(
            {
                "volunteer_assist": True,
                "volunteer_assist_member_id": "civilpower-legacy-member",
                "volunteer_assist_member_name": "江尚諭",
                "volunteer_assist_member_title": "隊員",
                "volunteer_assist_member_unit": "大園救護分隊",
            }
        )
        self.store.save_payload(task_request.task_id, payload)

        response = self.client.get(f"/tasks/{task_request.task_id}/edit", headers={"Host": "nas.test:8080"})
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(200, response.status_code)
        self.assertIn(
            'value="civilpower-legacy-member" data-name="江尚諭" data-title="隊員" data-unit="大園救護分隊" selected',
            body,
        )

    def test_civilpower_preferences_sync_and_prioritize_names_in_ems_form(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        nas_headers = {"Host": "100.114.126.58:8080"}
        self.client.post(
            "/worker/civilpower-roster",
            headers=worker_headers,
            json={
                "status": "civilpower_roster_loaded",
                "attempted_at": "2026-08-20T09:00:00",
                "last_success_at": "2026-08-20T09:00:00",
                "members": [
                    {"unit": "大園救護分隊", "title": "隊員", "name": "一般義消"},
                    {"unit": "大園救護分隊", "title": "小隊長", "name": "常用義消"},
                ],
            },
        )
        settings = self.client.get("/worker/vehicle-settings", headers=worker_headers).get_json()

        saved = self.client.post(
            "/admin/civilpower-preferences",
            headers=nas_headers,
            data={
                "settings_revision": settings["revision"],
                "frequent_civilpower_member_ids": "常用義消",
            },
        )

        self.assertEqual(200, saved.status_code)
        settings = self.client.get("/worker/vehicle-settings", headers=worker_headers).get_json()
        self.assertEqual(["常用義消"], settings["frequent_civilpower_member_ids"])
        settings_body = html.unescape(self.client.get("/admin/vehicles", headers=nas_headers).data.decode("utf-8"))
        self.assertIn("救護義消名冊", settings_body)
        self.assertIn('value="常用義消" checked', settings_body)
        self.assertNotIn("小隊長", settings_body)
        self.assertIn(
            ".civilpower-member-list { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; margin-bottom: 12px; }",
            settings_body,
        )
        self.assertIn("min-height: 38px;", settings_body)
        self.assertIn("padding: 7px 9px;", settings_body)

        os.environ["WORKER_SERVER_URL"] = ""
        self.import_case_for_form(self.valid_task_data())
        form_body = html.unescape(self.client.get("/app").data.decode("utf-8"))
        frequent_index = form_body.index('value="常用義消"')
        divider_index = form_body.index('data-civilpower-roster-divider')
        other_index = form_body.index('value="一般義消"')
        self.assertIn('<optgroup label="常出勤人員">', form_body)
        self.assertIn('<hr data-civilpower-roster-divider>', form_body)
        self.assertIn('<optgroup label="其他人員">', form_body)
        self.assertLess(frequent_index, divider_index)
        self.assertLess(divider_index, other_index)
        self.assertNotIn("常用義消（", form_body)

    def test_volunteer_assist_can_run_independently_after_duty_work_log(self):
        site_statuses = {
            "duty_work_log": {"status": "duty_work_log_saved"},
            "vehicle_mileage": {"status": "vehicle_mileage_running"},
            "fuel_record": {"status": "fuel_record_running"},
            "consumables": {"status": "consumables_running"},
            "disinfection": {"status": "disinfection_running"},
            "volunteer_assist": {"status": "created"},
        }

        self.assertEqual("duty_work_log", app_module.SITE_RUN_ORDER[0])
        self.assertEqual("volunteer_assist", app_module.SITE_RUN_ORDER[1])
        self.assertTrue(app_module.site_can_run_individually(site_statuses, "volunteer_assist"))

        site_statuses["duty_work_log"] = {"status": "duty_work_log_running"}
        self.assertFalse(app_module.site_can_run_individually(site_statuses, "volunteer_assist"))

    def test_worker_roster_failure_preserves_last_successful_snapshot(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        self.client.post(
            "/worker/civilpower-roster",
            headers=headers,
            json={
                "status": "civilpower_roster_loaded",
                "attempted_at": "2026-08-12T09:00:00",
                "last_success_at": "2026-08-12T09:00:00",
                "members": [{"unit": "大園救護分隊", "title": "隊員", "name": "測試義消"}],
            },
        )

        failed = self.client.post(
            "/worker/civilpower-roster",
            headers=headers,
            json={
                "status": "civilpower_roster_failed",
                "detail": "登入失敗",
                "attempted_at": "2026-08-19T09:00:00",
                "members": [],
            },
        )

        self.assertEqual(failed.status_code, 200)
        roster = app_module.read_civilpower_roster()
        self.assertEqual("civilpower_roster_failed", roster["status"])
        self.assertEqual("2026-08-12T09:00:00", roster["last_success_at"])
        self.assertEqual(["測試義消"], [member["name"] for member in roster["members"]])

    def test_worker_cases_rejects_unsolicited_nonempty_request_id(self):
        os.environ["WORKER_TOKEN"] = "test-token"

        response = self.client.post(
            "/worker/cases",
            headers={"X-Worker-Token": "test-token"},
            json={
                "request_id": "unknown-request",
                "status": "cases_loaded",
                "detail": "must be rejected",
                "cases": [{"case_id": "unsolicited"}],
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse((app_module.artifacts_dir / "cases" / "latest.json").exists())

    def test_worker_cases_requires_request_id_while_manual_request_is_active(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        app_module.write_case_lookup_request("24h", source="NAS", mode="worker_queue")

        response = self.client.post(
            "/worker/cases",
            headers={"X-Worker-Token": "test-token"},
            json={
                "status": "cases_loaded",
                "detail": "missing request identity",
                "cases": [{"case_id": "missing-id"}],
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse((app_module.artifacts_dir / "cases" / "latest.json").exists())

    def test_worker_case_lookup_failure_does_not_become_empty_success(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        lookup_request = app_module.write_case_lookup_request("24h", source="NAS端", mode="worker_queue")
        self.assertIn("request_id", lookup_request)

        response = self.client.post(
            "/worker/cases",
            headers={"X-Worker-Token": "test-token"},
            json={
                "request_id": lookup_request["request_id"],
                "status": "case_lookup_failed",
                "detail": "登入失敗",
                "cases": [],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(app_module.read_case_lookup_request()["status"], "case_lookup_failed")
        prepared = app_module.prepared_case_lookup()
        self.assertEqual(prepared["detail"], "登入失敗")
        self.assertNotIn("empty_message", prepared)

    def test_worker_api_requires_configured_token(self):
        os.environ["WORKER_TOKEN"] = ""

        response = self.client.get("/worker/tasks")

        self.assertEqual(response.status_code, 403)

    def test_worker_tasks_api_requires_token_and_returns_recent_tasks(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        denied = self.client.get("/worker/tasks")
        self.assertEqual(denied.status_code, 403)

        list_response = self.client.get("/worker/tasks", headers={"X-Worker-Token": "test-token"})
        self.assertEqual(list_response.status_code, 200)
        list_payload = list_response.get_json()
        self.assertEqual(list_payload["tasks"][0]["task"]["task_id"], task_id)

        task_response = self.client.get(f"/worker/tasks/{task_id}", headers={"X-Worker-Token": "test-token"})
        self.assertEqual(task_response.status_code, 200)
        self.assertEqual(task_response.get_json()["task"]["driver"], "\u66fe\u5f65\u7db8")

    def test_import_case_redirects_to_app(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        (cases_dir / "latest.json").write_text(
            """
            {
              "status": "cases_loaded",
              "updated_at": "2026-06-03T08:00:00",
              "cases": [
                {
                  "case_id": "20260602090556012",
                  "address": "桃園市觀音區",
                  "case_time_hhmm": "0905",
                  "personnel": ["吳宗耕", "楊弘宇"]
                }
              ]
            }
            """,
            encoding="utf-8",
        )

        response = self.client.post("/cases/import", data={"case_id": "20260602090556012"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/app#task-form")
        imported_response = self.client.get("/app")
        imported_body = html.unescape(imported_response.data.decode("utf-8"))
        self.assertIn("0905", imported_body)
        self.assertIn(" checked>", imported_body)
        self.assertIn('formaction="/cases/clear"', imported_body)
        self.assertIn("const baselineConsumablesLoaded = true;", imported_body)
        self.assertEqual(app_module.read_selected_case(), {})

        refreshed_response = self.client.get("/app")
        refreshed_body = html.unescape(refreshed_response.data.decode("utf-8"))
        self.assertNotIn('value="0905"', refreshed_body)
        self.assertNotIn('value="桃園市觀音區"', refreshed_body)
        self.assertNotIn("const baselineConsumablesLoaded = true;", refreshed_body)
        self.assertIn("請先從上方案件按「帶入」", refreshed_body)

        clear_response = self.client.post("/cases/clear", follow_redirects=False)
        self.assertEqual(clear_response.status_code, 302)
        self.assertIn("/app?notice=", clear_response.headers["Location"])
        self.assertEqual(app_module.read_selected_case(), {})
        cleared_response = self.client.get(clear_response.headers["Location"])
        cleared_body = html.unescape(cleared_response.data.decode("utf-8"))
        self.assertIn("已清除資料", cleared_body)
        self.assertNotIn('value="0905"', cleared_body)
        self.assertNotIn('value="桃園市觀音區"', cleared_body)
        self.assertNotIn(" checked>", cleared_body)
        self.assertIn("const defaultConsumables = {};", cleared_body)
        self.assertIn("const baselineConsumablesLoaded = false;", cleared_body)

        self.client.post("/cases/import", data={"case_id": "20260602090556012"}, follow_redirects=False)
        self.client.post("/tasks", data=self.valid_task_data(), follow_redirects=False)
        self.assertEqual(app_module.read_selected_case(), {})

    def test_opening_task_pages_clears_stale_imported_case_without_pending_import(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)

        for path, form_id in (("/app", "task-form"), ("/app/disaster", "disaster-form")):
            with self.subTest(path=path):
                app_module.write_json_atomic(
                    cases_dir / "selected.json",
                    {
                        "status": "case_imported",
                        "selected_case": {
                            "case_id": "stale-case",
                            "address": "舊案件地址",
                            "personnel": ["舊人員"],
                        },
                    },
                )

                response = self.client.get(path)
                body = html.unescape(response.data.decode("utf-8"))

                self.assertEqual(200, response.status_code)
                self.assertNotIn('value="stale-case"', body)
                self.assertNotIn('value="舊案件地址"', body)
                self.assertNotIn(f'id="{form_id}"', body)
                self.assertEqual({}, app_module.read_selected_case())

    def test_replacing_imported_case_shows_clear_and_import_notice(self):
        cases_dir = app_module.artifacts_dir / "cases"
        cases_dir.mkdir(parents=True)
        app_module.write_json_atomic(
            cases_dir / "latest.json",
            {
                "status": "cases_loaded",
                "cases": [
                    {"case_id": "case-one", "address": "第一案件地址"},
                    {"case_id": "case-two", "address": "第二案件地址"},
                ],
            },
        )

        first = self.client.post("/cases/import", data={"case_id": "case-one"}, follow_redirects=False)
        self.assertEqual("/app#task-form", first.headers["Location"])
        self.client.get("/app")

        replacement = self.client.post(
            "/cases/import",
            data={"case_id": "case-two", "replace_existing": "1"},
            follow_redirects=False,
        )
        self.assertIn("/app?notice=", replacement.headers["Location"])

        body = html.unescape(self.client.get(replacement.headers["Location"]).data.decode("utf-8"))
        self.assertIn("已清除資料並帶入", body)
        self.assertIn("第二案件地址", body)
        self.assertEqual({}, app_module.read_selected_case())

    def test_task_detail_run_does_not_allow_blind_manual_complete(self):
        os.environ["WORKER_TOKEN"] = "0123456789abcdef0123456789abcdef"
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                case_address="\u6843\u5712\u5e02\u89c0\u97f3\u5340",
                case_reason="\u8eca\u798d",
                case_time="1420",
                return_time="1505",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        self.client.post(f"/tasks/{task_id}/confirm-case-closed")
        detail_response = self.client.get(f"/tasks/{task_id}")
        self.assertEqual(detail_response.status_code, 200)
        detail_body = html.unescape(detail_response.data.decode("utf-8"))
        self.assertEqual(detail_body.count("\u55ae\u7368\u767b\u6253"), 0)
        self.assertIn("四站登打啟動", detail_body)
        self.assertNotIn("送到公務電腦", detail_body)
        self.assertIn("main { max-width: 1080px;", detail_body)
        self.assertIn("repeating-linear-gradient", detail_body)

        run_response = self.client.post(f"/tasks/{task_id}/run", follow_redirects=False)
        self.assertEqual(run_response.status_code, 302)
        app_module.runner.wait_for_idle()

        complete_response = self.client.post(
            f"/tasks/{task_id}/sites/vehicle_mileage/complete",
            data={
                "confirmation_token": app_module.site_manual_complete_token(
                    task_id,
                    "vehicle_mileage",
                )
            },
            follow_redirects=False,
        )
        self.assertEqual(complete_response.status_code, 409)
        payload = self.store.get(task_id)
        self.assertNotEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "completed_by_user")

    def test_waiting_confirmation_shows_manual_confirmation_without_blind_retry(self):
        os.environ["WORKER_TOKEN"] = "0123456789abcdef0123456789abcdef"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "vehicle_mileage",
                "車輛里程",
                "vehicle_mileage_waiting_confirmation",
                "已按儲存，但未偵測到成功回應。",
            ),
        )

        response = self.client.get(
            f"/tasks/{task_id}",
            base_url="http://100.114.126.58:8080",
        )
        body = html.unescape(response.get_data(as_text=True))
        mileage_card = body[body.index("<h3>里程</h3>") : body.index("<h3>耗材</h3>")]

        self.assertIn("待人工確認", mileage_card)
        self.assertIn(f"/tasks/{task_id}/sites/vehicle_mileage/complete", mileage_card)
        self.assertIn("已在官方頁確認完成", mileage_card)
        confirmation_token = app_module.site_manual_complete_token(task_id, "vehicle_mileage")
        self.assertIn(f'value="{confirmation_token}"', mileage_card)
        self.assertNotIn(f"/tasks/{task_id}/sites/vehicle_mileage/run", mileage_card)
        self.assertNotIn("四站登打啟動", body)
        self.assertNotIn(f'href="/tasks/{task_id}/edit"', body)

        edit_response = self.client.get(f"/tasks/{task_id}/edit")
        self.assertEqual(edit_response.status_code, 409)
        self.assertIn("待人工確認", edit_response.get_data(as_text=True))

        missing_token_response = self.client.post(
            f"/tasks/{task_id}/sites/vehicle_mileage/complete",
            base_url="http://100.114.126.58:8080",
            follow_redirects=False,
        )
        self.assertEqual(missing_token_response.status_code, 403)

        complete_response = self.client.post(
            f"/tasks/{task_id}/sites/vehicle_mileage/complete",
            data={"confirmation_token": confirmation_token},
            base_url="http://100.114.126.58:8080",
            follow_redirects=False,
        )
        self.assertEqual(complete_response.status_code, 302)
        self.assertEqual(
            self.store.get(task_id)["site_statuses"]["vehicle_mileage"]["status"],
            "completed_by_user",
        )

    def test_waiting_confirmation_rejects_direct_full_and_single_site_restart(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "vehicle_mileage",
                "車輛里程",
                "vehicle_mileage_waiting_confirmation",
                "已按儲存，但未偵測到成功回應。",
            ),
        )

        full_response = self.client.post(f"/tasks/{task_id}/run", follow_redirects=False)
        site_response = self.client.post(
            f"/tasks/{task_id}/sites/vehicle_mileage/run",
            follow_redirects=False,
        )

        self.assertEqual(full_response.status_code, 409)
        self.assertEqual(site_response.status_code, 409)
        self.assertEqual(app_module.desktop_runner.started, [])
        self.assertEqual(app_module.desktop_runner.started_sites, [])

    def test_manual_complete_rejects_site_that_is_not_waiting_for_confirmation(self):
        os.environ["WORKER_TOKEN"] = "0123456789abcdef0123456789abcdef"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "vehicle_mileage",
                "車輛里程",
                "vehicle_mileage_failed",
                "官方頁未儲存",
            ),
        )

        response = self.client.post(
            f"/tasks/{task_id}/sites/vehicle_mileage/complete",
            data={
                "confirmation_token": app_module.site_manual_complete_token(
                    task_id,
                    "vehicle_mileage",
                )
            },
            base_url="http://100.114.126.58:8080",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.store.get(task_id)["site_statuses"]["vehicle_mileage"]["status"],
            "vehicle_mileage_failed",
        )

    def test_manual_complete_reports_updated_public_pc_payload(self):
        os.environ["WORKER_TOKEN"] = "0123456789abcdef0123456789abcdef"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "vehicle_mileage",
                "車輛里程",
                "vehicle_mileage_waiting_confirmation",
                "已按儲存，但未偵測到成功回應。",
            ),
        )

        with mock.patch.object(app_module, "report_public_pc_task_event") as report:
            response = self.client.post(
                f"/tasks/{task_id}/sites/vehicle_mileage/complete",
                data={
                    "confirmation_token": app_module.site_manual_complete_token(
                        task_id,
                        "vehicle_mileage",
                    )
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        report.assert_called_once()
        reported_payload, action = report.call_args.args
        self.assertEqual(reported_payload, self.store.get(task_id))
        self.assertEqual(
            reported_payload["site_statuses"]["vehicle_mileage"]["status"],
            "completed_by_user",
        )
        self.assertEqual(action, "人工確認站別完成：車輛里程")

    def test_manual_complete_does_not_report_rejected_requests(self):
        os.environ["WORKER_TOKEN"] = "0123456789abcdef0123456789abcdef"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "vehicle_mileage",
                "車輛里程",
                "vehicle_mileage_waiting_confirmation",
                "已按儲存，但未偵測到成功回應。",
            ),
        )

        with mock.patch.object(app_module, "report_public_pc_task_event") as report:
            forbidden = self.client.post(
                f"/tasks/{task_id}/sites/vehicle_mileage/complete",
                data={"confirmation_token": "wrong"},
                follow_redirects=False,
            )
            missing_task_id = "missing-task"
            missing = self.client.post(
                f"/tasks/{missing_task_id}/sites/vehicle_mileage/complete",
                data={
                    "confirmation_token": app_module.site_manual_complete_token(
                        missing_task_id,
                        "vehicle_mileage",
                    )
                },
                follow_redirects=False,
            )
            payload = self.store.get(task_id)
            payload["site_statuses"]["vehicle_mileage"].update(
                status="vehicle_mileage_failed",
                detail="官方頁未儲存。",
            )
            self.store.save_payload(task_id, payload)
            conflict = self.client.post(
                f"/tasks/{task_id}/sites/vehicle_mileage/complete",
                data={
                    "confirmation_token": app_module.site_manual_complete_token(
                        task_id,
                        "vehicle_mileage",
                    )
                },
                follow_redirects=False,
            )

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(conflict.status_code, 409)
        report.assert_not_called()

    def test_multi_vehicle_manual_confirmation_only_confirms_waiting_vehicle(self):
        os.environ["WORKER_TOKEN"] = "0123456789abcdef0123456789abcdef"
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                two_vehicle="1",
                two_vehicle_available="1",
                vehicle="新坡92",
                vehicle_2="新坡93",
                driver_2="乙",
                mileage_2="200",
                return_date_2="2026-06-07",
                return_time_2="1130",
                patient_summary_2="女一名",
                consumables_2="桃-口罩(片)=2",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "vehicle_mileage",
                "車輛里程",
                "vehicle_mileage_waiting_confirmation",
                "92 儲存回應不明",
            ),
            vehicle_key="新坡92",
            vehicle_label="第一車 新坡92",
        )
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "vehicle_mileage",
                "車輛里程",
                "vehicle_mileage_failed",
                "93 未儲存",
            ),
            vehicle_key="新坡93",
            vehicle_label="第二車 新坡93",
        )

        detail = self.client.get(
            f"/tasks/{task_id}",
            base_url="http://100.114.126.58:8080",
        ).get_data(as_text=True)
        mileage_card = html.unescape(detail[detail.index("<h3>里程</h3>") : detail.index("<h3>耗材</h3>")])

        self.assertIn('name="vehicle_key" value="新坡92"', mileage_card)
        self.assertNotIn('name="vehicle_key" value="新坡93"', mileage_card)
        token = app_module.site_manual_complete_token(task_id, "vehicle_mileage", "新坡92")
        response = self.client.post(
            f"/tasks/{task_id}/sites/vehicle_mileage/complete",
            data={"vehicle_key": "新坡92", "confirmation_token": token},
            base_url="http://100.114.126.58:8080",
            follow_redirects=False,
        )
        site = self.store.get(task_id)["site_statuses"]["vehicle_mileage"]

        self.assertEqual(response.status_code, 302)
        self.assertEqual(site["status"], "vehicle_mileage_failed")
        self.assertEqual(site["vehicle_results"]["新坡92"]["status"], "completed_by_user")
        self.assertEqual(site["vehicle_results"]["新坡93"]["status"], "vehicle_mileage_failed")

    def test_single_site_buttons_show_for_failed_and_unfinished_sites(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "consumables",
                "\u4e00\u7ad9\u901a\u8017\u6750",
                "consumables_failed",
                "missing consumables save button",
            ),
        )

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(body.count("<button class=\"secondary\" type=\"submit\">\u55ae\u7368\u767b\u6253</button>"), 2)
        self.assertIn(f"/tasks/{task_id}/sites/consumables/run", body)
        self.assertIn(f"/tasks/{task_id}/sites/disinfection/run", body)
        consumables_card = body[body.index("<h3>\u8017\u6750</h3>") : body.index("<h3>\u6d88\u6bd2</h3>")]
        self.assertLess(consumables_card.index("\u55ae\u7368\u767b\u6253"), consumables_card.index("\u5931\u6557"))
        self.assertNotIn("\u932f\u8aa4\u6307\u5f15", body)
        task_section = body[body.index('aria-label="\u4efb\u52d9\u5167\u5bb9"') : body.index('aria-label="\u56db\u7ad9\u968e\u6bb5\u6aa2\u67e5"')]
        self.assertNotIn("\u672a\u5b8c\u6210\u9ede", task_section)
        self.assertNotIn("\u586b\u5beb\u8017\u6750\u54c1\u9805", task_section)
        self.assertNotIn("\u9801\u9762\u6309\u9215\u6216\u6b04\u4f4d\u8207\u7a0b\u5f0f\u9810\u671f\u4e0d\u540c", task_section)
        self.assertIn("\u8017\u6750\uff1a\u5931\u6557", body)
        self.assertNotIn("\u6d88\u6bd2\uff1a\u672a\u63a5\u7e8c", body)
        self.assertNotIn("\u4e94\u7ad9\u6d41\u7a0b\u5df2\u505c\u6b62", body)

    def test_task_detail_shows_failure_stage_reason_and_next_action(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "vehicle_mileage",
                "車輛里程",
                "vehicle_mileage_failed",
                "vehicle not found: 新坡91",
            ),
        )

        self.client.post(f"/tasks/{task_id}/confirm-case-closed")
        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertLess(body.index("四站登打啟動"), body.index("<h2>任務內容</h2>"))
        task_section = body[body.index('aria-label="任務內容"') : body.index('aria-label="四站階段檢查"')]
        self.assertNotIn("未完成點", task_section)
        self.assertNotIn("原因", task_section)
        self.assertNotIn("下一步", task_section)
        stage_section = body[body.index('aria-label="四站階段檢查"') :]
        self.assertIn("失敗點", stage_section)
        self.assertIn("未完成", stage_section)

    def test_task_detail_refreshes_when_later_site_runs_after_failure(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("duty_work_log", "消防勤務工作紀錄", "duty_work_log_failed", "login failed"),
        )
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_running", "running"),
        )

        detail_response = self.client.get(f"/tasks/{task_id}")
        detail_body = html.unescape(detail_response.data.decode("utf-8"))
        app_response = self.client.get("/app")
        app_body = html.unescape(app_response.data.decode("utf-8"))

        self.assertIn("window.location.reload()", detail_body)
        self.assertIn("已完成 0/4；目前：里程執行中", app_body)

    def test_task_detail_auto_refreshes_while_running(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.set_overall_status(task_id, "desktop_fast_running", "running")

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("window.location.reload()", body)
        self.assertIn("1500", body)

    def test_task_detail_auto_refreshes_when_site_status_is_running(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_running", "running"),
        )

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("window.location.reload()", body)
        self.assertIn(f'action="/tasks/{task_id}/abort"', body)

    def test_abort_running_task_stops_active_status_and_worker_browsers(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.set_overall_status(task_id, "desktop_fast_running", "running")
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_running", "running"),
        )
        set_manual_task_lock(app_module.artifacts_dir, f"desktop_fast:{task_id}")
        lock_path = app_module.artifacts_dir / "manual_task_active.lock"

        with mock.patch.object(app_module, "cleanup_active_worker_browsers", return_value=2, create=True) as cleanup:
            response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

        payload = self.store.get(task_id)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(app_module.task_payload_is_active(payload))
        self.assertEqual(payload["overall_status"], "desktop_fast_completed_with_errors")
        self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_failed")
        self.assertFalse(lock_path.exists())
        cleanup.assert_called_once_with()

    def test_abort_desktop_task_does_not_bind_marker_to_an_old_worker_claim(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        claimed = self.store.claim_task_for_worker(task_id, "old-worker")
        claim_id = claimed["worker_queue"]["claim_id"]
        payload = self.store.get(task_id)
        payload["worker_queue"]["status"] = "completed"
        payload["overall_status"] = "desktop_fast_running"
        self.store.save_payload(task_id, payload)
        owner = f"desktop_fast:{task_id}:current-run"
        set_manual_task_lock(app_module.artifacts_dir, owner)

        with mock.patch.object(app_module, "cleanup_active_worker_browsers", return_value=1):
            response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            task_cancellation_requested(
                app_module.artifacts_dir,
                task_id,
                execution_owner=owner,
            )
        )
        marker = json.loads(
            task_cancellation_marker_path(app_module.artifacts_dir, task_id).read_text(encoding="utf-8")
        )
        self.assertEqual(marker["claim_id"], "")
        self.assertNotEqual(claim_id, "")

    def test_abort_keeps_running_task_and_browser_when_cancellation_marker_cannot_be_written(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.set_overall_status(task_id, "desktop_fast_running", "running")
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_running", "running"),
        )
        owner = f"desktop_fast:{task_id}:current-run"
        set_manual_task_lock(app_module.artifacts_dir, owner)

        with mock.patch.object(
            app_module,
            "request_task_cancellation",
            side_effect=OSError("disk unavailable"),
        ), mock.patch.object(app_module, "cleanup_active_worker_browsers", return_value=1) as cleanup:
            response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

        payload = self.store.get(task_id)
        self.assertEqual(response.status_code, 503)
        self.assertIn("無法建立中止訊號", response.data.decode("utf-8"))
        self.assertEqual(payload["overall_status"], "desktop_fast_running")
        self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_running")
        self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), owner)
        cleanup.assert_not_called()

    def test_abort_commit_keeps_marker_when_desktop_lock_clear_fails(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.set_overall_status(task_id, "desktop_fast_running", "running")
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_running", "running"),
        )
        owner = f"desktop_fast:{task_id}:clear-failure"
        self.assertTrue(set_manual_task_lock(app_module.artifacts_dir, owner))
        lock_path = manual_task_lock_path(app_module.artifacts_dir)
        original_unlink = Path.unlink

        def fail_only_manual_lock(path, *args, **kwargs):
            if Path(path) == lock_path:
                raise PermissionError("manual lock is busy")
            return original_unlink(path, *args, **kwargs)

        try:
            with mock.patch.object(
                Path,
                "unlink",
                autospec=True,
                side_effect=fail_only_manual_lock,
            ), mock.patch.object(app_module, "cleanup_active_worker_browsers", return_value=1) as cleanup:
                response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

            payload = self.store.get(task_id)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(payload["overall_status"], "desktop_fast_completed_with_errors")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_failed")
            self.assertTrue(
                task_cancellation_requested(
                    app_module.artifacts_dir,
                    task_id,
                    execution_owner=owner,
                )
            )
            self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), owner)
            cleanup.assert_called_once()
        finally:
            app_module.clear_task_cancellation(
                app_module.artifacts_dir,
                task_id,
                execution_owner=owner,
            )
            clear_manual_task_lock(app_module.artifacts_dir, owner)

    def test_abort_owner_a_cannot_abort_claim_b_after_same_task_lease_is_replaced(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        claimed = self.store.claim_task_for_worker(task_id, "worker-a")
        claim_a = claimed["worker_queue"]["claim_id"]
        payload = self.store.get(task_id)
        payload["overall_status"] = "desktop_fast_running"
        payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_running"
        self.store.save_payload(task_id, payload)
        owner_a = f"worker-manual:{task_id}:1:1"
        set_manual_task_lock(app_module.artifacts_dir, owner_a)
        original_request_cancellation = app_module.request_task_cancellation

        def replace_claim_after_owner_a_marker(*args, **kwargs):
            marker_path = original_request_cancellation(*args, **kwargs)
            current = self.store.get(task_id)
            current["worker_queue"].update(
                {
                    "status": "claimed",
                    "claim_id": "claim-b",
                    "worker_id": "worker-b",
                }
            )
            current["worker"]["claim_id"] = "claim-b"
            current["worker"]["id"] = "worker-b"
            current["overall_status"] = "desktop_fast_running"
            current["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_running"
            self.store.save_payload(task_id, current)
            return marker_path

        with mock.patch.object(
            app_module,
            "request_task_cancellation",
            side_effect=replace_claim_after_owner_a_marker,
        ), mock.patch.object(app_module, "cleanup_active_worker_browsers", return_value=1) as cleanup:
            response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

        current = self.store.get(task_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(current["worker_queue"]["status"], "claimed")
        self.assertEqual(current["worker_queue"]["claim_id"], "claim-b")
        self.assertEqual(current["overall_status"], "desktop_fast_running")
        self.assertEqual(current["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_running")
        self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), owner_a)
        self.assertFalse(
            task_cancellation_requested(
                app_module.artifacts_dir,
                task_id,
                execution_owner=owner_a,
                claim_id=claim_a,
            )
        )
        cleanup.assert_not_called()

    def test_abort_queued_generation_a_cannot_abort_requeued_generation_b(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        queued_a = self.store.queue_for_worker(task_id)
        queue_a = queued_a["worker_queue"]["queue_id"]
        original_absent_guard = app_module.run_with_manual_task_lock_absent
        queue_b = ""

        def requeue_before_guarded_abort(artifacts_dir, action):
            nonlocal queue_b
            queued_b = self.store.queue_for_worker(task_id)
            queue_b = queued_b["worker_queue"]["queue_id"]
            return original_absent_guard(artifacts_dir, action)

        with mock.patch.object(
            app_module,
            "run_with_manual_task_lock_absent",
            side_effect=requeue_before_guarded_abort,
        ):
            response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

        current = self.store.get(task_id)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(queue_a)
        self.assertTrue(queue_b)
        self.assertNotEqual(queue_a, queue_b)
        self.assertEqual(current["worker_queue"]["status"], "queued")
        self.assertEqual(current["worker_queue"]["queue_id"], queue_b)

    def test_abort_returns_conflict_when_manual_owner_changes_between_snapshots(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        owner_a = f"desktop_fast:{task_id}:owner-a"
        owner_b = f"desktop_fast:{task_id}:owner-b"
        self.assertTrue(set_manual_task_lock(app_module.artifacts_dir, owner_a))
        payload = self.store.get(task_id)
        payload["overall_status"] = "desktop_fast_running"
        self.store.save_payload(task_id, payload)
        real_snapshot = app_module.manual_task_lock_snapshot
        snapshot_calls = 0

        def replace_owner_on_second_snapshot(artifacts_dir):
            nonlocal snapshot_calls
            snapshot_calls += 1
            if snapshot_calls == 2:
                self.assertTrue(clear_manual_task_lock(artifacts_dir, owner_a))
                self.assertTrue(set_manual_task_lock(artifacts_dir, owner_b))
                current = self.store.get(task_id)
                current["overall_status"] = "desktop_fast_running"
                self.store.save_payload(task_id, current)
            return real_snapshot(artifacts_dir)

        try:
            with mock.patch.object(
                app_module,
                "manual_task_lock_snapshot",
                side_effect=replace_owner_on_second_snapshot,
            ), mock.patch.object(app_module, "cleanup_active_worker_browsers") as cleanup:
                response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.store.get(task_id)["overall_status"], "desktop_fast_running")
            self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), owner_b)
            cleanup.assert_not_called()
        finally:
            clear_manual_task_lock(app_module.artifacts_dir, owner_b)

    def test_abort_returns_conflict_when_legacy_owner_is_recreated_with_same_text(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        legacy_owner = f"desktop_fast:{task_id}:legacy-fixed-owner"
        self.assertTrue(set_manual_task_lock(app_module.artifacts_dir, legacy_owner))
        payload = self.store.get(task_id)
        payload["overall_status"] = "desktop_fast_running"
        self.store.save_payload(task_id, payload)
        real_snapshot = app_module.manual_task_lock_snapshot
        snapshot_calls = 0

        def recreate_same_owner_on_second_snapshot(artifacts_dir):
            nonlocal snapshot_calls
            snapshot_calls += 1
            if snapshot_calls == 2:
                self.assertTrue(clear_manual_task_lock(artifacts_dir, legacy_owner))
                time.sleep(0.01)
                self.assertTrue(set_manual_task_lock(artifacts_dir, legacy_owner))
                current = self.store.get(task_id)
                current["overall_status"] = "desktop_fast_running"
                self.store.save_payload(task_id, current)
            return real_snapshot(artifacts_dir)

        try:
            with mock.patch.object(
                app_module,
                "manual_task_lock_snapshot",
                side_effect=recreate_same_owner_on_second_snapshot,
            ):
                response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.store.get(task_id)["overall_status"], "desktop_fast_running")
            self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), legacy_owner)
        finally:
            clear_manual_task_lock(app_module.artifacts_dir, legacy_owner)

    def test_abort_owner_guard_rejects_same_legacy_owner_recreated_after_snapshots(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        legacy_owner = f"desktop_fast:{task_id}:legacy-fixed-owner"
        self.assertTrue(set_manual_task_lock(app_module.artifacts_dir, legacy_owner))
        payload = self.store.get(task_id)
        payload["overall_status"] = "desktop_fast_running"
        self.store.save_payload(task_id, payload)
        original_owner_guard = app_module.run_with_manual_task_lock_owner

        def recreate_before_owner_guard(*args, **kwargs):
            self.assertTrue(clear_manual_task_lock(app_module.artifacts_dir, legacy_owner))
            time.sleep(0.01)
            self.assertTrue(set_manual_task_lock(app_module.artifacts_dir, legacy_owner))
            current = self.store.get(task_id)
            current["overall_status"] = "desktop_fast_running"
            self.store.save_payload(task_id, current)
            return original_owner_guard(*args, **kwargs)

        try:
            with mock.patch.object(
                app_module,
                "run_with_manual_task_lock_owner",
                side_effect=recreate_before_owner_guard,
            ):
                response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.store.get(task_id)["overall_status"], "desktop_fast_running")
            self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), legacy_owner)
        finally:
            clear_manual_task_lock(app_module.artifacts_dir, legacy_owner)

    def test_abort_without_owner_cannot_abort_desktop_owner_started_before_mutation(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        owner_b = f"desktop_fast:{task_id}:owner-b"
        original_absent_guard = app_module.run_with_manual_task_lock_absent

        def start_owner_b_before_absent_guard(artifacts_dir, action):
            self.assertTrue(set_manual_task_lock(artifacts_dir, owner_b))
            current = self.store.get(task_id)
            current["overall_status"] = "desktop_fast_running"
            self.store.save_payload(task_id, current)
            return original_absent_guard(artifacts_dir, action)

        try:
            with mock.patch.object(
                app_module,
                "run_with_manual_task_lock_absent",
                side_effect=start_owner_b_before_absent_guard,
            ), mock.patch.object(app_module, "cleanup_active_worker_browsers") as cleanup:
                response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

            current = self.store.get(task_id)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(current["overall_status"], "desktop_fast_running")
            self.assertEqual(current["worker_queue"]["status"], "queued")
            self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), owner_b)
            cleanup.assert_not_called()
        finally:
            clear_manual_task_lock(app_module.artifacts_dir, owner_b)

    def test_desktop_run_is_abortable_before_background_thread_executes(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        real_runner = app_module.DesktopFastRunner(app_module.artifacts_dir, store=self.store)
        app_module.desktop_runner = real_runner
        try:
            with mock.patch.object(
                app_module,
                "effective_task_execution_mode",
                return_value="desktop_fast",
            ), mock.patch(
                "ambulance_bot.desktop_fast_runner.threading.Thread"
            ) as thread_mock, mock.patch.object(
                app_module,
                "cleanup_active_worker_browsers",
                return_value=0,
            ):
                run_response = self.client.post(f"/tasks/{task_id}/run", follow_redirects=False)
                abort_response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

            payload = self.store.get(task_id)
            self.assertEqual(run_response.status_code, 302)
            self.assertEqual(abort_response.status_code, 302)
            self.assertEqual(thread_mock.call_count, 1)
            self.assertEqual(payload["overall_status"], "desktop_fast_completed_with_errors")
            self.assertFalse(manual_task_lock_active(app_module.artifacts_dir))
        finally:
            with real_runner._lock:
                real_runner._running.clear()
                real_runner._execution_owners.clear()

    def test_abort_inactive_task_does_not_clear_or_kill_another_task_owner(self):
        first_response = self.client.post("/tasks", data=self.valid_task_data())
        inactive_task_id = first_response.headers["Location"].rstrip("/").split("/")[-1]
        other_task_id = "running-task-b"
        owner = f"desktop_fast:{other_task_id}"
        set_manual_task_lock(app_module.artifacts_dir, owner)

        with mock.patch.object(app_module, "cleanup_active_worker_browsers", return_value=3) as cleanup:
            response = self.client.post(f"/tasks/{inactive_task_id}/abort", follow_redirects=False)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), owner)
        cleanup.assert_not_called()

    def test_abort_missing_task_does_not_clear_or_kill_current_task_owner(self):
        owner = "desktop_fast:running-task-b"
        set_manual_task_lock(app_module.artifacts_dir, owner)

        with mock.patch.object(app_module, "cleanup_active_worker_browsers", return_value=3) as cleanup:
            response = self.client.post("/tasks/missing-task-a/abort", follow_redirects=False)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), owner)
        cleanup.assert_not_called()

    def test_abort_worker_manual_task_signals_exact_owner_and_closes_its_browser(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        payload = self.store.get(task_id)
        payload["site_statuses"]["consumables"]["status"] = "manual_captcha_required"
        self.store.save_payload(task_id, payload)
        owner = f"worker-manual:{task_id}:123:456"
        set_manual_task_lock(app_module.artifacts_dir, owner)

        with mock.patch.object(app_module, "cleanup_active_worker_browsers", return_value=1) as cleanup:
            response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        cleanup.assert_called_once_with()
        self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), owner)
        self.assertTrue(
            task_cancellation_requested(
                app_module.artifacts_dir,
                task_id,
                execution_owner=owner,
            )
        )

    def test_abort_does_not_infer_auto_claim_owner_for_a_different_bound_task(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        self.store.claim_task_for_worker(task_id, "worker-a")
        owner = "worker-manual:__auto_claim__:123:456"
        set_manual_task_lock(app_module.artifacts_dir, owner, task_id="different-task-b")

        with mock.patch.object(app_module, "cleanup_active_worker_browsers", return_value=2) as cleanup:
            response = self.client.post(f"/tasks/{task_id}/abort", follow_redirects=False)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(manual_task_lock_owner(app_module.artifacts_dir), owner)
        self.assertEqual(self.store.get(task_id)["worker_queue"]["status"], "claimed")
        cleanup.assert_not_called()

    def test_waiting_site_with_live_worker_claim_is_still_active(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        self.store.claim_task_for_worker(task_id, "worker-a")
        payload = self.store.get(task_id)
        payload["site_statuses"]["consumables"]["status"] = "manual_captcha_required"
        self.store.save_payload(task_id, payload)

        refreshed = self.store.get(task_id)
        self.assertEqual(app_module.status_class(app_module.effective_task_status(refreshed)), "waiting")
        self.assertTrue(app_module.task_payload_is_active(refreshed))

    def test_waiting_site_with_matching_manual_lease_is_still_active(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        payload = self.store.get(task_id)
        payload["site_statuses"]["consumables"]["status"] = "manual_captcha_required"
        self.store.save_payload(task_id, payload)
        set_manual_task_lock(app_module.artifacts_dir, f"desktop_fast:{task_id}")

        self.assertTrue(app_module.task_payload_is_active(self.store.get(task_id)))

    def test_task_detail_hides_entry_buttons_while_task_is_active(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("consumables", "一站通耗材", "consumables_running", "running"),
        )

        response = self.client.get(f"/tasks/{task_id}", base_url="http://127.0.0.1:8080")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn(f'action="/tasks/{task_id}/abort"', body)
        self.assertNotIn("四站登打啟動", body)
        self.assertNotIn("五站登打啟動", body)
        self.assertNotIn("單獨登打", body)

    def test_localhost_run_does_not_start_new_runner_when_task_is_active(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("consumables", "一站通耗材", "consumables_running", "running"),
        )

        response = self.client.post(f"/tasks/{task_id}/run", base_url="http://127.0.0.1:8080", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started, [])

    def test_localhost_run_expires_stale_running_task_before_starting_new_runner(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("consumables", "一站通耗材", "consumables_running", "running"),
        )
        path = self.store.path_for(task_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["site_statuses"]["consumables"]["updated_at"] = (
            datetime.now() - timedelta(minutes=11)
        ).isoformat(timespec="seconds")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        with mock.patch.object(app_module, "cleanup_active_worker_browsers", return_value=1, create=True) as cleanup:
            response = self.client.post(f"/tasks/{task_id}/run", base_url="http://127.0.0.1:8080", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started, [task_id])
        self.assertEqual(self.store.get(task_id)["overall_status"], "desktop_fast_running")
        cleanup.assert_not_called()

    def test_task_detail_does_not_expire_stale_site_while_worker_claim_lease_is_live(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        claimed = self.store.claim_task_for_worker(task_id, "worker-a")
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("consumables", "一站通耗材", "consumables_running", "running"),
        )
        payload = self.store.get(task_id)
        payload["site_statuses"]["consumables"]["updated_at"] = (
            datetime.now() - timedelta(minutes=11)
        ).isoformat(timespec="seconds")
        payload["worker_queue"]["lease_expires_at"] = claimed["worker_queue"]["lease_expires_at"]
        self.store.save_payload(task_id, payload)

        response = self.client.get(
            f"/tasks/{task_id}",
            base_url="http://100.114.126.58:8080",
        )
        refreshed = self.store.get(task_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(refreshed["site_statuses"]["consumables"]["status"], "consumables_running")
        self.assertEqual(refreshed["worker_queue"]["status"], "claimed")

    def test_task_detail_does_not_expire_stale_site_while_manual_lock_heartbeat_is_live(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("consumables", "一站通耗材", "consumables_running", "running"),
        )
        payload = self.store.get(task_id)
        payload["site_statuses"]["consumables"]["updated_at"] = (
            datetime.now() - timedelta(minutes=11)
        ).isoformat(timespec="seconds")
        self.store.save_payload(task_id, payload)
        set_manual_task_lock(app_module.artifacts_dir, f"desktop-fast:{task_id}")

        response = self.client.get(f"/tasks/{task_id}", base_url="http://127.0.0.1:8080")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.store.get(task_id)["site_statuses"]["consumables"]["status"],
            "consumables_running",
        )

    def test_localhost_single_site_run_does_not_start_new_runner_when_task_is_active(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("consumables", "一站通耗材", "consumables_running", "running"),
        )

        response = self.client.post(
            f"/tasks/{task_id}/sites/disinfection/run",
            base_url="http://127.0.0.1:8080",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started_sites, [])

    def test_localhost_single_site_run_uses_desktop_fast_runner(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.post(
            f"/tasks/{task_id}/sites/disinfection/run",
            base_url="http://127.0.0.1:8080",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started_sites, [(task_id, "disinfection")])
        self.assertEqual(self.store.get(task_id)["overall_status"], "desktop_fast_running")

    def test_remote_single_site_run_queues_the_selected_site_for_public_pc_worker(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.post(
            f"/tasks/{task_id}/sites/disinfection/run",
            base_url="http://100.114.126.58:8080",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started_sites, [])
        queued = self.store.get(task_id)
        self.assertEqual(queued["overall_status"], "queued_for_worker")
        self.assertEqual(queued["worker_queue"]["run_site_key"], "disinfection")

    def test_nas_failed_task_shows_public_pc_retry_controls(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "disinfection",
                "消毒",
                "disinfection_failed",
                "save button missing",
            ),
        )

        self.client.post(f"/tasks/{task_id}/confirm-case-closed")
        response = self.client.get(
            f"/tasks/{task_id}",
            base_url="http://100.114.126.58:8080",
        )
        body = html.unescape(response.get_data(as_text=True))

        self.assertIn("四站登打啟動", body)
        self.assertIn(f"/tasks/{task_id}/sites/disinfection/run", body)
        self.assertIn("單獨登打", body)

    def test_nas_ems_admin_exposes_failed_site_retry_and_returns_to_admin(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "consumables",
                "一站通耗材",
                "consumables_failed",
                "耗材儲存失敗",
            ),
        )
        payload = self.store.get(task_id)
        app_module.upsert_public_pc_report(
            {
                "event_id": "evt-admin-retry",
                "task_id": task_id,
                "title": "NAS 重送測試",
                "task": payload["task"],
                "status": "desktop_fast_completed_with_errors",
                "overall_status": "desktop_fast_completed_with_errors",
                "site_statuses": payload["site_statuses"],
            }
        )

        page = self.client.get("/admin/ems", base_url="http://100.114.126.58:8080")
        body = html.unescape(page.get_data(as_text=True))

        self.assertNotIn(f'href="/tasks/{task_id}"', body)
        self.assertIn(f'action="/tasks/{task_id}/sites/consumables/run"', body)
        self.assertIn('name="return_service" value="ems"', body)
        self.assertIn("重新送出", body)

        response = self.client.post(
            f"/tasks/{task_id}/sites/consumables/run",
            base_url="http://100.114.126.58:8080",
            data={"return_service": "ems"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin/ems")
        self.assertEqual(self.store.get(task_id)["worker_queue"]["run_site_key"], "consumables")

    def test_nas_ems_admin_materializes_report_only_task_for_retry(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "consumables",
                "一站通耗材",
                "consumables_failed",
                "耗材儲存失敗",
            ),
        )
        payload = self.store.get(task_id)
        app_module.upsert_public_pc_report(
            {
                "event_id": "evt-admin-report-only-retry",
                "task_id": task_id,
                "title": "公務電腦回報重送測試",
                "task": payload["task"],
                "status": "desktop_fast_completed_with_errors",
                "overall_status": "desktop_fast_completed_with_errors",
                "site_statuses": payload["site_statuses"],
            }
        )

        report_only_store = JsonTaskStore(Path(self.tmp.name) / "report-only-tasks")
        app_module.store = report_only_store
        app_module.runner = TaskRunner(Path(self.tmp.name), store=report_only_store)
        app_module.desktop_runner = FakeDesktopRunner(report_only_store)

        page = self.client.get("/admin/ems", base_url="http://100.114.126.58:8080")
        body = html.unescape(page.get_data(as_text=True))

        self.assertNotIn(f'href="/tasks/{task_id}"', body)
        self.assertIn(f'action="/tasks/{task_id}/sites/consumables/run"', body)

        response = self.client.post(
            f"/tasks/{task_id}/sites/consumables/run",
            base_url="http://100.114.126.58:8080",
            data={"return_service": "ems"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin/ems")
        self.assertEqual(report_only_store.get(task_id)["worker_queue"]["run_site_key"], "consumables")

    def test_nas_ems_admin_retry_preserves_public_pc_origin_and_syncs_completion(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(case_id="case-admin-retry-completion"),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        payload = self.store.get(task_id)
        for site in payload["site_statuses"].values():
            site.update(status="completed_by_user", detail="前次已完成")
        payload["site_statuses"]["consumables"].update(
            status="consumables_failed",
            detail="耗材儲存失敗",
        )
        payload["overall_status"] = "desktop_fast_completed_with_errors"
        self.store.save_payload(task_id, payload)
        app_module.upsert_public_pc_report(
            {
                "event_id": "evt-admin-retry-completion",
                "task_id": task_id,
                "title": "公務電腦重送完成測試",
                "task": payload["task"],
                "status": "desktop_fast_completed_with_errors",
                "overall_status": "desktop_fast_completed_with_errors",
                "site_statuses": payload["site_statuses"],
            }
        )

        report_only_store = JsonTaskStore(Path(self.tmp.name) / "report-only-completion-tasks")
        app_module.store = report_only_store
        app_module.runner = TaskRunner(Path(self.tmp.name), store=report_only_store)
        app_module.desktop_runner = FakeDesktopRunner(report_only_store)

        retry_response = self.client.post(
            f"/tasks/{task_id}/sites/consumables/run",
            base_url="http://100.114.126.58:8080",
            data={"return_service": "ems"},
            follow_redirects=False,
        )
        self.assertEqual(retry_response.status_code, 302)
        self.assertEqual(retry_response.headers["Location"], "/admin/ems")

        next_response = self.client.get(
            "/worker/next-task?worker_id=test-worker",
            headers=worker_headers,
        )
        self.assertEqual(next_response.status_code, 200)
        claimed = next_response.get_json()["payload"]
        status_response = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=worker_headers,
            json={
                "status": "consumables_saved",
                "detail": "重新送出後儲存成功",
                "site_key": "consumables",
                "site_name": "一站通耗材",
                "worker_id": "test-worker",
                "claim_id": claimed["worker_queue"]["claim_id"],
            },
        )
        self.assertEqual(status_response.status_code, 200)

        report = app_module.public_pc_report_for_task(task_id)
        recent_body = html.unescape(
            self.client.get("/app", base_url="http://100.114.126.58:8080").get_data(as_text=True)
        )
        success_body = html.unescape(
            self.client.get("/admin/ems?result=success", base_url="http://100.114.126.58:8080").get_data(as_text=True)
        )
        failed_body = html.unescape(
            self.client.get("/admin/ems?result=failed", base_url="http://100.114.126.58:8080").get_data(as_text=True)
        )

        with self.subTest("recent task retains public PC origin"):
            self.assertIn("公務電腦建立", recent_body)
            self.assertNotIn("NAS建立", recent_body)
        with self.subTest("admin result follows the completed retry"):
            self.assertIsNotNone(report)
            self.assertEqual("success", app_module.public_pc_report_result(report))
            self.assertIn("公務電腦重送完成測試", success_body)
            self.assertNotIn("公務電腦重送完成測試", failed_body)

    def test_local_public_pc_task_detail_syncs_newer_nas_completion(self):
        os.environ["PUBLIC_PC_REPORT_ENABLED"] = "true"
        os.environ["WORKER_SERVER_URL"] = "http://nas.example:8080"
        os.environ["WORKER_TOKEN"] = "test-token"
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(case_id="case-local-task-sync"),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        local_payload = self.store.get(task_id)
        for site_key in ("vehicle_mileage", "consumables", "disinfection"):
            local_payload["site_statuses"][site_key].update(
                status=f"{site_key}_saved",
                detail="前次已完成",
            )
        local_payload["site_statuses"]["duty_work_log"].update(
            status="duty_work_log_failed",
            detail="前次工作紀錄登打失敗",
        )
        local_payload["site_statuses"]["vehicle_mileage"]["vehicle_results"] = {
            "vehicle-1": {"status": "vehicle_mileage_saved", "detail": "本機車輛明細"}
        }
        local_payload["site_attempts"]["duty_work_log"] = [{"attempt_id": "local-attempt"}]
        local_payload["overall_status"] = "desktop_fast_completed_with_errors"
        self.store.save_payload(task_id, local_payload)

        nas_payload = json.loads(json.dumps(local_payload, ensure_ascii=False))
        nas_payload["site_statuses"]["vehicle_mileage"].pop("vehicle_results", None)
        nas_payload["site_attempts"]["duty_work_log"] = [{"attempt_id": "nas-attempt"}]
        nas_payload["site_statuses"]["duty_work_log"].update(
            status="duty_work_log_saved",
            detail="NAS 重送後已完成",
        )
        nas_payload["overall_status"] = "desktop_fast_completed"
        nas_payload["updated_at"] = "2099-01-01T00:00:00"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return json.dumps({"ok": True, "payload": nas_payload}).encode("utf-8")

        with mock.patch.object(app_module.urllib.request, "urlopen", return_value=FakeResponse()):
            response = self.client.get(
                f"/tasks/{task_id}",
                base_url="http://127.0.0.1:8090",
            )

        body = html.unescape(response.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertIn("✓ 四站登打完成", body)
        self.assertNotIn("已完成 3/4", body)
        self.assertEqual(
            self.store.get(task_id)["site_statuses"]["duty_work_log"]["status"],
            "duty_work_log_saved",
        )
        synced_payload = self.store.get(task_id)
        self.assertIn(
            "vehicle-1",
            synced_payload["site_statuses"]["vehicle_mileage"]["vehicle_results"],
        )
        self.assertEqual(
            [item["attempt_id"] for item in synced_payload["site_attempts"]["duty_work_log"]],
            ["local-attempt", "nas-attempt"],
        )

    def test_nas_ems_admin_reconciles_completed_legacy_materialized_retry(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(case_id="case-admin-legacy-retry"),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        payload = self.store.get(task_id)
        for site in payload["site_statuses"].values():
            site.update(status="completed_by_user", detail="前次已完成")
        payload["site_statuses"]["consumables"].update(
            status="consumables_failed",
            detail="耗材儲存失敗",
        )
        payload["overall_status"] = "desktop_fast_completed_with_errors"
        self.store.save_payload(task_id, payload)
        app_module.upsert_public_pc_report(
            {
                "event_id": "evt-admin-legacy-retry",
                "task_id": task_id,
                "title": "舊版公務電腦重送完成測試",
                "task": payload["task"],
                "status": "desktop_fast_completed_with_errors",
                "overall_status": "desktop_fast_completed_with_errors",
                "site_statuses": payload["site_statuses"],
            }
        )

        report_only_store = JsonTaskStore(Path(self.tmp.name) / "legacy-report-only-tasks")
        app_module.store = report_only_store
        app_module.runner = TaskRunner(Path(self.tmp.name), store=report_only_store)
        app_module.desktop_runner = FakeDesktopRunner(report_only_store)
        retry_response = self.client.post(
            f"/tasks/{task_id}/sites/consumables/run",
            base_url="http://100.114.126.58:8080",
            data={"return_service": "ems"},
            follow_redirects=False,
        )
        self.assertEqual(retry_response.status_code, 302)

        legacy_payload = report_only_store.get(task_id)
        legacy_payload.pop("task_source", None)
        legacy_payload["site_statuses"]["consumables"].update(
            status="consumables_saved",
            detail="舊版重送後儲存成功",
        )
        legacy_payload["overall_status"] = "desktop_fast_completed"
        report_only_store.save_payload(task_id, legacy_payload)

        response = self.client.get(
            "/admin/ems?result=success",
            base_url="http://100.114.126.58:8080",
        )
        body = html.unescape(response.get_data(as_text=True))
        report = app_module.public_pc_report_for_task(task_id)

        self.assertTrue(app_module.public_pc_report_retry_task(report_only_store.get(task_id)))
        self.assertIsNotNone(report)
        self.assertEqual("success", app_module.public_pc_report_result(report))
        self.assertIn("舊版公務電腦重送完成測試", body)

    def test_task_detail_shows_chinese_statuses_without_raw_statuses(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "vehicle_mileage",
                "車輛里程",
                "local_pc_ready",
                "已建立本機電腦操作任務",
            ),
        )

        self.client.post(f"/tasks/{task_id}/confirm-case-closed")
        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("待確認", body)
        self.assertNotIn("local_pc_ready", body)
        self.assertNotIn("https://ppe.tyfd.gov.tw", body)
        task_section = body.split('aria-label="任務內容"', 1)[1]
        task_section_head = task_section.split('<div class="task-grid">', 1)[0]
        self.assertLess(task_section_head.index("四站登打啟動"), task_section_head.index("<h2>任務內容</h2>"))
        self.assertIn("任務內容", task_section_head)
        self.assertNotIn("待確認", task_section_head)

    def test_task_detail_header_hides_meta_and_keeps_run_button_in_content(self):
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                vehicle="\u65b0\u576192",
                driver="\u5305\u83ef\u5148",
                mileage="200",
                case_time="1633",
                case_date="2026-06-06",
                return_time="1700",
                return_date="2026-06-06",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        self.client.post(f"/tasks/{task_id}/confirm-case-closed")
        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))
        header = body.split('aria-label="任務內容"', 1)[0]

        self.assertIn('class="page-chrome page-head" data-page-accent="ems"', header)
        self.assertIn('<p class="page-chrome__eyebrow">任務明細</p>', header)
        self.assertIn("返回首頁", header)
        self.assertNotIn("回到上一頁", header)
        self.assertIn(".page-head { align-items: stretch; flex-direction: column; }", body)
        self.assertIn(".head-actions .button { width: 100%; }", body)
        self.assertNotIn("06/06 1633", header)
        self.assertNotIn("\u65b0\u576192 / \u5305\u83ef\u5148", header)
        self.assertNotIn("\u9001\u5230\u516c\u52d9\u96fb\u8166", header)
        self.assertLess(body.index("\u56db\u7ad9\u767b\u6253\u555f\u52d5"), body.index("\u8fd4\u56de\u7de8\u8f2f"))

    def test_ems_task_detail_requires_case_closed_confirmation_before_showing_run_button(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        before_confirmation = html.unescape(self.client.get(f"/tasks/{task_id}").data.decode("utf-8"))

        self.assertIn("是否已結案?", before_confirmation)
        self.assertIn(f'action="/tasks/{task_id}/confirm-case-closed"', before_confirmation)
        self.assertNotIn("四站登打啟動", before_confirmation)

        response = self.client.post(f"/tasks/{task_id}/confirm-case-closed", follow_redirects=True)
        after_confirmation = html.unescape(response.data.decode("utf-8"))

        self.assertEqual(200, response.status_code)
        self.assertNotIn("是否已結案?", after_confirmation)
        self.assertIn("四站登打啟動", after_confirmation)
        self.assertTrue(self.store.get(task_id)["ems_case_closed_confirmed"])

    def test_ems_task_detail_labels_missing_consumables_and_disinfection_cases(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "consumables",
                "耗材",
                "consumables_failed",
                "耗材列表找不到符合案件的內容列",
            ),
        )
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "disinfection",
                "消毒",
                "disinfection_failed",
                "missing disinfection detail: query returned no data",
            ),
        )

        body = html.unescape(self.client.get(f"/tasks/{task_id}").data.decode("utf-8"))

        self.assertEqual(2, body.count("找不到案件"))
        self.assertNotIn('<span class="status failed">失敗</span>', body)

    def test_completed_task_hides_all_run_buttons_and_shows_four_site_completion(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        for site_key, name in (
            ("duty_work_log", "工作"),
            ("vehicle_mileage", "里程"),
            ("consumables", "耗材"),
            ("disinfection", "消毒"),
        ):
            self.store.update_site_result(
                task_id,
                app_module.SiteAutomationResult(
                    site_key,
                    name,
                    f"{site_key}_saved",
                    "saved",
                ),
            )

        body = html.unescape(
            self.client.get(f"/tasks/{task_id}").get_data(as_text=True)
        )

        self.assertIn("✓ 四站登打完成", body)
        self.assertNotIn("四站登打啟動", body)
        self.assertNotIn("單獨登打", body)
        self.assertIn("返回編輯", body)

    def test_completed_task_edit_shows_changed_field_site_and_vehicle(self):
        original = self.valid_task_data(
            two_vehicle="1",
            vehicle_2="新坡93",
            driver_2="陳小華",
            mileage_2="200",
            return_time_2="1120",
            patient_summary_2="女一名",
            consumables_2="手套=2",
        )
        create_response = self.client.post("/tasks", data=original)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        payload = self.store.get(task_id)
        for site_key in (
            "duty_work_log",
            "vehicle_mileage",
            "consumables",
            "disinfection",
        ):
            payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"
        payload["site_statuses"]["vehicle_mileage"]["vehicle_results"] = {
            "新坡91": {
                "status": "vehicle_mileage_saved",
                "detail": "first",
            },
            "新坡93": {
                "status": "vehicle_mileage_saved",
                "detail": "second",
            },
        }
        payload["overall_status"] = "desktop_fast_completed"
        self.store.save_payload(task_id, payload)

        update_data = {
            **original,
            "mileage_2": "220",
        }
        self.client.post(f"/tasks/{task_id}/edit", data=update_data)
        self.client.post(f"/tasks/{task_id}/confirm-case-closed")
        body = html.unescape(
            self.client.get(f"/tasks/{task_id}").get_data(as_text=True)
        )

        self.assertIn("任務資料已修改", body)
        self.assertIn("已修改：第 2 車里程", body)
        self.assertIn("需重新登打：里程（只重登第 2 車）", body)
        self.assertIn("更新里程", body)
        self.assertIn("四站登打啟動", body)
        self.assertEqual(
            list(
                self.store.get(task_id)["site_statuses"]["vehicle_mileage"][
                    "vehicle_results"
                ]
            ),
            ["新坡91"],
        )

    def test_recent_task_uses_four_site_completion_label(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        payload = self.store.get(task_id)
        for site_key in (
            "duty_work_log",
            "vehicle_mileage",
            "consumables",
            "disinfection",
        ):
            payload["site_statuses"][site_key]["status"] = f"{site_key}_saved"
        payload["overall_status"] = "site_run_completed"
        self.store.save_payload(task_id, payload)

        body = html.unescape(self.client.get("/app").get_data(as_text=True))

        self.assertIn("四站登打完成", body)
        self.assertIn('class="status complete">完成</span>', body)

    def test_task_edit_updates_existing_task_and_marks_changed_saved_sites_for_update(self):
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(mileage="100", consumables="\u53e3\u7f69=2"),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_saved", "done"),
        )

        edit_response = self.client.get(f"/tasks/{task_id}/edit")
        edit_body = html.unescape(edit_response.data.decode("utf-8"))
        self.assertEqual(edit_response.status_code, 200)
        self.assertIn("SinpoSmart - 救護Worker - 編輯狀態", edit_body)
        self.assertNotIn("勤務案件", edit_body)
        self.assertNotIn("救護各項設定", edit_body)
        self.assertNotIn("SinpoSmart - 救災救護Worker 後台", edit_body)
        self.assertIn("儲存修改", edit_body)
        self.assertIn('class="form-actions"', edit_body)
        self.assertIn('value="100"', edit_body)

        update_response = self.client.post(
            f"/tasks/{task_id}/edit",
            data={
                "case_id": "case-test-001",
                "vehicle": "\u65b0\u576191",
                "driver": "\u5305\u83ef\u5148",
                "mileage": "200",
                "case_date": "2026-06-07",
                "case_time": "1024",
                "return_date": "2026-06-07",
                "return_time": "1119",
                "case_address": "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def1\u865f",
                "case_reason": "\u8eca\u798d",
                "patient_summary": "\u5973\u4e00\u540d",
                "consumables": "\u624b\u5957=1",
            },
            follow_redirects=False,
        )
        payload = self.store.get(task_id)

        self.assertEqual(update_response.status_code, 302)
        self.assertEqual(update_response.headers["Location"], f"/tasks/{task_id}")
        self.assertEqual(payload["task"]["vehicle"], "\u65b0\u576191")
        self.assertEqual(payload["task"]["mileage"], "200")
        self.assertEqual(payload["task"]["consumables"], {"\u624b\u5957": 1})
        self.assertEqual(payload["overall_status"], "task_updated_needs_site_update")
        self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_needs_update")
        self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["update_context"]["previous_task"]["mileage"], "100")
        self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["update_context"]["current_task"]["mileage"], "200")
        self.assertEqual(payload["site_statuses"]["duty_work_log"]["status"], "not_started")
        self.assertEqual(payload["site_statuses"]["consumables"]["status"], "not_started")

    def test_edit_task_does_not_compare_mileage_with_later_entry(self):
        older_data = self.valid_task_data(
            case_id="mileage-edit-older",
            case_date="2026-08-20",
            case_time="0900",
            return_date="2026-08-20",
            return_time="0930",
            mileage="12000",
        )
        older_response = self.client.post("/tasks", data=older_data, follow_redirects=False)
        older_task_id = older_response.headers["Location"].rstrip("/").split("/")[-1]
        newer_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                case_id="mileage-edit-newer",
                case_date="2026-08-21",
                case_time="0900",
                return_date="2026-08-21",
                return_time="0930",
                mileage="12100",
            ),
            follow_redirects=False,
        )

        self.assertEqual(302, older_response.status_code)
        self.assertEqual(302, newer_response.status_code)
        edit_body = html.unescape(self.client.get(f"/tasks/{older_task_id}/edit").data.decode("utf-8"))
        update_response = self.client.post(
            f"/tasks/{older_task_id}/edit",
            data=older_data,
            follow_redirects=False,
        )

        self.assertIn("const enforceMileageChangeLimit = false;", edit_body)
        self.assertEqual(302, update_response.status_code)
        self.assertEqual(f"/tasks/{older_task_id}", update_response.headers["Location"])

    def test_queued_task_can_be_opened_and_submitted_for_edit(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data(mileage="100"))
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        remote_base = "http://100.114.126.58:8080"

        detail = html.unescape(self.client.get(f"/tasks/{task_id}", base_url=remote_base).data.decode("utf-8"))
        edit_get = self.client.get(f"/tasks/{task_id}/edit", base_url=remote_base)
        edit_post = self.client.post(
            f"/tasks/{task_id}/edit",
            base_url=remote_base,
            data=self.valid_task_data(mileage="999"),
            follow_redirects=False,
        )

        payload = self.store.get(task_id)
        self.assertIn(f'href="/tasks/{task_id}/edit"', detail)
        self.assertEqual(edit_get.status_code, 200)
        self.assertEqual(edit_post.status_code, 302)
        self.assertEqual(payload["task"]["mileage"], "999")
        self.assertEqual(payload["worker_queue"]["status"], "queued")

    def test_claimed_task_cannot_be_opened_or_submitted_for_edit(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data(mileage="100"))
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        self.assertIsNotNone(self.store.claim_next_for_worker("test-worker"))

        detail = html.unescape(self.client.get(f"/tasks/{task_id}").data.decode("utf-8"))
        edit_get = self.client.get(f"/tasks/{task_id}/edit")
        edit_post = self.client.post(
            f"/tasks/{task_id}/edit",
            data=self.valid_task_data(mileage="999"),
            follow_redirects=False,
        )

        self.assertNotIn(f'href="/tasks/{task_id}/edit"', detail)
        self.assertEqual(edit_get.status_code, 409)
        self.assertEqual(edit_post.status_code, 409)
        self.assertIn("正在執行", edit_get.data.decode("utf-8"))
        self.assertEqual(self.store.get(task_id)["task"]["mileage"], "100")

    def test_task_edit_consumables_only_preserves_other_completed_sites(self):
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(mileage="100", consumables="\u53e3\u7f69=2"),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "\u8eca\u8f1b\u91cc\u7a0b", "vehicle_mileage_saved", "done"),
        )
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("consumables", "\u4e00\u7ad9\u901a\u8017\u6750", "consumables_saved", "done"),
        )

        update_response = self.client.post(
            f"/tasks/{task_id}/edit",
            data=self.valid_task_data(mileage="100", consumables="\u624b\u5957=1"),
            follow_redirects=False,
        )
        payload = self.store.get(task_id)
        detail_response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(detail_response.data.decode("utf-8"))

        self.assertEqual(update_response.status_code, 302)
        self.assertEqual(payload["task"]["consumables"], {"\u624b\u5957": 1})
        self.assertEqual(payload["overall_status"], "task_updated_needs_site_update")
        self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
        self.assertEqual(payload["site_statuses"]["consumables"]["status"], "consumables_needs_update")
        self.assertEqual(payload["pending_edit_impact"]["changed_labels"], ["耗材"])
        self.assertEqual(
            list(payload["pending_edit_impact"]["affected_sites"]),
            ["consumables"],
        )
        self.assertIn("\u66f4\u65b0\u8017\u6750", body)
        self.assertIn("\u9700\u66f4\u65b0", body)

    def test_task_edit_driver_marks_work_and_mileage_for_update(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("duty_work_log", "\u5de5\u4f5c", "duty_work_log_saved", "done"),
        )
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "\u8eca\u8f1b\u91cc\u7a0b", "vehicle_mileage_saved", "done"),
        )

        update_response = self.client.post(
            f"/tasks/{task_id}/edit",
            data=self.valid_task_data(driver="\u5305\u83ef\u5148"),
            follow_redirects=False,
        )
        payload = self.store.get(task_id)
        detail_response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(detail_response.data.decode("utf-8"))

        self.assertEqual(update_response.status_code, 302)
        self.assertEqual(payload["site_statuses"]["duty_work_log"]["status"], "duty_work_log_needs_update")
        self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_needs_update")
        self.assertIn("\u66f4\u65b0\u5de5\u4f5c", body)
        self.assertIn("\u66f4\u65b0\u91cc\u7a0b", body)

    def test_task_edit_shared_case_time_and_address_marks_every_dependent_site(self):
        original = self.valid_task_data(
            two_vehicle="1",
            vehicle_2="新坡92",
            driver_2="陳小明",
            return_time_2="1210",
            mileage_2="200",
            patient_summary_2="女一名",
            consumables_2="手套=2",
            case_date="2026-06-07",
            case_time="1024",
            case_address="桃園市觀音區舊址",
        )
        create_response = self.client.post("/tasks", data=original)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        for site_key, site_name in (
            ("duty_work_log", "工作"),
            ("vehicle_mileage", "車輛里程"),
            ("consumables", "耗材"),
            ("disinfection", "消毒"),
        ):
            self.store.update_site_result(
                task_id,
                app_module.SiteAutomationResult(site_key, site_name, f"{site_key}_saved", "done"),
            )

        update_response = self.client.post(
            f"/tasks/{task_id}/edit",
            data={
                **original,
                "case_date": "2026-06-08",
                "case_time": "1030",
                "case_address": "桃園市觀音區新址",
                "return_date": "2026-06-08",
                "return_date_2": "2026-06-08",
            },
            follow_redirects=False,
        )
        payload = self.store.get(task_id)

        self.assertEqual(update_response.status_code, 302)
        for site_key in ("duty_work_log", "vehicle_mileage", "consumables", "disinfection"):
            site = payload["site_statuses"][site_key]
            self.assertEqual(site["status"], f"{site_key}_needs_update")
            self.assertEqual(site["update_context"]["previous_task"]["case_time"], "1024")
            self.assertEqual(site["update_context"]["current_task"]["case_time"], "1030")

    def test_two_vehicle_validation_requires_second_vehicle_fields(self):
        task_request = app_module.request_from_form(
            self.valid_task_data(
                two_vehicle="1",
                vehicle_2="",
                driver_2="",
                return_time_2="",
                mileage_2="",
                patient_summary_2="",
                consumables_2="",
            )
        )

        errors = app_module.validate_task_form(task_request)

        self.assertTrue(any("2\u8eca" in error for error in errors))

    def test_fuel_record_validation_requires_time_and_decimal_numbers(self):
        task_request = app_module.request_from_form(
            self.valid_task_data(
                fuel_record="1",
                fuel_date="20260607",
                fuel_time="2460",
                fuel_quantity="abc",
                fuel_unit_price="30.3",
            )
        )

        errors = app_module.validate_task_form(task_request)

        self.assertIn("1\u8eca\u52a0\u6cb9\u6642\u9593\u683c\u5f0f\u9700\u70ba HHmm", errors)
        self.assertIn("1\u8eca\u6cb9\u91cf\u9700\u70ba\u6578\u5b57", errors)

    def test_two_vehicle_validation_rejects_duplicate_vehicle(self):
        task_request = app_module.request_from_form(
            self.valid_task_data(
                two_vehicle="1",
                vehicle="\u65b0\u576191",
                vehicle_2="\u65b0\u576191",
                driver_2="\u738b\u6631\u52db",
                patient_summary_2="\u5973\u4e00\u540d",
                mileage_2="200",
                consumables_2="\u53e3\u7f69=2",
            )
        )

        errors = app_module.validate_task_form(task_request)

        self.assertIn("1\u8eca\u82072\u8eca\u4e0d\u53ef\u9078\u64c7\u540c\u4e00\u53f0\u6551\u8b77\u8eca", errors)

    def test_new_task_page_shows_fuel_record_controls_under_each_mileage(self):
        selected_case = AmbulanceReturnRequest(
            task_id="case-two-fuel",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            case_id="case-two-fuel",
            case_date="2026/06/07",
            case_time="1024",
            return_date="2026/06/07",
            return_time="1119",
            case_address="\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def1\u865f",
            vehicle="\u65b0\u576191",
            driver="\u66fe\u5f65\u7db8",
            mileage="12345",
            personnel=["\u66fe\u5f65\u7db8", "\u738b\u6631\u52db", "\u9673\u5c0f\u660e", "\u6797\u5fd7\u5049"],
            personnel_accounts=["tyfd01510", "tyfd01111", "tyfd02222", "tyfd03333"],
        )
        with app_module.app.test_request_context("/app"):
            body = html.unescape(
                app_module.render_task_form_from_request(
                    selected_case,
                    form_action="/tasks",
                    submit_label="建立任務",
                    cancel_url="",
                    recent_tasks=[],
                    case_lookup={"cases": [], "case_count": 0, "debug_artifacts": []},
                    form_errors=[],
                    baseline_consumables_loaded=True,
                    two_vehicle_available=True,
                )
            )

        self.assertIn('name="fuel_record"', body)
        self.assertIn('name="fuel_record_2"', body)
        self.assertLess(body.index('name="mileage"'), body.index('name="fuel_record"'))
        self.assertLess(body.index('name="mileage_2"'), body.index('name="fuel_record_2"'))
        self.assertIn('class="mileage-row"', body)
        self.assertIn(".mileage-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);", body)
        self.assertIn(".mileage-hint-panel { min-width: 0; }", body)
        self.assertIn(".mileage-hint-spacer { min-height: calc(1.45em + 6px); visibility: hidden; }", body)
        self.assertIn(".mileage-hint { min-height: 46px; margin: 0;", body)
        self.assertIn(".mileage-hint { margin-top: 0; }", body)
        self.assertIn(".fuel-required.is-pending .field-error-mark", body)
        self.assertIn("#task-form input,", body)
        self.assertIn("#task-form select { min-height: 46px;", body)
        self.assertIn('data-numeric="integer"', body)
        self.assertIn('data-numeric="decimal"', body)
        self.assertIn("data-time-hhmm", body)
        self.assertIn('id="client-form-errors"', body)
        self.assertIn("function updateVehicleOptionAvailability()", body)
        self.assertIn("option.disabled = duplicated;", body)
        self.assertIn("option.hidden = duplicated;", body)
        self.assertIn("function normalizeTimeInput(input)", body)
        self.assertIn("function updateFuelRequiredHints()", body)
        self.assertIn("function fuelRecordErrors(prefix, label)", body)
        self.assertIn("function loadedConsumableLabels2()", body)
        self.assertIn('if (baselineConsumablesLoaded) labels.push("基礎三項");', body)
        self.assertIn("const labels = loadedConsumableLabels2();", body)
        self.assertNotIn("alert(`${label}", body)
        self.assertIn('name="fuel_date" inputmode="numeric" autocomplete="off" placeholder="YYYY/MM/DD" maxlength="10" value="2026/06/07"', body)
        self.assertIn('name="fuel_date_2" inputmode="numeric" autocomplete="off" placeholder="YYYY/MM/DD" maxlength="10" value="2026/06/07"', body)
        self.assertIn('value="\u8d85\u7d1a\u67f4\u6cb9"', body)

    def test_last_vehicle_mileages_scans_beyond_recent_ten_tasks(self):
        for index in range(65):
            request = AmbulanceReturnRequest(
                task_id=f"mileage-history-{index:02d}",
                created_at=datetime.now(),
                raw_text="",
                vehicle="91A1" if index == 0 else f"91B{index:02d}",
                mileage=str(10000 + index),
            )
            app_module.store.create(request)

        mileages = app_module.last_vehicle_mileages()

        self.assertEqual(mileages["91A1"], "10000")

    def test_last_vehicle_mileage_ignores_single_site_retry_recency(self):
        older = AmbulanceReturnRequest(
            task_id="mileage-history-old",
            created_at=datetime(2026, 8, 13, 8, 0),
            raw_text="",
            vehicle="新坡91",
            mileage="10000",
            case_date="2026-08-13",
            case_time="0800",
        )
        newer = AmbulanceReturnRequest(
            task_id="mileage-history-new",
            created_at=datetime(2026, 8, 14, 8, 0),
            raw_text="",
            vehicle="新坡91",
            mileage="11000",
            case_date="2026-08-14",
            case_time="0800",
        )
        app_module.store.create(older)
        app_module.store.create(newer)
        app_module.store.update_site_result(
            older.task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_saved", "單站重登"),
        )

        self.assertEqual(app_module.last_vehicle_mileages()["新坡91"], "11000")

    def test_last_vehicle_mileage_uses_ppe_record_datetime_not_snapshot_fetch_datetime(self):
        request = AmbulanceReturnRequest(
            task_id="mileage-history-after-ppe-record",
            created_at=datetime(2026, 8, 22, 10, 0),
            raw_text="",
            vehicle="新坡91",
            mileage="20000",
            case_date="2026-08-22",
            case_time="1000",
        )
        app_module.store.create(request)
        nas_settings = {
            "ems_vehicles": [{"label": "新坡91", "ppe_name": "KEC-2608"}],
            "disaster_vehicles": [],
            "daily_vehicle_mileage_snapshot": {
                "vehicles": [
                    {
                        "vehicle_key": "KEC-2608",
                        "ppe_name": "KEC-2608",
                        "labels": ["新坡91"],
                        "status": "daily_vehicle_mileage_synced",
                        "mileage": "12345",
                        "record_end_date": "20260821",
                        "record_end_time": "1800",
                        "last_success_at": "2026-08-23T06:03:00",
                        "last_success_business_date": "2026-08-23",
                    }
                ]
            },
        }

        mileages = app_module.last_vehicle_mileages(nas_settings=nas_settings)

        self.assertEqual("20000", mileages["新坡91"])

    def test_worker_daily_vehicle_mileage_snapshot_requires_token_and_preserves_last_success(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        success = {
            "business_date": "2026-08-22",
            "attempted_at": "2026-08-22T06:03:00",
            "vehicles": [
                {
                    "vehicle_key": "KEC-2608",
                    "ppe_name": "KEC-2608",
                    "labels": ["新坡91"],
                    "status": "daily_vehicle_mileage_synced",
                    "mileage": "12345",
                    "record_end_date": "20260821",
                    "record_end_time": "1800",
                }
            ],
        }
        failure = {
            "business_date": "2026-08-22",
            "attempted_at": "2026-08-22T06:30:00",
            "vehicles": [
                {
                    "vehicle_key": "KEC-2608",
                    "ppe_name": "KEC-2608",
                    "status": "daily_vehicle_mileage_failed",
                    "detail": "PPE 登入失敗",
                }
            ],
        }

        forbidden = self.client.post("/worker/daily-vehicle-mileage", json=success)
        saved = self.client.post("/worker/daily-vehicle-mileage", headers=headers, json=success)
        failed = self.client.post("/worker/daily-vehicle-mileage", headers=headers, json=failure)
        snapshot = self.client.get("/worker/daily-vehicle-mileage", headers=headers)

        self.assertEqual(403, forbidden.status_code)
        self.assertEqual(200, saved.status_code)
        self.assertEqual(200, failed.status_code)
        self.assertEqual(200, snapshot.status_code)
        record = snapshot.get_json()["snapshot"]["vehicles"][0]
        self.assertEqual("daily_vehicle_mileage_failed", record["status"])
        self.assertEqual("12345", record["mileage"])
        self.assertEqual("2026-08-22T06:03:00", record["last_success_at"])

    def test_manual_vehicle_mileage_query_uses_worker_command_and_refreshes_card_status(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        nas_headers = {"Host": "100.114.126.58:8080"}
        self.client.post(
            "/admin/vehicles",
            data={"label": "手動查詢救護車", "ppe_name": "MANUAL-001"},
            headers=nas_headers,
        )

        queued = self.client.post(
            "/admin/vehicles/mileage-query",
            data={"ppe_name": "MANUAL-001"},
            headers=nas_headers,
            follow_redirects=False,
        )
        self.assertEqual(303, queued.status_code)
        self.assertEqual("/admin/vehicles", queued.headers["Location"])
        queued_page = self.client.get(queued.headers["Location"], headers=nas_headers)
        forbidden = self.client.get("/worker/manual-refreshes")
        commands_response = self.client.get("/worker/manual-refreshes", headers=worker_headers)
        command = commands_response.get_json()["commands"][0]
        self.client.post(
            "/worker/daily-vehicle-mileage",
            headers=worker_headers,
            json={
                "business_date": "2026-08-22",
                "attempted_at": "2026-08-22T10:03:00",
                "vehicles": [
                    {
                        "vehicle_key": "MANUAL-001",
                        "ppe_name": "MANUAL-001",
                        "labels": ["手動查詢救護車"],
                        "status": "daily_vehicle_mileage_synced",
                        "mileage": "56789",
                        "record_end_date": "20260821",
                        "record_end_time": "1800",
                    }
                ],
            },
        )
        acknowledged = self.client.post(
            f"/worker/manual-refreshes/{command['request_id']}/status",
            headers=worker_headers,
            json={"status": "completed", "detail": "已讀取 PPE 最新里程。"},
        )
        page = self.client.get("/admin/vehicles", headers=nas_headers)
        queued_body = html.unescape(queued_page.data.decode("utf-8"))
        page_body = html.unescape(page.data.decode("utf-8"))

        self.assertEqual(403, forbidden.status_code)
        self.assertEqual(200, commands_response.status_code)
        self.assertEqual("vehicle_mileage", command["kind"])
        self.assertEqual("MANUAL-001", command["vehicle_key"])
        self.assertEqual(200, acknowledged.status_code)
        self.assertIn('action="/admin/vehicles/mileage-query"', queued_body)
        self.assertIn("查詢中…", queued_body)
        self.assertIn('http-equiv="refresh" content="4;url=/admin/vehicles"', queued_body)
        self.assertIn("56789 km", page_body)
        self.assertIn("PPE 同步", page_body)
        self.assertIn("查詢最新里程", page_body)

    def test_manual_disaster_vehicle_mileage_query_queues_rescue_target(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        nas_headers = {"Host": "100.114.126.58:8080"}
        self.client.post(
            "/admin/disaster-vehicles",
            data={
                "label": "手動查詢救災車",
                "ppe_name": "RESCUE-MANUAL-001",
                "recorder_code": "RESCUE-CAM-001",
            },
            headers=nas_headers,
        )

        queued = self.client.post(
            "/admin/disaster-vehicles/mileage-query",
            data={"ppe_name": "RESCUE-MANUAL-001"},
            headers=nas_headers,
            follow_redirects=False,
        )
        self.assertEqual(303, queued.status_code)
        self.assertEqual("/admin/disaster-vehicles", queued.headers["Location"])
        queued_page = self.client.get(queued.headers["Location"], headers=nas_headers)
        commands_response = self.client.get("/worker/manual-refreshes", headers=worker_headers)
        command = commands_response.get_json()["commands"][0]
        queued_body = html.unescape(queued_page.data.decode("utf-8"))

        self.assertEqual("vehicle_mileage", command["kind"])
        self.assertEqual("RESCUE-MANUAL-001", command["vehicle_key"])
        self.assertIn('action="/admin/disaster-vehicles/mileage-query"', queued_body)
        self.assertIn("查詢中…", queued_body)
        self.assertIn('http-equiv="refresh" content="4;url=/admin/disaster-vehicles"', queued_body)

    def test_manual_civilpower_roster_query_shows_worker_error_next_to_button(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        nas_headers = {"Host": "100.114.126.58:8080"}

        queued = self.client.post(
            "/admin/civilpower-roster/query",
            headers=nas_headers,
            follow_redirects=False,
        )
        self.assertEqual(303, queued.status_code)
        self.assertEqual("/admin/vehicles", queued.headers["Location"])
        queued_page = self.client.get(queued.headers["Location"], headers=nas_headers)
        commands_response = self.client.get("/worker/manual-refreshes", headers=worker_headers)
        command = commands_response.get_json()["commands"][0]
        acknowledged = self.client.post(
            f"/worker/manual-refreshes/{command['request_id']}/status",
            headers=worker_headers,
            json={"status": "failed", "detail": "民力系統登入失敗"},
        )
        page = self.client.get("/admin/vehicles", headers=nas_headers)
        queued_body = html.unescape(queued_page.data.decode("utf-8"))
        page_body = html.unescape(page.data.decode("utf-8"))

        self.assertEqual("civilpower_roster", command["kind"])
        self.assertEqual(200, acknowledged.status_code)
        self.assertIn('formaction="/admin/civilpower-roster/query"', queued_body)
        self.assertIn("event.submitter", queued_body)
        self.assertIn("button.textContent='查詢中…';", queued_body)
        self.assertIn("查詢中…", queued_body)
        self.assertIn('http-equiv="refresh" content="4;url=/admin/vehicles"', queued_body)
        self.assertIn("查詢義消名冊", page_body)
        self.assertIn("民力系統登入失敗", page_body)

    def test_ems_and_disaster_vehicle_settings_show_shared_daily_mileage_and_forms_use_it(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        nas_headers = {"Host": "100.114.126.58:8080"}
        self.client.post(
            "/admin/vehicles",
            data={"label": "同步救護車", "ppe_name": "SYNC-001"},
            headers=nas_headers,
        )
        self.client.post(
            "/admin/disaster-vehicles",
            data={"label": "同步救災車", "ppe_name": "SYNC-001", "recorder_code": "CAM-001"},
            headers=nas_headers,
        )
        self.client.post(
            "/worker/daily-vehicle-mileage",
            headers=worker_headers,
            json={
                "business_date": "2026-08-22",
                "attempted_at": "2026-08-22T06:03:00",
                "vehicles": [
                    {
                        "vehicle_key": "SYNC-001",
                        "ppe_name": "SYNC-001",
                        "labels": ["同步救護車", "同步救災車"],
                        "status": "daily_vehicle_mileage_synced",
                        "mileage": "12345",
                        "record_end_date": "20260821",
                        "record_end_time": "1800",
                    }
                ],
            },
        )

        ems_body = html.unescape(self.client.get("/admin/vehicles", headers=nas_headers).data.decode("utf-8"))
        disaster_body = html.unescape(
            self.client.get("/admin/disaster-vehicles", headers=nas_headers).data.decode("utf-8")
        )
        mileages = app_module.last_vehicle_mileages()

        for body in (ems_body, disaster_body):
            self.assertIn("最新里程", body)
            self.assertIn("12345", body)
            self.assertIn("PPE 同步", body)
        self.assertEqual("12345", mileages["同步救護車"])
        self.assertEqual("12345", mileages["同步救災車"])

    def test_task_edit_second_vehicle_fields_mark_saved_sites_for_update(self):
        previous_request = app_module.request_from_form(
            self.valid_task_data(
                two_vehicle="1",
                vehicle_2="\u65b0\u576192",
                driver_2="\u9673\u5c0f\u660e",
                return_time_2="1210",
                mileage_2="200",
                patient_summary_2="\u7121",
                consumables_2="\u624b\u5957=2",
            )
        )
        current_request = app_module.request_from_form(
            self.valid_task_data(
                two_vehicle="1",
                vehicle_2="\u65b0\u576193",
                driver_2="\u9673\u5c0f\u660e",
                return_time_2="1220",
                mileage_2="220",
                patient_summary_2="\u5973\u4e00\u540d",
                consumables_2="\u53e3\u7f69=2",
            )
        )

        changed = app_module.changed_sites_for_task_edit(previous_request.to_dict(), current_request.to_dict())

        self.assertEqual(
            changed,
            {"duty_work_log", "vehicle_mileage", "consumables", "disinfection"},
        )

    def test_task_edit_refusal_summary_marks_work_site_for_update(self):
        previous_request = app_module.request_from_form(
            self.valid_task_data(refusal_summary="無")
        )
        current_request = app_module.request_from_form(
            self.valid_task_data(refusal_summary="女1名拒送")
        )

        changed = app_module.changed_sites_for_task_edit(previous_request.to_dict(), current_request.to_dict())

        self.assertEqual(changed, {"duty_work_log"})

    def test_task_edit_second_vehicle_identity_marks_consumables_for_update(self):
        previous_request = app_module.request_from_form(
            self.valid_task_data(
                two_vehicle="1",
                vehicle_2="\u65b0\u576193",
                driver_2="\u9673\u5c0f\u660e",
                consumables_2="\u624b\u5957=2",
            )
        )
        current_request = app_module.request_from_form(
            self.valid_task_data(
                two_vehicle="1",
                vehicle_2="\u65b0\u576194",
                driver_2="\u9673\u5c0f\u660e",
                consumables_2="\u624b\u5957=2",
            )
        )

        changed = app_module.changed_sites_for_task_edit(previous_request.to_dict(), current_request.to_dict())

        self.assertIn("consumables", changed)

    def test_task_edit_enabling_second_vehicle_fuel_activates_fuel_update(self):
        original = self.valid_task_data(
            two_vehicle="1",
            vehicle_2="新坡93",
            driver_2="陳小明",
            mileage_2="220",
            return_date_2="2026-06-07",
            return_time_2="1120",
            patient_summary_2="女一名",
            consumables_2="手套=2",
        )
        create_response = self.client.post("/tasks", data=original)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        update_response = self.client.post(
            f"/tasks/{task_id}/edit",
            data={
                **original,
                "fuel_record_2": "1",
                "fuel_date_2": "2026-06-07",
                "fuel_time_2": "1125",
                "fuel_quantity_2": "40",
                "fuel_unit_price_2": "30",
            },
            follow_redirects=False,
        )
        payload = self.store.get(task_id)

        self.assertEqual(update_response.status_code, 302)
        self.assertEqual(payload["site_statuses"]["fuel_record"]["status"], "fuel_record_needs_update")
        self.assertTrue(payload["site_statuses"]["fuel_record"]["update_context"]["current_task"]["vehicle_entries"][1]["fuel_record"]["enabled"])

    def test_task_edit_disabling_saved_fuel_keeps_manual_cleanup_visible(self):
        os.environ["WORKER_TOKEN"] = "0123456789abcdef0123456789abcdef"
        original = self.valid_task_data(
            fuel_record="1",
            fuel_date="2026-06-07",
            fuel_time="1125",
            fuel_quantity="40",
            fuel_unit_price="30",
        )
        create_response = self.client.post("/tasks", data=original)
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("fuel_record", "加油", "fuel_record_saved", "done"),
        )

        update_response = self.client.post(
            f"/tasks/{task_id}/edit",
            data=self.valid_task_data(),
            follow_redirects=False,
        )
        payload = self.store.get(task_id)
        body = html.unescape(self.client.get(f"/tasks/{task_id}").data.decode("utf-8"))

        self.assertEqual(update_response.status_code, 302)
        self.assertEqual(payload["site_statuses"]["fuel_record"]["status"], "fuel_record_waiting_confirmation")
        self.assertIn("人工刪除", payload["site_statuses"]["fuel_record"]["detail"])
        self.assertIn("里程+加油", body)
        self.assertIn(f"/tasks/{task_id}/sites/fuel_record/complete", body)

        complete_response = self.client.post(
            f"/tasks/{task_id}/sites/fuel_record/complete",
            data={
                "confirmation_token": app_module.site_manual_complete_token(
                    task_id,
                    "fuel_record",
                )
            },
            follow_redirects=False,
        )

        self.assertEqual(complete_response.status_code, 302)
        self.assertEqual(
            self.store.get(task_id)["site_statuses"]["fuel_record"]["status"],
            "completed_by_user",
        )

    def test_task_detail_combines_mileage_and_fuel_record_when_enabled(self):
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                fuel_record="1",
                fuel_time="1720",
                fuel_quantity="42.122",
                fuel_unit_price="30.3",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))
        task_section = body[body.index('aria-label="\u4efb\u52d9\u5167\u5bb9"') : body.index('class="stage-panel"')]

        self.assertLess(task_section.index("<h3>\u5de5\u4f5c</h3>"), task_section.index("<h3>\u91cc\u7a0b+\u52a0\u6cb9</h3>"))
        self.assertIn("<h3>\u91cc\u7a0b+\u52a0\u6cb9</h3>", task_section)
        self.assertNotIn("<h3>\u52a0\u6cb9</h3>", task_section)
        self.assertLess(task_section.index("<h3>\u91cc\u7a0b+\u52a0\u6cb9</h3>"), task_section.index("<h3>\u8017\u6750</h3>"))
        self.assertLess(task_section.index("<h3>\u8017\u6750</h3>"), task_section.index("<h3>\u6d88\u6bd2</h3>"))
        work = task_section[task_section.index("<h3>\u5de5\u4f5c</h3>") : task_section.index("<h3>\u91cc\u7a0b+\u52a0\u6cb9</h3>")]
        self.assertLess(work.index("\u5730\u5740"), work.index("\u4e8b\u7531"))
        self.assertLess(work.index("\u4e8b\u7531"), work.index("\u8eca\u8f1b"))
        self.assertLess(work.index("\u8eca\u8f1b"), work.index("\u53f8\u6a5f"))
        self.assertLess(work.index("\u53f8\u6a5f"), work.index("\u9001\u91ab\u4eba\u6578"))
        self.assertLess(work.index("\u9001\u91ab\u4eba\u6578"), work.index("\u62d2\u9001\u4eba\u6578"))
        mileage = task_section[task_section.index("<h3>\u91cc\u7a0b+\u52a0\u6cb9</h3>") : task_section.index("<h3>\u8017\u6750</h3>")]
        self.assertLess(mileage.index(">\u8eca\u8f1b</span>"), mileage.index(">\u51fa\u52d5</span>"))
        self.assertLess(mileage.index(">\u51fa\u52d5</span>"), mileage.index(">\u8fd4\u968a</span>"))
        self.assertLess(mileage.index(">\u8fd4\u968a</span>"), mileage.index(">\u91cc\u7a0b</span>"))
        self.assertLess(mileage.index(">\u91cc\u7a0b</span>"), mileage.index(">\u53f8\u6a5f</span>"))
        self.assertIn(">\u52a0\u6cb9\u6642\u9593</span>", mileage)
        self.assertIn(">\u6cb9\u54c1</span>", mileage)
        self.assertIn(">\u6cb9\u91cf</span>", mileage)
        self.assertIn(">\u55ae\u50f9</span>", mileage)
        self.assertIn("1720", mileage)
        self.assertIn("\u8d85\u7d1a\u67f4\u6cb9", mileage)
        self.assertIn("42.122", mileage)
        self.assertIn("30.3", mileage)

    def test_task_detail_combined_mileage_card_shows_fuel_failure(self):
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                fuel_record="1",
                fuel_time="1154",
                fuel_quantity="35.304",
                fuel_unit_price="30.3",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult("vehicle_mileage", "\u8eca\u8f1b\u91cc\u7a0b", "vehicle_mileage_saved", "saved"),
        )
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "fuel_record",
                "\u767b\u6253\u52a0\u6cb9\u7d00\u9304",
                "fuel_record_failed",
                "\u52a0\u6cb9\u7d00\u9304\u64cd\u4f5c\u5931\u6557\uff1aMessage: fuel card not found: BGV-2310",
            ),
        )

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))
        task_section = body[body.index('aria-label="\u4efb\u52d9\u5167\u5bb9"') : body.index('class="stage-panel"')]
        mileage = task_section[task_section.index("<h3>\u91cc\u7a0b+\u52a0\u6cb9</h3>") : task_section.index("<h3>\u8017\u6750</h3>")]

        self.assertIn('<article class="task-card failed">', task_section)
        self.assertIn(f'action="/tasks/{task_id}/sites/fuel_record/run"', mileage)

    def test_task_detail_lists_single_vehicle_consumables_one_per_row(self):
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                consumables="\u6843-\u53e3\u7f69(\u7247)=2,\u6843-9\u540b\u624b\u5957-L(\u96d9)=1",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))
        task_section = body[body.index('aria-label="\u4efb\u52d9\u5167\u5bb9"') : body.index('class="stage-panel"')]
        consumables = task_section[task_section.index("<h3>\u8017\u6750</h3>") : task_section.index("<h3>\u6d88\u6bd2</h3>")]

        self.assertIn('<span class="value">\u6843-\u53e3\u7f69(\u7247) x2</span>', consumables)
        self.assertIn('<span class="value">\u6843-9\u540b\u624b\u5957-L(\u96d9) x1</span>', consumables)
        self.assertNotIn("\u3001", consumables)

    def test_task_detail_uses_mileage_title_when_fuel_record_not_enabled(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))
        task_section = body[body.index('aria-label="\u4efb\u52d9\u5167\u5bb9"') : body.index('class="stage-panel"')]

        self.assertIn("<h3>\u91cc\u7a0b</h3>", task_section)
        self.assertNotIn("<h3>\u91cc\u7a0b+\u52a0\u6cb9</h3>", task_section)
        self.assertNotIn("<h3>\u52a0\u6cb9</h3>", task_section)
        self.assertNotIn("\u672a\u52fe\u9078", task_section)

    def test_task_detail_hides_fuel_timeline_when_fuel_record_not_enabled(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.update_site_result(
            task_id,
            app_module.SiteAutomationResult(
                "fuel_record",
                "\u767b\u6253\u52a0\u6cb9\u7d00\u9304",
                "fuel_record_failed",
                "\u52a0\u6cb9\u7d00\u9304\u64cd\u4f5c\u5931\u6557",
            ),
        )

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))
        timeline = body[body.index('aria-label="\u57f7\u884c\u7d00\u9304"') :]

        self.assertNotIn("\u52a0\u6cb9\uff1a", timeline)

    def test_task_detail_shows_second_vehicle_values(self):
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                two_vehicle="1",
                vehicle_2="\u65b0\u576192",
                driver_2="\u738b\u6631\u52db",
                return_date_2="2026-06-07",
                return_time_2="1125",
                mileage_2="23456",
                patient_summary_2="\u7121",
                consumables_2="\u6843-9\u540b\u624b\u5957-L(\u96d9)=1",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("\u65b0\u576192", body)
        self.assertIn("\u738b\u6631\u52db", body)
        self.assertIn("23456", body)
        self.assertIn("\u6843-9\u540b\u624b\u5957-L(\u96d9) x1", body)

    def test_task_detail_lists_four_site_stage_checks(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.get(f"/tasks/{task_id}")
        body = html.unescape(response.data.decode("utf-8"))

        self.assertIn("四站階段檢查", body)
        self.assertIn("登入勤務系統", body)
        self.assertIn("登入 PPE", body)
        self.assertIn("登入消毒系統", body)
        self.assertIn("登入一站通", body)
        stage_section = body[body.index('aria-label="四站階段檢查"') :]
        self.assertLess(stage_section.index("<h3>工作</h3>"), stage_section.index("<h3>里程</h3>"))
        self.assertLess(stage_section.index("<h3>里程</h3>"), stage_section.index("<h3>耗材</h3>"))
        self.assertNotIn("<h3>加油</h3>", stage_section)
        self.assertLess(stage_section.index("<h3>耗材</h3>"), stage_section.index("<h3>消毒</h3>"))
        self.assertIn("未執行", body)
        self.assertNotIn("未開始", body)
        self.assertNotIn("工作：未執行", body)
        self.assertNotIn("里程：未執行", body)
        self.assertNotIn("消毒：未執行", body)
        self.assertNotIn("耗材：未執行", body)

    def test_run_queues_task_for_worker_and_worker_updates_status(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        run_response = self.client.post(f"/tasks/{task_id}/run", follow_redirects=False)
        self.assertEqual(run_response.status_code, 302)
        queued = self.store.get(task_id)
        self.assertEqual(queued["overall_status"], "queued_for_worker")

        next_response = self.client.get("/worker/next-task?worker_id=test-worker", headers=worker_headers)
        self.assertEqual(next_response.status_code, 200)
        next_payload = next_response.get_json()
        self.assertEqual(next_payload["task"]["task_id"], task_id)

        status_response = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=worker_headers,
            json={
                "status": "duty_work_log_saved",
                "detail": "saved",
                "site_key": "duty_work_log",
                "site_name": "\u6d88\u9632\u52e4\u52d9\u5de5\u4f5c\u7d00\u9304",
            },
        )
        self.assertEqual(status_response.status_code, 200)
        updated = self.store.get(task_id)
        self.assertEqual(updated["overall_status"], "claimed_by_worker")
        self.assertEqual(updated["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")

    def test_worker_queue_retry_preserves_saved_sites_and_prepares_only_unfinished_sites(self):
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        payload = self.store.get(task_id)
        payload["site_statuses"]["vehicle_mileage"].update(
            status="vehicle_mileage_saved",
            detail="已完成的里程紀錄",
        )
        payload["site_statuses"]["duty_work_log"].update(
            status="duty_work_log_failed",
            detail="前次失敗",
        )
        payload["site_statuses"]["consumables"].update(
            status="consumables_waiting_confirmation",
            detail="請人工確認的現有狀態",
        )
        self.store.save_payload(task_id, payload)
        confirmed = self.store.mark_site_completed(task_id, "consumables")
        self.assertEqual(
            confirmed["site_statuses"]["consumables"]["status"],
            "completed_by_user",
        )

        app_module.queue_task_for_worker(task_id)

        queued = self.store.get(task_id)
        self.assertEqual(queued["worker_queue"]["status"], "queued")
        self.assertEqual(queued["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
        self.assertEqual(queued["site_statuses"]["vehicle_mileage"]["detail"], "已完成的里程紀錄")
        self.assertEqual(queued["site_statuses"]["duty_work_log"]["status"], "local_pc_ready")
        self.assertEqual(
            queued["site_statuses"]["consumables"]["status"],
            "completed_by_user",
        )

    def test_worker_site_status_can_update_overall_when_explicitly_requested(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.client.post(f"/tasks/{task_id}/run", follow_redirects=False)
        self.client.get("/worker/next-task?worker_id=test-worker", headers=worker_headers)

        status_response = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=worker_headers,
            json={
                "status": "duty_work_log_saved",
                "detail": "saved",
                "site_key": "duty_work_log",
                "site_name": "\u6d88\u9632\u52e4\u52d9\u5de5\u4f5c\u7d00\u9304",
                "overall_status": "desktop_fast_completed",
                "overall_detail": "五站登打完成。",
            },
        )
        self.assertEqual(status_response.status_code, 200)
        updated = self.store.get(task_id)
        self.assertEqual(updated["overall_status"], "site_run_completed")
        self.assertEqual(updated["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")
        self.assertFalse(app_module.task_completion_snapshot(updated)["all_complete"])

    def test_worker_status_rejects_explicit_wrong_claim_owner(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        claimed = self.store.claim_next_for_worker("worker-a")
        assert claimed is not None

        wrong_claim = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=headers,
            json={
                "status": "worker_running",
                "detail": "stale worker",
                "worker_id": "worker-a",
                "claim_id": "wrong-claim",
            },
        )
        wrong_worker = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=headers,
            json={
                "status": "worker_running",
                "detail": "stale worker",
                "worker_id": "worker-b",
                "claim_id": claimed["worker_queue"]["claim_id"],
            },
        )

        self.assertEqual(wrong_claim.status_code, 409)
        self.assertEqual(wrong_claim.get_json()["error"], "worker_claim_conflict")
        self.assertIn("不符", wrong_claim.get_json()["detail"])
        self.assertEqual(wrong_worker.status_code, 409)
        self.assertEqual(wrong_worker.get_json()["error"], "worker_claim_conflict")
        self.assertEqual(self.store.get(task_id)["overall_status"], "claimed_by_worker")

    def test_worker_can_claim_specific_task_idempotently_but_other_worker_is_fenced(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        url = f"/worker/tasks/{task_id}/claim"

        first = self.client.post(url, headers=headers, json={"worker_id": "worker-a"})
        second = self.client.post(url, headers=headers, json={"worker_id": "worker-a"})
        conflict = self.client.post(url, headers=headers, json={"worker_id": "worker-b"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        first_queue = first.get_json()["worker_queue"]
        self.assertEqual(first_queue["status"], "claimed")
        self.assertEqual(first_queue["claim_id"], second.get_json()["worker_queue"]["claim_id"])
        self.assertEqual(first.get_json()["task"]["task_id"], task_id)
        self.assertEqual(conflict.get_json()["error"], "worker_claim_conflict")

    def test_worker_status_requires_claim_id_after_task_is_reclaimed(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        first = self.store.claim_next_for_worker("worker-a")
        assert first is not None
        first["worker_queue"]["lease_expires_at"] = (
            datetime.now() - timedelta(seconds=1)
        ).isoformat(timespec="seconds")
        self.store.save_payload(task_id, first)
        reclaimed = self.store.claim_next_for_worker("worker-b")
        assert reclaimed is not None

        missing_claim = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=headers,
            json={"status": "worker_running", "detail": "legacy stale reply"},
        )
        accepted = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=headers,
            json={
                "status": "worker_running",
                "detail": "current worker",
                "claim_id": reclaimed["worker_queue"]["claim_id"],
            },
        )

        self.assertEqual(missing_claim.status_code, 409)
        self.assertEqual(missing_claim.get_json()["error"], "worker_claim_identity_required")
        self.assertIn("claim_id", missing_claim.get_json()["detail"])
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.store.get(task_id)["overall_status"], "worker_running")

    def test_worker_status_rejects_identityless_stale_reply_after_legacy_claim_is_reclaimed(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        first = self.store.claim_next_for_worker("worker-a")
        assert first is not None
        first["worker_queue"].pop("claim_attempt", None)
        first["worker_queue"]["lease_expires_at"] = (
            datetime.now() - timedelta(seconds=1)
        ).isoformat(timespec="seconds")
        self.store.save_payload(task_id, first)
        reclaimed = self.store.claim_next_for_worker("worker-b")
        assert reclaimed is not None

        stale = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=headers,
            json={"status": "worker_running", "detail": "legacy stale reply"},
        )

        self.assertEqual(reclaimed["worker_queue"]["claim_attempt"], "2")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["error"], "worker_claim_identity_required")
        current = self.store.get(task_id)
        self.assertEqual(current["worker_queue"]["worker_id"], "worker-b")
        self.assertEqual(current["overall_status"], "claimed_by_worker")

    def test_worker_status_rejects_expired_claim_without_renewing_it(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        claimed = self.store.claim_next_for_worker("worker-a")
        assert claimed is not None
        expired_at = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")
        claimed["worker_queue"]["lease_expires_at"] = expired_at
        self.store.save_payload(task_id, claimed)
        events_before = len(claimed["events"])

        stale = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=headers,
            json={
                "status_event_id": "expired-claim-event",
                "status": "worker_running",
                "detail": "late outbox delivery",
                "worker_id": "worker-a",
                "claim_id": claimed["worker_queue"]["claim_id"],
            },
        )

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["error"], "worker_claim_inactive")
        current = self.store.get(task_id)
        self.assertEqual(current["worker_queue"]["lease_expires_at"], expired_at)
        self.assertEqual(current["overall_status"], "claimed_by_worker")
        self.assertEqual(len(current["events"]), events_before)

    def test_identityless_stale_status_is_rejected_after_abort_and_requeue(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        queued_a = self.store.queue_for_worker(task_id)
        claimed_a = self.store.claim_task_for_worker(task_id, "worker-a")
        self.store.abort_running_task(
            task_id,
            expected_claim_id=claimed_a["worker_queue"]["claim_id"],
            expected_queue_id=queued_a["worker_queue"]["queue_id"],
        )
        self.store.queue_for_worker(task_id)
        claimed_b = self.store.claim_task_for_worker(task_id, "worker-b")

        stale = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=headers,
            json={"status": "worker_running", "detail": "legacy stale reply"},
        )

        self.assertEqual(claimed_b["worker_queue"]["claim_attempt"], "2")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["error"], "worker_claim_identity_required")
        current = self.store.get(task_id)
        self.assertEqual(current["worker_queue"]["worker_id"], "worker-b")
        self.assertEqual(current["worker_queue"]["claim_id"], claimed_b["worker_queue"]["claim_id"])
        self.assertEqual(current["overall_status"], "claimed_by_worker")

    def test_worker_status_event_id_is_idempotent(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        claimed = self.store.claim_next_for_worker("worker-a")
        assert claimed is not None
        claim_id = claimed["worker_queue"]["claim_id"]
        status_url = f"/worker/tasks/{task_id}/status"

        first = self.client.post(
            status_url,
            headers=headers,
            json={
                "status_event_id": "event-idempotent-1",
                "status": "duty_work_log_saved",
                "detail": "saved once",
                "site_key": "duty_work_log",
                "site_name": "\u6d88\u9632\u52e4\u52d9\u5de5\u4f5c\u7d00\u9304",
                "overall_status": "worker_running",
                "overall_detail": "first delivery",
                "worker_id": "worker-a",
                "claim_id": claim_id,
            },
        )
        self.assertEqual(first.status_code, 200)
        first_payload = self.store.get(task_id)
        first_file = self.store.path_for(task_id).read_bytes()
        first_event_count = len(first_payload["events"])
        first_attempt_count = len(first_payload["site_attempts"]["duty_work_log"])

        duplicate = self.client.post(
            status_url,
            headers=headers,
            json={
                "status_event_id": "event-idempotent-1",
                "status": "duty_work_log_failed",
                "detail": "must be ignored",
                "site_key": "duty_work_log",
                "site_name": "\u6d88\u9632\u52e4\u52d9\u5de5\u4f5c\u7d00\u9304",
                "overall_status": "worker_failed",
                "overall_detail": "must be ignored",
                "worker_id": "worker-a",
                "claim_id": claim_id,
            },
        )

        self.assertEqual(duplicate.status_code, 200)
        after = self.store.get(task_id)
        self.assertEqual(self.store.path_for(task_id).read_bytes(), first_file)
        self.assertEqual(after["overall_status"], "worker_running")
        self.assertEqual(after["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")
        self.assertEqual(len(after["events"]), first_event_count)
        self.assertEqual(len(after["site_attempts"]["duty_work_log"]), first_attempt_count)
        self.assertEqual(after["recent_status_event_ids"], ["event-idempotent-1"])
        self.assertTrue(duplicate.get_json()["duplicate"])

    def test_worker_status_duplicate_event_still_rejects_different_owner(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        claimed = self.store.claim_next_for_worker("worker-a")
        assert claimed is not None
        claim_id = claimed["worker_queue"]["claim_id"]
        status_url = f"/worker/tasks/{task_id}/status"
        accepted = self.client.post(
            status_url,
            headers=headers,
            json={
                "status_event_id": "event-owner-1",
                "status": "worker_running",
                "detail": "accepted",
                "worker_id": "worker-a",
                "claim_id": claim_id,
            },
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.store.get(task_id)["recent_status_event_ids"], ["event-owner-1"])
        accepted_file = self.store.path_for(task_id).read_bytes()

        wrong_owner = self.client.post(
            status_url,
            headers=headers,
            json={
                "status_event_id": "event-owner-1",
                "status": "worker_failed",
                "detail": "must not bypass owner validation",
                "worker_id": "worker-b",
                "claim_id": claim_id,
            },
        )

        self.assertEqual(wrong_owner.status_code, 409)
        self.assertEqual(wrong_owner.get_json()["error"], "worker_claim_conflict")
        self.assertEqual(self.store.path_for(task_id).read_bytes(), accepted_file)

    def test_worker_status_event_id_history_is_bounded(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        claimed = self.store.claim_next_for_worker("worker-a")
        assert claimed is not None
        claim_id = claimed["worker_queue"]["claim_id"]
        status_url = f"/worker/tasks/{task_id}/status"

        with mock.patch("ambulance_bot.task_store.RECENT_STATUS_EVENT_ID_LIMIT", 3):
            for index in range(5):
                response = self.client.post(
                    status_url,
                    headers=headers,
                    json={
                        "status_event_id": f"bounded-event-{index}",
                        "status": "worker_running",
                        "detail": f"heartbeat {index}",
                        "worker_id": "worker-a",
                        "claim_id": claim_id,
                    },
                )
                self.assertEqual(response.status_code, 200)

        event_ids = self.store.get(task_id)["recent_status_event_ids"]
        self.assertEqual(len(event_ids), 3)
        self.assertEqual(event_ids[0], "bounded-event-2")
        self.assertEqual(event_ids[-1], "bounded-event-4")

    def test_worker_status_rejects_new_event_after_claim_is_aborted(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        claimed = self.store.claim_next_for_worker("worker-a")
        assert claimed is not None
        claim_id = claimed["worker_queue"]["claim_id"]
        status_url = f"/worker/tasks/{task_id}/status"
        identity = {"worker_id": "worker-a", "claim_id": claim_id}
        accepted = self.client.post(
            status_url,
            headers=headers,
            json={
                **identity,
                "status_event_id": "event-before-abort",
                "status": "worker_running",
                "detail": "running",
            },
        )
        self.assertEqual(accepted.status_code, 200)
        self.store.abort_running_task(task_id, "operator aborted")
        aborted_file = self.store.path_for(task_id).read_bytes()

        stale_new_event = self.client.post(
            status_url,
            headers=headers,
            json={
                **identity,
                "status_event_id": "event-after-abort",
                "status": "worker_running",
                "detail": "must not revive task",
            },
        )
        safe_duplicate = self.client.post(
            status_url,
            headers=headers,
            json={
                **identity,
                "status_event_id": "event-before-abort",
                "status": "worker_running",
                "detail": "duplicate must not mutate",
            },
        )

        self.assertEqual(stale_new_event.status_code, 409)
        self.assertEqual(stale_new_event.get_json()["error"], "worker_claim_inactive")
        self.assertEqual(safe_duplicate.status_code, 200)
        self.assertTrue(safe_duplicate.get_json()["duplicate"])
        self.assertEqual(self.store.path_for(task_id).read_bytes(), aborted_file)

    def test_worker_vehicle_statuses_checkpoint_and_aggregate_all_expected_vehicles(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                two_vehicle="1",
                two_vehicle_available="1",
                vehicle="新坡91",
                vehicle_2="新坡92",
                driver_2="包華先",
                mileage_2="54321",
                return_date_2="2026-06-07",
                return_time_2="1129",
                patient_summary_2="男一名",
                consumables_2="桃-口罩(片)=2",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        self.assertIsNotNone(self.store.claim_next_for_worker("test-worker"))
        status_url = f"/worker/tasks/{task_id}/status"

        first = self.client.post(
            status_url,
            headers=headers,
            json={
                "site_key": "consumables",
                "site_name": "一站通耗材",
                "vehicle_key": "新坡91",
                "vehicle_label": "第一車 新坡91",
                "status": "consumables_saved",
                "detail": "first saved",
            },
        )
        first_payload = first.get_json()["payload"]
        self.assertEqual(first_payload["site_statuses"]["consumables"]["status"], "consumables_running")
        self.assertEqual(
            first_payload["site_statuses"]["consumables"]["vehicle_results"]["新坡91"]["status"],
            "consumables_saved",
        )
        self.assertEqual(
            first_payload["site_statuses"]["consumables"]["vehicle_results"]["新坡91"]["vehicle_label"],
            "第一車 新坡91",
        )

        second = self.client.post(
            status_url,
            headers=headers,
            json={
                "site_key": "consumables",
                "site_name": "一站通耗材",
                "vehicle_key": "新坡92",
                "status": "consumables_failed",
                "detail": "second failed",
            },
        )
        second_payload = second.get_json()["payload"]
        self.assertEqual(second_payload["site_statuses"]["consumables"]["status"], "consumables_failed")
        self.assertEqual(
            second_payload["site_statuses"]["consumables"]["vehicle_results"]["新坡91"]["status"],
            "consumables_saved",
        )

        retried = self.client.post(
            status_url,
            headers=headers,
            json={
                "site_key": "consumables",
                "site_name": "一站通耗材",
                "vehicle_key": "新坡92",
                "status": "consumables_saved",
                "detail": "second saved on retry",
            },
        ).get_json()["payload"]
        self.assertEqual(retried["site_statuses"]["consumables"]["status"], "consumables_saved")
        self.assertEqual(set(retried["site_statuses"]["consumables"]["vehicle_results"]), {"新坡91", "新坡92"})

    def test_worker_vehicle_parent_does_not_hide_waiting_vehicle_behind_saved_vehicle(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(
                two_vehicle="1",
                two_vehicle_available="1",
                vehicle="新坡91",
                vehicle_2="新坡92",
                driver_2="包華先",
                mileage_2="54321",
                return_date_2="2026-06-07",
                return_time_2="1129",
                patient_summary_2="男一名",
                consumables_2="桃-口罩(片)=2",
            ),
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        self.assertIsNotNone(self.store.claim_next_for_worker("test-worker"))
        status_url = f"/worker/tasks/{task_id}/status"
        self.client.post(
            status_url,
            headers=headers,
            json={
                "site_key": "consumables",
                "site_name": "一站通耗材",
                "vehicle_key": "新坡91",
                "status": "manual_captcha_required",
                "detail": "waiting",
            },
        )
        payload = self.client.post(
            status_url,
            headers=headers,
            json={
                "site_key": "consumables",
                "site_name": "一站通耗材",
                "vehicle_key": "新坡92",
                "status": "consumables_saved",
                "detail": "saved",
            },
        ).get_json()["payload"]

        self.assertEqual(payload["site_statuses"]["consumables"]["status"], "manual_captcha_required")

    def test_worker_site_status_accepts_failure_diagnostics(self):
        os.environ["WORKER_TOKEN"] = "test-token"
        worker_headers = {"X-Worker-Token": "test-token"}
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.store.queue_for_worker(task_id)
        self.assertIsNotNone(self.store.claim_next_for_worker("test-worker"))

        status_response = self.client.post(
            f"/worker/tasks/{task_id}/status",
            headers=worker_headers,
            json={
                "status": "consumables_failed",
                "detail": "SSO login failed",
                "site_key": "consumables",
                "site_name": "一站通耗材",
                "failure_stage": "登入一站通",
                "failure_reason": "測試指定原因",
                "next_action": "測試下一步",
                "exception_type": "RuntimeError",
            },
        )

        self.assertEqual(status_response.status_code, 200)
        site = self.store.get(task_id)["site_statuses"]["consumables"]
        self.assertEqual(site["failure_stage"], "登入一站通")
        self.assertEqual(site["failure_reason"], "測試指定原因")
        self.assertEqual(site["next_action"], "測試下一步")
        self.assertEqual(site["exception_type"], "RuntimeError")

    def test_localhost_run_uses_desktop_fast_mode_when_auto(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.post(f"/tasks/{task_id}/run", base_url="http://127.0.0.1:8080", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started, [task_id])
        self.assertEqual(self.store.get(task_id)["overall_status"], "desktop_fast_running")

    def test_remote_host_run_queues_for_worker_when_auto(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        response = self.client.post(f"/tasks/{task_id}/run", base_url="http://100.114.126.58:8080", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started, [])
        self.assertEqual(self.store.get(task_id)["overall_status"], "queued_for_worker")

    def test_remote_create_queues_for_worker_and_hides_entry_controls(self):
        os.environ["DESKTOP_FAST_MODE"] = "auto"
        create_response = self.client.post(
            "/tasks",
            data=self.valid_task_data(),
            base_url="http://100.114.126.58:8080",
            follow_redirects=False,
        )
        task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(app_module.desktop_runner.started, [])
        self.assertEqual(self.store.get(task_id)["overall_status"], "queued_for_worker")

        detail_response = self.client.get(f"/tasks/{task_id}", base_url="http://100.114.126.58:8080")
        body = html.unescape(detail_response.data.decode("utf-8"))
        self.assertNotIn("四站登打啟動", body)
        self.assertNotIn("五站登打啟動", body)
        self.assertNotIn("單獨登打", body)
        self.assertIn("返回編輯", body)

    def test_desktop_fast_mode_environment_overrides_host(self):
        os.environ["DESKTOP_FAST_MODE"] = "1"
        create_response = self.client.post("/tasks", data=self.valid_task_data())
        fast_task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]

        self.client.post(f"/tasks/{fast_task_id}/run", base_url="http://100.114.126.58:8080", follow_redirects=False)

        os.environ["DESKTOP_FAST_MODE"] = "0"
        create_response = self.client.post("/tasks", data=self.valid_task_data(case_id="case-test-002"))
        queued_task_id = create_response.headers["Location"].rstrip("/").split("/")[-1]
        self.client.post(f"/tasks/{queued_task_id}/run", base_url="http://127.0.0.1:8080", follow_redirects=False)

        self.assertEqual(app_module.desktop_runner.started, [fast_task_id])
        self.assertEqual(self.store.get(fast_task_id)["overall_status"], "desktop_fast_running")
        self.assertEqual(self.store.get(queued_task_id)["overall_status"], "queued_for_worker")


if __name__ == "__main__":
    unittest.main()
