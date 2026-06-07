from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .adapters import SITE_DEFINITIONS, SiteAutomationResult
from .models import AmbulanceReturnRequest


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
            "site_statuses": {
                site.key: {
                    "key": site.key,
                    "name": site.name,
                    "url": site.url,
                    "status": "not_started",
                    "detail": "",
                    "updated_at": "",
                }
                for site in SITE_DEFINITIONS
            },
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
            self.add_event_to_payload(payload, "queued_for_worker", "任務已排隊，等待公務電腦 worker 執行。")
            self.save_payload(task_id, payload)
            return payload

    def claim_next_for_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self._lock:
            self.cleanup()
            paths = sorted(self.tasks_dir.glob("*.json"), key=lambda item: item.stat().st_mtime)
            for path in paths:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("overall_status") != "queued_for_worker":
                    continue
                task_id = payload["task"]["task_id"]
                payload["overall_status"] = "claimed_by_worker"
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
            self.add_event_to_payload(payload, status, detail)
            self.save_payload(task_id, payload)
            return payload

    def update_site_result(self, task_id: str, result: SiteAutomationResult) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            site = payload["site_statuses"][result.key]
            if site.get("status") == "completed_by_user":
                self.add_event_to_payload(payload, result.status, f"{result.name}: 背景狀態已略過，因使用者已確認完成。")
                self.save_payload(task_id, payload)
                return payload
            site["status"] = result.status
            site["detail"] = result.detail
            site["updated_at"] = now_text()
            self.add_event_to_payload(payload, result.status, f"{result.name}: {result.detail}")
            self.save_payload(task_id, payload)
            return payload

    def mark_site_completed(self, task_id: str, site_key: str) -> dict[str, Any]:
        with self._lock:
            payload = self.get(task_id)
            site = payload["site_statuses"][site_key]
            site["status"] = "completed_by_user"
            site["detail"] = "使用者已人工確認完成。"
            site["updated_at"] = now_text()
            self.add_event_to_payload(payload, "completed_by_user", f"{site['name']} 使用者已確認完成。")
            self.save_payload(task_id, payload)
            return payload

    def path_for(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def save_payload(self, task_id: str, payload: dict[str, Any]) -> None:
        payload["updated_at"] = now_text()
        path = self.path_for(task_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def add_event_to_payload(self, payload: dict[str, Any], status: str, detail: str = "") -> None:
        payload.setdefault("events", []).append(
            {
                "time": now_text(),
                "status": status,
                "detail": detail,
            }
        )

    def cleanup(self, max_age_hours: int = 24) -> None:
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        for path in self.tasks_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if self._is_expired(payload, cutoff) or self._is_fully_done(payload):
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
