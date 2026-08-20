from __future__ import annotations

import json
from pathlib import Path
import threading
from uuid import uuid4


_PREFERENCES_LOCK = threading.RLock()
_PREFERENCES_FILENAME = "civilpower_volunteer_preferences.json"


def normalize_frequent_member_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for member_id in value:
        if not isinstance(member_id, str):
            continue
        cleaned = " ".join(member_id.split())
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def load_frequent_member_ids(artifacts_dir: Path) -> list[str]:
    path = Path(artifacts_dir) / "settings" / _PREFERENCES_FILENAME
    with _PREFERENCES_LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
    return normalize_frequent_member_ids(payload)


def save_frequent_member_ids(member_ids: object, artifacts_dir: Path) -> list[str]:
    normalized = normalize_frequent_member_ids(member_ids)
    path = Path(artifacts_dir) / "settings" / _PREFERENCES_FILENAME
    with _PREFERENCES_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return normalized
