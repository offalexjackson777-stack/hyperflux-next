# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .integrations import upstream_index
from .model import ModelError, load_json, require_unique
from .profiles_capability import (
    _validate_candidates,
    _validate_capabilities,
    _validate_compatibility,
)
from .profiles_common import (
    FORBIDDEN_COMBINATION_KEYS,
    PROFILE_KEYS,
    PUBLIC_EVIDENCE,
    WRITE_EVIDENCE_LEVELS,
    ProfileInputs,
    _expect_keys,
    _expect_nonempty_string,
    _expect_string_list,
    _runtime_profile_digest,
    _walk_keys,
)
from .profiles_evidence import _load_documents, _source_digest, _validate_evidence
from .profiles_lifecycle import (
    _validate_lifecycle_policy_catalog,
    _validate_qualification_migrations,
)
from .profiles_presentation import (
    _validate_presentation,
    _validate_presentation_sources,
)
from .profiles_telemetry import _validate_lighting, _validate_passive


def _validate_profiles(
    profiles: list[dict[str, Any]],
    capability_index: dict[str, dict[str, Any]],
    claim_index: dict[str, dict[str, Any]],
    lifecycle_policy_index: dict[str, dict[str, Any]],
) -> None:
    profile_ids = [profile["profile_id"] for profile in profiles]
    require_unique(profile_ids, "hardware profile id")
    domain_kinds = {"receiver", "child", "surface"}
    device_kinds = {"receiver", "mat", "mouse", "keyboard"}
    identities: list[str] = []
    for profile in profiles:
        identifier = profile["profile_id"]
        forbidden = sorted(_walk_keys(profile) & FORBIDDEN_COMBINATION_KEYS)
        if forbidden:
            raise ModelError(f"{identifier}: exact-combination keys are forbidden: {', '.join(forbidden)}")
        _expect_keys(profile, PROFILE_KEYS | {"_source_path"}, identifier)
        if profile.get("schema") != "hyperflux-hardware-profile-v1":
            raise ModelError(f"{identifier}: unsupported profile schema")
        kind = profile.get("kind")
        if kind not in domain_kinds or not identifier.startswith(f"{kind}."):
            raise ModelError(f"{identifier}: profile kind and id disagree")
        device_kind = profile.get("device_kind")
        if device_kind not in device_kinds:
            raise ModelError(f"{identifier}: invalid device kind")
        if (kind == "receiver") != (device_kind == "receiver"):
            raise ModelError(f"{identifier}: receiver kind mismatch")
        if (kind == "surface") != (device_kind == "mat"):
            raise ModelError(f"{identifier}: surface kind mismatch")
        if kind == "child" and device_kind not in {"mouse", "keyboard"}:
            raise ModelError(f"{identifier}: child must be a mouse or keyboard")
        _validate_compatibility(profile)
        revision = profile.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ModelError(f"{identifier}: invalid revision")
        identity = profile.get("identity")
        if not isinstance(identity, dict):
            raise ModelError(f"{identifier}: identity is required")
        _expect_nonempty_string(identity.get("manufacturer"), f"{identifier} manufacturer")
        _expect_nonempty_string(identity.get("model_name"), f"{identifier} model name")
        if kind in {"receiver", "child"}:
            for field in ("vendor_id", "product_id"):
                value = identity.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65_535:
                    raise ModelError(f"{identifier}: {field} must be a 16-bit integer")
            identities.append(f"{kind}:{device_kind}:{identity['vendor_id']:04x}:{identity['product_id']:04x}")
        elif "vendor_id" in identity or "product_id" in identity:
            raise ModelError(f"{identifier}: a surface variant must not invent USB identity")
        if kind == "receiver":
            transport = profile.get("transport", {})
            if transport.get("application_raw_frames") is not False:
                raise ModelError(f"{identifier}: applications must not supply raw receiver frames")
        _validate_presentation(profile)
        capabilities = profile.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities:
            raise ModelError(f"{identifier}: profile capabilities are required")
        capability_ids = [capability.get("id") for capability in capabilities]
        if any(not isinstance(value, str) for value in capability_ids):
            raise ModelError(f"{identifier}: invalid capability id")
        require_unique(capability_ids, f"{identifier} capability id")
        if capability_ids != sorted(capability_ids):
            raise ModelError(f"{identifier}: capabilities must be sorted by id")
        profile_claims = set(_expect_string_list(profile.get("evidence_claims"), f"{identifier} evidence", allow_empty=False))
        unknown_claims = profile_claims - set(claim_index)
        if unknown_claims:
            raise ModelError(f"{identifier}: unknown evidence claims: {', '.join(sorted(unknown_claims))}")
        for claim_id in profile_claims:
            if identifier not in claim_index[claim_id]["profile_ids"]:
                raise ModelError(f"{identifier}: evidence claim has a different scope: {claim_id}")
        for capability in capabilities:
            capability_id = capability["id"]
            definition = capability_index.get(capability_id)
            if definition is None:
                raise ModelError(f"{identifier}: unknown capability {capability_id}")
            if kind not in definition["profile_kinds"]:
                raise ModelError(f"{identifier}: capability {capability_id} is invalid for {kind}")
            refs = set(_expect_string_list(capability.get("evidence_claims"), f"{identifier} {capability_id} evidence", allow_empty=False))
            if not refs <= profile_claims:
                raise ModelError(f"{identifier}: capability evidence is absent from the profile evidence set")
            qualifying = []
            for claim_id in refs:
                claim = claim_index.get(claim_id)
                if claim is None or identifier not in claim["profile_ids"]:
                    raise ModelError(f"{identifier}: invalid evidence scope for {capability_id}")
                if capability_id in claim["qualifies_capabilities"]:
                    qualifying.append(claim)
            if not qualifying:
                raise ModelError(f"{identifier}: no evidence claim qualifies {capability_id}")
            if definition["access"] == "write" and not any(
                claim["evidence_level"] in WRITE_EVIDENCE_LEVELS and claim["privacy"] in PUBLIC_EVIDENCE
                for claim in qualifying
            ):
                raise ModelError(f"{identifier}: writable capability lacks public physical qualification: {capability_id}")
        if kind == "surface" and any(capability_index[item["id"]]["access"] == "write" for item in capabilities):
            raise ModelError(f"{identifier}: surface profile exposes an unqualified write")
        _validate_lighting(profile)
        _validate_passive(profile, lifecycle_policy_index)
        _validate_qualification_migrations(
            profile, capability_index, lifecycle_policy_index
        )
    require_unique(identities, "profile hardware identity")


