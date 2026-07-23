from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

from ambulance_bot.adapters import SITE_DEFINITIONS, SiteAutomationResult, default_adapters
from ambulance_bot.chrome_startup import cleanup_worker_chrome_residue
from ambulance_bot.consumables import consumable_inventory_options
from ambulance_bot.credential_envelope import (
    CredentialEnvelopeError,
    MAX_CREDENTIAL_PAYLOAD_BYTES,
    seal_credential_payload,
)
from ambulance_bot.desktop_fast_runner import DesktopFastRunner
from ambulance_bot.disaster_settings import (
    delete_disaster_vehicle_record,
    disaster_vehicle_options,
    disaster_vehicle_recorder_codes,
    load_disaster_vehicle_records,
    save_disaster_vehicle_record,
)
from ambulance_bot.duty_credentials import (
    credential_sync_accounts_from_payload,
    load_synced_worker_credential,
)
from ambulance_bot.login_audit import compact_login_account_summary, site_login_account_summaries
from ambulance_bot.line_api import reply_text, verify_signature
from ambulance_bot.manual_task_lock import (
    manual_task_lock_active,
    manual_task_lock_max_age_seconds,
    manual_task_lock_snapshot,
    run_with_manual_task_lock_absent,
    run_with_manual_task_lock_owner,
)
from ambulance_bot.models import (
    AmbulanceReturnRequest,
    CASE_REASON_OPTIONS,
    COMMAND_PREFIX,
    DEFAULT_DISINFECTION_ITEMS,
    DEFAULT_CONSUMABLES,
    DISASTER_ACTION_PACKAGES,
    DISASTER_REASON_OPTIONS,
    DISASTER_REASON_OPTIONS_BY_TYPE,
    DISINFECTION_ITEM_OPTIONS,
    PERSON_OPTIONS,
    example_command,
    clean_case_address,
    delete_vehicle_record,
    load_vehicle_records,
    normalize_hhmm,
    parse_case_date,
    parse_request,
    request_from_form,
    request_from_disaster_form,
    save_vehicle_record,
    vehicle_options,
    vehicle_ppe_names,
)
from ambulance_bot.profile_paths import runtime_profile_root
from ambulance_bot.record_folders import (
    RecordFolderError,
    disaster_folder_plan,
    ems_record_relative_paths,
    ensure_disaster_record_folders,
)
from ambulance_bot.site_diagnostics import DIAGNOSTIC_FIELDS, SITE_STAGE_DEFINITIONS, merge_diagnostic_fields
from ambulance_bot.sinposmart_backend import (
    SinpoSmartBackendStore,
    sinposmart_fire_day_label,
    sinposmart_person_label,
    sinposmart_record_type_label,
    sinposmart_status_class,
    sinposmart_status_label,
    sinposmart_trigger_label,
)
from ambulance_bot.task_runner import TaskRunner
from ambulance_bot.task_cancellation import clear_task_cancellation, request_task_cancellation
from ambulance_bot.task_edit_impact import analyze_task_edit, changed_site_keys
from ambulance_bot.task_store import (
    JsonTaskStore,
    SiteCompletionConflictError,
    TaskActiveError,
    WorkerClaimConflictError,
    pending_legacy_silent_save_report_event_id,
    task_completion_snapshot,
    task_payload_is_active_for_edit,
    worker_claim_lease_is_active,
    worker_queue_state,
)


