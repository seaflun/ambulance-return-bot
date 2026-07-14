from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .adapters import SITE_DEFINITIONS, SiteAutomationResult
from .models import AmbulanceReturnRequest
from .site_diagnostics import DIAGNOSTIC_FIELDS, result_with_diagnostics


def _legacy_silent_save_pattern(site_label: str, exact_detail_pattern: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?:登入帳號：{re.escape(site_label)}=[^\r\n。]+。)?{exact_detail_pattern}"
    )


LEGACY_SILENT_SAVE_RECONCILIATION_RULES: dict[str, tuple[str, str, re.Pattern[str]]] = {
    "duty_work_log": (
        "duty_work_log_waiting_confirmation",
        "duty_work_log_saved",
        _legacy_silent_save_pattern(
            "工作",
            re.escape("waiting_confirmation: 已按下儲存，但未收到儲存成功回應；請人工確認。"),
        ),
    ),
    "vehicle_mileage": (
        "vehicle_mileage_waiting_confirmation",
        "vehicle_mileage_saved",
        _legacy_silent_save_pattern(
            "里程",
            re.escape(
                "waiting_confirmation: 已填寫車輛里程並按下儲存；"
                "未偵測到確認視窗，尚未確認伺服器已儲存。"
            ),
        ),
    ),
    "consumables": (
        "consumables_failed",
        "consumables_saved",
        _legacy_silent_save_pattern(
            "耗材",
            re.escape("耗材儲存未取得明確成功回應：未出現確認訊息"),
        ),
    ),
    "disinfection": (
        "disinfection_waiting_confirmation",
        "disinfection_saved",
        _legacy_silent_save_pattern(
            "消毒",
            r"waiting_confirmation:\ disinfection\ items\ updated=[1-9][0-9]*;\ save\ response\ not\ confirmed\.",
        ),
    ),
}


SUCCESS_SITE_STATUSES = {
    "completed_by_user",
    "duty_work_log_saved",
    "vehicle_mileage_saved",
    "disinfection_saved",
    "consumables_saved",
}
COMPLETED_TASK_HISTORY_HOURS = 24 * 14
WORKER_CLAIM_LEASE_SECONDS = 15 * 60
RECENT_STATUS_EVENT_ID_LIMIT = 256


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TaskActiveError(RuntimeError):
    pass


class WorkerClaimConflictError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class SiteCompletionConflictError(RuntimeError):
    pass


