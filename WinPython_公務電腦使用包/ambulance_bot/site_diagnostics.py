from __future__ import annotations

from dataclasses import is_dataclass, replace
from typing import Any

from .adapters import SiteAutomationResult


DIAGNOSTIC_FIELDS = ("failure_stage", "failure_reason", "next_action", "exception_type")

SITE_STAGE_DEFINITIONS = {
    "duty_work_log": ["啟動 Chrome", "登入勤務系統", "新增工作紀錄", "由案件帶入", "填寫勤務資料", "儲存"],
    "vehicle_mileage": ["啟動 Chrome", "登入 PPE", "開啟車輛里程", "填寫返隊時間與里程", "儲存"],
    "fuel_record": ["啟動 Chrome", "登入 PPE", "開啟登打油耗", "填寫加油紀錄", "儲存"],
    "consumables": ["啟動 Chrome", "登入一站通", "開啟耗材紀錄", "填寫耗材品項", "儲存"],
    "disinfection": ["啟動 Chrome", "登入消毒系統", "查詢案件", "開啟消毒紀錄", "填寫消毒項目", "儲存"],
}

SITE_SHORT_NAMES = {
    "duty_work_log": "工作",
    "vehicle_mileage": "里程",
    "fuel_record": "加油",
    "consumables": "耗材",
    "disinfection": "消毒",
}

SITE_STATUS_STAGE = {
    "needs_duty_login": "登入勤務系統",
    "duty_login_failed": "登入勤務系統",
    "case_picker_opened": "由案件帶入",
    "duty_case_not_found": "由案件帶入",
    "duty_case_choose_failed": "由案件帶入",
    "duty_work_log_prefill_partial": "填寫勤務資料",
    "duty_work_log_prefilled": "儲存",
    "duty_work_log_save_failed": "儲存",
    "vehicle_mileage_prefilled": "儲存",
    "fuel_record_prefilled": "儲存",
    "manual_captcha_required": "登入一站通",
    "consumables_prefilled": "儲存",
    "disinfection_session_ready": "儲存",
    "local_pc_ready": "啟動 Chrome",
    "chrome_start_failed": "啟動 Chrome",
}

SITE_DEFAULT_FAILURE_STAGE = {
    "duty_work_log": "登入勤務系統",
    "vehicle_mileage": "開啟車輛里程",
    "fuel_record": "開啟登打油耗",
    "consumables": "開啟耗材紀錄",
    "disinfection": "查詢案件",
}


def diagnostic_payload(site_key: str, status: str, detail: str, exception: BaseException | None = None) -> dict[str, str]:
    status_text = str(status or "")
    detail_text = str(detail or "")
    category = _diagnostic_category(status_text, detail_text, exception)
    if not category:
        return {field: "" for field in DIAGNOSTIC_FIELDS}
    stage = _stage_for(site_key, status_text, detail_text, category)
    return {
        "failure_stage": stage,
        "failure_reason": _reason_for(category, status_text, detail_text),
        "next_action": _next_action_for(site_key, category),
        "exception_type": exception.__class__.__name__ if exception is not None else category,
    }


def merge_diagnostic_fields(site: dict[str, Any]) -> dict[str, str]:
    site_key = str(site.get("key") or "")
    status = str(site.get("status") or "")
    detail = str(site.get("detail") or "")
    computed = diagnostic_payload(site_key, status, detail)
    prefer_computed = computed["exception_type"] in {
        "case_not_closed",
        "ppe_driver",
        "web_renderer_timeout",
        "web_page_timeout",
        "renderer_timeout_unverified",
        "chrome_unresponsive",
        "chromedriver_ended",
    }
    def value_for(field: str) -> str:
        if prefer_computed:
            return str(computed[field])
        return str(site.get(field) or computed[field])

    merged = {
        field: value_for(field)
        for field in DIAGNOSTIC_FIELDS
    }
    return merged if merged["failure_reason"] else {field: "" for field in DIAGNOSTIC_FIELDS}


