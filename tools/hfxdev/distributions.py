# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from .linux_runtime import load_linux_runtime
from .model import ModelError, load_json, require_unique, sha256_file
from .openrazer_native_route import load_native_deployment


TARGET_IDS = ("arch", "ubuntu", "debian", "fedora", "opensuse")
DEBIAN_FORMAT_TARGET_IDS = ("ubuntu", "debian")
RPM_FORMAT_TARGET_IDS = ("fedora", "opensuse")
COMPONENT_IDS = ("linux", "full")
# Every target carries both editions.
TARGET_COMPONENT_IDS = {
    "arch": COMPONENT_IDS,
    "ubuntu": COMPONENT_IDS,
    "debian": COMPONENT_IDS,
    "fedora": COMPONENT_IDS,
    "opensuse": COMPONENT_IDS,
}
# Full carries the OpenRGB plugin only where the release ships an OpenRGB that
# can load it: OpenRGB 1.0 final or newer, plugin API 5. Checked 18 October
# 2026: Arch ships 1.1 built with Qt 6 and Debian 14 ships 1.0 with Qt 5.15; Ubuntu 26.10 ships a
# 1.0 development snapshot with plugin API 4, and Fedora 44 and openSUSE
# Tumbleweed ship a 1.0 release candidate with plugin API 4. Full there is the
# OpenRazer route alone.
OPENRGB_PLUGIN_TARGET_IDS = ("arch", "debian")
# The Qt major version each plugin target's OpenRGB is built with. The plugin
# must use the same one, so a target other than Arch builds the plugin inside
# its own release against the release's Qt.
OPENRGB_PLUGIN_QT_MAJOR = {"arch": 6, "debian": 5}
# The Arch packages only the OpenRGB plugin needs. A target without the plugin
# leaves them out.
OPENRGB_PLUGIN_ARCH_DEPENDENCIES = ("qt6-base", "hidapi", "libstdc++")
# The release packages that hold the libraries the plugin links on each
# plugin target other than Arch, read from the plugin built there.
OPENRGB_PLUGIN_RELEASE_DEPENDENCIES = {
    "debian": (
        "libqt5core5t64",
        "libqt5gui5t64",
        "libqt5widgets5t64",
        "libhidapi-hidraw0",
        "libstdc++6",
    ),
}
# The split build and payload that make up the OpenRGB plugin.
OPENRGB_PLUGIN_BUILD = "openrgb-native"
TARGET_PACKAGE_FORMATS = {
    "arch": "pacman",
    "ubuntu": "deb",
    "debian": "deb",
    "fedora": "rpm",
    "opensuse": "rpm",
}
MATURITIES = ("ci-only", "published")
DEPENDENCY_ROLES = {
    "dkms",
    "c-runtime",
    "compiler-runtime",
    "service-manager",
    "device-manager",
}
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.() >=~-]{0,95}$")
ARCHITECTURE = re.compile(r"^[A-Za-z0-9_.-]{1,127}$")
DEBIAN_RELATION = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9+.-]{1,63})"
    r"(?: \((?P<operator><<|<=|=|>=|>>) (?P<version>[0-9][A-Za-z0-9.+~-]{0,63})\))?$"
)
DEBIAN_UPSTREAM_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~]*$")
# An RPM relation separates name, operator and EVR with single spaces. The
# version may carry one release after a hyphen.
RPM_RELATION = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_+.-]{1,63})"
    r"(?: (?P<operator><=|>=|=|<|>) (?P<version>[0-9][A-Za-z0-9.+~^_]{0,63}(?:-[0-9][A-Za-z0-9.+~^_]{0,31})?))?$"
)
RPM_UPSTREAM_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~^_]*$")
RELEASE_VERSION = re.compile(r"^[0-9]{1,2}(?:\.[0-9]{2})?$")
RELEASE_CODENAME = re.compile(r"^[a-z]{2,32}$")
RELEASE_DISTRIBUTION = re.compile(r"^[A-Z][A-Za-z]{1,31}$")
# RPM releases are a numbered release (`44`, `16.0`) or a rolling one named by
# its flavor (`Tumbleweed`). Distribution names may start in lower case
# (`openSUSE`).
RPM_RELEASE_VERSION = re.compile(r"^(?:[0-9]{1,2}(?:\.[0-9]{1,2})?|[A-Z][a-z]{2,31})$")
RPM_RELEASE_DISTRIBUTION = re.compile(r"^[A-Za-z][A-Za-z]{1,31}$")
RELATIONS = {"deb": DEBIAN_RELATION, "rpm": RPM_RELATION}
RELATION_LABELS = {"deb": "a Debian", "rpm": "an RPM"}
C_RUNTIME_VERSION = re.compile(r"^[0-9]+\.[0-9]+$")
LICENSE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
OPENRAZER_DRIVER_VERSION_TOKEN = "@openrazer_driver_version@"
SIBLING_VERSION_TOKEN = "@sibling_version@"
FORBIDDEN_APPLICATION_RELATIONS = {
    "polychromatic",
    "openrazer-daemon",
    "python-openrazer",
    "openrgb",
}


