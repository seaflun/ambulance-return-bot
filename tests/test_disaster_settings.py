import json
import tempfile
import unittest
from pathlib import Path

from ambulance_bot.disaster_settings import (
    delete_disaster_vehicle_record,
    disaster_vehicle_options,
    disaster_vehicle_recorder_codes,
    load_disaster_vehicle_records,
    load_disaster_action_packages,
    save_disaster_vehicle_record,
    save_disaster_action_packages,
)


class DisasterSettingsTests(unittest.TestCase):
    def test_defaults_include_known_disaster_vehicles_and_recorder_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = load_disaster_vehicle_records(Path(tmp))

        self.assertEqual(["新坡11", "新坡15", "新坡85"], [item["label"] for item in records])
        self.assertEqual("15", records[1]["recorder_code"])
        self.assertEqual("85", records[2]["recorder_code"])

    def test_current_rescue_vehicles_are_built_in_and_only_new_custom_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            self.assertTrue(
                all(record["vehicle_type"] == "內建" for record in load_disaster_vehicle_records(base_dir))
            )
            save_disaster_vehicle_record("新坡15", "FIRE-15", "CAM15", base_dir, vehicle_type="自訂")

            self.assertEqual("CAM15", disaster_vehicle_recorder_codes(base_dir)["新坡15"])
            self.assertIn("新坡15", disaster_vehicle_options(base_dir))
            newpo_15 = next(
                record for record in load_disaster_vehicle_records(base_dir) if record["label"] == "新坡15"
            )
            self.assertEqual("內建", newpo_15["vehicle_type"])
            self.assertFalse(delete_disaster_vehicle_record("新坡15", base_dir))
            save_disaster_vehicle_record("測試自訂救災車", "CUSTOM-FIRE", "CAM-CUSTOM", base_dir)
            custom = next(
                record for record in load_disaster_vehicle_records(base_dir) if record["label"] == "測試自訂救災車"
            )
            self.assertEqual("自訂", custom["vehicle_type"])
            self.assertTrue(delete_disaster_vehicle_record("測試自訂救災車", base_dir))
            self.assertNotIn("測試自訂救災車", disaster_vehicle_options(base_dir))

    def test_legacy_deleted_builtin_rescue_vehicle_is_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            settings_path = base_dir / "settings" / "disaster_vehicles.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps({"vehicles": [], "deleted": ["新坡15"]}, ensure_ascii=False),
                encoding="utf-8",
            )

            self.assertIn("新坡15", disaster_vehicle_options(base_dir))
            save_disaster_vehicle_record("新坡15", "FIRE-15", "15", base_dir)

            persisted = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertNotIn("新坡15", persisted["deleted"])

    def test_action_packages_can_be_saved_without_overwriting_vehicle_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            save_disaster_vehicle_record("新坡15", "FIRE-15", "CAM15", base_dir)
            save_disaster_action_packages(["先鋒搶救", "現場待命", "先鋒搶救"], base_dir)

            self.assertEqual(["先鋒搶救", "現場待命"], load_disaster_action_packages(base_dir))
            self.assertEqual("CAM15", disaster_vehicle_recorder_codes(base_dir)["新坡15"])


if __name__ == "__main__":
    unittest.main()
