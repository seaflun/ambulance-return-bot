from datetime import datetime
import unittest
from unittest.mock import patch

import app as runtime
from tests import test_web_app as web_fixtures


class MileageReferenceFailuresTests(unittest.TestCase):
    def setUp(self):
        self.fixture = web_fixtures.WebAppTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def report(self, time, mileage, status="vehicle_mileage_saved", **site_fields):
        return {"task": {"task_id": "reference-" + time, "service_type": "disaster",
                         "created_at": "2026-10-24T14:10:00", "case_date": "2026/10/24",
                         "case_time": time, "vehicle": "新坡11", "driver": "甲", "mileage": mileage},
                "site_statuses": {"vehicle_mileage": {"status": status, **site_fields}}}

    def references(self, reports):
        with patch.object(runtime, "_vehicle_mileage_history_payloads", return_value=reports):
            return self.fixture.client.get("/api/vehicle-mileages").json

    def test_failed_submitted_mileages_never_replace_the_last_saved_reference(self):
        reports = [self.report("1336", "35477", "vehicle_mileage_failed"),
                   self.report("0933", "35467"),
                   self.report("0837", "35481", "vehicle_mileage_failed")]
        result = self.references(reports)
        self.assertEqual("35467", result["mileages"]["新坡11"])
        self.assertEqual("202610240933", result["times"]["新坡11"])
        self.assertEqual([{"time": "202610240933", "mileage": "35467"}], result["history"]["新坡11"])

    def test_pending_unconfirmed_and_manual_completion_are_not_official_mileage_references(self):
        for status in ("not_started", "vehicle_mileage_running", "vehicle_mileage_waiting_confirmation",
                       "vehicle_mileage_needs_update", "completed_by_user"):
            with self.subTest(status=status):
                result = self.references([self.report("1336", "35477", status), self.report("0933", "35467")])
                self.assertEqual("35467", result["mileages"]["新坡11"])
                self.assertEqual(1, len(result["history"]["新坡11"]))

    def test_mixed_vehicle_failure_preserves_only_the_independently_saved_car(self):
        mixed = self.report("1336", "35477", "vehicle_mileage_failed", vehicle_results={
            "新坡11": {"status": "vehicle_mileage_failed"},
            "新坡15": {"status": "vehicle_mileage_saved"}})
        mixed["task"]["vehicle_entries"] = [
            {"vehicle": "新坡11", "driver": "甲", "mileage": "35477"},
            {"vehicle": "新坡15", "driver": "乙", "mileage": "25449"}]
        result = self.references([mixed, self.report("0933", "35467")])
        self.assertEqual({"新坡11": "35467", "新坡15": "25449"}, result["mileages"])
        self.assertEqual([{"time": "202610241336", "mileage": "25449"}], result["history"]["新坡15"])

    def test_unreported_car_does_not_inherit_aggregate_saved_status(self):
        report = self.report("1336", "35477", vehicle_results={"新坡15": {"status": "vehicle_mileage_saved"}})
        self.assertNotIn("新坡11", self.references([report])["mileages"])

    def test_existing_record_confirmation_does_not_certify_the_submitted_mileage(self):
        existing = self.report("1336", "35477", detail="已登打；保留 PPE 既有紀錄，本次未新增或儲存。")
        result = self.references([existing, self.report("0933", "35467")])
        self.assertEqual("35467", result["mileages"]["新坡11"])
        self.assertEqual(1, len(result["history"]["新坡11"]))

    def test_other_site_failure_does_not_discard_saved_mileage(self):
        report = self.report("1336", "35491")
        report["overall_status"] = "desktop_fast_completed_with_errors"
        report["site_statuses"]["duty_work_log"] = {"status": "duty_work_log_failed"}
        self.assertEqual("35491", self.references([report])["mileages"]["新坡11"])

    def test_failed_reference_cannot_reject_a_later_valid_mileage(self):
        reports = [self.report("1000", "35500", "vehicle_mileage_failed"), self.report("0933", "35467")]
        entry = runtime.AmbulanceReturnRequest.from_dict(self.report("1336", "35491")["task"])
        with patch.object(runtime, "_vehicle_mileage_history_payloads", return_value=reports):
            self.assertEqual([], runtime.validate_vehicle_mileage_change(
                [entry], ["第1車"], case_datetime=datetime(2026, 10, 24, 13, 36)))

    def test_current_failed_task_cannot_fall_back_to_an_old_success_report_with_the_same_id(self):
        report = self.report("1336", "35477")
        model = runtime.AmbulanceReturnRequest.from_dict(report["task"])
        self.fixture.store.create(model)
        self.fixture.store.update_site_result(model.task_id, runtime.SiteAutomationResult(
            "vehicle_mileage", "里程", "vehicle_mileage_failed", "本案結束里程小於前一筆結束里程。"))
        with patch.object(runtime, "_load_public_pc_reports", return_value=[report]):
            result = self.fixture.client.get("/api/vehicle-mileages").json
        self.assertNotIn("新坡11", result["mileages"])
        self.assertNotIn("新坡11", result["history"])

    def test_malformed_vehicle_results_do_not_certify_the_submitted_mileage(self):
        report = self.report("1336", "35477", vehicle_results=[{"status": "vehicle_mileage_saved"}])
        self.assertNotIn("新坡11", self.references([report])["mileages"])


if __name__ == "__main__":
    unittest.main()
