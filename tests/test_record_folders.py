import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ambulance_bot.models import AmbulanceReturnRequest, VehicleEntry
from ambulance_bot.record_folders import RecordFolderError, disaster_folder_plan, ensure_disaster_record_folders


class RecordFolderTests(unittest.TestCase):
    def disaster_request(self, **changes):
        values = {
            "task_id": "task-1",
            "created_at": datetime(2026, 7, 22, 12, 7),
            "raw_text": "",
            "service_type": "disaster",
            "case_date": "2026/07/21",
            "case_time": "1207",
            "case_address": "桃園市觀音區金華路31號",
            "case_reason": "一般(集合)住宅",
            "recorder_category": "轄內A3",
            "vehicle_entries": [
                VehicleEntry(vehicle="新坡11", driver="甲"),
                VehicleEntry(vehicle="新坡15", driver="乙"),
            ],
        }
        values.update(changes)
        return AmbulanceReturnRequest(**values)

    def test_disaster_other_case_uses_roc_year_subcategory_and_each_vehicle(self):
        request = self.disaster_request(recorder_category="轄內其他案件", recorder_subcategory="破門")

        plan = disaster_folder_plan(request, Path("X:/records"))

        self.assertEqual(
            [
                Path("X:/records/115年/轄內其他案件/破門/202607211207桃園市觀音區金華路31號(破門)-11"),
                Path("X:/records/115年/轄內其他案件/破門/202607211207桃園市觀音區金華路31號(破門)-15"),
            ],
            [item.path for item in plan],
        )

    def test_disaster_a2_display_maps_to_existing_a2_directory(self):
        request = self.disaster_request(recorder_category="轄內A2")
        path = disaster_folder_plan(request, Path("X:/records"))[0].path
        self.assertEqual(("115年", "A2"), path.parts[-3:-1])
        self.assertNotIn("轄內A2", str(path))

    def test_existing_disaster_directory_is_reused_without_copy_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = self.disaster_request(vehicle_entries=[VehicleEntry(vehicle="新坡11", driver="甲")])
            target = disaster_folder_plan(request, Path(tmp))[0].path
            target.mkdir(parents=True)

            results = ensure_disaster_record_folders(request, Path(tmp))

            self.assertEqual("reused", results[0].status)
            self.assertEqual(target, results[0].path)
            self.assertFalse(target.with_name(target.name + " (複製)").exists())

    def test_disaster_folder_uses_configured_recorder_code(self):
        request = self.disaster_request(vehicle_entries=[VehicleEntry(vehicle="新坡15", driver="甲")])

        path = disaster_folder_plan(request, Path("X:/records"), {"新坡15": "CAM15"})[0].path

        self.assertTrue(path.name.endswith("-CAM15"))

    def test_disaster_folder_creation_preflights_all_targets_before_creating_any(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = self.disaster_request()
            plan = disaster_folder_plan(request, root)
            plan[1].path.parent.mkdir(parents=True)
            plan[1].path.write_text("collision", encoding="utf-8")

            with self.assertRaises(RecordFolderError):
                ensure_disaster_record_folders(request, root)

            self.assertFalse(plan[0].path.exists())


if __name__ == "__main__":
    unittest.main()
