# AGENTS.md

## Project

This repository is the ambulance return web app, NAS task center, and public-duty Windows worker automation.

Primary entrypoint:

```powershell
py app.py
```

Phone/tablet web entry through NAS Tailscale:

```text
http://100.114.126.58:8080/app
```

Public-duty PC worker server URL:

```text
http://10.30.65.30:8080
```

Visible worker GUI:

```text
run_worker_forever.bat
```

## Working Rules

- Keep credentials only in `.env`; never commit `.env`, tokens, passwords, cookies, screenshots, or generated task JSON.
- Do not use `.env.example` for local/NAS/public-duty settings unless the user explicitly asks; edit the real `.env` directly for operational changes.
- Background duty case lookup must use `DUTY_ACCOUNT` and `DUTY_PASSWORD` from `.env`; do not depend on Chrome Password Manager UI selection for this flow.
- Do not auto-submit final records on external government sites unless the user explicitly approves that behavior.
- Prefer small focused edits and run tests before reporting completion.
- After changing code and completing tests, restart the worker, then state clearly whether the worker was restarted.
- Always list every file changed in the final response.
- Daily worker launch should be no-console-window mode. Prefer `run_worker_forever.vbs` or `pyw -3 worker_gui.py`; use console launchers only for debugging.
- Use UTF-8 for Traditional Chinese text. If PowerShell display is garbled, verify file content with Python `encoding="utf-8"` before rewriting.
- Keep generated runtime files under `artifacts/`, `logs/`, `tmp/`, or another ignored directory.
- When worker or web behavior changes, sync the same source updates into `NAS包` and `WinPython_公務電腦使用包` before packaging or deployment.
- When the user asks to finish the current round, run checks, rebuild the public-duty package when needed, restart the worker when runtime files changed, and create a git commit unless explicitly told not to.
- Before committing, inspect `git status --short` and make sure no `.env`, token, password, cookie, Chrome profile, screenshot, or generated task JSON is staged.

## Test

```powershell
py -m py_compile app.py ambulance_bot\*.py
py -m unittest discover -s tests -v
```

## Current Automation Scope

- NAS runs the Flask task center only. NAS must not run Selenium, store four-site portal passwords, or do final data entry.
- The public-duty Windows PC runs `worker.py` with WinPython, polls NAS every 10 seconds, queries the previous 24 hours of emergency cases every 5 minutes, and deduplicates unchanged case lists.
- `run_worker_forever.vbs` starts `worker_gui.py` with no console window; `run_worker_forever.bat` delegates to `pyw` and exits immediately.
- The GUI can test NAS through Tailscale with `http://100.114.126.58:8080`.
- `run_worker_web_panel.bat` starts the older local web panel at `http://127.0.0.1:8090/`.
- Use `run_worker_headless.bat` only when a visible panel is not needed.
- Phone/tablet "查詢" only creates `case_lookup_requested`; the worker should pick it up on the next poll and post cases back to NAS.
- Duty case lookup uses the public-duty PC's local Chrome/Selenium and reads emergency cases.
- Case import may press the duty-system case `select` button to populate the work-log form, but it must not press final save.
- The worker uses a local AppData `CHROME_PROFILE_DIR`; the intended Chrome account/profile is `sinpo666@gmail.com`, with portal passwords handled by Google Password Manager rather than `.env`.
- Do not put Chrome profile data in Google Drive.
- Worker Chrome entry buttons should launch with `WORKER_CHROME_DEBUGGER_PORT=9223` so Selenium can attach instead of starting a second profile instance.
- CAPTCHA automation must not attempt to bypass or solve protected CAPTCHA. If CAPTCHA blocks a flow, mark the site for manual handoff.

## Fixed Network Values

- NAS LAN IP: `10.30.65.30`
- NAS Tailscale IP: `100.114.126.58`
- Phone/tablet entry: `http://100.114.126.58:8080/app`
- Public-duty PC worker URL: `http://10.30.65.30:8080`

## Current Implemented Behavior

- Public-duty PC web users create and update tasks on the NAS task center. The NAS admin page groups public-duty PC tasks and shows who created tasks, who started entry, per-site progress, success, and failure reports.
- The local desktop web app is for the operator machine only. It must not show the public-duty admin entry point; the public-duty admin button belongs on the NAS web app.
- Recent task delete/clear actions are hidden on public-duty and edit flows where deleting would prevent NAS-side tracking.
- The task detail page shows four-site task cards and a detailed stage checklist. `未開始` wording is standardized to `未執行`.
- Failed sites and sites blocked behind a failed earlier site expose independent entry controls so the operator can finish only the missing part.
- Retrying four-site entry skips completed sites and starts from the failed or unfinished site.
- Public-duty case lookup requests can be triggered from NAS or local web. Worker logs should identify the source as NAS or local when possible.
- Background case lookup uses the public-duty PC Chrome/Selenium session, closes lookup Chrome resources after lookup, and writes simplified worker GUI logs.
- Four-site entry launched from the public-duty web page should bring the Selenium Chrome pages to the foreground and keep the four site pages maximized at the end for manual inspection.
- Manual four-site entry from the local desktop web app and automatic NAS worker entry coordinate through a local manual-task lock so they do not run Selenium at the same time.
- The worker queue state is separated from final task status. A task can remain visible while queued, running, succeeded, failed, or pending retry.
- Per-site attempt history is stored so failures from older attempts do not hide the current state.
- Public-duty worker events use stable event identifiers and acknowledgements so pending events are not removed until the worker confirms receipt.
- Default consumables are `桃-9吋手套-L(雙)*2` and `桃-口罩(片)*2`.
- Address cleanup should strip trailing parenthesized remarks such as `(OHCA-D)` from the address field while preserving the case category elsewhere.
- Synchronized account labels should be consistent across badge, person name, and account name when those fields are available.
- The Worker GUI is now the CustomTkinter-based `救護回程小幫手`. It uses a 2x2 card layout, unified orange buttons, simplified logs, and hides noisy Selenium Chrome session progress messages.
- If a synced account display name only repeats the account id, do not show it as the person name. Prefer `actor_no + name + user_id`; otherwise show `未填姓名` instead of duplicating the account.
- Worker GUI NAS timeout logs should be user-readable as `連線｜NAS逾時｜等待下次重試`, while preserving real failure/error logs.

## Packaging, Restart, And Git Commit

Use this finish flow after meaningful code changes:

```powershell
py -m py_compile app.py ambulance_bot\*.py
py -m unittest discover -s tests -v
py tools\build_public_pc_package.py
.\WinPython_公務電腦使用包\run_worker_forever.vbs
git status --short
git add -A
git commit -m "Describe the worker or web change"
```

Current public-duty package version after the latest worker/web updates:

```text
2026.06.09.1707
```
