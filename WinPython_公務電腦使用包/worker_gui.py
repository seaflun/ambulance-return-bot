from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

import customtkinter as ctk
from dotenv import load_dotenv

import worker
from ambulance_bot.chrome_startup import cleanup_worker_chrome_residue
from ambulance_bot.duty_credentials import (
    DutyCredential,
    legacy_configured_saved_login_path,
    list_saved_duty_automation_credentials,
    load_saved_duty_automation_credential,
    save_duty_automation_credentials,
    saved_login_path,
    set_last_selected_duty_automation_credential,
    stable_synced_account_selection,
)
from ambulance_bot.profile_paths import (
    WORKER_BROWSER_PROFILE_NAME,
    cleanup_stale_runtime_profiles,
    runtime_profile_root,
    worker_browser_profile_dir,
)

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    pystray = None
    Image = None
    ImageDraw = None


load_dotenv()

NAS_LAN_URL = "http://10.30.65.30:8080"
NAS_TAILSCALE_URL = "http://100.114.126.58:8080"
SINGLE_INSTANCE_MUTEX_NAME = "Local\\AmbulanceReturnBotWorkerGui"
_SINGLE_INSTANCE_MUTEX_HANDLE: int | None = None
FALSE_ENV_VALUES = {"0", "false", "no", "off"}
WORKER_CHROME_PROFILE_PREFIXES = (
    WORKER_BROWSER_PROFILE_NAME,
    "chrome_profile",
    "case_lookup_profile",
    "duty_work_log_profile",
    "vehicle_mileage_profile",
    "fuel_record_profile",
    "consumables_profile",
    "disinfection_profile",
    "duty_work_log_profile_",
    "vehicle_mileage_profile_",
    "fuel_record_profile_",
    "consumables_profile_",
    "disinfection_profile_",
)

GUI_THEME = {
    "bg": "#fff7ef",
    "surface": "#ffffff",
    "surface_soft": "#fff1e6",
    "surface_hover": "#ffe2cd",
    "ink": "#10233f",
    "muted": "#667085",
    "line": "#efd8c4",
    "accent": "#f08a4b",
    "accent_active": "#dc6f32",
    "success": "#2f8f6b",
    "success_active": "#247556",
    "status_bg": "#fff1e6",
    "input": "#fffaf5",
    "log_bg": "#10233f",
    "log_fg": "#f8efe7",
}
GUI_FONT_FAMILY = "Microsoft JhengHei UI"


class QueueTextWriter:
    def __init__(self, log_queue: queue.Queue[str]) -> None:
        self.log_queue = log_queue
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += str(text)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                formatted = format_worker_output_line(line)
                if formatted:
                    self.log_queue.put(formatted)
        return len(text)

    def flush(self) -> None:
        line = self._buffer.strip()
        if line:
            formatted = format_worker_output_line(line)
            if formatted:
                self.log_queue.put(formatted)
        self._buffer = ""


def format_worker_output_line(line: str) -> str:
    text = str(line or "").strip()
    if not text:
        return ""

    if text.startswith("[selenium] ") and any(
        marker in text
        for marker in (
            "creating local chrome session attempt",
            "waiting for session lock",
            "acquired session lock",
            "released session lock",
        )
    ):
        return ""
    if text == "[worker] no queued task":
        return ""
    if text.startswith((
        "[profiles] cleaned stale runtime profiles:",
        "[profiles] cleaned startup-failed runtime profiles:",
    )):
        names_text = text.split(":", 1)[1] if ":" in text else ""
        names = [name.strip() for name in names_text.split(",") if name.strip()]
        return f"Chrome 清理｜已清理舊 profile {len(names)} 個"

    if text.startswith("[worker] manual case lookup requested "):
        range_match = re.search(r"range=([^ ]+)", text)
        source_match = re.search(r"source=([^ ]+)", text)
        return (
            f"案件查詢｜{source_match.group(1) if source_match else 'NAS端'}按下查詢｜"
            f"{range_match.group(1) if range_match else '24h'}"
        )

    prefix_replacements = [
        ("[worker] scheduled case lookup range=", "案件查詢｜背景查詢｜"),
        ("[case_lookup] starting duty emergency case lookup range=", "案件查詢｜開始｜"),
    ]
    exact_replacements = {
        "[worker] scheduled case lookup skipped: waiting for valid duty login": "案件查詢｜等待帳號｜暫停",
        "[worker] scheduled case lookup skipped: manual task active": "案件查詢｜手動任務中｜略過背景查詢",
        "[worker] case lookup unchanged; skip posting": "案件查詢｜未變更｜略過上傳",
        "[app] waitress serving": "系統｜本機網頁已就緒",
    }
    for source, target in prefix_replacements:
        if text.startswith(source):
            return text.replace(source, target, 1)
    if text in exact_replacements:
        return exact_replacements[text]

    if text.startswith("[case_lookup] step="):
        step_match = re.search(r"step=([^ ]+)", text)
        count_match = re.search(r"count=(\d+)", text)
        index_match = re.search(r"index=([^ ]+)", text)
        step = step_match.group(1) if step_match else ""
        step_labels = {
            "waiting_lock": "等待查詢程序",
            "chrome_starting": "啟動 Chrome",
            "chrome_ready": "Chrome 已啟動",
            "duty_login": "登入消防勤務",
            "duty_login_ok": "消防勤務已登入",
            "open_query": "開啟案件查詢",
            "read_rows": "讀取案件列表",
            "rows_loaded": "已讀取案件列表",
            "read_details": "讀取案件詳情",
            "read_detail": "讀取單筆案件詳情",
            "details_loaded": "案件詳情讀取完成",
        }
        message = f"案件查詢｜{step_labels.get(step, step or '處理中')}"
        if count_match:
            message += f"｜{count_match.group(1)} 筆"
        if index_match:
            message += f"｜{index_match.group(1)}"
        return message

    if text.startswith("[worker] starting "):
        server = re.search(r"server=([^ ]+)", text)
        worker_id = re.search(r"worker_id=([^ ]+)", text)
        detail_parts = []
        if worker_id:
            detail_parts.append(worker_id.group(1))
        if server:
            detail_parts.append(server.group(1))
        return "系統｜Worker 已啟動" + (f"｜{'，'.join(detail_parts)}" if detail_parts else "")
    if text.startswith((
        "[app] starting ambulance return web app on ",
        "[app] starting SinpoSmart ambulance worker web app on ",
    )):
        return ""
    if text.startswith("[worker] loop error:") and "timed out" in text.lower():
        return "連線｜NAS逾時｜等待下次重試"
    if text.startswith("[worker] loop error:") and ("http 403" in text.lower() or "worker_token" in text.lower()):
        return "連線｜授權失敗｜WORKER_TOKEN 未設定或不一致，請同步 NAS 與公務電腦 .env 後重啟 worker"
    if text.startswith("[worker] loop error:"):
        return text.replace("[worker] loop error:", "錯誤｜Worker｜", 1).strip()
    if text.startswith("[worker] case lookup result "):
        status = re.search(r"status=([^ ]+)", text)
        count = re.search(r"count=(\d+)", text)
        status_text = status.group(1) if status else "未知"
        count_text = count.group(1) if count else "0"
        if status_text == "cases_loaded":
            return f"案件查詢｜完成｜已查到 {count_text} 筆"
        return f"案件查詢｜完成｜{status_text}，{count_text} 筆"
    if text.startswith("[worker] case lookup posted count="):
        count_text = text.replace("[worker] case lookup posted count=", "", 1)
        return f"案件查詢｜已送出｜{count_text} 筆"
    if text.startswith("[case_lookup] query requested "):
        host_match = re.search(r"host=([^ ]+)", text)
        source_match = re.search(r"source=([^ ]+)", text)
        range_match = re.search(r"range=([^ ]+)", text)
        mode_match = re.search(r"mode=([^ ]+)", text)
        host_text = host_match.group(1) if host_match else ""
        source_text = source_match.group(1) if source_match else ""
        if not source_text or "�" in source_text:
            source_text = case_lookup_source_label(host_text)
        return (
            f"案件查詢｜{source_text}按下查詢｜{range_match.group(1) if range_match else '24h'}，"
            f"{mode_match.group(1) if mode_match else 'auto'}"
        )
    return text


def case_lookup_source_label(host: str) -> str:
    value = str(host or "").strip().lower()
    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]
    elif value.count(":") == 1:
        value = value.rsplit(":", 1)[0]
    if value in {"localhost", "127.0.0.1", "::1"}:
        return "本機端"
    return "NAS端"


