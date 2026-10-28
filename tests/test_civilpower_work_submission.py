from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest import mock

import civilpower
from ambulance_bot.civilpower_records import CivilpowerIoDecision
from tests import test_civilpower_manual_flow


class CivilpowerWorkSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_civilpower_manual_flow.CivilpowerManualFlowTests()
        self.fixture.setUp()
        self.saved = False
        self.manual_completed = False
        self.controls = {
            "#txt_AddDisDate": "2026/08/19", "#txt_AddDisHour": "12", "#txt_AddDisMin": "32",
            "#txt_AddBackDate": "2026/08/19", "#txt_AddBackHour": "14", "#txt_AddBackMin": "33",
            "#txt_AddStat": "3.救護義消協勤:測試義消", "#hf_AddIOLogIDs": "101",
        }

    def context(self):
        stack = ExitStack()
        for name in ("_wait_visible", "_wait_for_work_log_add_controls", "_wait_after_save"):
            stack.enter_context(mock.patch("civilpower." + name))
        self.select = stack.enter_context(mock.patch("civilpower._select_out_io_record_for_work_log"))
        self.import_case = stack.enter_context(mock.patch("civilpower._import_work_log_case"))
        stack.enter_context(mock.patch("civilpower._control_value", side_effect=lambda _driver, selector: self.controls[selector]))
        stack.enter_context(mock.patch("civilpower._in_civilpower_query_tab", side_effect=lambda _driver, operation: operation()))
        stack.enter_context(mock.patch("civilpower._query_civilpower_records", side_effect=self.query))
        self.click = stack.enter_context(mock.patch("civilpower._click", side_effect=self.clicked))
        return stack

    def query(self, *_args, **kwargs):
        if kwargs["kind"] == "io":
            return [self.fixture.out, self.fixture.inbound]
        return [self.fixture.work] if self.saved or self.manual_completed else []

    def clicked(self, _wait, selector):
        if selector == "#btn_WorkLogAdd":
            self.saved = True

    def submit(self):
        f = self.fixture
        return civilpower._submit_verified_work_log(
            object(), f.request, f.plan, f.window, CivilpowerIoDecision("reuse_pair", f.out, f.inbound),
            [f.out, f.inbound], 1, {}, cancel_check=None, progress=None, save_checkpoint=mock.Mock(),
        )

    def test_work_log_selects_verified_io_id_and_preserves_manual_out_time(self):
        with self.context():
            work = self.submit()
        self.assertEqual(self.fixture.work, work)
        self.assertEqual("101", self.select.call_args.kwargs["record_id"])
        self.assertEqual("1233", self.select.call_args.args[2].out_time)
        self.assertEqual(1, sum(call.args[1] == "#btn_WorkLogAdd" for call in self.click.call_args_list))

    def test_another_person_completing_the_case_before_save_prevents_a_duplicate(self):
        with self.context():
            self.import_case.side_effect = lambda *_args: setattr(self, "manual_completed", True)
            work = self.submit()
        self.assertEqual(self.fixture.work, work)
        self.assertFalse(self.saved)

    def test_wrong_selected_id_dispatch_or_return_cannot_be_saved(self):
        for selector, value in (("#hf_AddIOLogIDs", "999"), ("#txt_AddDisMin", "31"), ("#txt_AddBackMin", "34")):
            with self.subTest(selector=selector), self.context():
                original = self.controls[selector]
                self.controls[selector] = value
                try:
                    with self.assertRaises(RuntimeError):
                        self.submit()
                    self.assertFalse(self.saved)
                finally:
                    self.controls[selector] = original

    def test_dry_run_stops_before_the_form_is_submitted(self):
        with self.context(), mock.patch.dict("os.environ", {"SAVE_CIVILPOWER_RECORD": "false"}):
            with self.assertRaisesRegex(RuntimeError, "不儲存"):
                self.submit()
        self.assertFalse(self.saved)

    def test_human_status_line_format_is_accepted_but_other_names_are_not(self):
        with self.context():
            self.controls["#txt_AddStat"] = "救護義消協勤：測試義消、其他義消"
            self.assertEqual(self.fixture.work, self.submit())
        self.saved = False
        with self.context():
            self.controls["#txt_AddStat"] = "3.救護義消協勤:測試義消甲"
            with self.assertRaises(RuntimeError):
                self.submit()
        self.assertFalse(self.saved)
