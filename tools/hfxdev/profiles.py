# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .integrations import upstream_index
from .model import ModelError, load_json, require_unique


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
    source_sha256: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _source_digest(documents: list[tuple[str, dict[str, Any]]]) -> str:
    digest = hashlib.sha256()
    for relative, document in sorted(documents):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_canonical_bytes(document))
        digest.update(b"\0")
    return digest.hexdigest()


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


def _validate_compatibility(profile: dict[str, Any]) -> None:
    identifier = profile["profile_id"]
    kind = profile["kind"]
    compatibility = profile.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ModelError(f"{identifier}: compatibility is required")
    if kind == "receiver":
        _expect_keys(compatibility, RECEIVER_COMPATIBILITY_KEYS, f"{identifier} compatibility")
        _expect_nonempty_string(
            compatibility.get("protocol_family"), f"{identifier} protocol family"
        )
        child_kinds = _expect_unique_string_list(
            compatibility.get("supported_child_kinds"),
            f"{identifier} supported child kinds",
            allow_empty=False,
        )
        if any(value not in {"keyboard", "mouse"} for value in child_kinds):
            raise ModelError(f"{identifier}: unsupported child kind")
        if compatibility.get("exact_child_combinations") is not False:
            raise ModelError(f"{identifier}: receiver must compose children independently")
        return
    expected = CHILD_COMPATIBILITY_KEYS if kind == "child" else SURFACE_COMPATIBILITY_KEYS
    _expect_keys(compatibility, expected, f"{identifier} compatibility")
    _expect_unique_string_list(
        compatibility.get("receiver_protocols"),
        f"{identifier} receiver protocols",
        allow_empty=False,
    )
    if kind == "surface":
        if compatibility.get("selection") != "explicit-metadata-not-usb-guess":
            raise ModelError(f"{identifier}: surface selection must remain explicit metadata")
        return
    routes = _expect_unique_string_list(
        compatibility.get("routes"), f"{identifier} routes", allow_empty=False
    )
    if any(route not in ROUTE_KINDS for route in routes):
        raise ModelError(f"{identifier}: invalid route kind")
    sibling_kinds = _expect_unique_string_list(
        compatibility.get("required_sibling_kinds"), f"{identifier} required sibling kinds"
    )
    if any(value not in {"keyboard", "mouse"} for value in sibling_kinds):
        raise ModelError(f"{identifier}: invalid sibling kind")
    if sibling_kinds:
        raise ModelError(f"{identifier}: child profile must not require a sibling device")


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


def _source_inventory(root: Path, sources: dict[str, Any]) -> dict[str, set[str]]:
    inventory: dict[str, set[str]] = {}
    for source in sources["sources"]:
        if source["kind"] == "git":
            content = load_json(root / source["inventory"])
            inventory[source["id"]] = {entry["path"] for entry in content["entries"]}
        elif source["kind"] == "imported-document":
            inventory[source["id"]] = {source["imported_path"]}
    return inventory


def _load_documents(root: Path) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    documents: list[tuple[str, dict[str, Any]]] = []
    profiles: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for directory in PROFILE_DIRECTORIES:
        for path in sorted((root / "profiles" / directory).glob("*.json")):
            value = load_json(path)
            relative = path.relative_to(root).as_posix()
            value["_source_path"] = relative
            profiles.append(value)
            documents.append((relative, {key: item for key, item in value.items() if key != "_source_path"}))
    for path in sorted((root / "profiles" / "candidates").glob("*.json")):
        value = load_json(path)
        value["_source_path"] = path.relative_to(root).as_posix()
        candidates.append(value)
        documents.append((value["_source_path"], {key: item for key, item in value.items() if key != "_source_path"}))
    return documents, profiles, candidates


