from __future__ import annotations

import os
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlparse
from uuid import uuid4

import ddddocr
from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from ambulance_bot.chrome_startup import add_worker_chrome_options, create_chrome_driver_with_retry, mark_driver_operation_active
from ambulance_bot.consumables import consumable_inventory_options
from ambulance_bot.duty_credentials import (
    LOGIN_POLICY_CONSUMABLES,
    load_synced_worker_credential,
    task_login_credential_attempts,
)
from ambulance_bot.failure_evidence import augment_failure_detail, capture_failure_artifacts
from ambulance_bot.models import AmbulanceReturnRequest, clean_case_address, normalize_hhmm, vehicle_ppe_names
from ambulance_bot.profile_paths import runtime_profile_dir
from ambulance_bot.task_cancellation import TaskCancellationError
from ambulance_bot.update_safety import ManualUpdateRequiredError, manual_update_reason
from ambulance_bot.vehicle_reconciliation import VehicleCandidateLookupError
from ambulance_bot.window_layout import apply_tile


SSO_URL = "https://nfaemsap3.nfa.gov.tw/SSO/"
ACS_URL = "https://nfaemsap3.nfa.gov.tw/ACS/ACS15001"
OUTPUT_DIR = Path(os.getenv("CAPTCHA_OUTPUT_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ambulance_return_bot" / "captcha")
CAPTCHA_PATH = OUTPUT_DIR / "nfa_acs_captcha.png"
MAX_LOGIN_ATTEMPTS = 3
SUPPLEMENTAL_GLOVE_NAME = "桃-9吋手套-L(雙)"

class _ConsumableListPendingError(RuntimeError):
    pass


ocr = ddddocr.DdddOcr(show_ad=False)


def save_consumables_record_enabled() -> bool:
    return os.getenv("SAVE_CONSUMABLES_RECORD", "true").strip().lower() in {"1", "true", "yes", "on"}


def _consumables_dry_run_enabled() -> bool:
    return os.getenv("AUTOMATION_DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "on"}


def _distribute_consumables(consumables: dict[str, int], page_count: int) -> list[dict[str, int]]:
    if page_count < 1:
        raise ValueError("page_count must be at least 1")
    allocations = [dict() for _ in range(page_count)]
    for name, raw_quantity in consumables.items():
        quantity = int(raw_quantity or 0)
        if not name or quantity <= 0:
            continue
        base, remainder = divmod(quantity, page_count)
        for index in range(page_count):
            assigned = base + (1 if index < remainder else 0)
            if assigned > 0:
                allocations[index][name] = assigned
    for allocation in allocations:
        if not allocation:
            allocation[SUPPLEMENTAL_GLOVE_NAME] = 1
    return allocations


def login_acs_and_get_driver(
    profile_name: str = "consumables_profile",
    debugger_port: int | None = None,
    tile_name: str = "",
    task: dict[str, object] | AmbulanceReturnRequest | None = None,
    artifacts_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> webdriver.Chrome:
    account_text, password_text = _load_acs_credentials(task)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument("--window-size=1280,900")
    options.add_argument(f"--user-data-dir={_chrome_profile_dir(profile_name)}")
    add_worker_chrome_options(options)
    if debugger_port:
        options.add_argument(f"--remote-debugging-port={debugger_port}")
    options.add_experimental_option("detach", True)

    if progress is not None:
        progress("啟動 Chrome")
    driver = create_chrome_driver_with_retry(options, "一站通耗材", fresh_session=True)
    page_timeout = int(os.getenv("SELENIUM_PAGE_LOAD_TIMEOUT_SECONDS", "45"))
    driver.set_page_load_timeout(page_timeout)
    driver.set_script_timeout(page_timeout)
    apply_tile(driver, tile_name)
    try:
        login_error = ""
        if progress is not None:
            progress("登入一站通")
        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            driver.get(SSO_URL)
            wait = WebDriverWait(driver, 15)
            _fill_login_form(driver, wait, account_text, password_text, attempt)
            _wait_until_sso_login_finished(driver, wait)
            if _sso_login_succeeded(driver):
                break
            login_error = _login_error_text(driver) or "登入後仍停留在一站通 SSO 頁，可能是驗證碼錯誤。"
        else:
            raise RuntimeError(f"一站通登入失敗，已嘗試 {MAX_LOGIN_ATTEMPTS} 次。{login_error}")

        _open_acs_system(driver, wait)
        _remember_initial_consumable_list(driver, account_text, task)
        return driver
    except TaskCancellationError:
        raise
    except Exception as exc:
        request = _request_or_none(task)
        evidence = _capture_consumables_failure(
            driver,
            artifacts_dir,
            request,
            exc,
        )
        raise RuntimeError(augment_failure_detail(str(exc), evidence)) from exc


def open_consumable_record_for_task(
    driver: webdriver.Chrome,
    task: dict[str, object] | AmbulanceReturnRequest,
    cancel_check: Callable[[], None] | None = None,
    update_context: dict[str, object] | None = None,
    artifacts_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> str:
    mark_driver_operation_active(driver)
    try:
        return _open_consumable_record_for_task(
            driver,
            task,
            cancel_check=cancel_check,
            update_context=update_context,
            progress=progress,
        )
    except (TaskCancellationError, ManualUpdateRequiredError, VehicleCandidateLookupError):
        raise
    except Exception as exc:
        request = task if isinstance(task, AmbulanceReturnRequest) else AmbulanceReturnRequest.from_dict(task)
        evidence = _capture_consumables_failure(
            driver,
            artifacts_dir,
            request,
            exc,
        )
        raise RuntimeError(augment_failure_detail(str(exc), evidence)) from exc
    finally:
        mark_driver_operation_active(driver, False)


def _open_consumable_record_for_task(
    driver: webdriver.Chrome,
    task: dict[str, object] | AmbulanceReturnRequest,
    cancel_check: Callable[[], None] | None = None,
    update_context: dict[str, object] | None = None,
    progress: Callable[[str], None] | None = None,
) -> str:
    request = task if isinstance(task, AmbulanceReturnRequest) else AmbulanceReturnRequest.from_dict(task)
    manual_reason = manual_update_reason("consumables", request, update_context)
    if manual_reason:
        raise ManualUpdateRequiredError(f"manual correction required: {manual_reason}")
    if save_consumables_record_enabled() and not _normalized_consumable_case_id(request.case_id):
        raise RuntimeError("耗材自動儲存需要有效的官方案件案號，已停止處理。")
    wait = WebDriverWait(driver, 15)
    if progress is not None:
        progress("開啟耗材紀錄")
    reuse_initial_query = _consume_initial_consumable_list(driver, task)
    classification_context = _new_consumable_classification_context(driver, task)
    if not reuse_initial_query:
        _open_consumable_maintenance_page(driver, wait)
    if progress is not None:
        progress("查詢案件")
    hrefs = _find_consumable_detail_hrefs(driver, request)
    if len(hrefs) > 1 and not save_consumables_record_enabled() and not _consumables_dry_run_enabled():
        raise RuntimeError("同案多患者耗材必須啟用自動儲存，否則切換頁面會遺失未送出的資料。")
    allocations = _distribute_consumables(request.consumables, len(hrefs))
    completed: list[str] = []
    summaries: list[str] = []
    supplements: list[str] = []
    single_detail = ""
    for index, (href, allocation) in enumerate(zip(hrefs, allocations)):
        suffix = _patient_sid_parts(_emm_temsis_id_from_href(href))[1]
        try:
            if progress is not None:
                progress("選取患者頁")
            _open_consumable_detail_if_needed(driver, href)
            if not _wait_for_consumable_detail_page(driver, wait):
                raise RuntimeError("consumable detail page did not open; SSO login may be required")
            expected_case_id = _normalized_consumable_case_id(request.case_id)
            actual_case_id = _normalized_consumable_case_id(_consumable_detail_case_id(driver))
            if expected_case_id and actual_case_id != expected_case_id:
                raise RuntimeError(
                    f"耗材內容頁案件案號不符：預期={expected_case_id} 實際={actual_case_id or '空白'}"
                )
            actual_vehicle = _consumable_detail_vehicle_label(driver)
            if request.vehicle and not actual_vehicle:
                raise RuntimeError(f"患者序號 {suffix} 無法讀取耗材頁車輛。")
            if request.vehicle and actual_vehicle and actual_vehicle != request.vehicle:
                raise RuntimeError(f"患者序號 {suffix} 車輛不符：預期={request.vehicle} 實際={actual_vehicle}")
            page_request = replace(request, consumables=dict(allocation))
            if progress is not None:
                progress("填寫耗材品項")
                if save_consumables_record_enabled() and not _consumables_dry_run_enabled():
                    progress("儲存")
            single_detail = _write_current_consumable_page(
                driver,
                wait,
                page_request,
                **({"classification_context": classification_context} if classification_context is not None else {}),
                **({"cancel_check": cancel_check} if cancel_check is not None else {}),
            )
            if progress is not None and save_consumables_record_enabled() and not _consumables_dry_run_enabled():
                progress("回查耗材紀錄")
        except TaskCancellationError:
            raise
        except Exception as exc:
            if len(hrefs) == 1:
                raise
            success_text = ",".join(completed) or "無"
            raise RuntimeError(
                f"同案多患者耗材分配／確認失敗：成功={success_text}；失敗={suffix}；原因={exc}"
            ) from exc
        completed.append(suffix)
        summaries.append(f"{suffix}填入{sum(allocation.values())}件")
        if _allocation_needs_supplement(request.consumables, len(hrefs), index):
            supplements.append(f"{suffix}原分配為空，已補手套×1以完成確認。")
    if len(hrefs) == 1:
        return f"已開啟耗材內容頁：{driver.current_url}{_consumable_vehicle_notice(driver, request)}{single_detail}"
    page_label = {2: "兩頁", 3: "三頁", 4: "四頁"}.get(len(hrefs), f"{len(hrefs)}頁")
    if _consumables_dry_run_enabled():
        return f"辨識{request.vehicle}同案{len(hrefs)}位患者；{page_label}均已唯讀核對耗材，未修改或儲存。"
    detail = f"辨識{request.vehicle}同案{len(hrefs)}位患者；{'、'.join(summaries)}，{page_label}均已儲存確認。"
    if supplements:
        detail = f"{detail} {' '.join(supplements)}"
    return detail


def _write_current_consumable_page(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    request: AmbulanceReturnRequest,
    cancel_check: Callable[[], None] | None = None,
    classification_context: dict[str, object] | None = None,
) -> str:
    added = ""
    if _needs_extra_consumable_row(request):
        detail_url = str(driver.current_url or "")
        expected_sid = _emm_temsis_id_from_href(detail_url)
        if not expected_sid:
            raise RuntimeError("耗材內容頁缺少 emmTemsisid，停止填寫。")
        item_quantities = _resolve_consumable_item_quantities(
            driver, request, classification_context=classification_context,
        )
        if _consumables_dry_run_enabled():
            return f" 已唯讀核對耗材 {len(item_quantities)} 筆的品項與數量，未修改或儲存。"
        if save_consumables_record_enabled():
            _clear_existing_consumables(driver, wait)
            _inject_consumables_for_save(driver, item_quantities)
            _assert_consumable_rows_match(driver, item_quantities, "耗材儲存前")
            _save_consumables(driver, wait, cancel_check=cancel_check)
            if _is_sso_page(driver):
                raise RuntimeError("consumable save returned to SSO login page")
            _reopen_consumable_detail_for_readback(driver, wait, detail_url, expected_sid)
            _verify_saved_consumables(driver, item_quantities)
            added = f" 已清除舊資料、填入耗材 {len(item_quantities)} 筆、按下儲存並確認。"
        else:
            _clear_existing_consumables(driver, wait)
            filled_count = _fill_consumables(driver, wait, request)
            _assert_consumable_rows_match(driver, item_quantities, "耗材預填後")
            added = f" 已清除舊資料、在畫面填入耗材 {filled_count} 筆，未按儲存。"
    return added


def _allocation_needs_supplement(consumables: dict[str, int], page_count: int, index: int) -> bool:
    for name, raw_quantity in consumables.items():
        quantity = int(raw_quantity or 0)
        if not name or quantity <= 0:
            continue
        base, remainder = divmod(quantity, page_count)
        if base + (1 if index < remainder else 0) > 0:
            return False
    return True


def _load_acs_credentials(task: dict[str, object] | AmbulanceReturnRequest | None = None) -> tuple[str, str]:
    for credential, _source in _acs_credential_attempts(task):
        acs_account = _acs_login_account(credential)
        if acs_account and credential.password:
            return acs_account, credential.password

    synced = load_synced_worker_credential()
    if synced is not None and synced.password:
        _lookup_synced_credential_id_number_for_acs()
        for credential, _source in _acs_credential_attempts(task):
            acs_account = _acs_login_account(credential)
            if acs_account and credential.password:
                return acs_account, credential.password

    raise RuntimeError("找不到一站通耗材帳密；請先在 worker GUI 同步含身分證字號的帳號。")


def _acs_credential_attempts(
    task: dict[str, object] | AmbulanceReturnRequest | None,
):
    return task_login_credential_attempts(
        _request_or_none(task),
        duty_password=False,
        login_policy=LOGIN_POLICY_CONSUMABLES,
    )


def _acs_login_account(credential) -> str:
    return credential.id_number.strip() or (
        credential.user_id if re.fullmatch(r"[A-Za-z][0-9]{9}", credential.user_id) else ""
    )


def _lookup_synced_credential_id_number_for_acs() -> None:
    try:
        from ambulance_bot.selenium_local import lookup_synced_credential_id_number

        artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
        lookup_range = os.getenv("ACS_ID_LOOKUP_RANGE", "24h").strip() or "24h"
        result = lookup_synced_credential_id_number(artifacts_dir, lookup_range=lookup_range)
    except Exception as exc:
        print(f"[consumables] synced id lookup failed: {exc}", flush=True)
        return

    if result.ok and result.id_number:
        print(f"[consumables] synced id lookup saved id_number={result.id_number}", flush=True)
    elif result.status != "already_has_id_number":
        print(f"[consumables] synced id lookup skipped status={result.status}", flush=True)


def _chrome_profile_dir(profile_name: str) -> Path:
    return runtime_profile_dir(profile_name)


def _fill_login_form(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    account_text: str,
    password_text: str,
    attempt: int,
) -> None:
    account = wait.until(EC.element_to_be_clickable((By.ID, "account")))
    account.clear()
    account.send_keys(account_text)

    password = wait.until(EC.element_to_be_clickable((By.ID, "password-input")))
    password.clear()
    password.send_keys(password_text)

    Select(wait.until(EC.presence_of_element_located((By.ID, "country")))).select_by_value("03")

    captcha = _find_captcha_image(driver, wait)
    image = Image.open(BytesIO(captcha.screenshot_as_png)).convert("RGB")
    captcha_path = OUTPUT_DIR / f"nfa_acs_captcha_attempt_{attempt}.png"
    image.save(captcha_path)
    image.save(CAPTCHA_PATH)
    digits = "".join(ch for ch in ocr.classification(captcha_path.read_bytes()) if ch.isdigit())
    if not digits:
        raise RuntimeError(f"一站通驗證碼辨識失敗，第 {attempt} 次。")

    code = wait.until(EC.element_to_be_clickable((By.ID, "verificationCode")))
    code.clear()
    code.send_keys(digits)

    submit = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "form[action='/SSO/login'] input[type='submit'], button[type='submit']"))
    )
    submit.click()


def _sso_login_succeeded(driver: webdriver.Chrome) -> bool:
    return "/ACS/" in driver.current_url or bool(driver.find_elements(By.CSS_SELECTOR, "img.cardImg[src*='ACS.jpg']"))


def _login_error_text(driver: webdriver.Chrome) -> str:
    for selector in (".alert", ".error", ".text-danger", "#error", "[role='alert']"):
        texts = [item.text.strip() for item in driver.find_elements(By.CSS_SELECTOR, selector) if item.text.strip()]
        if texts:
            return " ".join(texts)
    return ""


def _find_captcha_image(driver: webdriver.Chrome, wait: WebDriverWait):
    wait.until(EC.presence_of_element_located((By.ID, "verificationCode")))
    exact = driver.find_elements(By.CSS_SELECTOR, "img#verificationCode[src*='/SSO/file/Verification']")
    if exact:
        return exact[0]
    for image in driver.find_elements(By.CSS_SELECTOR, "img[src*='/SSO/file/Verification']"):
        if image.is_displayed():
            return image
    raise RuntimeError("找不到一站通驗證碼圖片。")


def _request_or_none(task: dict[str, object] | AmbulanceReturnRequest | None) -> AmbulanceReturnRequest | None:
    if isinstance(task, AmbulanceReturnRequest):
        return task
    if isinstance(task, dict):
        return AmbulanceReturnRequest.from_dict(task)
    return None


def _capture_consumables_failure(
    driver: webdriver.Chrome,
    artifacts_dir: Path | None,
    request: AmbulanceReturnRequest | None,
    exception: BaseException,
) -> dict[str, object]:
    output_dir = Path(artifacts_dir or os.getenv("ARTIFACTS_DIR", "artifacts")) / "selenium"
    try:
        return capture_failure_artifacts(
            driver,
            output_dir,
            request.task_id if request is not None else "unknown_task",
            "consumables",
            vehicle=request.vehicle if request is not None else "",
            exception=exception,
            target_url=ACS_URL,
        )
    except Exception as capture_exc:
        return {
            "category": "",
            "reason": "",
            "screenshot_error": f"{capture_exc.__class__.__name__}: {capture_exc}",
        }


def _wait_until_sso_login_finished(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    wait.until(
        lambda d: d.current_url != SSO_URL
        or not d.find_elements(By.ID, "verificationCode")
        or bool(d.find_elements(By.CSS_SELECTOR, "img.cardImg[src*='ACS.jpg']"))
    )


def _is_sso_page(driver: webdriver.Chrome) -> bool:
    if "/SSO/" in driver.current_url:
        return True
    return bool(driver.find_elements(By.ID, "verificationCode"))


def _open_acs_system(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    if "/ACS/" not in driver.current_url:
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        cards = driver.find_elements(By.CSS_SELECTOR, "img.cardImg[src*='ACS.jpg']")
        if cards:
            try:
                cards[0].click()
            except Exception as exc:
                raise RuntimeError(
                    "點選 ACS 系統失敗（"
                    f"目前網址：{_navigation_current_url(driver)}；"
                    f"{exc.__class__.__name__}: {str(exc).strip() or '無訊息'}）"
                ) from exc
            wait.until(lambda d: "/ACS/" in d.current_url)
        else:
            driver.get("https://nfaemsap3.nfa.gov.tw/ACS/ACS13001")
            wait.until(lambda d: "/ACS/" in d.current_url)

    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    if _is_sso_page(driver):
        raise RuntimeError("ACS system returned to SSO login page")
    _open_consumable_maintenance_page(driver, wait)


def _open_consumable_maintenance_page(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    if _is_consumable_maintenance_page_ready(driver):
        _prepare_consumable_list_snapshot(driver, wait)
        if _read_complete_consumable_candidates(driver) is not None:
            return
    navigation_errors: list[str] = []
    for attempt in range(1, 4):
        try:
            driver.get(ACS_URL)
        except TimeoutException as exc:
            navigation_errors.append(
                f"第 {attempt} 次導向逾時（目前網址：{_navigation_current_url(driver)}；"
                f"{exc.__class__.__name__}: {str(exc).strip() or '無訊息'}）"
            )
        except Exception as exc:
            raise RuntimeError(
                f"開啟耗材紀錄頁 ACS15001 失敗（第 {attempt} 次；"
                f"目前網址：{_navigation_current_url(driver)}；"
                f"{exc.__class__.__name__}: {str(exc).strip() or '無訊息'}）"
            ) from exc
        try:
            wait.until(
                lambda d: _is_sso_page(d) or _is_consumable_maintenance_page_ready(d)
            )
        except TimeoutException as exc:
            navigation_errors.append(
                f"第 {attempt} 次等待 ACS15001 逾時（目前網址：{_navigation_current_url(driver)}；"
                f"{exc.__class__.__name__}: {str(exc).strip() or '無訊息'}）"
            )
            continue
        if _is_consumable_maintenance_page_ready(driver):
            _prepare_consumable_list_snapshot(driver, wait)
            return
        if _is_sso_page(driver):
            raise RuntimeError("進入耗材紀錄頁時被導回 SSO，登入 session 尚未建立。")
    diagnostics = "；".join(navigation_errors[-3:])
    raise RuntimeError(
        "已登入 ACS，但無法進入耗材紀錄頁 ACS15001。"
        f"最後網址：{_navigation_current_url(driver)}"
        + (f"；{diagnostics}" if diagnostics else "")
    )


def _consumable_list_context(driver: webdriver.Chrome, document_token: str = "") -> dict | None:
    state = driver.execute_script(
        """
        const state = window.__ambulanceAcsList;
        if (!state || document.readyState !== 'complete' || !window.jQuery || window.jQuery.active !== 0 ||
            !state.generation || state.generation !== state.completedGeneration ||
            state.query !== state.fingerprint()) return null;
        if (arguments[0]) state.documentToken = arguments[0];
        if (!state.documentToken) return null;
        return {url: location.href, query: state.query, generation: state.generation,
            document: state.documentToken};
        """,
        document_token,
    )
    return state if isinstance(state, dict) else None


def _consumable_task_key(task) -> str:
    return str(task.task_id if isinstance(task, AmbulanceReturnRequest) else (task or {}).get("task_id") or "")


def _remember_initial_consumable_list(driver: webdriver.Chrome, account: str, task=None) -> None:
    driver._acs_authenticated_account = account
    driver.__dict__.pop("_acs_initial_list_handoff", None)
    task_key = _consumable_task_key(task)
    if not task_key or _read_complete_consumable_candidates(driver) is None:
        return
    context = _consumable_list_context(driver, uuid4().hex)
    if context is not None:
        driver._acs_initial_list_handoff = {
            "account": account, "session_id": driver.session_id, "context": context,
            "window": driver.current_window_handle, "task_key": task_key,
        }


def _consume_initial_consumable_list(driver: webdriver.Chrome, task) -> bool:
    handoff = driver.__dict__.pop("_acs_initial_list_handoff", None)
    if not isinstance(handoff, dict):
        return False
    if handoff["task_key"] != _consumable_task_key(task):
        return False
    if _load_acs_credentials(task)[0] != handoff["account"]:
        raise RuntimeError("耗材登入帳號與任務帳號不符，停止處理。")
    if driver.__dict__.get("_acs_authenticated_account") != handoff["account"]:
        raise RuntimeError("耗材登入帳號已變更，停止處理。")
    if (driver.session_id != handoff["session_id"] or driver.current_window_handle != handoff["window"]
            or _consumable_list_context(driver) != handoff["context"]):
        return False
    if _read_complete_consumable_candidates(driver) is None:
        return False
    return _consumable_list_context(driver) == handoff["context"]


def _new_consumable_classification_context(driver: webdriver.Chrome, task) -> dict | None:
    account = driver.__dict__.get("_acs_authenticated_account")
    session_id = getattr(driver, "session_id", None)
    if not isinstance(account, str) or not account or not isinstance(session_id, str) or not session_id:
        return None
    if _load_acs_credentials(task)[0] != account:
        raise RuntimeError("耗材登入帳號與任務帳號不符，停止處理。")
    return {"driver": driver, "account": account, "session_id": session_id,
            "window": driver.current_window_handle, "maps": {}}


def _navigation_current_url(driver: webdriver.Chrome) -> str:
    try:
        return str(driver.current_url or "").strip() or "空白"
    except Exception:
        return "無法讀取"


def _is_consumable_maintenance_page_ready(driver: webdriver.Chrome) -> bool:
    return (
        _is_consumable_maintenance_page(driver)
        and driver.execute_script("return document.readyState") == "complete"
    )


def _is_consumable_maintenance_page(driver: webdriver.Chrome) -> bool:
    current = urlparse(str(driver.current_url or ""))
    target = urlparse(ACS_URL)
    return (
        current.scheme == target.scheme
        and current.netloc == target.netloc
        and current.path.rstrip("/") == target.path
    )


def _is_consumable_detail_page(driver: webdriver.Chrome) -> bool:
    if "ACS15002" in driver.current_url:
        return True
    text = driver.find_element(By.TAG_NAME, "body").text
    return "TEMSISID" in text or "emmTemsisid" in text


def _wait_for_consumable_detail_page(driver: webdriver.Chrome, wait: WebDriverWait) -> bool:
    latest_state = None

    def ready_or_sso(current_driver):
        nonlocal latest_state
        latest_state = _read_consumable_detail_state(current_driver)
        if _has_consumable_navigation_state(latest_state):
            return latest_state["isSsoPage"] or _consumable_detail_payload_ready(current_driver, state=latest_state)
        return _is_sso_page(current_driver) or _consumable_detail_payload_ready(current_driver)

    try:
        wait.until(ready_or_sso)
    except TimeoutException:
        return False
    if _has_consumable_navigation_state(latest_state):
        return not latest_state["isSsoPage"] and _consumable_detail_payload_ready(driver, state=latest_state)
    return not _is_sso_page(driver) and _is_consumable_detail_page(driver)


def _has_consumable_navigation_state(state) -> bool:
    return (isinstance(state, dict) and isinstance(state.get("currentUrl"), str)
            and type(state.get("isSsoPage")) is bool and type(state.get("isDetailPage")) is bool)


def _read_consumable_detail_state(driver: webdriver.Chrome) -> dict | None:
    try:
        return driver.execute_script(
            """
            const currentUrl = location.href;
            const sidField = document.querySelector('#emmTemsisid');
            const caseField = document.querySelector('#csNo');
            const callNoField = document.querySelector('#callNo');
            return {
                currentUrl: currentUrl,
                isSsoPage: currentUrl.includes('/SSO/') || !!document.getElementById('verificationCode'),
                isDetailPage: currentUrl.includes('ACS15002') ||
                    /TEMSISID|emmTemsisid/.test(String(document.body?.innerText || '')),
                hasEmmTemsisidField: !!sidField,
                emmTemsisid: String(sidField?.value || '').trim(),
                hasCaseIdField: !!caseField,
                caseId: String(caseField?.value || '').trim(),
                hasCallNoField: !!callNoField,
                callNo: String(callNoField?.value || '').trim(),
                documentReady: document.readyState,
                ajaxActive: window.jQuery ? window.jQuery.active : null
            };
            """
        )
    except Exception:
        return None


def _consumable_detail_payload_ready(driver: webdriver.Chrome, *, state: dict | None = None) -> bool:
    if state is None:
        state = _read_consumable_detail_state(driver)
    if not isinstance(state, dict):
        return False
    if _has_consumable_navigation_state(state):
        if state["isSsoPage"] or not state["isDetailPage"]:
            return False
        current_url = state["currentUrl"]
    else:
        if not _is_consumable_detail_page(driver):
            return False
        current_url = str(driver.current_url or "")
    if state.get("documentReady", "complete") != "complete" or state.get("ajaxActive", 0) != 0:
        return False
    if not all(
        state.get(key)
        for key in ("hasEmmTemsisidField", "hasCaseIdField", "hasCallNoField")
    ):
        return False
    actual_sid = str(state.get("emmTemsisid") or "").strip()
    actual_case_id = str(state.get("caseId") or "").strip()
    actual_call_no = str(state.get("callNo") or "").strip()
    expected_sid = _emm_temsis_id_from_href(current_url)
    return (
        bool(actual_sid and actual_case_id and actual_call_no)
        and (not expected_sid or actual_sid == expected_sid)
    )


def _open_consumable_detail_if_needed(driver: webdriver.Chrome, href: str) -> None:
    target = urlparse(urljoin("https://nfaemsap3.nfa.gov.tw", href))
    current = urlparse(str(driver.current_url or ""))
    if (
        current.scheme == target.scheme == "https"
        and current.netloc == target.netloc == "nfaemsap3.nfa.gov.tw"
        and current.path.rstrip("/") == target.path.rstrip("/") == "/ACS/ACS15002"
        and parse_qs(current.query) == parse_qs(target.query)
        and _consumable_detail_payload_ready(driver)
    ):
        return
    driver.get(target.geturl())


def _find_consumable_detail_href(driver: webdriver.Chrome, request: AmbulanceReturnRequest) -> str:
    return _find_consumable_detail_hrefs(driver, request)[0]


def _find_consumable_detail_hrefs(driver: webdriver.Chrome, request: AmbulanceReturnRequest) -> list[str]:
    wait = WebDriverWait(driver, 15)
    wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "a.btn_t02[href^='/ACS/ACS15002?emmTemsisid=']")) > 0)
    candidates = _collect_consumable_candidates(driver, request)
    if not candidates:
        raise RuntimeError("耗材列表找不到內容連結。")

    case_time = normalize_hhmm(request.case_time)
    address = clean_case_address(request.case_address)
    scored: list[tuple[int, str, str]] = []
    sid_scored: list[tuple[int, str, str]] = []
    for item in candidates:
        href = str(item.get("href") or "")
        sid = str(item.get("sid") or _emm_temsis_id_from_href(href))
        text = str(item.get("text") or "")
        if not _consumable_candidate_date_matches(request.case_id, sid, text):
            continue
        row_hhmm = _hhmm_from_row_text(text)
        score = 0
        strong_score = 0
        sid_score = _consumable_sid_score(request.case_id, sid)
        score += sid_score
        strong_score += sid_score
        if case_time and row_hhmm == case_time:
            score += 3
            strong_score += 3
        if address and address in clean_case_address(text):
            score += 5
            strong_score += 5
        if request.case_reason and request.case_reason in text:
            score += 1
        if strong_score <= 0:
            continue
        if sid_score > 0:
            sid_scored.append((score, href, text))
        scored.append((score, href, text))

    if request.vehicle and scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        candidate_records: list[dict[str, str]] = []
        matched_hrefs = _find_consumable_hrefs_by_vehicle_code(
            driver,
            [href for _, href, _ in scored],
            request.vehicle,
            request.case_id,
            candidate_records=candidate_records,
        )
        matched = [item for item in scored if item[1] in matched_hrefs]
        if matched:
            return _select_consumable_patient_group(matched)
        if candidate_records:
            raise VehicleCandidateLookupError(
                "consumables",
                request.vehicle,
                candidate_records,
            )
        raise RuntimeError(f"耗材內容頁找不到符合車輛的紀錄：車輛={request.vehicle} 候選={len(scored)}")

    sid_scored.sort(key=lambda item: item[0], reverse=True)
    if sid_scored:
        tied = [item for item in sid_scored if item[0] == sid_scored[0][0]]
        if len(tied) > 1 and request.vehicle:
            matched = _find_consumable_href_by_vehicle_code(driver, [href for _, href, _ in tied], request.vehicle)
            if matched:
                return [matched]
        if len(sid_scored) > 1 and request.vehicle:
            matched = _find_consumable_href_by_vehicle_code(driver, [href for _, href, _ in sid_scored], request.vehicle)
            if matched:
                return [matched]
        return [sid_scored[0][1]]

    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] > 0:
        tied = [item for item in scored if item[0] == scored[0][0]]
        if len(tied) > 1 and request.vehicle:
            matched = _find_consumable_href_by_vehicle_code(driver, [href for _, href, _ in tied], request.vehicle)
            if matched:
                return [matched]
        return [scored[0][1]]
    raise RuntimeError(f"耗材列表找不到符合案件的內容列：時間={case_time or '未填'} 地址={address or '未填'}")


