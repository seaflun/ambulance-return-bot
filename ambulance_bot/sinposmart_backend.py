# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


SINPOSMART_RECORD_TYPES = {
    "login",
    "login_failed",
    "action_queued",
    "action_result",
    "schedule_snapshot",
    "comparison_snapshot",
    "error",
}
SINPOSMART_EVENT_FIELDS = (
    "event_id",
    "occurred_at",
    "fire_day",
    "record_type",
    "actor_no",
    "user_id",
    "display_name",
    "trigger_type",
    "status",
    "item_kind",
    "item_title",
    "content",
    "error",
    "source",
    "target",
    "target_time",
    "result_ref",
    "snapshot",
)
SENSITIVE_KEY_PATTERN = re.compile(r"(password|passwd|pwd|token|secret|cookie|authorization|credential)", re.I)
SENSITIVE_TEXT_PATTERN = re.compile(r"(?i)(password|token|secret|cookie|authorization)\s*[:=]\s*[^,\s;]+")


def sinposmart_fire_day_for(value: datetime | None = None) -> str:
    value = value or datetime.now()
    business_date = value.date() if value.hour >= 8 else value.date() - timedelta(days=1)
    return business_date.isoformat()


def sinposmart_fire_day_label(value: str) -> str:
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        return str(value or "未知消防日")
    end_day = day + timedelta(days=1)
    return f"{day:%Y/%m/%d} 08:00 - {end_day:%m/%d} 08:00"


def sinposmart_record_type_label(value: str) -> str:
    labels = {
        "login": "登入",
        "login_failed": "登入失敗",
        "action_queued": "加入佇列",
        "action_result": "登打結果",
        "schedule_snapshot": "整日勤務",
        "comparison_snapshot": "已登打資料",
        "error": "錯誤",
    }
    return labels.get(str(value or ""), str(value or "事件"))


def sinposmart_trigger_label(value: str) -> str:
    labels = {
        "manual": "手動",
        "due": "自動",
        "login": "登入",
        "schedule": "勤務快照",
        "comparison": "比對快照",
        "system": "系統",
    }
    return labels.get(str(value or ""), str(value or "未標示"))


def sinposmart_status_class(value: str) -> str:
    text = str(value or "").lower()
    if any(word in text for word in ("failed", "error", "fail", "失敗")):
        return "failed"
    if any(word in text for word in ("running", "queued", "pending", "manual_marked")):
        return "running"
    if any(word in text for word in ("submitted", "saved", "success", "skipped_duplicate", "completed", "ok")):
        return "complete"
    return "idle"


