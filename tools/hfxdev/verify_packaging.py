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
from .installed_baseline import load_installed_baselines
from .knowledge import compiled_knowledge_catalog, load_knowledge_inputs
from .linux_runtime import load_linux_runtime
from .licensing import verify_licensing_policy
from .local_companion import load_local_companion
from .migration import execute_shadow_comparison, load_shadow_fixture
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
from .reconstruction import load_reconstruction_ledger
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

from .verify_compiled import _run_command
from .verify_integration import _openrazer_source, _openrgb_source


def _staged_tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    snapshot: list[tuple[object, ...]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ModelError(f"package stage contains a symbolic link: {relative}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            snapshot.append((relative, "directory", mode, path.stat().st_mtime_ns))
        elif path.is_file():
            snapshot.append(
                (
                    relative,
                    "file",
                    mode,
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    sha256_file(path),
                )
            )
        else:
            raise ModelError(f"package stage contains an unsupported file type: {relative}")
    return tuple(snapshot)


def _qualify_staged_openrgb_plugin(
    root: Path,
    node: TestNode,
    plugin: Path,
    workspace: Path,
) -> None:
    source = _openrgb_source(root)
    build_directory = workspace / "openrgb-sdk-harness"
    if build_directory.exists():
        shutil.rmtree(build_directory)
    build_directory.mkdir()
    project = root / "tests" / "openrgb_plugin_manager_sdk_harness.pro"
    _run_command(
        root,
        [
            "qmake",
            "-o",
            str(build_directory / "Makefile"),
            str(project),
            f"HFX_OPENRGB_SOURCE_DIR={source}",
        ],
        "pinned OpenRGB PluginManager harness configure",
        node.timeout_seconds,
    )
    _run_command(
        root,
        ["make", "-C", str(build_directory), "--jobs=4"],
        "pinned OpenRGB PluginManager harness build",
        node.timeout_seconds,
    )
    harness = build_directory / "openrgb-plugin-manager-sdk-harness"
    if not harness.is_file() or harness.is_symlink():
        raise ModelError("pinned OpenRGB PluginManager harness was not produced")

    upstreams = {
        upstream["id"]: upstream
        for upstream in load_integration_catalog(root)["upstreams"]
    }
    openrgb_commit = upstreams["openrgb"]["commit"]
    evidence = workspace / "openrgb-packaged-plugin-sdk-qualification.json"
    _run_command(
        root,
        [
            sys.executable,
            "tests/openrgb_packaged_plugin_sdk_qualification.py",
            "--harness",
            str(harness),
            "--plugin",
            str(plugin),
            "--openrgb-commit",
            openrgb_commit,
            "--evidence",
            str(evidence),
            "--runtime",
            "system",
        ],
        "isolated packaged OpenRGB PluginManager and SDK qualification",
        node.timeout_seconds,
    )
    result = load_json(evidence)
    inputs = result.get("inputs")
    if (
        result.get("schema") != "hyperflux-openrgb-isolated-qualification-v1"
        or result.get("isolation", {}).get("runtime") != "system"
        or not isinstance(inputs, dict)
        or inputs.get("openrgb_commit") != openrgb_commit
        or inputs.get("harness_sha256") != sha256_file(harness)
        or inputs.get("plugin_sha256") != sha256_file(plugin)
        or any(
            not isinstance(result.get(case), dict)
            or result[case].get("result") != "pass"
            for case in (
                "positive",
                "historical_constant_identity",
                "missing_ownership_leases",
                "missing_native_receiver_tunnel",
            )
        )
    ):
        raise ModelError("packaged OpenRGB SDK qualification evidence is invalid")


def _run_package_contracts(root: Path, node: TestNode) -> None:
    workspace = root / "build" / "package-contracts"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    manifest_path = build_artifacts(
        root,
        workspace / "artifacts",
        capabilities={
            "openrgb-source": _openrgb_source(root),
            "openrazer-source": _openrazer_source(root),
        },
    )
    artifacts = load_artifact_set(root, manifest_path)
    if artifacts.omitted:
        raise ModelError("complete package contract unexpectedly omitted a build product")

    stages: dict[str, tuple[StageResult, StageResult]] = {}
    destinations: dict[str, set[str]] = {}
    for component in ("linux", "full"):
        first = stage_rootfs(
            root, manifest_path, workspace / f"{component}-root-a", component
        )
        second = stage_rootfs(
            root, manifest_path, workspace / f"{component}-root-b", component
        )
        if (
            first.payload_sha256 != second.payload_sha256
            or first.file_count != second.file_count
            or first.inventory.read_bytes() != second.inventory.read_bytes()
            or _staged_tree_snapshot(first.root)
            != _staged_tree_snapshot(second.root)
        ):
            raise ModelError(
                f"independent {component} package stages are not byte-for-byte reproducible"
            )
        stages[component] = (first, second)
        destinations[component] = {
            path.relative_to(first.root).as_posix()
            for path in first.root.rglob("*")
            if path.is_file()
        }
    if any(
        destinations[left] & destinations[right]
        for index, left in enumerate(("linux", "full"))
        for right in ("linux", "full")[index + 1 :]
    ):
        raise ModelError("split package stages contain overlapping file paths")

    first = stages["linux"][0]

    _run_command(
        root,
        [str(first.root / "usr/bin/hyperfluxctl"), "--help"],
        "staged operations CLI",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [
            str(first.root / "usr/lib/hyperflux-next/hyperflux-next-bridge"),
            "--help",
        ],
        "staged bridge CLI",
        node.timeout_seconds,
    )
    plugin_stage = stages["full"][0]
    plugin = plugin_stage.root / "usr/lib/openrgb/plugins/hyperflux-next-openrgb.so"
    try:
        dynamic = subprocess.run(
            ["readelf", "-d", str(plugin)],
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=node.timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect staged OpenRGB module: {error}") from error
    if dynamic.returncode != 0 or "(RPATH)" in dynamic.stdout or "(RUNPATH)" in dynamic.stdout:
        raise ModelError("staged OpenRGB module has an invalid dynamic-library contract")
    _qualify_staged_openrgb_plugin(root, node, plugin, workspace)

    staged_payload_size = sum(
        path.stat().st_size
        for component in ("linux", "full")
        for path in stages[component][0].root.rglob("*")
        if path.is_file()
    )
    verify_package_performance_budgets(
        load_performance_budgets(root),
        {artifact.build_id: artifact.size for artifact in artifacts.artifacts},
        staged_payload_size,
    )

    first_arch = build_distribution_package(
        root,
        manifest_path,
        "arch",
        workspace / "arch-package-a",
    )
    second_arch = build_distribution_package(
        root,
        manifest_path,
        "arch",
        workspace / "arch-package-b",
    )
    first_packages = {
        package.package_name: package.package.read_bytes()
        for package in first_arch.packages
    }
    second_packages = {
        package.package_name: package.package.read_bytes()
        for package in second_arch.packages
    }
    if first_packages != second_packages or (
        first_arch.manifest.read_bytes() != second_arch.manifest.read_bytes()
    ):
        raise ModelError("independent Arch package builds are not byte-for-byte reproducible")
    full = next(
        package
        for package in first_arch.packages
        if package.component == "full"
    )
    linux = next(
        package
        for package in first_arch.packages
        if package.component == "linux"
    )
    verify_arch_openrazer_update_policy(
        full.package,
        package_name=full.package_name,
        dependency_packages=(linux.package,),
        workspace=workspace / "openrazer-update-policy",
        timeout_seconds=node.timeout_seconds,
    )
