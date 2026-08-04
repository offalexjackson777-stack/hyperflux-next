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
IGNORED_PATH_PARTS = {
    ".git",
    ".hfx",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "target",
}
FUTURE_MTIME_TOLERANCE_SECONDS = 5.0
INSTALLED_SCHEMA_ROOT = Path("/usr/share/hyperflux-next/schemas")


def _tool_environment(root: Path) -> dict[str, str]:
    return toolchain_environment(root)


def _run_command(
    root: Path,
    command: list[str],
    label: str,
    timeout_seconds: int,
    *,
    environment: dict[str, str] | None = None,
) -> None:
    process_environment = _tool_environment(root)
    if environment is not None:
        process_environment.update(environment)
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            env=process_environment,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise ModelError(f"{label} exceeded its {timeout_seconds}s timeout") from error
    if result.returncode != 0:
        raise ModelError(f"{label} failed with exit status {result.returncode}")


def _run_python_tests(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        "repository unit tests",
        node.timeout_seconds,
    )


def _run_cpp_sdk_contracts(root: Path, node: TestNode) -> None:
    binary = root / "build" / "domain-types-smoke"
    binary.parent.mkdir(parents=True, exist_ok=True)
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "tests/cpp/domain_types_smoke.cpp",
            "-o",
            str(binary),
        ],
        "C++ SDK compile",
        node.timeout_seconds,
    )
    _run_command(root, [str(binary)], "C++ SDK smoke test", node.timeout_seconds)
    integration_binary = root / "build" / "integration-catalog-smoke"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "tests/cpp/integration_catalog_smoke.cpp",
            "-o",
            str(integration_binary),
        ],
        "C++ integration catalog compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(integration_binary)],
        "C++ integration catalog smoke test",
        node.timeout_seconds,
    )
    protocol_json_binary = root / "build" / "protocol-json-contract"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "-Isdk/cpp/vendor/include",
            "tests/cpp/protocol_json_contract.cpp",
            "-o",
            str(protocol_json_binary),
        ],
        "C++ protocol JSON contract compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(protocol_json_binary)],
        "C++ protocol JSON contract",
        node.timeout_seconds,
    )
    sdk_client_binary = root / "build" / "sdk-client-contract"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "-Isdk/cpp/vendor/include",
            "tests/cpp/sdk_client_contract.cpp",
            "sdk/cpp/src/client.cpp",
            "sdk/cpp/src/channel.cpp",
            "sdk/cpp/src/identity.cpp",
            "-pthread",
            "-o",
            str(sdk_client_binary),
        ],
        "C++ SDK client contract compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(sdk_client_binary)],
        "C++ SDK client contract",
        node.timeout_seconds,
    )
    sdk_channel_binary = root / "build" / "sdk-channel-contract"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "-Isdk/cpp/vendor/include",
            "tests/cpp/sdk_channel_contract.cpp",
            "sdk/cpp/src/channel.cpp",
            "-pthread",
            "-o",
            str(sdk_channel_binary),
        ],
        "C++ SDK channel contract compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(sdk_channel_binary)],
        "C++ SDK channel contract",
        node.timeout_seconds,
    )
    sdk_lighting_binary = root / "build" / "sdk-lighting-contract"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "-Isdk/cpp/vendor/include",
            "tests/cpp/sdk_lighting_contract.cpp",
            "sdk/cpp/src/lighting.cpp",
            "-o",
            str(sdk_lighting_binary),
        ],
        "C++ SDK lighting contract compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(sdk_lighting_binary)],
        "C++ SDK lighting contract",
        node.timeout_seconds,
    )
    sdk_recovery_binary = root / "build" / "sdk-recovery-contract"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "-Isdk/cpp/vendor/include",
            "tests/cpp/sdk_recovery_contract.cpp",
            "sdk/cpp/src/recovery.cpp",
            "sdk/cpp/src/client.cpp",
            "sdk/cpp/src/channel.cpp",
            "sdk/cpp/src/identity.cpp",
            "-pthread",
            "-o",
            str(sdk_recovery_binary),
        ],
        "C++ SDK recovery contract compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(sdk_recovery_binary)],
        "C++ SDK recovery contract",
        node.timeout_seconds,
    )


def _remove_expected_kernel_source_link(module_directory: Path) -> None:
    source_link = module_directory / "source"
    if source_link.is_symlink() and os.readlink(source_link) == ".":
        source_link.unlink()
    elif source_link.exists() or source_link.is_symlink():
        raise ModelError("kernel build left an unexpected source-tree artifact")


