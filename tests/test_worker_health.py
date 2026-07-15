import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from ambulance_bot import worker_health


class WorkerHealthTests(unittest.TestCase):
    def test_build_heartbeat_rejects_unknown_state_and_writes_allowlisted_fields(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                worker_health.build_heartbeat(
                    worker_id="PC-01",
                    package_version="2026.07.15.1326",
                    pid=123,
                    state="online",
                    execution_mode="gui",
                    package_path="C:/package",
                    process_started_at="2026-07-15T12:00:00.000+00:00",
                )

            payload = worker_health.build_heartbeat(
                worker_id="PC-01",
                package_version="2026.07.15.1326",
                pid=123,
                state="idle",
                execution_mode="gui",
                package_path="C:/package",
                process_started_at="2026-07-15T12:00:00.000+00:00",
            )

        self.assertEqual(payload["state"], "idle")
        self.assertNotIn("token", payload)
        self.assertEqual(
            set(payload),
            {
                "worker_id",
                "package_version",
                "pid",
                "state",
                "execution_mode",
                "package_path",
                "process_started_at",
                "activity",
                "busy_reason",
                "request_id",
                "observed_at",
            },
        )

    def test_atomic_write_replaces_complete_json_without_tmp_residue(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            path = worker_health.worker_heartbeat_path()
            worker_health.write_json_atomic(path, {"sequence": 1})
            worker_health.write_json_atomic(path, {"sequence": 2})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["sequence"], 2)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_activity_clear_only_removes_matching_owner(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            worker_health.write_activity(activity="case_lookup", owner="owner-a")

            self.assertFalse(worker_health.clear_activity("owner-b"))
            self.assertTrue(worker_health.activity_is_fresh(120))
            self.assertTrue(worker_health.clear_activity("owner-a"))
            self.assertFalse(worker_health.activity_is_fresh(120))

    def test_activity_freshness_rejects_expired_or_malformed_state(self):
        now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            worker_health.write_activity(
                activity="case_lookup",
                owner="owner-a",
                observed_at=now - timedelta(seconds=121),
            )
            self.assertFalse(worker_health.activity_is_fresh(120, now=now))

            worker_health.worker_activity_path().write_text("not json", encoding="utf-8")
            self.assertFalse(worker_health.activity_is_fresh(120, now=now))

    def test_gui_restart_decision_has_grace_busy_guard_and_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            now = 1_000.0

            self.assertEqual(
                worker_health.decide_gui_restart(
                    now_monotonic=now,
                    thread_alive=True,
                    stopped_at=now - 20,
                    activity_active=False,
                    update_active=False,
                    restart_times=[],
                ).reason,
                "thread_alive",
            )
            self.assertFalse(
                worker_health.decide_gui_restart(
                    now_monotonic=now,
                    thread_alive=False,
                    stopped_at=now - 14,
                    activity_active=False,
                    update_active=False,
                    restart_times=[],
                ).should_restart,
            )
            self.assertFalse(
                worker_health.decide_gui_restart(
                    now_monotonic=now,
                    thread_alive=False,
                    stopped_at=now - 16,
                    activity_active=True,
                    update_active=False,
                    restart_times=[],
                ).should_restart,
            )
            self.assertFalse(
                worker_health.decide_gui_restart(
                    now_monotonic=now,
                    thread_alive=False,
                    stopped_at=now - 16,
                    activity_active=False,
                    update_active=True,
                    restart_times=[],
                ).should_restart,
            )

            limited = worker_health.decide_gui_restart(
                now_monotonic=now,
                thread_alive=False,
                stopped_at=now - 16,
                activity_active=False,
                update_active=False,
                restart_times=[900.0, 950.0, 990.0],
            )
            self.assertEqual(limited.reason, "restart_rate_limited")
            self.assertFalse(limited.should_restart)
            self.assertEqual(limited.retained_restart_times, (900.0, 950.0, 990.0))

            safe = worker_health.decide_gui_restart(
                now_monotonic=now,
                thread_alive=False,
                stopped_at=now - 16,
                activity_active=False,
                update_active=False,
                restart_times=[200.0],
            )
            self.assertTrue(safe.should_restart)
            self.assertEqual(safe.reason, "safe_to_restart")
            self.assertEqual(safe.retained_restart_times, ())


if __name__ == "__main__":
    unittest.main()
