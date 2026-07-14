# Layered Worker Self-Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓公務電腦救護 Worker 能在 GUI 執行緒退出、Worker 心跳中斷或遠端更新交易卡死時自動復原，同時絕不重啟 Windows、不碰非本專案的 Python／PowerShell／Chrome，也不在救護登打、案件查詢或健康更新期間介入。

**Architecture:** 在既有 WinPython source-of-truth 增加三層保護：Worker 每 10 秒寫本機心跳並回報 NAS；Worker GUI 每 5 秒監督自己的背景執行緒；Windows 排程每分鐘執行獨立 PowerShell watchdog。更新 wrapper 以具 phase、owner identity 與時間戳的交易 marker 提供可驗證恢復依據。所有終止／重啟動作都要求 package path、PID、process start time、request ID 與 script identity 完整吻合；證據不完整時 fail closed。

**Tech Stack:** Python 3、Flask、Tkinter、`unittest`、PowerShell 5.1、Windows Task Scheduler、JSON 原子檔案、既有 Worker token API。

## Global Constraints

- 執行碼只編輯 `WinPython_公務電腦使用包` source-of-truth；不得手改 generated `UPDATE/NAS包`。
- 僅可停止／重啟此套件目錄內的 Worker GUI、Worker Python 與 `REMOTE_UPDATE_PACKAGE.ps1` owner；不得碰其他 Python、PowerShell、Chrome、ChromeDriver 或使用者程式。
- 不得重啟 Windows，不建立 Windows Service，不加入 WinRM/RDP，也不擴張 NAS 對公務電腦的遠端控制權。
- fresh manual-task／case-lookup activity marker 或健康的 update transaction 存在時，只記錄狀態，不得終止或重啟。
- 更新 owner 必須同時驗證 PID、process start time、request ID、owner nonce、script path 與 package path；任一項不符就 fail closed。
- 所有 CIM 查詢皆須有 5 秒 operation timeout；CIM timeout、marker 損壞、多筆交易衝突或 identity 不確定時只寫安全診斷，不執行 destructive recovery。
- GUI 10 分鐘最多重啟 Worker thread 3 次；Windows watchdog 10 分鐘最多執行 3 次終止／重啟動作。計數狀態寫入 `%LOCALAPPDATA%\AmbulanceReturnBot\self_recovery.json`。
- Worker 心跳狀態只允許 `starting`、`idle`、`busy`、`update_handoff`、`recovering`、`stopping`；NAS 以 45 秒判定 online/offline，本機 watchdog 以 120 秒判定 stale。
- `remote_update_active.json` phase 只允許 `discovering_runtime`、`installing`、`validating`、`committing`、`rolling_back`、`restarting`；phase 超過 10 分鐘未更新才可進入 stale-update 判定。
- 不把 token、帳密、task JSON、瀏覽器 profile、screenshots、logs、`.env` 或 artifacts 納入 Git、測試 fixture 或 release asset。
- 不碰使用者未追蹤的 `.codex/`、`NAS包(舊版)/` 與 `docs/superpowers/plans/2026-06-29-nas-entry-implementation.md`。
- 每個行為變更都先新增會因缺少該行為而失敗的測試，再做最小實作，再跑完整相關測試。

---

### Task 1: 建立共用 Worker health 資料模型與純判定函式

**Files:**
- Create: `WinPython_公務電腦使用包/ambulance_bot/worker_health.py`
- Create: `tests/test_worker_health.py`

**Interfaces:**

```python
ALLOWED_HEARTBEAT_STATES: frozenset[str]

GuiRestartDecision fields:
    should_restart: bool
    reason: str
    retained_restart_times: Sequence[float]

health_state_root() -> Path
local_heartbeat_path() -> Path
local_activity_path() -> Path
self_recovery_state_path() -> Path
write_health_json_atomic(path: Path, payload: Mapping[str, object]) -> None
def build_worker_heartbeat(
    *, worker_id: str, state: str, package_version: str, pid: int,
    activity: str = "", busy_reason: str = "", request_id: str = "",
    observed_at: datetime | None = None,
) -> dict[str, object]
def decide_gui_restart(
    *, now_monotonic: float, thread_alive: bool, stopped_at: float | None,
    busy_reason: str, update_active: bool, restart_times: Sequence[float],
    grace_seconds: float = 15.0, window_seconds: float = 600.0,
    max_restarts: int = 3,
) -> GuiRestartDecision
```

