import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from werkzeug.datastructures import MultiDict

import app as app_module
from ambulance_bot.models import FuelRecord
from tests import test_web_app as web_fixtures


INVALID_PRICES = ("", "abc", "NaN", "Infinity", "-Infinity", "-1", "100.01", "1699", "1e1", "100.000000000000000000001")
VALID_PRICES = ("0", "29.9", "100", "029.900", "99.999999999999999999999", "100.000000000000000000000")
PRICE_MESSAGE = "0–100 元／公升"


class FuelPriceModelTests(unittest.TestCase):
    def test_enabled_price_accepts_inclusive_decimal_range_without_rewriting(self):
        for value in VALID_PRICES:
            with self.subTest(value=value):
                record = FuelRecord(enabled=True, unit_price=value)
                record.validate_unit_price()
                self.assertEqual(value, record.unit_price)

    def test_enabled_price_rejects_invalid_nonfinite_and_out_of_range_values(self):
        for value in INVALID_PRICES:
            with self.subTest(value=value):
                record = FuelRecord(enabled=True, unit_price=value)
                with self.assertRaisesRegex(ValueError, PRICE_MESSAGE):
                    record.validate_unit_price()
                self.assertEqual(value, record.unit_price)

    def test_disabled_price_does_not_require_validation(self):
        for value in INVALID_PRICES:
            with self.subTest(value=value):
                FuelRecord(enabled=False, unit_price=value).validate_unit_price()

    def test_historical_invalid_price_still_loads_without_rewriting(self):
        record = FuelRecord.from_dict({"enabled": True, "unit_price": "1699"})
        self.assertTrue(record.enabled)
        self.assertEqual("1699", record.unit_price)


