from __future__ import annotations

import threading
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Callable
from uuid import uuid4

from consumables_login import login_acs_and_get_driver, open_consumable_record_for_task, save_consumables_record_enabled
from disinfect import login_and_get_driver as login_disinfection_and_get_driver

from .adapters import SITE_DEFINITIONS, SiteAutomationResult
from .login_audit import login_audit_for_site, with_login_audit
from .manual_task_lock import (
    acquire_manual_task_lock,
    clear_manual_task_lock,
    manual_task_lock_max_age_seconds,
    refresh_manual_task_lock,
    run_with_manual_task_lock_owner,
)
from .selenium_local import run_disinfection_task, run_fuel_record_task, run_local_selenium_task, run_vehicle_mileage_task
from .site_diagnostics import make_site_result
from .task_cancellation import (
    TaskCancellationError,
    clear_task_cancellation,
    task_cancellation_requested,
)
from .task_store import JsonTaskStore, now_text
from .update_safety import ManualUpdateRequiredError, require_safe_automated_update
from .window_layout import maximize_worker_site_windows


SITE_NAMES = {site.key: site.name for site in SITE_DEFINITIONS}
MILEAGE_FUEL_PAIR = ("vehicle_mileage", "fuel_record")
MAX_PARALLEL_SITE_GROUPS = 2
MAX_MANUAL_TASK_LOCK_HEARTBEAT_ERRORS = 3
DEFAULT_RECORD_ROOT = Path(r"W:\救護硬碟\救護密錄器及行車紀錄器")


def active_site_runners(request, profile_suffix: str, runner: "DesktopFastRunner") -> list[tuple[str, Callable[[], object]]]:
    return [site_runner for group in active_site_groups(request, profile_suffix, runner) for site_runner in group]


def active_site_groups(request, profile_suffix: str, runner: "DesktopFastRunner") -> list[list[tuple[str, Callable[[], object]]]]:
    site_groups: list[list[tuple[str, Callable[[], object]]]] = [
        [
            (
                "duty_work_log",
                lambda: run_local_selenium_task(
                    request,
                    runner.artifacts_dir,
                    profile_name=f"duty_work_log_profile_{profile_suffix}",
                    use_session_lock=False,
                    tile_name="duty_work_log",
                    force_new_driver=True,
                    update_context=runner._site_update_context(request.task_id, "duty_work_log"),
                    cancel_check=runner._cancel_check(request.task_id),
                ),
            )
        ],
        [
            (
                "vehicle_mileage",
                lambda: runner._run_vehicle_mileage(request, profile_suffix),
            )
        ],
    ]
    if request.has_fuel_record():
        site_groups[1].append(
            (
                "fuel_record",
                lambda: runner._run_fuel_record(request, profile_suffix),
            )
        )
    site_groups.extend(
        [
            [
                (
                    "consumables",
                    lambda: runner._run_consumables(request, profile_suffix),
                )
            ],
            [
                (
                    "disinfection",
                    lambda: runner._run_disinfection(request, profile_suffix),
                )
            ],
        ]
    )
    return site_groups


