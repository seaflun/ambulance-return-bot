# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import struct
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4
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
    "notice_collection",
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
LOGIN_SENSITIVE_TEXT_PATTERN = re.compile(
    r'''(?i)(password|passwd|pwd|token|secret|cookie|authorization|credential|username|user_id|account|session[_-]?id|api[_-]?key|帳號|账号|密碼|密码|帳密|權杖|驗證碼|验证码|授權碼)["']?\s*[:=：]\s*(?:"[^"\n]*"|'[^'\n]*'|[^,;，；\r\n]+)'''
)
LOGIN_CAPTURE_ERRORS = {
    "driver_unavailable": "瀏覽器尚未建立或已無法使用，無法擷取截圖。",
    "frame_context_failed": "無法切回主頁面，未執行截圖。",
    "redaction_rejected": "欄位或文字遮蔽檢查未通過，未執行截圖。",
    "redaction_failed": "遮蔽頁面時發生錯誤，未執行截圖。",
    "screenshot_failed": "瀏覽器擷取截圖時發生錯誤。",
    "invalid_image": "瀏覽器未回傳有效的 PNG 圖片。",
    "image_oversize": "圖片尺寸或容量超過保存限制。",
    "server_image_rejected": "後台圖片安全或格式驗證未通過，未保存截圖。",
}
LOGIN_CAPTURE_ERROR_TYPES = {
    "Error", "TypeError", "ValueError", "RuntimeError", "TimeoutError", "AttributeError", "SecurityError", "InvalidStateError",
    "WebDriverException", "TimeoutException", "JavascriptException", "UnexpectedAlertPresentException", "NoSuchWindowException",
    "InvalidSessionIdException", "NoSuchFrameException", "StaleElementReferenceException", "InvalidArgumentException",
}
DAILY_VEHICLE_REASONS = {
    "verified_today": "已確認今日清點紀錄。", "saved_verified": "儲存後已回查確認。",
    "not_required": "網站標示今日不用保養",
    "empty_list": "清單沒有可確認的車輛。", "today_not_found": "尚未找到今日紀錄。",
    "state_unconfirmed": "清點狀態尚未確認。", "save_unconfirmed": "儲存結果尚未回查確認。",
    "list_changed": "查詢期間清單已變更，需重新確認。", "execution_unconfirmed": "未能確認本次執行的全部結果。",
    "report_missing": "此紀錄沒有逐車清點明細。", "report_invalid": "回報明細不完整或格式不符，無法確認全部完成。",
    "unexpected_error": "清點期間發生未預期錯誤。", "row_limit": "明細超過 200 筆保存限制，無法確認全部完成。",
}
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


GUI_HEARTBEAT_ONLINE_SECONDS = 45
GUI_HEARTBEAT_STARTUP_SECONDS = 60
GUI_HEARTBEAT_FIELDS = frozenset({
    "schema_version", "workstation", "component", "instance_id", "process_started_at", "process_id",
    "sequence", "app_version", "state", "login_state", "phase", "update_request_id", "target_version",
    "result_code", "exit_code", "progress_percent", "route",
})
GUI_HEARTBEAT_PHASE_LABELS = {
    "startup": "啟動中", "idle": "閒置", "busy": "處理中", "preparing": "準備更新",
    "staged": "更新已備妥", "waiting_handoff": "等待交接", "waiting_parent": "等待值班台退出",
    "preflight": "更新前檢查", "waiting_busy": "等待作業完成", "applying": "套用更新",
    "installing": "安裝中", "environment_check": "檢查執行環境", "restarting": "重新啟動",
    "waiting_gui": "等待新版啟動", "completed": "安裝器步驟完成", "failed": "失敗",
    "stopping": "關閉中", "unknown": "未知",
}


def _heartbeat_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _heartbeat_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=TAIPEI_TIMEZONE)
    return current.astimezone(timezone.utc)


def _heartbeat_time_label(value: datetime | None) -> str:
    if value is None:
        return ""
    try:
        return taipei_local_datetime(value).isoformat(sep=" ", timespec="seconds")
    except (ValueError, OverflowError):
        return ""


