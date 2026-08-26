from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Callable
from uuid import uuid4

import ddddocr
from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from ambulance_bot.adapters import SiteAutomationResult
from ambulance_bot.chrome_startup import (
    add_worker_chrome_options,
    create_chrome_driver_with_retry,
    mark_driver_operation_active,
)
from ambulance_bot.civilpower_roster import HOME_RESCUE_UNIT, normalize_roster_members
from ambulance_bot.duty_credentials import task_login_credential_attempts
from ambulance_bot.failure_evidence import augment_failure_detail, capture_failure_artifacts, compact_failure_text
from ambulance_bot.models import AmbulanceReturnRequest, clean_case_address, normalize_hhmm
from ambulance_bot.profile_paths import runtime_profile_dir
from ambulance_bot.task_cancellation import TaskCancellationError
from ambulance_bot.window_layout import apply_tile


OA_LOGIN_URL = "https://oa.tyfd.gov.tw/login.php"
CIVILPOWER_BASE_URL = "https://civilpower.tyfd.gov.tw/TYCC/"
CIVILPOWER_SSO_LOGIN_URL = f"{CIVILPOWER_BASE_URL}Home/SSOLogin"
FIREMAN_URL = f"{CIVILPOWER_BASE_URL}Home/FireMan"
IO_WORK_LOG_URL = f"{CIVILPOWER_BASE_URL}Home/IOWorkLog"
WORK_LOG_URL = f"{CIVILPOWER_BASE_URL}Home/WorkLog"
SERVE_UNIT = "新坡分隊"
OUT_STATUS = "出"
IN_STATUS = "入"
OUT_REASON = "救護出勤"
IN_REASON = "救護返隊"
MAX_LOGIN_ATTEMPTS = 3
DEFAULT_WAIT_SECONDS = 15
WORK_LOG_FORM_INITIAL_WAIT_SECONDS = 10
WORK_LOG_FORM_RETRY_WAIT_SECONDS = 5
WORK_LOG_LIST_SELECTORS = (
    "#txt_Date_S",
    "#txt_Date_E",
    "#btn_Add",
)
WORK_LOG_FORM_SELECTORS = (
    "#txt_IODate_S",
    "#txt_IODate_E",
    "#btn_AddSltIOWorkLog",
)
OUTPUT_DIR = Path(
    os.getenv("CAPTCHA_OUTPUT_DIR")
    or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ambulance_return_bot" / "captcha"
)
ocr = ddddocr.DdddOcr(show_ad=False)


@dataclass(frozen=True, slots=True)
class CivilpowerTaskPlan:
    task_id: str
    case_id: str
    case_address: str
    member_id: str
    member_name: str
    member_title: str
    home_unit: str
    serve_unit: str
    out_date: str
    out_time: str
    in_date: str
    in_time: str
    out_reason: str
    in_reason: str
    duty_status_line: str
    case_reason: str = ""

    @property
    def case_search_start_hour(self) -> str:
        return self.out_time[:2]

    @property
    def case_search_end_hour(self) -> str:
        return self.in_time[:2]


def build_civilpower_task_plan(request: AmbulanceReturnRequest) -> CivilpowerTaskPlan:
    if request.service_type != "ems" or not request.volunteer_assist:
        raise ValueError("此任務未啟用義消協勤。")
    member_id = _clean_text(request.volunteer_assist_member_id)
    member_name = _clean_text(request.volunteer_assist_member_name)
    home_unit = _clean_text(request.volunteer_assist_member_unit) or HOME_RESCUE_UNIT
    if not member_id or not member_name:
        raise ValueError("義消協勤未選擇 NAS 名冊中的人員。")
    if home_unit != HOME_RESCUE_UNIT:
        raise ValueError(f"義消協勤人員所屬單位不符：預期={HOME_RESCUE_UNIT} 實際={home_unit}")
    out_time = normalize_hhmm(request.case_time)
    in_time = normalize_hhmm(request.return_time)
    if not _valid_hhmm(out_time):
        raise ValueError("義消協勤需要精準救護出勤時間。")
    if not _valid_hhmm(in_time):
        raise ValueError("義消協勤需要救護返隊時間。")
    out_date = request.service_case_date().strftime("%Y/%m/%d")
    in_date = request.service_return_date().strftime("%Y/%m/%d")
    return CivilpowerTaskPlan(
        task_id=_clean_text(request.task_id),
        case_id=_clean_text(request.case_id),
        case_address=clean_case_address(request.case_address),
        member_id=member_id,
        member_name=member_name,
        member_title=_clean_text(request.volunteer_assist_member_title),
        home_unit=home_unit,
        serve_unit=SERVE_UNIT,
        out_date=out_date,
        out_time=out_time,
        in_date=in_date,
        in_time=in_time,
        out_reason=OUT_REASON,
        in_reason=IN_REASON,
        duty_status_line=f"3.救護義消協勤:{member_name}",
        case_reason=_clean_text(request.case_reason),
    )


def civilpower_roster_refresh_due(
    artifacts_dir: Path,
    *,
    now: datetime | None = None,
    interval_seconds: int = 7 * 24 * 60 * 60,
) -> bool:
    path = Path(artifacts_dir) / "civilpower" / "roster_refresh.json"
    if not path.exists():
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        attempted_at = datetime.fromisoformat(str(payload.get("attempted_at") or ""))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return True
    if str(payload.get("status") or "") != "civilpower_roster_loaded":
        return True
    if payload.get("pagination_complete") is not True:
        return True
    current = now or datetime.now()
    return (current - attempted_at).total_seconds() >= max(int(interval_seconds), 60)


def refresh_civilpower_roster(
    artifacts_dir: Path,
    *,
    profile_name: str = "civilpower_roster_profile",
    tile_name: str = "volunteer_assist",
    driver=None,
) -> dict[str, object]:
    attempted_at = datetime.now().isoformat(timespec="seconds")
    active_driver = driver
    try:
        if active_driver is None:
            active_driver = login_civilpower_and_get_driver(
                profile_name=profile_name,
                tile_name=tile_name,
            )
        members = query_civilpower_roster(active_driver)
        if not members:
            raise RuntimeError("民力運用系統未找到可用的大園救護分隊義消名冊。")
        report = {
            "status": "civilpower_roster_loaded",
            "detail": f"已由民力運用系統更新 {len(members)} 位大園救護分隊可選義消。",
            "source": "public_duty_pc_worker",
            "attempted_at": attempted_at,
            "last_success_at": attempted_at,
            "pagination_complete": True,
            "members": members,
        }
    except Exception as exc:
        report = {
            "status": "civilpower_roster_failed",
            "detail": f"義消名冊更新失敗：{exc}",
            "source": "public_duty_pc_worker",
            "attempted_at": attempted_at,
            "members": [],
        }
    _write_json_atomic(Path(artifacts_dir) / "civilpower" / "roster_refresh.json", report)
    return report


