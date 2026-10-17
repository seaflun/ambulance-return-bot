import copy
import json
import shutil
import subprocess
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import consumables_login as module
from ambulance_bot.models import AmbulanceReturnRequest


def candidate(suffix):
    sid = f"20260713101003080319{suffix}"
    return {
        "href": f"/ACS/ACS15002?emmTemsisid={sid}",
        "sid": sid,
        "text": "2026/07/13 08:05:05 測試案件",
    }


class ConsumablesSpeedTests(unittest.TestCase):
    def request(self):
        return AmbulanceReturnRequest(
            task_id="speed-test",
            created_at=datetime(2026, 7, 13, 8, 0),
            raw_text="",
            case_id="20260713080319001",
            vehicle="新坡92",
            consumables={"桃-9吋手套-L(雙)": 2},
        )

    def test_complete_dataset_requeries_all_patients_without_fixed_observation(self):
        rows = [candidate("01"), candidate("02")]
        driver = Mock(current_url=module.ACS_URL)
        driver.execute_script.return_value = {"complete": True, "candidates": rows}
        with patch.object(module, "_stable_consumable_candidates") as fallback, patch.object(
            module.time, "sleep"
        ) as sleep, patch.object(module, "_prepare_consumable_list_snapshot", return_value=True) as refresh:
            result = module._collect_consumable_candidates(driver, self.request())
        self.assertEqual(result, rows)
        fallback.assert_not_called()
        sleep.assert_not_called()
        refresh.assert_called_once()

    def test_patient_collection_change_in_official_requery_stops(self):
        first = [candidate("01")]
        full = [candidate("01"), candidate("02")]
        driver = Mock(current_url=module.ACS_URL)
        snapshots = [first, full]
        with patch.object(module, "_read_complete_consumable_candidates", side_effect=snapshots), patch.object(
            module.time, "sleep"
        ), patch.object(module, "_prepare_consumable_list_snapshot", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "患者.*變動"):
                module._collect_consumable_candidates(driver, self.request())

    def test_patient_only_present_in_final_official_query_stops_instead_of_partial_plan(self):
        first = [candidate("01")]
        full = [candidate("01"), candidate("02")]
        driver = Mock(current_url=module.ACS_URL)
        with patch.object(module, "_read_complete_consumable_candidates", side_effect=[first, full]), patch.object(
            module.time, "sleep"
        ), patch.object(module, "_prepare_consumable_list_snapshot", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "患者.*變動"):
                module._collect_consumable_candidates(driver, self.request())

    def test_official_requery_failure_stops_instead_of_using_original_snapshot(self):
        first = [candidate("01")]
        full = [candidate("01"), candidate("02")]
        driver = Mock(current_url=module.ACS_URL)
        with patch.object(module, "_read_complete_consumable_candidates", return_value=first), patch.object(
            module.time, "sleep"
        ), patch.object(module, "_prepare_consumable_list_snapshot", return_value=False) as refresh:
            with self.assertRaisesRegex(RuntimeError, "正式重新查詢"):
                module._collect_consumable_candidates(driver, self.request())
        refresh.assert_called_once()

    def test_unrelated_case_change_does_not_reject_same_patient_collection(self):
        first = [candidate("01")]
        other = {
            "href": "/ACS/ACS15002?emmTemsisid=2026071310100399999901",
            "sid": "2026071310100399999901",
            "text": "2026/07/13 09:00:00 另一案件",
        }
        final = first + [other]
        driver = Mock(current_url=module.ACS_URL)
        with patch.object(module, "_read_complete_consumable_candidates", side_effect=[first, final]), patch.object(
            module.time, "sleep"
        ), patch.object(module, "_prepare_consumable_list_snapshot", return_value=True):
            self.assertEqual(module._collect_consumable_candidates(driver, self.request()), final)

    def test_unknown_dataset_requires_ui_collection_and_official_requery(self):
        rows = [candidate("01")]
        driver = Mock(current_url=module.ACS_URL)
        driver.execute_script.return_value = {"complete": False}
        with patch.object(module, "_reset_consumable_page_to_first"), patch.object(
            module, "_stable_consumable_candidates", return_value=rows
        ) as fallback, patch.object(module, "_prepare_consumable_list_snapshot", return_value=True) as refresh, patch.object(
            module, "_click_next_consumable_page_if_present", return_value=False
        ):
            result = module._collect_consumable_candidates(driver, self.request())
        self.assertEqual(result, rows)
        self.assertEqual(fallback.call_count, 2)
        refresh.assert_called_once()

    def test_observed_failed_or_pending_query_cannot_fall_back_to_stale_dom(self):
        driver = Mock(current_url=module.ACS_URL)
        for key in ("failed", "pending"):
            driver.execute_script.return_value = {"complete": False, key: True}
            with self.subTest(key=key), patch.object(module, "_stable_consumable_candidates") as fallback:
                with self.assertRaisesRegex(RuntimeError, "耗材查詢"):
                    module._collect_consumable_candidates(driver, self.request())
                fallback.assert_not_called()

    def test_fallback_collects_same_case_patients_across_pages_before_and_after_requery(self):
        first, second = [candidate("01")], [candidate("02")]
        driver = Mock(current_url=module.ACS_URL)
        with patch.object(module, "_read_complete_consumable_candidates", return_value=None), patch.object(
            module, "_reset_consumable_page_to_first"
        ), patch.object(module, "_stable_consumable_candidates", side_effect=[first, second, first, second]) as ready, patch.object(
            module, "_click_next_consumable_page_if_present", side_effect=[True, False, True, False]
        ), patch.object(module, "_wait_for_consumable_page_change", return_value=True), patch.object(
            module, "_prepare_consumable_list_snapshot", return_value=True
        ) as requery:
            self.assertEqual(module._collect_consumable_candidates(driver, self.request()), first + second)
        self.assertEqual(ready.call_count, 4)
        requery.assert_called_once()

    def test_fallback_requery_patient_change_stops_before_saving_any_patient(self):
        first, full = [candidate("01")], [candidate("01"), candidate("02")]
        driver = Mock(current_url=module.ACS_URL)
        driver.find_elements.return_value = [object()]
        with patch.object(module, "_open_consumable_maintenance_page"), patch.object(
            module, "_read_complete_consumable_candidates", return_value=None
        ), patch.object(module, "_reset_consumable_page_to_first"), patch.object(
            module, "_stable_consumable_candidates", side_effect=[first, full]
        ), patch.object(module, "_click_next_consumable_page_if_present", return_value=False), patch.object(
            module, "_prepare_consumable_list_snapshot", return_value=True
        ), patch.object(module, "_write_current_consumable_page") as write:
            with self.assertRaisesRegex(RuntimeError, "患者.*變動"):
                module._open_consumable_record_for_task(driver, self.request())
        write.assert_not_called()

    def test_reuses_current_list_only_with_complete_dataset(self):
        driver = Mock(current_url=module.ACS_URL)
        driver.execute_script.return_value = {"complete": True, "candidates": [candidate("01")]}
        with patch.object(module, "_is_consumable_maintenance_page_ready", return_value=True):
            module._open_consumable_maintenance_page(driver, Mock())
        driver.get.assert_not_called()

    def test_dry_run_validates_item_ids_without_mutating_existing_detail(self):
        driver = Mock(current_url="https://nfaemsap3.nfa.gov.tw" + candidate("01")["href"])
        expected = [{"itemId": "816", "quantity": "2", "name": "桃-9吋手套-L(雙)"}]
        with patch.dict("os.environ", {"AUTOMATION_DRY_RUN": "true"}), patch.object(
            module, "_resolve_consumable_item_quantities", return_value=expected
        ), patch.object(module, "_clear_existing_consumables") as clear, patch.object(
            module, "_fill_consumables"
        ) as fill, patch.object(module, "_save_consumables") as save, patch.object(
            module, "_inject_consumables_for_save"
        ) as inject:
            result = module._write_current_consumable_page(driver, Mock(), self.request())
        for mutation in (clear, fill, save, inject):
            mutation.assert_not_called()
        self.assertIn("未修改", result)

    def test_legacy_no_save_still_prefills_detail_when_dry_run_is_disabled(self):
        driver = Mock(current_url="https://nfaemsap3.nfa.gov.tw" + candidate("01")["href"])
        expected = [{"itemId": "816", "quantity": "2", "name": "桃-9吋手套-L(雙)"}]
        with patch.dict("os.environ", {"AUTOMATION_DRY_RUN": "false", "SAVE_CONSUMABLES_RECORD": "false"}), patch.object(
            module, "_resolve_consumable_item_quantities", return_value=expected
        ), patch.object(module, "_clear_existing_consumables") as clear, patch.object(
            module, "_fill_consumables", return_value=1
        ) as fill, patch.object(module, "_assert_consumable_rows_match"), patch.object(
            module, "_save_consumables"
        ) as save:
            result = module._write_current_consumable_page(driver, Mock(), self.request())
        clear.assert_called_once()
        fill.assert_called_once()
        save.assert_not_called()
        self.assertIn("未按儲存", result)

    def test_explicit_dry_run_checks_all_patient_details_without_legacy_prefill_rejection(self):
        driver = Mock(current_url=module.ACS_URL)
        rows = [candidate("01"), candidate("02")]
        with patch.dict("os.environ", {"AUTOMATION_DRY_RUN": "true", "SAVE_CONSUMABLES_RECORD": "false"}), patch.object(
            module, "_open_consumable_maintenance_page"
        ), patch.object(module, "_find_consumable_detail_hrefs", return_value=[row["href"] for row in rows]), patch.object(
            module, "_open_consumable_detail_if_needed"
        ), patch.object(module, "_wait_for_consumable_detail_page", return_value=True), patch.object(
            module, "_consumable_detail_case_id", return_value=self.request().case_id
        ), patch.object(module, "_consumable_detail_vehicle_label", return_value="新坡92"), patch.object(
            module, "_write_current_consumable_page", return_value="未修改或儲存。"
        ) as validate:
            result = module._open_consumable_record_for_task(driver, self.request())
        self.assertEqual(validate.call_count, 2)
        self.assertIn("兩頁均已唯讀核對", result)

    def test_reuses_verified_detail_but_checks_case_vehicle_before_writing(self):
        row = candidate("01")
        driver = Mock(current_url="https://nfaemsap3.nfa.gov.tw" + row["href"])
        with patch.object(module, "_open_consumable_maintenance_page"), patch.object(
            module, "_find_consumable_detail_hrefs", return_value=[row["href"]]
        ), patch.object(module, "_consumable_detail_payload_ready", return_value=True), patch.object(
            module, "_wait_for_consumable_detail_page", return_value=True
        ), patch.object(module, "_consumable_detail_case_id", return_value=self.request().case_id), patch.object(
            module, "_consumable_detail_vehicle_label", return_value="新坡93"
        ), patch.object(module, "_write_current_consumable_page") as write:
            with self.assertRaisesRegex(RuntimeError, "車輛不符"):
                module._open_consumable_record_for_task(driver, self.request())
        driver.get.assert_not_called()
        write.assert_not_called()

    def test_detail_identity_does_not_finish_before_item_ajax_initialization(self):
        row = candidate("01")
        driver = Mock(current_url="https://nfaemsap3.nfa.gov.tw" + row["href"])
        driver.execute_script.return_value = {
            "hasEmmTemsisidField": True, "emmTemsisid": row["sid"],
            "hasCaseIdField": True, "caseId": self.request().case_id,
            "hasCallNoField": True, "callNo": "新坡92", "ajaxActive": 1,
            "documentReady": "complete",
        }
        self.assertFalse(module._consumable_detail_payload_ready(driver))


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the browser-side contract")
class ConsumablesBrowserContractTests(unittest.TestCase):
    def run_ui_readiness(self, **changes):
        driver = Mock(current_url=module.ACS_URL)
        driver.execute_script.return_value = True
        module._consumable_list_ui_ready(driver)
        source = driver.execute_script.call_args.args[0]
        state = {"ready": "complete", "active": 0, "generation": 2, "completed": 2,
                 "query": "same", "currentQuery": "same", "status": 200, "result": "success",
                 "payload": [{"emmTemsisid": "test01"}], "rows": [{"emmTemsisid": "test01"}],
                 "visible": ["test01"], "text": "loaded row", "nodeCount": 1,
                 "start": 0, "end": 1, "recordsDisplay": 1, "drawing": False,
                 "processing": False, "datatable": True, "serverSide": False, "recordsTotal": 1,
                 "allRows": None}
        state.update(changes)
        harness = r"""
const s = JSON.parse(process.argv[2]); global.window = global;
global.location = {href: 'https://nfaemsap3.nfa.gov.tw/ACS/ACS15001', origin: 'https://nfaemsap3.nfa.gov.tw'};
const links = s.visible.map(id => ({getAttribute: () => '/ACS/ACS15002?emmTemsisid=' + id,
    closest: () => ({innerText: s.text})}));
global.document = {readyState: s.ready, querySelectorAll: () => links,
    querySelector: () => ({getClientRects: () => s.processing ? [{}] : []})};
global.getComputedStyle = () => ({visibility: 'visible'});
const api = {page: {info: () => ({start: s.start, end: s.end, recordsDisplay: s.recordsDisplay,
    serverSide: s.serverSide, recordsTotal: s.recordsTotal})},
    settings: () => [{bDrawing: s.drawing}], rows: options => ({
        data: () => ({toArray: () => options.page === 'all' ? (s.allRows || s.rows) : s.rows}),
        nodes: () => ({toArray: () => Array(s.nodeCount).fill({})})})};
window.jQuery = () => ({DataTable: () => api});
window.jQuery.active = s.active;
window.jQuery.fn = {dataTable: {isDataTable: () => s.datatable}};
window.__ambulanceAcsList = {generation: s.generation, completedGeneration: s.completed,
    query: s.query, fingerprint: () => s.currentQuery, status: s.status,
    payload: {result: s.result, list: s.payload}};
try {process.stdout.write(JSON.stringify({ready: new Function(process.argv[1])()}));}
catch(error) {process.stdout.write(JSON.stringify({error: error.message}));}
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness, source, json.dumps(state)],
            text=True, encoding="utf-8", capture_output=True, check=True,
        )
        return json.loads(result.stdout)

    def test_ui_readiness_accepts_complete_current_page_without_observation(self):
        self.assertEqual(self.run_ui_readiness(), {"ready": True})

    def test_ui_readiness_rejects_loading_ajax_pending_drawing_or_partial_rows(self):
        for change in ({"ready": "loading"}, {"active": 1}, {"completed": 1},
                       {"currentQuery": "changed"}, {"drawing": True}, {"processing": True},
                       {"nodeCount": 0}, {"end": 2}, {"text": ""}, {"rows": [{"emmTemsisid": "old"}]}):
            with self.subTest(change=change):
                self.assertEqual(self.run_ui_readiness(**change), {"ready": False})

    def test_ui_readiness_does_not_use_stale_rows_after_failed_or_invalid_query(self):
        for change in ({"status": 500}, {"result": "fail"}, {"status": None},
                       {"payload": [{"emmTemsisid": "test01"}, {"emmTemsisid": "test01"}]}):
            with self.subTest(change=change):
                self.assertIn("error", self.run_ui_readiness(**change))

    def test_client_side_payload_patient_missing_or_filtered_from_datatable_is_not_ready(self):
        payload = [{"emmTemsisid": "test01"}, {"emmTemsisid": "test02"}]
        for change in ({"payload": payload}, {"payload": payload, "allRows": payload, "recordsTotal": 2}):
            with self.subTest(change=change):
                self.assertEqual(self.run_ui_readiness(**change), {"ready": False})

    def test_legacy_without_datatables_requires_full_current_wire_and_dom_identity_match(self):
        self.assertEqual(self.run_ui_readiness(datatable=False), {"ready": True})
        self.assertEqual(self.run_ui_readiness(datatable=False, visible=[]), {"ready": False})

    def test_empty_list_requires_positive_current_query_and_empty_table_evidence(self):
        empty = {"payload": [], "rows": [], "visible": [], "nodeCount": 0, "end": 0,
                 "recordsDisplay": 0, "recordsTotal": 0}
        self.assertEqual(self.run_ui_readiness(**empty), {"ready": True})
        self.assertEqual(self.run_ui_readiness(**dict(empty, completed=1)), {"ready": False})

    def test_query_observer_keeps_wire_payload_and_ignores_older_completions(self):
        driver = Mock()
        driver.execute_script.return_value = False
        module._prepare_consumable_list_snapshot(driver, Mock())
        source = driver.execute_script.call_args.args[0]
        harness = r"""
