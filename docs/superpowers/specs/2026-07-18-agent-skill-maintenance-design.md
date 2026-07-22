# Agent 與 Skill 維護設計

## 目標

讓救護返隊專案的 `AGENTS.md`、repo vendored skills、雲端 SKILL library 與
`%USERPROFILE%\.codex\skills` runtime 保持一致，並移除已知會誤導 agent 或在本機無法執行的設定。

## 邊界

- `WinPython_公務電腦使用包`、執行程式、測試資料與現有使用者變更不在本次修改範圍。
- `project_skills/ambulance-return-workflow` 是本專案工作流的權威副本。
- `I:\我的雲端硬碟\專案\SKILL` 是自訂 skill 的雲端主儲存。
- `C:\Users\User\.codex\skills` 是本機 runtime；只能由雲端安裝器同步。
- OpenAI 管理的 `.system` skill 不直接修改。

## 設計

1. 以可重複的檢查驗證三方內容 parity、disabled skill 不得出現在 runtime、
   `agents/openai.yaml` 使用 `interface:`，以及 repo 不追蹤 `.codex/`。
2. 保留 Hivemind 雲端來源，但透過 `DISABLED_SKILLS.txt` 排除 runtime 安裝；
   此機沒有 Bash，因此不宣稱 Hivemind 可用。
3. 將 `ambulance-return-workflow` 的現行 generated-NAS 邊界同步到雲端與 runtime。
4. 將 `karpathy-guidelines` metadata 更新為現行 OpenAI 介面格式，再同步三方。
5. 將 Superpowers vendored/runtime 副本更新到已驗證的上游 commit，並記錄 commit。
6. `nas-line-push` 保留通用用途，但加入 ambulance 專案的 NAS-only-Flask 例外，
   Selenium image 改由 `.env` 提供已測試的明確 tag 或 digest。
7. `AGENTS.md` 不再內嵌可能漂移的 public key；保留受限帳號、key 路徑與唯讀驗證方式。

## 驗證

- 雲端 installer 測試與 `-VerifyOnly`。
- Skill frontmatter、OpenAI metadata 與三方 SHA/parity 檢查。
- Repo `git diff --check`、完整 Python 測試。
- NAS `/status`、管理頁與 SSH `docker ps` 唯讀檢查。

## 非目標

- 不發布公務電腦更新包。
- 不部署或重啟 NAS。
- 沒有 NAS 管理權限時，不嘗試繞過 SSH 驗證。
