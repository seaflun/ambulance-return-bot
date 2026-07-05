from __future__ import annotations

import json
import os
import signal
import subprocess
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
        except (WebDriverException, OSError) as exc:
            last_error = exc
            if not _is_chrome_startup_error(exc) or attempt >= attempts:
                break
            print(f"[chrome] {label} start attempt {attempt} failed: {_short_error(exc)}", flush=True)
            cleanup_worker_chrome_residue(options, label)
            time.sleep(delay_seconds)

    raise WebDriverException(f"{label} Chrome 啟動失敗，已重試 {attempts} 次：{_short_error(last_error)}") from last_error


def cleanup_worker_chrome_residue(options: Options, label: str = "Chrome") -> int:
    if os.getenv("SELENIUM_CLEANUP_CHROME_ON_STARTUP_RETRY", "true").strip().lower() in {"0", "false", "no", "off"}:
        return 0

    user_data_dirs = _worker_user_data_dirs(options)
    debugger_ports = _worker_debugger_ports(options)
    processes = _list_chrome_processes()
    target_ids = _target_worker_process_ids(processes, user_data_dirs, debugger_ports)
    killed = 0
    for process_id in target_ids:
        if _terminate_process(process_id):
            killed += 1
    if killed:
        print(f"[chrome] cleaned worker Chrome residue before retry: {label} ({killed} processes)", flush=True)
    return killed


def _is_chrome_startup_error(exc: Exception) -> bool:
    if _is_invalid_argument_oserror(exc):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in STARTUP_ERROR_MARKERS)


def _is_invalid_argument_oserror(exc: Exception) -> bool:
    if not isinstance(exc, OSError):
        return False
    return getattr(exc, "errno", None) == 22 or "invalid argument" in str(exc).lower()


def _short_error(exc: Exception | None) -> str:
    if exc is None:
        return "unknown error"
    text = str(exc).strip() or exc.__class__.__name__
    return text.splitlines()[0][:240]


def _worker_user_data_dirs(options: Options) -> list[str]:
    values: list[str] = []
    args = _chrome_option_arguments(options)
    for index, arg in enumerate(args):
        text = str(arg)
        if text.startswith("--user-data-dir="):
            values.append(text.split("=", 1)[1])
        elif text == "--user-data-dir" and index + 1 < len(args):
            values.append(str(args[index + 1]))
    return [_normalize_match_text(value) for value in values if str(value).strip()]


def _worker_debugger_ports(options: Options) -> set[str]:
    ports: set[str] = set()
    args = _chrome_option_arguments(options)
    for index, arg in enumerate(args):
        text = str(arg)
        if text.startswith("--remote-debugging-port="):
            ports.add(text.split("=", 1)[1].strip())
        elif text == "--remote-debugging-port" and index + 1 < len(args):
            ports.add(str(args[index + 1]).strip())
    return {port for port in ports if port}


def _chrome_option_arguments(options: Options) -> list[str]:
    for attr in ("arguments", "_arguments"):
        value = getattr(options, attr, None)
        if isinstance(value, list):
            return [str(item) for item in value]
    return []


def _target_worker_process_ids(processes: list[dict[str, object]], user_data_dirs: list[str], debugger_ports: set[str]) -> list[int]:
    target_ids: set[int] = set()
    cleanup_chromedriver = os.getenv("SELENIUM_CLEANUP_CHROMEDRIVER_ON_STARTUP_RETRY", "true").strip().lower() not in {"0", "false", "no", "off"}
    for process in processes:
        process_id = _process_id(process)
        if process_id <= 0:
            continue
        name = str(process.get("Name") or "").lower()
        command_line = str(process.get("CommandLine") or "")
        if name == "chromedriver.exe" and cleanup_chromedriver:
            target_ids.add(process_id)
            continue
        if name == "chrome.exe" and _chrome_process_matches(command_line, user_data_dirs, debugger_ports):
            target_ids.add(process_id)

    changed = True
    while changed:
        changed = False
        for process in processes:
            process_id = _process_id(process)
            parent_id = _parent_process_id(process)
            name = str(process.get("Name") or "").lower()
            if process_id > 0 and parent_id in target_ids and name in {"chrome.exe", "chromedriver.exe"} and process_id not in target_ids:
                target_ids.add(process_id)
                changed = True

    parent_by_id = {_process_id(process): _parent_process_id(process) for process in processes}
    return sorted(target_ids, key=lambda process_id: _process_depth(process_id, parent_by_id), reverse=True)


def _chrome_process_matches(command_line: str, user_data_dirs: list[str], debugger_ports: set[str]) -> bool:
    normalized = _normalize_match_text(command_line)
    if any(path and path in normalized for path in user_data_dirs):
        return True
    for port in debugger_ports:
        if f"--remote-debugging-port={port}" in normalized or f"--remote-debugging-port {port}" in normalized:
            return True
    return False


def _list_chrome_processes() -> list[dict[str, object]]:
    command = (
        "$ErrorActionPreference='Stop'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^(chrome|chromedriver)\\.exe$' } | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=creationflags,
        )
    except Exception:
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _terminate_process(process_id: int) -> bool:
    try:
        os.kill(int(process_id), signal.SIGTERM)
        return True
    except OSError:
        return False


def _process_id(process: dict[str, object]) -> int:
    try:
        return int(process.get("ProcessId") or 0)
    except (TypeError, ValueError):
        return 0


def _parent_process_id(process: dict[str, object]) -> int:
    try:
        return int(process.get("ParentProcessId") or 0)
    except (TypeError, ValueError):
        return 0


def _process_depth(process_id: int, parent_by_id: dict[int, int]) -> int:
    depth = 0
    seen: set[int] = set()
    current = process_id
    while current not in seen:
        seen.add(current)
        parent = parent_by_id.get(current, 0)
        if parent not in parent_by_id:
            return depth
        depth += 1
        current = parent
    return depth


def _normalize_match_text(value: str) -> str:
    return os.path.expandvars(str(value)).strip().strip('"').replace("/", "\\").lower()
