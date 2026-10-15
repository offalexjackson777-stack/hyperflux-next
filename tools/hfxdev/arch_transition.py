# SPDX-License-Identifier: GPL-2.0-only

"""Static proof of the move from the two-package releases to four outputs.

Users of the signed project repository must reach the four-output topology
with an ordinary `pacman -Syu`, without being forced onto the AUR, without a
Core host silently becoming Full, without a forced removal of a package the
old release allowed, and without breaking anything that depended on a name
the old release provided. Each prior release is read with its own loaders
from its own pinned source, so the comparison is against what that release
actually declared rather than against today's interpretation of it.

The optional repository-to-AUR switch is rendered as two scripts: one that
backs up pacman.conf and comments out exactly the HyperFlux Include line,
and one that restores it. Neither deletes the repository key or the include
file, which remain the rollback inputs. Nothing the packages ship edits
Pacman configuration or keys.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile

from .arch_official_snapshot import satisfies, vercmp
from .arch_repository import ArchRepositoryPolicy
from .arch_split import ArchSplit, relation_name
from .install import load_install_manifest
from .model import ModelError


SCHEMA = "hyperflux-arch-transition-v1"
EDITION_REQUESTS = {"linux": "hyperflux-next-linux", "full": "hyperflux-next-full"}
SWITCH_MARKER = "#HFX-AUR-SWITCH# "
PACMAN_PATHS = ("/etc/pacman.conf", "/etc/pacman.d/", "/usr/share/libalpm/hooks/")
PACMAN_MARKERS = (b"pacman.conf", b"/etc/pacman.d", b"pacman-key", b"libalpm")


@dataclass(frozen=True)
class PriorRelease:
    release: str
    ref: str | None
    commit: str | None


PRIOR_RELEASES = (
    PriorRelease("2.0.0-1", "v2.0.0", "db75a075e56b1c571fb22fe4326ff6683a934d52"),
    # The current tree's two-package release; once 2.0.1-1 is tagged this
    # entry pins that tag like 2.0.0-1.
    PriorRelease("2.0.1-1", None, None),
)

_LOADER = r"""
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools"))
from hfxdev.distributions import load_distribution_catalog
from hfxdev.install import load_install_manifest
from hfxdev.linux_runtime import load_linux_runtime
runtime = load_linux_runtime(root)
manifest = load_install_manifest(root)
target = load_distribution_catalog(root).targets["arch"]
packages = []
for component_id, component in target.components.items():
    builds = [b for b in manifest.builds if b.distribution_component == component_id and b.destination]
    packages.append({
        "edition": component_id,
        "name": component.package_name,
        "depends": list(component.dependencies),
        "conflicts": list(component.conflicts),
        "provides": list(component.provides),
        "replaces": list(component.replaces),
        "files": sorted(
            {str(item.destination) for item in manifest.files if item.distribution_component == component_id}
            | {b.destination for b in builds if b.kind != "source-transform"}
        ),
        "trees": sorted(b.destination.rstrip("/") for b in builds if b.kind == "source-transform"),
    })
