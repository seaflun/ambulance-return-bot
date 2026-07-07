from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .adapters import SITE_DEFINITIONS, SiteAutomationResult
from .models import AmbulanceReturnRequest
from .site_diagnostics import DIAGNOSTIC_FIELDS, result_with_diagnostics


SUCCESS_SITE_STATUSES = {
    "completed_by_user",
    "duty_work_log_saved",
    "vehicle_mileage_saved",
    "disinfection_saved",
    "consumables_saved",
}


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JsonTaskStore:
    def __init__(self, tasks_dir: Path) -> None:
        self.tasks_dir = tasks_dir
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def create(self, request: AmbulanceReturnRequest) -> dict[str, Any]:
        payload = {
            "task": request.to_dict(),
            "created_at": now_text(),
            "updated_at": now_text(),
            "overall_status": "created",
            "worker_queue": initial_worker_queue_state(),
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
            request.task_id = task_id
            payload["task"] = request.to_dict()
            payload["updated_at"] = now_text()
            changed_site_keys = set(changed_site_keys or set())
            updated_sites = self._mark_changed_sites_for_update(payload, changed_site_keys, site_update_contexts or {})
            if updated_sites:
                payload["overall_status"] = "task_updated_needs_site_update"
                payload["worker_queue"] = initial_worker_queue_state()
            else:
                payload["overall_status"] = str(payload.get("overall_status") or "created")
            self.add_event_to_payload(payload, "task_updated", "任務內容已修改。")
            self.save_payload(task_id, payload)
            return payload

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
            if status not in SUCCESS_SITE_STATUSES and not status.endswith("_saved"):
                continue
            site["status"] = f"{site_key}_needs_update"
            site["detail"] = "任務內容已修改，請更新此站。"
            site["updated_at"] = now_text()
            context = site_update_contexts.get(site_key)
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
            return [json.loads(path.read_text(encoding="utf-8")) for path in paths[:limit]]

    def queue_for_worker(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            payload["overall_status"] = "queued_for_worker"
            queue_state = worker_queue_state(payload)
            queue_state.update(
                {
                    "status": "queued",
                    "queued_at": now_text(),
                    "claimed_at": "",
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
                payload = json.loads(path.read_text(encoding="utf-8"))
                if worker_queue_state(payload).get("status") != "queued":
                    continue
                task_id = payload["task"]["task_id"]
                payload["overall_status"] = "claimed_by_worker"
                queue_state = worker_queue_state(payload)
                queue_state.update(
                    {
                        "status": "claimed",
                        "claimed_at": now_text(),
                        "worker_id": worker_id,
                    }
                )
                payload["worker_queue"] = queue_state
                payload["worker"] = {
                    "id": worker_id,
                    "claimed_at": now_text(),
                }
                self.add_event_to_payload(payload, "claimed_by_worker", f"公務電腦 worker 已領取：{worker_id}")
                self.save_payload(task_id, payload)
                return payload
        return None

    def set_overall_status(self, task_id: str, status: str, detail: str = "") -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            payload["overall_status"] = status
            queue_state = worker_queue_state(payload)
            if status == "queued_for_worker":
                queue_state["status"] = "queued"
                queue_state["queued_at"] = queue_state.get("queued_at") or now_text()
            elif status == "claimed_by_worker":
                queue_state["status"] = "claimed"
                queue_state["claimed_at"] = queue_state.get("claimed_at") or now_text()
            elif queue_state.get("status") in {"queued", "claimed"}:
                queue_state["status"] = "completed"
                queue_state["completed_at"] = now_text()
                if "failed" in status or "error" in status:
                    queue_state["last_error"] = detail
            payload["worker_queue"] = queue_state
            self.add_event_to_payload(payload, status, detail)
            self.save_payload(task_id, payload)
            return payload

    def update_site_result(self, task_id: str, result: SiteAutomationResult) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            result = result_with_diagnostics(result)
            site = payload["site_statuses"][result.key]
            attempts = site_attempts(payload)
            if site.get("status") == "completed_by_user":
                self.add_event_to_payload(payload, result.status, f"{result.name}: 背景狀態已略過，因使用者已確認完成。")
                self.save_payload(task_id, payload)
                return payload
            site["status"] = result.status
            site["detail"] = result.detail
            site["updated_at"] = now_text()
            if result.status in SUCCESS_SITE_STATUSES or result.status.endswith("_saved"):
                site.pop("update_context", None)
            for field in DIAGNOSTIC_FIELDS:
                site[field] = str(getattr(result, field, "") or "")
            attempt = {
                "attempt_id": str(uuid4()),
                "time": now_text(),
                "status": result.status,
                "detail": result.detail,
                "site_name": result.name,
            }
            for field in DIAGNOSTIC_FIELDS:
                attempt[field] = str(getattr(result, field, "") or "")
            attempts.setdefault(result.key, []).append(attempt)
            payload["site_attempts"] = attempts
            self.add_event_to_payload(
                payload,
                result.status,
                f"{result.name}: {result.detail}",
                {field: str(getattr(result, field, "") or "") for field in DIAGNOSTIC_FIELDS},
            )
            self.save_payload(task_id, payload)
            return payload

    def mark_site_completed(self, task_id: str, site_key: str) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            site = payload["site_statuses"][site_key]
            attempts = site_attempts(payload)
            site["status"] = "completed_by_user"
            site["detail"] = "使用者已人工確認完成。"
            site["updated_at"] = now_text()
            for field in DIAGNOSTIC_FIELDS:
                site[field] = ""
            attempts.setdefault(site_key, []).append(
                {
                    "attempt_id": str(uuid4()),
                    "time": now_text(),
                    "status": "completed_by_user",
                    "detail": site["detail"],
                    "site_name": site["name"],
                    **{field: "" for field in DIAGNOSTIC_FIELDS},
                }
            )
            payload["site_attempts"] = attempts
            self.add_event_to_payload(payload, "completed_by_user", f"{site['name']} 使用者已確認完成。")
            self.save_payload(task_id, payload)
            return payload

    def abort_running_task(self, task_id: str, detail: str = "使用者中止登打。") -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            site_statuses = payload.get("site_statuses")
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
            overall_status = str(payload.get("overall_status") or "")
            queue_state = worker_queue_state(payload)
            if aborted_sites or "running" in overall_status or queue_state.get("status") in {"queued", "claimed"}:
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

    def delete(self, task_id: str) -> None:
        path = self.path_for(task_id)
        with self._lock:
            if not path.exists():
                raise FileNotFoundError(task_id)
            path.unlink()

    def path_for(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

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

    def cleanup(self, max_age_hours: int = 24) -> None:
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        for path in self.tasks_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if self._is_expired(payload, cutoff):
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
        for site in site_statuses.values():
            status = str(site.get("status") or "")
            if status not in SUCCESS_SITE_STATUSES and not status.endswith("_saved"):
                return False
        return True


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
                **{field: str(entry.get(field) or "") for field in DIAGNOSTIC_FIELDS},
            }
            for entry in entries
            if isinstance(entry, dict)
        ]
    return normalized
