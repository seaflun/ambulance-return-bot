import dataclasses
import json
import multiprocessing
import msvcrt
import os
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from ambulance_bot import worker_routes


def _promote_known_identity_from_process(
    local_app_data: str,
    instance_id: str,
    start,
    results,
) -> None:
    with mock.patch.dict(os.environ, {"LOCALAPPDATA": local_app_data}, clear=False):
        start.wait(5.0)
        try:
            promoted = worker_routes.try_promote_known_server_identity(instance_id)
        except AttributeError:
            promoted = False
        results.put((instance_id, promoted))


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

    def test_route_choice_defaults_provenance_to_manual(self):
        choice = worker_routes.RouteChoice("http://lan", "", "lan", "unverified", "", "offline")

        self.assertEqual(choice.provenance, "manual")

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

    def test_control_client_bootstraps_only_pinned_first_start_route(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://lan",
            "",
            "lan",
            "unverified",
            instance_id,
            "single_route_unverified",
            "builtin",
        )
        payloads: list[dict[str, object]] = []

        def post(_url: str, payload: dict[str, object]) -> dict[str, object]:
            payloads.append(payload)
            return {"ok": True, "server": {"instance_id": instance_id}}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=post,
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            response = client.control({"state": "idle"})

            self.assertEqual(payloads[0]["route"]["identity_status"], "unverified")
            snapshot = response.request_route
            self.assertIsInstance(snapshot, worker_routes.RequestRouteSnapshot)
            self.assertEqual(snapshot.url, "http://lan")
            self.assertEqual(snapshot.identity_status, "unverified")
            with self.assertRaises(dataclasses.FrozenInstanceError):
                snapshot.url = "http://other"
            self.assertEqual(client.choice.identity_status, "verified")
            self.assertEqual(client.choice.instance_id, instance_id)
            self.assertEqual(worker_routes.load_known_server_identity(), instance_id)

    def test_control_client_never_bootstraps_manual_route_even_when_url_matches(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://lan",
            "",
            "lan",
            "unverified",
            instance_id,
            "single_route_unverified",
            "manual",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=lambda _url, _payload: {"ok": True, "server": {"instance_id": instance_id}},
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            client.control({"state": "idle"})

            self.assertEqual(client.choice.identity_status, "unverified")
            self.assertEqual(worker_routes.load_known_server_identity(), "")

    def test_control_client_rejects_mismatched_unverified_response_before_promotion(self):
        expected_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        response_id = "5aa40273-190d-4e53-b7d1-6d2cf2f27212"
        choice = worker_routes.RouteChoice(
            "http://lan",
            "",
            "lan",
            "unverified",
            expected_id,
            "single_route_unverified",
            "builtin",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=lambda _url, _payload: {"ok": True, "server": {"instance_id": response_id}},
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                client.control({"state": "idle"})

            self.assertEqual(client.choice.identity_status, "unverified")
            self.assertEqual(worker_routes.load_known_server_identity(), "")

    def test_control_client_requires_canonical_route_name_to_match_bootstrap_url(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://lan",
            "",
            "tailscale",
            "unverified",
            instance_id,
            "single_route_unverified",
            "builtin",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=lambda _url, _payload: {"ok": True, "server": {"instance_id": instance_id}},
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            client.control({"state": "idle"})

            self.assertEqual(client.choice.identity_status, "unverified")
            self.assertEqual(worker_routes.load_known_server_identity(), "")

    def test_control_client_requires_canonical_bootstrap_url_to_match_route(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://other",
            "",
            "lan",
            "unverified",
            instance_id,
            "single_route_unverified",
            "builtin",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=lambda _url, _payload: {"ok": True, "server": {"instance_id": instance_id}},
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            client.control({"state": "idle"})

            self.assertEqual(client.choice.identity_status, "unverified")
            self.assertEqual(worker_routes.load_known_server_identity(), "")

    def test_control_client_keeps_bootstrap_url_bound_to_request_snapshot(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://other",
            "",
            "lan",
            "unverified",
            instance_id,
            "single_route_unverified",
            "builtin",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client: worker_routes.WorkerControlClient

            def post(url: str, _payload: dict[str, object]) -> dict[str, object]:
                self.assertEqual(url, "http://other/worker/control")
                client.choice = dataclasses.replace(client.choice, primary_url="http://lan")
                return {"ok": True, "server": {"instance_id": instance_id}}

            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=post,
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            client.control({"state": "idle"})

            self.assertEqual(client.choice.primary_url, "http://lan")
            self.assertEqual(client.choice.identity_status, "unverified")
            self.assertEqual(worker_routes.load_known_server_identity(), "")

    def test_control_client_rechecks_current_choice_against_bootstrap_url(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://lan",
            "",
            "lan",
            "unverified",
            instance_id,
            "single_route_unverified",
            "builtin",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client: worker_routes.WorkerControlClient

            def post(url: str, _payload: dict[str, object]) -> dict[str, object]:
                self.assertEqual(url, "http://lan/worker/control")
                client.choice = dataclasses.replace(client.choice, primary_url="http://other")
                return {"ok": True, "server": {"instance_id": instance_id}}

            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=post,
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            client.control({"state": "idle"})

            self.assertEqual(client.choice.primary_url, "http://other")
            self.assertEqual(client.choice.identity_status, "unverified")
            self.assertEqual(worker_routes.load_known_server_identity(), "")

    def test_control_client_does_not_promote_choice_changed_during_identity_cas(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://lan",
            "",
            "lan",
            "unverified",
            instance_id,
            "single_route_unverified",
            "builtin",
        )
        client = worker_routes.WorkerControlClient(
            choice,
            request_json=mock.Mock(),
            post_json=lambda _url, _payload: {"ok": True, "server": {"instance_id": instance_id}},
            bootstrap_url="http://lan",
            bootstrap_route_name="lan",
        )

        def promote(_instance_id: str) -> bool:
            client.choice = dataclasses.replace(client.choice, primary_url="http://other")
            return True

        with mock.patch.object(worker_routes, "try_promote_known_server_identity", side_effect=promote):
            client.control({"state": "idle"})

        self.assertEqual(client.choice.primary_url, "http://other")
        self.assertEqual(client.choice.identity_status, "unverified")

    def test_control_client_does_not_bootstrap_with_a_fallback_route(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://lan",
            "http://tail",
            "lan",
            "unverified",
            instance_id,
            "single_route_unverified",
            "builtin",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=lambda _url, _payload: {"ok": True, "server": {"instance_id": instance_id}},
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            client.control({"state": "idle"})

            self.assertEqual(client.choice.identity_status, "unverified")
            self.assertEqual(worker_routes.load_known_server_identity(), "")

    def test_control_client_keeps_fallback_gate_bound_to_request_snapshot(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://lan",
            "http://tail",
            "lan",
            "unverified",
            instance_id,
            "single_route_unverified",
            "builtin",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client: worker_routes.WorkerControlClient

            def post(_url: str, _payload: dict[str, object]) -> dict[str, object]:
                client.choice = dataclasses.replace(client.choice, fallback_url="")
                return {"ok": True, "server": {"instance_id": instance_id}}

            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=post,
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            client.control({"state": "idle"})

            self.assertEqual(client.choice.fallback_url, "")
            self.assertEqual(client.choice.identity_status, "unverified")
            self.assertEqual(worker_routes.load_known_server_identity(), "")

    def test_control_client_requires_the_exact_single_route_diagnostic(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://lan",
            "",
            "lan",
            "unverified",
            instance_id,
            "single_route_unverified_with_note",
            "builtin",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=lambda _url, _payload: {"ok": True, "server": {"instance_id": instance_id}},
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            client.control({"state": "idle"})

            self.assertEqual(client.choice.identity_status, "unverified")
            self.assertEqual(worker_routes.load_known_server_identity(), "")

    def test_control_client_keeps_diagnostic_gate_bound_to_request_snapshot(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        choice = worker_routes.RouteChoice(
            "http://lan",
            "",
            "lan",
            "unverified",
            instance_id,
            "single_route_unverified_with_note",
            "builtin",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            client: worker_routes.WorkerControlClient

            def post(_url: str, _payload: dict[str, object]) -> dict[str, object]:
                client.choice = dataclasses.replace(client.choice, diagnostic="single_route_unverified")
                return {"ok": True, "server": {"instance_id": instance_id}}

            client = worker_routes.WorkerControlClient(
                choice,
                request_json=mock.Mock(),
                post_json=post,
                bootstrap_url="http://lan",
                bootstrap_route_name="lan",
            )

            client.control({"state": "idle"})

            self.assertEqual(client.choice.diagnostic, "single_route_unverified")
            self.assertEqual(client.choice.identity_status, "unverified")
            self.assertEqual(worker_routes.load_known_server_identity(), "")

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

        response = client.control(payload)

        self.assertEqual(calls[0][1]["route"]["name"], "lan")
        self.assertEqual(calls[1][1]["route"]["name"], "tailscale")
        self.assertEqual(payload["route"]["name"], "lan")
        self.assertEqual(response.request_route.url, "http://tail")
        self.assertEqual(response.request_route.route_name, "tailscale")

    def test_control_client_rejects_response_from_different_instance(self):
        choice = worker_routes.RouteChoice("http://lan", "", "lan", "verified", "same", "")
        client = worker_routes.WorkerControlClient(
            choice,
            request_json=mock.Mock(),
            post_json=lambda _url, _payload: {"ok": True, "server": {"instance_id": "other"}},
        )

        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            client.control({"state": "idle"})

    def test_control_client_allows_unverified_heartbeat_without_claiming_identity(self):
        choice = worker_routes.RouteChoice("http://manual", "", "manual", "unverified", "", "manual_url")
        client = worker_routes.WorkerControlClient(
            choice,
            request_json=mock.Mock(),
            post_json=lambda _url, _payload: {"ok": True, "server": {"instance_id": "nas-a"}},
        )

        self.assertTrue(client.control({"state": "idle"})["ok"])

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

    def test_single_route_known_instance_mismatch_has_distinct_diagnostic(self):
        reachable_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        known_id = "5aa40273-190d-4e53-b7d1-6d2cf2f27212"

        def only_lan(url: str) -> worker_routes.ServerIdentity:
            if url == "http://lan":
                return worker_routes.ServerIdentity(url, reachable_id, "v", "nas")
            raise TimeoutError()

        choice = worker_routes.choose_verified_route(
            "http://lan",
            "http://tail",
            fetch_identity=only_lan,
            known_instance_id=known_id,
        )

        self.assertEqual(choice.identity_status, "unverified")
        self.assertEqual(choice.diagnostic, "single_route_known_instance_mismatch")

    def test_try_promote_known_server_identity_rejects_malformed_and_different_cache(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        other_id = "5aa40273-190d-4e53-b7d1-6d2cf2f27212"
        malformed = '{"instance_id":"not-a-uuid"}'
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            identity_path = worker_routes.known_server_identity_path()
            identity_path.parent.mkdir(parents=True, exist_ok=True)
            identity_path.write_text(malformed, encoding="utf-8")

            self.assertFalse(worker_routes.try_promote_known_server_identity(instance_id))
            self.assertEqual(identity_path.read_text(encoding="utf-8"), malformed)

            identity_path.write_text(json.dumps({"instance_id": other_id}), encoding="utf-8")
            self.assertFalse(worker_routes.try_promote_known_server_identity(instance_id))
            self.assertEqual(worker_routes.load_known_server_identity(), other_id)

            self.assertTrue(worker_routes.try_promote_known_server_identity(other_id))
            self.assertEqual(worker_routes.load_known_server_identity(), other_id)

    def test_try_promote_known_server_identity_fails_when_sidecar_lock_is_held(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            lock_path = worker_routes.known_server_identity_lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as lock_file:
                lock_file.seek(0)
                lock_file.write(b"\0")
                lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    started_at = time.monotonic()
                    self.assertFalse(worker_routes.try_promote_known_server_identity(instance_id))
                    self.assertLess(time.monotonic() - started_at, 2.0)
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

    def test_try_promote_known_server_identity_fails_when_atomic_write_fails(self):
        instance_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        for write_error in (OSError("disk full"), RuntimeError("writer failed")):
            with self.subTest(write_error=type(write_error).__name__), tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": tmp},
                clear=False,
            ), mock.patch.object(worker_routes.worker_health, "write_json_atomic", side_effect=write_error):
                self.assertFalse(worker_routes.try_promote_known_server_identity(instance_id))
                self.assertFalse(worker_routes.known_server_identity_path().exists())

    def test_try_promote_known_server_identity_allows_only_one_concurrent_candidate(self):
        first_id = "6a04200e-e1d6-4a31-9ba5-eaf4f8d2d0dc"
        second_id = "5aa40273-190d-4e53-b7d1-6d2cf2f27212"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": tmp},
            clear=False,
        ):
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            candidates = [
                context.Process(target=_promote_known_identity_from_process, args=(tmp, instance_id, start, results))
                for instance_id in (first_id, second_id)
            ]
            for candidate in candidates:
                candidate.start()
            start.set()
            for candidate in candidates:
                candidate.join(5.0)

            self.assertTrue(all(not candidate.is_alive() for candidate in candidates))
            outcomes = [results.get(timeout=2.0) for _ in candidates]
            accepted = [instance_id for instance_id, promoted in outcomes if promoted]
            self.assertEqual(len(accepted), 1)
            self.assertEqual(worker_routes.load_known_server_identity(), accepted[0])


if __name__ == "__main__":
    unittest.main()
