# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import configparser
from dataclasses import dataclass
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
from typing import Any
import zipfile

from .install import BuildSpec, InstallManifest, load_install_manifest
from .integrations import load_integration_catalog
from .linux_runtime import LinuxRuntime, load_linux_runtime
from .model import ModelError, load_json, require_unique, sha256_file


REVISION = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_NAME = "package-build-manifest.json"
INVENTORY_PATH = PurePosixPath("/usr/share/hyperflux-next/installed-files.json")


@dataclass(frozen=True)
class BuiltArtifact:
    build_id: str
    kind: str
    path: Path
    sha256: str
    size: int
    mode: int
    destination: str | None
    distribution: str | None


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
    payload_sha256: str
    file_count: int


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
            "CARGO_TARGET_DIR": str(output / "work" / "cargo-target"),
            "RUSTFLAGS": rust_remap,
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


def _copy_artifact(
    source: Path,
    artifacts_root: Path,
    build: BuildSpec,
    *,
    kind: str,
    destination: str | None = None,
    distribution: str | None = None,
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
        kind=kind,
        path=target,
        sha256=sha256_file(target),
        size=target.stat().st_size,
        mode=mode,
        destination=destination,
        distribution=distribution,
    )


def _build_cargo(
    root: Path,
    output: Path,
    environment: dict[str, str],
    builds: list[BuildSpec],
) -> list[BuiltArtifact]:
    by_package: dict[str, list[BuildSpec]] = {}
    for build in builds:
        if build.package is None or build.target is None:
            raise ModelError(f"build {build.id}: incomplete Cargo declaration")
        by_package.setdefault(build.package, []).append(build)
    for package, package_builds in sorted(by_package.items()):
        command = ["cargo", "build", "--release", "--locked", "--package", package]
        for build in package_builds:
            command.extend(["--bin", build.target or ""])
        _run(
            command,
            cwd=root,
            environment=environment,
            label=f"Cargo package {package}",
        )
    release = Path(environment["CARGO_TARGET_DIR"]) / "release"
    return [
        _copy_artifact(
            release / (build.target or ""),
            output,
            build,
            kind="cargo-binary",
            destination=build.destination,
            mode=build.mode or 0o755,
        )
        for build in builds
    ]


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


def _build_python(
    root: Path,
    output: Path,
    environment: dict[str, str],
    build: BuildSpec,
) -> BuiltArtifact:
    source = root / (build.source or "")
    projection = output / "work" / "python-sources" / build.id
    if (root / ".git").is_dir():
        tracked = _output(
            ["git", "ls-files", "--", build.source or ""],
            cwd=root,
            label=f"Python source inventory {build.id}",
        ).splitlines()
        if not tracked:
            raise ModelError(f"build {build.id}: Python source has no tracked files")
        projection.mkdir(parents=True)
        for value in tracked:
            repository_path = root / value
            try:
                relative = repository_path.relative_to(source)
            except ValueError as error:
                raise ModelError(
                    f"build {build.id}: tracked Python source escaped its project"
                ) from error
            if not repository_path.is_file() or repository_path.is_symlink():
                raise ModelError(
                    f"build {build.id}: tracked Python source is not a regular file"
                )
            target = projection / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repository_path, target, follow_symlinks=False)
    else:
        shutil.copytree(
            source,
            projection,
            symlinks=False,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "*.pyo", "build", "dist", "*.egg-info"
            ),
        )
    wheel_directory = output / "work" / "wheels" / build.id
    wheel_directory.mkdir(parents=True, exist_ok=True)
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-build-isolation",
            "--no-deps",
            "--no-cache-dir",
            "--wheel-dir",
            str(wheel_directory),
            str(projection),
        ],
        cwd=root,
        environment=environment,
        label=f"Python wheel {build.id}",
    )
    wheels = sorted(wheel_directory.glob("*.whl"))
    if len(wheels) != 1:
        raise ModelError(f"build {build.id}: expected exactly one wheel")
    return _copy_artifact(
        wheels[0],
        output,
        build,
        kind="python-wheel",
        distribution=build.distribution,
        mode=0o644,
    )


