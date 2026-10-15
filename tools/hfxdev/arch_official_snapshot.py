# SPDX-License-Identifier: GPL-2.0-only

"""Official Arch package snapshot and the OpenRazer provider-alignment gate.

The four-output topology ships an OpenRazer driver output that provides and
conflicts with the official `openrazer-driver-dkms`. An unversioned daemon
dependency can resolve to that provider, but resolution alone is no
compatibility proof. Before a candidate is published, the official `extra`,
`extra-testing`, and `extra-staging` repositories must be observed for that
candidate, every official package built from the provider's package base must
carry exactly the transformed upstream version, the transformed commit must be
the one that version is tagged at, every official dependency on a provided
name must accept the provided version, and a physical application receipt
must exist for every official userspace package at its exact version.

The snapshot is imported offline from pacman sync databases the user already
holds; nothing here contacts the network. A repository that was not supplied
is recorded as unobserved, and an unobserved repository fails the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from typing import Any, Mapping

from .arch_split import ArchSplit, SplitOutput, load_arch_split, release_key
from .model import ModelError, load_json, sha256_file
from .openrazer_native_route import load_native_deployment
from .openrazer_native_route import load_native_transform
from .openrazer_version_matrix import load_openrazer_version_matrix


SCHEMA = "hyperflux-arch-official-snapshot-v1"
SCHEMA_PATH = "../schemas/arch-official-snapshot.schema.json"
AUTHORITY = PurePosixPath("packaging/arch-official-snapshot.json")
RECEIPT_SCHEMA = "hyperflux-openrazer-app-behavior-receipt-v1"
REPOSITORIES = ("extra", "extra-testing", "extra-staging")
PROVIDER_OUTPUT = "openrazer-driver"
PACKAGE_NAME = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]{0,127}$")
DEPENDENCY = re.compile(r"^([a-z0-9@_+][a-z0-9@._+-]*)(?:(<=|>=|=|<|>)([^\s<>=]+))?(?::\s.*)?$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^(?:[0-9]+:)?[A-Za-z0-9._+~]+-[0-9]+(?:\.[0-9]+)?$")
RULE = (
    "Refresh at every candidate from the official extra, extra-testing and "
    "extra-staging sync databases; an unobserved repository fails the gate."
)
PACKAGE_KEYS = {
    "name",
    "base",
    "version",
    "filename",
    "sha256",
    "build_date",
    "depends",
    "provides",
    "conflicts",
}
DESC_FIELDS = {
    "NAME": "name",
    "BASE": "base",
    "VERSION": "version",
    "FILENAME": "filename",
    "SHA256SUM": "sha256",
    "BUILDDATE": "build_date",
    "DEPENDS": "depends",
    "PROVIDES": "provides",
    "CONFLICTS": "conflicts",
}
LIST_FIELDS = {"depends", "provides", "conflicts"}


# --- pacman version ordering -------------------------------------------------


def _rpmvercmp(one: str, two: str) -> int:
    """libalpm's `rpmvercmp` for one version segment without epoch or release."""

    if one == two:
        return 0
    i = j = 0
    while i < len(one) and j < len(two):
        start_i, start_j = i, j
        while i < len(one) and not one[i].isalnum():
            i += 1
        while j < len(two) and not two[j].isalnum():
            j += 1
        if i >= len(one) or j >= len(two):
            break
        if i - start_i != j - start_j:
            return -1 if i - start_i < j - start_j else 1
        numeric = one[i].isdigit()
        test = str.isdigit if numeric else str.isalpha
        end_i, end_j = i, j
        while end_i < len(one) and test(one[end_i]):
            end_i += 1
        while end_j < len(two) and test(two[end_j]):
            end_j += 1
        left, right = one[i:end_i], two[j:end_j]
        if not right:
            return 1 if numeric else -1
        if numeric:
            left, right = left.lstrip("0"), right.lstrip("0")
            if len(left) != len(right):
                return 1 if len(left) > len(right) else -1
        if left != right:
            return 1 if left > right else -1
        i, j = end_i, end_j
    if i >= len(one) and j >= len(two):
        return 0
    if (i >= len(one) and not two[j].isalpha()) or (i < len(one) and one[i].isalpha()):
        return -1
    return 1


def _split_version(value: str) -> tuple[str, str, str | None]:
    epoch, separator, rest = value.partition(":")
    if not separator:
        epoch, rest = "0", value
    version, separator, release = rest.rpartition("-")
    if not separator:
        return epoch or "0", rest, None
    return epoch or "0", version, release


