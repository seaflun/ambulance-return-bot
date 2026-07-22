import tempfile
import unittest
from pathlib import Path

from ambulance_bot.disaster_settings import (
    delete_disaster_vehicle_record,
    disaster_vehicle_options,
    disaster_vehicle_recorder_codes,
    load_disaster_vehicle_records,
    save_disaster_vehicle_record,
)


class DisasterSettingsTests(unittest.TestCase):
    def test_defaults_include_known_disaster_vehicles_and_recorder_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = load_disaster_vehicle_records(Path(tmp))

        self.assertEqual(["新坡11", "新坡15", "新坡16", "新坡91", "新坡92", "新坡93"], [item["label"] for item in records])
        self.assertEqual("15", records[1]["recorder_code"])

    def test_save_updates_recorder_code_and_delete_hides_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            save_disaster_vehicle_record("新坡15", "FIRE-15", "CAM15", base_dir)

            self.assertEqual("CAM15", disaster_vehicle_recorder_codes(base_dir)["新坡15"])
            self.assertIn("新坡15", disaster_vehicle_options(base_dir))
            self.assertTrue(delete_disaster_vehicle_record("新坡15", base_dir))
            self.assertNotIn("新坡15", disaster_vehicle_options(base_dir))


if __name__ == "__main__":
    unittest.main()
