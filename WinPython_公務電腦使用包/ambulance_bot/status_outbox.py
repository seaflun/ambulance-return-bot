from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_EVENT_ID_PATTERN = re.compile(r"^[0-9]{20}-[0-9a-f]{32}$")


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _interprocess_lock(path: Path) -> Iterator[None]:
    """Hold a one-byte lock shared by all worker processes using this spool."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - the production worker runs on Windows
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class WorkerStatusOutbox:
    """Durable FIFO status spool with atomic consumer claims."""

    def __init__(self, root_dir: Path, *, claim_lease_seconds: float = 300.0) -> None:
        self.root_dir = Path(root_dir)
        self.pending_dir = self.root_dir / "pending"
        self.inflight_dir = self.root_dir / "inflight"
        self.quarantine_dir = self.root_dir / "quarantine"
        self.dead_letter_dir = self.root_dir / "dead_letter"
        self.sequence_path = self.root_dir / ".sequence"
        self.process_lock_path = self.root_dir / ".lock"
        self.claim_lease_seconds = max(1.0, float(claim_lease_seconds))
        self._lock = _lock_for(self.root_dir)

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self._lock:
            with _interprocess_lock(self.process_lock_path):
                yield

    def enqueue(self, payload: dict[str, Any]) -> str:
        with self._exclusive():
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            sequence = self._next_sequence()
            event_id = f"{sequence:020d}-{uuid.uuid4().hex}"
            record = {
                "event_id": event_id,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "payload": dict(payload),
            }
            self._write_json_atomic(self.pending_dir / f"{event_id}.json", record)
            return event_id

    def pending(self) -> list[dict[str, Any]]:
        """Return unclaimed valid records in durable sequence order."""

        with self._exclusive():
            if not self.pending_dir.exists():
                return []
            entries: list[dict[str, Any]] = []
            for path in sorted(self.pending_dir.glob("*.json"), key=lambda item: item.name):
                record = self._read_record(path)
                if record is not None:
                    entries.append(record)
            return entries

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically move and return the oldest event for one consumer."""

        with self._exclusive():
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            self.inflight_dir.mkdir(parents=True, exist_ok=True)
            self._recover_expired_claims()
            for claimed_path in sorted(self.inflight_dir.glob("*.json"), key=lambda item: item.name):
                if self._read_record(claimed_path) is not None:
                    # A single active claim preserves FIFO even when two worker
                    # processes accidentally run at the same time.
                    return None
                if claimed_path.exists():
                    # A locked malformed claim could not be quarantined. Do not
                    # let a newer status pass it.
                    return None
            for path in sorted(self.pending_dir.glob("*.json"), key=lambda item: item.name):
                record = self._read_record(path)
                if record is None:
                    if path.exists():
                        return None
                    continue
                target = self.inflight_dir / path.name
                try:
                    os.replace(path, target)
                    # A queued event can be old. Refresh mtime only after the atomic
                    # claim so lease recovery measures claim age, not event age.
                    os.utime(target, None)
                except FileNotFoundError:
                    continue
                except OSError:
                    # A transient Windows sharing violation must not consume the
                    # event. Preserve FIFO by retrying the oldest event later.
                    return None
                return record
            return None

    def ack(self, event_id: str) -> bool:
        safe_id = self._safe_event_id(event_id)
        if safe_id is None:
            return False
        with self._exclusive():
            for directory in (self.inflight_dir, self.pending_dir):
                try:
                    (directory / f"{safe_id}.json").unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    # The POST may already have succeeded. Leave the claimed
                    # record for lease recovery and an idempotent replay.
                    return False
            return True

    def release(self, event_id: str) -> bool:
        """Return a claimed event to the FIFO after a transient send failure."""

        safe_id = self._safe_event_id(event_id)
        if safe_id is None:
            return False
        with self._exclusive():
            source = self.inflight_dir / f"{safe_id}.json"
            target = self.pending_dir / source.name
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            try:
                if target.exists():
                    source.unlink()
                else:
                    os.replace(source, target)
            except FileNotFoundError:
                return True
            except OSError:
                # Leave it inflight; lease recovery will make it available again.
                return False
            return True

    def reject(self, event_id: str, reason: str = "") -> bool:
        """Move a valid but permanently rejected event out of the live FIFO."""

        safe_id = self._safe_event_id(event_id)
        if safe_id is None:
            return False
        with self._exclusive():
            self.dead_letter_dir.mkdir(parents=True, exist_ok=True)
            source = self.inflight_dir / f"{safe_id}.json"
            if not source.exists():
                source = self.pending_dir / f"{safe_id}.json"
            target = self.dead_letter_dir / f"{safe_id}.json"
            try:
                if target.exists():
                    source.unlink()
                else:
                    os.replace(source, target)
            except FileNotFoundError:
                return target.exists()
            except OSError:
                return False
            if reason:
                try:
                    self._write_text_atomic(
                        self.dead_letter_dir / f"{safe_id}.reason.txt",
                        str(reason)[:2000],
                    )
                except OSError:
                    pass
            self._trim_dead_letters()
            return True

    def _next_sequence(self) -> int:
        current = 0
        try:
            value = self.sequence_path.read_text(encoding="ascii").strip()
            current = int(value or "0")
        except (FileNotFoundError, OSError, ValueError):
            # Recover safely if an old/incomplete install has no counter. The lock
            # ensures another producer cannot create a colliding sequence here.
            for directory in (self.pending_dir, self.inflight_dir):
                if not directory.exists():
                    continue
                for path in directory.glob("*.json"):
                    try:
                        current = max(current, int(path.name.split("-", 1)[0]))
                    except (ValueError, IndexError):
                        continue
        sequence = current + 1
        self._write_text_atomic(self.sequence_path, str(sequence))
        return sequence

    def _read_record(self, path: Path) -> dict[str, Any] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            # A legitimate file can be briefly unreadable while antivirus or
            # indexing software holds it. Retry later; do not quarantine it.
            return None
        try:
            record = json.loads(text)
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                raise ValueError("outbox record must contain an object payload")
            event_id = str(record.get("event_id") or "").strip()
            if self._safe_event_id(event_id) is None or event_id != path.stem:
                raise ValueError("outbox event identity does not match its filename")
            return record
        except (ValueError, json.JSONDecodeError):
            self._quarantine(path)
            return None

    def _trim_dead_letters(self, limit: int = 256) -> None:
        records = sorted(self.dead_letter_dir.glob("*.json"), key=lambda item: item.name)
        for path in records[:-max(1, int(limit))]:
            try:
                path.unlink()
                path.with_suffix(".reason.txt").unlink(missing_ok=True)
            except OSError:
                continue

    def _recover_expired_claims(self) -> None:
        if not self.inflight_dir.exists():
            return
        now = time.time()
        for path in sorted(self.inflight_dir.glob("*.json"), key=lambda item: item.name):
            try:
                expired = now - path.stat().st_mtime >= self.claim_lease_seconds
            except OSError:
                continue
            if not expired:
                continue
            target = self.pending_dir / path.name
            try:
                if target.exists():
                    self._quarantine(path)
                else:
                    os.replace(path, target)
            except OSError:
                continue

    @staticmethod
    def _safe_event_id(event_id: object) -> str | None:
        value = str(event_id or "").strip()
        return value if _EVENT_ID_PATTERN.fullmatch(value) else None

    def _quarantine(self, path: Path) -> bool:
        try:
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)
            target = self.quarantine_dir / path.name
            if target.exists():
                target = self.quarantine_dir / f"{path.stem}-{uuid.uuid4().hex}{path.suffix}"
            os.replace(path, target)
            return True
        except OSError:
            # Antivirus/indexer locks are transient on Windows. Skip the poison
            # record for this pass so later valid records still replay.
            return False

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        WorkerStatusOutbox._write_bytes_atomic(
            path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    @staticmethod
    def _write_text_atomic(path: Path, value: str) -> None:
        WorkerStatusOutbox._write_bytes_atomic(path, value.encode("utf-8"))

    @staticmethod
    def _write_bytes_atomic(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
