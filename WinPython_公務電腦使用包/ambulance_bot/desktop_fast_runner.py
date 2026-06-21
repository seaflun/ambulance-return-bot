from __future__ import annotations

import threading
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Callable

from consumables_login import login_acs_and_get_driver, open_consumable_record_for_task, save_consumables_record_enabled
from disinfect import login_and_get_driver as login_disinfection_and_get_driver

from .adapters import SITE_DEFINITIONS, SiteAutomationResult
from .login_audit import login_audit_for_site, with_login_audit
from .manual_task_lock import clear_manual_task_lock, set_manual_task_lock
from .selenium_local import run_disinfection_task, run_local_selenium_task, run_vehicle_mileage_task
from .site_diagnostics import make_site_result
from .task_store import JsonTaskStore, now_text
from .window_layout import maximize_worker_site_windows


SITE_NAMES = {site.key: site.name for site in SITE_DEFINITIONS}


class DesktopFastRunner:
    def __init__(
        self,
        artifacts_dir: Path,
        store: JsonTaskStore | None = None,
        event_callback: Callable[[dict, str], None] | None = None,
    ) -> None:
        self.artifacts_dir = artifacts_dir
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.store = store or JsonTaskStore(self.artifacts_dir / "tasks")
        self.event_callback = event_callback
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def start_existing(self, task_id: str) -> str:
        with self._lock:
            if task_id in self._running:
                return task_id
            self._running.add(task_id)
        thread = threading.Thread(target=self._run, args=(task_id,), daemon=True)
        thread.start()
        return task_id

    def start_site(self, task_id: str, site_key: str) -> str:
        if site_key not in SITE_NAMES:
            raise KeyError(site_key)
        run_key = f"{task_id}:{site_key}"
        with self._lock:
            if run_key in self._running:
                return task_id
            self._running.add(run_key)
        thread = threading.Thread(target=self._run_single_site, args=(task_id, site_key, run_key), daemon=True)
        thread.start()
        return task_id

    def wait_for_idle(self, timeout_seconds: float = 5.0) -> bool:
        import time

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            with self._lock:
                if not self._running:
                    return True
            time.sleep(0.05)
        return False

    def _run(self, task_id: str) -> None:
        failures = 0
        lock_owner = f"desktop_fast:{task_id}"
        set_manual_task_lock(self.artifacts_dir, lock_owner)
        self.store.set_overall_status(task_id, "desktop_fast_running", "本機快速執行已啟動。")
        self._notify(task_id, "四站登打開始")
        request = self.store.request_for(task_id)
        profile_suffix = task_id.replace("-", "_")
        site_runners = [
            (
                "duty_work_log",
                lambda: run_local_selenium_task(
                    request,
                    self.artifacts_dir,
                    profile_name=f"duty_work_log_profile_{profile_suffix}",
                    use_session_lock=False,
                    tile_name="duty_work_log",
                    force_new_driver=True,
                ),
            ),
            (
                "vehicle_mileage",
                lambda: self._run_vehicle_mileage(request, profile_suffix),
            ),
            (
                "consumables",
                lambda: self._run_consumables(request, profile_suffix),
            ),
            (
                "disinfection",
                lambda: self._run_disinfection(request, profile_suffix),
            ),
        ]
        try:
            folder_detail = self._ensure_record_folders(request)
            if folder_detail:
                self.store.set_overall_status(task_id, "desktop_fast_running", folder_detail)
            for site_key, action in site_runners:
                failed = self._run_site(task_id, site_key, action)
                if failed:
                    failures += 1
            if failures:
                self.store.set_overall_status(
                    task_id,
                    "desktop_fast_completed_with_errors",
                    f"本機快速執行完成，{failures} 站失敗；已略過失敗站並接續後續站別。",
                )
                self._notify(task_id, "四站登打部分失敗")
            else:
                self.store.set_overall_status(task_id, "desktop_fast_completed", "本機快速執行完成。")
                self._notify(task_id, "四站登打成功")
        finally:
            maximize_worker_site_windows()
            clear_manual_task_lock(self.artifacts_dir, lock_owner)
            with self._lock:
                self._running.discard(task_id)

    def _run_single_site(self, task_id: str, site_key: str, run_key: str) -> None:
        lock_owner = f"desktop_fast:{run_key}"
        set_manual_task_lock(self.artifacts_dir, lock_owner)
        self.store.set_overall_status(task_id, "desktop_fast_running", f"本機快速執行中：{SITE_NAMES[site_key]}。")
        self._notify(task_id, f"單站登打開始：{SITE_NAMES[site_key]}")
        request = self.store.request_for(task_id)
        profile_suffix = task_id.replace("-", "_")
        try:
            failed = self._run_site(task_id, site_key, self._site_action(request, profile_suffix, site_key))
            if failed:
                self.store.set_overall_status(
                    task_id,
                    "desktop_fast_completed_with_errors",
                    f"單站登打失敗：{SITE_NAMES[site_key]} 未完成。",
                )
                self._notify(task_id, f"單站登打失敗：{SITE_NAMES[site_key]}")
            else:
                self.store.set_overall_status(task_id, "desktop_fast_completed", f"單站登打完成：{SITE_NAMES[site_key]}。")
                self._notify(task_id, f"單站登打成功：{SITE_NAMES[site_key]}")
        finally:
            maximize_worker_site_windows()
            clear_manual_task_lock(self.artifacts_dir, lock_owner)
            with self._lock:
                self._running.discard(run_key)

    def _site_action(self, request, profile_suffix: str, site_key: str):
        if site_key == "duty_work_log":
            return lambda: run_local_selenium_task(
                request,
                self.artifacts_dir,
                profile_name=f"duty_work_log_profile_{profile_suffix}",
                use_session_lock=False,
                tile_name="duty_work_log",
                force_new_driver=True,
            )
        if site_key == "vehicle_mileage":
            return lambda: self._run_vehicle_mileage(request, profile_suffix)
        if site_key == "disinfection":
            return lambda: self._run_disinfection(request, profile_suffix)
        if site_key == "consumables":
            return lambda: self._run_consumables(request, profile_suffix)
        raise KeyError(site_key)

    def _run_site(self, task_id: str, site_key: str, action) -> int:
        site_name = SITE_NAMES[site_key]
        login_audit = login_audit_for_site(site_key, self.store.request_for(task_id))
        if _site_is_complete(str(self.store.get(task_id).get("site_statuses", {}).get(site_key, {}).get("status") or "")):
            self.store.set_overall_status(task_id, "desktop_fast_running", f"{site_name} 已完成，略過。")
            self._notify(task_id, f"{site_name} 略過")
            return False
        self.store.update_site_result(
            task_id,
            SiteAutomationResult(site_key, site_name, f"{site_key}_running", with_login_audit("本機快速執行中。", login_audit)),
        )
        self._notify(task_id, f"{site_name} 開始")
        try:
            result = action()
            result = _result_with_login_audit(result, login_audit)
            result = make_site_result(site_key, site_name, str(result.status), str(result.detail))
            self.store.update_site_result(
                task_id,
                result,
            )
            self._notify(task_id, f"{site_name} 結果")
            return _result_blocks_next(result)
        except Exception as exc:
            self.store.update_site_result(
                task_id,
                make_site_result(site_key, site_name, f"{site_key}_failed", str(exc), exc),
            )
            self._notify(task_id, f"{site_name} 失敗")
            return True

    def _notify(self, task_id: str, action: str) -> None:
        if not self.event_callback:
            return
        try:
            self.event_callback(self.store.get(task_id), action)
        except Exception:
            pass

    def _site_update_context(self, task_id: str, site_key: str) -> dict[str, object] | None:
        site = dict(self.store.get(task_id).get("site_statuses", {}).get(site_key) or {})
        context = site.get("update_context")
        return context if isinstance(context, dict) else None

    def _run_vehicle_mileage(self, request, profile_suffix: str) -> SiteAutomationResult:
        vehicle_requests = request.vehicle_requests()
        if len(vehicle_requests) <= 1:
            return run_vehicle_mileage_task(
                request,
                self.artifacts_dir,
                profile_name=f"vehicle_mileage_profile_{profile_suffix}",
                use_session_lock=False,
                tile_name="vehicle_mileage",
                force_new_driver=True,
                update_context=self._site_update_context(request.task_id, "vehicle_mileage"),
            )
        debugger_port = _site_debugger_port("VEHICLE_MILEAGE_DEBUGGER_PORT", 9234)
        return self._run_per_vehicle_site(
            request,
            "vehicle_mileage",
            lambda vehicle_request, index: run_vehicle_mileage_task(
                vehicle_request,
                self.artifacts_dir,
                profile_name=f"vehicle_mileage_profile_{profile_suffix}",
                debugger_port=debugger_port,
                use_session_lock=False,
                tile_name="vehicle_mileage",
                force_new_driver=index == 1,
                update_context=self._site_update_context(request.task_id, "vehicle_mileage"),
            ),
        )

    def _run_consumables(self, request, profile_suffix: str) -> SiteAutomationResult:
        driver = login_acs_and_get_driver(
            profile_name=f"consumables_profile_{profile_suffix}",
            tile_name="consumables",
            task=request,
        )
        if len(request.vehicle_requests()) <= 1:
            detail = open_consumable_record_for_task(driver, request)
            status = "consumables_saved" if save_consumables_record_enabled() else "consumables_prefilled"
            return SiteAutomationResult("consumables", SITE_NAMES["consumables"], status, detail)
        return self._run_per_vehicle_site(
            request,
            "consumables",
            lambda vehicle_request, index: SiteAutomationResult(
                "consumables",
                SITE_NAMES["consumables"],
                "consumables_saved" if save_consumables_record_enabled() else "consumables_prefilled",
                open_consumable_record_for_task(driver, vehicle_request),
            ),
        )

    def _run_disinfection(self, request, profile_suffix: str):
        driver = login_disinfection_and_get_driver(
            profile_name=f"disinfection_profile_{profile_suffix}",
            tile_name="disinfection",
        )
        if len(request.vehicle_requests()) <= 1:
            return run_disinfection_task(
                request,
                self.artifacts_dir,
                existing_driver=driver,
                profile_name=f"disinfection_profile_{profile_suffix}",
                use_session_lock=False,
                tile_name="disinfection",
                force_new_driver=True,
            )
        return self._run_per_vehicle_site(
            request,
            "disinfection",
            lambda vehicle_request, index: run_disinfection_task(
                vehicle_request,
                self.artifacts_dir,
                existing_driver=driver,
                profile_name=f"disinfection_profile_{profile_suffix}",
                use_session_lock=False,
                tile_name="disinfection",
                force_new_driver=True,
            ),
        )

    def _run_per_vehicle_site(self, request, site_key: str, action) -> SiteAutomationResult:
        site_name = SITE_NAMES[site_key]
        details: list[str] = []
        failures = 0
        ran = 0
        for index, vehicle_request in enumerate(request.vehicle_requests(), start=1):
            vehicle_key = _vehicle_result_key(vehicle_request, index)
            if _vehicle_site_result_is_complete(self._vehicle_site_results(request.task_id, site_key).get(vehicle_key)):
                details.append(f"{vehicle_key}: skipped")
                continue
            ran += 1
            try:
                result = action(vehicle_request, index)
            except Exception as exc:
                result = make_site_result(site_key, site_name, f"{site_key}_failed", str(exc), exc)
            self._record_vehicle_site_result(request.task_id, site_key, vehicle_key, result)
            details.append(f"{vehicle_key}: {getattr(result, 'detail', '')}")
            if _result_blocks_next(result):
                failures += 1
        if failures:
            status = f"{site_key}_failed"
        elif ran == 0:
            status = f"{site_key}_saved"
        else:
            status = _aggregate_vehicle_site_status(site_key, request.vehicle_requests(), self._vehicle_site_results(request.task_id, site_key))
        return SiteAutomationResult(site_key, site_name, status, " | ".join(details))

    def _vehicle_site_results(self, task_id: str, site_key: str) -> dict[str, dict[str, str]]:
        site = dict(self.store.get(task_id).get("site_statuses", {}).get(site_key) or {})
        results = site.get("vehicle_results")
        if not isinstance(results, dict):
            return {}
        return {str(key): dict(value) for key, value in results.items() if isinstance(value, dict)}

    def _record_vehicle_site_result(self, task_id: str, site_key: str, vehicle_key: str, result) -> None:
        payload = self.store.get(task_id)
        site = payload["site_statuses"][site_key]
        results = dict(site.get("vehicle_results") or {})
        results[vehicle_key] = {
            "status": str(getattr(result, "status", "") or ""),
            "detail": str(getattr(result, "detail", "") or ""),
            "updated_at": now_text(),
        }
        site["vehicle_results"] = results
        self.store.save_payload(task_id, payload)

    def _ensure_record_folders(self, request) -> str:
        root = Path(os.getenv("AMBULANCE_RECORD_ROOT") or r"W:\救護硬碟\救護登錄器及行車紀錄器")
        created: list[str] = []
        errors: list[str] = []
        for index, vehicle_request in enumerate(request.vehicle_requests(), start=1):
            try:
                folder = root / f"{vehicle_request.service_case_date().year}" / f"{vehicle_request.service_case_date().month}月" / _record_folder_name(vehicle_request, index)
                for child in ("1", "2", "車"):
                    (folder / child).mkdir(parents=True, exist_ok=True)
                created.append(str(folder))
            except Exception as exc:
                errors.append(f"{_vehicle_result_key(vehicle_request, index)}: {exc}")
        if errors:
            return f"record folder warning: {' | '.join(errors)}"
        if created:
            return f"record folders ready: {' | '.join(created)}"
        return ""