def _validate_capabilities(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if catalog.get("schema") != "hyperflux-capability-catalog-v1":
        raise ModelError("unsupported capability catalog schema")
    entries = catalog.get("capabilities")
    if not isinstance(entries, list) or not entries:
        raise ModelError("capability catalog is empty")
    identifiers = [_expect_nonempty_string(entry.get("id"), "capability id") for entry in entries]
    require_unique(identifiers, "capability id")
    if identifiers != sorted(identifiers):
        raise ModelError("capability catalog must be sorted by id")
    allowed_access = {"read", "write"}
    allowed_owners = {"kernel", "bridge", "sdk", "integration"}
    allowed_kinds = {"receiver", "child", "surface"}
    for entry in entries:
        _expect_keys(entry, {"id", "summary", "access", "owner", "resource", "profile_kinds"}, entry["id"])
        _expect_nonempty_string(entry.get("summary"), f"{entry['id']} summary")
        if entry.get("access") not in allowed_access:
            raise ModelError(f"{entry['id']}: invalid capability access")
        if entry.get("owner") not in allowed_owners:
            raise ModelError(f"{entry['id']}: invalid capability owner")
        kinds = set(_expect_string_list(entry.get("profile_kinds"), f"{entry['id']} profile kinds", allow_empty=False))
        if not kinds <= allowed_kinds:
            raise ModelError(f"{entry['id']}: invalid profile kind")
    return {entry["id"]: entry for entry in entries}


def _validate_candidates(catalogs: list[dict[str, Any]]) -> tuple[set[str], list[dict[str, Any]]]:
    snapshot_ids: set[str] = set()
    all_candidates: list[dict[str, Any]] = []
    catalogs_by_id: dict[str, dict[str, Any]] = {}
    retrieval_dates: dict[str, date] = {}
    successors: list[str] = []
    for catalog in catalogs:
        if catalog.get("schema") != "hyperflux-candidate-catalog-v1":
            raise ModelError(f"{catalog['_source_path']}: unsupported candidate catalog schema")
        _expect_keys(
            catalog,
            {
                "$schema",
                "schema",
                "snapshot_id",
                "retrieved_utc",
                "supersedes_snapshot_id",
                "source_claim",
                "candidates",
                "_source_path",
            },
            f"{catalog['_source_path']} candidate catalog",
        )
        snapshot_id = _expect_nonempty_string(catalog.get("snapshot_id"), "candidate snapshot id")
        if snapshot_id in snapshot_ids:
            raise ModelError(f"duplicate candidate snapshot id: {snapshot_id}")
        snapshot_ids.add(snapshot_id)
        catalogs_by_id[snapshot_id] = catalog
        retrieved = _expect_nonempty_string(
            catalog.get("retrieved_utc"), f"{snapshot_id} retrieval date"
        )
        try:
            retrieval_dates[snapshot_id] = date.fromisoformat(retrieved)
        except ValueError as error:
            raise ModelError(f"{snapshot_id}: retrieval date must be an ISO date") from error
        supersedes = catalog.get("supersedes_snapshot_id")
        if supersedes is not None:
            successors.append(_expect_nonempty_string(supersedes, f"{snapshot_id} superseded snapshot"))
        source_claim = _expect_nonempty_string(catalog.get("source_claim"), f"{snapshot_id} source claim")
        candidates = catalog.get("candidates")
        if not isinstance(candidates, list):
            raise ModelError(f"{snapshot_id}: candidates must be a list")
        identifiers = [_expect_nonempty_string(item.get("id"), f"{snapshot_id} candidate id") for item in candidates]
        require_unique(identifiers, f"{snapshot_id} candidate id")
        for candidate in candidates:
            if candidate.get("support_level") != "candidate":
                raise ModelError(f"{candidate['id']}: candidate support level must remain candidate")
            if candidate.get("writable_capabilities") != []:
                raise ModelError(f"{candidate['id']}: unqualified candidate exposes a writable capability")
            if "product_id" in candidate or "vendor_id" in candidate:
                raise ModelError(f"{candidate['id']}: candidate catalog must not guess USB identity")
            copied = deepcopy(candidate)
            copied["snapshot_id"] = snapshot_id
            copied["source_claim"] = source_claim
            all_candidates.append(copied)
    require_unique(successors, "superseded candidate snapshot")
    for snapshot_id, catalog in catalogs_by_id.items():
        supersedes = catalog.get("supersedes_snapshot_id")
        if supersedes is None:
            continue
        previous = catalogs_by_id.get(supersedes)
        if previous is None:
            raise ModelError(f"{snapshot_id}: superseded candidate snapshot is absent: {supersedes}")
        if retrieval_dates[supersedes] >= retrieval_dates[snapshot_id]:
            raise ModelError(f"{snapshot_id}: supersession must move to a later retrieval date")
        previous_candidates = {candidate["id"]: candidate for candidate in previous["candidates"]}
        current_candidates = {candidate["id"]: candidate for candidate in catalog["candidates"]}
        missing = sorted(set(previous_candidates) - set(current_candidates))
        if missing:
            raise ModelError(
                f"{snapshot_id}: successor silently removes candidates: {', '.join(missing)}"
            )
        changed = sorted(
            identifier
            for identifier, candidate in previous_candidates.items()
            if current_candidates[identifier] != candidate
        )
        if changed:
            raise ModelError(
                f"{snapshot_id}: successor rewrites historical candidates: {', '.join(changed)}"
            )
    candidate_keys = [f"{item['snapshot_id']}:{item['id']}" for item in all_candidates]
    require_unique(candidate_keys, "candidate identity")
    return snapshot_ids, all_candidates


def _validate_evidence(
    root: Path,
    registry: dict[str, Any],
    sources: dict[str, Any],
    capability_ids: set[str],
    target_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if registry.get("schema") != "hyperflux-evidence-registry-v1":
        raise ModelError("unsupported evidence registry schema")
    claims = registry.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ModelError("evidence registry is empty")
    identifiers = [_expect_nonempty_string(claim.get("id"), "evidence claim id") for claim in claims]
    require_unique(identifiers, "evidence claim id")
    if identifiers != sorted(identifiers):
        raise ModelError("evidence registry must be sorted by claim id")
    inventories = _source_inventory(root, sources)
    source_ids = set(inventories)
    for claim in claims:
        identifier = claim["id"]
        _expect_nonempty_string(claim.get("summary"), f"{identifier} summary")
        if claim.get("privacy") not in PUBLIC_EVIDENCE:
            raise ModelError(f"{identifier}: product evidence must be public or a public summary")
        profiles = set(_expect_string_list(claim.get("profile_ids"), f"{identifier} profile ids", allow_empty=False))
        unknown_targets = profiles - target_ids
        if unknown_targets:
            raise ModelError(f"{identifier}: unknown evidence targets: {', '.join(sorted(unknown_targets))}")
        qualified = set(_expect_string_list(claim.get("qualifies_capabilities"), f"{identifier} capabilities"))
        unknown_capabilities = qualified - capability_ids
        if unknown_capabilities:
            raise ModelError(f"{identifier}: unknown capabilities: {', '.join(sorted(unknown_capabilities))}")
        refs = claim.get("source_refs")
        if not isinstance(refs, list) or not refs:
            raise ModelError(f"{identifier}: source refs are required")
        for ref in refs:
            source_id = ref.get("source_id")
            if source_id not in source_ids:
                raise ModelError(f"{identifier}: unknown source {source_id}")
            paths = _expect_string_list(ref.get("paths"), f"{identifier} source paths", allow_empty=False)
            for path in paths:
                if path.startswith("/") or ".." in Path(path).parts:
                    raise ModelError(f"{identifier}: unsafe evidence path {path}")
                if path not in inventories[source_id]:
                    raise ModelError(f"{identifier}: source path is absent from frozen inventory: {path}")
        _expect_string_list(claim.get("limitations"), f"{identifier} limitations")
    return {claim["id"]: claim for claim in claims}


def _validate_lighting(profile: dict[str, Any]) -> None:
    write_capabilities = [capability for capability in profile["capabilities"] if capability["id"].startswith("lighting.")]
    if profile["kind"] != "child":
        return
    lighting = profile.get("transport", {}).get("lighting")
    if write_capabilities and not isinstance(lighting, dict):
        raise ModelError(f"{profile['profile_id']}: writable lighting requires a transport map")
    if not isinstance(lighting, dict):
        return
    required = {"physical_led_count", "application_slot_count", "carrier_count", "rows", "columns", "application_index_to_carrier"}
    missing = sorted(required - set(lighting))
    if missing:
        raise ModelError(f"{profile['profile_id']}: incomplete lighting map: {', '.join(missing)}")
    counts = {key: lighting[key] for key in required - {"application_index_to_carrier"}}
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in counts.values()):
        raise ModelError(f"{profile['profile_id']}: lighting dimensions must be positive integers")
    if lighting["rows"] * lighting["columns"] != lighting["application_slot_count"]:
        raise ModelError(f"{profile['profile_id']}: lighting rows and columns do not cover application slots")
    if lighting["physical_led_count"] > lighting["application_slot_count"]:
        raise ModelError(f"{profile['profile_id']}: physical LED count exceeds application slots")
    mapping = lighting["application_index_to_carrier"]
    if not isinstance(mapping, list) or len(mapping) != lighting["application_slot_count"]:
        raise ModelError(f"{profile['profile_id']}: lighting map length mismatch")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in mapping):
        raise ModelError(f"{profile['profile_id']}: lighting carriers must be non-negative integers")
    if len(set(mapping)) != len(mapping):
        raise ModelError(f"{profile['profile_id']}: lighting map repeats a receiver carrier")
    if max(mapping) >= lighting["carrier_count"]:
        raise ModelError(f"{profile['profile_id']}: lighting map exceeds carrier count")


