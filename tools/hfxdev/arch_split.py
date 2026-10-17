# SPDX-License-Identifier: GPL-2.0-only

"""Four-output Arch package topology shared by the signed repository and AUR.

The two-package products (`hyperflux-next-linux`, `hyperflux-next-full`) stay
the user-facing editions. This authority partitions each edition into the
outputs that own its files, so every channel derives one product contract:
the same package names, versions, sibling dependencies, relations, and file
ownership.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .distributions import (
    COMPONENT_IDS,
    FORBIDDEN_APPLICATION_RELATIONS,
    OPENRAZER_DRIVER_VERSION_TOKEN,
    DistributionCatalog,
    OptionalDependency,
    load_distribution_catalog,
)
from .install import InstallManifest, load_install_manifest
from .distribution_native import package_directory_modes
from .linux_runtime import load_linux_runtime
from .model import ModelError, load_json, require_unique, sha256_file
from .openrazer_native_route import load_native_deployment


SCHEMA = "hyperflux-arch-split-packages-v1"
SCHEMA_PATH = "../schemas/arch-split-packages.schema.json"
AUTHORITY = PurePosixPath("packaging/arch-split.json")
STOCK_OPENRAZER_DRIVER = "openrazer-driver-dkms"
TRANSFORMED_DRIVER_BUILD = "openrazer-native-driver"
OUTPUT_FIELDS = {
    "id",
    "package_name",
    "edition",
    "description",
    "licenses",
    "builds",
    "payloads",
    "dependency_roles",
    "sibling_dependencies",
    "dependencies",
    "optional_dependencies",
    "conflicts",
    "provides",
    "replaces",
}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
PACKAGE_NAME = re.compile(r"^[a-z][a-z0-9+_.-]{0,63}$")
PACKAGE_EXPRESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.() >=~-]{0,95}$")
RELEASE = re.compile(r"^([0-9]+(?:\.[0-9]+)*)-([1-9][0-9]*)$")
CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


# Build kinds that install source files rather than compiled machine code.
SOURCE_BUILD_KINDS = frozenset({"source-transform", "source-transform-file"})


@dataclass(frozen=True)
class RetiredRelation:
    output: str
    relation: str
    reason: str
    expires_before_release: str


@dataclass(frozen=True)
class SplitOutput:
    id: str
    package_name: str
    edition: str
    description: str
    licenses: tuple[str, ...]
    builds: tuple[str, ...]
    payloads: tuple[str, ...]
    dependency_roles: tuple[str, ...]
    sibling_dependencies: tuple[str, ...]
    external_dependencies: tuple[str, ...]
    dependencies: tuple[str, ...]
    optional_dependencies: tuple[OptionalDependency, ...]
    conflicts: tuple[str, ...]
    provides: tuple[str, ...]
    replaces: tuple[str, ...]
    backup: tuple[str, ...]
    file_destinations: tuple[str, ...]
    tree_destinations: tuple[str, ...]
    directory_modes: tuple[tuple[str, int], ...]
    # Whether any build compiles machine code. An output of sources, scripts
    # and data alone is architecture-independent on Debian and RPM targets.
    compiled: bool = True


@dataclass(frozen=True)
class ArchSplit:
    source_sha256: str
    pkgbase: str
    first_release: str
    version: str
    package_release: int
    architectures: tuple[str, ...]
    editions: dict[str, str]
    outputs: tuple[SplitOutput, ...]
    retired_relations: tuple[RetiredRelation, ...]
    licenses: tuple[str, ...]
    description: str
    homepage: str

    @property
    def full_version(self) -> str:
        return f"{self.version}-{self.package_release}"

    @property
    def active(self) -> bool:
        return release_key(self.full_version) >= release_key(self.first_release)

    def output(self, output_id: str) -> SplitOutput:
        for output in self.outputs:
            if output.id == output_id:
                return output
        raise ModelError(f"Arch split: unknown output {output_id}")

    def by_package(self, package_name: str) -> SplitOutput:
        for output in self.outputs:
            if output.package_name == package_name:
                return output
        raise ModelError(f"Arch split: unknown package {package_name}")

    def edition_output(self, edition: str) -> SplitOutput:
        try:
            return self.output(self.editions[edition])
        except KeyError as error:
            raise ModelError(f"Arch split: unknown edition {edition}") from error

    def closure(self, output_id: str) -> tuple[str, ...]:
        seen: list[str] = []
        pending = [output_id]
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.append(current)
            pending.extend(self.output(current).sibling_dependencies)
        return tuple(item.id for item in self.outputs if item.id in seen)

    def install_set(self, edition: str) -> tuple[str, ...]:
        """Package names a user installs for one edition, in output order."""

        return tuple(
            self.output(output_id).package_name
            for output_id in self.closure(self.edition_output(edition).id)
        )

    def owner_of(self, path: str) -> str:
        """Return the output that owns one installed path.

        Directories shared by several outputs (for example `/usr/lib`) have no
        single owner; callers pass regular file paths.
        """

        normalized = "/" + path.strip("/")
        owners = []
        for output in self.outputs:
            if normalized in output.file_destinations or any(
                normalized.startswith(tree + "/") for tree in output.tree_destinations
            ):
                owners.append(output.id)
        if len(owners) != 1:
            state = "no" if not owners else "several"
            raise ModelError(f"Arch split: {state} output owns {normalized}")
        return owners[0]


def release_key(value: str) -> tuple[tuple[int, ...], int]:
    match = RELEASE.fullmatch(value)
    if match is None:
        raise ModelError(f"Arch split: release {value!r} is not version-release")
    return tuple(int(part) for part in match.group(1).split(".")), int(match.group(2))


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


def _identifiers(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise ModelError(f"{label}: invalid identifier list")
    if any(not isinstance(item, str) or not IDENTIFIER.fullmatch(item) for item in value):
        raise ModelError(f"{label}: invalid identifier")
    require_unique(value, label)
    return tuple(value)


def _relations(value: Any, label: str, transformed_version: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 16:
        raise ModelError(f"{label}: invalid relation list")
    result = []
    for item in value:
        if not isinstance(item, str) or item.count(OPENRAZER_DRIVER_VERSION_TOKEN) > 1:
            raise ModelError(f"{label}: invalid relation")
        expanded = item.replace(OPENRAZER_DRIVER_VERSION_TOKEN, transformed_version)
        if "@" in expanded or not PACKAGE_EXPRESSION.fullmatch(expanded):
            raise ModelError(f"{label}: invalid relation {item!r}")
        result.append(expanded)
    require_unique(result, label)
    return tuple(result)


def relation_name(value: str) -> str:
    return re.split(r"[<>= ]", value, maxsplit=1)[0]


def _check_partition(
    outputs: list[dict[str, Any]], install: InstallManifest
) -> None:
    for edition in COMPONENT_IDS:
        expected_builds = {build.id for build in install.builds_for(edition)}
        expected_payloads = {
            payload.id for payload in install.payloads if payload.distribution_component == edition
        }
        assigned_builds: list[str] = []
        assigned_payloads: list[str] = []
        for output in outputs:
            if output["edition"] != edition:
                continue
            assigned_builds.extend(output["builds"])
            assigned_payloads.extend(output["payloads"])
        require_unique(assigned_builds, f"Arch split {edition} build assignment")
        require_unique(assigned_payloads, f"Arch split {edition} payload assignment")
        if set(assigned_builds) != expected_builds:
            raise ModelError(
                f"Arch split {edition} outputs must own exactly the edition builds: "
                f"missing {sorted(expected_builds - set(assigned_builds))}, "
                f"foreign {sorted(set(assigned_builds) - expected_builds)}"
            )
        if set(assigned_payloads) != expected_payloads:
            raise ModelError(
                f"Arch split {edition} outputs must own exactly the edition payloads: "
                f"missing {sorted(expected_payloads - set(assigned_payloads))}, "
                f"foreign {sorted(set(assigned_payloads) - expected_payloads)}"
            )


def _destinations(
    output: dict[str, Any], install: InstallManifest
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    files: list[str] = []
    trees: list[str] = []
    backup: list[str] = []
    for build_id in output["builds"]:
        build = install.build(build_id)
        if build.destination is None:
            raise ModelError(f"Arch split build {build_id}: destination is missing")
        if build.kind == "source-transform":
            trees.append(build.destination.rstrip("/"))
        else:
            files.append(build.destination)
    for item in install.files:
        if item.component in output["payloads"]:
            files.append(str(item.destination))
            if item.preserve:
                backup.append(str(item.destination).lstrip("/"))
    return tuple(sorted(files)), tuple(sorted(trees)), tuple(sorted(backup))


def _check_ownership(outputs: tuple[SplitOutput, ...]) -> None:
    owners: dict[str, str] = {}
    for output in outputs:
        for path in (*output.file_destinations, *output.tree_destinations):
            previous = owners.setdefault(path, output.id)
            if previous != output.id:
                raise ModelError(f"Arch split ownership overlap at {path}")
    for output in outputs:
        for tree in output.tree_destinations:
            for path, owner in owners.items():
                if owner != output.id and (
                    path.startswith(tree + "/") or tree.startswith(path + "/")
                ):
                    raise ModelError(f"Arch split ownership overlap under {tree}")


def _check_relations(
    split_outputs: tuple[SplitOutput, ...],
    retired: tuple[RetiredRelation, ...],
    catalog: DistributionCatalog,
    install: InstallManifest,
    transformed_version: str,
    full_version: str,
) -> None:
    target = catalog.targets["arch"]
    by_id = {output.id: output for output in split_outputs}
    driver_owner = [
        output for output in split_outputs if TRANSFORMED_DRIVER_BUILD in output.builds
    ]
    if len(driver_owner) != 1:
        raise ModelError("Arch split: exactly one output must own the transformed driver")
    driver = driver_owner[0]
    for output in split_outputs:
        names = {
            *map(relation_name, output.conflicts),
            *map(relation_name, output.provides),
            *map(relation_name, output.replaces),
        }
        forbidden = FORBIDDEN_APPLICATION_RELATIONS & names
        if forbidden:
            raise ModelError(
                f"Arch split {output.id}: application relations are forbidden: "
                f"{', '.join(sorted(forbidden))}"
            )
        if output.package_name in names:
            raise ModelError(f"Arch split {output.id}: package cannot relate to itself")
        if STOCK_OPENRAZER_DRIVER in map(relation_name, output.replaces):
            raise ModelError(
                f"Arch split {output.id}: the stock OpenRazer driver is an alternative, "
                "not an obsolete package; it may not be replaced"
            )
        if output is not driver and STOCK_OPENRAZER_DRIVER in names:
            raise ModelError(
                f"Arch split {output.id}: only the transformed-driver output may relate "
                "to the stock OpenRazer driver"
            )
        for provided in output.provides:
            if "=" not in provided or relation_name(provided) + "=" + transformed_version != provided:
                raise ModelError(
                    f"Arch split {output.id}: provided names must carry the exact "
                    "transformed source version"
                )
    expected_driver_provides = {
        f"{STOCK_OPENRAZER_DRIVER}={transformed_version}",
        *(
            item
            for item in target.component("full").provides
            if relation_name(item) != STOCK_OPENRAZER_DRIVER
        ),
    }
    if set(driver.provides) != expected_driver_provides:
        raise ModelError("Arch split: transformed-driver provides differ from the Full contract")
    if driver.conflicts != (STOCK_OPENRAZER_DRIVER,) or driver.replaces:
        raise ModelError(
            "Arch split: the transformed driver must conflict with, and not replace, "
            "the stock OpenRazer driver"
        )

    retired_by_key = {(item.output, item.relation): item for item in retired}
    require_unique(
        [f"{item.output}:{item.relation}" for item in retired],
        "Arch split retired relation",
    )
    used: set[tuple[str, str]] = set()
    for output in split_outputs:
        for replaced in output.replaces:
            record = retired_by_key.get((output.id, replaced))
            if record is None:
                raise ModelError(
                    f"Arch split {output.id}: replace {replaced} has no retired-relation record"
                )
            if "=" not in replaced or replaced not in output.conflicts:
                raise ModelError(
                    f"Arch split {output.id}: replace {replaced} must name one exact "
                    "retired version that the output also conflicts with"
                )
            if release_key(full_version) >= release_key(record.expires_before_release):
                raise ModelError(
                    f"Arch split {output.id}: retired relation {replaced} expired at "
                    f"{record.expires_before_release}; review or remove it"
                )
            used.add((output.id, replaced))
    if used != set(retired_by_key):
        raise ModelError("Arch split: retired-relation record has no matching replace")

    for edition in COMPONENT_IDS:
        component = target.component(edition)
        members = [output for output in split_outputs if output.edition == edition]
        sibling_component = {
            item
            for item in component.dependencies
            if relation_name(item) in {
                target.component(other).package_name for other in COMPONENT_IDS
            }
        }
        external_expected = set(component.dependencies) - sibling_component
        external_actual = {
            item
            for output in members
            for item in output.dependencies
            if relation_name(item) not in {split.package_name for split in split_outputs}
        }
        if external_actual != external_expected:
            raise ModelError(
                f"Arch split {edition}: external dependencies differ from the edition "
                f"contract: expected {sorted(external_expected)}, found {sorted(external_actual)}"
            )
    linux_conflicts = set(target.component("linux").conflicts)
    if {item for output in split_outputs if output.edition == "linux" for item in output.conflicts} != linux_conflicts:
        raise ModelError("Arch split: Core outputs must keep the receiver-driver conflicts")
    _ = by_id, install


def _check_closures(split: ArchSplit) -> None:
    for output in split.outputs:
        for sibling in output.sibling_dependencies:
            target = split.output(sibling)
            if output.edition == "linux" and target.edition != "linux":
                raise ModelError(
                    f"Arch split {output.id}: Core outputs may not depend on Full outputs"
                )
        visiting: set[str] = set()

        def visit(current: str, path: tuple[str, ...]) -> None:
            if current in path:
                raise ModelError(f"Arch split: sibling dependency cycle at {current}")
            for sibling in split.output(current).sibling_dependencies:
                visit(sibling, (*path, current))

        visit(output.id, ())
        visiting.clear()
    core = set(split.closure(split.edition_output("linux").id))
    full = set(split.closure(split.edition_output("full").id))
    if core != {output.id for output in split.outputs if output.edition == "linux"}:
        raise ModelError("Arch split: the Core request must resolve exactly the Core outputs")
    if full != {output.id for output in split.outputs}:
        raise ModelError("Arch split: the Full request must resolve every output")


def load_arch_split(
    root: Path,
    *,
    release: tuple[str, int] | None = None,
) -> ArchSplit:
    """Load and verify the four-output topology.

    `release` overrides the runtime product version and package release. It
    exists for qualification of a candidate before `runtime/linux.json` moves.
    """

    root = root.resolve()
    path = root / AUTHORITY
    value = load_json(path)
    _exact(
        value,
        {
            "$schema",
            "schema",
            "pkgbase",
            "first_release",
            "first_release_rule",
            "architectures",
            "editions",
            "outputs",
            "retired_relations",
        },
        "Arch split",
    )
    if value["$schema"] != SCHEMA_PATH or value["schema"] != SCHEMA:
        raise ModelError("unsupported Arch split authority")
    pkgbase = value["pkgbase"]
    if not isinstance(pkgbase, str) or not PACKAGE_NAME.fullmatch(pkgbase):
        raise ModelError("Arch split: invalid pkgbase")
    first_release = _text(value["first_release"], "Arch split first release", 32)
    release_key(first_release)
    _text(value["first_release_rule"], "Arch split first-release rule", 400)

    runtime = load_linux_runtime(root)
    catalog = load_distribution_catalog(root)
    install = load_install_manifest(root)
    target = catalog.targets["arch"]
    if release is None:
        version, package_release = runtime.product.version, runtime.product.package_release
    else:
        version, package_release = release
    version = version.replace("-", "_")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version) or package_release < 1:
        raise ModelError("Arch split: product version cannot be compared as a release")
    full_version = f"{version}-{package_release}"
    transformed_version = load_native_deployment(root).driver.dkms_version

    architectures_value = value["architectures"]
    if (
        not isinstance(architectures_value, list)
        or not architectures_value
        or any(item not in target.architectures.values() for item in architectures_value)
    ):
        raise ModelError("Arch split: architectures must be admitted by the distribution catalog")
    require_unique(architectures_value, "Arch split architecture")

    outputs_value = value["outputs"]
    if not isinstance(outputs_value, list) or len(outputs_value) != 4:
        raise ModelError("Arch split: exactly four outputs are required")
    for index, item in enumerate(outputs_value):
        if not isinstance(item, dict):
            raise ModelError(f"Arch split output {index}: must be an object")
        _exact(item, OUTPUT_FIELDS, f"Arch split output {index}")
        if item["edition"] not in COMPONENT_IDS:
            raise ModelError(f"Arch split output {index}: unknown edition")
        for field in ("builds", "payloads", "sibling_dependencies"):
            _identifiers(item[field], f"Arch split output {index} {field}")
    ids = [item["id"] for item in outputs_value]
    names = [item["package_name"] for item in outputs_value]
    require_unique(ids, "Arch split output id")
    require_unique(names, "Arch split package name")
    if pkgbase in names:
        raise ModelError("Arch split: pkgbase must not equal an output name")
    package_of = dict(zip(ids, names))
    _check_partition(outputs_value, install)

    editions_value = value["editions"]
    if not isinstance(editions_value, dict) or set(editions_value) != set(COMPONENT_IDS):
        raise ModelError("Arch split: editions must name Core and Full")
    editions = {edition: editions_value[edition] for edition in COMPONENT_IDS}

    outputs: list[SplitOutput] = []
    for item in outputs_value:
        label = f"Arch split output {item['id']}"
        if not PACKAGE_NAME.fullmatch(item["package_name"]):
            raise ModelError(f"{label}: invalid package name")
        description = _text(item["description"], f"{label} description", 160)
        licenses = tuple(item["licenses"])
        if not licenses or any(entry not in catalog.licenses for entry in licenses):
            raise ModelError(f"{label}: licenses must come from the distribution catalog")
        require_unique(list(licenses), f"{label} license")
        roles = item["dependency_roles"]
        if not isinstance(roles, list) or any(role not in target.dependency_roles for role in roles):
            raise ModelError(f"{label}: unknown dependency role")
        require_unique(roles, f"{label} dependency role")
        for sibling in item["sibling_dependencies"]:
            if sibling not in package_of or sibling == item["id"]:
                raise ModelError(f"{label}: unknown sibling dependency {sibling}")
        external = _relations(item["dependencies"], f"{label} dependency", transformed_version)
        if any(relation_name(entry) in names for entry in external):
            raise ModelError(f"{label}: siblings must be declared as sibling dependencies")
        role_packages = tuple(target.dependency_roles[role] for role in roles)
        siblings = tuple(
            f"{package_of[sibling]}={full_version}" for sibling in item["sibling_dependencies"]
        )
        dependencies = tuple(dict.fromkeys([*role_packages, *siblings, *external]))
        optional_mode = item["optional_dependencies"]
        if optional_mode == "edition":
            if editions[item["edition"]] != item["id"]:
                raise ModelError(f"{label}: only the edition request carries optional applications")
            optional = target.component(item["edition"]).optional_dependencies
        elif optional_mode == "none":
            optional = ()
        else:
            raise ModelError(f"{label}: invalid optional dependency mode")
        files, trees, backup = _destinations(item, install)
        owned_modes = tuple(
            (directory, mode)
            for directory, mode in package_directory_modes(runtime)
            if any(path.startswith(directory.rstrip("/") + "/") for path in (*files, *trees))
        )
        outputs.append(
            SplitOutput(
                id=item["id"],
                package_name=item["package_name"],
                edition=item["edition"],
                description=description,
                licenses=licenses,
                builds=tuple(item["builds"]),
                payloads=tuple(item["payloads"]),
                dependency_roles=tuple(roles),
                sibling_dependencies=tuple(item["sibling_dependencies"]),
                external_dependencies=tuple(dict.fromkeys([*role_packages, *external])),
                dependencies=dependencies,
                optional_dependencies=tuple(optional),
                conflicts=_relations(item["conflicts"], f"{label} conflict", transformed_version),
                provides=_relations(item["provides"], f"{label} provide", transformed_version),
                replaces=_relations(item["replaces"], f"{label} replace", transformed_version),
                backup=backup,
                file_destinations=files,
                tree_destinations=trees,
                directory_modes=owned_modes,
                compiled=any(
                    install.build(build_id).kind not in SOURCE_BUILD_KINDS
                    for build_id in item["builds"]
                ),
            )
        )
    split_outputs = tuple(outputs)

    for edition in COMPONENT_IDS:
        request = editions[edition]
        if request not in package_of:
            raise ModelError(f"Arch split: edition {edition} names an unknown output")
        component = target.component(edition)
        primary = next(output for output in split_outputs if output.id == request)
        if (
            primary.edition != edition
            or primary.package_name != component.package_name
            or primary.description != component.description
        ):
            raise ModelError(
                f"Arch split: edition {edition} must be requested by the unchanged "
                f"{component.package_name} product"
            )

    retired_value = value["retired_relations"]
    if not isinstance(retired_value, list) or len(retired_value) > 8:
        raise ModelError("Arch split: invalid retired relations")
    retired: list[RetiredRelation] = []
    for index, item in enumerate(retired_value):
        label = f"Arch split retired relation {index}"
        if not isinstance(item, dict):
            raise ModelError(f"{label}: must be an object")
        _exact(item, {"output", "relation", "reason", "expires_before_release"}, label)
        expiry = _text(item["expires_before_release"], f"{label} expiry", 32)
        release_key(expiry)
        retired.append(
            RetiredRelation(
                output=_text(item["output"], f"{label} output", 64),
                relation=_text(item["relation"], f"{label} relation", 96),
                reason=_text(item["reason"], f"{label} reason", 400),
                expires_before_release=expiry,
            )
        )

    _check_ownership(split_outputs)
    _check_relations(
        split_outputs,
        tuple(retired),
        catalog,
        install,
        transformed_version,
        full_version,
    )
    split = ArchSplit(
        source_sha256=sha256_file(path),
        pkgbase=pkgbase,
        first_release=first_release,
        version=version,
        package_release=package_release,
        architectures=tuple(architectures_value),
        editions=editions,
        outputs=split_outputs,
        retired_relations=tuple(retired),
        licenses=catalog.licenses,
        description=catalog.description,
        homepage=catalog.homepage,
    )
    _check_closures(split)
    return split
