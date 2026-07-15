import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


class WorkerSelfRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_patch = mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": self.tmp.name},
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.package_root = Path("WinPython_公務電腦使用包").resolve()
        self.script_path = self.package_root / "WORKER_SELF_RECOVERY.ps1"
        self.state_dir = Path(self.tmp.name) / "AmbulanceReturnBot"
        self.snapshot_path = Path(self.tmp.name) / "processes.json"
        self.worker_started_at = self._iso_age(300)
        self.updater_started_at = self._iso_age(240)

    @staticmethod
    def _iso_age(seconds: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()

    @staticmethod
    def _process(
        name: str,
        command_line: str | Path,
        *,
        pid: int,
        started_at: str,
    ) -> dict:
        return {
            "ProcessId": pid,
            "Name": name,
            "CommandLine": str(command_line),
            "CreationDate": started_at,
        }

    def _package_id(self) -> str:
        normalized = str(self.package_root).rstrip("\\/").lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    def _exact_worker_process(self) -> dict:
        worker_script = self.package_root / "worker_gui.py"
        return self._process(
            "pythonw.exe",
            f'"{self.package_root / "pythonw.exe"}" "{worker_script}"',
            pid=321,
            started_at=self.worker_started_at,
        )

    def _exact_updater_process(self) -> dict:
        script = self.package_root / "REMOTE_UPDATE_PACKAGE.ps1"
        return self._process(
            "powershell.exe",
            f'-File "{script}" -RequestId update-1',
            pid=654,
            started_at=self.updater_started_at,
        )

    def _exact_marker(
        self,
        *,
        age_seconds: int,
        transaction_path: Path | None = None,
    ) -> dict:
        started_at = datetime.fromisoformat(self.updater_started_at)
        return {
            "request_id": "update-1",
            "owner_pid": 654,
            "owner_nonce": "nonce-1",
            "owner_started_unix_ms": int(started_at.timestamp() * 1000),
            "script_path": str((self.package_root / "REMOTE_UPDATE_PACKAGE.ps1").resolve()),
            "package_path": str(self.package_root),
            "transaction_path": str(transaction_path or (Path(self.tmp.name) / "transaction.json")),
            "phase": "validating",
            "phase_started_at": self._iso_age(age_seconds + 60),
            "phase_updated_at": self._iso_age(age_seconds),
            "started_at_utc": self.updater_started_at,
        }

    def _write_state(
        self,
        heartbeat_age_seconds: int,
        *,
        activity: dict | None = None,
        active_marker: dict | None = None,
        include_process_started_at: bool = True,
        process_started_at: str | None = None,
    ) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        heartbeat = {
            "worker_id": "PC-01",
            "pid": 321,
            "package_path": str(self.package_root),
            "observed_at": self._iso_age(heartbeat_age_seconds),
        }
        if include_process_started_at:
            heartbeat["process_started_at"] = process_started_at or self.worker_started_at
        (self.state_dir / "worker_heartbeat.json").write_text(
            json.dumps(heartbeat),
            encoding="utf-8",
        )
        activity_path = self.state_dir / "worker_activity.json"
        if activity is not None:
            activity_path.write_text(
                json.dumps({**activity, "updated_at": self._iso_age(0)}),
                encoding="utf-8",
            )
        else:
            activity_path.unlink(missing_ok=True)
        marker_path = self.state_dir / "remote_update_active.json"
        if active_marker is not None:
            marker_path.write_text(
                json.dumps(active_marker),
                encoding="utf-8",
            )
        else:
            marker_path.unlink(missing_ok=True)

    def _write_recovery_history(self, offsets: list[int]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        entries = [self._iso_age(-offset) for offset in offsets]
        (self.state_dir / "self_recovery.json").write_text(
            json.dumps({"destructive_recoveries": entries}),
            encoding="utf-8",
        )

    def _write_recovery_transaction(self, *, request_id: str = "update-1") -> Path:
        transaction_dir = self.state_dir / "update_transactions"
        transaction_dir.mkdir(parents=True, exist_ok=True)
        transaction_path = transaction_dir / f"{self._package_id()}-{request_id}.json"
        transaction_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "prepared",
                    "package_id": self._package_id(),
                    "package_dir": str(self.package_root),
                    "request_id": request_id,
                    "owner_pid": 654,
                    "owner_nonce": "nonce-1",
                }
            ),
            encoding="utf-8",
        )
        return transaction_path

    def _watchdog_command(self, *, whatif: bool, snapshot: Path) -> list[str]:
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script_path),
        ]
        if whatif:
            command.append("-WhatIf")
        return command + ["-ProcessSnapshotPath", str(snapshot)]

    @staticmethod
    def _stop_lock_holder(process: subprocess.Popen[str]) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def _hold_update_lock(self) -> subprocess.Popen[str]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_dir / "package-update.lock"
        escaped_lock_path = str(lock_path).replace("'", "''")
        holder_script = (
            "$stream = [System.IO.File]::Open("
            f"'{escaped_lock_path}', "
            "[System.IO.FileMode]::OpenOrCreate, "
            "[System.IO.FileAccess]::ReadWrite, "
            "[System.IO.FileShare]::None); "
            "[Console]::Out.WriteLine('lock-held'); "
            "try { Start-Sleep -Seconds 20 } finally { $stream.Dispose() }"
        )
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", holder_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.addCleanup(self._stop_lock_holder, process)
        self.assertIsNotNone(process.stdout)
        self.assertEqual(process.stdout.readline().strip(), "lock-held")
        return process

    def _run_watchdog(
        self,
        *,
        heartbeat_age_seconds: int = 30,
        processes: list[dict] | None = None,
        activity: dict | None = None,
        active_marker: dict | None = None,
        snapshot_document: dict | None = None,
        include_process_started_at: bool = True,
        process_started_at: str | None = None,
        before_run: Callable[[], None] | None = None,
    ) -> dict:
        self._write_state(
            heartbeat_age_seconds,
            activity=activity,
            active_marker=active_marker,
            include_process_started_at=include_process_started_at,
            process_started_at=process_started_at,
        )
        snapshot = snapshot_document if snapshot_document is not None else {"processes": processes or []}
        self.snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        if before_run is not None:
            before_run()
        result = subprocess.run(
            self._watchdog_command(whatif=True, snapshot=self.snapshot_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(lines.__len__(), 1, result.stdout + result.stderr)
        output = json.loads(lines[0])
        self.assertEqual(
            set(output),
            {"decision", "reason", "matched_owner", "proposed_actions"},
        )
        return output

    def _get_marker_status(self) -> dict:
        escaped_script_path = str(self.script_path).replace("'", "''")
        escaped_snapshot_path = str(self.snapshot_path).replace("'", "''")
        command = (
            f". '{escaped_script_path}' -WhatIf -ProcessSnapshotPath "
            f"'{escaped_snapshot_path}'; Get-MarkerStatus | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertGreaterEqual(lines.__len__(), 2, result.stdout + result.stderr)
        return json.loads(lines[-1])

    def test_watchdog_whatif_takes_no_action_for_fresh_heartbeat(self):
        output = self._run_watchdog(heartbeat_age_seconds=30, processes=[])

        self.assertEqual(output["decision"], "no_action")
        self.assertEqual(output["proposed_actions"], [])

    def test_watchdog_whatif_keeps_busy_activity_and_healthy_update_untouched(self):
        busy = self._run_watchdog(
            heartbeat_age_seconds=121,
            activity={"activity": "case_lookup", "owner": "lookup"},
        )
        self.assertEqual(busy["decision"], "no_action_busy")
        self.assertEqual(busy["proposed_actions"], [])

        healthy_transaction = self._write_recovery_transaction()
        updating = self._run_watchdog(
            heartbeat_age_seconds=121,
            active_marker=self._exact_marker(
                age_seconds=30,
                transaction_path=healthy_transaction,
            ),
            processes=[self._exact_updater_process()],
        )
        self.assertEqual(updating["decision"], "healthy_update")
        self.assertEqual(updating["proposed_actions"], [])

    def test_watchdog_never_targets_foreign_process_or_pid_reuse(self):
        foreign_processes = (
            self._process(
                "python.exe",
                "C:/other/worker.py",
                pid=11,
                started_at=self.worker_started_at,
            ),
            self._process(
                "powershell.exe",
                "C:/other/REMOTE_UPDATE_PACKAGE.ps1",
                pid=12,
                started_at=self.updater_started_at,
            ),
            self._process(
                "chrome.exe",
                "--remote-debugging-port=9222",
                pid=13,
                started_at=self.worker_started_at,
            ),
            self._process(
                "python.exe",
                f'"{self.package_root / "python.exe"}" "{self.package_root / "worker.py"}"',
                pid=321,
                started_at=self._iso_age(0),
            ),
        )

        for process in foreign_processes:
            with self.subTest(process=process):
                output = self._run_watchdog(
                    heartbeat_age_seconds=121,
                    processes=[process],
                )
                self.assertEqual(output["proposed_actions"], [])
                self.assertIn(
                    output["decision"],
                    {"identity_uncertain", "no_exact_owner"},
                )

    def test_watchdog_rejects_command_mode_decoys(self):
        worker_decoy = self._process(
            "pythonw.exe",
            f'"{self.package_root / "pythonw.exe"}" -c "import runpy; runpy.run_path(r\'{self.package_root / "worker_gui.py"}\')"',
            pid=321,
            started_at=self.worker_started_at,
        )
        worker_output = self._run_watchdog(
            heartbeat_age_seconds=121,
            processes=[worker_decoy],
        )
        self.assertEqual(worker_output["decision"], "identity_uncertain")
        self.assertEqual(worker_output["proposed_actions"], [])

        transaction_path = self._write_recovery_transaction()
        updater_decoy = self._process(
            "powershell.exe",
            f'-Command "& \'{self.package_root / "REMOTE_UPDATE_PACKAGE.ps1"}\' -RequestId update-1"',
            pid=654,
            started_at=self.updater_started_at,
        )
        updater_output = self._run_watchdog(
            heartbeat_age_seconds=121,
            active_marker=self._exact_marker(
                age_seconds=601,
                transaction_path=transaction_path,
            ),
            processes=[updater_decoy],
        )
        self.assertEqual(updater_output["decision"], "identity_uncertain")
        self.assertEqual(updater_output["proposed_actions"], [])

    def test_watchdog_rejects_legacy_heartbeat_without_process_start_identity(self):
        output = self._run_watchdog(
            heartbeat_age_seconds=121,
            processes=[self._exact_worker_process()],
            include_process_started_at=False,
        )

        self.assertEqual(output["decision"], "identity_uncertain")
        self.assertEqual(output["proposed_actions"], [])

    def test_snapshot_path_requires_whatif(self):
        self.snapshot_path.write_text(json.dumps({"processes": []}), encoding="utf-8")

        result = subprocess.run(
            self._watchdog_command(whatif=False, snapshot=self.snapshot_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertNotEqual(result.returncode, 0)

    def test_watchdog_proposes_exact_worker_restart_only_when_stale(self):
        output = self._run_watchdog(
            heartbeat_age_seconds=121,
            processes=[self._exact_worker_process()],
        )

        self.assertEqual(output["decision"], "restart_stale_worker")
        self.assertEqual(output["proposed_actions"][0]["kind"], "restart_gui")

    def test_watchdog_proposes_exact_stale_update_recovery_only(self):
        transaction_path = self._write_recovery_transaction()
        output = self._run_watchdog(
            heartbeat_age_seconds=121,
            active_marker=self._exact_marker(
                age_seconds=601,
                transaction_path=transaction_path,
            ),
            processes=[self._exact_updater_process()],
        )

        self.assertEqual(output["decision"], "recover_stale_update")
        self.assertEqual(output["proposed_actions"][0]["kind"], "recover_stale_update")

    def test_watchdog_rejects_strict_recovery_without_phase_start_identity(self):
        transaction_path = self._write_recovery_transaction()
        marker = self._exact_marker(
            age_seconds=601,
            transaction_path=transaction_path,
        )
        marker.pop("phase_started_at")

        output = self._run_watchdog(
            heartbeat_age_seconds=121,
            active_marker=marker,
            processes=[self._exact_updater_process()],
        )

        self.assertEqual(output["decision"], "identity_uncertain")
        self.assertEqual(output["reason"], "update_owner_not_verified")
        self.assertEqual(output["proposed_actions"], [])

    def test_watchdog_does_not_classify_future_phase_times_as_strict(self):
        transaction_path = self._write_recovery_transaction()
        marker = self._exact_marker(
            age_seconds=1,
            transaction_path=transaction_path,
        )
        marker["phase_started_at"] = self._iso_age(-4)
        marker["phase_updated_at"] = self._iso_age(-5)
        self._write_state(
            30,
            active_marker=marker,
        )
        self.snapshot_path.write_text(json.dumps({"processes": []}), encoding="utf-8")

        status = self._get_marker_status()

        self.assertEqual(status["Mode"], "legacy")
        self.assertTrue(status["Valid"])

    def test_watchdog_treats_held_update_lock_as_expected_only_for_exact_stale_updater(self):
        transaction_path = self._write_recovery_transaction()
        output = self._run_watchdog(
            heartbeat_age_seconds=121,
            active_marker=self._exact_marker(
                age_seconds=601,
                transaction_path=transaction_path,
            ),
            processes=[self._exact_updater_process()],
            before_run=self._hold_update_lock,
        )

        self.assertEqual(output["decision"], "recover_stale_update")
        self.assertEqual(output["proposed_actions"][0]["kind"], "recover_stale_update")

    def test_watchdog_keeps_held_update_lock_for_nonmatching_state(self):
        output = self._run_watchdog(
            heartbeat_age_seconds=30,
            processes=[],
            before_run=self._hold_update_lock,
        )

        self.assertEqual(output["decision"], "update_lock_held")
        self.assertEqual(output["proposed_actions"], [])

    def test_watchdog_rate_limits_the_fourth_destructive_recovery(self):
        self._write_recovery_history([-100, -50, -10])

        output = self._run_watchdog(
            heartbeat_age_seconds=121,
            processes=[self._exact_worker_process()],
        )

        self.assertEqual(output["decision"], "recovery_rate_limited")
        self.assertEqual(output["proposed_actions"], [])

    def test_watchdog_fails_closed_for_cim_snapshot_error(self):
        output = self._run_watchdog(
            heartbeat_age_seconds=121,
            processes=[self._exact_worker_process()],
            snapshot_document={"error": "cim_timeout"},
        )

        self.assertEqual(output["decision"], "fail_closed")
        self.assertEqual(output["proposed_actions"], [])

    def test_whatif_does_not_write_recovery_history(self):
        self.assertFalse((self.state_dir / "self_recovery.json").exists())

        output = self._run_watchdog(
            heartbeat_age_seconds=121,
            processes=[self._exact_worker_process()],
        )

        self.assertEqual(output["decision"], "restart_stale_worker")
        self.assertFalse((self.state_dir / "self_recovery.json").exists())


if __name__ == "__main__":
    unittest.main()
