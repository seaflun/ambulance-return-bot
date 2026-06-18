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
    "logout",
    "action_queued",
    "action_result",
    "tool_action_started",
    "tool_action_finished",
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
    "repeat_count",
    "first_occurred_at",
    "last_occurred_at",
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
        "logout": "登出",
        "action_queued": "加入佇列",
        "action_result": "登打結果",
        "tool_action_started": "工具開始",
        "tool_action_finished": "工具結果",
        "schedule_snapshot": "整日勤務",
        "comparison_snapshot": "已登打資料",
        "error": "錯誤",
    }
    return labels.get(str(value or ""), "事件")


def sinposmart_trigger_label(value: str) -> str:
    labels = {
        "manual": "手動",
        "due": "自動",
        "login": "登入",
        "schedule": "勤務快照",
        "comparison": "比對快照",
        "tool_start": "工具開始",
        "system": "系統",
    }
    return labels.get(str(value or ""), "未標示")


def sinposmart_status_class(value: str) -> str:
    text = str(value or "").lower()
    if any(word in text for word in ("failed", "error", "fail", "失敗")):
        return "failed"
    if any(word in text for word in ("running", "queued", "pending", "manual_marked", "started")):
        return "running"
    if any(word in text for word in ("submitted", "saved", "success", "skipped_duplicate", "completed", "ok")):
        return "complete"
    return "idle"


def sinposmart_status_label(value: str, record_type: str = "") -> str:
    text = str(value or "").strip()
    labels = {
        "started": "開始",
        "submitted": "已登打",
        "ok": "成功",
        "success": "成功",
        "saved": "已儲存",
        "completed": "完成",
        "skipped_duplicate": "已存在",
        "running": "執行中",
        "queued": "等待中",
        "pending": "等待中",
        "manual_marked": "已手動標記",
        "failed": "失敗",
        "fail": "失敗",
        "error": "錯誤",
    }
    if text.lower() in labels:
        return labels[text.lower()]
    if text:
        if re.search(r"[A-Za-z_]", text):
            return sinposmart_record_type_label(record_type)
        return text
    return sinposmart_record_type_label(record_type)


def sinposmart_person_label(event: dict[str, Any]) -> str:
    display_name = str(event.get("display_name") or "").strip()
    if " - " in display_name:
        display_name = display_name.split(" - ", 1)[0].strip()
    actor_no = str(event.get("actor_no") or "").strip()
    if display_name:
        return display_name
    if actor_no:
        return f"番號 {actor_no}"
    return "未知使用者"


def sinposmart_person_label_score(label: str) -> int:
    text = str(label or "").strip()
    if not text or text == "未知使用者":
        return 0
    if re.search(r"\btyfd\d+\b", text, re.I):
        return 1
    if re.search(r"[\u4e00-\u9fff]{2,}", text):
        return 4
    if text.startswith("番號 "):
        return 2
    return 3


def build_sinposmart_preferred_person_labels(events: list[dict[str, Any]]) -> dict[str, str]:
    preferred: dict[str, str] = {}
    scores: dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        actor_no = str(event.get("actor_no") or "").strip()
        if not actor_no:
            continue
        label = sinposmart_person_label(event)
        score = sinposmart_person_label_score(label)
        if score > scores.get(actor_no, -1):
            preferred[actor_no] = label
            scores[actor_no] = score
    return preferred


def sinposmart_action_status_label(value: str, record_type: str = "") -> str:
    text = str(value or "").strip().lower()
    if record_type == "action_queued":
        return "等待登打"
    if any(word in text for word in ("failed", "error", "fail", "失敗")):
        return "失敗"
    if text == "skipped_duplicate":
        return "已存在"
    if any(word in text for word in ("submitted", "saved", "success", "completed", "ok", "已登打", "已儲存", "成功", "完成")):
        return "已登打"
    if any(word in text for word in ("queued", "pending", "running", "started", "等待", "執行")):
        return "等待登打"
    if text and not re.search(r"[A-Za-z_]", text):
        return sinposmart_status_label(value, record_type)
    return "等待登打" if record_type == "action_queued" else sinposmart_record_type_label(record_type)


def sinposmart_action_status_rank(value: str, record_type: str = "") -> int:
    label = sinposmart_action_status_label(value, record_type)
    priorities = {
        "失敗": 40,
        "已存在": 30,
        "已登打": 20,
        "等待登打": 10,
    }
    return priorities.get(label, 0)


