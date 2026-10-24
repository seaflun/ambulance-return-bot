from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import worker
import worker_gui
from ambulance_bot import duty_credentials
from ambulance_bot import worker_routes
from ambulance_bot.status_outbox import WorkerStatusOutbox


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


@contextmanager
def _http_server(callback):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            callback(self)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            callback(self)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class WorkerAuditRegressionTests(unittest.TestCase):
    def test_worker_api_rejects_redirects_before_contacting_another_authority(self):
        received = []

        def receiver(handler):
            received.append(handler.headers.get("X-Worker-Token"))
            handler.send_response(200)
            handler.end_headers()
            handler.wfile.write(b'{"ok":true}')

        with _http_server(receiver) as target:
            def sender(handler):
                handler.send_response(302)
                handler.send_header("Location", target + "/unverified")
                handler.end_headers()

            with _http_server(sender) as source, mock.patch.dict(os.environ, {"WORKER_TOKEN": "SYNTHETIC_AUDIT_TOKEN"}):
                for endpoint in ("identity", "control"):
                    with self.subTest(endpoint=endpoint), self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                        if endpoint == "identity":
                            worker.request_json(source + "/worker/identity")
                        else:
                            worker.post_json(source + "/worker/control", {"state": "idle"})
                with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                    "ARTIFACTS_DIR": tmp, "WORKER_STATUS_POST_RETRIES": "1", "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "0",
                }):
                    worker.post_status(source, "audit-cross-redirect", "worker_running", "running")
                    pending = WorkerStatusOutbox(Path(tmp) / "worker_status_outbox").pending()
                    self.assertEqual(len(pending), 1)
                    self.assertTrue(pending[0]["payload"]["body"]["status_event_id"])
                self.assertEqual(received, [])

    def test_same_authority_redirect_keeps_status_event_until_direct_ack(self):
        seen = []
        redirect = True

        def respond(handler):
            seen.append(handler.path)
            handler.send_response(302 if redirect else 200)
            if redirect:
                handler.send_header("Location", "/unverified")
            handler.end_headers()
            if not redirect:
                handler.wfile.write(b'{"ok":true}')

        with _http_server(respond) as source, tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
            "ARTIFACTS_DIR": tmp, "WORKER_STATUS_POST_RETRIES": "1", "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "0",
        }):
            worker.post_status(source, "audit-redirect", "worker_running", "running")
            outbox = WorkerStatusOutbox(Path(tmp) / "worker_status_outbox")
            pending = outbox.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(seen, ["/worker/tasks/audit-redirect/status"])
            original_id = pending[0]["event_id"]
            redirect = False
            self.assertEqual(worker.flush_status_outbox(source), 1)
            self.assertEqual(outbox.pending(), [])
            self.assertEqual(len(seen), 2)
            self.assertTrue(original_id)

    def test_verified_control_fallback_routes_task_reads_and_status_without_post_retry(self):
        lan = worker.WORKER_NAS_LAN_URL
        tail = worker.WORKER_NAS_TAILSCALE_URL
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        requests = []

        def send(request, timeout):
            requests.append(request)
            if request.full_url == lan + "/worker/control":
                raise urllib.error.URLError("synthetic LAN unavailable")
            if request.full_url.startswith(lan):
                raise self.failureException("ordinary API still used failed LAN route")
            if request.full_url == tail + "/worker/control":
                return _Response(json.dumps({"ok": True, "server": {"instance_id": instance_id}}).encode())
            return _Response(b'{"ok":true}')

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
            "LOCALAPPDATA": tmp, "ARTIFACTS_DIR": tmp,
            "WORKER_SERVER_ROUTE_PROVENANCE": "builtin",
            "WORKER_SERVER_IDENTITY_STATUS": "verified",
            "WORKER_SERVER_INSTANCE_ID": instance_id,
            "WORKER_SERVER_FALLBACK_URL": tail,
            "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "0",
        }), mock.patch.object(worker, "_WORKER_CONTROL_CLIENT", None, create=True), mock.patch.object(
            worker._WORKER_API_OPENER, "open", side_effect=send,
        ):
            control = worker.build_worker_control_loop(lan, "audit-worker", Path(tmp), worker.worker_control.WorkerRuntimeState())
            control.run_once()
            self.assertIsNone(worker.fetch_credential_sync_request(lan))
            self.assertIsNone(worker.fetch_next_task(lan, "audit-worker"))
            worker.post_status(lan, "audit-route-task", "worker_running", "running")
            status_requests = [item for item in requests if item.full_url.endswith("/status")]
            self.assertEqual(len(status_requests), 1)
            self.assertTrue(status_requests[0].full_url.startswith(tail))
            with mock.patch.object(worker._WORKER_API_OPENER, "open", return_value=_Response(b'{"ok":true}')) as manual:
                worker.request_json("http://manual-server/worker/tasks")
                self.assertEqual(manual.call_args.args[0].full_url, "http://manual-server/worker/tasks")

    def test_api_route_requires_successful_verified_control_for_fixed_builtin_pair(self):
        lan = worker.WORKER_NAS_LAN_URL
        tail = worker.WORKER_NAS_TAILSCALE_URL
        base = worker_routes.RouteChoice(lan, tail, "lan", "verified", "same", "both_paths_match", "builtin")
        cases = [
            (base, "wrong"),
            (replace(base, identity_status="unverified"), "same"),
            (replace(base, provenance="manual"), "same"),
            (replace(base, route_name="manual"), "same"),
            (replace(base, fallback_url="http://untrusted.invalid"), "same"),
        ]
        for choice, response_id in cases:
            with self.subTest(choice=choice, response_id=response_id):
                def post(url, payload):
                    if url.startswith(lan):
                        raise urllib.error.URLError("synthetic LAN unavailable")
                    return {"ok": True, "server": {"instance_id": response_id}}
                client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)
                try:
                    client.control({"state": "idle"})
                except (RuntimeError, urllib.error.URLError):
                    pass
                self.assertEqual(client.api_request_url(lan + "/worker/credential-sync"), lan + "/worker/credential-sync")

    def test_confirmed_api_route_keeps_identity_probes_and_http_errors_on_their_route(self):
        lan = worker.WORKER_NAS_LAN_URL
        tail = worker.WORKER_NAS_TAILSCALE_URL
        choice = worker_routes.RouteChoice(lan, tail, "lan", "verified", "same", "both_paths_match", "builtin")
        fail_lan = True

        def post(url, payload):
            if url.startswith(lan) and fail_lan:
                raise urllib.error.URLError("synthetic LAN unavailable")
            return {"ok": True, "server": {"instance_id": "same"}}

        client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)
        original = lan + "/worker/credential-sync"
        self.assertEqual(client.api_request_url(original), original)
        client.control({"state": "idle"})
        self.assertEqual(client.api_request_url(original), tail + "/worker/credential-sync")
        for endpoint in ("identity", "control"):
            self.assertEqual(client.api_request_url(lan + "/worker/" + endpoint), lan + "/worker/" + endpoint)
        for error in (urllib.error.URLError("synthetic timeout"), urllib.error.HTTPError(tail, 403, "Forbidden", {}, None)):
            with self.subTest(error=error), mock.patch.object(worker, "_WORKER_CONTROL_CLIENT", client), mock.patch.object(
                worker._WORKER_API_OPENER, "open", side_effect=error,
            ) as send:
                with self.assertRaises((urllib.error.URLError, RuntimeError)):
                    worker.post_json(lan + "/worker/synthetic-command", {"request_id": "same-request"})
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[0].full_url, tail + "/worker/synthetic-command")
        fail_lan = False
        client.control({"state": "idle"})
        self.assertEqual(client.api_request_url(original), original)
        fail_lan = True
        client.control({"state": "idle"})
        client.choice = replace(choice, provenance="manual")
        self.assertEqual(client.api_request_url(original), original)

    def test_rejected_control_response_revokes_cached_api_route_for_that_choice(self):
        lan = worker.WORKER_NAS_LAN_URL
        tail = worker.WORKER_NAS_TAILSCALE_URL
        choice = worker_routes.RouteChoice(lan, tail, "lan", "verified", "same", "both_paths_match", "builtin")
        for rejected_route in ("lan", "tailscale"):
            for rejected_response in (None, {"ok": False}, {"ok": True}, {"ok": True, "server": {}},
                                      {"ok": True, "server": {"instance_id": "different"}}):
                with self.subTest(route=rejected_route, response=rejected_response):
                    reject = False

                    def post(url, _payload):
                        if url == lan + "/worker/control" and (not reject or rejected_route == "tailscale"):
                            raise urllib.error.URLError("synthetic LAN unavailable")
                        return rejected_response if reject else {"ok": True, "server": {"instance_id": "same"}}

                    client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)
                    original = lan + "/worker/tasks"
                    client.control({"state": "idle"})
                    self.assertEqual(client.api_request_url(original), tail + "/worker/tasks")
                    reject = True
                    with self.assertRaisesRegex(RuntimeError, "schema invalid|identity mismatch"):
                        client.control({"state": "idle"})
                    self.assertEqual(client.api_request_url(original), original)
                    self.assertEqual(client.choice, choice)

    def test_old_control_response_does_not_replace_new_choice_api_route(self):
        lan = worker.WORKER_NAS_LAN_URL
        tail = worker.WORKER_NAS_TAILSCALE_URL
        choice = worker_routes.RouteChoice(lan, tail, "lan", "verified", "same", "both_paths_match", "builtin")
        for new_choice in (replace(choice), replace(choice, instance_id="new-instance")):
            for old_response_id in ("same", "different"):
                with self.subTest(new_choice=new_choice, old_response_id=old_response_id):
                    phase = "initial"
                    client = None

                    def post(url, _payload):
                        nonlocal phase
                        if phase == "old-inflight":
                            phase = "new-choice"
                            client.choice = new_choice
                            client.control({"state": "idle"})
                            return {"ok": True, "server": {"instance_id": old_response_id}}
                        if url == lan + "/worker/control":
                            raise urllib.error.URLError("synthetic LAN unavailable")
                        expected = new_choice.instance_id if phase == "new-choice" else choice.instance_id
                        return {"ok": True, "server": {"instance_id": expected}}

                    client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)
                    client.control({"state": "idle"})
                    phase = "old-inflight"
                    if old_response_id == "same":
                        client.control({"state": "idle"})
                    else:
                        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                            client.control({"state": "idle"})
                    self.assertIs(client.choice, new_choice)
                    self.assertEqual(client.api_request_url(lan + "/worker/tasks"), tail + "/worker/tasks")

    def test_transport_outage_preserves_last_verified_api_route(self):
        lan = worker.WORKER_NAS_LAN_URL
        tail = worker.WORKER_NAS_TAILSCALE_URL
        choice = worker_routes.RouteChoice(lan, tail, "lan", "verified", "same", "both_paths_match", "builtin")
        transport_down = False

        def post(url, _payload):
            if url == lan + "/worker/control" or transport_down:
                raise urllib.error.URLError("synthetic network unavailable")
            return {"ok": True, "server": {"instance_id": "same"}}

        client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)
        client.control({"state": "idle"})
        transport_down = True
        with self.assertRaises(urllib.error.URLError):
            client.control({"state": "idle"})
        self.assertEqual(client.api_request_url(lan + "/worker/tasks"), tail + "/worker/tasks")

    def test_corrupt_single_credential_does_not_block_other_accounts_or_gui(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved_login.json"
            path.write_text(json.dumps({"accounts": [
                {"user_id": "valid-sync", "actor_no": "8", "password": "synthetic-password"},
                {"user_id": "corrupt", "password_dpapi": "a"},
            ]}), encoding="utf-8")
            original = path.read_bytes()
            with mock.patch.dict(os.environ, {
                "DUTY_SAVED_LOGIN_PATH": str(path), "DUTY_SAVED_LOGIN_PATH_OVERRIDE": "true",
            }):
                self.assertEqual([item.user_id for item in duty_credentials.list_saved_duty_automation_credentials()], ["valid-sync"])
                gui = mock.Mock()
                gui.credential_combo = None
                worker_gui.WorkerGui._refresh_credential_choices(gui)
                self.assertEqual(len(gui.saved_credentials), 1)
            self.assertEqual(path.read_bytes(), original)

    def test_unusable_credential_file_is_preserved_without_valid_accounts(self):
        for content in (b'{"accounts":[{"user_id":"corrupt","password_dpapi":"a"}]}', b"\xff"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "saved_login.json"
                path.write_bytes(content)
                self.assertEqual(duty_credentials.list_saved_duty_automation_credentials(path), [])
                self.assertIsNone(duty_credentials.load_saved_duty_automation_credential(path))
                self.assertEqual(path.read_bytes(), content)

    def test_non_utf8_decrypted_credential_is_unavailable(self):
        fake_dpapi = SimpleNamespace(CryptUnprotectData=lambda *_args: (None, b"\xff"))
        with mock.patch.dict("sys.modules", {"win32crypt": fake_dpapi}):
            self.assertEqual(duty_credentials.decrypt_dpapi("c3ludGhldGlj"), "")

    def test_sync_write_preserves_an_unreadable_saved_credential_file(self):
        for content in (b"\xff", b'{"accounts":', b"[]", b'{"accounts":"broken"}', b'{"accounts":[null]}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "saved_login.json"
                path.write_bytes(content)
                with mock.patch.object(duty_credentials, "encrypt_dpapi", return_value="synthetic-encrypted"):
                    with self.assertRaises(ValueError):
                        duty_credentials.save_duty_automation_credentials([
                            {"user_id": "new-sync", "actor_no": "8", "password": "synthetic-password"},
                        ], path=path)
                self.assertEqual(path.read_bytes(), content)

    def test_status_requires_valid_ack_before_removing_durable_event(self):
        for response_body in (b"<html>temporarily unavailable</html>", b"", b"[]", b'{"ok":false}'):
            with self.subTest(response_body=response_body), tempfile.TemporaryDirectory() as tmp:
                with mock.patch.dict(os.environ, {
                    "ARTIFACTS_DIR": tmp,
                    "WORKER_STATUS_POST_RETRIES": "1",
                    "WORKER_STATUS_RETRY_BACKOFF_SECONDS": "0",
                }), mock.patch.object(worker._WORKER_API_OPENER, "open", return_value=_Response(response_body)):
                    worker.post_status("http://synthetic-nas", "audit-task", "worker_running", "running")
                    outbox = WorkerStatusOutbox(Path(tmp) / "worker_status_outbox")
                    pending = outbox.pending()
                    self.assertEqual(len(pending), 1)
                    original_id = pending[0]["payload"]["body"]["status_event_id"]
                    with mock.patch.object(worker._WORKER_API_OPENER, "open", return_value=_Response(b'{"ok":true}')) as send:
                        self.assertEqual(worker.flush_status_outbox("http://synthetic-nas"), 1)
                    delivered = json.loads(send.call_args.args[0].data.decode("utf-8"))
                    self.assertEqual(delivered["status_event_id"], original_id)
                    self.assertEqual(outbox.pending(), [])


if __name__ == "__main__":
    unittest.main()
