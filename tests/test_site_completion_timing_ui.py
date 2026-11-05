from datetime import datetime, timedelta
from html.parser import HTMLParser
import json
import unittest

import app as web
from ambulance_bot.models import FuelRecord
from tests import test_lan_login_visibility as fixture


class DurationCards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = {}
        self.label = ""
        self.depth = 0

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "div" and "site-duration" in attrs.get("class", "").split():
            self.label = attrs["aria-label"]
            self.cards[self.label] = ""
            self.depth = 1
        elif self.depth and tag == "div":
            self.depth += 1

    def handle_endtag(self, tag):
        if self.depth and tag == "div":
            self.depth -= 1

    def handle_data(self, text):
        if self.depth:
            self.cards[self.label] += text


class SiteCompletionTimingUiTests(unittest.TestCase):
    setUp = fixture.LanLoginVisibilityTests.setUp
    task = fixture.LanLoginVisibilityTests.task

    def render(self, durations, *, states=None, include_optional=False, service="ems", timeline=()):
        self.renders = getattr(self, "renders", 0) + 1
        started = datetime.now().replace(microsecond=0) - timedelta(minutes=15)
        task = self.task(task_id=f"timing-ui-{self.renders}", created_at=started - timedelta(seconds=10),
                         service_type=service, fuel_record=FuelRecord(enabled=include_optional),
                         volunteer_assist=include_optional)
        events, sites = [], {}
        for key, seconds in durations.items():
            status = (states or {}).get(key, f"{key}_saved")
            sites[key] = dict(key=key, status=status)
            if seconds is not None:
                events.append(dict(time=started.isoformat(), action=f"{web.site_display_name(key)} 開始",
                                   status=f"{key}_running"))
                events.append(dict(time=(started + timedelta(seconds=seconds)).isoformat(),
                                   action=f"{web.site_display_name(key)} 結果", status=status))
        events.extend(dict(time=(started + timedelta(seconds=offset)).isoformat(), status=status, detail=detail)
                      for offset, status, detail in timeline)
        events.sort(key=lambda event: event["time"])
        report = dict(task_id=task.task_id, task=task.to_dict(), title="各站耗時測試",
                      updated_at=datetime.now().isoformat(), site_statuses=sites, events=events)
        path = web.public_pc_report_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(tasks=[report]), ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()
        response = self.client.get(f"/admin/{service}")
        self.assertEqual(200, response.status_code)
        self.assertEqual(before, path.read_bytes())
        body = response.get_data(as_text=True)
        parsed = DurationCards()
        parsed.feed(body)
        return {label: " ".join(text.split()) for label, text in parsed.cards.items()}, body

    def test_each_ems_station_card_shows_its_own_completion_seconds(self):
        cards, body = self.render(dict(duty_work_log=29, vehicle_mileage=33, consumables=632, disinfection=34))
        for label, seconds in (("工作", 29), ("里程", 33), ("耗材", 632), ("消毒", 34)):
            self.assertEqual(f"約 {seconds} 秒", cards[f"{label}完成耗時"])
        self.assertNotIn("首次登打", body)
        self.assertIn("依該站開始與完成回報時間估算", body)

    def test_disaster_and_optional_station_cards_also_show_completion_seconds(self):
        cards, _ = self.render(dict(duty_work_log=29, vehicle_mileage=33), service="disaster")
        self.assertEqual("約 33 秒", cards["里程完成耗時"])
        cards, _ = self.render(dict(duty_work_log=29, vehicle_mileage=33, fuel_record=12),
                               service="disaster", include_optional=True)
        self.assertEqual(3, len(cards))
        self.assertEqual("約 12 秒", cards["加油完成耗時"])
        cards, _ = self.render(dict(duty_work_log=29, vehicle_mileage=33, consumables=32,
                                    disinfection=34, fuel_record=12, volunteer_assist=70), include_optional=True)
        self.assertEqual("約 12 秒", cards["加油完成耗時"])
        self.assertEqual("約 70 秒", cards["民力完成耗時"])

    def test_missing_history_is_unrecorded_and_zero_seconds_remains_visible(self):
        cards, _ = self.render(dict(duty_work_log=0, vehicle_mileage=None, consumables=None, disinfection=None))
        self.assertEqual("約 0 秒", cards["工作完成耗時"])
        self.assertEqual("未記錄", cards["里程完成耗時"])

    def test_unfinished_stations_do_not_show_a_completed_duration(self):
        cards, _ = self.render(dict(duty_work_log=30, vehicle_mileage=40, consumables=50, disinfection=None), states={
            "duty_work_log": "duty_work_log_running", "vehicle_mileage": "vehicle_mileage_failed",
            "consumables": "consumables_waiting_confirmation", "disinfection": "not_started"})
        self.assertEqual(4, len(cards))
        self.assertEqual("—", cards["工作完成耗時"])
        self.assertEqual("—", cards["里程完成耗時"])
        self.assertEqual("—", cards["耗材完成耗時"])
        self.assertEqual("—", cards["消毒完成耗時"])


    def test_successful_retry_seconds_and_task_total_are_displayed(self):
        cards, body = self.render({"duty_work_log": 26, "vehicle_mileage": None}, timeline=[
            (-8, "queued_for_worker", "排隊"),
            (0, "desktop_fast_running", "公務電腦 worker 四站登打已啟動。"),
            (1, "vehicle_mileage_running", "公務電腦 worker 執行車輛里程：pc"),
            (31, "vehicle_mileage_failed", "查詢失敗"),
            (32, "desktop_fast_completed_with_errors", "有站失敗"),
            (110, "queued_for_worker", "排隊"),
            (120, "desktop_fast_running", "公務電腦 worker 車輛里程登打已啟動。"),
            (121, "vehicle_mileage_running", "公務電腦 worker 執行車輛里程：pc"),
            (130, "vehicle_mileage_failed", "再次失敗"),
            (131, "desktop_fast_completed_with_errors", "有站失敗"),
            (180, "desktop_fast_running", "公務電腦 worker 車輛里程登打已啟動。"),
            (181, "vehicle_mileage_running", "公務電腦 worker 執行車輛里程：pc"),
            (193, "desktop_fast_completed", "任務完成"),
            (194, "vehicle_mileage_saved", "回查通過"),
        ])
        self.assertEqual("約 13 秒", cards["里程完成耗時"])
        self.assertEqual("約 26 秒", cards["工作完成耗時"])
        self.assertRegex(body, r'整筆耗時\s*</span>\s*<strong[^>]*>約 194 秒')


if __name__ == "__main__":
    unittest.main()
