from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


DEFAULT_MANUAL_TASK_LOCK_MAX_AGE_SECONDS = 600
DEFAULT_MANUAL_TASK_LOCK_GUARD_TIMEOUT_SECONDS = 2.0
_MANUAL_TASK_LOCK_THREAD_GUARD = threading.RLock()


class _InvalidManualTaskLock(ValueError):
    """The lease file was readable JSON but not a valid lease object."""


def manual_task_lock_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / "manual_task_active.lock"


def manual_task_lock_guard_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / "manual_task_active.lock.guard"


def _manual_task_lock_guard_timeout_seconds() -> float:
    raw_value = os.getenv(
        "MANUAL_TASK_LOCK_GUARD_TIMEOUT_SECONDS",
        str(DEFAULT_MANUAL_TASK_LOCK_GUARD_TIMEOUT_SECONDS),
    )
    try:
        return max(0.05, float(raw_value))
    except ValueError:
        return DEFAULT_MANUAL_TASK_LOCK_GUARD_TIMEOUT_SECONDS


@contextmanager
def _manual_task_lock_guard(artifacts_dir: Path):
    """Serialize lease read/replace/clear across threads and processes."""

    guard_path = manual_task_lock_guard_path(artifacts_dir)
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_seconds = _manual_task_lock_guard_timeout_seconds()
    if not _MANUAL_TASK_LOCK_THREAD_GUARD.acquire(timeout=timeout_seconds):
        raise TimeoutError("manual task lock thread guard is busy")
    try:
        descriptor = os.open(guard_path, os.O_CREAT | os.O_RDWR)
        try:
            if os.fstat(descriptor).st_size < 1:
                os.write(descriptor, b"\0")
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("manual task lock guard is busy") from exc
                    time.sleep(0.01)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    finally:
        _MANUAL_TASK_LOCK_THREAD_GUARD.release()


def _task_id_from_owner(owner: str) -> str:
    normalized_owner = str(owner or "").strip()
    for prefix in ("desktop_fast:", "desktop-fast:", "worker-manual:"):
        if normalized_owner.startswith(prefix):
            return normalized_owner[len(prefix) :].split(":", 1)[0].strip()
    return ""


def _read_manual_task_lock(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        raise _InvalidManualTaskLock("manual task lock must be a JSON object")
    return payload


def _manual_task_lock_file_is_stale(path: Path) -> bool:
    return time.time() - path.stat().st_mtime > manual_task_lock_max_age_seconds()


def _quarantine_corrupt_manual_task_lock(path: Path) -> None:
    quarantine_path = path.with_name(
        f"{path.name}.corrupt.{time.time_ns()}.{os.getpid()}.{threading.get_ident()}"
    )
    os.replace(path, quarantine_path)


def _manual_task_lock_payload_is_active(path: Path, payload: dict[str, object]) -> bool:
    if not str(payload.get("owner") or "").strip():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds <= manual_task_lock_max_age_seconds()


def _write_manual_task_lock(path: Path, payload: dict[str, object]) -> None:
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, path)


