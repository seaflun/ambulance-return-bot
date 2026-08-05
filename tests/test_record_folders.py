import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ambulance_bot.models import AmbulanceReturnRequest, VehicleEntry
from ambulance_bot import record_folders
from ambulance_bot.record_folders import (
    RecordFolderError,
    disaster_folder_plan,
    ensure_disaster_record_folders,
    ensure_disaster_media_folders,
    firecam_folder_plan,
)


class RecordFolderTests(unittest.TestCase):
    def test_disaster_record_root_defaults_to_public_duty_w_drive(self):
        with patch.dict("os.environ", {"DISASTER_RECORD_ROOT": ""}, clear=False):
            self.assertEqual(
                Path(r"W:\搶救災害硬碟\救災行車紀錄器"),
                record_folders.disaster_record_root(),
            )

    def test_firecam_record_root_defaults_to_public_duty_w_drive(self):
        with patch.dict("os.environ", {"FIRECAM_RECORD_ROOT": ""}, clear=False):
            self.assertEqual(
                Path(r"W:\搶救災害硬碟\fire cam"),
                record_folders.firecam_record_root(),
            )

    def disaster_request(self, **changes):
        values = {
            "task_id": "task-1",
            "created_at": datetime(2026, 7, 22, 12, 7),
            "raw_text": "",
            "service_type": "disaster",
            "case_date": "2026/07/21",
            "case_time": "1207",
            "case_address": "桃園市觀音區金華路31號",
            "summary_type": "火災",
            "duty_item": "火警",
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

    def test_fire_false_alarm_supporting_jurisdiction_folder_uses_false_alarm_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = self.disaster_request(
                case_reason="誤(謊)報",
                recorder_category="支援他轄",
                vehicle_entries=[VehicleEntry(vehicle="新坡11", driver="甲")],
            )

            result = ensure_disaster_record_folders(request, Path(tmp))[0]

            self.assertEqual("created", result.status)
            self.assertEqual(("115年", "支援他轄"), result.path.parts[-3:-1])
            self.assertIn("(誤報)-11", result.path.name)
            self.assertTrue(result.path.is_dir())

    def test_fire_false_alarm_local_other_case_uses_fixed_false_alarm_subcategory(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = self.disaster_request(
                case_reason="誤(謊)報",
                recorder_category="轄內其他案件",
                recorder_subcategory="誤報",
                vehicle_entries=[VehicleEntry(vehicle="新坡11", driver="甲")],
            )

            result = ensure_disaster_record_folders(request, Path(tmp))[0]

            self.assertEqual(("115年", "轄內其他案件", "誤報"), result.path.parts[-4:-1])
            self.assertIn("(誤報)-11", result.path.name)

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

    def test_firecam_folder_uses_selected_people_under_the_matching_category(self):
        request = self.disaster_request(firecam_people=["曾彥綸", "王治任"])

        plan = firecam_folder_plan(request, Path("X:/firecam"))

        self.assertEqual(
            [
                Path("X:/firecam/115年/轄內A3/202607211207桃園市觀音區金華路31號(住宅火警)-曾彥綸"),
                Path("X:/firecam/115年/轄內A3/202607211207桃園市觀音區金華路31號(住宅火警)-王治任"),
            ],
            [item.path for item in plan],
        )

    def test_disaster_rescue_folder_uses_reason_without_fire_suffix(self):
        request = self.disaster_request(
            summary_type="災害搶救",
            duty_item="其他類災害",
            case_reason="溺水",
        )

        path = disaster_folder_plan(request, Path("X:/records"))[0].path

        self.assertTrue(path.name.endswith("(溺水)-11"))
        self.assertNotIn("溺水火警", path.name)

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

    def test_disaster_media_creation_preflights_firecam_before_creating_recorder_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = self.disaster_request(firecam_people=["曾彥綸"])
            recorder_plan = disaster_folder_plan(request, root / "recorder")
            firecam_plan = firecam_folder_plan(request, root / "firecam")
            firecam_plan[0].path.parent.mkdir(parents=True)
            firecam_plan[0].path.write_text("collision", encoding="utf-8")

            with self.assertRaises(RecordFolderError):
                ensure_disaster_media_folders(
                    request,
                    disaster_root=root / "recorder",
                    firecam_root=root / "firecam",
                )

            self.assertFalse(recorder_plan[0].path.exists())


if __name__ == "__main__":
    unittest.main()
