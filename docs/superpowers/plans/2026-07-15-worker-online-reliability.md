# 救護 Worker 在線與遠端更新可靠性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓公務電腦 Worker 以低負載的單一 control request 維持可驗證在線、及時收取遠端更新命令，並在 Worker thread、GUI 或更新交易異常時安全復原。

**Architecture:** 新增三個小型 Python 模組：`worker_health.py` 管本機原子狀態與純判定、`worker_routes.py` 管 NAS instance identity 與受限 failover、`worker_control.py` 管單一 10 秒 control loop。NAS 的 `POST /worker/control` 同時保存心跳、回傳身分和更新命令；GUI supervisor 與 PowerShell watchdog 只依精確的活動、交易與程序身分介入。

**Tech Stack:** Python 3 / Flask / Tkinter / `unittest` / PowerShell 5.1 / Windows Task Scheduler / JSON 原子替換 / 現有 Worker Token API。

## Global Constraints

- 只編輯 `WinPython_公務電腦使用包` 作為執行來源；不得手改 `UPDATE/NAS包` 或發行 ZIP 內容。
- 不得觸碰未追蹤的 `.codex/`、`NAS包(舊版)/`、`docs/superpowers/plans/2026-06-29-nas-entry-implementation.md`，也不得 stage 現有非本計畫的工作區變更。
- 公務電腦只可主動呼叫 NAS；不建立 Windows Service、WinRM、RDP、NAS 主動連入或 Windows 重開機。
- 只允許啟動或停止目前套件目錄下、PID/啟動時間/完整路徑全部吻合的 Worker GUI、Worker Python 或 `REMOTE_UPDATE_PACKAGE.ps1` owner；絕不停止 Chrome、ChromeDriver、其他 Python、其他 PowerShell 或套件外程序。
- 單一 control request 每 10 秒一次，啟動時有 0–1 秒 jitter；NAS 只保留每台 Worker 最新心跳，不保存逐次心跳歷史，也不為成功心跳寫逐筆診斷。
- Worker control heartbeat 允許的 state 僅為 `starting`、`idle`、`busy`、`update_handoff`、`recovering`、`stopping`；NAS 收到時間 45 秒內為 online，本機 watchdog 120 秒以上才視為 stale。
- GUI supervisor 每 5 秒檢查、thread 停止滿 15 秒才可重啟、10 分鐘最多 3 次；Windows watchdog 每分鐘一次、10 分鐘最多 3 次破壞性復原。
- 有新鮮手動勤務鎖、案件查詢 activity lease、可驗證的更新 owner 或被持有的 `package-update.lock` 時，所有復原流程只能回報診斷，不得重啟。
- 更新 marker phase 僅可為 `discovering_runtime`、`installing`、`validating`、`committing`、`rolling_back`、`restarting`；phase 超過 10 分鐘才可判定 stale。
- 不把 token、帳密、任務 payload、完整 command line、Chrome profile、截圖或 logs 寫入 Git、release asset、NAS heartbeat 或測試 fixture。
- 每個行為變更一律先寫會失敗的 `unittest`，再做最小實作、跑相關回歸，最後只提交該任務檔案。

---

## File Structure

| 檔案 | 責任 |
| --- | --- |
| `WinPython_公務電腦使用包/ambulance_bot/worker_health.py` | 本機 health/activity/mailbox JSON、原子寫入與 GUI restart 純判定。 |
| `WinPython_公務電腦使用包/ambulance_bot/worker_routes.py` | NAS instance identity、已驗證路徑選擇與只對傳輸錯誤的 failover。 |
| `WinPython_公務電腦使用包/ambulance_bot/worker_control.py` | 10 秒 control loop、最新命令 mailbox 與狀態回報節流。 |
| `WinPython_公務電腦使用包/worker.py` | 啟停 control loop、活動快照、從 mailbox 安全啟動既有更新 wrapper。 |
| `WinPython_公務電腦使用包/app.py` | identity/control API、NAS heartbeat storage、remote-command unlocked helpers、後台 view。 |
| `WinPython_公務電腦使用包/templates/admin_public_pc.html` | NAS 版本、心跳、最後任務回報與路徑診斷的分離呈現。 |
| `WinPython_公務電腦使用包/worker_gui.py` | route bootstrap、Worker thread exit 記錄與 GUI 自我 supervisor。 |
| `WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1` | 完整 owner identity、phase heartbeat 和安全清除 marker。 |
| `WinPython_公務電腦使用包/WORKER_SELF_RECOVERY.ps1` | 每分鐘 fail-closed watchdog，支援 `-WhatIf` snapshot 測試。 |
| `WinPython_公務電腦使用包/install_startup_shortcut.ps1` | 安裝／移除登入 task 和 watchdog task。 |
| `scripts/build_public_duty_package.ps1` | 封裝 watchdog，並同步產生與來源一致的 installer。 |
| `tests/test_worker_health.py`、`tests/test_worker_routes.py`、`tests/test_worker_control.py`、`tests/test_worker_self_recovery.py` | 新模組與安全矩陣。 |
| `tests/test_worker.py`、`tests/test_worker_gui.py`、`tests/test_web_app.py`、`tests/test_update_package_integration.py` | 既有流程的回歸與 package contract。 |

---

### Task 1: 建立本機 health、activity 與 supervisor 純函式

**Files:**
- Create: `WinPython_公務電腦使用包/ambulance_bot/worker_health.py`
- Create: `tests/test_worker_health.py`

**Interfaces:**

```python
HEARTBEAT_STATES: frozenset[str]

@dataclass(frozen=True)
class GuiRestartDecision:
    should_restart: bool
    reason: str
    retained_restart_times: tuple[float, ...]

def state_root() -> Path
def worker_heartbeat_path() -> Path
def worker_activity_path() -> Path
def worker_control_mailbox_path() -> Path
def self_recovery_state_path() -> Path
def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None
def build_heartbeat(*, worker_id: str, package_version: str, pid: int,
                    state: str, execution_mode: str, package_path: str,
                    activity: str = "", busy_reason: str = "",
                    request_id: str = "", observed_at: datetime | None = None) -> dict[str, object]
def write_activity(*, activity: str, owner: str, observed_at: datetime | None = None) -> None
def clear_activity(owner: str) -> bool
def activity_is_fresh(max_age_seconds: float, now: datetime | None = None) -> bool
def decide_gui_restart(*, now_monotonic: float, thread_alive: bool,
                       stopped_at: float | None, activity_active: bool,
                       update_active: bool, restart_times: Sequence[float],
                       grace_seconds: float = 15.0,
                       window_seconds: float = 600.0,
                       max_restarts: int = 3) -> GuiRestartDecision
```

- [ ] **Step 1: 寫 health 與原子 JSON 的失敗測試**

  在 `tests/test_worker_health.py` 加入下列測試。所有測試都用 `tempfile.TemporaryDirectory()` 與 `mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp})`，不得寫真實使用者資料夾。

  ```python
  def test_build_heartbeat_rejects_unknown_state_and_writes_allowlisted_fields(self):
      with self.assertRaises(ValueError):
          worker_health.build_heartbeat(
              worker_id="PC-01", package_version="2026.07.15.1326", pid=123,
              state="online", execution_mode="gui", package_path="C:/package",
          )
      payload = worker_health.build_heartbeat(
          worker_id="PC-01", package_version="2026.07.15.1326", pid=123,
          state="idle", execution_mode="gui", package_path="C:/package",
      )
      self.assertEqual(payload["state"], "idle")
      self.assertNotIn("token", payload)

  def test_atomic_write_replaces_complete_json_without_tmp_residue(self):
      path = worker_health.worker_heartbeat_path()
      worker_health.write_json_atomic(path, {"sequence": 1})
      worker_health.write_json_atomic(path, {"sequence": 2})
      self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["sequence"], 2)
      self.assertEqual(list(path.parent.glob("*.tmp")), [])
  ```

- [ ] **Step 2: 寫 activity owner 與 GUI restart 決策的失敗測試**

  ```python
  def test_activity_clear_only_removes_matching_owner(self):
      worker_health.write_activity(activity="case_lookup", owner="owner-a")
      self.assertFalse(worker_health.clear_activity("owner-b"))
      self.assertTrue(worker_health.activity_is_fresh(120))
      self.assertTrue(worker_health.clear_activity("owner-a"))
      self.assertFalse(worker_health.activity_is_fresh(120))

  def test_gui_restart_decision_has_grace_busy_guard_and_rate_limit(self):
      now = 1_000.0
      self.assertFalse(worker_health.decide_gui_restart(
          now_monotonic=now, thread_alive=False, stopped_at=now - 14,
          activity_active=False, update_active=False, restart_times=[]).should_restart)
      self.assertFalse(worker_health.decide_gui_restart(
          now_monotonic=now, thread_alive=False, stopped_at=now - 16,
          activity_active=True, update_active=False, restart_times=[]).should_restart)
      limited = worker_health.decide_gui_restart(
          now_monotonic=now, thread_alive=False, stopped_at=now - 16,
          activity_active=False, update_active=False,
          restart_times=[900.0, 950.0, 990.0])
      self.assertEqual(limited.reason, "restart_rate_limited")
      self.assertFalse(limited.should_restart)
  ```

