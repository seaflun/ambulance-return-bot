from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from dotenv import load_dotenv

from consumables_login import login_acs_and_get_driver, open_consumable_record_for_task, save_consumables_record_enabled
from disinfect import login_and_get_driver as login_disinfection_and_get_driver
from ambulance_bot import worker_control, worker_health, worker_routes
from ambulance_bot.adapters import SITE_DEFINITIONS, SiteAutomationResult
from ambulance_bot.credential_envelope import open_credential_payload
from ambulance_bot.duty_credentials import save_credential_sync_payload
from ambulance_bot.login_audit import login_audit_for_site, with_login_audit
from ambulance_bot.manual_task_lock import (
    acquire_manual_task_lock,
    bind_manual_task_lock_task,
    clear_manual_task_lock,
    manual_task_lock_active,
    manual_task_lock_max_age_seconds,
    refresh_manual_task_lock,
)
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.selenium_local import (
    query_duty_emergency_cases,
    run_disinfection_task,
    run_fuel_record_task,
    run_local_selenium_task,
    run_vehicle_mileage_task,
)
from ambulance_bot.site_diagnostics import DIAGNOSTIC_FIELDS, diagnostic_payload, make_site_result
from ambulance_bot.status_outbox import WorkerStatusOutbox
from ambulance_bot.task_cancellation import (
    TaskCancellationError,
    clear_task_cancellation,
    task_cancellation_requested,
)
from ambulance_bot.task_store import task_completion_snapshot
from ambulance_bot.update_safety import ManualUpdateRequiredError, require_safe_automated_update
from ambulance_bot.window_layout import maximize_worker_site_windows


load_dotenv()

MANUAL_TASK_ACTIVE = threading.Event()
SITE_NAMES = {site.key: site.name for site in SITE_DEFINITIONS}
MAX_PARALLEL_SITE_GROUPS = 2
MAX_EXECUTION_LEASE_HEARTBEAT_ERRORS = 3
WORKER_NAS_LAN_URL = "http://10.30.65.30:8080"
WORKER_NAS_TAILSCALE_URL = "http://100.114.126.58:8080"
_TASK_CLAIM_CONTEXT: dict[str, dict[str, str]] = {}
_TASK_CLAIM_CONTEXT_LOCK = threading.Lock()
_STATUS_DELIVERY_LOCK = threading.RLock()
_STATUS_DELIVERY_RETRY_AFTER: dict[str, float] = {}
_STALE_TASK_CLAIMS: dict[str, str] = {}
_STALE_TASK_CLAIMS_LOCK = threading.Lock()
_TASK_CANCELLATION_EVENTS: dict[str, dict[threading.Event, str]] = {}
_TASK_CANCELLATION_EVENTS_LOCK = threading.Lock()
_TASK_EXECUTION_LOCK = threading.Lock()
_EXECUTION_LEASES: dict[
    str,
    tuple[Path, str, threading.Event, threading.Thread, threading.Event],
] = {}
_EXECUTION_LEASES_LOCK = threading.Lock()