def _collect_consumable_candidates(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest,
) -> list[dict[str, str]]:
    complete_candidates = _read_complete_consumable_candidates(driver)
    if complete_candidates is not None:
        return _observe_complete_consumable_candidates(driver, request, complete_candidates)
    candidates = _collect_paginated_consumable_candidates(driver, request)
    signature = _consumable_case_candidates_signature(request, candidates)
    if not _prepare_consumable_list_snapshot(driver, WebDriverWait(driver, 15)):
        raise RuntimeError("耗材清單無法完成當次正式重新查詢，停止辨識。")
    refreshed = _read_complete_consumable_candidates(driver)
    if refreshed is None:
        refreshed = _collect_paginated_consumable_candidates(driver, request)
    if _consumable_case_candidates_signature(request, refreshed) != signature:
        raise RuntimeError("耗材同案患者清單於正式重新查詢後變動，停止整批填寫。")
    return refreshed


def _collect_paginated_consumable_candidates(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest,
) -> list[dict[str, str]]:
    _reset_consumable_page_to_first(driver)
    candidates = _stable_consumable_candidates(driver)
    if not candidates:
        return []
    collected: list[dict[str, str]] = []
    seen_hrefs: set[str] = set()
    for _ in range(200):
        for item in candidates:
            href = str(item.get("href") or "")
            if href and href not in seen_hrefs:
                seen_hrefs.add(href)
                collected.append(item)
        if _consumable_page_is_older_than_case(request.case_id, candidates):
            break
        previous_signature = _consumable_candidates_signature(candidates)
        if not _click_next_consumable_page_if_present(driver):
            break
        if not _wait_for_consumable_page_change(driver, previous_signature):
            raise RuntimeError("耗材列表換頁未完成，停止辨識。")
        candidates = _stable_consumable_candidates(driver)
        if not candidates:
            break
    else:
        raise RuntimeError("耗材列表超過安全分頁上限，停止辨識。")
    return collected


