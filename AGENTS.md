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

Visible worker panel:

```text
http://127.0.0.1:8090/
```

## Working Rules

- Keep credentials only in `.env`; never commit `.env`, tokens, passwords, cookies, screenshots, or generated task JSON.
- Do not auto-submit final records on external government sites unless the user explicitly approves that behavior.
- Prefer small focused edits and run tests before reporting completion.
- After changing code and completing tests, restart the worker, then state clearly whether the worker was restarted.
- Always list every file changed in the final response.
- Use UTF-8 for Traditional Chinese text. If PowerShell display is garbled, verify file content with Python `encoding="utf-8"` before rewriting.
- Keep generated runtime files under `artifacts/`, `logs/`, `tmp/`, or another ignored directory.

## Test

```powershell
py -m py_compile app.py ambulance_bot\*.py
py -m unittest discover -s tests -v
```

## Current Automation Scope

- NAS runs the Flask task center only. NAS must not run Selenium, store four-site portal passwords, or do final data entry.
- The public-duty Windows PC runs `worker.py` with WinPython, polls NAS every 10 seconds, queries today's emergency cases every 5 minutes, and deduplicates unchanged case lists.
- `run_worker_forever.bat` starts `worker_panel.py`, which shows the visible worker panel and starts the background worker thread.
- Use `run_worker_headless.bat` only when a visible panel is not needed.
- Phone/tablet "查詢" only creates `case_lookup_requested`; the worker should pick it up on the next poll and post cases back to NAS.
- Duty case lookup uses the public-duty PC's local Chrome/Selenium and reads emergency cases.
- Case import may press the duty-system case `select` button to populate the work-log form, but it must not press final save.
- The worker uses `CHROME_PROFILE_DIR=artifacts/chrome_profile`; the intended Chrome account/profile is `sinpo666@gmail.com`, with portal passwords handled by Google Password Manager rather than `.env`.
- CAPTCHA automation must not attempt to bypass or solve protected CAPTCHA. If CAPTCHA blocks a flow, mark the site for manual handoff.

## Fixed Network Values

- NAS LAN IP: `10.30.65.30`
- NAS Tailscale IP: `100.114.126.58`
- Phone/tablet entry: `http://100.114.126.58:8080/app`
- Public-duty PC worker URL: `http://10.30.65.30:8080`
