# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time

from .model import ModelError, load_foundation, load_json, require_unique, sha256_file
from .assurance import load_design_coverage
from .arch_update_policy import verify_arch_openrazer_update_policy
from .atlas import load_repository_atlas
from .development import load_development_environment
from .errors import load_error_catalog
from .formal_model import load_formal_model, run_formal_model
from .governance import load_github_governance
from .integrations import load_integration_catalog
from .install import load_install_manifest
from .knowledge import compiled_knowledge_catalog, load_knowledge_inputs
from .linux_runtime import load_linux_runtime
from .licensing import verify_licensing_policy
from .local_companion import load_local_companion
from .native_command import (
    load_native_command_catalog,
    load_native_command_policy,
    transformed_native_command_catalog,
)
from .native_control import (
    load_native_control_policy,
    load_native_method_inventory,
    transformed_native_inventory,
)
from .native_probe import (
    c_function_source,
    load_native_probe_catalog,
    load_native_probe_policy,
    transformed_native_probe_catalog,
)
from .openrazer import (
    effective_event_regex,
    extract_openrazer_catalog,
    load_imported_metadata,
    transformed_metadata,
    verify_global_event_file_matcher,
)
from .openrazer_native_route import (
    load_native_deployment,
    load_native_route_catalog,
    prepare_openrazer_native_source,
    transformed_native_route_catalog,
)
from .openrazer_version_matrix import (
    load_openrazer_version_compatibility,
    load_openrazer_version_matrix,
)
from .openrgb import extract_openrgb_catalog
from .openrgb_native_route import prepare_openrgb_native_source
from .package_pipeline import StageResult, build_artifacts, load_artifact_set, stage_rootfs
from .distribution_package import build_distribution_package
from .performance import (
    load_performance_budgets,
    verify_package_performance_budgets,
    verify_static_performance_budgets,
)
from .protected_behavior import load_protected_behavior
from .render import rendered_binary_files, rendered_files
from .profiles import load_profile_inputs
from .protocol import load_protocol_catalog
from .release import load_release_gates
from .supply_chain import load_dependency_inventory
from .testgraph import TestNode, load_test_catalog
from .toolchains import toolchain_environment, verify_current_toolchain


ALLOWED_DISPOSITIONS = {
    "REIMPLEMENT",
    "REIMPLEMENT_AND_GENERATE",
    "REIMPLEMENT_WITH_PINNED_IMPORT",
    "MIGRATE_AFTER_REVIEW",
    "SPECIFICATION_ONLY",
    "TEST_REFERENCE",
    "RESEARCH_LINK",
    "REJECT",
}
ALLOWED_STATUSES = {"PENDING_REVIEW", "IN_PROGRESS", "ACCEPTED", "REJECTED"}
FUTURE_MTIME_TOLERANCE_SECONDS = 5.0
INSTALLED_SCHEMA_ROOT = Path("/usr/share/hyperflux-next/schemas")

from .verify_compiled import (
    _build_kernel_module_copy,
    _kernel_build_directories,
    _run_command,
)


def _openrazer_payload_retry_is_safe(source: str) -> bool:
    function = (
        "razer_send_payload_transport"
        if "razer_send_payload_transport" in source
        else "razer_send_payload"
    )
    payload, _ = c_function_source(source, function)
    if "razer_hyperflux_error_is_uncertain(err)" in payload:
        return True
    return (
        payload.count("razer_get_report(") == 1
        and "goto " not in payload
        and "retry:" not in payload
        and not re.search(r"\b(?:for|while)\s*\(", payload)
        and not re.search(r"\bdo\s*\{", payload)
    )


def _openrazer_native_payload_errors_propagate(
    source: str, device_type: str, *, no_response: bool = False
) -> bool:
    wrapper_count = 2 if no_response else 1
    repeated = (
        f"struct {device_type} *__hyperflux_device = (device);",
        "razer_is_hyperflux_native(__hyperflux_device->hdev)",
        "return __hyperflux_error;",
    )
    if any(source.count(marker) != wrapper_count for marker in repeated):
        return False
    if source.count("int __hyperflux_error = razer_send_payload_transport(") != 1:
        return False
    if source.count("#define razer_send_payload(device, request, response)") != 1:
        return False
    if re.search(r"static int razer_send_payload\s*\(", source):
        return False
    if not no_response:
        return True
    no_response_required = (
        "static int razer_send_payload_no_response_transport(",
        "#define razer_send_payload_no_response(device, request)",
        "int __hyperflux_error = razer_send_payload_no_response_transport(",
    )
    return all(source.count(marker) == 1 for marker in no_response_required) and not re.search(
        r"static int razer_send_payload_no_response\s*\(", source
    )


