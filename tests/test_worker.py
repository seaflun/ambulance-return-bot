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
import ambulance_bot.manual_task_lock as manual_task_lock_module
from ambulance_bot.credential_envelope import seal_credential_payload
from ambulance_bot.manual_task_lock import (
    acquire_manual_task_lock,
    clear_manual_task_lock,
    manual_task_lock_active,
    manual_task_lock_owner,
    manual_task_lock_path,
    manual_task_lock_task_id,
    refresh_manual_task_lock,
    run_with_manual_task_lock_owner,
    set_manual_task_lock,
)
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.selenium_local import DutyCaseLookupResult
from ambulance_bot.task_cancellation import request_task_cancellation


class WorkerTests(unittest.TestCase):
    def test_consumables_manual_case_identity_change_does_not_start_chrome(self):
        previous = AmbulanceReturnRequest(
            task_id="task-consumables-manual",
            created_at=__import__("datetime").datetime(2026, 7, 13, 8, 0),
            raw_text="",
            case_id="20260713080500001",
            case_date="2026-07-13",
            case_time="0805",
            vehicle="新坡92",
        )
        current = AmbulanceReturnRequest.from_dict(
            {**previous.to_dict(), "case_id": "20260713081000002", "case_time": "0810"}
        )
        context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

        with mock.patch.object(worker_module, "post_status"), mock.patch.object(
            worker_module,
            "login_acs_and_get_driver",
        ) as login:
            result = worker_module.run_consumables_worker_task(
                "http://nas",
                "worker-a",
                current.to_dict(),
                Path("artifacts"),
                update_context=context,
            )

        self.assertEqual(result.status, "consumables_waiting_confirmation")
        self.assertIn("人工", result.detail)
        login.assert_not_called()

    def setUp(self):
        worker_module.MANUAL_TASK_ACTIVE.clear()
        worker_module._TASK_CLAIM_CONTEXT.clear()
        stale_claims = getattr(worker_module, "_STALE_TASK_CLAIMS", None)
        if stale_claims is not None:
            stale_claims.clear()
        retry_after = getattr(worker_module, "_STATUS_DELIVERY_RETRY_AFTER", None)
        if retry_after is not None:
            retry_after.clear()
        cancellation_events = getattr(worker_module, "_TASK_CANCELLATION_EVENTS", None)
        if cancellation_events is not None:
            cancellation_events.clear()

    def test_manual_execution_lease_heartbeats_until_end(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"WORKER_EXECUTION_LEASE_HEARTBEAT_SECONDS": "0.01"},
        ):
            artifacts_dir = Path(tmp)
            event = worker_module.begin_manual_task_execution("task-heartbeat-lock", artifacts_dir)
            self.assertIsNotNone(event)
            path = manual_task_lock_path(artifacts_dir)
            first = json.loads(path.read_text(encoding="utf-8"))["heartbeat_at"]
            deadline = time.time() + 1.0
            latest = first
            while latest <= first and time.time() < deadline:
                time.sleep(0.01)
                latest = json.loads(path.read_text(encoding="utf-8"))["heartbeat_at"]

            self.assertGreater(latest, first)
            assert event is not None
            worker_module.end_manual_task_execution("task-heartbeat-lock", event, artifacts_dir)
            self.assertFalse(path.exists())

    def test_execution_lease_heartbeat_retries_transient_refresh_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"WORKER_EXECUTION_LEASE_HEARTBEAT_SECONDS": "0.01"},
        ):
            cancellation_event = threading.Event()
            refreshed_after_error = threading.Event()
            calls = 0

            def refresh(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("antivirus temporarily blocked replace")
                refreshed_after_error.set()
                return True

            with mock.patch.object(worker_module, "refresh_manual_task_lock", side_effect=refresh):
                stop, thread = worker_module._start_execution_lease_heartbeat(
                    Path(tmp),
                    "worker-manual:task-heartbeat-retry:owner",
                    "task-heartbeat-retry",
                    cancellation_event,
                )
                self.assertTrue(refreshed_after_error.wait(1.0))
                stop.set()
                thread.join(1.0)

            self.assertGreaterEqual(calls, 2)
            self.assertFalse(cancellation_event.is_set())

    def test_execution_lease_heartbeat_owner_loss_cancels_execution_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"WORKER_EXECUTION_LEASE_HEARTBEAT_SECONDS": "0.01"},
        ), mock.patch.object(
            worker_module,
            "refresh_manual_task_lock",
            return_value=False,
        ):
            cancellation_event = threading.Event()
            stop, thread = worker_module._start_execution_lease_heartbeat(
                Path(tmp),
                "worker-manual:task-heartbeat-owner-loss:owner",
                "task-heartbeat-owner-loss",
                cancellation_event,
            )

            self.assertTrue(cancellation_event.wait(1.0))
            thread.join(1.0)
            self.assertFalse(thread.is_alive())
            stop.set()

    def test_execution_lease_heartbeat_repeated_io_errors_cancel_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"WORKER_EXECUTION_LEASE_HEARTBEAT_SECONDS": "0.01"},
        ), mock.patch.object(
            worker_module,
            "refresh_manual_task_lock",
            side_effect=OSError("lease storage unavailable"),
        ) as refresh:
            cancellation_event = threading.Event()
            stop, thread = worker_module._start_execution_lease_heartbeat(
                Path(tmp),
                "worker-manual:task-heartbeat-io-loss:owner",
                "task-heartbeat-io-loss",
                cancellation_event,
            )

            self.assertTrue(cancellation_event.wait(1.0))
            thread.join(1.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(refresh.call_count, 3)
            stop.set()

    def test_execution_lease_owner_loss_stops_inner_event_for_same_claim(self):
        task_id = "task-heartbeat-inner-event"
        outer_event = threading.Event()
        inner_event = threading.Event()
        worker_module._register_task_cancellation_event(task_id, outer_event, claim_id="claim-a")
        worker_module._register_task_cancellation_event(task_id, inner_event, claim_id="claim-a")
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ,
                {"WORKER_EXECUTION_LEASE_HEARTBEAT_SECONDS": "0.01"},
            ), mock.patch.object(
                worker_module,
                "refresh_manual_task_lock",
                return_value=False,
            ):
                stop, thread = worker_module._start_execution_lease_heartbeat(
                    Path(tmp),
                    "worker-manual:task-heartbeat-inner-event:owner",
                    task_id,
                    outer_event,
                )

                self.assertTrue(outer_event.wait(1.0))
                thread.join(1.0)
                self.assertTrue(inner_event.is_set())
                self.assertFalse(thread.is_alive())
                stop.set()
        finally:
            worker_module._unregister_task_cancellation_event(task_id, inner_event)
            worker_module._unregister_task_cancellation_event(task_id, outer_event)

    def test_auto_claim_heartbeat_owner_loss_stops_rebound_inner_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            task_id = "task-rebound-heartbeat"
            outer_event = worker_module.begin_manual_task_execution("__auto_claim__", artifacts_dir)
            self.assertIsNotNone(outer_event)
            assert outer_event is not None
            worker_module._remember_task_claim(
                {"task_id": task_id},
                {"claim_id": "claim-a", "worker_id": "PC-01"},
            )
            worker_module._rebind_task_execution("__auto_claim__", task_id, outer_event)
            inner_event = threading.Event()
            worker_module._register_task_cancellation_event(task_id, inner_event)
            try:
                with mock.patch.dict(
                    os.environ,
                    {"WORKER_EXECUTION_LEASE_HEARTBEAT_SECONDS": "0.01"},
                ), mock.patch.object(
                    worker_module,
                    "refresh_manual_task_lock",
                    return_value=False,
                ):
                    stop, thread = worker_module._start_execution_lease_heartbeat(
                        artifacts_dir,
                        worker_module._EXECUTION_LEASES[task_id][1],
                        "__auto_claim__",
                        outer_event,
                    )
                    self.assertTrue(inner_event.wait(1.0))
                    thread.join(1.0)
                    self.assertFalse(thread.is_alive())
                    stop.set()
            finally:
                worker_module._unregister_task_cancellation_event(task_id, inner_event)
                worker_module.end_manual_task_execution(task_id, outer_event, artifacts_dir)

    def test_manual_execution_owner_is_unique_when_pid_and_thread_id_are_reused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            worker_module.os,
            "getpid",
            return_value=123,
        ), mock.patch.object(
            worker_module.threading,
            "get_ident",
            return_value=456,
        ):
            artifacts_dir = Path(tmp)
            first_event = worker_module.begin_manual_task_execution("task-owner-unique", artifacts_dir)
            self.assertIsNotNone(first_event)
            assert first_event is not None
            first_owner = worker_module._EXECUTION_LEASES["task-owner-unique"][1]
            worker_module.end_manual_task_execution("task-owner-unique", first_event, artifacts_dir)

            second_event = worker_module.begin_manual_task_execution("task-owner-unique", artifacts_dir)
            self.assertIsNotNone(second_event)
            assert second_event is not None
            second_owner = worker_module._EXECUTION_LEASES["task-owner-unique"][1]
            worker_module.end_manual_task_execution("task-owner-unique", second_event, artifacts_dir)

            self.assertNotEqual(first_owner, second_owner)

    def test_begin_manual_execution_acquire_error_releases_local_guards(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            worker_module,
            "acquire_manual_task_lock",
            side_effect=OSError("lock path unavailable"),
        ):
            event = worker_module.begin_manual_task_execution("task-acquire-error", Path(tmp))

        self.assertIsNone(event)
        self.assertFalse(worker_module.MANUAL_TASK_ACTIVE.is_set())
        self.assertFalse(worker_module._TASK_EXECUTION_LOCK.locked())
        self.assertNotIn("task-acquire-error", worker_module._EXECUTION_LEASES)

    def test_begin_manual_execution_heartbeat_start_error_rolls_back_every_lease(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            worker_module,
            "_start_execution_lease_heartbeat",
            side_effect=RuntimeError("thread start failed"),
        ):
            artifacts_dir = Path(tmp)
            event = worker_module.begin_manual_task_execution(
                "task-heartbeat-start-error",
                artifacts_dir,
            )

            self.assertIsNone(event)
            self.assertFalse(worker_module.MANUAL_TASK_ACTIVE.is_set())
            self.assertFalse(worker_module._TASK_EXECUTION_LOCK.locked())
            self.assertNotIn("task-heartbeat-start-error", worker_module._EXECUTION_LEASES)
            self.assertNotIn("task-heartbeat-start-error", worker_module._TASK_CANCELLATION_EVENTS)
            self.assertFalse(manual_task_lock_path(artifacts_dir).exists())

    def test_end_manual_execution_clear_error_still_releases_local_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            task_id = "task-clear-error"
            event = worker_module.begin_manual_task_execution(task_id, artifacts_dir)
            self.assertIsNotNone(event)
            assert event is not None
            owner = worker_module._EXECUTION_LEASES[task_id][1]

            with mock.patch.object(
                worker_module,
                "clear_manual_task_lock",
                side_effect=OSError("lock path unavailable"),
            ):
                worker_module.end_manual_task_execution(task_id, event, artifacts_dir)

            self.assertFalse(worker_module.MANUAL_TASK_ACTIVE.is_set())
            self.assertFalse(worker_module._TASK_EXECUTION_LOCK.locked())
            self.assertNotIn(task_id, worker_module._EXECUTION_LEASES)
            self.assertTrue(clear_manual_task_lock(artifacts_dir, owner))

    def test_auto_claim_execution_lease_rebinds_lock_to_claimed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            event = worker_module.begin_manual_task_execution("__auto_claim__", artifacts_dir)
            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(manual_task_lock_task_id(artifacts_dir), "__auto_claim__")

            worker_module._rebind_task_execution("__auto_claim__", "claimed-task-a", event)
            try:
                self.assertEqual(manual_task_lock_task_id(artifacts_dir), "claimed-task-a")
            finally:
                worker_module.end_manual_task_execution("claimed-task-a", event, artifacts_dir)

    def test_failed_auto_claim_rebind_keeps_placeholder_lease_for_scoped_finally(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            event = worker_module.begin_manual_task_execution("__auto_claim__", artifacts_dir)
            self.assertIsNotNone(event)
            assert event is not None
            try:
                with mock.patch.object(worker_module, "bind_manual_task_lock_task", return_value=False):
                    with self.assertRaises(worker_module.StaleWorkerClaimError):
                        worker_module._rebind_task_execution("__auto_claim__", "claimed-task-a", event)

                self.assertIn("__auto_claim__", worker_module._EXECUTION_LEASES)
                self.assertNotIn("claimed-task-a", worker_module._EXECUTION_LEASES)
            finally:
                worker_module.end_manual_task_execution("__auto_claim__", event, artifacts_dir)

    def test_end_without_owned_lease_never_clears_another_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            owner_b = "worker-manual:task-b:2:2"
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, owner_b))

            worker_module.end_manual_task_execution(
                "missing-task-a",
                threading.Event(),
                artifacts_dir,
            )

            self.assertEqual(manual_task_lock_owner(artifacts_dir), owner_b)

    def test_old_execution_finally_cannot_clear_new_claim_context_or_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            task_id = "task-reused-finally"
            event_a = worker_module.begin_manual_task_execution(task_id, artifacts_dir)
            self.assertIsNotNone(event_a)
            assert event_a is not None
            worker_module._remember_task_claim(
                {"task_id": task_id},
                {"claim_id": "claim-a", "worker_id": "PC-01"},
            )
            owner = worker_module._EXECUTION_LEASES[task_id][1]
            event_b = threading.Event()
            worker_module._register_task_cancellation_event(task_id, event_b, claim_id="claim-b")
            worker_module._remember_task_claim(
                {"task_id": task_id},
                {"claim_id": "claim-b", "worker_id": "PC-01"},
            )
            with worker_module._EXECUTION_LEASES_LOCK:
                lease = worker_module._EXECUTION_LEASES[task_id]
                worker_module._EXECUTION_LEASES[task_id] = (*lease[:4], event_b)

            try:
                worker_module.end_manual_task_execution(task_id, event_a, artifacts_dir)

                self.assertEqual(worker_module._task_claim_context(task_id)["claim_id"], "claim-b")
                self.assertIs(worker_module._EXECUTION_LEASES[task_id][4], event_b)
                self.assertEqual(manual_task_lock_owner(artifacts_dir), owner)
                self.assertTrue(worker_module.MANUAL_TASK_ACTIVE.is_set())
                self.assertTrue(worker_module._TASK_EXECUTION_LOCK.locked())
            finally:
                worker_module.end_manual_task_execution(task_id, event_b, artifacts_dir)

            self.assertEqual(worker_module._task_claim_context(task_id), {})
            self.assertFalse(manual_task_lock_path(artifacts_dir).exists())
            self.assertFalse(worker_module.MANUAL_TASK_ACTIVE.is_set())

    def test_cross_process_abort_marker_stops_only_its_worker_manual_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            task_id = "task-abort-marker-a"
            event = worker_module.begin_manual_task_execution(task_id, artifacts_dir)
            self.assertIsNotNone(event)
            assert event is not None
            try:
                owner = worker_module._EXECUTION_LEASES[task_id][1]
                request_task_cancellation(
                    artifacts_dir,
                    task_id,
                    execution_owner=owner,
                )

                with self.assertRaises(worker_module.StaleWorkerClaimError):
                    worker_module._raise_if_task_cancelled(task_id, event)

                other_event = threading.Event()
                worker_module._raise_if_task_cancelled("task-abort-marker-b", other_event)
                self.assertFalse(other_event.is_set())
            finally:
                worker_module.end_manual_task_execution(task_id, event, artifacts_dir)

    def test_old_claim_abort_marker_does_not_cancel_reused_owner_with_new_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            task_id = "task-reused-owner"
            event = worker_module.begin_manual_task_execution(task_id, artifacts_dir)
            self.assertIsNotNone(event)
            assert event is not None
            try:
                owner = worker_module._EXECUTION_LEASES[task_id][1]
                worker_module._remember_task_claim(
                    {"task_id": task_id},
                    {"claim_id": "claim-b", "worker_id": "PC-01"},
                )
                request_task_cancellation(
                    artifacts_dir,
                    task_id,
                    execution_owner=owner,
                    claim_id="claim-a",
                )

                worker_module._raise_if_task_cancelled(task_id, event)
                self.assertFalse(event.is_set())
            finally:
                worker_module.end_manual_task_execution(task_id, event, artifacts_dir)

    def test_same_claim_inner_event_uses_active_lease_owner_for_abort_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            task_id = "task-inner-cancellation-event"
            outer_event = worker_module.begin_manual_task_execution(task_id, artifacts_dir)
            self.assertIsNotNone(outer_event)
            assert outer_event is not None
            worker_module._remember_task_claim(
                {"task_id": task_id},
                {"claim_id": "claim-b", "worker_id": "PC-01"},
            )
            inner_event = threading.Event()
            worker_module._register_task_cancellation_event(task_id, inner_event)
            try:
                owner = worker_module._EXECUTION_LEASES[task_id][1]
                request_task_cancellation(
                    artifacts_dir,
                    task_id,
                    execution_owner=owner,
                    claim_id="claim-b",
                )

                with self.assertRaises(worker_module.StaleWorkerClaimError):
                    worker_module._raise_if_task_cancelled(task_id, inner_event)
                self.assertTrue(inner_event.is_set())
                self.assertFalse(outer_event.is_set())
            finally:
                worker_module._unregister_task_cancellation_event(task_id, inner_event)
                worker_module.end_manual_task_execution(task_id, outer_event, artifacts_dir)

    def test_claim_task_posts_selected_task_and_remembers_fenced_claim(self):
        response_payload = {
            "ok": True,
            "task": {"task_id": "task-manual"},
            "payload": {
                "task": {"task_id": "task-manual"},
                "worker_queue": {
                    "status": "claimed",
                    "claim_id": "claim-manual",
                    "worker_id": "worker-a",
                },
                "site_statuses": {
                    "duty_work_log": {"update_context": {"previous_task": {"task_id": "task-manual"}}}
                },
            },
        }
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(response_payload).encode("utf-8")

        with mock.patch.object(worker_module.urllib.request, "urlopen", return_value=response) as urlopen:
            task = worker_module.claim_task("http://nas", "task-manual", "worker-a")

        self.assertIsNotNone(task)
        assert task is not None
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/worker/tasks/task-manual/claim"))
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"worker_id": "worker-a"})
        self.assertEqual(worker_module._task_claim_context("task-manual")["claim_id"], "claim-manual")
        self.assertEqual(task["_worker_payload"]["worker_queue"]["claim_id"], "claim-manual")

    @staticmethod
    def _two_vehicle_task(task_id: str = "task-two-vehicle") -> dict[str, object]:
        return {
            "task_id": task_id,
            "created_at": "2026-07-13T08:00:00",
            "case_id": "20260713080500001",
            "case_date": "2026-07-13",
            "case_time": "0805",
            "two_vehicle": True,
            "vehicle": "新坡92",
            "vehicle_entries": [
                {
                    "vehicle": "新坡92",
                    "driver": "甲",
                    "mileage": "101",
                    "return_time": "0900",
                    "patient_summary": "2人",
                    "consumables": {"桃-口罩(片)": 2},
                },
                {
                    "vehicle": "新坡93",
                    "driver": "乙",
                    "mileage": "202",
                    "return_time": "0910",
                    "patient_summary": "3人",
                    "consumables": {"桃-9吋手套-L(雙)": 3},
                },
            ],
        }

    @staticmethod
    def _two_vehicle_fuel_task(task_id: str = "task-two-fuel") -> dict[str, object]:
        task = WorkerTests._two_vehicle_task(task_id)
        task["vehicle_entries"][0]["fuel_record"] = {
            "enabled": True,
            "date": "20260713",
            "time": "0915",
            "driver": "甲",
            "product": "超級柴油",
            "quantity": "20.1",
            "unit_price": "30.0",
        }
        task["vehicle_entries"][1]["fuel_record"] = {
            "enabled": True,
            "date": "20260713",
            "time": "0920",
            "driver": "乙",
            "product": "超級柴油",
            "quantity": "30.2",
            "unit_price": "30.0",
        }
        return task

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

    def test_manual_task_lock_same_owner_refresh_preserves_start_time(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "ambulance_bot.manual_task_lock.time.time", side_effect=[100.0, 200.0]
        ):
            artifacts_dir = Path(tmp)
            set_manual_task_lock(artifacts_dir, "task-a")
            set_manual_task_lock(artifacts_dir, "task-a")
            payload = json.loads(manual_task_lock_path(artifacts_dir).read_text(encoding="utf-8"))

        self.assertEqual(payload["owner"], "task-a")
        self.assertEqual(payload["started_at"], 100.0)
        self.assertEqual(payload["heartbeat_at"], 200.0)

    def test_manual_task_lock_atomic_acquire_refuses_second_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)

            self.assertTrue(acquire_manual_task_lock(artifacts_dir, "manual-a"))
            self.assertFalse(acquire_manual_task_lock(artifacts_dir, "manual-b"))
            clear_manual_task_lock(artifacts_dir, "manual-a")
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, "manual-b"))

    def test_old_owner_heartbeat_and_clear_cannot_replace_or_delete_new_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, "worker-manual:task-a:1:1"))
            self.assertTrue(clear_manual_task_lock(artifacts_dir, "worker-manual:task-a:1:1"))
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, "worker-manual:task-b:2:2"))

            self.assertFalse(refresh_manual_task_lock(artifacts_dir, "worker-manual:task-a:1:1"))
            self.assertFalse(clear_manual_task_lock(artifacts_dir, "worker-manual:task-a:1:1"))
            self.assertEqual(manual_task_lock_owner(artifacts_dir), "worker-manual:task-b:2:2")

    def test_heartbeat_write_is_serialized_before_abort_and_new_owner_acquire(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            owner_a = "worker-manual:task-a:1:1"
            owner_b = "worker-manual:task-b:2:2"
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, owner_a))
            heartbeat_at_write = threading.Event()
            allow_heartbeat_write = threading.Event()
            b_finished = threading.Event()
            original_write = manual_task_lock_module._write_manual_task_lock

            def blocking_write(path, payload):
                if str(payload.get("owner") or "") == owner_a:
                    heartbeat_at_write.set()
                    self.assertTrue(allow_heartbeat_write.wait(1.0))
                return original_write(path, payload)

            results: dict[str, bool] = {}

            def heartbeat_a():
                results["heartbeat_a"] = refresh_manual_task_lock(artifacts_dir, owner_a)

            def abort_a_then_acquire_b():
                results["clear_a"] = clear_manual_task_lock(artifacts_dir, owner_a)
                results["acquire_b"] = acquire_manual_task_lock(artifacts_dir, owner_b)
                b_finished.set()

            with mock.patch.object(manual_task_lock_module, "_write_manual_task_lock", side_effect=blocking_write):
                heartbeat_thread = threading.Thread(target=heartbeat_a)
                replacement_thread = threading.Thread(target=abort_a_then_acquire_b)
                heartbeat_thread.start()
                self.assertTrue(heartbeat_at_write.wait(1.0))
                replacement_thread.start()
                self.assertFalse(b_finished.wait(0.05))
                allow_heartbeat_write.set()
                heartbeat_thread.join(1.0)
                replacement_thread.join(1.0)

            self.assertFalse(heartbeat_thread.is_alive())
            self.assertFalse(replacement_thread.is_alive())
            self.assertEqual(
                results,
                {"heartbeat_a": True, "clear_a": True, "acquire_b": True},
            )
            self.assertEqual(manual_task_lock_owner(artifacts_dir), owner_b)

    def test_stale_cleanup_and_same_owner_heartbeat_are_serialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            owner = "worker-manual:task-a:1:1"
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, owner))
            stale_time = time.time() - 700
            os.utime(manual_task_lock_path(artifacts_dir), (stale_time, stale_time))
            stale_snapshot_read = threading.Event()
            allow_stale_cleanup = threading.Event()
            original_active = manual_task_lock_module._manual_task_lock_payload_is_active

            def blocking_active(path, payload):
                if threading.current_thread().name == "stale-cleanup":
                    stale_snapshot_read.set()
                    self.assertTrue(allow_stale_cleanup.wait(1.0))
                return original_active(path, payload)

            results: dict[str, bool] = {}

            def clean_stale():
                results["active"] = manual_task_lock_active(artifacts_dir)

            def heartbeat_same_owner():
                results["heartbeat"] = refresh_manual_task_lock(artifacts_dir, owner)

            with mock.patch.object(
                manual_task_lock_module,
                "_manual_task_lock_payload_is_active",
                side_effect=blocking_active,
            ):
                cleanup_thread = threading.Thread(target=clean_stale, name="stale-cleanup")
                heartbeat_thread = threading.Thread(target=heartbeat_same_owner, name="same-owner-heartbeat")
                cleanup_thread.start()
                self.assertTrue(stale_snapshot_read.wait(1.0))
                heartbeat_thread.start()
                allow_stale_cleanup.set()
                cleanup_thread.join(1.0)
                heartbeat_thread.join(1.0)

            self.assertEqual(results, {"active": False, "heartbeat": False})
            self.assertFalse(manual_task_lock_path(artifacts_dir).exists())

    def test_same_owner_heartbeat_can_refresh_a_stale_lease_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            owner = "worker-manual:task-a:1:1"
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, owner))
            stale_time = time.time() - 700
            os.utime(manual_task_lock_path(artifacts_dir), (stale_time, stale_time))

            self.assertTrue(refresh_manual_task_lock(artifacts_dir, owner))
            self.assertTrue(manual_task_lock_active(artifacts_dir))
            self.assertEqual(manual_task_lock_owner(artifacts_dir), owner)

    def test_guard_timeout_is_fail_closed_for_active_check_and_acquire(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"MANUAL_TASK_LOCK_GUARD_TIMEOUT_SECONDS": "0.05"},
        ):
            artifacts_dir = Path(tmp)
            guard_held = threading.Event()
            release_guard = threading.Event()

            def hold_guard():
                with manual_task_lock_module._manual_task_lock_guard(artifacts_dir):
                    guard_held.set()
                    self.assertTrue(release_guard.wait(1.0))

            holder = threading.Thread(target=hold_guard)
            holder.start()
            self.assertTrue(guard_held.wait(1.0))
            try:
                self.assertTrue(manual_task_lock_active(artifacts_dir))
                self.assertFalse(acquire_manual_task_lock(artifacts_dir, "worker-manual:task-b:2:2"))
            finally:
                release_guard.set()
                holder.join(1.0)

    def test_manual_task_lock_read_error_is_fail_closed_and_cannot_be_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            owner_a = "worker-manual:task-read-error:owner-a"
            owner_b = "worker-manual:task-read-error:owner-b"
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, owner_a))

            with mock.patch.object(Path, "read_text", side_effect=PermissionError("blocked")):
                self.assertTrue(manual_task_lock_active(artifacts_dir))
                self.assertFalse(acquire_manual_task_lock(artifacts_dir, owner_b))

            self.assertEqual(manual_task_lock_owner(artifacts_dir), owner_a)
            self.assertTrue(clear_manual_task_lock(artifacts_dir, owner_a))

    def test_corrupt_manual_task_lock_recovers_only_after_stale_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            path = manual_task_lock_path(artifacts_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{broken-json", encoding="utf-8")
            owner_b = "worker-manual:task-corrupt-lock:owner-b"

            self.assertTrue(manual_task_lock_active(artifacts_dir))
            self.assertFalse(acquire_manual_task_lock(artifacts_dir, owner_b))
            old_time = time.time() - 601
            os.utime(path, (old_time, old_time))

            self.assertTrue(acquire_manual_task_lock(artifacts_dir, owner_b))
            self.assertEqual(manual_task_lock_owner(artifacts_dir), owner_b)
            self.assertTrue(clear_manual_task_lock(artifacts_dir, owner_b))

    def test_refresh_guard_timeout_is_retryable_error_not_owner_loss(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"MANUAL_TASK_LOCK_GUARD_TIMEOUT_SECONDS": "0.05"},
        ):
            artifacts_dir = Path(tmp)
            owner = "worker-manual:task-refresh-timeout:owner"
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, owner))
            guard_held = threading.Event()
            release_guard = threading.Event()

            def hold_guard():
                with manual_task_lock_module._manual_task_lock_guard(artifacts_dir):
                    guard_held.set()
                    self.assertTrue(release_guard.wait(1.0))

            holder = threading.Thread(target=hold_guard)
            holder.start()
            self.assertTrue(guard_held.wait(1.0))
            try:
                with self.assertRaises(TimeoutError):
                    refresh_manual_task_lock(artifacts_dir, owner)
            finally:
                release_guard.set()
                holder.join(1.0)
                clear_manual_task_lock(artifacts_dir, owner)

    def test_owned_action_oserror_propagates_instead_of_looking_like_owner_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            task_id = "task-action-error"
            owner = f"worker-manual:{task_id}:owner"
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, owner))

            with self.assertRaisesRegex(OSError, "store write failed"):
                run_with_manual_task_lock_owner(
                    artifacts_dir,
                    owner,
                    task_id,
                    lambda: (_ for _ in ()).throw(OSError("store write failed")),
                )

            self.assertEqual(manual_task_lock_owner(artifacts_dir), owner)
            self.assertTrue(clear_manual_task_lock(artifacts_dir, owner))

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

    def test_remote_update_blocks_other_work_without_relaunching_updating_command(self):
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
                active_update_check=lambda _request_id: True,
            )

        self.assertTrue(started)
        self.assertEqual(launches, [])
        self.assertEqual(statuses, [])

    def test_remote_update_marks_orphaned_updating_command_failed_and_keeps_worker_available(self):
        launches: list[str] = []
        statuses: list[tuple[str, str, str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            started = worker_module.maybe_run_remote_update(
                "http://nas",
                "PC-01",
                Path(tmp),
                fetch_command=lambda *_: {"request_id": "update-orphaned", "status": "updating"},
                post_command_status=lambda *args, **_kwargs: statuses.append(args),
                launch_update=lambda request_id: launches.append(request_id),
                active_update_check=lambda _request_id: False,
            )

        self.assertFalse(started)
        self.assertEqual(launches, [])
        self.assertEqual(statuses[-1][2], "failed")
        self.assertIn("中斷", statuses[-1][3])

    def test_remote_update_active_marker_requires_matching_request_and_live_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
                path = worker_module.remote_update_active_path()
                path.parent.mkdir(parents=True)
                path.write_text(
                    json.dumps(
                        {
                            "request_id": "update-live",
                            "owner_pid": os.getpid(),
                            "owner_nonce": "wrapper-run",
                            "owner_started_unix_ms": worker_module.process_start_unix_ms(os.getpid()),
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertTrue(worker_module.remote_update_wrapper_is_active("update-live"))
                self.assertFalse(worker_module.remote_update_wrapper_is_active("update-other"))
                path.write_text(
                    json.dumps(
                        {
                            "request_id": "update-live",
                            "owner_pid": 999999,
                            "owner_nonce": "wrapper-run",
                            "owner_started_unix_ms": 0,
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertFalse(worker_module.remote_update_wrapper_is_active("update-live"))

    def test_remote_update_active_marker_rejects_valid_json_list(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            path = worker_module.remote_update_active_path()
            path.parent.mkdir(parents=True)
            path.write_text("[]", encoding="utf-8")

            self.assertFalse(worker_module.remote_update_wrapper_is_active("update-list"))

    def test_remote_update_active_marker_rejects_wrong_process_start_with_tight_fence(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ), mock.patch.object(
            worker_module,
            "process_id_is_running",
            return_value=True,
        ), mock.patch.object(
            worker_module,
            "process_start_unix_ms",
            return_value=1_000_000,
        ):
            path = worker_module.remote_update_active_path()
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "request_id": "update-wrong-start",
                        "owner_pid": 123,
                        "owner_nonce": "wrapper-run",
                        "owner_started_unix_ms": 1_000_100,
                    }
                ),
                encoding="utf-8",
            )

            self.assertFalse(worker_module.remote_update_wrapper_is_active("update-wrong-start"))

    def test_remote_update_active_marker_rejects_stale_mtime(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ), mock.patch.object(
            worker_module,
            "process_id_is_running",
            return_value=True,
        ), mock.patch.object(
            worker_module,
            "process_start_unix_ms",
            return_value=1_000_000,
        ):
            path = worker_module.remote_update_active_path()
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "request_id": "update-stale",
                        "owner_pid": 123,
                        "owner_nonce": "wrapper-run",
                        "owner_started_unix_ms": 1_000_000,
                    }
                ),
                encoding="utf-8",
            )
            stale_time = time.time() - 3601
            os.utime(path, (stale_time, stale_time))

            self.assertFalse(worker_module.remote_update_wrapper_is_active("update-stale"))

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
        self.assertIn("-CallerRuntime", args)

    def test_update_probe_blocks_all_worker_work_until_transaction_is_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            state_root = Path(tmp) / "state"
            root.mkdir()
            (root / "VERSION.txt").write_text("2026.07.13.2000", encoding="utf-8")
            transaction_dir = state_root / "AmbulanceReturnBot" / "update_transactions"
            transaction_dir.mkdir(parents=True)
            transaction_path = transaction_dir / f"{worker_module.package_update_identity(root)}-probe.json"
            transaction_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "phase": "prepared",
                        "package_id": worker_module.package_update_identity(root),
                        "package_dir": str(root.resolve()),
                        "new_version": "2026.07.13.2000",
                        "owner_pid": os.getpid(),
                        "owner_nonce": "probe-owner",
                        "owner_heartbeat_path": f"{transaction_path.resolve()}.owner.heartbeat",
                    }
                ),
                encoding="utf-8",
            )
            Path(f"{transaction_path}.owner.heartbeat").write_text(
                json.dumps({"owner_pid": os.getpid(), "owner_nonce": "probe-owner"}),
                encoding="utf-8",
            )
            result: list[bool] = []
            with mock.patch.dict(
                os.environ,
                {
                    "LOCALAPPDATA": str(state_root),
                    "AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH": str(transaction_path),
                    "WORKER_RUNTIME_MODE": "headless",
                },
                clear=False,
            ):
                thread = threading.Thread(
                    target=lambda: result.append(worker_module.wait_for_update_probe_gate(package_dir=root)),
                    daemon=True,
                )
                thread.start()
                ready_path = Path(f"{transaction_path}.probe-{os.getpid()}.ready")
                deadline = time.time() + 3
                while not ready_path.exists() and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready_path.is_file())
                self.assertTrue(thread.is_alive())
                transaction_path.unlink()
                thread.join(timeout=3)

            self.assertEqual(result, ["committed"])
            self.assertFalse(ready_path.exists())

    def test_interrupted_update_is_discovered_by_package_identity_and_recovery_wrapper_is_launched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            state_root = Path(tmp) / "state"
            root.mkdir()
            transaction_dir = state_root / "AmbulanceReturnBot" / "update_transactions"
            transaction_dir.mkdir(parents=True)
            transaction_path = transaction_dir / f"{worker_module.package_update_identity(root)}-recovery.json"
            transaction_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "phase": "prepared",
                        "package_id": worker_module.package_update_identity(root),
                        "package_dir": str(root.resolve()),
                        "request_id": "update-interrupted",
                        "owner_pid": 0,
                        "owner_nonce": "",
                        "owner_heartbeat_path": f"{transaction_path.resolve()}.owner.heartbeat",
                    }
                ),
                encoding="utf-8",
            )
            launches: list[tuple[tuple[object, ...], dict[str, object]]] = []

            recovered = worker_module.maybe_recover_interrupted_update(
                package_dir=root,
                state_root=state_root,
                launch_update=lambda *args, **kwargs: launches.append((args, kwargs)),
            )

            self.assertTrue(recovered)
            self.assertEqual(launches[0][0], ("update-interrupted",))
            self.assertEqual(launches[0][1]["recover_transaction_path"], transaction_path)

    def test_orphaned_update_probe_launches_rollback_instead_of_waiting_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            state_root = Path(tmp) / "state"
            root.mkdir()
            (root / "VERSION.txt").write_text("2026.07.13.2000", encoding="utf-8")
            transaction_dir = state_root / "AmbulanceReturnBot" / "update_transactions"
            transaction_dir.mkdir(parents=True)
            transaction_path = transaction_dir / f"{worker_module.package_update_identity(root)}-orphan.json"
            transaction_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "phase": "prepared",
                        "package_id": worker_module.package_update_identity(root),
                        "package_dir": str(root.resolve()),
                        "new_version": "2026.07.13.2000",
                        "request_id": "update-orphaned",
                        "owner_pid": 999999,
                        "owner_nonce": "dead-owner",
                        "owner_heartbeat_path": f"{transaction_path.resolve()}.owner.heartbeat",
                    }
                ),
                encoding="utf-8",
            )
            launches: list[tuple[tuple[object, ...], dict[str, object]]] = []
            with mock.patch.dict(
                os.environ,
                {
                    "LOCALAPPDATA": str(state_root),
                    "AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH": str(transaction_path),
                    "WORKER_RUNTIME_MODE": "headless",
                },
                clear=False,
            ):
                outcome = worker_module.wait_for_update_probe_gate(
                    package_dir=root,
                    heartbeat_timeout_seconds=0,
                    launch_update=lambda *args, **kwargs: launches.append((args, kwargs)),
                )

            self.assertEqual(outcome, "recovery")
            self.assertEqual(launches[0][0], ("update-orphaned",))
            self.assertEqual(launches[0][1]["recover_transaction_path"], transaction_path.resolve())

    def test_multiple_interrupted_update_transactions_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            state_root = Path(tmp) / "state"
            root.mkdir()
            transaction_dir = state_root / "AmbulanceReturnBot" / "update_transactions"
            transaction_dir.mkdir(parents=True)
            prefix = worker_module.package_update_identity(root)
            for suffix in ("one", "two"):
                (transaction_dir / f"{prefix}-{suffix}.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "multiple pending update transactions"):
                worker_module.find_pending_update_transaction(package_dir=root, state_root=state_root)

    def test_update_state_root_matches_powershell_temp_fallback_without_localappdata(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": "", "TEMP": tmp}, clear=False):
                self.assertEqual(worker_module.update_state_root(), Path(tmp))

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

    def test_report_remote_update_result_marks_stale_404_without_retrying(self):
        attempts: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                path = worker_module.remote_update_result_path()
                worker_module.write_json_atomic(
                    path,
                    {
                        "request_id": "expired-update",
                        "status": "failed",
                        "detail": "old result",
                        "exit_code": 1,
                    },
                )

                def reject_stale(_server_url, request_id, *_args, **_kwargs):
                    attempts.append(request_id)
                    raise RuntimeError("NAS worker API 回應 HTTP 404：NOT FOUND")

                first = worker_module.report_remote_update_result(
                    "http://nas",
                    "PC-01",
                    post_command_status=reject_stale,
                    reported_at=lambda: "2026-07-11T16:00:00",
                )
                second = worker_module.report_remote_update_result(
                    "http://nas",
                    "PC-01",
                    post_command_status=reject_stale,
                    reported_at=lambda: "2026-07-11T16:01:00",
                )
                saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(attempts, ["expired-update"])
        self.assertEqual(saved["reported_at"], "2026-07-11T16:00:00")
        self.assertIn("HTTP 404", saved["report_error"])

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

    def test_remote_update_wrapper_writes_result_before_restarting_worker_runtime(self):
        package_dir = Path(worker_module.__file__).parent
        wrapper_source = (package_dir / "REMOTE_UPDATE_PACKAGE.ps1").read_text(encoding="utf-8")

        self.assertIn("AMBULANCE_SKIP_WORKER_RESTART", wrapper_source)
        self.assertIn("RUN_WORKER_GUI_WINPYTHON.vbs", wrapper_source)
        self.assertIn("function Start-WorkerGui", wrapper_source)
        self.assertIn("function Start-WorkerHeadless", wrapper_source)
        self.assertIn("Remove-Item Env:AMBULANCE_SKIP_WORKER_RESTART", wrapper_source)
        restart_call = wrapper_source.rindex("Restart-WorkerRuntimes -StartGui")
        self.assertLess(wrapper_source.index("Move-Item"), restart_call)
        self.assertLess(
            wrapper_source.index("Remove-Item Env:AMBULANCE_SKIP_WORKER_RESTART"),
            restart_call,
        )

    def test_updaters_detect_headless_worker_only_inside_current_package(self):
        package_dir = Path(worker_module.__file__).parent
        for name in ("update_package.ps1", "REMOTE_UPDATE_PACKAGE.ps1"):
            source = (package_dir / name).read_text(encoding="utf-8-sig")
            function_source = source[
                source.index("function Get-WorkerPackageProcesses") : source.index(
                    "function ",
                    source.index("function Get-WorkerPackageProcesses") + 10,
                )
            ]

            self.assertIn('(?:worker_gui|worker|app)\\.py', function_source)
            self.assertIn('(?<![A-Za-z0-9_])', function_source)
            self.assertIn("IndexOf($packagePath", function_source)
            self.assertIn("TrimEnd([char]92) + [string][char]92", function_source)
            self.assertNotIn('ambulance_return_bot|WinPython_', function_source)

    def test_updaters_restore_the_exact_preupdate_runtime_modes_and_verify_health(self):
        package_dir = Path(worker_module.__file__).parent
        for name in ("update_package.ps1", "REMOTE_UPDATE_PACKAGE.ps1"):
            with self.subTest(name=name):
                source = (package_dir / name).read_text(encoding="utf-8-sig")
                self.assertIn("function Get-WorkerRuntimeState", source)
                self.assertIn("function Start-WorkerHeadless", source)
                self.assertIn("function Restart-WorkerRuntimes", source)
                self.assertIn("function Wait-WorkerRuntime", source)
                self.assertIn("$workerGuiWasRunning", source)
                self.assertIn("$workerHeadlessWasRunning", source)
                self.assertIn("run_worker_headless.bat", source)
                self.assertIn("Worker runtime health check timed out", source)
                self.assertIn("$readySince", source)
                self.assertIn("TotalSeconds -ge 2", source)

    def test_headless_launcher_uses_absolute_script_and_legacy_parent_tree_is_detectable(self):
        package_dir = Path(worker_module.__file__).parent
        launcher = (package_dir / "run_worker_headless.bat").read_text(encoding="ascii")

        self.assertIn('"%~dp0worker.py"', launcher)
        for name in ("update_package.ps1", "REMOTE_UPDATE_PACKAGE.ps1"):
            with self.subTest(name=name):
                source = (package_dir / name).read_text(encoding="utf-8-sig")
                function_source = source[
                    source.index("function Get-WorkerPackageProcesses") : source.index(
                        "function ", source.index("function Get-WorkerPackageProcesses") + 10
                    )
                ]
                self.assertIn("run_worker_headless\\.bat", function_source)
                self.assertIn("ParentProcessId", function_source)
                self.assertIn("$headlessLauncherIds", function_source)

    def test_manual_updater_uses_only_targeted_temporary_rollback(self):
        source = Path(worker_module.__file__).with_name("update_package.ps1").read_text(encoding="utf-8-sig")

        self.assertIn('$rollbackDir = Join-Path $tempDir "rollback"', source)
        self.assertIn("Backup-UpdateTree", source)
        self.assertIn("Restore-UpdateTree", source)
        self.assertNotIn("update_backups", source)
        self.assertNotRegex(source, r"(?i)Compress-Archive[^\r\n]*\$packageDir")

    def test_updater_preserves_recovery_on_incomplete_rollback_and_serializes_updates(self):
        package_dir = Path(worker_module.__file__).parent
        updater = (package_dir / "update_package.ps1").read_text(encoding="utf-8-sig")
        wrapper = (package_dir / "REMOTE_UPDATE_PACKAGE.ps1").read_text(encoding="utf-8-sig")

        self.assertIn("$rollbackComplete", updater)
        self.assertIn("$replacementAttempted", updater)
        self.assertIn("Recovery files:", updater)
        self.assertIn("rollback was incomplete", updater)
        self.assertRegex(updater, r"(?s)if \(\$preserveRecovery\).*Remove-Item -LiteralPath \$tempDir")
        for source in (updater, wrapper):
            self.assertIn("package-update.lock", source)
            self.assertIn("FileShare]::None", source)
            self.assertIn("Another package update is already in progress", source)
        self.assertIn("AMBULANCE_UPDATE_LOCK_HELD", wrapper)
        self.assertIn("AMBULANCE_UPDATE_LOCK_HELD", updater)

    def test_remote_update_defers_commit_until_runtime_health_and_can_roll_back(self):
        package_dir = Path(worker_module.__file__).parent
        updater = (package_dir / "update_package.ps1").read_text(encoding="utf-8-sig")
        wrapper = (package_dir / "REMOTE_UPDATE_PACKAGE.ps1").read_text(encoding="utf-8-sig")

        for source in (updater, wrapper):
            self.assertIn("AMBULANCE_UPDATE_TRANSACTION_PATH", source)
            self.assertIn("AMBULANCE_UPDATE_TRANSACTION_ACTION", source)
        self.assertIn("Write-DeferredUpdateTransaction", updater)
        self.assertIn("Invoke-DeferredUpdateRollback", updater)
        self.assertIn("Complete-DeferredUpdateTransaction", updater)
        self.assertIn("$preserveDeferredCommit", updater)
        self.assertIn('Invoke-UpdateTransactionAction -Action "rollback"', wrapper)
        self.assertIn('Invoke-UpdateTransactionAction -Action "finalize"', wrapper)
        self.assertLess(wrapper.index("Write-RemoteUpdateResult"), wrapper.rindex("Restart-WorkerRuntimes -StartGui"))

    def test_update_transaction_is_durable_isolated_and_recoverable_before_any_version_check(self):
        package_dir = Path(worker_module.__file__).parent
        updater = (package_dir / "update_package.ps1").read_text(encoding="utf-8-sig")
        wrapper = (package_dir / "REMOTE_UPDATE_PACKAGE.ps1").read_text(encoding="utf-8-sig")

        main_update = updater[updater.index("Expand-Archive") :]
        self.assertLess(main_update.index("Write-DeferredUpdateTransaction"), main_update.index("Stop-WorkerPackageProcesses -Processes"))
        updater_main = updater[updater.index("$updateLockStream = $null") :]
        self.assertLess(updater_main.index("Recover-PendingDeferredUpdate"), updater_main.index("Resolve-RemoteDownloadUrls"))
        self.assertIn("Assert-RollbackBackupSet", updater)
        self.assertIn("backed_up_files", updater)
        self.assertIn("Rollback backup hash mismatch", updater)
        self.assertIn("$stream.Flush($true)", updater)
        self.assertNotIn("$packageVersion | Set-Content", updater)
        for source in (updater, wrapper):
            self.assertIn("Suspend-UpdateControlEnvironmentForProbe", source)
            self.assertIn("AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH", source)
            self.assertIn("Write-UpdateOwnerHeartbeat", source)
            self.assertIn("ExcludedProcessIds", source)
        self.assertIn("$ownsUpdateLock", wrapper)
        self.assertRegex(wrapper, r"(?s)if \(\$ownsUpdateLock.*Restart-WorkerRuntimesFresh")
        self.assertIn("$runtimePackageSafe = $true", wrapper)
        self.assertRegex(wrapper, r"(?s)Invoke-UpdateTransactionAction -Action \"rollback\".*?\$runtimePackageSafe = \$true")
        self.assertIn("$noPendingTransaction", wrapper)
        self.assertRegex(wrapper, r"\$noPendingTransaction -and \$installedVersion -eq \$beforeVersion")
        self.assertIn("$runtimePackageSafe = (-not $replacementAttempted) -or $updateCommitted -or $rollbackComplete", updater)
        self.assertIn("-not $restartManagedByCaller -and $runtimePackageSafe", updater)

    def test_updater_fails_closed_on_directory_file_collision_and_verifies_installed_tree(self):
        updater = Path(worker_module.__file__).with_name("update_package.ps1").read_text(encoding="utf-8-sig")

        self.assertIn("Update target exists but is not a file", updater)
        self.assertIn("Restore target exists but is not a file", updater)
        self.assertIn("function Assert-InstalledUpdateTree", updater)
        self.assertIn("Installed update hash mismatch", updater)
        self.assertIn("Obsolete managed file still exists", updater)

    def test_manifest_removal_is_bounded_protected_and_rollback_aware(self):
        package_dir = Path(worker_module.__file__).parent
        updater = (package_dir / "update_package.ps1").read_text(encoding="utf-8-sig")
        builder = (package_dir.parent / "scripts" / "build_public_duty_package.ps1").read_text(encoding="utf-8")

        self.assertIn('$manifestName = "UPDATE_MANIFEST.json"', updater)
        self.assertIn("function Read-UpdateManifest", updater)
        self.assertIn("function Get-ObsoleteManagedPaths", updater)
        self.assertIn("function Remove-ManagedFiles", updater)
        self.assertIn("$rollbackPaths", updater)
        self.assertIn("$newManagedPaths", updater)
        self.assertIn("$obsoleteManagedPaths", updater)
        self.assertIn("Test-ProtectedUpdatePath", updater)
        for protected in ('.env', 'artifacts', 'local_data', 'logs', 'chrome_profile'):
            self.assertIn(protected, updater)
        self.assertRegex(updater, r"(?s)if \(-not \(Test-Path -LiteralPath \$manifestPath.*?return @\(\)")
        self.assertIn('$manifestName = "UPDATE_MANIFEST.json"', builder)
        self.assertIn("function Write-UpdateManifest", builder)
        self.assertIn('".git"', builder)
        self.assertLess(builder.index("Write-UpdateManifest"), builder.index("Compress-Archive"))

    def test_public_env_template_does_not_publish_a_profile_account(self):
        env_source = Path(worker_module.__file__).with_name(".env.example").read_text(encoding="utf-8")

        self.assertIn("CHROME_PROFILE_EMAIL=", env_source)
        self.assertNotRegex(env_source, r"(?m)^CHROME_PROFILE_EMAIL=.+$")

    def test_existing_updater_can_skip_restart_only_for_remote_wrapper(self):
        package_dir = Path(worker_module.__file__).parent
        updater_source = (package_dir / "update_package.ps1").read_text(encoding="utf-8-sig")
        build_source = (package_dir.parent / "scripts" / "build_public_duty_package.ps1").read_text(encoding="utf-8")

        for source in (updater_source, build_source):
            self.assertIn("AMBULANCE_SKIP_WORKER_RESTART", source)
            self.assertIn("Start-WorkerGui", source)

    def test_remote_update_idle_setting_is_documented_for_public_package(self):
        env_example = Path(worker_module.__file__).with_name(".env.example").read_text(encoding="utf-8")

        self.assertIn("REMOTE_UPDATE_IDLE_SECONDS=120", env_example)

    def test_main_checks_remote_update_after_confirming_no_pending_work(self):
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

        self.assertEqual(calls, ["result", "credential", "lookup", "remote"])

    def test_main_runs_pending_nas_work_before_remote_update(self):
        env_keys = ["WORKER_RUN_ONCE", "WORKER_AUTO_CLAIM_TASKS"]
        previous_env = {key: worker_module.os.environ.get(key) for key in env_keys}
        originals = {
            "report": worker_module.report_remote_update_result,
            "remote": worker_module.maybe_run_remote_update,
            "sync": worker_module.maybe_run_credential_sync,
            "lookup": worker_module.maybe_run_case_lookup,
            "fetch": worker_module.fetch_next_task,
            "run": worker_module.run_all_sites_task,
        }
        calls: list[str] = []
        try:
            worker_module.os.environ["WORKER_RUN_ONCE"] = "true"
            worker_module.os.environ["WORKER_AUTO_CLAIM_TASKS"] = "true"
            worker_module.report_remote_update_result = lambda *_args, **_kwargs: calls.append("result") or False
            worker_module.maybe_run_remote_update = lambda *_args, **_kwargs: calls.append("remote") or True
            worker_module.maybe_run_credential_sync = lambda *_args, **_kwargs: calls.append("credential")
            worker_module.maybe_run_case_lookup = (
                lambda _server_url, _artifacts_dir, last_lookup_at, last_case_hash, _interval_seconds:
                calls.append("lookup") or (last_lookup_at, last_case_hash)
            )
            worker_module.fetch_next_task = lambda *_args, **_kwargs: calls.append("claim") or {"task_id": "priority-task"}
            worker_module.run_all_sites_task = lambda *_args, **_kwargs: calls.append("run")

            worker_module.main()
        finally:
            worker_module.report_remote_update_result = originals["report"]
            worker_module.maybe_run_remote_update = originals["remote"]
            worker_module.maybe_run_credential_sync = originals["sync"]
            worker_module.maybe_run_case_lookup = originals["lookup"]
            worker_module.fetch_next_task = originals["fetch"]
            worker_module.run_all_sites_task = originals["run"]
            for key, value in previous_env.items():
                if value is None:
                    worker_module.os.environ.pop(key, None)
                else:
                    worker_module.os.environ[key] = value

        self.assertEqual(calls, ["result", "credential", "lookup", "claim", "run"])

    def test_main_does_not_claim_auto_task_while_manual_execution_is_active(self):
        env_keys = ["WORKER_RUN_ONCE", "WORKER_AUTO_CLAIM_TASKS"]
        previous_env = {key: worker_module.os.environ.get(key) for key in env_keys}
        worker_module.MANUAL_TASK_ACTIVE.set()
        try:
            worker_module.os.environ["WORKER_RUN_ONCE"] = "true"
            worker_module.os.environ["WORKER_AUTO_CLAIM_TASKS"] = "true"
            with mock.patch.object(worker_module, "flush_status_outbox"), mock.patch.object(
                worker_module,
                "report_remote_update_result",
                return_value=False,
            ), mock.patch.object(worker_module, "maybe_run_credential_sync"), mock.patch.object(
                worker_module,
                "maybe_run_case_lookup",
                side_effect=lambda _server, _artifacts, last_at, last_hash, _interval: (last_at, last_hash),
            ), mock.patch.object(worker_module, "fetch_next_task") as fetch, mock.patch.object(
                worker_module,
                "maybe_run_remote_update",
                return_value=False,
            ):
                worker_module.main()

            fetch.assert_not_called()
        finally:
            worker_module.MANUAL_TASK_ACTIVE.clear()
            for key, value in previous_env.items():
                if value is None:
                    worker_module.os.environ.pop(key, None)
                else:
                    worker_module.os.environ[key] = value

    def test_main_continues_work_when_result_reporting_temporarily_fails(self):
        env_keys = ["WORKER_RUN_ONCE", "WORKER_AUTO_CLAIM_TASKS"]
        previous_env = {key: worker_module.os.environ.get(key) for key in env_keys}
        originals = {
            "report": worker_module.report_remote_update_result,
            "remote": worker_module.maybe_run_remote_update,
            "sync": worker_module.maybe_run_credential_sync,
            "lookup": worker_module.maybe_run_case_lookup,
        }
        calls: list[str] = []
        try:
            worker_module.os.environ["WORKER_RUN_ONCE"] = "true"
            worker_module.os.environ["WORKER_AUTO_CLAIM_TASKS"] = "false"
            worker_module.report_remote_update_result = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("NAS offline")
            )
            worker_module.maybe_run_remote_update = lambda *_args, **_kwargs: calls.append("remote") or False
            worker_module.maybe_run_credential_sync = lambda *_args, **_kwargs: calls.append("credential")
            worker_module.maybe_run_case_lookup = (
                lambda _server_url, _artifacts_dir, last_lookup_at, last_case_hash, _interval_seconds:
                calls.append("lookup") or (last_lookup_at, last_case_hash)
            )

            worker_module.main()
        finally:
            worker_module.report_remote_update_result = originals["report"]
            worker_module.maybe_run_remote_update = originals["remote"]
            worker_module.maybe_run_credential_sync = originals["sync"]
            worker_module.maybe_run_case_lookup = originals["lookup"]
            for key, value in previous_env.items():
                if value is None:
                    worker_module.os.environ.pop(key, None)
                else:
                    worker_module.os.environ[key] = value

        self.assertEqual(calls, ["credential", "lookup", "remote"])

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
        calls = {"posts": 0, "lookup_range": "", "request_id": ""}
        original_fetch = worker_module.fetch_case_lookup_request
        original_query = worker_module.query_duty_emergency_cases
        original_post = worker_module.post_cases
        try:
            cases = [{"case_id": "1"}]
            case_hash = worker_module.hash_cases(cases)
            worker_module.fetch_case_lookup_request = lambda server_url: {
                "lookup_range": "legacy-range",
                "request_id": "lookup-request-123",
            }
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
            def fake_post_cases(*args, **kwargs):
                calls["posts"] += 1
                calls["request_id"] = str(kwargs.get("request_id") or "")

            worker_module.post_cases = fake_post_cases

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
        self.assertEqual(calls["request_id"], "lookup-request-123")

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

    def test_fetch_credential_sync_request_opens_sealed_payload_only_in_memory(self):
        worker_token = "0123456789abcdef0123456789abcdef"
        payload = {
            "accounts": [
                {"actor_no": "8", "user_id": "user8", "password": "secret-pass"},
            ]
        }
        response = {
            "ok": True,
            "request": {
                "request_id": "sealed-sync-1",
                "sealed_payload": seal_credential_payload(payload, worker_token),
            },
        }

        with mock.patch.dict(os.environ, {"WORKER_TOKEN": worker_token}, clear=False), mock.patch.object(
            worker_module,
            "request_json",
            return_value=response,
        ):
            request_payload = worker_module.fetch_credential_sync_request("http://nas")

        self.assertIsNotNone(request_payload)
        assert request_payload is not None
        self.assertEqual(request_payload["payload"], payload)
        self.assertNotIn("sealed_payload", request_payload)

    def test_fetch_credential_sync_request_rejects_wrong_worker_token(self):
        sealed = seal_credential_payload(
            {"accounts": [{"actor_no": "8", "user_id": "user8", "password": "secret-pass"}]},
            "0123456789abcdef0123456789abcdef",
        )
        response = {
            "ok": True,
            "request": {"request_id": "sealed-sync-1", "sealed_payload": sealed},
        }

        with mock.patch.dict(
            os.environ,
            {"WORKER_TOKEN": "fedcba9876543210fedcba9876543210"},
            clear=False,
        ), mock.patch.object(worker_module, "request_json", return_value=response):
            with self.assertRaises(ValueError):
                worker_module.fetch_credential_sync_request("http://nas")

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

    def test_run_vehicle_executes_each_unsaved_vehicle_and_posts_vehicle_key(self):
        calls: list[str] = []
        posts: list[tuple[str, str]] = []
        with mock.patch.object(
            worker_module,
            "run_vehicle_mileage_task",
            side_effect=lambda request, *_args, **_kwargs: calls.append(request.vehicle)
            or SimpleNamespace(ok=True, status="vehicle_mileage_saved", detail=f"{request.vehicle} ok"),
        ), mock.patch.object(
            worker_module,
            "post_status",
            side_effect=lambda _server, _task_id, status, _detail, **kwargs: posts.append(
                (status, str(kwargs.get("vehicle_key") or ""))
            ),
        ):
            result = worker_module.run_vehicle_task(
                "http://nas",
                "worker-a",
                self._two_vehicle_task(),
                Path("artifacts"),
                vehicle_results={"新坡92": {"status": "vehicle_mileage_saved", "detail": "already done"}},
            )

        self.assertEqual(calls, ["新坡93"])
        self.assertEqual(result.status, "vehicle_mileage_saved")
        self.assertIn(("vehicle_mileage_saved", "新坡93"), posts)
        self.assertIn(("vehicle_mileage_saved", ""), posts)
        self.assertEqual(posts[-1], ("desktop_fast_completed", ""))

    def test_run_fuel_retries_only_failed_enabled_vehicle_and_posts_vehicle_checkpoint(self):
        task = self._two_vehicle_fuel_task()
        calls: list[str] = []
        vehicle_posts: list[tuple[str, str]] = []
        first_results: dict[str, dict[str, str]] = {}

        def first_attempt(request, *_args, **_kwargs):
            calls.append(request.vehicle)
            if request.vehicle == "新坡93":
                return SimpleNamespace(ok=False, status="fuel_record_failed", detail="save timeout")
            return SimpleNamespace(ok=True, status="fuel_record_saved", detail="saved")

        def capture_first(_server, _task_id, status, detail, **kwargs):
            vehicle_key = str(kwargs.get("vehicle_key") or "")
            if vehicle_key:
                first_results[vehicle_key] = {"status": status, "detail": detail}

        with mock.patch.object(worker_module, "run_fuel_record_task", side_effect=first_attempt), mock.patch.object(
            worker_module,
            "post_status",
            side_effect=capture_first,
        ):
            first = worker_module.run_fuel_worker_task(
                "http://nas",
                "worker-a",
                task,
                Path("artifacts"),
                update_overall=False,
            )

        self.assertEqual(first.status, "fuel_record_failed")
        self.assertEqual(calls, ["新坡92", "新坡93"])
        self.assertEqual(first_results["新坡92"]["status"], "fuel_record_saved")
        self.assertEqual(first_results["新坡93"]["status"], "fuel_record_failed")

        calls.clear()

        def retry_attempt(request, *_args, **_kwargs):
            calls.append(request.vehicle)
            return SimpleNamespace(ok=True, status="fuel_record_saved", detail="saved on retry")

        with mock.patch.object(worker_module, "run_fuel_record_task", side_effect=retry_attempt), mock.patch.object(
            worker_module,
            "post_status",
            side_effect=lambda _server, _task_id, status, _detail, **kwargs: vehicle_posts.append(
                (status, str(kwargs.get("vehicle_key") or ""))
            ),
        ):
            retried = worker_module.run_fuel_worker_task(
                "http://nas",
                "worker-a",
                task,
                Path("artifacts"),
                update_overall=False,
                vehicle_results=first_results,
            )

        self.assertEqual(calls, ["新坡93"])
        self.assertEqual(retried.status, "fuel_record_saved")
        self.assertIn(("fuel_record_saved", "新坡93"), vehicle_posts)
        self.assertNotIn(("fuel_record_saved", "新坡92"), vehicle_posts)

    def test_worker_vehicle_aggregate_prioritizes_waiting_confirmation_over_failure(self):
        request = AmbulanceReturnRequest.from_dict(self._two_vehicle_task())

        status = worker_module._aggregate_worker_vehicle_status(
            "vehicle_mileage",
            request.vehicle_requests(),
            {
                "新坡92": {"status": "vehicle_mileage_waiting_confirmation", "detail": "wait"},
                "新坡93": {"status": "vehicle_mileage_failed", "detail": "failed"},
            },
        )

        self.assertEqual(status, "vehicle_mileage_waiting_confirmation")

    def test_run_consumables_executes_both_vehicles_with_one_login(self):
        calls: list[str] = []
        posts: list[tuple[str, str]] = []
        driver = object()
        with mock.patch.object(worker_module, "login_acs_and_get_driver", return_value=driver) as login, mock.patch.object(
            worker_module,
            "open_consumable_record_for_task",
            side_effect=lambda actual_driver, request: calls.append(request.vehicle) or f"{request.vehicle} ok",
        ), mock.patch.object(worker_module, "save_consumables_record_enabled", return_value=True), mock.patch.object(
            worker_module,
            "post_status",
            side_effect=lambda _server, _task_id, status, _detail, **kwargs: posts.append(
                (status, str(kwargs.get("vehicle_key") or ""))
            ),
        ):
            result = worker_module.run_consumables_worker_task(
                "http://nas", "worker-a", self._two_vehicle_task(), Path("artifacts")
            )

        login.assert_called_once()
        self.assertEqual(calls, ["新坡92", "新坡93"])
        self.assertEqual(result.status, "consumables_saved")
        self.assertIn(("consumables_saved", "新坡92"), posts)
        self.assertIn(("consumables_saved", "新坡93"), posts)

    def test_run_disinfection_aggregates_partial_vehicle_failure(self):
        calls: list[str] = []
        posts: list[tuple[str, str]] = []

        def run(request, *_args, **_kwargs):
            calls.append(request.vehicle)
            if request.vehicle == "新坡93":
                return SimpleNamespace(ok=False, status="disinfection_failed", detail="vehicle mismatch")
            return SimpleNamespace(ok=True, status="disinfection_saved", detail="ok")

        with mock.patch.object(worker_module, "run_disinfection_task", side_effect=run), mock.patch.object(
            worker_module,
            "login_disinfection_and_get_driver",
            return_value=object(),
            create=True,
        ), mock.patch.object(
            worker_module,
            "post_status",
            side_effect=lambda _server, _task_id, status, _detail, **kwargs: posts.append(
                (status, str(kwargs.get("vehicle_key") or ""))
            ),
        ):
            result = worker_module.run_disinfection_worker_task(
                "http://nas", "worker-a", self._two_vehicle_task(), Path("artifacts")
            )

        self.assertEqual(calls, ["新坡92", "新坡93"])
        self.assertEqual(result.status, "disinfection_failed")
        self.assertIn(("disinfection_failed", ""), posts)
        self.assertEqual(posts[-1], ("desktop_fast_completed_with_errors", ""))

    def test_post_status_serializes_optional_vehicle_key_and_label(self):
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            worker_module.post_status(
                "http://nas",
                "task-two-vehicle",
                "vehicle_mileage_saved",
                "ok",
                site_key="vehicle_mileage",
                site_name="車輛里程",
                vehicle_key="新坡92",
                vehicle_label="新坡92",
            )

        self.assertEqual(captured["payload"]["vehicle_key"], "新坡92")
        self.assertEqual(captured["payload"]["vehicle_label"], "新坡92")

    def test_post_cases_serializes_manual_request_id_and_omits_it_for_scheduled_lookup(self):
        payloads: list[dict[str, object]] = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            worker_module.post_cases(
                "http://nas", "cases_loaded", "ok", "24h", [], "hash-1", request_id="lookup-request-123"
            )
            worker_module.post_cases("http://nas", "cases_loaded", "ok", "24h", [], "hash-2")

        self.assertEqual(payloads[0]["request_id"], "lookup-request-123")
        self.assertNotIn("request_id", payloads[1])

    def test_fetch_next_task_remembers_claim_context_for_all_status_posts(self):
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        response = {
            "ok": True,
            "task": {"task_id": "claimed-task", "created_at": "2026-07-13T08:00:00"},
            "worker_queue": {"claim_id": "claim-2", "worker_id": "PC-01"},
        }
        with mock.patch.object(worker_module, "request_json", return_value=response):
            task = worker_module.fetch_next_task("http://nas", "PC-01")
        self.assertIsNotNone(task)

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            worker_module.post_status("http://nas", "claimed-task", "vehicle_mileage_running", "running")

        self.assertEqual(captured["payload"]["claim_id"], "claim-2")
        self.assertEqual(captured["payload"]["worker_id"], "PC-01")

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

        self.assertCountEqual(calls, ["duty_work_log", "vehicle_mileage", "consumables", "disinfection"])
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
        self.assertIn("四站流程已停止", statuses[-1][1])

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

    def test_stale_claim_signal_stops_parallel_run_instead_of_becoming_site_failure(self):
        stale_error = getattr(worker_module, "StaleWorkerClaimError", None)
        self.assertIsNotNone(stale_error, "worker.StaleWorkerClaimError is missing")
        task = {"task_id": "stale-task", "created_at": "2026-07-13T08:00:00"}
        with mock.patch.object(worker_module, "post_status"), mock.patch.object(
            worker_module, "fetch_task_payload", return_value={"task": task, "site_statuses": {}}
        ), mock.patch.object(
            worker_module, "_run_worker_site_group", side_effect=stale_error("stale-task")
        ):
            with self.assertRaises(stale_error):
                worker_module.run_all_sites_task("http://nas", "worker-a", task, Path("artifacts"))

    def test_stale_claim_cancels_already_running_group_before_protected_side_effect(self):
        task = {"task_id": "stale-running-task", "created_at": "2026-07-13T08:00:00"}
        queue = {"status": "claimed", "claim_id": "claim-a", "worker_id": "worker-a"}
        worker_module._remember_task_claim(task, queue, fallback_worker_id="worker-a")
        vehicle_started = threading.Event()
        stale_marked = threading.Event()
        protected_side_effects: list[str] = []

        def stale_work(*_args, **_kwargs):
            self.assertTrue(vehicle_started.wait(1.0))
            worker_module._mark_task_claim_stale(task["task_id"], "claim-a")
            stale_marked.set()
            raise worker_module.StaleWorkerClaimError(task["task_id"])

        def already_running_vehicle(*_args, **kwargs):
            vehicle_started.set()
            self.assertTrue(stale_marked.wait(1.0))
            cancel_check = kwargs.get("cancel_check")
            if cancel_check is not None:
                cancel_check()
            protected_side_effects.append("vehicle_mileage_save")
            return SimpleNamespace(ok=True, status="vehicle_mileage_saved", detail="saved")

        payload = {
            "task": task,
            "worker_queue": queue,
            "site_statuses": {},
        }
        with mock.patch.object(worker_module, "post_status"), mock.patch.object(
            worker_module,
            "fetch_task_payload",
            return_value=payload,
        ), mock.patch.object(worker_module, "run_task", side_effect=stale_work), mock.patch.object(
            worker_module,
            "run_vehicle_task",
            side_effect=already_running_vehicle,
        ):
            with self.assertRaises(worker_module.StaleWorkerClaimError):
                worker_module.run_all_sites_task("http://nas", "worker-a", task, Path("artifacts"))

        self.assertEqual(protected_side_effects, [])

    def test_worker_claim_heartbeat_posts_periodically_and_joins_on_stop(self):
        start_heartbeat = getattr(worker_module, "_start_worker_claim_heartbeat", None)
        self.assertIsNotNone(start_heartbeat, "worker._start_worker_claim_heartbeat is missing")
        fired = threading.Event()
        calls: list[str] = []

        def post(*args, **kwargs):
            calls.append(str(args[2]))
            fired.set()

        with mock.patch.dict(os.environ, {"WORKER_CLAIM_HEARTBEAT_SECONDS": "0.01"}), mock.patch.object(
            worker_module, "post_status", side_effect=post
        ):
            stop = start_heartbeat("http://nas", "task-heartbeat", "worker-a")
            self.assertTrue(fired.wait(1.0))
            stop()
            count_after_stop = len(calls)
            time.sleep(0.03)

        self.assertGreaterEqual(count_after_stop, 1)
        self.assertEqual(len(calls), count_after_stop)

    def test_worker_claim_monitor_cancels_quickly_when_nas_claim_changes(self):
        task_id = "task-heartbeat-stale"
        worker_module._remember_task_claim(
            {"task_id": task_id},
            {"status": "claimed", "claim_id": "claim-a", "worker_id": "worker-a"},
        )
        cancellation_event = threading.Event()
        worker_module._register_task_cancellation_event(task_id, cancellation_event)
        changed_payload = {
            "worker_queue": {"status": "claimed", "claim_id": "claim-b", "worker_id": "worker-b"}
        }
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "WORKER_CLAIM_POLL_SECONDS": "0.01",
                    "WORKER_CLAIM_HEARTBEAT_SECONDS": "240",
                },
            ), mock.patch.object(
                worker_module,
                "fetch_task_payload",
                return_value=changed_payload,
            ), mock.patch.object(worker_module, "post_status") as post:
                stop = worker_module._start_worker_claim_heartbeat("http://nas", task_id, "worker-a")
                self.assertTrue(cancellation_event.wait(1.0))
                stop()

            post.assert_not_called()
            with self.assertRaises(worker_module.StaleWorkerClaimError):
                worker_module._raise_if_task_cancelled(task_id, cancellation_event)
        finally:
            worker_module._unregister_task_cancellation_event(task_id, cancellation_event)

    def test_site_group_stops_before_selenium_when_payload_claim_has_changed(self):
        task_id = "claim-changed-task"
        worker_module._remember_task_claim(
            {"task_id": task_id},
            {"claim_id": "claim-a", "worker_id": "worker-a"},
        )
        runner = mock.Mock()
        payload = {
            "task": {"task_id": task_id},
            "worker_queue": {"status": "claimed", "claim_id": "claim-b", "worker_id": "worker-b"},
            "site_statuses": {"duty_work_log": {"status": "not_started"}},
        }
        with mock.patch.object(worker_module, "fetch_task_payload", return_value=payload):
            with self.assertRaises(worker_module.StaleWorkerClaimError):
                worker_module._run_worker_site_group(
                    "http://nas",
                    task_id,
                    [("duty_work_log", runner)],
                )

        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
