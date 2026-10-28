from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


class CivilpowerRecordConflict(RuntimeError):
    """The portal records do not identify a single safe action for this case."""

    def __init__(self, message: str, *, code: str = "conflict"):
        super().__init__(message)
        self.code = code


def taipei_datetime(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    return value


def taipei_minute(value: datetime) -> datetime:
    return taipei_datetime(value).replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class CivilpowerCaseWindow:
    case_id: str
    dispatch_at: datetime
    return_at: datetime
    source_record_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "dispatch_at", taipei_datetime(self.dispatch_at))
        object.__setattr__(self, "return_at", taipei_datetime(self.return_at))
        if self.return_at < self.dispatch_at:
            raise CivilpowerRecordConflict("正式案件返隊時間早於派遣時間，尚未新增民力資料。")

    @property
    def start(self) -> datetime:
        return taipei_minute(self.dispatch_at)

    @property
    def end(self) -> datetime:
        return taipei_minute(self.return_at)


@dataclass(frozen=True, slots=True)
class CivilpowerIoRecord:
    record_id: str
    at: datetime
    serve_unit: str
    home_unit: str
    name: str
    title: str
    status: str
    reason: str = ""
    operator: str = ""
    signed: bool = False

    def __post_init__(self):
        object.__setattr__(self, "at", taipei_datetime(self.at))


@dataclass(frozen=True, slots=True)
class CivilpowerIoDecision:
    action: str
    out: CivilpowerIoRecord | None = None
    inbound: CivilpowerIoRecord | None = None


@dataclass(frozen=True, slots=True)
class CivilpowerWorkRecord:
    record_id: str
    at: datetime
    serve_unit: str
    volunteers: tuple[str, ...]
    summary: str
    handling: str
    official_return_at: datetime | None
    io_out_at: datetime | None
    io_in_at: datetime | None
    case_id: str = ""
    operator: str = ""
    kind: str = ""

    def __post_init__(self):
        for field in ("at", "official_return_at", "io_out_at", "io_in_at"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, taipei_datetime(value))

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def volunteer_in_status(status_text: str, name: str) -> bool:
    if not name:
        return False
    confirmations = []
    for match in re.finditer(r"(?<![A-Za-z0-9\u4e00-\u9fff])救護義消協勤\s*[:：]\s*([^\r\n]*)", status_text):
        segment = re.split(r"\s+\d+[.、]\s*", match.group(1), maxsplit=1)[0]
        if name in re.split(r"[,，、;；\s]+", segment):
            confirmations.append(not re.search(r"未(?:參與|出勤|協勤|到)|取消", segment))
    return bool(confirmations) and all(confirmations)


def find_civilpower_work_record(
    records: Iterable[CivilpowerWorkRecord],
    window: CivilpowerCaseWindow,
    *,
    name: str,
    serve_unit: str,
    address: str,
    vehicle: str = "",
) -> CivilpowerWorkRecord | None:
    matches = []
    for row in records:
        if row.serve_unit != serve_unit or name not in row.volunteers or taipei_minute(row.at) != window.start:
            continue
        if row.case_id and row.case_id != window.case_id:
            continue
        if row.kind not in {"緊急救護", "救護"}:
            raise CivilpowerRecordConflict("既有工作紀錄的服勤項目不是緊急救護，尚未重複新增。")
        if not row.case_id:
            if not address:
                raise CivilpowerRecordConflict("工作紀錄未提供案件編號且缺少可核對地址，尚未重複新增。")
            if re.sub(r"\s+", "", address) not in re.sub(r"\s+", "", row.summary):
                continue
        if vehicle and not re.search(r"(?:^|[.\s、,，;；])" + re.escape(vehicle) + r"\s*[:：]", row.handling):
            continue
        if row.official_return_at is None or taipei_datetime(row.official_return_at) != taipei_datetime(window.return_at):
            raise CivilpowerRecordConflict("既有工作紀錄的返隊時間與正式案件不同，尚未新增或覆蓋紀錄。")
        if row.io_out_at is None or row.io_in_at is None:
            raise CivilpowerRecordConflict("既有工作紀錄缺少可核對的出入時間，尚未重複新增。")
        if not window.start <= taipei_minute(row.io_out_at) <= taipei_minute(row.io_in_at) <= window.end:
            raise CivilpowerRecordConflict("既有工作紀錄的出入時間超出正式案件時段，尚未新增或修改。")
        matches.append(row)
    if len(matches) > 1:
        raise CivilpowerRecordConflict("本案找到多筆工作紀錄，無法唯一確認，尚未重複新增。")
    return matches[0] if matches else None


