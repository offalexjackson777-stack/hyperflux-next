# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
from typing import Any

from .atlas import load_repository_atlas
from .knowledge import compiled_knowledge_catalog
from .release import load_release_gates


def public_readiness(root: Path) -> dict[str, Any]:
    gates = load_release_gates(root)
    knowledge = compiled_knowledge_catalog(root)
    atlas = load_repository_atlas(root)
    candidates = knowledge["candidates"]
    gates_ready = sum(gate.status == "software-satisfied" for gate in gates)
    hardware_remaining = sum(
        gate.status == "blocked-by-physical-evidence" for gate in gates
    )
    lifecycle_remaining = sum(
        gate.status == "blocked-by-lifecycle-evidence" for gate in gates
    )
    qualified_routes = sum(
        candidate["hyperflux_support"] == "route-qualified" for candidate in candidates
    )
    research_candidates = sum(
        candidate["hyperflux_support"] == "candidate-only" for candidate in candidates
    )
    reviewed_facts = sum(len(candidate["reviewed_facts"]) for candidate in candidates)
    known_gaps = sum(len(candidate["knowledge_gaps"]) for candidate in candidates)
    return {
        "$schema": "../schemas/public-readiness.schema.json",
        "schema": "hyperflux-public-readiness-v1",
        "generated_from": [
            "architecture/repository-atlas.json",
            "assurance/release-gates.json",
            "generated/knowledge/catalog.json",
        ],
        "publication": {
            "state": "locked",
            "label": "Unreleased",
            "summary": "Public source is available for review; no supported product release or package channel exists.",
        },
        "software": {
            "state": "partial" if gates_ready < len(gates) else "ready",
            "label": "Software verification",
            "gates_ready": gates_ready,
            "gates_total": len(gates),
            "summary": f"{gates_ready} of {len(gates)} release gates are ready in software.",
        },
        "hardware": {
            "state": "partial" if qualified_routes else "unknown",
            "label": "Hardware qualification",
            "qualified_routes": qualified_routes,
            "research_candidates": research_candidates,
            "summary": f"{qualified_routes} receiver routes have bounded physical evidence; {research_candidates} candidates remain research only.",
        },
        "evidence": {
            "state": "blocked" if hardware_remaining or lifecycle_remaining else "ready",
            "label": "Remaining evidence",
            "hardware_gates": hardware_remaining,
            "lifecycle_gates": lifecycle_remaining,
            "reviewed_facts": reviewed_facts,
            "known_gaps": known_gaps,
            "summary": f"{hardware_remaining} hardware gate and {lifecycle_remaining} lifecycle gate remain; known gaps stay explicit.",
        },
        "repository": {
            "state": "ready",
            "label": "Repository map",
            "atlas_subsystems": len(atlas.nodes),
            "summary": f"The Repository Atlas owns {len(atlas.nodes)} subsystem records and their generated projections.",
        },
        "portal_hardware_access": "none",
    }
