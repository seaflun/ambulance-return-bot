from __future__ import annotations

from collections.abc import Mapping

from ambulance_bot.daily_vehicle_mileage import vehicle_mileage_key


MANUAL_REFRESH_KIND_VEHICLE_MILEAGE = "vehicle_mileage"
MANUAL_REFRESH_KIND_CIVILPOWER_ROSTER = "civilpower_roster"
MANUAL_REFRESH_STATUS_PENDING = "pending"
MANUAL_REFRESH_STATUS_COMPLETED = "completed"
MANUAL_REFRESH_STATUS_FAILED = "failed"

MANUAL_REFRESH_KINDS = {
    MANUAL_REFRESH_KIND_VEHICLE_MILEAGE,
    MANUAL_REFRESH_KIND_CIVILPOWER_ROSTER,
}
MANUAL_REFRESH_STATUSES = {
    MANUAL_REFRESH_STATUS_PENDING,
    MANUAL_REFRESH_STATUS_COMPLETED,
    MANUAL_REFRESH_STATUS_FAILED,
}


def manual_refresh_command_key(kind: object, vehicle_key: object = "") -> str:
    normalized_kind = str(kind or "").strip()
    if normalized_kind == MANUAL_REFRESH_KIND_VEHICLE_MILEAGE:
        key = vehicle_mileage_key(vehicle_key)
        return f"{normalized_kind}:{key}" if key else ""
    if normalized_kind == MANUAL_REFRESH_KIND_CIVILPOWER_ROSTER:
        return normalized_kind
    return ""


def normalize_manual_refresh_command(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    kind = str(value.get("kind") or "").strip()
    request_id = _clean_text(value.get("request_id"), limit=80)
    status = str(value.get("status") or "").strip().lower()
    vehicle_key = vehicle_mileage_key(value.get("vehicle_key"))
    if kind not in MANUAL_REFRESH_KINDS or not request_id or status not in MANUAL_REFRESH_STATUSES:
        return None
    if kind == MANUAL_REFRESH_KIND_VEHICLE_MILEAGE:
        if not vehicle_key:
            return None
    else:
        vehicle_key = ""
    return {
        "request_id": request_id,
        "kind": kind,
        "vehicle_key": vehicle_key,
        "status": status,
        "requested_at": _clean_text(value.get("requested_at"), limit=40),
        "completed_at": _clean_text(value.get("completed_at"), limit=40),
        "detail": _clean_text(value.get("detail"), limit=500),
    }


def manual_refresh_command_is_active(command: Mapping[str, object] | None) -> bool:
    return bool(command) and str(command.get("status") or "") == MANUAL_REFRESH_STATUS_PENDING


def _clean_text(value: object, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]
