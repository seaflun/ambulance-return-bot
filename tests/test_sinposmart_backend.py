import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ambulance_bot.sinposmart_backend import (
    SinpoSmartBackendStore,
    build_sinposmart_admin_view,
    normalize_sinposmart_event,
    sinposmart_fire_day_for,
    sinposmart_status_label,
)


class SinpoSmartBackendStoreTests(unittest.TestCase):
    def test_fire_day_uses_0800_boundary(self):
        self.assertEqual(sinposmart_fire_day_for(datetime(2026, 6, 15, 7, 59)), "2026-06-14")
        self.assertEqual(sinposmart_fire_day_for(datetime(2026, 6, 15, 8, 0)), "2026-06-15")

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
            day = store.read_day("2026-06-15")

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
            day = store.read_day("2026-06-15")

            self.assertEqual(event["record_type"], "tool_action_started")
            self.assertEqual(event["item_title"], "開始勤務表登打")
            self.assertEqual(day["summary"]["tool_starts"], 1)
            self.assertEqual(day["summary"]["failed"], 0)
            self.assertEqual(len(day["admin_view"]["tool_events"]), 1)
            self.assertEqual(day["admin_view"]["tool_events"][0]["record_label"], "工具開始")
            self.assertEqual(day["admin_view"]["tool_events"][0]["status_label"], "執行中")

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

    def test_admin_view_combines_queue_and_result_in_one_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            base_payload = {
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "trigger_type": "due",
                "item_kind": "出入",
                "item_title": "值退 / 值退｜27 林宏為",
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
            self.assertEqual(actions[0]["item_title"], "18:00｜出入｜值退 / 值退｜27 林宏為")
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
            ]
        )

        self.assertEqual(len(view["action_events"]), 1)
        self.assertEqual(view["action_events"][0]["record_label"], "到點勤務")
        self.assertEqual(view["action_events"][0]["item_title"], "17:00｜工作｜在隊訓練｜戰術體能訓練")
        self.assertEqual(view["action_events"][0]["status_label"], "等待登打")
        self.assertEqual(view["action_events"][0]["started_at"], "2026-06-18T17:00:00")
        self.assertEqual(view["action_events"][0]["completed_at"], "")
        self.assertEqual(view["action_events"][0]["pause_reason"], "尚未收到完成結果，可能仍在等待登打或流程已暫停。")
        self.assertNotEqual(view["action_events"][0]["status_label"], "pending_write_automation")

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

    def test_admin_view_login_section_shows_latest_logout_status(self):
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

        self.assertEqual(len(view["login_events"]), 1)
        self.assertEqual(view["login_events"][0]["record_label"], "登出")
        self.assertEqual(view["login_events"][0]["status_label"], "登出")
        self.assertEqual(view["login_events"][0]["person_label"], "27番 隊員 林宏為")

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


if __name__ == "__main__":
    unittest.main()
