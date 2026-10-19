#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

"""Qualify the packaged OpenRGB plugin against an isolated protocol-v7 bridge."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
import pwd
import queue
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO


MAX_FRAME_BYTES = 1_048_576
MAX_DIAGNOSTIC_LINE_BYTES = 4_096
MAX_DIAGNOSTIC_LINES = 32
MAX_HARNESS_EVENTS = 256
MAX_EVIDENCE_RECORDS = 512
MAX_BRIDGE_EVENTS = 256
MAX_ACTIVE_CONNECTIONS = 32
MAX_ACTIVE_LEASES = 64
MAX_REQUEST_METHODS = 32
MAX_SUBSCRIPTIONS_PER_SESSION = 8
MAX_CLIENT_FIELD_BYTES = 256
MAX_CONTROLLER_CONVERGENCE_MS = 20_000
SDK_PORT = 6743
SOCKET_PATH = Path("/run/hyperflux-next/bridge.sock")
SYSTEM_OPENRGB_PLUGIN_DIRECTORY = Path("/usr/lib/openrgb/plugins")
DESKTOP_UID = 1_000
RUNTIME_MODES = ("system", "desktop")
EXPECTED_NAMES = {
    "Razer Basilisk V3 Pro 35K (Wireless)": {
        "serial": "HFX-MOUSE-000000000001",
        "type": 6,
        "generation_device": "mouse-00cd",
    },
    "Razer DeathStalker V2 Pro TKL (Wireless)": {
        "serial": "HFX-KEYBD-000000000001",
        "type": 5,
        "generation_device": "keyboard-0296",
    },
}
REQUIRED_FEATURES = {"native-receiver-tunnel", "ownership-leases"}
PINNED_VERIFICATION_ENVIRONMENT = "HFX_PINNED_VERIFICATION_ENVIRONMENT"
OUTER_ISOLATION_ENVIRONMENT = "HFX_OPENRGB_OUTER_ISOLATION"
OUTER_EXECUTABLE_SCRATCH_ENVIRONMENT = "HFX_OPENRGB_EXECUTABLE_SCRATCH"
OUTER_EXECUTABLE_SCRATCH = Path("/qualification/runtime")
ISOLATION_AUTHORITIES = ("user-namespace", "outer-oci-container")
OUTER_SETUP_CAPABILITIES = (
    "DAC_OVERRIDE",
    "NET_ADMIN",
    "SETPCAP",
    "SYS_ADMIN",
)


class QualificationFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise QualificationFailure(message)


def bounded_diagnostic(value: object) -> str:
    return str(value).encode("utf-8")[:MAX_DIAGNOSTIC_LINE_BYTES].decode(
        "utf-8", errors="replace"
    )


@dataclass(frozen=True)
class RuntimeContext:
    mode: str
    harness_uid: int
    socket_path: Path


def runtime_context(runtime: str) -> RuntimeContext:
    return RuntimeContext(
        runtime,
        DESKTOP_UID if runtime == "desktop" else 0,
        runtime_socket_path(runtime),
    )


def runtime_socket_path(runtime: str) -> Path:
    require(runtime in RUNTIME_MODES, f"unknown qualification runtime: {runtime}")
    if runtime == "desktop":
        return Path(f"/run/user/{DESKTOP_UID}/hyperflux-next-bridge.sock")
    return SOCKET_PATH


def harness_command(binary: Path, runtime: str = "system") -> tuple[str, ...]:
    command = [
        "setpriv",
        "--no-new-privs",
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
    ]
    if runtime == "desktop":
        command.extend(
            (
                f"--reuid={DESKTOP_UID}",
                f"--regid={DESKTOP_UID}",
                "--clear-groups",
            )
        )
    else:
        require(runtime == "system", f"unknown qualification runtime: {runtime}")
    command.extend((str(binary), str(SDK_PORT)))
    command.append(str(SYSTEM_OPENRGB_PLUGIN_DIRECTORY))
    return tuple(command)


def require_harness_capability_drop() -> None:
    completed = subprocess.run(
        (
            "setpriv",
            "--no-new-privs",
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "setpriv",
            "--dump",
        ),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    require(
        completed.returncode == 0,
        "OpenRGB harness capability-drop preflight failed: "
        + bounded_diagnostic(completed.stderr),
    )
    properties = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            properties[key.strip()] = value.strip()
    require(
        properties.get("no_new_privs") == "1"
        and properties.get("Inheritable capabilities") == "[none]"
        and properties.get("Ambient capabilities") == "[none]"
        and properties.get("Capability bounding set") == "[none]",
        "OpenRGB harness capability-drop preflight retained authority",
    )


def read_bounded_line(
    stream: BinaryIO,
    limit: int = MAX_DIAGNOSTIC_LINE_BYTES,
) -> tuple[str, bool] | None:
    require(limit > 0, "diagnostic line bound must be positive")
    first = stream.readline(limit + 1)
    if not first:
        return None
    complete = first.endswith(b"\n")
    truncated = len(first) > limit
    retained = first[:limit]
    while not complete:
        remainder = stream.readline(limit + 1)
        if not remainder:
            break
        truncated = True
        complete = remainder.endswith(b"\n")
    return retained.rstrip(b"\r\n").decode("utf-8", errors="replace"), truncated


def read_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise EOFError("bridge client disconnected")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(connection: socket.socket) -> dict[str, Any]:
    length = struct.unpack("!I", read_exact(connection, 4))[0]
    require(0 < length <= MAX_FRAME_BYTES, "synthetic bridge received an invalid frame length")
    payload = read_exact(connection, length)
    value = json.loads(payload)
    require(isinstance(value, dict), "synthetic bridge request must be an object")
    return value


def write_frame(connection: socket.socket, value: dict[str, Any]) -> None:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    require(0 < len(payload) <= MAX_FRAME_BYTES, "synthetic bridge response is out of bounds")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def token(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def cursor(epoch: int, sequence: int) -> dict[str, Any]:
    return {
        "stream_id": f"native-route-stream-{epoch}",
        "stream_epoch": str(epoch),
        "projection_revision": 1,
        "sequence": str(sequence),
    }


def resource(generation: int, device_id: str) -> dict[str, Any]:
    return {
        "receiver_id": "receiver-hyperflux-v2",
        "generation_id": str(generation),
        "device_id": device_id,
        "kind": "native-control",
    }


def route(
    generation: int,
    server_epoch: int,
    device_id: str,
    endpoint_id: str,
    device_kind: str,
    product_id: int,
) -> dict[str, Any]:
    return {
        "receiver_id": "receiver-hyperflux-v2",
        "generation_id": str(generation),
        "device_id": device_id,
        "endpoint_id": endpoint_id,
        "device_kind": device_kind,
        "vendor_id": 0x1532,
        "product_id": product_id,
        "route_state": "available",
        "access": "qualified",
        "protocol_family": "razer-feature-report-v1",
        "report_bytes": 90,
        "route_token": token(
            f"route:{server_epoch}:{generation}:{device_id}:{endpoint_id}"
        ),
        "resource": resource(generation, device_id),
    }


@dataclass
class Session:
    connection_id: int
    client_id: str
    client_name: str
    identity_key: str
    protocol_session_id: str
    negotiation_token: str
    subscriptions: set[str] = field(default_factory=set)


@dataclass
class BridgeEvidence:
    lock: threading.Lock = field(default_factory=threading.Lock)
    accepted_client_ids: deque[str] = field(
        default_factory=lambda: deque(maxlen=MAX_EVIDENCE_RECORDS)
    )
    client_names: dict[str, str] = field(default_factory=dict)
    active_client_ids: set[str] = field(default_factory=set)
    active_identity_keys: set[str] = field(default_factory=set)
    duplicate_rejections: int = 0
    feature_rejections: int = 0
    invalid_sessions: int = 0
    disconnects: int = 0
    requests: dict[str, int] = field(default_factory=dict)
    native_commands: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=MAX_EVIDENCE_RECORDS)
    )
    leases_granted: int = 0
    leases_released: int = 0
    leases_reclaimed: int = 0
    active_leases: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_active_clients: int = 0
    generations: deque[int] = field(
        default_factory=lambda: deque([1], maxlen=MAX_EVIDENCE_RECORDS)
    )
    resource_rejections: int = 0
    protocol_errors: int = 0
    first_protocol_error: str | None = None
    teardown_failures: int = 0
    first_teardown_failure: str | None = None
    evidence_overflow: bool = False

    def _reserve(self, collection: Any, limit: int = MAX_EVIDENCE_RECORDS) -> bool:
        if len(collection) < limit:
            return True
        self.evidence_overflow = True
        return False

    def record_request(self, method: str) -> bool:
        with self.lock:
            if method not in self.requests and not self._reserve(
                self.requests, MAX_REQUEST_METHODS
            ):
                return False
            self.requests[method] = self.requests.get(method, 0) + 1
            return True

    def record_protocol_error(self, error: BaseException) -> None:
        with self.lock:
            self.protocol_errors += 1
            if self.first_protocol_error is None:
                self.first_protocol_error = bounded_diagnostic(error)

    def record_teardown_failure(self, message: str) -> None:
        with self.lock:
            self.teardown_failures += 1
            if self.first_teardown_failure is None:
                self.first_teardown_failure = bounded_diagnostic(message)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "accepted_client_ids": list(self.accepted_client_ids),
                "client_names": dict(self.client_names),
                "active_client_ids": sorted(self.active_client_ids),
                "active_identity_keys": sorted(self.active_identity_keys),
                "duplicate_rejections": self.duplicate_rejections,
                "feature_rejections": self.feature_rejections,
                "invalid_sessions": self.invalid_sessions,
                "disconnects": self.disconnects,
                "requests": dict(sorted(self.requests.items())),
                "native_commands": list(self.native_commands),
                "leases_granted": self.leases_granted,
                "leases_released": self.leases_released,
                "leases_reclaimed": self.leases_reclaimed,
                "active_leases": dict(self.active_leases),
                "max_active_clients": self.max_active_clients,
                "generations": list(self.generations),
                "resource_rejections": self.resource_rejections,
                "protocol_errors": self.protocol_errors,
                "first_protocol_error": self.first_protocol_error,
                "teardown_failures": self.teardown_failures,
                "first_teardown_failure": self.first_teardown_failure,
                "evidence_overflow": self.evidence_overflow,
            }


class SyntheticNativeBridge:
    def __init__(
        self,
        evidence: BridgeEvidence,
        *,
        server_epoch: int = 1,
        generation: int = 1,
        collapse_feature_identity: bool = False,
        enabled_features: set[str] | None = None,
        socket_path: Path = SOCKET_PATH,
        socket_owner_uid: int | None = None,
        reject_leases: bool = False,
    ) -> None:
        self.evidence = evidence
        self.server_epoch = server_epoch
        self.generation = generation
        self.collapse_feature_identity = collapse_feature_identity
        self.enabled_features = set(
            REQUIRED_FEATURES if enabled_features is None else enabled_features
        )
        self.socket_path = socket_path
        self.socket_owner_uid = socket_owner_uid
        self.reject_leases = reject_leases
        self.sequence = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_BRIDGE_EVENTS)
        self.dropped_events = 0
        self._listener: socket.socket | None = None
        self._connections: dict[int, socket.socket] = {}
        self._sessions: dict[int, Session] = {}
        self._accept_thread: threading.Thread | None = None
        self._client_threads: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connection_sequence = 0
        self._lease_sequence = 0

    @property
    def server_instance_id(self) -> str:
        return f"synthetic-bridge-instance-{self.server_epoch}"

    def routes(self) -> list[dict[str, Any]]:
        return [
            route(
                self.generation,
                self.server_epoch,
                "keyboard-0296",
                "keyboard-feature",
                "keyboard",
                0x0296,
            ),
            route(
                self.generation,
                self.server_epoch,
                "mouse-00cd",
                "mouse-feature",
                "mouse",
                0x00CD,
            ),
        ]

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        socket_identity = self.socket_path.stat()
        if self.socket_owner_uid is not None and (
            socket_identity.st_uid != self.socket_owner_uid
            or socket_identity.st_gid != self.socket_owner_uid
        ):
            os.chown(self.socket_path, self.socket_owner_uid, self.socket_owner_uid)
        os.chmod(
            self.socket_path,
            0o600 if self.socket_owner_uid not in (None, 0) else 0o660,
        )
        listener.listen(16)
        listener.settimeout(0.1)
        self._listener = listener
        self._stop.clear()
        thread = threading.Thread(target=self._accept_loop, name="synthetic-bridge", daemon=True)
        self._accept_thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError as error:
                self.evidence.record_teardown_failure(
                    f"synthetic bridge listener cleanup failed: {error}"
                )
        accept_thread = self._accept_thread
        self._accept_thread = None
        if accept_thread is not None:
            accept_thread.join(timeout=2)
            if accept_thread.is_alive():
                self.evidence.record_teardown_failure(
                    "synthetic bridge accept worker exceeded its stop bound"
                )
        with self._lock:
            connections = list(self._connections.values())
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError as error:
                self.evidence.record_teardown_failure(
                    f"synthetic bridge connection cleanup failed: {error}"
                )
        with self._lock:
            client_threads = list(self._client_threads)
        for thread in client_threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in client_threads):
            self.evidence.record_teardown_failure(
                "synthetic bridge client worker exceeded its stop bound"
            )
        with self._lock:
            self._client_threads = {
                thread for thread in self._client_threads if thread.is_alive()
            }
        if self.socket_path.exists() or self.socket_path.is_symlink():
            try:
                self.socket_path.unlink()
            except OSError as error:
                self.evidence.record_teardown_failure(
                    f"synthetic bridge socket cleanup failed: {error}"
                )

    def bump_generation(self, generation: int) -> None:
        require(generation > self.generation, "route generation must increase")
        self.generation = generation
        with self.evidence.lock:
            if self.evidence._reserve(self.evidence.generations):
                self.evidence.generations.append(generation)
        for current in self.routes():
            self.sequence += 1
            if len(self.events) == MAX_BRIDGE_EVENTS:
                self.dropped_events += 1
            self.events.append(
                {
                    "sequence": str(self.sequence),
                    "kind": "generation-replaced",
                    "route": current,
                }
            )

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                assert self._listener is not None
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            connection.settimeout(6)
            over_capacity = False
            with self._lock:
                self._client_threads = {
                    thread for thread in self._client_threads if thread.is_alive()
                }
                if (
                    len(self._connections) >= MAX_ACTIVE_CONNECTIONS
                    or len(self._client_threads) >= MAX_ACTIVE_CONNECTIONS
                ):
                    over_capacity = True
                    connection_id = 0
                else:
                    self._connection_sequence += 1
                    connection_id = self._connection_sequence
                    self._connections[connection_id] = connection
            if over_capacity:
                connection.close()
                with self.evidence.lock:
                    self.evidence.resource_rejections += 1
                    self.evidence.evidence_overflow = True
                continue
            thread = threading.Thread(
                target=self._serve,
                args=(connection_id, connection),
                name=f"synthetic-client-{connection_id}",
                daemon=True,
            )
            with self._lock:
                self._client_threads.add(thread)
            thread.start()

    def _error(
        self,
        request_id: str | None,
        kind: str,
        message: str,
        finding_id: str,
    ) -> dict[str, Any]:
        return {
            "type": "error",
            "response": {
                "request_id": request_id,
                "server_instance_id": self.server_instance_id,
                "error": {
                    "request_id": request_id,
                    "kind": kind,
                    "message": message,
                    "finding_id": finding_id,
                },
            },
        }

    def _success(
        self, response_type: str, request_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "type": response_type,
            "response": {
                "request_id": request_id,
                "server_instance_id": self.server_instance_id,
                "result": result,
            },
        }

    def _identity_key(self, client_id: str, client_name: str) -> str:
        if self.collapse_feature_identity and client_name in {
            "OpenRGB HyperFlux native transport",
            "HyperFlux OpenRazer native relay",
        }:
            return "historical-constant-feature-client"
        return client_id

    def _require_bounded_text(self, value: str, label: str) -> None:
        if not 0 < len(value.encode("utf-8")) <= MAX_CLIENT_FIELD_BYTES:
            with self.evidence.lock:
                self.evidence.evidence_overflow = True
            raise QualificationFailure(f"{label} exceeds its bound")

    def _negotiate(
        self, connection_id: int, request: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        envelope = request.get("request")
        require(isinstance(envelope, dict), "negotiate envelope is malformed")
        request_id = envelope.get("request_id")
        params = envelope.get("params")
        require(isinstance(request_id, str), "negotiate request identity is malformed")
        require(isinstance(params, dict), "negotiate parameters are malformed")
        client_id = params.get("client_id")
        client_name = params.get("client_name")
        required = params.get("required_features")
        require(isinstance(client_id, str), "client identity is malformed")
        require(isinstance(client_name, str), "client name is malformed")
        self._require_bounded_text(client_id, "client identity")
        self._require_bounded_text(client_name, "client name")
        require(isinstance(required, list) and all(isinstance(v, str) for v in required),
                "required feature list is malformed")
        missing = set(required) - self.enabled_features
        if missing:
            with self.evidence.lock:
                self.evidence.feature_rejections += 1
            return self._error(
                request_id,
                "unsupported-feature",
                "synthetic bridge omitted a required feature",
                "HFX-PROFILE-001",
            ), False

        identity_key = self._identity_key(client_id, client_name)
        with self.evidence.lock:
            if identity_key in self.evidence.active_identity_keys:
                self.evidence.duplicate_rejections += 1
                return self._error(
                    request_id,
                    "ownership-conflict",
                    "synthetic bridge rejected a duplicate active client identity",
                    "HFX-OWNERSHIP-002",
                ), False
            if (
                len(self.evidence.active_client_ids) >= MAX_ACTIVE_CONNECTIONS
                or len(self.evidence.active_identity_keys) >= MAX_ACTIVE_CONNECTIONS
                or not self.evidence._reserve(self.evidence.accepted_client_ids)
                or not self.evidence._reserve(self.evidence.client_names)
            ):
                self.evidence.resource_rejections += 1
                self.evidence.evidence_overflow = True
                return self._error(
                    request_id,
                    "resource-exhausted",
                    "synthetic bridge client evidence reached its bound",
                    "HFX-REQUEST-001",
                ), False
            self.evidence.active_identity_keys.add(identity_key)
            self.evidence.active_client_ids.add(client_id)
            if self.evidence._reserve(self.evidence.accepted_client_ids):
                self.evidence.accepted_client_ids.append(client_id)
            if self.evidence._reserve(self.evidence.client_names):
                self.evidence.client_names[client_id] = client_name
            self.evidence.max_active_clients = max(
                self.evidence.max_active_clients,
                len(self.evidence.active_client_ids),
            )

        protocol_session_id = f"protocol-session-{self.server_epoch}-{connection_id}"
        negotiation_token = token(
            f"negotiation:{self.server_epoch}:{connection_id}:{client_id}"
        )
        with self._lock:
            if len(self._sessions) >= MAX_ACTIVE_CONNECTIONS:
                with self.evidence.lock:
                    self.evidence.evidence_overflow = True
                raise QualificationFailure(
                    "synthetic bridge session map reached its bound"
                )
            self._sessions[connection_id] = Session(
                connection_id,
                client_id,
                client_name,
                identity_key,
                protocol_session_id,
                negotiation_token,
            )
        return self._success(
            "negotiate-success",
            request_id,
            {
                "selected_version": 8,
                "server_instance_id": self.server_instance_id,
                "protocol_session_id": protocol_session_id,
                "negotiation_token": negotiation_token,
                "bridge_version": "0.0.0-dev.1",
                "enabled_features": sorted(self.enabled_features),
                "event_buffer_capacity": 64,
            },
        ), True

    def _validated_session(
        self, connection_id: int, request: dict[str, Any]
    ) -> tuple[Session | None, dict[str, Any], str]:
        envelope = request.get("request")
        require(isinstance(envelope, dict), "session envelope is malformed")
        request_id = envelope.get("request_id")
        require(isinstance(request_id, str), "session request identity is malformed")
        with self._lock:
            session = self._sessions.get(connection_id)
        if (
            session is None
            or envelope.get("protocol_session_id") != session.protocol_session_id
            or envelope.get("negotiation_token") != session.negotiation_token
        ):
            with self.evidence.lock:
                self.evidence.invalid_sessions += 1
            return None, envelope, request_id
        return session, envelope, request_id

    def _inventory(self, request_id: str) -> dict[str, Any]:
        return self._success(
            "native-enumerate-success",
            request_id,
            {
                "cursor": cursor(self.server_epoch, self.sequence),
                "routes": self.routes(),
            },
        )

    def _watch(
        self, session: Session, envelope: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        params = envelope.get("params")
        require(isinstance(params, dict), "native watch parameters are malformed")
        subscription_id = params.get("subscription_id")
        if subscription_id is None:
            if len(session.subscriptions) >= MAX_SUBSCRIPTIONS_PER_SESSION:
                with self.evidence.lock:
                    self.evidence.evidence_overflow = True
                raise QualificationFailure("native subscription set reached its bound")
            subscription_id = f"native-subscription-{self.server_epoch}-{session.connection_id}"
            session.subscriptions.add(subscription_id)
        require(
            isinstance(subscription_id, str) and subscription_id in session.subscriptions,
            "native subscription identity is malformed",
        )
        expected = params.get("expected_cursor")
        require(isinstance(expected, dict), "native watch cursor is malformed")
        expected_sequence = int(expected.get("sequence", "-1"))
        pending = [event for event in self.events if int(event["sequence"]) > expected_sequence]
        limit = params.get("max_events")
        require(isinstance(limit, int) and 1 <= limit <= 256, "native watch limit is malformed")
        selected = pending[:limit]
        next_sequence = int(selected[-1]["sequence"]) if selected else expected_sequence
        oldest = int(self.events[0]["sequence"]) if self.events else self.sequence
        return self._success(
            "native-watch-success",
            request_id,
            {
                "subscription_id": subscription_id,
                "next_cursor": cursor(self.server_epoch, next_sequence),
                "events": selected,
                "oldest_available": str(oldest),
                "latest_available": str(self.sequence),
                "dropped_events": str(self.dropped_events),
                "cursor_gap": expected_sequence < oldest and self.dropped_events > 0,
                "has_more": len(pending) > len(selected),
            },
        )

    def _acquire(
        self, session: Session, envelope: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        params = envelope.get("params")
        require(isinstance(params, dict), "lease request parameters are malformed")
        require(params.get("request_id") == request_id, "lease request identity diverged")
        require(params.get("client_id") == session.client_id, "lease client identity diverged")
        resources = params.get("resources")
        require(isinstance(resources, list) and len(resources) == 1, "lease resources are malformed")
        requested = resources[0]
        require(
            requested in [item["resource"] for item in self.routes()],
            "lease resource is not an active synthetic route",
        )
        with self.evidence.lock:
            if self.reject_leases:
                result = {
                    "outcome": "rejected",
                    "detail": {
                        "error_kind": "ownership-conflict",
                        "finding_id": "HFX-OWNERSHIP-002",
                    },
                }
                return self._success("acquire-lease-success", request_id, result)
            conflict = next(
                (
                    grant
                    for grant in self.evidence.active_leases.values()
                    if grant["resources"] == resources and grant["client_id"] != session.client_id
                ),
                None,
            )
            if conflict is not None:
                result = {
                    "outcome": "conflict",
                    "detail": {
                        "conflicting_client": conflict["client_id"],
                        "conflicting_resource": requested,
                    },
                }
            else:
                if not self.evidence._reserve(
                    self.evidence.active_leases, MAX_ACTIVE_LEASES
                ):
                    self.evidence.resource_rejections += 1
                    result = {
                        "outcome": "rejected",
                        "detail": {
                            "error_kind": "resource-exhausted",
                            "finding_id": "HFX-REQUEST-001",
                        },
                    }
                    return self._success("acquire-lease-success", request_id, result)
                self._lease_sequence += 1
                lease_id = f"native-lease-{self.server_epoch}-{self._lease_sequence}"
                grant = {
                    "lease_id": lease_id,
                    "client_id": session.client_id,
                    "resources": resources,
                    "expires_at_ms": str(10_000_000 + self._lease_sequence),
                    "state": "granted",
                }
                self.evidence.active_leases[lease_id] = grant
                self.evidence.leases_granted += 1
                result = {"outcome": "granted", "detail": grant}
        return self._success("acquire-lease-success", request_id, result)

    def _release(
        self, session: Session, envelope: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        params = envelope.get("params")
        require(isinstance(params, dict), "lease release parameters are malformed")
        lease_id = params.get("lease_id")
        require(params.get("request_id") == request_id, "release request identity diverged")
        require(params.get("client_id") == session.client_id, "release client identity diverged")
        with self.evidence.lock:
            grant = self.evidence.active_leases.pop(str(lease_id), None)
            require(grant is not None, "lease release names an unknown lease")
            require(grant["client_id"] == session.client_id, "lease release owner diverged")
            self.evidence.leases_released += 1
            released = dict(grant)
            released["state"] = "released"
        return self._success(
            "release-lease-success",
            request_id,
            {"outcome": "granted", "detail": released},
        )

    def _response_report(self, route_token: str, report_hex: str) -> str:
        report = bytearray.fromhex(report_hex)
        require(len(report) == 90, "native report must contain exactly 90 bytes")
        report[0] = 0x02
        command_class = report[6]
        command_id = report[7]
        require(command_id & 0x80 != 0, "isolated enumeration attempted a write command")
        matched_route = next(
            (item for item in self.routes() if item["route_token"] == route_token),
            None,
        )
        require(matched_route is not None, "native response route token is unknown")
        device = matched_route["device_kind"]
        if command_class == 0x00 and command_id == 0x81:
            report[8:10] = bytes([3, 7])
        elif command_class == 0x00 and command_id == 0x82:
            serial = (
                b"HFX-MOUSE-000000000001"
                if device == "mouse"
                else b"HFX-KEYBD-000000000001"
            )
            require(len(serial) == 22, "synthetic serial must be exactly 22 bytes")
            report[8:30] = serial
        elif command_class == 0x00 and command_id == 0x86:
            report[8:10] = bytes([1, 0])
        elif command_class == 0x00 and command_id == 0x84:
            report[8:10] = bytes([3, 0])
        report[88] = 0
        for value in report[2:88]:
            report[88] ^= value
        with self.evidence.lock:
            if self.evidence._reserve(self.evidence.native_commands):
                self.evidence.native_commands.append(
                    {
                        "device": device,
                        "command_class": command_class,
                        "command_id": command_id,
                        "direction": "read",
                    }
                )
        return report.hex()

    def _transact(
        self, session: Session, envelope: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        params = envelope.get("params")
        require(isinstance(params, dict), "native transaction parameters are malformed")
        require(params.get("request_id") == request_id, "native transaction identity diverged")
        require(params.get("client_id") == session.client_id, "native transaction client diverged")
        lease_id = params.get("lease_id")
        route_token = params.get("route_token")
        report_hex = params.get("report")
        require(isinstance(route_token, str), "native transaction token is malformed")
        require(isinstance(report_hex, str), "native transaction report is malformed")
        with self.evidence.lock:
            grant = self.evidence.active_leases.get(str(lease_id))
            require(grant is not None, "native transaction has no active lease")
            require(grant["client_id"] == session.client_id, "native transaction lease owner diverged")
        active_tokens = {item["route_token"] for item in self.routes()}
        require(route_token in active_tokens, "native transaction route token is stale")
        response = self._response_report(route_token, report_hex)
        return self._success(
            "native-transact-success",
            request_id,
            {
                "request_id": request_id,
                "route_token": route_token,
                "result": {
                    "certainty": "matched-response",
                    "detail": {"response": response},
                },
            },
        )

    def _dispatch(self, connection_id: int, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        method = request.get("method")
        require(isinstance(method, str), "synthetic bridge method is malformed")
        self._require_bounded_text(method, "synthetic bridge method")
        require(
            self.evidence.record_request(method),
            "synthetic bridge request evidence reached its bound",
        )
        if method == "negotiate":
            with self._lock:
                already_negotiated = connection_id in self._sessions
            require(not already_negotiated, "connection negotiated more than once")
            return self._negotiate(connection_id, request)

        session, envelope, request_id = self._validated_session(connection_id, request)
        if session is None:
            return self._error(
                request_id,
                "ownership-conflict",
                "synthetic bridge rejected an invalid session binding",
                "HFX-OWNERSHIP-001",
            ), False
        if method == "native-enumerate":
            return self._inventory(request_id), True
        if method == "native-watch":
            return self._watch(session, envelope, request_id), True
        if method == "acquire-lease":
            return self._acquire(session, envelope, request_id), True
        if method == "release-lease":
            return self._release(session, envelope, request_id), True
        if method == "native-transact":
            return self._transact(session, envelope, request_id), True
        return self._error(
            request_id,
            "unsupported-feature",
            "synthetic bridge does not implement this method",
            "HFX-PROFILE-001",
        ), True

    def _cleanup(self, connection_id: int) -> None:
        with self._lock:
            session = self._sessions.pop(connection_id, None)
        with self.evidence.lock:
            if session is not None:
                self.evidence.active_client_ids.discard(session.client_id)
                self.evidence.active_identity_keys.discard(session.identity_key)
                owned = [
                    lease_id
                    for lease_id, grant in self.evidence.active_leases.items()
                    if grant["client_id"] == session.client_id
                ]
                for lease_id in owned:
                    del self.evidence.active_leases[lease_id]
                    self.evidence.leases_reclaimed += 1
            self.evidence.disconnects += 1
        with self._lock:
            connection = self._connections.pop(connection_id, None)
        if connection is not None:
            connection.close()

    def _serve(self, connection_id: int, connection: socket.socket) -> None:
        try:
            keep_open = True
            while keep_open and not self._stop.is_set():
                request = read_frame(connection)
                response, keep_open = self._dispatch(connection_id, request)
                write_frame(connection, response)
        except (EOFError, OSError, socket.timeout):
            pass
        except (QualificationFailure, ValueError, json.JSONDecodeError) as error:
            self.evidence.record_protocol_error(error)
            try:
                write_frame(
                    connection,
                    self._error(None, "invalid-request", str(error), "HFX-REQUEST-001"),
                )
            except OSError:
                pass
        finally:
            self._cleanup(connection_id)


class HarnessProcess:
    def __init__(
        self,
        binary: Path,
        environment: dict[str, str],
        runtime: str = "system",
    ) -> None:
        self.process = subprocess.Popen(
            harness_command(binary, runtime),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=environment,
        )
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_HARNESS_EVENTS)
        self.stdout_lines: deque[str] = deque(maxlen=MAX_DIAGNOSTIC_LINES)
        self.stderr_lines: deque[str] = deque(maxlen=MAX_DIAGNOSTIC_LINES)
        self._event_overflow = threading.Event()
        self._diagnostic_truncation = threading.Event()
        self._cleanup_failure: str | None = None
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name="openrgb-harness-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="openrgb-harness-stderr", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        decoder = json.JSONDecoder()
        while True:
            result = read_bounded_line(self.process.stdout)
            if result is None:
                return
            line, truncated = result
            if truncated:
                self._diagnostic_truncation.set()
            self.stdout_lines.append(line)
            if truncated:
                continue
            event_offset = line.find('{"event":')
            if event_offset < 0:
                continue
            try:
                event, _ = decoder.raw_decode(line[event_offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and isinstance(event.get("event"), str):
                try:
                    self.events.put_nowait(event)
                except queue.Full:
                    self._event_overflow.set()

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        while True:
            result = read_bounded_line(self.process.stderr)
            if result is None:
                return
            line, truncated = result
            if truncated:
                self._diagnostic_truncation.set()
            self.stderr_lines.append(line)

    def _require_bounded_output(self) -> None:
        require(not self._event_overflow.is_set(), "OpenRGB harness event queue overflowed")
        require(
            not self._diagnostic_truncation.is_set(),
            "OpenRGB harness emitted an overlong diagnostic line",
        )

    def wait_event(self, event_name: str, timeout: float = 12) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._require_bounded_output()
            if self.process.poll() is not None:
                raise QualificationFailure(
                    f"OpenRGB harness exited while waiting for {event_name}: "
                    + "\n".join(list(self.stderr_lines)[-20:])
                )
            try:
                event = self.events.get(timeout=min(0.1, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if event.get("event") == event_name:
                return event
        diagnostics = "\n".join(
            [
                "stdout tail:",
                *list(self.stdout_lines)[-20:],
                "stderr tail:",
                *list(self.stderr_lines)[-20:],
            ]
        )
        raise QualificationFailure(
            f"timed out waiting for OpenRGB harness event {event_name}:\n{diagnostics}"
        )

    def command(self, command: str, event_name: str, timeout: float = 12) -> dict[str, Any]:
        require(self.process.stdin is not None, "OpenRGB harness stdin is unavailable")
        self.process.stdin.write((command + "\n").encode("ascii"))
        self.process.stdin.flush()
        return self.wait_event(event_name, timeout)

    def snapshot(self) -> dict[str, Any]:
        return self.command("snapshot", "snapshot")

    def registry_trace(self) -> dict[str, Any]:
        return self.command("registry-trace", "registry-trace")

    def wait_ready(self) -> dict[str, Any]:
        event = self.wait_event("ready")
        require(
            event.get("system_plugin_directory")
            == str(SYSTEM_OPENRGB_PLUGIN_DIRECTORY),
            "OpenRGB harness system plugin directory is not source-bound",
        )
        return event

    def wait_controllers(
        self,
        count: int,
        *,
        generation: int | None = None,
        timeout: float = 15,
    ) -> dict[str, Any]:
        started = time.monotonic()
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            latest = self.snapshot()
            controllers = latest.get("controllers")
            if isinstance(controllers, list) and len(controllers) == count:
                if generation is None or all(
                    f"/generation/{generation}/" in item.get("location", "")
                    for item in controllers
                ):
                    latest["qualification_convergence_ms"] = round(
                        (time.monotonic() - started) * 1_000
                    )
                    require(
                        latest["qualification_convergence_ms"]
                        <= MAX_CONTROLLER_CONVERGENCE_MS,
                        "OpenRGB controller convergence exceeded its measured bound",
                    )
                    return latest
            time.sleep(0.05)
        diagnostics = "\n".join(
            [
                "stdout tail:",
                *list(self.stdout_lines)[-20:],
                "stderr tail:",
                *list(self.stderr_lines)[-20:],
            ]
        )
        raise QualificationFailure(
            f"OpenRGB registry did not converge to {count} controllers: {latest}\n"
            f"{diagnostics}"
        )

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.command("quit", "stopped", 8)
            except (BrokenPipeError, QualificationFailure) as error:
                self._cleanup_failure = bounded_diagnostic(
                    f"graceful OpenRGB harness shutdown failed: {error}"
                )
                try:
                    self.process.terminate()
                except OSError as error:
                    self._cleanup_failure = bounded_diagnostic(error)
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._cleanup_failure = "OpenRGB harness exceeded its graceful stop bound"
            try:
                self.process.kill()
                self.process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError) as error:
                self._cleanup_failure = bounded_diagnostic(error)
        except OSError as error:
            self._cleanup_failure = bounded_diagnostic(error)
        self._stdout_thread.join(timeout=1)
        self._stderr_thread.join(timeout=1)
        if self._stdout_thread.is_alive() or self._stderr_thread.is_alive():
            self._cleanup_failure = "OpenRGB harness diagnostic worker exceeded its stop bound"

    def require_clean(self) -> None:
        self._require_bounded_output()
        require(
            self._cleanup_failure is None,
            f"OpenRGB harness cleanup failed: {self._cleanup_failure}",
        )


def validate_controllers(event: dict[str, Any], generation: int) -> None:
    controllers = event.get("controllers")
    require(isinstance(controllers, list), "OpenRGB controller snapshot is malformed")
    require(
        len(controllers) == 2,
        "OpenRGB did not publish the complete two-device qualification fixture",
    )
    by_name = {item.get("name"): item for item in controllers}
    require(set(by_name) == set(EXPECTED_NAMES), "OpenRGB controller identities differ")
    require(len(by_name) == len(controllers), "OpenRGB published a duplicate controller")
    for name, expected in EXPECTED_NAMES.items():
        item = by_name[name]
        require(item.get("vendor") == "Razer", f"{name} vendor is incorrect")
        require(item.get("serial") == expected["serial"], f"{name} serial is incorrect")
        require(item.get("type") == expected["type"], f"{name} type is incorrect")
        require("Razer Device" in item.get("description", ""), f"{name} description is incorrect")
        location = item.get("location", "")
        require("00cf" not in location.lower(), "transport receiver leaked into OpenRGB")
        require(
            f"/generation/{generation}/device/{expected['generation_device']}/" in location,
            f"{name} route location is incorrect",
        )


def validate_registry_trace(
    event: dict[str, Any] | None,
    *,
    expected_final: int,
    allowed_counts: set[int],
    required_count: int | None = None,
) -> dict[str, Any]:
    require(isinstance(event, dict), "OpenRGB registry trace is missing")
    require(event.get("overflow") is False, "OpenRGB registry trace overflowed")
    counts = event.get("counts")
    times_ms = event.get("times_ms")
    require(
        isinstance(counts, list)
        and counts
        and all(isinstance(value, int) and value >= 0 for value in counts),
        "OpenRGB registry trace is malformed",
    )
    require(
        isinstance(times_ms, list)
        and len(times_ms) == len(counts)
        and all(isinstance(value, int) and value >= 0 for value in times_ms)
        and times_ms == sorted(times_ms),
        "OpenRGB registry timestamps are malformed",
    )
    require(counts[0] == 0, "OpenRGB registry did not begin empty")
    require(counts[-1] == expected_final, "OpenRGB registry trace ended unexpectedly")
    require(set(counts) <= allowed_counts, "OpenRGB registry published an unexpected count")
    convergence_ms = None
    if required_count is not None:
        require(required_count in counts, "OpenRGB registry never reached the eligible inventory")
        convergence_ms = times_ms[counts.index(required_count)]
        require(
            convergence_ms <= MAX_CONTROLLER_CONVERGENCE_MS,
            "OpenRGB registry did not converge within the measured bound",
        )
    return {
        "counts": counts,
        "times_ms": times_ms,
        "convergence_ms": convergence_ms,
        "overflow": False,
    }


def require_clean_bridge_evidence(state: dict[str, Any], label: str) -> None:
    require(not state["evidence_overflow"], f"{label} bridge evidence overflowed")
    require(
        state["protocol_errors"] == 0,
        f"{label} bridge observed a protocol error: {state['first_protocol_error']}",
    )
    require(
        state["teardown_failures"] == 0,
        f"{label} bridge teardown exceeded its bound: "
        f"{state['first_teardown_failure']}",
    )


def wait_for(predicate: Any, message: str, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise QualificationFailure(message)


def observe_empty_registry(
    harness: HarnessProcess,
    *,
    duration: float = 0.5,
) -> list[dict[str, Any]]:
    require(0 < duration <= 2, "negative observation duration is out of bounds")
    snapshots: list[dict[str, Any]] = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        snapshot = harness.snapshot()
        require(snapshot.get("controllers") == [], "negative case published a controller")
        snapshots.append(snapshot)
        time.sleep(0.05)
    return snapshots


def isolated_environment(root: Path, runtime: RuntimeContext) -> dict[str, str]:
    home = root / "home"
    config = root / "config"
    runtime_directory = Path(f"/run/user/{runtime.harness_uid}")
    for path in (
        home,
        config,
        config / "OpenRGB",
        config / "OpenRGB" / "plugins",
        runtime_directory,
    ):
        path.mkdir(parents=True, exist_ok=True)
    runtime_directory.chmod(0o700)
    environment = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_RUNTIME_DIR": str(runtime_directory),
        "PATH": "/usr/bin:/bin",
        "QT_QPA_PLATFORM": "offscreen",
        "QT_LOGGING_RULES": "*.debug=false",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    return environment


def install_test_files(
    root: Path,
    harness: Path,
    plugin: Path,
    runtime: RuntimeContext,
) -> tuple[Path, Path]:
    binary = root / "bin" / "openrgb-plugin-manager-sdk-harness"
    plugin_target = root / "config" / "OpenRGB" / "plugins" / "hyperflux-next-openrgb.so"
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(harness, binary)
    shutil.copy2(plugin, plugin_target)
    binary.chmod(0o755)
    plugin_target.chmod(0o755)
    if runtime.harness_uid:
        for owned_root in (root / "home", root / "config", Path(environment_runtime(runtime))):
            os.chown(owned_root, runtime.harness_uid, runtime.harness_uid)
            for path in owned_root.rglob("*"):
                os.chown(path, runtime.harness_uid, runtime.harness_uid)
    return binary, plugin_target


def environment_runtime(runtime: RuntimeContext) -> str:
    return f"/run/user/{runtime.harness_uid}"


def synthetic_bridge(
    evidence: BridgeEvidence,
    runtime: RuntimeContext,
    **options: Any,
) -> SyntheticNativeBridge:
    return SyntheticNativeBridge(
        evidence,
        socket_path=runtime.socket_path,
        socket_owner_uid=runtime.harness_uid,
        **options,
    )


def run_positive_case(
    binary: Path,
    environment: dict[str, str],
    runtime: RuntimeContext,
) -> dict[str, Any]:
    evidence = BridgeEvidence()
    bridge = synthetic_bridge(evidence, runtime)
    bridge.start()
    harness: HarnessProcess | None = None
    snapshots: list[dict[str, Any]] = []
    registry_trace: dict[str, Any] | None = None
    try:
        harness = HarnessProcess(binary, environment, runtime.mode)
        harness.wait_ready()
        first = harness.wait_controllers(2, generation=1)
        validate_controllers(first, 1)
        snapshots.append(first)

        sdk = harness.command("sdk-snapshot", "sdk-snapshot", 20)
        validate_controllers(sdk, 1)
        snapshots.append(sdk)

        harness.command("rescan", "rescan-complete", 12)
        rescanned = harness.wait_controllers(2, generation=1)
        validate_controllers(rescanned, 1)
        snapshots.append(rescanned)

        bridge.bump_generation(2)
        replaced = harness.wait_controllers(2, generation=2)
        validate_controllers(replaced, 2)
        snapshots.append(replaced)

        bridge.stop()
        wait_for(
            lambda: not evidence.snapshot()["active_client_ids"],
            "socket loss leaked a synthetic bridge client",
        )
        during_loss = harness.wait_controllers(2, generation=2)
        validate_controllers(during_loss, 2)
        snapshots.append(during_loss)

        bridge = synthetic_bridge(
            evidence,
            runtime,
            server_epoch=2,
            generation=3,
        )
        with evidence.lock:
            if evidence._reserve(evidence.generations):
                evidence.generations.append(3)
        bridge.start()
        reconnected = harness.wait_controllers(2, generation=3, timeout=20)
        validate_controllers(reconnected, 3)
        snapshots.append(reconnected)

        for _ in range(3):
            harness.command("unload", "unloaded", 8)
            harness.wait_controllers(0, timeout=8)
            wait_for(
                lambda: not evidence.snapshot()["active_client_ids"],
                "plugin unload leaked a synthetic bridge client",
            )
            harness.command("load", "loaded", 8)
            loaded = harness.wait_controllers(2, generation=3, timeout=15)
            validate_controllers(loaded, 3)
            snapshots.append(loaded)

        bridge.stop()
        wait_for(
            lambda: not evidence.snapshot()["active_client_ids"],
            "reconnect shutdown leaked a synthetic bridge client",
        )
        started = time.monotonic()
        harness.command("unload", "unloaded", 3)
        require(time.monotonic() - started < 3, "plugin unload blocked during reconnect")
        harness.wait_controllers(0, timeout=3)
        registry_trace = harness.registry_trace()
    except QualificationFailure as error:
        raise QualificationFailure(
            f"{error}\nbridge state: {json.dumps(evidence.snapshot(), sort_keys=True)}"
        ) from error
    finally:
        try:
            if harness is not None:
                harness.close()
        finally:
            bridge.stop()

    require(harness is not None, "positive OpenRGB harness was not created")
    harness.require_clean()
    state = evidence.snapshot()
    require(not state["active_client_ids"], "positive case leaked client identities")
    require(not state["active_leases"], "positive case leaked ownership leases")
    require(
        state["leases_granted"]
        == state["leases_released"] + state["leases_reclaimed"],
        "lease lifecycle diverged",
    )
    require(state["duplicate_rejections"] == 0, "unique client identities were rejected")
    require_clean_bridge_evidence(state, "positive")
    require(
        len(state["accepted_client_ids"]) == len(set(state["accepted_client_ids"])),
        "a process-wide OpenRGB client identity was reused",
    )
    # Generation replacement briefly overlaps discovery with the prepared
    # replacement pair; teardown must still return every client to zero.
    require(
        state["max_active_clients"] == 5,
        "unexpected concurrent OpenRGB client count: "
        f"{state['max_active_clients']} ({state['client_names']})",
    )
    require(state["requests"].get("native-transact", 0) >= 6, "upstream constructors were not exercised")
    require(
        all(command["direction"] == "read" for command in state["native_commands"]),
        "isolated enumeration emitted a write command",
    )
    validated_trace = validate_registry_trace(
        registry_trace,
        expected_final=0,
        allowed_counts={0, 1, 2},
        required_count=2,
    )
    return {
        "result": "pass",
        "snapshots": snapshots,
        "registry_trace": validated_trace,
        "bridge": state,
        "harness_stderr": list(harness.stderr_lines),
    }


def run_duplicate_case(
    binary: Path,
    environment: dict[str, str],
    runtime: RuntimeContext,
) -> dict[str, Any]:
    evidence = BridgeEvidence()
    bridge = synthetic_bridge(evidence, runtime, collapse_feature_identity=True)
    bridge.start()
    harness: HarnessProcess | None = None
    sdk_snapshot: dict[str, Any] | None = None
    registry_trace: dict[str, Any] | None = None
    snapshots: list[dict[str, Any]] = []
    try:
        harness = HarnessProcess(binary, environment, runtime.mode)
        harness.wait_ready()
        harness.wait_controllers(0, timeout=8)
        wait_for(
            lambda: evidence.snapshot()["duplicate_rejections"] >= 1,
            "historical constant identity was not rejected",
        )
        snapshots = observe_empty_registry(harness)
        sdk_snapshot = harness.command("sdk-snapshot", "sdk-snapshot", 20)
        require(sdk_snapshot.get("controllers") == [], "duplicate case leaked through SDK")
        state = evidence.snapshot()
        active_feature_clients = [
            client_id
            for client_id in state["active_client_ids"]
            if state["client_names"].get(client_id) == "OpenRGB HyperFlux native transport"
        ]
        require(not active_feature_clients, "preparation failure leaked a feature session")
        require(not state["active_leases"], "preparation failure leaked an ownership lease")
        harness.command("unload", "unloaded", 8)
        harness.wait_controllers(0, timeout=3)
        registry_trace = harness.registry_trace()
    finally:
        try:
            if harness is not None:
                harness.close()
        finally:
            bridge.stop()
    require(harness is not None, "duplicate OpenRGB harness was not created")
    harness.require_clean()
    state = evidence.snapshot()
    require(not state["active_client_ids"], "duplicate case leaked client identities")
    require(not state["active_leases"], "duplicate case leaked ownership leases")
    require_clean_bridge_evidence(state, "duplicate")
    validated_trace = validate_registry_trace(
        registry_trace,
        expected_final=0,
        allowed_counts={0},
    )
    return {
        "result": "pass",
        "snapshots": snapshots,
        "sdk_snapshot": sdk_snapshot,
        "registry_trace": validated_trace,
        "bridge": state,
        "harness_stderr": list(harness.stderr_lines),
    }


def run_missing_feature_case(
    binary: Path,
    environment: dict[str, str],
    runtime: RuntimeContext,
    *,
    enabled_features: set[str],
    missing_feature: str,
) -> dict[str, Any]:
    evidence = BridgeEvidence()
    bridge = synthetic_bridge(
        evidence,
        runtime,
        enabled_features=enabled_features,
    )
    bridge.start()
    harness: HarnessProcess | None = None
    sdk_snapshot: dict[str, Any] | None = None
    registry_trace: dict[str, Any] | None = None
    snapshots: list[dict[str, Any]] = []
    try:
        harness = HarnessProcess(binary, environment, runtime.mode)
        harness.wait_ready()
        harness.wait_controllers(0, timeout=5)
        wait_for(
            lambda: evidence.snapshot()["feature_rejections"] >= 1,
            f"missing {missing_feature} feature was not rejected",
        )
        snapshots = observe_empty_registry(harness)
        sdk_snapshot = harness.command("sdk-snapshot", "sdk-snapshot", 20)
        require(sdk_snapshot.get("controllers") == [], "feature case leaked through SDK")
        harness.command("unload", "unloaded", 8)
        harness.wait_controllers(0, timeout=3)
        registry_trace = harness.registry_trace()
    finally:
        try:
            if harness is not None:
                harness.close()
        finally:
            bridge.stop()
    require(harness is not None, "feature OpenRGB harness was not created")
    harness.require_clean()
    state = evidence.snapshot()
    require(not state["active_client_ids"], "feature rejection leaked client identities")
    require(not state["active_leases"], "feature rejection leaked ownership leases")
    require_clean_bridge_evidence(state, "feature")
    validated_trace = validate_registry_trace(
        registry_trace,
        expected_final=0,
        allowed_counts={0},
    )
    return {
        "result": "pass",
        "missing_feature": missing_feature,
        "snapshots": snapshots,
        "sdk_snapshot": sdk_snapshot,
        "registry_trace": validated_trace,
        "bridge": state,
        "harness_stderr": list(harness.stderr_lines),
    }


def install_isolated_bridge_account() -> None:
    source = Path("/etc/passwd").read_text(encoding="utf-8")
    require(len(source.encode("utf-8")) <= 262_144, "account database exceeds its bound")
    records = [
        line
        for line in source.splitlines()
        if line and not line.startswith("hyperflux-next:")
    ]
    records.append("hyperflux-next:x:0:0:HyperFlux test bridge:/:/usr/bin/nologin")
    target = Path(tempfile.mkdtemp(prefix="hfx-openrgb-passwd-")) / "passwd"
    target.write_text("\n".join(records) + "\n", encoding="utf-8")
    target.chmod(0o444)
    subprocess.run(["mount", "--bind", str(target), "/etc/passwd"], check=True)


def mount_isolated_runtime() -> bool:
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    install_isolated_bridge_account()
    subprocess.run(
        ["mount", "-t", "tmpfs", "-o", "mode=0755,nosuid,nodev,noexec", "tmpfs", "/run"],
        check=True,
    )
    subprocess.run(
        ["mount", "-t", "tmpfs", "-o", "mode=0555,nosuid,nodev,noexec", "tmpfs", "/sys"],
        check=True,
    )
    system_plugins_hidden = SYSTEM_OPENRGB_PLUGIN_DIRECTORY.is_dir()
    require(
        not SYSTEM_OPENRGB_PLUGIN_DIRECTORY.is_symlink(),
        "OpenRGB system plugin directory may not be a symbolic link",
    )
    if system_plugins_hidden:
        subprocess.run(
            [
                "mount",
                "-t",
                "tmpfs",
                "-o",
                "mode=0555,nosuid,nodev,noexec",
                "tmpfs",
                str(SYSTEM_OPENRGB_PLUGIN_DIRECTORY),
            ],
            check=True,
        )
        require(
            not any(SYSTEM_OPENRGB_PLUGIN_DIRECTORY.iterdir()),
            "isolated runtime exposed a system OpenRGB plugin",
        )
    safe_dev = Path(tempfile.mkdtemp(prefix="hfx-openrgb-dev-"))
    (safe_dev / "shm").mkdir()
    for name in ("null", "zero", "random", "urandom"):
        target = safe_dev / name
        target.touch()
        subprocess.run(["mount", "--bind", f"/dev/{name}", str(target)], check=True)
    subprocess.run(
        ["mount", "-t", "tmpfs", "-o", "mode=1777,nosuid,nodev", "tmpfs", str(safe_dev / "shm")],
        check=True,
    )
    subprocess.run(["mount", "--rbind", str(safe_dev), "/dev"], check=True)
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    subprocess.run(
        ["mount", "-t", "tmpfs", "-o", "mode=0555,nosuid,nodev,noexec", "tmpfs", "/proc"],
        check=True,
    )
    require(not any(Path("/proc").iterdir()), "isolated runtime retained a process view")
    require(not Path("/dev/hidraw0").exists(), "isolated runtime exposed real hidraw")
    require(not Path("/dev/uhid").exists(), "isolated runtime exposed real UHID")
    return system_plugins_hidden


def run_inside(args: argparse.Namespace) -> int:
    require(os.geteuid() == 0, "isolated qualifier must begin as namespace root")
    if args.isolation_authority == "outer-oci-container":
        require(
            os.environ.get(OUTER_ISOLATION_ENVIRONMENT) == "1",
            "outer OCI isolation authority is not bound",
        )
    system_plugins_hidden = mount_isolated_runtime()
    require_harness_capability_drop()
    runtime = runtime_context(args.runtime)
    executable_scratch: Path | None = None
    if args.isolation_authority == "outer-oci-container":
        require(
            os.environ.get(OUTER_EXECUTABLE_SCRATCH_ENVIRONMENT)
            == str(OUTER_EXECUTABLE_SCRATCH),
            "outer OCI executable scratch authority is not bound",
        )
        require(
            OUTER_EXECUTABLE_SCRATCH.is_dir()
            and not OUTER_EXECUTABLE_SCRATCH.is_symlink(),
            "outer OCI executable scratch is unavailable",
        )
        executable_scratch = OUTER_EXECUTABLE_SCRATCH
    with tempfile.TemporaryDirectory(
        prefix="hfx-openrgb-qualification-", dir=executable_scratch
    ) as temporary:
        root = Path(temporary)
        root.chmod(0o711)
        environment = isolated_environment(root, runtime)
        binary, copied_plugin = install_test_files(root, args.harness, args.plugin, runtime)
        evidence = {
            "schema": "hyperflux-openrgb-isolated-qualification-v1",
            "isolation": {
                "authority": args.isolation_authority,
                "runtime": runtime.mode,
                "user_namespace_root": args.isolation_authority == "user-namespace",
                "container_root": args.isolation_authority == "outer-oci-container",
                "harness_uid": runtime.harness_uid,
                "harness_capability_bounding_set": "empty",
                "harness_capability_drop_preflight": "pass",
                "outer_setup_capabilities": (
                    list(OUTER_SETUP_CAPABILITIES)
                    if args.isolation_authority == "outer-oci-container"
                    else []
                ),
                "bridge_peer_uid": 0,
                "bridge_peer_account": (
                    "numeric-root" if runtime.mode == "desktop" else "hyperflux-next"
                ),
                "network_namespace": "loopback-only",
                "process_view": "empty-tmpfs",
                "sysfs": "empty-tmpfs",
                "hidraw_visible": False,
                "uhid_visible": False,
                "system_openrgb_plugins": (
                    "empty-tmpfs" if system_plugins_hidden else "directory-absent"
                ),
            },
            "inputs": {
                "openrgb_commit": args.openrgb_commit,
                "harness_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "plugin_sha256": hashlib.sha256(copied_plugin.read_bytes()).hexdigest(),
            },
            "positive": run_positive_case(binary, environment, runtime),
            "historical_constant_identity": run_duplicate_case(binary, environment, runtime),
            "missing_ownership_leases": run_missing_feature_case(
                binary,
                environment,
                runtime,
                enabled_features={"native-receiver-tunnel"},
                missing_feature="ownership-leases",
            ),
            "missing_native_receiver_tunnel": run_missing_feature_case(
                binary,
                environment,
                runtime,
                enabled_features={"ownership-leases"},
                missing_feature="native-receiver-tunnel",
            ),
        }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print("openrgb-packaged-plugin-sdk-qualification: 4/4 passed")
    print(f"evidence: {args.evidence}")
    return 0


def checked_file(path: str, label: str) -> Path:
    value = Path(path).resolve()
    require(value.is_file(), f"{label} is not a regular file: {value}")
    require(not value.is_symlink(), f"{label} may not be a symbolic link: {value}")
    return value


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", required=True)
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--openrgb-commit", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--runtime", choices=RUNTIME_MODES, required=True)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--isolation-authority",
        choices=ISOLATION_AUTHORITIES,
        default="user-namespace",
        help=argparse.SUPPRESS,
    )
    parsed = parser.parse_args()
    parsed.harness = checked_file(parsed.harness, "OpenRGB PluginManager harness")
    parsed.plugin = checked_file(parsed.plugin, "packaged OpenRGB plugin")
    require(
        len(parsed.openrgb_commit) == 40
        and all(character in "0123456789abcdef" for character in parsed.openrgb_commit),
        "OpenRGB source commit must be a lowercase full Git object ID",
    )
    parsed.evidence = Path(parsed.evidence).resolve()
    return parsed


def isolation_command(
    args: argparse.Namespace,
    *,
    pinned_container: bool | None = None,
    subordinate_ids: tuple[int, int] | None = None,
) -> tuple[str, ...]:
    if pinned_container is None:
        pinned_container = os.environ.get(PINNED_VERIFICATION_ENVIRONMENT) == "1"
    command = [
        "unshare",
        "--user",
        "--map-root-user",
    ]
    if args.runtime == "desktop":
        require(
            not pinned_container,
            "desktop runtime qualification requires host subordinate-ID mapping",
        )
        if subordinate_ids is None:
            account = pwd.getpwuid(os.getuid()).pw_name
            subordinate_ids = (
                subordinate_id_start(Path("/etc/subuid"), account),
                subordinate_id_start(Path("/etc/subgid"), account),
            )
        command.extend(
            (
                f"--map-users={DESKTOP_UID}:{subordinate_ids[0]}:1",
                f"--map-groups={DESKTOP_UID}:{subordinate_ids[1]}:1",
            )
        )
    command.extend(("--mount", "--net", "--pid", "--fork"))
    command.extend(
        (
            sys.executable,
            str(Path(__file__).resolve()),
            "--inside",
            "--isolation-authority",
            "user-namespace",
            "--harness",
            str(args.harness),
            "--plugin",
            str(args.plugin),
            "--openrgb-commit",
            args.openrgb_commit,
            "--evidence",
            str(args.evidence),
            "--runtime",
            args.runtime,
        )
    )
    return tuple(command)


def subordinate_id_start(path: Path, account: str) -> int:
    source = path.read_bytes()
    require(len(source) <= 262_144, f"{path} exceeds the qualification bound")
    for raw_line in source.decode("utf-8", errors="strict").splitlines():
        fields = raw_line.split(":")
        if len(fields) != 3 or fields[0] != account:
            continue
        start = int(fields[1], 10)
        count = int(fields[2], 10)
        require(start > 0 and count > 0, f"{path} contains an invalid subordinate range")
        return start
    raise QualificationFailure(f"{path} has no subordinate range for {account}")


def main() -> int:
    args = parse_arguments()
    if args.inside:
        return run_inside(args)
    completed = subprocess.run(isolation_command(args), check=False)
    return completed.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QualificationFailure as error:
        print(f"openrgb-packaged-plugin-sdk-qualification: FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
