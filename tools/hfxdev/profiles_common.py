# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from .model import ModelError, require_unique



PROFILE_DIRECTORIES = ("receivers", "children", "surfaces")
WRITE_EVIDENCE_LEVELS = {"hardware-qualified", "production-qualified"}
PUBLIC_EVIDENCE = {"public", "public-summary"}
PROFILE_KEYS = {
    "$schema",
    "schema",
    "profile_id",
    "revision",
    "kind",
    "device_kind",
    "identity",
    "compatibility",
    "transport",
    "qualification_migrations",
    "presentation",
    "capabilities",
    "evidence_claims",
    "restrictions",
}
FORBIDDEN_COMBINATION_KEYS = {
    "keyboard_profile_id",
    "mouse_profile_id",
    "required_children",
    "required_profiles",
    "qualified_pairing",
}
RECEIVER_COMPATIBILITY_KEYS = {
    "protocol_family",
    "supported_child_kinds",
    "exact_child_combinations",
}
CHILD_COMPATIBILITY_KEYS = {
    "receiver_protocols",
    "routes",
    "required_sibling_kinds",
}
SURFACE_COMPATIBILITY_KEYS = {"receiver_protocols", "selection"}
ROUTE_KINDS = {"hyperflux-wireless", "direct-usb", "bluetooth"}
PRESENTATION_KEYS = {
    "upstream_id",
    "owner",
    "project_version",
    "source_commit",
    "model_key",
    "layout_key",
    "transport_variant",
}
PRESENTATION_VARIANT_ROUTES = {
    "wireless": "hyperflux-wireless",
    "wired": "direct-usb",
    "bluetooth": "bluetooth",
}
PASSIVE_KEYS = {
    "endpoint_lane",
    "battery_encoding",
    "contact",
    "route",
    "report_implies_route_available",
}
RECEIVER_TRANSPORT_KEYS = {
    "backend",
    "backend_id",
    "maximum_targets",
    "generation_bound",
    "application_raw_frames",
    "pairing_inventory",
    "native_exchange",
}
NATIVE_EXCHANGE_KEYS = {
    "protocol_family",
    "report_bytes",
    "set_report_index",
    "response_report_index",
    "initial_response_delay_us",
    "response_retry_delay_us",
    "max_response_attempts",
    "report_format",
    "route_selector",
    "match_ranges",
}
NATIVE_EXCHANGE_REQUIRED_KEYS = NATIVE_EXCHANGE_KEYS - {"route_selector"}
NATIVE_MATCH_RANGE_KEYS = {"offset", "length"}
NATIVE_ROUTE_SELECTOR_KEYS = {
    "field",
    "pointer_lane",
    "keyboard_lane",
    "restore_original_response",
}
NATIVE_REPORT_FORMAT_KEYS = {
    "request_status",
    "response_status",
    "protocol_type",
    "data_size",
    "checksum",
    "trailer",
    "transaction_id",
}
NATIVE_BYTE_VALUE_KEYS = {"offset", "value"}
NATIVE_BYTE_RANGE_KEYS = {"offset", "minimum", "maximum"}
NATIVE_DATA_SIZE_KEYS = {"offset", "maximum"}
NATIVE_CHECKSUM_KEYS = {
    "algorithm",
    "start_offset",
    "end_offset_exclusive",
    "result_offset",
}
NATIVE_TRANSACTION_ID_KEYS = {"offset", "match_mask"}
LIFECYCLE_KEYS = {
    "power_off_departure_evidence",
    "return_policy_id",
    "open_evidence",
    "accelerator_evidence",
}
LIFECYCLE_REQUIRED_KEYS = LIFECYCLE_KEYS
POWER_OFF_DEPARTURE_EVIDENCE = {"receiver-state", "watched-retained-route"}
RETURN_EVIDENCE_SOURCES = {
    "battery-report",
    "contact-report",
    "endpoint-activity",
    "route-return",
}
LIFECYCLE_POLICY_KEYS = {
    "id",
    "strategy",
    "qualification",
    "quiescence_candidate_ms",
    "observation_jitter_ms",
    "retry_offsets_ms",
    "allowed_open_evidence",
    "allowed_accelerator_evidence",
    "delivery_semantics",
    "timing_authorities",
}
LIFECYCLE_TIMING_AUTHORITY_KEYS = {"kind", "source_id", "source_sha256", "summary"}
LIFECYCLE_POLICY_STRATEGIES = {"activity-assisted-return-epoch"}
LIFECYCLE_POLICY_QUALIFICATIONS = {"prequalification-candidate", "qualified"}
LIFECYCLE_TIMING_AUTHORITY_KINDS = {
    "pinned-upstream-contract",
    "prequalification-hypothesis",
    "physical-qualification",
}
MAX_RETURN_RETRY_ATTEMPTS = 32
MAX_RETURN_RETRY_WINDOW_MS = 30_000
MAX_QUIESCENCE_CANDIDATE_MS = 900_000
QUALIFICATION_MIGRATION_KEYS = {"from_runtime_sha256", "preserve_stage_roles"}
QUALIFICATION_MIGRATION_STAGE_ROLES = {"lighting", "map", "telemetry"}
PASSIVE_TELEMETRY_CAPABILITIES = {
    "presence.passive-evidence",
    "route.hyperflux-wireless",
    "telemetry.battery-percent",
    "telemetry.connection-evidence",
    "telemetry.mouse-contact",
}
UPSTREAM_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
PRESENTATION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ProfileInputs:
    capabilities: dict[str, Any]
    evidence: dict[str, Any]
    lifecycle_policies: dict[str, Any]
    profiles: tuple[dict[str, Any], ...]
    candidates: tuple[dict[str, Any], ...]
    official_receiver_scope: dict[str, Any]
    source_sha256: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _runtime_profile_digest(
    profile: dict[str, Any],
    capability_index: dict[str, dict[str, Any]],
    lifecycle_policy_index: dict[str, dict[str, Any]],
) -> str:
    identity = profile["identity"]
    runtime_identity = {
        key: identity[key]
        for key in ("authority", "vendor_id", "product_id", "variant_key")
        if key in identity
    }
    transport = deepcopy(profile.get("transport", {}))
    lifecycle = transport.get("lifecycle")
    if lifecycle is not None:
        policy = lifecycle_policy_index[lifecycle["return_policy_id"]]
        lifecycle["resolved_return_policy"] = {
            "strategy": policy["strategy"],
            "quiescence_candidate_ms": policy["quiescence_candidate_ms"],
            "observation_jitter_ms": policy["observation_jitter_ms"],
            "retry_offsets_ms": policy["retry_offsets_ms"],
            "delivery_semantics": policy["delivery_semantics"],
        }
    runtime_contract = {
        "schema": "hyperflux-runtime-profile-binding-v1",
        "profile_id": profile["profile_id"],
        "revision": profile["revision"],
        "kind": profile["kind"],
        "device_kind": profile["device_kind"],
        "identity": runtime_identity,
        "compatibility": profile["compatibility"],
        "transport": transport,
        "capabilities": [
            {
                "id": capability["id"],
                "support_level": capability["support_level"],
                "access": capability_index[capability["id"]]["access"],
            }
            for capability in profile["capabilities"]
        ],
    }
    return hashlib.sha256(_canonical_bytes(runtime_contract)).hexdigest()


def _expect_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    extras = sorted(set(value) - allowed)
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _expect_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"{label}: expected a non-empty string")
    return value


def _expect_string_list(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ModelError(f"{label}: expected a {'non-empty ' if not allow_empty else ''}list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ModelError(f"{label}: every item must be a non-empty string")
    require_unique(value, label)
    return value


def _expect_unique_string_list(
    value: Any, label: str, *, allow_empty: bool = True
) -> list[str]:
    result = _expect_string_list(value, label, allow_empty=allow_empty)
    if result != sorted(result):
        raise ModelError(f"{label}: values must be sorted")
    return result


def _walk_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for child in value.values():
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys
