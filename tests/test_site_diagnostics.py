import unittest

from ambulance_bot.site_diagnostics import diagnostic_payload, merge_diagnostic_fields


class SiteDiagnosticsTests(unittest.TestCase):
    def test_multi_patient_consumables_failure_is_not_button_error(self):
        payload = diagnostic_payload(
            "consumables",
            "consumables_failed",
            "同案多患者耗材分配／確認失敗：成功=01；失敗=02；原因=耗材儲存後讀回不一致",
        )

        self.assertEqual(payload["failure_stage"], "同案多患者耗材確認")
        self.assertEqual(payload["exception_type"], "multi_patient_consumables")
        self.assertIn("多患者", payload["failure_reason"])
        self.assertIn("患者序號", payload["next_action"])
        self.assertNotIn("按鈕", payload["failure_reason"])

    def test_login_failure_points_to_site_login_stage(self):
        payload = diagnostic_payload("consumables", "consumables_failed", "SSO login failed")

        self.assertEqual(payload["failure_stage"], "登入一站通")
        self.assertIn("登入", payload["failure_reason"])
        self.assertIn("驗證碼", payload["next_action"])
        self.assertEqual(payload["exception_type"], "login")

    def test_errno_22_oserror_points_to_chrome_start_stage(self):
        payload = diagnostic_payload(
            "consumables",
            "consumables_failed",
            "[Errno 22] Invalid argument",
            OSError(22, "Invalid argument"),
        )

        self.assertEqual(payload["failure_stage"], "啟動 Chrome")
        self.assertIn("Chrome", payload["failure_reason"])
        self.assertEqual(payload["exception_type"], "OSError")

    def test_vehicle_not_found_points_to_mileage_fill_stage(self):
        payload = diagnostic_payload("vehicle_mileage", "vehicle_mileage_failed", "vehicle not found: 新坡91")

        self.assertEqual(payload["failure_stage"], "填寫返隊時間與里程")
        self.assertIn("救護車", payload["failure_reason"])

    def test_fuel_card_not_found_is_not_classified_as_login_failure(self):
        payload = diagnostic_payload(
            "fuel_record",
            "fuel_record_failed",
            "登入帳號：加油=司機帳號優先。加油紀錄操作失敗：Message: fuel card not found: BGV-2310",
        )

        self.assertEqual(payload["exception_type"], "vehicle_not_found")
        self.assertEqual(payload["failure_stage"], "開啟登打油耗")
        self.assertNotIn("登入", payload["failure_reason"])

    def test_fuel_period_mismatch_points_to_fuel_query_stage(self):
        payload = diagnostic_payload(
            "fuel_record",
            "fuel_record_failed",
            "登入帳號：加油=司機帳號優先。加油紀錄操作失敗：Message: fuel period mismatch: page=2026/06 task=2026/07",
        )

        self.assertEqual(payload["exception_type"], "fuel_period")
        self.assertEqual(payload["failure_stage"], "開啟登打油耗")
        self.assertIn("月份", payload["failure_reason"])
        self.assertIn("自動切換月份", payload["next_action"])
        self.assertNotIn("登入", payload["failure_reason"])

    def test_consumable_missing_case_row_points_to_tablet_closure(self):
        payload = diagnostic_payload(
            "consumables",
            "consumables_failed",
            "一站通耗材: 耗材列表找不到符合案件的內容列：時間=2000 地址=桃園市觀音區金華路631巷76號1樓",
        )

        self.assertEqual(payload["exception_type"], "case_not_closed")
        self.assertEqual(payload["failure_stage"], "開啟耗材紀錄")
        self.assertIn("尚未在救護平板結案", payload["failure_reason"])
        self.assertIn("請先去救護平板結案", payload["next_action"])

    def test_disinfection_missing_detail_points_to_tablet_closure(self):
        payload = diagnostic_payload(
            "disinfection",
            "disinfection_failed",
            "消毒紀錄操作失敗：Message: missing disinfection detail for case time 2000",
        )

        self.assertEqual(payload["exception_type"], "case_not_closed")
        self.assertEqual(payload["failure_stage"], "開啟消毒紀錄")
        self.assertIn("尚未在救護平板結案", payload["failure_reason"])
        self.assertIn("請先去救護平板結案", payload["next_action"])

    def test_consumable_missing_case_row_with_login_prefix_points_to_tablet_closure(self):
        payload = diagnostic_payload(
            "consumables",
            "consumables_failed",
            "一站通耗材: 登入帳號：耗材=公務電腦同步帳號。耗材列表找不到符合案件的內容列：時間=2047 地址=桃園市中壢區月桃路一段270巷52號",
        )

        self.assertEqual(payload["exception_type"], "case_not_closed")
        self.assertEqual(payload["failure_stage"], "開啟耗材紀錄")
        self.assertIn("尚未在救護平板結案", payload["failure_reason"])
        self.assertIn("請先去救護平板結案", payload["next_action"])

    def test_disinfection_missing_detail_with_login_prefix_points_to_tablet_closure(self):
        payload = diagnostic_payload(
            "disinfection",
            "disinfection_failed",
            "緊急救護消毒: 登入帳號：消毒=公務電腦同步帳號。消毒紀錄操作失敗：Message: missing disinfection detail for case time 2047",
        )

        self.assertEqual(payload["exception_type"], "case_not_closed")
        self.assertEqual(payload["failure_stage"], "開啟消毒紀錄")
        self.assertIn("尚未在救護平板結案", payload["failure_reason"])
        self.assertIn("請先去救護平板結案", payload["next_action"])

    def test_consumable_empty_readback_points_to_tablet_closure(self):
        payload = diagnostic_payload(
            "consumables",
            "consumables_failed",
            "一站通耗材: 耗材儲存後讀回不一致：expected=[('813', '1')] actual=[]",
        )

        self.assertEqual(payload["exception_type"], "case_not_closed")
        self.assertEqual(payload["failure_stage"], "開啟耗材紀錄")
        self.assertIn("請先去救護平板結案", payload["next_action"])

    def test_case_not_closed_recomputes_old_generic_diagnostics_for_display(self):
        diagnostic = merge_diagnostic_fields(
            {
                "key": "consumables",
                "status": "consumables_failed",
                "detail": "一站通耗材: 耗材儲存後讀回不一致：expected=[('813', '1')] actual=[]",
                "failure_stage": "填寫耗材品項",
                "failure_reason": "送出前資料檢查不一致，程式已停止避免寫入錯誤資料。",
                "next_action": "先不要儲存；檢查畫面是否仍有舊資料或欄位對應錯誤，修正後再重試。",
                "exception_type": "validation",
            }
        )

        self.assertEqual(diagnostic["exception_type"], "case_not_closed")
        self.assertIn("請先去救護平板結案", diagnostic["next_action"])

    def test_success_has_no_failure_diagnostic(self):
        payload = diagnostic_payload("duty_work_log", "duty_work_log_saved", "saved")

        self.assertEqual(payload["failure_stage"], "")
        self.assertEqual(payload["failure_reason"], "")


if __name__ == "__main__":
    unittest.main()