def _consumable_case_candidates_signature(
    request: AmbulanceReturnRequest,
    candidates: list[dict[str, str]],
) -> tuple[tuple[str, str, str, str, bool, bool], ...]:
    case_time = normalize_hhmm(request.case_time)
    address = clean_case_address(request.case_address)
    relevant = []
    for item in candidates:
        href = str(item.get("href") or "")
        sid = str(item.get("sid") or _emm_temsis_id_from_href(href))
        text = str(item.get("text") or "")
        row_time = _hhmm_from_row_text(text)
        address_matches = bool(address and address in clean_case_address(text))
        if _consumable_candidate_date_matches(request.case_id, sid, text) and (
            _consumable_sid_score(request.case_id, sid) > 0
            or bool(case_time and row_time == case_time)
            or address_matches
        ):
            # Row numbers and saved-status labels can change without changing patients.
            relevant.append((href, sid, _yyyymmdd_from_row_text(text), row_time,
                             address_matches, bool(request.case_reason and request.case_reason in text)))
    return tuple(sorted(relevant))


def _observe_complete_consumable_candidates(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest,
    candidates: list[dict[str, str]],
) -> list[dict[str, str]]:
    signature = _consumable_case_candidates_signature(request, candidates)
    if not _prepare_consumable_list_snapshot(driver, WebDriverWait(driver, 15)):
        raise RuntimeError("耗材完整清單無法完成當次正式重新查詢，停止辨識。")
    refreshed = None

    def complete_list_ready(current):
        nonlocal refreshed
        try:
            refreshed = _read_complete_consumable_candidates(current)
        except _ConsumableListPendingError:
            return False
        return refreshed is not None

    try:
        WebDriverWait(driver, 15).until(complete_list_ready)
    except TimeoutException as exc:
        raise RuntimeError("耗材正式重新查詢結果尚未就緒，停止整批填寫。") from exc
    if _consumable_case_candidates_signature(request, refreshed) != signature:
        raise RuntimeError("耗材同案患者清單於正式重新查詢後變動，停止整批填寫。")
    return refreshed