- [ ] **Step 3: 確認測試為 RED**

  Run: `py -m unittest tests.test_worker_health -v`

  Expected: import error for `ambulance_bot.worker_health`.

- [ ] **Step 4: 實作純本機模組**

  建立 state root 為 `Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AmbulanceReturnBot"`。原子寫入必須在目標同目錄建立唯一 `.tmp`、`flush()`、`os.fsync()`、`os.replace()`，並在 `finally` 清除 temp。activity payload 至少有 `activity`、`owner`、`updated_at`；`clear_activity()` 只能在 owner 完全一致時刪除。restart 決策必須是純函式，不讀 process、不 sleep、不修改檔案。

  ```python
  def decide_gui_restart(...):
      retained = tuple(value for value in restart_times if now_monotonic - value <= window_seconds)
      if thread_alive:
          return GuiRestartDecision(False, "thread_alive", retained)
      if stopped_at is None or now_monotonic - stopped_at < grace_seconds:
          return GuiRestartDecision(False, "within_grace", retained)
      if activity_active:
          return GuiRestartDecision(False, "activity_active", retained)
      if update_active:
          return GuiRestartDecision(False, "update_active", retained)
      if len(retained) >= max_restarts:
          return GuiRestartDecision(False, "restart_rate_limited", retained)
      return GuiRestartDecision(True, "safe_to_restart", retained)
  ```

- [ ] **Step 5: 確認 GREEN 並跑格式回歸**

  Run:

  ```powershell
  py -m unittest tests.test_worker_health -v
  py -m py_compile WinPython_公務電腦使用包/ambulance_bot/worker_health.py
  ```

  Expected: all tests pass and `py_compile` exits 0.

- [ ] **Step 6: 提交 Task 1**

  ```powershell
  git add -- WinPython_公務電腦使用包/ambulance_bot/worker_health.py tests/test_worker_health.py
  git commit -m "feat: add worker health primitives"
  ```

---

### Task 2: 建立 NAS identity、單一 control API 與後台 health view

**Files:**
- Modify: `WinPython_公務電腦使用包/app.py:94-122, 625-653, 727-925, 1067-1077, 1241-1366`
- Modify: `WinPython_公務電腦使用包/templates/admin_public_pc.html:88-150, 255-294`
- Modify: `tests/test_web_app.py:54-105, 2720-3135`

**Interfaces:**

```python
WORKER_HEARTBEAT_ONLINE_SECONDS = 45
WORKER_HEARTBEAT_STATES = frozenset({"starting", "idle", "busy", "update_handoff", "recovering", "stopping"})

def worker_server_identity_file() -> Path
def worker_server_identity() -> dict[str, str]
def worker_heartbeat_file() -> Path
def _normalize_worker_control_payload(data: object) -> dict[str, object]
def _upsert_worker_heartbeat_unlocked(data: Mapping[str, object], received_at: datetime) -> dict[str, object]
def _claim_remote_update_command_unlocked(worker_id: str, package_version: str, *, allow_claim: bool) -> tuple[dict[str, object] | None, str]
def worker_heartbeat_admin_view(reports: Sequence[Mapping[str, object]], now: datetime | None = None) -> dict[str, object]

GET /worker/identity -> {"ok": true, "server": {"instance_id", "version", "deployment"}}
POST /worker/control -> {"ok", "received_at", "server", "heartbeat", "command", "command_delivery"}
```

- [ ] **Step 1: 寫 identity 與 control schema 的失敗測試**

  在 `WebAppTests` 加入 helper，且每個資料夾仍使用既有 `setUp()` 的 temporary `app_module.artifacts_dir`。

  ```python
  def post_worker_control(self, payload: dict, token: str = "test-token"):
      return self.client.post(
          "/worker/control", headers={"X-Worker-Token": token}, json=payload,
      )

  def _valid_control_payload(self, *, route: dict | None = None) -> dict:
      return {
          "worker_id": "PC-01", "package_version": "2026.07.15.1326", "pid": 321,
          "process_started_at": "2026-07-15T15:00:00", "execution_mode": "gui",
          "package_path": "C:/Ambulance/WinPython_公務電腦使用包", "state": "idle",
          "activity": "", "busy_reason": "",
          "route": route or {"name": "lan", "identity_status": "unverified", "instance_id": ""},
      }

  def test_worker_identity_requires_token_and_is_stable(self):
      self.assertEqual(self.client.get("/worker/identity").status_code, 403)
      os.environ["WORKER_TOKEN"] = "test-token"
      first = self.client.get("/worker/identity", headers={"X-Worker-Token": "test-token"}).get_json()
      second = self.client.get("/worker/identity", headers={"X-Worker-Token": "test-token"}).get_json()
      self.assertEqual(first["server"]["instance_id"], second["server"]["instance_id"])
      self.assertNotEqual(first["server"]["instance_id"], "")

  def test_worker_control_requires_token_and_valid_schema(self):
      self.assertEqual(self.post_worker_control({}).status_code, 403)
      os.environ["WORKER_TOKEN"] = "test-token"
      self.assertEqual(self.post_worker_control({"state": "online"}).status_code, 400)
      response = self.post_worker_control(self._valid_control_payload(route={
          "name": "lan", "identity_status": "verified", "instance_id": "will-be-replaced",
      }))
      self.assertEqual(response.status_code, 200)
      self.assertNotEqual(response.get_json()["server"]["instance_id"], "will-be-replaced")
  ```

- [ ] **Step 2: 寫 command claim、route guard 與 online view 的失敗測試**

  ```python
  def test_worker_control_claims_only_verified_current_instance(self):
      os.environ["WORKER_TOKEN"] = "test-token"
      self.post_remote_update()
      server = self.client.get("/worker/identity", headers={"X-Worker-Token": "test-token"}).get_json()["server"]
      payload = self._valid_control_payload(route={
          "name": "tailscale", "identity_status": "verified", "instance_id": server["instance_id"],
      })
      response = self.post_worker_control(payload)
      self.assertEqual(response.get_json()["command_delivery"], "claimed")
      self.assertEqual(response.get_json()["command"]["worker_id"], "PC-01")

  def test_worker_control_keeps_heartbeat_but_refuses_unverified_command_claim(self):
      os.environ["WORKER_TOKEN"] = "test-token"
      self.post_remote_update()
      response = self.post_worker_control(self._valid_control_payload(route={
          "name": "lan", "identity_status": "unverified", "instance_id": "",
      }))
      self.assertIsNone(response.get_json()["command"])
      self.assertEqual(response.get_json()["command_delivery"], "unverified_route")
      self.assertEqual(app_module.worker_heartbeat_admin_view([])["worker_id"], "PC-01")

  def test_worker_heartbeat_admin_view_uses_server_received_45_second_threshold(self):
      now = datetime(2026, 7, 15, 16, 0, 0)
      app_module._upsert_worker_heartbeat_unlocked(self._valid_control_payload(), now - timedelta(seconds=44))
      self.assertTrue(app_module.worker_heartbeat_admin_view([], now=now)["online"])
      self.assertFalse(app_module.worker_heartbeat_admin_view([], now=now + timedelta(seconds=2))["online"])
  ```

- [ ] **Step 3: 確認 RED**

  Run:

  ```powershell
  py -m unittest tests.test_web_app.WebAppTests.test_worker_identity_requires_token_and_is_stable tests.test_web_app.WebAppTests.test_worker_control_claims_only_verified_current_instance tests.test_web_app.WebAppTests.test_worker_heartbeat_admin_view_uses_server_received_45_second_threshold -v
  ```

  Expected: FAIL/ERROR because the identity/control routes and view do not exist.

- [ ] **Step 4: 實作 storage、unlocked helpers 與 API**

  `worker_server_identity()` 在 `artifacts/public_pc/worker_server_identity.json` 不存在時產生 UUID，之後永遠讀同一檔。它只回傳 `instance_id`、`version=package_version()`、`deployment="ambulance_return_bot_nas"`。所有會在 `_public_pc_report_lock` 中呼叫的 helper 必須是 `*_unlocked`；不要從 control route 內呼叫 `read_remote_update_command()` 或 `worker_server_identity()` 這種再次鎖定的公開函式。

  ```python
  @app.post("/worker/control")
  def worker_control():
      if not worker_authorized():
          abort(403)
      payload = _normalize_worker_control_payload(request.get_json(silent=True))
      received_at = datetime.now()
      with _public_pc_report_lock:
          server = _worker_server_identity_unlocked()
          heartbeat = _upsert_worker_heartbeat_unlocked(payload, received_at)
          route = payload["route"]
          command, delivery = _claim_remote_update_command_unlocked(
              str(payload["worker_id"]), str(payload["package_version"]),
              allow_claim=(
                  route["identity_status"] == "verified"
                  and route["instance_id"] == server["instance_id"]
              ),
          )
      return jsonify({
          "ok": True, "received_at": received_at.isoformat(timespec="seconds"),
          "server": server, "heartbeat": heartbeat,
          "command": command, "command_delivery": delivery,
      })
  ```

  `_normalize_worker_control_payload()` 必須做 allowlist、trim、型別與長度驗證：worker ID/package version/execution mode/path/state/activity/busy reason/request ID/route name/route status/route instance ID；`pid` 必須正整數；未知 state、list payload、空 worker ID、非 dict route 一律 `abort(400)`。server 只保存 allowlist 資料和 `received_at`，不可保存 client `online`、token 或任務資料。

  將現有 GET command claim 的內容移入 `_claim_remote_update_command_unlocked()`；舊 `/worker/remote-update` 仍呼叫同一 helper 並保持原先 token/owner/idempotence 行為。從 status POST 抽出 `_apply_remote_update_status_unlocked()`，使 control request 有附帶 `remote_update` 時可安全更新 `waiting_busy`/`waiting_idle`，但 stale/foreign status 只回傳 control 診斷，不讓 heartbeat 失敗。

