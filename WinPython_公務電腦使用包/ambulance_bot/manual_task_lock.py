from __future__ import annotations

import json
import os
import time
from pathlib import Path


DEFAULT_MANUAL_TASK_LOCK_MAX_AGE_SECONDS = 600


def manual_task_lock_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / "manual_task_active.lock"


def set_manual_task_lock(artifacts_dir: Path, owner: str) -> None:
    path = manual_task_lock_path(artifacts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "owner": owner,
        "started_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def clear_manual_task_lock(artifacts_dir: Path, owner: str = "") -> None:
    path = manual_task_lock_path(artifacts_dir)
    if owner:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if str(payload.get("owner") or "") != owner:
            return
    try:
        path.unlink()
    except OSError:
        pass


def manual_task_lock_max_age_seconds() -> int:
    raw_value = os.getenv("MANUAL_TASK_LOCK_MAX_AGE_SECONDS", str(DEFAULT_MANUAL_TASK_LOCK_MAX_AGE_SECONDS))
    try:
        return max(60, int(raw_value))
    except ValueError:
        return DEFAULT_MANUAL_TASK_LOCK_MAX_AGE_SECONDS


def manual_task_lock_active(artifacts_dir: Path) -> bool:
    path = manual_task_lock_path(artifacts_dir)
    if not path.exists():
        return False
    max_age_seconds = manual_task_lock_max_age_seconds()
    try:
        age_seconds = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if age_seconds > max_age_seconds:
        clear_manual_task_lock(artifacts_dir)
        return False
    return True
