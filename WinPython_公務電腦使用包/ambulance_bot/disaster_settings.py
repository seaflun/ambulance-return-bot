from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from .models import (
    DISASTER_ACTION_PACKAGES,
    VEHICLE_TYPE_BUILT_IN,
    VEHICLE_TYPE_CUSTOM,
    normalize_vehicle_type,
)


DEFAULT_DISASTER_VEHICLES = [
    {
        "label": "新坡11",
        "ppe_name": "",
        "recorder_code": "11",
        "vehicle_type": VEHICLE_TYPE_BUILT_IN,
    },
    {
        "label": "新坡15",
        "ppe_name": "",
        "recorder_code": "15",
        "vehicle_type": VEHICLE_TYPE_BUILT_IN,
    },
    {
        "label": "新坡85",
        "ppe_name": "",
        "recorder_code": "85",
        "vehicle_type": VEHICLE_TYPE_BUILT_IN,
    },
]
DEFAULT_DISASTER_VEHICLE_LABELS = frozenset(record["label"] for record in DEFAULT_DISASTER_VEHICLES)
_SETTINGS_LOCK = threading.RLock()


def disaster_vehicle_settings_path(base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return Path(base_dir) / "settings" / "disaster_vehicles.json"
    configured = str(os.getenv("DISASTER_VEHICLE_SETTINGS_PATH") or "").strip()
    if configured:
        return Path(configured)
    return Path(os.getenv("ARTIFACTS_DIR", "artifacts")) / "settings" / "disaster_vehicles.json"


def clean_disaster_vehicle_records(records: Any) -> list[dict[str, str]]:
    if not isinstance(records, list):
        return []
    cleaned: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        label = str(record.get("label") or "").strip()
        if not label:
            continue
        cleaned.append(
            {
                "label": label,
                "ppe_name": str(record.get("ppe_name") or "").strip().upper(),
                "recorder_code": str(record.get("recorder_code") or "").strip(),
                "vehicle_type": normalize_vehicle_type(record.get("vehicle_type")),
            }
        )
    return cleaned


def read_disaster_vehicle_settings(base_dir: Path | None = None) -> dict[str, Any]:
    path = disaster_vehicle_settings_path(base_dir)
    with _SETTINGS_LOCK:
        if not path.exists():
            return {"vehicles": [], "deleted": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"vehicles": [], "deleted": []}
    return payload if isinstance(payload, dict) else {"vehicles": [], "deleted": []}


def write_disaster_vehicle_settings(settings: dict[str, Any], base_dir: Path | None = None) -> None:
    path = disaster_vehicle_settings_path(base_dir)
    payload = {
        "vehicles": clean_disaster_vehicle_records(settings.get("vehicles")),
        "deleted": [
            str(label).strip()
            for label in settings.get("deleted", [])
            if str(label).strip() and str(label).strip() not in DEFAULT_DISASTER_VEHICLE_LABELS
        ],
        "action_packages": clean_disaster_action_packages(settings.get("action_packages")),
    }
    with _SETTINGS_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def load_disaster_vehicle_records(base_dir: Path | None = None) -> list[dict[str, str]]:
    with _SETTINGS_LOCK:
        settings = read_disaster_vehicle_settings(base_dir)
        records = [dict(record) for record in DEFAULT_DISASTER_VEHICLES]
        for record in clean_disaster_vehicle_records(settings.get("vehicles")):
            if record["label"] in DEFAULT_DISASTER_VEHICLE_LABELS:
                record["vehicle_type"] = VEHICLE_TYPE_BUILT_IN
            records = [existing for existing in records if existing["label"] != record["label"]]
            records.append(record)
        return records


def save_disaster_vehicle_record(
    label: str,
    ppe_name: str,
    recorder_code: str,
    base_dir: Path | None = None,
    vehicle_type: str = VEHICLE_TYPE_CUSTOM,
) -> None:
    record = clean_disaster_vehicle_records(
        [
            {
                "label": label,
                "ppe_name": ppe_name,
                "recorder_code": recorder_code,
                "vehicle_type": vehicle_type,
            }
        ]
    )
    if not record:
        raise ValueError("請輸入車輛名稱")
    if not record[0]["recorder_code"]:
        raise ValueError("請輸入行車紀錄器車號")
    with _SETTINGS_LOCK:
        existing = next(
            (item for item in load_disaster_vehicle_records(base_dir) if item["label"] == record[0]["label"]),
            None,
        )
        if existing is not None:
            record[0]["vehicle_type"] = normalize_vehicle_type(existing.get("vehicle_type"))
        settings = read_disaster_vehicle_settings(base_dir)
        records = clean_disaster_vehicle_records(settings.get("vehicles"))
        records = [item for item in records if item["label"] != record[0]["label"]]
        records.append(record[0])
        settings["vehicles"] = records
        settings["deleted"] = [item for item in settings.get("deleted", []) if item != record[0]["label"]]
        write_disaster_vehicle_settings(settings, base_dir)


def delete_disaster_vehicle_record(label: str, base_dir: Path | None = None) -> bool:
    label = str(label or "").strip()
    if not label:
        return False
    with _SETTINGS_LOCK:
        record = next((item for item in load_disaster_vehicle_records(base_dir) if item["label"] == label), None)
        if (
            record is None
            or label in DEFAULT_DISASTER_VEHICLE_LABELS
            or normalize_vehicle_type(record.get("vehicle_type")) != VEHICLE_TYPE_CUSTOM
        ):
            return False
        settings = read_disaster_vehicle_settings(base_dir)
        settings["vehicles"] = [item for item in clean_disaster_vehicle_records(settings.get("vehicles")) if item["label"] != label]
        write_disaster_vehicle_settings(settings, base_dir)
        return True


def disaster_vehicle_options(base_dir: Path | None = None) -> list[str]:
    return [record["label"] for record in load_disaster_vehicle_records(base_dir)]


def disaster_vehicle_recorder_codes(base_dir: Path | None = None) -> dict[str, str]:
    return {record["label"]: record["recorder_code"] for record in load_disaster_vehicle_records(base_dir)}


def disaster_vehicle_ppe_names(base_dir: Path | None = None) -> dict[str, str]:
    return {record["label"]: record["ppe_name"] for record in load_disaster_vehicle_records(base_dir) if record["ppe_name"]}


def clean_disaster_action_packages(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    packages: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in packages:
            packages.append(text)
    return packages


def load_disaster_action_packages(base_dir: Path | None = None) -> list[str]:
    settings = read_disaster_vehicle_settings(base_dir)
    packages = clean_disaster_action_packages(settings.get("action_packages"))
    return packages or list(DISASTER_ACTION_PACKAGES)


def save_disaster_action_packages(values: list[str], base_dir: Path | None = None) -> None:
    with _SETTINGS_LOCK:
        settings = read_disaster_vehicle_settings(base_dir)
        settings["action_packages"] = clean_disaster_action_packages(values)
        write_disaster_vehicle_settings(settings, base_dir)