class StaleWorkerClaimError(TaskCancellationError):
    """The NAS has fenced this worker out of a reassigned task."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"worker claim is stale for task {task_id}")
        self.task_id = task_id


def task_site_count_label(request: AmbulanceReturnRequest) -> str:
    return "五站" if request.has_fuel_record() else "四站"


def main() -> None:
    server_url = os.getenv("WORKER_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
    worker_id = os.getenv("WORKER_ID", socket.gethostname() or "public-duty-pc")
    poll_seconds = int(os.getenv("WORKER_POLL_SECONDS", "10"))
    lookup_interval_seconds = max(1800, int(os.getenv("CASE_LOOKUP_INTERVAL_SECONDS", "1800")))
    run_once = os.getenv("WORKER_RUN_ONCE", "false").strip().lower() in {"1", "true", "yes", "on"}
    auto_claim_tasks = os.getenv("WORKER_AUTO_CLAIM_TASKS", "false").strip().lower() in {"1", "true", "yes", "on"}
    artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
    last_case_lookup_at = time.time()
    last_case_hash = load_last_case_hash(artifacts_dir)

    if os.getenv("WORKER_USE_LOCAL_CHROME", "true").strip().lower() not in {"0", "false", "no", "off"}:
        os.environ["SELENIUM_REMOTE_URL"] = ""
        os.environ.setdefault("SELENIUM_DETACH", "true")

    probe_outcome = wait_for_update_probe_gate()
    if probe_outcome == "recovery":
        print("[worker] interrupted probe recovery launched; stopping worker loop", flush=True)
        return
    if maybe_recover_interrupted_update():
        print("[worker] interrupted update recovery launched; stopping worker loop", flush=True)
        return

    runtime_state = worker_control.WorkerRuntimeState()
    control = build_worker_control_loop(server_url, worker_id, artifacts_dir, runtime_state)
    print(f"[worker] starting worker_id={worker_id} server={server_url}", flush=True)
    try:
        runtime_state.set("starting")
        control.start()
        while True:
            try:
                runtime_state.set("idle")
                flush_status_outbox(server_url)
                try:
                    report_remote_update_result(server_url, worker_id)
                except Exception as exc:
                    print(f"[worker] remote update result report deferred: {exc}", flush=True)
                maybe_run_credential_sync(server_url)
                runtime_state.set("busy", activity="case_lookup", busy_reason="checking case lookup")
                try:
                    last_case_lookup_at, last_case_hash = maybe_run_case_lookup(
                        server_url,
                        artifacts_dir,
                        last_case_lookup_at,
                        last_case_hash,
                        lookup_interval_seconds,
                    )
                finally:
                    runtime_state.set("idle")
                if auto_claim_tasks:
                    execution_key = "__auto_claim__"
                    execution_event = begin_manual_task_execution(execution_key, artifacts_dir)
                    if execution_event is not None:
                        try:
                            runtime_state.set("busy", activity="task_execution", busy_reason="checking queued task")
                            task = fetch_next_task(server_url, worker_id)
                            if task is not None:
                                task_id = str(task.get("task_id") or "").strip()
                                if task_id:
                                    _rebind_task_execution(execution_key, task_id, execution_event)
                                    execution_key = task_id
                                runtime_state.set(
                                    "busy",
                                    activity="task_execution",
                                    busy_reason="running assigned task",
                                    request_id=task_id,
                                )
                                run_all_sites_task(server_url, worker_id, task, artifacts_dir)
                                if run_once:
                                    return
                                continue
                        finally:
                            runtime_state.set("idle")
                            end_manual_task_execution(execution_key, execution_event, artifacts_dir)
                command = control.pending_command()
                if command is not None:
                    request_id = str(command.get("request_id") or "").strip()
                    runtime_state.set("idle", request_id=request_id)
                    if maybe_start_remote_update(
                        server_url,
                        worker_id,
                        artifacts_dir,
                        command,
                        waiting_status=control.set_remote_update_waiting,
                    ):
                        runtime_state.set("update_handoff", request_id=request_id)
                        control.clear_command(request_id)
                        print("[worker] remote update active; stopping worker loop", flush=True)
                        return
                if run_once:
                    print("[worker] no queued task", flush=True)
                    return
                time.sleep(poll_seconds)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                runtime_state.set("idle")
                print(f"[worker] loop error: {exc}", flush=True)
                if run_once:
                    return
                time.sleep(poll_seconds)
    finally:
        runtime_state.set("stopping")
        control.stop(timeout_seconds=2.0)


def remote_update_idle_seconds() -> int:
    try:
        return max(30, int(os.getenv("REMOTE_UPDATE_IDLE_SECONDS", "120")))
    except ValueError:
        return 120


def worker_control_interval_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("WORKER_CONTROL_INTERVAL_SECONDS", "10")))
    except ValueError:
        return 10.0


def worker_control_route_choice(server_url: str) -> worker_routes.RouteChoice:
    primary_url = str(server_url or "").strip().rstrip("/")
    fallback_url = os.getenv("WORKER_SERVER_FALLBACK_URL", "").strip().rstrip("/")
    identity_status = os.getenv("WORKER_SERVER_IDENTITY_STATUS", "unverified").strip()
    instance_id = os.getenv("WORKER_SERVER_INSTANCE_ID", "").strip()
    provenance = os.getenv("WORKER_SERVER_ROUTE_PROVENANCE", "").strip().lower()
    diagnostic = os.getenv("WORKER_SERVER_ROUTE_DIAGNOSTIC", "").strip()
    if provenance != "builtin":
        provenance = "manual"
    if diagnostic not in {"single_route_unverified", "single_route_known_instance_mismatch"}:
        diagnostic = "worker_environment"
    if diagnostic == "single_route_known_instance_mismatch":
        fallback_url = ""
        identity_status = "unverified"
    elif identity_status != "verified" or not instance_id:
        identity_status = "unverified"
    if provenance != "builtin" or primary_url not in {WORKER_NAS_LAN_URL, WORKER_NAS_TAILSCALE_URL}:
        provenance = "manual"
        fallback_url = ""
        identity_status = "unverified"
        route_name = "manual"
        diagnostic = "worker_environment"
    elif primary_url == WORKER_NAS_LAN_URL:
        route_name = "lan"
    else:
        route_name = "tailscale"
    return worker_routes.RouteChoice(
        primary_url,
        fallback_url,
        route_name,
        identity_status,
        instance_id,
        diagnostic,
        provenance,
    )


def build_worker_control_loop(
    server_url: str,
    worker_id: str,
    artifacts_dir: Path,
    runtime_state: worker_control.WorkerRuntimeState,
) -> worker_control.WorkerControlLoop:
    choice = worker_control_route_choice(server_url)
    client_options: dict[str, object] = {
        "request_json": request_json,
        "post_json": post_json,
    }
    if (
        choice.provenance == "builtin"
        and choice.primary_url in {WORKER_NAS_LAN_URL, WORKER_NAS_TAILSCALE_URL}
        and choice.route_name in {"lan", "tailscale"}
        and choice.identity_status == "unverified"
        and not choice.fallback_url
        and choice.diagnostic == "single_route_unverified"
    ):
        client_options["bootstrap_url"] = choice.primary_url
        client_options["bootstrap_route_name"] = choice.route_name
    client = worker_routes.WorkerControlClient(
        choice,
        **client_options,
    )
    return worker_control.WorkerControlLoop(
        client=client,
        worker_id=worker_id,
        package_version=current_package_version,
        package_path=lambda: str(Path(__file__).resolve().parent),
        execution_mode=current_worker_runtime_kind,
        snapshot=lambda: worker_control_runtime_snapshot(runtime_state, artifacts_dir),
        mailbox_path=worker_health.worker_control_mailbox_path(),
        interval_seconds=worker_control_interval_seconds(),
        process_started_at=worker_process_started_at(),
    )


def worker_control_runtime_snapshot(
    runtime_state: worker_control.WorkerRuntimeState,
    artifacts_dir: Path,
) -> worker_control.RuntimeSnapshot:
    snapshot = runtime_state.snapshot()
    if snapshot.state in {"busy", "update_handoff", "recovering", "stopping"}:
        return snapshot
    busy_reason = remote_update_busy_reason(artifacts_dir)
    if not busy_reason:
        return snapshot
    activity = snapshot.activity or "manual_task"
    return worker_control.RuntimeSnapshot("busy", activity, busy_reason, snapshot.request_id)


def remote_update_busy_reason(artifacts_dir: Path) -> str:
    if MANUAL_TASK_ACTIVE.is_set() or manual_task_lock_active(artifacts_dir):
        return "勤務登打仍在執行。"
    if worker_health.activity_is_fresh(90.0):
        return "Worker 仍在執行案件查詢或勤務"
    if remote_update_marker_is_healthy():
        return "既有遠端更新程序仍在執行"
    request_path = artifacts_dir / "cases" / "request.json"
    if not request_path.exists():
        return ""
    try:
        request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if str(request_payload.get("status") or "") == "case_lookup_requested":
        return "案件查詢仍在執行。"
    return ""


def windows_user_idle_seconds(
    *,
    last_input_tick: Callable[[], int] | None = None,
    current_tick: Callable[[], int] | None = None,
) -> float:
    if last_input_tick is None or current_tick is None:
        if os.name != "nt":
            return float("inf")

        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        last_input_tick = lambda: int(info.dwTime)
        current_tick = lambda: int(ctypes.windll.kernel32.GetTickCount())
    elapsed_milliseconds = (int(current_tick()) - int(last_input_tick())) & 0xFFFFFFFF
    return elapsed_milliseconds / 1000.0


def launch_remote_update(
    request_id: str,
    *,
    package_dir: Path | None = None,
    popen: Callable[..., object] | None = None,
    recover_transaction_path: Path | None = None,
) -> None:
    root = package_dir or Path(__file__).resolve().parent
    wrapper = root / "REMOTE_UPDATE_PACKAGE.ps1"
    if not wrapper.exists():
        raise RuntimeError(f"找不到遠端更新包裝器：{wrapper}")
    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(wrapper),
        "-RequestId",
        request_id,
        "-CallerRuntime",
        current_worker_runtime_kind(),
    ]
    if recover_transaction_path is not None:
        args.extend(["-RecoverTransactionPath", str(recover_transaction_path)])
    (popen or subprocess.Popen)(
        args,
        cwd=root,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def current_worker_runtime_kind() -> str:
    configured = os.getenv("WORKER_RUNTIME_MODE", "").strip().lower()
    if configured in {"gui", "headless"}:
        return configured
    executable_name = Path(sys.argv[0]).name.lower()
    return "gui" if executable_name in {"worker_gui.py", "worker_gui.pyw"} else "headless"


def update_state_root() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data)
    temp_dir = os.getenv("TEMP", "").strip()
    return Path(temp_dir) if temp_dir else Path.home() / "AppData" / "Local"


def package_update_identity(package_dir: Path | None = None) -> str:
    root = (package_dir or Path(__file__).resolve().parent).resolve()
    normalized = str(root).rstrip("\\").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _read_pending_update_transaction(
    path: Path,
    package_dir: Path,
    *,
    state_root: Path | None = None,
) -> dict[str, object]:
    expected_dir = ((state_root or update_state_root()) / "AmbulanceReturnBot" / "update_transactions").resolve()
    expected_prefix = package_update_identity(package_dir) + "-"
    if path.resolve().parent != expected_dir or not path.name.startswith(expected_prefix) or path.suffix != ".json":
        raise RuntimeError(f"pending update transaction path is unsafe: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"pending update transaction is unreadable: {path}: {exc}") from exc
    expected_package = package_dir.resolve()
    try:
        payload_package = Path(str(payload.get("package_dir") or "")).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"pending update transaction package path is invalid: {path}") from exc
    if (
        payload.get("schema_version") != 2
        or str(payload.get("phase") or "") != "prepared"
        or str(payload.get("package_id") or "") != package_update_identity(package_dir)
        or os.path.normcase(str(payload_package)) != os.path.normcase(str(expected_package))
    ):
        raise RuntimeError(f"pending update transaction does not belong to this package: {path}")
    owner_pid = payload.get("owner_pid", 0)
    owner_nonce = str(payload.get("owner_nonce") or "")
    if not isinstance(owner_pid, int) or owner_pid < 0 or len(owner_nonce) > 128:
        raise RuntimeError(f"pending update transaction has invalid owner metadata: {path}")
    expected_heartbeat = f"{path.resolve()}.owner.heartbeat"
    if os.path.normcase(str(payload.get("owner_heartbeat_path") or "")) != os.path.normcase(expected_heartbeat):
        raise RuntimeError(f"pending update transaction has an invalid heartbeat path: {path}")
    return payload


def find_pending_update_transaction(
    *,
    package_dir: Path | None = None,
    state_root: Path | None = None,
) -> tuple[Path, dict[str, object]] | None:
    root = (package_dir or Path(__file__).resolve().parent).resolve()
    transaction_dir = (state_root or update_state_root()) / "AmbulanceReturnBot" / "update_transactions"
    prefix = package_update_identity(root) + "-"
    paths = sorted(transaction_dir.glob(f"{prefix}*.json")) if transaction_dir.is_dir() else []
    if len(paths) > 1:
        raise RuntimeError("multiple pending update transactions require manual recovery")
    if not paths:
        return None
    return paths[0], _read_pending_update_transaction(paths[0], root, state_root=state_root)


def wait_for_update_probe_gate(
    *,
    package_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    launch_update: Callable[..., None] | None = None,
    heartbeat_timeout_seconds: float = 10.0,
) -> str:
    raw_path = os.getenv("AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH", "").strip()
    if not raw_path:
        return "none"
    root = (package_dir or Path(__file__).resolve().parent).resolve()
    transaction_path = Path(raw_path).resolve()
    payload = _read_pending_update_transaction(transaction_path, root)
    version_path = root / "VERSION.txt"
    installed_version = version_path.read_text(encoding="utf-8-sig").strip() if version_path.is_file() else "0"
    if str(payload.get("new_version") or "") != installed_version:
        raise RuntimeError("update probe version does not match the pending transaction")
    runtime_kind = current_worker_runtime_kind()
    ready_path = Path(f"{transaction_path}.probe-{os.getpid()}.ready")
    write_json_atomic(
        ready_path,
        {
            "pid": os.getpid(),
            "runtime_kind": runtime_kind,
            "version": installed_version,
            "transaction_path": str(transaction_path),
        },
    )
    print(f"[worker] update probe ready kind={runtime_kind} pid={os.getpid()}", flush=True)
    owner_pid = int(payload.get("owner_pid") or 0)
    owner_nonce = str(payload.get("owner_nonce") or "")
    heartbeat_path = Path(f"{transaction_path}.owner.heartbeat")
    stale_since: float | None = None
    try:
        while transaction_path.exists():
            heartbeat_current = False
            try:
                heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8-sig"))
                heartbeat_current = (
                    int(heartbeat.get("owner_pid") or 0) == owner_pid
                    and str(heartbeat.get("owner_nonce") or "") == owner_nonce
                    and time.time() - heartbeat_path.stat().st_mtime <= heartbeat_timeout_seconds
                    and process_id_is_running(owner_pid)
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                heartbeat_current = False
            if heartbeat_current:
                stale_since = None
            else:
                stale_since = stale_since or time.monotonic()
                if time.monotonic() - stale_since >= heartbeat_timeout_seconds:
                    request_id = str(payload.get("request_id") or "interrupted-update").strip() or "interrupted-update"
                    clear_update_control_environment()
                    (launch_update or launch_remote_update)(
                        request_id,
                        package_dir=root,
                        recover_transaction_path=transaction_path,
                    )
                    return "recovery"
            sleep(0.25)
    finally:
        ready_path.unlink(missing_ok=True)
        clear_update_control_environment()
    print("[worker] update probe committed; starting normal worker loop", flush=True)
    return "committed"


def clear_update_control_environment() -> None:
    for name in (
        "AMBULANCE_SKIP_WORKER_RESTART",
        "AMBULANCE_UPDATE_LOCK_HELD",
        "AMBULANCE_UPDATE_TRANSACTION_PATH",
        "AMBULANCE_UPDATE_TRANSACTION_ACTION",
        "AMBULANCE_UPDATE_PROBE_TRANSACTION_PATH",
        "AMBULANCE_UPDATE_REQUEST_ID",
        "AMBULANCE_RESTART_GUI_INTENT",
        "AMBULANCE_RESTART_HEADLESS_INTENT",
        "AMBULANCE_UPDATE_OWNER_PID",
        "AMBULANCE_UPDATE_OWNER_NONCE",
    ):
        os.environ.pop(name, None)


def process_id_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def maybe_recover_interrupted_update(
    *,
    package_dir: Path | None = None,
    state_root: Path | None = None,
    launch_update: Callable[..., None] | None = None,
) -> bool:
    pending = find_pending_update_transaction(package_dir=package_dir, state_root=state_root)
    if pending is None:
        return False
    transaction_path, payload = pending
    request_id = str(payload.get("request_id") or "interrupted-update").strip() or "interrupted-update"
    launcher = launch_update or launch_remote_update
    launcher(
        request_id,
        package_dir=package_dir,
        recover_transaction_path=transaction_path,
    )
    return True


def remote_update_result_path() -> Path:
    return update_state_root() / "AmbulanceReturnBot" / "remote_update_result.json"


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def report_remote_update_result(
    server_url: str,
    worker_id: str,
    *,
    post_command_status: Callable[..., None] | None = None,
    reported_at: Callable[[], str] | None = None,
) -> bool:
    path = remote_update_result_path()
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or str(payload.get("reported_at") or "").strip():
        return False
    request_id = str(payload.get("request_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    if not request_id or status not in {"completed", "up_to_date", "failed"}:
        return False
    post = post_command_status or post_remote_update_status
    timestamp = (reported_at or (lambda: time.strftime("%Y-%m-%dT%H:%M:%S")))()
    try:
        post(
            server_url,
            request_id,
            status,
            str(payload.get("detail") or "遠端更新已結束。"),
            worker_id=worker_id,
            before_version=str(payload.get("before_version") or ""),
            installed_version=str(payload.get("installed_version") or ""),
            exit_code=payload.get("exit_code"),
        )
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        payload["reported_at"] = timestamp
        payload["report_error"] = str(exc)
        write_json_atomic(path, payload)
        return False
    payload["reported_at"] = timestamp
    write_json_atomic(path, payload)
    return True


def maybe_start_remote_update(
    server_url: str,
    worker_id: str,
    artifacts_dir: Path,
    command: Mapping[str, object],
    *,
    post_command_status: Callable[..., None] | None = None,
    idle_seconds: Callable[[], float] | None = None,
    launch_update: Callable[[str], None] | None = None,
    active_update_check: Callable[[str], bool] | None = None,
    waiting_status: Callable[[str, str, str], None] | None = None,
    route_verified: bool = True,
) -> bool:
    if not route_verified:
        print("[worker] remote update command ignored: route is not verified", flush=True)
        return False
    post = post_command_status or post_remote_update_status
    idle = idle_seconds or windows_user_idle_seconds
    launch = launch_update or launch_remote_update
    request_id = str(command.get("request_id") or "").strip()
    if not request_id:
        return False
    status = str(command.get("status") or "").strip()
    if status in {"completed", "up_to_date", "failed", "timed_out"}:
        return False
    if status == "updating":
        is_active = (active_update_check or remote_update_marker_is_healthy)(request_id)
        if is_active:
            return True
        post(
            server_url,
            request_id,
            "failed",
            "背景更新程序已中斷，公務電腦已恢復接案；請重新發出更新。",
            worker_id=worker_id,
        )
        return False
    busy_reason = remote_update_busy_reason(artifacts_dir)
    if busy_reason:
        if waiting_status is not None:
            waiting_status(request_id, "waiting_busy", busy_reason)
        else:
            post(server_url, request_id, "waiting_busy", busy_reason, worker_id=worker_id)
        return False
    minimum_idle = remote_update_idle_seconds()
    actual_idle = max(0.0, float(idle()))
    if actual_idle < minimum_idle:
        detail = f"等待電腦停止操作滿 {minimum_idle} 秒。"
        if waiting_status is not None:
            waiting_status(request_id, "waiting_idle", detail)
        else:
            post(server_url, request_id, "waiting_idle", detail, worker_id=worker_id)
        return False
    post(server_url, request_id, "updating", "安全條件已符合，開始背景更新。", worker_id=worker_id)
    try:
        launch(request_id)
    except Exception as exc:
        post(server_url, request_id, "failed", f"無法啟動背景更新：{exc}", worker_id=worker_id)
        return False
    return True


def maybe_run_remote_update(
    server_url: str,
    worker_id: str,
    artifacts_dir: Path,
    *,
    fetch_command: Callable[[str, str], dict[str, object] | None] | None = None,
    post_command_status: Callable[..., None] | None = None,
    idle_seconds: Callable[[], float] | None = None,
    launch_update: Callable[[str], None] | None = None,
    active_update_check: Callable[[str], bool] | None = None,
) -> bool:
    fetch = fetch_command or fetch_remote_update_command
    command = fetch(server_url, worker_id)
    if not isinstance(command, Mapping):
        return False
    return maybe_start_remote_update(
        server_url,
        worker_id,
        artifacts_dir,
        command,
        post_command_status=post_command_status,
        idle_seconds=idle_seconds,
        launch_update=launch_update,
        active_update_check=active_update_check,
    )


def remote_update_active_path() -> Path:
    return update_state_root() / "AmbulanceReturnBot" / "remote_update_active.json"


REMOTE_UPDATE_MARKER_PHASES = frozenset(
    {
        "discovering_runtime",
        "installing",
        "validating",
        "committing",
        "rolling_back",
        "restarting",
    }
)


def remote_update_wrapper_is_active(request_id: str, *, max_age_seconds: float = 3600.0) -> bool:
    path = remote_update_active_path()
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return (
            _remote_update_marker_owner_is_active(payload, request_id)
            and 0 <= time.time() - path.stat().st_mtime <= max(0.0, float(max_age_seconds))
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def remote_update_marker_is_healthy(
    request_id: str | None = None,
    *,
    max_age_seconds: float = 600.0,
) -> bool:
    path = remote_update_active_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    marker_request_id = payload.get("request_id")
    if not isinstance(marker_request_id, str) or not marker_request_id.strip():
        return False
    expected_request_id = str(request_id or marker_request_id).strip()
    if not expected_request_id:
        return False
    if not _remote_update_marker_owner_is_active(payload, expected_request_id):
        return False
    if not _remote_update_marker_path_matches(payload.get("script_path"), Path(__file__).with_name("REMOTE_UPDATE_PACKAGE.ps1")):
        return False
    if not _remote_update_marker_path_matches(payload.get("package_path"), Path(__file__).parent):
        return False
    if not _remote_update_marker_transaction_path_is_safe(payload.get("transaction_path")):
        return False
    phase_value = payload.get("phase")
    if not isinstance(phase_value, str) or phase_value not in REMOTE_UPDATE_MARKER_PHASES:
        return False
    phase_started_at = _parse_remote_update_marker_time(payload.get("phase_started_at"))
    phase_updated_at = _parse_remote_update_marker_time(payload.get("phase_updated_at"))
    if phase_started_at is None or phase_updated_at is None or phase_started_at > phase_updated_at:
        return False
    age_seconds = (datetime.now(timezone.utc) - phase_updated_at).total_seconds()
    return 0 <= age_seconds <= max(0.0, float(max_age_seconds))


def _remote_update_marker_owner_is_active(payload: object, request_id: str) -> bool:
    if not isinstance(payload, dict):
        return False
    try:
        marker_request_id = payload.get("request_id")
        owner_pid = payload.get("owner_pid")
        expected_start = payload.get("owner_started_unix_ms")
        owner_nonce = payload.get("owner_nonce")
        if (
            not isinstance(marker_request_id, str)
            or not marker_request_id.strip()
            or not isinstance(owner_pid, int)
            or isinstance(owner_pid, bool)
            or owner_pid <= 0
            or not isinstance(expected_start, int)
            or isinstance(expected_start, bool)
            or expected_start <= 0
            or not isinstance(owner_nonce, str)
            or not owner_nonce.strip()
        ):
            return False
        actual_start = process_start_unix_ms(owner_pid)
        return (
            marker_request_id == str(request_id or "")
            and process_id_is_running(owner_pid)
            and actual_start is not None
            and abs(actual_start - expected_start) <= 10
        )
    except (TypeError, ValueError):
        return False


def _remote_update_marker_path_matches(value: object, expected_path: Path) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        actual_candidate = Path(value)
        if not actual_candidate.is_absolute():
            return False
        actual = actual_candidate.resolve()
        expected = Path(expected_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return os.path.normcase(str(actual)) == os.path.normcase(str(expected))


def _remote_update_marker_transaction_path_is_safe(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value == "":
        return True
    try:
        transaction_candidate = Path(value)
        if not transaction_candidate.is_absolute():
            return False
        transaction_path = transaction_candidate.resolve()
        transaction_dir = (update_state_root() / "AmbulanceReturnBot" / "update_transactions").resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    prefix = package_update_identity(Path(__file__).parent) + "-"
    return (
        transaction_path.parent == transaction_dir
        and transaction_path.suffix == ".json"
        and transaction_path.name.startswith(prefix)
    )


def _parse_remote_update_marker_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def process_start_unix_ms(pid: int) -> int | None:
    if pid <= 0:
        return None
    if os.name != "nt":
        try:
            stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            start_ticks = int(stat_fields[19])
            boot_line = next(
                line for line in Path("/proc/stat").read_text(encoding="ascii").splitlines() if line.startswith("btime ")
            )
            boot_seconds = int(boot_line.split()[1])
            ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
            return int((boot_seconds + start_ticks / ticks_per_second) * 1000)
        except (OSError, ValueError, StopIteration, IndexError):
            return None

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        creation = FileTime()
        exit_time = FileTime()
        kernel = FileTime()
        user = FileTime()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (int(creation.high) << 32) | int(creation.low)
        return (ticks - 116444736000000000) // 10000
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def worker_process_started_at() -> str:
    started_unix_ms = process_start_unix_ms(os.getpid())
    if started_unix_ms is not None:
        return datetime.fromtimestamp(started_unix_ms / 1000, timezone.utc).isoformat(timespec="milliseconds")
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def maybe_run_case_lookup(
    server_url: str,
    artifacts_dir: Path,
    last_lookup_at: float,
    last_case_hash: str,
    interval_seconds: int,
) -> tuple[float, str]:
    if MANUAL_TASK_ACTIVE.is_set() or manual_task_lock_active(artifacts_dir):
        print("[worker] scheduled case lookup skipped: manual task active", flush=True)
        return last_lookup_at, last_case_hash

    request_payload = fetch_case_lookup_request(server_url)
    now = time.time()
    manual_lookup = request_payload is not None
    request_id = ""
    if manual_lookup:
        lookup_range = "24h"
        source = str(request_payload.get("source") or "NAS端")
        request_id = str(request_payload.get("request_id") or "").strip()
        print(f"[worker] manual case lookup requested range={lookup_range} source={source}", flush=True)
    elif now - last_lookup_at >= max(interval_seconds, 60):
        if last_case_lookup_waiting_for_login(artifacts_dir):
            print("[worker] scheduled case lookup skipped: waiting for valid duty login", flush=True)
            return now, last_case_hash
        lookup_range = "24h"
        print(f"[worker] scheduled case lookup range={lookup_range}", flush=True)
    else:
        return last_lookup_at, last_case_hash

    activity_owner = f"case_lookup:{os.getpid()}:{request_id or int(now)}"
    worker_health.write_activity(activity="case_lookup", owner=activity_owner)
    try:
        result = query_duty_emergency_cases(artifacts_dir, lookup_range=lookup_range)
        print(f"[worker] case lookup result status={result.status} count={len(result.cases)} detail={result.detail}", flush=True)
        case_hash = hash_cases(result.cases)
        if not manual_lookup and case_hash == last_case_hash:
            print("[worker] case lookup unchanged; skip posting", flush=True)
            return now, last_case_hash
        post_cases(
            server_url,
            result.status,
            result.detail,
            lookup_range,
            result.cases,
            case_hash,
            request_id=request_id,
        )
        print(f"[worker] case lookup posted count={len(result.cases)}", flush=True)
        return now, case_hash
    finally:
        worker_health.clear_activity(activity_owner)


def fetch_next_task(server_url: str, worker_id: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/next-task?worker_id={urllib.parse.quote(worker_id)}"
    data = request_json(url)
    task = data.get("task") if data.get("ok") else None
    if not isinstance(task, dict):
        return None
    response_payload = data.get("payload")
    worker_queue = data.get("worker_queue")
    if not isinstance(worker_queue, dict) and isinstance(response_payload, dict):
        worker_queue = response_payload.get("worker_queue")
    if not isinstance(worker_queue, dict):
        worker_queue = task.get("worker_queue")
    _remember_task_claim(task, worker_queue, fallback_worker_id=worker_id)
    return task


def claim_task(server_url: str, task_id: str, worker_id: str) -> dict[str, object] | None:
    body = json.dumps({"worker_id": str(worker_id or "").strip()}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{str(server_url).rstrip('/')}/worker/tasks/{urllib.parse.quote(str(task_id), safe='')}/claim",
        data=body,
        headers={**worker_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=worker_api_timeout()) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = worker_api_error_message(exc)
        exc.close()
        raise RuntimeError(message) from exc
    task = data.get("task") if data.get("ok") else None
    if not isinstance(task, dict):
        return None
    response_payload = data.get("payload")
    worker_queue = data.get("worker_queue")
    if not isinstance(worker_queue, dict) and isinstance(response_payload, dict):
        worker_queue = response_payload.get("worker_queue")
    _remember_task_claim(task, worker_queue, fallback_worker_id=worker_id)
    task = dict(task)
    if isinstance(response_payload, dict):
        task["_worker_payload"] = response_payload
    return task


def _remember_task_claim(
    task: dict[str, object],
    worker_queue: object,
    *,
    fallback_worker_id: str = "",
) -> None:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id or not isinstance(worker_queue, dict):
        return
    claim_id = str(worker_queue.get("claim_id") or "").strip()
    claimed_worker_id = str(worker_queue.get("worker_id") or fallback_worker_id or "").strip()
    if not claim_id:
        return
    _clear_task_claim_stale(task_id)
    with _TASK_CLAIM_CONTEXT_LOCK:
        _TASK_CLAIM_CONTEXT[task_id] = {
            "claim_id": claim_id,
            "worker_id": claimed_worker_id,
        }
    _bind_task_cancellation_events_to_claim(task_id, claim_id)


def _task_claim_context(task_id: str) -> dict[str, str]:
    with _TASK_CLAIM_CONTEXT_LOCK:
        return dict(_TASK_CLAIM_CONTEXT.get(str(task_id or "").strip(), {}))


def _clear_task_claim_context_if_matches(task_id: str, expected_claim_id: str) -> bool:
    """Clear only the claim generation owned by the finishing execution."""

    key = str(task_id or "").strip()
    expected = str(expected_claim_id or "").strip()
    if not key or not expected:
        return False
    context_cleared = False
    with _TASK_CLAIM_CONTEXT_LOCK:
        current_claim_id = str(_TASK_CLAIM_CONTEXT.get(key, {}).get("claim_id") or "").strip()
        if current_claim_id == expected:
            _TASK_CLAIM_CONTEXT.pop(key, None)
            context_cleared = True
    stale_cleared = False
    with _STALE_TASK_CLAIMS_LOCK:
        if _STALE_TASK_CLAIMS.get(key) == expected:
            _STALE_TASK_CLAIMS.pop(key, None)
            stale_cleared = True
    return context_cleared or stale_cleared


def _mark_task_claim_stale(task_id: str, claim_id: str = "") -> None:
    key = str(task_id or "").strip()
    stale_claim_id = str(claim_id or "").strip()
    if not key or not stale_claim_id:
        return
    with _TASK_CANCELLATION_EVENTS_LOCK:
        registrations = tuple(_TASK_CANCELLATION_EVENTS.get(key, {}).items())
        current_claim_id = str(_task_claim_context(key).get("claim_id") or "").strip()
        matching_events = tuple(
            event
            for event, registered_claim_id in registrations
            if registered_claim_id == stale_claim_id
            or (not registered_claim_id and current_claim_id == stale_claim_id)
        )
        if current_claim_id != stale_claim_id and not matching_events:
            # A delayed response from an older claim must not poison the current
            # task generation after that older execution has already finished.
            return
        with _STALE_TASK_CLAIMS_LOCK:
            _STALE_TASK_CLAIMS[key] = stale_claim_id
        for event in matching_events:
            event.set()


def _clear_task_claim_stale(task_id: str) -> None:
    with _STALE_TASK_CLAIMS_LOCK:
        _STALE_TASK_CLAIMS.pop(str(task_id or "").strip(), None)


def _task_claim_is_stale(task_id: str, claim_id: str = "") -> bool:
    key = str(task_id or "").strip()
    current_claim_id = str(claim_id or _task_claim_context(key).get("claim_id") or "").strip()
    with _STALE_TASK_CLAIMS_LOCK:
        if key not in _STALE_TASK_CLAIMS:
            return False
        stale_claim_id = _STALE_TASK_CLAIMS[key]
    return not stale_claim_id or not current_claim_id or stale_claim_id == current_claim_id


def _raise_if_task_claim_stale(task_id: str, claim_id: str = "") -> None:
    if _task_claim_is_stale(task_id, claim_id):
        raise StaleWorkerClaimError(str(task_id or "").strip())


def _bind_task_cancellation_events_to_claim(task_id: str, claim_id: str) -> None:
    key = str(task_id or "").strip()
    normalized_claim_id = str(claim_id or "").strip()
    if not key or not normalized_claim_id:
        return
    with _TASK_CANCELLATION_EVENTS_LOCK:
        registrations = _TASK_CANCELLATION_EVENTS.get(key)
        if registrations is None:
            return
        for event, registered_claim_id in tuple(registrations.items()):
            if not registered_claim_id:
                registrations[event] = normalized_claim_id


def _register_task_cancellation_event(
    task_id: str,
    event: threading.Event,
    claim_id: str | None = None,
) -> None:
    key = str(task_id or "").strip()
    if not key:
        return
    source_claim_id = _task_claim_context(key).get("claim_id") if claim_id is None else claim_id
    normalized_claim_id = str(source_claim_id or "").strip()
    with _TASK_CANCELLATION_EVENTS_LOCK:
        _TASK_CANCELLATION_EVENTS.setdefault(key, {})[event] = normalized_claim_id


def _unregister_task_cancellation_event(task_id: str, event: threading.Event) -> str:
    key = str(task_id or "").strip()
    with _TASK_CANCELLATION_EVENTS_LOCK:
        registrations = _TASK_CANCELLATION_EVENTS.get(key)
        if registrations is None:
            return ""
        registered_claim_id = str(registrations.pop(event, "") or "").strip()
        if not registrations:
            _TASK_CANCELLATION_EVENTS.pop(key, None)
        return registered_claim_id


def begin_manual_task_execution(
    task_id: str,
    artifacts_dir: Path | None = None,
) -> threading.Event | None:
    """Acquire the in-process and cross-process execution lease for a GUI task."""

    key = str(task_id or "").strip()
    if not key:
        return None
    effective_artifacts_dir = artifacts_dir or Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
    if MANUAL_TASK_ACTIVE.is_set() or manual_task_lock_active(effective_artifacts_dir):
        return None
    MANUAL_TASK_ACTIVE.set()
    if not _TASK_EXECUTION_LOCK.acquire(blocking=False):
        MANUAL_TASK_ACTIVE.clear()
        return None
    owner = f"worker-manual:{key}:{os.getpid()}:{threading.get_ident()}:{uuid4().hex}"
    try:
        acquired = acquire_manual_task_lock(effective_artifacts_dir, owner)
    except OSError as exc:
        print(f"[worker] execution lease acquire failed task={key}: {exc}", flush=True)
        _TASK_EXECUTION_LOCK.release()
        MANUAL_TASK_ACTIVE.clear()
        return None
    if not acquired:
        _TASK_EXECUTION_LOCK.release()
        MANUAL_TASK_ACTIVE.clear()
        return None
    event = threading.Event()
    registered = False
    try:
        # A new execution has not received its claim generation yet. Never inherit
        # a stale claim context left by an earlier run of the same task.
        _register_task_cancellation_event(key, event, claim_id="")
        registered = True
        heartbeat_stop, heartbeat_thread = _start_execution_lease_heartbeat(
            effective_artifacts_dir,
            owner,
            key,
            event,
        )
        with _EXECUTION_LEASES_LOCK:
            _EXECUTION_LEASES[key] = (
                effective_artifacts_dir,
                owner,
                heartbeat_stop,
                heartbeat_thread,
                event,
            )
        return event
    except Exception as exc:
        print(f"[worker] execution lease startup failed task={key}: {exc}", flush=True)
        if registered:
            _unregister_task_cancellation_event(key, event)
        try:
            clear_task_cancellation(
                effective_artifacts_dir,
                key,
                execution_owner=owner,
            )
        finally:
            try:
                clear_manual_task_lock(effective_artifacts_dir, owner)
            except OSError as cleanup_exc:
                print(
                    f"[worker] execution lease startup cleanup failed task={key}: {cleanup_exc}",
                    flush=True,
                )
            finally:
                with _EXECUTION_LEASES_LOCK:
                    lease = _EXECUTION_LEASES.get(key)
                    if lease is not None and lease[4] is event:
                        _EXECUTION_LEASES.pop(key, None)
                MANUAL_TASK_ACTIVE.clear()
                if _TASK_EXECUTION_LOCK.locked():
                    _TASK_EXECUTION_LOCK.release()
        return None


def worker_execution_lease_heartbeat_seconds() -> float:
    try:
        return max(0.01, float(os.getenv("WORKER_EXECUTION_LEASE_HEARTBEAT_SECONDS", "30")))
    except ValueError:
        return 30.0


def _start_execution_lease_heartbeat(
    artifacts_dir: Path,
    owner: str,
    task_id: str,
    cancellation_event: threading.Event | None = None,
) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()
    interval = min(
        worker_execution_lease_heartbeat_seconds(),
        max(
            0.01,
            manual_task_lock_max_age_seconds()
            / (MAX_EXECUTION_LEASE_HEARTBEAT_ERRORS + 1),
        ),
    )

    def heartbeat() -> None:
        consecutive_errors = 0
        while not stop.wait(interval):
            try:
                refreshed = refresh_manual_task_lock(artifacts_dir, owner)
            except OSError as exc:
                consecutive_errors += 1
                print(
                    f"[worker] execution lease heartbeat retry "
                    f"task={task_id} attempt={consecutive_errors}: {exc}",
                    flush=True,
                )
                if consecutive_errors >= MAX_EXECUTION_LEASE_HEARTBEAT_ERRORS:
                    print(
                        f"[worker] execution lease heartbeat unavailable task={task_id}; cancelling",
                        flush=True,
                    )
                    if cancellation_event is not None:
                        _cancel_execution_generation_events(task_id, cancellation_event)
                    return
                continue
            consecutive_errors = 0
            if not refreshed:
                print(f"[worker] execution lease owner lost task={task_id}; cancelling", flush=True)
                if cancellation_event is not None:
                    _cancel_execution_generation_events(task_id, cancellation_event)
                return

    thread = threading.Thread(
        target=heartbeat,
        name=f"worker-execution-lease:{task_id}",
        daemon=True,
    )
    thread.start()
    return stop, thread


def _cancel_execution_generation_events(
    task_id: str,
    primary_event: threading.Event,
) -> None:
    """Cancel only events registered to the active lease's claim generation."""

    primary_event.set()
    key = str(task_id or "").strip()
    with _TASK_CANCELLATION_EVENTS_LOCK:
        registrations = _TASK_CANCELLATION_EVENTS.get(key) if key else None
        if not registrations or primary_event not in registrations:
            registrations = next(
                (
                    candidate_registrations
                    for candidate_registrations in _TASK_CANCELLATION_EVENTS.values()
                    if primary_event in candidate_registrations
                ),
                None,
            )
        if not registrations or primary_event not in registrations:
            return
        primary_claim_id = registrations[primary_event]
        for event, registered_claim_id in tuple(registrations.items()):
            if registered_claim_id == primary_claim_id:
                event.set()


