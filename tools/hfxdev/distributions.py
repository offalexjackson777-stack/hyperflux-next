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


TARGET_IDS = ("arch",)
COMPONENT_IDS = ("linux", "full")
DEPENDENCY_ROLES = {
    "dkms",
    "c-runtime",
    "compiler-runtime",
    "service-manager",
    "device-manager",
}
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.() >=~-]{0,95}$")
ARCHITECTURE = re.compile(r"^[A-Za-z0-9_.-]{1,127}$")
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
class DistributionTarget:
    id: str
    architectures: dict[str, str]
    dependency_roles: dict[str, str]
    components: dict[str, DistributionComponent]

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


def _target(
    target_id: str,
    value: Any,
    *,
    openrazer_version: str,
    sibling_version: str,
) -> DistributionTarget:
    if not isinstance(value, dict):
        raise ModelError(f"distribution {target_id}: must be an object")
    _exact(
        value,
        {"architectures", "dependency_roles", "components"},
        f"distribution {target_id}",
    )
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
    components_value = value["components"]
    if not isinstance(components_value, dict) or tuple(components_value) != COMPONENT_IDS:
        raise ModelError(
            f"distribution {target_id}: components must be ordered linux, full"
        )
    components = {
        component_id: _component(
            target_id,
            component_id,
            components_value[component_id],
            roles,
            openrazer_version=openrazer_version,
            sibling_version=sibling_version,
        )
        for component_id in COMPONENT_IDS
    }
    require_unique(
        [component.package_name for component in components.values()],
        f"distribution {target_id} package name",
    )
    return DistributionTarget(
        id=target_id,
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
        "qt5-base",
        "hidapi",
        "libstdc++",
    }
    if set(full.dependencies) != expected_full:
        raise ModelError("full-package dependencies differ from the adapter contract")
    expected_optional = {
        "openrgb": "Stock OpenRGB host for the HyperFlux plugin; OpenRGB 1.0rc3/plugin API 4 verified",
        "openrazer-daemon": "Stock OpenRazer service for compatible application access",
        "python-openrazer": "Official Python OpenRazer client API",
        "polychromatic": "Stock graphical OpenRazer frontend",
    }
    if {item.package: item.purpose for item in full.optional_dependencies} != expected_optional:
        raise ModelError("full-package optional applications differ from the contract")


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
        raise ModelError("distribution targets must contain only arch")
    runtime = load_linux_runtime(root)
    version = runtime.product.version.replace("-", "_")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_]*", version):
        raise ModelError("distribution sibling version cannot be represented on Arch")
    sibling_version = f"{version}-{runtime.product.package_release}"
    openrazer_version = load_native_deployment(root).driver.dkms_version
    target = _target(
        "arch",
        targets_value["arch"],
        openrazer_version=openrazer_version,
        sibling_version=sibling_version,
    )
    _check_arch_contract(target, sibling_version)
    return DistributionCatalog(
        source_sha256=sha256_file(path),
        description=description,
        homepage=homepage,
        licenses=licenses,
        targets={"arch": target},
    )
