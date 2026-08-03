# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
from typing import Any

from .atlas import load_repository_atlas
from .knowledge import compiled_knowledge_catalog
from .release import load_release_context


def public_readiness(root: Path) -> dict[str, Any]:
    release = load_release_context(root)
    gates = release.gates
    baseline = release.installed_baseline
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
    hardware_gate_label = "gate" if hardware_remaining == 1 else "gates"
    lifecycle_gate_label = "gate" if lifecycle_remaining == 1 else "gates"
    return {
        "$schema": "../schemas/public-readiness.schema.json",
        "schema": "hyperflux-public-readiness-v2",
        "generated_from": [
            "architecture/repository-atlas.json",
            "assurance/installed-baselines.json",
            "assurance/release-gates.json",
            "generated/knowledge/catalog.json",
        ],
        "publication": {
            "state": "locked",
            "label": "Unreleased",
            "summary": "Release 8 is an accepted private baseline; reconstructed 2.0 artifacts and publication remain unauthorized.",
        },
        "baseline": {
            "state": "ready",
            "label": "Behavioral baseline",
            "summary": "Exact installed release 8 is frozen, verified, and accepted as the private reconstruction oracle.",
            "id": baseline.id,
            "package_version": baseline.package_version,
            "artifact_frozen": baseline.artifact_frozen,
            "installed_verified": baseline.installed_verified,
            "behavioral_baseline_accepted": baseline.behavioral_baseline_accepted,
            "public_release_authorized": baseline.public_release_authorized,
        },
        "software": {
            "state": "partial" if gates_ready < len(gates) else "ready",
            "label": "2.0 software verification",
            "gates_ready": gates_ready,
            "gates_total": len(gates),
            "summary": f"{gates_ready} of {len(gates)} release gates are ready in software.",
        },
        "hardware": {
            "state": "partial" if qualified_routes else "unknown",
            "label": "Hardware qualification",
            "qualified_routes": qualified_routes,
            "research_candidates": research_candidates,
            "summary": f"{qualified_routes} release-8 reference routes have bounded physical evidence; {research_candidates} candidates remain research only.",
        },
        "evidence": {
            "state": "blocked" if hardware_remaining or lifecycle_remaining else "ready",
            "label": "Remaining evidence",
            "hardware_gates": hardware_remaining,
            "lifecycle_gates": lifecycle_remaining,
            "reviewed_facts": reviewed_facts,
            "known_gaps": known_gaps,
            "summary": (
                f"The 2.0 target retains {hardware_remaining} physical "
                f"{hardware_gate_label} and {lifecycle_remaining} lifecycle "
                f"{lifecycle_gate_label}; baseline evidence is not target authorization."
            ),
        },
        "repository": {
            "state": "ready",
            "label": "Repository map",
            "atlas_subsystems": len(atlas.nodes),
            "summary": f"The Repository Atlas owns {len(atlas.nodes)} subsystem records and their generated projections.",
        },
        "qualification_console_access": "loopback-only",
    }
