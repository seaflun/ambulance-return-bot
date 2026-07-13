import tempfile
import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path

from ambulance_bot.adapters import SiteAutomationResult
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_store import (
    JsonTaskStore,
    SiteCompletionConflictError,
    WorkerClaimConflictError,
    worker_claim_lease_is_active,
)


class JsonTaskStoreTests(unittest.TestCase):
    def test_task_id_cannot_escape_tasks_directory_with_windows_backslashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JsonTaskStore(root / "tasks")
            cases_dir = root / "cases"
            cases_dir.mkdir()
            neighbor = cases_dir / "latest.json"
            neighbor.write_text('{"secret": true}', encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                store.get(r"..\cases\latest")
            with self.assertRaises(FileNotFoundError):
                store.delete(r"..\cases\latest")
            self.assertEqual(neighbor.read_text(encoding="utf-8"), '{"secret": true}')

    def test_create_and_update_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="task-1",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="91A1",
            )

            payload = store.create(request)
            self.assertEqual(payload["overall_status"], "created")
            self.assertEqual(payload["worker_queue"]["status"], "idle")
            self.assertEqual(
                list(payload["site_statuses"]),
                ["duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"],
            )
            self.assertTrue((Path(tmp) / "task-1.json").exists())

            store.update_site_result(
                "task-1",
                SiteAutomationResult("vehicle_mileage", "車輛里程", "prefill_ready", "ready"),
            )
            updated = store.get("task-1")
            self.assertEqual(updated["site_statuses"]["vehicle_mileage"]["status"], "prefill_ready")
            self.assertEqual(len(updated["site_attempts"]["vehicle_mileage"]), 1)
            self.assertEqual(updated["site_attempts"]["vehicle_mileage"][0]["status"], "prefill_ready")

            pending = store.get("task-1")
            pending["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_waiting_confirmation"
            store.save_payload("task-1", pending)
            store.mark_site_completed("task-1", "vehicle_mileage")
            completed = store.get("task-1")
            self.assertEqual(completed["site_statuses"]["vehicle_mileage"]["status"], "completed_by_user")
            self.assertEqual(completed["site_attempts"]["vehicle_mileage"][-1]["status"], "completed_by_user")

    def test_manual_site_completion_rejects_non_waiting_status_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-not-waiting", created_at=datetime.now(), raw_text="")
            store.create(request)

            with self.assertRaises(SiteCompletionConflictError):
                store.mark_site_completed(request.task_id, "vehicle_mileage")

            self.assertEqual(store.get(request.task_id)["site_statuses"]["vehicle_mileage"]["status"], "not_started")

    def test_worker_queue_state_tracks_queue_claim_and_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-queue", created_at=datetime.now(), raw_text="")
            store.create(request)

            queued = store.queue_for_worker("task-queue")
            self.assertEqual(queued["worker_queue"]["status"], "queued")
            self.assertTrue(queued["worker_queue"]["queued_at"])

            claimed = store.claim_next_for_worker("worker-a")
            assert claimed is not None
            self.assertEqual(claimed["worker_queue"]["status"], "claimed")
            self.assertEqual(claimed["worker_queue"]["worker_id"], "worker-a")
            self.assertTrue(claimed["worker_queue"]["claimed_at"])

            completed = store.set_overall_status("task-queue", "desktop_fast_completed", "done")
            self.assertEqual(completed["worker_queue"]["status"], "completed")
            self.assertTrue(completed["worker_queue"]["completed_at"])

    def test_worker_claim_reclaims_task_after_lease_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-lease", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.queue_for_worker("task-lease")
            first = store.claim_next_for_worker("worker-a")
            assert first is not None
            first["worker_queue"]["lease_expires_at"] = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            store.save_payload("task-lease", first)

            reclaimed = store.claim_next_for_worker("worker-b")

            self.assertIsNotNone(reclaimed)
            assert reclaimed is not None
            self.assertEqual(reclaimed["worker_queue"]["worker_id"], "worker-b")
            self.assertEqual(reclaimed["worker_queue"]["status"], "claimed")
            self.assertEqual(reclaimed["worker_queue"]["claim_attempt"], "2")
            self.assertTrue(reclaimed["worker_queue"]["lease_expires_at"])

    def test_worker_claim_reclaims_legacy_claim_without_attempt_as_second_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-legacy-lease", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.queue_for_worker(request.task_id)
            first = store.claim_next_for_worker("worker-a")
            assert first is not None
            first["worker_queue"].pop("claim_attempt", None)
            first["worker_queue"]["lease_expires_at"] = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            store.save_payload(request.task_id, first)

            reclaimed = store.claim_next_for_worker("worker-b")

            self.assertIsNotNone(reclaimed)
            assert reclaimed is not None
            self.assertEqual(reclaimed["worker_queue"]["worker_id"], "worker-b")
            self.assertEqual(reclaimed["worker_queue"]["claim_attempt"], "2")

    def test_claim_specific_task_creates_fenced_claim_and_is_idempotent_for_same_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-manual-claim", created_at=datetime.now(), raw_text="")
            store.create(request)

            first = store.claim_task_for_worker(request.task_id, "worker-a")
            second = store.claim_task_for_worker(request.task_id, "worker-a")

            self.assertEqual(first["worker_queue"]["status"], "claimed")
            self.assertTrue(first["worker_queue"]["claim_id"])
            self.assertEqual(second["worker_queue"]["claim_id"], first["worker_queue"]["claim_id"])
            self.assertEqual(second["worker_queue"]["claim_attempt"], "1")

    def test_claim_specific_task_rejects_fully_completed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-already-complete", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site_key, site in payload["site_statuses"].items():
                if site_key != "fuel_record":
                    site["status"] = "completed_by_user"
            store.save_payload(request.task_id, payload)

            with self.assertRaises(WorkerClaimConflictError) as raised:
                store.claim_task_for_worker(request.task_id, "worker-a")

            self.assertEqual(raised.exception.code, "task_already_completed")

    def test_claim_specific_task_allows_failed_or_needs_update_task(self):
        for status in ("vehicle_mileage_failed", "vehicle_mileage_needs_update"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                store = JsonTaskStore(Path(tmp))
                request = AmbulanceReturnRequest(task_id=f"task-retry-{status}", created_at=datetime.now(), raw_text="")
                payload = store.create(request)
                payload["site_statuses"]["vehicle_mileage"]["status"] = status
                store.save_payload(request.task_id, payload)

                claimed = store.claim_task_for_worker(request.task_id, "worker-a")

                self.assertEqual(claimed["worker_queue"]["status"], "claimed")

    def test_claim_specific_task_rejects_active_other_worker_but_reclaims_expired_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-manual-conflict", created_at=datetime.now(), raw_text="")
            store.create(request)
            first = store.claim_task_for_worker(request.task_id, "worker-a")

            with self.assertRaises(WorkerClaimConflictError):
                store.claim_task_for_worker(request.task_id, "worker-b")

            first["worker_queue"]["lease_expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")
            store.save_payload(request.task_id, first)
            reclaimed = store.claim_task_for_worker(request.task_id, "worker-b")
            self.assertEqual(reclaimed["worker_queue"]["worker_id"], "worker-b")
            self.assertEqual(reclaimed["worker_queue"]["claim_attempt"], "2")

    def test_claim_specific_task_reclaims_legacy_claim_without_attempt_as_second_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(
                task_id="task-legacy-manual-conflict",
                created_at=datetime.now(),
                raw_text="",
            )
            store.create(request)
            first = store.claim_task_for_worker(request.task_id, "worker-a")
            first["worker_queue"].pop("claim_attempt", None)
            first["worker_queue"]["lease_expires_at"] = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            store.save_payload(request.task_id, first)

            reclaimed = store.claim_task_for_worker(request.task_id, "worker-b")

            self.assertEqual(reclaimed["worker_queue"]["worker_id"], "worker-b")
            self.assertEqual(reclaimed["worker_queue"]["claim_attempt"], "2")

    def test_worker_claim_lease_active_requires_claimed_status_and_future_valid_expiry(self):
        future = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
        past = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")

        self.assertTrue(worker_claim_lease_is_active({"worker_queue": {"status": "claimed", "lease_expires_at": future}}))
        self.assertFalse(worker_claim_lease_is_active({"worker_queue": {"status": "claimed", "lease_expires_at": past}}))
        self.assertFalse(worker_claim_lease_is_active({"worker_queue": {"status": "queued", "lease_expires_at": future}}))
        self.assertFalse(worker_claim_lease_is_active({"worker_queue": {"status": "claimed", "lease_expires_at": "bad"}}))

    def test_worker_running_status_keeps_claim_lease_reclaimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-running-lease", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.queue_for_worker("task-running-lease")
            store.claim_next_for_worker("worker-a")

            running = store.set_overall_status("task-running-lease", "worker_running", "running")

            self.assertEqual(running["worker_queue"]["status"], "claimed")
            self.assertTrue(running["worker_queue"]["lease_expires_at"])
            running["worker_queue"]["lease_expires_at"] = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            store.save_payload("task-running-lease", running)
            reclaimed = store.claim_next_for_worker("worker-b")
            self.assertIsNotNone(reclaimed)

    def test_expire_stale_running_sites_keeps_old_site_while_worker_claim_lease_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-active-old-site", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.queue_for_worker(request.task_id)
            payload = store.claim_next_for_worker("worker-a")
            assert payload is not None
            site = payload["site_statuses"]["vehicle_mileage"]
            site["status"] = "vehicle_mileage_running"
            site["updated_at"] = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
            store.save_payload(request.task_id, payload)

            unchanged = store.expire_stale_running_sites(request.task_id, 600, "stale")

            self.assertEqual(unchanged["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_running")
            self.assertEqual(unchanged["worker_queue"]["status"], "claimed")

    def test_claim_quarantines_corrupt_json_and_continues_to_valid_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            corrupt = store.path_for("000-corrupt")
            corrupt.write_text("{broken", encoding="utf-8")
            request = AmbulanceReturnRequest(task_id="task-valid", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.queue_for_worker("task-valid")

            try:
                claimed = store.claim_next_for_worker("worker-a")
            except json.JSONDecodeError as exc:
                self.fail(f"corrupt task JSON must be quarantined instead of crashing claim: {exc}")

            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["task"]["task_id"], "task-valid")
            self.assertFalse(corrupt.exists())
            self.assertEqual(len(list((Path(tmp) / "quarantine").glob("000-corrupt-*.json.corrupt"))), 1)

    def test_claim_quarantines_task_with_unsafe_embedded_task_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            bad_request = AmbulanceReturnRequest(task_id="000-malicious", created_at=datetime.now(), raw_text="")
            bad_payload = store.create(bad_request)
            bad_payload = store.queue_for_worker("000-malicious")
            bad_payload["task"]["task_id"] = r"..\cases\latest"
            bad_path = store.path_for("000-malicious")
            bad_path.write_text(json.dumps(bad_payload), encoding="utf-8")
            valid_request = AmbulanceReturnRequest(task_id="task-valid", created_at=datetime.now(), raw_text="")
            store.create(valid_request)
            store.queue_for_worker("task-valid")

            claimed = store.claim_next_for_worker("worker-a")

            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["task"]["task_id"], "task-valid")
            self.assertFalse(bad_path.exists())
            self.assertEqual(len(list((Path(tmp) / "quarantine").glob("000-malicious-*.json.corrupt"))), 1)

    def test_list_recent_quarantines_corrupt_json_and_returns_valid_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-valid", created_at=datetime.now(), raw_text="")
            store.create(request)
            corrupt = store.path_for("zzz-corrupt")
            corrupt.write_text("not-json", encoding="utf-8")

            try:
                recent = store.list_recent()
            except json.JSONDecodeError as exc:
                self.fail(f"corrupt task JSON must be quarantined instead of crashing list: {exc}")

            self.assertEqual([item["task"]["task_id"] for item in recent], ["task-valid"])
            self.assertFalse(corrupt.exists())
            self.assertEqual(len(list((Path(tmp) / "quarantine").glob("zzz-corrupt-*.json.corrupt"))), 1)

    def test_list_recent_quarantines_task_with_unsafe_embedded_task_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="unsafe-list", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            payload["task"]["task_id"] = r"..\cases\latest"
            unsafe_path = store.path_for("unsafe-list")
            unsafe_path.write_text(json.dumps(payload), encoding="utf-8")

            recent = store.list_recent()

            self.assertEqual(recent, [])
            self.assertFalse(unsafe_path.exists())
            self.assertEqual(len(list((Path(tmp) / "quarantine").glob("unsafe-list-*.json.corrupt"))), 1)

    def test_site_attempts_preserve_retry_history_per_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-retry", created_at=datetime.now(), raw_text="")
            store.create(request)

            store.update_site_result(
                "task-retry",
                SiteAutomationResult("disinfection", "消毒", "disinfection_failed", "login failed"),
            )
            store.update_site_result(
                "task-retry",
                SiteAutomationResult("disinfection", "消毒", "disinfection_saved", "retry ok"),
            )

            payload = store.get("task-retry")
            attempts = payload["site_attempts"]["disinfection"]
            self.assertEqual([item["status"] for item in attempts], ["disinfection_failed", "disinfection_saved"])
            self.assertEqual(attempts[0]["detail"], "login failed")
            self.assertEqual(attempts[1]["detail"], "retry ok")

    def test_abort_running_task_marks_running_sites_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-abort", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.set_overall_status("task-abort", "desktop_fast_running", "running")
            store.update_site_result(
                "task-abort",
                SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_running", "running"),
            )

            aborted = store.abort_running_task("task-abort", "使用者中止登打。")

            self.assertEqual(aborted["overall_status"], "desktop_fast_completed_with_errors")
            self.assertEqual(aborted["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_failed")
            self.assertEqual(aborted["site_statuses"]["vehicle_mileage"]["detail"], "使用者中止登打。")
            self.assertEqual(aborted["site_attempts"]["vehicle_mileage"][-1]["status"], "vehicle_mileage_failed")
            self.assertEqual(aborted["events"][-1]["status"], "desktop_fast_completed_with_errors")

    def test_abort_running_task_rejects_reassigned_claim_generation_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-abort-reassigned", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            payload["worker_queue"].update(
                {
                    "status": "claimed",
                    "claim_id": "claim-b",
                    "worker_id": "worker-b",
                }
            )
            payload["overall_status"] = "desktop_fast_running"
            payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_running"
            store.save_payload(request.task_id, payload)

            with self.assertRaises(WorkerClaimConflictError) as raised:
                store.abort_running_task(
                    request.task_id,
                    execution_lease_active=True,
                    expected_claim_id="claim-a",
                )

            self.assertEqual(raised.exception.code, "worker_claim_conflict")
            current = store.get(request.task_id)
            self.assertEqual(current["worker_queue"]["status"], "claimed")
            self.assertEqual(current["worker_queue"]["claim_id"], "claim-b")
            self.assertEqual(current["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_running")

    def test_requeue_issues_new_queue_generation_and_stale_abort_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-requeue-generation", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued_a = store.queue_for_worker(request.task_id)
            queue_a = queued_a["worker_queue"]["queue_id"]
            queued_b = store.queue_for_worker(request.task_id)
            queue_b = queued_b["worker_queue"]["queue_id"]

            self.assertTrue(queue_a)
            self.assertTrue(queue_b)
            self.assertNotEqual(queue_a, queue_b)
            with self.assertRaises(WorkerClaimConflictError) as raised:
                store.abort_running_task(
                    request.task_id,
                    expected_claim_id="",
                    expected_queue_id=queue_a,
                )

            self.assertEqual(raised.exception.code, "worker_claim_conflict")
            current = store.get(request.task_id)
            self.assertEqual(current["worker_queue"]["status"], "queued")
            self.assertEqual(current["worker_queue"]["queue_id"], queue_b)

    def test_queue_generation_is_preserved_when_claimed_and_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp), claim_lease_seconds=60)
            request = AmbulanceReturnRequest(task_id="task-queue-generation-claim", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued = store.queue_for_worker(request.task_id)
            queue_id = queued["worker_queue"]["queue_id"]
            claimed = store.claim_next_for_worker("worker-a")
            assert claimed is not None
            self.assertEqual(claimed["worker_queue"]["queue_id"], queue_id)
            claimed["worker_queue"]["lease_expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat(
                timespec="seconds"
            )
            store.save_payload(request.task_id, claimed)

            reclaimed = store.claim_next_for_worker("worker-b")
            assert reclaimed is not None
            self.assertEqual(reclaimed["worker_queue"]["queue_id"], queue_id)

    def test_requeue_preserves_monotonic_claim_attempt_across_generations(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-requeue-attempt", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued_a = store.queue_for_worker(request.task_id)
            claimed_a = store.claim_task_for_worker(request.task_id, "worker-a")
            self.assertEqual(claimed_a["worker_queue"]["claim_attempt"], "1")
            store.abort_running_task(
                request.task_id,
                expected_claim_id=claimed_a["worker_queue"]["claim_id"],
                expected_queue_id=queued_a["worker_queue"]["queue_id"],
            )

            store.queue_for_worker(request.task_id)
            claimed_b = store.claim_task_for_worker(request.task_id, "worker-b")

            self.assertEqual(claimed_b["worker_queue"]["claim_attempt"], "2")

    def test_requeue_infers_prior_attempt_from_legacy_completed_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-legacy-requeue", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued_a = store.queue_for_worker(request.task_id)
            claimed_a = store.claim_task_for_worker(request.task_id, "worker-a")
            completed_a = store.abort_running_task(
                request.task_id,
                expected_claim_id=claimed_a["worker_queue"]["claim_id"],
                expected_queue_id=queued_a["worker_queue"]["queue_id"],
            )
            completed_a["worker_queue"].pop("claim_attempt", None)
            store.save_payload(request.task_id, completed_a)

            store.queue_for_worker(request.task_id)
            claimed_b = store.claim_task_for_worker(request.task_id, "worker-b")

            self.assertEqual(claimed_b["worker_queue"]["claim_attempt"], "2")

    def test_requeue_after_never_claimed_abort_keeps_first_claim_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-never-claimed-requeue", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued = store.queue_for_worker(request.task_id)
            store.abort_running_task(
                request.task_id,
                expected_claim_id="",
                expected_queue_id=queued["worker_queue"]["queue_id"],
            )

            store.queue_for_worker(request.task_id)
            first_claim = store.claim_task_for_worker(request.task_id, "worker-a")

            self.assertEqual(first_claim["worker_queue"]["claim_attempt"], "1")

    def test_task_edit_preserves_monotonic_claim_attempt_for_next_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-edit-attempt", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued_a = store.queue_for_worker(request.task_id)
            claimed_a = store.claim_task_for_worker(request.task_id, "worker-a")
            store.abort_running_task(
                request.task_id,
                expected_claim_id=claimed_a["worker_queue"]["claim_id"],
                expected_queue_id=queued_a["worker_queue"]["queue_id"],
            )

            store.update_task(
                request.task_id,
                request,
                changed_site_keys={"vehicle_mileage"},
            )
            idle = store.get(request.task_id)
            self.assertEqual(idle["worker_queue"]["claim_attempt"], "1")
            store.queue_for_worker(request.task_id)
            claimed_b = store.claim_task_for_worker(request.task_id, "worker-b")

            self.assertEqual(claimed_b["worker_queue"]["claim_attempt"], "2")

    def test_task_edit_infers_prior_attempt_from_legacy_completed_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-legacy-edit-attempt", created_at=datetime.now(), raw_text="")
            store.create(request)
            queued_a = store.queue_for_worker(request.task_id)
            claimed_a = store.claim_task_for_worker(request.task_id, "worker-a")
            completed_a = store.abort_running_task(
                request.task_id,
                expected_claim_id=claimed_a["worker_queue"]["claim_id"],
                expected_queue_id=queued_a["worker_queue"]["queue_id"],
            )
            completed_a["worker_queue"].pop("claim_attempt", None)
            store.save_payload(request.task_id, completed_a)

            store.update_task(
                request.task_id,
                request,
                changed_site_keys={"vehicle_mileage"},
            )
            store.queue_for_worker(request.task_id)
            claimed_b = store.claim_task_for_worker(request.task_id, "worker-b")

            self.assertEqual(claimed_b["worker_queue"]["claim_attempt"], "2")

    def test_expire_stale_running_sites_marks_old_running_site_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-stale", created_at=datetime.now(), raw_text="")
            store.create(request)
            store.set_overall_status("task-stale", "desktop_fast_running", "running")
            store.update_site_result(
                "task-stale",
                SiteAutomationResult("consumables", "一站通耗材", "consumables_running", "running"),
            )
            path = store.path_for("task-stale")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["site_statuses"]["consumables"]["updated_at"] = (
                datetime.now() - timedelta(minutes=11)
            ).isoformat(timespec="seconds")
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            expired = store.expire_stale_running_sites(
                "task-stale",
                600,
                "登打流程超過 10 分鐘未回報，已自動中止。",
            )

            self.assertEqual(expired["overall_status"], "desktop_fast_completed_with_errors")
            self.assertEqual(expired["site_statuses"]["consumables"]["status"], "consumables_failed")
            self.assertEqual(expired["site_statuses"]["consumables"]["detail"], "登打流程超過 10 分鐘未回報，已自動中止。")
            self.assertEqual(expired["site_attempts"]["consumables"][-1]["status"], "consumables_failed")
            self.assertEqual(expired["events"][-1]["status"], "desktop_fast_completed_with_errors")

    def test_site_result_records_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-diag", created_at=datetime.now(), raw_text="")
            store.create(request)

            store.update_site_result(
                "task-diag",
                SiteAutomationResult("consumables", "一站通耗材", "consumables_failed", "SSO login failed"),
            )

            payload = store.get("task-diag")
            site = payload["site_statuses"]["consumables"]
            attempt = payload["site_attempts"]["consumables"][0]
            self.assertEqual(site["failure_stage"], "登入一站通")
            self.assertIn("登入", site["failure_reason"])
            self.assertEqual(attempt["failure_stage"], "登入一站通")
            self.assertIn("驗證碼", attempt["next_action"])

    def test_worker_queue_state_reads_legacy_overall_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            legacy_payload = {
                "task": {"task_id": "legacy"},
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "overall_status": "queued_for_worker",
                "site_statuses": {},
                "events": [],
            }
            store.path_for("legacy").write_text(__import__("json").dumps(legacy_payload, ensure_ascii=False), encoding="utf-8")

            claimed = store.claim_next_for_worker("worker-b")
            assert claimed is not None
            self.assertEqual(claimed["worker_queue"]["status"], "claimed")
            self.assertEqual(claimed["worker_queue"]["worker_id"], "worker-b")

    def test_delete_removes_task_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="delete-me", created_at=datetime.now(), raw_text="")
            store.create(request)

            store.delete("delete-me")

            self.assertFalse((Path(tmp) / "delete-me.json").exists())
            with self.assertRaises(FileNotFoundError):
                store.get("delete-me")

    def test_cleanup_keeps_old_unfinished_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="old-task", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            payload["updated_at"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
            store.path_for("old-task").write_text(__import__("json").dumps(payload), encoding="utf-8")

            recent = store.list_recent()

            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["task"]["task_id"], "old-task")
            self.assertTrue((Path(tmp) / "old-task.json").exists())

    def test_cleanup_keeps_fully_done_tasks_until_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="done-task", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site in payload["site_statuses"].values():
                site["status"] = "completed_by_user"
            store.save_payload("done-task", payload)

            self.assertEqual(len(store.list_recent()), 1)
            self.assertTrue((Path(tmp) / "done-task.json").exists())

    def test_cleanup_keeps_fully_done_tasks_for_mileage_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="done-history-task", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site in payload["site_statuses"].values():
                site["status"] = "completed_by_user"
            payload["updated_at"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
            store.path_for("done-history-task").write_text(__import__("json").dumps(payload), encoding="utf-8")

            self.assertEqual(len(store.list_recent()), 1)
            self.assertTrue((Path(tmp) / "done-history-task.json").exists())

    def test_cleanup_treats_unselected_fuel_site_as_done_for_four_site_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="four-site-done", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site_key, site in payload["site_statuses"].items():
                if site_key != "fuel_record":
                    site["status"] = "completed_by_user"
            payload["updated_at"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
            store.path_for("four-site-done").write_text(json.dumps(payload), encoding="utf-8")

            recent = store.list_recent()

            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["task"]["task_id"], "four-site-done")

    def test_task_edit_clears_stale_per_vehicle_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="task-edit", created_at=datetime.now(), raw_text="", vehicle="新坡91")
            payload = store.create(request)
            payload["site_statuses"]["consumables"]["vehicle_results"] = {
                "新坡91": {"status": "consumables_saved", "detail": "old", "updated_at": "old"}
            }
            payload["site_statuses"]["disinfection"]["vehicle_results"] = {
                "新坡91": {"status": "disinfection_failed", "detail": "old", "updated_at": "old"}
            }
            store.save_payload("task-edit", payload)

            updated = store.update_task("task-edit", request)

            for site in updated["site_statuses"].values():
                self.assertNotIn("vehicle_results", site)

    def test_multi_vehicle_edit_preserves_unchanged_vehicle_checkpoint_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-multi-edit",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "driver": "甲", "mileage": "101", "return_time": "0900"},
                        {"vehicle": "新坡93", "driver": "乙", "mileage": "202", "return_time": "0910"},
                    ],
                }
            )
            payload = store.create(previous)
            payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_saved"
            payload["site_statuses"]["vehicle_mileage"]["vehicle_results"] = {
                "新坡92": {"status": "vehicle_mileage_saved", "detail": "first saved"},
                "新坡93": {"status": "vehicle_mileage_saved", "detail": "second saved"},
            }
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["vehicle_entries"][1]["mileage"] = "210"
            current = AmbulanceReturnRequest.from_dict(current_payload)

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"vehicle_mileage"},
                site_update_contexts={
                    "vehicle_mileage": {
                        "previous_task": previous.to_dict(),
                        "current_task": current.to_dict(),
                    }
                },
            )

            self.assertIn("vehicle_results", updated["site_statuses"]["vehicle_mileage"])
            results = updated["site_statuses"]["vehicle_mileage"]["vehicle_results"]
            self.assertEqual(list(results), ["新坡92"])
            self.assertEqual(results["新坡92"]["status"], "vehicle_mileage_saved")
            self.assertNotIn("新坡93", results)

    def test_multi_vehicle_duty_edit_preserves_only_unchanged_vehicle_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-multi-duty-edit",
                    "created_at": "2026-07-13T08:00:00",
                    "case_reason": "急病",
                    "work_note": "返隊完成。",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "driver": "甲", "patient_summary": "男一名"},
                        {"vehicle": "新坡93", "driver": "乙", "patient_summary": "女一名"},
                    ],
                }
            )
            payload = store.create(previous)
            duty_site = payload["site_statuses"]["duty_work_log"]
            duty_site["status"] = "duty_work_log_saved"
            duty_site["vehicle_results"] = {
                "新坡92": {"status": "duty_work_log_saved", "detail": "first saved"},
                "新坡93": {"status": "duty_work_log_saved", "detail": "second saved"},
            }
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["vehicle_entries"][1]["patient_summary"] = "女二名"
            current = AmbulanceReturnRequest.from_dict(current_payload)
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"duty_work_log"},
                site_update_contexts={"duty_work_log": context},
            )

            updated_site = updated["site_statuses"]["duty_work_log"]
            self.assertEqual(updated_site["status"], "duty_work_log_needs_update")
            self.assertEqual(list(updated_site["vehicle_results"]), ["新坡92"])
            self.assertEqual(updated_site["update_context"], context)

    def test_enabling_previously_unselected_second_vehicle_fuel_marks_site_for_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-enable-second-fuel",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "fuel_record": {"enabled": False}},
                        {"vehicle": "新坡93", "fuel_record": {"enabled": False}},
                    ],
                }
            )
            store.create(previous)
            current_payload = previous.to_dict()
            current_payload["vehicle_entries"][1]["fuel_record"] = {
                "enabled": True,
                "date": "20260713",
                "time": "0915",
                "driver": "乙",
                "quantity": "40",
                "unit_price": "30",
            }
            current = AmbulanceReturnRequest.from_dict(current_payload)
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"fuel_record"},
                site_update_contexts={"fuel_record": context},
            )

            fuel_site = updated["site_statuses"]["fuel_record"]
            self.assertEqual(fuel_site["status"], "fuel_record_needs_update")
            self.assertEqual(fuel_site["update_context"], context)

    def test_disabling_saved_fuel_requires_visible_manual_record_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-disable-fuel",
                    "created_at": "2026-07-13T08:00:00",
                    "vehicle": "新坡92",
                    "fuel_record": {
                        "enabled": True,
                        "date": "20260713",
                        "time": "0915",
                        "driver": "甲",
                        "quantity": "40",
                        "unit_price": "30",
                    },
                }
            )
            payload = store.create(previous)
            payload["site_statuses"]["fuel_record"]["status"] = "fuel_record_saved"
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["fuel_record"]["enabled"] = False
            current = AmbulanceReturnRequest.from_dict(current_payload)
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"fuel_record"},
                site_update_contexts={"fuel_record": context},
            )

            fuel_site = updated["site_statuses"]["fuel_record"]
            self.assertEqual(fuel_site["status"], "fuel_record_waiting_confirmation")
            self.assertIn("人工刪除", fuel_site["detail"])
            self.assertEqual(fuel_site["update_context"], context)
            self.assertFalse(store._is_fully_done(updated))

    def test_removing_second_vehicle_requires_manual_cleanup_for_existing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-remove-second-vehicle",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "fuel_record": {"enabled": False}},
                        {
                            "vehicle": "新坡93",
                            "fuel_record": {
                                "enabled": True,
                                "date": "20260713",
                                "time": "0915",
                                "quantity": "40",
                                "unit_price": "30",
                            },
                        },
                    ],
                }
            )
            payload = store.create(previous)
            for site_key in ("duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"):
                site = payload["site_statuses"][site_key]
                site["status"] = f"{site_key}_saved"
                site["vehicle_results"] = {
                    "新坡92": {"status": f"{site_key}_saved", "detail": "first"},
                    "新坡93": {"status": f"{site_key}_saved", "detail": "second"},
                }
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["two_vehicle"] = False
            current_payload["vehicle"] = "新坡92"
            current_payload["fuel_record"] = {"enabled": False}
            current_payload["vehicle_entries"] = []
            current = AmbulanceReturnRequest.from_dict(current_payload)
            changed_sites = {"duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"}
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys=changed_sites,
                site_update_contexts={site_key: context for site_key in changed_sites},
            )

            for site_key in changed_sites:
                site = updated["site_statuses"][site_key]
                self.assertEqual(site["status"], f"{site_key}_waiting_confirmation")
                self.assertIn("新坡93", site["detail"])
                self.assertIn("人工刪除", site["detail"])
                self.assertIn("所有現行車輛", site["detail"])
                self.assertNotIn("vehicle_results", site)

    def test_replacing_second_vehicle_requires_delete_old_and_manually_create_current_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-replace-second-vehicle",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [{"vehicle": "新坡92"}, {"vehicle": "新坡93"}],
                }
            )
            payload = store.create(previous)
            site = payload["site_statuses"]["consumables"]
            site["status"] = "consumables_saved"
            site["vehicle_results"] = {
                "新坡92": {"status": "consumables_saved"},
                "新坡93": {"status": "consumables_saved"},
            }
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["vehicle_entries"][1]["vehicle"] = "新坡94"
            current = AmbulanceReturnRequest.from_dict(current_payload)
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"consumables"},
                site_update_contexts={"consumables": context},
            )

            updated_site = updated["site_statuses"]["consumables"]
            self.assertEqual(updated_site["status"], "consumables_waiting_confirmation")
            self.assertIn("新坡93", updated_site["detail"])
            self.assertIn("所有現行車輛", updated_site["detail"])

    def test_failed_partial_vehicle_site_keeps_update_context_after_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            previous = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-partial-edit",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92", "driver": "甲", "mileage": "101", "return_time": "0900"},
                        {"vehicle": "新坡93", "driver": "乙", "mileage": "202", "return_time": "0910"},
                    ],
                }
            )
            payload = store.create(previous)
            site = payload["site_statuses"]["vehicle_mileage"]
            site["status"] = "vehicle_mileage_failed"
            site["vehicle_results"] = {
                "新坡92": {"status": "vehicle_mileage_saved", "detail": "first saved"},
                "新坡93": {"status": "vehicle_mileage_failed", "detail": "second failed"},
            }
            store.save_payload(previous.task_id, payload)
            current_payload = previous.to_dict()
            current_payload["vehicle_entries"][1]["mileage"] = "210"
            current = AmbulanceReturnRequest.from_dict(current_payload)
            context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

            updated = store.update_task(
                previous.task_id,
                current,
                changed_site_keys={"vehicle_mileage"},
                site_update_contexts={"vehicle_mileage": context},
            )

            updated_site = updated["site_statuses"]["vehicle_mileage"]
            self.assertEqual(updated_site["update_context"], context)
            self.assertEqual(updated_site["status"], "vehicle_mileage_needs_update")
            self.assertEqual(list(updated_site["vehicle_results"]), ["新坡92"])

    def test_vehicle_waiting_confirmation_takes_priority_over_other_vehicle_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-wait-and-fail",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {"vehicle": "新坡92"},
                        {"vehicle": "新坡93"},
                    ],
                }
            )
            store.create(request)
            store.update_site_result(
                request.task_id,
                SiteAutomationResult(
                    "vehicle_mileage",
                    "車輛里程",
                    "vehicle_mileage_waiting_confirmation",
                    "92 wait",
                ),
                vehicle_key="新坡92",
            )
            updated = store.update_site_result(
                request.task_id,
                SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_failed", "93 failed"),
                vehicle_key="新坡93",
            )

            self.assertEqual(
                updated["site_statuses"]["vehicle_mileage"]["status"],
                "vehicle_mileage_waiting_confirmation",
            )

            confirmed = store.mark_site_completed(request.task_id, "vehicle_mileage", vehicle_key="新坡92")
            site = confirmed["site_statuses"]["vehicle_mileage"]
            self.assertEqual(site["vehicle_results"]["新坡92"]["status"], "completed_by_user")
            self.assertEqual(site["vehicle_results"]["新坡93"]["status"], "vehicle_mileage_failed")
            self.assertEqual(site["status"], "vehicle_mileage_failed")

    def test_whole_site_confirmation_cannot_hide_failed_or_missing_vehicle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-no-whole-confirm",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [{"vehicle": "新坡92"}, {"vehicle": "新坡93"}],
                }
            )
            payload = store.create(request)
            site = payload["site_statuses"]["vehicle_mileage"]
            site["status"] = "vehicle_mileage_waiting_confirmation"
            site["vehicle_results"] = {
                "新坡92": {"status": "vehicle_mileage_waiting_confirmation", "detail": "wait"},
                "新坡93": {"status": "vehicle_mileage_failed", "detail": "failed"},
            }
            store.save_payload(request.task_id, payload)

            with self.assertRaises(SiteCompletionConflictError):
                store.mark_site_completed(request.task_id, "vehicle_mileage")

            unchanged = store.get(request.task_id)["site_statuses"]["vehicle_mileage"]
            self.assertEqual(unchanged["vehicle_results"]["新坡93"]["status"], "vehicle_mileage_failed")

    def test_fuel_vehicle_aggregate_waits_only_for_enabled_vehicle_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest.from_dict(
                {
                    "task_id": "task-fuel-enabled-vehicles",
                    "created_at": "2026-07-13T08:00:00",
                    "two_vehicle": True,
                    "vehicle_entries": [
                        {
                            "vehicle": "新坡92",
                            "fuel_record": {"enabled": True, "date": "20260713", "time": "0915"},
                        },
                        {
                            "vehicle": "新坡93",
                            "fuel_record": {"enabled": False},
                        },
                    ],
                }
            )
            store.create(request)

            updated = store.update_site_result(
                request.task_id,
                SiteAutomationResult("fuel_record", "登打加油紀錄", "fuel_record_saved", "92 saved"),
                vehicle_key="新坡92",
            )

            site = updated["site_statuses"]["fuel_record"]
            self.assertEqual(site["status"], "fuel_record_saved")
            self.assertNotIn("新坡93: 等待回報", site["detail"])

    def test_cleanup_removes_fully_done_tasks_after_history_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp))
            request = AmbulanceReturnRequest(task_id="done-expired-task", created_at=datetime.now(), raw_text="")
            payload = store.create(request)
            for site in payload["site_statuses"].values():
                site["status"] = "completed_by_user"
            payload["updated_at"] = (datetime.now() - timedelta(days=15)).isoformat(timespec="seconds")
            store.path_for("done-expired-task").write_text(__import__("json").dumps(payload), encoding="utf-8")

            self.assertEqual(store.list_recent(), [])
            self.assertFalse((Path(tmp) / "done-expired-task.json").exists())


if __name__ == "__main__":
    unittest.main()
