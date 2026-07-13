import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import ambulance_bot.task_cancellation as cancellation_module


class TaskCancellationTests(unittest.TestCase):
    def test_corrupt_marker_stops_current_execution_but_cleanup_unpoisons_future_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            task_id = "task-corrupt-marker"
            marker_path = cancellation_module.task_cancellation_marker_path(artifacts_dir, task_id)
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text("{broken-json", encoding="utf-8")

            self.assertTrue(
                cancellation_module.task_cancellation_requested(
                    artifacts_dir,
                    task_id,
                    execution_owner="owner-a",
                )
            )
            cancellation_module.clear_task_cancellation(
                artifacts_dir,
                task_id,
                execution_owner="owner-a",
            )

            self.assertFalse(marker_path.exists())
            self.assertFalse(
                cancellation_module.task_cancellation_requested(
                    artifacts_dir,
                    task_id,
                    execution_owner="owner-b",
                )
            )

    def test_marker_read_failure_fails_closed_and_clear_keeps_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            task_id = "task-marker-read-failure"
            marker_path = cancellation_module.request_task_cancellation(
                artifacts_dir,
                task_id,
                execution_owner="owner-a",
            )

            with mock.patch.object(Path, "read_text", side_effect=PermissionError("blocked")):
                self.assertTrue(
                    cancellation_module.task_cancellation_requested(
                        artifacts_dir,
                        task_id,
                        execution_owner="owner-a",
                    )
                )
                cancellation_module.clear_task_cancellation(
                    artifacts_dir,
                    task_id,
                    execution_owner="owner-a",
                )

            self.assertTrue(marker_path.exists())

    def test_old_owner_clear_cannot_delete_new_owner_marker_written_during_compare_unlink_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            task_id = "task-marker-generation"
            marker_path = cancellation_module.request_task_cancellation(
                artifacts_dir,
                task_id,
                execution_owner="owner-a",
            )
            marker_read = threading.Event()
            allow_clear_to_continue = threading.Event()
            replacement_finished = threading.Event()
            original_read_text = Path.read_text

            def pause_after_marker_read(path, *args, **kwargs):
                text = original_read_text(path, *args, **kwargs)
                if Path(path) == marker_path and not marker_read.is_set():
                    marker_read.set()
                    self.assertTrue(allow_clear_to_continue.wait(1.0))
                return text

            def write_replacement_marker():
                cancellation_module.request_task_cancellation(
                    artifacts_dir,
                    task_id,
                    execution_owner="owner-b",
                )
                replacement_finished.set()

            with mock.patch.object(Path, "read_text", autospec=True, side_effect=pause_after_marker_read):
                clear_thread = threading.Thread(
                    target=cancellation_module.clear_task_cancellation,
                    args=(artifacts_dir, task_id),
                    kwargs={"execution_owner": "owner-a"},
                )
                clear_thread.start()
                self.assertTrue(marker_read.wait(1.0))
                replacement_thread = threading.Thread(target=write_replacement_marker)
                replacement_thread.start()
                replacement_finished.wait(0.1)
                allow_clear_to_continue.set()
                clear_thread.join(1.0)
                replacement_thread.join(1.0)

            self.assertFalse(clear_thread.is_alive())
            self.assertFalse(replacement_thread.is_alive())
            self.assertTrue(
                cancellation_module.task_cancellation_requested(
                    artifacts_dir,
                    task_id,
                    execution_owner="owner-b",
                )
            )


if __name__ == "__main__":
    unittest.main()
