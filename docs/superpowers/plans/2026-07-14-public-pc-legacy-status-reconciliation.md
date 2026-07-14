# Public-PC 舊版儲存狀態校正 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓公務電腦新版啟動後，自動把本機仍保留、且只符合舊版「已按儲存但官方頁沒有成功提示」精準特徵的案件校正為成功並回報 NAS；同時讓人工確認立即更新 NAS，真正的登打錯誤仍保持失敗。

**Architecture:** 校正判定集中在 `JsonTaskStore`，在單一鎖與單次原子寫入內完成，保留既有事件並新增稽核事件。公務電腦 Web 啟動時以背景執行緒掃描最多 500 筆本機保留任務，只將實際有變更的原始 `task_id` 交給既有 pending-report 管線。人工確認則使用同一份更新後 payload 回報。NAS 不猜測 task ID、不重跑官方頁、不直接覆寫歷史檔。

**Tech Stack:** Python 3、Flask、`unittest`、JSON 原子檔案儲存、既有 Public-PC Worker API。

## Global Constraints

- 僅校正下列「舊狀態 + 精準訊息尾碼」配對；登入稽核文字可位於前綴，但尾碼必須完整相符：
  - `duty_work_log_waiting_confirmation` + `waiting_confirmation: 已按下儲存，但未收到儲存成功回應；請人工確認。`
  - `vehicle_mileage_waiting_confirmation` + `waiting_confirmation: 已填寫車輛里程並按下儲存；未偵測到確認視窗，尚未確認伺服器已儲存。`
  - `disinfection_waiting_confirmation` + `waiting_confirmation: disinfection items updated=<正整數>; save response not confirmed.`
  - `consumables_failed` + `耗材儲存未取得明確成功回應：未出現確認訊息`
- 只處理沒有 `vehicle_results` 或其內容為空的舊單一彙總結果；多車逐車結果一律不推測。
- 缺駕駛、找不到案件或車輛、例外、timeout、部分更新、其他失敗訊息與任何非精準配對都不可校正。
- 校正不得開啟或重送工作、里程、耗材、消毒官方頁；只處理本機任務狀態與 NAS 報告。
- 必須保留原本 `events` 與 `site_attempts`，另加一筆可讀的 `legacy_silent_save_reconciled` 稽核事件；不得刪除歷史錯誤紀錄。
- 校正必須冪等：第二次執行不得再次修改檔案、增加事件或重送 NAS。
- 每個任務的多站校正與 overall/queue 更新須在同一把 store lock、同一次原子 `save_payload` 完成。
- 僅當所有實際需要的站別都成功時，才將 overall 設為 `desktop_fast_completed`；若仍有真正失敗，保留原 overall 失敗狀態。
- NAS 離線時沿用現有 pending report 機制；不得因回報失敗回滾本機校正。
- 不觸碰使用者未追蹤的 `.codex/`、`NAS包(舊版)/` 與 `docs/superpowers/plans/2026-06-29-nas-entry-implementation.md`。

---

### Task 1: 以 TDD 建立資料層精準校正

**Files:**
- Modify: `tests/test_task_store.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_store.py`

- [ ] **Step 1: 寫下會失敗的完整舊版校正測試**

  在 `JsonTaskStoreTests` 增加 fixture/helper 建立無加油、四站舊版結果，並測試：

  - 四個精準配對分別轉成 `duty_work_log_saved`、`vehicle_mileage_saved`、`consumables_saved`、`disinfection_saved`。
  - 訊息前方帶登入稽核前綴仍可辨識。
  - 診斷欄位被清空、`update_context` 移除、既有事件與 attempts 完整保留。
  - 一次只新增一筆 `legacy_silent_save_reconciled` 稽核事件，事件 detail 列出被修正站別。
  - 四站全成功時 overall 為 `desktop_fast_completed`。
  - 回傳 `(payload, changed)`，第一次 `changed=True`、第二次 `changed=False`，第二次事件數與檔案內容不變。

- [ ] **Step 2: 寫下安全邊界的失敗測試**

  使用 table-driven subtests 驗證以下情況維持原狀且 `changed=False`：狀態不符、訊息只差一字、消毒數量為 0 或非數字、缺駕駛、車輛不符、timeout、非空 `vehicle_results`。另建混合案件，只校正精準站別並保留一個真正失敗站與 overall 失敗。

- [ ] **Step 3: 執行測試並確認 RED**

  Run: `WinPython_公務電腦使用包/python/python.exe -m unittest tests.test_task_store.JsonTaskStoreTests.test_reconcile_legacy_silent_save_results -v`

  Expected: FAIL/ERROR，因 `reconcile_legacy_silent_save_results` 尚未存在或未符合規則。

