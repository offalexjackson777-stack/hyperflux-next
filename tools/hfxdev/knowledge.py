# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from .integrations import upstream_index
from .knowledge_review import (
    fact_assurance as _fact_assurance,
    fact_hyperflux_capabilities as _fact_hyperflux_capabilities,
    implementation_records_for_fact as _implementation_records_for_fact,
    source_record_url as _source_record_url,
    transport_matrix as _transport_matrix,
    validate_reviewed_facts as _validate_reviewed_facts,
)
from .model import ModelError, load_json, require_unique
from .openrazer import extract_openrazer_catalog
from .openrgb import extract_openrgb_catalog
from .profiles import load_profile_inputs


UPSTREAM_IDS = ("openrazer", "openrgb")
SOURCE_CATALOG_KEYS = {"$schema", "schema", "source", "records"}
SOURCE_KEYS = {
    "upstream_id",
    "repository",
    "version",
    "commit",
    "license_expression",
    "extractor",
    "source_files",
}
RECORD_KEYS = {
    "record_id",
    "source_device_key",
    "model_name",
    "device_kind",
    "source_route",
    "usb_identity",
    "lighting_topology",
    "settings_methods",
    "facts",
    "source_location",
}
LINK_KEYS = {
    "candidate_id",
    "assurance",
    "source_records",
    "hyperflux_profile_ids",
    "notes",
}
RULE_KEYS = {
    "id",
    "semantic_capability",
    "family",
    "access",
    "presentation",
    "methods",
    "required_hyperflux_capabilities",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class KnowledgeInputs:
    source_catalogs: dict[str, dict[str, Any]]
    records: dict[str, dict[str, Any]]
    links: tuple[dict[str, Any], ...]
    rules: tuple[dict[str, Any], ...]
    reviewed_sources: dict[str, dict[str, Any]]
    reviewed_candidates: dict[str, dict[str, Any]]
    reviewed_on: str
    source_sha256: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _expect_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    missing = sorted(allowed - set(value))
    extras = sorted(set(value) - allowed)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _expect_sorted_strings(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ModelError(f"{label}: expected a {'non-empty ' if not allow_empty else ''}list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ModelError(f"{label}: every item must be a non-empty string")
    require_unique(value, label)
    if value != sorted(value):
        raise ModelError(f"{label}: values must be sorted")
    return value


def _validate_source_catalog(
    value: dict[str, Any], expected_id: str, upstreams: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    _expect_keys(value, SOURCE_CATALOG_KEYS, f"{expected_id} source catalog")
    if value.get("$schema") != "../../schemas/upstream-device-catalog.schema.json":
        raise ModelError(f"{expected_id}: source catalog schema reference is not canonical")
    if value.get("schema") != "hyperflux-upstream-device-catalog-v1":
        raise ModelError(f"{expected_id}: unsupported upstream device catalog schema")
    source = value.get("source")
    if not isinstance(source, dict):
        raise ModelError(f"{expected_id}: source metadata is required")
    _expect_keys(source, SOURCE_KEYS, f"{expected_id} source")
    if source.get("upstream_id") != expected_id:
        raise ModelError(f"{expected_id}: upstream id mismatch")
    upstream = upstreams.get(expected_id)
    if upstream is None:
        raise ModelError(f"{expected_id}: integration catalog pin is missing")
    for field in ("repository", "version", "commit", "license_expression"):
        if source.get(field) != upstream[field]:
            raise ModelError(f"{expected_id}: imported {field} differs from the integration pin")
    source_files = source.get("source_files")
    if not isinstance(source_files, list) or not 1 <= len(source_files) <= 8:
        raise ModelError(f"{expected_id}: source files are required")
    source_paths: list[str] = []
    for item in source_files:
        if not isinstance(item, dict):
            raise ModelError(f"{expected_id}: invalid source file metadata")
        _expect_keys(item, {"path", "sha256"}, f"{expected_id} source file")
        path = item["path"]
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise ModelError(f"{expected_id}: unsafe source file path")
        if not isinstance(item["sha256"], str) or SHA256_PATTERN.fullmatch(item["sha256"]) is None:
            raise ModelError(f"{expected_id}: invalid source file digest")
        source_paths.append(path)
    require_unique(source_paths, f"{expected_id} source file path")
    if source_paths != sorted(source_paths):
        raise ModelError(f"{expected_id}: source files must be sorted")

    records = value.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= 2_048:
        raise ModelError(f"{expected_id}: imported records are empty")
    identifiers: list[str] = []
    index: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ModelError(f"{expected_id}: record must be an object")
        _expect_keys(record, RECORD_KEYS, f"{expected_id} record")
        identifier = record.get("record_id")
        if not isinstance(identifier, str) or not identifier.startswith(f"{expected_id}:"):
            raise ModelError(f"{expected_id}: invalid record id")
        identifiers.append(identifier)
        if record.get("device_kind") not in {"keyboard", "mouse"}:
            raise ModelError(f"{identifier}: invalid device kind")
        if record.get("source_route") not in {
            "direct-usb",
            "vendor-wireless-receiver",
            "bluetooth",
        }:
            raise ModelError(f"{identifier}: invalid source route")
        location = record.get("source_location")
        if not isinstance(location, dict):
            raise ModelError(f"{identifier}: source location is missing")
        _expect_keys(location, {"path", "line"}, f"{identifier} source location")
        if location.get("path") not in source_paths:
            raise ModelError(f"{identifier}: source location is outside the imported files")
        if (
            isinstance(location.get("line"), bool)
            or not isinstance(location.get("line"), int)
            or location["line"] < 1
        ):
            raise ModelError(f"{identifier}: source line is invalid")
        methods = _expect_sorted_strings(
            record.get("settings_methods"), f"{identifier} settings methods"
        )
        if expected_id == "openrgb" and methods:
            raise ModelError(f"{identifier}: OpenRGB presentation records must not invent settings")
        index[identifier] = record
    require_unique(identifiers, f"{expected_id} record id")
    if identifiers != sorted(identifiers):
        raise ModelError(f"{expected_id}: records must be sorted")
    return index


def _validate_rules(value: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    if value.get("schema") != "hyperflux-device-capability-map-v1":
        raise ModelError("unsupported device capability map schema")
    rules = value.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ModelError("device capability map is empty")
    identifiers: list[str] = []
    methods: list[str] = []
    semantics: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ModelError("device capability rule must be an object")
        _expect_keys(rule, RULE_KEYS, "device capability rule")
        identifier = rule.get("id")
        semantic = rule.get("semantic_capability")
        if not isinstance(identifier, str) or not identifier:
            raise ModelError("device capability rule id is invalid")
        if not isinstance(semantic, str) or not semantic:
            raise ModelError(f"{identifier}: semantic capability is invalid")
        identifiers.append(identifier)
        semantics.append(semantic)
        methods.extend(
            _expect_sorted_strings(rule.get("methods"), f"{identifier} methods", allow_empty=False)
        )
        _expect_sorted_strings(
            rule.get("required_hyperflux_capabilities"),
            f"{identifier} required HyperFlux capabilities",
            allow_empty=False,
        )
    require_unique(identifiers, "device capability rule id")
    require_unique(semantics, "semantic device capability")
    require_unique(methods, "mapped upstream method")
    if identifiers != sorted(identifiers):
        raise ModelError("device capability rules must be sorted by id")
    return tuple(deepcopy(rules))


def _validate_links(
    value: dict[str, Any],
    records: dict[str, dict[str, Any]],
    candidate_index: dict[str, dict[str, Any]],
    profile_index: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    if value.get("schema") != "hyperflux-device-knowledge-links-v1":
        raise ModelError("unsupported device knowledge link schema")
    snapshot_ids = {candidate["snapshot_id"] for candidate in candidate_index.values()}
    if value.get("candidate_snapshot_id") not in snapshot_ids:
        raise ModelError("device knowledge links reference an unknown candidate snapshot")
    links = value.get("links")
    if not isinstance(links, list) or not links:
        raise ModelError("device knowledge links are empty")
    identifiers: list[str] = []
    for link in links:
        if not isinstance(link, dict):
            raise ModelError("device knowledge link must be an object")
        _expect_keys(link, LINK_KEYS, "device knowledge link")
        identifier = link.get("candidate_id")
        if identifier not in candidate_index:
            raise ModelError(f"unknown linked candidate: {identifier}")
        identifiers.append(identifier)
        if link.get("assurance") != "reviewed-link":
            raise ModelError(f"{identifier}: candidate link must be explicitly reviewed")
        source_records = link.get("source_records")
        if not isinstance(source_records, dict) or set(source_records) != set(UPSTREAM_IDS):
            raise ModelError(f"{identifier}: both upstream record lists are required")
        for upstream_id in UPSTREAM_IDS:
            linked_ids = _expect_sorted_strings(
                source_records[upstream_id], f"{identifier} {upstream_id} records"
            )
            for record_id in linked_ids:
                record = records.get(record_id)
                if record is None:
                    raise ModelError(f"{identifier}: unknown source record {record_id}")
                if record["device_kind"] != candidate_index[identifier]["device_kind"]:
                    raise ModelError(f"{identifier}: source record device kind differs")
        profile_ids = _expect_sorted_strings(
            link.get("hyperflux_profile_ids"), f"{identifier} HyperFlux profile ids"
        )
        selected_records = [
            records[record_id]
            for upstream_id in UPSTREAM_IDS
            for record_id in source_records[upstream_id]
        ]
        for profile_id in profile_ids:
            profile = profile_index.get(profile_id)
            if profile is None:
                raise ModelError(f"{identifier}: unknown HyperFlux profile {profile_id}")
            if profile["device_kind"] != candidate_index[identifier]["device_kind"]:
                raise ModelError(f"{identifier}: HyperFlux profile device kind differs")
            identity = profile["identity"]
            if selected_records and not any(
                record["usb_identity"]["product_id"] == identity["product_id"]
                and record["usb_identity"]["vendor_id"] in {None, identity["vendor_id"]}
                for record in selected_records
            ):
                raise ModelError(
                    f"{identifier}: HyperFlux profile identity has no reviewed source record"
                )
        _expect_sorted_strings(link.get("notes"), f"{identifier} notes")
    require_unique(identifiers, "linked candidate id")
    if identifiers != sorted(identifiers):
        raise ModelError("device knowledge links must be sorted by candidate id")
    missing = sorted(set(candidate_index) - set(identifiers))
    extras = sorted(set(identifiers) - set(candidate_index))
    if missing or extras:
        raise ModelError(
            "device knowledge links must cover the candidate snapshot exactly"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; extra: {', '.join(extras)}" if extras else "")
        )
    return tuple(deepcopy(links))


def _validated_knowledge_inputs(
    root: Path, source_catalogs: dict[str, dict[str, Any]]
) -> KnowledgeInputs:
    profiles = load_profile_inputs(root)
    upstreams = upstream_index(root)
    if set(source_catalogs) != set(UPSTREAM_IDS):
        raise ModelError("device knowledge requires exactly the pinned OpenRazer and OpenRGB catalogs")
    records: dict[str, dict[str, Any]] = {}
    for upstream_id, catalog in source_catalogs.items():
        for identifier, record in _validate_source_catalog(
            catalog, upstream_id, upstreams
        ).items():
            if identifier in records:
                raise ModelError(f"duplicate upstream record id: {identifier}")
            records[identifier] = record
    link_catalog = load_json(root / "knowledge" / "candidate-links.json")
    capability_map = load_json(root / "knowledge" / "capability-map.json")
    reviewed_catalog = load_json(root / "knowledge" / "reviewed-facts.json")
    if capability_map.get("$schema") != "../schemas/device-capability-map.schema.json":
        raise ModelError("device capability map schema reference is not canonical")
    if link_catalog.get("$schema") != "../schemas/device-knowledge-links.schema.json":
        raise ModelError("device knowledge link schema reference is not canonical")
    if reviewed_catalog.get("$schema") != "../schemas/reviewed-device-facts.schema.json":
        raise ModelError("reviewed device facts schema reference is not canonical")
    rules = _validate_rules(capability_map)
    selected_snapshot = link_catalog.get("candidate_snapshot_id")
    candidate_index = {
        candidate["id"]: candidate
        for candidate in profiles.candidates
        if candidate["snapshot_id"] == selected_snapshot
    }
    if not candidate_index:
        raise ModelError("device knowledge selects an empty or unknown candidate snapshot")
    profile_index = {profile["profile_id"]: profile for profile in profiles.profiles}
    links = _validate_links(
        link_catalog,
        records,
        candidate_index,
        profile_index,
    )
    reviewed_sources, reviewed_candidates, reviewed_on = _validate_reviewed_facts(
        reviewed_catalog,
        candidate_index,
    )
    mapped_methods = {method for rule in rules for method in rule["methods"]}
    selected_methods = {
        method
        for link in links
        for record_id in link["source_records"]["openrazer"]
        for method in records[record_id]["settings_methods"]
    }
    unmapped = sorted(selected_methods - mapped_methods)
    if unmapped:
        raise ModelError(f"selected OpenRazer methods are unmapped: {', '.join(unmapped)}")
    unused = sorted(mapped_methods - selected_methods)
    if unused:
        raise ModelError(f"capability map contains unused methods: {', '.join(unused)}")

    digest = hashlib.sha256()
    for name, value in (
        ("profiles", {"source_sha256": profiles.source_sha256}),
        ("candidate-links", link_catalog),
        ("capability-map", capability_map),
        ("reviewed-facts", reviewed_catalog),
        *((f"upstream-{key}", source_catalogs[key]) for key in UPSTREAM_IDS),
    ):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_canonical_bytes(value))
        digest.update(b"\0")
    return KnowledgeInputs(
        source_catalogs=deepcopy(source_catalogs),
        records=records,
        links=links,
        rules=rules,
        reviewed_sources=reviewed_sources,
        reviewed_candidates=reviewed_candidates,
        reviewed_on=reviewed_on,
        source_sha256=digest.hexdigest(),
    )


def load_knowledge_inputs(root: Path) -> KnowledgeInputs:
    source_catalogs = {
        upstream_id: load_json(root / "knowledge" / "upstreams" / f"{upstream_id}.json")
        for upstream_id in UPSTREAM_IDS
    }
    return _validated_knowledge_inputs(root, source_catalogs)


def _slot_count(record: dict[str, Any]) -> int | None:
    topology = record["lighting_topology"]
    if not isinstance(topology, dict):
        return None
    dimensions = topology.get("matrix_dimensions")
    if isinstance(dimensions, list) and len(dimensions) == 2:
        return dimensions[0] * dimensions[1]
    value = topology.get("application_slot_count")
    return value if isinstance(value, int) else None


def _source_conflicts(link: dict[str, Any], records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    selected = [
        records[record_id]
        for upstream_id in UPSTREAM_IDS
        for record_id in link["source_records"][upstream_id]
    ]
    for route in ("direct-usb", "vendor-wireless-receiver", "bluetooth"):
        routed = [record for record in selected if record["source_route"] == route]
        sources = {record["record_id"].split(":", 1)[0] for record in routed}
        if len(sources) < 2:
            continue
        identities = {
            (
                record["usb_identity"]["vendor_id"],
                record["usb_identity"]["product_id"],
            )
            for record in routed
            if isinstance(record["usb_identity"]["vendor_id"], int)
        }
        if len(identities) > 1:
            conflicts.append(
                {
                    "field": "usb-identity",
                    "route": route,
                    "values": [f"{vendor:04x}:{product:04x}" for vendor, product in sorted(identities)],
                }
            )
        slot_counts = sorted(
            {value for record in routed if (value := _slot_count(record)) is not None}
        )
        if len(slot_counts) > 1:
            conflicts.append(
                {
                    "field": "lighting-slot-count",
                    "route": route,
                    "values": [str(value) for value in slot_counts],
                }
            )
    return conflicts


def compiled_knowledge_catalog(root: Path) -> dict[str, Any]:
    inputs = load_knowledge_inputs(root)
    profile_inputs = load_profile_inputs(root)
    selected_snapshot = load_json(root / "knowledge" / "candidate-links.json")[
        "candidate_snapshot_id"
    ]
    candidate_index = {
        candidate["id"]: candidate
        for candidate in profile_inputs.candidates
        if candidate["snapshot_id"] == selected_snapshot
    }
    profile_index = {profile["profile_id"]: profile for profile in profile_inputs.profiles}
    candidates: list[dict[str, Any]] = []
    for link in inputs.links:
        candidate = candidate_index[link["candidate_id"]]
        reviewed = inputs.reviewed_candidates[candidate["id"]]
        selected_records = [
            inputs.records[record_id]
            for upstream_id in UPSTREAM_IDS
            for record_id in link["source_records"][upstream_id]
        ]
        linked_profiles = [profile_index[value] for value in link["hyperflux_profile_ids"]]
        qualified_capabilities = {
            capability["id"] for profile in linked_profiles for capability in profile["capabilities"]
        }
        qualification_evidence: dict[str, set[str]] = {}
        for profile in linked_profiles:
            for capability in profile["capabilities"]:
                qualification_evidence.setdefault(capability["id"], set()).update(
                    capability["evidence_claims"]
                )
        selected_methods = {
            method for record in selected_records for method in record["settings_methods"]
        }
        settings: list[dict[str, Any]] = []
        for rule in inputs.rules:
            source_methods = sorted(selected_methods & set(rule["methods"]))
            if not source_methods:
                continue
            required = set(rule["required_hyperflux_capabilities"])
            missing = sorted(required - qualified_capabilities)
            settings.append(
                {
                    "id": rule["id"],
                    "semantic_capability": rule["semantic_capability"],
                    "family": rule["family"],
                    "access": rule["access"],
                    "presentation": rule["presentation"],
                    "source_methods": source_methods,
                    "source_records": sorted(
                        record["record_id"]
                        for record in selected_records
                        if set(record["settings_methods"]) & set(source_methods)
                    ),
                    "required_hyperflux_capabilities": sorted(required),
                    "control_state": "enabled" if not missing and linked_profiles else "blocked",
                    "missing_hyperflux_capabilities": missing,
                }
            )
        topology_records = sorted(
            record["record_id"]
            for record in selected_records
            if record["record_id"].startswith("openrgb:")
            and record["lighting_topology"] is not None
        )
        direct_frame = next(
            (value for value in settings if value["id"] == "lighting-direct-frame"),
            None,
        )
        if topology_records and direct_frame is None:
            required = {"lighting.direct-frame"}
            missing = sorted(required - qualified_capabilities)
            settings.append(
                {
                    "id": "lighting-direct-frame",
                    "semantic_capability": "lighting.direct-frame",
                    "family": "lighting",
                    "access": "write",
                    "presentation": "lighting-frame",
                    "source_methods": [],
                    "source_records": topology_records,
                    "required_hyperflux_capabilities": sorted(required),
                    "control_state": "enabled" if not missing and linked_profiles else "blocked",
                    "missing_hyperflux_capabilities": missing,
                }
            )
        elif direct_frame is not None:
            direct_frame["source_records"] = sorted(
                set(direct_frame["source_records"]) | set(topology_records)
            )
        settings.sort(key=lambda item: item["id"])
        sources_present = [
            upstream_id
            for upstream_id in UPSTREAM_IDS
            if link["source_records"][upstream_id]
        ]
        conflicts = _source_conflicts(link, inputs.records)
        if conflicts:
            knowledge_status = "conflicted"
        elif len(sources_present) == len(UPSTREAM_IDS):
            knowledge_status = "cross-referenced"
        elif sources_present:
            knowledge_status = "single-source"
        else:
            knowledge_status = "missing"
        hyperflux_support = "route-qualified" if linked_profiles else "candidate-only"
        reviewed_facts: list[dict[str, Any]] = []
        for fact in reviewed["facts"]:
            implementation_records = _implementation_records_for_fact(
                fact,
                selected_records,
                settings,
            )
            hyperflux_capabilities = _fact_hyperflux_capabilities(
                fact["semantic_capability"],
                qualified_capabilities,
            )
            source_kinds = sorted(
                {inputs.reviewed_sources[source_id]["kind"] for source_id in fact["source_ids"]}
            )
            copied_fact = deepcopy(fact)
            copied_fact.update(
                {
                    "assurance": _fact_assurance(
                        fact,
                        inputs.reviewed_sources,
                        implementation_records,
                        hyperflux_capabilities,
                    ),
                    "evidence_layers": {
                        "product_documentation": fact["claim_state"] == "documented-product",
                        "upstream_report": fact["claim_state"] == "reported-upstream",
                        "pinned_linux_implementation": bool(implementation_records),
                        "hyperflux_route_mapping": bool(hyperflux_capabilities),
                        "physical_qualification": bool(hyperflux_capabilities),
                    },
                    "implementation_records": implementation_records,
                    "hyperflux_capabilities": hyperflux_capabilities,
                    "hyperflux_evidence_claims": sorted(
                        {
                            claim
                            for capability in hyperflux_capabilities
                            for claim in qualification_evidence.get(capability, set())
                        }
                    ),
                    "source_kinds": source_kinds,
                }
            )
            reviewed_facts.append(copied_fact)
        reviewed_source_ids = list(reviewed["source_ids"])
        enriched_records = []
        for record in sorted(selected_records, key=lambda item: item["record_id"]):
            copied_record = deepcopy(record)
            copied_record["source_url"] = _source_record_url(
                record,
                inputs.source_catalogs,
            )
            enriched_records.append(copied_record)
        candidates.append(
            {
                "candidate_id": candidate["id"],
                "official_name": candidate["official_name"],
                "device_kind": candidate["device_kind"],
                "knowledge_status": knowledge_status,
                "sources_present": sources_present,
                "source_records": enriched_records,
                "source_conflicts": conflicts,
                "hyperflux_support": hyperflux_support,
                "hyperflux_profile_ids": list(link["hyperflux_profile_ids"]),
                "qualified_hyperflux_capabilities": sorted(qualified_capabilities),
                "settings": settings,
                "reviewed_on": inputs.reviewed_on,
                "reviewed_source_ids": reviewed_source_ids,
                "reviewed_facts": reviewed_facts,
                "knowledge_gaps": deepcopy(reviewed["gaps"]),
                "transport_matrix": _transport_matrix(
                    reviewed,
                    selected_records,
                    hyperflux_support,
                ),
                "coverage": {
                    "reviewed_fact_count": len(reviewed_facts),
                    "reviewed_source_count": len(reviewed_source_ids),
                    "official_source_count": sum(
                        inputs.reviewed_sources[source_id]["kind"] != "upstream-issue"
                        for source_id in reviewed_source_ids
                    ),
                    "upstream_report_count": sum(
                        inputs.reviewed_sources[source_id]["kind"] == "upstream-issue"
                        for source_id in reviewed_source_ids
                    ),
                    "implementation_record_count": len(selected_records),
                    "implemented_fact_count": sum(
                        bool(fact["implementation_records"]) for fact in reviewed_facts
                    ),
                    "physically_qualified_fact_count": sum(
                        bool(fact["hyperflux_capabilities"]) for fact in reviewed_facts
                    ),
                    "open_gap_count": len(reviewed["gaps"]),
                },
                "notes": list(link["notes"]),
            }
        )
    return {
        "$schema": "../../schemas/compiled-device-knowledge.schema.json",
        "schema": "hyperflux-compiled-device-knowledge-v1",
        "source_sha256": inputs.source_sha256,
        "candidate_snapshot_id": selected_snapshot,
        "candidate_snapshot_history": [
            {
                "snapshot_id": catalog["snapshot_id"],
                "retrieved_utc": catalog["retrieved_utc"],
                "supersedes_snapshot_id": catalog.get("supersedes_snapshot_id"),
                "candidate_count": len(catalog["candidates"]),
            }
            for catalog in sorted(
                (
                    load_json(path)
                    for path in (root / "profiles" / "candidates").glob("*.json")
                ),
                key=lambda value: value["retrieved_utc"],
            )
        ],
        "policy": {
            "source_knowledge_grants_transport_authority": False,
            "reviewed_candidate_links_required": True,
            "unmapped_selected_methods_allowed": False,
            "controls_require_qualified_hyperflux_capabilities": True,
            "source_conflicts_are_not_promoted": True,
        },
        "upstreams": [
            {
                **deepcopy(inputs.source_catalogs[value]["source"]),
                "record_count": len(inputs.source_catalogs[value]["records"]),
            }
            for value in UPSTREAM_IDS
        ],
        "reviewed_on": inputs.reviewed_on,
        "reviewed_sources": [
            deepcopy(inputs.reviewed_sources[source_id])
            for source_id in sorted(inputs.reviewed_sources)
        ],
        "candidates": candidates,
    }


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        raise ModelError(
            f"{path}: git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _validate_upstream_checkout(path: Path, upstream: dict[str, Any]) -> Path:
    if path.is_symlink():
        raise ModelError(f"{upstream['id']}: source checkout may not be a symbolic link")
    source = path.resolve()
    if not source.is_dir() or (source / ".git").is_symlink():
        raise ModelError(f"{upstream['id']}: source checkout is missing or unsafe")
    if _git(source, "rev-parse", "HEAD") != upstream["commit"]:
        raise ModelError(f"{upstream['id']}: source checkout differs from the integration pin")
    if _git(source, "status", "--porcelain", "--untracked-files=all"):
        raise ModelError(f"{upstream['id']}: source checkout has local modifications")
    if _git(source, "remote", "get-url", "origin") != upstream["repository"]:
        raise ModelError(f"{upstream['id']}: source checkout has an unexpected origin")
    return source


def import_upstream_catalogs(
    root: Path, openrazer_source: Path, openrgb_source: Path
) -> tuple[Path, Path]:
    upstreams = upstream_index(root)
    openrazer_source = _validate_upstream_checkout(
        openrazer_source, upstreams["openrazer"]
    )
    openrgb_source = _validate_upstream_checkout(openrgb_source, upstreams["openrgb"])
    catalogs = {
        "openrazer": extract_openrazer_catalog(
            openrazer_source,
            repository=upstreams["openrazer"]["repository"],
            commit=upstreams["openrazer"]["commit"],
            version=upstreams["openrazer"]["version"],
            license_expression=upstreams["openrazer"]["license_expression"],
        ),
        "openrgb": extract_openrgb_catalog(
            openrgb_source,
            repository=upstreams["openrgb"]["repository"],
            commit=upstreams["openrgb"]["commit"],
            version=upstreams["openrgb"]["version"],
            license_expression=upstreams["openrgb"]["license_expression"],
        ),
    }
    _validated_knowledge_inputs(root, catalogs)
    destination = root / "knowledge" / "upstreams"
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for upstream_id, catalog in catalogs.items():
        path = destination / f"{upstream_id}.json"
        path.write_text(
            json.dumps(catalog, indent=2, sort_keys=False, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        paths.append(path)
    return paths[0], paths[1]
