from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

from ambulance_bot.adapters import SiteAutomationResult, default_adapters
from ambulance_bot.consumables import consumable_inventory_options
from ambulance_bot.line_api import reply_text, verify_signature
from ambulance_bot.models import (
    CASE_REASON_OPTIONS,
    COMMAND_PREFIX,
    DEFAULT_DISINFECTION_ITEMS,
    DEFAULT_CONSUMABLES,
    DISINFECTION_ITEM_OPTIONS,
    PERSON_OPTIONS,
    VEHICLE_OPTIONS,
    example_command,
    clean_case_address,
    normalize_hhmm,
    parse_case_date,
    parse_request,
    request_from_form,
)
from ambulance_bot.task_runner import TaskRunner
from ambulance_bot.task_store import JsonTaskStore


load_dotenv()

app = Flask(__name__)
artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
store = JsonTaskStore(artifacts_dir / "tasks")
runner = TaskRunner(artifacts_dir, store=store)


@app.get("/")
def index():
    return redirect(url_for("new_task"))


@app.get("/app")
def new_task():
    selected_case = pop_selected_case()
    person_options = selected_case.get("person_options") or PERSON_OPTIONS
    case_lookup = read_case_lookup()
    lookup_request = read_case_lookup_request()
    if lookup_request.get("status") == "case_lookup_requested":
        current_detail = str(case_lookup.get("detail") or "").strip()
        suffix = "已要求公務電腦重新查詢，等待 worker 回傳。"
        case_lookup["detail"] = f"{current_detail} {suffix}".strip()
        case_lookup["is_running"] = True
    case_lookup.setdefault("cases", [])
    case_lookup["case_count"] = len(case_lookup.get("cases") or [])
    case_lookup["debug_artifacts"] = case_lookup_debug_artifacts()
    return render_template(
        "new_task.html",
        recent_tasks=store.list_recent(limit=5),
        case_lookup=case_lookup,
        selected_case=selected_case,
        vehicle_options=VEHICLE_OPTIONS,
        person_options=person_options,
        case_reason_options=CASE_REASON_OPTIONS,
        consumable_options=consumable_inventory_options(),
        default_consumables=DEFAULT_CONSUMABLES,
        disinfection_item_options=DISINFECTION_ITEM_OPTIONS,
        default_disinfection_items=DEFAULT_DISINFECTION_ITEMS,
    )


@app.post("/cases/query")
def query_cases():
    lookup_range = str(request.form.get("lookup_range") or "24h").strip()
    if lookup_range not in {"24h", "6h", "today"}:
        lookup_range = "24h"
    write_case_lookup_request(lookup_range)
    return redirect(url_for("new_task"))


@app.post("/cases/import")
def import_case():
    case_id = str(request.form.get("case_id") or "").strip()
    if not case_id:
        abort(400)
    if not write_selected_case_from_lookup(case_id):
        abort(404)
    return redirect(url_for("new_task"))


@app.post("/tasks")
def create_task():
    task_request = request_from_form(request.form)
    store.create(task_request)
    return redirect(url_for("task_detail", task_id=task_request.task_id))


@app.get("/tasks/<task_id>")
def task_detail(task_id: str):
    try:
        payload = store.get(task_id)
    except FileNotFoundError:
        abort(404)
    return render_template("task_detail.html", payload=payload)


@app.post("/tasks/<task_id>/run")
def run_task(task_id: str):
    try:
        store.get(task_id)
    except FileNotFoundError:
        abort(404)
    if task_execution_mode() == "worker_queue":
        request_payload = store.request_for(task_id)
        for adapter in default_adapters():
            store.update_site_result(task_id, adapter.run(request_payload))
        store.queue_for_worker(task_id)
        return redirect(url_for("task_detail", task_id=task_id))
    runner.start_existing(task_id)
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
    return jsonify({"ok": True, "status": runner.latest_status_text()})


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
    if not status_text:
        abort(400)
    site_key = str(data.get("site_key") or "").strip()
    site_name = str(data.get("site_name") or "公務電腦 worker").strip()
    try:
        if site_key:
            store.update_site_result(task_id, SiteAutomationResult(site_key, site_name, status_text, detail))
        payload = store.set_overall_status(task_id, status_text, detail)
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