def _pinned_upstream_source(
    root: Path,
    *,
    environment_name: str,
    upstream_id: str,
    required_paths: tuple[str, ...],
    label: str,
) -> Path:
    value = os.environ.get(environment_name)
    source = Path(value) if value is not None else root / ".hfx" / "upstreams" / upstream_id
    if not source.is_absolute():
        raise ModelError(f"{environment_name} must name an absolute path")
    source = source.resolve()
    if not source.is_dir() or not all((source / path).is_file() for path in required_paths):
        raise ModelError(
            f"pinned {label} checkout is unavailable; run ./hfx upstream prepare"
        )
    expected = {
        upstream["id"]: upstream["commit"]
        for upstream in load_integration_catalog(root)["upstreams"]
    }[upstream_id]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect pinned {label} source: {error}") from error
    if result.stdout.strip() != expected:
        raise ModelError(f"{label} source checkout does not match the integration catalog pin")
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect pinned {label} worktree: {error}") from error
    if status.stdout:
        raise ModelError(f"pinned {label} source checkout has local modifications")
    return source


def _openrgb_source(root: Path) -> Path:
    return _pinned_upstream_source(
        root,
        environment_name="HFX_OPENRGB_SOURCE_DIR",
        upstream_id="openrgb",
        required_paths=("OpenRGBPluginInterface.h",),
        label="OpenRGB",
    )


def _openrazer_source(root: Path) -> Path:
    return _pinned_upstream_source(
        root,
        environment_name="HFX_OPENRAZER_SOURCE_DIR",
        upstream_id="openrazer",
        required_paths=("pylib/openrazer/client/__init__.py",),
        label="OpenRazer",
    )


def _run_openrazer_metadata_contracts(root: Path, _node: TestNode) -> None:
    source = _openrazer_source(root)
    expected = transformed_metadata(root, source)
    actual = load_imported_metadata(root)
    if actual != expected:
        raise ModelError("committed OpenRazer metadata is stale; rerun ./hfx import openrazer")
    expected_native = transformed_native_inventory(root, source)
    actual_native = load_native_method_inventory(root)
    if actual_native != expected_native:
        raise ModelError(
            "committed OpenRazer native method inventory is stale; "
            "rerun ./hfx import openrazer"
        )
    expected_probes = transformed_native_probe_catalog(root, source)
    actual_probes = load_native_probe_catalog(root)
    if actual_probes != expected_probes:
        raise ModelError(
            "committed OpenRazer native probe catalog is stale; "
            "rerun ./hfx import openrazer"
        )
    expected_commands = transformed_native_command_catalog(root, source)
    actual_commands = load_native_command_catalog(root)
    if actual_commands != expected_commands:
        raise ModelError(
            "committed OpenRazer native command catalog is stale; "
            "rerun ./hfx import openrazer"
        )


