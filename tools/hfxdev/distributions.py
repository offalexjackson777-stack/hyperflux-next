# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .linux_runtime import load_linux_runtime
from .model import ModelError, load_json, require_unique, sha256_file


TARGET_IDS = ("arch", "debian", "rpm")
DEPENDENCY_ROLES = {
    "dkms",
    "c-runtime",
    "compiler-runtime",
    "python",
    "service-manager",
    "device-manager",
}
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.() >=~-]{0,95}$")
ARCHITECTURE = re.compile(r"^[A-Za-z0-9_.-]{1,127}$")
LICENSE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class OptionalDependency:
    package: str
    purpose: str


@dataclass(frozen=True)
class DistributionTarget:
    id: str
    architectures: dict[str, str]
    dependency_roles: dict[str, str]
    optional_dependencies: tuple[OptionalDependency, ...]
    conflicts: tuple[str, ...]
    python_discovery_path: str

    @property
    def dependencies(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.dependency_roles.values()))

    def dependencies_for(self, python_version: str) -> tuple[str, ...]:
        dependencies = list(self.dependencies)
        if "@python_major_minor@" not in self.python_discovery_path:
            return tuple(dependencies)
        python_package = self.dependency_roles["python"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,63}", python_package):
            raise ModelError(
                f"distribution {self.id}: minor-bound Python package must be a plain name"
            )
        major, minor = self.python_major_minor(python_version)
        dependencies.remove(python_package)
        dependencies.extend(
            (f"{python_package}>={major}.{minor}", f"{python_package}<{major}.{minor + 1}")
        )
        return tuple(dependencies)

    def architecture_for(self, build_target: str) -> str:
        try:
            return self.architectures[build_target]
        except KeyError as error:
            raise ModelError(
                f"distribution {self.id}: unsupported build target {build_target}"
            ) from error

    def python_major_minor(self, python_version: str) -> tuple[int, int]:
        match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+.*)?", python_version)
        if match is None or (int(match.group(1)), int(match.group(2))) < (3, 11):
            raise ModelError(f"distribution {self.id}: unsupported Python version")
        return int(match.group(1)), int(match.group(2))

    def python_discovery_for(self, python_version: str) -> str:
        major, minor = self.python_major_minor(python_version)
        return self.python_discovery_path.replace(
            "@python_major_minor@", f"{major}.{minor}"
        )


@dataclass(frozen=True)
class DistributionCatalog:
    source_sha256: str
    description: str
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


def _package(value: Any, label: str) -> str:
    if not isinstance(value, str) or not PACKAGE.fullmatch(value):
        raise ModelError(f"{label}: invalid package expression")
    return value


def _target(target_id: str, value: Any) -> DistributionTarget:
    if not isinstance(value, dict):
        raise ModelError(f"distribution {target_id}: must be an object")
    _exact(
        value,
        {
            "architectures",
            "dependency_roles",
            "optional_dependencies",
            "conflicts",
            "python_discovery_path",
        },
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

    roles = value["dependency_roles"]
    if not isinstance(roles, dict) or set(roles) != DEPENDENCY_ROLES:
        raise ModelError(f"distribution {target_id}: dependency roles are incomplete")
    dependencies = {role: _package(package, f"{target_id} {role}") for role, package in roles.items()}

    optional_values = value["optional_dependencies"]
    if not isinstance(optional_values, list) or len(optional_values) > 16:
        raise ModelError(f"distribution {target_id}: invalid optional dependencies")
    optional: list[OptionalDependency] = []
    for index, item in enumerate(optional_values):
        if not isinstance(item, dict):
            raise ModelError(f"distribution {target_id} optional {index}: must be an object")
        _exact(item, {"package", "purpose"}, f"distribution {target_id} optional {index}")
        purpose = item["purpose"]
        if (
            not isinstance(purpose, str)
            or not purpose
            or purpose != purpose.strip()
            or len(purpose) > 120
            or CONTROL_CHARACTER.search(purpose)
        ):
            raise ModelError(f"distribution {target_id} optional {index}: invalid purpose")
        optional.append(
            OptionalDependency(
                package=_package(item["package"], f"{target_id} optional package"),
                purpose=purpose,
            )
        )
    require_unique([item.package for item in optional], f"distribution {target_id} optional package")

    conflicts_value = value["conflicts"]
    if not isinstance(conflicts_value, list) or len(conflicts_value) > 16:
        raise ModelError(f"distribution {target_id}: invalid conflicts")
    conflicts = tuple(_package(item, f"{target_id} conflict") for item in conflicts_value)
    require_unique(list(conflicts), f"distribution {target_id} conflict")

    discovery = value["python_discovery_path"]
    if not isinstance(discovery, str) or discovery.count("@python_major_minor@") > 1:
        raise ModelError(f"distribution {target_id}: invalid Python discovery path")
    expanded = discovery.replace("@python_major_minor@", "3.11")
    path = PurePosixPath(expanded)
    if (
        not expanded.startswith("/usr/")
        or str(path) != expanded
        or ".." in path.parts
        or path.suffix != ".pth"
    ):
        raise ModelError(f"distribution {target_id}: unsafe Python discovery path")
    return DistributionTarget(
        id=target_id,
        architectures=dict(sorted(architectures.items())),
        dependency_roles=dependencies,
        optional_dependencies=tuple(optional),
        conflicts=conflicts,
        python_discovery_path=discovery,
    )


def load_distribution_catalog(root: Path) -> DistributionCatalog:
    path = root / "packaging" / "distributions.json"
    value = load_json(path)
    _exact(value, {"$schema", "schema", "metadata", "targets"}, "distribution catalog")
    if (
        value["$schema"] != "../schemas/distribution-packages.schema.json"
        or value["schema"] != "hyperflux-distribution-packages-v1"
    ):
        raise ModelError("unsupported distribution package catalog")
    metadata = value["metadata"]
    if not isinstance(metadata, dict):
        raise ModelError("distribution metadata must be an object")
    _exact(metadata, {"description", "licenses"}, "distribution metadata")
    description = metadata["description"]
    if (
        not isinstance(description, str)
        or not description
        or description != description.strip()
        or len(description) > 160
        or CONTROL_CHARACTER.search(description)
    ):
        raise ModelError("distribution description is invalid")
    licenses_value = metadata["licenses"]
    if not isinstance(licenses_value, list) or not 1 <= len(licenses_value) <= 8:
        raise ModelError("distribution licenses are invalid")
    licenses = tuple(licenses_value)
    if any(not isinstance(item, str) or not LICENSE.fullmatch(item) for item in licenses):
        raise ModelError("distribution license identifier is invalid")
    require_unique(list(licenses), "distribution license")
    targets_value = value["targets"]
    if not isinstance(targets_value, dict) or set(targets_value) != set(TARGET_IDS):
        raise ModelError("distribution targets must be exactly arch, debian, and rpm")
    targets = {target_id: _target(target_id, targets_value[target_id]) for target_id in TARGET_IDS}
    runtime = load_linux_runtime(root)
    if runtime.product.package_name in {
        conflict for target in targets.values() for conflict in target.conflicts
    }:
        raise ModelError("distribution package cannot conflict with itself")
    return DistributionCatalog(
        source_sha256=sha256_file(path),
        description=description,
        licenses=licenses,
        targets=targets,
    )
