# SinpoSmart - 救護Worker

SinpoSmart - 救護Worker is maintained as a Windows public-duty worker plus a NAS Flask task center.

## Layout

- `WinPython_公務電腦使用包`: source of truth for the public-duty runtime, including Flask app code, worker GUI, Selenium flow, templates, and `ambulance_bot`.
- Repository root: compatibility entrypoints, tests, release scripts, and documentation.
- `UPDATE/`: generated public-duty update assets and generated NAS deployment package.
- `NAS包/`: legacy local/generated folder. It is ignored by Git; build fresh NAS output with `scripts\build_nas_package.ps1`.

Root `app.py`, `worker.py`, `worker_gui.py`, `consumables_login.py`, and `disinfect.py` are compatibility launchers that load runtime code from `WinPython_公務電腦使用包`.

## Verify

```powershell
py -m py_compile app.py worker.py worker_gui.py consumables_login.py disinfect.py _runtime_loader.py
py -m unittest discover -s tests -v
```

Local public-duty app:

```text
http://127.0.0.1:8090/app
```

## Public-Duty Package

Build the public-duty update package:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_public_duty_package.ps1
```

The build uses `WinPython_公務電腦使用包` as the package source and writes release/update assets under `UPDATE/`.

## NAS Package

Build the NAS deployment package:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_nas_package.ps1
```

The generated NAS output is:

```text
UPDATE\NAS包
```

Deploy the generated contents to Synology:

```text
/docker/ambulance_return_bot/
```

Keep the NAS `.env` already on the NAS. The generated package does not include `.env`. After deployment, restart the `ambulance_return_bot` stack in DSM Container Manager.
