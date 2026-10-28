from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from ambulance_bot.civilpower_records import (
    CivilpowerCaseWindow,
    CivilpowerIoRecord,
    CivilpowerRecordConflict,
    CivilpowerWorkRecord,
    find_civilpower_work_record,
    io_pair_for_work_record,
    plan_civilpower_io,
    volunteer_in_status,
)


class CivilpowerReconciliationTests(unittest.TestCase):
    def record(self, record_id, time, status, reason="", *, date="2026-10-28"):
        return CivilpowerIoRecord(
            record_id=record_id,
            at=datetime.fromisoformat(f"{date}T{time}"),
            serve_unit="新坡分隊",
            home_unit="大園救護分隊",
            name="測試義消",
            title="隊員",
            status=status,
            reason=reason,
        )

    def decide(self, records, *, start="2026-10-28T21:39:29", end="2026-10-28T22:28:47"):
        return plan_civilpower_io(
            records,
            CivilpowerCaseWindow("test-case", datetime.fromisoformat(start), datetime.fromisoformat(end)),
            name="測試義消",
            home_unit="大園救護分隊",
            serve_unit="新坡分隊",
            title="隊員",
        )

    def test_manual_pair_inside_official_case_window_is_reused_despite_different_minutes_and_reason(self):
        records = [
            self.record("duty", "19:08", "服勤", "到勤"),
            self.record("previous-out", "19:15", "出", "案件類別：緊急救護"),
            self.record("previous-in", "20:36", "入", "救護返隊"),
            self.record("manual-out", "21:40", "出", "救護出勤"),
            self.record("manual-in", "22:28", "入", "返隊"),
            self.record("next-out", "22:49", "出", "救護出勤"),
            self.record("next-in", "23:47", "入", "返隊"),
        ]

        decision = self.decide(records)

        self.assertEqual("reuse_pair", decision.action)
        self.assertEqual("manual-out", decision.out.record_id)
        self.assertEqual("manual-in", decision.inbound.record_id)

    def test_partial_manual_out_only_needs_a_missing_in(self):
        decision = self.decide([self.record("out", "21:40", "出", "協勤")])
        self.assertEqual("create_in", decision.action)
        self.assertEqual("out", decision.out.record_id)
        self.assertIsNone(decision.inbound)

    def test_new_pair_requires_confirmed_active_duty_and_no_overlapping_record(self):
        decision = self.decide([self.record("duty", "19:08", "服勤", "到勤")])
        self.assertEqual("create_pair", decision.action)
        for records in ([], [self.record("off", "21:00", "退勤")]):
            with self.subTest(records=records), self.assertRaises(CivilpowerRecordConflict):
                self.decide(records)

    def test_duplicate_manual_and_automated_outs_are_not_resolved_by_closest_minute(self):
        with self.assertRaisesRegex(CivilpowerRecordConflict, "多筆出勤"):
            self.decide([
                self.record("automated", "21:39", "出", "救護出勤"),
                self.record("manual", "21:40", "出", "出勤"),
                self.record("in", "22:28", "入", "返隊"),
            ])

    def test_out_before_dispatch_is_a_conflict_instead_of_permission_to_add_another(self):
        with self.assertRaisesRegex(CivilpowerRecordConflict, "跨越"):
            self.decide([
                self.record("out", "21:38", "出"),
                self.record("in", "22:28", "入"),
            ])

    def test_existing_in_after_official_return_is_preserved_and_stops_the_task(self):
        with self.assertRaisesRegex(CivilpowerRecordConflict, "超出正式案件"):
            self.decide([self.record("out", "21:40", "出"), self.record("in", "22:34", "入")])

    def test_orphan_in_and_intervening_off_duty_do_not_create_more_io_records(self):
        for records in (
            [self.record("in", "22:28", "入")],
            [self.record("out", "21:40", "出"), self.record("off", "22:00", "退勤")],
        ):
            with self.subTest(records=records), self.assertRaises(CivilpowerRecordConflict):
                self.decide(records)

    def test_two_return_records_inside_one_case_are_ambiguous(self):
        with self.assertRaisesRegex(CivilpowerRecordConflict, "多筆入勤"):
            self.decide([
                self.record("out", "21:40", "出"),
                self.record("in-one", "22:27", "入"),
                self.record("in-two", "22:28", "入"),
            ])

    def test_previous_case_can_return_after_dispatch_but_before_this_cases_actual_out(self):
        decision = self.decide([
            self.record("previous-out", "21:00", "出"),
            self.record("previous-in", "21:40", "入"),
            self.record("out", "21:41", "出"),
            self.record("in", "22:28", "入"),
        ])
        self.assertEqual("reuse_pair", decision.action)
        self.assertEqual("out", decision.out.record_id)

    def test_arrival_and_out_in_the_same_minute_do_not_hide_the_valid_pair(self):
        decision = self.decide([self.record("duty", "21:40", "服勤"), self.record("out", "21:40", "出"),
                                self.record("in", "22:28", "入")])
        self.assertEqual("reuse_pair", decision.action)

    def test_off_duty_before_out_is_a_conflict_without_a_new_arrival(self):
        with self.assertRaisesRegex(CivilpowerRecordConflict, "退勤"):
            self.decide([self.record("off", "21:00", "退勤"), self.record("out", "21:40", "出"),
                         self.record("in", "22:28", "入")])

    def test_an_unpaired_in_before_the_out_is_not_assumed_to_be_another_case(self):
        with self.assertRaises(CivilpowerRecordConflict):
            self.decide([self.record("in", "21:40", "入"), self.record("out", "21:41", "出")])

    def test_same_name_in_another_unit_cannot_supply_a_case_pair(self):
        other = replace(self.record("other", "21:40", "出"), home_unit="其他救護分隊")
        decision = self.decide([self.record("duty", "19:08", "服勤"), other])
        self.assertEqual("create_pair", decision.action)

    def test_roster_identity_conflict_does_not_look_like_missing_io(self):
        wrong_title = replace(self.record("out", "21:40", "出"), title="小隊長")
        with self.assertRaisesRegex(CivilpowerRecordConflict, "職稱"):
            self.decide([wrong_title])

    def test_missing_or_duplicate_record_ids_stop_reconciliation(self):
        out = self.record("same", "21:40", "出")
        for records in ([replace(out, record_id="")], [out, replace(out, at=datetime(2026, 10, 28, 22, 28), status="入")]):
            with self.subTest(records=records), self.assertRaisesRegex(CivilpowerRecordConflict, "識別"):
                self.decide(records)

    def test_cross_midnight_pair_uses_full_dates(self):
        records = [
            self.record("out", "23:51", "出"),
            self.record("in", "00:29", "入", date="2026-10-29"),
        ]
        decision = self.decide(records, start="2026-10-28T23:50:10", end="2026-10-29T00:30:20")
        self.assertEqual("reuse_pair", decision.action)

    def test_aware_utc_case_and_records_are_compared_in_taiwan_time(self):
        out = replace(self.record("out", "21:40", "出"), at=datetime(2026, 10, 28, 13, 40, tzinfo=timezone.utc))
        decision = self.decide([out, self.record("in", "22:28", "入")],
                               start="2026-10-28T13:39:29+00:00", end="2026-10-28T14:28:47+00:00")
        self.assertEqual("reuse_pair", decision.action)

    def test_return_seconds_cannot_precede_dispatch_within_the_same_minute(self):
        with self.assertRaisesRegex(CivilpowerRecordConflict, "早於"):
            CivilpowerCaseWindow("bad", datetime(2026, 10, 28, 21, 39, 50), datetime(2026, 10, 28, 21, 39, 20))


