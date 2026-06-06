from __future__ import annotations

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

from ambulance_bot.adapters import SiteAutomationResult, default_adapters
from ambulance_bot.line_api import reply_text, verify_signature
from ambulance_bot.models import (
    CASE_REASON_OPTIONS,
    COMMAND_PREFIX,
    DEFAULT_DISINFECTION_ITEMS,
    DISINFECTION_ITEM_OPTIONS,
    PERSON_OPTIONS,
    VEHICLE_OPTIONS,
    example_command,
    clean_case_address,
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
    selected_case = read_selected_case()
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
        disinfection_item_options=DISINFECTION_ITEM_OPTIONS,
        default_disinfection_items=DEFAULT_DISINFECTION_ITEMS,
    )


@app.post("/cases/query")
def query_cases():
    lookup_range = str(request.form.get("lookup_range") or "6h").strip()
    if lookup_range not in {"6h", "today"}:
        lookup_range = "6h"
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
        "detail": "手機端已要求公務電腦 worker 重新查詢案件。",
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
