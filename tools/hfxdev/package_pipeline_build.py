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


def _facade():
    from . import package_pipeline

    return package_pipeline


def _run(*args: Any, **kwargs: Any) -> Any:
    return _facade()._run(*args, **kwargs)


def _source_identity(*args: Any, **kwargs: Any) -> Any:
    return _facade()._source_identity(*args, **kwargs)


def _copy_artifact(*args: Any, **kwargs: Any) -> Any:
    return _facade()._copy_artifact(*args, **kwargs)


def _pinned_source(*args: Any, **kwargs: Any) -> Any:
    return _facade()._pinned_source(*args, **kwargs)


def prepare_openrgb_native_source(*args: Any, **kwargs: Any) -> Any:
    return _facade().prepare_openrgb_native_source(*args, **kwargs)


def _source_tree_has_license(files: set[str]) -> bool:
    if "LICENSE" in files:
        return True
    return any(
        len(path.parts) == 2
        and path.parts[0] == "LICENSES"
        and path.suffix == ".txt"
        for path in map(PurePosixPath, files)
    )


@dataclass(frozen=True)
class BuiltArtifact:
    build_id: str
    distribution_component: str
    kind: str
    path: Path
    sha256: str
    size: int
    mode: int
    destination: str | None


@dataclass(frozen=True)
class ArtifactSet:
    root: Path
    revision: str
    source_date_epoch: int
    install_manifest_sha256: str
    linux_runtime_sha256: str
    python: str
    target: str
    artifacts: tuple[BuiltArtifact, ...]
    omitted: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class StageResult:
    root: Path
    inventory: Path
    distribution_component: str
    payload_sha256: str
    file_count: int


def _output(command: list[str], *, cwd: Path, label: str) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"{label}: {error}") from error
    return result.stdout.strip()


def _new_output_directory(path: Path, label: str) -> Path:
    path = path.resolve()
    if path == Path("/") or path == Path.home():
        raise ModelError(f"{label}: refusing unsafe output directory")
    if path.exists():
        if not path.is_dir() or path.is_symlink() or any(path.iterdir()):
            raise ModelError(f"{label}: output directory must be absent or empty")
    else:
        path.mkdir(parents=True)
    return path


def _base_environment(root: Path, output: Path, epoch: int) -> dict[str, str]:
    environment = os.environ.copy()
    cargo_home = Path(
        environment.get("CARGO_HOME", str(Path.home() / ".cargo"))
    ).expanduser().resolve()
    remaps = (
        (output, ".hfx-build"),
        (root, "."),
        (cargo_home, ".cargo"),
    )
    rust_remap = " ".join(
        f"--remap-path-prefix={source}={destination}"
        for source, destination in remaps
    )
    environment.update(
        {
            "SOURCE_DATE_EPOCH": str(epoch),
            "PYTHONHASHSEED": "0",
            "CARGO_INCREMENTAL": "0",
            "CARGO_NET_OFFLINE": "true",
            "CARGO_PROFILE_RELEASE_STRIP": "symbols",
            "CARGO_TARGET_DIR": str(output / "work" / "cargo-target"),
            "RUSTFLAGS": rust_remap,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "CC": "clang",
            "CXX": "clang++",
            "CMAKE_GENERATOR": "Ninja",
        }
    )
    remap = " ".join(
        f"-ffile-prefix-map={source}={destination} "
        f"-fdebug-prefix-map={source}={destination}"
        for source, destination in remaps
    )
    environment["CFLAGS"] = f"{environment.get('CFLAGS', '')} {remap}".strip()
    environment["CXXFLAGS"] = f"{environment.get('CXXFLAGS', '')} {remap}".strip()
    return environment


def _build_cargo(
    root: Path,
    output: Path,
    environment: dict[str, str],
    builds: list[BuildSpec],
) -> list[BuiltArtifact]:
    by_package: dict[tuple[str, str], list[BuildSpec]] = {}
    for build in builds:
        if build.package is None or build.target is None or build.cargo_profile is None:
            raise ModelError(f"build {build.id}: incomplete Cargo declaration")
        by_package.setdefault((build.package, build.cargo_profile), []).append(build)
    for (package, profile), package_builds in sorted(by_package.items()):
        command = [
            "cargo",
            "build",
            "--profile",
            profile,
            "--locked",
            "--offline",
            "--package",
            package,
        ]
        for build in package_builds:
            command.extend(["--bin", build.target or ""])
        _run(
            command,
            cwd=root,
            environment=environment,
            label=f"Cargo package {package}",
        )
    return [
        _copy_artifact(
            Path(environment["CARGO_TARGET_DIR"])
            / (build.cargo_profile or "")
            / (build.target or ""),
            output,
            build,
            kind="cargo-binary",
            destination=build.destination,
            mode=build.mode or 0o755,
        )
        for build in builds
    ]


