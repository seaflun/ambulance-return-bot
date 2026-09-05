import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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

    def test_aware_timestamp_is_converted_to_taipei_before_fire_day_boundary(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-aware-time",
                "occurred_at": "2026-07-13T00:00:00Z",
                "record_type": "action_result",
                "status": "submitted",
            },
            now=datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(event["occurred_at"], "2026-07-13T08:00:00")
        self.assertEqual(event["fire_day"], "2026-07-13")

    def test_normalize_records_server_received_time_instead_of_client_value(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-server-received-time",
                "occurred_at": "2026-07-13T08:00:00",
                "received_at": "2000-01-01T00:00:00",
                "record_type": "action_queued",
                "status": "pending_write_automation",
            },
            now=datetime(2026, 7, 13, 8, 1),
        )

        self.assertEqual(event["received_at"], "2026-07-13T08:01:00")

    def test_concurrent_store_instances_preserve_every_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def write_event(index: int) -> str:
                try:
                    SinpoSmartBackendStore(root).upsert_event(
                        {
                            "event_id": f"evt-concurrent-{index}",
                            "occurred_at": f"2026-07-13T09:00:{index:02d}",
                            "record_type": "action_result",
                            "status": "submitted",
                            "content": f"concurrent event {index}",
                        },
                        now=datetime(2026, 7, 13, 9, 0),
                    )
                except OSError as exc:
                    return type(exc).__name__
                return ""

            with ThreadPoolExecutor(max_workers=16) as pool:
                errors = [error for error in pool.map(write_event, range(24)) if error]

            events = SinpoSmartBackendStore(root).read_day("2026-07-13")["events"]

            self.assertEqual(errors, [])
            self.assertEqual(len(events), 24)
            self.assertEqual({event["event_id"] for event in events}, {f"evt-concurrent-{index}" for index in range(24)})

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
            day = store.read_day("2026-06-15", now=datetime(2026, 6, 15, 12, 10, 41))

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

    def test_retry_of_merged_event_id_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            first_payload = {
                "event_id": "evt-merged-retry-1",
                "occurred_at": "2026-06-15T09:00:00",
                "record_type": "action_result",
                "status": "submitted",
                "actor_no": "8",
                "content": "same logical result",
            }
            second_payload = dict(first_payload, event_id="evt-merged-retry-2", occurred_at="2026-06-15T09:01:00")

            store.upsert_event(first_payload, now=datetime(2026, 6, 15, 9, 0))
            store.upsert_event(second_payload, now=datetime(2026, 6, 15, 9, 1))
            store.upsert_event(second_payload, now=datetime(2026, 6, 15, 9, 2))
            merged = store.read_day("2026-06-15")["events"][0]

            self.assertEqual(merged["repeat_count"], 2)
            self.assertEqual(merged["merged_event_ids"], ["evt-merged-retry-1", "evt-merged-retry-2"])
            self.assertEqual(merged["last_occurred_at"], "2026-06-15T09:01:00")

    def test_same_event_id_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            payload = {
                "event_id": "evt-update-logout-retry",
                "occurred_at": "2026-06-15T07:19:54",
                "record_type": "logout",
                "status": "ok",
                "actor_no": "8",
                "display_name": "8番 隊員 曾彥綸",
                "trigger_type": "update",
                "content": "更新前登出",
            }
            retry_payload = dict(payload, occurred_at="2026-06-15T07:20:10")

            store.upsert_event(payload, now=datetime(2026, 6, 15, 7, 19))
            store.upsert_event(retry_payload, now=datetime(2026, 6, 15, 7, 20))
            day = store.read_day("2026-06-14")

            self.assertEqual(len(day["events"]), 1)
            self.assertEqual(day["events"][0]["repeat_count"], 1)
            self.assertEqual(day["events"][0]["occurred_at"], "2026-06-15T07:19:54")
            self.assertEqual(day["events"][0]["last_occurred_at"], "2026-06-15T07:19:54")

    def test_existing_login_record_repeat_count_is_normalized_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            path = store.path_for_day("2026-06-14")
            path.write_text(
                """{
  "fire_day": "2026-06-14",
  "updated_at": "2026-06-15T07:20:10",
  "events": [
    {
      "event_id": "evt-update-logout-retry",
      "occurred_at": "2026-06-15T07:20:10",
      "last_occurred_at": "2026-06-15T07:20:10",
      "record_type": "logout",
      "status": "ok",
      "actor_no": "8",
      "display_name": "8番 隊員 曾彥綸",
      "trigger_type": "update",
      "content": "更新前登出",
      "repeat_count": 2
    }
  ]
}""",
                encoding="utf-8",
            )

            day = store.read_day("2026-06-14")

            self.assertEqual(day["events"][0]["repeat_count"], 1)
            self.assertEqual(day["admin_view"]["login_events"][0]["repeat_count"], 1)

    def test_login_events_with_different_ids_do_not_merge_when_content_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            first_payload = {
                "event_id": "evt-login-1",
                "occurred_at": "2026-06-15T09:00:00",
                "record_type": "login",
                "status": "ok",
                "actor_no": "4",
                "display_name": "4番 隊員 測試",
            }
            second_payload = dict(first_payload, event_id="evt-login-2", occurred_at="2026-06-15T09:05:00")

            store.upsert_event(first_payload, now=datetime(2026, 6, 15, 9, 0))
            store.upsert_event(second_payload, now=datetime(2026, 6, 15, 9, 5))
            day = store.read_day("2026-06-15")

            self.assertEqual(len(day["events"]), 2)
            self.assertEqual(len(day["admin_view"]["login_events"]), 2)
            self.assertEqual([event["last_occurred_at"] for event in day["admin_view"]["login_events"]], ["2026-06-15T09:05:00", "2026-06-15T09:00:00"])

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
            day = store.read_day("2026-06-15", now=datetime(2026, 6, 15, 12, 10, 41))

            self.assertEqual(event["record_type"], "tool_action_started")
            self.assertEqual(event["item_title"], "開始勤務表登打")
            self.assertEqual(day["summary"]["tool_starts"], 1)
            self.assertEqual(day["summary"]["failed"], 0)
            self.assertEqual(len(day["admin_view"]["tool_events"]), 1)
            self.assertEqual(day["admin_view"]["tool_events"][0]["record_label"], "工具開始")
            self.assertEqual(day["admin_view"]["tool_events"][0]["status_label"], "執行中")
            self.assertEqual(day["admin_view"]["tool_events"][0]["waiting_age_seconds"], 41)
            self.assertEqual(
                day["admin_view"]["tool_events"][0]["waiting_reason"],
                "已收到開始執行，已等待 41 秒，等待工具完成結果回傳。",
            )
            self.assertEqual(day["admin_view"]["tool_events"][0]["pause_reason"], "")

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

    def test_admin_view_keeps_rescue_video_runs_separate_with_summary(self):
        events = []
        for run_id, occurred_at, case_count, total_count, usage_seconds in (
            ("rescue-run-1", "2026-08-24T10:00:00", 2, 5, 65),
            ("rescue-run-2", "2026-08-24T10:10:00", 1, 2, 30),
        ):
            shared_snapshot = {
                "tool_name": "rescue_video",
                "tool_label": "救護行車紀錄器",
                "run_id": run_id,
            }
            events.append(
                normalize_sinposmart_event(
                    {
                        "event_id": f"{run_id}-started",
                        "occurred_at": occurred_at,
                        "record_type": "tool_action_started",
                        "trigger_type": "tool_start",
                        "status": "started",
                        "actor_no": "27",
                        "display_name": "27番 隊員 林宏為",
                        "snapshot": shared_snapshot,
                    },
                    now=datetime(2026, 8, 24, 10, 0),
                )
            )
            events.append(
                normalize_sinposmart_event(
                    {
                        "event_id": f"{run_id}-finished",
                        "occurred_at": occurred_at,
                        "record_type": "tool_action_finished",
                        "trigger_type": "tool_finish",
                        "status": "completed",
                        "content": "救護行車紀錄器完成",
                        "actor_no": "27",
                        "display_name": "27番 隊員 林宏為",
                        "snapshot": {
                            **shared_snapshot,
                            "target_date": "2026-08-24",
                            "vehicle": "92",
                            "case_count": case_count,
                            "total_count": total_count,
                            "usage_seconds": usage_seconds,
                        },
                    },
                    now=datetime(2026, 8, 24, 10, 0),
                )
            )

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["tool_events"]), 2)
        summaries = {
            event["rescue_video_summary"]["total_count"]: event["rescue_video_summary"]
            for event in view["tool_events"]
        }
        self.assertEqual(
            summaries[5],
            {
                "target_date": "2026-08-24",
                "vehicle": "92",
                "case_count": 2,
                "total_count": 5,
                "usage_time": "1 分 5 秒",
            },
        )
        self.assertEqual(summaries[2]["usage_time"], "30 秒")
        self.assertTrue(
            all(
                [step["label"] for step in event["steps"]] == ["開始執行", "結束執行"]
                for event in view["tool_events"]
            )
        )

    def test_admin_view_shows_safe_tool_failure_stage(self):
        view = build_sinposmart_admin_view(
            [
                normalize_sinposmart_event(
                    {
                        "event_id": "evt-tool-failed",
                        "occurred_at": "2026-08-02T10:00:00",
                        "record_type": "tool_action_finished",
                        "trigger_type": "tool_finish",
                        "status": "failed",
                        "snapshot": {
                            "tool_name": "rest_time",
                            "tool_label": "休息時間登打",
                            "failure_stage": "browser_start",
                            "failure_detail": "browser_startup",
                        },
                    },
                    now=datetime(2026, 8, 2, 10, 0),
                )
            ]
        )

        self.assertEqual(view["tool_events"][0]["failure_stage_label"], "瀏覽器啟動")
        self.assertEqual(
            view["tool_events"][0]["failure_detail"],
            "專用瀏覽器啟動或連線未完成；已清理過期暫存設定檔並重試一次。",
        )
        self.assertEqual(view["tool_events"][0]["status_label"], "失敗")
        self.assertEqual(view["tool_events"][0]["waiting_reason"], "")

    def test_admin_view_marks_tool_waiting_as_attention_without_calling_it_failed(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-tool-overdue",
                "occurred_at": "2026-08-09T08:29:02",
                "record_type": "tool_action_started",
                "trigger_type": "tool_start",
                "status": "started",
                "snapshot": {"tool_name": "daily_vehicle", "tool_label": "車輛保養清點"},
            },
            now=datetime(2026, 8, 9, 8, 29, 2),
        )

        view = build_sinposmart_admin_view([event], now=datetime(2026, 8, 9, 8, 44, 2))
        card = view["tool_events"][0]

        self.assertEqual(card["status_label"], "等待逾時待確認")
        self.assertEqual(card["status_class"], "attention")
        self.assertEqual(card["waiting_age_seconds"], 900)
        self.assertTrue(card["waiting_overdue"])
        self.assertIn("尚不代表工具失敗", card["waiting_reason"])
        self.assertEqual(card["pause_reason"], "")

    def test_admin_view_combines_queue_and_result_in_one_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            base_payload = {
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "trigger_type": "due",
                "item_kind": "出入",
                "item_title": "值退 / 值退｜27番 林宏為（隊員）",
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
                    "target": "",
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
            self.assertEqual(
                actions[0]["item_title"],
                "18:00｜出入｜值退 / 值退｜27番 林宏為",
            )
            self.assertEqual(actions[0]["item_title"].count("27番 林宏為"), 1)
            self.assertNotIn("隊員", actions[0]["item_title"])
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
            ],
            now=datetime(2026, 6, 18, 17, 0, 41),
        )

        self.assertEqual(len(view["action_events"]), 1)
        self.assertEqual(view["action_events"][0]["record_label"], "到點勤務")
        self.assertEqual(
            view["action_events"][0]["item_title"],
            "17:00｜工作｜在隊訓練｜戰術體能訓練｜1,5,6,8,10,11,15番",
        )
        self.assertEqual(view["action_events"][0]["status_label"], "等待完成回傳")
        self.assertEqual(view["action_events"][0]["status_class"], "running")
        self.assertEqual(view["action_events"][0]["started_at"], "2026-06-18T17:00:00")
        self.assertEqual(view["action_events"][0]["completed_at"], "")
        self.assertEqual(view["action_events"][0]["waiting_age_seconds"], 41)
        self.assertEqual(
            view["action_events"][0]["waiting_reason"],
            "已收到開始送出，已等待 41 秒，等待勤務系統完成結果回傳。",
        )
        self.assertFalse(view["action_events"][0]["waiting_overdue"])
        self.assertEqual(view["action_events"][0]["pause_reason"], "")
        self.assertNotEqual(view["action_events"][0]["status_label"], "pending_write_automation")

    def test_admin_view_marks_action_waiting_as_attention_without_calling_it_failed(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-queue-overdue",
                "occurred_at": "2026-06-18T17:00:00",
                "record_type": "action_queued",
                "status": "pending_write_automation",
                "trigger_type": "due",
                "item_kind": "工作",
                "item_title": "值班(宿)",
                "target_time": "17:00",
            },
            now=datetime(2026, 6, 18, 17, 0),
        )

        view = build_sinposmart_admin_view([event], now=datetime(2026, 6, 18, 17, 15))
        card = view["action_events"][0]

        self.assertEqual(card["status_label"], "等待逾時待確認")
        self.assertEqual(card["status_class"], "attention")
        self.assertEqual(card["waiting_age_seconds"], 900)
        self.assertTrue(card["waiting_overdue"])
        self.assertIn("尚不代表登打失敗", card["waiting_reason"])
        self.assertEqual(card["pause_reason"], "")
        self.assertEqual(view["summary"]["failed"], 0)

    def test_admin_view_merges_targetless_result_before_queued(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-result-before-queue",
                    "occurred_at": "2026-06-18T12:00:00",
                    "record_type": "action_result",
                    "status": "submitted",
                    "trigger_type": "due",
                    "item_kind": "工作",
                    "item_title": "值班(宿)",
                    "target": "",
                    "target_time": "12:00",
                },
                now=datetime(2026, 6, 18, 12, 0),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-queue-after-result",
                    "occurred_at": "2026-06-18T12:00:01",
                    "record_type": "action_queued",
                    "status": "pending_write_automation",
                    "trigger_type": "due",
                    "item_kind": "工作",
                    "item_title": "值班(宿)",
                    "target": "12番 王小明（隊員）",
                    "target_time": "12:00",
                },
                now=datetime(2026, 6, 18, 12, 0),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["action_events"]), 1)
        self.assertEqual(view["action_events"][0]["item_title"], "12:00｜工作｜值班(宿)｜12番 王小明")
        self.assertNotIn("隊員", view["action_events"][0]["item_title"])

    def test_admin_view_omits_empty_action_target_from_title(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-empty-target",
                "occurred_at": "2026-06-18T12:00:00",
                "record_type": "action_result",
                "status": "submitted",
                "trigger_type": "due",
                "item_kind": "工作",
                "item_title": "值班(宿)",
                "target": "",
                "target_time": "12:00",
            },
            now=datetime(2026, 6, 18, 12, 0),
        )

        view = build_sinposmart_admin_view([event])

        self.assertEqual(view["action_events"][0]["item_title"], "12:00｜工作｜值班(宿)")

    def test_admin_view_combines_manual_action_by_completion_key_when_times_differ(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-manual-queue",
                    "occurred_at": "2026-06-18T07:58:00",
                    "record_type": "action_queued",
                    "status": "manual_marked",
                    "trigger_type": "manual",
                    "item_kind": "工作",
                    "item_title": "救護返隊｜測試案件",
                    "target": "8番 曾彥綸（隊員）",
                    "target_time": "07:58",
                    "snapshot": {"completion_key": "work-log-case-1"},
                },
                now=datetime(2026, 6, 18, 7, 58),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-manual-result",
                    "occurred_at": "2026-06-18T08:00:00",
                    "record_type": "action_result",
                    "status": "submitted",
                    "trigger_type": "manual",
                    "item_kind": "工作",
                    "item_title": "救護返隊｜測試案件",
                    "target": "8番 曾彥綸（隊員）",
                    "target_time": "08:00",
                    "snapshot": {"completion_key": "work-log-case-1"},
                },
                now=datetime(2026, 6, 18, 8, 0),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["action_events"]), 1)
        self.assertEqual(view["action_events"][0]["started_at"], "2026-06-18T07:58:00")
        self.assertEqual(view["action_events"][0]["completed_at"], "2026-06-18T08:00:00")
        self.assertEqual(view["action_events"][0]["status_label"], "已登打")

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

    def test_admin_view_uses_later_success_after_failed_retry(self):
        completion_key = "entry:2026-08-14:18:in:27:返隊:防溺車巡繼領取氣瓶"
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-retry-queued",
                    "occurred_at": "2026-08-14T18:00:00",
                    "record_type": "action_queued",
                    "status": "pending_write_automation",
                    "trigger_type": "due",
                    "item_kind": "出入",
                    "item_title": "入 / 返隊",
                    "target": "27番 林宏為",
                    "target_time": "18:00",
                    "snapshot": {"completion_key": completion_key},
                },
                now=datetime(2026, 8, 14, 18, 0),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-retry-failed",
                    "occurred_at": "2026-08-14T18:00:13",
                    "record_type": "action_result",
                    "status": "failed",
                    "error": "第一次登打失敗",
                    "trigger_type": "due",
                    "item_kind": "出入",
                    "item_title": "入 / 返隊",
                    "target": "27番 林宏為",
                    "target_time": "18:00",
                    "snapshot": {"completion_key": completion_key},
                },
                now=datetime(2026, 8, 14, 18, 0, 13),
            ),
            normalize_sinposmart_event(
                {
                    "event_id": "evt-retry-submitted",
                    "occurred_at": "2026-08-14T18:01:39",
                    "record_type": "action_result",
                    "status": "submitted",
                    "trigger_type": "due",
                    "item_kind": "出入",
                    "item_title": "入 / 返隊",
                    "target": "27番 林宏為",
                    "target_time": "18:00",
                    "snapshot": {"completion_key": completion_key},
                },
                now=datetime(2026, 8, 14, 18, 1, 39),
            ),
        ]

        view = build_sinposmart_admin_view(events)
        card = view["action_events"][0]

        self.assertEqual(len(view["action_events"]), 1)
        self.assertEqual(card["status_label"], "已登打")
        self.assertEqual(card["status_class"], "complete")
        self.assertEqual(card["completed_at"], "2026-08-14T18:01:39")
        self.assertEqual(card["pause_reason"], "")

    def test_admin_view_preserves_submission_after_relogin_existing_check(self):
        now = datetime(2026, 9, 5, 8, 3)
        base = {
            "record_type": "action_result",
            "trigger_type": "due",
            "actor_no": "8",
            "display_name": "8番 測試員",
            "item_kind": "工作",
            "item_title": "值班交接",
            "target_time": "08:00",
            "snapshot": {"completion_key": "work:2026-09-05:8:值班交接:8"},
        }
        submitted = {
            **base,
            "event_id": "evt-original-submission",
            "occurred_at": "2026-09-05T08:00:26",
            "status": "submitted",
            "result_ref": "original_submission.json",
        }
        existing = {
            **base,
            "event_id": "evt-relogin-existing",
            "occurred_at": "2026-09-05T08:02:14",
            "actor_no": "15",
            "display_name": "15番 測試員",
            "status": "skipped_duplicate",
        }
        for order in ((submitted, existing), (existing, submitted)):
            with self.subTest(first=order[0]["status"]), tempfile.TemporaryDirectory() as tmp:
                store = SinpoSmartBackendStore(Path(tmp))
                for event in order:
                    store.upsert_event(event, now=now)

                day = store.read_day("2026-09-05", now=now)
                view = day["admin_view"]
                card = view["action_events"][0]

                self.assertEqual(len(day["events"]), 2)
                self.assertEqual({event["status"] for event in day["events"]}, {"submitted", "skipped_duplicate"})
                self.assertEqual(len(view["action_events"]), 1)
                self.assertEqual(card["status_label"], "已登打")
                self.assertEqual(card["completed_at"], submitted["occurred_at"])
                self.assertEqual(card["person_label"], "8番 測試員")
                self.assertEqual(card["event_id"], submitted["event_id"])
                source = next(event for event in day["events"] if event["event_id"] == card["event_id"])
                self.assertEqual(source["result_ref"], "original_submission.json")
                self.assertEqual(view["summary"]["submitted"], 1)
                self.assertEqual(view["summary"]["existing"], 0)

    def test_admin_view_existing_check_does_not_invent_submission_or_hide_failure(self):
        now = datetime(2026, 9, 5, 8, 3)
        for statuses, expected in (
            (("skipped_duplicate",), "已存在"),
            (("skipped_duplicate", "submitted"), "已登打"),
            (("submitted", "failed"), "失敗"),
            (("failed", "skipped_duplicate"), "已存在"),
        ):
            with self.subTest(statuses=statuses):
                events = [
                    normalize_sinposmart_event(
                        {
                            "event_id": f"evt-state-{index}",
                            "occurred_at": f"2026-09-05T08:0{index}:26",
                            "record_type": "action_result",
                            "status": status,
                            "snapshot": {"completion_key": "same-duty-action"},
                        },
                        now=now,
                    )
                    for index, status in enumerate(statuses)
                ]

                view = build_sinposmart_admin_view(events, now=now)

                self.assertEqual(len(view["action_events"]), 1)
                self.assertEqual(view["action_events"][0]["status_label"], expected)

    def test_admin_view_finds_submission_before_fire_day_boundary_without_moving_events(self):
        now = datetime(2026, 9, 5, 8, 3)
        with tempfile.TemporaryDirectory() as tmp:
            store = SinpoSmartBackendStore(Path(tmp))
            submitted = store.upsert_event(
                {
                    "event_id": "evt-before-boundary",
                    "occurred_at": "2026-09-05T07:55:22",
                    "record_type": "action_result",
                    "status": "submitted",
                    "actor_no": "8",
                    "display_name": "8番 測試員",
                    "result_ref": "original_arrival.json",
                    "snapshot": {"completion_key": "entry:2026-09-05:755:in:5:到勤"},
                },
                now=now,
            )
            for suffix, key in (
                ("matching", "entry:2026-09-05:755:in:5:到勤"),
                ("other-target", "entry:2026-09-05:755:in:15:到勤"),
                ("other-date", "entry:2026-09-04:755:in:5:到勤"),
            ):
                store.upsert_event(
                    {
                        "event_id": f"evt-recheck-{suffix}",
                        "occurred_at": "2026-09-05T08:02:15",
                        "record_type": "action_result",
                        "status": "skipped_duplicate",
                        "actor_no": "15",
                        "content": suffix,
                        "snapshot": {"completion_key": key},
                    },
                    now=now,
                )
            original_files = {path: path.read_bytes() for path in Path(tmp).glob("*.json")}

            day = store.read_day("2026-09-05", now=now)
            listed_day = store.list_days(limit=1, now=now)[0]

            for payload in (day, listed_day):
                view = payload["admin_view"]
                self.assertEqual(view["summary"]["actions"], 3)
                self.assertEqual(view["summary"]["submitted"], 1)
                self.assertEqual(view["summary"]["existing"], 2)
                card = next(card for card in view["action_events"] if card["status_label"] == "已登打")
                self.assertEqual(card["event_id"], submitted["event_id"])
                self.assertEqual(card["completed_at"], "2026-09-05T07:55:22")
                self.assertEqual(card["person_label"], "8番 測試員")
                self.assertEqual({event["status"] for event in payload["events"]}, {"skipped_duplicate"})
                self.assertEqual({event["fire_day"] for event in payload["events"]}, {"2026-09-05"})
            self.assertEqual(store.read_day("2026-09-04", now=now)["events"], [submitted])
            self.assertEqual({path: path.read_bytes() for path in original_files}, original_files)

    def test_admin_view_cross_day_evidence_requires_prior_success_and_exact_task_key(self):
        now = datetime(2026, 9, 5, 8, 3)
        recheck = {
            "event_id": "evt-current-recheck",
            "occurred_at": "2026-09-05T08:02:15",
            "record_type": "action_result",
            "status": "skipped_duplicate",
            "snapshot": {"completion_key": "entry:2026-09-05:755:in:5:到勤"},
        }
        original = {
            **recheck,
            "event_id": "evt-historical-result",
            "occurred_at": "2026-09-05T07:55:22",
            "status": "submitted",
        }
        for current_changes, history_changes, expected in (
            ({}, {}, "已登打"),
            ({}, {"occurred_at": "2026-09-05T08:03:00"}, "已存在"),
            ({}, {"status": "failed"}, "已存在"),
            ({}, {"record_type": "tool_action_finished"}, "已存在"),
            ({}, {"snapshot": {"completion_key": "entry:2026-09-04:755:in:5:到勤"}}, "已存在"),
            ({"snapshot": {}}, {"snapshot": {}}, "已存在"),
            ({"status": "failed"}, {}, "失敗"),
        ):
            with self.subTest(current=current_changes, history=history_changes):
                view = build_sinposmart_admin_view(
                    [normalize_sinposmart_event({**recheck, **current_changes}, now=now)],
                    related_results=[normalize_sinposmart_event({**original, **history_changes}, now=now)],
                    now=now,
                )

                self.assertEqual(view["summary"]["actions"], 1)
                self.assertEqual(view["action_events"][0]["status_label"], expected)

    def test_admin_view_cross_day_evidence_does_not_match_truncated_completion_key(self):
        now = datetime(2026, 9, 5, 8, 3)
        shared_prefix = "entry:" + "x" * 1200
        recheck = normalize_sinposmart_event(
            {
                "event_id": "evt-long-recheck",
                "occurred_at": "2026-09-05T08:02:15",
                "record_type": "action_result",
                "status": "skipped_duplicate",
                "snapshot": {"completion_key": f"{shared_prefix}:current"},
            },
            now=now,
        )
        unrelated_submission = normalize_sinposmart_event(
            {
                "event_id": "evt-long-unrelated-submission",
                "occurred_at": "2026-09-05T07:55:22",
                "record_type": "action_result",
                "status": "submitted",
                "snapshot": {"completion_key": f"{shared_prefix}:other"},
            },
            now=now,
        )

        view = build_sinposmart_admin_view([recheck], related_results=[unrelated_submission], now=now)

        self.assertEqual(view["summary"]["actions"], 1)
        self.assertEqual(view["action_events"][0]["status_label"], "已存在")

    def test_admin_view_cross_day_evidence_does_not_match_redacted_completion_key(self):
        now = datetime(2026, 9, 5, 8, 3)
        recheck = normalize_sinposmart_event(
            {
                "event_id": "evt-redacted-recheck",
                "occurred_at": "2026-09-05T08:02:15",
                "record_type": "action_result",
                "status": "skipped_duplicate",
                "snapshot": {"completion_key": "entry:token=current"},
            },
            now=now,
        )
        unrelated_submission = normalize_sinposmart_event(
            {
                "event_id": "evt-redacted-unrelated-submission",
                "occurred_at": "2026-09-05T07:55:22",
                "record_type": "action_result",
                "status": "submitted",
                "snapshot": {"completion_key": "entry:token=other"},
            },
            now=now,
        )

        self.assertEqual(
            recheck["snapshot"]["completion_key"],
            unrelated_submission["snapshot"]["completion_key"],
        )
        self.assertNotEqual(
            recheck["snapshot"]["completion_key_sha256"],
            unrelated_submission["snapshot"]["completion_key_sha256"],
        )
        view = build_sinposmart_admin_view([recheck], related_results=[unrelated_submission], now=now)

        self.assertEqual(view["summary"]["actions"], 1)
        self.assertEqual(view["action_events"][0]["status_label"], "已存在")

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

    def test_admin_view_keeps_non_login_error_in_background_updates(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-schedule-error",
                "occurred_at": "2026-06-18T18:10:00",
                "record_type": "error",
                "trigger_type": "schedule",
                "status": "failed",
                "actor_no": "27",
                "display_name": "27番 隊員 林宏為",
                "item_title": "勤務表背景更新",
                "error": "勤務表背景更新失敗",
            },
            now=datetime(2026, 6, 18, 18, 10),
        )

        view = build_sinposmart_admin_view([event])

        self.assertEqual(len(view["background_updates"]), 1)
        card = view["background_updates"][0]
        self.assertEqual(card["status_label"], "失敗")
        self.assertEqual(card["status_class"], "failed")
        self.assertEqual(card["error"], "勤務表背景更新失敗")
        self.assertEqual(view["login_events"], [])

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

    def test_admin_view_login_section_keeps_logout_event_status(self):
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

        self.assertEqual(len(view["login_events"]), 2)
        self.assertEqual([event["record_label"] for event in view["login_events"]], ["登出", "登入"])
        self.assertEqual(view["login_events"][0]["status_label"], "登出")
        self.assertEqual(view["login_events"][0]["person_label"], "27番 隊員 林宏為")

    def test_admin_view_login_section_keeps_each_login_logout_event(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-login-first",
                    "occurred_at": "2026-06-18T16:30:40",
                    "record_type": "login",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
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
            normalize_sinposmart_event(
                {
                    "event_id": "evt-login-second",
                    "occurred_at": "2026-06-18T18:08:30",
                    "record_type": "login",
                    "status": "ok",
                    "actor_no": "27",
                    "display_name": "27番 隊員 林宏為",
                },
                now=datetime(2026, 6, 18, 18, 8),
            ),
        ]

        view = build_sinposmart_admin_view(events)

        self.assertEqual(len(view["login_events"]), 3)
        self.assertEqual([event["record_label"] for event in view["login_events"]], ["登入", "登出", "登入"])
        self.assertEqual([event["last_occurred_at"] for event in view["login_events"]], ["2026-06-18T18:08:30", "2026-06-18T18:05:12", "2026-06-18T16:30:40"])

    def test_admin_view_login_expired_event_is_visible(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-login-expired",
                "occurred_at": "2026-06-18T18:20:00",
                "record_type": "login_expired",
                "status": "failed",
                "actor_no": "4",
                "display_name": "4番 隊員 測試",
                "error": "登入失效",
            },
            now=datetime(2026, 6, 18, 18, 20),
        )

        view = build_sinposmart_admin_view([event])

        self.assertEqual(event["record_type"], "login_expired")
        self.assertEqual(len(view["login_events"]), 1)
        self.assertEqual(view["login_events"][0]["record_label"], "登入失效")
        self.assertEqual(view["login_events"][0]["status_label"], "登入失效")
        self.assertEqual(view["login_events"][0]["status_class"], "failed")

    def test_admin_view_legacy_login_error_event_is_visible(self):
        event = {
            "event_id": "evt-legacy-login-expired",
            "occurred_at": "2026-06-18T18:20:00",
            "last_occurred_at": "2026-06-18T18:20:00",
            "record_type": "error",
            "trigger_type": "login",
            "status": "failed",
            "actor_no": "4",
            "display_name": "4番 隊員 測試",
            "error": "勤務系統登入失效，請重新登入。",
        }

        view = build_sinposmart_admin_view([event])

        self.assertEqual(len(view["login_events"]), 1)
        self.assertEqual(view["login_events"][0]["record_label"], "登入失效")
        self.assertEqual(view["login_events"][0]["status_label"], "登入失效")
        self.assertEqual(view["login_events"][0]["status_class"], "failed")

    def test_admin_view_login_cards_keep_their_own_event_time(self):
        events = [
            normalize_sinposmart_event(
                {
                    "event_id": "evt-login-time",
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
                    "event_id": "evt-logout-time",
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
        logout_card = view["login_events"][0]
        login_card = view["login_events"][1]

        self.assertEqual(login_card["login_at"], "2026-06-18T16:30:40")
        self.assertEqual(login_card["logout_at"], "")
        self.assertEqual(logout_card["login_at"], "")
        self.assertEqual(logout_card["logout_at"], "2026-06-18T18:05:12")
        self.assertEqual([step["label"] for step in login_card["steps"]], ["登入時間"])
        self.assertEqual([step["label"] for step in logout_card["steps"]], ["登出時間"])

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

    def test_admin_view_keeps_only_active_unreturned_return_records(self):
        def event(event_id, occurred_at, status, queue_id, *, retry_minutes):
            return normalize_sinposmart_event(
                {
                    "event_id": event_id,
                    "occurred_at": occurred_at,
                    "record_type": "unreturned_return",
                    "trigger_type": "recovery",
                    "status": status,
                    "item_kind": "出入",
                    "item_title": "出 / 退勤",
                    "target": "08 測試員",
                    "target_time": "08:05",
                    "snapshot": {
                        "queue_id": queue_id,
                        "first_paused_at": "2026-08-07T08:00:00",
                        "last_attempt_at": occurred_at,
                        "next_retry_at": "2026-08-07T08:15:00",
                        "expires_at": "2026-08-08T02:00:00",
                        "last_owner_actor_no": "11",
                        "retry_interval_minutes": retry_minutes,
                    },
                },
                now=datetime(2026, 8, 7, 8, 0),
            )

        view = build_sinposmart_admin_view(
            [
                event("evt-paused", "2026-08-07T08:00:00", "pending", "queue-resolved", retry_minutes=5),
                event("evt-resolved", "2026-08-07T08:06:00", "resolved", "queue-resolved", retry_minutes=5),
                event("evt-retrying", "2026-08-07T08:10:00", "retrying", "queue-active", retry_minutes=10),
            ]
        )

        self.assertEqual(view["summary"]["unreturned_returns"], 1)
        self.assertEqual(len(view["unreturned_return_events"]), 1)
        card = view["unreturned_return_events"][0]
        self.assertEqual(card["target"], "08 測試員")
        self.assertEqual(card["item_title"], "08:05｜出入｜出 / 退勤｜08番 測試員")
        self.assertEqual(card["status_label"], "重新確認中")
        self.assertEqual(card["retry_interval_minutes"], 10)
        self.assertEqual(card["owner_actor_no"], "11")
        self.assertTrue(view["unreturned_return_history_events"][0]["is_active"])
        self.assertFalse(view["unreturned_return_history_events"][0]["has_handoff_context"])
        self.assertFalse(view["unreturned_return_history_events"][0]["missing_task_details"])

    def test_admin_view_labels_legacy_unreturned_return_without_task_details(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-unreturned-legacy",
                "occurred_at": "2026-08-12T10:00:02",
                "record_type": "unreturned_return",
                "trigger_type": "due",
                "status": "resolved",
                "snapshot": {
                    "queue_id": "queue-legacy",
                    "first_paused_at": "2026-08-12T10:00:02",
                },
            },
            now=datetime(2026, 8, 12, 10, 0),
        )

        view = build_sinposmart_admin_view([event])

        card = view["unreturned_return_history_events"][0]
        self.assertEqual(card["item_title"], "10:00｜未返隊｜舊版事件未提供勤務明細")
        self.assertTrue(card["missing_task_details"])
        self.assertFalse(card["has_handoff_context"])

    def test_admin_view_marks_partial_legacy_task_details_as_missing(self):
        event = normalize_sinposmart_event(
            {
                "event_id": "evt-unreturned-partial",
                "occurred_at": "2026-08-12T10:00:02",
                "record_type": "unreturned_return",
                "trigger_type": "due",
                "status": "resolved",
                "target_time": "10:00",
                "target": "21",
                "snapshot": {"queue_id": "queue-partial"},
            },
            now=datetime(2026, 8, 12, 10, 0),
        )

        view = build_sinposmart_admin_view([event])

        card = view["unreturned_return_history_events"][0]
        self.assertEqual(card["item_title"], "10:00｜未返隊暫停｜21番")
        self.assertTrue(card["missing_task_details"])

    def test_admin_view_orders_active_unreturned_returns_by_next_retry(self):
        def event(event_id, status, first_paused_at, next_retry_at):
            return normalize_sinposmart_event(
                {
                    "event_id": event_id,
                    "occurred_at": first_paused_at,
                    "record_type": "unreturned_return",
                    "trigger_type": "recovery",
                    "status": status,
                    "item_kind": "出入",
                    "item_title": "入 / 返隊",
                    "target": "21 張家和",
                    "target_time": "20:01",
                    "snapshot": {
                        "queue_id": event_id,
                        "first_paused_at": first_paused_at,
                        "next_retry_at": next_retry_at,
                    },
                },
                now=datetime.fromisoformat(first_paused_at),
            )

        view = build_sinposmart_admin_view(
            [
                event("queue-later", "retrying", "2026-08-12T10:10:00", "2026-08-12T10:20:00"),
                event("queue-earlier", "pending", "2026-08-12T10:00:00", "2026-08-12T10:15:00"),
                event("queue-login", "pending", "2026-08-12T09:55:00", ""),
                event("queue-resolved", "resolved", "2026-08-12T11:00:00", ""),
            ]
        )

        cards = view["unreturned_return_history_events"]
        self.assertEqual([card["is_active"] for card in cards], [True, True, True, False])
        self.assertEqual(
            [card["next_retry_at"] for card in cards[:3]],
            ["", "2026-08-12T10:15:00", "2026-08-12T10:20:00"],
        )

    def test_admin_view_fills_handoff_people_from_later_snapshot(self):
        def event(event_id, occurred_at, snapshot):
            return normalize_sinposmart_event(
                {
                    "event_id": event_id,
                    "occurred_at": occurred_at,
                    "record_type": "unreturned_return",
                    "trigger_type": "recovery",
                    "status": "resolved",
                    "item_kind": "出入",
                    "item_title": "值班交接",
                    "target": "7 原值退",
                    "target_time": "10:00",
                    "snapshot": {"queue_id": "queue-handoff", "handoff": snapshot},
                },
                now=datetime.fromisoformat(occurred_at),
            )

        view = build_sinposmart_admin_view(
            [
                event(
                    "evt-handoff-first",
                    "2026-08-12T10:00:00",
                    {"original_handoff_time": "10:00"},
                ),
                event(
                    "evt-handoff-later",
                    "2026-08-12T10:05:00",
                    {
                        "outgoing_person": "7番 原值退",
                        "scheduled_incoming_person": "8番 表定接班",
                    },
                ),
            ]
        )

        card = view["unreturned_return_history_events"][0]
        self.assertEqual(card["outgoing_person"], "7番 原值退")
        self.assertEqual(card["scheduled_incoming_person"], "8番 表定接班")
        self.assertTrue(card["has_handoff_context"])


if __name__ == "__main__":
    unittest.main()
