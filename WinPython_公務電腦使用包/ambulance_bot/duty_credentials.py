from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class DutyCredential:
    user_id: str
    password: str
    actor_no: str = ""
    display_name: str = ""
    name: str = ""
    id_number: str = ""


def load_duty_credential(
    preferred_user_ids: Iterable[str] | None = None,
    fallback_user_id: str = "",
) -> DutyCredential | None:
    preferred = _normalized_user_ids(preferred_user_ids)
    fallback = str(fallback_user_id or "").strip()

    if preferred:
        selected = _find_saved_credential(preferred, duty_password=True)
        if selected is not None:
            return selected
        env_credential = _env_credential()
        if env_credential is not None and _credential_matches_any(env_credential, preferred):
            return env_credential

    if fallback:
        selected = _find_saved_credential([fallback], duty_password=True)
        if selected is not None:
            return selected
        env_credential = _env_credential()
        if env_credential is not None and _credential_matches_any(env_credential, [fallback]):
            return env_credential

    saved = load_saved_duty_work_credential()
    if saved is not None:
        return saved

    env_credential = _env_credential()
    if env_credential is not None:
        return env_credential

    return None


def load_synced_worker_credential(path: Path | None = None) -> DutyCredential | None:
    return load_saved_duty_automation_credential(path)


def load_saved_duty_work_credential(path: Path | None = None) -> DutyCredential | None:
    return load_saved_duty_automation_credential(path, duty_password=True)


def _env_credential() -> DutyCredential | None:
    user_id = os.getenv("DUTY_ACCOUNT", "").strip()
    password = os.getenv("DUTY_PASSWORD", "")
    if user_id and password:
        return DutyCredential(user_id=user_id, password=password)
    return None


def _find_saved_credential(user_ids: list[str], duty_password: bool = False) -> DutyCredential | None:
    credentials = list_saved_duty_automation_credentials(duty_password=duty_password)
    for user_id in user_ids:
        for credential in credentials:
            if _credential_matches_any(credential, [user_id]):
                return credential
    return None


def _credential_matches_any(credential: DutyCredential, user_ids: list[str]) -> bool:
    identities = {
        credential.user_id.lower(),
        credential.actor_no.lower(),
        credential.id_number.lower(),
        credential.name.lower(),
        credential.display_name.lower(),
    }
    return any(user_id.lower() in identities for user_id in user_ids)


