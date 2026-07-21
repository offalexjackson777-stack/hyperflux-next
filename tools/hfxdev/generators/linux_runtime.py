# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import PurePosixPath

from ..linux_runtime import LinuxRuntime
from .domain import HEADER_PYTHON, HEADER_RUST


def _json(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _shell(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def rust_constants(runtime: LinuxRuntime) -> str:
    product = runtime.product
    bridge = runtime.bridge
    kernel = runtime.kernel
    operations = runtime.operations
    values = [
        ("LINUX_RUNTIME_SHA256", runtime.source_sha256),
        ("PRODUCT_DISPLAY_NAME", product.display_name),
        ("PRODUCT_VERSION", product.version),
        ("PACKAGE_NAME", product.package_name),
        ("BRIDGE_SERVICE_ACCOUNT", bridge.service_account),
        ("BRIDGE_KERNEL_ACCESS_GROUP", bridge.kernel_access_group),
        ("BRIDGE_CLIENT_GROUP", bridge.client_group),
        ("BRIDGE_SERVICE_UNIT", bridge.service_unit),
        ("BRIDGE_EXECUTABLE_PATH", bridge.executable_path),
        ("BRIDGE_RUNTIME_DIRECTORY", bridge.runtime_directory),
        ("BRIDGE_SOCKET_NAME", bridge.socket_name),
        ("BRIDGE_SOCKET_PATH", bridge.socket_path),
        ("BRIDGE_SOCKET_LOCK_PATH", bridge.socket_lock_path),
        ("BRIDGE_STATE_DIRECTORY", bridge.state_directory),
        ("BRIDGE_STATE_FILE_PATH", bridge.state_file_path),
        ("BRIDGE_IDENTITY_SECRET_FILE_PATH", bridge.identity_secret_file_path),
        ("BRIDGE_CONFIGURATION_DIRECTORY", bridge.configuration_directory),
        ("BRIDGE_CONFIGURATION_FILE_PATH", bridge.configuration_file_path),
        ("KERNEL_MODULE_NAME", kernel.module_name),
        ("KERNEL_DKMS_NAME", kernel.dkms_name),
        ("KERNEL_DEVICE_PREFIX", kernel.device_prefix),
        ("KERNEL_SOURCE_DIRECTORY", kernel.source_directory),
        ("OPERATIONS_CLI_PATH", operations.cli_path),
        ("ACTIVATION_UTILITY_PATH", operations.activation_path),
        ("ACTIVATION_SERVICE_UNIT", operations.activation_service_unit),
        ("CONFIRMATION_SERVICE_UNIT", operations.confirmation_service_unit),
        ("PYTHON_MODULE_DIRECTORY", operations.python_module_directory),
        ("UPDATE_STATE_PATH", runtime.update_state_path),
        ("SUPPORT_BUNDLE_PREFIX", operations.support_bundle_prefix),
    ]
    lines = [HEADER_RUST.rstrip(), ""]
    for name, value in values:
        declaration = f"pub const {name}: &str = {_json(value)};"
        if len(declaration) <= 100:
            lines.append(declaration)
        else:
            lines.extend([f"pub const {name}: &str =", f"    {_json(value)};"])
    lines.extend(
        [
            f"pub const PACKAGE_RELEASE: u16 = {product.package_release};",
            "pub const MAX_RECEIVER_GENERATIONS: usize = "
            f"{operations.max_receiver_generations:_};",
            f"pub const MAX_STRUCTURED_EVENTS: usize = {operations.max_structured_events:_};",
            "pub const MAX_TRANSACTION_OUTCOMES: usize = "
            f"{operations.max_transaction_outcomes:_};",
            "pub const MAX_SUPPORT_BUNDLE_BYTES: usize = "
            f"{operations.max_support_bundle_bytes:_};",
            f"pub const STATUS_TIMEOUT_MS: u64 = {operations.status_timeout_ms:_};",
            "pub const CONFIGURATION_MAX_BYTES: u64 = "
            f"{bridge.configuration_max_bytes:_};",
            f"pub const MAX_CONNECTIONS: u16 = {bridge.limits.max_connections:_};",
            "pub const COMMAND_QUEUE_CAPACITY: u16 = "
            f"{bridge.limits.command_queue_capacity:_};",
            f"pub const LEASE_CAPACITY: u16 = {bridge.limits.lease_capacity:_};",
            "pub const LEASE_HISTORY_CAPACITY: u16 = "
            f"{bridge.limits.lease_history_capacity:_};",
            "pub const TRANSACTION_CAPACITY: u16 = "
            f"{bridge.limits.transaction_capacity:_};",
            f"pub const EVENT_CAPACITY: u16 = {bridge.limits.event_capacity:_};",
            "pub const DIAGNOSTIC_CAPACITY: u16 = "
            f"{bridge.limits.diagnostic_capacity:_};",
            "pub const SUBSCRIPTION_CAPACITY: u16 = "
            f"{bridge.limits.subscription_capacity:_};",
            "pub const OBSERVATION_BATCHES_PER_TICK: usize = "
            f"{bridge.limits.observation_batches_per_tick:_};",
            "pub const DISPATCHES_PER_TICK: usize = "
            f"{bridge.limits.dispatches_per_tick:_};",
            "pub const DISCOVERY_INTERVAL_MS: u64 = "
            f"{bridge.timing.discovery_interval_ms:_};",
            "pub const ACCEPT_POLL_INTERVAL_MS: u64 = "
            f"{bridge.timing.accept_poll_interval_ms:_};",
            "pub const ACTOR_RESPONSE_TIMEOUT_MS: u64 = "
            f"{bridge.timing.actor_response_timeout_ms:_};",
            "pub const KERNEL_SESSION_DURATION_MS: u64 = "
            f"{bridge.timing.kernel_session_duration_ms:_};",
            "pub const RESTORATION_MAX_PENDING_TRIGGERS: usize = "
            f"{bridge.restoration.max_pending_triggers:_};",
            "pub const RESTORATION_MAX_PENDING_CLAIMS: usize = "
            f"{bridge.restoration.max_pending_claims:_};",
            "pub const RESTORATION_CLAIMS_PER_TICK: usize = "
            f"{bridge.restoration.claims_per_tick:_};",
            "pub const RESTORATION_RETRY_INTERVAL_MS: u64 = "
            f"{bridge.restoration.retry_interval_ms:_};",
            "pub const RESTORATION_LEASE_DURATION_MS: u32 = "
            f"{bridge.restoration.lease_duration_ms:_};",
            "pub const RESTORATION_AUTHORITY_WINDOW_MS: u64 = "
            f"{bridge.restoration.authority_window_ms:_};",
            "pub const RESTORATION_MAX_PERSISTED_RECEIVERS: usize = "
            f"{bridge.restoration.max_persisted_receivers:_};",
            "pub const RESTORATION_MAX_PERSISTENCE_BYTES: u64 = "
            f"{bridge.restoration.max_persistence_bytes:_};",
        ]
    )
    return "\n".join(lines) + "\n"


def python_constants(runtime: LinuxRuntime) -> str:
    product = runtime.product
    bridge = runtime.bridge
    kernel = runtime.kernel
    operations = runtime.operations
    values: list[tuple[str, str | int]] = [
        ("LINUX_RUNTIME_SHA256", runtime.source_sha256),
        ("PRODUCT_DISPLAY_NAME", product.display_name),
        ("PRODUCT_VERSION", product.version),
        ("PACKAGE_NAME", product.package_name),
        ("PACKAGE_RELEASE", product.package_release),
        ("BRIDGE_SERVICE_ACCOUNT", bridge.service_account),
        ("BRIDGE_KERNEL_ACCESS_GROUP", bridge.kernel_access_group),
        ("BRIDGE_CLIENT_GROUP", bridge.client_group),
        ("BRIDGE_SERVICE_UNIT", bridge.service_unit),
        ("BRIDGE_EXECUTABLE_PATH", bridge.executable_path),
        ("BRIDGE_SOCKET_PATH", bridge.socket_path),
        ("BRIDGE_SOCKET_LOCK_PATH", bridge.socket_lock_path),
        ("BRIDGE_STATE_FILE_PATH", bridge.state_file_path),
        ("BRIDGE_IDENTITY_SECRET_FILE_PATH", bridge.identity_secret_file_path),
        ("BRIDGE_CONFIGURATION_FILE_PATH", bridge.configuration_file_path),
        ("KERNEL_MODULE_NAME", kernel.module_name),
        ("KERNEL_DKMS_NAME", kernel.dkms_name),
        ("KERNEL_DEVICE_PREFIX", kernel.device_prefix),
        ("KERNEL_SOURCE_DIRECTORY", kernel.source_directory),
        ("OPERATIONS_CLI_PATH", operations.cli_path),
        ("ACTIVATION_UTILITY_PATH", operations.activation_path),
        ("ACTIVATION_SERVICE_UNIT", operations.activation_service_unit),
        ("CONFIRMATION_SERVICE_UNIT", operations.confirmation_service_unit),
        ("PYTHON_MODULE_DIRECTORY", operations.python_module_directory),
        ("UPDATE_STATE_PATH", runtime.update_state_path),
        ("SUPPORT_BUNDLE_PREFIX", operations.support_bundle_prefix),
        ("MAX_RECEIVER_GENERATIONS", operations.max_receiver_generations),
        ("MAX_STRUCTURED_EVENTS", operations.max_structured_events),
        ("MAX_TRANSACTION_OUTCOMES", operations.max_transaction_outcomes),
        ("MAX_SUPPORT_BUNDLE_BYTES", operations.max_support_bundle_bytes),
        ("STATUS_TIMEOUT_MS", operations.status_timeout_ms),
        ("CONFIGURATION_MAX_BYTES", bridge.configuration_max_bytes),
        ("MAX_CONNECTIONS", bridge.limits.max_connections),
        ("COMMAND_QUEUE_CAPACITY", bridge.limits.command_queue_capacity),
        ("LEASE_CAPACITY", bridge.limits.lease_capacity),
        ("LEASE_HISTORY_CAPACITY", bridge.limits.lease_history_capacity),
        ("TRANSACTION_CAPACITY", bridge.limits.transaction_capacity),
        ("EVENT_CAPACITY", bridge.limits.event_capacity),
        ("DIAGNOSTIC_CAPACITY", bridge.limits.diagnostic_capacity),
        ("SUBSCRIPTION_CAPACITY", bridge.limits.subscription_capacity),
        ("OBSERVATION_BATCHES_PER_TICK", bridge.limits.observation_batches_per_tick),
        ("DISPATCHES_PER_TICK", bridge.limits.dispatches_per_tick),
        ("DISCOVERY_INTERVAL_MS", bridge.timing.discovery_interval_ms),
        ("ACCEPT_POLL_INTERVAL_MS", bridge.timing.accept_poll_interval_ms),
        ("ACTOR_RESPONSE_TIMEOUT_MS", bridge.timing.actor_response_timeout_ms),
        ("KERNEL_SESSION_DURATION_MS", bridge.timing.kernel_session_duration_ms),
        ("RESTORATION_MAX_PENDING_TRIGGERS", bridge.restoration.max_pending_triggers),
        ("RESTORATION_MAX_PENDING_CLAIMS", bridge.restoration.max_pending_claims),
        ("RESTORATION_CLAIMS_PER_TICK", bridge.restoration.claims_per_tick),
        ("RESTORATION_RETRY_INTERVAL_MS", bridge.restoration.retry_interval_ms),
        ("RESTORATION_LEASE_DURATION_MS", bridge.restoration.lease_duration_ms),
        ("RESTORATION_AUTHORITY_WINDOW_MS", bridge.restoration.authority_window_ms),
        (
            "RESTORATION_MAX_PERSISTED_RECEIVERS",
            bridge.restoration.max_persisted_receivers,
        ),
        (
            "RESTORATION_MAX_PERSISTENCE_BYTES",
            bridge.restoration.max_persistence_bytes,
        ),
    ]
    lines = [HEADER_PYTHON.rstrip(), ""]
    for name, value in values:
        rendered = _json(value) if isinstance(value, str) else str(value)
        lines.append(f"{name} = {rendered}")
    return "\n".join(lines) + "\n"


def kernel_version_header(runtime: LinuxRuntime) -> str:
    return "\n".join(
        [
            "/* Generated by ./hfx generate. Do not edit manually. */",
            "/* SPDX-License-Identifier: GPL-2.0-only */",
            "#ifndef HYPERFLUX_NEXT_VERSION_H",
            "#define HYPERFLUX_NEXT_VERSION_H",
            "",
            f'#define HYPERFLUX_NEXT_MODULE_VERSION "{runtime.product.version}"',
            "#define HYPERFLUX_NEXT_CONTROL_TRANSFER_TIMEOUT_MS "
            f"{runtime.kernel.control_transfer_timeout_ms}U",
            "",
            "#endif",
            "",
        ]
    )


def python_distribution_version(runtime: LinuxRuntime) -> str:
    version = runtime.product.version
    if "-dev." in version:
        return version.replace("-dev.", ".dev", 1)
    if "-rc." in version:
        return version.replace("-rc.", "rc", 1)
    return version


def python_version_module(runtime: LinuxRuntime, license_expression: str) -> str:
    return "\n".join(
        [
            "# Generated by ./hfx generate. Do not edit manually.",
            f"# SPDX-License-Identifier: {license_expression}",
            "",
            f'__version__ = "{python_distribution_version(runtime)}"',
            "",
        ]
    )


def dkms_configuration(runtime: LinuxRuntime) -> str:
    kernel = runtime.kernel
    version = runtime.product.version
    module_output = (
        '${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/'
        '${kernelver}/${arch}/module'
    )
    return "\n".join(
        [
            "# Generated by ./hfx generate. Do not edit manually.",
            "# SPDX-License-Identifier: GPL-2.0-only",
            f'PACKAGE_NAME="{kernel.dkms_name}"',
            f'PACKAGE_VERSION="{version}"',
            f'BUILT_MODULE_NAME[0]="{kernel.module_name}"',
            'DEST_MODULE_LOCATION[0]="/updates/dkms"',
            f'MAKE[0]="make KDIR=/lib/modules/${{kernelver}}/build MO={module_output}"',
            f'CLEAN="make KDIR=/lib/modules/${{kernelver}}/build MO={module_output} clean"',
            'AUTOINSTALL="yes"',
            "",
        ]
    )


def systemd_service(runtime: LinuxRuntime) -> str:
    bridge = runtime.bridge
    operations = runtime.operations
    private_directory = PurePosixPath(bridge.executable_path).parent
    return "\n".join(
        [
            "# Generated by ./hfx generate. Do not edit manually.",
            "# SPDX-License-Identifier: GPL-2.0-only",
            "[Unit]",
            f"Description={runtime.product.display_name} bridge",
            "Documentation=man:hyperfluxctl(1)",
            f"Requires={operations.activation_service_unit}",
            f"Wants={operations.confirmation_service_unit}",
            f"After=systemd-udevd.service {operations.activation_service_unit}",
            f"Before={operations.confirmation_service_unit}",
            f"ConditionPathExists={bridge.configuration_file_path}",
            "",
            "[Service]",
            "Type=simple",
            f"User={bridge.service_account}",
            f"Group={bridge.client_group}",
            f"SupplementaryGroups={bridge.kernel_access_group}",
            f"ExecStart={bridge.executable_path} --config {bridge.configuration_file_path}",
            f"RuntimeDirectory={PurePosixPath(bridge.runtime_directory).name}",
            "RuntimeDirectoryMode=2750",
            f"StateDirectory={PurePosixPath(bridge.state_directory).name}",
            "StateDirectoryMode=0700",
            f"ConfigurationDirectory={PurePosixPath(bridge.configuration_directory).name}",
            "ConfigurationDirectoryMode=2750",
            "UMask=0027",
            "Restart=on-failure",
            "RestartSec=2s",
            "TimeoutStartSec=10s",
            "TimeoutStopSec=10s",
            "LimitNOFILE=128",
            "TasksMax=64",
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "PrivateTmp=yes",
            "ProtectControlGroups=yes",
            "ProtectKernelTunables=yes",
            "ProtectKernelModules=yes",
            "ProtectKernelLogs=yes",
            "RestrictAddressFamilies=AF_UNIX",
            "RestrictSUIDSGID=yes",
            "LockPersonality=yes",
            "MemoryDenyWriteExecute=yes",
            "SystemCallArchitectures=native",
            "SystemCallFilter=@system-service",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            f"ReadOnlyPaths={private_directory} {bridge.configuration_directory}",
            f"ReadWritePaths={bridge.runtime_directory} {bridge.state_directory}",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _activation_sandbox(runtime: LinuxRuntime) -> list[str]:
    bridge = runtime.bridge
    private_directory = PurePosixPath(bridge.executable_path).parent
    return [
        "User=root",
        "Group=root",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "ProtectControlGroups=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        f"ReadOnlyPaths={private_directory}",
        f"ReadWritePaths={bridge.configuration_directory} {bridge.state_directory}",
    ]


def activation_service(runtime: LinuxRuntime) -> str:
    operations = runtime.operations
    lines = [
        "# Generated by ./hfx generate. Do not edit manually.",
        "# SPDX-License-Identifier: GPL-2.0-only",
        "[Unit]",
        f"Description={runtime.product.display_name} start preparation",
        f"Before={runtime.bridge.service_unit}",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={operations.activation_path} prepare-start",
    ]
    lines.extend(_activation_sandbox(runtime))
    lines.append("")
    return "\n".join(lines)


def confirmation_service(runtime: LinuxRuntime) -> str:
    operations = runtime.operations
    lines = [
        "# Generated by ./hfx generate. Do not edit manually.",
        "# SPDX-License-Identifier: GPL-2.0-only",
        "[Unit]",
        f"Description={runtime.product.display_name} start confirmation",
        f"Requires={runtime.bridge.service_unit}",
        f"After={runtime.bridge.service_unit}",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={operations.activation_path} confirm-start",
    ]
    lines.extend(_activation_sandbox(runtime))
    lines.append("")
    return "\n".join(lines)


def sysusers(runtime: LinuxRuntime) -> str:
    bridge = runtime.bridge
    return "\n".join(
        [
            "# Generated by ./hfx generate. Do not edit manually.",
            "# SPDX-License-Identifier: GPL-2.0-only",
            f"g {bridge.kernel_access_group} - -",
            f"g {bridge.client_group} - -",
            f'u {bridge.service_account} - "{runtime.product.display_name} bridge" '
            f"{bridge.state_directory} /usr/bin/nologin",
            f"m {bridge.service_account} {bridge.client_group}",
            "",
        ]
    )


def tmpfiles(runtime: LinuxRuntime) -> str:
    bridge = runtime.bridge
    return "\n".join(
        [
            "# Generated by ./hfx generate. Do not edit manually.",
            "# SPDX-License-Identifier: GPL-2.0-only",
            f"d {bridge.runtime_directory} 2750 {bridge.service_account} {bridge.client_group} -",
            f"d {bridge.state_directory} 0700 {bridge.service_account} {bridge.client_group} -",
            f"d {bridge.configuration_directory} 2750 root {bridge.kernel_access_group} -",
            "",
        ]
    )


def udev_rules(runtime: LinuxRuntime) -> str:
    return "\n".join(
        [
            "# Generated by ./hfx generate. Do not edit manually.",
            "# SPDX-License-Identifier: GPL-2.0-only",
            f'SUBSYSTEM=="misc", KERNEL=="{runtime.kernel.device_prefix}-*", '
            f'GROUP="{runtime.bridge.kernel_access_group}", MODE="0660"',
            "",
        ]
    )


def default_bridge_configuration(runtime: LinuxRuntime) -> str:
    value = {
        "$schema": "/usr/share/hyperflux-next/schemas/bridge-config.schema.json",
        "schema": "hyperflux-bridge-config-v1",
        "mode": "read-only",
        "restoration": {"enabled": False},
        "socket": {"group": runtime.bridge.client_group, "mode": "0660"},
    }
    return json.dumps(value, indent=2, sort_keys=False, ensure_ascii=True) + "\n"


def package_environment(runtime: LinuxRuntime) -> str:
    product = runtime.product
    bridge = runtime.bridge
    kernel = runtime.kernel
    operations = runtime.operations
    values: list[tuple[str, str | int]] = [
        ("HFX_PRODUCT_VERSION", product.version),
        ("HFX_PACKAGE_NAME", product.package_name),
        ("HFX_PACKAGE_RELEASE", product.package_release),
        ("HFX_BRIDGE_SERVICE_UNIT", bridge.service_unit),
        ("HFX_BRIDGE_EXECUTABLE_PATH", bridge.executable_path),
        ("HFX_CONFIGURATION_FILE_PATH", bridge.configuration_file_path),
        ("HFX_KERNEL_MODULE_NAME", kernel.module_name),
        ("HFX_KERNEL_DKMS_NAME", kernel.dkms_name),
        ("HFX_KERNEL_SOURCE_DIRECTORY", kernel.source_directory),
        ("HFX_OPERATIONS_CLI_PATH", operations.cli_path),
        ("HFX_ACTIVATION_UTILITY_PATH", operations.activation_path),
        ("HFX_ACTIVATION_SERVICE_UNIT", operations.activation_service_unit),
        ("HFX_CONFIRMATION_SERVICE_UNIT", operations.confirmation_service_unit),
        ("HFX_PYTHON_MODULE_DIRECTORY", operations.python_module_directory),
    ]
    lines = [
        "# Generated by ./hfx generate. Do not edit manually.",
        "# SPDX-License-Identifier: GPL-2.0-only",
    ]
    for name, value in values:
        lines.append(f"{name}={value if isinstance(value, int) else _shell(value)}")
    return "\n".join(lines) + "\n"


def markdown(runtime: LinuxRuntime) -> str:
    product = runtime.product
    bridge = runtime.bridge
    kernel = runtime.kernel
    operations = runtime.operations
    return "\n".join(
        [
            "# Linux Runtime Authority",
            "",
            "> Generated by `./hfx generate`. Do not edit manually.",
            "",
            f"Canonical source digest: `{runtime.source_sha256}`.",
            "",
            "## Product",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Name | {product.display_name} |",
            f"| Version | `{product.version}` |",
            f"| Package | `{product.package_name}` release `{product.package_release}` |",
            "",
            "## Bridge",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Service | `{bridge.service_unit}` |",
            f"| Private service account | `{bridge.service_account}` |",
            f"| Kernel endpoint access group | `{bridge.kernel_access_group}` |",
            f"| Client access group | `{bridge.client_group}` |",
            f"| SDK socket | `{bridge.socket_path}` |",
            f"| Process lock | `{bridge.socket_lock_path}` |",
            f"| Configuration | `{bridge.configuration_file_path}` |",
            f"| Durable state | `{bridge.state_file_path}` |",
            f"| Private receiver identity | `{bridge.identity_secret_file_path}` |",
            f"| Version-neutral Python modules | `{operations.python_module_directory}` |",
            f"| Start preparation | `{operations.activation_service_unit}` |",
            f"| Start confirmation | `{operations.confirmation_service_unit}` |",
            "",
            "The service owns receiver access. Application integrations use only the SDK socket. "
            "The default configuration is read-only and restoration is disabled until a user makes "
            "an explicit setup choice.",
            "",
            "## Kernel Activation",
            "",
            f"The DKMS module is `{kernel.dkms_name}` version `{product.version}` and creates "
            f"generation-scoped `{kernel.device_prefix}-*` misc devices for the bridge account.",
            "",
            "A userspace-only compatible update may restart the bridge automatically. If the loaded "
            "kernel module differs from the installed module, activation requires a reboot or the "
            "documented receiver-disconnect, module-reload, and receiver-reconnect sequence. Doctor "
            "must report that condition as a driver activation finding, never as a service failure.",
            "",
            "## Operational Bounds",
            "",
            f"- Receiver generations in a support bundle: `{operations.max_receiver_generations}`",
            f"- Structured events in a support bundle: `{operations.max_structured_events}`",
            f"- Transaction outcomes in a support bundle: `{operations.max_transaction_outcomes}`",
            f"- Maximum support bundle size: `{operations.max_support_bundle_bytes}` bytes",
            f"- Ordinary status timeout: `{operations.status_timeout_ms}` ms",
            f"- Active SDK connections: `{bridge.limits.max_connections}`",
            f"- Actor command queue: `{bridge.limits.command_queue_capacity}`",
            f"- Transaction queue: `{bridge.limits.transaction_capacity}`",
            f"- Event stream: `{bridge.limits.event_capacity}`",
            f"- Passive batches per actor tick: `{bridge.limits.observation_batches_per_tick}`",
            f"- Shared hardware dispatches per actor tick: `{bridge.limits.dispatches_per_tick}`",
            f"- Kernel control-transfer timeout: `{kernel.control_transfer_timeout_ms}` ms",
            f"- Actor response timeout: `{bridge.timing.actor_response_timeout_ms}` ms",
            f"- Discovery interval: `{bridge.timing.discovery_interval_ms}` ms",
            f"- Socket accept poll interval: `{bridge.timing.accept_poll_interval_ms}` ms",
            f"- Configuration size: `{bridge.configuration_max_bytes}` bytes",
            f"- Kernel writer-session lifetime: `{bridge.timing.kernel_session_duration_ms}` ms",
            f"- Pending restoration triggers: `{bridge.restoration.max_pending_triggers}`",
            f"- Pending restoration claims: `{bridge.restoration.max_pending_claims}`",
            f"- Restoration claims advanced per tick: `{bridge.restoration.claims_per_tick}`",
            f"- Deferred restoration retry interval: `{bridge.restoration.retry_interval_ms}` ms",
            f"- Restoration persistence size: `{bridge.restoration.max_persistence_bytes}` bytes",
            "",
        ]
    )
