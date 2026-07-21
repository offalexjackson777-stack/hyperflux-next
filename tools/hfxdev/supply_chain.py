# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from pathlib import Path, PurePosixPath
import tomllib
from typing import Any

from .integrations import load_integration_catalog
from .model import ModelError, load_json, require_unique, sha256_file


ROOT_KEYS = {
    "$schema",
    "schema",
    "inventory_created",
    "repository_license",
    "allowed_license_expressions",
    "rust_lock",
    "rust_registry_packages",
    "python_projects",
    "python_external_packages",
    "vendored_manifests",
    "upstream_catalog",
    "build_policy",
}
RUST_KEYS = {"name", "version", "license_expression"}
PYTHON_PROJECT_KEYS = {"path", "name", "license_expression"}
PYTHON_PACKAGE_KEYS = {"name", "specifier", "license_expression", "scope"}
BUILD_POLICY_KEYS = {
    "network_access",
    "locked_rust_dependencies",
    "isolated_python_dependency_resolution",
    "dirty_upstream_sources",
}
CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(.*)$")
SPDX_EXPRESSION = re.compile(r"^[A-Za-z0-9.+() -]+$")


@dataclass(frozen=True)
class RustPackage:
    name: str
    version: str
    license_expression: str
    checksum: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class WorkspacePackage:
    name: str
    version: str
    license_expression: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class PythonProject:
    path: str
    name: str
    license_expression: str


@dataclass(frozen=True)
class PythonPackage:
    name: str
    specifier: str
    license_expression: str
    scope: str


@dataclass(frozen=True)
class VendoredPackage:
    name: str
    version: str
    license_expression: str
    repository: str
    path: str
    sha256: str


@dataclass(frozen=True)
class UpstreamPackage:
    id: str
    name: str
    version: str
    commit: str
    license_expression: str
    repository: str


@dataclass(frozen=True)
class DependencyInventory:
    inventory_created: str
    repository_license: str
    allowed_licenses: tuple[str, ...]
    workspace_version: str
    workspace_packages: tuple[WorkspacePackage, ...]
    rust_packages: tuple[RustPackage, ...]
    python_projects: tuple[PythonProject, ...]
    python_packages: tuple[PythonPackage, ...]
    vendored_packages: tuple[VendoredPackage, ...]
    upstream_packages: tuple[UpstreamPackage, ...]
    authority_sha256: str


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ModelError(f"{label}: missing or unknown fields")
    return value


def _strings(value: Any, label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ModelError(f"{label}: must be a string array")
    if nonempty and not value:
        raise ModelError(f"{label}: must not be empty")
    require_unique(value, label)
    return tuple(value)


def _path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str):
        raise ModelError(f"{label}: path must be a string")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise ModelError(f"{label}: path escapes the repository")
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise ModelError(f"{label}: file does not exist: {value}")
    return path


def _license(value: Any, allowed: set[str], label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 160
        or not SPDX_EXPRESSION.fullmatch(value)
        or value not in allowed
    ):
        raise ModelError(f"{label}: license expression is not admitted by policy")
    return value


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _requirement(value: Any, label: str) -> tuple[str, str]:
    if not isinstance(value, str) or ";" in value or "[" in value or "@" in value:
        raise ModelError(f"{label}: requirement must be one bounded index requirement")
    match = REQUIREMENT.fullmatch(value)
    if match is None:
        raise ModelError(f"{label}: malformed requirement")
    name, specifier = match.groups()
    if any(character.isspace() for character in specifier) or len(specifier) > 128:
        raise ModelError(f"{label}: requirement specifier is not canonical")
    return _normalized_name(name), specifier


def _toml(path: Path, label: str) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ModelError(f"{label}: {error}") from error
    if not isinstance(value, dict):
        raise ModelError(f"{label}: top-level TOML value must be a table")
    return value