def _raw_values(value: object, label: str) -> list[int]:
    if not isinstance(value, list):
        raise ModelError(f"{label} must be a list")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 0xFFFF_FFFF
        for item in value
    ):
        raise ModelError(f"{label} contains an invalid raw value")
    if value != sorted(set(value)):
        raise ModelError(f"{label} must be sorted and unique")
    return value


def _validate_native_exchange(profile_id: str, value: object) -> None:
    if not isinstance(value, dict):
        raise ModelError(f"{profile_id}: native exchange must be an object")
    _expect_keys(value, NATIVE_EXCHANGE_KEYS, f"{profile_id} native exchange")
    missing = sorted(NATIVE_EXCHANGE_REQUIRED_KEYS - set(value))
    if missing:
        raise ModelError(f"{profile_id}: incomplete native exchange: {', '.join(missing)}")
    if value["protocol_family"] != "razer-feature-report-v1":
        raise ModelError(f"{profile_id}: unsupported native exchange protocol")
    if value["report_bytes"] != 90:
        raise ModelError(f"{profile_id}: native exchange report must be exactly 90 bytes")
    integer_bounds = {
        "set_report_index": (0, 255),
        "response_report_index": (0, 255),
        "initial_response_delay_us": (0, 100_000),
        "response_retry_delay_us": (1, 100_000),
        "max_response_attempts": (1, 16),
    }
    for key, (minimum, maximum) in integer_bounds.items():
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise ModelError(f"{profile_id}: native exchange {key} is out of bounds")
    report_format = value["report_format"]
    if not isinstance(report_format, dict):
        raise ModelError(f"{profile_id}: native report format must be an object")
    _expect_keys(report_format, NATIVE_REPORT_FORMAT_KEYS, f"{profile_id} native report format")
    if set(report_format) != NATIVE_REPORT_FORMAT_KEYS:
        raise ModelError(f"{profile_id}: native report format is incomplete")

    def byte_value(name: str) -> None:
        item = report_format[name]
        if not isinstance(item, dict):
            raise ModelError(f"{profile_id}: native {name} must be an object")
        _expect_keys(item, NATIVE_BYTE_VALUE_KEYS, f"{profile_id} native {name}")
        if set(item) != NATIVE_BYTE_VALUE_KEYS:
            raise ModelError(f"{profile_id}: native {name} is incomplete")
        if any(
            isinstance(item[key], bool)
            or not isinstance(item[key], int)
            or not 0 <= item[key] <= maximum
            for key, maximum in (("offset", value["report_bytes"] - 1), ("value", 255))
        ):
            raise ModelError(f"{profile_id}: native {name} is out of bounds")

    byte_value("request_status")
    byte_value("protocol_type")
    byte_value("trailer")
    response_status = report_format["response_status"]
    if not isinstance(response_status, dict):
        raise ModelError(f"{profile_id}: native response status must be an object")
    _expect_keys(
        response_status,
        NATIVE_BYTE_RANGE_KEYS,
        f"{profile_id} native response status",
    )
    if set(response_status) != NATIVE_BYTE_RANGE_KEYS or any(
        isinstance(response_status[key], bool)
        or not isinstance(response_status[key], int)
        or not 0 <= response_status[key] <= maximum
        for key, maximum in (
            ("offset", value["report_bytes"] - 1),
            ("minimum", 255),
            ("maximum", 255),
        )
    ):
        raise ModelError(f"{profile_id}: native response status is out of bounds")
    if response_status["minimum"] > response_status["maximum"]:
        raise ModelError(f"{profile_id}: native response status range is reversed")
    data_size = report_format["data_size"]
    if not isinstance(data_size, dict):
        raise ModelError(f"{profile_id}: native data size must be an object")
    _expect_keys(data_size, NATIVE_DATA_SIZE_KEYS, f"{profile_id} native data size")
    if set(data_size) != NATIVE_DATA_SIZE_KEYS or any(
        isinstance(data_size[key], bool)
        or not isinstance(data_size[key], int)
        or not 0 <= data_size[key] <= maximum
        for key, maximum in (("offset", value["report_bytes"] - 1), ("maximum", 255))
    ):
        raise ModelError(f"{profile_id}: native data size is out of bounds")
    checksum = report_format["checksum"]
    if not isinstance(checksum, dict):
        raise ModelError(f"{profile_id}: native checksum must be an object")
    _expect_keys(checksum, NATIVE_CHECKSUM_KEYS, f"{profile_id} native checksum")
    if set(checksum) != NATIVE_CHECKSUM_KEYS or checksum["algorithm"] != "xor8":
        raise ModelError(f"{profile_id}: native checksum is incomplete or unsupported")
    for key in ("start_offset", "end_offset_exclusive", "result_offset"):
        if isinstance(checksum[key], bool) or not isinstance(checksum[key], int):
            raise ModelError(f"{profile_id}: native checksum {key} is invalid")
    if not (
        0 <= checksum["start_offset"] < checksum["end_offset_exclusive"] <= value["report_bytes"]
        and 0 <= checksum["result_offset"] < value["report_bytes"]
        and not checksum["start_offset"]
        <= checksum["result_offset"]
        < checksum["end_offset_exclusive"]
    ):
        raise ModelError(f"{profile_id}: native checksum range is out of bounds")
    transaction_id = report_format["transaction_id"]
    if not isinstance(transaction_id, dict):
        raise ModelError(f"{profile_id}: native transaction id must be an object")
    _expect_keys(
        transaction_id,
        NATIVE_TRANSACTION_ID_KEYS,
        f"{profile_id} native transaction id",
    )
    if set(transaction_id) != NATIVE_TRANSACTION_ID_KEYS or any(
        isinstance(transaction_id[key], bool)
        or not isinstance(transaction_id[key], int)
        or not 0 <= transaction_id[key] <= maximum
        for key, maximum in (("offset", value["report_bytes"] - 1), ("match_mask", 255))
    ):
        raise ModelError(f"{profile_id}: native transaction id is out of bounds")
    route_selector = value.get("route_selector")
    if route_selector is not None:
        if not isinstance(route_selector, dict):
            raise ModelError(f"{profile_id}: native route selector must be an object")
        _expect_keys(
            route_selector,
            NATIVE_ROUTE_SELECTOR_KEYS,
            f"{profile_id} native route selector",
        )
        if set(route_selector) != NATIVE_ROUTE_SELECTOR_KEYS:
            raise ModelError(f"{profile_id}: native route selector is incomplete")
        if route_selector["field"] != "transaction-id":
            raise ModelError(f"{profile_id}: native route selector field is unsupported")
        for lane in ("pointer_lane", "keyboard_lane"):
            selector = route_selector[lane]
            if (
                isinstance(selector, bool)
                or not isinstance(selector, int)
                or not 1 <= selector <= 255
            ):
                raise ModelError(f"{profile_id}: native route selector {lane} is invalid")
        if route_selector["pointer_lane"] == route_selector["keyboard_lane"]:
            raise ModelError(f"{profile_id}: native route selectors must isolate endpoint lanes")
        if route_selector["restore_original_response"] is not True:
            raise ModelError(
                f"{profile_id}: native route selector must restore the application response"
            )
    ranges = value["match_ranges"]
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 4:
        raise ModelError(f"{profile_id}: native exchange needs one to four match ranges")
    previous_end = 0
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            raise ModelError(f"{profile_id}: native match range must be an object")
        _expect_keys(item, NATIVE_MATCH_RANGE_KEYS, f"{profile_id} native match range")
        if set(item) != NATIVE_MATCH_RANGE_KEYS:
            raise ModelError(f"{profile_id}: native match range is incomplete")
        offset = item["offset"]
        length = item["length"]
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or isinstance(length, bool)
            or not isinstance(length, int)
            or offset < 0
            or length <= 0
            or offset + length > value["report_bytes"]
        ):
            raise ModelError(f"{profile_id}: native match range is out of bounds")
        if index and offset < previous_end:
            raise ModelError(f"{profile_id}: native match ranges must be sorted and disjoint")
        previous_end = offset + length


