from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any


DAILY_VEHICLE_MILEAGE_SYNCED = "daily_vehicle_mileage_synced"
DAILY_VEHICLE_MILEAGE_NO_RECORD = "daily_vehicle_mileage_no_record"
DAILY_VEHICLE_MILEAGE_FAILED = "daily_vehicle_mileage_failed"
DAILY_VEHICLE_MILEAGE_SYNC_HOUR = 6
DAILY_VEHICLE_MILEAGE_RETRY_SECONDS = 30 * 60
_TERMINAL_STATUSES = {
    DAILY_VEHICLE_MILEAGE_SYNCED,
    DAILY_VEHICLE_MILEAGE_NO_RECORD,
}


def vehicle_mileage_key(value: object) -> str:
    return " ".join(str(value or "").split()).upper()


def daily_vehicle_targets(settings: Mapping[str, object] | None) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    source = settings or {}
    for collection_name in ("ems_vehicles", "disaster_vehicles"):
        records = source.get(collection_name)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, Mapping):
                continue
            ppe_name = vehicle_mileage_key(record.get("ppe_name"))
            label = _clean_text(record.get("label"), limit=80)
            if not ppe_name or not label:
                continue
            target = grouped.setdefault(
                ppe_name,
                {"vehicle_key": ppe_name, "ppe_name": ppe_name, "labels": []},
            )
            labels = target["labels"]
            if isinstance(labels, list) and label not in labels:
                labels.append(label)
    return [grouped[key] for key in sorted(grouped)]


def unavailable_vehicle_mileage_snapshot() -> dict[str, object]:
    return {
        "source": "public_duty_pc_worker",
        "last_attempted_at": "",
        "vehicles": [],
    }


def normalize_vehicle_mileage_snapshot(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, Mapping):
        return None
    records = payload.get("vehicles")
    normalized_records: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    if isinstance(records, list):
        for raw_record in records:
            normalized = _normalize_vehicle_record(raw_record)
            if normalized is None or normalized["vehicle_key"] in seen_keys:
                continue
            seen_keys.add(str(normalized["vehicle_key"]))
            normalized_records.append(normalized)
    normalized_records.sort(key=lambda item: str(item["vehicle_key"]))
    return {
        "source": _clean_text(payload.get("source"), limit=80) or "public_duty_pc_worker",
        "last_attempted_at": _clean_text(payload.get("last_attempted_at"), limit=40),
        "vehicles": normalized_records,
    }


def merge_vehicle_mileage_report(existing: object, report: object) -> dict[str, object]:
    snapshot = normalize_vehicle_mileage_snapshot(existing) or unavailable_vehicle_mileage_snapshot()
    if not isinstance(report, Mapping):
        return snapshot
    records_by_key = {
        str(record["vehicle_key"]): dict(record)
        for record in snapshot["vehicles"]
        if isinstance(record, Mapping)
    }
    attempted_at = _clean_text(report.get("attempted_at"), limit=40)
    business_date = _clean_text(report.get("business_date"), limit=20)
    source = _clean_text(report.get("source"), limit=80) or str(snapshot["source"])
    reports = report.get("vehicles")
    if isinstance(reports, list):
        for raw_report in reports:
            current = _normalize_vehicle_record(raw_report)
            if current is None:
                continue
            key = str(current["vehicle_key"])
            previous = records_by_key.get(key, {})
            status = str(current["status"])
            merged = {
                **previous,
                "vehicle_key": key,
                "ppe_name": str(current["ppe_name"]),
                "labels": _merge_labels(previous.get("labels"), current.get("labels")),
                "status": status,
                "detail": str(current["detail"]),
                "last_attempted_at": attempted_at or str(current["last_attempted_at"]),
            }
            if status == DAILY_VEHICLE_MILEAGE_SYNCED and str(current["mileage"]):
                merged.update(
                    mileage=str(current["mileage"]),
                    record_end_date=str(current["record_end_date"]),
                    record_end_time=str(current["record_end_time"]),
                    last_success_at=attempted_at or str(current["last_success_at"]),
                    last_success_business_date=business_date,
                )
            elif status == DAILY_VEHICLE_MILEAGE_NO_RECORD:
                merged["last_success_business_date"] = business_date
            records_by_key[key] = _normalize_vehicle_record(merged) or merged
    return {
        "source": source,
        "last_attempted_at": attempted_at or str(snapshot["last_attempted_at"]),
        "vehicles": [records_by_key[key] for key in sorted(records_by_key)],
    }


def vehicle_mileage_sync_due(
    snapshot: object,
    target: Mapping[str, object],
    *,
    now: datetime | None = None,
    retry_seconds: int = DAILY_VEHICLE_MILEAGE_RETRY_SECONDS,
) -> bool:
    current = now or datetime.now()
    if current.hour < DAILY_VEHICLE_MILEAGE_SYNC_HOUR:
        return False
    vehicle_key = vehicle_mileage_key(target.get("vehicle_key") or target.get("ppe_name"))
    if not vehicle_key:
        return False
    normalized = normalize_vehicle_mileage_snapshot(snapshot) or unavailable_vehicle_mileage_snapshot()
    record = next(
        (
            item
            for item in normalized["vehicles"]
            if isinstance(item, Mapping) and str(item.get("vehicle_key") or "") == vehicle_key
        ),
        None,
    )
    if not isinstance(record, Mapping):
        return True
    business_date = current.date().isoformat()
    if (
        str(record.get("status") or "") in _TERMINAL_STATUSES
        and str(record.get("last_success_business_date") or "") == business_date
    ):
        return False
    attempted_at = _parse_datetime(record.get("last_attempted_at"))
    if attempted_at is None:
        return True
    elapsed_seconds = (current - attempted_at).total_seconds()
    return elapsed_seconds >= max(int(retry_seconds), 60)


def vehicle_mileage_snapshot_record(snapshot: object, ppe_name: object) -> dict[str, object] | None:
    key = vehicle_mileage_key(ppe_name)
    if not key:
        return None
    normalized = normalize_vehicle_mileage_snapshot(snapshot)
    if normalized is None:
        return None
    for record in normalized["vehicles"]:
        if isinstance(record, Mapping) and str(record.get("vehicle_key") or "") == key:
            return dict(record)
    return None


def _normalize_vehicle_record(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    ppe_name = vehicle_mileage_key(value.get("ppe_name") or value.get("vehicle_key"))
    if not ppe_name:
        return None
    status = _clean_text(value.get("status"), limit=80) or DAILY_VEHICLE_MILEAGE_FAILED
    labels = _merge_labels([], value.get("labels"))
    return {
        "vehicle_key": ppe_name,
        "ppe_name": ppe_name,
        "labels": labels,
        "status": status,
        "detail": _clean_text(value.get("detail"), limit=500),
        "mileage": _clean_text(value.get("mileage"), limit=20),
        "record_end_date": _clean_text(value.get("record_end_date"), limit=20),
        "record_end_time": _clean_text(value.get("record_end_time"), limit=12),
        "last_attempted_at": _clean_text(value.get("last_attempted_at"), limit=40),
        "last_success_at": _clean_text(value.get("last_success_at"), limit=40),
        "last_success_business_date": _clean_text(value.get("last_success_business_date"), limit=20),
    }


def _merge_labels(previous: object, current: object) -> list[str]:
    labels: list[str] = []
    for values in (previous, current):
        if not isinstance(values, list):
            continue
        for value in values:
            label = _clean_text(value, limit=80)
            if label and label not in labels:
                labels.append(label)
    return labels


def _parse_datetime(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").strip()).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _clean_text(value: object, *, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]
