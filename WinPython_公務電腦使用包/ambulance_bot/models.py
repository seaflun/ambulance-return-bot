from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable
from uuid import uuid4


DEFAULT_CONSUMABLES = {"桃-口罩(片)": 2, "桃-9吋手套-L(雙)": 2}
DISINFECTION_ITEM_OPTIONS = [
    "\u6551\u8b77\u8eca\u9ad4",
    "\u64d4\u67b6\u5e8a",
    "\u64d4\u67b6\u5e8a\u588a",
    "\u5152\u7ae5\u64d4\u67b6\u56fa\u5b9a\u5668",
    "\u5b30\u5152\u64d4\u67b6\u56fa\u5b9a\u5668",
    "\u642c\u904b\u6905",
    "\u56fa\u5b9a\u5f0f\u6c27\u6c23\u7d44",
    "\u81ea\u52d5\u7d66\u6c27\u6a5f",
    "\u651c\u5e36\u5f0f\u6c27\u6c23\u7d44(\u542b\u5167\u5bb9\u7269)",
    "\u6025\u6551\u7bb1/\u6025\u6551\u5305",
    "\u651c\u5e36\u5f0f\u62bd\u5438\u5668",
    "\u9577\u80cc\u677f(\u542b\u982d\u90e8\u56fa\u5b9a\u5668)",
    "\u93df\u5f0f\u64d4\u67b6(\u542b\u982d\u90e8\u56fa\u5b9a\u5668)",
    "\u9aa8\u6298\u56fa\u5b9a\u677f",
    "\u62bd\u6c23\u5f0f\u8b77\u6728",
    "\u8ec0\u5e79\u56fa\u5b9a\u5668",
    "\u8840\u6c27\u6fc3\u5ea6\u5206\u6790\u5100",
    "\u9ad4\u6eab\u8a08",
    "\u8840\u58d3\u8a08",
    "\u8840\u7cd6\u6a5f",
    "\u5fc3\u81df\u96fb\u64ca\u53bb\u986b\u5668",
    "\u81ea\u52d5\u5fc3\u80ba\u5fa9\u7526\u6a5f",
    "\u6210\u4eba\u7526\u9192\u7403",
    "\u5152\u7ae5\u7526\u9192\u7403",
    "\u5b30\u5152\u7526\u9192\u7403",
    "\u6210\u4eba\u9838\u5708",
    "\u5152\u7ae5\u9838\u5708",
    "\u6bdb\u6bef/\u88ab\u5b50",
    "\u88ab\u55ae",
    "\u9ad8\u6551\u5305(\u542b\u5167\u5bb9\u7269)",
    "\u5927\u91cf\u50b7\u75c5\u60a3\u4e8b\u4ef6\u5668\u6750\u5305(\u542b\u5167\u5bb9\u7269)",
]
DEFAULT_DISINFECTION_ITEMS: list[str] = [
    "救護車體",
    "擔架床",
    "擔架床墊",
    "攜帶式氧氣組(含內容物)",
    "急救箱/急救包",
    "血氧濃度分析儀",
    "體溫計",
    "血壓計",
]
COMMAND_PREFIX = "\u6551\u8b77\u56de\u7a0b"
VEHICLE_OPTIONS = ["\u65b0\u576191", "\u65b0\u576192", "\u65b0\u576193"]
VEHICLE_PPE_NAMES = {
    "\u65b0\u576191": "BGV-2310",
    "\u65b0\u576192": "BXB-7593",
    "\u65b0\u576193": "BSL-9230",
}
DEFAULT_CUSTOM_VEHICLES = [{"label": "\u65b0\u576195", "ppe_name": "BPE-5951"}]
PERSON_OPTIONS = [
    ("6", "\u5433\u5b97\u8015"),
    ("7", "\u5305\u83ef\u5148"),
    ("8", "\u66fe\u5f65\u7db8"),
    ("9", "\u937e\u4f73\u8aed"),
    ("10", "\u6797\u5b8f\u6fa4"),
    ("11", "\u7c21\u541b\u8afa"),
    ("12", "\u738b\u6631\u52db"),
    ("13", "\u8449\u5b97\u54f2"),
    ("14", "\u738b\u6d69\u4efb"),
    ("15", "\u674e\u4ed5\u8a6e"),
    ("16", "\u694a\u5f18\u5b87"),
    ("17", "\u694a\u4ef2\u8c6a"),
    ("18", "\u6797\u5fd7\u5049"),
    ("19", "\u5289\u5bb6\u8aa0"),
    ("21", "\u5f35\u5bb6\u548c"),
    ("23", "\u9673\u4fca\u7ff0"),
    ("24", "\u8cf4\u4fca\u8c6a"),
    ("25", "\u90ed\u570b\u5075"),
    ("26", "\u694a\u7d39\u6587"),
    ("27", "\u6797\u5b8f\u70ba"),
    ("28", "\u6797\u5bb8\u5f65"),
    ("1", "\u9127\u529b\u5609"),
    ("2", "\u838a\u52dd\u658c"),
    ("3", "\u5b6b\u5b50\u7fd4"),
    ("4", "\u7c21\u6c38\u8c50"),
    ("5", "\u5f35\u9d3b\u5fd7"),
]
CASE_REASON_OPTIONS = [
    "\u6025\u75c5",
    "\u8eca\u798d",
    "\u8def\u5012",
    "\u7a7a\u8dd1",
    "\u5275\u50b7",
    "\u81ea\u6bba",
    "\u5b55\u5a66\u6025\u7522",
    "\u85e5\u7269\u3001\u98df\u7269\u4e2d\u6bd2",
    "\u4e00\u6c27\u5316\u70ad\u4e2d\u6bd2",
    "\u96fb\u64ca\u50b7",
    "\u6eba\u6c34",
    "\u7cbe\u795e\u7570\u5e38",
    "\u751f\u7269\u54ac\u87ab\u50b7",
    "\u71d2\u71d9\u50b7",
    "\u7570\u7269\u54fd\u585e",
    "\u8aa4(\u8b0a)\u5831",
    "\u5176\u4ed6",
]