def task_site_count_label(request) -> str:
    return "五站" if request.has_fuel_record() else "四站"


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
        self._execution_owners: dict[str, str] = {}
        self._lease_lost_events: dict[str, threading.Event] = {}

    def start_existing(self, task_id: str) -> str:
        with self._lock:
            if self._task_running_locked(task_id):
                return task_id
            self._running.add(task_id)
        lock_owner = self._prepare_execution(task_id, task_id, "目前已有其他登打或查詢執行中，未啟動本任務。")
        if not lock_owner:
            return task_id
        try:
            thread = threading.Thread(target=self._run, args=(task_id, lock_owner), daemon=True)
            thread.start()
        except Exception as exc:
            try:
                self._set_overall_status_owned(
                    task_id,
                    "desktop_fast_completed_with_errors",
                    f"無法啟動本機快速執行線程：{exc}",
                    owner=lock_owner,
                )
            except TaskCancellationError:
                pass
            finally:
                self._release_prepared_execution(task_id, lock_owner, task_id)
        return task_id

    def start_site(self, task_id: str, site_key: str) -> str:
        if site_key not in SITE_NAMES:
            raise KeyError(site_key)
        run_key = f"{task_id}:{site_key}"
        with self._lock:
            if self._task_running_locked(task_id):
                return task_id
            self._running.add(run_key)
        lock_owner = self._prepare_execution(task_id, run_key, "目前已有其他登打或查詢執行中，未啟動單站任務。")
        if not lock_owner:
            return task_id
        try:
            thread = threading.Thread(
                target=self._run_single_site,
                args=(task_id, site_key, run_key, lock_owner),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            try:
                self._set_overall_status_owned(
                    task_id,
                    "desktop_fast_completed_with_errors",
                    f"無法啟動本機單站執行線程：{exc}",
                    owner=lock_owner,
                )
            except TaskCancellationError:
                pass
            finally:
                self._release_prepared_execution(task_id, lock_owner, run_key)
        return task_id

    def _task_running_locked(self, task_id: str) -> bool:
        return bool(self._running)

    def wait_for_idle(self, timeout_seconds: float = 5.0) -> bool:
        import time

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            with self._lock:
                if not self._running:
                    return True
            time.sleep(0.05)
        return False

    def _execution_owner(self, task_id: str) -> str:
        with self._lock:
            return str(self._execution_owners.get(task_id) or "")

    def _prepare_execution(self, task_id: str, running_key: str, busy_detail: str) -> str:
        lock_owner = f"desktop_fast:{task_id}:{uuid4().hex}"
        try:
            acquired = acquire_manual_task_lock(self.artifacts_dir, lock_owner)
        except OSError as exc:
            print(f"[desktop-fast] execution lease acquire failed task={task_id}: {exc}", flush=True)
            acquired = False
        if not acquired:
            print(f"[desktop-fast] execution lease busy task={task_id}: {busy_detail}", flush=True)
            with self._lock:
                self._running.discard(running_key)
            return ""
        try:
            self._register_execution_owner(task_id, lock_owner)
        except Exception as exc:
            print(f"[desktop-fast] execution owner registration failed task={task_id}: {exc}", flush=True)
            try:
                clear_manual_task_lock(self.artifacts_dir, lock_owner)
            except OSError as cleanup_exc:
                print(f"[desktop-fast] execution lease rollback failed task={task_id}: {cleanup_exc}", flush=True)
            with self._lock:
                self._running.discard(running_key)
            return ""
        return lock_owner

    def _release_prepared_execution(self, task_id: str, lock_owner: str, running_key: str) -> bool:
        cancelled = False
        try:
            cancelled = self._task_is_cancelled(task_id)
            if cancelled:
                self._preserve_cancelled_status(task_id)
        except Exception as exc:
            print(f"[desktop-fast] prepared cancellation check failed task={task_id}: {exc}", flush=True)
        finally:
            try:
                clear_task_cancellation(
                    self.artifacts_dir,
                    task_id,
                    execution_owner=lock_owner,
                )
            except Exception as exc:
                print(f"[desktop-fast] prepared cancellation cleanup failed task={task_id}: {exc}", flush=True)
            finally:
                try:
                    clear_manual_task_lock(self.artifacts_dir, lock_owner)
                except OSError as exc:
                    print(f"[desktop-fast] prepared lease cleanup failed task={task_id}: {exc}", flush=True)
                finally:
                    self._unregister_execution_owner(task_id, lock_owner)
                    with self._lock:
                        self._running.discard(running_key)
        return cancelled

    def _register_execution_owner(
        self,
        task_id: str,
        owner: str,
        lease_lost_event: threading.Event | None = None,
    ) -> threading.Event:
        event = lease_lost_event or threading.Event()
        with self._lock:
            self._execution_owners[task_id] = owner
            self._lease_lost_events[task_id] = event
        return event

    def _unregister_execution_owner(self, task_id: str, owner: str) -> None:
        with self._lock:
            if self._execution_owners.get(task_id) == owner:
                self._execution_owners.pop(task_id, None)
                self._lease_lost_events.pop(task_id, None)

    def _lease_lost_event(self, task_id: str) -> threading.Event | None:
        with self._lock:
            return self._lease_lost_events.get(task_id)

    def _task_is_cancelled(self, task_id: str) -> bool:
        owner = self._execution_owner(task_id)
        lease_lost_event = self._lease_lost_event(task_id)
        if lease_lost_event is not None and lease_lost_event.is_set():
            return True
        return bool(
            owner
            and task_cancellation_requested(
                self.artifacts_dir,
                task_id,
                execution_owner=owner,
            )
        )

    def _raise_if_cancelled(self, task_id: str) -> None:
        if self._task_is_cancelled(task_id):
            raise TaskCancellationError("使用者中止登打。")

    def _cancel_check(self, task_id: str) -> Callable[[], None]:
        return lambda: self._raise_if_cancelled(task_id)

    def _run_owned_mutation(
        self,
        task_id: str,
        action: Callable[[], object],
        *,
        allow_cancelled: bool = False,
        owner: str = "",
    ) -> object:
        effective_owner = str(owner or self._execution_owner(task_id)).strip()
        if not effective_owner:
            raise TaskCancellationError("execution lease owner is missing")
        result: list[object] = []

        def mutate() -> None:
            if not allow_cancelled:
                self._raise_if_cancelled(task_id)
            result.append(action())

        if not run_with_manual_task_lock_owner(
            self.artifacts_dir,
            effective_owner,
            task_id,
            mutate,
        ):
            raise TaskCancellationError("execution lease owner changed")
        return result[0] if result else None

    def _set_overall_status_owned(
        self,
        task_id: str,
        status: str,
        detail: str = "",
        *,
        owner: str = "",
    ) -> object:
        return self._run_owned_mutation(
            task_id,
            lambda: self.store.set_overall_status(task_id, status, detail),
            owner=owner,
        )

    def _update_site_result_owned(self, task_id: str, result: SiteAutomationResult) -> object:
        return self._run_owned_mutation(
            task_id,
            lambda: self.store.update_site_result(task_id, result),
        )

    def _update_vehicle_site_result_owned(
        self,
        task_id: str,
        site_key: str,
        vehicle_key: str,
        status: str,
        detail: str,
    ) -> object:
        return self._run_owned_mutation(
            task_id,
            lambda: self.store.update_vehicle_site_result(
                task_id,
                site_key,
                vehicle_key,
                status,
                detail,
            ),
        )

    def _preserve_cancelled_status(self, task_id: str) -> bool:
        owner = self._execution_owner(task_id)
        if not owner:
            return False

        def preserve() -> None:
            payload = self.store.get(task_id)
            statuses = payload.get("site_statuses")
            has_running_site = isinstance(statuses, dict) and any(
                "running" in str(site.get("status") or "")
                for site in statuses.values()
                if isinstance(site, dict)
            )
            if (
                str(payload.get("overall_status") or "") == "desktop_fast_completed_with_errors"
                and not has_running_site
            ):
                return
            self.store.abort_running_task(
                task_id,
                "使用者中止登打。",
                execution_lease_active=True,
            )

        try:
            self._run_owned_mutation(
                task_id,
                preserve,
                allow_cancelled=True,
                owner=owner,
            )
            return True
        except TaskCancellationError:
            return False

    def _finish_execution(
        self,
        task_id: str,
        lock_owner: str,
        running_key: str,
        stop_heartbeat: Callable[[], None],
    ) -> None:
        try:
            maximize_worker_site_windows()
        except Exception as exc:
            print(f"[desktop-fast] window layout cleanup failed: {exc}", flush=True)
        try:
            stop_heartbeat()
        except Exception as exc:
            print(f"[desktop-fast] heartbeat cleanup failed: {exc}", flush=True)
        try:
            if self._task_is_cancelled(task_id):
                self._preserve_cancelled_status(task_id)
        except Exception as exc:
            print(f"[desktop-fast] cancellation status cleanup failed: {exc}", flush=True)
        try:
            clear_task_cancellation(
                self.artifacts_dir,
                task_id,
                execution_owner=lock_owner,
            )
        except Exception as exc:
            print(f"[desktop-fast] cancellation marker cleanup failed task={task_id}: {exc}", flush=True)
        finally:
            try:
                clear_manual_task_lock(self.artifacts_dir, lock_owner)
            finally:
                self._unregister_execution_owner(task_id, lock_owner)
                with self._lock:
                    self._running.discard(running_key)

    def _run(self, task_id: str, prepared_lock_owner: str = "") -> None:
        failures = 0
        lock_owner = prepared_lock_owner or self._prepare_execution(
            task_id,
            task_id,
            "目前已有其他登打或查詢執行中，未啟動本任務。",
        )
        if not lock_owner:
            return
        stop_heartbeat: Callable[[], None] = lambda: None
        try:
            stop_heartbeat = _start_manual_task_lock_heartbeat(
                self.artifacts_dir,
                lock_owner,
                self._lease_lost_event(task_id),
            )
            self._raise_if_cancelled(task_id)
            self._set_overall_status_owned(task_id, "desktop_fast_running", "本機快速執行已啟動。")
            request = self.store.request_for(task_id)
            site_count_label = task_site_count_label(request)
            self._notify(task_id, f"{site_count_label}登打開始")
            profile_suffix = task_id.replace("-", "_")
            site_groups = active_site_groups(request, profile_suffix, self)
            self._raise_if_cancelled(task_id)
            folder_detail = self._ensure_record_folders(request)
            self._raise_if_cancelled(task_id)
            if folder_detail:
                self._set_overall_status_owned(task_id, "desktop_fast_running", folder_detail)
            cancelled = False
            with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SITE_GROUPS) as executor:
                futures = [executor.submit(self._run_site_group, task_id, site_group) for site_group in site_groups]
                for future in as_completed(futures):
                    try:
                        failures += future.result()
                    except TaskCancellationError:
                        cancelled = True
                    except Exception as exc:
                        if self._task_is_cancelled(task_id):
                            cancelled = True
                        else:
                            failures += 1
                            self._set_overall_status_owned(
                                task_id,
                                "desktop_fast_running",
                                f"本機快速執行平行流程例外：{exc}",
                            )
            if cancelled or self._task_is_cancelled(task_id):
                self._preserve_cancelled_status(task_id)
                return
            if failures:
                self._set_overall_status_owned(
                    task_id,
                    "desktop_fast_completed_with_errors",
                    f"本機快速執行完成，{failures} 站失敗；已略過失敗站並接續後續站別。",
                )
                self._notify(task_id, f"{site_count_label}登打部分失敗")
            else:
                self._set_overall_status_owned(task_id, "desktop_fast_completed", "本機快速執行完成。")
                self._notify(task_id, f"{site_count_label}登打成功")
        except TaskCancellationError:
            self._preserve_cancelled_status(task_id)
        except Exception as exc:
            try:
                self._set_overall_status_owned(
                    task_id,
                    "desktop_fast_completed_with_errors",
                    f"本機快速執行未完成：{exc}",
                )
            except TaskCancellationError:
                self._preserve_cancelled_status(task_id)
        finally:
            self._finish_execution(task_id, lock_owner, task_id, stop_heartbeat)

    def _run_site_group(self, task_id: str, site_group: list[tuple[str, Callable[[], object]]]) -> int:
        failures = 0
        for site_key, action in site_group:
            self._raise_if_cancelled(task_id)
            if self._run_site(task_id, site_key, action):
                failures += 1
            self._raise_if_cancelled(task_id)
        return failures

    def _run_single_site(
        self,
        task_id: str,
        site_key: str,
        run_key: str,
        prepared_lock_owner: str = "",
    ) -> None:
        lock_owner = prepared_lock_owner or self._prepare_execution(
            task_id,
            run_key,
            "目前已有其他登打或查詢執行中，未啟動單站任務。",
        )
        if not lock_owner:
            return
        stop_heartbeat: Callable[[], None] = lambda: None
        try:
            stop_heartbeat = _start_manual_task_lock_heartbeat(
                self.artifacts_dir,
                lock_owner,
                self._lease_lost_event(task_id),
            )
            self._raise_if_cancelled(task_id)
            request = self.store.request_for(task_id)
            profile_suffix = task_id.replace("-", "_")
            if site_key == "fuel_record" and not request.has_fuel_record():
                self._set_overall_status_owned(
                    task_id,
                    "desktop_fast_unavailable",
                    "此任務未勾選加油紀錄，已略過加油登打。",
                )
                return
            self._set_overall_status_owned(
                task_id,
                "desktop_fast_running",
                f"本機快速執行中：{SITE_NAMES[site_key]}。",
            )
            self._notify(task_id, f"單站登打開始：{SITE_NAMES[site_key]}")
            failures = 0
            for current_site_key in _single_site_sequence(request, site_key):
                self._raise_if_cancelled(task_id)
                if self._run_site(task_id, current_site_key, self._site_action(request, profile_suffix, current_site_key)):
                    failures += 1
            self._raise_if_cancelled(task_id)
            if failures:
                self._set_overall_status_owned(
                    task_id,
                    "desktop_fast_completed_with_errors",
                    f"單站登打失敗：{SITE_NAMES[site_key]} 未完成。",
                )
                self._notify(task_id, f"單站登打失敗：{SITE_NAMES[site_key]}")
            else:
                self._set_overall_status_owned(
                    task_id,
                    "site_run_completed",
                    f"單站登打完成：{SITE_NAMES[site_key]}。",
                )
                self._notify(task_id, f"單站登打成功：{SITE_NAMES[site_key]}")
        except TaskCancellationError:
            self._preserve_cancelled_status(task_id)
        except Exception as exc:
            try:
                self._set_overall_status_owned(
                    task_id,
                    "desktop_fast_completed_with_errors",
                    f"單站登打未完成：{exc}",
                )
            except TaskCancellationError:
                self._preserve_cancelled_status(task_id)
        finally:
            self._finish_execution(task_id, lock_owner, run_key, stop_heartbeat)

    def _site_action(self, request, profile_suffix: str, site_key: str):
        if site_key == "duty_work_log":
            return lambda: run_local_selenium_task(
                request,
                self.artifacts_dir,
                profile_name=f"duty_work_log_profile_{profile_suffix}",
                use_session_lock=False,
                tile_name="duty_work_log",
                force_new_driver=True,
                update_context=self._site_update_context(request.task_id, "duty_work_log"),
                cancel_check=self._cancel_check(request.task_id),
            )
        if site_key == "vehicle_mileage":
            return lambda: self._run_vehicle_mileage(request, profile_suffix)
        if site_key == "fuel_record":
            return lambda: self._run_fuel_record(request, profile_suffix)
        if site_key == "disinfection":
            return lambda: self._run_disinfection(request, profile_suffix)
        if site_key == "consumables":
            return lambda: self._run_consumables(request, profile_suffix)
        raise KeyError(site_key)

    def _run_site(self, task_id: str, site_key: str, action) -> int:
        self._raise_if_cancelled(task_id)
        site_name = SITE_NAMES[site_key]
        login_audit = login_audit_for_site(site_key, self.store.request_for(task_id))
        if _site_is_complete(str(self.store.get(task_id).get("site_statuses", {}).get(site_key, {}).get("status") or "")):
            self._set_overall_status_owned(task_id, "desktop_fast_running", f"{site_name} 已完成，略過。")
            self._notify(task_id, f"{site_name} 略過")
            return False
        self._update_site_result_owned(
            task_id,
            SiteAutomationResult(site_key, site_name, f"{site_key}_running", with_login_audit("本機快速執行中。", login_audit)),
        )
        self._notify(task_id, f"{site_name} 開始")
        try:
            self._raise_if_cancelled(task_id)
            result = action()
            self._raise_if_cancelled(task_id)
            result = _result_with_login_audit(result, login_audit)
            result = make_site_result(site_key, site_name, str(result.status), str(result.detail))
            self._update_site_result_owned(task_id, result)
            self._notify(task_id, f"{site_name} 結果")
            return _result_blocks_next(result)
        except TaskCancellationError:
            raise
        except ManualUpdateRequiredError as exc:
            self._raise_if_cancelled(task_id)
            self._update_site_result_owned(
                task_id,
                make_site_result(
                    site_key,
                    site_name,
                    f"{site_key}_waiting_confirmation",
                    f"需人工更新：{exc}",
                    exc,
                ),
            )
            self._notify(task_id, f"{site_name} 待人工確認")
            return True
        except Exception as exc:
            self._raise_if_cancelled(task_id)
            self._update_site_result_owned(
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

    def _vehicle_site_update_context(
        self,
        task_id: str,
        site_key: str,
        vehicle_request,
        vehicle_index: int,
    ) -> dict[str, object] | None:
        context = self._site_update_context(task_id, site_key)
        if context is None:
            return None
        vehicle_key = _vehicle_result_key(vehicle_request, vehicle_index)
        return {
            **context,
            "vehicle_index": vehicle_index,
            "vehicle_key": vehicle_key,
            "vehicle_label": str(getattr(vehicle_request, "vehicle", "") or "").strip(),
        }

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
                cancel_check=self._cancel_check(request.task_id),
            )
        debugger_port = _site_debugger_port("VEHICLE_MILEAGE_DEBUGGER_PORT", 9234)
        first_driver = True

        def run_vehicle(vehicle_request, index):
            nonlocal first_driver
            force_new_driver = first_driver
            first_driver = False
            return run_vehicle_mileage_task(
                vehicle_request,
                self.artifacts_dir,
                profile_name=f"vehicle_mileage_profile_{profile_suffix}",
                debugger_port=debugger_port,
                use_session_lock=False,
                tile_name="vehicle_mileage",
                force_new_driver=force_new_driver,
                update_context=self._vehicle_site_update_context(
                    request.task_id,
                    "vehicle_mileage",
                    vehicle_request,
                    index,
                ),
                cancel_check=self._cancel_check(request.task_id),
            )

        return self._run_per_vehicle_site(
            request,
            "vehicle_mileage",
            run_vehicle,
        )

    def _run_consumables(self, request, profile_suffix: str) -> SiteAutomationResult:
        self._raise_if_cancelled(request.task_id)
        driver = login_acs_and_get_driver(
            profile_name=f"consumables_profile_{profile_suffix}",
            tile_name="consumables",
            task=request,
            artifacts_dir=self.artifacts_dir,
        )
        self._raise_if_cancelled(request.task_id)
        if len(request.vehicle_requests()) <= 1:
            detail = open_consumable_record_for_task(
                driver,
                request,
                update_context=self._site_update_context(request.task_id, "consumables"),
                cancel_check=self._cancel_check(request.task_id),
                artifacts_dir=self.artifacts_dir,
            )
            status = "consumables_saved" if save_consumables_record_enabled() else "consumables_prefilled"
            return SiteAutomationResult("consumables", SITE_NAMES["consumables"], status, detail)
        return self._run_per_vehicle_site(
            request,
            "consumables",
            lambda vehicle_request, index: SiteAutomationResult(
                "consumables",
                SITE_NAMES["consumables"],
                "consumables_saved" if save_consumables_record_enabled() else "consumables_prefilled",
                open_consumable_record_for_task(
                    driver,
                    vehicle_request,
                    update_context=self._vehicle_site_update_context(
                        request.task_id,
                        "consumables",
                        vehicle_request,
                        index,
                    ),
                    cancel_check=self._cancel_check(request.task_id),
                    artifacts_dir=self.artifacts_dir,
                ),
            ),
        )

    def _run_fuel_record(self, request, profile_suffix: str) -> SiteAutomationResult:
        vehicle_requests = request.vehicle_requests()
        if len(vehicle_requests) <= 1:
            return run_fuel_record_task(
                request,
                self.artifacts_dir,
                profile_name=f"fuel_record_profile_{profile_suffix}",
                use_session_lock=False,
                tile_name="fuel_record",
                force_new_driver=True,
                update_context=self._site_update_context(request.task_id, "fuel_record"),
                cancel_check=self._cancel_check(request.task_id),
            )
        indexed_fuel_requests = [
            (index, vehicle_request)
            for index, vehicle_request in enumerate(vehicle_requests, start=1)
            if vehicle_request.fuel_record.enabled
        ]
        debugger_port = _site_debugger_port("FUEL_RECORD_DEBUGGER_PORT", 9235)
        first_driver = True

        def run_vehicle(vehicle_request, index):
            nonlocal first_driver
            force_new_driver = first_driver
            first_driver = False
            return run_fuel_record_task(
                vehicle_request,
                self.artifacts_dir,
                profile_name=f"fuel_record_profile_{profile_suffix}",
                debugger_port=debugger_port,
                use_session_lock=False,
                tile_name="fuel_record",
                force_new_driver=force_new_driver,
                update_context=self._vehicle_site_update_context(
                    request.task_id,
                    "fuel_record",
                    vehicle_request,
                    index,
                ),
                cancel_check=self._cancel_check(request.task_id),
            )

        return self._run_per_vehicle_site(
            request,
            "fuel_record",
            run_vehicle,
            indexed_vehicle_requests=indexed_fuel_requests,
        )

    def _run_disinfection(self, request, profile_suffix: str):
        self._raise_if_cancelled(request.task_id)
        update_context = self._site_update_context(request.task_id, "disinfection")
        require_safe_automated_update("disinfection", request, update_context)

        def run_one(vehicle_request, index: int, vehicle_update_context):
            profile_name = f"disinfection_profile_{profile_suffix}_{index}"
            driver = login_disinfection_and_get_driver(
                request=vehicle_request,
                profile_name=profile_name,
                tile_name="disinfection",
                artifacts_dir=self.artifacts_dir,
            )
            self._raise_if_cancelled(request.task_id)
            return run_disinfection_task(
                vehicle_request,
                self.artifacts_dir,
                existing_driver=driver,
                profile_name=profile_name,
                use_session_lock=False,
                tile_name="disinfection",
                force_new_driver=True,
                update_context=vehicle_update_context,
                cancel_check=self._cancel_check(request.task_id),
            )

        if len(request.vehicle_requests()) <= 1:
            return run_one(request, 1, update_context)
        return self._run_per_vehicle_site(
            request,
            "disinfection",
            lambda vehicle_request, index: run_one(
                vehicle_request,
                index,
                self._vehicle_site_update_context(
                    request.task_id,
                    "disinfection",
                    vehicle_request,
                    index,
                ),
            ),
        )

    def _run_per_vehicle_site(
        self,
        request,
        site_key: str,
        action,
        *,
        indexed_vehicle_requests: list[tuple[int, object]] | None = None,
    ) -> SiteAutomationResult:
        site_name = SITE_NAMES[site_key]
        details: list[str] = []
        targets = indexed_vehicle_requests
        if targets is None:
            targets = list(enumerate(request.vehicle_requests(), start=1))
        for index, vehicle_request in targets:
            self._raise_if_cancelled(request.task_id)
            vehicle_key = _vehicle_result_key(vehicle_request, index)
            if _vehicle_site_result_is_complete(self._vehicle_site_results(request.task_id, site_key).get(vehicle_key)):
                details.append(f"{vehicle_key}: skipped")
                continue
            try:
                result = action(vehicle_request, index)
                self._raise_if_cancelled(request.task_id)
            except TaskCancellationError:
                raise
            except ManualUpdateRequiredError as exc:
                self._raise_if_cancelled(request.task_id)
                result = make_site_result(
                    site_key,
                    site_name,
                    f"{site_key}_waiting_confirmation",
                    f"需人工更新：{exc}",
                    exc,
                )
            except Exception as exc:
                self._raise_if_cancelled(request.task_id)
                result = make_site_result(site_key, site_name, f"{site_key}_failed", str(exc), exc)
            self._record_vehicle_site_result(request.task_id, site_key, vehicle_key, result)
            details.append(f"{vehicle_key}: {getattr(result, 'detail', '')}")
            self._raise_if_cancelled(request.task_id)
        status = _aggregate_vehicle_site_status(
            site_key,
            [vehicle_request for _index, vehicle_request in targets],
            self._vehicle_site_results(request.task_id, site_key),
        )
        return SiteAutomationResult(site_key, site_name, status, " | ".join(details))

    def _vehicle_site_results(self, task_id: str, site_key: str) -> dict[str, dict[str, str]]:
        site = dict(self.store.get(task_id).get("site_statuses", {}).get(site_key) or {})
        results = site.get("vehicle_results")
        if not isinstance(results, dict):
            return {}
        return {str(key): dict(value) for key, value in results.items() if isinstance(value, dict)}

    def _record_vehicle_site_result(self, task_id: str, site_key: str, vehicle_key: str, result) -> None:
        self._update_vehicle_site_result_owned(
            task_id,
            site_key,
            vehicle_key,
            str(getattr(result, "status", "") or ""),
            str(getattr(result, "detail", "") or ""),
        )

    def _ensure_record_folders(self, request) -> str:
        root = DEFAULT_RECORD_ROOT
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
    if status.startswith("needs_") or "login" in status or "waiting_confirmation" in status:
        return True
    return False


