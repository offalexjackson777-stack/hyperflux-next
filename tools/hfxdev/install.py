# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable

from .linux_runtime import LinuxRuntime, load_linux_runtime
from .model import ModelError, load_json, require_unique, sha256_file
from .openrazer_native_route import load_native_deployment


ROOT_KEYS = {"$schema", "schema", "policy", "first_run_activation", "builds", "payloads"}
FIRST_RUN_ACTIVATION = {
    "receiver_during_install": "connected",
    "unit_enable": "enable-without-start",
    "activation": "normal-reboot",
    "disconnected_activation_command": "activate-disconnected-driver",
    "disconnected_activation_precondition": "verified-alternate-input",
}
POLICY_KEYS = {
    "hardware_writes_on_install",
    "start_service_on_install",
    "enable_service_on_install",
    "preserve_configuration",
}
CARGO_KEYS = {
    "id",
    "distribution_component",
    "kind",
    "package",
    "target",
    "profile",
    "destination",
    "mode",
    "required",
}
CARGO_PROFILES = {"operations", "release", "relay"}
CMAKE_KEYS = {
    "id",
    "distribution_component",
    "kind",
    "source",
    "target",
    "output",
    "destination",
    "mode",
    "required",
    "capability",
}
SOURCE_TRANSFORM_KEYS = {
    "id",
    "distribution_component",
    "kind",
    "transform",
    "destination",
    "mode",
    "required",
    "capability",
}
SOURCE_TRANSFORM_FILE_KEYS = SOURCE_TRANSFORM_KEYS | {"source"}
PAYLOAD_KEYS = {
    "id",
    "distribution_component",
    "kind",
    "source",
    "destination",
    "mode",
    "preserve",
    "include",
}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
MODES = {"0644": 0o644, "0755": 0o755}
ALLOWED_INSTALL_ROOTS = {"etc", "usr"}
DISTRIBUTION_COMPONENTS = ("linux", "full")


@dataclass(frozen=True)
class InstallPolicy:
    hardware_writes_on_install: bool
    start_service_on_install: bool
    enable_service_on_install: bool
    preserve_configuration: bool


@dataclass(frozen=True)
class FirstRunActivation:
    """The one supported first-run path after a package transaction.

    Packages install units but never enable or start them. The user enables the
    bridge and one desktop socket without starting them, then reboots with the
    receiver connected. Live activation is a separate path that needs verified
    alternate input because it requires the receiver to be disconnected.
    """

    receiver_during_install: str
    unit_enable: str
    activation: str
    disconnected_activation_command: str
    disconnected_activation_precondition: str

    def enable_command(self, runtime: LinuxRuntime) -> str:
        socket = runtime.desktop_access.socket_unit.replace("@.", "@$(id -u).")
        return f'sudo systemctl enable {runtime.bridge.service_unit} "{socket}"'

    def disable_command(self, runtime: LinuxRuntime) -> str:
        """Turn the units off before removal; packages never stop them."""

        socket = runtime.desktop_access.socket_unit.replace("@.", "@$(id -u).")
        return f'sudo systemctl disable --now {runtime.bridge.service_unit} "{socket}"'

    def activation_command(self) -> str:
        return "sudo reboot"

    def disconnected_command(self, runtime: LinuxRuntime) -> str:
        return (
            f"sudo {runtime.operations.activation_path} "
            f"{self.disconnected_activation_command}"
        )


def _first_run_activation(value: Any) -> FirstRunActivation:
    activation = _object(value, "first-run activation")
    _exact(activation, set(FIRST_RUN_ACTIVATION), "first-run activation")
    if activation != FIRST_RUN_ACTIVATION:
        raise ModelError(
            "first-run activation must enable units without starting them and activate "
            "through a normal reboot, with live activation only after alternate input"
        )
    return FirstRunActivation(**activation)


def load_first_run_activation(root: Path) -> FirstRunActivation:
    value = _object(load_json(root / "packaging" / "install.json"), "install manifest")
    return _first_run_activation(value.get("first_run_activation"))


@dataclass(frozen=True)
class BuildSpec:
    id: str
    distribution_component: str
    kind: str
    required: bool
    source: str | None = None
    package: str | None = None
    target: str | None = None
    cargo_profile: str | None = None
    output: str | None = None
    destination: str | None = None
    mode: int | None = None
    capability: str | None = None
    transform: str | None = None


@dataclass(frozen=True)
class PayloadSpec:
    id: str
    distribution_component: str
    kind: str
    source: str
    destination: str
    mode: int
    preserve: bool
    include: tuple[str, ...]