def _normalized_user_ids(values: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        user_id = str(value or "").strip()
        if not user_id:
            continue
        key = user_id.lower()
        if key not in seen:
            result.append(user_id)
            seen.add(key)
    return result


def load_saved_duty_automation_credential(path: Path | None = None, duty_password: bool = False) -> DutyCredential | None:
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
    password = _account_duty_password(selected) if duty_password else _account_password(selected)
    user_id = str(selected.get("user_id", "") or "").strip()
    if not user_id or not password:
        return None
    return DutyCredential(
        user_id=user_id,
        password=password,
        actor_no=str(selected.get("actor_no", "") or "").strip(),
        display_name=str(selected.get("display_name", "") or "").strip(),
        name=str(selected.get("name", "") or "").strip(),
        id_number=str(selected.get("id_number", "") or "").strip(),
    )


def list_saved_duty_automation_credentials(path: Path | None = None, duty_password: bool = False) -> list[DutyCredential]:
    source = path or saved_login_path()
    if not source.exists():
        return []
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []

    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        credential = _legacy_credential(payload)
        return [credential] if credential is not None else []

    credentials: list[DutyCredential] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        credential = _credential_from_account(account, duty_password=duty_password)
        if credential is not None:
            credentials.append(credential)

    last_selected = str(payload.get("last_selected", "") or "").strip()
    if last_selected:
        credentials.sort(key=lambda item: 0 if item.user_id == last_selected or item.actor_no == last_selected else 1)
    return credentials


def saved_login_path() -> Path:
    configured = os.getenv("DUTY_SAVED_LOGIN_PATH", "").strip()
    override = os.getenv("DUTY_SAVED_LOGIN_PATH_OVERRIDE", "").strip().lower() in {"1", "true", "yes", "on"}
    if configured and override:
        return Path(os.path.expandvars(configured)).expanduser()
    return default_saved_login_path()


def default_saved_login_path() -> Path:
    return Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "DutyAutomation" / "saved_login.json"


def legacy_configured_saved_login_path() -> Path | None:
    configured = os.getenv("DUTY_SAVED_LOGIN_PATH", "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return None


def set_last_selected_duty_automation_credential(identifier: str, path: Path | None = None) -> Path:
    selected = str(identifier or "").strip()
    if not selected:
        raise ValueError("selected credential id is required")

    source = path or saved_login_path()
    payload = _read_saved_login(source)
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError("saved credential file does not contain synced accounts")
    if _select_account(accounts, selected) is None:
        raise ValueError(f"saved credential not found: {selected}")

    payload["last_selected"] = selected
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return source


def _select_account(accounts: list[object], last_selected: str) -> dict | None:
    candidates = [account for account in accounts if isinstance(account, dict)]
    if not candidates:
        return None
    if last_selected:
        for account in candidates:
            identities = {
                str(account.get("user_id", "") or "").strip(),
                str(account.get("actor_no", "") or "").strip(),
                str(account.get("id_number", "") or "").strip(),
            }
            if last_selected in identities:
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
        display_name=str(payload.get("display_name", "") or "").strip(),
        name=str(payload.get("name", "") or "").strip(),
        id_number=str(payload.get("id_number", "") or "").strip(),
    )


def _credential_from_account(account: dict, duty_password: bool = False) -> DutyCredential | None:
    user_id = str(account.get("user_id", "") or "").strip()
    password = _account_duty_password(account) if duty_password else _account_password(account)
    if not user_id or not password:
        return None
    return DutyCredential(
        user_id=user_id,
        password=password,
        actor_no=str(account.get("actor_no", "") or "").strip(),
        display_name=str(account.get("display_name", "") or "").strip(),
        name=str(account.get("name", "") or "").strip(),
        id_number=str(account.get("id_number", "") or "").strip(),
    )


def save_duty_automation_credential(
    user_id: str,
    password: str,
    actor_no: str = "",
    display_name: str = "",
    name: str = "",
    id_number: str = "",
    path: Path | None = None,
) -> Path:
    return save_duty_automation_credentials(
        [
            {
                "actor_no": actor_no,
                "user_id": user_id,
                "password": password,
                "display_name": display_name,
                "name": name,
                "id_number": id_number,
            }
        ],
        last_selected=str(user_id or "").strip(),
        path=path,
    )


def save_duty_automation_credentials(
    accounts: Iterable[dict[str, object]],
    last_selected: str = "",
    path: Path | None = None,
) -> Path:
    normalized_accounts: list[dict[str, str]] = []
    for account in accounts:
        normalized = _normalized_account_for_save(account)
        if normalized is not None:
            normalized_accounts.append(normalized)
    if not normalized_accounts:
        raise ValueError("at least one account with user_id and password is required")

    source = path or saved_login_path()
    existing = _read_saved_login(source)
    accounts = existing.get("accounts")
    account_list = [account for account in accounts if isinstance(account, dict)] if isinstance(accounts, list) else []

    for normalized in normalized_accounts:
        password = normalized.pop("password")
        duty_password = normalized.pop("duty_password", "")
        encrypted_password = encrypt_dpapi(password)
        if not encrypted_password:
            raise RuntimeError("Windows DPAPI is not available")
        updated = {
            **normalized,
            "password_dpapi": encrypted_password,
        }
        if duty_password:
            encrypted_duty_password = encrypt_dpapi(duty_password)
            if not encrypted_duty_password:
                raise RuntimeError("Windows DPAPI is not available")
            updated["duty_password_dpapi"] = encrypted_duty_password
        _upsert_account(account_list, updated)

    selected = str(last_selected or "").strip()
    if not selected:
        selected = normalized_accounts[0]["user_id"]

    payload = {
        "last_selected": selected,
        "accounts": account_list,
    }
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return source


def credential_sync_accounts_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    accounts_payload = payload.get("accounts")
    if isinstance(accounts_payload, list):
        accounts = [account for account in accounts_payload if isinstance(account, dict)]
    else:
        accounts = [payload]
    return [
        account
        for account in accounts
        if str(account.get("user_id") or "").strip() and str(account.get("password") or "")
    ]


def select_credential_sync_account(accounts: list[dict[str, object]], payload: dict[str, object]) -> dict[str, object] | None:
    if not accounts:
        return None
    selected_user_id = str(payload.get("user_id") or "").strip()
    selected_actor_no = str(payload.get("actor_no") or "").strip()
    for account in accounts:
        user_id = str(account.get("user_id") or "").strip()
        actor_no = str(account.get("actor_no") or "").strip()
        if selected_user_id and user_id == selected_user_id:
            return account
        if selected_actor_no and actor_no == selected_actor_no:
            return account
    return accounts[0]


def save_credential_sync_payload(payload: dict[str, object], path: Path | None = None) -> tuple[str, str, Path, int] | None:
    accounts = credential_sync_accounts_from_payload(payload)
    selected = select_credential_sync_account(accounts, payload)
    if selected is None:
        return None
    user_id = str(selected.get("user_id") or "").strip()
    password = str(selected.get("password") or "")
    if not user_id or not password:
        return None
    last_selected = str(payload.get("user_id") or payload.get("actor_no") or user_id).strip()
    saved_path = save_duty_automation_credentials(accounts, last_selected=last_selected, path=path)
    os.environ["DUTY_ACCOUNT"] = user_id
    os.environ["DUTY_PASSWORD"] = password
    return user_id, password, saved_path, len(accounts)


def _normalized_account_for_save(account: dict[str, object]) -> dict[str, str] | None:
    user_id = str(account.get("user_id", "") or "").strip()
    password = str(account.get("password", "") or "")
    if not user_id or not password:
        return None
    duty_password = _first_account_value(account, "duty_password", "work_password", "work_log_password")
    actor_no = str(account.get("actor_no", "") or "").strip()
    display_name = str(account.get("display_name", "") or "").strip()
    name = str(account.get("name", "") or "").strip()
    if not name:
        name = _name_from_display_name(display_name, account=user_id, actor_no=actor_no)
    return {
        "actor_no": actor_no,
        "user_id": user_id,
        "password": password,
        "duty_password": duty_password,
        "display_name": display_name,
        "name": name,
        "id_number": str(account.get("id_number", "") or "").strip(),
    }


def _name_from_display_name(display_name: str, account: str = "", actor_no: str = "") -> str:
    text = str(display_name or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\s*\d+\s*番\s*", "", text).strip()
    account_text = str(account or "").strip()
    actor_text = str(actor_no or "").strip()
    if account_text and text.lower() == account_text.lower():
        return ""
    if actor_text and text == actor_text:
        return ""
    return text


def _upsert_account(account_list: list[dict], updated: dict[str, str]) -> None:
    user_id = updated["user_id"]
    actor_no = updated.get("actor_no", "")
    for index, account in enumerate(account_list):
        identity = str(account.get("user_id", "") or account.get("actor_no", "") or "").strip()
        if identity == user_id or (actor_no and identity == actor_no):
            account_list[index] = updated
            return
    account_list.append(updated)


def _read_saved_login(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _account_password(account: dict) -> str:
    encrypted = str(account.get("password_dpapi", "") or "")
    if encrypted:
        return decrypt_dpapi(encrypted)
    return str(account.get("password", "") or "")


def _account_duty_password(account: dict) -> str:
    encrypted = _first_account_value(account, "duty_password_dpapi", "work_password_dpapi", "work_log_password_dpapi")
    if encrypted:
        return decrypt_dpapi(encrypted)
    password = _first_account_value(account, "duty_password", "work_password", "work_log_password")
    return password or _account_password(account)


def _first_account_value(account: dict, *keys: str) -> str:
    for key in keys:
        value = str(account.get(key, "") or "")
        if value:
            return value
    return ""


def encrypt_dpapi(password: str) -> str:
    try:
        import win32crypt

        encrypted = win32crypt.CryptProtectData(password.encode("utf-8"), "SinpoSmart credential sync", None, None, None, 0)
    except Exception:
        encrypted = _encrypt_dpapi_ctypes(password.encode("utf-8"))
        if not encrypted:
            return ""
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_dpapi(encrypted_password: str) -> str:
    encrypted = base64.b64decode(encrypted_password)
    try:
        import win32crypt

        _, decrypted = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
    except Exception:
        decrypted = _decrypt_dpapi_ctypes(encrypted)
        if not decrypted:
            return ""
    return decrypted.decode("utf-8")


def _encrypt_dpapi_ctypes(data: bytes) -> bytes:
    return _crypt_dpapi_ctypes(data, protect=True)


def _decrypt_dpapi_ctypes(data: bytes) -> bytes:
    return _crypt_dpapi_ctypes(data, protect=False)


def _crypt_dpapi_ctypes(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        return b""
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        source = ctypes.create_string_buffer(data)
        blob_in = DATA_BLOB(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
        blob_out = DATA_BLOB()
        if protect:
            ok = crypt32.CryptProtectData(
                ctypes.byref(blob_in),
                "Ambulance return credential sync",
                None,
                None,
                None,
                0,
                ctypes.byref(blob_out),
            )
        else:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(blob_in),
                None,
                None,
                None,
                None,
                0,
                ctypes.byref(blob_out),
            )
        if not ok:
            return b""
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return b""
