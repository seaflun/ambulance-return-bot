import json
import os
import tempfile
import unittest
from pathlib import Path

from ambulance_bot.selenium_local import (
    _attach_case_form_details,
    _previous_case_details,
    _resolve_end_mileage,
    _write_json_atomic,
    selenium_enabled,
)


class SeleniumLocalTests(unittest.TestCase):
    def test_selenium_enabled_default_and_false(self):
        os.environ.pop("USE_LOCAL_SELENIUM", None)
        self.assertTrue(selenium_enabled())
        os.environ["USE_LOCAL_SELENIUM"] = "false"
        self.assertFalse(selenium_enabled())

    def test_resolve_end_mileage_accepts_delta(self):
        self.assertEqual(_resolve_end_mileage("123400", "+50"), "123450")
        self.assertEqual(_resolve_end_mileage("123400", "123456"), "123456")

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
