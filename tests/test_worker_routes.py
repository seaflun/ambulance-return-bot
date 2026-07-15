import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from ambulance_bot import worker_routes


class WorkerRouteTests(unittest.TestCase):
    def test_fetch_server_identity_requires_complete_server_schema(self):
        request = mock.Mock(
            return_value={
                "ok": True,
                "server": {
                    "instance_id": "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc",
                    "version": "2026.07.15.1",
                    "deployment": "ambulance_return_bot_nas",
                },
            }
        )

        identity = worker_routes.fetch_server_identity("http://nas:8080/", request)

        self.assertEqual(identity.base_url, "http://nas:8080")
        self.assertEqual(identity.instance_id, "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc")
        request.assert_called_once_with("http://nas:8080/worker/identity")

        request.return_value = {"ok": True, "server": {"instance_id": "same", "version": "v"}}
        with self.assertRaisesRegex(RuntimeError, "schema invalid"):
            worker_routes.fetch_server_identity("http://nas:8080", request)

    def test_choose_verified_route_prefers_lan_only_when_instances_match(self):
        identities = {
            "http://10.30.65.30:8080": worker_routes.ServerIdentity(
                "http://10.30.65.30:8080", "same", "v", "nas"
            ),
            "http://100.114.126.58:8080": worker_routes.ServerIdentity(
                "http://100.114.126.58:8080", "same", "v", "nas"
            ),
        }

        choice = worker_routes.choose_verified_route(
            "http://10.30.65.30:8080",
            "http://100.114.126.58:8080",
            fetch_identity=identities.__getitem__,
        )

        self.assertEqual(choice.primary_url, "http://10.30.65.30:8080")
        self.assertEqual(choice.fallback_url, "http://100.114.126.58:8080")
        self.assertEqual(choice.route_name, "lan")
        self.assertEqual(choice.identity_status, "verified")

    def test_choose_verified_route_rejects_mismatched_lan_identity(self):
        def fetch(url):
            return worker_routes.ServerIdentity(url, "old" if "10.30" in url else "live", "v", "nas")

        choice = worker_routes.choose_verified_route(
            "http://10.30.65.30:8080",
            "http://100.114.126.58:8080",
            fetch_identity=fetch,
        )

        self.assertEqual(choice.primary_url, "http://100.114.126.58:8080")
        self.assertEqual(choice.fallback_url, "")
        self.assertEqual(choice.route_name, "tailscale")
        self.assertIn("mismatch", choice.diagnostic)

    def test_single_reachable_route_is_verified_only_when_it_matches_local_identity(self):
        def only_lan(url):
            if url == "http://lan":
                return worker_routes.ServerIdentity(url, "known", "v", "nas")
            raise TimeoutError()

        verified = worker_routes.choose_verified_route(
            "http://lan",
            "http://tail",
            fetch_identity=only_lan,
            known_instance_id="known",
        )
        unverified = worker_routes.choose_verified_route(
            "http://lan",
            "http://tail",
            fetch_identity=only_lan,
            known_instance_id="",
        )

        self.assertEqual(verified.identity_status, "verified")
        self.assertEqual(unverified.identity_status, "unverified")

    def test_control_client_uses_verified_fallback_only_for_transport_failure(self):
        choice = worker_routes.RouteChoice("http://lan", "http://tail", "lan", "verified", "same", "")
        calls: list[str] = []

        def post(url, _payload):
            calls.append(url)
            if url == "http://lan/worker/control":
                raise urllib.error.URLError("network down")
            return {"ok": True, "server": {"instance_id": "same"}}

        client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)

        self.assertTrue(client.control({"state": "idle"})["ok"])
        self.assertEqual(calls, ["http://lan/worker/control", "http://tail/worker/control"])

    def test_control_client_does_not_mask_http_403_with_fallback(self):
        choice = worker_routes.RouteChoice("http://lan", "http://tail", "lan", "verified", "same", "")
        post = mock.Mock(side_effect=RuntimeError("NAS worker API 回應 HTTP 403：FORBIDDEN"))
        client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)

        with self.assertRaisesRegex(RuntimeError, "403"):
            client.control({"state": "idle"})

        self.assertEqual(post.call_count, 1)

    def test_control_client_does_not_treat_http_error_as_transport_failure(self):
        choice = worker_routes.RouteChoice("http://lan", "http://tail", "lan", "verified", "same", "")
        error = urllib.error.HTTPError("http://lan/worker/control", 403, "Forbidden", None, None)
        post = mock.Mock(side_effect=error)
        client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)

        try:
            with self.assertRaises(urllib.error.HTTPError):
                client.control({"state": "idle"})

            self.assertEqual(post.call_count, 1)
        finally:
            error.close()

    def test_control_client_does_not_treat_http_timeout_response_as_transport_failure(self):
        choice = worker_routes.RouteChoice("http://lan", "http://tail", "lan", "verified", "same", "")
        post = mock.Mock(side_effect=RuntimeError("NAS worker API 回應 HTTP 504：Gateway Timeout"))
        client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)

        with self.assertRaisesRegex(RuntimeError, "504"):
            client.control({"state": "idle"})

        self.assertEqual(post.call_count, 1)

    def test_control_client_does_not_fallback_when_http_error_is_wrapped(self):
        choice = worker_routes.RouteChoice("http://lan", "http://tail", "lan", "verified", "same", "")
        error = urllib.error.HTTPError("http://lan/worker/control", 503, "Gateway Timeout", None, None)
        calls: list[str] = []

        def post(url, _payload):
            calls.append(url)
            try:
                raise error
            except urllib.error.HTTPError as caught:
                raise RuntimeError("wrapped server failure") from caught

        client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)

        try:
            with self.assertRaisesRegex(RuntimeError, "wrapped server failure"):
                client.control({"state": "idle"})

            self.assertEqual(calls, ["http://lan/worker/control"])
        finally:
            error.close()

    def test_control_client_labels_the_actual_fallback_route(self):
        choice = worker_routes.RouteChoice("http://lan", "http://tail", "lan", "verified", "same", "")
        payload = {
            "state": "idle",
            "route": {"name": "lan", "identity_status": "verified", "instance_id": "same"},
        }
        calls: list[tuple[str, dict[str, object]]] = []

        def post(url, posted_payload):
            calls.append((url, posted_payload))
            if url == "http://lan/worker/control":
                raise urllib.error.URLError("network down")
            return {"ok": True, "server": {"instance_id": "same"}}

        client = worker_routes.WorkerControlClient(choice, request_json=mock.Mock(), post_json=post)

        client.control(payload)

        self.assertEqual(calls[0][1]["route"]["name"], "lan")
        self.assertEqual(calls[1][1]["route"]["name"], "tailscale")
        self.assertEqual(payload["route"]["name"], "lan")

    def test_control_client_rejects_response_from_different_instance(self):
        choice = worker_routes.RouteChoice("http://lan", "", "lan", "verified", "same", "")
        client = worker_routes.WorkerControlClient(
            choice,
            request_json=mock.Mock(),
            post_json=lambda _url, _payload: {"ok": True, "server": {"instance_id": "other"}},
        )

        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            client.control({"state": "idle"})

    def test_known_identity_storage_accepts_only_uuid_like_values(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            self.assertEqual(worker_routes.load_known_server_identity(), "")
            self.assertFalse(worker_routes.remember_known_server_identity("not-an-instance"))
            self.assertFalse(worker_routes.known_server_identity_path().exists())
            self.assertTrue(worker_routes.remember_known_server_identity(instance_id))

            self.assertEqual(worker_routes.load_known_server_identity(), instance_id)
            self.assertEqual(
                json.loads(worker_routes.known_server_identity_path().read_text(encoding="utf-8")),
                {"instance_id": instance_id},
            )


if __name__ == "__main__":
    unittest.main()
