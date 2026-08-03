# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from typing import Any

from .model import ModelError, require_unique
from .profiles_common import (
    DIGEST_PATTERN,
    LIFECYCLE_KEYS,
    LIFECYCLE_POLICY_KEYS,
    LIFECYCLE_POLICY_QUALIFICATIONS,
    LIFECYCLE_POLICY_STRATEGIES,
    LIFECYCLE_REQUIRED_KEYS,
    LIFECYCLE_TIMING_AUTHORITY_KEYS,
    LIFECYCLE_TIMING_AUTHORITY_KINDS,
    MAX_QUIESCENCE_CANDIDATE_MS,
    MAX_RETURN_RETRY_ATTEMPTS,
    MAX_RETURN_RETRY_WINDOW_MS,
    PASSIVE_TELEMETRY_CAPABILITIES,
    POWER_OFF_DEPARTURE_EVIDENCE,
    QUALIFICATION_MIGRATION_KEYS,
    QUALIFICATION_MIGRATION_STAGE_ROLES,
    RETURN_EVIDENCE_SOURCES,
    _expect_keys,
    _expect_nonempty_string,
    _expect_unique_string_list,
    _runtime_profile_digest,
)


def _validate_lifecycle_policy_catalog(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if catalog.get("schema") != "hyperflux-lifecycle-policy-catalog-v1":
        raise ModelError("unsupported lifecycle policy catalog schema")
    _expect_keys(catalog, {"$schema", "schema", "policies"}, "lifecycle policy catalog")
    policies = catalog.get("policies")
    if not isinstance(policies, list) or not policies:
        raise ModelError("lifecycle policy catalog is empty")
    identifiers: list[str] = []
    for policy in policies:
        if not isinstance(policy, dict):
            raise ModelError("lifecycle policy must be an object")
        _expect_keys(policy, LIFECYCLE_POLICY_KEYS, "lifecycle policy")
        missing = sorted(LIFECYCLE_POLICY_KEYS - set(policy))
        if missing:
            raise ModelError(f"incomplete lifecycle policy: {', '.join(missing)}")
        identifier = _expect_nonempty_string(policy["id"], "lifecycle policy id")
        identifiers.append(identifier)
        if policy["strategy"] not in LIFECYCLE_POLICY_STRATEGIES:
            raise ModelError(f"{identifier}: unsupported lifecycle strategy")
        qualification = policy["qualification"]
        if qualification not in LIFECYCLE_POLICY_QUALIFICATIONS:
            raise ModelError(f"{identifier}: invalid lifecycle qualification")
        quiescence_ms = policy["quiescence_candidate_ms"]
        if (
            isinstance(quiescence_ms, bool)
            or not isinstance(quiescence_ms, int)
            or not 1_000 <= quiescence_ms <= MAX_QUIESCENCE_CANDIDATE_MS
        ):
            raise ModelError(f"{identifier}: quiescence candidate is out of bounds")
        observation_jitter_ms = policy["observation_jitter_ms"]
        if (
            isinstance(observation_jitter_ms, bool)
            or not isinstance(observation_jitter_ms, int)
            or not 0 <= observation_jitter_ms <= 5_000
            or observation_jitter_ms >= quiescence_ms
        ):
            raise ModelError(f"{identifier}: observation jitter tolerance is out of bounds")
        offsets = policy["retry_offsets_ms"]
        if (
            not isinstance(offsets, list)
            or not 2 <= len(offsets) <= MAX_RETURN_RETRY_ATTEMPTS
            or any(isinstance(value, bool) or not isinstance(value, int) for value in offsets)
            or offsets != sorted(set(offsets))
            or offsets[0] != 0
            or offsets[-1] > MAX_RETURN_RETRY_WINDOW_MS
        ):
            raise ModelError(
                f"{identifier}: retry offsets must be unique, increasing, begin at zero, and remain bounded"
            )
        open_evidence = _expect_unique_string_list(
            policy["allowed_open_evidence"],
            f"{identifier} allowed open evidence",
            allow_empty=False,
        )
        accelerator_evidence = _expect_unique_string_list(
            policy["allowed_accelerator_evidence"],
            f"{identifier} allowed accelerator evidence",
            allow_empty=False,
        )
        if set(open_evidence) - RETURN_EVIDENCE_SOURCES or set(accelerator_evidence) - RETURN_EVIDENCE_SOURCES:
            raise ModelError(f"{identifier}: unsupported return evidence source")
        if not set(open_evidence) <= set(accelerator_evidence):
            raise ModelError(f"{identifier}: opening evidence must also accelerate an active epoch")
        if policy["delivery_semantics"] != "receiver-delivered-not-child-applied":
            raise ModelError(f"{identifier}: lifecycle delivery semantics overclaim child application")
        authorities = policy["timing_authorities"]
        if not isinstance(authorities, list) or len(authorities) < 2:
            raise ModelError(f"{identifier}: timing values require explicit authorities")
        authority_kinds: set[str] = set()
        for authority in authorities:
            if not isinstance(authority, dict):
                raise ModelError(f"{identifier}: timing authority must be an object")
            _expect_keys(authority, LIFECYCLE_TIMING_AUTHORITY_KEYS, f"{identifier} timing authority")
            missing_authority = {"kind", "source_id", "summary"} - set(authority)
            if missing_authority:
                raise ModelError(
                    f"{identifier}: incomplete timing authority: {', '.join(sorted(missing_authority))}"
                )
            kind = authority["kind"]
            if kind not in LIFECYCLE_TIMING_AUTHORITY_KINDS:
                raise ModelError(f"{identifier}: unsupported timing authority kind")
            authority_kinds.add(kind)
            _expect_nonempty_string(authority["source_id"], f"{identifier} timing source")
            _expect_nonempty_string(authority["summary"], f"{identifier} timing summary")
            source_sha256 = authority.get("source_sha256")
            if source_sha256 is not None and (
                not isinstance(source_sha256, str)
                or DIGEST_PATTERN.fullmatch(source_sha256) is None
            ):
                raise ModelError(f"{identifier}: timing source digest must be lowercase SHA-256")
            if kind == "pinned-upstream-contract" and source_sha256 is None:
                raise ModelError(f"{identifier}: pinned timing authority requires a source digest")
        if "pinned-upstream-contract" not in authority_kinds:
            raise ModelError(f"{identifier}: quiescence timing lacks a pinned upstream contract")
        if qualification == "prequalification-candidate":
            if "prequalification-hypothesis" not in authority_kinds:
                raise ModelError(f"{identifier}: candidate retry timing lacks an explicit hypothesis")
        elif "physical-qualification" not in authority_kinds:
            raise ModelError(f"{identifier}: qualified retry timing lacks physical evidence")
    require_unique(identifiers, "lifecycle policy id")
    if identifiers != sorted(identifiers):
        raise ModelError("lifecycle policies must be sorted by id")
    return {policy["id"]: policy for policy in policies}


def _validate_lifecycle_policy(
    profile: dict[str, Any],
    transport: dict[str, Any],
    passive: object,
    lifecycle_policy_index: dict[str, dict[str, Any]],
) -> None:
    lifecycle = transport.get("lifecycle")
    if lifecycle is None:
        return
    if not isinstance(lifecycle, dict):
        raise ModelError(f"{profile['profile_id']}: lifecycle policy must be an object")
    _expect_keys(lifecycle, LIFECYCLE_KEYS, f"{profile['profile_id']} lifecycle policy")
    missing = sorted(LIFECYCLE_REQUIRED_KEYS - set(lifecycle))
    if missing:
        raise ModelError(
            f"{profile['profile_id']}: incomplete lifecycle policy: {', '.join(missing)}"
        )
    departure = lifecycle["power_off_departure_evidence"]
    if departure not in POWER_OFF_DEPARTURE_EVIDENCE:
        raise ModelError(f"{profile['profile_id']}: unsupported power-off departure evidence")
    policy_id = lifecycle["return_policy_id"]
    policy = lifecycle_policy_index.get(policy_id)
    if policy is None:
        raise ModelError(f"{profile['profile_id']}: unknown return policy: {policy_id}")
    open_evidence = _expect_unique_string_list(
        lifecycle["open_evidence"],
        f"{profile['profile_id']} return opening evidence",
        allow_empty=False,
    )
    accelerator_evidence = _expect_unique_string_list(
        lifecycle["accelerator_evidence"],
        f"{profile['profile_id']} return accelerator evidence",
        allow_empty=False,
    )
    if not set(open_evidence) <= set(policy["allowed_open_evidence"]):
        raise ModelError(f"{profile['profile_id']}: opening evidence is not allowed by its return policy")
    if not set(accelerator_evidence) <= set(policy["allowed_accelerator_evidence"]):
        raise ModelError(
            f"{profile['profile_id']}: accelerator evidence is not allowed by its return policy"
        )
    if not set(open_evidence) <= set(accelerator_evidence):
        raise ModelError(
            f"{profile['profile_id']}: opening evidence must also accelerate an active epoch"
        )
    if departure == "watched-retained-route":
        if (
            not isinstance(passive, dict)
            or not passive["report_implies_route_available"]
            or passive["route"]["unavailable"]
        ):
            raise ModelError(
                f"{profile['profile_id']}: retained-route departure contradicts passive route evidence"
            )
    evidence = set(open_evidence) | set(accelerator_evidence)
    if evidence & {"battery-report", "contact-report", "endpoint-activity", "route-return"} and not isinstance(passive, dict):
        raise ModelError(f"{profile['profile_id']}: return evidence requires a passive decoder")
    if "contact-report" in evidence and (
        profile["device_kind"] != "mouse"
        or passive["contact"] is None
        or not passive["contact"]["on_mat"]
    ):
        raise ModelError(f"{profile['profile_id']}: contact return evidence lacks a decoder")
    if "route-return" in evidence and not passive["route"]["unavailable"]:
        raise ModelError(f"{profile['profile_id']}: route-return evidence lacks departure values")


def _validate_qualification_migrations(
    profile: dict[str, Any],
    capability_index: dict[str, dict[str, Any]],
    lifecycle_policy_index: dict[str, dict[str, Any]],
) -> None:
    migrations = profile.get("qualification_migrations")
    if migrations is None:
        return
    identifier = profile["profile_id"]
    if profile["kind"] != "child":
        raise ModelError(f"{identifier}: qualification migrations are child-profile only")
    if profile["revision"] < 2:
        raise ModelError(f"{identifier}: qualification migrations require a revised profile")
    if not isinstance(migrations, list) or not migrations:
        raise ModelError(f"{identifier}: qualification migrations must be a non-empty list")

    capability_ids = {item["id"] for item in profile["capabilities"]}
    available_roles: set[str] = set()
    if capability_ids & PASSIVE_TELEMETRY_CAPABILITIES:
        available_roles.add("telemetry")
    if capability_ids & {"lighting.per-key", "lighting.per-led"}:
        available_roles.add("map")
    if capability_ids & {
        "lighting.brightness",
        "lighting.complete-black",
        "lighting.off",
        "lighting.software-effect-frames",
        "lighting.static",
    }:
        available_roles.add("lighting")

    current_digest = _runtime_profile_digest(
        profile, capability_index, lifecycle_policy_index
    )
    predecessors: list[str] = []
    for index, migration in enumerate(migrations):
        label = f"{identifier} qualification migration {index}"
        if not isinstance(migration, dict):
            raise ModelError(f"{label}: expected an object")
        _expect_keys(migration, QUALIFICATION_MIGRATION_KEYS, label)
        missing = sorted(QUALIFICATION_MIGRATION_KEYS - set(migration))
        if missing:
            raise ModelError(f"{label}: incomplete migration: {', '.join(missing)}")
        predecessor = migration["from_runtime_sha256"]
        if not isinstance(predecessor, str) or DIGEST_PATTERN.fullmatch(predecessor) is None:
            raise ModelError(f"{label}: predecessor must be a lowercase SHA-256 digest")
        if predecessor == current_digest:
            raise ModelError(f"{label}: predecessor already equals the current runtime profile")
        roles = _expect_unique_string_list(
            migration["preserve_stage_roles"],
            f"{label} preserved stage roles",
            allow_empty=False,
        )
        unknown_roles = sorted(set(roles) - QUALIFICATION_MIGRATION_STAGE_ROLES)
        if unknown_roles:
            raise ModelError(f"{label}: unsupported stage roles: {', '.join(unknown_roles)}")
        unavailable_roles = sorted(set(roles) - available_roles)
        if unavailable_roles:
            raise ModelError(
                f"{label}: profile does not expose stages for: {', '.join(unavailable_roles)}"
            )
        predecessors.append(predecessor)
    require_unique(predecessors, f"{identifier} qualification migration predecessor")
    if predecessors != sorted(predecessors):
        raise ModelError(f"{identifier}: qualification migrations must be sorted by predecessor")