- [ ] **Step 1: 寫下路徑、payload 與原子寫入的失敗測試**

  在 temporary `LOCALAPPDATA` 下斷言三個狀態檔都位於 `AmbulanceReturnBot`，heartbeat 包含 worker ID、state、version、PID、activity、request ID 與 timezone-aware `observed_at`。傳入未知 state 必須 `raise ValueError`。連續覆寫後 JSON 必須完整可解析，且不得殘留 `.tmp`。

- [ ] **Step 2: 寫下 GUI restart 純判定的失敗測試**

  table-driven 測試以下結果：thread alive 不重啟；剛停止未滿 15 秒不重啟；fresh busy/update 不重啟；停止滿 15 秒且無 activity 時重啟；600 秒外的歷史計數被清除；10 分鐘內已有 3 次時回傳 `restart_rate_limited`。

- [ ] **Step 3: 確認 RED**

  Run: `py -m unittest tests.test_worker_health -v`

  Expected: ERROR，因 `ambulance_bot.worker_health` 尚未存在。

- [ ] **Step 4: 實作最小共用模組**

  使用 `Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")` 建立 state root；原子寫入採同目錄唯一 temp file、`flush()`、`os.fsync()`、`os.replace()`，finally 清除 temp。`decide_gui_restart()` 保持純函式，不讀 process、不 sleep、不直接啟動任何程式。

- [ ] **Step 5: 確認 GREEN 並提交**

  Run: `py -m unittest tests.test_worker_health -v`

  Expected: PASS。

  Commit only the two Task 1 files with message: `feat: add worker health primitives`

---

### Task 2: 新增 NAS Worker heartbeat API 與分離式管理畫面狀態

**Files:**
- Modify: `WinPython_公務電腦使用包/app.py`
- Modify: `WinPython_公務電腦使用包/templates/admin_public_pc.html`
- Modify: `tests/test_web_app.py`

**Interfaces:**

```python
worker_heartbeat_file() -> Path
upsert_worker_heartbeat(data: Mapping[str, object]) -> dict[str, object]
def worker_heartbeat_admin_view(
    reports: Sequence[Mapping[str, object]] | None = None,
    now: datetime | None = None,
) -> dict[str, object]

POST /worker/heartbeat -> Response
```

- [ ] **Step 1: 寫下 API authentication/schema 的失敗測試**

  在 `tests/test_web_app.py` 使用既有 `X-Worker-Token` fixture 測試：無 token 為 403；未知 state、空 worker ID、非正整數 PID 為 400；合法 heartbeat 為 200。斷言 server 覆寫 `last_seen_at`，不信任 client 傳入的 online 狀態，並以 atomic JSON 保存每個 worker 的最後一筆資料。

- [ ] **Step 2: 寫下 online/offline 與版本分離的失敗測試**

  固定 `now` 測試 44 秒為 online、46 秒為 offline。管理 view 必須同時回傳：NAS package version、最新 heartbeat package version/time/state、最新 task report package version/time。沒有 heartbeat 時不得把 NAS version 冒充公務電腦目前版本。

- [ ] **Step 3: 寫下 template 失敗測試**

  斷言 `/admin/public-pc` 清楚顯示「公務電腦心跳：在線／離線」、「心跳版本」、「最後任務回報版本」與「NAS 後台版本」。remote-update 卡片在線時使用 heartbeat version；離線時標示「最後心跳版本」，不得 fallback 成 NAS version。

- [ ] **Step 4: 確認 RED**

  Run: `py -m unittest tests.test_web_app.WebAppTests.test_worker_heartbeat_requires_token tests.test_web_app.WebAppTests.test_worker_heartbeat_admin_online_threshold tests.test_web_app.WebAppTests.test_public_pc_admin_separates_heartbeat_report_and_nas_versions -v`

  Expected: FAIL/ERROR，因 route/view 尚未存在或頁面仍混用 task report 與 NAS version。

