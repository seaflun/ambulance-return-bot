from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re


DEFAULT_EMS_RECORD_ROOT = Path(r"W:\救護硬碟\救護密錄器及行車紀錄器")
DEFAULT_DISASTER_RECORD_ROOT = Path(r"\\100.114.126.58\nas\搶救災害硬碟\救災行車紀錄器")

REASON_SHORT_LABELS = {
    "商店(量販店)": "商店",
    "公共場所(機場、車站)": "公共場所",
    "隧道": "隧道",
    "航空器、火車等大眾運輸工具": "大眾運輸工具",
    "船舶": "船舶",
    "汽機車": "汽機車",
    "雜草(含廢棄物、墓地)": "雜草",
    "誤(謊)報": "誤報",
    "其他": "其他",
    "一般(集合)住宅": "住宅",
    "高層(超高)建築物": "高層建築物",
    "地下建築物": "地下建築物",
    "臨時屋(含工寮、樣品屋、雞舍等無門牌之建築物)": "臨時屋",
    "機關、學校(軍公教辦公廳舍、宿舍)": "機關學校",
    "山林": "山林",
    "工廠及倉庫(含石化工業設備設施)": "工廠",
    "爆炸": "爆炸",
}


class RecordFolderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FolderPlanEntry:
    vehicle: str
    path: Path


@dataclass(frozen=True, slots=True)
class FolderResult:
    vehicle: str
    path: Path
    status: str


def disaster_record_root() -> Path:
    configured = str(os.getenv("DISASTER_RECORD_ROOT") or "").strip()
    return Path(configured) if configured else DEFAULT_DISASTER_RECORD_ROOT


def safe_folder_component(value: object) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(value or "")).strip().rstrip(". ")
    if not text:
        raise RecordFolderError("資料夾名稱清理後為空")
    return text


def recorder_vehicle_code(vehicle: str, recorder_codes: dict[str, str] | None = None) -> str:
    configured = str((recorder_codes or {}).get(str(vehicle or "").strip()) or "").strip()
    if configured:
        return safe_folder_component(configured)
    digits = "".join(character for character in str(vehicle or "") if character.isdigit())
    if not digits:
        raise RecordFolderError(f"車輛缺少行車紀錄器車號代碼：{vehicle or '未填車輛'}")
    return digits


def recorder_category_directory(value: str) -> str:
    return "A2" if value == "轄內A2" else safe_folder_component(value)


def disaster_folder_plan(
    request,
    root: Path | None = None,
    recorder_codes: dict[str, str] | None = None,
) -> list[FolderPlanEntry]:
    root = Path(root) if root is not None else disaster_record_root()
    case_date = request.service_case_date()
    if case_date.year <= 1911:
        raise RecordFolderError("案件年份無法轉換為民國年份")
    year_folder = f"{case_date.year - 1911}年"
    category = str(request.recorder_category or "").strip()
    category_directory = recorder_category_directory(category)
    path_parts = [year_folder, category_directory]
    if category == "轄內其他案件":
        subcategory = safe_folder_component(request.recorder_subcategory)
        path_parts.append(subcategory)
        name_label = subcategory
    else:
        reason = str(request.case_reason or "").strip()
        reason_label = REASON_SHORT_LABELS.get(reason)
        name_label = f"{reason_label or safe_folder_component(reason)}火警"
    hhmm = re.sub(r"\D", "", str(request.case_time or ""))[:4]
    if len(hhmm) != 4:
        raise RecordFolderError("案件時間格式需為 HHmm")
    address = safe_folder_component(request.case_address)
    base_name = safe_folder_component(f"{case_date:%Y%m%d}{hhmm}{address}({name_label})")
    return [
        FolderPlanEntry(
            entry.vehicle,
            root.joinpath(*path_parts, f"{base_name}-{recorder_vehicle_code(entry.vehicle, recorder_codes)}"),
        )
        for entry in request.effective_vehicle_entries()
    ]


def ensure_disaster_record_folders(
    request,
    root: Path | None = None,
    recorder_codes: dict[str, str] | None = None,
) -> list[FolderResult]:
    plan = disaster_folder_plan(request, root, recorder_codes)
    for entry in plan:
        if entry.path.exists() and not entry.path.is_dir():
            raise RecordFolderError(f"同名物件不是資料夾：{entry.path}")
    results: list[FolderResult] = []
    for entry in plan:
        existed = entry.path.is_dir()
        entry.path.mkdir(parents=True, exist_ok=True)
        if not entry.path.is_dir():
            raise RecordFolderError(f"資料夾建立後無法驗證：{entry.path}")
        results.append(FolderResult(entry.vehicle, entry.path, "reused" if existed else "created"))
    return results


def ems_record_relative_paths(request) -> list[Path]:
    paths: list[Path] = []
    for index, entry in enumerate(request.effective_vehicle_entries(), start=1):
        case_date = request.service_case_date()
        hhmm = re.sub(r"\D", "", str(request.case_time or ""))[:4]
        if len(hhmm) != 4:
            continue
        vehicle = recorder_vehicle_code(entry.vehicle) if entry.vehicle else str(index)
        paths.append(Path(str(case_date.year)) / f"{case_date.month}月" / f"{case_date:%m%d}{hhmm}-{vehicle}")
    return paths


def ensure_ems_record_folders(request, root: Path = DEFAULT_EMS_RECORD_ROOT) -> list[FolderResult]:
    results: list[FolderResult] = []
    for entry, relative in zip(request.effective_vehicle_entries(), ems_record_relative_paths(request)):
        folder = Path(root) / relative
        existed = folder.is_dir()
        for child in ("1", "2", "車"):
            (folder / child).mkdir(parents=True, exist_ok=True)
        results.append(FolderResult(entry.vehicle, folder, "reused" if existed else "created"))
    return results
