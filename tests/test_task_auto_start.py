import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from ambulance_bot.desktop_fast_runner import DesktopFastRunner
from ambulance_bot.manual_task_lock import acquire_manual_task_lock, clear_manual_task_lock
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_store import JsonTaskStore


class TaskAutoStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = JsonTaskStore(Path(self.tmp.name) / "tasks")

    def create_task(self, task_id="auto-task", mode="desktop_fast"):
        request = AmbulanceReturnRequest(
            task_id=task_id, created_at=datetime.now(), raw_text="", vehicle="新坡92"
        )
        return self.store.create(request, auto_start_mode=mode)

    def test_due_at_ten_minutes_and_persists_across_store_reopen(self):
        payload = self.create_task()
        created = datetime.fromisoformat(payload["created_at"])
        due = datetime.fromisoformat(payload["auto_start"]["due_at"])
        self.assertEqual(timedelta(minutes=10), due - created)
        reopened = JsonTaskStore(self.store.tasks_dir)
        self.assertEqual([], reopened.list_due_auto_starts(now=due - timedelta(seconds=1)))
        self.assertEqual(1, len(reopened.list_due_auto_starts(now=due)))
        self.assertTrue(reopened.begin_auto_start("auto-task", mode="desktop_fast", now=due))
        self.assertFalse(reopened.begin_auto_start("auto-task", mode="desktop_fast", now=due))

    def test_legacy_tasks_without_opt_in_do_not_auto_start(self):
        self.create_task(mode="")
        self.assertEqual([], self.store.list_due_auto_starts(now=datetime.now() + timedelta(days=1)))

    def test_ems_and_disaster_auto_start_without_claiming_manual_confirmation(self):
        payload = self.create_task()
        payload["ems_case_closed_confirmed"] = False
        self.store.save_payload("auto-task", payload)
        later = datetime.now() + timedelta(minutes=11)
        self.assertEqual(1, len(self.store.list_due_auto_starts(now=later)))
        payload["task"]["service_type"] = "disaster"
        self.store.save_payload("auto-task", payload)
        self.assertEqual(1, len(self.store.list_due_auto_starts(now=later)))
        self.assertTrue(self.store.begin_auto_start("auto-task", mode="desktop_fast", now=later))
        self.assertFalse(self.store.get("auto-task")["ems_case_closed_confirmed"])

    def test_started_paused_failed_and_confirmation_tasks_are_excluded(self):
        for index, status in enumerate(("queued_for_worker", "desktop_fast_running", "paused",
                                        "cancelled", "desktop_fast_completed", "desktop_fast_completed_with_errors")):
            with self.subTest(status=status):
                payload = self.create_task(str(index))
                payload["overall_status"] = status
                self.store.save_payload(str(index), payload)
        payload = self.create_task("confirmation")
        payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_waiting_confirmation"
        self.store.save_payload("confirmation", payload)
        self.assertEqual([], self.store.list_due_auto_starts(now=datetime.now() + timedelta(days=1)))

    def test_manual_start_cancels_deadline_without_resetting_it(self):
        payload = self.create_task()
        self.store.cancel_auto_start("auto-task")
        self.assertEqual(payload["auto_start"]["due_at"], self.store.get("auto-task")["auto_start"]["due_at"])
        self.assertEqual([], self.store.list_due_auto_starts(now=datetime.now() + timedelta(days=1)))

    def make_due(self, mode="desktop_fast"):
        payload = self.create_task(mode=mode)
        payload["auto_start"]["due_at"] = (datetime.now() - timedelta(seconds=1)).isoformat()
        self.store.save_payload("auto-task", payload)
        return DesktopFastRunner(Path(self.tmp.name), store=self.store)

    def test_busy_desktop_defers_without_consuming_auto_start(self):
        runner = self.make_due()
        self.assertTrue(acquire_manual_task_lock(Path(self.tmp.name), "other-task"))
        with mock.patch.object(runner, "_run") as run:
            runner.start_existing("auto-task", auto_start=True)
            run.assert_not_called()
        self.assertEqual("pending", self.store.get("auto-task")["auto_start"]["status"])
        clear_manual_task_lock(Path(self.tmp.name), "other-task")

    def test_auto_start_before_deadline_does_not_launch(self):
        self.create_task()
        runner = DesktopFastRunner(Path(self.tmp.name), store=self.store)
        with mock.patch.object(runner, "_run") as run:
            runner.start_existing("auto-task", auto_start=True)
            run.assert_not_called()
        self.assertTrue(runner.wait_for_idle())
        self.assertEqual("pending", self.store.get("auto-task")["auto_start"]["status"])

    def test_manual_then_auto_and_repeated_auto_launch_only_once(self):
        for first_auto in (False, True):
            with self.subTest(first_auto=first_auto):
                runner = self.make_due()
                def finish(task_id, owner):
                    runner._release_prepared_execution(task_id, owner, task_id)
                with mock.patch.object(runner, "_run", side_effect=finish) as run:
                    runner.start_existing("auto-task", auto_start=first_auto)
                    self.assertTrue(runner.wait_for_idle())
                    runner.start_existing("auto-task", auto_start=True)
                    self.assertTrue(runner.wait_for_idle())
                    self.assertEqual(1, run.call_count)

    def test_two_automatic_callers_share_execution_guard(self):
        runner = self.make_due()
        entered, release = threading.Event(), threading.Event()
        def finish(task_id, owner):
            entered.set()
            release.wait(3)
            runner._release_prepared_execution(task_id, owner, task_id)
        with mock.patch.object(runner, "_run", side_effect=finish) as run:
            try:
                runner.start_existing("auto-task", auto_start=True)
                self.assertTrue(entered.wait(2))
                runner.start_existing("auto-task", auto_start=True)
                self.assertEqual(1, run.call_count)
            finally:
                release.set()
                self.assertTrue(runner.wait_for_idle())

    def test_auto_queue_once_and_manual_queue_win(self):
        for manual_first in (False, True):
            with self.subTest(manual_first=manual_first):
                self.make_due(mode="worker_queue")
                if manual_first:
                    self.store.queue_for_worker("auto-task")
                    self.assertIsNone(self.store.queue_due_auto_start("auto-task"))
                else:
                    self.assertIsNotNone(self.store.queue_due_auto_start("auto-task"))
                queue_id = self.store.get("auto-task")["worker_queue"]["queue_id"]
                self.assertIsNone(self.store.queue_due_auto_start("auto-task"))
                self.assertEqual(queue_id, self.store.get("auto-task")["worker_queue"]["queue_id"])

    def test_deleted_and_malformed_deadlines_are_not_started(self):
        self.make_due()
        self.store.delete("auto-task")
        self.assertEqual([], self.store.list_due_auto_starts())
        for deadline in ("", "not-a-time", "2026-09-06T12:00:00+08:00"):
            with self.subTest(deadline=deadline):
                payload = self.create_task()
                payload["auto_start"]["due_at"] = deadline
                self.store.save_payload("auto-task", payload)
                self.assertEqual([], self.store.list_due_auto_starts())

    def test_editing_unstarted_task_preserves_original_deadline_and_uses_new_data(self):
        payload = self.create_task()
        edited = self.store.request_for("auto-task")
        edited.mileage = "123"
        self.store.update_task("auto-task", edited, changed_site_keys={"vehicle_mileage"})
        saved = self.store.get("auto-task")
        self.assertEqual(payload["auto_start"]["due_at"], saved["auto_start"]["due_at"])
        self.assertEqual("123", saved["task"]["mileage"])
        self.assertEqual(1, len(self.store.list_due_auto_starts(now=datetime.now() + timedelta(minutes=11))))


if __name__ == "__main__":
    unittest.main()
