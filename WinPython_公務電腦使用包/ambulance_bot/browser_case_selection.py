from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


class BrowserCaseSelectionStore:
    """Short-lived, single-use case selections referenced by opaque browser cookies."""

    def __init__(self, path: Path, ttl_seconds: int = 300):
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 0:
            raise ValueError("ttl_seconds must be a non-negative integer")
        self.path = Path(path).resolve()
        self.ttl_seconds = ttl_seconds
        key = os.path.normcase(str(self.path))
        with _LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(key, threading.RLock())

    def put(self, selected_case: dict) -> str:
        if not isinstance(selected_case, dict):
            raise TypeError("selected_case must be a dictionary")
        # Snapshot the case data supplied by the caller before it can be mutated.
        payload = json.loads(json.dumps(selected_case, ensure_ascii=False, allow_nan=False))
        with self._lock:
            now = time.time()
            selections = self._read()
            self._prune(selections, now)
            token = secrets.token_urlsafe(32)
            while token in selections:
                token = secrets.token_urlsafe(32)
            selections[token] = {"expires_at": now + self.ttl_seconds, "payload": payload}
            self._write(selections)
            return token

    def pop(self, token: str) -> dict:
        if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
            return {}
        with self._lock:
            selections = self._read()
            pruned = self._prune(selections, time.time())
            selection = selections.pop(token, None)
            if selection is not None or pruned:
                # Publish consumption before returning data, including across instances.
                self._write(selections)
            return selection["payload"] if selection is not None else {}

    def _read(self) -> dict:
        try:
            selections = json.loads(self.path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
        except (OSError, UnicodeError, ValueError):
            return {}
        if not isinstance(selections, dict):
            return {}
        for token, selection in selections.items():
            if not _TOKEN_PATTERN.fullmatch(token) or not isinstance(selection, dict):
                return {}
            if set(selection) != {"expires_at", "payload"} or not isinstance(selection["payload"], dict):
                return {}
            expires_at = selection["expires_at"]
            try:
                if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
                    return {}
            except OverflowError:
                return {}
        return selections

    @staticmethod
    def _prune(selections: dict, now: float) -> bool:
        expired = [token for token, selection in selections.items() if selection["expires_at"] <= now]
        for token in expired:
            selections.pop(token)
        return bool(expired)

    def _write(self, selections: dict) -> None:
        encoded = json.dumps(selections, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A fixed staging path is protected by the shared per-path lock; no per-token files.
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
