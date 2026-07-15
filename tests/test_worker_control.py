import json
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from ambulance_bot import worker_control, worker_health, worker_routes


def _verified_request_route() -> worker_routes.RequestRouteSnapshot:
    return worker_routes.RequestRouteSnapshot(
        "http://lan",
        "lan",
        "verified",
        "nas-a",
        "manual",
    )


def _verified_control_response(command: dict[str, object] | None = None) -> worker_routes.ControlResponse:
    payload: dict[str, object] = {"ok": True, "server": {"instance_id": "nas-a"}}
    if command is not None:
        payload["command"] = command
    return worker_routes.ControlResponse(payload, _verified_request_route())


class WorkerControlTests(unittest.TestCase):
    def _loop(
        self,
        tmp: str,
        *,
        client: mock.Mock,
        snapshot: worker_control.RuntimeSnapshot | None = None,
        interval_seconds: float = 10.0,
        status_refresh_seconds: float = 60.0,
    ) -> worker_control.WorkerControlLoop:
        self.env_patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        client.choice = worker_routes.RouteChoice(
            "http://lan",
            "http://tail",
            "lan",
            "verified",
            "nas-a",
            "both_paths_match",
        )
        current_snapshot = snapshot or worker_control.RuntimeSnapshot("idle", "", "", "")
        return worker_control.WorkerControlLoop(
            client=client,
            worker_id="PC-01",
            package_version=lambda: "2026.07.15.1326",
            package_path=lambda: "C:/Ambulance/WinPython_公務電腦使用包",
            execution_mode=lambda: "gui",
            snapshot=lambda: current_snapshot,
            mailbox_path=worker_health.worker_control_mailbox_path(),
            interval_seconds=interval_seconds,
            status_refresh_seconds=status_refresh_seconds,
        )

    def test_runtime_state_is_locked_and_rejects_unknown_heartbeat_state(self):
        state = worker_control.WorkerRuntimeState()
        state.set("busy", activity="case_lookup", busy_reason="querying", request_id="lookup-1")

        self.assertEqual(
            state.snapshot(),
            worker_control.RuntimeSnapshot("busy", "case_lookup", "querying", "lookup-1"),
        )
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            state.set("unexpected")

    def test_control_loop_writes_heartbeat_before_control_and_persists_sanitized_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = mock.Mock()
            payloads: list[dict[str, object]] = []

            def control(payload: dict[str, object]) -> dict[str, object]:
                self.assertTrue(worker_health.worker_heartbeat_path().exists())
                local_heartbeat = json.loads(
                    worker_health.worker_heartbeat_path().read_text(encoding="utf-8")
                )
                self.assertEqual(
                    local_heartbeat["process_started_at"],
                    payload["process_started_at"],
                )
                payloads.append(payload)
                return _verified_control_response(
                    {"request_id": "update-1", "status": "pending", "token": "must-not-persist"}
                )

            client.control.side_effect = control
            loop = self._loop(tmp, client=client)

            result = loop.run_once()
            loop.run_once()

            self.assertEqual(result["command"]["request_id"], "update-1")
            self.assertEqual(loop.pending_command()["request_id"], "update-1")
            persisted = json.loads(worker_health.worker_control_mailbox_path().read_text(encoding="utf-8"))
            self.assertNotIn("token", json.dumps(persisted))
            self.assertEqual(payloads[0]["process_started_at"], payloads[1]["process_started_at"])
            self.assertEqual(payloads[0]["state"], "idle")

    def test_control_loop_discards_command_without_request_route_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = mock.Mock()
            client.control.return_value = {
                "ok": True,
                "server": {"instance_id": "nas-a"},
                "command": {"request_id": "update-1", "status": "pending"},
            }
            loop = self._loop(tmp, client=client)

            result = loop.run_once()

            self.assertEqual(result["command"]["request_id"], "update-1")
            self.assertFalse(worker_health.worker_control_mailbox_path().exists())
            self.assertIsNone(loop.pending_command())

    def test_control_loop_discards_first_command_from_unverified_bootstrap_snapshot(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            choice = worker_routes.RouteChoice(
                "http://lan",
                "",
                "lan",
                "unverified",
                instance_id,
                "single_route_unverified",
                "builtin",
            )
            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=lambda _url, _payload: {
                    "ok": True,
                    "server": {"instance_id": instance_id},
                    "command": {"request_id": "update-1", "status": "pending"},
                },
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )
            loop = worker_control.WorkerControlLoop(
                client=client,
                worker_id="PC-01",
                package_version=lambda: "2026.07.15.1326",
                package_path=lambda: "C:/Ambulance/WinPython",
                execution_mode=lambda: "gui",
                snapshot=lambda: worker_control.RuntimeSnapshot("idle", "", "", ""),
                mailbox_path=worker_health.worker_control_mailbox_path(),
            )

            result = loop.run_once()

            self.assertEqual(result["command"]["request_id"], "update-1")
            self.assertEqual(client.choice.identity_status, "verified")
            self.assertFalse(worker_health.worker_control_mailbox_path().exists())
            self.assertIsNone(loop.pending_command())

    def test_control_loop_sends_waiting_status_once_until_refresh_and_clears_matching_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = mock.Mock()
            client.control.return_value = _verified_control_response()
            loop = self._loop(tmp, client=client, status_refresh_seconds=3600.0)

            loop.set_remote_update_waiting("update-1", "waiting_idle", "waiting for user idle")
            loop.run_once()
            loop.run_once()

            first_payload = client.control.call_args_list[0].args[0]
            second_payload = client.control.call_args_list[1].args[0]
            self.assertEqual(first_payload["remote_update"]["request_id"], "update-1")
            self.assertNotIn("remote_update", second_payload)
            self.assertFalse(loop.clear_command("other-request"))

            loop._write_mailbox({"request_id": "update-1", "status": "pending"}, _verified_request_route())
            self.assertTrue(loop.clear_command("update-1"))
            self.assertIsNone(loop.pending_command())

    def test_control_loop_rejects_mailbox_command_from_unverified_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = mock.Mock()
            loop = self._loop(tmp, client=client)
            worker_health.write_json_atomic(
                worker_health.worker_control_mailbox_path(),
                {
                    "command": {"request_id": "update-1", "status": "pending"},
                    "received_at": "2026-07-15T12:00:00+00:00",
                    "route": {"name": "manual", "identity_status": "unverified", "instance_id": ""},
                },
            )

            self.assertIsNone(loop.pending_command())

    def test_control_loop_rejects_mailbox_command_from_different_nas_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = mock.Mock()
            loop = self._loop(tmp, client=client)
            worker_health.write_json_atomic(
                worker_health.worker_control_mailbox_path(),
                {
                    "command": {"request_id": "update-1", "status": "pending"},
                    "received_at": "2026-07-15T12:00:00+00:00",
                    "route": {"name": "lan", "identity_status": "verified", "instance_id": "old-nas"},
                },
            )

            self.assertIsNone(loop.pending_command())

    def test_control_loop_clears_terminal_mailbox_even_when_route_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = mock.Mock()
            loop = self._loop(tmp, client=client)
            mailbox_path = worker_health.worker_control_mailbox_path()
            worker_health.write_json_atomic(
                mailbox_path,
                {
                    "command": {"request_id": "update-1", "status": "failed"},
                    "received_at": "2026-07-15T12:00:00+00:00",
                    "route": {"name": "manual", "identity_status": "unverified", "instance_id": ""},
                },
            )

            self.assertIsNone(loop.pending_command())
            self.assertFalse(mailbox_path.exists())

    def test_control_loop_survives_network_failure_and_stop_interrupts_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempted = threading.Event()
            client = mock.Mock()

            def offline(_payload: dict[str, object]) -> None:
                attempted.set()
                raise urllib.error.URLError("offline")

            client.control.side_effect = offline
            loop = self._loop(tmp, client=client, interval_seconds=60.0)

            loop.start()
            self.assertTrue(attempted.wait(0.5))
            loop.stop(timeout_seconds=0.5)

            self.assertFalse(loop._thread.is_alive())

    def test_control_loop_stop_writes_the_current_stopping_heartbeat_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = mock.Mock()
            loop = self._loop(
                tmp,
                client=client,
                snapshot=worker_control.RuntimeSnapshot("stopping", "", "", ""),
            )

            loop.stop()

            heartbeat = json.loads(worker_health.worker_heartbeat_path().read_text(encoding="utf-8"))
            self.assertEqual(heartbeat["state"], "stopping")
            client.control.assert_not_called()

    def test_control_loop_serializes_mailbox_writes_with_main_thread_reads_and_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = mock.Mock()
            loop = self._loop(tmp, client=client)
            wrote_mailbox = threading.Event()
            original_write = worker_health.write_json_atomic

            def observe_write(path: Path, payload: dict[str, object]) -> None:
                if Path(path) == worker_health.worker_control_mailbox_path():
                    wrote_mailbox.set()
                original_write(path, payload)

            with mock.patch.object(worker_health, "write_json_atomic", side_effect=observe_write):
                with loop._mailbox_lock:
                    writer = threading.Thread(
                        target=loop._write_mailbox,
                        args=({"request_id": "update-1", "status": "pending"}, _verified_request_route()),
                    )
                    writer.start()
                    write_was_blocked = not wrote_mailbox.wait(0.1)

                writer.join(timeout=0.5)

            self.assertFalse(writer.is_alive())
            self.assertTrue(write_was_blocked)
            self.assertTrue(wrote_mailbox.is_set())

    def test_control_loop_retries_after_transient_local_heartbeat_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = mock.Mock()
            control_attempted = threading.Event()

            def control(_payload: dict[str, object]) -> dict[str, object]:
                control_attempted.set()
                return {"ok": True, "server": {"instance_id": "nas-a"}}

            client.control.side_effect = control
            loop = self._loop(tmp, client=client, interval_seconds=0.1)
            original_write = worker_health.write_json_atomic
            attempts = 0

            def flaky_write(path: Path, payload: dict[str, object]) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("temporary state storage failure")
                original_write(path, payload)

            with mock.patch.object(worker_health, "write_json_atomic", side_effect=flaky_write):
                loop.start()
                self.assertTrue(control_attempted.wait(0.5))
                loop.stop(timeout_seconds=0.5)

            self.assertFalse(loop._thread.is_alive())
            self.assertGreaterEqual(attempts, 2)
            client.control.assert_called()


if __name__ == "__main__":
    unittest.main()
