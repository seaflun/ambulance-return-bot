# NAS 網頁查詢器操作手冊

## 目標

在 Synology NAS 的 Container Manager 啟動 `ambulance_return_bot`，用內網網址操作救護回程網頁查詢器：

```text
http://NAS_IP:8080/app
```

第一階段只使用內網，不設定外網公開、HTTPS、DSM 反向代理或 Cloudflare Tunnel。

## 部署前檢查

1. NAS 專案資料夾內要有這些檔案：
   - `app.py`
   - `compose.nas.yml`
   - `requirements.txt`
   - `.env.example`
   - `ambulance_bot/`
   - `templates/`
2. 專案若用 Git 同步，確認目前版本至少包含 baseline commit：
   - `a4a76c1 Initial ambulance return bot baseline`
3. `.env` 不進 Git；實際帳密只放 NAS 專案資料夾的 `.env`。

## .env 設定

在 NAS 專案資料夾把 `.env.example` 複製成 `.env`，至少填：

```dotenv
WEB_HOST=0.0.0.0
WEB_PORT=8080
ARTIFACTS_DIR=artifacts

DUTY_ACCOUNT=你的消防勤務帳號
DUTY_PASSWORD=你的消防勤務密碼

CASE_LOOKUP_INTERVAL_SECONDS=300
CASE_LOOKUP_SCHEDULER_ENABLED=true
```

如需 LINE 通知，再填：

```dotenv
LINE_CHANNEL_ACCESS_TOKEN=你的 LINE Messaging API token
LINE_CHANNEL_SECRET=你的 LINE channel secret
LINE_TO_USER_IDS=Uxxxxxxxx,Uyyyyyyyy
```

NAS 使用 `compose.nas.yml` 時，`SELENIUM_REMOTE_URL` 由 compose 設成 `http://selenium:4444/wd/hub`，不需要在 `.env` 重複填。

## Container Manager 啟動

1. DSM 開啟 Container Manager。
2. 建立 Project，路徑選 `ambulance_return_bot` 專案資料夾。
3. Compose 檔選 `compose.nas.yml`。
4. 啟動後應有兩個服務：
   - `selenium`
   - `app`
5. 確認 port mapping：
   - NAS `8080` 對 container `8080`

啟動後先看 log：

- `selenium` log 應看到 Selenium Grid / Chromium ready。
- `app` log 應看到 pip install 完成，接著進入 `python -u app.py`。
- 如果 app log 只有 pip 訊息但沒有服務啟動，優先檢查 command 是否被 DSM 改壞。

## Web 驗證

用同一個內網的電腦或手機開：

```text
http://NAS_IP:8080/status
```

應回傳 JSON，包含：

```json
{"ok": true}
```

再開：

```text
http://NAS_IP:8080/app
```

應看到救護回程網頁表單。

## 日常操作

1. 開 `http://NAS_IP:8080/app`。
2. 等背景查詢完成，或按「查詢最近 6 小時」。
3. 案件清單應顯示：
   - `緊急救護-事由 - 地址`
4. 按案件右側「帶入這筆」。
5. 確認表單自動帶入：
   - 案發地址
   - 案件時間
   - 回程時間
   - 事由
   - 服勤人員/司機下拉選項
6. 補里程、傷病患、耗材、消毒紀錄。
7. 建立任務。

注意：系統可開頁與預填，但外部網站最後儲存/送出仍需人工確認。

## 檔案驗證

案件查詢成功後，NAS 專案資料夾會更新：

```text
artifacts/cases/latest.json
```

帶入案件後會更新：

```text
artifacts/cases/selected.json
```

建立任務後會新增：

```text
artifacts/tasks/<task_id>.json
```

這些都是 runtime 產物，不應提交到 Git。

## 常見問題

- `http://NAS_IP:8080/app` 打不開：
  - 確認 Container Manager 的 `app` 服務是 Running。
  - 確認 NAS 防火牆允許 8080。
  - 確認 port mapping 是 `8080:8080`。

- `/status` 打得開，但查不到案件：
  - 看 `app` log 是否有 Selenium connection error。
  - 看 `selenium` log 是否 ready。
  - 確認 `.env` 的 `DUTY_ACCOUNT` / `DUTY_PASSWORD` 正確。

- app log 顯示 Selenium ready 但 session 建立失敗：
  - 等 1-2 分鐘再查一次。
  - 確認 `selenium` 服務沒有重啟循環。
  - 確認 `shm_size` 保持 `4gb`。

- LINE 沒通知：
  - 確認 `.env` 有 `LINE_CHANNEL_ACCESS_TOKEN` 和 `LINE_TO_USER_IDS`。
  - LINE 失敗不應影響網頁查詢器，先以 `/app` 和 artifacts 驗證主流程。
