import unittest
import json
import shutil
import subprocess
from datetime import datetime
from unittest.mock import patch

from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot import selenium_local as runtime


def record(identity, start, end, first, last, month="2026/09"):
    return dict(Id=identity, StartDay="20260907", StartTime=start,
                EndDay="20260907", EndTime=end, StartMileage=first,
                EndMileage=last, Mileage=last-first, DriverName="甲",
                Destination="新坡", month=month)


class MileageBackfillTests(unittest.TestCase):
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