def query_civilpower_roster(driver) -> list[dict[str, str]]:
    driver.get(FIREMAN_URL)
    wait = WebDriverWait(driver, DEFAULT_WAIT_SECONDS)
    _wait_for_civilpower_page(driver, wait)
    _select_option_containing(wait, "#ddl_Prop", "救護", clear_others=True)
    _select_option_containing(wait, "#ddl_Type3Unit", HOME_RESCUE_UNIT)
    _click(wait, "#btn_Query")
    _wait_for_rows(driver, wait)
    raw_members = _read_civilpower_roster_page(driver)
    while _open_next_civilpower_roster_page(driver, wait):
        raw_members.extend(_read_civilpower_roster_page(driver))
    return normalize_roster_members(raw_members)


def _read_civilpower_roster_page(driver) -> list[dict[str, str]]:
    raw_members: list[dict[str, str]] = []
    for row in _table_rows(driver):
        cells = [_clean_text(cell.text) for cell in _row_cells(row)]
        if len(cells) < 3:
            continue
        unit, title, name = cells[:3]
        raw_members.append({"unit": unit, "title": title, "name": name})
    return raw_members


def _open_next_civilpower_roster_page(driver, wait: WebDriverWait) -> bool:
    next_links = driver.find_elements(
        By.CSS_SELECTOR,
        "#tableresult .pagination li.PagedList-skipToNext a[rel='next']",
    )
    if not next_links:
        return False
    current_page = _civilpower_roster_page_marker(driver)
    next_links[0].click()
    wait.until(lambda current: _civilpower_roster_page_marker(current) != current_page)
    _wait_for_rows(driver, wait)
    return True


def _civilpower_roster_page_marker(driver) -> str:
    active_pages = driver.find_elements(By.CSS_SELECTOR, "#tableresult .pagination li.active a")
    if active_pages:
        return _clean_text(active_pages[0].text)
    return str(getattr(driver, "current_url", "") or "")


def open_civilpower_from_oa_dashboard(driver, wait: WebDriverWait) -> None:
    _click(wait, "#moduleBox_other")
    sso_entry = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, ".custom_icon[onclick*='SSOLogin']"))
    )
    existing_handles = set(driver.window_handles)
    sso_entry.click()
    wait.until(
        lambda current: any(handle not in existing_handles for handle in current.window_handles)
        or CIVILPOWER_SSO_LOGIN_URL in str(current.current_url or "")
    )
    new_handles = [handle for handle in driver.window_handles if handle not in existing_handles]
    if new_handles:
        driver.switch_to.window(new_handles[-1])
    _wait_for_civilpower_page(driver, wait)


def login_civilpower_and_get_driver(
    *,
    request: AmbulanceReturnRequest | None = None,
    profile_name: str = "civilpower_profile",
    debugger_port: int | None = None,
    tile_name: str = "",
) -> webdriver.Chrome:
    credentials = task_login_credential_attempts(request, duty_password=False)
    if not credentials:
        raise RuntimeError("尚未取得可用 Worker 帳密，無法登入消防局內部入口網。")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    options = Options()
    options.add_argument("--window-size=1280,900")
    options.add_argument(f"--user-data-dir={runtime_profile_dir(profile_name)}")
    add_worker_chrome_options(options)
    if debugger_port:
        options.add_argument(f"--remote-debugging-port={debugger_port}")
    options.add_experimental_option("detach", True)
    driver = create_chrome_driver_with_retry(options, "民力系統", fresh_session=True)
    timeout = int(os.getenv("SELENIUM_PAGE_LOAD_TIMEOUT_SECONDS", "45"))
    driver.set_page_load_timeout(timeout)
    driver.set_script_timeout(timeout)
    apply_tile(driver, tile_name)
    errors: list[str] = []
    try:
        for credential, source in credentials:
            for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
                try:
                    _login_once(driver, credential.user_id, credential.password, attempt)
                    open_civilpower_from_oa_dashboard(driver, WebDriverWait(driver, DEFAULT_WAIT_SECONDS))
                    return driver
                except Exception as exc:
                    errors.append(f"{source}第 {attempt} 次：{compact_failure_text(exc, maximum=120)}")
        raise RuntimeError("內部入口網登入失敗，已依帳號優先順序重試：" + "；".join(errors))
    except Exception:
        if os.getenv("CIVILPOWER_CLOSE_BROWSER_ON_LOGIN_FAILURE", "false").strip().lower() in {"1", "true", "yes", "on"}:
            driver.quit()
        raise


def run_civilpower_task(
    request: AmbulanceReturnRequest,
    artifacts_dir: Path,
    *,
    profile_name: str = "civilpower_profile",
    debugger_port: int | None = None,
    tile_name: str = "volunteer_assist",
    cancel_check: Callable[[], None] | None = None,
    progress: Callable[[str], None] | None = None,
    driver=None,
) -> SiteAutomationResult:
    started_at = time.monotonic()
    active_driver = driver
    current_stage = "準備登入民力運用管理系統"

    def report_stage(stage: str) -> None:
        nonlocal current_stage
        current_stage = stage
        _report_progress(progress, stage)

    mark_driver_operation_active(active_driver)
    try:
        plan = build_civilpower_task_plan(request)
        _raise_if_cancelled(cancel_check)
        if active_driver is None:
            report_stage("登入內部入口網")
            active_driver = login_civilpower_and_get_driver(
                request=request,
                profile_name=profile_name,
                debugger_port=debugger_port,
                tile_name=tile_name,
            )
        mark_driver_operation_active(active_driver)
        checkpoint = _load_task_checkpoint(artifacts_dir, plan)
        report_stage("確認救護出勤出入登記")
        _ensure_io_record(active_driver, plan, OUT_STATUS, checkpoint, cancel_check=cancel_check)
        _save_task_checkpoint(artifacts_dir, plan, checkpoint)
        report_stage("確認救護返隊出入登記")
        _ensure_io_record(active_driver, plan, IN_STATUS, checkpoint, cancel_check=cancel_check)
        _save_task_checkpoint(artifacts_dir, plan, checkpoint)
        report_stage("案件代入")
        _ensure_work_log(
            active_driver,
            request,
            plan,
            checkpoint,
            cancel_check=cancel_check,
            progress=report_stage,
        )
        _save_task_checkpoint(artifacts_dir, plan, checkpoint)
        return SiteAutomationResult(
            "volunteer_assist",
            "民力系統",
            "volunteer_assist_saved",
            "已新增救護出勤／救護返隊出入登記，案件代入工作紀錄並回查確認。",
        )
    except TaskCancellationError:
        raise
    except Exception as exc:
        detail = _civilpower_failure_detail(current_stage, exc)
        evidence: dict[str, object] = {}
        if active_driver is not None:
            try:
                evidence = capture_failure_artifacts(
                    active_driver,
                    Path(artifacts_dir) / "selenium",
                    request.task_id,
                    "volunteer_assist",
                    exception=exc,
                    target_url=str(getattr(active_driver, "current_url", "") or CIVILPOWER_BASE_URL),
                    elapsed_seconds=time.monotonic() - started_at,
                )
                detail = augment_failure_detail(detail, evidence)
                if current_stage not in detail:
                    detail = f"{current_stage}：{detail}"
            except Exception as capture_exc:
                print(
                    "[civilpower] failure evidence capture skipped: "
                    f"{capture_exc.__class__.__name__}",
                    flush=True,
                )
                detail = f"{compact_failure_text(detail)} [failure_capture_error:{capture_exc.__class__.__name__}]"
        return SiteAutomationResult(
            "volunteer_assist",
            "民力系統",
            "volunteer_assist_failed",
            detail,
            **_civilpower_failure_diagnostics(current_stage, exc, evidence),
        )
    finally:
        mark_driver_operation_active(active_driver, False)


