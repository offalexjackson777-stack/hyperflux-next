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
    BuiltArtifact,
    StageResult,
    _new_output_directory,
    _source_tree_has_license,
)
from .package_pipeline_manifest import load_artifact_set


def _stage_path(root: Path, destination: str | PurePosixPath) -> Path:
    path = PurePosixPath(destination)
    if not path.is_absolute() or len(path.parts) < 2 or path.parts[1] not in {"etc", "usr"}:
        raise ModelError(f"unsafe staged destination: {destination}")
    return root.joinpath(*path.parts[1:])


def _copy_to_stage(source: Path, destination: Path, mode: int) -> None:
    if not source.is_file() or source.is_symlink():
        raise ModelError(f"staged source is not a regular file: {source}")
    if destination.exists() or destination.is_symlink():
        raise ModelError(f"staged destination collision: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)
    destination.chmod(mode)


def _write_to_stage(destination: Path, payload: bytes, mode: int) -> None:
    if destination.exists() or destination.is_symlink():
        raise ModelError(f"staged destination collision: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    destination.chmod(mode)


def _install_source_tree(stage_root: Path, artifact: BuiltArtifact) -> None:
    if artifact.destination is None:
        raise ModelError(f"artifact {artifact.build_id}: source tree has no destination")
    destination = _stage_path(stage_root, artifact.destination)
    if destination.exists() or destination.is_symlink():
        raise ModelError(f"artifact {artifact.build_id}: source-tree destination collides")
    try:
        archive = tarfile.open(artifact.path, mode="r:")
    except (OSError, tarfile.TarError) as error:
        raise ModelError(f"artifact {artifact.build_id}: invalid source-tree archive") from error
    files: set[str] = set()
    directories: set[str] = set()
    with archive:
        members = archive.getmembers()
        if not 4 <= len(members) <= 1_024:
            raise ModelError(f"artifact {artifact.build_id}: source-tree member bound failed")
        if sum(member.size for member in members) > 64 * 1024 * 1024:
            raise ModelError(f"artifact {artifact.build_id}: source tree exceeds its size bound")
        names: set[str] = set()
        for member in members:
            raw_name = member.name[:-1] if member.name.endswith("/") else member.name
            relative = PurePosixPath(raw_name)
            if (
                not raw_name
                or raw_name.startswith("/")
                or ".." in relative.parts
                or str(relative) != raw_name
                or raw_name in names
                or member.pax_headers
            ):
                raise ModelError(f"artifact {artifact.build_id}: unsafe source-tree member")
            names.add(raw_name)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                if member.mode != 0o755:
                    raise ModelError(f"artifact {artifact.build_id}: invalid directory mode")
                target.mkdir(parents=True, exist_ok=False)
                target.chmod(0o755)
                directories.add(raw_name)
                continue
            if not member.isfile() or member.mode != 0o644:
                raise ModelError(f"artifact {artifact.build_id}: unsupported source-tree member")
            source = archive.extractfile(member)
            if source is None:
                raise ModelError(f"artifact {artifact.build_id}: source-tree member is unreadable")
            _write_to_stage(target, source.read(), 0o644)
            files.add(raw_name)
    if (
        not SOURCE_TREE_REQUIRED_FILES <= files
        or not _source_tree_has_license(files)
        or "driver" not in directories
        or not any(name.startswith("driver/") and name.endswith(".c") for name in files)
        or not any(name.startswith("driver/") and name.endswith(".h") for name in files)
    ):
        raise ModelError(f"artifact {artifact.build_id}: DKMS source tree is incomplete")


def _tree_digest(files: list[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.stat().st_mode)
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(f"{mode:04o}".encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _inspect_staged_files(stage_root: Path, private_prefixes: tuple[bytes, ...]) -> list[Path]:
    files: list[Path] = []
    for path in sorted(stage_root.rglob("*")):
        if path.is_symlink():
            raise ModelError(f"staged root contains a symbolic link: {path.relative_to(stage_root)}")
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"/home/" in data or any(prefix and prefix in data for prefix in private_prefixes):
            raise ModelError(f"staged file contains a private build path: {path.relative_to(stage_root)}")
        files.append(path)
    return files


def _private_stage_prefixes(
    root: Path,
    artifacts_root: Path,
    canonical_workspace: Path,
) -> tuple[bytes, ...]:
    prefixes = [str(artifacts_root).encode()]
    if root != canonical_workspace:
        prefixes.append(str(root).encode())
    return tuple(prefixes)


def _component_directory_modes(
    runtime: LinuxRuntime, distribution_component: str
) -> tuple[tuple[str, int], ...]:
    if distribution_component != "linux":
        return ()
    return (
        (
            runtime.bridge.configuration_directory,
            runtime.bridge.directory_modes.configuration,
        ),
    )


# A Full package without the OpenRGB plugin leaves out the plugin build and
# installs the descriptor that names the OpenRazer route alone. A release that
# builds the plugin against its own Qt stages that module in place of the
# shared one.
OPENRGB_PLUGIN_BUILD = "openrgb-native"
FULL_COMPONENT_DESCRIPTOR = "full-component-descriptor"
FULL_COMPONENT_ROUTE_ONLY_SOURCE = "packaging/generated/full-component-route-only.json"


def stage_rootfs(
    root: Path,
    manifest_path: Path,
    stage_root: Path,
    distribution_component: str = "linux",
    *,
    openrgb_plugin: bool = True,
    openrgb_plugin_module: Path | None = None,
) -> StageResult:
    root = root.resolve()
    install = load_install_manifest(root)
    if distribution_component not in {"linux", "full"}:
        raise ModelError("package staging: unsupported distribution component")
    if not openrgb_plugin and distribution_component != "full":
        raise ModelError("package staging: only Full carries the OpenRGB plugin")
    if openrgb_plugin_module is not None and (
        not openrgb_plugin or distribution_component != "full"
    ):
        raise ModelError("package staging: a release plugin module needs Full with the plugin")
    artifacts = load_artifact_set(root, manifest_path)
    stage_root = _new_output_directory(stage_root, "package staging")

    for file in install.files_for(distribution_component):
        source = file.source
        if not openrgb_plugin and file.component == FULL_COMPONENT_DESCRIPTOR:
            source = root / FULL_COMPONENT_ROUTE_ONLY_SOURCE
        _copy_to_stage(
            source,
            _stage_path(stage_root, file.destination),
            file.mode,
        )
    for artifact in artifacts.artifacts:
        if artifact.distribution_component != distribution_component:
            continue
        if not openrgb_plugin and artifact.build_id == OPENRGB_PLUGIN_BUILD:
            continue
        if artifact.kind == "source-tree":
            _install_source_tree(stage_root, artifact)
            continue
        if artifact.destination is None:
            raise ModelError(f"artifact {artifact.build_id}: missing destination")
        source = artifact.path
        if openrgb_plugin_module is not None and artifact.build_id == OPENRGB_PLUGIN_BUILD:
            source = openrgb_plugin_module
        _copy_to_stage(
            source,
            _stage_path(stage_root, artifact.destination),
            artifact.mode,
        )

    runtime = load_linux_runtime(root)
    directory_modes = _component_directory_modes(runtime, distribution_component)
    _normalize_tree(stage_root, artifacts.source_date_epoch, directory_modes)
    canonical_workspace = Path(load_development_environment(root).workspace_path)
    private_prefixes = _private_stage_prefixes(root, artifacts.root, canonical_workspace)
    staged_files = _inspect_staged_files(stage_root, private_prefixes)
    payload_digest = _tree_digest(staged_files, stage_root)

    preserve = {
        str(file.destination): file.preserve
        for file in install.files_for(distribution_component)
        if file.preserve
    }
    inventory_value = {
        "schema": "hyperflux-staged-files-v2",
        "distribution_component": distribution_component,
        "openrgb_plugin": openrgb_plugin,
        "source_revision": artifacts.revision,
        "source_date_epoch": artifacts.source_date_epoch,
        "install_manifest_sha256": install.source_sha256,
        "linux_runtime_sha256": artifacts.linux_runtime_sha256,
        "payload_sha256": payload_digest,
        "omitted_optional_builds": [
            {"build_id": build_id, "reason": reason}
            for build_id, reason in artifacts.omitted
            if install.build(build_id).distribution_component
            == distribution_component
        ],
        "files": [
            {
                "path": "/" + path.relative_to(stage_root).as_posix(),
                "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "configuration_preserved": preserve.get(
                    "/" + path.relative_to(stage_root).as_posix(), False
                ),
            }
            for path in staged_files
        ],
    }
    inventory = stage_root.parent / f"{stage_root.name}.inventory.json"
    if inventory.exists() or inventory.is_symlink():
        raise ModelError("package staging inventory destination already exists")
    inventory.write_text(
        json.dumps(inventory_value, indent=2, sort_keys=False, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    inventory.chmod(0o644)
    os.utime(inventory, (artifacts.source_date_epoch, artifacts.source_date_epoch))
    _normalize_tree(stage_root, artifacts.source_date_epoch, directory_modes)
    return StageResult(
        root=stage_root,
        inventory=inventory,
        distribution_component=distribution_component,
        payload_sha256=payload_digest,
        file_count=len(staged_files),
    )