def _rebind_task_execution(
    previous_task_id: str,
    task_id: str,
    event: threading.Event,
) -> None:
    previous_key = str(previous_task_id or "").strip()
    key = str(task_id or "").strip()
    if not key or key == previous_key:
        return
    with _EXECUTION_LEASES_LOCK:
        lease = _EXECUTION_LEASES.get(previous_key)
    if lease is None or lease[4] is not event:
        event.set()
        raise StaleWorkerClaimError(key)
    if not bind_manual_task_lock_task(lease[0], lease[1], key):
        event.set()
        raise StaleWorkerClaimError(key)
    _unregister_task_cancellation_event(previous_key, event)
    _register_task_cancellation_event(
        key,
        event,
        claim_id=str(_task_claim_context(key).get("claim_id") or "").strip(),
    )
    with _EXECUTION_LEASES_LOCK:
        current_lease = _EXECUTION_LEASES.get(previous_key)
        if current_lease is not lease:
            event.set()
            raise StaleWorkerClaimError(key)
        _EXECUTION_LEASES.pop(previous_key, None)
        _EXECUTION_LEASES[key] = lease


def end_manual_task_execution(
    task_id: str,
    event: threading.Event,
    artifacts_dir: Path | None = None,
) -> None:
    key = str(task_id or "").strip()
    registered_claim_id = _unregister_task_cancellation_event(key, event)
    with _EXECUTION_LEASES_LOCK:
        lease = _EXECUTION_LEASES.get(key)
    owns_lease = lease is not None and lease[4] is event
    if not owns_lease:
        _clear_task_claim_context_if_matches(key, registered_claim_id)
        return
    assert lease is not None
    effective_artifacts_dir = lease[0]
    owner = lease[1]
    lease_removed = False
    try:
        try:
            heartbeat_stop, heartbeat_thread = lease[2], lease[3]
            heartbeat_stop.set()
            if threading.current_thread() is not heartbeat_thread:
                heartbeat_thread.join(timeout=2.0)
        finally:
            try:
                clear_task_cancellation(
                    effective_artifacts_dir,
                    key,
                    execution_owner=owner,
                    claim_id=registered_claim_id,
                )
            finally:
                _clear_task_claim_context_if_matches(key, registered_claim_id)
    finally:
        try:
            for _attempt in range(3):
                try:
                    if clear_manual_task_lock(effective_artifacts_dir, owner):
                        break
                except OSError as exc:
                    print(f"[worker] execution lease cleanup retry task={key}: {exc}", flush=True)
                time.sleep(0.05)
        finally:
            with _EXECUTION_LEASES_LOCK:
                if _EXECUTION_LEASES.get(key) is lease:
                    _EXECUTION_LEASES.pop(key, None)
                    lease_removed = True
            if lease_removed:
                MANUAL_TASK_ACTIVE.clear()
                if _TASK_EXECUTION_LOCK.locked():
                    _TASK_EXECUTION_LOCK.release()