def _civilpower_failure_detail(stage: str, exc: BaseException) -> str:
    if isinstance(exc, TimeoutException):
        context = _controlled_timeout_context(exc)
        if context:
            return f"民力系統{stage}逾時：{context}"
        return f"民力系統{stage}逾時，網頁未在 {DEFAULT_WAIT_SECONDS} 秒內完成預期操作。"
    message = compact_failure_text(exc, maximum=180)
    return f"民力系統{stage}失敗：{message or exc.__class__.__name__}"


def _civilpower_failure_diagnostics(
    stage: str,
    exc: BaseException,
    evidence: dict[str, object],
) -> dict[str, str]:
    browser_reason = _clean_text(evidence.get("reason"))
    browser_next_action = _clean_text(evidence.get("next_action"))
    if isinstance(exc, TimeoutException):
        reason = browser_reason or _controlled_timeout_context(exc) or f"民力網站在{stage}等待逾時，未出現預期的選取視窗或資料結果。"
        next_action = browser_next_action or "保留目前畫面與截圖，再單獨重跑民力系統；程式不會略過資料驗證。"
    else:
        reason = compact_failure_text(exc, maximum=180) or f"民力系統在{stage}發生未預期錯誤。"
        next_action = "保留目前畫面與截圖，確認資料無誤後單獨重跑民力系統。"
    return {
        "failure_stage": stage,
        "failure_reason": reason,
        "next_action": next_action,
        "exception_type": exc.__class__.__name__,
    }


def _controlled_timeout_context(exc: TimeoutException) -> str:
    message = compact_failure_text(exc, maximum=180)
    if message.startswith("Message:"):
        message = message.removeprefix("Message:").strip()
    controlled_markers = (
        "工作紀錄簿頁面未完整載入",
        "選取按鈕未就緒",
        "選取視窗未出現",
    )
    if any(marker in message for marker in controlled_markers):
        return message
    return ""


def _login_once(driver, account: str, password: str, attempt: int) -> None:
    driver.get(OA_LOGIN_URL)
    wait = WebDriverWait(driver, DEFAULT_WAIT_SECONDS)
    _set_input(wait, "#login_name", account)
    _set_input(wait, "#password", password)
    captcha = wait.until(EC.visibility_of_element_located((By.ID, "checkcodeImg")))
    wait.until(lambda _driver: captcha.get_attribute("src") and captcha.size.get("width", 0) > 0)
    image_path = OUTPUT_DIR / f"civilpower_oa_captcha_attempt_{attempt}.png"
    Image.open(BytesIO(captcha.screenshot_as_png)).convert("RGB").save(image_path)
    captcha_text = "".join(character for character in ocr.classification(image_path.read_bytes()) if character.isalnum())
    if not captcha_text:
        raise RuntimeError("內部入口網驗證碼 OCR 未辨識到文字。")
    _set_input(wait, "#verify_code", captcha_text)
    _click(wait, "#loginBtn")
    wait.until(lambda current: not _is_oa_login_page(current))


def _is_oa_login_page(driver) -> bool:
    return bool(driver.find_elements(By.CSS_SELECTOR, "#login_name, #verify_code, #loginBtn"))


def _wait_for_civilpower_page(driver, wait: WebDriverWait) -> None:
    def ready(current) -> bool:
        if _is_oa_login_page(current) or _is_civilpower_login_page(current):
            return False
        body_text = _clean_text(current.find_element(By.TAG_NAME, "body").text)
        return "民力運用" in body_text or "/TYCC/" in str(current.current_url or "")

    wait.until(ready)


def _is_civilpower_login_page(driver) -> bool:
    return _clean_text(getattr(driver, "title", "")).lower() == "login"


def _ensure_io_record(
    driver,
    plan: CivilpowerTaskPlan,
    status: str,
    checkpoint: dict[str, object],
    *,
    cancel_check: Callable[[], None] | None,
) -> None:
    marker = "out" if status == OUT_STATUS else "in"
    _raise_if_cancelled(cancel_check)
    if _find_io_record(driver, plan, status, wait_for_match=True):
        checkpoint[f"{marker}_verified"] = True
        return
    wait = WebDriverWait(driver, DEFAULT_WAIT_SECONDS)
    _click(wait, "#btn_Add")
    _wait_visible(wait, "#jqxAddWindow")
    previous_serve_unit_signature = _wait_for_jqx_combobox_option_signature(
        driver,
        wait,
        "#txt_AddServeUnit",
    )
    _select_jqx_combobox(driver, wait, "#txt_AddUnit", plan.home_unit)
    _wait_for_io_form_dependencies(
        driver,
        wait,
        plan,
        previous_serve_unit_signature=previous_serve_unit_signature,
    )
    _select_jqx_combobox(driver, wait, "#txt_AddServeUnit", plan.serve_unit)
    _select_io_person(driver, wait, plan)
    date_text = plan.out_date if status == OUT_STATUS else plan.in_date
    time_text = plan.out_time if status == OUT_STATUS else plan.in_time
    _set_input(wait, "#txt_AddLogDate", date_text)
    _set_input(wait, "#txt_AddLogHour", time_text[:2])
    _set_input(wait, "#txt_AddLogMin", time_text[2:])
    _select_jqx_combobox(driver, wait, "#txt_AddServeUnit", plan.serve_unit)
    _select_option_containing(wait, "#ddl_AddIO", status)
    _set_input(wait, "#txt_AddReason", plan.out_reason if status == OUT_STATUS else plan.in_reason)
    _wait_for_io_record_form_values(driver, wait, plan, status)
    _raise_if_cancelled(cancel_check)
    _click(wait, "#btn_IOWorkLogAdd")
    _wait_after_save(driver, wait, "#jqxAddWindow")
    _raise_if_cancelled(cancel_check)
    if not _find_io_record(driver, plan, status, wait_for_match=True, reload_page=False):
        raise RuntimeError(f"出入登記簿儲存後回查不到{status}／{plan.out_reason if status == OUT_STATUS else plan.in_reason}紀錄。")
    checkpoint[f"{marker}_verified"] = True


