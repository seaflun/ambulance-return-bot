from __future__ import annotations

import os
import time

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options


STARTUP_ERROR_MARKERS = (
    "chrome failed to start",
    "devtoolsactiveport",
    "from chrome not reachable",
    "chrome not reachable",
    "session not created",
    "no longer running",
)


def add_worker_chrome_options(options: Options) -> Options:
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-popup-blocking")
    return options


def create_chrome_driver_with_retry(options: Options, label: str = "Chrome") -> webdriver.Chrome:
    attempts = max(int(os.getenv("SELENIUM_CHROME_START_ATTEMPTS", os.getenv("SELENIUM_LOCAL_SESSION_ATTEMPTS", "3"))), 1)
    delay_seconds = max(float(os.getenv("SELENIUM_CHROME_RETRY_DELAY_SECONDS", "2")), 0)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            if attempts > 1:
                print(f"[chrome] starting {label} attempt {attempt}/{attempts}", flush=True)
            return webdriver.Chrome(options=options)
        except WebDriverException as exc:
            last_error = exc
            if not _is_chrome_startup_error(exc) or attempt >= attempts:
                break
            print(f"[chrome] {label} start attempt {attempt} failed: {_short_error(exc)}", flush=True)
            time.sleep(delay_seconds)

    raise WebDriverException(f"{label} Chrome 啟動失敗，已重試 {attempts} 次：{_short_error(last_error)}") from last_error


def _is_chrome_startup_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in STARTUP_ERROR_MARKERS)


def _short_error(exc: Exception | None) -> str:
    if exc is None:
        return "unknown error"
    text = str(exc).strip() or exc.__class__.__name__
    return text.splitlines()[0][:240]