print(json.dumps({"version": f"{runtime.product.version}-{runtime.product.package_release}", "packages": packages}))
"""


@dataclass(frozen=True)
class PriorPackage:
    edition: str
    name: str
    depends: tuple[str, ...]
    conflicts: tuple[str, ...]
    provides: tuple[str, ...]
    replaces: tuple[str, ...]
    files: tuple[str, ...]
    trees: tuple[str, ...]


@dataclass(frozen=True)
class PriorTopology:
    release: str
    source: str
    packages: tuple[PriorPackage, ...]

    def package(self, name: str) -> PriorPackage:
        for package in self.packages:
            if package.name == name:
                return package
        raise ModelError(f"{self.release} has no package {name}")


@dataclass(frozen=True)
class EditionTransition:
    edition: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    moved: int
    dropped: tuple[str, ...]


@dataclass(frozen=True)
class TransitionReport:
    source_release: str
    target_release: str
    editions: tuple[EditionTransition, ...]
    problems: tuple[str, ...]
    disclosures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.problems

    def as_json(self) -> dict:
        return {
            "source_release": self.source_release,
            "target_release": self.target_release,
            "passed": self.passed,
            "editions": [
                {
                    "edition": item.edition,
                    "before": list(item.before),
                    "after": list(item.after),
                    "moved_paths": item.moved,
                    "dropped_paths": list(item.dropped),
                }
                for item in self.editions
            ],
            "problems": list(self.problems),
            "disclosures": list(self.disclosures),
        }


def _topology(value: dict, release: str, source: str) -> PriorTopology:
    if value.get("version") != release:
        raise ModelError(f"prior source {source} declares {value.get('version')}, not {release}")
    return PriorTopology(
        release=release,
        source=source,
        packages=tuple(
            PriorPackage(
                edition=item["edition"],
                name=item["name"],
                depends=tuple(item["depends"]),
                conflicts=tuple(item["conflicts"]),
                provides=tuple(item["provides"]),
                replaces=tuple(item["replaces"]),
                files=tuple(item["files"]),
                trees=tuple(item["trees"]),
            )
            for item in value["packages"]
        ),
    )


def _load(tree: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _LOADER, str(tree)],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["no output"]
        raise ModelError(f"prior release loaders failed: {detail[0]}")
    return json.loads(result.stdout)


def load_prior_topology(root: Path, prior: PriorRelease) -> PriorTopology:
    """Read one prior two-package release with its own pinned loaders."""

    if prior.ref is None:
        return _topology(_load(root), prior.release, "working tree")
    resolved = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{prior.ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not resolved:
        raise ModelError(f"prior release {prior.release}: {prior.ref} is not available in this clone")
    if resolved != prior.commit:
        raise ModelError(f"prior release {prior.release}: {prior.ref} moved to {resolved}")
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", resolved],
        capture_output=True,
        check=True,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="hfx-prior-") as directory:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
            handle.extractall(directory, filter="data")
        return _topology(_load(Path(directory)), prior.release, f"{prior.ref} {resolved}")


def _closure(names: set[str], depends: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    pending = list(names)
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(depends.get(name, ()))
    return seen


def _relocate(path: str, old: str, new: str) -> str:
    """Map a versioned DKMS source directory of the old release to the new one."""

    parts = PurePosixPath(path).parts
    if len(parts) > 3 and parts[1:3] == ("usr", "src") and parts[3].endswith(f"-{old}"):
        renamed = parts[3][: -len(old)] + new
        return str(PurePosixPath(*parts[:3], renamed, *parts[4:]))
    return path


def _provided(relations: tuple[str, ...]) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for relation in relations:
        name, separator, version = relation.partition("=")
        values[name] = version if separator else None
    return values


def analyse_transition(split: ArchSplit, prior: PriorTopology) -> TransitionReport:
    if not split.active:
        raise ModelError(f"the four-output topology starts at {split.first_release}")
    problems: list[str] = []
    disclosures: list[str] = []
    if vercmp(split.full_version, prior.release) <= 0:
        problems.append(f"{split.full_version} does not upgrade {prior.release}")
    old_names = {package.name for package in prior.packages}
    old_depends = {
        package.name: {relation_name(item) for item in package.depends} & old_names
        for package in prior.packages
    }
    new_names = {output.package_name for output in split.outputs}
    new_depends = {
        output.package_name: {split.output(item).package_name for item in output.sibling_dependencies}
        for output in split.outputs
    }
    full_only = {output.package_name for output in split.outputs if output.edition == "full"}
    old_upstream = prior.release.rsplit("-", 1)[0]
    for package in prior.packages:
        if package.name not in new_names:
            problems.append(f"{prior.release} {package.name} has no successor; -Syu would strand it")
        if any(relation_name(item) == "openrazer-driver-dkms" for item in package.replaces):
            disclosures.append(
                f"{prior.release} {package.name} replaced openrazer-driver-dkms; "
                f"{split.full_version} only conflicts with and provides it"
            )
    editions: list[EditionTransition] = []
    for edition, request in EDITION_REQUESTS.items():
        label = f"{prior.release} {edition}"
        if request not in old_names:
            problems.append(f"{label}: {prior.release} has no {request} to upgrade")
            continue
        before = _closure({request}, old_depends)
        after = _closure(before & new_names, new_depends)
        if edition == "linux" and after & full_only:
            problems.append(f"{label}: -Syu would install Full outputs {sorted(after & full_only)}")
        if after != set(split.install_set(edition)):
            problems.append(f"{label}: -Syu lands on {sorted(after)}, not the {edition} install set")
        moved = 0
        dropped: list[str] = []
        for name in sorted(before):
            package = prior.package(name)
            paths = [*package.files, *(f"{tree}/dkms.conf" for tree in package.trees)]
            for path in paths:
                current = _relocate(path, old_upstream, split.version)
                try:
                    owner = split.output(split.owner_of(current)).package_name
                except ModelError:
                    dropped.append(path)
                    continue
                if owner not in after:
                    problems.append(f"{label}: {path} moves to {owner}, which -Syu does not install")
                elif owner != name:
                    moved += 1
        for path in dropped:
            disclosures.append(f"{label}: {path} is no longer shipped")
        old_conflicts = {relation_name(item) for name in before for item in prior.package(name).conflicts}
        for name in sorted(after):
            output = split.by_package(name)
            for relation in output.conflicts:
                if relation_name(relation) not in old_conflicts:
                    problems.append(
                        f"{label}: {name} newly conflicts with {relation}, forcing a removal during -Syu"
                    )
                for other in sorted(after - {name}):
                    peer = split.by_package(other)
                    if relation_name(relation) in {other, *(relation_name(item) for item in peer.provides)}:
                        problems.append(f"{label}: {name} conflicts with {other} in the same set")
        old_provided: dict[str, str | None] = {}
        for name in before:
            old_provided.update(_provided(prior.package(name).provides))
        new_provided: dict[str, str | None] = {}
        for name in after:
            new_provided.update(_provided(split.by_package(name).provides))
        for name, version in sorted(old_provided.items()):
            if name not in new_provided:
                problems.append(f"{label}: {name} is no longer provided; its dependants would break")
            elif version is not None and (
                new_provided[name] is None or not satisfies(new_provided[name], "=", version)
            ):
                problems.append(
                    f"{label}: {name} was provided at {version}, now at {new_provided[name]}"
                )
        editions.append(
            EditionTransition(
                edition=edition,
                before=tuple(sorted(before)),
                after=tuple(item.package_name for item in split.outputs if item.package_name in after),
                moved=moved,
                dropped=tuple(dropped),
            )
        )
    return TransitionReport(
        source_release=prior.release,
        target_release=split.full_version,
        editions=tuple(editions),
        problems=tuple(problems),
        disclosures=tuple(disclosures),
    )


def payload_items(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    """Every installed payload file as (component, destination, content)."""

    return tuple(
        (item.component, str(item.destination), item.source.read_bytes())
        for item in load_install_manifest(root).files
    )


def check_pacman_safety(
    payload: tuple[tuple[str, str, bytes], ...], pkgbuild: str | None = None
) -> tuple[str, ...]:
    """Nothing the outputs ship, or the AUR recipe runs, touches Pacman state.

    Hooks and install scripts are the only ways a package can act on Pacman
    configuration or keys during a transaction, so the outputs ship neither,
    and no shipped file or recipe command names Pacman configuration or keys.
    """

    problems: list[str] = []
    for component, destination, content in payload:
        if destination == PACMAN_PATHS[0] or destination.startswith(PACMAN_PATHS[1:]):
            problems.append(f"payload {component} installs into Pacman state at {destination}")
        for marker in PACMAN_MARKERS:
            if marker in content:
                problems.append(f"payload {component} mentions {marker.decode()}")
    if pkgbuild is not None:
        code = "\n".join(line for line in pkgbuild.splitlines() if not line.lstrip().startswith("#"))
        for marker in PACMAN_MARKERS:
            if marker.decode() in code:
                problems.append(f"AUR recipe code mentions {marker.decode()}")
        if any(line.strip().startswith("install=") for line in code.splitlines()):
            problems.append("AUR recipe declares an install script")
    return tuple(problems)


def include_line(policy: ArchRepositoryPolicy) -> str:
    return f"Include = /etc/pacman.d/{policy.name}.conf"


def _script_header(policy: ArchRepositoryPolicy) -> str:
    return f"""#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

