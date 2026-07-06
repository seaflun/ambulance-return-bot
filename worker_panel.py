from __future__ import annotations

import os
import socket
import threading
import time
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, url_for

import worker
from ambulance_bot.adapters import SITE_DEFINITIONS
from ambulance_bot.chrome_launcher import open_url_in_worker_chrome
from ambulance_bot.profile_paths import runtime_profile_root


load_dotenv()

PUBLIC_DUTY_TEMPLATES = Path(__file__).resolve().parent / "WinPython_公務電腦使用包" / "templates"

app = Flask(__name__, template_folder=str(PUBLIC_DUTY_TEMPLATES))
worker_thread: threading.Thread | None = None
worker_started_at = ""
last_opened: dict[str, str] = {}


@app.get("/")
def panel():
    server_url = os.getenv("WORKER_SERVER_URL", "http://127.0.0.1:8080")
    return render_template(
        "worker_panel.html",
        sites=SITE_DEFINITIONS,
        worker_running=worker_thread is not None and worker_thread.is_alive(),
        worker_started_at=worker_started_at,
        worker_id=os.getenv("WORKER_ID", socket.gethostname() or "public-duty-pc"),
        server_url=server_url,
        poll_seconds=os.getenv("WORKER_POLL_SECONDS", "10"),
        lookup_interval=os.getenv("CASE_LOOKUP_INTERVAL_SECONDS", "300"),
        profile_root_dir=str(runtime_profile_root()),
        chrome_profile_email=os.getenv("CHROME_PROFILE_EMAIL", ""),
        last_opened=last_opened,
    )


@app.post("/open/<site_key>")
def open_site(site_key: str):
    site = next((item for item in SITE_DEFINITIONS if item.key == site_key), None)
    if site is None:
        abort(404)
    status = open_url_in_worker_chrome(site.url)
    last_opened[site.key] = f"{time.strftime('%H:%M:%S')} {status}"
    return redirect(url_for("panel"))


def start_worker_thread() -> None:
    global worker_thread, worker_started_at
    if worker_thread is not None and worker_thread.is_alive():
        return
    os.environ["WORKER_RUN_ONCE"] = "false"
    worker_started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    worker_thread = threading.Thread(target=worker.main, name="ambulance-worker", daemon=True)
    worker_thread.start()


def main() -> None:
    start_worker_thread()
    host = os.getenv("WORKER_PANEL_HOST", "127.0.0.1")
    port = int(os.getenv("WORKER_PANEL_PORT", "8090"))
    if os.getenv("WORKER_PANEL_OPEN_BROWSER", "true").strip().lower() not in {"0", "false", "no", "off"}:
        threading.Timer(1.0, lambda: webbrowser.open_new_tab(f"http://{host}:{port}/")).start()
    print(f"[worker_panel] serving http://{host}:{port}/", flush=True)
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
