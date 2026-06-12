from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from consumables_login import login_acs_and_get_driver, open_consumable_record_for_task
from ambulance_bot.adapters import SITE_DEFINITIONS, SiteAutomationResult
from ambulance_bot.manual_task_lock import manual_task_lock_active
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.selenium_local import (
    query_duty_emergency_cases,
    run_disinfection_task,
    run_local_selenium_task,
    run_vehicle_mileage_task,
)
from ambulance_bot.window_layout import maximize_worker_site_windows


load_dotenv()

MANUAL_TASK_ACTIVE = threading.Event()
SITE_NAMES = {site.key: site.name for site in SITE_DEFINITIONS}


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
            run_all_sites_task(server_url, worker_id, task, artifacts_dir)
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
    if MANUAL_TASK_ACTIVE.is_set() or manual_task_lock_active(artifacts_dir):
        print("[worker] scheduled case lookup skipped: manual task active", flush=True)
        return last_lookup_at, last_case_hash

    request_payload = fetch_case_lookup_request(server_url)
    now = time.time()
    manual_lookup = request_payload is not None
    if manual_lookup:
        lookup_range = "24h"
        source = str(request_payload.get("source") or "NAS端")
        print(f"[worker] manual case lookup requested range={lookup_range} source={source}", flush=True)
    elif now - last_lookup_at >= max(interval_seconds, 60):
        if last_case_lookup_waiting_for_login(artifacts_dir):
            print("[worker] scheduled case lookup skipped: waiting for valid duty login", flush=True)
            return now, last_case_hash
        lookup_range = "24h"
        print(f"[worker] scheduled case lookup range={lookup_range}", flush=True)
    else:
        return last_lookup_at, last_case_hash

    result = query_duty_emergency_cases(artifacts_dir, lookup_range=lookup_range)
    print(f"[worker] case lookup result status={result.status} count={len(result.cases)} detail={result.detail}", flush=True)
    case_hash = hash_cases(result.cases)
    if not manual_lookup and case_hash == last_case_hash:
        print("[worker] case lookup unchanged; skip posting", flush=True)
        return now, last_case_hash
    post_cases(server_url, result.status, result.detail, lookup_range, result.cases, case_hash)
    print(f"[worker] case lookup posted count={len(result.cases)}", flush=True)
    return now, case_hash


def fetch_next_task(server_url: str, worker_id: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/next-task?worker_id={urllib.parse.quote(worker_id)}"
    data = request_json(url)
    return data.get("task") if data.get("ok") else None


def fetch_task(server_url: str, task_id: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/tasks/{urllib.parse.quote(task_id)}"
    data = request_json(url)
    return data.get("task") if data.get("ok") else None


def fetch_task_payload(server_url: str, task_id: str) -> dict[str, object] | None:
    url = f"{server_url}/worker/tasks/{urllib.parse.quote(task_id)}"
    data = request_json(url)
    payload = data.get("payload") if data.get("ok") else None
    return payload if isinstance(payload, dict) else None


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


def run_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
    profile_name: str = "chrome_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
    update_overall: bool = True,
) -> object:
    request = AmbulanceReturnRequest.from_dict(task)
    print(f"[worker] claimed task {request.task_id}", flush=True)
    post_status(server_url, request.task_id, "worker_running", f"公務電腦 worker 執行中：{worker_id}")
    try:
        result = run_local_selenium_task(
            request,
            artifacts_dir,
            profile_name=profile_name,
            debugger_port=debugger_port,
            use_session_lock=use_session_lock,
            tile_name=tile_name,
            force_new_driver=force_new_driver,
        )
    except Exception as exc:
        result = SiteAutomationResult("duty_work_log", "消防勤務工作紀錄", "duty_work_log_failed", f"工作紀錄操作失敗：{exc}")
    post_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
        site_key="duty_work_log",
        site_name="消防勤務工作紀錄",
    )
    if update_overall:
        post_status(
            server_url,
            request.task_id,
            "desktop_fast_completed_with_errors" if _status_blocks_progress(result.status) else "desktop_fast_completed",
            result.detail,
        )
    print(f"[worker] finished task {request.task_id}: {result.status}", flush=True)
    return result


