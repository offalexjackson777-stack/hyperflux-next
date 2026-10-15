# SPDX-License-Identifier: GPL-2.0-only

"""Shared fixtures for the four-output Arch topology tests."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from pathlib import Path
import shutil
import sys
from typing import Any, Callable, Iterator
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev import arch_split as arch_split_module  # noqa: E402
from hfxdev.arch_split import ArchSplit, load_arch_split  # noqa: E402
from hfxdev.distribution_native import tree_digest, tree_files  # noqa: E402
from hfxdev.package_pipeline import StageResult  # noqa: E402
from hfxdev.tree_metadata import normalize_tree  # noqa: E402


CANDIDATE_RELEASE = ("2.1.0", 1)
EPOCH = 1_767_225_600
AUTHORITY = ROOT / "packaging" / "arch-split.json"


def candidate_split() -> ArchSplit:
    return load_arch_split(ROOT, release=CANDIDATE_RELEASE)


def authority_value() -> dict[str, Any]:
    return json.loads(AUTHORITY.read_text(encoding="utf-8"))


@contextmanager
def mutated_authority(mutate: Callable[[dict[str, Any]], None]) -> Iterator[None]:
    """Load the split with an in-memory edit of `packaging/arch-split.json`."""

    original = arch_split_module.load_json
    value = authority_value()
    mutate(value)

    def load(path: Path) -> Any:
        if Path(path).resolve() == AUTHORITY.resolve():
            return copy.deepcopy(value)
        return original(path)

    with mock.patch.object(arch_split_module, "load_json", load):
        yield


def _content(path: str) -> bytes:
    return f"synthetic content for {path}\n".encode("utf-8")


def synthetic_edition_stages(split: ArchSplit, workspace: Path) -> dict[str, StageResult]:
    """Stage one deterministic file per declared destination of each edition."""

    stages: dict[str, StageResult] = {}
    for edition in ("linux", "full"):
        root = workspace / f"{edition}-stage"
        root.mkdir()
        for output in split.outputs:
            if output.edition != edition:
                continue
            paths = list(output.file_destinations)
            for tree in output.tree_destinations:
                paths.extend([f"{tree}/dkms.conf", f"{tree}/driver/razermouse_driver.c"])
            for destination in paths:
                target = root.joinpath(*Path(destination).parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_content(destination))
                target.chmod(0o755 if "/bin/" in destination else 0o644)
        modes = tuple(
            mode
            for output in split.outputs
            if output.edition == edition
            for mode in output.directory_modes
        )
        normalize_tree(root, EPOCH, modes)
        files = tree_files(root)
        stages[edition] = StageResult(
            root=root,
            inventory=workspace / f"{edition}-inventory.json",
            distribution_component=edition,
            payload_sha256=tree_digest(root, files),
            file_count=len(files),
        )
    return stages


def makepkg_available() -> bool:
    return all(shutil.which(tool) for tool in ("makepkg", "fakeroot", "bsdtar", "zstd"))
