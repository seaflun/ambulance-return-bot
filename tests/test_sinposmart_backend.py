import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ambulance_bot.sinposmart_backend import (
    SinpoSmartBackendStore,
    normalize_sinposmart_event,
    sinposmart_fire_day_for,
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


if __name__ == "__main__":
    unittest.main()