def _prepare_consumable_list_snapshot(driver: webdriver.Chrome, wait: WebDriverWait) -> bool:
    try:
        state = driver.execute_script(
            """
            const $ = window.jQuery;
            const search = document.querySelector('#search');
            const ids = ['emmTemsisid', 'deptno', 'isSaved', 'attendanceDateStart',
                         'attendanceDateEnd', 'address', 'traumaPrimaryCategory'];
            if (!$ || !search || !ids.every(id => document.getElementById(id))) return false;
            if (window.__ambulanceAcsList) {
                const generation = window.__ambulanceAcsList.generation;
                search.click();
                return window.__ambulanceAcsList.generation > generation;
            }
            const state = {generation: 0, completedGeneration: 0, payload: null};
            state.fingerprint = () => JSON.stringify(ids.map(id => [id,
                String(document.getElementById(id)?.value || '')]).concat([
                ['trauma', String(document.querySelector('[name="trauma"]:checked')?.value || '')]
            ]));
            window.__ambulanceAcsList = state;
            function isQuery(settings) {
                const url = new URL(settings.url, window.location.href);
                return url.origin === window.location.origin &&
                    url.pathname === '/ACS/ACS15001/getACS15001DTO';
            }
            $(document).on('ajaxSend.ambulanceAcsList', function(_event, xhr, settings) {
                if (!isQuery(settings)) return;
                state.generation += 1;
                state.xhr = xhr;
                state.query = state.fingerprint();
                state.payload = null;
                state.status = 0;
            });
            $(document).on('ajaxComplete.ambulanceAcsList', function(_event, xhr, settings) {
                if (!isQuery(settings) || state.xhr !== xhr) return;
                state.completedGeneration = state.generation;
                state.status = xhr.status;
                try { state.payload = JSON.parse(xhr.responseText); }
                catch (_error) { state.payload = null; }
            });
            search.click();
            return state.generation > 0;
            """
        )
        if state is not True:
            return False
        wait.until(
            lambda d: d.execute_script(
                """
                const state = window.__ambulanceAcsList;
                return !!state && state.generation > 0 &&
                    state.generation === state.completedGeneration && window.jQuery.active === 0;
                """
            ) is True
        )
        return True
    except Exception:
        # Unknown contracts require separately verified UI readiness and a fresh query.
        return False


