from __future__ import annotations

from .models import AmbulanceReturnRequest


class ManualUpdateRequiredError(RuntimeError):
    """An existing official record cannot be changed safely by automation."""


def _vehicle_key(request: AmbulanceReturnRequest, index: int) -> str:
    return str(request.vehicle or "").strip() or f"{index}車"


def _update_vehicle_requests(
    update_context: dict[str, object] | None,
) -> tuple[list[AmbulanceReturnRequest], list[AmbulanceReturnRequest]]:
    if not isinstance(update_context, dict):
        return [], []
    previous_task = update_context.get("previous_task")
    current_task = update_context.get("current_task")
    if not isinstance(previous_task, dict) or not isinstance(current_task, dict):
        return [], []
    return (
        AmbulanceReturnRequest.from_dict(previous_task).vehicle_requests(),
        AmbulanceReturnRequest.from_dict(current_task).vehicle_requests(),
    )


def removed_official_vehicle_keys(
    site_key: str,
    update_context: dict[str, object] | None,
) -> list[str]:
    previous, current = _update_vehicle_requests(update_context)
    previous_by_key = {_vehicle_key(request, index): request for index, request in enumerate(previous, start=1)}
    current_by_key = {_vehicle_key(request, index): request for index, request in enumerate(current, start=1)}
    if site_key == "fuel_record":
        return [
            key
            for key, request in previous_by_key.items()
            if request.fuel_record.enabled
            and (key not in current_by_key or not current_by_key[key].fuel_record.enabled)
        ]
    if site_key in {"duty_work_log", "vehicle_mileage", "consumables", "disinfection"}:
        return [key for key in previous_by_key if key not in current_by_key]
    return []


def _case_identity(request: AmbulanceReturnRequest, *, include_case_id: bool) -> tuple[str, ...]:
    date_text = request.service_case_date().strftime("%Y%m%d")
    time_text = "".join(character for character in str(request.case_time or "") if character.isdigit())[:4]
    identity = (date_text, time_text)
    if include_case_id:
        return (str(request.case_id or "").strip(),) + identity
    return identity


def previous_vehicle_request_for_update(
    update_context: dict[str, object] | None,
    current_request: AmbulanceReturnRequest,
) -> AmbulanceReturnRequest | None:
    if not isinstance(update_context, dict):
        return None
    previous_task = update_context.get("previous_task")
    if not isinstance(previous_task, dict):
        return None
    previous_requests = AmbulanceReturnRequest.from_dict(previous_task).vehicle_requests()
    if not previous_requests:
        return None
    if len(previous_requests) == 1:
        return previous_requests[0]

    current_vehicle = str(current_request.vehicle or "").strip()
    matches = [item for item in previous_requests if str(item.vehicle or "").strip() == current_vehicle]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ManualUpdateRequiredError(
            f"manual correction required: ambiguous previous vehicle {current_vehicle or 'empty'}"
        )
    try:
        vehicle_index = int(update_context.get("vehicle_index") or 0)
    except (TypeError, ValueError):
        vehicle_index = 0
    if 1 <= vehicle_index <= len(previous_requests):
        return previous_requests[vehicle_index - 1]
    raise ManualUpdateRequiredError(
        f"manual correction required: missing stable previous vehicle identity "
        f"vehicle={current_vehicle or 'empty'} index={vehicle_index or 'empty'}"
    )


def manual_update_reason(
    site_key: str,
    current_request: AmbulanceReturnRequest,
    update_context: dict[str, object] | None,
) -> str:
    if not isinstance(update_context, dict) or not isinstance(update_context.get("previous_task"), dict):
        return ""
    removed_vehicle_keys = removed_official_vehicle_keys(site_key, update_context)
    if removed_vehicle_keys:
        return (
            "已有官方紀錄對應的車輛已移除或取消："
            + "、".join(removed_vehicle_keys)
            + "；請到官方網頁人工刪除舊資料，並人工新增或更新所有現行車輛資料。"
        )
    if site_key == "duty_work_log":
        return "已存的消防勤務工作紀錄不能安全自動修改，請人工更新。"

    try:
        previous = previous_vehicle_request_for_update(update_context, current_request)
    except (TypeError, ValueError, ManualUpdateRequiredError) as exc:
        return str(exc)
    if previous is None:
        return ""

    previous_vehicle = str(previous.vehicle or "").strip()
    current_vehicle = str(current_request.vehicle or "").strip()
    if site_key in {"vehicle_mileage", "fuel_record", "consumables", "disinfection"}:
        if previous_vehicle != current_vehicle:
            return (
                f"{site_key} vehicle change requires manual correction: "
                f"previous={previous_vehicle or 'empty'} current={current_vehicle or 'empty'}"
            )

    if site_key == "consumables":
        previous_case_identity = _case_identity(previous, include_case_id=True)
        current_case_identity = _case_identity(current_request, include_case_id=True)
        if previous_case_identity != current_case_identity:
            return (
                "耗材紀錄的 TEMSIS 案件識別已變更，舊頁無法安全定位，請人工更新："
                f"previous={previous_case_identity} current={current_case_identity}"
            )

    if site_key == "fuel_record" and previous.fuel_record.enabled:
        previous_period = str(previous.fuel_record.date or "")[:6]
        current_period = str(current_request.fuel_record.date or "")[:6]
        if previous_period != current_period:
            return (
                "fuel period change requires manual correction: "
                f"previous={previous_period or 'empty'} current={current_period or 'empty'}"
            )

    if site_key == "disinfection":
        previous_case_key = _case_identity(previous, include_case_id=False)
        current_case_key = _case_identity(current_request, include_case_id=False)
        if previous_case_key != current_case_key:
            return (
                "消毒紀錄案件日期或時間已變更，舊列無法安全定位，請人工更新："
                f"previous={previous_case_key[0]} {previous_case_key[1]} "
                f"current={current_case_key[0]} {current_case_key[1]}"
            )
        previous_items = {str(item).strip() for item in previous.disinfection_items if str(item).strip()}
        current_items = {str(item).strip() for item in current_request.disinfection_items if str(item).strip()}
        removed_items = sorted(previous_items - current_items)
        if removed_items:
            return "消毒項目有取消勾選，現有網頁無法驗證清除舊值，請人工更新：" + "、".join(removed_items)

    return ""


def require_safe_automated_update(
    site_key: str,
    current_request: AmbulanceReturnRequest,
    update_context: dict[str, object] | None,
) -> None:
    reason = manual_update_reason(site_key, current_request, update_context)
    if reason:
        raise ManualUpdateRequiredError(reason)
