from __future__ import annotations

import os
import subprocess
import webbrowser
from pathlib import Path

from .profile_paths import worker_browser_profile_dir


def open_url_in_worker_chrome(url: str) -> str:
    chrome = _chrome_binary()
    if not chrome:
        webbrowser.open_new_tab(url)
        return "opened_default_browser"

    args = [str(chrome)]
    args.append(f"--user-data-dir={worker_browser_profile_dir()}")
    debugger_port = os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223").strip()
    if debugger_port:
        args.append(f"--remote-debugging-port={debugger_port}")
    args.extend(
        [
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ]
    )
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "opened_worker_chrome"


def _chrome_binary() -> Path | None:
    configured = os.getenv("CHROME_BINARY", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.exists():
            return path

    candidates = [
        Path(os.getenv("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.getenv("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None
