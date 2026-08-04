# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
from typing import Any

from .install import BuildSpec, InstallManifest, load_install_manifest
from .development import load_development_environment
from .generators.supply_chain import spdx_json
from .integrations import load_integration_catalog
from .linux_runtime import LinuxRuntime, load_linux_runtime
from .model import ModelError, load_json, require_unique, sha256_file
from .openrazer_native_route import (
    PreparedOpenRazerNativeSource,
    load_native_deployment,
    load_native_transform as load_openrazer_native_transform,
    prepare_openrazer_native_source,
)
from .openrgb_native_route import (
    load_native_transform as load_openrgb_native_transform,
    prepare_openrgb_native_source,
)
from .supply_chain import load_dependency_inventory
from .toolchains import load_toolchain_pins, verify_current_toolchain
from .tree_metadata import normalize_tree as _normalize_tree


REVISION = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_NAME = "package-build-manifest.json"
SOURCE_TREE_REQUIRED_FILES = frozenset({"Makefile", "dkms.conf", "driver/Makefile"})


from .package_pipeline_build import (
    ArtifactSet,
    BuiltArtifact,
    _provenance_inputs,
    _provenance_materials,
)


def _source_identity(*args: Any, **kwargs: Any) -> Any:
    from . import package_pipeline

    return package_pipeline._source_identity(*args, **kwargs)


def _manifest_artifact(root: Path, value: Any, label: str) -> BuiltArtifact:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: must be an object")
    required = {
        "build_id",
        "distribution_component",
        "kind",
        "path",
        "sha256",
        "size",
        "mode",
        "destination",
    }
    optional: set[str] = set()
    if not required <= value.keys() or value.keys() - required - optional:
        raise ModelError(f"{label}: fields do not match the artifact contract")
    build_id = value["build_id"]
    distribution_component = value["distribution_component"]
    kind = value["kind"]
    if not isinstance(build_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", build_id):
        raise ModelError(f"{label}: invalid build id")
    if distribution_component not in {"linux", "full"}:
        raise ModelError(f"{label}: invalid distribution component")
    if kind not in {
        "cargo-binary",
        "cmake-module",
        "source-tree",
        "source-file",
    }:
        raise ModelError(f"{label}: invalid artifact kind")
    path_value = value["path"]
    if not isinstance(path_value, str) or path_value.startswith("/"):
        raise ModelError(f"{label}: artifact path must be relative")
    relative = PurePosixPath(path_value)
    if ".." in relative.parts or str(relative) != path_value:
        raise ModelError(f"{label}: artifact path is not normalized")
    root = root.resolve()
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ModelError(f"{label}: artifact file is missing") from error
    if (
        not resolved.is_relative_to(root)
        or resolved != path
        or not path.is_file()
        or path.is_symlink()
    ):
        raise ModelError(f"{label}: artifact file is missing")
    digest = value["sha256"]
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest) or sha256_file(path) != digest:
        raise ModelError(f"{label}: artifact digest mismatch")
    size = value["size"]
    if not isinstance(size, int) or isinstance(size, bool) or path.stat().st_size != size:
        raise ModelError(f"{label}: artifact size mismatch")
    mode_value = value["mode"]
    if mode_value not in {"0644", "0755"}:
        raise ModelError(f"{label}: invalid artifact mode")
    destination = value.get("destination")
    if destination is None or (
        not isinstance(destination, str)
        or not destination.startswith(("/etc/", "/usr/"))
        or ".." in PurePosixPath(destination).parts
    ):
        raise ModelError(f"{label}: invalid artifact destination")
    return BuiltArtifact(
        build_id=build_id,
        distribution_component=distribution_component,
        kind=kind,
        path=path,
        sha256=digest,
        size=size,
        mode=int(mode_value, 8),
        destination=destination,
    )


