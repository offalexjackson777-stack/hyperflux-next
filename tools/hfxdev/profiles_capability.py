# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from datetime import date
import re
from typing import Any

from .model import ModelError, require_unique
from .profiles_common import (
    CHILD_COMPATIBILITY_KEYS,
    RECEIVER_COMPATIBILITY_KEYS,
    ROUTE_KINDS,
    SURFACE_COMPATIBILITY_KEYS,
    _expect_keys,
    _expect_nonempty_string,
    _expect_string_list,
    _expect_unique_string_list,
)


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


def _validate_candidates(
    catalogs: list[dict[str, Any]],
) -> tuple[set[str], list[dict[str, Any]], dict[str, Any]]:
    snapshot_ids: set[str] = set()
    all_candidates: list[dict[str, Any]] = []
    catalogs_by_id: dict[str, dict[str, Any]] = {}
    retrieval_dates: dict[str, date] = {}
    successors: list[str] = []
    official_scopes: list[tuple[str, str, dict[str, Any], list[dict[str, Any]]]] = []
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
                "official_receiver_scope",
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
        official_scope = catalog.get("official_receiver_scope")
        if official_scope is not None:
            official_scopes.append((snapshot_id, source_claim, official_scope, candidates))
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
    if len(official_scopes) != 1:
        raise ModelError("candidate catalogs must contain exactly one official receiver scope")
    scope_snapshot, scope_claim, scope, scope_candidates = official_scopes[0]
    if scope_snapshot in successors:
        raise ModelError("official receiver scope must belong to the current candidate snapshot")
    if not isinstance(scope, dict):
        raise ModelError(f"{scope_snapshot}: official receiver scope must be an object")
    _expect_keys(scope, {"vendor_id", "receiver_product_id", "entries"}, scope_snapshot)
    if scope.get("vendor_id") != 0x1532 or scope.get("receiver_product_id") != 0x00CF:
        raise ModelError(f"{scope_snapshot}: official receiver identity is invalid")
    entries = scope.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > 32:
        raise ModelError(f"{scope_snapshot}: official receiver entries are empty or unbounded")
    candidate_index = {candidate["id"]: candidate for candidate in scope_candidates}
    identifiers: list[str] = []
    wireless_product_ids: list[int] = []
    direct_product_ids: list[int] = []
    enriched_entries: list[dict[str, Any]] = []
    pending_pattern = re.compile(
        r"^https://github\.com/(openrazer/openrazer|CalcProgrammer1/OpenRGB)/pull/[1-9][0-9]*$"
    )
    for index, entry in enumerate(entries):
        label = f"{scope_snapshot} official receiver entry {index}"
        if not isinstance(entry, dict):
            raise ModelError(f"{label}: expected an object")
        _expect_keys(
            entry,
            {
                "candidate_id",
                "wireless_product_id",
                "direct_usb_product_id",
                "upstream_pending",
            },
            label,
        )
        candidate_id = _expect_nonempty_string(entry.get("candidate_id"), f"{label} id")
        candidate = candidate_index.get(candidate_id)
        if candidate is None:
            raise ModelError(f"{label}: candidate is absent from the same snapshot")
        wireless = entry.get("wireless_product_id")
        direct = entry.get("direct_usb_product_id")
        if (
            isinstance(wireless, bool)
            or not isinstance(wireless, int)
            or isinstance(direct, bool)
            or not isinstance(direct, int)
            or not 1 <= wireless <= 0xFFFF
            or not 1 <= direct <= 0xFFFF
            or wireless == direct
            or wireless == 0x00CF
            or direct == 0x00CF
        ):
            raise ModelError(f"{label}: wireless/direct identity is invalid")
        pending = entry.get("upstream_pending")
        if not isinstance(pending, list) or len(pending) > 2:
            raise ModelError(f"{label}: upstream pending records are invalid")
        pending_keys: list[str] = []
        for pending_index, record in enumerate(pending):
            pending_label = f"{label} pending record {pending_index}"
            if not isinstance(record, dict):
                raise ModelError(f"{pending_label}: expected an object")
            _expect_keys(record, {"application", "reference"}, pending_label)
            application = record.get("application")
            reference = record.get("reference")
            if application not in {"openrazer", "openrgb"} or not isinstance(
                reference, str
            ) or pending_pattern.fullmatch(reference) is None:
                raise ModelError(f"{pending_label}: provenance is invalid")
            pending_keys.append(f"{application}:{reference}")
        if pending_keys != sorted(set(pending_keys)):
            raise ModelError(f"{label}: upstream pending records must be unique and sorted")
        identifiers.append(candidate_id)
        wireless_product_ids.append(wireless)
        direct_product_ids.append(direct)
        enriched_entries.append(
            {
                **deepcopy(entry),
                "official_name": candidate["official_name"],
                "device_kind": candidate["device_kind"],
                "snapshot_id": scope_snapshot,
                "source_claim": scope_claim,
            }
        )
    if identifiers != sorted(candidate_index) or identifiers != sorted(identifiers):
        raise ModelError(
            f"{scope_snapshot}: official receiver scope must cover every current candidate in id order"
        )
    require_unique(wireless_product_ids, "official wireless product id")
    require_unique(direct_product_ids, "official direct USB product id")
    if set(wireless_product_ids) & set(direct_product_ids):
        raise ModelError("official wireless and direct USB product ids must be disjoint")
    return snapshot_ids, all_candidates, {
        "vendor_id": 0x1532,
        "receiver_product_id": 0x00CF,
        "snapshot_id": scope_snapshot,
        "source_claim": scope_claim,
        "entries": enriched_entries,
    }
