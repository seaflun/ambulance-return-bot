from __future__ import annotations

import os
import re
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlparse

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
from ambulance_bot.duty_credentials import load_synced_worker_credential
from ambulance_bot.models import AmbulanceReturnRequest, clean_case_address, normalize_hhmm, vehicle_ppe_names
from ambulance_bot.profile_paths import runtime_profile_dir
from ambulance_bot.task_cancellation import TaskCancellationError
from ambulance_bot.update_safety import ManualUpdateRequiredError, manual_update_reason
from ambulance_bot.window_layout import apply_tile


SSO_URL = "https://nfaemsap3.nfa.gov.tw/SSO/"
ACS_URL = "https://nfaemsap3.nfa.gov.tw/ACS/ACS15001"
OUTPUT_DIR = Path(os.getenv("CAPTCHA_OUTPUT_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ambulance_return_bot" / "captcha")
CAPTCHA_PATH = OUTPUT_DIR / "nfa_acs_captcha.png"
MAX_LOGIN_ATTEMPTS = 3
SUPPLEMENTAL_GLOVE_NAME = "桃-9吋手套-L(雙)"

ocr = ddddocr.DdddOcr(show_ad=False)


def save_consumables_record_enabled() -> bool:
    return os.getenv("SAVE_CONSUMABLES_RECORD", "true").strip().lower() in {"1", "true", "yes", "on"}


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

    driver = create_chrome_driver_with_retry(options, "一站通耗材")
    page_timeout = int(os.getenv("SELENIUM_PAGE_LOAD_TIMEOUT_SECONDS", "45"))
    driver.set_page_load_timeout(page_timeout)
    driver.set_script_timeout(page_timeout)
    apply_tile(driver, tile_name)
    try:
        login_error = ""
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
        return driver
    except Exception:
        _save_failure_artifacts(driver)
        raise


def open_consumable_record_for_task(
    driver: webdriver.Chrome,
    task: dict[str, object] | AmbulanceReturnRequest,
    cancel_check: Callable[[], None] | None = None,
    update_context: dict[str, object] | None = None,
) -> str:
    mark_driver_operation_active(driver)
    try:
        return _open_consumable_record_for_task(
            driver,
            task,
            cancel_check=cancel_check,
            update_context=update_context,
        )
    finally:
        mark_driver_operation_active(driver, False)


def _open_consumable_record_for_task(
    driver: webdriver.Chrome,
    task: dict[str, object] | AmbulanceReturnRequest,
    cancel_check: Callable[[], None] | None = None,
    update_context: dict[str, object] | None = None,
) -> str:
    request = task if isinstance(task, AmbulanceReturnRequest) else AmbulanceReturnRequest.from_dict(task)
    manual_reason = manual_update_reason("consumables", request, update_context)
    if manual_reason:
        raise ManualUpdateRequiredError(f"manual correction required: {manual_reason}")
    wait = WebDriverWait(driver, 15)
    _open_consumable_maintenance_page(driver, wait)
    hrefs = _find_consumable_detail_hrefs(driver, request)
    if len(hrefs) > 1 and not save_consumables_record_enabled():
        raise RuntimeError("同案多患者耗材必須啟用自動儲存，否則切換頁面會遺失未送出的資料。")
    allocations = _distribute_consumables(request.consumables, len(hrefs))
    completed: list[str] = []
    summaries: list[str] = []
    supplements: list[str] = []
    single_detail = ""
    for index, (href, allocation) in enumerate(zip(hrefs, allocations)):
        suffix = _patient_sid_parts(_emm_temsis_id_from_href(href))[1]
        try:
            driver.get(urljoin("https://nfaemsap3.nfa.gov.tw", href))
            if not _wait_for_consumable_detail_page(driver, wait):
                raise RuntimeError("consumable detail page did not open; SSO login may be required")
            actual_vehicle = _consumable_detail_vehicle_label(driver)
            if len(hrefs) > 1 and not actual_vehicle:
                raise RuntimeError(f"患者序號 {suffix} 無法讀取耗材頁車輛。")
            if request.vehicle and actual_vehicle and actual_vehicle != request.vehicle:
                raise RuntimeError(f"患者序號 {suffix} 車輛不符：預期={request.vehicle} 實際={actual_vehicle}")
            page_request = replace(request, consumables=dict(allocation))
            single_detail = _write_current_consumable_page(
                driver,
                wait,
                page_request,
                **({"cancel_check": cancel_check} if cancel_check is not None else {}),
            )
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
    detail = f"辨識{request.vehicle}同案{len(hrefs)}位患者；{'、'.join(summaries)}，{page_label}均已儲存確認。"
    if supplements:
        detail = f"{detail} {' '.join(supplements)}"
    return detail


def _write_current_consumable_page(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    request: AmbulanceReturnRequest,
    cancel_check: Callable[[], None] | None = None,
) -> str:
    added = ""
    if _needs_extra_consumable_row(request):
        detail_url = str(driver.current_url or "")
        expected_sid = _emm_temsis_id_from_href(detail_url)
        if not expected_sid:
            raise RuntimeError("耗材內容頁缺少 emmTemsisid，停止填寫。")
        item_quantities = _resolve_consumable_item_quantities(driver, request)
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
    credential = load_synced_worker_credential()
    if credential is not None:
        acs_account = credential.id_number.strip() or (credential.user_id if re.fullmatch(r"[A-Za-z][0-9]{9}", credential.user_id) else "")
        if acs_account and credential.password:
            return acs_account, credential.password
        if credential.password:
            _lookup_synced_credential_id_number_for_acs()
            credential = load_synced_worker_credential()
            if credential is not None:
                acs_account = credential.id_number.strip() or (credential.user_id if re.fullmatch(r"[A-Za-z][0-9]{9}", credential.user_id) else "")
                if acs_account and credential.password:
                    return acs_account, credential.password

    raise RuntimeError("找不到一站通耗材帳密；請先在 worker GUI 同步含身分證字號的帳號。")


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


def _save_failure_artifacts(driver: webdriver.Chrome) -> None:
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(OUTPUT_DIR / "nfa_acs_login_failed.png"))
        (OUTPUT_DIR / "nfa_acs_login_failed.html").write_text(driver.page_source, encoding="utf-8")
    except Exception:
        pass


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
        cards = driver.find_elements(By.CSS_SELECTOR, "img.cardImg[src*='ACS.jpg']")
        if cards:
            cards[0].click()
            wait.until(lambda d: "/ACS/" in d.current_url)
        else:
            driver.get("https://nfaemsap3.nfa.gov.tw/ACS/ACS13001")
            wait.until(lambda d: "/ACS/" in d.current_url)

    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    if _is_sso_page(driver):
        raise RuntimeError("ACS system returned to SSO login page")
    time.sleep(1.2)
    _open_consumable_maintenance_page(driver, wait)