- [ ] **Step 5: 實作管理頁**

  `admin_public_pc()` 傳入 `worker_health=worker_heartbeat_admin_view(reports)`。`worker_admin_version_info()` 只表示 NAS backend version；template 加獨立 health card。remote update 的版本文字規則如下：在線時顯示 heartbeat version；離線時顯示「最後心跳版本」；完全沒有 heartbeats 時顯示「尚未收到心跳」，不得 fallback 成 NAS version。

  ```jinja2
  <section class="worker-health-card {{ 'online' if worker_health.online else 'offline' }}" aria-label="公務電腦心跳">
    <div class="worker-health-title">公務電腦心跳</div>
    <span class="status {{ worker_health.status_class }}">{{ worker_health.status_label }}</span>
    <div class="meta">最後心跳：{{ worker_health.last_seen_at or '尚未收到' }}</div>
    <div class="meta">心跳版本：{{ worker_health.package_version or '未標示' }}</div>
    <div class="meta">最後任務回報：{{ worker_health.last_task_report_version or '尚未回報' }}</div>
    <div class="meta">路徑：{{ worker_health.route_label }}</div>
  </section>
  ```

- [ ] **Step 6: 確認 GREEN 與 Web 回歸**

  Run:

  ```powershell
  py -m unittest tests.test_web_app.WebAppTests.test_worker_identity_requires_token_and_is_stable tests.test_web_app.WebAppTests.test_worker_control_requires_token_and_valid_schema tests.test_web_app.WebAppTests.test_worker_control_claims_only_verified_current_instance tests.test_web_app.WebAppTests.test_worker_heartbeat_admin_view_uses_server_received_45_second_threshold tests.test_web_app.WebAppTests.test_admin_public_pc_separates_nas_heartbeat_and_task_report_versions -v
  py -m unittest tests.test_web_app.WebAppTests.test_remote_update_command_is_idempotent_and_worker_authenticated tests.test_web_app.WebAppTests.test_remote_update_command_is_owned_by_first_worker_that_claims_it tests.test_web_app.WebAppTests.test_remote_update_status_requires_token_and_valid_transition -v
  ```

  Expected: all pass; legacy GET/POST update behavior is unchanged.

- [ ] **Step 7: 提交 Task 2**

  ```powershell
  git add -- WinPython_公務電腦使用包/app.py WinPython_公務電腦使用包/templates/admin_public_pc.html tests/test_web_app.py
  git commit -m "feat: add worker control heartbeat API"
  ```

---

### Task 3: 實作 NAS 身分驗證與只對傳輸錯誤 failover 的 route client

**Files:**
- Create: `WinPython_公務電腦使用包/ambulance_bot/worker_routes.py`
- Create: `tests/test_worker_routes.py`
- Modify: `WinPython_公務電腦使用包/worker_gui.py:52-53, 630-666, 1589-1602`
- Modify: `tests/test_worker_gui.py:334-345`

**Interfaces:**

```python
@dataclass(frozen=True)
class ServerIdentity:
    base_url: str
    instance_id: str
    version: str
    deployment: str

@dataclass(frozen=True)
class RouteChoice:
    primary_url: str
    fallback_url: str
    route_name: str
    identity_status: str  # "verified" or "unverified"
    instance_id: str
    diagnostic: str

class WorkerControlClient:
    def __init__(self, choice: RouteChoice, *, request_json: Callable[..., dict[str, object]], post_json: Callable[..., dict[str, object]]) -> None
    def control(self, payload: Mapping[str, object]) -> dict[str, object]

def fetch_server_identity(base_url: str, request_json: Callable[[str], dict[str, object]]) -> ServerIdentity
def known_server_identity_path() -> Path
def load_known_server_identity() -> str
def remember_known_server_identity(instance_id: str) -> None
def choose_verified_route(primary_url: str, fallback_url: str, *, fetch_identity: Callable[[str], ServerIdentity], known_instance_id: str = "") -> RouteChoice
def is_transport_failure(exc: BaseException) -> bool
```

- [ ] **Step 1: 寫 pure route-choice 的失敗測試**

  ```python
  def test_choose_verified_route_prefers_lan_only_when_instances_match(self):
      identities = {
          "http://10.30.65.30:8080": ServerIdentity("http://10.30.65.30:8080", "same", "v", "nas"),
          "http://100.114.126.58:8080": ServerIdentity("http://100.114.126.58:8080", "same", "v", "nas"),
      }
      choice = worker_routes.choose_verified_route(
          "http://10.30.65.30:8080", "http://100.114.126.58:8080",
          fetch_identity=identities.__getitem__,
      )
      self.assertEqual(choice.route_name, "lan")
      self.assertEqual(choice.identity_status, "verified")

  def test_choose_verified_route_rejects_mismatched_lan_identity(self):
      def fetch(url):
          return ServerIdentity(url, "old" if "10.30" in url else "live", "v", "nas")
      choice = worker_routes.choose_verified_route(
          "http://10.30.65.30:8080", "http://100.114.126.58:8080", fetch_identity=fetch,
      )
      self.assertEqual(choice.primary_url, "http://100.114.126.58:8080")
      self.assertIn("mismatch", choice.diagnostic)

  def test_single_reachable_route_is_verified_only_when_it_matches_local_identity(self):
      only_lan = lambda url: ServerIdentity(url, "known", "v", "nas") if url == "http://lan" else (_ for _ in ()).throw(TimeoutError())
      verified = worker_routes.choose_verified_route("http://lan", "http://tail", fetch_identity=only_lan, known_instance_id="known")
      unverified = worker_routes.choose_verified_route("http://lan", "http://tail", fetch_identity=only_lan, known_instance_id="")
      self.assertEqual(verified.identity_status, "verified")
      self.assertEqual(unverified.identity_status, "unverified")
  ```

- [ ] **Step 2: 寫 control failover 的失敗測試**

  ```python
  def test_control_client_uses_verified_fallback_only_for_transport_failure(self):
      choice = RouteChoice("http://lan", "http://tail", "lan", "verified", "same", "")
      calls = []
      def post(url, _payload):
          calls.append(url)
          if url == "http://lan/worker/control":
              raise urllib.error.URLError("network down")
          return {"ok": True, "server": {"instance_id": "same"}}
      client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)
      self.assertTrue(client.control({"state": "idle"})["ok"])
      self.assertEqual(calls, ["http://lan/worker/control", "http://tail/worker/control"])

  def test_control_client_does_not_mask_http_403_with_fallback(self):
      choice = RouteChoice("http://lan", "http://tail", "lan", "verified", "same", "")
      post = mock.Mock(side_effect=RuntimeError("NAS worker API 回應 HTTP 403：FORBIDDEN"))
      client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)
      with self.assertRaisesRegex(RuntimeError, "403"):
          client.control({"state": "idle"})
      self.assertEqual(post.call_count, 1)
  ```

- [ ] **Step 3: 確認 RED**

  Run: `py -m unittest tests.test_worker_routes -v`

  Expected: import error for `ambulance_bot.worker_routes`.

- [ ] **Step 4: 實作 route module**

  `fetch_server_identity()` 只接受 `{"ok": true, "server": {"instance_id", "version", "deployment"}}` 的嚴格 schema；欄位缺失、instance ID 空白、HTTP 404/403 均視為不可驗證。`known_server_identity_path()` 使用 Task 1 state root 的 `worker_server_identity.json`；它只保存非敏感 instance ID。`remember_known_server_identity()` 只接受非空 UUID-like value 並用 Task 1 原子寫入。`choose_verified_route()` 同時探測已設定 primary 和另一候選；兩端 instance 相同時維持 LAN 優先，instance 不同時只選 Tailscale，單端可達時僅在與 local known instance ID 一致時標示 `verified`，否則標示 `unverified`。

  `WorkerControlClient.control()` 只在 `urllib.error.URLError`、`TimeoutError`、`ConnectionError` 或訊息代表 timeout/connection refused 的例外嘗試 verified fallback；收到 response 後再次比較 `response["server"]["instance_id"]` 與 choice ID，任何不符都 raise `RuntimeError("NAS instance identity mismatch")`。它不可 fallback HTTP 4xx 或 JSON/schema 錯誤。

- [ ] **Step 5: 接到 GUI route bootstrap**

  將 `choose_worker_server()` 改為呼叫 `choose_verified_route(..., known_instance_id=load_known_server_identity())`，並在 verified choice 後呼叫 `remember_known_server_identity(choice.instance_id)`；保留原本兩個固定候選和手動設定的非候選 URL。成功 choice 需要在 GUI 中設定：

  ```python
  os.environ["WORKER_SERVER_URL"] = choice.primary_url
  os.environ["WORKER_SERVER_FALLBACK_URL"] = choice.fallback_url
  os.environ["WORKER_SERVER_INSTANCE_ID"] = choice.instance_id
  os.environ["WORKER_SERVER_IDENTITY_STATUS"] = choice.identity_status
  ```

  `worker_gui` 在 route mismatch/unverified 時顯示明確文字，但仍保留既有已設定 URL 的一般勤務功能；control loop 會帶 `identity_status="unverified"`，NAS 因此不會 claim 遠端更新命令。

