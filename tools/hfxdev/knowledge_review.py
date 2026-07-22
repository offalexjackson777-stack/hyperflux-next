# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

from .model import ModelError, require_unique


REVIEW_ROOT_KEYS = {"$schema", "schema", "reviewed_on", "sources", "candidates"}
REVIEW_SOURCE_KEYS = {
    "id",
    "kind",
    "publisher",
    "title",
    "url",
    "retrieved_on",
    "revision",
    "authority",
}
REVIEW_CANDIDATE_KEYS = {"candidate_id", "source_ids", "facts", "gaps"}
REVIEW_FACT_KEYS = {
    "id",
    "semantic_capability",
    "family",
    "label",
    "value",
    "unit",
    "routes",
    "source_ids",
    "claim_state",
    "notes",
}
REVIEW_GAP_KEYS = {"id", "title", "detail", "status", "source_ids"}
REVIEW_SOURCE_KINDS = {
    "official-compatibility",
    "official-manual",
    "official-product",
    "official-support",
    "upstream-issue",
}
REVIEW_FACT_FAMILIES = {
    "connectivity",
    "identity",
    "input",
    "lighting",
    "macros",
    "performance",
    "power",
    "profiles",
    "telemetry",
}
REVIEW_ROUTES = {
    "bluetooth",
    "hyperflux-receiver",
    "not-route-specific",
    "vendor-wireless-receiver",
    "wired",
}
REVIEW_GAP_STATES = {
    "not-documented",
    "not-implemented",
    "not-mapped",
    "not-validated",
    "source-conflict",
}
FACT_PROFILE_CAPABILITY_ALIASES = {
    "lighting.addressable-zones": {"lighting.per-led"},
    "lighting.per-key": {"lighting.per-key"},
}


