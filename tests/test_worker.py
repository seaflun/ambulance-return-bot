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

    def test_auto_claim_run_all_sites_runs_four_sites_and_sets_final_status(self):
        original_fetch_payload = worker_module.fetch_task_payload
        original_run_task = worker_module.run_task
        original_run_vehicle = worker_module.run_vehicle_task
        original_run_disinfection = worker_module.run_disinfection_worker_task
        original_run_consumables = worker_module.run_consumables_worker_task
        original_post_status = worker_module.post_status
        calls: list[str] = []
        statuses: list[tuple[str, str]] = []
        try:
            worker_module.fetch_task_payload = lambda server_url, task_id: {"site_statuses": {}}
            worker_module.run_task = lambda *args, **kwargs: calls.append("duty_work_log") or SimpleNamespace(
                ok=True, status="duty_work_log_saved", detail="duty ok"
            )
            worker_module.run_vehicle_task = lambda *args, **kwargs: calls.append("vehicle_mileage") or SimpleNamespace(
                ok=True, status="vehicle_mileage_saved", detail="mileage ok"
            )
            worker_module.run_disinfection_worker_task = lambda *args, **kwargs: calls.append("disinfection") or SimpleNamespace(
                ok=True, status="disinfection_saved", detail="disinfection ok"
            )
            worker_module.run_consumables_worker_task = lambda *args, **kwargs: calls.append("consumables") or SimpleNamespace(
                ok=True, status="consumables_saved", detail="consumables ok"
            )
            worker_module.post_status = lambda server_url, task_id, status, detail, **kwargs: statuses.append(
                (status, kwargs.get("site_key", ""))
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
            worker_module.run_disinfection_worker_task = original_run_disinfection
            worker_module.run_consumables_worker_task = original_run_consumables
            worker_module.post_status = original_post_status

        self.assertEqual(calls, ["duty_work_log", "vehicle_mileage", "disinfection", "consumables"])
        self.assertEqual(result.status, "consumables_saved")
        self.assertEqual(statuses[-1], ("desktop_fast_completed", ""))

    def test_auto_claim_run_all_sites_stops_after_blocking_failure(self):
        original_fetch_payload = worker_module.fetch_task_payload
        original_run_task = worker_module.run_task
        original_run_vehicle = worker_module.run_vehicle_task
        original_run_disinfection = worker_module.run_disinfection_worker_task
        original_run_consumables = worker_module.run_consumables_worker_task
        original_post_status = worker_module.post_status
        calls: list[str] = []
        statuses: list[tuple[str, str]] = []
        try:
            worker_module.fetch_task_payload = lambda server_url, task_id: {"site_statuses": {}}
            worker_module.run_task = lambda *args, **kwargs: calls.append("duty_work_log") or SimpleNamespace(
                ok=True, status="duty_work_log_saved", detail="duty ok"
            )
            worker_module.run_vehicle_task = lambda *args, **kwargs: calls.append("vehicle_mileage") or SimpleNamespace(
                ok=True, status="vehicle_mileage_saved", detail="mileage ok"
            )
            worker_module.run_disinfection_worker_task = lambda *args, **kwargs: calls.append("disinfection") or SimpleNamespace(
                ok=False, status="disinfection_failed", detail="login failed"
            )
            worker_module.run_consumables_worker_task = lambda *args, **kwargs: calls.append("consumables") or SimpleNamespace(
                ok=True, status="consumables_saved", detail="consumables ok"
            )
            worker_module.post_status = lambda server_url, task_id, status, detail, **kwargs: statuses.append(
                (status, kwargs.get("site_key", ""))
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
            worker_module.run_disinfection_worker_task = original_run_disinfection
            worker_module.run_consumables_worker_task = original_run_consumables
            worker_module.post_status = original_post_status

        self.assertEqual(calls, ["duty_work_log", "vehicle_mileage", "disinfection"])
        self.assertEqual(result.status, "disinfection_failed")
        self.assertEqual(statuses[-1], ("desktop_fast_completed_with_errors", ""))


if __name__ == "__main__":
    unittest.main()