@dataclass(frozen=True)
class PayloadFile:
    component: str
    distribution_component: str
    source: Path
    destination: PurePosixPath
    mode: int
    preserve: bool


@dataclass(frozen=True)
class InstallManifest:
    root: Path
    source_sha256: str
    policy: InstallPolicy
    first_run_activation: FirstRunActivation
    builds: tuple[BuildSpec, ...]
    payloads: tuple[PayloadSpec, ...]
    files: tuple[PayloadFile, ...]

    def build(self, build_id: str) -> BuildSpec:
        for build in self.builds:
            if build.id == build_id:
                return build
        raise KeyError(build_id)

    def builds_for(self, distribution_component: str) -> tuple[BuildSpec, ...]:
        return tuple(
            build
            for build in self.builds
            if build.distribution_component == distribution_component
        )

    def files_for(self, distribution_component: str) -> tuple[PayloadFile, ...]:
        return tuple(
            file
            for file in self.files
            if file.distribution_component == distribution_component
        )


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


def _distribution_component(value: Any, label: str) -> str:
    if value not in DISTRIBUTION_COMPONENTS:
        raise ModelError(f"{label}: unsupported distribution component")
    return value


def _relative_path(
    root: Path,
    value: Any,
    label: str,
    *,
    directory: bool,
    projected_files: tuple[Path, ...] = (),
) -> str:
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
    projected = path.resolve() in projected_files
    expected = path.is_dir() if directory else path.is_file() or projected
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
    template_path = expanded.replace("@.", ".", 1)
    template_destination = (
        expanded.count("@.") == 1
        and "@" not in template_path
        and path.parts[:5] == ("/", "usr", "lib", "systemd", "system")
    )
    if ("@" in expanded and not template_destination) or any(
        ord(character) < 32 or ord(character) > 126 for character in expanded
    ):
        raise ModelError(f"{label}: destination contains an unknown token or character")
    return expanded


def _mode(value: Any, label: str) -> int:
    if value not in MODES:
        raise ModelError(f"{label}: mode must be 0644 or 0755")
    return MODES[value]


def _tokens(root: Path, runtime: LinuxRuntime) -> dict[str, str]:
    openrazer = load_native_deployment(root)
    return {
        "@bridge_executable_path@": runtime.bridge.executable_path,
        "@operations_cli_path@": runtime.operations.cli_path,
        "@activation_utility_path@": runtime.operations.activation_path,
        "@kernel_source_directory@": runtime.kernel.source_directory,
        "@kernel_dkms_configuration_path@": str(
            PurePosixPath(runtime.kernel.source_directory) / "dkms.conf"
        ),
        "@bridge_configuration_file_path@": runtime.bridge.configuration_file_path,
        "@openrazer_relay_executable_path@": openrazer.relay.executable_path,
        "@openrazer_dkms_source_directory@": openrazer.driver.source_directory,
        "@openrazer_udev_rules_path@": openrazer.access.udev_rules_path,
        "@openrazer_mount_helper_path@": openrazer.access.mount_helper_path,
        "@openrazer_sysusers_path@": openrazer.access.sysusers_path,
    }


def _cargo_build(value: dict[str, Any], tokens: dict[str, str], label: str) -> BuildSpec:
    _exact(value, CARGO_KEYS, label)
    if value["required"] is not True:
        raise ModelError(f"{label}: Cargo product must be required")
    if value["profile"] not in CARGO_PROFILES:
        raise ModelError(f"{label}: unsupported Cargo profile")
    return BuildSpec(
        id=_identifier(value["id"], f"{label} id"),
        distribution_component=_distribution_component(
            value["distribution_component"], f"{label} distribution component"
        ),
        kind="cargo-binary",
        required=True,
        package=_identifier(value["package"], f"{label} package"),
        target=_identifier(value["target"], f"{label} target"),
        cargo_profile=value["profile"],
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
        distribution_component=_distribution_component(
            value["distribution_component"], f"{label} distribution component"
        ),
        kind="cmake-module",
        required=required,
        source=_relative_path(root, value["source"], f"{label} source", directory=True),
        target=_identifier(value["target"], f"{label} target"),
        output=output,
        destination=_destination(value["destination"], f"{label} destination", tokens),
        mode=_mode(value["mode"], f"{label} mode"),
        capability=_identifier(value["capability"], f"{label} capability"),
    )


