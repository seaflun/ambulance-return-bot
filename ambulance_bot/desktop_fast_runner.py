from __future__ import annotations

import threading
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
from .task_store import JsonTaskStore
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
                lambda: run_vehicle_mileage_task(
                    request,
                    self.artifacts_dir,
                    profile_name=f"vehicle_mileage_profile_{profile_suffix}",
                    use_session_lock=False,
                    tile_name="vehicle_mileage",
                    force_new_driver=True,
                ),
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
            return lambda: run_vehicle_mileage_task(
                request,
                self.artifacts_dir,
                profile_name=f"vehicle_mileage_profile_{profile_suffix}",
                use_session_lock=False,
                tile_name="vehicle_mileage",
                force_new_driver=True,
                update_context=self._site_update_context(request.task_id, site_key),
            )
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

    def _run_consumables(self, request, profile_suffix: str) -> SiteAutomationResult:
        driver = login_acs_and_get_driver(
            profile_name=f"consumables_profile_{profile_suffix}",
            tile_name="consumables",
            task=request,
        )
        detail = open_consumable_record_for_task(driver, request)
        status = "consumables_saved" if save_consumables_record_enabled() else "consumables_prefilled"
        return SiteAutomationResult("consumables", SITE_NAMES["consumables"], status, detail)

    def _run_disinfection(self, request, profile_suffix: str):
        driver = login_disinfection_and_get_driver(
            profile_name=f"disinfection_profile_{profile_suffix}",
            tile_name="disinfection",
        )
        return run_disinfection_task(
            request,
            self.artifacts_dir,
            existing_driver=driver,
            profile_name=f"disinfection_profile_{profile_suffix}",
            use_session_lock=False,
            tile_name="disinfection",
            force_new_driver=True,
        )


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
