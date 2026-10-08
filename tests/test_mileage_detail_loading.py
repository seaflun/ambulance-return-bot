import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from selenium.common.exceptions import TimeoutException

from ambulance_bot import selenium_local as runtime
from tests import test_vehicle_mileage_backfill as backfill


class MileageDetailDriver:
    def __init__(self, phases):
        self.phases = list(phases)
        self.current_url = "https://ppe.tyfd.gov.tw/CarRecord/List"

    def get(self, url):
        self.current_url = url

    def find_elements(self, by, selector):
        return []

    def execute_script(self, script, *args):
        if "return Boolean" in script:
            return True
        phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
        harness = """
        const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
        const phase = input.phase;
        const rows = phase.rows.map(row => ({...row, toJSON() {return {...row};}}));
        const source = {data: () => rows, total: () => phase.total ?? rows.length,
          _requestInProgress: !!phase.pending};
        const grid = {dataSource: source};
        global.document = {readyState: phase.loading ? 'loading' : 'complete'};
        global.$ = global.jQuery = () => ({data: () => grid});
        jQuery.active = phase.active || 0;
        global.window = {$, jQuery};
        if (!phase.missing_payload) global.recordList = phase.server_rows || phase.rows;
        const value = new Function(input.script)(...input.args);
        process.stdout.write(JSON.stringify({value}));
        """
        result = subprocess.run([shutil.which("node"), "-e", harness],
                                input=json.dumps(dict(script=script, args=args, phase=phase)),
                                text=True, capture_output=True, encoding="utf-8", check=True)
        return json.loads(result.stdout)["value"]


@unittest.skipUnless(shutil.which("node"), "Node.js required for browser-script tests")
class MileageDetailLoadingTests(unittest.TestCase):
    def load(self, phases):
        driver = MileageDetailDriver(phases)
        real_wait = runtime.WebDriverWait

        def select(current, label):
            current.current_url = "https://ppe.tyfd.gov.tw/CarRecord/Edit?id=1524&period=2026/09"

        with patch.object(runtime, "WebDriverWait", side_effect=lambda d, t: real_wait(d, 0.25, poll_frequency=0.001)), \
             patch.object(runtime, "_wait_for_ppe_vehicle_mileage_page", return_value=True), \
             patch.object(runtime, "_select_daily_vehicle_mileage_month"), \
             patch.object(runtime, "_select_vehicle_record", side_effect=select), \
             patch.object(runtime, "vehicle_mileage_record_label", return_value="KES-5922"), \
             patch.object(runtime.EC, "element_to_be_clickable", return_value=lambda d: type("Button", (), {"click": lambda s: None})()):
            return runtime._load_vehicle_mileage_month(driver, backfill.MileageBackfillTests().request(), "2026/09")

    def test_loading_empty_grid_does_not_hide_persisted_previous_record(self):
        before = backfill.record(1, "0800", "0900", 9980, 10000)
        for phase in ({"loading": True}, {"active": 1}, {"pending": True}):
            with self.subTest(phase=phase):
                rows = self.load([dict(phase, rows=[]), {"rows": [before]}])
                self.assertEqual([before], rows)

    def test_unfinished_query_never_becomes_empty_history(self):
        with self.assertRaises(TimeoutException):
            self.load([{"active": 1, "rows": []}])

    def test_completed_empty_month_is_still_valid(self):
        self.assertEqual([], self.load([{"rows": []}]))

    def test_incomplete_history_remains_blocked(self):
        with self.assertRaises(runtime.WebDriverException):
            self.load([{"rows": [], "total": 1}])

    def test_grid_must_match_the_server_rendered_history_payload(self):
        before = backfill.record(1, "0800", "0900", 9980, 10000)
        for phase in [{"rows": [], "server_rows": [before]},
                      {"rows": [dict(before, Id=2)], "server_rows": [before]},
                      {"rows": [], "missing_payload": True}]:
            with self.subTest(phase=phase), self.assertRaises(runtime.WebDriverException):
                self.load([phase])


class MileageHistoryRecoveryTests(unittest.TestCase):
    def request(self):
        request = backfill.MileageBackfillTests().request()
        request.case_date, request.case_time = "2026/10/08", "2234"
        request.return_date, request.return_time = "2026/10/09", "0027"
        request.mileage = "25117"
        return request

    def rows(self):
        september = backfill.record(1, "0900", "1000", 25100, 25159)
        september.update(StartDay="20260930", EndDay="20260930")
        october = backfill.record(2, "0900", "1029", 25090, 25095, "2026/10")
        october.update(StartDay="20261008", EndDay="20261008")
        return september, october

    def test_invalid_plan_refreshes_case_month_before_deciding_history_is_wrong(self):
        september, october = self.rows()
        with patch.object(runtime, "_vehicle_mileage_history", return_value=[september]), \
             patch.object(runtime, "_load_vehicle_mileage_month", return_value=[october]), \
             patch.object(runtime, "_write_vehicle_mileage_backfill") as write, \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=False):
            detail = runtime._add_vehicle_mileage_record(object(), self.request())
        self.assertIn("未按儲存", detail)
        self.assertEqual("25095", write.call_args.args[2]["start_mileage"])

    def test_real_mileage_conflict_still_stops_after_fresh_case_query(self):
        september, october = self.rows()
        october.update(EndMileage=25159, Mileage=69)
        with patch.object(runtime, "_vehicle_mileage_history", return_value=[september]), \
             patch.object(runtime, "_load_vehicle_mileage_month", return_value=[october]), \
             patch.object(runtime, "_write_vehicle_mileage_backfill") as write, \
             patch.object(runtime, "_save_vehicle_mileage_form") as save:
            with self.assertRaisesRegex(runtime.WebDriverException, "2026/10/08 10:29、紀錄 2、結束里程 25159"):
                runtime._add_vehicle_mileage_record(object(), self.request())
        write.assert_not_called()
        save.assert_not_called()

    def test_cancel_stops_before_refresh_and_any_write(self):
        september, _ = self.rows()
        with patch.object(runtime, "_vehicle_mileage_history", return_value=[september]), \
             patch.object(runtime, "_load_vehicle_mileage_month") as load, \
             patch.object(runtime, "_write_vehicle_mileage_backfill") as write:
            with self.assertRaises(runtime.TaskCancellationError):
                runtime._add_vehicle_mileage_record(object(), self.request(),
                    cancel_check=lambda: (_ for _ in ()).throw(runtime.TaskCancellationError("cancelled")))
        load.assert_not_called()
        write.assert_not_called()

    def test_cancel_during_final_refresh_stops_before_filling_the_grid(self):
        before = backfill.record(1, "0800", "0900", 9980, 10000)
        cancelled = [False]

        def refresh(*args):
            cancelled[0] = True
            return [before]

        def check():
            if cancelled[0]:
                raise runtime.TaskCancellationError("cancelled")

        with patch.object(runtime, "_vehicle_mileage_history", return_value=[before]), \
             patch.object(runtime, "_load_vehicle_mileage_month", side_effect=refresh), \
             patch.object(runtime, "_write_vehicle_mileage_backfill") as write, \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=False):
            with self.assertRaises(runtime.TaskCancellationError):
                runtime._add_vehicle_mileage_record(object(), backfill.MileageBackfillTests().request(), cancel_check=check)
        write.assert_not_called()