def _artifact_json(artifact: BuiltArtifact, root: Path) -> dict[str, Any]:
    value: dict[str, Any] = {
        "build_id": artifact.build_id,
        "kind": artifact.kind,
        "path": artifact.path.relative_to(root).as_posix(),
        "sha256": artifact.sha256,
        "size": artifact.size,
        "mode": f"{artifact.mode:04o}",
    }
    if artifact.destination is not None:
        value["destination"] = artifact.destination
    if artifact.distribution is not None:
        value["distribution"] = artifact.distribution
    return value


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
    capabilities = capabilities or {}

    artifacts: list[BuiltArtifact] = []
    cargo = [build for build in install.builds if build.kind == "cargo-binary"]
    artifacts.extend(_build_cargo(root, output, environment, cargo))
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
        elif build.kind == "python-project":
            artifacts.append(_build_python(root, output, environment, build))

    manifest = {
        "$schema": "https://hyperflux.dev/schemas/package-build-manifest-v1.json",
        "schema": "hyperflux-package-build-manifest-v1",
        "source": {"revision": revision, "source_date_epoch": epoch},
        "inputs": {
            "install_manifest_sha256": install.source_sha256,
            "linux_runtime_sha256": runtime.source_sha256,
            "python": platform.python_version(),
            "target": _output(["rustc", "-vV"], cwd=root, label="Rust target")
            .split("host: ", 1)[-1]
            .splitlines()[0],
        },
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


def _manifest_artifact(root: Path, value: Any, label: str) -> BuiltArtifact:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: must be an object")
    required = {"build_id", "kind", "path", "sha256", "size", "mode"}
    optional = {"destination", "distribution"}
    if not required <= value.keys() or value.keys() - required - optional:
        raise ModelError(f"{label}: fields do not match the artifact contract")
    build_id = value["build_id"]
    kind = value["kind"]
    if not isinstance(build_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", build_id):
        raise ModelError(f"{label}: invalid build id")
    if kind not in {"cargo-binary", "cmake-module", "python-wheel"}:
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
    distribution = value.get("distribution")
    if (destination is None) == (distribution is None):
        raise ModelError(f"{label}: artifact must have one install target")
    if destination is not None and (
        not isinstance(destination, str)
        or not destination.startswith(("/etc/", "/usr/"))
        or ".." in PurePosixPath(destination).parts
    ):
        raise ModelError(f"{label}: invalid artifact destination")
    if distribution is not None and (
        not isinstance(distribution, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,95}", distribution)
    ):
        raise ModelError(f"{label}: invalid Python distribution")
    return BuiltArtifact(
        build_id=build_id,
        kind=kind,
        path=path,
        sha256=digest,
        size=size,
        mode=int(mode_value, 8),
        destination=destination,
        distribution=distribution,
    )


def load_artifact_set(root: Path, manifest_path: Path) -> ArtifactSet:
    root = root.resolve()
    manifest_path = manifest_path.resolve()
    value = load_json(manifest_path)
    expected_keys = {"$schema", "schema", "source", "inputs", "artifacts", "omitted"}
    if (
        set(value) != expected_keys
        or value["$schema"]
        != "https://hyperflux.dev/schemas/package-build-manifest-v1.json"
        or value["schema"] != "hyperflux-package-build-manifest-v1"
    ):
        raise ModelError("unsupported package build manifest")
    source = value["source"]
    inputs = value["inputs"]
    if not isinstance(source, dict) or set(source) != {"revision", "source_date_epoch"}:
        raise ModelError("package build source identity is malformed")
    if not isinstance(inputs, dict) or set(inputs) != {
        "install_manifest_sha256",
        "linux_runtime_sha256",
        "python",
        "target",
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
    if inputs["install_manifest_sha256"] != install.source_sha256:
        raise ModelError("package build manifest uses a stale installation contract")
    if inputs["linux_runtime_sha256"] != runtime.source_sha256:
        raise ModelError("package build manifest uses a stale Linux runtime contract")
    if not isinstance(inputs["python"], str) or not inputs["python"]:
        raise ModelError("package Python identity is malformed")
    if not isinstance(inputs["target"], str) or not inputs["target"]:
        raise ModelError("package target identity is malformed")

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
        expected_kind = "python-wheel" if build.kind == "python-project" else build.kind
        if artifact.kind != expected_kind:
            raise ModelError(f"artifact {artifact.build_id}: kind does not match install contract")
        if artifact.destination != build.destination or artifact.distribution != build.distribution:
            raise ModelError(f"artifact {artifact.build_id}: install target drifted")
    for build_id, _ in omitted:
        build = declared.get(build_id)
        if build is None or build.required:
            raise ModelError(f"omission {build_id}: required or unknown build cannot be omitted")
    return ArtifactSet(
        root=manifest_path.parent,
        revision=revision,
        source_date_epoch=epoch,
        install_manifest_sha256=inputs["install_manifest_sha256"],
        linux_runtime_sha256=inputs["linux_runtime_sha256"],
        python=inputs["python"],
        target=inputs["target"],
        artifacts=artifacts,
        omitted=tuple(omitted),
    )


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


def _wheel_member_path(name: str, label: str) -> PurePosixPath | None:
    if not name or "\\" in name or any(ord(character) < 32 for character in name):
        raise ModelError(f"{label}: wheel member path is invalid")
    directory = name.endswith("/")
    normalized = name[:-1] if directory else name
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or ".." in path.parts
        or str(path) != normalized
    ):
        raise ModelError(f"{label}: wheel member path is not normalized")
    if directory:
        return None
    if path.parts[0].endswith(".data"):
        if len(path.parts) < 3 or path.parts[1] not in {"purelib", "platlib"}:
            raise ModelError(f"{label}: wheel data target is not portable")
        path = PurePosixPath(*path.parts[2:])
    return path


def _console_scripts(payload: bytes, label: str) -> tuple[tuple[str, str, str], ...]:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(payload.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as error:
        raise ModelError(f"{label}: invalid wheel entry points") from error
    scripts: list[tuple[str, str, str]] = []
    if not parser.has_section("console_scripts"):
        return ()
    for command, target in parser.items("console_scripts"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", command):
            raise ModelError(f"{label}: invalid console command")
        match = re.fullmatch(
            r"([A-Za-z_][A-Za-z0-9_.]*):([A-Za-z_][A-Za-z0-9_]*)", target.strip()
        )
        if match is None:
            raise ModelError(f"{label}: unsupported console entry point")
        scripts.append((command, match.group(1), match.group(2)))
    require_unique([command for command, _, _ in scripts], f"{label} console command")
    return tuple(sorted(scripts))


def _launcher(module_directory: str, module: str, function: str) -> bytes:
    return (
        "#!/usr/bin/python3\n"
        "# Generated from a source-bound HyperFlux Next wheel.\n"
        "import sys\n"
        f"sys.path.insert(0, {module_directory!r})\n"
        f"from {module} import {function}\n"
        "if __name__ == '__main__':\n"
        f"    raise SystemExit({function}())\n"
    ).encode("ascii")


def _install_wheels(
    stage_root: Path,
    wheels: list[BuiltArtifact],
    module_directory: str,
) -> None:
    if not wheels:
        return
    require_unique(
        [wheel.distribution or "" for wheel in wheels],
        "Python wheel distribution",
    )
    require_unique([wheel.path.name for wheel in wheels], "Python wheel filename")
    module_root = _stage_path(stage_root, module_directory)
    entry_points: list[tuple[str, str, str]] = []
    for wheel in sorted(wheels, key=lambda item: item.build_id):
        label = f"Python wheel {wheel.build_id}"
        try:
            archive = zipfile.ZipFile(wheel.path)
        except (OSError, zipfile.BadZipFile) as error:
            raise ModelError(f"{label}: invalid wheel archive") from error
        with archive:
            members = archive.infolist()
            if not 1 <= len(members) <= 10_000:
                raise ModelError(f"{label}: wheel member count is outside bounds")
            if sum(member.file_size for member in members) > 64 * 1024 * 1024:
                raise ModelError(f"{label}: expanded wheel exceeds its size bound")
            if archive.testzip() is not None:
                raise ModelError(f"{label}: wheel checksum verification failed")
            files: dict[PurePosixPath, zipfile.ZipInfo] = {}
            for member in members:
                mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ModelError(f"{label}: symbolic links are forbidden")
                path = _wheel_member_path(member.filename, label)
                if path is None:
                    continue
                if path in files:
                    raise ModelError(f"{label}: duplicate wheel member")
                files[path] = member
            metadata_paths = [
                path for path in files if len(path.parts) == 2 and path.parts[0].endswith(".dist-info") and path.name == "METADATA"
            ]
            wheel_paths = [
                path for path in files if len(path.parts) == 2 and path.parts[0].endswith(".dist-info") and path.name == "WHEEL"
            ]
            if len(metadata_paths) != 1 or len(wheel_paths) != 1:
                raise ModelError(f"{label}: wheel metadata is incomplete")
            metadata = Parser().parsestr(archive.read(files[metadata_paths[0]]).decode("utf-8"))
            expected_name = (wheel.distribution or "").replace("-", "_").lower()
            actual_name = metadata.get("Name", "").replace("-", "_").lower()
            if actual_name != expected_name:
                raise ModelError(f"{label}: distribution identity mismatch")
            wheel_metadata = archive.read(files[wheel_paths[0]]).decode("utf-8")
            if "Root-Is-Purelib: true" not in wheel_metadata:
                raise ModelError(f"{label}: native Python wheels are not portable")
            for path, member in sorted(files.items(), key=lambda item: str(item[0])):
                _write_to_stage(module_root.joinpath(*path.parts), archive.read(member), 0o644)
                if path.name == "entry_points.txt" and path.parts[0].endswith(".dist-info"):
                    entry_points.extend(_console_scripts(archive.read(member), label))
    require_unique([command for command, _, _ in entry_points], "Python console command")
    for command, module, function in sorted(entry_points):
        _write_to_stage(
            _stage_path(stage_root, f"/usr/bin/{command}"),
            _launcher(module_directory, module, function),
            0o755,
        )


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


def _normalize_tree(stage_root: Path, epoch: int) -> None:
    for path in sorted(stage_root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o755)
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    stage_root.chmod(0o755)
    os.utime(stage_root, (epoch, epoch))


def stage_rootfs(root: Path, manifest_path: Path, stage_root: Path) -> StageResult:
    root = root.resolve()
    install = load_install_manifest(root)
    artifacts = load_artifact_set(root, manifest_path)
    stage_root = _new_output_directory(stage_root, "package staging")

    for file in install.files:
        _copy_to_stage(
            file.source,
            _stage_path(stage_root, file.destination),
            file.mode,
        )
    wheels: list[BuiltArtifact] = []
    for artifact in artifacts.artifacts:
        if artifact.kind == "python-wheel":
            wheels.append(artifact)
            continue
        if artifact.destination is None:
            raise ModelError(f"artifact {artifact.build_id}: missing destination")
        _copy_to_stage(
            artifact.path,
            _stage_path(stage_root, artifact.destination),
            artifact.mode,
        )

    runtime = load_linux_runtime(root)
    _install_wheels(
        stage_root,
        wheels,
        runtime.operations.python_module_directory,
    )
    _normalize_tree(stage_root, artifacts.source_date_epoch)
    private = (str(root).encode(), str(artifacts.root).encode())
    staged_files = _inspect_staged_files(stage_root, private)
    payload_digest = _tree_digest(staged_files, stage_root)

    preserve = {
        str(file.destination): file.preserve for file in install.files if file.preserve
    }
    inventory_value = {
        "schema": "hyperflux-installed-files-v1",
        "source_revision": artifacts.revision,
        "source_date_epoch": artifacts.source_date_epoch,
        "install_manifest_sha256": install.source_sha256,
        "linux_runtime_sha256": artifacts.linux_runtime_sha256,
        "payload_sha256": payload_digest,
        "omitted_optional_builds": [
            {"build_id": build_id, "reason": reason}
            for build_id, reason in artifacts.omitted
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
    inventory = _stage_path(stage_root, INVENTORY_PATH)
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(
        json.dumps(inventory_value, indent=2, sort_keys=False, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    inventory.chmod(0o644)
    os.utime(inventory, (artifacts.source_date_epoch, artifacts.source_date_epoch))
    _normalize_tree(stage_root, artifacts.source_date_epoch)
    return StageResult(
        root=stage_root,
        inventory=inventory,
        payload_sha256=payload_digest,
        file_count=len(staged_files) + 1,
    )