class CivilpowerWorkRecordTests(unittest.TestCase):
    def test_negated_participation_is_not_confirmed_from_the_members_name(self):
        self.assertFalse(volunteer_in_status("3.救護義消協勤:測試義消 未出勤", "測試義消"))
        self.assertFalse(volunteer_in_status("未參與救護義消協勤:測試義消", "測試義消"))
        self.assertTrue(volunteer_in_status("3.救護義消協勤:測試義消 4.其他事項取消", "測試義消"))

    def setUp(self):
        self.window = CivilpowerCaseWindow("20261028213929099", datetime(2026, 10, 28, 21, 39, 29), datetime(2026, 10, 28, 22, 28, 47))
        self.record = CivilpowerWorkRecord(
            record_id="work-1", at=datetime(2026, 10, 28, 21, 39), serve_unit="新坡分隊",
            volunteers=("測試義消",), summary="地點：桃園市測試路1號",
            handling="1.新坡92:測試甲 3.救護義消協勤:測試義消",
            official_return_at=self.window.return_at,
            io_out_at=datetime(2026, 10, 28, 21, 40), io_in_at=datetime(2026, 10, 28, 22, 28),
            kind="緊急救護",
        )

    def find(self, records):
        return find_civilpower_work_record(records, self.window, name="測試義消", serve_unit="新坡分隊",
                                         address="桃園市測試路1號", vehicle="新坡92")

    def test_existing_case_is_identified_by_typed_fields_and_its_actual_io_times(self):
        self.assertEqual(self.record, self.find([self.record]))

    def test_member_in_handling_text_only_cannot_confirm_volunteer_participation(self):
        self.assertIsNone(self.find([replace(self.record, volunteers=("其他義消",))]))

    def test_different_primary_event_time_is_not_matched_by_other_dates_in_the_row(self):
        self.assertIsNone(self.find([replace(self.record, at=datetime(2026, 10, 28, 20, 0),
                                            summary=self.record.summary + " 最後修改時間2026/10/28 21:39")]))

    def test_name_and_unit_are_exact(self):
        self.assertIsNone(self.find([replace(self.record, volunteers=("測試義消甲",))]))
        self.assertIsNone(self.find([replace(self.record, serve_unit="其他分隊")]))

    def test_duplicate_existing_work_records_require_confirmation(self):
        with self.assertRaisesRegex(CivilpowerRecordConflict, "多筆工作紀錄"):
            self.find([self.record, replace(self.record, record_id="work-2")])

    def test_existing_record_with_conflicting_official_return_is_not_treated_as_missing(self):
        with self.assertRaisesRegex(CivilpowerRecordConflict, "返隊時間"):
            self.find([replace(self.record, official_return_at=datetime(2026, 10, 28, 22, 34))])

    def test_case_id_conflicts_are_never_hidden_by_matching_address_and_time(self):
        self.assertIsNone(self.find([replace(self.record, case_id="another-case")]))

    def test_vehicle_numbers_are_not_substring_matches(self):
        self.assertIsNone(self.find([replace(self.record, handling="1.新坡192:測試甲")]))

    def test_matching_record_without_pair_evidence_cannot_trigger_another_add(self):
        with self.assertRaisesRegex(CivilpowerRecordConflict, "出入時間"):
            self.find([replace(self.record, io_out_at=None)])

    def test_declared_work_pair_is_separate_from_other_case_outs_in_a_long_case_window(self):
        def row(record_id, at, status):
            return CivilpowerIoRecord(record_id, at, "新坡分隊", "大園救護分隊", "測試義消", "隊員", status)

        rows = [row("earlier-out", self.window.start, "出"),
                row("manual-out", self.record.io_out_at, "出"),
                row("manual-in", self.record.io_in_at, "入")]
        result = io_pair_for_work_record(rows, self.window, self.record, name="測試義消",
                                         home_unit="大園救護分隊", serve_unit="新坡分隊", title="隊員")
        self.assertEqual("manual-out", result.out.record_id)
        self.assertEqual("manual-in", result.inbound.record_id)

    def test_two_io_ids_at_the_declared_work_out_time_remain_ambiguous(self):
        rows = [CivilpowerIoRecord(record_id, self.record.io_out_at, "新坡分隊", "大園救護分隊", "測試義消", "隊員", "出")
                for record_id in ("out-1", "out-2")]
        with self.assertRaises(CivilpowerRecordConflict):
            io_pair_for_work_record(rows, self.window, self.record, name="測試義消",
                                    home_unit="大園救護分隊", serve_unit="新坡分隊", title="隊員")

    def test_another_kind_of_work_is_not_accepted_as_the_ems_case(self):
        with self.assertRaisesRegex(CivilpowerRecordConflict, "服勤項目"):
            self.find([replace(self.record, kind="火災")])


if __name__ == "__main__":
    unittest.main()
