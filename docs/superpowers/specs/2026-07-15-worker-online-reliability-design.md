# 救護 Worker 在線與遠端更新可靠性設計

日期：2026-07-15
狀態：已由使用者確認採用推薦方案

## 問題與結論

本次已經確認 NAS 的遠端更新鏈路可以正常完成；因此「某一筆命令逾時且沒有 `worker_id`」代表當時該命令沒有被目標 Worker 成功拉取，不能推論 NAS 下達更新功能持續失效。

目前程式仍有四個可靠性缺口：

1. GUI 視窗存在，不代表 `worker.main()` 背景執行緒仍在執行；它正常 return 時目前不會自動重啟。
2. Worker 主迴圈把遠端更新輪詢放在帳密同步、案件查詢與自動接案之後；其中任一長工作都可能延遲命令收件。
3. 後台只有任務事件，沒有獨立的 Worker 心跳，因此無法分辨「最後曾回報」與「現在仍在線」。
4. 啟動排程只在登入時啟動 GUI；VBS/batch 會脫離排程程序，排程本身無法監督 Worker thread 或 GUI 是否仍健康。

本設計的目標是讓公務電腦在不重開 Windows、不誤關 Chrome、不打斷勤務的前提下，持續可觀測、可接收更新命令，並能在 Worker 或 GUI 異常消失時安全復原。

## 決策

採用四層防護，而不改成 Windows Service：

1. NAS 可驗證的 Worker control heartbeat，作為唯一在線判定與遠端更新命令收件通道。
2. 與長工作分離的 control 協調器，優先確認命令已收到，但延後實際更新。
3. GUI 內的 Worker thread supervisor，處理 Worker loop 單獨停止。
4. 每分鐘一次、fail-closed 的 Windows watchdog，處理 GUI 消失或更新交易卡住。

同時加入 NAS 端點身分比對與受限切換，避免 LAN 位址可通卻指向舊容器或不同服務時，Worker 靜默停留在錯誤端點。

## 不可違反的安全邊界

- 公務電腦只能主動向 NAS 發出 HTTP 請求；不新增 NAS 主動連入、WinRM、RDP、遠端桌面或重開 Windows。
- 只允許啟動或停止完全屬於目前 `WinPython_公務電腦使用包` 的 GUI、Worker 和遠端更新 wrapper；不可依名稱模糊比對。
- 不得停止 Chrome、ChromeDriver、其他 Python、其他 PowerShell 或套件目錄外的程序。
- 有新鮮的手動勤務鎖、案件查詢 activity lease 或健康更新交易時，所有復原層只能記錄狀態，不得重啟。
- 程序身分、PID 重用防護、套件路徑、更新 Request ID 或交易 marker 任一項無法確認時，採 fail-closed：不停止、不刪除、不回復。
- 不將 `WORKER_TOKEN`、帳密、任務 payload、Chrome profile、截圖或本機 logs 寫入 Git、發行檔或 NAS 心跳資料。
- `WinPython_公務電腦使用包` 是唯一公務電腦來源；`UPDATE/NAS包` 只能由建置腳本產生。

## 在線、命令與路由的資料流

### 1. 合併的 Worker control heartbeat

Worker 啟動後建立一條獨立 control loop，每 10 秒執行一次，並在啟動時加入小幅隨機抖動，避免未來多台 Worker 同秒送出請求。每輪先以原子寫入更新 `%LOCALAPPDATA%\AmbulanceReturnBot\worker_heartbeat.json`，再以既有 `X-Worker-Token` POST 至 NAS 的 control endpoint。這一個回應同時回傳最新遠端更新命令；不再另外建立一條固定頻率的更新 GET 輪詢。

允許的狀態固定為：`starting`、`idle`、`busy`、`update_handoff`、`recovering`、`stopping`。資料只包含 Worker ID、版本、PID、執行模式、標準化套件路徑、活動類型、非敏感 busy reason、Request ID 與時間；NAS 以自己的接收時間判定新鮮度，不信任用戶端宣告的 online/offline。

NAS 以每台 Worker 最新一筆資料保存，不保留逐次心跳歷史。收到時間不超過 45 秒顯示「在線」，超過 45 秒顯示「離線」。後台須分開顯示：NAS 後台版本、最後心跳版本/時間/狀態、最後任務回報版本/時間。最後任務回報永遠不能再冒充在線證據。

### 2. 高優先 control 協調器

