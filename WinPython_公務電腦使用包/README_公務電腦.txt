# SinpoSmart - 救災救護Worker 公務電腦使用包

## 第一次設定

1. 把 `.env.example` 複製成 `.env`。
2. 填入 `WORKER_TOKEN`，要和 NAS 的值相同。
3. `SELENIUM_PROFILE_ROOT` 預設使用 `%LOCALAPPDATA%\ambulance_return_bot`，不要放到 Google Drive 資料夾；`chrome_profile` 只是舊快取資料，不是四站登打必要條件。
4. 舊 runtime profiles 預設超過 4 小時且未被 Chrome 使用時會清理；登打網頁預設 10 分鐘後自動關閉。
5. 遇到 Chrome/ChromeDriver 工作階段失效時，該站會自動建立新的瀏覽器工作階段並重試一次；若要停用可設定 `SELENIUM_SESSION_RECOVERY_ATTEMPTS=0`。
6. Worker GUI 預設啟動後自動縮到系統匣；若要停用可設定 `WORKER_GUI_START_MINIMIZED=false`。
7. 執行 `SETUP_WINPYTHON.bat` 安裝套件、檢查環境，並建立登入後自動啟動工作排程。
8. 平常用 `RUN_WORKER_GUI_WINPYTHON.vbs` 啟動，沒有黑色命令列視窗。

## 登打模式

救護預設最多四站同時登打。若要切回雙站，在 `.env` 設定 `WORKER_PARALLEL_SITE_GROUPS=2`；改為 `4` 可恢復四站，修改後重啟 Worker。未設定時使用四站，其他無效值保守使用雙站。
里程完成後才進行加油，工作紀錄完成後才進行民力；各站保留獨立錯誤紀錄及單站重試。救災只執行任務適用的站點。

## GitHub 更新

1. 管理端更新專案後執行 `scripts\build_public_duty_package.ps1`。
2. 預設更新來源是 `https://github.com/seaflun/ambulance-return-bot/releases/latest/download`。
3. 建立 SinpoSmart - 救災救護Worker 專用 GitHub Release，並上傳 `ambulance-return-version.txt`、`ambulance-return-public-package.zip`、`ambulance-return-public-package.zip.sha256.txt`、`update_package.ps1`。
4. 之後按 `UPDATE_PACKAGE.bat` 即可從 GitHub latest release 比對版本、下載 zip、驗證 sha256、備份後更新。
5. 若 GitHub repo 名稱不同，可在 `.env` 設定 `AMBULANCE_RETURN_RELEASE_BASE_URL` 覆蓋下載來源。

`.env`、logs、artifacts、runtime profiles 都不會被更新 zip 覆蓋。
