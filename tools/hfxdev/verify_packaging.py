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
from .arch_split import load_arch_split
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
from .package_pipeline import (
    MANIFEST_NAME,
    StageResult,
    build_artifacts,
    load_artifact_set,
    stage_rootfs,
)
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
EXTERNAL_OPENRGB_QUALIFICATION_BINDING = (
    "HFX_EXTERNAL_OPENRGB_QUALIFICATION_BINDING"
)
OPENRGB_QUALIFICATION_REQUEST_SCHEMA = (
    "hyperflux-openrgb-external-qualification-request-v1"
)
OPENRGB_QUALIFICATION_BINDING_SCHEMA = (
    "hyperflux-openrgb-external-qualification-binding-v1"
)
MAX_OPENRGB_QUALIFICATION_DOCUMENT_BYTES = 1_048_576
PACKAGE_BUILD_REUSE_NAME = "package-build-reuse.json"
PACKAGE_BUILD_REUSE_SCHEMA = "hyperflux-package-build-reuse-v1"
# What the qualification step leaves in the package-contracts workspace for
# the complete graph to reuse.
PACKAGE_BUILD_REUSED_ENTRIES = frozenset(
    {"artifacts", "openrgb-sdk-harness", PACKAGE_BUILD_REUSE_NAME}
)

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