Control loop 與帳密同步、案件查詢和 Selenium 勤務分離。它在同一個每 10 秒 control request 中完成心跳與命令收件，責任只有三件事：

- 讓 NAS 儘快寫入 `worker_id`、`before_version` 與 `last_seen_at`，證明命令已由目標 Worker 收到。
- 將命令的安全等待理由更新為既有 `waiting_busy` 或 `waiting_idle`，讓管理頁可讀。
- 將最新命令以原子方式保留在本機非敏感 mailbox，交給主 Worker 在安全檢查通過時啟動更新。

協調器不直接操作檔案替換、不直接關閉 GUI，也不在勤務或案件查詢中啟動 updater。主 Worker 保留既有優先順序：勤務與案件完整性優先；只有不存在活動 lease、電腦閒置達既有門檻、且沒有更新 owner 時，才把命令轉成 `updating` 並移交背景更新 wrapper。

這項分離的預期效果是：即使 Selenium 查詢持續數分鐘，更新命令仍可在約 10 秒內顯示「已收到／等待勤務完成」，而非被誤判為公務電腦離線。既有 `/worker/remote-update` 保留給舊版 Worker 相容使用；新版不以它作固定週期的第二次請求。

### 3. NAS 路徑身分驗證與受限 failover

NAS 新增受 Worker Token 保護的輕量身分回應，提供不含秘密的穩定 instance ID、服務版本與部署資訊。Worker GUI 與 Worker 網路客戶端把既有 LAN 與 Tailscale 位址視為候選路徑：

- 兩個位址都可用且 instance ID 相同時，使用目前可達的優先路徑，並持續保存最近成功路徑。
- 首選路徑發生連線逾時、DNS/路由錯誤或連線拒絕時，才嘗試同身分的備援路徑；HTTP 403、409、400 等授權或協定錯誤不可藉由切換路徑掩蓋。
- 兩個位址回傳不同 instance ID 時，視為錯誤路徑，不提升該候選為可用路徑；GUI 與 NAS 管理面要留下明確診斷。
- 只有單一路徑可達且沒有可比對身分時，既有勤務功能可維持使用已明確設定的路徑，但遠端更新命令收件器顯示「路徑身分未驗證」並不會主動 claim 更新命令。

首批部署先讓 NAS 提供身分回應，再由可正常輪詢的公務電腦接收新版。這避免新版把舊 NAS 的 404 或錯誤代理靜默視為「沒有命令」。

### 4. GUI thread supervisor

GUI 每 5 秒檢查自身的 `ambulance-worker` thread。無論 `worker.main()` 是正常 return 或丟出例外，`_run_worker()` 都要留下停止時間、例外摘要與可讀狀態，而不是只有例外時才顯示「已停止」。

Supervisor 的判斷規則如下：

- thread 存活：不動作。
- thread 停止未滿 15 秒：等待，避免更新交接中的短暫停止被誤判。
- 偵測到有效手動勤務、案件查詢 activity 或健康更新 marker：不重啟，只顯示原因。
- 停止超過 15 秒且沒有保護條件：重新呼叫既有 GUI 啟動邏輯。
- 10 分鐘內已有 3 次 GUI 內重啟：停止本層重試並留下 rate-limit 診斷，交由外層 watchdog 或人工判讀。

GUI supervisor 不列舉或停止任何 Windows 程序；它只管理自己建立的 Python thread。

### 5. 外層 Windows watchdog

新增一次性 PowerShell 腳本，由目前互動使用者的 Task Scheduler 每分鐘執行，使用 named mutex 保證不重疊。它在每輪輸出可讀決策，但只有所有識別條件吻合時才採取破壞性動作。

判斷順序：

1. 本機心跳仍在 120 秒內：`no_action`。
2. 手動勤務、案件查詢 activity lease 仍新鮮：`no_action_busy`。
3. `remote_update_active.json` owner、PID 啟動時間、Request ID、nonce、script path、package path 與 phase freshness 均吻合：`healthy_update`。
4. 更新 phase 超過 10 分鐘且 exact owner 全部吻合：只處理那一個 updater，呼叫既有交易 recovery；資料損壞、多筆交易或 owner 不確定則 `fail_closed`。
5. 無有效更新且心跳超過 120 秒：再次確認套件內唯一 Worker/GUI 身分，才從 `RUN_WORKER_GUI_WINPYTHON.vbs` 重新啟動。