def _run_openrazer_native_route_contracts(root: Path, node: TestNode) -> None:
    source = _openrazer_source(root)
    deployment = load_native_deployment(root)
    build_directory = root / "build" / "openrazer-native-route-contracts"
    if build_directory.exists():
        shutil.rmtree(build_directory)
    prepared = prepare_openrazer_native_source(root, source, build_directory)
    if (prepared.result.stat().st_mode & 0o777) != 0o600:
        raise ModelError("OpenRazer native source binding is not private")
    result = load_json(prepared.result)
    if result.get("schema") != "hyperflux-openrazer-native-transform-result-v1":
        raise ModelError("OpenRazer native source binding schema is invalid")
    if any(
        result.get(field) is not False
        for field in (
            "network_access_executed",
            "hardware_queried",
            "hardware_write_executed",
        )
    ):
        raise ModelError("OpenRazer native source preparation crossed a prohibited boundary")
    if (prepared.root / ".git").exists():
        raise ModelError("OpenRazer native source output copied upstream Git metadata")

    verify_global_event_file_matcher(
        source / "daemon" / "openrazer_daemon" / "hardware" / "device_base.py"
    )

    route_catalog = load_native_route_catalog(root)
    if route_catalog != transformed_native_route_catalog(root, source):
        raise ModelError(
            "committed OpenRazer native route catalog is stale; "
            "rerun ./hfx import openrazer"
        )
    metadata = load_imported_metadata(root)
    probes = load_native_probe_catalog(root)
    metadata_by_profile = {device["profile_id"]: device for device in metadata["devices"]}
    probes_by_profile = {device["profile_id"]: device for device in probes["devices"]}
    for route in route_catalog["routes"]:
        profile_id = route["profile_id"]
        device = metadata_by_profile.get(profile_id)
        probe_device = probes_by_profile.get(profile_id)
        if device is None or probe_device is None:
            raise ModelError(f"OpenRazer native route {profile_id} lacks an upstream binding")
        if route["source"] != device["source"]:
            raise ModelError(f"OpenRazer native route {profile_id} source binding drifted")
        identity = device["identity"]
        if any(
            route[field] != identity[field]
            for field in ("vendor_id", "model_name", "device_kind")
        ) or route["wireless_product_id"] != identity["product_id"]:
            raise ModelError(f"OpenRazer native route {profile_id} identity drifted")
        direct = probe_device["direct_usb_reference"]
        if (
            direct["vendor_id"] != route["vendor_id"]
            or direct["device_kind"] != route["device_kind"]
            or direct["product_id"] not in route["direct_data_product_ids"]
            or route["wireless_product_id"] in route["direct_data_product_ids"]
        ):
            raise ModelError(f"OpenRazer native route {profile_id} direct precedence drifted")
        serials = [probe for probe in probe_device["probes"] if probe["method"] == "get_serial"]
        if len(serials) != 1 or any(
            (
                serials[0]["privacy"] != "redacted-equality-only",
                serials[0]["driver"]["attribute"] != "device_serial",
                serials[0]["response_summary"] != "redacted-serial-equality",
                serials[0]["comparison"] != "redacted-equality",
            )
        ):
            raise ModelError(f"OpenRazer native route {profile_id} serial proof drifted")
        source_path = source / route["source"]["path"]
        effective_regex = effective_event_regex(
            source_path, route["source"]["class_name"]
        )
        if effective_regex != route["event_file_regex"]:
            raise ModelError(f"OpenRazer native route {profile_id} event matcher drifted")
        try:
            re.compile(effective_regex)
        except re.error as error:
            raise ModelError(
                f"OpenRazer native route {profile_id} event matcher is invalid: {error}"
            ) from error

    common = (prepared.root / "driver" / "razercommon.c").read_text(encoding="utf-8")
    required_common = (
        'RAZER_HYPERFLUX_PHYS_PREFIX "hyperflux-next/native/v1/"',
        "!hdev->phys[0]",
        "hdev->dev_rsize != sizeof(razer_hyperflux_descriptor)",
        "hid_hw_raw_request(hdev, 0",
        "RAZER_HYPERFLUX_TRANSPORT_NOT_SENT",
        "RAZER_HYPERFLUX_TRANSPORT_MAY_HAVE_BEEN_SENT",
    )
    if any(marker not in common for marker in required_common):
        raise ModelError("OpenRazer native common transport is incomplete")

    keyboard_source = (prepared.root / "driver" / "razerkbd_driver.c").read_text(
        encoding="utf-8"
    )
    preserved_keyboard_fixes = (
        "strscpy(&serial_string[0], dmi_get_system_info(DMI_PRODUCT_SERIAL), 50);",
        "memcpy(&serial_string[0], &response.arguments[0], 22);",
        "(LINUX_VERSION_CODE >= KERNEL_VERSION(6, 12, 93)",
        "(LINUX_VERSION_CODE >= KERNEL_VERSION(6, 6, 143)",
        "(LINUX_VERSION_CODE >= KERNEL_VERSION(6, 1, 176)",
        "(LINUX_VERSION_CODE >= KERNEL_VERSION(5, 15, 210)",
        "(LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 259)",
        "#define LINUX_HID_REPORT_RAW_EVENT_WITH_BUFFER_SIZE",
        "hid_report_raw_event(hdev, HID_INPUT_REPORT, xdata, sizeof(xdata), sizeof(xdata), 0);",
        "hid_report_raw_event(hdev, HID_INPUT_REPORT, xdata, sizeof(xdata), 0);",
    )
    if any(marker not in keyboard_source for marker in preserved_keyboard_fixes):
        raise ModelError(
            "OpenRazer native keyboard source lost a required upstream 3.12.4 fix"
        )
    if keyboard_source.count("#ifdef LINUX_HID_REPORT_RAW_EVENT_WITH_BUFFER_SIZE") != 2:
        raise ModelError("OpenRazer native keyboard HID backport guard is incomplete")

    accessory_source = (
        prepared.root / "driver" / "razeraccessory_driver.c"
    ).read_text(encoding="utf-8")
    preserved_accessory_fixes = (
        "strscpy(&serial_string[0], &device->serial[0], sizeof(serial_string));",
        "memcpy(&serial_string[0], &response.arguments[0], 22);",
    )
    if any(marker not in accessory_source for marker in preserved_accessory_fixes):
        raise ModelError(
            "OpenRazer native accessory source lost a required upstream 3.12.4 fix"
        )

    rules = (prepared.root / "install_files" / "udev" / "99-razer.rules").read_text(
        encoding="utf-8"
    )
    bind_rules = (
        'ACTION=="bind", SUBSYSTEM=="hid", KERNEL=="0006:1532:*", '
        'ENV{HID_PHYS}=="hyperflux-next/native/v1/*", DRIVER=="razerkbd", '
        'RUN+="razer_mount razerkbd $kernel"',
        'ACTION=="bind", SUBSYSTEM=="hid", KERNEL=="0006:1532:*", '
        'ENV{HID_PHYS}=="hyperflux-next/native/v1/*", DRIVER=="razermouse", '
        'RUN+="razer_mount razermouse $kernel"',
    )
    if any(rules.count(marker) != 1 for marker in bind_rules):
        raise ModelError("OpenRazer native virtual-device access rules are incomplete")
    if any(rules.index(marker) > rules.index('ACTION!="add"') for marker in bind_rules):
        raise ModelError("OpenRazer native virtual-device access rules run after the add-only gate")
    if 'GROUP:="openrazer"' not in rules or "plugdev" in rules:
        raise ModelError("OpenRazer native udev group differs from its deployment authority")

    mount_helper = (prepared.root / "install_files" / "udev" / "razer_mount").read_text(
        encoding="utf-8"
    )
    if "chgrp -R openrazer" not in mount_helper or "plugdev" in mount_helper:
        raise ModelError("OpenRazer native mount helper group differs from its deployment authority")

    native_commands = load_native_command_catalog(root)
    command_sources: dict[str, set[str]] = {
        "driver/razerkbd_driver.c": set(),
        "driver/razermouse_driver.c": set(),
    }
    for device in native_commands["devices"]:
        for rule in device["rules"]:
            for source_binding in rule["sources"]:
                functions = command_sources.get(source_binding["path"])
                if functions is not None:
                    functions.add(source_binding["function"])

    for driver in ("razerkbd_driver.c", "razermouse_driver.c"):
        source_text = (prepared.root / "driver" / driver).read_text(encoding="utf-8")
        if source_text.count("HID_DEVICE(BUS_VIRTUAL,HID_GROUP_ANY") != 1:
            raise ModelError(f"OpenRazer native {driver} has an invalid virtual match boundary")
        if "identity->bus == BUS_USB" not in source_text:
            raise ModelError(f"OpenRazer native {driver} no longer reuses its exact USB catalog")
        if not _openrazer_payload_retry_is_safe(source_text):
            raise ModelError(f"OpenRazer native {driver} can retry an uncertain transaction")
        device_type = "razer_kbd_device" if driver == "razerkbd_driver.c" else "razer_mouse_device"
        if not _openrazer_native_payload_errors_propagate(
            source_text,
            device_type,
            no_response=driver == "razerkbd_driver.c",
        ):
            raise ModelError(
                f"OpenRazer native {driver} can convert a transport error into device data"
            )
        for function in sorted(command_sources[f"driver/{driver}"]):
            body, _ = c_function_source(source_text, function)
            if (
                "razer_send_payload(" not in body
                and "razer_send_payload_no_response(" not in body
            ):
                raise ModelError(
                    f"OpenRazer native command function {function} bypasses error propagation"
                )
            if "razer_send_payload_transport(" in body:
                raise ModelError(
                    f"OpenRazer native command function {function} bypasses the checked wrapper"
                )

    capability = deployment.compatibility.runtime_capability
    capability_marker = (
        f'MODULE_INFO({capability.modinfo_field}, "{capability.expected_value}");'
    )
    for module in deployment.driver.modules:
        source_text = (prepared.root / "driver" / f"{module}_driver.c").read_text(
            encoding="utf-8"
        )
        expected_count = 1 if module in capability.modules else 0
        if source_text.count(capability_marker) != expected_count:
            raise ModelError(
                f"OpenRazer native {module} capability marker differs from deployment policy"
            )

    prepared_driver = prepared.root / "driver"
    prepared_source_link = prepared_driver / "source"
    if prepared_source_link.exists() or prepared_source_link.is_symlink():
        raise ModelError("prepared OpenRazer source contains an unexpected build artifact")
    kernel_output_root = root / "build" / "openrazer-native-kernel-contracts"
    if kernel_output_root.exists():
        shutil.rmtree(kernel_output_root)
    for build_directory in _kernel_build_directories():
        label = build_directory.resolve().parent.name
        _build_kernel_module_copy(
            root,
            build_directory,
            prepared_driver,
            kernel_output_root / label / "driver",
            label=f"OpenRazer native kernel module build ({label})",
            timeout_seconds=node.timeout_seconds,
        )
        if prepared_source_link.exists() or prepared_source_link.is_symlink():
            raise ModelError("OpenRazer verification modified its prepared source tree")

    service = (
        root / "packaging" / "generated" / deployment.relay.service_unit
    ).read_text(encoding="utf-8")
    preflight = (
        root
        / "packaging"
        / "generated"
        / deployment.relay.preflight_service_unit
    ).read_text(encoding="utf-8")
    runtime = load_linux_runtime(root)
    required_service = (
        "User=root",
        f"SupplementaryGroups={runtime.bridge.client_group}",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=\n",
        "DevicePolicy=closed",
        f"DeviceAllow={deployment.relay.uhid_device} rw",
        f"PartOf={runtime.bridge.service_unit}",
        "ProtectKernelModules=yes",
        f"Requires={runtime.bridge.service_unit} {deployment.relay.preflight_service_unit}",
        f"After={runtime.bridge.service_unit} {deployment.relay.preflight_service_unit} systemd-udevd.service",
    )
    if any(marker not in service for marker in required_service):
        raise ModelError("OpenRazer native relay service sandbox is incomplete")
    if "check-openrazer-runtime" in service:
        raise ModelError("OpenRazer native relay still gates on a userspace version")
    if any(
        marker in service
        for marker in (
            "ExecStartPre=",
            "ExecCondition=",
            "check-openrazer-driver",
            "/usr/lib/modules",
            "/sys/module",
        )
    ):
        raise ModelError(
            "OpenRazer native relay repeats module inspection inside its sealed namespace"
        )
    if "PrivateDevices=yes" in service:
        raise ModelError("OpenRazer native relay exposes a broader device boundary")
    supplementary_groups = [
        line
        for line in service.splitlines()
        if line.startswith("SupplementaryGroups=")
    ]
    if supplementary_groups != [
        f"SupplementaryGroups={runtime.bridge.client_group}"
    ]:
        raise ModelError("OpenRazer native relay socket group authority drifted")
    required_preflight = (
        "Type=oneshot",
        "DynamicUser=yes",
        f"ExecStart={runtime.operations.activation_path} check-openrazer-driver",
        "Restart=no",
        "TimeoutStartSec=10s",
        "ProtectKernelModules=no",
        "RestrictAddressFamilies=none",
        "CapabilityBoundingSet=\n",
        "AmbientCapabilities=\n",
        "PrivateDevices=yes",
        "DevicePolicy=closed",
        f"InaccessiblePaths={runtime.bridge.runtime_directory}",
        "ReadOnlyPaths=/usr/lib/hyperflux-next /usr/lib/modules",
        "SystemCallFilter=~@mount @module @raw-io @reboot @swap",
        f"Before={deployment.relay.service_unit}",
        f"After={runtime.bridge.service_unit} systemd-udevd.service",
    )
    if any(marker not in preflight for marker in required_preflight):
        raise ModelError("OpenRazer driver metadata preflight sandbox is incomplete")
    preflight_directives = {
        line.partition("=")[0]
        for line in preflight.splitlines()
        if "=" in line and not line.startswith("#")
    }
    if any(
        directive in preflight_directives
        for directive in ("User", "Group", "SupplementaryGroups", "DeviceAllow")
    ):
        raise ModelError("OpenRazer driver metadata preflight gained persistent authority")
    if any(
        marker in preflight
        for marker in ("RemainAfterExit=", "RestartSec=", "Upholds=")
    ):
        raise ModelError("OpenRazer driver metadata preflight gained lifecycle state")
    rust_deployment = (
        root
        / "crates"
        / "hfx-integration-model"
        / "src"
        / "openrazer_native_generated.rs"
    ).read_text(encoding="utf-8")
    if (
        deployment.source_sha256 not in rust_deployment
        or deployment.relay.preflight_service_unit not in rust_deployment
        or any(
            f'"{module}"' not in rust_deployment
            for module in deployment.driver.modules
        )
    ):
        raise ModelError("OpenRazer native Rust deployment projection is incomplete")
    bridge_service = (
        root / "packaging" / "generated" / "hyperflux-next-bridge.service"
    ).read_text(encoding="utf-8")
    full_layer = (
        root
        / "packaging"
        / "generated"
        / "hyperflux-next-bridge.service.d"
        / "50-full.conf"
    ).read_text(encoding="utf-8")
    if (
        deployment.relay.service_unit in bridge_service
        or deployment.relay.preflight_service_unit in bridge_service
        or f"Wants={deployment.relay.service_unit}" not in full_layer
        or f"Before={deployment.relay.service_unit}" not in full_layer
        or deployment.relay.preflight_service_unit in full_layer
        or "Requires=" in full_layer
    ):
        raise ModelError("Full layer does not preserve optional relay ownership")
    install = load_install_manifest(root)
    if (
        install.build("openrazer-native-driver").destination
        != deployment.driver.source_directory
        or install.build("openrazer-native-relay").destination
        != deployment.relay.executable_path
    ):
        raise ModelError("OpenRazer native deployment drifted from package staging")