def _raise_if_task_cancelled(task_id: str, event: threading.Event) -> None:
    key = str(task_id or "").strip()
    with _EXECUTION_LEASES_LOCK:
        lease = _EXECUTION_LEASES.get(key)
    claim_id = str(_task_claim_context(key).get("claim_id") or "").strip()
    if lease is not None and lease[4] is not event:
        with _TASK_CANCELLATION_EVENTS_LOCK:
            registered_claim_id = str(
                _TASK_CANCELLATION_EVENTS.get(key, {}).get(event) or ""
            ).strip()
        if not registered_claim_id or registered_claim_id != claim_id:
            lease = None
        elif lease[4].is_set():
            event.set()
    effective_artifacts_dir = lease[0] if lease else Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
    execution_owner = lease[1] if lease else ""
    if task_cancellation_requested(
        effective_artifacts_dir,
        key,
        execution_owner=execution_owner,
        claim_id=claim_id,
    ):
        event.set()
    if event.is_set():
        raise StaleWorkerClaimError(key)
    _raise_if_task_claim_stale(key)


def _assert_task_payload_claim_current(task_id: str, payload: dict[str, object]) -> None:
    context = _task_claim_context(task_id)
    expected_claim_id = str(context.get("claim_id") or "").strip()
    if not expected_claim_id:
        return
    queue_state = payload.get("worker_queue")
    actual_claim_id = str(queue_state.get("claim_id") or "").strip() if isinstance(queue_state, dict) else ""
    actual_status = str(queue_state.get("status") or "").strip() if isinstance(queue_state, dict) else ""
    if actual_status == "claimed" and actual_claim_id == expected_claim_id:
        return
    _mark_task_claim_stale(task_id, expected_claim_id)
    raise StaleWorkerClaimError(str(task_id or "").strip())