- [ ] **Step 5: 實作 endpoint、store 與 view**

  heartbeat store 沿用 `write_json_atomic()` 與專用 lock；key 使用 server-normalized worker ID。只接受 allowlist 欄位，server 寫入 `last_seen_at`。`worker_heartbeat_admin_view()` 以 45 秒門檻計算狀態，保留 task-report view 但不再稱它為 heartbeat。

- [ ] **Step 6: 確認 GREEN、跑 Web 回歸並提交**

  Run:

  ```powershell
  py -m unittest tests.test_web_app.WebAppTests.test_worker_heartbeat_requires_token tests.test_web_app.WebAppTests.test_worker_heartbeat_admin_online_threshold tests.test_web_app.WebAppTests.test_public_pc_admin_separates_heartbeat_report_and_nas_versions -v
  py -m unittest tests.test_web_app -v
  ```

  Expected: PASS。

  Commit only the three Task 2 files with message: `feat: report worker heartbeat to NAS`

---

### Task 3: Worker 背景心跳與 activity lease

**Files:**
- Modify: `WinPython_公務電腦使用包/worker.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/worker_health.py`
- Modify: `tests/test_worker.py`
- Modify: `tests/test_worker_health.py`

**Interfaces:**

```python
def set_worker_heartbeat_state(
    state: str, *, activity: str = "", busy_reason: str = "", request_id: str = ""
) -> None
emit_worker_heartbeat(server_url: str, worker_id: str) -> dict[str, object]
def run_worker_heartbeat_loop(
    server_url: str, worker_id: str, stop_event: threading.Event,
    interval_seconds: float = 10.0,
) -> None
start_worker_heartbeat(server_url: str, worker_id: str) -> tuple[threading.Event, threading.Thread]
```

- [ ] **Step 1: 寫下 heartbeat loop 的失敗測試**

  patch `request_json` 與本機 temp state root，驗證每次 emit 先完成本機 atomic write 再 POST `/worker/heartbeat`；NAS timeout 只記 log 並繼續下一輪；payload 帶目前 package version 與 PID；stop event 能立即中止等待，不用固定 sleep 阻塞測試。

- [ ] **Step 2: 寫下 lifecycle/activity 的失敗測試**

  驗證：startup recovery gate 完成後才 emit `starting`；正常 poll 為 `idle`；manual four-site 與 case lookup 期間為 `busy` 並寫 fresh `worker_activity.json`；準備啟動 updater 前同步 emit `update_handoff`；交易 recovery 時為 `recovering`；`main()` finally emit `stopping` 並 stop/join heartbeat thread。狀態 POST 失敗不可使 worker loop return。

- [ ] **Step 3: 確認 RED**

  Run: `py -m unittest tests.test_worker.WorkerTests.test_heartbeat_writes_local_state_before_post tests.test_worker.WorkerTests.test_heartbeat_post_failure_does_not_stop_worker tests.test_worker.WorkerTests.test_worker_lifecycle_emits_safe_heartbeat_states -v`

  Expected: FAIL/ERROR，因 heartbeat lifecycle 尚未實作。

- [ ] **Step 4: 實作 background heartbeat 與 activity lease**

  使用 module-level lock 保存目前 state snapshot。heartbeat daemon 每 10 秒寫 `worker_heartbeat.json` 並 POST；state 為 busy 時同時刷新 `worker_activity.json`，離開 activity 時只在 owner token 一致時清除。manual task 使用既有 lock 作第一道 guard，case lookup 使用 activity lease。`maybe_run_remote_update()` 必須在啟動 hidden updater process 前完成一次同步 `update_handoff` emit。

- [ ] **Step 5: 確認 GREEN、跑 Worker 回歸並提交**

  Run:

  ```powershell
  py -m unittest tests.test_worker_health tests.test_worker.WorkerTests.test_heartbeat_writes_local_state_before_post tests.test_worker.WorkerTests.test_heartbeat_post_failure_does_not_stop_worker tests.test_worker.WorkerTests.test_worker_lifecycle_emits_safe_heartbeat_states -v
  py -m unittest tests.test_worker -v
  ```

  Expected: PASS。

  Commit only the four Task 3 files with message: `feat: emit worker lifecycle heartbeat`

