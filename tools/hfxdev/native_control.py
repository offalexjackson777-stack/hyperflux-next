# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .integrations import upstream_index
from .model import ModelError, load_json, require_unique, sha256_file
from .openrazer import (
    _ClassCatalog,
    _model_name,
    _source_commit,
    load_import_selection,
    load_imported_metadata,
    transformed_metadata,
)


POLICY_SCHEMA = "hyperflux-openrazer-native-control-policy-v1"
INVENTORY_SCHEMA = "hyperflux-openrazer-native-method-inventory-v1"
METHOD_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
PROFILE_PATTERN = re.compile(r"child\.[a-z0-9.-]+\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

POLICY_KEYS = {
    "$schema",
    "schema",
    "upstream_id",
    "source_commit",
    "scope",
    "report_size",
    "common_methods",
    "kind_methods",
    "classifications",
    "qualification_probes",
    "privacy_overrides",
}
CLASSIFICATION_KEYS = {
    "read_only",
    "reversible_with_readback",
    "software_only",
    "unsafe",
}
INVENTORY_KEYS = {
    "$schema",
    "schema",
    "upstream",
    "policy_sha256",
    "metadata_selection_sha256",
    "report_size",
    "source_files",
    "devices",
}
DEVICE_KEYS = {
    "profile_id",
    "wireless_identity",
    "direct_usb_reference",
    "source",
    "methods",
    "qualification_probes",
}
METHOD_KEYS = {
    "id",
    "origin",
    "classification",
    "transport",
    "readback",
    "privacy",
}
DIRECT_REFERENCE_KEYS = {
    "vendor_id",
    "product_id",
    "model_name",
    "device_kind",
    "transport_variant",
    "class_name",
    "source_path",
    "source_line",
}

COMMON_SOURCE_PATHS = (
    "daemon/openrazer_daemon/hardware/device_base.py",
    "driver/razercommon.c",
    "driver/razercommon.h",
    "driver/razerchromacommon.c",
    "driver/razerchromacommon.h",
)
KIND_SOURCE_PATHS = {
    "keyboard": (
        "daemon/openrazer_daemon/hardware/keyboards.py",
        "driver/razerkbd_driver.c",
        "driver/razerkbd_driver.h",
    ),
    "mouse": (
        "daemon/openrazer_daemon/hardware/mouse.py",
        "driver/razermouse_driver.c",
        "driver/razermouse_driver.h",
    ),
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _method_list(value: Any, label: str, *, sorted_required: bool) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 512
        or any(not isinstance(item, str) or METHOD_PATTERN.fullmatch(item) is None for item in value)
    ):
        raise ModelError(f"{label}: expected a bounded method list")
    require_unique(value, label)
    if sorted_required and value != sorted(value):
        raise ModelError(f"{label}: methods must be sorted")
    return value


def _policy_digest(policy: dict[str, Any]) -> str:
    return _digest({key: value for key, value in policy.items() if key != "$schema"})


def _expected_methods(
    metadata: dict[str, Any], policy: dict[str, Any]
) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = {}
    for device in metadata["devices"]:
        methods = set(device["advertised_methods"])
        methods.update(policy["common_methods"])
        methods.update(policy["kind_methods"][device["identity"]["device_kind"]])
        expected[device["profile_id"]] = methods
    return expected


def _classification_index(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for method in policy["classifications"]["read_only"]:
        index[method] = {
            "classification": "read-only",
            "transport": "feature-report",
            "readback": None,
        }
    for value in policy["classifications"]["reversible_with_readback"]:
        index[value["method"]] = {
            "classification": "reversible-with-readback",
            "transport": "feature-report",
            "readback": value["readback"],
        }
    for method in policy["classifications"]["software_only"]:
        index[method] = {
            "classification": "software-only",
            "transport": "software-only",
            "readback": None,
        }
    for method in policy["classifications"]["unsafe"]:
        index[method] = {
            "classification": "unsafe",
            "transport": "feature-report",
            "readback": None,
        }
    return index


def load_native_control_policy(root: Path) -> dict[str, Any]:
    path = root / "integrations" / "openrazer" / "native-control-policy.json"
    policy = load_json(path)
    _exact_keys(policy, POLICY_KEYS, "OpenRazer native-control policy")
    if policy["$schema"] != "../../schemas/openrazer-native-control-policy.schema.json":
        raise ModelError("OpenRazer native-control policy schema reference is not canonical")
    if policy["schema"] != POLICY_SCHEMA:
        raise ModelError("unsupported OpenRazer native-control policy schema")
    upstream = upstream_index(root).get(policy["upstream_id"])
    if upstream is None or upstream["id"] != "openrazer":
        raise ModelError("OpenRazer native-control policy names an unknown upstream")
    if policy["source_commit"] != upstream["commit"]:
        raise ModelError("OpenRazer native-control policy differs from the upstream pin")
    if policy["scope"] != "transport-facing-exact-wireless-classes":
        raise ModelError("OpenRazer native-control policy has an unsupported scope")
    if policy["report_size"] != 90:
        raise ModelError("OpenRazer native control must use one 90-byte feature report")

    common = _method_list(policy["common_methods"], "common methods", sorted_required=True)
    kind_methods = policy["kind_methods"]
    if not isinstance(kind_methods, dict):
        raise ModelError("OpenRazer kind methods must be an object")
    _exact_keys(kind_methods, {"keyboard", "mouse"}, "OpenRazer kind methods")
    for kind in ("keyboard", "mouse"):
        _method_list(kind_methods[kind], f"{kind} methods", sorted_required=True)

    classifications = policy["classifications"]
    if not isinstance(classifications, dict):
        raise ModelError("OpenRazer method classifications must be an object")
    _exact_keys(classifications, CLASSIFICATION_KEYS, "OpenRazer method classifications")
    classified_groups: list[list[str]] = []
    for key in ("read_only", "software_only", "unsafe"):
        classified_groups.append(
            _method_list(
                classifications[key],
                key.replace("_", " "),
                sorted_required=True,
            )
        )
    reversible = classifications["reversible_with_readback"]
    if not isinstance(reversible, list) or len(reversible) > 512:
        raise ModelError("reversible methods must be a bounded list")
    reversible_methods: list[str] = []
    read_only = set(classifications["read_only"])
    for index, value in enumerate(reversible):
        label = f"reversible method {index}"
        if not isinstance(value, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(value, {"method", "readback"}, label)
        method, readback = value["method"], value["readback"]
        if (
            not isinstance(method, str)
            or METHOD_PATTERN.fullmatch(method) is None
            or not isinstance(readback, str)
            or METHOD_PATTERN.fullmatch(readback) is None
        ):
            raise ModelError(f"{label}: invalid method name")
        if readback not in read_only:
            raise ModelError(f"{label}: readback is not classified read-only")
        reversible_methods.append(method)
    require_unique(reversible_methods, "reversible method")
    if reversible_methods != sorted(reversible_methods):
        raise ModelError("reversible methods must be sorted")
    classified_groups.append(reversible_methods)
    flat = [method for group in classified_groups for method in group]
    require_unique(flat, "classified method")

    metadata = load_imported_metadata(root)
    expected_by_profile = _expected_methods(metadata, policy)
    expected_union = set().union(*expected_by_profile.values())
    if set(flat) != expected_union:
        missing = sorted(expected_union - set(flat))
        unused = sorted(set(flat) - expected_union)
        details = []
        if missing:
            details.append(f"unclassified: {', '.join(missing)}")
        if unused:
            details.append(f"not exposed: {', '.join(unused)}")
        raise ModelError("OpenRazer native-control method drift: " + "; ".join(details))

    probes = policy["qualification_probes"]
    if not isinstance(probes, list) or not probes:
        raise ModelError("OpenRazer qualification probes are empty")
    probe_profiles: list[str] = []
    for index, probe in enumerate(probes):
        label = f"OpenRazer qualification probe {index}"
        if not isinstance(probe, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(probe, {"profile_id", "methods"}, label)
        profile_id = probe["profile_id"]
        if not isinstance(profile_id, str) or PROFILE_PATTERN.fullmatch(profile_id) is None:
            raise ModelError(f"{label}: invalid profile id")
        methods = _method_list(probe["methods"], f"{label} methods", sorted_required=False)
        if profile_id not in expected_by_profile:
            raise ModelError(f"{label}: unknown profile")
        if not set(methods) <= expected_by_profile[profile_id]:
            raise ModelError(f"{label}: includes a method absent from the exact class")
        if not set(methods) <= read_only:
            raise ModelError(f"{label}: includes a method that is not read-only")
        probe_profiles.append(profile_id)
    require_unique(probe_profiles, "qualification probe profile")
    if set(probe_profiles) != set(expected_by_profile):
        raise ModelError("OpenRazer qualification probes do not cover every exact class")

    privacy = policy["privacy_overrides"]
    if not isinstance(privacy, dict):
        raise ModelError("OpenRazer privacy overrides must be an object")
    for method, mode in privacy.items():
        if method not in read_only or mode not in {"summary-only", "redacted-equality-only"}:
            raise ModelError("OpenRazer privacy override is invalid")
    if privacy.get("get_serial") != "redacted-equality-only":
        raise ModelError("OpenRazer serial probes must be redacted")
    if not common:
        raise ModelError("OpenRazer common method authority is empty")
    return policy


def _direct_usb_reference(
    catalog: _ClassCatalog,
    class_name: str,
    wireless_identity: dict[str, Any],
) -> dict[str, Any]:
    for ancestor in catalog.linearization(class_name)[1:]:
        parsed = catalog.classes[ancestor]
        try:
            vendor_id = catalog.field(ancestor, "USB_VID")
            product_id = catalog.field(ancestor, "USB_PID")
        except KeyError:
            continue
        model_name = _model_name(parsed, strict=False)
        if "(Wired)" not in model_name or product_id == wireless_identity["product_id"]:
            continue
        if vendor_id != wireless_identity["vendor_id"]:
            raise ModelError(f"OpenRazer wired ancestor {ancestor} changes vendor identity")
        return {
            "vendor_id": vendor_id,
            "product_id": product_id,
            "model_name": model_name,
            "device_kind": wireless_identity["device_kind"],
            "transport_variant": "wired",
            "class_name": ancestor,
            "source_path": parsed.source_file,
            "source_line": parsed.line,
        }
    raise ModelError(f"OpenRazer class {class_name} has no exact wired reference ancestor")


def _source_file_records(source_root: Path, metadata: dict[str, Any]) -> list[dict[str, str]]:
    paths = set(COMMON_SOURCE_PATHS)
    for device in metadata["devices"]:
        paths.add(device["source"]["path"])
        paths.update(KIND_SOURCE_PATHS[device["identity"]["device_kind"]])
    records: list[dict[str, str]] = []
    for relative in sorted(paths):
        path = source_root / relative
        if path.is_symlink() or not path.is_file():
            raise ModelError(f"OpenRazer native-control source is missing or symbolic: {relative}")
        records.append({"path": relative, "sha256": sha256_file(path)})
    return records


def transformed_native_inventory(root: Path, source_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    policy = load_native_control_policy(root)
    metadata = load_imported_metadata(root)
    if _source_commit(source_root) != policy["source_commit"]:
        raise ModelError("OpenRazer native-control checkout does not match the immutable pin")
    if transformed_metadata(root, source_root) != metadata:
        raise ModelError("OpenRazer metadata must be refreshed before native-control import")

    catalog = _ClassCatalog(source_root)
    selection = load_import_selection(root)
    selected_by_profile = {value["profile_id"]: value for value in selection["devices"]}
    classification = _classification_index(policy)
    probes = {value["profile_id"]: value["methods"] for value in policy["qualification_probes"]}
    privacy = policy["privacy_overrides"]
    devices: list[dict[str, Any]] = []
    for device in metadata["devices"]:
        profile_id = device["profile_id"]
        selected = selected_by_profile[profile_id]
        wireless_identity = deepcopy(device["identity"])
        method_names = set(device["advertised_methods"])
        method_names.update(policy["common_methods"])
        method_names.update(policy["kind_methods"][wireless_identity["device_kind"]])
        methods: list[dict[str, Any]] = []
        for method in sorted(method_names):
            record = deepcopy(classification[method])
            if method in device["advertised_methods"]:
                origin = "advertised"
            elif method in policy["common_methods"]:
                origin = "common-daemon-api"
            else:
                origin = "device-kind-mixin"
            methods.append(
                {
                    "id": method,
                    "origin": origin,
                    **record,
                    "privacy": privacy.get(method, "summary-only"),
                }
            )
        devices.append(
            {
                "profile_id": profile_id,
                "wireless_identity": wireless_identity,
                "direct_usb_reference": _direct_usb_reference(
                    catalog, selected["class_name"], wireless_identity
                ),
                "source": deepcopy(device["source"]),
                "methods": methods,
                "qualification_probes": deepcopy(probes[profile_id]),
            }
        )
    upstream = upstream_index(root)["openrazer"]
    return {
        "$schema": "../../schemas/openrazer-native-method-inventory.schema.json",
        "schema": INVENTORY_SCHEMA,
        "upstream": {
            key: upstream[key]
            for key in ("id", "repository", "version", "commit", "license_expression")
        },
        "policy_sha256": _policy_digest(policy),
        "metadata_selection_sha256": metadata["selection_sha256"],
        "report_size": policy["report_size"],
        "source_files": _source_file_records(source_root, metadata),
        "devices": devices,
    }


def load_native_method_inventory(root: Path) -> dict[str, Any]:
    path = root / "integrations" / "openrazer" / "native-method-inventory.json"
    inventory = load_json(path)
    _exact_keys(inventory, INVENTORY_KEYS, "OpenRazer native method inventory")
    if inventory["$schema"] != "../../schemas/openrazer-native-method-inventory.schema.json":
        raise ModelError("OpenRazer native method inventory schema reference is not canonical")
    if inventory["schema"] != INVENTORY_SCHEMA:
        raise ModelError("unsupported OpenRazer native method inventory schema")
    policy = load_native_control_policy(root)
    metadata = load_imported_metadata(root)
    if inventory["policy_sha256"] != _policy_digest(policy):
        raise ModelError("OpenRazer native method inventory policy is stale")
    if inventory["metadata_selection_sha256"] != metadata["selection_sha256"]:
        raise ModelError("OpenRazer native method inventory metadata selection is stale")
    upstream = upstream_index(root)["openrazer"]
    expected_upstream = {
        key: upstream[key]
        for key in ("id", "repository", "version", "commit", "license_expression")
    }
    if inventory["upstream"] != expected_upstream:
        raise ModelError("OpenRazer native method inventory upstream binding has drifted")
    if inventory["report_size"] != policy["report_size"]:
        raise ModelError("OpenRazer native method inventory report size has drifted")

    sources = inventory["source_files"]
    if not isinstance(sources, list) or not sources:
        raise ModelError("OpenRazer native method inventory source binding is empty")
    source_paths: list[str] = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ModelError(f"OpenRazer native source {index}: expected an object")
        _exact_keys(source, {"path", "sha256"}, f"OpenRazer native source {index}")
        if (
            not isinstance(source["path"], str)
            or Path(source["path"]).is_absolute()
            or ".." in Path(source["path"]).parts
            or not isinstance(source["sha256"], str)
            or SHA256_PATTERN.fullmatch(source["sha256"]) is None
        ):
            raise ModelError(f"OpenRazer native source {index}: invalid source binding")
        source_paths.append(source["path"])
    require_unique(source_paths, "OpenRazer native source path")
    if source_paths != sorted(source_paths):
        raise ModelError("OpenRazer native source bindings must be sorted")

    metadata_by_profile = {value["profile_id"]: value for value in metadata["devices"]}
    expected_by_profile = _expected_methods(metadata, policy)
    classification = _classification_index(policy)
    probes = {value["profile_id"]: value["methods"] for value in policy["qualification_probes"]}
    devices = inventory["devices"]
    if not isinstance(devices, list) or not devices:
        raise ModelError("OpenRazer native method inventory has no devices")
    profile_ids: list[str] = []
    for index, device in enumerate(devices):
        label = f"OpenRazer native device {index}"
        if not isinstance(device, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(device, DEVICE_KEYS, label)
        profile_id = device["profile_id"]
        if not isinstance(profile_id, str) or PROFILE_PATTERN.fullmatch(profile_id) is None:
            raise ModelError(f"{label}: invalid profile id")
        metadata_device = metadata_by_profile.get(profile_id)
        if metadata_device is None:
            raise ModelError(f"{label}: profile is absent from imported metadata")
        if device["wireless_identity"] != metadata_device["identity"]:
            raise ModelError(f"{label}: wireless identity has drifted")
        if device["source"] != metadata_device["source"]:
            raise ModelError(f"{label}: class source has drifted")
        direct = device["direct_usb_reference"]
        if not isinstance(direct, dict):
            raise ModelError(f"{label}: direct USB reference must be an object")
        _exact_keys(direct, DIRECT_REFERENCE_KEYS, f"{label} direct USB reference")
        if (
            direct["vendor_id"] != metadata_device["identity"]["vendor_id"]
            or direct["product_id"] == metadata_device["identity"]["product_id"]
            or direct["device_kind"] != metadata_device["identity"]["device_kind"]
            or direct["transport_variant"] != "wired"
            or not isinstance(direct["source_line"], int)
            or direct["source_line"] < 1
        ):
            raise ModelError(f"{label}: invalid direct USB reference")

        method_records = device["methods"]
        if not isinstance(method_records, list) or not method_records:
            raise ModelError(f"{label}: method inventory is empty")
        method_ids: list[str] = []
        for method_index, method in enumerate(method_records):
            method_label = f"{label} method {method_index}"
            if not isinstance(method, dict):
                raise ModelError(f"{method_label}: expected an object")
            _exact_keys(method, METHOD_KEYS, method_label)
            method_id = method["id"]
            if method_id not in expected_by_profile[profile_id]:
                raise ModelError(f"{method_label}: method is absent from the exact class")
            expected = classification[method_id]
            for key in ("classification", "transport", "readback"):
                if method[key] != expected[key]:
                    raise ModelError(f"{method_label}: {key} differs from policy")
            expected_origin = (
                "advertised"
                if method_id in metadata_device["advertised_methods"]
                else (
                    "common-daemon-api"
                    if method_id in policy["common_methods"]
                    else "device-kind-mixin"
                )
            )
            if method["origin"] != expected_origin:
                raise ModelError(f"{method_label}: origin differs from the exact class")
            expected_privacy = policy["privacy_overrides"].get(method_id, "summary-only")
            if method["privacy"] != expected_privacy:
                raise ModelError(f"{method_label}: privacy handling differs from policy")
            method_ids.append(method_id)
        require_unique(method_ids, f"{label} method")
        if method_ids != sorted(method_ids) or set(method_ids) != expected_by_profile[profile_id]:
            raise ModelError(f"{label}: method inventory is incomplete or unsorted")
        if device["qualification_probes"] != probes[profile_id]:
            raise ModelError(f"{label}: qualification probe order has drifted")
        profile_ids.append(profile_id)
    require_unique(profile_ids, "OpenRazer native profile")
    if profile_ids != sorted(metadata_by_profile):
        raise ModelError("OpenRazer native method inventory does not cover the exact selection")
    return inventory


def write_native_method_inventory(root: Path, source_root: Path) -> Path:
    destination = root / "integrations" / "openrazer" / "native-method-inventory.json"
    value = transformed_native_inventory(root, source_root)
    destination.write_text(
        json.dumps(value, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return destination
