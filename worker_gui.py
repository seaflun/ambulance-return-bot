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
from tkinter import filedialog, messagebox, ttk
from typing import Any
from consumables_login import login_acs_and_get_driver, open_consumable_record_for_task
from disinfect import login_and_get_driver
from dotenv import load_dotenv

import worker
from ambulance_bot.chrome_launcher import open_url_in_worker_chrome
from ambulance_bot.duty_credentials import (
    DutyCredential,
    legacy_configured_saved_login_path,
    list_saved_duty_automation_credentials,
    load_saved_duty_automation_credential,
    save_duty_automation_credentials,
    saved_login_path,
    set_last_selected_duty_automation_credential,
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

GUI_THEME = {
    "bg": "#fff7ef",
    "surface": "#ffffff",
    "surface_soft": "#fff1e6",
    "ink": "#10233f",
    "muted": "#667085",
    "line": "#efd8c4",
    "accent": "#f08a4b",
    "accent_active": "#dc6f32",
    "success": "#2f8f6b",
    "success_active": "#247556",
    "status_bg": "#fff1e6",
    "log_bg": "#10233f",
    "log_fg": "#f8efe7",
}


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
            "waiting for session lock",
            "acquired session lock",
            "released session lock",
        )
    ):
        return ""
    if text == "[worker] no queued task":
        return ""

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

    if text.startswith("[worker] starting "):
        server = re.search(r"server=([^ ]+)", text)
        worker_id = re.search(r"worker_id=([^ ]+)", text)
        detail_parts = []
        if worker_id:
            detail_parts.append(worker_id.group(1))
        if server:
            detail_parts.append(server.group(1))
        return "系統｜Worker 已啟動" + (f"｜{'，'.join(detail_parts)}" if detail_parts else "")
    if text.startswith("[app] starting ambulance return web app on "):
        return ""
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
        source_text = source_match.group(1) if source_match else case_lookup_source_label(host_match.group(1) if host_match else "")
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
        ("四站登打啟動：", "四站｜啟動｜"),
        ("四站登打已啟動：", "四站｜已啟動｜"),
        ("四站登打流程結束：", "四站｜流程結束｜"),
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


class WorkerGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("救護回程 Worker")
        self.geometry("680x760")
        self.minsize(600, 680)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.log_path = Path(os.getenv("ARTIFACTS_DIR", "artifacts")) / "worker_gui.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.worker_thread: threading.Thread | None = None
        self.local_web_process: subprocess.Popen | None = None
        self.worker_started_at = ""

        self.server_url = tk.StringVar(value=initial_worker_server_url(os.getenv("WORKER_SERVER_URL", "")))
        self.local_web_url = tk.StringVar(value=local_web_url())
        self.worker_status = tk.StringVar(value="啟動中")
        self.worker_id = tk.StringVar(value=os.getenv("WORKER_ID", socket.gethostname() or "public-duty-pc"))
        self.duty_account = tk.StringVar(value=os.getenv("DUTY_ACCOUNT", ""))
        self.duty_password = tk.StringVar(value=os.getenv("DUTY_PASSWORD", ""))
        self.credential_choice = tk.StringVar(value="")
        self.package_version = tk.StringVar(value=current_package_version())
        self.connection_summary = tk.StringVar(value=f"目前連線：{self.server_url.get()}")
        self.saved_credentials: dict[str, DutyCredential] = {}
        self.credential_combo: ttk.Combobox | None = None
        self.duty_saved_login_path = tk.StringVar(value=str(saved_login_path()))
        self.credential_sync_status = tk.StringVar(value="")
        self.task_tree: ttk.Treeview | None = None
        self.tray_icon: Any | None = None
        self.tray_available = bool(pystray and Image and ImageDraw)

        self._configure_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._refresh_credential_choices(apply_first_if_empty=True)
        self.after(500, self.ensure_startup_tray_icon)
        self.after(250, self._drain_log)

    def _configure_styles(self) -> None:
        theme = GUI_THEME
        self.configure(bg=theme["bg"])
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        base_font = ("Microsoft JhengHei UI", 10)
        heading_font = ("Microsoft JhengHei UI", 24, "bold")
        card_heading_font = ("Microsoft JhengHei UI", 11, "bold")
        style.configure(".", font=base_font)
        style.configure("Root.TFrame", background=theme["bg"])
        style.configure("Header.TLabel", background=theme["bg"], foreground=theme["ink"], font=heading_font)
        style.configure("Subtle.TLabel", background=theme["bg"], foreground=theme["muted"], font=("Microsoft JhengHei UI", 10))
        style.configure(
            "Status.TLabel",
            background=theme["bg"],
            foreground=theme["ink"],
            padding=(14, 7),
            font=("Microsoft JhengHei UI", 10, "bold"),
        )
        style.configure("Card.TLabelframe", background=theme["surface"], bordercolor=theme["line"], relief="solid")
        style.configure("Card.TLabelframe.Label", background=theme["bg"], foreground=theme["ink"], font=card_heading_font)
        style.map("Card.TLabelframe.Label", background=[("active", theme["bg"]), ("!disabled", theme["bg"])])
        style.configure("Card.TFrame", background=theme["surface"])
        style.configure("Card.TLabel", background=theme["surface"], foreground=theme["ink"])
        style.configure("Hint.TLabel", background=theme["surface"], foreground=theme["muted"], font=("Microsoft JhengHei UI", 9))
        style.configure("Muted.TLabel", background=theme["surface"], foreground=theme["muted"], font=("Microsoft JhengHei UI", 9))
        style.configure(
            "TEntry",
            fieldbackground=theme["surface"],
            foreground=theme["ink"],
            bordercolor=theme["line"],
            lightcolor=theme["line"],
            darkcolor=theme["line"],
            padding=(8, 7),
        )
        style.configure(
            "TCombobox",
            fieldbackground=theme["surface"],
            foreground=theme["ink"],
            bordercolor=theme["line"],
            lightcolor=theme["line"],
            darkcolor=theme["line"],
            padding=(8, 7),
        )
        style.configure(
            "Primary.TButton",
            foreground="#ffffff",
            background=theme["accent"],
            bordercolor=theme["accent"],
            padding=(18, 11),
            focusthickness=1,
            font=("Microsoft JhengHei UI", 10, "bold"),
        )
        style.map("Primary.TButton", background=[("active", theme["accent_active"]), ("pressed", theme["accent_active"])])
        style.configure(
            "Success.TButton",
            foreground="#ffffff",
            background=theme["success"],
            bordercolor=theme["success"],
            padding=(18, 11),
            focusthickness=1,
            font=("Microsoft JhengHei UI", 10, "bold"),
        )
        style.map("Success.TButton", background=[("active", theme["success_active"]), ("pressed", theme["success_active"])])
        style.configure(
            "Soft.TButton",
            foreground=theme["ink"],
            background=theme["surface"],
            bordercolor=theme["line"],
            padding=(14, 9),
            focusthickness=1,
        )
        style.map("Soft.TButton", background=[("active", theme["surface_soft"]), ("pressed", theme["surface_soft"])])
        style.configure("Wide.TButton", padding=(18, 12), font=("Microsoft JhengHei UI", 10, "bold"))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=20, style="Root.TFrame")
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root, style="Root.TFrame")
        header.pack(fill="x", pady=(0, 16))
        title_block = ttk.Frame(header, style="Root.TFrame")
        title_block.pack(side="left", fill="x", expand=True)
        ttk.Label(title_block, text="救護回程 Worker", style="Header.TLabel").pack(anchor="w")
        ttk.Label(title_block, text="公務電腦本機網頁與 NAS 佇列 worker 控制面板", style="Subtle.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.worker_status, style="Status.TLabel").pack(side="right", padx=(12, 0))

        top_area = ttk.Frame(root, style="Root.TFrame")
        top_area.pack(fill="x")
        top_area.columnconfigure(0, weight=1)
        top_area.columnconfigure(1, weight=1)

        services = ttk.LabelFrame(top_area, text="服務", padding=16, style="Card.TLabelframe")
        services.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        services.columnconfigure(0, weight=1)

        local = ttk.Frame(services, style="Card.TFrame")
        local.grid(row=0, column=0, sticky="nsew")
        ttk.Label(local, text="本機快速網頁", style="Card.TLabel", font=("Microsoft JhengHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(local, text="按一次會確認服務，必要時啟動，然後開啟網頁。", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 8))
        ttk.Entry(local, textvariable=self.local_web_url, state="readonly").grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(local, text="開啟本機網頁", command=self._start_local_web_app, style="Primary.TButton").grid(row=3, column=0, sticky="ew")
        local.columnconfigure(0, weight=1)

        nas = ttk.Frame(services, style="Card.TFrame")
        nas.grid(row=1, column=0, sticky="nsew", pady=(16, 0))
        ttk.Label(nas, text="NAS Worker", style="Card.TLabel", font=("Microsoft JhengHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(nas, textvariable=self.connection_summary, style="Hint.TLabel", wraplength=260).grid(row=1, column=0, sticky="w", pady=(4, 8))
        ttk.Entry(nas, textvariable=self.server_url, state="readonly").grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(nas, text="自動測試並切換", command=self._test_connection, style="Soft.TButton").grid(row=3, column=0, sticky="ew")
        nas.columnconfigure(0, weight=1)

        right_stack = ttk.Frame(top_area, style="Root.TFrame")
        right_stack.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right_stack.columnconfigure(0, weight=1)

        credentials = ttk.LabelFrame(right_stack, text="帳號", padding=16, style="Card.TLabelframe")
        credentials.grid(row=0, column=0, sticky="ew")
        ttk.Label(credentials, text="同步帳號", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        self.credential_combo = ttk.Combobox(credentials, textvariable=self.credential_choice, state="readonly")
        self.credential_combo.grid(row=1, column=0, sticky="ew", pady=(4, 10))
        self.credential_combo.bind("<<ComboboxSelected>>", lambda _event: self._apply_selected_saved_credential())
        ttk.Button(credentials, text="匯入同步", command=self._import_credential_sync_file, style="Primary.TButton").grid(row=2, column=0, sticky="ew")
        ttk.Label(credentials, textvariable=self.credential_sync_status, wraplength=260, style="Hint.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 0))
        credentials.columnconfigure(0, weight=1)

        version_card = ttk.LabelFrame(right_stack, text="版本", padding=16, style="Card.TLabelframe")
        version_card.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(version_card, text="目前版本", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(version_card, textvariable=self.package_version, style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 10))
        ttk.Button(version_card, text="檢查更新", command=self._check_for_updates, style="Soft.TButton").grid(row=2, column=0, sticky="ew")
        version_card.columnconfigure(0, weight=1)

        log_frame = ttk.LabelFrame(root, text="執行紀錄", padding=12, style="Card.TLabelframe")
        log_frame.pack(fill="both", expand=True, pady=(14, 0))
        self.log_text = tk.Text(
            log_frame,
            height=13,
            wrap="word",
            bg=GUI_THEME["log_bg"],
            fg=GUI_THEME["log_fg"],
            insertbackground=GUI_THEME["log_fg"],
            relief="flat",
            padx=12,
            pady=10,
            font=("Consolas", 10),
        )
        self.log_text.pack(fill="both", expand=True)

        self._log("面板已啟動。")

    def _set_server(self, url: str) -> None:
        self.server_url.set(url)
        self._apply_server_url()
        self.connection_summary.set(f"目前連線：{url}")
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
            self._log(f"NAS 內網連線成功：{selected_url}")
        elif mode == "tailscale":
            self.connection_summary.set(f"目前連線：Tailscale {selected_url}")
            self._log(f"NAS 內網無法連線，已切換 Tailscale：{selected_url}")
        else:
            self.connection_summary.set(f"目前連線：未確認，暫留 {selected_url}")
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
        if self.local_web_process is not None and self.local_web_process.poll() is None:
            return
        if self._local_web_reachable():
            self._log(f"本機網頁已可使用：{self.local_web_url.get()}")
            self._open_local_web_app()
            return
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
                self.log_queue.put(text)

    def _run_local_web_app(self) -> None:
        try:
            import app as web_app

            web_app.run_web_app(host=local_web_host(), port=local_web_port())
        except Exception as exc:
            self.log_queue.put(f"本機網頁啟動失敗：{exc}")

    def _local_web_reachable(self) -> bool:
        try:
            worker.request_json(f"{local_web_base_url()}/status")
        except Exception:
            return False
        return True

    def _open_local_web_app(self) -> None:
        url = self.local_web_url.get().strip() or local_web_url()
        try:
            webbrowser.open_new_tab(url)
            self._log(f"已開啟本機網頁：{url}")
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
            "救護回程 Worker",
            pystray.Menu(
                pystray.MenuItem("救護回程 Worker", lambda _icon, _item: None, enabled=False),
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
        credentials = list_saved_duty_automation_credentials()
        self.saved_credentials = {credential_choice_label(credential): credential for credential in credentials}
        labels = list(self.saved_credentials)
        if self.credential_combo is not None:
            self.credential_combo.configure(values=labels)
        if not labels:
            self.credential_choice.set("")
            self.credential_sync_status.set("目前沒有同步帳號，請按「匯入同步」。")
            return

        current_account = self.duty_account.get().strip()
        selected = next((label for label, credential in self.saved_credentials.items() if credential.user_id == current_account), labels[0])
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
            self.credential_sync_status.set("匯入同步失敗：同步資料缺少帳號或密碼。")
            messagebox.showerror("匯入同步失敗", "同步資料缺少帳號或密碼。")
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
        self.worker_status.set(f"四站登打：{task_id}")
        self._log(f"四站登打啟動：{task_id}")
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

    def _run_selected_task_background(
        self,
        task_id: str,
        profile_name: str = "chrome_profile",
        debugger_port: int | None = None,
        use_session_lock: bool = True,
        tile_name: str = "",
        force_new_driver: bool = False,
        manage_manual_lock: bool = True,
        update_overall: bool | None = None,
    ):
        server_url = self.server_url.get().strip().rstrip("/")
        worker_id = self.worker_id.get().strip() or socket.gethostname() or "public-duty-pc"
        if update_overall is None:
            update_overall = manage_manual_lock
        if manage_manual_lock:
            worker.MANUAL_TASK_ACTIVE.set()
        started_at = time.monotonic()
        try:
            self.log_queue.put("工作紀錄：向 NAS 取任務...")
            task = worker.fetch_task(server_url, task_id)
            self.log_queue.put(f"工作紀錄：取任務完成，耗時 {time.monotonic() - started_at:.1f} 秒")
            if not task:
                self.log_queue.put(f"找不到任務：{task_id}")
                return
            self.log_queue.put(f"開始執行選取任務：{task_id}")
            selenium_started_at = time.monotonic()
            result = worker.run_task(
                server_url,
                worker_id,
                task,
                Path(os.getenv("ARTIFACTS_DIR", "artifacts")),
                profile_name=profile_name,
                debugger_port=debugger_port,
                use_session_lock=use_session_lock,
                tile_name=tile_name,
                force_new_driver=force_new_driver,
                update_overall=update_overall,
            )
            if result is not None:
                self.log_queue.put(f"工作紀錄結果：{result.status}；{result.detail}")
            self.log_queue.put(f"選取任務已執行完成：{task_id}，登打耗時 {time.monotonic() - selenium_started_at:.1f} 秒")
            self._refresh_tasks()
            return result
        except Exception as exc:
            self.log_queue.put(f"執行選取任務失敗：{task_id} {exc}")
            return None
        finally:
            if manage_manual_lock:
                worker.MANUAL_TASK_ACTIVE.clear()

    def _run_selected_vehicle_mileage_background(
        self,
        task_id: str,
        profile_name: str = "chrome_profile",
        debugger_port: int | None = None,
        use_session_lock: bool = True,
        tile_name: str = "",
        force_new_driver: bool = False,
        manage_manual_lock: bool = True,
        update_overall: bool | None = None,
    ):
        server_url = self.server_url.get().strip().rstrip("/")
        worker_id = self.worker_id.get().strip() or socket.gethostname() or "public-duty-pc"
        if update_overall is None:
            update_overall = manage_manual_lock
        if manage_manual_lock:
            worker.MANUAL_TASK_ACTIVE.set()
        started_at = time.monotonic()
        try:
            if use_session_lock and not _worker_chrome_is_running():
                self.log_queue.put("車輛里程：預先喚起 Chrome...")
                open_url_in_worker_chrome("about:blank")
            self.log_queue.put("車輛里程：向 NAS 取任務...")
            task = worker.fetch_task(server_url, task_id)
            self.log_queue.put(f"車輛里程：取任務完成，耗時 {time.monotonic() - started_at:.1f} 秒")
            if not task:
                self.log_queue.put(f"找不到任務：{task_id}")
                return
            self.log_queue.put(f"開始執行車輛里程：{task_id}")
            selenium_started_at = time.monotonic()
            result = worker.run_vehicle_task(
                server_url,
                worker_id,
                task,
                Path(os.getenv("ARTIFACTS_DIR", "artifacts")),
                profile_name=profile_name,
                debugger_port=debugger_port,
                use_session_lock=use_session_lock,
                tile_name=tile_name,
                force_new_driver=force_new_driver,
                update_overall=update_overall,
            )
            if result is not None:
                self.log_queue.put(f"車輛里程結果：{result.status}；{result.detail}")
            self.log_queue.put(f"車輛里程已執行完成：{task_id}，登打耗時 {time.monotonic() - selenium_started_at:.1f} 秒")
            self._refresh_tasks()
            return result
        except Exception as exc:
            self.log_queue.put(f"執行車輛里程失敗：{task_id} {exc}")
            return None
        finally:
            if manage_manual_lock:
                worker.MANUAL_TASK_ACTIVE.clear()

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
    ):
        server_url = self.server_url.get().strip().rstrip("/")
        worker_id = self.worker_id.get().strip() or socket.gethostname() or "public-duty-pc"
        if update_overall is None:
            update_overall = manage_manual_lock
        if manage_manual_lock:
            worker.MANUAL_TASK_ACTIVE.set()
        started_at = time.monotonic()
        try:
            # 💡 註解掉舊的預喚起 Chrome 邏輯，因為我們會用 login_and_get_driver() 精準喚起
            # if not _worker_chrome_is_running():
            #     self.log_queue.put("消毒紀錄：預先喚起 Chrome...")
            #     open_url_in_worker_chrome("about:blank")
            
            self.log_queue.put("消毒紀錄：向 NAS 取任務...")
            task = worker.fetch_task(server_url, task_id)
            self.log_queue.put(f"消毒紀錄：取任務完成，耗時 {time.monotonic() - started_at:.1f} 秒")
            
            if not task:
                self.log_queue.put(f"找不到任務：{task_id}")
                return
                
            self.log_queue.put(f"開始執行消毒紀錄：{task_id}")
            selenium_started_at = time.monotonic()
            
            # 🚀 方案 B 核心改動：先呼叫自動化登入，拿到已經登入成功的瀏覽器 driver
            self.log_queue.put("消毒紀錄：正在啟動 Chrome 並進行 AI 驗證碼登入...")
            active_driver = login_and_get_driver(profile_name=profile_name, debugger_port=debugger_port, tile_name=tile_name)
            
            # 將這個登入好的 active_driver 餵給原本的 worker 任務執行後續動作
            # (備註：請確保你的 worker.run_disinfection_worker_task 支援傳入自訂 driver 參數)
            result = worker.run_disinfection_worker_task(
                server_url,
                worker_id,
                task,
                Path(os.getenv("ARTIFACTS_DIR", "artifacts")),
                driver=active_driver,
                profile_name=profile_name,
                debugger_port=debugger_port,
                use_session_lock=use_session_lock,
                tile_name=tile_name,
                force_new_driver=force_new_driver,
                update_overall=update_overall,
            )
            elapsed = time.monotonic() - selenium_started_at
            if result is not None:
                self.log_queue.put(f"???????{result.status}??? {elapsed:.1f} ?")
                self.log_queue.put(f"???????{result.detail}")
            else:
                self.log_queue.put(f"??????????{task_id}??? {elapsed:.1f} ?")
            self._refresh_tasks()
            return result
            
        except Exception as exc:
            self.log_queue.put(f"執行消毒紀錄失敗：{task_id} {exc}")
            try:
                worker.post_status(
                    server_url,
                    task_id,
                    "disinfection_failed",
                    f"消毒紀錄操作失敗：{exc}",
                    site_key="disinfection",
                    site_name="緊急救護消毒",
                )
            except Exception as post_exc:
                self.log_queue.put(f"消毒紀錄失敗狀態回寫失敗：{post_exc}")
            return None
        finally:
            if manage_manual_lock:
                worker.MANUAL_TASK_ACTIVE.clear()

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
    ):
        server_url = self.server_url.get().strip().rstrip("/")
        worker_id = self.worker_id.get().strip() or socket.gethostname() or "public-duty-pc"
        if manage_manual_lock:
            worker.MANUAL_TASK_ACTIVE.set()
        started_at = time.monotonic()
        try:
            self.log_queue.put("耗材：向 NAS 取任務...")
            task = worker.fetch_task(server_url, task_id)
            if not task:
                self.log_queue.put(f"找不到任務：{task_id}")
                return
            self.log_queue.put("耗材：正在啟動 Chrome 並登入一站通...")
            driver = login_acs_and_get_driver(profile_name=profile_name, debugger_port=debugger_port, tile_name=tile_name)
            detail = open_consumable_record_for_task(driver, task)
            worker.post_status(
                server_url,
                task_id,
                "consumables_saved",
                detail,
                site_key="consumables",
                site_name="一站通耗材",
            )
            if update_overall:
                worker.post_status(server_url, task_id, "desktop_fast_completed", detail)
            self.log_queue.put(f"耗材系統已開啟案件內容：{detail}，耗時 {time.monotonic() - started_at:.1f} 秒")
            self._refresh_tasks()
            return "consumables_saved"
        except Exception as exc:
            self.log_queue.put(f"執行耗材失敗：{task_id} {exc}")
            try:
                worker.post_status(
                    server_url,
                    task_id,
                    "consumables_failed",
                    f"耗材操作失敗：{exc}",
                    site_key="consumables",
                    site_name="一站通耗材",
                )
            except Exception as post_exc:
                self.log_queue.put(f"耗材失敗狀態回寫失敗：{post_exc}")
            if update_overall:
                try:
                    worker.post_status(server_url, task_id, "desktop_fast_completed_with_errors", f"耗材操作失敗：{exc}")
                except Exception:
                    pass
            return "consumables_failed"
        finally:
            if manage_manual_lock:
                worker.MANUAL_TASK_ACTIVE.clear()

    def _run_selected_all_sites_background(self, task_id: str) -> None:
        profile_suffix = task_id.replace("-", "_")
        runners = [
            ("工作紀錄", "duty_work_log", self._run_selected_task_background, f"duty_work_log_profile_{profile_suffix}", None, "duty_work_log"),
            ("車輛里程", "vehicle_mileage", self._run_selected_vehicle_mileage_background, f"vehicle_mileage_profile_{profile_suffix}", None, "vehicle_mileage"),
            ("消毒紀錄", "disinfection", self._run_selected_disinfection_background, f"disinfection_profile_{profile_suffix}", None, "disinfection"),
            ("耗材", "consumables", self._run_selected_consumables_background, f"consumables_profile_{profile_suffix}", None, "consumables"),
        ]
        server_url = self.server_url.get().strip().rstrip("/")
        worker.MANUAL_TASK_ACTIVE.set()
        try:
            worker.post_status(server_url, task_id, "desktop_fast_running", "本機快速執行已啟動。")
            blocked_site = ""
            for name, site_key, target, profile_name, debugger_port, tile_name in runners:
                payload = worker.fetch_task_payload(server_url, task_id)
                if not payload:
                    blocked_site = name
                    break
                site_statuses = payload.get("site_statuses") if isinstance(payload.get("site_statuses"), dict) else {}
                current_status = str((site_statuses.get(site_key) or {}).get("status") or "")
                if _gui_site_is_complete(current_status):
                    self.log_queue.put(f"四站登打略過：{name} 已完成")
                    continue
                self.log_queue.put(f"四站登打已啟動：{name}")
                target(task_id, profile_name, debugger_port, False, tile_name, True, False)
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
                worker.post_status(server_url, task_id, "desktop_fast_completed", "四站登打完成。")
            self.log_queue.put(f"四站登打流程結束：{task_id}")
            self._refresh_tasks()
        finally:
            worker.MANUAL_TASK_ACTIVE.clear()

    def _warm_worker_chrome(self) -> None:
        if os.getenv("WORKER_WARM_CHROME_ON_START", "true").strip().lower() in {"0", "false", "no", "off"}:
            return
        threading.Thread(target=self._warm_worker_chrome_background, daemon=True).start()

    def _warm_worker_chrome_background(self) -> None:
        try:
            if _worker_chrome_is_running():
                self.log_queue.put("Chrome 已在背景待命。")
                return
            status = open_url_in_worker_chrome("about:blank")
            self.log_queue.put(f"Chrome 已預先啟動：{status}")
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
    if value.startswith("needs_") or "login" in value:
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
    name = credential.name or _name_from_display_name(credential.display_name) or "未填姓名"
    account = credential.user_id or "未填帳號"
    return f"{actor} {name} - {account}"


def _name_from_display_name(display_name: str) -> str:
    text = str(display_name or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\s*\d+\s*番\s*", "", text)
    return text.strip()


def persist_selected_saved_credential(credential: DutyCredential) -> Path:
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
    user_id = str(selected.get("user_id") or "").strip()
    password = str(selected.get("password") or "")
    if not user_id or not password:
        return None
    last_selected = str(payload.get("user_id") or payload.get("actor_no") or user_id).strip()
    path = save_duty_automation_credentials(accounts, last_selected=last_selected)
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


def local_web_host() -> str:
    return os.getenv("DESKTOP_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"


def local_web_port() -> int:
    return int(os.getenv("DESKTOP_WEB_PORT", "8090"))


def local_web_base_url() -> str:
    return f"http://{local_web_host()}:{local_web_port()}"


def local_web_url() -> str:
    return f"{local_web_base_url()}/app"


def local_web_process_env() -> dict[str, str]:
    env = os.environ.copy()
    env["WEB_HOST"] = local_web_host()
    env["WEB_PORT"] = str(local_web_port())
    env["DESKTOP_FAST_MODE"] = "auto"
    env["PUBLIC_PC_REPORT_ENABLED"] = "true"
    env["PUBLIC_PC_REPORT_SERVER_URL"] = env.get("WORKER_SERVER_URL", "")
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
                "救護回程 Worker 已在執行中，請查看右下角系統匣圖示。",
                "救護回程 Worker",
                0x40,
            )
            return
        except Exception:
            pass
    print("Ambulance return worker is already running.", file=sys.stderr)


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