@dataclass(frozen=True)
class OptionalDependency:
    package: str
    purpose: str


@dataclass(frozen=True)
class DistributionComponent:
    id: str
    package_name: str
    description: str
    dependency_roles: tuple[str, ...]
    dependencies: tuple[str, ...]
    optional_dependencies: tuple[OptionalDependency, ...]
    conflicts: tuple[str, ...]
    provides: tuple[str, ...]
    replaces: tuple[str, ...]


@dataclass(frozen=True)
class DistributionRelease:
    """The one distribution release a Debian- or RPM-format target is built for.

    Debian-format releases carry a codename; RPM-format releases have none.
    """

    distribution: str
    version: str
    codename: str | None
    c_runtime_version: str


@dataclass(frozen=True)
class DistributionTarget:
    id: str
    package_format: str
    maturity: str
    release: DistributionRelease | None
    architectures: dict[str, str]
    dependency_roles: dict[str, str]
    components: dict[str, DistributionComponent]

    @property
    def published(self) -> bool:
        return self.maturity == "published"

    @property
    def openrgb_plugin(self) -> bool:
        """Whether this target's Full package carries the OpenRGB plugin."""

        return self.id in OPENRGB_PLUGIN_TARGET_IDS

    @property
    def openrgb_plugin_release_build(self) -> bool:
        """Whether the plugin is built inside this target's release.

        Arch Full stages the plugin from the shared build. Any other plugin
        target builds it against its own release's Qt.
        """

        return self.openrgb_plugin and self.id != "arch"

    @property
    def openrgb_qt_major(self) -> int:
        """The Qt major version the plugin is built against on this target."""

        try:
            return OPENRGB_PLUGIN_QT_MAJOR[self.id]
        except KeyError as error:
            raise ModelError(
                f"distribution {self.id}: Full carries no OpenRGB plugin"
            ) from error

    def architecture_for(self, build_target: str) -> str:
        try:
            return self.architectures[build_target]
        except KeyError as error:
            raise ModelError(
                f"distribution {self.id}: unsupported build target {build_target}"
            ) from error

    def component(self, component_id: str) -> DistributionComponent:
        try:
            return self.components[component_id]
        except KeyError as error:
            raise ModelError(
                f"distribution {self.id}: unknown component {component_id}"
            ) from error


@dataclass(frozen=True)
class DistributionCatalog:
    source_sha256: str
    description: str
    homepage: str
    licenses: tuple[str, ...]
    targets: dict[str, DistributionTarget]


