# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable

from .model import ModelError, load_json, require_unique


ROOT_KEYS = {
    "$schema",
    "schema",
    "source_revision",
    "software_baseline",
    "protected_surfaces",
    "known_gaps",
    "change_policy",
}
SURFACE_KEYS = {"id", "summary", "authority_paths", "claim_ids", "verification_nodes"}
GAP_KEYS = {"id", "summary", "forbidden_claim", "required_evidence"}
POLICY_KEYS = {
    "integration_only_prefixes",
    "protected_prefixes",
    "advanced_capability_prefixes",
    "approval_required_when",
    "required_reverification",
}
REVISION = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ProtectedBehavior:
    source_revision: str
    required_nodes: tuple[str, ...]
    protected_prefixes: tuple[str, ...]
    integration_only_prefixes: tuple[str, ...]
    advanced_capability_prefixes: tuple[str, ...]
    claim_ids: tuple[str, ...]
    known_gap_ids: tuple[str, ...]

    def protected_changes(self, paths: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            sorted(
                path
                for path in paths
                if any(path.startswith(prefix) for prefix in self.protected_prefixes)
            )
        )

    def integration_only(self, paths: Iterable[str]) -> bool:
        values = tuple(paths)
        return bool(values) and all(
            any(path.startswith(prefix) for prefix in self.integration_only_prefixes)
            for path in values
        )

    def advanced_change_requires_approval(self, paths: Iterable[str]) -> bool:
        values = tuple(paths)
        is_advanced = any(
            any(path.startswith(prefix) for prefix in self.advanced_capability_prefixes)
            for path in values
        )
        return is_advanced and bool(self.protected_changes(values))


def _exact(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing:
        raise ModelError(f"{label}: missing fields {', '.join(missing)}")
    if extra:
        raise ModelError(f"{label}: unknown fields {', '.join(extra)}")


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ModelError(f"{label}: expected a non-empty string list")
    require_unique(value, label)
    return tuple(value)


def load_protected_behavior(root: Path) -> ProtectedBehavior:
    value = load_json(root / "assurance" / "protected-behavior.json")
    _exact(value, ROOT_KEYS, "protected behavior")
    if value["schema"] != "hyperflux-protected-behavior-v1":
        raise ModelError("protected behavior: unsupported schema")
    revision = value["source_revision"]
    if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
        raise ModelError("protected behavior: invalid source revision")

    baseline = value["software_baseline"]
    if not isinstance(baseline, dict):
        raise ModelError("protected behavior: software baseline must be an object")
    _exact(baseline, {"lane", "status", "required_nodes"}, "software baseline")
    if baseline["lane"] != "fast" or baseline["status"] != "passed":
        raise ModelError("protected behavior: baseline must record a passing fast lane")
    required_nodes = _strings(baseline["required_nodes"], "protected baseline nodes")

    evidence = load_json(root / "profiles" / "evidence" / "claims.json")
    available_claims = {claim["id"] for claim in evidence["claims"]}
    surfaces = value["protected_surfaces"]
    if not isinstance(surfaces, list) or not surfaces:
        raise ModelError("protected behavior: protected surfaces must be non-empty")
    surface_ids: list[str] = []
    claim_ids: list[str] = []
    for index, surface in enumerate(surfaces):
        if not isinstance(surface, dict):
            raise ModelError(f"protected surface {index}: expected an object")
        _exact(surface, SURFACE_KEYS, f"protected surface {index}")
        surface_ids.append(surface["id"])
        _strings(surface["authority_paths"], f"protected surface {surface['id']} paths")
        claims = _strings(surface["claim_ids"], f"protected surface {surface['id']} claims")
        missing_claims = sorted(set(claims) - available_claims)
        if missing_claims:
            raise ModelError(
                f"protected surface {surface['id']}: unknown claims {', '.join(missing_claims)}"
            )
        _strings(surface["verification_nodes"], f"protected surface {surface['id']} nodes")
        claim_ids.extend(claims)
    require_unique(surface_ids, "protected surface id")

    gaps = value["known_gaps"]
    if not isinstance(gaps, list) or not gaps:
        raise ModelError("protected behavior: known gaps must be non-empty")
    gap_ids: list[str] = []
    for index, gap in enumerate(gaps):
        if not isinstance(gap, dict):
            raise ModelError(f"known gap {index}: expected an object")
        _exact(gap, GAP_KEYS, f"known gap {index}")
        gap_ids.append(gap["id"])
        _strings(gap["required_evidence"], f"known gap {gap['id']} evidence")
    require_unique(gap_ids, "known gap id")

    policy = value["change_policy"]
    if not isinstance(policy, dict):
        raise ModelError("protected behavior: change policy must be an object")
    _exact(policy, POLICY_KEYS, "protected change policy")
    integration = _strings(policy["integration_only_prefixes"], "integration prefixes")
    protected = _strings(policy["protected_prefixes"], "protected prefixes")
    advanced = _strings(policy["advanced_capability_prefixes"], "advanced prefixes")
    _strings(policy["approval_required_when"], "approval requirements")
    reverification = _strings(policy["required_reverification"], "required reverification")
    unknown_nodes = sorted(set(reverification) - set(required_nodes))
    if unknown_nodes:
        raise ModelError(
            "protected behavior: reverification nodes are absent from the baseline: "
            + ", ".join(unknown_nodes)
        )
    return ProtectedBehavior(
        source_revision=revision,
        required_nodes=required_nodes,
        protected_prefixes=protected,
        integration_only_prefixes=integration,
        advanced_capability_prefixes=advanced,
        claim_ids=tuple(sorted(set(claim_ids))),
        known_gap_ids=tuple(gap_ids),
    )