def io_pair_for_work_record(
    records: Iterable[CivilpowerIoRecord],
    window: CivilpowerCaseWindow,
    work: CivilpowerWorkRecord,
    *, name: str, home_unit: str, serve_unit: str, title: str = "",
) -> CivilpowerIoDecision:
    if work.io_out_at is None or work.io_in_at is None:
        raise CivilpowerRecordConflict("既有工作紀錄缺少出入時間，尚未確認完成。")
    start, end = taipei_minute(work.io_out_at), taipei_minute(work.io_in_at)
    if not window.start <= start <= end <= window.end:
        raise CivilpowerRecordConflict("工作紀錄的出入時間不在正式案件時段內。")
    relevant = [row for row in records if row.name == name and row.home_unit == home_unit and row.serve_unit == serve_unit]
    outs = [row for row in relevant if row.status == "出" and taipei_minute(row.at) == start]
    ins = [row for row in relevant if row.status == "入" and taipei_minute(row.at) == end]
    if len(outs) != 1 or len(ins) != 1 or not outs[0].record_id or not ins[0].record_id:
        raise CivilpowerRecordConflict("工作紀錄對應的出入登記缺少或重複，尚未新增或修改資料。")
    pair_ids = {outs[0].record_id, ins[0].record_id}
    if len(pair_ids) != 2 or any(title and row.title != title for row in (outs[0], ins[0])):
        raise CivilpowerRecordConflict("工作紀錄對應的出入人員識別不一致，尚未確認完成。")
    def interferes(row):
        at = taipei_minute(row.at)
        if row.record_id in pair_ids or not start <= at <= end:
            return False
        if at == start and row.status in {"服勤", "入"}:
            return False
        if at == end and row.status in {"退勤", "出"}:
            return False
        return True

    if any(interferes(row) for row in relevant):
        raise CivilpowerRecordConflict("工作紀錄對應的出入之間存在其他狀態，需確認配對後再處理。")
    return CivilpowerIoDecision("reuse_pair", outs[0], ins[0])


def plan_civilpower_io(
    records: Iterable[CivilpowerIoRecord],
    window: CivilpowerCaseWindow,
    *,
    name: str,
    home_unit: str,
    serve_unit: str,
    title: str = "",
) -> CivilpowerIoDecision:
    relevant = [
        row for row in records
        if row.name == name and row.home_unit == home_unit and row.serve_unit == serve_unit
    ]
    if any(title and row.title != title and window.start <= taipei_minute(row.at) <= window.end for row in relevant):
        raise CivilpowerRecordConflict("既有人員職稱與本次名冊不同，請確認人員身分；尚未新增民力資料。")
    if any(not row.record_id for row in relevant) or len({row.record_id for row in relevant}) != len(relevant):
        raise CivilpowerRecordConflict("出入登記識別缺漏或重複，無法安全判定；尚未新增民力資料。")
    relevant.sort(key=lambda row: (taipei_minute(row.at), row.record_id))
    inside = [row for row in relevant if window.start <= taipei_minute(row.at) <= window.end]
    outs = [row for row in inside if row.status == "出"]
    if len(outs) > 1:
        raise CivilpowerRecordConflict("本案正式時段找到多筆出勤，可能包含人工與重複登記；尚未新增或修改民力資料。")
    if outs:
        out = outs[0]
        previous = [row for row in relevant if taipei_minute(row.at) < taipei_minute(out.at)]
        peers = [row for row in relevant if row is not out and taipei_minute(row.at) == taipei_minute(out.at)]
        arrived_now = any(row.status == "服勤" for row in peers)
        if previous and previous[-1].status == "出":
            raise CivilpowerRecordConflict("既有出勤跨越本案實際出勤時間，無法確認配對；尚未新增民力資料。")
        if previous and previous[-1].status == "退勤" and not arrived_now:
            raise CivilpowerRecordConflict("本案出勤之前已退勤且沒有新到勤，尚未新增或修改資料。")
        if previous and previous[-1].status == "入" and taipei_minute(previous[-1].at) >= window.start:
            before_in = [row for row in previous if taipei_minute(row.at) < taipei_minute(previous[-1].at)]
            if not before_in or before_in[-1].status != "出":
                raise CivilpowerRecordConflict("本案出勤前有無法對應的入勤，需確認前一趟紀錄；尚未新增資料。", code="missing_context")
        if sum(row.status == "入" and taipei_minute(row.at) >= taipei_minute(out.at) for row in inside) > 1:
            raise CivilpowerRecordConflict("本案正式時段找到多筆入勤，無法唯一對應；尚未新增或修改民力資料。")
        following = [row for row in relevant if taipei_minute(row.at) > taipei_minute(out.at)]
        if any(row.status != "服勤" for row in peers):
            raise CivilpowerRecordConflict("出勤同一分鐘有其他出入狀態，需確認實際配對；尚未新增民力資料。")
        if following:
            next_row = following[0]
            if next_row.status == "入":
                if taipei_minute(next_row.at) > window.end:
                    raise CivilpowerRecordConflict("既有返隊時間超出正式案件時段，請確認原紀錄；未覆蓋人工時間。")
                return CivilpowerIoDecision("reuse_pair", out, next_row)
            if taipei_minute(next_row.at) <= window.end:
                raise CivilpowerRecordConflict("出勤之後有其他服勤或退勤狀態，無法安全補登返隊。")
        return CivilpowerIoDecision("create_in", out)
    prior = [row for row in relevant if taipei_minute(row.at) < window.start]
    if prior and prior[-1].status == "出":
        raise CivilpowerRecordConflict("既有出勤跨越本案派遣時間，無法確認案件對應；尚未新增民力資料。")
    if inside:
        raise CivilpowerRecordConflict("本案時段已有入勤、服勤或退勤但缺少可對應的出勤，尚未重複新增。")
    if not prior or prior[-1].status not in {"服勤", "入"}:
        raise CivilpowerRecordConflict("無法確認本案出勤前的在勤狀態，請先確認到勤及退勤紀錄。", code="missing_context")
    return CivilpowerIoDecision("create_pair")
