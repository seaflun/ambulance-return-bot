import copy
import os
import unittest
from unittest import mock

import app as app_module
from tests import test_disaster_personnel_order as form_helpers
from tests import test_web_app as web_fixtures


class DisasterPersonnelDisplayOptionsTests(unittest.TestCase):
    def test_three_display_orders_preserve_original_names_and_case_data(self):
        case = {"personnel": ["員甲", "長甲", "未知甲", "小甲", "員乙", "長乙", "未知乙"]}
        original = copy.deepcopy(case)
        titles = {"員甲": "隊員", "員乙": "隊員", "長甲": "分隊長", "長乙": "分隊長", "小甲": "小隊長"}
        settings = {"disaster_personnel_titles": {"complete": True}}
        with mock.patch.object(app_module, "effective_disaster_personnel_titles", return_value=titles) as read:
            result = app_module.disaster_personnel_display_options(case, settings)
        read.assert_called_once_with(case, settings)
        leaders = ["長甲", "長乙", "小甲", "員甲", "員乙", "未知甲", "未知乙"]
        drivers = ["員甲", "員乙", "小甲", "長甲", "長乙", "未知甲", "未知乙"]
        self.assertEqual([(name, name) for name in leaders], result["current_personnel_options"])
        self.assertEqual([(name, name) for name in leaders], result["firecam_person_options"])
        self.assertEqual([(name, name) for name in drivers], result["driver_person_options"])
        self.assertEqual(titles, result["personnel_titles"])
        self.assertEqual(original, case)

    def test_existing_person_option_values_and_labels_are_not_rewritten(self):
        case = {"personnel": "乙,甲", "person_options": [("乙", "乙原標籤"), ("甲", "甲原標籤")]}
        with mock.patch.object(app_module, "effective_disaster_personnel_titles", return_value={"甲": "分隊長", "乙": "隊員"}):
            result = app_module.disaster_personnel_display_options(case)
        self.assertEqual([("甲", "甲"), ("乙", "乙")], result["current_personnel_options"])
        self.assertEqual(case["person_options"], result["driver_person_options"])
        self.assertEqual(list(reversed(case["person_options"])), result["firecam_person_options"])

    def test_missing_or_unrecognized_titles_keep_stable_original_order(self):
        case = {"personnel": ["丙", "甲", "乙"]}
        for titles in ({}, {"丙": "副分隊長", "甲": "", "乙": "不明"}):
            with self.subTest(titles=titles), mock.patch.object(app_module, "effective_disaster_personnel_titles", return_value=titles):
                result = app_module.disaster_personnel_display_options(case)
            for field in ("current_personnel_options", "driver_person_options", "firecam_person_options"):
                self.assertEqual([("丙", "丙"), ("甲", "甲"), ("乙", "乙")], result[field])

    def test_empty_case_does_not_invent_personnel(self):
        with mock.patch.object(app_module, "effective_disaster_personnel_titles", return_value={}):
            result = app_module.disaster_personnel_display_options({})
        self.assertTrue(all(result[field] == [] for field in
                            ("current_personnel_options", "driver_person_options", "firecam_person_options")))


class DisasterPersonnelRouteOrderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = web_fixtures.WebAppTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client

    def test_original_and_dedicated_nas_routes_supply_all_three_display_orders(self):
        case = {"case_id": "isolated-personnel-route", "case_date": "2026/10/19", "case_time": "1200",
                "return_time": "1300", "personnel": ["隊甲", "分甲", "小甲"], "summary_type": "火災"}
        snapshot = {"schema_version": 1, "source": "official_duty_staff", "target_date": "2026-10-19",
                    "complete": True, "members": [
                        {"actor_no": "1", "name": "分甲", "title": "分隊長"},
                        {"actor_no": "2", "name": "小甲", "title": "小隊長"},
                        {"actor_no": "3", "name": "隊甲", "title": "隊員"}]}
        for port in ("8080", "8091"):
            with self.subTest(port=port), mock.patch.dict(os.environ, {"TASK_ENTRY_PORT": "8091"}), mock.patch.object(
                app_module, "read_personnel_title_snapshot", return_value=snapshot
            ):
                options = {"headers": {"Host": "100.114.126.58:" + port}, "environ_overrides": {"SERVER_PORT": port}}
                app_module.write_json_atomic(app_module.artifacts_dir / "cases/latest.json", {"status": "cases_loaded", "cases": [case]})
                imported = self.client.post("/cases/import", data={"case_id": case["case_id"], "return_to": "disaster"}, **options)
                self.assertEqual(302, imported.status_code)
                response = self.client.get("/app/disaster", **options)
                self.assertEqual(200, response.status_code)
                form = form_helpers.PersonnelFormParser(response.get_data(as_text=True))
                self.assertEqual(["分隊長 分甲", "小隊長 小甲", "隊員 隊甲"], [person["label"] for person in form.current])
                self.assertEqual(["分甲", "小甲", "隊甲"], [person["name"] for person in form.firecam])
                drivers = next(select for select in form.selects if select["name"] == "driver")
                self.assertEqual(["隊甲", "小甲", "分甲"], [option["value"] for option in drivers["options"][1:]])
                self.assertEqual("隊甲,分甲,小甲", form.hidden["personnel"])


if __name__ == "__main__":
    unittest.main()
