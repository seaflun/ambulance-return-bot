---
name: ambulance-return-workflow
description: Use when working in ambulance_return_bot or 救護返隊小幫手 on the NAS Flask task center, public-duty WinPython worker GUI, four-site Selenium automation, package builds, GitHub releases, or generated NAS deployment output.
---

# Ambulance Return Workflow

## Current Layout

Resolve the current project path without assuming a drive letter:

```powershell
$repoRoot = Get-PSDrive -PSProvider FileSystem | ForEach-Object {
  $candidate = Join-Path $_.Root '我的雲端硬碟\專案\救護返隊小幫手\ambulance_return_bot'
  if (Test-Path -LiteralPath $candidate -PathType Container) { $candidate }
} | Select-Object -First 1
if (-not $repoRoot) { throw '找不到救護返隊小幫手專案。' }
Set-Location -LiteralPath $repoRoot
```

The old `專案\IOS\ambulance_return_bot` path is historical. Do not add new commands, shortcuts, docs, or restart scripts that point there.

Treat this repository as linked deliverables:

- `WinPython_公務電腦使用包`: canonical source tree for both the public-duty PC runtime and the NAS Flask files copied during packaging.
- `UPDATE\NAS包`: generated NAS Flask/task-center deployment output.
- repository root: tests, release scripts, docs, and thin compatibility entrypoints.

Root `app.py`, `worker.py`, `worker_gui.py`, `consumables_login.py`, and `disinfect.py` are compatibility launchers. They load runtime code from `WinPython_公務電腦使用包`. Root `ambulance_bot/__init__.py` redirects imports to `WinPython_公務電腦使用包\ambulance_bot`.

## Boundaries

NAS runs Flask/task APIs only. The public-duty Windows PC runs Chrome/Selenium, worker GUI, local web, background case lookup, and four-site entry.

Do not move portal credentials, Chrome profiles, cookies, task JSON, screenshots, logs, `.env`, or local generated artifacts into tracked files or release zips. Keep protected-site CAPTCHA and final-submit behavior human-in-the-loop unless the user explicitly approves otherwise.

## Before Editing

Start with:

```powershell
git status --short --branch
```

Read and edit the owning runtime source first:

- Canonical local web/API/GUI/worker source: `WinPython_公務電腦使用包\app.py`, `worker.py`, `worker_gui.py`, `templates\`, `ambulance_bot\`.
- NAS web/API/task center: edit those same canonical files, then rebuild `UPDATE\NAS包` with `scripts\build_nas_package.ps1`.
- Tests/release flow: root `tests\` and `scripts\`.

Do not restore duplicated full runtime source under root. Do not edit generated `NAS包` output as source.

## Verification

Compile compatibility files and the public-duty package runtime:

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

Use targeted tests when iterating:

- Web/template/API changes: `py -m unittest tests.test_web_app -v`
- GUI/log/package changes: `py -m unittest tests.test_worker_gui -v`
- Four-site runner changes: `py -m unittest tests.test_desktop_fast_runner -v`
- Worker polling/case lookup changes: `py -m unittest tests.test_worker -v`

If PowerShell displays Chinese as mojibake, verify content with UTF-8 reads before rewriting. Do not treat terminal display alone as file corruption.

## Build And Release

Build the public-duty package from `WinPython_公務電腦使用包`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_public_duty_package.ps1
```

Build the generated NAS deployment output:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1
```

For GitHub releases, verify remote parity before closing:

- remote `ambulance-return-version.txt`
- downloaded release zip internal `VERSION.txt`
- remote/downloaded SHA256

## Commit And GitHub Release

When the user asks for `git commit`, `GitHub release`, `release`, `publish`, or a full finish after package-affecting changes, carry the workflow through source commit, push, package build, release publication, and remote parity checks unless the user explicitly narrows the scope.

Before committing:

```powershell
git status --short --branch
git diff --check
py -m unittest discover -s tests -v
powershell -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1
powershell -ExecutionPolicy Bypass -File scripts\build_public_duty_package.ps1
```

Stage source files intentionally. Do not stage `.env`, secrets, `artifacts/`, `logs/`, `tmp/`, `__pycache__/`, root `NAS包/`, generated `UPDATE\NAS包/`, Chrome profiles, screenshots, or task JSON. Generated release assets under `UPDATE/` are upload artifacts, not source commits, unless the user explicitly asks to track them.

Commit and push:

```powershell
git add <explicit source files>
git status --short
git commit -m "<clear change summary>"
git push origin HEAD
```

Publish GitHub release assets from `UPDATE/`. For a new tag, use the publish script:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\publish_ambulance_return_release.ps1
```

If the tag or assets already exist, replace the assets with `--clobber` instead of trusting the old release:

```powershell
$version = (Get-Content -LiteralPath UPDATE\ambulance-return-version.txt -Raw -Encoding UTF8).Trim().TrimStart([char]0xFEFF)
$tag = "ambulance-return-$version"
gh release upload $tag UPDATE\ambulance-return-version.txt UPDATE\ambulance-return-public-package.zip UPDATE\ambulance-return-public-package.zip.sha256.txt --repo seaflun/ambulance-return-bot --clobber
```

After upload, read back the remote version file, downloaded zip internal `VERSION.txt`, and remote/downloaded SHA256. Do not close a release task based only on `gh release` success. If the user says they will deploy NAS manually, stop after package/release publication and clearly state that NAS deployment/restart was not performed.

## Restart

Restart from the package launcher:

```powershell
$procs = Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -and
  $_.CommandLine -match 'ambulance_return_bot|worker_gui.py|worker.py|app.py|救護返隊小幫手' -and
  $_.Name -notmatch 'powershell'
}
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$repoRoot = (Resolve-Path -LiteralPath ".").Path
$launcher = Join-Path $repoRoot "WinPython_公務電腦使用包\run_worker_forever.vbs"
Start-Process -FilePath "wscript.exe" -ArgumentList ('"{0}"' -f $launcher)
Start-Sleep -Seconds 7
Invoke-WebRequest -Uri http://127.0.0.1:8090/app -UseBasicParsing -TimeoutSec 5
```

Always say whether the worker was restarted.

## NAS Reminder

If NAS behavior changed, deploy the generated `UPDATE\NAS包` contents to:

```text
/docker/ambulance_return_bot/
```

Then restart the `ambulance_return_bot` stack in DSM Container Manager.