def _cargo_packages(
    root: Path,
    lock_path: Path,
    catalog: tuple[tuple[str, str, str], ...],
    allowed: set[str],
) -> tuple[str, tuple[WorkspacePackage, ...], tuple[RustPackage, ...]]:
    lock = _toml(lock_path, "Cargo lock")
    packages = lock.get("package")
    if lock.get("version") != 4 or not isinstance(packages, list):
        raise ModelError("Cargo lock must use format version 4 and contain packages")
    root_manifest = _toml(root / "Cargo.toml", "Cargo workspace")
    workspace_policy = root_manifest.get("workspace", {}).get("package", {})
    workspace_version = workspace_policy.get("version")
    workspace_license = workspace_policy.get("license")
    if not isinstance(workspace_version, str) or not isinstance(workspace_license, str):
        raise ModelError("Cargo workspace package version and license must be canonical")
    workspace_license = _license(workspace_license, allowed, "Cargo workspace")

    registry: list[RustPackage] = []
    workspace: list[WorkspacePackage] = []
    seen_keys: list[str] = []
    for index, item in enumerate(packages):
        if not isinstance(item, dict):
            raise ModelError(f"Cargo package {index}: must be a table")
        name = item.get("name")
        version = item.get("version")
        dependencies = item.get("dependencies", [])
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(dependencies, list)
            or not all(isinstance(dependency, str) for dependency in dependencies)
        ):
            raise ModelError(f"Cargo package {index}: malformed identity or dependencies")
        key = f"{name}@{version}"
        seen_keys.append(key)
        source = item.get("source")
        if source is None:
            workspace.append(
                WorkspacePackage(
                    name=name,
                    version=version,
                    license_expression=workspace_license,
                    dependencies=tuple(dependencies),
                )
            )
            continue
        checksum = item.get("checksum")
        if (
            not isinstance(source, str)
            or not source.startswith("registry+")
            or not isinstance(checksum, str)
            or not CHECKSUM.fullmatch(checksum)
        ):
            raise ModelError(f"Cargo package {key}: only checksummed registry sources are admitted")
        registry.append(
            RustPackage(
                name=name,
                version=version,
                license_expression="",
                checksum=checksum,
                dependencies=tuple(dependencies),
            )
        )
    require_unique(seen_keys, "Cargo package identity")

    declared = {(name, version): license_expression for name, version, license_expression in catalog}
    actual = {(package.name, package.version) for package in registry}
    if set(declared) != actual:
        missing = sorted(actual - set(declared))
        stale = sorted(set(declared) - actual)
        raise ModelError(
            "Rust dependency policy differs from Cargo.lock: "
            f"missing={missing or 'none'} stale={stale or 'none'}"
        )
    registry = [
        RustPackage(
            name=package.name,
            version=package.version,
            license_expression=_license(
                declared[(package.name, package.version)],
                allowed,
                f"Rust package {package.name}@{package.version}",
            ),
            checksum=package.checksum,
            dependencies=package.dependencies,
        )
        for package in registry
    ]
    return (
        workspace_version,
        tuple(sorted(workspace, key=lambda package: (package.name, package.version))),
        tuple(sorted(registry, key=lambda package: (package.name, package.version))),
    )


