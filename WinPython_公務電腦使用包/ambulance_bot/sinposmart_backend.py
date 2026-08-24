# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


SINPOSMART_RECORD_TYPES = {
    "login",
    "login_failed",
    "login_expired",
    "logout",
    "action_queued",
    "action_result",
    "tool_action_started",
    "tool_action_finished",
    "schedule_snapshot",
    "comparison_snapshot",
    "unreturned_return",
    "error",
}
SINPOSMART_LOGIN_RECORD_TYPES = {"login", "login_failed", "login_expired", "logout"}
SINPOSMART_EVENT_FIELDS = (
    "event_id",
    "merged_event_ids",
    "occurred_at",
    "received_at",
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
TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
SINPOSMART_RESULT_ADVISORY_SECONDS = 15 * 60
_STORE_LOCKS_GUARD = Lock()
_STORE_LOCKS: dict[str, RLock] = {}


def shared_store_lock(root_dir: Path) -> RLock:
    key = str(root_dir.resolve()).casefold()
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _STORE_LOCKS[key] = lock
        return lock


def taipei_local_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(TAIPEI_TIMEZONE).replace(tzinfo=None)


def sinposmart_fire_day_for(value: datetime | None = None) -> str:
    value = taipei_local_datetime(value or datetime.now())
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
        "login_expired": "登入失效",
        "logout": "登出",
        "action_queued": "加入佇列",
        "action_result": "登打結果",
        "tool_action_started": "工具開始",
        "tool_action_finished": "工具結果",
        "schedule_snapshot": "整日勤務",
        "comparison_snapshot": "已登打資料",
        "unreturned_return": "未返隊暫停",
        "error": "錯誤",
    }
    return labels.get(str(value or ""), "事件")


def sinposmart_trigger_label(value: str) -> str:
    labels = {
        "manual": "手動",
        "due": "自動",
        "recovery": "返隊確認",
        "login": "登入",
        "schedule": "勤務快照",
        "comparison": "比對快照",
        "tool_start": "工具開始",
        "system": "系統",
        "update": "更新",
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


def sinposmart_action_target_label(value: Any) -> str:
    target = " ".join(str(value or "").split())
    if not target:
        return ""
    target = re.sub(r"\s*[（(][^（）()]*[）)]\s*$", "", target).strip()
    number_match = re.fullmatch(r"(\d{1,2})(?:番)?", target)
    if number_match:
        return f"{number_match.group(1)}番"
    person_match = re.fullmatch(r"(\d{1,2})番\s*(.+)", target)
    if person_match:
        return f"{person_match.group(1)}番 {person_match.group(2)}"
    person_match = re.fullmatch(r"(\d{1,2})\s+((?!號(?:\s|$)).+)", target)
    if person_match:
        return f"{person_match.group(1)}番 {person_match.group(2)}"
    return target


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


def sinposmart_unreturned_return_status_label(value: str) -> str:
    labels = {
        "pending": "暫停待確認",
        "retrying": "重新確認中",
        "resolved": "已確認返隊",
        "expired": "逾 18 小時移除",
    }
    return labels.get(str(value or "").strip().lower(), "未返隊暫停")


def sinposmart_unreturned_return_status_class(value: str) -> str:
    status = str(value or "").strip().lower()
    if status == "resolved":
        return "complete"
    if status == "expired":
        return "failed"
    return "running"


def sinposmart_unreturned_return_trigger_label(event: dict[str, Any]) -> str:
    trigger_type = str(event.get("trigger_type") or "").strip().lower()
    if trigger_type == "manual":
        return "手動確認返隊"
    if trigger_type == "recovery":
        return "自動返隊確認"
    if trigger_type == "due":
        return "自動偵測未返隊"
    return sinposmart_trigger_label(trigger_type)


def sinposmart_action_status_class(label: str) -> str:
    if label == "失敗":
        return "failed"
    if label in {"已登打", "已存在"}:
        return "complete"
    if label == "等待逾時待確認":
        return "attention"
    if label in {"等待登打", "等待完成回傳", "執行中"}:
        return "running"
    return "idle"


def sinposmart_display_status_class(label: str, value: str) -> str:
    if label in {"失敗", "登入失敗", "登入失效"}:
        return "failed"
    if label in {"成功", "登入成功", "登出", "已登打", "已存在", "已儲存", "完成"}:
        return "complete"
    if label == "等待逾時待確認":
        return "attention"
    if label in {"開始", "等待中", "執行中", "等待登打", "等待完成回傳"}:
        return "running"
    return sinposmart_status_class(value)


def sinposmart_action_group_key(event: dict[str, Any], *, include_target: bool = True) -> tuple[str, ...]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    completion_key = sanitize_scalar(snapshot.get("completion_key"), 160) if snapshot else ""
    if completion_key:
        return ("completion_key", completion_key)
    fields = [
        "fields",
        str(event.get("target_time") or ""),
        str(event.get("item_kind") or ""),
        str(event.get("item_title") or ""),
    ]
    if include_target:
        fields.append(str(event.get("target") or ""))
    fields.append(str(event.get("trigger_type") or ""))
    return tuple(fields)


def sinposmart_summary_key(event: dict[str, Any]) -> tuple[str, ...]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    tool_label = sanitize_scalar(snapshot.get("tool_label"), 120) if snapshot else ""
    return (
        str(event.get("record_type") or ""),
        str(event.get("actor_no") or ""),
        sinposmart_person_label(event),
        str(event.get("item_title") or tool_label or ""),
    )


def parse_sinposmart_snapshot_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    if text.isdigit() and len(text) == 7:
        try:
            return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7]))
        except ValueError:
            return None
    return None


