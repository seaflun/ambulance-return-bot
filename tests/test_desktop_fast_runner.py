import tempfile
import threading
import time
import unittest
import os
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ambulance_bot.desktop_fast_runner as desktop_fast_runner_module
from ambulance_bot.desktop_fast_runner import DEFAULT_RECORD_ROOT, DesktopFastRunner
from ambulance_bot.manual_task_lock import acquire_manual_task_lock, manual_task_lock_path
from ambulance_bot.models import AmbulanceReturnRequest, FuelRecord, request_from_form
from ambulance_bot.task_cancellation import request_task_cancellation, task_cancellation_marker_path
from ambulance_bot.task_store import JsonTaskStore, task_completion_snapshot
from ambulance_bot.update_safety import ManualUpdateRequiredError


class DesktopFastRunnerTests(unittest.TestCase):
    def test_disinfection_preflights_manual_update_before_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-disinfection-preflight",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch.object(
                desktop_fast_runner_module,
                "require_safe_automated_update",
                side_effect=ManualUpdateRequiredError("消毒舊資料需人工處理"),
                create=True,
            ) as preflight, patch.object(
                desktop_fast_runner_module, "login_disinfection_and_get_driver"
            ) as login, patch.object(
                desktop_fast_runner_module, "run_disinfection_task"
            ) as run:
                with self.assertRaises(ManualUpdateRequiredError):
                    runner._run_disinfection(request, "preflight")

            preflight.assert_called_once()
            login.assert_not_called()
            run.assert_not_called()

    def test_manual_update_error_becomes_waiting_confirmation_instead_of_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-manual-consumables",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)
            runner._running.add(request.task_id)
            owner = runner._prepare_execution(request.task_id, request.task_id, "busy")
            try:
                blocked = runner._run_site(
                    request.task_id,
                    "consumables",
                    lambda: (_ for _ in ()).throw(ManualUpdateRequiredError("舊耗材頁需人工更新")),
                )
            finally:
                runner._finish_execution(request.task_id, owner, request.task_id, lambda: None)

            site = store.get(request.task_id)["site_statuses"]["consumables"]
            self.assertTrue(blocked)
            self.assertEqual(site["status"], "consumables_waiting_confirmation")
            self.assertIn("人工", site["detail"])

    def test_manual_task_lock_heartbeat_returns_joining_stop_callback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MANUAL_TASK_LOCK_HEARTBEAT_SECONDS": "0.01"}
        ):
            artifacts_dir = Path(tmp)
            stop_heartbeat = desktop_fast_runner_module._start_manual_task_lock_heartbeat(
                artifacts_dir, "desktop_fast:join-test"
            )
            self.assertTrue(callable(stop_heartbeat))
            stop_heartbeat()

    def test_manual_task_lock_heartbeat_owner_loss_cancels_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MANUAL_TASK_LOCK_HEARTBEAT_SECONDS": "0.01"}
        ), patch.object(
            desktop_fast_runner_module,
            "refresh_manual_task_lock",
            return_value=False,
        ):
            lost_lease = threading.Event()
            stop_heartbeat = desktop_fast_runner_module._start_manual_task_lock_heartbeat(
                Path(tmp),
                "desktop_fast:owner-loss",
                lost_lease,
            )

            self.assertTrue(lost_lease.wait(1.0))
            stop_heartbeat()

    def test_manual_task_lock_heartbeat_repeated_io_errors_cancel_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MANUAL_TASK_LOCK_HEARTBEAT_SECONDS": "0.01"}
        ), patch.object(
            desktop_fast_runner_module,
            "refresh_manual_task_lock",
            side_effect=OSError("lease storage unavailable"),
        ) as refresh:
            lost_lease = threading.Event()
            stop_heartbeat = desktop_fast_runner_module._start_manual_task_lock_heartbeat(
                Path(tmp),
                "desktop_fast:io-loss",
                lost_lease,
            )

            self.assertTrue(lost_lease.wait(1.0))
            self.assertEqual(refresh.call_count, 3)
            stop_heartbeat()

    def test_prepare_execution_acquire_error_does_not_leak_running_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            store = JsonTaskStore(artifacts_dir / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-prepare-error",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(artifacts_dir, store=store)
            runner._running.add(request.task_id)

            with patch.object(
                desktop_fast_runner_module,
                "acquire_manual_task_lock",
                side_effect=OSError("lease path unavailable"),
            ):
                owner = runner._prepare_execution(request.task_id, request.task_id, "busy")

            self.assertEqual(owner, "")
            self.assertEqual(runner._running, set())
            self.assertEqual(runner._execution_owner(request.task_id), "")
            self.assertNotEqual(store.get(request.task_id)["overall_status"], "desktop_fast_busy")

    def test_background_thread_start_error_releases_prepared_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            store = JsonTaskStore(artifacts_dir / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-thread-start-error",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(artifacts_dir, store=store)

            with patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")):
                runner.start_existing(request.task_id)

            self.assertEqual(runner._running, set())
            self.assertEqual(runner._execution_owner(request.task_id), "")
            self.assertFalse(manual_task_lock_path(artifacts_dir).exists())
            self.assertEqual(
                store.get(request.task_id)["overall_status"],
                "desktop_fast_completed_with_errors",
            )

    def test_heartbeat_start_error_inside_run_releases_prepared_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            store = JsonTaskStore(artifacts_dir / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-heartbeat-start-error",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(artifacts_dir, store=store)

            with patch.object(
                desktop_fast_runner_module,
                "_start_manual_task_lock_heartbeat",
                side_effect=RuntimeError("heartbeat start failed"),
            ):
                runner._run(request.task_id)

            self.assertEqual(runner._running, set())
            self.assertEqual(runner._execution_owner(request.task_id), "")
            self.assertFalse(manual_task_lock_path(artifacts_dir).exists())

    def test_prepared_execution_cleanup_error_still_releases_owner_and_running_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            store = JsonTaskStore(artifacts_dir / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-prepared-cleanup-error",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(artifacts_dir, store=store)
            runner._running.add(request.task_id)
            owner = runner._prepare_execution(request.task_id, request.task_id, "busy")

            with patch.object(
                desktop_fast_runner_module,
                "clear_task_cancellation",
                side_effect=OSError("marker cleanup unavailable"),
            ):
                runner._release_prepared_execution(request.task_id, owner, request.task_id)

            self.assertEqual(runner._running, set())
            self.assertEqual(runner._execution_owner(request.task_id), "")
            self.assertFalse(manual_task_lock_path(artifacts_dir).exists())

    def test_lost_owner_a_cleanup_cannot_abort_replacement_owner_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            store = JsonTaskStore(artifacts_dir / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-owner-replaced",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(artifacts_dir, store=store)
            runner._running.add(request.task_id)
            owner_a = runner._prepare_execution(request.task_id, request.task_id, "busy")
            lost_event = runner._lease_lost_event(request.task_id)
            self.assertIsNotNone(lost_event)
            assert lost_event is not None

            self.assertTrue(desktop_fast_runner_module.clear_manual_task_lock(artifacts_dir, owner_a))
            owner_b = "desktop_fast:task-owner-replaced:owner-b"
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, owner_b))
            store.set_overall_status(request.task_id, "desktop_fast_running", "owner B is active")
            lost_event.set()

            try:
                runner._finish_execution(request.task_id, owner_a, request.task_id, lambda: None)
                payload = store.get(request.task_id)
                self.assertEqual(payload["overall_status"], "desktop_fast_running")
                self.assertEqual(payload["events"][-1]["detail"], "owner B is active")
                self.assertEqual(runner._running, set())
                self.assertEqual(runner._execution_owner(request.task_id), "")
            finally:
                desktop_fast_runner_module.clear_manual_task_lock(artifacts_dir, owner_b)

    def test_empty_site_run_owner_a_cannot_write_terminal_status_after_owner_b_takes_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            store = JsonTaskStore(artifacts_dir / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-terminal-owner-replaced",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            replacement_done = False
            owner_b = "desktop_fast:task-terminal-owner-replaced:owner-b"
            runner = DesktopFastRunner(artifacts_dir, store=store)

            def replace_owner_before_terminal(_payload, action):
                nonlocal replacement_done
                if replacement_done or "登打開始" not in action:
                    return
                replacement_done = True
                owner_a = runner._execution_owner(request.task_id)
                self.assertTrue(desktop_fast_runner_module.clear_manual_task_lock(artifacts_dir, owner_a))
                self.assertTrue(acquire_manual_task_lock(artifacts_dir, owner_b))
                store.set_overall_status(request.task_id, "desktop_fast_running", "owner B is active")

            runner.event_callback = replace_owner_before_terminal
            try:
                with patch.object(
                    desktop_fast_runner_module,
                    "active_site_groups",
                    return_value=[],
                ), patch.object(runner, "_ensure_record_folders", return_value=""):
                    runner._run(request.task_id)

                payload = store.get(request.task_id)
                self.assertTrue(replacement_done)
                self.assertEqual(payload["overall_status"], "desktop_fast_running")
                self.assertEqual(payload["events"][-1]["detail"], "owner B is active")
            finally:
                desktop_fast_runner_module.clear_manual_task_lock(artifacts_dir, owner_b)

    def test_waiting_confirmation_blocks_overall_success(self):
        result = SimpleNamespace(ok=True, status="vehicle_mileage_waiting_confirmation", detail="not confirmed")

        self.assertTrue(desktop_fast_runner_module._result_blocks_next(result))

    def test_full_run_heartbeats_manual_lock_while_site_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-heartbeat",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)
            heartbeat_calls: list[str] = []
            original_set = desktop_fast_runner_module.refresh_manual_task_lock

            def record_set(artifacts_dir, owner):
                heartbeat_calls.append(owner)
                return original_set(artifacts_dir, owner)

            slow_saved = lambda *_args, **_kwargs: time.sleep(0.06) or SimpleNamespace(
                status="duty_work_log_saved", detail="ok"
            )
            with patch.dict(os.environ, {"MANUAL_TASK_LOCK_HEARTBEAT_SECONDS": "0.01"}), patch.object(
                desktop_fast_runner_module, "refresh_manual_task_lock", side_effect=record_set
            ), patch.object(desktop_fast_runner_module, "run_local_selenium_task", side_effect=slow_saved), patch.object(
                desktop_fast_runner_module, "run_vehicle_mileage_task",
                return_value=SimpleNamespace(status="vehicle_mileage_saved", detail="ok"),
            ), patch.object(
                desktop_fast_runner_module, "login_disinfection_and_get_driver", return_value=Mock()
            ), patch.object(
                desktop_fast_runner_module, "run_disinfection_task",
                return_value=SimpleNamespace(status="disinfection_saved", detail="ok"),
            ), patch.object(desktop_fast_runner_module, "login_acs_and_get_driver", return_value=Mock()), patch.object(
                desktop_fast_runner_module, "open_consumable_record_for_task", return_value="ok"
            ):
                runner.start_existing("task-heartbeat")
                self.assertTrue(runner.wait_for_idle())

        task_heartbeats = [owner for owner in heartbeat_calls if owner.startswith("desktop_fast:task-heartbeat:")]
        self.assertGreaterEqual(len(task_heartbeats), 2)
        self.assertEqual(len(set(task_heartbeats)), 1)

    def test_abort_marker_stops_desktop_runner_before_next_site_and_preserves_abort_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            store = JsonTaskStore(artifacts_dir / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-abort-desktop",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(artifacts_dir, store=store)
            protected_side_effects: list[str] = []

            def abort_during_first_site():
                owner = json.loads(manual_task_lock_path(artifacts_dir).read_text(encoding="utf-8"))["owner"]
                store.abort_running_task(
                    request.task_id,
                    "使用者中止登打。",
                    execution_lease_active=True,
                )
                request_task_cancellation(
                    artifacts_dir,
                    request.task_id,
                    execution_owner=owner,
                )
                return SimpleNamespace(status="duty_work_log_saved", detail="must not overwrite abort")

            def second_site_action():
                protected_side_effects.append("vehicle_mileage")
                return SimpleNamespace(status="vehicle_mileage_saved", detail="must not run")

            site_groups = [[
                ("duty_work_log", abort_during_first_site),
                ("vehicle_mileage", second_site_action),
            ]]
            with patch.object(desktop_fast_runner_module, "active_site_groups", return_value=site_groups), patch.object(
                runner, "_ensure_record_folders", return_value=""
            ), patch.object(desktop_fast_runner_module, "maximize_worker_site_windows"):
                runner._run(request.task_id)

            payload = store.get(request.task_id)
            self.assertEqual(protected_side_effects, [])
            self.assertEqual(payload["overall_status"], "desktop_fast_completed_with_errors")
            self.assertEqual(payload["site_statuses"]["duty_work_log"]["status"], "duty_work_log_failed")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "not_started")
            self.assertFalse(task_cancellation_marker_path(artifacts_dir, request.task_id).exists())

    def test_window_layout_cleanup_failure_does_not_leak_execution_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            store = JsonTaskStore(artifacts_dir / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-layout-cleanup",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(artifacts_dir, store=store)
            site_groups = [[
                (
                    "duty_work_log",
                    lambda: SimpleNamespace(status="duty_work_log_saved", detail="done"),
                )
            ]]

            with patch.object(desktop_fast_runner_module, "active_site_groups", return_value=site_groups), patch.object(
                runner, "_ensure_record_folders", return_value=""
            ), patch.object(
                desktop_fast_runner_module,
                "maximize_worker_site_windows",
                side_effect=RuntimeError("window manager unavailable"),
            ):
                runner._run(request.task_id)

            self.assertFalse(manual_task_lock_path(artifacts_dir).exists())
            self.assertEqual(runner._execution_owner(request.task_id), "")
            completed_payload = store.get(request.task_id)
            self.assertEqual(
                completed_payload["overall_status"],
                "site_run_completed",
            )
            self.assertFalse(
                task_completion_snapshot(completed_payload)["all_complete"]
            )

    def test_runs_four_sites_and_writes_local_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-1",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_local_selenium_task",
                return_value=SimpleNamespace(status="duty_work_log_saved", detail="duty ok"),
            ) as duty_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                return_value=SimpleNamespace(status="vehicle_mileage_saved", detail="mileage ok"),
            ) as mileage_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                return_value=SimpleNamespace(status="fuel_record_saved", detail="fuel ok"),
            ) as fuel_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ) as disinfection_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(status="disinfection_saved", detail="disinfection ok"),
            ) as disinfection_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
                return_value=Mock(name="driver"),
            ) as acs_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.open_consumable_record_for_task",
                return_value="consumables ok",
            ) as consumables_mock:
                runner.start_existing("task-1")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-1")
            self.assertEqual(payload["overall_status"], "desktop_fast_completed")
            self.assertTrue(task_completion_snapshot(payload)["all_complete"])
            self.assertFalse(manual_task_lock_path(Path(tmp)).exists())
            self.assertEqual(payload["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
            self.assertEqual(payload["site_statuses"]["fuel_record"]["status"], "not_started")
            self.assertEqual(payload["site_statuses"]["disinfection"]["status"], "disinfection_saved")
            self.assertEqual(payload["site_statuses"]["consumables"]["status"], "consumables_saved")
            duty_mock.assert_called_once()
            mileage_mock.assert_called_once()
            fuel_mock.assert_not_called()
            self.assertEqual(mileage_mock.call_args.kwargs["profile_name"], "vehicle_mileage_profile_task_1")
            self.assertTrue(mileage_mock.call_args.kwargs["force_new_driver"])
            disinfection_login_mock.assert_called_once()
            disinfection_mock.assert_called_once()
            self.assertIs(disinfection_mock.call_args.kwargs["existing_driver"], disinfection_login_mock.return_value)
            acs_login_mock.assert_called_once()
            consumables_mock.assert_called_once()

    def test_runs_fuel_site_when_fuel_record_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-fuel",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
                fuel_record=FuelRecord(enabled=True, date="20260627", time="1250", quantity="20.5", unit_price="30.1"),
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_local_selenium_task",
                return_value=SimpleNamespace(status="duty_work_log_saved", detail="duty ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                return_value=SimpleNamespace(status="vehicle_mileage_saved", detail="mileage ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                return_value=SimpleNamespace(status="fuel_record_saved", detail="fuel ok"),
            ) as fuel_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(status="disinfection_saved", detail="disinfection ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
                return_value=Mock(name="driver"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.open_consumable_record_for_task",
                return_value="consumables ok",
            ):
                runner.start_existing("task-fuel")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-fuel")
            self.assertEqual(payload["site_statuses"]["fuel_record"]["status"], "fuel_record_saved")
            fuel_mock.assert_called_once()

    def test_full_run_limits_parallel_sites_to_two_and_keeps_mileage_fuel_sequential(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-parallel",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
                fuel_record=FuelRecord(enabled=True, date="20260707", time="1240", quantity="35.0", unit_price="30.3"),
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)
            active = 0
            peak = 0
            intervals: dict[str, dict[str, float]] = {}
            lock = threading.Lock()

            def fake_run_site(task_id, site_key, action):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    intervals.setdefault(site_key, {})["start"] = time.perf_counter()
                time.sleep(0.05)
                with lock:
                    intervals[site_key]["end"] = time.perf_counter()
                    active -= 1
                return False

            with patch.object(runner, "_ensure_record_folders", return_value=""), patch.object(
                runner,
                "_run_site",
                side_effect=fake_run_site,
            ), patch("ambulance_bot.desktop_fast_runner.maximize_worker_site_windows"):
                runner.start_existing("task-parallel")
                self.assertTrue(runner.wait_for_idle())

            self.assertEqual(peak, 2)
            self.assertLessEqual(intervals["vehicle_mileage"]["end"], intervals["fuel_record"]["start"])

    def test_continues_to_disinfection_when_consumables_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-2",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_local_selenium_task",
                return_value=SimpleNamespace(ok=True, status="duty_work_log_saved", detail="duty ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                return_value=SimpleNamespace(ok=True, status="vehicle_mileage_saved", detail="mileage ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                return_value=SimpleNamespace(ok=True, status="fuel_record_saved", detail="fuel ok"),
            ), patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ) as disinfection_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(
                    ok=True,
                    status="disinfection_saved",
                    detail="disinfection ok",
                ),
            ) as disinfection_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
                return_value=Mock(name="driver"),
            ) as acs_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.open_consumable_record_for_task",
                side_effect=RuntimeError("耗材系統需要重新登入或驗證碼"),
            ) as consumables_mock:
                runner.start_existing("task-2")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-2")
            self.assertEqual(payload["overall_status"], "desktop_fast_completed_with_errors")
            self.assertIn("已略過失敗站並接續後續站別", payload["events"][-1]["detail"])
            self.assertEqual(payload["site_statuses"]["consumables"]["status"], "consumables_failed")
            self.assertEqual(payload["site_statuses"]["disinfection"]["status"], "disinfection_saved")
            acs_login_mock.assert_called_once()
            consumables_mock.assert_called_once()
            disinfection_login_mock.assert_called_once()
            disinfection_mock.assert_called_once()

    def test_single_site_runs_only_requested_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-3",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="?啣91",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_local_selenium_task",
            ) as duty_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
            ) as mileage_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                return_value=SimpleNamespace(ok=True, status="fuel_record_saved", detail="fuel skipped"),
            ) as fuel_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ) as disinfection_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(ok=True, status="disinfection_saved", detail="disinfection ok"),
            ) as disinfection_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
            ) as acs_login_mock:
                runner.start_site("task-3", "disinfection")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-3")
            self.assertEqual(payload["overall_status"], "site_run_completed")
            self.assertFalse(task_completion_snapshot(payload)["all_complete"])
            self.assertEqual(payload["site_statuses"]["disinfection"]["status"], "disinfection_saved")
            self.assertEqual(payload["site_statuses"]["duty_work_log"]["status"], "not_started")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "not_started")
            self.assertEqual(payload["site_statuses"]["consumables"]["status"], "not_started")
            duty_mock.assert_not_called()
            mileage_mock.assert_not_called()
            disinfection_login_mock.assert_called_once()
            disinfection_mock.assert_called_once()
            acs_login_mock.assert_not_called()

    def test_single_site_success_writes_site_terminal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="single-site-terminal",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch.object(
                runner,
                "_prepare_execution",
                return_value="desktop_fast:single-site-terminal",
            ), patch.object(
                desktop_fast_runner_module,
                "_start_manual_task_lock_heartbeat",
                return_value=lambda: None,
            ), patch.object(
                runner,
                "_run_site",
                return_value=0,
            ), patch.object(
                runner,
                "_finish_execution",
            ), patch.object(
                runner,
                "_set_overall_status_owned",
            ) as set_status:
                runner._run_single_site(
                    request.task_id,
                    "disinfection",
                    f"{request.task_id}:disinfection",
                )

            set_status.assert_any_call(
                request.task_id,
                "site_run_completed",
                "單站登打完成：緊急救護消毒。",
            )

    def test_mileage_fuel_single_site_continues_to_other_unfinished_pair_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-mileage-fuel",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="\u65b0\u576193",
                fuel_record=FuelRecord(enabled=True, date="20260707", time="1240", quantity="35.0", unit_price="30.3"),
            )
            store.create(request)
            store.update_site_result(
                "task-mileage-fuel",
                SimpleNamespace(key="vehicle_mileage", name="\u8eca\u8f1b\u91cc\u7a0b", status="vehicle_mileage_failed", detail="retry"),
            )
            store.update_site_result(
                "task-mileage-fuel",
                SimpleNamespace(key="fuel_record", name="\u767b\u6253\u52a0\u6cb9\u7d00\u9304", status="fuel_record_failed", detail="retry"),
            )
            runner = DesktopFastRunner(Path(tmp), store=store)
            calls = []

            def fuel_result(*args, **kwargs):
                calls.append("fuel_record")
                return SimpleNamespace(ok=True, status="fuel_record_saved", detail="fuel ok")

            def mileage_result(*args, **kwargs):
                calls.append("vehicle_mileage")
                return SimpleNamespace(ok=True, status="vehicle_mileage_saved", detail="mileage ok")

            with patch(
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                side_effect=fuel_result,
            ) as fuel_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                side_effect=mileage_result,
            ) as mileage_mock:
                runner.start_site("task-mileage-fuel", "fuel_record")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-mileage-fuel")
            self.assertEqual(calls, ["fuel_record", "vehicle_mileage"])
            self.assertEqual(payload["overall_status"], "site_run_completed")
            self.assertFalse(task_completion_snapshot(payload)["all_complete"])
            self.assertEqual(payload["site_statuses"]["fuel_record"]["status"], "fuel_record_saved")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
            fuel_mock.assert_called_once()
            mileage_mock.assert_called_once()

    def test_two_failed_sites_then_two_single_site_retries_finish_four_sites(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="two-retries-finish",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡92",
            )
            payload = store.create(request)
            payload["site_statuses"]["duty_work_log"]["status"] = "duty_work_log_saved"
            payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_saved"
            payload["site_statuses"]["consumables"]["status"] = "consumables_failed"
            payload["site_statuses"]["disinfection"]["status"] = "disinfection_failed"
            payload["overall_status"] = "desktop_fast_completed_with_errors"
            store.save_payload(request.task_id, payload)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
                return_value=SimpleNamespace(),
            ), patch(
                "ambulance_bot.desktop_fast_runner.open_consumable_record_for_task",
                return_value="saved",
            ), patch(
                "ambulance_bot.desktop_fast_runner.save_consumables_record_enabled",
                return_value=True,
            ), patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=SimpleNamespace(),
            ), patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(
                    ok=True,
                    status="disinfection_saved",
                    detail="saved",
                ),
            ):
                runner.start_site(request.task_id, "consumables")
                self.assertTrue(runner.wait_for_idle())
                first_retry = store.get(request.task_id)
                self.assertFalse(
                    task_completion_snapshot(first_retry)["all_complete"]
                )
                self.assertEqual(
                    first_retry["overall_status"],
                    "site_run_completed",
                )

                runner.start_site(request.task_id, "disinfection")
                self.assertTrue(runner.wait_for_idle())

            completed = store.get(request.task_id)
            self.assertTrue(task_completion_snapshot(completed)["all_complete"])
            self.assertEqual(
                completed["overall_status"],
                "desktop_fast_completed",
            )

    def test_four_site_run_skips_completed_sites_and_resumes_at_failed_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-4",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            store.update_site_result(
                "task-4",
                SimpleNamespace(key="duty_work_log", name="消防勤務工作紀錄", status="duty_work_log_saved", detail="done"),
            )
            store.update_site_result(
                "task-4",
                SimpleNamespace(key="vehicle_mileage", name="車輛里程", status="vehicle_mileage_saved", detail="done"),
            )
            store.update_site_result(
                "task-4",
                SimpleNamespace(key="disinfection", name="緊急救護消毒", status="disinfection_failed", detail="retry"),
            )
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_local_selenium_task",
            ) as duty_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
            ) as mileage_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                return_value=SimpleNamespace(ok=True, status="fuel_record_saved", detail="fuel skipped"),
            ) as fuel_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_disinfection_and_get_driver",
                return_value=Mock(name="disinfection_driver"),
            ) as disinfection_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.run_disinfection_task",
                return_value=SimpleNamespace(ok=True, status="disinfection_saved", detail="disinfection ok"),
            ) as disinfection_mock, patch(
                "ambulance_bot.desktop_fast_runner.login_acs_and_get_driver",
                return_value=Mock(name="driver"),
            ) as acs_login_mock, patch(
                "ambulance_bot.desktop_fast_runner.open_consumable_record_for_task",
                return_value="consumables ok",
            ) as consumables_mock:
                runner.start_existing("task-4")
                self.assertTrue(runner.wait_for_idle())

            payload = store.get("task-4")
            self.assertEqual(payload["overall_status"], "desktop_fast_completed")
            self.assertEqual(payload["site_statuses"]["duty_work_log"]["status"], "duty_work_log_saved")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
            self.assertEqual(payload["site_statuses"]["fuel_record"]["status"], "not_started")
            self.assertEqual(payload["site_statuses"]["disinfection"]["status"], "disinfection_saved")
            self.assertEqual(payload["site_statuses"]["consumables"]["status"], "consumables_saved")
            duty_mock.assert_not_called()
            mileage_mock.assert_not_called()
            fuel_mock.assert_not_called()
            disinfection_login_mock.assert_called_once()
            disinfection_mock.assert_called_once()
            acs_login_mock.assert_called_once()
            consumables_mock.assert_called_once()

    def test_vehicle_single_site_passes_update_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-update",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="\u65b0\u576191",
                mileage="200",
            )
            store.create(request)
            payload = store.get("task-update")
            context = {"previous_task": {**request.to_dict(), "mileage": "100"}, "current_task": request.to_dict()}
            payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_needs_update"
            payload["site_statuses"]["vehicle_mileage"]["update_context"] = context
            store.save_payload("task-update", payload)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                return_value=SimpleNamespace(ok=True, status="vehicle_mileage_saved", detail="mileage ok"),
            ) as mileage_mock:
                runner.start_site("task-update", "vehicle_mileage")
                self.assertTrue(runner.wait_for_idle())

            mileage_mock.assert_called_once()
            self.assertEqual(mileage_mock.call_args.kwargs["update_context"], context)

    def test_two_vehicle_site_rerun_skips_saved_vehicle_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = request_from_form(
                {
                    "two_vehicle": "1",
                    "vehicle": "\u65b0\u576191",
                    "driver": "\u66fe\u5f65\u7db8",
                    "return_time": "0200",
                    "mileage": "101",
                    "patient_summary": "\u7537\u4e00\u540d",
                    "consumables": "\u53e3\u7f69=2",
                    "vehicle_2": "\u65b0\u576192",
                    "driver_2": "\u9673\u5c0f\u660e",
                    "return_time_2": "0210",
                    "mileage_2": "202",
                    "patient_summary_2": "\u7121",
                    "consumables_2": "\u624b\u5957=2",
                }
            )
            request.task_id = "task-two-vehicle"
            store.create(request)
            payload = store.get("task-two-vehicle")
            payload["site_statuses"]["vehicle_mileage"]["status"] = "vehicle_mileage_failed"
            payload["site_statuses"]["vehicle_mileage"]["vehicle_results"] = {
                "\u65b0\u576191": {"status": "vehicle_mileage_saved", "detail": "first done"}
            }
            update_context = {
                "previous_task": {**request.to_dict(), "mileage_2": "201"},
                "current_task": request.to_dict(),
            }
            payload["site_statuses"]["vehicle_mileage"]["update_context"] = update_context
            store.save_payload("task-two-vehicle", payload)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch(
                "ambulance_bot.desktop_fast_runner.run_vehicle_mileage_task",
                return_value=SimpleNamespace(ok=True, status="vehicle_mileage_saved", detail="second ok"),
            ) as mileage_mock:
                runner.start_site("task-two-vehicle", "vehicle_mileage")
                self.assertTrue(runner.wait_for_idle())

            mileage_mock.assert_called_once()
            self.assertEqual(mileage_mock.call_args.args[0].vehicle, "\u65b0\u576192")
            self.assertEqual(mileage_mock.call_args.kwargs["update_context"]["vehicle_index"], 2)
            self.assertEqual(mileage_mock.call_args.kwargs["update_context"]["vehicle_key"], "\u65b0\u576192")
            self.assertEqual(mileage_mock.call_args.kwargs["update_context"]["previous_task"], update_context["previous_task"])
            payload = store.get("task-two-vehicle")
            self.assertEqual(payload["site_statuses"]["vehicle_mileage"]["status"], "vehicle_mileage_saved")
            self.assertEqual(
                payload["site_statuses"]["vehicle_mileage"]["vehicle_results"]["\u65b0\u576191"]["status"],
                "vehicle_mileage_saved",
            )
            self.assertEqual(
                payload["site_statuses"]["vehicle_mileage"]["vehicle_results"]["\u65b0\u576192"]["status"],
                "vehicle_mileage_saved",
            )

    def test_two_vehicle_fuel_retry_skips_saved_first_vehicle_and_keeps_second_slot_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = request_from_form(
                {
                    "two_vehicle": "1",
                    "vehicle": "\u65b0\u576192",
                    "driver": "\u7532",
                    "fuel_record": "1",
                    "fuel_date": "2026/07/13",
                    "fuel_time": "0915",
                    "fuel_quantity": "20.1",
                    "fuel_unit_price": "30.0",
                    "vehicle_2": "\u65b0\u576193",
                    "driver_2": "\u4e59",
                    "fuel_record_2": "1",
                    "fuel_date_2": "2026/07/13",
                    "fuel_time_2": "0920",
                    "fuel_quantity_2": "30.2",
                    "fuel_unit_price_2": "30.0",
                }
            )
            request.task_id = "task-two-fuel"
            store.create(request)
            payload = store.get(request.task_id)
            update_context = {
                "previous_task": request.to_dict(),
                "current_task": request.to_dict(),
            }
            payload["site_statuses"]["fuel_record"]["status"] = "fuel_record_failed"
            payload["site_statuses"]["fuel_record"]["update_context"] = update_context
            payload["site_statuses"]["fuel_record"]["vehicle_results"] = {
                "\u65b0\u576192": {"status": "fuel_record_saved", "detail": "first done"},
                "\u65b0\u576193": {"status": "fuel_record_failed", "detail": "retry"},
            }
            store.save_payload(request.task_id, payload)
            runner = DesktopFastRunner(Path(tmp), store=store)
            runner._running.add(request.task_id)
            owner = runner._prepare_execution(request.task_id, request.task_id, "busy")

            try:
                with patch(
                    "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                    return_value=SimpleNamespace(ok=True, status="fuel_record_saved", detail="second ok"),
                ) as fuel_mock:
                    result = runner._run_fuel_record(request, "task_two_fuel")
            finally:
                runner._finish_execution(request.task_id, owner, request.task_id, lambda: None)

            self.assertEqual(result.status, "fuel_record_saved")
            fuel_mock.assert_called_once()
            self.assertEqual(fuel_mock.call_args.args[0].vehicle, "\u65b0\u576193")
            self.assertEqual(fuel_mock.call_args.kwargs["update_context"]["vehicle_index"], 2)
            self.assertEqual(fuel_mock.call_args.kwargs["update_context"]["vehicle_key"], "\u65b0\u576193")
            saved = store.get(request.task_id)["site_statuses"]["fuel_record"]["vehicle_results"]
            self.assertEqual(saved["\u65b0\u576192"]["status"], "fuel_record_saved")
            self.assertEqual(saved["\u65b0\u576193"]["status"], "fuel_record_saved")

    def test_two_vehicle_fuel_runs_only_enabled_vehicle_but_preserves_original_slot_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = request_from_form(
                {
                    "two_vehicle": "1",
                    "vehicle": "\u65b0\u576192",
                    "driver": "\u7532",
                    "vehicle_2": "\u65b0\u576193",
                    "driver_2": "\u4e59",
                    "fuel_record_2": "1",
                    "fuel_date_2": "2026/07/13",
                    "fuel_time_2": "0920",
                    "fuel_quantity_2": "30.2",
                    "fuel_unit_price_2": "30.0",
                }
            )
            request.task_id = "task-second-fuel-only"
            store.create(request)
            payload = store.get(request.task_id)
            payload["site_statuses"]["fuel_record"]["update_context"] = {
                "previous_task": request.to_dict(),
                "current_task": request.to_dict(),
            }
            store.save_payload(request.task_id, payload)
            runner = DesktopFastRunner(Path(tmp), store=store)
            runner._running.add(request.task_id)
            owner = runner._prepare_execution(request.task_id, request.task_id, "busy")

            try:
                with patch(
                    "ambulance_bot.desktop_fast_runner.run_fuel_record_task",
                    return_value=SimpleNamespace(ok=True, status="fuel_record_saved", detail="ok"),
                ) as fuel_mock:
                    result = runner._run_fuel_record(request, "task_second_fuel_only")
            finally:
                runner._finish_execution(request.task_id, owner, request.task_id, lambda: None)

            self.assertEqual(result.status, "fuel_record_saved")
            fuel_mock.assert_called_once()
            self.assertEqual(fuel_mock.call_args.args[0].vehicle, "\u65b0\u576193")
            self.assertEqual(fuel_mock.call_args.kwargs["update_context"]["vehicle_index"], 2)

    def test_record_folders_ignore_env_and_use_existing_nas_record_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = DesktopFastRunner(Path(tmp))
            request = request_from_form(
                {
                    "case_date": "2026-06-02",
                    "case_time": "1010",
                    "vehicle": "\u65b0\u576191",
                }
            )

            with patch.dict("os.environ", {"AMBULANCE_RECORD_ROOT": r"W:\救護硬碟\救護登錄器及行車紀錄器"}), patch("pathlib.Path.mkdir"):
                detail = runner._ensure_record_folders(request)

            self.assertIn(str(DEFAULT_RECORD_ROOT), detail)
            self.assertIn("\u6551\u8b77\u5bc6\u9304\u5668\u53ca\u884c\u8eca\u7d00\u9304\u5668", detail)
            self.assertNotIn("\u6551\u8b77\u767b\u9304\u5668", detail)

    def test_runner_blocks_second_start_for_same_task_while_first_run_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonTaskStore(Path(tmp) / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-active",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="\u65b0\u576191",
            )
            store.create(request)
            runner = DesktopFastRunner(Path(tmp), store=store)

            with patch("ambulance_bot.desktop_fast_runner.threading.Thread") as thread_mock:
                runner.start_site("task-active", "consumables")
                runner.start_site("task-active", "vehicle_mileage")
                runner.start_existing("task-active")

            self.assertEqual(thread_mock.call_count, 1)

    def test_runner_is_global_single_flight_across_different_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            store = JsonTaskStore(artifacts_dir / "tasks")
            for task_id in ("task-a", "task-b"):
                store.create(
                    AmbulanceReturnRequest(
                        task_id=task_id,
                        created_at=__import__("datetime").datetime.now(),
                        raw_text="",
                        vehicle="新坡91",
                    )
                )
            runner = DesktopFastRunner(artifacts_dir, store=store)
            runner._running.add("task-a")
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, "desktop_fast:task-a"))

            with patch("ambulance_bot.desktop_fast_runner.threading.Thread") as thread_mock:
                runner.start_existing("task-b")
                runner.start_site("task-b", "consumables")

            self.assertEqual(thread_mock.call_count, 0)
            lock_payload = json.loads(manual_task_lock_path(artifacts_dir).read_text(encoding="utf-8"))
            self.assertEqual(lock_payload["owner"], "desktop_fast:task-a")

    def test_runner_refuses_cross_process_lock_without_starting_site_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            store = JsonTaskStore(artifacts_dir / "tasks")
            request = AmbulanceReturnRequest(
                task_id="task-foreign-lock",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                vehicle="新坡91",
            )
            store.create(request)
            self.assertTrue(acquire_manual_task_lock(artifacts_dir, "other-process"))
            runner = DesktopFastRunner(artifacts_dir, store=store)

            with patch.object(runner, "_run_site_group") as run_group:
                runner._running.add(request.task_id)
                runner._run(request.task_id)

            run_group.assert_not_called()
            self.assertEqual(store.get(request.task_id)["overall_status"], "created")
            self.assertEqual(runner._running, set())
            lock_payload = json.loads(manual_task_lock_path(artifacts_dir).read_text(encoding="utf-8"))
            self.assertEqual(lock_payload["owner"], "other-process")


if __name__ == "__main__":
    unittest.main()
