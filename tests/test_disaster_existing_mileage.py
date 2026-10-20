from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import worker
from ambulance_bot import selenium_local as runtime
from ambulance_bot.models import AmbulanceReturnRequest


class DisasterExistingMileageTests(unittest.TestCase):
    def request(self, **changes):
        values = dict(task_id="disaster-existing", created_at=datetime(2026, 10, 20), raw_text="",
                      service_type="disaster", vehicle="新坡11", driver="甲",
                      case_date="2026/10/20", case_time="1636", return_date="2026/10/20",
                      return_time="1713", mileage="35444",
                      case_address="桃園市觀音區新生路552巷內土地公廟對面")
        values.update(changes)
        return AmbulanceReturnRequest(**values)

    def rows(self):
        before = dict(Id=1, StartDay="20261020", EndDay="20261020", StartTime="1509",
                      EndTime="1612", StartMileage=35431, EndMileage=35441, Mileage=10,
                      Destination="別處", DriverName="乙", month="2026/10")
        existing = dict(Id=2, StartDay="20261020", EndDay="20261020", StartTime="1637",
                        EndTime="1713", StartMileage=35441, EndMileage=35444, Mileage=3,
                        Destination="觀音區新生路552巷", DriverName="甲", month="2026/10")
        return before, existing

    def enter(self, request, history, refreshed=None, *, previous_request=None, cancel_check=None):
        with patch.object(runtime, "_vehicle_mileage_history", return_value=history), \
             patch.object(runtime, "_load_vehicle_mileage_month", return_value=history if refreshed is None else refreshed) as load, \
             patch.object(runtime, "_write_vehicle_mileage_backfill") as write, \
             patch.object(runtime, "_save_vehicle_mileage_form") as save:
            detail = runtime._add_vehicle_mileage_record(
                object(), request, previous_request=previous_request, cancel_check=cancel_check)
        write.assert_not_called()
        save.assert_not_called()
        return detail, load.call_args_list

    def test_overlapping_similar_address_is_already_recorded_without_writing(self):
        before, existing = self.rows()
        detail, _ = self.enter(self.request(), [before, existing])
        for expected in ("新坡11", "已登打", "紀錄 2", "16:37", "35441", "35444", self.request().case_address):
            self.assertIn(expected, detail)
        self.assertNotIn(runtime.WAITING_CONFIRMATION_MARKER, detail)

    def test_different_return_time_driver_and_mileage_are_reported_without_overwriting_existing(self):
        before, existing = self.rows()
        existing.update(StartTime="1636", EndTime="1728", DriverName="乙", EndMileage=35445, Mileage=4)
        detail, _ = self.enter(self.request(), [before, existing])
        for expected in ("已登打", "17:28", "乙", "35445", "17:13", "35444"):
            self.assertIn(expected, detail)

    def test_previous_month_cross_day_match_is_verified_in_its_own_month(self):
        _, existing = self.rows()
        existing.update(StartDay="20260930", EndDay="20261001", StartTime="2359",
                        EndTime="0030", month="2026/09")
        request = self.request(case_date="2026/10/01", case_time="0005",
                               return_date="2026/10/01", return_time="0025")
        detail, queries = self.enter(request, [existing])
        self.assertIn("已登打", detail)
        self.assertEqual("2026/09", queries[-1].args[2])

    def test_different_lane_remains_blocked(self):
        before, existing = self.rows()
        existing["Destination"] = "觀音區新生路553巷"
        with self.assertRaises(runtime.WebDriverException):
            self.enter(self.request(), [before, existing])

    def test_matching_record_created_between_history_and_final_refresh_is_not_added_again(self):
        before, existing = self.rows()
        detail, _ = self.enter(self.request(), [before], [before, existing])
        self.assertIn("已登打", detail)

    def test_matching_record_found_during_conflict_refresh_is_confirmed_without_writing(self):
        before, existing = self.rows()
        stale = dict(existing, Destination="不同道路553巷")
        detail, _ = self.enter(self.request(), [before, stale], [before, existing])
        self.assertIn("已登打", detail)

    def test_multiple_similar_overlaps_are_not_automatically_confirmed(self):
        before, existing = self.rows()
        with self.assertRaises(runtime.WebDriverException):
            self.enter(self.request(), [before, existing, dict(existing, Id=3)])

    def test_readback_disappearance_change_or_new_ambiguity_never_becomes_saved(self):
        before, existing = self.rows()
        for refreshed in ([before], [before, dict(existing, EndMileage=35445)],
                          [before, existing, dict(existing, Id=3)]):
            with self.subTest(refreshed=refreshed):
                detail, _ = self.enter(self.request(), [before, existing], refreshed)
                self.assertIn(runtime.WAITING_CONFIRMATION_MARKER, detail)
                self.assertIn("未新增", detail)
                self.assertNotIn("已嘗試儲存", detail)

    def test_ems_and_explicit_updates_keep_their_existing_strict_checks(self):
        before, existing = self.rows()
        for request, previous in ((self.request(service_type="ems"), None),
                                  (self.request(), self.request())):
            with self.subTest(service=request.service_type, update=previous is not None), \
                 self.assertRaises(runtime.WebDriverException):
                self.enter(request, [before, existing], previous_request=previous)

    def test_touching_intervals_do_not_count_as_overlap(self):
        before, existing = self.rows()
        existing.update(StartTime="1713", EndTime="1800", EndMileage=35442)
        with self.assertRaises(runtime.WebDriverException):
            self.enter(self.request(), [before, existing])

    def test_cancel_after_readback_still_interrupts_instead_of_reporting_already_recorded(self):
        before, existing = self.rows()
        checks = []

        def check():
            checks.append(True)
            if len(checks) == 2:
                raise runtime.TaskCancellationError("cancelled")

        with self.assertRaises(runtime.TaskCancellationError):
            self.enter(self.request(), [before, existing], cancel_check=check)

    def test_verified_existing_record_is_saved_even_when_new_writes_are_disabled(self):
        detail, _ = self.enter(self.request(), list(self.rows()))
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runtime, "_open_vehicle_mileage_page", return_value=detail), \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=False), \
             patch.object(runtime, "apply_tile"), \
             patch.object(runtime, "_set_window_size_if_enabled"), \
             patch.object(runtime, "mark_driver_operation_active"):
            result = runtime.run_vehicle_mileage_task(
                self.request(), Path(tmp), existing_driver=Mock(), use_session_lock=False)
        self.assertEqual("vehicle_mileage_saved", result.status)

    def test_failed_existing_readback_remains_waiting_when_new_writes_are_disabled(self):
        before, existing = self.rows()
        detail, _ = self.enter(self.request(), [before, existing], [before])
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runtime, "_open_vehicle_mileage_page", return_value=detail), \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=False), \
             patch.object(runtime, "apply_tile"), \
             patch.object(runtime, "_set_window_size_if_enabled"), \
             patch.object(runtime, "mark_driver_operation_active"):
            result = runtime.run_vehicle_mileage_task(
                self.request(), Path(tmp), existing_driver=Mock(), use_session_lock=False)
        self.assertEqual("vehicle_mileage_waiting_confirmation", result.status)

    def test_unverified_text_containing_already_recorded_is_not_a_success_signal(self):
        detail = f"{runtime.WAITING_CONFIRMATION_MARKER} 查到已登打候選，但回查尚未確認。"
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runtime, "_open_vehicle_mileage_page", return_value=detail), \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=True), \
             patch.object(runtime, "apply_tile"), \
             patch.object(runtime, "_set_window_size_if_enabled"), \
             patch.object(runtime, "mark_driver_operation_active"):
            result = runtime.run_vehicle_mileage_task(
                self.request(), Path(tmp), existing_driver=Mock(), use_session_lock=False)
        self.assertEqual("vehicle_mileage_waiting_confirmation", result.status)

    def test_real_worker_flow_skips_existing_first_car_and_saves_only_missing_second_car(self):
        request = self.request(vehicle_entries=[
            dict(vehicle="新坡11", driver="甲", mileage="35444", return_date="2026/10/20", return_time="1713"),
            dict(vehicle="新坡15", driver="乙", mileage="25400", return_date="2026/10/20", return_time="1713")])
        before, existing = self.rows()
        histories = {"新坡11": [before, existing], "新坡15": [dict(
            before, Id=10, StartMileage=25377, EndMileage=25394, Mileage=17)]}
        writes, reports, logins = [], {}, []

        def open_page(driver, car, output_dir, **kwargs):
            logins.append((car.vehicle, kwargs["login_request"].driver))
            return runtime._add_vehicle_mileage_record(driver, car, output_dir, progress=kwargs["progress"])

        def write(_driver, car, plan, **_kwargs):
            writes.append(car.vehicle)
            histories[car.vehicle].append(dict(
                Id=20, StartDay="20261020", EndDay="20261020", StartTime="1636", EndTime="1713",
                StartMileage=int(plan["start_mileage"]), EndMileage=int(plan["end_mileage"]),
                Mileage=int(plan["end_mileage"]) - int(plan["start_mileage"]),
                Destination=car.case_address, DriverName=car.driver, month="2026/10"))

        def post(_server, _task_id, status, detail, **kwargs):
            if kwargs.get("vehicle_key"):
                reports[kwargs["vehicle_key"]] = (status, detail)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(worker, "post_status", side_effect=post), \
             patch.object(worker, "login_audit_for_site", return_value=""), \
             patch.object(runtime, "_create_driver", return_value=Mock()), \
             patch.object(runtime, "_open_vehicle_mileage_page", side_effect=open_page), \
             patch.object(runtime, "_vehicle_mileage_history", side_effect=lambda _d, car, *_: list(histories[car.vehicle])), \
             patch.object(runtime, "_load_vehicle_mileage_month", side_effect=lambda _d, car, *_: list(histories[car.vehicle])), \
             patch.object(runtime, "_write_vehicle_mileage_backfill", side_effect=write), \
             patch.object(runtime, "_save_vehicle_mileage_form", return_value="儲存成功") as save, \
             patch.object(runtime, "_vehicle_mileage_matching_row_indices", return_value=[0]), \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=True), \
             patch.object(runtime, "apply_tile"), \
             patch.object(runtime, "_set_window_size_if_enabled"), \
             patch.object(runtime, "mark_driver_operation_active"):
            result = worker.run_vehicle_task("http://nas", "worker", request.to_dict(), Path(tmp),
                                             use_session_lock=False, update_overall=False)
        self.assertEqual("vehicle_mileage_saved", result.status, result.detail)
        self.assertEqual(["新坡15"], writes)
        save.assert_called_once()
        self.assertEqual([("新坡11", "甲"), ("新坡15", "甲")], logins)
        self.assertEqual("vehicle_mileage_saved", reports["新坡11"][0])
        self.assertIn("已登打", reports["新坡11"][1])
        self.assertEqual("vehicle_mileage_saved", reports["新坡15"][0])
        self.assertIn("已依案件時間銜接前後里程", reports["新坡15"][1])


if __name__ == "__main__":
    unittest.main()