def fetch_task(server_url: str, task_id: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/tasks/{urllib.parse.quote(task_id)}"
    data = request_json(url)
    return data.get("task") if data.get("ok") else None


def fetch_task_payload(server_url: str, task_id: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/tasks/{urllib.parse.quote(task_id)}"
    data = request_json(url)
    payload = data.get("payload") if data.get("ok") else None
    return payload if isinstance(payload, dict) else None


def worker_completion_log_line(
    payload: dict[str, object],
    task_id: str,
) -> str:
    snapshot = task_completion_snapshot(dict(payload or {}))
    if not snapshot["all_complete"]:
        return ""
    return f"{snapshot['site_count_label']}｜完成｜{task_id}"


def print_worker_completion_if_reached(
    server_url: str,
    task_id: str,
) -> str:
    try:
        payload = fetch_task_payload(server_url, task_id)
    except Exception as exc:
        print(
            f"[worker] completion status unavailable task={task_id}: {exc}",
            flush=True,
        )
        return ""
    line = worker_completion_log_line(payload or {}, task_id)
    if line:
        print(line, flush=True)
    return line


def post_site_terminal_status(
    server_url: str,
    task_id: str,
    result_status: str,
    detail: str,
) -> None:
    blocked = _status_blocks_progress(result_status)
    post_status(
        server_url,
        task_id,
        (
            "desktop_fast_completed_with_errors"
            if blocked
            else "site_run_completed"
        ),
        detail,
    )
    if not blocked:
        print_worker_completion_if_reached(server_url, task_id)


def fetch_recent_tasks(server_url: str, limit: int = 20) -> list[dict[str, object]]:
    url = f"{server_url}/worker/tasks?limit={int(limit)}"
    data = request_json(url)
    tasks = data.get("tasks") if data.get("ok") else []
    return tasks if isinstance(tasks, list) else []


def fetch_case_lookup_request(server_url: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/case-lookup-request"
    data = request_json(url)
    request_payload = data.get("request") if data.get("ok") else None
    return request_payload if isinstance(request_payload, dict) else None


def fetch_credential_sync_request(server_url: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/credential-sync"
    data = request_json(url)
    request_payload = data.get("request") if data.get("ok") else None
    if not isinstance(request_payload, dict):
        return None
    sealed_payload = request_payload.get("sealed_payload")
    if isinstance(sealed_payload, dict):
        payload = open_credential_payload(
            sealed_payload,
            os.getenv("WORKER_TOKEN", "").strip(),
        )
        request_payload = dict(request_payload)
        request_payload.pop("sealed_payload", None)
        request_payload["payload"] = payload
    return request_payload


def current_package_version() -> str:
    path = Path(__file__).with_name("VERSION.txt")
    if not path.exists():
        return "0"
    try:
        return path.read_text(encoding="utf-8-sig").strip() or "0"
    except OSError:
        return "0"


def fetch_remote_update_command(server_url: str, worker_id: str) -> dict[str, object] | None:
    query = urllib.parse.urlencode(
        {
            "worker_id": worker_id,
            "package_version": current_package_version(),
        }
    )
    try:
        data = request_json(f"{server_url}/worker/remote-update?{query}")
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise
    command = data.get("command") if data.get("ok") else None
    return command if isinstance(command, dict) else None


def maybe_run_credential_sync(server_url: str) -> None:
    request_payload = fetch_credential_sync_request(server_url)
    if not request_payload:
        return
    request_id = str(request_payload.get("request_id") or "").strip()
    payload = request_payload.get("payload")
    if not request_id or not isinstance(payload, dict):
        return
    status = "failed"
    detail = "帳密同步資料格式錯誤。"
    try:
        result = save_credential_sync_payload(payload)
        if result is None:
            detail = "帳密同步資料缺少 8 號帳號或密碼。"
        else:
            user_id, _password, path, count = result
            status = "saved"
            detail = f"已同步 {count} 組帳密，目前套用 {user_id}，儲存於 {path}。"
    except Exception as exc:
        detail = f"帳密同步儲存失敗：{exc}"
    finally:
        ack_credential_sync_request(server_url, request_id, status, detail)
    print(f"[worker] credential sync {status}: {detail}", flush=True)


def run_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
    profile_name: str = "duty_work_log_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
    update_overall: bool = True,
    update_context: dict[str, object] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> object:
    update_context = update_context or site_update_context_from_embedded_task(task, "duty_work_log")
    request = AmbulanceReturnRequest.from_dict(task)
    login_audit = login_audit_for_site("duty_work_log", request)
    print(f"[worker] claimed task {request.task_id}", flush=True)
    post_status(server_url, request.task_id, "worker_running", with_login_audit(f"公務電腦 worker 執行中：{worker_id}", login_audit))
    try:
        result = run_local_selenium_task(
            request,
            artifacts_dir,
            profile_name=profile_name,
            debugger_port=debugger_port,
            use_session_lock=use_session_lock,
            tile_name=tile_name,
            force_new_driver=force_new_driver,
            update_context=update_context,
            cancel_check=cancel_check,
        )
    except TaskCancellationError:
        raise
    except Exception as exc:
        result = make_site_result("duty_work_log", "消防勤務工作紀錄", "duty_work_log_failed", f"工作紀錄操作失敗：{exc}", exc)
    result = _result_with_login_audit(result, login_audit)
    post_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
        site_key="duty_work_log",
        site_name="消防勤務工作紀錄",
        **_result_diagnostic_kwargs(result),
    )
    if update_overall:
        post_site_terminal_status(
            server_url,
            request.task_id,
            result.status,
            result.detail,
        )
    print(f"[worker] finished task {request.task_id}: {result.status}", flush=True)
    return result


def run_all_sites_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
) -> object | None:
    task_id = str(task.get("task_id") or "").strip()
    cancellation_event = threading.Event()
    _register_task_cancellation_event(task_id, cancellation_event)
    stop_heartbeat = _start_worker_claim_heartbeat(server_url, task_id, worker_id)
    try:
        return _run_all_sites_task_impl(
            server_url,
            worker_id,
            task,
            artifacts_dir,
            cancellation_event=cancellation_event,
        )
    finally:
        stop_heartbeat()
        _unregister_task_cancellation_event(task_id, cancellation_event)


def _run_all_sites_task_impl(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
    *,
    cancellation_event: threading.Event,
) -> object | None:
    request = AmbulanceReturnRequest.from_dict(task)
    profile_suffix = request.task_id.replace("-", "_")
    site_count_label = task_site_count_label(request)
    post_status(server_url, request.task_id, "desktop_fast_running", f"公務電腦 worker {site_count_label}登打已啟動。")
    try:
        payload = fetch_task_payload(server_url, request.task_id)
    except Exception as exc:
        detail = f"讀取任務狀態失敗，{site_count_label}流程已停止：{exc}"
        result = make_site_result("duty_work_log", SITE_NAMES.get("duty_work_log", "duty_work_log"), "duty_work_log_failed", detail, exc)
        post_status(server_url, request.task_id, "desktop_fast_completed_with_errors", detail)
        return result
    if not isinstance(payload, dict):
        detail = f"讀取任務狀態失敗，NAS 未回傳任務內容，{site_count_label}流程已停止。"
        result = make_site_result("duty_work_log", SITE_NAMES.get("duty_work_log", "duty_work_log"), "duty_work_log_failed", detail)
        post_status(server_url, request.task_id, "desktop_fast_completed_with_errors", detail)
        return result
    _assert_task_payload_claim_current(request.task_id, payload)
    site_groups = [
        [
            (
                "duty_work_log",
                lambda payload: run_task(
                    server_url,
                    worker_id,
                    dict(payload.get("task") or task),
                    artifacts_dir,
                    profile_name=f"duty_work_log_profile_{profile_suffix}",
                    use_session_lock=False,
                    tile_name="duty_work_log",
                    force_new_driver=True,
                    update_overall=False,
                    update_context=site_update_context_from_payload(payload, "duty_work_log"),
                    cancel_check=lambda: _raise_if_task_cancelled(request.task_id, cancellation_event),
                ),
            )
        ],
        [
            (
                "vehicle_mileage",
                lambda payload: run_vehicle_task(
                    server_url,
                    worker_id,
                    dict(payload.get("task") or task),
                    artifacts_dir,
                    profile_name=f"vehicle_mileage_profile_{profile_suffix}",
                    use_session_lock=False,
                    tile_name="vehicle_mileage",
                    force_new_driver=True,
                    update_overall=False,
                    update_context=site_update_context_from_payload(payload, "vehicle_mileage"),
                    vehicle_results=site_vehicle_results_from_payload(payload, "vehicle_mileage"),
                    cancel_check=lambda: _raise_if_task_cancelled(request.task_id, cancellation_event),
                ),
            )
        ],
        [
            (
                "consumables",
                lambda payload: run_consumables_worker_task(
                    server_url,
                    worker_id,
                    dict(payload.get("task") or task),
                    artifacts_dir,
                    profile_name=f"consumables_profile_{profile_suffix}",
                    tile_name="consumables",
                    update_overall=False,
                    update_context=site_update_context_from_payload(payload, "consumables"),
                    vehicle_results=site_vehicle_results_from_payload(payload, "consumables"),
                    cancel_check=lambda: _raise_if_task_cancelled(request.task_id, cancellation_event),
                ),
            )
        ],
        [
            (
                "disinfection",
                lambda payload: run_disinfection_worker_task(
                    server_url,
                    worker_id,
                    dict(payload.get("task") or task),
                    artifacts_dir,
                    profile_name=f"disinfection_profile_{profile_suffix}",
                    use_session_lock=False,
                    tile_name="disinfection",
                    force_new_driver=True,
                    update_overall=False,
                    update_context=site_update_context_from_payload(payload, "disinfection"),
                    vehicle_results=site_vehicle_results_from_payload(payload, "disinfection"),
                    cancel_check=lambda: _raise_if_task_cancelled(request.task_id, cancellation_event),
                ),
            )
        ],
    ]
    if request.has_fuel_record():
        site_groups[1].append(
            (
                "fuel_record",
                lambda payload: run_fuel_worker_task(
                    server_url,
                    worker_id,
                    dict(payload.get("task") or task),
                    artifacts_dir,
                    profile_name=f"fuel_record_profile_{profile_suffix}",
                    use_session_lock=False,
                    tile_name="fuel_record",
                    force_new_driver=True,
                    update_overall=False,
                    update_context=site_update_context_from_payload(payload, "fuel_record"),
                    vehicle_results=site_vehicle_results_from_payload(payload, "fuel_record"),
                    cancel_check=lambda: _raise_if_task_cancelled(request.task_id, cancellation_event),
                ),
            ),
        )
    last_result = None
    failed_results = []
    group_results: list[tuple[object | None, list[object]] | None] = [None] * len(site_groups)
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SITE_GROUPS) as executor:
        futures = {
            executor.submit(
                _run_worker_site_group,
                server_url,
                request.task_id,
                site_group,
                cancellation_event=cancellation_event,
            ): index
            for index, site_group in enumerate(site_groups)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                group_results[index] = future.result()
            except StaleWorkerClaimError:
                cancellation_event.set()
                for pending_future in futures:
                    pending_future.cancel()
                raise
            except Exception as exc:
                site_key = site_groups[index][0][0]
                detail = f"公務電腦 worker 平行流程例外：{exc}"
                group_results[index] = (
                    None,
                    [make_site_result(site_key, SITE_NAMES.get(site_key, site_key), f"{site_key}_failed", detail, exc)],
                )
    for group_result in group_results:
        if group_result is None:
            continue
        group_last_result, group_failed_results = group_result
        if group_last_result is not None:
            last_result = group_last_result
        failed_results.extend(group_failed_results)
    if failed_results:
        post_status(
            server_url,
            request.task_id,
            "desktop_fast_completed_with_errors",
            f"公務電腦 worker {site_count_label}登打完成，{len(failed_results)} 站失敗；已略過失敗站並接續後續站別。",
        )
        maximize_worker_site_windows()
        return failed_results[-1]
    post_status(
        server_url,
        request.task_id,
        "desktop_fast_completed",
        f"公務電腦 worker {site_count_label}登打完成。",
    )
    print_worker_completion_if_reached(server_url, request.task_id)
    maximize_worker_site_windows()
    return last_result


def _run_worker_site_group(
    server_url: str,
    task_id: str,
    site_group: list[tuple[str, Callable[[dict[str, object]], object]]],
    *,
    cancellation_event: threading.Event | None = None,
) -> tuple[object | None, list[object]]:
    effective_cancellation_event = cancellation_event or threading.Event()
    last_result = None
    failed_results = []
    for site_key, runner in site_group:
        try:
            _raise_if_task_cancelled(task_id, effective_cancellation_event)
            try:
                payload = fetch_task_payload(server_url, task_id)
            except Exception as exc:
                detail = f"讀取任務狀態失敗，登打流程已停止：{exc}"
                result = make_site_result(site_key, SITE_NAMES.get(site_key, site_key), f"{site_key}_failed", detail, exc)
                failed_results.append(result)
                return last_result, failed_results
            if not isinstance(payload, dict):
                detail = "讀取任務狀態失敗，NAS 未回傳任務內容，登打流程已停止。"
                result = make_site_result(site_key, SITE_NAMES.get(site_key, site_key), f"{site_key}_failed", detail)
                failed_results.append(result)
                return last_result, failed_results
            _assert_task_payload_claim_current(task_id, payload)
            _raise_if_task_cancelled(task_id, effective_cancellation_event)
            site_statuses = payload.get("site_statuses") if isinstance(payload, dict) and isinstance(payload.get("site_statuses"), dict) else {}
            current_status = str((site_statuses.get(site_key) or {}).get("status") or "")
            if _site_is_complete(current_status):
                print(f"[worker] skip completed site task={task_id} site={site_key}", flush=True)
                continue
            last_result = runner(payload)
            if _result_blocks_progress(last_result):
                failed_results.append(last_result)
        finally:
            maximize_worker_site_windows()
    return last_result, failed_results


def worker_claim_heartbeat_seconds() -> float:
    try:
        return max(0.01, float(os.getenv("WORKER_CLAIM_HEARTBEAT_SECONDS", "240")))
    except ValueError:
        return 240.0


def worker_claim_poll_seconds() -> float:
    try:
        return max(0.01, min(5.0, float(os.getenv("WORKER_CLAIM_POLL_SECONDS", "2"))))
    except ValueError:
        return 2.0


def _start_worker_claim_heartbeat(server_url: str, task_id: str, worker_id: str) -> Callable[[], None]:
    stop = threading.Event()

    def heartbeat() -> None:
        claim_context = _task_claim_context(task_id)
        monitor_claim = bool(str(claim_context.get("claim_id") or "").strip())
        poll_seconds = worker_claim_poll_seconds()
        heartbeat_seconds = worker_claim_heartbeat_seconds()
        now = time.monotonic()
        next_poll = now + poll_seconds if monitor_claim else float("inf")
        next_heartbeat = now + heartbeat_seconds
        while True:
            delay = max(0.01, min(next_poll, next_heartbeat) - time.monotonic())
            if stop.wait(delay):
                return
            now = time.monotonic()
            if monitor_claim and now >= next_poll:
                claim_id = str(_task_claim_context(task_id).get("claim_id") or "").strip()
                try:
                    payload = fetch_task_payload(server_url, task_id)
                    if not isinstance(payload, dict):
                        raise RuntimeError("NAS did not return the claimed task payload")
                    _assert_task_payload_claim_current(task_id, payload)
                except StaleWorkerClaimError:
                    return
                except Exception as exc:
                    _mark_task_claim_stale(task_id, claim_id)
                    print(f"[worker] claim monitor stopped task={task_id}: {exc}", flush=True)
                    return
                next_poll = now + poll_seconds
            if now >= next_heartbeat:
                try:
                    post_status(
                        server_url,
                        task_id,
                        "worker_running",
                        f"公務電腦 worker 持續執行中：{worker_id}",
                    )
                except StaleWorkerClaimError:
                    return
                except Exception as exc:
                    print(f"[worker] claim heartbeat deferred task={task_id}: {exc}", flush=True)
                next_heartbeat = now + heartbeat_seconds

    thread = threading.Thread(target=heartbeat, name=f"worker-claim-heartbeat:{task_id}", daemon=True)
    thread.start()

    def stop_heartbeat() -> None:
        stop.set()
        if threading.current_thread() is not thread:
            thread.join(timeout=2.0)

    return stop_heartbeat


def site_update_context_from_payload(payload: dict[str, object], site_key: str) -> dict[str, object] | None:
    site_statuses = payload.get("site_statuses")
    if not isinstance(site_statuses, dict):
        return None
    site = site_statuses.get(site_key)
    if not isinstance(site, dict):
        return None
    context = site.get("update_context")
    return context if isinstance(context, dict) else None


def site_update_context_from_embedded_task(
    task: dict[str, object],
    site_key: str,
) -> dict[str, object] | None:
    payload = task.get("_worker_payload")
    return site_update_context_from_payload(payload, site_key) if isinstance(payload, dict) else None


def site_vehicle_results_from_payload(payload: dict[str, object], site_key: str) -> dict[str, dict[str, str]]:
    site_statuses = payload.get("site_statuses")
    if not isinstance(site_statuses, dict):
        return {}
    site = site_statuses.get(site_key)
    if not isinstance(site, dict) or not isinstance(site.get("vehicle_results"), dict):
        return {}
    return {
        str(key): {str(field): str(value or "") for field, value in dict(result).items()}
        for key, result in site["vehicle_results"].items()
        if isinstance(result, dict)
    }


def site_vehicle_results_from_embedded_task(
    task: dict[str, object],
    site_key: str,
) -> dict[str, dict[str, str]]:
    payload = task.get("_worker_payload")
    return site_vehicle_results_from_payload(payload, site_key) if isinstance(payload, dict) else {}


def _vehicle_specific_update_context(
    update_context: dict[str, object] | None,
    vehicle_request: AmbulanceReturnRequest,
    vehicle_index: int,
) -> dict[str, object] | None:
    if not isinstance(update_context, dict):
        return None
    context = dict(update_context)
    context["vehicle_index"] = int(vehicle_index)
    context["vehicle_key"] = str(vehicle_request.vehicle or "").strip() or f"{vehicle_index}車"
    return context


def _vehicle_result_key(request: AmbulanceReturnRequest, index: int) -> str:
    vehicle = str(request.vehicle or "").strip()
    return vehicle or f"{index}車"


def _vehicle_result_is_complete(result: object) -> bool:
    return isinstance(result, dict) and _site_is_complete(str(result.get("status") or ""))


def _aggregate_worker_vehicle_status(
    site_key: str,
    vehicle_requests: list[AmbulanceReturnRequest],
    results: dict[str, dict[str, str]],
) -> str:
    statuses = [
        str(dict(results.get(_vehicle_result_key(vehicle_request, index)) or {}).get("status") or "")
        for index, vehicle_request in enumerate(vehicle_requests, start=1)
    ]
    if not statuses or any(not status for status in statuses):
        return f"{site_key}_failed"
    if any("waiting_confirmation" in status for status in statuses):
        return f"{site_key}_waiting_confirmation"
    if any("failed" in status or "error" in status for status in statuses):
        return f"{site_key}_failed"
    if all(_site_is_complete(status) for status in statuses):
        return f"{site_key}_saved"
    return statuses[-1]


def _run_worker_per_vehicle_site(
    server_url: str,
    request: AmbulanceReturnRequest,
    site_key: str,
    site_name: str,
    action: Callable[[AmbulanceReturnRequest, int], object],
    *,
    vehicle_results: dict[str, dict[str, str]] | None = None,
    login_audit: str = "",
    vehicle_requests: list[AmbulanceReturnRequest] | None = None,
) -> SiteAutomationResult:
    stored = {str(key): dict(value) for key, value in dict(vehicle_results or {}).items() if isinstance(value, dict)}
    details: list[str] = []
    vehicle_requests = list(vehicle_requests or request.vehicle_requests())
    for index, vehicle_request in enumerate(vehicle_requests, start=1):
        vehicle_key = _vehicle_result_key(vehicle_request, index)
        if _vehicle_result_is_complete(stored.get(vehicle_key)):
            details.append(f"{vehicle_key}: 已完成，略過")
            continue
        try:
            vehicle_result = action(vehicle_request, index)
        except TaskCancellationError:
            raise
        except ManualUpdateRequiredError as exc:
            vehicle_result = make_site_result(
                site_key,
                site_name,
                f"{site_key}_waiting_confirmation",
                f"需人工更新：{exc}",
                exc,
            )
        except Exception as exc:
            vehicle_result = make_site_result(site_key, site_name, f"{site_key}_failed", str(exc), exc)
        vehicle_result = _result_with_login_audit(vehicle_result, login_audit)
        vehicle_status = str(getattr(vehicle_result, "status", "") or f"{site_key}_failed")
        vehicle_detail = str(getattr(vehicle_result, "detail", "") or "")
        stored[vehicle_key] = {"status": vehicle_status, "detail": vehicle_detail}
        post_status(
            server_url,
            request.task_id,
            vehicle_status,
            vehicle_detail,
            site_key=site_key,
            site_name=site_name,
            vehicle_key=vehicle_key,
            vehicle_label=str(vehicle_request.vehicle or "").strip(),
            **_result_diagnostic_kwargs(vehicle_result),
        )
        details.append(f"{vehicle_key}: {vehicle_detail}")
    status = _aggregate_worker_vehicle_status(site_key, vehicle_requests, stored)
    return make_site_result(site_key, site_name, status, " | ".join(details))


def run_vehicle_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
    profile_name: str = "vehicle_mileage_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
    update_overall: bool = True,
    update_context: dict[str, object] | None = None,
    vehicle_results: dict[str, dict[str, str]] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> object:
    if update_context is None:
        update_context = site_update_context_from_embedded_task(task, "vehicle_mileage")
    if vehicle_results is None:
        vehicle_results = site_vehicle_results_from_embedded_task(task, "vehicle_mileage")
    request = AmbulanceReturnRequest.from_dict(task)
    login_audit = login_audit_for_site("vehicle_mileage", request)
    print(f"[worker] vehicle mileage task {request.task_id}", flush=True)
    post_status(
        server_url,
        request.task_id,
        "vehicle_mileage_running",
        with_login_audit(f"公務電腦 worker 執行車輛里程：{worker_id}", login_audit),
    )
    if len(request.vehicle_requests()) > 1:
        shared_port = debugger_port or int(os.getenv("VEHICLE_MILEAGE_DEBUGGER_PORT", "9234"))
        result = _run_worker_per_vehicle_site(
            server_url,
            request,
            "vehicle_mileage",
            "車輛里程",
            lambda vehicle_request, index: run_vehicle_mileage_task(
                vehicle_request,
                artifacts_dir,
                profile_name=profile_name,
                debugger_port=shared_port,
                use_session_lock=use_session_lock,
                tile_name=tile_name,
                force_new_driver=index == 1,
                update_context=_vehicle_specific_update_context(update_context, vehicle_request, index),
                cancel_check=cancel_check,
            ),
            vehicle_results=vehicle_results,
            login_audit=login_audit,
        )
    else:
        try:
            result = run_vehicle_mileage_task(
                request,
                artifacts_dir,
                profile_name=profile_name,
                debugger_port=debugger_port,
                use_session_lock=use_session_lock,
                tile_name=tile_name,
                force_new_driver=force_new_driver,
                update_context=update_context,
                cancel_check=cancel_check,
            )
        except TaskCancellationError:
            raise
        except Exception as exc:
            result = make_site_result("vehicle_mileage", "車輛里程", "vehicle_mileage_failed", f"車輛里程操作失敗：{exc}", exc)
        result = _result_with_login_audit(result, login_audit)
    post_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
        site_key="vehicle_mileage",
        site_name="車輛里程",
        **_result_diagnostic_kwargs(result),
    )
    if update_overall:
        post_site_terminal_status(
            server_url,
            request.task_id,
            result.status,
            result.detail,
        )
    print(f"[worker] finished vehicle mileage {request.task_id}: {result.status}", flush=True)
    return result


def run_fuel_worker_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
    profile_name: str = "fuel_record_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
    update_overall: bool = True,
    update_context: dict[str, object] | None = None,
    vehicle_results: dict[str, dict[str, str]] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> object:
    if update_context is None:
        update_context = site_update_context_from_embedded_task(task, "fuel_record")
    if vehicle_results is None:
        vehicle_results = site_vehicle_results_from_embedded_task(task, "fuel_record")
    request = AmbulanceReturnRequest.from_dict(task)
    all_vehicle_requests = request.vehicle_requests()
    fuel_requests = [item for item in all_vehicle_requests if item.fuel_record.enabled]
    if not fuel_requests:
        return make_site_result("fuel_record", "登打加油紀錄", "fuel_record_skipped", "未勾選加油紀錄，已略過。")
    login_audit = login_audit_for_site("fuel_record", request)
    print(f"[worker] fuel record task {request.task_id}", flush=True)
    post_status(
        server_url,
        request.task_id,
        "fuel_record_running",
        with_login_audit(f"公務電腦 worker 登打加油紀錄：{worker_id}", login_audit),
    )
    try:
        if len(all_vehicle_requests) > 1:
            original_indexes = {id(item): index for index, item in enumerate(all_vehicle_requests, start=1)}
            shared_port = debugger_port or int(os.getenv("FUEL_RECORD_DEBUGGER_PORT", "9235"))
            result = _run_worker_per_vehicle_site(
                server_url,
                request,
                "fuel_record",
                "登打加油紀錄",
                lambda vehicle_request, index: run_fuel_record_task(
                    vehicle_request,
                    artifacts_dir,
                    profile_name=profile_name,
                    debugger_port=shared_port,
                    use_session_lock=use_session_lock,
                    tile_name=tile_name,
                    force_new_driver=index == 1,
                    update_context=_vehicle_specific_update_context(
                        update_context,
                        vehicle_request,
                        original_indexes[id(vehicle_request)],
                    ),
                    cancel_check=cancel_check,
                ),
                vehicle_results=vehicle_results,
                login_audit=login_audit,
                vehicle_requests=fuel_requests,
            )
        else:
            result = run_fuel_record_task(
                request,
                artifacts_dir,
                profile_name=profile_name,
                debugger_port=debugger_port,
                use_session_lock=use_session_lock,
                tile_name=tile_name,
                force_new_driver=force_new_driver,
                update_context=update_context,
                cancel_check=cancel_check,
            )
    except TaskCancellationError:
        raise
    except Exception as exc:
        result = make_site_result("fuel_record", "登打加油紀錄", "fuel_record_failed", f"加油紀錄操作失敗：{exc}", exc)
    if len(all_vehicle_requests) <= 1:
        result = _result_with_login_audit(result, login_audit)
    post_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
        site_key="fuel_record",
        site_name="登打加油紀錄",
        **_result_diagnostic_kwargs(result),
    )
    if update_overall:
        post_site_terminal_status(
            server_url,
            request.task_id,
            result.status,
            result.detail,
        )
    print(f"[worker] finished fuel record {request.task_id}: {result.status}", flush=True)
    return result


