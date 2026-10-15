# SPDX-License-Identifier: GPL-2.0-only

"""Compare the signed-repository and AUR channels of one split release.

Both channels must deliver one product: the same outputs, the same PKGINFO
product contract, the same paths with the same owners, types and modes, and
the same bytes for everything that is not compiled. Compiled ELF bytes from
two independent toolchains may differ; such differences are disclosed with
both digests rather than hidden or treated as parity. Source revision and
version are compared when the AUR recipe and the repository commit are
supplied; whatever cannot be compared is disclosed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

from .arch_split import ArchSplit
from .arch_split_package import output_contract, product_fields
from .distribution_package import _arch_metadata
from .model import ModelError


SCHEMA = "hyperflux-channel-parity-v1"
CHANNELS = ("repository", "aur")
METADATA_MEMBERS = frozenset({".PKGINFO", ".BUILDINFO", ".MTREE"})
ELF_MAGIC = b"\x7fELF"


@dataclass(frozen=True)
class Entry:
    kind: str
    mode: int
    digest: str
    compiled: bool


@dataclass(frozen=True)
class ChannelPackage:
    archive: Path
    name: str
    fields: dict[str, tuple[str, ...]]
    metadata_members: tuple[str, ...]
    entries: dict[str, Entry]


@dataclass(frozen=True)
class ChannelComparison:
    candidate: str
    sources: dict[str, str | None]
    packages: dict[str, dict[str, str]]
    compared_paths: int
    identical_files: int
    problems: tuple[str, ...]
    disclosures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.problems

    def as_json(self) -> str:
        value = {
            "schema": SCHEMA,
            "candidate": self.candidate,
            "passed": self.passed,
            "sources": self.sources,
            "packages": self.packages,
            "compared_paths": self.compared_paths,
            "identical_files": self.identical_files,
            "problems": list(self.problems),
            "disclosures": list(self.disclosures),
        }
        return json.dumps(value, indent=2, sort_keys=True) + "\n"


def read_channel_package(archive: Path) -> ChannelPackage:
    entries: dict[str, Entry] = {}
    metadata: dict[str, bytes] = {}
    try:
        with tarfile.open(archive, "r:*") as handle:
            for member in handle.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ModelError(f"{archive.name}: unsafe member path {member.name}")
                name = relative.as_posix()
                if name.startswith("."):
                    stream = handle.extractfile(member) if member.isfile() else None
                    metadata[name] = stream.read() if stream is not None else b""
                    continue
                path = "/" + name.rstrip("/")
                if path in entries:
                    raise ModelError(f"{archive.name}: duplicate member {path}")
                if member.isdir():
                    entries[path] = Entry("directory", member.mode & 0o7777, "", False)
                elif member.issym():
                    entries[path] = Entry("symlink", 0o777, member.linkname, False)
                elif member.isfile():
                    stream = handle.extractfile(member)
                    content = stream.read() if stream is not None else b""
                    entries[path] = Entry(
                        "file",
                        member.mode & 0o7777,
                        hashlib.sha256(content).hexdigest(),
                        content.startswith(ELF_MAGIC),
                    )
                else:
                    raise ModelError(f"{archive.name}: unsupported member type at {path}")
    except (OSError, tarfile.TarError) as error:
        raise ModelError(f"cannot read channel package {archive}: {error}") from error
    if ".PKGINFO" not in metadata:
        raise ModelError(f"{archive.name}: missing .PKGINFO")
    info = _arch_metadata(metadata[".PKGINFO"], "PKGINFO")
    names = info.get("pkgname", ())
    if len(names) != 1:
        raise ModelError(f"{archive.name}: PKGINFO must name one package")
    return ChannelPackage(
        archive=archive,
        name=names[0],
        fields=product_fields(info),
        metadata_members=tuple(sorted(metadata)),
        entries=entries,
    )


def read_channel(directory: Path) -> dict[str, ChannelPackage]:
    if not directory.is_dir():
        raise ModelError(f"channel directory does not exist: {directory}")
    packages: dict[str, ChannelPackage] = {}
    for archive in sorted(directory.glob("*.pkg.tar*")):
        if archive.name.endswith(".sig") or not archive.is_file():
            continue
        package = read_channel_package(archive)
        if package.name in packages:
            raise ModelError(f"{directory}: two archives carry {package.name}")
        packages[package.name] = package
    return packages


def recipe_source(recipe: Path) -> dict[str, str]:
    """Read version, commit and mode from a generated AUR recipe directory."""

    try:
        text = (recipe / "PKGBUILD").read_text(encoding="utf-8")
    except OSError as error:
        raise ModelError(f"cannot read AUR recipe: {error}") from error

    def value(pattern: str, label: str) -> str:
        match = re.search(pattern, text, re.MULTILINE)
        if match is None:
            raise ModelError(f"AUR recipe has no {label}")
        return match.group(1)

    return {
        "version": f"{value(r'^pkgver=(\S+)$', 'pkgver')}-{value(r'^pkgrel=(\S+)$', 'pkgrel')}",
        "commit": value(r"^_commit='([0-9a-f]{40})'$", "_commit"),
        "mode": value(r"^# Recipe mode: (\S+)$", "recipe mode"),
    }


def _compare_fields(
    split: ArchSplit, name: str, channels: dict[str, ChannelPackage], architecture: str
) -> list[str]:
    problems: list[str] = []
    expected = output_contract(split, split.by_package(name), architecture)
    for channel, package in channels.items():
        for field, value in expected.items():
            if package.fields[field] != value:
                problems.append(
                    f"{channel} {name} PKGINFO {field} is {list(package.fields[field])}, "
                    f"the split contract says {list(value)}"
                )
    return problems


def _compare_entries(
    split: ArchSplit, name: str, channels: dict[str, ChannelPackage]
) -> tuple[list[str], list[str], int, int]:
    problems: list[str] = []
    disclosures: list[str] = []
    output = split.by_package(name)
    repository, aur = channels["repository"], channels["aur"]
    for channel, package in channels.items():
        if ".INSTALL" in package.metadata_members:
            problems.append(f"{channel} {name} carries an install script")
        extra = sorted(set(package.metadata_members) - METADATA_MEMBERS - {".INSTALL"})
        if extra:
            problems.append(f"{channel} {name} carries unreviewed metadata {extra}")
        for path, entry in package.entries.items():
            if entry.kind == "directory":
                continue
            try:
                owner = split.owner_of(path)
            except ModelError as error:
                problems.append(f"{channel} {name}: {error}")
            else:
                if owner != output.id:
                    problems.append(
                        f"{channel} {name} ships {path}, which the split assigns to {owner}"
                    )
    paths = sorted(set(repository.entries) | set(aur.entries))
    identical = 0
    for path in paths:
        left, right = repository.entries.get(path), aur.entries.get(path)
        if left is None or right is None:
            missing = "repository" if left is None else "aur"
            problems.append(f"{name} {path} is missing from the {missing} channel")
            continue
        if left.kind != right.kind:
            problems.append(f"{name} {path} is a {left.kind} in repository and a {right.kind} in aur")
            continue
        if left.mode != right.mode:
            problems.append(
                f"{name} {path} mode is {left.mode:04o} in repository and {right.mode:04o} in aur"
            )
        if left.digest == right.digest:
            if left.kind == "file":
                identical += 1
            continue
        if left.kind == "symlink":
            problems.append(f"{name} {path} points to {left.digest} in repository and {right.digest} in aur")
        elif left.compiled and right.compiled:
            disclosures.append(
                f"{name} {path} compiled bytes differ: repository {left.digest}, aur {right.digest}"
            )
        else:
            problems.append(f"{name} {path} non-compiled content differs between channels")
    return problems, disclosures, len(paths), identical


def compare_channels(
    split: ArchSplit,
    *,
    repository: Path,
    aur: Path,
    architecture: str = "x86_64",
    aur_recipe: Path | None = None,
    repository_commit: str | None = None,
) -> ChannelComparison:
    if not split.active:
        raise ModelError(
            f"channel parity starts with the four-output split at {split.first_release}"
        )
    problems: list[str] = []
    disclosures: list[str] = []
    channels = {"repository": read_channel(repository), "aur": read_channel(aur)}
    expected = [output.package_name for output in split.outputs]
    for channel, packages in channels.items():
        missing = [name for name in expected if name not in packages]
        extra = sorted(set(packages) - set(expected))
        if missing:
            problems.append(f"{channel} channel lacks {missing}")
        if extra:
            problems.append(f"{channel} channel carries packages outside the split {extra}")
    compared = identical = 0
    for name in expected:
        present = {channel: packages[name] for channel, packages in channels.items() if name in packages}
        problems.extend(_compare_fields(split, name, present, architecture))
        if len(present) == len(CHANNELS):
            entry_problems, entry_disclosures, paths, same = _compare_entries(split, name, present)
            problems.extend(entry_problems)
            disclosures.extend(entry_disclosures)
            compared += paths
            identical += same

    sources: dict[str, str | None] = {"repository_commit": repository_commit, "aur_commit": None}
    if repository_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", repository_commit):
        raise ModelError("repository commit must be a full lowercase SHA-1")
    if aur_recipe is None:
        disclosures.append("AUR recipe not supplied: source revision and version were not compared")
    else:
        recipe = recipe_source(aur_recipe)
        sources["aur_commit"] = recipe["commit"]
        if recipe["version"] != split.full_version:
            problems.append(
                f"AUR recipe builds {recipe['version']}, the release is {split.full_version}"
            )
        if recipe["mode"] != "publication":
            disclosures.append(f"AUR recipe is a {recipe['mode']} recipe, not a signed-tag publication")
        if repository_commit is None:
            disclosures.append("repository source commit not supplied: source revisions were not compared")
        elif recipe["commit"] != repository_commit:
            problems.append(
                f"AUR recipe builds {recipe['commit']}, the repository packages {repository_commit}"
            )
    return ChannelComparison(
        candidate=split.full_version,
        sources=sources,
        packages={
            channel: {name: package.archive.name for name, package in sorted(packages.items())}
            for channel, packages in channels.items()
        },
        compared_paths=compared,
        identical_files=identical,
        problems=tuple(problems),
        disclosures=tuple(disclosures),
    )