---

### Task 4: GUI 監督 Worker thread 並限制重啟頻率

**Files:**
- Modify: `WinPython_公務電腦使用包/worker_gui.py`
- Modify: `tests/test_worker_gui.py`

**Interfaces:**

```python
_schedule_worker_supervisor(self) -> None
_supervise_worker_thread(self) -> None
_record_worker_thread_exit(self, error: BaseException | None = None) -> None
```

- [ ] **Step 1: 寫下正常退出與例外退出的失敗測試**

  以 `SimpleNamespace`/fake root 測 `_run_worker()`：不論 `worker.main()` 正常 return 或 raise，都要清除 thread-running 狀態並記錄 `worker_stopped_at`；GUI 不可直接退出。

- [ ] **Step 2: 寫下 supervisor 決策與排程的失敗測試**

  驗證 init 後每 5 秒排程一次；停止未滿 15 秒不動；fresh manual task、case lookup 或 update marker 不動；安全停止滿 15 秒呼叫 `_restart_worker()` 一次；10 分鐘內第 4 次只顯示 rate-limit 訊息且不再重啟。

- [ ] **Step 3: 確認 RED**

  Run: `py -m unittest tests.test_worker_gui.WorkerGuiEnvTests.test_worker_exit_is_observed_by_supervisor tests.test_worker_gui.WorkerGuiEnvTests.test_supervisor_restarts_only_after_safe_grace tests.test_worker_gui.WorkerGuiEnvTests.test_supervisor_rate_limits_restarts -v`

  Expected: FAIL/ERROR，因 supervisor 尚未存在，正常 return 也未記錄 stopped state。

- [ ] **Step 4: 實作 GUI supervisor**

  `_supervise_worker_thread()` 僅讀自身 `self.worker_thread`、shared health state 與 exact update marker，再呼叫 Task 1 純函式。允許重啟時先將 timestamp append 到 GUI process 內的 bounded restart list，再呼叫既有 `_restart_worker()`；watchdog 的 `self_recovery.json` 只由 Windows watchdog 在 named mutex 內更新，避免跨 process lost update。所有 UI 更新透過 Tk root thread；不得 enumerate 或 terminate OS processes。

- [ ] **Step 5: 確認 GREEN、跑 GUI 回歸並提交**

  Run: `py -m unittest tests.test_worker_gui -v`

  Expected: PASS。

  Commit only the two Task 4 files with message: `feat: supervise worker GUI thread`

---

### Task 5: 為 remote updater 增加 phase heartbeat 與完整 owner identity

**Files:**
- Modify: `WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1`
- Modify: `tests/test_worker.py`
- Modify: `tests/test_update_package_integration.py`

**Marker schema:**

```json
{
  "request_id": "server command id",
  "owner_pid": 1234,
  "owner_process_started_at": "2026-07-14T18:00:00+08:00",
  "owner_nonce": "random per run",
  "script_path": "exact REMOTE_UPDATE_PACKAGE.ps1 path",
  "package_path": "exact package root",
  "transaction_path": "exact owned recovery transaction path or empty before creation",
  "phase": "validating",
  "phase_started_at": "2026-07-14T18:01:00+08:00",
  "phase_updated_at": "2026-07-14T18:01:10+08:00"
}
```

- [ ] **Step 1: 寫下 marker schema 與 transition 的失敗測試**

  驗證 wrapper 建立 marker 時具完整 identity；正常流程依序至少出現 `discovering_runtime`、`installing`、`validating`、`committing`、`restarting`；失敗回復出現 `rolling_back`、`restarting`。長時間 validation 每次 probe 都刷新 `phase_updated_at`，且 temp marker 使用同目錄 atomic replace。

- [ ] **Step 2: 寫下 bounded CIM 與 owner mismatch 失敗測試**

  保留 `Get-CimInstance -OperationTimeoutSec 5` contract。以 process snapshot fixture 驗證 PID 重用、start time 不同、script/package path 不同、nonce/request ID 不同均不得被當作同一 owner。

