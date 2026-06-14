from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WindowRect:
    x: int
    y: int
    width: int
    height: int


def tile_rect(tile_name: str) -> WindowRect | None:
    if os.getenv("WORKER_TILE_WINDOWS", "true").strip().lower() in {"0", "false", "no", "off"}:
        return None

    screen_width, screen_height = _screen_size()
    usable_height = max(600, screen_height - int(os.getenv("WORKER_TILE_TASKBAR_RESERVE", "80")))
    half_width = max(640, screen_width // 2)
    half_height = max(420, usable_height // 2)
    right_x = max(0, screen_width - half_width)
    bottom_y = max(0, usable_height - half_height)

    mapping = {
        "duty_work_log": WindowRect(0, 0, half_width, half_height),
        "vehicle_mileage": WindowRect(right_x, 0, half_width, half_height),
        "consumables": WindowRect(0, bottom_y, half_width, half_height),
        "disinfection": WindowRect(right_x, bottom_y, half_width, half_height),
    }
    return mapping.get(tile_name)


def apply_tile(driver, tile_name: str) -> None:
    rect = tile_rect(tile_name)
    if rect is None:
        bring_window_to_front(driver)
        return
    try:
        driver.set_window_rect(rect.x, rect.y, rect.width, rect.height)
    except Exception:
        try:
            driver.set_window_position(rect.x, rect.y)
            driver.set_window_size(rect.width, rect.height)
        except Exception:
            return
    bring_window_to_front(driver)


def bring_window_to_front(driver) -> None:
    try:
        driver.execute_script("window.focus();")
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Page.bringToFront", {})
    except Exception:
        pass
    try:
        window = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        window_id = window.get("windowId")
        if window_id:
            driver.execute_cdp_cmd("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})
    except Exception:
        return


def minimize_window(driver) -> None:
    try:
        window = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        window_id = window.get("windowId")
        if window_id:
            driver.execute_cdp_cmd("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "minimized"}})
            return
    except Exception:
        pass
    try:
        driver.minimize_window()
    except Exception:
        return


def maximize_worker_site_windows() -> int:
    if os.name != "nt":
        return 0
    keywords = _worker_site_title_keywords()
    matched = 0

    def callback(hwnd, _):
        nonlocal matched
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title or "chrome" not in _class_name(hwnd).lower():
            return True
        if keywords and not any(keyword in title.lower() for keyword in keywords):
            return True
        ctypes.windll.user32.ShowWindow(hwnd, 3)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        matched += 1
        time.sleep(0.03)
        return True

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(callback)
    try:
        ctypes.windll.user32.EnumWindows(enum_proc, 0)
    except Exception:
        return matched
    return matched


def _screen_size() -> tuple[int, int]:
    width = int(os.getenv("WORKER_TILE_SCREEN_WIDTH", "0") or "0")
    height = int(os.getenv("WORKER_TILE_SCREEN_HEIGHT", "0") or "0")
    if width > 0 and height > 0:
        return width, height
    try:
        return ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080


def _worker_site_title_keywords() -> list[str]:
    default_keywords = "tyfd119,dutymgt,ppe,carrecord,nfaemsap,acs,emsdt,emmweb,消防,勤務,工作紀錄,車輛,里程,一站通,耗材,消毒"
    configured = os.getenv("WORKER_MAXIMIZE_TITLE_KEYWORDS", default_keywords)
    return [item.strip().lower() for item in configured.split(",") if item.strip()]
    raw = os.getenv(
        "WORKER_MAXIMIZE_TITLE_KEYWORDS",
        "消防,勤務,工作紀錄,tyfd119,ppe,車輛,里程,nfaemsap,一站通,耗材,emsdt,emmweb,消毒,emergency,chrome",
    )
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _window_title(hwnd) -> str:
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class_name(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value
