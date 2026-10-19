import html
import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from werkzeug.datastructures import MultiDict

import app as app_module
from tests import test_web_app as web_fixtures


class NasEntryConsoleTests(unittest.TestCase):
    host = {"Host": "entry.test"}

    def setUp(self):
        environment = mock.patch.dict(os.environ, {"TASK_ENTRY_HOSTS": "entry.test"})
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = web_fixtures.WebAppTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        self.store = self.fixture.store

    def test_dedicated_root_and_navigation_stay_in_entry_console(self):
        root = self.client.get("/", headers=self.host)
        self.assertEqual(302, root.status_code)
        self.assertEqual("/task-entry", root.headers["Location"])
        body = self.client.get(root.headers["Location"], headers=self.host).get_data(as_text=True)
        self.assertIn('href="/app/disaster"', body)
        self.assertIn('href="/app"', body)
        self.assertNotIn('href="/"', body)
        self.assertNotIn('href="/admin/', body)
        for path, settings_path in (("/app", "/admin/vehicles"), ("/app/disaster", "/admin/disaster-vehicles")):
            body = self.client.get(path, headers=self.host).get_data(as_text=True)
            self.assertIn(f'href="{settings_path}"', body)
            self.assertNotIn('href="/admin/sinposmart"', body)
            self.assertNotIn('href="/admin/public-pc"', body)
            self.assertNotIn('href="/"', body)
            self.assertIn('href="/task-entry"', body)

    def test_dedicated_host_blocks_admin_worker_and_other_routes(self):
        for path in ("/admin/sinposmart", "/admin/public-pc", "/admin/ems", "/admin/disaster",
                     "/updates", "/worker/identity", "/worker/next-task", "/status", "/artifacts/test.json"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.host)
                self.assertEqual(403, response.status_code)
                self.assertNotIn("SinpoSmart - 值班後台", response.get_data(as_text=True))
        response = self.client.post("/api/credential-sync", headers=self.host, json={})
        self.assertEqual(403, response.status_code)
        with self.client.get("/static/sinposmart-ui.css", headers=self.host) as response:
            self.assertEqual(200, response.status_code)

    def test_original_nas_and_worker_hosts_keep_existing_behavior(self):
        body = self.client.get("/", headers={"Host": "100.114.126.58:8080"}).get_data(as_text=True)
        self.assertIn('href="/admin/sinposmart"', body)
        with mock.patch.dict(os.environ, {"WORKER_TOKEN": "test-entry-worker"}):
            self.assertEqual(403, self.client.get("/worker/identity", headers={"Host": "100.114.126.58:8080"}).status_code)
            worker_headers = {"Host": "100.114.126.58:8080", "X-Worker-Token": "test-entry-worker"}
            self.assertEqual(200, self.client.get("/worker/identity", headers=worker_headers).status_code)
            self.assertEqual(403, self.client.get("/worker/identity", headers=dict(worker_headers, Host="entry.test")).status_code)
        self.assertEqual(200, self.client.get("/admin/ems", headers={"Host": "100.114.126.58:8080"}).status_code)

    def test_entry_browser_icon_probe_has_no_missing_resource_error(self):
        self.assertEqual(204, self.client.get("/favicon.ico", headers=self.host).status_code)
        self.assertEqual(204, self.client.head("/favicon.ico", headers=self.host).status_code)
        self.assertEqual(403, self.client.post("/favicon.ico", headers=self.host).status_code)

    def test_dedicated_socket_port_cannot_escape_with_another_host_header(self):
        with mock.patch.dict(os.environ, {"TASK_ENTRY_HOSTS": "", "TASK_ENTRY_PORT": "8091"}):
            headers = {"Host": "10.30.65.30:8080"}
            override = {"SERVER_PORT": "8091"}
            self.assertEqual(302, self.client.get("/", headers=headers, environ_overrides=override).status_code)
            self.assertEqual(403, self.client.get("/admin/sinposmart", headers=headers, environ_overrides=override).status_code)
            self.assertEqual(200, self.client.get("/", headers=headers, environ_overrides={"SERVER_PORT": "8080"}).status_code)

    def test_host_with_explicit_port_does_not_capture_original_management_port(self):
        with mock.patch.dict(os.environ, {"TASK_ENTRY_HOSTS": "10.30.65.30:8091", "TASK_ENTRY_PORT": ""}):
            self.assertEqual(302, self.client.get("/", headers={"Host": "10.30.65.30:8091"}).status_code)
            self.assertEqual(200, self.client.get("/", headers={"Host": "10.30.65.30:8080"}).status_code)

    def test_worker_receives_validated_entry_configuration_on_original_host_only(self):
        worker_headers = {"Host": "10.30.65.30:8080", "X-Worker-Token": "test-entry-worker"}
        with mock.patch.dict(os.environ, {"WORKER_TOKEN": "test-entry-worker", "TASK_ENTRY_PORT": "8091",
                                         "TASK_ENTRY_URL": "http://10.30.65.30:8091/", "TASK_ENTRY_LAN_IP": "10.30.65.30"}):
            self.assertEqual(403, self.client.get("/worker/task-entry-settings", headers={"Host": "10.30.65.30:8080"}).status_code)
            response = self.client.get("/worker/task-entry-settings", headers=worker_headers)
            self.assertEqual(200, response.status_code)
            self.assertEqual("configured", response.json["task_entry"]["state"])
            self.assertEqual("http://10.30.65.30:8091/", response.json["task_entry"]["url"])
            self.assertTrue(response.json["instance_id"])
            self.assertNotIn("test-entry-worker", response.get_data(as_text=True))
            self.assertEqual(403, self.client.get("/worker/task-entry-settings", headers=dict(worker_headers, Host="entry.test")).status_code)

    def test_worker_configuration_rejects_urls_that_do_not_use_an_entry_listener_or_host(self):
        with mock.patch.dict(os.environ, {"WORKER_TOKEN": "test-entry-worker", "TASK_ENTRY_PORT": "8091"}):
            for url in ("http://10.30.65.30:8080/", "http://evil.test:8091/", "javascript:alert(1)", "https://user:secret@entry.test/", "http://entry.test/admin/ems"):
                with self.subTest(url=url), mock.patch.dict(os.environ, {"TASK_ENTRY_URL": url}):
                    response = self.client.get("/worker/task-entry-settings", headers={"Host": "10.30.65.30:8080", "X-Worker-Token": "test-entry-worker"})
                    self.assertEqual(200, response.status_code)
                    self.assertEqual("invalid", response.json["task_entry"]["state"])
                    self.assertEqual("", response.json["task_entry"]["url"])

    def test_worker_configuration_disabled_and_auto_port_url(self):
        headers = {"Host": "10.30.65.30:8080", "X-Worker-Token": "test-entry-worker"}
        with mock.patch.dict(os.environ, {"WORKER_TOKEN": "test-entry-worker", "TASK_ENTRY_HOSTS": "", "TASK_ENTRY_PORT": "", "TASK_ENTRY_URL": ""}):
            self.assertEqual("disabled", self.client.get("/worker/task-entry-settings", headers=headers).json["task_entry"]["state"])
            with mock.patch.dict(os.environ, {"TASK_ENTRY_PORT": "8091"}):
                settings = self.client.get("/worker/task-entry-settings", headers=headers).json["task_entry"]
                self.assertEqual("configured", settings["state"])
                self.assertEqual("http://10.30.65.30:8091/", settings["url"])

    def test_same_browser_legacy_and_dedicated_ports_keep_separate_imports(self):
        self.write_cases()
        original_host = {"Host": "10.30.65.30:8080"}
        entry_host = {"Host": "10.30.65.30:8091"}
        with mock.patch.dict(os.environ, {"TASK_ENTRY_HOSTS": "", "TASK_ENTRY_PORT": "8091"}):
            self.client.post("/cases/import", headers=original_host, data={"case_id": "case-A"})
            self.client.post("/cases/import", headers=entry_host, environ_overrides={"SERVER_PORT": "8091"}, data={"case_id": "case-B"})
            legacy = self.client.get("/app", headers=original_host).get_data(as_text=True)
            dedicated = self.client.get("/app", headers=entry_host, environ_overrides={"SERVER_PORT": "8091"}).get_data(as_text=True)
        self.assertTrue('value="甲案件地址"' in legacy, "Original port lost its case selection")
        self.assertTrue('value="乙案件地址"' in dedicated, "Entry port lost its case selection")

    def test_web_start_uses_two_listeners_in_one_waitress_process(self):
        with mock.patch.dict(os.environ, {"TASK_ENTRY_PORT": "8091", "PUBLIC_PC_REPORT_ENABLED": "false"}), \
                mock.patch("waitress.serve") as serve, \
                mock.patch.object(app_module, "credential_sync_record_for_worker"), \
                mock.patch.object(app_module, "start_task_auto_start_scheduler", return_value=(mock.Mock(), mock.Mock())):
            app_module.run_web_app(host="127.0.0.1", port=8080)
        self.assertEqual(1, serve.call_count)
        self.assertEqual("127.0.0.1:8080 127.0.0.1:8091", serve.call_args.kwargs["listen"])
        self.assertNotIn("host", serve.call_args.kwargs)

    def test_web_start_rejects_invalid_or_colliding_entry_port(self):
        for value in ("8080", "0", "65536", "bad"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TASK_ENTRY_PORT": value}), \
                    mock.patch("waitress.serve") as serve, \
                    mock.patch.object(app_module, "credential_sync_record_for_worker"), \
                    mock.patch.object(app_module, "start_task_auto_start_scheduler", return_value=(mock.Mock(), mock.Mock())):
                with self.assertRaises(ValueError):
                    app_module.run_web_app(host="127.0.0.1", port=8080)
                serve.assert_not_called()

    def test_entry_host_forces_queue_even_when_local_fast_mode_is_enabled(self):
        with mock.patch.dict(os.environ, {"DESKTOP_FAST_MODE": "true"}), \
                mock.patch.object(app_module, "local_host_candidates", return_value={"entry.test"}):
            with app_module.app.test_request_context("/app", headers=self.host):
                self.assertFalse(app_module.request_is_local_host())
                self.assertEqual("worker_queue", app_module.effective_task_execution_mode())
            response = self.client.post("/tasks", headers=self.host, data=self.fixture.valid_task_data())
        self.assertEqual(302, response.status_code)
        task_id = response.headers["Location"].split("/")[-1]
        self.assertEqual("queued", self.store.get(task_id)["worker_queue"]["status"])
        self.assertEqual([], app_module.desktop_runner.started)

    def write_cases(self):
        app_module.write_json_atomic(app_module.artifacts_dir / "cases" / "latest.json", {
            "status": "cases_loaded", "cases": [
                {"case_id": "case-A", "address": "甲案件地址", "case_time_hhmm": "0905"},
                {"case_id": "case-B", "address": "乙案件地址", "case_time_hhmm": "1010"},
            ],
        })

    def test_two_browsers_interleaved_imports_do_not_mix_or_consume_each_other(self):
        self.write_cases()
        other = app_module.app.test_client()
        bystander = app_module.app.test_client()
        a = self.client.post("/cases/import", headers=self.host, data={"case_id": "case-A"})
        b = other.post("/cases/import", headers=self.host, data={"case_id": "case-B"})
        self.assertEqual(302, a.status_code)
        self.assertEqual(302, b.status_code)
        self.assertNotIn("case-A", a.headers.get("Set-Cookie", ""))
        self.assertNotIn("甲案件地址", a.headers.get("Set-Cookie", ""))
        self.assertEqual(200, bystander.get("/app", headers=self.host).status_code)
        body_b = other.get("/app", headers=self.host).get_data(as_text=True)
        body_a = self.client.get("/app", headers=self.host).get_data(as_text=True)
        self.assertIn('value="甲案件地址"', body_a)
        self.assertNotIn('value="乙案件地址"', body_a)
        self.assertIn('value="乙案件地址"', body_b)
        self.assertNotIn('value="甲案件地址"', body_b)
        self.assertNotIn('value="甲案件地址"', self.client.get("/app", headers=self.host).get_data(as_text=True))

    def test_clearing_one_browser_does_not_clear_another_pending_import(self):
        self.write_cases()
        other = app_module.app.test_client()
        self.client.post("/cases/import", headers=self.host, data={"case_id": "case-A"})
        other.post("/cases/import", headers=self.host, data={"case_id": "case-B"})
        self.client.post("/cases/clear", headers=self.host)
        body = other.get("/app", headers=self.host).get_data(as_text=True)
        self.assertIn('value="乙案件地址"', body)

    def test_concurrent_same_ems_case_is_created_and_queued_once(self):
        original_create = self.store.create
        def slow_create(*args, **kwargs):
            time.sleep(.05)
            return original_create(*args, **kwargs)
        barrier = threading.Barrier(2)
        def submit():
            client = app_module.app.test_client()
            barrier.wait(timeout=3)
            return client.post("/tasks", headers=self.host, data=self.fixture.valid_task_data()).headers["Location"]
        with mock.patch.object(self.store, "create", side_effect=slow_create):
            with ThreadPoolExecutor(max_workers=2) as pool:
                locations = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(locations[0], locations[1])
        self.assertEqual(1, len(self.store.list_recent(limit=10)))
        self.assertEqual("queued", self.store.list_recent(limit=10)[0]["worker_queue"]["status"])

    def test_same_case_for_distinct_ems_vehicles_still_creates_distinct_tasks(self):
        a = self.client.post("/tasks", headers=self.host, data=self.fixture.valid_task_data(vehicle="新坡92"))
        b = self.client.post("/tasks", headers=self.host, data=self.fixture.valid_task_data(vehicle="新坡95"))
        self.assertEqual(302, a.status_code)
        self.assertEqual(302, b.status_code)
        self.assertNotEqual(a.headers["Location"], b.headers["Location"])
        self.assertEqual(2, len(self.store.list_recent()))

    def test_concurrent_same_disaster_case_builds_folders_and_queues_once(self):
        data = MultiDict([
            ("case_id", "FIRE-NAS"), ("case_date", "2026/07/22"), ("case_time", "1207"),
            ("return_time", "1300"), ("case_address", "測試救災地址"),
            ("summary_type", "火災"), ("case_reason", "一般(集合)住宅"),
            ("personnel", "甲,乙"), ("commander", "乙"), ("action_note", "現場待命"),
            ("recorder_category", "轄內A3"), ("vehicle", "新坡11"),
            ("driver", "甲"), ("vehicle_return_time", "1300"), ("mileage", "100"),
        ])
        barrier = threading.Barrier(2)
        def folders(*args, **kwargs):
            time.sleep(.05)
            return []
        def submit():
            client = app_module.app.test_client()
            barrier.wait(timeout=3)
            response = client.post("/tasks/disaster", headers=self.host, data=data.copy())
            self.assertEqual(302, response.status_code)
            return response.headers["Location"]
        with mock.patch.object(app_module, "ensure_disaster_media_folders", side_effect=folders) as folder_call:
            with ThreadPoolExecutor(max_workers=2) as pool:
                locations = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(locations[0], locations[1])
        self.assertEqual(1, folder_call.call_count)
        self.assertEqual(1, len(self.store.list_recent()))
        self.assertEqual("queued", self.store.list_recent()[0]["worker_queue"]["status"])

    def test_entry_tasks_remain_queued_until_authenticated_worker_claims_them(self):
        a = self.client.post("/tasks", headers=self.host, data=self.fixture.valid_task_data(case_id="queue-A"))
        b = self.client.post("/tasks", headers=self.host, data=self.fixture.valid_task_data(case_id="queue-B"))
        a_id = a.headers["Location"].split("/")[-1]
        b_id = b.headers["Location"].split("/")[-1]
        self.assertEqual("queued", self.store.get(a_id)["worker_queue"]["status"])
        self.assertEqual("queued", self.store.get(b_id)["worker_queue"]["status"])
        with mock.patch.dict(os.environ, {"WORKER_TOKEN": "test-entry-worker"}):
            headers = {"Host": "100.114.126.58:8080", "X-Worker-Token": "test-entry-worker"}
            claimed = self.client.get("/worker/next-task?worker_id=test-entry-worker", headers=headers)
        self.assertEqual(200, claimed.status_code)
        claimed_id = claimed.json["task"]["task_id"]
        other_id = b_id if claimed_id == a_id else a_id
        self.assertEqual("claimed", self.store.get(claimed_id)["worker_queue"]["status"])
        self.assertEqual("queued", self.store.get(other_id)["worker_queue"]["status"])
        self.assertEqual([], app_module.desktop_runner.started)

    def test_single_site_retry_queues_only_failed_site_and_detail_stays_in_console(self):
        request = app_module.request_from_form(self.fixture.valid_task_data())
        self.store.create(request)
        self.store.update_site_result(request.task_id, app_module.SiteAutomationResult(
            "disinfection", "消毒", "disinfection_failed", "測試錯誤"))
        response = self.client.post(f"/tasks/{request.task_id}/sites/disinfection/run", headers=self.host)
        self.assertEqual(302, response.status_code)
        payload = self.store.get(request.task_id)
        self.assertEqual("queued", payload["worker_queue"]["status"])
        self.assertEqual("disinfection", payload["worker_queue"]["run_site_key"])
        body = self.client.get(response.headers["Location"], headers=self.host).get_data(as_text=True)
        self.assertNotIn('href="/admin/', body)
        self.assertNotIn('href="/"', body)
        self.assertEqual([], app_module.desktop_runner.started_sites)


if __name__ == "__main__":
    unittest.main()