def vercmp(left: str, right: str) -> int:
    """pacman's `vercmp`: epoch, then version, then release when both have one."""

    if left == right:
        return 0
    left_epoch, left_version, left_release = _split_version(left)
    right_epoch, right_version, right_release = _split_version(right)
    result = _rpmvercmp(left_epoch, right_epoch)
    if result == 0:
        result = _rpmvercmp(left_version, right_version)
    if result == 0 and left_release is not None and right_release is not None:
        result = _rpmvercmp(left_release, right_release)
    return result


def satisfies(provided: str, operator: str | None, required: str | None) -> bool:
    if operator is None:
        return True
    assert required is not None
    order = vercmp(provided, required)
    return {
        "=": order == 0,
        ">=": order >= 0,
        "<=": order <= 0,
        ">": order > 0,
        "<": order < 0,
    }[operator]


def upstream_version(version: str) -> str:
    """The upstream part of an Arch `[epoch:]version-release` string."""

    return _split_version(version)[1]


# --- snapshot import ----------------------------------------------------------


def _parse_desc(text: str) -> dict[str, Any]:
    record: dict[str, Any] = {}
    key: str | None = None
    values: list[str] = []
    for line in [*text.splitlines(), ""]:
        if key is None:
            match = re.fullmatch(r"%([A-Z0-9]+)%", line)
            if match:
                key, values = match.group(1), []
            continue
        if line:
            values.append(line)
            continue
        if key in DESC_FIELDS:
            field = DESC_FIELDS[key]
            record[field] = sorted(values) if field in LIST_FIELDS else (values[0] if values else "")
        key = None
    for field in LIST_FIELDS:
        record.setdefault(field, [])
    return record


def watched_packages(split: ArchSplit) -> tuple[str, ...]:
    """Official packages whose versions the four-output topology depends on."""

    names: set[str] = set()
    for output in split.outputs:
        for relation in (*output.provides, *output.conflicts):
            names.add(_dependency(relation)[0])
        for optional in output.optional_dependencies:
            names.add(optional.package)
    # Project-owned relation names (sibling outputs, retired packages, ABI
    # capabilities) are never official packages.
    return tuple(sorted(name for name in names if not name.startswith("hyperflux")))