def _git_object_id(root: Path, revision: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", revision],
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot resolve source identity {revision}: {error}") from error
    value = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ModelError(f"source identity is unavailable: {revision}")
    return value


def _source_date_epoch(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "HEAD"],
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot resolve source date: {error}") from error
    value = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise ModelError("source date is unavailable")
    return value


def _source_identity(root: Path) -> dict[str, str]:
    return {
        "revision": _git_object_id(root, "HEAD^{commit}"),
        "tree": _git_object_id(root, "HEAD^{tree}"),
    }


def _clean_source_identity(root: Path) -> dict[str, str] | None:
    """Return the source identity, or None when the checkout has local changes."""
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect the checkout state: {error}") from error
    if completed.returncode != 0:
        raise ModelError("cannot inspect the checkout state")
    if completed.stdout.strip():
        return None
    return _source_identity(root)


def _record_package_build_reuse(
    root: Path, workspace: Path, before: dict[str, str] | None
) -> None:
    """Let the complete graph reuse this build when it came from a clean checkout."""
    if before is None or _clean_source_identity(root) != before:
        return
    record = {"schema": PACKAGE_BUILD_REUSE_SCHEMA, "source": before}
    (workspace / PACKAGE_BUILD_REUSE_NAME).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _reusable_package_build(root: Path, workspace: Path) -> Path | None:
    """Return the manifest of the qualified package build when it is still exact.

    The external OpenRGB qualification builds the complete artifact set and
    the PluginManager harness in this workspace. Package contracts reuse them
    only when that qualification is bound to this run, and the build and this
    run both saw the same clean commit. Anything else rebuilds from source.
    The reused manifest is still checked against the checkout, the toolchain
    pins and every artifact hash when it is loaded.
    """

    if not os.environ.get(EXTERNAL_OPENRGB_QUALIFICATION_BINDING):
        return None
    marker = workspace / PACKAGE_BUILD_REUSE_NAME
    manifest = workspace / "artifacts" / MANIFEST_NAME
    if (
        workspace.is_symlink()
        or not marker.is_file()
        or marker.is_symlink()
        or not manifest.is_file()
        or manifest.is_symlink()
    ):
        return None
    current = _clean_source_identity(root)
    record = _bounded_json_document(marker, "package build reuse record")
    if current is None or record != {
        "schema": PACKAGE_BUILD_REUSE_SCHEMA,
        "source": current,
    }:
        return None
    for entry in workspace.iterdir():
        if entry.name in PACKAGE_BUILD_REUSED_ENTRIES:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    return manifest


def _bounded_json_document(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ModelError(f"{label} is not a regular non-symbolic-link file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_OPENRGB_QUALIFICATION_DOCUMENT_BYTES:
        raise ModelError(f"{label} is empty or exceeds its byte bound")
    value = load_json(path)
    if not isinstance(value, dict):
        raise ModelError(f"{label} must be a JSON object")
    return value


def _qualification_path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ModelError(f"{label} path is missing")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not pure.parts
        or pure.parts[0] != "build"
    ):
        raise ModelError(f"{label} path must stay below build/")
    path = root / pure
    if path.resolve(strict=False) != path or path.is_symlink():
        raise ModelError(f"{label} path may not traverse a symbolic link")
    return path


def _validate_openrgb_qualification_evidence(
    evidence: Path,
    *,
    harness: Path,
    plugin: Path,
    openrgb_commit: str,
) -> dict[str, object]:
    result = _bounded_json_document(
        evidence, "packaged OpenRGB SDK qualification evidence"
    )
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
    return result


def _pinned_openrgb_commit(root: Path) -> str:
    upstreams = {
        upstream["id"]: upstream
        for upstream in load_integration_catalog(root)["upstreams"]
    }
    return upstreams["openrgb"]["commit"]


def _build_openrgb_qualification_harness(
    root: Path,
    workspace: Path,
    timeout_seconds: int,
) -> tuple[Path, str]:
    source = _openrgb_source(root)
    build_directory = workspace / "openrgb-sdk-harness"
    if build_directory.exists():
        shutil.rmtree(build_directory)
    build_directory.mkdir()
    project = root / "tests" / "openrgb_plugin_manager_sdk_harness.pro"
    # Upstream bakes the configure time into the binary unless
    # SOURCE_DATE_EPOCH is set; the qualified and packaged builds must match.
    environment = {"SOURCE_DATE_EPOCH": _source_date_epoch(root)}
    _run_command(
        root,
        [
            "qmake6",
            "-o",
            str(build_directory / "Makefile"),
            str(project),
            f"HFX_OPENRGB_SOURCE_DIR={source}",
        ],
        "pinned OpenRGB PluginManager harness configure",
        timeout_seconds,
        environment=environment,
    )
    _run_command(
        root,
        ["make", "-C", str(build_directory), "--jobs=4"],
        "pinned OpenRGB PluginManager harness build",
        timeout_seconds,
        environment=environment,
    )
    harness = build_directory / "openrgb-plugin-manager-sdk-harness"
    if not harness.is_file() or harness.is_symlink():
        raise ModelError("pinned OpenRGB PluginManager harness was not produced")
    return harness, _pinned_openrgb_commit(root)


def _consume_external_openrgb_qualification(
    root: Path,
    *,
    binding_path: Path,
    destination: Path,
    harness: Path,
    plugin: Path,
    openrgb_commit: str,
) -> None:
    binding = _bounded_json_document(
        binding_path, "external packaged OpenRGB qualification binding"
    )
    expected_inputs = {
        "harness_sha256": sha256_file(harness),
        "openrgb_commit": openrgb_commit,
        "plugin_sha256": sha256_file(plugin),
        "runtime": "system",
    }
    request_record = binding.get("request")
    evidence_record = binding.get("evidence")
    if (
        set(binding) != {
            "schema",
            "source",
            "request",
            "inputs",
            "isolation_authority",
            "evidence",
        }
        or binding.get("schema") != OPENRGB_QUALIFICATION_BINDING_SCHEMA
        or binding.get("source") != _source_identity(root)
        or binding.get("inputs") != expected_inputs
        or binding.get("isolation_authority") != "outer-networkless-device-free-oci"
        or not isinstance(request_record, dict)
        or set(request_record) != {"path", "sha256"}
        or not isinstance(evidence_record, dict)
        or set(evidence_record) != {"path", "sha256"}
    ):
        raise ModelError("external packaged OpenRGB qualification binding is invalid")
    request_path = _qualification_path(
        root, request_record.get("path"), "external qualification request"
    )
    if (
        not isinstance(request_record.get("sha256"), str)
        or sha256_file(request_path) != request_record["sha256"]
    ):
        raise ModelError("external packaged OpenRGB qualification request hash drifted")
    request, request_harness, request_plugin = (
        load_external_openrgb_qualification_request(root, request_path)
    )
    if (
        request.get("source") != binding.get("source")
        or request.get("inputs") != expected_inputs
        or sha256_file(request_harness) != sha256_file(harness)
        or sha256_file(request_plugin) != sha256_file(plugin)
    ):
        raise ModelError("external packaged OpenRGB qualification request drifted")
    evidence = _qualification_path(
        root, evidence_record.get("path"), "external qualification evidence"
    )
    if (
        not isinstance(evidence_record.get("sha256"), str)
        or sha256_file(evidence) != evidence_record["sha256"]
    ):
        raise ModelError("external packaged OpenRGB qualification evidence hash drifted")
    result = _validate_openrgb_qualification_evidence(
        evidence,
        harness=harness,
        plugin=plugin,
        openrgb_commit=openrgb_commit,
    )
    isolation = result.get("isolation")
    if (
        not isinstance(isolation, dict)
        or isolation.get("authority") != "outer-oci-container"
        or isolation.get("network_namespace") != "loopback-only"
        or isolation.get("process_view") != "empty-tmpfs"
        or isolation.get("sysfs") != "empty-tmpfs"
        or isolation.get("hidraw_visible") is not False
        or isolation.get("uhid_visible") is not False
        or isolation.get("harness_capability_bounding_set") != "empty"
        or isolation.get("harness_capability_drop_preflight") != "pass"
        or isolation.get("outer_setup_capabilities")
        != ["DAC_OVERRIDE", "NET_ADMIN", "SETPCAP", "SYS_ADMIN"]
    ):
        raise ModelError("external packaged OpenRGB isolation evidence is incomplete")
    destination.write_bytes(evidence.read_bytes())


def _qualify_staged_openrgb_plugin(
    root: Path,
    node: TestNode,
    plugin: Path,
    workspace: Path,
    *,
    reuse_harness: bool = False,
) -> None:
    if reuse_harness:
        harness = workspace / "openrgb-sdk-harness" / "openrgb-plugin-manager-sdk-harness"
        if not harness.is_file() or harness.is_symlink():
            raise ModelError("qualified OpenRGB PluginManager harness is unavailable")
        openrgb_commit = _pinned_openrgb_commit(root)
    else:
        harness, openrgb_commit = _build_openrgb_qualification_harness(
            root, workspace, node.timeout_seconds
        )
    evidence = workspace / "openrgb-packaged-plugin-sdk-qualification.json"
    external_binding = os.environ.get(EXTERNAL_OPENRGB_QUALIFICATION_BINDING)
    if external_binding:
        _consume_external_openrgb_qualification(
            root,
            binding_path=_qualification_path(
                root, external_binding, "external qualification binding"
            ),
            destination=evidence,
            harness=harness,
            plugin=plugin,
            openrgb_commit=openrgb_commit,
        )
        return
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
    _validate_openrgb_qualification_evidence(
        evidence,
        harness=harness,
        plugin=plugin,
        openrgb_commit=openrgb_commit,
    )


def prepare_external_openrgb_qualification(
    root: Path,
    output: Path,
    *,
    timeout_seconds: int = 1_800,
) -> Path:
    root = root.resolve()
    output = output.resolve()
    try:
        relative_output = output.relative_to(root)
    except ValueError as error:
        raise ModelError(
            "external OpenRGB qualification output must stay in the repository"
        ) from error
    if (
        not relative_output.parts
        or relative_output.parts[0] != "build"
        or output.is_symlink()
    ):
        raise ModelError("external OpenRGB qualification output must stay below build/")
    if output.exists() and not output.is_dir():
        raise ModelError("external OpenRGB qualification output is not a directory")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    workspace = root / "build" / "package-contracts"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    source = _clean_source_identity(root)
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
        raise ModelError("external qualification build omitted a package artifact")
    stage = stage_rootfs(
        root, manifest_path, workspace / "external-full-root", "full"
    )
    plugin = stage.root / "usr/lib/openrgb/plugins/hyperflux-next-openrgb.so"
    if plugin.is_symlink() or not plugin.is_file():
        raise ModelError("external qualification plugin is unavailable")
    harness, openrgb_commit = _build_openrgb_qualification_harness(
        root, workspace, timeout_seconds
    )
    inputs = output / "inputs"
    inputs.mkdir()
    qualified_harness = inputs / "openrgb-plugin-manager-sdk-harness"
    qualified_plugin = inputs / "hyperflux-next-openrgb.so"
    shutil.copy2(harness, qualified_harness, follow_symlinks=False)
    shutil.copy2(plugin, qualified_plugin, follow_symlinks=False)
    _record_package_build_reuse(root, workspace, source)
    harness = qualified_harness
    plugin = qualified_plugin
    request = {
        "schema": OPENRGB_QUALIFICATION_REQUEST_SCHEMA,
        "source": _source_identity(root),
        "inputs": {
            "harness_sha256": sha256_file(harness),
            "openrgb_commit": openrgb_commit,
            "plugin_sha256": sha256_file(plugin),
            "runtime": "system",
        },
        "paths": {
            "harness": harness.relative_to(root).as_posix(),
            "plugin": plugin.relative_to(root).as_posix(),
        },
    }
    destination = output / "request.json"
    destination.write_text(
        json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


def load_external_openrgb_qualification_request(
    root: Path,
    request_path: Path,
) -> tuple[dict[str, object], Path, Path]:
    root = root.resolve()
    request = _bounded_json_document(
        request_path, "external OpenRGB qualification request"
    )
    if (
        set(request) != {"schema", "source", "inputs", "paths"}
        or request.get("schema") != OPENRGB_QUALIFICATION_REQUEST_SCHEMA
        or request.get("source") != _source_identity(root)
        or not isinstance(request.get("inputs"), dict)
        or set(request["inputs"])
        != {"harness_sha256", "openrgb_commit", "plugin_sha256", "runtime"}
        or request["inputs"].get("runtime") != "system"
        or not isinstance(request.get("paths"), dict)
        or set(request["paths"]) != {"harness", "plugin"}
    ):
        raise ModelError("external OpenRGB qualification request is invalid")
    harness = _qualification_path(
        root, request["paths"].get("harness"), "qualification harness"
    )
    plugin = _qualification_path(
        root, request["paths"].get("plugin"), "qualification plugin"
    )
    inputs = request["inputs"]
    if (
        not harness.is_file()
        or not plugin.is_file()
        or harness.is_symlink()
        or plugin.is_symlink()
        or inputs.get("harness_sha256") != sha256_file(harness)
        or inputs.get("plugin_sha256") != sha256_file(plugin)
        or not isinstance(inputs.get("openrgb_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", inputs["openrgb_commit"]) is None
    ):
        raise ModelError("external OpenRGB qualification inputs drifted")
    return request, harness, plugin


def finalize_external_openrgb_qualification(
    root: Path,
    *,
    request_path: Path,
    evidence_path: Path,
    binding_path: Path,
) -> Path:
    root = root.resolve()
    request, harness, plugin = load_external_openrgb_qualification_request(
        root, request_path
    )
    inputs = request["inputs"]
    if not isinstance(inputs, dict):
        raise ModelError("external OpenRGB qualification request lost its inputs")
    result = _validate_openrgb_qualification_evidence(
        evidence_path,
        harness=harness,
        plugin=plugin,
        openrgb_commit=inputs["openrgb_commit"],
    )
    isolation = result.get("isolation")
    if (
        not isinstance(isolation, dict)
        or isolation.get("authority") != "outer-oci-container"
        or isolation.get("container_root") is not True
        or isolation.get("harness_capability_bounding_set") != "empty"
        or isolation.get("harness_capability_drop_preflight") != "pass"
        or isolation.get("outer_setup_capabilities")
        != ["DAC_OVERRIDE", "NET_ADMIN", "SETPCAP", "SYS_ADMIN"]
        or isolation.get("network_namespace") != "loopback-only"
        or isolation.get("process_view") != "empty-tmpfs"
        or isolation.get("sysfs") != "empty-tmpfs"
        or isolation.get("hidraw_visible") is not False
        or isolation.get("uhid_visible") is not False
    ):
        raise ModelError("external OpenRGB qualification did not prove OCI isolation")
    try:
        evidence_relative = evidence_path.resolve().relative_to(root).as_posix()
        request_relative = request_path.resolve().relative_to(root).as_posix()
    except ValueError as error:
        raise ModelError(
            "external OpenRGB qualification evidence must stay in the repository"
        ) from error
    binding = {
        "schema": OPENRGB_QUALIFICATION_BINDING_SCHEMA,
        "source": request["source"],
        "request": {
            "path": request_relative,
            "sha256": sha256_file(request_path),
        },
        "inputs": inputs,
        "isolation_authority": "outer-networkless-device-free-oci",
        "evidence": {
            "path": evidence_relative,
            "sha256": sha256_file(evidence_path),
        },
    }
    if binding_path.exists() or binding_path.is_symlink():
        raise ModelError("external OpenRGB qualification binding already exists")
    binding_path.write_text(
        json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return binding_path


def _arch_full_edition_packages(root: Path, packages: tuple) -> tuple:
    """Return the Full edition's Arch package and the siblings it depends on.

    Before the four-output topology is active the build has one `linux` and
    one `full` package. Once it is active, the Full edition maps to a split
    output whose sibling dependencies come from `packaging/arch-split.json`.
    """

    split = load_arch_split(root)
    if split.active:
        full_id = split.edition_output("full").id
        dependency_ids = tuple(item for item in split.closure(full_id) if item != full_id)
    else:
        full_id, dependency_ids = "full", ("linux",)
    by_component = {package.component: package for package in packages}
    missing = [item for item in (full_id, *dependency_ids) if item not in by_component]
    if missing:
        raise ModelError(
            "Arch package build lacks the Full edition package set: " + ", ".join(missing)
        )
    return by_component[full_id], tuple(by_component[item] for item in dependency_ids)


def _run_package_contracts(root: Path, node: TestNode) -> None:
    workspace = root / "build" / "package-contracts"
    reused_manifest = _reusable_package_build(root, workspace)
    if reused_manifest is not None:
        manifest_path = reused_manifest
    else:
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

    try:
        operations_help = subprocess.run(
            [str(first.root / "usr/bin/hyperfluxctl"), "--help"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=node.timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise ModelError(
            f"staged operations CLI exceeded its {node.timeout_seconds}s timeout"
        ) from error
    if operations_help.returncode != 0:
        raise ModelError(
            f"staged operations CLI failed with exit status {operations_help.returncode}"
        )
    if "hyperfluxctl qualification [--json] [--output PATH]" not in operations_help.stdout:
        raise ModelError("staged operations CLI does not offer the qualification report")
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
    _qualify_staged_openrgb_plugin(
        root, node, plugin, workspace, reuse_harness=reused_manifest is not None
    )

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
    full, dependencies = _arch_full_edition_packages(root, first_arch.packages)
    verify_arch_openrazer_update_policy(
        full.package,
        package_name=full.package_name,
        dependency_packages=tuple(package.package for package in dependencies),
        workspace=workspace / "openrazer-update-policy",
        timeout_seconds=node.timeout_seconds,
    )