def _kernel_build_directories() -> list[Path]:
    configured = os.environ.get("HFX_KERNEL_BUILD_DIRS")
    if configured:
        build_directories = [Path(value) for value in configured.split(os.pathsep) if value]
    else:
        running = Path("/lib/modules") / os.uname().release / "build"
        if (running / "Makefile").is_file():
            build_directories = [running]
        else:
            build_directories = sorted(
                path
                for root_path in (Path("/lib/modules"), Path("/usr/lib/modules"))
                if root_path.is_dir()
                for path in root_path.glob("*/build")
                if (path / "Makefile").is_file()
            )
    if not build_directories:
        raise ModelError("kernel module verification has no configured header directory")
    unique_build_directories: list[Path] = []
    resolved_directories: set[Path] = set()
    for build_directory in build_directories:
        if not build_directory.is_absolute() or not (build_directory / "Makefile").is_file():
            raise ModelError(f"kernel header directory is unavailable: {build_directory}")
        resolved = build_directory.resolve()
        if resolved in resolved_directories:
            continue
        resolved_directories.add(resolved)
        unique_build_directories.append(build_directory)
    return unique_build_directories


def _run_kernel_profile_contracts(root: Path, node: TestNode) -> None:
    kernel_binary = root / "build" / "kernel-profile-table-smoke"
    kernel_binary.parent.mkdir(parents=True, exist_ok=True)
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "tests/cpp/kernel_profile_table_smoke.cpp",
            "-o",
            str(kernel_binary),
        ],
        "kernel profile table compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(kernel_binary)],
        "kernel profile table smoke test",
        node.timeout_seconds,
    )
    uapi_binary = root / "build" / "kernel-uapi-smoke"
    _run_command(
        root,
        [
            "clang",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Idriver/kernel/uapi",
            "tests/c/kernel_uapi_smoke.c",
            "-o",
            str(uapi_binary),
        ],
        "kernel UAPI compile",
        node.timeout_seconds,
    )
    _run_command(root, [str(uapi_binary)], "kernel UAPI smoke test", node.timeout_seconds)

    protocol_binary = root / "build" / "kernel-protocol-smoke"
    _run_command(
        root,
        [
            "clang",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Idriver/kernel",
            "-Idriver/kernel/uapi",
            "tests/c/kernel_protocol_smoke.c",
            "-o",
            str(protocol_binary),
        ],
        "kernel protocol compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(protocol_binary)],
        "kernel protocol smoke test",
        node.timeout_seconds,
    )

    build_directories = _kernel_build_directories()
    source_directory = root / "driver" / "kernel"
    source_link = source_directory / "source"
    if source_link.exists() or source_link.is_symlink():
        raise ModelError("kernel source tree contains an unexpected build artifact")
    for build_directory in build_directories:
        label = build_directory.resolve().parent.name
        output_directory = root / "build" / "kernel" / label
        if output_directory.exists():
            shutil.rmtree(output_directory)
        module_directory = output_directory / "source"
        shutil.copytree(source_directory, module_directory, symlinks=True)
        _run_command(
            root,
            [
                "make",
                "-C",
                str(build_directory),
                f"M={module_directory}",
                "clean",
            ],
            f"kernel verification copy cleanup ({label})",
            node.timeout_seconds,
        )
        _remove_expected_kernel_source_link(module_directory)
        _run_command(
            root,
            [
                "make",
                "-C",
                str(build_directory),
                f"M={module_directory}",
                "W=1",
                "modules",
            ],
            f"kernel module build ({label})",
            node.timeout_seconds,
        )
        _remove_expected_kernel_source_link(module_directory)
        if source_link.exists() or source_link.is_symlink():
            raise ModelError("kernel verification modified the canonical source tree")


def _check_build_cache_clock(root: Path, *, now: float | None = None) -> None:
    target = root / "target"
    if not target.is_dir():
        return
    current = time.time() if now is None else now
    cutoff = current + FUTURE_MTIME_TOLERANCE_SECONDS
    future: list[Path] = []
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        try:
            modified = path.stat().st_mtime
        except OSError as error:
            raise ModelError(f"cannot inspect build cache timestamp: {path}") from error
        if modified > cutoff:
            future.append(path.relative_to(root))
            if len(future) == 3:
                break
    if future:
        sample = ", ".join(path.as_posix() for path in future)
        raise ModelError(
            "build cache contains future-dated artifacts and may produce false green results; "
            f"run cargo clean before verification (examples: {sample})"
        )


def _run_rust_format(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        ["cargo", "fmt", "--all", "--", "--check"],
        "Rust formatting",
        node.timeout_seconds,
    )


def _run_rust_clippy(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        ["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"],
        "Rust Clippy",
        node.timeout_seconds,
    )


def _run_rust_unit(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        ["cargo", "test", "--workspace", "--exclude", "hfx-sim", "--all-targets", "--locked"],
        "Rust domain and profile tests",
        node.timeout_seconds,
    )


def _run_simulator_contracts(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        ["cargo", "test", "-p", "hfx-sim", "--all-targets", "--locked"],
        "virtual receiver and deterministic replay tests",
        node.timeout_seconds,
    )