- [ ] **Step 3: 確認 RED**

  Run: `py -m unittest tests.test_worker.WorkerTests.test_remote_update_marker_has_phase_and_owner_identity tests.test_update_package_integration.RemoteUpdatePackageIntegrationTests.test_phase_heartbeat_advances_during_validation -v`

  Expected: FAIL，現行 marker 沒有完整 phase/owner identity。

- [ ] **Step 4: 實作 phase marker helper**

  在 wrapper 內建立 `Write-RemoteUpdateActiveMarker` 與 `Set-RemoteUpdatePhase`，所有 phase 更新都保留 immutable owner 欄位，只改 phase/timestamps/transaction path。每個主要交易區段進入時設定 phase，transaction 建立後立即將其 exact path 寫入 marker，validation loop 每輪 refresh。保留並強化現有 `-RecoverTransactionPath`：只接受同 package identity 的 transaction file，且 recovery request ID 必須等於 marker request ID。finally 僅在 marker identity 仍屬於本 run 時清除。

- [ ] **Step 5: 確認 GREEN、跑 updater 回歸並提交**

  Run:

  ```powershell
  py -m unittest tests.test_worker.WorkerTests.test_remote_update_marker_has_phase_and_owner_identity tests.test_update_package_integration -v
  $errors = $null
  [void][Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1'), [ref]$null, [ref]$errors)
  if ($errors.Count) { $errors | Format-List; exit 1 }
  ```

  Expected: PASS，PowerShell parser 0 errors。

  Commit only the three Task 5 files with message: `fix: identify remote update transaction phases`

---

### Task 6: 實作 fail-closed Windows watchdog 與可重現 dry-run

**Files:**
- Create: `WinPython_公務電腦使用包/WORKER_SELF_RECOVERY.ps1`
- Create: `tests/test_worker_self_recovery.py`

**Command contract:**

```powershell
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\WORKER_SELF_RECOVERY.ps1

powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\WORKER_SELF_RECOVERY.ps1 -WhatIf
```

`-ProcessSnapshotPath` 僅可搭配 `-WhatIf`，測試會在 temporary directory 生成 fake snapshot 後傳入；dry-run 輸出單一 decision JSON，列出 `decision`、`reason`、`matched_owner` 與 proposed actions，絕不終止或啟動 process。

- [ ] **Step 1: 寫下健康／busy／update 情境的失敗測試**

  Python test 在 temp `LOCALAPPDATA` 建立心跳、activity、update marker 與 process snapshots，呼叫 PowerShell dry-run 並解析 JSON。斷言：fresh heartbeat=`no_action`；fresh manual/case activity=`no_action`；phase 未滿 10 分鐘=`healthy_update`；fresh owned GUI/worker 不產生 proposed termination。

- [ ] **Step 2: 寫下 exact identity 與 fail-closed 失敗測試**

  測試 foreign Python、foreign PowerShell、Chrome、ChromeDriver，即使 command line 含相似文字也永遠不在 actions；PID/start/request/nonce/script/package 任一 mismatch=`identity_uncertain`；CIM timeout、JSON 損壞、多筆 transaction=`fail_closed`；`ProcessSnapshotPath` 未搭配 `-WhatIf` 必須 exit nonzero。

- [ ] **Step 3: 寫下 recovery 與限流失敗測試**

  - stale update 超過 10 分鐘且 exact owner 全部吻合：propose 終止唯一 updater owner、執行既有 transaction recovery，再以 VBS launcher 啟動。
  - 無 update、heartbeat 超過 120 秒且 exact package-owned Worker identity 唯一：propose 終止該 Worker tree 並啟動 `RUN_WORKER_GUI_WINPYTHON.vbs`。
  - 10 分鐘內已有 3 次 destructive recovery：`recovery_rate_limited`，actions 為空。

- [ ] **Step 4: 確認 RED**

  Run: `py -m unittest tests.test_worker_self_recovery -v`

  Expected: ERROR，因 watchdog script 尚未存在。

