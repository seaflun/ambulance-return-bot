"""Independent Worker control heartbeat and remote-update mailbox."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ambulance_bot import worker_health

if TYPE_CHECKING:
    from ambulance_bot.worker_routes import WorkerControlClient


MAILBOX_COMMAND_FIELDS = frozenset(
    {
        "request_id",
        "status",
        "requested_at",
        "updated_at",
        "worker_id",
        "before_version",
        "installed_version",
        "detail",
        "exit_code",
    }
)
REMOTE_UPDATE_STATUSES = frozenset(
    {
        "pending",
        "waiting_busy",
        "waiting_idle",
        "updating",
        "completed",
        "up_to_date",
        "failed",
        "timed_out",
    }
)
TERMINAL_REMOTE_UPDATE_STATUSES = frozenset({"completed", "up_to_date", "failed", "timed_out"})
WAITING_REMOTE_UPDATE_STATUSES = frozenset({"waiting_busy", "waiting_idle"})


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: str
    activity: str
    busy_reason: str
    request_id: str


class WorkerRuntimeState:
    """Thread-safe runtime state shared by the main Worker and control loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = RuntimeSnapshot("starting", "", "", "")

    def set(
        self,
        state: str,
        *,
        activity: str = "",
        busy_reason: str = "",
        request_id: str = "",
    ) -> None:
        normalized_state = str(state or "").strip()
        if normalized_state not in worker_health.HEARTBEAT_STATES:
            raise ValueError(f"Unsupported Worker runtime state: {state}")
        snapshot = RuntimeSnapshot(
            normalized_state,
            str(activity or "").strip(),
            str(busy_reason or "").strip(),
            str(request_id or "").strip(),
        )
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot


