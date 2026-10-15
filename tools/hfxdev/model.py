# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any


# Untracked/generated directory names no repository walker may descend into,
# at any depth, even when git cannot enumerate the checkout.
REPOSITORY_SKIPPED_NAMES = frozenset(
    {".git", ".hfx", ".venv", "__pycache__", "build", "dist", "target"}
)


class ModelError(ValueError):
    """Raised when canonical repository data violates a foundation rule."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelError(f"{path}: {error}") from error
    if not isinstance(value, dict):
        raise ModelError(f"{path}: top-level value must be an object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_unique(values: list[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ModelError(f"duplicate {label}: {', '.join(duplicates)}")


def load_foundation(root: Path) -> dict[str, Any]:
    return load_json(root / "architecture" / "constitution.json")


def _git_repository_files(root: Path) -> tuple[str, ...] | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return tuple(
        os.fsdecode(entry) for entry in result.stdout.split(b"\0") if entry
    )


def _walked_repository_files(root: Path) -> tuple[str, ...]:
    found: list[str] = []
    for directory, directories, filenames in os.walk(
        root, topdown=True, followlinks=False
    ):
        current = Path(directory)
        # A nested .git entry marks another checkout or worktree (for example
        # an editor worktree); its files belong to that checkout.
        directories[:] = sorted(
            name
            for name in directories
            if name not in REPOSITORY_SKIPPED_NAMES
            and not (current / name).is_symlink()
            and not os.path.lexists(current / name / ".git")
        )
        relative = current.relative_to(root)
        found.extend((relative / name).as_posix() for name in filenames)
    return tuple(found)


def repository_files(root: Path) -> tuple[Path, ...]:
    """Regular files that belong to the checkout at root, sorted.

    Git decides first: tracked files plus untracked files it does not ignore,
    so ignored nested worktrees and local outputs never count. When git cannot
    enumerate root (no git, a linked worktree whose common directory is not
    mounted, or an unsafe-ownership refusal) a pruned walk stands in that skips
    the shared generated names and never enters a nested checkout.
    """
    relatives = _git_repository_files(root)
    if relatives is None:
        relatives = _walked_repository_files(root)
    files: set[Path] = set()
    for relative in relatives:
        path = root / relative
        if REPOSITORY_SKIPPED_NAMES.intersection(Path(relative).parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        files.add(path)
    return tuple(sorted(files))