pacman_conf=${{HFX_PACMAN_CONF:-/etc/pacman.conf}}
include_path=${{HFX_REPOSITORY_INCLUDE:-/etc/pacman.d/{policy.name}.conf}}
repository='{policy.name}'
include_line="Include = $include_path"
marker='{SWITCH_MARKER}'

repository_active() {{
  pacman-conf --config "$pacman_conf" --repo-list | grep -qxF -- "$repository"
}}

rewrite() {{
  local from=$1 to=$2 staged
  staged=$(mktemp "$pacman_conf.hfx.XXXXXX")
  awk -v from="$from" -v to="$to" '$0 == from {{ print to; next }} {{ print }}' "$pacman_conf" > "$staged"
  chmod --reference="$pacman_conf" "$staged"
  mv -f -- "$staged" "$pacman_conf"
}}

count() {{
  awk -v line="$1" '$0 == line {{ n++ }} END {{ print n + 0 }}' "$pacman_conf"
}}
"""


def switch_script(policy: ArchRepositoryPolicy) -> str:
    """Disable the project repository for an AUR install, keeping rollback inputs."""

    return _script_header(policy) + """
if [[ "$(count "$include_line")" -ne 1 ]] || ! repository_active; then
  printf 'Expected exactly one active "%s"; nothing changed.\\n' "$include_line" >&2
  exit 1
