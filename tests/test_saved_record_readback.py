import unittest
import shutil
import json
import subprocess
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from ambulance_bot import selenium_local as runtime
from ambulance_bot.models import AmbulanceReturnRequest, VehicleEntry
from tests.test_vehicle_mileage_backfill import ScriptDriver


class FormScriptDriver:
    def __init__(self, elements):
        self.elements = elements
        self.get = Mock()
        self.switch_to = Mock()

    def execute_script(self, script, *args):
        harness = """
        const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
        global.document = {getElementById: id => input.elements[id] || null};
        process.stdout.write(JSON.stringify(new Function(input.script)(...input.args)));
        """
        result = subprocess.run([shutil.which("node"), "-e", harness],
                                input=json.dumps(dict(script=script, args=args, elements=self.elements)),
                                text=True, capture_output=True, encoding="utf-8", check=True)
        return json.loads(result.stdout)


class SavedRecordReadbackTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required for browser-script tests")
    def test_duty_query_waits_for_complete_document_before_reading_rows(self):
        driver = Mock()
        driver.find_element.return_value.is_enabled.side_effect = runtime.StaleElementReferenceException("reloaded")
        document_state = "loading"

        def execute(script, *args):
            nonlocal document_state
            harness = """
            const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
            global.document = {
              readyState: input.state,
              body: {innerText: 'QUY-000:查詢完成 共 ' + (input.state === 'complete' ? '0' : '1') + ' 筆'},
              getElementById: id => ({value: '', options: [1]}),
              querySelectorAll: () => []
            };
            process.stdout.write(JSON.stringify(new Function(input.script)(...input.args)));
            """
            completed = subprocess.run([shutil.which("node"), "-e", harness],
                                       input=json.dumps(dict(script=script, args=args, state=document_state)),
                                       text=True, capture_output=True, encoding="utf-8", check=True)
            result = json.loads(completed.stdout)
            if result is False:
                document_state = "complete"
            return result

        driver.execute_script.side_effect = execute
        with patch.object(runtime, "_click_by_text_or_id"):
            self.assertEqual([], runtime._query_duty_work_logs(driver, self.request()))
        self.assertEqual("complete", document_state)

    def test_duty_prequery_timeout_requeries_and_confirms_existing_without_insert(self):
        driver = Mock()
        with patch.object(runtime, "_ensure_duty_login", return_value=True), \
             patch.object(runtime, "_query_duty_work_logs", side_effect=[runtime.TimeoutException(),
                          [self.stored_duty_record()]]) as query, \
             patch.object(runtime, "_verify_saved_duty_work_log") as verify, \
             patch.object(runtime, "_click_by_text_or_id") as click:
            result = runtime._prepare_duty_work_log_form(driver, self.request(), Path("."), Path("summary.txt"))
        self.assertEqual("duty_work_log_saved", result.status)
        self.assertEqual(2, query.call_count)
        verify.assert_called_once()
        click.assert_not_called()

    def test_duty_prequery_repeated_timeout_stops_before_insert(self):
        with patch.object(runtime, "_ensure_duty_login", return_value=True), \
             patch.object(runtime, "_query_duty_work_logs", side_effect=runtime.TimeoutException()) as query, \
             patch.object(runtime, "_click_by_text_or_id") as click:
            result = runtime._prepare_duty_work_log_form(Mock(), self.request(), Path("."), Path("summary.txt"))
        self.assertEqual("duty_work_log_waiting_confirmation", result.status)
        self.assertEqual(2, query.call_count)
        self.assertIn("逾時", result.detail)
        click.assert_not_called()

    def test_duty_prequery_unsafe_data_is_not_retried(self):
        with patch.object(runtime, "_ensure_duty_login", return_value=True), \
             patch.object(runtime, "_query_duty_work_logs", side_effect=RuntimeError("無法確認車輛")) as query, \
             patch.object(runtime, "_click_by_text_or_id") as click:
            result = runtime._prepare_duty_work_log_form(Mock(), self.request(), Path("."), Path("summary.txt"))
        self.assertEqual("duty_work_log_waiting_confirmation", result.status)
        query.assert_called_once()
        click.assert_not_called()

    def test_duty_prequery_retry_propagates_cancellation(self):
        with patch.object(runtime, "_ensure_duty_login", return_value=True), \
             patch.object(runtime, "_query_duty_work_logs", side_effect=[runtime.TimeoutException(),
                          runtime.TaskCancellationError("cancelled")]):
            with self.assertRaises(runtime.TaskCancellationError):
                runtime._prepare_duty_work_log_form(Mock(), self.request(), Path("."), Path("summary.txt"))

    def test_duty_prequery_cancellation_is_not_swallowed_as_waiting(self):
        with patch.object(runtime, "_ensure_duty_login", return_value=True), \
             patch.object(runtime, "_query_duty_work_logs", side_effect=runtime.TaskCancellationError("cancelled")):
            with self.assertRaises(runtime.TaskCancellationError):
                runtime._prepare_duty_work_log_form(Mock(), self.request(), Path("."), Path("summary.txt"))

    @unittest.skipUnless(shutil.which("node"), "Node.js required for browser-script tests")
    def test_duty_snapshot_reads_form_and_normalizes_blank_reason(self):
        elements = {key: {"value": value} for key, value in {
            "_txtDATE": "1150930", "_selTIMEH": "22", "_selTIMEM": "58",
            "_areDescription": "119案件\r\n救護", "_areStatus": "新坡92:甲", "_areMan": "甲,乙"}.items()}
        elements["_selList"] = {"value": "34", "selectedIndex": 0, "options": [{"text": "救護"}]}
        elements["_selList2"] = {"value": "", "selectedIndex": 0, "options": [{"text": "請選擇"}]}
        driver = FormScriptDriver(elements)
        snapshot = runtime._duty_work_log_snapshot(driver)
        self.assertEqual(("1150930 22:58", "", "119案件\n救護"),
                         (snapshot["work_at"], snapshot["reason"], snapshot["description"]))
        del elements["_txtDATE"]
        with self.assertRaises(runtime.WebDriverException):
            runtime._duty_work_log_snapshot(driver)

    def stored_duty_record(self):
        request = self.request()
        return {"record_id": "123", "case_id": request.case_id, "work_at": "1150930 22:58",
                "department": "第三大隊", "unit": "新坡分隊", "item": "救護", "reason": "急病",
                "description": "119案件\n救護\n返隊時間:2026/10/01 01:01:00\n地點:測試",
                "status": request.duty_status_text, "personnel": ""}

    def query_duty(self, records, pages=1, total=None, request=None):
        actions = []
        keys = ["record_id", "case_id", "work_at", "department", "unit", "item", "reason", "description", "status", "personnel"]
        for record in records:
            data = "(^w^)".join(record[key].replace("\n", "<BR>") for key in keys) + "(^w^)"
            actions.append(f"Submit_SetSelectedRowData(frm,'wap119.RPS04060U','{data}');")
        driver = Mock()
        driver.find_element.return_value.is_enabled.side_effect = runtime.StaleElementReferenceException("query reloaded")
        driver.execute_script.side_effect = [True, True, {"pages": pages, "rows": actions,
                                                       "total": len(actions) if total is None else total}]
        with patch.object(runtime, "_click_by_text_or_id"):
            return runtime._query_duty_work_logs(driver, request or self.request())

    def test_duty_query_separates_work_items_in_both_orders(self):
        for target_item, stored_item in [("火警", "救護"), ("救護", "火警")]:
            request = self.request()
            request.duty_item = target_item
            for status in ["新坡92:甲", "新坡95:乙", "車輛資料缺漏"]:
                record = dict(self.stored_duty_record(), item=stored_item, status=status)
                with self.subTest(target=target_item, stored=stored_item, status=status):
                    self.assertEqual([], self.query_duty([record], request=request))

    def test_duty_query_accepts_other_vehicle_without_numbered_prefix(self):
        for status in ["新坡95:乙", "新坡95司機：乙", "1.新坡95司機:乙、新坡11司機:丙\n2.處理完成",
                       "1.觀音11司機:乙\n2.處理完成"]:
            with self.subTest(status=status):
                self.assertEqual([], self.query_duty([dict(self.stored_duty_record(), status=status)]))

    def test_duty_query_considers_all_requested_vehicles(self):
        request = self.request()
        request.two_vehicle = True
        request.vehicle_entries = [VehicleEntry(vehicle="新坡92", driver="甲"),
                                   VehicleEntry(vehicle="新坡95", driver="乙")]
        record = dict(self.stored_duty_record(), status="新坡95:乙")
        self.assertEqual([record], self.query_duty([record], request=request))
        with patch.object(runtime, "_query_duty_work_logs", return_value=[record]):
            with self.assertRaises(RuntimeError):
                runtime._verify_saved_duty_work_log(Mock(), request)

    def test_duty_query_rejects_unknown_work_item_or_vehicle_assignments(self):
        for item, status in [("", "新坡95:乙"), ("救護", "1.指揮官:甲"),
                             ("救護", "新坡95:乙、車輛不明:丙"), ("救護", "新坡95:"),
                             ("救護", "新坡95: "), ("救護", "1.新坡95:乙\n2.新坡92:甲")]:
            with self.subTest(item=item, status=status), self.assertRaises(RuntimeError):
                self.query_duty([dict(self.stored_duty_record(), item=item, status=status)])

    def test_duty_same_work_duplicate_records_still_stop_saved_verification(self):
        record = self.stored_duty_record()
        rows = self.query_duty([record, dict(record, record_id="456")])
        self.assertEqual(2, len(rows))
        with patch.object(runtime, "_query_duty_work_logs", return_value=rows):
            with self.assertRaises(RuntimeError):
                runtime._verify_saved_duty_work_log(Mock(), self.request())

    def test_duty_query_parses_real_row_protocol_and_uses_accepted_case_id(self):
        stored = self.stored_duty_record()
        self.assertEqual([stored], self.query_duty([stored]))

    def test_duty_query_does_not_confuse_same_time_different_case(self):
        other = self.stored_duty_record()
        other["case_id"] = "20260930225845016"
        self.assertEqual([], self.query_duty([other]))

    def test_duty_query_does_not_treat_missing_row_controls_as_an_empty_result(self):
        with self.assertRaises(RuntimeError):
            self.query_duty([], total=1)

    def test_duty_query_rejects_remaining_pages_and_unknown_vehicle(self):
        for pages, status in [(2, "新坡92:甲"), (1, "車輛資料缺漏")]:
            stored = self.stored_duty_record()
            stored["status"] = status
            with self.subTest(pages=pages, status=status), self.assertRaises(RuntimeError):
                self.query_duty([stored], pages)

    def test_duty_verify_rejects_duplicates_and_saved_content_changes(self):
        stored = self.stored_duty_record()
        for rows in [[stored, dict(stored, record_id="456")], [dict(stored, status="新坡92:乙")]]:
            with self.subTest(rows=rows), patch.object(runtime, "_query_duty_work_logs", return_value=rows):
                with self.assertRaises(RuntimeError):
                    runtime._verify_saved_duty_work_log(Mock(), self.request(), {"status": self.request().duty_status_text})

    def test_duty_verify_retry_checks_request_and_preserved_summary(self):
        stored = self.stored_duty_record()
        with patch.object(runtime, "_query_duty_work_logs", return_value=[stored]):
            runtime._verify_saved_duty_work_log(Mock(), self.request())
            stored["description"] = "返隊時間:無效"
            with self.assertRaises(RuntimeError):
                runtime._verify_saved_duty_work_log(Mock(), self.request())

    def test_readback_cancellation_propagates_before_navigation(self):
        for verify, args in [(runtime._query_duty_work_logs, ()), (runtime._verify_saved_fuel_record, ()),
                             (runtime._verify_saved_disinfection_record, ())]:
            driver = Mock()
            cancelled = runtime.TaskCancellationError("cancelled")
            with self.subTest(verify=verify.__name__), self.assertRaises(runtime.TaskCancellationError):
                runtime._verified_save_detail("test", "", lambda: verify(
                    driver, self.request(), *args, cancel_check=Mock(side_effect=cancelled),
                ))
            driver.get.assert_not_called()

    def duty_entry(self, rows, readback_error=None):
        driver = Mock()
        with ExitStack() as stack:
            for name, value in {"_ensure_duty_login": True, "_query_duty_work_logs": rows,
                                "_duty_work_log_snapshot": {"case_id": self.request().case_id},
                                "_extract_all_emergency_cases": [{"case_id": self.request().case_id}],
                                "_click_case_choose": True, "_fill_duty_work_log_values": [],
                                "_save_duty_work_log_enabled": True,
                                "_click_duty_work_log_save": {"ok": True, "alert": "儲存成功"}}.items():
                stack.enter_context(patch.object(runtime, name, return_value=value, create=True))
            for name in ["_click_by_text_or_id", "_switch_to_window_containing", "_set_case_query_date_range",
                         "_click_query_if_present", "_switch_to_work_log_form_for_case", "_save_artifacts"]:
                stack.enter_context(patch.object(runtime, name))
            stack.enter_context(patch.object(runtime.time, "sleep"))
            verify = stack.enter_context(patch.object(runtime, "_verify_saved_duty_work_log",
                                                     side_effect=readback_error, create=True))
            save = runtime._click_duty_work_log_save
            result = runtime._prepare_duty_work_log_form(driver, self.request(), Path("."), Path("summary.txt"))
            return result, save.call_count, verify.call_count

    def test_duty_success_message_without_saved_row_remains_waiting(self):
        result, saves, reads = self.duty_entry([], RuntimeError("回查沒有紀錄"))
        self.assertEqual("duty_work_log_waiting_confirmation", result.status)
        self.assertEqual((1, 1), (saves, reads))

    def test_duty_reload_exact_record_is_complete(self):
        result, saves, reads = self.duty_entry([])
        self.assertEqual("duty_work_log_saved", result.status)
        self.assertEqual((1, 1), (saves, reads))

    def test_duty_retry_existing_candidate_never_adds_another_record(self):
        result, saves, reads = self.duty_entry([{"record_id": "1"}], RuntimeError("內容尚未吻合"))
        self.assertEqual("duty_work_log_waiting_confirmation", result.status)
        self.assertEqual((0, 1), (saves, reads))

    def test_duty_retry_existing_verified_record_is_complete_without_save(self):
        result, saves, reads = self.duty_entry([{"record_id": "1"}])
        self.assertEqual("duty_work_log_saved", result.status)
        self.assertEqual((0, 1), (saves, reads))

    def request(self):
        return AmbulanceReturnRequest.from_dict({
            "task_id": "readback", "created_at": datetime.now().isoformat(),
            "case_id": "20260930225845015", "case_date": "2026/09/30", "case_time": "2356",
            "return_date": "2026/10/01", "return_time": "0101", "vehicle": "新坡92", "driver": "甲",
            "fuel_record": {"enabled": True, "date": "20260930", "time": "2359", "driver": "甲",
                            "product": "超級柴油", "quantity": "20.1", "unit_price": "30.0"},
        })

    def fuel_entry(self, saved_rows, *, stored_amount_ok=True):
        driver = Mock()
        driver.execute_script.return_value = stored_amount_ok
        with ExitStack() as stack:
            for name, value in {"_wait_for_ppe_fuel_record_page": True, "_ensure_fuel_query_period": "2026/09",
                                "_fuel_card_labels": ["synthetic-plate"], "_click_fuel_card_register": None,
                                "_wait_for_ppe_fuel_record_detail_page": True, "_click_fuel_add_row": None,
                                "_fill_fuel_grid_record": None, "_assert_fuel_grid_record_present": None,
                                "_save_fuel_record_enabled": True, "_save_fuel_record_form": "存檔完成"}.items():
                stack.enter_context(patch.object(runtime, name, return_value=value))
            matcher = stack.enter_context(patch.object(runtime, "_fuel_grid_matching_row_indices", side_effect=[[], saved_rows]))
            detail = runtime._prepare_fuel_record_form(driver, self.request())
        return detail, driver, matcher.call_count

    def test_fuel_success_message_without_saved_row_remains_waiting(self):
        detail, driver, reads = self.fuel_entry([])
        self.assertIn(runtime.WAITING_CONFIRMATION_MARKER, detail)
        self.assertEqual(2, reads)
        self.assertEqual(2, driver.get.call_count)

    def test_fuel_requires_unique_persisted_row_and_correct_amount(self):
        for rows, amount in [([0, 1], True), ([0], False)]:
            with self.subTest(rows=rows, amount=amount):
                detail, _, _ = self.fuel_entry(rows, stored_amount_ok=amount)
                self.assertIn(runtime.WAITING_CONFIRMATION_MARKER, detail)

    def test_fuel_reloaded_exact_record_is_complete(self):
        detail, driver, _ = self.fuel_entry([0])
        self.assertNotIn(runtime.WAITING_CONFIRMATION_MARKER, detail)
        self.assertIn("重新查詢", detail)
        self.assertEqual(2, driver.get.call_count)

    @unittest.skipUnless(shutil.which("node"), "Node.js required for browser-script tests")
    def test_fuel_readback_executes_matcher_and_persisted_amount_checks(self):
        row = {"FCUseID": 1, "FuelDate": "20260930", "FuelTime": "2359", "DriverName": "甲",
               "FuelTypeName": "超級柴油", "FuelQty": 20.1, "FuelPrice": 30, "FuelAmount": 603}
        for override, complete in [({}, True), ({"FCUseID": 0}, False), ({"FuelAmount": 602}, False),
                                   ({"FuelTime": "2358"}, False), ({"DriverName": "乙"}, False)]:
            driver = ScriptDriver([dict(row, **override)])
            driver.get = Mock()
            with self.subTest(override=override), patch.object(runtime, "_wait_for_ppe_fuel_record_page", return_value=True), \
                 patch.object(runtime, "_ensure_fuel_query_period", return_value="2026/09"), \
                 patch.object(runtime, "_fuel_card_labels", return_value=["synthetic"]), \
                 patch.object(runtime, "_click_fuel_card_register"), \
                 patch.object(runtime, "_wait_for_ppe_fuel_record_detail_page", return_value=True):
                detail = runtime._verified_save_detail("加油", "", lambda: runtime._verify_saved_fuel_record(driver, self.request()))
            self.assertEqual(complete, runtime.WAITING_CONFIRMATION_MARKER not in detail)
            driver.get.assert_called_once()

    def disinfection_entry(self, stored_items):
        driver = Mock()
        driver.execute_script.return_value = stored_items
        with ExitStack() as stack:
            for name in ["_switch_to_disinfection_content_if_present", "_wait_for_disinfection_query_fields",
                         "_save_disinfection_progress_artifacts", "_assert_disinfection_not_login",
                         "_set_disinfection_query_date", "_wait_for_disinfection_query_completed",
                         "_wait_for_disinfection_detail_ready"]:
                stack.enter_context(patch.object(runtime, name))
            for name, value in {"_click_disinfection_query": True, "_open_disinfection_detail_for_case": True,
                                "_set_disinfection_item_statuses": 8, "_save_disinfection_record_enabled": True,
                                "_click_disinfection_save": True, "_accept_alert_if_present": "存檔完成",
                                "_confirm_sweetalert_if_present": ""}.items():
                stack.enter_context(patch.object(runtime, name, return_value=value))
            detail = runtime._prepare_disinfection_record(driver, self.request(), Path("."))
        return detail, driver

    def test_disinfection_success_message_without_persisted_items_remains_waiting(self):
        detail, driver = self.disinfection_entry(False)
        self.assertIn(runtime.WAITING_CONFIRMATION_MARKER, detail)
        self.assertEqual(2, driver.get.call_count)

    def test_disinfection_reopens_accepted_time_vehicle_record_and_checks_items(self):
        detail, driver = self.disinfection_entry(True)
        self.assertNotIn(runtime.WAITING_CONFIRMATION_MARKER, detail)
        self.assertIn("重新查詢", detail)
        self.assertEqual(2, driver.get.call_count)

    @unittest.skipUnless(shutil.which("node"), "Node.js required for browser-script tests")
    def test_disinfection_readback_executes_saved_item_checks(self):
        request = self.request()
        ids = runtime._disinfection_item_ids(runtime._effective_disinfection_items(request.disinfection_items))
        for wrong_value in [None, "0", "missing"]:
            elements = {f"_selIVBALL_{identity}": {"value": "1"} for identity in ids}
            if wrong_value == "missing":
                del elements[f"_selIVBALL_{ids[0]}"]
            elif wrong_value is not None:
                elements[f"_selIVBALL_{ids[0]}"]["value"] = wrong_value
            driver = FormScriptDriver(elements)
            with self.subTest(wrong_value=wrong_value), ExitStack() as stack:
                for name in ["_switch_to_disinfection_content_if_present", "_wait_for_disinfection_query_fields",
                             "_assert_disinfection_not_login", "_set_disinfection_query_date",
                             "_wait_for_disinfection_query_completed", "_wait_for_disinfection_detail_ready"]:
                    stack.enter_context(patch.object(runtime, name))
                stack.enter_context(patch.object(runtime, "_click_disinfection_query", return_value=True))
                stack.enter_context(patch.object(runtime, "_open_disinfection_detail_for_case", return_value=True))
                detail = runtime._verified_save_detail("消毒", "", lambda: runtime._verify_saved_disinfection_record(driver, request))
            self.assertEqual(wrong_value is None, runtime.WAITING_CONFIRMATION_MARKER not in detail)


if __name__ == "__main__":
    unittest.main()
