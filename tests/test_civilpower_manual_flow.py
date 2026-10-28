from __future__ import annotations

import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import civilpower
from ambulance_bot.civilpower_records import CivilpowerCaseWindow, CivilpowerIoRecord, CivilpowerWorkRecord
from tests import test_civilpower


class CivilpowerManualFlowTests(unittest.TestCase):
    def setUp(self):
        self.request = test_civilpower.CivilpowerPlanTests._enabled_request()
        self.plan = replace(civilpower.build_civilpower_task_plan(self.request), in_time="1433")
        self.window = CivilpowerCaseWindow(self.plan.case_id, datetime(2026, 8, 19, 12, 32, 45), datetime(2026, 8, 19, 14, 33, 40))
        self.out = CivilpowerIoRecord("101", datetime(2026, 8, 19, 12, 33), "新坡分隊", "大園救護分隊", "測試義消", "隊員", "出", "人工出勤", "原登打人")
        self.inbound = replace(self.out, record_id="102", at=datetime(2026, 8, 19, 14, 33), status="入", reason="返隊")
        self.work = CivilpowerWorkRecord("201", self.window.start, "新坡分隊", ("測試義消",), self.plan.case_address, "3.救護義消協勤:測試義消", self.window.return_at, self.out.at, self.inbound.at, kind="緊急救護")

    def context(self, *, work_records=()):
        stack = ExitStack()
        self.portal_io = [self.out, self.inbound]
        self.portal_work = list(work_records)
        stack.enter_context(mock.patch("civilpower.mark_driver_operation_active"))
        stack.enter_context(mock.patch("civilpower.capture_failure_artifacts", return_value={}))
        stack.enter_context(mock.patch("civilpower._resolve_official_case_plan", return_value=(self.plan, self.window), create=True))
        stack.enter_context(mock.patch("civilpower._query_civilpower_records", side_effect=lambda *args, **kwargs: list(self.portal_io) if kwargs["kind"] == "io" else list(self.portal_work)))
        stack.enter_context(mock.patch("civilpower._prepare_rejection_requery", create=True))
        self.io_save = stack.enter_context(mock.patch("civilpower._ensure_io_record"))
        stack.enter_context(mock.patch("civilpower._ensure_correct_in_io_record"))
        stack.enter_context(mock.patch("civilpower._ensure_work_log", return_value=self.plan))
        self.work_save = stack.enter_context(mock.patch("civilpower._submit_verified_work_log", return_value=self.work, create=True))
        return stack

    def test_manual_pair_is_preserved_and_only_missing_work_log_is_submitted(self):
        with TemporaryDirectory() as directory, self.context():
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_saved", result.status)
        self.io_save.assert_not_called()
        self.work_save.assert_called_once()
        self.assertIn("101", result.detail)
        self.assertIn("201", result.detail)

    def test_already_completed_manual_case_does_not_resubmit_any_form(self):
        with TemporaryDirectory() as directory, self.context(work_records=[self.work]):
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_saved", result.status)
        self.io_save.assert_not_called()
        self.work_save.assert_not_called()

    def test_existing_work_log_with_missing_io_stops_instead_of_recreating_records(self):
        with TemporaryDirectory() as directory, self.context(work_records=[self.work]):
            self.portal_io = []
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_failed", result.status)
        self.io_save.assert_not_called()
        self.work_save.assert_not_called()

    def test_duplicate_manual_and_automated_out_records_stop_all_submissions(self):
        with TemporaryDirectory() as directory, self.context():
            self.portal_io.append(replace(self.out, record_id="duplicate", at=self.window.start))
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_failed", result.status)
        self.assertIn("多筆出勤", result.detail)
        self.io_save.assert_not_called()
        self.work_save.assert_not_called()

    def test_partial_manual_out_creates_only_the_missing_in_with_official_time(self):
        with TemporaryDirectory() as directory, self.context():
            self.portal_io = [self.out]

            def save(_driver, plan, status, *_args, **_kwargs):
                self.assertEqual(civilpower.IN_STATUS, status)
                self.assertEqual("1433", plan.in_time)
                self.portal_io.append(self.inbound)
                return True

            self.io_save.side_effect = save
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_saved", result.status)
        self.assertEqual([civilpower.IN_STATUS], [call.args[2] for call in self.io_save.call_args_list])

    def test_no_existing_pair_creates_out_and_in_once_then_verifies_work_log(self):
        with TemporaryDirectory() as directory, self.context():
            self.portal_io = [replace(self.out, record_id="duty", at=datetime(2026, 8, 19, 9), status="服勤")]
            self.work_save.return_value = replace(self.work, io_out_at=self.window.start)

            def save(_driver, _plan, status, *_args, **_kwargs):
                row = replace(self.out, at=self.window.start) if status == civilpower.OUT_STATUS else self.inbound
                self.portal_io.append(row)
                return True

            self.io_save.side_effect = save
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_saved", result.status)
        self.assertEqual([civilpower.OUT_STATUS, civilpower.IN_STATUS], [call.args[2] for call in self.io_save.call_args_list])

    def test_manual_in_arriving_before_submit_is_reused_after_guard_cancels_the_write(self):
        with TemporaryDirectory() as directory, self.context():
            self.portal_io = [self.out]

            def cancel_stale_form(*_args, **_kwargs):
                self.portal_io.append(self.inbound)
                raise civilpower._CivilpowerRecordsChanged("人工已補返隊，取消尚未送出的表單")

            self.io_save.side_effect = cancel_stale_form
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_saved", result.status)
        self.assertEqual(1, self.io_save.call_count)
        self.work_save.assert_called_once()

    def test_official_case_query_failure_stops_before_any_save(self):
        with TemporaryDirectory() as directory, self.context(), mock.patch("civilpower._resolve_official_case_plan", side_effect=RuntimeError("正式時段尚無有效返隊")):
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_failed", result.status)
        self.io_save.assert_not_called()
        self.work_save.assert_not_called()

    def test_existing_work_record_disappearing_during_verification_is_not_reported_saved(self):
        with TemporaryDirectory() as directory, self.context(work_records=[self.work]):
            calls = 0

            def query(*_args, **kwargs):
                nonlocal calls
                if kwargs["kind"] == "io":
                    return list(self.portal_io)
                calls += 1
                return [self.work] if calls == 1 else []

            with mock.patch("civilpower._query_civilpower_records", side_effect=query):
                result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_failed", result.status)
        self.io_save.assert_not_called()
        self.work_save.assert_not_called()

    def test_old_success_checkpoint_does_not_replace_fresh_portal_verification(self):
        checkpoint = {"out_verified": True, "in_verified": True, "work_log_verified": True}
        with TemporaryDirectory() as directory, self.context(), mock.patch("civilpower._load_task_checkpoint", return_value=checkpoint):
            self.portal_io = []
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_failed", result.status)
        self.io_save.assert_not_called()
        self.work_save.assert_not_called()

    def test_in_rejected_after_manual_completion_only_rechecks_then_fills_work_log(self):
        with TemporaryDirectory() as directory, self.context():
            self.portal_io = [self.out]

            def rejected(*_args, **_kwargs):
                self.portal_io.append(self.inbound)
                raise civilpower._CivilpowerSaveRejected("尚未出勤，無法新增入勤資料")

            self.io_save.side_effect = rejected
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_saved", result.status)
        self.assertEqual(1, self.io_save.call_count)
        self.work_save.assert_called_once()

    def test_rejected_in_is_not_submitted_again_when_requery_still_finds_it_missing(self):
        with TemporaryDirectory() as directory, self.context():
            self.portal_io = [self.out]
            self.io_save.side_effect = civilpower._CivilpowerSaveRejected("網站不接受本次返隊")
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_failed", result.status)
        self.assertEqual(1, self.io_save.call_count)
        self.assertIn("網站不接受本次返隊", result.detail)
        self.work_save.assert_not_called()

    def test_work_save_rejected_after_manual_completion_is_verified_without_resubmission(self):
        with TemporaryDirectory() as directory, self.context():
            def rejected(*_args, **_kwargs):
                self.portal_work.append(self.work)
                raise civilpower._CivilpowerSaveRejected("相同工作紀錄已存在")

            self.work_save.side_effect = rejected
            result = civilpower.run_civilpower_task(self.request, Path(directory), driver=object())
        self.assertEqual("volunteer_assist_saved", result.status)
        self.work_save.assert_called_once()
        self.io_save.assert_not_called()