def _manifest_material(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not {"id", "uri"} <= value.keys():
        raise ModelError(f"{label}: malformed material")
    if set(value) not in ({"id", "uri", "sha256"}, {"id", "uri", "revision"}):
        raise ModelError(f"{label}: material must carry exactly one identity digest")
    material_id = value["id"]
    uri = value["uri"]
    if (
        not isinstance(material_id, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", material_id)
        or not isinstance(uri, str)
        or not uri
        or len(uri) > 256
        or "/home/" in uri
    ):
        raise ModelError(f"{label}: invalid material identity")
    if "sha256" in value and (
        not isinstance(value["sha256"], str) or not DIGEST.fullmatch(value["sha256"])
    ):
        raise ModelError(f"{label}: invalid material digest")
    if "revision" in value and (
        not isinstance(value["revision"], str) or not REVISION.fullmatch(value["revision"])
    ):
        raise ModelError(f"{label}: invalid material revision")
    return dict(value)


def load_artifact_set(root: Path, manifest_path: Path) -> ArtifactSet:
    root = root.resolve()
    manifest_path = manifest_path.resolve()
    value = load_json(manifest_path)
    expected_keys = {
        "$schema",
        "schema",
        "source",
        "builder",
        "toolchain",
        "inputs",
        "materials",
        "artifacts",
        "omitted",
    }
    if (
        set(value) != expected_keys
        or value["$schema"]
        != "https://hyperflux.dev/schemas/package-build-manifest-v2.json"
        or value["schema"] != "hyperflux-package-build-manifest-v2"
    ):
        raise ModelError("unsupported package build manifest")
    source = value["source"]
    builder = value["builder"]
    toolchain = value["toolchain"]
    inputs = value["inputs"]
    if not isinstance(source, dict) or set(source) != {"revision", "source_date_epoch"}:
        raise ModelError("package build source identity is malformed")
    if builder != {
        "id": "https://hyperflux.dev/builders/hfx-package-v2",
        "network_access": False,
    }:
        raise ModelError("package builder identity is malformed")
    toolchain_keys = {
        "rustc",
        "cargo",
        "python",
        "pip",
        "setuptools",
        "wheel",
        "clang",
        "cmake",
        "ninja",
        "target",
    }
    if (
        not isinstance(toolchain, dict)
        or set(toolchain) != toolchain_keys
        or not all(isinstance(toolchain[key], str) and toolchain[key] for key in toolchain_keys)
    ):
        raise ModelError("package build toolchain identity is malformed")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", toolchain["python"]):
        raise ModelError("package Python identity is malformed")
    if not re.fullmatch(
        r"[A-Za-z0-9_+.]+(?:-[A-Za-z0-9_+.]+){2,4}", toolchain["target"]
    ):
        raise ModelError("package target identity is malformed")
    pins = load_toolchain_pins(root)
    pinned_toolchain = {
        "rustc": pins.rustc,
        "cargo": pins.cargo,
        "pip": pins.pip,
        "setuptools": pins.setuptools,
        "wheel": pins.wheel,
        "clang": pins.clang,
        "cmake": pins.cmake,
        "ninja": pins.ninja,
    }
    if any(toolchain[key] != expected for key, expected in pinned_toolchain.items()):
        raise ModelError("package build toolchain differs from the repository pins")
    if ".".join(toolchain["python"].split(".")[:2]) != pins.python:
        raise ModelError("package Python identity differs from the repository pin")
    if not isinstance(inputs, dict) or set(inputs) != {
        "install_manifest_sha256",
        "linux_runtime_sha256",
        "dependency_policy_sha256",
        "sbom_sha256",
        "toolchain_pins_sha256",
        "cargo_lock_sha256",
        "integration_catalog_sha256",
        "source_validated_catalog_sha256",
        "openrazer_native_catalog_sha256",
        "openrazer_native_transform_sha256",
        "openrazer_native_deployment_sha256",
        "openrazer_native_patch_sha256",
        "openrgb_native_transform_sha256",
        "openrgb_native_patch_sha256",
    }:
        raise ModelError("package build inputs are malformed")
    revision = source["revision"]
    epoch = source["source_date_epoch"]
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ModelError("package build revision is malformed")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or not 1 <= epoch <= 4_102_444_800:
        raise ModelError("package source timestamp is malformed")

    if (root / ".git").exists():
        current_revision, current_epoch = _source_identity(root, revision)
        if current_revision != revision or current_epoch != epoch:
            raise ModelError("package build artifacts do not match the checked-out source")

    install = load_install_manifest(root)
    runtime = load_linux_runtime(root)
    expected_inputs = _provenance_inputs(root, install, runtime)
    if inputs != expected_inputs:
        raise ModelError("package build manifest uses stale or incomplete source materials")

    material_values = value["materials"]
    if not isinstance(material_values, list):
        raise ModelError("package build materials are malformed")
    materials = [
        _manifest_material(item, f"material {index}")
        for index, item in enumerate(material_values)
    ]
    require_unique([item["id"] for item in materials], "package material id")
    if materials != sorted(materials, key=lambda item: item["id"]):
        raise ModelError("package build materials are not canonical")

    artifact_values = value["artifacts"]
    omitted_values = value["omitted"]
    if not isinstance(artifact_values, list) or not isinstance(omitted_values, list):
        raise ModelError("package build artifact lists are malformed")
    artifacts = tuple(
        _manifest_artifact(manifest_path.parent, item, f"artifact {index}")
        for index, item in enumerate(artifact_values)
    )
    require_unique([artifact.build_id for artifact in artifacts], "package artifact build id")
    omitted: list[tuple[str, str]] = []
    for index, item in enumerate(omitted_values):
        if not isinstance(item, dict) or set(item) != {"build_id", "reason"}:
            raise ModelError(f"omission {index}: malformed entry")
        if not isinstance(item["build_id"], str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{0,63}", item["build_id"]
        ):
            raise ModelError(f"omission {index}: invalid build id")
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ModelError(f"omission {index}: empty reason")
        omitted.append((item["build_id"], item["reason"]))
    require_unique([build_id for build_id, _ in omitted], "omitted build id")

    declared = {build.id: build for build in install.builds}
    accounted = {artifact.build_id for artifact in artifacts} | {
        build_id for build_id, _ in omitted
    }
    if accounted != set(declared):
        raise ModelError("package build manifest does not account for every declared build")
    for artifact in artifacts:
        build = declared.get(artifact.build_id)
        if build is None:
            raise ModelError(f"artifact {artifact.build_id}: build is not declared")
        expected_kind = {
            "source-transform": "source-tree",
            "source-transform-file": "source-file",
        }.get(build.kind, build.kind)
        if artifact.kind != expected_kind:
            raise ModelError(f"artifact {artifact.build_id}: kind does not match install contract")
        if artifact.distribution_component != build.distribution_component:
            raise ModelError(
                f"artifact {artifact.build_id}: distribution component drifted"
            )
        if artifact.destination != build.destination:
            raise ModelError(f"artifact {artifact.build_id}: install target drifted")
    for build_id, _ in omitted:
        build = declared.get(build_id)
        if build is None or build.required:
            raise ModelError(f"omission {build_id}: required or unknown build cannot be omitted")
    supplied_capabilities = {
        build.capability
        for artifact in artifacts
        if (build := declared[artifact.build_id]).capability is not None
    }
    if materials != _provenance_materials(root, inputs, supplied_capabilities):
        raise ModelError("package build materials differ from the declared source inputs")
    return ArtifactSet(
        root=manifest_path.parent,
        revision=revision,
        source_date_epoch=epoch,
        install_manifest_sha256=inputs["install_manifest_sha256"],
        linux_runtime_sha256=inputs["linux_runtime_sha256"],
        python=toolchain["python"],
        target=toolchain["target"],
        artifacts=artifacts,
        omitted=tuple(omitted),
    )