def worker_authorized() -> bool:
    expected = os.getenv("WORKER_TOKEN", "").strip()
    if not expected:
        return True
    supplied = request.headers.get("X-Worker-Token", "").strip()
    return supplied == expected


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


def write_case_lookup_request(lookup_range: str) -> dict:
    output_dir = artifacts_dir / "cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "case_lookup_requested",
        "lookup_range": lookup_range,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "detail": "手機端已要求公務電腦 worker 重新查詢前 24 小時案件。",
    }
    write_json_atomic(case_lookup_request_path(), payload)
    return payload


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


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def status_label(status: str) -> str:
    value = str(status or "")
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
    if value in {"completed_by_user"} or value.endswith("_saved"):
        return "complete"
    if "failed" in value or "error" in value:
        return "failed"
    if "running" in value or value in {"queued_for_worker", "claimed_by_worker"}:
        return "running"
    if "captcha" in value or "ready" in value or "prefilled" in value:
        return "waiting"
    return "idle"


def effective_task_status(payload: dict) -> str:
    sites = list(dict(payload.get("site_statuses") or {}).values())
    if any(status_class(str(site.get("status") or "")) == "failed" for site in sites):
        return "failed"
    if any(status_class(str(site.get("status") or "")) == "waiting" for site in sites):
        return "manual_captcha_required"
    if sites and all(status_class(str(site.get("status") or "")) == "complete" for site in sites):
        return "completed_by_user"
    return str(payload.get("overall_status") or "")


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
    return {
        "vehicle_mileage": "里程",
        "consumables": "耗材",
        "disinfection": "消毒",
        "duty_work_log": "工作",
    }.get(key, str(site.get("name") or "站台"))


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
    end_date = short_date(case.get("return_time") or "") or start_date
    start_time = normalize_hhmm(str(case.get("case_time_hhmm") or _time_from_text(case.get("report_time"))))
    end_time = normalize_hhmm(str(case.get("return_time_hhmm") or _time_from_text(case.get("return_time"))))
    start = f"{start_date} {start_time}".strip() if start_date or start_time else "未填"
    if not end_time:
        return start
    end = f"{end_date} {end_time}".strip() if end_date or end_time else ""
    return f"{start} - {end}" if end else start


def selected_case_address(case: dict) -> str:
    return display_case_address(case)


def selected_case_date_input(case: dict) -> str:
    return date_input_value(case.get("case_date") or case.get("report_time") or "")


def selected_return_date_input(case: dict) -> str:
    if not normalize_hhmm(str(case.get("return_time_hhmm") or _time_from_text(case.get("return_time")))):
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


def visible_events(events: list[dict]) -> list[dict]:
    important: list[dict] = []
    for event in events:
        status = str(event.get("status") or "")
        if status in {"created", "running", "queued_for_worker", "claimed_by_worker"}:
            continue
        if status in {"local_pc_ready", "manual_captcha_required"}:
            continue
        important.append(event)
    return important[-5:] if important else events[-3:]


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
        "site_short_name": site_short_name,
        "status_class": status_class,
        "status_label": status_label,
        "task_datetime_display": task_datetime_display,
        "task_title": task_title,
        "visible_events": visible_events,
    }


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
        "detail": "已由查詢結果帶入案件資料與服勤人員。",
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


def pop_selected_case() -> dict:
    selected = read_selected_case()
    if selected:
        try:
            (artifacts_dir / "cases" / "selected.json").unlink()
        except OSError:
            pass
    return selected


if __name__ == "__main__":
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("WEB_PORT", "8080"))
    print(f"[app] starting ambulance return web app on {host}:{port}", flush=True)
    try:
        from waitress import serve
    except ImportError:
        print("[app] waitress unavailable, using Flask development server", flush=True)
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    else:
        print("[app] waitress serving", flush=True)
        serve(app, host=host, port=port, threads=8)