class FuelPriceWebTests(unittest.TestCase):
    setUp = web_fixtures.WebAppTests.setUp
    tearDown = web_fixtures.WebAppTests.tearDown
    _restore_env = web_fixtures.WebAppTests._restore_env
    valid_task_data = web_fixtures.WebAppTests.valid_task_data
    rendered_form_errors = web_fixtures.WebAppTests.rendered_form_errors

    def fuel_data(self, price, **overrides):
        return self.valid_task_data(
            fuel_record="1", fuel_date="20260607", fuel_time="1120",
            fuel_quantity="56.815", fuel_unit_price=price, **overrides,
        )

    def disaster_data(self, price, case_id="FIRE-PRICE", second_price=None):
        data = MultiDict([
            ("case_id", case_id), ("case_date", "2026/07/22"), ("case_time", "1207"),
            ("return_time", "1300"), ("case_address", "桃園市觀音區測試路1號"),
            ("summary_type", "火災"), ("case_reason", "一般(集合)住宅"),
            ("personnel", "甲,乙,丙"), ("commander", "丙"), ("action_note", "現場待命"),
            ("recorder_category", "轄內A3"), ("vehicle", "新坡11"), ("driver", "甲"),
            ("vehicle_return_time", "1300"), ("mileage", "100"),
            ("fuel_enabled", "0"), ("fuel_date", "20260722"), ("fuel_time", "1300"),
            ("fuel_quantity", "56.815"), ("fuel_unit_price", price),
        ])
        if second_price is not None:
            for name, value in (
                ("vehicle", "新坡15"), ("driver", "乙"), ("vehicle_return_time", "1300"),
                ("mileage", "200"), ("fuel_enabled", "1"), ("fuel_date", "20260722"),
                ("fuel_time", "1300"), ("fuel_quantity", "40"), ("fuel_unit_price", second_price),
            ):
                data.add(name, value)
        return data

    def test_ems_create_rejects_invalid_prices_without_storing_or_queueing(self):
        for index, price in enumerate(INVALID_PRICES):
            with self.subTest(price=price), mock.patch.object(app_module, "queue_task_for_worker") as queue:
                before = self.store.list_recent()
                response = self.client.post("/tasks", data=self.fuel_data(price, case_id=f"EMS-BAD-{index}"))
                self.assertEqual(400, response.status_code)
                self.assertIn(f"1車加油單價需為 {PRICE_MESSAGE}的有限數字", self.rendered_form_errors(response))
                self.assertEqual(before, self.store.list_recent())
                queue.assert_not_called()

    def test_ems_create_accepts_both_boundaries_and_preserves_decimal_strings(self):
        for index, price in enumerate(VALID_PRICES):
            with self.subTest(price=price):
                response = self.client.post("/tasks", data=self.fuel_data(price, case_id=f"EMS-GOOD-{index}"))
                self.assertEqual(302, response.status_code)
                task_id = response.headers["Location"].rsplit("/", 1)[-1]
                self.assertEqual(price, self.store.get(task_id)["task"]["fuel_record"]["unit_price"])

    def test_ems_second_vehicle_price_is_validated(self):
        response = self.client.post("/tasks", data=self.fuel_data(
            "29.9", two_vehicle="1", vehicle_2="新坡92", driver_2="王昱勛",
            return_time_2="1119", mileage_2="200", patient_summary_2="女一名",
            consumables_2="桃-口罩(片)=2", fuel_record_2="1", fuel_date_2="20260607",
            fuel_time_2="1120", fuel_quantity_2="40", fuel_unit_price_2="1699",
        ))
        self.assertEqual(400, response.status_code)
        self.assertIn(f"2車加油單價需為 {PRICE_MESSAGE}的有限數字", self.rendered_form_errors(response))
        self.assertEqual([], self.store.list_recent())

    def test_ems_edit_rejects_invalid_price_without_changing_existing_task(self):
        created = self.client.post("/tasks", data=self.fuel_data("29.9"))
        self.assertEqual(302, created.status_code)
        task_id = created.headers["Location"].rsplit("/", 1)[-1]
        before = self.store.get(task_id)
        for price in INVALID_PRICES:
            with self.subTest(price=price):
                response = self.client.post(f"/tasks/{task_id}/edit", data=self.fuel_data(price))
                self.assertEqual(400, response.status_code)
                self.assertIn(f"1車加油單價需為 {PRICE_MESSAGE}的有限數字", self.rendered_form_errors(response))
                self.assertEqual(before, self.store.get(task_id))

    def test_disaster_create_rejects_invalid_price_before_folders_or_store(self):
        for index, price in enumerate(INVALID_PRICES):
            with self.subTest(price=price), mock.patch.object(app_module, "ensure_disaster_media_folders", return_value=[]) as folders:
                before = self.store.list_recent()
                response = self.client.post("/tasks/disaster", data=self.disaster_data(price, f"FIRE-BAD-{index}"))
                self.assertEqual(400, response.status_code)
                self.assertIn(f"第1車加油單價需為 {PRICE_MESSAGE}的有限數字", self.rendered_form_errors(response))
                self.assertEqual(before, self.store.list_recent())
                folders.assert_not_called()

    def test_disaster_second_vehicle_price_is_validated(self):
        with mock.patch.object(app_module, "ensure_disaster_media_folders", return_value=[]) as folders:
            response = self.client.post("/tasks/disaster", data=self.disaster_data("29.9", second_price="1699"))
        self.assertEqual(400, response.status_code)
        self.assertIn(f"第2車加油單價需為 {PRICE_MESSAGE}的有限數字", self.rendered_form_errors(response))
        self.assertEqual([], self.store.list_recent())
        folders.assert_not_called()

    def test_disaster_create_accepts_inclusive_bounds_without_rewriting(self):
        for index, price in enumerate(VALID_PRICES):
            with self.subTest(price=price), mock.patch.object(app_module, "ensure_disaster_media_folders", return_value=[]):
                response = self.client.post("/tasks/disaster", data=self.disaster_data(price, f"FIRE-GOOD-{index}"))
                self.assertEqual(302, response.status_code)
                task_id = response.headers["Location"].rsplit("/", 1)[-1]
                self.assertEqual(price, self.store.get(task_id)["task"]["vehicle_entries"][0]["fuel_record"]["unit_price"])

    def test_disabled_fuel_allows_other_task_fields_without_valid_price(self):
        data = self.fuel_data("1699")
        data["fuel_record"] = "0"
        response = self.client.post("/tasks", data=data)
        self.assertEqual(302, response.status_code)

    def test_historical_invalid_fuel_price_is_visible_in_edit_form(self):
        task = app_module.request_from_form(self.fuel_data("1699"))
        self.store.create(task)
        response = self.client.get(f"/tasks/{task.task_id}/edit")
        self.assertEqual(200, response.status_code)
        self.assertIn('value="1699"', response.get_data(as_text=True))
        self.assertEqual("1699", self.store.get(task.task_id)["task"]["fuel_record"]["unit_price"])

    def test_rendered_forms_describe_price_per_litre_and_bound_number_inputs(self):
        responses = (
            self.client.post("/tasks", data=self.fuel_data("1699", two_vehicle="1")),
            self.client.post("/tasks/disaster", data=self.disaster_data("1699")),
        )
        for response in responses:
            with self.subTest(endpoint=response.request.path):
                self.assertEqual(400, response.status_code)
                body = response.get_data(as_text=True)
                fields = re.findall(r'<input\b[^>]*name="fuel_unit_price(?:_2)?"[^>]*>', body)
                self.assertTrue(fields)
                for field in fields:
                    for expected in ('type="number"', 'min="0"', 'max="100"', 'step="any"'):
                        self.assertIn(expected, field)
                    self.assertNotIn("data-numeric", field)
                self.assertIn("單價（元／公升，0–100）", body)


class FuelPriceClientTests(unittest.TestCase):
    def test_both_forms_apply_inclusive_client_price_validation(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required to verify form JavaScript")
        templates = Path(__file__).resolve().parents[1] / "WinPython_公務電腦使用包" / "templates"
        for filename in ("new_task.html", "disaster_task.html"):
            with self.subTest(filename=filename):
                source = (templates / filename).read_text(encoding="utf-8")
                function = re.search(r"(?ms)^ +function fuelUnitPriceValid\(value\)\s*\{.*?^ +\}", source)
                self.assertIsNotNone(function)
                values = VALID_PRICES + INVALID_PRICES
                helper = re.search(r"(?ms)^ +function fuelDecimalValid\(value\)\s*\{.*?^ +\}", source)
                script = (helper.group(0) if helper else "") + function.group(0)
                script += "\nprocess.stdout.write(JSON.stringify(" + json.dumps(values) + ".map(fuelUnitPriceValid)));"
                output = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True, timeout=10)
                self.assertEqual([True] * len(VALID_PRICES) + [False] * len(INVALID_PRICES), json.loads(output.stdout))
                if filename == "new_task.html":
                    self.assertIn("fuelUnitPriceValid(unitPrice)", source)
                else:
                    self.assertIn("fuelUnitPriceValid(input.value)", source)


if __name__ == "__main__":
    unittest.main()