def _heartbeat_uuid(value: Any, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("invalid_heartbeat")
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError("invalid_heartbeat") from None


def normalize_duty_gui_heartbeat(raw: Any, server_instance_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != GUI_HEARTBEAT_FIELDS:
        raise ValueError("invalid_heartbeat")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("invalid_heartbeat")
    workstation = raw["workstation"]
    if not isinstance(workstation, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", workstation):
        raise ValueError("invalid_heartbeat")
    for key in ("process_id", "sequence"):
        if type(raw[key]) is not int or raw[key] <= 0:
            raise ValueError("invalid_heartbeat")
    for key in ("app_version", "target_version"):
        value = raw[key]
        if key == "target_version" and value == "":
            continue
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]{4}", value):
            raise ValueError("invalid_heartbeat")
    choices = {
        "component": {"gui", "installer"},
        "state": {"starting", "idle", "busy", "updating", "failed", "stopping"},
        "login_state": {"unknown", "logged_out", "logging_in", "logged_in"},
        "phase": set(GUI_HEARTBEAT_PHASE_LABELS),
        "result_code": {"", "installer_failed", "gui_start_failed", "gui_ready_timeout", "update_completed", "closed"},
    }
    for key, allowed in choices.items():
        if not isinstance(raw[key], str) or raw[key] not in allowed:
            raise ValueError("invalid_heartbeat")
    for key, lower, upper in (("exit_code", -1, 65535), ("progress_percent", 0, 100)):
        if raw[key] is not None and (type(raw[key]) is not int or not lower <= raw[key] <= upper):
            raise ValueError("invalid_heartbeat")
    started = _heartbeat_datetime(raw["process_started_at"])
    if started is None:
        raise ValueError("invalid_heartbeat")
    request_id = _heartbeat_uuid(raw["update_request_id"], optional=True)
    if raw["component"] == "installer" and (not request_id or raw["login_state"] != "unknown"):
        raise ValueError("invalid_heartbeat")
    route = raw["route"]
    if (not isinstance(route, dict) or set(route) != {"name", "identity_status", "instance_id"}
            or not isinstance(route["name"], str) or route["name"] not in {"lan", "tailscale", "configured"}
            or route["identity_status"] != "verified"
            or _heartbeat_uuid(route["instance_id"]) != _heartbeat_uuid(server_instance_id)):
        raise ValueError("invalid_heartbeat")
    result = dict(raw)
    result.update(instance_id=_heartbeat_uuid(raw["instance_id"]), update_request_id=request_id,
                  process_started_at=started.isoformat(), route=dict(route))
    result["route"]["instance_id"] = _heartbeat_uuid(route["instance_id"])
    return result


class DutyGuiHeartbeatStore:
    """Technical telemetry only; never reads or writes event/update control records."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = shared_store_lock(path.parent)

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "workstations": {}}
        except (OSError, ValueError):
            raise OSError("heartbeat_store_unavailable") from None
        if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
                or not isinstance(payload.get("workstations"), dict)):
            raise OSError("heartbeat_store_unavailable")
        return payload

    def upsert(self, raw: Any, *, server_instance_id: str, now: datetime | None = None) -> bool:
        incoming = normalize_duty_gui_heartbeat(raw, server_instance_id)
        received = _heartbeat_now(now).isoformat(timespec="seconds")
        with self._lock:
            payload = self._read()
            stations = payload["workstations"]
            station = stations.get(incoming["workstation"], {})
            if not isinstance(station, dict):
                raise OSError("heartbeat_store_unavailable")
            if incoming["workstation"] not in stations and len(stations) >= 32:
                return False
            current = station.get(incoming["component"], {})
            retired: list[str] = []
            if current:
                try:
                    normalized = normalize_duty_gui_heartbeat(
                        {key: current[key] for key in GUI_HEARTBEAT_FIELDS}, current["route"]["instance_id"])
                    retired = [_heartbeat_uuid(item) for item in current.get("retired_instances", [])][-16:]
                except (KeyError, TypeError, ValueError):
                    raise OSError("heartbeat_store_unavailable") from None
                if incoming["instance_id"] == normalized["instance_id"]:
                    immutable = ["process_started_at", "process_id", "app_version"]
                    if incoming["component"] == "installer":
                        immutable += ["update_request_id", "target_version"]
                    if (incoming["sequence"] <= normalized["sequence"]
                            or any(incoming[key] != normalized[key] for key in immutable)):
                        return False
                else:
                    if (incoming["instance_id"] in retired
                            or _heartbeat_datetime(incoming["process_started_at"]) <=
                            _heartbeat_datetime(normalized["process_started_at"])):
                        return False
                    retired = (retired + [normalized["instance_id"]])[-16:]
            first_received = (current.get("first_received_at") if current
                              and current.get("instance_id") == incoming["instance_id"] else received)
            station[incoming["component"]] = {
                **incoming, "received_at": received, "first_received_at": first_received,
                "retired_instances": retired,
            }
            stations[incoming["workstation"]] = station
            write_json_atomic(self.path, payload)
        return True

    def admin_view(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = _heartbeat_now(now)
        try:
            with self._lock:
                stations = self._read()["workstations"]
        except OSError:
            stations = {}
        result = {}
        for component in ("gui", "installer"):
            valid = []
            for station in stations.values():
                record = station.get(component) if isinstance(station, dict) else None
                if not isinstance(record, dict):
                    continue
                try:
                    normalized = normalize_duty_gui_heartbeat(
                        {key: record[key] for key in GUI_HEARTBEAT_FIELDS}, record["route"]["instance_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                received = _heartbeat_datetime(record.get("received_at"))
                first = _heartbeat_datetime(record.get("first_received_at"))
                if received is not None and first is not None and first <= received:
                    valid.append((received, first, normalized, record))
            latest = max(valid, key=lambda item: item[0], default=None)
            if latest is None:
                result[component] = {"alive": False, "fresh": False, "status_class": "",
                                     "status_label": "尚未收到心跳／狀態未知", "last_seen_at": "尚未收到",
                                     "last_seen_label": "", "process_started_label": "",
                                     "age_seconds": None, "age_label": "未知", "app_version": "未知",
                                     "state_label": "未知", "login_label": "未知", "phase_label": "未知",
                                     "instance_id": "", "process_started_at": "", "workstation": "",
                                     "startup_label": "", "retired_count": 0, "update_request_id": "",
                                     "result_label": "", "result_code": "", "exit_code": None,
                                     "target_version": "", "progress_label": "", "last_known_label": "",
                                     "state": "unknown", "phase": "unknown"}
                continue
            received, first, normalized, record = latest
            age = (current - received).total_seconds()
            fresh = 0 <= age <= GUI_HEARTBEAT_ONLINE_SECONDS
            stopping = normalized["state"] == "stopping"
            alive = fresh and not stopping
            status = "已回報關閉" if stopping else "在線" if alive else "失聯／無法確認程式是否開啟"
            state_label = {"starting": "啟動中", "idle": "閒置", "busy": "處理中", "updating": "更新中",
                           "failed": "失敗", "stopping": "關閉中"}[normalized["state"]]
            login = {"unknown": "未知", "logged_out": "未登入", "logging_in": "登入中",
                     "logged_in": "已登入"}[normalized["login_state"]]
            startup = ""
            if normalized["state"] == "starting":
                startup = "啟動中" if (current - first).total_seconds() <= GUI_HEARTBEAT_STARTUP_SECONDS else "啟動仍未完成"
            result[component] = {
                "alive": alive, "fresh": fresh, "status_class": "complete" if alive else "" if stopping else "failed",
                "status_label": status, "last_seen_at": received.isoformat(timespec="seconds"),
                "last_seen_label": _heartbeat_time_label(received),
                "age_seconds": int(age) if age >= 0 else None, "age_label": f"{int(age)} 秒前" if age >= 0 else "時間待確認",
                "app_version": normalized["app_version"], "state_label": state_label + ("（最後回報）" if not fresh else ""),
                "login_label": login + ("（最後回報）" if not fresh else ""),
                "phase_label": GUI_HEARTBEAT_PHASE_LABELS[normalized["phase"]] + ("（最後回報）" if not fresh else ""),
                "last_known_label": "" if fresh else "以下為最後回報",
                "instance_id": normalized["instance_id"], "process_started_at": normalized["process_started_at"],
                "process_started_label": _heartbeat_time_label(_heartbeat_datetime(normalized["process_started_at"])),
                "workstation": normalized["workstation"], "startup_label": startup,
                "retired_count": len(record["retired_instances"]) if isinstance(record.get("retired_instances"), list) else 0,
                "update_request_id": normalized["update_request_id"], "result_code": normalized["result_code"],
                "exit_code": normalized["exit_code"],
                "result_label": {"": "", "installer_failed": "安裝失敗", "gui_start_failed": "新版啟動失敗",
                                 "gui_ready_timeout": "等待新版啟動逾時", "update_completed": "安裝器步驟完成",
                                 "closed": "已回報關閉"}[normalized["result_code"]],
                "target_version": normalized["target_version"],
                "progress_label": f"{normalized['progress_percent']}%" if normalized["progress_percent"] is not None else "",
                "state": normalized["state"], "phase": normalized["phase"],
            }
        return result


def duty_gui_installer_activity_view(installer: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    """Describe update activity without changing process freshness or install proof."""
    view = {**installer, "status_label": "未執行更新", "status_class": "", "progress_label": ""}
    request_id = str(command.get("request_id") or "")
    owner = str(command.get("worker_id") or "")
    status = command.get("status")
    status = status if isinstance(status, str) else ""
    applying = status in {"applying", "updating"}
    matching = bool(request_id and installer.get("update_request_id") == request_id
                    and (not owner or installer.get("workstation") == owner))
    fresh_host = installer.get("alive") is True and installer.get("fresh") is True
    if matching and fresh_host:
        if installer.get("state") == "failed" or installer.get("phase") == "failed":
            view.update(status_label="更新失敗", status_class="failed", progress_label=installer.get("progress_label", ""))
            return view
        if (installer.get("state") in {"starting", "busy", "updating"}
                and status not in {"completed", "up_to_date"}):
            view.update(status_label="更新中", status_class="running", progress_label=installer.get("progress_label", ""))
            return view
    if applying:
        view.update(status_label="更新狀態待確認", status_class="attention")
    return view


def duty_gui_installation_view(health: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    gui, installer = health["gui"], health["installer"]
    request_id = str(command.get("request_id") or "")
    owner = str(command.get("worker_id") or "")
    installer_current = bool(request_id and installer["update_request_id"] == request_id
                             and (not owner or installer["workstation"] == owner))
    installed = command.get("installed_version")
    verified = bool(gui["alive"] and request_id and gui["update_request_id"] == request_id
                    and (not owner or gui["workstation"] == owner)
                    and isinstance(command.get("status"), str) and command["status"] in {"completed", "up_to_date"}
                    and type(command.get("exit_code")) is int and command["exit_code"] == 0
                    and installed and gui["app_version"] == installed)
    return {"verified": verified, "status_label": "已確認新版啟動" if verified else "尚未確認新版啟動",
            "installer_current": installer_current,
            "installer_request_label": "對應目前更新命令" if installer_current else
            "其他更新請求" if installer["update_request_id"] else "尚未回報更新請求"}


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
        "cancelled": "人工取消（停止重查與補登）",
    }
    return labels.get(str(value or "").strip().lower(), "未返隊暫停")


def sinposmart_unreturned_return_status_class(value: str) -> str:
    status = str(value or "").strip().lower()
    if status in {"resolved", "cancelled"}:
        return "complete"
    if status == "expired":
        return "failed"
    return "running"


def sinposmart_unreturned_return_trigger_label(event: dict[str, Any]) -> str:
    trigger_type = str(event.get("trigger_type") or "").strip().lower()
    if trigger_type == "admin_cancel":
        return "NAS 人工取消"
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


def sinposmart_completion_key_identity(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def sinposmart_completion_key_digest(snapshot: dict[str, Any]) -> str:
    completion_key = sinposmart_completion_key_identity(snapshot.get("completion_key"))
    if not completion_key:
        return ""
    recorded_digest = sinposmart_completion_key_identity(snapshot.get("completion_key_sha256")).lower()
    if re.fullmatch(r"[0-9a-f]{64}", recorded_digest):
        return recorded_digest
    return hashlib.sha256(completion_key.encode("utf-8")).hexdigest()


def sinposmart_action_group_key(event: dict[str, Any], *, include_target: bool = True) -> tuple[str, ...]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    completion_key = sinposmart_completion_key_identity(snapshot.get("completion_key")) if snapshot else ""
    if completion_key:
        return ("completion_key", sinposmart_completion_key_digest(snapshot))
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


def _personnel_title_snapshot(raw: Any, *, allow_incomplete: bool = False) -> dict[str, Any]:
    if (not isinstance(raw, dict) or type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1 or raw.get("source") != "official_duty_staff"
            or type(raw.get("complete")) is not bool):
        return {}
    target = raw.get("target_date")
    if (not isinstance(target, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", target)
            or parse_sinposmart_snapshot_date(target) is None):
        return {}
    envelope = {"schema_version": 1, "source": "official_duty_staff", "target_date": target,
                "complete": False, "members": []}
    unknown = envelope if allow_incomplete else {}
    rows = raw.get("members")
    if not raw["complete"] or not isinstance(rows, list) or len(rows) > 200:
        return unknown
    members = []
    for row in rows:
        if not isinstance(row, dict):
            return unknown
        actor, name, title = row.get("actor_no"), row.get("name"), row.get("title")
        if (not isinstance(actor, str) or not re.fullmatch(r"[0-9]{1,4}", actor.strip())
                or not isinstance(name, str) or not name.strip() or len(name) > 80
                or not all(char.isalpha() or char in " ·．・-'’" for char in name)
                or not any(char.isalpha() for char in name) or not isinstance(title, str)):
            return unknown
        members.append({"actor_no": actor.strip(), "name": " ".join(name.split()),
                        "title": title.strip() if title.strip() in {"分隊長", "小隊長", "隊員"} else ""})
    return {**envelope, "complete": True, "members": members}


def normalize_personnel_title_snapshot(raw: Any) -> dict[str, Any]:
    """Return an official complete roster without credentials, or an unknown empty result."""
    return _personnel_title_snapshot(raw)


def _personnel_title_snapshot_key(snapshot: dict[str, Any]) -> tuple:
    return (snapshot["target_date"], snapshot["complete"], tuple(sorted(
        (row["actor_no"], row["name"], row["title"]) for row in snapshot["members"])))


def read_personnel_title_snapshot(
    artifacts_dir: Path, now: datetime | None = None, *, target_date: str | None = None,
) -> dict[str, Any]:
    """Read the requested day's newest complete roster without cleanup or writes."""
    current = taipei_local_datetime(now or datetime.now(TAIPEI_TIMEZONE))
    target = current.date() if target_date is None else parse_sinposmart_snapshot_date(target_date)
    if target is None or target > current.date():
        return {}
    cutoff = current - timedelta(hours=24)
    days = {sinposmart_fire_day_for(current), sinposmart_fire_day_for(cutoff)}
    candidates = []
    for day in sorted(days):
        path = Path(artifacts_dir) / "sinposmart" / f"{day}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            return {}
        for event in payload["events"]:
            if not isinstance(event, dict) or event.get("record_type") != "schedule_snapshot":
                continue
            raw_snapshot = event.get("snapshot")
            if not isinstance(raw_snapshot, dict) or "personnel_titles" not in raw_snapshot:
                continue
            snapshot = _personnel_title_snapshot(raw_snapshot["personnel_titles"], allow_incomplete=True)
            if not snapshot or snapshot["target_date"] != target.isoformat():
                continue
            try:
                occurred = taipei_local_datetime(datetime.fromisoformat(sinposmart_event_time(event)))
            except (TypeError, ValueError, OverflowError):
                return {}
            if occurred > current:
                return {}
            if occurred >= cutoff:
                candidates.append((occurred, snapshot))
    if not candidates:
        return {}
    latest_at = max(occurred for occurred, _snapshot in candidates)
    latest = [snapshot for occurred, snapshot in candidates if occurred == latest_at]
    if len({_personnel_title_snapshot_key(snapshot) for snapshot in latest}) != 1:
        return {}
    snapshot = normalize_personnel_title_snapshot(latest[0])
    return snapshot if snapshot and snapshot["members"] else {}


def personnel_titles_for_case(snapshot: Any, case_date: Any, personnel: list[str]) -> dict[str, str]:
    """Select unambiguous titles for the exact case day without changing personnel values."""
    roster = normalize_personnel_title_snapshot(snapshot)
    case_day = parse_sinposmart_snapshot_date(str(case_date or "").replace("/", "-"))
    if (not roster or case_day is None or case_day.isoformat() != roster["target_date"]
            or not isinstance(personnel, (list, tuple))):
        return {}
    by_name, actor_names = {}, {}
    for member in roster["members"]:
        actor = str(int(member["actor_no"]))
        by_name.setdefault(member["name"], set()).add((actor, member["title"]))
        actor_names.setdefault(actor, set()).add(member["name"])
    selected = {}
    for name in personnel:
        if not isinstance(name, str) or len(by_name.get(name, ())) != 1:
            continue
        actor, title = next(iter(by_name[name]))
        if title and len(actor_names[actor]) == 1:
            selected[name] = title
    return selected


def personnel_actor_numbers_for_case(snapshot: Any, case_date: Any, personnel: list[str]) -> dict[str, int]:
    """Use the same date, completeness and identity guards as displayed titles."""
    titles = personnel_titles_for_case(snapshot, case_date, personnel)
    roster = normalize_personnel_title_snapshot(snapshot)
    return {member["name"]: int(member["actor_no"]) for member in (roster or {}).get("members", [])
            if member["name"] in titles}


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


def sinposmart_tool_group_key(event: dict[str, Any], *, run_id: str | None = None) -> tuple[str, ...]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    tool_name = sanitize_scalar(snapshot.get("tool_name"), 120) if snapshot else ""
    tool_label = sanitize_scalar(snapshot.get("tool_label"), 120) if snapshot else ""
    resolved_run_id = run_id if run_id is not None else sanitize_scalar(snapshot.get("run_id"), 120) if snapshot else ""
    title = str(event.get("item_title") or "")
    if sanitize_scalar(snapshot.get("run_id"), 120):
        return ("run_id", tool_name or tool_label or title, resolved_run_id)
    return (
        str(event.get("actor_no") or ""),
        sinposmart_person_label(event),
        tool_name or tool_label or title,
        resolved_run_id,
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


def sinposmart_duty_sheet_summary(event: dict[str, Any]) -> dict[str, str]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    if sanitize_scalar(snapshot.get("tool_name"), 120) != "duty_sheet":
        return {}
    workbook_name = sanitize_scalar(snapshot.get("workbook_name"), 240).replace("\\", "/").rsplit("/", 1)[-1]
    raw_date = sanitize_scalar(snapshot.get("target_date"), 40)
    target_date = parse_sinposmart_snapshot_date(raw_date.replace("/", "-"))
    mode_label = {"manual": "手動", "automatic": "自動"}.get(sanitize_scalar(snapshot.get("execution_mode"), 40), "未知")
    return {
        "workbook_name": workbook_name or "未知",
        "target_date": target_date.strftime("%Y/%m/%d") if target_date else "未知",
        "mode_label": mode_label,
    }


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
    mode = sanitize_scalar(snapshot.get("mode"), 40)
    mode_label = {
        "preview": "預覽分類",
        "copy": "實際複製",
        "delete": "複製並刪除來源",
    }.get(mode, "")
    reason_labels = {
        "no_matching_work_or_return_time": "沒有符合工作／返隊時間的案件",
        "destination_mismatch": "目的地不一致",
        "source_unreadable": "影片無法讀取",
        "classification_error": "分類或驗證失敗",
    }
    classification_rows: list[dict[str, str]] = []
    raw_rows = snapshot.get("classification_rows")
    if isinstance(raw_rows, list):
        for raw_row in raw_rows[:200]:
            if not isinstance(raw_row, dict):
                continue
            source_file = sanitize_scalar(raw_row.get("source_file"), 160).replace("\\", "/").rsplit("/", 1)[-1]
            case_name = sanitize_scalar(raw_row.get("case"), 160).replace("\\", "/").rsplit("/", 1)[-1]
            reason = sanitize_scalar(raw_row.get("reason"), 80)
            row = {
                "video_time": sanitize_scalar(raw_row.get("video_time"), 40),
                "source_file": source_file,
                "case": case_name or "待確認",
                "status": sanitize_scalar(raw_row.get("status"), 80),
                "reason_label": reason_labels.get(reason, ""),
            }
            if any(row.values()):
                classification_rows.append(row)
    summary: dict[str, Any] = {
        "target_date": target_date,
        "vehicle": vehicle,
        "case_count": case_count,
        "total_count": total_count,
        "usage_time": sinposmart_elapsed_label(usage_seconds),
    }
    if mode_label:
        summary["mode_label"] = mode_label
    if classification_rows:
        summary["classification_rows"] = classification_rows
        summary["classification_rows_omitted"] = max(0, total_count - len(classification_rows))
    return summary


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
    card["queue_id"] = sanitize_scalar(snapshot.get("queue_id"), 80)
    card["workstation"] = sanitize_scalar(snapshot.get("workstation"), 120)
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
    current_label = sinposmart_action_status_label(str(current.get("status") or ""), str(current.get("record_type") or ""))
    candidate_label = sinposmart_action_status_label(str(candidate.get("status") or ""), str(candidate.get("record_type") or ""))
    # A later existence check is not a replacement for a recorded submission.
    if current_label == "已登打" and candidate_label == "已存在":
        return current
    if current_label == "已存在" and candidate_label == "已登打":
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


def sinposmart_following_day_result_matches(queued: dict[str, Any], result: dict[str, Any]) -> bool:
    key = sinposmart_action_group_key(queued)
    if key[0] != "completion_key" or sinposmart_action_group_key(result) != key:
        return False
    if str(result.get("record_type") or "") != "action_result":
        return False
    status = str(result.get("status") or "").strip().lower()
    terminal_statuses = {
        "submitted", "saved", "success", "completed", "ok", "已登打", "已儲存", "成功", "完成",
        "skipped_duplicate", "failed", "error", "fail", "失敗",
    }
    if status not in terminal_statuses or sinposmart_action_status_label(status, "action_result") not in {"已登打", "已存在", "失敗"}:
        return False
    queued_day = parse_sinposmart_snapshot_date(queued.get("fire_day"))
    result_day = parse_sinposmart_snapshot_date(result.get("fire_day"))
    if not queued_day or not result_day or (result_day - queued_day).days != 1:
        return False
    try:
        queued_at = taipei_local_datetime(datetime.fromisoformat(sinposmart_event_time(queued)))
        result_at = taipei_local_datetime(datetime.fromisoformat(sinposmart_event_time(result)))
    except ValueError:
        return False
    return result_at >= queued_at


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
    queued_day = parse_sinposmart_snapshot_date(queued_event.get("fire_day")) if queued_event else None
    result_day = parse_sinposmart_snapshot_date(result_event.get("fire_day")) if result_event else None
    card["completion_source_fire_day"] = result_day.isoformat() if queued_day and result_day and queued_day != result_day else ""
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
        "maintenance_page_ready": "等待保養頁初始化",
        "maintenance_query_start": "啟動保養查詢",
        "maintenance_query_wait": "等待保養查詢結果",
        "maintenance_query_verify": "核對保養本次查詢",
        "maintenance_inventory_verify": "核對保養完整清單",
        "equipment_page_ready": "等待器材頁初始化",
        "equipment_query_start": "啟動器材查詢",
        "equipment_query_wait": "等待器材查詢結果",
        "equipment_query_verify": "核對器材本次查詢",
        "equipment_inventory_verify": "核對器材完整清單",
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
    card = sinposmart_admin_event(
        base_event, status_label,
        person_label=sinposmart_person_label(started_event) if started_event else None,
    )
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
    card["duty_sheet_summary"] = sinposmart_duty_sheet_summary(base_event)
    snapshot = base_event.get("snapshot") if isinstance(base_event.get("snapshot"), dict) else {}
    is_daily_vehicle = snapshot.get("tool_name") == "daily_vehicle"
    card["daily_vehicle_report"] = None
    card["daily_vehicle_report_missing"] = bool(is_daily_vehicle and finished_event and "daily_vehicle_report" not in snapshot)
    if is_daily_vehicle and "daily_vehicle_report" in snapshot:
        report = sanitize_daily_vehicle_report(snapshot["daily_vehicle_report"], expected_date=sinposmart_daily_vehicle_target_date(base_event))
        if failed and report["verified"]:
            report.update(verified=False, reason_code="execution_unconfirmed")
        if finished_event and not failed and not report["verified"]:
            card.update(status_label="結果未確認", status_class="attention")
            steps[-1].update(status_label="結果未確認", status_class="attention")
        report["mode_label"] = {"manual": "手動", "automatic": "自動"}.get(report["execution_mode"], "未記錄")
        report["reason_label"] = DAILY_VEHICLE_REASONS.get(report["reason_code"], "")
        report["verified_label"] = "已確認全部作業結果" if any(row["status"] == "not_required" for row in report["items"]) else "已驗證全部完成"
        for row in report["items"]:
            row["operation_label"] = {"maintenance": "車輛保養", "equipment": "器材清點"}[row["operation"]]
            row["status_label"] = {"completed": "已完成並回查", "already_done": "今日已完成", "not_required": "不用保養", "unconfirmed": "未確認"}[row["status"]]
            row["reason_label"] = DAILY_VEHICLE_REASONS[row["reason_code"]]
        card["daily_vehicle_report"] = report
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
        snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
        diagnostic = snapshot.get("login_diagnostic") if isinstance(snapshot.get("login_diagnostic"), dict) else {}
        if diagnostic.get("reason_code") == "password_change_required":
            return "密碼須更新"
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
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    diagnostic = sanitize_login_diagnostic(snapshot.get("login_diagnostic"))
    if diagnostic:
        card["login_diagnostic"] = diagnostic
        card["login_stage_label"] = "登入後檢查" if diagnostic["stage"] == "post_login" else "網站登入"
    if record_type == "login_failed" or (record_type == "error" and str(event.get("trigger_type") or "") == "login"):
        card["content"] = sanitize_login_text(card["content"], 1200)
        card["error"] = sanitize_login_text(card["error"], 1200)
    if record_type in {"login", "login_failed"}:
        snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
        method = str(snapshot.get("login_method") or "")
        card["login_method_label"] = {
            "automatic": "自動登入",
            "manual": "手動登入",
        }.get(method, "方式未記錄")
    if record_type == "logout":
        snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
        trigger = str(event.get("trigger_type") or "")
        method = "update" if trigger == "update" else str(snapshot.get("logout_method") or trigger)
        card["status_label"] = {
            "automatic": "自動登出",
            "manual": "手動登出",
            "system": "系統登出",
            "update": "更新前登出",
        }.get(method, "登出")
    card["login_detail"] = diagnostic.get("reason") or card["error"] or (card["content"] if card["status_class"] == "failed" else "")
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
    related_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action_groups: dict[tuple[str, ...], dict[str, dict[str, Any]]] = {}
    tool_events: dict[tuple[str, ...], dict[str, dict[str, Any]]] = {}
    legacy_tool_starts: dict[tuple[str, ...], list[str]] = {}
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
            snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
            run_id = sanitize_scalar(snapshot.get("run_id"), 120) if snapshot else ""
            if run_id:
                key = sinposmart_tool_group_key(event)
            else:
                base_key = sinposmart_tool_group_key(event, run_id="")
                event_id = str(event.get("event_id") or sinposmart_event_time(event) or "unknown")
                if record_type == "tool_action_started":
                    run_id = f"legacy:{event_id}"
                    legacy_tool_starts.setdefault(base_key, []).append(run_id)
                else:
                    pending_starts = legacy_tool_starts.get(base_key, [])
                    run_id = pending_starts.pop(0) if pending_starts else f"legacy:{event_id}"
                key = sinposmart_tool_group_key(event, run_id=run_id)
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

    # Queues and rechecks can cross 08:00; borrow proof without changing day events.
    local_result_keys = {key for key, state in action_groups.items() if state.get("result")}
    related_events = sorted(related_results or [], key=sinposmart_event_time)
    for event in related_events:
        key = sinposmart_action_group_key(event)
        if key[0] != "completion_key" or key not in action_groups or key in local_result_keys:
            continue
        action_state = action_groups[key]
        queued = action_state.get("queued")
        if queued and sinposmart_following_day_result_matches(queued, event):
            action_state["result"] = better_sinposmart_action_result(action_state.get("result"), event)
    # Preserve descending prior-proof selection for existing duplicate results.
    for event in reversed(related_events):
        key = sinposmart_action_group_key(event)
        if key[0] != "completion_key" or key not in action_groups or key not in local_result_keys:
            continue
        current = action_groups[key].get("result")
        if not current or str(current.get("status") or "") != "skipped_duplicate":
            continue
        if str(event.get("record_type") or "") != "action_result":
            continue
        if sinposmart_action_status_label(str(event.get("status") or ""), "action_result") != "已登打":
            continue
        if sinposmart_event_time(event) <= sinposmart_event_time(current):
            action_groups[key]["result"] = event

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


NOTICE_STATES = {"running", "waiting", "missing_credentials", "success", "partial", "uncertain", "failed"}
NOTICE_TERMINAL_STATES = {"success", "partial", "uncertain", "failed"}
NOTICE_STATE_PRIORITY = {"running": 0, "waiting": 1, "success": 2, "missing_credentials": 3,
                         "partial": 3, "uncertain": 3, "failed": 3, "invalid": 4}
NOTICE_STATUS = {
    "running": ("收取中", "running"), "waiting": ("等待收取", "running"),
    "missing_credentials": ("尚未設定通報帳密", "attention"), "success": ("未讀列表檢查完成", "attention"),
    "partial": ("部分完成，請確認", "attention"), "uncertain": ("收取結果待確認", "attention"),
    "failed": ("收取失敗", "failed"), "invalid": ("回報格式不符，狀態待確認", "attention"),
}
NOTICE_REASONS = {
    "configuration": "通報帳密尚未設定或設定無法使用。", "login": "通報網站登入未完成，請確認登入狀態。",
    "scan": "未能完成未讀通報檢查。", "write": "部分通報尚未確認寫入試算表。",
    "mark_read": "通報已入表，但部分已讀狀態尚未確認。", "state": "通報收取狀態待確認。",
    "busy": "瀏覽器忙碌，等待下一次檢查。", "unknown": "通報收取發生異常，請至值班台確認。",
}
NOTICE_ERROR_MESSAGES = {
    "publication_invalid": "通報公布日期時間或已讀狀態無法確認，補查尚未完成。",
    "oa_ocr_unavailable": "通報驗證碼辨識元件未就緒，請確認公務電腦執行環境。",
    "oa_captcha_not_ready": "通報驗證碼圖片尚未完整載入，未送出登入。",
    "oa_captcha_unrecognized": "通報驗證碼未能辨識，未送出登入。",
    "oa_captcha_ocr_failed": "通報驗證碼辨識元件執行失敗，未送出登入。",
    "oa_captcha_rejected": "通報網站判定驗證碼不正確，請稍後重試。",
    "oa_credentials_rejected": "通報網站拒絕帳號密碼，請確認已儲存的通報帳密。",
    "oa_login_page_failed": "通報登入頁載入失敗，請確認網站與連線。",
    "oa_login_form_timeout": "通報登入表單載入逾時，未完成登入。",
    "oa_login_verification_timeout": "通報登入送出後驗證逾時，尚未確認登入成功。",
    "recipient_unconfirmed": "通報簽收責任尚未確認，請人工核對。",
    "browser_start_failed": "通報專用瀏覽器無法啟動，請確認 Chrome 狀態。",
    "browser_window_closed": "通報專用視窗已關閉，下輪將重新開啟。",
    "snapshot_failed": "通報頁未正常回應，請確認通報專用瀏覽器。",
    "snapshot_invalid": "通報頁資料格式無法確認。",
    "origin_mismatch": "通報頁來源不符，已停止操作。",
    "account_mismatch": "通報頁登入身分不符，僅允許新坡分隊公務信箱。",
    "login_target_mismatch": "登入頁來源或路徑不符，未填入任何帳號密碼。",
    "login_page_failed": "通報登入頁重新整理失敗，請確認連線。",
    "list_navigation_failed": "通報列表重新整理失敗，請確認連線。",
    "list_wait_timeout": "未讀列表尚未完成載入或資料不完整，已停止本輪收取。",
    "page_limit": "未讀通報頁數過多，請先人工確認收取範圍。",
    "list_changed": "未讀列表在查詢期間變動，將於下輪重新確認。",
    "duplicate_notice_id": "通報分頁出現重複識別碼，已停止本輪收取。",
    "next_page_ambiguous": "無法唯一確認通報下一頁按鈕。",
    "count_mismatch": "未讀通報數量不符，已停止本輪收取。",
    "notice_url_invalid": "通報內文網址無法確認，已停止讀取。",
    "scan_unexpected": "通報列表載入未完成，已保留原有未讀狀態。",
    "session_expired": "入口網作業逾時，將於下輪重新登入。",
    "login_failed": "通報背景登入未成功，請確認已儲存的密碼與網站狀態。",
    "credentials_missing": "通報帳號尚未設定密碼，請先至值班台設定。",
    "cancelled": "通報收取已停止。",
}
NOTICE_COUNT_FIELDS = ("unread_count", "new_count", "marked_read_count", "pending_write_count", "pending_read_count")
NOTICE_VERIFICATION_FILENAME = "notice_verifications.json"
NOTICE_VERIFICATION_NOTES = {
    "read_confirmed": "已核對試算表及網站已讀清單。",
    "original_not_found": "已核對試算表；本次網站查核未找到原件，撤回／重發尚未證實。",
}


def normalize_notice_verifications(value: Any, *, now: datetime | None = None) -> list[dict[str, Any]]:
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or not isinstance(value.get("entries"), list)
            or len(value["entries"]) > 200):
        return []
    current, entries, conflicts = _heartbeat_now(now), {}, set()
    for raw in value["entries"]:
        if not isinstance(raw, dict):
            continue
        station, key, result = raw.get("workstation"), raw.get("key"), raw.get("result")
        subject, related, note = raw.get("subject"), raw.get("related_key", ""), raw.get("note", "")
        received, verified = _heartbeat_datetime(raw.get("received_at")), _heartbeat_datetime(raw.get("verified_at"))
        if (not isinstance(station, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", station) is None
                or not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None
                or not isinstance(result, str) or result not in NOTICE_VERIFICATION_NOTES
                or not isinstance(subject, str) or len(subject) > 240
                or not isinstance(related, str) or related and (re.fullmatch(r"[0-9a-f]{64}", related) is None or related == key)
                or not isinstance(note, str) or note not in {"", NOTICE_VERIFICATION_NOTES[result]}
                or received is None or verified is None or not received <= verified <= current):
            continue
        try:
            entry = {"workstation": station, "key": key,
                     "received_at": received.astimezone(TAIPEI_TIMEZONE).isoformat(),
                     "verified_at": verified.astimezone(TAIPEI_TIMEZONE).isoformat(), "result": result,
                     "subject": sanitize_login_text(re.sub(r"<[^>]*>|[<>\x00-\x1f]", "", subject), 240),
                     "related_key": related, "note": NOTICE_VERIFICATION_NOTES[result]}
        except OverflowError:
            continue
        identity = (station, key, received)
        if identity in entries and entries[identity] != entry:
            conflicts.add(identity)
        entries[identity] = entry
    return [entry for identity, entry in entries.items() if identity not in conflicts]


def apply_notice_verifications(rows: list[dict[str, Any]], value: Any, workstation: str,
                               *, now: datetime | None = None) -> dict[str, Any]:
    """Annotate only the derived ledger; source machine events remain unchanged."""
    entries = {(entry["key"], _heartbeat_datetime(entry["received_at"])): entry
               for entry in normalize_notice_verifications(value, now=now) if entry["workstation"] == workstation}
    applied = []
    for row in rows:
        entry = entries.get((row["key"], _heartbeat_datetime(row["received_at"])))
        if entry is None or row["read_state"] != "unknown":
            continue
        row.update(verification_result=entry["result"], verification_checked_at=entry["verified_at"],
                   verification_note=entry["note"])
        if not row["subject"]:
            row["subject"] = entry["subject"]
        if entry["result"] == "read_confirmed":
            row.update(read_state="confirmed", verification_read_label="已人工核實已讀")
        else:
            row.update(verification_read_label="原件未找到（疑似撤回／重發）", related_key=entry["related_key"])
        applied.append(entry)
    read_count = sum(entry["result"] == "read_confirmed" for entry in applied)
    missing_count = len(applied) - read_count
    return {"verification_checked_at": max((entry["verified_at"] for entry in applied), default=""),
            "verified_read_count": read_count, "original_not_found_count": missing_count,
            "verification_note": NOTICE_VERIFICATION_NOTES["original_not_found" if missing_count else "read_confirmed"] if applied else ""}


def normalize_notice_collection_report(value: Any) -> dict[str, Any]:
    """Only confirmed ledger metadata is allowed across the operational event boundary."""
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        return {}
    if not isinstance(value.get("state"), str) or value["state"] not in NOTICE_STATES:
        return {}
    try:
        run_id = _heartbeat_uuid(value.get("run_id"))
    except ValueError:
        return {}
    index, count = value.get("part_index"), value.get("part_count")
    if type(index) is not int or type(count) is not int or not 0 <= index < count <= 10000:
        return {}
    clean = {"schema_version": 1, "run_id": run_id, "part_index": index, "part_count": count, "state": value["state"]}
    dates = {}
    for field in ("started_at", "completed_at", "reported_at", "next_check_at", "shift_start", "shift_end"):
        raw = value.get(field)
        parsed = _heartbeat_datetime(raw)
        if parsed is None and not (raw == "" and field in {"started_at", "completed_at", "shift_start", "shift_end"}):
            return {}
        dates[field] = parsed
        try:
            clean[field] = parsed.astimezone(TAIPEI_TIMEZONE).isoformat(timespec="seconds") if parsed else ""
        except OverflowError:
            return {}
    reported = dates["reported_at"]
    if (value.get("calendar_day") != reported.astimezone(TAIPEI_TIMEZONE).date().isoformat()
            or dates["next_check_at"] < reported or dates["next_check_at"] > reported + timedelta(minutes=31)
            or dates["started_at"] and dates["started_at"] > reported
            or dates["completed_at"] and (dates["completed_at"] > reported or not dates["started_at"]
                                         or dates["completed_at"] < dates["started_at"])
            or value["state"] in {"running", "waiting"} and dates["completed_at"]
            or value["state"] == "success" and not dates["completed_at"]
            or bool(dates["shift_start"]) != bool(dates["shift_end"])
            or dates["shift_start"] and dates["shift_end"] <= dates["shift_start"]):
        return {}
    clean["calendar_day"] = value["calendar_day"]
    for field in NOTICE_COUNT_FIELDS:
        number = value.get(field)
        if number is not None and (type(number) is not int or not 0 <= number <= 1000000):
            return {}
        clean[field] = number
    stage, message = value.get("error_stage"), value.get("message")
    if not isinstance(stage, str) or stage not in {"", *NOTICE_REASONS} or not isinstance(message, str) or len(message) > 300:
        return {}
    clean.update(error_stage=stage, message=NOTICE_REASONS.get(stage, ""))
    code = value.get("error_code")
    if (value["state"] in {"failed", "partial", "uncertain"}
            and isinstance(code, str) and code in NOTICE_ERROR_MESSAGES):
        clean.update(error_code=code, message=NOTICE_ERROR_MESSAGES[code])
    records, new_keys = value.get("records"), value.get("new_keys")
    if not isinstance(records, list) or len(records) > 200 or not isinstance(new_keys, list):
        return {}
    rows, keys = [], set()
    for record in records:
        if not isinstance(record, dict):
            return {}
        key, subject, read_state = record.get("key"), record.get("subject"), record.get("read_state")
        received = _heartbeat_datetime(record.get("received_at"))
        if (not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None or key in keys
                or not isinstance(subject, str) or len(subject) > 240 or read_state not in ("confirmed", "pending", "unknown")
                or received is None or received > reported):
            return {}
        keys.add(key)
        subject = re.sub(r"<[^>]*>", "", subject)
        subject = sanitize_login_text(re.sub(r"[<>\x00-\x1f]", "", subject), 240)
        row = {"key": key, "subject": subject, "received_at": received.astimezone(TAIPEI_TIMEZONE).isoformat(timespec="seconds"),
               "read_state": read_state}
        if "signing_state" in record:
            signing, signed_by = record["signing_state"], record.get("signed_by")
            if (signing not in {"confirmed", "unknown"} or not isinstance(signed_by, str) or len(signed_by) > 40
                    or re.search(r"[<>\x00-\x1f]", signed_by) or (signing == "confirmed") != bool(signed_by)):
                return {}
            row.update(signing_state=signing, signed_by=signed_by)
        if "published_at" in record:
            published = _heartbeat_datetime(record["published_at"])
            if published is None or published > reported + timedelta(minutes=5):
                return {}
            row["published_at"] = published.astimezone(TAIPEI_TIMEZONE).isoformat(timespec="seconds")
        rows.append(row)
    if (any(not isinstance(key, str) or key not in keys for key in new_keys)
            or len(set(new_keys)) != len(new_keys)):
        return {}
    clean.update(records=rows, new_keys=list(new_keys))
    if "manual_records" in value:
        manual_records = value["manual_records"]
        if not isinstance(manual_records, list) or len(manual_records) > 200:
            return {}
        manual, manual_keys = [], set()
        for record in manual_records:
            if not isinstance(record, dict):
                return {}
            key, subject = record.get("key"), record.get("subject")
            published, observed = _heartbeat_datetime(record.get("published_at")), _heartbeat_datetime(record.get("observed_at"))
            if (not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None or key in keys or key in manual_keys
                    or not isinstance(subject, str) or len(subject) > 240 or record.get("read_state") != "confirmed"
                    or published is None or observed is None or observed > reported or published > observed + timedelta(minutes=5)
                    or record.get("published_date") != published.astimezone(TAIPEI_TIMEZONE).date().isoformat()):
                return {}
            manual_keys.add(key)
            manual.append({"key": key, "subject": sanitize_login_text(re.sub(r"<[^>]*>|[<>\x00-\x1f]", "", subject), 240),
                "published_at": published.astimezone(TAIPEI_TIMEZONE).isoformat(timespec="seconds"),
                "published_date": record["published_date"],
                "observed_at": observed.astimezone(TAIPEI_TIMEZONE).isoformat(timespec="seconds"), "read_state": "confirmed"})
        clean["manual_records"] = manual
    if any(field in value for field in ("scan_scope", "scan_from", "scan_to", "scan_verified_at")):
        verified = _heartbeat_datetime(value.get("scan_verified_at"))
        try:
            scan_from, scan_to = date.fromisoformat(value["scan_from"]), date.fromisoformat(value["scan_to"])
        except (KeyError, ValueError, TypeError):
            return {}
        if (value.get("scan_scope") != "publication_window" or not 0 <= (scan_to - scan_from).days <= 7
                or "manual_records" not in value
                or not 0 <= (reported.astimezone(TAIPEI_TIMEZONE).date() - scan_to).days <= 1 or verified is None or verified > reported
                or dates["started_at"] and verified < dates["started_at"]):
            return {}
        clean.update(scan_scope="publication_window", scan_from=scan_from.isoformat(), scan_to=scan_to.isoformat(),
                     scan_verified_at=verified.astimezone(TAIPEI_TIMEZONE).isoformat(timespec="seconds"))
    return clean


def _notice_groups(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = {}
    for event in events:
        if not isinstance(event, dict) or event.get("record_type") != "notice_collection":
            continue
        snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
        station = snapshot.get("workstation")
        if not isinstance(station, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", station) is None:
            continue
        report = normalize_notice_collection_report(snapshot.get("notice_collection"))
        if snapshot.get("run_id") != report.get("run_id") or event.get("status") != report.get("state"):
            report = {}
        timestamp = report.get("reported_at") or event.get("occurred_at")
        try:
            moment = _heartbeat_now(datetime.fromisoformat(str(timestamp)))
        except (ValueError, TypeError, OverflowError):
            continue
        key = (station, report.get("run_id") or str(event.get("event_id")), report.get("state"), moment)
        group = groups.setdefault(key, {"workstation": station, "at": moment, "report": report, "parts": {}, "conflict": False})
        if not report:
            continue
        shared = {k: v for k, v in report.items() if k not in {"part_index", "records", "new_keys", "manual_records"}}
        original = {k: v for k, v in group["report"].items() if k not in {"part_index", "records", "new_keys", "manual_records"}}
        part = group["parts"].get(report["part_index"])
        if shared != original or part is not None and part != report:
            group["conflict"] = True
        group["parts"][report["part_index"]] = report
    for group in groups.values():
        report, parts = group["report"], group["parts"]
        complete = bool(report) and not group["conflict"] and set(parts) == set(range(report["part_count"]))
        rows, keys, manual = [], [], []
        if complete:
            for index in sorted(parts):
                rows.extend(parts[index]["records"])
                keys.extend(parts[index]["new_keys"])
                manual.extend(parts[index].get("manual_records", []))
            complete = (len({row["key"] for row in rows}) == len(rows) and len(set(keys)) == len(keys)
                        and len({row["key"] for row in manual}) == len(manual)
                        and not {row["key"] for row in manual} & {row["key"] for row in rows}
                        and (report["new_count"] is None or report["new_count"] == len(keys)))
        group.update(complete=bool(complete), records=rows if complete else [], new_keys=keys if complete else [],
                     manual_records=manual if complete else [])
    return sorted(groups.values(), key=lambda group: (group["at"], NOTICE_STATE_PRIORITY[group["report"].get("state", "invalid")]))


def build_notice_collection_view(events: list[dict[str, Any]], calendar_day: str, *, workstation: str = "",
                                 preferred_workstation: str = "", now: datetime | None = None,
                                 verifications: Any = None) -> dict[str, Any]:
    current = _heartbeat_now(now)
    current_day = current.astimezone(TAIPEI_TIMEZONE).date()
    target = date.fromisoformat(calendar_day)
    calendar_days = {(current_day - timedelta(days=offset)).isoformat() for offset in range(7)}
    calendar_days.add(target.isoformat())
    groups = _notice_groups(events)
    daily = [group for group in groups if group["at"].astimezone(TAIPEI_TIMEZONE).date() == target]
    observed = [group for group in groups if any(row["published_date"] == calendar_day for row in group.get("manual_records", []))]
    sources = {group["workstation"] for group in daily + observed}
    requested = workstation or preferred_workstation
    has_request = bool(requested)
    if not isinstance(requested, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", requested) is None:
        requested = ""
    if requested and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", requested):
        sources.add(requested)
    selected = requested if has_request else ((daily or observed)[-1]["workstation"] if daily or observed else "")
    relevant = [group for group in groups if group["workstation"] == selected]
    today = [group for group in daily if group["workstation"] == selected]
    historical = target != current_day
    view = {"date": calendar_day, "calendar_days": sorted(calendar_days, reverse=True), "historical": historical,
            "workstation": selected, "count_known": False, "today_count": None, "shift_count": None,
            "shift_label": "", "state": "unknown", "status_label": "尚無回報", "status_class": "",
            "last_reported_at": "", "last_checked_at": "", "last_success_at": "", "last_received_at": "",
            "last_received_count": None, "next_check_at": "", "error": "", "records": [], "history": [], "parts_complete": False,
            "manual_count": 0, "authentication_label": "尚無登入驗證回報", "scan_verified_at": "", "coverage_label": "尚未取得公布日期補查回報",
            "spreadsheet_url": "/admin/sinposmart/notices/spreadsheet?notice_day=" + target.isoformat(),
            "sources": [{"workstation": source, "selected": source == selected,
                         "last_reported_at": next((group["at"].astimezone(TAIPEI_TIMEZONE).isoformat(timespec="seconds")
                                                   for group in reversed(daily) if group["workstation"] == source), "")}
                        for source in sorted(sources)]}
    latest = today[-1] if today else None
    report = latest["report"] if latest else {}
    state = report.get("state", "invalid")
    if latest:
        view.update(state=state, parts_complete=latest["complete"], last_reported_at=latest["at"].astimezone(TAIPEI_TIMEZONE).isoformat(timespec="seconds"),
                    next_check_at=report.get("next_check_at", ""), error=report.get("message", ""))
        view["status_label"], view["status_class"] = NOTICE_STATUS[state]
    ledger, history, received_count, conflicting_key, manual_ledger = {}, [], None, False, {}
    checked_groups = []
    for group in relevant:
        row_report = group["report"]
        if not group["complete"]:
            if group in today:
                history.append({"run_id": row_report.get("run_id", ""), "reported_at": group["at"].astimezone(TAIPEI_TIMEZONE).isoformat(timespec="seconds"),
                                "state": "uncertain", "status_label": "回報分片缺漏或格式不符", "status_class": "attention",
                                "new_count": None, "error": "無法確認完整通報數量。", "records": []})
            continue
        for row in group.get("manual_records", []):
            manual_ledger[row["key"]] = row
        new_rows = [row for row in group["records"] if row["key"] in group["new_keys"] and row["key"] not in ledger]
        for row in group["records"]:
            old = ledger.get(row["key"])
            if old and old["received_at"] != row["received_at"]:
                conflicting_key = True
                continue
            rank = {"unknown": 0, "pending": 1, "confirmed": 2}
            if not old or rank[row["read_state"]] >= rank[old["read_state"]]:
                ledger[row["key"]] = dict(row, subject=row["subject"] or (old or {}).get("subject", ""))
        if (group["at"].astimezone(TAIPEI_TIMEZONE).date() <= target
                and row_report["state"] in NOTICE_TERMINAL_STATES and row_report["completed_at"]
                and row_report["unread_count"] is not None and row_report["error_stage"] in {"", "write", "mark_read"}):
            checked_groups.append(group)
        if group not in today:
            continue
        if new_rows:
            received_count = len(new_rows)
        if new_rows or row_report["state"] in {"partial", "uncertain", "failed", "missing_credentials"}:
            history.append({"run_id": row_report["run_id"], "reported_at": row_report["reported_at"], "state": row_report["state"],
                            "status_label": NOTICE_STATUS[row_report["state"]][0], "status_class": NOTICE_STATUS[row_report["state"]][1],
                            "new_count": len(new_rows), "error": row_report["message"], "records": new_rows})
    for key, row in manual_ledger.items():
        if key in ledger and not ledger[key].get("published_at"):
            ledger[key]["published_at"] = row["published_at"]
    rows = sorted(ledger.values(), key=lambda row: (row["received_at"], row["key"]), reverse=True)
    start, end = _heartbeat_datetime(report.get("shift_start")), _heartbeat_datetime(report.get("shift_end"))
    daily_rows = [row for row in rows if _heartbeat_datetime(row["received_at"]).astimezone(TAIPEI_TIMEZONE).date() == target]
    visible_rows = [row for row in rows if row in daily_rows or row.get("published_at", "")[:10] == calendar_day
                    or start and end and start <= _heartbeat_datetime(row["received_at"]) < end]
    view.update(apply_notice_verifications(visible_rows, verifications, selected, now=current))
    manual_rows = [dict(row, received_at="", collection_method="manual", write_confirmed=False, write_label="手動收取",
                        read_label="網站已讀（無自動入表收據）") for key, row in manual_ledger.items()
                   if key not in ledger and row["published_date"] == calendar_day]
    view.update(last_received_at=daily_rows[0]["received_at"] if daily_rows else "", last_received_count=received_count,
                records=sorted([dict(row, write_confirmed=True, write_label="已確認入表" + (
                               " · 簽收：" + row["signed_by"] if row.get("signing_state") == "confirmed" else
                               " · 簽收待確認" if row.get("signing_state") == "unknown" else ""),
                              read_label=row.get("verification_read_label") or {"confirmed": "已確認已讀", "pending": "已讀待確認", "unknown": "已讀狀態未知"}[row["read_state"]])
                         for row in visible_rows] + manual_rows,
                         key=lambda row: (row.get("published_at") or row["received_at"], row["key"]), reverse=True),
                manual_count=len(manual_rows),
                history=list(reversed(history)))
    if historical:
        covering = [group for group in relevant if group["complete"] and group["report"].get("scan_scope") == "publication_window"
                    and group["report"]["scan_from"] <= calendar_day <= group["report"]["scan_to"]]
        if covering:
            proof = covering[-1]["report"]
            view.update(scan_verified_at=proof["scan_verified_at"], authentication_label="補查時列表身分已驗證",
                        coverage_label=f"公布日期補查：{proof['scan_from']} 至 {proof['scan_to']}（含已讀）")
    if not today:
        if historical:
            view["status_label"] = "歷史：已完成公布日期補查" if view["scan_verified_at"] else "歷史：尚無回報"
        return view
    if checked_groups:
        view["last_checked_at"] = checked_groups[-1]["report"]["completed_at"]
    successes = [group for group in checked_groups if group["report"]["state"] == "success"
                 and all(group["report"][field] is not None for field in NOTICE_COUNT_FIELDS)
                 and group["report"]["pending_write_count"] == group["report"]["pending_read_count"] == 0]
    if successes:
        view["last_success_at"] = successes[-1]["report"]["completed_at"]
    known = (latest["complete"] and not conflicting_key and any(group in today for group in checked_groups)
             and (state != "success" or all(report[field] is not None for field in NOTICE_COUNT_FIELDS)))
    view["count_known"] = bool(known)
    if start and end:
        local_start, local_end = start.astimezone(TAIPEI_TIMEZONE), end.astimezone(TAIPEI_TIMEZONE)
        view["shift_label"] = f"{local_start:%H:%M}–{local_end:%H:%M}"
        if known:
            view["shift_count"] = sum(start <= _heartbeat_datetime(row["received_at"]) < end for row in rows)
    if known:
        view["today_count"] = len(daily_rows) + len(manual_rows)
    if not latest["complete"] or conflicting_key:
        view.update(state="uncertain", status_label="回報分片缺漏或狀態待確認", status_class="attention", error="無法確認完整通報數量。")
    elif state == "success":
        unresolved = [row for row in visible_rows if row["read_state"] != "confirmed"]
        clean_poll = known and report["pending_write_count"] == report["pending_read_count"] == 0 and not report["error_stage"]
        review_current = (latest["at"] <= current + timedelta(minutes=5)
                          and (historical or current <= _heartbeat_datetime(report["next_check_at"]) + timedelta(minutes=2)))
        if clean_poll and review_current and unresolved and all(row.get("verification_result") == "original_not_found" for row in unresolved):
            view.update(state="reviewed", status_label=f"已核對，{len(unresolved)}則原件未找到", status_class="")
        elif not clean_poll or unresolved:
            view.update(state="uncertain", status_label="入表或已讀狀態待確認", status_class="attention")
        elif report.get("scan_scope") == "publication_window":
            view.update(status_label=(f"列表檢查完成（新增入表 {report['new_count']} 則）" if report.get("new_count")
                                      else "列表檢查完成（無新增入表）"), status_class="complete")
    if not historical and latest["complete"] and report.get("scan_scope") == "publication_window":
        view.update(scan_verified_at=report["scan_verified_at"], authentication_label="本輪列表身分已驗證",
                    coverage_label=f"公布日期補查：{report['scan_from']} 至 {report['scan_to']}（含已讀）")
    elif not historical and state == "failed" and report.get("error_stage") == "login":
        view["authentication_label"] = "本輪登入未完成"
    elif not historical and state == "running":
        view["authentication_label"] = "本輪登入與列表檢查中"
    if latest["at"] > current + timedelta(minutes=5):
        view.update(count_known=False, today_count=None, shift_count=None, status_label="回報時間待確認", status_class="attention")
    elif target == current.astimezone(TAIPEI_TIMEZONE).date() and report.get("next_check_at") and current > _heartbeat_datetime(report["next_check_at"]) + timedelta(minutes=2):
        view.update(status_label="超過預定檢查時間，狀態待確認", status_class="attention")
        view["authentication_label"] = "上次驗證已過期，等待新回報"
    if historical:
        view["status_label"] = "歷史：" + view["status_label"]
    return view


class SinpoSmartBackendStore:
    def __init__(self, root_dir: Path, retention_days: int = 7, *, create: bool = True) -> None:
        self.root_dir = root_dir
        self.retention_days = max(1, retention_days)
        if create:
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

    def notice_view(self, calendar_day: str | None = None, *, workstation: str = "", preferred_workstation: str = "",
                    now: datetime | None = None) -> dict[str, Any]:
        try:
            target = date.fromisoformat(calendar_day) if isinstance(calendar_day, str) else None
        except ValueError:
            target = None
        target = target or _heartbeat_now(now).astimezone(TAIPEI_TIMEZONE).date()
        events = []
        # Civil days straddle fire-day files; the three-day publication scan may report two days later.
        with self._lock:
            try:
                verifications = json.loads((self.root_dir / NOTICE_VERIFICATION_FILENAME).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                verifications = None
            for offset in (-1, 0, 1, 2):
                try:
                    path = self.path_for_day((target + timedelta(days=offset)).isoformat())
                except OverflowError:
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                rows = payload.get("events") if isinstance(payload, dict) else None
                if isinstance(rows, list):
                    events.extend(row for row in rows if isinstance(row, dict))
        return build_notice_collection_view(events, target.isoformat(), workstation=workstation,
                                            preferred_workstation=preferred_workstation, now=now, verifications=verifications)

    def list_days(self, limit: int = 7, now: datetime | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self.cleanup(now)
            days: list[dict[str, Any]] = []
            for path in sorted(self.root_dir.glob("*.json"), reverse=True):
                if path.name == NOTICE_VERIFICATION_FILENAME:
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    payload["events"] = compact_sinposmart_events(payload.get("events") or [])
                    payload["summary"] = summarize_sinposmart_events(payload.get("events") or [])
                    payload["admin_view"] = build_sinposmart_admin_view(
                        payload["events"], now=now, related_results=self._related_submission_results(payload["events"])
                    )
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
            payload["admin_view"] = build_sinposmart_admin_view(
                payload["events"], now=now, related_results=self._related_submission_results(payload["events"])
            )
            return payload

    def _related_submission_results(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        local_result_keys = {sinposmart_action_group_key(event) for event in events if event.get("record_type") == "action_result"}
        queued_events: dict[tuple[str, ...], dict[str, Any]] = {}
        for event in events:
            key = sinposmart_action_group_key(event)
            if event.get("record_type") == "action_queued" and key[0] == "completion_key" and key not in local_result_keys:
                queued_events[key] = newer_sinposmart_event(queued_events.get(key), event)
        keys = {
            sinposmart_action_group_key(event)
            for event in events
            if event.get("record_type") == "action_result" and event.get("status") == "skipped_duplicate"
        }
        keys = {key for key in keys if key[0] == "completion_key"}
        if not keys and not queued_events:
            return []
        results: list[dict[str, Any]] = []
        for path in self.root_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
                continue
            for event in payload["events"]:
                if not isinstance(event, dict) or event.get("record_type") != "action_result":
                    continue
                key = sinposmart_action_group_key(event)
                if key in keys and sinposmart_action_status_label(str(event.get("status") or ""), "action_result") == "已登打":
                    results.append(event)
                elif key in queued_events and sinposmart_following_day_result_matches(queued_events[key], event):
                    results.append(event)
        return results

    def cleanup(self, now: datetime | None = None) -> None:
        with self._lock:
            current_day = date.fromisoformat(sinposmart_fire_day_for(now))
            cutoff = current_day - timedelta(days=self.retention_days - 1)
            for path in self.root_dir.glob("*.json"):
                if path.name == NOTICE_VERIFICATION_FILENAME:
                    continue
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
    raw_snapshot = raw_event.get("snapshot")
    snapshot = {} if record_type == "notice_collection" else sanitize_value(raw_snapshot, depth=0)
    if isinstance(raw_snapshot, dict) and isinstance(snapshot, dict):
        completion_key = sinposmart_completion_key_identity(raw_snapshot.get("completion_key"))
        if completion_key:
            snapshot["completion_key_sha256"] = hashlib.sha256(completion_key.encode("utf-8")).hexdigest()
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
        "snapshot": snapshot,
    }
    event["merged_event_ids"] = [event["event_id"]]
    if isinstance(snapshot, dict):
        snapshot.pop("personnel_titles", None)
        if record_type == "schedule_snapshot" and isinstance(raw_snapshot, dict) and "personnel_titles" in raw_snapshot:
            personnel_titles = _personnel_title_snapshot(raw_snapshot["personnel_titles"], allow_incomplete=True)
            if personnel_titles:
                try:
                    datetime.fromisoformat(str(raw_event.get("occurred_at") or ""))
                except (ValueError, TypeError, OverflowError):
                    personnel_titles = {**personnel_titles, "complete": False, "members": []}
                snapshot["personnel_titles"] = personnel_titles
    if isinstance(raw_snapshot, dict) and "daily_vehicle_report" in raw_snapshot:
        event["snapshot"]["daily_vehicle_report"] = sanitize_daily_vehicle_report(
            raw_snapshot["daily_vehicle_report"], expected_date=sinposmart_daily_vehicle_target_date(event)
        )
    if record_type == "login_failed" or (record_type == "error" and event["trigger_type"] == "login"):
        event["content"] = sanitize_login_text(raw_event.get("content"), 1200)
        event["error"] = sanitize_login_text(raw_event.get("error"), 1200)
    if record_type == "tool_action_started" and not event["item_title"]:
        snapshot = event["snapshot"] if isinstance(event.get("snapshot"), dict) else {}
        tool_label = sanitize_scalar(snapshot.get("tool_label"), 120)
        if tool_label:
            event["item_title"] = f"開始{tool_label}"
    if record_type == "notice_collection":
        raw_snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        report = normalize_notice_collection_report(raw_snapshot.get("notice_collection"))
        station = raw_snapshot.get("workstation")
        station = station if isinstance(station, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", station) else ""
        version = raw_snapshot.get("app_version")
        version = version if isinstance(version, str) and re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d{4}", version) else ""
        if raw_snapshot.get("run_id") != report.get("run_id") or event["status"] != report.get("state"):
            report = {}
        event["snapshot"] = {"app_version": version, "workstation": station,
                             "run_id": report.get("run_id", ""), "notice_collection": report}
        event.update(status=report.get("state", "failed"), trigger_type="notice_poll", item_title="已收通報", source="SinpoSmart", item_kind="notice_collection",
                     actor_no="", user_id="", display_name="", content="", target="", target_time="", result_ref="",
                     error="" if report else "通報回報格式不符，狀態待確認。")
        event["fire_day"] = sinposmart_fire_day_for(occurred_at)
        if report:
            event.update(occurred_at=report["reported_at"], first_occurred_at=report["reported_at"], last_occurred_at=report["reported_at"])
            event["fire_day"] = sinposmart_fire_day_for(datetime.fromisoformat(report["reported_at"]))
    return {field: event[field] for field in SINPOSMART_EVENT_FIELDS}


def sinposmart_event_merge_key(event: dict[str, Any]) -> tuple[str, ...]:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    tool_label = sanitize_scalar(snapshot.get("tool_label"), 120) if snapshot else ""
    queue_id = sanitize_scalar(snapshot.get("queue_id"), 120) if snapshot else ""
    run_id = sanitize_scalar(snapshot.get("run_id"), 120) if snapshot else ""
    key = (
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
    personnel_titles = (_personnel_title_snapshot(snapshot.get("personnel_titles"), allow_incomplete=True)
                        if event.get("record_type") == "schedule_snapshot" else {})
    if personnel_titles:
        fingerprint = json.dumps(_personnel_title_snapshot_key(personnel_titles), ensure_ascii=False)
        return (*key, hashlib.sha256(fingerprint.encode("utf-8")).hexdigest())
    return key


def sinposmart_event_keeps_individual_record(event: dict[str, Any]) -> bool:
    return sinposmart_is_login_event(event) or str(event.get("record_type") or "") in {
        "tool_action_started",
        "tool_action_finished",
        "unreturned_return",
        "notice_collection",
    }


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
    if (existing.get("record_type") == event.get("record_type") == "schedule_snapshot"
            and isinstance(existing.get("snapshot"), dict) and isinstance(event.get("snapshot"), dict)
            and _personnel_title_snapshot(existing["snapshot"].get("personnel_titles"), allow_incomplete=True)
            and _personnel_title_snapshot(event["snapshot"].get("personnel_titles"), allow_incomplete=True)):
        try:
            existing_at = taipei_local_datetime(datetime.fromisoformat(sinposmart_event_time(existing)))
            incoming_at = taipei_local_datetime(datetime.fromisoformat(sinposmart_event_time(event)))
        except (TypeError, ValueError, OverflowError):
            pass
        else:
            if existing_at > incoming_at:
                merged["last_occurred_at"] = sinposmart_event_time(existing)
                merged["occurred_at"] = merged["last_occurred_at"]
                merged["snapshot"] = existing["snapshot"]
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
            is_legacy_merged_tool_event = (
                str(event.get("record_type") or "") in {"tool_action_started", "tool_action_finished"}
                and len(event["merged_event_ids"]) > 1
            )
            event["repeat_count"] = (
                max(event_repeat_count(event), len(event["merged_event_ids"]))
                if is_legacy_merged_tool_event
                else 1
            )
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


def sanitize_login_url(value: Any) -> str:
    if not isinstance(value, str) or re.search(r'''[\s\\\x00-\x1f\x7f<>"']''', value.strip()):
        return ""
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        if parts.port is not None:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme.lower(), host, parts.path, "", ""))[:320]
    except ValueError:
        return ""


def sanitize_login_text(value: Any, max_length: int) -> str:
    text = value.replace("\x00", "").strip() if isinstance(value, str) else ""
    text = re.sub(r'''https?://[^\s<>"'，；]+''', lambda match: sanitize_login_url(match.group()), text, flags=re.I)
    text = LOGIN_SENSITIVE_TEXT_PATTERN.sub(r"\1=***", text)
    return text[:max_length]


def sanitize_login_png(value: Any, redacted: Any) -> tuple[str, str]:
    if redacted is not True or not isinstance(value, str) or not value:
        return "unavailable", ""
    if len(value) > ((512 * 1024 + 2) // 3) * 4:
        return "oversize", ""
    try:
        data = base64.b64decode(value, validate=True)
        if len(data) > 512 * 1024:
            return "oversize", ""
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "unavailable", ""
        position, header, compressed, chunks, ended = 8, None, [], [], False
        palette_size, transparency_seen, image_ended = 0, False, False
        while position + 12 <= len(data):
            length = struct.unpack_from(">I", data, position)[0]
            kind = data[position + 4:position + 8]
            end = position + 12 + length
            if end > len(data) or not re.fullmatch(b"[A-Za-z]{4}", kind):
                return "unavailable", ""
            content = data[position + 8:end - 4]
            if zlib.crc32(kind + content) != struct.unpack_from(">I", data, end - 4)[0]:
                return "unavailable", ""
            if kind in {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}:
                chunks.append(data[position:end])
            if compressed and kind != b"IDAT":
                image_ended = True
            if header is None:
                if kind != b"IHDR" or length != 13:
                    return "unavailable", ""
                header = struct.unpack(">IIBBBBB", content)
                width, height, bit_depth, color, compression, filtering, interlace = header
                if not width or not height or width > 4096 or height > 4096 or width * height > 4194304:
                    return "oversize", ""
                depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
                if bit_depth not in depths.get(color, set()) or compression or filtering or interlace not in {0, 1}:
                    return "unavailable", ""
            elif kind == b"IHDR":
                return "unavailable", ""
            elif kind == b"PLTE":
                if compressed or palette_size or color in {0, 4} or not length or length % 3 or length > 768:
                    return "unavailable", ""
                palette_size = length // 3
                if color == 3 and palette_size > 2 ** bit_depth:
                    return "unavailable", ""
            elif kind == b"tRNS":
                if compressed or transparency_seen or color not in {0, 2, 3}:
                    return "unavailable", ""
                if (color == 0 and length != 2) or (color == 2 and length != 6) or (color == 3 and not 0 < length <= palette_size):
                    return "unavailable", ""
                transparency_seen = True
            elif kind == b"IDAT":
                if image_ended or (color == 3 and not palette_size):
                    return "unavailable", ""
                compressed.append(content)
            elif kind == b"IEND":
                ended = length == 0 and end == len(data)
                break
            elif not kind[0] & 32 and kind != b"PLTE":
                return "unavailable", ""
            position = end
        if not ended or not compressed or header is None:
            return "unavailable", ""
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        passes = [(0, 0, 1, 1)] if not interlace else [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
        rows = []
        for x, y, dx, dy in passes:
            columns = max(0, (width - x + dx - 1) // dx)
            count = max(0, (height - y + dy - 1) // dy)
            if columns and count:
                rows.extend([1 + (columns * channels * bit_depth + 7) // 8] * count)
        expected = sum(rows)
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(b"".join(compressed), expected + 1)
        if len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            return "unavailable", ""
        offset = 0
        for row_length in rows:
            if pixels[offset] > 4:
                return "unavailable", ""
            offset += row_length
        return "captured", base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"".join(chunks)).decode("ascii")
    except (ValueError, binascii.Error, struct.error, zlib.error):
        return "unavailable", ""


def sanitize_login_diagnostic(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    codes = {"password_change_required", "invalid_credentials", "login_failed", "browser_start_failed", "login_timeout"}
    reason_code = value.get("reason_code")
    clean = {
        "reason_code": reason_code if isinstance(reason_code, str) and reason_code in codes else "login_failed",
        "stage": "post_login" if value.get("stage") == "post_login" else "login",
        "reason": sanitize_login_text(value.get("reason"), 600),
        "page_url": sanitize_login_url(value.get("page_url")),
        "page_title": sanitize_login_text(value.get("page_title"), 160),
        "page_text": sanitize_login_text(value.get("page_text"), 4000),
        "captured_at": sanitize_login_text(value.get("captured_at"), 40),
        "image_redacted": value.get("image_redacted") is True,
    }
    raw_image = value.get("image_png")
    capture_status, image = sanitize_login_png(raw_image, value.get("image_redacted"))
    clean["capture_status"] = capture_status
    if image:
        clean["image_png"] = image
        clean.update(capture_error_code="", capture_error="", capture_error_type="")
    else:
        code = value.get("capture_error_code")
        code = code if isinstance(code, str) and code in LOGIN_CAPTURE_ERRORS else ""
        server_rejected = (raw_image is not None and raw_image != "") or value.get("capture_status") == "captured"
        if capture_status == "oversize":
            code = "image_oversize"
        elif server_rejected:
            code = "server_image_rejected"
        elif value.get("capture_status") == "oversize":
            code = "image_oversize"
            clean["capture_status"] = "oversize"
        if code:
            error_type = value.get("capture_error_type")
            if server_rejected or code != value.get("capture_error_code") or not isinstance(error_type, str) or error_type not in LOGIN_CAPTURE_ERROR_TYPES:
                error_type = ""
            clean.update(capture_error_code=code, capture_error=LOGIN_CAPTURE_ERRORS[code], capture_error_type=error_type)
    return clean


def sinposmart_daily_vehicle_target_date(event: dict[str, Any]) -> str:
    snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    raw = snapshot.get("target_date") if "target_date" in snapshot else str(event.get("occurred_at") or "").split("T")[0]
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    roc = re.fullmatch(r"(\d{3})[/-](\d{1,2})[/-](\d{1,2})", text)
    if roc:
        text = f"{int(roc[1]) + 1911:04d}-{int(roc[2]):02d}-{int(roc[3]):02d}"
    parsed = parse_sinposmart_snapshot_date(text)
    return parsed.isoformat() if parsed else ""


def sanitize_daily_vehicle_report(value: Any, *, expected_date: str | None = None) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    invalid = not isinstance(value, dict)
    target_date = raw.get("target_date")
    parsed_date = parse_sinposmart_snapshot_date(target_date) if isinstance(target_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date) else None
    target_date = parsed_date.isoformat() if parsed_date else ""
    mode = raw.get("execution_mode")
    mode = mode if isinstance(mode, str) and mode in {"manual", "automatic"} else ""
    reason = raw.get("reason_code", "")
    if not isinstance(reason, str) or reason not in {"", *DAILY_VEHICLE_REASONS} or reason == "not_required":
        reason, invalid = "report_invalid", True
    if not target_date or not mode or not isinstance(raw.get("verified"), bool) or (expected_date is not None and target_date != expected_date):
        invalid = True
    rows = raw.get("items")
    if not isinstance(rows, list):
        rows, invalid = [], True
    oversize = len(rows) > 200
    items, seen = [], set()
    positive = {"verified_today", "saved_verified"}
    for row in rows[:200]:
        if not isinstance(row, dict) or row.get("operation") not in ("maintenance", "equipment"):
            invalid = True
            continue
        vehicle = row.get("vehicle")
        safe_vehicle = isinstance(vehicle, str) and len(vehicle) <= 32 and re.fullmatch(r"[A-Za-z0-9\u3400-\u9fff -]*", vehicle) and not re.search(r"secret|token|password|cookie|account|username|帳號|密碼", vehicle, re.I)
        vehicle = " ".join(vehicle.split()) if safe_vehicle else ""
        status, row_reason = row.get("status"), row.get("reason_code")
        if not safe_vehicle or status not in ("completed", "already_done", "not_required", "unconfirmed") or not isinstance(row_reason, str) or row_reason not in DAILY_VEHICLE_REASONS:
            status, row_reason, invalid = "unconfirmed", "report_invalid", True
        if ((status in {"completed", "already_done"} and (not vehicle or row_reason not in positive))
                or (status == "unconfirmed" and row_reason in positive)
                or (status == "not_required" and (not vehicle or row["operation"] != "maintenance" or row_reason != "not_required"))
                or (status != "not_required" and row_reason == "not_required")):
            status, row_reason, invalid = "unconfirmed", "report_invalid", True
        key = (vehicle.casefold(), row["operation"])
        if key in seen:
            invalid = True
        seen.add(key)
        items.append({"vehicle": vehicle, "operation": row["operation"], "status": status, "reason_code": row_reason})
    operations = {row["operation"] for row in items}
    if raw.get("verified") is True and items and (operations != {"maintenance", "equipment"} or reason not in {"", *positive}):
        invalid = True
    verified = raw.get("verified") is True and not invalid and not oversize and bool(items) and operations == {"maintenance", "equipment"} and all(row["vehicle"] and row["status"] in {"completed", "already_done", "not_required"} for row in items)
    if oversize:
        reason = "row_limit"
    elif invalid:
        reason = "report_invalid"
    elif not items and reason in {"", *positive}:
        reason = "empty_list"
    elif not verified and reason in {"", *positive}:
        reason = next((row["reason_code"] for row in items if row["status"] == "unconfirmed"), "execution_unconfirmed")
    return {"target_date": target_date, "execution_mode": mode, "verified": bool(verified), "reason_code": reason, "items": items}


def sanitize_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return ""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SENSITIVE_KEY_PATTERN.search(key_text):
                continue
            if key_text == "login_diagnostic":
                clean[key_text] = sanitize_login_diagnostic(item)
            elif key_text == "daily_vehicle_report":
                clean[key_text] = sanitize_daily_vehicle_report(item)
            else:
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
