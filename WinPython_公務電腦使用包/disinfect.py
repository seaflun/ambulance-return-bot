from __future__ import annotations

import os
import re
from io import BytesIO
from pathlib import Path

import ddddocr
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from ambulance_bot.chrome_startup import add_worker_chrome_options, create_chrome_driver_with_retry
from ambulance_bot.duty_credentials import DutyCredential, load_duty_credential, load_synced_worker_credential
from ambulance_bot.failure_evidence import augment_failure_detail, capture_failure_artifacts
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.profile_paths import runtime_profile_dir
from ambulance_bot.window_layout import apply_tile


URL = "https://emsdt.tyfd.gov.tw/EmmWeb/"
OUTPUT_DIR = Path(os.getenv("CAPTCHA_OUTPUT_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ambulance_return_bot" / "captcha")
IMAGE_PATH = OUTPUT_DIR / "capt.png"
MAX_LOGIN_ATTEMPTS = 3

ocr = ddddocr.DdddOcr(show_ad=False)


def ocr_digits_with_ddddocr(image_path: Path) -> str:
    """Use ddddocr to recognize numeric captcha text."""
    if not image_path.exists():
        raise FileNotFoundError(f"找不到驗證碼圖片：{image_path}")

    result = ocr.classification(image_path.read_bytes())
    return "".join(ch for ch in result if ch.isdigit())


def wait_until_logged_in(driver: webdriver.Chrome, timeout: int = 15) -> None:
    wait = WebDriverWait(driver, timeout)
    wait.until(lambda d: _is_logged_in(d))


def login_and_get_driver(
    request: AmbulanceReturnRequest | None = None,
    profile_name: str = "disinfection_profile",
    debugger_port: int | None = None,
    tile_name: str = "",
    artifacts_dir: Path | None = None,
) -> webdriver.Chrome:
    credentials = _disinfection_credential_attempts(request)
    if not credentials:
        raise RuntimeError("尚未同步 worker 帳號；請先在 worker GUI 接收同步帳密後再執行消毒。")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument("--window-size=1280,900")
    options.add_argument(f"--user-data-dir={_chrome_profile_dir(profile_name)}")
    add_worker_chrome_options(options)
    if debugger_port:
        options.add_argument(f"--remote-debugging-port={debugger_port}")
    options.add_experimental_option("detach", True)

    driver = create_chrome_driver_with_retry(options, "緊急救護消毒")
    page_timeout = int(os.getenv("SELENIUM_PAGE_LOAD_TIMEOUT_SECONDS", "45"))
    driver.set_page_load_timeout(page_timeout)
    driver.set_script_timeout(page_timeout)
    apply_tile(driver, tile_name)
    errors: list[str] = []

    try:
        for credential, source in credentials:
            account = _mask_login_account(credential.user_id)
            for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
                try:
                    _login_once(driver, credential.user_id, credential.password, attempt)
                    wait_until_logged_in(driver)
                    if _is_logged_in(driver):
                        return driver
                    errors.append(f"{source}（{account}）第 {attempt} 次登入後未進入系統")
                except Exception as exc:
                    errors.append(f"{source}（{account}）第 {attempt} 次：{exc}")

                if attempt < MAX_LOGIN_ATTEMPTS:
                    driver.get(URL)

        raise RuntimeError("消毒紀錄登入失敗，已重新整理並重試 3 次：" + "；".join(errors))
    except Exception as exc:
        output_dir = Path(artifacts_dir or os.getenv("ARTIFACTS_DIR", "artifacts")) / "selenium"
        try:
            evidence = capture_failure_artifacts(
                driver,
                output_dir,
                request.task_id if request is not None else "unknown_task",
                "disinfection",
                vehicle=request.vehicle if request is not None else "",
                exception=exc,
                target_url=URL,
            )
            detail = augment_failure_detail(str(exc), evidence)
        except Exception as capture_exc:
            detail = f"{exc} [failure_capture_error:{capture_exc.__class__.__name__}: {capture_exc}]"
        if os.getenv("DISINFECTION_CLOSE_BROWSER_ON_LOGIN_FAILURE", "false").strip().lower() in {"1", "true", "yes", "on"}:
            driver.quit()
        raise RuntimeError(f"消毒紀錄登入失敗：{detail}") from exc


def _chrome_profile_dir(profile_name: str) -> Path:
    return runtime_profile_dir(profile_name)


def _disinfection_credential_attempts(
    request: AmbulanceReturnRequest | None,
) -> list[tuple[DutyCredential, str]]:
    attempts: list[tuple[DutyCredential, str]] = []
    if request is not None:
        _append_disinfection_credentials(attempts, request.driver_duty_login_account_candidates, "任務司機")
        _append_disinfection_credentials(attempts, request.personnel_duty_login_account_candidates, "出勤人員")
    synced = load_synced_worker_credential()
    if synced is not None:
        attempts.append((synced, "同步帳號"))
    return _dedupe_disinfection_credentials(attempts)


def _append_disinfection_credentials(
    attempts: list[tuple[DutyCredential, str]],
    candidates: list[str],
    source: str,
) -> None:
    for candidate in candidates:
        credential = load_duty_credential([candidate], fallback_user_id="", allow_default=False)
        if credential is not None:
            attempts.append((credential, source))


def _dedupe_disinfection_credentials(
    attempts: list[tuple[DutyCredential, str]],
) -> list[tuple[DutyCredential, str]]:
    deduped: list[tuple[DutyCredential, str]] = []
    seen: set[str] = set()
    for credential, source in attempts:
        key = credential.user_id.lower()
        if not credential.user_id or key in seen:
            continue
        seen.add(key)
        deduped.append((credential, source))
    return deduped


def _mask_login_account(account: str) -> str:
    value = str(account or "").strip()
    if re.fullmatch(r"[A-Za-z][0-9]{9}", value):
        return f"{value[:4]}***{value[-3:]}"
    return value


def _login_once(driver: webdriver.Chrome, account: str, password: str, attempt: int) -> None:
    driver.get(URL)
    wait = WebDriverWait(driver, 15)

    username = wait.until(EC.element_to_be_clickable((By.ID, "_txtUsername")))
    username.clear()
    username.send_keys(account)

    password_input = wait.until(EC.element_to_be_clickable((By.ID, "_txtPassword")))
    password_input.clear()
    password_input.send_keys(password)

    captcha = wait.until(EC.visibility_of_element_located((By.ID, "capt")))
    wait.until(
        lambda d: d.execute_script(
            """
            const img = document.getElementById('capt');
            return img && img.complete && img.naturalWidth > 0;
            """
        )
    )

    attempt_image_path = OUTPUT_DIR / f"emm_captcha_attempt_{attempt}.png"
    image = Image.open(BytesIO(captcha.screenshot_as_png)).convert("RGB")
    image.save(attempt_image_path)
    image.save(IMAGE_PATH)

    code = ocr_digits_with_ddddocr(attempt_image_path)
    if not code:
        raise RuntimeError("驗證碼 OCR 未辨識到數字")

    code_input = wait.until(EC.element_to_be_clickable((By.ID, "txtUserCode")))
    code_input.clear()
    code_input.send_keys(code)

    wait.until(EC.element_to_be_clickable((By.ID, "_btnOK"))).click()


def _is_login_page(driver: webdriver.Chrome) -> bool:
    return bool(
        driver.execute_script(
            """
            return !!document.getElementById('_btnOK') || !!document.getElementById('capt');
            """
        )
    )


def _is_logged_in(driver: webdriver.Chrome) -> bool:
    return bool(
        driver.execute_script(
            """
            if (!!document.getElementById('_btnOK') || !!document.getElementById('capt')) {
                return false;
            }
            if (location.href.includes('/EmmWeb/') || location.href.includes('ActionControlServlet')) {
                return true;
            }
            const text = document.body ? document.body.innerText : '';
            return text.includes('報表系統')
                || text.includes('消毒紀錄')
                || text.includes('登出')
                || text.includes('功能選單');
            """
        )
    )
