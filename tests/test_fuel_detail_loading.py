import json
import shutil
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from ambulance_bot import selenium_local as runtime
from tests import test_saved_record_readback


class FuelDetailDriver:
    def __init__(self, *, loading=False, active=0, response=None, period="202609", card=1126,
                 deferred=False, grid_rows=None, missing_card=False):
        self.loading = loading
        self.active = active
        self.response = response if response is not None else {"Success": True, "Data": []}
        self.period = period
        self.card = card
        self.deferred = deferred
        self.grid_rows = grid_rows
        self.missing_card = missing_card
        self.get = Mock()

    def execute_script(self, script, *args):
        harness = """
        const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
        const page = input.page;
        let rows = input.rows || [];
        const grid = {dataSource: {data: () => rows, total: () => rows.length}};
        global.document = {readyState: page.loading ? 'loading' : 'complete',
          getElementById: id => ['Account', 'Password'].includes(id) ? null : {value: String(page.card)},
          querySelector: () => null,
          scripts: [{textContent: 'DriverName; dataTextField: "Text"'}]};
        global.location = {pathname: '/FUC04100/Detail', search: `?FCID=${page.card}&period=${page.period}`};
        global.jQuery = () => ({data: () => grid});
        jQuery.active = page.active;
        global.window = {jQuery, location};
        if (!page.missing_card) global.fcId = page.card;
        let pending;
        window.PostJsonData = (url, payload, callback) => {
          if (page.deferred) pending = callback;
          else callback(page.response);
        };
        window.GetFuelUseData = (card, period) => window.PostJsonData('/FUC04100/QueryFCUse',
          {FCID: card, PERIOD: period}, response => {
            rows = page.grid_rows || (response.Success ? response.Data : []);
          });
        if (input.state) window.__sinpoFuelDetailQueryState = input.state;
        const value = new Function(input.script)(...input.args);
        if (pending) pending(page.response);
        process.stdout.write(JSON.stringify({value, rows, state: window.__sinpoFuelDetailQueryState}));
        """
        output = subprocess.run([shutil.which("node"), "-e", harness],
                                input=json.dumps(dict(script=script, args=args, page={
                                    "loading": self.loading, "active": self.active, "response": self.response,
                                    "period": self.period, "card": self.card, "deferred": self.deferred,
                                    "grid_rows": self.grid_rows, "missing_card": self.missing_card},
                                    rows=getattr(self, "rows", []), state=getattr(self, "state", None))),
                                text=True, capture_output=True, encoding="utf-8", check=True)
        result = json.loads(output.stdout)
        self.state = result.get("state")
        self.rows = result.get("rows", [])
        return result.get("value")


