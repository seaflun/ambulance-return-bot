import unittest

from ambulance_bot.site_diagnostics import (
    diagnostic_payload, merge_diagnostic_fields, site_stage_from_detail,
)


class MileageStageDiagnosticsTests(unittest.TestCase):
    def test_actual_worker_query_progress_has_the_canonical_mileage_stage(self):
        detail = (
            "登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，27番 林宏為 - tyfd02321。"
            "公務電腦 worker 車輛里程：依案件時間查詢前後里程"
        )
        self.assertEqual("核對前後里程", site_stage_from_detail("vehicle_mileage", detail))

    def test_actual_mileage_progress_aliases_work_for_worker_and_local_execution(self):
        for prefix in ("公務電腦 worker 車輛里程：", "本機快速 車輛里程："):
            for reported, canonical in (
                ("依案件時間查詢前後里程", "核對前後里程"),
                ("本案與後一筆里程已核對", "確認里程紀錄"),
                ("啟動 Chrome", "啟動 Chrome"),
                ("登入 PPE", "登入 PPE"),
                ("開啟車輛里程", "開啟車輛里程"),
            ):
                with self.subTest(prefix=prefix, reported=reported):
                    self.assertEqual(canonical, site_stage_from_detail("vehicle_mileage", prefix + reported))

    def test_multi_vehicle_progress_uses_the_last_reported_stage(self):
        first = "新坡11: 公務電腦 worker 車輛里程：依案件時間查詢前後里程"
        second = "新坡15: 公務電腦 worker 車輛里程：登入 PPE"
        self.assertEqual("登入 PPE", site_stage_from_detail("vehicle_mileage", first + " | " + second))
        self.assertEqual("核對前後里程", site_stage_from_detail("vehicle_mileage", second + " | " + first))

    def test_legacy_unknown_cannot_override_a_recognizable_mileage_conflict(self):
        for message, category, stage in (
            ("案件與既有用車時間重疊，停止補登。", "mileage_overlap", "填寫返隊時間與里程"),
            ("相同案件時間已有不同內容的里程紀錄，停止補登。", "mileage_existing_conflict", "核對前後里程"),
        ):
            with self.subTest(category=category):
                fields = merge_diagnostic_fields(dict(
                    key="vehicle_mileage", status="vehicle_mileage_failed",
                    detail="車輛里程操作失敗：Message: " + message,
                    failure_stage="階段未判定", failure_reason="舊版未知原因",
                    next_action="查看畫面", exception_type="unknown"))
                self.assertEqual(category, fields["exception_type"])
                self.assertEqual(stage, fields["failure_stage"])

    def test_saved_existing_mileage_does_not_create_failure_diagnostics(self):
        detail = ("新坡11 已登打（車輛里程紀錄 2，時間重疊且地址相似）；"
                  "里程已重新查詢並核對既有紀錄；本次未新增或儲存。")
        self.assertTrue(all(value == "" for value in diagnostic_payload(
            "vehicle_mileage", "vehicle_mileage_saved", detail).values()))

    def test_multi_vehicle_failure_keeps_its_own_stage_after_another_vehicle_is_already_recorded(self):
        detail = ("新坡11: 已登打，時間重疊且地址相似，里程已重新查詢並核對既有紀錄。 | "
                  "新坡15: 車輛里程操作失敗：Message: 相同案件時間已有不同內容的里程紀錄，停止補登。")
        fields = diagnostic_payload("vehicle_mileage", "vehicle_mileage_failed", detail)
        self.assertEqual("mileage_existing_conflict", fields["exception_type"])
        self.assertEqual("核對前後里程", fields["failure_stage"])

    def test_unstructured_or_unknown_text_is_not_invented_as_live_progress(self):
        for detail in (
            "錯誤原文：依案件時間查詢前後里程",
            "未知錯誤，附註本案與後一筆里程已核對",
            'worker.py 原始錯誤："依案件時間查詢前後里程"',
            "公務電腦 worker 車輛里程：未知錯誤",
        ):
            with self.subTest(detail=detail):
                self.assertEqual("", site_stage_from_detail("vehicle_mileage", detail))
        self.assertEqual("", site_stage_from_detail(
            "fuel_record", "公務電腦 worker 加油：依案件時間查詢前後里程"))
        fields = diagnostic_payload("vehicle_mileage", "vehicle_mileage_failed", "未分類例外：opaque failure")
        self.assertEqual("unknown", fields["exception_type"])


if __name__ == "__main__":
    unittest.main()
