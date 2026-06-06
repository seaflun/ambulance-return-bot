from __future__ import annotations

import os
import queue
import socket
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from dotenv import load_dotenv

import worker
from ambulance_bot.adapters import SITE_DEFINITIONS
from ambulance_bot.chrome_launcher import open_url_in_worker_chrome
from ambulance_bot.duty_credentials import load_saved_duty_automation_credential, saved_login_path


load_dotenv()

NAS_LAN_URL = "http://10.30.65.30:8080"
NAS_TAILSCALE_URL = "http://100.114.126.58:8080"


class WorkerGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("救護回程 Worker")
        self.geometry("900x760")
        self.minsize(780, 680)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.worker_started_at = ""

        self.server_url = tk.StringVar(value=os.getenv("WORKER_SERVER_URL", NAS_LAN_URL))
        self.worker_status = tk.StringVar(value="未啟動")
        self.worker_id = tk.StringVar(value=os.getenv("WORKER_ID", socket.gethostname() or "public-duty-pc"))
        self.duty_account = tk.StringVar(value=os.getenv("DUTY_ACCOUNT", ""))
        self.duty_password = tk.StringVar(value=os.getenv("DUTY_PASSWORD", ""))
        self.duty_saved_login_path = tk.StringVar(value=str(saved_login_path()))
        self.task_tree: ttk.Treeview | None = None

        self._build_ui()
        self.after(250, self._drain_log)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        title = ttk.Label(root, text="救護回程 Worker", font=("Microsoft JhengHei UI", 20, "bold"))
        title.pack(anchor="w")
        desc = ttk.Label(root, text="背景 worker 查詢案件；登打任務先在下方選取後手動執行，不會按最後儲存或送出。")
        desc.pack(anchor="w", pady=(4, 14))

        status = ttk.LabelFrame(root, text="狀態", padding=12)
        status.pack(fill="x")
        self._row(status, "Worker", self.worker_status, 0)
        self._row(status, "Worker ID", self.worker_id, 1)

        server = ttk.LabelFrame(root, text="NAS 連線", padding=12)
        server.pack(fill="x", pady=(12, 0))
        entry = ttk.Entry(server, textvariable=self.server_url)
        entry.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        server.columnconfigure(0, weight=1)
        ttk.Button(server, text="使用內網", command=lambda: self._set_server(NAS_LAN_URL)).grid(row=1, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(server, text="使用 Tailscale", command=lambda: self._set_server(NAS_TAILSCALE_URL)).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(server, text="測試連線", command=self._test_connection).grid(row=1, column=2, sticky="ew", padx=6)
        ttk.Button(server, text="啟動 / 重啟 Worker", command=self._restart_worker).grid(row=1, column=3, sticky="ew", padx=(6, 0))

        credentials = ttk.LabelFrame(root, text="消防勤務自動登入", padding=12)
        credentials.pack(fill="x", pady=(12, 0))
        ttk.Label(credentials, text="帳號").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(credentials, textvariable=self.duty_account).grid(row=0, column=1, sticky="ew", padx=(8, 12), pady=3)
        ttk.Label(credentials, text="密碼").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(credentials, textvariable=self.duty_password, show="*").grid(row=1, column=1, sticky="ew", padx=(8, 12), pady=3)
        ttk.Button(credentials, text="儲存到 .env", command=self._save_duty_credentials).grid(row=0, column=2, rowspan=2, sticky="nsew")
        ttk.Button(credentials, text="載入值班專案帳密", command=self._load_saved_duty_credentials).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(credentials, textvariable=self.duty_saved_login_path).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        credentials.columnconfigure(1, weight=1)

        sites = ttk.LabelFrame(root, text="四站入口", padding=12)
        sites.pack(fill="x", pady=(12, 0))
        for index, site in enumerate(SITE_DEFINITIONS):
            button = ttk.Button(sites, text=site.name, command=lambda item=site: self._open_site(item.key))
            row = index // 2
            col = index % 2
            button.grid(row=row, column=col, sticky="ew", padx=6, pady=6)
        sites.columnconfigure(0, weight=1)
        sites.columnconfigure(1, weight=1)

        tasks = ttk.LabelFrame(root, text="NAS 任務", padding=12)
        tasks.pack(fill="both", expand=True, pady=(12, 0))
        task_actions = ttk.Frame(tasks)
        task_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(task_actions, text="刷新任務", command=self._refresh_tasks).pack(side="left")
        ttk.Button(task_actions, text="執行工作紀錄", command=self._run_selected_task).pack(side="left", padx=(8, 0))
        ttk.Button(task_actions, text="執行車輛里程", command=self._run_selected_vehicle_mileage).pack(side="left", padx=(8, 0))
        columns = ("status", "vehicle", "driver", "time", "address")
        self.task_tree = ttk.Treeview(tasks, columns=columns, show="tree headings", height=5)
        self.task_tree.heading("#0", text="任務 ID")
        self.task_tree.heading("status", text="狀態")
        self.task_tree.heading("vehicle", text="車輛")
        self.task_tree.heading("driver", text="司機")
        self.task_tree.heading("time", text="時間")
        self.task_tree.heading("address", text="地址")
        self.task_tree.column("#0", width=155, stretch=False)
        self.task_tree.column("status", width=130, stretch=False)
        self.task_tree.column("vehicle", width=70, stretch=False)
        self.task_tree.column("driver", width=80, stretch=False)
        self.task_tree.column("time", width=95, stretch=False)
        self.task_tree.column("address", width=330, stretch=True)
        self.task_tree.pack(fill="both", expand=True)

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

    def _save_duty_credentials(self) -> None:
        account = self.duty_account.get().strip()
        password = self.duty_password.get()
        if not account or not password:
            messagebox.showerror("缺少資料", "請輸入消防勤務帳號與密碼。")
            return
        update_env_values(
            {
                "DUTY_ACCOUNT": account,
                "DUTY_PASSWORD": password,
            }
        )
        os.environ["DUTY_ACCOUNT"] = account
        os.environ["DUTY_PASSWORD"] = password
        self._log("消防勤務帳密已儲存到 .env。")

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

    def _open_site(self, site_key: str) -> None:
        site = next((item for item in SITE_DEFINITIONS if item.key == site_key), None)
        if site is None:
            messagebox.showerror("錯誤", "找不到網站入口")
            return
        status = open_url_in_worker_chrome(site.url)
        self._log(f"已開啟 {site.name}: {status}")

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
            task_id, values = task_row_values(payload)
            if task_id:
                self.task_tree.insert("", "end", iid=task_id, text=task_id, values=values)

    def _run_selected_task(self) -> None:
        task_id = self._selected_task_id()
        if not task_id:
            return
        self._apply_server_url()
        threading.Thread(target=self._run_selected_task_background, args=(task_id,), daemon=True).start()

    def _run_selected_vehicle_mileage(self) -> None:
        task_id = self._selected_task_id()
        if not task_id:
            return
        self._apply_server_url()
        threading.Thread(target=self._run_selected_vehicle_mileage_background, args=(task_id,), daemon=True).start()

    def _selected_task_id(self) -> str:
        if self.task_tree is None:
            return ""
        selected = self.task_tree.selection()
        if not selected:
            messagebox.showerror("未選任務", "請先在 NAS 任務清單選一筆任務。")
            return ""
        return str(selected[0])

    def _run_selected_task_background(self, task_id: str) -> None:
        server_url = self.server_url.get().strip().rstrip("/")
        worker_id = self.worker_id.get().strip() or socket.gethostname() or "public-duty-pc"
        try:
            task = worker.fetch_task(server_url, task_id)
            if not task:
                self.log_queue.put(f"找不到任務：{task_id}")
                return
            self.log_queue.put(f"開始執行選取任務：{task_id}")
            worker.run_task(server_url, worker_id, task, Path(os.getenv("ARTIFACTS_DIR", "artifacts")))
            self.log_queue.put(f"選取任務已執行完成：{task_id}")
            self._refresh_tasks()
        except Exception as exc:
            self.log_queue.put(f"執行選取任務失敗：{task_id} {exc}")

    def _run_selected_vehicle_mileage_background(self, task_id: str) -> None:
        server_url = self.server_url.get().strip().rstrip("/")
        worker_id = self.worker_id.get().strip() or socket.gethostname() or "public-duty-pc"
        try:
            task = worker.fetch_task(server_url, task_id)
            if not task:
                self.log_queue.put(f"找不到任務：{task_id}")
                return
            self.log_queue.put(f"開始執行車輛里程：{task_id}")
            worker.run_vehicle_task(server_url, worker_id, task, Path(os.getenv("ARTIFACTS_DIR", "artifacts")))
            self.log_queue.put(f"車輛里程已執行完成：{task_id}")
            self._refresh_tasks()
        except Exception as exc:
            self.log_queue.put(f"執行車輛里程失敗：{task_id} {exc}")

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


def task_row_values(payload: dict[str, object]) -> tuple[str, tuple[str, str, str, str, str]]:
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    task_dict = task if isinstance(task, dict) else {}
    task_id = str(task_dict.get("task_id") or "")
    status = str(payload.get("overall_status") or "")
    vehicle = str(task_dict.get("vehicle") or "")
    driver = str(task_dict.get("driver") or "")
    case_time = str(task_dict.get("case_time") or "")
    return_time = str(task_dict.get("return_time") or "")
    address = str(task_dict.get("case_address") or "")
    time_text = f"{case_time}/{return_time}".strip("/")
    return task_id, (status, vehicle, driver, time_text, address)


def main() -> None:
    app = WorkerGui()
    app._restart_worker()
    app.mainloop()


def update_env_values(values: dict[str, str]) -> None:
    path = os.getenv("DOTENV_PATH", ".env")
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()

    seen: set[str] = set()
    updated: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            updated.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            updated.append(line)

    if updated and updated[-1].strip():
        updated.append("")
    for key, value in values.items():
        if key not in seen:
            updated.append(f"{key}={value}")

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(updated).rstrip() + "\n")


if __name__ == "__main__":
    main()
