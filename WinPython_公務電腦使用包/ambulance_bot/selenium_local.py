from __future__ import annotations

import errno
import os
import json
import re
import shlex
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
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .adapters import SITE_DEFINITION_BY_KEY, SITE_DEFINITIONS
from .chrome_startup import (
    ChromeStartTimeoutError,
    _worker_user_data_paths,
    cleanup_worker_chrome_residue,
    create_webdriver_chrome_with_timeout,
    schedule_driver_auto_close,
)
from .duty_credentials import (
    DutyCredential,
    load_duty_credential,
    load_recent_synced_duty_credential,
    load_synced_worker_credential,
    update_saved_credential_id_number,
)
from .models import DEFAULT_DISINFECTION_ITEMS, AmbulanceReturnRequest, clean_case_address, vehicle_ppe_names
from .profile_paths import cleanup_runtime_profiles_for_startup_failure, cleanup_stale_runtime_profiles, runtime_profile_dir, runtime_profile_root
from .window_layout import apply_tile


BASE_URL = "https://dutymgt.tyfd.gov.tw/tyfd119"
EMS_BASE_URL = "https://emsdt.tyfd.gov.tw/EmmWeb"
EMS_DISINFECTION_AP = "wap119.RPS64101014"
DUTY_WORK_LOG_AP = "wap119.RPS04060"
CASE_LOOKUP_DEBUGGER_PORT = 9223
_SELENIUM_SESSION_LOCK = threading.Lock()
_GENERATED_SELENIUM_PROFILE_NAMES = {
    "chrome_profile",
    "case_lookup_profile",
    "duty_work_log_profile",
    "vehicle_mileage_profile",
    "fuel_record_profile",
    "consumables_profile",
    "disinfection_profile",
    "fuel_record_probe",
    "probe_vehicle_delete_location",
}
_GENERATED_SELENIUM_PROFILE_PREFIXES = (
    "case_lookup_profile_",
    "duty_work_log_profile_",
    "vehicle_mileage_profile_",
    "fuel_record_profile_",
    "consumables_profile_",
    "disinfection_profile_",
    "acs_login_test_",
)
_CHROME_PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
_PPE_OPTION_NAME_FIELDS = ("Text", "Name", "DriverName", "UserName", "EmpName")
_PPE_OPTION_ID_FIELDS = ("Value", "Id", "UserId", "EmpId", "Code", "Driver")


def _normalize_ppe_option_name(value: object) -> str:
    return " ".join(str(value or "").split())