def run_all_sites_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
) -> object | None:
    request = AmbulanceReturnRequest.from_dict(task)
    profile_suffix = request.task_id.replace("-", "_")
    post_status(server_url, request.task_id, "desktop_fast_running", "公務電腦 worker 四站登打已啟動。")
    runners = [
        (
            "duty_work_log",
            lambda: run_task(
                server_url,
                worker_id,
                task,
                artifacts_dir,
                profile_name=f"duty_work_log_profile_{profile_suffix}",
                use_session_lock=False,
                tile_name="duty_work_log",
                force_new_driver=True,
                update_overall=False,
            ),
        ),
        (
            "vehicle_mileage",
            lambda: run_vehicle_task(
                server_url,
                worker_id,
                task,
                artifacts_dir,
                profile_name=f"vehicle_mileage_profile_{profile_suffix}",
                use_session_lock=False,
                tile_name="vehicle_mileage",
                force_new_driver=True,
                update_overall=False,
            ),
        ),
        (
            "disinfection",
            lambda: run_disinfection_worker_task(
                server_url,
                worker_id,
                task,
                artifacts_dir,
                profile_name=f"disinfection_profile_{profile_suffix}",
                use_session_lock=False,
                tile_name="disinfection",
                force_new_driver=True,
                update_overall=False,
            ),
        ),
        (
            "consumables",
            lambda: run_consumables_worker_task(
                server_url,
                worker_id,
                task,
                artifacts_dir,
                profile_name=f"consumables_profile_{profile_suffix}",
                tile_name="consumables",
                update_overall=False,
            ),
        ),
    ]
    last_result = None
    for site_key, runner in runners:
        try:
            try:
                payload = fetch_task_payload(server_url, request.task_id)
            except Exception as exc:
                detail = f"讀取任務狀態失敗，四站流程已停止：{exc}"
                result = SiteAutomationResult(site_key, SITE_NAMES.get(site_key, site_key), f"{site_key}_failed", detail)
                post_status(server_url, request.task_id, "desktop_fast_completed_with_errors", detail)
                return result
            if not isinstance(payload, dict):
                detail = "讀取任務狀態失敗，NAS 未回傳任務內容，四站流程已停止。"
                result = SiteAutomationResult(site_key, SITE_NAMES.get(site_key, site_key), f"{site_key}_failed", detail)
                post_status(server_url, request.task_id, "desktop_fast_completed_with_errors", detail)
                return result
            site_statuses = payload.get("site_statuses") if isinstance(payload, dict) and isinstance(payload.get("site_statuses"), dict) else {}
            current_status = str((site_statuses.get(site_key) or {}).get("status") or "")
            if _site_is_complete(current_status):
                print(f"[worker] skip completed site task={request.task_id} site={site_key}", flush=True)
                continue
            last_result = runner()
            if _result_blocks_progress(last_result):
                post_status(
                    server_url,
                    request.task_id,
                    "desktop_fast_completed_with_errors",
                    f"{SITE_NAMES.get(site_key, site_key)}未完成：{getattr(last_result, 'detail', '')}",
                )
                return last_result
        finally:
            maximize_worker_site_windows()
    post_status(server_url, request.task_id, "desktop_fast_completed", "公務電腦 worker 四站登打完成。")
    maximize_worker_site_windows()
    return last_result


def run_vehicle_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
    profile_name: str = "chrome_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
    update_overall: bool = True,
) -> object:
    request = AmbulanceReturnRequest.from_dict(task)
    print(f"[worker] vehicle mileage task {request.task_id}", flush=True)
    post_status(server_url, request.task_id, "vehicle_mileage_running", f"公務電腦 worker 執行車輛里程：{worker_id}")
    try:
        result = run_vehicle_mileage_task(
            request,
            artifacts_dir,
            profile_name=profile_name,
            debugger_port=debugger_port,
            use_session_lock=use_session_lock,
            tile_name=tile_name,
            force_new_driver=force_new_driver,
        )
    except Exception as exc:
        result = SiteAutomationResult("vehicle_mileage", "車輛里程", "vehicle_mileage_failed", f"車輛里程操作失敗：{exc}")
    post_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
        site_key="vehicle_mileage",
        site_name="車輛里程",
    )
    if update_overall:
        post_status(
            server_url,
            request.task_id,
            "desktop_fast_completed_with_errors" if _status_blocks_progress(result.status) else "desktop_fast_completed",
            result.detail,
        )
    print(f"[worker] finished vehicle mileage {request.task_id}: {result.status}", flush=True)
    return result