def sinposmart_snapshot_target_dates(event: dict[str, Any]) -> list[date | None]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    days = snapshot.get("days") if isinstance(snapshot, dict) else []
    if not isinstance(days, list):
        return []
    target_dates: list[date | None] = []
    seen: set[str] = set()
    for day_item in days:
        if not isinstance(day_item, dict):
            continue
        raw_date = str(day_item.get("target_date") or "").strip()
        if raw_date in seen:
            continue
        seen.add(raw_date)
        target_dates.append(parse_sinposmart_snapshot_date(raw_date))
    return target_dates


def sinposmart_schedule_snapshot_scope(event: dict[str, Any], target_date: date | None) -> str:
    fire_day = parse_sinposmart_snapshot_date(event.get("fire_day"))
    if fire_day and target_date:
        if target_date == fire_day:
            return "當日整日勤務"
        if target_date == fire_day + timedelta(days=1):
            return "隔日整日勤務"
        if target_date == fire_day - timedelta(days=1):
            return "前日整日勤務"
    return "整日勤務"


def sinposmart_background_summary_events(event: dict[str, Any]) -> list[dict[str, Any]]:
    if str(event.get("record_type") or "") != "schedule_snapshot":
        return [event]
    target_dates = sinposmart_snapshot_target_dates(event)
    if not target_dates:
        return [event]
    summary_events: list[dict[str, Any]] = []
    for target_date in target_dates:
        scoped_event = dict(event)
        scoped_event["item_title"] = sinposmart_schedule_snapshot_scope(event, target_date)
        summary_events.append(scoped_event)
    return summary_events


def sinposmart_tool_group_key(event: dict[str, Any]) -> tuple[str, ...]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    tool_name = sanitize_scalar(snapshot.get("tool_name"), 120) if snapshot else ""
    tool_label = sanitize_scalar(snapshot.get("tool_label"), 120) if snapshot else ""
    run_id = sanitize_scalar(snapshot.get("run_id"), 120) if snapshot else ""
    title = str(event.get("item_title") or "")
    return (
        str(event.get("actor_no") or ""),
        sinposmart_person_label(event),
        tool_name or tool_label or title,
        run_id,
    )


def sinposmart_login_key(event: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(event.get("actor_no") or ""),
        sinposmart_person_label(event),
    )


def sinposmart_event_time(event: dict[str, Any]) -> str:
    return str(event.get("last_occurred_at") or event.get("occurred_at") or "")


def sinposmart_pending_age_seconds(event: dict[str, Any], now: datetime | None = None) -> int | None:
    received_at = str(event.get("received_at") or "").strip()
    if not received_at:
        return None
    current = taipei_local_datetime(now or datetime.now())
    received = parse_event_datetime(received_at, current)
    return max(0, int((current - received).total_seconds()))