def result_with_diagnostics(
    result: SiteAutomationResult,
    exception: BaseException | None = None,
) -> SiteAutomationResult:
    computed = diagnostic_payload(result.key, result.status, result.detail, exception)
    values = {
        field: str(getattr(result, field, "") or computed[field])
        for field in DIAGNOSTIC_FIELDS
    }
    if not is_dataclass(result):
        for field, value in values.items():
            setattr(result, field, value)
        return result
    return replace(result, **values)


def make_site_result(
    site_key: str,
    site_name: str,
    status: str,
    detail: str,
    exception: BaseException | None = None,
) -> SiteAutomationResult:
    return result_with_diagnostics(SiteAutomationResult(site_key, site_name, status, detail), exception)


def _diagnostic_category(status: str, detail: str, exception: BaseException | None) -> str:
    text = f"{status} {detail}".lower()
    raw_detail = detail or ""
    if "running" in status or status in {"not_started", "created", "desktop_fast_completed", "completed_by_user"}:
        return ""
    if status.endswith("_saved"):
        return ""
    if "waiting_confirmation" in status:
        return "waiting_confirmation"
    if "prefilled" in status or "ready" in status or "captcha" in status or "未按儲存" in raw_detail:
        return "waiting_confirmation"
    for browser_category in (
        "web_renderer_timeout",
        "web_page_timeout",
        "chrome_unresponsive",
        "chromedriver_ended",
    ):
        if f"[browser_failure:{browser_category}]" in text:
            return browser_category
    if "timed out receiving message from renderer" in text:
        return "renderer_timeout_unverified"
    if _is_invalid_argument_oserror(exception, text):
        return "chrome_session"
    if "chrome" in text or "devtoolsactiveport" in text or "session not created" in text or "not reachable" in text:
        return "chrome_session"
    if "讀取任務狀態失敗" in raw_detail or "worker api" in text or "http 403" in text or "nas" in text:
        return "worker_api"
    if "fuel card not found" in text or "fuel register button not found" in text:
        return "vehicle_not_found"
    if "fuel period mismatch" in text:
        return "fuel_period"
    if (
        "missing fuel driver" in text
        or "missing vehicle mileage driver" in text
        or ("fuel_record" in status and "missing driver" in text)
    ):
        return "ppe_driver"
    if "同案多患者耗材分配／確認失敗" in raw_detail:
        return "multi_patient_consumables"
    if (
        "耗材列表找不到符合案件的內容列" in raw_detail
        or "missing disinfection detail" in text
        or ("耗材儲存後讀回不一致" in raw_detail and "actual=[]" in raw_detail)
    ):
        return "case_not_closed"
    if "captcha" in text or "驗證碼" in raw_detail or "sso" in text or "login" in text or "登入" in raw_detail or "帳密" in raw_detail:
        return "login"
    if "case not found" in text or "找不到符合案件" in raw_detail or "未在前 24 小時案件清單找到" in raw_detail:
        return "case_not_found"
    if "missing disinfection detail" in text or "無法開啟消毒紀錄" in raw_detail:
        return "case_detail"
    if "vehicle not found" in text or "找不到車輛" in raw_detail:
        return "vehicle_not_found"
    if "query failed" in text or "查詢" in raw_detail:
        return "query"
    if "missing" in text or "not found" in text or "找不到" in raw_detail or "無法按" in raw_detail:
        return "element_missing"
    if "不一致" in raw_detail or "停止儲存" in raw_detail or "驗證" in raw_detail:
        return "validation"
    if "save" in text or "儲存" in raw_detail or "確認" in raw_detail:
        return "save"
    if "failed" in status or "error" in status or exception is not None:
        return "unknown"
    return ""


def _is_invalid_argument_oserror(exception: BaseException | None, text: str) -> bool:
    if isinstance(exception, OSError):
        if getattr(exception, "errno", None) == 22:
            return True
        if "invalid argument" in str(exception).lower():
            return True
    return "[errno 22]" in text and "invalid argument" in text