def run_disinfection_worker_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
    driver=None,
    profile_name: str = "chrome_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
    update_overall: bool = True,
):
    request = AmbulanceReturnRequest.from_dict(task)
    print(f"[worker] disinfection task {request.task_id}", flush=True)
    post_status(server_url, request.task_id, "disinfection_running", f"公務電腦 worker 執行消毒紀錄：{worker_id}")
    try:
        result = run_disinfection_task(
            request,
            artifacts_dir,
            existing_driver=driver,
            profile_name=profile_name,
            debugger_port=debugger_port,
            use_session_lock=use_session_lock,
            tile_name=tile_name,
            force_new_driver=force_new_driver,
        )
    except Exception as exc:
        result = SiteAutomationResult("disinfection", "緊急救護消毒", "disinfection_failed", f"消毒紀錄操作失敗：{exc}")
    post_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
        site_key="disinfection",
        site_name="緊急救護消毒",
    )
    if update_overall:
        post_status(
            server_url,
            request.task_id,
            "desktop_fast_completed_with_errors" if _status_blocks_progress(result.status) else "desktop_fast_completed",
            result.detail,
        )
    print(f"[worker] finished disinfection {request.task_id}: {result.status}", flush=True)
    return result


def run_consumables_worker_task(
    server_url: str,
    worker_id: str,
    task: dict[str, object],
    artifacts_dir: Path,
    profile_name: str = "consumables_profile",
    debugger_port: int | None = None,
    tile_name: str = "",
    update_overall: bool = True,
) -> SiteAutomationResult:
    request = AmbulanceReturnRequest.from_dict(task)
    print(f"[worker] consumables task {request.task_id}", flush=True)
    post_status(
        server_url,
        request.task_id,
        "consumables_running",
        f"公務電腦 worker 執行耗材：{worker_id}",
        site_key="consumables",
        site_name="一站通耗材",
    )
    try:
        driver = login_acs_and_get_driver(
            profile_name=profile_name,
            debugger_port=debugger_port,
            tile_name=tile_name,
            task=request,
        )
        detail = open_consumable_record_for_task(driver, request)
        result = SiteAutomationResult("consumables", "一站通耗材", "consumables_saved", detail)
    except Exception as exc:
        result = SiteAutomationResult("consumables", "一站通耗材", "consumables_failed", f"耗材登打失敗：{exc}")
    post_status(
        server_url,
        request.task_id,
        result.status,
        result.detail,
        site_key="consumables",
        site_name="一站通耗材",
    )
    if update_overall:
        post_status(
            server_url,
            request.task_id,
            "desktop_fast_completed_with_errors" if _result_blocks_progress(result) else "desktop_fast_completed",
            result.detail,
        )
    print(f"[worker] finished consumables {request.task_id}: {result.status}", flush=True)
    return result


def request_json(url: str) -> dict[str, object]:
    req = urllib.request.Request(url, headers=worker_headers())
    try:
        with urllib.request.urlopen(req, timeout=worker_api_timeout()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(worker_api_error_message(exc)) from exc


def post_status(
    server_url: str,
    task_id: str,
    status: str,
    detail: str,
    site_key: str = "",
    site_name: str = "",
    overall_status: str = "",
    overall_detail: str = "",
) -> None:
    payload = {
        "status": status,
        "detail": detail,
        "site_key": site_key,
        "site_name": site_name,
    }
    if overall_status:
        payload["overall_status"] = overall_status
        payload["overall_detail"] = overall_detail or detail
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/worker/tasks/{task_id}/status",
        data=body,
        headers={**worker_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=worker_api_timeout()) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(worker_api_error_message(exc)) from exc


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
    try:
        with urllib.request.urlopen(req, timeout=worker_api_timeout()) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(worker_api_error_message(exc)) from exc


def worker_api_timeout() -> int:
    return int(os.getenv("WORKER_API_TIMEOUT_SECONDS", "8"))


def worker_api_error_message(exc: urllib.error.HTTPError) -> str:
    if exc.code == 403:
        return "NAS worker API 拒絕連線（HTTP 403）：WORKER_TOKEN 未設定或與 NAS 不一致，請同步 NAS 與公務電腦 .env 後重啟 worker。"
    return f"NAS worker API 回應 HTTP {exc.code}：{exc.reason}"


def hash_cases(cases: list[dict[str, object]]) -> str:
    normalized = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _status_blocks_progress(status: str) -> bool:
    text = str(status or "")
    if "failed" in text or "error" in text:
        return True
    if text.startswith("needs_") or "login" in text:
        return True
    return False


def _result_blocks_progress(result: object) -> bool:
    if getattr(result, "ok", True) is False:
        return True
    return _status_blocks_progress(str(getattr(result, "status", "") or ""))


def _site_is_complete(status: str) -> bool:
    value = str(status or "")
    return value == "completed_by_user" or value.endswith("_saved")


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
