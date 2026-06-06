from __future__ import annotations

import os
import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.client_config import ClientConfig
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .adapters import SITE_DEFINITIONS
from .duty_credentials import load_duty_credential
from .models import DEFAULT_DISINFECTION_ITEMS, VEHICLE_PPE_NAMES, AmbulanceReturnRequest, clean_case_address


BASE_URL = "https://dutymgt.tyfd.gov.tw/tyfd119"
EMS_BASE_URL = "https://emsdt.tyfd.gov.tw/EmmWeb"
EMS_DISINFECTION_AP = "wap119.RPS64101014"
DUTY_WORK_LOG_AP = "wap119.RPS04060"
CASE_LOOKUP_DEBUGGER_PORT = 9223
_SELENIUM_SESSION_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class SeleniumRunResult:
    ok: bool
    status: str
    detail: str
    summary_path: Path


@dataclass(frozen=True, slots=True)
class DutyCaseLookupResult:
    ok: bool
    status: str
    detail: str
    cases: list[dict[str, str]]
    path: Path


@dataclass(frozen=True, slots=True)
class DutyCaseImportResult:
    ok: bool
    status: str
    detail: str
    selected_case: dict[str, object]
    path: Path


def selenium_enabled() -> bool:
    raw = os.getenv("USE_LOCAL_SELENIUM", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def run_local_selenium_task(request: AmbulanceReturnRequest, artifacts_dir: Path) -> SeleniumRunResult:
    output_dir = artifacts_dir / "selenium"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"{request.task_id}.txt"
    summary_path.write_text(_task_text(request), encoding="utf-8")

    driver = None
    lock_acquired = False
    keep_browser_open = os.getenv("WORKER_KEEP_BROWSER_OPEN_ON_TASK", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    try:
        lock_acquired = _acquire_selenium_session(f"task {request.task_id}")
        print(f"[task] creating selenium driver for {request.task_id}", flush=True)
        debugger_port = int(os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223"))
        driver = _create_driver(artifacts_dir, debugger_port=debugger_port, attach_existing=True)
        print(f"[task] selenium driver ready for {request.task_id}", flush=True)
        _set_window_size_if_enabled(driver, "task")
        driver.implicitly_wait(2)
        result = _prepare_duty_work_log_form(driver, request, output_dir, summary_path)
        return result
    except Exception as exc:
        if driver is not None:
            _save_artifacts(driver, output_dir, request.task_id, "duty_work_log_error")
        return SeleniumRunResult(
            ok=False,
            status="chrome_start_failed",
            detail=f"Selenium \u555f\u52d5\u6216\u64cd\u4f5c\u5931\u6557\uff1a{exc}",
            summary_path=summary_path,
        )
    finally:
        if not keep_browser_open:
            _quit_driver(driver)
        if lock_acquired:
            _release_selenium_session(f"task {request.task_id}")


def run_vehicle_mileage_task(request: AmbulanceReturnRequest, artifacts_dir: Path) -> SeleniumRunResult:
    output_dir = artifacts_dir / "selenium"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"{request.task_id}.txt"
    summary_path.write_text(_task_text(request), encoding="utf-8")

    driver = None
    lock_acquired = False
    keep_browser_open = os.getenv("WORKER_KEEP_BROWSER_OPEN_ON_TASK", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    try:
        lock_acquired = _acquire_selenium_session(f"vehicle_mileage {request.task_id}")
        debugger_port = int(os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223"))
        driver = _create_driver(artifacts_dir, debugger_port=debugger_port, attach_existing=True)
        _set_window_size_if_enabled(driver, "vehicle_mileage")
        driver.implicitly_wait(2)
        detail = _open_vehicle_mileage_page(driver, request, output_dir)
        return SeleniumRunResult(True, "vehicle_mileage_prefilled", detail, summary_path)
    except Exception as exc:
        if driver is not None:
            _save_artifacts(driver, output_dir, request.task_id, "vehicle_mileage_error")
        return SeleniumRunResult(False, "vehicle_mileage_failed", f"車輛里程操作失敗：{exc}", summary_path)
    finally:
        if not keep_browser_open:
            _quit_driver(driver)
        if lock_acquired:
            _release_selenium_session(f"vehicle_mileage {request.task_id}")


def run_disinfection_task(
    request: AmbulanceReturnRequest,
    artifacts_dir: Path,
    existing_driver: webdriver.Chrome | None = None,
) -> SeleniumRunResult:
    output_dir = artifacts_dir / "selenium"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"{request.task_id}.txt"
    summary_path.write_text(_task_text(request), encoding="utf-8")

    driver = existing_driver
    lock_acquired = False
    owns_driver = existing_driver is None
    keep_browser_open = os.getenv("WORKER_KEEP_BROWSER_OPEN_ON_TASK", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    try:
        lock_acquired = _acquire_selenium_session(f"disinfection {request.task_id}")
        if driver is None:
            debugger_port = int(os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223"))
            driver = _create_driver(artifacts_dir, debugger_port=debugger_port, attach_existing=True)
        _set_window_size_if_enabled(driver, "disinfection")
        driver.implicitly_wait(2)
        detail = _open_disinfection_page(driver, request, output_dir)
        return SeleniumRunResult(True, "disinfection_session_ready", detail, summary_path)
    except Exception as exc:
        if driver is not None:
            _save_artifacts(driver, output_dir, request.task_id, "disinfection_error")
        return SeleniumRunResult(False, "disinfection_failed", f"消毒紀錄操作失敗：{exc}", summary_path)
    finally:
        if owns_driver and not keep_browser_open:
            _quit_driver(driver)
        if lock_acquired:
            _release_selenium_session(f"disinfection {request.task_id}")


def query_duty_emergency_cases(artifacts_dir: Path, lookup_range: str = "6h") -> DutyCaseLookupResult:
    output_dir = artifacts_dir / "cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "latest.json"
    previous_cases = _previous_case_details(output_path)
    print(f"[case_lookup] starting duty emergency case lookup range={lookup_range}", flush=True)
    driver = None
    lock_acquired = False
    try:
        lock_acquired = _acquire_selenium_session(f"case_lookup {lookup_range}")
        driver = _create_driver(
            artifacts_dir,
            profile_name="case_lookup_profile",
            debugger_port=CASE_LOOKUP_DEBUGGER_PORT,
            attach_existing=True,
        )
    except Exception as exc:
        _quit_driver(driver)
        if lock_acquired:
            _release_selenium_session(f"case_lookup {lookup_range}")
            lock_acquired = False
        payload = _case_lookup_payload(
            "chrome_start_failed",
            f"Chrome \u555f\u52d5\u5931\u6557\uff1a{exc}",
            [],
        )
        _write_json_atomic(output_path, payload)
        return DutyCaseLookupResult(False, payload["status"], payload["detail"], [], output_path)
    try:
        _set_window_size_if_enabled(driver, "case_lookup")
        driver.implicitly_wait(2)
        if not _ensure_duty_login(driver):
            _save_artifacts(driver, artifacts_dir / "selenium", "case_lookup", "duty_login")
            login_error = _login_error_text(driver)
            status = "duty_login_failed" if login_error else "needs_duty_login"
            detail = (
                f"\u6d88\u9632\u52e4\u52d9\u81ea\u52d5\u767b\u5165\u5931\u6557\uff1a{login_error}"
                if login_error
                else "\u5df2\u5728\u516c\u52d9\u96fb\u8166 worker Chrome \u958b\u555f\u6d88\u9632\u52e4\u52d9\u767b\u5165\u9801\uff0c\u4f46\u76ee\u524d\u5c1a\u672a\u767b\u5165\uff1b\u8acb\u78ba\u8a8d worker \u53ef\u8b80\u53d6\u6b63\u78ba\u5e33\u5bc6\u3002"
            )
            payload = _case_lookup_payload(
                status,
                detail,
                [],
            )
            _write_json_atomic(output_path, payload)
            driver = None
            return DutyCaseLookupResult(True, payload["status"], payload["detail"], [], output_path)

        _open_case_query(driver, lookup_range=lookup_range)
        cases = _extract_all_emergency_cases(driver)
        cases = _attach_case_form_details(driver, cases, artifacts_dir, previous_cases)
        _save_artifacts(driver, artifacts_dir / "selenium", "case_lookup", "duty_cases")
        payload = _case_lookup_payload(
            "cases_loaded",
            f"\u5df2\u67e5\u5230 {len(cases)} \u7b46{_lookup_range_label(lookup_range)}\u7684\u7dca\u6025\u6551\u8b77\u6848\u4ef6\uff0c\u4e26\u9810\u5148\u8b80\u53d6\u670d\u52e4\u4eba\u54e1\u3002",
            cases,
        )
        _write_json_atomic(output_path, payload)
        return DutyCaseLookupResult(True, payload["status"], payload["detail"], cases, output_path)
    except Exception as exc:
        payload = _case_lookup_payload(
            "case_lookup_failed",
            f"\u6848\u4ef6\u67e5\u8a62\u5931\u6557\uff1a{exc}",
            [],
        )
        _write_json_atomic(output_path, payload)
        return DutyCaseLookupResult(False, payload["status"], payload["detail"], [], output_path)
    finally:
        _quit_driver(driver)
        if lock_acquired:
            _release_selenium_session(f"case_lookup {lookup_range}")


def import_duty_case(artifacts_dir: Path, case_id: str) -> DutyCaseImportResult:
    output_dir = artifacts_dir / "cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "selected.json"
    selected: dict[str, object] = {"case_id": case_id}
    selected.update(_case_from_latest(output_dir / "latest.json", case_id))
    try:
        driver = _create_driver(
            artifacts_dir,
            profile_name="case_lookup_profile",
            debugger_port=CASE_LOOKUP_DEBUGGER_PORT,
            attach_existing=True,
        )
        _set_window_size_if_enabled(driver, "case_import")
        driver.implicitly_wait(2)
        if not _try_switch_to_window_containing(driver, case_id):
            _open_case_query(driver)
            if not _try_switch_to_window_containing(driver, case_id):
                raise WebDriverException(f"case not found: {case_id}")
        if not _click_case_choose(driver, case_id):
            raise WebDriverException(f"case not found: {case_id}")
        time.sleep(1.5)
        _switch_to_work_log_form(driver)
        selected.update(_extract_selected_case_form(driver))
        _save_artifacts(driver, artifacts_dir / "selenium", case_id, "selected_case")
        payload = {
            "status": "case_imported",
            "detail": "\u5df2\u7531\u6848\u4ef6\u5e36\u5165\u5de5\u4f5c\u7d00\u9304\u9801\uff0c\u4e26\u8b80\u53d6\u670d\u52e4\u4eba\u54e1\u3002",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "selected_case": selected,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return DutyCaseImportResult(True, payload["status"], payload["detail"], selected, output_path)
    except WebDriverException as exc:
        payload = {
            "status": "case_import_failed",
            "detail": f"\u6848\u4ef6\u5e36\u5165\u5931\u6557\uff1a{exc}",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "selected_case": selected,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return DutyCaseImportResult(False, payload["status"], payload["detail"], selected, output_path)


def _case_from_latest(path: Path, case_id: str) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for item in payload.get("cases") or []:
        if str(item.get("case_id") or "") == case_id:
            return {str(key): str(value or "") for key, value in dict(item).items()}
    return {}


def _case_lookup_payload(status: str, detail: str, cases: list[dict[str, str]]) -> dict[str, object]:
    return {
        "status": status,
        "detail": detail,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "cases": cases,
    }


def _previous_case_details(path: Path) -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    details: dict[str, dict[str, object]] = {}
    for item in payload.get("cases") or []:
        case = dict(item)
        case_id = str(case.get("case_id") or "")
        if case_id:
            details[case_id] = case
    return details


def _acquire_selenium_session(label: str) -> bool:
    timeout = int(os.getenv("SELENIUM_SESSION_LOCK_TIMEOUT_SECONDS", "180"))
    print(f"[selenium] waiting for session lock: {label}", flush=True)
    if not _SELENIUM_SESSION_LOCK.acquire(timeout=timeout):
        raise WebDriverException(f"selenium is busy for more than {timeout} seconds: {label}")
    print(f"[selenium] acquired session lock: {label}", flush=True)
    return True


def _release_selenium_session(label: str) -> None:
    _SELENIUM_SESSION_LOCK.release()
    print(f"[selenium] released session lock: {label}", flush=True)


def _set_window_size_if_enabled(driver: webdriver.Chrome, label: str) -> None:
    raw = os.getenv("SELENIUM_SET_WINDOW_SIZE", "false").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return
    try:
        driver.set_window_size(1280, 900)
    except WebDriverException as exc:
        print(f"[{label}] set_window_size skipped: {exc}", flush=True)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _quit_driver(driver: webdriver.Chrome | None) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except WebDriverException as exc:
        print(f"[selenium] driver quit skipped: {exc}", flush=True)


def _create_driver(
    artifacts_dir: Path,
    profile_name: str = "chrome_profile",
    debugger_port: int | None = None,
    attach_existing: bool = False,
) -> webdriver.Chrome:
    remote_url = os.getenv("SELENIUM_REMOTE_URL", "").strip()
    if remote_url:
        _wait_for_remote_selenium(remote_url)
        options = _remote_browser_options()
        page_timeout = int(os.getenv("SELENIUM_PAGE_LOAD_TIMEOUT_SECONDS", "45"))
        command_timeout = int(os.getenv("SELENIUM_REMOTE_COMMAND_TIMEOUT_SECONDS", "120"))
        driver = _create_remote_driver_with_retry(remote_url, options, command_timeout)
        driver.set_page_load_timeout(page_timeout)
        driver.set_script_timeout(page_timeout)
        return driver

    if attach_existing and debugger_port:
        existing = _connect_existing_chrome(debugger_port)
        if existing:
            return existing

    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-popup-blocking")
    user_data_dir = _profile_dir(profile_name)
    options.add_argument(f"--user-data-dir={user_data_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    if debugger_port:
        options.add_argument(f"--remote-debugging-port={debugger_port}")
    if os.getenv("SELENIUM_DETACH", "true").strip().lower() not in {"0", "false", "no", "off"}:
        options.add_experimental_option("detach", True)
    return webdriver.Chrome(options=options)


def _remote_browser_options() -> Options | FirefoxOptions:
    browser = os.getenv("SELENIUM_BROWSER", "chromium").strip().lower()
    headless = os.getenv("SELENIUM_HEADLESS", "true").strip().lower() not in {"0", "false", "no", "off"}
    if browser == "firefox":
        options = FirefoxOptions()
        if headless:
            options.add_argument("-headless")
        options.add_argument("--width=1280")
        options.add_argument("--height=900")
        return options

    options = Options()
    if headless:
        options.add_argument(os.getenv("SELENIUM_HEADLESS_ARG", "--headless=new"))
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    return options


def _create_remote_driver_with_retry(remote_url: str, options: Options | FirefoxOptions, command_timeout: int) -> webdriver.Chrome:
    attempts = int(os.getenv("SELENIUM_REMOTE_SESSION_ATTEMPTS", "2"))
    browser = os.getenv("SELENIUM_BROWSER", "chromium").strip().lower()
    last_error: Exception | None = None
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            print(
                f"[selenium] creating remote {browser} session attempt {attempt}/{attempts} timeout={command_timeout}",
                flush=True,
            )
            client_config = ClientConfig(remote_server_addr=remote_url, timeout=command_timeout)
            return webdriver.Remote(command_executor=remote_url, options=options, client_config=client_config)
        except Exception as exc:
            last_error = exc
            print(f"[selenium] remote {browser} session attempt {attempt} failed: {exc}", flush=True)
            if attempt < attempts:
                time.sleep(5)
    raise WebDriverException(f"remote {browser} session failed after {attempts} attempts: {last_error}")


def _wait_for_remote_selenium(remote_url: str) -> None:
    timeout = int(os.getenv("SELENIUM_REMOTE_READY_TIMEOUT_SECONDS", "180"))
    status_url = remote_url.rstrip("/")
    if status_url.endswith("/wd/hub"):
        status_url = status_url[:-7]
    status_url = f"{status_url}/status"
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(status_url, timeout=5) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(2)
    raise WebDriverException(f"remote selenium not ready: {last_error}")


def _connect_existing_chrome(debugger_port: int) -> webdriver.Chrome | None:
    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{debugger_port}")
    try:
        return webdriver.Chrome(options=options)
    except WebDriverException:
        return None


def _profile_dir(profile_name: str) -> Path:
    configured = os.getenv("CHROME_PROFILE_DIR", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    root = Path(os.getenv("SELENIUM_PROFILE_ROOT") or os.getenv("LOCALAPPDATA") or Path.home())
    profile_root = root / "ambulance_return_bot"
    profile_root.mkdir(parents=True, exist_ok=True)
    return profile_root / profile_name


def _open_duty_work_log_case_picker(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest,
    output_dir: Path,
    summary_path: Path,
) -> SeleniumRunResult:
    if not _ensure_duty_login(driver):
        _save_artifacts(driver, output_dir, request.task_id, "duty_login")
        return SeleniumRunResult(
            ok=True,
            status="needs_duty_login",
            detail="\u5df2\u5728\u672c\u6a5f Chrome \u958b\u555f\u6d88\u9632\u52e4\u52d9\u767b\u5165\u9801\uff1b\u8acb\u767b\u5165\u5f8c\u518d\u56de\u7db2\u9801\u6309\u4e00\u6b21\u555f\u52d5\u6d41\u7a0b\u3002",
            summary_path=summary_path,
        )

    driver.get(_ap_url(DUTY_WORK_LOG_AP))
    time.sleep(1)
    _click_by_text_or_id(driver, ["_btnInsert"], ["\u65b0\u589e"])
    time.sleep(1.5)
    _click_by_text_or_id(driver, ["_btnReCallntman"], ["\u7531\u6848\u4ef6\u5e36\u5165"])
    _switch_to_window_containing(driver, "_txtSDATE")
    time.sleep(1.5)
    _save_artifacts(driver, output_dir, request.task_id, "duty_case_picker")
    return SeleniumRunResult(
        ok=True,
        status="case_picker_opened",
        detail="\u5df2\u5728\u672c\u6a5f Chrome \u958b\u5230\u6848\u4ef6\u8cc7\u6599\u67e5\u8a62\uff1b\u8acb\u4eba\u5de5\u78ba\u8a8d\u6848\u4ef6\u5f8c\u6309\u8a72\u5217\u300c\u9078\u64c7\u300d\u3002\u9078\u5b8c\u5f8c\u7a0b\u5f0f\u4e0b\u4e00\u6b65\u6703\u88dc\u586b\u52e4\u52d9\u9805\u76ee\u3001\u4e8b\u7531\u8207\u8655\u7406\u60c5\u5f62\u3002",
        summary_path=summary_path,
    )


def _prepare_duty_work_log_form(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest,
    output_dir: Path,
    summary_path: Path,
) -> SeleniumRunResult:
    if not _ensure_duty_login(driver):
        _save_artifacts(driver, output_dir, request.task_id, "duty_login")
        return SeleniumRunResult(
            ok=True,
            status="needs_duty_login",
            detail="已在公務電腦 worker Chrome 開啟消防勤務登入頁，但目前尚未登入；請在 worker GUI 手動登入一次。",
            summary_path=summary_path,
        )

    driver.get(_ap_url(DUTY_WORK_LOG_AP))
    time.sleep(1)
    _click_by_text_or_id(driver, ["_btnInsert"], ["\u65b0\u589e"])
    time.sleep(1.5)
    _click_by_text_or_id(driver, ["_btnReCallntman"], ["\u7531\u6848\u4ef6\u5e36\u5165"])
    _switch_to_window_containing(driver, "_txtSDATE")
    time.sleep(1.5)
    _set_case_query_date_range(driver, lookup_range="today")
    _click_query_if_present(driver)
    time.sleep(1)
    cases = _extract_all_emergency_cases(driver)
    case = _match_case_for_request(cases, request)
    if not case:
        _save_artifacts(driver, output_dir, request.task_id, "duty_case_picker")
        return SeleniumRunResult(
            ok=False,
            status="duty_case_not_found",
            detail=f"未在今日案件清單找到符合時間={request.case_time}、地址={request.case_address} 的案件；已保存查詢頁截圖。",
            summary_path=summary_path,
        )
    if not _click_case_choose(driver, case["case_id"]):
        _save_artifacts(driver, output_dir, request.task_id, "duty_case_picker")
        return SeleniumRunResult(
            ok=False,
            status="duty_case_choose_failed",
            detail=f"找到案件但無法按選擇：{case.get('category')} {case.get('address')}",
            summary_path=summary_path,
        )
    time.sleep(1.5)
    _switch_to_work_log_form_for_case(driver, case)
    fill_result = _fill_duty_work_log_values(driver, request)
    _save_artifacts(driver, output_dir, request.task_id, "duty_work_log_prefilled")
    if fill_result:
        detail = f"消防勤務工作紀錄已預填但有欄位未確認：{', '.join(fill_result)}。已保存截圖，不會自動儲存。"
        status = "duty_work_log_prefill_partial"
    else:
        save_result = _click_duty_work_log_save(driver)
        time.sleep(1.5)
        _save_artifacts(driver, output_dir, request.task_id, "duty_work_log_saved")
        if save_result.get("ok"):
            detail = "消防勤務工作紀錄已預填勤務項目、事由、處理情形並按下儲存。"
            status = "duty_work_log_saved"
        else:
            detail = f"消防勤務工作紀錄已預填，但儲存按鈕未成功點擊：{save_result.get('reason', 'unknown')}。"
            status = "duty_work_log_save_failed"
    return SeleniumRunResult(ok=True, status=status, detail=detail, summary_path=summary_path)


def _open_vehicle_mileage_page(driver: webdriver.Chrome, request: AmbulanceReturnRequest, output_dir: Path) -> str:
    driver.get(SITE_DEFINITIONS[0].url)
    time.sleep(1)
    username, password = _ppe_credentials()
    try:
        has_login = driver.execute_script("return !!document.getElementById('Account') && !!document.getElementById('Password');")
        if has_login and username and password:
            driver.find_element(By.ID, "Account").clear()
            driver.find_element(By.ID, "Account").send_keys(username)
            driver.find_element(By.ID, "Password").clear()
            driver.find_element(By.ID, "Password").send_keys(password)
            _click_ppe_login(driver)
            WebDriverWait(driver, 8).until(lambda current: not _is_ppe_login_page(current))
        if has_login and not (username and password):
            _save_artifacts(driver, output_dir, request.task_id, "vehicle_mileage")
            return "\u5df2\u958b\u555f\u8eca\u8f1b\u91cc\u7a0b\u767b\u5165\u9801\uff1b\u8acb\u5148\u767b\u5165 PPE \u7cfb\u7d71\u3002"
        if _is_ppe_login_page(driver):
            _save_artifacts(driver, output_dir, request.task_id, "vehicle_mileage")
            return "\u8eca\u8f1b\u91cc\u7a0b PPE \u81ea\u52d5\u767b\u5165\u672a\u901a\u904e\uff1b\u8acb\u624b\u52d5\u767b\u5165\u5f8c\u518d\u57f7\u884c\u3002"
        detail = _prepare_vehicle_mileage_form(driver, request)
        _save_artifacts(driver, output_dir, request.task_id, "vehicle_mileage")
    except WebDriverException:
        _save_artifacts(driver, output_dir, request.task_id, "vehicle_mileage_error")
        raise

    return detail


def _ppe_credentials() -> tuple[str, str]:
    username = os.getenv("PPE_ACCOUNT", "").strip() or os.getenv("DUTY_ACCOUNT", "").strip()
    password = os.getenv("PPE_PASSWORD", "").strip() or os.getenv("DUTY_PASSWORD", "").strip()
    if username and password:
        return username, password
    saved = load_duty_credential()
    if saved is None:
        return "", ""
    return saved.user_id, saved.password


def _click_ppe_login(driver: webdriver.Chrome) -> None:
    clicked = driver.execute_script(
        """
        const candidates = [
          document.getElementById('btnSubmit'),
          ...Array.from(document.querySelectorAll('button,input[type=submit],input[type=button]'))
        ].filter(Boolean);
        const target = candidates.find(el => {
          const text = [el.id, el.name, el.value, el.innerText, el.title].map(x => String(x || '')).join(' ');
          return /btnSubmit|確定|登入|Login|Submit/i.test(text);
        });
        if (!target) return false;
        target.click();
        return true;
        """
    )
    if not clicked:
        driver.find_element(By.ID, "Password").submit()


def _match_case_for_request(cases: list[dict[str, str]], request: AmbulanceReturnRequest) -> dict[str, str] | None:
    address = clean_case_address(request.case_address)
    case_time = str(request.case_time or "").strip()
    for case in cases:
        if case_time and case.get("case_time_hhmm") != case_time:
            continue
        if address and address not in clean_case_address(case.get("address", "")):
            continue
        return case
    for case in cases:
        if case_time and case.get("case_time_hhmm") == case_time:
            return case
    for case in cases:
        if address and address in clean_case_address(case.get("address", "")):
            return case
    return None


def _fill_duty_work_log_values(driver: webdriver.Chrome, request: AmbulanceReturnRequest) -> list[str]:
    status_text = f"1.{request.vehicle}:{request.driver}  2.{request.patient_summary}"
    item_missing = driver.execute_script(
        """
        const value = arguments[0];
        function writable(el) {
          if (!el || el.disabled || el.readOnly) return false;
          const tag = el.tagName;
          if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
          if (tag !== 'INPUT') return false;
          const type = String(el.type || 'text').toLowerCase();
          return ['text', 'number', 'search', 'tel', 'time'].includes(type);
        }
        function setValue(el, value) {
          if (!writable(el) || value === undefined || value === null || String(value) === '') return false;
          if (el.tagName === 'SELECT') {
            const option = Array.from(el.options || []).find(item => {
              const text = String(item.text || '').trim();
              const raw = String(item.value || '').trim();
              return text === value || text.includes(value) || raw === value;
            });
            if (!option) return false;
            el.value = option.value;
          } else {
            el.value = String(value);
          }
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }
        const selected = setValue(document.getElementById('_selList'), value);
        if (selected) {
          const reloadButton = document.getElementById('_btnChangeList');
          if (reloadButton) reloadButton.click();
        }
        return selected ? [] : ['勤務項目'];
        """,
        "救護",
    )
    if not item_missing:
        try:
            WebDriverWait(driver, 6).until(
                lambda current: len(current.find_elements(By.CSS_SELECTOR, "#_selList2 option")) > 1
            )
        except TimeoutException:
            time.sleep(1)

    reason_missing = driver.execute_script(
        """
        const value = arguments[0];
        function writable(el) {
          if (!el || el.disabled || el.readOnly) return false;
          const tag = el.tagName;
          if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
          if (tag !== 'INPUT') return false;
          const type = String(el.type || 'text').toLowerCase();
          return ['text', 'number', 'search', 'tel', 'time'].includes(type);
        }
        function setValue(el, value) {
          if (!writable(el) || value === undefined || value === null || String(value) === '') return false;
          if (el.tagName === 'SELECT') {
            const target = String(value || '').trim();
            const option = Array.from(el.options || []).find(item => {
              const text = String(item.text || '').trim();
              const raw = String(item.value || '').trim();
              return text === target || raw === target || text.includes(target);
            });
            if (!option) return false;
            el.value = option.value;
          } else {
            el.value = String(value);
          }
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }
        function controlsNear(labelText) {
          const normalizedLabel = labelText.replace(/\\s+/g, '');
          const controlsOf = root => Array.from(root.querySelectorAll('input, textarea, select')).filter(writable);
          const rows = Array.from(document.querySelectorAll('tr'));
          for (const row of rows) {
            const cells = Array.from(row.children);
            const labelIndex = cells.findIndex(cell => String(cell.innerText || '').replace(/\\s+/g, '').includes(normalizedLabel));
            if (labelIndex < 0) continue;
            return cells.slice(labelIndex + 1).flatMap(controlsOf);
          }
          return [];
        }
        function setNearby(label, value, preferTextarea = false) {
          const controls = controlsNear(label);
          const candidates = preferTextarea ? controls.filter(el => el.tagName === 'TEXTAREA') : controls;
          return candidates.some(el => setValue(el, value));
        }
        function setByOptionText(value) {
          return Array.from(document.querySelectorAll('select')).filter(writable).some(el => setValue(el, value));
        }
        const missing = [];
        if (value && !setValue(document.getElementById('_selList2'), value) && !setNearby('事由', value) && !setByOptionText(value)) missing.push('事由');
        return missing;
        """,
        request.case_reason,
    )
    if not reason_missing:
        time.sleep(1.2)
        try:
            WebDriverWait(driver, 6).until(
                lambda current: current.execute_script(
                    "return document.readyState === 'complete' && !!document.getElementById('_areStatus');"
                )
            )
        except TimeoutException:
            time.sleep(1)

    final_values = {
        "status": status_text,
        "return_line": request.return_time_description_line,
    }
    missing = driver.execute_script(
        """
        const values = arguments[0];
        function writable(el) {
          if (!el || el.disabled || el.readOnly) return false;
          const tag = el.tagName;
          if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
          if (tag !== 'INPUT') return false;
          const type = String(el.type || 'text').toLowerCase();
          return ['text', 'number', 'search', 'tel', 'time'].includes(type);
        }
        function setValue(el, value) {
          if (!writable(el) || value === undefined || value === null || String(value) === '') return false;
          el.value = String(value);
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }
        function patchReturnLine(value) {
          if (!value) return true;
          const el = document.getElementById('_areDescription');
          if (!writable(el)) return false;
          const current = String(el.value || '');
          const lines = current ? current.split(/\\r?\\n/) : [];
          const index = lines.findIndex(line => line.trim().startsWith('返隊時間:'));
          if (index >= 0) {
            const existing = String(lines[index] || '');
            const afterColon = existing.replace(/^\\s*返隊時間:\\s*/, '').trim();
            if (afterColon) return true;
            lines[index] = value;
          } else if (lines.length >= 2) {
            lines.splice(2, 0, value);
          } else {
            lines.push(value);
          }
          el.value = lines.join('\\n');
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }
        function controlsNear(labelText) {
          const normalizedLabel = labelText.replace(/\\s+/g, '');
          const controlsOf = root => Array.from(root.querySelectorAll('input, textarea, select')).filter(writable);
          const rows = Array.from(document.querySelectorAll('tr'));
          for (const row of rows) {
            const cells = Array.from(row.children);
            const labelIndex = cells.findIndex(cell => String(cell.innerText || '').replace(/\\s+/g, '').includes(normalizedLabel));
            if (labelIndex < 0) continue;
            return cells.slice(labelIndex + 1).flatMap(controlsOf);
          }
          return [];
        }
        const missing = [];
        if (!patchReturnLine(values.return_line)) missing.push('工作概述返隊時間');
        const controls = controlsNear('處理情形').filter(el => el.tagName === 'TEXTAREA');
        const ok = setValue(document.getElementById('_areStatus'), values.status) || controls.some(el => setValue(el, values.status));
        if (!ok) missing.push('處理情形');
        return missing;
        """,
        final_values,
    )
    all_missing = list(item_missing or []) + list(reason_missing or []) + list(missing or [])
    return [str(item) for item in all_missing]


def _click_duty_work_log_save(driver: webdriver.Chrome) -> dict[str, object]:
    result = driver.execute_script(
        """
        const controls = Array.from(document.querySelectorAll('input, button, a'));
        function visible(el) {
          if (!el || el.disabled) return false;
          if (String(el.type || '').toLowerCase() === 'hidden') return false;
          const style = window.getComputedStyle(el);
          return style.display !== 'none' && style.visibility !== 'hidden';
        }
        const target = controls.find(el => {
          if (!visible(el)) return false;
          const text = [el.id, el.name, el.value, el.title, el.innerText].map(x => String(x || '')).join(' ');
          return /_btnSave|儲存|存檔|Save/i.test(text);
        });
        if (!target) return {ok: false, reason: 'save control not found'};
        target.click();
        return {
          ok: true,
          id: target.id || '',
          name: target.name || '',
          value: target.value || '',
          text: target.innerText || ''
        };
        """
    )
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.accept()
        if isinstance(result, dict):
            result["alert"] = text
    except Exception:
        pass
    return dict(result or {"ok": False, "reason": "empty save result"})


def _is_ppe_login_page(driver: webdriver.Chrome) -> bool:
    return bool(
        driver.execute_script(
            "return !!document.getElementById('Account') && !!document.getElementById('Password');"
        )
    )


def _prepare_vehicle_mileage_form(driver: webdriver.Chrome, request: AmbulanceReturnRequest) -> str:
    _click_text_if_present(driver, ["\u8eca\u8f1b\u7ba1\u7406"])
    _click_text_if_present(driver, ["\u8eca\u8f1b\u4f7f\u7528\u7d00\u9304"])
    time.sleep(1)
    _click_text_if_present(driver, ["\u767b\u6253"])
    time.sleep(1)
    vehicle_label = VEHICLE_PPE_NAMES.get(request.vehicle, request.vehicle)
    _select_vehicle_record(driver, vehicle_label)
    time.sleep(1)
    latest_end_mileage = _extract_latest_end_mileage(driver)
    _add_vehicle_mileage_row(driver)
    time.sleep(1)

    start_date = _today_yyyymmdd()
    end_date = start_date
    start_mileage = latest_end_mileage
    end_mileage = _resolve_end_mileage(start_mileage, request.mileage)
    values = {
        "\u958b\u59cb\u65e5\u671f": start_date,
        "\u958b\u59cb\u6642\u9593": request.case_time,
        "\u7d50\u675f\u65e5\u671f": end_date,
        "\u7d50\u675f\u6642\u9593": request.return_time,
        "\u958b\u59cb\u91cc\u7a0b": start_mileage,
        "\u7d50\u675f\u91cc\u7a0b": end_mileage,
        "\u4e8b\u7531": "\u6551\u8b77",
        "\u524d\u5f80\u5730\u9ede": clean_case_address(request.case_address),
        "\u99d5\u99db\u4eba": request.driver,
    }
    _fill_vehicle_grid_values(driver, values)
    _assert_vehicle_mileage_values_present(driver, values)
    if os.getenv("SAVE_VEHICLE_MILEAGE", "false").strip().lower() in {"1", "true", "yes", "on"}:
        _click_text_if_present(driver, ["\u5132\u5b58"])
        alert_text = _accept_alert_if_present(driver)
        if alert_text:
            return f"\u5df2\u586b\u5beb\u8eca\u8f1b\u91cc\u7a0b\u3001\u6309\u4e0b\u5132\u5b58\u4e26\u78ba\u8a8d\u8996\u7a97\uff1a{alert_text}"
        return "\u5df2\u586b\u5beb\u8eca\u8f1b\u91cc\u7a0b\u4e26\u6309\u4e0b\u5132\u5b58\u3002"
    return "\u5df2\u586b\u5beb\u8eca\u8f1b\u91cc\u7a0b\uff0c\u672a\u6309\u5132\u5b58\u3002"


def _open_disinfection_page(driver: webdriver.Chrome, request: AmbulanceReturnRequest, output_dir: Path) -> str:
    current_url = driver.current_url.lower()
    if "emsdt.tyfd.gov.tw/emmweb" not in current_url:
        driver.get(SITE_DEFINITIONS[2].url)
        time.sleep(1.5)
    _save_artifacts(driver, output_dir, request.task_id, "disinfection_opened")
    if _is_disinfection_login_page(driver):
        raise WebDriverException("消毒系統需要重新登入或驗證碼；請先在 worker Chrome 手動登入一次後再執行。")
    detail = _prepare_disinfection_record(driver, request, output_dir)
    controls_path = _save_disinfection_probe(driver, output_dir, request.task_id)
    return f"{detail} 已保存頁面控制項：{controls_path}"


def _is_disinfection_login_page(driver: webdriver.Chrome) -> bool:
    url = driver.current_url.lower()
    if "login" in url or "signin" in url:
        return True
    source = driver.page_source
    login_markers = ["驗證碼", "帳號", "密碼", "登入"]
    return sum(1 for marker in login_markers if marker in source) >= 3


def _save_disinfection_probe(driver: webdriver.Chrome, output_dir: Path, task_id: str) -> Path:
    probe_dir = output_dir / "disinfection_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)

    html_path = probe_dir / f"{task_id}-after_login.html"
    png_path = probe_dir / f"{task_id}-after_login.png"
    controls_path = probe_dir / f"{task_id}-controls.json"

    html_path.write_text(driver.page_source, encoding="utf-8")
    driver.save_screenshot(str(png_path))

    controls = driver.execute_script(
        """
        const textOf = el => [
          el.innerText, el.value, el.title, el.name, el.id,
          el.placeholder, el.getAttribute('aria-label'), el.href
        ].map(x => String(x || '').trim()).filter(Boolean).join(' | ');
        return Array.from(document.querySelectorAll('a, button, input, select, textarea'))
          .filter(el => el.offsetParent !== null || el.tagName === 'INPUT')
          .map((el, index) => ({
            index,
            tag: el.tagName,
            type: el.type || '',
            id: el.id || '',
            name: el.name || '',
            value: el.value || '',
            text: textOf(el),
            href: el.href || ''
          }));
        """
    )
    controls_path.write_text(json.dumps(controls, ensure_ascii=False, indent=2), encoding="utf-8")
    return controls_path


def _prepare_disinfection_record(driver: webdriver.Chrome, request: AmbulanceReturnRequest, output_dir: Path) -> str:
    driver.switch_to.default_content()
    driver.get(_ems_ap_url(EMS_DISINFECTION_AP))
    time.sleep(1.5)
    _switch_to_disinfection_content_if_present(driver)
    _save_artifacts(driver, output_dir, request.task_id, "disinfection_entry")

    _set_disinfection_query_date(driver, _disinfection_query_date(request))
    if not _click_text_if_present(driver, ["\u67e5\u8a62"]):
        raise WebDriverException("missing disinfection query button")
    time.sleep(1.5)
    _save_artifacts(driver, output_dir, request.task_id, "disinfection_query")

    if not _open_disinfection_detail_for_case(driver, request.case_time):
        raise WebDriverException(f"missing disinfection detail for case time {request.case_time or 'empty'}")
    time.sleep(1.5)
    _save_artifacts(driver, output_dir, request.task_id, "disinfection_detail")

    selected_items = _effective_disinfection_items(request.disinfection_items)
    if selected_items:
        updated = _set_disinfection_item_statuses(driver, selected_items, "\u5df2\u9078\u53d6\u5340")
        if updated <= 0:
            raise WebDriverException(f"missing disinfection item selects: {request.disinfection_items_summary}")
    else:
        updated = 0
    _save_artifacts(driver, output_dir, request.task_id, "disinfection_prefilled")

    if os.getenv("SAVE_DISINFECTION_RECORD", "false").strip().lower() in {"1", "true", "yes", "on"}:
        if not _click_text_if_present(driver, ["\u5132\u5b58"]):
            raise WebDriverException("missing disinfection save button")
        alert_text = _accept_alert_if_present(driver)
        return f"disinfection items updated={updated}; saved. {alert_text}"
    return f"disinfection items updated={updated}; not saved."

def _switch_to_disinfection_content_if_present(driver: webdriver.Chrome) -> None:
    driver.switch_to.default_content()
    try:
        driver.switch_to.frame("R_content")
    except Exception:
        driver.switch_to.default_content()


def _ems_ap_url(ap_name: str) -> str:
    return (
        f"{EMS_BASE_URL}/ActionControlServlet?id=00&APname={ap_name}"
        f"&pushButton=load&nextAPname={ap_name}&_txtFirstEntry=TRUE"
    )

def _set_disinfection_query_date(driver: webdriver.Chrome, date_text: str) -> None:
    from_value = f"{date_text} 00:00:00"
    to_value = f"{date_text} 23:59:59"
    changed = driver.execute_script(
        """
        const fromValue = arguments[0];
        const toValue = arguments[1];
        function setDate(id, value) {
          const el = document.getElementById(id);
          if (!el) return false;
          el.value = value;
          el.setAttribute('realvalue', value);
          if ('realValue' in el) el.realValue = value;
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        }
        return [
          setDate('_txtFromDate', fromValue),
          setDate('_txtToDate', toValue)
        ];
        """,
        from_value,
        to_value,
    )
    if not all(bool(item) for item in changed):
        raise WebDriverException("missing disinfection date fields _txtFromDate/_txtToDate")


def _disinfection_query_date(request: AmbulanceReturnRequest) -> str:
    case_hhmm = normalize_hhmm_local(request.case_time)
    return_hhmm = normalize_hhmm_local(request.return_time)
    query_date = request.created_at
    if len(case_hhmm) == 4 and len(return_hhmm) == 4 and int(case_hhmm) > int(return_hhmm):
        query_date = query_date - timedelta(days=1)
    return query_date.strftime("%Y-%m-%d")

def _open_disinfection_detail_for_case(driver: webdriver.Chrome, case_time: str) -> bool:
    digits = normalize_hhmm_local(case_time)
    return bool(
        driver.execute_script(
            """
            const hhmm = arguments[0];
            const variants = hhmm && hhmm.length === 4 ? [hhmm, `${hhmm.slice(0,2)}:${hhmm.slice(2)}`] : [];
            const rows = Array.from(document.querySelectorAll('tr'));
            const row = rows.find(tr => {
              const text = tr.innerText || '';
              return variants.length === 0 || variants.some(v => text.includes(v));
            });
            if (!row) return false;
            const controls = Array.from(row.querySelectorAll('a, button, input[type=button], input[type=submit]'));
            const detail = controls.find(el => {
              const text = [el.innerText, el.value, el.title, el.getAttribute('aria-label')].map(x => String(x || '')).join(' ');
              return text.includes('明細');
            }) || controls[controls.length - 1];
            if (!detail) return false;
            detail.click();
            return true;
            """,
            digits,
        )
    )

def _effective_disinfection_items(items: list[str]) -> list[str]:
    legacy_default = ["\u6551\u8b77\u8eca\u9ad4", "\u64d4\u67b6\u5e8a"]
    cleaned = [item for item in items if item]
    if cleaned == legacy_default:
        return list(DEFAULT_DISINFECTION_ITEMS)
    return cleaned


def _set_disinfection_item_statuses(driver: webdriver.Chrome, items: list[str], status_text: str) -> int:
    item_ids = {
        '\u6551\u8b77\u8eca\u9ad4': 1,
        '\u64d4\u67b6\u5e8a': 2,
        '\u64d4\u67b6\u5e8a\u588a': 3,
        '\u5152\u7ae5\u64d4\u67b6\u56fa\u5b9a\u5668': 4,
        '\u5b30\u5152\u64d4\u67b6\u56fa\u5b9a\u5668': 5,
        '\u642c\u904b\u6905': 6,
        '\u56fa\u5b9a\u5f0f\u6c27\u6c23\u7d44': 7,
        '\u81ea\u52d5\u7d66\u6c27\u6a5f': 8,
        '\u651c\u5e36\u5f0f\u6c27\u6c23\u7d44(\u542b\u5167\u5bb9\u7269)': 9,
        '\u6025\u6551\u7bb1/\u6025\u6551\u5305': 10,
        '\u651c\u5e36\u5f0f\u62bd\u5438\u5668': 11,
        '\u9577\u80cc\u677f(\u542b\u982d\u90e8\u56fa\u5b9a\u5668)': 12,
        '\u93df\u5f0f\u64d4\u67b6(\u542b\u982d\u90e8\u56fa\u5b9a\u5668)': 13,
        '\u9aa8\u6298\u56fa\u5b9a\u677f': 14,
        '\u62bd\u6c23\u5f0f\u8b77\u6728': 15,
        '\u8ec0\u5e79\u56fa\u5b9a\u5668': 16,
        '\u8840\u6c27\u6fc3\u5ea6\u5206\u6790\u5100': 17,
        '\u9ad4\u6eab\u8a08': 18,
        '\u8840\u58d3\u8a08': 19,
        '\u8840\u7cd6\u6a5f': 20,
        '\u5fc3\u81df\u96fb\u64ca\u53bb\u986b\u5668': 21,
        '\u81ea\u52d5\u5fc3\u80ba\u5fa9\u7526\u6a5f': 22,
        '\u6210\u4eba\u7526\u9192\u7403': 23,
        '\u5152\u7ae5\u7526\u9192\u7403': 24,
        '\u5b30\u5152\u7526\u9192\u7403': 25,
        '\u6210\u4eba\u9838\u5708': 26,
        '\u5152\u7ae5\u9838\u5708': 27,
        '\u6bdb\u6bef/\u88ab\u5b50': 28,
        '\u88ab\u55ae': 29,
        '\u9ad8\u6551\u5305(\u542b\u5167\u5bb9\u7269)': 30,
        '\u5927\u91cf\u50b7\u75c5\u60a3\u4e8b\u4ef6\u5668\u6750\u5305(\u542b\u5167\u5bb9\u7269)': 31,
    }
    selected_ids = [item_ids[item] for item in items if item in item_ids]
    if not selected_ids:
        return 0
    return int(
        driver.execute_script(
            """
            const selectedIds = arguments[0];
            let updated = 0;
            for (const id of selectedIds) {
              const select = document.getElementById(`_selIVBALL_${id}`);
              if (!select) continue;
              select.value = '1';
              select.dispatchEvent(new Event('input', {bubbles: true}));
              select.dispatchEvent(new Event('change', {bubbles: true}));
              updated++;
            }
            return updated;
            """,
            selected_ids,
        )
    )

def normalize_hhmm_local(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:4] if len(digits) >= 4 else digits


def _accept_alert_if_present(driver: webdriver.Chrome, timeout: float = 4) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            alert = driver.switch_to.alert
            text = alert.text
            alert.accept()
            return text
        except Exception:
            time.sleep(0.2)
    return ""


def _click_text_if_present(driver: webdriver.Chrome, texts: list[str]) -> bool:
    for text in texts:
        controls = driver.find_elements(
            By.XPATH,
            f"//button[contains(normalize-space(), '{text}')] | //a[contains(normalize-space(), '{text}')] | //input[contains(@value, '{text}')]",
        )
        for control in controls:
            if control.is_displayed() and control.is_enabled():
                control.click()
                time.sleep(1)
                return True

    script = """
    const texts = arguments[0];
    const controls = Array.from(document.querySelectorAll('a, button, input[type=button], input[type=submit]'))
      .filter(el => el.offsetParent !== null || el.tagName === 'INPUT');
    const target = controls.find(el => {
      const haystack = [el.innerText, el.value, el.title, el.getAttribute('aria-label')].map(x => String(x || '')).join(' ');
      return texts.some(text => haystack.includes(text));
    });
    if (!target) return false;
    target.click();
    return true;
    """
    return bool(driver.execute_script(script, texts))


def _select_vehicle_record(driver: webdriver.Chrome, vehicle_label: str) -> None:
    buttons = driver.find_elements(
        By.XPATH,
        f"//tr[.//td[contains(normalize-space(), '{vehicle_label}')]]//button[contains(normalize-space(), '\u767b\u6253')]",
    )
    if buttons:
        buttons[0].click()
        time.sleep(2)
        return

    script = """
    const vehicleLabel = arguments[0];
    const rows = Array.from(document.querySelectorAll('tr, .row, [role=row]'));
    const row = rows.find(item => item.innerText && item.innerText.includes(vehicleLabel));
    if (row) {
      const controls = Array.from(row.querySelectorAll('input, button, a'));
      const control = controls.find(el => {
        const text = [el.value, el.innerText, el.title].map(x => String(x || '')).join(' ');
        return text.includes('登打') || text.includes('選擇') || text.includes('明細') || el.type === 'radio';
      }) || row;
      control.click();
      return true;
    }
    const select = Array.from(document.querySelectorAll('select')).find(el => {
      return Array.from(el.options || []).some(option => option.text.includes(vehicleLabel));
    });
    if (select) {
      const option = Array.from(select.options).find(option => option.text.includes(vehicleLabel));
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    return false;
    """
    if not driver.execute_script(script, vehicle_label):
        raise WebDriverException(f"vehicle not found: {vehicle_label}")


def _extract_latest_end_mileage(driver: webdriver.Chrome) -> str:
    script = """
    const grid = window.$ && $("#grid").data("kendoGrid");
    if (grid) {
      const values = grid.dataSource.data().map(item => Number(item.EndMileage || 0)).filter(value => value > 0);
      if (values.length) return String(Math.max(...values));
    }
    const textOf = el => String(el && (el.innerText || el.value || el.textContent) || '').trim();
    const rows = Array.from(document.querySelectorAll('tr')).map(row => {
      return Array.from(row.querySelectorAll('td, th')).map(textOf).filter(Boolean);
    }).filter(cells => cells.length);
    for (const cells of rows) {
      const joined = cells.join(' ');
      if (!joined.includes('結束') && !joined.includes('里程')) continue;
      const nums = joined.match(/\\d{4,7}/g);
      if (nums && nums.length) return nums[nums.length - 1];
    }
    const nums = document.body.innerText.match(/\\d{4,7}/g);
    return nums && nums.length ? nums[nums.length - 1] : '';
    """
    value = str(driver.execute_script(script) or "").strip()
    if not value:
        raise WebDriverException("latest end mileage not found")
    return value


def _add_vehicle_mileage_row(driver: webdriver.Chrome) -> None:
    added = driver.execute_script(
        """
        if (typeof addRow === 'function') {
          addRow();
          return true;
        }
        return false;
        """
    )
    if not added:
        if not _click_text_if_present(driver, ["\u65b0\u589e"]):
            raise WebDriverException("add vehicle mileage row button not found")


def _resolve_end_mileage(start_mileage: str, raw_mileage: str) -> str:
    raw = str(raw_mileage or "").strip()
    if raw.startswith("+"):
        return str(int(start_mileage) + int(raw[1:]))
    return raw


def _fill_vehicle_grid_values(driver: webdriver.Chrome, values: dict[str, str]) -> None:
    missing = driver.execute_script(
        """
        const values = arguments[0];
        const grid = window.$ && $("#grid").data("kendoGrid");
        if (!grid) return ['grid'];
        const rows = grid.dataSource.data();
        if (!rows.length) return ['newRow'];
        const row = rows[0];
        const driverName = values['駕駛人'] || '';
        let driverId = row.Driver || null;
        if (Array.isArray(window.driverList)) {
          const driver = window.driverList.find(item => JSON.stringify(item).includes(driverName));
          if (driver) {
            driverId = driver.Id ?? driver.Value ?? driver.UserId ?? driver.EmpId ?? driver.Code ?? driver.Driver;
          }
        }
        const pairs = {
          StartDay: values['開始日期'],
          StartTime: values['開始時間'],
          EndDay: values['結束日期'],
          EndTime: values['結束時間'],
          StartMileage: Number(values['開始里程']),
          EndMileage: Number(values['結束里程']),
          Mileage: Number(values['結束里程']) - Number(values['開始里程']),
          Reason: values['事由'],
          Destination: values['前往地點'],
          DeptNo: row.DeptNo || '3012',
          DeptName: '新坡分隊',
          Driver: driverId,
          DriverName: driverName,
        };
        for (const [key, value] of Object.entries(pairs)) {
          if (value !== undefined && value !== null && value !== '') row.set(key, value);
        }
        grid.refresh();
        const missing = [];
        for (const key of ['StartTime', 'EndTime', 'EndMileage', 'Reason', 'Destination', 'DriverName']) {
          if (!row.get(key)) missing.push(key);
        }
        return missing;
        """,
        values,
    )
    if missing:
        raise WebDriverException(f"vehicle mileage grid values not filled: {missing}")


def _fill_form_by_labels(driver: webdriver.Chrome, values: dict[str, str]) -> None:
    script = """
    const values = arguments[0];
    function writable(el) {
      return el && !el.disabled && !el.readOnly && ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
    }
    function setValue(el, value) {
      if (!writable(el) || value === undefined || value === null || String(value) === '') return false;
      if (el.tagName === 'SELECT') {
        const option = Array.from(el.options || []).find(item => item.text.includes(value) || item.value === value);
        if (!option) return false;
        el.value = option.value;
      } else {
        el.value = String(value);
      }
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    function findControl(labelText) {
      const labels = Array.from(document.querySelectorAll('label, td, th, div, span'));
      const label = labels.find(el => (el.innerText || '').replace(/\\s+/g, '').includes(labelText));
      if (!label) return null;
      const scoped = Array.from(label.querySelectorAll('input, textarea, select')).find(writable);
      if (scoped) return scoped;
      let node = label.nextElementSibling;
      for (let i = 0; node && i < 4; i++, node = node.nextElementSibling) {
        const direct = writable(node) ? node : Array.from(node.querySelectorAll?.('input, textarea, select') || []).find(writable);
        if (direct) return direct;
      }
      const row = label.closest('tr, .form-group, .row');
      if (row) {
        const inRow = Array.from(row.querySelectorAll('input, textarea, select')).find(writable);
        if (inRow) return inRow;
      }
      return null;
    }
    const missing = [];
    for (const [label, value] of Object.entries(values)) {
      const control = findControl(label);
      if (!setValue(control, value)) missing.push(label);
    }
    return missing;
    """
    missing = driver.execute_script(script, values)
    if missing:
        raise WebDriverException(f"vehicle mileage fields not found: {missing}")


def _assert_vehicle_mileage_values_present(driver: webdriver.Chrome, values: dict[str, str]) -> None:
    expected = [values[key] for key in ("\u958b\u59cb\u6642\u9593", "\u7d50\u675f\u6642\u9593", "\u7d50\u675f\u91cc\u7a0b") if values.get(key)]
    script = """
    const expected = arguments[0];
    const grid = window.$ && $("#grid").data("kendoGrid");
    if (grid && grid.dataSource.data().length) {
      const row = grid.dataSource.data()[0];
      const values = [row.StartTime, row.EndTime, String(row.EndMileage || '')].map(item => String(item || ''));
      return expected.filter(item => !values.includes(String(item)));
    }
    const values = Array.from(document.querySelectorAll('input, textarea, select')).map(el => String(el.value || ''));
    return expected.filter(item => !values.includes(String(item)));
    """
    missing = driver.execute_script(script, expected)
    if missing:
        raise WebDriverException(f"vehicle mileage values not filled: {missing}")


def _today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def _open_case_query(driver: webdriver.Chrome, lookup_range: str = "6h") -> None:
    driver.get(_ap_url(DUTY_WORK_LOG_AP))
    time.sleep(1)
    _click_by_text_or_id(driver, ["_btnInsert"], ["\u65b0\u589e"])
    time.sleep(1)
    _click_by_text_or_id(driver, ["_btnReCallntman"], ["\u7531\u6848\u4ef6\u5e36\u5165"])
    _switch_to_window_containing(driver, "_txtSDATE")
    time.sleep(1.5)
    _set_case_query_date_range(driver, lookup_range=lookup_range)
    _click_query_if_present(driver)
    time.sleep(1)


def _ensure_duty_login(driver: webdriver.Chrome) -> bool:
    driver.get(f"{BASE_URL}/login119")
    time.sleep(1)
    if _looks_logged_in(driver):
        return True
    credential = load_duty_credential()
    if credential is None:
        return False
    try:
        wait = WebDriverWait(driver, 10)
        username = wait.until(EC.presence_of_element_located((By.ID, "_txtUsername")))
        password = driver.find_element(By.ID, "_txtPassword")
        username.clear()
        username.send_keys(credential.user_id)
        password.clear()
        password.send_keys(credential.password)
        driver.find_element(By.NAME, "login").click()
        deadline = time.time() + 8
        while time.time() < deadline:
            if _looks_logged_in(driver):
                return True
            if _login_error_text(driver):
                return False
            time.sleep(1)
        driver.get(_ap_url(DUTY_WORK_LOG_AP))
        time.sleep(1.5)
        if _login_form_present(driver):
            return False
        return True
    except (TimeoutException, WebDriverException):
        return False


def _login_form_present(driver: webdriver.Chrome) -> bool:
    try:
        return bool(
            driver.execute_script(
                "return !!document.getElementById('_txtUsername') && !!document.getElementById('_txtPassword');"
            )
        )
    except WebDriverException:
        return False


def _login_error_text(driver: webdriver.Chrome) -> str:
    try:
        text = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
    except WebDriverException:
        return ""
    markers = [
        "帳號密碼有誤",
        "尚未申請帳號權限",
    ]
    return text if any(marker in text for marker in markers) else ""


def _looks_logged_in(driver: webdriver.Chrome) -> bool:
    try:
        text = driver.execute_script(
            """
            const parts = [];
            function collect(win) {
              try {
                if (win.document && win.document.body) parts.push(win.document.body.innerText || '');
                for (let i = 0; i < win.frames.length; i++) collect(win.frames[i]);
              } catch (e) {}
            }
            collect(window);
            return parts.join('\\n');
            """
        )
    except WebDriverException:
        return False
    return "\u4e3b\u8981\u4f5c\u696d\u9078\u55ae" in text or "\u6d88\u9632\u52e4\u52d9\u7ba1\u7406\u7cfb\u7d71" in text


def _ap_url(ap_name: str) -> str:
    return (
        f"{BASE_URL}/ActionControlServlet?id=00&APname={ap_name}"
        f"&pushButton=load&nextAPname={ap_name}&_txtFirstEntry=TRUE"
    )


def _click_by_text_or_id(driver: webdriver.Chrome, ids: list[str], texts: list[str]) -> None:
    script = """
    const ids = arguments[0];
    const texts = arguments[1];
    function isClickable(el) {
      if (!el || el.disabled) return false;
      if (String(el.type || '').toLowerCase() === 'hidden') return false;
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      return true;
    }
    for (const id of ids) {
      const matches = Array.from(document.querySelectorAll(`[id="${CSS.escape(id)}"]`));
      const el = matches.find(isClickable);
      if (el) { el.click(); return {ok: true, via: id}; }
    }
    const controls = Array.from(document.querySelectorAll('input, button, a'));
    const target = controls.find(el => {
      if (!isClickable(el)) return false;
      const haystack = [el.value, el.innerText, el.title, el.id, el.name].map(x => String(x || '')).join(' ');
      return texts.some(text => haystack.includes(text));
    });
    if (target) { target.click(); return {ok: true, via: target.id || target.value || target.innerText}; }
    return {ok: false};
    """
    result = driver.execute_script(script, ids, texts)
    if not result or not result.get("ok"):
        raise WebDriverException(f"control not found: {ids} {texts}")


def _switch_to_newest_window(driver: webdriver.Chrome) -> None:
    latest_handle = driver.current_window_handle
    deadline = time.time() + 5
    while time.time() < deadline:
        handles = driver.window_handles
        if handles:
            latest_handle = handles[-1]
        if len(handles) > 1:
            break
        time.sleep(0.2)
    driver.switch_to.window(latest_handle)


def _switch_to_window_containing(driver: webdriver.Chrome, text: str) -> None:
    if _try_switch_to_window_containing(driver, text):
        return
    raise WebDriverException(f"window containing text not found: {text}")


def _try_switch_to_window_containing(driver: webdriver.Chrome, text: str) -> bool:
    for handle in driver.window_handles:
        driver.switch_to.window(handle)
        try:
            if text in driver.page_source:
                return True
        except WebDriverException:
            continue
    return False


def _switch_to_work_log_form(driver: webdriver.Chrome) -> None:
    deadline = time.time() + 8
    while time.time() < deadline:
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            try:
                if driver.execute_script("return !!document.getElementById('_areMan') || !!document.getElementById('_areStatus');"):
                    return
            except WebDriverException:
                continue
        time.sleep(0.3)
    raise WebDriverException("work log form not found after case choose")


def _switch_to_work_log_form_for_case(driver: webdriver.Chrome, case: dict[str, str]) -> None:
    address = case.get("address", "")
    return_time = case.get("return_time", "")
    deadline = time.time() + 8
    while time.time() < deadline:
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            try:
                matched = driver.execute_script(
                    """
                    const address = arguments[0];
                    const returnTime = arguments[1];
                    const description = String(document.getElementById('_areDescription')?.value || '');
                    const hasForm = !!document.getElementById('_areMan') || !!document.getElementById('_areStatus');
                    if (!hasForm) return false;
                    if (address && description.includes(address)) return true;
                    if (returnTime && description.includes(returnTime)) return true;
                    return false;
                    """,
                    address,
                    return_time,
                )
                if matched:
                    return
            except WebDriverException:
                continue
        time.sleep(0.3)
    raise WebDriverException("matching work log form not found after case choose")


def _click_case_choose(driver: webdriver.Chrome, case_id: str) -> bool:
    script = """
    const caseId = arguments[0];
    const rows = Array.from(document.querySelectorAll('tr'));
    for (const row of rows) {
      if (!row.innerText.includes(caseId)) continue;
      const controls = Array.from(row.querySelectorAll('input, button, a'));
      const target = controls.find(el => {
        const text = [el.value, el.innerText, el.title, el.id, el.name].map(x => String(x || '')).join(' ');
        const onclick = String(el.getAttribute('onclick') || '');
        return text.includes('選擇') && onclick.includes(caseId);
      });
      if (target) { target.click(); return true; }
    }
    return false;
    """
    return bool(driver.execute_script(script, case_id))


def _extract_selected_case_form(driver: webdriver.Chrome) -> dict[str, object]:
    script = """
    function valueOf(id) {
      const el = document.getElementById(id);
      return el ? String(el.value || el.innerText || '').trim() : '';
    }
    function selectedText(id) {
      const el = document.getElementById(id);
      if (!el || typeof el.selectedIndex !== 'number' || el.selectedIndex < 0) return valueOf(id);
      return String(el.options[el.selectedIndex].text || el.value || '').trim();
    }
    const rawPeople = valueOf('_areMan');
    const rawHiddenPeople = valueOf('_hidManId');
    const peopleSource = rawPeople || rawHiddenPeople;
    const people = peopleSource.split(/[\\n,，、\\s]+/)
      .map(x => x.trim())
      .filter(x => x && !/^\\d+$/.test(x))
      .filter(x => !/^[A-Z]\\d+/i.test(x) && !/^tyfd\\d+/i.test(x));
    return {
      case_date: valueOf('_txtDATE'),
      case_time_h: selectedText('_selTIMEH'),
      case_time_m: selectedText('_selTIMEM'),
      description: valueOf('_areDescription'),
      personnel_raw: rawPeople,
      personnel_hidden_raw: rawHiddenPeople,
      personnel: Array.from(new Set(people)),
    };
    """
    result = driver.execute_script(script)
    return dict(result or {})


def _attach_case_form_details(
    driver: webdriver.Chrome,
    cases: list[dict[str, str]],
    artifacts_dir: Path,
    previous_cases: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, str]]:
    previous_cases = previous_cases or {}
    for case in cases:
        case_id = case.get("case_id", "")
        if not case_id:
            continue
        if case.get("personnel") or case.get("personnel_raw") or case.get("personnel_hidden_raw"):
            case["detail_status"] = "case_detail_from_choose_data"
            continue
        previous = previous_cases.get(case_id)
        if previous and (
            previous.get("personnel") or previous.get("personnel_raw") or previous.get("personnel_hidden_raw")
        ):
            for key in ("case_date", "case_time_h", "case_time_m", "description", "personnel", "personnel_raw", "personnel_hidden_raw"):
                if key in previous:
                    case[key] = previous[key]
            case["detail_status"] = "case_detail_cached"
            continue
        try:
            if not _try_switch_to_window_containing(driver, case_id):
                _open_case_query(driver)
            if not _try_switch_to_window_containing(driver, case_id):
                continue
            if not _click_case_choose(driver, case_id):
                continue
            time.sleep(1.5)
            _switch_to_work_log_form_for_case(driver, case)
            selected = _extract_selected_case_form(driver)
            case.update(selected)
            _save_artifacts(driver, artifacts_dir / "selenium", case_id, "selected_case")
        except WebDriverException:
            case["detail_status"] = "case_detail_failed"
    return cases


def _click_query_if_present(driver: webdriver.Chrome) -> None:
    try:
        _click_by_text_or_id(driver, ["_btnQuery"], ["\u67e5\u8a62"])
    except WebDriverException:
        return


def _set_case_query_date_range(driver: webdriver.Chrome, lookup_range: str = "6h") -> None:
    end_at = datetime.now()
    if lookup_range == "today":
        start_at = end_at.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start_at = end_at - timedelta(hours=6)
    start_date = _roc_date(start_at)
    end_date = _roc_date(end_at)
    driver.execute_script(
        """
        const startDate = arguments[0];
        const endDate = arguments[1];
        const startHour = arguments[2];
        const startMinute = arguments[3];
        const endHour = arguments[4];
        const endMinute = arguments[5];
        const pairs = [
          ['_txtSDATE', startDate],
          ['_txtEDATE', endDate],
          ['_selSTIMEH', startHour],
          ['_selSTIMEM', startMinute],
          ['_selETIMEH', endHour],
          ['_selETIMEM', endMinute],
        ];
        for (const [id, value] of pairs) {
          const el = document.getElementById(id);
          if (!el) continue;
          el.value = value;
          el.dispatchEvent(new Event('change', { bubbles: true }));
        }
        """,
        start_date,
        end_date,
        f"{start_at.hour:02d}",
        f"{start_at.minute:02d}",
        f"{end_at.hour:02d}",
        f"{end_at.minute:02d}",
    )


def _roc_date(value: datetime) -> str:
    return f"{value.year - 1911:03d}{value.month:02d}{value.day:02d}"


def _lookup_range_label(lookup_range: str) -> str:
    if lookup_range == "today":
        return "\u4eca\u5929"
    return "\u6700\u8fd1 6 \u5c0f\u6642"


def _extract_emergency_cases(driver: webdriver.Chrome) -> list[dict[str, str]]:
    script = """
    function textOf(el) {
      return (el && (el.innerText || el.value || el.textContent) || '').trim().replace(/\\s+/g, ' ');
    }
    function hhmm(value) {
      const match = String(value || '').match(/(\\d{1,2}):(\\d{2})/);
      if (!match) return '';
      return match[1].padStart(2, '0') + match[2];
    }
    const cases = [];
    const rows = Array.from(document.querySelectorAll('tr'));
    for (let rowIndex = 0; rowIndex < rows.length; rowIndex++) {
      const row = rows[rowIndex];
      const cells = Array.from(row.querySelectorAll('td, th')).map(textOf).filter(Boolean);
      const joined = cells.join(' ');
      if (!joined.includes('緊急救護')) continue;
      const caseId = cells[0] || '';
      if (!/^\\d{17}$/.test(caseId)) continue;
      const choose = Array.from(row.querySelectorAll('input, button, a')).find(el => {
        const haystack = [el.value, el.innerText, el.title, el.id, el.name].map(x => String(x || '')).join(' ');
        return haystack.includes('選擇');
      });
      const chooseDataMatch = String(choose ? (choose.getAttribute('onclick') || '') : '').match(/choose\\('([\\s\\S]*)'\\)/);
      const chooseParts = chooseDataMatch ? chooseDataMatch[1].split('(^w^)') : [];
      const category = cells.find(cell => cell.includes('緊急救護')) || '';
      if (!category.startsWith('緊急救護')) continue;
      const reason = category.includes('-') ? category.split('-').slice(1).join('-').trim() : '';
      const personnelRaw = chooseParts[34] || '';
      cases.push({
        row_index: String(rowIndex),
        case_id: caseId,
        report_time: cells[1] || '',
        return_time: cells[2] || '',
        case_time_hhmm: hhmm(cells[1] || ''),
        return_time_hhmm: hhmm(cells[2] || ''),
        category,
        reason,
        address: cells[4] || '',
        choose_id: choose ? (choose.id || '') : '',
        choose_name: choose ? (choose.name || '') : '',
        case_date: chooseParts[1] || '',
        case_time_h: chooseParts[2] || '',
        case_time_m: chooseParts[3] || '',
        description: chooseParts.length ? ['119案件', chooseParts[5] || '', `返隊時間:${chooseParts[35] || ''}`, `地點:${chooseParts[8] || ''}`].join('\\n') : '',
        personnel_raw: personnelRaw,
        personnel_hidden_raw: chooseParts[33] || '',
        personnel: personnelRaw ? Array.from(new Set(personnelRaw.split(/[\\n,，、\\s]+/).map(x => x.trim()).filter(Boolean))) : [],
      });
    }
    return cases;
    """
    cases = driver.execute_script(script)
    if not isinstance(cases, list):
        return []
    normalized = []
    for item in cases:
        row = {}
        for key, value in dict(item).items():
            if isinstance(value, list):
                row[str(key)] = [str(part or "") for part in value]
            else:
                row[str(key)] = str(value or "")
        normalized.append(row)
    return normalized


def _extract_all_emergency_cases(driver: webdriver.Chrome) -> list[dict[str, str]]:
    seen: set[str] = set()
    results: list[dict[str, str]] = []
    for _ in range(5):
        for item in _extract_emergency_cases(driver):
            case_id = item.get("case_id", "")
            if case_id and case_id not in seen:
                seen.add(case_id)
                results.append(item)
        if not _click_next_page_if_present(driver):
            break
        time.sleep(1)
    return results


def _click_next_page_if_present(driver: webdriver.Chrome) -> bool:
    script = """
    const controls = Array.from(document.querySelectorAll('input, button, a'));
    const target = controls.find(el => {
      const text = [el.value, el.innerText, el.title].map(x => String(x || '')).join(' ');
      const disabled = el.disabled || String(el.className || '').includes('disabled');
      return !disabled && text.includes('下一頁');
    });
    if (!target) return false;
    target.click();
    return true;
    """
    try:
        return bool(driver.execute_script(script))
    except WebDriverException:
        return False


def _save_artifacts(driver: webdriver.Chrome, output_dir: Path, task_id: str, site_key: str) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{task_id}-{site_key}.html").write_text(driver.page_source, encoding="utf-8", errors="replace")
        driver.save_screenshot(str(output_dir / f"{task_id}-{site_key}.png"))
    except WebDriverException:
        return


def _task_text(request: AmbulanceReturnRequest) -> str:
    lines = [
        "\u6551\u8b77\u56de\u7a0b\u4efb\u52d9",
        "",
        request.summary,
        "",
        "\u8655\u7406\u60c5\u5f62",
        request.duty_status_text,
        "",
        "\u672c\u6a5f\u64cd\u4f5c\u6d41\u7a0b",
        "- \u6d88\u9632\u52e4\u52d9\u7ba1\u7406\u7cfb\u7d71",
        "- \u5de5\u4f5c\u7d00\u9304\u7c3f",
        "- \u65b0\u589e",
        "- \u7531\u6848\u4ef6\u5e36\u5165",
        "- \u4eba\u5de5\u9078\u64c7\u6848\u4ef6",
        "",
        "\u7b2c\u4e00\u7248\u4e0d\u6703\u81ea\u52d5\u6309\u6700\u5f8c\u5132\u5b58\u3002",
    ]
    lines.extend(f"- {site.name}: {site.url}" for site in SITE_DEFINITIONS)
    return "\n".join(lines)
