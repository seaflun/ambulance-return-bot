from __future__ import annotations

import ctypes
import os
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
        "disinfection": WindowRect(0, bottom_y, half_width, half_height),
        "consumables": WindowRect(right_x, bottom_y, half_width, half_height),
    }
    return mapping.get(tile_name)


def apply_tile(driver, tile_name: str) -> None:
    rect = tile_rect(tile_name)
    if rect is None:
        return
    try:
        driver.set_window_rect(rect.x, rect.y, rect.width, rect.height)
    except Exception:
        try:
            driver.set_window_position(rect.x, rect.y)
            driver.set_window_size(rect.width, rect.height)
        except Exception:
            return


def _screen_size() -> tuple[int, int]:
    width = int(os.getenv("WORKER_TILE_SCREEN_WIDTH", "0") or "0")
    height = int(os.getenv("WORKER_TILE_SCREEN_HEIGHT", "0") or "0")
    if width > 0 and height > 0:
        return width, height
    try:
        return ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080
