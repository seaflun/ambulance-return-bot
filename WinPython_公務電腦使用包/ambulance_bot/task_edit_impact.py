from __future__ import annotations

from itertools import zip_longest
from typing import Any


SITE_ORDER = (
    "duty_work_log",
    "vehicle_mileage",
    "fuel_record",
    "consumables",
    "disinfection",
)

SITE_LABELS = {
    "duty_work_log": "工作",
    "vehicle_mileage": "里程",
    "fuel_record": "加油",
    "consumables": "耗材",
    "disinfection": "消毒",
}

COMMON_FIELD_RULES = {
    "case_date": ("案件日期", {"duty_work_log", "vehicle_mileage", "consumables", "disinfection"}),
    "case_time": ("案件時間", {"duty_work_log", "vehicle_mileage", "consumables", "disinfection"}),
    "case_address": ("案件地址", {"duty_work_log", "vehicle_mileage"}),
    "case_reason": ("出勤原因", {"duty_work_log"}),
    "work_note": ("工作備註", {"duty_work_log"}),
}

VEHICLE_FIELD_RULES = {
    "vehicle": (
        "車號",
        {"duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"},
    ),
    "driver": ("駕駛", {"duty_work_log", "vehicle_mileage", "fuel_record"}),
    "mileage": ("里程", {"vehicle_mileage"}),
    "return_date": ("返隊日期", {"vehicle_mileage"}),
    "return_time": ("返隊時間", {"vehicle_mileage"}),
    "patient_summary": ("傷病患摘要", {"duty_work_log"}),
    "fuel_record": ("加油資料", {"fuel_record"}),
    "consumables": ("耗材", {"consumables"}),
    "disinfection": ("消毒方式", {"disinfection"}),
    "disinfection_items": ("消毒項目", {"disinfection"}),
}


def normalize_edit_value(value: object) -> object:
    if isinstance(value, dict):
        if "enabled" in value:
            return tuple(
                sorted(
                    (str(key).strip(), normalize_edit_value(item))
                    for key, item in value.items()
                    if str(key).strip()
                )
            )
        normalized_items: list[tuple[str, int]] = []
        for key, item_value in value.items():
            name = str(key).strip()
            try:
                quantity = int(item_value)
            except (TypeError, ValueError):
                quantity = 0
            if name and quantity > 0:
                normalized_items.append((name, quantity))
        return tuple(sorted(normalized_items))
    if isinstance(value, list):
        return tuple(sorted(str(item).strip() for item in value if str(item).strip()))
    if isinstance(value, bool):
        return value
    return str(value or "").strip()


def _vehicle_entries(task: dict[str, Any]) -> list[dict[str, Any]]:
    entries = task.get("vehicle_entries")
    if isinstance(entries, list) and entries:
        return [dict(item or {}) for item in entries if isinstance(item, dict)]
    return [dict(task)]


def _fuel_enabled(entry: dict[str, Any]) -> bool:
    fuel = entry.get("fuel_record")
    return bool(dict(fuel or {}).get("enabled")) if isinstance(fuel, dict) else False


def analyze_task_edit(
    previous_task: dict[str, Any],
    current_task: dict[str, Any],
) -> dict[str, Any]:
    changed_fields: list[dict[str, str]] = []
    affected_sites: dict[str, dict[str, Any]] = {}

    def record(
        *,
        key: str,
        label: str,
        site_keys: set[str],
        vehicle_key: str = "",
        vehicle_label: str = "",
        fuel_active: bool = True,
    ) -> None:
        changed_fields.append({"key": key, "label": label})
        for site_key in SITE_ORDER:
            if site_key not in site_keys:
                continue
            if site_key == "fuel_record" and not fuel_active:
                continue
            site = affected_sites.setdefault(
                site_key,
                {
                    "site_key": site_key,
                    "site_label": SITE_LABELS[site_key],
                    "vehicle_keys": [],
                    "vehicle_labels": [],
                    "field_labels": [],
                },
            )
            if vehicle_key and vehicle_key not in site["vehicle_keys"]:
                site["vehicle_keys"].append(vehicle_key)
            if vehicle_label and vehicle_label not in site["vehicle_labels"]:
                site["vehicle_labels"].append(vehicle_label)
            if label not in site["field_labels"]:
                site["field_labels"].append(label)

    for field_name, (label, site_keys) in COMMON_FIELD_RULES.items():
        if normalize_edit_value(previous_task.get(field_name)) != normalize_edit_value(
            current_task.get(field_name)
        ):
            record(key=field_name, label=label, site_keys=site_keys)

    previous_entries = _vehicle_entries(previous_task)
    current_entries = _vehicle_entries(current_task)
    multiple = max(len(previous_entries), len(current_entries)) > 1
    for index, pair in enumerate(
        zip_longest(previous_entries, current_entries, fillvalue={}),
        start=1,
    ):
        previous_entry, current_entry = pair
        vehicle_label = f"第 {index} 車" if multiple else ""
        vehicle_key = str(
            current_entry.get("vehicle")
            or previous_entry.get("vehicle")
            or vehicle_label
        ).strip()
        fuel_active = _fuel_enabled(previous_entry) or _fuel_enabled(current_entry)
        for field_name, (field_label, site_keys) in VEHICLE_FIELD_RULES.items():
            if normalize_edit_value(previous_entry.get(field_name)) == normalize_edit_value(
                current_entry.get(field_name)
            ):
                continue
            label = f"{vehicle_label}{field_label}" if vehicle_label else field_label
            record(
                key=f"vehicle_entries.{index - 1}.{field_name}",
                label=label,
                site_keys=site_keys,
                vehicle_key=vehicle_key,
                vehicle_label=vehicle_label,
                fuel_active=fuel_active,
            )

    changed_labels = list(dict.fromkeys(item["label"] for item in changed_fields))
    site_summaries: list[str] = []
    for site in affected_sites.values():
        labels = list(site["vehicle_labels"])
        if len(labels) == 1:
            site_summaries.append(f"{site['site_label']}（只重登{labels[0]}）")
        else:
            site_summaries.append(str(site["site_label"]))
    return {
        "changed_fields": changed_fields,
        "changed_labels": changed_labels,
        "affected_sites": affected_sites,
        "site_summaries": site_summaries,
    }


def changed_site_keys(impact: dict[str, Any]) -> set[str]:
    affected_sites = impact.get("affected_sites")
    return set(affected_sites) if isinstance(affected_sites, dict) else set()