def sinposmart_elapsed_label(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, remainder = divmod(seconds, 60)
    if remainder:
        return f"{minutes} 分 {remainder} 秒"
    return f"{minutes} 分鐘"


def sinposmart_rescue_video_summary(event: dict[str, Any]) -> dict[str, Any]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    if sanitize_scalar(snapshot.get("tool_name"), 120) != "rescue_video":
        return {}
    target_date = sanitize_scalar(snapshot.get("target_date"), 40)
    vehicle = sanitize_scalar(snapshot.get("vehicle"), 40)
    try:
        case_count = max(0, int(snapshot.get("case_count")))
        total_count = max(0, int(snapshot.get("total_count")))
        usage_seconds = max(0, int(snapshot.get("usage_seconds")))
    except (TypeError, ValueError):
        return {}
    if not target_date or not vehicle:
        return {}
    return {
        "target_date": target_date,
        "vehicle": vehicle,
        "case_count": case_count,
        "total_count": total_count,
        "usage_time": sinposmart_elapsed_label(usage_seconds),
    }


def sinposmart_waiting_state(
    event: dict[str, Any],
    *,
    now: datetime | None = None,
    status_label: str,
    start_label: str,
    completion_label: str,
    failure_label: str,
) -> dict[str, Any]:
    age_seconds = sinposmart_pending_age_seconds(event, now)
    overdue = age_seconds is not None and age_seconds >= SINPOSMART_RESULT_ADVISORY_SECONDS
    if overdue:
        return {
            "status_label": "等待逾時待確認",
            "status_class": "attention",
            "waiting_age_seconds": age_seconds,
            "waiting_overdue": True,
            "waiting_reason": (
                f"已等待 {sinposmart_elapsed_label(age_seconds)}仍未收到{completion_label}，"
                f"請確認公務電腦；尚不代表{failure_label}。"
            ),
        }
    waiting_prefix = f"{start_label}，"
    if age_seconds is not None:
        waiting_prefix += f"已等待 {sinposmart_elapsed_label(age_seconds)}，"
    return {
        "status_label": status_label,
        "status_class": "running",
        "waiting_age_seconds": age_seconds or 0,
        "waiting_overdue": False,
        "waiting_reason": f"{waiting_prefix}等待{completion_label}回傳。",
    }


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


def sinposmart_admin_unreturned_return_event(event: dict[str, Any]) -> dict[str, Any]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    card = sinposmart_admin_event(
        event,
        sinposmart_unreturned_return_status_label(str(event.get("status") or "")),
    )
    card["record_label"] = "因人員未返隊暫停"
    card["status_class"] = sinposmart_unreturned_return_status_class(str(event.get("status") or ""))
    card["trigger_label"] = sinposmart_unreturned_return_trigger_label(event)
    card["item_title"] = sinposmart_action_display_title(event, target=event.get("target"))
    card["first_paused_at"] = sanitize_scalar(snapshot.get("first_paused_at"), 80)
    card["last_attempt_at"] = sanitize_scalar(snapshot.get("last_attempt_at"), 80)
    card["next_retry_at"] = sanitize_scalar(snapshot.get("next_retry_at"), 80)
    card["expires_at"] = sanitize_scalar(snapshot.get("expires_at"), 80)
    card["owner_actor_no"] = sanitize_scalar(snapshot.get("last_owner_actor_no"), 40)
    try:
        card["retry_interval_minutes"] = max(0, int(snapshot.get("retry_interval_minutes") or 0))
    except (TypeError, ValueError):
        card["retry_interval_minutes"] = 0
    return card


def sinposmart_admin_unreturned_return_history_event(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered_events = sorted(events, key=sinposmart_event_time)
    latest_event = ordered_events[-1]
    card = sinposmart_admin_unreturned_return_event(latest_event)
    task_fields = ("target_time", "item_kind", "item_title", "target")
    task_event = dict(latest_event)
    for field in task_fields:
        task_event[field] = next(
            (
                event.get(field)
                for event in ordered_events
                if str(event.get(field) or "").strip()
            ),
            "",
        )
    has_any_task_details = any(
        str(task_event.get(field) or "").strip()
        for field in task_fields
    )
    has_complete_task_details = all(
        str(task_event.get(field) or "").strip()
        for field in task_fields
    )
    first_paused_at = next(
        (
            sanitize_scalar(event["snapshot"].get("first_paused_at"), 80)
            for event in ordered_events
            if isinstance(event.get("snapshot"), dict)
            and sanitize_scalar(event["snapshot"].get("first_paused_at"), 80)
        ),
        "",
    )
    if first_paused_at:
        card["first_paused_at"] = first_paused_at
    if has_any_task_details:
        card["item_title"] = sinposmart_action_display_title(
            task_event,
            target=task_event.get("target"),
        )
    else:
        paused_at = str(card.get("first_paused_at") or latest_event.get("occurred_at") or "")
        time_match = re.search(r"(?:^|[T\s])(\d{2}:\d{2})(?::\d{2})?", paused_at)
        paused_time = time_match.group(1) if time_match else ""
        card["item_title"] = "｜".join(
            part for part in (paused_time, "未返隊", "舊版事件未提供勤務明細") if part
        )
    handoff_snapshots = [
        snapshot.get("handoff")
        for event in ordered_events
        if isinstance(event.get("snapshot"), dict)
        for snapshot in [event["snapshot"]]
        if isinstance(snapshot.get("handoff"), dict)
    ]
    latest_handoff = handoff_snapshots[-1] if handoff_snapshots else {}
    bridge_history = latest_handoff.get("bridge_history", []) if isinstance(latest_handoff, dict) else []

    def people(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(person or "").strip() for person in value if str(person or "").strip()]
        return [person.strip() for person in str(value or "").split("、") if person.strip()]

    skipped_people = people(latest_handoff.get("skipped_scheduled_people")) + [
        str(person or "").strip()
        for bridge in bridge_history
        if isinstance(bridge, dict)
        for person in bridge.get("skipped_scheduled_people", [])
        if str(person or "").strip()
    ]
    actual_people = people(latest_handoff.get("actual_incoming_people")) + [
        str(person or "").strip()
        for bridge in bridge_history
        if isinstance(bridge, dict)
        for person in bridge.get("actual_incoming_people", [])
        if str(person or "").strip()
    ]
    def first_handoff_value(field: str, limit: int) -> str:
        return next(
            (
                sanitize_scalar(snapshot.get(field), limit)
                for snapshot in handoff_snapshots
                if sanitize_scalar(snapshot.get(field), limit)
            ),
            "",
        )

    card["original_handoff_time"] = first_handoff_value("original_handoff_time", 80)
    card["outgoing_person"] = first_handoff_value("outgoing_person", 120)
    card["scheduled_incoming_person"] = first_handoff_value(
        "scheduled_incoming_person",
        120,
    )
    card["actual_incoming_person"] = sanitize_scalar(
        actual_people[-1] if actual_people else latest_handoff.get("actual_incoming_person"),
        120,
    )
    card["skipped_scheduled_people"] = list(dict.fromkeys(skipped_people))
    card["has_handoff_context"] = any(
        (
            card["original_handoff_time"],
            card["outgoing_person"],
            card["scheduled_incoming_person"],
            card["actual_incoming_person"],
            card["skipped_scheduled_people"],
        )
    )
    card["missing_task_details"] = not has_complete_task_details
    card["is_active"] = str(latest_event.get("status") or "").strip().lower() in {
        "pending",
        "retrying",
    }
    card["transitions"] = [
        {
            "occurred_at": sinposmart_event_time(event),
            "status_label": sinposmart_unreturned_return_status_label(
                str(event.get("status") or "")
            ),
            "status_class": sinposmart_unreturned_return_status_class(
                str(event.get("status") or "")
            ),
            "trigger_label": sinposmart_unreturned_return_trigger_label(event),
            "owner_actor_no": sanitize_scalar(
                (event.get("snapshot") or {}).get("last_owner_actor_no"), 40
            )
            if isinstance(event.get("snapshot"), dict)
            else "",
        }
        for event in ordered_events
    ]
    card["last_result_at"] = (
        sinposmart_event_time(latest_event)
        if str(latest_event.get("status") or "").strip().lower() in {"resolved", "expired"}
        else ""
    )
    return card


def sinposmart_action_display_title(
    event: dict[str, Any],
    *,
    target: Any = None,
) -> str:
    item_title = str(event.get("item_title") or "").strip()
    if not item_title:
        item_title = sinposmart_record_type_label(str(event.get("record_type") or ""))
    parts = [str(event.get("target_time") or "").strip(), str(event.get("item_kind") or "").strip(), item_title]
    if target is not None:
        raw_target = " ".join(str(target or "").split())
        target_label = sinposmart_action_target_label(target)
        if target_label:
            item_parts = item_title.split("｜")
            if item_parts[-1].strip() in {raw_target, target_label}:
                item_parts[-1] = target_label
            else:
                item_parts.append(target_label)
            parts[-1] = "｜".join(item_parts)
    return "｜".join(part for part in parts if part)


def better_sinposmart_action_result(current: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    if current is None:
        return candidate
    current_time = sinposmart_event_time(current)
    candidate_time = sinposmart_event_time(candidate)
    if candidate_time > current_time:
        return candidate
    if candidate_time < current_time:
        return current
    current_rank = sinposmart_action_status_rank(str(current.get("status") or ""), str(current.get("record_type") or ""))
    candidate_rank = sinposmart_action_status_rank(str(candidate.get("status") or ""), str(candidate.get("record_type") or ""))
    if candidate_rank > current_rank:
        return candidate
    if candidate_rank == current_rank and sinposmart_event_time(candidate) >= sinposmart_event_time(current):
        return candidate
    return current


def merge_sinposmart_targetless_action_groups(
    action_groups: dict[tuple[str, ...], dict[str, dict[str, Any]]],
) -> None:
    for key, action_state in list(action_groups.items()):
        if len(action_state) != 1:
            continue
        action_key, event = next(iter(action_state.items()))
        if action_key not in {"queued", "result"}:
            continue
        if str(event.get("target") or "").strip():
            continue
        counterpart_key = "result" if action_key == "queued" else "queued"
        targetless_key = sinposmart_action_group_key(event, include_target=False)
        candidates = [
            candidate_state
            for candidate_key, candidate_state in action_groups.items()
            if candidate_key != key
            and set(candidate_state) == {counterpart_key}
            and str(candidate_state[counterpart_key].get("target") or "").strip()
            and sinposmart_action_group_key(candidate_state[counterpart_key], include_target=False) == targetless_key
        ]
        if len(candidates) != 1:
            continue
        candidate_state = candidates[0]
        if action_key == "queued":
            candidate_state["queued"] = newer_sinposmart_event(candidate_state.get("queued"), event)
        else:
            candidate_state["result"] = better_sinposmart_action_result(candidate_state.get("result"), event)
        action_groups.pop(key, None)


def sinposmart_admin_action_event(
    action_state: dict[str, dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    queued_event = action_state.get("queued")
    result_event = action_state.get("result")
    base_event = result_event or queued_event or {}
    waiting_state: dict[str, Any] | None = None
    if queued_event and not result_event:
        waiting_state = sinposmart_waiting_state(
            queued_event,
            now=now,
            status_label="等待完成回傳",
            start_label="已收到開始送出",
            completion_label="勤務系統完成結果",
            failure_label="登打失敗",
        )
        status_label = str(waiting_state["status_label"])
    else:
        status_label = sinposmart_action_status_label(
            str(base_event.get("status") or ""),
            str(base_event.get("record_type") or ""),
        )
    card = sinposmart_admin_event(base_event, status_label)
    if waiting_state:
        card["status_class"] = str(waiting_state["status_class"])
    target = base_event.get("target")
    if not str(target or "").strip() and queued_event:
        target = queued_event.get("target")
    card["item_title"] = sinposmart_action_display_title(base_event, target=target)
    started_at = sinposmart_event_time(queued_event) if queued_event else ""
    completed_at = sinposmart_event_time(result_event) if result_event else ""
    steps: list[dict[str, str]] = []
    if queued_event:
        steps.append(
            {
                "label": "開始送出",
                "occurred_at": started_at,
                "status_label": "已送出" if result_event else status_label,
                "status_class": "running" if result_event else card["status_class"],
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
    card["waiting_age_seconds"] = 0
    card["waiting_overdue"] = False
    card["waiting_reason"] = ""
    card["pause_reason"] = ""
    if result_event and status_label == "失敗":
        card["pause_reason"] = str(result_event.get("error") or result_event.get("content") or "登打失敗，請檢查公務電腦或網站回應。")
    elif waiting_state:
        card.update(waiting_state)
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


def sinposmart_tool_failure_stage_label(event: dict[str, Any]) -> str:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    stage = re.sub(r"[^a-z0-9_]", "", str(snapshot.get("failure_stage") or "").lower())
    labels = {
        "preflight": "前置檢查",
        "source_load": "資料來源讀取",
        "config_load": "工具設定讀取",
        "browser_start": "瀏覽器啟動",
        "login": "網站登入",
        "duty_form_open": "開啟勤務表頁面",
        "duty_fill": "填寫勤務表",
        "duty_save": "儲存勤務表",
        "vehicle_form_open": "開啟車輛勤務頁面",
        "vehicle_fill": "填寫車輛勤務",
        "form_open": "開啟登打頁面",
        "fill": "填寫資料",
        "save": "儲存資料",
        "process_start": "啟動車輛清點程序",
        "process_running": "執行車輛清點程序",
        "result_evaluation": "檢查車輛清點結果",
        "maintenance_check": "車輛保養檢查",
        "equipment_check": "車輛器材檢查",
        "artifact_write": "儲存診斷資料",
        "module_load": "載入行車紀錄器工具",
        "offset_detection": "偵測影片時間偏移",
        "classification": "分類行車紀錄器影片",
        "report_write": "寫入分類報表",
    }
    return labels.get(stage, "")


def sinposmart_tool_failure_detail(event: dict[str, Any]) -> str:
    """Return a short, non-sensitive detail for a failed public-PC tool."""

    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    detail = sanitize_scalar(snapshot.get("failure_detail"), 600)
    details = {
        "browser_startup": "專用瀏覽器啟動或連線未完成；已清理過期暫存設定檔並重試一次。",
    }
    return details.get(detail, detail)


def sinposmart_admin_tool_event(
    tool_state: dict[str, dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    started_event = tool_state.get("started")
    finished_event = tool_state.get("finished")
    base_event = finished_event or started_event or {}
    failed = bool(finished_event and sinposmart_status_class(str(finished_event.get("status") or "")) == "failed")
    waiting_state: dict[str, Any] | None = None
    if started_event and not finished_event:
        waiting_state = sinposmart_waiting_state(
            started_event,
            now=now,
            status_label="執行中",
            start_label="已收到開始執行",
            completion_label="工具完成結果",
            failure_label="工具失敗",
        )
        status_label = str(waiting_state["status_label"])
    else:
        status_label = "失敗" if failed else "完成" if finished_event else "執行中"
    card = sinposmart_admin_event(base_event, status_label)
    if waiting_state:
        card["status_class"] = str(waiting_state["status_class"])
    card["item_title"] = sinposmart_tool_label(base_event)
    card["failure_stage_label"] = sinposmart_tool_failure_stage_label(finished_event or {}) if failed else ""
    card["failure_detail"] = sinposmart_tool_failure_detail(finished_event or {}) if failed else ""
    started_at = sinposmart_event_time(started_event) if started_event else ""
    finished_at = sinposmart_event_time(finished_event) if finished_event else ""
    steps: list[dict[str, str]] = []
    if started_event:
        steps.append(
            {
                "label": "開始執行",
                "occurred_at": started_at,
                "status_label": "已開始" if finished_event else status_label,
                "status_class": "running" if finished_event else card["status_class"],
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
    card["rescue_video_summary"] = sinposmart_rescue_video_summary(base_event)
    card["waiting_age_seconds"] = 0
    card["waiting_overdue"] = False
    card["waiting_reason"] = ""
    card["pause_reason"] = ""
    if failed:
        card["pause_reason"] = card["result_text"] or "工具執行失敗，請檢查公務電腦或網站回應。"
    elif waiting_state:
        card.update(waiting_state)
    return card


def sinposmart_login_status_label(event: dict[str, Any]) -> str:
    record_type = str(event.get("record_type") or "")
    if record_type == "login_failed":
        return "登入失敗"
    if record_type == "login_expired":
        return "登入失效"
    if record_type == "logout":
        return "登出"
    if record_type == "error" and str(event.get("trigger_type") or "") == "login":
        details = f"{event.get('error') or ''} {event.get('content') or ''}"
        if "失效" in details or "expired" in details.lower():
            return "登入失效"
        return "登入失敗"
    if sinposmart_status_class(str(event.get("status") or "")) == "failed":
        return "登入失敗"
    return "登入成功"


def sinposmart_is_login_event(event: dict[str, Any]) -> bool:
    record_type = str(event.get("record_type") or "")
    if record_type in SINPOSMART_LOGIN_RECORD_TYPES:
        return True
    return record_type == "error" and str(event.get("trigger_type") or "") == "login"


def sinposmart_admin_login_event(event: dict[str, Any], preferred_people: dict[str, str]) -> dict[str, Any]:
    record_type = str(event.get("record_type") or "")
    label = sinposmart_login_status_label(event)
    actor_no = str(event.get("actor_no") or "").strip()
    current_person = sinposmart_person_label(event)
    preferred_person = preferred_people.get(actor_no, "")
    if sinposmart_person_label_score(preferred_person) > sinposmart_person_label_score(current_person):
        current_person = preferred_person
    card = sinposmart_admin_event(event, label, current_person)
    if record_type == "error":
        card["record_label"] = label
    occurred_at = sinposmart_event_time(event)
    login_at = occurred_at if record_type in {"login", "login_failed", "login_expired"} else ""
    logout_at = occurred_at if record_type == "logout" else ""
    step_label = {
        "login": "登入時間",
        "login_failed": "登入失敗時間",
        "login_expired": "登入失效時間",
        "logout": "登出時間",
    }.get(record_type)
    if not step_label:
        step_label = "登入失效時間" if label == "登入失效" else "登入失敗時間" if label == "登入失敗" else "事件時間"
    steps: list[dict[str, str]] = []
    if occurred_at:
        steps.append(
            {
                "label": step_label,
                "occurred_at": occurred_at,
                "status_label": label,
                "status_class": sinposmart_display_status_class(label, str(event.get("status") or "")),
            }
        )
    card["login_at"] = login_at
    card["logout_at"] = logout_at
    card["steps"] = steps
    card["last_occurred_at"] = occurred_at
    return card


def build_sinposmart_admin_view(
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    action_groups: dict[tuple[str, ...], dict[str, dict[str, Any]]] = {}
    tool_events: dict[tuple[str, ...], dict[str, dict[str, Any]]] = {}
    background_updates: dict[tuple[str, ...], dict[str, Any]] = {}
    unreturned_return_events: dict[str, dict[str, Any]] = {}
    unreturned_return_history_events: dict[str, list[dict[str, Any]]] = {}
    login_events: list[dict[str, Any]] = []
    compacted_events = compact_sinposmart_events(events)
    preferred_people = build_sinposmart_preferred_person_labels(compacted_events)

    for event in compacted_events:
        if not isinstance(event, dict):
            continue
        record_type = str(event.get("record_type") or "")
        if record_type == "unreturned_return":
            snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
            queue_id = sanitize_scalar(snapshot.get("queue_id"), 120) or str(event.get("event_id") or "")
            unreturned_return_events[queue_id] = newer_sinposmart_event(
                unreturned_return_events.get(queue_id),
                event,
            )
            unreturned_return_history_events.setdefault(queue_id, []).append(event)
            continue
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
            for summary_event in sinposmart_background_summary_events(event):
                key = sinposmart_summary_key(summary_event)
                background_updates[key] = newer_sinposmart_event(background_updates.get(key), summary_event)
            continue
        if record_type == "error":
            if sinposmart_is_login_event(event):
                login_events.append(event)
            else:
                key = sinposmart_summary_key(event)
                background_updates[key] = newer_sinposmart_event(background_updates.get(key), event)
            continue
        if sinposmart_is_login_event(event):
            login_events.append(event)

    merge_sinposmart_targetless_action_groups(action_groups)
    action_events = [sinposmart_admin_action_event(action_state, now=now) for action_state in action_groups.values()]
    action_events.sort(key=lambda item: str(item.get("last_occurred_at") or ""), reverse=True)

    tool_update_events = [sinposmart_admin_tool_event(tool_state, now=now) for tool_state in tool_events.values()]
    tool_update_events.sort(key=lambda item: str(item.get("last_occurred_at") or ""), reverse=True)

    background_update_events = [sinposmart_admin_event(event) for event in background_updates.values()]
    background_update_events.sort(key=lambda item: str(item.get("last_occurred_at") or ""), reverse=True)

    login_update_events = [sinposmart_admin_login_event(event, preferred_people) for event in login_events]
    login_update_events.sort(key=lambda item: str(item.get("last_occurred_at") or ""), reverse=True)

    unreturned_return_cards = [
        sinposmart_admin_unreturned_return_event(event)
        for event in unreturned_return_events.values()
        if str(event.get("status") or "").strip().lower() in {"pending", "retrying"}
    ]
    unreturned_return_cards.sort(key=lambda item: str(item.get("next_retry_at") or ""))
    unreturned_return_history_cards = [
        sinposmart_admin_unreturned_return_history_event(queue_events)
        for queue_events in unreturned_return_history_events.values()
        if queue_events
    ]
    active_unreturned_return_history_cards = sorted(
        (item for item in unreturned_return_history_cards if item.get("is_active")),
        key=lambda item: (
            str(item.get("next_retry_at") or ""),
            str(item.get("first_paused_at") or item.get("occurred_at") or ""),
        ),
    )
    completed_unreturned_return_history_cards = sorted(
        (item for item in unreturned_return_history_cards if not item.get("is_active")),
        key=lambda item: str(item.get("first_paused_at") or item.get("occurred_at") or ""),
        reverse=True,
    )
    unreturned_return_history_cards = (
        active_unreturned_return_history_cards + completed_unreturned_return_history_cards
    )

    summary = {
        "actions": len(action_events),
        "submitted": sum(1 for event in action_events if event["status_label"] == "已登打"),
        "existing": sum(1 for event in action_events if event["status_label"] == "已存在"),
        "failed": sum(1 for event in action_events if event["status_label"] == "失敗"),
        "waiting": sum(1 for event in action_events if event["status_label"] in {"等待登打", "等待完成回傳"}),
        "attention": sum(1 for event in action_events if event["status_label"] == "等待逾時待確認"),
        "tools": len(tool_update_events),
        "background_updates": len(background_update_events),
        "logins": len(login_update_events),
        "unreturned_returns": len(unreturned_return_cards),
        "unreturned_return_history": len(unreturned_return_history_cards),
    }
    return {
        "summary": summary,
        "action_events": action_events,
        "tool_events": tool_update_events,
        "background_updates": background_update_events,
        "login_events": login_update_events,
        "unreturned_return_events": unreturned_return_cards,
        "unreturned_return_history_events": unreturned_return_history_cards,
    }


class SinpoSmartBackendStore:
    def __init__(self, root_dir: Path, retention_days: int = 7) -> None:
        self.root_dir = root_dir
        self.retention_days = max(1, retention_days)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = shared_store_lock(self.root_dir)

    def upsert_event(self, raw_event: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            self.cleanup(now)
            event = normalize_sinposmart_event(raw_event, now=now)
            path = self.path_for_day(event["fire_day"])
            payload = self.read_day(event["fire_day"])
            payload.pop("admin_view", None)
            events = compact_sinposmart_events(list(payload.get("events") or []))
            known_ids = {event_id for item in events for event_id in sinposmart_event_ids(item)}
            if event["event_id"] in known_ids:
                pass
            elif sinposmart_event_keeps_individual_record(event):
                events.append(event)
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
        with self._lock:
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
                    payload["admin_view"] = build_sinposmart_admin_view(payload.get("events") or [], now=now)
                    days.append(payload)
            return days[:limit]

    def read_day(self, fire_day: str, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            path = self.path_for_day(fire_day)
            if not path.exists():
                return {"fire_day": fire_day, "updated_at": "", "summary": {}, "events": [], "admin_view": build_sinposmart_admin_view([], now=now)}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {"fire_day": fire_day, "updated_at": "", "summary": {}, "events": [], "admin_view": build_sinposmart_admin_view([], now=now)}
            if not isinstance(payload, dict):
                return {"fire_day": fire_day, "updated_at": "", "summary": {}, "events": [], "admin_view": build_sinposmart_admin_view([], now=now)}
            payload["events"] = compact_sinposmart_events(payload.get("events") or [])
            payload["summary"] = summarize_sinposmart_events(payload.get("events") or [])
            payload["admin_view"] = build_sinposmart_admin_view(payload.get("events") or [], now=now)
            return payload

    def cleanup(self, now: datetime | None = None) -> None:
        with self._lock:
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
    received_at = taipei_local_datetime(now or datetime.now())
    occurred_at = parse_event_datetime(raw_event.get("occurred_at"), received_at)
    record_type = str(raw_event.get("record_type") or "error").strip()
    if record_type not in SINPOSMART_RECORD_TYPES:
        record_type = "error"
    fire_day = str(raw_event.get("fire_day") or sinposmart_fire_day_for(occurred_at)).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fire_day):
        fire_day = sinposmart_fire_day_for(occurred_at)
    event = {
        "event_id": str(raw_event.get("event_id") or uuid4()).strip(),
        "merged_event_ids": [],
        "occurred_at": occurred_at.isoformat(timespec="seconds"),
        "received_at": received_at.isoformat(timespec="seconds"),
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
    event["merged_event_ids"] = [event["event_id"]]
    if record_type == "tool_action_started" and not event["item_title"]:
        snapshot = event["snapshot"] if isinstance(event.get("snapshot"), dict) else {}
        tool_label = sanitize_scalar(snapshot.get("tool_label"), 120)
        if tool_label:
            event["item_title"] = f"開始{tool_label}"
    return {field: event[field] for field in SINPOSMART_EVENT_FIELDS}


def sinposmart_event_merge_key(event: dict[str, Any]) -> tuple[str, ...]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    tool_label = sanitize_scalar(snapshot.get("tool_label"), 120) if snapshot else ""
    queue_id = sanitize_scalar(snapshot.get("queue_id"), 120) if snapshot else ""
    run_id = sanitize_scalar(snapshot.get("run_id"), 120) if snapshot else ""
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
        queue_id,
        run_id,
    )


def sinposmart_event_keeps_individual_record(event: dict[str, Any]) -> bool:
    return sinposmart_is_login_event(event) or str(event.get("record_type") or "") == "unreturned_return"


def event_repeat_count(event: dict[str, Any]) -> int:
    try:
        return max(1, int(event.get("repeat_count") or 1))
    except (TypeError, ValueError):
        return 1


def sinposmart_event_ids(event: dict[str, Any]) -> list[str]:
    raw_ids = event.get("merged_event_ids")
    candidates = raw_ids if isinstance(raw_ids, list) else []
    values = [str(event.get("event_id") or ""), *(str(item or "") for item in candidates)]
    return list(dict.fromkeys(value for value in values if value))


def merge_sinposmart_event(existing: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged["merged_event_ids"] = list(dict.fromkeys(sinposmart_event_ids(existing) + sinposmart_event_ids(event)))
    merged["repeat_count"] = event_repeat_count(existing) + event_repeat_count(event)
    merged["first_occurred_at"] = str(existing.get("first_occurred_at") or existing.get("occurred_at") or event.get("occurred_at") or "")
    merged["last_occurred_at"] = str(event.get("occurred_at") or existing.get("last_occurred_at") or existing.get("occurred_at") or "")
    merged["occurred_at"] = merged["last_occurred_at"] or str(existing.get("occurred_at") or "")
    if not str(merged.get("received_at") or "").strip() and event.get("received_at"):
        merged["received_at"] = event["received_at"]
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
        event["merged_event_ids"] = sinposmart_event_ids(event)
        if sinposmart_event_keeps_individual_record(event):
            event["repeat_count"] = 1
        event_ids = sinposmart_event_ids(event)
        event_id = event_ids[0] if event_ids else ""
        if any(item in known_ids for item in event_ids):
            continue
        if sinposmart_event_keeps_individual_record(event):
            for item in event_ids:
                known_ids[item] = len(compacted)
            compacted.append(event)
            continue
        merge_key = sinposmart_event_merge_key(event)
        if merge_key in index_by_key:
            compacted[index_by_key[merge_key]] = merge_sinposmart_event(compacted[index_by_key[merge_key]], event)
            for item in sinposmart_event_ids(compacted[index_by_key[merge_key]]):
                known_ids[item] = index_by_key[merge_key]
            continue
        index_by_key[merge_key] = len(compacted)
        for item in event_ids:
            known_ids[item] = len(compacted)
        compacted.append(event)
    return sorted(compacted, key=lambda item: str(item.get("occurred_at") or ""))


def parse_event_datetime(value: Any, fallback: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        return taipei_local_datetime(fallback)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return taipei_local_datetime(fallback)
    return taipei_local_datetime(parsed)


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
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