load_dotenv()

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
store = JsonTaskStore(artifacts_dir / "tasks")
runner = TaskRunner(artifacts_dir, store=store)
desktop_runner = DesktopFastRunner(artifacts_dir, store=store)
_local_case_lookup_thread_lock = threading.Lock()
_local_case_lookup_thread: threading.Thread | None = None
_public_pc_report_lock = threading.Lock()
_public_pc_pending_report_lock = threading.RLock()
_credential_sync_relay_lock = threading.RLock()
_case_lookup_start_error = ""
PUBLIC_PC_REPORT_RETENTION_DAYS = 7
PUBLIC_PC_FAILURE_SCREENSHOT_MAX_BYTES = 2 * 1024 * 1024
PUBLIC_PC_FAILURE_SCREENSHOT_MAX_COUNT = 5
PUBLIC_PC_FAILURE_REPORT_MAX_BYTES = 15 * 1024 * 1024
PUBLIC_PC_FAILURE_SITE_KEYS = {
    "duty_work_log",
    "vehicle_mileage",
    "fuel_record",
    "consumables",
    "disinfection",
}
TASK_FORM_COMPLETED_HISTORY_HOURS = 48
TASK_FORM_RECENT_TASK_LIMIT = 5
TASK_FORM_RECENT_TASK_SCAN_LIMIT = 500
WORKER_HEARTBEAT_ONLINE_SECONDS = 45
WORKER_HEARTBEAT_STATES = frozenset({"starting", "idle", "busy", "update_handoff", "recovering", "stopping"})
WORKER_ROUTE_IDENTITY_STATUSES = frozenset({"verified", "unverified"})
PUBLIC_PC_LEGACY_RECONCILE_LIMIT = 500
PUBLIC_PC_PENDING_REPORT_FLUSH_INTERVAL_SECONDS = 30
PUBLIC_PC_LEGACY_RECONCILE_ERRORS = (
    AttributeError,
    FileNotFoundError,
    KeyError,
    TypeError,
    ValueError,
    OSError,
    UnicodeError,
)
MAX_CREDENTIAL_RELAY_FILE_BYTES = ((MAX_CREDENTIAL_PAYLOAD_BYTES + 2) // 3) * 4 + (64 * 1024)
REMOTE_UPDATE_ACTIVE_STATUSES = {"pending", "waiting_busy", "waiting_idle", "updating"}
REMOTE_UPDATE_TERMINAL_STATUSES = {"completed", "up_to_date", "failed", "timed_out"}
REMOTE_UPDATE_STATUSES = REMOTE_UPDATE_ACTIVE_STATUSES | REMOTE_UPDATE_TERMINAL_STATUSES
REMOTE_UPDATE_STATUS_LABELS = {
    "pending": "等待公務電腦接收",
    "waiting_busy": "等待勤務完成",
    "waiting_idle": "等待電腦閒置",
    "updating": "背景更新中",
    "completed": "更新完成",
    "up_to_date": "已是最新版本",
    "failed": "更新失敗",
    "timed_out": "更新命令逾時",
}
REMOTE_UPDATE_TRANSITIONS = {
    "pending": {"pending", "waiting_busy", "waiting_idle", "updating"},
    "waiting_busy": {"waiting_busy", "waiting_idle", "updating"},
    "waiting_idle": {"waiting_idle", "waiting_busy", "updating"},
    "updating": {"updating", "completed", "up_to_date", "failed"},
}
VALID_SITE_KEYS = {site.key for site in SITE_DEFINITIONS}
SITE_RUN_ORDER = ["duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection"]
SITE_SHORT_NAMES = {
    "duty_work_log": "工作",
    "vehicle_mileage": "里程",
    "fuel_record": "加油",
    "disinfection": "消毒",
    "consumables": "耗材",
}
SITE_DISPLAY_NAMES = {
    "duty_work_log": "工作",
    "vehicle_mileage": "里程",
    "fuel_record": "加油",
    "consumables": "耗材",
    "disinfection": "消毒",
}
SITE_UPDATE_BUTTON_LABELS = {
    "duty_work_log": "更新工作",
    "vehicle_mileage": "更新里程",
    "fuel_record": "更新加油",
    "consumables": "更新耗材",
    "disinfection": "更新消毒",
}
CONSUMABLE_PACKAGE_KEYS = {"glucose", "iv", "io", "ecg", "ohca"}
STALE_RUNNING_TASK_DETAIL = "登打流程超過 10 分鐘未回報，已自動中止；請先修復 Chrome 或重新啟動 Worker 後再重試。"

class _WorkerBrowserCleanupOptions:
    def __init__(self, arguments: list[str]) -> None:
        self.arguments = arguments


class CaseLookupProcessTimeout(TimeoutError):
    pass


def cleanup_active_worker_browsers() -> int:
    arguments = [f"--user-data-dir={runtime_profile_root()}"]
    debugger_port = os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223").strip()
    if debugger_port:
        arguments.append(f"--remote-debugging-port={debugger_port}")
    return cleanup_worker_chrome_residue(_WorkerBrowserCleanupOptions(arguments), "manual abort")


@app.get("/")
def index():
    if not request_is_local_host():
        return render_template("nas_home.html")
    return redirect(url_for("task_entry"))


@app.get("/task-entry")
def task_entry():
    return render_template("task_entry.html")


@app.get("/app")
def new_task():
    selected_case = pop_selected_case()
    person_options = selected_case.get("person_options") or PERSON_OPTIONS
    return render_template(
        "new_task.html",
        form_action=url_for("create_task"),
        submit_label="建立任務",
        cancel_url="",
        recent_tasks=recent_tasks_for_task_form("ems"),
        case_lookup=prepared_case_lookup(),
        selected_case=selected_case,
        vehicle_options=effective_ems_vehicle_options(),
        person_options=person_options,
        case_reason_options=CASE_REASON_OPTIONS,
        consumable_options=consumable_inventory_options(),
        default_consumables=DEFAULT_CONSUMABLES if selected_case else {},
        baseline_consumables_loaded=bool(selected_case),
        selected_consumable_packages=[],
        two_vehicle_available=two_vehicle_option_available(selected_case),
        last_vehicle_mileages=last_vehicle_mileages(),
        disinfection_item_options=DISINFECTION_ITEM_OPTIONS,
        default_disinfection_items=DEFAULT_DISINFECTION_ITEMS if selected_case else [],
        form_errors=[],
    )


@app.get("/app/disaster")
def disaster_task():
    selected_case = pop_selected_case()
    person_options = selected_case.get("person_options") or person_options_from_personnel(selected_case.get("personnel") or [])
    return render_template(
        "disaster_task.html",
        form_action=url_for("create_disaster_task"),
        selected_case=selected_case,
        recent_tasks=recent_tasks_for_task_form("disaster"),
        case_lookup=prepared_case_lookup(),
        person_options=person_options,
        vehicle_options=effective_disaster_vehicle_options(),
        disaster_reason_options=DISASTER_REASON_OPTIONS,
        disaster_reason_options_by_type=DISASTER_REASON_OPTIONS_BY_TYPE,
        disaster_action_packages=DISASTER_ACTION_PACKAGES,
        form_errors=[],
    )


@app.post("/cases/query")
def query_cases():
    global _case_lookup_start_error

    lookup_range = "24h"
    source = case_lookup_source_label(request.host)
    mode = effective_task_execution_mode()
    try:
        write_case_lookup_request(lookup_range, source=source, mode=mode)
        print(f"[case_lookup] query requested host={request.host} source={source} range={lookup_range} mode={mode}", flush=True)
        if mode == "desktop_fast":
            start_local_case_lookup(lookup_range)
    except (OSError, RuntimeError) as exc:
        _case_lookup_start_error = f"案件查詢啟動失敗：{exc}。請按「修復 Chrome」或重新啟動 Worker 後再試。"
        print(f"[case_lookup] startup failed host={request.host} source={source} range={lookup_range} mode={mode} error={exc}", flush=True)
    else:
        _case_lookup_start_error = ""
    return redirect(task_form_url())


@app.post("/api/record-folder-preview")
def record_folder_preview():
    service_type = str(request.form.get("service_type") or "ems").strip().lower()
    required_preview_values = [request.form.get("case_date"), request.form.get("case_time")]
    if service_type == "disaster":
        required_preview_values.append(request.form.get("case_address"))
    if not all(str(value or "").strip() for value in required_preview_values):
        return jsonify({"ok": True, "paths": [], "detail": "請先完成案件與車輛資料。"})
    if not any(str(value or "").strip() for value in request.form.getlist("vehicle")):
        return jsonify({"ok": True, "paths": [], "detail": "請先完成案件與車輛資料。"})
    try:
        if service_type == "disaster":
            task_request = request_from_disaster_form(request.form)
            paths = [
                entry.path.as_posix()
                for entry in disaster_folder_plan(
                    task_request,
                    Path(),
                    effective_disaster_vehicle_recorder_codes(),
                )
            ]
        else:
            task_request = request_from_form(request.form)
            paths = [path.as_posix() for path in ems_record_relative_paths(task_request)]
    except (OSError, RecordFolderError, ValueError) as exc:
        return jsonify({"ok": False, "paths": [], "detail": str(exc)}), 400
    return jsonify({"ok": True, "paths": paths})


@app.post("/cases/import")
def import_case():
    case_id = str(request.form.get("case_id") or "").strip()
    if not case_id:
        abort(400)
    if not write_selected_case_from_lookup(case_id):
        abort(404)
    anchor = "disaster-form" if str(request.form.get("return_to") or "").strip() == "disaster" else "task-form"
    return redirect(task_form_url(anchor=anchor))


@app.post("/cases/clear")
def clear_imported_case():
    pop_selected_case()
    return redirect(task_form_url())


def task_form_url(*, anchor: str = "") -> str:
    endpoint = "disaster_task" if str(request.form.get("return_to") or "").strip() == "disaster" else "new_task"
    return url_for(endpoint, _anchor=anchor or None)


@app.post("/tasks")
def create_task():
    task_request = request_from_form(request.form)
    errors = validate_task_form(task_request)
    if errors:
        return render_task_form_from_request(
            task_request,
            form_action=url_for("create_task"),
            submit_label="建立任務",
            cancel_url="",
            recent_tasks=recent_tasks_for_task_form("ems"),
            case_lookup=prepared_case_lookup(),
            form_errors=errors,
            baseline_consumables_loaded=form_flag_enabled(request.form.get("baseline_consumables_loaded")),
            selected_consumable_packages=selected_consumable_packages_from_form(request.form),
            two_vehicle_available=form_flag_enabled(request.form.get("two_vehicle_available")) or task_request.two_vehicle,
        ), 400
    payload = store.create(task_request)
    report_public_pc_task_event(payload, "建立任務")
    if should_auto_queue_task_on_create():
        queue_task_for_worker(task_request.task_id)
    pop_selected_case()
    return redirect(url_for("task_detail", task_id=task_request.task_id))


@app.post("/tasks/disaster")
def create_disaster_task():
    task_request = request_from_disaster_form(request.form)
    errors = validate_disaster_task_form(task_request)
    if errors:
        return render_template(
            "disaster_task.html",
            form_action=url_for("create_disaster_task"),
            selected_case=task_form_values(task_request.to_dict()),
            recent_tasks=recent_tasks_for_task_form("disaster"),
            case_lookup=prepared_case_lookup(),
            person_options=person_options_from_personnel(task_request.personnel),
            vehicle_options=effective_disaster_vehicle_options(),
            disaster_reason_options=DISASTER_REASON_OPTIONS,
            disaster_reason_options_by_type=DISASTER_REASON_OPTIONS_BY_TYPE,
            disaster_action_packages=DISASTER_ACTION_PACKAGES,
            form_errors=errors,
        ), 400
    existing = existing_disaster_task_for_case(task_request.case_id)
    if existing:
        existing_task_id = str(dict(existing.get("task") or {}).get("task_id") or "")
        return redirect(url_for("task_detail", task_id=existing_task_id))
    try:
        folder_results = ensure_disaster_record_folders(
            task_request,
            recorder_codes=effective_disaster_vehicle_recorder_codes(),
        )
    except (OSError, RecordFolderError) as exc:
        return render_template(
            "disaster_task.html",
            form_action=url_for("create_disaster_task"),
            selected_case=task_form_values(task_request.to_dict()),
            recent_tasks=recent_tasks_for_task_form("disaster"),
            case_lookup=prepared_case_lookup(),
            person_options=person_options_from_personnel(task_request.personnel),
            vehicle_options=effective_disaster_vehicle_options(),
            disaster_reason_options=DISASTER_REASON_OPTIONS,
            disaster_reason_options_by_type=DISASTER_REASON_OPTIONS_BY_TYPE,
            disaster_action_packages=DISASTER_ACTION_PACKAGES,
            form_errors=[f"行車紀錄器資料夾建立失敗：{exc}"],
        ), 400
    payload = store.create(task_request)
    for folder in folder_results:
        store.add_event_to_payload(
            payload,
            "disaster_record_folder_ready",
            f"{folder.vehicle}：{folder.status}：{folder.path}",
        )
    store.save_payload(task_request.task_id, payload)
    report_public_pc_task_event(payload, "建立救災任務")
    if should_auto_queue_task_on_create():
        queue_task_for_worker(task_request.task_id)
    pop_selected_case()
    return redirect(url_for("task_detail", task_id=task_request.task_id))


@app.get("/tasks/<task_id>/edit")
def edit_task(task_id: str):
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    payload = refresh_stale_running_task(payload)
    task = dict(payload.get("task") or {})
    if str(task.get("service_type") or "ems").strip().lower() == "disaster":
        return "救災案件不使用救護編輯頁，請返回救災任務明細。", 409
    if task_edit_is_locked(payload):
        return task_edit_lock_message(payload), 409
    selected_case = task_form_values(task)
    return render_template(
        "new_task.html",
        form_action=url_for("update_task", task_id=task_id),
        submit_label="儲存修改",
        cancel_url=url_for("task_detail", task_id=task_id),
        recent_tasks=[],
        case_lookup={"cases": [], "case_count": 0, "debug_artifacts": []},
        selected_case=selected_case,
        vehicle_options=effective_ems_vehicle_options(),
        person_options=PERSON_OPTIONS,
        case_reason_options=CASE_REASON_OPTIONS,
        consumable_options=consumable_inventory_options(),
        default_consumables=dict(task.get("consumables") or {}),
        baseline_consumables_loaded=False,
        selected_consumable_packages=[],
        two_vehicle_available=two_vehicle_option_available(selected_case),
        disinfection_item_options=DISINFECTION_ITEM_OPTIONS,
        default_disinfection_items=list(task.get("disinfection_items") or []),
        form_errors=[],
    )


@app.post("/tasks/<task_id>/edit")
def update_task(task_id: str):
    try:
        previous_payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    previous_payload = refresh_stale_running_task(previous_payload)
    previous_task = dict(previous_payload.get("task") or {})
    if str(previous_task.get("service_type") or "ems").strip().lower() == "disaster":
        return "救災案件不使用救護編輯頁，請返回救災任務明細。", 409
    if task_edit_is_locked(previous_payload):
        return task_edit_lock_message(previous_payload), 409
    task_request = request_from_form(request.form)
    errors = validate_task_form(task_request)
    if errors:
        return render_task_form_from_request(
            task_request,
            form_action=url_for("update_task", task_id=task_id),
            submit_label="儲存修改",
            cancel_url=url_for("task_detail", task_id=task_id),
            recent_tasks=[],
            case_lookup={"cases": [], "case_count": 0, "debug_artifacts": []},
            form_errors=errors,
            baseline_consumables_loaded=form_flag_enabled(request.form.get("baseline_consumables_loaded")),
            selected_consumable_packages=selected_consumable_packages_from_form(request.form),
            two_vehicle_available=form_flag_enabled(request.form.get("two_vehicle_available")) or task_request.two_vehicle,
        ), 400
    current_task = task_request.to_dict()
    edit_impact = analyze_task_edit(previous_task, current_task)
    changed_site_keys_for_edit = changed_site_keys(edit_impact)
    site_update_contexts = site_update_contexts_for_task_edit(
        previous_task,
        current_task,
        changed_site_keys_for_edit,
    )
    try:
        payload = store.update_task(
            task_id,
            task_request,
            changed_site_keys=changed_site_keys_for_edit,
            site_update_contexts=site_update_contexts,
            edit_impact=edit_impact,
        )
    except TaskActiveError:
        return "任務正在執行中，請等待完成或先中止登打後再編輯。", 409
    report_public_pc_task_event(payload, "修改任務")
    return redirect(url_for("task_detail", task_id=task_id))


@app.get("/tasks/<task_id>")
def task_detail(task_id: str):
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    payload = refresh_stale_running_task(payload)
    return render_template("task_detail.html", payload=payload, site_can_run_individually=site_can_run_individually)


@app.post("/tasks/<task_id>/delete")
def delete_task(task_id: str):
    try:
        store.delete(task_id)
    except TaskActiveError:
        return "任務正在執行中，請先中止登打再刪除。", 409
    except FileNotFoundError:
        abort(404)
    return redirect(url_for("new_task"))


@app.post("/tasks/<task_id>/run")
def run_task(task_id: str):
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    payload = refresh_stale_running_task(payload)
    if status_class(effective_task_status(payload)) == "complete":
        return "任務已全部完成；如需修正，請先編輯內容產生待更新站別。", 409
    if task_payload_is_active(payload):
        store.set_overall_status(
            task_id,
            effective_task_status(payload),
            "已有登打流程執行中，請等待完成，或先按「中止登打」再重試。",
        )
        return redirect(url_for("task_detail", task_id=task_id))
    if task_has_waiting_confirmation(dict(payload.get("site_statuses") or {})):
        return "任務尚有待人工確認的資料，請先到官方網頁核對後按「已確認」。", 409
    mode = effective_task_execution_mode()
    if mode == "desktop_fast":
        report_public_pc_task_event(payload, f"按下{task_site_count_label(payload.get('task') or {})}登打")
        desktop_runner.start_existing(task_id)
        return redirect(url_for("task_detail", task_id=task_id))
    if mode == "worker_queue":
        try:
            queue_task_for_worker(task_id)
        except WorkerClaimConflictError as exc:
            return exc.detail, 409
        return redirect(url_for("task_detail", task_id=task_id))
    runner.start_existing(task_id)
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<task_id>/sites/<site_key>/run")
def run_task_site(task_id: str, site_key: str):
    if site_key not in VALID_SITE_KEYS:
        abort(404)
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    payload = refresh_stale_running_task(payload)
    if site_key == "fuel_record" and not task_has_fuel_record(payload.get("task") or {}):
        store.set_overall_status(task_id, "desktop_fast_unavailable", "此任務未勾選加油紀錄，已略過加油登打。")
        return redirect(url_for("task_detail", task_id=task_id))
    if task_payload_is_active(payload):
        store.set_overall_status(
            task_id,
            effective_task_status(payload),
            "已有登打流程執行中，請等待完成，或先按「中止登打」再重試。",
        )
        return redirect(url_for("task_detail", task_id=task_id))
    if task_has_waiting_confirmation(dict(payload.get("site_statuses") or {})):
        return "任務尚有待人工確認的資料，請先到官方網頁核對後按「已確認」。", 409
    mode = effective_task_execution_mode()
    if mode == "desktop_fast":
        report_public_pc_task_event(payload, f"按下單站登打：{site_display_name(site_key)}")
        desktop_runner.start_site(task_id, site_key)
    else:
        store.set_overall_status(
            task_id,
            "desktop_fast_unavailable",
            "單站登打只能在本機網頁使用；手機/NAS 請使用五站登打或公務電腦 worker。",
        )
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<task_id>/sites/<site_key>/complete")
def complete_site(task_id: str, site_key: str):
    vehicle_key = str(request.form.get("vehicle_key") or "").strip()
    expected_token = site_manual_complete_token(task_id, site_key, vehicle_key)
    if not expected_token:
        return "人工確認功能缺少安全設定，請先設定 WORKER_TOKEN。", 503
    supplied_token = str(request.form.get("confirmation_token") or "").strip()
    if not hmac.compare_digest(supplied_token, expected_token):
        abort(403)
    try:
        payload = store.mark_site_completed(task_id, site_key, vehicle_key=vehicle_key)
    except SiteCompletionConflictError as exc:
        return str(exc), 409
    except (FileNotFoundError, KeyError):
        abort(404)
    report_public_pc_task_event(payload, f"人工確認站別完成：{site_display_name(site_key)}")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<task_id>/abort")
def abort_task(task_id: str):
    lock_before = manual_task_lock_snapshot(artifacts_dir)
    if lock_before.get("guard_busy"):
        return "本機執行鎖正在更新，為避免中止錯誤任務，請稍後再試。", 409
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    lock_after = manual_task_lock_snapshot(artifacts_dir)
    if lock_after.get("guard_busy"):
        return "本機執行鎖正在更新，為避免中止錯誤任務，請稍後再試。", 409
    lock_before_identity = (
        str(lock_before.get("owner") or "").strip(),
        str(lock_before.get("task_id") or "").strip(),
        str(lock_before.get("started_at") or "").strip(),
    )
    lock_after_identity = (
        str(lock_after.get("owner") or "").strip(),
        str(lock_after.get("task_id") or "").strip(),
        str(lock_after.get("started_at") or "").strip(),
    )
    if lock_before_identity != lock_after_identity:
        return "任務執行租約已變更，為避免中止新一輪登打，本次未執行中止。", 409
    lease_owner = task_execution_lease_owner(payload, lock_before)
    active_owner = lock_before_identity[0]
    if active_owner and not lease_owner:
        return "目前執行中的是另一筆任務，未中止也未關閉其瀏覽器。", 409
    initial_queue_state = worker_queue_state(payload)
    expected_claim_id = str(initial_queue_state.get("claim_id") or "").strip()
    expected_queue_id = str(initial_queue_state.get("queue_id") or "").strip()
    marker_identity: dict[str, str] = {}
    abort_committed = False

    def clear_abort_marker() -> None:
        marker_owner = marker_identity.get("owner", "")
        if marker_owner:
            clear_task_cancellation(
                artifacts_dir,
                task_id,
                execution_owner=marker_owner,
                claim_id=marker_identity.get("claim_id", ""),
            )

    def abort_current_generation() -> None:
        nonlocal abort_committed
        store.get(task_id)
        marker_claim_id = expected_claim_id if lease_owner.startswith("worker-manual:") else ""
        if lease_owner:
            request_task_cancellation(
                artifacts_dir,
                task_id,
                execution_owner=lease_owner,
                claim_id=marker_claim_id,
            )
            marker_identity.update(owner=lease_owner, claim_id=marker_claim_id)
        store.abort_running_task(
            task_id,
            "使用者中止登打。",
            execution_lease_active=bool(lease_owner),
            expected_claim_id=expected_claim_id,
            expected_queue_id=expected_queue_id,
        )
        abort_committed = True
        if lease_owner.startswith(("desktop_fast:", "desktop-fast:", "worker-manual:")):
            try:
                cleanup_active_worker_browsers()
            except Exception as exc:
                print(f"[worker] abort browser cleanup deferred task={task_id}: {exc}", flush=True)

    try:
        if lease_owner:
            lease_still_matches = run_with_manual_task_lock_owner(
                artifacts_dir,
                lease_owner,
                task_id,
                abort_current_generation,
                clear_after=lease_owner.startswith(("desktop_fast:", "desktop-fast:")),
                expected_started_at=lock_before.get("started_at"),
            )
        else:
            lease_still_matches = run_with_manual_task_lock_absent(
                artifacts_dir,
                abort_current_generation,
            )
    except FileNotFoundError:
        if not abort_committed:
            clear_abort_marker()
        abort(404)
    except WorkerClaimConflictError as exc:
        if not abort_committed:
            clear_abort_marker()
        return exc.detail, 409
    except (OSError, ValueError) as exc:
        print(f"[worker] abort signal or guarded mutation failed task={task_id}: {exc}", flush=True)
        if abort_committed:
            return "任務已完成中止，但本機執行鎖尚未清除；已保留中止訊號阻止舊流程繼續寫入，請稍後重試或重啟 Worker。", 503
        clear_abort_marker()
        return "無法建立中止訊號或安全更新任務，為避免狀態與實際執行不一致，本次未中止也未關閉瀏覽器，請稍後再試。", 503
    if not lease_still_matches:
        clear_abort_marker()
        return "任務執行租約已變更，為避免中止新一輪登打，本次未執行中止。", 409
    return redirect(url_for("task_detail", task_id=task_id))


@app.get("/status")
def status():
    return jsonify(
        {
            "ok": True,
            "status": runner.latest_status_text(),
            "version": package_version(),
            "host": request.host,
            "desktop_fast_mode": os.getenv("DESKTOP_FAST_MODE", "auto"),
            "effective_mode": effective_task_execution_mode(),
            "app_dir": str(Path(__file__).resolve().parent),
            "default_consumables": list(DEFAULT_CONSUMABLES),
            "consumable_top_names": [item["name"] for item in consumable_inventory_options()[:5]],
        }
    )


@app.post("/api/credential-sync")
def credential_sync():
    if not credential_sync_receiver_enabled():
        abort(404)
    if not credential_sync_authorized():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    accounts = credential_sync_accounts_from_payload(data)
    if not accounts:
        return jsonify({"ok": False, "error": "missing_credentials"}), 400
    ack_id = str(data.get("sync_code") or data.get("event_id") or uuid4())
    try:
        sealed_payload = seal_credential_payload(data, credential_envelope_secret())
    except CredentialEnvelopeError as exc:
        return jsonify({"ok": False, "error": "credential_sealing_unavailable", "detail": str(exc)}), 503
    write_credential_sync_relay(
        {
            "request_id": ack_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "pending",
            "source_host": request.host,
            "account_count": len(accounts),
            "sealed_payload": sealed_payload,
        }
    )
    return jsonify({"ok": True, "ack_id": ack_id, "count": len(accounts), "queued": True})


@app.post("/api/sinposmart/events")
def sinposmart_events():
    if not credential_sync_receiver_enabled():
        abort(404)
    if not credential_sync_authorized():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    event = sinposmart_store().upsert_event(data)
    return jsonify({"ok": True, "ack_id": event["event_id"], "fire_day": event["fire_day"]})


@app.get("/admin/vehicles")
def admin_vehicles():
    if request_is_local_host():
        abort(404)
    return render_template(
        "admin_vehicles.html",
        vehicles=vehicle_admin_records(),
        errors=[],
        message="",
    )


@app.get("/admin/disaster-vehicles")
def admin_disaster_vehicles():
    if request_is_local_host():
        abort(404)
    return render_disaster_vehicle_settings()


@app.post("/admin/disaster-vehicles")
def save_disaster_vehicle_option():
    if request_is_local_host():
        abort(404)
    label = str(request.form.get("label") or "").strip()
    ppe_name = str(request.form.get("ppe_name") or "").strip()
    recorder_code = str(request.form.get("recorder_code") or "").strip()
    try:
        save_disaster_vehicle_record(label, ppe_name, recorder_code, artifacts_dir)
    except ValueError as exc:
        return render_disaster_vehicle_settings(errors=[str(exc)]), 400
    return render_disaster_vehicle_settings(message=f"已儲存 {label}")


@app.post("/admin/disaster-vehicles/delete")
def delete_disaster_vehicle_option():
    if request_is_local_host():
        abort(404)
    label = str(request.form.get("label") or "").strip()
    if not delete_disaster_vehicle_record(label, artifacts_dir):
        return render_disaster_vehicle_settings(errors=["找不到要刪除的救災車輛"]), 400
    return render_disaster_vehicle_settings(message=f"已刪除 {label}")


def render_disaster_vehicle_settings(*, errors: list[str] | None = None, message: str = ""):
    return render_template(
        "admin_disaster_vehicles.html",
        vehicles=load_disaster_vehicle_records(artifacts_dir),
        errors=errors or [],
        message=message,
    )


@app.get("/admin/public-pc")
def admin_public_pc():
    return render_admin_public_pc()


@app.get("/admin/disaster")
def admin_disaster():
    return render_admin_public_pc(locked_service="disaster")


@app.get("/admin/ems")
def admin_ems():
    return render_admin_public_pc(locked_service="ems")


def render_admin_public_pc(*, locked_service: str = ""):
    reports = public_pc_reports()
    csrf_token = remote_update_csrf_token()
    admin_token = remote_update_admin_token()
    worker_health_enabled = not public_pc_reporting_enabled()
    remote_update_enabled = worker_health_enabled and bool(csrf_token) and bool(admin_token)
    result_filter = str(request.args.get("result") or "all").strip().lower()
    if result_filter not in {"all", "success", "failed"}:
        result_filter = "all"
    service_filter = locked_service or str(request.args.get("service") or "all").strip().lower()
    if service_filter not in {"all", "disaster", "ems"}:
        service_filter = "all"
    service_counts = {
        "all": len(reports),
        "disaster": sum(public_pc_report_service_type(item) == "disaster" for item in reports),
        "ems": sum(public_pc_report_service_type(item) == "ems" for item in reports),
    }
    service_reports = (
        reports
        if service_filter == "all"
        else [item for item in reports if public_pc_report_service_type(item) == service_filter]
    )
    report_counts = {
        "all": len(service_reports),
        "success": sum(public_pc_report_result(item) == "success" for item in service_reports),
        "failed": sum(public_pc_report_result(item) == "failed" for item in service_reports),
    }
    visible_reports = (
        service_reports
        if result_filter == "all"
        else [item for item in service_reports if public_pc_report_result(item) == result_filter]
    )
    return render_template(
        "admin_public_pc.html",
        reports=visible_reports,
        report_counts=report_counts,
        result_filter=result_filter,
        service_counts=service_counts,
        service_filter=service_filter,
        service_filter_locked=bool(locked_service),
        admin_base_url=request.path,
        admin_page_title={
            "disaster": "SinpoSmart - 救災後台",
            "ems": "SinpoSmart - 救護後台",
        }.get(locked_service, "SinpoSmart - 救災救護Worker 後台"),
        version_info=worker_admin_version_info(reports),
        worker_health=worker_heartbeat_admin_view(reports),
        worker_health_enabled=worker_health_enabled,
        remote_update=remote_update_admin_view() if remote_update_enabled else {},
        remote_update_enabled=remote_update_enabled,
        remote_update_csrf_token=csrf_token if remote_update_enabled else "",
    )


@app.post("/admin/public-pc/remote-update")
def admin_public_pc_remote_update():
    if public_pc_reporting_enabled():
        abort(404)
    expected_token = remote_update_csrf_token()
    supplied_token = str(request.form.get("csrf_token") or "").strip()
    if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
        abort(403)
    expected_admin_token = remote_update_admin_token()
    supplied_admin_token = str(request.form.get("admin_token") or "").strip()
    if not expected_admin_token or not hmac.compare_digest(supplied_admin_token, expected_admin_token):
        abort(403)
    create_remote_update_command()
    return_service = str(request.form.get("return_service") or "").strip().lower()
    if return_service == "disaster":
        return redirect(url_for("admin_disaster"))
    if return_service == "ems":
        return redirect(url_for("admin_ems"))
    return redirect(url_for("admin_public_pc"))


@app.get("/admin/sinposmart")
def admin_sinposmart():
    days = sinposmart_store().list_days(limit=7)
    selected_fire_day = str(request.args.get("fire_day") or "").strip()
    selected_day = next((day for day in days if str(day.get("fire_day") or "") == selected_fire_day), None)
    if selected_day is None and days:
        selected_day = days[0]
        selected_fire_day = str(selected_day.get("fire_day") or "")
    return render_template(
        "admin_sinposmart.html",
        days=days,
        selected_day=selected_day,
        selected_fire_day=selected_fire_day,
        version_info=sinposmart_admin_version_info(selected_day),
    )


@app.post("/admin/vehicles")
def create_vehicle_option():
    if request_is_local_host():
        abort(404)
    label = str(request.form.get("label") or "").strip()
    ppe_name = str(request.form.get("ppe_name") or "").strip().upper()
    if not label:
        return render_template(
            "admin_vehicles.html",
            vehicles=vehicle_admin_records(),
            errors=["請輸入救護車代號"],
            message="",
        ), 400
    save_vehicle_record(label, ppe_name, artifacts_dir)
    return render_template(
        "admin_vehicles.html",
        vehicles=vehicle_admin_records(),
        errors=[],
        message=f"已新增或更新 {label}",
    )


@app.post("/admin/vehicles/delete")
def delete_vehicle_option():
    if request_is_local_host():
        abort(404)
    label = str(request.form.get("label") or "").strip()
    if not delete_vehicle_record(label, artifacts_dir):
        return render_template(
            "admin_vehicles.html",
            vehicles=vehicle_admin_records(),
            errors=["內建救護車不能刪除"],
            message="",
        ), 400
    return render_template(
        "admin_vehicles.html",
        vehicles=vehicle_admin_records(),
        errors=[],
        message=f"已刪除 {label}",
    )


@app.get("/worker/identity")
def worker_identity():
    if not worker_authorized():
        abort(403)
    with _public_pc_report_lock:
        server = _worker_server_identity_unlocked()
    return jsonify({"ok": True, "server": server})


@app.get("/worker/vehicle-settings")
def worker_vehicle_settings():
    if not worker_authorized():
        abort(403)
    return jsonify(
        {
            "ok": True,
            "ems_vehicles": vehicle_admin_records(),
            "disaster_vehicles": load_disaster_vehicle_records(artifacts_dir),
        }
    )


@app.post("/worker/control")
def worker_control():
    if not worker_authorized():
        abort(403)
    payload = _normalize_worker_control_payload(request.get_json(silent=True))
    received_at = datetime.now()
    with _public_pc_report_lock:
        server = _worker_server_identity_unlocked()
        heartbeat = _upsert_worker_heartbeat_unlocked(payload, received_at)
        route = payload["route"]
        route_is_verified = (
            isinstance(route, Mapping)
            and route["identity_status"] == "verified"
            and route["instance_id"] == server["instance_id"]
        )
        command, command_delivery = _claim_remote_update_command_unlocked(
            str(payload["worker_id"]),
            str(payload["package_version"]),
            allow_claim=route_is_verified,
        )
        remote_update_delivery = ""
        remote_update = payload.get("remote_update")
        if isinstance(remote_update, Mapping):
            if route_is_verified:
                status_payload = {**remote_update, "worker_id": payload["worker_id"]}
                status_command, remote_update_delivery = _apply_remote_update_status_unlocked(
                    str(remote_update["request_id"]), status_payload
                )
                if status_command is not None:
                    command = status_command
            else:
                remote_update_delivery = "unverified_route"
    response = {
        "ok": True,
        "received_at": received_at.isoformat(timespec="seconds"),
        "server": server,
        "heartbeat": heartbeat,
        "command": command,
        "command_delivery": command_delivery,
    }
    if remote_update_delivery:
        response["remote_update_delivery"] = remote_update_delivery
    return jsonify(response)


@app.get("/worker/next-task")
def worker_next_task():
    if not worker_authorized():
        abort(403)
    worker_id = str(request.args.get("worker_id") or "public-duty-pc").strip()
    payload = store.claim_next_for_worker(worker_id)
    if payload is None:
        return jsonify({"ok": True, "task": None})
    return jsonify({"ok": True, "task": payload["task"], "payload": payload})


@app.get("/worker/tasks")
def worker_tasks():
    if not worker_authorized():
        abort(403)
    limit_text = str(request.args.get("limit") or "20").strip()
    try:
        limit = max(1, min(int(limit_text), 50))
    except ValueError:
        limit = 20
    return jsonify({"ok": True, "tasks": refresh_recent_tasks(store.list_recent(limit))})


@app.get("/worker/tasks/<task_id>")
def worker_task(task_id: str):
    if not worker_authorized():
        abort(403)
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    payload = refresh_stale_running_task(payload)
    return jsonify({"ok": True, "payload": payload, "task": payload["task"]})


@app.post("/worker/tasks/<task_id>/claim")
def worker_claim_task(task_id: str):
    if not worker_authorized():
        abort(403)
    data = request.get_json(silent=True) or {}
    worker_id = str(data.get("worker_id") or "").strip()
    if not worker_id:
        return jsonify({"ok": False, "error": "worker_id_required"}), 400
    try:
        payload = store.claim_task_for_worker(task_id, worker_id)
    except WorkerClaimConflictError as exc:
        return jsonify({"ok": False, "error": exc.code, "detail": exc.detail}), 409
    except FileNotFoundError:
        abort(404)
    return jsonify(
        {
            "ok": True,
            "task": payload["task"],
            "payload": payload,
            "worker_queue": payload["worker_queue"],
        }
    )


@app.post("/worker/tasks/<task_id>/status")
def worker_task_status(task_id: str):
    if not worker_authorized():
        abort(403)
    data = request.get_json(silent=True) or {}
    status_text = str(data.get("status") or "").strip()
    detail = str(data.get("detail") or "").strip()
    overall_status = str(data.get("overall_status") or "").strip()
    overall_detail = str(data.get("overall_detail") or detail).strip()
    claim_id = str(data.get("claim_id") or "").strip()
    worker_id = str(data.get("worker_id") or "").strip()
    status_event_id = str(data.get("status_event_id") or "").strip()
    if not status_text:
        abort(400)
    site_key = str(data.get("site_key") or "").strip()
    site_name = str(data.get("site_name") or "公務電腦 worker").strip()
    try:
        site_result = None
        if site_key:
            diagnostic_fields = {field: str(data.get(field) or "").strip() for field in DIAGNOSTIC_FIELDS}
            site_result = SiteAutomationResult(site_key, site_name, status_text, detail, **diagnostic_fields)
        target_overall_status = overall_status if site_key else overall_status or status_text
        target_overall_detail = overall_detail if overall_status else detail
        payload, duplicate = store.apply_worker_status(
            task_id,
            result=site_result,
            vehicle_key=str(data.get("vehicle_key") or "").strip(),
            vehicle_label=str(data.get("vehicle_label") or "").strip(),
            overall_status=target_overall_status,
            overall_detail=target_overall_detail,
            status_event_id=status_event_id,
            claim_id=claim_id,
            worker_id=worker_id,
            enforce_claim_identity=True,
        )
    except WorkerClaimConflictError as exc:
        return jsonify({"ok": False, "error": exc.code, "detail": exc.detail}), 409
    except (FileNotFoundError, KeyError):
        abort(404)
    return jsonify({"ok": True, "duplicate": duplicate, "payload": payload})


@app.get("/worker/case-lookup-request")
def worker_case_lookup_request():
    if not worker_authorized():
        abort(403)
    payload = read_case_lookup_request()
    if payload.get("status") != "case_lookup_requested":
        return jsonify({"ok": True, "request": None})
    return jsonify({"ok": True, "request": payload})


@app.get("/worker/credential-sync")
def worker_credential_sync():
    if not worker_authorized():
        abort(403)
    try:
        record = credential_sync_record_for_worker()
    except CredentialEnvelopeError as exc:
        return jsonify({"ok": False, "error": "credential_sealing_unavailable", "detail": str(exc)}), 503
    if not record or record.get("status") != "pending":
        return jsonify({"ok": True, "request": None})
    return jsonify(
        {
            "ok": True,
            "request": {
                "request_id": str(record.get("request_id") or ""),
                "created_at": str(record.get("created_at") or ""),
                "account_count": int(record.get("account_count") or 0),
                "sealed_payload": record.get("sealed_payload")
                if isinstance(record.get("sealed_payload"), dict)
                else {},
            },
        }
    )


@app.get("/worker/remote-update")
def worker_remote_update():
    if not worker_authorized():
        abort(403)
    worker_id = str(request.args.get("worker_id") or "public-duty-pc").strip()
    package_version = str(request.args.get("package_version") or "").strip()
    with _public_pc_report_lock:
        command, _delivery = _claim_remote_update_command_unlocked(
            worker_id,
            package_version,
            allow_claim=True,
        )
    return jsonify({"ok": True, "command": command})


@app.post("/worker/remote-update/<request_id>/status")
def worker_remote_update_status(request_id: str):
    if not worker_authorized():
        abort(403)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        abort(400)
    status = str(data.get("status") or "").strip()
    if status not in REMOTE_UPDATE_STATUSES:
        abort(400)
    with _public_pc_report_lock:
        command, outcome = _apply_remote_update_status_unlocked(request_id, data)
    if outcome == "not_found":
        abort(404)
    if outcome in {"owner_conflict", "terminal_conflict", "transition_conflict"}:
        abort(409)
    return jsonify({"ok": True, "command": command, "ack_id": request_id})


@app.post("/worker/credential-sync/<request_id>/ack")
def worker_credential_sync_ack(request_id: str):
    if not worker_authorized():
        abort(403)
    data = request.get_json(silent=True) or {}
    status = str(data.get("status") or "saved").strip().lower()
    detail = str(data.get("detail") or "").strip()
    outcome = ack_credential_sync_relay(request_id, status=status, detail=detail)
    if not outcome:
        abort(404)
    return jsonify({"ok": True, "ack_id": request_id, "retained": outcome == "retained"})


@app.post("/worker/cases")
def worker_cases():
    if not worker_authorized():
        abort(403)
    data = request.get_json(silent=True) or {}
    cases = data.get("cases")
    if not isinstance(cases, list):
        abort(400)
    request_id = str(data.get("request_id") or "").strip()
    current_request = read_case_lookup_request()
    active_request = current_request.get("status") == "case_lookup_requested"
    current_request_id = str(current_request.get("request_id") or "").strip()
    if active_request and (not request_id or request_id != current_request_id):
        abort(409)
    if not active_request and request_id:
        abort(409)
    output_dir = artifacts_dir / "cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": str(data.get("status") or "cases_loaded"),
        "detail": str(data.get("detail") or f"公務電腦 worker 已回傳 {len(cases)} 筆案件。"),
        "lookup_range": str(data.get("lookup_range") or ""),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": str(data.get("source") or "public_duty_pc_worker"),
        "case_hash": str(data.get("case_hash") or ""),
        "request_id": request_id,
        "cases": cases,
    }
    write_json_atomic(output_dir / "latest.json", payload)
    if payload["status"] == "cases_loaded":
        mark_case_lookup_request_completed(payload)
    else:
        mark_case_lookup_request_failed(payload)
    return jsonify({"ok": True, "case_count": len(cases), "payload": payload})


