from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DutyCredential:
    user_id: str
    password: str
    actor_no: str = ""
    display_name: str = ""


def load_duty_credential() -> DutyCredential | None:
    user_id = os.getenv("DUTY_ACCOUNT", "").strip()
    password = os.getenv("DUTY_PASSWORD", "")
    if user_id and password:
        return DutyCredential(user_id=user_id, password=password)

    saved = load_saved_duty_automation_credential()
    if saved is not None:
        return saved
    return None


def load_saved_duty_automation_credential(path: Path | None = None) -> DutyCredential | None:
    source = path or saved_login_path()
    if not source.exists():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return _legacy_credential(payload)

    last_selected = str(payload.get("last_selected", "") or "").strip()
    selected = _select_account(accounts, last_selected)
    if selected is None:
        return None
    password = _account_password(selected)
    user_id = str(selected.get("user_id", "") or "").strip()
    if not user_id or not password:
        return None
    return DutyCredential(
        user_id=user_id,
        password=password,
        actor_no=str(selected.get("actor_no", "") or "").strip(),
        display_name=str(selected.get("display_name", "") or "").strip(),
    )


def saved_login_path() -> Path:
    configured = os.getenv("DUTY_SAVED_LOGIN_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "DutyAutomation" / "saved_login.json"


def _select_account(accounts: list[object], last_selected: str) -> dict | None:
    candidates = [account for account in accounts if isinstance(account, dict)]
    if not candidates:
        return None
    if last_selected:
        for account in candidates:
            identity = str(account.get("user_id", "") or account.get("actor_no", "") or "").strip()
            if identity == last_selected:
                return account
    return candidates[0]


def _legacy_credential(payload: dict) -> DutyCredential | None:
    user_id = str(payload.get("user_id", "") or "").strip()
    password = str(payload.get("password", "") or "")
    if not user_id or not password:
        return None
    return DutyCredential(
        user_id=user_id,
        password=password,
        actor_no=str(payload.get("actor_no", "") or "").strip(),
    )


def _account_password(account: dict) -> str:
    encrypted = str(account.get("password_dpapi", "") or "")
    if encrypted:
        return decrypt_dpapi(encrypted)
    return str(account.get("password", "") or "")


def decrypt_dpapi(encrypted_password: str) -> str:
    try:
        import win32crypt

        _, decrypted = win32crypt.CryptUnprotectData(base64.b64decode(encrypted_password), None, None, None, 0)
    except Exception:
        return ""
    return decrypted.decode("utf-8")