- [ ] **Step 6: 確認 GREEN 與 GUI route 回歸**

  Run:

  ```powershell
  py -m unittest tests.test_worker_routes -v
  py -m unittest tests.test_worker_gui.WorkerGuiEnvTests.test_initial_worker_server_prefers_lan_for_known_urls tests.test_worker_gui.WorkerGuiEnvTests.test_choose_worker_server_falls_back_to_tailscale -v
  ```

  Expected: route tests pass; GUI tests are updated to assert verified LAN preference and mismatched-LAN rejection.

- [ ] **Step 7: 提交 Task 3**

  ```powershell
  git add -- WinPython_公務電腦使用包/ambulance_bot/worker_routes.py WinPython_公務電腦使用包/worker_gui.py tests/test_worker_routes.py tests/test_worker_gui.py
  git commit -m "feat: verify worker NAS route identity"
  ```

---

### Task 4: 建立單一 10 秒 Worker control loop 並讓更新命令不受長工作阻塞

**Files:**
- Create: `WinPython_公務電腦使用包/ambulance_bot/worker_control.py`
- Create: `tests/test_worker_control.py`
- Modify: `WinPython_公務電腦使用包/worker.py:90-159, 169-181, 443-605, 1187-1203, 2130-2150, 2517-2519`
- Modify: `tests/test_worker.py:851-1189, 1672-1745`

**Interfaces:**

```python
@dataclass(frozen=True)
class RuntimeSnapshot:
    state: str
    activity: str
    busy_reason: str
    request_id: str

class WorkerRuntimeState:
    def set(self, state: str, *, activity: str = "", busy_reason: str = "", request_id: str = "") -> None
    def snapshot(self) -> RuntimeSnapshot

class WorkerControlLoop:
    def __init__(self, *, client: WorkerControlClient, worker_id: str,
                 package_version: Callable[[], str], package_path: Callable[[], str],
                 execution_mode: Callable[[], str], snapshot: Callable[[], RuntimeSnapshot],
                 mailbox_path: Path, interval_seconds: float = 10.0,
                 status_refresh_seconds: float = 60.0) -> None
    def start(self) -> None
    def stop(self, timeout_seconds: float = 2.0) -> None
    def run_once(self) -> dict[str, object] | None
    def pending_command(self) -> dict[str, object] | None
    def clear_command(self, request_id: str) -> bool

def worker_control_interval_seconds() -> float
def build_worker_control_loop(server_url: str, worker_id: str, artifacts_dir: Path,
                              runtime_state: WorkerRuntimeState) -> WorkerControlLoop
def remote_update_marker_is_healthy(request_id: str | None = None, *, max_age_seconds: float = 600.0) -> bool
def maybe_start_remote_update(server_url: str, worker_id: str, artifacts_dir: Path,
                              command: Mapping[str, object], *, idle_seconds: Callable[[], float] | None = None,
                              launch_update: Callable[[str], None] | None = None,
                              active_update_check: Callable[[str], bool] | None = None) -> bool
```

- [ ] **Step 1: 寫 control loop 的失敗測試**

  先在新的 `WorkerControlTests` 建立兩個 test helper；它們只組裝受測物，不啟動真實 NAS 或 browser。

  ```python
  def _loop(self, tmp: str, *, client: mock.Mock, interval_seconds: float = 10.0) -> worker_control.WorkerControlLoop:
      self.env_patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False)
      self.env_patch.start()
      self.addCleanup(self.env_patch.stop)
      snapshot = worker_control.RuntimeSnapshot("idle", "", "", "")
      return worker_control.WorkerControlLoop(
          client=client, worker_id="PC-01", package_version=lambda: "2026.07.15.1326",
          package_path=lambda: "C:/Ambulance/WinPython_公務電腦使用包",
          execution_mode=lambda: "gui", snapshot=lambda: snapshot,
          mailbox_path=worker_health.worker_control_mailbox_path(), interval_seconds=interval_seconds,
      )

  def _run_main_once(self) -> None:
      with mock.patch.dict(os.environ, {"WORKER_RUN_ONCE": "true", "WORKER_AUTO_CLAIM_TASKS": "false"}, clear=False), \
           mock.patch.object(worker_module, "wait_for_update_probe_gate", return_value="none"), \
           mock.patch.object(worker_module, "maybe_recover_interrupted_update", return_value=False), \
           mock.patch.object(worker_module, "flush_status_outbox"), \
           mock.patch.object(worker_module, "report_remote_update_result", return_value=False), \
           mock.patch.object(worker_module, "maybe_run_credential_sync"):
          worker_module.main()

  def test_control_loop_writes_heartbeat_before_single_control_request_and_persists_command(self):
      with tempfile.TemporaryDirectory() as tmp:
          client = mock.Mock()
          client.control.return_value = {
              "ok": True, "server": {"instance_id": "nas-a"},
              "command": {"request_id": "update-1", "status": "pending"},
          }
          loop = self._loop(tmp, client=client)
          result = loop.run_once()
          self.assertEqual(result["command"]["request_id"], "update-1")
          self.assertEqual(loop.pending_command()["request_id"], "update-1")
          self.assertTrue(worker_health.worker_heartbeat_path().exists())
          client.control.assert_called_once()

  def test_control_loop_survives_network_failure_and_stop_interrupts_wait(self):
      with tempfile.TemporaryDirectory() as tmp:
          client = mock.Mock()
          client.control.side_effect = urllib.error.URLError("offline")
          loop = self._loop(tmp, client=client, interval_seconds=60.0)
          loop.start()
          loop.stop(timeout_seconds=0.5)
          self.assertFalse(loop._thread.is_alive())
  ```

- [ ] **Step 2: 寫長案件查詢與更新安全 gate 的失敗測試**

  ```python
  def test_main_starts_control_before_case_lookup_and_uses_mailbox_after_work_is_safe(self):
      calls = []
      fake_control = SimpleNamespace(
          start=lambda: calls.append("control_start"),
          stop=lambda **_kwargs: calls.append("control_stop"),
          pending_command=lambda: {"request_id": "update-1", "status": "pending"},
          clear_command=lambda _request_id: calls.append("clear_command") or True,
      )
      with mock.patch.object(worker_module, "build_worker_control_loop", return_value=fake_control), \
           mock.patch.object(worker_module, "maybe_run_case_lookup", side_effect=lambda *_args: calls.append("lookup") or _args[2:4]), \
           mock.patch.object(worker_module, "maybe_start_remote_update", side_effect=lambda *_args, **_kwargs: calls.append("start_update") or False):
          self._run_main_once()
      self.assertLess(calls.index("control_start"), calls.index("lookup"))
      self.assertIn("start_update", calls)
      self.assertIn("control_stop", calls)

  def test_maybe_start_remote_update_waits_when_activity_is_busy(self):
      statuses, launches = [], []
      with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
          worker_health.write_activity(activity="case_lookup", owner="lookup-owner")
          started = worker_module.maybe_start_remote_update(
              "http://nas", "PC-01", Path(tmp), {"request_id": "update-1", "status": "pending"},
              idle_seconds=lambda: 999.0, launch_update=launches.append,
          )
      self.assertFalse(started)
      self.assertEqual(launches, [])
  ```

- [ ] **Step 3: 確認 RED**

  Run:

  ```powershell
  py -m unittest tests.test_worker_control -v
  py -m unittest tests.test_worker.WorkerTests.test_main_starts_control_before_case_lookup_and_uses_mailbox_after_work_is_safe -v
  ```

  Expected: import/attribute errors for `worker_control`, `build_worker_control_loop`, and `maybe_start_remote_update`.

- [ ] **Step 4: 實作 control loop 與 mailbox**

  `WorkerRuntimeState` 以 `threading.Lock` 保護 `set()`/`snapshot()`；它拒絕 Task 1 allowlist 外 state。`WorkerControlLoop.run_once()` 先從 shared `RuntimeSnapshot` 產生 heartbeat，立即用 Task 1 原子寫入本機；再呼叫一次 `WorkerControlClient.control()`。成功 response 的 command 只有含非空 request ID 且 dict 時才寫 mailbox。mailbox 必須只保存 command allowlist、`received_at` 和 route identity，不保存 token。重複 request ID 覆寫同一 mailbox；terminal command 或 `clear_command()` 才移除。

  `start()` 建立 daemon thread；thread 使用 `stop_event.wait(interval_seconds + jitter)` 而不是 `time.sleep()`，因此 `stop()` 可在 2 秒內 join。network/status 錯誤只記 `print(..., flush=True)` 並等待下一輪，不可讓主 Worker return。

  Control payload 必須包含最新 `RuntimeSnapshot`；當命令已收到而活動 busy 或 idle 未達標時，payload 的 `remote_update` 帶現有 `waiting_busy` 或 `waiting_idle`。同一 request/status/detail 在 60 秒內不得重複上傳，避免 NAS 寫入放大。