class JsonTaskStore:
    def __init__(self, tasks_dir: Path, claim_lease_seconds: int = WORKER_CLAIM_LEASE_SECONDS) -> None:
        self.tasks_dir = tasks_dir
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.claim_lease_seconds = max(60, int(claim_lease_seconds))
        self._lock = threading.RLock()

    def create(self, request: AmbulanceReturnRequest) -> dict[str, Any]:
        payload = {
            "task": request.to_dict(),
            "created_at": now_text(),
            "updated_at": now_text(),
            "overall_status": "created",
            "worker_queue": initial_worker_queue_state(),
            "recent_status_event_ids": [],
            "site_statuses": initial_site_statuses(),
            "site_attempts": initial_site_attempts(),
            "events": [
                {
                    "time": now_text(),
                    "status": "created",
                    "detail": "任務已建立。",
                }
            ],
        }
        with self._lock:
            self.save_payload(request.task_id, payload)
            return payload

    def update_task(
        self,
        task_id: str,
        request: AmbulanceReturnRequest,
        changed_site_keys: set[str] | None = None,
        site_update_contexts: dict[str, dict[str, object]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            if task_payload_is_active_for_edit(payload):
                raise TaskActiveError(task_id)
            previous_queue_state = worker_queue_state(payload)
            previous_claim_attempt = worker_claim_attempt(payload, previous_queue_state)
            previous_task = dict(payload.get("task") or {})
            explicit_changed_sites = changed_site_keys is not None
            changed_site_keys = set(changed_site_keys or set())
            request.task_id = task_id
            payload["task"] = request.to_dict()
            payload["updated_at"] = now_text()
            if explicit_changed_sites:
                self._prune_changed_vehicle_results(
                    payload,
                    previous_task,
                    request.to_dict(),
                    changed_site_keys,
                )
            else:
                self._clear_vehicle_results(payload)
            updated_sites = self._mark_changed_sites_for_update(payload, changed_site_keys, site_update_contexts or {})
            if updated_sites:
                payload["overall_status"] = "task_updated_needs_site_update"
                reset_queue_state = initial_worker_queue_state()
                reset_queue_state["claim_attempt"] = str(previous_claim_attempt)
                payload["worker_queue"] = reset_queue_state
            else:
                payload["overall_status"] = str(payload.get("overall_status") or "created")
            self.add_event_to_payload(payload, "task_updated", "任務內容已修改。")
            self.save_payload(task_id, payload)
            return payload

    def _clear_vehicle_results(self, payload: dict[str, Any]) -> None:
        site_statuses = payload.get("site_statuses")
        if not isinstance(site_statuses, dict):
            return
        for site in site_statuses.values():
            if isinstance(site, dict):
                site.pop("vehicle_results", None)

    def _prune_changed_vehicle_results(
        self,
        payload: dict[str, Any],
        previous_task: dict[str, object],
        current_task: dict[str, object],
        changed_site_keys: set[str],
    ) -> None:
        site_statuses = payload.get("site_statuses")
        if not isinstance(site_statuses, dict):
            return
        for site_key in changed_site_keys:
            site = site_statuses.get(site_key)
            if not isinstance(site, dict) or not isinstance(site.get("vehicle_results"), dict):
                continue
            unchanged_keys = _unchanged_vehicle_checkpoint_keys(previous_task, current_task, site_key)
            kept = {
                str(key): dict(record)
                for key, record in dict(site["vehicle_results"]).items()
                if str(key) in unchanged_keys and isinstance(record, dict)
            }
            if kept:
                site["vehicle_results"] = kept
            else:
                site.pop("vehicle_results", None)

    def _mark_changed_sites_for_update(
        self,
        payload: dict[str, Any],
        changed_site_keys: set[str],
        site_update_contexts: dict[str, dict[str, object]],
    ) -> list[str]:
        site_statuses = payload.get("site_statuses")
        if not isinstance(site_statuses, dict):
            return []
        updated_sites: list[str] = []
        for site_key in changed_site_keys:
            site = site_statuses.get(site_key)
            if not isinstance(site, dict):
                continue
            status = str(site.get("status") or "")
            context = site_update_contexts.get(site_key)
            manual_removal_reason = ""
            if context and _site_may_have_official_record(site):
                manual_removal_reason = _manual_record_removal_reason(context, site_key)
            if manual_removal_reason:
                site.pop("vehicle_results", None)
                site["status"] = f"{site_key}_waiting_confirmation"
                site["detail"] = manual_removal_reason
                site["updated_at"] = now_text()
                site["update_context"] = context
                for field in DIAGNOSTIC_FIELDS:
                    site[field] = ""
                updated_sites.append(site_key)
                continue
            partial_or_uncertain = (
                "failed" in status
                or "error" in status
                or "waiting_confirmation" in status
                or isinstance(site.get("vehicle_results"), dict)
            )
            newly_enabled_fuel = (
                site_key == "fuel_record"
                and bool(context)
                and _fuel_record_became_enabled(context)
            )
            if status not in SUCCESS_SITE_STATUSES and not status.endswith("_saved") and not (
                context and (partial_or_uncertain or newly_enabled_fuel)
            ):
                continue
            site["status"] = f"{site_key}_needs_update"
            site["detail"] = "任務內容已修改，請更新此站。"
            site["updated_at"] = now_text()
            if context:
                site["update_context"] = context
            for field in DIAGNOSTIC_FIELDS:
                site[field] = ""
            updated_sites.append(site_key)
        return updated_sites

    def get(self, task_id: str) -> dict[str, Any]:
        path = self.path_for(task_id)
        with self._lock:
            if not path.exists():
                raise FileNotFoundError(task_id)
            return json.loads(path.read_text(encoding="utf-8"))

    def request_for(self, task_id: str) -> AmbulanceReturnRequest:
        return AmbulanceReturnRequest.from_dict(self.get(task_id)["task"])

    def list_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            self.cleanup()
            paths = sorted(self.tasks_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
            recent: list[dict[str, Any]] = []
            for path in paths:
                payload = self._read_payload_or_quarantine(path)
                if payload is None:
                    continue
                recent.append(payload)
                if len(recent) >= limit:
                    break
            return recent

    def queue_for_worker(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            if worker_claim_lease_is_active(payload):
                raise WorkerClaimConflictError(
                    "worker_claim_conflict",
                    "任務已由 worker 執行中，不可重新排隊或取代目前的 claim。",
                )
            if self._is_fully_done(payload):
                raise WorkerClaimConflictError(
                    "task_already_completed",
                    "任務已全部完成；如需修正，請先編輯內容產生待更新站別。",
                )
            payload["overall_status"] = "queued_for_worker"
            queue_state = worker_queue_state(payload)
            prior_claim_attempt = worker_claim_attempt(payload, queue_state)
            queue_state.update(
                {
                    "status": "queued",
                    "queued_at": now_text(),
                    "claimed_at": "",
                    "lease_expires_at": "",
                    "last_heartbeat_at": "",
                    "queue_id": str(uuid4()),
                    "claim_id": "",
                    "claim_attempt": str(prior_claim_attempt),
                    "completed_at": "",
                    "worker_id": "",
                    "last_error": "",
                }
            )
            payload["worker_queue"] = queue_state
            self.add_event_to_payload(payload, "queued_for_worker", "任務已排隊，等待公務電腦 worker 執行。")
            self.save_payload(task_id, payload)
            return payload

    def claim_next_for_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self._lock:
            self.cleanup()
            paths = sorted(self.tasks_dir.glob("*.json"), key=lambda item: item.stat().st_mtime)
            for path in paths:
                payload = self._read_payload_or_quarantine(path)
                if payload is None:
                    continue
                queue_state = worker_queue_state(payload)
                queue_status = queue_state.get("status")
                reclaimed = queue_status == "claimed" and self._worker_claim_expired(queue_state)
                if queue_status != "queued" and not reclaimed:
                    continue
                task = payload.get("task")
                if not isinstance(task, dict):
                    self._quarantine_corrupt_path(path)
                    continue
                task_id = str(task.get("task_id") or path.stem).strip()
                if not task_id:
                    self._quarantine_corrupt_path(path)
                    continue
                try:
                    expected_path = self.path_for(task_id)
                except FileNotFoundError:
                    self._quarantine_corrupt_path(path)
                    continue
                if expected_path.name != path.name:
                    self._quarantine_corrupt_path(path)
                    continue
                now = datetime.now()
                now_value = now.isoformat(timespec="seconds")
                payload["overall_status"] = "claimed_by_worker"
                prior_claim_attempt = worker_claim_attempt(payload, queue_state)
                claim_attempt = max(prior_claim_attempt + 1, 2 if reclaimed else 1)
                queue_id = str(queue_state.get("queue_id") or uuid4())
                queue_state.update(
                    {
                        "status": "claimed",
                        "claimed_at": now_value,
                        "lease_expires_at": (now + timedelta(seconds=self.claim_lease_seconds)).isoformat(timespec="seconds"),
                        "last_heartbeat_at": now_value,
                        "queue_id": queue_id,
                        "claim_id": str(uuid4()),
                        "claim_attempt": str(claim_attempt),
                        "completed_at": "",
                        "worker_id": worker_id,
                        "last_error": "",
                    }
                )
                payload["worker_queue"] = queue_state
                payload["worker"] = {
                    "id": worker_id,
                    "claimed_at": now_value,
                    "claim_id": queue_state["claim_id"],
                }
                event_status = "worker_claim_reclaimed" if reclaimed else "claimed_by_worker"
                event_detail = (
                    f"前次 worker 租約逾時，任務已由 {worker_id} 重新領取。"
                    if reclaimed
                    else f"公務電腦 worker 已領取：{worker_id}"
                )
                self.add_event_to_payload(payload, event_status, event_detail)
                self.save_payload(task_id, payload)
                return payload
        return None

    def claim_task_for_worker(self, task_id: str, worker_id: str) -> dict[str, Any]:
        """Claim one explicitly selected task while preserving claim fencing."""

        normalized_worker_id = str(worker_id or "").strip() or "public-duty-pc"
        with self._lock:
            payload = self.get(task_id)
            if self._is_fully_done(payload):
                raise WorkerClaimConflictError(
                    "task_already_completed",
                    "此任務已全部完成，不可再由 worker 接手執行。",
                )
            queue_state = worker_queue_state(payload)
            queue_status = queue_state.get("status")
            claim_expired = queue_status == "claimed" and self._worker_claim_expired(queue_state)
            if queue_status == "claimed" and not claim_expired:
                if str(queue_state.get("worker_id") or "").strip() != normalized_worker_id:
                    raise WorkerClaimConflictError(
                        "worker_claim_conflict",
                        "任務已由其他 worker 執行，請等待租約結束或中止原執行。",
                    )
                self._renew_worker_claim(payload)
                self.save_payload(task_id, payload)
                return payload
            if not claim_expired and task_payload_is_active_for_edit(payload):
                raise WorkerClaimConflictError(
                    "task_already_running",
                    "任務已有登打流程執行中，無法另行領取。",
                )

            now = datetime.now()
            now_value = now.isoformat(timespec="seconds")
            prior_claim_attempt = worker_claim_attempt(payload, queue_state)
            claim_attempt = max(prior_claim_attempt + 1, 2 if claim_expired else 1)
            queue_id = str(queue_state.get("queue_id") or "").strip()
            if not queue_id or queue_status not in {"queued", "claimed"}:
                queue_id = str(uuid4())
            queue_state.update(
                {
                    "status": "claimed",
                    "claimed_at": now_value,
                    "lease_expires_at": (now + timedelta(seconds=self.claim_lease_seconds)).isoformat(timespec="seconds"),
                    "last_heartbeat_at": now_value,
                    "queue_id": queue_id,
                    "claim_id": str(uuid4()),
                    "claim_attempt": str(claim_attempt),
                    "completed_at": "",
                    "worker_id": normalized_worker_id,
                    "last_error": "",
                }
            )
            payload["overall_status"] = "claimed_by_worker"
            payload["worker_queue"] = queue_state
            payload["worker"] = {
                "id": normalized_worker_id,
                "claimed_at": now_value,
                "claim_id": queue_state["claim_id"],
            }
            event_status = "worker_claim_reclaimed" if claim_expired else "claimed_by_worker"
            self.add_event_to_payload(
                payload,
                event_status,
                f"手動選取任務已由 worker 領取：{normalized_worker_id}",
            )
            self.save_payload(task_id, payload)
            return payload

    def _worker_claim_expired(self, queue_state: dict[str, str], now: datetime | None = None) -> bool:
        current = now or datetime.now()
        raw_expiry = str(queue_state.get("lease_expires_at") or "").strip()
        if raw_expiry:
            try:
                return datetime.fromisoformat(raw_expiry) <= current
            except ValueError:
                return True
        raw_claimed = str(queue_state.get("claimed_at") or "").strip()
        if not raw_claimed:
            return True
        try:
            claimed_at = datetime.fromisoformat(raw_claimed)
        except ValueError:
            return True
        return claimed_at + timedelta(seconds=self.claim_lease_seconds) <= current

    def _renew_worker_claim(self, payload: dict[str, Any]) -> None:
        queue_state = worker_queue_state(payload)
        if queue_state.get("status") != "claimed":
            return
        now = datetime.now()
        queue_state["last_heartbeat_at"] = now.isoformat(timespec="seconds")
        queue_state["lease_expires_at"] = (now + timedelta(seconds=self.claim_lease_seconds)).isoformat(timespec="seconds")
        payload["worker_queue"] = queue_state

    def _validate_worker_claim_identity(
        self,
        payload: dict[str, Any],
        claim_id: str,
        worker_id: str,
        enforce: bool,
    ) -> None:
        if not enforce:
            return
        queue_state = worker_queue_state(payload)
        supplied_claim_id = str(claim_id or "").strip()
        supplied_worker_id = str(worker_id or "").strip()
        claim_attempt = worker_claim_attempt(payload, queue_state)
        if claim_attempt > 1 and not supplied_claim_id:
            raise WorkerClaimConflictError(
                "worker_claim_identity_required",
                "任務已由其他 worker 重新領取；回報必須包含目前的 claim_id。",
            )
        expected_claim_id = str(queue_state.get("claim_id") or "").strip()
        expected_worker_id = str(queue_state.get("worker_id") or "").strip()
        if (
            supplied_claim_id
            and supplied_claim_id != expected_claim_id
        ) or (
            supplied_worker_id
            and supplied_worker_id != expected_worker_id
        ):
            raise WorkerClaimConflictError(
                "worker_claim_conflict",
                "worker claim_id 或 worker_id 與目前任務租約不符，已拒絕過期回報。",
            )

    def _apply_overall_status_to_payload(
        self,
        payload: dict[str, Any],
        status: str,
        detail: str,
        *,
        add_event: bool = True,
    ) -> None:
        payload["overall_status"] = status
        queue_state = worker_queue_state(payload)
        if status == "queued_for_worker":
            queue_state["status"] = "queued"
            queue_state["queued_at"] = queue_state.get("queued_at") or now_text()
        elif status == "claimed_by_worker":
            queue_state["status"] = "claimed"
            queue_state["claimed_at"] = queue_state.get("claimed_at") or now_text()
        elif queue_state.get("status") in {"queued", "claimed"} and worker_queue_overall_status_is_terminal(status):
            queue_state["status"] = "completed"
            queue_state["completed_at"] = now_text()
            queue_state["lease_expires_at"] = ""
            if "failed" in status or "error" in status:
                queue_state["last_error"] = detail
        payload["worker_queue"] = queue_state
        self._renew_worker_claim(payload)
        if add_event:
            self.add_event_to_payload(payload, status, detail)

    def set_overall_status(
        self,
        task_id: str,
        status: str,
        detail: str = "",
        *,
        claim_id: str = "",
        worker_id: str = "",
        enforce_claim_identity: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            self._validate_worker_claim_identity(
                payload,
                claim_id,
                worker_id,
                enforce_claim_identity,
            )
            self._apply_overall_status_to_payload(payload, status, detail)
            self.save_payload(task_id, payload)
            return payload

    def update_site_result(
        self,
        task_id: str,
        result: SiteAutomationResult,
        vehicle_key: str = "",
        vehicle_label: str = "",
        *,
        claim_id: str = "",
        worker_id: str = "",
        enforce_claim_identity: bool = False,
        _payload: dict[str, Any] | None = None,
        _save: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            payload = _payload if _payload is not None else self.get(task_id)
            self._validate_worker_claim_identity(
                payload,
                claim_id,
                worker_id,
                enforce_claim_identity,
            )
            result = result_with_diagnostics(result)
            site = payload["site_statuses"][result.key]
            attempts = site_attempts(payload)
            if site.get("status") == "completed_by_user":
                self.add_event_to_payload(payload, result.status, f"{result.name}: 背景狀態已略過，因使用者已確認完成。")
                self._renew_worker_claim(payload)
                if _save:
                    self.save_payload(task_id, payload)
                return payload
            vehicle_key = str(vehicle_key or "").strip()
            if vehicle_key:
                results = dict(site.get("vehicle_results") or {})
                existing_vehicle_result = results.get(vehicle_key)
                if isinstance(existing_vehicle_result, dict) and str(
                    existing_vehicle_result.get("status") or ""
                ) == "completed_by_user":
                    self.add_event_to_payload(
                        payload,
                        result.status,
                        f"{result.name} ({vehicle_key}): 背景狀態已略過，因使用者已確認此車完成。",
                    )
                    self._renew_worker_claim(payload)
                    if _save:
                        self.save_payload(task_id, payload)
                    return payload
                results[vehicle_key] = {
                    "status": result.status,
                    "detail": result.detail,
                    "updated_at": now_text(),
                    "vehicle_label": str(vehicle_label or vehicle_key).strip(),
                    **{field: str(getattr(result, field, "") or "") for field in DIAGNOSTIC_FIELDS},
                }
                site["vehicle_results"] = results
                site["status"] = aggregate_vehicle_site_status(payload, result.key, results)
                site["detail"] = vehicle_site_result_detail(payload, result.key, results)
            else:
                site["status"] = result.status
                site["detail"] = result.detail
            site["updated_at"] = now_text()
            if site["status"] in SUCCESS_SITE_STATUSES or str(site["status"]).endswith("_saved"):
                site.pop("update_context", None)
            diagnostic_source = result
            if vehicle_key and ("failed" in str(site["status"]) or "error" in str(site["status"])):
                failure_record = next(
                    (
                        record
                        for record in dict(site.get("vehicle_results") or {}).values()
                        if isinstance(record, dict)
                        and ("failed" in str(record.get("status") or "") or "error" in str(record.get("status") or ""))
                    ),
                    {},
                )
                for field in DIAGNOSTIC_FIELDS:
                    site[field] = str(failure_record.get(field) or "")
            elif vehicle_key and str(site["status"]).endswith("_saved"):
                for field in DIAGNOSTIC_FIELDS:
                    site[field] = ""
            else:
                for field in DIAGNOSTIC_FIELDS:
                    site[field] = str(getattr(diagnostic_source, field, "") or "")
            attempt = {
                "attempt_id": str(uuid4()),
                "time": now_text(),
                "status": result.status,
                "detail": result.detail,
                "site_name": result.name,
                "vehicle_key": vehicle_key,
            }
            for field in DIAGNOSTIC_FIELDS:
                attempt[field] = str(getattr(result, field, "") or "")
            attempts.setdefault(result.key, []).append(attempt)
            payload["site_attempts"] = attempts
            self.add_event_to_payload(
                payload,
                result.status,
                f"{result.name}: {result.detail}",
                {
                    **{field: str(getattr(result, field, "") or "") for field in DIAGNOSTIC_FIELDS},
                    **({"vehicle_key": vehicle_key} if vehicle_key else {}),
                },
            )
            self._renew_worker_claim(payload)
            if _save:
                self.save_payload(task_id, payload)
            return payload

    def _recent_status_event_ids(self, payload: dict[str, Any]) -> list[str]:
        existing = payload.get("recent_status_event_ids")
        if not isinstance(existing, list):
            return []
        return [value for item in existing if (value := str(item or "").strip())]

    def _remember_status_event_id(self, payload: dict[str, Any], status_event_id: str) -> None:
        event_id = str(status_event_id or "").strip()
        if not event_id:
            return
        event_ids = [value for value in self._recent_status_event_ids(payload) if value != event_id]
        event_ids.append(event_id)
        payload["recent_status_event_ids"] = event_ids[-max(1, int(RECENT_STATUS_EVENT_ID_LIMIT)) :]

    def apply_worker_status(
        self,
        task_id: str,
        *,
        result: SiteAutomationResult | None = None,
        vehicle_key: str = "",
        vehicle_label: str = "",
        overall_status: str = "",
        overall_detail: str = "",
        status_event_id: str = "",
        claim_id: str = "",
        worker_id: str = "",
        enforce_claim_identity: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        with self._lock:
            payload = self.get(task_id)
            self._validate_worker_claim_identity(
                payload,
                claim_id,
                worker_id,
                enforce_claim_identity,
            )
            queue_state = worker_queue_state(payload)
            if (
                enforce_claim_identity
                and queue_state.get("status") == "claimed"
                and self._worker_claim_expired(queue_state)
            ):
                raise WorkerClaimConflictError(
                    "worker_claim_inactive",
                    "目前 worker claim 的租約已逾時，拒絕過期狀態回報。",
                )
            event_id = str(status_event_id or "").strip()
            if event_id and event_id in self._recent_status_event_ids(payload):
                return payload, True
            if enforce_claim_identity and queue_state.get("status") != "claimed":
                raise WorkerClaimConflictError(
                    "worker_claim_inactive",
                    "目前 worker claim 已失效或任務已結束，拒絕新的狀態回報。",
                )
            if result is not None:
                self.update_site_result(
                    task_id,
                    result,
                    vehicle_key=vehicle_key,
                    vehicle_label=vehicle_label,
                    _payload=payload,
                    _save=False,
                )
            if overall_status:
                self._apply_overall_status_to_payload(payload, overall_status, overall_detail)
            self._remember_status_event_id(payload, event_id)
            self.save_payload(task_id, payload)
            return payload, False

    def update_vehicle_site_result(self, task_id: str, site_key: str, vehicle_key: str, status: str, detail: str) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            site = payload["site_statuses"][site_key]
            results = dict(site.get("vehicle_results") or {})
            results[vehicle_key] = {
                "status": status,
                "detail": detail,
                "updated_at": now_text(),
            }
            site["vehicle_results"] = results
            self._renew_worker_claim(payload)
            self.save_payload(task_id, payload)
            return payload

    def reconcile_legacy_silent_save_results(self, task_id: str) -> tuple[dict[str, Any], bool]:
        with self._lock:
            payload = self.get(task_id)
            site_statuses = payload.get("site_statuses")
            if not isinstance(site_statuses, dict):
                return payload, False

            corrected_site_names: list[str] = []
            missing_vehicle_results = object()
            for site_key, (legacy_status, saved_status, detail_pattern) in (
                LEGACY_SILENT_SAVE_RECONCILIATION_RULES.items()
            ):
                site = site_statuses.get(site_key)
                if not isinstance(site, dict):
                    continue
                vehicle_results = site.get("vehicle_results", missing_vehicle_results)
                if vehicle_results is not missing_vehicle_results and vehicle_results != {}:
                    continue
                if str(site.get("status") or "") != legacy_status:
                    continue
                if detail_pattern.fullmatch(str(site.get("detail") or "")) is None:
                    continue

                site["status"] = saved_status
                site["detail"] = "舊版無提示儲存誤判已校正為已儲存。"
                site["updated_at"] = now_text()
                site.pop("update_context", None)
                for field in DIAGNOSTIC_FIELDS:
                    site[field] = ""
                corrected_site_names.append(str(site.get("name") or site_key))

            if not corrected_site_names:
                return payload, False

            detail = f"舊版無提示儲存誤判已修正：{'、'.join(corrected_site_names)}。"
            if self._is_fully_done(payload):
                self._apply_overall_status_to_payload(
                    payload,
                    "desktop_fast_completed",
                    detail,
                    add_event=False,
                )
                queue_state = worker_queue_state(payload)
                queue_state["status"] = "completed"
                queue_state["completed_at"] = now_text()
                queue_state["lease_expires_at"] = ""
                queue_state["last_error"] = ""
                payload["worker_queue"] = queue_state
            self.add_event_to_payload(payload, "legacy_silent_save_reconciled", detail)
            self.save_payload(task_id, payload)
            return payload, True

    def mark_site_completed(self, task_id: str, site_key: str, vehicle_key: str = "") -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            site = payload["site_statuses"][site_key]
            attempts = site_attempts(payload)
            normalized_vehicle_key = str(vehicle_key or "").strip()
            results = site.get("vehicle_results")
            completed_detail = "使用者已人工確認完成。"

            if normalized_vehicle_key:
                if not isinstance(results, dict) or not isinstance(results.get(normalized_vehicle_key), dict):
                    raise SiteCompletionConflictError("找不到要確認的車輛回報。")
                record = dict(results[normalized_vehicle_key])
                if "waiting_confirmation" not in str(record.get("status") or ""):
                    raise SiteCompletionConflictError("只有待人工確認的車輛可標記完成。")
                record.update(status="completed_by_user", detail=completed_detail, updated_at=now_text())
                results = dict(results)
                results[normalized_vehicle_key] = record
                site["vehicle_results"] = results
                site["status"] = aggregate_vehicle_site_status(payload, site_key, results)
                site["detail"] = vehicle_site_result_detail(payload, site_key, results)
            elif isinstance(results, dict) and results:
                expected_keys = expected_vehicle_result_keys(payload, site_key) or [str(key) for key in results]
                expected_records = [results.get(key) for key in expected_keys]
                if any(not isinstance(record, dict) for record in expected_records):
                    raise SiteCompletionConflictError("尚有車輛未回報，不能直接確認整站完成。")
                statuses = [str(dict(record).get("status") or "") for record in expected_records]
                if any("failed" in status or "error" in status or "running" in status for status in statuses):
                    raise SiteCompletionConflictError("尚有車輛失敗或執行中，不能直接確認整站完成。")
                waiting_keys = [
                    key for key, status in zip(expected_keys, statuses) if "waiting_confirmation" in status
                ]
                if not waiting_keys:
                    raise SiteCompletionConflictError("只有待人工確認的站點可標記完成。")
                results = dict(results)
                for key in waiting_keys:
                    record = dict(results[key])
                    record.update(status="completed_by_user", detail=completed_detail, updated_at=now_text())
                    results[key] = record
                site["vehicle_results"] = results
                site["status"] = aggregate_vehicle_site_status(payload, site_key, results)
                site["detail"] = vehicle_site_result_detail(payload, site_key, results)
            else:
                if "waiting_confirmation" not in str(site.get("status") or ""):
                    raise SiteCompletionConflictError("只有待人工確認的站點可標記完成。")
                site["status"] = "completed_by_user"
                site["detail"] = completed_detail

            site["updated_at"] = now_text()
            remaining_failure: dict[str, Any] = {}
            if isinstance(site.get("vehicle_results"), dict) and (
                "failed" in str(site.get("status") or "") or "error" in str(site.get("status") or "")
            ):
                vehicle_results = dict(site["vehicle_results"])
                ordered_keys = expected_vehicle_result_keys(payload, site_key) or [str(key) for key in vehicle_results]
                remaining_failure = next(
                    (
                        dict(record)
                        for key in ordered_keys
                        if isinstance((record := vehicle_results.get(key)), dict)
                        and (
                            "failed" in str(record.get("status") or "")
                            or "error" in str(record.get("status") or "")
                        )
                    ),
                    {},
                )
            for field in DIAGNOSTIC_FIELDS:
                site[field] = str(remaining_failure.get(field) or "")
            attempts.setdefault(site_key, []).append(
                {
                    "attempt_id": str(uuid4()),
                    "time": now_text(),
                    "status": "completed_by_user",
                    "detail": completed_detail,
                    "site_name": site["name"],
                    "vehicle_key": normalized_vehicle_key,
                    **{field: "" for field in DIAGNOSTIC_FIELDS},
                }
            )
            payload["site_attempts"] = attempts
            event_target = f" ({normalized_vehicle_key})" if normalized_vehicle_key else ""
            self.add_event_to_payload(
                payload,
                "completed_by_user",
                f"{site['name']}{event_target} 使用者已確認完成。",
            )
            self.save_payload(task_id, payload)
            return payload

    def abort_running_task(
        self,
        task_id: str,
        detail: str = "使用者中止登打。",
        *,
        execution_lease_active: bool = False,
        expected_claim_id: str | None = None,
        expected_queue_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            queue_state = worker_queue_state(payload)
            if expected_claim_id is not None:
                normalized_expected_claim_id = str(expected_claim_id or "").strip()
                current_claim_id = str(queue_state.get("claim_id") or "").strip()
                if current_claim_id != normalized_expected_claim_id:
                    raise WorkerClaimConflictError(
                        "worker_claim_conflict",
                        "任務已由新的 worker claim 接手，未中止新一輪登打。",
                    )
            if expected_queue_id is not None:
                normalized_expected_queue_id = str(expected_queue_id or "").strip()
                current_queue_id = str(queue_state.get("queue_id") or "").strip()
                if current_queue_id != normalized_expected_queue_id:
                    raise WorkerClaimConflictError(
                        "worker_claim_conflict",
                        "任務已重新排隊為新一輪 worker 工作，未中止新一輪登打。",
                    )
            site_statuses = payload.get("site_statuses")
            has_running_site = isinstance(site_statuses, dict) and any(
                "running" in str(site.get("status") or "")
                for site in site_statuses.values()
                if isinstance(site, dict)
            )
            overall_status = str(payload.get("overall_status") or "")
            abortable = (
                bool(execution_lease_active)
                or queue_state.get("status") == "queued"
                or worker_claim_lease_is_active(payload)
                or "running" in overall_status
                or has_running_site
            )
            if not abortable:
                raise WorkerClaimConflictError(
                    "task_not_active",
                    "此任務目前沒有執行中的登打或有效 worker claim，未執行中止。",
                )
            attempts = site_attempts(payload)
            aborted_sites = 0
            if isinstance(site_statuses, dict):
                for site_key, site in site_statuses.items():
                    if not isinstance(site, dict):
                        continue
                    status = str(site.get("status") or "")
                    if "running" not in status:
                        continue
                    failed_status = f"{site_key}_failed"
                    site["status"] = failed_status
                    site["detail"] = detail
                    site["updated_at"] = now_text()
                    for field in DIAGNOSTIC_FIELDS:
                        site[field] = ""
                    attempts.setdefault(site_key, []).append(
                        {
                            "attempt_id": str(uuid4()),
                            "time": now_text(),
                            "status": failed_status,
                            "detail": detail,
                            "site_name": str(site.get("name") or site_key),
                            **{field: "" for field in DIAGNOSTIC_FIELDS},
                        }
                    )
                    aborted_sites += 1
            if (
                aborted_sites
                or execution_lease_active
                or "running" in overall_status
                or queue_state.get("status") in {"queued", "claimed"}
            ):
                payload["overall_status"] = "desktop_fast_completed_with_errors"
                if queue_state.get("status") in {"queued", "claimed"}:
                    queue_state["status"] = "completed"
                    queue_state["completed_at"] = now_text()
                    queue_state["last_error"] = detail
                payload["worker_queue"] = queue_state
                payload["site_attempts"] = attempts
                self.add_event_to_payload(payload, "desktop_fast_completed_with_errors", detail)
                self.save_payload(task_id, payload)
            return payload

    def expire_stale_running_sites(self, task_id: str, max_age_seconds: int, detail: str) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            if worker_claim_lease_is_active(payload):
                return payload
            site_statuses = payload.get("site_statuses")
            attempts = site_attempts(payload)
            expired_sites = 0
            now = datetime.now()
            max_age_seconds = max(60, int(max_age_seconds))
            if isinstance(site_statuses, dict):
                for site_key, site in site_statuses.items():
                    if not isinstance(site, dict):
                        continue
                    status = str(site.get("status") or "")
                    if "running" not in status:
                        continue
                    raw_updated_at = str(site.get("updated_at") or "")
                    try:
                        updated_at = datetime.fromisoformat(raw_updated_at)
                    except ValueError:
                        continue
                    if (now - updated_at).total_seconds() <= max_age_seconds:
                        continue
                    failed_status = f"{site_key}_failed"
                    site["status"] = failed_status
                    site["detail"] = detail
                    site["updated_at"] = now_text()
                    for field in DIAGNOSTIC_FIELDS:
                        site[field] = ""
                    attempts.setdefault(site_key, []).append(
                        {
                            "attempt_id": str(uuid4()),
                            "time": now_text(),
                            "status": failed_status,
                            "detail": detail,
                            "site_name": str(site.get("name") or site_key),
                            **{field: "" for field in DIAGNOSTIC_FIELDS},
                        }
                    )
                    expired_sites += 1
            if expired_sites:
                payload["overall_status"] = "desktop_fast_completed_with_errors"
                queue_state = worker_queue_state(payload)
                if queue_state.get("status") in {"queued", "claimed"}:
                    queue_state["status"] = "completed"
                    queue_state["completed_at"] = now_text()
                    queue_state["last_error"] = detail
                payload["worker_queue"] = queue_state
                payload["site_attempts"] = attempts
                self.add_event_to_payload(payload, "desktop_fast_completed_with_errors", detail)
                self.save_payload(task_id, payload)
            return payload

    def delete(self, task_id: str) -> None:
        path = self.path_for(task_id)
        with self._lock:
            if not path.exists():
                raise FileNotFoundError(task_id)
            payload = self.get(task_id)
            if task_payload_is_active_for_edit(payload):
                raise TaskActiveError(task_id)
            path.unlink()

    def path_for(self, task_id: str) -> Path:
        value = str(task_id or "")
        if (
            not value
            or value != value.strip()
            or len(value) > 128
            or value in {".", ".."}
            or value.endswith(".")
            or any(not (character.isalnum() or character in {"-", "_", "."}) for character in value)
        ):
            raise FileNotFoundError(value)
        return self.tasks_dir / f"{value}.json"

    def save_payload(self, task_id: str, payload: dict[str, Any]) -> None:
        payload["updated_at"] = now_text()
        path = self.path_for(task_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def add_event_to_payload(
        self,
        payload: dict[str, Any],
        status: str,
        detail: str = "",
        extra: dict[str, str] | None = None,
    ) -> None:
        event = {
            "time": now_text(),
            "status": status,
            "detail": detail,
        }
        if extra:
            event.update({str(key): str(value) for key, value in extra.items()})
        payload.setdefault("events", []).append(event)

    def cleanup(self, max_age_hours: int = 24, completed_max_age_hours: int = COMPLETED_TASK_HISTORY_HOURS) -> None:
        # Unfinished work is an operational queue and audit trail, not a cache.
        # Never remove it merely because the public-duty PC was offline for a day.
        # The legacy max_age_hours argument remains for caller compatibility.
        _ = max_age_hours
        completed_cutoff = datetime.now() - timedelta(hours=completed_max_age_hours)
        for path in self.tasks_dir.glob("*.json"):
            payload = self._read_payload_or_quarantine(path)
            if payload is None:
                continue
            if self._is_fully_done(payload) and self._is_expired(payload, completed_cutoff):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _is_expired(self, payload: dict[str, Any], cutoff: datetime) -> bool:
        raw_time = str(payload.get("updated_at") or payload.get("created_at") or "")
        try:
            updated_at = datetime.fromisoformat(raw_time)
        except ValueError:
            return False
        return updated_at < cutoff

    def _is_fully_done(self, payload: dict[str, Any]) -> bool:
        site_statuses = payload.get("site_statuses")
        if not isinstance(site_statuses, dict) or not site_statuses:
            return False
        try:
            has_fuel_record = AmbulanceReturnRequest.from_dict(dict(payload.get("task") or {})).has_fuel_record()
        except (TypeError, ValueError):
            has_fuel_record = True
        fuel_site = dict(site_statuses.get("fuel_record") or {})
        fuel_cleanup_pending = "waiting_confirmation" in str(fuel_site.get("status") or "")
        expected_site_keys = [
            site.key
            for site in SITE_DEFINITIONS
            if site.key != "fuel_record" or has_fuel_record or fuel_cleanup_pending
        ]
        for site_key in expected_site_keys:
            site = site_statuses.get(site_key)
            if not isinstance(site, dict):
                return False
            status = str(site.get("status") or "")
            if status not in SUCCESS_SITE_STATUSES and not status.endswith("_saved"):
                return False
        return True

    def _read_payload_or_quarantine(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            return None
        except (UnicodeError, json.JSONDecodeError):
            self._quarantine_corrupt_path(path)
            return None
        if not isinstance(payload, dict):
            self._quarantine_corrupt_path(path)
            return None
        task = payload.get("task")
        if not isinstance(task, dict):
            self._quarantine_corrupt_path(path)
            return None
        task_id = str(task.get("task_id") or path.stem).strip()
        try:
            expected_path = self.path_for(task_id)
        except FileNotFoundError:
            self._quarantine_corrupt_path(path)
            return None
        if expected_path.name != path.name:
            self._quarantine_corrupt_path(path)
            return None
        return payload

    def _quarantine_corrupt_path(self, path: Path) -> Path | None:
        quarantine_dir = self.tasks_dir / "quarantine"
        try:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            target = quarantine_dir / f"{path.stem}-{timestamp}-{uuid4().hex}.json.corrupt"
            path.replace(target)
            return target
        except OSError:
            return None


def _vehicle_checkpoint_signature(request: AmbulanceReturnRequest, site_key: str) -> tuple[object, ...]:
    common = (
        str(request.case_id or "").strip(),
        str(request.case_date or "").strip(),
        str(request.case_time or "").strip(),
        str(request.case_address or "").strip(),
    )
    if site_key == "duty_work_log":
        return common + (
            str(request.vehicle or "").strip(),
            str(request.driver or "").strip(),
            str(request.patient_summary or "").strip(),
            str(request.case_reason or "").strip(),
            str(request.work_note or "").strip(),
        )
    if site_key == "vehicle_mileage":
        return common + (
            str(request.vehicle or "").strip(),
            str(request.driver or "").strip(),
            str(request.mileage or "").strip(),
            str(request.return_date or "").strip(),
            str(request.return_time or "").strip(),
        )
    if site_key == "fuel_record":
        fuel = request.fuel_record
        return common + (
            str(request.vehicle or "").strip(),
            bool(fuel.enabled),
            str(fuel.date or "").strip(),
            str(fuel.time or "").strip(),
            str(fuel.driver or "").strip(),
            str(fuel.product or "").strip(),
            str(fuel.quantity or "").strip(),
            str(fuel.unit_price or "").strip(),
        )
    if site_key == "consumables":
        return common + (
            str(request.vehicle or "").strip(),
            tuple(sorted((str(name), int(quantity)) for name, quantity in request.consumables.items())),
        )
    if site_key == "disinfection":
        return common + (
            str(request.vehicle or "").strip(),
            str(request.disinfection or "").strip(),
            tuple(str(item) for item in request.disinfection_items),
        )
    return common + (str(request.vehicle or "").strip(),)


def _site_may_have_official_record(site: dict[str, Any]) -> bool:
    status = str(site.get("status") or "")
    return status != "not_started" or bool(site.get("vehicle_results"))


def _update_context_vehicle_requests(
    update_context: dict[str, object],
) -> tuple[list[AmbulanceReturnRequest], list[AmbulanceReturnRequest]]:
    previous_task = update_context.get("previous_task")
    current_task = update_context.get("current_task")
    if not isinstance(previous_task, dict) or not isinstance(current_task, dict):
        return [], []
    try:
        previous = AmbulanceReturnRequest.from_dict(previous_task).vehicle_requests()
        current = AmbulanceReturnRequest.from_dict(current_task).vehicle_requests()
    except (TypeError, ValueError):
        return [], []
    return previous, current


def _vehicle_request_key(request: AmbulanceReturnRequest, index: int) -> str:
    return str(request.vehicle or "").strip() or f"{index}車"


def _fuel_record_became_enabled(update_context: dict[str, object]) -> bool:
    previous, current = _update_context_vehicle_requests(update_context)
    previous_enabled = {
        _vehicle_request_key(request, index)
        for index, request in enumerate(previous, start=1)
        if request.fuel_record.enabled
    }
    current_enabled = {
        _vehicle_request_key(request, index)
        for index, request in enumerate(current, start=1)
        if request.fuel_record.enabled
    }
    return bool(current_enabled - previous_enabled)


def _manual_record_removal_reason(update_context: dict[str, object], site_key: str) -> str:
    previous, current = _update_context_vehicle_requests(update_context)
    if not previous:
        return ""
    previous_by_key = {
        _vehicle_request_key(request, index): request
        for index, request in enumerate(previous, start=1)
    }
    current_by_key = {
        _vehicle_request_key(request, index): request
        for index, request in enumerate(current, start=1)
    }
    if site_key == "fuel_record":
        removed_keys = [
            key
            for key, request in previous_by_key.items()
            if request.fuel_record.enabled
            and (key not in current_by_key or not current_by_key[key].fuel_record.enabled)
        ]
    elif site_key in {"duty_work_log", "vehicle_mileage", "consumables", "disinfection"}:
        removed_keys = [key for key in previous_by_key if key not in current_by_key]
    else:
        removed_keys = []
    if not removed_keys:
        return ""
    labels = "、".join(removed_keys)
    return (
        f"已有官方紀錄對應的車輛已移除或取消：{labels}；"
        "請到官方網頁人工刪除舊資料，並人工新增或更新所有現行車輛資料，"
        "完整核對後再按「已確認」。"
    )


def _unchanged_vehicle_checkpoint_keys(
    previous_task: dict[str, object],
    current_task: dict[str, object],
    site_key: str,
) -> set[str]:
    try:
        previous_requests = AmbulanceReturnRequest.from_dict(previous_task).vehicle_requests()
        current_requests = AmbulanceReturnRequest.from_dict(current_task).vehicle_requests()
    except (TypeError, ValueError):
        return set()
    unchanged: set[str] = set()
    for index, (previous, current) in enumerate(zip(previous_requests, current_requests), start=1):
        previous_key = str(previous.vehicle or "").strip() or f"{index}車"
        current_key = str(current.vehicle or "").strip() or f"{index}車"
        if previous_key != current_key:
            continue
        if _vehicle_checkpoint_signature(previous, site_key) == _vehicle_checkpoint_signature(current, site_key):
            unchanged.add(current_key)
    return unchanged


def expected_vehicle_result_keys(payload: dict[str, Any], site_key: str = "") -> list[str]:
    try:
        requests = AmbulanceReturnRequest.from_dict(dict(payload.get("task") or {})).vehicle_requests()
    except (TypeError, ValueError):
        return []
    keys: list[str] = []
    for index, vehicle_request in enumerate(requests, start=1):
        if site_key == "fuel_record" and not vehicle_request.fuel_record.enabled:
            continue
        key = str(getattr(vehicle_request, "vehicle", "") or "").strip() or f"{index}車"
        if key not in keys:
            keys.append(key)
    return keys


def aggregate_vehicle_site_status(payload: dict[str, Any], site_key: str, results: dict[str, Any]) -> str:
    expected_keys = expected_vehicle_result_keys(payload, site_key)
    if not expected_keys:
        expected_keys = [str(key) for key in results]
    statuses = [str(dict(results.get(key) or {}).get("status") or "") for key in expected_keys]
    if not statuses or any(not status for status in statuses):
        return f"{site_key}_running"
    if all(status in SUCCESS_SITE_STATUSES or status.endswith("_saved") for status in statuses):
        return f"{site_key}_saved"
    if any("waiting_confirmation" in status for status in statuses):
        return f"{site_key}_waiting_confirmation"
    if any("failed" in status or "error" in status for status in statuses):
        return f"{site_key}_failed"
    if any("running" in status for status in statuses):
        return f"{site_key}_running"
    return next(
        status
        for status in statuses
        if status not in SUCCESS_SITE_STATUSES and not status.endswith("_saved")
    )


def vehicle_site_result_detail(payload: dict[str, Any], site_key: str, results: dict[str, Any]) -> str:
    expected_keys = expected_vehicle_result_keys(payload, site_key) or [str(key) for key in results]
    details: list[str] = []
    for key in expected_keys:
        record = results.get(key)
        if not isinstance(record, dict):
            details.append(f"{key}: 等待回報")
            continue
        detail = str(record.get("detail") or record.get("status") or "").strip()
        label = str(record.get("vehicle_label") or key).strip()
        details.append(f"{label}: {detail}" if detail else label)
    return " | ".join(details)


def task_payload_is_active_for_edit(payload: dict[str, Any]) -> bool:
    queue_status = worker_queue_state(payload).get("status")
    if queue_status == "claimed":
        return True
    overall_status = str(payload.get("overall_status") or "")
    if "running" in overall_status or overall_status == "claimed_by_worker":
        return True
    site_statuses = payload.get("site_statuses")
    if not isinstance(site_statuses, dict):
        return False
    return any(
        "running" in str(site.get("status") or "")
        for site in site_statuses.values()
        if isinstance(site, dict)
    )


def worker_queue_overall_status_is_terminal(status: str) -> bool:
    value = str(status or "").strip().lower()
    return value.startswith("desktop_fast_completed") or value in {"failed", "worker_failed"}


def initial_site_statuses() -> dict[str, dict[str, str]]:
    return {
        site.key: {
            "key": site.key,
            "name": site.name,
            "url": site.url,
            "status": "not_started",
            "detail": "",
            "updated_at": "",
            **{field: "" for field in DIAGNOSTIC_FIELDS},
        }
        for site in SITE_DEFINITIONS
    }


def initial_site_attempts() -> dict[str, list[dict[str, str]]]:
    return {site.key: [] for site in SITE_DEFINITIONS}


def initial_worker_queue_state() -> dict[str, str]:
    return {
        "status": "idle",
        "queued_at": "",
        "claimed_at": "",
        "lease_expires_at": "",
        "last_heartbeat_at": "",
        "queue_id": "",
        "claim_id": "",
        "claim_attempt": "0",
        "completed_at": "",
        "worker_id": "",
        "last_error": "",
    }


def worker_queue_state(payload: dict[str, Any]) -> dict[str, str]:
    existing = payload.get("worker_queue")
    if isinstance(existing, dict):
        merged = {**initial_worker_queue_state(), **{str(key): str(value) for key, value in existing.items()}}
        return merged
    status = str(payload.get("overall_status") or "")
    state = initial_worker_queue_state()
    if status == "queued_for_worker":
        state["status"] = "queued"
    elif status == "claimed_by_worker":
        state["status"] = "claimed"
    return state


def worker_claim_attempt(
    payload: dict[str, Any],
    queue_state: dict[str, str] | None = None,
) -> int:
    """Return a monotonic claim generation, including legacy payload evidence."""

    state = queue_state if queue_state is not None else worker_queue_state(payload)
    try:
        attempt = max(0, int(str(state.get("claim_attempt") or "0")))
    except (TypeError, ValueError):
        attempt = 0

    if str(state.get("status") or "").strip() == "claimed":
        attempt = max(attempt, 1)
    if any(
        str(state.get(field) or "").strip()
        for field in (
            "claimed_at",
            "lease_expires_at",
            "last_heartbeat_at",
            "claim_id",
            "worker_id",
        )
    ):
        attempt = max(attempt, 1)

    worker = payload.get("worker")
    if isinstance(worker, dict) and any(str(value or "").strip() for value in worker.values()):
        attempt = max(attempt, 1)

    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            status = str(event.get("status") or "").strip()
            if status == "worker_claim_reclaimed":
                attempt = max(attempt, 2)
            elif status == "claimed_by_worker":
                attempt = max(attempt, 1)
    return attempt


def worker_claim_lease_is_active(payload: dict[str, Any], now: datetime | None = None) -> bool:
    queue_state = worker_queue_state(payload)
    if queue_state.get("status") != "claimed":
        return False
    raw_expiry = str(queue_state.get("lease_expires_at") or "").strip()
    if not raw_expiry:
        return False
    try:
        expires_at = datetime.fromisoformat(raw_expiry)
    except ValueError:
        return False
    return expires_at > (now or datetime.now())


def site_attempts(payload: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    existing = payload.get("site_attempts")
    normalized = initial_site_attempts()
    if not isinstance(existing, dict):
        return normalized
    for site_key, entries in existing.items():
        key = str(site_key)
        if key not in normalized:
            normalized[key] = []
        if not isinstance(entries, list):
            continue
        normalized[key] = [
            {
                "attempt_id": str(entry.get("attempt_id") or ""),
                "time": str(entry.get("time") or ""),
                "status": str(entry.get("status") or ""),
                "detail": str(entry.get("detail") or ""),
                "site_name": str(entry.get("site_name") or ""),
                "vehicle_key": str(entry.get("vehicle_key") or ""),
                **{field: str(entry.get(field) or "") for field in DIAGNOSTIC_FIELDS},
            }
            for entry in entries
            if isinstance(entry, dict)
        ]
    return normalized
