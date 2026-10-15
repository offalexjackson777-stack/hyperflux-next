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
from .coverage import (
    load_rust_coverage_policy,
    parse_llvm_coverage_summary,
    verify_rust_coverage_report,
)
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
from .quality import (
    load_quality_audit,
    verify_current_package_measurements,
    verify_pinned_rust_sanitizer_disposition,
)
from .render import rendered_binary_files, rendered_files
from .profiles import load_profile_inputs
from .protocol import load_protocol_catalog
from .release import load_release_gates
from .security import run_rust_dependency_audit
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
    _check_build_cache_clock,
    _kernel_build_directories,
    _remove_expected_kernel_source_link,
    _run_command,
    _run_cpp_sdk_contracts,
    _run_kernel_profile_contracts,
    _run_python_tests,
    _run_rust_clippy,
    _run_rust_format,
    _run_rust_unit,
    _run_simulator_contracts,
    _tool_environment,
)
from .verify_integration import (
    _openrazer_native_payload_errors_propagate,
    _openrazer_payload_retry_is_safe,
    _openrazer_source,
    _openrgb_source,
    _pinned_upstream_source,
    _run_device_knowledge_contracts,
    _run_native_onboarding_contracts,
    _run_openrazer_metadata_contracts,
    _run_openrazer_native_route_contracts,
    _run_openrazer_version_matrix_contracts,
    _run_repository_atlas_contracts,
)
from .verify_packaging import (
    _qualify_staged_openrgb_plugin,
    _run_package_contracts,
    _staged_tree_snapshot,
)
from .verify_repository import (
    _check_constitution,
    _check_error_contract,
    _check_generated,
    _check_integration_contract,
    _check_profile_contract,
    _check_protocol_contract,
    _check_repository_paths,
    _check_schema_contracts,
    _check_toolchain,
    _resolve_schema_reference,
    _run_assurance_contracts,
    _run_development_environment_contracts,
    _run_device_qualification_console_contracts,
    _run_error_contracts,
    _run_formal_model_contracts,
    _run_foundation_contracts,
    _run_generated_freshness,
    _run_governance_contracts,
    _run_integration_contracts,
    _run_privacy_boundary,
    _run_profile_contracts,
    _run_protocol_contracts,
    _run_repository_documentation_contracts,
    _run_schema_contracts,
    _run_toolchain_contract,
)


def _run_openrgb_cmake_contracts(
    root: Path,
    node: TestNode,
    *,
    build_name: str,
    build_type: str,
    label: str,
    thread_sanitizer: bool,
    native_route: bool,
) -> None:
    source = _openrgb_source(root)
    build_directory = root / "build" / build_name
    if build_directory.exists():
        shutil.rmtree(build_directory)
    native_source: Path | None = None
    if native_route:
        native_source = root / "build" / f"{build_name}-source"
        if native_source.exists():
            shutil.rmtree(native_source)
        prepared = prepare_openrgb_native_source(root, source, native_source)
        if (prepared.result.stat().st_mode & 0o777) != 0o600:
            raise ModelError("OpenRGB native source binding is not private")
        result = load_json(prepared.result)
        if result.get("schema") != "hyperflux-openrgb-native-transform-result-v1":
            raise ModelError("OpenRGB native source binding schema is invalid")
        if any(
            result.get(field) is not False
            for field in (
                "network_access_executed",
                "hardware_queried",
                "hardware_write_executed",
            )
        ):
            raise ModelError("OpenRGB native source preparation crossed a prohibited boundary")
        if (prepared.root / ".git").exists():
            raise ModelError("OpenRGB native source output copied upstream Git metadata")
        native_source = prepared.root
    configure = [
        "cmake",
        "-S",
        "integrations/openrgb",
        "-B",
        str(build_directory),
        f"-DCMAKE_BUILD_TYPE={build_type}",
        "-DBUILD_TESTING=ON",
        f"-DHFX_OPENRGB_SOURCE_DIR={source}",
    ]
    if native_source is not None:
        configure.append(f"-DHFX_OPENRGB_NATIVE_SOURCE_DIR={native_source}")
    if thread_sanitizer:
        configure.append("-DHFX_OPENRGB_THREAD_SANITIZER=ON")
    _run_command(
        root,
        configure,
        f"{label} configure",
        node.timeout_seconds,
    )
    _run_command(
        root,
        ["cmake", "--build", str(build_directory), "--parallel", "4"],
        f"{label} build",
        node.timeout_seconds,
    )
    ctest = ["ctest", "--test-dir", str(build_directory), "--output-on-failure"]
    environment = None
    if thread_sanitizer:
        ctest.extend(["--timeout", "60"])
        environment = {
            "TSAN_OPTIONS": "halt_on_error=1 history_size=7 second_deadlock_stack=1"
        }
    _run_command(
        root,
        ctest,
        f"{label} contracts",
        node.timeout_seconds,
        environment=environment,
    )


def _run_openrgb_adapter_contracts(root: Path, node: TestNode) -> None:
    _run_openrgb_cmake_contracts(
        root,
        node,
        build_name="openrgb-adapter",
        build_type="Release",
        label="OpenRGB adapter",
        thread_sanitizer=False,
        native_route=False,
    )


def _run_openrgb_native_route_contracts(root: Path, node: TestNode) -> None:
    _run_openrgb_cmake_contracts(
        root,
        node,
        build_name="openrgb-native-route",
        build_type="Release",
        label="OpenRGB native route",
        thread_sanitizer=False,
        native_route=True,
    )