def _manual_task_lock_heartbeat_seconds() -> float:
    try:
        return max(float(os.getenv("MANUAL_TASK_LOCK_HEARTBEAT_SECONDS", "30")), 0.01)
    except ValueError:
        return 30.0


def _start_manual_task_lock_heartbeat(
    artifacts_dir: Path,
    owner: str,
    lease_lost_event: threading.Event | None = None,
) -> Callable[[], None]:
    stop = threading.Event()
    interval = min(
        _manual_task_lock_heartbeat_seconds(),
        max(
            0.01,
            manual_task_lock_max_age_seconds()
            / (MAX_MANUAL_TASK_LOCK_HEARTBEAT_ERRORS + 1),
        ),
    )

    def heartbeat() -> None:
        consecutive_errors = 0
        while not stop.wait(interval):
            try:
                refreshed = refresh_manual_task_lock(artifacts_dir, owner)
                if stop.is_set():
                    clear_manual_task_lock(artifacts_dir, owner)
            except OSError as exc:
                consecutive_errors += 1
                print(
                    f"[desktop-fast] execution lease heartbeat retry "
                    f"owner={owner} attempt={consecutive_errors}: {exc}",
                    flush=True,
                )
                if consecutive_errors >= MAX_MANUAL_TASK_LOCK_HEARTBEAT_ERRORS:
                    print(
                        f"[desktop-fast] execution lease heartbeat unavailable owner={owner}; cancelling",
                        flush=True,
                    )
                    if lease_lost_event is not None:
                        lease_lost_event.set()
                    return
                continue
            consecutive_errors = 0
            if not refreshed:
                print(f"[desktop-fast] execution lease owner lost owner={owner}; cancelling", flush=True)
                if lease_lost_event is not None:
                    lease_lost_event.set()
                return

    thread = threading.Thread(target=heartbeat, name=f"manual-task-lock:{owner}", daemon=True)
    thread.start()

    def stop_heartbeat() -> None:
        stop.set()
        if threading.current_thread() is not thread:
            thread.join(timeout=1.0)

    return stop_heartbeat


