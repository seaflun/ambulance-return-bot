from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen


BROWSER_FAILURE_MARKER = "[browser_failure:{category}]"
KNOWN_SITE_KEYS = {
    "duty_work_log",
    "vehicle_mileage",
    "fuel_record",
    "consumables",
    "disinfection",
}


def safe_evidence_token(value: object, fallback: str = "unknown") -> str:
    token = re.sub(r"[^\w.-]+", "_", str(value or "").strip(), flags=re.UNICODE).strip("._")
    return (token or fallback)[:80]


def probe_browser_runtime(driver: Any) -> dict[str, Any]:
    process = getattr(getattr(driver, "service", None), "process", None)
    process_return_code = None
    chromedriver_alive = None
    if process is not None:
        try:
            process_return_code = process.poll()
            chromedriver_alive = process_return_code is None
        except Exception:
            chromedriver_alive = None

    capabilities = getattr(driver, "capabilities", {}) or {}
    chrome_options = capabilities.get("goog:chromeOptions") or {}
    debugger_address = str(chrome_options.get("debuggerAddress") or "").strip()
    devtools_reachable = None
    devtools_error = ""
    if debugger_address:
        response = None
        try:
            response = urlopen(f"http://{debugger_address}/json/version", timeout=1.0)
            devtools_reachable = True
        except Exception as exc:
            devtools_reachable = False
            devtools_error = f"{exc.__class__.__name__}: {exc}"
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    return {
        "chromedriver_alive": chromedriver_alive,
        "chromedriver_return_code": process_return_code,
        "devtools_reachable": devtools_reachable,
        "devtools_error": devtools_error,
        "debugger_address": debugger_address,
        "chrome_version": str(capabilities.get("browserVersion") or capabilities.get("version") or ""),
    }


def classify_browser_failure(exception: BaseException | None, probe: dict[str, Any]) -> dict[str, str]:
    text = str(exception or "").lower()
    chromedriver_alive = probe.get("chromedriver_alive")
    devtools_reachable = probe.get("devtools_reachable")

    if chromedriver_alive is False:
        category = "chromedriver_ended"
    elif (
        devtools_reachable is False
        or "not connected to devtools" in text
        or "chrome not reachable" in text
        or "tab crashed" in text
        or "disconnected" in text
    ):
        category = "chrome_unresponsive"
    elif "timed out receiving message from renderer" in text and devtools_reachable is True:
        category = "web_renderer_timeout"
    elif (
        ("timeout" in text or "timed out" in text)
        and devtools_reachable is True
    ):
        category = "web_page_timeout"
    elif "invalid session id" in text or "session deleted" in text:
        category = "chrome_unresponsive"
    else:
        category = ""

    descriptions = {
        "web_renderer_timeout": (
            "網頁轉譯程序逾時；Chrome 與 ChromeDriver 仍可連線，較可能是該網頁卡住。",
            "保留截圖，重新整理該站頁面後單獨重跑；若持續發生再重啟 Chrome。",
        ),
        "web_page_timeout": (
            "網頁載入或等待元件逾時；Chrome 與 ChromeDriver 仍可連線。",
            "查看截圖確認頁面停在哪一步，再重新整理並單獨重跑該站。",
        ),
        "chrome_unresponsive": (
            "Google Chrome 無回應、頁籤崩潰，或 DevTools 連線已中斷。",
            "關閉殘留 Chrome／ChromeDriver，重啟 Worker，再單獨重跑該站。",
        ),
        "chromedriver_ended": (
            "ChromeDriver 程序已結束，瀏覽器自動化工作階段無法繼續。",
            "重啟 Worker 以建立新的 Chrome 工作階段，再單獨重跑該站。",
        ),
    }
    reason, next_action = descriptions.get(category, ("", ""))
    return {"category": category, "reason": reason, "next_action": next_action}


def capture_failure_artifacts(
    driver: Any,
    output_dir: Path,
    task_id: object,
    site_key: str,
    *,
    vehicle: object = "",
    exception: BaseException | None = None,
    target_url: str = "",
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now().astimezone()
    site = site_key if site_key in KNOWN_SITE_KEYS else safe_evidence_token(site_key, "unknown_site")
    task_token = safe_evidence_token(task_id, "unknown_task")
    vehicle_token = safe_evidence_token(vehicle, "")
    suffix = f"-{vehicle_token}" if vehicle_token else ""
    timestamp = captured_at.strftime("%Y%m%dT%H%M%S%f")
    stem = f"{task_token}-{site}_error{suffix}-{timestamp}"

    probe = probe_browser_runtime(driver)
    diagnosis = classify_browser_failure(exception, probe)
    evidence: dict[str, Any] = {
        "task_id": str(task_id or ""),
        "site_key": site,
        "vehicle": str(vehicle or ""),
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "target_url": str(target_url or ""),
        "elapsed_seconds": elapsed_seconds,
        "exception_type": exception.__class__.__name__ if exception is not None else "",
        "exception": str(exception or ""),
        **probe,
        **diagnosis,
        "screenshot_path": "",
        "screenshot_error": "",
        "html_path": "",
        "html_error": "",
        "metadata_path": "",
    }

    screenshot_path = output_dir / f"{stem}.png"
    try:
        saved = driver.save_screenshot(str(screenshot_path))
        if saved is False or not screenshot_path.is_file():
            raise RuntimeError("driver did not create a screenshot")
        evidence["screenshot_path"] = str(screenshot_path)
    except Exception as exc:
        evidence["screenshot_error"] = f"{exc.__class__.__name__}: {exc}"

    html_path = output_dir / f"{stem}.html"
    try:
        html_path.write_text(str(driver.page_source or ""), encoding="utf-8")
        evidence["html_path"] = str(html_path)
    except Exception as exc:
        evidence["html_error"] = f"{exc.__class__.__name__}: {exc}"

    metadata_path = output_dir / f"{stem}.json"
    evidence["metadata_path"] = str(metadata_path)
    metadata_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return evidence


def augment_failure_detail(detail: object, evidence: dict[str, Any]) -> str:
    text = str(detail or "").strip()
    category = str(evidence.get("category") or "").strip()
    reason = str(evidence.get("reason") or "").strip()
    parts = [text]
    if category:
        parts.append(BROWSER_FAILURE_MARKER.format(category=category))
    if reason:
        parts.append(f"瀏覽器診斷：{reason}")
    return " ".join(part for part in parts if part)
