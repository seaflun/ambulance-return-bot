from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from PIL import Image
from selenium.common.exceptions import TimeoutException


BROWSER_FAILURE_MARKER = "[browser_failure:{category}]"
FAILURE_SCREENSHOT_MAX_BYTES = 2 * 1024 * 1024
FAILURE_SCREENSHOT_SETTLE_SECONDS = 0.8
FAILURE_DETAIL_MAX_CHARS = 240
KNOWN_SITE_KEYS = {
    "duty_work_log",
    "vehicle_mileage",
    "fuel_record",
    "consumables",
    "disinfection",
    "volunteer_assist",
}


def safe_evidence_token(value: object, fallback: str = "unknown") -> str:
    token = re.sub(r"[^\w.-]+", "_", str(value or "").strip(), flags=re.UNICODE).strip("._")
    return (token or fallback)[:80]


def _fit_failure_screenshot_size(png_bytes: bytes) -> bytes:
    if len(png_bytes) <= FAILURE_SCREENSHOT_MAX_BYTES:
        return png_bytes

    with Image.open(BytesIO(png_bytes)) as source:
        source.load()
        image = source.copy()

    for _ in range(6):
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        candidate = buffer.getvalue()
        if len(candidate) <= FAILURE_SCREENSHOT_MAX_BYTES:
            return candidate
        ratio = min(0.85, (FAILURE_SCREENSHOT_MAX_BYTES / len(candidate)) ** 0.5 * 0.9)
        width = max(1, int(image.width * ratio))
        height = max(1, int(image.height * ratio))
        if (width, height) == image.size:
            break
        image = image.resize((width, height), Image.Resampling.LANCZOS)

    raise RuntimeError("完整頁面截圖壓縮後仍超過 2 MB 上限")


def _capture_full_page_screenshot(driver: Any, target: Path) -> bool:
    execute_cdp_cmd = getattr(driver, "execute_cdp_cmd", None)
    if not callable(execute_cdp_cmd):
        return False

    result = execute_cdp_cmd(
        "Page.captureScreenshot",
        {"format": "png", "captureBeyondViewport": True},
    )
    encoded = result.get("data") if isinstance(result, dict) else ""
    if not isinstance(encoded, str) or not encoded.strip():
        raise RuntimeError("Chrome 未回傳完整頁面截圖資料")
    png_bytes = base64.b64decode(encoded.strip(), validate=True)
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Chrome 回傳的完整頁面截圖不是有效 PNG")
    target.write_bytes(_fit_failure_screenshot_size(png_bytes))
    return True


def _fit_failure_screenshot_file(target: Path) -> None:
    png_bytes = target.read_bytes()
    if len(png_bytes) <= FAILURE_SCREENSHOT_MAX_BYTES:
        return
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("瀏覽器截圖不是有效 PNG")
    target.write_bytes(_fit_failure_screenshot_size(png_bytes))


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


def browser_network_error_code(text: object) -> str:
    match = re.search(
        r"\bERR_(?:CONNECTION_TIMED_OUT|TIMED_OUT|NETWORK_CHANGED|INTERNET_DISCONNECTED|"
        r"NAME_NOT_RESOLVED|ADDRESS_UNREACHABLE|CONNECTION_RESET|CONNECTION_REFUSED)\b",
        str(text or ""), re.IGNORECASE,
    )
    return match.group(0).upper() if match else ""


class _BrowserErrorCodeParser(HTMLParser):
    _VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.ignored = False
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._VOID_TAGS:
            return
        attributes = dict(attrs)
        if self.depth:
            self.depth += 1
        elif (attributes.get("id") == "error-code"
              or "error-code" in str(attributes.get("class") or "").split()):
            self.depth = 1
        if self.depth and tag in {"script", "style"}:
            self.ignored = True

    def handle_endtag(self, tag):
        if tag in self._VOID_TAGS:
            return
        if tag in {"script", "style"}:
            self.ignored = False
        if self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and not self.ignored:
            self.text.append(data)


def _page_network_error_code(page_source: str) -> str:
    parser = _BrowserErrorCodeParser()
    parser.feed(page_source)
    return browser_network_error_code(" ".join(parser.text))


