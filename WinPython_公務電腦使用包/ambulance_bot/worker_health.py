"""Local Worker health, activity, and GUI restart primitives."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


HEARTBEAT_STATES = frozenset(
    {
        "starting",
        "idle",
        "busy",
        "update_handoff",
        "recovering",
        "stopping",
    }
)


@dataclass(frozen=True)
class GuiRestartDecision:
    should_restart: bool
    reason: str
    retained_restart_times: tuple[float, ...]


def state_root() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "AmbulanceReturnBot"


def worker_heartbeat_path() -> Path:
    return state_root() / "worker_heartbeat.json"


def worker_activity_path() -> Path:
    return state_root() / "worker_activity.json"


def worker_control_mailbox_path() -> Path:
    return state_root() / "worker_control_mailbox.json"


def self_recovery_state_path() -> Path:
    return state_root() / "self_recovery_state.json"


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def build_heartbeat(
    *,
    worker_id: str,
    package_version: str,
    pid: int,
    state: str,
    execution_mode: str,
    package_path: str,
    process_started_at: str,
    activity: str = "",
    busy_reason: str = "",
    request_id: str = "",
    observed_at: datetime | None = None,
) -> dict[str, object]:
    if state not in HEARTBEAT_STATES:
        raise ValueError(f"Unsupported worker heartbeat state: {state}")
    return {
        "worker_id": worker_id,
        "package_version": package_version,
        "pid": pid,
        "state": state,
        "execution_mode": execution_mode,
        "package_path": package_path,
        "process_started_at": process_started_at,
        "activity": activity,
        "busy_reason": busy_reason,
        "request_id": request_id,
        "observed_at": _as_utc(observed_at).isoformat(),
    }


def write_activity(
    *,
    activity: str,
    owner: str,
    observed_at: datetime | None = None,
) -> None:
    write_json_atomic(
        worker_activity_path(),
        {
            "activity": activity,
            "owner": owner,
            "updated_at": _as_utc(observed_at).isoformat(),
        },
    )


def clear_activity(owner: str) -> bool:
    path = worker_activity_path()
    try:
        payload = _read_json(path)
    except (OSError, ValueError):
        return False
    if payload.get("owner") != owner:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def activity_is_fresh(max_age_seconds: float, now: datetime | None = None) -> bool:
    if max_age_seconds < 0:
        return False
    try:
        payload = _read_json(worker_activity_path())
        updated_at = _parse_utc_timestamp(payload["updated_at"])
    except (KeyError, OSError, TypeError, ValueError):
        return False
    age_seconds = (_as_utc(now) - updated_at).total_seconds()
    return 0 <= age_seconds <= max_age_seconds


def decide_gui_restart(
    *,
    now_monotonic: float,
    thread_alive: bool,
    stopped_at: float | None,
    activity_active: bool,
    update_active: bool,
    restart_times: Sequence[float],
    grace_seconds: float = 15.0,
    window_seconds: float = 600.0,
    max_restarts: int = 3,
) -> GuiRestartDecision:
    retained = tuple(value for value in restart_times if now_monotonic - value <= window_seconds)
    if thread_alive:
        return GuiRestartDecision(False, "thread_alive", retained)
    if stopped_at is None or now_monotonic - stopped_at < grace_seconds:
        return GuiRestartDecision(False, "within_grace", retained)
    if activity_active:
        return GuiRestartDecision(False, "activity_active", retained)
    if update_active:
        return GuiRestartDecision(False, "update_active", retained)
    if len(retained) >= max_restarts:
        return GuiRestartDecision(False, "restart_rate_limited", retained)
    return GuiRestartDecision(True, "safe_to_restart", retained)


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Worker state JSON must be an object.")
    return payload


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Worker activity timestamp must be a string.")
    return _as_utc(datetime.fromisoformat(value))
