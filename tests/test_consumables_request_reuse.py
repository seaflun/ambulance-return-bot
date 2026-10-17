import copy
import json
import shutil
import subprocess
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import consumables_login as module
from ambulance_bot.models import AmbulanceReturnRequest


class InitialListHandoffTests(unittest.TestCase):
    def setUp(self):
        self.driver = SimpleNamespace(session_id="session-a", current_url=module.ACS_URL, current_window_handle="window-a")
        self.context = {"url": module.ACS_URL, "query": "same-query", "generation": 1, "document": "document-a"}

    def remember(self):
        with patch.object(module, "_read_complete_consumable_candidates", return_value=[]), patch.object(
            module, "_consumable_list_context", return_value=self.context
        ):
            module._remember_initial_consumable_list(self.driver, "test-account", {"task_id": "task-a"})

    def consume(self, context=None, complete=None):
        with patch.object(module, "_load_acs_credentials", return_value=("test-account", "unused")), patch.object(
            module, "_consumable_list_context", return_value=self.context if context is None else context
        ), patch.object(module, "_read_complete_consumable_candidates", return_value=[] if complete is None else complete):
            return module._consume_initial_consumable_list(self.driver, {"task_id": "task-a"})

    def test_login_query_handoff_is_consumed_once(self):
        self.remember()
        self.assertTrue(self.consume())
        self.assertFalse(self.consume())

    def test_document_query_or_generation_change_never_reuses_query(self):
        for key, value in (("url", module.SSO_URL), ("query", "changed"), ("generation", 2), ("document", "other")):
            with self.subTest(key=key):
                self.remember()
                changed = dict(self.context, **{key: value})
                self.assertFalse(self.consume(changed))
                self.assertFalse(self.consume())

    def test_session_change_never_reuses_initial_query(self):
        self.remember()
        self.driver.session_id = "session-b"
        self.assertFalse(self.consume())

    def test_account_change_stops_before_using_another_accounts_query(self):
        self.remember()
        with patch.object(module, "_load_acs_credentials", return_value=("other-account", "unused")):
            with self.assertRaisesRegex(RuntimeError, "帳號"):
                module._consume_initial_consumable_list(self.driver, {"task_id": "task-a"})
        self.assertFalse(self.consume())

    def test_contract_failure_is_not_hidden_by_a_handoff(self):
        self.remember()
        with patch.object(module, "_load_acs_credentials", return_value=("test-account", "unused")), patch.object(
            module, "_consumable_list_context", return_value=self.context
        ), patch.object(module, "_read_complete_consumable_candidates", side_effect=RuntimeError("官方查詢失敗")):
            with self.assertRaisesRegex(RuntimeError, "官方查詢失敗"):
                module._consume_initial_consumable_list(self.driver, {"task_id": "task-a"})
        self.assertFalse(self.consume())

    def test_other_task_cannot_use_unconsumed_login_snapshot(self):
        self.remember()
        self.assertFalse(module._consume_initial_consumable_list(self.driver, {"task_id": "task-b"}))
        self.assertFalse(self.consume())

    def test_window_change_or_unknown_contract_never_reuses_initial_query(self):
        self.remember()
        self.driver.current_window_handle = "other-window"
        self.assertFalse(self.consume())
        self.driver.current_window_handle = "window-a"
        self.remember()
        with patch.object(module, "_load_acs_credentials", return_value=("test-account", "unused")), patch.object(
            module, "_consumable_list_context", return_value=self.context
        ), patch.object(module, "_read_complete_consumable_candidates", return_value=None):
            self.assertFalse(module._consume_initial_consumable_list(self.driver, {"task_id": "task-a"}))

    def test_change_during_full_dataset_validation_rejects_handoff(self):
        self.remember()
        with patch.object(module, "_load_acs_credentials", return_value=("test-account", "unused")), patch.object(
            module, "_consumable_list_context", side_effect=[self.context, dict(self.context, generation=2)]
        ), patch.object(module, "_read_complete_consumable_candidates", return_value=[]):
            self.assertFalse(module._consume_initial_consumable_list(self.driver, {"task_id": "task-a"}))

    def test_login_then_task_skips_duplicate_query_and_idle_wait_but_keeps_final_query(self):
        request = AmbulanceReturnRequest(
            task_id="task-a", created_at=datetime(2026, 7, 13, 8), raw_text="",
            case_id="20260713080319001", consumables={"桃-9吋手套-L(雙)": 1},
        )
        sid = "2026071310100308031901"
        candidates = [{"href": "/ACS/ACS15002?emmTemsisid=" + sid, "sid": sid, "text": "2026/07/13 08:05:05"}]
        driver = Mock(session_id="session-a", current_window_handle="window-a", current_url=module.ACS_URL)
        driver.find_elements.return_value = [object()]
        driver.get.side_effect = lambda url: setattr(driver, "current_url", url)
        context = dict(self.context)
        with patch.object(module, "_load_acs_credentials", return_value=("test-account", "unused")), patch.object(
            module, "create_chrome_driver_with_retry", return_value=driver
        ), patch.object(module, "apply_tile"), patch.object(module, "_fill_login_form"), patch.object(
            module, "_wait_until_sso_login_finished"
        ), patch.object(module, "_sso_login_succeeded", return_value=True), patch.object(module, "OUTPUT_DIR"), patch.object(
            module, "_prepare_consumable_list_snapshot", return_value=True
        ) as query, patch.object(module, "_open_acs_system", side_effect=lambda d, w: query(d, w)), patch.object(
            module, "_consumable_list_context", return_value=context
        ), patch.object(module, "_read_complete_consumable_candidates", return_value=candidates), patch.object(
            module, "_open_consumable_maintenance_page", side_effect=lambda d, w: query(d, w)
        ) as entry, patch.object(module.time, "sleep") as sleep, patch.object(
            module, "_wait_for_consumable_detail_page", return_value=True
        ), patch.object(module, "_consumable_detail_case_id", return_value=request.case_id), patch.object(
            module, "_consumable_detail_vehicle_label", return_value=""
        ), patch.object(module, "_write_current_consumable_page", return_value="confirmed"):
            module.login_acs_and_get_driver(task=request)
            module._open_consumable_record_for_task(driver, request)
            entry.assert_not_called()
            self.assertEqual(query.call_count, 2)
            sleep.assert_not_called()
            driver.current_url = module.ACS_URL
            module._open_consumable_record_for_task(driver, request)
            entry.assert_called_once()
            self.assertEqual(query.call_count, 4)
            sleep.assert_not_called()


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for the browser API contract")
class ClassificationMapReuseTests(unittest.TestCase):
    def setUp(self):
        self.driver = SimpleNamespace(session_id="session-a", current_window_handle="window-a", _acs_authenticated_account="test-account")
        self.calls = []
        self.responses = {
            "ACS15002/getAcsClassId": {"class-1": "A class"},
            "ACS15002/getAcsSeriesId": {"series-1": "B series"},
            "ACS15002/getAcsItemId": {"item-1": "C first", "item-2": "D second"},
        }
        self.driver.execute_script = self.execute_script

    def execute_script(self, source, *args):
        harness = r"""
global.window = global; global.contextPath = '/ACS/';
global.location = {origin: 'https://nfaemsap3.nfa.gov.tw', pathname: '/ACS/ACS15002'};
const responses = JSON.parse(process.argv[2]), args = JSON.parse(process.argv[3]), calls = [];
window.jQuery = {ajax: options => {
    const endpoint = options.url.substring('/ACS/'.length);
    calls.push([endpoint, options.data]);
    const response = responses[endpoint];
    if (response && response.error) options.error({}, 'error', 'simulated');
    else options.success(response && response.result ? response : {result: 'success', map: response});
}};
try {const value = new Function(process.argv[1])(...args);
    process.stdout.write(JSON.stringify({value, calls}));}
catch(error) {process.stdout.write(JSON.stringify({error: error.message, calls}));}
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness, source, json.dumps(self.responses), json.dumps(args)],
            text=True, encoding="utf-8", capture_output=True, check=True,
        )
        result = json.loads(result.stdout)
        self.calls.extend(result["calls"])
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["value"]

    def context(self):
        with patch.object(module, "_load_acs_credentials", return_value=("test-account", "unused")):
            return module._new_consumable_classification_context(self.driver, {})

    def resolve(self, context, prefix="C", **kwargs):
        return module._resolve_consumable_item_id(
            self.driver, class_type="type", class_id_prefix="A", series_prefix="B",
            item_prefix=prefix, item_name="測試耗材", classification_context=context, **kwargs,
        )

    def test_shared_map_reduces_same_parameter_http_calls_without_caching_item_id(self):
        context = self.context()
        self.assertEqual(self.resolve(context), "item-1")
        self.assertEqual(self.resolve(context, "D"), "item-2")
        self.assertEqual(self.resolve(context), "item-1")
        self.assertEqual(len(self.calls), 3)

    def test_new_task_cannot_reuse_previous_tasks_maps(self):
        self.resolve(self.context())
        self.resolve(self.context())
        self.assertEqual(len(self.calls), 6)

    def test_same_endpoint_with_different_complete_parameters_queries_again(self):
        context = self.context()
        self.resolve(context)
        self.responses["ACS15002/getAcsClassId"] = {"class-2": "A class"}
        module._resolve_consumable_item_id(
            self.driver, class_type="other-type", class_id_prefix="A", series_prefix="B",
            item_prefix="C", item_name="測試耗材", classification_context=context,
        )
        self.assertEqual(len(self.calls), 5)

    def test_duplicate_prefix_in_shared_map_still_stops(self):
        self.responses["ACS15002/getAcsItemId"]["item-3"] = "D duplicate"
        context = self.context()
        self.resolve(context)
        with self.assertRaisesRegex(RuntimeError, "前綴不唯一"):
            self.resolve(context, "D")
        self.assertEqual(len(self.calls), 3)

    def test_official_error_invalid_or_failed_response_stops_without_cache_fallback(self):
        for response in ({"result": "fail", "map": {}}, {"result": "unknown", "map": {}},
                         {"result": "success", "map": []}, {"error": True}):
            with self.subTest(response=response):
                self.responses["ACS15002/getAcsClassId"] = response
                context = self.context()
                with self.assertRaises(RuntimeError):
                    self.resolve(context)
                self.assertFalse(context["maps"])

    def test_session_or_account_change_stops_cached_lookup(self):
        for attribute in ("session_id", "_acs_authenticated_account", "current_window_handle"):
            with self.subTest(attribute=attribute):
                self.driver.session_id = "session-a"
                self.driver._acs_authenticated_account = "test-account"
                self.driver.current_window_handle = "window-a"
                context = self.context()
                self.resolve(context)
                setattr(self.driver, attribute, "changed")
                with self.assertRaisesRegex(RuntimeError, "帳號|session"):
                    self.resolve(context)

    def patient_workflow(self, quantities, *, fail_second_readback=False):
        request = AmbulanceReturnRequest(
            task_id="two-patients", created_at=datetime(2026, 7, 13, 8), raw_text="",
            case_id="20260713080319001", vehicle="新坡92", consumables=quantities,
        )
        hrefs = ["/ACS/ACS15002?emmTemsisid=202607131010030803190" + suffix for suffix in ("1", "2")]
        self.driver.current_url = module.ACS_URL
        self.driver.get = Mock(side_effect=lambda url: setattr(self.driver, "current_url", url))
        injected, saved, verified = {}, [], []

        def inject(driver, items):
            injected[driver.current_url] = copy.deepcopy(items)

        def save(driver, wait, **kwargs):
            saved.append((driver.current_url, copy.deepcopy(injected[driver.current_url])))
            return "儲存成功"

        def readback(driver):
            verified.append(driver.current_url)
            if fail_second_readback and driver.current_url.endswith("02"):
                return []
            return injected[driver.current_url]

        def script(source, *args):
            if "const classType" in source:
                return self.execute_script(source, *args)
            return "complete"

        self.driver.execute_script = script
        wait = SimpleNamespace(until=lambda predicate: predicate(self.driver))
        with patch.object(module, "_load_acs_credentials", return_value=("test-account", "unused")), patch.object(
            module, "WebDriverWait", return_value=wait
        ), patch.object(module, "_open_consumable_maintenance_page"), patch.object(
            module, "_find_consumable_detail_hrefs", return_value=hrefs
        ), patch.object(module, "_wait_for_consumable_detail_page", return_value=True), patch.object(
            module, "_consumable_detail_case_id", return_value=request.case_id
        ), patch.object(module, "_consumable_detail_vehicle_label", return_value=request.vehicle), patch.object(
            module, "consumable_inventory_options", return_value=[
                {"name": "桃-9吋手套-L(雙)", "code": "type-A-B-C"}, {"name": "口罩", "code": "type-A-B-D"},
            ]
        ), patch.dict("os.environ", {"AUTOMATION_DRY_RUN": "false", "SAVE_CONSUMABLES_RECORD": "true"}), patch.object(
            module, "_clear_existing_consumables"
        ), patch.object(module, "_inject_consumables_for_save", side_effect=inject), patch.object(
            module, "_assert_consumable_rows_match"
        ), patch.object(module, "_save_consumables", side_effect=save), patch.object(
            module, "_is_sso_page", return_value=False
        ), patch.object(module, "_read_consumable_payload_rows", side_effect=readback):
            if fail_second_readback:
                with self.assertRaisesRegex(RuntimeError, "成功=01；失敗=02；原因=耗材儲存後讀回不一致"):
                    module._open_consumable_record_for_task(self.driver, request)
            else:
                result = module._open_consumable_record_for_task(self.driver, request)
                self.assertIn("兩頁均已儲存確認", result)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(saved), 2)
        self.assertEqual(verified, [url for url, _ in saved])
        self.assertEqual(self.driver.get.call_args_list[1].args[0], saved[0][0])
        self.assertEqual(self.driver.get.call_args_list[3].args[0], saved[1][0])
        return [items for _, items in saved]

    def test_two_patients_share_classification_maps_and_each_save_and_read_back_once(self):
        pages = self.patient_workflow({"桃-9吋手套-L(雙)": 3, "口罩": 3})
        self.assertEqual([[item["quantity"] for item in page] for page in pages], [["2", "2"], ["1", "1"]])
        self.assertEqual([[item["itemId"] for item in page] for page in pages], [["item-1", "item-2"]] * 2)

    def test_empty_second_allocation_still_supplements_gloves_and_saves_each_patient(self):
        pages = self.patient_workflow({"口罩": 1})
        self.assertEqual([(page[0]["itemId"], page[0]["quantity"]) for page in pages], [("item-2", "1"), ("item-1", "1")])

    def test_second_patient_readback_mismatch_does_not_resend_first_or_report_success(self):
        self.patient_workflow({"桃-9吋手套-L(雙)": 2}, fail_second_readback=True)