class SinpoSmartBackendStore:
    def __init__(self, root_dir: Path, retention_days: int = 7) -> None:
        self.root_dir = root_dir
        self.retention_days = max(1, retention_days)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def upsert_event(self, raw_event: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        self.cleanup(now)
        event = normalize_sinposmart_event(raw_event, now=now)
        path = self.path_for_day(event["fire_day"])
        payload = self.read_day(event["fire_day"])
        events = list(payload.get("events") or [])
        known_ids = {str(item.get("event_id") or "") for item in events if isinstance(item, dict)}
        if event["event_id"] not in known_ids:
            events.append(event)
        payload["fire_day"] = event["fire_day"]
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        payload["events"] = sorted(events, key=lambda item: str(item.get("occurred_at") or ""))
        payload["summary"] = summarize_sinposmart_events(payload["events"])
        write_json_atomic(path, payload)
        return event

    def list_days(self, limit: int = 7, now: datetime | None = None) -> list[dict[str, Any]]:
        self.cleanup(now)
        days: list[dict[str, Any]] = []
        for path in sorted(self.root_dir.glob("*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                payload.setdefault("events", [])
                payload["summary"] = summarize_sinposmart_events(payload.get("events") or [])
                days.append(payload)
        return days[:limit]

    def read_day(self, fire_day: str) -> dict[str, Any]:
        path = self.path_for_day(fire_day)
        if not path.exists():
            return {"fire_day": fire_day, "updated_at": "", "summary": {}, "events": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"fire_day": fire_day, "updated_at": "", "summary": {}, "events": []}
        return payload if isinstance(payload, dict) else {"fire_day": fire_day, "updated_at": "", "summary": {}, "events": []}

    def cleanup(self, now: datetime | None = None) -> None:
        current_day = date.fromisoformat(sinposmart_fire_day_for(now))
        cutoff = current_day - timedelta(days=self.retention_days - 1)
        for path in self.root_dir.glob("*.json"):
            try:
                fire_day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if fire_day < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass

    def path_for_day(self, fire_day: str) -> Path:
        safe_day = str(fire_day or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", safe_day):
            safe_day = sinposmart_fire_day_for()
        return self.root_dir / f"{safe_day}.json"


def normalize_sinposmart_event(raw_event: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    occurred_at = parse_event_datetime(raw_event.get("occurred_at"), now)
    record_type = str(raw_event.get("record_type") or "error").strip()
    if record_type not in SINPOSMART_RECORD_TYPES:
        record_type = "error"
    fire_day = str(raw_event.get("fire_day") or sinposmart_fire_day_for(occurred_at)).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fire_day):
        fire_day = sinposmart_fire_day_for(occurred_at)
    event = {
        "event_id": str(raw_event.get("event_id") or uuid4()).strip(),
        "occurred_at": occurred_at.isoformat(timespec="seconds"),
        "fire_day": fire_day,
        "record_type": record_type,
        "actor_no": sanitize_scalar(raw_event.get("actor_no"), 40),
        "user_id": sanitize_scalar(raw_event.get("user_id"), 120),
        "display_name": sanitize_scalar(raw_event.get("display_name"), 160),
        "trigger_type": sanitize_scalar(raw_event.get("trigger_type"), 40),
        "status": sanitize_scalar(raw_event.get("status"), 80),
        "item_kind": sanitize_scalar(raw_event.get("item_kind"), 80),
        "item_title": sanitize_scalar(raw_event.get("item_title"), 240),
        "content": sanitize_scalar(raw_event.get("content"), 1200),
        "error": sanitize_scalar(raw_event.get("error"), 1200),
        "source": sanitize_scalar(raw_event.get("source"), 120),
        "target": sanitize_scalar(raw_event.get("target"), 120),
        "target_time": sanitize_scalar(raw_event.get("target_time"), 80),
        "result_ref": sanitize_scalar(raw_event.get("result_ref"), 260),
        "snapshot": sanitize_value(raw_event.get("snapshot"), depth=0),
    }
    return {field: event[field] for field in SINPOSMART_EVENT_FIELDS}


def parse_event_datetime(value: Any, fallback: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        return fallback
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return fallback
    return parsed.replace(tzinfo=None)


def sanitize_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return ""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SENSITIVE_KEY_PATTERN.search(key_text):
                continue
            clean[key_text[:80]] = sanitize_value(item, depth + 1)
        return clean
    if isinstance(value, list):
        return [sanitize_value(item, depth + 1) for item in value[:200]]
    return sanitize_scalar(value, 1200)


def sanitize_scalar(value: Any, max_length: int) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "").strip()
    text = SENSITIVE_TEXT_PATTERN.sub(r"\1=***", text)
    return text[:max_length]


def summarize_sinposmart_events(events: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "total": 0,
        "login": 0,
        "manual": 0,
        "auto": 0,
        "success": 0,
        "failed": 0,
        "schedule_snapshots": 0,
        "comparison_snapshots": 0,
    }
    for event in events:
        if not isinstance(event, dict):
            continue
        summary["total"] += 1
        record_type = str(event.get("record_type") or "")
        trigger_type = str(event.get("trigger_type") or "")
        status_class = sinposmart_status_class(str(event.get("status") or ""))
        if record_type == "login":
            summary["login"] += 1
        if trigger_type == "manual":
            summary["manual"] += 1
        if trigger_type == "due":
            summary["auto"] += 1
        if status_class == "complete":
            summary["success"] += 1
        if status_class == "failed" or record_type in {"login_failed", "error"}:
            summary["failed"] += 1
        if record_type == "schedule_snapshot":
            summary["schedule_snapshots"] += 1
        if record_type == "comparison_snapshot":
            summary["comparison_snapshots"] += 1
    return summary


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