def _validate_passive(
    profile: dict[str, Any], lifecycle_policy_index: dict[str, dict[str, Any]]
) -> None:
    transport = profile.get("transport", {})
    if not isinstance(transport, dict):
        raise ModelError(f"{profile['profile_id']}: transport must be an object")
    kind = profile["kind"]
    if kind == "receiver":
        _expect_keys(
            transport,
            RECEIVER_TRANSPORT_KEYS,
            f"{profile['profile_id']} receiver transport",
        )
        inventory = transport.get("pairing_inventory")
        if inventory is not None:
            if not isinstance(inventory, dict):
                raise ModelError(
                    f"{profile['profile_id']}: pairing inventory must be an object"
                )
            _expect_keys(
                inventory,
                {"decoder"},
                f"{profile['profile_id']} pairing inventory",
            )
            if inventory.get("decoder") != "razer-hyperflux-v2-feature-01-v1":
                raise ModelError(
                    f"{profile['profile_id']}: unsupported pairing inventory decoder"
                )
        native_exchange = transport.get("native_exchange")
        if native_exchange is not None:
            _validate_native_exchange(profile["profile_id"], native_exchange)
        return
    if kind != "child":
        if transport:
            raise ModelError(f"{profile['profile_id']}: surface transport is not allowed")
        return
    _expect_keys(
        transport,
        {"lighting", "passive", "lifecycle"},
        f"{profile['profile_id']} child transport",
    )
    passive = transport.get("passive")
    capability_ids = {item["id"] for item in profile["capabilities"]}
    needs_passive = bool(capability_ids & PASSIVE_TELEMETRY_CAPABILITIES)
    if passive is None:
        if needs_passive:
            raise ModelError(f"{profile['profile_id']}: passive telemetry requires a decoder")
        _validate_lifecycle_policy(profile, transport, passive, lifecycle_policy_index)
        return
    if not isinstance(passive, dict):
        raise ModelError(f"{profile['profile_id']}: passive decoder must be an object")
    _expect_keys(passive, PASSIVE_KEYS, f"{profile['profile_id']} passive decoder")
    missing = sorted(PASSIVE_KEYS - set(passive))
    if missing:
        raise ModelError(f"{profile['profile_id']}: incomplete passive decoder: {', '.join(missing)}")
    expected_lane = "pointer" if profile["device_kind"] == "mouse" else "keyboard"
    if passive["endpoint_lane"] != expected_lane:
        raise ModelError(f"{profile['profile_id']}: passive lane contradicts the device kind")
    if passive["battery_encoding"] != "linear-255":
        raise ModelError(f"{profile['profile_id']}: unsupported battery encoding")
    if not isinstance(passive["report_implies_route_available"], bool):
        raise ModelError(f"{profile['profile_id']}: route implication must be boolean")

    contact = passive["contact"]
    if profile["device_kind"] == "mouse":
        if not isinstance(contact, dict) or set(contact) != {"off_mat", "on_mat"}:
            raise ModelError(f"{profile['profile_id']}: mouse contact decoder is incomplete")
        off_mat = _raw_values(contact["off_mat"], f"{profile['profile_id']} off-mat values")
        on_mat = _raw_values(contact["on_mat"], f"{profile['profile_id']} on-mat values")
        if not off_mat or not on_mat or set(off_mat) & set(on_mat):
            raise ModelError(f"{profile['profile_id']}: contact values must be nonempty and disjoint")
    elif contact is not None:
        raise ModelError(f"{profile['profile_id']}: keyboard cannot declare mouse contact")

    route = passive["route"]
    route_keys = {"available", "unavailable", "indeterminate"}
    if (
        not isinstance(route, dict)
        or not {"available", "unavailable"}.issubset(route)
        or not set(route).issubset(route_keys)
    ):
        raise ModelError(f"{profile['profile_id']}: route decoder is incomplete")
    available = _raw_values(route["available"], f"{profile['profile_id']} available routes")
    unavailable = _raw_values(route["unavailable"], f"{profile['profile_id']} unavailable routes")
    indeterminate = _raw_values(
        route.get("indeterminate", []),
        f"{profile['profile_id']} indeterminate routes",
    )
    if (
        set(available) & set(unavailable)
        or set(available) & set(indeterminate)
        or set(unavailable) & set(indeterminate)
    ):
        raise ModelError(f"{profile['profile_id']}: route values must be disjoint")
    if "telemetry.connection-evidence" in capability_ids and not (
        available or unavailable or passive["report_implies_route_available"]
    ):
        raise ModelError(f"{profile['profile_id']}: connection evidence has no route decoder")
    _validate_lifecycle_policy(profile, transport, passive, lifecycle_policy_index)


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


