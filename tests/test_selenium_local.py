import json
import inspect
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ambulance_bot.selenium_local as selenium_local_module
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.selenium_local import (
    _attach_case_form_details,
    _assert_disinfection_not_login,
    _click_disinfection_query,
    _click_disinfection_save,
    _click_fuel_card_register,
    _click_save_control,
    _click_vehicle_mileage_save,
    _create_driver,
    _disinfection_query_date,
    _duty_login_credential_attempts,
    _ensure_duty_login,
    _ensure_fuel_query_period,
    _fill_fuel_grid_record,
    _fill_vehicle_grid_values,
    _fuel_card_labels,
    _fuel_query_period,
    _id_number_from_cases_for_credential,
    lookup_synced_credential_id_number,
    _open_disinfection_detail_for_case,
    _ppe_option_names,
    _ppe_option_records_from_script,
    _ppe_option_value,
    _set_disinfection_query_date,
    _ensure_ppe_vehicle_mileage_session,
    _create_local_driver_with_retry,
    _ppe_credentials,
    _prepare_duty_work_log_form,
    _previous_case_details,
    _profile_dir,
    cleanup_stale_selenium_profiles,
    _resolve_end_mileage,
    _save_duty_work_log_enabled,
    _save_disinfection_probe_enabled,
    _save_disinfection_record_enabled,
    _save_vehicle_mileage_enabled,
    _vehicle_mileage_previous_request,
    _vehicle_mileage_driver_value,
    _vehicle_mileage_values,
    _wait_for_fuel_driver_value,
    _write_json_atomic,
    run_disinfection_task,
    run_fuel_record_task,
    run_local_selenium_task,
    run_vehicle_mileage_task,
    selenium_enabled,
)
from ambulance_bot.duty_credentials import load_synced_worker_credential, save_credential_sync_payload, save_duty_automation_credentials
from ambulance_bot.duty_credentials import DutyCredential