@app.post("/worker/public-pc-task-events")
def worker_public_pc_task_events():
    if not worker_authorized():
        abort(403)
    if request.content_length and request.content_length > PUBLIC_PC_FAILURE_REPORT_MAX_BYTES:
        abort(413)
    data = request.get_json(silent=True) or {}
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    task_id = str(data.get("task_id") or task.get("task_id") or "").strip()
    if not task_id:
        abort(400)
    payload = upsert_public_pc_report(data)
    event_id = str(data.get("event_id") or "").strip()
    ack_id = event_id or str(uuid4())
    return jsonify({"ok": True, "payload": payload, "ack_id": ack_id})


@app.get("/artifacts/<path:filename>")
def artifact_file(filename: str):
    root = artifacts_dir.resolve()
    target = (root / filename).resolve()
    if root not in target.parents and target != root:
        abort(404)
    selenium_root = (root / "selenium").resolve()
    if selenium_root not in target.parents or target.suffix.lower() not in {".png", ".html"}:
        abort(404)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(root, filename)


@app.get("/admin/public-pc/failure-screenshots/<filename>")
def public_pc_failure_screenshot(filename: str):
    if Path(filename).name != filename or Path(filename).suffix.lower() != ".png":
        abort(404)
    root = public_pc_failure_screenshot_dir().resolve()
    target = (root / filename).resolve()
    if target.parent != root or not target.is_file():
        abort(404)
    return send_from_directory(root, filename)


def task_execution_mode() -> str:
    return os.getenv("TASK_EXECUTION_MODE", "worker_queue").strip().lower()


def package_version() -> str:
    candidates = [
        Path(__file__).with_name("VERSION.txt"),
        Path(__file__).with_name("WinPython_公務電腦使用包") / "VERSION.txt",
    ]
    for path in candidates:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            pass
    return ""