def _python_inventory(
    root: Path,
    projects_value: Any,
    packages_value: Any,
    allowed: set[str],
) -> tuple[tuple[PythonProject, ...], tuple[PythonPackage, ...]]:
    if not isinstance(projects_value, list) or not projects_value:
        raise ModelError("Python project policy must contain projects")
    projects: list[PythonProject] = []
    project_requirements: list[tuple[str, str, str]] = []
    internal_names: set[str] = set()
    parsed_projects: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, raw in enumerate(projects_value):
        item = _exact(raw, PYTHON_PROJECT_KEYS, f"Python project {index}")
        path = _path(root, item["path"], f"Python project {index}")
        pyproject = _toml(path, f"Python project {item['path']}")
        project = pyproject.get("project")
        build = pyproject.get("build-system")
        if not isinstance(project, dict) or not isinstance(build, dict):
            raise ModelError(f"Python project {item['path']}: missing project or build-system")
        name = item["name"]
        if not isinstance(name, str) or project.get("name") != name:
            raise ModelError(f"Python project {item['path']}: project name drifted")
        license_expression = _license(
            item["license_expression"], allowed, f"Python project {name}"
        )
        if project.get("license") != license_expression:
            raise ModelError(f"Python project {name}: license differs from pyproject.toml")
        projects.append(
            PythonProject(
                path=item["path"],
                name=name,
                license_expression=license_expression,
            )
        )
        internal_names.add(_normalized_name(name))
        parsed_projects.append((project, build))
    require_unique([project.path for project in projects], "Python project path")
    require_unique([_normalized_name(project.name) for project in projects], "Python project name")

    if not isinstance(packages_value, list):
        raise ModelError("Python external package policy must be an array")
    external: list[PythonPackage] = []
    for index, raw in enumerate(packages_value):
        item = _exact(raw, PYTHON_PACKAGE_KEYS, f"Python package {index}")
        name = item["name"]
        specifier = item["specifier"]
        scope = item["scope"]
        if not isinstance(name, str) or not isinstance(specifier, str):
            raise ModelError(f"Python package {index}: malformed identity")
        if scope not in {"build-system", "optional-runtime"}:
            raise ModelError(f"Python package {name}: invalid scope")
        normalized, parsed_specifier = _requirement(
            f"{name}{specifier}", f"Python package {name}"
        )
        external.append(
            PythonPackage(
                name=name,
                specifier=parsed_specifier,
                license_expression=_license(
                    item["license_expression"], allowed, f"Python package {name}"
                ),
                scope=scope,
            )
        )
        project_requirements.append((normalized, parsed_specifier, scope))
    require_unique(
        [f"{_normalized_name(package.name)}:{package.scope}" for package in external],
        "Python external package",
    )
    declared = set(project_requirements)

    observed: set[tuple[str, str, str]] = set()
    for project, build in parsed_projects:
        build_requires = build.get("requires")
        runtime_requires = project.get("dependencies", [])
        if not isinstance(build_requires, list) or not isinstance(runtime_requires, list):
            raise ModelError("Python project requirements must be arrays")
        for requirement in build_requires:
            name, specifier = _requirement(requirement, "Python build requirement")
            if name not in internal_names:
                observed.add((name, specifier, "build-system"))
        for requirement in runtime_requires:
            name, specifier = _requirement(requirement, "Python runtime requirement")
            if name not in internal_names:
                observed.add((name, specifier, "optional-runtime"))
    if observed != declared:
        raise ModelError(
            "Python dependency policy differs from pyproject.toml files: "
            f"missing={sorted(observed - declared) or 'none'} "
            f"stale={sorted(declared - observed) or 'none'}"
        )
    return (
        tuple(sorted(projects, key=lambda project: project.name)),
        tuple(sorted(external, key=lambda package: (_normalized_name(package.name), package.scope))),
    )


def _vendored_inventory(
    root: Path, values: Any, allowed: set[str]
) -> tuple[VendoredPackage, ...]:
    paths = _strings(values, "vendored manifest path", nonempty=True)
    packages: list[VendoredPackage] = []
    for value in paths:
        manifest_path = _path(root, value, "vendored manifest")
        manifest = load_json(manifest_path)
        dependencies = manifest.get("dependencies")
        if not isinstance(dependencies, list):
            raise ModelError(f"vendored manifest {value}: dependencies must be an array")
        for index, item in enumerate(dependencies):
            if not isinstance(item, dict):
                raise ModelError(f"vendored manifest {value} dependency {index}: malformed")
            required = {
                "id",
                "version",
                "license_expression",
                "source_repository",
                "source_via_repository",
                "source_via_commit",
                "path",
                "sha256",
            }
            if set(item) != required:
                raise ModelError(f"vendored manifest {value} dependency {index}: unknown fields")
            path = _path(root, item["path"], f"vendored dependency {item.get('id', index)}")
            digest = item["sha256"]
            if not isinstance(digest, str) or not CHECKSUM.fullmatch(digest) or sha256_file(path) != digest:
                raise ModelError(f"vendored dependency {item.get('id', index)}: digest mismatch")
            packages.append(
                VendoredPackage(
                    name=item["id"],
                    version=item["version"],
                    license_expression=_license(
                        item["license_expression"], allowed, f"vendored dependency {item['id']}"
                    ),
                    repository=item["source_repository"],
                    path=item["path"],
                    sha256=digest,
                )
            )
    require_unique([f"{package.name}@{package.version}" for package in packages], "vendored dependency")
    return tuple(sorted(packages, key=lambda package: (package.name, package.version)))