def _read_complete_consumable_candidates(driver: webdriver.Chrome) -> list[dict[str, str]] | None:
    """Read all rows only when this query's official payload and rendered identities agree."""
    try:
        if not _is_consumable_maintenance_page(driver):
            return None
        state = driver.execute_script(
            """
            const unavailable = {complete: false};
            const state = window.__ambulanceAcsList;
            const $ = window.jQuery;
            if (state?.generation && state.generation !== state.completedGeneration)
                return {complete: false, pending: true};
            if (state?.generation && (!Number.isInteger(state.status) || state.status < 200 || state.status >= 300 ||
                state.payload?.result !== 'success' || !Array.isArray(state.payload.list) ||
                state.payload.list.some(row => !row || typeof row !== 'object' || Array.isArray(row) ||
                    !String(row.emmTemsisid || '').trim())))
                return {complete: false, failed: true};
            const payload = new Map();
            if (state?.generation) {
                for (const row of state.payload.list) {
                    const sid = String(row.emmTemsisid || '').trim();
                    if (payload.has(sid)) return {complete: false, failed: true};
                    payload.set(sid, JSON.stringify(row));
                }
            }
            if (document.readyState !== 'complete' || !$ || $.active !== 0 ||
                !$.fn?.dataTable?.isDataTable('#result-datatable') || !state ||
                !state.generation || state.generation !== state.completedGeneration ||
                state.status < 200 || state.status >= 300 ||
                state.query !== state.fingerprint() || state.payload?.result !== 'success' ||
                !Array.isArray(state.payload.list)) return unavailable;
            const api = $('#result-datatable').DataTable();
            const info = api.page.info();
            const rows = api.rows({page: 'all', search: 'none', order: 'applied'}).data().toArray();
            const nodes = api.rows({page: 'all', search: 'none', order: 'applied'}).nodes().toArray();
            if (info.serverSide !== false || api.settings()[0]?.bDrawing ||
                info.recordsTotal !== rows.length || rows.length !== state.payload.list.length ||
                nodes.length !== rows.length || nodes.some(node => !node)) return unavailable;
            function identity(href) {
                const url = new URL(href, location.href);
                if (url.origin !== location.origin || url.pathname !== '/ACS/ACS15002') return '';
                const values = url.searchParams.getAll('emmTemsisid');
                return values.length === 1 ? values[0] : '';
            }
            const candidates = [];
            const seen = new Set();
            for (let i = 0; i < rows.length; i++) {
                const row = rows[i];
                const sid = String(row?.emmTemsisid || '').trim();
                const links = nodes[i].querySelectorAll("a.btn_t02[href*='ACS15002']");
                // ACS renders this anchor from emmTemsisid; row.url points to an external report.
                if (!sid || seen.has(sid) || payload.get(sid) !== JSON.stringify(row) ||
                    links.length !== 1 || identity(links[0].getAttribute('href')) !== sid) return unavailable;
                seen.add(sid);
                candidates.push({href: links[0].getAttribute('href'), sid: sid,
                    text: String(nodes[i].innerText || nodes[i].textContent || '').trim()});
            }
            const current = api.rows({page: 'current'}).data().toArray()
                .map(row => String(row.emmTemsisid)).sort();
            const visible = Array.from(document.querySelectorAll(
                "#result-datatable a.btn_t02[href*='ACS15002']"))
                .map(link => identity(link.getAttribute('href'))).sort();
            if (current.some(sid => !sid || !seen.has(sid)) ||
                JSON.stringify(current) !== JSON.stringify(visible)) return unavailable;
            return {complete: true, candidates: candidates};
            """
        )
    except Exception:
        return None
    if isinstance(state, dict) and state.get("pending"):
        raise _ConsumableListPendingError("耗材查詢尚未完成，停止辨識，避免使用上次查詢資料。")
    if isinstance(state, dict) and state.get("failed"):
        raise RuntimeError("耗材查詢失敗或尚未完成，停止辨識，避免使用上次查詢資料。")
    if not isinstance(state, dict) or state.get("complete") is not True:
        return None
    candidates = state.get("candidates")
    if not isinstance(candidates, list) or not all(isinstance(item, dict) for item in candidates):
        return None
    return [dict(item) for item in candidates]


def _reset_consumable_page_to_first(driver: webdriver.Chrome) -> None:
    previous_signature = _consumable_candidates_signature(_read_consumable_candidates(driver))
    state = driver.execute_script(
        """
        const first = document.querySelector('#result-datatable_first, a.paginate_button.first');
        if (!first || first.classList.contains('disabled') || first.getAttribute('aria-disabled') === 'true') {
          return {changed: false};
        }
        first.click();
        return {changed: true};
        """
    )
    if not isinstance(state, dict) or not state.get("changed"):
        return
    if not _wait_for_consumable_page_change(driver, previous_signature):
        raise RuntimeError("耗材列表返回第1頁逾時，停止辨識。")


def _consumable_candidates_signature(candidates: list[dict[str, str]]) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (
            str(item.get("href") or ""),
            str(item.get("sid") or ""),
            str(item.get("text") or ""),
        )
        for item in candidates
    )


