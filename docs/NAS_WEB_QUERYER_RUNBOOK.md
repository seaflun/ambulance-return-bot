# NAS 任務中心操作手冊

目標架構是 NAS 當任務中心，公務電腦 worker 負責查詢案件與開本機 Chrome 預填四個網站。

## 固定入口

- 手機/平板入口：`http://100.114.126.58:8080/app`
- 公務電腦 worker 連 NAS：`http://10.30.65.30:8080`
- NAS LAN IP：`10.30.65.30`
- NAS Tailscale IP：`100.114.126.58`

## NAS 責任

NAS 只負責：

- Flask 網頁。
- 任務 JSON。
- 最新案件清單。
- worker API。
- LINE 通知與狀態查詢，若有設定。

NAS 不負責：

- 不跑 Selenium。
- 不啟動 Chrome。
- 不保存四站帳密。
- 不做政府網站登打。

## NAS `.env`

NAS 專案資料夾內 `.env` 至少需要：

```env
WEB_HOST=0.0.0.0
WEB_PORT=8080
ARTIFACTS_DIR=artifacts
TASK_EXECUTION_MODE=worker_queue
CASE_LOOKUP_SCHEDULER_ENABLED=false
WORKER_TOKEN=同公務電腦worker
```

不要把四站帳密、OpenAI API key、runtime profiles 放 NAS `.env`。

## DSM Container Manager

1. 開 DSM。
2. 開 Container Manager。
3. 進「專案」。
4. 使用專案內 `compose.nas.yml` 建立或更新 Stack。
5. 啟動後看 app log，應看到 Flask/Waitress 啟動。

新版 `compose.nas.yml` 只有 app service，沒有 selenium service。

## 公務電腦 Worker

公務電腦 `.env` 至少需要：

```env
WORKER_SERVER_URL=http://10.30.65.30:8080
WORKER_TOKEN=同NAS
WORKER_POLL_SECONDS=10
CASE_LOOKUP_INTERVAL_SECONDS=300
WORKER_USE_LOCAL_CHROME=true
CHROME_PROFILE_EMAIL=sinpo666@gmail.com
SELENIUM_PROFILE_ROOT=C:\Users\User\AppData\Local\ambulance_return_bot
WORKER_CHROME_DEBUGGER_PORT=9223
```

啟動：

```powershell
run_worker_forever.vbs
```

這是無小黑窗模式，適合放到桌面捷徑或開機啟動。

備用啟動：

```powershell
run_worker_forever.bat
```

啟動後會開 Windows worker 程式視窗。視窗會同時啟動背景 worker，並提供入口按鈕測試各站登入與 saved worker credentials。

在個人電腦測試時，如果不在公務內網，可以在視窗中切換成 Tailscale：

```text
http://100.114.126.58:8080
```

單次測試：

```powershell
run_worker_once.bat
```

不需要面板時才使用：

```powershell
run_worker_headless.bat
```

需要舊版本機網頁面板時使用：

```powershell
run_worker_web_panel.bat
```

## 日常操作

1. 手機或平板開 `http://100.114.126.58:8080/app`。
2. 案件列表由公務電腦 worker 回傳。
3. 若要立即更新，按「查詢」；NAS 會記錄 `case_lookup_requested`。
4. worker 下一輪輪詢會查詢案件並回傳 NAS。
5. 選案件「帶入這筆」。
6. 補里程、車輛、司機、傷病患、耗材、消毒資料。
7. 建立任務。
8. 任務進入 `queued_for_worker`。
9. 公務電腦 worker 領取任務後開本機 Chrome 預填。

## 驗證

NAS：

```text
http://100.114.126.58:8080/status
http://10.30.65.30:8080/status
```

Worker：

- 無任務時 `run_worker_once.bat` 應顯示 no queued task，除非剛好有手動查詢要求。
- 手機按查詢後，worker log 應出現 manual case lookup requested。
- 案件成功回傳後，NAS 會更新 `artifacts/cases/latest.json`。

## 注意

- 四站第一版只預填，不按最後儲存/送出。
- 驗證碼不要破解或繞過；遇到驗證碼時改人工接手。
- 每次改程式並完成測試後，要重開 worker，並在回覆中說明是否已重開。