def _live_network_error_code(driver: Any) -> str:
    response = driver.execute_cdp_cmd("Runtime.evaluate", {
        "expression": """(() => {
            const roots = [document];
            for (let index = 0; index < roots.length && index < 12; index++) {
                const root = roots[index];
                const code = root.querySelector('.error-code, #error-code');
                if (code && code.getClientRects().length) return code.textContent || '';
                for (const element of root.querySelectorAll('*')) {
                    if (element.shadowRoot) roots.push(element.shadowRoot);
                }
            }
            return '';
        })()""",
        "returnByValue": True,
        "timeout": 1000,
    })
    return browser_network_error_code((response.get("result") or {}).get("value"))


def classify_browser_failure(exception: BaseException | None, probe: dict[str, Any]) -> dict[str, str]:
    text = str(exception or "").lower()
    chromedriver_alive = probe.get("chromedriver_alive")
    devtools_reachable = probe.get("devtools_reachable")
    network_error = browser_network_error_code(exception) or browser_network_error_code(probe.get("network_error"))

    if chromedriver_alive is False:
        category = "chromedriver_ended"
    elif network_error:
        category = "network_connection"
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
        (isinstance(exception, TimeoutException) or "timeout" in text or "timed out" in text)
        and devtools_reachable is True
    ):
        category = "web_page_timeout"
    elif "invalid session id" in text or "session deleted" in text:
        category = "chrome_unresponsive"
    else:
        category = ""

    descriptions = {
        "network_connection": (
            "網站連線逾時或中斷" + (f"（{network_error}）" if network_error else "") + "；請確認分隊網路與該站連線。",
            "網路恢復後先重新查詢，確認是否已保存，再單獨重跑；不要直接重複儲存。",
        ),
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


_BROWSER_SESSION_RECOVERY_MARKERS = (
    "invalid session id",
    "session deleted",
    "invalidsessionidexception",
    "[browser_failure:chrome_unresponsive]",
    "[browser_failure:chromedriver_ended]",
)


def browser_session_recovery_attempts() -> int:
    try:
        return max(0, min(int(os.getenv("SELENIUM_SESSION_RECOVERY_ATTEMPTS", "1")), 1))
    except ValueError:
        return 1


def is_browser_session_recovery_error(value: object) -> bool:
    """Return whether a failure should trigger one fresh browser-session retry."""

    text_parts = [
        str(getattr(value, "detail", "") or ""),
        str(getattr(value, "failure_reason", "") or ""),
        str(getattr(value, "exception_type", "") or ""),
    ]
    if isinstance(value, BaseException) or not any(text_parts):
        text_parts.append(str(value or ""))
    text = " ".join(text_parts).lower()
    return any(marker in text for marker in _BROWSER_SESSION_RECOVERY_MARKERS)


def compact_failure_text(value: object, maximum: int = FAILURE_DETAIL_MAX_CHARS) -> str:
    text = " ".join(str(value or "").split())
    stacktrace_at = text.lower().find("stacktrace:")
    if stacktrace_at >= 0:
        text = text[:stacktrace_at].rstrip(" :;-")
    if len(text) <= maximum:
        return text
    return f"{text[:max(maximum - 1, 0)].rstrip()}…"


def _failure_evidence_command_timeout_seconds() -> float:
    try:
        return max(float(os.getenv("SELENIUM_FAILURE_EVIDENCE_COMMAND_TIMEOUT_SECONDS", "4")), 0.0)
    except ValueError:
        return 4.0


@contextmanager
def _bounded_failure_evidence_driver_commands(driver: Any):
    timeout = _failure_evidence_command_timeout_seconds()
    command_executor = getattr(driver, "command_executor", None)
    client_config = getattr(command_executor, "_client_config", None)
    if timeout <= 0 or client_config is None:
        yield
        return

    try:
        previous = client_config.timeout
        current = float(previous) if previous is not None else 0.0
        client_config.timeout = min(current, timeout) if current > 0 else timeout
    except (AttributeError, TypeError, ValueError):
        yield
        return

    try:
        yield
    finally:
        try:
            client_config.timeout = previous
        except (AttributeError, TypeError, ValueError):
            pass


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
        "network_error": browser_network_error_code(exception),
    }

    with _bounded_failure_evidence_driver_commands(driver):
        screenshot_path = output_dir / f"{stem}.png"
        try:
            captured_full_page = False
            if probe.get("chromedriver_alive") is not False and probe.get("devtools_reachable") is not False:
                maximize_window = getattr(driver, "maximize_window", None)
                if callable(maximize_window):
                    try:
                        maximize_window()
                        time.sleep(FAILURE_SCREENSHOT_SETTLE_SECONDS)
                    except Exception:
                        pass
                try:
                    captured_full_page = _capture_full_page_screenshot(driver, screenshot_path)
                except Exception:
                    captured_full_page = False
            if not captured_full_page:
                saved = driver.save_screenshot(str(screenshot_path))
                if saved is False or not screenshot_path.is_file():
                    raise RuntimeError("driver did not create a screenshot")
            _fit_failure_screenshot_file(screenshot_path)
            evidence["screenshot_path"] = str(screenshot_path)
        except Exception as exc:
            evidence["screenshot_error"] = f"{exc.__class__.__name__}: {exc}"

        html_path = output_dir / f"{stem}.html"
        try:
            page_source = str(driver.page_source or "")
            html_path.write_text(page_source, encoding="utf-8")
            evidence["html_path"] = str(html_path)
            page_error = _page_network_error_code(page_source)
            if page_error:
                evidence["network_error"] = page_error
                evidence["network_error_source"] = "page_source"
        except Exception as exc:
            evidence["html_error"] = f"{exc.__class__.__name__}: {exc}"
        if (not evidence["network_error"] and diagnosis["category"] in {"web_renderer_timeout", "web_page_timeout"}
                and probe.get("devtools_reachable") is True):
            try:
                evidence["network_error"] = _live_network_error_code(driver)
                if evidence["network_error"]:
                    evidence["network_error_source"] = "live_error_element"
            except Exception as exc:
                evidence["network_error_probe_error"] = f"{exc.__class__.__name__}: {exc}"
        if evidence["network_error"]:
            evidence.update(classify_browser_failure(exception, evidence))

    metadata_path = output_dir / f"{stem}.json"
    evidence["metadata_path"] = str(metadata_path)
    metadata_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return evidence