def _open_io_work_log(driver) -> None:
    driver.get(IO_WORK_LOG_URL)
    _wait_for_civilpower_page(driver, WebDriverWait(driver, DEFAULT_WAIT_SECONDS))


def _find_io_record(
    driver,
    plan: CivilpowerTaskPlan,
    status: str,
    *,
    wait_for_match: bool = False,
    reload_page: bool = True,
) -> bool:
    if reload_page:
        _open_io_work_log(driver)
    wait = WebDriverWait(driver, DEFAULT_WAIT_SECONDS)
    date_text = plan.out_date if status == OUT_STATUS else plan.in_date
    time_text = plan.out_time if status == OUT_STATUS else plan.in_time
    _set_if_present(driver, wait, "#txt_Name", plan.member_name)
    _set_if_present(driver, wait, "#txt_Date_S", date_text)
    _set_if_present(driver, wait, "#txt_Date_E", date_text)
    _select_option_containing_if_present(driver, wait, "#ddl_IO", status)
    _click_if_present(driver, wait, "#btn_Query")
    tokens = [
        plan.member_name,
        plan.home_unit,
        plan.serve_unit,
        plan.out_reason if status == OUT_STATUS else plan.in_reason,
        time_text,
    ]
    rows = _matching_table_rows(driver, tokens)
    if not rows and wait_for_match:
        try:
            rows = wait.until(lambda current: _matching_table_rows(current, tokens) or False)
        except TimeoutException:
            return False
    if len(rows) > 1:
        raise RuntimeError(f"出入登記簿找到多筆相同{status}紀錄，無法安全判定。")
    return bool(rows)


def _select_io_person(driver, wait: WebDriverWait, plan: CivilpowerTaskPlan) -> None:
    _click(wait, "#btn_AddSltMan")
    tokens = [plan.home_unit, plan.member_name]
    if plan.member_title:
        tokens.append(plan.member_title)
    dialog, row = _wait_for_io_person_dialog_row(driver, wait, tokens)
    _click_dialog_row(driver, row)
    _confirm_dialog(driver, wait, dialog)
    _wait_for_io_person_value(driver, wait, plan.member_name)


def _ensure_work_log(
    driver,
    request: AmbulanceReturnRequest,
    plan: CivilpowerTaskPlan,
    checkpoint: dict[str, object],
    *,
    cancel_check: Callable[[], None] | None,
    progress: Callable[[str], None] | None = None,
) -> None:
    _raise_if_cancelled(cancel_check)
    _report_progress(progress, "查詢既有工作紀錄")
    if _find_work_log_record(driver, plan):
        checkpoint["work_log_verified"] = True
        return
    wait = WebDriverWait(driver, DEFAULT_WAIT_SECONDS)
    _report_progress(progress, "開啟工作紀錄簿新增表單")
    _click(wait, "#btn_Add")
    _wait_visible(wait, "#jqxAddWindow")
    _wait_for_work_log_add_controls(driver, wait)
    _report_progress(progress, "選取救護出勤登記")
    _select_out_io_record_for_work_log(driver, wait, plan)
    _raise_if_cancelled(cancel_check)
    _report_progress(progress, "案件代入")
    _import_work_log_case(driver, wait, plan)
    _report_progress(progress, "驗證案件代入")
    _assert_imported_work_log_values(driver, plan)
    _raise_if_cancelled(cancel_check)
    _report_progress(progress, "儲存工作紀錄")
    _click(wait, "#btn_WorkLogAdd")
    _wait_after_save(driver, wait, "#jqxAddWindow")
    _raise_if_cancelled(cancel_check)
    _report_progress(progress, "工作紀錄回查")
    if not _find_work_log_record(driver, plan):
        raise RuntimeError("工作紀錄簿儲存後回查不到本次救護義消協勤紀錄。")
    checkpoint["work_log_verified"] = True


def _select_out_io_record_for_work_log(driver, wait: WebDriverWait, plan: CivilpowerTaskPlan) -> None:
    _set_input(wait, "#txt_IODate_S", plan.out_date)
    _set_input(wait, "#txt_IODate_E", plan.out_date)
    dialog = _open_selection_dialog(
        driver,
        wait,
        "#btn_AddSltIOWorkLog",
        "救護出勤登記",
    )
    _select_dialog_row(
        driver,
        wait,
        dialog,
        [plan.member_name, plan.home_unit, plan.out_reason, plan.out_time],
    )
    _confirm_dialog(driver, wait, dialog)
    if not _control_value(driver, "#hf_AddIOLogIDs"):
        raise RuntimeError("工作紀錄簿未選取救護出勤的出入登記。")


def _import_work_log_case(driver, wait: WebDriverWait, plan: CivilpowerTaskPlan) -> None:
    checkbox = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#cb_SearchCase")))
    if not checkbox.is_selected():
        driver.execute_script("arguments[0].click();", checkbox)
    _set_input(wait, "#txt_SearchDate_S", plan.out_date)
    _set_input(wait, "#txt_SearchDate_S_H", plan.case_search_start_hour)
    _set_input(wait, "#txt_SearchDate_E", plan.in_date)
    _set_input(wait, "#txt_SearchDate_E_H", plan.case_search_end_hour)
    dialog = _open_selection_dialog(
        driver,
        wait,
        "#btn_CaseSlt",
        "案件代入",
        dialog_title="案件選取",
    )
    candidates: list[list[str]] = []
    if plan.case_id:
        candidates.append([plan.case_id])
    if plan.case_address:
        candidates.append([plan.case_address])
    if plan.case_reason:
        candidates.append([plan.case_reason, plan.out_time])
    last_error: RuntimeError | None = None
    for tokens in candidates:
        try:
            row = _wait_for_dialog_row(wait, dialog, tokens)
            break
        except RuntimeError as exc:
            last_error = exc
    else:
        if last_error is not None:
            raise last_error
        raise RuntimeError("案件代入缺少可比對的案件資料。")
    if _click_dialog_row_action(driver, row, "選取"):
        _wait_for_dialog_close(wait, dialog)
        return
    _click_dialog_row(driver, row)
    _confirm_dialog(driver, wait, dialog)