- [ ] **Step 5: 實作 single-instance watchdog**

  使用 named mutex `Local\AmbulanceReturnWorkerWatchdog` 防重疊。real mode 以 `Get-CimInstance Win32_Process -OperationTimeoutSec 5` 取得 snapshot；所有路徑先 `GetFullPath()`、case-insensitive 比對 exact package root。終止只使用已驗證 PID，且 Stop-Process 前再次讀 process start time 與 command line確認沒有 PID reuse。stale update 具合法 transaction path 時，以 `REMOTE_UPDATE_PACKAGE.ps1 -RequestId $marker.request_id -RecoverTransactionPath $marker.transaction_path` 執行既有 rollback/restart；phase 已進入 installing 之後卻沒有合法 transaction path 時 fail closed。正常 Worker 恢復只以 `wscript.exe "$packageDir\RUN_WORKER_GUI_WINPYTHON.vbs"` 啟動。每次 decision 與 action 原子追加到 local recovery state/log，不向 NAS 上傳 process command line。

- [ ] **Step 6: 確認 GREEN、解析 PowerShell 並提交**

  Run:

  ```powershell
  py -m unittest tests.test_worker_self_recovery -v
  $errors = $null
  [void][Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'WinPython_公務電腦使用包/WORKER_SELF_RECOVERY.ps1'), [ref]$null, [ref]$errors)
  if ($errors.Count) { $errors | Format-List; exit 1 }
  ```

  Expected: PASS，PowerShell parser 0 errors。

  Commit only the two Task 6 files with message: `feat: add fail-closed worker watchdog`

---

### Task 7: 安裝每分鐘 watchdog 排程並保證更新後自動刷新

**Files:**
- Modify: `WinPython_公務電腦使用包/install_startup_shortcut.ps1`
- Modify: `tests/test_worker_gui.py`
- Modify: `tests/test_update_package_integration.py`

**Scheduled task contract:**

- Name: `AmbulanceReturnWorkerWatchdog`
- Principal: current interactive user
- Run level: `Limited`
- Trigger: every 1 minute, indefinitely
- Multiple instances: `IgnoreNew`
- Action: hidden, noninteractive PowerShell running the exact packaged `WORKER_SELF_RECOVERY.ps1`

- [ ] **Step 1: 寫下 installer source/WhatIf 的失敗測試**

  驗證 `-WhatIf` 輸出兩個 task 的 name/principal/action/interval；watchdog 使用 Limited、IgnoreNew 與 exact package path。`WORKER_STARTUP_LAUNCHER_ENABLED=false` 同時移除 main task、startup shortcut 與 watchdog task。

- [ ] **Step 2: 寫下 `-SkipScheduledTask` 回歸測試**

  呼叫 `install_startup_shortcut.ps1 -WhatIf -SkipScheduledTask`，斷言它只跳過 legacy `AmbulanceReturnWorker` refresh，仍會 refresh `AmbulanceReturnWorkerWatchdog`。保留 `update_package.ps1` 現行 `-SkipScheduledTask` 呼叫即可讓新版本安裝完成時建立／更新 watchdog。

- [ ] **Step 3: 確認 RED**

  Run: `py -m unittest tests.test_worker_gui.WorkerGuiEnvTests.test_startup_installer_defines_watchdog_task tests.test_update_package_integration.UpdatePackageIntegrationTests.test_skip_scheduled_task_still_refreshes_watchdog -v`

  Expected: FAIL，現行 installer 只有 logon task，且 `-SkipScheduledTask` 直接退出。

- [ ] **Step 4: 實作排程安裝／移除**

  將 main task 與 watchdog task 建構拆開。先安裝 startup shortcut，再無條件嘗試註冊 watchdog；`-SkipScheduledTask` 僅控制 main task。使用 `New-ScheduledTaskAction/Trigger/Settings/Principal`，不得要求管理員或 SYSTEM。若 Task Scheduler API 拒絕，保留 startup shortcut 並顯示清楚 warning，不把安裝誤報成功。

- [ ] **Step 5: 確認 GREEN、跑更新整合測試並提交**

  Run:

  ```powershell
  py -m unittest tests.test_worker_gui.WorkerGuiEnvTests.test_startup_installer_defines_watchdog_task tests.test_update_package_integration -v
  powershell -NoProfile -ExecutionPolicy Bypass -File 'WinPython_公務電腦使用包/install_startup_shortcut.ps1' -WhatIf -SkipScheduledTask
  ```

  Expected: tests PASS；WhatIf 明確顯示跳過 main task 並安裝 `AmbulanceReturnWorkerWatchdog`。

  Commit only the three Task 7 files with message: `feat: install worker watchdog task`