def _stage_for(site_key: str, status: str, detail: str, category: str) -> str:
    if status in SITE_STATUS_STAGE:
        stage = SITE_STATUS_STAGE[status]
        if site_key == "disinfection" and status == "manual_captcha_required":
            return "登入消毒系統"
        return stage
    if category in {"chrome_session", "chrome_unresponsive", "chromedriver_ended"}:
        return "啟動 Chrome"
    if category == "worker_api":
        return "讀取任務"
    if category == "multi_patient_consumables":
        return "同案多患者耗材確認"
    if category == "ppe_driver":
        return "填寫加油紀錄" if site_key == "fuel_record" else "填寫返隊時間與里程"
    if category == "login":
        return _login_stage(site_key)
    if category == "case_not_found":
        return "由案件帶入" if site_key == "duty_work_log" else "查詢案件"
    if category == "case_not_closed":
        if site_key == "consumables":
            return "開啟耗材紀錄"
        if site_key == "disinfection":
            return "開啟消毒紀錄"
        return "查詢案件"
    if category == "case_detail":
        return "開啟消毒紀錄"
    if category == "vehicle_not_found":
        if site_key == "fuel_record":
            return SITE_DEFAULT_FAILURE_STAGE.get(site_key, "開啟登打油耗")
        return "填寫返隊時間與里程"
    if category == "fuel_period":
        return SITE_DEFAULT_FAILURE_STAGE.get(site_key, "開啟登打油耗")
    if category == "query":
        return "查詢案件"
    if category == "validation":
        if site_key == "consumables":
            return "填寫耗材品項"
        if site_key == "vehicle_mileage":
            return "填寫返隊時間與里程"
        if site_key == "fuel_record":
            return "填寫加油紀錄"
        if site_key == "disinfection":
            return "填寫消毒項目"
        return "填寫勤務資料"
    if category == "save":
        return "儲存"
    if category == "element_missing":
        return _field_stage(site_key, detail)
    if category == "waiting_confirmation":
        return SITE_STATUS_STAGE.get(status) or "儲存"
    return SITE_DEFAULT_FAILURE_STAGE.get(site_key, "執行流程")


def _login_stage(site_key: str) -> str:
    return {
        "duty_work_log": "登入勤務系統",
        "vehicle_mileage": "登入 PPE",
        "fuel_record": "登入 PPE",
        "consumables": "登入一站通",
        "disinfection": "登入消毒系統",
    }.get(site_key, "登入系統")


def _field_stage(site_key: str, detail: str) -> str:
    if site_key == "duty_work_log":
        return "填寫勤務資料"
    if site_key == "vehicle_mileage":
        return "填寫返隊時間與里程"
    if site_key == "fuel_record":
        return "填寫加油紀錄"
    if site_key == "consumables":
        return "填寫耗材品項"
    if site_key == "disinfection":
        if "detail" in detail.lower() or "開啟" in detail:
            return "開啟消毒紀錄"
        return "填寫消毒項目"
    return "填寫資料"


def _reason_for(category: str, status: str, detail: str) -> str:
    return {
        "waiting_confirmation": "資料已開啟或預填，但尚未完成儲存確認。",
        "chrome_session": "Chrome 或 ChromeDriver 工作階段無法建立或已中斷。",
        "web_renderer_timeout": "網頁轉譯程序逾時；Chrome 與 ChromeDriver 仍可連線，較可能是網頁卡住。",
        "web_page_timeout": "網頁載入或等待元件逾時；Chrome 與 ChromeDriver 仍可連線。",
        "renderer_timeout_unverified": "網頁轉譯程序回應逾時；舊紀錄未執行即時 Chrome 健康探測，無法確定是網頁卡住或 Chrome 異常。",
        "chrome_unresponsive": "Google Chrome 無回應、頁籤崩潰，或 DevTools 連線已中斷。",
        "chromedriver_ended": "ChromeDriver 程序已結束，瀏覽器自動化工作階段無法繼續。",
        "worker_api": "公務電腦與 NAS 任務狀態同步失敗。",
        "login": "登入、帳密、SSO 或驗證碼尚未完成。",
        "case_not_found": "系統清單內找不到符合本案件時間或地址的資料。",
        "case_not_closed": "案件可能尚未在救護平板結案，耗材或消毒明細尚未產生。",
        "case_detail": "找到清單後無法開啟該案件的明細頁。",
        "vehicle_not_found": "頁面內找不到任務指定的救護車。",
        "fuel_period": "加油頁月份與任務加油月份不一致，油卡清單尚未切到任務月份。",
        "ppe_driver": "PPE 駕駛清單找不到指定人員或有效代碼。",
        "multi_patient_consumables": "同案多患者耗材頁的辨識、分配、儲存或讀回確認未全部完成。",
        "validation": "送出前資料檢查不一致，程式已停止避免寫入錯誤資料。",
        "save": "填寫後的儲存動作未完成或未確認成功。",
        "query": "查詢案件時沒有取得可用結果。",
        "element_missing": "頁面按鈕或欄位與程式預期不同。",
        "unknown": "程式回報失敗，但未能歸類到明確原因。",
    }.get(category, "")


