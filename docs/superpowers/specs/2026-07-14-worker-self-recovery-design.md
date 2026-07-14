# 救護 Worker 分層自救設計

日期：2026-07-14

## 目標

讓公務電腦的救護 Worker 在執行緒停止、GUI 消失或遠端更新卡住時，自動恢復正常輪詢，不再把重新啟動 Windows 當成日常復原手段。同時讓 NAS 後台清楚區分「最後案件回報」與「Worker 目前在線」，避免以舊案件事件誤判 Worker 仍健康。

## 已確認安全邊界

- 只允許停止或重啟目前救護套件目錄下的 Worker GUI、Worker 與 `REMOTE_UPDATE_PACKAGE.ps1` 更新程序。
- 不重新啟動 Windows，不停止其他 Python、PowerShell、Chrome 或 ChromeDriver。
- 勤務、手動登打、案件查詢或正常更新仍在執行時，自救程序不得介入。
- 無法確認程序身分、更新交易歸屬或勤務鎖狀態時一律不採取破壞性動作。
- `WinPython_公務電腦使用包` 仍是公務電腦執行來源；`UPDATE/NAS包` 只能由建置腳本產生。
- `.env`、帳密、工作資料、瀏覽器設定檔及本機執行紀錄不得進入 Git 或發行套件。

## 採用方案

採用分層自救，不改成 Windows Service：

1. Worker 內部心跳與 GUI 執行緒監督，處理 Worker 執行緒單獨停止。
2. Windows 工作排程每分鐘執行一次獨立看門狗，處理 GUI 整個消失或更新程序卡死。
3. NAS 儲存真實 Worker 心跳，後台獨立顯示在線狀態與最後案件回報。

此方案不需要新增 Windows 管理員常駐服務，仍可覆蓋本次「1453 本機網頁有回報，但 Worker 輪詢未恢復」的失敗型態。

## 架構與資料流

### 1. 真實 Worker 心跳

Worker 啟動後建立獨立背景心跳，不依賴主輪詢或 Selenium 任務是否正在執行。每 10 秒完成兩件事：

- 原子寫入 `%LOCALAPPDATA%\AmbulanceReturnBot\worker_heartbeat.json`。
- 使用既有 `X-Worker-Token` POST 至 NAS `/worker/heartbeat`。

心跳內容只包含 Worker ID、套件版本、執行模式、程序 PID、標準化套件路徑、狀態與時間，不包含帳密或案件內容。狀態值固定為 `starting`、`idle`、`busy`、`update_handoff`、`recovering` 或 `stopping`。

NAS 以原子檔保存各 Worker 最新心跳。最後心跳不超過 45 秒時顯示「在線」；超過 45 秒顯示「離線」，並保留最後時間與版本供判讀。案件事件仍保留原本的 `package_version`，但標籤改為「最後案件回報」，不再被當成心跳。

### 2. GUI 執行緒監督

Worker GUI 每 5 秒檢查 Worker 執行緒：

- 執行緒健康時不處理。
- 執行緒停止但存在有效勤務鎖、手動任務鎖或健康的更新接管標記時，只等待並更新可讀狀態。
- 執行緒停止且沒有上述保護條件時，等待 15 秒後重新啟動同一 GUI 內的 Worker 執行緒。

GUI 內的重啟紀錄採滑動視窗限制：10 分鐘內最多 3 次。達到上限後停止自動重啟，顯示「自救次數過多，等待 Windows 看門狗或人工處理」，避免高速重啟迴圈。

### 3. 更新階段與健康標記

`REMOTE_UPDATE_PACKAGE.ps1` 延用現有交易、備份、回復與 PID/啟動時間驗證，並在 `remote_update_active.json` 原子記錄：

- Request ID、owner PID、owner nonce 與程序啟動時間。
- `discovering_runtime`、`installing`、`validating`、`committing`、`rolling_back`、`restarting` 等階段。
- 階段開始時間及最後更新時間。

既有 CIM 查詢 5 秒逾時保留。正常更新只要 owner 程序仍吻合且階段未超過 10 分鐘，看門狗不得干預。

### 4. Windows 獨立看門狗

新增一次性執行的看門狗腳本，由目前使用者的 Windows 工作排程在登入後每分鐘執行。它先取得互斥鎖，避免兩次排程重疊，再依序判斷：

