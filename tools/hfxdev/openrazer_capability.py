# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import ast
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
    _source_commit,
    load_import_selection,
    load_imported_metadata,
    transformed_metadata,
)


POLICY_SCHEMA = "hyperflux-openrazer-userspace-capability-policy-v1"
CATALOG_SCHEMA = "hyperflux-openrazer-userspace-capability-catalog-v1"
POLICY_KEYS = {
    "$schema",
    "schema",
    "upstream_id",
    "source_commit",
    "service",
    "root",
    "device",
    "features",
}
SERVICE_KEYS = {"bus_name", "root_path", "device_path_prefix"}
ROOT_KEYS = {"required_methods", "diagnostic_methods", "required_signals"}
DEVICE_KEYS = {"required_common", "required_by_kind", "semantic_probes"}
FEATURE_KEYS = {"id", "device_kinds", "requirement", "methods"}
METHOD_ID = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
FEATURE_ID = re.compile(r"[a-z][a-z0-9.-]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_PATHS = (
    "daemon/openrazer_daemon/daemon.py",
    "daemon/openrazer_daemon/hardware/device_base.py",
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _exact(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _method_ids(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 128
        or any(not isinstance(item, str) or METHOD_ID.fullmatch(item) is None for item in value)
    ):
        raise ModelError(f"{label}: expected a bounded method-id list")
    require_unique(value, label)
    if value != sorted(value):
        raise ModelError(f"{label}: method ids must be sorted")
    return value


def load_userspace_capability_policy(root: Path) -> dict[str, Any]:
    policy = load_json(root / "integrations" / "openrazer" / "userspace-capability-policy.json")
    _exact(policy, POLICY_KEYS, "OpenRazer userspace capability policy")
    if policy["$schema"] != "../../schemas/openrazer-userspace-capability-policy.schema.json":
        raise ModelError("OpenRazer userspace capability policy schema reference is not canonical")
    if policy["schema"] != POLICY_SCHEMA:
        raise ModelError("unsupported OpenRazer userspace capability policy schema")
    upstream = upstream_index(root).get(policy["upstream_id"])
    if upstream is None or upstream["id"] != "openrazer":
        raise ModelError("OpenRazer userspace capability policy names an unknown upstream")
    if policy["source_commit"] != upstream["commit"]:
        raise ModelError("OpenRazer userspace capability policy does not match the source pin")
    _exact(policy["service"], SERVICE_KEYS, "OpenRazer userspace service")
    if policy["service"] != {
        "bus_name": "org.razer",
        "root_path": "/org/razer",
        "device_path_prefix": "/org/razer/device/",
    }:
        raise ModelError("OpenRazer userspace service identity is not canonical")
    _exact(policy["root"], ROOT_KEYS, "OpenRazer userspace root")
    _method_ids(policy["root"]["required_methods"], "OpenRazer root methods")
    _method_ids(policy["root"]["diagnostic_methods"], "OpenRazer root diagnostics")
    _method_ids(policy["root"]["required_signals"], "OpenRazer root signals")
    overlap = set(policy["root"]["required_methods"]) & set(
        policy["root"]["diagnostic_methods"]
    )
    if overlap:
        raise ModelError("OpenRazer root diagnostics cannot also gate compatibility")
    _exact(policy["device"], DEVICE_KEYS, "OpenRazer userspace device")
    _method_ids(policy["device"]["required_common"], "OpenRazer required common methods")
    _method_ids(policy["device"]["semantic_probes"], "OpenRazer semantic probes")
    if set(policy["device"]["required_by_kind"]) != {"keyboard", "mouse"}:
        raise ModelError("OpenRazer required methods must cover keyboard and mouse")
    for kind, methods in policy["device"]["required_by_kind"].items():
        _method_ids(methods, f"OpenRazer {kind} required methods")
    if not isinstance(policy["features"], list) or not 1 <= len(policy["features"]) <= 64:
        raise ModelError("OpenRazer userspace features must be a bounded list")
    feature_ids: list[str] = []
    for index, feature in enumerate(policy["features"]):
        label = f"OpenRazer userspace feature {index}"
        if not isinstance(feature, dict):
            raise ModelError(f"{label}: expected an object")
        _exact(feature, FEATURE_KEYS, label)
        if not isinstance(feature["id"], str) or FEATURE_ID.fullmatch(feature["id"]) is None:
            raise ModelError(f"{label}: invalid feature id")
        feature_ids.append(feature["id"])
        if (
            not isinstance(feature["device_kinds"], list)
            or not feature["device_kinds"]
            or feature["device_kinds"] != sorted(feature["device_kinds"])
            or any(kind not in {"keyboard", "mouse"} for kind in feature["device_kinds"])
        ):
            raise ModelError(f"{label}: invalid device kinds")
        require_unique(feature["device_kinds"], f"{label} device kind")
        if feature["requirement"] not in {"required", "optional"}:
            raise ModelError(f"{label}: invalid requirement")
        _method_ids(feature["methods"], f"{label} methods")
    require_unique(feature_ids, "OpenRazer userspace feature id")
    semantic = set(policy["device"]["semantic_probes"])
    required = set(policy["device"]["required_common"])
    if not semantic or not semantic <= required:
        raise ModelError("OpenRazer semantic probes must be required common methods")
    return policy


def _constant_string(value: ast.expr | None) -> str | None:
    if value is None or isinstance(value, ast.Constant) and value.value is None:
        return ""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _callback_id(value: ast.expr) -> str | None:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _method_record(
    method_id: str,
    interface: str,
    member: str,
    in_signature: str,
    out_signature: str,
) -> dict[str, str]:
    return {
        "id": method_id,
        "interface": interface,
        "member": member,
        "in_signature": in_signature,
        "out_signature": out_signature,
    }


def _register_method(
    methods: dict[str, dict[str, str]],
    record: dict[str, str],
    label: str,
) -> None:
    existing = methods.get(record["id"])
    if existing is not None and existing != record:
        raise ModelError(f"OpenRazer D-Bus method {record['id']} is ambiguous in {label}")
    methods[record["id"]] = record


def _decorated_methods(source_root: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    directory = source_root / "daemon" / "openrazer_daemon" / "dbus_services" / "dbus_methods"
    if directory.is_symlink() or not directory.is_dir():
        raise ModelError("OpenRazer D-Bus method source directory is missing or symbolic")
    methods: dict[str, dict[str, str]] = {}
    sources: list[str] = []
    for path in sorted(directory.glob("*.py")):
        relative = path.relative_to(source_root).as_posix()
        sources.append(relative)
        try:
            module = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeDecodeError) as error:
            raise ModelError(f"cannot parse OpenRazer D-Bus methods {relative}: {error}") from error
        for node in module.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            endpoints = [
                decorator
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "endpoint"
            ]
            if not endpoints:
                continue
            if len(endpoints) != 1:
                raise ModelError(f"OpenRazer D-Bus method {node.name} has multiple endpoints")
            endpoint = endpoints[0]
            if len(endpoint.args) < 2:
                raise ModelError(f"OpenRazer D-Bus method {node.name} has an incomplete endpoint")
            interface = _constant_string(endpoint.args[0])
            member = _constant_string(endpoint.args[1])
            keywords = {keyword.arg: keyword.value for keyword in endpoint.keywords if keyword.arg}
            in_signature = _constant_string(keywords.get("in_sig"))
            out_signature = _constant_string(keywords.get("out_sig"))
            if None in {interface, member, in_signature, out_signature} or not interface or not member:
                raise ModelError(f"OpenRazer D-Bus method {node.name} has dynamic metadata")
            _register_method(
                methods,
                _method_record(node.name, interface, member, in_signature, out_signature),
                relative,
            )
    return methods, sources


def _tuple_methods(
    path: Path, source_root: Path, wanted: set[str]
) -> dict[str, dict[str, str]]:
    relative = path.relative_to(source_root).as_posix()
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError, UnicodeDecodeError) as error:
        raise ModelError(f"cannot parse OpenRazer D-Bus surface {relative}: {error}") from error
    methods: dict[str, dict[str, str]] = {}
    for node in ast.walk(module):
        if not isinstance(node, ast.Tuple) or len(node.elts) != 5:
            continue
        interface = _constant_string(node.elts[0])
        member = _constant_string(node.elts[1])
        callback = _callback_id(node.elts[2])
        in_signature = _constant_string(node.elts[3])
        out_signature = _constant_string(node.elts[4])
        if (
            interface is None
            or member is None
            or callback is None
            or in_signature is None
            or out_signature is None
            or not interface.startswith("razer.")
        ):
            continue
        if callback not in wanted:
            continue
        _register_method(
            methods,
            _method_record(callback, interface, member, in_signature, out_signature),
            relative,
        )
    return methods


def _root_signals(path: Path, source_root: Path) -> dict[str, dict[str, str]]:
    relative = path.relative_to(source_root).as_posix()
    module = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    signals: dict[str, dict[str, str]] = {}
    for node in module.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for child in node.body:
            if not isinstance(child, ast.FunctionDef):
                continue
            for decorator in child.decorator_list:
                if not isinstance(decorator, ast.Call) or not decorator.args:
                    continue
                function = decorator.func
                if not (
                    isinstance(function, ast.Attribute)
                    and function.attr == "signal"
                    and isinstance(function.value, ast.Attribute)
                    and function.value.attr == "service"
                ):
                    continue
                interface = _constant_string(decorator.args[0])
                keywords = {keyword.arg: keyword.value for keyword in decorator.keywords if keyword.arg}
                signature = _constant_string(keywords.get("signature"))
                if not interface or signature is None:
                    raise ModelError(f"OpenRazer D-Bus signal {child.name} has dynamic metadata")
                signals[child.name] = {
                    "id": child.name,
                    "interface": interface,
                    "member": child.name,
                    "signature": signature,
                }
    return signals


def _select_methods(
    index: dict[str, dict[str, str]], method_ids: list[str], label: str
) -> list[dict[str, str]]:
    missing = sorted(set(method_ids) - set(index))
    if missing:
        raise ModelError(f"{label}: unresolved D-Bus methods: {', '.join(missing)}")
    return [deepcopy(index[method_id]) for method_id in method_ids]


def transformed_userspace_capability_catalog(root: Path, source_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    policy = load_userspace_capability_policy(root)
    metadata = load_imported_metadata(root)
    if _source_commit(source_root) != policy["source_commit"]:
        raise ModelError("OpenRazer userspace capability checkout does not match the immutable pin")
    if transformed_metadata(root, source_root) != metadata:
        raise ModelError("OpenRazer metadata must be refreshed before userspace capability import")

    decorated, dbus_sources = _decorated_methods(source_root)
    daemon_path = source_root / SOURCE_PATHS[0]
    base_path = source_root / SOURCE_PATHS[1]
    root_methods = _tuple_methods(
        daemon_path,
        source_root,
        {
            *policy["root"]["required_methods"],
            *policy["root"]["diagnostic_methods"],
        },
    )
    device_method_ids = {
        *policy["device"]["required_common"],
        *policy["device"]["semantic_probes"],
        *(method for methods in policy["device"]["required_by_kind"].values() for method in methods),
        *(method for feature in policy["features"] for method in feature["methods"]),
    }
    base_methods = _tuple_methods(base_path, source_root, device_method_ids)
    method_index = dict(decorated)
    for method_id, record in base_methods.items():
        _register_method(method_index, record, SOURCE_PATHS[1])
    signals = _root_signals(daemon_path, source_root)
    root_signal_ids = policy["root"]["required_signals"]
    missing_signals = sorted(set(root_signal_ids) - set(signals))
    if missing_signals:
        raise ModelError(f"OpenRazer root signals are missing: {', '.join(missing_signals)}")

    selection = load_import_selection(root)
    selected = {value["profile_id"]: value for value in selection["devices"]}
    classes = _ClassCatalog(source_root)
    devices: list[dict[str, Any]] = []
    for device in metadata["devices"]:
        profile_id = device["profile_id"]
        chosen = selected[profile_id]
        kind = device["identity"]["device_kind"]
        classes.resolve(chosen["class_name"], {"METHODS"})
        required_ids = sorted(
            {
                *policy["device"]["required_common"],
                *policy["device"]["required_by_kind"][kind],
            }
        )
        features: list[dict[str, Any]] = []
        for feature in sorted(policy["features"], key=lambda value: value["id"]):
            if kind not in feature["device_kinds"]:
                continue
            features.append(
                {
                    "id": feature["id"],
                    "requirement": feature["requirement"],
                    "methods": _select_methods(
                        method_index,
                        feature["methods"],
                        f"OpenRazer profile {profile_id} feature {feature['id']}",
                    ),
                }
            )
        identity = device["identity"]
        devices.append(
            {
                "profile_id": profile_id,
                "identity": {
                    key: identity[key]
                    for key in ("vendor_id", "product_id", "model_name", "device_kind")
                },
                "source": deepcopy(device["source"]),
                "required": _select_methods(
                    method_index, required_ids, f"OpenRazer profile {profile_id} required surface"
                ),
                "semantic_probes": _select_methods(
                    method_index,
                    policy["device"]["semantic_probes"],
                    f"OpenRazer profile {profile_id} semantic probes",
                ),
                "features": features,
            }
        )
    devices.sort(key=lambda value: value["profile_id"])
    upstream = upstream_index(root)["openrazer"]
    source_paths = sorted(
        {
            *SOURCE_PATHS,
            *dbus_sources,
            *(device["source"]["path"] for device in metadata["devices"]),
        }
    )
    return {
        "$schema": "../../schemas/openrazer-userspace-capability-catalog.schema.json",
        "schema": CATALOG_SCHEMA,
        "upstream": {
            key: upstream[key]
            for key in ("id", "repository", "version", "commit", "license_expression")
        },
        "policy_sha256": _digest({key: value for key, value in policy.items() if key != "$schema"}),
        "service": deepcopy(policy["service"]),
        "root": {
            "methods": _select_methods(
                root_methods,
                policy["root"]["required_methods"],
                "OpenRazer root required surface",
            ),
            "diagnostic_methods": _select_methods(
                root_methods,
                policy["root"]["diagnostic_methods"],
                "OpenRazer root diagnostic surface",
            ),
            "signals": [deepcopy(signals[signal_id]) for signal_id in root_signal_ids],
        },
        "devices": devices,
        "source_files": [
            {"path": path, "sha256": sha256_file(source_root / path)}
            for path in source_paths
        ],
    }


def load_userspace_capability_catalog(root: Path) -> dict[str, Any]:
    catalog = load_json(
        root / "integrations" / "openrazer" / "userspace-capability-catalog.json"
    )
    if catalog.get("$schema") != "../../schemas/openrazer-userspace-capability-catalog.schema.json":
        raise ModelError("OpenRazer userspace capability catalog schema reference is not canonical")
    if catalog.get("schema") != CATALOG_SCHEMA:
        raise ModelError("unsupported OpenRazer userspace capability catalog schema")
    policy = load_userspace_capability_policy(root)
    upstream = upstream_index(root)["openrazer"]
    expected_upstream = {
        key: upstream[key]
        for key in ("id", "repository", "version", "commit", "license_expression")
    }
    if catalog.get("upstream") != expected_upstream:
        raise ModelError("OpenRazer userspace capability catalog upstream binding drifted")
    if catalog.get("policy_sha256") != _digest(
        {key: value for key, value in policy.items() if key != "$schema"}
    ):
        raise ModelError("OpenRazer userspace capability catalog policy binding drifted")
    if catalog.get("service") != policy["service"]:
        raise ModelError("OpenRazer userspace capability catalog service identity drifted")
    if not isinstance(catalog.get("devices"), list) or not catalog["devices"]:
        raise ModelError("OpenRazer userspace capability catalog has no devices")
    profile_ids = [device.get("profile_id") for device in catalog["devices"]]
    if profile_ids != sorted(profile_ids):
        raise ModelError("OpenRazer userspace capability devices are not sorted")
    require_unique(profile_ids, "OpenRazer userspace capability profile id")
    for source in catalog.get("source_files", []):
        if (
            not isinstance(source, dict)
            or set(source) != {"path", "sha256"}
            or not isinstance(source["path"], str)
            or Path(source["path"]).is_absolute()
            or ".." in Path(source["path"]).parts
            or not isinstance(source["sha256"], str)
            or SHA256.fullmatch(source["sha256"]) is None
        ):
            raise ModelError("OpenRazer userspace capability source binding is invalid")
    return catalog


def write_userspace_capability_catalog(root: Path, source_root: Path) -> Path:
    destination = (
        root / "integrations" / "openrazer" / "userspace-capability-catalog.json"
    )
    destination.write_text(
        json.dumps(
            transformed_userspace_capability_catalog(root, source_root),
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination
