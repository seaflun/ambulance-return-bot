import unittest
import json
import shutil
import subprocess
from datetime import datetime
from unittest.mock import Mock, patch

from selenium.common.exceptions import ElementClickInterceptedException, TimeoutException

from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot import selenium_local as runtime


def record(identity, start, end, first, last, month="2026/09"):
    return dict(Id=identity, StartDay="20260907", StartTime=start,
                EndDay="20260907", EndTime=end, StartMileage=first,
                EndMileage=last, Mileage=last-first, DriverName="甲",
                Destination="新坡", month=month)


class MileageBackfillTests(unittest.TestCase):
    def test_month_query_retries_loading_overlay_before_opening_vehicle(self):
        self._run_obstructed_month_query(2)

    def test_month_query_stops_when_loading_overlay_never_clears(self):
        self._run_obstructed_month_query(None)

    def _run_obstructed_month_query(self, blocked_attempts):
        driver = Mock()
        driver.current_url = "https://ppe.tyfd.gov.tw/CarRecord/List"
        driver.find_elements.return_value = []
        button = driver.find_element.return_value
        button.is_displayed.return_value = True
        button.is_enabled.return_value = True
        attempts = []

        def click():
            attempts.append(1)
            if blocked_attempts is None or len(attempts) <= blocked_attempts:
                raise ElementClickInterceptedException("Other element: jquery-spinner")

        def select_vehicle(current, label):
            self.assertGreater(len(attempts), blocked_attempts)
            current.current_url = "https://ppe.tyfd.gov.tw/CarRecord/Edit?id=1&period=2026/09"

        button.click.side_effect = click
        driver.execute_script.side_effect = [True, {"rows": []}]
        real_wait = runtime.WebDriverWait
        with patch.object(runtime, "WebDriverWait", side_effect=lambda d, t: real_wait(d, 0.05, poll_frequency=0.001)), \
             patch.object(runtime, "_wait_for_ppe_vehicle_mileage_page", return_value=True), \
             patch.object(runtime, "_select_daily_vehicle_mileage_month"), \
             patch.object(runtime, "_select_vehicle_record", side_effect=select_vehicle) as select, \
             patch.object(runtime, "vehicle_mileage_record_label", return_value="BSL-9230"):
            if blocked_attempts is None:
                with self.assertRaises(TimeoutException):
                    runtime._load_vehicle_mileage_month(driver, self.request(), "2026/09")
                select.assert_not_called()
                self.assertGreater(len(attempts), 1)
            else:
                self.assertEqual([], runtime._load_vehicle_mileage_month(driver, self.request(), "2026/09"))
                self.assertEqual(blocked_attempts + 1, len(attempts))

    def test_month_query_clicks_form_button_not_sidebar_query_link(self):
        class Button:
            def __init__(self, driver):
                self.driver = driver

            def is_displayed(self):
                return True

            def is_enabled(self):
                return True

            def click(self):
                self.driver.clicked.append("form-query")

        class Driver:
            def __init__(self):
                self.current_url = ""
                self.clicked = []

            def get(self, url):
                self.current_url = url

            def find_elements(self, by, selector):
                if selector == "#grid tbody":
                    return []
                return [self.find_element(by, selector)]

            def find_element(self, by, selector):
                if by != runtime.By.CSS_SELECTOR or selector != "#QueryForm button[onclick='Query()']":
                    raise AssertionError(f"unscoped query selector: {by} {selector}")
                return Button(self)

            def execute_script(self, script, *args):
                if "const ids = arguments[0]" in script:
                    # The live page has no _btnQuery; its first matching control is the sidebar link.
                    self.clicked.append("sidebar-query")
                    self.current_url = "https://ppe.tyfd.gov.tw/CarRecord/query"
                    return {"ok": True}
                if "return Boolean" in script:
                    return True
                return {"rows": []}

        driver = Driver()
        def select_vehicle(current, label):
            self.assertEqual(["form-query"], current.clicked)
            self.assertEqual("/CarRecord/List", runtime.urlsplit(current.current_url).path)
            current.current_url = "https://ppe.tyfd.gov.tw/CarRecord/Edit?id=1524&period=2026/09"
        with patch.object(runtime, "_wait_for_ppe_vehicle_mileage_page", return_value=True), \
             patch.object(runtime, "_select_daily_vehicle_mileage_month"), \
             patch.object(runtime, "_select_vehicle_record", side_effect=select_vehicle), \
             patch.object(runtime, "vehicle_mileage_record_label", return_value="CDD-2171"):
            self.assertEqual([], runtime._load_vehicle_mileage_month(driver, self.request(), "2026/09"))

    def request(self):
        return AmbulanceReturnRequest(
            task_id="backfill", created_at=datetime(2026, 9, 7, 18), raw_text="",
            vehicle="新坡91", driver="甲", case_address="新坡",
            case_date="2026/09/07", case_time="1000",
            return_date="2026/09/07", return_time="1100", mileage="10020")

    def run_entry(self, rows, save=True):
        with patch.object(runtime, "_extract_latest_end_mileage", return_value="10050"), \
             patch.object(runtime, "_add_vehicle_mileage_row"), \
             patch.object(runtime, "_fill_vehicle_grid_values"), \
             patch.object(runtime, "_assert_vehicle_mileage_values_present"), \
             patch.object(runtime.time, "sleep"), \
             patch.object(runtime, "_vehicle_mileage_history", return_value=rows, create=True), \
             patch.object(runtime, "_load_vehicle_mileage_month", side_effect=lambda d, r, m, a: [row for row in rows if row["month"] == m], create=True), \
             patch.object(runtime, "_write_vehicle_mileage_backfill", create=True) as write, \
             patch.object(runtime, "_verify_vehicle_mileage_backfill", create=True), \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=save), \
             patch.object(runtime, "_save_vehicle_mileage_form", return_value="saved") as persist:
            detail = runtime._add_vehicle_mileage_record(object(), self.request())
            return detail, write.call_args_list, persist.call_count

    def test_insert_uses_previous_end_and_repairs_next_start(self):
        before = record(1, "0800", "0900", 9980, 10000)
        after = record(2, "1200", "1300", 10000, 10050)
        detail, writes, saves = self.run_entry([after, before])
        self.assertEqual(1, saves)
        plan = writes[0].args[2]
        self.assertEqual("10000", plan["start_mileage"])
        self.assertEqual("10020", plan["end_mileage"])
        self.assertEqual(after, plan["following"])
        self.assertIsNone(plan["existing"])

    def test_retry_repairs_next_without_adding_existing_case(self):
        current = record(3, "1000", "1100", 10000, 10020)
        _, writes, _ = self.run_entry([
            record(1, "0800", "0900", 9980, 10000), current,
            record(2, "1200", "1300", 10000, 10050)])
        self.assertEqual(current, writes[0].args[2]["existing"])

    def test_invalid_neighbors_fail_before_writing(self):
        cases = [
            [record(2, "1200", "1300", 10000, 10050)],
            [record(1, "0800", "1030", 9980, 10000)],
            [record(1, "0800", "0900", 9980, 10030)],
            [record(1, "0800", "0900", 9980, 10000), record(2, "1200", "1300", 10000, 10010)],
            [record(1, "0800", "0900", 9980, 10000), record(2, "0800", "0900", 9980, 10000)],
        ]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(runtime.WebDriverException):
                self.run_entry(rows)

    def test_existing_complete_case_does_not_save_again(self):
        detail, writes, saves = self.run_entry([
            record(1, "0800", "0900", 9980, 10000),
            record(3, "1000", "1100", 10000, 10020),
            record(2, "1200", "1300", 10020, 10050)])
        self.assertIn("已存在", detail)
        self.assertEqual([], writes)
        self.assertEqual(0, saves)

    def test_cross_month_repairs_after_case_save_and_supports_retry(self):
        after = record(2, "1200", "1300", 10000, 10050, "2026/10")
        after.update(StartDay="20261001", EndDay="20261001")
        _, writes, saves = self.run_entry([record(1, "0800", "0900", 9980, 10000), after])
        self.assertEqual(2, saves)
        self.assertFalse(writes[0].kwargs["include_following"])
        self.assertTrue(writes[1].kwargs["following_only"])

    def test_cross_month_without_save_does_not_leave_partial_edits(self):
        after = record(2, "1200", "1300", 10000, 10050, "2026/10")
        after.update(StartDay="20261001", EndDay="20261001")
        detail, writes, saves = self.run_entry([record(1, "0800", "0900", 9980, 10000), after], save=False)
        self.assertIn(runtime.WAITING_CONFIRMATION_MARKER, detail)
        self.assertEqual([], writes)
        self.assertEqual(0, saves)

    def test_unknown_save_response_does_not_advance_to_next_month(self):
        after = record(2, "1200", "1300", 10000, 10050, "2026/10")
        after.update(StartDay="20261001", EndDay="20261001")
        with patch.object(runtime, "_vehicle_mileage_history", return_value=[record(1, "0800", "0900", 9980, 10000), after]), \
             patch.object(runtime, "_load_vehicle_mileage_month", return_value=[record(1, "0800", "0900", 9980, 10000)]), \
             patch.object(runtime, "_write_vehicle_mileage_backfill") as write, \
             patch.object(runtime, "_verify_vehicle_mileage_backfill") as verify, \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=True), \
             patch.object(runtime, "_save_vehicle_mileage_form", return_value=runtime.WAITING_CONFIRMATION_MARKER):
            runtime._add_vehicle_mileage_record(object(), self.request())
            self.assertEqual(1, write.call_count)
            verify.assert_not_called()

    def test_cross_day_and_roc_dates_use_previous_month_end(self):
        before = record(1, "2300", "0030", 9980, 10000, "2026/08")
        before.update(StartDay="115/08/31", EndDay="115/09/01")
        request = self.request()
        request.case_date = "2026/09/01"
        request.return_date = "2026/09/01"
        plan = runtime._vehicle_mileage_backfill_plan(request, [before])
        self.assertEqual("10000", plan["start_mileage"])

    def test_latest_case_has_no_following_repair(self):
        _, writes, saves = self.run_entry([record(1, "0800", "0900", 9980, 10000)])
        self.assertIsNone(writes[0].args[2]["following"])
        self.assertEqual(1, saves)

    def test_ppe_save_completed_message_is_recognized(self):
        self.assertEqual("success", runtime._save_confirmation_state("存檔完成"))

    def test_history_search_continues_across_empty_months(self):
        request = self.request()
        request.case_date = "2026/07/07"
        request.return_date = "2026/07/07"
        before = record(1, "0800", "0900", 9980, 10000, "2026/06")
        before.update(StartDay="20260630", EndDay="20260630")
        after = record(2, "1200", "1300", 10000, 10050)
        loaded = {"2026/06": [before], "2026/09": [after]}
        with patch.object(runtime, "_load_vehicle_mileage_month", side_effect=lambda d, r, m, a: loaded.get(m, [])) as load, \
             patch.object(runtime, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 7)
            rows = runtime._vehicle_mileage_history(object(), request)
        self.assertEqual(["2026/07", "2026/06", "2026/08", "2026/09"], [c.args[2] for c in load.call_args_list])
        self.assertEqual([before, after], rows)