def vehicle_settings_path(base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return base_dir / "settings" / "vehicles.json"
    configured = os.getenv("VEHICLE_SETTINGS_PATH")
    if configured:
        return Path(configured)
    return Path(os.getenv("ARTIFACTS_DIR", "artifacts")) / "settings" / "vehicles.json"


def read_vehicle_settings(base_dir: Path | None = None) -> dict[str, Any]:
    path = vehicle_settings_path(base_dir)
    if not path.exists():
        return {"vehicles": [], "deleted": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"vehicles": [], "deleted": []}
    if isinstance(payload, list):
        return {"vehicles": payload, "deleted": []}
    if not isinstance(payload, dict):
        return {"vehicles": [], "deleted": []}
    vehicles = payload.get("vehicles")
    deleted = payload.get("deleted")
    return {
        "vehicles": vehicles if isinstance(vehicles, list) else [],
        "deleted": deleted if isinstance(deleted, list) else [],
    }


def _clean_vehicle_records(records: Any) -> list[dict[str, str]]:
    if not isinstance(records, list):
        return []
    cleaned: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        label = str(record.get("label") or "").strip()
        ppe_name = str(record.get("ppe_name") or record.get("plate") or "").strip()
        if label:
            cleaned.append({"label": label, "ppe_name": ppe_name})
    return cleaned


def write_vehicle_settings(settings: dict[str, Any], base_dir: Path | None = None) -> None:
    path = vehicle_settings_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "vehicles": _clean_vehicle_records(settings.get("vehicles")),
        "deleted": [str(label).strip() for label in settings.get("deleted", []) if str(label).strip()],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_vehicle_records(base_dir: Path | None = None) -> list[dict[str, str]]:
    settings = read_vehicle_settings(base_dir)
    deleted = {str(label).strip() for label in settings.get("deleted", []) if str(label).strip()}
    records = [record for record in DEFAULT_CUSTOM_VEHICLES if record["label"] not in deleted]
    for record in _clean_vehicle_records(settings.get("vehicles")):
        records = [existing for existing in records if existing["label"] != record["label"]]
        records.append(record)
    return records


def save_vehicle_record(label: str, ppe_name: str = "", base_dir: Path | None = None) -> None:
    label = label.strip()
    ppe_name = ppe_name.strip()
    if not label:
        raise ValueError("missing vehicle label")
    records = load_vehicle_records(base_dir)
    for record in records:
        if record["label"] == label:
            record["ppe_name"] = ppe_name
            break
    else:
        records.append({"label": label, "ppe_name": ppe_name})
    settings = read_vehicle_settings(base_dir)
    settings["vehicles"] = records
    settings["deleted"] = [deleted for deleted in settings.get("deleted", []) if deleted != label]
    write_vehicle_settings(settings, base_dir)


def delete_vehicle_record(label: str, base_dir: Path | None = None) -> bool:
    label = label.strip()
    if not label or label in VEHICLE_OPTIONS:
        return False
    settings = read_vehicle_settings(base_dir)
    records = [record for record in _clean_vehicle_records(settings.get("vehicles")) if record["label"] != label]
    deleted = [str(item).strip() for item in settings.get("deleted", []) if str(item).strip()]
    if label in {record["label"] for record in DEFAULT_CUSTOM_VEHICLES} and label not in deleted:
        deleted.append(label)
    settings["vehicles"] = records
    settings["deleted"] = deleted
    write_vehicle_settings(settings, base_dir)
    return True


def vehicle_options(base_dir: Path | None = None) -> list[str]:
    options = list(VEHICLE_OPTIONS)
    for record in load_vehicle_records(base_dir):
        if record["label"] not in options:
            options.append(record["label"])
    return options


def vehicle_ppe_names(base_dir: Path | None = None) -> dict[str, str]:
    names = dict(VEHICLE_PPE_NAMES)
    for record in load_vehicle_records(base_dir):
        if record["ppe_name"]:
            names[record["label"]] = record["ppe_name"]
    return names


@dataclass(slots=True)
class AmbulanceReturnRequest:
    task_id: str
    created_at: datetime
    raw_text: str
    vehicle: str = ""
    driver: str = ""
    mileage: str = ""
    case_id: str = ""
    personnel: list[str] = field(default_factory=list)
    personnel_accounts: list[str] = field(default_factory=list)
    case_date: str = ""
    case_time: str = ""
    return_date: str = ""
    return_time: str = ""
    case_address: str = ""
    patient_summary: str = "\u7537\u4e00\u540d"
    case_reason: str = "\u6025\u75c5"
    disinfection: str = "\u6551\u8b77\u8fd4\u968a\u5f8c\u8eca\u5167\u3001\u64d4\u67b6\u53ca\u63a5\u89f8\u9762\u5b8c\u6210\u6d88\u6bd2\u3002"
    disinfection_items: list[str] = field(default_factory=lambda: list(DEFAULT_DISINFECTION_ITEMS))
    work_note: str = "\u6551\u8b77\u6848\u4ef6\u8fd4\u968a\u5f8c\u5b8c\u6210\u8eca\u8f1b\u3001\u8017\u6750\u53ca\u6d88\u6bd2\u767b\u6253\u3002"
    consumables: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_CONSUMABLES))

    @property
    def consumable_summary(self) -> str:
        return "\u3001".join(f"{name} x{qty}" for name, qty in self.consumables.items() if name and qty > 0)

    @property
    def disinfection_items_summary(self) -> str:
        return "\u3001".join(item for item in self.disinfection_items if item)

    @property
    def tyfd_personnel_accounts(self) -> list[str]:
        return [account for account in self.personnel_accounts if account.lower().startswith("tyfd")]

    @property
    def duty_login_account_candidates(self) -> list[str]:
        accounts = [account.strip() for account in self.personnel_accounts if account.strip()]
        if not accounts:
            return []
        ordered: list[str] = []
        driver = self.driver.strip()
        if driver:
            for index, name in enumerate(self.personnel):
                if index < len(accounts) and name.strip() == driver:
                    ordered.append(accounts[index])
        ordered.extend(accounts)
        deduped: list[str] = []
        seen: set[str] = set()
        for account in ordered:
            key = account.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(account)
        return deduped

    @property
    def consumables_account_candidates(self) -> list[str]:
        return [account for account in self.personnel_accounts if re.fullmatch(r"[A-Za-z][0-9]{9}", account)]

    @property
    def summary(self) -> str:
        missing = "\u672a\u586b"
        rows = [
            f"\u4efb\u52d9\uff1a{self.task_id}",
            f"\u8eca\u8f1b\uff1a{self.vehicle or missing}",
            f"\u53f8\u6a5f\uff1a{self.driver or missing}",
            f"\u91cc\u7a0b\uff1a{self.mileage or missing}",
            f"\u6848\u4ef6\u6642\u9593\uff1a{self.case_time or missing}",
            f"\u56de\u7a0b\u6642\u9593\uff1a{self.return_time or missing}",
            f"\u6848\u767c\u5730\u5740\uff1a{clean_case_address(self.case_address) or missing}",
            f"\u4e8b\u7531\uff1a{self.case_reason or missing}",
            f"\u50b7\u75c5\u60a3\uff1a{self.patient_summary or missing}",
            f"\u8017\u6750\uff1a{self.consumable_summary}",
            f"\u6d88\u6bd2\uff1a{self.disinfection}",
            f"\u6d88\u6bd2\u9805\u76ee\uff1a{self.disinfection_items_summary or '\u672a\u9078'}",
            f"\u5de5\u4f5c\u7d00\u9304\uff1a{self.work_note}",
        ]
        return "\n".join(rows)

    @property
    def duty_status_text(self) -> str:
        vehicle = self.vehicle or "\u672a\u586b\u8eca\u8f1b"
        driver = self.driver or "\u672a\u586b\u53f8\u6a5f"
        patient = self.patient_summary or "\u7537\u4e00\u540d"
        if patient == "\u7121":
            return f"{vehicle};{driver}"
        return f"1.{vehicle}:{driver}\n2.{patient}"

    @property
    def return_time_hhmm(self) -> str:
        return normalize_hhmm(self.return_time)

    @property
    def return_time_description_line(self) -> str:
        hhmm = self.return_time_hhmm
        if len(hhmm) != 4:
            return ""
        value_date = self.service_return_date()
        return f"\u8fd4\u968a\u6642\u9593:{value_date:%Y/%m/%d} {hhmm[:2]}:{hhmm[2:]}:00"

    def service_case_date(self) -> datetime:
        parsed = parse_case_date(self.case_date)
        if parsed:
            return parsed
        value = self.created_at
        case_hhmm = normalize_hhmm(self.case_time)
        return_hhmm = self.return_time_hhmm
        if len(case_hhmm) == 4 and len(return_hhmm) == 4 and int(case_hhmm) > int(return_hhmm):
            value = value - __import__("datetime").timedelta(days=1)
        return value

    def service_return_date(self) -> datetime:
        parsed = parse_case_date(self.return_date)
        if parsed:
            return parsed
        value = self.service_case_date()
        case_hhmm = normalize_hhmm(self.case_time)
        return_hhmm = self.return_time_hhmm
        if len(case_hhmm) == 4 and len(return_hhmm) == 4 and int(return_hhmm) < int(case_hhmm):
            value = value + __import__("datetime").timedelta(days=1)
        return value

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.isoformat(timespec="seconds")
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AmbulanceReturnRequest":
        created_at_raw = payload.get("created_at") or datetime.now().isoformat(timespec="seconds")
        return cls(
            task_id=str(payload.get("task_id") or new_task_id()),
            created_at=datetime.fromisoformat(created_at_raw),
            raw_text=str(payload.get("raw_text") or ""),
            vehicle=str(payload.get("vehicle") or ""),
            driver=str(payload.get("driver") or ""),
            mileage=str(payload.get("mileage") or ""),
            case_id=str(payload.get("case_id") or ""),
            personnel=parse_list(payload.get("personnel") or []),
            personnel_accounts=parse_account_list(payload.get("personnel_accounts") or payload.get("personnel_hidden_raw") or []),
            case_date=str(payload.get("case_date") or ""),
            case_time=str(payload.get("case_time") or ""),
            return_date=str(payload.get("return_date") or ""),
            return_time=str(payload.get("return_time") or ""),
            case_address=clean_case_address(str(payload.get("case_address") or "")),
            patient_summary=str(payload.get("patient_summary") or cls.__dataclass_fields__["patient_summary"].default),
            case_reason=str(payload.get("case_reason") or cls.__dataclass_fields__["case_reason"].default),
            disinfection=str(payload.get("disinfection") or cls.__dataclass_fields__["disinfection"].default),
            disinfection_items=parse_list(payload.get("disinfection_items") or DEFAULT_DISINFECTION_ITEMS),
            work_note=str(payload.get("work_note") or cls.__dataclass_fields__["work_note"].default),
            consumables={str(k): int(v) for k, v in dict(payload.get("consumables") or DEFAULT_CONSUMABLES).items()},
        )