def _next_action_for(site_key: str, category: str) -> str:
    site_name = SITE_SHORT_NAMES.get(site_key, "該站")
    if category == "waiting_confirmation":
        return f"在公務電腦確認{site_name}資料無誤後手動儲存；若要重跑，請回可操作的本機任務頁重新執行該站。"
    if category == "chrome_session":
        return "關閉殘留 Chrome/ChromeDriver，重啟 worker，再重新登打。"
    if category == "web_renderer_timeout":
        return f"保留截圖，重新整理{site_name}頁面後單獨重跑；若持續發生再重啟 Chrome。"
    if category == "web_page_timeout":
        return f"查看截圖確認{site_name}頁面停在哪一步，再重新整理並單獨重跑。"
    if category == "renderer_timeout_unverified":
        return f"此舊紀錄沒有失敗截圖；更新後若再發生，後台會以即時探測結果區分網頁與 Chrome。"
    if category == "chrome_unresponsive":
        return f"關閉殘留 Chrome／ChromeDriver，重啟 Worker，再單獨重跑{site_name}。"
    if category == "chromedriver_ended":
        return f"重啟 Worker 以建立新的 Chrome 工作階段，再單獨重跑{site_name}。"
    if category == "worker_api":
        return "確認 NAS 網址與 WORKER_TOKEN，重啟 worker 後重試登打流程。"
    if category == "multi_patient_consumables":
        return "依患者序號查看成功與失敗頁面；修正一站通資料後可單獨重跑耗材。"
    if category == "login":
        return f"到公務電腦完成{site_name}登入或驗證碼，再回任務頁按「單獨登打」重試。"
    if category == "case_not_found":
        return "確認案件時間、日期、地址是否正確；必要時重新查詢案件或人工選取。"
    if category == "case_not_closed":
        return "請先去救護平板結案，完成後再回本頁按「單獨登打」重試。"
    if category == "case_detail":
        return "保留目前清單畫面，先人工開啟明細；若仍無法開啟，回報該站頁面變更。"
    if category == "vehicle_not_found":
        return "確認任務車號與系統車輛名稱一致，必要時到救護車設定修正後重試。"
    if category == "fuel_period":
        return "將加油頁月份切到任務月份後重新查詢油卡；新版 Worker 會自動切換月份後再登打。"
    if category == "ppe_driver":
        return f"確認 PPE 人員清單包含任務駕駛後，再回任務頁單獨重跑{site_name}。"
    if category == "validation":
        return "先不要儲存；檢查畫面是否仍有舊資料或欄位對應錯誤，修正後再重試。"
    if category == "save":
        return f"檢查{site_name}頁面是否有彈窗、錯誤訊息或未按到儲存；必要時手動儲存。"
    if category == "query":
        return "確認查詢日期與案件時間，必要時重新查詢或人工開啟案件。"
    if category == "element_missing":
        return f"先人工完成{site_name}，保留畫面截圖後回報程式修正。"
    return f"查看公務電腦{site_name}畫面與執行紀錄，修正後再重試。"
