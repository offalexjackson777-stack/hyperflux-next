# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from .knowledge import load_knowledge_inputs
from .model import ModelError, require_unique, sha256_file
from .source_validated_common import (
    ABSENT,
    BACKEND_REQUIRED,
    CATALOG_PATH,
    REPORT_BYTES,
    REPORT_FAMILY,
    SCHEMA,
    SCHEMA_REF,
    SOURCE_VALIDATED,
    _identity,
    _require_exact_keys,
    _route_id,
    _wireless_records,
)


def _validate_loaded_integration(
    name: str,
    integration: Any,
    identity: tuple[int, int, str],
    upstream_records: dict[tuple[int, int, str], list[dict[str, Any]]],
    label: str,
) -> str:
    state = integration.get("state") if isinstance(integration, dict) else None
    source_fields: set[str] = set()
    if name == "openrazer" and state == SOURCE_VALIDATED:
        source_fields = {
            "class_name",
            "source",
            "image_url",
            "methods",
            "driver_family",
            "event_file_regex",
            "direct_usb_reference",
            "publication_identity",
        }
    elif name == "openrgb" and state == SOURCE_VALIDATED:
        source_fields = {
            "source_device_key",
            "source",
            "transaction_id",
            "led_selector",
            "lighting_topology",
        }
    value = _require_exact_keys(
        integration,
        {"state", "reason", "record_id"} | source_fields,
        label,
    )
    state = value["state"]
    matches = upstream_records.get(identity, [])
    if state == ABSENT:
        if value["reason"] is not None or value["record_id"] is not None or matches:
            raise ModelError(f"{label}: absent integration contradicts the upstream catalog")
        return state
    if state == BACKEND_REQUIRED:
        if not isinstance(value["reason"], str) or not value["reason"]:
            raise ModelError(f"{label}: backend-required integration needs one reason")
        record_id = value["record_id"]
        if record_id is not None and not any(
            record["record_id"] == record_id for record in matches
        ):
            raise ModelError(f"{label}: backend-required record does not match exact identity")
        if not matches:
            raise ModelError(f"{label}: backend-required identity is absent upstream")
        return state
    if state != SOURCE_VALIDATED or value["reason"] is not None or len(matches) != 1:
        raise ModelError(f"{label}: source-validated integration is ambiguous")
    if value["record_id"] != matches[0]["record_id"]:
        raise ModelError(f"{label}: source-validated record does not match exact identity")
    return state


def _validate_publication_identity(
    integration: dict[str, Any], rules: list[dict[str, Any]], label: str
) -> None:
    identity = integration["publication_identity"]
    value = _require_exact_keys(
        identity,
        {
            "method",
            "transaction_id",
            "command_class",
            "command_id",
            "data_size",
            "arguments",
            "normalized_sha256",
        },
        f"{label} publication identity",
    )
    transaction_id = value["transaction_id"]
    data_size = value["data_size"]
    arguments = value["arguments"]
    if (
        value["method"] != "get_serial"
        or isinstance(transaction_id, bool)
        or not isinstance(transaction_id, int)
        or not 1 <= transaction_id <= 255
        or isinstance(data_size, bool)
        or not isinstance(data_size, int)
        or not 1 <= data_size <= 80
        or not isinstance(arguments, list)
        or len(arguments) != data_size
        or any(
            isinstance(argument, bool)
            or not isinstance(argument, int)
            or not 0 <= argument <= 255
            for argument in arguments
        )
    ):
        raise ModelError(f"{label}: publication identity request is invalid")
    normalized = bytearray(REPORT_BYTES)
    normalized[1] = transaction_id
    normalized[5] = data_size
    normalized[6] = value["command_class"]
    normalized[7] = value["command_id"]
    normalized[8 : 8 + data_size] = bytes(arguments)
    if value["normalized_sha256"] != hashlib.sha256(normalized).hexdigest():
        raise ModelError(f"{label}: publication identity digest is invalid")
    matches = [
        rule
        for rule in rules
        if "openrazer" in rule["integrations"]
        and value["method"] in rule["methods"]
        and rule["direction"] == "read"
        and rule["command_class"] == value["command_class"]
        and rule["command_id"] == value["command_id"]
        and data_size in rule["data_sizes"]
        and transaction_id in rule.get("transaction_ids", [])
        and rule["matcher"]
        == {
            "kind": "fields",
            "fields": [{"kind": "exact", "offset": 0, "bytes": arguments}],
        }
    ]
    if len(matches) != 1:
        raise ModelError(f"{label}: publication identity is not source-bound")