def new_task_id() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:6]


def normalize_hhmm(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) >= 4:
        return digits[:4]
    return digits


def parse_case_date(value: str) -> datetime | None:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    formats = ["%Y%m%d", "%Y/%m/%d", "%Y-%m-%d"]
    if len(digits) == 7:
        try:
            year = int(digits[:3]) + 1911
            return datetime(year, int(digits[3:5]), int(digits[5:7]))
        except ValueError:
            return None
    if len(digits) == 8:
        raw = digits
    for fmt in formats:
        try:
            return datetime.strptime(raw[:10], fmt)
        except ValueError:
            continue
    return None


def example_command() -> str:
    return (
        "\u6551\u8b77\u56de\u7a0b\n"
        "\u8eca\u8f1b:91A1\n"
        "\u53f8\u6a5f:\u738b\u5c0f\u660e\n"
        "\u91cc\u7a0b:12345\n"
        "\u6848\u4ef6\u6642\u9593:1420\n"
        "\u56de\u7a0b\u6642\u9593:1505\n"
        "\u6848\u767c\u5730\u5740:\u6843\u5712\u5e02\u89c0\u97f3\u5340\n"
        "\u4e8b\u7531:\u6025\u75c5\n"
        "\u50b7\u75c5\u60a3:\u7537\u4e00\u540d\n"
        "\u8017\u6750:\u53e3\u7f69=2,\u624b\u5957=2,\u6c27\u6c23\u9762\u7f69=1\n"
        "\u6d88\u6bd2:\u8eca\u5167\u3001\u64d4\u67b6\u3001\u76e3\u8996\u5668\u63a5\u89f8\u9762\u5b8c\u6210\u6d88\u6bd2\n"
        "\u5de5\u4f5c\u7d00\u9304:\u6551\u8b77\u8fd4\u968a\u5b8c\u6210\u88dc\u767b"
    )


