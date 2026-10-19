import html
import os
import unittest
from html.parser import HTMLParser
from unittest import mock

import app as app_module
from tests import test_web_app as web_fixtures


class PageElements(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.hrefs = []
        self.option_values = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href")
        if tag == "option":
            self.option_values.extend(value for key, value in attrs if key == "value")


class NasEntryVehicleSettingsTests(unittest.TestCase):
    original = {"headers": {"Host": "100.114.126.58:8080"}, "environ_overrides": {"SERVER_PORT": "8080"}}
    entry_modes = (
        ("dedicated_host", {"headers": {"Host": "entry.test"}, "environ_overrides": {"SERVER_PORT": "8080"}}),
        ("socket_port_with_forged_host", {"headers": {"Host": "100.114.126.58:8080"}, "environ_overrides": {"SERVER_PORT": "8091"}}),
    )

    def setUp(self):
        environment = mock.patch.dict(os.environ, {"TASK_ENTRY_HOSTS": "entry.test", "TASK_ENTRY_PORT": "8091"})
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = web_fixtures.WebAppTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client

    def worker_settings(self):
        headers = {**self.original["headers"], "X-Worker-Token": "test-entry-worker"}
        with mock.patch.dict(os.environ, {"WORKER_TOKEN": "test-entry-worker"}):
            response = self.client.get("/worker/vehicle-settings", headers=headers,
                                       environ_overrides=self.original["environ_overrides"])
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json["revision"])
        return response.json

    def form_vehicle_options(self, form_path, options):
        service = "disaster" if form_path == "/app/disaster" else "ems"
        case = {"case_id": f"isolated-settings-{service}", "case_date": "2026/10/19",
                "case_time": "0905", "address": "隔離測試案件地址", "personnel": ["甲"]}
        app_module.write_json_atomic(app_module.artifacts_dir / "cases" / "latest.json",
                                     {"status": "cases_loaded", "cases": [case]})
        imported = self.client.post("/cases/import", data={"case_id": case["case_id"], "return_to": service}, **options)
        self.assertEqual(302, imported.status_code)
        form = self.client.get(form_path, **options)
        self.assertEqual(200, form.status_code)
        return PageElements(form.get_data(as_text=True)).option_values

    def test_both_entry_forms_keep_their_existing_settings_button(self):
        for mode, options in self.entry_modes:
            for form, settings, label in (("/app", "/admin/vehicles", "救護各項設定"),
                                          ("/app/disaster", "/admin/disaster-vehicles", "救災各項設定")):
                with self.subTest(mode=mode, form=form):
                    response = self.client.get(form, **options)
                    self.assertEqual(200, response.status_code)
                    body = response.get_data(as_text=True)
                    self.assertIn(settings, PageElements(body).hrefs)
                    self.assertIn(label, body)

    def test_settings_pages_return_to_the_correct_form_without_admin_or_home_links(self):
        for mode, options in self.entry_modes:
            for settings, form in (("/admin/vehicles", "/app"), ("/admin/disaster-vehicles", "/app/disaster")):
                with self.subTest(mode=mode, settings=settings):
                    response = self.client.get(settings, **options)
                    self.assertEqual(200, response.status_code)
                    links = PageElements(response.get_data(as_text=True)).hrefs
                    self.assertIn(form, links)
                    self.assertNotIn("/", links)
                    self.assertFalse(any(link.startswith("/admin/") for link in links), links)

    def test_custom_ems_settings_are_shared_bidirectionally_with_original_port_and_worker(self):
        self.assert_shared_vehicle_settings(
            settings_path="/admin/vehicles", form_path="/app", record_key="ems_vehicles",
            label="共用救護71", extra_fields={},
        )

    def test_custom_disaster_settings_are_shared_bidirectionally_with_original_port_and_worker(self):
        self.assert_shared_vehicle_settings(
            settings_path="/admin/disaster-vehicles", form_path="/app/disaster", record_key="disaster_vehicles",
            label="共用救災72", extra_fields={"recorder_code": "TEST-CAM72"},
        )

    def assert_shared_vehicle_settings(self, *, settings_path, form_path, record_key, label, extra_fields):
        for mode, options in self.entry_modes:
            with self.subTest(mode=mode):
                initial = {"label": label, "ppe_name": "TEST-ENTRY-A", **extra_fields}
                saved = self.client.post(settings_path, data=initial, **options)
                self.assertEqual(200, saved.status_code)
                original_page = self.client.get(settings_path, **self.original)
                self.assertEqual(200, original_page.status_code)
                body = html.unescape(original_page.get_data(as_text=True))
                self.assertIn(label, body)
                self.assertIn("TEST-ENTRY-A", body)
                self.assertIn(label, self.form_vehicle_options(form_path, self.original))
                before = self.worker_settings()
                record = next(item for item in before[record_key] if item["label"] == label)
                self.assertEqual("TEST-ENTRY-A", record["ppe_name"])
                self.assertEqual("自訂", record["vehicle_type"])
                if extra_fields:
                    self.assertEqual(extra_fields["recorder_code"], record["recorder_code"])

                updated = self.client.post(settings_path, data={**initial, "ppe_name": "TEST-ORIGINAL-B"}, **self.original)
                self.assertEqual(200, updated.status_code)
                entry_page = self.client.get(settings_path, **options)
                self.assertEqual(200, entry_page.status_code)
                body = html.unescape(entry_page.get_data(as_text=True))
                self.assertIn("TEST-ORIGINAL-B", body)
                self.assertNotIn("TEST-ENTRY-A", body)
                self.assertIn(label, self.form_vehicle_options(form_path, options))
                after = self.worker_settings()
                record = next(item for item in after[record_key] if item["label"] == label)
                self.assertEqual("TEST-ORIGINAL-B", record["ppe_name"])
                self.assertNotEqual(before["revision"], after["revision"])
                self.assertEqual(403, self.client.get("/worker/vehicle-settings", **options).status_code)

    def test_custom_vehicle_delete_actions_reach_existing_storage(self):
        for mode, options in self.entry_modes:
            for path, label, save, load in (
                ("/admin/vehicles/delete", "測試救護73", app_module.save_vehicle_record, app_module.load_vehicle_records),
                ("/admin/disaster-vehicles/delete", "測試救災74", app_module.save_disaster_vehicle_record, app_module.load_disaster_vehicle_records),
            ):
                with self.subTest(mode=mode, path=path):
                    if "disaster" in path:
                        save(label, "TEST-74", "TEST-CAM74", app_module.artifacts_dir)
                    else:
                        save(label, "TEST-73", app_module.artifacts_dir)
                    response = self.client.post(path, data={"label": label}, **options)
                    self.assertEqual(200, response.status_code)
                    self.assertNotIn(label, [record["label"] for record in load(app_module.artifacts_dir)])

    def test_manual_mileage_actions_reach_existing_handler(self):
        for mode, options in self.entry_modes:
            for service, path in (("ems", "/admin/vehicles/mileage-query"),
                                  ("disaster", "/admin/disaster-vehicles/mileage-query")):
                with self.subTest(mode=mode, path=path), mock.patch.object(app_module, "submit_manual_vehicle_mileage_query", return_value="isolated-mileage-query") as submit:
                    response = self.client.post(path, data={"ppe_name": "TEST-75"}, **options)
                    self.assertEqual(200, response.status_code)
                    self.assertEqual("isolated-mileage-query", response.get_data(as_text=True))
                    submit.assert_called_once_with(service, "TEST-75")

    def test_disaster_action_packages_save_to_shared_settings(self):
        for mode, options in self.entry_modes:
            with self.subTest(mode=mode):
                response = self.client.post("/admin/disaster-vehicles/action-packages", data={"action_packages": "測試套餐甲\n測試套餐乙"}, **options)
                self.assertEqual(200, response.status_code)
                self.assertEqual(["測試套餐甲", "測試套餐乙"], app_module.load_disaster_action_packages(app_module.artifacts_dir))
                body = self.client.get("/admin/disaster-vehicles", **self.original).get_data(as_text=True)
                self.assertIn("測試套餐甲", body)
                self.assertIn("測試套餐乙", body)

    def test_civilpower_preferences_and_roster_query_reach_existing_handlers(self):
        for mode, options in self.entry_modes:
            with self.subTest(mode=mode), mock.patch.object(app_module, "save_civilpower_frequent_members", return_value=["member-1"]) as save:
                response = self.client.post("/admin/civilpower-preferences", data={"frequent_civilpower_member_ids": ["member-1"]}, **options)
                self.assertEqual(200, response.status_code)
                save.assert_called_once_with(["member-1"])
            with self.subTest(mode=mode), mock.patch.object(app_module, "submit_manual_civilpower_roster_query", return_value="isolated-roster-query") as query:
                response = self.client.post("/admin/civilpower-roster/query", **options)
                self.assertEqual(200, response.status_code)
                self.assertEqual("isolated-roster-query", response.get_data(as_text=True))
                query.assert_called_once_with()

    def test_unrelated_admin_worker_and_update_routes_remain_forbidden(self):
        get_paths = ("/admin/sinposmart", "/admin/public-pc", "/admin/ems", "/admin/disaster", "/updates", "/status",
                     "/worker/identity", "/worker/vehicle-settings", "/worker/task-entry-settings", "/worker/next-task",
                     "/admin/vehicles/unrelated", "/admin/disaster-vehicles/unrelated")
        post_paths = ("/admin/public-pc/remote-update", "/admin/sinposmart/remote-update", "/worker/control", "/api/credential-sync",
                      "/admin/vehicle-settings/pending/delete")
        for mode, options in self.entry_modes:
            for path in get_paths:
                with self.subTest(mode=mode, method="GET", path=path):
                    self.assertEqual(403, self.client.get(path, **options).status_code)
            for path in post_paths:
                with self.subTest(mode=mode, method="POST", path=path):
                    self.assertEqual(403, self.client.post(path, data={}, **options).status_code)
            root = self.client.get("/", **options)
            self.assertEqual(302, root.status_code)
            self.assertEqual("/task-entry", root.headers["Location"])


if __name__ == "__main__":
    unittest.main()
