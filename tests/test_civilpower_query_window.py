from __future__ import annotations

from dataclasses import asdict, replace
import unittest
from unittest import mock

import civilpower


class CivilpowerQueryWindowTests(unittest.TestCase):
    def setUp(self):
        self.plan = civilpower.CivilpowerTaskPlan(
            task_id="query-window-test", case_id="EMS-QUERY-WINDOW-TEST", case_address="測試地址",
            member_id="test-member", member_name="測試人員", member_title="隊員",
            home_unit="大園救護分隊", serve_unit="新坡分隊",
            out_date="2026/10/19", out_time="1510", in_date="2026/10/19", in_time="1550",
            out_reason="救護出勤", in_reason="救護返隊", duty_status_line="3.救護義消協勤:測試人員",
        )

    def test_case_import_query_covers_return_minutes_and_midnight_without_changing_records(self):
        cases = (
            ("2026/10/19", "1510", "2026/10/19", "1550", "2026/10/19", "16"),
            ("2026/10/19", "1510", "2026/10/19", "1600", "2026/10/19", "16"),
            ("2026/10/19", "1510", "2026/10/19", "1601", "2026/10/19", "17"),
            ("2026/10/19", "1500", "2026/10/19", "1500", "2026/10/19", "16"),
            ("2026/10/19", "0010", "2026/10/19", "0020", "2026/10/19", "01"),
            ("2026/10/19", "2310", "2026/10/19", "2350", "2026/10/20", "00"),
            ("2026/10/19", "2310", "2026/10/20", "0000", "2026/10/20", "00"),
            ("2026/10/19", "2310", "2026/10/20", "0010", "2026/10/20", "01"),
            ("2026/10/31", "2310", "2026/10/31", "2359", "2026/11/01", "00"),
            ("2026/12/31", "2310", "2026/12/31", "2359", "2027/01/01", "00"),
        )
        for out_date, out_time, in_date, in_time, end_date, end_hour in cases:
            with self.subTest(out_date=out_date, out_time=out_time, in_date=in_date, in_time=in_time):
                plan = replace(self.plan, out_date=out_date, out_time=out_time, in_date=in_date, in_time=in_time)
                before = asdict(plan)
                driver, wait, row = mock.Mock(), mock.Mock(), mock.Mock()
                row.text = plan.case_id
                wait.until.return_value.is_selected.return_value = True
                query_inputs = []

                def open_dialog(*args, **kwargs):
                    self.assertEqual([
                        ("#txt_SearchDate_S", out_date), ("#txt_SearchDate_S_H", out_time[:2]),
                        ("#txt_SearchDate_E", end_date), ("#txt_SearchDate_E_H", end_hour),
                    ], query_inputs)
                    return mock.Mock()

                with mock.patch("civilpower._set_input", side_effect=lambda _, selector, value: query_inputs.append((selector, value))), \
                     mock.patch("civilpower._open_selection_dialog", side_effect=open_dialog), \
                     mock.patch("civilpower._wait_for_dialog_row", return_value=row) as matching, \
                     mock.patch("civilpower._click_dialog_row_action", return_value=True) as select, \
                     mock.patch("civilpower._wait_for_dialog_close"), mock.patch("civilpower._confirm_dialog") as confirm:
                    civilpower._import_work_log_case(driver, wait, plan)
                matching.assert_called_once()
                self.assertEqual([plan.case_id], matching.call_args.args[2])
                select.assert_called_once_with(driver, row, "選取")
                confirm.assert_not_called()
                self.assertEqual(before, asdict(plan))

    def test_query_boundary_properties_do_not_change_serialized_plan_fields(self):
        before = asdict(self.plan)
        self.assertEqual("15", self.plan.case_search_start_hour)
        self.assertEqual("16", self.plan.case_search_end_hour)
        self.assertEqual("2026/10/19", self.plan.case_search_end_date)
        self.assertEqual(before, asdict(self.plan))
        self.assertNotIn("case_search_end_date", asdict(self.plan))


if __name__ == "__main__":
    unittest.main()