def load_profile_inputs(root: Path) -> ProfileInputs:
    capabilities = load_json(root / "profiles" / "capabilities.json")
    evidence = load_json(root / "profiles" / "evidence" / "claims.json")
    lifecycle_policies = load_json(root / "profiles" / "lifecycle-policies.json")
    documents, profiles, candidate_catalogs = _load_documents(root)
    documents.extend(
        [
            ("profiles/capabilities.json", capabilities),
            ("profiles/evidence/claims.json", evidence),
            ("profiles/lifecycle-policies.json", lifecycle_policies),
        ]
    )
    capability_index = _validate_capabilities(capabilities)
    lifecycle_policy_index = _validate_lifecycle_policy_catalog(lifecycle_policies)
    snapshot_ids, candidates, official_receiver_scope = _validate_candidates(
        candidate_catalogs
    )
    profile_ids = {profile.get("profile_id") for profile in profiles if isinstance(profile.get("profile_id"), str)}
    claims = _validate_evidence(
        root,
        evidence,
        load_json(root / "migration" / "sources.json"),
        set(capability_index),
        profile_ids | snapshot_ids,
    )
    for catalog in candidate_catalogs:
        claim = claims.get(catalog["source_claim"])
        if claim is None or catalog["snapshot_id"] not in claim["profile_ids"]:
            raise ModelError(f"{catalog['snapshot_id']}: source claim does not cover candidate snapshot")
        if claim["qualifies_capabilities"]:
            raise ModelError(f"{catalog['snapshot_id']}: candidate source claim must not qualify capabilities")
    _validate_profiles(profiles, capability_index, claims, lifecycle_policy_index)
    _validate_presentation_sources(profiles, upstream_index(root))
    return ProfileInputs(
        capabilities=capabilities,
        evidence=evidence,
        lifecycle_policies=lifecycle_policies,
        profiles=tuple(sorted(profiles, key=lambda value: value["profile_id"])),
        candidates=tuple(sorted(candidates, key=lambda value: (value["snapshot_id"], value["id"]))),
        official_receiver_scope=official_receiver_scope,
        source_sha256=_source_digest(documents),
    )


