from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

from ambulance_bot.adapters import SITE_DEFINITIONS, SiteAutomationResult, default_adapters
from ambulance_bot.consumables import consumable_inventory_options
from ambulance_bot.desktop_fast_runner import DesktopFastRunner
from ambulance_bot.duty_credentials import (
    credential_sync_accounts_from_payload,
    load_synced_worker_credential,
    select_credential_sync_account,
)
from ambulance_bot.login_audit import site_login_account_summaries
from ambulance_bot.line_api import reply_text, verify_signature
from ambulance_bot.models import (
    CASE_REASON_OPTIONS,
    COMMAND_PREFIX,
    DEFAULT_DISINFECTION_ITEMS,
    DEFAULT_CONSUMABLES,
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
    save_vehicle_record,
    vehicle_options,
    vehicle_ppe_names,
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
from ambulance_bot.task_store import JsonTaskStore


load_dotenv()

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
store = JsonTaskStore(artifacts_dir / "tasks")
runner = TaskRunner(artifacts_dir, store=store)
desktop_runner = DesktopFastRunner(artifacts_dir, store=store)
_local_case_lookup_thread_lock = threading.Lock()
_local_case_lookup_thread: threading.Thread | None = None
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

@app.get("/")
def index():
    return redirect(url_for("new_task"))


@app.get("/app")
def new_task():
    selected_case = pop_selected_case()
    person_options = selected_case.get("person_options") or PERSON_OPTIONS
    return render_template(
        "new_task.html",
        form_action=url_for("create_task"),
        submit_label="建立任務",
        cancel_url="",
        recent_tasks=store.list_recent(limit=5),
        case_lookup=prepared_case_lookup(),
        selected_case=selected_case,
        vehicle_options=vehicle_options(artifacts_dir),
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


@app.post("/cases/query")
def query_cases():
    lookup_range = "24h"
    source = case_lookup_source_label(request.host)
    mode = effective_task_execution_mode()
    write_case_lookup_request(lookup_range, source=source, mode=mode)
    print(f"[case_lookup] query requested host={request.host} source={source} range={lookup_range} mode={mode}", flush=True)
    if mode == "desktop_fast":
        start_local_case_lookup(lookup_range)
    return redirect(url_for("new_task"))


@app.post("/cases/import")
def import_case():
    case_id = str(request.form.get("case_id") or "").strip()
    if not case_id:
        abort(400)
    if not write_selected_case_from_lookup(case_id):
        abort(404)
    return redirect(url_for("new_task", _anchor="task-form"))


@app.post("/cases/clear")
def clear_imported_case():
    pop_selected_case()
    return redirect(url_for("new_task"))


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
            recent_tasks=store.list_recent(limit=5),
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


@app.get("/tasks/<task_id>/edit")
def edit_task(task_id: str):
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    task = dict(payload.get("task") or {})
    selected_case = task_form_values(task)
    return render_template(
        "new_task.html",
        form_action=url_for("update_task", task_id=task_id),
        submit_label="儲存修改",
        cancel_url=url_for("task_detail", task_id=task_id),
        recent_tasks=[],
        case_lookup={"cases": [], "case_count": 0, "debug_artifacts": []},
        selected_case=selected_case,
        vehicle_options=vehicle_options(artifacts_dir),
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
    changed_site_keys = changed_sites_for_task_edit(dict(previous_payload.get("task") or {}), task_request.to_dict())
    site_update_contexts = site_update_contexts_for_task_edit(dict(previous_payload.get("task") or {}), task_request.to_dict(), changed_site_keys)
    payload = store.update_task(task_id, task_request, changed_site_keys=changed_site_keys, site_update_contexts=site_update_contexts)
    report_public_pc_task_event(payload, "修改任務")
    return redirect(url_for("task_detail", task_id=task_id))


@app.get("/tasks/<task_id>")
def task_detail(task_id: str):
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    return render_template("task_detail.html", payload=payload, site_can_run_individually=site_can_run_individually)


@app.post("/tasks/<task_id>/delete")
def delete_task(task_id: str):
    try:
        store.delete(task_id)
    except FileNotFoundError:
        abort(404)
    return redirect(url_for("new_task"))


@app.post("/tasks/<task_id>/run")
def run_task(task_id: str):
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    mode = effective_task_execution_mode()
    if mode == "desktop_fast":
        report_public_pc_task_event(payload, f"按下{task_site_count_label(payload.get('task') or {})}登打")
        desktop_runner.start_existing(task_id)
        return redirect(url_for("task_detail", task_id=task_id))
    if mode == "worker_queue":
        queue_task_for_worker(task_id)
        return redirect(url_for("task_detail", task_id=task_id))
    runner.start_existing(task_id)
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<task_id>/sites/<site_key>/run")
def run_task_site(task_id: str, site_key: str):
    if site_key not in VALID_SITE_KEYS:
        abort(404)
    try:
        store.get(task_id)
    except FileNotFoundError:
        abort(404)
    mode = effective_task_execution_mode()
    if mode == "desktop_fast":
        report_public_pc_task_event(store.get(task_id), f"按下單站登打：{site_display_name(site_key)}")
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
    try:
        store.mark_site_completed(task_id, site_key)
    except (FileNotFoundError, KeyError):
        abort(404)
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
    selected = select_credential_sync_account(accounts, data) or accounts[0]
    ack_id = str(data.get("sync_code") or data.get("event_id") or uuid4())
    write_credential_sync_relay(
        {
            "request_id": ack_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "pending",
            "source_host": request.host,
            "account_count": len(accounts),
            "selected_user_id": str(selected.get("user_id") or ""),
            "payload": data,
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
    return render_template(
        "admin_vehicles.html",
        vehicles=vehicle_admin_records(),
        errors=[],
        message="",
    )


@app.get("/admin/public-pc")
def admin_public_pc():
    reports = public_pc_reports()
    return render_template("admin_public_pc.html", reports=reports, version_info=worker_admin_version_info(reports))


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
    return jsonify({"ok": True, "tasks": store.list_recent(limit)})


@app.get("/worker/tasks/<task_id>")
def worker_task(task_id: str):
    if not worker_authorized():
        abort(403)
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    return jsonify({"ok": True, "payload": payload, "task": payload["task"]})


@app.post("/worker/tasks/<task_id>/status")
def worker_task_status(task_id: str):
    if not worker_authorized():
        abort(403)
    data = request.get_json(silent=True) or {}
    status_text = str(data.get("status") or "").strip()
    detail = str(data.get("detail") or "").strip()
    overall_status = str(data.get("overall_status") or "").strip()
    overall_detail = str(data.get("overall_detail") or detail).strip()
    if not status_text:
        abort(400)
    site_key = str(data.get("site_key") or "").strip()
    site_name = str(data.get("site_name") or "公務電腦 worker").strip()
    try:
        if site_key:
            diagnostic_fields = {field: str(data.get(field) or "").strip() for field in DIAGNOSTIC_FIELDS}
            payload = store.update_site_result(
                task_id,
                SiteAutomationResult(site_key, site_name, status_text, detail, **diagnostic_fields),
            )
            if overall_status:
                payload = store.set_overall_status(task_id, overall_status, overall_detail)
        else:
            payload = store.set_overall_status(task_id, overall_status or status_text, overall_detail if overall_status else detail)
    except (FileNotFoundError, KeyError):
        abort(404)
    return jsonify({"ok": True, "payload": payload})


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
    record = read_credential_sync_relay()
    if not record or record.get("status") != "pending":
        return jsonify({"ok": True, "request": None})
    return jsonify(
        {
            "ok": True,
            "request": {
                "request_id": str(record.get("request_id") or ""),
                "created_at": str(record.get("created_at") or ""),
                "account_count": int(record.get("account_count") or 0),
                "selected_user_id": str(record.get("selected_user_id") or ""),
                "payload": record.get("payload") if isinstance(record.get("payload"), dict) else {},
            },
        }
    )


@app.post("/worker/credential-sync/<request_id>/ack")
def worker_credential_sync_ack(request_id: str):
    if not worker_authorized():
        abort(403)
    record = read_credential_sync_relay()
    if not record or str(record.get("request_id") or "") != request_id:
        abort(404)
    clear_credential_sync_relay()
    return jsonify({"ok": True, "ack_id": request_id})


@app.post("/worker/cases")
def worker_cases():
    if not worker_authorized():
        abort(403)
    data = request.get_json(silent=True) or {}
    cases = data.get("cases")
    if not isinstance(cases, list):
        abort(400)
    output_dir = artifacts_dir / "cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": str(data.get("status") or "cases_loaded"),
        "detail": str(data.get("detail") or f"公務電腦 worker 已回傳 {len(cases)} 筆案件。"),
        "lookup_range": str(data.get("lookup_range") or ""),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": str(data.get("source") or "public_duty_pc_worker"),
        "case_hash": str(data.get("case_hash") or ""),
        "cases": cases,
    }
    write_json_atomic(output_dir / "latest.json", payload)
    mark_case_lookup_request_completed(payload)
    return jsonify({"ok": True, "case_count": len(cases), "payload": payload})


@app.post("/worker/public-pc-task-events")
def worker_public_pc_task_events():
    if not worker_authorized():
        abort(403)
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
    if not target.exists() or not target.is_file():
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
    for report in reports or []:
        version = str(report.get("package_version") or "").strip()
        if version:
            return {"label": "SinpoSmart - 救護Worker", "version": version, "detail": "公務電腦已安裝"}
    return {"label": "SinpoSmart - 救護Worker", "version": package_version() or "未標示", "detail": "目前後台"}


def credential_sync_relay_file() -> Path:
    return artifacts_dir / "credential_sync" / "pending.json"


def sinposmart_store() -> SinpoSmartBackendStore:
    return SinpoSmartBackendStore(artifacts_dir / "sinposmart")


def credential_sync_token() -> str:
    return os.getenv("CREDENTIAL_SYNC_TOKEN", "").strip() or os.getenv("WORKER_TOKEN", "").strip()


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
        return max(60, int(os.getenv("CREDENTIAL_SYNC_TTL_SECONDS", "3600")))
    except ValueError:
        return 3600


def read_credential_sync_relay() -> dict:
    path = credential_sync_relay_file()
    if not path.exists():
        return {}
    try:
        if time.time() - path.stat().st_mtime > credential_sync_ttl_seconds():
            path.unlink()
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_credential_sync_relay(payload: dict) -> None:
    write_json_atomic(credential_sync_relay_file(), payload)


def clear_credential_sync_relay() -> None:
    try:
        credential_sync_relay_file().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def public_pc_report_file() -> Path:
    return artifacts_dir / "public_pc" / "task_events.json"


def public_pc_pending_report_file() -> Path:
    return artifacts_dir / "public_pc" / "pending_events.jsonl"


def public_pc_reports() -> list[dict]:
    path = public_pc_report_file()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    reports = payload.get("tasks") if isinstance(payload, dict) else []
    if not isinstance(reports, list):
        return []
    return sorted(reports, key=lambda item: str(item.get("updated_at") or ""), reverse=True)


def upsert_public_pc_report(data: dict) -> dict:
    path = public_pc_report_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = {"tasks": public_pc_reports()}
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    task_id = str(data.get("task_id") or task.get("task_id") or "").strip()
    now = datetime.now().isoformat(timespec="seconds")
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
    reports = [item for item in current["tasks"] if str(item.get("task_id") or "") != task_id]
    existing = next((item for item in current["tasks"] if str(item.get("task_id") or "") == task_id), {})
    events = list(existing.get("events") or [])
    known_event_ids = {str(item.get("event_id") or "").strip() for item in events if isinstance(item, dict)}
    if event_id not in known_event_ids:
        events.append(event)
    site_login_accounts = (
        data.get("site_login_accounts")
        if isinstance(data.get("site_login_accounts"), dict)
        else existing.get("site_login_accounts", {})
    )
    payload = {
        **existing,
        "task_id": task_id,
        "title": str(data.get("title") or task_title(task) or task_id),
        "task": task,
        "operator": operator_label,
        "user": operator_label,
        "synced_account": synced_account,
        "site_login_accounts": site_login_accounts,
        "worker_id": event["worker_id"],
        "package_version": event["package_version"] or str(existing.get("package_version") or ""),
        "overall_status": str(data.get("overall_status") or existing.get("overall_status") or ""),
        "site_statuses": data.get("site_statuses") if isinstance(data.get("site_statuses"), dict) else existing.get("site_statuses", {}),
        "created_at": str(data.get("created_at") or existing.get("created_at") or now),
        "updated_at": now,
        "last_action": event["action"],
        "last_status": event["status"],
        "last_detail": event["detail"],
        "events": events,
    }
    reports.insert(0, payload)
    write_json_atomic(path, {"tasks": reports[:100]})
    return payload


def _enqueue_public_pc_report(payload: dict) -> None:
    path = public_pc_pending_report_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _load_pending_public_pc_reports() -> list[dict]:
    path = public_pc_pending_report_file()
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                entries.append(payload)
    except OSError:
        return []
    return entries


def _write_pending_public_pc_reports(entries: list[dict]) -> None:
    path = public_pc_pending_report_file()
    if not entries:
        try:
            path.unlink()
        except OSError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n"
    path.write_text(body, encoding="utf-8")


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


def report_public_pc_task_event(payload: dict, action: str) -> None:
    if not public_pc_reporting_enabled():
        return
    task = dict(payload.get("task") or {})
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    latest_event = events[-1] if events else {}
    operator_label = current_public_pc_user_label()
    site_login_accounts = public_pc_site_login_accounts(task)
    body = {
        "event_id": str(uuid4()),
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
        "site_statuses": payload.get("site_statuses") or {},
        "created_at": str(payload.get("created_at") or ""),
    }
    server_url = public_pc_report_server_url()
    if not server_url:
        return
    pending = _load_pending_public_pc_reports()
    pending.append(body)
    sent_count = 0
    try:
        for index, entry in enumerate(pending, start=1):
            ack_payload = _post_public_pc_report(server_url, entry)
            ack_id = str((ack_payload or {}).get("ack_id") or entry.get("event_id") or "").strip()
            if ack_id != str(entry.get("event_id") or "").strip():
                break
            sent_count = index
    except (OSError, urllib.error.URLError) as exc:
        _write_pending_public_pc_reports(pending[sent_count:])
        print(f"[public_pc_report] pending task_id={task_id} action={action} server={server_url} error={exc}", flush=True)
    else:
        _write_pending_public_pc_reports(pending[sent_count:])


def public_pc_reporting_enabled() -> bool:
    value = os.getenv("PUBLIC_PC_REPORT_ENABLED", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


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
    if current.get("status") != "case_lookup_requested":
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
    if current.get("status") != "case_lookup_requested":
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


def _run_and_clear_local_case_lookup(lookup_range: str) -> None:
    global _local_case_lookup_thread
    try:
        run_local_case_lookup(lookup_range)
    finally:
        with _local_case_lookup_thread_lock:
            if _local_case_lookup_thread is threading.current_thread():
                _local_case_lookup_thread = None


def start_local_case_lookup(lookup_range: str) -> threading.Thread:
    global _local_case_lookup_thread
    with _local_case_lookup_thread_lock:
        if _local_case_lookup_thread is not None and _local_case_lookup_thread.is_alive():
            return _local_case_lookup_thread
        thread = threading.Thread(target=_run_and_clear_local_case_lookup, args=(lookup_range,), daemon=True)
        _local_case_lookup_thread = thread
    thread.start()
    return thread


def run_local_case_lookup(lookup_range: str) -> None:
    from ambulance_bot.selenium_local import query_duty_emergency_cases

    try:
        result = query_duty_emergency_cases(artifacts_dir, lookup_range=lookup_range)
    except Exception as exc:
        payload = {
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
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def changed_sites_for_task_edit(previous_task: dict, current_task: dict) -> set[str]:
    changed_sites: set[str] = set()
    if task_fields_changed(previous_task, current_task, ("two_vehicle",)):
        changed_sites.update({"duty_work_log", "vehicle_mileage", "consumables", "disinfection"})
    if task_fields_changed(previous_task, current_task, ("vehicle", "driver")):
        changed_sites.update({"duty_work_log", "vehicle_mileage"})
    if task_fields_changed(previous_task, current_task, ("mileage", "return_date", "return_time")):
        changed_sites.add("vehicle_mileage")
    if task_fields_changed(previous_task, current_task, ("fuel_record",)):
        changed_sites.add("fuel_record")
    if task_fields_changed(previous_task, current_task, ("case_reason", "patient_summary", "work_note")):
        changed_sites.add("duty_work_log")
    if task_fields_changed(previous_task, current_task, ("consumables",)):
        changed_sites.add("consumables")
    if task_fields_changed(previous_task, current_task, ("disinfection", "disinfection_items")):
        changed_sites.add("disinfection")
    if task_vehicle_entries_changed(previous_task, current_task, ("vehicle", "driver", "patient_summary")):
        changed_sites.add("duty_work_log")
    if task_vehicle_entries_changed(previous_task, current_task, ("vehicle", "driver", "mileage", "return_date", "return_time")):
        changed_sites.add("vehicle_mileage")
    if task_vehicle_entries_changed(previous_task, current_task, ("vehicle", "driver", "fuel_record")):
        changed_sites.add("fuel_record")
    if task_vehicle_entries_changed(previous_task, current_task, ("consumables",)):
        changed_sites.add("consumables")
    if task_vehicle_entries_changed(previous_task, current_task, ("vehicle", "disinfection", "disinfection_items")):
        changed_sites.add("disinfection")
    return changed_sites


def site_update_contexts_for_task_edit(previous_task: dict, current_task: dict, changed_site_keys: set[str]) -> dict[str, dict[str, object]]:
    return {
        site_key: {
            "previous_task": previous_task,
            "current_task": current_task,
        }
        for site_key in changed_site_keys
    }


def task_fields_changed(previous_task: dict, current_task: dict, field_names: tuple[str, ...]) -> bool:
    return any(normalized_task_edit_value(previous_task.get(name)) != normalized_task_edit_value(current_task.get(name)) for name in field_names)


def task_vehicle_entries_changed(previous_task: dict, current_task: dict, field_names: tuple[str, ...]) -> bool:
    return normalized_vehicle_entry_values(previous_task, field_names) != normalized_vehicle_entry_values(current_task, field_names)


def normalized_vehicle_entry_values(task: dict, field_names: tuple[str, ...]) -> tuple[tuple[object, ...], ...]:
    raw_entries = task.get("vehicle_entries")
    entries = raw_entries if isinstance(raw_entries, list) and raw_entries else [
        {
            "vehicle": task.get("vehicle"),
            "driver": task.get("driver"),
            "mileage": task.get("mileage"),
            "return_date": task.get("return_date"),
            "return_time": task.get("return_time"),
            "patient_summary": task.get("patient_summary"),
            "consumables": task.get("consumables"),
            "disinfection": task.get("disinfection"),
            "disinfection_items": task.get("disinfection_items"),
        }
    ]
    normalized: list[tuple[object, ...]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        normalized.append(tuple(normalized_task_edit_value(entry.get(name)) for name in field_names))
    return tuple(normalized)


def normalized_task_edit_value(value: object) -> object:
    if isinstance(value, dict):
        normalized_items: list[tuple[str, int]] = []
        for key, item_value in value.items():
            name = str(key).strip()
            try:
                qty = int(item_value)
            except (TypeError, ValueError):
                qty = 0
            if name and qty > 0:
                normalized_items.append((name, qty))
        return tuple(sorted(normalized_items))
    if isinstance(value, list):
        return tuple(sorted(str(item).strip() for item in value if str(item).strip()))
    return str(value or "").strip()


def status_label(status: str) -> str:
    value = str(status or "")
    if value == "desktop_fast_running":
        return "本機快速執行"
    if value == "desktop_fast_completed_with_errors":
        return "部分失敗"
    if value == "desktop_fast_completed":
        return "完成"
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
    if value == "desktop_fast_completed":
        return "complete"
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
    if not task_has_fuel_record(task):
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
    site_statuses = dict(payload.get("site_statuses") or {})
    sites = [dict(site_statuses.get(site_key) or {}) for site_key in active_site_keys_for_task(payload.get("task") or {})]
    if any(status_class(str(site.get("status") or "")) == "running" for site in sites):
        return "desktop_fast_running"
    if any(status_class(str(site.get("status") or "")) == "failed" for site in sites):
        return "failed"
    if any(str(site.get("status") or "").endswith("_needs_update") for site in sites):
        return "site_needs_update"
    if any(status_class(str(site.get("status") or "")) == "waiting" for site in sites):
        return "manual_captcha_required"
    if sites and all(status_class(str(site.get("status") or "")) == "complete" for site in sites):
        return "completed_by_user"
    return str(payload.get("overall_status") or "")


def task_payload_is_active(payload: dict) -> bool:
    return status_class(effective_task_status(payload)) == "running"


def recent_tasks_need_refresh(recent_tasks: list[dict]) -> bool:
    return any(task_payload_is_active(dict(item or {})) for item in recent_tasks)


def task_progress_summary(payload: dict) -> str:
    site_statuses = dict(payload.get("site_statuses") or {})
    completed_count = 0
    site_keys = active_site_keys_for_task(payload.get("task") or {})
    total_count = len(site_keys)
    failed_sites: list[str] = []
    updated_sites: list[str] = []
    waiting_site = ""
    running_site = ""

    for site_key in site_keys:
        site = dict(site_statuses.get(site_key) or {})
        site_status = str(site.get("status") or "")
        site_class = status_class(site_status)
        site_name = SITE_SHORT_NAMES.get(site_key, site_key)
        if site_class == "complete":
            completed_count += 1
            continue
        if site_class == "running":
            running_site = running_site or site_name
            continue
        if site_class == "failed":
            failed_sites.append(site_name)
            continue
        if site_status.endswith("_needs_update"):
            updated_sites.append(site_name)
            continue
        if site_class == "waiting":
            waiting_site = waiting_site or site_name

    if running_site:
        return f"已完成 {completed_count}/{total_count}；目前：{running_site}執行中"
    if updated_sites:
        return f"已完成 {completed_count}/{total_count}；需更新：{'、'.join(updated_sites)}"
    if waiting_site:
        return f"已完成 {completed_count}/{total_count}；待確認：{waiting_site}"

    if completed_count == total_count:
        return f"{total_count}\u7ad9\u5b8c\u6210"
    if len(failed_sites) == 1:
        return f"已完成 {completed_count}/{total_count}；失敗：{failed_sites[0]}"
    if failed_sites:
        return f"已完成 {completed_count}/{total_count}；{len(failed_sites)} 站失敗"

    overall_status = str(payload.get("overall_status") or "")
    if overall_status == "queued_for_worker":
        return f"已完成 {completed_count}/{total_count}；等待公務電腦 worker"
    if overall_status == "claimed_by_worker":
        return f"已完成 {completed_count}/{total_count}；worker 已接手"
    if status_class(effective_task_status(payload)) == "running":
        return f"已完成 {completed_count}/{total_count}；{total_count}\u7ad9\u767b\u6253\u4e2d"
    if status_class(effective_task_status(payload)) == "failed":
        return f"已完成 {completed_count}/{total_count}；流程有錯誤"
    return f"已完成 {completed_count}/{total_count}；尚未開始"


def task_title(task: dict) -> str:
    reason = str(task.get("case_reason") or "救護").strip()
    address = display_case_address(task).strip()
    if address:
        return f"緊急救護-{reason} - {address}"
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
                "label": f"{index}\u8eca" if multiple else "\u8eca\u8f1b",
                "vehicle": str(entry.get("vehicle") or "").strip(),
                "driver": str(entry.get("driver") or "").strip(),
                "mileage": str(entry.get("mileage") or "").strip(),
                "return_date": str(entry.get("return_date") or "").strip(),
                "return_time": str(entry.get("return_time") or "").strip(),
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
    task = dict(task or {})
    raw_entries = task.get("vehicle_entries")
    entries = raw_entries if isinstance(raw_entries, list) and raw_entries else [task]
    for raw_entry in entries:
        entry = dict(raw_entry or {}) if isinstance(raw_entry, dict) else {}
        fuel_record = entry.get("fuel_record") if isinstance(entry.get("fuel_record"), dict) else {}
        if fuel_record and form_flag_enabled(fuel_record.get("enabled")):
            return True
    return False


def active_site_keys_for_task(task: dict) -> list[str]:
    if task_has_fuel_record(task):
        return list(SITE_RUN_ORDER)
    return [site_key for site_key in SITE_RUN_ORDER if site_key != "fuel_record"]


def task_site_count_label(task: dict) -> str:
    return "五站" if len(active_site_keys_for_task(task)) == 5 else "四站"


def task_site_display_pairs(task: dict) -> list[tuple[str, str]]:
    return [(site_key, SITE_DISPLAY_NAMES.get(site_key, site_key)) for site_key in active_site_keys_for_task(task)]


def last_vehicle_mileages(limit: int = 50) -> dict[str, str]:
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


def visible_events(events: list[dict]) -> list[dict]:
    important: list[dict] = []
    seen_sites: set[str] = set()
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
        if site_key in seen_sites:
            continue
        seen_sites.add(site_key)
        important.append(event)
        if len(important) >= len(SITE_RUN_ORDER):
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
        "recent_tasks_need_refresh": recent_tasks_need_refresh,
        "site_action_button_label": site_action_button_label,
        "site_diagnostic": site_diagnostic,
        "site_short_name": site_short_name,
        "site_error_guidance": site_error_guidance,
        "site_stage_rows": site_stage_rows,
        "show_public_pc_admin_button": show_public_pc_admin_button,
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
        "task_payload_is_active": task_payload_is_active,
        "task_progress_summary": task_progress_summary,
        "task_has_fuel_record": task_has_fuel_record,
        "task_site_count_label": task_site_count_label,
        "task_site_display_pairs": task_site_display_pairs,
        "task_title": task_title,
        "task_vehicle_display_entries": task_vehicle_display_entries,
        "visible_events": visible_events,
    }


def show_public_pc_admin_button() -> bool:
    return not request_is_local_host()


def show_task_entry_controls() -> bool:
    return request_is_local_host()


def should_auto_queue_task_on_create() -> bool:
    return effective_task_execution_mode() == "worker_queue" and not request_is_local_host()


def queue_task_for_worker(task_id: str) -> None:
    request_payload = store.request_for(task_id)
    for adapter in default_adapters():
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
    return "\u6551\u8b77" in text


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
            mark_case_lookup_request_failed({"detail": case_lookup["detail"], "cases": []})
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
        case_lookup["empty_message"] = f"查詢完成，{range_label}沒有找到案件。可以稍後再查，或直接手動輸入案件資料。"
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
        vehicle_options=vehicle_options(artifacts_dir),
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
    print(f"[app] starting SinpoSmart ambulance worker web app on {host}:{port}", flush=True)
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