def _build_cmake(
    root: Path,
    output: Path,
    environment: dict[str, str],
    build: BuildSpec,
    capabilities: dict[str, Path],
    revision: str,
) -> BuiltArtifact | None:
    capability = build.capability or ""
    source = capabilities.get(capability)
    if source is None:
        if build.required:
            raise ModelError(f"build {build.id}: required capability {capability} is absent")
        return None
    upstream = _pinned_source(root, capability, source)
    environment = environment.copy()
    upstream_remap = (
        f"-ffile-prefix-map={upstream}=.upstream/{capability} "
        f"-fdebug-prefix-map={upstream}=.upstream/{capability}"
    )
    environment["CFLAGS"] = f"{environment.get('CFLAGS', '')} {upstream_remap}".strip()
    environment["CXXFLAGS"] = (
        f"{environment.get('CXXFLAGS', '')} {upstream_remap}"
    ).strip()
    native_upstream: Path | None = None
    if capability == "openrgb-source":
        native_upstream = output / "work" / f"{build.id}-native-source"
        prepared = prepare_openrgb_native_source(root, upstream, native_upstream)
        native_remap = (
            f"-ffile-prefix-map={prepared.root}=.upstream/{capability}-native "
            f"-fdebug-prefix-map={prepared.root}=.upstream/{capability}-native"
        )
        environment["CFLAGS"] = (
            f"{environment.get('CFLAGS', '')} {native_remap}"
        ).strip()
        environment["CXXFLAGS"] = (
            f"{environment.get('CXXFLAGS', '')} {native_remap}"
        ).strip()
    build_directory = output / "work" / build.id
    configure = [
        "cmake",
        "-S",
        str(root / (build.source or "")),
        "-B",
        str(build_directory),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_TESTING=OFF",
        f"-DHFX_SOURCE_REVISION={revision}",
    ]
    if capability == "openrgb-source":
        configure.append(f"-DHFX_OPENRGB_SOURCE_DIR={upstream}")
        configure.append(f"-DHFX_OPENRGB_NATIVE_SOURCE_DIR={native_upstream}")
    _run(configure, cwd=root, environment=environment, label=f"configure {build.id}")
    _run(
        ["cmake", "--build", str(build_directory), "--target", build.target or ""],
        cwd=root,
        environment=environment,
        label=f"build {build.id}",
    )
    return _copy_artifact(
        build_directory / (build.output or ""),
        output,
        build,
        kind="cmake-module",
        destination=build.destination,
        mode=build.mode or 0o755,
    )


def _openrazer_dkms_projection(
    root: Path,
    output: Path,
    environment: dict[str, str],
    build: BuildSpec,
    prepared: PreparedOpenRazerNativeSource,
) -> BuiltArtifact:
    if build.transform != "openrazer-native-route" or build.capability != "openrazer-source":
        raise ModelError(f"build {build.id}: unsupported source transform")
    deployment = load_native_deployment(root)
    _verify_openrazer_dkms_contract(prepared.root, deployment)

    projection = output / "work" / build.id / "projection"
    projection.mkdir(parents=True)
    selected = list(_openrazer_dkms_projection_files(prepared.root, projection))
    for source_path, destination in selected:
        if not source_path.is_file() or source_path.is_symlink():
            raise ModelError(f"build {build.id}: OpenRazer DKMS source projection is incomplete")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination, follow_symlinks=False)
        destination.chmod(0o644)
    if len(selected) < 8:
        raise ModelError(f"build {build.id}: OpenRazer DKMS source projection is empty")

    epoch = int(environment["SOURCE_DATE_EPOCH"])
    archive = output / "work" / build.id / (
        f"{deployment.driver.dkms_name}-{deployment.driver.dkms_version}.tar"
    )
    _write_source_tree_archive(projection, archive, epoch)
    return _copy_artifact(
        archive,
        output,
        build,
        kind="source-tree",
        destination=build.destination,
        mode=build.mode or 0o644,
    )


