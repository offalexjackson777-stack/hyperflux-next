# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tarfile

from .distributions import DistributionCatalog, DistributionTarget
from .linux_runtime import LinuxRuntime
from .model import ModelError, sha256_file
from .package_pipeline import ArtifactSet


@dataclass(frozen=True)
class NativePackageContext:
    repository_root: Path
    workspace_root: Path
    package_root: Path
    packages: Path
    runtime: LinuxRuntime
    catalog: DistributionCatalog
    target: DistributionTarget
    artifacts: ArtifactSet
    architecture: str
    payload_sha256: str
    payload_file_count: int


def tree_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ModelError(f"distribution payload contains a symbolic link: {path}")
        if path.is_file():
            files.append(path)
    return files


def tree_digest(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(path.stat().st_mode):04o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def normalize_tree(root: Path, epoch: int) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o755)
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    root.chmod(0o755)
    os.utime(root, (epoch, epoch), follow_symlinks=False)


def write_payload_tar(root: Path, destination: Path, epoch: int) -> None:
    with tarfile.open(destination, "w", format=tarfile.GNU_FORMAT) as archive:
        for path in sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        ):
            relative = path.relative_to(root).as_posix()
            info = tarfile.TarInfo(relative + ("/" if path.is_dir() else ""))
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = epoch
            info.mode = 0o755 if path.is_dir() else stat.S_IMODE(path.stat().st_mode)
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif path.is_file() and not path.is_symlink():
                info.size = path.stat().st_size
                with path.open("rb") as source:
                    archive.addfile(info, source)
            else:
                raise ModelError(
                    f"unsupported distribution payload entry: {relative}"
                )
    os.utime(destination, (epoch, epoch))
