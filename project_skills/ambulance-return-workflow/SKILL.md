---
name: ambulance-return-workflow
description: Maintain the ambulance return bot project workflow across the NAS Flask task center, public-duty Windows worker GUI, local desktop web app, four-site Selenium automation, package builds, and NAS deployment bundle. Use when working in ambulance_return_bot on app.py, worker.py, worker_gui.py, ambulance_bot modules, templates, tests, NAS包, WinPython_公務電腦使用包, case lookup, public PC admin, task status, or four-site entry behavior.
---

# Ambulance Return Workflow

## Operating Model

Treat this repository as three linked deliverables:

- Source tree: `app.py`, `worker.py`, `worker_gui.py`, `ambulance_bot/`, `templates/`, `tests/`.
- NAS bundle: `NAS包`, deployed to `/docker/ambulance_return_bot/` on Synology and restarted through DSM Container Manager.
- Public-duty PC bundle: `WinPython_公務電腦使用包`, packaged through `scripts/build_public_duty_package.ps1`.

NAS runs the Flask task center only. The public-duty Windows PC runs Chrome/Selenium, background case lookup, and four-site entry. Do not move portal credentials or Chrome profiles into NAS or Google Drive.

## Before Editing

Read the relevant files before changing behavior:

- Web routes and task APIs: `app.py`.
- Public-duty worker polling and NAS status posting: `worker.py`.
- Desktop GUI, local web startup, log formatting, and package version display: `worker_gui.py`.
- Four-site local execution and resume/skip rules: `ambulance_bot/desktop_fast_runner.py`.
- Task JSON state, events, site statuses, cleanup: `ambulance_bot/task_store.py`.
- Mobile/local pages: `templates/new_task.html`, `templates/task_detail.html`, and admin templates.

Check `git status --short` before broad edits. Do not revert unrelated user changes.

## Change Routing

When a change affects NAS web behavior, sync it to `NAS包` before finishing. Typical files:

```powershell
Copy-Item -LiteralPath "app.py" -Destination "NAS包\app.py" -Force
Copy-Item -LiteralPath "worker.py" -Destination "NAS包\worker.py" -Force
Copy-Item -Path "ambulance_bot\*.py" -Destination "NAS包\ambulance_bot" -Force
Copy-Item -Path "templates\*.html" -Destination "NAS包\templates" -Force
Copy-Item -LiteralPath "requirements.txt" -Destination "NAS包\requirements.txt" -Force
```

When a change affects the public-duty PC app, worker, local web app, GUI, Selenium modules, or templates, sync it to `WinPython_公務電腦使用包` before packaging:

```powershell
Copy-Item -LiteralPath "app.py" -Destination "WinPython_公務電腦使用包\app.py" -Force
Copy-Item -LiteralPath "worker.py" -Destination "WinPython_公務電腦使用包\worker.py" -Force
Copy-Item -LiteralPath "worker_gui.py" -Destination "WinPython_公務電腦使用包\worker_gui.py" -Force
Copy-Item -Path "ambulance_bot\*.py" -Destination "WinPython_公務電腦使用包\ambulance_bot" -Force
Copy-Item -Path "templates\*.html" -Destination "WinPython_公務電腦使用包\templates" -Force
```

Keep `.env`, `artifacts/`, `logs/`, `tmp/`, Chrome profiles, screenshots, and secrets out of bundles unless the user explicitly asks and it is safe.

## Verification

Run targeted tests first for the touched area, then full verification:

```powershell
$files = @('app.py','worker.py','worker_gui.py') + (Get-ChildItem -Path ambulance_bot -Filter *.py | ForEach-Object { $_.FullName })
py -m py_compile @files
py -m unittest discover -s tests -v
```

Use targeted tests while iterating:

- Web/template/API changes: `py -m unittest tests.test_web_app -v`.
- GUI/log/package changes: `py -m unittest tests.test_worker_gui -v`.
- Four-site local runner changes: `py -m unittest tests.test_desktop_fast_runner -v`.
- Worker polling/case lookup changes: `py -m unittest tests.test_worker -v`.

If PowerShell displays Chinese as mojibake, verify file content with UTF-8 reads before rewriting. Do not treat terminal display alone as file corruption.

## Packaging And Restart

After code changes and tests, rebuild the public-duty PC package:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_public_duty_package.ps1
```

Then restart the local worker through the no-console launcher:

```powershell
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'ambulance_return_bot|worker_gui.py|worker.py|app.py' -and $_.Name -notmatch 'powershell' }
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
Start-Process -FilePath "wscript.exe" -ArgumentList '"I:\我的雲端硬碟\專案\IOS\ambulance_return_bot\run_worker_forever.vbs"'
Start-Sleep -Seconds 7
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'ambulance_return_bot|worker_gui.py|worker.py|app.py' -and $_.Name -notmatch 'powershell' } | Select-Object ProcessId, Name, CommandLine
```

Confirm local web availability when relevant:

```powershell
Invoke-WebRequest -Uri http://127.0.0.1:8090/app -UseBasicParsing -TimeoutSec 5
```

## NAS Deployment Reminder

If the change affects NAS pages or endpoints, explicitly tell the user to deploy the updated `NAS包` to:

```text
/docker/ambulance_return_bot/
```

Then restart the `ambulance_return_bot` stack in DSM Container Manager. If NAS still serves old HTML or returns 404 for new routes, verify the running container is mounted to the updated `NAS包` path and has been restarted.

## Final Response Checklist

Always include:

- Files changed.
- Tests/compile commands run and their result.
- Package version if `build_public_duty_package.ps1` ran.
- Whether worker was restarted.
- Whether NAS deployment is still required.

For behavior that differs between local and NAS web pages, state both URLs explicitly:

```text
Local: http://127.0.0.1:8090/app
NAS:   http://100.114.126.58:8080/app
```