def _result_with_login_audit(result, audit: str):
    detail = with_login_audit(str(getattr(result, "detail", "") or ""), audit)
    try:
        return replace(result, detail=detail)
    except (TypeError, ValueError):
        return result


def _site_is_complete(status: str) -> bool:
    value = str(status or "")
    return value == "completed_by_user" or value.endswith("_saved")


def _single_site_sequence(request, site_key: str) -> list[str]:
    if site_key not in MILEAGE_FUEL_PAIR or not request.has_fuel_record():
        return [site_key]
    return [site_key] + [key for key in MILEAGE_FUEL_PAIR if key != site_key]


def _vehicle_site_result_is_complete(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    return _site_is_complete(str(result.get("status") or ""))


def _vehicle_result_key(request, index: int) -> str:
    vehicle = str(getattr(request, "vehicle", "") or "").strip()
    return vehicle or f"{index}車"


def _aggregate_vehicle_site_status(site_key: str, vehicle_requests: list, results: dict[str, dict[str, str]]) -> str:
    statuses = [
        str(dict(results.get(_vehicle_result_key(vehicle_request, index)) or {}).get("status") or "")
        for index, vehicle_request in enumerate(vehicle_requests, start=1)
    ]
    if any("waiting_confirmation" in status for status in statuses):
        return f"{site_key}_waiting_confirmation"
    if not statuses or any(not status for status in statuses):
        return f"{site_key}_failed"
    if all(_site_is_complete(status) for status in statuses):
        return f"{site_key}_saved"
    if all("failed" not in status and "error" not in status for status in statuses):
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
