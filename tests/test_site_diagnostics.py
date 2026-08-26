import unittest

from ambulance_bot.site_diagnostics import diagnostic_payload, merge_diagnostic_fields


class SiteDiagnosticsTests(unittest.TestCase):
    def test_waiting_confirmation_status_has_save_stage_and_no_five_site_wording(self):
        payload = diagnostic_payload(
            "vehicle_mileage",
            "vehicle_mileage_waiting_confirmation",
            "waiting_confirmation: 已按儲存但未收到成功回應",
        )

        self.assertEqual(payload["exception_type"], "waiting_confirmation")
        self.assertEqual(payload["failure_stage"], "儲存")
        self.assertNotIn("五站", payload["next_action"])
    def test_fuel_missing_driver_is_not_login_failure(self):
        payload = diagnostic_payload(
            "fuel_record",
            "fuel_record_failed",
            (
                "登入帳號：加油=任務司機，25番 郭國偵 - tyfd02060。"
                "加油紀錄操作失敗：Message: missing fuel driver: "
                "requested=郭國偵; candidates=陳俊翰,林志偉"
            ),
        )

        self.assertEqual(payload["exception_type"], "ppe_driver")
        self.assertEqual(payload["failure_stage"], "填寫加油紀錄")
        self.assertIn("駕駛清單", payload["failure_reason"])
        self.assertNotIn("登入", payload["failure_reason"])
        self.assertIn("PPE 人員清單", payload["next_action"])

    def test_mileage_missing_driver_stops_at_fill_stage(self):
        payload = diagnostic_payload(
            "vehicle_mileage",
            "vehicle_mileage_failed",
            (
                "登入帳號：里程=任務司機，25番 郭國偵 - tyfd02060。"
                "車輛里程操作失敗：Message: missing vehicle mileage driver: "
                "requested=郭國偵; candidates=陳俊翰,林志偉"
            ),
        )

        self.assertEqual(payload["exception_type"], "ppe_driver")
        self.assertEqual(payload["failure_stage"], "填寫返隊時間與里程")
        self.assertIn("駕駛清單", payload["failure_reason"])
        self.assertNotIn("登入", payload["failure_reason"])

    def test_merge_replaces_stored_login_diagnosis_for_missing_driver(self):
        merged = merge_diagnostic_fields(
            {
                "key": "fuel_record",
                "status": "fuel_record_failed",
                "detail": (
                    "登入帳號：加油=任務司機。加油紀錄操作失敗：Message: "
                    "fuel grid fill failed: {'ok': False, 'reason': 'missing driver'}"
                ),
                "failure_stage": "登入 PPE",
                "failure_reason": "登入、帳密、SSO 或驗證碼尚未完成。",
                "next_action": "完成登入後重試。",
                "exception_type": "login",
            }
        )

        self.assertEqual(merged["exception_type"], "ppe_driver")
        self.assertEqual(merged["failure_stage"], "填寫加油紀錄")

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

    def test_civilpower_io_verify_is_not_classified_as_login_with_account_audit(self):
        payload = diagnostic_payload(
            "volunteer_assist",
            "volunteer_assist_failed",
            (
                "登入帳號：民力系統=值班人員 > 任務司機 > 出勤人員 > 同步帳號。"
                "民力系統登打失敗：出入登記簿儲存後回查不到出／救護出勤紀錄。"
            ),
        )

        self.assertEqual(payload["exception_type"], "civilpower_io_verify")
        self.assertEqual(payload["failure_stage"], "儲存並回查出／入登記")
        self.assertIn("出入登記簿", payload["failure_reason"])
        self.assertNotIn("登入", payload["failure_reason"])
        self.assertIn("清單刷新", payload["next_action"])

    def test_civilpower_reported_stage_overrides_a_generic_io_diagnosis(self):
        payload = diagnostic_payload(
            "volunteer_assist",
            "volunteer_assist_failed",
            "民力系統按修改入登記失敗：出入登記簿儲存後回查不到入／救護返隊紀錄。",
        )

        self.assertEqual(payload["exception_type"], "civilpower_io_verify")
        self.assertEqual(payload["failure_stage"], "按修改入登記")

    def test_merge_replaces_stored_login_diagnosis_for_civilpower_io_verify(self):
        merged = merge_diagnostic_fields(
            {
                "key": "volunteer_assist",
                "status": "volunteer_assist_failed",
                "detail": (
                    "登入帳號：民力系統=值班人員 > 任務司機 > 出勤人員 > 同步帳號。"
                    "民力系統登打失敗：出入登記簿儲存後回查不到出／救護出勤紀錄。"
                ),
                "failure_stage": "登入內部入口網",
                "failure_reason": "登入、帳密、SSO 或驗證碼尚未完成。",
                "next_action": "完成登入後重試。",
                "exception_type": "login",
            }
        )

        self.assertEqual(merged["exception_type"], "civilpower_io_verify")
        self.assertEqual(merged["failure_stage"], "儲存並回查出／入登記")

    def test_merge_replaces_stored_login_diagnosis_for_civilpower_io_form_timeout(self):
        merged = merge_diagnostic_fields(
            {
                "key": "volunteer_assist",
                "status": "volunteer_assist_failed",
                "detail": (
                    "登入帳號：民力系統=值班人員 > 任務司機 > 出勤人員 > 同步帳號。"
                    "民力系統確認救護返隊出入登記逾時，網頁未在 15 秒內完成預期操作。"
                ),
                "failure_stage": "登入內部入口網",
                "failure_reason": "登入、帳密、SSO 或驗證碼尚未完成。",
                "next_action": "完成登入後重試。",
                "exception_type": "login",
            }
        )

        self.assertEqual(merged["exception_type"], "civilpower_io_form_timeout")
        self.assertEqual(merged["failure_stage"], "等待所屬單位重整")
        self.assertIn("連動", merged["failure_reason"])
        self.assertNotIn("帳密", merged["failure_reason"])

    def test_merge_replaces_stored_chrome_diagnosis_for_civilpower_stale_element(self):
        merged = merge_diagnostic_fields(
            {
                "key": "volunteer_assist",
                "status": "volunteer_assist_failed",
                "detail": (
                    "民力系統登打失敗：Message: stale element reference: stale element not found "
                    "in the current frame (Session info: chrome=151.0.7922.174)"
                ),
                "failure_stage": "啟動 Chrome",
                "failure_reason": "Chrome 或 ChromeDriver 工作階段無法建立或已中斷。",
                "next_action": "關閉殘留 Chrome/ChromeDriver，重啟 worker，再重新登打。",
                "exception_type": "chrome_session",
            }
        )

        self.assertEqual(merged["exception_type"], "stale_element")
        self.assertEqual(merged["failure_stage"], "開啟出／入新增表單")

    def test_work_log_case_query_range_failure_is_classified_as_case_not_found(self):
        payload = diagnostic_payload(
            "duty_work_log",
            "duty_case_not_found",
            "未在案件查詢區間（2026/07/13 08:04 起至目前）找到符合時間=0805 的案件",
        )

        self.assertEqual(payload["exception_type"], "case_not_found")
        self.assertEqual(payload["failure_stage"], "由案件帶入")

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

    def test_renderer_timeout_marker_is_reported_as_webpage_stall(self):
        payload = diagnostic_payload(
            "vehicle_mileage",
            "vehicle_mileage_failed",
            (
                "車輛里程操作失敗：Timed out receiving message from renderer: 45.000 "
                "[browser_failure:web_renderer_timeout]"
            ),
        )

        self.assertEqual(payload["exception_type"], "web_renderer_timeout")
        self.assertEqual(payload["failure_stage"], "開啟車輛里程")
        self.assertIn("網頁", payload["failure_reason"])
        self.assertNotIn("ChromeDriver 工作階段", payload["failure_reason"])

    def test_legacy_renderer_timeout_is_honest_about_missing_live_probe(self):
        payload = diagnostic_payload(
            "vehicle_mileage",
            "vehicle_mileage_failed",
            (
                "車輛里程操作失敗：Message: timeout: "
                "Timed out receiving message from renderer: -0.012 "
                "(Session info: chrome=150.0.7871.127)"
            ),
        )

        self.assertEqual(payload["exception_type"], "renderer_timeout_unverified")
        self.assertEqual(payload["failure_stage"], "開啟車輛里程")
        self.assertIn("舊紀錄", payload["failure_reason"])
        self.assertIn("無法確定", payload["failure_reason"])

    def test_stale_element_with_chrome_session_info_is_not_a_chrome_start_failure(self):
        payload = diagnostic_payload(
            "volunteer_assist",
            "volunteer_assist_failed",
            (
                "民力系統登打失敗：Message: stale element reference: stale element not found "
                "in the current frame (Session info: chrome=151.0.7922.174)"
            ),
        )

        self.assertEqual(payload["exception_type"], "stale_element")
        self.assertEqual(payload["failure_stage"], "開啟出／入新增表單")
        self.assertIn("重新整理", payload["failure_reason"])
        self.assertNotIn("ChromeDriver 工作階段", payload["failure_reason"])

    def test_chrome_unresponsive_marker_is_reported_as_browser_problem(self):
        payload = diagnostic_payload(
            "disinfection",
            "disinfection_failed",
            (
                "消毒紀錄操作失敗：disconnected: not connected to DevTools "
                "[browser_failure:chrome_unresponsive]"
            ),
        )

        self.assertEqual(payload["exception_type"], "chrome_unresponsive")
        self.assertEqual(payload["failure_stage"], "啟動 Chrome")
        self.assertIn("Google Chrome", payload["failure_reason"])

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

    def test_disinfection_empty_query_with_login_prefix_points_to_tablet_closure(self):
        payload = diagnostic_payload(
            "disinfection",
            "disinfection_failed",
            "緊急救護消毒: 登入帳號：消毒=任務司機。消毒紀錄操作失敗：Message: "
            "missing disinfection detail: query returned no data",
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
