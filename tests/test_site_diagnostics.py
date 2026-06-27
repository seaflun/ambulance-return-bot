import unittest

from ambulance_bot.site_diagnostics import diagnostic_payload


class SiteDiagnosticsTests(unittest.TestCase):
    def test_login_failure_points_to_site_login_stage(self):
        payload = diagnostic_payload("consumables", "consumables_failed", "SSO login failed")

        self.assertEqual(payload["failure_stage"], "登入一站通")
        self.assertIn("登入", payload["failure_reason"])
        self.assertIn("驗證碼", payload["next_action"])
        self.assertEqual(payload["exception_type"], "login")

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

    def test_success_has_no_failure_diagnostic(self):
        payload = diagnostic_payload("duty_work_log", "duty_work_log_saved", "saved")

        self.assertEqual(payload["failure_stage"], "")
        self.assertEqual(payload["failure_reason"], "")


if __name__ == "__main__":
    unittest.main()
