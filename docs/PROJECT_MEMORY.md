# Project Memory

This file records project decisions that another computer or another coding agent must preserve.

## Architecture

- The first stable architecture is `NAS task center + public-duty Windows worker`.
- NAS runs Flask, stores task JSON, stores the latest case list, serves the phone/tablet web app, and exposes worker APIs.
- NAS must not run Selenium, open Chrome, store four-site portal credentials, or perform government-site data entry.
- The public-duty Windows PC runs the worker with WinPython and local Chrome/Selenium.
- The visible worker entry should be no-console-window mode. Prefer `run_worker_forever.vbs`, which delegates to `RUN_WORKER_GUI_WINPYTHON.vbs`.
- `run_worker_forever.bat` also delegates to the WinPython GUI launcher and exits immediately.
- The worker GUI can test NAS through Tailscale at `http://100.114.126.58:8080`.
- `run_worker_web_panel.bat` starts the older local web panel at `http://127.0.0.1:8090/`.
- Use `run_worker_headless.bat` only if a visible panel is not needed.
- The development PC and public-duty PC are separate. Do not copy `.env`, `artifacts/`, or runtime profile data between them unless the user explicitly asks.

## Fixed URLs And Network Values

- NAS LAN IP: `10.30.65.30`
- NAS Tailscale IP: `100.114.126.58`
- Phone/tablet entry: `http://100.114.126.58:8080/app`
- Public-duty PC worker server URL: `http://10.30.65.30:8080`

## Worker Behavior

- Worker polls NAS every 10 seconds.
- Worker automatically queries the previous 24 hours of emergency cases every 5 minutes.
- If the case list hash is unchanged, worker must not repost the same case list.
- If phone/tablet user presses "查詢", NAS writes `case_lookup_requested`; worker should query immediately on the next poll and post the latest cases back to NAS.
- After a web task is submitted, NAS marks it `queued_for_worker`; worker claims it and updates status through worker APIs.
- Worker artifacts such as Selenium screenshots and HTML remain on the public-duty PC under `artifacts/selenium/`.
- The worker GUI has entrance buttons for vehicle mileage, one-stop consumables, EMS disinfection, and duty work log. These buttons open pages in the worker browser runtime profile.

## Credentials And Runtime Profiles

- Worker APIs use a shared `WORKER_TOKEN`; the same value must be set on NAS and public-duty PC.
- Four-site portal passwords must not be committed and should not be stored on NAS.
- For operational configuration, edit the real `.env` directly. Do not keep changing `.env.example` unless the user explicitly asks.
- Background duty case lookup cannot rely on Chrome Password Manager selection. Set `DUTY_ACCOUNT` and `DUTY_PASSWORD` in the public-duty PC `.env` so Selenium can log in automatically.
- Do not rely on Google/Chrome profile login for background automation. The worker uses saved duty automation credentials or `.env` credentials to fill the website login form directly.
- Use a local, non-cloud runtime profile root such as `C:\Users\User\AppData\Local\ambulance_return_bot`.
- Do not put runtime profile data under Google Drive or another synced project folder.
- The worker GUI opens Chrome with `WORKER_CHROME_DEBUGGER_PORT=9223` and `worker_browser_profile` for manual pages only.
- `chrome_profile` is legacy cache data only. Four-site automation must work from saved worker credentials or `.env` credentials.
- Stale generated runtime profiles are program-cleaned after `SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS` when no Chrome lock file is present.
- `WORKER_BROWSER_AUTO_CLOSE_SECONDS=600` closes opened entry pages after 10 minutes by default.

## Data Entry Rules

- Four government sites are prefill-only in the first version.
- Do not click final save/submit on external government sites unless the user explicitly approves that behavior.
- Duty work log is the first full prefill target.
- PPE vehicle mileage, one-stop consumables, and EMS disinfection can start with login/navigation/manual handoff and be expanded site by site.
- Do not attempt to bypass or automatically solve protected CAPTCHA. If CAPTCHA blocks a flow, mark the site for manual handoff.

## Agent Operating Rules

- Use Matt Pocock engineering skills for implementation flow. Do not use the removed Superpowers bundle; `project_skills/` contains project-specific operational constraints, which take priority over the engineering flow.
- After code changes and tests, restart the worker and state clearly whether it was restarted.
- Always list every changed file in the final response.
- Use UTF-8 for Traditional Chinese text. If PowerShell output is garbled, verify with an explicit UTF-8 read before editing.
- Keep `.env`, tokens, passwords, cookies, generated task JSON, screenshots, and runtime artifacts out of Git.