def request_from_form(form: dict[str, Any]) -> AmbulanceReturnRequest:
    consumables = parse_consumables(str(form.get("consumables") or ""))
    disinfection_items = parse_disinfection_items_from_form(form)
    return AmbulanceReturnRequest(
        task_id=new_task_id(),
        created_at=datetime.now(),
        raw_text="",
        vehicle=str(form.get("vehicle") or "").strip(),
        driver=str(form.get("driver") or "").strip(),
        mileage=str(form.get("mileage") or "").strip(),
        case_id=str(form.get("case_id") or "").strip(),
        personnel=parse_list(form.get("personnel") or ""),
        personnel_accounts=parse_account_list(form.get("personnel_accounts") or ""),
        case_date=str(form.get("case_date") or "").strip(),
        case_time=str(form.get("case_time") or "").strip(),
        return_date=str(form.get("return_date") or "").strip(),
        return_time=str(form.get("return_time") or "").strip(),
        case_address=clean_case_address(str(form.get("case_address") or "")),
        patient_summary=str(form.get("patient_summary") or "").strip(),
        case_reason=str(form.get("case_reason") or "").strip() or "\u6025\u75c5",
        disinfection=str(form.get("disinfection") or "").strip()
        or "\u6551\u8b77\u8fd4\u968a\u5f8c\u8eca\u5167\u3001\u64d4\u67b6\u53ca\u63a5\u89f8\u9762\u5b8c\u6210\u6d88\u6bd2\u3002",
        disinfection_items=disinfection_items,
        work_note=str(form.get("work_note") or "").strip()
        or "\u6551\u8b77\u6848\u4ef6\u8fd4\u968a\u5f8c\u5b8c\u6210\u8eca\u8f1b\u3001\u8017\u6750\u53ca\u6d88\u6bd2\u767b\u6253\u3002",
        consumables=consumables,
    )