- [ ] **Step 5: 整合到 `worker.main()`**

  取得 `server_url`、`worker_id`、`artifacts_dir` 後，先完成既有 update recovery gates，接著建立 control loop、設定 `starting` 並啟動。主 `while` 保留既有 credential sync、case lookup、自動接案順序；每個可能長時間執行的區段之前/之後刷新 Task 1 activity owner，使 control loop 在它執行期間回報 `busy`。manual task 保留既有 cross-process lock，snapshot 必須同時檢查 `MANUAL_TASK_ACTIVE`、`manual_task_lock_active(artifacts_dir)`、新鮮 `worker_activity.json` 與 active update marker。

  這一任務先實作 `remote_update_marker_is_healthy()` 的既有-schema 版本：PID、tight start-time fence、nonce、request ID、current package root 和 mtime 都要吻合；Task 6 再將它收緊為 full phase identity。將原 `maybe_run_remote_update()` 改為保留舊呼叫相容的薄 wrapper；新主流程只呼叫 `maybe_start_remote_update(..., control.pending_command())`。它不得 fetch command；安全條件通過後才 post/inline `updating`、啟動 wrapper，成功啟動時把 runtime state 設為 `update_handoff`、清除對應 mailbox 並讓 `main()` return。`finally` 必須設定 `stopping`、做一次本機 heartbeat、stop/join control loop。

  ```python
  try:
      control.start()
      while True:
          # existing credential/task work remains here
          command = control.pending_command()
          if command and maybe_start_remote_update(server_url, worker_id, artifacts_dir, command):
              control.clear_command(str(command["request_id"]))
              return
          if run_once:
              return
          time.sleep(poll_seconds)
  finally:
      runtime_state.set("stopping")
      control.stop(timeout_seconds=2.0)
  ```

- [ ] **Step 6: 確認 GREEN 與 Worker 回歸**

  Run:

  ```powershell
  py -m unittest tests.test_worker_health tests.test_worker_routes tests.test_worker_control -v
  py -m unittest tests.test_worker.WorkerTests.test_remote_update_waits_for_windows_idle_without_launching tests.test_worker.WorkerTests.test_remote_update_waits_for_cross_process_task_lock tests.test_worker.WorkerTests.test_remote_update_waits_for_active_case_lookup tests.test_worker.WorkerTests.test_main_checks_remote_update_after_confirming_no_pending_work tests.test_worker.WorkerTests.test_main_runs_pending_nas_work_before_remote_update -v
  ```

  Expected: all pass; update command receipt is no longer ordered after case lookup, but actual update still obeys busy/idle gates.

- [ ] **Step 7: 提交 Task 4**

  ```powershell
  git add -- WinPython_公務電腦使用包/ambulance_bot/worker_control.py WinPython_公務電腦使用包/worker.py tests/test_worker_control.py tests/test_worker.py
  git commit -m "feat: poll worker control independently"
  ```

---

### Task 5: 讓 GUI 監督 Worker thread 並安全重啟

**Files:**
- Modify: `WinPython_公務電腦使用包/worker_gui.py:350-420, 633-666, 852-873`
- Modify: `tests/test_worker_gui.py:1-35, 696-712`

**Interfaces:**

```python
def _record_worker_thread_exit(self, error: BaseException | None = None) -> None
def _schedule_worker_supervisor(self) -> None
def _supervise_worker_thread(self) -> None
def _worker_supervisor_activity_active(self) -> bool
def _worker_supervisor_update_active(self) -> bool
```

- [ ] **Step 1: 寫正常 return 與例外退出的失敗測試**

  先將下列 helper 加入 `WorkerGuiEnvTests`；所有 GUI member 都是 `SimpleNamespace`/`Mock`，不可建立真實 Tk root。

  ```python
  def _supervisor_stub(self, **overrides):
      values = {
          "worker_thread": SimpleNamespace(is_alive=lambda: False),
          "worker_stopped_at": None, "worker_exit_error": "", "worker_restart_times": [],
          "worker_status": mock.Mock(), "log_queue": queue.Queue(), "after": mock.Mock(),
          "_restart_worker": mock.Mock(), "_log": mock.Mock(),
          "_worker_supervisor_activity_active": mock.Mock(return_value=False),
          "_worker_supervisor_update_active": mock.Mock(return_value=False),
      }
      values.update(overrides)
      return SimpleNamespace(**values)

  def test_run_worker_records_normal_return_on_gui_thread(self):
      gui = self._supervisor_stub()
      with mock.patch.object(worker_gui.worker, "main", return_value=None):
          worker_gui.WorkerGui._run_worker(gui)
      gui.after.assert_called_once()
      callback = gui.after.call_args.args[1]
      callback()
      self.assertIsNotNone(gui.worker_stopped_at)
      self.assertEqual(gui.worker_status.set.call_args.args[0], "已停止")

  def test_supervisor_restarts_only_after_safe_grace(self):
      gui = self._supervisor_stub(worker_thread=SimpleNamespace(is_alive=lambda: False))
      gui.worker_stopped_at = time.monotonic() - 16
      gui._worker_supervisor_activity_active.return_value = False
      gui._worker_supervisor_update_active.return_value = False
      worker_gui.WorkerGui._supervise_worker_thread(gui)
      gui._restart_worker.assert_called_once()
  ```

- [ ] **Step 2: 寫 busy/update/rate-limit 的失敗測試**

  ```python
  def test_supervisor_does_not_restart_during_activity_or_update(self):
      for activity, update in ((True, False), (False, True)):
          gui = self._supervisor_stub(worker_thread=SimpleNamespace(is_alive=lambda: False))
          gui.worker_stopped_at = time.monotonic() - 16
          gui._worker_supervisor_activity_active.return_value = activity
          gui._worker_supervisor_update_active.return_value = update
          worker_gui.WorkerGui._supervise_worker_thread(gui)
          gui._restart_worker.assert_not_called()

  def test_supervisor_rate_limits_fourth_restart_in_ten_minutes(self):
      gui = self._supervisor_stub(worker_thread=SimpleNamespace(is_alive=lambda: False))
      now = time.monotonic()
      gui.worker_stopped_at = now - 16
      gui.worker_restart_times = [now - 100, now - 50, now - 10]
      worker_gui.WorkerGui._supervise_worker_thread(gui)
      gui._restart_worker.assert_not_called()
      self.assertIn("過多", gui.log_queue.get_nowait())
  ```

- [ ] **Step 3: 確認 RED**

  Run: `py -m unittest tests.test_worker_gui.WorkerGuiEnvTests.test_run_worker_records_normal_return_on_gui_thread tests.test_worker_gui.WorkerGuiEnvTests.test_supervisor_restarts_only_after_safe_grace tests.test_worker_gui.WorkerGuiEnvTests.test_supervisor_rate_limits_fourth_restart_in_ten_minutes -v`

  Expected: attribute errors because supervisor methods/state are absent.

- [ ] **Step 4: 實作 GUI-only supervisor**

  在 `__init__` 建立 `worker_stopped_at: float | None`、`worker_exit_error`、`worker_restart_times: list[float]`，並在 GUI 建成後呼叫 `_schedule_worker_supervisor()`。`_run_worker()` 不可直接從 background thread 呼叫 `StringVar.set()`；它以 `self.after(0, lambda: self._record_worker_thread_exit(error))` 回到 Tk thread。每次 supervisor 結束都用 `self.after(5000, self._supervise_worker_thread)` 排下次檢查。

  `activity_active` 必須同時使用 `worker.remote_update_busy_reason(artifacts_dir)` 與 `worker_health.activity_is_fresh(120)`；`update_active` 必須只接受 `worker.remote_update_marker_is_healthy()` 的 exact marker。將這些布林值和自身 `worker_thread.is_alive()` 交給 Task 1 的 `decide_gui_restart()`。允許重啟時先 append `time.monotonic()`，再呼叫現有 `_restart_worker()`；`_restart_worker()` 本身仍拒絕 live thread。

- [ ] **Step 5: 確認 GREEN 與 GUI 全套測試**

  Run:

  ```powershell
  py -m unittest tests.test_worker_gui -v
  py -m py_compile WinPython_公務電腦使用包/worker_gui.py
  ```

  Expected: tests pass; no Tk variable is mutated from the Worker thread.

- [ ] **Step 6: 提交 Task 5**

  ```powershell
  git add -- WinPython_公務電腦使用包/worker_gui.py tests/test_worker_gui.py
  git commit -m "feat: supervise worker GUI thread"
  ```

---

### Task 6: 擴充更新 phase marker 與既有 Worker 驗證

**Files:**
- Modify: `WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1:409-443, 544-738`
- Modify: `WinPython_公務電腦使用包/worker.py:530-605`
- Modify: `tests/test_worker.py:978-1081, 1449-1622`
- Modify: `tests/test_update_package_integration.py:29-131, 228-310`

**Interfaces:**

```powershell
function Write-RemoteUpdateActiveMarker
function Set-RemoteUpdatePhase([string]$Phase, [string]$TransactionPath = "")
# Marker immutable identity: request_id, owner_pid, owner_started_unix_ms,
# owner_nonce, script_path, package_path.
# Mutable fields: transaction_path, phase, phase_started_at, phase_updated_at.
```

```python
def remote_update_marker_is_healthy(request_id: str | None = None, *, max_age_seconds: float = 600.0) -> bool
```

