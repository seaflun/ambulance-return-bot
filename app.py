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
from ambulance_bot.duty_credentials import load_synced_worker_credential
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
from ambulance_bot.task_runner import TaskRunner
from ambulance_bot.task_store import JsonTaskStore


load_dotenv()

app = Flask(__name__)
artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
store = JsonTaskStore(artifacts_dir / "tasks")
runner = TaskRunner(artifacts_dir, store=store)
desktop_runner = DesktopFastRunner(artifacts_dir, store=store)
VALID_SITE_KEYS = {site.key for site in SITE_DEFINITIONS}
SITE_RUN_ORDER = ["duty_work_log", "vehicle_mileage", "disinfection", "consumables"]
SITE_SHORT_NAMES = {
    "duty_work_log": "工作",
    "vehicle_mileage": "里程",
    "disinfection": "消毒",
    "consumables": "耗材",
}
SITE_STAGE_DEFINITIONS = {
    "duty_work_log": ["啟動 Chrome", "登入勤務系統", "新增工作紀錄", "由案件帶入", "填寫勤務資料", "儲存"],
    "vehicle_mileage": ["啟動 Chrome", "登入 PPE", "開啟車輛里程", "填寫返隊時間與里程", "儲存"],
    "disinfection": ["啟動 Chrome", "登入消毒系統", "查詢案件", "開啟消毒紀錄", "填寫消毒項目", "儲存"],
    "consumables": ["啟動 Chrome", "登入一站通", "開啟耗材紀錄", "填寫耗材品項", "儲存"],
}


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
        disinfection_item_options=DISINFECTION_ITEM_OPTIONS,
        default_disinfection_items=DEFAULT_DISINFECTION_ITEMS if selected_case else [],
        form_errors=[],
    )


@app.post("/cases/query")
def query_cases():
    lookup_range = "24h"
    source = case_lookup_source_label(request.host)
    write_case_lookup_request(lookup_range, source=source)
    mode = effective_task_execution_mode()
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
    return render_template(
        "new_task.html",
        form_action=url_for("update_task", task_id=task_id),
        submit_label="儲存修改",
        cancel_url=url_for("task_detail", task_id=task_id),
        recent_tasks=[],
        case_lookup={"cases": [], "case_count": 0, "debug_artifacts": []},
        selected_case=task_form_values(task),
        vehicle_options=vehicle_options(artifacts_dir),
        person_options=PERSON_OPTIONS,
        case_reason_options=CASE_REASON_OPTIONS,
        consumable_options=consumable_inventory_options(),
        default_consumables=dict(task.get("consumables") or {}),
        disinfection_item_options=DISINFECTION_ITEM_OPTIONS,
        default_disinfection_items=list(task.get("disinfection_items") or []),
        form_errors=[],
    )


