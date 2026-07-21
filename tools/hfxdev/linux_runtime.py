# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import ModelError, load_json, sha256_file


ROOT_KEYS = {"$schema", "schema", "product", "bridge", "kernel", "operations"}
PRODUCT_KEYS = {"display_name", "version", "package_name", "package_release"}
BRIDGE_KEYS = {
    "service_account",
    "kernel_access_group",
    "client_group",
    "service_unit",
    "executable_path",
    "runtime_directory",
    "socket_name",
    "socket_lock_name",
    "state_directory",
    "state_file_name",
    "identity_secret_file_name",
    "configuration_directory",
    "configuration_file_name",
    "limits",
    "timing",
    "restoration",
    "configuration_max_bytes",
}
BRIDGE_LIMIT_KEYS = {
    "max_connections",
    "command_queue_capacity",
    "lease_capacity",
    "lease_history_capacity",
    "transaction_capacity",
    "event_capacity",
    "diagnostic_capacity",
    "subscription_capacity",
    "observation_batches_per_tick",
    "dispatches_per_tick",
}
BRIDGE_TIMING_KEYS = {
    "discovery_interval_ms",
    "accept_poll_interval_ms",
    "actor_response_timeout_ms",
    "kernel_session_duration_ms",
}
BRIDGE_RESTORATION_KEYS = {
    "max_pending_triggers",
    "max_pending_claims",
    "claims_per_tick",
    "retry_interval_ms",
    "lease_duration_ms",
    "authority_window_ms",
    "max_persisted_receivers",
    "max_persistence_bytes",
}
KERNEL_KEYS = {
    "module_name",
    "dkms_name",
    "device_prefix",
    "source_directory",
    "control_transfer_timeout_ms",
}
OPERATIONS_KEYS = {
    "cli_path",
    "activation_path",
    "python_module_directory",
    "update_state_file_name",
    "support_bundle_prefix",
    "max_receiver_generations",
    "max_structured_events",
    "max_transaction_outcomes",
    "max_support_bundle_bytes",
    "status_timeout_ms",
}

