"""Verified NAS route selection and Worker control transport helpers."""

from __future__ import annotations

import json
import msvcrt
import re
import threading
import time
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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
KNOWN_SERVER_IDENTITY_LOCK_TIMEOUT_SECONDS = 0.5
_known_server_identity_thread_lock = threading.Lock()


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
    provenance: str = "manual"

    def __post_init__(self) -> None:
        if self.provenance not in {"builtin", "manual"}:
            object.__setattr__(self, "provenance", "manual")


@dataclass(frozen=True)
class RequestRouteSnapshot:
    url: str
    route_name: str
    identity_status: str
    instance_id: str
    provenance: str
    fallback_url: str = ""
    diagnostic: str = ""


class ControlResponse(dict[str, object]):
    def __init__(self, payload: Mapping[str, object], request_route: RequestRouteSnapshot) -> None:
        super().__init__(payload)
        self.request_route = request_route


class WorkerControlClient:
    def __init__(
        self,
        choice: RouteChoice,
        *,
        request_json: Callable[..., dict[str, object]],
        post_json: Callable[..., dict[str, object]],
        bootstrap_url: str = "",
        bootstrap_route_name: str = "",
    ) -> None:
        self._choice_lock = threading.RLock()
        self._choice = choice
        self._request_json = request_json
        self._post_json = post_json
        self._bootstrap_url = _normalized_url(bootstrap_url)
        self._bootstrap_route_name = (
            bootstrap_route_name if bootstrap_route_name in {"lan", "tailscale"} else ""
        )

    @property
    def choice(self) -> RouteChoice:
        with self._choice_lock:
            return self._choice

    @choice.setter
    def choice(self, value: RouteChoice) -> None:
        with self._choice_lock:
            self._choice = value

    def control(self, payload: Mapping[str, object]) -> ControlResponse:
        with self._choice_lock:
            choice = self._choice
        if not choice.primary_url:
            raise RuntimeError("NAS route is unavailable")
        request_route = self._request_route_snapshot(choice, choice.primary_url, choice.route_name)
        try:
            response = self._post_json(
                _control_url(request_route.url),
                self._payload_for_route(payload, request_route),
            )
        except Exception as exc:
            if not self._can_use_fallback(exc, request_route):
                raise
            request_route = self._request_route_snapshot(choice, request_route.fallback_url, "tailscale")
            response = self._post_json(
                _control_url(request_route.url),
                self._payload_for_route(payload, request_route),
            )
        return self._validate_control_response(response, request_route)

    def _can_use_fallback(self, exc: BaseException, request_route: RequestRouteSnapshot) -> bool:
        return bool(
            request_route.identity_status == "verified"
            and request_route.fallback_url
            and is_transport_failure(exc)
        )

    def _request_route_snapshot(
        self,
        choice: RouteChoice,
        url: str,
        route_name: str,
    ) -> RequestRouteSnapshot:
        return RequestRouteSnapshot(
            _normalized_url(url),
            str(route_name or "").strip(),
            choice.identity_status,
            choice.instance_id,
            choice.provenance,
            _normalized_url(choice.fallback_url),
            choice.diagnostic,
        )

    def _payload_for_route(
        self,
        payload: Mapping[str, object],
        request_route: RequestRouteSnapshot,
    ) -> dict[str, object]:
        normalized = dict(payload)
        normalized["route"] = {
            "name": request_route.route_name,
            "identity_status": request_route.identity_status,
            "instance_id": request_route.instance_id,
        }
        return normalized

    def _validate_control_response(
        self,
        response: object,
        request_route: RequestRouteSnapshot,
    ) -> ControlResponse:
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise RuntimeError("NAS control response schema invalid")
        server = response.get("server")
        if not isinstance(server, Mapping):
            raise RuntimeError("NAS control response schema invalid")
        instance_id = str(server.get("instance_id") or "").strip()
        if not instance_id:
            raise RuntimeError("NAS control response schema invalid")
        expected_instance_id = str(request_route.instance_id or "").strip()
        if expected_instance_id and instance_id != expected_instance_id:
            raise RuntimeError("NAS instance identity mismatch")
        validated = ControlResponse(response, request_route)
        if self._is_bootstrap_candidate(request_route):
            with self._choice_lock:
                choice = self._choice
                if self._choice_matches_request_route(choice, request_route):
                    if try_promote_known_server_identity(instance_id) and self._choice == choice:
                        self._choice = replace(choice, identity_status="verified")
        return validated

    def _is_bootstrap_candidate(self, request_route: RequestRouteSnapshot) -> bool:
        return bool(
            self._bootstrap_url
            and self._bootstrap_route_name
            and request_route.url == self._bootstrap_url
            and not request_route.fallback_url
            and request_route.diagnostic == "single_route_unverified"
            and request_route.route_name == self._bootstrap_route_name
            and request_route.identity_status == "unverified"
            and request_route.provenance == "builtin"
            and UUID_LIKE_PATTERN.fullmatch(request_route.instance_id)
        )

    def _choice_matches_request_route(
        self,
        choice: RouteChoice,
        request_route: RequestRouteSnapshot,
    ) -> bool:
        return bool(
            _normalized_url(choice.primary_url) == request_route.url
            and _normalized_url(choice.fallback_url) == request_route.fallback_url
            and choice.route_name == request_route.route_name
            and choice.identity_status == request_route.identity_status
            and choice.instance_id == request_route.instance_id
            and choice.diagnostic == request_route.diagnostic
            and choice.provenance == request_route.provenance
        )


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


def known_server_identity_lock_path() -> Path:
    return known_server_identity_path().with_suffix(".lock")


def try_promote_known_server_identity(instance_id: str) -> bool:
    normalized_id = str(instance_id or "").strip()
    if not UUID_LIKE_PATTERN.fullmatch(normalized_id):
        return False
    with _known_server_identity_thread_lock:
        try:
            lock_path = known_server_identity_lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as lock_file:
                if not _acquire_sidecar_byte_lock(lock_file):
                    return False
                try:
                    existing_id = _load_known_server_identity_strict()
                    if existing_id is None or (existing_id and existing_id != normalized_id):
                        return False
                    if existing_id == normalized_id:
                        return True
                    worker_health.write_json_atomic(known_server_identity_path(), {"instance_id": normalized_id})
                    return _load_known_server_identity_strict() == normalized_id
                except Exception:
                    return False
                finally:
                    _release_sidecar_byte_lock(lock_file)
        except OSError:
            return False


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


def _load_known_server_identity_strict() -> str | None:
    try:
        payload = json.loads(known_server_identity_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    instance_id = payload.get("instance_id")
    if not isinstance(instance_id, str):
        return None
    normalized_id = instance_id.strip()
    return normalized_id if UUID_LIKE_PATTERN.fullmatch(normalized_id) else None


def _acquire_sidecar_byte_lock(lock_file) -> bool:
    try:
        lock_file.seek(0, 2)
        if lock_file.tell() < 1:
            lock_file.write(b"\0")
            lock_file.flush()
    except OSError:
        return False
    deadline = time.monotonic() + KNOWN_SERVER_IDENTITY_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _release_sidecar_byte_lock(lock_file) -> None:
    try:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


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
    if known_instance_id:
        status = "verified" if known_instance_id == identity.instance_id else "unverified"
        diagnostic = (
            "single_route_matches_known_identity"
            if status == "verified"
            else "single_route_known_instance_mismatch"
        )
    else:
        status = "unverified"
        diagnostic = "single_route_unverified"
    return RouteChoice(url, "", route_name, status, identity.instance_id, diagnostic)