---

### Task 8: 安全性整合測試與管理畫面端到端驗證

**Files:**
- Modify only source/test files required by failures found in this task.

- [ ] **Step 1: 跑所有 targeted suites**

  Run:

  ```powershell
  py -m unittest tests.test_worker_health tests.test_worker_self_recovery tests.test_worker_gui tests.test_worker tests.test_update_package_integration tests.test_web_app -v
  ```

  Expected: PASS，0 failures/errors。

- [ ] **Step 2: 用 dry-run matrix 驗證安全邊界**

  對 healthy idle、busy manual task、busy case lookup、healthy update、stale exact update、stale exact worker、foreign Python、foreign PowerShell、Chrome、corrupt marker、CIM timeout、rate-limited 等 fixtures 逐一執行 watchdog `-WhatIf`。斷言只有 stale exact update/worker 產生 proposed destructive actions，且 actions 中從未出現 Chrome/ChromeDriver 或 package 外 PID。

- [ ] **Step 3: 本機啟動隔離測試 Worker**

  在 temp `LOCALAPPDATA`、非正式 worker ID、mock NAS URL 下啟動 package GUI，觀察至少 3 個 10 秒 heartbeat interval；讓 mock `worker.main()` 正常 return，驗證 GUI 15 秒後重啟；製造 fresh activity/update marker，驗證不重啟。不得登入或送出四個正式網站資料。

- [ ] **Step 4: 檢查管理畫面渲染**

  以 Flask test client 建立 heartbeat/report fixture，確認 online/offline、NAS version、heartbeat version、task-report version 與 remote-update status 不互相冒充；對 template snapshot 做文字 assertions。

- [ ] **Step 5: 修正整合發現並提交**

  每個發現先新增最小 regression test，再修改 owning source，重跑 Task 8 Step 1。若有修正，commit message: `fix: harden worker self recovery integration`；無修正則不建立空 commit。

---

### Task 9: 完整驗證、打包、發布、NAS 部署與明日實機啟用

**Files:**
- Modify through build script: `WinPython_公務電腦使用包/VERSION.txt`
- Generate through build script: `UPDATE/WinPython_公務電腦使用包/**`
- Generate through build script: `UPDATE/NAS包/**`
- Generate through build script: release ZIP/version/SHA assets under `UPDATE/`

- [ ] **Step 1: 完整靜態檢查**

  Run:

  ```powershell
  $files = @(
    'app.py', 'worker.py', 'worker_gui.py', 'consumables_login.py',
    'disinfect.py', '_runtime_loader.py'
  ) + (Get-ChildItem -Path 'WinPython_公務電腦使用包/ambulance_bot' -Filter *.py | ForEach-Object { $_.FullName })
  py -m py_compile @files
  $scripts = @(
    'WinPython_公務電腦使用包/REMOTE_UPDATE_PACKAGE.ps1',
    'WinPython_公務電腦使用包/WORKER_SELF_RECOVERY.ps1',
    'WinPython_公務電腦使用包/install_startup_shortcut.ps1'
  )
  foreach ($script in $scripts) {
    $tokens = $null; $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile((Resolve-Path $script), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { $errors | Format-List; exit 1 }
  }
  git diff --check
  ```

  Expected: Python compile、3 個 PowerShell parser 與 diff check 全部成功。

- [ ] **Step 2: 跑完整測試**

  Run: `py -m unittest discover -s tests -v`

  Expected: 全套 PASS，0 failures/errors；記錄實際 test count，不沿用舊數字。

- [ ] **Step 3: 獨立 review 完整 diff**

  逐項檢查：source/generated boundary、heartbeat thread lifecycle、atomic writes、token boundary、timezone、GUI/Tk thread safety、exact process identity、PID reuse、CIM timeout、activity/update gating、10 分鐘限流、`-SkipScheduledTask`、secret/package hygiene。Critical/Important findings 必須以 regression test 修正並重跑 Step 1/2。

