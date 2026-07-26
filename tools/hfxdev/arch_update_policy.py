# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import io
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

from .model import ModelError


_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+_.-]{0,63}$")
_PACKAGE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_~-]{0,63}-[1-9][0-9]*$")
_DEPENDENCY_NAME = re.compile(r"^([a-z0-9][a-z0-9+_.-]{0,63})")
_REQUIRED_TOOLS = ("bsdtar", "fakeroot", "pacman", "repo-add", "zstd")
_FIXTURE_REPOSITORY = "hyperflux-openrazer-policy"


def _run(
    command: list[str],
    *,
    label: str,
    cwd: Path | None = None,
    timeout_seconds: int,
    expected_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"{label} could not run: {error}") from error
    if expected_success and result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise ModelError(
            f"{label} failed with exit status {result.returncode}: {details}"
        )
    return result


def _fixture_package(
    directory: Path,
    *,
    name: str,
    version: str,
    dependencies: tuple[str, ...] = (),
    timeout_seconds: int,
) -> Path:
    if not _PACKAGE_NAME.fullmatch(name) or not _PACKAGE_VERSION.fullmatch(version):
        raise ModelError("OpenRazer update fixture has invalid package metadata")
    metadata = [
        f"pkgname = {name}",
        f"pkgbase = {name}",
        f"pkgver = {version}",
        "pkgdesc = HyperFlux Next offline update-policy fixture",
        "url = https://openrazer.github.io/",
        "builddate = 1700000000",
        "packager = HyperFlux Next verification",
        "size = 0",
        "arch = x86_64",
        "license = GPL2",
    ]
    metadata.extend(f"depend = {dependency}" for dependency in dependencies)
    payload = ("\n".join(metadata) + "\n").encode("utf-8")
    archive = directory / f"{name}-{version}-x86_64.pkg.tar"
    with tarfile.open(archive, mode="w", format=tarfile.GNU_FORMAT) as package:
        member = tarfile.TarInfo(".PKGINFO")
        member.size = len(payload)
        member.mode = 0o644
        member.mtime = 1_700_000_000
        member.uid = 0
        member.gid = 0
        member.uname = "root"
        member.gname = "root"
        package.addfile(member, io.BytesIO(payload))
    compressed = archive.with_suffix(archive.suffix + ".zst")
    _run(
        [
            "zstd",
            "--quiet",
            "--force",
            "--threads=1",
            "-19",
            "-o",
            str(compressed),
            str(archive),
        ],
        label=f"compress {name} update fixture",
        timeout_seconds=timeout_seconds,
    )
    archive.unlink()
    return compressed


def _pacman_arguments(workspace: Path) -> list[str]:
    return [
        "fakeroot",
        "pacman",
        "--root",
        str(workspace / "root"),
        "--dbpath",
        str(workspace / "database"),
        "--cachedir",
        str(workspace / "cache"),
        "--hookdir",
        str(workspace / "hooks"),
        "--logfile",
        str(workspace / "pacman.log"),
        "--config",
        str(workspace / "pacman.conf"),
        "--noconfirm",
    ]


def _host_dependency_assumptions(
    package: Path, *, timeout_seconds: int
) -> tuple[str, ...]:
    metadata = _run(
        ["bsdtar", "-xOf", str(package), ".PKGINFO"],
        label="read HyperFlux Arch package metadata",
        timeout_seconds=timeout_seconds,
    ).stdout
    names: set[str] = set()
    for line in metadata.splitlines():
        key, separator, value = line.partition(" = ")
        if key != "depend" or not separator:
            continue
        match = _DEPENDENCY_NAME.match(value)
        if match is None:
            raise ModelError("HyperFlux Arch package contains an invalid dependency")
        names.add(match.group(1))
    assumptions = []
    for name in sorted(names):
        query = _run(
            ["pacman", "-Q", name],
            label=f"query host dependency {name}",
            timeout_seconds=timeout_seconds,
        )
        fields = query.stdout.strip().split()
        if len(fields) != 2 or fields[0] != name:
            raise ModelError(f"host dependency {name} has malformed package metadata")
        assumptions.append(f"{name}={fields[1]}")
    return tuple(assumptions)