def parse_request(text: str) -> AmbulanceReturnRequest:
    request = AmbulanceReturnRequest(task_id=new_task_id(), created_at=datetime.now(), raw_text=text)
    for line in _meaningful_lines(text):
        key, value = _split_key_value(line)
        if not key:
            continue
        normalized = key.replace(" ", "")
        if normalized in {"\u8eca\u8f1b", "\u51fa\u52e4\u8eca\u8f1b", "\u8eca\u865f"}:
            request.vehicle = value
        elif normalized in {"\u53f8\u6a5f", "\u99d5\u99db"}:
            request.driver = value
        elif normalized in {"\u91cc\u7a0b", "\u516c\u91cc\u6578"}:
            request.mileage = value
        elif normalized in {"\u6848\u4ef6\u6642\u9593", "\u51fa\u52e4\u6642\u9593"}:
            request.case_time = value
        elif normalized in {"\u56de\u7a0b\u6642\u9593", "\u8fd4\u968a\u6642\u9593"}:
            request.return_time = value
        elif normalized in {"\u6848\u767c\u5730\u5740", "\u5730\u5740", "\u5730\u9ede"}:
            request.case_address = clean_case_address(value)
        elif normalized in {"\u4e8b\u7531", "\u6848\u4ef6\u985e\u5225", "\u6551\u8b77\u4e8b\u7531"}:
            request.case_reason = value
        elif normalized in {"\u50b7\u75c5\u60a3", "\u6027\u5225\u4eba\u6578", "\u7537\u5973"}:
            request.patient_summary = value
        elif normalized == "\u8017\u6750":
            parsed = parse_consumables(value)
            if parsed:
                request.consumables = parsed
        elif normalized in {"\u6d88\u6bd2", "\u6d88\u6bd2\u7d00\u9304"}:
            request.disinfection = value
        elif normalized in {"\u6d88\u6bd2\u9805\u76ee", "\u6d88\u6bd2\u57f7\u884c\u9805\u76ee"}:
            request.disinfection_items = parse_list(value)
        elif normalized in {"\u5de5\u4f5c\u7d00\u9304", "\u52e4\u52d9\u7d00\u9304", "\u7d00\u9304"}:
            request.work_note = value
    return request


