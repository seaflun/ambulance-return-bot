import base64
import copy
import json
import struct
import tempfile
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

import ambulance_bot.sinposmart_backend as backend

from ambulance_bot.sinposmart_backend import (
    SinpoSmartBackendStore,
    build_sinposmart_admin_view,
    compact_sinposmart_events,
    normalize_sinposmart_event,
    sanitize_login_diagnostic,
    sinposmart_fire_day_for,
    sinposmart_status_label,
)


class NoticeCollectionBackendTests(unittest.TestCase):
    def test_manual_notice_is_shown_on_its_publication_day_without_claiming_automatic_entry(self):
        manual = {"key": "e" * 64, "subject": "已由人工收取的通報", "published_at": "2026-10-22T22:30:00+08:00",
                  "published_date": "2026-10-22", "observed_at": "2026-10-23T08:01:00+08:00", "read_state": "confirmed"}
        event = self.event(reported="2026-10-23T08:01:00+08:00", manual_records=[manual],
            scan_scope="publication_window", scan_from="2026-10-21", scan_to="2026-10-23",
            scan_verified_at="2026-10-23T08:01:00+08:00", new_keys=[], new_count=0)
        self.put(event)
        view = self.view("2026-10-22", now=datetime(2026, 10, 23, 8, 2, tzinfo=backend.TAIPEI_TIMEZONE))
        row = view["records"][0]
        self.assertEqual(row["write_label"], "手動收取")
        self.assertFalse(row["write_confirmed"])
        self.assertEqual(row["published_at"], manual["published_at"])
        self.assertEqual(row["received_at"], "")
        self.assertEqual(view["manual_count"], 1)
        self.assertEqual(view["today_count"], None)
        self.assertEqual(view["records"][0]["subject"], manual["subject"])

    def test_publication_coverage_is_required_for_a_full_list_success_label(self):
        self.put(self.event(records=[], coverage=False))
        old = self.view()
        self.assertIn("未讀列表", old["status_label"])
        self.assertNotEqual(old["status_label"], "收取正常")
        self.put(self.event(run=2, reported="2026-10-22T09:02:00+08:00", records=[], manual_records=[],
            scan_scope="publication_window", scan_from="2026-10-20", scan_to="2026-10-22",
            scan_verified_at="2026-10-22T09:02:00+08:00"))
        view = self.view()
        self.assertEqual(view["status_label"], "列表檢查完成（無新增入表）")
        self.assertEqual(view["authentication_label"], "本輪列表身分已驗證")

    def test_invalid_manual_evidence_and_incomplete_manual_parts_never_claim_success(self):
        manual = {"key": "e" * 64, "subject": "人工通報", "published_at": "2026-10-22T08:00:00+08:00",
                  "published_date": "2026-10-22", "observed_at": "2026-10-22T09:00:00+08:00", "read_state": "confirmed"}
        for changes in ({"published_date": "2026-10-21"}, {"read_state": "unknown"}, {"key": "unsafe"},
                        {"published_at": "2026-10-23T08:00:00+08:00"}, {"observed_at": "2026-10-23T08:00:00+08:00"}):
            with self.subTest(changes=changes):
                report = self.event(manual_records=[{**manual, **changes}])["snapshot"]["notice_collection"]
                self.assertEqual(backend.normalize_notice_collection_report(report), {})
        self.put(self.event(manual_records=[manual], part_count=2))
        view = self.view()
        self.assertEqual(view["records"], [])
        self.assertFalse(view["parts_complete"])
        self.assertNotEqual(view["status_class"], "complete")
        self.assertNotEqual(view["authentication_label"], "本輪列表身分已驗證")

    def test_automatic_receipt_takes_precedence_over_an_earlier_manual_observation(self):
        record = self.record()
        manual = {"key": record["key"], "subject": record["subject"], "published_at": "2026-10-22T08:00:00+08:00",
                  "published_date": "2026-10-22", "observed_at": "2026-10-22T09:00:00+08:00", "read_state": "confirmed"}
        self.put(self.event(manual_records=[manual], new_count=0, new_keys=[]))
        self.put(self.event(run=2, reported="2026-10-22T09:02:00+08:00", records=[record]))
        view = self.view()
        self.assertEqual(len(view["records"]), 1)
        self.assertEqual(view["manual_count"], 0)
        self.assertTrue(view["records"][0]["write_confirmed"])
        self.assertEqual(view["records"][0]["published_at"], manual["published_at"])

    def test_notice_discovered_two_days_later_is_still_visible_on_publication_day(self):
        manual = {"key": "e" * 64, "subject": "跨兩日補查", "published_at": "2026-10-22T22:30:00+08:00",
                  "published_date": "2026-10-22", "observed_at": "2026-10-24T09:00:00+08:00", "read_state": "confirmed"}
        self.put(self.event(reported="2026-10-24T09:00:00+08:00", manual_records=[manual]))
        view = self.view("2026-10-22", now=datetime(2026, 10, 24, 9, 1, tzinfo=backend.TAIPEI_TIMEZONE))
        self.assertEqual(view["manual_count"], 1)
        self.assertEqual(view["records"][0]["published_at"], manual["published_at"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SinpoSmartBackendStore(Path(self.temporary.name))

    @staticmethod
    def record(number=1, received="2026-10-22T09:00:00+08:00", read_state="confirmed"):
        return {"key": f"{number:064x}", "subject": f"通報 {number}", "received_at": received, "read_state": read_state}

    def event(self, *, run=1, station="PUBLIC-PC", reported="2026-10-22T09:01:00+08:00", records=None, **changes):
        records = [] if records is None else records
        run_id = f"00000000-0000-4000-8000-{run:012d}"
        current = datetime.fromisoformat(reported)
        state = changes.pop("state", "success")
        coverage = changes.pop("coverage", True)
        report = {
            "schema_version": 1, "run_id": run_id, "part_index": 0, "part_count": 1, "state": state,
            "started_at": reported, "completed_at": reported if state not in {"running", "waiting"} else "",
            "reported_at": reported, "next_check_at": (current + timedelta(minutes=3 if 8 <= current.hour < 22 else 30)).isoformat(),
            "calendar_day": current.date().isoformat(), "shift_start": "", "shift_end": "",
            "unread_count": len(records), "new_count": len(records), "marked_read_count": len(records),
            "pending_write_count": 0, "pending_read_count": 0, "error_stage": "", "message": "",
            "records": records, "new_keys": [row["key"] for row in records],
        }
        report.update(changes)
        if coverage and state == "success":
            report.setdefault("manual_records", [])
            report.setdefault("scan_scope", "publication_window")
            report.setdefault("scan_from", (current.date() - timedelta(days=2)).isoformat())
            report.setdefault("scan_to", current.date().isoformat())
            report.setdefault("scan_verified_at", reported)
        return {"event_id": f"notice-{run}-{state}-{report['part_index']}", "occurred_at": reported,
                "record_type": "notice_collection", "trigger_type": "notice_poll", "status": state,
                "snapshot": {"app_version": "2026.10.22.1200", "workstation": station, "run_id": run_id,
                             "notice_collection": report}}

    def put(self, event):
        return self.store.upsert_event(event, now=datetime.fromisoformat(event["occurred_at"]))

    def view(self, day="2026-10-22", **kwargs):
        kwargs.setdefault("now", datetime(2026, 10, 22, 9, 2, tzinfo=backend.TAIPEI_TIMEZONE))
        return self.store.notice_view(day, **kwargs)

    def verification(self, record, result="read_confirmed", **changes):
        entry = {"workstation": "PUBLIC-PC", "key": record["key"], "received_at": record["received_at"],
                 "verified_at": "2026-10-22T09:01:30+08:00", "result": result, "subject": record["subject"]}
        entry.update(changes)
        return entry

    def save_verifications(self, entries):
        path = Path(self.temporary.name) / "notice_verifications.json"
        path.write_text(json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8")
        return path

    def test_operator_verification_separates_33_read_from_one_missing_original_without_changing_events(self):
        records = [self.record(index, read_state="unknown") for index in range(1, 35)]
        raw = self.event(records=[dict(record, subject="") for record in records], new_keys=[], new_count=0,
                         unread_count=0, marked_read_count=0)
        self.put(raw)
        event_path = self.store.path_for_day("2026-10-22")
        before = event_path.read_bytes()
        entries = [self.verification(record) for record in records[:-1]]
        entries.append(self.verification(records[-1], "original_not_found", related_key=records[-2]["key"]))
        self.save_verifications(entries)
        view = self.view()
        self.assertEqual((view["today_count"], view["verified_read_count"], view["original_not_found_count"]), (34, 33, 1))
        self.assertEqual((view["state"], view["status_class"], view["status_label"]), ("reviewed", "", "已核對，1則原件未找到"))
        by_key = {row["key"]: row for row in view["records"]}
        self.assertEqual(by_key[records[0]["key"]]["read_label"], "已人工核實已讀")
        missing = by_key[records[-1]["key"]]
        self.assertEqual(missing["read_state"], "unknown")
        self.assertEqual(missing["read_label"], "原件未找到（疑似撤回／重發）")
        self.assertEqual(missing["subject"], records[-1]["subject"])
        self.assertEqual(missing["related_key"], records[-2]["key"])
        self.assertEqual(view["verification_checked_at"], entries[-1]["verified_at"])
        self.assertEqual(view["last_checked_at"], raw["occurred_at"])
        self.assertEqual(view["history"], [])
        self.assertEqual(event_path.read_bytes(), before)

    def test_operator_verification_invalid_or_mismatched_evidence_stays_unknown(self):
        record = self.record(read_state="unknown")
        self.put(self.event(records=[record], new_keys=[], new_count=0))
        invalid = ({"workstation": "OTHER-PC"}, {"key": "b" * 64}, {"received_at": "2026-10-22T09:00:01+08:00"},
                   {"verified_at": "2026-10-22T09:03:00+08:00"}, {"verified_at": "2026-10-22T08:59:00+08:00"},
                   {"verified_at": "2026-10-22T09:01:00"}, {"result": []}, {"subject": []},
                   {"related_key": {}}, {"note": "password=unsafe https://example.invalid"},
                   {"key": True}, {"received_at": None})
        for changes in invalid:
            with self.subTest(changes=changes):
                self.save_verifications([self.verification(record, **changes)])
                view = self.view()
                self.assertEqual(view["records"][0]["read_state"], "unknown")
                self.assertEqual(view["verified_read_count"], 0)
                self.assertEqual(view["verification_checked_at"], "")
                self.assertNotEqual(view["status_class"], "complete")

    def test_operator_verification_is_stable_across_polls_and_machine_confirmation_clears_missing(self):
        record = self.record(read_state="unknown")
        event = self.event(records=[record], new_keys=[], new_count=0)
        self.put(event)
        self.save_verifications([self.verification(record, "original_not_found")])
        self.assertEqual(self.view()["original_not_found_count"], 1)
        self.put(event)
        self.put(self.event(run=2, reported="2026-10-22T09:02:00+08:00", records=[record], new_keys=[], new_count=0))
        self.assertEqual((self.view()["today_count"], self.view()["original_not_found_count"]), (1, 1))
        self.put(self.event(run=3, reported="2026-10-22T09:02:01+08:00", records=[dict(record, read_state="confirmed")],
                            new_keys=[], new_count=0))
        view = self.view(now=datetime(2026, 10, 22, 9, 3, tzinfo=backend.TAIPEI_TIMEZONE))
        self.assertEqual(view["original_not_found_count"], 0)
        self.assertEqual(view["records"][0]["read_label"], "已確認已讀")
        self.assertEqual(view["verification_checked_at"], "")
        self.assertEqual(view["status_class"], "complete")

    def test_operator_verification_does_not_mask_machine_failure_busy_partial_or_stale(self):
        record = self.record(read_state="unknown")
        self.put(self.event(records=[record], new_keys=[], new_count=0))
        self.save_verifications([self.verification(record, "original_not_found")])
        for index, state in enumerate(("waiting", "failed", "partial"), 2):
            with self.subTest(state=state):
                self.put(self.event(run=index, reported=f"2026-10-22T09:0{index}:00+08:00", state=state,
                                    records=[record], new_keys=[], new_count=0,
                                    error_stage="busy" if state == "waiting" else "login" if state == "failed" else "mark_read"))
                view = self.view(now=datetime(2026, 10, 22, 9, 5, tzinfo=backend.TAIPEI_TIMEZONE))
                self.assertEqual(view["state"], state)
                self.assertEqual(view["original_not_found_count"], 1)
        self.put(self.event(run=5, reported="2026-10-22T09:05:00+08:00", records=[record], new_keys=[], new_count=0))
        stale = self.view(now=datetime(2026, 10, 22, 9, 11, tzinfo=backend.TAIPEI_TIMEZONE))
        self.assertEqual(stale["status_class"], "attention")
        self.assertIn("超過", stale["status_label"])
        self.assertNotEqual(stale["state"], "reviewed")

    def test_operator_verification_cannot_cover_new_unknown_or_pending_machine_work(self):
        record, other = self.record(read_state="unknown"), self.record(2, read_state="unknown")
        self.put(self.event(records=[record, other], new_keys=[], new_count=0))
        self.save_verifications([self.verification(record, "original_not_found")])
        self.assertEqual(self.view()["state"], "uncertain")
        self.save_verifications([self.verification(record), self.verification(other)])
        self.assertEqual(self.view()["status_class"], "complete")
        self.put(self.event(run=2, reported="2026-10-22T09:02:00+08:00", records=[record, other],
                            new_keys=[], new_count=0, pending_write_count=1))
        self.assertEqual(self.view()["state"], "uncertain")

    def test_operator_verification_is_visible_range_only_and_not_a_fire_day_file(self):
        records = [self.record(), self.record(2, "2026-10-21T23:00:00+08:00", "unknown")]
        self.put(self.event(records=records, new_keys=[], new_count=0))
        path = self.save_verifications([self.verification(records[1], "original_not_found")])
        before = path.read_bytes()
        self.assertEqual(self.view()["original_not_found_count"], 0)
        days = self.store.list_days(now=datetime(2026, 10, 22, 9, 2, tzinfo=backend.TAIPEI_TIMEZONE))
        self.assertEqual([day["fire_day"] for day in days], ["2026-10-22"])
        self.store.cleanup(datetime(2026, 11, 22, 9, 2, tzinfo=backend.TAIPEI_TIMEZONE))
        self.assertEqual(path.read_bytes(), before)

    def test_operator_verification_malformed_document_and_cap_fail_closed(self):
        record = self.record(read_state="unknown")
        self.put(self.event(records=[record], new_keys=[], new_count=0))
        path = self.save_verifications([])
        for content in ("{bad", "[]", '{"schema_version":true,"entries":[]}',
                        json.dumps({"schema_version": 1, "entries": [self.verification(record)] * 201})):
            with self.subTest(content=content[:40]):
                path.write_text(content, encoding="utf-8")
                view = self.view()
                self.assertEqual(view["records"][0]["read_state"], "unknown")
                self.assertEqual(view["verification_checked_at"], "")

    def test_notice_typed_whitelist_preserves_metadata_without_generic_depth_loss(self):
        raw = self.event(records=[self.record()])
        raw["snapshot"]["notice_collection"].update(secret="do-not-store", message="password=do-not-store https://unsafe.invalid")
        raw["snapshot"]["notice_collection"]["records"][0].update(subject="<script>alert(1)</script> 通報", cookie="do-not-store")
        raw["snapshot"]["password"] = "do-not-store"
        stored = self.put(raw)
        report = stored["snapshot"]["notice_collection"]
        self.assertIs(type(report["new_count"]), int)
        self.assertEqual(report["records"][0]["key"], self.record()["key"])
        self.assertNotIn("<script>", report["records"][0]["subject"])
        self.assertNotIn("do-not-store", json.dumps(stored))
        self.assertEqual(self.view()["today_count"], 1)

    def test_readonly_notice_store_does_not_create_storage_directory(self):
        absent = Path(self.temporary.name) / "artifacts" / "sinposmart"
        store = SinpoSmartBackendStore(absent, create=False)
        view = store.notice_view("2026-10-22", now=datetime(2026, 10, 22, 9, 2, tzinfo=backend.TAIPEI_TIMEZONE))
        self.assertFalse(absent.parent.exists())
        self.assertFalse(view["count_known"])
        self.assertIsNone(view["today_count"])
        SinpoSmartBackendStore(absent)
        self.assertTrue(absent.is_dir())

    def test_notice_failure_codes_store_only_fixed_safe_messages_and_reach_card_view(self):
        messages = {
            "browser_start_failed": "通報專用瀏覽器無法啟動，請確認 Chrome 狀態。",
            "browser_window_closed": "通報專用視窗已關閉，下輪將重新開啟。",
            "snapshot_failed": "通報頁未正常回應，請確認通報專用瀏覽器。",
            "snapshot_invalid": "通報頁資料格式無法確認。",
            "origin_mismatch": "通報頁來源不符，已停止操作。",
            "account_mismatch": "通報頁登入身分不符，僅允許新坡分隊公務信箱。",
            "login_target_mismatch": "登入頁來源或路徑不符，未填入任何帳號密碼。",
            "login_page_failed": "通報登入頁重新整理失敗，請確認連線。",
            "list_navigation_failed": "通報列表重新整理失敗，請確認連線。",
            "list_wait_timeout": "未讀列表尚未完成載入或資料不完整，已停止本輪收取。",
            "page_limit": "未讀通報頁數過多，請先人工確認收取範圍。",
            "list_changed": "未讀列表在查詢期間變動，將於下輪重新確認。",
            "duplicate_notice_id": "通報分頁出現重複識別碼，已停止本輪收取。",
            "next_page_ambiguous": "無法唯一確認通報下一頁按鈕。",
            "count_mismatch": "未讀通報數量不符，已停止本輪收取。",
            "notice_url_invalid": "通報內文網址無法確認，已停止讀取。",
            "scan_unexpected": "通報列表載入未完成，已保留原有未讀狀態。",
            "session_expired": "入口網作業逾時，將於下輪重新登入。",
            "login_failed": "通報背景登入未成功，請確認已儲存的密碼與網站狀態。",
            "credentials_missing": "通報帳號尚未設定密碼，請先至值班台設定。",
            "cancelled": "通報收取已停止。",
        }
        for index, (code, message) in enumerate(messages.items(), 1):
            for state in ("failed", "partial", "uncertain"):
                with self.subTest(code=code, state=state):
                    station = f"PUBLIC-{index}-{state}"
                    raw = self.event(run=index, station=station, state=state, error_stage="scan",
                                     error_code=code, unread_count=None, message="password=must-not-retain")
                    stored = self.put(raw)
                    report = stored["snapshot"]["notice_collection"]
                    self.assertEqual(report["error_code"], code)
                    self.assertEqual(report["message"], message)
                    self.assertNotIn("must-not-retain", json.dumps(stored))
                    self.assertEqual(backend.normalize_sinposmart_event(stored)["snapshot"], stored["snapshot"])
                    view = self.view(workstation=station)
                    self.assertTrue(view["parts_complete"])
                    self.assertEqual(view["error"], message)
                    self.assertEqual(view["history"][0]["error"], message)

    def test_notice_missing_unknown_or_secret_error_codes_keep_legacy_shape_and_summary(self):
        legacy = self.event(state="failed", error_stage="scan", unread_count=None)
        expected = backend.normalize_sinposmart_event(legacy)["snapshot"]["notice_collection"]
        self.assertNotIn("error_code", expected)
        self.assertEqual(expected["message"], "未能完成未讀通報檢查。")
        for code in (None, "", "unknown", "browser_start_failed password=must-not-retain",
                     "browser_start_failed\n", " browser_start_failed", "BROWSER_START_FAILED",
                     "https://unsafe.invalid/must-not-retain", ["browser_start_failed"],
                     {"token": "must-not-retain"}, True, 21):
            with self.subTest(code=code):
                raw = copy.deepcopy(legacy)
                raw["snapshot"]["notice_collection"].update(error_code=code, message="token=must-not-retain")
                stored = backend.normalize_sinposmart_event(raw)
                self.assertEqual(stored["snapshot"]["notice_collection"], expected)
                self.assertNotIn("must-not-retain", json.dumps(stored))

    def test_notice_nonfailure_states_ignore_even_valid_error_code_without_shape_change(self):
        for state in ("success", "running", "waiting", "missing_credentials"):
            with self.subTest(state=state):
                raw = self.event(state=state)
                expected = backend.normalize_sinposmart_event(raw)
                raw["snapshot"]["notice_collection"]["error_code"] = "browser_start_failed"
                self.assertEqual(backend.normalize_sinposmart_event(raw), expected)

    def test_notice_parts_require_matching_safe_failure_code_but_unknown_code_is_legacy(self):
        for index, (first_code, second_code, complete) in enumerate((
                ("browser_start_failed", "browser_start_failed", True),
                ("browser_start_failed", "snapshot_failed", False),
                ("browser_start_failed", None, False),
                ("password=must-not-retain", None, True)), 1):
            with self.subTest(first=first_code, second=second_code):
                station = f"PUBLIC-PARTS-{index}"
                for part, code in enumerate((first_code, second_code)):
                    event = self.event(run=index, station=station, state="failed", part_count=2,
                                       part_index=part, error_stage="scan", unread_count=None)
                    if code is not None:
                        event["snapshot"]["notice_collection"]["error_code"] = code
                    self.put(event)
                view = self.view(workstation=station)
                self.assertEqual(view["parts_complete"], complete)
                expected = ("通報專用瀏覽器無法啟動，請確認 Chrome 狀態。" if index == 1 else
                            "未能完成未讀通報檢查。" if index == 4 else "無法確認完整通報數量。")
                self.assertEqual(view["error"], expected)
                self.assertNotIn("must-not-retain", json.dumps(self.store.read_day("2026-10-22")))

    def test_invalid_notice_metadata_never_becomes_normal_zero(self):
        for changes in ({"new_count": True}, {"new_count": -1}, {"state": "ok"},
                        {"reported_at": "2026-10-22T09:01:00"}, {"calendar_day": "2026-10-21"},
                        {"records": [{**self.record(), "key": "unsafe"}]}, {"part_index": 2}):
            with self.subTest(changes=changes):
                raw = self.event(**changes)
                stored = backend.normalize_sinposmart_event(raw)
                self.assertEqual(stored["snapshot"]["notice_collection"], {})
                self.assertEqual(stored["status"], "failed")
        self.assertFalse(self.view()["count_known"])
        self.assertIsNone(self.view()["today_count"])
        self.assertNotEqual(self.view()["status_class"], "complete")

    def test_notice_events_keep_each_running_result_and_part_id(self):
        for event in (self.event(state="running"), self.event(), self.event(run=2)):
            self.put(event)
        events = self.store.read_day("2026-10-22")["events"]
        self.assertEqual(len(events), 3)
        self.assertTrue(all(event["repeat_count"] == 1 for event in events))

    def test_full_ledger_parts_over_200_are_complete_or_explicitly_unknown(self):
        records = [self.record(number) for number in range(1, 302)]
        first = self.event(records=records[:200], part_count=2, new_count=301, unread_count=301, marked_read_count=301)
        second = self.event(records=records[200:], part_index=1, part_count=2, new_count=301, unread_count=301, marked_read_count=301)
        self.put(first)
        self.assertFalse(self.view()["parts_complete"])
        self.assertIsNone(self.view()["today_count"])
        self.put(second)
        self.put(second)
        view = self.view()
        self.assertTrue(view["parts_complete"])
        self.assertEqual(view["today_count"], 301)
        self.assertEqual(len(view["records"]), 301)
        self.assertEqual(len(view["history"]), 1)

    def test_calendar_day_and_shift_cross_midnight_and_fire_day_boundary(self):
        old = self.record(1, "2026-10-21T23:59:00+08:00")
        early = self.record(2, "2026-10-22T00:01:00+08:00")
        morning = self.record(3, "2026-10-22T07:59:00+08:00")
        self.put(self.event(run=1, reported="2026-10-21T23:59:10+08:00", records=[old]))
        self.put(self.event(run=2, reported="2026-10-22T00:02:00+08:00", records=[old, early],
                            shift_start="2026-10-21T22:00:00+08:00", shift_end="2026-10-22T08:00:00+08:00"))
        self.put(self.event(run=3, reported="2026-10-22T07:59:30+08:00", records=[old, early, morning],
                            shift_start="2026-10-21T22:00:00+08:00", shift_end="2026-10-22T08:00:00+08:00"))
        view = self.view(now=datetime(2026, 10, 22, 8, tzinfo=backend.TAIPEI_TIMEZONE))
        self.assertEqual((view["today_count"], view["shift_count"]), (2, 3))
        self.put(self.event(run=4, reported="2026-10-22T08:01:00+08:00", records=[early, morning]))
        self.assertEqual(self.view()["today_count"], 2)
        self.assertEqual(self.view("2026-10-21")["today_count"], 1)

    def test_retries_and_source_switch_do_not_sum_snapshots_or_fallback_preferred_host(self):
        self.put(self.event(records=[self.record()]))
        self.put(self.event(run=2, records=[self.record()]))
        self.put(self.event(run=3, station="LOCAL-PC", records=[self.record()]))
        self.assertEqual(self.view(workstation="PUBLIC-PC")["today_count"], 1)
        self.assertEqual(len(self.view(workstation="PUBLIC-PC")["history"]), 1)
        self.assertEqual(self.view(workstation="LOCAL-PC")["today_count"], 1)
        missing = self.view(preferred_workstation="NEW-PUBLIC-PC")
        self.assertEqual(missing["workstation"], "NEW-PUBLIC-PC")
        self.assertFalse(missing["count_known"])
        self.assertIsNone(missing["today_count"])
        self.assertIn("尚無回報", missing["status_label"])

    def test_written_receipt_and_read_confirmation_are_separate(self):
        self.put(self.event(records=[self.record(read_state="pending")], state="partial", marked_read_count=0,
                            pending_read_count=1, error_stage="mark_read"))
        view = self.view()
        self.assertEqual(view["today_count"], 1)
        self.assertNotEqual(view["status_class"], "complete")
        self.assertTrue(view["records"][0]["write_confirmed"])
        self.assertEqual(view["records"][0]["read_state"], "pending")

    def test_zero_polls_only_update_summary_and_busy_does_not_fake_scan(self):
        self.put(self.event())
        view = self.view()
        self.assertEqual(view["today_count"], 0)
        self.assertTrue(view["count_known"])
        self.assertEqual(view["history"], [])
        self.put(self.event(run=2, reported="2026-10-22T09:02:00+08:00", state="waiting", error_stage="busy"))
        waiting = self.view()
        self.assertEqual(waiting["last_checked_at"], view["last_checked_at"])
        self.assertEqual(waiting["last_success_at"], view["last_success_at"])
        self.assertNotEqual(waiting["status_class"], "complete")
        self.assertEqual(waiting["history"], [])

    def test_night_30_minute_schedule_is_fresh_until_next_check_plus_tolerance(self):
        self.put(self.event(reported="2026-10-22T00:01:00+08:00"))
        self.assertEqual(self.view(now=datetime(2026, 10, 22, 0, 20, tzinfo=backend.TAIPEI_TIMEZONE))["status_class"], "complete")
        self.assertNotEqual(self.view(now=datetime(2026, 10, 22, 0, 34, tzinfo=backend.TAIPEI_TIMEZONE))["status_class"], "complete")

    def test_historical_date_is_labelled_without_a_live_timeout(self):
        self.put(self.event(reported="2026-10-21T23:01:00+08:00"))
        self.put(self.event(run=2))
        view = self.view("2026-10-21")
        self.assertTrue(view["historical"])
        self.assertIn("歷史", view["status_label"])
        self.assertNotIn("超過", view["status_label"])
        self.assertTrue(view["last_checked_at"].startswith("2026-10-21"))

    def test_late_midnight_receipt_updates_history_ledger_without_borrowing_next_day_check(self):
        checked = "2026-10-21T23:59:00+08:00"
        self.put(self.event(reported=checked))
        late = self.record(1, "2026-10-21T23:59:55+08:00")
        next_day = self.record(2, "2026-10-22T00:00:05+08:00")
        self.put(self.event(run=2, reported="2026-10-22T00:00:10+08:00", records=[late, next_day],
                            started_at="2026-10-21T23:59:50+08:00"))
        self.put(self.event(run=3, station="LOCAL-PC", reported="2026-10-22T00:00:10+08:00",
                            records=[self.record(3, "2026-10-21T23:59:56+08:00")]))
        view = self.view("2026-10-21", workstation="PUBLIC-PC")
        self.assertTrue(view["count_known"])
        self.assertEqual(view["today_count"], 1)
        self.assertEqual([row["key"] for row in view["records"]], [late["key"]])
        self.assertEqual(view["last_received_at"], late["received_at"])
        self.assertEqual(view["last_checked_at"], checked)
        self.assertEqual(view["last_success_at"], checked)
        self.assertEqual(view["last_reported_at"], checked)

    def test_cross_midnight_shift_unknown_or_pending_read_never_gets_success_green(self):
        for index, read_state in enumerate(("unknown", "pending"), 1):
            with self.subTest(read_state=read_state):
                station = f"PUBLIC-{read_state}"
                self.put(self.event(run=index, station=station, reported="2026-10-22T00:01:00+08:00",
                                    records=[self.record(1, "2026-10-21T23:59:55+08:00", read_state)],
                                    new_keys=[], new_count=0, unread_count=0, marked_read_count=0,
                                    shift_start="2026-10-21T22:00:00+08:00", shift_end="2026-10-22T08:00:00+08:00"))
                view = self.view(workstation=station, now=datetime(2026, 10, 22, 0, 2, tzinfo=backend.TAIPEI_TIMEZONE))
                self.assertEqual((view["today_count"], view["shift_count"]), (0, 1))
                self.assertEqual(view["records"][0]["read_state"], read_state)
                self.assertEqual(view["state"], "uncertain")
                self.assertEqual(view["status_class"], "attention")

    def test_late_receipt_without_target_day_report_keeps_unknown_counts_but_shows_evidence(self):
        late = self.record(1, "2026-10-21T23:59:55+08:00")
        next_day = self.record(2, "2026-10-22T00:00:05+08:00")
        self.put(self.event(reported="2026-10-22T00:00:10+08:00", records=[late, next_day]))
        self.put(self.event(run=2, station="LOCAL-PC", reported="2026-10-22T00:00:10+08:00",
                            records=[self.record(3, "2026-10-21T23:59:56+08:00")]))
        view = self.view("2026-10-21", workstation="PUBLIC-PC")
        self.assertFalse(view["count_known"])
        self.assertIsNone(view["today_count"])
        self.assertIsNone(view["shift_count"])
        self.assertEqual(view["last_checked_at"], "")
        self.assertEqual(view["last_success_at"], "")
        self.assertEqual(view["last_reported_at"], "")
        self.assertNotEqual(view["status_class"], "complete")
        self.assertEqual([row["key"] for row in view["records"]], [late["key"]])
        self.assertTrue(view["records"][0]["write_confirmed"])

    def test_invalid_date_query_keeps_admin_health_available_and_sheet_link_tracks_selected_month(self):
        self.put(self.event())
        for day in ("bad", "2026-02-30", [], {}):
            with self.subTest(day=day):
                self.assertEqual(self.view(day)["date"], "2026-10-22")
        for day in ("2025-12-31", "2026-01-01", "2026-10-22", "2026-11-01"):
            with self.subTest(day=day):
                self.assertEqual(self.view(day)["spreadsheet_url"], "/admin/sinposmart/notices/spreadsheet?notice_day=" + day)

    def test_notice_calendar_offers_seven_taiwan_civil_days_without_events(self):
        for current, expected_today in (
            (datetime(2026, 10, 23, 15, 59, 59, tzinfo=timezone.utc), "2026-10-23"),
            (datetime(2026, 10, 23, 16, 0, 1, tzinfo=timezone.utc), "2026-10-24"),
        ):
            with self.subTest(current=current):
                view = self.view(expected_today, now=current)
                today = datetime.fromisoformat(expected_today)
                self.assertEqual(view["calendar_days"],
                                 [(today - timedelta(days=offset)).date().isoformat() for offset in range(7)])
                self.assertEqual(view["records"], [])

    def test_notice_calendar_retains_selected_historical_day_once_in_descending_order(self):
        current = datetime(2026, 10, 24, 0, 1, tzinfo=backend.TAIPEI_TIMEZONE)
        view = self.view("2026-09-30", now=current)
        self.assertEqual(view["calendar_days"], ["2026-10-24", "2026-10-23", "2026-10-22", "2026-10-21",
                                                 "2026-10-20", "2026-10-19", "2026-10-18", "2026-09-30"])
        selected = self.view("2026-10-22", now=current)
        self.assertEqual(len(selected["calendar_days"]), 7)
        self.assertEqual(selected["calendar_days"].count("2026-10-22"), 1)

    def test_untrusted_source_query_and_invalid_payload_never_fallback_to_normal_host(self):
        self.put(self.event())
        view = self.view(workstation='<script>alert(1)</script>')
        self.assertNotIn("<script>", json.dumps(view))
        self.assertFalse(view["count_known"])
        self.put(self.event(run=2, reported="2026-10-22T09:02:00+08:00", new_count=False))
        view = self.view(workstation="PUBLIC-PC")
        self.assertFalse(view["count_known"])
        self.assertNotEqual(view["status_class"], "complete")

    def test_unknown_success_counters_and_unscanned_login_failure_do_not_claim_success(self):
        self.put(self.event(new_count=None))
        view = self.view()
        self.assertFalse(view["count_known"])
        self.assertIsNone(view["today_count"])
        self.assertEqual(view["last_success_at"], "")
        self.assertNotEqual(view["status_class"], "complete")
        self.put(self.event(run=2, reported="2026-10-22T09:02:00+08:00", state="failed", error_stage="login"))
        self.assertEqual(self.view()["last_checked_at"], view["last_checked_at"])

    def test_conflicting_duplicate_part_or_original_receipt_timestamp_is_unknown(self):
        self.put(self.event(records=[self.record()]))
        altered = self.event(records=[self.record(received="2026-10-22T08:00:00+08:00")])
        altered["event_id"] = "conflicting-part"
        self.put(altered)
        self.assertFalse(self.view()["count_known"])
        self.assertIsNone(self.view()["today_count"])

    def test_later_read_confirmation_updates_one_receipt_without_new_history_or_count(self):
        self.put(self.event(records=[self.record(read_state="pending")], state="partial", marked_read_count=0,
                            pending_read_count=1, error_stage="mark_read"))
        self.put(self.event(run=2, reported="2026-10-22T09:02:00+08:00", records=[self.record()], new_keys=[], new_count=0))
        view = self.view()
        self.assertEqual(view["today_count"], 1)
        self.assertEqual(len(view["records"]), 1)
        self.assertEqual(view["records"][0]["read_state"], "confirmed")
        self.assertEqual(view["status_class"], "complete")
        self.assertEqual(len(view["history"]), 1)

    def test_notice_normalization_is_idempotent_and_drops_all_unknown_envelope_data(self):
        raw = self.event(records=[self.record()])
        raw.update(content="password=must-not-retain", error="token=must-not-retain", result_ref="https://unsafe.invalid/secret")
        first = backend.normalize_sinposmart_event(raw)
        again = backend.normalize_sinposmart_event(first)
        self.assertEqual(first["snapshot"], again["snapshot"])
        self.assertNotIn("must-not-retain", json.dumps(first))
        self.assertNotIn("unsafe.invalid", json.dumps(first))

    def test_same_second_terminal_result_outweighs_late_running_delivery(self):
        self.put(self.event(state="partial", error_stage="write", pending_write_count=1))
        self.put(self.event(state="running"))
        self.assertEqual(self.view()["state"], "partial")
        self.assertEqual(self.view()["status_class"], "attention")

    def test_extreme_valid_date_query_does_not_overflow_adjacent_file_lookup(self):
        for day in ("0001-01-01", "9999-12-31"):
            with self.subTest(day=day):
                self.assertEqual(self.view(day)["date"], day)


class DutyGuiHeartbeatTests(unittest.TestCase):
    SERVER = "b32a71f7-4d56-4dab-bc5d-a36be124370a"
    INSTANCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    @classmethod
    def pulse(cls, **changes):
        raw = {"schema_version": 1, "workstation": "SP-TEST", "component": "gui",
               "instance_id": cls.INSTANCE, "process_started_at": "2026-10-18T01:00:00+00:00",
               "process_id": 123, "sequence": 1, "app_version": "2026.10.18.1350",
               "state": "idle", "login_state": "logged_out", "phase": "idle",
               "update_request_id": "", "target_version": "", "result_code": "",
               "exit_code": None, "progress_percent": None,
               "route": {"name": "lan", "identity_status": "verified", "instance_id": cls.SERVER}}
        raw.update(changes)
        return raw

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = backend.DutyGuiHeartbeatStore(Path(self.tmp.name) / "sinposmart_heartbeat.json")
        self.now = datetime(2026, 10, 18, 1, 0, 10, tzinfo=timezone.utc)

    def put(self, raw, now=None):
        return self.store.upsert(raw, server_instance_id=self.SERVER, now=now or self.now)

    def test_strict_private_schema_and_identity(self):
        raw = self.pulse()
        original = copy.deepcopy(raw)
        self.assertTrue(self.put(raw))
        self.assertEqual(raw, original)
        for patch in ({"password": "private"}, {"sequence": True}, {"process_id": False},
                      {"exit_code": True}, {"progress_percent": -1}, {"sequence": 0},
                      {"state": "some task name"}, {"login_state": "actor 24"},
                      {"process_started_at": "2026-10-18T09:00:00"}, {"workstation": "甲"},
                      {"app_version": "https://private.test/?token=private"},
                      {"route": {"name": "lan", "identity_status": "verified", "instance_id": self.INSTANCE}},
                      {"component": "installer"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.put(self.pulse(**patch))
        self.assertNotIn("private", self.store.path.read_text(encoding="utf-8"))

    def test_server_age_boundaries_and_startup_grace_do_not_extend_alive(self):
        self.put(self.pulse(state="starting", phase="startup"))
        self.assertTrue(self.store.admin_view(now=self.now + timedelta(seconds=45))["gui"]["alive"])
        stale = self.store.admin_view(now=self.now + timedelta(seconds=46))["gui"]
        self.assertFalse(stale["alive"])
        self.assertIn("失聯", stale["status_label"])
        self.assertEqual(stale["login_label"], "未登入（最後回報）")
        self.assertEqual(stale["phase_label"], "啟動中（最後回報）")
        self.assertEqual(stale["last_known_label"], "以下為最後回報")
        self.put(self.pulse(sequence=2, state="starting", phase="startup"), self.now + timedelta(seconds=60))
        self.assertEqual(self.store.admin_view(now=self.now + timedelta(seconds=60))["gui"]["startup_label"], "啟動中")
        self.assertEqual(self.store.admin_view(now=self.now + timedelta(seconds=60))["gui"]["last_known_label"], "")
        self.assertEqual(self.store.admin_view(now=self.now + timedelta(seconds=61))["gui"]["startup_label"], "啟動仍未完成")

    def test_admin_time_labels_use_taipei_without_changing_raw_utc_or_liveness(self):
        received = datetime(2026, 10, 24, 18, 40, 10, tzinfo=timezone.utc)
        started = received - timedelta(seconds=10)
        for component in ("gui", "installer"):
            for sequence, zone in enumerate((timezone.utc, backend.TAIPEI_TIMEZONE), 1):
                with self.subTest(component=component, zone=zone):
                    fields = {"component": component, "sequence": sequence,
                              "process_started_at": started.astimezone(zone).isoformat()}
                    if component == "installer":
                        fields.update(login_state="unknown", update_request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd")
                    self.assertTrue(self.put(self.pulse(**fields), received.astimezone(zone)))
                    persisted = self.store.path.read_bytes()
                    view = self.store.admin_view(now=received + timedelta(seconds=30))[component]
                    self.assertEqual(view["last_seen_label"], "2026-10-25 02:40:10")
                    self.assertEqual(view["process_started_label"], "2026-10-25 02:40:00")
                    self.assertEqual(view["last_seen_at"], received.isoformat(timespec="seconds"))
                    self.assertEqual(view["process_started_at"], started.isoformat(timespec="seconds"))
                    self.assertEqual(view["age_seconds"], 30)
                    self.assertTrue(view["fresh"])
                    self.assertTrue(view["alive"])
                    self.assertEqual(self.store.path.read_bytes(), persisted)

    def test_missing_or_invalid_heartbeat_time_labels_are_blank(self):
        for component in ("gui", "installer"):
            view = self.store.admin_view(now=self.now)[component]
            self.assertEqual(view["last_seen_label"], "")
            self.assertEqual(view["process_started_label"], "")
            self.assertEqual(view["last_seen_at"], "尚未收到")
            self.assertEqual(view["process_started_at"], "")
        self.put(self.pulse())
        self.put(self.pulse(component="installer", login_state="unknown",
                            update_request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd"))
        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        data["workstations"]["SP-TEST"]["gui"]["received_at"] = "invalid timestamp"
        data["workstations"]["SP-TEST"]["installer"]["process_started_at"] = "invalid timestamp"
        self.store.path.write_text(json.dumps(data), encoding="utf-8")
        for component in ("gui", "installer"):
            view = self.store.admin_view(now=self.now)[component]
            self.assertEqual(view["last_seen_label"], "")
            self.assertEqual(view["process_started_label"], "")
            self.assertIsNone(view["age_seconds"])
            self.assertFalse(view["alive"])

    def test_taipei_time_label_overflow_does_not_break_heartbeat_health(self):
        limit = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        for component in ("gui", "installer"):
            fields = {"component": component, "process_started_at": limit.isoformat()}
            if component == "installer":
                fields.update(login_state="unknown", update_request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd")
            self.put(self.pulse(**fields), limit)
            view = self.store.admin_view(now=limit)[component]
            self.assertEqual(view["last_seen_label"], "")
            self.assertEqual(view["process_started_label"], "")
            self.assertEqual(view["last_seen_at"], limit.isoformat(timespec="seconds"))
            self.assertEqual(view["process_started_at"], limit.isoformat(timespec="seconds"))
            self.assertEqual(view["age_seconds"], 0)
            self.assertTrue(view["alive"])

    def test_duplicates_old_sequence_metadata_and_retired_instance_never_refresh(self):
        self.put(self.pulse(sequence=4))
        for pulse in (self.pulse(sequence=4), self.pulse(sequence=3),
                      self.pulse(sequence=5, app_version="2026.10.18.1400"),
                      self.pulse(sequence=5, process_id=456)):
            self.assertFalse(self.put(pulse, self.now + timedelta(seconds=30)))
        self.assertEqual(self.store.admin_view(now=self.now)["gui"]["last_seen_at"], self.now.isoformat(timespec="seconds"))
        new = self.pulse(instance_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                         process_started_at="2026-10-18T01:01:00+00:00")
        self.assertTrue(self.put(new, self.now + timedelta(seconds=60)))
        self.assertFalse(self.put(self.pulse(sequence=99), self.now + timedelta(seconds=70)))
        self.assertFalse(self.put(self.pulse(instance_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                                            process_started_at=new["process_started_at"]), self.now + timedelta(seconds=70)))
        view = self.store.admin_view(now=self.now + timedelta(seconds=70))["gui"]
        self.assertEqual(view["instance_id"], new["instance_id"])
        self.assertEqual(view["retired_count"], 1)

    def test_timezone_ordering_and_installer_request_immutability(self):
        self.put(self.pulse(process_started_at="2026-10-18T09:00:00+08:00"))
        self.assertTrue(self.put(self.pulse(instance_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                                            process_started_at="2026-10-18T01:00:01+00:00")))
        request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        installer = self.pulse(component="installer", update_request_id=request_id, login_state="unknown")
        self.assertTrue(self.put(installer))
        installer.update(sequence=2, update_request_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
        self.assertFalse(self.put(installer))

    def test_gui_request_changes_keep_the_same_instance_and_fixed_running_version(self):
        self.put(self.pulse())
        first = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        second = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        for sequence, request_id in ((2, first), (3, second), (4, "")):
            self.assertTrue(self.put(self.pulse(sequence=sequence, update_request_id=request_id),
                                     self.now + timedelta(seconds=sequence)))
            view = self.store.admin_view(now=self.now + timedelta(seconds=sequence))["gui"]
            self.assertEqual(view["instance_id"], self.INSTANCE)
            self.assertEqual(view["update_request_id"], request_id)
            self.assertEqual(view["app_version"], "2026.10.18.1350")

    def test_contract_accepts_safe_leading_punctuation_and_positive_integers(self):
        for workstation in ("_PC", ".PC", "-PC"):
            with self.subTest(workstation=workstation):
                self.assertTrue(self.put(self.pulse(workstation=workstation, process_id=2 ** 53,
                                                    sequence=2 ** 53)))

    def test_malformed_control_status_does_not_hide_gui_or_installer_health(self):
        request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        self.put(self.pulse(update_request_id=request_id))
        self.put(self.pulse(component="installer", login_state="unknown", update_request_id=request_id))
        health = self.store.admin_view(now=self.now)
        for status in ([], {}, None, True):
            with self.subTest(status=status):
                command = {"request_id": request_id, "worker_id": "SP-TEST", "status": status,
                           "exit_code": 0, "installed_version": "2026.10.18.1350"}
                proof = backend.duty_gui_installation_view(health, command)
                self.assertFalse(proof["verified"])
                self.assertTrue(health["gui"]["alive"])
                self.assertTrue(health["installer"]["alive"])

    def test_failed_installer_remains_observable_and_keeps_gui_unknown(self):
        self.put(self.pulse(component="installer", state="failed", phase="failed", login_state="unknown",
                            update_request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd", result_code="gui_ready_timeout",
                            exit_code=-1, progress_percent=90))
        view = self.store.admin_view(now=self.now)
        self.assertFalse(view["gui"]["alive"])
        self.assertTrue(view["installer"]["alive"])
        self.assertEqual(view["installer"]["result_label"], "等待新版啟動逾時")
        self.assertEqual(view["installer"]["progress_label"], "90%")

    def test_malformed_and_future_server_timestamps_fail_closed(self):
        self.put(self.pulse())
        original = json.loads(self.store.path.read_text(encoding="utf-8"))
        for timestamp in ("broken", "2026-10-18T01:00:10", "2027-10-18T01:00:10+00:00"):
            payload = copy.deepcopy(original)
            record = payload["workstations"]["SP-TEST"]["gui"]
            record.update(received_at=timestamp, first_received_at=timestamp, retired_instances=42)
            self.store.path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(self.store.admin_view(now=self.now)["gui"]["alive"])

    def test_no_business_events_installer_and_worker_cannot_make_gui_alive(self):
        empty = self.store.admin_view(now=self.now)
        self.assertFalse(empty["gui"]["alive"])
        self.assertIn("狀態未知", empty["gui"]["status_label"])
        self.put(self.pulse())
        later = self.now + timedelta(seconds=46)
        self.put(self.pulse(component="installer", login_state="unknown", state="updating", phase="installing",
                            update_request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd"), later)
        view = self.store.admin_view(now=later)
        self.assertFalse(view["gui"]["alive"])
        self.assertTrue(view["installer"]["alive"])
        self.assertEqual(view["installer"]["phase_label"], "安裝中")

    def test_installer_display_is_neutral_without_an_executing_host(self):
        health = self.store.admin_view(now=self.now)
        original = copy.deepcopy(health)
        request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        for status in (None, "pending", "preparing", "staged", "waiting_handoff", "completed", "up_to_date"):
            with self.subTest(status=status):
                command = {} if status is None else {"request_id": request_id, "worker_id": "SP-TEST", "status": status}
                view = backend.duty_gui_installer_activity_view(health["installer"], command)
                self.assertEqual(view["status_label"], "未執行更新")
                self.assertEqual(view["status_class"], "")
                self.assertEqual(view["progress_label"], "")
                self.assertFalse(view["alive"])
                self.assertFalse(view["fresh"])
        self.assertEqual(health, original)

    def test_installer_display_warns_when_an_applying_host_is_missing_or_stale(self):
        request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        command = {"request_id": request_id, "worker_id": "SP-TEST", "status": "applying"}
        missing = self.store.admin_view(now=self.now)["installer"]
        self.assertEqual(backend.duty_gui_installer_activity_view(missing, command)["status_label"], "更新狀態待確認")
        self.put(self.pulse(component="installer", state="updating", phase="installing", login_state="unknown",
                            update_request_id=request_id, progress_percent=70))
        original_bytes = self.store.path.read_bytes()
        for status in ("applying", "updating"):
            with self.subTest(status=status):
                original = self.store.admin_view(now=self.now + timedelta(seconds=46))["installer"]
                view = backend.duty_gui_installer_activity_view(original, {**command, "status": status})
                self.assertEqual(view["status_label"], "更新狀態待確認")
                self.assertEqual(view["status_class"], "attention")
                self.assertEqual(view["progress_label"], "")
                for key in ("alive", "fresh", "last_seen_at", "age_seconds", "instance_id", "phase_label"):
                    self.assertEqual(view[key], original[key])
        self.assertEqual(self.store.path.read_bytes(), original_bytes)

    def test_installer_display_shows_matching_fresh_activity_and_failure(self):
        request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        command = {"request_id": request_id, "worker_id": "SP-TEST", "status": "applying"}
        self.put(self.pulse(component="installer", state="updating", phase="installing", login_state="unknown",
                            update_request_id=request_id, progress_percent=70))
        raw = self.store.admin_view(now=self.now)["installer"]
        view = backend.duty_gui_installer_activity_view(raw, command)
        self.assertEqual(view["status_label"], "更新中")
        self.assertEqual(view["status_class"], "running")
        self.assertEqual(view["progress_label"], "70%")
        self.assertTrue(view["alive"])
        self.put(self.pulse(component="installer", state="failed", phase="failed", login_state="unknown",
                            update_request_id=request_id, result_code="gui_ready_timeout", sequence=2, exit_code=0))
        raw = self.store.admin_view(now=self.now)["installer"]
        view = backend.duty_gui_installer_activity_view(raw, command)
        self.assertEqual(view["status_label"], "更新失敗")
        self.assertEqual(view["status_class"], "failed")
        self.assertEqual(view["result_label"], "等待新版啟動逾時")
        self.assertTrue(view["alive"])
        for status in ("failed", "completed", "up_to_date"):
            with self.subTest(status=status):
                view = backend.duty_gui_installer_activity_view(raw, {**command, "status": status})
                self.assertEqual(view["status_label"], "更新失敗")
                self.assertEqual(view["result_label"], "等待新版啟動逾時")

    def test_installer_display_does_not_adopt_other_request_or_owner(self):
        request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        self.put(self.pulse(component="installer", state="updating", phase="installing", login_state="unknown",
                            update_request_id=request_id, progress_percent=70))
        health = self.store.admin_view(now=self.now)
        for command in (
            {"request_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "worker_id": "SP-TEST", "status": "applying"},
            {"request_id": request_id, "worker_id": "OTHER-PC", "status": "updating"},
        ):
            with self.subTest(command=command):
                view = backend.duty_gui_installer_activity_view(health["installer"], command)
                self.assertEqual(view["status_label"], "更新狀態待確認")
                self.assertEqual(view["status_class"], "attention")
                self.assertEqual(view["progress_label"], "")
                self.assertEqual(view["update_request_id"], request_id)
                self.assertFalse(backend.duty_gui_installation_view(health, command)["verified"])
        self.put(self.pulse(component="installer", state="failed", phase="failed", login_state="unknown",
                            update_request_id=request_id, result_code="installer_failed", sequence=2, exit_code=1))
        raw = self.store.admin_view(now=self.now)["installer"]
        view = backend.duty_gui_installer_activity_view(raw, {
            "request_id": request_id, "worker_id": "OTHER-PC", "status": "applying",
        })
        self.assertEqual(view["status_label"], "更新狀態待確認")
        self.assertEqual(view["result_label"], "安裝失敗")

    def test_installer_finished_command_does_not_report_an_old_working_host_lost(self):
        request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        self.put(self.pulse(component="installer", state="updating", phase="installing", login_state="unknown",
                            update_request_id=request_id, progress_percent=70))
        raw = self.store.admin_view(now=self.now + timedelta(seconds=46))["installer"]
        for status in (None, "completed", "up_to_date", "pending", "preparing", "staged", "waiting_handoff"):
            with self.subTest(status=status):
                command = {} if status is None else {"request_id": request_id, "worker_id": "SP-TEST", "status": status}
                view = backend.duty_gui_installer_activity_view(raw, command)
                self.assertEqual(view["status_label"], "未執行更新")
                self.assertEqual(view["status_class"], "")
                self.assertFalse(view["fresh"])
                self.assertEqual(view["last_seen_at"], raw["last_seen_at"])

    def test_installer_display_tolerates_malformed_status_without_changing_raw_health(self):
        request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        self.put(self.pulse(component="installer", state="updating", phase="installing", login_state="unknown",
                            update_request_id=request_id, progress_percent=70))
        raw = self.store.admin_view(now=self.now)["installer"]
        for status in ([], {}, None, True):
            with self.subTest(status=status):
                view = backend.duty_gui_installer_activity_view(raw, {"request_id": request_id, "status": status})
                self.assertEqual(view["alive"], raw["alive"])
                self.assertEqual(view["fresh"], raw["fresh"])
                self.assertEqual(view["update_request_id"], raw["update_request_id"])

    def test_installer_idle_or_closed_history_is_neutral_without_hiding_stale_gui(self):
        request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        self.put(self.pulse())
        self.put(self.pulse(component="installer", state="idle", phase="completed", login_state="unknown",
                            update_request_id=request_id, result_code="update_completed", progress_percent=100))
        for status in (None, "completed", "up_to_date", "pending", "staged", "waiting_handoff"):
            with self.subTest(status=status):
                command = {} if status is None else {"request_id": request_id, "worker_id": "SP-TEST", "status": status}
                health = self.store.admin_view(now=self.now + timedelta(seconds=46))
                view = backend.duty_gui_installer_activity_view(health["installer"], command)
                self.assertEqual(view["status_label"], "未執行更新")
                self.assertEqual(view["status_class"], "")
                self.assertIn("失聯", health["gui"]["status_label"])
                self.assertFalse(health["gui"]["alive"])
        self.put(self.pulse(component="installer", state="stopping", phase="stopping", login_state="unknown",
                            update_request_id=request_id, result_code="closed", sequence=2))
        fresh = self.store.admin_view(now=self.now)["installer"]
        self.assertEqual(backend.duty_gui_installer_activity_view(fresh, {})["status_label"], "未執行更新")

    def test_normal_close_and_invalid_or_unavailable_storage_are_not_online(self):
        self.put(self.pulse(state="stopping", phase="stopping", result_code="closed"))
        closed = self.store.admin_view(now=self.now)["gui"]
        self.assertFalse(closed["alive"])
        self.assertEqual(closed["status_label"], "已回報關閉")
        self.store.path.write_text("broken", encoding="utf-8")
        self.assertFalse(self.store.admin_view(now=self.now)["gui"]["alive"])
        with self.assertRaises(OSError):
            self.put(self.pulse(sequence=2))
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "broken")


class PersonnelTitleSnapshotTests(unittest.TestCase):
    @staticmethod
    def roster(**changes):
        raw = {"schema_version": 1, "source": "official_duty_staff", "target_date": "2026-10-18",
               "complete": True, "members": [{"actor_no": "1", "name": "甲", "title": "分隊長"},
                                             {"actor_no": "2", "name": "乙", "title": "隊員"}]}
        raw.update(changes)
        return raw

    @staticmethod
    def event(roster, occurred_at="2026-10-18T12:00:00", **changes):
        raw = {"record_type": "schedule_snapshot", "occurred_at": occurred_at, "status": "ok",
               "snapshot": {"personnel_titles": roster}}
        raw.update(changes)
        return raw

    def test_normalizer_keeps_only_safe_schema_without_mutating_input(self):
        raw = self.roster(password="private", extra=True)
        raw["members"][0].update(name="  甲  ", account="private", password="private")
        original = copy.deepcopy(raw)
        result = backend.normalize_personnel_title_snapshot(raw)
        self.assertEqual(result, self.roster())
        self.assertIs(result["complete"], True)
        self.assertIs(type(result["schema_version"]), int)
        self.assertEqual(raw, original)
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(backend.normalize_personnel_title_snapshot(result), result)

    def test_normalizer_rejects_wrong_schema_types_dates_and_oversize_instead_of_truncating(self):
        for changes in ({"schema_version": True}, {"schema_version": "1"}, {"complete": "true"},
                        {"complete": 1}, {"complete": False}, {"source": "local_cache"},
                        {"target_date": "2026-02-30"}, {"target_date": "2026/10/18"},
                        {"members": {}}, {"members": self.roster()["members"] * 101}):
            with self.subTest(changes=changes):
                self.assertEqual(backend.normalize_personnel_title_snapshot(self.roster(**changes)), {})
        self.assertEqual(backend.normalize_personnel_title_snapshot(None), {})

    def test_normalizer_rejects_bad_member_values_and_does_not_guess_unknown_titles(self):
        member = {"actor_no": "1", "name": "甲", "title": "分隊長"}
        for changes in ({"actor_no": True}, {"actor_no": "user1"}, {"actor_no": ""},
                        {"name": "<script>"}, {"name": "private123"}, {"name": "甲\n乙"},
                        {"name": ""}, {"name": "甲" * 81}, {"title": True}):
            with self.subTest(changes=changes):
                self.assertEqual(backend.normalize_personnel_title_snapshot(
                    self.roster(members=[{**member, **changes}])), {})
        for title in ("", "同仁", "副分隊長"):
            with self.subTest(title=title):
                result = backend.normalize_personnel_title_snapshot(self.roster(members=[{**member, "title": title}]))
                self.assertEqual(result["members"], [{**member, "title": ""}])

    def test_event_normalizer_preserves_bool_and_explicit_incomplete_tombstone_only_for_schedule(self):
        now = datetime(2026, 10, 18, 12)
        for roster in (self.roster(), self.roster(complete=False, members=[])):
            result = normalize_sinposmart_event(self.event(roster), now=now)
            self.assertEqual(result["snapshot"]["personnel_titles"], roster)
            self.assertEqual(normalize_sinposmart_event(result, now=now)["snapshot"]["personnel_titles"], roster)
        for record_type in ("login", "comparison_snapshot", "tool_action_finished"):
            with self.subTest(record_type=record_type):
                result = normalize_sinposmart_event(self.event(self.roster(), record_type=record_type), now=now)
                self.assertNotIn("personnel_titles", result["snapshot"])
        oversized = self.roster(members=self.roster()["members"] * 101)
        result = normalize_sinposmart_event(self.event(oversized), now=now)["snapshot"]["personnel_titles"]
        self.assertEqual(result, self.roster(complete=False, members=[]))

    def test_event_normalizer_does_not_refresh_roster_from_an_invalid_occurrence_time(self):
        for timestamp in ("", "bad time", None):
            with self.subTest(timestamp=timestamp):
                event = normalize_sinposmart_event(self.event(self.roster(), timestamp), now=datetime(2026, 10, 18, 13))
                self.assertEqual(event["snapshot"]["personnel_titles"], self.roster(complete=False, members=[]))

    def read_events(self, events, now=None, fire_day="2026-10-18"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "sinposmart"
            folder.mkdir()
            path = folder / (fire_day + ".json")
            path.write_text(json.dumps({"events": events}), encoding="utf-8")
            untouched = folder / "2000-01-01.json"
            untouched.write_text("old data", encoding="utf-8")
            before = path.read_bytes()
            with patch.object(SinpoSmartBackendStore, "list_days", side_effect=AssertionError("read-only")), patch.object(
                SinpoSmartBackendStore, "cleanup", side_effect=AssertionError("read-only")
            ):
                result = backend.read_personnel_title_snapshot(root, now=now or datetime(2026, 10, 18, 13))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(untouched.read_text(encoding="utf-8"), "old data")
            return result

    def test_reader_reads_current_roster_from_previous_fire_day_without_writes_or_cleanup(self):
        event = self.event(self.roster(), "2026-10-18T02:30:00+08:00")
        self.assertEqual(self.read_events([event], now=datetime(2026, 10, 18, 3), fire_day="2026-10-17"), self.roster())

    def test_reader_missing_or_malformed_file_is_unknown_and_does_not_create_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "missing"
            self.assertEqual(backend.read_personnel_title_snapshot(root, now=datetime(2026, 10, 18, 13)), {})
            self.assertFalse(root.exists())
            root.mkdir()
            (root / "sinposmart").mkdir()
            path = root / "sinposmart/2026-10-18.json"
            for text in ("invalid JSON", "[]", '{"events": {}}'):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(backend.read_personnel_title_snapshot(root, now=datetime(2026, 10, 18, 13)), {})

    def test_reader_uses_latest_whole_roster_across_workstations_without_filling_old_members(self):
        old = self.event(self.roster(), "2026-10-18T11:00:00")
        fresh = self.roster(members=[{"actor_no": "2", "name": "乙", "title": ""}])
        newer = self.event(fresh, "2026-10-18T04:00:00Z", source="another-workstation")
        self.assertEqual(self.read_events([newer, old]), fresh)
        self.assertEqual(backend.personnel_titles_for_case(fresh, "2026-10-18", ["甲", "乙"]), {})

    def test_reader_latest_incomplete_invalid_or_empty_roster_revokes_old_and_missing_key_does_not(self):
        old = self.event(self.roster(), "2026-10-18T11:00:00")
        for roster in (self.roster(complete=False, members=[]), self.roster(members=[]),
                       self.roster(members=[{"actor_no": "1", "name": "<bad>", "title": "隊員"}])):
            with self.subTest(roster=roster):
                newest = normalize_sinposmart_event(self.event(roster), now=datetime(2026, 10, 18, 13))
                self.assertEqual(self.read_events([old, newest]), {})
        ordinary = self.event(self.roster(), "2026-10-18T12:30:00")
        ordinary["snapshot"].pop("personnel_titles")
        self.assertEqual(self.read_events([old, ordinary]), self.roster())

    def test_reader_rejects_wrong_day_expired_future_and_invalid_times_or_untrusted_source(self):
        for roster, timestamp in ((self.roster(target_date="2026-10-17"), "2026-10-18T12:00:00"),
                                  (self.roster(), "2026-10-17T12:59:59"),
                                  (self.roster(), "2026-10-18T13:00:01"),
                                  (self.roster(), "bad time"),
                                  (self.roster(source="local_cache"), "2026-10-18T12:00:00")):
            with self.subTest(timestamp=timestamp, source=roster["source"]):
                self.assertEqual(self.read_events([self.event(roster, timestamp)]), {})

    def test_reader_equal_time_conflicts_are_unknown_but_order_only_changes_are_equal(self):
        first = self.event(self.roster())
        reversed_rows = self.roster(members=list(reversed(self.roster()["members"])))
        self.assertEqual(self.read_events([first, self.event(reversed_rows)]), self.roster())
        conflict = self.roster(members=[{"actor_no": "1", "name": "甲", "title": "小隊長"}])
        for events in ([first, self.event(conflict)], [self.event(conflict), first],
                       [first, self.event(self.roster(complete=False, members=[]))]):
            with self.subTest(events=events):
                self.assertEqual(self.read_events(events), {})

    def test_selector_matches_case_date_formats_and_preserves_original_personnel(self):
        personnel = ["甲", "乙", "未同步"]
        for case_date in ("2026-10-18", "2026/10/18", "20261018", "1151018"):
            with self.subTest(case_date=case_date):
                self.assertEqual(backend.personnel_titles_for_case(self.roster(), case_date, personnel),
                                 {"甲": "分隊長", "乙": "隊員"})
        self.assertEqual(personnel, ["甲", "乙", "未同步"])
        for case_date in ("2026-10-17", "2026-02-30", "", "bad"):
            self.assertEqual(backend.personnel_titles_for_case(self.roster(), case_date, personnel), {})

    def test_selector_does_not_guess_duplicate_names_actor_reuse_or_conflicting_titles(self):
        for extra in ({"actor_no": "3", "name": "甲", "title": "分隊長"},
                      {"actor_no": "1", "name": "丙", "title": "隊員"},
                      {"actor_no": "01", "name": "丙", "title": "隊員"},
                      {"actor_no": "1", "name": "甲", "title": "隊員"},
                      {"actor_no": "1", "name": "甲", "title": ""}):
            raw = self.roster(members=[*self.roster()["members"], extra])
            with self.subTest(extra=extra):
                self.assertEqual(backend.personnel_titles_for_case(raw, "2026-10-18", ["甲", "乙", "丙"]), {"乙": "隊員"})

    def test_store_and_compact_keep_newest_roster_and_explicit_clear_despite_out_of_order_arrival(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SinpoSmartBackendStore(root / "sinposmart")
            now = datetime(2026, 10, 18, 13)
            newer = self.roster(members=[{"actor_no": "1", "name": "甲", "title": "小隊長"}])
            for number, (roster, timestamp) in enumerate(((self.roster(), "2026-10-18T11:00:00"),
                    (newer, "2026-10-18T12:00:00"), (newer, "2026-10-18T10:00:00"))):
                store.upsert_event(self.event(roster, timestamp, event_id=str(number)), now=now)
            self.assertEqual(backend.read_personnel_title_snapshot(root, now=now), newer)
            tombstone = self.roster(complete=False, members=[])
            store.upsert_event(self.event(tombstone, "2026-10-18T12:30:00", event_id="clear"), now=now)
            payload = json.loads((root / "sinposmart/2026-10-18.json").read_text(encoding="utf-8"))
            payload["events"] = compact_sinposmart_events(payload["events"])
            (root / "sinposmart/2026-10-18.json").write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(backend.read_personnel_title_snapshot(root, now=now), {})

    def test_store_does_not_merge_away_equal_time_roster_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SinpoSmartBackendStore(root / "sinposmart")
            other = self.roster(members=[{"actor_no": "1", "name": "甲", "title": "小隊長"}])
            for number, roster in enumerate((self.roster(), other)):
                store.upsert_event(self.event(roster, event_id=str(number)), now=datetime(2026, 10, 18, 13))
            self.assertEqual(backend.read_personnel_title_snapshot(root, now=datetime(2026, 10, 18, 13)), {})


class SinpoSmartBackendStoreTests(unittest.TestCase):
    @staticmethod
    def daily_vehicle_report():
        return {"target_date": "2026-10-17", "execution_mode": "automatic", "verified": True, "reason_code": "",
                "items": [{"vehicle": "新坡91", "operation": operation, "status": "completed", "reason_code": "saved_verified"}
                          for operation in ("maintenance", "equipment")]}

    @staticmethod
    def daily_vehicle_event(report):
        return {"event_id": "vehicle-completion", "occurred_at": "2026-10-17T10:25:00", "record_type": "tool_action_finished",
                "status": "ok", "snapshot": {"tool_name": "daily_vehicle", "tool_label": "車輛保養清點", "run_id": "vehicle-run",
                "target_date": "115/10/17", "execution_mode": "automatic", "daily_vehicle_report": report}}

    def test_daily_vehicle_report_survives_store_depth_and_preserves_verified_rows(self):
        raw = self.daily_vehicle_event(self.daily_vehicle_report())
        original = json.dumps(raw, ensure_ascii=False)
        now = datetime(2026, 10, 17, 10, 26)
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            stored = store.upsert_event(raw, now=now)
            report = stored["snapshot"]["daily_vehicle_report"]
            self.assertIs(report["verified"], True)
            self.assertEqual(report["items"], raw["snapshot"]["daily_vehicle_report"]["items"])
            before = store.path_for_day("2026-10-17").read_bytes()
            day = store.read_day("2026-10-17", now=now)
            card = day["admin_view"]["tool_events"][0]
            self.assertEqual(card["status_label"], "完成")
            self.assertTrue(card["daily_vehicle_report"]["verified"])
            self.assertEqual(card["daily_vehicle_report"]["mode_label"], "自動")
            self.assertEqual({row["operation_label"] for row in card["daily_vehicle_report"]["items"]}, {"車輛保養", "器材清點"})
            self.assertEqual(store.path_for_day("2026-10-17").read_bytes(), before)
        self.assertEqual(json.dumps(raw, ensure_ascii=False), original)
        again = normalize_sinposmart_event(stored, now=now)["snapshot"]["daily_vehicle_report"]
        self.assertEqual(again, report)

    def test_daily_vehicle_not_required_maintenance_survives_store_and_partial_reports(self):
        now = datetime(2026, 10, 17, 10, 26)
        for verified in (True, False):
            with self.subTest(verified=verified), tempfile.TemporaryDirectory() as tmp:
                report = self.daily_vehicle_report()
                report["items"][0].update(vehicle="BGV2310", status="not_required", reason_code="not_required")
                if not verified:
                    report.update(verified=False, reason_code="save_unconfirmed")
                    report["items"][1].update(status="unconfirmed", reason_code="save_unconfirmed")
                raw = self.daily_vehicle_event(report)
                original = json.dumps(raw, ensure_ascii=False)
                store = SinpoSmartBackendStore(Path(tmp))
                stored = store.upsert_event(raw, now=now)
                self.assertEqual(stored["snapshot"]["daily_vehicle_report"], report)
                self.assertEqual(normalize_sinposmart_event(stored, now=now)["snapshot"]["daily_vehicle_report"], report)
                before = store.path_for_day("2026-10-17").read_bytes()
                card = store.read_day("2026-10-17", now=now)["admin_view"]["tool_events"][0]
                self.assertEqual(card["daily_vehicle_report"]["verified"], verified)
                self.assertEqual(card["daily_vehicle_report"]["items"][0]["status_label"], "不用保養")
                self.assertEqual(card["daily_vehicle_report"]["items"][0]["reason_label"], "網站標示今日不用保養")
                self.assertEqual(card["daily_vehicle_report"]["verified_label"], "已確認全部作業結果")
                self.assertEqual(card["status_label"], "完成" if verified else "結果未確認")
                self.assertEqual(store.path_for_day("2026-10-17").read_bytes(), before)
                self.assertEqual(json.dumps(raw, ensure_ascii=False), original)
        old = normalize_sinposmart_event(self.daily_vehicle_event(self.daily_vehicle_report()))
        self.assertEqual(build_sinposmart_admin_view([old])["tool_events"][0]["daily_vehicle_report"]["verified_label"], "已驗證全部完成")

    def test_daily_vehicle_not_required_requires_exact_maintenance_pair_and_never_overall_reason(self):
        base = self.daily_vehicle_report()
        base["items"][0].update(vehicle="BGV2310", status="not_required", reason_code="not_required")
        changes = ({"operation": "equipment"}, {"reason_code": "verified_today"}, {"vehicle": ""}, {"vehicle": "   "},
                   {"vehicle": "<script>unsafe</script>"}, {"status": "completed"}, {"status": "already_done"}, {"status": "unconfirmed"})
        for change in changes:
            with self.subTest(change=change):
                report = {**base, "items": [{**base["items"][0], **change}, base["items"][1]]}
                event = normalize_sinposmart_event(self.daily_vehicle_event(report))
                clean = event["snapshot"]["daily_vehicle_report"]
                self.assertIs(clean["verified"], False)
                self.assertEqual(clean["reason_code"], "report_invalid")
                self.assertEqual(clean["items"][0]["status"], "unconfirmed")
                self.assertEqual(clean["items"][0]["reason_code"], "report_invalid")
                self.assertEqual(normalize_sinposmart_event(event)["snapshot"]["daily_vehicle_report"], clean)
        for verified in (True, False):
            with self.subTest(overall_not_required=verified):
                event = normalize_sinposmart_event(self.daily_vehicle_event({**base, "verified": verified, "reason_code": "not_required"}))
                clean = event["snapshot"]["daily_vehicle_report"]
                self.assertFalse(clean["verified"])
                self.assertEqual(clean["reason_code"], "report_invalid")
                self.assertEqual(normalize_sinposmart_event(event)["snapshot"]["daily_vehicle_report"], clean)
        report = normalize_sinposmart_event(self.daily_vehicle_event({**base, "items": base["items"][:1]}))["snapshot"]["daily_vehicle_report"]
        self.assertFalse(report["verified"])
        self.assertEqual(report["reason_code"], "report_invalid")

    def test_daily_vehicle_report_cannot_claim_complete_without_strict_full_evidence(self):
        base = self.daily_vehicle_report()
        cases = [{"verified": value} for value in ("True", 1, False, None)] + [
            {"target_date": ""}, {"target_date": "2026-02-30"}, {"target_date": "2026-10-18"},
            {"execution_mode": "unknown"}, {"items": []}, {"items": base["items"][:1]},
            {"verified": None, "items": []},
            {"items": base["items"] + [base["items"][0]]},
            {"items": [base["items"][0], {**base["items"][1], "status": "unconfirmed", "reason_code": "today_not_found"}]},
            {"items": [base["items"][0], {**base["items"][1], "reason_code": "save_unconfirmed"}]},
            {"items": [base["items"][0], {**base["items"][1], "operation": "unknown"}]},
            {"reason_code": "unexpected_error"}, {"reason_code": "<script>client reason</script>"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                event = normalize_sinposmart_event(self.daily_vehicle_event({**base, **changes}))
                report = event["snapshot"]["daily_vehicle_report"]
                self.assertIs(report["verified"], False)
                self.assertNotIn("<script>", json.dumps(report))
                self.assertEqual(build_sinposmart_admin_view([event])["tool_events"][0]["status_label"], "結果未確認")
                self.assertEqual(normalize_sinposmart_event(event)["snapshot"]["daily_vehicle_report"], report)
        for value in (None, "raw client report", [], {"verified": True}):
            with self.subTest(invalid_type=value):
                report = normalize_sinposmart_event(self.daily_vehicle_event(value))["snapshot"]["daily_vehicle_report"]
                self.assertIs(report["verified"], False)
                self.assertEqual(report["reason_code"], "report_invalid")

    def test_daily_vehicle_unconfirmed_rows_cannot_keep_positive_reasons(self):
        for code in ("verified_today", "saved_verified"):
            for claimed in (False, True):
                with self.subTest(code=code, claimed=claimed):
                    raw = self.daily_vehicle_event(self.daily_vehicle_report())
                    raw["snapshot"]["daily_vehicle_report"].update(verified=claimed, reason_code=code)
                    raw["snapshot"]["daily_vehicle_report"]["items"][1].update(status="unconfirmed", reason_code=code)
                    original = json.dumps(raw, ensure_ascii=False)
                    event = normalize_sinposmart_event(raw)
                    report = event["snapshot"]["daily_vehicle_report"]
                    self.assertIs(report["verified"], False)
                    self.assertEqual(report["reason_code"], "report_invalid")
                    self.assertEqual(report["items"][1]["status"], "unconfirmed")
                    self.assertEqual(report["items"][1]["reason_code"], "report_invalid")
                    self.assertEqual(normalize_sinposmart_event(event)["snapshot"]["daily_vehicle_report"], report)
                    self.assertEqual(json.dumps(raw, ensure_ascii=False), original)

    def test_daily_vehicle_report_rejects_unsafe_vehicle_and_unknown_client_fields(self):
        for vehicle in ("secret-car", "TOKEN 1", "password", "cookie", "account", "username", "帳號", "密碼",
                        "https://example.test/token", "新坡91\n注入", "<img src=x onerror=alert(1)>", "車" * 33):
            with self.subTest(vehicle=vehicle):
                base = self.daily_vehicle_report()
                base["items"][0] = {**base["items"][0], "vehicle": vehicle, "raw_text": "must-not-save"}
                base["raw_html"] = "must-not-save"
                report = normalize_sinposmart_event(self.daily_vehicle_event(base))["snapshot"]["daily_vehicle_report"]
                self.assertFalse(report["verified"])
                self.assertNotIn(vehicle, [row["vehicle"] for row in report["items"]])
                self.assertNotIn("must-not-save", json.dumps(report))
                self.assertTrue(all(row["status"] == "unconfirmed" for row in report["items"] if not row["vehicle"]))
        base = self.daily_vehicle_report()
        base["items"][0] = {**base["items"][0], "vehicle": "", "status": "unconfirmed", "reason_code": "empty_list"}
        report = normalize_sinposmart_event(self.daily_vehicle_event(base))["snapshot"]["daily_vehicle_report"]
        self.assertFalse(report["verified"])
        self.assertEqual(report["items"][0]["reason_code"], "empty_list")

    def test_daily_vehicle_report_caps_rows_without_turning_partial_into_verified(self):
        for count in (200, 201):
            with self.subTest(count=count):
                base = self.daily_vehicle_report()
                base["items"] = [{"vehicle": f"車{index // 2}", "operation": "maintenance" if index % 2 == 0 else "equipment",
                                  "status": "already_done", "reason_code": "verified_today"} for index in range(count)]
                report = normalize_sinposmart_event(self.daily_vehicle_event(base))["snapshot"]["daily_vehicle_report"]
                self.assertEqual(len(report["items"]), 200)
                self.assertEqual(report["verified"], count == 200)
                if count > 200:
                    self.assertEqual(report["reason_code"], "row_limit")

    def test_daily_vehicle_failed_report_keeps_partial_and_old_events_stay_unknown(self):
        report = self.daily_vehicle_report()
        report.update(verified=False, reason_code="save_unconfirmed")
        report["items"][1].update(status="unconfirmed", reason_code="save_unconfirmed")
        failed = self.daily_vehicle_event(report)
        failed.update(status="failed", event_id="failed")
        legacy = self.daily_vehicle_event(None)
        legacy["snapshot"].pop("daily_vehicle_report")
        legacy["snapshot"]["run_id"] = "old-vehicle-run"
        legacy["event_id"] = "legacy"
        view = build_sinposmart_admin_view([normalize_sinposmart_event(event) for event in (failed, legacy)])
        cards = {card["event_id"]: card for card in view["tool_events"]}
        self.assertEqual(cards["failed"]["status_label"], "失敗")
        self.assertEqual(cards["failed"]["daily_vehicle_report"]["items"][0]["status"], "completed")
        self.assertEqual(cards["failed"]["daily_vehicle_report"]["items"][1]["status"], "unconfirmed")
        self.assertTrue(cards["legacy"]["daily_vehicle_report_missing"])
        self.assertIsNone(cards["legacy"]["daily_vehicle_report"])
        self.assertNotIn("daily_vehicle_report", normalize_sinposmart_event(legacy)["snapshot"])

    def test_daily_vehicle_report_uses_historical_context_not_current_day(self):
        raw = self.daily_vehicle_event(self.daily_vehicle_report())
        for date_value in ("2026-10-17", "115/10/17", "1151017"):
            with self.subTest(date_value=date_value):
                raw["snapshot"]["target_date"] = date_value
                event = normalize_sinposmart_event(raw, now=datetime(2026, 10, 20, 10))
                self.assertTrue(event["snapshot"]["daily_vehicle_report"]["verified"])
        raw["snapshot"].pop("target_date")
        self.assertTrue(normalize_sinposmart_event(raw)["snapshot"]["daily_vehicle_report"]["verified"])
        raw["occurred_at"] = "2026-10-18T00:01:00"
        self.assertFalse(normalize_sinposmart_event(raw)["snapshot"]["daily_vehicle_report"]["verified"])
        raw["snapshot"]["target_date"] = "115/10/17"
        self.assertTrue(normalize_sinposmart_event(raw)["snapshot"]["daily_vehicle_report"]["verified"])

    def test_daily_vehicle_empty_failed_report_preserves_reported_reason(self):
        now = datetime(2026, 10, 17, 10, 26)
        for code in ("report_missing", "unexpected_error", "execution_unconfirmed"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                raw = self.daily_vehicle_event({**self.daily_vehicle_report(), "verified": False, "reason_code": code, "items": []})
                raw["status"] = "failed"
                store = SinpoSmartBackendStore(Path(tmp))
                event = store.upsert_event(raw, now=now)
                report = event["snapshot"]["daily_vehicle_report"]
                self.assertEqual(report["reason_code"], code)
                self.assertFalse(report["verified"])
                self.assertEqual(normalize_sinposmart_event(event, now=now)["snapshot"]["daily_vehicle_report"], report)
                payload = store.read_day("2026-10-17", now=now)
                self.assertEqual(payload["events"][0]["snapshot"]["daily_vehicle_report"]["reason_code"], code)
                self.assertEqual(payload["admin_view"]["tool_events"][0]["daily_vehicle_report"]["reason_code"], code)

    def test_daily_vehicle_derived_status_cannot_claim_complete_for_conflicting_report(self):
        valid = self.daily_vehicle_report()
        for status, report, expected in (("ok", {**valid, "verified": False}, "結果未確認"),
                                         ("completed", {**valid, "verified": "True"}, "結果未確認"),
                                         ("failed", valid, "失敗")):
            with self.subTest(status=status):
                event = normalize_sinposmart_event({**self.daily_vehicle_event(report), "status": status})
                original = json.dumps(event, ensure_ascii=False)
                card = build_sinposmart_admin_view([event])["tool_events"][0]
                self.assertEqual(card["status_label"], expected)
                self.assertEqual(card["steps"][-1]["status_label"], expected)
                self.assertFalse(card["daily_vehicle_report"]["verified"])
                self.assertEqual(json.dumps(event, ensure_ascii=False), original)
        other_tool = self.daily_vehicle_event({**valid, "verified": False})
        other_tool["snapshot"]["tool_name"] = "duty_sheet"
        self.assertEqual(build_sinposmart_admin_view([normalize_sinposmart_event(other_tool)])["tool_events"][0]["status_label"], "完成")

    @staticmethod
    def login_page_png(width=512, height=1):
        def chunk(kind, content):
            return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))

        header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        pixels = b"\x00" + b"\x80\x80\x80" * 512
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels, 0)) + chunk(b"IEND", b"")

    def test_login_failure_diagnostic_is_sanitized_and_png_survives_store_round_trip(self):
        image = base64.b64encode(self.login_page_png()).decode("ascii")
        self.assertGreater(len(image), 1200)
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            event = store.upsert_event({
                "event_id": "login-password-expired", "occurred_at": "2026-10-15T10:00:00",
                "record_type": "login_failed", "status": "failed", "error": "密碼：不應保存的值",
                "snapshot": {"login_method": "automatic", "login_diagnostic": {
                    "reason_code": "password_change_required", "stage": "post_login",
                    "reason": "每三個月必須更換密碼；password='do not store'",
                    "page_url": "https://worker:private@example.test/change-password?token=hidden#secret",
                    "page_title": "密碼須更新 帳號=private-user",
                    "page_text": "請更換密碼\n帳號：private-user\n密碼：private-password\nhttps://a:b@example.test/form?secret=hidden#fragment",
                    "captured_at": "2026-10-15T10:00:00+08:00", "capture_status": "captured",
                    "image_redacted": True, "image_png": image, "raw_html": "<input value='private-password'>",
                }},
            }, now=datetime(2026, 10, 15, 10, 0))
            day = store.read_day("2026-10-15", now=datetime(2026, 10, 15, 10, 1))
            diagnostic = day["events"][0]["snapshot"]["login_diagnostic"]
            self.assertEqual(diagnostic["image_png"], image)
            self.assertIs(diagnostic["image_redacted"], True)
            self.assertEqual(diagnostic["page_url"], "https://example.test/change-password")
            self.assertEqual(diagnostic["capture_status"], "captured")
            self.assertNotIn("raw_html", diagnostic)
            saved = store.path_for_day("2026-10-15").read_text(encoding="utf-8")
            for secret in ("不應保存的值", "do not store", "private-user", "private-password", "worker:private", "a:b@", "hidden", "fragment"):
                self.assertNotIn(secret, saved)
            card = day["admin_view"]["login_events"][0]
            self.assertEqual(card["status_label"], "密碼須更新")
            self.assertEqual(card["status_class"], "failed")
            self.assertEqual(card["login_diagnostic"]["reason_code"], "password_change_required")
            self.assertEqual(event["record_type"], "login_failed")

    def test_login_capture_errors_round_trip_with_fixed_reasons_and_original_login_reason(self):
        reasons = {
            "driver_unavailable": "瀏覽器尚未建立或已無法使用，無法擷取截圖。",
            "frame_context_failed": "無法切回主頁面，未執行截圖。",
            "redaction_rejected": "欄位或文字遮蔽檢查未通過，未執行截圖。",
            "redaction_failed": "遮蔽頁面時發生錯誤，未執行截圖。",
            "screenshot_failed": "瀏覽器擷取截圖時發生錯誤。",
            "invalid_image": "瀏覽器未回傳有效的 PNG 圖片。",
            "image_oversize": "圖片尺寸或容量超過保存限制。",
            "server_image_rejected": "後台圖片安全或格式驗證未通過，未保存截圖。",
        }
        now = datetime(2026, 10, 16, 22, 11)
        for code, expected in reasons.items():
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                store = SinpoSmartBackendStore(Path(tmp))
                raw = {"event_id": f"capture-{code}", "record_type": "login_failed", "status": "failed",
                    "occurred_at": "2026-10-16T22:10:07", "error": "帳密或權限錯誤",
                    "snapshot": {"login_diagnostic": {"reason_code": "invalid_credentials", "reason": "帳密或權限錯誤",
                        "capture_status": "oversize" if code == "image_oversize" else "unavailable", "image_redacted": False,
                        "capture_error_code": code, "capture_error": "<script>private-message</script> password=private-secret",
                        "capture_error_type": "WebDriverException", "capture_error_stack": "private-stack"}}}
                original = json.dumps(raw, ensure_ascii=False)
                event = store.upsert_event(raw, now=now)
                source_bytes = store.path_for_day("2026-10-16").read_bytes()
                payload = store.read_day("2026-10-16", now=now)
                diagnostic = payload["events"][0]["snapshot"]["login_diagnostic"]
                self.assertEqual(diagnostic["capture_error_code"], code)
                self.assertEqual(diagnostic["capture_error"], expected)
                self.assertEqual(diagnostic["capture_error_type"], "WebDriverException")
                self.assertEqual(diagnostic["reason_code"], "invalid_credentials")
                self.assertEqual(diagnostic["reason"], "帳密或權限錯誤")
                self.assertEqual(event["error"], "帳密或權限錯誤")
                self.assertEqual(payload["admin_view"]["login_events"][0]["login_diagnostic"], diagnostic)
                self.assertEqual(sanitize_login_diagnostic(diagnostic), diagnostic)
                self.assertEqual(store.path_for_day("2026-10-16").read_bytes(), source_bytes)
                self.assertEqual(json.dumps(raw, ensure_ascii=False), original)
                for secret in ("private-message", "private-secret", "private-stack", "capture_error_stack"):
                    self.assertNotIn(secret, source_bytes.decode("utf-8"))

    def test_login_capture_error_types_use_exact_allowlist_and_unknown_codes_stay_unknown(self):
        types = ("Error", "TypeError", "ValueError", "RuntimeError", "TimeoutError", "AttributeError", "SecurityError",
                 "InvalidStateError", "WebDriverException", "TimeoutException", "JavascriptException", "UnexpectedAlertPresentException",
                 "NoSuchWindowException", "InvalidSessionIdException", "NoSuchFrameException", "StaleElementReferenceException", "InvalidArgumentException")
        base = {"reason": "登入失敗", "capture_status": "unavailable", "capture_error_code": "screenshot_failed"}
        for error_type in types:
            with self.subTest(error_type=error_type):
                clean = sanitize_login_diagnostic({**base, "capture_error_type": error_type})
                self.assertEqual(clean["capture_error_type"], error_type)
                self.assertEqual(clean["capture_error"], "瀏覽器擷取截圖時發生錯誤。")
        for error_type in (None, [], {}, True, 12, "unknown", "WebDriverException: private-message", "runtimeerror", " RuntimeError "):
            with self.subTest(rejected_type=error_type):
                clean = sanitize_login_diagnostic({**base, "capture_error_type": error_type, "capture_error": "private-message"})
                self.assertEqual(clean["capture_error_type"], "")
                self.assertNotIn("private-message", json.dumps(clean))
        for code in (None, [], {}, True, 12, "unknown", "Screenshot_failed", "screenshot_failed private-message"):
            with self.subTest(unknown_code=code):
                clean = sanitize_login_diagnostic({**base, "capture_error_code": code,
                    "capture_error": "private-message", "capture_error_type": "RuntimeError"})
                for field in ("capture_error_code", "capture_error", "capture_error_type"):
                    self.assertNotIn(field, clean)

    def test_login_capture_server_rejection_overrides_client_reason_and_forged_success(self):
        png = base64.b64encode(self.login_page_png()).decode("ascii")
        for image, redacted, reported_status, expected_code, expected_status in (
            (png, False, "captured", "server_image_rejected", "unavailable"),
            ("not-base64!", True, "captured", "server_image_rejected", "unavailable"),
            (base64.b64encode(b"not a PNG").decode("ascii"), True, "oversize", "server_image_rejected", "unavailable"),
            ([], True, "unavailable", "server_image_rejected", "unavailable"),
            ({"raw": "private-message"}, True, "unavailable", "server_image_rejected", "unavailable"),
            ("", True, "captured", "server_image_rejected", "unavailable"),
            (None, True, "captured", "server_image_rejected", "unavailable"),
            (base64.b64encode(b"x" * (512 * 1024 + 1)).decode("ascii"), True, "captured", "image_oversize", "oversize"),
            (base64.b64encode(self.login_page_png(4097, 1)).decode("ascii"), True, "captured", "image_oversize", "oversize"),
        ):
            with self.subTest(image_type=type(image).__name__, reported_status=reported_status, expected_code=expected_code):
                raw = {"reason_code": "invalid_credentials", "reason": "帳密或權限錯誤", "image_png": image,
                    "image_redacted": redacted, "capture_status": reported_status, "capture_error_code": "driver_unavailable",
                    "capture_error": "private-message", "capture_error_type": "RuntimeError"}
                clean = sanitize_login_diagnostic(raw)
                self.assertEqual(clean["capture_status"], expected_status)
                self.assertEqual(clean["capture_error_code"], expected_code)
                self.assertEqual(clean["capture_error_type"], "")
                self.assertEqual(clean["reason"], "帳密或權限錯誤")
                self.assertEqual(clean["reason_code"], "invalid_credentials")
                self.assertNotIn("image_png", clean)
                self.assertNotIn("private-message", json.dumps(clean))
                self.assertEqual(sanitize_login_diagnostic(clean), clean)

    def test_login_capture_valid_image_clears_all_claimed_error_fields(self):
        png = base64.b64encode(self.login_page_png()).decode("ascii")
        clean = sanitize_login_diagnostic({"reason": "帳密或權限錯誤", "reason_code": "invalid_credentials",
            "image_png": png, "image_redacted": True, "capture_status": "oversize",
            "capture_error_code": "server_image_rejected", "capture_error": "private-message", "capture_error_type": "RuntimeError"})
        self.assertEqual(clean["capture_status"], "captured")
        self.assertEqual(clean["image_png"], png)
        self.assertEqual(clean["reason"], "帳密或權限錯誤")
        for field in ("capture_error_code", "capture_error", "capture_error_type"):
            self.assertEqual(clean[field], "")
        self.assertEqual(sanitize_login_diagnostic(clean), clean)

    def test_login_capture_old_events_remain_unknown_and_are_not_rewritten(self):
        now = datetime(2026, 10, 16, 22, 11)
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            for status in ("unavailable", "oversize"):
                store.upsert_event({"event_id": f"old-{status}", "occurred_at": "2026-10-16T22:10:07",
                    "record_type": "login_failed", "status": "failed", "snapshot": {"login_diagnostic": {
                        "reason": "帳密或權限錯誤", "capture_status": status, "image_redacted": False}}}, now=now)
            path = store.path_for_day("2026-10-16")
            old_payload = json.loads(path.read_text(encoding="utf-8"))
            for event in old_payload["events"]:
                diagnostic = event["snapshot"]["login_diagnostic"]
                for field in ("capture_error_code", "capture_error", "capture_error_type"):
                    diagnostic.pop(field, None)
            path.write_text(json.dumps(old_payload, ensure_ascii=False), encoding="utf-8")
            original_bytes = path.read_bytes()
            payload = store.read_day("2026-10-16", now=now)
            for event in payload["events"]:
                for field in ("capture_error_code", "capture_error", "capture_error_type"):
                    self.assertNotIn(field, event["snapshot"]["login_diagnostic"])
            for card in payload["admin_view"]["login_events"]:
                diagnostic = card["login_diagnostic"]
                self.assertEqual(sanitize_login_diagnostic(diagnostic), diagnostic)
                if diagnostic["capture_status"] == "oversize":
                    self.assertEqual(diagnostic["capture_error_code"], "image_oversize")
                    self.assertEqual(diagnostic["capture_error"], "圖片尺寸或容量超過保存限制。")
                else:
                    self.assertNotIn("capture_error_code", diagnostic)
                    self.assertNotIn("capture_error", diagnostic)
                    self.assertNotIn("capture_error_type", diagnostic)
            store.list_days(now=now)
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_login_failure_retains_reason_when_evidence_image_is_unsafe(self):
        valid = base64.b64encode(self.login_page_png()).decode("ascii")
        cases = [
            ("not-base64!", True), (base64.b64encode(b"not a png").decode("ascii"), True),
            (base64.b64encode(self.login_page_png()[:-4]).decode("ascii"), True),
            (valid, False), (valid, "true"),
            (base64.b64encode(b"x" * (512 * 1024 + 1)).decode("ascii"), True),
            (base64.b64encode(self.login_page_png(4097, 1)).decode("ascii"), True),
            (base64.b64encode(self.login_page_png(2049, 2048)).decode("ascii"), True),
            (base64.b64encode(self.login_page_png(0, 1)).decode("ascii"), True),
        ]
        for image, redacted in cases:
            with self.subTest(image_prefix=image[:24], redacted=redacted):
                event = normalize_sinposmart_event({
                    "record_type": "login_failed", "status": "failed",
                    "snapshot": {"login_diagnostic": {"reason_code": "password_change_required",
                        "reason": "密碼已到期，請更換密碼", "capture_status": "captured",
                        "image_png": image, "image_redacted": redacted}},
                })
                diagnostic = event["snapshot"]["login_diagnostic"]
                self.assertEqual(diagnostic["reason"], "密碼已到期，請更換密碼")
                self.assertNotIn("image_png", diagnostic)
                self.assertIn(diagnostic["capture_status"], {"unavailable", "oversize"})

    def test_login_evidence_rejects_corrupt_png_content_and_drops_metadata(self):
        png = self.login_page_png()
        def chunk(kind, content):
            return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))
        bad_pixels = chunk(b"IDAT", zlib.compress(b"\x05" + b"\x80\x80\x80" * 512))
        invalid = [png[:-1] + bytes([png[-1] ^ 1]), png + b"trailing", png[:33] + bad_pixels + png[-12:],
                   png[:33] + chunk(b"IDAT", zlib.compress(b"too few pixels")) + png[-12:],
                   png[:33] + chunk(b"IDAT", b"invalid deflate") + png[-12:],
                   png[:-12] + chunk(b"PLTE", b"\x00\x00\x00") + png[-12:],
                   b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 512, 1, 8, 3, 0, 0, 0))
                   + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(512))) + png[-12:]]
        for image in invalid:
            with self.subTest(image_size=len(image)):
                event = normalize_sinposmart_event({"record_type": "login_failed", "snapshot": {"login_diagnostic": {
                    "reason": "密碼須更新", "image_redacted": True, "image_png": base64.b64encode(image).decode("ascii"),
                }}})
                self.assertNotIn("image_png", event["snapshot"]["login_diagnostic"])
        with_metadata = png[:33] + chunk(b"tEXt", b"password=do-not-save") + png[33:]
        event = normalize_sinposmart_event({"record_type": "login_failed", "snapshot": {"login_diagnostic": {
            "reason": "密碼須更新", "image_redacted": True, "image_png": base64.b64encode(with_metadata).decode("ascii"),
        }}})
        cleaned = base64.b64decode(event["snapshot"]["login_diagnostic"]["image_png"])
        self.assertEqual(cleaned, png)
        self.assertNotIn(b"do-not-save", cleaned)
        rgba_png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(b"\x00\x80\x80\x80\xff")) + png[-12:])
        event = normalize_sinposmart_event({"record_type": "login_failed", "snapshot": {"login_diagnostic": {
            "reason": "密碼須更新", "image_redacted": True, "image_png": base64.b64encode(rgba_png).decode("ascii"),
        }}})
        self.assertEqual(base64.b64decode(event["snapshot"]["login_diagnostic"]["image_png"]), rgba_png)

    def test_login_failure_diagnostic_bounds_fields_and_rejects_unsafe_urls(self):
        for url in ("javascript:alert(1)", "data:text/html,hello", "//example.test/path", "https://[bad/path", "https://example.test\\@evil.test/path"):
            with self.subTest(url=url):
                event = normalize_sinposmart_event({"record_type": "login_failed", "snapshot": {"login_diagnostic": {
                    "reason_code": "unknown", "stage": "unsafe", "reason": "理" * 700,
                    "page_url": url, "page_title": "題" * 200, "page_text": "文" * 4500,
                    "captured_at": "時" * 80, "capture_status": "unknown", "extra": "discard",
                }}})
                diagnostic = event["snapshot"]["login_diagnostic"]
                self.assertEqual(diagnostic["reason_code"], "login_failed")
                self.assertEqual(diagnostic["stage"], "login")
                self.assertEqual(diagnostic["page_url"], "")
                self.assertEqual(len(diagnostic["reason"]), 600)
                self.assertEqual(len(diagnostic["page_title"]), 160)
                self.assertEqual(len(diagnostic["page_text"]), 4000)
                self.assertEqual(len(diagnostic["captured_at"]), 40)
                self.assertNotIn("extra", diagnostic)
        for value in ({}, None, "invalid", []):
            with self.subTest(value=value):
                event = normalize_sinposmart_event({"record_type": "login_failed", "snapshot": {"login_diagnostic": value}})
                self.assertEqual(event["snapshot"]["login_diagnostic"], {})
        event = normalize_sinposmart_event({"record_type": "login_failed", "snapshot": {"login_diagnostic": {
            "reason_code": [], "reason": {"secret": "must-not-stringify"}, "page_text": ["must-not-stringify"],
        }}})
        self.assertEqual(event["snapshot"]["login_diagnostic"]["reason_code"], "login_failed")
        self.assertNotIn("must-not-stringify", json.dumps(event))
        for text in ('{"token": "demo token secret"}', "'password': 'demo password secret'", '"密碼"："範例密碼 值"',
                     "Authorization: Bearer demo-bearer-value", '"authorization": "Bearer demo-bearer-value"',
                     '"session_id": "demo session secret"', "session-id='demo session secret'", "sessionid=demo-session-secret",
                     '"api_key": "demo api secret"', "api-key='demo api secret'", "apikey=demo-api-secret"):
            with self.subTest(assignment=text):
                event = normalize_sinposmart_event({"record_type": "login_failed", "error": text,
                    "snapshot": {"login_diagnostic": {"reason": text, "page_text": text, "page_title": text}}})
                for secret in ("demo token secret", "demo password secret", "範例密碼 值", "demo-bearer-value",
                               "demo session secret", "demo-session-secret", "demo api secret", "demo-api-secret"):
                    self.assertNotIn(secret, json.dumps(event, ensure_ascii=False))

    def test_fire_day_uses_0800_boundary(self):
        self.assertEqual(sinposmart_fire_day_for(datetime(2026, 6, 15, 7, 59)), "2026-06-14")
        self.assertEqual(sinposmart_fire_day_for(datetime(2026, 6, 15, 8, 0)), "2026-06-15")

    def test_aware_timestamp_is_converted_to_taipei_before_fire_day_boundary(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-aware-time",
                "occurred_at": "2026-07-13T00:00:00Z",
                "record_type": "action_result",
                "status": "submitted",
            },
            now=datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(event["occurred_at"], "2026-07-13T08:00:00")
        self.assertEqual(event["fire_day"], "2026-07-13")

    def test_normalize_records_server_received_time_instead_of_client_value(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-server-received-time",
                "occurred_at": "2026-07-13T08:00:00",
                "received_at": "2000-01-01T00:00:00",
                "record_type": "action_queued",
                "status": "pending_write_automation",
            },
            now=datetime(2026, 7, 13, 8, 1),
        )

        self.assertEqual(event["received_at"], "2026-07-13T08:01:00")

    def test_concurrent_store_instances_preserve_every_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def write_event(index: int) -> str:
                try:
                    SinpoSmartBackendStore(root).upsert_event(
                        {
                            "event_id": f"evt-concurrent-{index}",
                            "occurred_at": f"2026-07-13T09:00:{index:02d}",
                            "record_type": "action_result",
                            "status": "submitted",
                            "content": f"concurrent event {index}",
                        },
                        now=datetime(2026, 7, 13, 9, 0),
                    )
                except OSError as exc:
                    return type(exc).__name__
                return ""

            with ThreadPoolExecutor(max_workers=16) as pool:
                errors = [error for error in pool.map(write_event, range(24)) if error]

            events = SinpoSmartBackendStore(root).read_day("2026-07-13")["events"]

            self.assertEqual(errors, [])
            self.assertEqual(len(events), 24)
            self.assertEqual({event["event_id"] for event in events}, {f"evt-concurrent-{index}" for index in range(24)})

    def test_event_dedupes_and_sanitizes_sensitive_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            payload = {
                "event_id": "evt-1",
                "occurred_at": "2026-06-15T09:00:00",
                "record_type": "action_result",
                "status": "submitted",
                "content": "完成 password=secret",
                "snapshot": {
                    "actions": [{"title": "值班交接"}],
                    "token": "hidden",
                    "nested": {"cookie": "hidden", "safe": "ok"},
                },
            }

            first = store.upsert_event(payload, now=datetime(2026, 6, 15, 10, 0))
            second = store.upsert_event(payload, now=datetime(2026, 6, 15, 10, 0))
            day = store.read_day("2026-06-15", now=datetime(2026, 6, 15, 12, 10, 41))

            self.assertEqual(first["event_id"], "evt-1")
            self.assertEqual(second["event_id"], "evt-1")
            self.assertEqual(len(day["events"]), 1)
            self.assertNotIn("secret", day["events"][0]["content"])
            self.assertNotIn("token", day["events"][0]["snapshot"])
            self.assertNotIn("cookie", day["events"][0]["snapshot"]["nested"])
            self.assertEqual(day["events"][0]["snapshot"]["nested"]["safe"], "ok")

    def test_events_with_different_ids_merge_when_content_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            first_payload = {
                "event_id": "evt-merge-1",
                "occurred_at": "2026-06-15T09:00:00",
                "record_type": "action_result",
                "status": "submitted",
                "actor_no": "8",
                "display_name": "8號學長 - tyfd01510",
                "trigger_type": "manual",
                "item_kind": "出入",
                "item_title": "休息後退勤",
                "content": "已登打休息後退勤",
                "target": "4",
                "target_time": "06:00",
            }
            second_payload = dict(first_payload, event_id="evt-merge-2", occurred_at="2026-06-15T09:01:00")

            store.upsert_event(first_payload, now=datetime(2026, 6, 15, 9, 0))
            store.upsert_event(second_payload, now=datetime(2026, 6, 15, 9, 1))
            day = store.read_day("2026-06-15")

            self.assertEqual(len(day["events"]), 1)
            self.assertEqual(day["events"][0]["repeat_count"], 2)
            self.assertEqual(day["events"][0]["first_occurred_at"], "2026-06-15T09:00:00")
            self.assertEqual(day["events"][0]["last_occurred_at"], "2026-06-15T09:01:00")

    def test_retry_of_merged_event_id_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            first_payload = {
                "event_id": "evt-merged-retry-1",
                "occurred_at": "2026-06-15T09:00:00",
                "record_type": "action_result",
                "status": "submitted",
                "actor_no": "8",
                "content": "same logical result",
            }
            second_payload = dict(first_payload, event_id="evt-merged-retry-2", occurred_at="2026-06-15T09:01:00")

            store.upsert_event(first_payload, now=datetime(2026, 6, 15, 9, 0))
            store.upsert_event(second_payload, now=datetime(2026, 6, 15, 9, 1))
            store.upsert_event(second_payload, now=datetime(2026, 6, 15, 9, 2))
            merged = store.read_day("2026-06-15")["events"][0]

            self.assertEqual(merged["repeat_count"], 2)
            self.assertEqual(merged["merged_event_ids"], ["evt-merged-retry-1", "evt-merged-retry-2"])
            self.assertEqual(merged["last_occurred_at"], "2026-06-15T09:01:00")

    def test_same_event_id_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            payload = {
                "event_id": "evt-update-logout-retry",
                "occurred_at": "2026-06-15T07:19:54",
                "record_type": "logout",
                "status": "ok",
                "actor_no": "8",
                "display_name": "8番 隊員 曾彥綸",
                "trigger_type": "update",
                "content": "更新前登出",
            }
            retry_payload = dict(payload, occurred_at="2026-06-15T07:20:10")

            store.upsert_event(payload, now=datetime(2026, 6, 15, 7, 19))
            store.upsert_event(retry_payload, now=datetime(2026, 6, 15, 7, 20))
            day = store.read_day("2026-06-14")

            self.assertEqual(len(day["events"]), 1)
            self.assertEqual(day["events"][0]["repeat_count"], 1)
            self.assertEqual(day["events"][0]["occurred_at"], "2026-06-15T07:19:54")
            self.assertEqual(day["events"][0]["last_occurred_at"], "2026-06-15T07:19:54")

    def test_existing_login_record_repeat_count_is_normalized_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            path = store.path_for_day("2026-06-14")
            path.write_text(
                """{
  "fire_day": "2026-06-14",
  "updated_at": "2026-06-15T07:20:10",
  "events": [
    {
      "event_id": "evt-update-logout-retry",
      "occurred_at": "2026-06-15T07:20:10",
      "last_occurred_at": "2026-06-15T07:20:10",
      "record_type": "logout",
      "status": "ok",
      "actor_no": "8",
      "display_name": "8番 隊員 曾彥綸",
      "trigger_type": "update",
      "content": "更新前登出",
      "repeat_count": 2
    }
  ]
}""",
                encoding="utf-8",
            )

            day = store.read_day("2026-06-14")

            self.assertEqual(day["events"][0]["repeat_count"], 1)
            self.assertEqual(day["admin_view"]["login_events"][0]["repeat_count"], 1)

    def test_existing_merged_tool_event_keeps_its_audit_repeat_count(self):
        compacted = compact_sinposmart_events(
            [
                {
                    "event_id": "legacy-tool-start-1",
                    "merged_event_ids": ["legacy-tool-start-1", "legacy-tool-start-2"],
                    "occurred_at": "2026-09-07T08:00:00",
                    "record_type": "tool_action_started",
                    "repeat_count": 2,
                    "snapshot": {"tool_name": "duty_sheet"},
                }
            ]
        )

        self.assertEqual(len(compacted), 1)
        self.assertEqual(compacted[0]["repeat_count"], 2)
        self.assertEqual(
            compacted[0]["merged_event_ids"],
            ["legacy-tool-start-1", "legacy-tool-start-2"],
        )

    def test_login_events_with_different_ids_do_not_merge_when_content_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            first_payload = {
                "event_id": "evt-login-1",
                "occurred_at": "2026-06-15T09:00:00",
                "record_type": "login",
                "status": "ok",
                "actor_no": "4",
                "display_name": "4番 隊員 測試",
            }
            second_payload = dict(first_payload, event_id="evt-login-2", occurred_at="2026-06-15T09:05:00")

            store.upsert_event(first_payload, now=datetime(2026, 6, 15, 9, 0))
            store.upsert_event(second_payload, now=datetime(2026, 6, 15, 9, 5))
            day = store.read_day("2026-06-15")

            self.assertEqual(len(day["events"]), 2)
            self.assertEqual(len(day["admin_view"]["login_events"]), 2)
            self.assertEqual([event["last_occurred_at"] for event in day["admin_view"]["login_events"]], ["2026-06-15T09:05:00", "2026-06-15T09:00:00"])

    def test_cleanup_keeps_only_recent_seven_fire_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            for day in range(1, 10):
                store.upsert_event(
                    {
                        "event_id": f"evt-{day}",
                        "occurred_at": f"2026-06-{day:02d}T09:00:00",
                        "record_type": "login",
                        "status": "ok",
                    },
                    now=datetime(2026, 6, day, 9, 0),
                )

            days = store.list_days(now=datetime(2026, 6, 9, 9, 0))
            fire_days = [day["fire_day"] for day in days]

            self.assertEqual(len(days), 7)
            self.assertNotIn("2026-06-01", fire_days)
            self.assertIn("2026-06-03", fire_days)
            self.assertIn("2026-06-09", fire_days)

    def test_normalize_invalid_record_type_as_error(self):
        event = normalize_sinposmart_event(
            {"record_type": "unknown", "occurred_at": "bad-date"},
            now=datetime(2026, 6, 15, 9, 0),
        )

        self.assertEqual(event["record_type"], "error")
        self.assertEqual(event["fire_day"], "2026-06-15")

    def test_status_label_translates_common_backend_statuses(self):
        self.assertEqual(sinposmart_status_label("started", "tool_action_started"), "開始")
        self.assertEqual(sinposmart_status_label("submitted", "action_result"), "已登打")
        self.assertEqual(sinposmart_status_label("ok", "login"), "成功")
        self.assertEqual(sinposmart_status_label("", "login"), "登入")
        self.assertEqual(sinposmart_status_label("queued_for_worker", "action_result"), "登打結果")

    def test_tool_action_started_keeps_record_type_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            event = store.upsert_event(
                {
                    "event_id": "evt-tool-start",
                    "occurred_at": "2026-06-15T12:10:00",
                    "record_type": "tool_action_started",
                    "trigger_type": "tool_start",
                    "status": "started",
                    "actor_no": "8",
                    "user_id": "tyfd01510",
                    "snapshot": {"tool_name": "duty_sheet", "tool_label": "勤務表登打"},
                },
                now=datetime(2026, 6, 15, 12, 10),
            )
            day = store.read_day("2026-06-15", now=datetime(2026, 6, 15, 12, 10, 41))

            self.assertEqual(event["record_type"], "tool_action_started")
            self.assertEqual(event["item_title"], "開始勤務表登打")
            self.assertEqual(day["summary"]["tool_starts"], 1)
            self.assertEqual(day["summary"]["failed"], 0)
            self.assertEqual(len(day["admin_view"]["tool_events"]), 1)
            self.assertEqual(day["admin_view"]["tool_events"][0]["record_label"], "工具開始")
            self.assertEqual(day["admin_view"]["tool_events"][0]["status_label"], "執行中")
            self.assertEqual(day["admin_view"]["tool_events"][0]["waiting_age_seconds"], 41)
            self.assertEqual(
                day["admin_view"]["tool_events"][0]["waiting_reason"],
                "已收到開始執行，已等待 41 秒，等待工具完成結果回傳。",
            )
            self.assertEqual(day["admin_view"]["tool_events"][0]["pause_reason"], "")

    def test_admin_view_combines_tool_start_and_finish_with_result(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-tool-start",
                    "occurred_at": "2026-06-18T16:30:52",
                    "record_type": "tool_action_started",
                    "trigger_type": "tool_start",
                    "status": "started",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                    "snapshot": {"tool_name": "duty_sheet", "tool_label": "勤務表登打"},
                },
                now=datetime(2026, 6, 18, 16, 30),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-tool-finish",
                    "occurred_at": "2026-06-18T16:31:30",
                    "record_type": "tool_action_finished",
                    "trigger_type": "tool_finish",
                    "status": "completed",
                    "content": "勤務表登打完成：115/06/19",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                    "snapshot": {"tool_name": "duty_sheet", "tool_label": "勤務表登打"},
                },
                now=datetime(2026, 6, 18, 16, 31),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["tool_events"]), 1)
        self.assertEqual(view["tool_events"][0]["item_title"], "勤務表登打")
        self.assertEqual(view["tool_events"][0]["status_label"], "完成")
        self.assertEqual(view["tool_events"][0]["result_text"], "勤務表登打完成：115/06/19")
        self.assertEqual([step["label"] for step in view["tool_events"][0]["steps"]], ["開始執行", "結束執行"])

    def test_admin_view_keeps_rescue_video_runs_separate_with_summary(self):
        events = []
        for run_id, occurred_at, case_count, total_count, usage_seconds in (
            ("rescue-run-1", "2026-08-24T10:00:00", 2, 5, 65),
            ("rescue-run-2", "2026-08-24T10:10:00", 1, 2, 30),
        ):
            shared_snapshot = {
                "tool_name": "rescue_video",
                "tool_label": "救護行車紀錄器",
                "run_id": run_id,
            }
            events.append(
                normalize_sinposmart_event(
                    {
                        "event_id": f"{run_id}-started",
                        "occurred_at": occurred_at,
                        "record_type": "tool_action_started",
                        "trigger_type": "tool_start",
                        "status": "started",
                        "actor_no": "27",
                        "display_name": "27番 隊員 林宏為",
                        "snapshot": shared_snapshot,
                    },
                    now=datetime(2026, 8, 24, 10, 0),
                )
            )
            events.append(
                normalize_sinposmart_event(
                    {
                        "event_id": f"{run_id}-finished",
                        "occurred_at": occurred_at,
                        "record_type": "tool_action_finished",
                        "trigger_type": "tool_finish",
                        "status": "completed",
                        "content": "救護行車紀錄器完成",
                        "actor_no": "27",
                        "display_name": "27番 隊員 林宏為",
                        "snapshot": {
                            **shared_snapshot,
                            "target_date": "2026-08-24",
                            "vehicle": "92",
                            "case_count": case_count,
                            "total_count": total_count,
                            "usage_seconds": usage_seconds,
                        },
                    },
                    now=datetime(2026, 8, 24, 10, 0),
                )
            )

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["tool_events"]), 2)
        summaries = {
            event["rescue_video_summary"]["total_count"]: event["rescue_video_summary"]
            for event in view["tool_events"]
        }
        self.assertEqual(
            summaries[5],
            {
                "target_date": "2026-08-24",
                "vehicle": "92",
                "case_count": 2,
                "total_count": 5,
                "usage_time": "1 分 5 秒",
            },
        )
        self.assertEqual(summaries[2]["usage_time"], "30 秒")
        self.assertTrue(
            all(
                [step["label"] for step in event["steps"]] == ["開始執行", "結束執行"]
                for event in view["tool_events"]
            )
        )

    def test_tool_run_id_pairs_terminal_even_when_identity_changes(self):
        for actor_no, display_name in (("", ""), ("11", "11番 測試乙")):
            with self.subTest(actor_no=actor_no), tempfile.TemporaryDirectory() as tmp:
                store = SinpoSmartBackendStore(Path(tmp))
                for suffix, record_type, status, actor, name, at in (
                    ("start", "tool_action_started", "started", "10", "10番 測試甲", "2026-10-01T09:00:00"),
                    ("finish", "tool_action_finished", "failed", actor_no, display_name, "2026-10-01T09:00:30"),
                ):
                    store.upsert_event({
                        "event_id": suffix, "occurred_at": at, "record_type": record_type,
                        "status": status, "actor_no": actor, "display_name": name,
                        "error": "測試失敗" if suffix == "finish" else "",
                        "snapshot": {"tool_name": "duty_sheet", "run_id": "test-run"},
                    }, now=datetime(2026, 10, 1, 9, 1))
                cards = store.read_day("2026-10-01", now=datetime(2026, 10, 1, 9, 2))["admin_view"]["tool_events"]
                self.assertEqual(len(cards), 1)
                self.assertEqual(cards[0]["status_label"], "失敗")
                self.assertEqual(cards[0]["person_label"], "10番 測試甲")
                self.assertEqual(len(cards[0]["steps"]), 2)

    def test_store_keeps_same_tool_runs_separate_by_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            for run_id, started_at, finished_at, status, result_text in (
                ("duty-run-1", "2026-09-07T09:00:00", "2026-09-07T09:00:20", "failed", "第一次失敗"),
                ("duty-run-2", "2026-09-07T09:05:00", "2026-09-07T09:05:20", "completed", "第二次完成"),
            ):
                snapshot = {
                    "tool_name": "duty_sheet",
                    "tool_label": "勤務表登打",
                    "run_id": run_id,
                }
                store.upsert_event(
                    {
                        "event_id": f"{run_id}-started",
                        "occurred_at": started_at,
                        "record_type": "tool_action_started",
                        "trigger_type": "tool_start",
                        "status": "started",
                        "actor_no": "14",
                        "display_name": "14番 隊員 測試",
                        "snapshot": snapshot,
                    },
                    now=datetime(2026, 9, 7, 9, 0),
                )
                store.upsert_event(
                    {
                        "event_id": f"{run_id}-finished",
                        "occurred_at": finished_at,
                        "record_type": "tool_action_finished",
                        "trigger_type": "tool_finish",
                        "status": status,
                        "actor_no": "14",
                        "display_name": "14番 隊員 測試",
                        "content": result_text if status == "completed" else "",
                        "error": result_text if status == "failed" else "",
                        "snapshot": snapshot,
                    },
                    now=datetime(2026, 9, 7, 9, 5),
                )

            day = store.read_day("2026-09-07", now=datetime(2026, 9, 7, 9, 6))

        self.assertEqual(len(day["events"]), 4)
        self.assertEqual(
            {event["event_id"] for event in day["events"]},
            {
                "duty-run-1-started",
                "duty-run-1-finished",
                "duty-run-2-started",
                "duty-run-2-finished",
            },
        )
        self.assertEqual([event["repeat_count"] for event in day["events"]], [1, 1, 1, 1])
        cards_by_event_id = {card["event_id"]: card for card in day["admin_view"]["tool_events"]}
        self.assertEqual(len(cards_by_event_id), 2)
        self.assertEqual(cards_by_event_id["duty-run-1-finished"]["status_label"], "失敗")
        self.assertEqual(cards_by_event_id["duty-run-2-finished"]["status_label"], "完成")
        self.assertTrue(
            all(
                [step["label"] for step in card["steps"]] == ["開始執行", "結束執行"]
                for card in cards_by_event_id.values()
            )
        )

    def test_store_keeps_sequential_same_tool_runs_without_run_id_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            for event_id, occurred_at, record_type, trigger_type, status, result_text in (
                ("legacy-run-1-started", "2026-09-07T10:00:00", "tool_action_started", "tool_start", "started", ""),
                ("legacy-run-1-finished", "2026-09-07T10:00:20", "tool_action_finished", "tool_finish", "failed", "第一次失敗"),
                ("legacy-run-2-started", "2026-09-07T10:05:00", "tool_action_started", "tool_start", "started", ""),
                ("legacy-run-2-finished", "2026-09-07T10:05:20", "tool_action_finished", "tool_finish", "completed", "第二次完成"),
            ):
                store.upsert_event(
                    {
                        "event_id": event_id,
                        "occurred_at": occurred_at,
                        "record_type": record_type,
                        "trigger_type": trigger_type,
                        "status": status,
                        "actor_no": "14",
                        "display_name": "14番 隊員 測試",
                        "content": result_text if status == "completed" else "",
                        "error": result_text if status == "failed" else "",
                        "snapshot": {
                            "tool_name": "duty_sheet",
                            "tool_label": "勤務表登打",
                        },
                    },
                    now=datetime(2026, 9, 7, 10, 6),
                )

            day = store.read_day("2026-09-07", now=datetime(2026, 9, 7, 10, 7))

        self.assertEqual(len(day["events"]), 4)
        self.assertEqual(
            {event["event_id"] for event in day["events"]},
            {
                "legacy-run-1-started",
                "legacy-run-1-finished",
                "legacy-run-2-started",
                "legacy-run-2-finished",
            },
        )
        self.assertEqual([event["repeat_count"] for event in day["events"]], [1, 1, 1, 1])
        cards_by_event_id = {card["event_id"]: card for card in day["admin_view"]["tool_events"]}
        self.assertEqual(len(cards_by_event_id), 2)
        self.assertEqual(cards_by_event_id["legacy-run-1-finished"]["status_label"], "失敗")
        self.assertEqual(cards_by_event_id["legacy-run-2-finished"]["status_label"], "完成")
        self.assertTrue(
            all(
                [step["label"] for step in card["steps"]] == ["開始執行", "結束執行"]
                for card in cards_by_event_id.values()
            )
        )

    def test_admin_view_shows_safe_tool_failure_stage(self):
        view = build_sinposmart_admin_view(
            [
                normalize_sinposmart_event(
                    {
                        "event_id": "evt-tool-failed",
                        "occurred_at": "2026-08-02T10:00:00",
                        "record_type": "tool_action_finished",
                        "trigger_type": "tool_finish",
                        "status": "failed",
                        "snapshot": {
                            "tool_name": "rest_time",
                            "tool_label": "休息時間登打",
                            "failure_stage": "browser_start",
                            "failure_detail": "browser_startup",
                        },
                    },
                    now=datetime(2026, 8, 2, 10, 0),
                )
            ]
        )

        self.assertEqual(view["tool_events"][0]["failure_stage_label"], "瀏覽器啟動")
        self.assertEqual(
            view["tool_events"][0]["failure_detail"],
            "專用瀏覽器啟動或連線未完成；已清理過期暫存設定檔並重試一次。",
        )
        self.assertEqual(view["tool_events"][0]["status_label"], "失敗")
        self.assertEqual(view["tool_events"][0]["waiting_reason"], "")
        stages = {
            "maintenance_page_ready": "等待保養頁初始化",
            "maintenance_query_start": "啟動保養查詢",
            "maintenance_query_wait": "等待保養查詢結果",
            "maintenance_query_verify": "核對保養本次查詢",
            "maintenance_inventory_verify": "核對保養完整清單",
            "equipment_page_ready": "等待器材頁初始化",
            "equipment_query_start": "啟動器材查詢",
            "equipment_query_wait": "等待器材查詢結果",
            "equipment_query_verify": "核對器材本次查詢",
            "equipment_inventory_verify": "核對器材完整清單",
            "maintenance_unknown": "",
            "equipment_query_wait<script>fixture</script>": "",
        }
        for stage, expected in stages.items():
            with self.subTest(stage=stage):
                event = normalize_sinposmart_event({
                    "event_id": "evt-query-stage", "occurred_at": "2026-10-18T10:03:29",
                    "record_type": "tool_action_finished", "status": "failed",
                    "snapshot": {"tool_name": "daily_vehicle", "failure_stage": stage},
                })
                card = build_sinposmart_admin_view([event])["tool_events"][0]
                self.assertEqual(card["failure_stage_label"], expected)
                self.assertEqual(card["status_label"], "失敗")
                self.assertEqual(event["snapshot"]["failure_stage"], stage)

    def test_admin_view_marks_tool_waiting_as_attention_without_calling_it_failed(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-tool-overdue",
                "occurred_at": "2026-08-09T08:29:02",
                "record_type": "tool_action_started",
                "trigger_type": "tool_start",
                "status": "started",
                "snapshot": {"tool_name": "daily_vehicle", "tool_label": "車輛保養清點"},
            },
            now=datetime(2026, 8, 9, 8, 29, 2),
        )

        view = build_sinposmart_admin_view([event], now=datetime(2026, 8, 9, 8, 44, 2))
        card = view["tool_events"][0]

        self.assertEqual(card["status_label"], "等待逾時待確認")
        self.assertEqual(card["status_class"], "attention")
        self.assertEqual(card["waiting_age_seconds"], 900)
        self.assertTrue(card["waiting_overdue"])
        self.assertIn("尚不代表工具失敗", card["waiting_reason"])
        self.assertEqual(card["pause_reason"], "")

    def test_admin_view_combines_queue_and_result_in_one_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            base_payload = {
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "trigger_type": "due",
                "item_kind": "出入",
                "item_title": "值退 / 值退｜27番 林宏為（隊員）",
                "target": "27番 林宏為（隊員）",
                "target_time": "18:00",
            }

            store.upsert_event(
                {
                    **base_payload,
                    "event_id": "evt-queue",
                    "occurred_at": "2026-06-18T18:00:00",
                    "record_type": "action_queued",
                    "status": "pending_write_automation",
                },
                now=datetime(2026, 6, 18, 18, 0),
            )
            store.upsert_event(
                {
                    **base_payload,
                    "target": "",
                    "event_id": "evt-result",
                    "occurred_at": "2026-06-18T18:00:22",
                    "record_type": "action_result",
                    "status": "submitted",
                },
                now=datetime(2026, 6, 18, 18, 0),
            )

            actions = store.read_day("2026-06-18")["admin_view"]["action_events"]

            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["record_label"], "到點勤務")
            self.assertEqual(
                actions[0]["item_title"],
                "18:00｜出入｜值退 / 值退｜27番 林宏為",
            )
            self.assertEqual(actions[0]["item_title"].count("27番 林宏為"), 1)
            self.assertNotIn("隊員", actions[0]["item_title"])
            self.assertEqual(actions[0]["status_label"], "已登打")
            self.assertEqual(actions[0]["started_at"], "2026-06-18T18:00:00")
            self.assertEqual(actions[0]["completed_at"], "2026-06-18T18:00:22")
            self.assertEqual([step["label"] for step in actions[0]["steps"]], ["開始送出", "完成結果"])
            self.assertEqual(actions[0]["pause_reason"], "")

    def test_admin_view_shows_waiting_when_queue_has_no_result(self):
        view = build_sinposmart_admin_view(
            [
                normalize_sinposmart_event(
                    {
                        "event_id": "evt-queue-only",
                        "occurred_at": "2026-06-18T17:00:00",
                        "record_type": "action_queued",
                        "status": "pending_write_automation",
                        "trigger_type": "due",
                        "item_kind": "工作",
                        "item_title": "在隊訓練｜戰術體能訓練",
                        "target": "1,5,6,8,10,11,15番",
                        "target_time": "17:00",
                    },
                    now=datetime(2026, 6, 18, 17, 0),
                )
            ],
            now=datetime(2026, 6, 18, 17, 0, 41),
        )

        self.assertEqual(len(view["action_events"]), 1)
        self.assertEqual(view["action_events"][0]["record_label"], "到點勤務")
        self.assertEqual(
            view["action_events"][0]["item_title"],
            "17:00｜工作｜在隊訓練｜戰術體能訓練｜1,5,6,8,10,11,15番",
        )
        self.assertEqual(view["action_events"][0]["status_label"], "等待完成回傳")
        self.assertEqual(view["action_events"][0]["status_class"], "running")
        self.assertEqual(view["action_events"][0]["started_at"], "2026-06-18T17:00:00")
        self.assertEqual(view["action_events"][0]["completed_at"], "")
        self.assertEqual(view["action_events"][0]["waiting_age_seconds"], 41)
        self.assertEqual(
            view["action_events"][0]["waiting_reason"],
            "已收到開始送出，已等待 41 秒，等待勤務系統完成結果回傳。",
        )
        self.assertFalse(view["action_events"][0]["waiting_overdue"])
        self.assertEqual(view["action_events"][0]["pause_reason"], "")
        self.assertNotEqual(view["action_events"][0]["status_label"], "pending_write_automation")

    def test_admin_view_marks_action_waiting_as_attention_without_calling_it_failed(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-queue-overdue",
                "occurred_at": "2026-06-18T17:00:00",
                "record_type": "action_queued",
                "status": "pending_write_automation",
                "trigger_type": "due",
                "item_kind": "工作",
                "item_title": "值班(宿)",
                "target_time": "17:00",
            },
            now=datetime(2026, 6, 18, 17, 0),
        )

        view = build_sinposmart_admin_view([event], now=datetime(2026, 6, 18, 17, 15))
        card = view["action_events"][0]

        self.assertEqual(card["status_label"], "等待逾時待確認")
        self.assertEqual(card["status_class"], "attention")
        self.assertEqual(card["waiting_age_seconds"], 900)
        self.assertTrue(card["waiting_overdue"])
        self.assertIn("尚不代表登打失敗", card["waiting_reason"])
        self.assertEqual(card["pause_reason"], "")
        self.assertEqual(view["summary"]["failed"], 0)

    def test_admin_view_merges_targetless_result_before_queued(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-result-before-queue",
                    "occurred_at": "2026-06-18T12:00:00",
                    "record_type": "action_result",
                    "status": "submitted",
                    "trigger_type": "due",
                    "item_kind": "工作",
                    "item_title": "值班(宿)",
                    "target": "",
                    "target_time": "12:00",
                },
                now=datetime(2026, 6, 18, 12, 0),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-queue-after-result",
                    "occurred_at": "2026-06-18T12:00:01",
                    "record_type": "action_queued",
                    "status": "pending_write_automation",
                    "trigger_type": "due",
                    "item_kind": "工作",
                    "item_title": "值班(宿)",
                    "target": "12番 王小明（隊員）",
                    "target_time": "12:00",
                },
                now=datetime(2026, 6, 18, 12, 0),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["action_events"]), 1)
        self.assertEqual(view["action_events"][0]["item_title"], "12:00｜工作｜值班(宿)｜12番 王小明")
        self.assertNotIn("隊員", view["action_events"][0]["item_title"])

    def test_admin_view_omits_empty_action_target_from_title(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-empty-target",
                "occurred_at": "2026-06-18T12:00:00",
                "record_type": "action_result",
                "status": "submitted",
                "trigger_type": "due",
                "item_kind": "工作",
                "item_title": "值班(宿)",
                "target": "",
                "target_time": "12:00",
            },
            now=datetime(2026, 6, 18, 12, 0),
        )

        view = build_sinposmart_admin_view([event])

        self.assertEqual(view["action_events"][0]["item_title"], "12:00｜工作｜值班(宿)")

    def test_admin_view_combines_manual_action_by_completion_key_when_times_differ(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-manual-queue",
                    "occurred_at": "2026-06-18T07:58:00",
                    "record_type": "action_queued",
                    "status": "manual_marked",
                    "trigger_type": "manual",
                    "item_kind": "工作",
                    "item_title": "救護返隊｜測試案件",
                    "target": "8番 曾彥綸（隊員）",
                    "target_time": "07:58",
                    "snapshot": {"completion_key": "work-log-case-1"},
                },
                now=datetime(2026, 6, 18, 7, 58),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-manual-result",
                    "occurred_at": "2026-06-18T08:00:00",
                    "record_type": "action_result",
                    "status": "submitted",
                    "trigger_type": "manual",
                    "item_kind": "工作",
                    "item_title": "救護返隊｜測試案件",
                    "target": "8番 曾彥綸（隊員）",
                    "target_time": "08:00",
                    "snapshot": {"completion_key": "work-log-case-1"},
                },
                now=datetime(2026, 6, 18, 8, 0),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["action_events"]), 1)
        self.assertEqual(view["action_events"][0]["started_at"], "2026-06-18T07:58:00")
        self.assertEqual(view["action_events"][0]["completed_at"], "2026-06-18T08:00:00")
        self.assertEqual(view["action_events"][0]["status_label"], "已登打")

    def test_admin_view_failed_action_shows_pause_reason(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-failed-result",
                "occurred_at": "2026-06-18T18:00:22",
                "record_type": "action_result",
                "status": "failed",
                "error": "找不到出入欄位",
                "trigger_type": "due",
                "item_kind": "出入",
                "item_title": "值退 / 值退｜27 林宏為",
                "target": "27番 林宏為（隊員）",
                "target_time": "18:00",
            },
            now=datetime(2026, 6, 18, 18, 0),
        )

        view = build_sinposmart_admin_view([event])

        self.assertEqual(view["action_events"][0]["status_label"], "失敗")
        self.assertEqual(view["action_events"][0]["pause_reason"], "找不到出入欄位")

    def test_admin_view_uses_later_success_after_failed_retry(self):
        completion_key = "entry:2026-08-14:18:in:27:返隊:防溺車巡繼領取氣瓶"
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-retry-queued",
                    "occurred_at": "2026-08-14T18:00:00",
                    "record_type": "action_queued",
                    "status": "pending_write_automation",
                    "trigger_type": "due",
                    "item_kind": "出入",
                    "item_title": "入 / 返隊",
                    "target": "27番 林宏為",
                    "target_time": "18:00",
                    "snapshot": {"completion_key": completion_key},
                },
                now=datetime(2026, 8, 14, 18, 0),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-retry-failed",
                    "occurred_at": "2026-08-14T18:00:13",
                    "record_type": "action_result",
                    "status": "failed",
                    "error": "第一次登打失敗",
                    "trigger_type": "due",
                    "item_kind": "出入",
                    "item_title": "入 / 返隊",
                    "target": "27番 林宏為",
                    "target_time": "18:00",
                    "snapshot": {"completion_key": completion_key},
                },
                now=datetime(2026, 8, 14, 18, 0, 13),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-retry-submitted",
                    "occurred_at": "2026-08-14T18:01:39",
                    "record_type": "action_result",
                    "status": "submitted",
                    "trigger_type": "due",
                    "item_kind": "出入",
                    "item_title": "入 / 返隊",
                    "target": "27番 林宏為",
                    "target_time": "18:00",
                    "snapshot": {"completion_key": completion_key},
                },
                now=datetime(2026, 8, 14, 18, 1, 39),
            ),
        ]

        view = build_sinposmart_admin_view(events)
        card = view["action_events"][0]

        self.assertEqual(len(view["action_events"]), 1)
        self.assertEqual(card["status_label"], "已登打")
        self.assertEqual(card["status_class"], "complete")
        self.assertEqual(card["completed_at"], "2026-08-14T18:01:39")
        self.assertEqual(card["pause_reason"], "")

    def test_admin_view_preserves_submission_after_relogin_existing_check(self):
        now = datetime(2026, 9, 5, 8, 3)
        base = {
            "record_type": "action_result",
            "trigger_type": "due",
            "actor_no": "8",
            "display_name": "8番 測試員",
            "item_kind": "工作",
            "item_title": "值班交接",
            "target_time": "08:00",
            "snapshot": {"completion_key": "work:2026-09-05:8:值班交接:8"},
        }
        submitted = {
            **base,
            "event_id": "evt-original-submission",
            "occurred_at": "2026-09-05T08:00:26",
            "status": "submitted",
            "result_ref": "original_submission.json",
        }
        existing = {
            **base,
            "event_id": "evt-relogin-existing",
            "occurred_at": "2026-09-05T08:02:14",
            "actor_no": "15",
            "display_name": "15番 測試員",
            "status": "skipped_duplicate",
        }
        for order in ((submitted, existing), (existing, submitted)):
            with self.subTest(first=order[0]["status"]), tempfile.TemporaryDirectory() as tmp:
                store = SinpoSmartBackendStore(Path(tmp))
                for event in order:
                    store.upsert_event(event, now=now)

                day = store.read_day("2026-09-05", now=now)
                view = day["admin_view"]
                card = view["action_events"][0]

                self.assertEqual(len(day["events"]), 2)
                self.assertEqual({event["status"] for event in day["events"]}, {"submitted", "skipped_duplicate"})
                self.assertEqual(len(view["action_events"]), 1)
                self.assertEqual(card["status_label"], "已登打")
                self.assertEqual(card["completed_at"], submitted["occurred_at"])
                self.assertEqual(card["person_label"], "8番 測試員")
                self.assertEqual(card["event_id"], submitted["event_id"])
                source = next(event for event in day["events"] if event["event_id"] == card["event_id"])
                self.assertEqual(source["result_ref"], "original_submission.json")
                self.assertEqual(view["summary"]["submitted"], 1)
                self.assertEqual(view["summary"]["existing"], 0)

    def test_admin_view_existing_check_does_not_invent_submission_or_hide_failure(self):
        now = datetime(2026, 9, 5, 8, 3)
        for statuses, expected in (
            (("skipped_duplicate",), "已存在"),
            (("skipped_duplicate", "submitted"), "已登打"),
            (("submitted", "failed"), "失敗"),
            (("failed", "skipped_duplicate"), "已存在"),
        ):
            with self.subTest(statuses=statuses):
                events = [
                    normalize_sinposmart_event(
                        {
                            "event_id": f"evt-state-{index}",
                            "occurred_at": f"2026-09-05T08:0{index}:26",
                            "record_type": "action_result",
                            "status": status,
                            "snapshot": {"completion_key": "same-duty-action"},
                        },
                        now=now,
                    )
                    for index, status in enumerate(statuses)
                ]

                view = build_sinposmart_admin_view(events, now=now)

                self.assertEqual(len(view["action_events"]), 1)
                self.assertEqual(view["action_events"][0]["status_label"], expected)

    def test_admin_view_closes_prior_day_queues_from_actual_next_day_results_without_writes(self):
        now = datetime(2026, 10, 16, 9, 0)
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            submitted_ids = {}
            for target, stale_second, completed_second in ((16, 2, 27), (26, 4, 34)):
                base = {"actor_no": "17", "trigger_type": "due", "item_kind": "出入", "item_title": "到勤",
                        "target": f"{target}番", "target_time": "07:55",
                        "snapshot": {"completion_key": f"entry:2026-10-16:755:in:{target}:到勤"}}
                for suffix, record_type, occurred_at, status in (
                    ("queued", "action_queued", "2026-10-16T07:59:09", "pending_write_automation"),
                    ("stale", "action_result", f"2026-10-16T08:00:{stale_second:02d}", "skipped_stale_schedule"),
                    ("retry", "action_queued", "2026-10-16T08:01:24", "pending_write_automation"),
                    ("submitted", "action_result", f"2026-10-16T08:01:{completed_second}", "submitted"),
                ):
                    event = store.upsert_event({**base, "event_id": f"arrival-{target}-{suffix}", "record_type": record_type,
                        "occurred_at": occurred_at, "status": status, "result_ref": f"arrival-{target}.json" if suffix == "submitted" else ""}, now=now)
                    if suffix == "submitted":
                        submitted_ids[target] = event["event_id"]
            original_files = {path: path.read_bytes() for path in Path(tmp).glob("*.json")}
            day = store.read_day("2026-10-15", now=now)
            listed_day = next(item for item in store.list_days(now=now) if item["fire_day"] == "2026-10-15")
            for payload in (day, listed_day):
                view = payload["admin_view"]
                self.assertEqual(view["summary"]["actions"], 2)
                self.assertEqual(view["summary"]["submitted"], 2)
                self.assertEqual(view["summary"]["attention"], 0)
                self.assertEqual(view["summary"]["waiting"], 0)
                self.assertEqual({event["record_type"] for event in payload["events"]}, {"action_queued"})
                self.assertEqual({event["fire_day"] for event in payload["events"]}, {"2026-10-15"})
                for target, second in ((16, 27), (26, 34)):
                    card = next(card for card in view["action_events"] if card["event_id"] == submitted_ids[target])
                    self.assertEqual(card["status_label"], "已登打")
                    self.assertEqual(card["started_at"], "2026-10-16T07:59:09")
                    self.assertEqual(card["completed_at"], f"2026-10-16T08:01:{second}")
                    self.assertEqual(card["completion_source_fire_day"], "2026-10-16")
            next_day = store.read_day("2026-10-16", now=now)
            self.assertEqual(next_day["admin_view"]["summary"]["submitted"], 2)
            for card in next_day["admin_view"]["action_events"]:
                self.assertEqual(card["started_at"], "2026-10-16T08:01:24")
                self.assertEqual(card["completion_source_fire_day"], "")
            self.assertEqual({path: path.read_bytes() for path in Path(tmp).glob("*.json")}, original_files)

    def test_admin_view_borrows_next_day_terminal_results_with_existing_retry_priority(self):
        now = datetime(2026, 10, 16, 9, 0)
        queue = {"event_id": "queue", "record_type": "action_queued", "occurred_at": "2026-10-16T07:59:09",
                 "status": "pending_write_automation", "snapshot": {"completion_key": "entry:2026-10-16:755:in:16:到勤"}}
        for statuses, expected, expected_index in (
            (("submitted", "failed"), "失敗", 1),
            (("failed", "submitted"), "已登打", 1),
            (("submitted", "skipped_duplicate"), "已登打", 0),
            (("skipped_duplicate",), "已存在", 0),
            (("failed",), "失敗", 0),
            (("skipped_stale_schedule", "pending_write_automation", "submitted"), "已登打", 2),
        ):
            candidates = [{**queue, "event_id": f"result-{index}", "record_type": "action_result",
                           "occurred_at": f"2026-10-16T08:01:{index:02d}", "status": status}
                          for index, status in enumerate(statuses)]
            for related in (candidates, list(reversed(candidates))):
                with self.subTest(statuses=statuses, order=[event["event_id"] for event in related]), tempfile.TemporaryDirectory() as tmp:
                    store = SinpoSmartBackendStore(Path(tmp))
                    for event in [queue, *related]:
                        store.upsert_event(event, now=now)
                    view = store.read_day("2026-10-15", now=now)["admin_view"]
                    self.assertEqual(len(view["action_events"]), 1)
                    card = view["action_events"][0]
                    self.assertEqual(card["status_label"], expected)
                    self.assertEqual(card["event_id"], f"result-{expected_index}")
                    self.assertEqual(card["completion_source_fire_day"], "2026-10-16")
                    self.assertEqual(view["summary"]["submitted"], int(expected == "已登打"))

    def test_admin_view_next_day_terminal_order_matches_same_day_priority(self):
        now = datetime(2026, 10, 16, 9, 0)
        queue = {"event_id": "queue", "record_type": "action_queued", "occurred_at": "2026-10-16T07:59:09",
                 "status": "pending_write_automation", "snapshot": {"completion_key": "entry:2026-10-16:755:in:16:到勤"}}
        for statuses, expected in ((("submitted", "failed", "skipped_duplicate"), "已存在"),
                                   (("submitted", "skipped_duplicate", "failed"), "失敗"),
                                   (("failed", "submitted", "skipped_duplicate"), "已登打")):
            candidates = [normalize_sinposmart_event({**queue, "event_id": f"result-{index}", "record_type": "action_result",
                "occurred_at": f"2026-10-16T08:0{index + 1}:00", "status": status}, now=now) for index, status in enumerate(statuses)]
            for ordered in permutations(candidates):
                with self.subTest(statuses=statuses, order=[event["event_id"] for event in ordered]):
                    same_day = build_sinposmart_admin_view([
                        normalize_sinposmart_event({**queue, "occurred_at": "2026-10-16T08:00:00"}, now=now), *ordered], now=now)["action_events"][0]
                    borrowed = build_sinposmart_admin_view([normalize_sinposmart_event(queue, now=now)],
                        related_results=list(ordered), now=now)["action_events"][0]
                    self.assertEqual(same_day["status_label"], expected)
                    self.assertEqual(borrowed["status_label"], same_day["status_label"])
                    self.assertEqual(borrowed["event_id"], same_day["event_id"])
                    self.assertEqual(borrowed["completed_at"], same_day["completed_at"])
                    self.assertEqual(borrowed["completion_source_fire_day"], "2026-10-16")
                    self.assertEqual(same_day["completion_source_fire_day"], "")

    def test_admin_view_next_day_completion_rejects_weak_or_unrelated_evidence(self):
        now = datetime(2026, 10, 16, 9, 0)
        queue = {"event_id": "queue", "record_type": "action_queued", "occurred_at": "2026-10-16T07:59:09",
                 "status": "pending_write_automation", "snapshot": {"completion_key": "entry:2026-10-16:755:in:16:到勤"}}
        result = {**queue, "event_id": "result", "record_type": "action_result", "occurred_at": "2026-10-16T08:01:27", "status": "submitted"}
        for queue_changes, result_changes in (
            ({"snapshot": {}}, {}), ({}, {"snapshot": {}}),
            ({}, {"snapshot": {"completion_key": "entry:2026-10-16:755:in:26:到勤"}}),
            ({}, {"snapshot": {"completion_key": "entry:2026-10-15:755:in:16:到勤"}}),
            ({}, {"fire_day": "2026-10-17"}), ({}, {"fire_day": "2026-10-15"}),
            ({}, {"occurred_at": "2026-10-16T07:58:00", "fire_day": "2026-10-16"}),
            ({}, {"record_type": "comparison_snapshot"}), ({}, {"status": "skipped_stale_schedule"}),
            ({}, {"status": "pending_write_automation"}),
            ({}, {"status": "not_submitted"}), ({}, {"status": "broken"}), ({}, {"status": "cancelled_ok"}),
        ):
            with self.subTest(queue=queue_changes, related=result_changes):
                view = build_sinposmart_admin_view([normalize_sinposmart_event({**queue, **queue_changes}, now=now)],
                    related_results=[normalize_sinposmart_event({**result, **result_changes}, now=now)], now=now)
                self.assertEqual(len(view["action_events"]), 1)
                self.assertEqual(view["action_events"][0]["status_label"], "等待完成回傳")
                self.assertEqual(view["action_events"][0]["completion_source_fire_day"], "")
        normalized_queue = normalize_sinposmart_event(queue, now=now)
        normalized_result = normalize_sinposmart_event(result, now=now)
        for changes in ({"snapshot": {**normalized_result["snapshot"], "completion_key_sha256": "f" * 64}},
                        {"fire_day": "invalid"}, {"occurred_at": "invalid", "last_occurred_at": "invalid"}):
            with self.subTest(invalid=changes):
                view = build_sinposmart_admin_view([normalized_queue], related_results=[{**normalized_result, **changes}], now=now)
                self.assertEqual(view["action_events"][0]["status_label"], "等待完成回傳")
        empty = build_sinposmart_admin_view([], related_results=[normalize_sinposmart_event(result, now=now)], now=now)
        self.assertEqual(empty["action_events"], [])

    def test_admin_view_next_day_completion_preserves_every_local_result(self):
        now = datetime(2026, 10, 16, 9, 0)
        queue = {"event_id": "queue", "record_type": "action_queued", "occurred_at": "2026-10-16T07:59:09",
                 "status": "pending_write_automation", "snapshot": {"completion_key": "entry:2026-10-16:755:in:16:到勤"}}
        for status in ("failed", "submitted", "skipped_duplicate", "skipped_stale_schedule", "pending_write_automation"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                store = SinpoSmartBackendStore(Path(tmp))
                for event in (queue, {**queue, "event_id": "local-result", "record_type": "action_result", "status": status,
                                    "occurred_at": "2026-10-16T07:59:20"},
                              {**queue, "event_id": "next-day-result", "record_type": "action_result", "status": "submitted",
                               "occurred_at": "2026-10-16T08:01:27"}):
                    store.upsert_event(event, now=now)
                card = store.read_day("2026-10-15", now=now)["admin_view"]["action_events"][0]
                self.assertEqual(card["event_id"], "local-result")
                self.assertEqual(card["completion_source_fire_day"], "")

    def test_admin_view_completion_source_requires_queue_and_two_valid_different_days(self):
        now = datetime(2026, 10, 16, 9, 0)
        queue = {"event_id": "queue", "record_type": "action_queued", "occurred_at": "2026-10-16T07:59:09",
                 "snapshot": {"completion_key": "stable"}}
        result = {**queue, "event_id": "result", "record_type": "action_result", "occurred_at": "2026-10-16T08:01:27", "status": "submitted"}
        for inputs, expected in (([queue, result], "2026-10-16"), ([result], ""), ([queue], ""),
                                 ([queue, {**result, "fire_day": "2026-10-15"}], "")):
            with self.subTest(expected=expected, count=len(inputs)):
                events = [normalize_sinposmart_event(event, now=now) for event in inputs]
                card = build_sinposmart_admin_view(events, now=now)["action_events"][0]
                self.assertEqual(card["completion_source_fire_day"], expected)
        invalid = [normalize_sinposmart_event(event, now=now) for event in (queue, result)]
        invalid[1]["fire_day"] = "invalid"
        self.assertEqual(build_sinposmart_admin_view(invalid, now=now)["action_events"][0]["completion_source_fire_day"], "")

    def test_admin_view_finds_submission_before_fire_day_boundary_without_moving_events(self):
        now = datetime(2026, 9, 5, 8, 3)
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            submitted = store.upsert_event(
                {
                    "event_id": "evt-before-boundary",
                    "occurred_at": "2026-09-05T07:55:22",
                    "record_type": "action_result",
                    "status": "submitted",
                    "actor_no": "8",
                    "display_name": "8番 測試員",
                    "result_ref": "original_arrival.json",
                    "snapshot": {"completion_key": "entry:2026-09-05:755:in:5:到勤"},
                },
                now=now,
            )
            for suffix, key in (
                ("matching", "entry:2026-09-05:755:in:5:到勤"),
                ("other-target", "entry:2026-09-05:755:in:15:到勤"),
                ("other-date", "entry:2026-09-04:755:in:5:到勤"),
            ):
                store.upsert_event(
                    {
                        "event_id": f"evt-recheck-{suffix}",
                        "occurred_at": "2026-09-05T08:02:15",
                        "record_type": "action_result",
                        "status": "skipped_duplicate",
                        "actor_no": "15",
                        "content": suffix,
                        "snapshot": {"completion_key": key},
                    },
                    now=now,
                )
            original_files = {path: path.read_bytes() for path in Path(tmp).glob("*.json")}

            day = store.read_day("2026-09-05", now=now)
            listed_day = store.list_days(limit=1, now=now)[0]

            for payload in (day, listed_day):
                view = payload["admin_view"]
                self.assertEqual(view["summary"]["actions"], 3)
                self.assertEqual(view["summary"]["submitted"], 1)
                self.assertEqual(view["summary"]["existing"], 2)
                card = next(card for card in view["action_events"] if card["status_label"] == "已登打")
                self.assertEqual(card["event_id"], submitted["event_id"])
                self.assertEqual(card["completed_at"], "2026-09-05T07:55:22")
                self.assertEqual(card["person_label"], "8番 測試員")
                self.assertEqual({event["status"] for event in payload["events"]}, {"skipped_duplicate"})
                self.assertEqual({event["fire_day"] for event in payload["events"]}, {"2026-09-05"})
            self.assertEqual(store.read_day("2026-09-04", now=now)["events"], [submitted])
            self.assertEqual({path: path.read_bytes() for path in original_files}, original_files)

    def test_admin_view_cross_day_evidence_requires_prior_success_and_exact_task_key(self):
        now = datetime(2026, 9, 5, 8, 3)
        recheck = {
            "event_id": "evt-current-recheck",
            "occurred_at": "2026-09-05T08:02:15",
            "record_type": "action_result",
            "status": "skipped_duplicate",
            "snapshot": {"completion_key": "entry:2026-09-05:755:in:5:到勤"},
        }
        original = {
            **recheck,
            "event_id": "evt-historical-result",
            "occurred_at": "2026-09-05T07:55:22",
            "status": "submitted",
        }
        for current_changes, history_changes, expected in (
            ({}, {}, "已登打"),
            ({}, {"occurred_at": "2026-09-05T08:03:00"}, "已存在"),
            ({}, {"status": "failed"}, "已存在"),
            ({}, {"record_type": "tool_action_finished"}, "已存在"),
            ({}, {"snapshot": {"completion_key": "entry:2026-09-04:755:in:5:到勤"}}, "已存在"),
            ({"snapshot": {}}, {"snapshot": {}}, "已存在"),
            ({"status": "failed"}, {}, "失敗"),
        ):
            with self.subTest(current=current_changes, history=history_changes):
                view = build_sinposmart_admin_view(
                    [normalize_sinposmart_event({**recheck, **current_changes}, now=now)],
                    related_results=[normalize_sinposmart_event({**original, **history_changes}, now=now)],
                    now=now,
                )

                self.assertEqual(view["summary"]["actions"], 1)
                self.assertEqual(view["action_events"][0]["status_label"], expected)

    def test_admin_view_cross_day_evidence_does_not_match_truncated_completion_key(self):
        now = datetime(2026, 9, 5, 8, 3)
        shared_prefix = "entry:" + "x" * 1200
        recheck = normalize_sinposmart_event(
            {
                "event_id": "evt-long-recheck",
                "occurred_at": "2026-09-05T08:02:15",
                "record_type": "action_result",
                "status": "skipped_duplicate",
                "snapshot": {"completion_key": f"{shared_prefix}:current"},
            },
            now=now,
        )
        unrelated_submission = normalize_sinposmart_event(
            {
                "event_id": "evt-long-unrelated-submission",
                "occurred_at": "2026-09-05T07:55:22",
                "record_type": "action_result",
                "status": "submitted",
                "snapshot": {"completion_key": f"{shared_prefix}:other"},
            },
            now=now,
        )

        view = build_sinposmart_admin_view([recheck], related_results=[unrelated_submission], now=now)

        self.assertEqual(view["summary"]["actions"], 1)
        self.assertEqual(view["action_events"][0]["status_label"], "已存在")

    def test_admin_view_cross_day_evidence_does_not_match_redacted_completion_key(self):
        now = datetime(2026, 9, 5, 8, 3)
        recheck = normalize_sinposmart_event(
            {
                "event_id": "evt-redacted-recheck",
                "occurred_at": "2026-09-05T08:02:15",
                "record_type": "action_result",
                "status": "skipped_duplicate",
                "snapshot": {"completion_key": "entry:token=current"},
            },
            now=now,
        )
        unrelated_submission = normalize_sinposmart_event(
            {
                "event_id": "evt-redacted-unrelated-submission",
                "occurred_at": "2026-09-05T07:55:22",
                "record_type": "action_result",
                "status": "submitted",
                "snapshot": {"completion_key": "entry:token=other"},
            },
            now=now,
        )

        self.assertEqual(
            recheck["snapshot"]["completion_key"],
            unrelated_submission["snapshot"]["completion_key"],
        )
        self.assertNotEqual(
            recheck["snapshot"]["completion_key_sha256"],
            unrelated_submission["snapshot"]["completion_key_sha256"],
        )
        view = build_sinposmart_admin_view([recheck], related_results=[unrelated_submission], now=now)

        self.assertEqual(view["summary"]["actions"], 1)
        self.assertEqual(view["action_events"][0]["status_label"], "已存在")

    def test_admin_view_keeps_same_time_duty_items_separate(self):
        events = []
        for index, (item_kind, title, target) in enumerate(
            [
                ("出入", "值退 / 值退｜27 林宏為", "27番 林宏為（隊員）"),
                ("出入", "值班 / 值班｜05 張鴻志", "5番 張鴻志（小隊長）"),
                ("工作", "值班(宿)｜27 林宏為", "27番 林宏為（隊員）"),
            ]
        ):
            events.append(
                normalize_sinposmart_event(
                    {
                        "event_id": f"evt-action-{index}",
                        "occurred_at": f"2026-06-18T18:00:{index + 20:02d}",
                        "record_type": "action_result",
                        "status": "submitted",
                        "trigger_type": "due",
                        "item_kind": item_kind,
                        "item_title": title,
                        "target": target,
                        "target_time": "18:00",
                    },
                    now=datetime(2026, 6, 18, 18, 0),
                )
            )

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["action_events"]), 3)
        self.assertEqual({event["item_kind"] for event in view["action_events"]}, {"出入", "工作"})

    def test_admin_view_keeps_only_latest_background_update_summary(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-schedule-old",
                    "occurred_at": "2026-06-18T16:31:12",
                    "record_type": "schedule_snapshot",
                    "status": "success",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                    "snapshot": {"raw": "old"},
                },
                now=datetime(2026, 6, 18, 16, 31),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-schedule-new",
                    "occurred_at": "2026-06-18T18:00:33",
                    "record_type": "schedule_snapshot",
                    "status": "success",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                    "snapshot": {"raw": "new"},
                },
                now=datetime(2026, 6, 18, 18, 0),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["background_updates"]), 1)
        self.assertEqual(view["background_updates"][0]["last_occurred_at"], "2026-06-18T18:00:33")
        self.assertNotIn("snapshot", view["background_updates"][0])

    def test_admin_view_keeps_non_login_error_in_background_updates(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-schedule-error",
                "occurred_at": "2026-06-18T18:10:00",
                "record_type": "error",
                "trigger_type": "schedule",
                "status": "failed",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "item_title": "勤務表背景更新",
                "error": "勤務表背景更新失敗",
            },
            now=datetime(2026, 6, 18, 18, 10),
        )

        view = build_sinposmart_admin_view([event])

        self.assertEqual(len(view["background_updates"]), 1)
        card = view["background_updates"][0]
        self.assertEqual(card["status_label"], "失敗")
        self.assertEqual(card["status_class"], "failed")
        self.assertEqual(card["error"], "勤務表背景更新失敗")
        self.assertEqual(view["login_events"], [])

    def test_admin_view_splits_schedule_snapshots_by_fire_day_scope(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-schedule-days",
                    "occurred_at": "2026-06-18T22:00:33",
                    "fire_day": "2026-06-18",
                    "record_type": "schedule_snapshot",
                    "status": "success",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                    "snapshot": {
                        "days": [
                            {"target_date": "1150618", "action_count": 3},
                            {"target_date": "1150619", "action_count": 5},
                        ]
                    },
                },
                now=datetime(2026, 6, 18, 22, 0),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["background_updates"]), 2)
        titles = {event["item_title"] for event in view["background_updates"]}
        self.assertEqual(titles, {"當日整日勤務", "隔日整日勤務"})
        self.assertTrue(all("snapshot" not in event for event in view["background_updates"]))

    def test_login_method_labels_preserve_unknown_history_and_each_attempt(self):
        events = [
            normalize_sinposmart_event({
                "event_id": f"login-{index}", "record_type": "login", "actor_no": "10",
                "occurred_at": "2026-09-14T12:00:00", "status": "ok",
                "snapshot": {"login_method": method} if method is not None else {},
            })
            for index, method in enumerate(("automatic", "manual", None, "invalid"))
        ]
        cards = build_sinposmart_admin_view(events)["login_events"]
        by_id = {card["event_id"]: card for card in cards}
        self.assertEqual(len(cards), 4)
        self.assertEqual(by_id["login-0"]["login_method_label"], "自動登入")
        self.assertEqual(by_id["login-1"]["login_method_label"], "手動登入")
        self.assertEqual(by_id["login-2"]["login_method_label"], "方式未記錄")
        self.assertEqual(by_id["login-3"]["login_method_label"], "方式未記錄")
        self.assertNotIn("login_method", events[2]["snapshot"])
        failed = normalize_sinposmart_event({
            "event_id": "failed-auto", "record_type": "login_failed", "status": "failed",
            "snapshot": {"login_method": "automatic"},
        })
        card = build_sinposmart_admin_view([failed])["login_events"][0]
        self.assertEqual(card["login_method_label"], "自動登入")
        self.assertEqual(card["status_label"], "登入失敗")

    def test_admin_view_login_section_keeps_logout_event_status(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-login",
                    "occurred_at": "2026-06-18T16:30:40",
                    "record_type": "login",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為 - tyfd01027",
                },
                now=datetime(2026, 6, 18, 16, 30),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-logout",
                    "occurred_at": "2026-06-18T18:05:12",
                    "record_type": "logout",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                },
                now=datetime(2026, 6, 18, 18, 5),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["login_events"]), 2)
        self.assertEqual([event["record_label"] for event in view["login_events"]], ["登出", "登入"])
        self.assertEqual(view["login_events"][0]["status_label"], "登出")
        self.assertEqual(view["login_events"][0]["person_label"], "27番 隊員 林宏為")

    def test_admin_view_login_section_keeps_each_login_logout_event(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-login-first",
                    "occurred_at": "2026-06-18T16:30:40",
                    "record_type": "login",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                },
                now=datetime(2026, 6, 18, 16, 30),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-logout",
                    "occurred_at": "2026-06-18T18:05:12",
                    "record_type": "logout",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                },
                now=datetime(2026, 6, 18, 18, 5),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-login-second",
                    "occurred_at": "2026-06-18T18:08:30",
                    "record_type": "login",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                },
                now=datetime(2026, 6, 18, 18, 8),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["login_events"]), 3)
        self.assertEqual([event["record_label"] for event in view["login_events"]], ["登入", "登出", "登入"])
        self.assertEqual([event["last_occurred_at"] for event in view["login_events"]], ["2026-06-18T18:08:30", "2026-06-18T18:05:12", "2026-06-18T16:30:40"])

    def test_admin_view_login_expired_event_is_visible(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-login-expired",
                "occurred_at": "2026-06-18T18:20:00",
                "record_type": "login_expired",
                "status": "failed",
                "actor_no": "4",
                "display_name": "4番 隊員 測試",
                "error": "登入失效",
            },
            now=datetime(2026, 6, 18, 18, 20),
        )

        view = build_sinposmart_admin_view([event])

        self.assertEqual(event["record_type"], "login_expired")
        self.assertEqual(len(view["login_events"]), 1)
        self.assertEqual(view["login_events"][0]["record_label"], "登入失效")
        self.assertEqual(view["login_events"][0]["status_label"], "登入失效")
        self.assertEqual(view["login_events"][0]["status_class"], "failed")

    def test_admin_view_legacy_login_error_event_is_visible(self):
        event = {
            "event_id": "evt-legacy-login-expired",
            "occurred_at": "2026-06-18T18:20:00",
            "last_occurred_at": "2026-06-18T18:20:00",
            "record_type": "error",
            "trigger_type": "login",
            "status": "failed",
            "actor_no": "4",
            "display_name": "4番 隊員 測試",
            "error": "勤務系統登入失效，請重新登入。",
        }

        view = build_sinposmart_admin_view([event])

        self.assertEqual(len(view["login_events"]), 1)
        self.assertEqual(view["login_events"][0]["record_label"], "登入失效")
        self.assertEqual(view["login_events"][0]["status_label"], "登入失效")
        self.assertEqual(view["login_events"][0]["status_class"], "failed")

    def test_admin_view_login_cards_keep_their_own_event_time(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-login-time",
                    "occurred_at": "2026-06-18T16:30:40",
                    "record_type": "login",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為 - tyfd01027",
                },
                now=datetime(2026, 6, 18, 16, 30),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-logout-time",
                    "occurred_at": "2026-06-18T18:05:12",
                    "record_type": "logout",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                },
                now=datetime(2026, 6, 18, 18, 5),
            ),
        ]

        view = build_sinposmart_admin_view(events)
        logout_card = view["login_events"][0]
        login_card = view["login_events"][1]

        self.assertEqual(login_card["login_at"], "2026-06-18T16:30:40")
        self.assertEqual(login_card["logout_at"], "")
        self.assertEqual(logout_card["login_at"], "")
        self.assertEqual(logout_card["logout_at"], "2026-06-18T18:05:12")
        self.assertEqual([step["label"] for step in login_card["steps"]], ["登入時間"])
        self.assertEqual([step["label"] for step in logout_card["steps"]], ["登出時間"])

    def test_admin_view_login_section_prefers_known_person_name(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-login-account",
                    "occurred_at": "2026-06-18T11:08:39",
                    "record_type": "login",
                    "status": "ok",
                    "actor_no": "8",
                    "display_name": "8番 tyfd01510",
                },
                now=datetime(2026, 6, 18, 11, 8),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-login-name",
                    "occurred_at": "2026-06-18T10:47:28",
                    "record_type": "login",
                    "status": "ok",
                    "actor_no": "8",
                    "display_name": "8番 隊員 曾彥綸",
                },
                now=datetime(2026, 6, 18, 10, 47),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["login_events"]), 2)
        self.assertEqual([event["person_label"] for event in view["login_events"]], ["8番 隊員 曾彥綸", "8番 隊員 曾彥綸"])

    def test_admin_view_does_not_surface_unknown_english_status(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-unknown-status",
                "occurred_at": "2026-06-18T18:00:00",
                "record_type": "action_result",
                "status": "queued_for_worker",
                "item_kind": "工作",
                "item_title": "值班交接",
                "target": "27番 林宏為（隊員）",
                "target_time": "18:00",
            },
            now=datetime(2026, 6, 18, 18, 0),
        )

        view = build_sinposmart_admin_view([event])

        self.assertEqual(view["action_events"][0]["status_label"], "等待登打")
        self.assertNotIn("queued_for_worker", view["action_events"][0].values())

    def test_admin_view_keeps_only_active_unreturned_return_records(self):
        def event(event_id, occurred_at, status, queue_id, *, retry_minutes):
            return normalize_sinposmart_event(
                {
                    "event_id": event_id,
                    "occurred_at": occurred_at,
                    "record_type": "unreturned_return",
                    "trigger_type": "recovery",
                    "status": status,
                    "item_kind": "出入",
                    "item_title": "出 / 退勤",
                    "target": "08 測試員",
                    "target_time": "08:05",
                    "snapshot": {
                        "queue_id": queue_id,
                        "first_paused_at": "2026-08-07T08:00:00",
                        "last_attempt_at": occurred_at,
                        "next_retry_at": "2026-08-07T08:15:00",
                        "expires_at": "2026-08-08T02:00:00",
                        "last_owner_actor_no": "11",
                        "retry_interval_minutes": retry_minutes,
                    },
                },
                now=datetime(2026, 8, 7, 8, 0),
            )

        view = build_sinposmart_admin_view(
            [
                event("evt-paused", "2026-08-07T08:00:00", "pending", "queue-resolved", retry_minutes=5),
                event("evt-resolved", "2026-08-07T08:06:00", "resolved", "queue-resolved", retry_minutes=5),
                event("evt-retrying", "2026-08-07T08:10:00", "retrying", "queue-active", retry_minutes=10),
            ]
        )

        self.assertEqual(view["summary"]["unreturned_returns"], 1)
        self.assertEqual(len(view["unreturned_return_events"]), 1)
        card = view["unreturned_return_events"][0]
        self.assertEqual(card["target"], "08 測試員")
        self.assertEqual(card["item_title"], "08:05｜出入｜出 / 退勤｜08番 測試員")
        self.assertEqual(card["status_label"], "重新確認中")
        self.assertEqual(card["retry_interval_minutes"], 10)
        self.assertEqual(card["owner_actor_no"], "11")
        self.assertTrue(view["unreturned_return_history_events"][0]["is_active"])
        self.assertFalse(view["unreturned_return_history_events"][0]["has_handoff_context"])
        self.assertFalse(view["unreturned_return_history_events"][0]["missing_task_details"])

    def test_admin_view_labels_legacy_unreturned_return_without_task_details(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-unreturned-legacy",
                "occurred_at": "2026-08-12T10:00:02",
                "record_type": "unreturned_return",
                "trigger_type": "due",
                "status": "resolved",
                "snapshot": {
                    "queue_id": "queue-legacy",
                    "first_paused_at": "2026-08-12T10:00:02",
                },
            },
            now=datetime(2026, 8, 12, 10, 0),
        )

        view = build_sinposmart_admin_view([event])

        card = view["unreturned_return_history_events"][0]
        self.assertEqual(card["item_title"], "10:00｜未返隊｜舊版事件未提供勤務明細")
        self.assertTrue(card["missing_task_details"])
        self.assertFalse(card["has_handoff_context"])

    def test_admin_view_marks_partial_legacy_task_details_as_missing(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-unreturned-partial",
                "occurred_at": "2026-08-12T10:00:02",
                "record_type": "unreturned_return",
                "trigger_type": "due",
                "status": "resolved",
                "target_time": "10:00",
                "target": "21",
                "snapshot": {"queue_id": "queue-partial"},
            },
            now=datetime(2026, 8, 12, 10, 0),
        )

        view = build_sinposmart_admin_view([event])

        card = view["unreturned_return_history_events"][0]
        self.assertEqual(card["item_title"], "10:00｜未返隊暫停｜21番")
        self.assertTrue(card["missing_task_details"])

    def test_admin_view_orders_active_unreturned_returns_by_next_retry(self):
        def event(event_id, status, first_paused_at, next_retry_at):
            return normalize_sinposmart_event(
                {
                    "event_id": event_id,
                    "occurred_at": first_paused_at,
                    "record_type": "unreturned_return",
                    "trigger_type": "recovery",
                    "status": status,
                    "item_kind": "出入",
                    "item_title": "入 / 返隊",
                    "target": "21 張家和",
                    "target_time": "20:01",
                    "snapshot": {
                        "queue_id": event_id,
                        "first_paused_at": first_paused_at,
                        "next_retry_at": next_retry_at,
                    },
                },
                now=datetime.fromisoformat(first_paused_at),
            )

        view = build_sinposmart_admin_view(
            [
                event("queue-later", "retrying", "2026-08-12T10:10:00", "2026-08-12T10:20:00"),
                event("queue-earlier", "pending", "2026-08-12T10:00:00", "2026-08-12T10:15:00"),
                event("queue-login", "pending", "2026-08-12T09:55:00", ""),
                event("queue-resolved", "resolved", "2026-08-12T11:00:00", ""),
            ]
        )

        cards = view["unreturned_return_history_events"]
        self.assertEqual([card["is_active"] for card in cards], [True, True, True, False])
        self.assertEqual(
            [card["next_retry_at"] for card in cards[:3]],
            ["", "2026-08-12T10:15:00", "2026-08-12T10:20:00"],
        )

    def test_admin_view_fills_handoff_people_from_later_snapshot(self):
        def event(event_id, occurred_at, snapshot):
            return normalize_sinposmart_event(
                {
                    "event_id": event_id,
                    "occurred_at": occurred_at,
                    "record_type": "unreturned_return",
                    "trigger_type": "recovery",
                    "status": "resolved",
                    "item_kind": "出入",
                    "item_title": "值班交接",
                    "target": "7 原值退",
                    "target_time": "10:00",
                    "snapshot": {"queue_id": "queue-handoff", "handoff": snapshot},
                },
                now=datetime.fromisoformat(occurred_at),
            )

        view = build_sinposmart_admin_view(
            [
                event(
                    "evt-handoff-first",
                    "2026-08-12T10:00:00",
                    {"original_handoff_time": "10:00"},
                ),
                event(
                    "evt-handoff-later",
                    "2026-08-12T10:05:00",
                    {
                        "outgoing_person": "7番 原值退",
                        "scheduled_incoming_person": "8番 表定接班",
                    },
                ),
            ]
        )

        card = view["unreturned_return_history_events"][0]
        self.assertEqual(card["outgoing_person"], "7番 原值退")
        self.assertEqual(card["scheduled_incoming_person"], "8番 表定接班")
        self.assertTrue(card["has_handoff_context"])


if __name__ == "__main__":
    unittest.main()
