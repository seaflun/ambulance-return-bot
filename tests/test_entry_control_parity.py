"""Compare actionable controls and the dedicated entry allowlist with local pages."""
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
from html.parser import HTMLParser
import json
import os
import unittest
from unittest import mock

import app as runtime
from tests import test_web_app as web_fixtures


class Controls(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.nodes = []
        self.stack = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parents = [self.nodes[index] for index in self.stack]
        node = {"tag": tag, "attrs": attrs, "text": "", "hidden": "hidden" in attrs or any(p["hidden"] for p in parents),
                "form": next((p["attrs"] for p in reversed(parents) if p["tag"] == "form"), {})}
        self.nodes.append(node)
        if tag not in {"input", "br", "meta", "link", "img", "hr", "source", "wbr"}:
            self.stack.append(len(self.nodes) - 1)

    def handle_endtag(self, tag):
        for position in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[position]]["tag"] == tag:
                self.stack = self.stack[:position]
                return

    def handle_data(self, text):
        for index in self.stack:
            self.nodes[index]["text"] += text

    def records(self):
        records = []
        for node in self.nodes:
            tag, attrs, form = node["tag"], node["attrs"], node["form"]
            if tag not in {"a", "button", "input", "select", "textarea", "summary"}:
                continue
            item = {"tag": tag, "id": attrs.get("id", ""), "name": attrs.get("name", ""),
                    "type": attrs.get("type", ""), "hidden": node["hidden"], "disabled": "disabled" in attrs,
                    "action": attrs.get("href", attrs.get("formaction", form.get("action", ""))),
                    "method": attrs.get("formmethod", form.get("method", "get")).lower()}
            if tag in {"a", "button", "summary"}:
                item["label"] = " ".join(node["text"].split())
            if attrs.get("type") in {"checkbox", "radio"}:
                item.update(value=attrs.get("value", ""), checked="checked" in attrs)
            records.append(item)
        return records



class EntryControlParityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = web_fixtures.WebAppTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {"TASK_ENTRY_PORT": "8091", "TASK_ENTRY_HOSTS": "",
            "DESKTOP_FAST_MODE": "auto", "WORKER_TOKEN": "isolated-audit-worker-token-32-or-longer"}))
        self.stack.enter_context(mock.patch.object(runtime, "local_host_candidates", return_value={"local.test"}))
        self.stack.enter_context(mock.patch.object(runtime, "request_nas_vehicle_settings", side_effect=lambda **kw: {
            "state": "ok", "settings": runtime.vehicle_settings_api_payload()}))
        self.stack.enter_context(mock.patch.object(runtime, "request_nas_civilpower_roster", side_effect=runtime.read_civilpower_roster))
        self.stack.enter_context(mock.patch.object(runtime, "refresh_stale_running_task", side_effect=lambda payload: payload))
        self.options = {"local": {"base_url": "http://local.test:8090", "environ_overrides": {"SERVER_PORT": "8090"}},
                        "entry": {"base_url": "http://nas.test:8091", "environ_overrides": {"SERVER_PORT": "8091"}}}

    def collect(self, output=None):
        results = []

        def compare(name, path, case=None, return_to="ems"):
            surfaces = {}
            for mode, options in self.options.items():
                client = runtime.app.test_client()
                if case:
                    runtime.write_json_atomic(runtime.artifacts_dir / "cases/latest.json", {"cases": [case], "status": "cases_loaded"})
                    imported = client.post("/cases/import", data={"case_id": case["case_id"], "return_to": return_to}, **options)
                    self.assertEqual(302, imported.status_code, (name, mode))
                response = client.get(path, **options)
                self.assertIn(response.status_code, (200, 409), (name, mode))
                source = response.get_data(as_text=True)
                if output:
                    (output / (name + "-" + mode + ".html")).write_text(source, encoding="utf-8")
                rows = Controls(source).records()
                surfaces[mode] = {"status": response.status_code, "controls": rows,
                    "counts": Counter(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)}
            missing = [json.loads(row) for row in (surfaces["local"]["counts"] - surfaces["entry"]["counts"]).elements()]
            extra = [json.loads(row) for row in (surfaces["entry"]["counts"] - surfaces["local"]["counts"]).elements()]
            blocked = []
            for row in surfaces["entry"]["controls"]:
                action = row["action"]
                if not action.startswith("/") or action.startswith("/static/"):
                    continue
                method = row["method"] if row["tag"] not in {"a", "summary"} else "get"
                with runtime.app.test_request_context(action, method=method.upper(), **self.options["entry"]):
                    try:
                        runtime.restrict_task_entry_console()
                        self.assertIsNotNone(runtime.request.url_rule, (name, action))
                    except Exception as exc:
                        blocked.append({"action": action, "method": method, "status": getattr(exc, "code", str(exc))})
            record = {"scenario": name, "path": path, "local_count": len(surfaces["local"]["controls"]),
                "entry_count": len(surfaces["entry"]["controls"]), "missing": missing, "extra": extra,
                "blocked": blocked, "status": {mode: data["status"] for mode, data in surfaces.items()},
                "controls": surfaces["entry"]["controls"]}
            results.append(record)

        for label, path in (("menu", "/task-entry"), ("ems_form", "/app"), ("disaster_form", "/app/disaster"),
                            ("ems_settings", "/admin/vehicles"), ("disaster_settings", "/admin/disaster-vehicles")):
            compare(label, path)
        for state in ("pending", "running", "failed", "completed"):
            commands = [{"request_id": "isolated-query-" + state + str(i), "kind": kind, "vehicle_key": vehicle,
                "status": state, "requested_at": datetime.now().isoformat(timespec="seconds"),
                "completed_at": "", "detail": "隔離查詢 " + state} for i, (kind, vehicle) in enumerate([
                    (runtime.MANUAL_REFRESH_KIND_VEHICLE_MILEAGE, "新坡95"),
                    (runtime.MANUAL_REFRESH_KIND_VEHICLE_MILEAGE, "新坡11"),
                    (runtime.MANUAL_REFRESH_KIND_CIVILPOWER_ROSTER, "")])]
            with runtime._manual_refresh_lock:
                runtime._write_manual_refresh_commands_unlocked(commands)
            compare("ems_settings_query_" + state, "/admin/vehicles")
            compare("disaster_settings_query_" + state, "/admin/disaster-vehicles")
        with runtime._manual_refresh_lock:
            runtime._write_manual_refresh_commands_unlocked([])
        case = {"case_id": "ISOLATED-RICH-CASE", "case_date": datetime.now().strftime("%Y/%m/%d"),
            "case_time_hhmm": "0911", "category": "緊急救護-急病", "address": "隔離功能比對地址",
            "personnel": ["甲", "乙", "丙", "丁"]}
        compare("ems_imported_two_vehicle", "/app", case=case)
        compare("disaster_imported", "/app/disaster", case=case, return_to="disaster")
        for service in ("ems", "disaster"):
            for rich in (False, True):
                states = ("initial", "confirmed", "queued", "running", "failed", "waiting", "saved", "needs_update", "aborted")
                if rich:
                    states += ("overlap", "candidate")
                for state in states:
                    task_id = "ISOLATED-" + service + ("-multi-" if rich else "-single-") + state
                    task = runtime.request_from_form(self.fixture.valid_task_data(case_id=task_id,
                        driver="隔離員", case_address="隔離功能比對地址")).to_dict()
                    task.update(task_id=task_id, service_type=service)
                    if service == "disaster":
                        task.update(vehicle="新坡11", summary_type="火災", vehicle_entries=[])
                    if rich:
                        vehicles = ["新坡95", "新坡92"] if service == "ems" else ["新坡11", "新坡15", "新坡16"]
                        task.update(two_vehicle=service == "ems", vehicle_entries=[{"vehicle": vehicle,
                            "driver": "甲乙丙"[i], "mileage": str(120 + i), "fuel_record": {"enabled": True, "date": "2026-06-07", "time": "1200", "quantity": "10", "unit_price": "30"}}
                            for i, vehicle in enumerate(vehicles)])
                        if service == "ems":
                            task.update(volunteer_assist=True, volunteer_assist_member_name="隔離義消",
                                        volunteer_assist_member_title="隊員", volunteer_assist_member_unit="大園救護分隊")
                    model = runtime.AmbulanceReturnRequest.from_dict(task)
                    payload = self.fixture.store.create(model)
                    payload["ems_case_closed_confirmed"] = state != "initial"
                    if state == "queued":
                        payload = self.fixture.store.queue_for_worker(model.task_id)
                    elif state == "running":
                        payload["overall_status"] = "desktop_fast_running"
                        payload["site_statuses"]["duty_work_log"].update(status="duty_work_log_running", detail="隔離進行中")
                    elif state == "aborted":
                        payload["overall_status"] = "task_aborted"
                    elif state not in {"initial", "confirmed", "overlap", "candidate"}:
                        suffix = {"failed": "failed", "waiting": "waiting_confirmation", "saved": "saved", "needs_update": "needs_update"}[state]
                        for key in model.active_site_keys():
                            payload["site_statuses"][key].update(status=key + "_" + suffix, detail="隔離狀態 " + state)
                    self.fixture.store.save_payload(model.task_id, payload)
                    if state in {"overlap", "candidate"}:
                        site_key = "vehicle_mileage" if state == "overlap" else "consumables"
                        vehicle = task["vehicle_entries"][0]["vehicle"]
                        for remaining in task["vehicle_entries"][1:]:
                            self.fixture.store.update_site_result(task_id, runtime.SiteAutomationResult(
                                site_key, "隔離站別", site_key + "_saved", "原成功"), vehicle_key=remaining["vehicle"])
                        result = (runtime.SiteAutomationResult(site_key, "里程", "vehicle_mileage_failed", "案件與既有用車時間重疊，停止補登。")
                            if state == "overlap" else runtime.SiteAutomationResult(site_key, "耗材", "consumables_vehicle_candidate_available",
                                "請確認候選車輛", vehicle_candidates=({"vehicle": "新坡96"},), reconciliation_vehicle_key=vehicle))
                        self.fixture.store.update_site_result(task_id, result, vehicle_key=vehicle)
                    compare(task_id, "/tasks/" + task_id)
                    if service == "ems" and state in {"initial", "confirmed", "failed", "waiting", "needs_update", "aborted"}:
                        compare(task_id + "_edit", "/tasks/" + task_id + "/edit")
        return results

    def test_local_and_entry_controls_and_allowed_routes_match_all_states(self):
        results = self.collect()
        self.assertGreaterEqual(len(results), 59)
        for record in results:
            with self.subTest(scenario=record["scenario"]):
                self.assertEqual([], record["missing"])
                self.assertEqual([], record["extra"])
                self.assertEqual([], record["blocked"])
                self.assertEqual(record["status"]["local"], record["status"]["entry"])
        labels = {row.get("label", "") for record in results for row in record["controls"]}
        self.assertIn("確認新坡95既有里程", labels)
        self.assertIn("確認新坡11既有里程", labels)
        self.assertIn("確認使用新坡96", labels)
        self.assertIn("六站登打啟動", labels)
        self.assertIn("三站登打啟動", labels)


if __name__ == "__main__":
    unittest.main()