def _exact(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields {', '.join(missing)}")
        if extra:
            details.append(f"unknown fields {', '.join(extra)}")
        raise ModelError(f"{label}: {'; '.join(details)}")


def _text(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or CONTROL_CHARACTER.search(value)
    ):
        raise ModelError(f"{label}: invalid text")
    return value


def _package(value: Any, label: str) -> str:
    if not isinstance(value, str) or not PACKAGE.fullmatch(value):
        raise ModelError(f"{label}: invalid package expression")
    return value


def _relation_name(value: str) -> str:
    return re.split(r"[<>= ]", value, maxsplit=1)[0]


def debian_version(version: str, release: int) -> str:
    """Return the Debian `upstream-revision` for a product version.

    A pre-release hyphen becomes `~` so `2.1.0-rc.1` sorts before `2.1.0`.
    """

    upstream = version.replace("-", "~")
    if not DEBIAN_UPSTREAM_VERSION.fullmatch(upstream) or release < 1:
        raise ModelError(f"product version {version}-{release} cannot be represented on Debian")
    return f"{upstream}-{release}"


def debian_exact(package_name: str, version: str) -> str:
    """Debian relation that pins one sibling package to one exact version."""

    relation = f"{package_name} (= {version})"
    if not DEBIAN_RELATION.fullmatch(relation):
        raise ModelError(f"invalid Debian relation {relation!r}")
    return relation


def rpm_version(version: str, release: int) -> str:
    """Return the RPM `version-release` for a product version.

    RPM forbids a hyphen inside the version, so a pre-release hyphen becomes
    `~`, which sorts `2.1.0~rc.1` before `2.1.0` as on Debian.
    """

    upstream = version.replace("-", "~")
    if not RPM_UPSTREAM_VERSION.fullmatch(upstream) or release < 1:
        raise ModelError(f"product version {version}-{release} cannot be represented in RPM")
    return f"{upstream}-{release}"


def rpm_exact(package_name: str, version: str) -> str:
    """RPM relation that pins one sibling package to one exact version-release."""

    relation = f"{package_name} = {version}"
    if not RPM_RELATION.fullmatch(relation):
        raise ModelError(f"invalid RPM relation {relation!r}")
    return relation


def version_key(value: str) -> tuple[int, ...]:
    if not C_RUNTIME_VERSION.fullmatch(value):
        raise ModelError(f"invalid runtime version {value!r}")
    return tuple(int(part) for part in value.split("."))


def c_runtime_floor(target: DistributionTarget) -> str:
    """The minimum C runtime version a Debian- or RPM-format target declares."""

    pattern = RELATIONS.get(target.package_format)
    if pattern is None:
        raise ModelError(f"distribution {target.id}: no C runtime floor for its package format")
    match = pattern.fullmatch(target.dependency_roles["c-runtime"])
    if match is None or match.group("operator") != ">=":
        raise ModelError(f"distribution {target.id}: C runtime role has no lower bound")
    return match.group("version")


def _expand_package(
    value: Any,
    label: str,
    *,
    openrazer_version: str,
    sibling_version: str,
) -> str:
    if not isinstance(value, str):
        raise ModelError(f"{label}: invalid package expression")
    for token in (OPENRAZER_DRIVER_VERSION_TOKEN, SIBLING_VERSION_TOKEN):
        if value.count(token) > 1:
            raise ModelError(f"{label}: repeated package token")
    expanded = value.replace(OPENRAZER_DRIVER_VERSION_TOKEN, openrazer_version)
    expanded = expanded.replace(SIBLING_VERSION_TOKEN, sibling_version)
    if "@" in expanded:
        raise ModelError(f"{label}: unknown package token")
    return _package(expanded, label)


def _package_list(
    value: Any,
    label: str,
    *,
    openrazer_version: str,
    sibling_version: str,
    maximum: int = 16,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ModelError(f"{label}: invalid package list")
    parsed = tuple(
        _expand_package(
            item,
            label,
            openrazer_version=openrazer_version,
            sibling_version=sibling_version,
        )
        for item in value
    )
    require_unique(list(parsed), label)
    return parsed


def _component(
    target_id: str,
    component_id: str,
    value: Any,
    roles: dict[str, str],
    *,
    openrazer_version: str,
    sibling_version: str,
    package_format: str = "pacman",
) -> DistributionComponent:
    label = f"distribution {target_id} component {component_id}"
    if not isinstance(value, dict):
        raise ModelError(f"{label}: must be an object")
    _exact(
        value,
        {
            "package_name",
            "description",
            "dependency_roles",
            "dependencies",
            "optional_dependencies",
            "conflicts",
            "provides",
            "replaces",
        },
        label,
    )
    package_name = _package(value["package_name"], f"{label} package name")
    description = _text(value["description"], f"{label} description", 160)
    role_values = value["dependency_roles"]
    if not isinstance(role_values, list) or len(role_values) > len(DEPENDENCY_ROLES):
        raise ModelError(f"{label}: invalid dependency roles")
    if any(not isinstance(role, str) or role not in roles for role in role_values):
        raise ModelError(f"{label}: unknown dependency role")
    require_unique(role_values, f"{label} dependency role")
    direct = _package_list(
        value["dependencies"],
        f"{label} dependency",
        openrazer_version=openrazer_version,
        sibling_version=sibling_version,
        maximum=24,
    )
    dependencies = tuple(dict.fromkeys([*(roles[role] for role in role_values), *direct]))

    optional_values = value["optional_dependencies"]
    if not isinstance(optional_values, list) or len(optional_values) > 16:
        raise ModelError(f"{label}: invalid optional dependencies")
    optional: list[OptionalDependency] = []
    for index, item in enumerate(optional_values):
        item_label = f"{label} optional dependency {index}"
        if not isinstance(item, dict):
            raise ModelError(f"{item_label}: must be an object")
        _exact(item, {"package", "purpose"}, item_label)
        optional.append(
            OptionalDependency(
                package=_expand_package(
                    item["package"],
                    f"{item_label} package",
                    openrazer_version=openrazer_version,
                    sibling_version=sibling_version,
                ),
                purpose=_text(item["purpose"], f"{item_label} purpose", 120),
            )
        )
    require_unique([item.package for item in optional], f"{label} optional package")

    conflicts = _package_list(
        value["conflicts"],
        f"{label} conflict",
        openrazer_version=openrazer_version,
        sibling_version=sibling_version,
    )
    provides = _package_list(
        value["provides"],
        f"{label} provide",
        openrazer_version=openrazer_version,
        sibling_version=sibling_version,
    )
    replaces = _package_list(
        value["replaces"],
        f"{label} replace",
        openrazer_version=openrazer_version,
        sibling_version=sibling_version,
    )
    if package_name in {
        *map(_relation_name, conflicts),
        *map(_relation_name, provides),
        *map(_relation_name, replaces),
    }:
        raise ModelError(f"{label}: package cannot relate to itself")
    forbidden = FORBIDDEN_APPLICATION_RELATIONS & {
        *map(_relation_name, conflicts),
        *map(_relation_name, provides),
        *map(_relation_name, replaces),
    }
    if forbidden:
        raise ModelError(
            f"{label}: application replacement relationships are forbidden: "
            f"{', '.join(sorted(forbidden))}"
        )
    relation_pattern = RELATIONS.get(package_format)
    if relation_pattern is not None:
        for relation in (
            package_name,
            *direct,
            *(item.package for item in optional),
            *conflicts,
            *provides,
            *replaces,
        ):
            if not relation_pattern.fullmatch(relation):
                raise ModelError(
                    f"{label}: {relation!r} is not {RELATION_LABELS[package_format]} relation"
                )
    return DistributionComponent(
        id=component_id,
        package_name=package_name,
        description=description,
        dependency_roles=tuple(role_values),
        dependencies=dependencies,
        optional_dependencies=tuple(optional),
        conflicts=conflicts,
        provides=provides,
        replaces=replaces,
    )


def _release(target_id: str, value: Any, package_format: str) -> DistributionRelease:
    label = f"distribution {target_id} release"
    if not isinstance(value, dict):
        raise ModelError(f"{label}: must be an object")
    if package_format == "deb":
        checks: tuple[tuple[str, re.Pattern[str]], ...] = (
            ("distribution", RELEASE_DISTRIBUTION),
            ("version", RELEASE_VERSION),
            ("codename", RELEASE_CODENAME),
            ("c_runtime_version", C_RUNTIME_VERSION),
        )
    else:
        checks = (
            ("distribution", RPM_RELEASE_DISTRIBUTION),
            ("version", RPM_RELEASE_VERSION),
            ("c_runtime_version", C_RUNTIME_VERSION),
        )
    _exact(value, {field for field, _ in checks}, label)
    for field, pattern in checks:
        if not isinstance(value[field], str) or not pattern.fullmatch(value[field]):
            raise ModelError(f"{label}: invalid {field.replace('_', ' ')}")
    return DistributionRelease(
        distribution=value["distribution"],
        version=value["version"],
        codename=value.get("codename"),
        c_runtime_version=value["c_runtime_version"],
    )


def _target(
    target_id: str,
    value: Any,
    *,
    openrazer_version: str,
    sibling_version: str,
) -> DistributionTarget:
    if not isinstance(value, dict):
        raise ModelError(f"distribution {target_id}: must be an object")
    package_format = TARGET_PACKAGE_FORMATS[target_id]
    # Every format other than pacman is built for one named release.
    released = package_format in RELATIONS
    fields = {"package_format", "maturity", "architectures", "dependency_roles", "components"}
    _exact(
        value,
        fields | {"release"} if released else fields,
        f"distribution {target_id}",
    )
    if value["package_format"] != package_format:
        raise ModelError(f"distribution {target_id}: package format must be {package_format}")
    maturity = value["maturity"]
    if maturity not in MATURITIES:
        raise ModelError(f"distribution {target_id}: unknown maturity")
    if not released and maturity != "published":
        raise ModelError(f"distribution {target_id}: the Arch target is the published channel")
    release = _release(target_id, value["release"], package_format) if released else None
    architectures = value["architectures"]
    if not isinstance(architectures, dict) or not 1 <= len(architectures) <= 8:
        raise ModelError(f"distribution {target_id}: invalid architecture map")
    for source, destination in architectures.items():
        if (
            not isinstance(source, str)
            or not isinstance(destination, str)
            or not ARCHITECTURE.fullmatch(source)
            or not ARCHITECTURE.fullmatch(destination)
        ):
            raise ModelError(f"distribution {target_id}: invalid architecture")
    require_unique(list(architectures.values()), f"distribution {target_id} architecture")

    role_values = value["dependency_roles"]
    if not isinstance(role_values, dict) or set(role_values) != DEPENDENCY_ROLES:
        raise ModelError(f"distribution {target_id}: dependency roles are incomplete")
    roles = {
        role: _package(package, f"distribution {target_id} role {role}")
        for role, package in role_values.items()
    }
    if released and any(
        not RELATIONS[package_format].fullmatch(item) for item in roles.values()
    ):
        raise ModelError(
            f"distribution {target_id}: dependency roles must be "
            f"{RELATION_LABELS[package_format].split(' ', 1)[1]} relations"
        )
    component_ids = TARGET_COMPONENT_IDS[target_id]
    components_value = value["components"]
    if not isinstance(components_value, dict) or tuple(components_value) != component_ids:
        raise ModelError(
            f"distribution {target_id}: components must be ordered {', '.join(component_ids)}"
        )
    components = {
        component_id: _component(
            target_id,
            component_id,
            components_value[component_id],
            roles,
            openrazer_version=openrazer_version,
            sibling_version=sibling_version,
            package_format=package_format,
        )
        for component_id in component_ids
    }
    require_unique(
        [component.package_name for component in components.values()],
        f"distribution {target_id} package name",
    )
    return DistributionTarget(
        id=target_id,
        package_format=package_format,
        maturity=maturity,
        release=release,
        architectures=dict(sorted(architectures.items())),
        dependency_roles=roles,
        components=components,
    )


def _check_arch_contract(target: DistributionTarget, sibling_version: str) -> None:
    linux = target.component("linux")
    full = target.component("full")
    if (linux.package_name, full.package_name) != (
        "hyperflux-next-linux",
        "hyperflux-next-full",
    ):
        raise ModelError("Arch component package names differ from the product contract")
    if linux.conflicts != ("hyperflux-v2-linux-dkms",):
        raise ModelError("Arch core conflicts differ from the receiver-driver contract")
    if linux.provides or linux.replaces:
        raise ModelError("Arch core cannot provide or replace OpenRazer packages")
    if set(full.conflicts) != {
        "hyperflux-next-openrgb=0.0.0_dev.1-2",
        "openrazer-driver-dkms",
    } or set(full.replaces) != {
        "hyperflux-next-openrgb=0.0.0_dev.1-2",
        "openrazer-driver-dkms",
    }:
        raise ModelError("full package replacement relations differ from the exact contract")
    if set(full.provides) != {
        "openrazer-driver-dkms=3.12.4",
        "hyperflux-openrazer-driver-abi=3.12.4",
    }:
        raise ModelError("full package OpenRazer driver ABI relations differ from the contract")
    expected_full = {
        target.dependency_roles["dkms"],
        target.dependency_roles["c-runtime"],
        target.dependency_roles["compiler-runtime"],
        target.dependency_roles["service-manager"],
        target.dependency_roles["device-manager"],
        f"hyperflux-next-linux={sibling_version}",
        *OPENRGB_PLUGIN_ARCH_DEPENDENCIES,
    }
    if set(full.dependencies) != expected_full:
        raise ModelError("full-package dependencies differ from the adapter contract")
    expected_optional = {
        "openrgb": OPENRGB_APPLICATION_PURPOSE,
        "openrazer-daemon": "Stock OpenRazer service for compatible application access",
        "python-openrazer": "Official Python OpenRazer client API",
        "polychromatic": "Stock graphical OpenRazer frontend",
    }
    if {item.package: item.purpose for item in full.optional_dependencies} != expected_optional:
        raise ModelError("full-package optional applications differ from the contract")


# Each Debian-format target is held to one exact release. Both use the same
# package names for their dependency roles.
DEBIAN_FORMAT_RELEASES = {
    "ubuntu": ("Ubuntu", "26.10", "stonking"),
    "debian": ("Debian", "14", "forky"),
}
DEBIAN_FORMAT_ROLES = {
    "dkms": "dkms",
    "compiler-runtime": "libgcc-s1",
    "service-manager": "systemd",
    "device-manager": "udev",
}


def repository_site_published(root: Path) -> bool:
    """Whether the signed repository site that carries these packages is live."""

    # The site policy names the distribution targets, so it is read lazily.
    from .package_repositories import load_repository_site_policy

    return load_repository_site_policy(root).published


def _check_publication(target: DistributionTarget, name: str, site_published: bool) -> None:
    """A release is published only through the live signed repository site."""

    if target.published and not site_published:
        raise ModelError(
            f"{name} packages cannot be published while the repository site is unpublished; "
            "set packaging/repository-site.json state to published first"
        )


def _check_debian_format_contract(
    target: DistributionTarget, arch: DistributionTarget, *, site_published: bool
) -> None:
    """Hold a Debian-format Core package to the Arch Core product contract."""

    name, version, codename = DEBIAN_FORMAT_RELEASES[target.id]
    release = target.release
    if release is None or (release.distribution, release.version, release.codename) != (
        name,
        version,
        codename,
    ):
        raise ModelError(f"{name} target must name exactly {name} {version} {codename}")
    _check_publication(target, name, site_published)
    if target.architectures != {"x86_64-unknown-linux-gnu": "amd64"}:
        raise ModelError(f"{name} target is amd64 only")
    roles = {key: value for key, value in target.dependency_roles.items() if key != "c-runtime"}
    if roles != DEBIAN_FORMAT_ROLES:
        raise ModelError(f"{name} dependency roles differ from the {codename} package names")
    _check_core_contract(target, arch, name, "libc6")
    _check_full_contract(target, arch, name)


def _check_core_contract(
    target: DistributionTarget,
    arch: DistributionTarget,
    name: str,
    c_runtime: str,
) -> None:
    """Hold a target's C runtime and Core package to the Arch Core contract."""

    release = target.release
    if release is None:
        raise ModelError(f"{name} target must name its release")
    floor = c_runtime_floor(target)
    if _relation_name(target.dependency_roles["c-runtime"]) != c_runtime:
        raise ModelError(f"{name} C runtime role must name {c_runtime}")
    if version_key(floor) > version_key(release.c_runtime_version):
        raise ModelError(f"{name} C runtime floor exceeds the release C runtime")
    # A plugin built inside the release links against that release's glibc,
    # so the packages declare that glibc rather than the portable floor.
    if target.openrgb_plugin_release_build and floor != release.c_runtime_version:
        raise ModelError(
            f"{name} builds the OpenRGB plugin itself and must declare its release C runtime"
        )
    linux = target.component("linux")
    arch_linux = arch.component("linux")
    if (linux.package_name, linux.description) != (
        arch_linux.package_name,
        arch_linux.description,
    ):
        raise ModelError(f"{name} Core identity differs from the Arch Core package")
    if linux.dependency_roles != arch_linux.dependency_roles:
        raise ModelError(f"{name} Core dependency roles differ from the Arch Core package")
    if (linux.conflicts, linux.provides, linux.replaces) != (
        arch_linux.conflicts,
        arch_linux.provides,
        arch_linux.replaces,
    ):
        raise ModelError(f"{name} Core relations differ from the Arch Core package")
    if linux.optional_dependencies or set(linux.dependencies) != set(
        target.dependency_roles.values()
    ):
        raise ModelError(f"{name} Core may depend only on its mapped dependency roles")


# Full on a Debian- or RPM-format target replaces the release's OpenRazer
# driver package, as Full on Arch replaces `openrazer-driver-dkms`, and stands
# in for it so the release's own OpenRazer daemon still installs.
#
# Ubuntu and Debian: the daemon depends on `openrazer-driver-dkms` at its own
# exact version, so Full provides every version a supported source ships:
# Ubuntu 26.10, OpenRazer's PPA, and Debian 14. Read from the archives on
# 18 October 2026. A new upstream version needs a new
# entry here; until then apt holds that daemon upgrade back.
#
# Fedora and openSUSE ship no OpenRazer. OpenRazer's own openSUSE Build
# Service repository names the driver `openrazer-kernel-modules-dkms`, and its
# daemon depends on it without a version.
DISTRIBUTION_FULL_RELATIONS = {
    "ubuntu": (
        ("openrazer-driver-dkms", "openrazer-kernel-modules-dkms"),
        (
            "openrazer-driver-dkms (= 3.12.4+dfsg-1)",
            "openrazer-driver-dkms (= 3.12.4~ubuntu26.10.1)",
            "openrazer-kernel-modules-dkms",
            "hyperflux-openrazer-driver-abi (= 3.12.4)",
        ),
        ("openrazer-driver-dkms", "openrazer-kernel-modules-dkms"),
    ),
    "debian": (
        ("openrazer-driver-dkms", "openrazer-kernel-modules-dkms"),
        (
            "openrazer-driver-dkms (= 3.12.4+dfsg-1)",
            "openrazer-kernel-modules-dkms",
            "hyperflux-openrazer-driver-abi (= 3.12.4)",
        ),
        ("openrazer-driver-dkms", "openrazer-kernel-modules-dkms"),
    ),
    "fedora": (
        ("openrazer-kernel-modules-dkms",),
        (
            "openrazer-kernel-modules-dkms = 3.12.4",
            "hyperflux-openrazer-driver-abi = 3.12.4",
        ),
        (),
    ),
    "opensuse": (
        ("openrazer-kernel-modules-dkms",),
        (
            "openrazer-kernel-modules-dkms = 3.12.4",
            "hyperflux-openrazer-driver-abi = 3.12.4",
        ),
        (),
    ),
}
OPENRGB_APPLICATION_PURPOSE = (
    "Stock OpenRGB host for the HyperFlux plugin; OpenRGB 1.0/plugin API 5 verified"
)
# Polychromatic is in no Ubuntu or Debian archive. OpenRazer's openSUSE Build
# Service repository carries it for Fedora and openSUSE.
DISTRIBUTION_FULL_APPLICATIONS = {
    "deb": {
        "openrazer-daemon": "Stock OpenRazer service for compatible application access",
        "python3-openrazer": "Official Python OpenRazer client API",
    },
    "rpm": {
        "openrazer-daemon": "Stock OpenRazer service for compatible application access",
        "python3-openrazer": "Official Python OpenRazer client API",
        "polychromatic": "Stock graphical OpenRazer frontend",
    },
}


def _check_full_contract(target: DistributionTarget, arch: DistributionTarget, name: str) -> None:
    """Hold a Debian- or RPM-format Full package to the Arch Full product contract.

    It is Arch Full with the release's own plugin libraries and OpenRGB
    where the release can load the plugin, and Arch Full without the plugin
    elsewhere: no plugin libraries and no OpenRGB application. Either way it
    stands in for the release's OpenRazer driver package instead of Arch's.
    """

    full = target.component("full")
    arch_full = arch.component("full")
    if target.openrgb_plugin and target.id not in OPENRGB_PLUGIN_RELEASE_DEPENDENCIES:
        raise ModelError(f"{name} ships no OpenRGB that can load the HyperFlux plugin")
    if full.package_name != arch_full.package_name:
        raise ModelError(f"{name} Full package name differs from the Arch Full package")
    description = (
        arch_full.description
        if target.openrgb_plugin
        else "Core plus the OpenRazer compatibility transport for optional stock applications"
    )
    if full.description != description:
        raise ModelError(f"{name} Full description must name exactly what it carries")
    if full.dependency_roles != arch_full.dependency_roles:
        raise ModelError(f"{name} Full dependency roles differ from the Arch Full package")
    plugin_dependencies = (
        OPENRGB_PLUGIN_RELEASE_DEPENDENCIES[target.id] if target.openrgb_plugin else ()
    )
    if set(full.dependencies) != {*target.dependency_roles.values(), *plugin_dependencies}:
        raise ModelError(
            f"{name} Full may depend only on its mapped dependency roles"
            + (" and the plugin libraries" if target.openrgb_plugin else "")
        )
    if (full.conflicts, full.provides, full.replaces) != DISTRIBUTION_FULL_RELATIONS[target.id]:
        raise ModelError(f"{name} Full OpenRazer driver relations differ from the contract")
    applications = dict(DISTRIBUTION_FULL_APPLICATIONS[target.package_format])
    if target.openrgb_plugin:
        applications = {"openrgb": OPENRGB_APPLICATION_PURPOSE, **applications}
    if {item.package: item.purpose for item in full.optional_dependencies} != applications:
        raise ModelError(f"{name} Full optional applications differ from the contract")


# Each RPM-format target is held to one exact release and to the package names
# that release gives each dependency role. Fedora takes DKMS from Fedora
# proper; openSUSE Tumbleweed carries it in its main OSS repository.
RPM_FORMAT_RELEASES = {
    "fedora": ("Fedora", "44"),
    "opensuse": ("openSUSE", "Tumbleweed"),
}
RPM_FORMAT_ROLES = {
    "fedora": {
        "dkms": "dkms",
        "compiler-runtime": "libgcc",
        "service-manager": "systemd",
        "device-manager": "systemd-udev",
    },
    "opensuse": {
        "dkms": "dkms",
        "compiler-runtime": "libgcc_s1",
        "service-manager": "systemd",
        "device-manager": "udev",
    },
}


def _check_rpm_format_contract(
    target: DistributionTarget, arch: DistributionTarget, *, site_published: bool
) -> None:
    """Hold an RPM-format Core package to the Arch Core product contract."""

    name, version = RPM_FORMAT_RELEASES[target.id]
    release = target.release
    if release is None or (release.distribution, release.version, release.codename) != (
        name,
        version,
        None,
    ):
        raise ModelError(f"{name} target must name exactly {name} {version}")
    _check_publication(target, name, site_published)
    if target.architectures != {"x86_64-unknown-linux-gnu": "x86_64"}:
        raise ModelError(f"{name} target is x86_64 only")
    roles = {key: value for key, value in target.dependency_roles.items() if key != "c-runtime"}
    if roles != RPM_FORMAT_ROLES[target.id]:
        raise ModelError(f"{name} dependency roles differ from the {name} {version} package names")
    _check_core_contract(target, arch, name, "glibc")
    _check_full_contract(target, arch, name)


def load_distribution_catalog(root: Path) -> DistributionCatalog:
    path = root / "packaging" / "distributions.json"
    value = load_json(path)
    _exact(value, {"$schema", "schema", "metadata", "targets"}, "distribution catalog")
    if (
        value["$schema"] != "../schemas/distribution-packages.schema.json"
        or value["schema"] != "hyperflux-distribution-packages-v2"
    ):
        raise ModelError("unsupported distribution package catalog")
    metadata = value["metadata"]
    if not isinstance(metadata, dict):
        raise ModelError("distribution metadata must be an object")
    _exact(metadata, {"description", "homepage", "licenses"}, "distribution metadata")
    description = _text(metadata["description"], "distribution description", 160)
    homepage = _text(metadata["homepage"], "distribution homepage", 256)
    parsed_homepage = urlsplit(homepage)
    if (
        parsed_homepage.scheme != "https"
        or parsed_homepage.hostname is None
        or parsed_homepage.username is not None
        or parsed_homepage.password is not None
    ):
        raise ModelError("distribution homepage must be a credential-free HTTPS URL")
    licenses_value = metadata["licenses"]
    if not isinstance(licenses_value, list) or not 1 <= len(licenses_value) <= 8:
        raise ModelError("distribution licenses are invalid")
    licenses = tuple(licenses_value)
    if any(not isinstance(item, str) or not LICENSE.fullmatch(item) for item in licenses):
        raise ModelError("distribution license identifier is invalid")
    require_unique(list(licenses), "distribution license")

    targets_value = value["targets"]
    if not isinstance(targets_value, dict) or tuple(targets_value) != TARGET_IDS:
        raise ModelError(f"distribution targets must be ordered {', '.join(TARGET_IDS)}")
    runtime = load_linux_runtime(root)
    version = runtime.product.version.replace("-", "_")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_]*", version):
        raise ModelError("distribution sibling version cannot be represented on Arch")
    sibling_version = f"{version}-{runtime.product.package_release}"
    openrazer_version = load_native_deployment(root).driver.dkms_version
    site_published = repository_site_published(root)
    target = _target(
        "arch",
        targets_value["arch"],
        openrazer_version=openrazer_version,
        sibling_version=sibling_version,
    )
    _check_arch_contract(target, sibling_version)
    targets = {"arch": target}
    for target_id in DEBIAN_FORMAT_TARGET_IDS:
        targets[target_id] = _target(
            target_id,
            targets_value[target_id],
            openrazer_version=openrazer_version,
            sibling_version=debian_version(
                runtime.product.version, runtime.product.package_release
            ),
        )
        _check_debian_format_contract(
            targets[target_id], target, site_published=site_published
        )
    for target_id in RPM_FORMAT_TARGET_IDS:
        targets[target_id] = _target(
            target_id,
            targets_value[target_id],
            openrazer_version=openrazer_version,
            sibling_version=rpm_version(runtime.product.version, runtime.product.package_release),
        )
        _check_rpm_format_contract(
            targets[target_id], target, site_published=site_published
        )
    return DistributionCatalog(
        source_sha256=sha256_file(path),
        description=description,
        homepage=homepage,
        licenses=licenses,
        targets=targets,
    )
