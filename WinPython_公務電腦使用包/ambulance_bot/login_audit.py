from __future__ import annotations

import os
import re

from .duty_credentials import DutyCredential, load_duty_credential, load_synced_worker_credential
from .models import AmbulanceReturnRequest


def login_audit_for_site(site_key: str, request: AmbulanceReturnRequest) -> str:
    if site_key == "duty_work_log":
        return duty_work_log_login_audit(request)
    if site_key == "vehicle_mileage":
        return vehicle_mileage_login_audit(request)
    if site_key == "fuel_record":
        return fuel_record_login_audit(request)
    if site_key == "disinfection":
        return disinfection_login_audit()
    if site_key == "consumables":
        return consumables_login_audit()
    return ""


def site_login_account_summaries(request: AmbulanceReturnRequest) -> dict[str, str]:
    return {
        "duty_work_log": duty_work_log_login_summary(request),
        "vehicle_mileage": vehicle_mileage_login_summary(request),
        "fuel_record": fuel_record_login_summary(request),
        "disinfection": disinfection_login_summary(),
        "consumables": consumables_login_summary(),
    }


def duty_work_log_login_summary(request: AmbulanceReturnRequest) -> str:
    credential = load_duty_credential(request.duty_login_account_candidates)
    if credential is None:
        return "未取得（任務司機優先）"
    return f"{credential_public_label(credential)}（任務司機優先）"


def vehicle_mileage_login_summary(request: AmbulanceReturnRequest) -> str:
    credential = load_duty_credential(request.duty_login_account_candidates, fallback_user_id="", allow_default=False)
    if credential is not None:
        return f"{credential_public_label(credential)}（司機帳號優先，失敗一次改同步帳號）"
    credential = load_synced_worker_credential()
    if credential is not None:
        return f"{credential_public_label(credential)}（同步帳號）"
    account = os.getenv("PPE_ACCOUNT", "").strip() or os.getenv("DUTY_ACCOUNT", "").strip()
    password = os.getenv("PPE_PASSWORD", "").strip() or os.getenv("DUTY_PASSWORD", "").strip()
    if account and password:
        return f"{mask_login_account(account)}（環境設定）"
    return "未取得"


def fuel_record_login_summary(request: AmbulanceReturnRequest) -> str:
    return vehicle_mileage_login_summary(request)


def disinfection_login_summary() -> str:
    credential = load_synced_worker_credential()
    if credential is None:
        return "未取得（同步帳號）"
    return f"{credential_public_label(credential)}（同步帳號）"


def consumables_login_summary() -> str:
    credential = load_synced_worker_credential()
    if credential is None:
        return "未取得（同步帳號）"
    acs_account = credential.id_number.strip() or (
        credential.user_id if re.fullmatch(r"[A-Za-z][0-9]{9}", credential.user_id) else ""
    )
    if acs_account and credential.password:
        return f"{credential_public_label(credential, login_account=acs_account)}（同步帳號）"
    return f"{credential_public_label(credential)}（同步帳號，缺 ACS 帳號）"


def duty_work_log_login_audit(request: AmbulanceReturnRequest) -> str:
    credential = load_duty_credential(request.duty_login_account_candidates)
    if credential is None:
        return "登入帳號：工作=任務司機優先，未取得可用帳號"
    return f"登入帳號：工作=任務司機優先，{credential_public_label(credential)}"


def vehicle_mileage_login_audit(request: AmbulanceReturnRequest) -> str:
    credential = load_duty_credential(request.duty_login_account_candidates, fallback_user_id="", allow_default=False)
    if credential is not None:
        return f"登入帳號：里程=司機帳號優先，失敗一次改同步帳號，{credential_public_label(credential)}"
    credential = load_synced_worker_credential()
    if credential is not None:
        return f"登入帳號：里程=公務電腦同步帳號，{credential_public_label(credential)}"
    account = os.getenv("PPE_ACCOUNT", "").strip() or os.getenv("DUTY_ACCOUNT", "").strip()
    password = os.getenv("PPE_PASSWORD", "").strip() or os.getenv("DUTY_PASSWORD", "").strip()
    if account and password:
        return f"登入帳號：里程=環境設定，{mask_login_account(account)}"
    return "登入帳號：里程=未取得可用帳號"


def fuel_record_login_audit(request: AmbulanceReturnRequest) -> str:
    return vehicle_mileage_login_audit(request).replace("里程", "加油", 1)


def disinfection_login_audit() -> str:
    credential = load_synced_worker_credential()
    if credential is None:
        return "登入帳號：消毒=公務電腦同步帳號，未取得可用帳號"
    return f"登入帳號：消毒=公務電腦同步帳號，{credential_public_label(credential)}"


def consumables_login_audit() -> str:
    credential = load_synced_worker_credential()
    if credential is None:
        return "登入帳號：耗材=公務電腦同步帳號，未取得可用帳號"
    acs_account = credential.id_number.strip() or (
        credential.user_id if re.fullmatch(r"[A-Za-z][0-9]{9}", credential.user_id) else ""
    )
    if acs_account and credential.password:
        return f"登入帳號：耗材=公務電腦同步帳號，{credential_public_label(credential, login_account=acs_account)}"
    return f"登入帳號：耗材=公務電腦同步帳號，{credential_public_label(credential)}（缺 ACS 可用帳號）"


def with_login_audit(detail: str, audit: str) -> str:
    clean_detail = str(detail or "").strip()
    clean_audit = str(audit or "").strip()
    if not clean_audit or clean_audit in clean_detail:
        return clean_detail
    return f"{clean_audit}。{clean_detail}" if clean_detail else clean_audit


def credential_public_label(credential: DutyCredential, login_account: str = "") -> str:
    account = mask_login_account(login_account or credential.user_id)
    actor = f"{credential.actor_no}番" if credential.actor_no else ""
    name = credential.name or _name_from_display_name(
        credential.display_name,
        account=credential.user_id,
        actor_no=credential.actor_no,
    )
    identity = " ".join(item for item in (actor, name) if item).strip()
    if identity and account:
        return f"{identity} - {account}"
    return identity or account or "未填帳號"


def mask_login_account(account: str) -> str:
    text = str(account or "").strip()
    if re.fullmatch(r"[A-Za-z][0-9]{9}", text):
        return f"{text[:4]}***{text[-3:]}"
    return text


def _name_from_display_name(display_name: str, account: str = "", actor_no: str = "") -> str:
    text = str(display_name or "").strip()
    if not text:
        return ""
    for token in (account, actor_no):
        if token:
            text = text.replace(token, " ")
    text = text.replace("番", " ")
    parts = [part for part in re.split(r"[\s｜|-]+", text) if part]
    for part in reversed(parts):
        if not re.fullmatch(r"tyfd\d+|[A-Za-z][0-9]{9}|\d+", part):
            return part
    return parts[-1] if parts else ""