def run_disinfection_worker_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
    driver=None,
    profile_name: str = "disinfection_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
    update_overall: bool = True,
    update_context: dict[str, object] | None = None,
    vehicle_results: dict[str, dict[str, str]] | None = None,
    cancel_check: Callable[[], None] | None = None,
):
    if update_context is None:
        update_context = site_update_context_from_embedded_task(task, "disinfection")
    if vehicle_results is None:
        vehicle_results = site_vehicle_results_from_embedded_task(task, "disinfection")
    request = AmbulanceReturnRequest.from_dict(task)
    login_audit = login_audit_for_site("disinfection", request)
    print(f"[worker] disinfection task {request.task_id}", flush=True)
    post_status(
        server_url,
        request.task_id,
        "disinfection_running",
        with_login_audit(f"公務電腦 worker 執行消毒紀錄：{worker_id}", login_audit),
    )
    try:
        require_safe_automated_update("disinfection", request, update_context)
        if len(request.vehicle_requests()) > 1:
            shared_driver = driver or login_disinfection_and_get_driver(
                profile_name=profile_name,
                debugger_port=debugger_port,
                tile_name=tile_name,
            )
            result = _run_worker_per_vehicle_site(
                server_url,
                request,
                "disinfection",
                "緊急救護消毒",
                lambda vehicle_request, _index: run_disinfection_task(
                    vehicle_request,
                    artifacts_dir,
                    existing_driver=shared_driver,
                    profile_name=profile_name,
                    debugger_port=debugger_port,
                    use_session_lock=use_session_lock,
                    tile_name=tile_name,
                    force_new_driver=False,
                    update_context=_vehicle_specific_update_context(update_context, vehicle_request, _index),
                    cancel_check=cancel_check,
                ),
                vehicle_results=vehicle_results,
                login_audit=login_audit,
            )
        else:
            shared_driver = driver or login_disinfection_and_get_driver(
                profile_name=profile_name,
                debugger_port=debugger_port,
                tile_name=tile_name,
            )
            result = run_disinfection_task(
                request,
                artifacts_dir,
                existing_driver=shared_driver,
                profile_name=profile_name,
                debugger_port=debugger_port,
                use_session_lock=use_session_lock,
                tile_name=tile_name,
                force_new_driver=False,
                update_context=update_context,
                cancel_check=cancel_check,
            )
            result = _result_with_login_audit(result, login_audit)
    except TaskCancellationError:
        raise
    except ManualUpdateRequiredError as exc:
        result = make_site_result(
            "disinfection",
            "緊急救護消毒",
            "disinfection_waiting_confirmation",
            f"需人工更新：{exc}",
            exc,
        )
        result = _result_with_login_audit(result, login_audit)
    except Exception as exc:
        result = make_site_result("disinfection", "緊急救護消毒", "disinfection_failed", f"消毒紀錄操作失敗：{exc}", exc)
        result = _result_with_login_audit(result, login_audit)
    post_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
        site_key="disinfection",
        site_name="緊急救護消毒",
        **_result_diagnostic_kwargs(result),
    )
    if update_overall:
        post_site_terminal_status(
            server_url,
            request.task_id,
            result.status,
            result.detail,
        )
    print(f"[worker] finished disinfection {request.task_id}: {result.status}", flush=True)
    return result