def _read_text_url(url: str, timeout_seconds: float = 2.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        content = response.read()
    return content.decode("utf-8-sig").strip()


def sinposmart_installed_version_info(selected_day: dict | None) -> dict[str, str] | None:
    if not isinstance(selected_day, dict):
        return None
    events = selected_day.get("events") if isinstance(selected_day.get("events"), list) else []
    latest: tuple[str, str] | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
        version = str(snapshot.get("app_version") or snapshot.get("package_version") or "").strip()
        if not version:
            continue
        event_time = str(event.get("last_occurred_at") or event.get("occurred_at") or "")
        if latest is None or event_time >= latest[0]:
            latest = (event_time, version)
    if latest is None:
        return None
    return {"label": "SinpoSmart 公務電腦", "version": latest[1], "detail": "公務電腦已安裝"}


def sinposmart_admin_version_info(selected_day: dict | None = None) -> dict[str, str]:
    installed = sinposmart_installed_version_info(selected_day)
    if installed:
        return installed
    url = os.getenv(
        "SINPOSMART_VERSION_URL",
        "https://github.com/seaflun/sinposmart/releases/latest/download/sinposmart-version.txt",
    ).strip()
    if not url:
        return {"label": "SinpoSmart 公務電腦", "version": "未設定", "detail": "未設定版本來源"}
    try:
        version = _read_text_url(url)
    except (OSError, urllib.error.URLError, UnicodeError):
        return {"label": "SinpoSmart 公務電腦", "version": "無法取得", "detail": "版本來源暫時無法連線"}
    return {"label": "SinpoSmart 公務電腦", "version": version or "未標示", "detail": "GitHub latest"}


def worker_admin_version_info(reports: list[dict] | None = None) -> dict[str, str]:
    backend_version = package_version() or "未標示"
    for report in reports or []:
        version = str(report.get("package_version") or "").strip()
        if version:
            return {
                "label": "SinpoSmart - 救災救護Worker",
                "version": backend_version,
                "detail": f"NAS 後台；公務電腦最後回報：{version}",
            }
    return {"label": "SinpoSmart - 救災救護Worker", "version": backend_version, "detail": "NAS 後台"}


def credential_sync_relay_file() -> Path:
    return artifacts_dir / "credential_sync" / "pending.json"


def sinposmart_store() -> SinpoSmartBackendStore:
    return SinpoSmartBackendStore(artifacts_dir / "sinposmart")


def credential_sync_token() -> str:
    return os.getenv("CREDENTIAL_SYNC_TOKEN", "").strip() or os.getenv("WORKER_TOKEN", "").strip()


def credential_envelope_secret() -> str:
    return os.getenv("WORKER_TOKEN", "").strip()


def credential_sync_receiver_enabled() -> bool:
    return bool(credential_sync_token())


def credential_sync_authorized() -> bool:
    expected = credential_sync_token()
    if not expected:
        return False
    supplied = (
        request.headers.get("X-Credential-Sync-Token", "").strip()
        or request.headers.get("X-Worker-Token", "").strip()
    )
    return supplied == expected


def credential_sync_ttl_seconds() -> int:
    try:
        return max(60, int(os.getenv("CREDENTIAL_SYNC_TTL_SECONDS", "900")))
    except ValueError:
        return 900


def read_credential_sync_relay() -> dict:
    path = credential_sync_relay_file()
    with _credential_sync_relay_lock:
        return _read_credential_sync_relay_unlocked(path)


def _read_credential_sync_relay_unlocked(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > MAX_CREDENTIAL_RELAY_FILE_BYTES:
            path.unlink()
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    created_epoch = credential_sync_created_epoch(payload, path)
    if time.time() - created_epoch > credential_sync_ttl_seconds():
        try:
            path.unlink()
        except OSError:
            pass
        return {}
    return payload


def credential_sync_created_epoch(payload: dict, path: Path) -> float:
    raw_created_at = str(payload.get("created_at") or "").strip()
    if raw_created_at:
        try:
            return datetime.fromisoformat(raw_created_at).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def write_credential_sync_relay(payload: dict) -> None:
    with _credential_sync_relay_lock:
        _write_credential_sync_relay_unlocked(payload)


def _write_credential_sync_relay_unlocked(payload: dict) -> None:
    path = credential_sync_relay_file()
    write_json_atomic(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def credential_sync_record_for_worker() -> dict:
    path = credential_sync_relay_file()
    with _credential_sync_relay_lock:
        record = _read_credential_sync_relay_unlocked(path)
        changed = False
        if record and not str(record.get("created_at") or "").strip():
            record = dict(record)
            record["created_at"] = datetime.fromtimestamp(
                credential_sync_created_epoch(record, path)
            ).isoformat(timespec="seconds")
            changed = True
        legacy_payload = record.get("payload") if isinstance(record, dict) else None
        if isinstance(legacy_payload, dict):
            sealed_payload = seal_credential_payload(legacy_payload, credential_envelope_secret())
            record = dict(record)
            record.pop("payload", None)
            record["sealed_payload"] = sealed_payload
            record["updated_at"] = datetime.now().isoformat(timespec="seconds")
            changed = True
        if changed:
            _write_credential_sync_relay_unlocked(record)
        return record


def clear_credential_sync_relay() -> None:
    with _credential_sync_relay_lock:
        try:
            credential_sync_relay_file().unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def ack_credential_sync_relay(request_id: str, *, status: str = "saved", detail: str = "") -> str:
    """Delete a saved relay record; retain failed attempts for safe retry."""

    expected = str(request_id or "").strip()
    if not expected:
        return ""
    path = credential_sync_relay_file()
    with _credential_sync_relay_lock:
        record = _read_credential_sync_relay_unlocked(path)
        if not record or str(record.get("request_id") or "") != expected:
            return ""
        if status != "saved":
            retained = dict(record)
            if not str(retained.get("created_at") or "").strip():
                retained["created_at"] = datetime.fromtimestamp(
                    credential_sync_created_epoch(record, path)
                ).isoformat(timespec="seconds")
            retained["status"] = "pending"
            retained["attempt_count"] = max(0, int(retained.get("attempt_count") or 0)) + 1
            retained["last_error_code"] = "worker_save_failed"
            retained["last_error"] = "公務電腦未能儲存帳密，系統將稍後重試。"
            retained["last_attempt_at"] = datetime.now().isoformat(timespec="seconds")
            retained["updated_at"] = retained["last_attempt_at"]
            _write_credential_sync_relay_unlocked(retained)
            return "retained"
        try:
            path.unlink()
        except FileNotFoundError:
            return ""
        except OSError:
            return ""
        return "deleted"


def public_pc_report_file() -> Path:
    return artifacts_dir / "public_pc" / "task_events.json"


def public_pc_report_backup_file() -> Path:
    return artifacts_dir / "public_pc" / "task_events.backup.json"


def public_pc_pending_report_file() -> Path:
    return artifacts_dir / "public_pc" / "pending_events.jsonl"


def public_pc_pending_report_spool_dir() -> Path:
    return artifacts_dir / "public_pc" / "pending_event_spool"


def public_pc_failure_screenshot_dir() -> Path:
    return artifacts_dir / "public_pc" / "failure_screenshots"


def _cleanup_public_pc_failure_screenshots(now: datetime | None = None) -> None:
    root = public_pc_failure_screenshot_dir()
    if not root.exists():
        return
    cutoff = (now or datetime.now()).timestamp() - (PUBLIC_PC_REPORT_RETENTION_DAYS * 86400)
    for path in root.glob("*.png"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def worker_server_identity_file() -> Path:
    return artifacts_dir / "public_pc" / "worker_server_identity.json"


def worker_heartbeat_file() -> Path:
    return artifacts_dir / "public_pc" / "worker_heartbeat.json"


def _worker_server_identity_unlocked() -> dict[str, str]:
    path = worker_server_identity_file()
    payload: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            payload = loaded
    instance_id = str(payload.get("instance_id") or "").strip()
    if not instance_id:
        instance_id = str(uuid4())
        write_json_atomic(path, {"instance_id": instance_id})
    return {
        "instance_id": instance_id,
        "version": package_version(),
        "deployment": "ambulance_return_bot_nas",
    }


def worker_server_identity() -> dict[str, str]:
    with _public_pc_report_lock:
        return _worker_server_identity_unlocked()


def _worker_control_text(
    data: Mapping[str, object],
    key: str,
    limit: int,
    *,
    required: bool = False,
) -> str:
    value = data.get(key, "")
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        abort(400)
    text = value.strip()
    if len(text) > limit or (required and not text):
        abort(400)
    return text


def _normalize_worker_control_remote_update(data: object) -> dict[str, object] | None:
    if data is None:
        return None
    if not isinstance(data, Mapping):
        abort(400)
    request_id = _worker_control_text(data, "request_id", 128, required=True)
    status = _worker_control_text(data, "status", 64, required=True)
    if status not in {"waiting_busy", "waiting_idle"}:
        abort(400)
    return {
        "request_id": request_id,
        "status": status,
        "detail": _worker_control_text(data, "detail", 512),
    }


def _normalize_worker_control_payload(data: object) -> dict[str, object]:
    if not isinstance(data, Mapping):
        abort(400)
    route_data = data.get("route")
    if not isinstance(route_data, Mapping):
        abort(400)
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        abort(400)
    state = _worker_control_text(data, "state", 64, required=True)
    if state not in WORKER_HEARTBEAT_STATES:
        abort(400)
    identity_status = _worker_control_text(route_data, "identity_status", 32, required=True)
    if identity_status not in WORKER_ROUTE_IDENTITY_STATUSES:
        abort(400)
    return {
        "worker_id": _worker_control_text(data, "worker_id", 128, required=True),
        "package_version": _worker_control_text(data, "package_version", 128, required=True),
        "pid": pid,
        "process_started_at": _worker_control_text(data, "process_started_at", 64, required=True),
        "execution_mode": _worker_control_text(data, "execution_mode", 64, required=True),
        "package_path": _worker_control_text(data, "package_path", 512, required=True),
        "state": state,
        "activity": _worker_control_text(data, "activity", 128),
        "busy_reason": _worker_control_text(data, "busy_reason", 512),
        "request_id": _worker_control_text(data, "request_id", 128),
        "route": {
            "name": _worker_control_text(route_data, "name", 64, required=True),
            "identity_status": identity_status,
            "instance_id": _worker_control_text(route_data, "instance_id", 128),
        },
        "remote_update": _normalize_worker_control_remote_update(data.get("remote_update")),
    }


def _read_worker_heartbeats_unlocked() -> dict[str, dict[str, object]]:
    path = worker_heartbeat_file()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    workers = payload.get("workers") if isinstance(payload, dict) else None
    if not isinstance(workers, Mapping):
        return {}
    heartbeats: dict[str, dict[str, object]] = {}
    for worker_id, record in workers.items():
        normalized_worker_id = str(worker_id or "").strip()
        if normalized_worker_id and isinstance(record, Mapping):
            heartbeats[normalized_worker_id] = dict(record)
    return heartbeats


def _upsert_worker_heartbeat_unlocked(data: Mapping[str, object], received_at: datetime) -> dict[str, object]:
    route = data.get("route")
    normalized_route = route if isinstance(route, Mapping) else {}
    worker_id = str(data.get("worker_id") or "").strip()
    heartbeat = {
        "worker_id": worker_id,
        "package_version": str(data.get("package_version") or "").strip(),
        "pid": int(data.get("pid") or 0),
        "process_started_at": str(data.get("process_started_at") or "").strip(),
        "execution_mode": str(data.get("execution_mode") or "").strip(),
        "package_path": str(data.get("package_path") or "").strip(),
        "state": str(data.get("state") or "").strip(),
        "activity": str(data.get("activity") or "").strip(),
        "busy_reason": str(data.get("busy_reason") or "").strip(),
        "request_id": str(data.get("request_id") or "").strip(),
        "route": {
            "name": str(normalized_route.get("name") or "").strip(),
            "identity_status": str(normalized_route.get("identity_status") or "").strip(),
            "instance_id": str(normalized_route.get("instance_id") or "").strip(),
        },
        "received_at": received_at.isoformat(timespec="seconds"),
    }
    heartbeats = _read_worker_heartbeats_unlocked()
    heartbeats[worker_id] = heartbeat
    write_json_atomic(worker_heartbeat_file(), {"workers": heartbeats})
    return heartbeat


def remote_update_command_file() -> Path:
    return artifacts_dir / "public_pc" / "remote_update.json"


def remote_update_csrf_token() -> str:
    secret = os.getenv("WORKER_TOKEN", "").strip().encode("utf-8")
    if not secret:
        return ""
    return hmac.new(secret, b"ambulance-remote-update-admin", hashlib.sha256).hexdigest()


def site_manual_complete_token(task_id: str, site_key: str, vehicle_key: str = "") -> str:
    secret = os.getenv("WORKER_TOKEN", "").strip().encode("utf-8")
    if len(secret) < 32:
        return ""
    message = f"ambulance-manual-complete\x00{task_id}\x00{site_key}\x00{vehicle_key}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def remote_update_admin_token() -> str:
    return os.getenv("REMOTE_UPDATE_ADMIN_TOKEN", "").strip()


def _read_remote_update_command_unlocked() -> dict:
    path = remote_update_command_file()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def remote_update_stale_seconds() -> int:
    try:
        return max(60, int(os.getenv("REMOTE_UPDATE_STALE_SECONDS", "3600")))
    except ValueError:
        return 3600


def remote_update_command_is_stale(command: dict, now: datetime | None = None) -> bool:
    if str(command.get("status") or "") not in REMOTE_UPDATE_ACTIVE_STATUSES:
        return False
    timestamp = str(command.get("updated_at") or command.get("requested_at") or "").strip()
    if not timestamp:
        return False
    try:
        updated_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return ((now or datetime.now()) - updated_at).total_seconds() > remote_update_stale_seconds()


def _expire_remote_update_command_unlocked(command: dict) -> dict:
    if not remote_update_command_is_stale(command):
        return command
    expired = {
        **command,
        "status": "timed_out",
        "detail": "遠端更新命令逾時，請確認公務電腦 Worker 是否在線。",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json_atomic(remote_update_command_file(), expired)
    return expired


def _claim_remote_update_command_unlocked(
    worker_id: str,
    package_version: str,
    *,
    allow_claim: bool,
) -> tuple[dict[str, object] | None, str]:
    command = _expire_remote_update_command_unlocked(_read_remote_update_command_unlocked())
    if str(command.get("status") or "") not in REMOTE_UPDATE_ACTIVE_STATUSES:
        return None, "no_active_command"
    if not allow_claim:
        return None, "unverified_route"
    owner = str(command.get("worker_id") or "").strip()
    if owner and owner != worker_id:
        return None, "owned_by_other_worker"
    if not owner:
        command["worker_id"] = worker_id
    if package_version and not str(command.get("before_version") or "").strip():
        command["before_version"] = package_version
    command["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
    write_json_atomic(remote_update_command_file(), command)
    return command, "claimed"


def _apply_remote_update_status_unlocked(
    request_id: str,
    data: Mapping[str, object],
) -> tuple[dict[str, object] | None, str]:
    command = _expire_remote_update_command_unlocked(_read_remote_update_command_unlocked())
    if str(command.get("request_id") or "") != request_id:
        return None, "not_found"
    status = str(data.get("status") or "").strip()
    if status not in REMOTE_UPDATE_STATUSES:
        return command, "invalid_status"
    current_status = str(command.get("status") or "").strip()
    owner = str(command.get("worker_id") or "").strip()
    supplied_worker_id = str(data.get("worker_id") or "").strip()
    if owner:
        idempotent_terminal_retry = current_status in REMOTE_UPDATE_TERMINAL_STATUSES and status == current_status
        if supplied_worker_id != owner and not (idempotent_terminal_retry and not supplied_worker_id):
            return command, "owner_conflict"
    else:
        if not supplied_worker_id:
            return command, "owner_conflict"
        command["worker_id"] = supplied_worker_id
    if current_status in REMOTE_UPDATE_TERMINAL_STATUSES:
        if status == current_status:
            return command, "idempotent"
        return command, "terminal_conflict"
    if status not in REMOTE_UPDATE_TRANSITIONS.get(current_status, set()):
        return command, "transition_conflict"
    now = datetime.now().isoformat(timespec="seconds")
    command["status"] = status
    command["updated_at"] = now
    for key in ("detail", "before_version", "installed_version", "exit_code"):
        if key in data:
            command[key] = data[key]
    if status == "updating":
        command["started_at"] = now
    if status in REMOTE_UPDATE_TERMINAL_STATUSES:
        command["completed_at"] = now
    write_json_atomic(remote_update_command_file(), command)
    return command, "updated"


def read_remote_update_command() -> dict:
    with _public_pc_report_lock:
        return _expire_remote_update_command_unlocked(_read_remote_update_command_unlocked())


def create_remote_update_command() -> tuple[dict, bool]:
    with _public_pc_report_lock:
        current = _expire_remote_update_command_unlocked(_read_remote_update_command_unlocked())
        if str(current.get("status") or "") in REMOTE_UPDATE_ACTIVE_STATUSES:
            return current, False
        now = datetime.now().isoformat(timespec="seconds")
        command = {
            "request_id": str(uuid4()),
            "status": "pending",
            "requested_at": now,
            "updated_at": now,
            "worker_id": "",
            "before_version": "",
            "installed_version": "",
            "detail": "等待公務電腦接收更新命令。",
        }
        write_json_atomic(remote_update_command_file(), command)
        return command, True


def remote_update_admin_view() -> dict:
    command = read_remote_update_command()
    status = str(command.get("status") or "").strip()
    status_class_name = ""
    if status in {"completed", "up_to_date"}:
        status_class_name = "complete"
    elif status in {"failed", "timed_out"}:
        status_class_name = "failed"
    elif status == "updating":
        status_class_name = "running"
    return {
        **command,
        "status": status,
        "status_label": REMOTE_UPDATE_STATUS_LABELS.get(status, "尚未下達更新命令"),
        "status_class": status_class_name,
        "active": status in REMOTE_UPDATE_ACTIVE_STATUSES,
    }


def _load_public_pc_reports(*, strict: bool = False) -> list[dict]:
    found_file = False
    for path in (public_pc_report_file(), public_pc_report_backup_file()):
        if not path.exists():
            continue
        found_file = True
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        reports = payload.get("tasks") if isinstance(payload, dict) else None
        if isinstance(reports, list):
            return [item for item in reports if isinstance(item, dict)]
    if strict and found_file:
        raise ValueError("救災救護後台案件主檔與備份均無法讀取，已停止寫入以保留現場資料。")
    return []


def _recent_public_pc_reports(reports: list[dict], now: datetime) -> list[dict]:
    cutoff = now - timedelta(days=PUBLIC_PC_REPORT_RETENTION_DAYS)
    recent: list[dict] = []
    for item in reports:
        raw_time = str(item.get("updated_at") or item.get("created_at") or "").strip()
        try:
            updated_at = datetime.fromisoformat(raw_time)
        except ValueError:
            recent.append(item)
            continue
        if updated_at >= cutoff:
            recent.append(item)
    return recent


def _public_pc_reports_unlocked(now: datetime) -> list[dict]:
    reports = _recent_public_pc_reports(_load_public_pc_reports(), now)
    return sorted(reports, key=lambda item: str(item.get("updated_at") or ""), reverse=True)


def public_pc_reports(now: datetime | None = None) -> list[dict]:
    with _public_pc_report_lock:
        return _public_pc_reports_unlocked(now or datetime.now())


def _latest_task_report_version(reports: Sequence[Mapping[str, object]]) -> tuple[str, str]:
    latest_version = ""
    latest_at = ""
    for report in reports:
        version = str(report.get("package_version") or "").strip()
        if not version:
            continue
        report_at = str(
            report.get("updated_at") or report.get("time") or report.get("created_at") or ""
        ).strip()
        if not latest_version or report_at >= latest_at:
            latest_version = version
            latest_at = report_at
    return latest_version, latest_at


def _worker_route_label(route: object) -> str:
    if not isinstance(route, Mapping):
        return "尚未確認"
    name = str(route.get("name") or "").strip()
    identity_status = str(route.get("identity_status") or "").strip()
    if not name:
        return "尚未確認"
    name_label = {"lan": "區網", "tailscale": "Tailscale"}.get(name, name)
    status_label = "已驗證" if identity_status == "verified" else "未驗證"
    return f"{name_label}（{status_label}）"


def worker_heartbeat_admin_view(
    reports: Sequence[Mapping[str, object]],
    now: datetime | None = None,
) -> dict[str, object]:
    now_value = now or datetime.now()
    with _public_pc_report_lock:
        heartbeats = _read_worker_heartbeats_unlocked()
    latest_heartbeat = max(
        heartbeats.values(),
        key=lambda item: str(item.get("received_at") or ""),
        default={},
    )
    received_at = str(latest_heartbeat.get("received_at") or "").strip()
    received_datetime: datetime | None = None
    if received_at:
        try:
            received_datetime = datetime.fromisoformat(received_at)
        except ValueError:
            received_datetime = None
    seconds_since_received = (
        (now_value - received_datetime).total_seconds() if received_datetime is not None else None
    )
    online = (
        seconds_since_received is not None
        and 0 <= seconds_since_received <= WORKER_HEARTBEAT_ONLINE_SECONDS
    )
    last_task_report_version, last_task_report_at = _latest_task_report_version(reports)
    if not latest_heartbeat:
        status_label = "尚未收到心跳"
    elif online:
        status_label = "在線"
    else:
        status_label = "離線"
    return {
        "online": online,
        "status_class": "complete" if online else "failed",
        "status_label": status_label,
        "worker_id": str(latest_heartbeat.get("worker_id") or "").strip(),
        "last_seen_at": received_at,
        "package_version": str(latest_heartbeat.get("package_version") or "").strip(),
        "state": str(latest_heartbeat.get("state") or "").strip(),
        "activity": str(latest_heartbeat.get("activity") or "").strip(),
        "busy_reason": str(latest_heartbeat.get("busy_reason") or "").strip(),
        "route_label": _worker_route_label(latest_heartbeat.get("route")),
        "last_task_report_version": last_task_report_version,
        "last_task_report_at": last_task_report_at,
    }


def task_completion_label(payload: dict) -> str:
    snapshot = task_completion_snapshot(payload)
    if snapshot["all_complete"]:
        return f"{snapshot['site_count_label']}登打完成"
    return task_progress_summary(payload)


def public_pc_report_result(report: dict) -> str:
    snapshot = task_completion_snapshot(report)
    if snapshot["all_complete"]:
        return "success"
    if snapshot["failed_site_keys"]:
        return "failed"
    return "pending"


def public_pc_report_service_type(report: dict) -> str:
    task = report.get("task") if isinstance(report.get("task"), dict) else {}
    return "disaster" if str(task.get("service_type") or "ems").strip().lower() == "disaster" else "ems"


def upsert_public_pc_report(data: dict) -> dict:
    with _public_pc_report_lock:
        path = public_pc_report_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        now_value = datetime.now()
        current_reports = _recent_public_pc_reports(_load_public_pc_reports(strict=True), now_value)
        task = data.get("task") if isinstance(data.get("task"), dict) else {}
        task_id = str(data.get("task_id") or task.get("task_id") or "").strip()
        now = now_value.isoformat(timespec="seconds")
        event_id = str(data.get("event_id") or "").strip() or str(uuid4())
        ack_id = str(data.get("ack_id") or event_id).strip() or event_id
        operator_label = str(data.get("operator") or data.get("user") or "未知使用者")
        synced_account = str(data.get("synced_account") or operator_label)
        event = {
            "event_id": event_id,
            "ack_id": ack_id,
            "time": str(data.get("time") or now),
            "operator": operator_label,
            "user": operator_label,
            "synced_account": synced_account,
            "worker_id": str(data.get("worker_id") or ""),
            "package_version": str(data.get("package_version") or ""),
            "action": public_pc_action_for_task(task, str(data.get("action") or "更新")),
            "status": str(data.get("status") or ""),
            "detail": str(data.get("detail") or ""),
        }
        reports = [item for item in current_reports if str(item.get("task_id") or "") != task_id]
        existing = next((item for item in current_reports if str(item.get("task_id") or "") == task_id), {})
        events = list(existing.get("events") or [])
        known_event_ids = {str(item.get("event_id") or "").strip() for item in events if isinstance(item, dict)}
        if event_id not in known_event_ids:
            events.append(event)
        site_login_accounts = (
            data.get("site_login_accounts")
            if isinstance(data.get("site_login_accounts"), dict)
            else existing.get("site_login_accounts", {})
        )
        stored_task = task or (
            existing.get("task") if isinstance(existing.get("task"), dict) else {}
        )
        site_statuses = (
            data.get("site_statuses")
            if isinstance(data.get("site_statuses"), dict)
            else existing.get("site_statuses", {})
        )
        site_statuses = _store_public_pc_failure_evidence(
            task_id,
            site_statuses,
            data.get("failure_evidence"),
            existing.get("site_statuses", {}),
            now_value,
        )
        payload = {
            **existing,
            "task_id": task_id,
            "title": str(data.get("title") or task_title(stored_task) or task_id),
            "task": stored_task,
            "operator": operator_label,
            "user": operator_label,
            "synced_account": synced_account,
            "site_login_accounts": site_login_accounts,
            "worker_id": event["worker_id"],
            "package_version": event["package_version"] or str(existing.get("package_version") or ""),
            "overall_status": str(data.get("overall_status") or existing.get("overall_status") or ""),
            "site_statuses": site_statuses,
            "completion": task_completion_snapshot(
                {
                    "task": stored_task,
                    "site_statuses": site_statuses,
                }
            ),
            "created_at": str(data.get("created_at") or existing.get("created_at") or now),
            "updated_at": now,
            "last_action": event["action"],
            "last_status": event["status"],
            "last_detail": event["detail"],
            "events": events,
        }
        reports.insert(0, payload)
        output = {"tasks": reports}
        write_json_atomic(path, output)
        write_json_atomic(public_pc_report_backup_file(), output)
        return payload


def _store_public_pc_failure_evidence(
    task_id: str,
    site_statuses: dict,
    failure_evidence: object,
    existing_site_statuses: object,
    now: datetime,
) -> dict:
    statuses = {
        str(site_key): dict(site)
        for site_key, site in site_statuses.items()
        if isinstance(site, dict)
    }
    existing_statuses = existing_site_statuses if isinstance(existing_site_statuses, dict) else {}
    for site_key, existing_site in existing_statuses.items():
        if not isinstance(existing_site, dict):
            continue
        current = statuses.setdefault(str(site_key), dict(existing_site))
        if "failure_screenshots" not in current and isinstance(existing_site.get("failure_screenshots"), list):
            current["failure_screenshots"] = list(existing_site["failure_screenshots"])
        if "failure_screenshot_error" not in current and existing_site.get("failure_screenshot_error"):
            current["failure_screenshot_error"] = str(existing_site["failure_screenshot_error"])

    evidence_by_site = failure_evidence if isinstance(failure_evidence, dict) else {}
    _cleanup_public_pc_failure_screenshots(now)
    remaining = PUBLIC_PC_FAILURE_SCREENSHOT_MAX_COUNT
    for raw_site_key, raw_evidence in evidence_by_site.items():
        site_key = str(raw_site_key)
        if site_key not in PUBLIC_PC_FAILURE_SITE_KEYS or not isinstance(raw_evidence, dict):
            continue
        site = statuses.setdefault(site_key, {"key": site_key, "status": f"{site_key}_failed"})
        stored_images = [
            dict(item)
            for item in site.get("failure_screenshots", [])
            if isinstance(item, dict) and str(item.get("url") or "")
        ]
        errors: list[str] = []
        capture_error = str(raw_evidence.get("screenshot_error") or "").strip()
        if capture_error:
            errors.append(capture_error)
        screenshots = raw_evidence.get("screenshots") if isinstance(raw_evidence.get("screenshots"), list) else []
        for item in screenshots:
            if remaining <= 0:
                errors.append("失敗截圖超過 5 張上限，後續圖片未保存。")
                break
            if not isinstance(item, dict):
                continue
            try:
                encoded = str(item.get("content_base64") or "")
                max_encoded_length = ((PUBLIC_PC_FAILURE_SCREENSHOT_MAX_BYTES + 2) // 3) * 4
                if len(encoded) > max_encoded_length:
                    raise ValueError("PNG Base64 超過 2 MB 上限")
                image_bytes = base64.b64decode(encoded, validate=True)
                if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("檔案不是有效 PNG")
                if len(image_bytes) > PUBLIC_PC_FAILURE_SCREENSHOT_MAX_BYTES:
                    raise ValueError("PNG 超過 2 MB 上限")
            except (ValueError, binascii.Error) as exc:
                errors.append(f"截圖驗證失敗：{exc}")
                continue

            root = public_pc_failure_screenshot_dir()
            root.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(image_bytes).hexdigest()[:20]
            safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._")[:80] or "task"
            safe_vehicle = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "_",
                str(item.get("vehicle") or ""),
            ).strip("._")[:40]
            vehicle_suffix = f"-{safe_vehicle}" if safe_vehicle else ""
            filename = f"{safe_task}-{site_key}{vehicle_suffix}-{digest}.png"
            target = root / filename
            if not target.exists():
                target.write_bytes(image_bytes)
            metadata = {
                "url": f"/admin/public-pc/failure-screenshots/{filename}",
                "vehicle": str(item.get("vehicle") or ""),
                "captured_at": str(item.get("captured_at") or ""),
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
            }
            stored_images = [
                existing
                for existing in stored_images
                if str(existing.get("url") or "") != metadata["url"]
            ]
            stored_images.append(metadata)
            remaining -= 1
        site["failure_screenshots"] = stored_images[-PUBLIC_PC_FAILURE_SCREENSHOT_MAX_COUNT:]
        site["failure_screenshot_error"] = "；".join(dict.fromkeys(errors))
    return statuses


def _collect_public_pc_failure_evidence(task_id: str, site_statuses: object) -> dict[str, dict]:
    statuses = site_statuses if isinstance(site_statuses, dict) else {}
    failed_site_keys = {
        str(site_key)
        for site_key, site in statuses.items()
        if (
            str(site_key) in PUBLIC_PC_FAILURE_SITE_KEYS
            and isinstance(site, dict)
            and (
                str(site.get("status") or "").endswith("_failed")
                or "error" in str(site.get("status") or "").lower()
            )
        )
    }
    if not failed_site_keys:
        return {}

    root = (artifacts_dir / "selenium").resolve()
    collected = {
        site_key: {"screenshots": [], "screenshot_error": ""}
        for site_key in failed_site_keys
    }
    if not root.exists():
        for value in collected.values():
            value["screenshot_error"] = "公務電腦找不到失敗截圖目錄。"
        return collected

    metadata_paths = sorted(
        root.glob("*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    remaining = PUBLIC_PC_FAILURE_SCREENSHOT_MAX_COUNT
    for metadata_path in metadata_paths[:200]:
        if remaining <= 0:
            break
        try:
            if metadata_path.stat().st_size > 64 * 1024:
                continue
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict) or str(metadata.get("task_id") or "") != task_id:
            continue
        site_key = str(metadata.get("site_key") or "")
        if site_key not in collected:
            continue
        screenshot_error = str(metadata.get("screenshot_error") or "").strip()
        if screenshot_error and not collected[site_key]["screenshot_error"]:
            collected[site_key]["screenshot_error"] = screenshot_error
        raw_path = str(metadata.get("screenshot_path") or "").strip()
        if not raw_path:
            continue
        try:
            screenshot_path = Path(raw_path).resolve()
            if root not in screenshot_path.parents or screenshot_path.suffix.lower() != ".png":
                continue
            image_bytes = screenshot_path.read_bytes()
        except OSError:
            continue
        if (
            not image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            or len(image_bytes) > PUBLIC_PC_FAILURE_SCREENSHOT_MAX_BYTES
        ):
            continue
        collected[site_key]["screenshots"].append(
            {
                "filename": screenshot_path.name,
                "content_base64": base64.b64encode(image_bytes).decode("ascii"),
                "vehicle": str(metadata.get("vehicle") or ""),
                "captured_at": str(metadata.get("captured_at") or ""),
            }
        )
        remaining -= 1

    for value in collected.values():
        if not value["screenshots"] and not value["screenshot_error"]:
            value["screenshot_error"] = "公務電腦未能取得失敗畫面，可能是 Chrome 已中斷。"
    return collected


def _enqueue_public_pc_report(payload: dict) -> None:
    with _public_pc_pending_report_lock:
        path = public_pc_pending_report_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _load_pending_public_pc_reports() -> list[dict]:
    with _public_pc_pending_report_lock:
        path = public_pc_pending_report_file()
        if not path.exists():
            return []
        entries: list[dict] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid public-PC outbox JSON at line {line_number}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"invalid public-PC outbox entry at line {line_number}")
            if not str(payload.get("event_id") or "").strip():
                raise ValueError(f"missing public-PC outbox event ID at line {line_number}")
            entries.append(payload)
        return entries


def _public_pc_report_spool_digest(event_id: str) -> str:
    return hashlib.sha256(str(event_id).encode("utf-8")).hexdigest()


def _persist_public_pc_report_spool(payload: dict) -> Path:
    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("missing public-PC spool event ID")
    spool_dir = public_pc_pending_report_spool_dir()
    spool_dir.mkdir(parents=True, exist_ok=True)
    digest = _public_pc_report_spool_digest(event_id)
    existing = sorted(spool_dir.glob(f"*-{digest}.json"))
    path = existing[0] if existing else spool_dir / f"{time.time_ns():020d}-{digest}.json"
    write_json_atomic(path, payload)
    return path


def _load_public_pc_report_spool_entry(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid public-PC spool entry: {path.name}")
    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        raise ValueError(f"missing public-PC spool event ID: {path.name}")
    if not path.name.endswith(f"-{_public_pc_report_spool_digest(event_id)}.json"):
        raise ValueError(f"public-PC spool event ID mismatch: {path.name}")
    return payload


def _deliver_public_pc_report_spool(server_url: str, *, retain_after_ack: bool = False) -> bool:
    spool_dir = public_pc_pending_report_spool_dir()
    if not spool_dir.exists():
        return True
    for path in sorted(spool_dir.glob("*.json")):
        try:
            payload = _load_public_pc_report_spool_entry(path)
            if retain_after_ack and payload.get("_spool_checkpoint_acked") is True:
                continue
            expected_event_id = str(payload["event_id"]).strip()
            outbound = dict(payload)
            outbound.pop("_spool_checkpoint_acked", None)
            ack_payload = _post_public_pc_report(server_url, outbound)
            ack_id = (
                str(ack_payload.get("ack_id") or "").strip()
                if isinstance(ack_payload, dict)
                else ""
            )
            if ack_id != expected_event_id:
                return False
            if retain_after_ack:
                payload["_spool_checkpoint_acked"] = True
                write_json_atomic(path, payload)
            else:
                path.unlink()
        except PUBLIC_PC_LEGACY_RECONCILE_ERRORS as exc:
            print(
                f"[public_pc_report] spool delivery deferred file={path.name} "
                f"error={type(exc).__name__}",
                flush=True,
            )
            return False
    return True


def _write_pending_public_pc_reports(entries: list[dict]) -> None:
    with _public_pc_pending_report_lock:
        path = public_pc_pending_report_file()
        if not entries:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n"
        tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            tmp_path.write_text(body, encoding="utf-8")
            tmp_path.replace(path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _post_public_pc_report(server_url: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/worker/public-pc-task-events",
        data=data,
        headers={**worker_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as response:
        response_body = response.read().decode("utf-8")
    try:
        return json.loads(response_body) if response_body else {}
    except json.JSONDecodeError:
        return {}


def _deliver_pending_public_pc_reports(
    server_url: str,
    pending: list[dict],
    *,
    task_id: str = "",
    action: str = "",
) -> bool:
    sent_count = 0
    try:
        for index, entry in enumerate(pending, start=1):
            expected_event_id = str(entry.get("event_id") or "").strip()
            if not expected_event_id:
                break
            ack_payload = _post_public_pc_report(server_url, entry)
            ack_id = (
                str(ack_payload.get("ack_id") or "").strip()
                if isinstance(ack_payload, dict)
                else ""
            )
            if ack_id != expected_event_id:
                break
            sent_count = index
    except (OSError, urllib.error.URLError) as exc:
        remaining = pending[sent_count:]
        _write_pending_public_pc_reports(remaining)
        context = f" task_id={task_id} action={action}" if task_id or action else ""
        print(f"[public_pc_report] pending{context} server={server_url} error={exc}", flush=True)
        return not remaining

    remaining = pending[sent_count:]
    _write_pending_public_pc_reports(remaining)
    return not remaining


def flush_pending_public_pc_reports() -> bool:
    if not public_pc_reporting_enabled():
        return False
    server_url = public_pc_report_server_url()
    if not server_url:
        return False
    main_flushed = False
    spool_flushed = False
    spool_delivery_allowed = True
    main_unreadable = False
    with _public_pc_pending_report_lock:
        try:
            pending = _load_pending_public_pc_reports()
            main_flushed = not pending or _deliver_pending_public_pc_reports(server_url, pending)
            spool_delivery_allowed = main_flushed
        except PUBLIC_PC_LEGACY_RECONCILE_ERRORS as exc:
            main_unreadable = True
            print(
                f"[public_pc_report] pending flush deferred server={server_url} "
                f"error={type(exc).__name__}",
                flush=True,
            )
        if spool_delivery_allowed:
            try:
                spool_flushed = _deliver_public_pc_report_spool(
                    server_url,
                    retain_after_ack=main_unreadable,
                )
            except PUBLIC_PC_LEGACY_RECONCILE_ERRORS as exc:
                print(
                    f"[public_pc_report] spool flush deferred server={server_url} "
                    f"error={type(exc).__name__}",
                    flush=True,
                )
    return main_flushed and spool_flushed


def report_public_pc_task_event(payload: dict, action: str, *, event_id: str = "") -> bool:
    if not public_pc_reporting_enabled():
        return False
    task = dict(payload.get("task") or {})
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return False
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    latest_event = events[-1] if events else {}
    operator_label = current_public_pc_user_label()
    site_login_accounts = public_pc_site_login_accounts(task)
    site_statuses = payload.get("site_statuses") or {}
    body = {
        "event_id": str(event_id or "").strip() or str(uuid4()),
        "task_id": task_id,
        "task": task,
        "title": task_title(task),
        "operator": operator_label,
        "user": operator_label,
        "synced_account": operator_label,
        "site_login_accounts": site_login_accounts,
        "worker_id": os.getenv("WORKER_ID", socket.gethostname() or "public-duty-pc"),
        "package_version": package_version(),
        "action": public_pc_action_for_task(task, action),
        "status": str(latest_event.get("status") or payload.get("overall_status") or ""),
        "detail": str(latest_event.get("detail") or ""),
        "overall_status": str(payload.get("overall_status") or ""),
        "site_statuses": site_statuses,
        "completion": task_completion_snapshot(payload),
        "created_at": str(payload.get("created_at") or ""),
    }
    failure_evidence = _collect_public_pc_failure_evidence(task_id, site_statuses)
    if failure_evidence:
        body["failure_evidence"] = failure_evidence
    server_url = public_pc_report_server_url()
    with _public_pc_pending_report_lock:
        try:
            spool_path = _persist_public_pc_report_spool(body)
        except PUBLIC_PC_LEGACY_RECONCILE_ERRORS as exc:
            print(
                f"[public_pc_report] spool unavailable task_id={task_id} action={action} "
                f"error={type(exc).__name__}",
                flush=True,
            )
            return False

        if not server_url:
            return True

        spool_paths = sorted(public_pc_pending_report_spool_dir().glob("*.json"))
        if spool_paths and spool_paths[0] != spool_path:
            flush_pending_public_pc_reports()
            return True

        try:
            pending = _load_pending_public_pc_reports()
            body_event_id = str(body["event_id"])
            if not any(str(entry.get("event_id") or "").strip() == body_event_id for entry in pending):
                pending.append(body)
            _write_pending_public_pc_reports(pending)
            try:
                spool_path.unlink()
            except FileNotFoundError:
                pass
        except PUBLIC_PC_LEGACY_RECONCILE_ERRORS as exc:
            print(
                f"[public_pc_report] outbox unavailable task_id={task_id} action={action} "
                f"error={type(exc).__name__}",
                flush=True,
            )
            return True

        try:
            pending = _load_pending_public_pc_reports()
            _deliver_pending_public_pc_reports(
                server_url,
                pending,
                task_id=task_id,
                action=action,
            )
        except PUBLIC_PC_LEGACY_RECONCILE_ERRORS as exc:
            # The complete queue was already persisted before delivery began.
            # Keep the UI responsive and let the background flusher retry it.
            print(
                f"[public_pc_report] delivery deferred task_id={task_id} action={action} "
                f"server={server_url} error={type(exc).__name__}",
                flush=True,
            )
    return True


def public_pc_reporting_enabled() -> bool:
    value = os.getenv("PUBLIC_PC_REPORT_ENABLED", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def reconcile_legacy_public_pc_tasks() -> int:
    if not public_pc_reporting_enabled():
        return 0
    flush_pending_public_pc_reports()
    try:
        recent_tasks = store.list_recent(limit=PUBLIC_PC_LEGACY_RECONCILE_LIMIT)
    except PUBLIC_PC_LEGACY_RECONCILE_ERRORS as exc:
        print(
            f"[public_pc_reconcile] unable to list local tasks error={type(exc).__name__}",
            flush=True,
        )
        return 0

    changed_count = 0
    for candidate in recent_tasks:
        task = candidate.get("task") if isinstance(candidate, dict) else None
        task_id = str(task.get("task_id") or "").strip() if isinstance(task, dict) else ""
        if not task_id:
            print("[public_pc_reconcile] skipped local task without task_id", flush=True)
            continue
        try:
            payload, changed = store.reconcile_legacy_silent_save_results(task_id)
        except PUBLIC_PC_LEGACY_RECONCILE_ERRORS as exc:
            print(
                f"[public_pc_reconcile] skipped task_id={task_id} error={type(exc).__name__}",
                flush=True,
            )
            continue
        report_event_id = pending_legacy_silent_save_report_event_id(payload)
        if changed:
            changed_count += 1
        if not report_event_id:
            continue
        try:
            report_enqueued = report_public_pc_task_event(
                payload,
                "舊版無提示儲存狀態自動校正",
                event_id=report_event_id,
            )
            if report_enqueued:
                store.mark_legacy_silent_save_report_enqueued(task_id, report_event_id)
        except PUBLIC_PC_LEGACY_RECONCILE_ERRORS as exc:
            print(
                f"[public_pc_reconcile] report deferred task_id={task_id} error={type(exc).__name__}",
                flush=True,
            )
    if changed_count:
        print(f"[public_pc_reconcile] corrected_tasks={changed_count}", flush=True)
    return changed_count


def start_public_pc_legacy_reconciliation() -> threading.Thread | None:
    if not public_pc_reporting_enabled():
        return None
    thread = threading.Thread(
        target=reconcile_legacy_public_pc_tasks,
        name="public-pc-legacy-reconciliation",
        daemon=True,
    )
    thread.start()
    return thread


def retry_pending_public_pc_reports() -> int:
    return reconcile_legacy_public_pc_tasks()


def _run_public_pc_pending_report_flush_loop() -> None:
    while public_pc_reporting_enabled():
        try:
            retry_pending_public_pc_reports()
        except Exception as exc:
            print(
                f"[public_pc_report] background retry failed error={type(exc).__name__}",
                flush=True,
            )
        time.sleep(PUBLIC_PC_PENDING_REPORT_FLUSH_INTERVAL_SECONDS)


def start_public_pc_pending_report_flusher() -> threading.Thread | None:
    if not public_pc_reporting_enabled():
        return None
    thread = threading.Thread(
        target=_run_public_pc_pending_report_flush_loop,
        name="public-pc-pending-report-flusher",
        daemon=True,
    )
    thread.start()
    return thread


def public_pc_action_for_task(task: dict, action: str) -> str:
    site_count = task_site_count_label(task)
    text = str(action or "")
    for old_count in ("四站", "五站"):
        text = text.replace(f"{old_count}登打", f"{site_count}登打")
    return text


def public_pc_report_server_url() -> str:
    server_url = os.getenv("PUBLIC_PC_REPORT_SERVER_URL") or os.getenv("WORKER_SERVER_URL", "")
    server_url = server_url.strip().rstrip("/")
    if not server_url:
        return ""
    if server_url == local_web_base_url():
        return ""
    return server_url


def fetch_nas_vehicle_settings() -> dict[str, list[dict[str, str]]] | None:
    server_url = nas_vehicle_settings_server_url()
    if not server_url:
        return None
    req = urllib.request.Request(
        f"{server_url}/worker/vehicle-settings",
        headers=worker_headers(),
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    ems_vehicles = clean_remote_vehicle_records(payload.get("ems_vehicles"), disaster=False)
    disaster_vehicles = clean_remote_vehicle_records(payload.get("disaster_vehicles"), disaster=True)
    if ems_vehicles is None or disaster_vehicles is None:
        return None
    return {"ems_vehicles": ems_vehicles, "disaster_vehicles": disaster_vehicles}


def nas_vehicle_settings_server_url() -> str:
    server_url = os.getenv("WORKER_SERVER_URL", "").strip().rstrip("/")
    if not server_url or server_url == local_web_base_url():
        return ""
    return server_url


def clean_remote_vehicle_records(value: object, *, disaster: bool) -> list[dict[str, str]] | None:
    if not isinstance(value, list):
        return None
    records: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        record = {
            "label": label,
            "ppe_name": str(item.get("ppe_name") or "").strip(),
        }
        if disaster:
            record["recorder_code"] = str(item.get("recorder_code") or "").strip()
        records.append(record)
    return records


def effective_ems_vehicle_options() -> list[str]:
    if request_is_local_host():
        settings = fetch_nas_vehicle_settings()
        if settings is not None:
            return [record["label"] for record in settings["ems_vehicles"]]
    return vehicle_options(artifacts_dir)


def effective_disaster_vehicle_records() -> list[dict[str, str]]:
    if request_is_local_host():
        settings = fetch_nas_vehicle_settings()
        if settings is not None:
            return settings["disaster_vehicles"]
    return load_disaster_vehicle_records(artifacts_dir)


def effective_disaster_vehicle_options() -> list[str]:
    return [record["label"] for record in effective_disaster_vehicle_records()]


def effective_disaster_vehicle_recorder_codes() -> dict[str, str]:
    return {
        record["label"]: record["recorder_code"]
        for record in effective_disaster_vehicle_records()
        if record.get("recorder_code")
    }


def current_public_pc_user_label() -> str:
    credential = load_synced_worker_credential()
    if credential is None:
        account = os.getenv("DUTY_ACCOUNT", "").strip()
        return account or "未知使用者"
    actor = f"{credential.actor_no}番" if credential.actor_no else "未填番號"
    account = credential.user_id or "未填帳號"
    name = credential.name or _name_from_display_name(credential.display_name, account=credential.user_id, actor_no=credential.actor_no) or "未填姓名"
    return f"{actor} {name} - {account}"


def public_pc_site_login_accounts(task: dict) -> dict[str, str]:
    try:
        request_model = AmbulanceReturnRequest.from_dict(task)
    except Exception:
        return {}
    try:
        return site_login_account_summaries(request_model)
    except Exception:
        return {}


def _name_from_display_name(display_name: str, account: str = "", actor_no: str = "") -> str:
    text = str(display_name or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\s*\d+\s*番\s*", "", text).strip()
    account_text = str(account or "").strip()
    actor_text = str(actor_no or "").strip()
    if account_text and text.lower() == account_text.lower():
        return ""
    if actor_text and text == actor_text:
        return ""
    return text


def site_display_name(site_key: str) -> str:
    for site in SITE_DEFINITIONS:
        if site.key == site_key:
            return site.name
    return site_key


def effective_task_execution_mode() -> str:
    desktop_fast_mode = os.getenv("DESKTOP_FAST_MODE", "auto").strip().lower()
    if desktop_fast_mode in {"1", "true", "yes", "on"}:
        return "desktop_fast"
    if desktop_fast_mode in {"0", "false", "no", "off"}:
        return "worker_queue"
    if desktop_fast_mode in {"", "auto"} and request_is_local_host():
        return "desktop_fast"
    return task_execution_mode()


def request_is_local_host() -> bool:
    host = _host_without_port(request.host)
    return host.lower() in local_host_candidates()


def local_host_candidates() -> set[str]:
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    try:
        names = {socket.gethostname(), socket.getfqdn()}
        for name in list(names):
            if not name:
                continue
            local_hosts.add(name)
            try:
                hostname, aliases, addresses = socket.gethostbyname_ex(name)
                local_hosts.add(hostname)
                local_hosts.update(aliases)
                local_hosts.update(addresses)
            except OSError:
                pass
            try:
                for item in socket.getaddrinfo(name, None):
                    sockaddr = item[4]
                    if sockaddr:
                        local_hosts.add(str(sockaddr[0]))
            except OSError:
                pass
    except OSError:
        pass
    return {item.lower() for item in local_hosts if item}


def _host_without_port(value: str) -> str:
    host = str(value or "").strip()
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def worker_authorized() -> bool:
    expected = os.getenv("WORKER_TOKEN", "").strip()
    if not expected:
        return False
    supplied = request.headers.get("X-Worker-Token", "").strip()
    return supplied == expected


def worker_headers() -> dict[str, str]:
    token = os.getenv("WORKER_TOKEN", "").strip()
    return {"X-Worker-Token": token} if token else {}


@app.post("/line/webhook")
def line_webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")
    if not verify_signature(os.getenv("LINE_CHANNEL_SECRET", "").strip(), body, signature):
        abort(403)

    payload = json.loads(body.decode("utf-8"))
    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue
        message = event.get("message", {})
        if message.get("type") != "text":
            continue
        text = message.get("text", "").strip()
        reply_token = event.get("replyToken", "")
        source = event.get("source", {})
        reply_to = source.get("groupId") or source.get("roomId") or source.get("userId") or ""
        handle_text_message(text, reply_token, reply_to)
    return jsonify({"ok": True})


def handle_text_message(text: str, reply_token: str = "", reply_to: str = "") -> str:
    if text in {"\u7bc4\u4f8b", "\u6551\u8b77\u7bc4\u4f8b"}:
        response = "\u8acb\u7528\u9019\u500b\u683c\u5f0f\u547c\u53eb\uff1a\n\n" + example_command()
        reply_text(reply_token, response)
        return response
    if text in {"\u72c0\u614b", "\u6551\u8b77\u72c0\u614b"}:
        response = runner.latest_status_text()
        reply_text(reply_token, response)
        return response
    if text.startswith(COMMAND_PREFIX):
        task_request = parse_request(text)
        store.create(task_request)
        runner.start_existing(task_request.task_id, reply_to=reply_to)
        response = (
            f"\u5df2\u6536\u5230\u6551\u8b77\u56de\u7a0b\u4efb\u52d9\uff1a{task_request.task_id}\n"
            "\u4efb\u52d9\u6703\u9001\u5230\u57f7\u884c Flask \u7684\u9019\u53f0\u96fb\u8166\u4e0a\u958b\u555f\u700f\u89bd\u5668\u3002\n\n"
            f"{task_request.summary}"
        )
        reply_text(reply_token, response)
        return response
    response = "\u76ee\u524d\u652f\u63f4\uff1a\u7bc4\u4f8b\u3001\u72c0\u614b\u3001\u6551\u8b77\u56de\u7a0b\u3002\u50b3\u300c\u7bc4\u4f8b\u300d\u770b\u683c\u5f0f\u3002"
    reply_text(reply_token, response)
    return response


def read_case_lookup() -> dict:
    path = artifacts_dir / "cases" / "latest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "case_lookup_unreadable",
            "detail": "\u6848\u4ef6\u67e5\u8a62\u7d50\u679c\u8b80\u53d6\u5931\u6557\u3002",
            "cases": [],
        }


def case_lookup_debug_artifacts() -> list[dict[str, str]]:
    output_dir = artifacts_dir / "selenium"
    items: list[dict[str, str]] = []
    for path in sorted(output_dir.glob("case_lookup-duty_cases.*"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.suffix.lower() not in {".png", ".html"}:
            continue
        try:
            href = str(path.relative_to(artifacts_dir)).replace("\\", "/")
        except ValueError:
            continue
        items.append({"name": path.name, "href": href})
    return items


def case_lookup_request_path() -> Path:
    return artifacts_dir / "cases" / "request.json"


def read_case_lookup_request() -> dict:
    path = case_lookup_request_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_case_lookup_request(lookup_range: str, source: str = "", mode: str = "worker_queue") -> dict:
    output_dir = artifacts_dir / "cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    range_label = case_lookup_range_label(lookup_range)
    payload = {
        "request_id": str(uuid4()),
        "status": "case_lookup_requested",
        "lookup_range": lookup_range,
        "mode": mode,
        "source": source or "未知來源",
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "detail": f"已送出案件查詢，正在查詢{range_label}案件。",
    }
    write_json_atomic(case_lookup_request_path(), payload)
    return payload


def case_lookup_range_label(lookup_range: str) -> str:
    return "最近 24 小時"


def case_lookup_source_label(host: str) -> str:
    return "本機端" if _host_without_port(host).lower() in local_host_candidates() else "NAS端"


def case_lookup_request_stale_seconds() -> int:
    try:
        return max(60, int(os.getenv("CASE_LOOKUP_STALE_SECONDS", "180")))
    except ValueError:
        return 180


def case_lookup_request_age_seconds(payload: dict) -> float | None:
    requested_at = str(payload.get("requested_at") or "").strip()
    if not requested_at:
        return None
    try:
        requested = datetime.fromisoformat(requested_at)
    except ValueError:
        return None
    return max(0.0, (datetime.now() - requested).total_seconds())


def case_lookup_request_is_stale(payload: dict) -> bool:
    age_seconds = case_lookup_request_age_seconds(payload)
    return age_seconds is not None and age_seconds > case_lookup_request_stale_seconds()


def case_lookup_request_needs_local_thread(payload: dict) -> bool:
    mode = str(payload.get("mode") or "").strip().lower()
    if mode == "desktop_fast":
        return True
    if mode:
        return False
    try:
        return effective_task_execution_mode() == "desktop_fast"
    except RuntimeError:
        return False


def mark_case_lookup_request_completed(latest_payload: dict) -> None:
    current = read_case_lookup_request()
    current_request_id = str(current.get("request_id") or "").strip()
    result_request_id = str(latest_payload.get("request_id") or "").strip()
    if (
        current.get("status") != "case_lookup_requested"
        or (current_request_id and result_request_id != current_request_id)
        or (not current_request_id and result_request_id)
    ):
        return
    payload = {
        **current,
        "status": "case_lookup_completed",
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "case_hash": latest_payload.get("case_hash", ""),
        "case_count": len(latest_payload.get("cases") or []),
    }
    write_json_atomic(case_lookup_request_path(), payload)


def mark_case_lookup_request_failed(latest_payload: dict) -> None:
    current = read_case_lookup_request()
    current_request_id = str(current.get("request_id") or "").strip()
    result_request_id = str(latest_payload.get("request_id") or "").strip()
    if (
        current.get("status") != "case_lookup_requested"
        or (current_request_id and result_request_id != current_request_id)
        or (not current_request_id and result_request_id)
    ):
        return
    payload = {
        **current,
        "status": "case_lookup_failed",
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "detail": str(latest_payload.get("detail") or "案件查詢失敗，請重新查詢。"),
        "case_count": len(latest_payload.get("cases") or []),
    }
    write_json_atomic(case_lookup_request_path(), payload)


def local_case_lookup_thread_is_running() -> bool:
    with _local_case_lookup_thread_lock:
        return _local_case_lookup_thread is not None and _local_case_lookup_thread.is_alive()


def _run_and_clear_local_case_lookup(lookup_range: str, request_id: str) -> None:
    global _local_case_lookup_thread
    try:
        run_local_case_lookup(lookup_range, request_id=request_id)
    finally:
        with _local_case_lookup_thread_lock:
            if _local_case_lookup_thread is threading.current_thread():
                _local_case_lookup_thread = None


def start_local_case_lookup(lookup_range: str) -> threading.Thread:
    global _local_case_lookup_thread
    with _local_case_lookup_thread_lock:
        if _local_case_lookup_thread is not None and _local_case_lookup_thread.is_alive():
            return _local_case_lookup_thread
        request_id = str(read_case_lookup_request().get("request_id") or "").strip()
        thread = threading.Thread(target=_run_and_clear_local_case_lookup, args=(lookup_range, request_id), daemon=True)
        _local_case_lookup_thread = thread
    thread.start()
    return thread


def case_lookup_process_timeout_seconds() -> float:
    try:
        return max(30.0, float(os.getenv("CASE_LOOKUP_PROCESS_TIMEOUT_SECONDS", "150")))
    except ValueError:
        return 150.0


def case_lookup_cleanup_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("CASE_LOOKUP_CLEANUP_TIMEOUT_SECONDS", "10")))
    except ValueError:
        return 10.0


def case_lookup_process_env() -> dict[str, str]:
    env = dict(os.environ)
    env["SELENIUM_REMOTE_URL"] = ""
    env["SELENIUM_DETACH"] = "false"
    env["SELENIUM_HEADLESS"] = "true"
    env["SELENIUM_HEADLESS_ARG"] = "--headless=new"
    env["OPEN_LOCAL_BROWSER_ON_RUN"] = "false"
    return env


def run_case_lookup_query(lookup_range: str):
    from ambulance_bot.selenium_local import DutyCaseLookupResult

    cmd = [
        sys.executable,
        "-m",
        "ambulance_bot.case_lookup_runner",
        "--artifacts-dir",
        str(artifacts_dir),
        "--lookup-range",
        lookup_range,
    ]
    timeout = case_lookup_process_timeout_seconds()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=case_lookup_process_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _relay_case_lookup_child_output(exc.output, exc.stderr)
        raise CaseLookupProcessTimeout(f"案件查詢啟動 Chrome/查詢程序超過 {timeout:g} 秒未完成，已中止。") from exc

    _relay_case_lookup_child_output(completed.stdout, completed.stderr)
    payload = read_case_lookup()
    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    if not payload:
        status = "case_lookup_failed"
        detail = f"案件查詢子程序未產生結果，退出碼 {completed.returncode}"
    else:
        status = str(payload.get("status") or "case_lookup_failed")
        detail = str(payload.get("detail") or "")
    ok = completed.returncode == 0 and status == "cases_loaded"
    return DutyCaseLookupResult(ok, status, detail, cases, artifacts_dir / "cases" / "latest.json")


def _relay_case_lookup_child_output(stdout: object, stderr: object) -> None:
    for line in _process_output_lines(stdout):
        print(line, flush=True)
    for line in _process_output_lines(stderr):
        print(line, flush=True)


def _process_output_lines(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return [line for line in text.splitlines() if line.strip()]


def cleanup_case_lookup_timeout_residue() -> int:
    result: dict[str, int | Exception] = {"killed": 0}

    def _cleanup() -> None:
        try:
            result["killed"] = cleanup_worker_chrome_residue(
                _WorkerBrowserCleanupOptions([f"--user-data-dir={runtime_profile_root()}"]),
                "case lookup timeout",
                include_generated_profiles=True,
                profile_root=runtime_profile_root(),
            )
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_cleanup, name="case-lookup-cleanup", daemon=True)
    thread.start()
    thread.join(case_lookup_cleanup_timeout_seconds())
    if thread.is_alive():
        print("[worker] case lookup cleanup still running in background", flush=True)
        return 0
    error = result.get("error")
    if isinstance(error, Exception):
        print(f"[worker] case lookup cleanup skipped: {error}", flush=True)
        return 0
    return int(result.get("killed") or 0)


def run_local_case_lookup(lookup_range: str, request_id: str = "") -> None:
    request_id = str(request_id or read_case_lookup_request().get("request_id") or "").strip()
    try:
        result = run_case_lookup_query(lookup_range)
    except CaseLookupProcessTimeout as exc:
        killed = cleanup_case_lookup_timeout_residue()
        detail = str(exc) or "Chrome startup timed out"
        if killed:
            detail = f"{detail} 已清理 {killed} 個殘留 Chrome/ChromeDriver。"
        payload = {
            "request_id": request_id,
            "status": "case_lookup_timeout",
            "detail": detail,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "lookup_range": lookup_range,
            "source": "local_public_duty_pc",
            "case_hash": "",
            "case_count": 0,
            "cases": [],
        }
        write_json_atomic(artifacts_dir / "cases" / "latest.json", payload)
        mark_case_lookup_request_failed(payload)
        print(f"[worker] case lookup result status=case_lookup_timeout count=0 detail={detail}", flush=True)
        return
    except Exception as exc:
        payload = {
            "request_id": request_id,
            "status": "case_lookup_failed",
            "detail": f"案件查詢失敗：{exc}",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "lookup_range": lookup_range,
            "source": "local_public_duty_pc",
            "case_hash": "",
            "case_count": 0,
            "cases": [],
        }
        write_json_atomic(artifacts_dir / "cases" / "latest.json", payload)
        mark_case_lookup_request_failed(payload)
        print("[worker] case lookup result status=case_lookup_failed count=0 detail=failed", flush=True)
        return
    cases = result.cases if isinstance(result.cases, list) else []
    payload = read_case_lookup()
    if not payload:
        payload = {
            "status": result.status,
            "detail": result.detail,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "cases": cases,
        }
    payload["lookup_range"] = lookup_range
    payload["request_id"] = request_id
    payload["source"] = "local_public_duty_pc"
    payload["case_hash"] = hash_cases(cases)
    payload["case_count"] = len(cases)
    write_json_atomic(artifacts_dir / "cases" / "latest.json", payload)
    if result.ok and result.status == "cases_loaded":
        mark_case_lookup_request_completed(payload)
    else:
        mark_case_lookup_request_failed(payload)
    print(f"[worker] case lookup result status={result.status} count={len(cases)} detail={result.detail}", flush=True)


def hash_cases(cases: list[dict[str, object]]) -> str:
    normalized = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def changed_sites_for_task_edit(previous_task: dict, current_task: dict) -> set[str]:
    return changed_site_keys(analyze_task_edit(previous_task, current_task))


def site_update_contexts_for_task_edit(previous_task: dict, current_task: dict, changed_site_keys: set[str]) -> dict[str, dict[str, object]]:
    return {
        site_key: {
            "previous_task": previous_task,
            "current_task": current_task,
        }
        for site_key in changed_site_keys
    }


def status_label(status: str) -> str:
    value = str(status or "")
    if site_waits_for_confirmation(value):
        return "待人工確認"
    if value == "desktop_fast_running":
        return "本機快速執行"
    if value == "desktop_fast_completed_with_errors":
        return "部分失敗"
    if value == "desktop_fast_completed":
        return "完成"
    if value == "site_run_completed":
        return "部分完成"
    if value == "site_needs_update" or value.endswith("_needs_update"):
        return "需更新"
    if value in {"not_started", ""}:
        return "未執行"
    if value in {"completed_by_user"} or value.endswith("_saved"):
        return "完成"
    if "running" in value or value in {"queued_for_worker", "claimed_by_worker"}:
        return "執行中"
    if "failed" in value or "error" in value:
        return "失敗"
    if "captcha" in value or "ready" in value or "prefilled" in value:
        return "待確認"
    return "其他"


def status_class(status: str) -> str:
    value = str(status or "")
    if site_waits_for_confirmation(value):
        return "waiting"
    if value == "desktop_fast_completed":
        return "complete"
    if value == "site_run_completed":
        return "waiting"
    if value == "desktop_fast_completed_with_errors":
        return "failed"
    if value == "site_needs_update" or value.endswith("_needs_update"):
        return "waiting"
    if value in {"completed_by_user"} or value.endswith("_saved"):
        return "complete"
    if "failed" in value or "error" in value:
        return "failed"
    if "running" in value or value in {"queued_for_worker", "claimed_by_worker"}:
        return "running"
    if "captcha" in value or "ready" in value or "prefilled" in value:
        return "waiting"
    return "idle"


def site_is_complete(status: str) -> bool:
    return status_class(status) == "complete"


def site_waits_for_confirmation(status: str) -> bool:
    value = str(status or "")
    return "waiting_confirmation" in value or value == "manual_confirmation_required"


def task_has_waiting_confirmation(site_statuses: dict) -> bool:
    return any(
        site_waits_for_confirmation(str(dict(site or {}).get("status") or ""))
        for site in dict(site_statuses or {}).values()
    )


def task_edit_is_locked(payload: dict) -> bool:
    return task_payload_is_active_for_edit(payload) or task_has_waiting_confirmation(
        dict(payload.get("site_statuses") or {})
    )


def task_edit_lock_message(payload: dict) -> str:
    if task_has_waiting_confirmation(dict(payload.get("site_statuses") or {})):
        return "任務尚有待人工確認的資料，請先到官方網頁核對或完成人工更新，再按「已確認」。"
    return "任務正在執行中，請等待完成或先中止登打後再編輯。"


def site_can_run_individually(site_statuses: dict, site_key: str) -> bool:
    blocked = False
    for ordered_key in SITE_RUN_ORDER:
        site = dict(site_statuses.get(ordered_key) or {})
        current_status = str(site.get("status") or "")
        current_class = status_class(current_status)
        if current_class == "failed":
            blocked = True
        if ordered_key != site_key:
            continue
        if current_status.endswith("_needs_update"):
            return True
        return current_class == "failed" or (blocked and current_class != "complete")
    return False


def site_action_button_label(status: str, site_key: str) -> str:
    if str(status or "").endswith("_needs_update"):
        return SITE_UPDATE_BUTTON_LABELS.get(site_key, "更新")
    return "單獨登打"


def combined_mileage_site_status(site_statuses: dict, task: dict) -> dict:
    mileage_site = dict((site_statuses or {}).get("vehicle_mileage") or {})
    if not task_has_active_fuel_site(task, site_statuses):
        mileage_site["run_site_key"] = "vehicle_mileage"
        return mileage_site

    fuel_site = dict((site_statuses or {}).get("fuel_record") or {})
    mileage_status = str(mileage_site.get("status") or "")
    fuel_status = str(fuel_site.get("status") or "")
    for preferred_site, status in ((fuel_site, fuel_status), (mileage_site, mileage_status)):
        if status_class(status) == "failed":
            combined = dict(preferred_site)
            combined["run_site_key"] = "fuel_record" if preferred_site is fuel_site else "vehicle_mileage"
            return combined
    for preferred_site, status in ((mileage_site, mileage_status), (fuel_site, fuel_status)):
        if status_class(status) == "running":
            combined = dict(preferred_site)
            combined["run_site_key"] = "fuel_record" if preferred_site is fuel_site else "vehicle_mileage"
            return combined
    for preferred_site, status in ((fuel_site, fuel_status), (mileage_site, mileage_status)):
        if status.endswith("_needs_update") or status_class(status) == "waiting":
            combined = dict(preferred_site)
            combined["run_site_key"] = "fuel_record" if preferred_site is fuel_site else "vehicle_mileage"
            return combined
    if status_class(mileage_status) == "complete" and status_class(fuel_status) == "complete":
        combined = dict(fuel_site or mileage_site)
        combined["run_site_key"] = "vehicle_mileage"
        return combined
    mileage_site["run_site_key"] = "vehicle_mileage"
    return mileage_site


def site_diagnostic(site: dict) -> dict[str, str]:
    return merge_diagnostic_fields(dict(site or {}))


def site_stage_rows(site_statuses: dict, site_key: str) -> list[dict[str, str]]:
    site = dict(site_statuses.get(site_key) or {})
    current_class = status_class(str(site.get("status") or ""))
    diagnostic = site_diagnostic(site)
    focus_stage = diagnostic.get("failure_stage") or ""
    stages = SITE_STAGE_DEFINITIONS.get(site_key, [])
    focus_index = stages.index(focus_stage) if focus_stage in stages else -1
    rows: list[dict[str, str]] = []
    for index, stage in enumerate(stages):
        if current_class == "complete":
            state = "已完成"
            state_class = "complete"
        elif current_class == "running":
            state = "執行中" if index == 0 else "待執行"
            state_class = "running" if index == 0 else "idle"
        elif current_class == "failed":
            if focus_index >= 0 and index < focus_index:
                state = "已通過"
                state_class = "complete"
            elif (focus_index < 0 and index == 0) or index == focus_index:
                state = "失敗點"
                state_class = "failed"
            else:
                state = "未完成"
                state_class = "idle"
        elif current_class == "waiting":
            if focus_index >= 0 and index < focus_index:
                state = "已通過"
                state_class = "complete"
            elif (focus_index < 0 and index == 0) or index == focus_index:
                state = "待確認"
                state_class = "waiting"
            else:
                state = "未完成"
                state_class = "idle"
        else:
            state = "未執行"
            state_class = "idle"
        rows.append({"name": stage, "state": state, "class": state_class})
    return rows


def effective_task_status(payload: dict) -> str:
    snapshot = task_completion_snapshot(payload)
    if snapshot["running_site_keys"]:
        return "desktop_fast_running"
    if snapshot["failed_site_keys"]:
        return "failed"
    if snapshot["needs_update_site_keys"]:
        return "site_needs_update"
    site_statuses = dict(payload.get("site_statuses") or {})
    if any(
        site_waits_for_confirmation(
            str(dict(site_statuses.get(site_key) or {}).get("status") or "")
        )
        for site_key in snapshot["waiting_site_keys"]
    ):
        return "manual_confirmation_required"
    if snapshot["waiting_site_keys"]:
        return "manual_captcha_required"
    if snapshot["all_complete"]:
        return "desktop_fast_completed"
    return str(payload.get("overall_status") or "")


def task_payload_is_active(payload: dict) -> bool:
    queue_state = worker_queue_state(payload)
    if queue_state.get("status") == "queued" or worker_claim_lease_is_active(payload):
        return True
    lock_snapshot = manual_task_lock_snapshot(artifacts_dir)
    if lock_snapshot.get("guard_busy") or task_execution_lease_owner(payload, lock_snapshot):
        return True
    return status_class(effective_task_status(payload)) == "running"


def task_execution_lease_owner(
    payload: dict,
    lock_snapshot: dict[str, object] | None = None,
) -> str:
    task = payload.get("task")
    task_id = str(task.get("task_id") or "").strip() if isinstance(task, dict) else ""
    if not task_id:
        return ""
    snapshot = lock_snapshot if lock_snapshot is not None else manual_task_lock_snapshot(artifacts_dir)
    owner = str(snapshot.get("owner") or "").strip()
    if not owner:
        return ""
    if str(snapshot.get("task_id") or "").strip() == task_id:
        return owner
    return ""


def refresh_stale_running_task(payload: dict) -> dict:
    if not task_payload_is_active(payload):
        return payload
    if worker_claim_lease_is_active(payload) or manual_task_lock_active(artifacts_dir):
        return payload
    task = payload.get("task")
    if not isinstance(task, dict):
        return payload
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return payload
    refreshed = store.expire_stale_running_sites(task_id, manual_task_lock_max_age_seconds(), STALE_RUNNING_TASK_DETAIL)
    return refreshed


def refresh_recent_tasks(recent_tasks: list[dict]) -> list[dict]:
    return [refresh_stale_running_task(dict(item or {})) for item in recent_tasks]


def task_form_recent_task_timestamp(payload: dict) -> datetime | None:
    raw_time = str(payload.get("updated_at") or payload.get("created_at") or "").strip()
    try:
        timestamp = datetime.fromisoformat(raw_time)
    except ValueError:
        return None
    if timestamp.tzinfo is not None:
        return timestamp.astimezone().replace(tzinfo=None)
    return timestamp


def recent_tasks_for_task_form(service_type: str = "ems") -> list[dict]:
    normalized_service = "disaster" if str(service_type).strip().lower() == "disaster" else "ems"
    cutoff = datetime.now() - timedelta(hours=TASK_FORM_COMPLETED_HISTORY_HOURS)
    incomplete_tasks: list[dict] = []
    recent_completed_tasks: list[dict] = []
    for payload in refresh_recent_tasks(store.list_recent(limit=TASK_FORM_RECENT_TASK_SCAN_LIMIT)):
        task = dict(payload.get("task") or {})
        task_service = "disaster" if str(task.get("service_type") or "ems").strip().lower() == "disaster" else "ems"
        if task_service != normalized_service:
            continue
        if status_class(effective_task_status(payload)) != "complete":
            incomplete_tasks.append(payload)
            continue
        updated_at = task_form_recent_task_timestamp(payload)
        if updated_at is None or updated_at >= cutoff:
            recent_completed_tasks.append(payload)
    completed_capacity = max(0, TASK_FORM_RECENT_TASK_LIMIT - len(incomplete_tasks))
    return incomplete_tasks + recent_completed_tasks[:completed_capacity]


def recent_tasks_need_refresh(recent_tasks: list[dict]) -> bool:
    return any(task_payload_is_active(dict(item or {})) for item in recent_tasks)


def task_progress_summary(payload: dict) -> str:
    snapshot = task_completion_snapshot(payload)
    completed_count = int(snapshot["completed_count"])
    total_count = int(snapshot["total_count"])
    if snapshot["all_complete"]:
        return f"{snapshot['site_count_label']}登打完成"
    if snapshot["running_site_keys"]:
        site_key = snapshot["running_site_keys"][0]
        return (
            f"已完成 {completed_count}/{total_count}；"
            f"目前：{SITE_SHORT_NAMES[site_key]}執行中"
        )
    if snapshot["needs_update_site_keys"]:
        names = "、".join(
            SITE_SHORT_NAMES[key] for key in snapshot["needs_update_site_keys"]
        )
        return f"已完成 {completed_count}/{total_count}；需更新：{names}"
    if snapshot["waiting_site_keys"]:
        site_key = snapshot["waiting_site_keys"][0]
        return (
            f"已完成 {completed_count}/{total_count}；"
            f"待確認：{SITE_SHORT_NAMES[site_key]}"
        )
    if len(snapshot["failed_site_keys"]) == 1:
        site_key = snapshot["failed_site_keys"][0]
        return (
            f"已完成 {completed_count}/{total_count}；"
            f"失敗：{SITE_SHORT_NAMES[site_key]}"
        )
    if snapshot["failed_site_keys"]:
        return (
            f"已完成 {completed_count}/{total_count}；"
            f"{len(snapshot['failed_site_keys'])} 站失敗"
        )
    return f"已完成 {completed_count}/{total_count}；尚未開始"


def task_title(task: dict) -> str:
    reason = str(task.get("case_reason") or "救護").strip()
    address = display_case_address(task).strip()
    if address:
        prefix = "救災" if str(task.get("service_type") or "ems") == "disaster" else "緊急救護"
        return f"{prefix}-{reason} - {address}"
    vehicle = str(task.get("vehicle") or "").strip()
    driver = str(task.get("driver") or "").strip()
    if vehicle or driver:
        return f"{vehicle} {driver}".strip()
    return str(task.get("task_id") or "未命名任務")


def task_vehicle_display_entries(task: dict) -> list[dict[str, object]]:
    task = dict(task or {})
    raw_entries = task.get("vehicle_entries")
    entries = raw_entries if isinstance(raw_entries, list) and raw_entries else []
    if not entries:
        entries = [task]
    multiple = len(entries) > 1
    shared_case_time = normalize_hhmm(str(task.get("case_time") or ""))
    display_entries: list[dict[str, object]] = []
    for index, raw_entry in enumerate(entries, start=1):
        entry = dict(raw_entry or {}) if isinstance(raw_entry, dict) else {}
        consumables = entry.get("consumables") if isinstance(entry.get("consumables"), dict) else {}
        consumable_parts = [
            f"{name} x{qty}"
            for name, qty in consumables.items()
            if str(name or "").strip() and _positive_quantity(qty)
        ]
        fuel_record = entry.get("fuel_record") if isinstance(entry.get("fuel_record"), dict) else {}
        fuel_enabled = form_flag_enabled(fuel_record.get("enabled")) if fuel_record else False
        fuel_parts = [
            str(fuel_record.get("date") or "").strip(),
            str(fuel_record.get("time") or "").strip(),
            str(fuel_record.get("driver") or "").strip(),
            str(fuel_record.get("product") or "").strip(),
            str(fuel_record.get("quantity") or "").strip(),
            str(fuel_record.get("unit_price") or "").strip(),
        ]
        patient_summary = str(entry.get("patient_summary") or "").strip()
        patient_gender = patient_summary.removesuffix("\u4e00\u540d")
        disinfection_items = [
            str(item).strip()
            for item in entry.get("disinfection_items", [])
            if str(item).strip()
        ] if isinstance(entry.get("disinfection_items"), list) else []
        display_entries.append(
            {
                "label": f"{index}\u8eca" if multiple else "登打明細",
                "vehicle": str(entry.get("vehicle") or "").strip(),
                "case_time": normalize_hhmm(str(entry.get("case_time") or "")) or shared_case_time,
                "driver": str(entry.get("driver") or "").strip(),
                "mileage": str(entry.get("mileage") or "").strip(),
                "return_date": str(entry.get("return_date") or "").strip(),
                "return_time": normalize_hhmm(str(entry.get("return_time") or "")),
                "patient_summary": patient_summary,
                "patient_gender": patient_gender,
                "consumables_items": list(consumable_parts),
                "consumables_summary": "\u3001".join(consumable_parts),
                "fuel_enabled": fuel_enabled,
                "fuel_time": str(fuel_record.get("time") or "").strip() if fuel_enabled else "",
                "fuel_product": str(fuel_record.get("product") or "").strip() if fuel_enabled else "",
                "fuel_quantity": str(fuel_record.get("quantity") or "").strip() if fuel_enabled else "",
                "fuel_unit_price": str(fuel_record.get("unit_price") or "").strip() if fuel_enabled else "",
                "fuel_summary": " / ".join(part for part in fuel_parts if part) if fuel_enabled else "",
                "disinfection": str(entry.get("disinfection") or "").strip(),
                "disinfection_items": disinfection_items,
                "disinfection_count": len(disinfection_items),
                "disinfection_count_text": f"\u6d88\u6bd2{len(disinfection_items)}\u9805",
            }
        )
    return display_entries


def task_has_fuel_record(task: dict) -> bool:
    return AmbulanceReturnRequest.from_dict(dict(task or {})).has_fuel_record()


def task_has_active_fuel_site(task: dict, site_statuses: dict | None = None) -> bool:
    if task_has_fuel_record(task):
        return True
    fuel_status = str(dict((site_statuses or {}).get("fuel_record") or {}).get("status") or "")
    return "waiting_confirmation" in fuel_status


def active_site_keys_for_task(task: dict, site_statuses: dict | None = None) -> list[str]:
    keys = AmbulanceReturnRequest.from_dict(dict(task or {})).active_site_keys()
    fuel_status = str(dict((site_statuses or {}).get("fuel_record") or {}).get("status") or "")
    if "waiting_confirmation" in fuel_status and "fuel_record" not in keys:
        keys.append("fuel_record")
    return keys


def task_site_count_label(task: dict, site_statuses: dict | None = None) -> str:
    return {2: "二站", 3: "三站", 4: "四站", 5: "五站"}.get(
        len(active_site_keys_for_task(task, site_statuses)),
        f"{len(active_site_keys_for_task(task, site_statuses))}站",
    )


def task_site_display_pairs(task: dict, site_statuses: dict | None = None) -> list[tuple[str, str]]:
    return [
        (site_key, SITE_DISPLAY_NAMES.get(site_key, site_key))
        for site_key in active_site_keys_for_task(task, site_statuses)
    ]


def last_vehicle_mileages(limit: int = 300) -> dict[str, str]:
    mileages: dict[str, str] = {}
    for payload in store.list_recent(limit=limit):
        if not isinstance(payload, dict):
            continue
        task = payload.get("task")
        if not isinstance(task, dict):
            continue
        for entry in task_vehicle_display_entries(task):
            vehicle = str(entry.get("vehicle") or "").strip()
            mileage = str(entry.get("mileage") or "").strip()
            if vehicle and mileage and vehicle not in mileages:
                mileages[vehicle] = mileage
    return mileages


def _positive_quantity(value: object) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def site_short_name(site: dict) -> str:
    key = str(site.get("key") or "")
    return SITE_SHORT_NAMES.get(key, str(site.get("name") or "站台"))


def display_case_title(case: dict) -> str:
    category = str(case.get("category") or "緊急救護").strip()
    return f"{category} - {display_case_address(case) or '未填地址'}"


def display_case_address(case: dict) -> str:
    direct = clean_case_address(str(case.get("address") or case.get("case_address") or ""))
    if direct:
        return direct
    description = str(case.get("description") or "")
    for marker in ("地點:", "地點："):
        if marker in description:
            return clean_case_address(description.split(marker, 1)[1])
    return ""


def task_datetime_display(task: dict, date_key: str, time_key: str) -> str:
    date_text = short_date(task.get(date_key) or task.get("case_date") or "")
    hhmm = normalize_hhmm(str(task.get(time_key) or ""))
    if date_text and hhmm:
        return f"{date_text} {hhmm}"
    return hhmm or date_text or "未填"


def case_time_range(case: dict) -> str:
    start_date = short_date(case.get("report_time") or case.get("case_date") or "")
    start_time = normalize_hhmm(str(case.get("case_time_hhmm") or _time_from_text(case.get("report_time"))))
    end_time = selected_return_time_input(case)
    start = f"{start_date} {start_time}".strip() if start_date or start_time else "未填"
    if not end_time:
        return start
    end_date = short_date(case.get("return_time") or "") or start_date
    end = f"{end_date} {end_time}".strip() if end_date or end_time else ""
    return f"{start} - {end}" if end else start


def selected_return_time_input(case: dict) -> str:
    if placeholder_return_datetime(case.get("return_time")):
        return ""
    return normalize_hhmm(str(case.get("return_time_hhmm") or _time_from_text(case.get("return_time"))))


def selected_case_address(case: dict) -> str:
    return display_case_address(case)


def selected_case_date_input(case: dict) -> str:
    return date_input_value(case.get("case_date") or case.get("report_time") or "")


def selected_return_date_input(case: dict) -> str:
    explicit_date = date_input_value(case.get("return_date") or "")
    if explicit_date:
        return explicit_date
    if not selected_return_time_input(case):
        return ""
    return date_input_value(case.get("return_time") or case.get("case_date") or case.get("report_time") or "")


def date_input_value(value: object) -> str:
    parsed = parse_datetime_text(value) or parse_case_date(str(value or ""))
    return parsed.strftime("%Y/%m/%d") if parsed else ""


def short_date(value: object) -> str:
    parsed = parse_datetime_text(value) or parse_case_date(str(value or ""))
    return parsed.strftime("%m/%d") if parsed else ""


def parse_datetime_text(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _time_from_text(value: object) -> str:
    parsed = parse_datetime_text(value)
    return parsed.strftime("%H%M") if parsed else ""


def placeholder_return_datetime(value: object) -> bool:
    parsed = parse_datetime_text(value)
    return bool(parsed and parsed.year <= 1900 and parsed.month == 1 and parsed.day == 1)


def visible_events(events: list[dict], task: dict | None = None) -> list[dict]:
    important: list[dict] = []
    seen_sites: set[str] = set()
    active_site_keys = active_site_keys_for_task(task or {}) if task is not None else list(SITE_RUN_ORDER)
    active_site_key_set = set(active_site_keys)
    for event in reversed(events):
        status = str(event.get("status") or "")
        if status in {
            "created",
            "running",
            "queued_for_worker",
            "claimed_by_worker",
            "desktop_fast_running",
            "desktop_fast_completed",
            "desktop_fast_completed_with_errors",
        }:
            continue
        if status in {"local_pc_ready", "manual_captcha_required"}:
            continue
        site_key = event_site_key(event)
        if site_key not in active_site_key_set:
            continue
        if site_key in seen_sites:
            continue
        seen_sites.add(site_key)
        important.append(event)
        if len(important) >= len(active_site_keys):
            break
    return list(reversed(important))


def event_site_key(event: dict) -> str:
    status = str(event.get("status") or "")
    detail = str(event.get("detail") or "")
    if status.startswith("vehicle_mileage") or "車輛里程" in detail:
        return "vehicle_mileage"
    if status.startswith("fuel_record") or "加油" in detail or "油耗" in detail:
        return "fuel_record"
    if status.startswith("consumables") or "一站通耗材" in detail:
        return "consumables"
    if status.startswith("disinfection") or "緊急救護消毒" in detail:
        return "disinfection"
    if status.startswith("duty_work_log") or "消防勤務工作紀錄" in detail:
        return "duty_work_log"
    return status or detail[:24]


def event_detail_text(event: dict) -> str:
    status = str(event.get("status") or "")
    detail = str(event.get("detail") or "").strip()
    if status in {"completed_by_user"} or status.endswith("_saved"):
        return "已完成"
    if "failed" in status or "error" in status:
        return detail[:80] or "執行失敗"
    if "captcha" in status or "ready" in status or "prefilled" in status:
        return "待確認"
    return detail[:80] or status_label(status)


def site_error_guidance(site_statuses: dict) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for site_key in SITE_RUN_ORDER:
        site = dict(site_statuses.get(site_key) or {})
        status = str(site.get("status") or "")
        detail = str(site.get("detail") or "").strip()
        diagnostic = site_diagnostic(site)
        current_class = status_class(status)
        site_name = SITE_SHORT_NAMES.get(site_key, site_key)

        if current_class == "failed":
            entries.append(
                {
                    "site_key": site_key,
                    "site_name": site_name,
                    "state": "失敗",
                    "stage": diagnostic.get("failure_stage") or "未判定",
                    "reason": diagnostic.get("failure_reason") or "程式回報此站未完成。",
                    "detail": detail[:160] or "程式回報此站未完成。",
                    "action": diagnostic.get("next_action") or site_next_action(site_key, status, detail),
                }
            )
            continue

        if current_class == "waiting":
            entries.append(
                {
                    "site_key": site_key,
                    "site_name": site_name,
                    "state": "待確認",
                    "stage": diagnostic.get("failure_stage") or "儲存",
                    "reason": diagnostic.get("failure_reason") or "此站需要人工確認後才能視為完成。",
                    "detail": detail[:160] or "此站需要人工確認後才能視為完成。",
                    "action": diagnostic.get("next_action") or site_next_action(site_key, status, detail),
                }
            )
            continue
    return entries


def site_next_action(site_key: str, status: str, detail: str) -> str:
    text = f"{status} {detail}".lower()
    site_name = SITE_SHORT_NAMES.get(site_key, site_key)
    can_retry_here = request_is_local_host()
    if "captcha" in text or "驗證碼" in detail or "login" in text or "登入" in detail:
        if can_retry_here:
            return f"到公務電腦 Chrome 完成登入或驗證碼，再回本頁按「單獨登打」重試{site_name}。"
        return f"到公務電腦 Chrome 完成登入或驗證碼，再回本頁確認{site_name}狀態。"
    if "chrome" in text or "session" in text or "not reachable" in text:
        return "確認 worker Chrome 沒有卡住；必要時關閉殘留 Chrome、重啟 worker，再重新登打。"
    if "missing" in text or "not found" in text or "找不到" in detail or "無法按" in detail:
        return f"頁面按鈕或欄位與程式預期不同；先在公務電腦人工完成{site_name}，保留畫面後再回報修正。"
    if "ready" in text or "prefilled" in text or "待確認" in detail:
        return f"在公務電腦確認{site_name}資料無誤並手動儲存，完成後回本頁確認狀態。"
    if can_retry_here:
        return f"先查看公務電腦該站畫面與執行紀錄；修正後按「單獨登打」重試{site_name}。"
    return f"先查看公務電腦該站畫面與執行紀錄；修正後由 worker 或本機頁面重試{site_name}。"


def event_site_name(event: dict) -> str:
    status = str(event.get("status") or "")
    detail = str(event.get("detail") or "")
    if status.startswith("vehicle_mileage") or "車輛里程" in detail:
        return "里程"
    if status.startswith("fuel_record") or "加油" in detail or "油耗" in detail:
        return "加油"
    if status.startswith("consumables") or "一站通耗材" in detail or "耗材" in detail:
        return "耗材"
    if status.startswith("disinfection") or "緊急救護消毒" in detail or "消毒" in detail:
        return "消毒"
    if status.startswith("duty_work_log") or "消防勤務工作紀錄" in detail or "工作紀錄" in detail:
        return "工作"
    return "任務"


@app.context_processor
def template_helpers() -> dict:
    return {
        "case_time_range": case_time_range,
        "combined_mileage_site_status": combined_mileage_site_status,
        "display_case_title": display_case_title,
        "effective_task_status": effective_task_status,
        "event_detail_text": event_detail_text,
        "event_site_name": event_site_name,
        "selected_case_date_input": selected_case_date_input,
        "selected_case_address": selected_case_address,
        "selected_return_date_input": selected_return_date_input,
        "selected_return_time_input": selected_return_time_input,
        "compact_login_account_summary": compact_login_account_summary,
        "recent_tasks_need_refresh": recent_tasks_need_refresh,
        "site_action_button_label": site_action_button_label,
        "site_diagnostic": site_diagnostic,
        "site_waits_for_confirmation": site_waits_for_confirmation,
        "site_manual_complete_token": site_manual_complete_token,
        "site_short_name": site_short_name,
        "site_error_guidance": site_error_guidance,
        "site_stage_rows": site_stage_rows,
        "show_nas_home_button": show_nas_home_button,
        "show_public_pc_admin_button": show_public_pc_admin_button,
        "show_vehicle_settings_button": show_vehicle_settings_button,
        "sinposmart_fire_day_label": sinposmart_fire_day_label,
        "sinposmart_person_label": sinposmart_person_label,
        "sinposmart_record_type_label": sinposmart_record_type_label,
        "sinposmart_status_class": sinposmart_status_class,
        "sinposmart_status_label": sinposmart_status_label,
        "sinposmart_trigger_label": sinposmart_trigger_label,
        "show_task_entry_controls": show_task_entry_controls,
        "status_class": status_class,
        "status_label": status_label,
        "task_datetime_display": task_datetime_display,
        "task_has_waiting_confirmation": task_has_waiting_confirmation,
        "task_payload_is_active": task_payload_is_active,
        "task_payload_is_active_for_edit": task_payload_is_active_for_edit,
        "task_edit_is_locked": task_edit_is_locked,
        "task_completion_snapshot": task_completion_snapshot,
        "task_completion_label": task_completion_label,
        "task_progress_summary": task_progress_summary,
        "task_has_active_fuel_site": task_has_active_fuel_site,
        "task_has_fuel_record": task_has_fuel_record,
        "task_site_count_label": task_site_count_label,
        "task_site_display_pairs": task_site_display_pairs,
        "task_title": task_title,
        "task_vehicle_display_entries": task_vehicle_display_entries,
        "visible_events": visible_events,
    }


def show_public_pc_admin_button() -> bool:
    return not request_is_local_host()


def show_nas_home_button() -> bool:
    return not request_is_local_host()


def show_vehicle_settings_button() -> bool:
    return not request_is_local_host()


def show_task_entry_controls() -> bool:
    return request_is_local_host()


def should_auto_queue_task_on_create() -> bool:
    return effective_task_execution_mode() == "worker_queue" and not request_is_local_host()


def queue_task_for_worker(task_id: str) -> None:
    request_payload = store.request_for(task_id)
    payload = store.get(task_id)
    site_statuses = dict(payload.get("site_statuses") or {})
    for adapter in default_adapters():
        if adapter.key not in request_payload.active_site_keys():
            continue
        site = dict(site_statuses.get(adapter.key) or {})
        status = str(site.get("status") or "")
        should_prepare = (
            status in {"", "not_started"}
            or "failed" in status
            or "error" in status
            or status.endswith("_needs_update")
        )
        if should_prepare:
            store.update_site_result(task_id, adapter.run(request_payload))
    store.queue_for_worker(task_id)


def write_selected_case_from_lookup(case_id: str) -> bool:
    lookup = read_case_lookup()
    selected = None
    for item in lookup.get("cases") or []:
        if str(item.get("case_id") or "") == case_id:
            selected = dict(item)
            break
    if selected is None:
        return False
    selected["address"] = clean_case_address(str(selected.get("address") or ""))
    category_text = " ".join(
        str(selected.get(key) or "")
        for key in ("category", "case_type", "title", "summary_type")
    )
    if "災害搶救" in category_text or "其他-打撈浮屍" in category_text:
        selected["summary_type"] = "災害搶救"
    elif "火災" in category_text:
        selected["summary_type"] = "火災"
    elif "救護" in category_text:
        selected["summary_type"] = "救護"

    output_dir = artifacts_dir / "cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "case_imported",
        "detail": "已由查詢結果帶入案件資料與出勤人員。",
        "updated_at": lookup.get("updated_at", ""),
        "selected_case": selected,
    }
    (output_dir / "selected.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def person_options_from_personnel(personnel: object) -> list[tuple[str, str]]:
    raw_people = personnel.split(",") if isinstance(personnel, str) else personnel or []
    people: list[str] = []
    seen: set[str] = set()
    for name in raw_people:
        person = str(name).strip()
        if person and person not in seen:
            people.append(person)
            seen.add(person)
    return [(name, name) for name in people]


def two_vehicle_option_available(case: dict) -> bool:
    if form_flag_enabled(case.get("two_vehicle")):
        return True
    if str(case.get("status") or "") != "case_imported":
        return False
    if len(person_options_from_personnel(case.get("personnel") or [])) <= 3:
        return False
    return selected_case_is_ambulance_case(case)


def selected_case_is_ambulance_case(case: dict) -> bool:
    text = " ".join(
        str(case.get(key) or "")
        for key in ("category", "case_type", "title", "description", "detail")
    )
    return "\u6551\u8b77" in text or "其他-打撈浮屍" in text


def read_selected_case() -> dict:
    path = artifacts_dir / "cases" / "selected.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    selected = dict(payload.get("selected_case") or {})
    people = person_options_from_personnel(selected.get("personnel") or [])
    if people:
        selected["person_options"] = people
    selected["status"] = payload.get("status", "")
    selected["detail"] = payload.get("detail", "")
    return selected


def task_form_values(task: dict) -> dict:
    values = dict(task)
    values["address"] = clean_case_address(str(task.get("case_address") or ""))
    values["case_time_hhmm"] = str(task.get("case_time") or "")
    values["return_time_hhmm"] = str(task.get("return_time") or "")
    values["reason"] = str(task.get("case_reason") or "")
    people = person_options_from_personnel(task.get("personnel") or [])
    if people:
        values["person_options"] = people
    return values


def prepared_case_lookup() -> dict:
    case_lookup = read_case_lookup()
    lookup_request = read_case_lookup_request()
    cases = case_lookup.get("cases") or []
    if _case_lookup_start_error:
        case_lookup["detail"] = _case_lookup_start_error
        case_lookup["is_running"] = False
        case_lookup["cases"] = cases
        case_lookup["case_count"] = len(cases)
        case_lookup["debug_artifacts"] = case_lookup_debug_artifacts()
        return case_lookup

    detail = str(case_lookup.get("detail") or "").strip()
    if detail:
        detail = detail.replace("緊急救護案件", "救護、火災案件")
        detail = detail.replace("前 24 小時的救護、火災案件，並預先讀取服勤人員", "24 小時內案件，並讀取出勤人員")
        detail = detail.replace("前 24 小時的緊急救護案件，並預先讀取服勤人員", "24 小時內案件，並讀取出勤人員")
        detail = detail.replace("筆24", "筆 24").replace("筆 24小時", "筆 24 小時")
        case_lookup["detail"] = detail
    if lookup_request.get("status") == "case_lookup_requested":
        lookup_range = str(lookup_request.get("lookup_range") or case_lookup.get("lookup_range") or "24h")
        range_label = case_lookup_range_label(lookup_range)
        if case_lookup_request_needs_local_thread(lookup_request) and not local_case_lookup_thread_is_running():
            case_lookup["detail"] = f"上一輪本機案件查詢已中斷，請重新查詢{range_label}案件。"
            case_lookup["is_running"] = False
            mark_case_lookup_request_failed(
                {
                    "request_id": str(lookup_request.get("request_id") or ""),
                    "detail": case_lookup["detail"],
                    "cases": [],
                }
            )
        elif case_lookup_request_is_stale(lookup_request):
            case_lookup["detail"] = f"案件查詢逾時，請確認公務電腦 Worker 是否啟動後再重新查詢{range_label}案件。"
            case_lookup["is_running"] = False
        else:
            case_lookup["detail"] = f"正在查詢{range_label}案件，請稍候。"
            case_lookup["is_running"] = True
    elif lookup_request.get("status") == "case_lookup_failed":
        case_lookup["detail"] = str(lookup_request.get("detail") or case_lookup.get("detail") or "案件查詢失敗，請重新查詢。")
        case_lookup["is_running"] = False
    elif not cases and (
        lookup_request.get("status") == "case_lookup_completed"
        or case_lookup.get("status") == "cases_loaded"
    ):
        lookup_range = str(case_lookup.get("lookup_range") or lookup_request.get("lookup_range") or "24h")
        range_label = case_lookup_range_label(lookup_range)
        case_lookup["empty_message"] = f"查詢完成，{range_label}沒有找到案件。"
    case_lookup["cases"] = cases
    case_lookup["case_count"] = len(cases)
    case_lookup["debug_artifacts"] = case_lookup_debug_artifacts()
    return case_lookup


def validate_task_form(task_request) -> list[str]:
    errors: list[str] = []
    if not task_request.case_id.strip():
        errors.append("請先從上方案件按「帶入」")
        return errors
    if not task_request.case_date.strip():
        errors.append("請填寫案件日期")
    if not normalize_hhmm(task_request.case_time):
        errors.append("請填寫案件時間")
    if not task_request.case_address.strip():
        errors.append("請填寫案發地址")

    if task_request.two_vehicle:
        vehicle_requests = task_request.vehicle_requests()
        selected_vehicles = [item.vehicle.strip() for item in vehicle_requests if item.vehicle.strip()]
        if len(selected_vehicles) != len(set(selected_vehicles)):
            errors.append("1車與2車不可選擇同一台救護車")
        for index, vehicle_request in enumerate(vehicle_requests, start=1):
            label = f"{index}\u8eca"
            if not vehicle_request.return_time.strip():
                errors.append(f"\u8acb\u586b\u5beb{label}\u8fd4\u968a\u6642\u9593")
            if not vehicle_request.vehicle.strip():
                errors.append(f"\u8acb\u9078\u64c7{label}\u8eca\u865f")
            if not vehicle_request.driver.strip():
                errors.append(f"\u8acb\u9078\u64c7{label}\u53f8\u6a5f")
            if not vehicle_request.patient_summary.strip():
                errors.append(f"\u8acb\u9078\u64c7{label}\u50b7\u75c5\u60a3")
            if not vehicle_request.mileage.strip():
                errors.append(f"\u8acb\u586b\u5beb{label}\u91cc\u7a0b")
            elif not re.fullmatch(r"\d+", vehicle_request.mileage.strip()):
                errors.append(f"{label}\u91cc\u7a0b\u53ea\u80fd\u8f38\u5165\u6578\u5b57")
            if not vehicle_request.consumables:
                errors.append(f"\u8acb\u586b\u5beb{label}\u8017\u6750")
            errors.extend(validate_fuel_record(vehicle_request.fuel_record, label))

            case_time = normalize_hhmm(vehicle_request.case_time)
            return_time = normalize_hhmm(vehicle_request.return_time)
            if len(case_time) == 4 and len(return_time) == 4:
                case_datetime = vehicle_request.service_case_date().replace(
                    hour=int(case_time[:2]),
                    minute=int(case_time[2:]),
                    second=0,
                    microsecond=0,
                )
                return_datetime = vehicle_request.service_return_date().replace(
                    hour=int(return_time[:2]),
                    minute=int(return_time[2:]),
                    second=0,
                    microsecond=0,
                )
                if return_datetime < case_datetime:
                    errors.append(f"{label}\u8fd4\u968a\u6642\u9593\u4e0d\u80fd\u65e9\u65bc\u6848\u4ef6\u6642\u9593")
        return errors

    if not task_request.return_time.strip():
        errors.append("請填寫返隊時間")
    if not task_request.vehicle.strip():
        errors.append("請選擇出動車輛")
    if not task_request.driver.strip():
        errors.append("請選擇司機")
    if not task_request.patient_summary.strip():
        errors.append("請選擇傷病患")
    if not task_request.mileage.strip():
        errors.append("請填寫里程")
    elif not re.fullmatch(r"\d+", task_request.mileage.strip()):
        errors.append("里程只能輸入數字")
    if not task_request.consumables:
        errors.append("請選擇耗材")
    errors.extend(validate_fuel_record(task_request.fuel_record, "1\u8eca"))

    case_time = normalize_hhmm(task_request.case_time)
    return_time = normalize_hhmm(task_request.return_time)
    if len(case_time) == 4 and len(return_time) == 4:
        case_datetime = task_request.service_case_date().replace(
            hour=int(case_time[:2]),
            minute=int(case_time[2:]),
            second=0,
            microsecond=0,
        )
        return_datetime = task_request.service_return_date().replace(
            hour=int(return_time[:2]),
            minute=int(return_time[2:]),
            second=0,
            microsecond=0,
        )
        if return_datetime < case_datetime:
            errors.append("返隊日期時間不能早於案件日期時間")
    return errors


def existing_disaster_task_for_case(case_id: str) -> dict | None:
    normalized = str(case_id or "").strip()
    if not normalized:
        return None
    for payload in store.list_recent(limit=100000):
        task = dict(payload.get("task") or {})
        if str(task.get("service_type") or "ems") == "disaster" and str(task.get("case_id") or "").strip() == normalized:
            return payload
    return None


def validate_disaster_task_form(task_request) -> list[str]:
    errors: list[str] = []
    if not task_request.case_id.strip():
        errors.append("請先由勤務案件查詢選擇案件")
    if not task_request.case_date.strip():
        errors.append("請填寫案件日期")
    if not normalize_hhmm(task_request.case_time):
        errors.append("請填寫正確案件時間")
    if not task_request.return_time.strip() or not normalize_hhmm(task_request.return_time):
        errors.append("請填寫正確返隊時間")
    if not task_request.case_address.strip():
        errors.append("請填寫案件地址")
    if task_request.summary_type not in {"火災", "災害搶救", "救護"}:
        errors.append("請選擇正確案件類型")
    valid_reasons = DISASTER_REASON_OPTIONS_BY_TYPE.get(task_request.summary_type)
    if not task_request.case_reason or (
        task_request.summary_type != "救護"
        and valid_reasons is not None
        and task_request.case_reason not in valid_reasons
    ):
        errors.append("請選擇正確事由")
    if task_request.summary_type == "火災" and task_request.case_reason == "其他" and not task_request.reason_other.strip():
        errors.append("事由選擇其他時請填寫說明")
    if not task_request.commander.strip():
        errors.append("請選擇指揮官")
    elif task_request.commander not in task_request.personnel:
        errors.append("指揮官必須是本案服勤人員")
    if not task_request.action_note.strip():
        errors.append("請填寫其他處理情形")
    categories = {"轄內A2", "轄內A3", "轄內其他案件", "支援他轄"}
    if task_request.recorder_category not in categories:
        errors.append("請選擇行車紀錄器分類")
    if task_request.recorder_category == "轄內其他案件" and not task_request.recorder_subcategory.strip():
        errors.append("請選擇轄內其他案件子分類")
    entries = task_request.effective_vehicle_entries()
    if not entries:
        errors.append("至少需要一輛出動車輛")
    vehicles = [entry.vehicle.strip() for entry in entries if entry.vehicle.strip()]
    if len(vehicles) != len(set(vehicles)):
        errors.append("同一任務的出動車輛不得重複")
    for index, entry in enumerate(entries, start=1):
        label = f"第{index}車"
        if not entry.vehicle.strip():
            errors.append(f"{label}請選擇車輛")
        if not entry.driver.strip():
            errors.append(f"{label}請選擇司機")
        if not normalize_hhmm(entry.return_time):
            errors.append(f"{label}請填寫正確返隊時間")
        if not re.fullmatch(r"\d+", entry.mileage.strip()):
            errors.append(f"{label}里程只能輸入數字")
        errors.extend(validate_fuel_record(entry.fuel_record, label))
    return errors


def validate_fuel_record(fuel_record, label: str) -> list[str]:
    if not getattr(fuel_record, "enabled", False):
        return []
    errors: list[str] = []
    date = str(getattr(fuel_record, "date", "") or "").strip()
    time_value = str(getattr(fuel_record, "time", "") or "").strip()
    quantity = str(getattr(fuel_record, "quantity", "") or "").strip()
    unit_price = str(getattr(fuel_record, "unit_price", "") or "").strip()
    if not re.fullmatch(r"\d{8}", date):
        errors.append(f"{label}\u52a0\u6cb9\u65e5\u671f\u683c\u5f0f\u9700\u70ba YYYYMMDD")
    if not re.fullmatch(r"([01]\d|2[0-3])[0-5]\d", time_value):
        errors.append(f"{label}\u52a0\u6cb9\u6642\u9593\u683c\u5f0f\u9700\u70ba HHmm")
    if not re.fullmatch(r"\d+(?:\.\d+)?", quantity):
        errors.append(f"{label}\u6cb9\u91cf\u9700\u70ba\u6578\u5b57")
    if not re.fullmatch(r"\d+(?:\.\d+)?", unit_price):
        errors.append(f"{label}\u55ae\u50f9\u9700\u70ba\u6578\u5b57")
    return errors


def form_flag_enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def selected_consumable_packages_from_form(form) -> list[str]:
    selected: list[str] = []
    for raw_key in re.split(r"[\s,]+", str(form.get("consumable_packages") or "")):
        key = raw_key.strip()
        if key in CONSUMABLE_PACKAGE_KEYS and key not in selected:
            selected.append(key)
    return selected


def render_task_form_from_request(
    task_request,
    *,
    form_action: str,
    submit_label: str,
    cancel_url: str,
    recent_tasks: list[dict],
    case_lookup: dict,
    form_errors: list[str],
    baseline_consumables_loaded: bool = False,
    selected_consumable_packages: list[str] | None = None,
    two_vehicle_available: bool = False,
) -> str:
    selected_case = task_form_values(asdict(task_request))
    person_options = selected_case.get("person_options") or PERSON_OPTIONS
    return render_template(
        "new_task.html",
        form_action=form_action,
        submit_label=submit_label,
        cancel_url=cancel_url,
        recent_tasks=recent_tasks,
        case_lookup=case_lookup,
        selected_case=selected_case,
        vehicle_options=effective_ems_vehicle_options(),
        person_options=person_options,
        case_reason_options=CASE_REASON_OPTIONS,
        consumable_options=consumable_inventory_options(),
        default_consumables=dict(task_request.consumables or {}),
        baseline_consumables_loaded=baseline_consumables_loaded,
        selected_consumable_packages=selected_consumable_packages or [],
        two_vehicle_available=two_vehicle_available,
        last_vehicle_mileages=last_vehicle_mileages(),
        disinfection_item_options=DISINFECTION_ITEM_OPTIONS,
        default_disinfection_items=list(task_request.disinfection_items or []),
        form_errors=form_errors,
    )


def vehicle_admin_records() -> list[dict[str, str]]:
    custom_names = {
        record["label"]: record.get("ppe_name", "")
        for record in load_vehicle_records(artifacts_dir)
    }
    ppe_names = vehicle_ppe_names(artifacts_dir)
    return [
        {
            "label": label,
            "ppe_name": ppe_names.get(label, ""),
            "is_custom": label in custom_names,
        }
        for label in vehicle_options(artifacts_dir)
    ]


def pop_selected_case() -> dict:
    selected = read_selected_case()
    if selected:
        try:
            (artifacts_dir / "cases" / "selected.json").unlink()
        except OSError:
            pass
    return selected


def local_web_base_url() -> str:
    host = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.getenv("WEB_PORT", "8090").strip() or "8090"
    return f"http://{host}:{port}".rstrip("/")


desktop_runner.event_callback = report_public_pc_task_event


def run_web_app(host: str | None = None, port: int | None = None) -> None:
    host = host or os.getenv("WEB_HOST", "0.0.0.0")
    port = port or int(os.getenv("WEB_PORT", "8080"))
    try:
        credential_sync_record_for_worker()
    except CredentialEnvelopeError as exc:
        print(f"[app] credential relay migration deferred: {exc}", flush=True)
    if public_pc_reporting_enabled():
        start_public_pc_legacy_reconciliation()
        start_public_pc_pending_report_flusher()
    print(f"[app] starting SinpoSmart disaster EMS worker web app on {host}:{port}", flush=True)
    try:
        from waitress import serve
    except ImportError:
        print("[app] waitress unavailable, using Flask development server", flush=True)
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    else:
        print("[app] waitress serving", flush=True)
        serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    run_web_app()