def _assert_imported_work_log_values(driver, plan: CivilpowerTaskPlan) -> None:
    expected = {
        "#txt_AddDisDate": plan.out_date,
        "#txt_AddBackDate": plan.in_date,
        "#txt_AddBackHour": plan.in_time[:2],
        "#txt_AddBackMin": plan.in_time[2:],
    }
    for selector, expected_value in expected.items():
        actual = _control_value(driver, selector)
        if not _same_value(actual, expected_value):
            raise RuntimeError(f"案件代入後欄位不符：{selector} 預期={expected_value} 實際={actual or '空白'}")
    dispatch_time = normalize_hhmm(
        _control_value(driver, "#txt_AddDisHour") + _control_value(driver, "#txt_AddDisMin")
    )
    if not _valid_hhmm(dispatch_time):
        raise RuntimeError("案件代入後未帶入有效案件派遣時間。")
    status_text = _control_value(driver, "#txt_AddStat")
    if plan.duty_status_line not in status_text:
        raise RuntimeError(f"案件代入後未帶入第一站工作紀錄的 {plan.duty_status_line}。")


def _find_work_log_record(driver, plan: CivilpowerTaskPlan) -> bool:
    wait = _open_work_log_form(driver)
    _set_if_present(driver, wait, "#txt_Date_S", plan.out_date)
    _set_if_present(driver, wait, "#txt_Date_E", plan.in_date)
    _click_if_present(driver, wait, "#btn_Query")
    tokens = [plan.member_name, plan.out_reason, plan.out_time]
    if plan.case_id:
        rows = _matching_table_rows(driver, [plan.member_name, plan.case_id])
        if rows:
            return len(rows) == 1
    if plan.case_address:
        rows = _matching_table_rows(driver, [plan.member_name, plan.case_address])
        if rows:
            return len(rows) == 1
    rows = _matching_table_rows(driver, tokens)
    if len(rows) > 1:
        raise RuntimeError("工作紀錄簿找到多筆相同義消協勤紀錄，無法安全判定。")
    return bool(rows)


def _open_selection_dialog(
    driver,
    wait: WebDriverWait,
    selector: str,
    label: str,
    *,
    dialog_title: str = "",
):
    previous_dialog_ids = _visible_dialog_ids(driver)

    def visible_dialog(active_wait: WebDriverWait):
        options: dict[str, object] = {}
        if previous_dialog_ids:
            options["previous_dialog_ids"] = previous_dialog_ids
        if dialog_title:
            options["title_text"] = dialog_title
        return _visible_dialog(driver, active_wait, **options)

    _click_selection_trigger(driver, wait, selector, label)
    try:
        return visible_dialog(wait)
    except TimeoutException:
        _click_selection_trigger(driver, wait, selector, label)
        retry_wait = WebDriverWait(driver, min(5, DEFAULT_WAIT_SECONDS))
        try:
            return visible_dialog(retry_wait)
        except TimeoutException as exc:
            raise TimeoutException(f"{label}選取視窗未出現，已安全重試一次。") from exc


def _open_work_log_form(driver) -> WebDriverWait:
    driver.get(WORK_LOG_URL)
    initial_wait = WebDriverWait(driver, min(WORK_LOG_FORM_INITIAL_WAIT_SECONDS, DEFAULT_WAIT_SECONDS))
    _wait_for_civilpower_page(driver, initial_wait)
    try:
        _wait_for_work_log_controls(driver, initial_wait)
    except TimeoutException:
        driver.refresh()
        retry_wait = WebDriverWait(driver, min(WORK_LOG_FORM_RETRY_WAIT_SECONDS, DEFAULT_WAIT_SECONDS))
        _wait_for_civilpower_page(driver, retry_wait)
        try:
            _wait_for_work_log_controls(driver, retry_wait)
        except TimeoutException as exc:
            raise TimeoutException(_incomplete_work_log_page_message(driver)) from exc
    return WebDriverWait(driver, DEFAULT_WAIT_SECONDS)


def _wait_for_work_log_controls(driver, wait: WebDriverWait) -> None:
    wait.until(
        lambda current: all(
            current.find_elements(By.CSS_SELECTOR, selector)
            for selector in WORK_LOG_LIST_SELECTORS
        )
    )


def _wait_for_work_log_add_controls(driver, wait: WebDriverWait) -> None:
    wait.until(
        lambda current: all(
            current.find_elements(By.CSS_SELECTOR, selector)
            for selector in WORK_LOG_FORM_SELECTORS
        )
    )


def _incomplete_work_log_page_message(driver) -> str:
    try:
        missing = [
            selector
            for selector in WORK_LOG_LIST_SELECTORS
            if not driver.find_elements(By.CSS_SELECTOR, selector)
        ]
    except Exception:
        missing = list(WORK_LOG_LIST_SELECTORS)
    page_url = _clean_text(str(getattr(driver, "current_url", "") or ""))
    location = page_url[:120] or "未知頁面"
    controls = "、".join(missing) or "必要欄位"
    return f"工作紀錄簿頁面未完整載入（缺少 {controls}；頁面={location}）。"


def _click_selection_trigger(driver, wait: WebDriverWait, selector: str, label: str) -> None:
    try:
        _click(wait, selector)
    except TimeoutException as exc:
        page_url = _clean_text(str(getattr(driver, "current_url", "") or ""))
        location = page_url[:120] or "未知頁面"
        raise TimeoutException(f"{label}選取按鈕未就緒（{selector}；頁面={location}）。") from exc


def _visible_dialog(
    driver,
    wait: WebDriverWait,
    *,
    title_text: str = "",
    previous_dialog_ids: set[str] | None = None,
):
    expected_title = _clean_text(title_text)
    prior_ids = previous_dialog_ids or set()

    def find_dialog(current):
        candidates = current.find_elements(By.CSS_SELECTOR, ".jqx-window, [role='dialog']")
        visible = [candidate for candidate in candidates if candidate.is_displayed()]
        if prior_ids:
            newly_opened = [candidate for candidate in visible if _dialog_identity(candidate) not in prior_ids]
            if not newly_opened:
                return False
            visible = newly_opened
        if expected_title:
            titled = [candidate for candidate in visible if _dialog_has_title(candidate, expected_title)]
            return titled[-1] if titled else False
        return visible[-1] if visible else False

    return wait.until(find_dialog)


