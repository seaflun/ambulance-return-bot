from __future__ import annotations

import hashlib
from typing import Any


HOME_RESCUE_UNIT = "大園救護分隊"
EXCLUDED_TITLE_TOKEN = "顧問"
ROSTER_LOADED_STATUS = "civilpower_roster_loaded"
MAX_ROSTER_MEMBERS = 200


def roster_member_id(unit: object, title: object, name: object) -> str:
    identity = "\x1f".join(_clean_text(value) for value in (unit, title, name))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"civilpower-{digest}"


def normalize_roster_members(raw_members: object) -> list[dict[str, str]]:
    if not isinstance(raw_members, list):
        return []
    normalized: list[dict[str, str]] = []
    seen_member_ids: set[str] = set()
    for raw_member in raw_members:
        if not isinstance(raw_member, dict):
            continue
        unit = _clean_text(raw_member.get("unit"), limit=80)
        title = _clean_text(raw_member.get("title"), limit=80)
        name = _clean_text(raw_member.get("name"), limit=80)
        if unit != HOME_RESCUE_UNIT or not name or EXCLUDED_TITLE_TOKEN in title:
            continue
        member_id = roster_member_id(unit, title, name)
        if member_id in seen_member_ids:
            continue
        seen_member_ids.add(member_id)
        normalized.append(
            {
                "member_id": member_id,
                "unit": unit,
                "title": title,
                "name": name,
            }
        )
        if len(normalized) >= MAX_ROSTER_MEMBERS:
            break
    return sorted(normalized, key=lambda member: (member["name"], member["title"], member["unit"]))


def merge_roster_report(existing: object, report: object) -> dict[str, Any]:
    previous = dict(existing) if isinstance(existing, dict) else {}
    current = dict(report) if isinstance(report, dict) else {}
    previous_members = normalize_roster_members(previous.get("members"))
    status = _clean_text(current.get("status"), limit=80) or "civilpower_roster_failed"
    detail = _clean_text(current.get("detail"), limit=500)
    attempted_at = _clean_text(
        current.get("attempted_at") or current.get("updated_at"),
        limit=40,
    )
    source = _clean_text(current.get("source"), limit=80) or _clean_text(previous.get("source"), limit=80)
    current_members = normalize_roster_members(current.get("members"))
    if status == ROSTER_LOADED_STATUS and current_members:
        last_success_at = _clean_text(
            current.get("last_success_at") or attempted_at or current.get("updated_at"),
            limit=40,
        )
        return {
            "status": ROSTER_LOADED_STATUS,
            "detail": detail or f"已更新 {len(current_members)} 位可選義消。",
            "source": source or "public_duty_pc_worker",
            "last_attempted_at": attempted_at or last_success_at,
            "last_success_at": last_success_at,
            "member_count": len(current_members),
            "members": current_members,
        }
    return {
        "status": status,
        "detail": detail or "義消名冊更新失敗，保留上次成功名冊。",
        "source": source or "public_duty_pc_worker",
        "last_attempted_at": attempted_at,
        "last_success_at": _clean_text(previous.get("last_success_at"), limit=40),
        "member_count": len(previous_members),
        "members": previous_members,
    }


def roster_member_by_id(snapshot: object, member_id: object) -> dict[str, str] | None:
    target = _clean_text(member_id, limit=80)
    if not target or not isinstance(snapshot, dict):
        return None
    for member in normalize_roster_members(snapshot.get("members")):
        if member["member_id"] == target:
            return member
    return None


def _clean_text(value: object, *, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]