class WorkerControlLoop:
    """A daemon loop which keeps Worker online state independent of long work."""

    def __init__(
        self,
        *,
        client: WorkerControlClient,
        worker_id: str,
        package_version: Callable[[], str],
        package_path: Callable[[], str],
        execution_mode: Callable[[], str],
        snapshot: Callable[[], RuntimeSnapshot],
        mailbox_path: Path,
        interval_seconds: float = 10.0,
        status_refresh_seconds: float = 60.0,
        process_started_at: str | None = None,
    ) -> None:
        self._client = client
        self._worker_id = str(worker_id or "").strip()
        self._package_version = package_version
        self._package_path = package_path
        self._execution_mode = execution_mode
        self._snapshot = snapshot
        self._mailbox_path = Path(mailbox_path)
        self._interval_seconds = max(0.1, float(interval_seconds))
        self._status_refresh_seconds = max(0.0, float(status_refresh_seconds))
        self._process_started_at = str(process_started_at or _utc_now()).strip()
        self._mailbox_lock = threading.RLock()
        self._waiting_lock = threading.Lock()
        self._waiting_status: tuple[str, str, str] | None = None
        self._last_waiting_status: tuple[str, str, str] | None = None
        self._last_waiting_sent_at = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="worker-control", daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds: float = 2.0) -> None:
        self._stop_event.set()
        try:
            self._write_local_heartbeat()
        except Exception as exc:
            print(f"[worker-control] stopping heartbeat deferred: {exc}", flush=True)
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, float(timeout_seconds)))

    def run_once(self) -> dict[str, object] | None:
        try:
            heartbeat = self._write_local_heartbeat()
        except Exception as exc:
            print(f"[worker-control] local heartbeat deferred: {exc}", flush=True)
            return None
        payload = {
            key: value
            for key, value in heartbeat.items()
            if key != "observed_at"
        }
        payload["process_started_at"] = self._process_started_at
        waiting = self._waiting_payload()
        if waiting is not None:
            payload["remote_update"] = waiting
        try:
            response = self._client.control(payload)
        except Exception as exc:
            print(f"[worker-control] control request deferred: {exc}", flush=True)
            return None
        if waiting is not None:
            self._mark_waiting_sent(waiting)
        if not isinstance(response, Mapping):
            print("[worker-control] invalid control response", flush=True)
            return None
        command = response.get("command")
        if isinstance(command, Mapping):
            try:
                self._write_mailbox(command)
            except Exception as exc:
                print(f"[worker-control] mailbox write deferred: {exc}", flush=True)
        return dict(response)

    def _write_local_heartbeat(self) -> dict[str, object]:
        current = self._snapshot()
        heartbeat = worker_health.build_heartbeat(
            worker_id=self._worker_id,
            package_version=str(self._package_version() or "").strip(),
            pid=os.getpid(),
            state=current.state,
            execution_mode=str(self._execution_mode() or "").strip(),
            package_path=str(self._package_path() or "").strip(),
            process_started_at=self._process_started_at,
            activity=current.activity,
            busy_reason=current.busy_reason,
            request_id=current.request_id,
        )
        worker_health.write_json_atomic(worker_health.worker_heartbeat_path(), heartbeat)
        return heartbeat

    def set_remote_update_waiting(self, request_id: str, status: str, detail: str) -> None:
        normalized_request_id = str(request_id or "").strip()
        normalized_status = str(status or "").strip()
        if not normalized_request_id or normalized_status not in WAITING_REMOTE_UPDATE_STATUSES:
            return
        with self._waiting_lock:
            self._waiting_status = (normalized_request_id, normalized_status, str(detail or "").strip())

    def pending_command(self) -> dict[str, object] | None:
        with self._mailbox_lock:
            payload = _read_json_object(self._mailbox_path)
            command = payload.get("command") if payload else None
            normalized = _normalize_command(command)
            if normalized is None:
                return None
            if str(normalized["status"]) in TERMINAL_REMOTE_UPDATE_STATUSES:
                self._remove_mailbox_if_matches(str(normalized["request_id"]))
                return None
            choice = getattr(self._client, "choice", None)
            expected_instance_id = str(getattr(choice, "instance_id", "") or "").strip()
            expected_identity_status = str(getattr(choice, "identity_status", "") or "").strip()
            if expected_identity_status != "verified" or not _route_is_verified(
                payload.get("route"),
                expected_instance_id,
            ):
                return None
            return normalized

    def clear_command(self, request_id: str) -> bool:
        normalized_request_id = str(request_id or "").strip()
        if not normalized_request_id:
            return False
        with self._mailbox_lock:
            removed = self._remove_mailbox_if_matches(normalized_request_id)
        if removed:
            with self._waiting_lock:
                if self._waiting_status is not None and self._waiting_status[0] == normalized_request_id:
                    self._waiting_status = None
        return removed

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                print(f"[worker-control] control loop deferred: {exc}", flush=True)
            jitter = min(1.0, self._interval_seconds * 0.1)
            if self._stop_event.wait(self._interval_seconds + jitter):
                return

    def _waiting_payload(self) -> dict[str, object] | None:
        with self._waiting_lock:
            current = self._waiting_status
            if current is None:
                return None
            now = time.monotonic()
            if current == self._last_waiting_status and now - self._last_waiting_sent_at < self._status_refresh_seconds:
                return None
            request_id, status, detail = current
            return {"request_id": request_id, "status": status, "detail": detail}

    def _mark_waiting_sent(self, payload: Mapping[str, object]) -> None:
        key = (
            str(payload.get("request_id") or "").strip(),
            str(payload.get("status") or "").strip(),
            str(payload.get("detail") or "").strip(),
        )
        with self._waiting_lock:
            self._last_waiting_status = key
            self._last_waiting_sent_at = time.monotonic()

    def _write_mailbox(self, command: Mapping[str, object]) -> bool:
        normalized = _normalize_command(command)
        if normalized is None:
            return False
        request_id = str(normalized["request_id"])
        with self._mailbox_lock:
            if str(normalized["status"]) in TERMINAL_REMOTE_UPDATE_STATUSES:
                self.clear_command(request_id)
                return False
            choice = getattr(self._client, "choice", None)
            route = {
                "name": str(getattr(choice, "route_name", "") or "").strip(),
                "identity_status": str(getattr(choice, "identity_status", "") or "").strip(),
                "instance_id": str(getattr(choice, "instance_id", "") or "").strip(),
            }
            worker_health.write_json_atomic(
                self._mailbox_path,
                {
                    "command": normalized,
                    "received_at": _utc_now(),
                    "route": route,
                },
            )
        return True

    def _remove_mailbox_if_matches(self, request_id: str) -> bool:
        payload = _read_json_object(self._mailbox_path)
        command = payload.get("command") if payload else None
        if not isinstance(command, Mapping) or str(command.get("request_id") or "").strip() != request_id:
            return False
        try:
            self._mailbox_path.unlink()
        except OSError:
            return False
        return True


def _normalize_command(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    request_id = str(value.get("request_id") or "").strip()
    status = str(value.get("status") or "").strip()
    if not request_id or status not in REMOTE_UPDATE_STATUSES:
        return None
    normalized: dict[str, object] = {"request_id": request_id, "status": status}
    for key in MAILBOX_COMMAND_FIELDS - {"request_id", "status", "exit_code"}:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            normalized[key] = item.strip()
    exit_code = value.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        normalized["exit_code"] = exit_code
    return normalized


def _route_is_verified(value: object, expected_instance_id: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    instance_id = str(value.get("instance_id") or "").strip()
    return (
        str(value.get("identity_status") or "").strip() == "verified"
        and bool(instance_id)
        and instance_id == str(expected_instance_id or "").strip()
    )


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