def read_sync_database(path: Path, names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Read selected package descriptors from one pacman sync database."""

    wanted = set(names)
    packages: list[dict[str, Any]] = []
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive:
                if not member.isfile() or not member.name.endswith("/desc"):
                    continue
                directory = member.name[: -len("/desc")]
                if "/" in directory or not any(directory.startswith(f"{name}-") for name in wanted):
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                record = _parse_desc(stream.read(1 << 20).decode("utf-8", "replace"))
                if record.get("name") not in wanted:
                    continue
                record["build_date"] = int(record.get("build_date") or 0)
                packages.append({key: record.get(key) for key in sorted(PACKAGE_KEYS)})
    except (OSError, tarfile.TarError, ValueError) as error:
        raise ModelError(f"cannot read sync database {path.name}: {error}") from error
    names_seen = [item["name"] for item in packages]
    if len(set(names_seen)) != len(names_seen):
        raise ModelError(f"sync database {path.name} lists one package twice")
    return sorted(packages, key=lambda item: item["name"])


def import_snapshot(
    root: Path,
    databases: Mapping[str, Path],
    *,
    candidate: str,
    split: ArchSplit | None = None,
) -> dict[str, Any]:
    """Build the snapshot authority from local sync databases, offline."""

    unknown = set(databases) - set(REPOSITORIES)
    if unknown:
        raise ModelError(f"not an official repository: {', '.join(sorted(unknown))}")
    release_key(candidate)
    split = split or load_arch_split(root)
    names = watched_packages(split)
    existing: list[dict[str, Any]] = []
    authority = root / AUTHORITY
    if authority.is_file():
        existing = list(load_snapshot(root)["app_behavior_receipts"])
    repositories = []
    for name in REPOSITORIES:
        path = databases.get(name)
        if path is None:
            repositories.append({"name": name, "state": "unobserved"})
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        repositories.append(
            {
                "name": name,
                "state": "observed",
                "database_sha256": sha256_file(path),
                "database_modified": modified.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "packages": read_sync_database(path, names),
            }
        )
    value = {
        "$schema": SCHEMA_PATH,
        "schema": SCHEMA,
        "candidate": candidate,
        "rule": RULE,
        "watched_packages": list(names),
        "repositories": repositories,
        "app_behavior_receipts": existing,
    }
    validate_snapshot(value)
    return value


def write_snapshot(root: Path, value: dict[str, Any]) -> Path:
    validate_snapshot(value)
    path = root / AUTHORITY
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


# --- snapshot validation ------------------------------------------------------


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ModelError(f"{label} has unexpected or missing fields")
    return value


def _names(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ModelError(f"{label} must be a list of text")
    if value != sorted(value) or len(set(value)) != len(value):
        raise ModelError(f"{label} must be unique and sorted")
    return value


def validate_snapshot(value: Any) -> dict[str, Any]:
    _exact(
        value,
        {"$schema", "schema", "candidate", "rule", "watched_packages", "repositories", "app_behavior_receipts"},
        "official Arch snapshot",
    )
    if value["$schema"] != SCHEMA_PATH or value["schema"] != SCHEMA:
        raise ModelError("unsupported official Arch snapshot schema")
    if not isinstance(value["candidate"], str):
        raise ModelError("official Arch snapshot candidate must be text")
    release_key(value["candidate"])
    if value["rule"] != RULE:
        raise ModelError("official Arch snapshot rule is not canonical")
    watched = _names(value["watched_packages"], "official Arch snapshot watched packages")
    repositories = value["repositories"]
    if not isinstance(repositories, list) or [item.get("name") if isinstance(item, dict) else None for item in repositories] != list(REPOSITORIES):
        raise ModelError("official Arch snapshot must list extra, extra-testing and extra-staging in order")
    for repository in repositories:
        label = f"official Arch snapshot {repository['name']}"
        if repository.get("state") == "unobserved":
            _exact(repository, {"name", "state"}, label)
            continue
        _exact(repository, {"name", "state", "database_sha256", "database_modified", "packages"}, label)
        if repository["state"] != "observed" or not DIGEST.fullmatch(str(repository["database_sha256"])):
            raise ModelError(f"{label}: invalid observation")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(repository["database_modified"])):
            raise ModelError(f"{label}: invalid database time")
        packages = repository["packages"]
        if not isinstance(packages, list):
            raise ModelError(f"{label}: packages must be a list")
        seen = []
        for package in packages:
            _exact(package, PACKAGE_KEYS, f"{label} package")
            if (
                package["name"] not in watched
                or not PACKAGE_NAME.fullmatch(package["base"] or "")
                or not VERSION.fullmatch(package["version"])
                or not DIGEST.fullmatch(package["sha256"])
                or not isinstance(package["build_date"], int)
                or not isinstance(package["filename"], str)
            ):
                raise ModelError(f"{label}: invalid package {package.get('name')}")
            for field in LIST_FIELDS:
                for item in _names(package[field], f"{label} {package['name']} {field}"):
                    _dependency(item)
            seen.append(package["name"])
        _names(seen, f"{label} package names")
    receipts = value["app_behavior_receipts"]
    if not isinstance(receipts, list):
        raise ModelError("official Arch snapshot receipts must be a list")
    keys = []
    for receipt in receipts:
        _exact(receipt, {"package", "version", "path", "sha256"}, "app behaviour receipt")
        path = PurePosixPath(str(receipt["path"]))
        if path.is_absolute() or ".." in path.parts or not DIGEST.fullmatch(str(receipt["sha256"])):
            raise ModelError("app behaviour receipt must be a repository path with a digest")
        keys.append(f"{receipt['package']}={receipt['version']}")
    _names(keys, "app behaviour receipts")
    return value


def load_snapshot(root: Path) -> dict[str, Any]:
    return validate_snapshot(load_json(root / AUTHORITY))


def _dependency(value: str) -> tuple[str, str | None, str | None]:
    match = DEPENDENCY.fullmatch(value)
    if match is None:
        raise ModelError(f"unsupported package relation {value!r}")
    return match.group(1), match.group(2), match.group(3)


# --- provider gate ------------------------------------------------------------


@dataclass(frozen=True)
class ProviderAlignment:
    candidate: str
    provided: tuple[tuple[str, str], ...]
    transformed_commit: str
    official_versions: tuple[tuple[str, str, str], ...]
    problems: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.problems

    def as_json(self) -> str:
        return json.dumps(
            {
                "candidate": self.candidate,
                "passed": self.passed,
                "provided": [f"{name}={version}" for name, version in self.provided],
                "transformed_commit": self.transformed_commit,
                "official_versions": [
                    {"repository": repository, "package": package, "version": version}
                    for repository, package, version in self.official_versions
                ],
                "problems": list(self.problems),
            },
            indent=2,
        ) + "\n"


def _provider_output(split: ArchSplit) -> SplitOutput:
    return split.output(PROVIDER_OUTPUT)


def _receipt_problem(
    root: Path, receipts: list[dict[str, Any]], package: str, version: str
) -> str | None:
    matches = [item for item in receipts if item["package"] == package and item["version"] == version]
    if not matches:
        return f"no physical application receipt for official {package} {version}"
    receipt = matches[0]
    path = root / receipt["path"]
    if not path.is_file() or path.is_symlink():
        return f"application receipt {receipt['path']} is missing"
    if sha256_file(path) != receipt["sha256"]:
        return f"application receipt {receipt['path']} differs from its digest"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"application receipt {receipt['path']} is not JSON"
    if (
        not isinstance(value, dict)
        or value.get("schema") != RECEIPT_SCHEMA
        or value.get("package") != package
        or value.get("version") != version
        or value.get("result") != "passed"
    ):
        return f"application receipt {receipt['path']} does not prove {package} {version}"
    return None


def evaluate_provider_alignment(
    root: Path,
    *,
    split: ArchSplit | None = None,
    snapshot: dict[str, Any] | None = None,
    receipt_root: Path | None = None,
) -> ProviderAlignment:
    """Fail-closed alignment of the OpenRazer provider with official Arch."""

    split = split or load_arch_split(root)
    receipt_root = receipt_root or root
    problems: list[str] = []
    try:
        snapshot = validate_snapshot(snapshot if snapshot is not None else load_snapshot(root))
    except ModelError as error:
        snapshot = None
        problems.append(f"official snapshot is unusable: {error}")
    output = _provider_output(split)
    provided = tuple(
        (name, version or "")
        for name, operator, version in map(_dependency, output.provides)
        if operator == "="
    )
    deployment = load_native_deployment(root)
    transform = load_native_transform(root)
    version = deployment.driver.dkms_version
    commit = transform["upstream"]["commit"]
    official: list[tuple[str, str, str]] = []
    for name, value in provided:
        if value != version:
            problems.append(f"{name}={value} differs from the transformed driver {version}")
    matrix = load_openrazer_version_matrix(root)
    tagged = {case["display_version"]: case["commit"] for case in matrix["cases"]}
    if tagged.get(version) != commit:
        problems.append(f"transformed commit {commit} is not the tagged OpenRazer {version}")
    if snapshot is None:
        return ProviderAlignment(split.full_version, provided, commit, (), tuple(problems))
    if snapshot["candidate"] != split.full_version:
        problems.append(
            f"official snapshot was taken for {snapshot['candidate']}, not candidate {split.full_version}"
        )
    provided_names = {name for name, _ in provided}
    bases: set[str] = set()
    for repository in snapshot["repositories"]:
        if repository["state"] != "observed":
            problems.append(f"official {repository['name']} was not observed for this candidate")
            continue
        for package in repository["packages"]:
            if package["name"] in provided_names:
                bases.add(package["base"])
    extra = snapshot["repositories"][0]
    if extra["state"] == "observed" and not any(
        package["name"] in provided_names for package in extra["packages"]
    ):
        problems.append("official extra does not carry the provided OpenRazer driver package")
    for repository in snapshot["repositories"]:
        if repository["state"] != "observed":
            continue
        for package in repository["packages"]:
            if package["base"] not in bases:
                continue
            official.append((repository["name"], package["name"], package["version"]))
            if upstream_version(package["version"]) != version:
                problems.append(
                    f"official {repository['name']} {package['name']} {package['version']} "
                    f"differs from the transformed driver {version}"
                )
            for relation in package["depends"]:
                name, operator, required = _dependency(relation)
                if name in provided_names and not satisfies(version, operator, required):
                    problems.append(
                        f"official {package['name']} requires {relation}, which the provider {version} does not satisfy"
                    )
            if package["name"] not in provided_names:
                problem = _receipt_problem(
                    receipt_root, snapshot["app_behavior_receipts"], package["name"], package["version"]
                )
                if problem is not None:
                    problems.append(problem)
    for name in sorted(provided_names & set(snapshot["watched_packages"])):
        if name not in {_dependency(item)[0] for item in output.conflicts}:
            problems.append(f"provider output does not conflict with {name}")
    return ProviderAlignment(
        split.full_version,
        provided,
        commit,
        tuple(sorted(set(official))),
        tuple(dict.fromkeys(problems)),
    )
