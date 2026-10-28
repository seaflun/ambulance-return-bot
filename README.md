# SinpoSmart - 救災救護Worker

SinpoSmart - 救災救護Worker is maintained as a Windows public-duty worker plus a NAS Flask task center.

## Layout

- `WinPython_公務電腦使用包`: source of truth for the public-duty runtime, including Flask app code, worker GUI, Selenium flow, templates, and `ambulance_bot`.
- Repository root: compatibility entrypoints, tests, release scripts, and documentation.
- `UPDATE/`: generated public-duty update assets and generated NAS deployment package.
- `NAS包/`: legacy local/generated folder. It is ignored by Git; build fresh NAS output with `scripts\build_nas_package.ps1`.

Root `app.py`, `worker.py`, `worker_gui.py`, `consumables_login.py`, `disinfect.py`, and `civilpower.py` are compatibility launchers that load runtime code from `WinPython_公務電腦使用包`.

## 值班 GUI 問題包

`POST /api/sinposmart/diagnostics/<sha256>` 沿用勤務事件的同步 token 驗證，接收已遮蔽的 ZIP 問題包。驗證雜湊、manifest、允許檔名、檔案數與容量後，原子保存至 `artifacts/sinposmart/diagnostics/<消防日>/`；相同 ZIP 重試只保留同一份，保存成功後才回覆相同雜湊。

值班後台的「問題包」區塊提供發生時間、來源電腦、番號、任務及下載連結。下載沿用既有管理頁唯讀範圍，以附件回傳並禁止快取。ZIP 上限 8 MiB、最多 80 個允許檔案、解壓總大小上限 16 MiB，不解壓到伺服器檔案系統。未遮蔽的舊包、憑證欄位、路徑穿越、加密 ZIP 或不符雜湊不予保存；本功能不自動刪除歷史問題包。

這項功能需另外更新 SinpoSmart 值班 GUI。NAS 只接收診斷，不執行公務網站登打，也不修改正式勤務或通報紀錄。

## Verify

```powershell
py -m py_compile app.py worker.py worker_gui.py consumables_login.py disinfect.py civilpower.py _runtime_loader.py
py -m unittest discover -s tests -v
```

Local public-duty app:

```text
http://127.0.0.1:8090/app
```

## Public-Duty Package

Build the public-duty update package:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_public_duty_package.ps1
```

The build uses `WinPython_公務電腦使用包` as the package source and writes release/update assets under `UPDATE/`.

## NAS 登打專用入口

NAS 可用獨立子網域，或區網 IP 加專用連接埠提供救災／救護登打。兩個埠由現有同一個
Waitress／Flask 程序與 Worker 佇列處理，不另開第二個 Flask 程序或容器共寫 JSON。

- 使用子網域時，在 NAS `.env` 的 `TASK_ENTRY_HOSTS` 設定實際專用主機名稱，可用逗號分隔；可選填 `主機:連接埠` 嚴格比對，不填協定或路徑。純主機會限制該主機的所有埠，不可將原管理 IP 以純主機形式加入。
- NAS DNS／反向代理將該網域導向現有 8080 服務，保留原始 `Host`。連線來源須在 NAS／反代限制為區網；網域名稱與程式路由限制本身不是登入驗證或網路防火牆。沒有另外授權時，不新增公開網際網路入口。
- 專用網域根路徑會導向救災／救護選單；只允許登打、查案、任務明細／編輯／重試與必要靜態資源，首頁、管理後台及 Worker API 會回覆 403。
- 原管理／Worker 位址保持原樣，**不可**列入 `TASK_ENTRY_HOSTS`；Worker 仍用原 `WORKER_SERVER_URL` 及 `WORKER_TOKEN` 領取與回報任務。
- 多台電腦的案件帶入以一次性、不含案件資料的 cookie token 隔離；有效期五分鐘。同案送出沿用救災按案件、救護按案件及車輛的查重規則，並將查重與建立串行化。
- 專用入口沿用本機操作：救護建立後先確認「是否已結案」，再按登打；救災建立後可直接按登打。建立滿 10 分鐘仍未開始時，沿用自動排隊備援；實際登打均由公務電腦 Worker 執行。原管理入口的立即排隊流程不變。
- NAS 設定 `TASK_ENTRY_URL` 為完整專用網址，新版 GUI 會在 NAS 身分確認後，以 Worker 驗證讀取 `/worker/task-entry-settings` 並比對 instance_id，再決定開 NAS 或本機頁。GUI 不需要透過更新器覆寫 `.env`。本機明填的 `TASK_ENTRY_URL` 優先。
- 使用有效 NAS 入口時，新版 GUI 啟動、按鈕與 Chrome 修復都不啟動本機 8090；Worker、查案與帳密同步照常運作。舊 NAS 回 404、未設定入口或讀取失敗時可回原本機備援；NAS 明確回報無效設定或身分不符時會提示，Worker 仍啟動，並阻擋本機回退。
- 設定無效網址時 GUI 會提示錯誤，Worker 仍可運作；不會退回開啟本機服務。`DESKTOP_WEB_OPEN_BROWSER=false` 可取消自動開頁，手動按鈕仍可開 NAS。

驗收前需確認兩個獨立瀏覽器交錯帶入互不干擾、同案同時送出只排一次、Worker 離線時任務保留排隊、
專用網域直接輸入 `/` 及 `/admin/*` 不會進入管理頁；再確認原管理／Worker 位址不受影響。
使用同一 NAS 瀏覽器的多個分頁，仍建議一次完成一筆帶入表單，再選下一筆。

無現成 DNS 時，可啟用 NAS 套件的 `compose.entry.yml`：確認 LAN IP 並設定
`TASK_ENTRY_LAN_IP`，以原 Compose project 同時套用 `compose.yaml` 與 `compose.entry.yml`。
此 NAS 現有 project 為 `ambulance`；在 `/volume1/docker/ambulance_return_bot/` 的範例命令為
`TASK_ENTRY_LAN_IP=10.30.65.30 docker compose -p ambulance -f compose.yaml -f compose.entry.yml up -d app`。
這會重建同一 app 容器以加入只綁 `10.30.65.30:8091` 的額外映射，原 8080 與資料掛載保留；
單純 restart 不會套用新埠。部署後區網入口為 `http://10.30.65.30:8091/`。
專用埠以實際 SERVER_PORT 判定，即使修改 Host 也不能進其他後台；管理埠仍供原 Worker 使用。
`TASK_ENTRY_PORT` 必須與原 WEB_PORT 不同。上述 IP／project 需在各自 NAS 實測確認後使用。

NAS 入口可由目前正式基底整合、驗證後先部署，不依賴 GitHub release；不得以舊測試包覆蓋較新正式版本。
GUI 自動取得 NAS 入口與停用本機 8090 則需要公務電腦安裝含此功能的新版 Worker GUI；只更新 NAS 不會改變舊版 GUI。

Waitress 同程序多埠使用官方 `listen` 參數，技術依據見
[Waitress arguments](https://docs.pylonsproject.org/projects/waitress/en/stable/arguments.html#listen)。

## NAS Package

Build the NAS deployment package:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1
```

The generated NAS output is:

```text
UPDATE\NAS包
```

Deploy the generated contents to Synology:

```text
/docker/ambulance_return_bot/
```

Keep the NAS `.env` already on the NAS. The generated package does not include `.env`. After deployment, restart the `ambulance_return_bot` stack in DSM Container Manager.