def _open_consumable_maintenance_page(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    for attempt in range(1, 4):
        driver.get(ACS_URL)
        wait.until(
            lambda d: _is_consumable_maintenance_page(d)
            or _is_sso_page(d)
            or d.execute_script("return document.readyState") in {"interactive", "complete"}
        )
        time.sleep(0.8)
        if _is_consumable_maintenance_page(driver):
            return
        if _is_sso_page(driver):
            raise RuntimeError("進入耗材紀錄頁時被導回 SSO，登入 session 尚未建立。")
    raise RuntimeError("已登入 ACS，但無法進入耗材紀錄頁 ACS15001。")


def _is_consumable_maintenance_page(driver: webdriver.Chrome) -> bool:
    if "ACS15001" in driver.current_url:
        return True
    text = driver.find_element(By.TAG_NAME, "body").text
    return "救護紀錄表耗材維護" in text or "救護紀錄表列表" in text


def _is_consumable_detail_page(driver: webdriver.Chrome) -> bool:
    if "ACS15002" in driver.current_url:
        return True
    text = driver.find_element(By.TAG_NAME, "body").text
    return "TEMSISID" in text or "emmTemsisid" in text


def _wait_for_consumable_detail_page(driver: webdriver.Chrome, wait: WebDriverWait) -> bool:
    try:
        wait.until(lambda d: _is_consumable_detail_page(d) or _is_sso_page(d))
    except TimeoutException:
        return False
    return _is_consumable_detail_page(driver)


def _find_consumable_detail_href(driver: webdriver.Chrome, request: AmbulanceReturnRequest) -> str:
    return _find_consumable_detail_hrefs(driver, request)[0]


def _find_consumable_detail_hrefs(driver: webdriver.Chrome, request: AmbulanceReturnRequest) -> list[str]:
    wait = WebDriverWait(driver, 15)
    wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "a.btn_t02[href^='/ACS/ACS15002?emmTemsisid=']")) > 0)
    candidates = driver.execute_script(
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

    vehicle_scored = [item for item in scored if _text_matches_vehicle(item[2], request.vehicle)]
    vehicle_scored.sort(key=lambda item: item[0], reverse=True)
    if vehicle_scored:
        return _select_consumable_patient_group(vehicle_scored)

    if request.vehicle and scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        matched_hrefs = _find_consumable_hrefs_by_vehicle_code(driver, [href for _, href, _ in scored], request.vehicle)
        matched = [item for item in scored if item[1] in matched_hrefs]
        if matched:
            return _select_consumable_patient_group(matched)
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
    best_score = max(max(item[1] for item in items) for items in groups.values())
    best_groups = [items for items in groups.values() if max(item[1] for item in items) == best_score]
    if len(best_groups) != 1:
        raise RuntimeError("同案耗材存在多組無法唯一辨識的 TEMSISID。")
    selected = best_groups[0]
    suffixes = [item[0] for item in selected]
    if len(suffixes) != len(set(suffixes)):
        raise RuntimeError("同案耗材 TEMSISID 患者序號重複。")
    return [item[2] for item in sorted(selected, key=lambda item: item[0])]


def _consumable_sid_score(case_id: str, sid: str) -> int:
    fragments = _case_id_sid_fragments(case_id)
    if not fragments:
        return 0
    sid_digits = "".join(ch for ch in str(sid or "") if ch.isdigit())
    for fragment in fragments:
        if fragment and fragment in sid_digits:
            return 20 if len(fragment) > 6 else 10
    return 0


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
    candidate_date = _yyyymmdd_from_row_text(text) or _yyyymmdd_from_sid(sid)
    return not candidate_date or candidate_date == case_date


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
    for page_text in (_consumable_detail_control_text(driver), _consumable_detail_body_text(driver)):
        for label in vehicle_ppe_names():
            if _text_matches_vehicle(page_text, label):
                return label
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


def _find_consumable_href_by_vehicle_code(driver: webdriver.Chrome, hrefs: list[str], vehicle: str) -> str:
    matched = _find_consumable_hrefs_by_vehicle_code(driver, hrefs, vehicle)
    return matched[0] if matched else ""


def _find_consumable_hrefs_by_vehicle_code(driver: webdriver.Chrome, hrefs: list[str], vehicle: str) -> list[str]:
    vehicle_tokens = _vehicle_match_tokens(vehicle)
    if not vehicle_tokens:
        return []
    matched: list[str] = []
    for href in hrefs:
        driver.get(urljoin("https://nfaemsap3.nfa.gov.tw", href))
        WebDriverWait(driver, 10).until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(0.5)
        page_text = _consumable_detail_page_text(driver)
        if _text_matches_any_vehicle_token(page_text, vehicle_tokens):
            matched.append(href)
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
) -> str:
    result = driver.execute_script(
        """
        const classType = arguments[0];
        const classIdPrefix = arguments[1];
        const seriesPrefix = arguments[2];
        const itemPrefix = arguments[3];

        function post(endpoint, data) {
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
            if (!response || response.result === 'fail') {
                throw new Error((response && response.msg) || endpoint + ' failed');
            }
            return response.map || {};
        }

        function findKey(map, prefix) {
            const normalizedPrefix = String(prefix).trim() + ' ';
            for (const [key, value] of Object.entries(map)) {
                if (String(value).trim().startsWith(normalizedPrefix)) {
                    return key;
                }
            }
            return '';
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
        return {ok: true, itemId: itemId};
        """,
        class_type,
        class_id_prefix,
        series_prefix,
        item_prefix,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("detail") if isinstance(result, dict) else str(result)
        raise RuntimeError(f"{item_name} 解析耗材 itemId 失敗：{detail}")
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
    time.sleep(1.0)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    confirmation = _consumable_save_confirmation_state(alert_text)
    if confirmation == "failure":
        raise RuntimeError(f"耗材儲存失敗：{alert_text}")
    if confirmation != "success":
        detail = alert_text or "未出現確認訊息"
        raise RuntimeError(f"耗材儲存未取得明確成功回應：{detail}")
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
