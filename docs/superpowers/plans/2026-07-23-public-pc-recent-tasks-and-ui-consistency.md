# 公務電腦最近任務唯讀整合與介面一致性執行計畫

**目標：** 讓公務電腦建立的 48 小時內任務可在 NAS 對應的救災／救護登打頁唯讀查看，同時統一入口卡片、值班後台及登打頁標題列控制項樣式。

**安全邊界：**

- 維持單向顯示：公務電腦報告只供 NAS 最近任務讀取，不寫入 NAS TaskStore，不反向同步。
- 公務電腦來源任務在 NAS 不提供明細連結、編輯、刪除或重跑入口。
- 相同 task_id 以 NAS TaskStore 任務為準，公務電腦報告不重複顯示。
- 48 小時只限制最近任務畫面；既有七日後台報告保存與稽核資料不刪除。
- 不刪除任何檔案，保留所有不相關變更。

## 任務 1：用測試固定單向唯讀與 48 小時規則

**檔案：**

- 修改：`tests/test_web_app.py`

**步驟：**

1. 新增 NAS 救護／救災頁只合併對應 service_type 公務電腦報告的測試。
2. 驗證公務電腦來源卡片顯示來源與唯讀標記，且沒有 `/tasks/<id>` 連結。
3. 新增相同 task_id 去重測試。
4. 將本機最近任務測試改為成功、失敗及等待任務皆於 48 小時後不顯示。
5. 先執行新增測試並確認因功能尚未存在而失敗。

## 任務 2：實作 NAS 唯讀合併

**檔案：**

- 修改：`WinPython_公務電腦使用包/app.py`
- 新增：`WinPython_公務電腦使用包/templates/_recent_tasks.html`
- 修改：`WinPython_公務電腦使用包/templates/new_task.html`
- 修改：`WinPython_公務電腦使用包/templates/disaster_task.html`
- 修改：`WinPython_公務電腦使用包/static/sinposmart-workspace.css`

**步驟：**

1. 依 request host 區分 NAS 與公務電腦最近任務顯示規則。
2. NAS 端將 48 小時內公務電腦報告轉成唯讀顯示資料，套用 service_type 篩選及 task_id 去重。
3. 公務電腦端所有狀態的最近任務統一套用 48 小時截止時間。
4. 抽出共用最近任務模板，加入來源與「NAS 僅查看」標記。
5. 執行任務 1 測試並確認通過。

## 任務 3：用測試固定介面一致性

**檔案：**

- 修改：`tests/test_web_app.py`

**步驟：**

1. 驗證救災救護入口卡片沿用主頁 `portal-card` 高度。
2. 驗證值班後台返回首頁採用次要 Apple 材質按鈕。
3. 驗證救災與救護登打頁右上按鈕使用同一共用類別。
4. 驗證手機寬度仍顯示各頁標題上方的小字，車輛設定頁也使用相同標題結構。
5. 先執行新增測試並確認失敗。

## 任務 4：統一卡片與標題列控制項

**檔案：**

- 修改：`WinPython_公務電腦使用包/templates/task_entry.html`
- 修改：`WinPython_公務電腦使用包/templates/admin_sinposmart.html`
- 修改：`WinPython_公務電腦使用包/templates/new_task.html`
- 修改：`WinPython_公務電腦使用包/templates/disaster_task.html`
- 修改：`WinPython_公務電腦使用包/static/sinposmart-workspace.css`

**步驟：**

1. 入口兩張卡片加入與首頁相同的 `portal-card` 尺寸規則。
2. 值班後台返回首頁改用次要按鈕及共用標題列導覽樣式。
3. 救災／救護登打頁的返回上一頁與車輛設定套用同一共用 Apple 材質樣式。
4. 移除手機版隱藏標題小字的規則，並補齊救災車／救護車設定頁小字。
5. 執行任務 3 測試並確認通過。

## 任務 5：驗證、打包、發布與部署

**步驟：**

1. 執行相關目標測試、完整 `test_web_app` 與全套測試。
2. 檢查 git diff 與變更檔案行號，確認無檔案刪除及無不相關修改。
3. 依 `sinposmart-update-package` 流程產生新版本套件。
4. 提交、推送並建立 GitHub Release，驗證 latest 與直接下載內容。
5. 部署 NAS，重啟並以 `/status`、版本與雜湊確認上線。
6. 在公務電腦 Worker 閒置且可安全更新時套用更新，讀回版本與狀態。