def _visible_dialog_ids(driver) -> set[str]:
    try:
        candidates = list(driver.find_elements(By.CSS_SELECTOR, ".jqx-window, [role='dialog']"))
    except Exception:
        return set()
    dialog_ids: set[str] = set()
    for candidate in candidates:
        try:
            if candidate.is_displayed():
                dialog_id = _dialog_identity(candidate)
                if dialog_id:
                    dialog_ids.add(dialog_id)
        except Exception:
            continue
    return dialog_ids


def _dialog_identity(dialog) -> str:
    try:
        return _clean_text(str(getattr(dialog, "id", "") or ""))
    except Exception:
        return ""


def _dialog_has_title(dialog, expected_title: str) -> bool:
    try:
        headers = dialog.find_elements(By.CSS_SELECTOR, ".jqx-window-header, .modal-title, [role='heading']")
    except Exception:
        return False
    for header in headers:
        try:
            if expected_title in _clean_text(header.text):
                return True
        except Exception:
            continue
    return False


def _wait_for_io_person_dialog_row(driver, wait: WebDriverWait, tokens: list[str]):
    def find_row(current):
        dialogs = [
            dialog
            for dialog in current.find_elements(By.CSS_SELECTOR, "#jqxSltWindow")
            if dialog.is_displayed()
        ]
        if not dialogs:
            return False
        dialog = dialogs[-1]
        rows = _matching_table_rows(dialog, tokens)
        if len(rows) > 1:
            raise RuntimeError("人員選取視窗找到多筆符合條件的紀錄，無法安全選取：" + "、".join(tokens))
        if not rows:
            return False
        return dialog, rows[0]

    return wait.until(find_row)


def _wait_for_io_person_value(driver, wait: WebDriverWait, member_name: str) -> None:
    wait.until(lambda current: member_name in _control_value(current, "#txt_AddVolFMan"))


def _select_dialog_row(driver, wait: WebDriverWait, dialog, required_tokens: list[str]) -> None:
    row = _wait_for_dialog_row(wait, dialog, required_tokens)
    _click_dialog_row(driver, row)


def _wait_for_dialog_row(wait: WebDriverWait, dialog, required_tokens: list[str]):
    def find_row(_current):
        rows = _matching_table_rows(dialog, required_tokens)
        if len(rows) > 1:
            raise RuntimeError("選取視窗找到多筆符合條件的紀錄，無法安全選取：" + "、".join(required_tokens))
        return rows[0] if rows else False

    try:
        row = wait.until(find_row)
    except TimeoutException as exc:
        raise RuntimeError(
            f"選取視窗在 {DEFAULT_WAIT_SECONDS} 秒內找不到符合條件的紀錄：" + "、".join(required_tokens)
        ) from exc
    return row


def _click_dialog_row_action(driver, row, action_text: str) -> bool:
    expected_text = _clean_text(action_text).replace(" ", "")
    candidates = row.find_elements(
        By.CSS_SELECTOR,
        "input[type='button'], input[type='submit'], button, a, [role='button'], .jqx-button",
    )
    for candidate in candidates:
        try:
            candidate_text = _clean_text(
                " ".join(
                    str(candidate.get_attribute(attribute) or "")
                    for attribute in ("value", "title", "aria-label")
                )
                + " "
                + str(candidate.text or "")
            ).replace(" ", "")
            selectable = candidate.is_displayed() and candidate.is_enabled()
        except Exception:
            continue
        if not selectable or candidate_text != expected_text:
            continue
        try:
            candidate.click()
        except Exception:
            try:
                clicked = driver.execute_script("arguments[0].click(); return true;", candidate)
            except Exception as exc:
                raise RuntimeError(f"選取視窗的「{action_text}」按鈕無法點選。") from exc
            if not clicked:
                raise RuntimeError(f"選取視窗的「{action_text}」按鈕無法點選。")
        return True
    return False


def _click_dialog_row(driver, row) -> None:
    controls = row.find_elements(By.CSS_SELECTOR, "input[type='checkbox'], input[type='radio'], button, input[type='button']")
    for control in controls:
        try:
            if control.is_displayed() and control.is_enabled():
                control.click()
                return
        except Exception:
            continue
    try:
        row.click()
        return
    except Exception:
        pass
    try:
        dispatched = driver.execute_script(
            """
            const row = arguments[0];
            const cells = Array.from(row.querySelectorAll("[role='gridcell'], .jqx-grid-cell"));
            const target = cells.find((cell) => cell.getClientRects().length) || row;
            if (!target || !target.isConnected) return false;
            target.scrollIntoView({block: 'center', inline: 'nearest'});
            for (const type of ['mousedown', 'mouseup', 'click']) {
              target.dispatchEvent(new MouseEvent(type, {
                bubbles: true,
                cancelable: true,
                view: window,
                button: 0,
                buttons: type === 'mousedown' ? 1 : 0,
              }));
            }
            return true;
            """,
            row,
        )
        if dispatched:
            return
    except Exception as exc:
        raise RuntimeError("選取視窗的符合紀錄無法選取。") from exc
    raise RuntimeError("選取視窗的符合紀錄無法選取。")


def _confirm_dialog(driver, wait: WebDriverWait, dialog) -> None:
    if _is_stale(dialog) or not dialog.is_displayed():
        return
    candidates = driver.find_elements(
        By.CSS_SELECTOR,
        "input[type='button'], input[type='submit'], button, a, [role='button'], .jqx-button",
    )
    for candidate in candidates:
        try:
            text = _clean_text(
                " ".join(
                    str(candidate.get_attribute(attribute) or "")
                    for attribute in ("value", "title", "aria-label")
                )
                + " "
                + str(candidate.text or "")
            )
            selectable = candidate.is_displayed() and candidate.is_enabled()
        except Exception:
            continue
        if selectable and "確認選取" in text.replace(" ", ""):
            try:
                candidate.click()
            except Exception:
                try:
                    clicked = driver.execute_script("arguments[0].click(); return true;", candidate)
                except Exception as exc:
                    raise RuntimeError("選取視窗的確認按鈕無法點選。") from exc
                if not clicked:
                    raise RuntimeError("選取視窗的確認按鈕無法點選。")
            _wait_for_dialog_close(wait, dialog)
            return
    raise RuntimeError("選取視窗找不到「確認選取」按鈕。")


def _wait_for_dialog_close(wait: WebDriverWait, dialog) -> None:
    wait.until(lambda _current: _is_stale(dialog) or not dialog.is_displayed())


def _wait_after_save(driver, wait: WebDriverWait, modal_selector: str) -> None:
    try:
        wait.until(lambda current: not _element_displayed(current, modal_selector))
    except TimeoutException:
        body_text = _clean_text(driver.find_element(By.TAG_NAME, "body").text)
        if any(token in body_text for token in ("失敗", "錯誤", "請輸入", "必填")):
            raise RuntimeError("民力運用系統未接受儲存：" + body_text[-300:])
    _dismiss_save_success_dialog(driver, wait)