def compiled_catalog(root: Path) -> dict[str, Any]:
    inputs = load_profile_inputs(root)
    capability_index = {item["id"]: item for item in inputs.capabilities["capabilities"]}
    lifecycle_policy_index = {
        item["id"]: item for item in inputs.lifecycle_policies["policies"]
    }
    profiles: list[dict[str, Any]] = []
    for profile in inputs.profiles:
        compiled = deepcopy({key: value for key, value in profile.items() if key != "$schema"})
        compiled["source_path"] = compiled.pop("_source_path")
        for capability in compiled["capabilities"]:
            capability["access"] = capability_index[capability["id"]]["access"]
        compiled["runtime_sha256"] = _runtime_profile_digest(
            profile, capability_index, lifecycle_policy_index
        )
        profiles.append(compiled)
    return {
        "schema": "hyperflux-compiled-profile-catalog-v1",
        "source_sha256": inputs.source_sha256,
        "composition_policy": {
            "receiver_child_profiles_are_independent": True,
            "surface_variants_are_not_usb_identities": True,
            "unknown_children_are_read_only": True,
            "unknown_children_writable_capabilities": [],
            "sibling_profile_required": False,
        },
        "capabilities": deepcopy(inputs.capabilities["capabilities"]),
        "evidence_claims": deepcopy(inputs.evidence["claims"]),
        "lifecycle_policies": deepcopy(inputs.lifecycle_policies["policies"]),
        "profiles": profiles,
        "candidates": deepcopy(list(inputs.candidates)),
        "official_receiver_scope": deepcopy(inputs.official_receiver_scope),
    }


def composition_fixtures(root: Path) -> dict[str, Any]:
    catalog = compiled_catalog(root)
    receivers = [profile for profile in catalog["profiles"] if profile["kind"] == "receiver"]
    children = [profile for profile in catalog["profiles"] if profile["kind"] == "child"]
    surfaces = [profile for profile in catalog["profiles"] if profile["kind"] == "surface"]
    cases: list[dict[str, Any]] = []
    for receiver in receivers:
        cases.append(
            {
                "id": f"{receiver['profile_id']}:receiver-only",
                "receiver_profile_id": receiver["profile_id"],
                "child_profile_ids": [],
                "surface_profile_id": None,
                "expected_writable_children": 0,
            }
        )
        for child in children:
            writable = sorted(item["id"] for item in child["capabilities"] if item["access"] == "write")
            cases.append(
                {
                    "id": f"{receiver['profile_id']}:{child['device_kind']}-only",
                    "receiver_profile_id": receiver["profile_id"],
                    "child_profile_ids": [child["profile_id"]],
                    "surface_profile_id": None,
                    "expected_writable_capabilities": {child["profile_id"]: writable},
                    "expected_writable_children": 1,
                }
            )
        cases.append(
            {
                "id": f"{receiver['profile_id']}:all-qualified-children",
                "receiver_profile_id": receiver["profile_id"],
                "child_profile_ids": sorted(child["profile_id"] for child in children),
                "surface_profile_id": surfaces[0]["profile_id"] if surfaces else None,
                "expected_writable_children": len(children),
            }
        )
        cases.append(
            {
                "id": f"{receiver['profile_id']}:unknown-child",
                "receiver_profile_id": receiver["profile_id"],
                "unknown_child": {"device_kind": "unknown", "product_id": 65_535},
                "child_profile_ids": [],
                "surface_profile_id": None,
                "expected_unknown_writable_capabilities": [],
                "expected_writable_children": 0,
            }
        )
    return {
        "schema": "hyperflux-generated-profile-compositions-v1",
        "source_sha256": catalog["source_sha256"],
        "cases": cases,
    }
