from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class TaskCancellationError(RuntimeError):
    """A task was fenced before its next protected external side effect."""


class _InvalidTaskCancellationMarker(ValueError):
    """The marker was readable JSON but not a valid cancellation object."""


_TASK_CANCELLATION_THREAD_GUARD = threading.RLock()
_TASK_CANCELLATION_GUARD_TIMEOUT_SECONDS = 2.0


def task_cancellation_marker_path(artifacts_dir: Path, task_id: str) -> Path:
    normalized_task_id = str(task_id or "").strip()
    digest = hashlib.sha256(normalized_task_id.encode("utf-8")).hexdigest()
    return Path(artifacts_dir) / "task_cancellations" / f"{digest}.json"


def task_cancellation_guard_path(artifacts_dir: Path, task_id: str) -> Path:
    return task_cancellation_marker_path(artifacts_dir, task_id).with_suffix(".guard")


@contextmanager
def _task_cancellation_guard(artifacts_dir: Path, task_id: str):
    """Serialize marker compare/replace/unlink across threads and processes."""

    guard_path = task_cancellation_guard_path(artifacts_dir, task_id)
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    if not _TASK_CANCELLATION_THREAD_GUARD.acquire(timeout=_TASK_CANCELLATION_GUARD_TIMEOUT_SECONDS):
        raise TimeoutError("task cancellation thread guard is busy")
    try:
        descriptor = os.open(guard_path, os.O_CREAT | os.O_RDWR)
        try:
            if os.fstat(descriptor).st_size < 1:
                os.write(descriptor, b"\0")
            deadline = time.monotonic() + _TASK_CANCELLATION_GUARD_TIMEOUT_SECONDS
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
                        raise TimeoutError("task cancellation guard is busy") from exc
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
        _TASK_CANCELLATION_THREAD_GUARD.release()


def _read_task_cancellation_marker(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        raise _InvalidTaskCancellationMarker("task cancellation marker must be a JSON object")
    return payload


def _quarantine_corrupt_task_cancellation_marker(path: Path) -> None:
    quarantine_path = path.with_name(
        f"{path.name}.corrupt.{time.time_ns()}.{os.getpid()}.{threading.get_ident()}"
    )
    try:
        os.replace(path, quarantine_path)
    except FileNotFoundError:
        return


def _task_cancellation_payload_matches(
    payload: dict[str, object],
    task_id: str,
    *,
    execution_owner: str = "",
    claim_id: str = "",
) -> bool:
    if str(payload.get("task_id") or "").strip() != task_id:
        return False
    marker_owner = str(payload.get("execution_owner") or "").strip()
    marker_claim_id = str(payload.get("claim_id") or "").strip()
    if marker_owner and marker_claim_id:
        return marker_owner == execution_owner and marker_claim_id == claim_id
    if marker_owner:
        return bool(execution_owner and marker_owner == execution_owner)
    if marker_claim_id:
        return bool(claim_id and marker_claim_id == claim_id)
    return False


def request_task_cancellation(
    artifacts_dir: Path,
    task_id: str,
    *,
    execution_owner: str = "",
    claim_id: str = "",
) -> Path:
    normalized_task_id = str(task_id or "").strip()
    normalized_owner = str(execution_owner or "").strip()
    normalized_claim_id = str(claim_id or "").strip()
    if not normalized_task_id or (not normalized_owner and not normalized_claim_id):
        raise ValueError("task cancellation requires a task and an execution owner or claim")
    path = task_cancellation_marker_path(artifacts_dir, normalized_task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": normalized_task_id,
        "execution_owner": normalized_owner,
        "claim_id": normalized_claim_id,
        "requested_at": time.time(),
    }
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with _task_cancellation_guard(artifacts_dir, normalized_task_id):
            temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass
    return path


def task_cancellation_requested(
    artifacts_dir: Path,
    task_id: str,
    *,
    execution_owner: str = "",
    claim_id: str = "",
) -> bool:
    normalized_task_id = str(task_id or "").strip()
    normalized_owner = str(execution_owner or "").strip()
    normalized_claim_id = str(claim_id or "").strip()
    path = task_cancellation_marker_path(artifacts_dir, normalized_task_id)
    try:
        with _task_cancellation_guard(artifacts_dir, normalized_task_id):
            payload = _read_task_cancellation_marker(path)
            return _task_cancellation_payload_matches(
                payload,
                normalized_task_id,
                execution_owner=normalized_owner,
                claim_id=normalized_claim_id,
            )
    except (OSError, TimeoutError, json.JSONDecodeError, _InvalidTaskCancellationMarker):
        # If scoped cancellation state cannot be read safely, protect the next
        # external side effect by failing closed for that execution.
        return bool(normalized_owner or normalized_claim_id)


def clear_task_cancellation(
    artifacts_dir: Path,
    task_id: str,
    *,
    execution_owner: str = "",
    claim_id: str = "",
) -> None:
    normalized_task_id = str(task_id or "").strip()
    normalized_owner = str(execution_owner or "").strip()
    normalized_claim_id = str(claim_id or "").strip()
    path = task_cancellation_marker_path(artifacts_dir, normalized_task_id)
    try:
        with _task_cancellation_guard(artifacts_dir, normalized_task_id):
            if normalized_owner or normalized_claim_id:
                try:
                    payload = _read_task_cancellation_marker(path)
                except (json.JSONDecodeError, _InvalidTaskCancellationMarker):
                    _quarantine_corrupt_task_cancellation_marker(path)
                    return
                if not _task_cancellation_payload_matches(
                    payload,
                    normalized_task_id,
                    execution_owner=normalized_owner,
                    claim_id=normalized_claim_id,
                ):
                    return
            try:
                path.unlink()
            except OSError:
                pass
    except (OSError, TimeoutError):
        # Leaving a marker in place is safer than deleting another generation.
        return