def acquire_manual_task_lock(artifacts_dir: Path, owner: str, task_id: str = "") -> bool:
    """Atomically acquire the cross-thread/process local execution lease."""

    path = manual_task_lock_path(artifacts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_owner = str(owner or "").strip()
    if not normalized_owner:
        return False
    try:
        with _manual_task_lock_guard(artifacts_dir):
            try:
                current = _read_manual_task_lock(path)
            except (json.JSONDecodeError, _InvalidManualTaskLock):
                if not _manual_task_lock_file_is_stale(path):
                    return False
                _quarantine_corrupt_manual_task_lock(path)
                current = {}
            if _manual_task_lock_payload_is_active(path, current):
                return False
            try:
                path.unlink()
            except OSError:
                pass
            now = time.time()
            payload = json.dumps(
                {
                    "owner": normalized_owner,
                    "task_id": str(task_id or _task_id_from_owner(normalized_owner)).strip(),
                    "started_at": now,
                    "heartbeat_at": now,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return False
            try:
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)
            return True
    except (TimeoutError, OSError, json.JSONDecodeError, _InvalidManualTaskLock):
        return False


def _set_manual_task_lock(
    artifacts_dir: Path,
    owner: str,
    task_id: str = "",
    *,
    require_existing_owner: bool,
) -> bool:
    path = manual_task_lock_path(artifacts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_owner = str(owner or "").strip()
    if not normalized_owner:
        return False
    try:
        with _manual_task_lock_guard(artifacts_dir):
            current = _read_manual_task_lock(path)
            current_owner = str(current.get("owner") or "").strip()
            current_active = (
                _manual_task_lock_payload_is_active(path, current)
                if require_existing_owner or (current_owner and current_owner != normalized_owner)
                else False
            )
            if require_existing_owner and current_owner != normalized_owner:
                return False
            if current_owner and current_owner != normalized_owner and current_active:
                return False
            now = time.time()
            started_at = now
            if current_owner == normalized_owner:
                try:
                    started_at = float(current.get("started_at") or now)
                except (TypeError, ValueError):
                    started_at = now
            current_task_id = (
                str(current.get("task_id") or "").strip()
                if current_owner == normalized_owner
                else ""
            )
            payload = {
                "owner": normalized_owner,
                "task_id": str(
                    task_id or current_task_id or _task_id_from_owner(normalized_owner)
                ).strip(),
                "started_at": started_at,
                "heartbeat_at": now,
            }
            _write_manual_task_lock(path, payload)
            return True
    except (json.JSONDecodeError, _InvalidManualTaskLock):
        return False


def set_manual_task_lock(artifacts_dir: Path, owner: str, task_id: str = "") -> bool:
    """Create a test/manual lease or refresh it when the owner still matches."""

    return _set_manual_task_lock(
        artifacts_dir,
        owner,
        task_id,
        require_existing_owner=False,
    )


def refresh_manual_task_lock(artifacts_dir: Path, owner: str, task_id: str = "") -> bool:
    """Heartbeat only an existing lease still owned by the same execution."""

    return _set_manual_task_lock(
        artifacts_dir,
        owner,
        task_id,
        require_existing_owner=True,
    )


def clear_manual_task_lock(artifacts_dir: Path, owner: str = "") -> bool:
    path = manual_task_lock_path(artifacts_dir)
    try:
        with _manual_task_lock_guard(artifacts_dir):
            if owner:
                payload = _read_manual_task_lock(path)
                if str(payload.get("owner") or "").strip() != owner:
                    return False
            try:
                path.unlink()
            except OSError:
                return False
            return True
    except (TimeoutError, json.JSONDecodeError, _InvalidManualTaskLock):
        return False


def run_with_manual_task_lock_owner(
    artifacts_dir: Path,
    owner: str,
    task_id: str,
    action: Callable[[], object],
    *,
    clear_after: bool = False,
    expected_started_at: object | None = None,
) -> bool:
    """Run a scoped cleanup only while the guarded lease still belongs to this task."""

    path = manual_task_lock_path(artifacts_dir)
    normalized_owner = str(owner or "").strip()
    normalized_task_id = str(task_id or "").strip()
    if not normalized_owner or not normalized_task_id:
        return False
    action_started = False
    try:
        with _manual_task_lock_guard(artifacts_dir):
            payload = _read_manual_task_lock(path)
            payload_owner = str(payload.get("owner") or "").strip()
            payload_task_id = str(
                payload.get("task_id") or _task_id_from_owner(payload_owner)
            ).strip()
            payload_started_at = str(payload.get("started_at") or "").strip()
            normalized_expected_started_at = (
                str(expected_started_at or "").strip()
                if expected_started_at is not None
                else None
            )
            if (
                not _manual_task_lock_payload_is_active(path, payload)
                or payload_owner != normalized_owner
                or payload_task_id != normalized_task_id
                or (
                    normalized_expected_started_at is not None
                    and payload_started_at != normalized_expected_started_at
                )
            ):
                return False
            action_started = True
            action()
            if clear_after:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            return True
    except (TimeoutError, OSError, json.JSONDecodeError, _InvalidManualTaskLock):
        if action_started:
            raise
        return False


def run_with_manual_task_lock_absent(
    artifacts_dir: Path,
    action: Callable[[], object],
) -> bool:
    """Run an action only while the guarded execution lease is still absent."""

    path = manual_task_lock_path(artifacts_dir)
    action_started = False
    try:
        with _manual_task_lock_guard(artifacts_dir):
            try:
                payload = _read_manual_task_lock(path)
            except (json.JSONDecodeError, _InvalidManualTaskLock):
                if not _manual_task_lock_file_is_stale(path):
                    return False
                _quarantine_corrupt_manual_task_lock(path)
                payload = {}
            if _manual_task_lock_payload_is_active(path, payload):
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return False
            action_started = True
            action()
            return True
    except (TimeoutError, OSError, json.JSONDecodeError, _InvalidManualTaskLock):
        if action_started:
            raise
        return False


def manual_task_lock_max_age_seconds() -> int:
    raw_value = os.getenv(
        "MANUAL_TASK_LOCK_MAX_AGE_SECONDS",
        str(DEFAULT_MANUAL_TASK_LOCK_MAX_AGE_SECONDS),
    )
    try:
        return max(60, int(raw_value))
    except ValueError:
        return DEFAULT_MANUAL_TASK_LOCK_MAX_AGE_SECONDS


def manual_task_lock_snapshot(artifacts_dir: Path) -> dict[str, object]:
    """Read one owner/task snapshot and remove stale data under the same guard."""

    path = manual_task_lock_path(artifacts_dir)
    try:
        with _manual_task_lock_guard(artifacts_dir):
            try:
                payload = _read_manual_task_lock(path)
            except (json.JSONDecodeError, _InvalidManualTaskLock):
                if not _manual_task_lock_file_is_stale(path):
                    raise
                _quarantine_corrupt_manual_task_lock(path)
                return {}
            if _manual_task_lock_payload_is_active(path, payload):
                owner = str(payload.get("owner") or "").strip()
                return {
                    **payload,
                    "owner": owner,
                    "task_id": str(payload.get("task_id") or _task_id_from_owner(owner)).strip(),
                }
            try:
                path.unlink()
            except OSError:
                pass
            return {}
    except (TimeoutError, OSError, json.JSONDecodeError, _InvalidManualTaskLock):
        return {"guard_busy": True}


def manual_task_lock_active(artifacts_dir: Path) -> bool:
    snapshot = manual_task_lock_snapshot(artifacts_dir)
    return bool(snapshot.get("owner") or snapshot.get("guard_busy"))


def manual_task_lock_owner(artifacts_dir: Path) -> str:
    """Return the active lease owner without exposing a stale lock as active."""

    return str(manual_task_lock_snapshot(artifacts_dir).get("owner") or "").strip()


def manual_task_lock_task_id(artifacts_dir: Path) -> str:
    """Return the task identity bound to the active cross-process lease."""

    return str(manual_task_lock_snapshot(artifacts_dir).get("task_id") or "").strip()


def bind_manual_task_lock_task(artifacts_dir: Path, owner: str, task_id: str) -> bool:
    """Bind an already-owned placeholder lease to the task claimed afterwards."""

    normalized_owner = str(owner or "").strip()
    normalized_task_id = str(task_id or "").strip()
    if not normalized_owner or not normalized_task_id:
        return False
    if not refresh_manual_task_lock(
        artifacts_dir,
        normalized_owner,
        task_id=normalized_task_id,
    ):
        return False
    snapshot = manual_task_lock_snapshot(artifacts_dir)
    return (
        str(snapshot.get("owner") or "").strip() == normalized_owner
        and str(snapshot.get("task_id") or "").strip() == normalized_task_id
    )