class SeleniumLocalTests(unittest.TestCase):
    def test_saved_duty_edit_returns_manual_update_required_before_driver_start(self):
        request = AmbulanceReturnRequest(
            task_id="duty-update",
            created_at=datetime(2026, 7, 13, 8, 0),
            raw_text="",
            vehicle="新坡92",
        )
        context = {"previous_task": request.to_dict(), "current_task": request.to_dict()}

        with tempfile.TemporaryDirectory() as tmp, patch.object(selenium_local_module, "_create_driver") as create_driver:
            result = selenium_local_module.run_local_selenium_task(
                request,
                Path(tmp),
                update_context=context,
            )

        self.assertEqual(result.status, "duty_work_log_waiting_confirmation")
        self.assertIn("人工", result.detail)
        create_driver.assert_not_called()

    def test_disinfection_removed_item_returns_manual_update_required_before_driver_start(self):
        previous = AmbulanceReturnRequest(
            task_id="disinfection-update",
            created_at=datetime(2026, 7, 13, 8, 0),
            raw_text="",
            vehicle="新坡92",
            disinfection_items=["救護車體", "擔架床"],
        )
        current = AmbulanceReturnRequest.from_dict(
            {**previous.to_dict(), "disinfection_items": ["救護車體"]}
        )
        context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

        with tempfile.TemporaryDirectory() as tmp, patch.object(selenium_local_module, "_create_driver") as create_driver:
            result = selenium_local_module.run_disinfection_task(
                current,
                Path(tmp),
                update_context=context,
            )

        self.assertEqual(result.status, "disinfection_waiting_confirmation")
        self.assertIn("人工", result.detail)
        create_driver.assert_not_called()

    def test_disinfection_case_time_change_returns_manual_update_required_before_driver_start(self):
        previous = AmbulanceReturnRequest(
            task_id="disinfection-time-update",
            created_at=datetime(2026, 7, 13, 8, 0),
            raw_text="",
            case_date="2026/07/13",
            case_time="0805",
            vehicle="新坡92",
            disinfection_items=["救護車體"],
        )
        current = AmbulanceReturnRequest.from_dict({**previous.to_dict(), "case_time": "0810"})
        context = {"previous_task": previous.to_dict(), "current_task": current.to_dict()}

        with tempfile.TemporaryDirectory() as tmp, patch.object(selenium_local_module, "_create_driver") as create_driver:
            result = selenium_local_module.run_disinfection_task(
                current,
                Path(tmp),
                update_context=context,
            )

        self.assertEqual(result.status, "disinfection_waiting_confirmation")
        self.assertIn("人工", result.detail)
        create_driver.assert_not_called()
    def test_work_log_case_match_prefers_exact_case_id_and_all_metadata(self):
        request = AmbulanceReturnRequest(
            task_id="match-exact",
            created_at=datetime(2026, 7, 13, 9, 0),
            raw_text="",
            case_id="20260713080500002",
            case_date="2026-07-13",
            case_time="0805",
            case_address="桃園市中壢區月桃路一段270巷52號",
        )
        cases = [
            {
                "case_id": "20260713080500001",
                "case_date": "1150713",
                "case_time_hhmm": "0805",
                "address": "桃園市中壢區月桃路一段270巷52號",
            },
            {
                "case_id": "20260713080500002",
                "case_date": "1150713",
                "case_time_hhmm": "0805",
                "address": "桃園市中壢區月桃路一段270巷52號",
            },
        ]

        matched = selenium_local_module._match_case_for_request(cases, request)

        self.assertIsNotNone(matched)
        self.assertEqual(matched["case_id"], request.case_id)

    def test_work_log_case_match_falls_back_to_unique_date_time_address_when_explicit_case_id_is_absent(self):
        request = AmbulanceReturnRequest(
            task_id="match-missing",
            created_at=datetime(2026, 7, 13, 9, 0),
            raw_text="",
            case_id="20260713080599999",
            case_date="2026-07-13",
            case_time="0805",
            case_address="桃園市中壢區月桃路一段270巷52號",
        )
        cases = [
            {
                "case_id": "20260713080500001",
                "case_date": "1150713",
                "case_time_hhmm": "0805",
                "address": "桃園市中壢區月桃路一段270巷52號",
            }
        ]

        matched = selenium_local_module._match_case_for_request(cases, request)

        self.assertIsNotNone(matched)
        self.assertEqual(matched["case_id"], "20260713080500001")

    def test_work_log_case_match_ignores_existing_wrong_id_when_full_metadata_identifies_another_case(self):
        request = AmbulanceReturnRequest(
            task_id="match-conflicting-id",
            created_at=datetime(2026, 7, 14, 9, 0),
            raw_text="",
            case_id="20260713095447003",
            case_date="2026-07-13",
            case_time="2249",
            case_address="桃園市觀音區福山路三段476號",
        )
        cases = [
            {
                "case_id": "20260713095447003",
                "case_date": "1150713",
                "case_time_hhmm": "0954",
                "address": "桃園市觀音區福祥路一段333號-創傷拒送",
            },
            {
                "case_id": "20260713224929003",
                "case_date": "1150713",
                "case_time_hhmm": "2249",
                "address": "桃園市觀音區福山路三段476號-車禍拒送",
            },
        ]

        matched = selenium_local_module._match_case_for_request(cases, request)

        self.assertIsNotNone(matched)
        self.assertEqual(matched["case_id"], "20260713224929003")

    def test_work_log_case_match_fails_closed_when_wrong_case_id_fallback_is_ambiguous(self):
        request = AmbulanceReturnRequest(
            task_id="match-ambiguous",
            created_at=datetime(2026, 7, 13, 9, 0),
            raw_text="",
            case_id="20260713080599999",
            case_date="2026-07-13",
            case_time="0805",
            case_address="桃園市中壢區月桃路一段270巷52號",
        )
        cases = [
            {
                "case_id": f"2026071308050000{index}",
                "case_date": "1150713",
                "case_time_hhmm": "0805",
                "address": "桃園市中壢區月桃路一段270巷52號",
            }
            for index in (1, 2)
        ]

        self.assertIsNone(selenium_local_module._match_case_for_request(cases, request))

    def test_work_log_case_match_rejects_missing_candidate_address_when_request_has_address(self):
        request = AmbulanceReturnRequest(
            task_id="match-address",
            created_at=datetime(2026, 7, 13, 9, 0),
            raw_text="",
            case_id="20260713080500002",
            case_date="2026-07-13",
            case_time="0805",
            case_address="No. 70 Zhongshan Road",
        )
        cases = [
            {
                "case_id": request.case_id,
                "case_date": "1150713",
                "case_time_hhmm": "0805",
                "address": "",
            }
        ]

        self.assertIsNone(selenium_local_module._match_case_for_request(cases, request))

    def test_work_log_case_match_does_not_fallback_to_neighboring_house_number(self):
        request = AmbulanceReturnRequest(
            task_id="match-neighbor-address",
            created_at=datetime(2026, 7, 14, 9, 0),
            raw_text="",
            case_id="20260713095447003",
            case_date="2026-07-13",
            case_time="2249",
            case_address="桃園市觀音區福山路三段476號",
        )
        cases = [
            {
                "case_id": "20260713095447003",
                "case_date": "1150713",
                "case_time_hhmm": "0954",
                "address": "桃園市觀音區福祥路一段333號",
            },
            {
                "case_id": "20260713224929004",
                "case_date": "1150713",
                "case_time_hhmm": "2249",
                "address": "桃園市觀音區福山路三段476號之1",
            },
        ]

        self.assertIsNone(selenium_local_module._match_case_for_request(cases, request))

    def test_disinfection_selector_rejects_time_match_when_vehicle_mismatches(self):
        rows = [{"index": 0, "text": "2026/07/13 08:05:00 新坡93 明細"}]

        self.assertIsNone(selenium_local_module._select_disinfection_detail_row(rows, "0805", "新坡92"))

    def test_disinfection_waits_raise_when_required_page_state_never_appears(self):
        class FakeDriver:
            def execute_script(self, _script):
                return False

        for wait_function in (
            selenium_local_module._wait_for_disinfection_query_fields,
            selenium_local_module._wait_for_disinfection_query_completed,
            selenium_local_module._wait_for_disinfection_detail_ready,
        ):
            with self.subTest(wait_function=wait_function.__name__), self.assertRaises(
                selenium_local_module.TimeoutException
            ):
                wait_function(FakeDriver(), timeout=0)

    def test_disinfection_partial_item_update_stops_before_save(self):
        request = AmbulanceReturnRequest(
            task_id="partial-disinfection",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
            case_time="0805",
            disinfection_items=["救護車體", "擔架床"],
        )

        class SwitchTo:
            def default_content(self):
                pass

        class FakeDriver:
            switch_to = SwitchTo()

            def get(self, _url):
                pass

        with patch.object(selenium_local_module, "_switch_to_disinfection_content_if_present"), patch.object(
            selenium_local_module, "_wait_for_disinfection_query_fields"
        ), patch.object(selenium_local_module, "_set_disinfection_query_date"), patch.object(
            selenium_local_module, "_click_disinfection_query", return_value=True
        ), patch.object(selenium_local_module, "_wait_for_disinfection_query_completed"), patch.object(
            selenium_local_module, "_save_disinfection_progress_artifacts"
        ), patch.object(selenium_local_module, "_assert_disinfection_not_login"), patch.object(
            selenium_local_module, "_open_disinfection_detail_for_case", return_value=True
        ), patch.object(selenium_local_module, "_wait_for_disinfection_detail_ready"), patch.object(
            selenium_local_module, "_set_disinfection_item_statuses", return_value=1
        ), patch.object(selenium_local_module, "_save_disinfection_record_enabled", return_value=True), patch.object(
            selenium_local_module, "_click_disinfection_save", return_value=True
        ) as save:
            expected_count = len(selenium_local_module._effective_disinfection_items(request.disinfection_items))
            with self.assertRaisesRegex(
                selenium_local_module.WebDriverException,
                f"updated=1 expected={expected_count}",
            ):
                selenium_local_module._prepare_disinfection_record(FakeDriver(), request, Path("artifacts"))

        save.assert_not_called()

    def test_click_only_save_helpers_treat_silent_submit_as_saved(self):
        class FakeDriver:
            current_url = "https://ppe.tyfd.gov.tw/CarRecord/List"
            page_source = ""

            def execute_script(self, _script, *_args):
                return True

        driver = FakeDriver()
        request = AmbulanceReturnRequest(
            task_id="save-no-confirm",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        with patch.object(selenium_local_module, "_accept_alert_if_present", return_value=""), patch.object(
            selenium_local_module, "_confirm_sweetalert_if_present", return_value=""
        ), patch.object(selenium_local_module, "_is_ppe_login_page", return_value=False), patch.object(
            selenium_local_module.time, "sleep"
        ):
            mileage = selenium_local_module._save_vehicle_mileage_form(driver)
            fuel = selenium_local_module._save_fuel_record_form(driver, request)

        self.assertNotIn("waiting_confirmation", mileage)
        self.assertNotIn("waiting_confirmation", fuel)
        self.assertIn("已填寫車輛里程", mileage)
        self.assertIn("已填寫加油紀錄", fuel)
        self.assertEqual(
            selenium_local_module._confirmation_aware_status(
                "vehicle_mileage", mileage, save_enabled=True, prefilled_status="vehicle_mileage_prefilled"
            ),
            "vehicle_mileage_saved",
        )
        self.assertEqual(
            selenium_local_module._confirmation_aware_status(
                "fuel_record", fuel, save_enabled=True, prefilled_status="fuel_record_prefilled"
            ),
            "fuel_record_saved",
        )

    def test_click_only_save_helpers_keep_unknown_nonempty_confirmation_waiting(self):
        class FakeDriver:
            current_url = "https://ppe.tyfd.gov.tw/CarRecord/List"
            page_source = ""

            def execute_script(self, _script, *_args):
                return True

        driver = FakeDriver()
        request = AmbulanceReturnRequest(
            task_id="save-unknown-confirm",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        with patch.object(
            selenium_local_module, "_accept_alert_if_present", return_value="權限狀態不明"
        ), patch.object(
            selenium_local_module, "_confirm_sweetalert_if_present", return_value=""
        ), patch.object(
            selenium_local_module, "_is_ppe_login_page", return_value=False
        ), patch.object(selenium_local_module.time, "sleep"):
            mileage = selenium_local_module._save_vehicle_mileage_form(driver)
            fuel = selenium_local_module._save_fuel_record_form(driver, request)

        self.assertIn(selenium_local_module.WAITING_CONFIRMATION_MARKER, mileage)
        self.assertIn(selenium_local_module.WAITING_CONFIRMATION_MARKER, fuel)

    def test_disinfection_silent_submit_is_saved_when_no_error_is_reported(self):
        request = AmbulanceReturnRequest(
            task_id="silent-disinfection",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
            case_time="0805",
            disinfection_items=["救護車體"],
        )

        class SwitchTo:
            def default_content(self):
                pass

        class FakeDriver:
            switch_to = SwitchTo()

            def get(self, _url):
                pass

        with patch.object(selenium_local_module, "_switch_to_disinfection_content_if_present"), patch.object(
            selenium_local_module, "_wait_for_disinfection_query_fields"
        ), patch.object(selenium_local_module, "_set_disinfection_query_date"), patch.object(
            selenium_local_module, "_click_disinfection_query", return_value=True
        ), patch.object(selenium_local_module, "_wait_for_disinfection_query_completed"), patch.object(
            selenium_local_module, "_save_disinfection_progress_artifacts"
        ), patch.object(selenium_local_module, "_assert_disinfection_not_login"), patch.object(
            selenium_local_module, "_open_disinfection_detail_for_case", return_value=True
        ), patch.object(selenium_local_module, "_wait_for_disinfection_detail_ready"), patch.object(
            selenium_local_module, "_set_disinfection_item_statuses", return_value=1
        ), patch.object(selenium_local_module, "_save_disinfection_record_enabled", return_value=True), patch.object(
            selenium_local_module, "_click_disinfection_save", return_value=True
        ), patch.object(selenium_local_module, "_accept_alert_if_present", return_value=""), patch.object(
            selenium_local_module, "_confirm_sweetalert_if_present", return_value=""
        ):
            detail = selenium_local_module._prepare_disinfection_record(
                FakeDriver(), request, Path("artifacts")
            )

        self.assertNotIn("waiting_confirmation", detail)
        self.assertIn("saved", detail)
        self.assertEqual(
            selenium_local_module._confirmation_aware_status(
                "disinfection", detail, save_enabled=True, prefilled_status="disinfection_prefilled"
            ),
            "disinfection_saved",
        )

    def test_disinfection_unknown_nonempty_confirmation_remains_waiting(self):
        request = AmbulanceReturnRequest(
            task_id="unknown-disinfection",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
            case_time="0805",
            disinfection_items=["救護車體"],
        )

        class SwitchTo:
            def default_content(self):
                pass

        class FakeDriver:
            switch_to = SwitchTo()

            def get(self, _url):
                pass

        with patch.object(selenium_local_module, "_switch_to_disinfection_content_if_present"), patch.object(
            selenium_local_module, "_wait_for_disinfection_query_fields"
        ), patch.object(selenium_local_module, "_set_disinfection_query_date"), patch.object(
            selenium_local_module, "_click_disinfection_query", return_value=True
        ), patch.object(selenium_local_module, "_wait_for_disinfection_query_completed"), patch.object(
            selenium_local_module, "_save_disinfection_progress_artifacts"
        ), patch.object(selenium_local_module, "_assert_disinfection_not_login"), patch.object(
            selenium_local_module, "_open_disinfection_detail_for_case", return_value=True
        ), patch.object(selenium_local_module, "_wait_for_disinfection_detail_ready"), patch.object(
            selenium_local_module, "_set_disinfection_item_statuses", return_value=1
        ), patch.object(selenium_local_module, "_save_disinfection_record_enabled", return_value=True), patch.object(
            selenium_local_module, "_click_disinfection_save", return_value=True
        ), patch.object(
            selenium_local_module, "_accept_alert_if_present", return_value="權限狀態不明"
        ), patch.object(selenium_local_module, "_confirm_sweetalert_if_present", return_value=""):
            detail = selenium_local_module._prepare_disinfection_record(
                FakeDriver(), request, Path("artifacts")
            )

        self.assertIn(selenium_local_module.WAITING_CONFIRMATION_MARKER, detail)

    def test_all_record_submit_helpers_check_cancellation_before_driver_side_effect(self):
        helper_names = (
            "_click_duty_work_log_save",
            "_click_vehicle_mileage_save",
            "_save_fuel_record_form",
            "_click_disinfection_save",
        )
        for helper_name in helper_names:
            helper = getattr(selenium_local_module, helper_name)
            self.assertIn(
                "cancel_check",
                inspect.signature(helper).parameters,
                f"{helper_name} must expose a last-moment cancellation gate",
            )

        class Cancelled(RuntimeError):
            pass

        class FakeDriver:
            execute_calls = 0

            def execute_script(self, *_args):
                self.execute_calls += 1
                return True

        request = AmbulanceReturnRequest(
            task_id="cancel-before-save",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )

        def cancel():
            raise Cancelled("stale claim")

        for helper_name, invoke in (
            (
                "_click_duty_work_log_save",
                lambda driver: selenium_local_module._click_duty_work_log_save(driver, cancel_check=cancel),
            ),
            (
                "_click_vehicle_mileage_save",
                lambda driver: selenium_local_module._click_vehicle_mileage_save(driver, cancel_check=cancel),
            ),
            (
                "_save_fuel_record_form",
                lambda driver: selenium_local_module._save_fuel_record_form(driver, request, cancel_check=cancel),
            ),
            (
                "_click_disinfection_save",
                lambda driver: selenium_local_module._click_disinfection_save(driver, cancel_check=cancel),
            ),
        ):
            with self.subTest(helper=helper_name):
                driver = FakeDriver()
                with self.assertRaises(Cancelled):
                    invoke(driver)
                self.assertEqual(driver.execute_calls, 0)

    def test_vehicle_mileage_fallback_save_rechecks_cancellation_before_click(self):
        class Cancelled(RuntimeError):
            pass

        class FakeDriver:
            def __init__(self):
                self.execute_calls = 0

            def execute_script(self, *_args):
                self.execute_calls += 1
                return False

        checks = {"count": 0}

        def cancel():
            checks["count"] += 1
            if checks["count"] >= 2:
                raise Cancelled("stale claim while locating fallback save")

        driver = FakeDriver()
        with self.assertRaises(Cancelled):
            _click_vehicle_mileage_save(driver, cancel_check=cancel)

        self.assertEqual(checks["count"], 2)
        self.assertEqual(driver.execute_calls, 1)

    def test_disinfection_fallback_save_rechecks_cancellation_before_click(self):
        class Cancelled(RuntimeError):
            pass

        class FakeDriver:
            def __init__(self):
                self.execute_calls = 0

            def execute_script(self, *_args):
                self.execute_calls += 1
                return False

        checks = {"count": 0}

        def cancel():
            checks["count"] += 1
            if checks["count"] >= 2:
                raise Cancelled("stale claim while locating fallback save")

        driver = FakeDriver()
        with self.assertRaises(Cancelled):
            _click_disinfection_save(driver, cancel_check=cancel)

        self.assertEqual(checks["count"], 2)
        self.assertEqual(driver.execute_calls, 1)

    def test_selenium_site_wrappers_propagate_task_cancellation(self):
        cancellation_error = getattr(selenium_local_module, "TaskCancellationError", None)
        self.assertIsNotNone(cancellation_error, "TaskCancellationError is required for worker fencing")

        class FakeDriver:
            page_source = ""

            def implicitly_wait(self, _seconds):
                pass

        signal = cancellation_error("stale claim")
        request = AmbulanceReturnRequest(task_id="cancel-wrapper", created_at=datetime.now(), raw_text="")
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            selenium_local_module,
            "mark_driver_operation_active",
        ), patch.object(selenium_local_module, "apply_tile"), patch.object(
            selenium_local_module,
            "_set_window_size_if_enabled",
        ):
            specs = (
                (
                    selenium_local_module.run_local_selenium_task,
                    "_prepare_duty_work_log_form",
                    {"force_new_driver": True},
                ),
                (
                    selenium_local_module.run_vehicle_mileage_task,
                    "_open_vehicle_mileage_page",
                    {"existing_driver": FakeDriver()},
                ),
                (
                    selenium_local_module.run_fuel_record_task,
                    "_open_fuel_record_page",
                    {"existing_driver": FakeDriver()},
                ),
                (
                    selenium_local_module.run_disinfection_task,
                    "_open_disinfection_page",
                    {"existing_driver": FakeDriver()},
                ),
            )
            for runner, inner_name, kwargs in specs:
                with self.subTest(runner=runner.__name__), patch.object(
                    selenium_local_module,
                    "_create_driver",
                    return_value=FakeDriver(),
                ), patch.object(selenium_local_module, inner_name, side_effect=signal):
                    with self.assertRaises(cancellation_error):
                        runner(request, Path(temp_dir), use_session_lock=False, **kwargs)

    def test_site_wrappers_do_not_mark_waiting_confirmation_as_saved(self):
        request = AmbulanceReturnRequest(
            task_id="wrapper-waiting",
            created_at=datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )

        class FakeDriver:
            def implicitly_wait(self, _seconds):
                pass

        cases = (
            ("vehicle_mileage", selenium_local_module.run_vehicle_mileage_task, "_open_vehicle_mileage_page"),
            ("fuel_record", selenium_local_module.run_fuel_record_task, "_open_fuel_record_page"),
            ("disinfection", selenium_local_module.run_disinfection_task, "_open_disinfection_page"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for site_key, runner, open_name in cases:
                with self.subTest(site_key=site_key), patch.object(
                    selenium_local_module, open_name, return_value="waiting_confirmation: save response not confirmed"
                ), patch.object(selenium_local_module, "apply_tile"), patch.object(
                    selenium_local_module, "_set_window_size_if_enabled"
                ):
                    result = runner(
                        request,
                        Path(tmp),
                        existing_driver=FakeDriver(),
                        use_session_lock=False,
                    )
                self.assertEqual(result.status, f"{site_key}_waiting_confirmation")

    def test_duty_work_log_silent_submit_is_saved_when_click_succeeds(self):
        request = AmbulanceReturnRequest(
            task_id="duty-waiting",
            created_at=datetime.now(),
            raw_text="",
            case_id="20260713080500001",
            case_time="0805",
        )

        class FakeDriver:
            def get(self, _url):
                pass

        with patch.object(selenium_local_module, "_ensure_duty_login", return_value=True), patch.object(
            selenium_local_module, "_click_by_text_or_id"
        ), patch.object(selenium_local_module, "_switch_to_window_containing"), patch.object(
            selenium_local_module, "_set_case_query_date_range"
        ), patch.object(selenium_local_module, "_click_query_if_present"), patch.object(
            selenium_local_module, "_extract_all_emergency_cases", return_value=[{"case_id": request.case_id}]
        ), patch.object(
            selenium_local_module, "_match_case_for_request", return_value={"case_id": request.case_id}
        ), patch.object(selenium_local_module, "_click_case_choose", return_value=True), patch.object(
            selenium_local_module, "_switch_to_work_log_form_for_case"
        ), patch.object(selenium_local_module, "_fill_duty_work_log_values", return_value=[]), patch.object(
            selenium_local_module, "_save_artifacts"
        ), patch.object(selenium_local_module, "_save_duty_work_log_enabled", return_value=True), patch.object(
            selenium_local_module, "_click_duty_work_log_save", return_value={"ok": True}
        ), patch.object(selenium_local_module.time, "sleep"):
            result = selenium_local_module._prepare_duty_work_log_form(
                FakeDriver(), request, Path("artifacts"), Path("summary.txt")
            )

        self.assertEqual(result.status, "duty_work_log_saved")

    def test_duty_work_log_unknown_nonempty_confirmation_remains_waiting(self):
        request = AmbulanceReturnRequest(
            task_id="duty-unknown-confirm",
            created_at=datetime.now(),
            raw_text="",
            case_id="20260713080500001",
            case_time="0805",
        )

        class FakeDriver:
            def get(self, _url):
                pass

        with patch.object(selenium_local_module, "_ensure_duty_login", return_value=True), patch.object(
            selenium_local_module, "_click_by_text_or_id"
        ), patch.object(selenium_local_module, "_switch_to_window_containing"), patch.object(
            selenium_local_module, "_set_case_query_date_range"
        ), patch.object(selenium_local_module, "_click_query_if_present"), patch.object(
            selenium_local_module, "_extract_all_emergency_cases", return_value=[{"case_id": request.case_id}]
        ), patch.object(
            selenium_local_module, "_match_case_for_request", return_value={"case_id": request.case_id}
        ), patch.object(selenium_local_module, "_click_case_choose", return_value=True), patch.object(
            selenium_local_module, "_switch_to_work_log_form_for_case"
        ), patch.object(selenium_local_module, "_fill_duty_work_log_values", return_value=[]), patch.object(
            selenium_local_module, "_save_artifacts"
        ), patch.object(selenium_local_module, "_save_duty_work_log_enabled", return_value=True), patch.object(
            selenium_local_module,
            "_click_duty_work_log_save",
            return_value={"ok": True, "alert": "權限狀態不明"},
        ), patch.object(selenium_local_module.time, "sleep"):
            result = selenium_local_module._prepare_duty_work_log_form(
                FakeDriver(), request, Path("artifacts"), Path("summary.txt")
            )

        self.assertEqual(result.status, "duty_work_log_waiting_confirmation")

    def test_duty_work_log_save_waits_for_delayed_alert(self):
        class FakeDriver:
            def execute_script(self, _script, *_args):
                return {"ok": True}

        driver = FakeDriver()
        with patch.object(
            selenium_local_module,
            "_accept_alert_if_present",
            return_value="權限不足",
        ) as accept_alert:
            result = selenium_local_module._click_duty_work_log_save(driver)

        accept_alert.assert_called_once_with(driver, timeout=2)
        self.assertEqual(result["alert"], "權限不足")

    def test_operation_failure_after_driver_creation_is_not_chrome_start_failed(self):
        request = AmbulanceReturnRequest(
            task_id="operation-failed",
            created_at=datetime.now(),
            raw_text="",
        )

        class FakeDriver:
            def implicitly_wait(self, _seconds):
                pass

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            selenium_local_module, "_create_driver", return_value=FakeDriver()
        ), patch.object(selenium_local_module, "apply_tile"), patch.object(
            selenium_local_module, "_set_window_size_if_enabled"
        ), patch.object(
            selenium_local_module, "_prepare_duty_work_log_form", side_effect=RuntimeError("form changed")
        ), patch.object(selenium_local_module, "_save_artifacts"):
            result = selenium_local_module.run_local_selenium_task(
                request, Path(tmp), use_session_lock=False, force_new_driver=True
            )

        self.assertEqual(result.status, "duty_work_log_failed")
    def test_ppe_option_records_decode_unicode_driver_names(self):
        source = 'dataSource: [{"DeptSeq":null,"Value":"2448","Text":"\\u90ED\\u570B\\u5075"}]'

        options = _ppe_option_records_from_script(source)

        self.assertEqual(_ppe_option_value(options, "郭國偵"), "2448")

    def test_ppe_option_value_accepts_reordered_fields_and_normalized_whitespace(self):
        options = [{"Text": " 郭  國偵 ", "DeptSeq": None, "Value": "2448"}]

        self.assertEqual(_ppe_option_value(options, "郭 國偵"), "2448")

    def test_ppe_option_value_rejects_partial_name_and_zero_id(self):
        self.assertEqual(_ppe_option_value([{"Text": "郭國偵", "Value": "2448"}], "郭國"), "")
        self.assertEqual(_ppe_option_value([{"Text": "郭國偵", "Value": "0"}], "郭國偵"), "")

    def test_ppe_option_names_are_unique_and_bounded(self):
        options = [
            {"Text": "郭國偵", "Value": "2448"},
            {"DriverName": "郭國偵", "Driver": "2448"},
            {"Name": "陳俊翰", "Id": "2481"},
        ]

        self.assertEqual(_ppe_option_names(options, limit=1), ["郭國偵"])

    def test_wait_for_fuel_driver_value_decodes_unicode_options(self):
        class FakeDriver:
            def execute_script(self, _script: str):
                return 'dataSource: [{"Value":"2448","Text":"\\u90ED\\u570B\\u5075"}]'

        self.assertEqual(_wait_for_fuel_driver_value(FakeDriver(), "郭國偵", timeout=0), "2448")

    def test_wait_for_fuel_driver_value_reports_requested_and_candidates(self):
        class FakeDriver:
            def execute_script(self, _script: str):
                return (
                    'dataSource: ['
                    '{"Value":"2448","Text":"\\u90ED\\u570B\\u5075"},'
                    '{"Value":"2481","Text":"\\u9673\\u4FCA\\u7FF0"}'
                    ']'
                )

        with self.assertRaisesRegex(
            selenium_local_module.WebDriverException,
            "missing fuel driver: requested=林志偉; candidates=郭國偵,陳俊翰",
        ):
            _wait_for_fuel_driver_value(FakeDriver(), "林志偉", timeout=0)

    def test_fill_fuel_grid_uses_resolved_driver_without_existing_rows(self):
        class FakeDriver:
            def __init__(self):
                self.driver_value = ""

            def execute_script(self, script: str, *args):
                if "return Array.from(document.scripts)" in script:
                    return 'dataSource: [{"Value":"2448","Text":"\\u90ED\\u570B\\u5075"}]'
                self.driver_value = str(args[1])
                return {"ok": True}

        request = AmbulanceReturnRequest.from_dict(
            {
                "vehicle": "新坡92",
                "driver": "郭國偵",
                "fuel_record": {
                    "enabled": True,
                    "date": "20260713",
                    "time": "1320",
                    "driver": "郭國偵",
                    "product": "超級柴油",
                    "quantity": "42.122",
                    "unit_price": "30.3",
                },
            }
        )
        driver = FakeDriver()

        _fill_fuel_grid_record(driver, request)

        self.assertEqual(driver.driver_value, "2448")

    def test_vehicle_mileage_driver_value_uses_exact_option(self):
        class FakeDriver:
            def execute_script(self, _script: str, _row_index: int):
                return {
                    "options": [
                        {"Text": "郭國偵", "Value": "2448"},
                        {"Text": "郭國", "Value": "9999"},
                    ],
                    "row_driver": "8888",
                    "row_driver_name": "其他人",
                }

        self.assertEqual(_vehicle_mileage_driver_value(FakeDriver(), "郭國偵"), "2448")

    def test_vehicle_mileage_driver_value_rejects_different_existing_row(self):
        class FakeDriver:
            def execute_script(self, _script: str, _row_index: int):
                return {
                    "options": [{"Text": "陳俊翰", "Value": "2481"}],
                    "row_driver": "8888",
                    "row_driver_name": "其他人",
                }

        with self.assertRaisesRegex(
            selenium_local_module.WebDriverException,
            "missing vehicle mileage driver: requested=郭國偵; candidates=陳俊翰",
        ):
            _vehicle_mileage_driver_value(FakeDriver(), "郭國偵")

    def test_fill_vehicle_grid_passes_exact_driver_id(self):
        class FakeDriver:
            def __init__(self):
                self.driver_value = ""

            def execute_script(self, script: str, *args):
                if "row_driver_name" in script:
                    return {
                        "options": [{"Text": "郭國偵", "Value": "2448"}],
                        "row_driver": "",
                        "row_driver_name": "",
                    }
                self.driver_value = str(args[2])
                return []

        driver = FakeDriver()
        values = {
            "開始日期": "20260713",
            "開始時間": "1101",
            "結束日期": "20260713",
            "結束時間": "1320",
            "開始里程": "20828",
            "結束里程": "20910",
            "事由": "急病",
            "前往地點": "新華路一段886號",
            "駕駛人": "郭國偵",
        }

        _fill_vehicle_grid_values(driver, values)

        self.assertEqual(driver.driver_value, "2448")

    def test_selenium_enabled_default_and_false(self):
        os.environ.pop("USE_LOCAL_SELENIUM", None)
        self.assertTrue(selenium_enabled())
        os.environ["USE_LOCAL_SELENIUM"] = "false"
        self.assertFalse(selenium_enabled())

    def test_save_flags_read_environment(self):
        previous_vehicle = os.environ.get("SAVE_VEHICLE_MILEAGE")
        previous_duty = os.environ.get("SAVE_DUTY_WORK_LOG")
        previous_disinfection = os.environ.get("SAVE_DISINFECTION_RECORD")
        previous_probe = os.environ.get("SAVE_DISINFECTION_PROBE")
        try:
            os.environ["SAVE_VEHICLE_MILEAGE"] = "true"
            os.environ["SAVE_DUTY_WORK_LOG"] = "yes"
            os.environ["SAVE_DISINFECTION_RECORD"] = "1"
            os.environ["SAVE_DISINFECTION_PROBE"] = "1"
            self.assertTrue(_save_vehicle_mileage_enabled())
            self.assertTrue(_save_duty_work_log_enabled())
            self.assertTrue(_save_disinfection_record_enabled())
            self.assertTrue(_save_disinfection_probe_enabled())
            os.environ.pop("SAVE_VEHICLE_MILEAGE", None)
            os.environ.pop("SAVE_DUTY_WORK_LOG", None)
            os.environ.pop("SAVE_DISINFECTION_RECORD", None)
            os.environ.pop("SAVE_DISINFECTION_PROBE", None)
            self.assertTrue(_save_vehicle_mileage_enabled())
            self.assertTrue(_save_duty_work_log_enabled())
            self.assertTrue(_save_disinfection_record_enabled())
            self.assertFalse(_save_disinfection_probe_enabled())
            os.environ["SAVE_VEHICLE_MILEAGE"] = "false"
            os.environ["SAVE_DUTY_WORK_LOG"] = "0"
            os.environ["SAVE_DISINFECTION_RECORD"] = "0"
            os.environ["SAVE_DISINFECTION_PROBE"] = "0"
            self.assertFalse(_save_vehicle_mileage_enabled())
            self.assertFalse(_save_duty_work_log_enabled())
            self.assertFalse(_save_disinfection_record_enabled())
            self.assertFalse(_save_disinfection_probe_enabled())
        finally:
            if previous_vehicle is None:
                os.environ.pop("SAVE_VEHICLE_MILEAGE", None)
            else:
                os.environ["SAVE_VEHICLE_MILEAGE"] = previous_vehicle
            if previous_duty is None:
                os.environ.pop("SAVE_DUTY_WORK_LOG", None)
            else:
                os.environ["SAVE_DUTY_WORK_LOG"] = previous_duty
            if previous_disinfection is None:
                os.environ.pop("SAVE_DISINFECTION_RECORD", None)
            else:
                os.environ["SAVE_DISINFECTION_RECORD"] = previous_disinfection
            if previous_probe is None:
                os.environ.pop("SAVE_DISINFECTION_PROBE", None)
            else:
                os.environ["SAVE_DISINFECTION_PROBE"] = previous_probe

    def test_vehicle_mileage_message_reports_silent_save_without_false_warning(self):
        source = Path(selenium_local_module.__file__).read_text(encoding="utf-8")

        self.assertNotIn("\\u5617\\u8a66\\u78ba\\u8a8d", source)
        self.assertIn("\\u6309\\u4e0b\\u78ba\\u8a8d", source)
        self.assertIn("\\u7db2\\u7ad9\\u672a\\u56de\\u5831\\u932f\\u8aa4", source)
        self.assertNotIn("\\u5c1a\\u672a\\u78ba\\u8a8d\\u4f3a\\u670d\\u5668\\u5df2\\u5132\\u5b58", source)

    def test_vehicle_mileage_runs_even_without_fuel_record(self):
        request = AmbulanceReturnRequest(
            task_id="task-mileage-no-fuel",
            created_at=datetime.now(),
            raw_text="",
            vehicle="\u65b0\u576191",
            driver="\u694a\u4ef2\u8c6a",
            mileage="143635",
        )
        opened: dict[str, object] = {}

        def fake_open(driver, opened_request, output_dir, update_context=None):
            opened["request"] = opened_request
            return "mileage ok"

        class FakeDriver:
            page_source = ""

            def implicitly_wait(self, seconds):
                self.wait_seconds = seconds

        original_open = selenium_local_module._open_vehicle_mileage_page
        original_save_enabled = selenium_local_module._save_vehicle_mileage_enabled
        original_apply_tile = selenium_local_module.apply_tile
        original_set_window = selenium_local_module._set_window_size_if_enabled
        try:
            selenium_local_module._open_vehicle_mileage_page = fake_open
            selenium_local_module._save_vehicle_mileage_enabled = lambda: True
            selenium_local_module.apply_tile = lambda _driver, _tile_name: None
            selenium_local_module._set_window_size_if_enabled = lambda _driver, _site_key: None
            with tempfile.TemporaryDirectory() as tmp:
                result = selenium_local_module.run_vehicle_mileage_task(
                    request,
                    Path(tmp),
                    existing_driver=FakeDriver(),
                    use_session_lock=False,
                )
        finally:
            selenium_local_module._open_vehicle_mileage_page = original_open
            selenium_local_module._save_vehicle_mileage_enabled = original_save_enabled
            selenium_local_module.apply_tile = original_apply_tile
            selenium_local_module._set_window_size_if_enabled = original_set_window

        self.assertEqual(result.status, "vehicle_mileage_saved")
        self.assertEqual(result.detail, "mileage ok")
        self.assertIs(opened["request"], request)

    def test_resolve_end_mileage_accepts_delta(self):
        self.assertEqual(_resolve_end_mileage("123400", "+50"), "123450")
        self.assertEqual(_resolve_end_mileage("123400", "123456"), "123456")

    def test_vehicle_mileage_previous_request_reads_update_context(self):
        previous = {
            "task_id": "task-old",
            "created_at": "2026-06-15T08:00:00",
            "raw_text": "",
            "vehicle": "\u65b0\u576191",
            "mileage": "142842",
        }

        request = _vehicle_mileage_previous_request({"previous_task": previous})

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.vehicle, "\u65b0\u576191")
        self.assertEqual(request.mileage, "142842")

    def test_vehicle_mileage_previous_request_maps_second_vehicle_by_stable_index(self):
        self.assertIn(
            "current_request",
            inspect.signature(_vehicle_mileage_previous_request).parameters,
            "multi-vehicle updates must identify the current vehicle request",
        )
        previous_task = {
            "task_id": "task-multi-old",
            "created_at": "2026-07-13T08:00:00",
            "two_vehicle": True,
            "vehicle_entries": [
                {"vehicle": "新坡92", "driver": "甲", "mileage": "101", "return_time": "0900"},
                {"vehicle": "新坡93", "driver": "乙", "mileage": "202", "return_time": "0910"},
            ],
        }
        current_task = {
            **previous_task,
            "vehicle_entries": [
                {"vehicle": "新坡92", "driver": "甲", "mileage": "101", "return_time": "0900"},
                {"vehicle": "新坡91", "driver": "乙", "mileage": "210", "return_time": "0910"},
            ],
        }
        current_second = AmbulanceReturnRequest.from_dict(current_task).vehicle_requests()[1]
        context = {
            "previous_task": previous_task,
            "current_task": current_task,
            "vehicle_index": 2,
            "vehicle_key": "新坡91",
        }

        previous_second = _vehicle_mileage_previous_request(context, current_second)

        self.assertIsNotNone(previous_second)
        assert previous_second is not None
        self.assertEqual(previous_second.vehicle, "新坡93")
        self.assertEqual(previous_second.mileage, "202")
        self.assertNotEqual(previous_second.vehicle, "新坡92")

    def test_vehicle_mileage_vehicle_change_fails_closed_before_delete_or_add(self):
        self.assertIn(
            "current_request",
            inspect.signature(_vehicle_mileage_previous_request).parameters,
            "multi-vehicle updates must identify the current vehicle request",
        )
        previous_task = {
            "task_id": "task-multi-edit",
            "created_at": "2026-07-13T08:00:00",
            "two_vehicle": True,
            "vehicle_entries": [
                {"vehicle": "新坡92", "driver": "甲", "mileage": "101", "return_time": "0900"},
                {"vehicle": "新坡93", "driver": "乙", "mileage": "202", "return_time": "0910"},
            ],
        }
        current_task = {
            **previous_task,
            "vehicle_entries": [
                {"vehicle": "新坡92", "driver": "甲", "mileage": "101", "return_time": "0900"},
                {"vehicle": "新坡91", "driver": "乙", "mileage": "210", "return_time": "0910"},
            ],
        }
        current_second = AmbulanceReturnRequest.from_dict(current_task).vehicle_requests()[1]
        context = {
            "previous_task": previous_task,
            "current_task": current_task,
            "vehicle_index": 2,
            "vehicle_key": "新坡91",
        }

        class FakeDriver:
            def get(self, _url):
                pass

        with patch.object(selenium_local_module, "_wait_for_ppe_vehicle_mileage_page", return_value=True), patch.object(
            selenium_local_module,
            "_click_text_if_present",
        ), patch.object(selenium_local_module.time, "sleep"), patch.object(
            selenium_local_module,
            "vehicle_ppe_names",
            return_value={"新坡92": "92牌", "新坡93": "93牌", "新坡91": "91牌"},
        ), patch.object(
            selenium_local_module,
            "_select_vehicle_record",
        ) as select_vehicle, patch.object(
            selenium_local_module,
            "_delete_vehicle_mileage_row",
        ) as delete_row, patch.object(
            selenium_local_module,
            "_save_vehicle_mileage_enabled",
            return_value=False,
        ), patch.object(
            selenium_local_module,
            "_add_vehicle_mileage_record",
        ) as add_row:
            with self.assertRaisesRegex(selenium_local_module.WebDriverException, "vehicle change requires manual correction"):
                selenium_local_module._prepare_vehicle_mileage_form(
                    FakeDriver(),
                    current_second,
                    Path("artifacts"),
                    update_context=context,
                )

        select_vehicle.assert_not_called()
        delete_row.assert_not_called()
        add_row.assert_not_called()

    def test_vehicle_mileage_unique_current_row_is_idempotent_before_update_or_add(self):
        matcher = getattr(selenium_local_module, "_vehicle_mileage_matching_row_indices", None)
        self.assertIsNotNone(matcher, "strict current mileage matcher is required")
        request = AmbulanceReturnRequest(
            task_id="mileage-existing",
            created_at=datetime(2026, 7, 13, 8, 0),
            raw_text="",
            case_date="2026/07/13",
            case_time="0805",
            return_date="2026/07/13",
            return_time="0910",
            vehicle="新坡92",
            driver="甲",
            mileage="12345",
            case_address="桃園市中壢區",
        )

        class FakeDriver:
            def get(self, _url):
                pass

        with patch.object(selenium_local_module, "_wait_for_ppe_vehicle_mileage_page", return_value=True), patch.object(
            selenium_local_module,
            "_click_text_if_present",
        ), patch.object(selenium_local_module.time, "sleep"), patch.object(
            selenium_local_module,
            "vehicle_ppe_names",
            return_value={"新坡92": "92牌"},
        ), patch.object(selenium_local_module, "_select_vehicle_record"), patch.object(
            selenium_local_module,
            "_vehicle_mileage_matching_row_indices",
            return_value=[1],
        ), patch.object(selenium_local_module, "_find_vehicle_mileage_row_index") as find_previous, patch.object(
            selenium_local_module,
            "_fill_vehicle_grid_values",
        ) as fill, patch.object(selenium_local_module, "_add_vehicle_mileage_record") as add, patch.object(
            selenium_local_module,
            "_delete_vehicle_mileage_row",
        ) as delete, patch.object(selenium_local_module, "_save_vehicle_mileage_form") as save:
            detail = selenium_local_module._prepare_vehicle_mileage_form(FakeDriver(), request, Path("artifacts"))

        self.assertIn("已存在", detail)
        find_previous.assert_not_called()
        fill.assert_not_called()
        add.assert_not_called()
        delete.assert_not_called()
        save.assert_not_called()

    def test_vehicle_mileage_ambiguous_current_rows_fail_before_mutation(self):
        matcher = getattr(selenium_local_module, "_vehicle_mileage_matching_row_indices", None)
        self.assertIsNotNone(matcher, "strict current mileage matcher is required")
        request = AmbulanceReturnRequest(
            task_id="mileage-ambiguous",
            created_at=datetime(2026, 7, 13, 8, 0),
            raw_text="",
            case_date="2026/07/13",
            case_time="0805",
            return_time="0910",
            vehicle="新坡92",
            driver="甲",
            mileage="12345",
        )

        class FakeDriver:
            def get(self, _url):
                pass

        with patch.object(selenium_local_module, "_wait_for_ppe_vehicle_mileage_page", return_value=True), patch.object(
            selenium_local_module,
            "_click_text_if_present",
        ), patch.object(selenium_local_module.time, "sleep"), patch.object(
            selenium_local_module,
            "vehicle_ppe_names",
            return_value={"新坡92": "92牌"},
        ), patch.object(selenium_local_module, "_select_vehicle_record"), patch.object(
            selenium_local_module,
            "_vehicle_mileage_matching_row_indices",
            return_value=[0, 1],
        ), patch.object(selenium_local_module, "_fill_vehicle_grid_values") as fill, patch.object(
            selenium_local_module,
            "_add_vehicle_mileage_record",
        ) as add, patch.object(selenium_local_module, "_delete_vehicle_mileage_row") as delete:
            with self.assertRaisesRegex(selenium_local_module.WebDriverException, "multiple current mileage rows"):
                selenium_local_module._prepare_vehicle_mileage_form(FakeDriver(), request, Path("artifacts"))

        fill.assert_not_called()
        add.assert_not_called()
        delete.assert_not_called()

    def test_vehicle_mileage_values_keep_existing_start_mileage_for_update(self):
        request = AmbulanceReturnRequest(
            task_id="task-update",
            created_at=datetime(2026, 6, 15, 8, 0),
            raw_text="",
            case_date="2026/06/15",
            case_time="0830",
            return_date="2026/06/15",
            return_time="0910",
            mileage="142900",
            case_address="\u6843\u5712\u5e02\u89c0\u97f3\u5340",
            driver="\u66fe\u5f65\u7db8",
        )

        values = _vehicle_mileage_values(request, "142842")

        self.assertEqual(values["\u958b\u59cb\u91cc\u7a0b"], "142842")
        self.assertEqual(values["\u7d50\u675f\u91cc\u7a0b"], "142900")
        self.assertEqual(values["\u958b\u59cb\u6642\u9593"], "0830")

    def test_disinfection_query_date_uses_case_date(self):
        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=datetime(2026, 6, 7, 1, 0),
            raw_text="",
            case_date="1150606",
            case_time="2350",
            return_time="0010",
        )

        self.assertEqual(_disinfection_query_date(request), "2026-06-06")

    def test_set_disinfection_query_date_does_not_dispatch_field_events(self):
        class FakeDriver:
            script = ""
            args = ()

            def execute_script(self, script: str, *args):
                self.script = script
                self.args = args
                return [True, True]

        driver = FakeDriver()

        _set_disinfection_query_date(driver, "2026-06-06")

        self.assertEqual(driver.args, ("2026-06-06 00:00:00", "2026-06-06 23:59:59"))
        self.assertNotIn("dispatchEvent", driver.script)

    def test_click_disinfection_query_uses_real_button_id(self):
        class FakeDriver:
            script = ""

            def execute_script(self, script: str):
                self.script = script
                return True

        driver = FakeDriver()

        self.assertTrue(_click_disinfection_query(driver))
        self.assertIn("_btnQuery", driver.script)

    def test_open_disinfection_detail_prefers_matching_vehicle_when_times_match(self):
        class FakeDriver:
            def __init__(self):
                self.clicked: list[int] = []

            def execute_script(self, script: str, *args):
                if "querySelectorAll('tr')" in script and "tr, index" in script:
                    return [
                        {"index": 0, "text": "2026/06/02 01:16:52 \u65b0\u576191"},
                        {"index": 1, "text": "2026/06/02 01:16:52 \u65b0\u576192"},
                    ]
                if args and isinstance(args[0], int):
                    self.clicked.append(args[0])
                    return True
                self.clicked.append(0)
                return True

        driver = FakeDriver()

        self.assertTrue(_open_disinfection_detail_for_case(driver, "0116", "\u65b0\u576192"))
        self.assertEqual(driver.clicked, [1])

    def test_assert_disinfection_not_login_raises_on_login_page(self):
        class FakeDriver:
            current_url = "https://emsdt.tyfd.gov.tw/EmmWeb/login"
            page_source = ""

        with self.assertRaises(Exception) as context:
            _assert_disinfection_not_login(FakeDriver(), "entry")

        self.assertIn("entry", str(context.exception))

    def test_site_task_defaults_do_not_use_legacy_chrome_profile(self):
        self.assertEqual(inspect.signature(run_local_selenium_task).parameters["profile_name"].default, "duty_work_log_profile")
        self.assertEqual(inspect.signature(run_vehicle_mileage_task).parameters["profile_name"].default, "vehicle_mileage_profile")
        self.assertEqual(inspect.signature(run_fuel_record_task).parameters["profile_name"].default, "fuel_record_profile")
        self.assertEqual(inspect.signature(run_disinfection_task).parameters["profile_name"].default, "disinfection_profile")

    def test_named_profile_uses_configured_runtime_profile_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("CHROME_PROFILE_DIR")
            previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            try:
                os.environ.pop("CHROME_PROFILE_DIR", None)
                os.environ["SELENIUM_PROFILE_ROOT"] = str(Path(tmp) / "runtime_profiles")

                self.assertEqual(_profile_dir("duty_work_log_profile"), Path(tmp) / "runtime_profiles" / "duty_work_log_profile")
                self.assertEqual(_profile_dir("vehicle_mileage_profile_task1"), Path(tmp) / "runtime_profiles" / "vehicle_mileage_profile_task1")
            finally:
                if previous is None:
                    os.environ.pop("CHROME_PROFILE_DIR", None)
                else:
                    os.environ["CHROME_PROFILE_DIR"] = previous
                if previous_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_root

    def test_legacy_chrome_profile_dir_is_only_used_as_profile_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_profile = os.environ.get("CHROME_PROFILE_DIR")
            previous_root = os.environ.get("AMBULANCE_TEST_PROFILE_ROOT")
            previous_runtime_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            try:
                os.environ["AMBULANCE_TEST_PROFILE_ROOT"] = tmp
                os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                os.environ["CHROME_PROFILE_DIR"] = r"%AMBULANCE_TEST_PROFILE_ROOT%\legacy_chrome_data"

                self.assertEqual(_profile_dir("chrome_profile"), Path(tmp) / "chrome_profile")
                self.assertEqual(_profile_dir("disinfection_profile_task1"), Path(tmp) / "disinfection_profile_task1")
            finally:
                if previous_profile is None:
                    os.environ.pop("CHROME_PROFILE_DIR", None)
                else:
                    os.environ["CHROME_PROFILE_DIR"] = previous_profile
                if previous_root is None:
                    os.environ.pop("AMBULANCE_TEST_PROFILE_ROOT", None)
                else:
                    os.environ["AMBULANCE_TEST_PROFILE_ROOT"] = previous_root
                if previous_runtime_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_runtime_root

    def test_cleanup_stale_selenium_profiles_removes_only_generated_old_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_generated = [
                root / "chrome_profile",
                root / "case_lookup_profile_123",
                root / "disinfection_profile_task1",
            ]
            keep_unknown = root / "notes"
            keep_recent = root / "vehicle_mileage_profile_recent"
            keep_locked = root / "consumables_profile_old"
            keep_locked_main = root / "chrome_profile_locked"
            for path in [*old_generated, keep_unknown, keep_recent, keep_locked, keep_locked_main]:
                path.mkdir()
                (path / "cache.dat").write_text("x", encoding="utf-8")
            (keep_locked / "SingletonLock").write_text("", encoding="utf-8")
            (keep_locked_main / "SingletonLock").write_text("", encoding="utf-8")
            old_time = time.time() - 7200
            for path in [*old_generated, keep_unknown, keep_locked, keep_locked_main]:
                os.utime(path / "cache.dat", (old_time, old_time))
                os.utime(path, (old_time, old_time))

            removed = cleanup_stale_selenium_profiles(root, max_age_hours=1)

            self.assertEqual({path.name for path in removed}, {"chrome_profile", "case_lookup_profile_123", "disinfection_profile_task1"})
            for path in old_generated:
                self.assertFalse(path.exists())
            self.assertTrue(keep_unknown.exists())
            self.assertTrue(keep_recent.exists())
            self.assertTrue(keep_locked.exists())
            self.assertTrue(keep_locked_main.exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locked_main = root / "chrome_profile"
            locked_main.mkdir()
            (locked_main / "cache.dat").write_text("x", encoding="utf-8")
            (locked_main / "SingletonLock").write_text("", encoding="utf-8")
            old_time = time.time() - 7200
            os.utime(locked_main / "cache.dat", (old_time, old_time))
            os.utime(locked_main, (old_time, old_time))

            removed = cleanup_stale_selenium_profiles(root, max_age_hours=1)

            self.assertEqual(removed, [])
            self.assertTrue(locked_main.exists())

    def test_create_driver_cleans_stale_profiles_before_starting_chrome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            root.mkdir()
            old_profile = root / "case_lookup_profile_123"
            old_profile.mkdir()
            (old_profile / "cache.dat").write_text("x", encoding="utf-8")
            old_time = time.time() - 7200
            os.utime(old_profile / "cache.dat", (old_time, old_time))
            os.utime(old_profile, (old_time, old_time))
            previous_profile = os.environ.get("CHROME_PROFILE_DIR")
            previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            previous_max_age = os.environ.get("SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS")
            original_create = selenium_local_module._create_local_driver_with_retry

            class FakeDriver:
                def set_page_load_timeout(self, timeout):
                    self.page_timeout = timeout

                def set_script_timeout(self, timeout):
                    self.script_timeout = timeout

            try:
                os.environ.pop("CHROME_PROFILE_DIR", None)
                os.environ["SELENIUM_PROFILE_ROOT"] = str(root)
                os.environ["SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS"] = "1"
                selenium_local_module._create_local_driver_with_retry = lambda options: FakeDriver()

                driver = _create_driver(Path(tmp), profile_name="disinfection_profile_task-new", headless=True)
            finally:
                selenium_local_module._create_local_driver_with_retry = original_create
                if previous_profile is None:
                    os.environ.pop("CHROME_PROFILE_DIR", None)
                else:
                    os.environ["CHROME_PROFILE_DIR"] = previous_profile
                if previous_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_root
                if previous_max_age is None:
                    os.environ.pop("SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS", None)
                else:
                    os.environ["SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS"] = previous_max_age

            self.assertIsNotNone(driver)
            self.assertFalse(old_profile.exists())
            self.assertTrue((root / "disinfection_profile_task-new").exists())

    def test_create_driver_schedules_auto_close_for_local_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            previous_profile = os.environ.get("CHROME_PROFILE_DIR")
            previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            previous_delay = os.environ.get("WORKER_BROWSER_AUTO_CLOSE_SECONDS")
            original_create = selenium_local_module._create_local_driver_with_retry
            original_schedule = selenium_local_module.schedule_driver_auto_close
            scheduled = []

            class FakeDriver:
                def set_page_load_timeout(self, timeout):
                    self.page_timeout = timeout

                def set_script_timeout(self, timeout):
                    self.script_timeout = timeout

            try:
                os.environ.pop("CHROME_PROFILE_DIR", None)
                os.environ["SELENIUM_PROFILE_ROOT"] = str(root)
                os.environ["WORKER_BROWSER_AUTO_CLOSE_SECONDS"] = "600"
                selenium_local_module._create_local_driver_with_retry = lambda options: FakeDriver()
                selenium_local_module.schedule_driver_auto_close = lambda driver, label="Chrome": scheduled.append((driver, label))

                driver = _create_driver(Path(tmp), profile_name="vehicle_mileage_profile_task1", headless=False)
            finally:
                selenium_local_module._create_local_driver_with_retry = original_create
                selenium_local_module.schedule_driver_auto_close = original_schedule
                if previous_profile is None:
                    os.environ.pop("CHROME_PROFILE_DIR", None)
                else:
                    os.environ["CHROME_PROFILE_DIR"] = previous_profile
                if previous_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_root
                if previous_delay is None:
                    os.environ.pop("WORKER_BROWSER_AUTO_CLOSE_SECONDS", None)
                else:
                    os.environ["WORKER_BROWSER_AUTO_CLOSE_SECONDS"] = previous_delay

            self.assertEqual(scheduled, [(driver, "vehicle_mileage_profile_task1")])

    def test_ppe_credentials_prefers_synced_worker_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_account = os.environ.get("DUTY_ACCOUNT")
            previous_password = os.environ.get("DUTY_PASSWORD")
            try:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                os.environ["DUTY_ACCOUNT"] = "env-user"
                os.environ["DUTY_PASSWORD"] = "env-pass"
                save_duty_automation_credentials(
                    [{"actor_no": "8", "user_id": "tyfd00008", "password": "synced-pass"}],
                    last_selected="tyfd00008",
                )

                self.assertEqual(_ppe_credentials(), ("tyfd00008", "synced-pass"))
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
                if previous_account is None:
                    os.environ.pop("DUTY_ACCOUNT", None)
                else:
                    os.environ["DUTY_ACCOUNT"] = previous_account
                if previous_password is None:
                    os.environ.pop("DUTY_PASSWORD", None)
                else:
                    os.environ["DUTY_PASSWORD"] = previous_password

    def test_ensure_ppe_vehicle_mileage_session_retries_when_login_returns_to_login_page(self):
        class FakeInput:
            def __init__(self):
                self.values: list[str] = []
                self.history: list[str] = []

            def clear(self):
                self.values.clear()

            def send_keys(self, value: str):
                self.values.append(value)
                self.history.append(value)

        class FakeDriver:
            def __init__(self):
                self.get_calls: list[str] = []
                self.inputs = {"Account": FakeInput(), "Password": FakeInput()}

            def get(self, url: str):
                self.get_calls.append(url)

            def find_element(self, by, value: str):
                return self.inputs[value]

        original_credentials = selenium_local_module._ppe_credential_attempts
        original_wait = selenium_local_module._wait_for_ppe_vehicle_mileage_page
        original_wait_login = selenium_local_module._wait_for_ppe_login_result
        original_is_login = selenium_local_module._is_ppe_login_page
        original_click = selenium_local_module._click_ppe_login
        original_sleep = selenium_local_module.time.sleep
        try:
            wait_results = iter([False, True])
            click_count = {"value": 0}
            selenium_local_module._ppe_credential_attempts = lambda request=None: [("tyfd00008", "pass")]
            selenium_local_module._wait_for_ppe_vehicle_mileage_page = lambda driver, timeout=12: next(wait_results)
            selenium_local_module._wait_for_ppe_login_result = lambda driver, timeout=12: True
            selenium_local_module._is_ppe_login_page = lambda driver: True
            selenium_local_module._click_ppe_login = lambda driver: click_count.__setitem__("value", click_count["value"] + 1)
            selenium_local_module.time.sleep = lambda seconds: None

            driver = FakeDriver()
            result = _ensure_ppe_vehicle_mileage_session(driver)
        finally:
            selenium_local_module._ppe_credential_attempts = original_credentials
            selenium_local_module._wait_for_ppe_vehicle_mileage_page = original_wait
            selenium_local_module._wait_for_ppe_login_result = original_wait_login
            selenium_local_module._is_ppe_login_page = original_is_login
            selenium_local_module._click_ppe_login = original_click
            selenium_local_module.time.sleep = original_sleep

        self.assertTrue(result)
        self.assertEqual(len(driver.get_calls), 2)
        self.assertEqual(click_count["value"], 1)
        self.assertEqual(driver.inputs["Account"].values, ["tyfd00008"])
        self.assertEqual(driver.inputs["Password"].values, ["pass"])

    def test_ppe_session_tries_driver_personnel_then_synced_account(self):
        class FakeInput:
            def __init__(self):
                self.values: list[str] = []
                self.history: list[str] = []

            def clear(self):
                self.values.clear()

            def send_keys(self, value: str):
                self.values.append(value)
                self.history.append(value)

        class FakeDriver:
            def __init__(self):
                self.get_calls: list[str] = []
                self.inputs = {"Account": FakeInput(), "Password": FakeInput()}

            def get(self, url: str):
                self.get_calls.append(url)

            def find_element(self, by, value: str):
                return self.inputs[value]

        previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        request = AmbulanceReturnRequest(
            task_id="task-ppe-fallback",
            created_at=datetime.now(),
            raw_text="",
            driver="司機甲",
            personnel=["司機甲", "出勤乙"],
            personnel_accounts=["tyfd01111", "tyfd03333"],
        )
        original_wait = selenium_local_module._wait_for_ppe_vehicle_mileage_page
        original_wait_login = selenium_local_module._wait_for_ppe_login_result
        original_is_login = selenium_local_module._is_ppe_login_page
        original_click = selenium_local_module._click_ppe_login
        original_sleep = selenium_local_module.time.sleep
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_duty_automation_credentials(
                    [
                        {"actor_no": "11", "name": "司機甲", "user_id": "tyfd01111", "password": "driver-pass"},
                        {"actor_no": "33", "name": "出勤乙", "user_id": "tyfd03333", "password": "personnel-pass"},
                        {"actor_no": "22", "name": "同步員", "user_id": "tyfd02222", "password": "synced-pass"},
                    ],
                    last_selected="tyfd02222",
                )
                page_checks = iter([False, False, False, False, False, True])
                login_results = iter([False, False, True])
                selenium_local_module._wait_for_ppe_vehicle_mileage_page = lambda driver, timeout=12: next(page_checks)
                selenium_local_module._wait_for_ppe_login_result = lambda driver, timeout=12: next(login_results)
                selenium_local_module._is_ppe_login_page = lambda driver: True
                selenium_local_module._click_ppe_login = lambda driver: None
                selenium_local_module.time.sleep = lambda seconds: None

                driver = FakeDriver()
                result = _ensure_ppe_vehicle_mileage_session(driver, request)
        finally:
            selenium_local_module._wait_for_ppe_vehicle_mileage_page = original_wait
            selenium_local_module._wait_for_ppe_login_result = original_wait_login
            selenium_local_module._is_ppe_login_page = original_is_login
            selenium_local_module._click_ppe_login = original_click
            selenium_local_module.time.sleep = original_sleep
            if previous_path is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
            if previous_override is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertTrue(result)
        self.assertEqual(driver.inputs["Account"].history, ["tyfd01111", "tyfd03333", "tyfd02222"])
        self.assertEqual(driver.inputs["Password"].history, ["driver-pass", "personnel-pass", "synced-pass"])
        self.assertEqual(
            driver.get_calls,
            [
                "https://ppe.tyfd.gov.tw/CarRecord/List",
                "https://ppe.tyfd.gov.tw/CarRecord/List",
                "https://ppe.tyfd.gov.tw/CarRecord/List",
                "https://ppe.tyfd.gov.tw/CarRecord/List",
            ],
        )

    def test_case_lookup_runs_headless_and_closes_driver_after_login_failure(self):
        class FakeDriver:
            def implicitly_wait(self, seconds: int) -> None:
                pass

        calls: dict[str, object] = {"released": False}
        original_create_driver = selenium_local_module._create_driver
        original_acquire = selenium_local_module._acquire_selenium_session
        original_release = selenium_local_module._release_selenium_session
        original_quit = selenium_local_module._quit_driver
        original_ensure_login = selenium_local_module._ensure_duty_login
        original_save_artifacts = selenium_local_module._save_artifacts
        original_login_error = selenium_local_module._login_error_text
        try:
            fake_driver = FakeDriver()

            def fake_create_driver(*args, **kwargs):
                calls["create_kwargs"] = kwargs
                return fake_driver

            selenium_local_module._create_driver = fake_create_driver
            selenium_local_module._acquire_selenium_session = lambda reason: True
            selenium_local_module._release_selenium_session = lambda reason: calls.__setitem__("released", True)
            selenium_local_module._quit_driver = lambda driver: calls.__setitem__("quit_driver", driver)
            selenium_local_module._ensure_duty_login = lambda driver, preferred_user_ids=None: False
            selenium_local_module._save_artifacts = lambda *args, **kwargs: None
            selenium_local_module._login_error_text = lambda driver: "login failed"

            with tempfile.TemporaryDirectory() as tmp:
                result = selenium_local_module.query_duty_emergency_cases(Path(tmp), lookup_range="24h")
        finally:
            selenium_local_module._create_driver = original_create_driver
            selenium_local_module._acquire_selenium_session = original_acquire
            selenium_local_module._release_selenium_session = original_release
            selenium_local_module._quit_driver = original_quit
            selenium_local_module._ensure_duty_login = original_ensure_login
            selenium_local_module._save_artifacts = original_save_artifacts
            selenium_local_module._login_error_text = original_login_error

        self.assertEqual(result.status, "duty_login_failed")
        self.assertIs(calls["quit_driver"], fake_driver)
        self.assertTrue(calls["released"])
        self.assertTrue(calls["create_kwargs"]["headless"])

    def test_headless_driver_uses_default_arg_when_env_is_blank(self):
        class FakeDriver:
            def set_page_load_timeout(self, seconds: int) -> None:
                pass

            def set_script_timeout(self, seconds: int) -> None:
                pass

        calls = []
        original_create = selenium_local_module._create_local_driver_with_retry
        original_headless_arg = os.environ.get("SELENIUM_HEADLESS_ARG")
        try:
            os.environ["SELENIUM_HEADLESS_ARG"] = " "
            selenium_local_module._create_local_driver_with_retry = lambda options: calls.append(options) or FakeDriver()

            with tempfile.TemporaryDirectory() as tmp:
                selenium_local_module._create_driver(Path(tmp), profile_name="case_lookup_profile_test", headless=True)
        finally:
            selenium_local_module._create_local_driver_with_retry = original_create
            if original_headless_arg is None:
                os.environ.pop("SELENIUM_HEADLESS_ARG", None)
            else:
                os.environ["SELENIUM_HEADLESS_ARG"] = original_headless_arg

        arguments = list(getattr(calls[0], "arguments", []))
        self.assertIn("--headless=new", arguments)
        self.assertNotIn("", arguments)

    def test_headless_driver_keeps_headless_when_env_arg_is_not_headless(self):
        class FakeDriver:
            def set_page_load_timeout(self, seconds: int) -> None:
                pass

            def set_script_timeout(self, seconds: int) -> None:
                pass

        calls = []
        original_create = selenium_local_module._create_local_driver_with_retry
        original_headless_arg = os.environ.get("SELENIUM_HEADLESS_ARG")
        try:
            os.environ["SELENIUM_HEADLESS_ARG"] = "--disable-gpu"
            selenium_local_module._create_local_driver_with_retry = lambda options: calls.append(options) or FakeDriver()

            with tempfile.TemporaryDirectory() as tmp:
                selenium_local_module._create_driver(Path(tmp), profile_name="case_lookup_profile_test", headless=True)
        finally:
            selenium_local_module._create_local_driver_with_retry = original_create
            if original_headless_arg is None:
                os.environ.pop("SELENIUM_HEADLESS_ARG", None)
            else:
                os.environ["SELENIUM_HEADLESS_ARG"] = original_headless_arg

        arguments = list(getattr(calls[0], "arguments", []))
        self.assertIn("--headless=new", arguments)
        self.assertIn("--disable-gpu", arguments)

    def test_local_driver_retries_when_chrome_is_not_reachable(self):
        class FakeDriver:
            pass

        calls: dict[str, object] = {"count": 0, "sleep": []}
        original_webdriver_chrome = selenium_local_module.webdriver.Chrome
        original_sleep = selenium_local_module.time.sleep
        original_cleanup = selenium_local_module.cleanup_worker_chrome_residue
        original_attempts = os.environ.get("SELENIUM_LOCAL_SESSION_ATTEMPTS")
        cleanups = []
        options = object()
        try:
            os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = "2"

            def fake_chrome(options=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise selenium_local_module.WebDriverException("session not created: from chrome not reachable")
                return FakeDriver()

            selenium_local_module.webdriver.Chrome = fake_chrome
            selenium_local_module.time.sleep = lambda seconds: calls["sleep"].append(seconds)
            selenium_local_module.cleanup_worker_chrome_residue = lambda opts, label="Chrome": cleanups.append((opts, label)) or 1

            result = _create_local_driver_with_retry(options)
        finally:
            selenium_local_module.webdriver.Chrome = original_webdriver_chrome
            selenium_local_module.time.sleep = original_sleep
            selenium_local_module.cleanup_worker_chrome_residue = original_cleanup
            if original_attempts is None:
                os.environ.pop("SELENIUM_LOCAL_SESSION_ATTEMPTS", None)
            else:
                os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = original_attempts

        self.assertIsInstance(result, FakeDriver)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(calls["sleep"], [2])
        self.assertEqual(cleanups, [(options, "local selenium")])

    def test_local_driver_cleans_failed_user_data_dir_before_retry(self):
        class FakeDriver:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            calls: dict[str, object] = {"count": 0, "sleep": []}
            profile_cleanups: list[tuple[Path, ...]] = []
            user_data_dir = Path(tmp) / "profiles" / "case_lookup_profile_test"
            options = selenium_local_module.Options()
            options.add_argument(f"--user-data-dir={user_data_dir}")
            original_webdriver_chrome = selenium_local_module.webdriver.Chrome
            original_sleep = selenium_local_module.time.sleep
            original_cleanup = selenium_local_module.cleanup_worker_chrome_residue
            original_profile_cleanup = selenium_local_module.cleanup_runtime_profiles_for_startup_failure
            original_attempts = os.environ.get("SELENIUM_LOCAL_SESSION_ATTEMPTS")
            try:
                os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = "2"

                def fake_chrome(options=None):
                    calls["count"] += 1
                    if calls["count"] == 1:
                        raise selenium_local_module.WebDriverException("session not created: from chrome not reachable")
                    return FakeDriver()

                selenium_local_module.webdriver.Chrome = fake_chrome
                selenium_local_module.time.sleep = lambda seconds: calls["sleep"].append(seconds)
                selenium_local_module.cleanup_worker_chrome_residue = lambda opts, label="Chrome": 0
                selenium_local_module.cleanup_runtime_profiles_for_startup_failure = (
                    lambda dirs: profile_cleanups.append(tuple(Path(path) for path in dirs)) or []
                )

                result = _create_local_driver_with_retry(options)
            finally:
                selenium_local_module.webdriver.Chrome = original_webdriver_chrome
                selenium_local_module.time.sleep = original_sleep
                selenium_local_module.cleanup_worker_chrome_residue = original_cleanup
                selenium_local_module.cleanup_runtime_profiles_for_startup_failure = original_profile_cleanup
                if original_attempts is None:
                    os.environ.pop("SELENIUM_LOCAL_SESSION_ATTEMPTS", None)
                else:
                    os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = original_attempts

        self.assertIsInstance(result, FakeDriver)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(profile_cleanups, [(user_data_dir,)])

    def test_local_driver_recovers_when_chrome_start_times_out(self):
        class FakeDriver:
            pass

        calls: dict[str, object] = {"count": 0, "sleep": []}
        original_webdriver_chrome = selenium_local_module.webdriver.Chrome
        original_sleep = selenium_local_module.time.sleep
        original_cleanup = selenium_local_module.cleanup_worker_chrome_residue
        original_attempts = os.environ.get("SELENIUM_LOCAL_SESSION_ATTEMPTS")
        original_timeout = os.environ.get("SELENIUM_CHROME_START_TIMEOUT_SECONDS")
        cleanups = []
        options = object()
        try:
            os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = "2"
            os.environ["SELENIUM_CHROME_START_TIMEOUT_SECONDS"] = "0.01"

            def fake_chrome(options=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    original_sleep(0.05)
                return FakeDriver()

            selenium_local_module.webdriver.Chrome = fake_chrome
            selenium_local_module.time.sleep = lambda seconds: calls["sleep"].append(seconds)
            selenium_local_module.cleanup_worker_chrome_residue = lambda opts, label="Chrome": cleanups.append((opts, label)) or 0

            result = _create_local_driver_with_retry(options)
        finally:
            selenium_local_module.webdriver.Chrome = original_webdriver_chrome
            selenium_local_module.time.sleep = original_sleep
            selenium_local_module.cleanup_worker_chrome_residue = original_cleanup
            if original_attempts is None:
                os.environ.pop("SELENIUM_LOCAL_SESSION_ATTEMPTS", None)
            else:
                os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = original_attempts
            if original_timeout is None:
                os.environ.pop("SELENIUM_CHROME_START_TIMEOUT_SECONDS", None)
            else:
                os.environ["SELENIUM_CHROME_START_TIMEOUT_SECONDS"] = original_timeout

        self.assertIsInstance(result, FakeDriver)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(cleanups, [(options, "local selenium")])

    def test_local_driver_retries_oserror_invalid_argument(self):
        class FakeDriver:
            pass

        calls: dict[str, object] = {"count": 0, "sleep": []}
        original_webdriver_chrome = selenium_local_module.webdriver.Chrome
        original_sleep = selenium_local_module.time.sleep
        original_cleanup = selenium_local_module.cleanup_worker_chrome_residue
        original_attempts = os.environ.get("SELENIUM_LOCAL_SESSION_ATTEMPTS")
        cleanups = []
        options = object()
        try:
            os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = "2"

            def fake_chrome(options=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError(22, "Invalid argument")
                return FakeDriver()

            selenium_local_module.webdriver.Chrome = fake_chrome
            selenium_local_module.time.sleep = lambda seconds: calls["sleep"].append(seconds)
            selenium_local_module.cleanup_worker_chrome_residue = lambda opts, label="Chrome": cleanups.append((opts, label)) or 1

            result = _create_local_driver_with_retry(options)
        finally:
            selenium_local_module.webdriver.Chrome = original_webdriver_chrome
            selenium_local_module.time.sleep = original_sleep
            selenium_local_module.cleanup_worker_chrome_residue = original_cleanup
            if original_attempts is None:
                os.environ.pop("SELENIUM_LOCAL_SESSION_ATTEMPTS", None)
            else:
                os.environ["SELENIUM_LOCAL_SESSION_ATTEMPTS"] = original_attempts

        self.assertIsInstance(result, FakeDriver)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(calls["sleep"], [2])
        self.assertEqual(cleanups, [(options, "local selenium")])

    def test_local_chrome_startup_error_treats_no_space_left_as_retryable(self):
        self.assertTrue(
            selenium_local_module._is_local_chrome_startup_error(
                OSError(28, "No space left on device")
            )
        )

    def test_case_lookup_closes_driver_after_success(self):
        class FakeDriver:
            def implicitly_wait(self, seconds: int) -> None:
                pass

        calls: dict[str, object] = {"released": False}
        original_create_driver = selenium_local_module._create_driver
        original_acquire = selenium_local_module._acquire_selenium_session
        original_release = selenium_local_module._release_selenium_session
        original_quit = selenium_local_module._quit_driver
        original_ensure_login = selenium_local_module._ensure_duty_login
        original_set_window = selenium_local_module._set_window_size_if_enabled
        original_open_case_query = selenium_local_module._open_case_query
        original_extract_cases = selenium_local_module._extract_all_emergency_cases
        original_attach_details = selenium_local_module._attach_case_form_details
        original_save_artifacts = selenium_local_module._save_artifacts
        try:
            fake_driver = FakeDriver()

            def fake_create_driver(*args, **kwargs):
                calls["create_kwargs"] = kwargs
                return fake_driver

            selenium_local_module._create_driver = fake_create_driver
            selenium_local_module._acquire_selenium_session = lambda reason: True
            selenium_local_module._release_selenium_session = lambda reason: calls.__setitem__("released", True)
            selenium_local_module._quit_driver = lambda driver: calls.__setitem__("quit_driver", driver)
            selenium_local_module._ensure_duty_login = lambda driver, preferred_user_ids=None: True
            selenium_local_module._set_window_size_if_enabled = lambda *args, **kwargs: None
            selenium_local_module._open_case_query = lambda *args, **kwargs: None
            selenium_local_module._extract_all_emergency_cases = lambda driver: [{"case_id": "case-1"}]
            selenium_local_module._attach_case_form_details = lambda driver, cases, artifacts_dir, previous_cases, deadline=None: cases
            selenium_local_module._save_artifacts = lambda *args, **kwargs: None

            with tempfile.TemporaryDirectory() as tmp:
                result = selenium_local_module.query_duty_emergency_cases(Path(tmp), lookup_range="24h")
        finally:
            selenium_local_module._create_driver = original_create_driver
            selenium_local_module._acquire_selenium_session = original_acquire
            selenium_local_module._release_selenium_session = original_release
            selenium_local_module._quit_driver = original_quit
            selenium_local_module._ensure_duty_login = original_ensure_login
            selenium_local_module._set_window_size_if_enabled = original_set_window
            selenium_local_module._open_case_query = original_open_case_query
            selenium_local_module._extract_all_emergency_cases = original_extract_cases
            selenium_local_module._attach_case_form_details = original_attach_details
            selenium_local_module._save_artifacts = original_save_artifacts

        self.assertEqual(result.status, "cases_loaded")
        self.assertEqual(result.detail, "已查到 1 筆 24 小時內案件，並讀取出勤人員。")
        self.assertIs(calls["quit_driver"], fake_driver)
        self.assertTrue(calls["released"])
        self.assertTrue(calls["create_kwargs"]["headless"])

    def test_click_save_control_uses_script_result(self):
        class FakeDriver:
            def __init__(self, result: bool):
                self.result = result
                self.script = ""

            def execute_script(self, script: str):
                self.script = script
                return self.result

        success_driver = FakeDriver(True)
        failed_driver = FakeDriver(False)

        self.assertTrue(_click_save_control(success_driver))
        self.assertFalse(_click_save_control(failed_driver))
        self.assertIn("btnsave", success_driver.script.lower())
        self.assertIn("submit", success_driver.script.lower())

    def test_site_specific_save_controls_use_real_button_signatures(self):
        class FakeDriver:
            def __init__(self, result: bool):
                self.result = result
                self.scripts: list[str] = []

            def execute_script(self, script: str):
                self.scripts.append(script)
                return self.result

        mileage_driver = FakeDriver(True)
        disinfection_driver = FakeDriver(True)

        self.assertTrue(_click_vehicle_mileage_save(mileage_driver))
        self.assertTrue(_click_disinfection_save(disinfection_driver))
        self.assertIn("SaveData", mileage_driver.scripts[0])
        self.assertIn("_btnSave", disinfection_driver.scripts[0])

    def test_fuel_record_save_never_uses_submit_action(self):
        source = Path(selenium_local_module.__file__).read_text(encoding="utf-8")

        self.assertIn("SaveData('save')", source)
        self.assertNotIn("SaveData('submit')", source)

    def test_fuel_grid_exact_match_uses_all_record_identity_fields(self):
        matcher = getattr(selenium_local_module, "_fuel_grid_matching_row_indices", None)
        self.assertIsNotNone(matcher, "fuel grid exact matcher is required for idempotent retries")

        class FakeDriver:
            def execute_script(self, _script):
                return [
                    {
                        "row_index": 0,
                        "date": "2026-07-13T09:15:00",
                        "time": "0915",
                        "driver": "甲",
                        "product": "超級柴油",
                        "quantity": "20.1",
                        "unit_price": "30.0",
                    },
                    {
                        "row_index": 1,
                        "date": "2026-07-13T09:15:00",
                        "time": "0915",
                        "driver": "甲",
                        "product": "超級柴油",
                        "quantity": "20.2",
                        "unit_price": "30.0",
                    },
                ]

        request = AmbulanceReturnRequest.from_dict(
            {
                "task_id": "fuel-match",
                "created_at": "2026-07-13T08:00:00",
                "vehicle": "新坡92",
                "fuel_record": {
                    "enabled": True,
                    "date": "20260713",
                    "time": "0915",
                    "driver": "甲",
                    "product": "超級柴油",
                    "quantity": "20.1",
                    "unit_price": "30.0",
                },
            }
        )

        self.assertEqual(matcher(FakeDriver(), request), [0])

    def test_fuel_prepare_treats_unique_current_record_as_already_saved(self):
        matcher = getattr(selenium_local_module, "_fuel_grid_matching_row_indices", None)
        self.assertIsNotNone(matcher, "fuel grid exact matcher is required for idempotent retries")
        request = AmbulanceReturnRequest.from_dict(
            {
                "task_id": "fuel-existing",
                "created_at": "2026-07-13T08:00:00",
                "vehicle": "新坡92",
                "fuel_record": {
                    "enabled": True,
                    "date": "20260713",
                    "time": "0915",
                    "driver": "甲",
                    "product": "超級柴油",
                    "quantity": "20.1",
                    "unit_price": "30.0",
                },
            }
        )
        with patch.object(selenium_local_module, "_wait_for_ppe_fuel_record_page", return_value=True), patch.object(
            selenium_local_module,
            "_ensure_fuel_query_period",
            return_value="2026/07",
        ), patch.object(selenium_local_module, "_click_fuel_card_register"), patch.object(
            selenium_local_module,
            "_wait_for_ppe_fuel_record_detail_page",
            return_value=True,
        ), patch.object(
            selenium_local_module,
            "_fuel_grid_matching_row_indices",
            return_value=[0],
        ), patch.object(selenium_local_module, "_click_fuel_add_row") as add_row, patch.object(
            selenium_local_module,
            "_fill_fuel_grid_record",
        ) as fill, patch.object(selenium_local_module, "_save_fuel_record_form") as save:
            detail = selenium_local_module._prepare_fuel_record_form(
                SimpleNamespace(get=lambda _url: None),
                request,
                Path("artifacts"),
            )

        self.assertIn("已存在", detail)
        add_row.assert_not_called()
        fill.assert_not_called()
        save.assert_not_called()

    def test_fuel_update_changes_only_unique_previous_row_without_adding(self):
        matcher = getattr(selenium_local_module, "_fuel_grid_matching_row_indices", None)
        self.assertIsNotNone(matcher, "fuel grid exact matcher is required for idempotent updates")
        previous_task = self._fuel_update_task(quantity="20.1")
        current_task = self._fuel_update_task(quantity="25.5")
        request = AmbulanceReturnRequest.from_dict(current_task)
        context = {
            "previous_task": previous_task,
            "current_task": current_task,
            "vehicle_index": 1,
            "vehicle_key": "新坡92",
        }

        def match_rows(_driver, matched_request):
            return [1] if matched_request.fuel_record.quantity == "20.1" else []

        with patch.object(selenium_local_module, "_wait_for_ppe_fuel_record_page", return_value=True), patch.object(
            selenium_local_module,
            "_ensure_fuel_query_period",
            return_value="2026/07",
        ), patch.object(selenium_local_module, "_click_fuel_card_register"), patch.object(
            selenium_local_module,
            "_wait_for_ppe_fuel_record_detail_page",
            return_value=True,
        ), patch.object(
            selenium_local_module,
            "_fuel_grid_matching_row_indices",
            side_effect=match_rows,
        ), patch.object(selenium_local_module, "_click_fuel_add_row") as add_row, patch.object(
            selenium_local_module,
            "_fill_fuel_grid_record",
        ) as fill, patch.object(selenium_local_module, "_assert_fuel_grid_record_present"), patch.object(
            selenium_local_module,
            "_save_fuel_record_enabled",
            return_value=True,
        ), patch.object(
            selenium_local_module,
            "_save_fuel_record_form",
            return_value="saved",
        ):
            detail = selenium_local_module._prepare_fuel_record_form(
                SimpleNamespace(get=lambda _url: None),
                request,
                Path("artifacts"),
                update_context=context,
            )

        add_row.assert_not_called()
        fill.assert_called_once()
        self.assertEqual(fill.call_args.kwargs["row_index"], 1)
        self.assertIn("已更新", detail)

    def test_fuel_update_fails_closed_when_previous_row_is_missing_or_ambiguous(self):
        matcher = getattr(selenium_local_module, "_fuel_grid_matching_row_indices", None)
        self.assertIsNotNone(matcher, "fuel grid exact matcher is required for safe updates")
        previous_task = self._fuel_update_task(quantity="20.1")
        current_task = self._fuel_update_task(quantity="25.5")
        request = AmbulanceReturnRequest.from_dict(current_task)
        context = {
            "previous_task": previous_task,
            "current_task": current_task,
            "vehicle_index": 1,
            "vehicle_key": "新坡92",
        }
        for previous_matches in ([], [0, 1]):
            with self.subTest(previous_matches=previous_matches), patch.object(
                selenium_local_module,
                "_wait_for_ppe_fuel_record_page",
                return_value=True,
            ), patch.object(selenium_local_module, "_ensure_fuel_query_period", return_value="2026/07"), patch.object(
                selenium_local_module,
                "_click_fuel_card_register",
            ), patch.object(
                selenium_local_module,
                "_wait_for_ppe_fuel_record_detail_page",
                return_value=True,
            ), patch.object(
                selenium_local_module,
                "_fuel_grid_matching_row_indices",
                side_effect=[[], previous_matches],
            ), patch.object(selenium_local_module, "_click_fuel_add_row") as add_row:
                with self.assertRaisesRegex(selenium_local_module.WebDriverException, "previous fuel row"):
                    selenium_local_module._prepare_fuel_record_form(
                        SimpleNamespace(get=lambda _url: None),
                        request,
                        Path("artifacts"),
                        update_context=context,
                    )
                add_row.assert_not_called()

    def test_fuel_update_fails_closed_before_opening_card_when_vehicle_or_month_changed(self):
        current_task = self._fuel_update_task(quantity="25.5")
        request = AmbulanceReturnRequest.from_dict(current_task)
        scenarios: list[tuple[str, dict[str, object]]] = []
        changed_vehicle = self._fuel_update_task(quantity="20.1")
        changed_vehicle["vehicle"] = "新坡93"
        scenarios.append(("vehicle change", changed_vehicle))
        changed_month = self._fuel_update_task(quantity="20.1")
        changed_month["fuel_record"] = {**dict(changed_month["fuel_record"]), "date": "20260630"}
        scenarios.append(("period change", changed_month))

        for expected_error, previous_task in scenarios:
            context = {
                "previous_task": previous_task,
                "current_task": current_task,
                "vehicle_index": 1,
                "vehicle_key": "新坡92",
            }
            with self.subTest(expected_error=expected_error), patch.object(
                selenium_local_module,
                "_wait_for_ppe_fuel_record_page",
                return_value=True,
            ), patch.object(selenium_local_module, "_ensure_fuel_query_period", return_value="2026/07"), patch.object(
                selenium_local_module,
                "_click_fuel_card_register",
            ) as click_card, patch.object(selenium_local_module, "_fuel_grid_matching_row_indices") as matcher:
                with self.assertRaisesRegex(selenium_local_module.WebDriverException, expected_error):
                    selenium_local_module._prepare_fuel_record_form(
                        SimpleNamespace(get=lambda _url: None),
                        request,
                        Path("artifacts"),
                        update_context=context,
                    )
                click_card.assert_not_called()
                matcher.assert_not_called()

    @staticmethod
    def _fuel_update_task(*, quantity: str) -> dict[str, object]:
        return {
            "task_id": "fuel-update",
            "created_at": "2026-07-13T08:00:00",
            "vehicle": "新坡92",
            "driver": "甲",
            "fuel_record": {
                "enabled": True,
                "date": "20260713",
                "time": "0915",
                "driver": "甲",
                "product": "超級柴油",
                "quantity": quantity,
                "unit_price": "30.0",
            },
        }

    def test_fuel_card_register_uses_plate_first_and_clicks_register_button(self):
        class FakeDriver:
            def __init__(self):
                self.labels = None

            def execute_script(self, script: str, labels: list[str]):
                self.labels = labels
                self.script = script
                return {"clicked": True, "rowMatched": True}

        driver = FakeDriver()

        self.assertEqual(_fuel_card_labels("新坡91"), ["BGV-2310", "新坡91"])
        _click_fuel_card_register(driver, _fuel_card_labels("新坡91"))

        self.assertEqual(driver.labels, ["BGV-2310", "新坡91"])
        self.assertIn("登錄", driver.script)
        self.assertIn("送出審核", driver.script)

    def test_fuel_query_period_switches_to_task_month(self):
        class FakeDriver:
            def __init__(self):
                self.period = "2026/06"
                self.scripts: list[str] = []
                self.clicked_query = False

            def execute_script(self, script: str, *args):
                self.scripts.append(script)
                if "FuelUseYM" in script and "clickQuery" in script:
                    self.period = args[0]
                    self.clicked_query = True
                    return {"changed": True, "clicked": True, "value": self.period}
                if "FuelUseYM" in script:
                    return self.period
                return None

        driver = FakeDriver()

        self.assertEqual(_ensure_fuel_query_period(driver, "2026/07"), "2026/07")
        self.assertTrue(driver.clicked_query)

    def test_fuel_query_period_keeps_matching_month(self):
        class FakeDriver:
            def __init__(self):
                self.period = "2026/07"
                self.scripts: list[str] = []

            def execute_script(self, script: str, *args):
                self.scripts.append(script)
                return self.period

        driver = FakeDriver()

        self.assertEqual(_ensure_fuel_query_period(driver, "2026/07"), "2026/07")
        self.assertEqual(_fuel_query_period(driver), "2026/07")

    def test_fuel_record_detail_page_rejects_query_page(self):
        class FakeDriver:
            def __init__(self, path: str, ready: bool = True):
                self.path = path
                self.ready = ready
                self.script = ""

            def execute_script(self, script: str):
                self.script = script
                return self.path == "/FUC04100/Detail" and self.ready

        query_driver = FakeDriver("/FUC04100/Query")
        detail_driver = FakeDriver("/FUC04100/Detail")
        loading_detail_driver = FakeDriver("/FUC04100/Detail", ready=False)

        self.assertFalse(selenium_local_module._is_ppe_fuel_record_detail_page(query_driver))
        self.assertTrue(selenium_local_module._is_ppe_fuel_record_detail_page(detail_driver))
        self.assertFalse(selenium_local_module._is_ppe_fuel_record_detail_page(loading_detail_driver))
        self.assertIn("/FUC04100/Detail", detail_driver.script)

    def test_fuel_register_waits_for_detail_page_before_filling_grid(self):
        source = Path(selenium_local_module.__file__).read_text(encoding="utf-8")

        self.assertIn("if not _wait_for_ppe_fuel_record_detail_page(driver, timeout=12):", source)

    def test_duty_work_log_login_uses_personnel_accounts(self):
        class FakeDriver:
            pass

        captured: dict[str, object] = {}
        original_ensure_login = selenium_local_module._ensure_duty_login
        original_save_artifacts = selenium_local_module._save_artifacts
        try:
            selenium_local_module._ensure_duty_login = (
                lambda driver, preferred_user_ids=None: captured.__setitem__("preferred", preferred_user_ids) or False
            )
            selenium_local_module._save_artifacts = lambda *args, **kwargs: None
            request = AmbulanceReturnRequest(
                task_id="task-1",
                created_at=datetime(2026, 6, 7, 1, 0),
                raw_text="",
                driver="Bob",
                personnel=["Alice", "Bob", "Carol"],
                personnel_accounts=["B123017532", "tyfd00008", "tyfd00009"],
            )

            result = _prepare_duty_work_log_form(FakeDriver(), request, Path("."), Path("task.txt"))
        finally:
            selenium_local_module._ensure_duty_login = original_ensure_login
            selenium_local_module._save_artifacts = original_save_artifacts

        self.assertEqual(result.status, "needs_duty_login")
        self.assertEqual(captured["preferred"], ["tyfd00008", "B123017532", "tyfd00009"])

    def test_duty_login_credential_attempts_use_driver_personnel_then_synced_account(self):
        previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_duty_automation_credentials(
                    [
                        {"actor_no": "1", "name": "Alice", "user_id": "tyfd00001", "password": "pass1"},
                        {"actor_no": "2", "name": "Bob", "user_id": "tyfd00002", "password": "pass2"},
                        {"actor_no": "3", "name": "Carol", "user_id": "tyfd00003", "password": "pass3"},
                        {"actor_no": "8", "name": "Sync", "user_id": "tyfd01510", "password": "pass8"},
                    ],
                    last_selected="tyfd01510",
                )
                request = AmbulanceReturnRequest(
                    task_id="task-1",
                    created_at=datetime(2026, 6, 7, 1, 0),
                    raw_text="",
                    driver="Bob",
                    personnel=["Alice", "Bob", "Carol"],
                    personnel_accounts=["tyfd00001", "tyfd00002", "tyfd00003"],
                )

                attempts = _duty_login_credential_attempts(request.duty_login_account_candidates)
        finally:
            if previous_path is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
            if previous_override is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertEqual([credential.user_id for credential in attempts], ["tyfd00002", "tyfd00001", "tyfd00003", "tyfd01510"])

    def test_case_lookup_login_uses_latest_synced_duty_account_before_fixed_sync_account(self):
        class FakeDriver:
            def implicitly_wait(self, seconds):
                self.wait_seconds = seconds

        previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        previous_account = os.environ.get("DUTY_ACCOUNT")
        previous_password = os.environ.get("DUTY_PASSWORD")
        captured: dict[str, object] = {}
        original_acquire = selenium_local_module._acquire_selenium_session
        original_release = selenium_local_module._release_selenium_session
        original_create_driver = selenium_local_module._create_driver
        original_timeouts = selenium_local_module._set_case_lookup_driver_timeouts
        original_window = selenium_local_module._set_window_size_if_enabled
        original_ensure_login = selenium_local_module._ensure_duty_login
        original_login_error = selenium_local_module._login_error_text
        original_save_artifacts = selenium_local_module._save_artifacts
        original_quit_driver = selenium_local_module._quit_driver
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_credential_sync_payload(
                    {
                        "actor_no": "12",
                        "user_id": "tyfd01212",
                        "accounts": [
                            {"actor_no": "19", "name": "司機甲", "user_id": "tyfd01919", "password": "pass19"},
                            {"actor_no": "12", "name": "出勤乙", "user_id": "tyfd01212", "password": "pass12"},
                            {"actor_no": "8", "name": "固定同步", "user_id": "tyfd01510", "password": "pass8"},
                        ],
                    }
                )
                selenium_local_module._acquire_selenium_session = lambda owner: True
                selenium_local_module._release_selenium_session = lambda owner: None
                selenium_local_module._create_driver = lambda *args, **kwargs: FakeDriver()
                selenium_local_module._set_case_lookup_driver_timeouts = lambda driver: None
                selenium_local_module._set_window_size_if_enabled = lambda driver, site_key: None
                selenium_local_module._login_error_text = lambda driver: ""
                selenium_local_module._save_artifacts = lambda *args, **kwargs: None
                selenium_local_module._quit_driver = lambda driver: None

                def fake_ensure_login(driver, preferred_user_ids=None):
                    captured["preferred"] = preferred_user_ids
                    return False

                selenium_local_module._ensure_duty_login = fake_ensure_login

                result = selenium_local_module.query_duty_emergency_cases(Path(tmp), lookup_range="24h")
        finally:
            selenium_local_module._acquire_selenium_session = original_acquire
            selenium_local_module._release_selenium_session = original_release
            selenium_local_module._create_driver = original_create_driver
            selenium_local_module._set_case_lookup_driver_timeouts = original_timeouts
            selenium_local_module._set_window_size_if_enabled = original_window
            selenium_local_module._ensure_duty_login = original_ensure_login
            selenium_local_module._login_error_text = original_login_error
            selenium_local_module._save_artifacts = original_save_artifacts
            selenium_local_module._quit_driver = original_quit_driver
            if previous_path is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
            if previous_override is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
            if previous_account is None:
                os.environ.pop("DUTY_ACCOUNT", None)
            else:
                os.environ["DUTY_ACCOUNT"] = previous_account
            if previous_password is None:
                os.environ.pop("DUTY_PASSWORD", None)
            else:
                os.environ["DUTY_PASSWORD"] = previous_password

        self.assertEqual(captured["preferred"], ["tyfd01212", "tyfd01510"])
        self.assertEqual(result.status, "needs_duty_login")

    def test_id_number_from_cases_matches_synced_actor_and_name(self):
        credential = DutyCredential(
            user_id="tyfd01510",
            password="pass8",
            actor_no="8",
            name="曾彥綸",
        )
        cases = [
            {
                "personnel_hidden_raw": "21番 張家和 S124774209\n8番 曾彥綸 B123017532",
                "personnel_raw": "張家和、曾彥綸",
            }
        ]

        self.assertEqual(_id_number_from_cases_for_credential(cases, credential), "B123017532")

    def test_lookup_synced_credential_id_number_updates_saved_account_from_case_lookup(self):
        previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        original_query = selenium_local_module.query_duty_emergency_cases
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
                save_duty_automation_credentials(
                    [
                        {
                            "actor_no": "8",
                            "name": "曾彥綸",
                            "user_id": "tyfd01510",
                            "password": "pass8",
                        }
                    ],
                    last_selected="8",
                )
                selenium_local_module.query_duty_emergency_cases = lambda artifacts_dir, lookup_range="24h": SimpleNamespace(
                    ok=True,
                    status="cases_loaded",
                    detail="loaded",
                    cases=[
                        {
                            "personnel_hidden_raw": "8番 曾彥綸 B123017532",
                            "personnel_raw": "曾彥綸",
                        }
                    ],
                    path=Path(tmp) / "cases" / "latest.json",
                )

                result = lookup_synced_credential_id_number(Path(tmp))
                selected = load_synced_worker_credential()
        finally:
            selenium_local_module.query_duty_emergency_cases = original_query
            if previous_path is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
            if previous_override is None:
                os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
            else:
                os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

        self.assertTrue(result.ok)
        self.assertEqual(result.id_number, "B123017532")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.id_number, "B123017532")

    def test_duty_login_tries_next_candidate_after_failure(self):
        class FakeDriver:
            def get(self, url):
                self.url = url

        attempted: list[str] = []
        original_attempts = selenium_local_module._duty_login_credential_attempts
        original_attempt_login = selenium_local_module._attempt_duty_login
        original_looks_logged_in = selenium_local_module._looks_logged_in
        original_sleep = selenium_local_module.time.sleep
        try:
            selenium_local_module._looks_logged_in = lambda driver: False
            selenium_local_module.time.sleep = lambda seconds: None
            selenium_local_module._duty_login_credential_attempts = lambda preferred_user_ids=None: [
                DutyCredential("driver", "driver-pass"),
                DutyCredential("other", "other-pass"),
                DutyCredential("sync", "sync-pass"),
            ]
            selenium_local_module._attempt_duty_login = (
                lambda driver, credential: attempted.append(credential.user_id) or credential.user_id == "sync"
            )

            result = _ensure_duty_login(FakeDriver(), ["driver", "other"])
        finally:
            selenium_local_module._duty_login_credential_attempts = original_attempts
            selenium_local_module._attempt_duty_login = original_attempt_login
            selenium_local_module._looks_logged_in = original_looks_logged_in
            selenium_local_module.time.sleep = original_sleep

        self.assertTrue(result)
        self.assertEqual(attempted, ["driver", "other", "sync"])

    def test_extract_emergency_cases_includes_fire_cases(self):
        class FakeDriver:
            def execute_script(self, script):
                self.script = script
                return [{"case_id": "20260610170000001", "category": "火災", "reason": "火災"}]

        driver = FakeDriver()
        cases = selenium_local_module._extract_emergency_cases(driver)

        self.assertEqual(cases[0]["category"], "火災")
        self.assertEqual(cases[0]["reason"], "火災")
        self.assertIn("includes('火災')", driver.script)

    def test_extract_emergency_cases_includes_salvaged_body_as_drowning(self):
        class FakeDriver:
            def execute_script(self, script):
                self.script = script
                return [{"case_id": "20260710170000001", "category": "其他-打撈浮屍", "reason": "溺水"}]

        driver = FakeDriver()
        cases = selenium_local_module._extract_emergency_cases(driver)

        self.assertEqual(cases[0]["category"], "其他-打撈浮屍")
        self.assertEqual(cases[0]["reason"], "溺水")
        self.assertIn("includes('其他-打撈浮屍')", driver.script)
        self.assertIn("isSalvagedBody ? '溺水'", driver.script)

    def test_attach_case_form_details_reuses_cached_personnel(self):
        cases = [{"case_id": "20260603080000001", "address": "新坡分隊"}]
        previous = {
            "20260603080000001": {
                "case_id": "20260603080000001",
                "personnel": ["曾彥綸"],
                "personnel_raw": "曾彥綸",
                "case_date": "1150603",
            }
        }

        result = _attach_case_form_details(None, cases, artifacts_dir=None, previous_cases=previous)

        self.assertEqual(result[0]["personnel"], ["曾彥綸"])
        self.assertEqual(result[0]["case_date"], "1150603")
        self.assertEqual(result[0]["detail_status"], "case_detail_cached")

    def test_write_json_atomic_and_previous_case_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            _write_json_atomic(
                path=path,
                payload={"cases": [{"case_id": "20260603080000001", "personnel": ["曾彥綸"]}]},
            )

            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["cases"][0]["case_id"], "20260603080000001")
            details = _previous_case_details(path)
            self.assertEqual(details["20260603080000001"]["personnel"], ["曾彥綸"])


if __name__ == "__main__":
    unittest.main()
