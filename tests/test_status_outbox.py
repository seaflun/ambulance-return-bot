from __future__ import annotations

import tempfile
import unittest
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


class WorkerStatusOutboxTests(unittest.TestCase):
    def _outbox_type(self):
        try:
            from ambulance_bot.status_outbox import WorkerStatusOutbox
        except (ImportError, AttributeError) as exc:
            self.fail(f"WorkerStatusOutbox is missing: {exc}")
        return WorkerStatusOutbox

    def test_entries_survive_a_new_outbox_instance_and_preserve_order(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = WorkerStatusOutbox(root)
            first.enqueue({"status": "worker_running", "task_id": "task-1"})
            first.enqueue({"status": "vehicle_mileage_saved", "task_id": "task-1"})

            second = WorkerStatusOutbox(root)
            entries = second.pending()

            self.assertEqual(
                [entry["payload"]["status"] for entry in entries],
                ["worker_running", "vehicle_mileage_saved"],
            )

    def test_concurrent_enqueue_does_not_lose_events(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = WorkerStatusOutbox(Path(temp_dir))

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda index: outbox.enqueue({"index": index}), range(64)))

            entries = outbox.pending()
            self.assertEqual(len(entries), 64)
            self.assertEqual({entry["payload"]["index"] for entry in entries}, set(range(64)))

    def test_sequence_order_does_not_reverse_when_wall_clock_moves_backward(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = WorkerStatusOutbox(Path(temp_dir))
            with mock.patch("ambulance_bot.status_outbox.time.time_ns", side_effect=[200, 100]):
                outbox.enqueue({"status": "first-running"})
                outbox.enqueue({"status": "second-saved"})

            self.assertEqual(
                [entry["payload"]["status"] for entry in outbox.pending()],
                ["first-running", "second-saved"],
            )

    def test_claim_next_is_atomic_across_competing_consumers(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            WorkerStatusOutbox(root).enqueue({"index": 1})

            probe = WorkerStatusOutbox(root)
            claim_next = getattr(probe, "claim_next", None)
            self.assertIsNotNone(claim_next, "WorkerStatusOutbox.claim_next is missing")

            with ThreadPoolExecutor(max_workers=2) as executor:
                claims = list(executor.map(lambda _index: WorkerStatusOutbox(root).claim_next(), range(2)))

            claimed = [item for item in claims if item is not None]
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0]["payload"]["index"], 1)

    def test_release_returns_a_transiently_failed_claim_to_fifo(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = WorkerStatusOutbox(Path(temp_dir))
            event_id = outbox.enqueue({"index": 1})

            claimed = outbox.claim_next()
            self.assertEqual(claimed["event_id"], event_id)
            self.assertEqual(outbox.pending(), [])

            outbox.release(event_id)

            self.assertEqual([item["event_id"] for item in outbox.pending()], [event_id])

    def test_active_claim_is_not_stolen_but_expired_claim_is_recovered(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outbox = WorkerStatusOutbox(root, claim_lease_seconds=1)
            event_id = outbox.enqueue({"index": 1})
            self.assertEqual(outbox.claim_next()["event_id"], event_id)

            self.assertIsNone(WorkerStatusOutbox(root, claim_lease_seconds=1).claim_next())

            inflight_path = root / "inflight" / f"{event_id}.json"
            old = time.time() - 10
            os.utime(inflight_path, (old, old))
            recovered = WorkerStatusOutbox(root, claim_lease_seconds=1).claim_next()

            self.assertEqual(recovered["event_id"], event_id)

    def test_active_oldest_claim_blocks_a_newer_event_from_another_consumer(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outbox = WorkerStatusOutbox(root)
            first_id = outbox.enqueue({"index": 1})
            outbox.enqueue({"index": 2})

            self.assertEqual(outbox.claim_next()["event_id"], first_id)

            self.assertIsNone(WorkerStatusOutbox(root).claim_next())

    def test_locked_oldest_valid_record_does_not_allow_newer_event_to_overtake(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outbox = WorkerStatusOutbox(root)
            first_id = outbox.enqueue({"index": 1})
            outbox.enqueue({"index": 2})
            first_path = root / "pending" / f"{first_id}.json"
            real_replace = os.replace

            def replace_with_locked_oldest(source, target):
                if Path(source) == first_path and Path(target).parent == root / "inflight":
                    raise PermissionError("locked")
                return real_replace(source, target)

            with mock.patch("ambulance_bot.status_outbox.os.replace", side_effect=replace_with_locked_oldest):
                claimed = outbox.claim_next()

            self.assertIsNone(claimed)
            self.assertEqual([item["payload"]["index"] for item in outbox.pending()], [1, 2])

    def test_ack_removes_only_the_acknowledged_event(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = WorkerStatusOutbox(Path(temp_dir))
            first_id = outbox.enqueue({"index": 1})
            second_id = outbox.enqueue({"index": 2})

            outbox.ack(first_id)

            self.assertEqual([entry["event_id"] for entry in outbox.pending()], [second_id])

    def test_corrupt_event_is_quarantined_without_blocking_valid_events(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outbox = WorkerStatusOutbox(root)
            outbox.enqueue({"index": 1})
            pending_dir = root / "pending"
            (pending_dir / "00000000000000000000-corrupt.json").write_text("{broken", encoding="utf-8")

            entries = outbox.pending()

            self.assertEqual([entry["payload"]["index"] for entry in entries], [1])
            self.assertTrue(any((root / "quarantine").glob("*corrupt*.json")))

    def test_event_id_mismatch_is_quarantined_and_cannot_delete_another_event(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outbox = WorkerStatusOutbox(root)
            valid_id = outbox.enqueue({"index": 1})
            forged_id = outbox.enqueue({"index": 2})
            forged_path = next((root / "pending").glob(f"*{forged_id}*.json"))
            forged = __import__("json").loads(forged_path.read_text(encoding="utf-8"))
            forged["event_id"] = valid_id
            forged_path.write_text(__import__("json").dumps(forged), encoding="utf-8")

            entries = outbox.pending()
            outbox.ack(valid_id)

            self.assertEqual([entry["payload"]["index"] for entry in entries], [1])
            self.assertEqual(outbox.pending(), [])
            self.assertTrue(any((root / "quarantine").glob(f"*{forged_id}*.json")))

    def test_locked_corrupt_record_does_not_block_later_valid_events(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outbox = WorkerStatusOutbox(root)
            outbox.enqueue({"index": 1})
            pending_dir = root / "pending"
            corrupt = pending_dir / "00000000000000000000-corrupt.json"
            corrupt.write_text("{broken", encoding="utf-8")

            with mock.patch("ambulance_bot.status_outbox.os.replace", side_effect=PermissionError("locked")):
                try:
                    entries = outbox.pending()
                except PermissionError as exc:  # pragma: no cover - explicit regression assertion
                    self.fail(f"locked corrupt record blocked valid events: {exc}")

            self.assertEqual([entry["payload"]["index"] for entry in entries], [1])

    def test_transient_read_error_does_not_quarantine_a_valid_event(self):
        WorkerStatusOutbox = self._outbox_type()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outbox = WorkerStatusOutbox(root)
            event_id = outbox.enqueue({"index": 1})
            event_path = root / "pending" / f"{event_id}.json"
            real_read_text = Path.read_text

            def temporarily_locked(path, *args, **kwargs):
                if path == event_path:
                    raise PermissionError("antivirus scan")
                return real_read_text(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", autospec=True, side_effect=temporarily_locked):
                self.assertEqual(outbox.pending(), [])

            self.assertEqual([entry["event_id"] for entry in outbox.pending()], [event_id])
            self.assertFalse((root / "quarantine").exists())


if __name__ == "__main__":
    unittest.main()