def _validate_loaded_device(
    device: Any,
    index: int,
    upstream_records: dict[str, dict[tuple[int, int, str], list[dict[str, Any]]]],
    official_records: dict[tuple[int, int, str], dict[str, Any]],
) -> str:
    label = f"source-validated device {index}"
    value = _require_exact_keys(
        device,
        {
            "route_id",
            "identity",
            "model_name",
            "operational_state",
            "report_family",
            "hardware_assurance",
            "integrations",
            "rules",
        },
        label,
    )
    identity_value = _require_exact_keys(
        value["identity"], {"vendor_id", "product_id", "device_kind"}, f"{label} identity"
    )
    identity = (
        identity_value["vendor_id"],
        identity_value["product_id"],
        identity_value["device_kind"],
    )
    if (
        _identity(
            {
                "usb_identity": {
                    "vendor_id": identity[0],
                    "product_id": identity[1],
                },
                "device_kind": identity[2],
            }
        )
        != identity
        or value["route_id"] != _route_id(identity)
        or not isinstance(value["model_name"], str)
        or not 1 <= len(value["model_name"]) <= 127
        or value["report_family"] != REPORT_FAMILY
    ):
        raise ModelError(f"{label}: exact identity is invalid")
    official = official_records.get(identity)
    if official is None or value["model_name"] != official["official_name"]:
        raise ModelError(f"{label}: identity is outside the official receiver scope")
    assurance = _require_exact_keys(
        value["hardware_assurance"], {"state", "profile_id"}, f"{label} assurance"
    )
    if not (
        (assurance == {"state": "not-hardware-verified", "profile_id": None})
        or (
            assurance["state"] == "hardware-verified"
            and isinstance(assurance["profile_id"], str)
            and assurance["profile_id"].startswith("child.")
        )
    ):
        raise ModelError(f"{label}: hardware assurance is invalid")
    integrations = _require_exact_keys(
        value["integrations"], {"openrazer", "openrgb"}, f"{label} integrations"
    )
    states = {
        name: _validate_loaded_integration(
            name,
            integrations[name],
            identity,
            upstream_records[name],
            f"{label} {name}",
        )
        for name in ("openrazer", "openrgb")
    }
    if states["openrazer"] == SOURCE_VALIDATED:
        direct = integrations["openrazer"]["direct_usb_reference"]
        if direct is None or direct["product_id"] != official["direct_usb_product_id"]:
            raise ModelError(f"{label}: OpenRazer direct identity differs from official scope")
    operational = any(state == SOURCE_VALIDATED for state in states.values())
    rules = value["rules"]
    if (
        value["operational_state"]
        != (SOURCE_VALIDATED if operational else BACKEND_REQUIRED)
        or not isinstance(rules, list)
        or len(rules) > 512
        or operational != bool(rules)
    ):
        raise ModelError(f"{label}: operational state is inconsistent")
    for rule_index, rule in enumerate(rules):
        rule_label = f"{label} rule {rule_index}"
        rule_keys = {
            "id",
            "integrations",
            "methods",
            "direction",
            "command_class",
            "command_id",
            "matcher",
            "sources",
            "data_sizes",
        }
        if not isinstance(rule, dict) or frozenset(rule) not in {
            frozenset(rule_keys),
            frozenset(rule_keys | {"transaction_ids"}),
        }:
            raise ModelError(f"{rule_label}: fields are invalid")
        rule_value = rule
        if "transaction_ids" in rule_value:
            transaction_ids = rule_value["transaction_ids"]
            if (
                not isinstance(transaction_ids, list)
                or not transaction_ids
                or transaction_ids != sorted(set(transaction_ids))
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 1 <= value <= 255
                    for value in transaction_ids
                )
            ):
                raise ModelError(f"{rule_label}: transaction IDs are invalid")
        adapters = rule_value["integrations"]
        if (
            not isinstance(adapters, list)
            or not adapters
            or adapters != sorted(set(adapters))
            or any(states.get(adapter) != SOURCE_VALIDATED for adapter in adapters)
        ):
            raise ModelError(f"{rule_label}: adapter scope is invalid")
    if states["openrazer"] == SOURCE_VALIDATED:
        _validate_publication_identity(
            integrations["openrazer"], rules, f"{label} OpenRazer"
        )
    return value["route_id"]


def load_source_validated_catalog(
    root: Path,
    *,
    load_json_fn: Callable[[Path], Any],
    load_profile_inputs_fn: Callable[[Path], Any],
) -> dict[str, Any]:
    catalog = load_json_fn(root / CATALOG_PATH)
    if catalog.get("$schema") != SCHEMA_REF or catalog.get("schema") != SCHEMA:
        raise ModelError("source-validated catalog schema is invalid")
    if catalog.get("transport") != {
        "protocol_family": REPORT_FAMILY,
        "report_bytes": REPORT_BYTES,
    }:
        raise ModelError("source-validated catalog transport is invalid")
    devices = catalog.get("devices")
    if not isinstance(devices, list) or not devices or len(devices) > 256:
        raise ModelError("source-validated catalog device set is invalid")
    inputs = load_knowledge_inputs(root)
    upstream_records = {
        name: _wireless_records(inputs.source_catalogs[name])
        for name in ("openrazer", "openrgb")
    }
    profile_inputs = load_profile_inputs_fn(root)
    scope = profile_inputs.official_receiver_scope
    official_records = {
        (
            scope["vendor_id"],
            entry["wireless_product_id"],
            entry["device_kind"],
        ): entry
        for entry in scope["entries"]
    }
    route_ids = [
        _validate_loaded_device(device, index, upstream_records, official_records)
        for index, device in enumerate(devices)
    ]
    require_unique(route_ids, "source-validated route id")
    if route_ids != sorted(route_ids):
        raise ModelError("source-validated routes must be sorted")
    if set(route_ids) != {_route_id(identity) for identity in official_records}:
        raise ModelError("source-validated catalog does not cover the exact official receiver scope")
    for name in ("openrazer", "openrgb"):
        source = catalog.get("sources", {}).get(name, {})
        upstream = inputs.source_catalogs[name]["source"]
        if source != {
            "commit": upstream["commit"],
            "catalog_sha256": sha256_file(root / f"knowledge/upstreams/{name}.json"),
        }:
            raise ModelError(f"source-validated {name} catalog binding drifted")
    return catalog
