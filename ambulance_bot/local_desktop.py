from __future__ import annotations

import os
import time
import webbrowser
from pathlib import Path

from .adapters import SITE_DEFINITIONS
from .models import AmbulanceReturnRequest


def local_browser_enabled() -> bool:
    raw = os.getenv("OPEN_LOCAL_BROWSER_ON_RUN", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def open_task_on_local_desktop(request: AmbulanceReturnRequest, artifacts_dir: Path) -> Path:
    output_dir = artifacts_dir / "local_desktop"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"{request.task_id}.txt"
    summary_path.write_text(_task_text(request), encoding="utf-8")

    delay = float(os.getenv("BROWSER_OPEN_DELAY_SECONDS", "0.4"))
    for site in SITE_DEFINITIONS:
        webbrowser.open_new_tab(site.url)
        if delay > 0:
            time.sleep(delay)
    return summary_path


def _task_text(request: AmbulanceReturnRequest) -> str:
    lines = [
        "\u6551\u8b77\u56de\u7a0b\u4efb\u52d9",
        "",
        request.summary,
        "",
        "\u56db\u7ad9\u9023\u7d50",
    ]
    lines.extend(f"- {site.name}: {site.url}" for site in SITE_DEFINITIONS)
    lines.append("")
    lines.append("\u7b2c\u4e00\u7248\u53ea\u958b\u9801\u8207\u63d0\u4f9b\u6458\u8981\uff0c\u4e0d\u6703\u81ea\u52d5\u6309\u6700\u5f8c\u9001\u51fa\u3002")
    return "\n".join(lines)