def _run_device_knowledge_contracts(root: Path, _node: TestNode) -> None:
    inputs = load_knowledge_inputs(root)
    catalog = compiled_knowledge_catalog(root)
    if len(catalog["candidates"]) != 12:
        raise ModelError("compiled device knowledge does not cover the selected snapshot")
    upstreams = {
        upstream["id"]: upstream
        for upstream in load_integration_catalog(root)["upstreams"]
    }
    expected = {
        "openrazer": extract_openrazer_catalog(
            _openrazer_source(root),
            repository=upstreams["openrazer"]["repository"],
            commit=upstreams["openrazer"]["commit"],
            version=upstreams["openrazer"]["version"],
            license_expression=upstreams["openrazer"]["license_expression"],
        ),
        "openrgb": extract_openrgb_catalog(
            _openrgb_source(root),
            repository=upstreams["openrgb"]["repository"],
            commit=upstreams["openrgb"]["commit"],
            version=upstreams["openrgb"]["version"],
            license_expression=upstreams["openrgb"]["license_expression"],
        ),
    }
    if inputs.source_catalogs != expected:
        raise ModelError(
            "committed device-knowledge imports are stale; rerun ./hfx knowledge import"
        )


def _run_native_onboarding_contracts(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_native_onboarding",
            "tests.test_source_validated_universality",
            "-v",
        ],
        "source-validated universality and native onboarding contracts",
        node.timeout_seconds,
        environment={
            "HFX_OPENRAZER_SOURCE_DIR": str(_openrazer_source(root)),
            "HFX_OPENRGB_SOURCE_DIR": str(_openrgb_source(root)),
        },
    )


def _run_repository_atlas_contracts(root: Path, _node: TestNode) -> None:
    atlas = load_repository_atlas(root)
    if len(atlas.nodes) < 24:
        raise ModelError("repository atlas does not cover enough meaningful subsystems")
    if set(atlas.used_by) != set(atlas.by_id):
        raise ModelError("repository atlas inverse relationships are incomplete")


def _run_openrazer_version_matrix_contracts(root: Path, node: TestNode) -> None:
    source = _openrazer_source(root)
    _run_command(
        root,
        [
            sys.executable,
            "tests/openrazer_version_matrix_contracts.py",
            "--source-repository",
            str(source),
            "--verify",
        ],
        "matched OpenRazer userspace version matrix",
        node.timeout_seconds,
    )