def sinposmart_action_status_class(label: str) -> str:
    if label == "失敗":
        return "failed"
    if label in {"已登打", "已存在"}:
        return "complete"
    if label == "等待登打":
        return "running"
    return "idle"


def sinposmart_display_status_class(label: str, value: str) -> str:
    if label in {"失敗", "登入失敗"}:
        return "failed"
    if label in {"成功", "登入成功", "登出", "已登打", "已存在", "已儲存", "完成"}:
        return "complete"
    if label in {"開始", "等待中", "執行中", "等待登打"}:
        return "running"
    return sinposmart_status_class(value)


def sinposmart_action_group_key(event: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(event.get("target_time") or ""),
        str(event.get("item_kind") or ""),
        str(event.get("item_title") or ""),
        str(event.get("target") or ""),
        str(event.get("trigger_type") or ""),
    )


def sinposmart_summary_key(event: dict[str, Any]) -> tuple[str, ...]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    tool_label = sanitize_scalar(snapshot.get("tool_label"), 120) if snapshot else ""
    return (
        str(event.get("record_type") or ""),
        str(event.get("actor_no") or ""),
        sinposmart_person_label(event),
        str(event.get("item_title") or tool_label or ""),
    )


def sinposmart_tool_group_key(event: dict[str, Any]) -> tuple[str, ...]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    tool_name = sanitize_scalar(snapshot.get("tool_name"), 120) if snapshot else ""
    tool_label = sanitize_scalar(snapshot.get("tool_label"), 120) if snapshot else ""
    title = str(event.get("item_title") or "")
    return (
        str(event.get("actor_no") or ""),
        sinposmart_person_label(event),
        tool_name or tool_label or title,
    )


def sinposmart_login_key(event: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(event.get("actor_no") or ""),
        sinposmart_person_label(event),
    )


def sinposmart_event_time(event: dict[str, Any]) -> str:
    return str(event.get("last_occurred_at") or event.get("occurred_at") or "")


