# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .model import ModelError, load_json, require_unique


IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$"
)
UPSTREAM_KEYS = {
    "id",
    "name",
    "repository",
    "version",
    "commit",
    "license_expression",
    "api_contract",
    "uses",
    "build_network_access",
}
ADAPTER_KEYS = {
    "id",
    "application",
    "kind",
    "status",
    "upstream_ids",
    "sdk_protocol_versions",
    "feature_families",
    "transport_access",
    "unrelated_device_policy",
    "coexistence_policy",
    "owns",
    "must_not_own",
}
ADAPTER_KINDS = {"native-plugin", "native-backend", "compatibility-service"}
ADAPTER_STATUSES = {"planned", "in-progress", "software-verified", "hardware-qualified"}
COEXISTENCE_POLICIES = {
    "application-plugin",
    "native-backend-beside-existing",
    "private-explicit-service-only",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"{label}: expected a non-empty string")
    return value


def _keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _sorted_unique_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ModelError(f"{label}: expected a non-empty list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ModelError(f"{label}: every item must be a non-empty string")
    require_unique(value, label)
    if value != sorted(value):
        raise ModelError(f"{label}: values must be sorted")
    return value


def _validate_upstreams(values: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(values, list) or not values:
        raise ModelError("integration upstream catalog is empty")
    identifiers = [_nonempty(value.get("id"), "upstream id") for value in values]
    require_unique(identifiers, "integration upstream id")
    if identifiers != sorted(identifiers):
        raise ModelError("integration upstreams must be sorted by id")
    for value in values:
        identifier = value["id"]
        _keys(value, UPSTREAM_KEYS, identifier)
        if IDENTIFIER.fullmatch(identifier) is None:
            raise ModelError(f"{identifier}: invalid upstream id")
        _nonempty(value["name"], f"{identifier} name")
        repository = _nonempty(value["repository"], f"{identifier} repository")
        if REPOSITORY.fullmatch(repository) is None:
            raise ModelError(f"{identifier}: repository must be a canonical GitHub HTTPS URL")
        _nonempty(value["version"], f"{identifier} version")
        if COMMIT.fullmatch(_nonempty(value["commit"], f"{identifier} commit")) is None:
            raise ModelError(f"{identifier}: commit must be 40 lowercase hex characters")
        _nonempty(value["license_expression"], f"{identifier} license")
        _nonempty(value["api_contract"], f"{identifier} API contract")
        uses = _sorted_unique_strings(value["uses"], f"{identifier} uses")
        if any(IDENTIFIER.fullmatch(item) is None for item in uses):
            raise ModelError(f"{identifier}: invalid upstream use")
        if value["build_network_access"] is not False:
            raise ModelError(f"{identifier}: builds must not fetch mutable upstream state")
    return {value["id"]: value for value in values}


def _validate_adapters(values: Any, upstreams: dict[str, dict[str, Any]]) -> None:
    if not isinstance(values, list) or not values:
        raise ModelError("integration adapter catalog is empty")
    identifiers = [_nonempty(value.get("id"), "adapter id") for value in values]
    require_unique(identifiers, "integration adapter id")
    if identifiers != sorted(identifiers):
        raise ModelError("integration adapters must be sorted by id")
    for value in values:
        identifier = value["id"]
        _keys(value, ADAPTER_KEYS, identifier)
        if IDENTIFIER.fullmatch(identifier) is None:
            raise ModelError(f"{identifier}: invalid adapter id")
        _nonempty(value["application"], f"{identifier} application")
        if value["kind"] not in ADAPTER_KINDS:
            raise ModelError(f"{identifier}: invalid adapter kind")
        if value["status"] not in ADAPTER_STATUSES:
            raise ModelError(f"{identifier}: invalid adapter status")
        upstream_ids = _sorted_unique_strings(
            value["upstream_ids"], f"{identifier} upstream ids"
        )
        unknown = sorted(set(upstream_ids) - set(upstreams))
        if unknown:
            raise ModelError(f"{identifier}: unknown upstream ids: {', '.join(unknown)}")
        versions = value["sdk_protocol_versions"]
        if (
            not isinstance(versions, list)
            or not versions
            or any(isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 65_535 for item in versions)
            or versions != sorted(set(versions))
        ):
            raise ModelError(f"{identifier}: SDK protocol versions must be sorted and unique")
        _sorted_unique_strings(value["feature_families"], f"{identifier} feature families")
        if value["transport_access"] != "sdk-only":
            raise ModelError(f"{identifier}: integration transport must remain SDK-only")
        if value["unrelated_device_policy"] != "preserve":
            raise ModelError(f"{identifier}: unrelated application devices must be preserved")
        if value["coexistence_policy"] not in COEXISTENCE_POLICIES:
            raise ModelError(f"{identifier}: invalid coexistence policy")
        _sorted_unique_strings(value["owns"], f"{identifier} ownership")
        _sorted_unique_strings(value["must_not_own"], f"{identifier} negative ownership")


def load_integration_catalog(root: Path) -> dict[str, Any]:
    catalog = load_json(root / "integrations" / "catalog.json")
    if catalog.get("schema") != "hyperflux-integration-catalog-v1":
        raise ModelError("unsupported integration catalog schema")
    expected = {"$schema", "schema", "upstreams", "adapters"}
    _keys(catalog, expected, "integration catalog")
    upstreams = _validate_upstreams(catalog["upstreams"])
    _validate_adapters(catalog["adapters"], upstreams)
    return catalog


def compiled_catalog(root: Path) -> dict[str, Any]:
    source = load_integration_catalog(root)
    canonical = {key: value for key, value in source.items() if key != "$schema"}
    return {
        **deepcopy(canonical),
        "source_sha256": hashlib.sha256(_canonical_bytes(canonical)).hexdigest(),
    }


def upstream_index(root: Path) -> dict[str, dict[str, Any]]:
    return {value["id"]: value for value in load_integration_catalog(root)["upstreams"]}
