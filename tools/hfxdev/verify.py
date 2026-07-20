# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

from .model import ModelError, load_foundation, load_json, require_unique, sha256_file
from .render import rendered_files


ALLOWED_DISPOSITIONS = {
    "REIMPLEMENT",
    "REIMPLEMENT_AND_GENERATE",
    "REIMPLEMENT_WITH_PINNED_IMPORT",
    "MIGRATE_AFTER_REVIEW",
    "SPECIFICATION_ONLY",
    "TEST_REFERENCE",
    "RESEARCH_LINK",
    "REJECT",
}
ALLOWED_STATUSES = {"PENDING_REVIEW", "IN_PROGRESS", "ACCEPTED", "REJECTED"}
IGNORED_PATH_PARTS = {".git", ".hfx", "__pycache__"}


def _check_constitution(constitution: dict) -> None:
    if constitution.get("schema") != "hyperflux-architecture-constitution-v1":
        raise ModelError("unsupported architecture constitution schema")
    invariant_ids = [entry["id"] for entry in constitution["invariants"]]
    require_unique(invariant_ids, "architecture invariant id")
    component_ids = [entry["id"] for entry in constitution["components"]]
    require_unique(component_ids, "architecture component id")
    if constitution["publication_interlock"].get("remote_repository_created") is not False:
        raise ModelError("publication interlock must keep remote repository creation false")
    if constitution["publication_interlock"].get("publication_authorized") is not False:
        raise ModelError("publication interlock must keep publication authorization false")


def _check_sources(root: Path, sources: dict) -> None:
    source_ids = [source["id"] for source in sources["sources"]]
    require_unique(source_ids, "migration source id")
    for source in sources["sources"]:
        if source["kind"] == "imported-document":
            path = root / source["imported_path"]
            if not path.is_file():
                raise ModelError(f"missing imported document: {source['imported_path']}")
            if source["sha256"] == "TO_BE_CAPTURED":
                raise ModelError(f"{source['id']}: imported document digest is not captured")
            actual = sha256_file(path)
            if actual != source["sha256"]:
                raise ModelError(f"{source['id']}: imported document digest mismatch")
            continue
        inventory_path = root / source["inventory"]
        if not inventory_path.is_file():
            raise ModelError(f"{source['id']}: missing source inventory")
        inventory = load_json(inventory_path)
        if inventory.get("source") != source["id"]:
            raise ModelError(f"{source['id']}: inventory source mismatch")
        if not inventory.get("commit", "").startswith(source["commit"]):
            raise ModelError(f"{source['id']}: inventory commit mismatch")
        entries = inventory.get("entries", [])
        if inventory.get("entry_count") != len(entries):
            raise ModelError(f"{source['id']}: inventory count mismatch")
        paths = [entry["path"] for entry in entries]
        require_unique(paths, f"{source['id']} inventory path")
        if paths != sorted(paths):
            raise ModelError(f"{source['id']}: inventory paths are not sorted")


def _check_ledger(sources: dict, ledger: dict) -> None:
    source_ids = {source["id"] for source in sources["sources"]}
    entry_ids = [entry["id"] for entry in ledger["entries"]]
    require_unique(entry_ids, "migration ledger id")
    for entry in ledger["entries"]:
        if entry["disposition"] not in ALLOWED_DISPOSITIONS:
            raise ModelError(f"{entry['id']}: unknown disposition {entry['disposition']}")
        if entry["status"] not in ALLOWED_STATUSES:
            raise ModelError(f"{entry['id']}: unknown status {entry['status']}")
        unknown_sources = sorted(set(entry["sources"]) - source_ids)
        if unknown_sources:
            raise ModelError(f"{entry['id']}: unknown sources {', '.join(unknown_sources)}")
        if not entry["rationale"].strip():
            raise ModelError(f"{entry['id']}: rationale is empty")


def _check_generated(root: Path) -> None:
    for path, expected in rendered_files(root).items():
        if not path.is_file():
            raise ModelError(f"missing generated file: {path.relative_to(root)}")
        if path.read_text(encoding="utf-8") != expected:
            raise ModelError(f"stale generated file: {path.relative_to(root)}; run ./hfx generate")


def _check_repository_paths(root: Path) -> None:
    absolute_home = re.compile(r"/home/[A-Za-z0-9_.-]+/")
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in IGNORED_PATH_PARTS for part in path.parts):
            continue
        if path.suffix in {".pyc", ".png", ".jpg", ".zst"} or path.name == "LICENSE":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if absolute_home.search(text):
            raise ModelError(f"private absolute path found in {path.relative_to(root)}")


def _run_tests(root: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=root,
        check=False,
    )
    if result.returncode != 0:
        raise ModelError("foundation unit tests failed")


def verify_all(root: Path) -> list[str]:
    constitution, sources, ledger = load_foundation(root)
    _check_constitution(constitution)
    _check_sources(root, sources)
    _check_ledger(sources, ledger)
    _check_generated(root)
    _check_repository_paths(root)
    _run_tests(root)
    return [
        "architecture constitution",
        "source identities and inventories",
        "migration ledger",
        "generated truth",
        "private-path boundary",
        "foundation unit tests",
    ]