def newer_sinposmart_event(current: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    if current is None:
        return candidate
    if sinposmart_event_time(candidate) >= sinposmart_event_time(current):
        return candidate
    return current


def sinposmart_admin_event(event: dict[str, Any], status_label: str | None = None, person_label: str | None = None) -> dict[str, Any]:
    record_type = str(event.get("record_type") or "")
    label = status_label or sinposmart_status_label(str(event.get("status") or ""), record_type)
    record_label = "到點勤務" if record_type in {"action_queued", "action_result"} else sinposmart_record_type_label(record_type)
    return {
        "event_id": str(event.get("event_id") or ""),
        "occurred_at": str(event.get("occurred_at") or ""),
        "last_occurred_at": sinposmart_event_time(event),
        "record_type": record_type,
        "record_label": record_label,
        "trigger_label": sinposmart_trigger_label(str(event.get("trigger_type") or "")),
        "person_label": person_label or sinposmart_person_label(event),
        "status_label": label,
        "status_class": sinposmart_display_status_class(label, str(event.get("status") or "")),
        "item_kind": str(event.get("item_kind") or ""),
        "item_title": str(event.get("item_title") or sinposmart_record_type_label(record_type)),
        "content": str(event.get("content") or ""),
        "error": str(event.get("error") or ""),
        "target": str(event.get("target") or ""),
        "target_time": str(event.get("target_time") or ""),
        "repeat_count": event_repeat_count(event),
    }


def better_sinposmart_action_result(current: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    if current is None:
        return candidate
    current_rank = sinposmart_action_status_rank(str(current.get("status") or ""), str(current.get("record_type") or ""))
    candidate_rank = sinposmart_action_status_rank(str(candidate.get("status") or ""), str(candidate.get("record_type") or ""))
    if candidate_rank > current_rank:
        return candidate
    if candidate_rank == current_rank and sinposmart_event_time(candidate) >= sinposmart_event_time(current):
        return candidate
    return current


def sinposmart_admin_action_event(action_state: dict[str, dict[str, Any]]) -> dict[str, Any]:
    queued_event = action_state.get("queued")
    result_event = action_state.get("result")
    base_event = result_event or queued_event or {}
    status_label = sinposmart_action_status_label(
        str(base_event.get("status") or ""),
        str(base_event.get("record_type") or ""),
    )
    card = sinposmart_admin_event(base_event, status_label)
    started_at = sinposmart_event_time(queued_event) if queued_event else ""
    completed_at = sinposmart_event_time(result_event) if result_event else ""
    steps: list[dict[str, str]] = []
    if queued_event:
        steps.append(
            {
                "label": "開始送出",
                "occurred_at": started_at,
                "status_label": "已送出" if result_event else "等待登打",
                "status_class": "running",
            }
        )
    if result_event:
        result_label = sinposmart_action_status_label(str(result_event.get("status") or ""), "action_result")
        steps.append(
            {
                "label": "完成結果",
                "occurred_at": completed_at,
                "status_label": result_label,
                "status_class": sinposmart_display_status_class(result_label, str(result_event.get("status") or "")),
            }
        )
    card["started_at"] = started_at
    card["completed_at"] = completed_at
    card["steps"] = steps
    card["last_occurred_at"] = completed_at or started_at or card["last_occurred_at"]
    card["pause_reason"] = ""
    if result_event and status_label == "失敗":
        card["pause_reason"] = str(result_event.get("error") or result_event.get("content") or "登打失敗，請檢查公務電腦或網站回應。")
    elif queued_event and not result_event:
        card["pause_reason"] = "尚未收到完成結果，可能仍在等待登打或流程已暫停。"
    return card


def sinposmart_tool_label(event: dict[str, Any]) -> str:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    tool_label = sanitize_scalar(snapshot.get("tool_label"), 120) if snapshot else ""
    if tool_label:
        return tool_label
    title = str(event.get("item_title") or "").strip()
    for prefix in ("開始", "完成"):
        if title.startswith(prefix):
            return title[len(prefix):].strip() or title
    return title or "未標示工具"


def sinposmart_admin_tool_event(tool_state: dict[str, dict[str, Any]]) -> dict[str, Any]:
    started_event = tool_state.get("started")
    finished_event = tool_state.get("finished")
    base_event = finished_event or started_event or {}
    failed = bool(finished_event and sinposmart_status_class(str(finished_event.get("status") or "")) == "failed")
    status_label = "失敗" if failed else "完成" if finished_event else "執行中"
    card = sinposmart_admin_event(base_event, status_label)
    card["item_title"] = sinposmart_tool_label(base_event)
    started_at = sinposmart_event_time(started_event) if started_event else ""
    finished_at = sinposmart_event_time(finished_event) if finished_event else ""
    steps: list[dict[str, str]] = []
    if started_event:
        steps.append(
            {
                "label": "開始執行",
                "occurred_at": started_at,
                "status_label": "已開始" if finished_event else "執行中",
                "status_class": "running",
            }
        )
    if finished_event:
        steps.append(
            {
                "label": "結束執行",
                "occurred_at": finished_at,
                "status_label": "失敗" if failed else "完成",
                "status_class": "failed" if failed else "complete",
            }
        )
    card["started_at"] = started_at
    card["finished_at"] = finished_at
    card["steps"] = steps
    card["last_occurred_at"] = finished_at or started_at or card["last_occurred_at"]
    card["result_text"] = str(base_event.get("error") or base_event.get("content") or "")
    card["pause_reason"] = ""
    if failed:
        card["pause_reason"] = card["result_text"] or "工具執行失敗，請檢查公務電腦或網站回應。"
    elif started_event and not finished_event:
        card["pause_reason"] = "尚未收到工具結束結果，可能仍在執行或流程已暫停。"
    return card


def sinposmart_login_status_label(event: dict[str, Any]) -> str:
    record_type = str(event.get("record_type") or "")
    if record_type == "login_failed":
        return "登入失敗"
    if record_type == "logout":
        return "登出"
    if sinposmart_status_class(str(event.get("status") or "")) == "failed":
        return "登入失敗"
    return "登入成功"


def build_sinposmart_admin_view(events: list[dict[str, Any]]) -> dict[str, Any]:
    action_groups: dict[tuple[str, ...], dict[str, dict[str, Any]]] = {}
    tool_events: dict[tuple[str, ...], dict[str, dict[str, Any]]] = {}
    background_updates: dict[tuple[str, ...], dict[str, Any]] = {}
    login_events: dict[tuple[str, ...], dict[str, Any]] = {}
    compacted_events = compact_sinposmart_events(events)
    preferred_people = build_sinposmart_preferred_person_labels(compacted_events)

    for event in compacted_events:
        if not isinstance(event, dict):
            continue
        record_type = str(event.get("record_type") or "")
        if record_type in {"action_queued", "action_result"}:
            key = sinposmart_action_group_key(event)
            action_state = action_groups.setdefault(key, {})
            if record_type == "action_queued":
                action_state["queued"] = newer_sinposmart_event(action_state.get("queued"), event)
            else:
                action_state["result"] = better_sinposmart_action_result(action_state.get("result"), event)
            continue
        if record_type in {"tool_action_started", "tool_action_finished"}:
            key = sinposmart_tool_group_key(event)
            tool_state = tool_events.setdefault(key, {})
            if record_type == "tool_action_started":
                tool_state["started"] = newer_sinposmart_event(tool_state.get("started"), event)
            else:
                tool_state["finished"] = newer_sinposmart_event(tool_state.get("finished"), event)
            continue
        if record_type in {"schedule_snapshot", "comparison_snapshot"}:
            key = sinposmart_summary_key(event)
            background_updates[key] = newer_sinposmart_event(background_updates.get(key), event)
            continue
        if record_type in {"login", "login_failed", "logout"}:
            key = sinposmart_login_key(event)
            login_events[key] = newer_sinposmart_event(login_events.get(key), event)

    action_events = [sinposmart_admin_action_event(action_state) for action_state in action_groups.values()]
    action_events.sort(key=lambda item: str(item.get("last_occurred_at") or ""), reverse=True)

    tool_update_events = [sinposmart_admin_tool_event(tool_state) for tool_state in tool_events.values()]
    tool_update_events.sort(key=lambda item: str(item.get("last_occurred_at") or ""), reverse=True)

    background_update_events = [sinposmart_admin_event(event) for event in background_updates.values()]
    background_update_events.sort(key=lambda item: str(item.get("last_occurred_at") or ""), reverse=True)

    login_update_events = []
    for event in login_events.values():
        label = sinposmart_login_status_label(event)
        actor_no = str(event.get("actor_no") or "").strip()
        current_person = sinposmart_person_label(event)
        preferred_person = preferred_people.get(actor_no, "")
        if sinposmart_person_label_score(preferred_person) > sinposmart_person_label_score(current_person):
            current_person = preferred_person
        login_update_events.append(sinposmart_admin_event(event, label, current_person))
    login_update_events.sort(key=lambda item: str(item.get("last_occurred_at") or ""), reverse=True)

    summary = {
        "actions": len(action_events),
        "submitted": sum(1 for event in action_events if event["status_label"] == "已登打"),
        "existing": sum(1 for event in action_events if event["status_label"] == "已存在"),
        "failed": sum(1 for event in action_events if event["status_label"] == "失敗"),
        "waiting": sum(1 for event in action_events if event["status_label"] == "等待登打"),
        "tools": len(tool_update_events),
        "background_updates": len(background_update_events),
        "logins": len(login_update_events),
    }
    return {
        "summary": summary,
        "action_events": action_events,
        "tool_events": tool_update_events,
        "background_updates": background_update_events,
        "login_events": login_update_events,
    }


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
        payload.pop("admin_view", None)
        events = compact_sinposmart_events(list(payload.get("events") or []))
        known_ids = {str(item.get("event_id") or "") for item in events if isinstance(item, dict)}
        if event["event_id"] in known_ids:
            events = [merge_sinposmart_event(item, event) if str(item.get("event_id") or "") == event["event_id"] else item for item in events]
        else:
            duplicate_index = next((index for index, item in enumerate(events) if sinposmart_event_merge_key(item) == sinposmart_event_merge_key(event)), None)
            if duplicate_index is None:
                events.append(event)
            else:
                events[duplicate_index] = merge_sinposmart_event(events[duplicate_index], event)
        payload["fire_day"] = event["fire_day"]
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        payload["events"] = sorted(events, key=lambda item: str(item.get("occurred_at") or ""))
        payload["summary"] = summarize_sinposmart_events(payload["events"])
        payload.pop("admin_view", None)
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
                payload["events"] = compact_sinposmart_events(payload.get("events") or [])
                payload["summary"] = summarize_sinposmart_events(payload.get("events") or [])
                payload["admin_view"] = build_sinposmart_admin_view(payload.get("events") or [])
                days.append(payload)
        return days[:limit]

    def read_day(self, fire_day: str) -> dict[str, Any]:
        path = self.path_for_day(fire_day)
        if not path.exists():
            return {"fire_day": fire_day, "updated_at": "", "summary": {}, "events": [], "admin_view": build_sinposmart_admin_view([])}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"fire_day": fire_day, "updated_at": "", "summary": {}, "events": [], "admin_view": build_sinposmart_admin_view([])}
        if not isinstance(payload, dict):
            return {"fire_day": fire_day, "updated_at": "", "summary": {}, "events": [], "admin_view": build_sinposmart_admin_view([])}
        payload["events"] = compact_sinposmart_events(payload.get("events") or [])
        payload["summary"] = summarize_sinposmart_events(payload.get("events") or [])
        payload["admin_view"] = build_sinposmart_admin_view(payload.get("events") or [])
        return payload

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
        "repeat_count": 1,
        "first_occurred_at": occurred_at.isoformat(timespec="seconds"),
        "last_occurred_at": occurred_at.isoformat(timespec="seconds"),
        "snapshot": sanitize_value(raw_event.get("snapshot"), depth=0),
    }
    if record_type == "tool_action_started" and not event["item_title"]:
        snapshot = event["snapshot"] if isinstance(event.get("snapshot"), dict) else {}
        tool_label = sanitize_scalar(snapshot.get("tool_label"), 120)
        if tool_label:
            event["item_title"] = f"開始{tool_label}"
    return {field: event[field] for field in SINPOSMART_EVENT_FIELDS}


