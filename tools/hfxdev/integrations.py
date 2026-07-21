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
DBUS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
OBJECT_PATH = re.compile(r"^(/[A-Za-z_][A-Za-z0-9_]*)+$")
OPENRAZER_CONTRACT_KEYS = {
    "$schema",
    "schema",
    "upstream",
    "service",
    "interfaces",
    "capability_policy",
    "safety",
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


def load_openrazer_compatibility_contract(root: Path) -> dict[str, Any]:
    value = load_json(root / "integrations" / "openrazer" / "compatibility.json")
    _keys(value, OPENRAZER_CONTRACT_KEYS, "OpenRazer compatibility contract")
    if value["schema"] != "hyperflux-openrazer-compatibility-v1":
        raise ModelError("unsupported OpenRazer compatibility contract schema")
    if value["$schema"] != "../../schemas/openrazer-compatibility.schema.json":
        raise ModelError("OpenRazer compatibility contract has a non-canonical schema reference")

    upstream = value["upstream"]
    _keys(upstream, {"id", "commit", "api_contract"}, "OpenRazer compatibility upstream")
    catalog_upstream = upstream_index(root)["openrazer"]
    if upstream != {
        "id": catalog_upstream["id"],
        "commit": catalog_upstream["commit"],
        "api_contract": catalog_upstream["api_contract"],
    }:
        raise ModelError("OpenRazer compatibility contract drifts from its upstream pin")

    service = value["service"]
    _keys(
        service,
        {"private_identity", "legacy_identity", "reconcile_interval_ms", "lifecycle"},
        "OpenRazer compatibility service",
    )
    for key in ("private_identity", "legacy_identity"):
        identity = service[key]
        _keys(
            identity,
            {"bus_name", "root_path", "requires_isolated_session", "activation_file_installed"},
            f"OpenRazer {key}",
        )
        if DBUS_NAME.fullmatch(identity["bus_name"]) is None:
            raise ModelError(f"OpenRazer {key} has an invalid D-Bus name")
        if OBJECT_PATH.fullmatch(identity["root_path"]) is None:
            raise ModelError(f"OpenRazer {key} has an invalid object path")
        if identity["activation_file_installed"] is not False:
            raise ModelError("OpenRazer compatibility must not install D-Bus activation")
    private = service["private_identity"]
    legacy = service["legacy_identity"]
    if private["bus_name"] == "org.razer" or private["root_path"].startswith("/org/razer"):
        raise ModelError("OpenRazer private identity must be HyperFlux-namespaced")
    if private["requires_isolated_session"] is not False:
        raise ModelError("OpenRazer private identity must work on the selected session bus")
    if legacy["bus_name"] != "org.razer" or legacy["root_path"] != "/org/razer":
        raise ModelError("OpenRazer legacy identity must reproduce the exact upstream root")
    if legacy["requires_isolated_session"] is not True:
        raise ModelError("OpenRazer legacy identity must require an isolated session")
    intervals = service["reconcile_interval_ms"]
    _keys(intervals, {"minimum", "default", "maximum"}, "OpenRazer intervals")
    if not (
        isinstance(intervals["minimum"], int)
        and isinstance(intervals["default"], int)
        and isinstance(intervals["maximum"], int)
        and 250 <= intervals["minimum"] <= intervals["default"] <= intervals["maximum"] <= 300_000
    ):
        raise ModelError("OpenRazer reconciliation intervals are invalid")
    if service["lifecycle"] != "on-demand-process":
        raise ModelError("OpenRazer compatibility must remain on-demand")

    interfaces = value["interfaces"]
    if not isinstance(interfaces, list) or not interfaces:
        raise ModelError("OpenRazer compatibility has no interfaces")
    names = [interface.get("name") for interface in interfaces]
    require_unique(names, "OpenRazer compatibility interface")
    if names != sorted(names):
        raise ModelError("OpenRazer compatibility interfaces must be sorted")
    for interface in interfaces:
        _keys(interface, {"name", "methods", "signals"}, f"OpenRazer {interface['name']}")
        methods = interface["methods"]
        signals = interface["signals"]
        if not isinstance(methods, list) or not isinstance(signals, list):
            raise ModelError(f"OpenRazer {interface['name']} members must be arrays")
        method_names = [method.get("name") for method in methods]
        require_unique(method_names, f"OpenRazer {interface['name']} method")
        if method_names != sorted(method_names):
            raise ModelError(f"OpenRazer {interface['name']} methods must be sorted")
        for method in methods:
            _keys(method, {"name", "in", "out"}, f"OpenRazer method {method.get('name')}")
            if not all(isinstance(method[field], str) for field in ("name", "in", "out")):
                raise ModelError("OpenRazer method signatures must be strings")
        if any(not isinstance(signal, str) for signal in signals):
            raise ModelError("OpenRazer signal names must be strings")
        require_unique(signals, f"OpenRazer {interface['name']} signal")
        if signals != sorted(signals):
            raise ModelError(f"OpenRazer {interface['name']} signals must be sorted")

    policy = value["capability_policy"]
    _keys(
        policy,
        {"required_for_export", "qualified_translations", "not_advertised"},
        "OpenRazer capability policy",
    )
    for key in sorted(policy):
        _sorted_unique_strings(policy[key], f"OpenRazer capability policy {key}")
    safety = value["safety"]
    _keys(
        safety,
        {
            "transport_access",
            "unknown_profile_writes",
            "uncertain_write_replay",
            "official_daemon_replacement",
            "unrelated_device_suppression",
            "hardware_serial_export",
            "global_activation_file",
        },
        "OpenRazer safety policy",
    )
    if safety["transport_access"] != "sdk-only" or any(
        safety[key] is not False for key in safety if key != "transport_access"
    ):
        raise ModelError("OpenRazer compatibility safety policy is not fail-closed")
    return value