def parse_consumables(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in value.replace("\uff0c", ",").replace("\u3001", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, qty = item.split("=", 1)
        elif "*" in item:
            name, qty = item.split("*", 1)
        elif "x" in item.lower():
            name, qty = item.lower().split("x", 1)
        else:
            name, qty = item, "1"
        try:
            quantity = int(qty.strip())
        except ValueError:
            quantity = 1
        name = name.strip()
        if name and quantity > 0:
            result[name] = quantity
    return result


def parse_disinfection_items_from_form(form: dict[str, Any]) -> list[str]:
    selected = _form_values(form, "disinfection_items")
    custom = str(form.get("disinfection_items_custom") or "")
    items = selected + parse_list(custom)
    return items


def parse_list(value: object) -> list[str]:
    if isinstance(value, list):
        parts = [str(item or "") for item in value]
    else:
        parts = re.split(r"[,，、\n]+", str(value or ""))
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        item = part.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def parse_account_list(value: object) -> list[str]:
    if isinstance(value, list):
        parts = [str(item or "") for item in value]
    else:
        parts = re.split(r"[,，、;\s]+", str(value or ""))
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        account = part.strip()
        if not account:
            continue
        normalized = account if account.lower().startswith("tyfd") else account.upper()
        key = normalized.lower()
        if key not in seen:
            result.append(normalized)
            seen.add(key)
    return result


def _form_values(form: dict[str, Any], name: str) -> list[str]:
    getlist = getattr(form, "getlist", None)
    if callable(getlist):
        return [str(value or "").strip() for value in getlist(name) if str(value or "").strip()]
    value = form.get(name)
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if value:
        return [str(value).strip()]
    return []


def clean_case_address(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\uff0d", "-").replace("\u2010", "-").replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    for marker in (
        "-\u6025\u75c5\u653e\u68c4\u6025\u6551",
        "-\u6848\u4ef6\u91cd\u8907",
        "-\u4f86\u96fb\u53d6\u6d88",
        "-\u8eca\u798d\u62d2\u9001",
        "-\u62d2\u9001",
        "-\u672a\u9001\u91ab",
        "-\u81ea\u884c\u5c31\u91ab",
        "\u6848\u4ef6\u91cd\u8907",
        "\u4f86\u96fb\u53d6\u6d88",
        "\u91cd\u8907\u5831\u6848",
        "\u53d6\u6d88",
    ):
        if marker in text:
            text = text.split(marker, 1)[0]
    text = re.sub(r"\s+", "", text)
    return text.strip("- \t\r\n")


def _meaningful_lines(text: str) -> Iterable[str]:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == COMMAND_PREFIX:
            continue
        yield line


def _split_key_value(line: str) -> tuple[str, str]:
    for delimiter in (":", "\uff1a"):
        if delimiter in line:
            key, value = line.split(delimiter, 1)
            return key.strip(), value.strip()
    return "", ""
