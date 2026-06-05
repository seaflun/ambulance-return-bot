# 救護回程網頁版 App + LINE Bot

這是手機網頁版 App 加後端任務服務。主要入口是手機 Safari 開 `/app`，LINE Bot 保留為通知與狀態查詢。

## 網頁版 App

本機啟動後開：

```text
http://localhost:8080/app
```

手機測試時，用 Cloudflare Tunnel 或 ngrok 把 `http://localhost:8080` 對外公開，再用 iPhone Safari 開 tunnel URL。可用「加入主畫面」當網頁 App。

第一版行為：

- 建立救護回程任務
- 任務 JSON 保存到 `artifacts/tasks/`
- 任務詳情顯示四站狀態
- 提供每站複製文字與人工完成按鈕
- 啟動四站流程後，後端會優先用 Selenium 在執行 Flask 的這台 Windows 電腦上開啟 Chrome
- Selenium 會嘗試填入可辨識的登入欄位與任務欄位，但不會按最後送出
- 同時會在 `artifacts/local_desktop/` 寫入本次任務摘要文字檔
- 第一版只建立預填計畫與狀態，不自動送出

## 使用方式

LINE 傳：

```text
範例
```

機器人會回傳格式。正式呼叫格式：

```text
救護回程
車輛:91A1
司機:王小明
里程:12345
案件時間:14:20
回程時間:15:05
耗材:口罩=2,手套=2,氧氣面罩=1
消毒:車內、擔架、監視器接觸面完成消毒
工作紀錄:救護返隊完成補登
```

## LINE Bot 目前做什麼

- 接收 LINE webhook：`POST /line/webhook`
- 支援 `範例`、`狀態`、`救護回程` 指令
- 解析車輛、司機、里程、時間、耗材、消毒、工作紀錄
- 產生任務 JSON 到 `artifacts/tasks/`
- 回覆任務摘要與四站狀態
- 四站 adapter 已分好：
  - 車輛里程
  - 一站通耗材
  - 緊急救護消毒
  - 消防勤務工作紀錄

## 重要限制

第一版還沒有真正送出四個網站。第 2、3 站有驗證碼，且四站欄位 selector 需要在實際登入頁面逐站驗證。現在的架構已經把網頁 App、webhook、任務、LINE 回報與 adapter 分開，下一步可以逐站接 Selenium。

程式不會破解驗證碼，也不會自動按最後送出。

## 本機執行

```powershell
cd I:\我的雲端硬碟\專案\IOS\ambulance_return_bot
py -m pip install -r requirements.txt
copy .env.example .env
py app.py
```

LINE Developers 後台 webhook URL 設成：

```text
https://你的公開網址/line/webhook
```

如果在家用電腦測試，可以先用 Cloudflare Tunnel 或 ngrok 把 `http://localhost:8080` 暫時公開。

## NAS 執行

Synology Container Manager 可以使用 `compose.nas.yml`。完整操作手冊在：

```text
docs/NAS_WEB_QUERYER_RUNBOOK.md
```

先把 `.env.example` 複製成 `.env` 後填入：

- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- 四站帳號密碼

啟動後確認：

- `GET /status` 回傳 `ok: true`
- `GET /app` 可以開啟手機網頁表單
- 按「查詢最近 6 小時」後 `artifacts/cases/latest.json` 更新
- 按「帶入這筆」後 `artifacts/cases/selected.json` 更新
- LINE 傳 `範例` 會收到格式