ACCOUNT = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
FILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
KERNEL_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PACKAGE_NAME = re.compile(r"^[a-z][a-z0-9+_.-]{0,63}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
FORBIDDEN_KERNEL_PRESENTATION = re.compile(r"razer|mouse|keyboard|hard|cloth")


@dataclass(frozen=True)
class ProductRuntime:
    display_name: str
    version: str
    package_name: str
    package_release: int


@dataclass(frozen=True)
class BridgeLimits:
    max_connections: int
    command_queue_capacity: int
    lease_capacity: int
    lease_history_capacity: int
    transaction_capacity: int
    event_capacity: int
    diagnostic_capacity: int
    subscription_capacity: int
    observation_batches_per_tick: int
    dispatches_per_tick: int


@dataclass(frozen=True)
class BridgeTiming:
    discovery_interval_ms: int
    accept_poll_interval_ms: int
    actor_response_timeout_ms: int
    kernel_session_duration_ms: int


@dataclass(frozen=True)
class BridgeRestoration:
    max_pending_triggers: int
    max_pending_claims: int
    claims_per_tick: int
    retry_interval_ms: int
    lease_duration_ms: int
    authority_window_ms: int
    max_persisted_receivers: int
    max_persistence_bytes: int


@dataclass(frozen=True)
class BridgeRuntime:
    service_account: str
    kernel_access_group: str
    client_group: str
    service_unit: str
    executable_path: str
    runtime_directory: str
    socket_name: str
    socket_lock_name: str
    state_directory: str
    state_file_name: str
    identity_secret_file_name: str
    configuration_directory: str
    configuration_file_name: str
    limits: BridgeLimits
    timing: BridgeTiming
    restoration: BridgeRestoration
    configuration_max_bytes: int

    @property
    def socket_path(self) -> str:
        return str(PurePosixPath(self.runtime_directory) / self.socket_name)

    @property
    def socket_lock_path(self) -> str:
        return str(PurePosixPath(self.runtime_directory) / self.socket_lock_name)

    @property
    def state_file_path(self) -> str:
        return str(PurePosixPath(self.state_directory) / self.state_file_name)

    @property
    def identity_secret_file_path(self) -> str:
        return str(PurePosixPath(self.state_directory) / self.identity_secret_file_name)

    @property
    def configuration_file_path(self) -> str:
        return str(PurePosixPath(self.configuration_directory) / self.configuration_file_name)


@dataclass(frozen=True)
class KernelRuntime:
    module_name: str
    dkms_name: str
    device_prefix: str
    source_directory: str
    control_transfer_timeout_ms: int


@dataclass(frozen=True)
class OperationsRuntime:
    cli_path: str
    activation_path: str
    python_module_directory: str
    update_state_file_name: str
    support_bundle_prefix: str
    max_receiver_generations: int
    max_structured_events: int
    max_transaction_outcomes: int
    max_support_bundle_bytes: int
    status_timeout_ms: int


@dataclass(frozen=True)
class LinuxRuntime:
    source_sha256: str
    product: ProductRuntime
    bridge: BridgeRuntime
    kernel: KernelRuntime
    operations: OperationsRuntime

    @property
    def update_state_path(self) -> str:
        return str(
            PurePosixPath(self.bridge.runtime_directory)
            / self.operations.update_state_file_name
        )


def _exact(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing:
        raise ModelError(f"{label}: missing fields {', '.join(missing)}")
    if extra:
        raise ModelError(f"{label}: unknown fields {', '.join(extra)}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: must be an object")
    return value


def _string(value: Any, label: str, pattern: re.Pattern[str], maximum: int = 192) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not pattern.fullmatch(value)
    ):
        raise ModelError(f"{label}: invalid value")
    if any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise ModelError(f"{label}: must contain printable ASCII only")
    return value


def _absolute_path(value: Any, label: str, prefix: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value != value.strip():
        raise ModelError(f"{label}: must be an absolute path")
    path = PurePosixPath(value)
    if path.parts[: len(prefix)] != prefix or ".." in path.parts or str(path) != value:
        raise ModelError(f"{label}: must be a normalized path below {'/'.join(prefix)}")
    if len(value.encode()) > 240 or any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise ModelError(f"{label}: path is not bounded printable ASCII")
    return value


def _bounded_integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ModelError(f"{label}: must be from {minimum} through {maximum}")
    return value


def load_linux_runtime(root: Path) -> LinuxRuntime:
    path = root / "runtime" / "linux.json"
    value = load_json(path)
    _exact(value, ROOT_KEYS, "Linux runtime")
    if value["schema"] != "hyperflux-linux-runtime-v1":
        raise ModelError("unsupported Linux runtime schema")

    product_value = _object(value["product"], "Linux product runtime")
    _exact(product_value, PRODUCT_KEYS, "Linux product runtime")
    display_name = product_value["display_name"]
    if display_name != "HyperFlux Next":
        raise ModelError("Linux product display name must remain HyperFlux Next")
    product = ProductRuntime(
        display_name=display_name,
        version=_string(product_value["version"], "Linux product version", VERSION, 64),
        package_name=_string(
            product_value["package_name"], "Linux package name", PACKAGE_NAME, 64
        ),
        package_release=_bounded_integer(
            product_value["package_release"], "Linux package release", 1, 65_535
        ),
    )

    bridge_value = _object(value["bridge"], "Linux bridge runtime")
    _exact(bridge_value, BRIDGE_KEYS, "Linux bridge runtime")
    limits_value = _object(bridge_value["limits"], "Linux bridge limits")
    _exact(limits_value, BRIDGE_LIMIT_KEYS, "Linux bridge limits")
    timing_value = _object(bridge_value["timing"], "Linux bridge timing")
    _exact(timing_value, BRIDGE_TIMING_KEYS, "Linux bridge timing")
    restoration_value = _object(
        bridge_value["restoration"], "Linux bridge restoration"
    )
    _exact(
        restoration_value,
        BRIDGE_RESTORATION_KEYS,
        "Linux bridge restoration",
    )
    bridge = BridgeRuntime(
        service_account=_string(
            bridge_value["service_account"], "bridge service account", ACCOUNT, 32
        ),
        kernel_access_group=_string(
            bridge_value["kernel_access_group"], "bridge kernel access group", ACCOUNT, 32
        ),
        client_group=_string(bridge_value["client_group"], "bridge client group", ACCOUNT, 32),
        service_unit=_string(
            bridge_value["service_unit"],
            "bridge service unit",
            re.compile(r"^[a-z][a-z0-9@_.-]{0,95}\.service$"),
            104,
        ),
        executable_path=_absolute_path(
            bridge_value["executable_path"], "bridge executable", ("/", "usr", "lib")
        ),
        runtime_directory=_absolute_path(
            bridge_value["runtime_directory"], "bridge runtime directory", ("/", "run")
        ),
        socket_name=_string(bridge_value["socket_name"], "bridge socket name", FILE_NAME, 64),
        socket_lock_name=_string(
            bridge_value["socket_lock_name"], "bridge socket lock name", FILE_NAME, 64
        ),
        state_directory=_absolute_path(
            bridge_value["state_directory"], "bridge state directory", ("/", "var", "lib")
        ),
        state_file_name=_string(
            bridge_value["state_file_name"], "bridge state file name", FILE_NAME, 64
        ),
        identity_secret_file_name=_string(
            bridge_value["identity_secret_file_name"],
            "bridge identity secret file name",
            FILE_NAME,
            64,
        ),
        configuration_directory=_absolute_path(
            bridge_value["configuration_directory"],
            "bridge configuration directory",
            ("/", "etc"),
        ),
        configuration_file_name=_string(
            bridge_value["configuration_file_name"],
            "bridge configuration file name",
            FILE_NAME,
            64,
        ),
        limits=BridgeLimits(
            max_connections=_bounded_integer(
                limits_value["max_connections"], "bridge connection bound", 1, 1024
            ),
            command_queue_capacity=_bounded_integer(
                limits_value["command_queue_capacity"], "bridge command queue", 1, 4096
            ),
            lease_capacity=_bounded_integer(
                limits_value["lease_capacity"], "bridge lease capacity", 1, 4096
            ),
            lease_history_capacity=_bounded_integer(
                limits_value["lease_history_capacity"], "bridge lease history", 1, 4096
            ),
            transaction_capacity=_bounded_integer(
                limits_value["transaction_capacity"], "bridge transaction capacity", 1, 4096
            ),
            event_capacity=_bounded_integer(
                limits_value["event_capacity"], "bridge event capacity", 1, 4096
            ),
            diagnostic_capacity=_bounded_integer(
                limits_value["diagnostic_capacity"], "bridge diagnostic capacity", 1, 128
            ),
            subscription_capacity=_bounded_integer(
                limits_value["subscription_capacity"], "bridge subscription capacity", 1, 4096
            ),
            observation_batches_per_tick=_bounded_integer(
                limits_value["observation_batches_per_tick"],
                "bridge observation batches per tick",
                1,
                64,
            ),
            dispatches_per_tick=_bounded_integer(
                limits_value["dispatches_per_tick"],
                "bridge dispatches per tick",
                1,
                64,
            ),
        ),
        timing=BridgeTiming(
            discovery_interval_ms=_bounded_integer(
                timing_value["discovery_interval_ms"], "bridge discovery interval", 10, 10000
            ),
            accept_poll_interval_ms=_bounded_integer(
                timing_value["accept_poll_interval_ms"], "bridge accept poll interval", 1, 1000
            ),
            actor_response_timeout_ms=_bounded_integer(
                timing_value["actor_response_timeout_ms"], "bridge actor response timeout", 100, 30000
            ),
            kernel_session_duration_ms=_bounded_integer(
                timing_value["kernel_session_duration_ms"], "kernel session duration", 6000, 300000
            ),
        ),
        restoration=BridgeRestoration(
            max_pending_triggers=_bounded_integer(
                restoration_value["max_pending_triggers"],
                "restoration trigger bound",
                1,
                1_024,
            ),
            max_pending_claims=_bounded_integer(
                restoration_value["max_pending_claims"],
                "restoration claim bound",
                1,
                16_384,
            ),
            claims_per_tick=_bounded_integer(
                restoration_value["claims_per_tick"],
                "restoration claims per tick",
                1,
                64,
            ),
            retry_interval_ms=_bounded_integer(
                restoration_value["retry_interval_ms"],
                "restoration retry interval",
                100,
                60_000,
            ),
            lease_duration_ms=_bounded_integer(
                restoration_value["lease_duration_ms"],
                "restoration lease duration",
                1_000,
                3_600_000,
            ),
            authority_window_ms=_bounded_integer(
                restoration_value["authority_window_ms"],
                "restoration authority window",
                1_000,
                3_600_000,
            ),
            max_persisted_receivers=_bounded_integer(
                restoration_value["max_persisted_receivers"],
                "persisted receiver bound",
                1,
                64,
            ),
            max_persistence_bytes=_bounded_integer(
                restoration_value["max_persistence_bytes"],
                "restoration persistence byte bound",
                65_536,
                16_777_216,
            ),
        ),
        configuration_max_bytes=_bounded_integer(
            bridge_value["configuration_max_bytes"],
            "bridge configuration byte bound",
            1_024,
            1_048_576,
        ),
    )
    if bridge.client_group == bridge.kernel_access_group:
        raise ModelError("bridge client and kernel access groups must remain separate")
    if bridge.identity_secret_file_name == bridge.state_file_name:
        raise ModelError("bridge identity secret and durable state must use separate files")
    if bridge.socket_lock_name == bridge.socket_name:
        raise ModelError("bridge socket and process lock must use separate files")
    if bridge.limits.lease_history_capacity < bridge.limits.lease_capacity:
        raise ModelError("bridge lease history must cover every active lease")
    if bridge.limits.command_queue_capacity < bridge.limits.max_connections:
        raise ModelError("bridge command queue must cover every active connection")
    if bridge.restoration.max_pending_claims < bridge.restoration.max_pending_triggers:
        raise ModelError("restoration claim bound must cover every pending trigger")
    if bridge.restoration.authority_window_ms <= bridge.restoration.lease_duration_ms:
        raise ModelError("restoration authority window must exceed its lease duration")
    if len(bridge.socket_path.encode()) >= 108:
        raise ModelError("bridge socket path exceeds the Unix socket path bound")

    kernel_value = _object(value["kernel"], "Linux kernel runtime")
    _exact(kernel_value, KERNEL_KEYS, "Linux kernel runtime")
    kernel = KernelRuntime(
        module_name=_string(kernel_value["module_name"], "kernel module name", KERNEL_NAME, 64),
        dkms_name=_string(kernel_value["dkms_name"], "DKMS name", KERNEL_NAME, 64),
        device_prefix=_string(
            kernel_value["device_prefix"], "kernel device prefix", KERNEL_NAME, 64
        ),
        source_directory=_absolute_path(
            kernel_value["source_directory"], "DKMS source directory", ("/", "usr", "src")
        ),
        control_transfer_timeout_ms=_bounded_integer(
            kernel_value["control_transfer_timeout_ms"],
            "kernel control transfer timeout",
            1,
            5_000,
        ),
    )
    for name in (kernel.module_name, kernel.dkms_name, kernel.device_prefix):
        if FORBIDDEN_KERNEL_PRESENTATION.search(name):
            raise ModelError("kernel names must not contain product presentation facts")
    expected_source = f"/usr/src/{kernel.dkms_name}-{product.version}"
    if kernel.source_directory != expected_source:
        raise ModelError("DKMS source directory must derive from the canonical name and version")
    from .kernel_uapi import load_kernel_uapi

    uapi = load_kernel_uapi(root)
    maximum_dispatch_ms = (
        uapi.limits["max_frames"] * kernel.control_transfer_timeout_ms
        + (uapi.limits["max_transaction_delay_us"] + 999) // 1_000
    )
    if bridge.limits.dispatches_per_tick != 1 or bridge.restoration.claims_per_tick != 1:
        raise ModelError("the synchronous actor must admit one hardware dispatch per tick")
    if bridge.timing.actor_response_timeout_ms < maximum_dispatch_ms + 1_000:
        raise ModelError(
            "bridge actor timeout must exceed one maximum kernel dispatch plus its margin"
        )

    operations_value = _object(value["operations"], "Linux operations runtime")
    _exact(operations_value, OPERATIONS_KEYS, "Linux operations runtime")
    operations = OperationsRuntime(
        cli_path=_absolute_path(operations_value["cli_path"], "operations CLI", ("/", "usr", "bin")),
        activation_path=_absolute_path(
            operations_value["activation_path"], "activation utility", ("/", "usr", "lib")
        ),
        python_module_directory=_absolute_path(
            operations_value["python_module_directory"],
            "private Python module directory",
            ("/", "usr", "lib"),
        ),
        update_state_file_name=_string(
            operations_value["update_state_file_name"], "update state file name", FILE_NAME, 64
        ),
        support_bundle_prefix=_string(
            operations_value["support_bundle_prefix"],
            "support bundle prefix",
            re.compile(r"^[a-z][a-z0-9-]{0,63}$"),
            64,
        ),
        max_receiver_generations=_bounded_integer(
            operations_value["max_receiver_generations"],
            "support receiver generation bound",
            1,
            64,
        ),
        max_structured_events=_bounded_integer(
            operations_value["max_structured_events"], "support event bound", 1, 4_096
        ),
        max_transaction_outcomes=_bounded_integer(
            operations_value["max_transaction_outcomes"], "support outcome bound", 1, 1_024
        ),
        max_support_bundle_bytes=_bounded_integer(
            operations_value["max_support_bundle_bytes"],
            "support bundle byte bound",
            65_536,
            16_777_216,
        ),
        status_timeout_ms=_bounded_integer(
            operations_value["status_timeout_ms"], "status timeout", 100, 10_000
        ),
    )
    if PurePosixPath(operations.activation_path).parent != PurePosixPath(bridge.executable_path).parent:
        raise ModelError("bridge and activation utilities must share one private executable directory")
    if PurePosixPath(operations.python_module_directory).parent != PurePosixPath(
        bridge.executable_path
    ).parent:
        raise ModelError("private executables and Python modules must share one product directory")

    return LinuxRuntime(
        source_sha256=sha256_file(path),
        product=product,
        bridge=bridge,
        kernel=kernel,
        operations=operations,
    )