def run_consumables_worker_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
    profile_name: str = "consumables_profile",
    debugger_port: int | None = None,
    tile_name: str = "",
    update_overall: bool = True,
    update_context: dict[str, object] | None = None,
    vehicle_results: dict[str, dict[str, str]] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> SiteAutomationResult:
    if update_context is None:
        update_context = site_update_context_from_embedded_task(task, "consumables")
    if vehicle_results is None:
        vehicle_results = site_vehicle_results_from_embedded_task(task, "consumables")
    request = AmbulanceReturnRequest.from_dict(task)
    login_audit = login_audit_for_site("consumables", request)
    print(f"[worker] consumables task {request.task_id}", flush=True)
    post_status(
        server_url,
        request.task_id,
        "consumables_running",
        with_login_audit(f"公務電腦 worker 執行耗材：{worker_id}", login_audit),
        site_key="consumables",
        site_name="一站通耗材",
    )
    try:
        require_safe_automated_update("consumables", request, update_context)
        driver = login_acs_and_get_driver(
            profile_name=profile_name,
            debugger_port=debugger_port,
            tile_name=tile_name,
            task=request,
        )
        if len(request.vehicle_requests()) > 1:
            result = _run_worker_per_vehicle_site(
                server_url,
                request,
                "consumables",
                "一站通耗材",
                lambda vehicle_request, _index: SiteAutomationResult(
                    "consumables",
                    "一站通耗材",
                    "consumables_saved" if save_consumables_record_enabled() else "consumables_prefilled",
                    open_consumable_record_for_task(
                        driver,
                        vehicle_request,
                        **(
                            {
                                "update_context": _vehicle_specific_update_context(
                                    update_context,
                                    vehicle_request,
                                    _index,
                                )
                            }
                            if update_context is not None
                            else {}
                        ),
                        **({"cancel_check": cancel_check} if cancel_check is not None else {}),
                    ),
                ),
                vehicle_results=vehicle_results,
                login_audit=login_audit,
            )
        else:
            detail = open_consumable_record_for_task(
                driver,
                request,
                **({"update_context": update_context} if update_context is not None else {}),
                **({"cancel_check": cancel_check} if cancel_check is not None else {}),
            )
            status = "consumables_saved" if save_consumables_record_enabled() else "consumables_prefilled"
            result = SiteAutomationResult("consumables", "一站通耗材", status, detail)
    except TaskCancellationError:
        raise
    except ManualUpdateRequiredError as exc:
        result = make_site_result(
            "consumables",
            "一站通耗材",
            "consumables_waiting_confirmation",
            f"需人工更新：{exc}",
            exc,
        )
        result = _result_with_login_audit(result, login_audit)
    except Exception as exc:
        result = make_site_result("consumables", "一站通耗材", "consumables_failed", f"耗材登打失敗：{exc}", exc)
        result = _result_with_login_audit(result, login_audit)
    if len(request.vehicle_requests()) <= 1:
        result = _result_with_login_audit(result, login_audit)
    post_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
        site_key="consumables",
        site_name="一站通耗材",
        **_result_diagnostic_kwargs(result),
    )
    if update_overall:
        post_site_terminal_status(
            server_url,
            request.task_id,
            result.status,
            result.detail,
        )
    print(f"[worker] finished consumables {request.task_id}: {result.status}", flush=True)
    return result