def _wait_for_consumable_page_change(
    driver: webdriver.Chrome,
    previous_signature: tuple[tuple[str, str, str], ...],
    timeout: float = 10.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current_signature = _consumable_candidates_signature(_read_consumable_candidates(driver))
        if current_signature and current_signature != previous_signature:
            return True
        time.sleep(0.2)
    return False


def _click_next_consumable_page_if_present(driver: webdriver.Chrome) -> bool:
    return bool(
        driver.execute_script(
            """
            const next = document.querySelector('#result-datatable_next, a.paginate_button.next');
            if (!next || next.classList.contains('disabled') || next.getAttribute('aria-disabled') === 'true') {
              return false;
            }
            next.click();
            return true;
            """
        )
    )


def _consumable_page_is_older_than_case(case_id: str, candidates: list[dict[str, str]]) -> bool:
    case_date = _yyyymmdd_from_case_id(case_id)
    if not case_date:
        return False
    candidate_dates = [
        candidate_date
        for item in candidates
        for candidate_date in [
            _yyyymmdd_from_row_text(str(item.get("text") or ""))
            or _yyyymmdd_from_sid(str(item.get("sid") or ""))
        ]
        if candidate_date
    ]
    return bool(candidate_dates) and max(candidate_dates) < case_date


def _read_consumable_candidates(driver: webdriver.Chrome) -> list[dict[str, str]]:
    raw_candidates = driver.execute_script(
        """
        return Array.from(document.querySelectorAll("a.btn_t02[href^='/ACS/ACS15002?emmTemsisid=']")).map((link) => {
            const row = link.closest("tr");
            return {
                href: link.getAttribute("href"),
                sid: new URL(link.href, window.location.href).searchParams.get('emmTemsisid') || '',
                text: row ? row.innerText : link.innerText
            };
        });
        """
    )
    if not isinstance(raw_candidates, list):
        return []
    return [dict(item) for item in raw_candidates if isinstance(item, dict)]


def _consumable_list_ui_ready(driver: webdriver.Chrome) -> bool:
    if not _is_consumable_maintenance_page(driver):
        return False
    return driver.execute_script(
        """
        const $ = window.jQuery, state = window.__ambulanceAcsList;
        if (document.readyState !== 'complete' || !$ || $.active !== 0 || !state ||
            !state.generation || state.generation !== state.completedGeneration ||
            state.query !== state.fingerprint()) return false;
        if (!Number.isInteger(state.status) || state.status < 200 || state.status >= 300 || state.payload?.result !== 'success' ||
            !Array.isArray(state.payload.list)) throw new Error('耗材當次正式查詢失敗');
        const payload = new Map();
        for (const row of state.payload.list) {
            const sid = String(row?.emmTemsisid || '');
            if (!sid || payload.has(sid)) throw new Error('耗材當次清單身分不完整');
            payload.set(sid, JSON.stringify(row));
        }
        const visible = Array.from(document.querySelectorAll(
            "#result-datatable a.btn_t02[href^='/ACS/ACS15002?emmTemsisid=']"));
        const identities = links => links.map(link => {
            const url = new URL(link.getAttribute('href'), location.href);
            const ids = url.searchParams.getAll('emmTemsisid');
            return url.origin === location.origin && url.pathname === '/ACS/ACS15002' &&
                ids.length === 1 && ids[0] && String(link.closest('tr')?.innerText || '').trim() ? ids[0] : '';
        });
        const ids = identities(visible);
        if (ids.some(id => !id) || new Set(ids).size !== ids.length) return false;
        if ($.fn?.dataTable?.isDataTable('#result-datatable')) {
            const api = $('#result-datatable').DataTable(), info = api.page.info();
            const rows = api.rows({page: 'current'}).data().toArray();
            const nodes = api.rows({page: 'current'}).nodes().toArray();
            const processing = document.querySelector('#result-datatable_processing');
            if (api.settings()[0]?.bDrawing || (processing && processing.getClientRects().length &&
                getComputedStyle(processing).visibility !== 'hidden') ||
                rows.length !== info.end - info.start || nodes.length !== rows.length ||
                nodes.some(node => !node)) return false;
            if (info.serverSide === false) {
                const allRows = api.rows({page: 'all', search: 'none'}).data().toArray();
                const allIds = allRows.map(row => String(row?.emmTemsisid || ''));
                if (info.recordsTotal !== payload.size || info.recordsDisplay !== info.recordsTotal ||
                    allRows.length !== payload.size || new Set(allIds).size !== payload.size ||
                    allRows.some(row => payload.get(String(row?.emmTemsisid || '')) !== JSON.stringify(row))) return false;
            } else if (info.serverSide !== true || rows.length !== payload.size) {
                return false;
            }
            const expected = rows.map(row => String(row?.emmTemsisid || ''));
            return rows.every(row => payload.get(String(row?.emmTemsisid || '')) === JSON.stringify(row)) &&
                expected.every(id => id && ids.includes(id)) &&
                JSON.stringify(expected.sort()) === JSON.stringify(ids.slice().sort()) &&
                (rows.length > 0 || info.recordsDisplay === 0);
        }
        const payloadIds = state.payload.list.map(row => String(row?.emmTemsisid || ''));
        return payloadIds.every(id => id && ids.includes(id)) &&
            JSON.stringify(payloadIds.sort()) === JSON.stringify(ids.slice().sort());
        """
    ) is True


def _stable_consumable_candidates(driver: webdriver.Chrome) -> list[dict[str, str]]:
    WebDriverWait(driver, 15).until(_consumable_list_ui_ready)
    candidates = _read_consumable_candidates(driver)
    if any(not item.get("href") or not item.get("sid") or not str(item.get("text") or "").strip()
           for item in candidates):
        raise RuntimeError("耗材候選列尚未完整載入，停止辨識。")
    return candidates


def _emm_temsis_id_from_href(href: str) -> str:
    return str(parse_qs(urlparse(str(href or "")).query).get("emmTemsisid", [""])[0])


def _patient_sid_parts(sid: str) -> tuple[str, str]:
    value = str(sid or "").strip()
    if len(value) < 3 or not value[-2:].isdigit():
        raise RuntimeError(f"TEMSISID 無法辨識患者序號：{value or '空白'}")
    return value[:-2], value[-2:]


def _select_consumable_patient_group(scored: list[tuple[int, str, str]]) -> list[str]:
    if len(scored) == 1:
        return [scored[0][1]]
    groups: dict[str, list[tuple[int, int, str]]] = {}
    for score, href, _ in scored:
        body, suffix = _patient_sid_parts(_emm_temsis_id_from_href(href))
        groups.setdefault(body, []).append((int(suffix), score, href))
    if len(groups) != 1:
        raise RuntimeError("同案耗材存在多組無法唯一辨識的 TEMSISID。")
    selected = next(iter(groups.values()))
    suffixes = [item[0] for item in selected]
    if len(suffixes) != len(set(suffixes)):
        raise RuntimeError("同案耗材 TEMSISID 患者序號重複。")
    return [item[2] for item in sorted(selected, key=lambda item: item[0])]


def _consumable_sid_score(case_id: str, sid: str) -> int:
    event_key = _case_id_temsis_event_key(case_id)
    sid_digits = "".join(ch for ch in str(sid or "") if ch.isdigit())
    if event_key and sid_digits.startswith(event_key):
        return 120
    fragments = _case_id_sid_fragments(case_id)
    if not fragments:
        return 0
    for fragment in fragments:
        if fragment and fragment in sid_digits:
            return 20 if len(fragment) > 6 else 10
    return 0


def _case_id_temsis_event_key(case_id: str) -> str:
    digits = "".join(ch for ch in str(case_id or "") if ch.isdigit())
    if len(digits) < 20 or digits[:2] not in {"19", "20"}:
        return ""
    return f"{digits[:8]}{digits[14:20]}"


def _case_id_sid_fragments(case_id: str) -> list[str]:
    digits = "".join(ch for ch in str(case_id or "") if ch.isdigit())
    if len(digits) < 6:
        return []
    fragments = [digits]
    if len(digits) >= 14:
        fragments.append(digits[:14])
        fragments.append(digits[8:])
        fragments.append(digits[8:14])
    else:
        fragments.append(digits[-6:])
    return [fragment for fragment in dict.fromkeys(fragments) if len(fragment) >= 6]


def _consumable_candidate_date_matches(case_id: str, sid: str, text: str) -> bool:
    case_date = _yyyymmdd_from_case_id(case_id)
    if not case_date:
        return True
    row_date = _yyyymmdd_from_row_text(text)
    sid_date = _yyyymmdd_from_sid(sid)
    candidate_date = row_date or sid_date
    if not candidate_date or candidate_date == case_date:
        return True
    if not row_date or sid_date != case_date:
        return False
    try:
        next_case_date = (datetime.strptime(case_date, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
    except ValueError:
        return False
    return row_date == next_case_date


def _yyyymmdd_from_case_id(case_id: str) -> str:
    digits = "".join(ch for ch in str(case_id or "") if ch.isdigit())
    if len(digits) >= 8 and digits[:2] in {"19", "20"}:
        return digits[:8]
    return ""


def _yyyymmdd_from_sid(sid: str) -> str:
    digits = "".join(ch for ch in str(sid or "") if ch.isdigit())
    if len(digits) >= 8 and digits[:2] in {"19", "20"}:
        return digits[:8]
    return ""


def _yyyymmdd_from_row_text(text: str) -> str:
    match = re.search(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b", str(text or ""))
    if not match:
        return ""
    return f"{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d}"


def _text_matches_vehicle(text: str, vehicle: str) -> bool:
    needles = _vehicle_match_tokens(vehicle)
    haystack = re.sub(r"\s+", "", str(text or ""))
    return any(needle in haystack for needle in needles)


def _vehicle_match_tokens(vehicle: str) -> list[str]:
    vehicle_text = str(vehicle or "").strip()
    tokens = [vehicle_text]
    ppe_name = vehicle_ppe_names().get(vehicle_text, "")
    if ppe_name:
        tokens.append(ppe_name)
    normalized: list[str] = []
    for token in tokens:
        value = re.sub(r"\s+", "", token)
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _consumable_detail_vehicle_label(driver: webdriver.Chrome) -> str:
    try:
        state = driver.execute_script(
            """
            const field = document.querySelector('#callNo');
            return {hasCallNoField: !!field, callNo: String(field?.value || '').trim()};
            """
        )
    except Exception:
        state = None
    if isinstance(state, dict):
        if not state.get("hasCallNoField"):
            return ""
        page_texts = (str(state.get("callNo") or ""),)
    else:
        page_texts = (_consumable_detail_control_text(driver), _consumable_detail_body_text(driver))
    for page_text in page_texts:
        for label in vehicle_ppe_names():
            if _text_matches_vehicle(page_text, label):
                return label
    return ""


def _consumable_detail_case_id(driver: webdriver.Chrome) -> str:
    try:
        return str(
            driver.execute_script(
                "return String(document.querySelector('#csNo')?.value || '').trim();"
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _normalized_consumable_case_id(value: object) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 14 and digits[:2] in {"19", "20"}:
        return digits
    return ""


def _consumable_detail_body_text(driver: webdriver.Chrome) -> str:
    try:
        return str(driver.find_element(By.TAG_NAME, "body").text or "")
    except Exception:
        return ""


def _consumable_detail_control_text(driver: webdriver.Chrome) -> str:
    try:
        return str(driver.execute_script(
            """
            return Array.from(document.querySelectorAll('input, select, textarea')).flatMap((node) => {
                const values = [node.value || ''];
                if (node.tagName === 'SELECT' && node.selectedIndex >= 0) {
                    values.push(node.options[node.selectedIndex].text || '');
                }
                return values;
            }).join(' ');
            """
        ) or "")
    except Exception:
        return ""


def _consumable_detail_page_text(driver: webdriver.Chrome) -> str:
    return " ".join(
        part for part in (_consumable_detail_control_text(driver), _consumable_detail_body_text(driver)) if part
    )


def _consumable_vehicle_notice(driver: webdriver.Chrome, request: AmbulanceReturnRequest) -> str:
    expected_vehicle = str(request.vehicle or "").strip()
    actual_vehicle = _consumable_detail_vehicle_label(driver)
    if expected_vehicle and actual_vehicle and expected_vehicle != actual_vehicle:
        return f" APP車輛={expected_vehicle}，耗材內容頁出勤單位={actual_vehicle}，已依內容頁車輛登打。"
    return ""


def _find_consumable_href_by_vehicle_code(
    driver: webdriver.Chrome,
    hrefs: list[str],
    vehicle: str,
    case_id: str = "",
) -> str:
    matched = _find_consumable_hrefs_by_vehicle_code(driver, hrefs, vehicle, case_id)
    return matched[0] if matched else ""


def _find_consumable_hrefs_by_vehicle_code(
    driver: webdriver.Chrome,
    hrefs: list[str],
    vehicle: str,
    case_id: str = "",
    candidate_records: list[dict[str, str]] | None = None,
) -> list[str]:
    expected_vehicle = str(vehicle or "").strip()
    if not expected_vehicle:
        return []
    expected_case_id = _normalized_consumable_case_id(case_id)
    matched: list[str] = []
    for href in hrefs:
        _open_consumable_detail_if_needed(driver, href)
        wait = WebDriverWait(driver, 15)
        if not _wait_for_consumable_detail_page(driver, wait):
            suffix = _patient_sid_parts(_emm_temsis_id_from_href(href))[1]
            raise RuntimeError(f"耗材內容頁無法載入：患者序號 {suffix}，停止整批辨識。")
        actual_case_id = _normalized_consumable_case_id(_consumable_detail_case_id(driver))
        if expected_case_id and actual_case_id != expected_case_id:
            continue
        actual_vehicle = _consumable_detail_vehicle_label(driver)
        if actual_vehicle == expected_vehicle:
            matched.append(href)
        elif candidate_records is not None and actual_vehicle:
            candidate_records.append(
                {
                    "vehicle": actual_vehicle,
                    "source": "一站通耗材",
                    "record_id": _emm_temsis_id_from_href(href),
                    "case_id": actual_case_id,
                }
            )
    return matched


def _text_matches_any_vehicle_token(text: str, vehicle_tokens: list[str]) -> bool:
    haystack = re.sub(r"\s+", "", str(text or ""))
    return any(token in haystack for token in vehicle_tokens)


def _hhmm_from_row_text(text: str) -> str:
    import re

    match = re.search(r"\b\d{4}/\d{2}/\d{2}\s+(\d{2}):(\d{2}):\d{2}\b", text)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    match = re.search(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\b", str(text or ""))
    if match:
        return f"{int(match.group(1)):02d}{match.group(2)}"
    return ""


def _needs_extra_consumable_row(request: AmbulanceReturnRequest) -> bool:
    return any(name and qty > 0 for name, qty in request.consumables.items())


def _resolve_consumable_item_quantities(
    driver: webdriver.Chrome,
    request: AmbulanceReturnRequest,
    classification_context: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    inventory_by_name = {str(item["name"]): str(item["code"]) for item in consumable_inventory_options()}
    items: list[dict[str, str]] = []
    for name, qty in request.consumables.items():
        if not name or qty <= 0:
            continue
        code = inventory_by_name.get(name, "")
        if not code:
            raise RuntimeError(f"找不到耗材代碼：{name}")
        class_type, class_id_prefix, series_prefix, item_prefix = _parse_consumable_code(code)
        item_id = _resolve_consumable_item_id(
            driver,
            class_type=class_type,
            class_id_prefix=class_id_prefix,
            series_prefix=series_prefix,
            item_prefix=item_prefix,
            item_name=name,
            classification_context=classification_context,
        )
        items.append({"itemId": item_id, "quantity": str(int(qty)), "name": name})
    if not items:
        raise RuntimeError("沒有可儲存的耗材項目。")
    return items


def _resolve_consumable_item_id(
    driver: webdriver.Chrome,
    *,
    class_type: str,
    class_id_prefix: str,
    series_prefix: str,
    item_prefix: str,
    item_name: str,
    classification_context: dict[str, object] | None = None,
) -> str:
    if classification_context is not None and (
        classification_context["driver"] is not driver
        or classification_context["session_id"] != driver.session_id
        or classification_context["account"] != driver.__dict__.get("_acs_authenticated_account")
        or classification_context["window"] != driver.current_window_handle
    ):
        raise RuntimeError("耗材分類查詢的登入帳號或 session 已變更，停止處理。")
    result = driver.execute_script(
        """
        const classType = arguments[0];
        const classIdPrefix = arguments[1];
        const seriesPrefix = arguments[2];
        const itemPrefix = arguments[3];
        const maps = arguments[4] || {};
        if (arguments[5] && (location.origin !== 'https://nfaemsap3.nfa.gov.tw' ||
            location.pathname !== '/ACS/ACS15002' || !window.jQuery)) {
            throw new Error('耗材分類查詢已離開原登入明細頁');
        }

        function post(endpoint, data) {
            const key = JSON.stringify([endpoint, Object.entries(data).sort()]);
            if (Object.prototype.hasOwnProperty.call(maps, key)) return maps[key];
            let response = null;
            let error = null;
            window.jQuery.ajax({
                url: contextPath + endpoint,
                method: 'POST',
                data: data,
                async: false,
                success: function (result) {
                    response = result;
                },
                error: function (xhr, status, err) {
                    error = status + ': ' + err;
                }
            });
            if (error) {
                throw new Error(error);
            }
            if (!response || response.result !== 'success' || !response.map ||
                typeof response.map !== 'object' || Array.isArray(response.map)) {
                throw new Error((response && response.msg) || endpoint + ' failed');
            }
            maps[key] = response.map;
            return response.map;
        }

        function findKey(map, prefix) {
            const normalizedPrefix = String(prefix).trim() + ' ';
            const matches = Object.entries(map).filter(([_key, value]) =>
                String(value).trim().startsWith(normalizedPrefix));
            if (matches.length > 1) {
                throw new Error('耗材代碼前綴不唯一：' + prefix);
            }
            return matches.length === 1 ? matches[0][0] : '';
        }

        const classId = findKey(post('ACS15002/getAcsClassId', {type: classType}), classIdPrefix);
        if (!classId) {
            return {ok: false, detail: '找不到物品分類 ' + classIdPrefix};
        }
        const seriesId = findKey(post('ACS15002/getAcsSeriesId', {classId: classId}), seriesPrefix);
        if (!seriesId) {
            return {ok: false, detail: '找不到物品系列 ' + seriesPrefix};
        }
        const itemId = findKey(post('ACS15002/getAcsItemId', {seriesId: seriesId}), itemPrefix);
        if (!itemId) {
            return {ok: false, detail: '找不到物品規格 ' + itemPrefix};
        }
        return {ok: true, itemId: itemId, maps: maps};
        """,
        class_type,
        class_id_prefix,
        series_prefix,
        item_prefix,
        classification_context["maps"] if classification_context is not None else {},
        classification_context is not None,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("detail") if isinstance(result, dict) else str(result)
        raise RuntimeError(f"{item_name} 解析耗材 itemId 失敗：{detail}")
    if classification_context is not None:
        classification_context["maps"] = result["maps"]
    return str(result["itemId"])


def _open_add_new_goods_box(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    before_count = _consumable_row_count(driver)
    ok = driver.execute_script(
        """
        const link = document.querySelector('a[href="#addNewGoodsBox"]');
        if (!link) {
            return false;
        }
        link.scrollIntoView({block: 'center', inline: 'nearest'});
        if (window.jQuery) {
            window.jQuery(link).trigger('click');
        } else {
            link.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
            link.click();
        }
        return true;
        """
    )
    if not ok:
        raise RuntimeError("找不到新增一種按鈕。")
    if before_count > 0:
        wait.until(lambda d: _consumable_row_count(d) > before_count)
    else:
        wait.until(lambda d: _consumable_row_count(d) > 0 or _add_new_box_visible(d))


def _fill_consumables(driver: webdriver.Chrome, wait: WebDriverWait, request: AmbulanceReturnRequest) -> int:
    inventory_by_name = {str(item["name"]): str(item["code"]) for item in consumable_inventory_options()}
    filled_count = 0
    for name, qty in request.consumables.items():
        if not name or qty <= 0:
            continue
        code = inventory_by_name.get(name, "")
        if not code:
            raise RuntimeError(f"找不到耗材代碼：{name}")
        class_type, class_id_prefix, series_prefix, item_prefix = _parse_consumable_code(code)
        _ensure_blank_consumable_row(driver, wait)
        _fill_blank_consumable_row(
            driver,
            wait,
            class_type=class_type,
            class_id_prefix=class_id_prefix,
            series_prefix=series_prefix,
            item_prefix=item_prefix,
            item_name=name,
            quantity=qty,
        )
        filled_count += 1
    return filled_count


def _clear_existing_consumables(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    _remove_consumable_rows_by_trash_button(driver, wait)
    if _consumable_payload_row_count(driver) > 0:
        _remove_consumable_rows_from_dom(driver)
    wait.until(lambda d: _consumable_row_count(d) == 0 and _consumable_payload_row_count(d) == 0)
    remaining = _read_consumable_payload_rows(driver, include_blank=True)
    if remaining:
        raise RuntimeError(f"耗材清除後仍剩 {len(remaining)} 筆，停止填寫。")


def _inject_consumables_for_save(driver: webdriver.Chrome, item_quantities: list[dict[str, str]]) -> None:
    ok = driver.execute_script(
        """
        const list = document.querySelector('.goods_list > ul');
        if (!list) {
            return false;
        }
        list.innerHTML = '';
        for (const item of arguments[0]) {
            const li = document.createElement('li');
            li.style.display = 'none';
            li.innerHTML = `
                <div class="goods_box">
                    <div class="snu">
                        <ul>
                            <li class="snu_one">
                                <select class="acs_item_id">
                                    <option value="${String(item.itemId).replaceAll('"', '&quot;')}" selected></option>
                                </select>
                                <input type="text" name="itemQuantity" value="${String(item.quantity).replaceAll('"', '&quot;')}">
                            </li>
                        </ul>
                    </div>
                </div>`;
            list.appendChild(li);
        }
        return document.querySelectorAll('.snu_one').length === arguments[0].length;
        """,
        item_quantities,
    )
    if not ok:
        raise RuntimeError("耗材儲存資料注入失敗，未按儲存。")


def _remove_consumable_rows_by_trash_button(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    for _ in range(20):
        before_count = _consumable_payload_row_count(driver)
        if before_count == 0:
            return
        clicked = _click_consumable_row_delete(driver)
        if not clicked:
            return
        try:
            wait.until(lambda d: _consumable_payload_row_count(d) < before_count)
        except TimeoutException:
            return


def _remove_consumable_rows_from_dom(driver: webdriver.Chrome) -> None:
    driver.execute_script(
        """
        const list = document.querySelector('.goods_list > ul');
        if (list) {
            Array.from(list.children).forEach((row) => row.remove());
        }
        """
    )


def _save_consumables_direct(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    item_quantities: list[dict[str, str]],
    cancel_check: Callable[[], None] | None = None,
) -> None:
    driver.set_script_timeout(20)
    account = _load_acs_credentials()[0]
    if cancel_check is not None:
        cancel_check()
    result = driver.execute_async_script(
        """
        const itemQuantities = arguments[0];
        const account = arguments[1];
        const done = arguments[arguments.length - 1];
        const params = new URLSearchParams(window.location.search);
        const emmTemsisid = params.get('emmTemsisid') || document.querySelector('#emmTemsisid')?.value || '';
        if (!emmTemsisid) {
            done({ok: false, detail: '找不到案件 emmTemsisid'});
            return;
        }

        const jsonObj = {
            acsEmmFormPojo: {
                emmTemsisid: emmTemsisid,
                luserCid: '03',
                luserId: account
            },
            acsEmmFormItemDTOList: itemQuantities.map((item) => ({
                acsEmmFormItemPojo: {
                    itemId: String(item.itemId),
                    quantity: String(item.quantity)
                }
            }))
        };

        fetch(contextPath + 'ACS15002/saveAcsEmmForm', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(jsonObj)
        }).then(async (response) => {
            const text = await response.text();
            let payload = null;
            try {
                payload = JSON.parse(text);
            } catch (error) {
                payload = {raw: text};
            }
            if (!response.ok) {
                done({ok: false, detail: response.status + ' ' + response.statusText, payload});
                return;
            }
            if (payload && payload.result === 'fail') {
                done({ok: false, detail: payload.msg || '儲存失敗', payload});
                return;
            }
            done({ok: true, payload});
        }).catch((error) => {
            done({ok: false, detail: String(error)});
        });
        """,
        item_quantities,
        account,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("detail") if isinstance(result, dict) else str(result)
        raise RuntimeError(f"耗材直接儲存失敗：{detail}")
    time.sleep(0.8)
    driver.refresh()
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    wait.until(lambda d: _consumable_row_count(d) >= len(item_quantities))
    _verify_saved_consumables(driver, item_quantities)


def _verify_saved_consumables(driver: webdriver.Chrome, expected_items: list[dict[str, str]]) -> None:
    expected = _normalize_consumable_pairs(expected_items)
    actual = _normalize_consumable_pairs(_read_consumable_payload_rows(driver))
    if actual != expected:
        raise RuntimeError(f"耗材儲存後讀回不一致：expected={expected} actual={actual}")


def _reopen_consumable_detail_for_readback(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    detail_url: str,
    expected_sid: str,
) -> None:
    driver.get(detail_url)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    if not _wait_for_consumable_detail_page(driver, wait):
        raise RuntimeError("耗材儲存後無法重新開啟內容頁讀回。")
    if _is_sso_page(driver):
        raise RuntimeError("耗材儲存後讀回時被導回 SSO 登入頁。")
    actual_sid = _emm_temsis_id_from_href(str(driver.current_url or ""))
    if actual_sid != expected_sid:
        raise RuntimeError(f"耗材儲存後讀回案件不符：expected={expected_sid} actual={actual_sid or '空白'}")


def _assert_consumable_rows_match(
    driver: webdriver.Chrome,
    expected_items: list[dict[str, str]],
    label: str,
) -> None:
    expected = _normalize_consumable_pairs(expected_items)
    actual = _normalize_consumable_pairs(_read_consumable_payload_rows(driver, include_blank=True))
    if actual != expected:
        raise RuntimeError(f"{label}資料不一致，停止儲存：expected={expected} actual={actual}")


def _normalize_consumable_pairs(items: list[dict[str, str]]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in items:
        item_id = str(item.get("itemId") or "").strip()
        quantity_text = str(item.get("quantity") or "").strip()
        if not item_id and not quantity_text:
            pairs.append(("", ""))
            continue
        try:
            quantity_text = str(int(quantity_text))
        except ValueError:
            pass
        pairs.append((item_id, quantity_text))
    return sorted(pairs)


def _read_consumable_payload_rows(driver: webdriver.Chrome, include_blank: bool = False) -> list[dict[str, str]]:
    rows = driver.execute_script(
        """
        return Array.from(document.querySelectorAll('.snu_one')).map((snu) => {
            const item = snu.querySelector('.acs_item_id');
            const quantity = snu.querySelector('input[name="itemQuantity"]');
            return {
                itemId: item ? item.value : '',
                quantity: quantity ? quantity.value : ''
            };
        });
        """
    )
    if not isinstance(rows, list):
        return []
    normalized = [
        {"itemId": str(item.get("itemId") or ""), "quantity": str(item.get("quantity") or "")}
        for item in rows
        if isinstance(item, dict)
    ]
    if include_blank:
        return normalized
    return [item for item in normalized if item["itemId"] and item["quantity"] and item["quantity"] != "0"]


def _click_consumable_row_delete(driver: webdriver.Chrome) -> bool:
    buttons = driver.find_elements(By.CSS_SELECTOR, ".goods_list > ul > li .goods_box_remove")
    if not buttons:
        return False
    button = buttons[0]
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", button)
    try:
        ActionChains(driver).move_to_element(button).click().perform()
        return True
    except Exception:
        pass
    return bool(
        driver.execute_script(
            """
            const button = arguments[0];
            if (!button) {
                return false;
            }
            if (window.jQuery) {
                window.jQuery(button).trigger('click');
                return true;
            }
            button.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
            button.click();
            return true;
            """,
            button,
        )
    )


def _filled_consumable_row_count(driver: webdriver.Chrome) -> int:
    return int(
        driver.execute_script(
            """
            return Array.from(document.querySelectorAll('select.acs_class_type'))
                .filter((select) => select.value)
                .length;
            """
        )
    )


def _save_consumables(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    cancel_check: Callable[[], None] | None = None,
) -> str:
    button = wait.until(EC.presence_of_element_located((By.ID, "addAcsEmmForm")))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", button)
    if cancel_check is not None:
        cancel_check()
    driver.execute_script(
        """
        const button = arguments[0];
        window.__acsSaveClickedAt = Date.now();
        button.click();
        """,
        button,
    )
    alert_text = _accept_alert_if_present(driver, timeout_seconds=5)
    confirmation = _consumable_save_confirmation_state(alert_text)
    if confirmation == "failure":
        raise RuntimeError(f"耗材儲存失敗：{alert_text}")
    if not re.fullmatch(r"(?:儲存成功|存檔成功|成功儲存|操作成功|儲存完成|存檔完成)[。.!！]?", alert_text.strip()):
        time.sleep(1.0)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    return alert_text


def _accept_alert_if_present(driver: webdriver.Chrome, timeout_seconds: int) -> str:
    try:
        WebDriverWait(driver, timeout_seconds).until(EC.alert_is_present())
        alert = driver.switch_to.alert
        text = str(alert.text or "").strip()
        alert.accept()
        return text
    except Exception:
        return ""


def _consumable_save_confirmation_state(message: str) -> str:
    compact = re.sub(r"\s+", "", str(message or "")).lower()
    if not compact:
        return "unknown"
    if any(marker in compact for marker in ("失敗", "錯誤", "未成功", "無法儲存", "error", "failed")):
        return "failure"
    if any(
        marker in compact
        for marker in ("儲存成功", "存檔成功", "成功儲存", "操作成功", "儲存完成", "存檔完成", "success")
    ):
        return "success"
    return "unknown"


def _parse_consumable_code(code: str) -> tuple[str, str, str, str]:
    parts = code.split("-")
    if len(parts) < 4:
        raise RuntimeError(f"耗材代碼格式錯誤：{code}")
    return parts[0], parts[1], parts[2], parts[3]


def _ensure_blank_consumable_row(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    if _has_blank_consumable_row(driver):
        return
    _open_add_new_goods_box(driver, wait)
    wait.until(lambda d: _has_blank_consumable_row(d))


def _has_blank_consumable_row(driver: webdriver.Chrome) -> bool:
    return bool(
        driver.execute_script(
            """
            return Array.from(document.querySelectorAll('select.acs_class_type')).some((select) => !select.value);
            """
        )
    )


def _fill_blank_consumable_row(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    *,
    class_type: str,
    class_id_prefix: str,
    series_prefix: str,
    item_prefix: str,
    item_name: str,
    quantity: int,
) -> None:
    ok = driver.execute_script(
        """
        const row = Array.from(document.querySelectorAll('select.acs_class_type'))
            .map((select) => select.closest('li') || select.closest('tr') || select.closest('div'))
            .find((root) => root && !root.querySelector('select.acs_class_type').value);
        if (!row) {
            return false;
        }

        window.__acsFillTargetRow = row;

        function setChosen(select, value) {
            select.value = value;
            if (window.jQuery) {
                window.jQuery(select).val(value).trigger('chosen:updated').trigger('change');
            } else {
                select.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }

        setChosen(row.querySelector('select.acs_class_type'), arguments[0]);
        return true;
        """,
        class_type,
    )
    if not ok:
        raise RuntimeError(f"找不到空白耗材列：{item_name}")

    _select_row_option(driver, wait, "select.acs_class_id", class_id_prefix, item_name)
    _select_row_option(driver, wait, "select.acs_series_id", series_prefix, item_name)
    _select_row_option(driver, wait, "select.acs_item_id", item_prefix, item_name)
    ok = driver.execute_script(
        """
        const row = window.__acsFillTargetRow;
        if (!row) {
            return false;
        }
        const qty = row.querySelector('input[name="itemQuantity"], input.qty');
        if (!qty) {
            return false;
        }
        qty.value = String(arguments[0]);
        qty.dispatchEvent(new Event('input', {bubbles: true}));
        qty.dispatchEvent(new Event('change', {bubbles: true}));
        return qty.value === String(arguments[0]);
        """,
        int(quantity),
    )
    if not ok:
        raise RuntimeError(f"耗材數量填入失敗：{item_name}")


def _select_row_option(driver: webdriver.Chrome, wait: WebDriverWait, selector: str, prefix: str, item_name: str) -> None:
    wait.until(lambda d: _row_option_available(d, selector, prefix))
    ok = driver.execute_script(
        """
        const row = window.__acsFillTargetRow;
        if (!row) {
            return false;
        }
        const select = row.querySelector(arguments[0]);
        if (!select) {
            return false;
        }
        const prefix = `${arguments[1]} `;
        const option = Array.from(select.options).find((item) => item.text.trim().startsWith(prefix));
        if (!option) {
            return false;
        }
        select.value = option.value;
        if (window.jQuery) {
            window.jQuery(select).val(option.value).trigger('chosen:updated').trigger('change');
        } else {
            select.dispatchEvent(new Event('change', {bubbles: true}));
        }
        return select.value === option.value;
        """,
        selector,
        prefix,
    )
    if not ok:
        raise RuntimeError(f"耗材欄位選取失敗：{item_name} / {selector} / {prefix}")


def _row_option_available(driver: webdriver.Chrome, selector: str, prefix: str) -> bool:
    return bool(
        driver.execute_script(
            """
            const row = window.__acsFillTargetRow;
            if (!row) {
                return false;
            }
            const select = row.querySelector(arguments[0]);
            if (!select) {
                return false;
            }
            const prefix = `${arguments[1]} `;
            return Array.from(select.options).some((item) => item.text.trim().startsWith(prefix));
            """,
            selector,
            prefix,
        )
    )


def _goods_box_count(driver: webdriver.Chrome) -> int:
    return int(
        driver.execute_script(
            "return document.querySelectorAll('.goods_box, .goodsBox, [class*=\"goods_box\"]').length;"
        )
    )


def _consumable_row_count(driver: webdriver.Chrome) -> int:
    return int(driver.execute_script("return document.querySelectorAll('select.acs_class_type').length;"))


def _consumable_payload_row_count(driver: webdriver.Chrome) -> int:
    return int(driver.execute_script("return document.querySelectorAll('.snu_one').length;"))


def _add_new_box_visible(driver: webdriver.Chrome) -> bool:
    return bool(
        driver.execute_script(
            """
            const box = document.querySelector('#addNewGoodsBox, [id*="addNewGoodsBox"]');
            if (!box) return false;
            const style = window.getComputedStyle(box);
            const rect = box.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            """
        )
    )
