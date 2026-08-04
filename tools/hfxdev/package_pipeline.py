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


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    label: str,
    timeout_seconds: int = 900,
) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ModelError(f"{label}: {error}") from error
    if result.returncode != 0:
        raise ModelError(f"{label} failed with exit status {result.returncode}")


def _source_identity(root: Path, revision: str | None) -> tuple[str, int]:
    if (root / ".git").exists():
        head = _output(["git", "rev-parse", "HEAD"], cwd=root, label="source revision")
        if revision is None:
            revision = head
        elif revision != head:
            raise ModelError("explicit source revision does not match the checked-out commit")
        dirty = _output(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            label="source worktree status",
        )
        if dirty:
            raise ModelError("package build requires a clean tracked worktree")
        epoch_text = _output(
            ["git", "show", "-s", "--format=%ct", revision],
            cwd=root,
            label="source timestamp",
        )
    else:
        if revision is None:
            raise ModelError("source revision is required outside a Git checkout")
        epoch_text = os.environ.get("SOURCE_DATE_EPOCH", "")
        if not epoch_text:
            raise ModelError("SOURCE_DATE_EPOCH is required with an explicit source revision")
    if not REVISION.fullmatch(revision):
        raise ModelError("source revision must be a lowercase 40-character Git object id")
    try:
        epoch = int(epoch_text)
    except ValueError as error:
        raise ModelError("source timestamp is not an integer") from error
    if not 1 <= epoch <= 4_102_444_800:
        raise ModelError("source timestamp is outside the supported reproducible range")
    return revision, epoch


from .package_pipeline_build import (
    BuiltArtifact,
    ArtifactSet,
    StageResult,
)

def _copy_artifact(
    source: Path,
    artifacts_root: Path,
    build: BuildSpec,
    *,
    kind: str,
    destination: str | None = None,
    mode: int,
) -> BuiltArtifact:
    if not source.is_file() or source.is_symlink():
        raise ModelError(f"build {build.id}: expected artifact is missing")
    component = artifacts_root / "files" / build.id
    component.mkdir(parents=True, exist_ok=True)
    target = component / source.name
    shutil.copyfile(source, target, follow_symlinks=False)
    target.chmod(mode)
    return BuiltArtifact(
        build_id=build.id,
        distribution_component=build.distribution_component,
        kind=kind,
        path=target,
        sha256=sha256_file(target),
        size=target.stat().st_size,
        mode=mode,
        destination=destination,
    )


def _pinned_source(root: Path, capability: str, source: Path) -> Path:
    if not source.is_absolute():
        raise ModelError(f"{capability}: upstream source must be an absolute path")
    source = source.resolve()
    upstream_id = capability.removesuffix("-source")
    expected = {
        upstream["id"]: upstream["commit"]
        for upstream in load_integration_catalog(root)["upstreams"]
    }.get(upstream_id)
    if expected is None:
        raise ModelError(f"{capability}: no pinned upstream contract")
    actual = _output(["git", "rev-parse", "HEAD"], cwd=source, label=capability)
    if actual != expected:
        raise ModelError(f"{capability}: source does not match the pinned revision")
    dirty = _output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=source,
        label=f"{capability} worktree status",
    )
    if dirty:
        raise ModelError(f"{capability}: pinned upstream worktree is not clean")
    return source


from .package_pipeline_build import (
    _source_tree_has_license,
    _output,
    _new_output_directory,
    _base_environment,
    _build_cargo,
    _build_cmake,
    _openrazer_dkms_projection,
    _openrazer_source_file_projection,
    _prepare_openrazer_source_transform,
    _openrazer_dkms_projection_files,
    _verify_openrazer_dkms_contract,
    _write_source_tree_archive,
    _artifact_json,
    _provenance_inputs,
    _provenance_materials,
    build_artifacts,
)
from .package_pipeline_manifest import (
    _manifest_artifact,
    _manifest_material,
    load_artifact_set,
)
from .package_pipeline_staging import (
    _stage_path,
    _copy_to_stage,
    _write_to_stage,
    _install_source_tree,
    _tree_digest,
    _inspect_staged_files,
    _private_stage_prefixes,
    _component_directory_modes,
    stage_rootfs,
)