def format_gui_log_message(message: str, now: str | None = None) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\d{2}:\d{2}:\d{2}\s+", "", text)
    text = format_worker_output_line(text)
    if not text:
        return ""
    if text == "worker 已啟動。":
        return ""

    exact_replacements = {
        "面板已啟動。": "系統｜面板已啟動",
        "目前 worker 已在執行；請關閉本程式再完全重啟。": "系統｜Worker 已在執行",
        "NAS 連線檢查：優先使用內網。": "連線｜開始檢查｜優先使用內網",
        "已縮小到右下角系統匣；右鍵圖示可顯示或結束。": "系統｜縮到系統匣",
        "Chrome 已在背景待命。": "Chrome｜背景待命",
    }
    if text in exact_replacements:
        text = exact_replacements[text]

    prefix_replacements = [
        ("本機網頁啟動中：", "系統｜本機網頁啟動中｜"),
        ("本機網頁已可使用：", "系統｜本機網頁已可使用｜"),
        ("本機網頁啟動失敗：", "錯誤｜本機網頁｜"),
        ("已開啟本機網頁：", "系統｜已開啟本機網頁｜"),
        ("開啟本機網頁失敗：", "錯誤｜本機網頁｜"),
        ("已開啟檢查更新：", "更新｜已開啟｜"),
        ("NAS URL 已切換：", "連線｜手動切換｜"),
        ("NAS 內網連線成功：", "連線｜內網成功｜"),
        ("NAS 內網無法連線，已切換 Tailscale：", "連線｜切換 Tailscale｜"),
        ("NAS 內網與 Tailscale 都無法連線，暫留內網：", "連線｜連線失敗｜"),
        ("已套用同步帳號：", "帳號｜已套用｜"),
        ("已載入值班勤務系統自動化帳密：", "帳號｜已載入｜"),
        ("帳密同步完成：", "帳號｜同步完成｜"),
        ("匯入同步 JSON 讀取失敗：", "錯誤｜帳號同步｜"),
        ("匯入同步失敗：", "錯誤｜帳號同步｜"),
        ("同步帳號選取狀態儲存失敗：", "錯誤｜帳號同步｜"),
        ("刷新任務失敗：", "錯誤｜任務刷新｜"),
        ("已刷新 NAS 任務：", "任務｜已刷新｜"),
        ("找不到任務：", "任務｜找不到｜"),
        ("未選任務，自動使用第一筆：", "任務｜自動選取｜"),
        ("開始執行選取任務：", "工作｜開始｜"),
        ("選取任務已執行完成：", "工作｜完成｜"),
        ("執行選取任務失敗：", "錯誤｜工作｜"),
        ("工作紀錄：", "工作｜"),
        ("工作紀錄結果：", "工作｜結果｜"),
        ("車輛里程：", "里程｜"),
        ("車輛里程結果：", "里程｜結果｜"),
        ("車輛里程已執行完成：", "里程｜完成｜"),
        ("執行車輛里程失敗：", "錯誤｜里程｜"),
        ("消毒紀錄：", "消毒｜"),
        ("執行消毒紀錄失敗：", "錯誤｜消毒｜"),
        ("消毒紀錄失敗狀態回寫失敗：", "錯誤｜消毒回寫｜"),
        ("耗材：", "耗材｜"),
        ("耗材系統已開啟案件內容：", "耗材｜已開啟案件｜"),
        ("執行耗材失敗：", "錯誤｜耗材｜"),
        ("耗材失敗狀態回寫失敗：", "錯誤｜耗材回寫｜"),
        ("五站登打啟動：", "五站｜啟動｜"),
        ("五站登打已啟動：", "五站｜已啟動｜"),
        ("五站登打流程結束：", "五站｜流程結束｜"),
        ("Chrome 已預先啟動：", "Chrome｜已預先啟動｜"),
        ("Chrome 預先啟動失敗：", "錯誤｜Chrome｜"),
    ]
    for source, target in prefix_replacements:
        if text.startswith(source):
            text = text.replace(source, target, 1)
            break

    if "｜" not in text:
        text = f"訊息｜{text}"
    return f"{now or time.strftime('%H:%M:%S')}｜{text}"


def current_package_version(root: Path | None = None) -> str:
    base = root or Path(__file__).resolve().parent
    candidates = [
        base / "VERSION.txt",
        base / "WinPython_公務電腦使用包" / "VERSION.txt",
        base / "UPDATE" / "VERSION.txt",
    ]
    for path in candidates:
        try:
            if path.exists():
                value = path.read_text(encoding="utf-8-sig").strip()
                if value:
                    return value
        except OSError:
            pass
    return "未知"


def _manual_task_for_execution(
    server_url: str,
    task_id: str,
    worker_id: str,
    claimed_task: dict[str, object] | None = None,
) -> dict[str, object] | None:
    if claimed_task is not None:
        claimed_task_id = str(claimed_task.get("task_id") or "").strip()
        if claimed_task_id != str(task_id or "").strip():
            raise ValueError(f"已領取任務與執行目標不一致：{claimed_task_id or '(missing)'} != {task_id}")
        return claimed_task
    return worker.claim_task(server_url, task_id, worker_id)


class WorkerGui(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SinpoSmart - 救護Worker")
        self.geometry("820x820")
        self.minsize(720, 720)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.log_path = Path(os.getenv("ARTIFACTS_DIR", "artifacts")) / "worker_gui.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.worker_thread: threading.Thread | None = None
        self.local_web_process: subprocess.Popen | None = None
        self.worker_started_at = ""

        self.server_url = tk.StringVar(value=initial_worker_server_url(os.getenv("WORKER_SERVER_URL", "")))
        self.local_web_url = tk.StringVar(value=local_web_url())
        self.local_web_status = tk.StringVar(value="服務狀態：尚未檢查")
        self.worker_status = tk.StringVar(value="啟動中")
        self.worker_id = tk.StringVar(value=os.getenv("WORKER_ID", socket.gethostname() or "public-duty-pc"))
        self.duty_account = tk.StringVar(value=os.getenv("DUTY_ACCOUNT", ""))
        self.duty_password = tk.StringVar(value=os.getenv("DUTY_PASSWORD", ""))
        self.credential_choice = tk.StringVar(value="")
        self.package_version = tk.StringVar(value=current_package_version())
        self.connection_summary = tk.StringVar(value=f"目前連線：{self.server_url.get()}")
        self.connection_status = tk.StringVar(value="目前連線：尚未檢查")
        self.saved_credentials: dict[str, DutyCredential] = {}
        self.credential_combo: ctk.CTkComboBox | None = None
        self.duty_saved_login_path = tk.StringVar(value=str(saved_login_path()))
        self.credential_sync_status = tk.StringVar(value="")
        self.task_tree: Any | None = None
        self.tray_icon: Any | None = None
        self.tray_available = bool(pystray and Image and ImageDraw)

        self._configure_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._refresh_credential_choices(apply_first_if_empty=True)
        self.after(500, self.ensure_startup_tray_icon)
        self.after(1200, self._refresh_startup_launcher)
        self.after(250, self._drain_log)
        self.after(1500, self._auto_hide_after_startup)

    def _configure_styles(self) -> None:
        theme = GUI_THEME
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=theme["bg"])

    def _build_ui(self) -> None:
        theme = GUI_THEME
        root = ctk.CTkFrame(self, fg_color=theme["bg"], corner_radius=0)
        root.pack(fill="both", expand=True)
        root.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(root, fg_color=theme["bg"], corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 14))
        header.grid_columnconfigure(0, weight=1)
        header.grid_columnconfigure(1, weight=0)
        title_block = ctk.CTkFrame(header, fg_color=theme["bg"], corner_radius=0)
        title_block.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            title_block,
            text="SinpoSmart - 救護Worker",
            text_color=theme["ink"],
            font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=26, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_block,
            text="公務電腦本機網頁與 NAS 佇列 worker 控制面板",
            text_color=theme["muted"],
            font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=13),
            wraplength=480,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))
        ctk.CTkLabel(
            header,
            textvariable=self.worker_status,
            fg_color=theme["status_bg"],
            text_color=theme["ink"],
            corner_radius=14,
            padx=16,
            pady=8,
            width=176,
            wraplength=150,
            justify="center",
            font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=13, weight="bold"),
        ).grid(row=0, column=1, sticky="ne", padx=(16, 0))

        top_area = ctk.CTkFrame(root, fg_color=theme["bg"], corner_radius=0)
        top_area.grid(row=1, column=0, sticky="ew", padx=24)
        top_area.columnconfigure(0, weight=1, uniform="main_cards")
        top_area.columnconfigure(1, weight=1, uniform="main_cards")
        top_area.rowconfigure(0, weight=0, minsize=172, uniform="main_card_rows")
        top_area.rowconfigure(1, weight=0, minsize=154, uniform="main_card_rows")

        local = self._card(top_area, "本地伺服器")
        local.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        local.columnconfigure(0, weight=1)
        self._hint(local, "", textvariable=self.local_web_status, wraplength=260).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))
        self._entry(local, self.local_web_url).grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 10))
        local_actions = self._action_row(local)
        local_actions.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        self._button(local_actions, "開啟本機網頁", self._start_local_web_app, "primary").grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._button(local_actions, "修復 Chrome", self._repair_worker_chrome, "soft").grid(row=0, column=1, sticky="ew", padx=(6, 0))

        nas = self._card(top_area, "NAS伺服器")
        nas.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._hint(nas, "", textvariable=self.connection_status, wraplength=260).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))
        self._entry(nas, self.server_url).grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 10))
        self._button(nas, "自動測試並切換", self._test_connection, "soft").grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        nas.columnconfigure(0, weight=1)

        credentials = self._card(top_area, "帳號")
        credentials.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(14, 0))
        credentials.columnconfigure(0, weight=1)
        credentials.rowconfigure(3, weight=1)
        self._body_label(credentials, "同步帳號").grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))
        self.credential_combo = ctk.CTkComboBox(
            credentials,
            variable=self.credential_choice,
            values=[],
            state="readonly",
            command=lambda _choice: self._apply_selected_saved_credential(),
            fg_color=theme["input"],
            border_color=theme["line"],
            border_width=1,
            button_color=theme["accent"],
            button_hover_color=theme["accent_active"],
            dropdown_fg_color=theme["surface"],
            dropdown_hover_color=theme["surface_soft"],
            text_color=theme["ink"],
            dropdown_text_color=theme["ink"],
            font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=13),
            height=36,
            corner_radius=10,
        )
        self.credential_combo.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._hint(credentials, "同步帳號為固定8番，請勿勾選其他帳號。", wraplength=330).grid(row=3, column=0, sticky="w", padx=16, pady=(0, 12))

        version_card = self._card(top_area, "版本")
        version_card.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(14, 0))
        version_card.columnconfigure(0, weight=0)
        version_card.columnconfigure(1, weight=1)
        version_card.rowconfigure(2, weight=1)
        self._body_label(version_card, "目前版本").grid(row=1, column=0, sticky="w", padx=(16, 10), pady=(0, 10))
        self._hint(version_card, "", textvariable=self.package_version).grid(row=1, column=1, sticky="e", padx=(0, 16), pady=(0, 10))
        self._button(version_card, "檢查更新", self._check_for_updates, "soft").grid(row=3, column=0, columnspan=2, sticky="sew", padx=16, pady=(0, 16))

        log_frame = self._card(root, "執行紀錄")
        log_frame.grid(row=2, column=0, sticky="nsew", padx=24, pady=(14, 24))
        root.grid_rowconfigure(2, weight=1)
        self.log_text = ctk.CTkTextbox(
            log_frame,
            height=220,
            wrap="word",
            fg_color=theme["log_bg"],
            text_color=theme["log_fg"],
            border_width=0,
            corner_radius=10,
            font=("Consolas", 11),
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)

        self._log("面板已啟動。")

    def _card(self, parent: Any, title: str) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(
            parent,
            fg_color=GUI_THEME["surface"],
            border_color=GUI_THEME["line"],
            border_width=1,
            corner_radius=14,
        )
        ctk.CTkLabel(
            frame,
            text=title,
            text_color=GUI_THEME["ink"],
            font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=15, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 12))
        frame.grid_columnconfigure(0, weight=1)
        return frame

    def _action_row(self, parent: Any) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        frame.columnconfigure(0, weight=1, uniform="actions")
        frame.columnconfigure(1, weight=1, uniform="actions")
        return frame

    def _section_title(self, parent: Any, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent,
            text=text,
            text_color=GUI_THEME["ink"],
            font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=15, weight="bold"),
        )

    def _body_label(self, parent: Any, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent,
            text=text,
            text_color=GUI_THEME["ink"],
            font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=13, weight="bold"),
        )

    def _hint(
        self,
        parent: Any,
        text: str,
        textvariable: tk.StringVar | None = None,
        wraplength: int = 0,
    ) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent,
            text=text,
            textvariable=textvariable,
            text_color=GUI_THEME["muted"],
            font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=12),
            wraplength=wraplength,
            justify="left",
            anchor="w",
        )

    def _entry(self, parent: Any, variable: tk.StringVar) -> ctk.CTkEntry:
        return ctk.CTkEntry(
            parent,
            textvariable=variable,
            state="readonly",
            fg_color=GUI_THEME["input"],
            border_color=GUI_THEME["line"],
            text_color=GUI_THEME["ink"],
            height=36,
            width=1,
            corner_radius=10,
            font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=13),
        )

    def _button(self, parent: Any, text: str, command: Any, variant: str) -> ctk.CTkButton:
        if variant == "soft":
            return ctk.CTkButton(
                parent,
                text=text,
                command=command,
                fg_color=GUI_THEME["surface_soft"],
                hover_color=GUI_THEME["surface_hover"],
                border_color=GUI_THEME["line"],
                border_width=1,
                text_color=GUI_THEME["ink"],
                height=40,
                width=1,
                corner_radius=10,
                font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=13, weight="bold"),
            )
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            fg_color=GUI_THEME["accent"],
            hover_color=GUI_THEME["accent_active"],
            text_color="#ffffff",
            height=40,
            width=1,
            corner_radius=10,
            font=ctk.CTkFont(family=GUI_FONT_FAMILY, size=13, weight="bold"),
        )

    def _set_server(self, url: str) -> None:
        self.server_url.set(url)
        self._apply_server_url()
        self.connection_summary.set(f"目前連線：{url}")
        self.connection_status.set("目前連線：手動指定")
        self._log(f"NAS URL 已切換：{url}")

    def _apply_server_url(self) -> None:
        os.environ["WORKER_SERVER_URL"] = self.server_url.get().strip().rstrip("/")

    def _start_worker_with_default_server(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self._log("目前 worker 已在執行；請關閉本程式再完全重啟。")
            return
        self._log("NAS 連線檢查：優先使用內網。")
        threading.Thread(target=self._start_worker_with_default_server_background, daemon=True).start()

    def _start_worker_with_default_server_background(self) -> None:
        selected_url, mode = choose_worker_server(self._server_reachable)
        self.after(0, lambda: self._apply_server_choice(selected_url, mode, start_worker=True))

    def _apply_server_choice(self, selected_url: str, mode: str, start_worker: bool = False) -> None:
        self.server_url.set(selected_url)
        self._apply_server_url()
        if mode == "lan":
            self.connection_summary.set(f"目前連線：內網 {selected_url}")
            self.connection_status.set("目前連線：內網")
            self._log(f"NAS 內網連線成功：{selected_url}")
        elif mode == "tailscale":
            self.connection_summary.set(f"目前連線：Tailscale {selected_url}")
            self.connection_status.set("目前連線：Tailscale")
            self._log(f"NAS 內網無法連線，已切換 Tailscale：{selected_url}")
        else:
            self.connection_summary.set(f"目前連線：未確認，暫留 {selected_url}")
            self.connection_status.set("目前連線：未確認")
            self._log(f"NAS 內網與 Tailscale 都無法連線，暫留內網：{selected_url}")
        if start_worker:
            self._restart_worker()

    def _server_reachable(self, url: str) -> bool:
        try:
            worker.request_json(f"{url.strip().rstrip('/')}/status")
        except Exception:
            return False
        return True

    def _start_local_web_app(self) -> None:
        status = self._local_web_status()
        if self._local_web_status_matches(status):
            self.local_web_status.set("服務狀態：可使用")
            self._log(f"本機網頁已可使用：{self.local_web_url.get()}")
            self._open_local_web_app()
            return
        if self._local_web_status_same_package(status):
            stopped = terminate_package_local_web_processes()
            self._log(f"本機網頁｜已停止舊版本本機網頁程序：{stopped} 個")
            status = None
        if status is not None:
            app_dir = str(status.get("app_dir") or "").strip() if isinstance(status, dict) else ""
            self.local_web_status.set("服務狀態：8090 已被其他套件占用")
            self._log(f"本機網頁 8090 不是目前套件，請先關閉舊 Worker GUI 或 Python：app_dir={app_dir or '(missing)'}")
            return
        if self.local_web_process is not None and self.local_web_process.poll() is None:
            self.local_web_status.set("服務狀態：啟動中")
            return
        self.local_web_status.set("服務狀態：啟動中")
        env = local_web_process_env()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.local_web_process = subprocess.Popen(
            [local_web_python_executable(), "-u", str(Path(__file__).with_name("app.py"))],
            cwd=Path(__file__).resolve().parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._start_local_web_log_reader(self.local_web_process)
        self._log(f"本機網頁啟動中：{self.local_web_url.get()}")
        if os.getenv("DESKTOP_WEB_OPEN_BROWSER", "true").strip().lower() not in {"0", "false", "no", "off"}:
            self.after(1200, self._open_local_web_app)

    def _start_local_web_log_reader(self, process: subprocess.Popen) -> None:
        threading.Thread(target=self._read_local_web_output, args=(process,), daemon=True).start()

    def _read_local_web_output(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        if stream is None:
            return
        for line in stream:
            text = format_worker_output_line(line)
            if text:
                if text == "系統｜本機網頁已就緒":
                    self.after(0, lambda: self.local_web_status.set("服務狀態：可使用"))
                self.log_queue.put(text)

    def _run_local_web_app(self) -> None:
        try:
            import app as web_app

            web_app.run_web_app(host=local_web_host(), port=local_web_port())
        except Exception as exc:
            self.after(0, lambda: self.local_web_status.set("服務狀態：啟動失敗"))
            self.log_queue.put(f"本機網頁啟動失敗：{exc}")

    def _local_web_reachable(self) -> bool:
        return self._local_web_status_matches(self._local_web_status())

    def _local_web_status(self) -> dict[str, Any] | None:
        try:
            payload = worker.request_json(f"{local_web_base_url()}/status")
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _local_web_status_matches(self, status: dict[str, Any] | None) -> bool:
        app_dir = str(status.get("app_dir") or "").strip() if isinstance(status, dict) else ""
        if not app_dir:
            return False
        running_version = str(status.get("version") or "").strip() if isinstance(status, dict) else ""
        expected_version = current_package_version().strip()
        if expected_version and running_version != expected_version:
            return False
        return self._local_web_status_same_package(status)

    def _local_web_status_same_package(self, status: dict[str, Any] | None) -> bool:
        app_dir = str(status.get("app_dir") or "").strip() if isinstance(status, dict) else ""
        if not app_dir:
            return False
        try:
            return Path(app_dir).resolve() == Path(__file__).resolve().parent
        except OSError:
            return False

    def _open_local_web_app(self) -> None:
        url = self.local_web_url.get().strip() or local_web_url()
        try:
            chrome = chrome_executable_path()
            if chrome:
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.Popen([str(chrome), url], creationflags=creationflags)
                self._log(f"已用 Chrome 開啟本機網頁：{url}")
                return
            webbrowser.open_new_tab(url)
            self._log(f"已用預設瀏覽器開啟本機網頁：{url}")
        except Exception as exc:
            self._log(f"開啟本機網頁失敗：{exc}")

    def _check_for_updates(self) -> None:
        launcher = find_update_launcher()
        if launcher is None:
            messagebox.showerror("檢查更新", "找不到 UPDATE_PACKAGE.bat；請確認公務電腦包是否完整。")
            return
        try:
            os.startfile(str(launcher))
        except OSError as exc:
            messagebox.showerror("檢查更新", f"無法啟動更新程式：{exc}")
            return
        self._log(f"已開啟檢查更新：{launcher}")

    def _repair_worker_chrome(self) -> None:
        self.deiconify()
        self.lift()
        if not messagebox.askyesno(
            "修復 Chrome",
            "將關閉 Worker 專用 Chrome/ChromeDriver，清理 Worker 產生的 runtime profiles，並重啟 Worker GUI。\n\n不會刪除一般 Chrome 資料、書籤或程式內帳密。確定執行？",
            parent=self,
        ):
            return
        self.worker_status.set("修復 Chrome 中")
        threading.Thread(target=self._repair_worker_chrome_background, daemon=True).start()

    def _repair_worker_chrome_background(self) -> None:
        try:
            self.log_queue.put("Chrome 修復｜開始")
            if self._terminate_local_web_process_for_repair():
                self.log_queue.put("Chrome 修復｜已停止本機網頁程序")
            killed = cleanup_worker_chrome_residue(
                worker_chrome_repair_options(),
                "worker repair",
                include_generated_profiles=True,
                profile_root=worker_chrome_profile_root(),
            )
            self.log_queue.put(f"Chrome 修復｜已關閉殘留 Chrome/ChromeDriver：{killed} 個")
            removed_profiles = purge_worker_chrome_profiles()
            if removed_profiles:
                self.log_queue.put(f"Chrome 修復｜已清理舊 Worker profile：{len(removed_profiles)} 個")
            else:
                self.log_queue.put("Chrome 修復｜沒有需要清理的 Worker profile")
            self._log_local_status_for_repair()
            if relaunch_worker_gui():
                self.log_queue.put("Chrome 修復｜即將重啟 Worker GUI")
                self.after(1200, self.quit_from_tray)
            else:
                self.log_queue.put("Chrome 修復｜無法自動重啟 GUI，改為重開本機網頁與 Worker")
                self.after(0, self._start_local_web_app)
                self.after(0, self._restart_worker)
        except Exception as exc:
            self.log_queue.put(f"Chrome 修復失敗：{exc}")
            self.after(0, lambda: self.worker_status.set("Chrome 修復失敗"))

    def _terminate_local_web_process_for_repair(self) -> bool:
        process = self.local_web_process
        if process is None or process.poll() is not None:
            return False
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self.local_web_process = None
        return True

    def _log_local_status_for_repair(self) -> None:
        status = self._local_web_status()
        if self._local_web_status_matches(status):
            self.log_queue.put("Chrome 修復｜本機狀態確認：app_dir 正確")
            return
        if isinstance(status, dict):
            app_dir = str(status.get("app_dir") or "").strip()
            self.log_queue.put(f"Chrome 修復｜本機狀態確認：8090 不是目前使用包 app_dir={app_dir or '(missing)'}")
            return
        self.log_queue.put("Chrome 修復｜本機狀態確認：127.0.0.1:8090 尚未啟動")

    def _restart_worker(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self._log("目前 worker 已在執行；請關閉本程式再完全重啟。")
            return
        self._apply_server_url()
        os.environ["WORKER_RUN_ONCE"] = "false"
        os.environ["WORKER_AUTO_CLAIM_TASKS"] = "true"
        self.worker_started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.worker_status.set(f"執行中，啟動於 {self.worker_started_at}")
        self.worker_thread = threading.Thread(target=self._run_worker, name="ambulance-worker", daemon=True)
        self.worker_thread.start()
        self._log("worker 已啟動。")

    def _run_worker(self) -> None:
        try:
            os.environ["WORKER_RUNTIME_MODE"] = "gui"
            writer = QueueTextWriter(self.log_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                worker.main()
        except Exception as exc:
            self.log_queue.put(f"worker 結束：{exc}")
            self.worker_status.set("已停止")

    def _test_connection(self) -> None:
        threading.Thread(target=self._test_connection_background, daemon=True).start()

    def _test_connection_background(self) -> None:
        selected_url, mode = choose_worker_server(self._server_reachable)
        self.after(0, lambda: self._apply_server_choice(selected_url, mode, start_worker=False))

    def hide_to_tray(self) -> None:
        try:
            has_tray = self.ensure_tray_icon()
        except Exception as exc:
            self._log(f"系統匣啟動失敗，改為最小化：{exc}")
            has_tray = False
        if has_tray:
            self.withdraw()
            self._log("已縮小到右下角系統匣；右鍵圖示可顯示或結束。")
            return
        self.iconify()

    def ensure_startup_tray_icon(self) -> None:
        self.ensure_tray_icon()

    def _auto_hide_after_startup(self) -> None:
        if worker_gui_start_minimized():
            self.hide_to_tray()

    def _refresh_startup_launcher(self) -> None:
        threading.Thread(target=self._refresh_startup_launcher_background, daemon=True).start()

    def _refresh_startup_launcher_background(self) -> None:
        if not startup_launcher_enabled():
            self.log_queue.put("開機啟動已停用。")
            return
        installer = Path(__file__).with_name("install_startup_shortcut.ps1")
        if not installer.exists():
            return
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(installer), "-SkipScheduledTask"],
                cwd=Path(__file__).resolve().parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                timeout=30,
            )
        except Exception as exc:
            self.log_queue.put(f"開機啟動設定失敗：{exc}")
            return
        if result.returncode == 0:
            self.log_queue.put("開機啟動已確認。")
        else:
            detail = (result.stderr or result.stdout or "").strip()
            self.log_queue.put(f"開機啟動設定失敗：{detail or result.returncode}")

    def ensure_tray_icon(self) -> bool:
        if not self.tray_available:
            return False
        if self.tray_icon:
            try:
                if hasattr(self.tray_icon, "visible") and not self.tray_icon.visible:
                    self.tray_icon.visible = True
            except Exception:
                pass
            return True
        image = self.build_tray_image()
        self.tray_icon = pystray.Icon(
            "ambulance_return_worker",
            image,
            "SinpoSmart - 救護Worker",
            pystray.Menu(
                pystray.MenuItem("SinpoSmart - 救護Worker", lambda _icon, _item: None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("顯示控制台", lambda _icon, _item: self.after(0, self.show_from_tray), default=True),
                pystray.MenuItem("縮小到背景", lambda _icon, _item: self.after(0, self.hide_to_tray)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("結束程式", lambda _icon, _item: self.after(0, self.quit_from_tray)),
            ),
        )
        self.tray_icon.run_detached()
        return True

    def build_tray_image(self) -> Any:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 6, 58, 58), radius=16, fill=GUI_THEME["ink"])
        draw.rounded_rectangle((13, 13, 51, 51), radius=12, fill=GUI_THEME["accent"])
        draw.ellipse((43, 7, 59, 23), fill=GUI_THEME["success"])
        draw.text((22, 20), "W", fill="#ffffff")
        return image

    def show_from_tray(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def quit_from_tray(self) -> None:
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.destroy()

    def _refresh_credential_choices(self, apply_first_if_empty: bool = False) -> None:
        credentials = locked_sync_credentials(list_saved_duty_automation_credentials())
        self.saved_credentials = {credential_choice_label(credential): credential for credential in credentials}
        labels = list(self.saved_credentials)
        if self.credential_combo is not None:
            self.credential_combo.configure(values=labels)
        if not labels:
            self.credential_choice.set("")
            self.credential_sync_status.set("目前沒有 8 號同步帳號，請先由值班台登入同步帳號。")
            return

        selected = selected_saved_credential_label(self.saved_credentials)
        self.credential_choice.set(selected)
        if apply_first_if_empty and (not self.duty_account.get().strip() or not self.duty_password.get()):
            self._apply_selected_saved_credential(log=False)
        else:
            self.credential_sync_status.set(f"目前套用：{selected}")

    def _apply_selected_saved_credential(self, log: bool = True) -> None:
        credential = self.saved_credentials.get(self.credential_choice.get())
        if credential is None:
            if log:
                messagebox.showerror("讀取失敗", f"找不到可用同步帳密：{saved_login_path()}")
            return
        self.duty_account.set(credential.user_id)
        self.duty_password.set(credential.password)
        os.environ["DUTY_ACCOUNT"] = credential.user_id
        os.environ["DUTY_PASSWORD"] = credential.password
        try:
            path = persist_selected_saved_credential(credential)
            self.duty_saved_login_path.set(str(path))
        except Exception as exc:
            if log:
                self._log(f"同步帳號選取狀態儲存失敗：{exc}")
        self.credential_sync_status.set(f"目前套用：{credential_choice_label(credential)}")
        if log:
            self._log(f"已套用同步帳號：{credential_choice_label(credential)}")

    def _load_saved_duty_credentials(self) -> None:
        credential = load_saved_duty_automation_credential()
        if credential is None:
            messagebox.showerror("讀取失敗", f"找不到可用帳密：{saved_login_path()}")
            return
        self.duty_account.set(credential.user_id)
        self.duty_password.set(credential.password)
        os.environ["DUTY_ACCOUNT"] = credential.user_id
        os.environ["DUTY_PASSWORD"] = credential.password
        self._log(f"已載入值班勤務系統自動化帳密：{credential.user_id}")

    def _import_credential_sync_file(self) -> None:
        filename = filedialog.askopenfilename(
            title="選擇帳密同步 JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not filename:
            return
        try:
            payload = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            self.credential_sync_status.set(f"匯入同步失敗：{exc}")
            self._log(f"匯入同步 JSON 讀取失敗：{exc}")
            messagebox.showerror("匯入同步失敗", f"同步 JSON 讀取失敗：{exc}")
            return
        if not isinstance(payload, dict):
            self.credential_sync_status.set("匯入同步失敗：同步 JSON 內容不是帳密同步物件。")
            messagebox.showerror("匯入同步失敗", "同步 JSON 內容不是帳密同步物件。")
            return
        try:
            result = save_credential_sync_payload(payload)
        except Exception as exc:
            self.credential_sync_status.set(f"匯入同步失敗：{exc}")
            self._log(f"匯入同步失敗：{exc}")
            messagebox.showerror("匯入同步失敗", f"同步資料儲存失敗：{exc}")
            return
        if result is None:
            self.credential_sync_status.set("匯入同步失敗：同步資料缺少 8 號帳號或密碼。")
            messagebox.showerror("匯入同步失敗", "同步資料缺少 8 號帳號或密碼。")
            return
        user_id, password, path, count = result
        self._apply_credential_sync_result(user_id, password, path, count)
        messagebox.showinfo("匯入同步完成", f"已匯入 {count} 筆帳號，目前套用 {user_id}。")

    def _apply_credential_sync_result(self, user_id: str, password: str, path: Path, count: int) -> None:
        self.duty_account.set(user_id)
        self.duty_password.set(password)
        self.duty_saved_login_path.set(str(path))
        self._refresh_credential_choices()
        legacy_path = legacy_configured_saved_login_path()
        path_note = ""
        if legacy_path and legacy_path != path:
            path_note = f"；已忽略 .env 舊路徑 {legacy_path}"
        self.credential_sync_status.set(f"已匯入帳密同步：{count} 筆；目前套用 {user_id}；儲存於 {path}{path_note}")
        self._log(f"帳密同步完成：{count} 筆；目前套用 {user_id}；儲存於 {path}{path_note}")

    def _refresh_tasks(self) -> None:
        self._apply_server_url()
        threading.Thread(target=self._refresh_tasks_background, daemon=True).start()

    def _refresh_tasks_background(self) -> None:
        try:
            tasks = worker.fetch_recent_tasks(self.server_url.get().strip().rstrip("/"), limit=20)
        except Exception as exc:
            self.log_queue.put(f"刷新任務失敗：{exc}")
            return
        self.after(0, lambda: self._set_task_rows(tasks))
        self.log_queue.put(f"已刷新 NAS 任務：{len(tasks)} 筆")

    def _set_task_rows(self, tasks: list[dict[str, object]]) -> None:
        if self.task_tree is None:
            return
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        for payload in tasks:
            task_id = task_row_id(payload)
            if task_id:
                self.task_tree.insert("", "end", iid=task_id, text=task_id)
        first = self.task_tree.get_children()
        if first:
            self.task_tree.selection_set(first[0])
            self.task_tree.focus(first[0])

    def _run_selected_task(self) -> None:
        task_id = self._selected_task_id()
        if not task_id:
            return
        self._apply_server_url()
        self.worker_status.set(f"手動執行工作紀錄：{task_id}")
        self._log(f"已收到工作紀錄指令，開始取任務：{task_id}")
        threading.Thread(target=self._run_selected_task_background, args=(task_id,), daemon=True).start()

    def _run_selected_vehicle_mileage(self) -> None:
        task_id = self._selected_task_id()
        if not task_id:
            return
        self._apply_server_url()
        self.worker_status.set(f"手動執行車輛里程：{task_id}")
        self._log(f"已收到車輛里程指令，開始取任務：{task_id}")
        threading.Thread(target=self._run_selected_vehicle_mileage_background, args=(task_id,), daemon=True).start()

    def _run_selected_disinfection(self) -> None:
        task_id = self._selected_task_id()
        if not task_id:
            return
        self._apply_server_url()
        self.worker_status.set(f"手動執行消毒紀錄：{task_id}")
        self._log(f"已收到消毒紀錄指令，開始取任務：{task_id}")
        threading.Thread(target=self._run_selected_disinfection_background, args=(task_id,), daemon=True).start()

    def _run_selected_consumables(self) -> None:
        task_id = self._selected_task_id()
        if not task_id:
            return
        self._apply_server_url()
        self.worker_status.set(f"正在執行耗材：{task_id}")
        self._log(f"開始執行耗材：{task_id}")
        threading.Thread(target=self._run_selected_consumables_background, args=(task_id,), daemon=True).start()

    def _run_selected_all_sites(self) -> None:
        task_id = self._selected_task_id()
        if not task_id:
            return
        self._apply_server_url()
        self.worker_status.set(f"五站登打：{task_id}")
        self._log(f"五站登打啟動：{task_id}")
        threading.Thread(target=self._run_selected_all_sites_background, args=(task_id,), daemon=True).start()

    def _selected_task_id(self) -> str:
        if self.task_tree is None:
            return ""
        selected = self.task_tree.selection()
        if not selected:
            first = self.task_tree.get_children()
            if first:
                self.task_tree.selection_set(first[0])
                self.task_tree.focus(first[0])
                self._log(f"未選任務，自動使用第一筆：{first[0]}")
                return str(first[0])
            messagebox.showerror("沒有任務", "任務清單是空的，請先按「刷新任務」。")
            return ""
        return str(selected[0])

    def _run_selected_site_background_common(
        self,
        site_key: str,
        task_id: str,
        *,
        profile_name: str,
        debugger_port: int | None,
        use_session_lock: bool,
        tile_name: str,
        force_new_driver: bool,
        manage_manual_lock: bool,
        update_overall: bool | None,
        claimed_task: dict[str, object] | None,
        cancellation_event: threading.Event | None,
    ):
        server_url = self.server_url.get().strip().rstrip("/")
        worker_id = self.worker_id.get().strip() or socket.gethostname() or "public-duty-pc"
        artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
        effective_update_overall = manage_manual_lock if update_overall is None else update_overall
        execution_event = cancellation_event
        stop_heartbeat = lambda: None
        if manage_manual_lock:
            execution_event = worker.begin_manual_task_execution(task_id, artifacts_dir)
            if execution_event is None:
                self.log_queue.put("目前已有其他登打任務執行中，已略過重複啟動。")
                return None
        try:
            self.log_queue.put(f"{site_key}：向 NAS 領取任務...")
            task = _manual_task_for_execution(server_url, task_id, worker_id, claimed_task)
            if not task:
                self.log_queue.put(f"找不到或無法領取任務：{task_id}")
                return None
            if manage_manual_lock:
                stop_heartbeat = worker._start_worker_claim_heartbeat(server_url, task_id, worker_id)
            cancel_check = (
                (lambda: worker._raise_if_task_cancelled(task_id, execution_event))
                if execution_event is not None
                else None
            )
            common_kwargs = {
                "profile_name": profile_name,
                "debugger_port": debugger_port,
                "tile_name": tile_name,
                "update_overall": effective_update_overall,
                "cancel_check": cancel_check,
            }
            if site_key == "duty_work_log":
                result = worker.run_task(
                    server_url,
                    worker_id,
                    task,
                    artifacts_dir,
                    use_session_lock=use_session_lock,
                    force_new_driver=force_new_driver,
                    **common_kwargs,
                )
            elif site_key == "vehicle_mileage":
                result = worker.run_vehicle_task(
                    server_url,
                    worker_id,
                    task,
                    artifacts_dir,
                    use_session_lock=use_session_lock,
                    force_new_driver=force_new_driver,
                    **common_kwargs,
                )
            elif site_key == "fuel_record":
                result = worker.run_fuel_worker_task(
                    server_url,
                    worker_id,
                    task,
                    artifacts_dir,
                    use_session_lock=use_session_lock,
                    force_new_driver=force_new_driver,
                    **common_kwargs,
                )
            elif site_key == "consumables":
                result = worker.run_consumables_worker_task(
                    server_url,
                    worker_id,
                    task,
                    artifacts_dir,
                    **common_kwargs,
                )
            elif site_key == "disinfection":
                result = worker.run_disinfection_worker_task(
                    server_url,
                    worker_id,
                    task,
                    artifacts_dir,
                    use_session_lock=use_session_lock,
                    force_new_driver=force_new_driver,
                    **common_kwargs,
                )
            else:
                raise KeyError(site_key)
            if site_key == "disinfection":
                if result is not None:
                    self.log_queue.put(
                        f"消毒紀錄完成：{getattr(result, 'status', '')}；{getattr(result, 'detail', '')}"
                    )
                else:
                    self.log_queue.put(f"消毒紀錄沒有回傳結果：{task_id}")
            if result is not None:
                self.log_queue.put(
                    f"{site_key} 結果：{getattr(result, 'status', '')}；{getattr(result, 'detail', '')}"
                )
            self._refresh_tasks()
            return result
        except worker.TaskCancellationError as exc:
            self.log_queue.put(f"任務已中止或 claim 已失效：{task_id} {exc}")
            if not manage_manual_lock:
                raise
            return None
        except Exception as exc:
            self.log_queue.put(f"執行 {site_key} 失敗：{task_id} {exc}")
            if not manage_manual_lock:
                raise
            return None
        finally:
            try:
                stop_heartbeat()
            finally:
                if manage_manual_lock and execution_event is not None:
                    worker.end_manual_task_execution(task_id, execution_event, artifacts_dir)

    def _run_selected_task_background(
        self,
        task_id: str,
        profile_name: str = "duty_work_log_profile",
        debugger_port: int | None = None,
        use_session_lock: bool = True,
        tile_name: str = "",
        force_new_driver: bool = False,
        manage_manual_lock: bool = True,
        update_overall: bool | None = None,
        claimed_task: dict[str, object] | None = None,
        cancellation_event: threading.Event | None = None,
    ):
        return WorkerGui._run_selected_site_background_common(
            self,
            "duty_work_log",
            task_id,
            profile_name=profile_name,
            debugger_port=debugger_port,
            use_session_lock=use_session_lock,
            tile_name=tile_name,
            force_new_driver=force_new_driver,
            manage_manual_lock=manage_manual_lock,
            update_overall=update_overall,
            claimed_task=claimed_task,
            cancellation_event=cancellation_event,
        )

    def _run_selected_vehicle_mileage_background(
        self,
        task_id: str,
        profile_name: str = "vehicle_mileage_profile",
        debugger_port: int | None = None,
        use_session_lock: bool = True,
        tile_name: str = "",
        force_new_driver: bool = False,
        manage_manual_lock: bool = True,
        update_overall: bool | None = None,
        claimed_task: dict[str, object] | None = None,
        cancellation_event: threading.Event | None = None,
    ):
        return WorkerGui._run_selected_site_background_common(
            self,
            "vehicle_mileage",
            task_id,
            profile_name=profile_name,
            debugger_port=debugger_port,
            use_session_lock=use_session_lock,
            tile_name=tile_name,
            force_new_driver=force_new_driver,
            manage_manual_lock=manage_manual_lock,
            update_overall=update_overall,
            claimed_task=claimed_task,
            cancellation_event=cancellation_event,
        )

    def _run_selected_disinfection_background(
        self,
        task_id: str,
        profile_name: str = "disinfection_profile",
        debugger_port: int | None = None,
        use_session_lock: bool = True,
        tile_name: str = "",
        force_new_driver: bool = False,
        manage_manual_lock: bool = True,
        update_overall: bool | None = None,
        claimed_task: dict[str, object] | None = None,
        cancellation_event: threading.Event | None = None,
    ):
        return WorkerGui._run_selected_site_background_common(
            self,
            "disinfection",
            task_id,
            profile_name=profile_name,
            debugger_port=debugger_port,
            use_session_lock=use_session_lock,
            tile_name=tile_name,
            force_new_driver=force_new_driver,
            manage_manual_lock=manage_manual_lock,
            update_overall=update_overall,
            claimed_task=claimed_task,
            cancellation_event=cancellation_event,
        )

    def _run_selected_fuel_record_background(
        self,
        task_id: str,
        profile_name: str = "fuel_record_profile",
        debugger_port: int | None = None,
        use_session_lock: bool = True,
        tile_name: str = "",
        force_new_driver: bool = False,
        manage_manual_lock: bool = True,
        update_overall: bool | None = None,
        claimed_task: dict[str, object] | None = None,
        cancellation_event: threading.Event | None = None,
    ):
        return WorkerGui._run_selected_site_background_common(
            self,
            "fuel_record",
            task_id,
            profile_name=profile_name,
            debugger_port=debugger_port,
            use_session_lock=use_session_lock,
            tile_name=tile_name,
            force_new_driver=force_new_driver,
            manage_manual_lock=manage_manual_lock,
            update_overall=update_overall,
            claimed_task=claimed_task,
            cancellation_event=cancellation_event,
        )

    def _run_selected_consumables_background(
        self,
        task_id: str,
        profile_name: str = "consumables_profile",
        debugger_port: int | None = None,
        use_session_lock: bool = True,
        tile_name: str = "",
        force_new_driver: bool = False,
        manage_manual_lock: bool = True,
        update_overall: bool = True,
        claimed_task: dict[str, object] | None = None,
        cancellation_event: threading.Event | None = None,
    ):
        return WorkerGui._run_selected_site_background_common(
            self,
            "consumables",
            task_id,
            profile_name=profile_name,
            debugger_port=debugger_port,
            use_session_lock=use_session_lock,
            tile_name=tile_name,
            force_new_driver=force_new_driver,
            manage_manual_lock=manage_manual_lock,
            update_overall=update_overall,
            claimed_task=claimed_task,
            cancellation_event=cancellation_event,
        )

    def _run_selected_all_sites_with_lease(self, task_id: str) -> None:
        profile_suffix = task_id.replace("-", "_")
        all_runners = [
            ("工作紀錄", "duty_work_log", self._run_selected_task_background, f"duty_work_log_profile_{profile_suffix}", None, "duty_work_log"),
            ("車輛里程", "vehicle_mileage", self._run_selected_vehicle_mileage_background, f"vehicle_mileage_profile_{profile_suffix}", None, "vehicle_mileage"),
            ("加油紀錄", "fuel_record", self._run_selected_fuel_record_background, f"fuel_record_profile_{profile_suffix}", None, "fuel_record"),
            ("耗材", "consumables", self._run_selected_consumables_background, f"consumables_profile_{profile_suffix}", None, "consumables"),
            ("消毒紀錄", "disinfection", self._run_selected_disinfection_background, f"disinfection_profile_{profile_suffix}", None, "disinfection"),
        ]
        server_url = self.server_url.get().strip().rstrip("/")
        worker_id = self.worker_id.get().strip() or socket.gethostname() or "public-duty-pc"
        artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
        execution_event = worker.begin_manual_task_execution(task_id, artifacts_dir)
        if execution_event is None:
            self.log_queue.put("目前已有其他登打任務執行中，已略過重複啟動。")
            return
        stop_heartbeat = lambda: None
        try:
            task = _manual_task_for_execution(server_url, task_id, worker_id)
            if not task:
                self.log_queue.put(f"找不到或無法領取任務：{task_id}")
                return
            stop_heartbeat = worker._start_worker_claim_heartbeat(server_url, task_id, worker_id)
            worker._raise_if_task_cancelled(task_id, execution_event)
            request = worker.AmbulanceReturnRequest.from_dict(task)
            runners = [runner for runner in all_runners if runner[1] != "fuel_record" or (request and request.has_fuel_record())]
            site_count_label = "五站" if request and request.has_fuel_record() else "四站"
            worker.post_status(server_url, task_id, "desktop_fast_running", f"本機快速執行已啟動：{site_count_label}登打。")
            blocked_site = ""
            for name, site_key, target, profile_name, debugger_port, tile_name in runners:
                worker._raise_if_task_cancelled(task_id, execution_event)
                payload = worker.fetch_task_payload(server_url, task_id)
                if not payload:
                    blocked_site = name
                    break
                worker._assert_task_payload_claim_current(task_id, payload)
                site_statuses = payload.get("site_statuses") if isinstance(payload.get("site_statuses"), dict) else {}
                current_status = str((site_statuses.get(site_key) or {}).get("status") or "")
                if _gui_site_is_complete(current_status):
                    self.log_queue.put(f"{site_count_label}登打略過：{name} 已完成")
                    continue
                self.log_queue.put(f"{site_count_label}登打已啟動：{name}")
                result = target(
                    task_id,
                    profile_name=profile_name,
                    debugger_port=debugger_port,
                    use_session_lock=False,
                    tile_name=tile_name,
                    force_new_driver=True,
                    manage_manual_lock=False,
                    update_overall=False,
                    claimed_task=task,
                    cancellation_event=execution_event,
                )
                if result is None:
                    blocked_site = name
                    break
                worker._raise_if_task_cancelled(task_id, execution_event)
                payload = worker.fetch_task_payload(server_url, task_id)
                if not payload:
                    blocked_site = name
                    break
                site_statuses = payload.get("site_statuses") if isinstance(payload.get("site_statuses"), dict) else {}
                current_status = str((site_statuses.get(site_key) or {}).get("status") or "")
                if _gui_site_blocks_next(current_status):
                    blocked_site = name
                    break
            if blocked_site:
                worker.post_status(server_url, task_id, "desktop_fast_completed_with_errors", f"{blocked_site} 未完成，已停止後續站別。")
            else:
                worker.post_status(server_url, task_id, "desktop_fast_completed", f"{site_count_label}登打完成。")
            self.log_queue.put(f"{site_count_label}登打流程結束：{task_id}")
            self._refresh_tasks()
        except worker.TaskCancellationError as exc:
            self.log_queue.put(f"任務已中止或 claim 已失效：{task_id} {exc}")
        except Exception as exc:
            self.log_queue.put(f"多站登打失敗：{task_id} {exc}")
            try:
                worker.post_status(
                    server_url,
                    task_id,
                    "desktop_fast_completed_with_errors",
                    f"多站登打未完成：{exc}",
                )
            except Exception:
                pass
        finally:
            try:
                stop_heartbeat()
            finally:
                worker.end_manual_task_execution(task_id, execution_event, artifacts_dir)

    def _run_selected_all_sites_background(self, task_id: str) -> None:
        return WorkerGui._run_selected_all_sites_with_lease(self, task_id)

    def _warm_worker_chrome(self) -> None:
        if os.getenv("WORKER_WARM_CHROME_ON_START", "true").strip().lower() in FALSE_ENV_VALUES:
            return
        threading.Thread(target=self._warm_worker_chrome_background, daemon=True).start()

    def _warm_worker_chrome_background(self) -> None:
        try:
            if _worker_chrome_is_running():
                self.log_queue.put("Chrome 已在背景待命。")
                return
            self.log_queue.put("Chrome 預先啟動已略過。")
        except Exception as exc:
            self.log_queue.put(f"Chrome 預先啟動失敗：{exc}")

    def _log(self, message: str) -> None:
        self.log_queue.put(message)

    def _drain_log(self) -> None:
        while True:
            try:
                raw_message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            message = format_gui_log_message(raw_message)
            if not message:
                continue
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            try:
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(message + "\n")
            except Exception:
                pass
        self.after(250, self._drain_log)


def task_row_id(payload: dict[str, object]) -> str:
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    task_dict = task if isinstance(task, dict) else {}
    return str(task_dict.get("task_id") or "")


def task_row_values(payload: dict[str, object]) -> tuple[str, tuple[str, str, str, str]]:
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    task_dict = task if isinstance(task, dict) else {}
    task_id = str(task_dict.get("task_id") or "")
    vehicle = str(task_dict.get("vehicle") or "")
    driver = str(task_dict.get("driver") or "")
    case_time = str(task_dict.get("case_time") or "")
    return_time = str(task_dict.get("return_time") or "")
    address = str(task_dict.get("case_address") or "")
    time_text = f"{case_time}/{return_time}".strip("/")
    return task_id, (vehicle, driver, time_text, address)


def _gui_site_is_complete(status: str) -> bool:
    value = str(status or "")
    return value == "completed_by_user" or value.endswith("_saved")


def _gui_site_blocks_next(status: str) -> bool:
    value = str(status or "")
    if "failed" in value or "error" in value:
        return True
    if value.startswith("needs_") or "login" in value or "waiting_confirmation" in value:
        return True
    return False


def initial_worker_server_url(configured: str) -> str:
    configured_url = str(configured or "").strip().rstrip("/")
    if configured_url and configured_url not in {NAS_LAN_URL, NAS_TAILSCALE_URL}:
        return configured_url
    return NAS_LAN_URL


def choose_worker_server(probe) -> tuple[str, str]:
    if probe(NAS_LAN_URL):
        return NAS_LAN_URL, "lan"
    if probe(NAS_TAILSCALE_URL):
        return NAS_TAILSCALE_URL, "tailscale"
    return NAS_LAN_URL, "offline"


def credential_choice_label(credential: DutyCredential) -> str:
    actor = f"{credential.actor_no}番" if credential.actor_no else "未填番號"
    account = credential.user_id or "未填帳號"
    display_name = _name_from_display_name(credential.display_name, account=credential.user_id, actor_no=credential.actor_no)
    name = credential.name or display_name or "未填姓名"
    return f"{actor} {name} - {account}"


def is_locked_sync_credential(credential: DutyCredential) -> bool:
    return credential.actor_no == "8" or credential.user_id.lower() == "tyfd01510"


def locked_sync_credentials(credentials: list[DutyCredential]) -> list[DutyCredential]:
    return [credential for credential in credentials if is_locked_sync_credential(credential)]


def _name_from_display_name(display_name: str, account: str = "", actor_no: str = "") -> str:
    text = str(display_name or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\s*\d+\s*番\s*", "", text).strip()
    account_text = str(account or "").strip()
    actor_text = str(actor_no or "").strip()
    if account_text and text.lower() == account_text.lower():
        return ""
    if actor_text and text == actor_text:
        return ""
    return text


def selected_saved_credential_label(saved_credentials: dict[str, DutyCredential]) -> str:
    return next(iter(saved_credentials), "")


def persist_selected_saved_credential(credential: DutyCredential) -> Path:
    if not is_locked_sync_credential(credential):
        raise ValueError("同步帳號已鎖定 8 號")
    selected = credential.user_id or credential.actor_no or credential.id_number
    return set_last_selected_duty_automation_credential(selected)


def credential_sync_accounts_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    accounts_payload = payload.get("accounts")
    if isinstance(accounts_payload, list):
        accounts = [account for account in accounts_payload if isinstance(account, dict)]
    else:
        accounts = [payload]
    return [
        account
        for account in accounts
        if str(account.get("user_id") or "").strip() and str(account.get("password") or "")
    ]


def select_credential_sync_account(accounts: list[dict[str, object]], payload: dict[str, object]) -> dict[str, object] | None:
    if not accounts:
        return None
    selected_user_id = str(payload.get("user_id") or "").strip()
    selected_actor_no = str(payload.get("actor_no") or "").strip()
    for account in accounts:
        user_id = str(account.get("user_id") or "").strip()
        actor_no = str(account.get("actor_no") or "").strip()
        if selected_user_id and user_id == selected_user_id:
            return account
        if selected_actor_no and actor_no == selected_actor_no:
            return account
    return accounts[0]


def save_credential_sync_payload(payload: dict[str, object]) -> tuple[str, str, Path, int] | None:
    accounts = credential_sync_accounts_from_payload(payload)
    selected = select_credential_sync_account(accounts, payload)
    if selected is None:
        return None
    last_selected = stable_synced_account_selection(accounts)
    if not last_selected:
        return None
    last_synced = str(selected.get("user_id") or selected.get("actor_no") or "").strip()
    path = save_duty_automation_credentials(accounts, last_selected=last_selected, last_synced=last_synced)
    synced = load_saved_duty_automation_credential(path)
    if synced is not None:
        user_id = synced.user_id
        password = synced.password
    else:
        stable_selected = select_credential_sync_account(accounts, {"user_id": last_selected, "actor_no": last_selected}) or selected
        user_id = str(stable_selected.get("user_id") or "").strip()
        password = str(stable_selected.get("password") or "")
    if not user_id or not password:
        return None
    os.environ["DUTY_ACCOUNT"] = user_id
    os.environ["DUTY_PASSWORD"] = password
    return user_id, password, path, len(accounts)


def _worker_chrome_is_running() -> bool:
    try:
        import urllib.request

        port = os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223").strip()
        if not port:
            return False
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


class _ChromeRepairOptions:
    def __init__(self, arguments: list[str]) -> None:
        self.arguments = arguments


def worker_chrome_repair_options() -> _ChromeRepairOptions:
    arguments = [f"--user-data-dir={worker_browser_profile_dir()}"]
    debugger_port = os.getenv("WORKER_CHROME_DEBUGGER_PORT", "9223").strip()
    if debugger_port:
        arguments.append(f"--remote-debugging-port={debugger_port}")
    return _ChromeRepairOptions(arguments)


def worker_chrome_profile_root() -> Path:
    return runtime_profile_root()


def worker_chrome_profile_dirs(root: Path | None = None) -> list[Path]:
    profile_root = root or worker_chrome_profile_root()
    if not profile_root.exists():
        return []
    paths = []
    for path in profile_root.iterdir():
        if path.is_dir() and ".chrome_repair_" not in path.name and path.name.startswith(WORKER_CHROME_PROFILE_PREFIXES):
            paths.append(path)
    return sorted(paths)


def backup_worker_chrome_profiles(root: Path | None = None, timestamp: str | None = None) -> list[tuple[Path, Path]]:
    stamp = timestamp or time.strftime("%Y%m%d_%H%M%S")
    backups: list[tuple[Path, Path]] = []
    for path in worker_chrome_profile_dirs(root):
        backup = unique_backup_path(path.with_name(f"{path.name}.chrome_repair_{stamp}"))
        path.rename(backup)
        backups.append((path, backup))
    return backups


def purge_worker_chrome_profiles(root: Path | None = None) -> list[Path]:
    profile_root = root or worker_chrome_profile_root()
    return cleanup_stale_runtime_profiles(profile_root, max_age_hours=0)


def unique_backup_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}_{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Cannot find available backup path for {path}")


def relaunch_worker_gui(delay_seconds: int = 2) -> bool:
    package_dir = Path(__file__).resolve().parent
    launcher = package_dir / "RUN_WORKER_GUI_WINPYTHON.vbs"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if os.name == "nt" and launcher.exists():
            if delay_seconds > 0:
                time.sleep(max(delay_seconds, 1))
            wscript = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "wscript.exe"
            executable = str(wscript) if wscript.exists() else "wscript.exe"
            subprocess.Popen([executable, str(launcher)], cwd=package_dir, creationflags=creationflags)
            return True
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())], cwd=package_dir, creationflags=creationflags)
        return True
    except OSError:
        return False


def startup_launcher_enabled() -> bool:
    return os.getenv("WORKER_STARTUP_LAUNCHER_ENABLED", "true").strip().lower() not in FALSE_ENV_VALUES


def worker_gui_start_minimized() -> bool:
    return os.getenv("WORKER_GUI_START_MINIMIZED", "true").strip().lower() not in FALSE_ENV_VALUES


def local_web_host() -> str:
    return os.getenv("DESKTOP_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"


def local_web_port() -> int:
    return int(os.getenv("DESKTOP_WEB_PORT", "8090"))


def local_web_base_url() -> str:
    return f"http://{local_web_host()}:{local_web_port()}"


def local_web_url() -> str:
    return f"{local_web_base_url()}/app"


def terminate_package_local_web_processes() -> int:
    if os.name != "nt":
        return 0
    package_dir = str(Path(__file__).resolve().parent)
    app_path = str(Path(__file__).with_name("app.py"))
    package_literal = package_dir.replace("'", "''")
    app_literal = app_path.replace("'", "''")
    script = f"""
$packagePath = '{package_literal}'
$appPath = '{app_literal}'
$count = 0
Get-CimInstance Win32_Process |
    Where-Object {{
        $commandLine = [string]$_.CommandLine
        $processId = [int]$_.ProcessId
        $commandLine -and
        $processId -ne {os.getpid()} -and
        (
            $commandLine.IndexOf($appPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            (
                $commandLine -match 'app\\.py' -and
                $commandLine.IndexOf($packagePath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
            )
        )
    }} |
    ForEach-Object {{
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $count++
    }}
Write-Output $count
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    for line in reversed((completed.stdout or "").splitlines()):
        text = line.strip()
        if text.isdigit():
            return int(text)
    return 0


def chrome_executable_path() -> Path | None:
    configured = os.getenv("CHROME_PATH", "").strip().strip('"')
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    for root_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.getenv(root_name, "").strip()
        if root:
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def local_web_process_env() -> dict[str, str]:
    env = os.environ.copy()
    env["WEB_HOST"] = local_web_host()
    env["WEB_PORT"] = str(local_web_port())
    env["DESKTOP_FAST_MODE"] = "auto"
    env["PUBLIC_PC_REPORT_ENABLED"] = "true"
    env["PUBLIC_PC_REPORT_SERVER_URL"] = env.get("WORKER_SERVER_URL", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def local_web_python_executable() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        python_exe = executable.with_name("python.exe")
        if python_exe.exists():
            return str(python_exe)
    return sys.executable


def find_update_launcher(base_dir: Path | None = None) -> Path | None:
    root = base_dir or Path(__file__).resolve().parent
    candidates = [
        root / "UPDATE_PACKAGE.bat",
        root / "WinPython_公務電腦使用包" / "UPDATE_PACKAGE.bat",
    ]
    return next((path for path in candidates if path.exists()), None)


def acquire_single_instance_lock(name: str = SINGLE_INSTANCE_MUTEX_NAME) -> bool:
    global _SINGLE_INSTANCE_MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            return False
        _SINGLE_INSTANCE_MUTEX_HANDLE = handle
        return True
    except Exception:
        return True


def release_single_instance_lock() -> None:
    global _SINGLE_INSTANCE_MUTEX_HANDLE
    if os.name != "nt" or not _SINGLE_INSTANCE_MUTEX_HANDLE:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(_SINGLE_INSTANCE_MUTEX_HANDLE)
    except Exception:
        pass
    _SINGLE_INSTANCE_MUTEX_HANDLE = None


def show_single_instance_message() -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                "SinpoSmart - 救護Worker 已在執行中，請查看右下角系統匣圖示。",
                "SinpoSmart - 救護Worker",
                0x40,
            )
            return
        except Exception:
            pass
    print("SinpoSmart ambulance worker is already running.", file=sys.stderr)


def main() -> None:
    if not acquire_single_instance_lock():
        show_single_instance_message()
        return
    app = WorkerGui()
    app.after(100, app._start_local_web_app)
    app.after(100, app._start_worker_with_default_server)
    try:
        app.mainloop()
    finally:
        release_single_instance_lock()


if __name__ == "__main__":
    main()