@app.post("/tasks/<task_id>/edit")
def update_task(task_id: str):
    try:
        store.get(task_id)
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
        ), 400
    payload = store.update_task(task_id, task_request)
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
        store.get(task_id)
    except FileNotFoundError:
        abort(404)
    mode = effective_task_execution_mode()
    if mode == "desktop_fast":
        report_public_pc_task_event(store.get(task_id), "按下四站登打")
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
            "單站登打只能在本機網頁使用；手機/NAS 請使用四站登打或公務電腦 worker。",
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
        }
    )


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
    return render_template("admin_public_pc.html", reports=reports)


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
            payload = store.update_site_result(task_id, SiteAutomationResult(site_key, site_name, status_text, detail))
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
    event = {
        "event_id": event_id,
        "ack_id": ack_id,
        "time": str(data.get("time") or now),
        "operator": operator_label,
        "user": operator_label,
        "worker_id": str(data.get("worker_id") or ""),
        "action": str(data.get("action") or "更新"),
        "status": str(data.get("status") or ""),
        "detail": str(data.get("detail") or ""),
    }
    reports = [item for item in current["tasks"] if str(item.get("task_id") or "") != task_id]
    existing = next((item for item in current["tasks"] if str(item.get("task_id") or "") == task_id), {})
    events = list(existing.get("events") or [])
    known_event_ids = {str(item.get("event_id") or "").strip() for item in events if isinstance(item, dict)}
    if event_id not in known_event_ids:
        events.append(event)
    payload = {
        **existing,
        "task_id": task_id,
        "title": str(data.get("title") or task_title(task) or task_id),
        "task": task,
        "operator": operator_label,
        "user": operator_label,
        "worker_id": event["worker_id"],
        "overall_status": str(data.get("overall_status") or existing.get("overall_status") or ""),
        "site_statuses": data.get("site_statuses") if isinstance(data.get("site_statuses"), dict) else existing.get("site_statuses", {}),
        "created_at": str(data.get("created_at") or existing.get("created_at") or now),
        "updated_at": now,
        "last_action": event["action"],
        "last_status": event["status"],
        "last_detail": event["detail"],
        "events": events[-40:],
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
    body = {
        "event_id": str(uuid4()),
        "task_id": task_id,
        "task": task,
        "title": task_title(task),
        "operator": operator_label,
        "user": operator_label,
        "worker_id": os.getenv("WORKER_ID", socket.gethostname() or "public-duty-pc"),
        "action": action,
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


def write_case_lookup_request(lookup_range: str, source: str = "") -> dict:
    output_dir = artifacts_dir / "cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    range_label = case_lookup_range_label(lookup_range)
    payload = {
        "status": "case_lookup_requested",
        "lookup_range": lookup_range,
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


def start_local_case_lookup(lookup_range: str) -> threading.Thread:
    thread = threading.Thread(target=run_local_case_lookup, args=(lookup_range,), daemon=True)
    thread.start()
    return thread


def run_local_case_lookup(lookup_range: str) -> None:
    from ambulance_bot.selenium_local import query_duty_emergency_cases

    result = query_duty_emergency_cases(artifacts_dir, lookup_range=lookup_range)
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
    mark_case_lookup_request_completed(payload)


def hash_cases(cases: list[dict[str, object]]) -> str:
    normalized = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def status_label(status: str) -> str:
    value = str(status or "")
    if value == "desktop_fast_running":
        return "本機快速執行"
    if value == "desktop_fast_completed_with_errors":
        return "部分失敗"
    if value == "desktop_fast_completed":
        return "完成"
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
        current_class = status_class(str(site.get("status") or ""))
        if current_class == "failed":
            blocked = True
        if ordered_key != site_key:
            continue
        return current_class == "failed" or (blocked and current_class != "complete")
    return False


def site_stage_rows(site_statuses: dict, site_key: str) -> list[dict[str, str]]:
    site = dict(site_statuses.get(site_key) or {})
    current_class = status_class(str(site.get("status") or ""))
    blocked_by_previous = _site_blocked_by_previous(site_statuses, site_key)
    rows: list[dict[str, str]] = []
    for index, stage in enumerate(SITE_STAGE_DEFINITIONS.get(site_key, [])):
        if current_class == "complete":
            state = "已完成"
            state_class = "complete"
        elif current_class == "running":
            state = "執行中" if index == 0 else "待執行"
            state_class = "running" if index == 0 else "idle"
        elif current_class == "failed":
            state = "需檢查"
            state_class = "failed"
        elif blocked_by_previous:
            state = "被前站卡住"
            state_class = "waiting"
        else:
            state = "未執行"
            state_class = "idle"
        rows.append({"name": stage, "state": state, "class": state_class})
    return rows


def _site_blocked_by_previous(site_statuses: dict, site_key: str) -> bool:
    for ordered_key in SITE_RUN_ORDER:
        if ordered_key == site_key:
            return False
        site = dict(site_statuses.get(ordered_key) or {})
        if status_class(str(site.get("status") or "")) == "failed":
            return True
    return False


def effective_task_status(payload: dict) -> str:
    sites = list(dict(payload.get("site_statuses") or {}).values())
    if any(status_class(str(site.get("status") or "")) == "failed" for site in sites):
        return "failed"
    if any(status_class(str(site.get("status") or "")) == "running" for site in sites):
        return "desktop_fast_running"
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
    total_count = len(SITE_RUN_ORDER)

    for site_key in SITE_RUN_ORDER:
        site = dict(site_statuses.get(site_key) or {})
        site_status = str(site.get("status") or "")
        site_class = status_class(site_status)
        site_name = SITE_SHORT_NAMES.get(site_key, site_key)
        if site_class == "complete":
            completed_count += 1
            continue
        if site_class == "running":
            return f"已完成 {completed_count}/{total_count}；目前：{site_name}執行中"
        if site_class == "failed":
            return f"已完成 {completed_count}/{total_count}；卡在：{site_name}失敗"
        if site_class == "waiting":
            return f"已完成 {completed_count}/{total_count}；待確認：{site_name}"

    if completed_count == total_count:
        return "四站完成"

    overall_status = str(payload.get("overall_status") or "")
    if overall_status == "queued_for_worker":
        return f"已完成 {completed_count}/{total_count}；等待公務電腦 worker"
    if overall_status == "claimed_by_worker":
        return f"已完成 {completed_count}/{total_count}；worker 已接手"
    if status_class(effective_task_status(payload)) == "running":
        return f"已完成 {completed_count}/{total_count}；四站登打中"
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
    if not selected_return_time_input(case):
        return ""
    return date_input_value(case.get("return_time") or case.get("case_date") or case.get("report_time") or "")


def date_input_value(value: object) -> str:
    parsed = parse_datetime_text(value) or parse_case_date(str(value or ""))
    return parsed.strftime("%Y-%m-%d") if parsed else ""


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
        if len(important) >= 4:
            break
    return list(reversed(important))


def event_site_key(event: dict) -> str:
    status = str(event.get("status") or "")
    detail = str(event.get("detail") or "")
    if status.startswith("vehicle_mileage") or "車輛里程" in detail:
        return "vehicle_mileage"
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
    blocking_site_key = ""
    for site_key in SITE_RUN_ORDER:
        site = dict(site_statuses.get(site_key) or {})
        status = str(site.get("status") or "")
        detail = str(site.get("detail") or "").strip()
        current_class = status_class(status)
        site_name = SITE_SHORT_NAMES.get(site_key, site_key)

        if current_class == "failed":
            entries.append(
                {
                    "site_key": site_key,
                    "site_name": site_name,
                    "state": "失敗",
                    "detail": detail[:120] or "程式回報此站未完成。",
                    "action": site_next_action(site_key, status, detail),
                }
            )
            if not blocking_site_key:
                blocking_site_key = site_key
            continue

        if current_class == "waiting":
            entries.append(
                {
                    "site_key": site_key,
                    "site_name": site_name,
                    "state": "待確認",
                    "detail": detail[:120] or "此站需要人工確認後才能視為完成。",
                    "action": site_next_action(site_key, status, detail),
                }
            )
            continue

        if blocking_site_key and current_class != "complete":
            if request_is_local_host():
                blocked_action = f"先處理前一站；若前一站已人工完成，再按「單獨登打」補做{site_name}。"
            else:
                blocked_action = f"先在公務電腦處理前一站；完成後由 worker 或本機頁面補做{site_name}。"
            entries.append(
                {
                    "site_key": site_key,
                    "site_name": site_name,
                    "state": "未接續",
                    "detail": f"前一站「{SITE_SHORT_NAMES.get(blocking_site_key, blocking_site_key)}」未完成，四站流程已停止。",
                    "action": blocked_action,
                }
            )
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
        "display_case_title": display_case_title,
        "effective_task_status": effective_task_status,
        "event_detail_text": event_detail_text,
        "event_site_name": event_site_name,
        "selected_case_date_input": selected_case_date_input,
        "selected_case_address": selected_case_address,
        "selected_return_date_input": selected_return_date_input,
        "selected_return_time_input": selected_return_time_input,
        "recent_tasks_need_refresh": recent_tasks_need_refresh,
        "site_short_name": site_short_name,
        "site_error_guidance": site_error_guidance,
        "site_stage_rows": site_stage_rows,
        "show_public_pc_admin_button": show_public_pc_admin_button,
        "show_task_entry_controls": show_task_entry_controls,
        "status_class": status_class,
        "status_label": status_label,
        "task_datetime_display": task_datetime_display,
        "task_payload_is_active": task_payload_is_active,
        "task_progress_summary": task_progress_summary,
        "task_title": task_title,
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


def read_selected_case() -> dict:
    path = artifacts_dir / "cases" / "selected.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    selected = dict(payload.get("selected_case") or {})
    people = [str(name).strip() for name in selected.get("personnel") or [] if str(name).strip()]
    if people:
        selected["person_options"] = [(name, name) for name in people]
    selected["status"] = payload.get("status", "")
    selected["detail"] = payload.get("detail", "")
    return selected


def task_form_values(task: dict) -> dict:
    values = dict(task)
    values["address"] = clean_case_address(str(task.get("case_address") or ""))
    values["case_time_hhmm"] = str(task.get("case_time") or "")
    values["return_time_hhmm"] = str(task.get("return_time") or "")
    values["reason"] = str(task.get("case_reason") or "")
    return values


def prepared_case_lookup() -> dict:
    case_lookup = read_case_lookup()
    lookup_request = read_case_lookup_request()
    cases = case_lookup.get("cases") or []
    detail = str(case_lookup.get("detail") or "").strip()
    if detail:
        detail = detail.replace("緊急救護案件", "救護、火災案件")
        detail = detail.replace("前 24 小時的救護、火災案件，並預先讀取服勤人員", "24小時內案件，並讀取出勤人員")
        detail = detail.replace("前 24 小時的緊急救護案件，並預先讀取服勤人員", "24小時內案件，並讀取出勤人員")
        case_lookup["detail"] = detail
    if lookup_request.get("status") == "case_lookup_requested":
        lookup_range = str(lookup_request.get("lookup_range") or case_lookup.get("lookup_range") or "24h")
        range_label = case_lookup_range_label(lookup_range)
        case_lookup["detail"] = f"正在查詢{range_label}案件，請稍候。"
        case_lookup["is_running"] = True
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


def render_task_form_from_request(
    task_request,
    *,
    form_action: str,
    submit_label: str,
    cancel_url: str,
    recent_tasks: list[dict],
    case_lookup: dict,
    form_errors: list[str],
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
    print(f"[app] starting ambulance return web app on {host}:{port}", flush=True)
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