def _openrazer_source_file_projection(
    output: Path,
    build: BuildSpec,
    prepared: PreparedOpenRazerNativeSource,
) -> BuiltArtifact:
    if build.transform != "openrazer-native-route" or build.capability != "openrazer-source":
        raise ModelError(f"build {build.id}: unsupported source transform file")
    if build.source is None or build.destination is None or build.mode is None:
        raise ModelError(f"build {build.id}: source transform file is incomplete")
    source = prepared.root / PurePosixPath(build.source)
    if (
        not source.is_file()
        or source.is_symlink()
        or not source.resolve().is_relative_to(prepared.root.resolve())
    ):
        raise ModelError(f"build {build.id}: transformed source file is missing")
    return _copy_artifact(
        source,
        output,
        build,
        kind="source-file",
        destination=build.destination,
        mode=build.mode,
    )


def _prepare_openrazer_source_transform(
    root: Path,
    output: Path,
    builds: list[BuildSpec],
    capabilities: dict[str, Path],
) -> PreparedOpenRazerNativeSource:
    if any(
        build.transform != "openrazer-native-route"
        or build.capability != "openrazer-source"
        for build in builds
    ):
        raise ModelError("OpenRazer source transform build set is inconsistent")
    source_value = capabilities.get("openrazer-source")
    if source_value is None:
        raise ModelError("required capability openrazer-source is absent")
    source = _pinned_source(root, "openrazer-source", source_value)
    prepared_path = output / "work" / "openrazer-native-route" / "prepared"
    return prepare_openrazer_native_source(root, source, prepared_path)


def _openrazer_dkms_projection_files(
    prepared: Path, projection: Path
) -> tuple[tuple[Path, Path], ...]:
    selected = [
        (prepared / "Makefile", projection / "Makefile"),
        (
            prepared / "install_files" / "dkms" / "dkms.conf",
            projection / "dkms.conf",
        ),
        (prepared / "driver" / "Makefile", projection / "driver" / "Makefile"),
    ]
    selected.extend(
        (path, projection / "driver" / path.name)
        for pattern in ("*.c", "*.h")
        for path in sorted((prepared / "driver").glob(pattern))
    )
    license_files = []
    legacy_license = prepared / "LICENSE"
    if legacy_license.is_file() and not legacy_license.is_symlink():
        license_files.append((legacy_license, projection / "LICENSE"))
    reuse_licenses = prepared / "LICENSES"
    if reuse_licenses.is_dir() and not reuse_licenses.is_symlink():
        license_files.extend(
            (path, projection / "LICENSES" / path.name)
            for path in sorted(reuse_licenses.glob("*.txt"))
            if path.is_file() and not path.is_symlink()
        )
    selected.extend(license_files)
    projected_files = {
        destination.relative_to(projection).as_posix()
        for _, destination in selected
    }
    if not _source_tree_has_license(projected_files):
        raise ModelError("OpenRazer DKMS source projection has no regular license text")
    return tuple(selected)


def _verify_openrazer_dkms_contract(prepared: Path, deployment: Any) -> None:
    path = prepared / "install_files" / "dkms" / "dkms.conf"
    if not path.is_file() or path.is_symlink():
        raise ModelError("OpenRazer native DKMS configuration is missing")
    text = path.read_text(encoding="utf-8")
    name = re.search(r'^PACKAGE_NAME="([^"]+)"$', text, re.MULTILINE)
    version = re.search(r'^PACKAGE_VERSION="([^"]+)"$', text, re.MULTILINE)
    modules = tuple(
        sorted(re.findall(r'^BUILT_MODULE_NAME\[[0-9]+\]="([a-z0-9_-]+)"$', text, re.MULTILINE))
    )
    if (
        name is None
        or version is None
        or name.group(1) != deployment.driver.dkms_name
        or version.group(1) != deployment.driver.dkms_version
        or modules != deployment.driver.modules
    ):
        raise ModelError("OpenRazer native DKMS configuration differs from deployment authority")