fi
backup="$pacman_conf.hyperflux-next-aur-switch.$(date -u +%Y%m%dT%H%M%SZ)"
cp -p -- "$pacman_conf" "$backup"
rewrite "$include_line" "$marker$include_line"
if repository_active; then
  cp -p -- "$backup" "$pacman_conf"
  printf 'The repository is still active; pacman.conf was restored.\\n' >&2
  exit 1
fi
printf 'Disabled [%s]. Backup: %s\\n' "$repository" "$backup"
printf 'Kept for rollback: %s and the repository key.\\n' "$include_path"
"""


def rollback_script(policy: ArchRepositoryPolicy, split: ArchSplit) -> str:
    """Re-enable the project repository disabled by the switch script.

    The script only restores the Include line. Replacing the locally built
    packages with the signed copies is a separate, printed Pacman step so a
    failure there never leaves the configuration half-restored.
    """

    names = " ".join(output.package_name for output in split.outputs)
    return _script_header(policy) + f"""
packages='{names}'
""" + """
if [[ "$(count "$marker$include_line")" -ne 1 ]] || [[ ! -r "$include_path" ]]; then
  printf 'No single switched "%s" with its include file; nothing changed.\\n' "$include_line" >&2
  exit 1
fi
before=$(mktemp "$pacman_conf.hfx-rollback.XXXXXX")
cp -p -- "$pacman_conf" "$before"
rewrite "$marker$include_line" "$include_line"
if ! repository_active; then
  cp -p -- "$before" "$pacman_conf"
  rm -f -- "$before"
  printf 'The repository did not come back; pacman.conf was restored.\\n' >&2
  exit 1
fi
rm -f -- "$before"
printf 'Re-enabled [%s].\\n' "$repository"
printf 'Next, replace the AUR builds with the signed copies:\\n'
printf '  sudo pacman -Syuu\\n'
printf '  sudo pacman -S $(pacman -Qq %s 2>/dev/null)\\n' "$packages"
"""
