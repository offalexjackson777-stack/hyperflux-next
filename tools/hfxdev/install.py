# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable

from .linux_runtime import LinuxRuntime, load_linux_runtime
from .model import ModelError, load_json, require_unique, sha256_file


ROOT_KEYS = {"$schema", "schema", "policy", "builds", "payloads"}
POLICY_KEYS = {
    "hardware_writes_on_install",
    "start_service_on_install",
    "enable_service_on_install",
    "preserve_configuration",
}
CARGO_KEYS = {"id", "kind", "package", "target", "destination", "mode", "required"}
CMAKE_KEYS = {
    "id",
    "kind",
    "source",
    "target",
    "output",
    "destination",
    "mode",
    "required",
    "capability",
}
PYTHON_KEYS = {"id", "kind", "source", "distribution", "required"}
PAYLOAD_KEYS = {"id", "kind", "source", "destination", "mode", "preserve", "include"}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DISTRIBUTION = re.compile(r"^[a-z][a-z0-9-]{0,95}$")
MODES = {"0644": 0o644, "0755": 0o755}
ALLOWED_INSTALL_ROOTS = {"etc", "usr"}


@dataclass(frozen=True)
class InstallPolicy:
    hardware_writes_on_install: bool
    start_service_on_install: bool
    enable_service_on_install: bool
    preserve_configuration: bool


@dataclass(frozen=True)
class BuildSpec:
    id: str
    kind: str
    required: bool
    source: str | None = None
    package: str | None = None
    target: str | None = None
    output: str | None = None
    destination: str | None = None
    mode: int | None = None
    distribution: str | None = None
    capability: str | None = None


@dataclass(frozen=True)
class PayloadSpec:
    id: str
    kind: str
    source: str
    destination: str
    mode: int
    preserve: bool
    include: tuple[str, ...]


@dataclass(frozen=True)
class PayloadFile:
    component: str
    source: Path
    destination: PurePosixPath
    mode: int
    preserve: bool


@dataclass(frozen=True)
class InstallManifest:
    root: Path
    source_sha256: str
    policy: InstallPolicy
    builds: tuple[BuildSpec, ...]
    payloads: tuple[PayloadSpec, ...]
    files: tuple[PayloadFile, ...]

    def build(self, build_id: str) -> BuildSpec:
        for build in self.builds:
            if build.id == build_id:
                return build
        raise KeyError(build_id)


