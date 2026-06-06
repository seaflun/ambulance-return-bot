from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.selenium_local import query_duty_emergency_cases, run_local_selenium_task


load_dotenv()


def main() -> None:
    server_url = os.getenv("WORKER_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
    worker_id = os.getenv("WORKER_ID", socket.gethostname() or "public-duty-pc")
    poll_seconds = int(os.getenv("WORKER_POLL_SECONDS", "10"))
    lookup_interval_seconds = int(os.getenv("CASE_LOOKUP_INTERVAL_SECONDS", "300"))
    run_once = os.getenv("WORKER_RUN_ONCE", "false").strip().lower() in {"1", "true", "yes", "on"}
    auto_claim_tasks = os.getenv("WORKER_AUTO_CLAIM_TASKS", "false").strip().lower() in {"1", "true", "yes", "on"}
    artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
    last_case_lookup_at = time.time()
    last_case_hash = load_last_case_hash(artifacts_dir)

    if os.getenv("WORKER_USE_LOCAL_CHROME", "true").strip().lower() not in {"0", "false", "no", "off"}:
        os.environ["SELENIUM_REMOTE_URL"] = ""
        os.environ.setdefault("SELENIUM_DETACH", "true")

    print(f"[worker] starting worker_id={worker_id} server={server_url}", flush=True)
    while True:
        try:
            last_case_lookup_at, last_case_hash = maybe_run_case_lookup(
                server_url,
                artifacts_dir,
                last_case_lookup_at,
                last_case_hash,
                lookup_interval_seconds,
            )
            task = fetch_next_task(server_url, worker_id) if auto_claim_tasks else None
            if task is None:
                if run_once:
                    print("[worker] no queued task", flush=True)
                    return
                time.sleep(poll_seconds)
                continue
            run_task(server_url, worker_id, task, artifacts_dir)
            if run_once:
                return
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[worker] loop error: {exc}", flush=True)
            if run_once:
                return
            time.sleep(poll_seconds)


def maybe_run_case_lookup(
    server_url: str,
    artifacts_dir: Path,
    last_lookup_at: float,
    last_case_hash: str,
    interval_seconds: int,
) -> tuple[float, str]:
    request_payload = fetch_case_lookup_request(server_url)
    now = time.time()
    manual_lookup = request_payload is not None
    if manual_lookup:
        lookup_range = str(request_payload.get("lookup_range") or "6h")
        print(f"[worker] manual case lookup requested range={lookup_range}", flush=True)
    elif now - last_lookup_at >= max(interval_seconds, 60):
        if last_case_lookup_waiting_for_login(artifacts_dir):
            print("[worker] scheduled case lookup skipped: waiting for valid duty login", flush=True)
            return now, last_case_hash
        lookup_range = "today"
        print(f"[worker] scheduled case lookup range={lookup_range}", flush=True)
    else:
        return last_lookup_at, last_case_hash

    result = query_duty_emergency_cases(artifacts_dir, lookup_range=lookup_range)
    case_hash = hash_cases(result.cases)
    if not manual_lookup and case_hash == last_case_hash:
        print("[worker] case lookup unchanged; skip posting", flush=True)
        return now, last_case_hash
    post_cases(server_url, result.status, result.detail, lookup_range, result.cases, case_hash)
    return now, case_hash


def fetch_next_task(server_url: str, worker_id: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/next-task?worker_id={urllib.parse.quote(worker_id)}"
    data = request_json(url)
    return data.get("task") if data.get("ok") else None


def fetch_task(server_url: str, task_id: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/tasks/{urllib.parse.quote(task_id)}"
    data = request_json(url)
    return data.get("task") if data.get("ok") else None


def fetch_recent_tasks(server_url: str, limit: int = 20) -> list[dict[str, object]]:
    url = f"{server_url}/worker/tasks?limit={int(limit)}"
    data = request_json(url)
    tasks = data.get("tasks") if data.get("ok") else []
    return tasks if isinstance(tasks, list) else []


def fetch_case_lookup_request(server_url: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/case-lookup-request"
    data = request_json(url)
    request_payload = data.get("request") if data.get("ok") else None
    return request_payload if isinstance(request_payload, dict) else None


def run_task(server_url: str, worker_id: str, task: dict[str, object], artifacts_dir: Path) -> None:
    request = AmbulanceReturnRequest.from_dict(task)
    print(f"[worker] claimed task {request.task_id}", flush=True)
    post_status(server_url, request.task_id, "worker_running", f"公務電腦 worker 執行中：{worker_id}")
    result = run_local_selenium_task(request, artifacts_dir)
    post_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
        site_key="duty_work_log",
        site_name="消防勤務工作紀錄",
    )
    print(f"[worker] finished task {request.task_id}: {result.status}", flush=True)


def request_json(url: str) -> dict[str, object]:
    req = urllib.request.Request(url, headers=worker_headers())
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def post_status(
    server_url: str,
    task_id: str,
    status: str,
    detail: str,
    site_key: str = "",
    site_name: str = "",
) -> None:
    payload = {
        "status": status,
        "detail": detail,
        "site_key": site_key,
        "site_name": site_name,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/worker/tasks/{task_id}/status",
        data=body,
        headers={**worker_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        response.read()


def post_cases(
    server_url: str,
    status: str,
    detail: str,
    lookup_range: str,
    cases: list[dict[str, object]],
    case_hash: str,
) -> None:
    payload = {
        "status": status,
        "detail": detail,
        "lookup_range": lookup_range,
        "case_hash": case_hash,
        "source": "public_duty_pc_worker",
        "cases": cases,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/worker/cases",
        data=body,
        headers={**worker_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        response.read()


def hash_cases(cases: list[dict[str, object]]) -> str:
    normalized = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_last_case_hash(artifacts_dir: Path) -> str:
    path = artifacts_dir / "cases" / "latest.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("case_hash") or hash_cases(payload.get("cases") or []))


def last_case_lookup_waiting_for_login(artifacts_dir: Path) -> bool:
    path = artifacts_dir / "cases" / "latest.json"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return str(payload.get("status") or "") in {"needs_duty_login", "duty_login_failed"}


def worker_headers() -> dict[str, str]:
    token = os.getenv("WORKER_TOKEN", "").strip()
    return {"X-Worker-Token": token} if token else {}


if __name__ == "__main__":
    main()