def _run_openrgb_thread_sanitizer(root: Path, node: TestNode) -> None:
    _run_openrgb_cmake_contracts(
        root,
        node,
        build_name="openrgb-tsan",
        build_type="RelWithDebInfo",
        label="OpenRGB ThreadSanitizer",
        thread_sanitizer=True,
        native_route=True,
    )


def _run_dependency_security_contracts(root: Path, _node: TestNode) -> None:
    run_rust_dependency_audit(root)


def _run_rust_property_contracts(root: Path, node: TestNode) -> None:
    cases = (
        (
            "hfx-protocol",
            "seeded_framing_and_version_adapter_mutations_fail_closed",
            "protocol framing and version properties",
        ),
        (
            "hfx-uhid-relay",
            "serial::tests::seeded_response_mutations_are_accepted_only_as_valid_private_serials",
            "relay response parsing properties",
        ),
        (
            "hfx-integration-model",
            "source_validated::document::tests::seeded_source_catalog_mutations_never_escape_typed_parsing",
            "source catalog parsing properties",
        ),
        (
            "hfx-profiles",
            "catalog::tests::generated_profile_catalog_permutations_preserve_exact_identity_rules",
            "profile catalog identity properties",
        ),
    )
    for package, test_name, label in cases:
        _run_command(
            root,
            [
                "cargo",
                "test",
                "--locked",
                "--offline",
                "-p",
                package,
                test_name,
                "--",
                "--exact",
            ],
            label,
            node.timeout_seconds,
        )


def _run_rust_coverage_contracts(root: Path, node: TestNode) -> None:
    policy = load_rust_coverage_policy(root)
    report = root / policy.output_path
    if report.is_symlink():
        raise ModelError("Rust coverage output path may not be a symlink")
    if report.exists():
        if not report.is_file():
            raise ModelError("Rust coverage output path is not a regular file")
        report.unlink()
    report.parent.mkdir(parents=True, exist_ok=True)
    _run_command(
        root,
        list(policy.command),
        "Rust workspace coverage",
        node.timeout_seconds,
    )
    # Keep line-level evidence next to the summary, written before the ratchet
    # is checked so it exists exactly when a coverage failure must be explained.
    line_report = report.with_suffix(".lcov")
    if line_report.is_symlink():
        raise ModelError("Rust line coverage output path may not be a symlink")
    if line_report.exists():
        if not line_report.is_file():
            raise ModelError("Rust line coverage output path is not a regular file")
        line_report.unlink()
    _run_command(
        root,
        [
            "cargo",
            "llvm-cov",
            "report",
            "--lcov",
            "--output-path",
            line_report.relative_to(root).as_posix(),
        ],
        "Rust line-level coverage report",
        node.timeout_seconds,
    )
    verify_rust_coverage_report(policy, parse_llvm_coverage_summary(report))


def _run_quality_contracts(root: Path, _node: TestNode) -> None:
    load_quality_audit(root)
    verify_pinned_rust_sanitizer_disposition()
    verify_current_package_measurements(root)


RUNNERS = {
    "foundation-contracts": _run_foundation_contracts,
    "schema-contracts": _run_schema_contracts,
    "profile-contracts": _run_profile_contracts,
    "integration-contracts": _run_integration_contracts,
    "device-knowledge-contracts": _run_device_knowledge_contracts,
    "native-onboarding-contracts": _run_native_onboarding_contracts,
    "repository-atlas-contracts": _run_repository_atlas_contracts,
    "protocol-contracts": _run_protocol_contracts,
    "error-contracts": _run_error_contracts,
    "generated-freshness": _run_generated_freshness,
    "privacy-boundary": _run_privacy_boundary,
    "python-unit": _run_python_tests,
    "toolchain-contract": _run_toolchain_contract,
    "development-environment-contracts": _run_development_environment_contracts,
    "governance-contracts": _run_governance_contracts,
    "repository-documentation-contracts": _run_repository_documentation_contracts,
    "device-qualification-console-contracts": _run_device_qualification_console_contracts,
    "assurance-contracts": _run_assurance_contracts,
    "dependency-security-contracts": _run_dependency_security_contracts,
    "rust-property-contracts": _run_rust_property_contracts,
    "rust-coverage-contracts": _run_rust_coverage_contracts,
    "quality-contracts": _run_quality_contracts,
    "formal-model-contracts": _run_formal_model_contracts,
    "rust-format": _run_rust_format,
    "rust-clippy": _run_rust_clippy,
    "rust-unit": _run_rust_unit,
    "simulator-contracts": _run_simulator_contracts,
    "cpp-sdk-contracts": _run_cpp_sdk_contracts,
    "openrgb-adapter-contracts": _run_openrgb_adapter_contracts,
    "openrgb-native-route-contracts": _run_openrgb_native_route_contracts,
    "openrgb-thread-sanitizer": _run_openrgb_thread_sanitizer,
    "openrazer-metadata-contracts": _run_openrazer_metadata_contracts,
    "openrazer-native-route-contracts": _run_openrazer_native_route_contracts,
    "openrazer-version-matrix-contracts": _run_openrazer_version_matrix_contracts,
    "kernel-profile-contracts": _run_kernel_profile_contracts,
    "package-contracts": _run_package_contracts,
}


def verify_all(root: Path) -> list[str]:
    catalog = load_test_catalog(root)
    passed: list[str] = []
    for node in catalog.ordered():
        print(f"[{node.id}] {node.title}", flush=True)
        RUNNERS[node.runner](root, node)
        passed.append(node.title)
    return passed
