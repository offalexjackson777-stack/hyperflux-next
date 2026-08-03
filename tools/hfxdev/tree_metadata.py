# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat

from .model import ModelError


DirectoryModes = tuple[tuple[str, int], ...]


def directory_path(root: Path, destination: str) -> Path:
    path = PurePosixPath(destination)
    if not path.is_absolute() or len(path.parts) < 2 or ".." in path.parts:
        raise ModelError(f"unsafe staged directory destination: {destination}")
    target = root.joinpath(*path.parts[1:])
    if not target.is_dir() or target.is_symlink():
        raise ModelError(f"staged directory mode target is missing: {destination}")
    return target


def normalize_tree(
    root: Path,
    epoch: int,
    directory_modes: DirectoryModes = (),
) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o755)
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    for destination, mode in directory_modes:
        directory_path(root, destination).chmod(mode)
    root.chmod(0o755)
    os.utime(root, (epoch, epoch), follow_symlinks=False)


def verify_directory_modes(
    root: Path,
    directory_modes: DirectoryModes,
    label: str,
) -> None:
    for destination, expected in directory_modes:
        actual = stat.S_IMODE(directory_path(root, destination).stat().st_mode)
        if actual != expected:
            raise ModelError(
                f"{label} directory mode differs for {destination}: "
                f"expected {expected:04o}, found {actual:04o}"
            )