- [ ] **Step 1: 寫 marker schema/transition 的失敗測試**

  先在 `tests/test_worker.py` 加入 `import contextlib` 及 `from datetime import datetime, timedelta`，再在 `WorkerTests` 建立只操作 temporary `LOCALAPPDATA` 的 marker helper，確保沒有讀取真實更新檔：

  ```python
  @contextlib.contextmanager
  def _active_marker(self, overrides: dict[str, object]):
      with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
          marker = {
              "request_id": "u", "owner_pid": os.getpid(), "owner_nonce": "nonce",
              "owner_started_unix_ms": worker_module.process_start_unix_ms(os.getpid()),
              "script_path": str(Path(worker_module.__file__).with_name("REMOTE_UPDATE_PACKAGE.ps1").resolve()),
              "package_path": str(Path(worker_module.__file__).parent.resolve()),
              "phase": "validating", "phase_updated_at": datetime.now().astimezone().isoformat(),
          }
          marker.update(overrides)
          path = worker_module.remote_update_active_path()
          path.parent.mkdir(parents=True, exist_ok=True)
          path.write_text(json.dumps(marker), encoding="utf-8")
          yield

  def _old_timestamp(self, seconds: int) -> str:
      return (datetime.now().astimezone() - timedelta(seconds=seconds)).isoformat()

  def test_remote_update_marker_has_exact_owner_identity_and_phase(self):
      source = Path("WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1").read_text(encoding="utf-8")
      for key in ("script_path", "package_path", "phase", "phase_started_at", "phase_updated_at"):
          self.assertIn(key, source)
      for phase in ("discovering_runtime", "installing", "validating", "committing", "rolling_back", "restarting"):
          self.assertIn(phase, source)

  def test_remote_update_marker_health_rejects_wrong_package_or_stale_phase(self):
      with self._active_marker({"request_id": "u", "script_path": "other.ps1", "phase": "validating"}):
          self.assertFalse(worker_module.remote_update_marker_is_healthy("u"))
      with self._active_marker({"request_id": "u", "phase_updated_at": self._old_timestamp(601)}):
          self.assertFalse(worker_module.remote_update_marker_is_healthy("u"))
  ```

- [ ] **Step 2: 寫 validation heartbeat 與 rollback phase 的整合失敗測試**

  在 `UpdatePackageIntegrationTests` 的 temporary package 中維持既有 `_prepare_fixture()`，增加 assertion：validation probe loop 每輪改寫 marker 的 `phase_updated_at`，失敗 rollback 至少出現 `rolling_back`，正常完成依序出現 `installing`、`validating`、`committing`、`restarting`。不要把真實 `.env` 或 token 寫入 fixture。

- [ ] **Step 3: 確認 RED**

  Run:

  ```powershell
  py -m unittest tests.test_worker.WorkerTests.test_remote_update_marker_has_exact_owner_identity_and_phase tests.test_update_package_integration.UpdatePackageIntegrationTests.test_phase_heartbeat_advances_during_validation -v
  ```

  Expected: FAIL because current marker has only request/PID/nonce/start fields.

- [ ] **Step 4: 實作 immutable identity 與 phase helper**

  `Write-RemoteUpdateActiveMarker` 以 `$MyInvocation.MyCommand.Path` 與 `$packageDir` 的 full path 寫入 immutable identity，並以同目錄 temp + `Move-Item -Force` 原子替換。`Set-RemoteUpdatePhase` 先讀 marker、驗證 nonce/PID/request/package/script 都屬本次 run，再只更新 phase、transaction path 和 timestamps。未知 phase 直接 throw。

  將 phase 放到精確區段：取得 lock 後 `discovering_runtime`；建立 transaction 前後 `installing`；每次 probe/validation loop `validating`；完成 tree commit 前 `committing`；catch rollback `rolling_back`；任何 `Restart-WorkerRuntimes*` 前 `restarting`。marker 清除前必須確認 immutable identity 仍屬本 run。

  將 Task 4 已存在的 `remote_update_marker_is_healthy()` 收緊為 marker request ID、PID running、tight process start fence、nonce、標準化 script/package path、allowlisted phase、parseable `phase_updated_at` 且小於 600 秒作完整驗證；不能只靠 file mtime。

- [ ] **Step 5: 確認 GREEN、跑 updater 回歸與 PowerShell parser**

  Run:

  ```powershell
  py -m unittest tests.test_worker.WorkerTests.test_remote_update_active_marker_requires_matching_request_and_live_owner tests.test_worker.WorkerTests.test_remote_update_marker_has_exact_owner_identity_and_phase tests.test_update_package_integration -v
  $tokens = $null; $errors = $null
  [void][Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1'), [ref]$tokens, [ref]$errors)
  if ($errors.Count) { $errors | Format-List; exit 1 }
  ```

  Expected: tests pass; parser reports zero errors.

- [ ] **Step 6: 提交 Task 6**

  ```powershell
  git add -- WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1 WinPython_公務電腦使用包/worker.py tests/test_worker.py tests/test_update_package_integration.py
  git commit -m "fix: identify remote update phases"
  ```

---

### Task 7: 實作 fail-closed Windows watchdog 與可重現 `-WhatIf`

**Files:**
- Create: `WinPython_公務電腦使用包/WORKER_SELF_RECOVERY.ps1`
- Create: `tests/test_worker_self_recovery.py`

**Interfaces:**

```powershell
param(
    [switch]$WhatIf,
    [string]$ProcessSnapshotPath = ""
)

# In WhatIf mode emits exactly one JSON object:
# { decision, reason, matched_owner, proposed_actions }
```

- [ ] **Step 1: 寫 fresh/busy/update 的失敗測試**

  `tests/test_worker_self_recovery.py` 匯入 `datetime`、`timedelta`、`timezone`、`json`、`os`、`subprocess`、`tempfile`、`unittest`、`Path` 和 `mock`，使用 temporary `LOCALAPPDATA` 產生 Task 1 的 heartbeat/activity、Task 6 marker 和 JSON process snapshot，只以 `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ... -WhatIf -ProcessSnapshotPath ...` 執行。每次 stdout 必須可解析為單一 JSON object。先建立下列完整 fixture；它只寫 temporary state，package root 和 script path 則指向來源檔案。

  ```python
  def setUp(self):
      self.tmp = tempfile.TemporaryDirectory()
      self.addCleanup(self.tmp.cleanup)
      self.env_patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name}, clear=False)
      self.env_patch.start()
      self.addCleanup(self.env_patch.stop)
      self.package_root = Path("WinPython_公務電腦使用包").resolve()
      self.script_path = self.package_root / "WORKER_SELF_RECOVERY.ps1"
      self.snapshot_path = Path(self.tmp.name) / "processes.json"
      self.worker_started_at = "2026-07-15T15:00:00Z"

  def _exact_marker(self, *, age_seconds: int) -> dict:
      started_at = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
      return {
          "request_id": "update-1", "owner_pid": 654, "owner_nonce": "nonce-1",
          "owner_started_unix_ms": int(started_at.timestamp() * 1000),
          "script_path": str((self.package_root / "REMOTE_UPDATE_PACKAGE.ps1").resolve()),
          "package_path": str(self.package_root), "transaction_path": str(Path(self.tmp.name) / "transaction.json"),
          "phase": "validating",
          "phase_updated_at": (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(),
      }

  def _exact_updater_process(self) -> dict:
      script = self.package_root / "REMOTE_UPDATE_PACKAGE.ps1"
      return self._process(
          "powershell.exe", f'-File "{script}" -RequestId update-1',
          pid=654, started_at="2026-07-15T15:00:00Z",
      )

  def _write_state(self, heartbeat_age_seconds: int, *, activity: dict | None = None, active_marker: dict | None = None) -> None:
      state_dir = Path(self.tmp.name) / "AmbulanceReturnBot"
      state_dir.mkdir(parents=True, exist_ok=True)
      heartbeat = {
          "worker_id": "PC-01", "pid": 321, "package_path": str(self.package_root),
          "observed_at": (datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age_seconds)).isoformat(),
      }
      (state_dir / "worker_heartbeat.json").write_text(json.dumps(heartbeat), encoding="utf-8")
      if activity is not None:
          (state_dir / "worker_activity.json").write_text(json.dumps({**activity, "updated_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
      if active_marker is not None:
          (state_dir / "remote_update_active.json").write_text(json.dumps(active_marker), encoding="utf-8")

  def _write_recovery_history(self, offsets: list[str]) -> None:
      entries = [
          (datetime.now(timezone.utc) + timedelta(seconds=int(offset))).isoformat()
          for offset in offsets
      ]
      state_dir = Path(self.tmp.name) / "AmbulanceReturnBot"
      state_dir.mkdir(parents=True, exist_ok=True)
      (state_dir / "self_recovery.json").write_text(json.dumps({"destructive_recoveries": entries}), encoding="utf-8")

  def _process(self, name: str, command_line: str | Path, *, pid: int, started_at: str = "2026-07-15T15:00:00Z") -> dict:
      return {"ProcessId": pid, "Name": name, "CommandLine": str(command_line), "CreationDate": started_at}

  def _exact_worker_process(self) -> dict:
      return self._process("pythonw.exe", self.package_root / "worker_gui.py", pid=321, started_at=self.worker_started_at)

  def _run_watchdog(self, *, heartbeat_age_seconds: int = 30, processes: list[dict] | None = None,
                    activity: dict | None = None, active_marker: dict | None = None) -> dict:
      self._write_state(heartbeat_age_seconds, activity=activity, active_marker=active_marker)
      self.snapshot_path.write_text(json.dumps({"processes": processes or []}), encoding="utf-8")
      result = subprocess.run(self._watchdog_command(whatif=True, snapshot=self.snapshot_path), capture_output=True, text=True, encoding="utf-8", check=True)
      return json.loads(result.stdout)

  def _watchdog_command(self, *, whatif: bool, snapshot: Path) -> list[str]:
      command = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.script_path)]
      if whatif:
          command.append("-WhatIf")
      return command + ["-ProcessSnapshotPath", str(snapshot)]

  def test_watchdog_whatif_takes_no_action_for_fresh_heartbeat(self):
      output = self._run_watchdog(heartbeat_age_seconds=30, processes=[])
      self.assertEqual(output["decision"], "no_action")
      self.assertEqual(output["proposed_actions"], [])

  def test_watchdog_whatif_keeps_busy_activity_and_healthy_update_untouched(self):
      busy = self._run_watchdog(activity={"activity": "case_lookup", "owner": "lookup"})
      self.assertEqual(busy["decision"], "no_action_busy")
      updating = self._run_watchdog(
          active_marker=self._exact_marker(age_seconds=30), processes=[self._exact_updater_process()],
      )
      self.assertEqual(updating["decision"], "healthy_update")
  ```

