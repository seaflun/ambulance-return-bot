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
            [record(1, "0800", "1100", 9980, 10000)],
            [record(1, "0800", "0900", 9980, 10030)],
            [record(1, "0800", "0900", 9980, 10000), record(2, "1200", "1300", 10000, 10010)],
            [record(1, "0800", "0900", 9980, 10000), record(2, "0800", "0900", 9980, 10000)],
        ]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(runtime.WebDriverException):
                self.run_entry(rows)

    def test_overlap_after_previous_return_uses_previous_end_as_start(self):
        request = self.request()
        request.case_date = "2026/09/26"
        request.case_time = "2029"
        request.return_date = "2026/09/26"
        request.return_time = "2151"
        request.mileage = "2887"
        previous = record(1, "1917", "2030", 2823, 2855)
        previous.update(StartDay="20260926", EndDay="20260926")
        plan = runtime._vehicle_mileage_backfill_plan(request, [previous])
        expected_start = datetime(2026, 9, 26, 20, 30)
        self.assertEqual(expected_start, plan["start_at"])
        self.assertEqual(previous, plan["previous"])
        values = runtime._vehicle_mileage_values(request, plan["start_mileage"], start_at=plan["start_at"])
        self.assertEqual("20260926", values["開始日期"])
        self.assertEqual("2030", values["開始時間"])
        self.assertEqual("2887", values["結束里程"])
        self.assertEqual("2029", request.case_time)
        saved = record(2, "2030", "2151", 2855, 2887)
        saved.update(StartDay="20260926", EndDay="20260926")
        retry_plan = runtime._vehicle_mileage_backfill_plan(request, [previous, saved])
        self.assertEqual(saved, retry_plan["existing"])

    def test_clamped_start_can_roll_over_to_next_date_and_month(self):
        request = self.request()
        request.case_date = "2026/09/30"
        request.case_time = "2359"
        request.return_date = "2026/10/01"
        request.return_time = "0030"
        previous = record(1, "2300", "0005", 9980, 10000, "2026/09")
        previous.update(StartDay="20260930", EndDay="20261001")
        plan = runtime._vehicle_mileage_backfill_plan(request, [previous])
        self.assertEqual(datetime(2026, 10, 1, 0, 5), plan["start_at"])
        values = runtime._vehicle_mileage_values(request, plan["start_mileage"], start_at=plan["start_at"])
        self.assertEqual("20261001", values["開始日期"])
        self.assertEqual("0005", values["開始時間"])

    def test_ambiguous_or_non_previous_overlap_still_fails_closed(self):
        request = self.request()
        overlaps = [
            record(1, "0800", "1003", 9980, 10000),
            record(2, "0900", "1005", 9980, 10010),
        ]
        starts_during_case = record(3, "1030", "1040", 10000, 10010)
        for rows in (overlaps, [record(1, "0800", "0900", 9980, 10000), starts_during_case]):
            with self.subTest(rows=rows), self.assertRaises(runtime.WebDriverException):
                runtime._vehicle_mileage_backfill_plan(request, rows)

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

    def test_unconfirmed_readback_does_not_advance_to_next_month(self):
        after = record(2, "1200", "1300", 10000, 10050, "2026/10")
        after.update(StartDay="20261001", EndDay="20261001")
        with patch.object(runtime, "_vehicle_mileage_history", return_value=[record(1, "0800", "0900", 9980, 10000), after]), \
             patch.object(runtime, "_load_vehicle_mileage_month", return_value=[record(1, "0800", "0900", 9980, 10000)]), \
             patch.object(runtime, "_write_vehicle_mileage_backfill") as write, \
             patch.object(runtime, "_verify_vehicle_mileage_backfill", side_effect=runtime.WebDriverException("not saved")) as verify, \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=True), \
             patch.object(runtime, "_save_vehicle_mileage_form", return_value=runtime.WAITING_CONFIRMATION_MARKER):
            detail = runtime._add_vehicle_mileage_record(object(), self.request())
            self.assertEqual(1, write.call_count)
            verify.assert_called_once()
            self.assertIn(runtime.WAITING_CONFIRMATION_MARKER, detail)

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
        global.jQuery = global.$;
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
class MileageEditTests(unittest.TestCase):
    def test_edit_rejects_moving_case_to_another_month(self):
        previous = MileageBackfillTests().request()
        current = AmbulanceReturnRequest.from_dict(previous.to_dict())
        current.case_date = "2026/10/07"
        with self.assertRaises(runtime.ManualUpdateRequiredError):
            runtime._vehicle_mileage_entry_plan(current, self.records(), previous)

    def test_retry_wrong_own_distance_remains_waiting(self):
        rows = self.records()
        rows[1].update(EndMileage=10030, Mileage=999)
        rows[2].update(StartMileage=10030, Mileage=20)
        _, detail, saves = self.run_edit(rows)
        self.assertIn(runtime.WAITING_CONFIRMATION_MARKER, detail)
        self.assertEqual(0, saves)

    def test_edit_does_not_accept_another_record_at_the_new_time(self):
        previous = MileageBackfillTests().request()
        current = AmbulanceReturnRequest.from_dict(previous.to_dict())
        current.case_time, current.return_time, current.mileage = "1200", "1230", "10030"
        rows = self.records()[:2] + [record(4, "1200", "1230", 10020, 10030)]
        with self.assertRaises(runtime.WebDriverException):
            runtime._vehicle_mileage_entry_plan(current, rows, previous)

    def run_edit(self, records, *, mileage="10030", return_time="1100", case_time="1000", save=True, fail_on_save=None):
        from copy import deepcopy
        previous = MileageBackfillTests().request()
        current = AmbulanceReturnRequest.from_dict(previous.to_dict())
        current.mileage, current.return_time, current.case_time = mileage, return_time, case_time
        persisted = deepcopy(records)
        driver = ScriptDriver(deepcopy([row for row in records if row["month"] == "2026/09"]))
        driver.get = lambda url: None
        def load(d, r, month, artifacts=None):
            d.rows = deepcopy([row for row in persisted if row["month"] == month])
            return deepcopy(d.rows)
        save_attempts = []
        def persist(d, **kwargs):
            save_attempts.append(1)
            if len(save_attempts) == fail_on_save:
                raise runtime.WebDriverException("synthetic save connection lost")
            for row in d.rows:
                index = next(i for i, old in enumerate(persisted) if old["Id"] == row["Id"])
                persisted[index] = deepcopy(row)
            return "saved"
        with patch.object(runtime, "_wait_for_ppe_vehicle_mileage_page", return_value=True), \
             patch.object(runtime, "_click_text_if_present"), patch.object(runtime.time, "sleep"), \
             patch.object(runtime, "_select_vehicle_record"), \
             patch.object(runtime, "_vehicle_mileage_history", side_effect=lambda *args: deepcopy(persisted)), \
             patch.object(runtime, "_load_vehicle_mileage_month", side_effect=load), \
             patch.object(runtime, "_vehicle_mileage_driver_value", return_value="synthetic-driver"), \
             patch.object(runtime, "_save_vehicle_mileage_enabled", return_value=save), \
             patch.object(runtime, "_save_vehicle_mileage_form", side_effect=persist) as saves:
            detail = runtime._prepare_vehicle_mileage_form(driver, current, update_context={"previous_task": previous.to_dict()})
        return persisted, detail, saves.call_count

    def records(self):
        return [record(1, "0800", "0900", 9980, 10000),
                record(3, "1000", "1100", 10000, 10020),
                record(2, "1200", "1300", 10020, 10050)]

    def test_edit_repairs_following_start_and_distance(self):
        rows, detail, saves = self.run_edit(self.records())
        self.assertEqual((10030, 10030, 20), (rows[1]["EndMileage"], rows[2]["StartMileage"], rows[2]["Mileage"]))
        self.assertEqual(1, saves)
        self.assertNotIn(runtime.WAITING_CONFIRMATION_MARKER, detail)

    def test_clamped_start_edit_preserves_actual_start_and_repairs_following(self):
        rows = self.records()
        rows[0]["EndTime"], rows[1]["StartTime"] = "1005", "1005"
        saved, _, _ = self.run_edit(rows)
        self.assertEqual("1005", saved[1]["StartTime"])
        self.assertEqual(10030, saved[2]["StartMileage"])

    def test_retry_repairs_following_after_case_was_already_saved(self):
        rows = self.records()
        rows[1].update(EndMileage=10030, Mileage=30)
        saved, _, saves = self.run_edit(rows)
        self.assertEqual(10030, saved[2]["StartMileage"])
        self.assertEqual(1, saves)

    def test_edit_rejects_end_mileage_beyond_following_end(self):
        with self.assertRaises(runtime.WebDriverException):
            self.run_edit(self.records(), mileage="10060")

    def test_edit_rejects_time_overlap_with_following(self):
        with self.assertRaises(runtime.WebDriverException):
            self.run_edit(self.records(), return_time="1230")

    def test_edit_repairs_following_in_next_month(self):
        rows = self.records()
        rows[2].update(StartDay="20261001", EndDay="20261001", month="2026/10")
        saved, _, saves = self.run_edit(rows)
        self.assertEqual((10030, 20), (saved[2]["StartMileage"], saved[2]["Mileage"]))
        self.assertEqual(2, saves)

    def test_cross_month_partial_failure_retries_without_duplicate_case(self):
        rows = self.records()
        rows[2].update(StartDay="20261001", EndDay="20261001", month="2026/10")
        partial, detail, _ = self.run_edit(rows, fail_on_save=2)
        self.assertIn(runtime.WAITING_CONFIRMATION_MARKER, detail)
        self.assertEqual((10030, 10020), (partial[1]["EndMileage"], partial[2]["StartMileage"]))
        complete, detail, _ = self.run_edit(partial)
        self.assertEqual(3, len(complete))
        self.assertEqual((10030, 10030, 20), (complete[1]["EndMileage"], complete[2]["StartMileage"], complete[2]["Mileage"]))
        self.assertNotIn(runtime.WAITING_CONFIRMATION_MARKER, detail)


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
            "前一件案件結束里程：10000（最近同步參考，非即時官網資料）",
            "上次該車輛登打的里程：10050（最近同步參考，非即時官網資料）",
            "前一件案件結束里程：尚無紀錄，登打時查詢里程系統（最近同步參考，非即時官網資料）",
            "上次該車輛登打的里程：20000（最近同步參考，非即時官網資料）",
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

    def test_matcher_can_find_case_using_clamped_mileage_start(self):
        request = MileageBackfillTests().request()
        row = record(3, "1005", "1100", 10000, 10020)
        start_at = datetime(2026, 9, 7, 10, 5)
        self.assertEqual([0], runtime._vehicle_mileage_matching_row_indices(
            ScriptDriver([row]), request, start_at=start_at,
        ))

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
                : selector.includes('vehicle') ? 'car' : '10020',
                closest: () => ({classList: {add() {}}})};
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