global.window = global;
global.location = {href: 'https://nfaemsap3.nfa.gov.tw/ACS/ACS15001',
    origin: 'https://nfaemsap3.nfa.gov.tw'};
const events = {};
const settings = {url: '/ACS/ACS15001/getACS15001DTO'};
const xhr = label => ({status: 200, responseText: JSON.stringify({result: 'success', list: [label]}),
    responseJSON: {result: 'fail', list: ['mutated']}});
const send = x => events['ajaxSend.ambulanceAcsList']({}, x, settings);
const complete = x => events['ajaxComplete.ambulanceAcsList']({}, x, settings);
const button = {click: () => {const x = xhr('initial'); send(x); complete(x);}};
global.document = {getElementById: id => ({value: id}),
    querySelector: selector => selector === '#search' ? button : {value: '1'}};
window.jQuery = () => ({on: (event, callback) => { events[event] = callback; }});
new Function(process.argv[1])();
const older = xhr('older'), latest = xhr('latest');
send(older); send(latest); complete(latest); complete(older);
const state = window.__ambulanceAcsList;
process.stdout.write(JSON.stringify({generation: state.generation,
    completedGeneration: state.completedGeneration, payload: state.payload}));
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness, source], text=True, encoding="utf-8",
            capture_output=True, check=True,
        )
        state = json.loads(result.stdout)
        self.assertEqual(state["generation"], 3)
        self.assertEqual(state["completedGeneration"], 3)
        self.assertEqual(state["payload"], {"result": "success", "list": ["latest"]})

    def test_duplicate_official_item_prefix_never_selects_first_result(self):
        driver = Mock()
        driver.execute_script.return_value = {"ok": True, "itemId": "unused"}
        module._resolve_consumable_item_id(
            driver, class_type="test", class_id_prefix="A", series_prefix="B",
            item_prefix="C", item_name="測試耗材",
        )
        source = driver.execute_script.call_args.args[0]
        harness = r"""
global.window = global;
global.contextPath = '/ACS/';
window.jQuery = {ajax: (options) => options.success({result: 'success',
    map: {'first': 'A first', 'second': 'A second'}})};
try {
    new Function(process.argv[1])('test', 'A', 'B', 'C');
    process.stdout.write('selected');
} catch(error) { process.stdout.write(error.message); }
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness, source], text=True, encoding="utf-8",
            capture_output=True, check=True,
        )
        self.assertIn("不唯一", result.stdout)

    def run_snapshot(self, changes=None):
        rows = [candidate("01"), candidate("02")]
        state = {
            "generation": 2,
            "completedGeneration": 2,
            "result": "success",
            "status": 200,
            "query": "same-query",
            "currentQuery": "same-query",
            "serverSide": False,
            "active": 0,
            "rows": rows,
            "payloadRows": rows,
            "recordsTotal": 2,
            "currentPage": [rows[0]],
            "domPage": [rows[0]],
        }
        if changes:
            state.update(changes)
        driver = Mock(current_url=module.ACS_URL)
        module._read_complete_consumable_candidates(driver)
        source = driver.execute_script.call_args.args[0]
        harness = r"""