def _write_pacman_configuration(workspace: Path, repository: Path) -> None:
    for name in ("root", "database", "cache", "hooks"):
        (workspace / name).mkdir(parents=True)
    (workspace / "pacman.conf").write_text(
        "\n".join(
            [
                "[options]",
                "Architecture = x86_64",
                "SigLevel = Never",
                "LocalFileSigLevel = Never",
                "",
                f"[{_FIXTURE_REPOSITORY}]",
                "SigLevel = Never",
                f"Server = file://{repository}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _verify_scenario(
    hyperflux_package: Path,
    *,
    package_name: str,
    candidate_version: str,
    assumptions: tuple[str, ...],
    workspace: Path,
    timeout_seconds: int,
) -> None:
    workspace.mkdir(parents=True)
    repository = workspace / "repository"
    repository.mkdir()
    current_daemon = _fixture_package(
        workspace,
        name="openrazer-daemon",
        version="3.12.4-2",
        dependencies=("openrazer-driver-dkms",),
        timeout_seconds=timeout_seconds,
    )
    candidate_daemon = _fixture_package(
        repository,
        name="openrazer-daemon",
        version=candidate_version,
        dependencies=("openrazer-driver-dkms",),
        timeout_seconds=timeout_seconds,
    )
    candidate_driver = _fixture_package(
        repository,
        name="openrazer-driver-dkms",
        version=candidate_version,
        timeout_seconds=timeout_seconds,
    )
    database = repository / f"{_FIXTURE_REPOSITORY}.db.tar.gz"
    _run(
        ["repo-add", str(database), str(candidate_daemon), str(candidate_driver)],
        label=f"create OpenRazer {candidate_version} fixture repository",
        timeout_seconds=timeout_seconds,
    )
    _write_pacman_configuration(workspace, repository)
    base = _pacman_arguments(workspace)
    _run(
        [
            *base,
            "-U",
            "--dbonly",
            "--nodeps",
            "--nodeps",
            "--noscriptlet",
            str(hyperflux_package),
            str(current_daemon),
        ],
        label="seed isolated HyperFlux and OpenRazer package database",
        timeout_seconds=timeout_seconds,
    )
    assumption_arguments = [
        argument
        for value in assumptions
        for argument in ("--assume-installed", value)
    ]
    plan = _run(
        [
            *base,
            "-Syu",
            "--print",
            "--print-format",
            "__HFX_PACKAGE__%n %v",
            *assumption_arguments,
        ],
        label=f"resolve OpenRazer {candidate_version} system upgrade",
        timeout_seconds=timeout_seconds,
    )
    planned = tuple(
        line.removeprefix("__HFX_PACKAGE__").strip()
        for line in plan.stdout.splitlines()
        if line.startswith("__HFX_PACKAGE__")
    )
    if planned != (f"openrazer-daemon {candidate_version}",):
        raise ModelError(
            f"OpenRazer {candidate_version} upgrade selected an unsafe package plan: "
            + (", ".join(planned) if planned else "empty")
        )
    _run(
        [
            *base,
            "-Syu",
            "--dbonly",
            "--noscriptlet",
            *assumption_arguments,
        ],
        label=f"apply isolated OpenRazer {candidate_version} system upgrade",
        timeout_seconds=timeout_seconds,
    )
    installed_query = _run(
        [*base, "-Q"],
        label="inspect isolated packages after OpenRazer upgrade",
        timeout_seconds=timeout_seconds,
    )
    installed: dict[str, str] = {}
    for line in installed_query.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] in installed:
            raise ModelError("isolated package database contains malformed metadata")
        installed[fields[0]] = fields[1]
    if package_name not in installed:
        raise ModelError("isolated system upgrade replaced the HyperFlux package")
    if installed.get("openrazer-daemon") != candidate_version:
        raise ModelError("isolated system upgrade did not update OpenRazer as expected")
    if "openrazer-driver-dkms" in installed:
        raise ModelError("isolated system upgrade replaced the HyperFlux-patched driver")
    _run(
        [*base, "-T", "openrazer-driver-dkms"],
        label="verify HyperFlux still satisfies the OpenRazer driver dependency",
        timeout_seconds=timeout_seconds,
    )


def verify_arch_openrazer_update_policy(
    hyperflux_package: Path,
    *,
    package_name: str,
    workspace: Path,
    timeout_seconds: int,
) -> None:
    if not hyperflux_package.is_file() or hyperflux_package.is_symlink():
        raise ModelError("OpenRazer update policy requires one regular Arch package")
    if not _PACKAGE_NAME.fullmatch(package_name):
        raise ModelError("OpenRazer update policy received an invalid package name")
    missing = [tool for tool in _REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise ModelError(
            "OpenRazer update policy requires: " + ", ".join(sorted(missing))
        )
    assumptions = _host_dependency_assumptions(
        hyperflux_package, timeout_seconds=timeout_seconds
    )
    workspace = workspace.resolve()
    if workspace.exists():
        if not workspace.is_dir() or workspace.is_symlink():
            raise ModelError("OpenRazer update policy workspace is unsafe")
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    with tempfile.TemporaryDirectory(
        prefix="hyperflux-openrazer-update-policy-", dir=workspace
    ) as temporary:
        root = Path(temporary)
        for index, candidate_version in enumerate(("3.12.4-4", "3.13.0-1")):
            _verify_scenario(
                hyperflux_package,
                package_name=package_name,
                candidate_version=candidate_version,
                assumptions=assumptions,
                workspace=root / f"scenario-{index}",
                timeout_seconds=timeout_seconds,
            )
