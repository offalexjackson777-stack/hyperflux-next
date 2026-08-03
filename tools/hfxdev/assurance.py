# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import ModelError, load_json, require_unique


STATUSES = {
    "software-verified",
    "policy-defined",
    "partially-implemented",
    "blocked-by-physical-evidence",
    "publication-locked",
}
ENTRY_KEYS = {
    "section",
    "title",
    "status",
    "owner",
    "evidence",
    "remaining",
    "physical_evidence_required",
    "release_blocking",
}
DESIGN_SECTION = re.compile(r"^(\d+)\. (.+)$", re.MULTILINE)
OWNER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class DesignCoverageEntry:
    section: int
    title: str
    status: str
    owner: str
    evidence: tuple[str, ...]
    remaining: tuple[str, ...]
    physical_evidence_required: bool
    release_blocking: bool


def _design_sections(root: Path) -> dict[int, str]:
    path = root / "docs" / "architecture" / "design-book.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ModelError(f"cannot read design book: {error}") from error
    result = {int(number): title.strip() for number, title in DESIGN_SECTION.findall(text)}
    if sorted(result) != list(range(1, 68)):
        raise ModelError("design book must contain exactly numbered sections 1 through 67")
    return result


def _relative_evidence(root: Path, value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ModelError(f"{label}: evidence must be a non-empty string array")
    require_unique(value, f"{label} evidence path")
    result: list[str] = []
    for item in value:
        path = PurePosixPath(item)
        if path.is_absolute() or ".." in path.parts:
            raise ModelError(f"{label}: evidence path escapes the repository: {item}")
        repository_path = root / path
        if not repository_path.exists() or repository_path.is_symlink():
            raise ModelError(f"{label}: evidence path does not exist: {item}")
        result.append(item)
    return tuple(result)


def _remaining(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ModelError(f"{label}: remaining work must be a string array")
    if len(value) > 8:
        raise ModelError(f"{label}: remaining work exceeds eight items")
    require_unique(value, f"{label} remaining item")
    return tuple(item.strip() for item in value)


def load_design_coverage(root: Path) -> tuple[DesignCoverageEntry, ...]:
    value = load_json(root / "assurance" / "design-coverage.json")
    if set(value) != {"$schema", "schema", "entries"}:
        raise ModelError("design coverage has missing or unknown top-level fields")
    if value["schema"] != "hyperflux-design-coverage-v1":
        raise ModelError("unsupported design coverage schema")
    raw_entries = value["entries"]
    if not isinstance(raw_entries, list) or len(raw_entries) != 67:
        raise ModelError("design coverage must contain exactly 67 entries")
    expected_titles = _design_sections(root)
    entries: list[DesignCoverageEntry] = []
    for index, raw in enumerate(raw_entries, start=1):
        if not isinstance(raw, dict) or set(raw) != ENTRY_KEYS:
            raise ModelError(f"design coverage entry {index}: missing or unknown fields")
        section = raw["section"]
        if isinstance(section, bool) or not isinstance(section, int):
            raise ModelError(f"design coverage entry {index}: section must be an integer")
        title = raw["title"]
        if section not in expected_titles or title != expected_titles[section]:
            raise ModelError(f"design coverage section {section}: title differs from the design book")
        status = raw["status"]
        if status not in STATUSES:
            raise ModelError(f"design coverage section {section}: unknown status {status}")
        owner = raw["owner"]
        if not isinstance(owner, str) or not OWNER.fullmatch(owner):
            raise ModelError(f"design coverage section {section}: invalid owner")
        physical = raw["physical_evidence_required"]
        blocking = raw["release_blocking"]
        if not isinstance(physical, bool) or not isinstance(blocking, bool):
            raise ModelError(f"design coverage section {section}: gates must be boolean")
        entries.append(
            DesignCoverageEntry(
                section=section,
                title=title,
                status=status,
                owner=owner,
                evidence=_relative_evidence(root, raw["evidence"], f"design section {section}"),
                remaining=_remaining(raw["remaining"], f"design section {section}"),
                physical_evidence_required=physical,
                release_blocking=blocking,
            )
        )
    if [entry.section for entry in entries] != list(range(1, 68)):
        raise ModelError("design coverage entries must be ordered sections 1 through 67")
    if entries[-1].status != "publication-locked" or not entries[-1].release_blocking:
        raise ModelError("final success must remain publication-locked and release-blocking")
    return tuple(entries)