def _dismiss_save_success_dialog(driver, wait: WebDriverWait) -> None:
    dialog = _visible_save_success_dialog(driver)
    if dialog is None:
        try:
            short_wait = WebDriverWait(driver, min(3, DEFAULT_WAIT_SECONDS))
            dialog = short_wait.until(lambda current: _visible_save_success_dialog(current) or False)
        except TimeoutException:
            return
    try:
        clicked = _click_dialog_row_action(driver, dialog, "確定")
    except RuntimeError as exc:
        raise RuntimeError("民力系統新增成功提示的「確定」按鈕無法點選。") from exc
    if not clicked:
        raise RuntimeError("民力系統顯示新增成功，但找不到「確定」按鈕。")
    _wait_for_dialog_close(wait, dialog)


def _visible_save_success_dialog(driver):
    candidates = driver.find_elements(By.CSS_SELECTOR, ".swal2-popup, .sweet-alert, [role='dialog']")
    for candidate in candidates:
        try:
            text = _clean_text(candidate.text)
            if candidate.is_displayed() and any(
                marker in text for marker in ("新增成功", "儲存成功", "存檔成功", "操作成功")
            ):
                return candidate
        except Exception:
            continue
    return None


def _matching_table_rows(driver, required_tokens: list[str]) -> list[object]:
    return [
        row
        for row in _table_rows(driver)
        if all(_token_matches(_clean_text(row.text), token) for token in required_tokens if token)
    ]


def _table_rows(driver) -> list[object]:
    rows: list[object] = []
    selectors = "table tbody tr, table tr, [role='row'], .jqx-grid-row"
    seen_ids: set[str] = set()
    for row in driver.find_elements(By.CSS_SELECTOR, selectors):
        try:
            row_id = str(getattr(row, "id", "") or "")
            if row_id and row_id in seen_ids:
                continue
            if row_id:
                seen_ids.add(row_id)
            if row.is_displayed() and _row_cells(row):
                rows.append(row)
        except Exception:
            continue
    return rows


def _row_cells(row) -> list[object]:
    return row.find_elements(By.CSS_SELECTOR, "td, [role='gridcell'], .jqx-grid-cell")


def _wait_for_rows(driver, wait: WebDriverWait) -> None:
    wait.until(lambda current: bool(_table_rows(current)))


def _wait_for_io_form_dependencies(
    driver,
    wait: WebDriverWait,
    plan: CivilpowerTaskPlan,
    *,
    previous_serve_unit_signature: tuple[tuple[str, str], ...] | None,
) -> None:
    wait.until(
        lambda current: _jqx_combobox_reloaded_with_option(
            current,
            "#txt_AddServeUnit",
            plan.serve_unit,
            previous_serve_unit_signature,
        )
    )
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#btn_AddSltMan")))


def _wait_for_jqx_combobox_option_signature(
    driver,
    wait: WebDriverWait,
    selector: str,
) -> tuple[tuple[str, str], ...]:
    signature: tuple[tuple[str, str], ...] | None = None

    def ready(current) -> bool:
        nonlocal signature
        signature = _jqx_combobox_option_signature(current, selector)
        return signature is not None

    wait.until(ready)
    if signature is None:
        raise RuntimeError(f"民力系統單位下拉元件尚未就緒：{selector}")
    return signature


def _jqx_combobox_reloaded_with_option(
    driver,
    selector: str,
    text: str,
    previous_signature: tuple[tuple[str, str], ...] | None,
) -> bool:
    signature = _jqx_combobox_option_signature(driver, selector)
    if signature is None:
        return _jqx_combobox_option_ready(driver, selector, text)
    if previous_signature is not None and signature == previous_signature:
        return False
    expected = _clean_text(text)
    return any(label == expected or value == expected for label, value in signature)


def _jqx_combobox_option_signature(driver, selector: str) -> tuple[tuple[str, str], ...] | None:
    try:
        element = driver.find_element(By.CSS_SELECTOR, selector)
    except NoSuchElementException:
        return None
    try:
        raw_items = driver.execute_script(
            """
            const outer = arguments[0];
            const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            if (!window.jQuery || !window.jQuery.fn || !window.jQuery.fn.jqxComboBox) return null;
            try {
              const items = window.jQuery(outer).jqxComboBox('getItems') || [];
              return items.map((item) => [clean(item.label), clean(item.value)]);
            } catch (_) {
              return null;
            }
            """,
            element,
        )
    except StaleElementReferenceException:
        return None
    if raw_items is None:
        return None
    return tuple(
        (_clean_text(item[0]), _clean_text(item[1]))
        for item in raw_items
        if isinstance(item, (list, tuple)) and len(item) >= 2
    )


def _wait_for_jqx_combobox_option(driver, wait: WebDriverWait, selector: str, text: str) -> None:
    wait.until(lambda current: _jqx_combobox_option_ready(current, selector, text))


def _jqx_combobox_option_ready(driver, selector: str, text: str) -> bool:
    try:
        element = driver.find_element(By.CSS_SELECTOR, selector)
    except NoSuchElementException:
        return False
    try:
        return bool(
            driver.execute_script(
                """
                const outer = arguments[0];
                const expected = arguments[1].replace(/\\s+/g, ' ').trim();
                const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                if (!window.jQuery || !window.jQuery.fn || !window.jQuery.fn.jqxComboBox) return true;
                try {
                  const widget = window.jQuery(outer);
                  if (widget.jqxComboBox('disabled')) return false;
                  const items = widget.jqxComboBox('getItems') || [];
                  return items.some((item) => clean(item.label) === expected || clean(item.value) === expected);
                } catch (_) {
                  return false;
                }
                """,
                element,
                text,
            )
        )
    except StaleElementReferenceException:
        return False


def _select_jqx_combobox(driver, wait: WebDriverWait, selector: str, text: str) -> None:
    _wait_for_jqx_combobox_option(driver, wait, selector, text)
    def select_option(current) -> bool:
        try:
            element = current.find_element(By.CSS_SELECTOR, selector)
            return bool(
                current.execute_script(
                    """
                    const outer = arguments[0];
                    const expected = arguments[1].replace(/\\s+/g, ' ').trim();
                    const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const input = outer.matches('input') ? outer : outer.querySelector('input');
                    if (window.jQuery && window.jQuery.fn && window.jQuery.fn.jqxComboBox) {
                      const widget = window.jQuery(outer);
                      try {
                        if (widget.jqxComboBox('disabled')) return false;
                        const items = widget.jqxComboBox('getItems') || [];
                        const item = items.find((candidate) => clean(candidate.label) === expected || clean(candidate.value) === expected);
                        if (!item) return false;
                        widget.jqxComboBox('selectItem', item);
                        widget.jqxComboBox('val', item.value);
                        if (input) input.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                      } catch (_) {
                        return false;
                      }
                    }
                    if (input) {
                      input.focus();
                      input.value = expected;
                      input.dispatchEvent(new Event('input', {bubbles: true}));
                      input.dispatchEvent(new Event('change', {bubbles: true}));
                      input.blur();
                      return true;
                    }
                    return false;
                    """,
                    element,
                    text,
                )
            )
        except (NoSuchElementException, StaleElementReferenceException):
            return False

    wait.until(select_option)
    wait.until(lambda current: _token_matches(_control_value(current, selector), text))