def _source_transform_build(
    value: dict[str, Any], tokens: dict[str, str], label: str
) -> BuildSpec:
    _exact(value, SOURCE_TRANSFORM_KEYS, label)
    if value["required"] is not True:
        raise ModelError(f"{label}: source transform must be required")
    return BuildSpec(
        id=_identifier(value["id"], f"{label} id"),
        distribution_component=_distribution_component(
            value["distribution_component"], f"{label} distribution component"
        ),
        kind="source-transform",
        required=True,
        transform=_identifier(value["transform"], f"{label} transform"),
        destination=_destination(value["destination"], f"{label} destination", tokens),
        mode=_mode(value["mode"], f"{label} mode"),
        capability=_identifier(value["capability"], f"{label} capability"),
    )


def _source_transform_file_build(
    value: dict[str, Any], tokens: dict[str, str], label: str
) -> BuildSpec:
    _exact(value, SOURCE_TRANSFORM_FILE_KEYS, label)
    if value["required"] is not True:
        raise ModelError(f"{label}: source transform file must be required")
    source = value["source"]
    if (
        not isinstance(source, str)
        or not source
        or source.startswith("/")
        or str(PurePosixPath(source)) != source
        or ".." in PurePosixPath(source).parts
        or len(source.encode()) > 256
    ):
        raise ModelError(f"{label}: source transform file path is invalid")
    return BuildSpec(
        id=_identifier(value["id"], f"{label} id"),
        distribution_component=_distribution_component(
            value["distribution_component"], f"{label} distribution component"
        ),
        kind="source-transform-file",
        required=True,
        source=source,
        transform=_identifier(value["transform"], f"{label} transform"),
        destination=_destination(value["destination"], f"{label} destination", tokens),
        mode=_mode(value["mode"], f"{label} mode"),
        capability=_identifier(value["capability"], f"{label} capability"),
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
        elif kind == "source-transform":
            builds.append(_source_transform_build(value, tokens, label))
        elif kind == "source-transform-file":
            builds.append(_source_transform_file_build(value, tokens, label))
        else:
            raise ModelError(f"{label}: unsupported build kind")
    require_unique([build.id for build in builds], "install build id")
    destinations = [build.destination for build in builds if build.destination is not None]
    require_unique(destinations, "install build destination")
    return tuple(builds)


def _load_payloads(
    root: Path,
    values: list[Any],
    tokens: dict[str, str],
    projected_files: tuple[Path, ...],
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
                distribution_component=_distribution_component(
                    value["distribution_component"],
                    f"{label} distribution component",
                ),
                kind=kind,
                source=_relative_path(
                    root,
                    value["source"],
                    f"{label} source",
                    directory=kind == "tree",
                    projected_files=projected_files,
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
                    distribution_component=payload.distribution_component,
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
                    distribution_component=payload.distribution_component,
                    source=path,
                    destination=destination / PurePosixPath(relative),
                    mode=payload.mode,
                    preserve=payload.preserve,
                )
            )
    require_unique([str(file.destination) for file in files], "install payload destination")
    return tuple(files)


def _check_cross_contract(
    root: Path,
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
        if (
            build is None
            or build.distribution_component != "linux"
            or build.destination != destination
            or not build.required
        ):
            raise ModelError(f"install build {build_id}: runtime destination is not authoritative")
    if "python-sdk" in build_by_id or "native-readonly-qualifier" in build_by_id:
        raise ModelError("development SDK and qualifier builds cannot enter runtime packages")
    openrgb = build_by_id.get("openrgb-native")
    if (
        openrgb is None
        or openrgb.distribution_component != "full"
        or openrgb.kind != "cmake-module"
        or openrgb.source != "integrations/openrgb"
        or openrgb.target != "hyperflux-next-openrgb"
        or openrgb.output != "hyperflux-next-openrgb.so"
        or openrgb.destination
        != "/usr/lib/openrgb/plugins/hyperflux-next-openrgb.so"
        or openrgb.mode != 0o755
        or openrgb.capability != "openrgb-source"
        or not openrgb.required
    ):
        raise ModelError("OpenRGB plugin must be the exact required full-package build")

    openrazer = load_native_deployment(root)
    relay = build_by_id.get("openrazer-native-relay")
    if (
        relay is None
        or relay.distribution_component != "full"
        or relay.kind != "cargo-binary"
        or relay.destination != openrazer.relay.executable_path
        or not relay.required
    ):
        raise ModelError("OpenRazer native relay build does not match its deployment authority")
    driver = build_by_id.get("openrazer-native-driver")
    if (
        driver is None
        or driver.distribution_component != "full"
        or driver.kind != "source-transform"
        or driver.transform != "openrazer-native-route"
        or driver.capability != "openrazer-source"
        or driver.destination != openrazer.driver.source_directory
        or not driver.required
    ):
        raise ModelError("OpenRazer native driver build does not match its deployment authority")
    rules = build_by_id.get("openrazer-native-udev-rules")
    if (
        rules is None
        or rules.distribution_component != "full"
        or rules.kind != "source-transform-file"
        or rules.transform != "openrazer-native-route"
        or rules.capability != "openrazer-source"
        or rules.source != openrazer.access.udev_rules_source
        or rules.destination != openrazer.access.udev_rules_path
        or rules.mode != 0o644
        or not rules.required
    ):
        raise ModelError("OpenRazer native udev rules build does not match its deployment authority")
    helper = build_by_id.get("openrazer-native-mount-helper")
    if (
        helper is None
        or helper.distribution_component != "full"
        or helper.kind != "source-transform-file"
        or helper.transform != "openrazer-native-route"
        or helper.capability != "openrazer-source"
        or helper.source != openrazer.access.mount_helper_source
        or helper.destination != openrazer.access.mount_helper_path
        or helper.mode != 0o755
        or not helper.required
    ):
        raise ModelError("OpenRazer native mount helper build does not match its deployment authority")

    payload_by_id = {payload.id: payload for payload in payloads}
    relay_service = payload_by_id.get("openrazer-native-relay-service")
    if (
        relay_service is None
        or relay_service.distribution_component != "full"
        or relay_service.source
        != f"packaging/generated/{openrazer.relay.service_unit}"
        or relay_service.destination
        != f"/usr/lib/systemd/system/{openrazer.relay.service_unit}"
        or relay_service.mode != 0o644
        or relay_service.preserve
    ):
        raise ModelError("OpenRazer native relay service is missing from the package")
    preflight_service = payload_by_id.get(
        "openrazer-native-driver-preflight-service"
    )
    if (
        preflight_service is None
        or preflight_service.distribution_component != "full"
        or preflight_service.source
        != f"packaging/generated/{openrazer.relay.preflight_service_unit}"
        or preflight_service.destination
        != f"/usr/lib/systemd/system/{openrazer.relay.preflight_service_unit}"
        or preflight_service.mode != 0o644
        or preflight_service.preserve
    ):
        raise ModelError(
            "OpenRazer native driver preflight service is missing from the package"
        )
    openrazer_group = payload_by_id.get("openrazer-system-group")
    if (
        openrazer_group is None
        or openrazer_group.distribution_component != "full"
        or openrazer_group.source != "packaging/generated/openrazer.sysusers"
        or openrazer_group.destination != openrazer.access.sysusers_path
        or openrazer_group.mode != 0o644
        or openrazer_group.preserve
    ):
        raise ModelError("OpenRazer native system group is missing from the package")
    modules_load = payload_by_id.get("openrazer-native-modules-load")
    if (
        modules_load is None
        or modules_load.distribution_component != "full"
        or modules_load.source != "packaging/generated/hyperflux-next-openrazer.modules-load.conf"
        or modules_load.destination != "/usr/lib/modules-load.d/hyperflux-next-openrazer.conf"
        or modules_load.mode != 0o644
        or modules_load.preserve
    ):
        raise ModelError("OpenRazer native boot module list is missing from the package")
    key_event_links = payload_by_id.get("openrazer-native-key-event-links")
    if (
        key_event_links is None
        or key_event_links.distribution_component != "full"
        or key_event_links.source
        != "packaging/generated/70-hyperflux-next-openrazer-keys.rules"
        or key_event_links.destination != openrazer.key_events.udev_rules_path
        or key_event_links.mode != 0o644
        or key_event_links.preserve
    ):
        raise ModelError("OpenRazer key-event udev rules are missing from the package")
    configuration = payload_by_id.get("default-configuration")
    if (
        configuration is None
        or configuration.distribution_component != "linux"
        or configuration.destination != runtime.bridge.configuration_file_path
        or not configuration.preserve
    ):
        raise ModelError("default bridge configuration must be preserved at the runtime path")
    if any(payload.preserve and payload.id != "default-configuration" for payload in payloads):
        raise ModelError("only the default bridge configuration may be preserved")
    full_builds = {
        build.id for build in builds if build.distribution_component == "full"
    }
    if full_builds != {
        "openrazer-native-relay",
        "openrazer-native-driver",
        "openrazer-native-udev-rules",
        "openrazer-native-mount-helper",
        "openrgb-native",
    }:
        raise ModelError("full component build ownership differs from the product contract")
    full_payloads = {
        payload.id for payload in payloads if payload.distribution_component == "full"
    }
    if full_payloads != {
        "openrazer-native-relay-service",
        "openrazer-native-driver-preflight-service",
        "openrazer-system-group",
        "openrazer-native-modules-load",
        "openrazer-native-key-event-links",
        "full-bridge-integration-drop-in",
        "full-component-descriptor",
        "openrgb-license",
        "lucide-license",
        "full-rust-crates-license",
        "nlohmann-json-license",
    }:
        raise ModelError("full component payload ownership differs from the product contract")
    bridge_drop_in = payload_by_id["full-bridge-integration-drop-in"]
    if (
        bridge_drop_in.source
        != "packaging/generated/hyperflux-next-bridge.service.d/50-full.conf"
        or bridge_drop_in.destination
        != "/usr/lib/systemd/system/hyperflux-next-bridge.service.d/50-full.conf"
        or bridge_drop_in.mode != 0o644
        or bridge_drop_in.preserve
    ):
        raise ModelError("Full bridge integration drop-in differs from the exact contract")
    descriptor = payload_by_id["full-component-descriptor"]
    if (
        descriptor.source != "packaging/generated/full-component.json"
        or descriptor.destination
        != runtime.operations.full_component_descriptor_path
        or descriptor.mode != 0o644
        or descriptor.preserve
    ):
        raise ModelError("Full component descriptor differs from the exact contract")
    full_license = payload_by_id["openrgb-license"]
    if (
        full_license.source != "LICENSE"
        or full_license.destination
        != "/usr/share/licenses/hyperflux-next-full/LICENSE"
        or full_license.mode != 0o644
        or full_license.preserve
        or full_license.include
    ):
        raise ModelError("full package license differs from the exact payload contract")
    icon_license = payload_by_id["lucide-license"]
    if (
        icon_license.source != "integrations/openrgb/icons/LICENSE-lucide"
        or icon_license.destination
        != "/usr/share/licenses/hyperflux-next-full/LICENSE-lucide"
        or icon_license.mode != 0o644
        or icon_license.preserve
        or icon_license.include
    ):
        raise ModelError("bundled icon license differs from the exact payload contract")
    # Notices for third-party code compiled into the programs: the Rust crates
    # in every package with a Rust program, and nlohmann/json in the plugin.
    for payload_id, component, source, name in (
        ("core-rust-crates-license", "linux", "packaging/licenses/LICENSE-rust-crates", "LICENSE-rust-crates"),
        ("full-rust-crates-license", "full", "packaging/licenses/LICENSE-rust-crates", "LICENSE-rust-crates"),
        ("nlohmann-json-license", "full", "sdk/cpp/vendor/LICENSE.MIT", "LICENSE-nlohmann-json"),
    ):
        notice = payload_by_id.get(payload_id)
        if (
            notice is None
            or notice.distribution_component != component
            or notice.source != source
            or notice.destination
            != f"/usr/share/licenses/hyperflux-next-{component}/{name}"
            or notice.mode != 0o644
            or notice.preserve
            or notice.include
        ):
            raise ModelError(f"{payload_id} differs from the exact payload contract")

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
        or value["schema"] != "hyperflux-install-manifest-v2"
    ):
        raise ModelError("unsupported install manifest schema")

    policy_value = _object(value["policy"], "install policy")
    _exact(policy_value, POLICY_KEYS, "install policy")
    if not all(isinstance(policy_value[key], bool) for key in POLICY_KEYS):
        raise ModelError("install policy fields must be boolean")
    policy = InstallPolicy(**policy_value)
    first_run_activation = _first_run_activation(value["first_run_activation"])

    runtime = load_linux_runtime(root)
    tokens = _tokens(root, runtime)
    projected = tuple(file.resolve() for file in projected_files)
    builds = _load_builds(
        root,
        _list(value["builds"], "install builds", minimum=1, maximum=32),
        tokens,
    )
    payloads = _load_payloads(
        root,
        _list(value["payloads"], "install payloads", minimum=1, maximum=128),
        tokens,
        projected,
    )
    files = _expand_payload_files(root, payloads, projected_files)
    _check_cross_contract(root, runtime, policy, builds, payloads, files)
    return InstallManifest(
        root=root.resolve(),
        source_sha256=sha256_file(path),
        policy=policy,
        first_run_activation=first_run_activation,
        builds=builds,
        payloads=payloads,
        files=files,
    )
