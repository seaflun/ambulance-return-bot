from __future__ import annotations

import os
import subprocess
import webbrowser
from pathlib import Path


def open_url_in_worker_chrome(url: str) -> str:
    chrome = _chrome_binary()
    if not chrome:
        webbrowser.open_new_tab(url)
        return "opened_default_browser"

    args = [str(chrome)]
    profile_dir = os.getenv("CHROME_PROFILE_DIR", "").strip()
    if profile_dir:
        path = Path(profile_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        args.append(f"--user-data-dir={path}")
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
