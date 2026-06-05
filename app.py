from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from ambulance_bot.line_api import reply_text, verify_signature
from ambulance_bot.models import (
    CASE_REASON_OPTIONS,
    COMMAND_PREFIX,
    PERSON_OPTIONS,
    VEHICLE_OPTIONS,
    example_command,
    clean_case_address,
    parse_request,
    request_from_form,
)
from ambulance_bot.selenium_local import query_duty_emergency_cases
from ambulance_bot.task_runner import TaskRunner
from ambulance_bot.task_store import JsonTaskStore


load_dotenv()

app = Flask(__name__)
artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
store = JsonTaskStore(artifacts_dir / "tasks")
runner = TaskRunner(artifacts_dir, store=store)
_case_lookup_lock = threading.Lock()
_case_lookup_running = False
_case_lookup_scheduler_started = False


@app.get("/")
def index():
    return redirect(url_for("new_task"))


@app.get("/app")
def new_task():
    selected_case = read_selected_case()
    person_options = selected_case.get("person_options") or PERSON_OPTIONS
    case_lookup = read_case_lookup()
    if _case_lookup_running:
        case_lookup["detail"] = "正在查詢最近 6 小時案件，完成後重新整理會顯示結果。"
    return render_template(
        "new_task.html",
        recent_tasks=store.list_recent(limit=5),
        case_lookup=case_lookup,
        selected_case=selected_case,
        vehicle_options=VEHICLE_OPTIONS,
        person_options=person_options,
        case_reason_options=CASE_REASON_OPTIONS,
    )


@app.post("/cases/query")
def query_cases():
    start_case_lookup_once()
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


def start_case_lookup_once() -> bool:
    global _case_lookup_running
    with _case_lookup_lock:
        if _case_lookup_running:
            return False
        _case_lookup_running = True
    if app.config.get("TESTING"):
        run_case_lookup_background()
    else:
        thread = threading.Thread(target=run_case_lookup_background, daemon=True)
        thread.start()
    return True


def start_case_lookup_scheduler() -> None:
    global _case_lookup_scheduler_started
    if app.config.get("TESTING"):
        return
    raw = os.getenv("CASE_LOOKUP_SCHEDULER_ENABLED", "true").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return
    with _case_lookup_lock:
        if _case_lookup_scheduler_started:
            return
        _case_lookup_scheduler_started = True
    thread = threading.Thread(target=run_case_lookup_scheduler, daemon=True)
    thread.start()


def run_case_lookup_scheduler() -> None:
    interval = int(os.getenv("CASE_LOOKUP_INTERVAL_SECONDS", "300"))
    while True:
        start_case_lookup_once()
        time.sleep(max(interval, 60))


def run_case_lookup_background() -> None:
    global _case_lookup_running
    try:
        query_duty_emergency_cases(artifacts_dir)
    finally:
        with _case_lookup_lock:
            _case_lookup_running = False


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
    start_case_lookup_scheduler()
    try:
        from waitress import serve
    except ImportError:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    else:
        serve(app, host=host, port=port, threads=8)
