import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from functools import partial
from pathlib import Path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format, *args):
        return


class UpdatePackageIntegrationTests(unittest.TestCase):
    OLD_VERSION = "2026.07.13.1000"
    NEW_VERSION = "2026.07.13.2000"

    def _package_identity(self, package_dir: Path) -> str:
        normalized = str(package_dir.resolve()).rstrip("\\").lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    def _prepare_fixture(self, root: Path):
        source_updater = Path("WinPython_公務電腦使用包/update_package.ps1").resolve()
        source_wrapper = Path("WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1").resolve()
        source_finder = Path("WinPython_公務電腦使用包/find_winpython.ps1").resolve()
        source_headless_launcher = Path("WinPython_公務電腦使用包/run_worker_headless.bat").resolve()
        installed = root / "installed"
        release = root / "release"
        payload = root / "payload" / "WinPython_package"
        state = root / "state"
        temp_root = root / "temp"
        for path in (installed, release, payload, state, temp_root):
            path.mkdir(parents=True)

        shutil.copy2(source_updater, installed / "update_package.ps1")
        shutil.copy2(source_wrapper, installed / "REMOTE_UPDATE_PACKAGE.ps1")
        (installed / "worker_gui.py").write_text("OLD_WORKER = True\n", encoding="utf-8")
        (installed / "VERSION.txt").write_text(self.OLD_VERSION, encoding="utf-8")

        shutil.copy2(source_updater, payload / "update_package.ps1")
        shutil.copy2(source_wrapper, payload / "REMOTE_UPDATE_PACKAGE.ps1")
        shutil.copy2(source_finder, payload / "find_winpython.ps1")
        shutil.copy2(source_headless_launcher, payload / "run_worker_headless.bat")
        (payload / "worker_gui.py").write_text("NEW_WORKER = True\n", encoding="utf-8")
        (payload / "worker.py").write_text(
            """import json
import os
import time
from pathlib import Path

transaction = Path(os.environ["AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH"])
ready = Path(f"{transaction}.probe-{os.getpid()}.ready")
version = Path(__file__).with_name("VERSION.txt").read_text(encoding="utf-8-sig").strip()
marker_path = Path(os.environ["LOCALAPPDATA"]) / "AmbulanceReturnBot" / "remote_update_active.json"
phase_history_path = Path(__file__).with_name("phase_history.json")
controls = sorted(name for name in os.environ if name.startswith("AMBULANCE_") and "UPDATE" in name)
payload = {
    "pid": os.getpid(),
    "runtime_kind": "headless",
    "version": version,
    "transaction_path": str(transaction.resolve()),
    "inherited_update_controls": controls,
}
temp = Path(f"{ready}.tmp")
temp.write_text(json.dumps(payload), encoding="utf-8")
os.replace(temp, ready)
Path(__file__).with_name("probe_env.json").write_text(json.dumps(payload), encoding="utf-8")
phase_history = []
while transaction.exists():
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        entry = {"phase": marker.get("phase"), "phase_updated_at": marker.get("phase_updated_at")}
        if not phase_history or entry != phase_history[-1]:
            phase_history.append(entry)
            temporary_history = Path(f"{phase_history_path}.tmp")
            temporary_history.write_text(json.dumps(phase_history), encoding="utf-8")
            os.replace(temporary_history, phase_history_path)
    except (OSError, json.JSONDecodeError):
        pass
    time.sleep(0.05)
ready.unlink(missing_ok=True)
time.sleep(0.5)
Path(__file__).with_name("fixture_worker_exited.marker").write_text("ok", encoding="utf-8")
""",
            encoding="utf-8",
        )
        (payload / "VERSION.txt").write_text(self.NEW_VERSION, encoding="utf-8")
        (payload / "payload.bin").write_bytes(os.urandom(4096))
        (payload / "WORKER_SELF_RECOVERY.ps1").write_text(
            "param()\nWrite-Output 'fixture watchdog script'\n",
            encoding="utf-8",
        )
        (payload / "install_startup_shortcut.ps1").write_text(
            """param(
    [switch]$WhatIf,
    [switch]$SkipScheduledTask
)

@{
    skipped_scheduled_task = [bool]$SkipScheduledTask
    invoked_from = $PSScriptRoot
} | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $PSScriptRoot "watchdog_install_signal.json") -Encoding UTF8
if ($env:FIXTURE_WATCHDOG_INSTALLER_FAIL -eq "true") {
    Write-Error "fixture watchdog registration failed"
    exit 2
}
exit 0
""",
            encoding="utf-8",
        )
        managed = [
            "REMOTE_UPDATE_PACKAGE.ps1",
            "UPDATE_MANIFEST.json",
            "VERSION.txt",
            "WORKER_SELF_RECOVERY.ps1",
            "find_winpython.ps1",
            "install_startup_shortcut.ps1",
            "payload.bin",
            "run_worker_headless.bat",
            "update_package.ps1",
            "worker.py",
            "worker_gui.py",
        ]
        (payload / "UPDATE_MANIFEST.json").write_text(
            json.dumps({"schema_version": 1, "files": managed}, indent=2),
            encoding="utf-8",
        )

        zip_path = release / "ambulance-return-public-package.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in payload.rglob("*"):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(payload.parent))
        (release / "ambulance-return-version.txt").write_text(self.NEW_VERSION, encoding="utf-8")
        (release / "ambulance-return-public-package.zip.sha256.txt").write_text(
            hashlib.sha256(zip_path.read_bytes()).hexdigest(),
            encoding="ascii",
        )

        handler = partial(_QuietHandler, directory=str(release))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        env = os.environ.copy()
        env.update(
            {
                "AMBULANCE_RETURN_RELEASE_BASE_URL": base_url,
                "LOCALAPPDATA": str(state),
                "TEMP": str(temp_root),
                "TMP": str(temp_root),
                "WINPYTHON_DIR": str(Path(sys.executable).resolve().parent),
            }
        )
        for name in (
            "AMBULANCE_UPDATE_LOCK_HELD",
            "AMBULANCE_UPDATE_TRANSACTION_ACTION",
            "AMBULANCE_UPDATE_TRANSACTION_PATH",
            "AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH",
        ):
            env.pop(name, None)
        return installed, state, env, server, thread

    def _run_updater(self, installed: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(installed / "update_package.ps1"),
            ],
            cwd=installed,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )

    def _run_wrapper(
        self,
        installed: Path,
        env: dict[str, str],
        *,
        recover_transaction_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        args = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(installed / "REMOTE_UPDATE_PACKAGE.ps1"),
                "-RequestId",
                "integration-update",
                "-CallerRuntime",
                "headless",
            ]
        if recover_transaction_path is not None:
            args.extend(["-RecoverTransactionPath", str(recover_transaction_path)])
        return subprocess.run(
            args,
            cwd=installed,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )

    def _wait_for_fixture_worker_exit(self, installed: Path) -> None:
        if not (installed / "probe_env.json").is_file():
            return
        marker = installed / "fixture_worker_exited.marker"
        deadline = time.monotonic() + 5
        while not marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.is_file(), "Fixture worker did not exit before temporary directory cleanup.")

    def test_deferred_rollback_preflights_every_backup_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed, state, env, server, thread = self._prepare_fixture(Path(tmp))
            try:
                transaction_dir = state / "AmbulanceReturnBot" / "update_transactions"
                transaction_dir.mkdir(parents=True, exist_ok=True)
                transaction_path = transaction_dir / f"{self._package_identity(installed)}-test.json"
                update_env = {
                    **env,
                    "AMBULANCE_SKIP_WORKER_RESTART": "true",
                    "AMBULANCE_UPDATE_TRANSACTION_PATH": str(transaction_path),
                }
                updated = self._run_updater(installed, update_env)
                self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
                self.assertEqual((installed / "VERSION.txt").read_text(encoding="utf-8-sig").strip(), self.NEW_VERSION)
                descriptor = json.loads(transaction_path.read_text(encoding="utf-8-sig"))
                worker_record = next(item for item in descriptor["backed_up_files"] if item["path"] == "worker_gui.py")
                backup = Path(descriptor["rollback_dir"]) / worker_record["path"]
                original_backup = backup.read_bytes()
                backup.write_text("TAMPERED\n", encoding="utf-8")

                rollback_env = {
                    **env,
                    "AMBULANCE_UPDATE_TRANSACTION_PATH": str(transaction_path),
                    "AMBULANCE_UPDATE_TRANSACTION_ACTION": "rollback",
                }
                rejected = self._run_updater(installed, rollback_env)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertTrue(transaction_path.is_file())
                self.assertIn("NEW_WORKER", (installed / "worker_gui.py").read_text(encoding="utf-8"))

                backup.write_bytes(original_backup)
                rolled_back = self._run_updater(installed, rollback_env)
                self.assertEqual(rolled_back.returncode, 0, rolled_back.stdout + rolled_back.stderr)
                self.assertEqual((installed / "VERSION.txt").read_text(encoding="utf-8-sig").strip(), self.OLD_VERSION)
                self.assertIn("OLD_WORKER", (installed / "worker_gui.py").read_text(encoding="utf-8"))
                self.assertFalse(transaction_path.exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_manual_update_without_running_worker_finalizes_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed, state, env, server, thread = self._prepare_fixture(Path(tmp))
            try:
                updated = self._run_updater(installed, env)
                self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
                self.assertEqual((installed / "VERSION.txt").read_text(encoding="utf-8-sig").strip(), self.NEW_VERSION)
                transaction_dir = state / "AmbulanceReturnBot" / "update_transactions"
                self.assertEqual(list(transaction_dir.glob("*.json")), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_next_updater_rolls_back_interrupted_transaction_before_network_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed, state, env, server, thread = self._prepare_fixture(Path(tmp))
            transaction_dir = state / "AmbulanceReturnBot" / "update_transactions"
            transaction_dir.mkdir(parents=True, exist_ok=True)
            transaction_path = transaction_dir / f"{self._package_identity(installed)}-interrupted.json"
            deferred_env = {
                **env,
                "AMBULANCE_SKIP_WORKER_RESTART": "true",
                "AMBULANCE_UPDATE_TRANSACTION_PATH": str(transaction_path),
            }
            updated = self._run_updater(installed, deferred_env)
            self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
            self.assertTrue(transaction_path.is_file())

            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            recovery_env = env.copy()
            recovery_env.pop("AMBULANCE_SKIP_WORKER_RESTART", None)
            recovery_env.pop("AMBULANCE_UPDATE_TRANSACTION_PATH", None)
            recovered_then_offline = self._run_updater(installed, recovery_env)

            self.assertNotEqual(recovered_then_offline.returncode, 0)
            self.assertIn("Recovering interrupted update transaction", recovered_then_offline.stdout)
            self.assertEqual((installed / "VERSION.txt").read_text(encoding="utf-8-sig").strip(), self.OLD_VERSION)
            self.assertFalse(transaction_path.exists())

    def test_remote_wrapper_commits_only_after_new_headless_probe_and_does_not_leak_control_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed, state, env, server, thread = self._prepare_fixture(Path(tmp))
            try:
                with zipfile.ZipFile(Path(tmp) / "release" / "ambulance-return-public-package.zip") as archive:
                    self.assertIn("WinPython_package/WORKER_SELF_RECOVERY.ps1", archive.namelist())
                result = self._run_wrapper(installed, env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((installed / "VERSION.txt").read_text(encoding="utf-8-sig").strip(), self.NEW_VERSION)
                watchdog_install = json.loads(
                    (installed / "watchdog_install_signal.json").read_text(encoding="utf-8-sig")
                )
                self.assertTrue(watchdog_install["skipped_scheduled_task"])
                self.assertEqual(Path(watchdog_install["invoked_from"]).resolve(), installed.resolve())
                report = json.loads(
                    (state / "AmbulanceReturnBot" / "remote_update_result.json").read_text(encoding="utf-8-sig")
                )
                self.assertEqual(report["status"], "completed")
                self.assertNotIn("watchdog_install_warning", report)
                probe = json.loads((installed / "probe_env.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    probe["inherited_update_controls"],
                    ["AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH"],
                )
                phase_history = json.loads((installed / "phase_history.json").read_text(encoding="utf-8"))
                validation_heartbeats = {
                    entry["phase_updated_at"]
                    for entry in phase_history
                    if entry.get("phase") == "validating" and entry.get("phase_updated_at")
                }
                self.assertGreaterEqual(len(validation_heartbeats), 2)
                transaction_dir = state / "AmbulanceReturnBot" / "update_transactions"
                self.assertEqual(list(transaction_dir.glob("*.json")), [])
            finally:
                self._wait_for_fixture_worker_exit(installed)
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_updater_refreshes_watchdog_only_after_installed_tree_validation(self):
        updater = Path("WinPython_公務電腦使用包/update_package.ps1").read_text(encoding="utf-8")

        validation = updater.rindex("Assert-InstalledUpdateTree")
        launcher_refresh = updater.rindex("Install-StartupLaunchers")
        self.assertLess(validation, launcher_refresh)
        self.assertIn("-File $installer -SkipScheduledTask", updater)

    def test_remote_wrapper_records_nonfatal_watchdog_install_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed, state, env, server, thread = self._prepare_fixture(Path(tmp))
            try:
                result = self._run_wrapper(
                    installed,
                    {
                        **env,
                        "FIXTURE_WATCHDOG_INSTALLER_FAIL": "true",
                    },
                )

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((installed / "VERSION.txt").read_text(encoding="utf-8-sig").strip(), self.NEW_VERSION)
                report = json.loads(
                    (state / "AmbulanceReturnBot" / "remote_update_result.json").read_text(encoding="utf-8-sig")
                )
                self.assertEqual(report["status"], "completed")
                self.assertIn("watchdog_install_warning", report)
                self.assertIn("exited with code 2", report["watchdog_install_warning"])
            finally:
                self._wait_for_fixture_worker_exit(installed)
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_remote_recovery_never_restarts_a_mixed_package_when_backup_preflight_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed, state, env, server, thread = self._prepare_fixture(Path(tmp))
            try:
                transaction_dir = state / "AmbulanceReturnBot" / "update_transactions"
                transaction_dir.mkdir(parents=True, exist_ok=True)
                transaction_path = transaction_dir / f"{self._package_identity(installed)}-mixed.json"
                deferred = self._run_updater(
                    installed,
                    {
                        **env,
                        "AMBULANCE_SKIP_WORKER_RESTART": "true",
                        "AMBULANCE_UPDATE_TRANSACTION_PATH": str(transaction_path),
                    },
                )
                self.assertEqual(deferred.returncode, 0, deferred.stdout + deferred.stderr)
                descriptor = json.loads(transaction_path.read_text(encoding="utf-8-sig"))
                backup = Path(descriptor["rollback_dir"]) / "worker_gui.py"
                backup.write_text("CORRUPTED BACKUP\n", encoding="utf-8")

                recovered = self._run_wrapper(
                    installed,
                    env,
                    recover_transaction_path=transaction_path,
                )

                self.assertNotEqual(recovered.returncode, 0)
                self.assertTrue(transaction_path.is_file())
                self.assertFalse((installed / "probe_env.json").exists())
                report = json.loads(
                    (state / "AmbulanceReturnBot" / "remote_update_result.json").read_text(encoding="utf-8-sig")
                )
                self.assertEqual(report["status"], "failed")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