def _upstream_inventory(
    root: Path, catalog_value: Any, allowed: set[str]
) -> tuple[UpstreamPackage, ...]:
    catalog_path = _path(root, catalog_value, "upstream catalog")
    if catalog_path != (root / "integrations" / "catalog.json").resolve():
        raise ModelError("dependency policy must use the canonical integration catalog")
    catalog = load_integration_catalog(root)
    packages = tuple(
        UpstreamPackage(
            id=item["id"],
            name=item["name"],
            version=item["version"],
            commit=item["commit"],
            license_expression=_license(
                item["license_expression"], allowed, f"upstream {item['id']}"
            ),
            repository=item["repository"],
        )
        for item in catalog["upstreams"]
    )
    return tuple(sorted(packages, key=lambda package: package.id))


def load_dependency_inventory(root: Path) -> DependencyInventory:
    root = root.resolve()
    policy_path = root / "assurance" / "dependencies.json"
    value = _exact(load_json(policy_path), ROOT_KEYS, "dependency policy")
    if value["schema"] != "hyperflux-dependency-policy-v1":
        raise ModelError("unsupported dependency policy schema")
    created = value["inventory_created"]
    if not isinstance(created, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", created
    ):
        raise ModelError("dependency inventory timestamp is not canonical UTC")
    allowed_values = _strings(
        value["allowed_license_expressions"], "allowed license expression", nonempty=True
    )
    if tuple(sorted(allowed_values)) != allowed_values:
        raise ModelError("allowed license expressions must be sorted")
    allowed = set(allowed_values)
    repository_license = _license(value["repository_license"], allowed, "repository")
    policy = _exact(value["build_policy"], BUILD_POLICY_KEYS, "dependency build policy")
    if policy != {
        "network_access": False,
        "locked_rust_dependencies": True,
        "isolated_python_dependency_resolution": False,
        "dirty_upstream_sources": "reject",
    }:
        raise ModelError("dependency build policy must remain offline, locked, and fail closed")

    rust_values = value["rust_registry_packages"]
    if not isinstance(rust_values, list) or not rust_values:
        raise ModelError("Rust dependency policy must contain registry packages")
    rust_catalog: list[tuple[str, str, str]] = []
    for index, raw in enumerate(rust_values):
        item = _exact(raw, RUST_KEYS, f"Rust dependency {index}")
        if not isinstance(item["name"], str) or not isinstance(item["version"], str):
            raise ModelError(f"Rust dependency {index}: malformed identity")
        rust_catalog.append(
            (item["name"], item["version"], item["license_expression"])
        )
    require_unique([f"{name}@{version}" for name, version, _ in rust_catalog], "Rust dependency")
    lock_path = _path(root, value["rust_lock"], "Rust lock")
    workspace_version, workspace_packages, rust_packages = _cargo_packages(
        root, lock_path, tuple(rust_catalog), allowed
    )
    python_projects, python_packages = _python_inventory(
        root, value["python_projects"], value["python_external_packages"], allowed
    )
    vendored = _vendored_inventory(root, value["vendored_manifests"], allowed)
    upstreams = _upstream_inventory(root, value["upstream_catalog"], allowed)
    vendored_manifest_paths = tuple(
        root / manifest for manifest in value["vendored_manifests"]
    )
    authority_material = b"\0".join(
        bytes.fromhex(sha256_file(path))
        for path in (
            policy_path,
            lock_path,
            root / "Cargo.toml",
            root / "integrations" / "catalog.json",
            *(root / project.path for project in python_projects),
            *vendored_manifest_paths,
        )
    )
    authority_sha256 = hashlib.sha256(authority_material).hexdigest()
    return DependencyInventory(
        inventory_created=created,
        repository_license=repository_license,
        allowed_licenses=allowed_values,
        workspace_version=workspace_version,
        workspace_packages=workspace_packages,
        rust_packages=rust_packages,
        python_projects=python_projects,
        python_packages=python_packages,
        vendored_packages=vendored,
        upstream_packages=upstreams,
        authority_sha256=authority_sha256,
    )
