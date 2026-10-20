import html
import re
import unittest

from ambulance_bot.models import FuelRecord
from tests import test_lan_login_visibility as fixture


class DisasterVehicleStatusDisplayTests(unittest.TestCase):
    setUp = fixture.LanLoginVisibilityTests.setUp
    task = fixture.LanLoginVisibilityTests.task

    def disaster_task(self, **changes):
        return self.task(service_type="disaster", vehicle="新坡11", vehicle_entries=[
            {"vehicle": "新坡11", "driver": "甲", "mileage": "35444"},
            {"vehicle": "新坡15", "driver": "乙", "mileage": "25400"}], **changes)

    def render_vehicle_rows(self, task, mileage_results, fuel_results=None):
        payload = self.store.get(task.task_id)
        payload["site_statuses"]["vehicle_mileage"].update(
            status="vehicle_mileage_failed", vehicle_results=mileage_results)
        if fuel_results is not None:
            payload["site_statuses"]["fuel_record"].update(
                status="fuel_record_failed", vehicle_results=fuel_results)
        self.store.save_payload(task.task_id, payload)
        response = self.client.get(f"/tasks/{task.task_id}")
        self.assertEqual(200, response.status_code)
        body = response.get_data(as_text=True)
        start = body.index("<h3>里程")
        card = body[start:body.index("</article>", start)]
        rows = re.findall(r"<li>(.*?)</li>", card, re.S)
        return {vehicle: next(row for row in rows if vehicle in html.unescape(row))
                for vehicle in ("新坡11", "新坡15")}

    def test_disaster_vehicles_show_their_own_mileage_status_and_existing_detail(self):
        task = self.disaster_task()
        rows = self.render_vehicle_rows(task, {
            "新坡11": {"status": "vehicle_mileage_saved", "detail": "新坡11 已登打，略過重複新增。"},
            "新坡15": {"status": "vehicle_mileage_failed", "detail": "新坡15 網頁載入逾時。"}})
        self.assertIn('class="status complete"', rows["新坡11"])
        self.assertIn("新坡11 已登打，略過重複新增。", rows["新坡11"])
        self.assertIn('class="status failed"', rows["新坡15"])
        self.assertIn("新坡15 網頁載入逾時。", rows["新坡15"])
        self.assertNotIn("新坡15 網頁載入逾時。", rows["新坡11"])

    def test_missing_vehicle_result_is_unreported_despite_station_failure(self):
        task = self.disaster_task()
        rows = self.render_vehicle_rows(task, {
            "新坡11": {"status": "vehicle_mileage_saved", "detail": "已登打。"}})
        self.assertIn("尚未回報", rows["新坡15"])
        self.assertNotIn('class="status failed"', rows["新坡15"])

    def test_combined_card_does_not_use_fuel_results_as_mileage_results(self):
        task = self.disaster_task(fuel_record=FuelRecord(enabled=True))
        rows = self.render_vehicle_rows(task, {
            "新坡11": {"status": "vehicle_mileage_saved", "detail": "里程已登打。"},
            "新坡15": {"status": "vehicle_mileage_failed", "detail": "里程尚未完成。"}}, {
            "新坡11": {"status": "fuel_record_failed", "detail": "不能當作里程的加油錯誤"},
            "新坡15": {"status": "fuel_record_saved", "detail": "不能當作里程的加油成功"}})
        self.assertIn('class="status complete"', rows["新坡11"])
        self.assertIn('class="status failed"', rows["新坡15"])
        self.assertNotIn("不能當作里程", "".join(rows.values()))

    def test_vehicle_detail_remains_escaped_text(self):
        task = self.disaster_task()
        rows = self.render_vehicle_rows(task, {"新坡11": {
            "status": "vehicle_mileage_saved", "detail": "已登打 <script>alert(1)</script>"}})
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rows["新坡11"])
        self.assertNotIn("<script>", rows["新坡11"])
