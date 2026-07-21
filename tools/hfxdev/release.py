# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import ModelError, load_foundation, load_json, require_unique


GATE_KEYS = {
    "id",
    "title",
    "status",
    "criteria",
    "evidence",
    "remaining",
    "physical_evidence_required",
    "publication_authorization_required",
}
STATUSES = {
    "software-satisfied",
    "blocked-by-lifecycle-evidence",
    "blocked-by-physical-evidence",
    "publication-locked",
}


@dataclass(frozen=True)
class ReleaseGate:
    id: str
    title: str
    status: str
    criteria: tuple[str, ...]
    evidence: tuple[str, ...]
    remaining: tuple[str, ...]
    physical_evidence_required: bool
    publication_authorization_required: bool


def _strings(value: Any, label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ModelError(f"{label}: must be a string array")
    if nonempty and not value:
        raise ModelError(f"{label}: must not be empty")
    require_unique(value, label)
    return tuple(item.strip() for item in value)


def _evidence(root: Path, value: Any, label: str) -> tuple[str, ...]:
    paths = _strings(value, label, nonempty=True)
    for item in paths:
        relative = PurePosixPath(item)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != item:
            raise ModelError(f"{label}: path escapes the repository: {item}")
        path = root / relative
        if not path.exists() or path.is_symlink():
            raise ModelError(f"{label}: path does not exist: {item}")
    return paths


def load_release_gates(root: Path) -> tuple[ReleaseGate, ...]:
    value = load_json(root / "assurance" / "release-gates.json")
    if set(value) != {"$schema", "schema", "gates"}:
        raise ModelError("release gates have missing or unknown top-level fields")
    if value["schema"] != "hyperflux-release-gates-v1":
        raise ModelError("unsupported release-gate schema")
    raw_gates = value["gates"]
    if not isinstance(raw_gates, list) or len(raw_gates) != 10:
        raise ModelError("release-gate ledger must contain exactly ten gates")
    constitution, _, _ = load_foundation(root)
    expected = constitution["publication_interlock"]["required_gate_ids"]
    gates: list[ReleaseGate] = []
    for index, raw in enumerate(raw_gates):
        if not isinstance(raw, dict) or set(raw) != GATE_KEYS:
            raise ModelError(f"release gate {index}: missing or unknown fields")
        gate_id = raw["id"]
        title = raw["title"]
        status = raw["status"]
        if not isinstance(gate_id, str) or not re.fullmatch(r"HFX-GATE-[A-Z0-9-]+", gate_id):
            raise ModelError(f"release gate {index}: invalid id")
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise ModelError(f"release gate {gate_id}: invalid title")
        if status not in STATUSES:
            raise ModelError(f"release gate {gate_id}: invalid status")
        physical = raw["physical_evidence_required"]
        publication = raw["publication_authorization_required"]
        if not isinstance(physical, bool) or not isinstance(publication, bool):
            raise ModelError(f"release gate {gate_id}: flags must be boolean")
        remaining = _strings(raw["remaining"], f"release gate {gate_id} remaining")
        if status == "software-satisfied" and remaining:
            raise ModelError(f"release gate {gate_id}: satisfied gate names remaining work")
        if status != "software-satisfied" and not remaining:
            raise ModelError(f"release gate {gate_id}: blocked gate must name remaining work")
        if status == "blocked-by-physical-evidence" and not physical:
            raise ModelError(f"release gate {gate_id}: physical block is not marked physical")
        if status == "publication-locked" and not publication:
            raise ModelError(f"release gate {gate_id}: publication lock is not explicit")
        gates.append(
            ReleaseGate(
                id=gate_id,
                title=title.strip(),
                status=status,
                criteria=_strings(raw["criteria"], f"release gate {gate_id} criteria", nonempty=True),
                evidence=_evidence(root, raw["evidence"], f"release gate {gate_id} evidence"),
                remaining=remaining,
                physical_evidence_required=physical,
                publication_authorization_required=publication,
            )
        )
    if [gate.id for gate in gates] != expected:
        raise ModelError("release gates must exactly match constitution order and identity")
    final = gates[-1]
    if final.id != "HFX-GATE-PUBLICATION-DECISION" or final.status != "publication-locked":
        raise ModelError("final publication decision must remain locked")
    return tuple(gates)
