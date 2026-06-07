from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

import ddddocr
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from ambulance_bot.duty_credentials import load_duty_credential
from ambulance_bot.window_layout import apply_tile


URL = "https://emsdt.tyfd.gov.tw/EmmWeb/"
OUTPUT_DIR = Path(r"C:\Users\User\Pictures")
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
    wait.until(lambda d: not _is_login_page(d))


def login_and_get_driver(
    profile_name: str = "disinfection_profile",
    debugger_port: int | None = None,
    tile_name: str = "",
) -> webdriver.Chrome:
    credential = load_duty_credential()
    if credential is None:
        raise RuntimeError("找不到勤務系統帳密，請先設定或保存登入資料。")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument("--window-size=1280,900")
    options.add_argument(f"--user-data-dir={_chrome_profile_dir(profile_name)}")
    if debugger_port:
        options.add_argument(f"--remote-debugging-port={debugger_port}")
    options.add_experimental_option("detach", True)

    driver = webdriver.Chrome(options=options)
    apply_tile(driver, tile_name)
    errors: list[str] = []

    try:
        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            try:
                _login_once(driver, credential.user_id, credential.password, attempt)
                wait_until_logged_in(driver)
                if not _is_login_page(driver) or _is_logged_in(driver):
                    return driver
                errors.append(f"第 {attempt} 次登入後未進入系統")
            except Exception as exc:
                errors.append(f"第 {attempt} 次：{exc}")

            if attempt < MAX_LOGIN_ATTEMPTS:
                driver.get(URL)

        raise RuntimeError("消毒紀錄登入失敗，已重新整理並重試 3 次：" + "；".join(errors))
    except Exception as exc:
        if os.getenv("DISINFECTION_CLOSE_BROWSER_ON_LOGIN_FAILURE", "false").strip().lower() in {"1", "true", "yes", "on"}:
            driver.quit()
        raise RuntimeError(f"消毒紀錄登入失敗：{exc}") from exc


def _chrome_profile_dir(profile_name: str) -> Path:
    root = Path(os.getenv("SELENIUM_PROFILE_ROOT") or os.getenv("LOCALAPPDATA") or Path.home())
    path = root / "ambulance_return_bot" / profile_name
    path.mkdir(parents=True, exist_ok=True)
    return path


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