- [ ] **Step 4: 實作最小校正邏輯**

  在 `task_store.py`：

  - 定義不可變規則表／嚴格消毒 regex，將四個舊狀態映射到四個 saved 狀態。
  - 新增 `JsonTaskStore.reconcile_legacy_silent_save_results(task_id: str) -> tuple[dict[str, Any], bool]`。
  - 在 store lock 中讀取 payload；只處理頂層站別、空的 `vehicle_results` 與精準尾碼。
  - 對已辨識站別設定 saved 狀態與可讀校正 detail，清除 `DIAGNOSTIC_FIELDS` 與 `update_context`，但不改 attempts/舊 events。
  - 無變更時直接回傳，不呼叫 `save_payload`。
  - 有變更時新增單一稽核事件；若 `_is_fully_done(payload)`，直接更新 overall/queue 為完成但避免再製造第二筆重複校正事件；最後只 save 一次。

- [ ] **Step 5: 執行 targeted tests 並確認 GREEN**

  Run: `WinPython_公務電腦使用包/python/python.exe -m unittest tests.test_task_store.JsonTaskStoreTests.test_reconcile_legacy_silent_save_results tests.test_task_store.JsonTaskStoreTests.test_reconcile_legacy_silent_save_results_rejects_near_matches tests.test_task_store.JsonTaskStoreTests.test_reconcile_legacy_silent_save_results_keeps_explicit_failure -v`

  Expected: PASS。

- [ ] **Step 6: 執行資料層回歸測試並提交**

  Run: `WinPython_公務電腦使用包/python/python.exe -m unittest tests.test_task_store -v`

  Expected: PASS。

  Commit only the two Task 1 files with message: `fix: reconcile legacy silent save results`

---

### Task 2: 人工確認同步完成 overall 並即時回報 NAS