看門狗的破壞性恢復在 10 分鐘內最多 3 次。其狀態、decision 和時間寫入 `%LOCALAPPDATA%\AmbulanceReturnBot\self_recovery.json`，不保存 token、帳密、任務或完整 command line。腳本必須有 `-WhatIf` 與受測試控制的程序 snapshot 模式，以便在不停止真實程序的情況下驗證安全邊界。

### 6. 排程與更新交接

`install_startup_shortcut.ps1` 保留登入時啟動 GUI 的行為，另建立 `AmbulanceReturnWorkerWatchdog`：目前互動使用者、Limited run level、每分鐘觸發、MultipleInstances=IgnoreNew、hidden/noninteractive PowerShell，執行套件內的 watchdog。

遠端更新完成後，安裝腳本必須刷新 watchdog。既有 `-SkipScheduledTask` 只可略過舊的登入啟動排程，不可略過 watchdog；這使第一次從舊包升級後即可取得外層保護。

## 負載預算與資料保留

一台公務電腦只會發出每 10 秒一次的 control request，即每天 8,640 次、平均 0.1 requests/second。即使以每次請求與回應合計 2 KB 的保守估算，日流量約 17 MB；實際 payload 預期更小。NAS 每輪只原子覆寫一筆最新心跳，不寫逐次歷史，也不為每個成功心跳寫應用程式診斷日誌。

GUI 的 5 秒 supervisor 只讀自身 thread 狀態，沒有網路或程序列舉。每分鐘 watchdog 只在公務電腦本機啟動短暫的 `-NoProfile -NonInteractive` PowerShell；它不對 NAS 發請求、不掃描 Chrome，也不執行 Selenium。watchdog 的診斷檔採固定筆數與固定大小輪替，避免任何本機檔案無限累積。

## 失敗處理與使用者可見結果

| 情況 | NAS 管理頁 | 公務電腦行為 |
| --- | --- | --- |
| Worker 正常忙於勤務 | 在線、busy，更新等待勤務完成 | 不更新、不重啟 |
| Worker 正常忙於案件查詢 | 在線、busy，更新等待勤務完成 | 收件器持續回報，主工作完成後再評估 |
| Worker thread 正常 return | 心跳停止後最晚 45 秒離線 | GUI 15 秒後安全重啟 |
| GUI 整體消失 | 45 秒後離線 | watchdog 在心跳超過 120 秒的下一輪安全重啟 |
| 正常背景更新 | 在線、update_handoff | GUI/watchdog 都不搶先重啟 |
| 更新 owner 消失或交易卡住 | 更新狀態顯示失敗或待恢復 | 僅在 exact owner 成立時回復；其餘 fail-closed |
| LAN 指到不同 NAS | 顯示路徑身分不符 | 不以錯誤路徑 claim 更新命令 |

## 測試與驗收

實作採 test-first，所有新行為都要有先失敗再通過的測試。最低驗證範圍如下：

- 單一 control request 的 heartbeat schema、原子寫入、Token 驗證、命令回應、45 秒 online/offline 和管理頁版本分離；
- control 協調器即使主 Worker 正在長案件查詢時仍能回報命令已收到，且新版不會再做第二條固定週期的更新請求；
- 路徑切換僅對傳輸錯誤生效，身分不符或 4xx 不會被遮蔽；
- GUI 的正常 return、例外退出、15 秒 grace、busy/update 抑制與 3 次 rate limit；
- watchdog 的 healthy、busy、stale Worker、stale exact updater、PID 重用、foreign Python/PowerShell/Chrome、損壞 marker、CIM timeout 與 `-WhatIf`；
- 安裝腳本在一般、`-SkipScheduledTask` 和停用啟動器時對兩個排程的行為；
- 全套 Python 測試、PowerShell parser、build package、ZIP 內容、GitHub release SHA readback、NAS `/status` 和實機公務電腦演練。

實機驗收只能在沒有正式勤務的空檔執行：讓 Worker thread 正常 return、停止整個 GUI、建立新鮮 busy/update marker。成功標準是 thread 在約 15 秒後恢復、GUI 消失在約 3 分鐘內恢復、busy/update 期間零介入、更新命令在約 10 秒內可見收件狀態，且沒有 Chrome 或非本套件程序被終止。

## 不在本次範圍

- Windows Service、管理員權限常駐服務、WinRM、RDP、NAS 主動連入公務電腦與遠端重開 Windows。
- 四站登打流程、案件資料、患者/車輛分配、帳號鎖定、任務保留時間等既有功能規則。
- 以 GUI 視窗、本機網頁或過去任務事件作為 Worker 在線的替代證據。
