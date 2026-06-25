import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace

import worker as worker_module
from ambulance_bot.manual_task_lock import set_manual_task_lock
from ambulance_bot.selenium_local import DutyCaseLookupResult


class WorkerTests(unittest.TestCase):
    def test_hash_cases_is_stable_for_same_content(self):
        left = [{"case_id": "1", "address": "A"}, {"case_id": "2", "address": "B"}]
        right = [{"address": "A", "case_id": "1"}, {"address": "B", "case_id": "2"}]

        self.assertEqual(worker_module.hash_cases(left), worker_module.hash_cases(right))

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

        self.assertEqual(calls, ["duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"])
        self.assertEqual(result.status, "disinfection_saved")
        self.assertEqual(statuses[-1][0], "desktop_fast_completed")
        self.assertEqual(statuses[-1][2], "")

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

        self.assertEqual(calls, ["duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"])
        self.assertEqual(result.status, "consumables_failed")
        self.assertEqual(statuses[-1][0], "desktop_fast_completed_with_errors")
        self.assertIn("1 站失敗", statuses[-1][1])
        self.assertIn("接續後續站別", statuses[-1][1])
        self.assertEqual(statuses[-1][2], "")

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