**Files:**
- Modify: `tests/test_task_store.py`
- Modify: `tests/test_web_app.py`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/task_store.py`
- Modify: `WinPython_公務電腦使用包/app.py`

- [ ] **Step 1: 寫下人工確認後完成 overall 的失敗測試**

  建立僅剩一個 waiting-confirmation 站別的任務，呼叫 `mark_site_completed()`，斷言站別為 `completed_by_user`、overall 為 `desktop_fast_completed`，且 queued/claimed queue 轉成 completed。另測試仍有失敗站時 overall 不被改為成功。

- [ ] **Step 2: 寫下 Flask route 回報的失敗測試**

  在 `tests/test_web_app.py` patch `report_public_pc_task_event`：

  - 合法人工確認成功時只呼叫一次，payload 是 store 已保存的新狀態，action 為 `人工確認站別完成：<站名>`。
  - token 錯誤、404 與 409 不得呼叫回報。

- [ ] **Step 3: 執行測試並確認 RED**

  Run: `WinPython_公務電腦使用包/python/python.exe -m unittest tests.test_task_store.JsonTaskStoreTests.test_manual_site_completion_finishes_overall_status tests.test_web_app.WebAppTests.test_manual_complete_reports_updated_public_pc_payload -v`

  Expected: FAIL，現行 store 不更新 overall、route 丟棄回傳 payload。

- [ ] **Step 4: 實作人工確認的原子 overall 更新與 route 回報**

  - 在 `mark_site_completed()` 原本事件寫入後、單次 `save_payload` 前，以 `_is_fully_done(payload)` 判斷並更新 `desktop_fast_completed`；仍有失敗則不動 overall。
  - 在 `complete_site()` 保留 `mark_site_completed()` 回傳 payload；僅成功時呼叫 `report_public_pc_task_event(payload, f"人工確認站別完成：{site_display_name(site_key)}")`。
  - 既有 403/404/409 行為與安全 token 邊界維持不變。

- [ ] **Step 5: 執行 targeted 與回歸測試並提交**

  Run: `WinPython_公務電腦使用包/python/python.exe -m unittest tests.test_task_store tests.test_web_app.WebAppTests.test_waiting_confirmation_shows_manual_confirmation_without_blind_retry tests.test_web_app.WebAppTests.test_manual_complete_reports_updated_public_pc_payload tests.test_web_app.WebAppTests.test_manual_complete_does_not_report_conflict -v`

  Expected: PASS。

  Commit only the four Task 2 files with message: `fix: report manual public-pc confirmations`

---

### Task 3: 公務電腦啟動時背景掃描並回寫舊案件

**Files:**
- Modify: `tests/test_web_app.py`
- Modify: `WinPython_公務電腦使用包/app.py`

- [ ] **Step 1: 寫下掃描 helper 的失敗測試**

  建立三種本機任務：可校正、真正失敗、已校正。patch `report_public_pc_task_event` 後呼叫掃描 helper，斷言：

  - 最多呼叫 `store.list_recent(limit=500)` 一次。
  - 只針對實際 changed 的任務，以原始 task ID 與 action `舊版無提示儲存狀態自動校正` 回報一次。
  - 個別不存在／損壞 task 不妨礙其他任務，錯誤只輸出安全 log。
  - 再掃一次不重送。

- [ ] **Step 2: 寫下啟動 gating 與非阻塞測試**

  - `PUBLIC_PC_REPORT_ENABLED=false` 時不建立 reconciliation thread。
  - 啟用時 `run_web_app()` 在 serve 前呼叫一次 `start_public_pc_legacy_reconciliation()`。
  - starter 建立 daemon thread 並立即返回；worker 內部例外不終止 Web 啟動。

- [ ] **Step 3: 執行測試並確認 RED**

  Run: `WinPython_公務電腦使用包/python/python.exe -m unittest tests.test_web_app.WebAppTests.test_reconcile_legacy_public_pc_tasks_reports_only_changed_tasks tests.test_web_app.WebAppTests.test_web_startup_starts_legacy_reconciliation_only_for_public_pc -v`

  Expected: FAIL/ERROR，helper 與 startup hook 尚未存在。

- [ ] **Step 4: 實作 bounded background reconciliation**

  在 `app.py`：

  - 新增 `reconcile_legacy_public_pc_tasks()`，讀取 `store.list_recent(limit=500)`，從每筆 payload 取合法 task ID，再呼叫 store 校正方法；每筆獨立 catch `FileNotFoundError`、`KeyError`、`TypeError`、`ValueError`、`OSError`，不猜 task ID。
  - changed 才呼叫既有 `report_public_pc_task_event()`；該函式本身負責立即 POST 或 pending queue。
  - 新增 `start_public_pc_legacy_reconciliation()`，僅於 reporting enabled 時建立具固定名稱的 daemon thread。
  - 在 `run_web_app()` 完成 credential relay migration 後、開始 serve 前呼叫 starter；不等待掃描完成。

- [ ] **Step 5: 執行 Web targeted tests 並確認 GREEN**

  Run: `WinPython_公務電腦使用包/python/python.exe -m unittest tests.test_web_app.WebAppTests.test_reconcile_legacy_public_pc_tasks_reports_only_changed_tasks tests.test_web_app.WebAppTests.test_reconcile_legacy_public_pc_tasks_is_idempotent tests.test_web_app.WebAppTests.test_web_startup_starts_legacy_reconciliation_only_for_public_pc tests.test_web_app.WebAppTests.test_public_pc_report_is_queued_on_failure_and_flushed_on_next_success -v`

  Expected: PASS。

- [ ] **Step 6: 執行 Web 全檔回歸並提交**

  Run: `WinPython_公務電腦使用包/python/python.exe -m unittest tests.test_web_app -v`

  Expected: PASS。

  Commit only the two Task 3 files with message: `fix: reconcile public-pc reports on startup`

---

### Task 4: 全面驗證、打包發布與實機回寫

**Files:**
- Modify: `WinPython_公務電腦使用包/VERSION.txt`
- Generated/synced by existing release scripts: `UPDATE/WinPython_公務電腦使用包/**`
- Generated/synced by existing release scripts: `UPDATE/NAS包/**`

- [ ] **Step 1: 靜態與完整測試**

  Run:

  - `WinPython_公務電腦使用包/python/python.exe -m py_compile WinPython_公務電腦使用包/app.py WinPython_公務電腦使用包/ambulance_bot/task_store.py`
  - `WinPython_公務電腦使用包/python/python.exe -m unittest discover -s tests -v`
  - `git diff --check`

  Expected: 全部 PASS，無 whitespace error。

- [ ] **Step 2: 獨立 code review 與必要修正**

  依完整 spec 檢查精準匹配、冪等、原子寫入、例外隔離、人工回報、非阻塞啟動與測試品質。Critical/Important findings 必須修正並重跑涵蓋測試，直到 review clean。

- [ ] **Step 3: 使用既有發布腳本建立新版本**

  依 `ambulance-return-workflow` 的 source-of-truth/release 指令產生當下 `2026.07.14.HHMM` 版本，重建 public-duty ZIP 與 NAS package。不得手工複製部分檔案取代發布腳本。

- [ ] **Step 4: 驗證來源、UPDATE 與 ZIP 一致後提交／推送／發布**

  逐項確認：source `VERSION.txt`、`UPDATE` 版本檔、ZIP 內 `VERSION.txt`、本機 SHA256、GitHub release target commit、遠端下載 ZIP SHA256 全部一致。僅明確 stage 本計畫列出的程式、測試、版本與腳本產物，不納入 Global Constraints 所列使用者檔案。

- [ ] **Step 5: 觸發公務電腦遠端更新並驗證實際結果**

  透過現有 NAS remote-update command 讓公務電腦安裝新版本，等待版本回報。確認 startup reconciliation 已執行並透過既有 Worker API 更新 NAS `/admin/public-pc`。

  Expected live outcome（前提是公務電腦仍保留三筆原 task JSON）：

  - 成功件由 8 變 11。
  - 失敗件由 5 變 2。
  - 四維路148、福山路三段476、華興路二段462號旁改為成功。
  - 新華路一段886號的缺駕駛與月桃路／月山路口的車輛不符仍保持失敗。

  若任一原 task JSON 已不存在，必須記錄「無本機 task JSON，未校正」，不得依地址或畫面猜 task ID；其餘可校正案件仍應完成。

- [ ] **Step 6: 最終交付證據**

  回報：實作 commit、release tag、完整測試數、public-duty/NAS 版本與 SHA256 readback、公務電腦安裝版本、NAS 成功／失敗件數，以及任何因缺少本機 task JSON 未能自動校正的案件。
