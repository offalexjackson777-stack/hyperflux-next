# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
from typing import Any

from .arch_repository import load_arch_repository_policy, published_arch_release
from .atlas import load_repository_atlas
from .knowledge import compiled_knowledge_catalog
from .model import CATALOG_ROOT
from .release import load_release_context
from .source_validated import load_source_validated_catalog
from .source_validated_catalog import (
    PHYSICALLY_PROVEN,
    SOURCE_QUALIFIED_OPERATIONAL,
    UPSTREAM_BLOCKED_OR_RESEARCH_ONLY,
    public_support_tier_counts,
)


def public_readiness(root: Path) -> dict[str, Any]:
    release = load_release_context(root)
    gates = release.gates
    knowledge = compiled_knowledge_catalog(root)
    atlas = load_repository_atlas(root)
    candidates = knowledge["candidates"]
    support_counts = public_support_tier_counts(
        load_source_validated_catalog(root)["devices"]
    )
    arch_repository = load_arch_repository_policy(root)
    gates_ready = sum(gate.status == "verified" for gate in gates)
    hardware_remaining = sum(
        gate.status == "blocked-by-physical-evidence" for gate in gates
    )
    lifecycle_remaining = sum(
        gate.status == "blocked-by-lifecycle-evidence" for gate in gates
    )
    reviewed_facts = sum(len(candidate["reviewed_facts"]) for candidate in candidates)
    known_gaps = sum(len(candidate["knowledge_gaps"]) for candidate in candidates)
    hardware_gate_label = "gate" if hardware_remaining == 1 else "gates"
    lifecycle_gate_label = "gate" if lifecycle_remaining == 1 else "gates"
    published = atlas.publication_state == "public-arch-release"
    return {
        "$schema": "../../schemas/public-readiness.schema.json",
        "schema": "hyperflux-public-readiness-v3",
        "generated_from": [
            "architecture/repository-atlas.json",
            "assurance/release-gates.json",
            "assurance/arch-repository.json",
            (CATALOG_ROOT / "knowledge" / "catalog.json").as_posix(),
            "integrations/native-route/source-validated.json",
        ],
        "publication": {
            "state": "ready" if published else "locked",
            "label": (
                f"Arch {arch_repository.product_release_tag[1:]} release"
                if published
                else "Pre-release"
            ),
            "summary": (
                "Arch Linux x86_64 Core and Full "
                f"{published_arch_release(arch_repository)} packages are published "
                "in the human-facing GitHub Release "
                f"{arch_repository.product_release_tag}. The signed HyperFlux "
                "Next third-party Pacman repository is served separately through "
                f"the fixed `{arch_repository.release_tag}` machine channel."
                if published
                else "The source is available for review; no product release, version "
                "tag, or package channel is published."
            ),
        },
        "software": {
            "state": "partial" if gates_ready < len(gates) else "ready",
            "label": "2.0 verification",
            "gates_ready": gates_ready,
            "gates_total": len(gates),
            "summary": (
                f"{gates_ready} of {len(gates)} release gates are verified; "
                "publication remains a separate decision."
            ),
        },
        "hardware": {
            "state": "ready" if support_counts[PHYSICALLY_PROVEN] else "unknown",
            "label": "Hardware support tiers",
            "physically_proven": support_counts[PHYSICALLY_PROVEN],
            "source_qualified_operational": support_counts[
                SOURCE_QUALIFIED_OPERATIONAL
            ],
            "upstream_blocked_or_research_only": support_counts[
                UPSTREAM_BLOCKED_OR_RESEARCH_ONLY
            ],
            "summary": (
                f"{support_counts[PHYSICALLY_PROVEN]} devices are physically proven, "
                f"{support_counts[SOURCE_QUALIFIED_OPERATIONAL]} are source-qualified "
                "operational candidates, and "
                f"{support_counts[UPSTREAM_BLOCKED_OR_RESEARCH_ONLY]} are upstream-blocked "
                "or research-only."
            ),
        },
        "evidence": {
            "state": "blocked" if hardware_remaining or lifecycle_remaining else "ready",
            "label": "Remaining evidence",
            "hardware_gates": hardware_remaining,
            "lifecycle_gates": lifecycle_remaining,
            "reviewed_facts": reviewed_facts,
            "known_gaps": known_gaps,
            "summary": (
                f"The 2.0 release retains {hardware_remaining} physical "
                f"{hardware_gate_label} and {lifecycle_remaining} lifecycle "
                f"{lifecycle_gate_label}; publication was authorized separately from "
                "technical verification."
                if published
                else f"The 2.0 candidate retains {hardware_remaining} physical "
                f"{hardware_gate_label} and {lifecycle_remaining} lifecycle "
                f"{lifecycle_gate_label}; technical evidence does not authorize publication."
            ),
        },
        "repository": {
            "state": "ready",
            "label": "Repository map",
            "atlas_subsystems": len(atlas.nodes),
            "summary": (
                f"The Repository Atlas owns {len(atlas.nodes)} subsystem records and "
                "their generated projections."
            ),
        },
        "qualification_console_access": "loopback-only",
    }
