from __future__ import annotations

import os
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from dotenv import load_dotenv

import worker
from ambulance_bot.adapters import SITE_DEFINITIONS
from ambulance_bot.chrome_launcher import open_url_in_worker_chrome


load_dotenv()

NAS_LAN_URL = "http://10.30.65.30:8080"
NAS_TAILSCALE_URL = "http://100.114.126.58:8080"


class WorkerGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("救護回程 Worker")
        self.geometry("760x620")
        self.minsize(680, 560)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.worker_started_at = ""

        self.server_url = tk.StringVar(value=os.getenv("WORKER_SERVER_URL", NAS_LAN_URL))
        self.worker_status = tk.StringVar(value="未啟動")
        self.worker_id = tk.StringVar(value=os.getenv("WORKER_ID", socket.gethostname() or "public-duty-pc"))
        self.profile_dir = tk.StringVar(value=os.getenv("CHROME_PROFILE_DIR", ""))
        self.profile_email = tk.StringVar(value=os.getenv("CHROME_PROFILE_EMAIL", ""))

        self._build_ui()
        self.after(250, self._drain_log)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        title = ttk.Label(root, text="救護回程 Worker", font=("Microsoft JhengHei UI", 20, "bold"))
        title.pack(anchor="w")
        desc = ttk.Label(root, text="這個程式會啟動背景 worker，並提供四個入口按鈕；不會按最後儲存或送出。")
        desc.pack(anchor="w", pady=(4, 14))

        status = ttk.LabelFrame(root, text="狀態", padding=12)
        status.pack(fill="x")
        self._row(status, "Worker", self.worker_status, 0)
        self._row(status, "Worker ID", self.worker_id, 1)
        self._row(status, "Chrome profile", self.profile_dir, 2)
        self._row(status, "Profile 帳號", self.profile_email, 3)

        server = ttk.LabelFrame(root, text="NAS 連線", padding=12)
        server.pack(fill="x", pady=(12, 0))
        entry = ttk.Entry(server, textvariable=self.server_url)
        entry.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        server.columnconfigure(0, weight=1)
        ttk.Button(server, text="使用內網", command=lambda: self._set_server(NAS_LAN_URL)).grid(row=1, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(server, text="使用 Tailscale", command=lambda: self._set_server(NAS_TAILSCALE_URL)).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(server, text="測試連線", command=self._test_connection).grid(row=1, column=2, sticky="ew", padx=6)
        ttk.Button(server, text="啟動 / 重啟 Worker", command=self._restart_worker).grid(row=1, column=3, sticky="ew", padx=(6, 0))

        sites = ttk.LabelFrame(root, text="四站入口", padding=12)
        sites.pack(fill="x", pady=(12, 0))
        ttk.Button(sites, text="開啟全部四站", command=self._open_all_sites).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        for index, site in enumerate(SITE_DEFINITIONS, start=1):
            button = ttk.Button(sites, text=site.name, command=lambda item=site: self._open_site(item.key))
            row = (index + 1) // 2
            col = (index - 1) % 2
            button.grid(row=row, column=col, sticky="ew", padx=6, pady=6)
        sites.columnconfigure(0, weight=1)
        sites.columnconfigure(1, weight=1)

        log_frame = ttk.LabelFrame(root, text="Log", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(log_frame, height=12, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self._log("面板已啟動。")

    def _row(self, parent: ttk.Frame, label: str, var: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Label(parent, textvariable=var, font=("Microsoft JhengHei UI", 10, "bold")).grid(
            row=row, column=1, sticky="w", padx=(12, 0), pady=3
        )

    def _set_server(self, url: str) -> None:
        self.server_url.set(url)
        self._apply_server_url()
        self._log(f"NAS URL 已切換：{url}")

    def _apply_server_url(self) -> None:
        os.environ["WORKER_SERVER_URL"] = self.server_url.get().strip().rstrip("/")

    def _restart_worker(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self._log("目前 worker 已在執行；請關閉本程式再完全重啟。")
            return
        self._apply_server_url()
        os.environ["WORKER_RUN_ONCE"] = "false"
        self.worker_started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.worker_status.set(f"執行中，啟動於 {self.worker_started_at}")
        self.worker_thread = threading.Thread(target=self._run_worker, name="ambulance-worker", daemon=True)
        self.worker_thread.start()
        self._log("worker 已啟動。")

    def _run_worker(self) -> None:
        try:
            worker.main()
        except Exception as exc:
            self.log_queue.put(f"worker 結束：{exc}")
            self.worker_status.set("已停止")

    def _test_connection(self) -> None:
        self._apply_server_url()
        threading.Thread(target=self._test_connection_background, daemon=True).start()

    def _test_connection_background(self) -> None:
        try:
            data = worker.request_json(f"{self.server_url.get().strip().rstrip('/')}/status")
        except Exception as exc:
            self.log_queue.put(f"NAS 連線失敗：{exc}")
            return
        self.log_queue.put(f"NAS 連線成功：{data}")

    def _open_site(self, site_key: str) -> None:
        site = next((item for item in SITE_DEFINITIONS if item.key == site_key), None)
        if site is None:
            messagebox.showerror("錯誤", "找不到網站入口")
            return
        status = open_url_in_worker_chrome(site.url)
        self._log(f"已開啟 {site.name}: {status}")

    def _open_all_sites(self) -> None:
        delay = float(os.getenv("BROWSER_OPEN_DELAY_SECONDS", "0.4"))
        for site in SITE_DEFINITIONS:
            status = open_url_in_worker_chrome(site.url)
            self._log(f"已開啟 {site.name}: {status}")
            if delay > 0:
                time.sleep(delay)

    def _log(self, message: str) -> None:
        self.log_queue.put(f"{time.strftime('%H:%M:%S')} {message}")

    def _drain_log(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
        self.after(250, self._drain_log)


def main() -> None:
    app = WorkerGui()
    app._restart_worker()
    app.mainloop()


if __name__ == "__main__":
    main()
