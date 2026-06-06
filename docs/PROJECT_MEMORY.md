# Project Memory

This file records project decisions that another computer or another coding agent must preserve.

## Architecture

- The first stable architecture is `NAS task center + public-duty Windows worker`.
- NAS runs Flask, stores task JSON, stores the latest case list, serves the phone/tablet web app, and exposes worker APIs.
- NAS must not run Selenium, open Chrome, store four-site portal credentials, or perform government-site data entry.
- The public-duty Windows PC runs the worker with WinPython and local Chrome/Selenium.
- The visible worker entry should be no-console-window mode. Prefer `run_worker_forever.vbs`, which starts `worker_gui.py` through `pyw`.
- `run_worker_forever.bat` also starts `worker_gui.py` through `pyw` and exits immediately.
- The worker GUI can test NAS through Tailscale at `http://100.114.126.58:8080`.
- `run_worker_web_panel.bat` starts the older local web panel at `http://127.0.0.1:8090/`.
- Use `run_worker_headless.bat` only if a visible panel is not needed.
- The development PC and public-duty PC are separate. Do not copy `.env`, `artifacts/`, or Chrome profile data between them unless the user explicitly asks.

## Fixed URLs And Network Values

- NAS LAN IP: `10.30.65.30`
- NAS Tailscale IP: `100.114.126.58`
- Phone/tablet entry: `http://100.114.126.58:8080/app`
- Public-duty PC worker server URL: `http://10.30.65.30:8080`

## Worker Behavior

- Worker polls NAS every 10 seconds.
- Worker automatically queries today's emergency cases every 5 minutes.
- If the case list hash is unchanged, worker must not repost the same case list.
- If phone/tablet user presses "查詢", NAS writes `case_lookup_requested`; worker should query immediately on the next poll and post the latest cases back to NAS.
- After a web task is submitted, NAS marks it `queued_for_worker`; worker claims it and updates status through worker APIs.
- Worker artifacts such as Selenium screenshots and HTML remain on the public-duty PC under `artifacts/selenium/`.
- The worker GUI has four entrance buttons for vehicle mileage, one-stop consumables, EMS disinfection, and duty work log. These buttons only open pages in the worker Chrome profile.

## Credentials And Chrome Profile

- Worker APIs use a shared `WORKER_TOKEN`; the same value must be set on NAS and public-duty PC.
- Four-site portal passwords must not be committed and should not be stored on NAS.
- For operational configuration, edit the real `.env` directly. Do not keep changing `.env.example` unless the user explicitly asks.
- The intended Chrome profile account is `sinpo666@gmail.com`.
- Use a local, non-cloud Chrome profile directory such as `C:\Users\User\AppData\Local\ambulance_return_bot\chrome_profile`.
- Do not put Chrome profile data under Google Drive or another synced project folder.
- The worker GUI opens Chrome with `WORKER_CHROME_DEBUGGER_PORT=9223` so Selenium can attach to the same profile window.
- Portal passwords are expected to come from Google Password Manager inside that Chrome profile.

## Data Entry Rules

- Four government sites are prefill-only in the first version.
- Do not click final save/submit on external government sites unless the user explicitly approves that behavior.
- Duty work log is the first full prefill target.
- PPE vehicle mileage, one-stop consumables, and EMS disinfection can start with login/navigation/manual handoff and be expanded site by site.
- Do not attempt to bypass or automatically solve protected CAPTCHA. If CAPTCHA blocks a flow, mark the site for manual handoff.

## Agent Operating Rules

- After code changes and tests, restart the worker and state clearly whether it was restarted.
- Always list every changed file in the final response.
- Use UTF-8 for Traditional Chinese text. If PowerShell output is garbled, verify with an explicit UTF-8 read before editing.
- Keep `.env`, tokens, passwords, cookies, generated task JSON, screenshots, and runtime artifacts out of Git.
