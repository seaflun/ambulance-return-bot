# Cloudflare Tunnel 外部入口

不在 NAS 同網路時，可以用 Cloudflare Tunnel 對外提供救護回程網頁，不需要開路由器 port。

## Cloudflare 設定

1. 進 Cloudflare Zero Trust。
2. 到 `Networks` -> `Tunnels`。
3. 建立一個 tunnel，例如 `ambulance-return`。
4. Connector 選 Docker。
5. 複製 Docker 指令中的 token，只取 `eyJ...` 那段。
6. 在 Public Hostname 設定：
   - Subdomain：例如 `ambulance`
   - Domain：你的 Cloudflare 網域
   - Type：`HTTP`
   - URL：`http://app:8080`

Cloudflare 官方 Docker 執行格式是：

```bash
cloudflared tunnel --no-autoupdate run --token <TOKEN>
```

本專案已經把這個指令寫在 `compose.cloudflare.yml`。

## NAS `.env`

在 NAS 專案資料夾的 `.env` 加入：

```dotenv
CLOUDFLARE_TUNNEL_TOKEN=eyJ...
```

不要把真實 token commit 到 Git。

## DSM Container Manager

建立 Project 時，同時使用兩個 compose 檔：

```text
compose.nas.yml
compose.cloudflare.yml
```

啟動後應有三個服務：

- `app`
- `selenium`
- `cloudflared`

`cloudflared` log 看到 connected 或 registered connection，就代表 tunnel 已連上 Cloudflare。

## 外部網址

如果 Public Hostname 設為：

```text
ambulance.example.com
```

手機外網入口就是：

```text
https://ambulance.example.com/app
```

狀態檢查：

```text
https://ambulance.example.com/status
```

## 注意

第一版建議先不要加 Cloudflare Access 驗證，等確認網頁可用後再加。加驗證後，手機需要先通過 Cloudflare 登入才能開 `/app`。