def _result_blocks_next(result) -> bool:
    if getattr(result, "ok", True) is False:
        return True
    status = str(getattr(result, "status", "") or "")
    if "failed" in status or "error" in status:
        return True
    if status.startswith("needs_") or "login" in status:
        return True
    return False


def _result_with_login_audit(result, audit: str):
    detail = with_login_audit(str(getattr(result, "detail", "") or ""), audit)
    try:
        return replace(result, detail=detail)
    except (TypeError, ValueError):
        return result


def _site_is_complete(status: str) -> bool:
    value = str(status or "")
    return value == "completed_by_user" or value.endswith("_saved")


def _vehicle_site_result_is_complete(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    return _site_is_complete(str(result.get("status") or ""))


def _vehicle_result_key(request, index: int) -> str:
    vehicle = str(getattr(request, "vehicle", "") or "").strip()
    return vehicle or f"{index}車"


def _aggregate_vehicle_site_status(site_key: str, vehicle_requests: list, results: dict[str, dict[str, str]]) -> str:
    statuses: list[str] = []
    for index, vehicle_request in enumerate(vehicle_requests, start=1):
        vehicle_key = _vehicle_result_key(vehicle_request, index)
        status = str(dict(results.get(vehicle_key) or {}).get("status") or "")
        if status:
            statuses.append(status)
    if statuses and all(_site_is_complete(status) for status in statuses):
        return f"{site_key}_saved"
    if statuses and all("failed" not in status and "error" not in status for status in statuses):
        return statuses[-1]
    return f"{site_key}_failed"


def _site_debugger_port(env_name: str, default: int) -> int:
    try:
        return int(os.getenv(env_name, str(default)))
    except ValueError:
        return default


def _record_folder_name(request, index: int) -> str:
    case_date = request.service_case_date()
    hhmm = re.sub(r"\D", "", str(getattr(request, "case_time", "") or ""))[:4]
    if len(hhmm) != 4:
        hhmm = "0000"
    vehicle = str(getattr(request, "vehicle", "") or "").strip()
    vehicle_digits = "".join(ch for ch in vehicle if ch.isdigit())
    vehicle_label = vehicle_digits or re.sub(r'[<>:"/\\|?*\s]+', "", vehicle) or str(index)
    return f"{case_date:%m%d}{hhmm}-{vehicle_label}"