def _exact(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing:
        raise ModelError(f"{label}: missing fields {', '.join(missing)}")
    if extra:
        raise ModelError(f"{label}: unknown fields {', '.join(extra)}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: must be an object")
    return value


def _list(value: Any, label: str, *, minimum: int = 0, maximum: int = 128) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ModelError(f"{label}: must contain from {minimum} through {maximum} items")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ModelError(f"{label}: invalid identifier")
    return value


def _relative_path(root: Path, value: Any, label: str, *, directory: bool) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ModelError(f"{label}: must be a repository-relative path")
    relative = PurePosixPath(value)
    if ".." in relative.parts or str(relative) != value or len(value.encode()) > 256:
        raise ModelError(f"{label}: path is not normalized")
    path = root / relative
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ModelError(f"{label}: symbolic-link source paths are forbidden")
    expected = path.is_dir() if directory else path.is_file()
    if not expected or path.is_symlink():
        kind = "directory" if directory else "file"
        raise ModelError(f"{label}: missing regular {kind} {value}")
    return value


def _destination(value: Any, label: str, tokens: dict[str, str]) -> str:
    if not isinstance(value, str):
        raise ModelError(f"{label}: destination must be a string")
    expanded = tokens.get(value, value)
    path = PurePosixPath(expanded)
    if (
        not expanded.startswith("/")
        or str(path) != expanded
        or ".." in path.parts
        or len(path.parts) < 2
        or path.parts[1] not in ALLOWED_INSTALL_ROOTS
        or len(expanded.encode()) > 256
    ):
        raise ModelError(f"{label}: destination must be normalized below /etc or /usr")
    if "@" in expanded or any(ord(character) < 32 or ord(character) > 126 for character in expanded):
        raise ModelError(f"{label}: destination contains an unknown token or character")
    return expanded


def _mode(value: Any, label: str) -> int:
    if value not in MODES:
        raise ModelError(f"{label}: mode must be 0644 or 0755")
    return MODES[value]


def _tokens(runtime: LinuxRuntime) -> dict[str, str]:
    return {
        "@bridge_executable_path@": runtime.bridge.executable_path,
        "@operations_cli_path@": runtime.operations.cli_path,
        "@activation_utility_path@": runtime.operations.activation_path,
        "@kernel_source_directory@": runtime.kernel.source_directory,
        "@kernel_dkms_configuration_path@": str(
            PurePosixPath(runtime.kernel.source_directory) / "dkms.conf"
        ),
        "@bridge_configuration_file_path@": runtime.bridge.configuration_file_path,
    }


def _cargo_build(value: dict[str, Any], tokens: dict[str, str], label: str) -> BuildSpec:
    _exact(value, CARGO_KEYS, label)
    if value["required"] is not True:
        raise ModelError(f"{label}: Cargo product must be required")
    return BuildSpec(
        id=_identifier(value["id"], f"{label} id"),
        kind="cargo-binary",
        required=True,
        package=_identifier(value["package"], f"{label} package"),
        target=_identifier(value["target"], f"{label} target"),
        destination=_destination(value["destination"], f"{label} destination", tokens),
        mode=_mode(value["mode"], f"{label} mode"),
    )


def _cmake_build(
    root: Path, value: dict[str, Any], tokens: dict[str, str], label: str
) -> BuildSpec:
    _exact(value, CMAKE_KEYS, label)
    output = value["output"]
    if not isinstance(output, str) or not output.endswith(".so") or "/" in output:
        raise ModelError(f"{label}: output must be one shared-object file name")
    required = value["required"]
    if not isinstance(required, bool):
        raise ModelError(f"{label}: required must be boolean")
    return BuildSpec(
        id=_identifier(value["id"], f"{label} id"),
        kind="cmake-module",
        required=required,
        source=_relative_path(root, value["source"], f"{label} source", directory=True),
        target=_identifier(value["target"], f"{label} target"),
        output=output,
        destination=_destination(value["destination"], f"{label} destination", tokens),
        mode=_mode(value["mode"], f"{label} mode"),
        capability=_identifier(value["capability"], f"{label} capability"),
    )


def _python_build(root: Path, value: dict[str, Any], label: str) -> BuildSpec:
    _exact(value, PYTHON_KEYS, label)
    required = value["required"]
    if not isinstance(required, bool):
        raise ModelError(f"{label}: required must be boolean")
    source = _relative_path(root, value["source"], f"{label} source", directory=True)
    if not (root / source / "pyproject.toml").is_file():
        raise ModelError(f"{label}: Python project is missing pyproject.toml")
    distribution = value["distribution"]
    if not isinstance(distribution, str) or not DISTRIBUTION.fullmatch(distribution):
        raise ModelError(f"{label}: invalid Python distribution name")
    return BuildSpec(
        id=_identifier(value["id"], f"{label} id"),
        kind="python-project",
        required=required,
        source=source,
        distribution=distribution,
    )


def _load_builds(
    root: Path, values: list[Any], tokens: dict[str, str]
) -> tuple[BuildSpec, ...]:
    builds: list[BuildSpec] = []
    for index, item in enumerate(values):
        label = f"install build {index}"
        value = _object(item, label)
        kind = value.get("kind")
        if kind == "cargo-binary":
            builds.append(_cargo_build(value, tokens, label))
        elif kind == "cmake-module":
            builds.append(_cmake_build(root, value, tokens, label))
        elif kind == "python-project":
            builds.append(_python_build(root, value, label))
        else:
            raise ModelError(f"{label}: unsupported build kind")
    require_unique([build.id for build in builds], "install build id")
    destinations = [build.destination for build in builds if build.destination is not None]
    require_unique(destinations, "install build destination")
    distributions = [
        build.distribution for build in builds if build.distribution is not None
    ]
    require_unique(distributions, "install Python distribution")
    return tuple(builds)


def _load_payloads(
    root: Path, values: list[Any], tokens: dict[str, str]
) -> tuple[PayloadSpec, ...]:
    payloads: list[PayloadSpec] = []
    for index, item in enumerate(values):
        label = f"install payload {index}"
        value = _object(item, label)
        _exact(value, PAYLOAD_KEYS, label)
        kind = value["kind"]
        if kind not in {"file", "tree"}:
            raise ModelError(f"{label}: unsupported payload kind")
        include_value = _list(value["include"], f"{label} include", maximum=64)
        if kind == "file" and include_value:
            raise ModelError(f"{label}: file payload cannot define include patterns")
        if kind == "tree" and not include_value:
            raise ModelError(f"{label}: tree payload requires include patterns")
        include: list[str] = []
        for pattern in include_value:
            if (
                not isinstance(pattern, str)
                or not pattern
                or pattern.startswith("/")
                or ".." in PurePosixPath(pattern).parts
                or len(pattern.encode()) > 128
            ):
                raise ModelError(f"{label}: invalid include pattern")
            include.append(pattern)
        require_unique(include, f"{label} include pattern")
        preserve = value["preserve"]
        if not isinstance(preserve, bool):
            raise ModelError(f"{label}: preserve must be boolean")
        payloads.append(
            PayloadSpec(
                id=_identifier(value["id"], f"{label} id"),
                kind=kind,
                source=_relative_path(
                    root, value["source"], f"{label} source", directory=kind == "tree"
                ),
                destination=_destination(
                    value["destination"], f"{label} destination", tokens
                ),
                mode=_mode(value["mode"], f"{label} mode"),
                preserve=preserve,
                include=tuple(include),
            )
        )
    require_unique([payload.id for payload in payloads], "install payload id")
    return tuple(payloads)


def _expand_payload_files(
    root: Path,
    payloads: tuple[PayloadSpec, ...],
    projected_files: Iterable[Path] = (),
) -> tuple[PayloadFile, ...]:
    projected: tuple[Path, ...] = tuple(path.resolve() for path in projected_files)
    for path in projected:
        if path == root.resolve() or root.resolve() not in path.parents:
            raise ModelError(f"projected install file escapes the repository: {path}")
    files: list[PayloadFile] = []
    for payload in payloads:
        source = root / payload.source
        destination = PurePosixPath(payload.destination)
        if payload.kind == "file":
            files.append(
                PayloadFile(
                    component=payload.id,
                    source=source,
                    destination=destination,
                    mode=payload.mode,
                    preserve=payload.preserve,
                )
            )
            continue
        matches: dict[str, Path] = {}
        for pattern in payload.include:
            for path in source.glob(pattern):
                if not path.is_file() or path.is_symlink():
                    continue
                cursor = source
                for part in path.relative_to(source).parts:
                    cursor /= part
                    if cursor.is_symlink():
                        raise ModelError(
                            f"install payload {payload.id}: symbolic-link files are forbidden"
                        )
                relative = path.relative_to(source).as_posix()
                matches[relative] = path
            for path in projected:
                try:
                    relative_path = path.relative_to(source.resolve())
                except ValueError:
                    continue
                if relative_path.match(pattern):
                    matches[relative_path.as_posix()] = path
        if not matches:
            raise ModelError(f"install payload {payload.id}: include patterns matched no files")
        for relative, path in sorted(matches.items()):
            files.append(
                PayloadFile(
                    component=payload.id,
                    source=path,
                    destination=destination / PurePosixPath(relative),
                    mode=payload.mode,
                    preserve=payload.preserve,
                )
            )
    require_unique([str(file.destination) for file in files], "install payload destination")
    return tuple(files)


def _check_cross_contract(
    runtime: LinuxRuntime,
    policy: InstallPolicy,
    builds: tuple[BuildSpec, ...],
    payloads: tuple[PayloadSpec, ...],
    files: tuple[PayloadFile, ...],
) -> None:
    if any(
        (
            policy.hardware_writes_on_install,
            policy.start_service_on_install,
            policy.enable_service_on_install,
        )
    ) or not policy.preserve_configuration:
        raise ModelError("install policy must remain non-writing, non-activating, and preserving")

    build_by_id = {build.id: build for build in builds}
    required_builds = {
        "bridge-daemon": runtime.bridge.executable_path,
        "operations-cli": runtime.operations.cli_path,
        "activation-utility": runtime.operations.activation_path,
    }
    for build_id, destination in required_builds.items():
        build = build_by_id.get(build_id)
        if build is None or build.destination != destination or not build.required:
            raise ModelError(f"install build {build_id}: runtime destination is not authoritative")
    if "python-sdk" not in build_by_id or not build_by_id["python-sdk"].required:
        raise ModelError("Python SDK must be a required install build")

    payload_by_id = {payload.id: payload for payload in payloads}
    configuration = payload_by_id.get("default-configuration")
    if (
        configuration is None
        or configuration.destination != runtime.bridge.configuration_file_path
        or not configuration.preserve
    ):
        raise ModelError("default bridge configuration must be preserved at the runtime path")
    if any(payload.preserve and payload.id != "default-configuration" for payload in payloads):
        raise ModelError("only the default bridge configuration may be preserved")

    build_destinations = {build.destination for build in builds if build.destination is not None}
    payload_destinations = {str(file.destination) for file in files}
    overlap = sorted(build_destinations & payload_destinations)
    if overlap:
        raise ModelError(f"build and payload destinations overlap: {', '.join(overlap)}")

    kernel_prefix = PurePosixPath(runtime.kernel.source_directory)
    kernel_files = [file for file in files if file.component == "kernel-source"]
    if not kernel_files or any(kernel_prefix not in file.destination.parents for file in kernel_files):
        raise ModelError("kernel source payload is not rooted in the DKMS source directory")
    dkms_path = kernel_prefix / "dkms.conf"
    if not any(file.destination == dkms_path for file in files):
        raise ModelError("DKMS configuration is missing from the kernel source directory")


def load_install_manifest(
    root: Path,
    *,
    projected_files: Iterable[Path] = (),
) -> InstallManifest:
    path = root / "packaging" / "install.json"
    value = load_json(path)
    _exact(value, ROOT_KEYS, "install manifest")
    if (
        value["$schema"] != "../schemas/install-manifest.schema.json"
        or value["schema"] != "hyperflux-install-manifest-v1"
    ):
        raise ModelError("unsupported install manifest schema")

    policy_value = _object(value["policy"], "install policy")
    _exact(policy_value, POLICY_KEYS, "install policy")
    if not all(isinstance(policy_value[key], bool) for key in POLICY_KEYS):
        raise ModelError("install policy fields must be boolean")
    policy = InstallPolicy(**policy_value)

    runtime = load_linux_runtime(root)
    tokens = _tokens(runtime)
    builds = _load_builds(
        root,
        _list(value["builds"], "install builds", minimum=1, maximum=32),
        tokens,
    )
    payloads = _load_payloads(
        root,
        _list(value["payloads"], "install payloads", minimum=1, maximum=128),
        tokens,
    )
    files = _expand_payload_files(root, payloads, projected_files)
    _check_cross_contract(runtime, policy, builds, payloads, files)
    return InstallManifest(
        root=root.resolve(),
        source_sha256=sha256_file(path),
        policy=policy,
        builds=builds,
        payloads=payloads,
        files=files,
    )
