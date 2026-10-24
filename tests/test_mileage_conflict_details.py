from datetime import datetime
import unittest
from unittest.mock import patch

from ambulance_bot import selenium_local as runtime
from ambulance_bot.models import AmbulanceReturnRequest


class MileageConflictDetailsTests(unittest.TestCase):
    def request(self, **changes):
        values = dict(task_id="conflict-detail", created_at=datetime(2026, 10, 24), raw_text="",
                      service_type="disaster", vehicle="新坡11", driver="甲", mileage="35477",
                      case_date="2026/10/24", case_time="1336", return_date="2026/10/24",
                      return_time="1409", case_address="測試地點")
        return AmbulanceReturnRequest(**{**values, **changes})

    def blocked_detail(self, request, rows):
        with patch.object(runtime, "_vehicle_mileage_history", return_value=rows), \
             patch.object(runtime, "_load_vehicle_mileage_month", return_value=rows), \
             patch.object(runtime, "_write_vehicle_mileage_backfill") as write, \
             patch.object(runtime, "_save_vehicle_mileage_form") as save:
            with self.assertRaises(runtime.WebDriverException) as raised:
                runtime._add_vehicle_mileage_record(object(), request)
        write.assert_not_called()
        save.assert_not_called()
        return str(raised.exception)

    def test_lower_than_predecessor_reports_case_and_official_record_for_the_car(self):
        before = dict(Id=180820, StartDay="20261024", StartTime="0933", EndDay="20261024",
                      EndTime="1002", StartMileage=35474, EndMileage=35480, Mileage=6,
                      Destination="其他地點", month="2026/10")
        detail = self.blocked_detail(self.request(), [before])
        for expected in ("本案結束里程小於前一筆結束里程", "新坡11", "13:36", "14:09",
                         "2026/10/24 10:02", "180820", "35480", "35477"):
            self.assertIn(expected, detail)

    def test_historical_case_above_following_end_reports_both_mileages_and_record(self):
        before = dict(Id=10, StartDay="20261023", StartTime="1204", EndDay="20261023",
                      EndTime="1257", StartMileage=35444, EndMileage=35465, Mileage=21,
                      Destination="其他地點", month="2026/10")
        following = dict(Id=20, StartDay="20261024", StartTime="0933", EndDay="20261024",
                         EndTime="0953", StartMileage=35465, EndMileage=35467, Mileage=2,
                         Destination="另一地點", month="2026/10")
        request = self.request(case_time="0837", return_time="0928", mileage="35481")
        detail = self.blocked_detail(request, [before, following])
        for expected in ("本案結束里程超過後一筆結束里程", "新坡11", "08:37", "09:28",
                         "2026/10/24 09:33", "09:53", "紀錄 20", "35467", "35481"):
            self.assertIn(expected, detail)


if __name__ == "__main__":
    unittest.main()
