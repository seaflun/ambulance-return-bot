# AGENTS.md

## Superpowers Skills

- This repository vendors Superpowers skills under `project_skills/superpowers/`.
- At the start of work in this repository, prefer the `using-superpowers` skill to decide whether another Superpowers skill should be used.
- For planning, debugging, testing, branch finishing, code review, or completion verification, use the matching Superpowers skill when applicable.
- Superpowers skills do not override this `AGENTS.md` or direct user instructions; repository rules and user instructions remain higher priority.

## Project

Current local path:

```powershell
I:\我的雲端硬碟\專案\救護返隊小幫手\ambulance_return_bot
```

The old `I:\我的雲端硬碟\專案\IOS` path is historical only. Do not introduce new commands, shortcuts, docs, or restart scripts that point there.

## Source Ownership

- `WinPython_公務電腦使用包` is the public-duty runtime source of truth.
- `UPDATE\NAS包` is a generated NAS deployment output produced by `scripts\build_nas_package.ps1`.
- Root files are thin compatibility entrypoints plus tests, scripts, and documentation.
- Root `app.py`, `worker.py`, `worker_gui.py`, `consumables_login.py`, and `disinfect.py` load the public-duty runtime package for backward compatibility.
- Root `ambulance_bot/__init__.py` points imports to `WinPython_公務電腦使用包\ambulance_bot`.
- Do not restore duplicate full runtime copies under root. Do not edit generated `NAS包` output as source.

## Operating Boundaries

- NAS runs the Flask task center only.
- The public-duty Windows PC runs Chrome/Selenium, the local web app, worker GUI, background case lookup, and four-site entry.
- Do not move portal credentials, Chrome profiles, task JSON, screenshots, logs, `.env`, or generated artifacts into tracked files or release assets.
- Four-site automation must log in from saved worker credentials or local `.env` credentials. Do not make a fixed `chrome_profile` folder a required part of the flow.
- Use `SELENIUM_PROFILE_ROOT` as the runtime cache root for generated Selenium profiles; `CHROME_PROFILE_DIR` is legacy compatibility only.
- Keep stale generated runtime profiles auto-cleanable and keep opened entry pages auto-closable after `WORKER_BROWSER_AUTO_CLOSE_SECONDS`.
- Keep `.env` local and untracked. Commit only `.env.example` when defaults change.
- Protected-site CAPTCHA and final-submit boundaries remain human-in-the-loop unless the user explicitly approves otherwise.

## Before Editing

Check status first:

```powershell
git status --short --branch
```

Read and edit the owning runtime source first:

- Public-duty local web/API/GUI/worker: `WinPython_公務電腦使用包\app.py`, `worker.py`, `worker_gui.py`, `templates\`, `ambulance_bot\`.
- NAS task-center behavior: edit the same source package first, then rebuild `UPDATE\NAS包` with `scripts\build_nas_package.ps1`.
- Tests and release scripts: root `tests\` and `scripts\`.

Do not rely on root compatibility files or generated NAS output as the source.

## Verification

Compile the compatibility layer and package runtime:

```powershell
$files = @(
  'app.py',
  'worker.py',
  'worker_gui.py',
  'consumables_login.py',
  'disinfect.py',
  '_runtime_loader.py'
) + (Get-ChildItem -Path 'WinPython_公務電腦使用包\ambulance_bot' -Filter *.py | ForEach-Object { $_.FullName })
py -m py_compile @files
```

Run tests:

```powershell
py -m unittest discover -s tests -v
```

Use targeted tests while iterating:

- Web/template/API changes: `py -m unittest tests.test_web_app -v`
- GUI/log/package changes: `py -m unittest tests.test_worker_gui -v`
- Four-site local runner changes: `py -m unittest tests.test_desktop_fast_runner -v`
- Worker polling/case lookup changes: `py -m unittest tests.test_worker -v`

If PowerShell displays Chinese as mojibake, verify content with UTF-8 reads before rewriting. Do not treat terminal display alone as file corruption.

## Packaging And Release

Build the public-duty package:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_public_duty_package.ps1
```

Build the NAS deployment output:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1
```

For GitHub releases, verify all three:

- remote `ambulance-return-version.txt`
- downloaded zip internal `VERSION.txt`
- remote/downloaded SHA256

## Restart

Restart the local worker through the package launcher:

```powershell
$procs = Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -and
  $_.CommandLine -match 'ambulance_return_bot|worker_gui.py|worker.py|app.py|救護返隊小幫手' -and
  $_.Name -notmatch 'powershell'
}
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
Start-Process -FilePath "wscript.exe" -ArgumentList '"I:\我的雲端硬碟\專案\救護返隊小幫手\ambulance_return_bot\WinPython_公務電腦使用包\run_worker_forever.vbs"'
Start-Sleep -Seconds 7
Invoke-WebRequest -Uri http://127.0.0.1:8090/app -UseBasicParsing -TimeoutSec 5
```

Always state whether the worker was restarted.

## NAS Deployment Reminder

If NAS behavior changed, deploy the generated `UPDATE\NAS包` contents to:

```text
/docker/ambulance_return_bot/
```

Then restart the `ambulance_return_bot` stack in DSM Container Manager. If NAS still serves old HTML or returns 404 for new routes, verify the container mount and restart state.
