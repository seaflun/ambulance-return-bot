from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest import mock

import worker as worker_module
from ambulance_bot.status_outbox import WorkerStatusOutbox


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return b'{"ok":true}'


class WorkerStatusDeliveryTests(unittest.TestCase):
    def setUp(self):
        stale_claims = getattr(worker_module, "_STALE_TASK_CLAIMS", None)
        if stale_claims is not None:
            stale_claims.clear()
        retry_after = getattr(worker_module, "_STATUS_DELIVERY_RETRY_AFTER", None)
        if retry_after is not None:
            retry_after.clear()
        cancellation_events = getattr(worker_module, "_TASK_CANCELLATION_EVENTS", None)
        if cancellation_events is not None:
            cancellation_events.clear()

    def test_transient_post_failure_is_durably_queued_without_escaping(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_TOKEN": "token",
                "WORKER_STATUS_POST_RETRIES": "1",
                "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "0",
            },
        ):
            with mock.patch.object(
                worker_module.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("temporary outage"),
            ):
                try:
                    worker_module.post_status("http://nas", "task-1", "worker_running", "running")
                except Exception as exc:  # pragma: no cover - explicit regression assertion
                    self.fail(f"transient status failure escaped instead of being queued: {exc}")

            pending = WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox").pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["payload"]["body"]["status"], "worker_running")
            self.assertTrue(pending[0]["payload"]["body"]["status_event_id"])
            self.assertNotIn("token", json.dumps(pending, ensure_ascii=False))

    def test_pending_status_is_delivered_before_a_newer_status(self):
        sent_statuses: list[str] = []

        def successful_urlopen(request, timeout):
            sent_statuses.append(json.loads(request.data.decode("utf-8"))["status"])
            return _Response()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_TOKEN": "token",
                "WORKER_STATUS_POST_RETRIES": "1",
                "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "0",
            },
        ):
            with mock.patch.object(
                worker_module.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("temporary outage"),
            ):
                try:
                    worker_module.post_status("http://nas", "task-1", "worker_running", "running")
                except Exception as exc:  # pragma: no cover - explicit regression assertion
                    self.fail(f"transient status failure escaped instead of being queued: {exc}")

            with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=successful_urlopen):
                worker_module.post_status("http://nas", "task-1", "vehicle_mileage_saved", "saved")

            self.assertEqual(sent_statuses, ["worker_running", "vehicle_mileage_saved"])
            self.assertEqual(WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox").pending(), [])

    def test_flush_status_outbox_replays_events_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_SERVER_URL": "http://nas",
                "WORKER_TOKEN": "token",
                "WORKER_STATUS_POST_RETRIES": "1",
            },
        ):
            outbox = WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox")
            outbox.enqueue(
                {
                    "task_id": "task-1",
                    "body": {
                        "status": "desktop_fast_completed",
                        "detail": "done",
                        "site_key": "",
                        "site_name": "",
                        "status_event_id": "event-after-restart",
                    },
                }
            )
            with mock.patch.object(worker_module.urllib.request, "urlopen", return_value=_Response()):
                flush = getattr(worker_module, "flush_status_outbox", None)
                self.assertIsNotNone(flush, "worker.flush_status_outbox is missing")
                self.assertEqual(flush(), 1)

            self.assertEqual(outbox.pending(), [])

    def test_flush_uses_configured_server_instead_of_a_spooled_url(self):
        sent_urls: list[str] = []

        def successful_urlopen(request, timeout):
            sent_urls.append(request.full_url)
            return _Response()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_SERVER_URL": "http://nas",
                "WORKER_TOKEN": "token",
                "WORKER_STATUS_POST_RETRIES": "1",
            },
        ):
            WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox").enqueue(
                {
                    "url": "http://attacker.invalid/collect",
                    "task_id": "task-1",
                    "body": {"status": "worker_running", "status_event_id": "safe-event"},
                }
            )
            with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=successful_urlopen):
                worker_module.flush_status_outbox()

        self.assertEqual(sent_urls, ["http://nas/worker/tasks/task-1/status"])

    def test_http_409_stale_claim_is_dropped_without_blocking_new_events(self):
        conflict_body = BytesIO(b'{"error":"worker_claim_conflict"}')
        conflict = urllib.error.HTTPError(
            "http://nas/worker/tasks/task-1/status",
            409,
            "Conflict",
            hdrs=None,
            fp=conflict_body,
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {"ARTIFACTS_DIR": temp_dir, "WORKER_SERVER_URL": "http://nas", "WORKER_TOKEN": "token"},
        ):
            outbox = WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox")
            outbox.enqueue(
                {
                    "task_id": "task-1",
                    "body": {"status": "worker_running", "status_event_id": "stale-event"},
                }
            )
            with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=conflict):
                self.assertEqual(worker_module.flush_status_outbox(), 0)

            self.assertEqual(outbox.pending(), [])
            self.assertTrue(conflict_body.closed)

    def test_http_503_releases_event_for_later_retry(self):
        unavailable_body = BytesIO(b"temporary")
        unavailable = urllib.error.HTTPError(
            "http://nas/worker/tasks/task-1/status",
            503,
            "Unavailable",
            hdrs=None,
            fp=unavailable_body,
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {"ARTIFACTS_DIR": temp_dir, "WORKER_SERVER_URL": "http://nas", "WORKER_TOKEN": "token"},
        ):
            outbox = WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox")
            event_id = outbox.enqueue(
                {
                    "task_id": "task-1",
                    "body": {"status": "worker_running", "status_event_id": "retry-event"},
                }
            )
            with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=unavailable):
                self.assertEqual(worker_module.flush_status_outbox(), 0)

            self.assertEqual([item["event_id"] for item in outbox.pending()], [event_id])
            self.assertTrue(unavailable_body.closed)

    def test_permanent_404_is_dead_lettered_without_blocking_a_live_task(self):
        sent_tasks: list[str] = []

        def urlopen_by_task(request, timeout):
            if "/missing/status" in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)
            sent_tasks.append(request.full_url)
            return _Response()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_SERVER_URL": "http://nas",
                "WORKER_STATUS_POST_RETRIES": "1",
            },
        ):
            outbox = WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox")
            outbox.enqueue(
                {"task_id": "missing", "body": {"status": "worker_running", "status_event_id": "missing-event"}}
            )
            outbox.enqueue(
                {"task_id": "live", "body": {"status": "worker_running", "status_event_id": "live-event"}}
            )

            with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=urlopen_by_task):
                self.assertEqual(worker_module.flush_status_outbox(), 1)

            self.assertEqual(outbox.pending(), [])
            self.assertEqual(sent_tasks, ["http://nas/worker/tasks/live/status"])
            self.assertEqual(len(list((Path(temp_dir) / "worker_status_outbox" / "dead_letter").glob("*.json"))), 1)

    def test_flush_lock_error_after_enqueue_does_not_escape_or_lose_spooled_event(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {"ARTIFACTS_DIR": temp_dir, "WORKER_STATUS_POST_RETRIES": "1"},
        ), mock.patch.object(WorkerStatusOutbox, "claim_next", side_effect=PermissionError("locked")):
            try:
                worker_module.post_status("http://nas", "task-1", "worker_running", "running")
            except PermissionError as exc:  # pragma: no cover - explicit regression assertion
                self.fail(f"outbox claim lock escaped post_status: {exc}")

            pending = WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox").pending()
            self.assertEqual(len(pending), 1)

    def test_transient_enqueue_failure_preserves_completed_status_across_restart_and_fifo(self):
        sent_statuses: list[str] = []

        def successful_urlopen(request, timeout):
            sent_statuses.append(json.loads(request.data.decode("utf-8"))["status"])
            return _Response()

        original_enqueue = WorkerStatusOutbox.enqueue
        enqueue_attempts = 0

        def transiently_locked(outbox, payload):
            nonlocal enqueue_attempts
            enqueue_attempts += 1
            if enqueue_attempts == 1:
                raise PermissionError("disk temporarily locked")
            return original_enqueue(outbox, payload)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_STATUS_POST_RETRIES": "1",
                "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "0",
                "WORKER_STATUS_ENQUEUE_RETRIES": "3",
                "WORKER_STATUS_ENQUEUE_RETRY_DELAY_SECONDS": "0",
            },
        ):
            outbox = WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox")
            outbox.enqueue(
                {
                    "task_id": "task-old",
                    "body": {
                        "status": "worker_running",
                        "detail": "older",
                        "status_event_id": "older-event",
                    },
                }
            )
            with mock.patch.object(WorkerStatusOutbox, "enqueue", new=transiently_locked), mock.patch.object(
                worker_module.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("NAS offline"),
            ):
                worker_module.post_status("http://nas", "task-new", "desktop_fast_completed", "done")

            restarted_outbox = WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox")
            self.assertEqual(
                [record["payload"]["body"]["status"] for record in restarted_outbox.pending()],
                ["worker_running", "desktop_fast_completed"],
            )
            with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=successful_urlopen):
                self.assertEqual(worker_module.flush_status_outbox("http://nas"), 2)

        self.assertEqual(enqueue_attempts, 2)
        self.assertEqual(sent_statuses, ["worker_running", "desktop_fast_completed"])

    def test_exhausted_enqueue_retry_is_bounded_raises_and_never_overtakes_fifo(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_STATUS_POST_RETRIES": "1",
                "WORKER_STATUS_ENQUEUE_RETRIES": "3",
                "WORKER_STATUS_ENQUEUE_RETRY_DELAY_SECONDS": "0",
            },
        ), mock.patch.object(
            WorkerStatusOutbox,
            "enqueue",
            side_effect=PermissionError("disk locked"),
        ) as enqueue, mock.patch.object(worker_module.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(PermissionError):
                worker_module.post_status("http://nas", "task-new", "desktop_fast_completed", "done")

            self.assertEqual(enqueue.call_count, 3)
            urlopen.assert_not_called()

    def test_transient_failure_arms_backoff_so_each_new_status_does_not_wait_again(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_STATUS_POST_RETRIES": "1",
                "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "60",
            },
        ), mock.patch.object(
            worker_module.urllib.request,
            "urlopen",
            side_effect=TimeoutError("NAS timeout"),
        ) as urlopen:
            worker_module.post_status("http://nas", "task-1", "worker_running", "first")
            worker_module.post_status("http://nas", "task-1", "vehicle_mileage_running", "second")

            self.assertEqual(urlopen.call_count, 1)
            self.assertEqual(len(WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox").pending()), 2)

    def test_flush_has_a_bounded_batch_size(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_SERVER_URL": "http://nas",
                "WORKER_STATUS_FLUSH_MAX_EVENTS": "2",
            },
        ):
            outbox = WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox")
            for index in range(3):
                outbox.enqueue(
                    {
                        "task_id": f"task-{index}",
                        "body": {"status": "worker_running", "status_event_id": f"event-{index}"},
                    }
                )
            with mock.patch.object(worker_module.urllib.request, "urlopen", return_value=_Response()):
                self.assertEqual(worker_module.flush_status_outbox(), 2)

            self.assertEqual(len(outbox.pending()), 1)

    def test_current_claim_409_raises_stale_claim_signal_after_preserving_event(self):
        stale_error = getattr(worker_module, "StaleWorkerClaimError", None)
        self.assertIsNotNone(stale_error, "worker.StaleWorkerClaimError is missing")
        conflict = urllib.error.HTTPError("http://nas", 409, "Conflict", None, BytesIO(b"stale"))
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {"ARTIFACTS_DIR": temp_dir, "WORKER_STATUS_POST_RETRIES": "1"},
        ):
            worker_module._remember_task_claim(
                {"task_id": "claimed-task"},
                {"claim_id": "claim-a", "worker_id": "PC-01"},
            )
            with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=conflict):
                with self.assertRaises(stale_error):
                    worker_module.post_status("http://nas", "claimed-task", "worker_running", "running")

            self.assertEqual(WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox").pending(), [])
            self.assertEqual(len(list((Path(temp_dir) / "worker_status_outbox" / "dead_letter").glob("*.json"))), 1)

    def test_old_claim_outbox_409_does_not_cancel_new_claim_for_same_task(self):
        task_id = "same-task-new-claim"
        cancellation_event = threading.Event()
        conflict = urllib.error.HTTPError("http://nas", 409, "Conflict", None, BytesIO(b"stale"))
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_STATUS_POST_RETRIES": "1",
                "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "0",
            },
        ):
            outbox = WorkerStatusOutbox(Path(temp_dir) / "worker_status_outbox")
            outbox.enqueue(
                {
                    "task_id": task_id,
                    "body": {
                        "status": "worker_running",
                        "status_event_id": "old-claim-event",
                        "claim_id": "claim-a",
                        "worker_id": "PC-01",
                    },
                }
            )
            worker_module._remember_task_claim(
                {"task_id": task_id},
                {"claim_id": "claim-b", "worker_id": "PC-01"},
            )
            worker_module._register_task_cancellation_event(task_id, cancellation_event)
            try:
                with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=conflict):
                    worker_module.flush_status_outbox("http://nas")

                self.assertFalse(cancellation_event.is_set())
                worker_module._raise_if_task_cancelled(task_id, cancellation_event)
                self.assertFalse(worker_module._task_claim_is_stale(task_id, "claim-b"))
            finally:
                worker_module._unregister_task_cancellation_event(task_id, cancellation_event)

    def test_delayed_claim_a_409_does_not_cancel_claim_b_execution_started_after_stale_context(self):
        task_id = "same-task-restarted-claim"
        conflict = urllib.error.HTTPError("http://nas", 409, "Conflict", None, BytesIO(b"stale"))
        context_after_end: dict[str, str] = {}
        stale_after_end = ""
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "ARTIFACTS_DIR": temp_dir,
                "WORKER_STATUS_POST_RETRIES": "1",
                "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "0",
            },
        ):
            artifacts_dir = Path(temp_dir)
            worker_module._remember_task_claim(
                {"task_id": task_id},
                {"claim_id": "claim-a", "worker_id": "PC-01"},
            )
            cancellation_event = worker_module.begin_manual_task_execution(task_id, artifacts_dir)
            self.assertIsNotNone(cancellation_event)
            assert cancellation_event is not None
            worker_module._remember_task_claim(
                {"task_id": task_id},
                {"claim_id": "claim-b", "worker_id": "PC-01"},
            )
            registered_claim = worker_module._TASK_CANCELLATION_EVENTS[task_id][cancellation_event]
            outbox = WorkerStatusOutbox(artifacts_dir / "worker_status_outbox")
            outbox.enqueue(
                {
                    "task_id": task_id,
                    "body": {
                        "status": "worker_running",
                        "status_event_id": "delayed-claim-a-event",
                        "claim_id": "claim-a",
                        "worker_id": "PC-01",
                    },
                }
            )
            try:
                with mock.patch.object(worker_module.urllib.request, "urlopen", side_effect=conflict):
                    worker_module.flush_status_outbox("http://nas")
                event_was_cancelled = cancellation_event.is_set()
                claim_b_was_stale = worker_module._task_claim_is_stale(task_id, "claim-b")
            finally:
                worker_module.end_manual_task_execution(task_id, cancellation_event, artifacts_dir)
                context_after_end = worker_module._task_claim_context(task_id)
                stale_after_end = worker_module._STALE_TASK_CLAIMS.get(task_id, "")
                with worker_module._TASK_CLAIM_CONTEXT_LOCK:
                    worker_module._TASK_CLAIM_CONTEXT.pop(task_id, None)
                worker_module._clear_task_claim_stale(task_id)

        self.assertEqual(registered_claim, "claim-b")
        self.assertFalse(event_was_cancelled)
        self.assertFalse(claim_b_was_stale)
        self.assertEqual(context_after_end, {})
        self.assertEqual(stale_after_end, "")


if __name__ == "__main__":
    unittest.main()
