# NAS 入口重新設計

## Context

救護返隊小幫手目前同一套 Flask runtime 供 NAS task center 與公務電腦 local web 使用。現有 `/` 會直接轉到 `/app`，而 `/app` 是救護登打頁。NAS 與公務電腦需要不同入口，但不能影響既有設定、任務流程、車輛設定資料、worker API 或舊 `/app` 書籤。

## Goals

- NAS 開 `/` 時顯示單純首頁，提供三個入口：`值班後台`、`救護後台`、`救護登打`。
- NAS 開 `/app` 時保留直接進入救護登打，避免既有手機和平板書籤失效。
- NAS 的救護登打頁不顯示 `救護車設定`。
- 公務電腦維持既有體驗：開 `/` 直接進救護登打，救護登打頁上方保留 `救護車設定`。
- 改動只限入口呈現與導覽按鈕，不搬移資料、不改設定檔、不改 worker API。

## Non-Goals

- 不改 `救護車設定` 的功能、儲存格式或路由。
- 不改 `值班後台`、`救護後台`、`救護登打` 各頁既有內容與資料處理。
- 不新增登入權限機制。
- 不改 NAS 與公務電腦的 worker token、Chrome/Selenium、任務 JSON 或 `.env` 行為。

## Design

`/` route 依目前 request host 判斷使用情境：

- 若是 NAS/非 localhost request，render 新的首頁模板。
- 若是公務電腦 localhost request，維持 redirect 到 `/app`。

NAS 首頁只放三個主要按鈕：

- `值班後台` -> `/admin/sinposmart`
- `救護後台` -> `/admin/public-pc`
- `救護登打` -> `/app`

`/app` route 繼續 render 現有救護登打模板。模板的 header actions 依 request host 顯示：

- 公務電腦 localhost：顯示 `救護車設定`。
- NAS/非 localhost：不顯示 `救護車設定`，也不把後台按鈕塞回登打頁。

這讓 NAS 的主要入口集中在 `/`，並保留 `/app` 舊連結。

## Testing

Targeted tests should verify:

- Localhost `GET /` still redirects to `/app`.
- NAS host `GET /` returns the new homepage with exactly the three expected links.
- NAS host `GET /app` loads rescue entry and does not include `救護車設定`.
- Localhost `GET /app` still includes `救護車設定` and does not show NAS backend buttons.
- Existing vehicle admin tests still pass, confirming route and settings behavior were not changed.