class ScriptDriver:
    def __init__(self, rows):
        self.rows = rows

    def execute_script(self, script, *args):
        harness = r"""
        const fs = require('fs');
        const input = JSON.parse(fs.readFileSync(0, 'utf8'));
        const rows = input.rows.map(data => Object.assign(data, {
          get(key) { return this[key]; }, set(key, value) { this[key] = value; },
          toJSON() { return Object.fromEntries(Object.entries(this).filter(([k,v]) => typeof v !== 'function')); }
        }));
        const grid = {dataSource: {data: () => rows, total: () => rows.length}, refresh() {}};
        global.window = global;
        global.$ = () => ({data: () => grid});
        const result = new Function(input.script)(...input.args);
        process.stdout.write(JSON.stringify({result, rows}));
        """
        result = subprocess.run([shutil.which("node"), "-e", harness],
                                input=json.dumps(dict(script=script, args=args, rows=self.rows)),
                                text=True, capture_output=True, encoding="utf-8", check=True)
        output = json.loads(result.stdout)
        self.rows = output["rows"]
        return output["result"]


@unittest.skipUnless(shutil.which("node"), "Node.js required for browser-script tests")
class MileageGridScriptTests(unittest.TestCase):
    def test_historical_hint_uses_previous_mileage_and_changes_with_case_or_vehicle(self):
        source = (runtime.Path(runtime.__file__).parent.parent / "templates/new_task.html").read_text(encoding="utf-8")
        function = source[source.index("function updateLastMileageHint("):source.index("function updateReasonOptionsForSummaryType(")]
        script = """
        const lastVehicleMileages = {car: '10050', other: '20000'};
        const vehicleMileageHintHistory = {car: [
          {time:'202609071200', mileage:'10050'}, {time:'202609070800', mileage:'10000'}]};
        let clock = '1000', car = 'car';
        const hint = {textContent:''};
        const document = {querySelector(selector) {
          if (selector.includes('data-last-mileage')) return hint;
          return {value: selector.includes('case_date') ? '2026/09/07' : selector.includes('case_time') ? clock : car};
        }};
        """ + function + """
        const values = [];
        for (const time of ['1000', '1400', '0700']) {
          clock = time; updateLastMileageHint('vehicle'); values.push(hint.textContent);
        }
        car = 'other'; updateLastMileageHint('vehicle_2'); values.push(hint.textContent);
        process.stdout.write(JSON.stringify(values));
        """
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual([
            "前一件案件結束里程：10000", "上次該車輛登打的里程：10050",
            "前一件案件結束里程：尚無紀錄，登打時查詢里程系統", "上次該車輛登打的里程：20000",
        ], json.loads(result.stdout))

    def test_following_row_is_found_by_id_after_insert_and_only_mileages_change(self):
        request = MileageBackfillTests().request()
        before = record(1, "0800", "0900", 9980, 10000)
        after = record(2, "1200", "1300", 10000, 10050)
        after.update(Reason="保養", Remarks="保留備註", Driver=77)
        plan = runtime._vehicle_mileage_backfill_plan(request, [after, before])
        driver = ScriptDriver([after.copy(), before.copy()])
        def insert(_driver):
            driver.rows.insert(0, record("", "1000", "1100", 10000, 10020))
        with patch.object(runtime, "_add_vehicle_mileage_row", side_effect=insert), \
             patch.object(runtime, "_fill_vehicle_grid_values"), \
             patch.object(runtime, "_assert_vehicle_mileage_values_present"):
            runtime._write_vehicle_mileage_backfill(driver, request, plan, include_following=True)
        self.assertEqual(dict(after, StartMileage=10020, Mileage=30), driver.rows[1])
        self.assertEqual(before, driver.rows[2])

    def test_concurrent_record_change_stops_before_new_row(self):
        request = MileageBackfillTests().request()
        before = record(1, "0800", "0900", 9980, 10000)
        after = record(2, "1200", "1300", 10000, 10050)
        plan = runtime._vehicle_mileage_backfill_plan(request, [before, after])
        driver = ScriptDriver([before, dict(after, EndMileage=10060)])
        with patch.object(runtime, "_add_vehicle_mileage_row") as add, self.assertRaises(runtime.WebDriverException):
            runtime._write_vehicle_mileage_backfill(driver, request, plan, include_following=True)
        add.assert_not_called()

    def test_matcher_accepts_ppe_slash_dates(self):
        row = record(3, "1000", "1100", 10000, 10020)
        row.update(StartDay="2026/09/07", EndDay="2026/09/07")
        self.assertEqual([0], runtime._vehicle_mileage_matching_row_indices(ScriptDriver([row]), MileageBackfillTests().request()))

    def test_readback_checks_both_mileages_and_preserved_following_fields(self):
        request = MileageBackfillTests().request()
        before = record(1, "0800", "0900", 9980, 10000)
        after = record(2, "1200", "1300", 10000, 10050)
        plan = runtime._vehicle_mileage_backfill_plan(request, [before, after])
        driver = ScriptDriver([before, record(3, "1000", "1100", 10000, 10020), dict(after, StartMileage=10020, Mileage=30)])
        with patch.object(runtime, "_load_vehicle_mileage_month", side_effect=lambda *args: driver.rows):
            runtime._verify_vehicle_mileage_backfill(driver, request, plan, "2026/09", None, True)
            driver.rows[2]["EndMileage"] = 10051
            with self.assertRaises(runtime.WebDriverException):
                runtime._verify_vehicle_mileage_backfill(driver, request, plan, "2026/09", None, True)

    def test_client_historical_mileage_is_deferred_but_current_mileage_still_checked(self):
        folder = runtime.Path(runtime.__file__).parent.parent / "templates"
        for template in ("new_task.html", "disaster_task.html"):
            source = (folder / template).read_text(encoding="utf-8")
            begin = source.index("function mileageChangeErrors(")
            stop = source.index("function showClientFormErrors", begin) if template == "new_task.html" else source.index("form.addEventListener('submit'", begin)
            function = source[begin:stop]
            prefix = """
            const lastVehicleMileages = {car: '10050'}, lastVehicleMileageTimes = {car: '202609071200'};
            const enforceMileageChangeLimit = true, maxVehicleMileageChangeKm = 300;
            let day = '2026/09/07', clock = '1000';
            const document = {querySelector(selector) {
              return {value: selector.includes('case_date') ? day : selector.includes('case_time') ? clock
                : selector.includes('vehicle') ? 'car' : '10020'};
            }};
            const form = document, card = document;
            """
            call = "mileageChangeErrors('vehicle','mileage')" if template == "new_task.html" else "mileageChangeErrors(card,0)"
            script = prefix + function + f"const old = {call}; clock = '1400'; const current = {call}; process.stdout.write(JSON.stringify([old,current]));"
            result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
            old, current = json.loads(result.stdout)
            self.assertEqual([], old, template)
            self.assertEqual(1, len(current), template)


if __name__ == "__main__":
    unittest.main()
