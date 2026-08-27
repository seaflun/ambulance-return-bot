import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import tempfile
from werkzeug.datastructures import MultiDict

from ambulance_bot.models import apply_disaster_vehicle_mileage_system_names, clean_case_address, parse_case_date, parse_consumables, parse_request, patient_counts_from_summary, patient_summary_from_counts, request_from_disaster_form, request_from_form
from ambulance_bot.models import AmbulanceReturnRequest, VehicleEntry
from ambulance_bot.models import delete_vehicle_record, load_vehicle_records, save_vehicle_record, vehicle_options, vehicle_ppe_names


class ModelParsingTests(unittest.TestCase):
    def test_fire_false_alarm_preserves_selected_recorder_category(self):
        local_false_alarm_request = request_from_disaster_form(
            MultiDict(
                [
                    ("summary_type", "火災"),
                    ("case_reason", "誤(謊)報"),
                    ("recorder_category", "轄內其他案件"),
                    ("recorder_subcategory", "其他"),
                ]
            )
        )
        support_false_alarm_request = request_from_disaster_form(
            MultiDict(
                [
                    ("summary_type", "火災"),
                    ("case_reason", "誤(謊)報"),
                    ("recorder_category", "支援他轄"),
                    ("recorder_subcategory", "其他"),
                ]
            )
        )

        self.assertEqual("轄內其他案件", local_false_alarm_request.recorder_category)
        self.assertEqual("誤報", local_false_alarm_request.recorder_subcategory)
        self.assertEqual("支援他轄", support_false_alarm_request.recorder_category)
        self.assertEqual("", support_false_alarm_request.recorder_subcategory)

    def test_disaster_work_record_matches_processing_preview(self):
        request = AmbulanceReturnRequest(
            task_id="DISASTER-1",
            created_at=datetime(2026, 7, 27, 9, 20),
            raw_text="",
            service_type="disaster",
            vehicle_entries=[VehicleEntry(vehicle="新坡11", driver="甲")],
            commander="乙",
            team_leader="丙",
            action_note="處理完成",
        )

        self.assertEqual(
            "1.新坡11司機:甲、指揮官:乙\n2.處理完成",
            request.duty_status_text,
        )

    def test_disaster_work_record_matches_processing_preview_fallback(self):
        request = AmbulanceReturnRequest(
            task_id="DISASTER-2",
            created_at=datetime(2026, 7, 27, 9, 20),
            raw_text="",
            service_type="disaster",
            vehicle_entries=[VehicleEntry(vehicle="新坡11"), VehicleEntry(vehicle="新坡15", driver="乙")],
            action_note="現場待命",
        )

        self.assertEqual(
            "1.新坡15司機:乙\n2.現場待命",
            request.duty_status_text,
        )

        request.vehicle_entries = [VehicleEntry()]
        request.action_note = ""
        self.assertEqual(
            "1.尚未選擇車輛／司機／指揮官\n2.",
            request.duty_status_text,
        )

    def test_disaster_form_persists_team_leader(self):
        request = request_from_disaster_form(
            MultiDict(
                [
                    ("summary_type", "火災"),
                    ("case_reason", "汽機車"),
                    ("commander", "甲"),
                    ("team_leader", "乙"),
                ]
            )
        )

        restored = AmbulanceReturnRequest.from_dict(request.to_dict())

        self.assertEqual("乙", restored.team_leader)

    def test_disaster_form_parses_n_vehicle_entries_and_active_sites(self):
        request = request_from_disaster_form(
            MultiDict(
                [
                    ("case_id", "CASE-1"),
                    ("case_date", "2026/07/22"),
                    ("case_time", "1207"),
                    ("return_date", "2026/07/23"),
                    ("return_time", "1300"),
                    ("case_address", "桃園市觀音區金華路31號"),
                    ("summary_type", "災害搶救"),
                    ("case_reason", "一般(集合)住宅"),
                    ("commander", "王小明"),
                    ("action_note", "現場待命"),
                    ("recorder_category", "轄內A3"),
                    ("vehicle", "新坡11"),
                    ("driver", "甲"),
                    ("mileage", "100"),
                    ("vehicle_return_date", "2026/07/23"),
                    ("vehicle_return_time", "1300"),
                    ("vehicle", "新坡15"),
                    ("driver", "乙"),
                    ("mileage", "200"),
                    ("vehicle_return_date", "2026/07/24"),
                    ("vehicle_return_time", "1310"),
                ]
            )
        )

        self.assertEqual("disaster", request.service_type)
        self.assertEqual(["新坡11", "新坡15"], [item.vehicle for item in request.vehicle_entries])
        self.assertEqual("災害搶救", request.summary_type)
        self.assertEqual("其他類災害", request.duty_item)
        self.assertEqual(["2026/07/23", "2026/07/24"], [item.return_date for item in request.vehicle_entries])
        self.assertEqual(["1300", "1310"], [item.return_time for item in request.vehicle_entries])
        self.assertEqual(["duty_work_log", "vehicle_mileage"], request.active_site_keys())

    def test_ems_form_maps_case_type_to_duty_item_and_keeps_reason(self):
        request = request_from_form(
            {
                "summary_type": "災害搶救",
                "case_reason": "溺水",
            }
        )

        self.assertEqual("災害搶救", request.summary_type)
        self.assertEqual("其他類災害", request.duty_item)
        self.assertEqual("溺水", request.case_reason)

    def test_ems_volunteer_assist_adds_site_and_duty_status_line(self):
        request = request_from_form(
            {
                "vehicle": "新坡91",
                "driver": "測試司機",
                "patient_summary": "男一名",
                "volunteer_assist": "1",
                "volunteer_assist_member_id": "VOL-001",
                "volunteer_assist_member_name": "測試義消",
                "volunteer_assist_member_title": "隊員",
                "volunteer_assist_member_unit": "大園救護分隊",
            }
        )

        self.assertTrue(request.volunteer_assist)
        self.assertEqual("VOL-001", request.volunteer_assist_member_id)
        self.assertEqual("測試義消", request.volunteer_assist_member_name)
        self.assertEqual(
            "1.新坡91:測試司機\n2.男一名\n3.救護義消協勤:測試義消",
            request.duty_status_text,
        )
        self.assertIn("volunteer_assist", request.active_site_keys())

    def test_disabled_volunteer_assist_drops_stale_person(self):
        request = request_from_form(
            {
                "volunteer_assist": "",
                "volunteer_assist_member_id": "VOL-001",
                "volunteer_assist_member_name": "測試義消",
            }
        )

        self.assertFalse(request.volunteer_assist)
        self.assertEqual("", request.volunteer_assist_member_id)
        self.assertEqual("", request.volunteer_assist_member_name)
        self.assertNotIn("volunteer_assist", request.active_site_keys())

    def test_disaster_form_uses_top_return_date_for_vehicle_default(self):
        request = request_from_disaster_form(
            MultiDict(
                [
                    ("case_date", "2026/07/22"),
                    ("return_date", "2026/07/23"),
                    ("return_time", "0030"),
                    ("vehicle", "新坡11"),
                    ("driver", "甲"),
                    ("mileage", "100"),
                ]
            )
        )

        self.assertEqual("2026/07/23", request.vehicle_entries[0].return_date)

    def test_disaster_form_keeps_selected_firecam_people(self):
        request = request_from_disaster_form(
            MultiDict(
                [
                    ("case_id", "CASE-FIRECAM"),
                    ("firecam_person", "甲"),
                    ("firecam_person", "乙"),
                ]
            )
        )

        self.assertEqual(["甲", "乙"], request.firecam_people)

    def test_disaster_vehicle_requests_keep_each_mileage_system_name(self):
        request = AmbulanceReturnRequest(
            task_id="task-disaster-mileage-names",
            created_at=datetime.now(),
            raw_text="",
            service_type="disaster",
            vehicle_entries=[
                VehicleEntry(vehicle="新坡11", mileage_system_name="KEC-2608"),
                VehicleEntry(vehicle="新坡15", mileage_system_name="KES-5922"),
            ],
        )

        self.assertEqual(
            ["KEC-2608", "KES-5922"],
            [item.mileage_system_name for item in request.vehicle_requests()],
        )

    def test_disaster_vehicle_settings_apply_to_any_task_vehicle(self):
        request = AmbulanceReturnRequest(
            task_id="task-disaster-new-vehicle",
            created_at=datetime.now(),
            raw_text="",
            service_type="disaster",
            vehicle_entries=[VehicleEntry(vehicle="新坡99")],
        )

        changed = apply_disaster_vehicle_mileage_system_names(
            request,
            [{"label": "新坡99", "ppe_name": "NEW-9900", "recorder_code": "99"}],
        )

        self.assertTrue(changed)
        self.assertEqual("NEW-9900", request.vehicle_entries[0].mileage_system_name)
        self.assertEqual("NEW-9900", request.mileage_system_name)
        self.assertEqual("NEW-9900", request.vehicle_requests()[0].mileage_system_name)

    def test_disaster_vehicle_settings_do_not_replace_existing_task_snapshot(self):
        request = AmbulanceReturnRequest(
            task_id="task-disaster-snapshot",
            created_at=datetime.now(),
            raw_text="",
            service_type="disaster",
            mileage_system_name="KEC-2608",
            vehicle_entries=[VehicleEntry(vehicle="新坡11", mileage_system_name="KEC-2608")],
        )

        changed = apply_disaster_vehicle_mileage_system_names(
            request,
            [{"label": "新坡11", "ppe_name": "KEC-9999", "recorder_code": "11"}],
        )

        self.assertFalse(changed)
        self.assertEqual("KEC-2608", request.vehicle_entries[0].mileage_system_name)
        self.assertEqual("KEC-2608", request.mileage_system_name)

    def test_disaster_work_log_login_prefers_15_then_11_then_other_personnel(self):
        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=datetime.now(),
            raw_text="",
            service_type="disaster",
            personnel=["甲", "乙", "丙"],
            personnel_accounts=["TYFD-A", "TYFD-B", "TYFD-C"],
            vehicle_entries=[
                {"vehicle": "新坡11", "driver": "甲"},
                {"vehicle": "新坡15", "driver": "乙"},
            ],
        )

        self.assertEqual(["TYFD-B", "TYFD-A", "TYFD-C"], request.duty_login_account_candidates)

    def test_disaster_form_keeps_repeated_vehicle_fields_aligned_when_middle_value_is_blank(self):
        request = request_from_disaster_form(
            MultiDict(
                [
                    ("case_date", "2026/07/22"),
                    ("return_time", "1300"),
                    ("vehicle", "新坡11"),
                    ("vehicle", "新坡15"),
                    ("vehicle", "新坡16"),
                    ("driver", "甲"),
                    ("driver", ""),
                    ("driver", "丙"),
                    ("mileage", "100"),
                    ("mileage", ""),
                    ("mileage", "300"),
                    ("vehicle_return_time", "1300"),
                    ("vehicle_return_time", ""),
                    ("vehicle_return_time", "1320"),
                ]
            )
        )

        self.assertEqual(["甲", "", "丙"], [item.driver for item in request.vehicle_entries])
        self.assertEqual(["100", "", "300"], [item.mileage for item in request.vehicle_entries])
        self.assertEqual(["1300", "1300", "1320"], [item.return_time for item in request.vehicle_entries])

    def test_default_consumables(self):
        request = parse_request("\u6551\u8b77\u56de\u7a0b\n\u8eca\u8f1b:91A1")

        self.assertEqual(request.vehicle, "91A1")
        self.assertEqual(
            request.consumables,
            {"桃-口罩(片)": 2, "桃-9吋手套-L(雙)": 2, "桃-可拋棄式耳溫槍耳套-福爾TD-1118(個)": 1},
        )
        self.assertEqual(request.patient_summary, "\u7537\u4e00\u540d")
        self.assertEqual(
            request.disinfection_items,
            [
                "救護車體",
                "擔架床",
                "擔架床墊",
                "攜帶式氧氣組(含內容物)",
                "急救箱/急救包",
                "血氧濃度分析儀",
                "體溫計",
                "血壓計",
            ],
        )

    def test_vehicle_options_include_borrowed_ambulance(self):
        self.assertIn("新坡95", vehicle_options())
        self.assertEqual(vehicle_ppe_names()["新坡95"], "CDD-2171")

    def test_current_ambulances_are_built_in_and_only_new_custom_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)

            self.assertIn("新坡95", vehicle_options(base_dir))
            self.assertTrue(
                all(record["vehicle_type"] == "內建" for record in load_vehicle_records(base_dir))
            )
            save_vehicle_record("新坡95", "CDD-9595", base_dir, vehicle_type="自訂")
            newpo_95 = next(record for record in load_vehicle_records(base_dir) if record["label"] == "新坡95")
            self.assertEqual("內建", newpo_95["vehicle_type"])
            self.assertFalse(delete_vehicle_record("新坡95", base_dir))
            self.assertFalse(delete_vehicle_record("新坡91", base_dir))
            self.assertIn("新坡91", vehicle_options(base_dir))
            save_vehicle_record("測試自訂救護車", "CUSTOM-EMS", base_dir)
            custom = next(
                record for record in load_vehicle_records(base_dir) if record["label"] == "測試自訂救護車"
            )
            self.assertEqual("自訂", custom["vehicle_type"])
            self.assertTrue(delete_vehicle_record("測試自訂救護車", base_dir))
            self.assertNotIn("測試自訂救護車", vehicle_options(base_dir))

    def test_legacy_deleted_builtin_ambulance_is_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            settings_path = base_dir / "settings" / "vehicles.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps({"vehicles": [], "deleted": ["新坡95"]}, ensure_ascii=False),
                encoding="utf-8",
            )

            self.assertIn("新坡95", vehicle_options(base_dir))
            save_vehicle_record("新坡95", "CDD-2171", base_dir)

            persisted = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertNotIn("新坡95", persisted["deleted"])

    def test_concurrent_vehicle_settings_updates_do_not_lose_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            labels = [f"測試車{index:02d}" for index in range(40)]

            try:
                with ThreadPoolExecutor(max_workers=16) as pool:
                    list(pool.map(lambda label: save_vehicle_record(label, f"PLATE-{label}", base_dir), labels))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                self.fail(f"concurrent vehicle settings writes must remain readable: {exc}")

            saved_labels = {record["label"] for record in load_vehicle_records(base_dir)}
            self.assertTrue(set(labels).issubset(saved_labels))

    def test_parse_full_request(self):
        request = parse_request(
            "\u6551\u8b77\u56de\u7a0b\n"
            "\u8eca\u8f1b:91A1\n"
            "\u53f8\u6a5f:\u66fe\u5f65\u7db8\n"
            "\u91cc\u7a0b:12345\n"
            "\u6848\u4ef6\u6642\u9593:1420\n"
            "\u56de\u7a0b\u6642\u9593:1505\n"
            "\u4e8b\u7531:\u6025\u75c5\n"
            "\u50b7\u75c5\u60a3:\u7537\u4e00\u540d\n"
            "\u8017\u6750:\u53e3\u7f69=2,\u624b\u5957=2,\u6c27\u6c23\u9762\u7f69=1\n"
            "\u6d88\u6bd2:\u5df2\u6d88\u6bd2\n"
            "\u5de5\u4f5c\u7d00\u9304:\u6551\u8b77\u8fd4\u968a"
        )

        self.assertEqual(request.vehicle, "91A1")
        self.assertEqual(request.driver, "\u66fe\u5f65\u7db8")
        self.assertEqual(request.mileage, "12345")
        self.assertEqual(request.case_time, "1420")
        self.assertEqual(request.return_time, "1505")
        self.assertEqual(request.case_reason, "\u6025\u75c5")
        self.assertEqual(request.consumables["\u6c27\u6c23\u9762\u7f69"], 1)
        self.assertEqual(request.disinfection, "\u5df2\u6d88\u6bd2")
        self.assertEqual(request.work_note, "\u6551\u8b77\u8fd4\u968a")
        self.assertEqual(request.duty_status_text, "1.91A1:\u66fe\u5f65\u7db8\n2.\u7537\u4e00\u540d")

    def test_request_from_form_parses_disinfection_items(self):
        request = request_from_form(
            {
                "case_id": "20260602011652012",
                "vehicle": "\u65b0\u576191",
                "driver": "\u66fe\u5f65\u7db8",
                "disinfection_items": ["\u6551\u8b77\u8eca\u9ad4", "\u64d4\u67b6\u5e8a"],
                "disinfection_items_custom": "\u81ea\u8a02\u9805\u76ee",
            }
        )

        self.assertEqual(request.case_id, "20260602011652012")
        self.assertEqual(request.disinfection_items, ["\u6551\u8b77\u8eca\u9ad4", "\u64d4\u67b6\u5e8a", "\u81ea\u8a02\u9805\u76ee"])

    def test_request_from_form_keeps_selected_duty_item(self):
        request = request_from_form(
            {
                "case_id": "20260725115356012",
                "duty_item": "其他類災害",
            }
        )

        self.assertEqual(request.duty_item, "其他類災害")

    def test_request_from_form_keeps_empty_consumables_and_disinfection_items(self):
        request = request_from_form({"vehicle": "\u65b0\u576191", "consumables": ""})

        self.assertEqual(request.consumables, {})
        self.assertEqual(request.disinfection_items, [])

    def test_request_from_form_keeps_empty_patient_summary(self):
        request = request_from_form({"patient_summary": ""})

        self.assertEqual(request.patient_summary, "")

    def test_patient_summary_from_counts_uses_numeric_gender_counts(self):
        self.assertEqual("無", patient_summary_from_counts("0", "0"))
        self.assertEqual("男1名", patient_summary_from_counts("1", "0"))
        self.assertEqual("女1名", patient_summary_from_counts("0", "1"))
        self.assertEqual("男2名、女1名", patient_summary_from_counts("2", "1"))

    def test_patient_counts_from_summary_keeps_old_and_new_formats(self):
        self.assertEqual((0, 0), patient_counts_from_summary("無"))
        self.assertEqual((1, 0), patient_counts_from_summary("男一名"))
        self.assertEqual((0, 2), patient_counts_from_summary("女二名"))
        self.assertEqual((2, 1), patient_counts_from_summary("男2名、女1名"))

    def test_request_from_form_composes_patient_summary_from_gender_counts(self):
        request = request_from_form(
            {
                "vehicle": "新坡91",
                "driver": "曾彥綸",
                "patient_male_count": "2",
                "patient_female_count": "1",
                "two_vehicle": "1",
                "vehicle_2": "新坡92",
                "driver_2": "陳小明",
                "patient_male_count_2": "0",
                "patient_female_count_2": "1",
            }
        )

        self.assertEqual("男2名、女1名", request.patient_summary)
        self.assertEqual("女1名", request.vehicle_entries[1].patient_summary)
        self.assertEqual(
            "1.新坡91:曾彥綸 新坡92:陳小明\n2.新坡91:男2名、女1名 新坡92:女1名",
            request.duty_status_text,
        )

    def test_request_from_form_normalizes_date_text(self):
        request = request_from_form({"case_date": "2026 / 06 / 07", "return_date": "2026-06-08"})

        self.assertEqual(request.case_date, "2026/06/07")
        self.assertEqual(request.return_date, "2026/06/08")

    def test_request_from_form_composes_refusal_summary_and_work_status(self):
        request = request_from_form(
            {
                "vehicle": "\u65b0\u576691",
                "driver": "\u66fe\u5f65\u7db8",
                "patient_male_count": "1",
                "patient_female_count": "0",
                "refusal_male_count": "0",
                "refusal_female_count": "1",
            }
        )

        self.assertEqual("\u75371\u540d", request.patient_summary)
        self.assertEqual("\u59731\u540d\u62d2\u9001", request.refusal_summary)
        restored = AmbulanceReturnRequest.from_dict(request.to_dict())
        self.assertEqual("\u59731\u540d\u62d2\u9001", restored.refusal_summary)
        self.assertEqual(
            "1.\u65b0\u576691:\u66fe\u5f65\u7db8\n2.\u75371\u540d\uff1b\u59731\u540d\u62d2\u9001",
            request.duty_status_text,
        )

    def test_all_patient_and_refusal_counts_zero_use_only_vehicle_driver(self):
        request = request_from_form(
            {
                "vehicle": "\u65b0\u576691",
                "driver": "\u66fe\u5f65\u7db8",
                "patient_male_count": "0",
                "patient_female_count": "0",
                "refusal_male_count": "0",
                "refusal_female_count": "0",
            }
        )

        self.assertEqual("\u7121", request.patient_summary)
        self.assertEqual("\u7121", request.refusal_summary)
        self.assertEqual("\u65b0\u576691:\u66fe\u5f65\u7db8", request.duty_status_text)

    def test_work_status_separates_multiple_send_and_refusal_genders(self):
        request = request_from_form(
            {
                "vehicle": "\u65b0\u576691",
                "driver": "\u66fe\u5f65\u7db8",
                "patient_male_count": "2",
                "patient_female_count": "1",
                "refusal_male_count": "1",
                "refusal_female_count": "3",
            }
        )

        self.assertEqual(
            "1.\u65b0\u576691:\u66fe\u5f65\u7db8\n2.\u75372\u540d\u3001\u59731\u540d\uff1b\u75371\u540d\u62d2\u9001\u3001\u59733\u540d\u62d2\u9001",
            request.duty_status_text,
        )

    def test_request_from_form_keeps_personnel_accounts_by_type(self):
        request = request_from_form(
            {
                "personnel": "Alice,Bob",
                "personnel_accounts": "B123017532,tyfd02317,L124961260",
            }
        )

        self.assertEqual(request.personnel, ["Alice", "Bob"])
        self.assertEqual(request.personnel_accounts, ["B123017532", "tyfd02317", "L124961260"])
        self.assertEqual(request.tyfd_personnel_accounts, ["tyfd02317"])
        self.assertEqual(request.consumables_account_candidates, ["B123017532", "L124961260"])

    def test_duty_login_account_candidates_put_driver_first(self):
        request = request_from_form(
            {
                "driver": "Bob",
                "personnel": "Alice,Bob,Carol",
                "personnel_accounts": "A123456789,B123456789,C123456789",
            }
        )

        self.assertEqual(request.duty_login_account_candidates, ["B123456789", "A123456789", "C123456789"])

    def test_ppe_login_account_candidates_split_driver_and_personnel(self):
        request = request_from_form(
            {
                "driver": "Bob",
                "personnel": "Alice,Bob,Carol",
                "personnel_accounts": "A123456789,B123456789,C123456789",
            }
        )

        self.assertEqual(request.driver_duty_login_account_candidates, ["B123456789", "Bob"])
        self.assertEqual(request.personnel_duty_login_account_candidates, ["A123456789", "C123456789", "Alice", "Carol"])

    def test_duty_login_account_candidates_falls_back_to_driver_name(self):
        request = request_from_form({"driver": "Bob", "personnel": "Alice,Carol"})

        self.assertEqual(request.duty_login_account_candidates, ["Bob"])
        self.assertEqual(request.driver_duty_login_account_candidates, ["Bob"])
        self.assertEqual(request.personnel_duty_login_account_candidates, ["Alice", "Carol"])

    def test_return_time_description_uses_mobile_hhmm_with_zero_seconds(self):
        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=datetime(2026, 6, 6, 18, 7, 0),
            raw_text="",
            return_time="1806",
        )

        self.assertEqual(request.return_time_hhmm, "1806")
        self.assertEqual(request.return_time_description_line, "\u8fd4\u968a\u6642\u9593:2026/06/06 18:06:00")

    def test_case_date_parses_roc_date_and_return_cross_day(self):
        request = request_from_form({"case_date": "1150606", "case_time": "2350", "return_time": "0010"})

        self.assertEqual(parse_case_date("1150606").strftime("%Y-%m-%d"), "2026-06-06")
        self.assertEqual(request.service_case_date().strftime("%Y-%m-%d"), "2026-06-06")
        self.assertEqual(request.service_return_date().strftime("%Y-%m-%d"), "2026-06-07")
        self.assertIn("2026/06/07 00:10:00", request.return_time_description_line)

    def test_explicit_return_date_overrides_cross_day_guess(self):
        request = request_from_form({"case_date": "2026-06-06", "return_date": "2026-06-06", "case_time": "2350", "return_time": "0010"})

        self.assertEqual(request.service_return_date().strftime("%Y-%m-%d"), "2026-06-06")

    def test_no_patient_uses_short_duty_status_text(self):
        request = request_from_form({"vehicle": "\u65b0\u576191", "driver": "\u66fe\u5f65\u7db8", "patient_summary": "\u7121"})

        self.assertEqual(request.duty_status_text, "\u65b0\u576191:\u66fe\u5f65\u7db8")

    def test_two_vehicle_form_parses_independent_vehicle_entries(self):
        request = request_from_form(
            {
                "two_vehicle": "1",
                "case_id": "20260602011652012",
                "case_date": "2026/06/02",
                "case_time": "0116",
                "vehicle": "\u65b0\u576191",
                "driver": "\u66fe\u5f65\u7db8",
                "return_date": "2026/06/02",
                "return_time": "0200",
                "mileage": "101",
                "patient_summary": "\u7537\u4e00\u540d",
                "consumables": "\u53e3\u7f69=2,\u624b\u5957=2",
                "disinfection_items": ["\u6551\u8b77\u8eca\u9ad4"],
                "fuel_record": "1",
                "fuel_date": "",
                "fuel_time": "1720",
                "fuel_quantity": "42.122",
                "fuel_unit_price": "30.3",
                "vehicle_2": "\u65b0\u576192",
                "driver_2": "\u9673\u5c0f\u660e",
                "return_date_2": "2026/06/02",
                "return_time_2": "0210",
                "mileage_2": "202",
                "patient_summary_2": "\u7121",
                "consumables_2": "\u8033\u6eab\u5957=1",
                "disinfection_items_2": ["\u64d4\u67b6\u5e8a"],
                "fuel_record_2": "1",
                "fuel_date_2": "2026/06/03",
                "fuel_time_2": "1735",
                "fuel_quantity_2": "40.5",
                "fuel_unit_price_2": "31",
            }
        )

        self.assertTrue(request.two_vehicle)
        self.assertEqual(len(request.vehicle_entries), 2)
        self.assertEqual(request.vehicle_entries[0].vehicle, "\u65b0\u576191")
        self.assertEqual(request.vehicle_entries[0].return_time, "0200")
        self.assertEqual(request.vehicle_entries[1].vehicle, "\u65b0\u576192")
        self.assertEqual(request.vehicle_entries[1].return_time, "0210")
        self.assertEqual(request.vehicle_entries[1].consumables, {"\u8033\u6eab\u5957": 1})
        self.assertEqual(request.vehicle_entries[1].disinfection_items, ["\u64d4\u67b6\u5e8a"])
        self.assertTrue(request.vehicle_entries[0].fuel_record.enabled)
        self.assertEqual(request.vehicle_entries[0].fuel_record.date, "20260602")
        self.assertEqual(request.vehicle_entries[0].fuel_record.time, "1720")
        self.assertEqual(request.vehicle_entries[0].fuel_record.driver, "\u66fe\u5f65\u7db8")
        self.assertEqual(request.vehicle_entries[0].fuel_record.product, "\u8d85\u7d1a\u67f4\u6cb9")
        self.assertEqual(request.vehicle_entries[0].fuel_record.quantity, "42.122")
        self.assertEqual(request.vehicle_entries[0].fuel_record.unit_price, "30.3")
        self.assertTrue(request.vehicle_entries[1].fuel_record.enabled)
        self.assertEqual(request.vehicle_entries[1].fuel_record.date, "20260603")
        self.assertEqual(request.vehicle_entries[1].fuel_record.time, "1735")
        self.assertEqual(request.vehicle_entries[1].fuel_record.driver, "\u9673\u5c0f\u660e")
        self.assertEqual(
            request.duty_status_text,
            "1.\u65b0\u576191:\u66fe\u5f65\u7db8 \u65b0\u576192:\u9673\u5c0f\u660e\n2.\u7537\u4e00\u540d",
        )

    def test_two_vehicle_requests_expand_to_single_vehicle_requests(self):
        request = request_from_form(
            {
                "two_vehicle": "1",
                "vehicle": "\u65b0\u576191",
                "driver": "\u66fe\u5f65\u7db8",
                "return_time": "0200",
                "mileage": "101",
                "patient_summary": "\u7537\u4e00\u540d",
                "consumables": "\u53e3\u7f69=2",
                "vehicle_2": "\u65b0\u576192",
                "driver_2": "\u9673\u5c0f\u660e",
                "return_time_2": "0210",
                "mileage_2": "202",
                "patient_summary_2": "\u7121",
                "consumables_2": "\u624b\u5957=2",
            }
        )

        first, second = request.vehicle_requests()

        self.assertFalse(first.two_vehicle)
        self.assertEqual(first.vehicle, "\u65b0\u576191")
        self.assertEqual(first.return_time, "0200")
        self.assertEqual(first.consumables, {"\u53e3\u7f69": 2})
        self.assertEqual(second.vehicle, "\u65b0\u576192")
        self.assertEqual(second.return_time, "0210")
        self.assertEqual(second.patient_summary, "\u7121")
        self.assertEqual(second.consumables, {"\u624b\u5957": 2})

    def test_missing_case_date_falls_back_to_cross_day_created_at(self):
        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=datetime(2026, 6, 7, 1, 0),
            raw_text="",
            case_time="2350",
            return_time="0010",
        )

        self.assertEqual(request.service_case_date().strftime("%Y-%m-%d"), "2026-06-06")

    def test_parse_consumables_accepts_multiple_separators(self):
        self.assertEqual(
            parse_consumables("\u53e3\u7f69*2\u3001\u624b\u5957x2,\u6c27\u6c23\u9762\u7f69=1"),
            {"\u53e3\u7f69": 2, "\u624b\u5957": 2, "\u6c27\u6c23\u9762\u7f69": 1},
        )

    def test_clean_case_address_removes_cancel_noise(self):
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u4e2d\u58e2\u5340\u5c71\u6771\u8def673\u865f-\u4f86\u96fb\u53d6\u6d88"),
            "\u6843\u5712\u5e02\u4e2d\u58e2\u5340\u5c71\u6771\u8def673\u865f",
        )
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4fdd\u969c\u4e8c\u8def-\u6848\u4ef6\u91cd\u8907"),
            "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4fdd\u969c\u4e8c\u8def",
        )
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u89c0\u97f3\u5340\u89c0\u97f3\u9ad8\u4e2d(\u4e2d\u5c71\u8def\u4e8c\u6bb5\u5074)-\u8eca\u798d\u62d2\u9001"),
            "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u89c0\u97f3\u9ad8\u4e2d(\u4e2d\u5c71\u8def\u4e8c\u6bb5\u5074)",
        )
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def\u4e8c\u6bb5705\u865f3\u6a13(OHCA-D)-\u6025\u75c5\u653e\u68c4\u6025\u6551\u52e4\u5340\u8655\u7406"),
            "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def\u4e8c\u6bb5705\u865f3\u6a13(OHCA-D)",
        )
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u4e2d\u58e2\u5340\u5c71\u6771\u4e00\u8def313\u5df721\u865f-\u9577\u5e9a"),
            "\u6843\u5712\u5e02\u4e2d\u58e2\u5340\u5c71\u6771\u4e00\u8def313\u5df721\u865f",
        )
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u89c0\u97f3\u5340\u6210\u529f\u8def\u4e00\u6bb5123\u865f-\u8aa4\u5831(\u81ea\u884c\u64b2\u6ec5)"),
            "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u6210\u529f\u8def\u4e00\u6bb5123\u865f",
        )
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def\u4e8c\u6bb5640-1\u865f-\u6025\u75c5\u62d2\u9001"),
            "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def\u4e8c\u6bb5640-1\u865f",
        )
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def\u4e8c\u6bb5\u8207\u6210\u529f\u8def\u53e3-\u6025\u75c5\u62d2\u9001"),
            "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def\u4e8c\u6bb5\u8207\u6210\u529f\u8def\u53e3",
        )
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u89c0\u97f3\u5340\u5c71\u6771\u4e00\u8def313\u5df7\u53e3-\u8eca\u798d\u62d2\u9001"),
            "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u5c71\u6771\u4e00\u8def313\u5df7\u53e3",
        )
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def\u4e8c\u6bb5705\u865f3\u6a13(OHCA-D)"),
            "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e2d\u5c71\u8def\u4e8c\u6bb5705\u865f3\u6a13(OHCA-D)",
        )


if __name__ == "__main__":
    unittest.main()
