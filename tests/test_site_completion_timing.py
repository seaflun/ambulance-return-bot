import copy
import unittest
from unittest.mock import patch

import app as app_module


class SiteCompletionTimingTests(unittest.TestCase):
    def event(self, site, time, state="running", *, action="", detail="", **extra):
        return {"time": time, "status": f"{site}_{state}", "action": action,
                "detail": detail, **extra}

    def report(self, events, **site_statuses):
        statuses = {key: {"status": f"{key}_saved"} for key in app_module.SITE_RUN_ORDER}
        statuses.update({key: {"status": value} for key, value in site_statuses.items()})
        return {"events": events, "site_statuses": statuses,
                "created_at": "2026-10-20T01:00:00", "updated_at": "2026-10-20T23:59:59"}

    def seconds(self, events, site="vehicle_mileage", **site_statuses):
        return app_module.task_site_completion_seconds(self.report(events, **site_statuses))[site]

    def test_each_site_uses_its_own_begin_and_completion(self):
        events = []
        for index, site in enumerate(app_module.SITE_RUN_ORDER):
            events.extend([
                self.event(site, f"2026-10-20T08:0{index}:00"),
                self.event(site, f"2026-10-20T08:0{index}:10", detail="階段進度"),
                self.event(site, f"2026-10-20T08:0{index}:29", "saved"),
            ])
        self.assertEqual(dict.fromkeys(app_module.SITE_RUN_ORDER, 29),
                         app_module.task_site_completion_seconds(self.report(events)))

    def test_missing_start_and_non_automated_current_states_are_unknown(self):
        self.assertIsNone(self.seconds([self.event("vehicle_mileage", "2026-10-20T08:00:29", "saved")]))
        events = [self.event("vehicle_mileage", "2026-10-20T08:00:00"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:29", "saved")]
        for state in ("completed_by_user", "vehicle_mileage_failed", "vehicle_mileage_running", "not_started"):
            with self.subTest(state=state):
                self.assertIsNone(self.seconds(events, vehicle_mileage=state))

    def test_failure_or_waiting_requires_a_new_start_and_excludes_retry_wait(self):
        for state in ("failed", "waiting_confirmation", "vehicle_candidate_available"):
            with self.subTest(state=state):
                events = [self.event("vehicle_mileage", "2026-10-20T08:00:00"),
                          self.event("vehicle_mileage", "2026-10-20T08:00:30", state)]
                self.assertIsNone(self.seconds(events + [self.event("vehicle_mileage", "2026-10-20T09:00:12", "saved")]))
                events.extend([self.event("vehicle_mileage", "2026-10-20T09:00:00", action="車輛里程 重試"),
                               self.event("vehicle_mileage", "2026-10-20T09:00:12", "saved")])
                self.assertEqual(12, self.seconds(events))

    def test_progress_snapshots_do_not_restart_or_end_the_site(self):
        events = [self.event("vehicle_mileage", "2026-10-20T08:00:00", action="車輛里程 開始"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:10", action="車輛里程 階段：登入"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:15", "failed", action="車輛里程 階段：填表"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:29", "saved")]
        self.assertEqual(29, self.seconds(events))

    def test_explicit_restart_resets_an_unfinished_attempt(self):
        for action, detail in (("車輛里程 開始", ""), ("車輛里程 重試", ""),
                               ("", "公務電腦 worker 執行車輛里程：pc-1")):
            with self.subTest(action=action, detail=detail):
                events = [self.event("vehicle_mileage", "2026-10-20T08:00:00"),
                          self.event("vehicle_mileage", "2026-10-20T09:00:00", action=action, detail=detail),
                          self.event("vehicle_mileage", "2026-10-20T09:00:29", "saved")]
                self.assertEqual(29, self.seconds(events))

    def test_worker_work_log_start_has_a_known_legacy_status(self):
        events = [{"time": "2026-10-20T08:00:00", "status": "worker_running",
                   "detail": "公務電腦 worker 執行中：pc-1"},
                  self.event("duty_work_log", "2026-10-20T08:00:29", "saved")]
        self.assertEqual(29, self.seconds(events, "duty_work_log"))

    def test_worker_civilpower_preparation_is_a_known_initial_event(self):
        events = [self.event("volunteer_assist", "2026-10-20T08:00:00",
                             detail="登入帳號：民力系統=任務司機。公務電腦 worker 民力系統：準備登入民力運用管理系統"),
                  self.event("volunteer_assist", "2026-10-20T08:00:29", "saved")]
        self.assertEqual(29, self.seconds(events, "volunteer_assist"))

    def test_vehicle_terminals_do_not_finish_or_reset_the_whole_site(self):
        events = [self.event("vehicle_mileage", "2026-10-20T08:00:00"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:09", "saved", vehicle_key="新坡11"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:20", "failed", vehicle_key="新坡15"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:40", "saved", vehicle_key="新坡15")]
        self.assertIsNone(self.seconds(events))
        events.append(self.event("vehicle_mileage", "2026-10-20T08:00:50", "saved"))
        self.assertEqual(50, self.seconds(events))

    def test_vehicle_progress_cannot_supply_a_missing_whole_site_start(self):
        events = [self.event("vehicle_mileage", "2026-10-20T08:00:10", vehicle_key="新坡11"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:29", "saved")]
        self.assertIsNone(self.seconds(events))

    def test_legacy_multi_vehicle_retry_waits_for_an_explicit_aggregate(self):
        for site in ("vehicle_mileage", "consumables", "disinfection"):
            with self.subTest(site=site):
                events = [self.event(site, "2026-10-20T08:00:00", action="開始"),
                          self.event(site, "2026-10-20T08:00:09", "saved", detail="第一車已儲存"),
                          self.event(site, "2026-10-20T08:00:10", detail="公務電腦 worker 進度：第二車"),
                          self.event(site, "2026-10-20T08:00:20", "failed", detail="第二車第一次失敗"),
                          self.event(site, "2026-10-20T08:00:40", "saved", detail="第二車已儲存")]
                for event in events[1:]:
                    event["event_id"] = "retry-status:" + event["time"]
                report = self.report(events)
                report["task"] = {"service_type": "disaster", "vehicle_entries": [
                    {"vehicle": "新坡11"}, {"vehicle": "新坡15"}]}
                self.assertIsNone(app_module.task_site_completion_seconds(report)[site])
                events.append(self.event(site, "2026-10-20T08:00:50", "saved",
                                         event_id="retry-status:aggregate",
                                         detail="新坡11: 已完成，略過 | 新坡15: 第二車已儲存"))
                self.assertEqual(50, app_module.task_site_completion_seconds(report)[site])

    def test_legacy_aggregate_prefixes_are_supported_but_every_vehicle_is_required(self):
        start = self.event("vehicle_mileage", "2026-10-20T08:00:00", action="開始")
        report = self.report([start])
        report["task"] = {"vehicle_entries": [{"vehicle": "新坡11"}, {"vehicle": "新坡15"}]}
        for detail, expected in (("新坡11: 完成 | 新坡15: 完成", 50),
                                 ("登入帳號：里程=測試帳號。新坡11: 完成 | 新坡15: 完成", 50),
                                 ("車輛里程: 新坡11: 完成 | 新坡15: 完成", 50),
                                 ("新坡11: 正文提到新坡15: 完成", None),
                                 ("新坡11: 完成 | 新坡11: 完成", None),
                                 ("新坡11: 完成 | 新坡155: 完成", None),
                                 ("新坡11: 完成 | 新坡15…", None)):
            with self.subTest(detail=detail):
                report["events"] = [start, self.event("vehicle_mileage", "2026-10-20T08:00:50", "saved",
                    event_id="retry-status:aggregate", detail=detail)]
                self.assertEqual(expected, app_module.task_site_completion_seconds(report)["vehicle_mileage"])

    def test_legacy_fuel_aggregate_only_requires_vehicles_with_fuel_enabled(self):
        events = [self.event("fuel_record", "2026-10-20T08:00:00", action="開始"),
                  self.event("fuel_record", "2026-10-20T08:00:40", "saved",
                             event_id="retry-status:partial", detail="新坡15 已完成加油紀錄")]
        report = self.report(events)
        report["task"] = {"vehicle_entries": [{"vehicle": "新坡11"},
                           {"vehicle": "新坡15", "fuel_record": {"enabled": True}}]}
        self.assertIsNone(app_module.task_site_completion_seconds(report)["fuel_record"])
        events.append(self.event("fuel_record", "2026-10-20T08:00:50", "saved",
                                 event_id="retry-status:aggregate", detail="新坡15: 已完成加油紀錄"))
        self.assertEqual(50, app_module.task_site_completion_seconds(report)["fuel_record"])
        for site in ("duty_work_log", "volunteer_assist"):
            report["events"] = [self.event(site, "2026-10-20T08:00:00", action="開始"),
                                self.event(site, "2026-10-20T08:00:50", "saved",
                                           event_id="retry-status:whole", detail="整案已完成")]
            self.assertEqual(50, app_module.task_site_completion_seconds(report)[site])

    def test_all_completed_skip_preserves_the_previous_successful_duration(self):
        for action, detail in (("車輛里程 略過", "已完成"),
                               ("", "新坡11: 已完成，略過 | 新坡15: 已完成，略過")):
            with self.subTest(action=action):
                events = [self.event("vehicle_mileage", "2026-10-20T08:00:00"),
                          self.event("vehicle_mileage", "2026-10-20T08:00:29", "saved"),
                          self.event("vehicle_mileage", "2026-10-20T09:00:00", detail="公務電腦 worker 執行車輛里程：pc-1"),
                          self.event("vehicle_mileage", "2026-10-20T09:00:01", "saved", action=action, detail=detail)]
                self.assertEqual(29, self.seconds(events))

    def test_skipping_one_vehicle_does_not_hide_the_other_vehicle_completion(self):
        events = [self.event("vehicle_mileage", "2026-10-20T09:00:00"),
                  self.event("vehicle_mileage", "2026-10-20T09:00:29", "saved",
                             detail="新坡11: 已完成，略過 | 新坡15: 里程紀錄已儲存")]
        self.assertEqual(29, self.seconds(events))

    def test_synthetic_reconciliation_and_overall_success_are_not_a_site_end(self):
        events = [self.event("vehicle_mileage", "2026-10-20T08:00:00"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:20", "saved",
                             action=app_module.PUBLIC_PC_LEGACY_RECONCILE_ACTION),
                  {"time": "2026-10-20T08:00:29", "status": "desktop_fast_completed",
                   "detail": "車輛里程及其他站別都已完成。"}]
        self.assertIsNone(self.seconds(events))

    def test_source_times_support_timezone_offsets_and_safe_rounding(self):
        cases = [("2026-10-20T08:00:00", "2026-10-20T00:00:29Z", 29),
                 ("2026-10-20T08:00:00+08:00", "2026-10-20T00:00:29Z", 29),
                 ("2026-10-20T08:00:00.1", "2026-10-20T08:00:00.6", 1),
                 ("2026-10-20T08:00:00", "2026-10-20T08:00:00", 0),
                 ("bad", "2026-10-20T08:00:29", None),
                 ("2026-10-20T08:00:00", "bad", None),
                 ("2026-10-20T08:00:30", "2026-10-20T08:00:29", None)]
        for start, end, expected in cases:
            with self.subTest(start=start, end=end):
                self.assertEqual(expected, self.seconds([
                    self.event("vehicle_mileage", start), self.event("vehicle_mileage", end, "saved")]))

    def test_invalid_explicit_restart_cannot_reuse_a_stale_start(self):
        events = [self.event("vehicle_mileage", "2026-10-20T08:00:00"),
                  self.event("vehicle_mileage", "bad", action="車輛里程 開始"),
                  self.event("vehicle_mileage", "2026-10-20T09:00:29", "saved")]
        self.assertIsNone(self.seconds(events))

    def test_success_followed_by_an_unfinished_attempt_does_not_reuse_old_seconds(self):
        events = [self.event("vehicle_mileage", "2026-10-20T08:00:00", action="車輛里程 開始"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:29", "saved"),
                  self.event("vehicle_mileage", "2026-10-20T09:00:00", action="車輛里程 開始")]
        self.assertIsNone(self.seconds(events))

    def test_delayed_events_cannot_replace_a_newer_attempt(self):
        for state in ("running", "saved", "failed"):
            with self.subTest(state=state):
                events = [self.event("vehicle_mileage", "2026-10-20T08:00:00", action="車輛里程 開始"),
                          self.event("vehicle_mileage", "2026-10-20T08:00:29", "saved"),
                          self.event("vehicle_mileage", "2026-10-20T09:00:00", action="車輛里程 開始"),
                          self.event("vehicle_mileage", "2026-10-20T08:00:10", state, action="車輛里程 開始"),
                          self.event("vehicle_mileage", "2026-10-20T09:00:50", "saved")]
                self.assertEqual(50, self.seconds(events))

    def test_new_unpaired_completion_is_unknown_but_exact_duplicate_preserves_seconds(self):
        events = [self.event("vehicle_mileage", "2026-10-20T08:00:00"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:29", "saved", detail="里程紀錄已儲存")]
        duplicate = self.event("vehicle_mileage", "2026-10-20T00:00:29Z", "saved", detail="里程紀錄已儲存")
        self.assertEqual(29, self.seconds(events + [duplicate]))
        newer = self.event("vehicle_mileage", "2026-10-20T09:00:50", "saved", detail="里程紀錄已儲存")
        self.assertIsNone(self.seconds(events + [newer]))

    def test_progress_without_a_begin_event_cannot_supply_the_whole_duration(self):
        for action, detail in (("車輛里程 階段：儲存確認", ""),
                               ("", "公務電腦 worker 車輛里程：儲存"),
                               ("執行中", "本機快速 車輛里程：確認里程紀錄")):
            with self.subTest(action=action, detail=detail):
                events = [self.event("vehicle_mileage", "2026-10-20T08:00:25", action=action, detail=detail),
                          self.event("vehicle_mileage", "2026-10-20T08:00:29", "saved")]
                self.assertIsNone(self.seconds(events))

    def test_normalizes_events_once_and_leaves_the_input_unchanged(self):
        report = self.report([])
        before = copy.deepcopy(report)
        events = [self.event("vehicle_mileage", "2026-10-20T08:00:00"),
                  self.event("vehicle_mileage", "2026-10-20T08:00:29", "saved")]
        with patch.object(app_module, "public_pc_report_display_events", return_value=events) as normalized:
            self.assertEqual(29, app_module.task_site_completion_seconds(report)["vehicle_mileage"])
        normalized.assert_called_once_with(report)
        self.assertEqual(before, report)

    def test_template_helper_is_registered_and_malformed_snapshots_are_safe(self):
        self.assertIs(app_module.task_site_completion_seconds,
                      app_module.template_helpers()["task_site_completion_seconds"])
        for report in ({}, {"site_statuses": []}, {"site_statuses": {"vehicle_mileage": None}, "events": [None]}):
            with self.subTest(report=report):
                self.assertEqual(dict.fromkeys(app_module.SITE_RUN_ORDER),
                                 app_module.task_site_completion_seconds(report))


if __name__ == "__main__":
    unittest.main()
