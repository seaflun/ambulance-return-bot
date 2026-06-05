# AGENTS.md

## Project

This repository is the ambulance return web app and local Windows automation.

Primary entrypoint:

```powershell
py app.py
```

Mobile web entry:

```text
http://127.0.0.1:8081/app
```

## Working Rules

- Keep credentials only in `.env`; never commit `.env`, tokens, passwords, cookies, screenshots, or generated task JSON.
- Do not auto-submit final records on external government sites unless the user explicitly approves that behavior.
- Prefer small focused edits and run tests before reporting completion.
- Use UTF-8 for Traditional Chinese text. If PowerShell display is garbled, verify file content with Python `encoding="utf-8"` before rewriting.
- Keep generated runtime files under `artifacts/`, `logs/`, `tmp/`, or another ignored directory.

## Test

```powershell
py -m py_compile app.py ambulance_bot\*.py
py -m unittest discover -s tests -v
```

## Current Automation Scope

- The web app sends tasks to this Windows computer.
- Duty case lookup uses local Chrome/Selenium and reads recent emergency cases.
- Case import may press the duty-system case `select` button to populate the work-log form, but it must not press final save.
