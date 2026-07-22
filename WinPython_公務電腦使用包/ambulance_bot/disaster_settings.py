from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4


DEFAULT_DISASTER_VEHICLES = [
    {"label": "新坡11", "ppe_name": "", "recorder_code": "11"},
    {"label": "新坡15", "ppe_name": "", "recorder_code": "15"},
    {"label": "新坡16", "ppe_name": "", "recorder_code": "16"},
    {"label": "新坡91", "ppe_name": "", "recorder_code": "91"},
    {"label": "新坡92", "ppe_name": "", "recorder_code": "92"},
    {"label": "新坡93", "ppe_name": "", "recorder_code": "93"},
]
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
        "deleted": [str(label).strip() for label in settings.get("deleted", []) if str(label).strip()],
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
        deleted = {str(label).strip() for label in settings.get("deleted", []) if str(label).strip()}
        records = [dict(record) for record in DEFAULT_DISASTER_VEHICLES if record["label"] not in deleted]
        for record in clean_disaster_vehicle_records(settings.get("vehicles")):
            records = [existing for existing in records if existing["label"] != record["label"]]
            records.append(record)
        return records


def save_disaster_vehicle_record(
    label: str,
    ppe_name: str,
    recorder_code: str,
    base_dir: Path | None = None,
) -> None:
    record = clean_disaster_vehicle_records(
        [{"label": label, "ppe_name": ppe_name, "recorder_code": recorder_code}]
    )
    if not record:
        raise ValueError("請輸入車輛名稱")
    if not record[0]["recorder_code"]:
        raise ValueError("請輸入行車紀錄器車號")
    with _SETTINGS_LOCK:
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
        settings = read_disaster_vehicle_settings(base_dir)
        known = label in {item["label"] for item in load_disaster_vehicle_records(base_dir)}
        if not known:
            return False
        settings["vehicles"] = [item for item in clean_disaster_vehicle_records(settings.get("vehicles")) if item["label"] != label]
        deleted = [str(item).strip() for item in settings.get("deleted", []) if str(item).strip()]
        if label not in deleted:
            deleted.append(label)
        settings["deleted"] = deleted
        write_disaster_vehicle_settings(settings, base_dir)
        return True


def disaster_vehicle_options(base_dir: Path | None = None) -> list[str]:
    return [record["label"] for record in load_disaster_vehicle_records(base_dir)]


def disaster_vehicle_recorder_codes(base_dir: Path | None = None) -> dict[str, str]:
    return {record["label"]: record["recorder_code"] for record in load_disaster_vehicle_records(base_dir)}


def disaster_vehicle_ppe_names(base_dir: Path | None = None) -> dict[str, str]:
    return {record["label"]: record["ppe_name"] for record in load_disaster_vehicle_records(base_dir) if record["ppe_name"]}