@unittest.skipUnless(shutil.which("node"), "Node.js required for page-script tests")
class FuelDetailLoadingTests(unittest.TestCase):
    def request(self):
        return test_saved_record_readback.SavedRecordReadbackTests().request()

    def test_grid_existence_does_not_allow_loading_document_or_pending_ajax(self):
        for driver in (FuelDetailDriver(loading=True), FuelDetailDriver(active=1)):
            with self.subTest(loading=driver.loading, active=driver.active):
                self.assertFalse(runtime._is_ppe_fuel_record_detail_page(driver))

    def test_successful_empty_query_is_confirmed_before_records_are_used(self):
        loader = getattr(runtime, "_load_fuel_detail_records", None)
        self.assertIsNotNone(loader, "fuel detail needs an explicit completed read query")
        driver = FuelDetailDriver()
        loader(driver, self.request())
        self.assertTrue(driver.state["ready"])

    def test_detail_waits_through_loading_document_and_pending_ajax(self):
        class LoadingDriver(FuelDetailDriver):
            def __init__(self):
                super().__init__()
                self.phases = [(True, 0), (False, 1), (False, 0)]

            def execute_script(self, script, *args):
                if self.phases:
                    self.loading, self.active = self.phases.pop(0)
                return super().execute_script(script, *args)

        real_wait = runtime.WebDriverWait
        driver = LoadingDriver()
        with patch.object(runtime, "WebDriverWait", side_effect=lambda d, t: real_wait(d, 1, poll_frequency=0.001)):
            self.assertTrue(runtime._wait_for_ppe_fuel_record_detail_page(driver, timeout=30))
        self.assertEqual([], driver.phases)

    def test_rejected_wrong_month_or_wrong_card_queries_never_allow_an_empty_grid(self):
        loader = getattr(runtime, "_load_fuel_detail_records", None)
        self.assertIsNotNone(loader)
        scenarios = [FuelDetailDriver(response={"Success": False, "Data": [], "Message": "查詢失敗"}),
                     FuelDetailDriver(period="202610"), FuelDetailDriver(card=0), FuelDetailDriver(missing_card=True),
                     FuelDetailDriver(response={"Success": True, "Data": [{"FCID": 99, "Close_Period": "202609", "FCUseID": 3}]}),
                     FuelDetailDriver(response={"Success": True, "Data": [{"FCID": 1126, "Close_Period": "202610", "FCUseID": 3}]})]
        for driver in scenarios:
            with self.subTest(period=driver.period, card=driver.card, response=driver.response), \
                 self.assertRaises(runtime.WebDriverException):
                loader(driver, self.request())

    def test_delayed_query_keeps_existing_persisted_records(self):
        rows = [{"FCID": 1126, "Close_Period": "202609", "FCUseID": 3}]
        driver = FuelDetailDriver(deferred=True, response={"Success": True, "Data": rows})
        runtime._load_fuel_detail_records(driver, self.request())
        self.assertEqual(rows, driver.rows)
        self.assertEqual(["3"], driver.state["ids"])

    def test_grid_not_matching_successful_server_response_is_rejected(self):
        row = {"FCID": 1126, "Close_Period": "202609", "FCUseID": 3}
        driver = FuelDetailDriver(response={"Success": True, "Data": [row]}, grid_rows=[dict(row, FCUseID=4)])
        with self.assertRaises(runtime.WebDriverException):
            runtime._load_fuel_detail_records(driver, self.request())

    def test_cancel_stops_before_query_and_while_waiting(self):
        for side_effect in ([runtime.TaskCancellationError("cancelled")],
                            [None, runtime.TaskCancellationError("cancelled")]):
            driver = FuelDetailDriver()
            cancel = Mock(side_effect=side_effect)
            with self.subTest(side_effect=side_effect), self.assertRaises(runtime.TaskCancellationError):
                runtime._load_fuel_detail_records(driver, self.request(), cancel_check=cancel)
            self.assertEqual(len(side_effect), cancel.call_count)

    def test_invalid_historical_unit_price_stops_before_opening_browser(self):
        request = self.request()
        request.fuel_record.unit_price = "1699"
        with TemporaryDirectory() as tmp, patch.object(runtime, "_acquire_selenium_session") as acquire, \
             patch.object(runtime, "_create_driver") as create:
            result = runtime.run_fuel_record_task(request, Path(tmp))
        self.assertFalse(result.ok)
        self.assertIn("0", result.detail)
        self.assertIn("100", result.detail)
        acquire.assert_not_called()
        create.assert_not_called()

    def test_failed_detail_query_never_adds_or_saves_a_fuel_row(self):
        with ExitStack() as stack:
            for name, value in {"_wait_for_ppe_fuel_record_page": True, "_ensure_fuel_query_period": "2026/09",
                                "_fuel_card_labels": ["synthetic"], "_click_fuel_card_register": None,
                                "_wait_for_ppe_fuel_record_detail_page": True}.items():
                stack.enter_context(patch.object(runtime, name, return_value=value))
            add = stack.enter_context(patch.object(runtime, "_click_fuel_add_row"))
            save = stack.enter_context(patch.object(runtime, "_save_fuel_record_form"))
            driver = FuelDetailDriver(response={"Success": False, "Data": [], "Message": "查詢失敗"})
            with self.assertRaises(runtime.WebDriverException):
                runtime._prepare_fuel_record_form(driver, self.request())
            add.assert_not_called()
            save.assert_not_called()
