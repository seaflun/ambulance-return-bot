from __future__ import annotations

import threading
import time
from pathlib import Path

from .adapters import SiteAutomationResult, default_adapters
from .line_api import configured_recipients, push_text
from .local_desktop import local_browser_enabled, open_task_on_local_desktop
from .models import AmbulanceReturnRequest
from .selenium_local import run_local_selenium_task, selenium_enabled
from .task_store import JsonTaskStore


class TaskRunner:
    def __init__(self, artifacts_dir: Path, store: JsonTaskStore | None = None) -> None:
        self.artifacts_dir = artifacts_dir
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.store = store or JsonTaskStore(self.artifacts_dir / "tasks")
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def start(self, request: AmbulanceReturnRequest, reply_to: str = "") -> str:
        try:
            self.store.get(request.task_id)
        except FileNotFoundError:
            self.store.create(request)
        return self.start_existing(request.task_id, reply_to=reply_to)

    def start_existing(self, task_id: str, reply_to: str = "") -> str:
        with self._lock:
            if task_id in self._running:
                return task_id
            self._running.add(task_id)
        thread = threading.Thread(target=self._run, args=(task_id, reply_to), daemon=True)
        thread.start()
        return task_id

    def status(self, task_id: str) -> str:
        try:
            return str(self.store.get(task_id).get("overall_status") or "unknown")
        except FileNotFoundError:
            return "not_found"

    def wait_for_idle(self, timeout_seconds: float = 5.0) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            with self._lock:
                if not self._running:
                    return True
            time.sleep(0.05)
        return False

    def latest_status_text(self) -> str:
        tasks = self.store.list_recent(limit=1)
        if not tasks:
            return "\u76ee\u524d\u6c92\u6709\u4efb\u52d9\u3002"
        payload = tasks[0]
        task_id = payload["task"]["task_id"]
        return f"\u6700\u8fd1\u4efb\u52d9\uff1a{task_id}\n\u72c0\u614b\uff1a{payload.get('overall_status', 'unknown')}"

    def _run(self, task_id: str, reply_to: str) -> None:
        self.store.set_overall_status(task_id, "running", "\u672c\u6a5f\u96fb\u8166\u64cd\u4f5c\u6d41\u7a0b\u5df2\u555f\u52d5\u3002")
        request = self.store.request_for(task_id)
        results: list[SiteAutomationResult] = []
        try:
            for adapter in default_adapters():
                result = adapter.run(request)
                results.append(result)
                self.store.update_site_result(task_id, result)

            if selenium_enabled():
                selenium_result = run_local_selenium_task(request, self.artifacts_dir)
                self.store.set_overall_status(
                    task_id,
                    selenium_result.status,
                    selenium_result.detail,
                )
            elif local_browser_enabled():
                summary_path = open_task_on_local_desktop(request, self.artifacts_dir)
                self.store.set_overall_status(task_id, "local_browser_opened", f"\u5df2\u5728\u672c\u6a5f\u96fb\u8166\u958b\u555f\u56db\u500b\u7db2\u7ad9\u5206\u9801\uff0c\u6458\u8981\u6a94\uff1a{summary_path}")
            else:
                self.store.set_overall_status(
                    task_id,
                    "needs_user_review",
                    "\u56db\u7ad9\u9810\u586b\u8a08\u756b\u5df2\u5efa\u7acb\uff0c\u672c\u6a5f\u958b\u9801\u529f\u80fd\u672a\u555f\u7528\u3002",
                )
            message = self._completion_message(request, results)
        except Exception as exc:
            self.store.set_overall_status(task_id, "failed", str(exc))
            message = f"\u6551\u8b77\u56de\u7a0b\u4efb\u52d9\u5931\u6557\uff1a{task_id}\n{exc}"
            print(f"[task] failed {task_id}: {exc}", flush=True)
        finally:
            with self._lock:
                self._running.discard(task_id)
        self._notify(reply_to, message)

    def _notify(self, reply_to: str, message: str) -> None:
        recipients = [reply_to] if reply_to else configured_recipients()
        for recipient in recipients:
            try:
                push_text(recipient, message)
            except Exception as exc:
                print(f"[line] notification failed for {recipient}: {exc}", flush=True)

    def _completion_message(self, request: AmbulanceReturnRequest, results: list[SiteAutomationResult]) -> str:
        lines = [f"\u6551\u8b77\u56de\u7a0b\u4efb\u52d9\u5df2\u9001\u5230\u672c\u6a5f\u96fb\u8166\uff1a{request.task_id}", "", request.summary, "", "\u56db\u7ad9\u72c0\u614b\uff1a"]
        lines.extend(f"- {result.name}: {result.status}" for result in results)
        lines.append("")
        lines.append("\u64cd\u4f5c\u6703\u5728\u57f7\u884c Flask \u7684\u9019\u53f0\u96fb\u8166\u4e0a\u958b\u555f\u700f\u89bd\u5668\uff1b\u7b2c\u4e00\u7248\u4e0d\u6703\u81ea\u52d5\u6309\u6700\u5f8c\u9001\u51fa\u3002")
        return "\n".join(lines)
