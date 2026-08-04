# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .knowledge import load_knowledge_inputs
from .model import ModelError, load_json, sha256_file
from .openrazer import _ClassCatalog
from .profiles import load_profile_inputs
from .source_validated_catalog import (
    _validate_loaded_device,
    _validate_loaded_integration,
    _validate_publication_identity,
    load_source_validated_catalog as _load_source_validated_catalog,
)
from .source_validated_common import (
    ABSENT,
    BACKEND_REQUIRED,
    CATALOG_PATH,
    RAZER_VENDOR_ID,
    REPORT_BYTES,
    REPORT_FAMILY,
    SCHEMA,
    SCHEMA_REF,
    SOURCE_VALIDATED,
    SUPPORTED_KINDS,
    _canonical_bytes,
    _checkout_commit,
    _classification,
    _digest,
    _identity,
    _merge_rules,
    _require_exact_keys,
    _route_id,
    _validate_checkout,
    _wireless_records,
)
from .source_validated_openrazer import (
    _narrow_keyboard_led_storage,
    _narrow_led_selector,
    _openrazer_constructor_rules,
    _openrazer_integration,
    _parameterize_custom_frame,
    _parameterize_openrazer_rules,
    _parameterize_rule,
    _publication_identity,
    _read_rules,
)
from .source_validated_openrgb import (
    OPENRGB_CONTROLLER_CPP,
    OPENRGB_CONTROLLER_HEADER,
    OPENRGB_DEVICES_HEADER,
    OPENRGB_EXTENDED_FUNCTIONS,
    _cpp_function_source,
    _openrgb_extended_rules,
    _openrgb_integration,
    _openrgb_led_selectors,
    _openrgb_source_binding,
)


def _hardware_assurance(root: Path, identity: tuple[int, int, str]) -> dict[str, Any]:
    vendor_id, product_id, kind = identity
    matches = []
    for profile in load_profile_inputs(root).profiles:
        profile_identity = profile.get("identity", {})
        if (
            profile.get("device_kind") == kind
            and profile_identity.get("vendor_id") == vendor_id
            and profile_identity.get("product_id") == product_id
        ):
            matches.append(profile["profile_id"])
    if len(matches) > 1:
        raise ModelError(f"{_route_id(identity)}: hardware assurance identity is ambiguous")
    return {
        "state": "hardware-verified" if matches else "not-hardware-verified",
        "profile_id": matches[0] if matches else None,
    }


def _source_validated_devices(
    root: Path,
    openrazer_source: Path,
    openrgb_source: Path,
    openrazer_catalog: dict[str, Any],
    openrgb_catalog: dict[str, Any],
) -> list[dict[str, Any]]:
    openrazer_records = _wireless_records(openrazer_catalog)
    openrgb_records = _wireless_records(openrgb_catalog)
    scope = load_profile_inputs(root).official_receiver_scope
    official_entries = {
        (
            scope["vendor_id"],
            entry["wireless_product_id"],
            entry["device_kind"],
        ): entry
        for entry in scope["entries"]
    }
    identities = sorted(official_entries)
    constructor_rules = _openrazer_constructor_rules(root)
    class_catalog = _ClassCatalog(openrazer_source)
    selectors, default_selector = _openrgb_led_selectors(openrgb_source)
    devices: list[dict[str, Any]] = []
    for identity in identities:
        official = official_entries[identity]
        openrazer_matches = openrazer_records.get(identity, [])
        openrgb_matches = openrgb_records.get(identity, [])
        integration_records: dict[str, Any] = {}
        rules: list[dict[str, Any]] = []
        if len(openrazer_matches) > 1:
            integration_records["openrazer"] = {
                "state": BACKEND_REQUIRED,
                "reason": "ambiguous-exact-identity",
                "record_id": None,
            }
        elif openrazer_matches:
            integration, generated = _openrazer_integration(
                root,
                openrazer_source,
                class_catalog,
                openrazer_matches[0],
                constructor_rules,
            )
            if integration["state"] == SOURCE_VALIDATED and (
                integration["direct_usb_reference"] is None
                or integration["direct_usb_reference"]["product_id"]
                != official["direct_usb_product_id"]
            ):
                integration = {
                    "state": BACKEND_REQUIRED,
                    "reason": "official-direct-usb-identity-mismatch",
                    "record_id": openrazer_matches[0]["record_id"],
                }
                generated = []
            integration_records["openrazer"] = integration
            rules.extend(generated)
        else:
            integration_records["openrazer"] = {"state": ABSENT, "reason": None, "record_id": None}
        if len(openrgb_matches) > 1:
            integration_records["openrgb"] = {
                "state": BACKEND_REQUIRED,
                "reason": "ambiguous-exact-identity",
                "record_id": None,
            }
        elif openrgb_matches:
            integration, generated = _openrgb_integration(
                openrgb_source, openrgb_matches[0], selectors, default_selector
            )
            integration_records["openrgb"] = integration
            rules.extend(generated)
        else:
            integration_records["openrgb"] = {"state": ABSENT, "reason": None, "record_id": None}
        operational = any(
            integration_records[name]["state"] == SOURCE_VALIDATED
            for name in ("openrazer", "openrgb")
        )
        devices.append(
            {
                "route_id": _route_id(identity),
                "identity": {
                    "vendor_id": identity[0],
                    "product_id": identity[1],
                    "device_kind": identity[2],
                },
                "model_name": official["official_name"],
                "operational_state": SOURCE_VALIDATED if operational else BACKEND_REQUIRED,
                "report_family": REPORT_FAMILY,
                "hardware_assurance": _hardware_assurance(root, identity),
                "integrations": integration_records,
                "rules": _merge_rules(rules) if operational else [],
            }
        )
    if not devices or len(devices) > 256:
        raise ModelError("source-validated device catalog is empty or unbounded")
    return devices


def transformed_source_validated_catalog(
    root: Path,
    openrazer_source: Path,
    openrgb_source: Path,
) -> dict[str, Any]:
    inputs = load_knowledge_inputs(root)
    openrazer_catalog = inputs.source_catalogs["openrazer"]
    openrgb_catalog = inputs.source_catalogs["openrgb"]
    openrazer_source = _validate_checkout(openrazer_source, openrazer_catalog["source"])
    openrgb_source = _validate_checkout(openrgb_source, openrgb_catalog["source"])
    devices = _source_validated_devices(
        root,
        openrazer_source,
        openrgb_source,
        openrazer_catalog,
        openrgb_catalog,
    )
    return {
        "$schema": SCHEMA_REF,
        "schema": SCHEMA,
        "sources": {
            name: {
                "commit": inputs.source_catalogs[name]["source"]["commit"],
                "catalog_sha256": sha256_file(root / f"knowledge/upstreams/{name}.json"),
            }
            for name in ("openrazer", "openrgb")
        },
        "transport": {
            "protocol_family": REPORT_FAMILY,
            "report_bytes": REPORT_BYTES,
        },
        "devices": devices,
    }


def write_source_validated_catalog(
    root: Path,
    openrazer_source: Path,
    openrgb_source: Path,
) -> Path:
    destination = root / CATALOG_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            transformed_source_validated_catalog(root, openrazer_source, openrgb_source),
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def load_source_validated_catalog(root: Path) -> dict[str, Any]:
    return _load_source_validated_catalog(
        root,
        load_json_fn=load_json,
        load_profile_inputs_fn=load_profile_inputs,
    )