def augment_failure_detail(detail: object, evidence: dict[str, Any]) -> str:
    text = compact_failure_text(detail)
    category = str(evidence.get("category") or "").strip()
    reason = str(evidence.get("reason") or "").strip()
    if category and reason:
        return f"{reason} {BROWSER_FAILURE_MARKER.format(category=category)}"
    parts = [text]
    if category:
        parts.append(BROWSER_FAILURE_MARKER.format(category=category))
    if reason:
        parts.append(f"瀏覽器診斷：{reason}")
    return " ".join(part for part in parts if part)


TASK_FAILURE_SITE_KEYS = frozenset({
    "duty_work_log", "vehicle_mileage", "fuel_record", "consumables", "disinfection", "volunteer_assist",
})


def collect_task_failure_evidence(
    artifacts_dir: Path, task_id: str, site_statuses: object, *,
    max_bytes: int = FAILURE_SCREENSHOT_MAX_BYTES, max_count: int = 5,
) -> dict[str, dict]:
    statuses = site_statuses if isinstance(site_statuses, dict) else {}
    failed_site_keys = {
        str(site_key)
        for site_key, site in statuses.items()
        if (
            str(site_key) in TASK_FAILURE_SITE_KEYS
            and isinstance(site, dict)
            and (
                str(site.get("status") or "").endswith("_failed")
                or "error" in str(site.get("status") or "").lower()
            )
        )
    }
    if not failed_site_keys:
        return {}

    root = (artifacts_dir / "selenium").resolve()
    collected = {
        site_key: {"screenshots": [], "screenshot_error": ""}
        for site_key in failed_site_keys
    }
    if not root.exists():
        for value in collected.values():
            value["screenshot_error"] = "公務電腦找不到失敗截圖目錄。"
        return collected

    metadata_paths = sorted(
        root.glob("*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    remaining = max_count
    for metadata_path in metadata_paths[:200]:
        if remaining <= 0:
            break
        try:
            if metadata_path.stat().st_size > 64 * 1024:
                continue
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict) or str(metadata.get("task_id") or "") != task_id:
            continue
        site_key = str(metadata.get("site_key") or "")
        if site_key not in collected:
            continue
        screenshot_error = str(metadata.get("screenshot_error") or "").strip()
        if screenshot_error and not collected[site_key]["screenshot_error"]:
            collected[site_key]["screenshot_error"] = screenshot_error
        raw_path = str(metadata.get("screenshot_path") or "").strip()
        if not raw_path:
            continue
        try:
            screenshot_path = Path(raw_path).resolve()
            if root not in screenshot_path.parents or screenshot_path.suffix.lower() != ".png":
                continue
            image_bytes = screenshot_path.read_bytes()
        except OSError:
            continue
        if (
            not image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            or len(image_bytes) > max_bytes
        ):
            continue
        collected[site_key]["screenshots"].append(
            {
                "filename": screenshot_path.name,
                "content_base64": base64.b64encode(image_bytes).decode("ascii"),
                "vehicle": str(metadata.get("vehicle") or ""),
                "captured_at": str(metadata.get("captured_at") or ""),
            }
        )
        remaining -= 1

    for value in collected.values():
        if not value["screenshots"] and not value["screenshot_error"]:
            value["screenshot_error"] = "公務電腦未能取得失敗畫面，可能是 Chrome 已中斷。"
    return collected



def store_task_failure_evidence(
    artifacts_dir: Path, task_id: str, site_statuses: dict,
    failure_evidence: object, existing_site_statuses: object, *,
    max_bytes: int = FAILURE_SCREENSHOT_MAX_BYTES, max_count: int = 5,
) -> dict:
    statuses = {
        str(site_key): dict(site)
        for site_key, site in site_statuses.items()
        if isinstance(site, dict)
    }
    existing_statuses = existing_site_statuses if isinstance(existing_site_statuses, dict) else {}
    for site_key, existing_site in existing_statuses.items():
        if not isinstance(existing_site, dict):
            continue
        current = statuses.setdefault(str(site_key), dict(existing_site))
        if "failure_screenshots" not in current and isinstance(existing_site.get("failure_screenshots"), list):
            current["failure_screenshots"] = list(existing_site["failure_screenshots"])
        if "failure_screenshot_error" not in current and existing_site.get("failure_screenshot_error"):
            current["failure_screenshot_error"] = str(existing_site["failure_screenshot_error"])

    evidence_by_site = failure_evidence if isinstance(failure_evidence, dict) else {}
    remaining = max_count
    for raw_site_key, raw_evidence in evidence_by_site.items():
        site_key = str(raw_site_key)
        if site_key not in TASK_FAILURE_SITE_KEYS or not isinstance(raw_evidence, dict):
            continue
        site = statuses.setdefault(site_key, {"key": site_key, "status": f"{site_key}_failed"})
        stored_images = [
            dict(item)
            for item in site.get("failure_screenshots", [])
            if isinstance(item, dict) and str(item.get("url") or "")
        ]
        errors: list[str] = []
        capture_error = str(raw_evidence.get("screenshot_error") or "").strip()
        if capture_error:
            errors.append(capture_error)
        screenshots = raw_evidence.get("screenshots") if isinstance(raw_evidence.get("screenshots"), list) else []
        for item in screenshots:
            if remaining <= 0:
                errors.append("失敗截圖超過 5 張上限，後續圖片未保存。")
                break
            if not isinstance(item, dict):
                continue
            try:
                encoded = str(item.get("content_base64") or "")
                max_encoded_length = ((max_bytes + 2) // 3) * 4
                if len(encoded) > max_encoded_length:
                    raise ValueError("PNG Base64 超過 2 MB 上限")
                image_bytes = base64.b64decode(encoded, validate=True)
                if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("檔案不是有效 PNG")
                if len(image_bytes) > max_bytes:
                    raise ValueError("PNG 超過 2 MB 上限")
            except (ValueError, binascii.Error) as exc:
                errors.append(f"截圖驗證失敗：{exc}")
                continue

            root = (artifacts_dir / "public_pc" / "failure_screenshots")
            root.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(image_bytes).hexdigest()[:20]
            safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._")[:80] or "task"
            safe_vehicle = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "_",
                str(item.get("vehicle") or ""),
            ).strip("._")[:40]
            vehicle_suffix = f"-{safe_vehicle}" if safe_vehicle else ""
            filename = f"{safe_task}-{site_key}{vehicle_suffix}-{digest}.png"
            target = root / filename
            if not target.exists():
                target.write_bytes(image_bytes)
            metadata = {
                "url": f"/admin/public-pc/failure-screenshots/{filename}",
                "vehicle": str(item.get("vehicle") or ""),
                "captured_at": str(item.get("captured_at") or ""),
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
            }
            stored_images = [
                existing
                for existing in stored_images
                if str(existing.get("url") or "") != metadata["url"]
            ]
            stored_images.append(metadata)
            remaining -= 1
        site["failure_screenshots"] = stored_images[-max_count:]
        site["failure_screenshot_error"] = "；".join(dict.fromkeys(errors))
    return statuses