def _expect_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    extras = sorted(set(value) - allowed)
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _expect_sorted_strings(
    value: Any, label: str, *, allow_empty: bool = True
) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ModelError(f"{label}: expected a {'non-empty ' if not allow_empty else ''}list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ModelError(f"{label}: every item must be a non-empty string")
    require_unique(value, label)
    if value != sorted(value):
        raise ModelError(f"{label}: values must be sorted")
    return value


def _review_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise ModelError(f"{label}: expected an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ModelError(f"{label}: expected an ISO date") from error


def _validate_fact_value(value: Any, label: str) -> None:
    if isinstance(value, (bool, int, str)):
        if isinstance(value, str) and not value:
            raise ModelError(f"{label}: empty text value")
        return
    if not isinstance(value, list) or not value:
        raise ModelError(f"{label}: unsupported fact value")
    if not all(isinstance(item, str) and item for item in value) and not all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        raise ModelError(f"{label}: fact list must contain only text or only integers")
    require_unique(value, f"{label} list value")


def validate_reviewed_facts(
    value: dict[str, Any], candidate_index: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    _expect_keys(value, REVIEW_ROOT_KEYS, "reviewed device facts")
    if value.get("schema") != "hyperflux-reviewed-device-facts-v1":
        raise ModelError("unsupported reviewed device facts schema")
    reviewed_on_value = value.get("reviewed_on")
    reviewed_on = _review_date(reviewed_on_value, "reviewed device facts date")

    source_values = value.get("sources")
    if not isinstance(source_values, list) or not source_values:
        raise ModelError("reviewed device sources are empty")
    source_ids: list[str] = []
    sources: dict[str, dict[str, Any]] = {}
    for source in source_values:
        if not isinstance(source, dict):
            raise ModelError("reviewed device source must be an object")
        _expect_keys(source, REVIEW_SOURCE_KEYS, "reviewed device source")
        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id:
            raise ModelError("reviewed device source id is invalid")
        source_ids.append(source_id)
        if source.get("kind") not in REVIEW_SOURCE_KINDS:
            raise ModelError(f"{source_id}: unsupported reviewed source kind")
        if not isinstance(source.get("publisher"), str) or not source["publisher"]:
            raise ModelError(f"{source_id}: publisher is required")
        if not isinstance(source.get("title"), str) or not source["title"]:
            raise ModelError(f"{source_id}: title is required")
        if not isinstance(source.get("url"), str) or not source["url"].startswith("https://"):
            raise ModelError(f"{source_id}: source URL must use HTTPS")
        retrieved = _review_date(source.get("retrieved_on"), f"{source_id} retrieval date")
        if retrieved > reviewed_on:
            raise ModelError(f"{source_id}: retrieval date is after the review date")
        if not isinstance(source.get("revision"), str) or not source["revision"]:
            raise ModelError(f"{source_id}: source revision is required")
        authority = _expect_sorted_strings(
            source.get("authority"), f"{source_id} authority", allow_empty=False
        )
        allowed_authority = {
            "product-capability",
            "product-procedure",
            "transport-identity",
            "transport-limitation",
        }
        if not set(authority).issubset(allowed_authority):
            raise ModelError(f"{source_id}: unsupported source authority")
        sources[source_id] = deepcopy(source)
    require_unique(source_ids, "reviewed device source id")
    if source_ids != sorted(source_ids):
        raise ModelError("reviewed device sources must be sorted by id")

    candidate_values = value.get("candidates")
    if not isinstance(candidate_values, list) or not candidate_values:
        raise ModelError("reviewed device candidate facts are empty")
    reviewed_candidates: dict[str, dict[str, Any]] = {}
    candidate_ids: list[str] = []
    for reviewed in candidate_values:
        if not isinstance(reviewed, dict):
            raise ModelError("reviewed device candidate must be an object")
        _expect_keys(reviewed, REVIEW_CANDIDATE_KEYS, "reviewed device candidate")
        candidate_id = reviewed.get("candidate_id")
        if candidate_id not in candidate_index:
            raise ModelError(f"unknown reviewed device candidate: {candidate_id}")
        candidate_ids.append(candidate_id)
        selected_source_ids = _expect_sorted_strings(
            reviewed.get("source_ids"), f"{candidate_id} reviewed sources", allow_empty=False
        )
        unknown_sources = sorted(set(selected_source_ids) - set(sources))
        if unknown_sources:
            raise ModelError(
                f"{candidate_id}: unknown reviewed sources: {', '.join(unknown_sources)}"
            )

        facts = reviewed.get("facts")
        if not isinstance(facts, list) or not facts:
            raise ModelError(f"{candidate_id}: reviewed facts are empty")
        fact_ids: list[str] = []
        semantics: list[str] = []
        for fact in facts:
            if not isinstance(fact, dict):
                raise ModelError(f"{candidate_id}: reviewed fact must be an object")
            _expect_keys(fact, REVIEW_FACT_KEYS, f"{candidate_id} reviewed fact")
            fact_id = fact.get("id")
            semantic = fact.get("semantic_capability")
            if not isinstance(fact_id, str) or not fact_id:
                raise ModelError(f"{candidate_id}: reviewed fact id is invalid")
            if not isinstance(semantic, str) or not semantic:
                raise ModelError(f"{candidate_id}/{fact_id}: semantic capability is invalid")
            fact_ids.append(fact_id)
            semantics.append(semantic)
            if fact.get("family") not in REVIEW_FACT_FAMILIES:
                raise ModelError(f"{candidate_id}/{fact_id}: invalid capability family")
            if not isinstance(fact.get("label"), str) or not fact["label"]:
                raise ModelError(f"{candidate_id}/{fact_id}: label is required")
            _validate_fact_value(fact.get("value"), f"{candidate_id}/{fact_id}")
            if "unit" in fact and (not isinstance(fact["unit"], str) or not fact["unit"]):
                raise ModelError(f"{candidate_id}/{fact_id}: unit is invalid")
            routes = _expect_sorted_strings(
                fact.get("routes"), f"{candidate_id}/{fact_id} routes", allow_empty=False
            )
            if not set(routes).issubset(REVIEW_ROUTES):
                raise ModelError(f"{candidate_id}/{fact_id}: unsupported route")
            if "not-route-specific" in routes and len(routes) != 1:
                raise ModelError(
                    f"{candidate_id}/{fact_id}: route-independent cannot be mixed with routes"
                )
            fact_sources = _expect_sorted_strings(
                fact.get("source_ids"), f"{candidate_id}/{fact_id} sources", allow_empty=False
            )
            if not set(fact_sources).issubset(selected_source_ids):
                raise ModelError(f"{candidate_id}/{fact_id}: source is not selected by candidate")
            claim_state = fact.get("claim_state")
            if claim_state not in {"documented-product", "reported-upstream"}:
                raise ModelError(f"{candidate_id}/{fact_id}: invalid claim state")
            source_kinds = {sources[source_id]["kind"] for source_id in fact_sources}
            if claim_state == "reported-upstream" and source_kinds != {"upstream-issue"}:
                raise ModelError(
                    f"{candidate_id}/{fact_id}: reported claims require only upstream reports"
                )
            if claim_state == "documented-product" and source_kinds == {"upstream-issue"}:
                raise ModelError(
                    f"{candidate_id}/{fact_id}: product claims require an official source"
                )
            _expect_sorted_strings(fact.get("notes"), f"{candidate_id}/{fact_id} notes")
        require_unique(fact_ids, f"{candidate_id} reviewed fact id")
        require_unique(semantics, f"{candidate_id} reviewed semantic capability")
        if fact_ids != sorted(fact_ids):
            raise ModelError(f"{candidate_id}: reviewed facts must be sorted by id")

        gaps = reviewed.get("gaps")
        if not isinstance(gaps, list):
            raise ModelError(f"{candidate_id}: reviewed gaps must be a list")
        gap_ids: list[str] = []
        for gap in gaps:
            if not isinstance(gap, dict):
                raise ModelError(f"{candidate_id}: reviewed gap must be an object")
            _expect_keys(gap, REVIEW_GAP_KEYS, f"{candidate_id} reviewed gap")
            gap_id = gap.get("id")
            if not isinstance(gap_id, str) or not gap_id:
                raise ModelError(f"{candidate_id}: reviewed gap id is invalid")
            gap_ids.append(gap_id)
            if not isinstance(gap.get("title"), str) or not gap["title"]:
                raise ModelError(f"{candidate_id}/{gap_id}: gap title is required")
            if not isinstance(gap.get("detail"), str) or not gap["detail"]:
                raise ModelError(f"{candidate_id}/{gap_id}: gap detail is required")
            if gap.get("status") not in REVIEW_GAP_STATES:
                raise ModelError(f"{candidate_id}/{gap_id}: invalid gap status")
            gap_sources = _expect_sorted_strings(
                gap.get("source_ids"), f"{candidate_id}/{gap_id} sources"
            )
            if not set(gap_sources).issubset(selected_source_ids):
                raise ModelError(f"{candidate_id}/{gap_id}: source is not selected by candidate")
        require_unique(gap_ids, f"{candidate_id} reviewed gap id")
        if gap_ids != sorted(gap_ids):
            raise ModelError(f"{candidate_id}: reviewed gaps must be sorted by id")
        reviewed_candidates[candidate_id] = deepcopy(reviewed)

    require_unique(candidate_ids, "reviewed device candidate id")
    if candidate_ids != sorted(candidate_ids):
        raise ModelError("reviewed device candidates must be sorted by id")
    missing = sorted(set(candidate_index) - set(candidate_ids))
    extras = sorted(set(candidate_ids) - set(candidate_index))
    if missing or extras:
        raise ModelError(
            "reviewed device facts must cover the candidate snapshot exactly"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; extra: {', '.join(extras)}" if extras else "")
        )
    return sources, reviewed_candidates, str(reviewed_on_value)


def source_record_url(
    record: dict[str, Any], source_catalogs: dict[str, dict[str, Any]]
) -> str:
    upstream_id = record["record_id"].split(":", 1)[0]
    source = source_catalogs[upstream_id]["source"]
    repository = source["repository"].removesuffix(".git")
    location = record["source_location"]
    return (
        f"{repository}/blob/{source['commit']}/{location['path']}"
        f"#L{location['line']}"
    )


def implementation_records_for_fact(
    fact: dict[str, Any],
    selected_records: list[dict[str, Any]],
    settings: list[dict[str, Any]],
) -> list[str]:
    semantic = fact["semantic_capability"]
    identifiers = {
        record_id
        for setting in settings
        if setting["semantic_capability"] == semantic
        for record_id in setting["source_records"]
    }
    if semantic == "input.dpi-maximum":
        identifiers.update(
            record["record_id"]
            for record in selected_records
            if isinstance(record["facts"].get("dpi_max"), int)
        )
    elif semantic == "input.polling-rate":
        identifiers.update(
            record["record_id"]
            for record in selected_records
            if record["facts"].get("poll_rates_hz")
        )
    elif semantic == "identity.usb-product-ids":
        identifiers.update(record["record_id"] for record in selected_records)
    elif semantic in {"lighting.addressable-zones", "lighting.per-key"}:
        identifiers.update(
            record["record_id"]
            for record in selected_records
            if record["lighting_topology"] is not None
        )
    return sorted(identifiers)


def fact_hyperflux_capabilities(
    semantic: str, qualified_capabilities: set[str]
) -> list[str]:
    candidates = {semantic} | FACT_PROFILE_CAPABILITY_ALIASES.get(semantic, set())
    return sorted(candidates & qualified_capabilities)


def fact_assurance(
    fact: dict[str, Any],
    source_index: dict[str, dict[str, Any]],
    implementation_records: list[str],
    hyperflux_capabilities: list[str],
) -> str:
    if hyperflux_capabilities:
        return "physically-qualified"
    source_kinds = {source_index[source_id]["kind"] for source_id in fact["source_ids"]}
    official_count = sum(kind != "upstream-issue" for kind in source_kinds)
    if implementation_records and official_count:
        return "documented-and-implemented"
    if official_count > 1:
        return "cross-documented"
    if fact["claim_state"] == "documented-product":
        return "product-documented"
    return "upstream-reported"


def transport_matrix(
    reviewed: dict[str, Any],
    selected_records: list[dict[str, Any]],
    hyperflux_support: str,
) -> list[dict[str, Any]]:
    record_routes = {
        "bluetooth": "bluetooth",
        "direct-usb": "wired",
        "vendor-wireless-receiver": "vendor-wireless-receiver",
    }
    records_by_route: dict[str, list[str]] = {
        "bluetooth": [],
        "vendor-wireless-receiver": [],
        "wired": [],
    }
    for record in selected_records:
        records_by_route[record_routes[record["source_route"]]].append(record["record_id"])

    documented_routes = {
        route
        for fact in reviewed["facts"]
        for route in fact["routes"]
        if route != "not-route-specific"
    }
    connectivity = next(
        (
            fact["value"]
            for fact in reviewed["facts"]
            if fact["semantic_capability"] == "connectivity.routes"
        ),
        [],
    )
    if isinstance(connectivity, list):
        normalized = {item.casefold() for item in connectivity}
        if any("bluetooth" in item for item in normalized):
            documented_routes.add("bluetooth")
        if any("2.4" in item or "hyperspeed" in item for item in normalized):
            documented_routes.add("vendor-wireless-receiver")
        if any("usb" in item or "cable" in item for item in normalized):
            documented_routes.add("wired")

    matrix = []
    for route_id, label in (
        ("wired", "USB cable"),
        ("vendor-wireless-receiver", "Vendor 2.4 GHz receiver"),
        ("bluetooth", "Bluetooth"),
    ):
        matrix.append(
            {
                "id": route_id,
                "label": label,
                "product_state": (
                    "documented" if route_id in documented_routes else "not-documented"
                ),
                "implementation_records": sorted(records_by_route[route_id]),
                "hyperflux_state": "outside-hyperflux",
            }
        )
    matrix.append(
        {
            "id": "hyperflux-receiver",
            "label": "HyperFlux V2 receiver",
            "product_state": "manufacturer-candidate",
            "implementation_records": [],
            "hyperflux_state": hyperflux_support,
        }
    )
    return matrix
