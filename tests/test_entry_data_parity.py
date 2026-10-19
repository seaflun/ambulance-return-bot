from contextlib import contextmanager
from datetime import datetime
import io
import json
import os
import re
import unittest
from unittest import mock
from urllib.parse import urlsplit

import app as runtime
from ambulance_bot import sinposmart_backend as backend
from ambulance_bot.task_store import JsonTaskStore
from tests import test_disaster_roster_fire_day as roster_fixtures
from tests import test_web_app as web_fixtures


class EntryDataParityTests(unittest.TestCase):
    options = {
        "local": {"base_url": "http://local.test:8090", "environ_overrides": {"SERVER_PORT": "8090"}},
        "nas": {"base_url": "http://nas.test:8091", "environ_overrides": {"SERVER_PORT": "8091"}},
    }

    def setUp(self):
        self.fixture = web_fixtures.WebAppTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.roots = {"local": runtime.artifacts_dir, "nas": runtime.artifacts_dir / "nas-data"}
        self.roots["nas"].mkdir()
        self.stores = {"local": self.fixture.store, "nas": JsonTaskStore(self.roots["nas"] / "tasks")}
        for patch in (
            mock.patch.dict(os.environ, {"TASK_ENTRY_PORT": "8091", "TASK_ENTRY_HOSTS": "",
                "WORKER_SERVER_URL": "http://nas.test:8080", "WORKER_TOKEN": "isolated-data-parity-token-32-or-longer"}),
            mock.patch.object(runtime, "local_host_candidates", return_value={"local.test"}),
            mock.patch.object(runtime.urllib.request, "urlopen", side_effect=self.nas_transport),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.requests = []

    @contextmanager
    def surface(self, mode):
        with mock.patch.object(runtime, "artifacts_dir", self.roots[mode]), \
                mock.patch.object(runtime, "store", self.stores[mode]):
            yield

    def nas_transport(self, request, **kwargs):
        url = urlsplit(request.full_url)
        self.assertEqual("nas.test:8080", url.netloc)
        self.assertEqual("/worker/vehicle-settings", url.path)
        self.requests.append(request.full_url)
        with self.surface("nas"):
            response = runtime.app.test_client().get(url.path + ("?" + url.query if url.query else ""),
                base_url="http://nas.test:8080", headers={"X-Worker-Token": os.environ["WORKER_TOKEN"]})
        self.assertEqual(200, response.status_code)
        return io.BytesIO(response.data)

    def request(self, mode, method, path, **kwargs):
        with self.surface(mode):
            return getattr(self.fixture.client, method)(path, **self.options[mode], **kwargs)

    def add_mileage(self, mode, task_id, time, mileage):
        model = runtime.AmbulanceReturnRequest.from_dict({"task_id": task_id,
            "created_at": "2026-10-20T12:00:00", "service_type": "ems", "case_date": "2026/10/20",
            "case_time": time, "vehicle": "新坡95", "driver": "隔離員", "mileage": mileage})
        self.stores[mode].create(model)

    def test_local_and_nas_mileage_hints_include_cases_created_only_on_nas(self):
        self.add_mileage("local", "earlier-local", "0900", "100")
        self.add_mileage("nas", "earlier-nas", "0900", "100")
        self.add_mileage("nas", "latest-nas", "1100", "120")
        expected = self.request("nas", "get", "/api/vehicle-mileages")
        actual = self.request("local", "get", "/api/vehicle-mileages")
        self.assertEqual("120", expected.json["mileages"]["新坡95"])
        self.assertEqual(expected.json, actual.json)
        self.assertIn("no-store", actual.headers["Cache-Control"])
        serialized = json.dumps(actual.json)
        for private in ("task_id", "case_address", "driver", "隔離員", "latest-nas"):
            self.assertNotIn(private, serialized)

    def test_remote_mileage_history_respects_case_cutoff_and_later_corrections(self):
        self.add_mileage("local", "old-local", "0900", "110")
        self.add_mileage("nas", "corrected-nas", "0900", "100")
        self.add_mileage("nas", "later-nas", "1100", "120")
        with self.surface("local"), runtime.app.test_request_context(**self.options["local"]):
            settings = runtime.fetch_nas_vehicle_settings()
            actual = runtime.last_vehicle_mileages(nas_settings=settings, before=datetime(2026, 10, 20, 10))
        self.assertEqual("100", actual["新坡95"])

    def test_equal_minute_mileage_keeps_nas_latest_order_in_hint_history(self):
        self.add_mileage("local", "local-old", "0900", "110")
        self.add_mileage("nas", "nas-old", "0900", "90")
        self.add_mileage("nas", "nas-new", "0900", "100")
        payload = self.stores["nas"].get("nas-new")
        payload["updated_at"] = "2026-10-20T12:01:00"
        self.stores["nas"].save_payload("nas-new", payload)
        expected = self.request("nas", "get", "/api/vehicle-mileages").json
        actual = self.request("local", "get", "/api/vehicle-mileages").json
        self.assertEqual("100", expected["mileages"]["新坡95"])
        self.assertEqual(expected, actual)
        self.assertEqual("100", actual["history"]["新坡95"][0]["mileage"])

    def test_read_only_mileage_metadata_never_changes_shared_settings_revision(self):
        with self.surface("nas"), runtime.app.test_request_context(base_url="http://nas.test:8080"):
            before = runtime.vehicle_settings_api_payload()
        self.add_mileage("nas", "latest-metadata", "1100", "120")
        with self.surface("nas"), runtime.app.test_request_context(base_url="http://nas.test:8080"):
            after = runtime.vehicle_settings_api_payload()
        self.assertEqual(before["revision"], after["revision"])
        self.assertNotEqual(before["vehicle_mileage_history"], after["vehicle_mileage_history"])

    def test_mileage_metadata_filters_private_fields_and_malformed_rows(self):
        payload = {"新坡95": [{"time": "202610200900", "mileage": "100", "task_id": "private", "driver": "private"},
                              {"time": "202602300900", "mileage": "200"}, {"time": "bad", "mileage": "300"},
                              {"time": "202610201000", "mileage": "private"}]}
        self.assertEqual({"新坡95": [{"time": "202610200900", "mileage": "100"}]},
                         runtime.normalize_vehicle_mileage_hint_history(payload))

    def test_older_nas_without_history_keeps_local_fallback(self):
        self.add_mileage("local", "fallback-local", "0900", "100")
        with self.surface("nas"):
            older = runtime.vehicle_settings_api_payload()
        older.pop("vehicle_mileage_history")
        with mock.patch.object(runtime, "fetch_nas_vehicle_settings", return_value=older):
            payload = self.request("local", "get", "/api/vehicle-mileages").json
        self.assertEqual("100", payload["mileages"]["新坡95"])
        self.assertEqual("100", payload["history"]["新坡95"][0]["mileage"])

    def test_offline_nas_does_not_add_another_settings_timeout(self):
        self.add_mileage("local", "offline-local", "0900", "100")
        with mock.patch.object(runtime, "fetch_nas_vehicle_settings", return_value=None) as fetch:
            payload = self.request("local", "get", "/api/vehicle-mileages").json
        self.assertEqual("100", payload["mileages"]["新坡95"])
        self.assertLessEqual(fetch.call_count, 2, "Preserve the existing offline fallback request budget")

    def test_roster_date_query_keeps_worker_authentication_and_entry_isolation(self):
        with self.surface("nas"):
            for day in ("2026-10-19", "invalid", "../../outside"):
                with self.subTest(day=day):
                    path = "/worker/vehicle-settings?personnel_target_date=" + day
                    self.assertEqual(403, self.fixture.client.get(path, base_url="http://nas.test:8080").status_code)
                    self.assertEqual(403, self.request("nas", "get", path).status_code)
                    authorized = self.fixture.client.get(path, base_url="http://nas.test:8080",
                        headers={"X-Worker-Token": os.environ["WORKER_TOKEN"]})
                    self.assertEqual(200, authorized.status_code)
                    self.assertEqual({}, authorized.json["disaster_personnel_titles"])

    def test_previous_fire_day_roster_matches_on_local_and_nas_after_eight_am(self):
        clock = mock.Mock(wraps=datetime)
        clock.now.return_value = datetime(2026, 10, 20, 9, 0)
        for day, stamp in (("2026-10-19", "2026-10-19T22:00:00"), ("2026-10-20", "2026-10-20T08:30:00")):
            folder = self.roots["nas"] / "sinposmart"
            folder.mkdir(exist_ok=True)
            runtime.write_json_atomic(folder / (day + ".json"), {"events": [{
                "record_type": "schedule_snapshot", "occurred_at": stamp,
                "snapshot": {"personnel_titles": roster_fixtures.DisasterRosterFireDayTests.roster(day)}}]})
        case = {"case_id": "ISOLATED-PREVIOUS-DAY", "case_date": "2026/10/20", "case_time_hhmm": "0730",
            "address": "隔離跨消防日案件", "personnel": ["員十", "小四", "分長", "員二"]}
        titles = {}
        with mock.patch.object(runtime, "datetime", clock), mock.patch.object(backend, "datetime", clock):
            for mode in ("nas", "local"):
                runtime.write_json_atomic(self.roots[mode] / "cases/latest.json", {"status": "cases_loaded", "cases": [case]})
                imported = self.request(mode, "post", "/cases/import", data={"case_id": case["case_id"], "return_to": "disaster"})
                self.assertEqual(302, imported.status_code)
                body = self.request(mode, "get", "/app/disaster").get_data(as_text=True)
                match = re.search(r"const personnelTitles\s*=\s*(\{[^;]*\});", body)
                self.assertIsNotNone(match)
                titles[mode] = json.loads(match[1])
        self.assertEqual(4, len(titles["nas"]))
        self.assertEqual(titles["nas"], titles["local"])


class EntryMileageConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = web_fixtures.WebAppTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        environment = mock.patch.dict(os.environ, {"TASK_ENTRY_PORT": "8091", "TASK_ENTRY_HOSTS": "",
            "WORKER_TOKEN": "isolated-mileage-confirm-token-32-or-longer"})
        environment.start()
        self.addCleanup(environment.stop)

    def create_overlap(self, task_id):
        model = runtime.AmbulanceReturnRequest.from_dict({"task_id": task_id,
            "created_at": datetime.now().isoformat(), "service_type": "disaster",
            "vehicle_entries": [{"vehicle": "新坡11"}, {"vehicle": "新坡15"}]})
        self.fixture.store.create(model)
        for vehicle, status, detail in (("新坡11", "vehicle_mileage_failed", "案件與既有用車時間重疊，停止補登。"),
                                         ("新坡15", "vehicle_mileage_saved", "原成功")):
            self.fixture.store.update_site_result(task_id, runtime.SiteAutomationResult(
                "vehicle_mileage", "里程", status, detail), vehicle_key=vehicle)
        return self.fixture.store.get(task_id)

    def test_entry_confirmation_checks_ack_token_and_preserves_other_vehicle(self):
        for mode in ("local", "nas"):
            with self.subTest(mode=mode):
                task_id = "isolated-manual-" + mode
                original = self.create_overlap(task_id)
                path = "/tasks/" + task_id + "/sites/vehicle_mileage/complete"
                fields = {"vehicle_key": "新坡11", "confirmation_token":
                    runtime.site_manual_complete_token(task_id, "vehicle_mileage", "新坡11")}
                options = EntryDataParityTests.options[mode]
                self.assertEqual(409, self.fixture.client.post(path, data=fields, **options).status_code)
                self.assertEqual(403, self.fixture.client.post(path, data=dict(fields,
                    confirmed_existing_mileage="1", confirmation_token="bad"), **options).status_code)
                self.assertEqual(original, self.fixture.store.get(task_id))
                response = self.fixture.client.post(path, data=dict(fields, confirmed_existing_mileage="1"), **options)
                self.assertEqual(302, response.status_code)
                self.assertTrue(response.headers["Location"].endswith("/tasks/" + task_id))
                completed = self.fixture.store.get(task_id)["site_statuses"]["vehicle_mileage"]["vehicle_results"]
                self.assertEqual("completed_by_user", completed["新坡11"]["status"])
                self.assertEqual(original["site_statuses"]["vehicle_mileage"]["vehicle_results"]["新坡15"],
                                 completed["新坡15"])

    def test_running_task_and_missing_token_never_offer_overlap_confirmation(self):
        task_id = "isolated-busy-confirm"
        original = self.create_overlap(task_id)
        for mode in ("local", "nas"):
            options = EntryDataParityTests.options[mode]
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"WORKER_TOKEN": ""}):
                body = self.fixture.client.get("/tasks/" + task_id, **options).get_data(as_text=True)
                self.assertNotIn("確認新坡11既有里程", body)
            queued = self.fixture.store.queue_for_worker(task_id)
            body = self.fixture.client.get("/tasks/" + task_id, **options).get_data(as_text=True)
            self.assertNotIn("確認新坡11既有里程", body)
            path = "/tasks/" + task_id + "/sites/vehicle_mileage/complete"
            response = self.fixture.client.post(path, data={"vehicle_key": "新坡11", "confirmed_existing_mileage": "1",
                "confirmation_token": runtime.site_manual_complete_token(task_id, "vehicle_mileage", "新坡11")}, **options)
            self.assertEqual(409, response.status_code)
            self.assertEqual(queued, self.fixture.store.get(task_id))
            self.fixture.store.save_payload(task_id, original)

    def test_scoped_task_page_can_confirm_existing_mileage_for_exact_vehicle(self):
        for service, vehicles in (("ems", ["新坡95", "新坡92"]), ("disaster", ["新坡11", "新坡15"])):
            model = runtime.AmbulanceReturnRequest.from_dict({"task_id": "overlap-entry-" + service,
                "created_at": datetime.now().isoformat(), "service_type": service,
                "two_vehicle": service == "ems",
                "vehicle_entries": [{"vehicle": vehicle} for vehicle in vehicles]})
            self.fixture.store.create(model)
            for vehicle, status, detail in ((vehicles[0], "vehicle_mileage_failed", "案件與既有用車時間重疊，停止補登。"),
                                             (vehicles[1], "vehicle_mileage_saved", "原成功")):
                self.fixture.store.update_site_result(model.task_id, runtime.SiteAutomationResult(
                    "vehicle_mileage", "里程", status, detail), vehicle_key=vehicle)
            path = "/tasks/" + model.task_id
            for mode in ("local", "nas"):
                with self.subTest(service=service, mode=mode):
                    response = self.fixture.client.get(path, **EntryDataParityTests.options[mode])
                    self.assertEqual(200, response.status_code)
                    self.assertIn("確認" + vehicles[0] + "既有里程", response.get_data(as_text=True))
                    self.assertNotIn("確認" + vehicles[1] + "既有里程", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
