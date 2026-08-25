from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .models import AmbulanceReturnRequest


RECONCILIATION_SITE_KEYS = frozenset({"consumables", "disinfection"})
_CANDIDATE_FIELDS = ("vehicle", "source", "record_id", "case_id", "case_time")


class VehicleCandidateLookupError(RuntimeError):
    """A verified same-case record exists, but its vehicle differs from the task."""

    def __init__(
        self,
        site_key: str,
        expected_vehicle: str,
        candidates: object,
    ) -> None:
        self.site_key = str(site_key or "").strip()
        self.expected_vehicle = str(expected_vehicle or "").strip()
        self.candidates = normalize_vehicle_candidates(candidates)
        candidate_labels = "、".join(
            str(candidate.get("vehicle") or "未標示車輛")
            for candidate in self.candidates
        ) or "未標示車輛"
        super().__init__(
            f"同案件不同車輛：系統車輛={self.expected_vehicle or '未填'}；"
            f"候選車輛={candidate_labels}。"
        )


def normalize_vehicle_candidates(values: object) -> tuple[dict[str, str], ...]:
    """Keep only display-safe, de-duplicated candidate records for a station."""

    if not isinstance(values, (list, tuple)):
        return ()
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        candidate = {field: str(value.get(field) or "").strip() for field in _CANDIDATE_FIELDS}
        if not candidate["vehicle"]:
            continue
        identity = candidate["vehicle"]
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append(candidate)
    return tuple(normalized)


def reconciliation_targets(site: object) -> dict[str, dict[str, Any]]:
    if not isinstance(site, Mapping):
        return {}
    reconciliation = site.get("vehicle_reconciliation")
    if not isinstance(reconciliation, Mapping) and isinstance(site.get("targets"), Mapping):
        reconciliation = site
    if not isinstance(reconciliation, Mapping):
        return {}
    raw_targets = reconciliation.get("targets")
    if not isinstance(raw_targets, Mapping):
        return {}
    targets: dict[str, dict[str, Any]] = {}
    for raw_key, raw_target in raw_targets.items():
        if not isinstance(raw_target, Mapping):
            continue
        vehicle_key = str(raw_key or "").strip()
        if not vehicle_key:
            continue
        target = dict(raw_target)
        target["original_vehicle"] = str(target.get("original_vehicle") or vehicle_key).strip()
        target["state"] = str(target.get("state") or "available").strip()
        target["selected_vehicle"] = str(target.get("selected_vehicle") or "").strip()
        target["candidates"] = list(normalize_vehicle_candidates(target.get("candidates")))
        targets[vehicle_key] = target
    return targets


def pending_reconciliation_targets(site: object) -> dict[str, dict[str, Any]]:
    return {
        vehicle_key: target
        for vehicle_key, target in reconciliation_targets(site).items()
        if str(target.get("state") or "") in {"available", "selected"}
    }


def site_has_pending_vehicle_reconciliation(site: object) -> bool:
    return bool(pending_reconciliation_targets(site))


def site_vehicle_reconciliation_ready_to_retry(site: object) -> bool:
    """A station may retry only after every detected lookup vehicle is confirmed."""

    targets = pending_reconciliation_targets(site)
    return bool(targets) and all(
        str(target.get("state") or "") == "selected"
        for target in targets.values()
    )


def task_has_pending_vehicle_reconciliation(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    site_statuses = payload.get("site_statuses")
    return isinstance(site_statuses, Mapping) and any(
        site_has_pending_vehicle_reconciliation(site)
        for site in site_statuses.values()
    )


def vehicle_reconciliation_run_block_detail(payload: object, run_site_key: str = "") -> str:
    """Return an action-safe reason when a detected candidate has not been consumed."""

    if not isinstance(payload, Mapping):
        return ""
    site_statuses = payload.get("site_statuses")
    if not isinstance(site_statuses, Mapping):
        return ""
    selected_site = str(run_site_key or "").strip()
    pending_sites = {
        str(raw_site_key or "").strip(): pending_reconciliation_targets(site)
        for raw_site_key, site in site_statuses.items()
        if pending_reconciliation_targets(site)
    }
    if not pending_sites:
        return ""
    if not selected_site:
        return "任務有同案件不同車輛待處理；請在對應卡片確認車輛後，以「單獨登打」重試。"
    targets = pending_sites.get(selected_site)
    if not targets:
        return "任務有同案件不同車輛待處理；請先在對應卡片確認車輛，且僅能重試該站。"
    if any(str(target.get("state") or "") != "selected" for target in targets.values()):
        return "請先確認同案件的候選車輛，再以「單獨登打」重試此站。"
    return ""


def selected_lookup_vehicle(site: object, original_vehicle: object) -> str:
    original = str(original_vehicle or "").strip()
    target = reconciliation_targets(site).get(original)
    if not target or str(target.get("state") or "") != "selected":
        return original
    selected = str(target.get("selected_vehicle") or "").strip()
    return selected or original


def request_with_selected_lookup_vehicle(
    request: AmbulanceReturnRequest,
    site: object,
) -> AmbulanceReturnRequest:
    lookup_vehicle = selected_lookup_vehicle(site, request.vehicle)
    return replace(request, vehicle=lookup_vehicle) if lookup_vehicle != request.vehicle else request


def selected_vehicle_target_key(site: object, original_vehicle: object) -> str:
    original = str(original_vehicle or "").strip()
    target = reconciliation_targets(site).get(original)
    if target and str(target.get("state") or "") == "selected":
        return original
    return ""