- [ ] **Step 2: 寫 fail-closed identity matrix 的失敗測試**

  ```python
  def test_watchdog_never_targets_foreign_process_or_pid_reuse(self):
      for process in (
          self._process("python.exe", "C:/other/worker.py", pid=11),
          self._process("powershell.exe", "C:/other/REMOTE_UPDATE_PACKAGE.ps1", pid=12),
          self._process("chrome.exe", "--remote-debugging-port=9222", pid=13),
          self._process("python.exe", self.package_root / "worker.py", pid=14, started_at="different"),
      ):
          output = self._run_watchdog(heartbeat_age_seconds=121, processes=[process])
          self.assertEqual(output["proposed_actions"], [])
          self.assertIn(output["decision"], {"identity_uncertain", "no_exact_owner"})

  def test_snapshot_path_requires_whatif(self):
      result = subprocess.run(self._watchdog_command(whatif=False, snapshot=self.snapshot_path), capture_output=True, text=True)
      self.assertNotEqual(result.returncode, 0)
  ```

- [ ] **Step 3: 寫 stale exact worker/update 與 rate-limit 的失敗測試**

  ```python
  def test_watchdog_proposes_exact_worker_restart_only_when_stale(self):
      output = self._run_watchdog(
          heartbeat_age_seconds=121,
          processes=[self._exact_worker_process()],
      )
      self.assertEqual(output["decision"], "restart_stale_worker")
      self.assertEqual(output["proposed_actions"][0]["kind"], "restart_gui")

  def test_watchdog_rate_limits_the_fourth_destructive_recovery(self):
      self._write_recovery_history(["-100", "-50", "-10"])
      output = self._run_watchdog(heartbeat_age_seconds=121, processes=[self._exact_worker_process()])
      self.assertEqual(output["decision"], "recovery_rate_limited")
      self.assertEqual(output["proposed_actions"], [])
  ```

- [ ] **Step 4: 確認 RED**

  Run: `py -m unittest tests.test_worker_self_recovery -v`

  Expected: error because `WORKER_SELF_RECOVERY.ps1` is absent.

- [ ] **Step 5: 實作 watchdog**

  Script 取得 named mutex `Local\AmbulanceReturnWorkerWatchdog`；無法取得時輸出 `already_running` 並退出 0。`-ProcessSnapshotPath` 必須只允許搭配 `-WhatIf`，否則 `throw`。real mode 使用 `Get-CimInstance Win32_Process -OperationTimeoutSec 5`；CIM timeout/讀取失敗輸出 `fail_closed`，不執行 process action。

  先以獨佔方式嘗試開啟 `%LOCALAPPDATA%\AmbulanceReturnBot\package-update.lock`；若被持有，輸出 `update_lock_held`，因為手動 `UPDATE_PACKAGE.ps1` 的空窗也不可被誤啟動。之後讀 health/activity/marker，所有 path 用 `[System.IO.Path]::GetFullPath()` 及 ordinal-ignore-case 比對。

  ```powershell
  if ($freshHeartbeat) { return (Write-Decision "no_action" "heartbeat_fresh" @()) }
  if ($freshActivity) { return (Write-Decision "no_action_busy" "activity_fresh" @()) }
  if ($healthyMarker) { return (Write-Decision "healthy_update" "owner_and_phase_verified" @()) }
  if ($staleMarker -and $exactUpdater) {
      return (Invoke-OrDescribeRecovery "recover_stale_update" $exactUpdater $transactionPath)
  }
  if ($staleHeartbeat -and $exactWorker) {
      return (Invoke-OrDescribeRestart "restart_stale_worker" $exactWorker)
  }
  return (Write-Decision "identity_uncertain" "no_exact_package_owner" @())
  ```

  真正的 stale updater recovery 只能呼叫套件內 `REMOTE_UPDATE_PACKAGE.ps1 -RequestId $marker.request_id -RecoverTransactionPath $marker.transaction_path`；不得自行刪除 transaction 或 rollback。停止前再取一次 exact process snapshot，比對 PID、creation date、command line、script path、package path 和 nonce，防止 PID reuse。首次升級相容期只有在舊 marker 的 PID/creation date/command line 可精確證明是本套件 wrapper 時，給最多 10 分鐘 `legacy_update_grace`；缺任何證據時仍 fail-closed。

- [ ] **Step 6: 確認 GREEN、PowerShell parser 與安全矩陣**

  Run:

  ```powershell
  py -m unittest tests.test_worker_self_recovery -v
  $tokens = $null; $errors = $null
  [void][Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'WinPython_公務電腦使用包/WORKER_SELF_RECOVERY.ps1'), [ref]$tokens, [ref]$errors)
  if ($errors.Count) { $errors | Format-List; exit 1 }
  ```

  Expected: tests cover healthy/busy/stale/exact/foreign/PID reuse/CIM failure/rate limit and parser reports zero errors.

- [ ] **Step 7: 提交 Task 7**

  ```powershell
  git add -- WinPython_公務電腦使用包/WORKER_SELF_RECOVERY.ps1 tests/test_worker_self_recovery.py
  git commit -m "feat: add fail-closed worker watchdog"
  ```

---

### Task 8: 安裝 watchdog 排程、保證建包收錄並驗證更新交接

**Files:**
- Modify: `WinPython_公務電腦使用包/install_startup_shortcut.ps1:1-159`
- Modify: `WinPython_公務電腦使用包/update_package.ps1:1131-1142, 1276-1294`
- Modify: `scripts/build_public_duty_package.ps1:285-305, 437-599`
- Modify: `tests/test_worker_gui.py:750-811, 906-920`
- Modify: `tests/test_update_package_integration.py:29-131, 228-310`

**Interfaces:**

```powershell
# Main logon task: AmbulanceReturnWorker
# Watchdog task: AmbulanceReturnWorkerWatchdog
# -SkipScheduledTask skips only AmbulanceReturnWorker refresh.
# It never skips AmbulanceReturnWorkerWatchdog refresh.
```

- [ ] **Step 1: 寫 installer/WhatIf 的失敗測試**

  先在 `WorkerGuiEnvTests` 加入固定指向來源 installer 的 helper，避免測試碰觸真實排程：

  ```python
  def _installer_command(self, *args: str) -> list[str]:
      return [
          "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
          str(Path("WinPython_公務電腦使用包/install_startup_shortcut.ps1").resolve()), *args,
      ]

  def test_startup_installer_defines_limited_watchdog_task_and_whatif(self):
      source = Path("WinPython_公務電腦使用包/install_startup_shortcut.ps1").read_text(encoding="utf-8")
      self.assertIn('"AmbulanceReturnWorkerWatchdog"', source)
      self.assertIn("-MultipleInstances IgnoreNew", source)
      self.assertIn("WORKER_SELF_RECOVERY.ps1", source)
      result = subprocess.run(self._installer_command("-WhatIf"), capture_output=True, text=True, encoding="utf-8")
      self.assertEqual(result.returncode, 0)
      self.assertIn("Would install watchdog task", result.stdout)

  def test_skip_scheduled_task_still_refreshes_watchdog(self):
      result = subprocess.run(self._installer_command("-WhatIf", "-SkipScheduledTask"), capture_output=True, text=True, encoding="utf-8")
      self.assertIn("Would skip scheduled task refresh: AmbulanceReturnWorker", result.stdout)
      self.assertIn("Would install watchdog task: AmbulanceReturnWorkerWatchdog", result.stdout)
  ```