const s = JSON.parse(process.argv[1]);
global.window = global;
global.location = {href: 'https://nfaemsap3.nfa.gov.tw/ACS/ACS15001',
    origin: 'https://nfaemsap3.nfa.gov.tw', pathname: '/ACS/ACS15001'};
function link(row) { return {href: new URL(row.href, location.href).href,
    getAttribute: () => row.href}; }
function node(row) { return {innerText: row.text, textContent: row.text,
    querySelectorAll: () => [link(row)]}; }
function raw(row) {
    const value = {emmTemsisid: row.sid, attendanceDate: row.text};
    if (!s.omitReportUrl) value.url = Object.prototype.hasOwnProperty.call(s, 'reportUrlOverride')
        ? s.reportUrlOverride : s.externalReportUrls ? 'https://reports.example.invalid/form/' + row.sid : row.href;
    return value;
}
global.document = {readyState: 'complete',
    querySelector: () => ({id: 'result-datatable'}),
    querySelectorAll: () => s.domPage.map(row => link(row))};
window.__ambulanceAcsList = {
    generation: s.generation, completedGeneration: s.completedGeneration,
    status: s.status, query: s.query,
    fingerprint: () => s.currentQuery,
    payload: Object.prototype.hasOwnProperty.call(s, 'payloadOverride') ? s.payloadOverride :
        {result: s.result, list: s.payloadRows.map(raw)}
};
const api = {settings: () => [{bDrawing: false}],
    page: {info: () => ({serverSide: s.serverSide, recordsTotal: s.recordsTotal})},
    rows: (selector) => {
        const rows = selector?.page === 'current' ? s.currentPage : s.rows;
        return {data: () => ({toArray: () => rows.map(raw)}),
            nodes: () => ({toArray: () => rows.map(node)})};
    }};