- [ ] **Step 4: 產生單一版本並建立兩套 package**

  Run:

  ```powershell
  $version = Get-Date -Format 'yyyy.MM.dd.HHmm'
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_all_packages.ps1 -Version $version
  ```

  Expected: source `VERSION.txt`、UPDATE version、NAS `VERSION.txt`、public ZIP internal `VERSION.txt` 與 release ZIP internal `VERSION.txt` 全等於 `$version`；兩份 SHA 檢查成功。

- [ ] **Step 5: 檢查 package 內容與提交範圍**

  展開 build ZIP，確認包含 `worker_health.py`、`WORKER_SELF_RECOVERY.ps1`、更新後 updater/installer/template，且不包含 `.env`、artifacts、task JSON、logs、profiles。`git status --short` 不得 stage `.codex/`、`NAS包(舊版)/` 或使用者舊計畫。只明確 `git add --` 本計畫列出的 source、tests、generated package/version assets 與本計畫文件。

- [ ] **Step 6: commit、push、發布並做三重 readback**

  Commit message: `feat: add layered worker self recovery`

  Run:

  ```powershell
  git push origin master
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/publish_ambulance_return_release.ps1 -Version $version
  gh release view "ambulance-return-$version" --repo seaflun/ambulance-return-bot --json tagName,targetCommitish,assets
  ```

  從 GitHub 重新下載 `ambulance-return-version.txt`、public ZIP 與 SHA file；確認 remote version、ZIP internal `VERSION.txt`、published SHA、downloaded SHA 與 release target commit 全部一致。

- [ ] **Step 7: 部署 NAS heartbeat route，僅在具備 write authority 時執行**

  若本機已有經驗證的 NAS deployment write path，將 generated `UPDATE/NAS包` 同步到 `/docker/ambulance_return_bot/`，保留 NAS `.env` 與 artifacts，再以受限帳號重啟：

  ```powershell
  ssh -i "$env:USERPROFILE\.ssh\id_ed25519_ambulance_nas" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=8 codex_restart@100.114.126.58 "sudo -n /usr/local/bin/docker restart ambulance-app-1"
  Start-Sleep -Seconds 15
  Invoke-RestMethod -Uri 'http://100.114.126.58:8080/status' -TimeoutSec 20
  ssh -i "$env:USERPROFILE\.ssh\id_ed25519_ambulance_nas" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=8 codex_restart@100.114.126.58 "sudo -n /usr/local/bin/docker ps --format '{{.Names}} {{.Status}}'"
  ```

  Expected: `/status` healthy、`ambulance-app-1` healthy、`POST /worker/heartbeat` 不再 404。若只有 restart/list 權限而無 deployment write 權限，停止在部署前並明確回報「NAS bundle 已建好，尚需人工同步」，不得假裝新 route 已上線。

- [ ] **Step 8: 明日公務電腦重啟後啟用與驗證**

  使用者到公務電腦重新啟動 Windows 後：先確認 startup shortcut 啟動既有版本，再由 NAS 重送新 remote-update command。安裝成功後確認 `AmbulanceReturnWorkerWatchdog` 存在、每分鐘 last-run 正常、`worker_heartbeat.json` 每 10 秒更新、NAS 45 秒內顯示 online 且 heartbeat version 等於 `$version`。

- [ ] **Step 9: 安全故障演練與最終交付**

  在沒有正式登打工作的空檔，只做以下可逆演練：讓 Worker thread 正常 return，確認 GUI 15 秒後重啟；停止整個 GUI，確認 watchdog 在 heartbeat stale 120 秒後只重啟 package launcher；建立 fresh busy/update marker，確認 watchdog 不介入。不得終止 Chrome、不得重啟 Windows、不得觸發正式四站 submit。

  最終回報：實作 commits、release tag/target、完整 test count、PowerShell parser 結果、public/NAS/version/SHA readback、NAS route/status、實機 heartbeat、排程 last-run，以及故障演練的 decision/reason。若明日實機步驟尚未執行，清楚標示為待現場完成，不將程式已發布等同於公務電腦已啟用。