def _validate_presentation(profile: dict[str, Any]) -> None:
    identifier = profile["profile_id"]
    presentation = profile.get("presentation")
    if profile["kind"] != "child":
        if presentation is not None:
            raise ModelError(f"{identifier}: only child profiles may declare application presentation")
        return
    if not isinstance(presentation, dict):
        raise ModelError(f"{identifier}: child presentation metadata is required")
    _expect_keys(presentation, PRESENTATION_KEYS, f"{identifier} presentation")
    missing = sorted(PRESENTATION_KEYS - set(presentation))
    if missing:
        raise ModelError(f"{identifier}: incomplete presentation metadata: {', '.join(missing)}")

    upstream_id = _expect_nonempty_string(
        presentation["upstream_id"], f"{identifier} presentation upstream"
    )
    if UPSTREAM_ID_PATTERN.fullmatch(upstream_id) is None:
        raise ModelError(f"{identifier}: invalid presentation upstream id")
    _expect_nonempty_string(presentation["owner"], f"{identifier} presentation owner")
    _expect_nonempty_string(
        presentation["project_version"], f"{identifier} presentation project version"
    )
    source_commit = _expect_nonempty_string(
        presentation["source_commit"], f"{identifier} presentation source commit"
    )
    if GIT_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ModelError(f"{identifier}: presentation source commit must be 40 lowercase hex characters")
    model_key = _expect_nonempty_string(
        presentation["model_key"], f"{identifier} presentation model key"
    )
    if PRESENTATION_KEY_PATTERN.fullmatch(model_key) is None:
        raise ModelError(f"{identifier}: invalid presentation model key")
    layout_key = presentation["layout_key"]
    if layout_key is not None and (
        not isinstance(layout_key, str)
        or PRESENTATION_KEY_PATTERN.fullmatch(layout_key) is None
    ):
        raise ModelError(f"{identifier}: invalid presentation layout key")
    variant = presentation["transport_variant"]
    expected_route = PRESENTATION_VARIANT_ROUTES.get(variant)
    if expected_route is None:
        raise ModelError(f"{identifier}: invalid presentation transport variant")
    if expected_route not in profile["compatibility"]["routes"]:
        raise ModelError(
            f"{identifier}: presentation transport variant has no matching device route"
        )


def _validate_presentation_sources(
    profiles: list[dict[str, Any]], upstreams: dict[str, dict[str, Any]]
) -> None:
    for profile in profiles:
        presentation = profile.get("presentation")
        if presentation is None:
            continue
        upstream = upstreams.get(presentation["upstream_id"])
        if upstream is None:
            raise ModelError(
                f"{profile['profile_id']}: presentation references an unknown upstream"
            )
        expected = {
            "owner": upstream["name"],
            "project_version": upstream["version"],
            "source_commit": upstream["commit"],
        }
        mismatches = [
            field for field, value in expected.items() if presentation[field] != value
        ]
        if mismatches:
            raise ModelError(
                f"{profile['profile_id']}: presentation pin differs from the integration catalog: "
                + ", ".join(mismatches)
            )


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
    snapshot_ids, candidates = _validate_candidates(candidate_catalogs)
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