def _write_source_tree_archive(source: Path, destination: Path, epoch: int) -> None:
    with tarfile.open(destination, "w", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
            relative = path.relative_to(source).as_posix()
            if path.is_symlink():
                raise ModelError("source transform produced a symbolic link")
            info = tarfile.TarInfo(relative + ("/" if path.is_dir() else ""))
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = epoch
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            elif path.is_file():
                info.mode = 0o644
                info.size = path.stat().st_size
                with path.open("rb") as source_file:
                    archive.addfile(info, source_file)
            else:
                raise ModelError("source transform produced an unsupported filesystem entry")
    os.utime(destination, (epoch, epoch))


def _artifact_json(artifact: BuiltArtifact, root: Path) -> dict[str, Any]:
    value: dict[str, Any] = {
        "build_id": artifact.build_id,
        "distribution_component": artifact.distribution_component,
        "kind": artifact.kind,
        "path": artifact.path.relative_to(root).as_posix(),
        "sha256": artifact.sha256,
        "size": artifact.size,
        "mode": f"{artifact.mode:04o}",
    }
    if artifact.destination is not None:
        value["destination"] = artifact.destination
    return value


def _provenance_inputs(
    root: Path, install: InstallManifest, runtime: LinuxRuntime
) -> dict[str, str]:
    inventory = load_dependency_inventory(root)
    sbom_path = root / "assurance" / "generated" / "hyperflux-next.spdx.json"
    expected_sbom = spdx_json(inventory)
    if not sbom_path.is_file() or sbom_path.read_text(encoding="utf-8") != expected_sbom:
        raise ModelError("generated SPDX inventory is stale; run ./hfx generate")
    openrazer_transform = load_openrazer_native_transform(root)
    deployment = load_native_deployment(root)
    openrazer_root = root / "integrations" / "openrazer" / "native-route"
    openrazer_patch = openrazer_root / openrazer_transform["patch"]["path"]
    openrgb_transform = load_openrgb_native_transform(root)
    openrgb_root = root / "integrations" / "openrgb" / "native-route"
    openrgb_patch = openrgb_root / openrgb_transform["patch"]["path"]
    return {
        "install_manifest_sha256": install.source_sha256,
        "linux_runtime_sha256": runtime.source_sha256,
        "dependency_policy_sha256": sha256_file(root / "assurance" / "dependencies.json"),
        "sbom_sha256": sha256_file(sbom_path),
        "toolchain_pins_sha256": sha256_file(root / "toolchains" / "pins.json"),
        "cargo_lock_sha256": sha256_file(root / "Cargo.lock"),
        "integration_catalog_sha256": sha256_file(root / "integrations" / "catalog.json"),
        "source_validated_catalog_sha256": sha256_file(
            root / "integrations" / "native-route" / "source-validated.json"
        ),
        "openrazer_native_catalog_sha256": sha256_file(openrazer_root / "catalog.json"),
        "openrazer_native_transform_sha256": sha256_file(openrazer_root / "transform.json"),
        "openrazer_native_deployment_sha256": deployment.source_sha256,
        "openrazer_native_patch_sha256": sha256_file(openrazer_patch),
        "openrgb_native_transform_sha256": sha256_file(openrgb_root / "transform.json"),
        "openrgb_native_patch_sha256": sha256_file(openrgb_patch),
    }


def _provenance_materials(
    root: Path,
    inputs: dict[str, str],
    supplied_capabilities: set[str],
) -> list[dict[str, str]]:
    paths = {
        "cargo-lock": ("Cargo.lock", "cargo_lock_sha256"),
        "dependency-policy": (
            "assurance/dependencies.json",
            "dependency_policy_sha256",
        ),
        "install-manifest": ("packaging/install.json", "install_manifest_sha256"),
        "integration-catalog": (
            "integrations/catalog.json",
            "integration_catalog_sha256",
        ),
        "linux-runtime": ("runtime/linux.json", "linux_runtime_sha256"),
        "openrazer-native-catalog": (
            "integrations/openrazer/native-route/catalog.json",
            "openrazer_native_catalog_sha256",
        ),
        "source-validated-device-catalog": (
            "integrations/native-route/source-validated.json",
            "source_validated_catalog_sha256",
        ),
        "openrazer-native-deployment": (
            "integrations/openrazer/native-route/deployment.json",
            "openrazer_native_deployment_sha256",
        ),
        "openrazer-native-patch": (
            "integrations/openrazer/native-route/patches/0001-Add-trusted-HyperFlux-native-HID-transport.patch",
            "openrazer_native_patch_sha256",
        ),
        "openrazer-native-transform": (
            "integrations/openrazer/native-route/transform.json",
            "openrazer_native_transform_sha256",
        ),
        "openrgb-native-patch": (
            "integrations/openrgb/native-route/patches/0001-Add-HyperFlux-feature-transport-seam.patch",
            "openrgb_native_patch_sha256",
        ),
        "openrgb-native-transform": (
            "integrations/openrgb/native-route/transform.json",
            "openrgb_native_transform_sha256",
        ),
        "source-sbom": (
            "assurance/generated/hyperflux-next.spdx.json",
            "sbom_sha256",
        ),
        "toolchain-pins": ("toolchains/pins.json", "toolchain_pins_sha256"),
    }
    materials = [
        {"id": material_id, "uri": path, "sha256": inputs[input_id]}
        for material_id, (path, input_id) in sorted(paths.items())
    ]
    upstreams = {
        f"{upstream['id']}-source": upstream
        for upstream in load_integration_catalog(root)["upstreams"]
    }
    unknown = sorted(supplied_capabilities - upstreams.keys())
    if unknown:
        raise ModelError(f"package build received unknown source capabilities: {', '.join(unknown)}")
    for capability in sorted(supplied_capabilities):
        upstream = upstreams[capability]
        materials.append(
            {
                "id": f"upstream-{upstream['id']}",
                "uri": upstream["repository"],
                "revision": upstream["commit"],
            }
        )
    return sorted(materials, key=lambda item: item["id"])


def build_artifacts(
    root: Path,
    output: Path,
    *,
    capabilities: dict[str, Path] | None = None,
    revision: str | None = None,
) -> Path:
    root = root.resolve()
    install = load_install_manifest(root)
    runtime = load_linux_runtime(root)
    revision, epoch = _source_identity(root, revision)
    output = _new_output_directory(output, "package build")
    environment = _base_environment(root, output, epoch)
    environment["HFX_SOURCE_REVISION"] = revision
    capabilities = capabilities or {}
    toolchain = verify_current_toolchain(root)
    inputs = _provenance_inputs(root, install, runtime)

    artifacts: list[BuiltArtifact] = []
    cargo = [build for build in install.builds if build.kind == "cargo-binary"]
    artifacts.extend(_build_cargo(root, output, environment, cargo))
    openrazer_builds = [
        build
        for build in install.builds
        if build.kind in {"source-transform", "source-transform-file"}
    ]
    prepared_openrazer = (
        _prepare_openrazer_source_transform(
            root, output, openrazer_builds, capabilities
        )
        if openrazer_builds
        else None
    )
    omitted: list[tuple[str, str]] = []
    for build in install.builds:
        if build.kind == "cmake-module":
            artifact = _build_cmake(
                root, output, environment, build, capabilities, revision
            )
            if artifact is None:
                omitted.append((build.id, f"optional capability {build.capability} was not supplied"))
            else:
                artifacts.append(artifact)
        elif build.kind == "source-transform":
            artifacts.append(
                _openrazer_dkms_projection(
                    root, output, environment, build, prepared_openrazer
                )
            )
        elif build.kind == "source-transform-file":
            artifacts.append(
                _openrazer_source_file_projection(
                    output, build, prepared_openrazer
                )
            )

    manifest = {
        "$schema": "https://hyperflux.dev/schemas/package-build-manifest-v2.json",
        "schema": "hyperflux-package-build-manifest-v2",
        "source": {"revision": revision, "source_date_epoch": epoch},
        "builder": {
            "id": "https://hyperflux.dev/builders/hfx-package-v2",
            "network_access": False,
        },
        "toolchain": toolchain,
        "inputs": inputs,
        "materials": _provenance_materials(root, inputs, set(capabilities)),
        "artifacts": [
            _artifact_json(artifact, output)
            for artifact in sorted(artifacts, key=lambda item: item.build_id)
        ],
        "omitted": [
            {"build_id": build_id, "reason": reason}
            for build_id, reason in sorted(omitted)
        ],
    }
    manifest_path = output / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    os.utime(manifest_path, (epoch, epoch))
    return manifest_path