- [ ] **Step 2: 寫 package/remote-update handoff 的失敗測試**

  ```python
  def test_public_package_requires_and_contains_watchdog(self):
      builder = Path("scripts/build_public_duty_package.ps1").read_text(encoding="utf-8")
      self.assertIn('"WORKER_SELF_RECOVERY.ps1"', builder)
      self.assertIn("AmbulanceReturnWorkerWatchdog", builder)

  def test_update_installs_watchdog_after_validating_installed_tree(self):
      updater = Path("WinPython_公務電腦使用包/update_package.ps1").read_text(encoding="utf-8-sig")
      self.assertLess(updater.index("Assert-InstalledUpdateTree"), updater.index("install_startup_shortcut.ps1"))
      self.assertIn("-SkipScheduledTask", updater)
  ```

- [ ] **Step 3: 確認 RED**

  Run:

  ```powershell
  py -m unittest tests.test_worker_gui.WorkerGuiEnvTests.test_startup_installer_defines_limited_watchdog_task tests.test_worker_gui.WorkerGuiEnvTests.test_skip_scheduled_task_still_refreshes_watchdog tests.test_worker_gui.WorkerGuiEnvTests.test_public_package_requires_and_contains_watchdog -v
  ```

  Expected: FAIL because installer only defines one logon task and builder does not require watchdog.

- [ ] **Step 4: 實作 installer source 和 builder template 的相同行為**

  在 source installer 和 `Write-PackageText -RelativePath "install_startup_shortcut.ps1"` 內嵌範本做相同修改。建立 `New-WatchdogTask`：每分鐘、current interactive user、`RunLevel Limited`、`MultipleInstances IgnoreNew`、`-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$packageDir\WORKER_SELF_RECOVERY.ps1"`。`WORKER_STARTUP_LAUNCHER_ENABLED=false` 必須移除 Startup shortcut、main task 和 watchdog task。

  `-WhatIf` 顯示兩個 task 的 name/user/action；`-SkipScheduledTask` 只跳過 `AmbulanceReturnWorker`，仍建立/更新 watchdog。Task Scheduler API 失敗時保留 Startup shortcut、輸出明確 warning，且在 updater result 中留下 `watchdog_install_warning`，不可宣稱 watchdog 已安裝。

  在 `update_package.ps1` 先完成 `Assert-InstalledUpdateTree`，再呼叫新的 installer；保留 `-SkipScheduledTask`，利用新語意只刷新 watchdog。builder 的 required file list 加入 `WORKER_SELF_RECOVERY.ps1`，讓遺漏 script 時 build fail fast；`Copy-ZipStage`/manifest 的既有遞迴 copy 會自動收錄檔案，仍要在整合測試確認 ZIP 內存在。

- [ ] **Step 5: 確認 GREEN、執行 installer dry run 和 package integration**

  Run:

  ```powershell
  py -m unittest tests.test_worker_gui.WorkerGuiEnvTests.test_startup_installer_defines_limited_watchdog_task tests.test_worker_gui.WorkerGuiEnvTests.test_skip_scheduled_task_still_refreshes_watchdog tests.test_worker_gui.WorkerGuiEnvTests.test_public_package_requires_and_contains_watchdog tests.test_update_package_integration -v
  powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File 'WinPython_公務電腦使用包/install_startup_shortcut.ps1' -WhatIf -SkipScheduledTask
  ```

  Expected: tests pass; WhatIf says main task skipped and watchdog task refreshed.

- [ ] **Step 6: 提交 Task 8**

  ```powershell
  git add -- WinPython_公務電腦使用包/install_startup_shortcut.ps1 WinPython_公務電腦使用包/update_package.ps1 scripts/build_public_duty_package.ps1 tests/test_worker_gui.py tests/test_update_package_integration.py
  git commit -m "feat: install worker watchdog task"
  ```

---

### Task 9: 全面驗證、建包、發布、NAS 部署與低風險實機演練

**Files:**
- Modify via build only: `WinPython_公務電腦使用包/VERSION.txt`
- Generate via build only: `UPDATE/WinPython_公務電腦使用包/**`, `UPDATE/NAS包/**`, release assets under `UPDATE/`
- Do not hand-edit generated files.

- [ ] **Step 1: 跑完整靜態檢查**

  Run:

  ```powershell
  $pythonFiles = @(
    'WinPython_公務電腦使用包/app.py',
    'WinPython_公務電腦使用包/worker.py',
    'WinPython_公務電腦使用包/worker_gui.py'
  ) + (Get-ChildItem -LiteralPath 'WinPython_公務電腦使用包/ambulance_bot' -Filter *.py | ForEach-Object { $_.FullName })
  py -m py_compile @pythonFiles
  $scripts = @(
    'WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1',
    'WinPython_公務電腦使用包/WORKER_SELF_RECOVERY.ps1',
    'WinPython_公務電腦使用包/install_startup_shortcut.ps1',
    'WinPython_公務電腦使用包/update_package.ps1'
  )
  foreach ($script in $scripts) {
    $tokens = $null; $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile((Resolve-Path $script), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { $errors | Format-List; exit 1 }
  }
  git diff --check -- WinPython_公務電腦使用包/VERSION.txt docs/superpowers/plans/2026-07-15-worker-online-reliability.md
  ```

  Expected: Python compile, all four PowerShell parsers and diff check succeed.

- [ ] **Step 2: 跑完整單元與整合測試**

  Run: `py -m unittest discover -s tests -v`

  Expected: 0 failures/errors; record the real test count in the release handoff, not an old count.

- [ ] **Step 3: 以 dry-run matrix 檢查安全邊界**

  對 fresh heartbeat、busy manual task、busy case lookup、healthy remote update、held `package-update.lock`、stale exact updater、stale exact Worker、foreign Python、foreign PowerShell、Chrome、PID reuse、corrupt marker、CIM timeout、rate limit 的 fixture 逐一執行 `WORKER_SELF_RECOVERY.ps1 -WhatIf`。確認只有 stale exact updater/Worker 產生 proposed action，actions 永遠沒有 Chrome 或套件外 PID。

- [ ] **Step 4: 建立版本與兩套 package**

  Run:

  ```powershell
  $version = Get-Date -Format 'yyyy.MM.dd.HHmm'
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_all_packages.ps1 -Version $version
  ```

  Expected: source `VERSION.txt`、public ZIP internal version、NAS `VERSION.txt`、release version asset 和 SHA all match `$version`; public ZIP contains `WORKER_SELF_RECOVERY.ps1`, `worker_health.py`, `worker_routes.py`, `worker_control.py`, updated installer and template.

- [ ] **Step 5: 做獨立 diff/release review**

  檢查 source/generated boundary、control request 每 10 秒而非兩條固定輪詢、heartbeat retention、token boundary、route instance matching、GUI/Tk thread boundary、activity/update suppression、PID reuse、CIM timeout、`package-update.lock`、rate limits、`-SkipScheduledTask` 和 package hygiene。任何 Critical/Important finding 必須先新增 regression test、修正 owning source、再重跑 Step 1–3。

- [ ] **Step 6: 只 stage 本計畫檔案並提交、推送、發布**

  ```powershell
  git status --short
  git diff --name-only
  git add -- WinPython_公務電腦使用包/VERSION.txt docs/superpowers/plans/2026-07-15-worker-online-reliability.md
  git diff --cached --name-only
  git commit -m "chore: release worker online recovery"
  git push origin master
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/publish_ambulance_return_release.ps1 -Version $version
  gh release view "ambulance-return-$version" --repo seaflun/ambulance-return-bot --json tagName,targetCommitish,assets
  ```

  The generated `UPDATE/**` tree is an ignored build artifact and must not be staged. Before `git add`, verify the two listed files contain only this release's version/plan changes. If `VERSION.txt` contains pre-existing user work, stop and ask rather than staging it. Never stage `.codex/`, `NAS包(舊版)/` or user-owned older plan. After publishing, download the direct-tag `ambulance-return-version.txt`, public ZIP and SHA file again; compare downloaded SHA, published SHA, ZIP internal `VERSION.txt` and release target commit.

- [ ] **Step 7: 部署 NAS bundle only with verified write authority**

  Compare source hashes, generated `UPDATE/NAS包` hashes and live `\\100.114.126.58\docker\ambulance_return_bot` hashes before restarting. Preserve live `.env` and artifacts. Only if the deployment write path is authorized, sync generated NAS bundle and use the existing limited restart account; then verify `/status`, `ambulance-app-1` status, `/worker/identity` token behavior and a test control request. If write authority is unavailable, stop after producing the bundle and explicitly report that NAS deployment remains pending.

- [ ] **Step 8: 實機公務電腦啟用與演練**

  在沒有正式勤務的時段，先確認新 control request 每 10 秒更新本機 heartbeat，NAS 45 秒內顯示 online、route verified、heartbeat version 等於 `$version`。再下達一次遠端更新命令，確認約 10 秒內顯示 received/waiting status。讓 Worker thread 正常 return，確認 GUI 約 15 秒重啟；停止 GUI，確認 watchdog 在心跳 stale 後的下一分鐘安全重啟；建立 fresh manual/case/update marker，確認不介入。不得終止 Chrome、不得重開 Windows、不得送出四站正式資料。

- [ ] **Step 9: 最終交付證據**

  回報 commits、test count、PowerShell parser、package contents、release tag/target、SHA readback、source/generated/live NAS hash comparison、NAS `/status`、公務電腦 heartbeat/route/version、Task Scheduler watchdog last-run 和三個故障演練的 decision/reason。若實機步驟未完成，清楚標示「程式已發布，公務電腦現場驗證待完成」，不得把發布視為啟用完成。
