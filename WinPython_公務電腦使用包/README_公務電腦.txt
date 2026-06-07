# 救護回程公務電腦使用包

## 第一次設定

1. 把 `.env.example` 複製成 `.env`。
2. 填入 `WORKER_TOKEN`，要和 NAS 的值相同。
3. `CHROME_PROFILE_DIR` 預設使用 `%LOCALAPPDATA%\ambulance_return_bot\chrome_profile`，不要放到 Google Drive 資料夾。
4. 執行 `SETUP_WINPYTHON.bat` 安裝套件、檢查環境，並建立開機自動啟動捷徑。
5. 平常用 `RUN_WORKER_GUI_WINPYTHON.vbs` 啟動，沒有黑色命令列視窗。
6. 在另一邊專案匯出帳密同步 JSON 檔，再回 GUI 按 `匯入同步` 選取該 JSON；不要把入口帳密寫進 `.env`。
7. 帳密會存到該台公務電腦自己的 Windows 本機儲存；舊 `.env` 裡的 `DUTY_SAVED_LOGIN_PATH` 預設會被忽略。

## GitHub 更新

1. 管理端更新專案後執行 `scripts\build_public_duty_package.ps1`。
2. 預設更新來源是 `https://github.com/seaflun/ambulance-return-bot/releases/latest/download`。
3. 建立救護回程專用 GitHub Release，並上傳 `ambulance-return-version.txt`、`ambulance-return-public-package.zip`、`ambulance-return-public-package.zip.sha256.txt`。
4. 之後按 `UPDATE_PACKAGE.bat` 即可從 GitHub latest release 比對版本、下載 zip、驗證 sha256、備份後更新。
5. 若 GitHub repo 名稱不同，可在 `.env` 設定 `AMBULANCE_RETURN_RELEASE_BASE_URL` 覆蓋下載來源。

`.env`、logs、artifacts、Chrome profile 都不會被更新 zip 覆蓋。
