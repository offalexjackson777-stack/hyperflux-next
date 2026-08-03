# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import ModelError, load_json, require_unique, sha256_file


CATALOG_KEYS = {"$schema", "schema", "append_only", "records"}
RECORD_KEYS = {
    "id",
    "recorded_at",
    "release",
    "source",
    "artifacts",
    "installation",
    "kernel",
    "runtime",
    "behavior",
    "accepted_limitations",
    "evidence",
    "release_state",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MANIFEST_LINE_RE = re.compile(r"([0-9a-f]{64})  (/[^\n]+)")
REQUIRED_PACKAGES = {
    "hyperflux-next-linux",
    "hyperflux-next-full",
    "openrgb",
    "openrazer-daemon",
    "python-openrazer",
    "polychromatic",
}
REQUIRED_SERVICES = {
    "hyperflux-next-bridge.service",
    "hyperflux-next-openrazer-driver-preflight.service",
    "hyperflux-next-openrazer-relay.service",
    "hyperflux-next-client@1000.service",
    "openrazer-daemon.service",
}


@dataclass(frozen=True)
class InstalledBaseline:
    id: str
    source_revision: str
    package_version: str
    artifact_frozen: bool
    installed_verified: bool
    behavioral_baseline_accepted: bool
    public_release_authorized: bool


def _objects(value: Any, label: str, *, nonempty: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ModelError(f"{label}: must be an object array")
    if nonempty and not value:
        raise ModelError(f"{label}: must not be empty")
    return value


def _unique_field(items: list[dict[str, Any]], field: str, label: str) -> None:
    values = [item.get(field) for item in items]
    if not all(isinstance(value, str) and value for value in values):
        raise ModelError(f"{label}: every item must name {field}")
    require_unique(values, label)


def _repository_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str):
        raise ModelError(f"{label}: must be a repository-relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise ModelError(f"{label}: path escapes the repository")
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise ModelError(f"{label}: expected a regular non-symlink file")
    return path


def _validate_file_manifest(root: Path, value: Any, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "entries"}:
        raise ModelError(f"{label}: missing or unknown fields")
    path = _repository_path(root, value["path"], f"{label} path")
    digest = value["sha256"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ModelError(f"{label}: invalid sha256")
    if sha256_file(path) != digest:
        raise ModelError(f"{label}: sha256 does not match exact bytes")
    lines = path.read_text(encoding="utf-8").splitlines()
    if value["entries"] != len(lines) or not lines:
        raise ModelError(f"{label}: entry count does not match exact lines")
    paths: list[str] = []
    for index, line in enumerate(lines):
        match = MANIFEST_LINE_RE.fullmatch(line)
        if match is None:
            raise ModelError(f"{label}: malformed line {index + 1}")
        installed_path = match.group(2)
        if installed_path.startswith(("/home/", "/run/user/")):
            raise ModelError(f"{label}: private runtime path is forbidden")
        paths.append(installed_path)
    require_unique(paths, f"{label} installed paths")


def _validate_record(root: Path, raw: dict[str, Any], index: int) -> InstalledBaseline:
    label = f"installed baseline {index}"
    if set(raw) != RECORD_KEYS:
        raise ModelError(f"{label}: missing or unknown fields")
    record_id = raw["id"]
    if not isinstance(record_id, str) or not re.fullmatch(
        r"installed-baseline\.[a-z0-9.-]+", record_id
    ):
        raise ModelError(f"{label}: invalid id")

    source = raw["source"]
    if not isinstance(source, dict) or set(source) != {"revision", "tree"}:
        raise ModelError(f"{label}: invalid source identity")
    if not all(
        isinstance(source[field], str) and re.fullmatch(r"[0-9a-f]{40}", source[field])
        for field in ("revision", "tree")
    ):
        raise ModelError(f"{label}: source identity must contain exact Git object ids")

    release = raw["release"]
    if not isinstance(release, dict) or release.get("distribution") != "arch":
        raise ModelError(f"{label}: baseline distribution must be Arch")
    package_version = release.get("package_version")
    if not isinstance(package_version, str) or not package_version:
        raise ModelError(f"{label}: missing package version")

    artifacts = raw["artifacts"]
    if not isinstance(artifacts, dict):
        raise ModelError(f"{label}: artifacts must be an object")
    artifact_packages = _objects(artifacts.get("packages"), f"{label} artifacts", nonempty=True)
    _unique_field(artifact_packages, "name", f"{label} artifact packages")
    if {item["name"] for item in artifact_packages} != {
        "hyperflux-next-linux",
        "hyperflux-next-full",
    }:
        raise ModelError(f"{label}: exact Core and Full artifacts are required")
    if {item.get("version") for item in artifact_packages} != {package_version}:
        raise ModelError(f"{label}: artifact versions do not match installed release")

    installation = raw["installation"]
    if not isinstance(installation, dict):
        raise ModelError(f"{label}: installation must be an object")
    installed_packages = _objects(
        installation.get("packages"), f"{label} installed packages", nonempty=True
    )
    _unique_field(installed_packages, "name", f"{label} installed packages")
    if not REQUIRED_PACKAGES.issubset({item["name"] for item in installed_packages}):
        raise ModelError(f"{label}: stock application and HyperFlux package set is incomplete")
    _validate_file_manifest(root, installation.get("file_manifest"), f"{label} file manifest")

    kernel = raw["kernel"]
    if not isinstance(kernel, dict):
        raise ModelError(f"{label}: kernel must be an object")
    modules = _objects(kernel.get("modules"), f"{label} modules", nonempty=True)
    _unique_field(modules, "name", f"{label} modules")
    for module in modules:
        files = _objects(module.get("files"), f"{label} module files", nonempty=True)
        _unique_field(files, "kernel", f"{label} {module['name']} kernel builds")

    runtime = raw["runtime"]
    if not isinstance(runtime, dict):
        raise ModelError(f"{label}: runtime must be an object")
    if runtime.get("restoration_enabled") is not False:
        raise ModelError(f"{label}: accepted baseline must keep restoration disabled")
    doctor = runtime.get("doctor")
    if not isinstance(doctor, dict) or doctor.get("state") != "ready" or doctor.get("findings") != 0:
        raise ModelError(f"{label}: accepted baseline Doctor must be Ready without findings")
    services = _objects(runtime.get("services"), f"{label} services", nonempty=True)
    _unique_field(services, "unit", f"{label} services")
    if not REQUIRED_SERVICES.issubset({item["unit"] for item in services}):
        raise ModelError(f"{label}: required service boundaries are incomplete")
    routes = _objects(runtime.get("routes"), f"{label} routes", nonempty=True)
    _unique_field(routes, "profile_id", f"{label} routes")
    if any(route.get("state") != "kernel-started" for route in routes):
        raise ModelError(f"{label}: every recorded route must be kernel-started")
    applications = _objects(
        runtime.get("applications"), f"{label} applications", nonempty=True
    )
    _unique_field(applications, "name", f"{label} applications")
    if {item["name"] for item in applications} != {"OpenRGB", "OpenRazer", "Polychromatic"}:
        raise ModelError(f"{label}: exact stock application boundaries are required")

    behavior = _objects(raw["behavior"], f"{label} behavior", nonempty=True)
    _unique_field(behavior, "id", f"{label} behavior")
    limitations = _objects(
        raw["accepted_limitations"], f"{label} limitations", nonempty=True
    )
    _unique_field(limitations, "id", f"{label} limitations")
    evidence = _objects(raw["evidence"], f"{label} evidence", nonempty=True)
    _unique_field(evidence, "id", f"{label} evidence")

    state = raw["release_state"]
    expected_state = {
        "artifact_frozen": True,
        "installed_verified": True,
        "behavioral_baseline_accepted": True,
        "public_release_authorized": False,
    }
    if state != expected_state:
        raise ModelError(f"{label}: release-state interlock is not exact")

    serialized = str(raw)
    for forbidden in ("/home/", "/var/lib/hyperflux-next-live-migration/", "receiver_id"):
        if forbidden in serialized:
            raise ModelError(f"{label}: private evidence detail is forbidden: {forbidden}")

    return InstalledBaseline(
        id=record_id,
        source_revision=source["revision"],
        package_version=package_version,
        artifact_frozen=True,
        installed_verified=True,
        behavioral_baseline_accepted=True,
        public_release_authorized=False,
    )


def load_installed_baselines(root: Path) -> tuple[InstalledBaseline, ...]:
    value = load_json(root / "assurance" / "installed-baselines.json")
    if set(value) != CATALOG_KEYS:
        raise ModelError("installed baselines have missing or unknown top-level fields")
    if value["schema"] != "hyperflux-installed-baseline-evidence-v1":
        raise ModelError("unsupported installed-baseline schema")
    if value["append_only"] is not True:
        raise ModelError("installed-baseline catalog must be append-only")
    raw_records = _objects(value["records"], "installed baselines", nonempty=True)
    records = tuple(
        _validate_record(root, record, index)
        for index, record in enumerate(raw_records)
    )
    require_unique([record.id for record in records], "installed baseline ids")
    return records
