from datetime import datetime
import unittest

from ambulance_bot.daily_vehicle_mileage import (
    DAILY_VEHICLE_MILEAGE_FAILED,
    DAILY_VEHICLE_MILEAGE_SYNCED,
    daily_vehicle_targets,
    merge_vehicle_mileage_report,
    vehicle_mileage_sync_due,
)


class DailyVehicleMileageTests(unittest.TestCase):
    def test_targets_merge_ems_and_disaster_records_by_ppe_name(self):
        targets = daily_vehicle_targets(
            {
                "ems_vehicles": [
                    {"label": "新坡91", "ppe_name": "KEC-2608"},
                    {"label": "新坡92", "ppe_name": ""},
                ],
                "disaster_vehicles": [
                    {"label": "救災91", "ppe_name": "KEC-2608"},
                    {"label": "救災11", "ppe_name": "FIRE-11"},
                ],
            }
        )

        self.assertEqual(
            [
                {"vehicle_key": "FIRE-11", "ppe_name": "FIRE-11", "labels": ["救災11"]},
                {"vehicle_key": "KEC-2608", "ppe_name": "KEC-2608", "labels": ["新坡91", "救災91"]},
            ],
            targets,
        )

    def test_failed_report_preserves_last_successful_mileage_and_is_retryable_after_thirty_minutes(self):
        existing = {
            "vehicles": [
                {
                    "vehicle_key": "KEC-2608",
                    "ppe_name": "KEC-2608",
                    "mileage": "12345",
                    "status": DAILY_VEHICLE_MILEAGE_SYNCED,
                    "last_success_at": "2026-08-21T06:03:00",
                    "last_success_business_date": "2026-08-21",
                }
            ]
        }
        report = {
            "business_date": "2026-08-22",
            "attempted_at": "2026-08-22T06:00:00",
            "vehicles": [
                {
                    "vehicle_key": "KEC-2608",
                    "ppe_name": "KEC-2608",
                    "status": DAILY_VEHICLE_MILEAGE_FAILED,
                    "detail": "PPE 登入失敗",
                }
            ],
        }

        merged = merge_vehicle_mileage_report(existing, report)
        record = merged["vehicles"][0]

        self.assertEqual(DAILY_VEHICLE_MILEAGE_FAILED, record["status"])
        self.assertEqual("12345", record["mileage"])
        self.assertEqual("2026-08-21T06:03:00", record["last_success_at"])
        self.assertFalse(
            vehicle_mileage_sync_due(
                merged,
                {"vehicle_key": "KEC-2608", "ppe_name": "KEC-2608", "labels": ["新坡91"]},
                now=datetime(2026, 8, 22, 5, 59),
            )
        )
        self.assertFalse(
            vehicle_mileage_sync_due(
                merged,
                {"vehicle_key": "KEC-2608", "ppe_name": "KEC-2608", "labels": ["新坡91"]},
                now=datetime(2026, 8, 22, 6, 29),
            )
        )
        self.assertTrue(
            vehicle_mileage_sync_due(
                merged,
                {"vehicle_key": "KEC-2608", "ppe_name": "KEC-2608", "labels": ["新坡91"]},
                now=datetime(2026, 8, 22, 6, 30),
            )
        )


if __name__ == "__main__":
    unittest.main()