def _ppe_option_records_from_script(script_text: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for match in re.finditer(r"\{[^{}]*\}", str(script_text or "")):
        fragment = match.group(0)
        if not any(f'"{field}"' in fragment for field in _PPE_OPTION_NAME_FIELDS):
            continue
        if not any(f'"{field}"' in fragment for field in _PPE_OPTION_ID_FIELDS):
            continue
        try:
            record = json.loads(fragment)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _ppe_option_name(record: dict[str, object]) -> str:
    for field in _PPE_OPTION_NAME_FIELDS:
        value = _normalize_ppe_option_name(record.get(field))
        if value:
            return value
    return ""


def _ppe_option_id(record: dict[str, object]) -> str:
    for field in _PPE_OPTION_ID_FIELDS:
        value = str(record.get(field) or "").strip()
        if value and value != "0":
            return value
    return ""


def _ppe_option_value(options: object, requested_name: str) -> str:
    expected = _normalize_ppe_option_name(requested_name)
    if not expected or not isinstance(options, list):
        return ""
    for item in options:
        if not isinstance(item, dict) or _ppe_option_name(item) != expected:
            continue
        return _ppe_option_id(item)
    return ""


def _ppe_option_names(options: object, limit: int = 8) -> list[str]:
    names: list[str] = []
    if not isinstance(options, list) or limit <= 0:
        return names
    for item in options:
        if not isinstance(item, dict):
            continue
        name = _ppe_option_name(item)
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


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
class SyncedCredentialIdLookupResult:
    ok: bool
    status: str
    detail: str
    id_number: str = ""
    output_path: Path | None = None


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


def _env_enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _save_vehicle_mileage_enabled() -> bool:
    return _env_enabled("SAVE_VEHICLE_MILEAGE", default="true")


def _save_fuel_record_enabled() -> bool:
    return _env_enabled("SAVE_FUEL_RECORD", default="true")


def _save_duty_work_log_enabled() -> bool:
    return _env_enabled("SAVE_DUTY_WORK_LOG", default="true")


def _save_disinfection_record_enabled() -> bool:
    return _env_enabled("SAVE_DISINFECTION_RECORD", default="true")


def _save_disinfection_probe_enabled() -> bool:
    return _env_enabled("SAVE_DISINFECTION_PROBE", default="false")


def run_local_selenium_task(
    request: AmbulanceReturnRequest,
    artifacts_dir: Path,
    profile_name: str = "duty_work_log_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
) -> SeleniumRunResult:
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
        if use_session_lock:
            lock_acquired = _acquire_selenium_session(f"task {request.task_id}")
        print(f"[task] creating selenium driver for {request.task_id}", flush=True)
        if debugger_port is None and not force_new_driver:
            debugger_port = int(os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223"))
        driver = _create_driver(
            artifacts_dir,
            profile_name=profile_name,
            debugger_port=debugger_port,
            attach_existing=not force_new_driver,
        )
        apply_tile(driver, tile_name)
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


def run_vehicle_mileage_task(
    request: AmbulanceReturnRequest,
    artifacts_dir: Path,
    existing_driver: webdriver.Chrome | None = None,
    profile_name: str = "vehicle_mileage_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
    update_context: dict[str, object] | None = None,
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
        if use_session_lock:
            lock_acquired = _acquire_selenium_session(f"vehicle_mileage {request.task_id}")
        if driver is None:
            if debugger_port is None and not force_new_driver:
                debugger_port = int(os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223"))
            driver = _create_driver(
                artifacts_dir,
                profile_name=profile_name,
                debugger_port=debugger_port,
                attach_existing=not force_new_driver,
            )
        apply_tile(driver, tile_name)
        _set_window_size_if_enabled(driver, "vehicle_mileage")
        driver.implicitly_wait(2)
        detail = _open_vehicle_mileage_page(driver, request, output_dir, update_context=update_context)
        status = "vehicle_mileage_saved" if _save_vehicle_mileage_enabled() else "vehicle_mileage_prefilled"
        return SeleniumRunResult(True, status, detail, summary_path)
    except Exception as exc:
        if driver is not None:
            _save_artifacts(driver, output_dir, request.task_id, "vehicle_mileage_error")
        return SeleniumRunResult(False, "vehicle_mileage_failed", f"車輛里程操作失敗：{exc}", summary_path)
    finally:
        if owns_driver and not keep_browser_open:
            _quit_driver(driver)
        if lock_acquired:
            _release_selenium_session(f"vehicle_mileage {request.task_id}")


def run_fuel_record_task(
    request: AmbulanceReturnRequest,
    artifacts_dir: Path,
    existing_driver: webdriver.Chrome | None = None,
    profile_name: str = "fuel_record_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
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
        if use_session_lock:
            lock_acquired = _acquire_selenium_session(f"fuel_record {request.task_id}")
        if driver is None:
            if debugger_port is None and not force_new_driver:
                debugger_port = int(os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223"))
            driver = _create_driver(
                artifacts_dir,
                profile_name=profile_name,
                debugger_port=debugger_port,
                attach_existing=not force_new_driver,
            )
        apply_tile(driver, tile_name)
        _set_window_size_if_enabled(driver, "fuel_record")
        driver.implicitly_wait(2)
        detail = _open_fuel_record_page(driver, request, output_dir)
        status = "fuel_record_saved" if _save_fuel_record_enabled() else "fuel_record_prefilled"
        return SeleniumRunResult(True, status, detail, summary_path)
    except Exception as exc:
        if driver is not None:
            _save_artifacts(driver, output_dir, request.task_id, "fuel_record_error")
        return SeleniumRunResult(False, "fuel_record_failed", f"加油紀錄操作失敗：{exc}", summary_path)
    finally:
        if owns_driver and not keep_browser_open:
            _quit_driver(driver)
        if lock_acquired:
            _release_selenium_session(f"fuel_record {request.task_id}")


def run_disinfection_task(
    request: AmbulanceReturnRequest,
    artifacts_dir: Path,
    existing_driver: webdriver.Chrome | None = None,
    profile_name: str = "disinfection_profile",
    debugger_port: int | None = None,
    use_session_lock: bool = True,
    tile_name: str = "",
    force_new_driver: bool = False,
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
        if use_session_lock:
            lock_acquired = _acquire_selenium_session(f"disinfection {request.task_id}")
        if driver is None:
            if debugger_port is None and not force_new_driver:
                debugger_port = int(os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223"))
            driver = _create_driver(
                artifacts_dir,
                profile_name=profile_name,
                debugger_port=debugger_port,
                attach_existing=not force_new_driver,
            )
        apply_tile(driver, tile_name)
        _set_window_size_if_enabled(driver, "disinfection")
        driver.implicitly_wait(2)
        detail = _open_disinfection_page(driver, request, output_dir)
        status = "disinfection_saved" if _save_disinfection_record_enabled() else "disinfection_session_ready"
        return SeleniumRunResult(True, status, detail, summary_path)
    except Exception as exc:
        if driver is not None:
            _save_artifacts(driver, output_dir, request.task_id, "disinfection_error")
        return SeleniumRunResult(False, "disinfection_failed", f"消毒紀錄操作失敗：{exc}", summary_path)
    finally:
        if owns_driver and not keep_browser_open:
            _quit_driver(driver)
        if lock_acquired:
            _release_selenium_session(f"disinfection {request.task_id}")


def query_duty_emergency_cases(artifacts_dir: Path, lookup_range: str = "24h") -> DutyCaseLookupResult:
    output_dir = artifacts_dir / "cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "latest.json"
    previous_cases = _previous_case_details(output_path)
    deadline = time.monotonic() + _case_lookup_timeout_seconds()
    print(f"[case_lookup] starting duty emergency case lookup range={lookup_range}", flush=True)
    driver = None
    lock_acquired = False
    try:
        _case_lookup_log_step("waiting_lock", range=lookup_range)
        lock_acquired = _acquire_selenium_session(f"case_lookup {lookup_range}")
        _check_case_lookup_deadline(deadline, "waiting for selenium lock")
        _case_lookup_log_step("chrome_starting", range=lookup_range)
        driver = _create_driver(
            artifacts_dir,
            profile_name=f"case_lookup_profile_{int(time.time())}",
            debugger_port=CASE_LOOKUP_DEBUGGER_PORT,
            attach_existing=False,
            headless=True,
        )
        _set_case_lookup_driver_timeouts(driver)
        _case_lookup_log_step("chrome_ready", range=lookup_range)
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
        _case_lookup_log_step("duty_login", range=lookup_range)
        if not _ensure_duty_login(driver, _case_lookup_duty_login_candidates()):
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
            return DutyCaseLookupResult(True, payload["status"], payload["detail"], [], output_path)

        _check_case_lookup_deadline(deadline, "duty login")
        _case_lookup_log_step("duty_login_ok", range=lookup_range)
        _case_lookup_log_step("open_query", range=lookup_range)
        _open_case_query(driver, lookup_range=lookup_range)
        _check_case_lookup_deadline(deadline, "opening case query")
        _case_lookup_log_step("read_rows", range=lookup_range)
        cases = _extract_all_emergency_cases(driver)
        _case_lookup_log_step("rows_loaded", range=lookup_range, count=len(cases))
        _check_case_lookup_deadline(deadline, "reading case rows")
        _case_lookup_log_step("read_details", range=lookup_range, count=len(cases))
        cases = _attach_case_form_details(driver, cases, artifacts_dir, previous_cases, deadline=deadline)
        _case_lookup_log_step("details_loaded", range=lookup_range, count=len(cases))
        _save_artifacts(driver, artifacts_dir / "selenium", "case_lookup", "duty_cases")
        payload = _case_lookup_payload(
            "cases_loaded",
            f"\u5df2\u67e5\u5230 {len(cases)} \u7b46 24 \u5c0f\u6642\u5167\u6848\u4ef6\uff0c\u4e26\u8b80\u53d6\u51fa\u52e4\u4eba\u54e1\u3002",
            cases,
        )
        _write_json_atomic(output_path, payload)
        return DutyCaseLookupResult(True, payload["status"], payload["detail"], cases, output_path)
    except TimeoutException as exc:
        payload = _case_lookup_payload(
            "case_lookup_timeout",
            f"案件查詢逾時：{exc}",
            [],
        )
        _write_json_atomic(output_path, payload)
        return DutyCaseLookupResult(False, payload["status"], payload["detail"], [], output_path)
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


def lookup_synced_credential_id_number(artifacts_dir: Path, lookup_range: str = "24h") -> SyncedCredentialIdLookupResult:
    credential = load_synced_worker_credential()
    if credential is None:
        return SyncedCredentialIdLookupResult(False, "missing_synced_credential", "missing synced credential")
    existing = str(credential.id_number or "").strip().upper()
    if existing:
        return SyncedCredentialIdLookupResult(True, "already_has_id_number", "synced credential already has id number", existing)

    lookup = query_duty_emergency_cases(artifacts_dir, lookup_range=lookup_range)
    output_path = getattr(lookup, "path", None)
    if not lookup.ok:
        return SyncedCredentialIdLookupResult(False, lookup.status, lookup.detail, output_path=output_path)

    id_number = _id_number_from_cases_for_credential(getattr(lookup, "cases", []) or [], credential)
    if not id_number:
        return SyncedCredentialIdLookupResult(False, "id_number_not_found", "synced credential id number was not found in duty cases", output_path=output_path)

    update_saved_credential_id_number(credential.user_id or credential.actor_no, id_number, name=credential.name)
    return SyncedCredentialIdLookupResult(True, "id_number_saved", "synced credential id number saved", id_number, output_path)


_ID_NUMBER_IN_TEXT_RE = re.compile(r"(?<![A-Z0-9])([A-Z][1289]\d{8})(?![A-Z0-9])", re.IGNORECASE)


def _id_number_from_cases_for_credential(cases: list[dict[str, object]], credential: DutyCredential) -> str:
    for case in cases:
        for segment in _case_personnel_segments(case):
            if not _segment_matches_credential(segment, credential):
                continue
            match = _ID_NUMBER_IN_TEXT_RE.search(segment)
            if match:
                return match.group(1).upper()
    return ""


def _case_personnel_segments(case: dict[str, object]) -> list[str]:
    segments: list[str] = []
    for key in ("personnel_hidden_raw", "personnel_raw", "personnel", "description"):
        value = case.get(key)
        if isinstance(value, list):
            raw_values = value
        else:
            raw_values = [value]
        for raw in raw_values:
            text = str(raw or "").strip()
            if not text:
                continue
            parts = [part.strip() for part in re.split(r"[\r\n,，、;；]+", text) if part.strip()]
            segments.extend(parts or [text])
    return segments


def _segment_matches_credential(segment: str, credential: DutyCredential) -> bool:
    text = str(segment or "").strip()
    if not text:
        return False
    actor_no = str(credential.actor_no or "").strip()
    if actor_no and re.search(rf"(?<!\d){re.escape(actor_no)}\s*(?:番|號)", text):
        return True
    name = str(credential.name or "").strip()
    if name and name in text:
        return True
    display_name = str(credential.display_name or "").strip()
    if display_name and display_name in text:
        return True
    user_id = str(credential.user_id or "").strip()
    return bool(user_id and user_id.lower() in text.lower())


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


def _case_lookup_timeout_seconds() -> int:
    try:
        return max(30, int(os.getenv("CASE_LOOKUP_TIMEOUT_SECONDS", "120")))
    except ValueError:
        return 120


def _case_lookup_command_timeout_seconds() -> int:
    try:
        return max(10, int(os.getenv("CASE_LOOKUP_COMMAND_TIMEOUT_SECONDS", "20")))
    except ValueError:
        return 20


def _set_case_lookup_driver_timeouts(driver: webdriver.Chrome) -> None:
    timeout = _case_lookup_command_timeout_seconds()
    try:
        set_page_timeout = getattr(driver, "set_page_load_timeout", None)
        set_script_timeout = getattr(driver, "set_script_timeout", None)
        if callable(set_page_timeout):
            set_page_timeout(timeout)
        if callable(set_script_timeout):
            set_script_timeout(timeout)
    except WebDriverException as exc:
        print(f"[case_lookup] step=timeout_setup_skipped reason={_short_webdriver_error(exc)}", flush=True)


def _check_case_lookup_deadline(deadline: float, stage: str) -> None:
    if time.monotonic() > deadline:
        raise TimeoutException(f"{stage} exceeded {_case_lookup_timeout_seconds()} seconds")


def _case_lookup_log_step(step: str, **fields: object) -> None:
    suffix = "".join(f" {key}={str(value).replace(' ', '_')}" for key, value in fields.items())
    print(f"[case_lookup] step={step}{suffix}", flush=True)


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
    profile_name: str = "duty_work_log_profile",
    debugger_port: int | None = None,
    attach_existing: bool = False,
    headless: bool = False,
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

    if attach_existing and debugger_port and not headless:
        existing = _connect_existing_chrome(debugger_port)
        if existing:
            return existing

    options = Options()
    if headless:
        for arg in _chrome_headless_args():
            options.add_argument(arg)
        options.add_argument("--window-size=1280,900")
    else:
        options.add_argument("--start-maximized")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    _cleanup_stale_profiles_for_driver(profile_name)
    user_data_dir = _profile_dir(profile_name)
    options.add_argument(f"--user-data-dir={user_data_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    if debugger_port and not headless:
        options.add_argument(f"--remote-debugging-port={debugger_port}")
    if not headless and os.getenv("SELENIUM_DETACH", "true").strip().lower() not in {"0", "false", "no", "off"}:
        options.add_experimental_option("detach", True)
    driver = _create_local_driver_with_retry(options)
    if not headless:
        schedule_driver_auto_close(driver, profile_name)
    page_timeout = int(os.getenv("SELENIUM_PAGE_LOAD_TIMEOUT_SECONDS", "45"))
    driver.set_page_load_timeout(page_timeout)
    driver.set_script_timeout(page_timeout)
    return driver


def _create_local_driver_with_retry(options: Options) -> webdriver.Chrome:
    attempts = int(os.getenv("SELENIUM_LOCAL_SESSION_ATTEMPTS", "2"))
    last_error: Exception | None = None
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            if attempts > 1:
                print(f"[selenium] creating local chrome session attempt {attempt}/{attempts}", flush=True)
            return create_webdriver_chrome_with_timeout(options, factory=webdriver.Chrome)
        except (ChromeStartTimeoutError, WebDriverException, OSError) as exc:
            last_error = exc
            if not _is_local_chrome_startup_error(exc):
                raise
            print(f"[selenium] local chrome session attempt {attempt} failed: {_short_webdriver_error(exc)}", flush=True)
            cleanup_worker_chrome_residue(options, "local selenium")
            cleanup_runtime_profiles_for_startup_failure(_worker_user_data_paths(options))
            if attempt >= attempts:
                break
            time.sleep(2)
    raise WebDriverException(f"local chrome session failed after {attempts} attempts: {_short_webdriver_error(last_error)}")


def _is_local_chrome_startup_error(exc: Exception) -> bool:
    if isinstance(exc, ChromeStartTimeoutError):
        return True
    if _is_invalid_argument_oserror(exc):
        return True
    message = str(exc).lower()
    startup_markers = (
        "from chrome not reachable",
        "chrome not reachable",
        "session not created",
        "devtoolsactiveport file doesn't exist",
    )
    return any(marker in message for marker in startup_markers)


def _is_invalid_argument_oserror(exc: Exception) -> bool:
    if not isinstance(exc, OSError):
        return False
    message = str(exc).lower()
    return getattr(exc, "errno", None) in {22, errno.ENOSPC} or "invalid argument" in message or "no space left" in message


def _short_webdriver_error(exc: Exception | None) -> str:
    if exc is None:
        return "unknown error"
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return first_line[:240]


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
        for arg in _chrome_headless_args():
            options.add_argument(arg)
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    return options


def _chrome_headless_arg() -> str:
    return next((arg for arg in _chrome_headless_args() if "headless" in arg.lower()), "--headless=new")


def _chrome_headless_args() -> list[str]:
    raw = os.getenv("SELENIUM_HEADLESS_ARG", "").strip()
    args = shlex.split(raw) if raw else []
    if not any("headless" in arg.lower() for arg in args):
        args.insert(0, "--headless=new")
    return args


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


def cleanup_stale_selenium_profiles(
    profile_root: Path | None = None,
    max_age_hours: float | None = None,
    skip_profile_names: set[str] | None = None,
) -> list[Path]:
    return cleanup_stale_runtime_profiles(profile_root, max_age_hours, skip_profile_names=skip_profile_names)


def _cleanup_stale_profiles_for_driver(profile_name: str) -> None:
    cleanup_stale_selenium_profiles(runtime_profile_root(), skip_profile_names={profile_name})


def _selenium_profile_cleanup_max_age_hours() -> float:
    try:
        return max(float(os.getenv("SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS", "4")), 0.0)
    except ValueError:
        return 4.0


def _is_generated_selenium_profile(profile_name: str) -> bool:
    return profile_name in _GENERATED_SELENIUM_PROFILE_NAMES or profile_name.startswith(_GENERATED_SELENIUM_PROFILE_PREFIXES)


def _profile_has_active_lock(profile_dir: Path) -> bool:
    return any((profile_dir / name).exists() for name in _CHROME_PROFILE_LOCK_NAMES)


def _profile_root() -> Path:
    return runtime_profile_root()


def _profile_dir(profile_name: str) -> Path:
    return runtime_profile_dir(profile_name)


def _open_duty_work_log_case_picker(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest,
    output_dir: Path,
    summary_path: Path,
) -> SeleniumRunResult:
    if not _ensure_duty_login(driver, request.duty_login_account_candidates):
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
    if not _ensure_duty_login(driver, request.duty_login_account_candidates):
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
    _set_case_query_date_range(driver, lookup_range="24h")
    _click_query_if_present(driver)
    time.sleep(1)
    cases = _extract_all_emergency_cases(driver)
    case = _match_case_for_request(cases, request)
    if not case:
        _save_artifacts(driver, output_dir, request.task_id, "duty_case_picker")
        return SeleniumRunResult(
            ok=False,
            status="duty_case_not_found",
            detail=f"未在前 24 小時案件清單找到符合時間={request.case_time}、地址={request.case_address} 的案件；已保存查詢頁截圖。",
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
    elif _save_duty_work_log_enabled():
        save_result = _click_duty_work_log_save(driver)
        time.sleep(1.5)
        _save_artifacts(driver, output_dir, request.task_id, "duty_work_log_saved")
        if save_result.get("ok"):
            detail = "消防勤務工作紀錄已預填勤務項目、事由、處理情形並按下儲存。"
            status = "duty_work_log_saved"
        else:
            detail = f"消防勤務工作紀錄已預填，但儲存按鈕未成功點擊：{save_result.get('reason', 'unknown')}。"
            status = "duty_work_log_save_failed"
    else:
        detail = "消防勤務工作紀錄已預填勤務項目、事由、處理情形，未按儲存。"
        status = "duty_work_log_prefilled"
    return SeleniumRunResult(ok=True, status=status, detail=detail, summary_path=summary_path)


def _open_vehicle_mileage_page(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest,
    output_dir: Path,
    update_context: dict[str, object] | None = None,
) -> str:
    try:
        if not _ensure_ppe_vehicle_mileage_session(driver, request):
            _save_artifacts(driver, output_dir, request.task_id, "vehicle_mileage_login")
            raise WebDriverException("PPE login did not reach vehicle mileage page")
        detail = _prepare_vehicle_mileage_form(driver, request, output_dir.parent, update_context=update_context)
        _save_artifacts(driver, output_dir, request.task_id, "vehicle_mileage")
    except WebDriverException:
        _save_artifacts(driver, output_dir, request.task_id, "vehicle_mileage_error")
        raise

    return detail


def _ensure_ppe_vehicle_mileage_session(driver: webdriver.Chrome, request: AmbulanceReturnRequest | None = None) -> bool:
    return _ensure_ppe_session(
        driver,
        request,
        target_url="https://ppe.tyfd.gov.tw/CarRecord/List",
        wait_for_target=_wait_for_ppe_vehicle_mileage_page,
    )


def _ensure_ppe_fuel_record_session(driver: webdriver.Chrome, request: AmbulanceReturnRequest | None = None) -> bool:
    return _ensure_ppe_session(
        driver,
        request,
        target_url="https://ppe.tyfd.gov.tw/FUC04100/Query",
        wait_for_target=_wait_for_ppe_fuel_record_page,
    )


def _ensure_ppe_session(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest | None,
    *,
    target_url: str,
    wait_for_target,
) -> bool:
    credential_attempts = _ppe_credential_attempts(request)
    if not credential_attempts:
        raise WebDriverException("missing PPE login credentials")
    for username, password in credential_attempts:
        driver.get(target_url)
        if wait_for_target(driver, timeout=8):
            return True
        if not _is_ppe_login_page(driver):
            continue
        driver.find_element(By.ID, "Account").clear()
        driver.find_element(By.ID, "Account").send_keys(username)
        driver.find_element(By.ID, "Password").clear()
        driver.find_element(By.ID, "Password").send_keys(password)
        _click_ppe_login(driver)
        if _wait_for_ppe_login_result(driver, timeout=12):
            driver.get(target_url)
        if wait_for_target(driver, timeout=12):
            return True
        time.sleep(1)
    return False


def _ppe_credentials() -> tuple[str, str]:
    saved = load_synced_worker_credential()
    if saved is None:
        username = os.getenv("PPE_ACCOUNT", "").strip() or os.getenv("DUTY_ACCOUNT", "").strip()
        password = os.getenv("PPE_PASSWORD", "").strip() or os.getenv("DUTY_PASSWORD", "").strip()
        if username and password:
            return username, password
        return "", ""
    return saved.user_id, saved.password


def _ppe_credential_attempts(request: AmbulanceReturnRequest | None = None) -> list[tuple[str, str]]:
    attempts: list[tuple[str, str]] = []
    if request is not None:
        _append_ppe_duty_attempts(attempts, request.driver_duty_login_account_candidates)
        _append_ppe_duty_attempts(attempts, request.personnel_duty_login_account_candidates)
    synced = load_synced_worker_credential()
    if synced is not None:
        attempts.append((synced.user_id, synced.password))
    else:
        username = os.getenv("PPE_ACCOUNT", "").strip() or os.getenv("DUTY_ACCOUNT", "").strip()
        password = os.getenv("PPE_PASSWORD", "").strip() or os.getenv("DUTY_PASSWORD", "").strip()
        if username and password:
            attempts.append((username, password))
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for username, password in attempts:
        if not (username and password):
            continue
        key = username.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((username, password))
    return deduped


def _append_ppe_duty_attempts(attempts: list[tuple[str, str]], candidates: list[str]) -> None:
    for candidate in candidates:
        credential = load_duty_credential([candidate], fallback_user_id="", allow_default=False)
        if credential is not None:
            attempts.append((credential.user_id, credential.password))


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
    status_text = request.duty_status_text
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


def _is_ppe_vehicle_mileage_page(driver: webdriver.Chrome) -> bool:
    return bool(
        driver.execute_script(
            """
            if (!!document.getElementById('Account') && !!document.getElementById('Password')) return false;
            const path = String(location.pathname || '');
            const text = document.body ? document.body.innerText : '';
            return path.includes('/CarRecord') || text.includes('\u8eca\u8f1b\u4f7f\u7528\u7d00\u9304');
            """
        )
    )


def _is_ppe_fuel_record_page(driver: webdriver.Chrome) -> bool:
    return bool(
        driver.execute_script(
            """
            if (!!document.getElementById('Account') && !!document.getElementById('Password')) return false;
            const path = String(location.pathname || '');
            const text = document.body ? document.body.innerText : '';
            return path.includes('/FUC04100') || text.includes('登打油耗里程') || text.includes('加油紀錄');
            """
        )
    )


def _is_ppe_fuel_record_detail_page(driver: webdriver.Chrome) -> bool:
    return bool(
        driver.execute_script(
            """
            if (!!document.getElementById('Account') && !!document.getElementById('Password')) return false;
            const path = String(location.pathname || '');
            if (!path.includes('/FUC04100/Detail')) return false;
            const grid = window.jQuery ? jQuery('#grid').data('kendoGrid') : null;
            if (!grid) return false;
            const scripts = Array.from(document.scripts).map(script => String(script.textContent || '')).join('\\n');
            return scripts.includes('DriverName') && scripts.includes('dataTextField: "Text"');
            """
        )
    )


def _wait_for_ppe_vehicle_mileage_page(driver: webdriver.Chrome, timeout: int = 12) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda current: _is_ppe_vehicle_mileage_page(current) or _is_ppe_login_page(current)
        )
    except TimeoutException:
        return False
    return _is_ppe_vehicle_mileage_page(driver)


def _wait_for_ppe_fuel_record_page(driver: webdriver.Chrome, timeout: int = 12) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda current: _is_ppe_fuel_record_page(current) or _is_ppe_login_page(current)
        )
    except TimeoutException:
        return False
    return _is_ppe_fuel_record_page(driver)


def _wait_for_ppe_fuel_record_detail_page(driver: webdriver.Chrome, timeout: int = 12) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda current: _is_ppe_fuel_record_detail_page(current) or _is_ppe_login_page(current)
        )
    except TimeoutException:
        return False
    return _is_ppe_fuel_record_detail_page(driver)


def _wait_for_ppe_login_result(driver: webdriver.Chrome, timeout: int = 12) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda current: _is_ppe_vehicle_mileage_page(current) or not _is_ppe_login_page(current)
        )
    except TimeoutException:
        return False
    return not _is_ppe_login_page(driver)


def _prepare_vehicle_mileage_form(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest,
    artifacts_dir: Path | None = None,
    update_context: dict[str, object] | None = None,
) -> str:
    driver.get("https://ppe.tyfd.gov.tw/CarRecord/List")
    if not _wait_for_ppe_vehicle_mileage_page(driver, timeout=12):
        raise WebDriverException("PPE session returned to login page before vehicle mileage form")
    _click_text_if_present(driver, ["\u8eca\u8f1b\u7ba1\u7406"])
    _click_text_if_present(driver, ["\u8eca\u8f1b\u4f7f\u7528\u7d00\u9304"])
    time.sleep(1)
    previous_request = _vehicle_mileage_previous_request(update_context)
    if previous_request and previous_request.vehicle and previous_request.vehicle != request.vehicle:
        old_vehicle_label = vehicle_ppe_names(artifacts_dir).get(previous_request.vehicle, previous_request.vehicle)
        _select_vehicle_record(driver, old_vehicle_label)
        time.sleep(1)
        deleted = _delete_vehicle_mileage_row(driver, previous_request)
        delete_detail = f"已在原車輛 {previous_request.vehicle} 刪除里程列：{deleted}。"
        if _save_vehicle_mileage_enabled():
            delete_detail = f"{delete_detail}{_save_vehicle_mileage_form(driver)}"
        else:
            delete_detail = f"{delete_detail}未按儲存。"
        driver.get("https://ppe.tyfd.gov.tw/CarRecord/List")
        if not _wait_for_ppe_vehicle_mileage_page(driver, timeout=12):
            raise WebDriverException("PPE session returned to login page before new vehicle mileage row")
        time.sleep(1)
        new_vehicle_label = vehicle_ppe_names(artifacts_dir).get(request.vehicle, request.vehicle)
        _select_vehicle_record(driver, new_vehicle_label)
        time.sleep(1)
        add_detail = _add_vehicle_mileage_record(driver, request, artifacts_dir)
        return f"{delete_detail} 已改至新車輛 {request.vehicle} 新增里程列：{add_detail}"

    vehicle_label = vehicle_ppe_names(artifacts_dir).get(request.vehicle, request.vehicle)
    _select_vehicle_record(driver, vehicle_label)
    time.sleep(1)
    if previous_request:
        row_index = _find_vehicle_mileage_row_index(driver, previous_request)
        start_mileage = _vehicle_mileage_row_value(driver, row_index, "StartMileage")
        values = _vehicle_mileage_values(request, start_mileage)
        _fill_vehicle_grid_values(driver, values, row_index=row_index)
        _assert_vehicle_mileage_values_present(driver, values, row_index=row_index)
        if _save_vehicle_mileage_enabled():
            return f"已修正原車輛里程列。{_save_vehicle_mileage_form(driver)}"
        return "已修正原車輛里程列，未按儲存。"

    return _add_vehicle_mileage_record(driver, request, artifacts_dir)


def _open_fuel_record_page(driver: webdriver.Chrome, request: AmbulanceReturnRequest, output_dir: Path) -> str:
    fuel_requests = [item for item in request.vehicle_requests() if item.fuel_record.enabled]
    if not fuel_requests:
        return "未勾選加油紀錄，已略過。"
    if not _ensure_ppe_fuel_record_session(driver, request):
        _save_artifacts(driver, output_dir, request.task_id, "fuel_record_login")
        raise WebDriverException("PPE login did not reach fuel record page")
    details: list[str] = []
    for vehicle_request in fuel_requests:
        details.append(_prepare_fuel_record_form(driver, vehicle_request, output_dir.parent))
    _save_artifacts(driver, output_dir, request.task_id, "fuel_record")
    return " ".join(details)


def _prepare_fuel_record_form(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest,
    artifacts_dir: Path | None = None,
) -> str:
    fuel = request.fuel_record
    driver.get("https://ppe.tyfd.gov.tw/FUC04100/Query")
    if not _wait_for_ppe_fuel_record_page(driver, timeout=12):
        raise WebDriverException("PPE session returned to login page before fuel record query")
    target_period = f"{fuel.date[:4]}/{fuel.date[4:6]}"
    current_period = _ensure_fuel_query_period(driver, target_period)
    if current_period and current_period != target_period:
        raise WebDriverException(f"fuel period mismatch: page={current_period} task={target_period}")
    _click_fuel_card_register(driver, _fuel_card_labels(request.vehicle, artifacts_dir))
    if not _wait_for_ppe_fuel_record_detail_page(driver, timeout=12):
        raise WebDriverException("fuel detail page did not open")
    _click_fuel_add_row(driver)
    _fill_fuel_grid_record(driver, request)
    _assert_fuel_grid_record_present(driver, request)
    if _save_fuel_record_enabled():
        return _save_fuel_record_form(driver, request)
    return f"{request.vehicle} 已填寫加油紀錄，未按儲存。"


def _fuel_query_period(driver: webdriver.Chrome) -> str:
    try:
        return str(
            driver.execute_script(
                "return document.getElementById('FuelUseYM') ? document.getElementById('FuelUseYM').value : '';"
            )
            or ""
        ).strip()
    except WebDriverException:
        return ""


def _ensure_fuel_query_period(driver: webdriver.Chrome, target_period: str) -> str:
    current_period = _fuel_query_period(driver)
    if not current_period or current_period == target_period:
        return current_period

    driver.execute_script(
        """
        const targetPeriod = arguments[0];
        const compactTarget = targetPeriod.replace('/', '');
        const el = document.getElementById('FuelUseYM');
        const textOf = node => [node?.innerText, node?.value, node?.textContent, node?.title, node?.id, node?.name]
          .map(value => String(value || '')).join(' ');
        const dispatch = node => {
          for (const type of ['input', 'change', 'blur']) {
            node.dispatchEvent(new Event(type, {bubbles: true}));
          }
        };
        const clickQuery = () => {
          const controls = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]'));
          const target = controls.find(node => {
            if (!node || node.disabled) return false;
            const text = textOf(node);
            return text.includes('查詢') || text.includes('Query') || text.includes('_btnQuery') || text.includes('btnQuery');
          });
          if (!target) return false;
          target.scrollIntoView({block: 'center', inline: 'center'});
          target.click();
          return true;
        };
        if (!el) return {changed: false, clicked: false, value: ''};
        if (el.tagName === 'SELECT') {
          const option = Array.from(el.options || []).find(item => {
            const text = [item.value, item.textContent].map(value => String(value || '')).join(' ');
            return text.includes(targetPeriod) || text.includes(compactTarget);
          });
          if (option) el.value = option.value;
        } else {
          el.value = targetPeriod;
        }
        dispatch(el);
        if (window.jQuery) {
          try {
            const field = window.jQuery(el);
            field.val(targetPeriod).trigger('input').trigger('change').trigger('blur');
            for (const widgetName of ['kendoDatePicker', 'kendoDateInput', 'kendoMaskedTextBox']) {
              const widget = field.data(widgetName);
              if (!widget || typeof widget.value !== 'function') continue;
              try { widget.value(targetPeriod); } catch (_) {}
              try { widget.value(new Date(Number(targetPeriod.slice(0, 4)), Number(targetPeriod.slice(5, 7)) - 1, 1)); } catch (_) {}
              try { widget.trigger && widget.trigger('change'); } catch (_) {}
            }
          } catch (_) {}
        }
        return {changed: true, clicked: clickQuery(), value: String(el.value || '').trim()};
        """,
        target_period,
    )
    time.sleep(1)

    deadline = time.monotonic() + 8
    latest_period = _fuel_query_period(driver)
    while latest_period != target_period and time.monotonic() < deadline:
        time.sleep(0.5)
        latest_period = _fuel_query_period(driver)
    return latest_period


def _fuel_card_labels(vehicle: str, artifacts_dir: Path | None = None) -> list[str]:
    labels: list[str] = []
    for label in (vehicle_ppe_names(artifacts_dir).get(vehicle, ""), vehicle):
        label = str(label or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def _click_fuel_card_register(driver: webdriver.Chrome, vehicle_labels: str | list[str] | tuple[str, ...]) -> None:
    labels = [vehicle_labels] if isinstance(vehicle_labels, str) else list(vehicle_labels)
    labels = [str(label or "").strip() for label in labels if str(label or "").strip()]
    result: dict[str, object] = {}
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = dict(
            driver.execute_script(
                """
                const vehicles = arguments[0];
                const rows = Array.from(document.querySelectorAll('tr,[role="row"]'));
                for (const row of rows) {
                  const rowText = String(row.innerText || '').replace(/\\s+/g, ' ');
                  if (!vehicles.some(vehicle => rowText.includes(vehicle))) continue;
                  const controls = Array.from(row.querySelectorAll('button,a,input[type=button],input[type=submit]'));
                  const availableControls = controls.filter(el => {
                    const text = [el.innerText, el.value, el.textContent, el.title, el.id, el.name].map(x => String(x || '')).join(' ');
                    return !text.includes('送出審核') && !text.includes('儲存');
                  });
                  const target = availableControls.find(el => {
                    const text = [el.innerText, el.value, el.textContent, el.title, el.id, el.name].map(x => String(x || '')).join(' ');
                    return text.includes('登錄');
                  }) || (availableControls.length === 1 ? availableControls[0] : null);
                  if (target) {
                    target.scrollIntoView({block: 'center', inline: 'center'});
                    target.click();
                    return {clicked: true, rowMatched: true, rowCount: rows.length};
                  }
                  return {clicked: false, rowMatched: true, rowCount: rows.length};
                }
                return {clicked: false, rowMatched: false, rowCount: rows.length};
                """,
                labels,
            )
            or {}
        )
        if result.get("clicked"):
            time.sleep(1.5)
            return
        time.sleep(0.5)
    label_text = " / ".join(labels)
    if result.get("rowMatched"):
        raise WebDriverException(f"fuel register button not found: {label_text}")
    raise WebDriverException(f"fuel card not found: {label_text}; rows={result.get('rowCount', 0)}")


def _click_fuel_add_row(driver: webdriver.Chrome) -> None:
    clicked = bool(
        driver.execute_script(
            """
            const controls = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]'));
            const target = controls.find(el => {
              if (!el || el.disabled) return false;
              const text = [el.innerText, el.value, el.textContent, el.title, el.id, el.name].map(x => String(x || '')).join(' ');
              return text.includes('新增') && !text.includes('送出審核');
            });
            if (!target) return false;
            target.scrollIntoView({block: 'center', inline: 'center'});
            target.click();
            return true;
            """
        )
    )
    if not clicked:
        raise WebDriverException("missing fuel add button")
    time.sleep(1)


def _fill_fuel_grid_record(driver: webdriver.Chrome, request: AmbulanceReturnRequest) -> None:
    fuel = request.fuel_record
    result = driver.execute_script(
        """
        const fuel = arguments[0];
        const grid = window.jQuery ? jQuery('#grid').data('kendoGrid') : null;
        if (!grid) return {ok: false, reason: 'missing grid'};
        let item = Array.from(grid.dataSource.data()).find(row => Number(row.FCUseID || 0) === 0);
        if (!item) {
          grid.addRow();
          item = Array.from(grid.dataSource.data()).find(row => Number(row.FCUseID || 0) === 0);
        }
        if (!item) return {ok: false, reason: 'missing new row'};
        const scripts = Array.from(document.scripts).map(s => String(s.textContent || '')).join('\\n');
        const escapeRegex = value => String(value).replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
        function lookupValue(text, fieldName) {
          const direct = new RegExp('"Value"\\\\s*:\\\\s*"([^"]+)"\\\\s*,\\\\s*"Text"\\\\s*:\\\\s*"' + escapeRegex(text) + '"').exec(scripts);
          if (direct) return direct[1];
          const rows = Array.from(grid.dataSource.data());
          const matched = rows.find(row => String(row[fieldName + 'Name'] || '') === text);
          return matched ? matched[fieldName] : null;
        }
        const driverValue = lookupValue(fuel.driver, 'Driver');
        const fuelTypeValue = lookupValue(fuel.product, 'FuelType') || 535;
        if (!driverValue) return {ok: false, reason: 'missing driver'};
        const date = `${fuel.date.slice(0, 4)}-${fuel.date.slice(4, 6)}-${fuel.date.slice(6, 8)}T${fuel.time.slice(0, 2)}:${fuel.time.slice(2, 4)}:00`;
        const qty = Number(fuel.quantity);
        const price = Number(fuel.unit_price);
        item.set('FuelDate', date);
        item.set('FuelTime', fuel.time);
        item.set('DriverName', fuel.driver);
        item.set('Driver', Number(driverValue));
        item.set('FuelTypeName', fuel.product);
        item.set('FuelType', Number(fuelTypeValue));
        item.set('FuelQty', qty);
        item.set('FuelPrice', price);
        item.set('FuelAmount', Math.round(qty * price));
        item.set('MUserName', fuel.driver);
        item.set('CreateBy', fuel.driver);
        grid.refresh();
        return {ok: true};
        """,
        {
            "date": fuel.date,
            "time": fuel.time,
            "driver": fuel.driver or request.driver,
            "product": fuel.product,
            "quantity": fuel.quantity,
            "unit_price": fuel.unit_price,
        },
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise WebDriverException(f"fuel grid fill failed: {result}")


def _assert_fuel_grid_record_present(driver: webdriver.Chrome, request: AmbulanceReturnRequest) -> None:
    fuel = request.fuel_record
    present = bool(
        driver.execute_script(
            """
            const fuel = arguments[0];
            const text = document.body ? document.body.innerText : '';
            return text.includes(fuel.date) && text.includes(fuel.time) && text.includes(fuel.driver)
              && text.includes(fuel.product) && text.includes(String(fuel.quantity)) && text.includes(String(fuel.unit_price));
            """,
            {
                "date": fuel.date,
                "time": fuel.time,
                "driver": fuel.driver or request.driver,
                "product": fuel.product,
                "quantity": fuel.quantity,
                "unit_price": fuel.unit_price,
            },
        )
    )
    if not present:
        raise WebDriverException("fuel grid values not visible after fill")


def _save_fuel_record_form(driver: webdriver.Chrome, request: AmbulanceReturnRequest) -> str:
    called = bool(
        driver.execute_script(
            """
            if (typeof SaveData !== 'function') return false;
            SaveData('save');
            return true;
            """
        )
    )
    if not called:
        raise WebDriverException("missing fuel save function")
    alert_text = _accept_alert_if_present(driver)
    sweetalert_text = _confirm_sweetalert_if_present(driver)
    final_alert_text = _accept_alert_if_present(driver, timeout=1)
    if _is_ppe_login_page(driver):
        raise WebDriverException("PPE session returned to login page after fuel save")
    confirmations = [text for text in (alert_text, sweetalert_text, final_alert_text) if text]
    suffix = f"：{' / '.join(confirmations)}" if confirmations else "。"
    return f"{request.vehicle} 已填寫加油紀錄並按下儲存{suffix}"


def _add_vehicle_mileage_record(driver: webdriver.Chrome, request: AmbulanceReturnRequest, artifacts_dir: Path | None = None) -> str:
    latest_end_mileage = _extract_latest_end_mileage(driver)
    _add_vehicle_mileage_row(driver)
    time.sleep(1)

    values = _vehicle_mileage_values(request, latest_end_mileage)
    _fill_vehicle_grid_values(driver, values)
    _assert_vehicle_mileage_values_present(driver, values)
    if _save_vehicle_mileage_enabled():
        return _save_vehicle_mileage_form(driver)
    return "\u5df2\u586b\u5beb\u8eca\u8f1b\u91cc\u7a0b\uff0c\u672a\u6309\u5132\u5b58\u3002"


def _open_disinfection_page(driver: webdriver.Chrome, request: AmbulanceReturnRequest, output_dir: Path) -> str:
    current_url = driver.current_url.lower()
    if "emsdt.tyfd.gov.tw/emmweb" not in current_url:
        driver.get(SITE_DEFINITION_BY_KEY["disinfection"].url)
        time.sleep(1.5)
    _save_artifacts(driver, output_dir, request.task_id, "disinfection_opened")
    _assert_disinfection_not_login(driver, "opened")
    detail = _prepare_disinfection_record(driver, request, output_dir)
    if _save_disinfection_probe_enabled():
        controls_path = _save_disinfection_probe(driver, output_dir, request.task_id)
        return f"{detail} 已保存頁面控制項：{controls_path}"
    return detail


def _is_disinfection_login_page(driver: webdriver.Chrome) -> bool:
    url = driver.current_url.lower()
    if "login" in url or "signin" in url:
        return True
    source = driver.page_source
    login_markers = ["驗證碼", "帳號", "密碼", "登入"]
    return sum(1 for marker in login_markers if marker in source) >= 3


def _assert_disinfection_not_login(driver: webdriver.Chrome, label: str) -> None:
    if _is_disinfection_login_page(driver):
        raise WebDriverException(f"disinfection session returned to login page: {label}")


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
    _switch_to_disinfection_content_if_present(driver)
    _wait_for_disinfection_query_fields(driver)
    _save_disinfection_progress_artifacts(driver, output_dir, request.task_id, "disinfection_entry")
    _assert_disinfection_not_login(driver, "entry")

    _set_disinfection_query_date(driver, _disinfection_query_date(request))
    if not _click_disinfection_query(driver):
        raise WebDriverException("missing disinfection query button")
    _wait_for_disinfection_query_completed(driver)
    _save_disinfection_progress_artifacts(driver, output_dir, request.task_id, "disinfection_query")
    _assert_disinfection_not_login(driver, "query")

    if not _open_disinfection_detail_for_case(driver, request.case_time, request.vehicle):
        raise WebDriverException(f"missing disinfection detail for case time {request.case_time or 'empty'}")
    _wait_for_disinfection_detail_ready(driver)
    _save_disinfection_progress_artifacts(driver, output_dir, request.task_id, "disinfection_detail")

    selected_items = _effective_disinfection_items(request.disinfection_items)
    if selected_items:
        updated = _set_disinfection_item_statuses(driver, selected_items, "\u5df2\u9078\u53d6\u5340")
        if updated <= 0:
            raise WebDriverException(f"missing disinfection item selects: {request.disinfection_items_summary}")
    else:
        updated = 0
    _save_disinfection_progress_artifacts(driver, output_dir, request.task_id, "disinfection_prefilled")

    if _save_disinfection_record_enabled():
        if not _click_disinfection_save(driver):
            raise WebDriverException("missing disinfection save button")
        alert_text = _accept_alert_if_present(driver)
        _assert_disinfection_not_login(driver, "save")
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


def _click_disinfection_query(driver: webdriver.Chrome) -> bool:
    return bool(
        driver.execute_script(
            """
            const target = document.getElementById('_btnQuery') ||
              Array.from(document.querySelectorAll('input[type=button], input[type=submit], button'))
                .find(el => String(el.value || el.innerText || el.textContent || '').includes('查詢'));
            if (!target) return false;
            target.click();
            return true;
            """
        )
    )


def _wait_for_disinfection_query_fields(driver: webdriver.Chrome, timeout: float = 4) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if driver.execute_script("return !!document.getElementById('_txtFromDate') && !!document.getElementById('_txtToDate');"):
            return
        time.sleep(0.2)


def _wait_for_disinfection_query_completed(driver: webdriver.Chrome, timeout: float = 3) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
        if "查詢完成" in str(text):
            return
        time.sleep(0.2)


def _wait_for_disinfection_detail_ready(driver: webdriver.Chrome, timeout: float = 3) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready = driver.execute_script(
            """
            return Array.from(document.querySelectorAll('select, input, button'))
              .some(el => /已選取區|消毒|儲存|存檔/.test(String(el.value || el.innerText || el.textContent || el.name || el.id || '')));
            """
        )
        if ready:
            return
        time.sleep(0.2)


def _save_disinfection_progress_artifacts(driver: webdriver.Chrome, output_dir: Path, task_id: str, site_key: str) -> None:
    enabled = os.getenv("SAVE_DISINFECTION_PROGRESS_ARTIFACTS", "").strip().lower() in {"1", "true", "yes", "on"}
    if enabled:
        _save_artifacts(driver, output_dir, task_id, site_key)


def _disinfection_query_date(request: AmbulanceReturnRequest) -> str:
    return request.service_case_date().strftime("%Y-%m-%d")

def _open_disinfection_detail_for_case(driver: webdriver.Chrome, case_time: str, vehicle: str = "") -> bool:
    rows = driver.execute_script(
        """
        return Array.from(document.querySelectorAll('tr')).map((tr, index) => ({
          index,
          text: tr.innerText || ''
        }));
        """
    )
    row_index = _select_disinfection_detail_row(rows, case_time, vehicle)
    if row_index is not None:
        return bool(
            driver.execute_script(
                """
                const rows = Array.from(document.querySelectorAll('tr'));
                const row = rows[arguments[0]];
                if (!row) return false;
                const controls = Array.from(row.querySelectorAll('a, button, input[type=button], input[type=submit]'));
                const detail = controls.find(el => {
                  const text = [el.innerText, el.value, el.title, el.getAttribute('aria-label')].map(x => String(x || '')).join(' ');
                  return text.includes('?敦');
                }) || controls[controls.length - 1];
                if (!detail) return false;
                detail.click();
                return true;
                """,
                row_index,
            )
        )
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


def _select_disinfection_detail_row(rows: object, case_time: str, vehicle: str = "") -> int | None:
    if not isinstance(rows, list):
        return None
    digits = normalize_hhmm_local(case_time)
    variants = [digits, f"{digits[:2]}:{digits[2:]}"] if len(digits) == 4 else []
    best_index: int | None = None
    best_score = -1
    for fallback_index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "")
        if variants and not any(variant in text for variant in variants):
            continue
        score = 1
        if _disinfection_text_matches_vehicle(text, vehicle):
            score += 10
        try:
            row_index = int(row.get("index"))
        except (TypeError, ValueError):
            row_index = fallback_index
        if score > best_score:
            best_score = score
            best_index = row_index
    return best_index


def _disinfection_text_matches_vehicle(text: str, vehicle: str) -> bool:
    needle = re.sub(r"\s+", "", str(vehicle or ""))
    if not needle:
        return False
    haystack = re.sub(r"\s+", "", str(text or ""))
    return needle in haystack


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


def _confirm_sweetalert_if_present(driver: webdriver.Chrome, timeout: float = 4) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = driver.execute_script(
            """
            const overlay = document.querySelector('.swal-overlay--show-modal');
            if (!overlay) return {clicked: false, text: ''};
            const modal = overlay.querySelector('.swal-modal') || overlay;
            const content = Array.from(modal.querySelectorAll('.swal-title, .swal-text, .swal-content'))
              .map(el => String(el.innerText || el.textContent || '').trim())
              .filter(Boolean)
              .join(' ');
            const buttons = Array.from(modal.querySelectorAll('button'));
            const target = modal.querySelector('.swal-button--confirm') ||
              buttons.find(button => /^(是|確定|OK|Yes)$/i.test(String(button.innerText || button.textContent || button.value || '').trim()));
            if (!target) return {clicked: false, text: content};
            target.click();
            return {clicked: true, text: content};
            """
        )
        if isinstance(result, dict) and result.get("clicked"):
            text = str(result.get("text") or "")
            time.sleep(1)
            return text
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


def _click_save_control(driver: webdriver.Chrome) -> bool:
    if _click_save_control_in_current_frame(driver):
        return True
    try:
        driver.switch_to.default_content()
    except Exception:
        return False
    if _click_save_control_in_current_frame(driver):
        return True
    return _click_save_control_in_child_frames(driver)


def _click_vehicle_mileage_save(driver: webdriver.Chrome) -> bool:
    clicked = bool(
        driver.execute_script(
            """
            const controls = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]'));
            const target = controls.find(el => {
              if (!el || el.disabled) return false;
              const text = [
                el.id,
                el.name,
                el.value,
                el.title,
                el.innerText,
                el.textContent,
                el.getAttribute('onclick')
              ].map(x => String(x || '')).join(' ');
              return /SaveData\\s*\\(|儲存|存檔/.test(text);
            });
            if (target) {
              target.scrollIntoView({block: 'center', inline: 'center'});
              target.focus && target.focus();
              target.click();
              return true;
            }
            if (typeof SaveData === 'function') {
              SaveData();
              return true;
            }
            return false;
            """
        )
    )
    if clicked:
        time.sleep(1)
        return True
    return _click_save_control(driver)


def _save_vehicle_mileage_form(driver: webdriver.Chrome) -> str:
    if not _click_vehicle_mileage_save(driver):
        raise WebDriverException("missing vehicle mileage save button")
    alert_text = _accept_alert_if_present(driver)
    sweetalert_text = _confirm_sweetalert_if_present(driver)
    final_alert_text = _accept_alert_if_present(driver, timeout=1)
    if _is_ppe_login_page(driver):
        raise WebDriverException("PPE session returned to login page after vehicle mileage save")
    confirmations = [text for text in (alert_text, sweetalert_text, final_alert_text) if text]
    if confirmations:
        return f"\u5df2\u586b\u5beb\u8eca\u8f1b\u91cc\u7a0b\u3001\u6309\u4e0b\u5132\u5b58\u4e26\u6309\u4e0b\u78ba\u8a8d\uff1a{' / '.join(confirmations)}"
    return "\u5df2\u586b\u5beb\u8eca\u8f1b\u91cc\u7a0b\u4e26\u6309\u4e0b\u5132\u5b58\uff1b\u672a\u5075\u6e2c\u5230\u78ba\u8a8d\u8996\u7a97\u3002"


def _click_disinfection_save(driver: webdriver.Chrome) -> bool:
    clicked = bool(
        driver.execute_script(
            """
            const target =
              document.getElementById('_btnSave') ||
              document.querySelector('input[name="_btnSave"]') ||
              Array.from(document.querySelectorAll('input[type=button],input[type=submit],button,a')).find(el => {
                const text = [
                  el.id,
                  el.name,
                  el.value,
                  el.title,
                  el.innerText,
                  el.textContent,
                  el.getAttribute('onclick')
                ].map(x => String(x || '')).join(' ');
                return /_btnSave|儲存|存檔/.test(text);
              });
            if (!target || target.disabled) return false;
            target.scrollIntoView({block: 'center', inline: 'center'});
            target.focus && target.focus();
            for (const type of ['mousedown', 'mouseup']) {
              target.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
            }
            target.click();
            return true;
            """
        )
    )
    if clicked:
        time.sleep(1)
        return True
    return _click_save_control(driver)


def _click_save_control_in_child_frames(driver: webdriver.Chrome) -> bool:
    try:
        driver.switch_to.default_content()
        frame_count = len(driver.find_elements(By.CSS_SELECTOR, "iframe,frame"))
    except Exception:
        return False
    for index in range(frame_count):
        try:
            driver.switch_to.default_content()
            frame = driver.find_elements(By.CSS_SELECTOR, "iframe,frame")[index]
            driver.switch_to.frame(frame)
            if _click_save_control_in_current_frame(driver):
                return True
            nested_count = len(driver.find_elements(By.CSS_SELECTOR, "iframe,frame"))
            for nested_index in range(nested_count):
                driver.switch_to.default_content()
                frame = driver.find_elements(By.CSS_SELECTOR, "iframe,frame")[index]
                driver.switch_to.frame(frame)
                nested_frame = driver.find_elements(By.CSS_SELECTOR, "iframe,frame")[nested_index]
                driver.switch_to.frame(nested_frame)
                if _click_save_control_in_current_frame(driver):
                    return True
        except WebDriverException:
            continue
    return False


def _click_save_control_in_current_frame(driver: webdriver.Chrome) -> bool:
    clicked = bool(
        driver.execute_script(
            """
            const labels = ['儲存', '存檔', '保存', '送出', '確定', 'Save', 'Submit'];
            const controls = Array.from(document.querySelectorAll([
              'button',
              'a',
              'input[type=button]',
              'input[type=submit]',
              'input[type=image]',
              '[role=button]',
              '[onclick]',
              'img',
              'span',
              'div'
            ].join(',')));
            function visible(el) {
              if (!el || el.disabled) return false;
              if (String(el.type || '').toLowerCase() === 'hidden') return false;
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') return false;
              const rect = el.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0 || el.offsetParent !== null || el.tagName === 'INPUT';
            }
            function score(el) {
              const text = [
                el.id,
                el.name,
                el.value,
                el.alt,
                el.title,
                el.innerText,
                el.textContent,
                el.getAttribute('aria-label'),
                el.getAttribute('onclick'),
                el.getAttribute('src')
              ]
                .map(x => String(x || '')).join(' ');
              const lower = text.toLowerCase();
              if (/_?btnsave|btn.?save|save|submit|update|confirm|dosave|saveform/.test(lower)) return 4;
              if (String(el.type || '').toLowerCase() === 'submit') return 2;
              if (labels.some(label => text.includes(label))) return 2;
              const parentText = el.parentElement ? String(el.parentElement.innerText || el.parentElement.textContent || '') : '';
              if (labels.some(label => parentText.includes(label))) return 1;
              return 0;
            }
            const candidates = controls
              .filter(visible)
              .map(el => ({el, score: score(el)}))
              .filter(item => item.score > 0)
              .sort((a, b) => b.score - a.score);
            const target = candidates.length ? candidates[0].el : null;
            if (!target) return false;
            target.scrollIntoView({block: 'center', inline: 'center'});
            target.focus && target.focus();
            for (const type of ['mousedown', 'mouseup']) {
              target.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
            }
            if (typeof target.click === 'function') target.click();
            return true;
            """
        )
    )
    if clicked:
        time.sleep(1)
    return clicked


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


def _vehicle_mileage_previous_request(update_context: dict[str, object] | None) -> AmbulanceReturnRequest | None:
    if not isinstance(update_context, dict):
        return None
    previous_task = update_context.get("previous_task")
    if not isinstance(previous_task, dict):
        return None
    try:
        return AmbulanceReturnRequest.from_dict(previous_task)
    except Exception:
        return None


def _vehicle_mileage_values(request: AmbulanceReturnRequest, start_mileage: str) -> dict[str, str]:
    end_mileage = _resolve_end_mileage(start_mileage, request.mileage)
    return {
        "\u958b\u59cb\u65e5\u671f": request.service_case_date().strftime("%Y%m%d"),
        "\u958b\u59cb\u6642\u9593": request.case_time,
        "\u7d50\u675f\u65e5\u671f": request.service_return_date().strftime("%Y%m%d"),
        "\u7d50\u675f\u6642\u9593": request.return_time,
        "\u958b\u59cb\u91cc\u7a0b": start_mileage,
        "\u7d50\u675f\u91cc\u7a0b": end_mileage,
        "\u4e8b\u7531": "\u6551\u8b77",
        "\u524d\u5f80\u5730\u9ede": clean_case_address(request.case_address),
        "\u99d5\u99db\u4eba": request.driver,
    }


def _find_vehicle_mileage_row_index(driver: webdriver.Chrome, previous_request: AmbulanceReturnRequest) -> int:
    expected = {
        "StartDay": previous_request.service_case_date().strftime("%Y%m%d"),
        "StartTime": normalize_hhmm_local(previous_request.case_time),
        "EndDay": previous_request.service_return_date().strftime("%Y%m%d"),
        "EndTime": normalize_hhmm_local(previous_request.return_time),
        "EndMileage": "" if str(previous_request.mileage or "").strip().startswith("+") else str(previous_request.mileage or "").strip(),
        "Destination": clean_case_address(previous_request.case_address),
        "DriverName": str(previous_request.driver or "").strip(),
    }
    result = driver.execute_script(
        """
        const expected = arguments[0];
        const grid = window.$ && $("#grid").data("kendoGrid");
        if (!grid) return {ok: false, reason: 'grid not found'};
        const rows = grid.dataSource.data();
        const norm = value => String(value ?? '').replace(/\\s+/g, '').trim();
        const scoreRow = row => {
          let score = 0;
          if (expected.StartDay && norm(row.StartDay) === norm(expected.StartDay)) score += 4;
          if (expected.StartTime && norm(row.StartTime) === norm(expected.StartTime)) score += 5;
          if (expected.EndDay && norm(row.EndDay) === norm(expected.EndDay)) score += 3;
          if (expected.EndTime && norm(row.EndTime) === norm(expected.EndTime)) score += 5;
          if (expected.EndMileage && norm(row.EndMileage) === norm(expected.EndMileage)) score += 4;
          if (expected.Destination && norm(row.Destination).includes(norm(expected.Destination))) score += 2;
          if (expected.DriverName && norm(row.DriverName) === norm(expected.DriverName)) score += 1;
          return score;
        };
        const scored = rows.map((row, index) => ({index, score: scoreRow(row)}))
          .filter(item => item.score >= 10)
          .sort((a, b) => b.score - a.score);
        if (!scored.length) return {ok: false, reason: 'matching mileage row not found'};
        if (scored.length > 1 && scored[0].score === scored[1].score) {
          return {ok: false, reason: 'matching mileage row is ambiguous', matches: scored.slice(0, 3)};
        }
        return {ok: true, index: scored[0].index, score: scored[0].score};
        """,
        expected,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise WebDriverException(f"vehicle mileage row not found safely: {result}")
    return int(result["index"])


def _vehicle_mileage_row_value(driver: webdriver.Chrome, row_index: int, field_name: str) -> str:
    value = driver.execute_script(
        """
        const rowIndex = arguments[0];
        const fieldName = arguments[1];
        const grid = window.$ && $("#grid").data("kendoGrid");
        if (!grid) return '';
        const row = grid.dataSource.data()[rowIndex];
        if (!row) return '';
        return String(row.get ? row.get(fieldName) : row[fieldName] || '');
        """,
        row_index,
        field_name,
    )
    value = str(value or "").strip()
    if not value:
        raise WebDriverException(f"vehicle mileage row value not found: {field_name}")
    return value


def _delete_vehicle_mileage_row(driver: webdriver.Chrome, previous_request: AmbulanceReturnRequest) -> str:
    row_index = _find_vehicle_mileage_row_index(driver, previous_request)
    result = driver.execute_script(
        """
        const rowIndex = arguments[0];
        const grid = window.$ && $("#grid").data("kendoGrid");
        if (!grid) return {ok: false, reason: 'grid not found'};
        const row = grid.dataSource.data()[rowIndex];
        if (!row) return {ok: false, reason: 'row not found'};
        const id = row.Id || (row.get && row.get('Id')) || '';
        if (id) {
          if (!Array.isArray(window.deleteList)) window.deleteList = [];
          window.deleteList.push(id);
        }
        grid.dataSource.remove(row);
        grid.refresh();
        return {ok: true, id: String(id || ''), rowIndex};
        """,
        row_index,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise WebDriverException(f"vehicle mileage row delete failed: {result}")
    return f"row={result.get('rowIndex')} id={result.get('id') or 'new'}"


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


def _fill_vehicle_grid_values(driver: webdriver.Chrome, values: dict[str, str], row_index: int = 0) -> None:
    missing = driver.execute_script(
        """
        const values = arguments[0];
        const rowIndex = arguments[1] || 0;
        const grid = window.$ && $("#grid").data("kendoGrid");
        if (!grid) return ['grid'];
        const rows = grid.dataSource.data();
        if (!rows.length) return ['newRow'];
        const row = rows[rowIndex];
        if (!row) return ['row'];
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
        row_index,
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


def _assert_vehicle_mileage_values_present(driver: webdriver.Chrome, values: dict[str, str], row_index: int = 0) -> None:
    expected = [values[key] for key in ("\u958b\u59cb\u6642\u9593", "\u7d50\u675f\u6642\u9593", "\u7d50\u675f\u91cc\u7a0b") if values.get(key)]
    script = """
    const expected = arguments[0];
    const rowIndex = arguments[1] || 0;
    const grid = window.$ && $("#grid").data("kendoGrid");
    if (grid && grid.dataSource.data().length) {
      const row = grid.dataSource.data()[rowIndex];
      if (!row) return expected;
      const values = [row.StartTime, row.EndTime, String(row.EndMileage || '')].map(item => String(item || ''));
      return expected.filter(item => !values.includes(String(item)));
    }
    const values = Array.from(document.querySelectorAll('input, textarea, select')).map(el => String(el.value || ''));
    return expected.filter(item => !values.includes(String(item)));
    """
    missing = driver.execute_script(script, expected, row_index)
    if missing:
        raise WebDriverException(f"vehicle mileage values not filled: {missing}")


def _open_case_query(driver: webdriver.Chrome, lookup_range: str = "24h") -> None:
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


def _duty_login_credential_attempts(preferred_user_ids: list[str] | None = None) -> list:
    attempts = []
    seen: set[str] = set()
    for user_id in preferred_user_ids or []:
        credential = load_duty_credential([user_id], fallback_user_id="", allow_default=False)
        if credential is None:
            continue
        key = credential.user_id.lower()
        if key in seen:
            continue
        seen.add(key)
        attempts.append(credential)
    fallback = load_duty_credential(None, fallback_user_id="", allow_default=True)
    if fallback is not None and fallback.user_id.lower() not in seen:
        attempts.append(fallback)
    return attempts


def _case_lookup_duty_login_candidates() -> list[str]:
    candidates: list[str] = []
    recent = load_recent_synced_duty_credential()
    if recent is not None:
        candidates.append(recent.user_id)
    synced = load_synced_worker_credential()
    if synced is not None:
        candidates.append(synced.user_id)

    result: list[str] = []
    seen: set[str] = set()
    for user_id in candidates:
        value = str(user_id or "").strip()
        key = value.lower()
        if value and key not in seen:
            result.append(value)
            seen.add(key)
    return result


def _ensure_duty_login(driver: webdriver.Chrome, preferred_user_ids: list[str] | None = None) -> bool:
    driver.get(f"{BASE_URL}/login119")
    time.sleep(1)
    if _looks_logged_in(driver):
        return True
    credentials = _duty_login_credential_attempts(preferred_user_ids)
    if not credentials:
        return False
    for credential in credentials:
        if _attempt_duty_login(driver, credential):
            return True
        driver.get(f"{BASE_URL}/login119")
        time.sleep(1)
    return False


def _attempt_duty_login(driver: webdriver.Chrome, credential) -> bool:
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
    deadline: float | None = None,
) -> list[dict[str, str]]:
    previous_cases = previous_cases or {}
    for index, case in enumerate(cases, start=1):
        if deadline is not None:
            _check_case_lookup_deadline(deadline, "reading case details")
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
            _case_lookup_log_step("read_detail", index=f"{index}/{len(cases)}", case_id=case_id)
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
        except TimeoutException:
            raise
        except WebDriverException:
            case["detail_status"] = "case_detail_failed"
    return cases


def _click_query_if_present(driver: webdriver.Chrome) -> None:
    try:
        _click_by_text_or_id(driver, ["_btnQuery"], ["\u67e5\u8a62"])
    except WebDriverException:
        return


def _set_case_query_date_range(driver: webdriver.Chrome, lookup_range: str = "24h") -> None:
    end_at = datetime.now()
    start_at = end_at - timedelta(hours=24)
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
      const isSalvagedBody = joined.includes('其他-打撈浮屍');
      const isEmergencyOrFire = joined.includes('緊急救護') || joined.includes('火災') || isSalvagedBody;
      if (!isEmergencyOrFire) continue;
      const caseId = cells[0] || '';
      if (!/^\\d{17}$/.test(caseId)) continue;
      const choose = Array.from(row.querySelectorAll('input, button, a')).find(el => {
        const haystack = [el.value, el.innerText, el.title, el.id, el.name].map(x => String(x || '')).join(' ');
        return haystack.includes('選擇');
      });
      const chooseDataMatch = String(choose ? (choose.getAttribute('onclick') || '') : '').match(/choose\\('([\\s\\S]*)'\\)/);
      const chooseParts = chooseDataMatch ? chooseDataMatch[1].split('(^w^)') : [];
      const category = cells.find(cell => cell.includes('緊急救護') || cell.includes('火災') || cell.includes('其他-打撈浮屍')) || '';
      if (!category.startsWith('緊急救護') && !category.includes('火災') && !category.includes('其他-打撈浮屍')) continue;
      const reason = isSalvagedBody ? '溺水' : (category.includes('-') ? category.split('-').slice(1).join('-').trim() : (category.includes('火災') ? '火災' : ''));
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
