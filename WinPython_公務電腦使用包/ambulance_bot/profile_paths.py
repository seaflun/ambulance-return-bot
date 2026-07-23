from __future__ import annotations

import errno
import os
import shutil
import time
from pathlib import Path


DEFAULT_PROFILE_ROOT_NAME = "ambulance_return_bot"
WORKER_BROWSER_PROFILE_NAME = "worker_browser_profile"
GENERATED_PROFILE_NAMES = {
    WORKER_BROWSER_PROFILE_NAME,
    "chrome_profile",
    "case_lookup_profile",
    "duty_work_log_profile",
    "vehicle_mileage_profile",
    "fuel_record_profile",
    "consumables_profile",
    "disinfection_profile",
    "fuel_record_probe",
    "probe_vehicle_delete_location",
}
GENERATED_PROFILE_PREFIXES = (
    "case_lookup_profile_",
    "duty_work_log_profile_",
    "vehicle_mileage_profile_",
    "fuel_record_profile_",
    "consumables_profile_",
    "disinfection_profile_",
    "acs_login_test_",
)
PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
REPAIR_BACKUP_MARKER = ".chrome_repair_"


def runtime_profile_root() -> Path:
    configured_root = os.getenv("SELENIUM_PROFILE_ROOT", "").strip()
    if configured_root:
        return _expand_path(configured_root)

    legacy_profile_dir = os.getenv("CHROME_PROFILE_DIR", "").strip()
    if legacy_profile_dir:
        return _expand_path(legacy_profile_dir).parent

    local_root = Path(os.getenv("LOCALAPPDATA") or Path.home())
    return local_root / DEFAULT_PROFILE_ROOT_NAME


def runtime_profile_dir(profile_name: str) -> Path:
    name = str(profile_name or "").strip() or WORKER_BROWSER_PROFILE_NAME
    try:
        cleanup_stale_runtime_profiles()
    except OSError as exc:
        print(f"[profiles] profile cleanup unavailable: {_short_error(exc)}", flush=True)
    path = runtime_profile_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def worker_browser_profile_dir() -> Path:
    name = os.getenv("WORKER_BROWSER_PROFILE_NAME", WORKER_BROWSER_PROFILE_NAME).strip() or WORKER_BROWSER_PROFILE_NAME
    return runtime_profile_dir(name)


def cleanup_stale_runtime_profiles(
    profile_root: Path | None = None,
    max_age_hours: float | None = None,
    skip_profile_names: set[str] | None = None,
) -> list[Path]:
    if os.getenv("SELENIUM_PROFILE_CLEANUP_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        return []
    root = Path(profile_root) if profile_root is not None else runtime_profile_root()
    try:
        if not root.exists():
            return []
        paths = list(root.iterdir())
    except OSError as exc:
        print(f"[profiles] profile cleanup unavailable: {_short_error(exc)}", flush=True)
        return []
    max_age = _profile_cleanup_max_age_hours() if max_age_hours is None else max_age_hours
    cutoff = time.time() - max(float(max_age), 0.0) * 3600
    skip_names = set(skip_profile_names or set())
    removed: list[Path] = []
    for path in paths:
        if path.name in skip_names:
            continue
        if not _is_generated_runtime_profile(path.name):
            continue
        try:
            if not path.is_dir() or path.is_symlink() or _profile_has_active_lock(path):
                continue
            if path.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(path)
        except OSError as exc:
            if _is_locked_profile_cleanup_error(exc):
                continue
            print(f"[profiles] cleanup skipped {path.name}: {_short_error(exc)}", flush=True)
            continue
        removed.append(path)
    if removed:
        print(f"[profiles] cleaned stale runtime profiles: {', '.join(path.name for path in removed)}", flush=True)
    return removed


def cleanup_runtime_profiles_for_startup_failure(
    user_data_dirs: list[str | Path] | tuple[str | Path, ...],
    profile_root: Path | None = None,
) -> list[Path]:
    requested_dirs = [raw_dir for raw_dir in user_data_dirs if str(raw_dir).strip()]
    if not requested_dirs:
        return []
    root = (Path(profile_root) if profile_root is not None else runtime_profile_root()).resolve()
    removed: list[Path] = []
    seen: set[Path] = set()
    for path in cleanup_stale_runtime_profiles(root, max_age_hours=0):
        resolved = path.resolve()
        removed.append(path)
        seen.add(resolved)
    for raw_dir in requested_dirs:
        try:
            path = _expand_path(str(raw_dir))
        except OSError:
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        if not _is_path_under(path, root):
            continue
        if not _is_generated_runtime_profile(path.name):
            continue
        if not path.is_dir() or path.is_symlink() or _profile_has_active_lock(path):
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            if _is_locked_profile_cleanup_error(exc):
                continue
            print(f"[profiles] startup cleanup skipped {path.name}: {_short_error(exc)}", flush=True)
            continue
        removed.append(path)
        seen.add(resolved)
    if removed:
        print(f"[profiles] cleaned startup-failed runtime profiles: {', '.join(path.name for path in removed)}", flush=True)
    return removed


def _profile_cleanup_max_age_hours() -> float:
    try:
        return max(float(os.getenv("SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS", "4")), 0.0)
    except ValueError:
        return 4.0


def _is_generated_runtime_profile(profile_name: str) -> bool:
    name = _repair_backup_source_name(str(profile_name))
    return name in GENERATED_PROFILE_NAMES or name.startswith(GENERATED_PROFILE_PREFIXES)


def _repair_backup_source_name(profile_name: str) -> str:
    if REPAIR_BACKUP_MARKER not in profile_name:
        return profile_name
    return profile_name.split(REPAIR_BACKUP_MARKER, 1)[0]


def _is_path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _profile_has_active_lock(profile_dir: Path) -> bool:
    return any((profile_dir / name).exists() for name in PROFILE_LOCK_NAMES)


def _is_locked_profile_cleanup_error(exc: OSError) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in {5, 32}:
        return True
    return getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM}


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text.splitlines()[0][:240]


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve()