def sinposmart_event_merge_key(event: dict[str, Any]) -> tuple[str, ...]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    tool_label = sanitize_scalar(snapshot.get("tool_label"), 120) if snapshot else ""
    return (
        str(event.get("record_type") or ""),
        str(event.get("actor_no") or ""),
        str(event.get("display_name") or ""),
        str(event.get("trigger_type") or ""),
        str(event.get("status") or ""),
        str(event.get("item_kind") or ""),
        str(event.get("item_title") or ""),
        str(event.get("content") or ""),
        str(event.get("error") or ""),
        str(event.get("source") or ""),
        str(event.get("target") or ""),
        str(event.get("target_time") or ""),
        tool_label,
    )


def event_repeat_count(event: dict[str, Any]) -> int:
    try:
        return max(1, int(event.get("repeat_count") or 1))
    except (TypeError, ValueError):
        return 1


def merge_sinposmart_event(existing: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged["repeat_count"] = event_repeat_count(existing) + event_repeat_count(event)
    merged["first_occurred_at"] = str(existing.get("first_occurred_at") or existing.get("occurred_at") or event.get("occurred_at") or "")
    merged["last_occurred_at"] = str(event.get("occurred_at") or existing.get("last_occurred_at") or existing.get("occurred_at") or "")
    merged["occurred_at"] = merged["last_occurred_at"] or str(existing.get("occurred_at") or "")
    for key in ("result_ref", "snapshot"):
        if event.get(key):
            merged[key] = event[key]
    return merged


def compact_sinposmart_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    index_by_key: dict[tuple[str, ...], int] = {}
    known_ids: dict[str, int] = {}
    for raw_event in events:
        if not isinstance(raw_event, dict):
            continue
        event = dict(raw_event)
        event.setdefault("repeat_count", 1)
        event.setdefault("first_occurred_at", event.get("occurred_at") or "")
        event.setdefault("last_occurred_at", event.get("occurred_at") or "")
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in known_ids:
            compacted[known_ids[event_id]] = merge_sinposmart_event(compacted[known_ids[event_id]], event)
            continue
        merge_key = sinposmart_event_merge_key(event)
        if merge_key in index_by_key:
            compacted[index_by_key[merge_key]] = merge_sinposmart_event(compacted[index_by_key[merge_key]], event)
            if event_id:
                known_ids[event_id] = index_by_key[merge_key]
            continue
        index_by_key[merge_key] = len(compacted)
        if event_id:
            known_ids[event_id] = len(compacted)
        compacted.append(event)
    return sorted(compacted, key=lambda item: str(item.get("occurred_at") or ""))


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
        "tool_starts": 0,
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
        if record_type == "tool_action_started":
            summary["tool_starts"] += 1
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