def request_json(url: str) -> dict[str, object]:
    req = urllib.request.Request(url, headers=worker_headers())
    try:
        with urllib.request.urlopen(req, timeout=worker_api_timeout()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = worker_api_error_message(exc)
        exc.close()
        raise RuntimeError(message) from exc


def post_json(url: str, payload: Mapping[str, object]) -> dict[str, object]:
    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        str(url),
        data=body,
        headers={**worker_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=worker_api_timeout()) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = worker_api_error_message(exc)
        exc.close()
        raise RuntimeError(message) from exc
    if not isinstance(data, dict):
        raise RuntimeError("NAS worker API JSON response must be an object")
    return data


def post_status(
    server_url: str,
    task_id: str,
    status: str,
    detail: str,
    site_key: str = "",
    site_name: str = "",
    overall_status: str = "",
    overall_detail: str = "",
    failure_stage: str = "",
    failure_reason: str = "",
    next_action: str = "",
    exception_type: str = "",
    vehicle_key: str = "",
    vehicle_label: str = "",
    claim_id: str = "",
    worker_id: str = "",
) -> None:
    task_id = str(task_id or "").strip()
    _raise_if_task_claim_stale(task_id, claim_id)
    payload = {
        "status": status,
        "detail": detail,
        "site_key": site_key,
        "site_name": site_name,
    }
    if vehicle_key:
        payload["vehicle_key"] = vehicle_key
        payload["vehicle_label"] = vehicle_label or vehicle_key
    claim_context = _task_claim_context(task_id)
    effective_claim_id = str(claim_id or claim_context.get("claim_id") or "").strip()
    effective_worker_id = str(worker_id or claim_context.get("worker_id") or "").strip()
    if effective_claim_id:
        payload["claim_id"] = effective_claim_id
        if effective_worker_id:
            payload["worker_id"] = effective_worker_id
    if site_key:
        computed = diagnostic_payload(site_key, status, detail)
        explicit = {
            "failure_stage": failure_stage,
            "failure_reason": failure_reason,
            "next_action": next_action,
            "exception_type": exception_type,
        }
        for field in DIAGNOSTIC_FIELDS:
            value = str(explicit.get(field) or computed.get(field) or "").strip()
            if value:
                payload[field] = value
    if overall_status:
        payload["overall_status"] = overall_status
        payload["overall_detail"] = overall_detail or detail
    payload["status_event_id"] = uuid4().hex
    outbox = worker_status_outbox()
    with _STATUS_DELIVERY_LOCK:
        try:
            _enqueue_worker_status_with_retry(outbox, {"task_id": task_id, "body": payload})
        except OSError as exc:
            # Never bypass an older durable event. A direct fallback could send
            # a terminal state ahead of the FIFO and invalidate every older one.
            print(f"[worker] status outbox unavailable task={task_id}: {exc}", flush=True)
            raise
        try:
            _flush_status_outbox_locked(server_url, outbox)
        except OSError as exc:
            # The event is already durable. Windows antivirus/indexer locks must
            # not unwind the Selenium workflow.
            print(f"[worker] status outbox flush deferred task={task_id}: {exc}", flush=True)
    _raise_if_task_claim_stale(task_id, effective_claim_id)


def worker_status_outbox() -> WorkerStatusOutbox:
    artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
    return WorkerStatusOutbox(
        artifacts_dir / "worker_status_outbox",
        claim_lease_seconds=worker_status_claim_lease_seconds(),
    )


def worker_status_claim_lease_seconds() -> float:
    try:
        return max(15.0, float(os.getenv("WORKER_STATUS_CLAIM_LEASE_SECONDS", "30")))
    except ValueError:
        return 30.0


def worker_status_enqueue_retries() -> int:
    try:
        return max(1, min(int(os.getenv("WORKER_STATUS_ENQUEUE_RETRIES", "3")), 5))
    except ValueError:
        return 3


def worker_status_enqueue_retry_delay_seconds() -> float:
    try:
        return max(0.0, min(float(os.getenv("WORKER_STATUS_ENQUEUE_RETRY_DELAY_SECONDS", "0.1")), 1.0))
    except ValueError:
        return 0.1


def _enqueue_worker_status_with_retry(outbox: WorkerStatusOutbox, record: dict[str, object]) -> str:
    """Persist before delivery, with a bounded retry for transient Windows locks."""

    attempts = worker_status_enqueue_retries()
    delay = worker_status_enqueue_retry_delay_seconds()
    last_error: OSError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return outbox.enqueue(record)
        except OSError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            print(
                f"[worker] status outbox enqueue retry {attempt}/{attempts - 1}: {exc}",
                flush=True,
            )
            if delay:
                time.sleep(delay)
    assert last_error is not None
    raise last_error


def worker_status_post_retries() -> int:
    try:
        return max(1, min(int(os.getenv("WORKER_STATUS_POST_RETRIES", "1")), 5))
    except ValueError:
        return 1


def worker_status_retry_backoff_seconds() -> float:
    try:
        return max(0.0, min(float(os.getenv("WORKER_STATUS_RETRY_BACKOFF_SECONDS", "30")), 300.0))
    except ValueError:
        return 30.0


def worker_status_flush_max_events() -> int:
    try:
        return max(1, min(int(os.getenv("WORKER_STATUS_FLUSH_MAX_EVENTS", "20")), 200))
    except ValueError:
        return 20


def _status_delivery_key(server_url: str, outbox: WorkerStatusOutbox) -> str:
    root = os.path.normcase(str(outbox.root_dir.resolve()))
    return f"{root}|{str(server_url).rstrip('/').lower()}"


def _arm_status_delivery_backoff(key: str) -> None:
    _STATUS_DELIVERY_RETRY_AFTER[key] = time.monotonic() + worker_status_retry_backoff_seconds()


def _status_http_error_is_permanent(exc: urllib.error.HTTPError) -> bool:
    if exc.code == 409:
        return True
    retryable_client_codes = {401, 403, 408, 423, 425, 429}
    return 400 <= exc.code < 500 and exc.code not in retryable_client_codes


def flush_status_outbox(server_url: str = "") -> int:
    """Replay durable statuses in FIFO order; never let an outage stop work."""

    effective_server_url = str(
        server_url or os.getenv("WORKER_SERVER_URL", "http://127.0.0.1:8080")
    ).rstrip("/")
    with _STATUS_DELIVERY_LOCK:
        try:
            return _flush_status_outbox_locked(effective_server_url, worker_status_outbox())
        except OSError as exc:
            print(f"[worker] status outbox replay unavailable: {exc}", flush=True)
            return 0


def _flush_status_outbox_locked(server_url: str, outbox: WorkerStatusOutbox) -> int:
    delivery_key = _status_delivery_key(server_url, outbox)
    if time.monotonic() < _STATUS_DELIVERY_RETRY_AFTER.get(delivery_key, 0.0):
        return 0
    delivered = 0
    processed = 0
    max_events = worker_status_flush_max_events()
    while processed < max_events:
        record = outbox.claim_next()
        if record is None:
            return delivered
        processed += 1
        event_id = str(record.get("event_id") or "")
        stored = record.get("payload")
        task_id = str(stored.get("task_id") or "").strip() if isinstance(stored, dict) else ""
        body = stored.get("body") if isinstance(stored, dict) else None
        if (
            not task_id
            or not isinstance(body, dict)
            or not str(body.get("status_event_id") or "").strip()
        ):
            if not outbox.reject(event_id, "invalid worker status spool payload"):
                print(f"[worker] invalid status event could not be isolated event={event_id}", flush=True)
                _arm_status_delivery_backoff(delivery_key)
                return delivered
            print(f"[worker] dead-lettered invalid status outbox event={event_id}", flush=True)
            continue

        last_error: BaseException | None = None
        permanent_error: urllib.error.HTTPError | None = None
        sent = False
        retries = worker_status_post_retries()
        for attempt in range(retries):
            try:
                _send_worker_status(server_url, task_id, body)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if _status_http_error_is_permanent(exc):
                    permanent_error = exc
                    break
                exc.close()
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
            else:
                sent = True
                break
            if attempt + 1 < retries:
                time.sleep(0.25 * (attempt + 1))

        if permanent_error is not None:
            claim_id = str(body.get("claim_id") or "").strip()
            error_code = permanent_error.code
            reason = worker_api_error_message(permanent_error)
            permanent_error.close()
            if error_code == 409:
                _mark_task_claim_stale(task_id, claim_id)
            if not outbox.reject(event_id, reason):
                outbox.release(event_id)
                _arm_status_delivery_backoff(delivery_key)
                print(f"[worker] rejected status could not be isolated task={task_id}: {reason}", flush=True)
                return delivered
            print(
                f"[worker] status dead-lettered task={task_id} event={event_id}: {reason}",
                flush=True,
            )
            continue

        if sent:
            delivered += 1
            _STATUS_DELIVERY_RETRY_AFTER.pop(delivery_key, None)
            if not outbox.ack(event_id):
                _arm_status_delivery_backoff(delivery_key)
                print(
                    f"[worker] status accepted but local ack deferred task={task_id} event={event_id}",
                    flush=True,
                )
                return delivered
            continue

        released = outbox.release(event_id)
        _arm_status_delivery_backoff(delivery_key)
        if not released:
            print(
                f"[worker] status release deferred task={task_id} event={event_id}; "
                "claim lease recovery will retry it",
                flush=True,
            )
        print(
            f"[worker] status delivery deferred task={task_id} event={event_id}: {last_error}",
            flush=True,
        )
        return delivered
    return delivered


def _send_worker_status(server_url: str, task_id: str, payload: dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{str(server_url).rstrip('/')}/worker/tasks/{urllib.parse.quote(task_id, safe='')}/status",
        data=body,
        headers={**worker_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=worker_api_timeout()) as response:
        response.read()


def post_cases(
    server_url: str,
    status: str,
    detail: str,
    lookup_range: str,
    cases: list[dict[str, object]],
    case_hash: str,
    request_id: str = "",
) -> None:
    payload = {
        "status": status,
        "detail": detail,
        "lookup_range": lookup_range,
        "case_hash": case_hash,
        "source": "public_duty_pc_worker",
        "cases": cases,
    }
    if request_id:
        payload["request_id"] = request_id
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/worker/cases",
        data=body,
        headers={**worker_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=worker_api_timeout()) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        message = worker_api_error_message(exc)
        exc.close()
        raise RuntimeError(message) from exc


def ack_credential_sync_request(server_url: str, request_id: str, status: str, detail: str) -> None:
    payload = {
        "status": status,
        "detail": detail,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/worker/credential-sync/{urllib.parse.quote(request_id)}/ack",
        data=body,
        headers={**worker_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=worker_api_timeout()) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        message = worker_api_error_message(exc)
        exc.close()
        raise RuntimeError(message) from exc


def post_remote_update_status(
    server_url: str,
    request_id: str,
    status: str,
    detail: str,
    *,
    worker_id: str = "",
    before_version: str = "",
    installed_version: str = "",
    exit_code: int | str | None = None,
) -> None:
    payload: dict[str, object] = {
        "status": status,
        "detail": detail,
    }
    for key, value in {
        "worker_id": worker_id,
        "before_version": before_version,
        "installed_version": installed_version,
    }.items():
        if value:
            payload[key] = value
    if exit_code is not None:
        payload["exit_code"] = exit_code
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/worker/remote-update/{urllib.parse.quote(request_id)}/status",
        data=body,
        headers={**worker_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=worker_api_timeout()) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        message = worker_api_error_message(exc)
        exc.close()
        raise RuntimeError(message) from exc


def worker_api_timeout() -> int:
    return int(os.getenv("WORKER_API_TIMEOUT_SECONDS", "8"))


def worker_api_error_message(exc: urllib.error.HTTPError) -> str:
    if exc.code == 409:
        return "NAS worker claim 已過期或已由其他 worker 接手（HTTP 409），不再回報舊任務狀態。"
    if exc.code == 403:
        return "NAS worker API 拒絕連線（HTTP 403）：WORKER_TOKEN 未設定或與 NAS 不一致，請同步 NAS 與公務電腦 .env 後重啟 worker。"
    return f"NAS worker API 回應 HTTP {exc.code}：{exc.reason}"


def hash_cases(cases: list[dict[str, object]]) -> str:
    normalized = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _status_blocks_progress(status: str) -> bool:
    text = str(status or "")
    if "failed" in text or "error" in text:
        return True
    if text.startswith("needs_") or "login" in text or "waiting_confirmation" in text:
        return True
    return False


def _result_with_login_audit(result, audit: str):
    detail = with_login_audit(str(getattr(result, "detail", "") or ""), audit)
    try:
        return replace(result, detail=detail)
    except (TypeError, ValueError):
        return result


def _result_diagnostic_kwargs(result: object) -> dict[str, str]:
    return {
        field: str(getattr(result, field, "") or "")
        for field in DIAGNOSTIC_FIELDS
    }


def _result_blocks_progress(result: object) -> bool:
    if getattr(result, "ok", True) is False:
        return True
    return _status_blocks_progress(str(getattr(result, "status", "") or ""))


def _site_is_complete(status: str) -> bool:
    value = str(status or "")
    return value == "completed_by_user" or value.endswith("_saved")


def load_last_case_hash(artifacts_dir: Path) -> str:
    path = artifacts_dir / "cases" / "latest.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("case_hash") or hash_cases(payload.get("cases") or []))


def last_case_lookup_waiting_for_login(artifacts_dir: Path) -> bool:
    path = artifacts_dir / "cases" / "latest.json"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return str(payload.get("status") or "") in {"needs_duty_login", "duty_login_failed"}


def worker_headers() -> dict[str, str]:
    token = os.getenv("WORKER_TOKEN", "").strip()
    return {"X-Worker-Token": token} if token else {}


if __name__ == "__main__":
    main()