1. 勤務鎖、手動任務鎖或案件查詢鎖仍有效：不處理。
2. 更新 owner 與階段皆健康：不處理。
3. 更新 owner 已不存在，或同一階段超過 10 分鐘：只有在 PID、啟動時間、Request ID、更新腳本名稱及標準化套件路徑全部吻合時，才可停止該更新程序，接著呼叫現有交易回復流程。
4. 沒有有效更新，但本機 Worker 心跳超過 120 秒：再次確認沒有勤務鎖後，只停止完全匹配本套件路徑的 Worker GUI/Worker，並從 `RUN_WORKER_GUI_WINPYTHON.vbs` 重新啟動。
5. 找到多筆交易、交易損壞、程序查詢逾時或身分不明：不刪檔、不停止程序，只留下可讀診斷。

看門狗的恢復紀錄保存在 `%LOCALAPPDATA%\AmbulanceReturnBot\self_recovery.json`。10 分鐘內最多執行 3 次破壞性恢復；超過後只記錄診斷。紀錄寫入採原子替換，且不含秘密資料。

### 5. 排程安裝與第一次部署

`install_startup_shortcut.ps1` 保留現有登入啟動方式，另建立或更新 `AmbulanceReturnWorkerWatchdog` 排程。排程只以目前互動使用者及有限權限執行，不要求管理員系統服務。

套件更新成功後必須刷新看門狗排程。既有更新器呼叫新版安裝腳本時，即使使用 `-SkipScheduledTask` 跳過主啟動排程，也仍要刷新看門狗排程，確保從 1453/舊版升級的第一次更新就能安裝自救層。

目前已停止輪詢的公務電腦無法自行取得這個版本。新版本發布後，仍需現場重新啟動 Windows 一次；Worker 恢復輪詢後重新下達更新，安裝完成後自救機制才正式生效。

## NAS 後台呈現

「系統版本」區分三個來源：

- NAS 後台版本。
- 公務電腦最後心跳版本及最後心跳時間。
- 最後案件回報版本及案件回報時間。

遠端更新卡片的「目前版本」優先採用最新 Worker 心跳版本，不再以 NAS 後台版本代替公務電腦版本。Worker 離線時明確顯示「最後心跳版本」，避免把它解讀為目前仍在線。

## 錯誤處理與防誤殺

- 所有程序比對必須同時驗證 PID、程序啟動時間、完整套件路徑與預期腳本名稱；更新程序另驗證 Request ID。
- `Get-CimInstance` 或任何程序盤點逾時時直接結束本輪看門狗，不使用模糊名稱進行停止。
- 健康勤務鎖優先於離線心跳，避免長時間 Selenium 工作被誤判為故障。
- 健康更新優先於 GUI/Worker 心跳，避免正常替換檔案時被舊程序搶先重啟。
- 自救失敗不得刪除待回復交易；下次排程可在條件安全時重試。
- 看門狗日誌限制大小並輪替，不記錄 Token、帳密、案件資料或完整入口網址。

## 測試策略

每項行為先建立會失敗的測試，再修改執行程式：

- NAS 心跳 API 的 Token 驗證、原子保存、45 秒在線判斷與後台標籤。
- Worker 背景心跳在主輪詢忙碌時仍持續，NAS 失聯時不阻塞登打。
- GUI 執行緒停止後的 15 秒恢復、健康勤務/更新抑制及 3 次限制。
- 看門狗對正常勤務、正常更新、owner 消失、更新階段卡死、GUI 消失及損壞交易的決策。
- 外部 Python、PowerShell、Chrome 與不同套件路徑程序永遠不會成為停止目標。
- 工作排程安裝與更新刷新行為，包括 `-SkipScheduledTask` 路徑。
- 更新交易回復後 Worker 能重新產生心跳並再次取得 NAS 命令。

完成條件包括目標測試、完整單元測試、Python 編譯、PowerShell 語法與整合檢查、NAS/公務電腦套件建置、Git 差異檢查、提交推送、GitHub 發布，以及遠端版本檔、ZIP 內版本與 SHA256 三方一致。

## 不在本次範圍

- 不建立 Windows Service。
- 不新增 WinRM、RDP、NAS 主動連入公務電腦或遠端重新啟動 Windows。
- 不更動四站登打、患者/車輛分配、網站儲存判斷或帳號選擇規則。
- 不將本機網頁是否可開啟當成 Worker 輪詢健康證據。