def _wait_for_io_record_form_values(
    driver,
    wait: WebDriverWait,
    plan: CivilpowerTaskPlan,
    status: str,
) -> None:
    expected_values = {
        "#txt_AddLogDate": plan.out_date if status == OUT_STATUS else plan.in_date,
        "#txt_AddLogHour": plan.out_time[:2] if status == OUT_STATUS else plan.in_time[:2],
        "#txt_AddLogMin": plan.out_time[2:] if status == OUT_STATUS else plan.in_time[2:],
        "#txt_AddUnit": plan.home_unit,
        "#txt_AddServeUnit": plan.serve_unit,
        "#txt_AddReason": plan.out_reason if status == OUT_STATUS else plan.in_reason,
    }
    for selector, expected_value in expected_values.items():
        wait.until(
            lambda current, selector=selector, expected_value=expected_value: _same_value(
                _control_value(current, selector),
                expected_value,
            )
        )
    wait.until(lambda current: plan.member_name in _control_value(current, "#txt_AddVolFMan"))
    wait.until(lambda current: _selected_option_text(current, "#ddl_AddIO") == status)


def _selected_option_text(driver, selector: str) -> str:
    try:
        return _clean_text(Select(driver.find_element(By.CSS_SELECTOR, selector)).first_selected_option.text)
    except NoSuchElementException:
        return ""


def _select_option_containing(wait: WebDriverWait, selector: str, text: str, *, clear_others: bool = False) -> None:
    element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
    select = Select(element)
    if clear_others and select.is_multiple:
        select.deselect_all()
    matches = [option for option in select.options if _token_matches(_clean_text(option.text), text)]
    if len(matches) != 1:
        raise RuntimeError(f"下拉選單找不到唯一選項：{selector}={text}")
    select.select_by_visible_text(matches[0].text)


def _select_option_containing_if_present(driver, wait: WebDriverWait, selector: str, text: str) -> None:
    if driver.find_elements(By.CSS_SELECTOR, selector):
        _select_option_containing(wait, selector, text)


def _set_input(wait: WebDriverWait, selector: str, value: str) -> None:
    element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
    element.clear()
    element.send_keys(value)
    element.send_keys("\t")


def _set_if_present(driver, wait: WebDriverWait, selector: str, value: str) -> None:
    if driver.find_elements(By.CSS_SELECTOR, selector):
        _set_input(wait, selector, value)


def _click(wait: WebDriverWait, selector: str) -> None:
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector))).click()


def _click_if_present(driver, wait: WebDriverWait, selector: str) -> None:
    if driver.find_elements(By.CSS_SELECTOR, selector):
        _click(wait, selector)


def _wait_visible(wait: WebDriverWait, selector: str):
    return wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, selector)))


def _control_value(driver, selector: str) -> str:
    try:
        element = driver.find_element(By.CSS_SELECTOR, selector)
    except NoSuchElementException:
        return ""
    value = element.get_attribute("value")
    if value:
        return _clean_text(value)
    if selector.startswith("#txt_"):
        nested = element.find_elements(By.CSS_SELECTOR, "input")
        if nested:
            return _clean_text(nested[0].get_attribute("value"))
    return _clean_text(element.text)


def _element_displayed(driver, selector: str) -> bool:
    elements = driver.find_elements(By.CSS_SELECTOR, selector)
    return any(element.is_displayed() for element in elements)


def _same_value(actual: str, expected: str) -> bool:
    actual_date = _date_parts(actual)
    expected_date = _date_parts(expected)
    if actual_date is not None and expected_date is not None:
        return actual_date == expected_date
    clean_actual = re.sub(r"\D", "", str(actual or ""))
    clean_expected = re.sub(r"\D", "", str(expected or ""))
    return clean_actual == clean_expected if clean_expected else _clean_text(actual) == _clean_text(expected)


def _date_parts(value: object) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", _clean_text(value))
    if match is None:
        return None
    try:
        parsed = datetime(*map(int, match.groups()))
    except ValueError:
        return None
    return parsed.year, parsed.month, parsed.day


def _token_matches(text: str, token: str) -> bool:
    actual = _clean_text(text)
    expected = _clean_text(token)
    if not expected:
        return True
    if expected in actual:
        return True
    expected_digits = re.sub(r"\D", "", expected)
    return bool(expected_digits and expected_digits in re.sub(r"\D", "", actual))


def _valid_hhmm(value: str) -> bool:
    return bool(re.fullmatch(r"([01]\d|2[0-3])[0-5]\d", value))


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _raise_if_cancelled(cancel_check: Callable[[], None] | None) -> None:
    if cancel_check is not None:
        cancel_check()


def _report_progress(progress: Callable[[str], None] | None, stage: str) -> None:
    if progress is not None:
        progress(stage)


def _task_checkpoint_path(artifacts_dir: Path, task_id: str) -> Path:
    safe_task_id = re.sub(r"[^\w.-]+", "_", task_id).strip("._") or "unknown_task"
    return Path(artifacts_dir) / "civilpower" / "tasks" / f"{safe_task_id}.json"


def _plan_fingerprint(plan: CivilpowerTaskPlan) -> str:
    raw = json.dumps(asdict(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_task_checkpoint(artifacts_dir: Path, plan: CivilpowerTaskPlan) -> dict[str, object]:
    path = _task_checkpoint_path(artifacts_dir, plan.task_id)
    fingerprint = _plan_fingerprint(plan)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict) or payload.get("fingerprint") != fingerprint:
        return {"fingerprint": fingerprint, "created_at": datetime.now().isoformat(timespec="seconds")}
    return payload


def _save_task_checkpoint(artifacts_dir: Path, plan: CivilpowerTaskPlan, checkpoint: dict[str, object]) -> None:
    checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json_atomic(_task_checkpoint_path(artifacts_dir, plan.task_id), checkpoint)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_stale(element) -> bool:
    try:
        _ = element.is_displayed()
        return False
    except Exception:
        return True