const jq = () => ({DataTable: () => api});
jq.active = s.active;
jq.fn = {dataTable: {isDataTable: () => true}};
window.jQuery = jq;
const result = new Function(process.argv[2])();
process.stdout.write(JSON.stringify(result));
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness, json.dumps(state), source],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

    def test_full_successful_dataset_contains_all_pages(self):
        result = self.run_snapshot()
        self.assertTrue(result["complete"])
        self.assertEqual([row["sid"] for row in result["candidates"]], [candidate("01")["sid"], candidate("02")["sid"]])

    def test_external_report_url_does_not_replace_official_node_navigation_identity(self):
        result = self.run_snapshot({"externalReportUrls": True})
        self.assertTrue(result["complete"])
        self.assertEqual([row["href"] for row in result["candidates"]], [candidate("01")["href"], candidate("02")["href"]])
        foreign = dict(candidate("01"), href="https://foreign.example.invalid" + candidate("01")["href"])
        result = self.run_snapshot({"externalReportUrls": True, "rows": [foreign, candidate("02")]})
        self.assertFalse(result["complete"])

    def test_optional_report_url_may_be_missing_blank_or_null_without_blocking_navigation(self):
        for change in ({"omitReportUrl": True}, {"reportUrlOverride": ""}, {"reportUrlOverride": None}):
            with self.subTest(change=change):
                result = self.run_snapshot(change)
                self.assertTrue(result["complete"])
                self.assertEqual(len(result["candidates"]), 2)

    def test_empty_dataset_requires_the_same_successful_completion_contract(self):
        result = self.run_snapshot({"rows": [], "payloadRows": [], "recordsTotal": 0,
                                    "currentPage": [], "domPage": []})
        self.assertEqual(result, {"complete": True, "candidates": []})

    def test_observed_invalid_json_result_or_list_is_terminal_instead_of_unknown(self):
        for payload in (
            None, {}, {"result": "unknown", "list": []},
            {"result": "success", "list": None}, {"result": "success", "list": {}},
            {"result": "success", "list": [None]},
            {"result": "success", "list": [{"emmTemsisid": ""}]},
        ):
            with self.subTest(payload=payload):
                result = self.run_snapshot({"payloadOverride": payload})
                self.assertFalse(result["complete"])
                self.assertTrue(result.get("failed"))
        for change in (
            {"status": None}, {"payloadRows": [candidate("01"), candidate("01")]},
            {"payloadRows": [candidate("01"), candidate("01"), candidate("02")]},
        ):
            with self.subTest(change=change):
                self.assertTrue(self.run_snapshot(change).get("failed"))

    def test_stale_failed_pending_or_inconsistent_results_do_not_enter_fast_path(self):
        wrong_href = copy.deepcopy(candidate("01"))
        wrong_href["href"] = candidate("02")["href"]
        for change in (
            {"completedGeneration": 1}, {"result": "fail"}, {"status": 500},
            {"currentQuery": "different-query"}, {"serverSide": True}, {"active": 1},
            {"payloadRows": [candidate("01")]}, {"recordsTotal": 3},
            {"domPage": [candidate("02")]},
            {"rows": [candidate("01"), candidate("01")]},
            {"rows": [wrong_href, candidate("02")]},
        ):
            with self.subTest(change=change):
                self.assertFalse(self.run_snapshot(change)["complete"])


if __name__ == "__main__":
    unittest.main()
