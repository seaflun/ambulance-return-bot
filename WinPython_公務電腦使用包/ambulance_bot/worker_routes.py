"""Verified NAS route selection and Worker control transport helpers."""

from __future__ import annotations

import json
import re
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ambulance_bot import worker_health


UUID_LIKE_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
TRANSPORT_FAILURE_TEXT = (
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "name or service not known",
)


@dataclass(frozen=True)
class ServerIdentity:
    base_url: str
    instance_id: str
    version: str
    deployment: str


@dataclass(frozen=True)
class RouteChoice:
    primary_url: str
    fallback_url: str
    route_name: str
    identity_status: str
    instance_id: str
    diagnostic: str


class WorkerControlClient:
    def __init__(
        self,
        choice: RouteChoice,
        *,
        request_json: Callable[..., dict[str, object]],
        post_json: Callable[..., dict[str, object]],
    ) -> None:
        self.choice = choice
        self._request_json = request_json
        self._post_json = post_json

    def control(self, payload: Mapping[str, object]) -> dict[str, object]:
        if not self.choice.primary_url:
            raise RuntimeError("NAS route is unavailable")
        try:
            response = self._post_json(
                _control_url(self.choice.primary_url),
                self._payload_for_route(payload, self.choice.route_name),
            )
        except Exception as exc:
            if not self._can_use_fallback(exc):
                raise
            response = self._post_json(
                _control_url(self.choice.fallback_url),
                self._payload_for_route(payload, "tailscale"),
            )
        return self._validate_control_response(response)

    def _can_use_fallback(self, exc: BaseException) -> bool:
        return bool(
            self.choice.identity_status == "verified"
            and self.choice.fallback_url
            and is_transport_failure(exc)
        )

    def _payload_for_route(self, payload: Mapping[str, object], route_name: str) -> dict[str, object]:
        normalized = dict(payload)
        normalized["route"] = {
            "name": route_name,
            "identity_status": self.choice.identity_status,
            "instance_id": self.choice.instance_id,
        }
        return normalized

    def _validate_control_response(self, response: object) -> dict[str, object]:
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise RuntimeError("NAS control response schema invalid")
        server = response.get("server")
        if not isinstance(server, Mapping):
            raise RuntimeError("NAS control response schema invalid")
        instance_id = str(server.get("instance_id") or "").strip()
        if instance_id != self.choice.instance_id:
            raise RuntimeError("NAS instance identity mismatch")
        return response


def fetch_server_identity(
    base_url: str,
    request_json: Callable[[str], dict[str, object]],
) -> ServerIdentity:
    normalized_url = _normalized_url(base_url)
    if not normalized_url:
        raise RuntimeError("NAS server URL is empty")
    response = request_json(f"{normalized_url}/worker/identity")
    if not isinstance(response, Mapping) or response.get("ok") is not True:
        raise RuntimeError("NAS identity response schema invalid")
    server = response.get("server")
    if not isinstance(server, Mapping):
        raise RuntimeError("NAS identity response schema invalid")
    instance_id = _required_identity_field(server, "instance_id")
    version = _required_identity_field(server, "version")
    deployment = _required_identity_field(server, "deployment")
    return ServerIdentity(normalized_url, instance_id, version, deployment)


def known_server_identity_path() -> Path:
    return worker_health.state_root() / "worker_server_identity.json"


def load_known_server_identity() -> str:
    try:
        payload = json.loads(known_server_identity_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    instance_id = str(payload.get("instance_id") or "").strip()
    return instance_id if UUID_LIKE_PATTERN.fullmatch(instance_id) else ""


def remember_known_server_identity(instance_id: str) -> bool:
    normalized_id = str(instance_id or "").strip()
    if not UUID_LIKE_PATTERN.fullmatch(normalized_id):
        return False
    worker_health.write_json_atomic(known_server_identity_path(), {"instance_id": normalized_id})
    return True


def choose_verified_route(
    primary_url: str,
    fallback_url: str,
    *,
    fetch_identity: Callable[[str], ServerIdentity],
    known_instance_id: str = "",
) -> RouteChoice:
    primary = _normalized_url(primary_url)
    fallback = _normalized_url(fallback_url)
    primary_identity = _try_fetch_identity(primary, fetch_identity)
    fallback_identity = _try_fetch_identity(fallback, fetch_identity)
    known = str(known_instance_id or "").strip()

    if primary_identity and fallback_identity:
        if primary_identity.instance_id == fallback_identity.instance_id:
            status = "verified" if not known or known == primary_identity.instance_id else "unverified"
            diagnostic = "both_paths_match" if status == "verified" else "known_instance_mismatch"
            return RouteChoice(
                primary,
                fallback,
                "lan",
                status,
                primary_identity.instance_id,
                diagnostic,
            )
        status = "verified" if not known or known == fallback_identity.instance_id else "unverified"
        diagnostic = "lan_instance_mismatch_tailscale_selected"
        if status != "verified":
            diagnostic = "lan_instance_mismatch_known_instance_mismatch"
        return RouteChoice(
            fallback,
            "",
            "tailscale",
            status,
            fallback_identity.instance_id,
            diagnostic,
        )

    if primary_identity:
        return _single_route_choice(
            primary,
            "lan" if fallback else "manual",
            primary_identity,
            known,
        )
    if fallback_identity:
        return _single_route_choice(fallback, "tailscale", fallback_identity, known)
    return RouteChoice(primary or fallback, "", "offline", "unverified", "", "identity_unreachable")


def is_transport_failure(exc: BaseException) -> bool:
    if _has_http_error_in_chain(exc):
        return False
    text = str(exc).casefold()
    if re.search(r"http\s+\d{3}\b", text):
        return False
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    return any(fragment in text for fragment in TRANSPORT_FAILURE_TEXT)


def _has_http_error_in_chain(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, urllib.error.HTTPError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _normalized_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _control_url(base_url: str) -> str:
    return f"{_normalized_url(base_url)}/worker/control"


def _required_identity_field(server: Mapping[str, object], key: str) -> str:
    value = server.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("NAS identity response schema invalid")
    return value.strip()


def _try_fetch_identity(
    url: str,
    fetch_identity: Callable[[str], ServerIdentity],
) -> ServerIdentity | None:
    if not url:
        return None
    try:
        identity = fetch_identity(url)
    except Exception:
        return None
    if not isinstance(identity, ServerIdentity) or identity.base_url != url or not identity.instance_id:
        return None
    return identity


def _single_route_choice(
    url: str,
    route_name: str,
    identity: ServerIdentity,
    known_instance_id: str,
) -> RouteChoice:
    status = "verified" if known_instance_id and known_instance_id == identity.instance_id else "unverified"
    diagnostic = "single_route_matches_known_identity" if status == "verified" else "single_route_unverified"
    return RouteChoice(url, "", route_name, status, identity.instance_id, diagnostic)
