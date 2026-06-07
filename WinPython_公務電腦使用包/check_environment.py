# -*- coding: utf-8 -*-
"""Quick environment check for the ambulance return public-duty worker."""

from __future__ import annotations

import importlib.util
import platform
import sys
import tkinter as tk

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


REQUIRED_MODULES = [
    "dotenv",
    "selenium",
    "ddddocr",
    "PIL",
    "pystray",
]


def ok(message: str) -> None:
    print(f"[OK] {message}")


def fail(message: str) -> None:
    print(f"[FAIL] {message}")
    raise SystemExit(1)


def main() -> int:
    if sys.version_info < (3, 11):
        fail(f"Python version is {platform.python_version()}; Python 3.11+ is required.")
    ok(f"Python {platform.python_version()}")

    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        fail(f"Missing Python packages: {', '.join(missing)}. Run SETUP_WINPYTHON.bat.")
    ok("Required Python packages are installed.")

    try:
        root = tk.Tk()
        root.withdraw()
        root.destroy()
    except Exception as exc:
        fail(f"Tkinter GUI is unavailable: {exc}")
    ok("Tkinter GUI is available.")

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--window-size=1280,900")
    try:
        driver = webdriver.Chrome(options=options)
        driver.get("about:blank")
        driver.quit()
    except Exception as exc:
        fail(f"Chrome / ChromeDriver test failed: {exc}")
    ok("Chrome / ChromeDriver can start.")

    ok("Environment check passed. Start the worker with RUN_WORKER_GUI_WINPYTHON.vbs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
