import unittest
from datetime import datetime
from unittest.mock import patch

from ambulance_bot import selenium_local as runtime
from ambulance_bot.models import AmbulanceReturnRequest


def record(identity, day, start, end, first, last):
    return dict(Id=identity, StartDay=day, StartTime=start,
                EndDay=day, EndTime=end, StartMileage=first,
                EndMileage=last, Mileage=last-first, DriverName="甲",
                Destination="新坡", month=f"{day[:4]}/{day[4:6]}")


class MileageHistoryScopeTests(unittest.TestCase):
    def request(self, day="2026/10/07", start="1000"):
        return AmbulanceReturnRequest(
            task_id="history-scope", created_at=datetime(2026, 10, 20), raw_text="",
            service_type="disaster", vehicle="新坡11", driver="甲", case_address="新坡",
            case_date=day, case_time=start, return_date=day,
            return_time="1100", mileage="10020")

    def enter(self, request, records, *, today=datetime(2026, 10, 20)):
        loaded = []

        def load(driver, current_request, month, artifacts):
            loaded.append(month)
            return [row.copy() for row in records.get(month, [])]

        with patch.object(runtime, "_load_vehicle_mileage_month", side_effect=load), \
             patch.object(runtime, "datetime", wraps=datetime) as clock, \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=False), \
             patch.object(runtime, "_write_vehicle_mileage_backfill") as write:
            clock.now.return_value = today
            detail = runtime._add_vehicle_mileage_record(object(), request)
        return detail, loaded, write.call_args_list

    def test_case_month_predecessor_still_checks_the_previous_month(self):
        older = record(2, "20260930", "0800", "0900", 9960, 9980)
        for previous_end in ("0900", "1000"):
            with self.subTest(previous_end=previous_end):
                before = record(1, "20261007", "0800", previous_end, 9980, 10000)
                detail, loaded, writes = self.enter(
                    self.request(), {"2026/10": [before], "2026/09": [older]})
                self.assertIn("未按儲存", detail)
                self.assertIn("2026/09", loaded)
                self.assertEqual(before, writes[0].args[2]["previous"])

    def test_previous_month_cross_day_conflict_stops_before_any_write(self):
        request = self.request("2026/10/01", "0030")
        request.return_time = "0045"
        before = record(1, "20261001", "0000", "0010", 9980, 10000)
        conflict = record(2, "20260930", "2350", "0100", 9980, 10030)
        conflict["EndDay"] = "20261001"
        records = {"2026/10": [before], "2026/09": [conflict]}
        with patch.object(runtime, "_load_vehicle_mileage_month",
                          side_effect=lambda d, r, m, a: records.get(m, [])), \
             patch.object(runtime, "datetime", wraps=datetime) as clock, \
             patch.object(runtime, "_write_vehicle_mileage_backfill") as write, \
             patch.object(runtime, "_save_vehicle_mileage_form") as save:
            clock.now.return_value = datetime(2026, 10, 20)
            with self.assertRaisesRegex(runtime.WebDriverException, "案件與既有用車時間重疊"):
                runtime._add_vehicle_mileage_record(object(), request)
        write.assert_not_called()
        save.assert_not_called()

    def test_missing_predecessor_searches_across_empty_older_month(self):
        before = record(1, "20260831", "0800", "0900", 9980, 10000)
        after = record(2, "20261007", "1200", "1300", 10000, 10050)
        _, loaded, writes = self.enter(
            self.request(), {"2026/10": [after], "2026/09": [], "2026/08": [before]})
        self.assertIn("2026/09", loaded)
        self.assertIn("2026/08", loaded)
        self.assertEqual(before, writes[0].args[2]["previous"])
        self.assertEqual(after, writes[0].args[2]["following"])

    def test_case_month_predecessor_preserves_search_for_later_following_record(self):
        before = record(1, "20260907", "0800", "0900", 9980, 10000)
        older = record(3, "20260831", "0800", "0900", 9960, 9980)
        after = record(2, "20261101", "1200", "1300", 10000, 10050)
        detail, loaded, writes = self.enter(
            self.request("2026/09/07"),
            {"2026/09": [before], "2026/08": [older], "2026/10": [], "2026/11": [after]},
            today=datetime(2026, 11, 20))
        self.assertIn("補登需跨月修正後一筆開始里程", detail)
        self.assertIn("2026/10", loaded)
        self.assertIn("2026/11", loaded)
        self.assertIn("2026/08", loaded)
        self.assertEqual([], writes)


if __name__ == "__main__":
    unittest.main()
