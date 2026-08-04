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
from .native_control import load_native_method_inventory
from .native_probe import c_function_source, load_native_probe_catalog, simple_c_defines
from .openrazer import _source_commit


POLICY_SCHEMA = "hyperflux-openrazer-native-command-policy-v1"
CATALOG_SCHEMA = "hyperflux-openrazer-native-command-catalog-v1"
REPORT_SIZE = 90
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,127}\Z")
FUNCTION_PATTERN = re.compile(r"razer_[a-z0-9_]+\Z")
FUNCTION_DEFINITION_PATTERN = re.compile(
    r"(?m)^(?:static\s+)?struct\s+razer_report\s+(razer_[a-z0-9_]+)\s*\("
)
FUNCTION_CALL_PATTERN = re.compile(r"\b(razer_[a-z0-9_]+)\s*\(")
REPORT_CONSTRUCTOR_PATTERN = re.compile(
    r"\bget_razer_report\s*\(\s*([^,()]+)\s*,\s*([^,()]+)\s*,\s*([^,()]+)\s*\)"
)
FIXED_ARGUMENT_PATTERN = re.compile(
    r"\breport\.arguments\[(\d+)\]\s*=\s*"
    r"([A-Z][A-Z0-9_]*|0[xX][0-9A-Fa-f]+|[0-9]+)\s*;"
)
SOURCE_PATHS = {
    "driver/razerchromacommon.c",
    "driver/razerkbd_driver.c",
    "driver/razermouse_driver.c",
}
POLICY_KEYS = {
    "$schema",
    "schema",
    "upstream_id",
    "source_commit",
    "report_size",
    "families",
}
FAMILY_KEYS = {"device_kind", "rules"}
CATALOG_DEVICE_KEYS = {"profile_id", "rules"}
RULE_KEYS = {
    "id",
    "methods",
    "command_class",
    "command_id",
    "data_size",
    "matcher",
    "sources",
}
SOURCE_KEYS = {"path", "functions"}
CATALOG_KEYS = {
    "$schema",
    "schema",
    "upstream",
    "policy_sha256",
    "inventory_sha256",
    "probe_catalog_sha256",
    "report_size",
    "devices",
}
CATALOG_RULE_KEYS = {
    "id",
    "methods",
    "direction",
    "command_class",
    "command_id",
    "data_size",
    "matcher",
    "sources",
}
CATALOG_SOURCE_KEYS = {"path", "function", "line", "sha256"}


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extra:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extra)}")


def _bounded_int(value: Any, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _validate_fields(fields: Any, data_size: int, label: str) -> None:
    if not isinstance(fields, list) or not fields or len(fields) > 80:
        raise ModelError(f"{label}: fields must be a bounded non-empty list")
    covered = [False] * data_size
    previous_offset = -1
    for index, field in enumerate(fields):
        field_label = f"{label} field {index}"
        if not isinstance(field, dict):
            raise ModelError(f"{field_label}: expected an object")
        kind = field.get("kind")
        offset = field.get("offset")
        if not _bounded_int(offset, 0, max(data_size - 1, 0)):
            raise ModelError(f"{field_label}: invalid offset")
        if offset <= previous_offset:
            raise ModelError(f"{label}: fields must be ordered by offset")
        previous_offset = offset
        if kind == "exact":
            _exact_keys(field, {"kind", "offset", "bytes"}, field_label)
            values = field["bytes"]
            if (
                not isinstance(values, list)
                or not values
                or any(not _bounded_int(value, 0, 255) for value in values)
            ):
                raise ModelError(f"{field_label}: invalid exact bytes")
            width = len(values)
        elif kind == "one-of":
            _exact_keys(field, {"kind", "offset", "values"}, field_label)
            values = field["values"]
            if (
                not isinstance(values, list)
                or not values
                or values != sorted(values)
                or any(not _bounded_int(value, 0, 255) for value in values)
            ):
                raise ModelError(f"{field_label}: invalid allowed values")
            require_unique(values, f"{field_label} allowed value")
            width = 1
        elif kind == "u8-range":
            _exact_keys(
                field,
                {"kind", "offset", "minimum", "maximum"},
                field_label,
            )
            if (
                not _bounded_int(field["minimum"], 0, 255)
                or not _bounded_int(field["maximum"], 0, 255)
                or field["minimum"] > field["maximum"]
            ):
                raise ModelError(f"{field_label}: invalid byte range")
            width = 1
        elif kind == "be16-range":
            _exact_keys(
                field,
                {"kind", "offset", "minimum", "maximum"},
                field_label,
            )
            if (
                not _bounded_int(field["minimum"], 0, 65535)
                or not _bounded_int(field["maximum"], 0, 65535)
                or field["minimum"] > field["maximum"]
            ):
                raise ModelError(f"{field_label}: invalid big-endian range")
            width = 2
        elif kind == "rgb":
            _exact_keys(field, {"kind", "offset", "pixels"}, field_label)
            if not _bounded_int(field["pixels"], 1, 26):
                raise ModelError(f"{field_label}: invalid RGB pixel count")
            width = field["pixels"] * 3
        else:
            raise ModelError(f"{field_label}: unsupported constraint kind")
        if offset + width > data_size:
            raise ModelError(f"{field_label}: constraint exceeds the data payload")
        for covered_index in range(offset, offset + width):
            if covered[covered_index]:
                raise ModelError(f"{field_label}: constraint overlaps another field")
            covered[covered_index] = True
    if not all(covered):
        raise ModelError(f"{label}: every data byte must have an explicit policy")


def _validate_matcher(matcher: Any, data_size: int, label: str) -> None:
    if not isinstance(matcher, dict):
        raise ModelError(f"{label}: matcher must be an object")
    kind = matcher.get("kind")
    if kind == "fields":
        _exact_keys(matcher, {"kind", "fields"}, label)
        _validate_fields(matcher["fields"], data_size, label)
        return
    if kind == "custom-frame":
        _exact_keys(matcher, {"kind", "rows", "columns"}, label)
        if (
            data_size != 71
            or not _bounded_int(matcher["rows"], 1, 32)
            or not _bounded_int(matcher["columns"], 1, 25)
        ):
            raise ModelError(f"{label}: invalid custom-frame geometry")
        return
    if kind == "dpi-stages":
        _exact_keys(
            matcher,
            {"kind", "maximum_stages", "minimum_dpi", "maximum_dpi"},
            label,
        )
        if (
            data_size != 38
            or not _bounded_int(matcher["maximum_stages"], 1, 5)
            or not _bounded_int(matcher["minimum_dpi"], 1, 65535)
            or not _bounded_int(matcher["maximum_dpi"], 1, 65535)
            or matcher["minimum_dpi"] > matcher["maximum_dpi"]
        ):
            raise ModelError(f"{label}: invalid DPI-stage policy")
        return
    raise ModelError(f"{label}: unsupported matcher kind")


def _feature_write_methods(device: dict[str, Any]) -> set[str]:
    return {
        method["id"]
        for method in device["methods"]
        if method["transport"] == "feature-report"
        and method["classification"] != "read-only"
    }


def load_native_command_policy(root: Path) -> dict[str, Any]:
    policy = load_json(root / "integrations" / "openrazer" / "native-command-policy.json")
    _exact_keys(policy, POLICY_KEYS, "OpenRazer native command policy")
    if policy["$schema"] != "../../schemas/openrazer-native-command-policy.schema.json":
        raise ModelError("OpenRazer native command policy schema reference is not canonical")
    if policy["schema"] != POLICY_SCHEMA:
        raise ModelError("unsupported OpenRazer native command policy schema")
    upstream = upstream_index(root).get(policy["upstream_id"])
    if upstream is None or upstream["id"] != "openrazer":
        raise ModelError("OpenRazer native command policy names an unknown upstream")
    if policy["source_commit"] != upstream["commit"] or policy["report_size"] != REPORT_SIZE:
        raise ModelError("OpenRazer native command policy differs from the pinned transport")

    families = policy["families"]
    if not isinstance(families, list) or not families or len(families) > 8:
        raise ModelError("OpenRazer native command policy families must be bounded")
    device_kinds: list[str] = []
    for family_index, family in enumerate(families):
        label = f"OpenRazer native command family {family_index}"
        if not isinstance(family, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(family, FAMILY_KEYS, label)
        device_kind = family["device_kind"]
        if device_kind not in {"keyboard", "mouse"}:
            raise ModelError(f"{label}: unsupported device kind")
        rules = family["rules"]
        if not isinstance(rules, list) or not rules or len(rules) > 128:
            raise ModelError(f"{label}: rules must be bounded")
        rule_ids: list[str] = []
        for rule_index, rule in enumerate(rules):
            rule_label = f"{label} rule {rule_index}"
            if not isinstance(rule, dict):
                raise ModelError(f"{rule_label}: expected an object")
            _exact_keys(rule, RULE_KEYS, rule_label)
            if (
                not isinstance(rule["id"], str)
                or IDENTIFIER_PATTERN.fullmatch(rule["id"].replace("-", "_")) is None
                or not _bounded_int(rule["command_class"], 0, 255)
                or not _bounded_int(rule["command_id"], 0, 127)
                or not _bounded_int(rule["data_size"], 1, 80)
            ):
                raise ModelError(f"{rule_label}: invalid command signature")
            methods = rule["methods"]
            if (
                not isinstance(methods, list)
                or not methods
                or methods != sorted(methods)
                or any(not isinstance(method, str) for method in methods)
            ):
                raise ModelError(f"{rule_label}: invalid method list")
            require_unique(methods, f"{rule_label} method")
            _validate_matcher(rule["matcher"], rule["data_size"], f"{rule_label} matcher")
            sources = rule["sources"]
            if not isinstance(sources, list) or not sources or len(sources) > 8:
                raise ModelError(f"{rule_label}: invalid source bindings")
            source_paths: list[str] = []
            driver_paths: set[str] = set()
            has_constructor = False
            for source_index, source in enumerate(sources):
                source_label = f"{rule_label} source {source_index}"
                if not isinstance(source, dict):
                    raise ModelError(f"{source_label}: expected an object")
                _exact_keys(source, SOURCE_KEYS, source_label)
                path = source["path"]
                functions = source["functions"]
                if (
                    path not in SOURCE_PATHS
                    or not isinstance(functions, list)
                    or not functions
                    or functions != sorted(functions)
                    or any(
                        not isinstance(function, str)
                        or FUNCTION_PATTERN.fullmatch(function) is None
                        for function in functions
                    )
                ):
                    raise ModelError(f"{source_label}: invalid source binding")
                require_unique(functions, f"{source_label} function")
                has_constructor |= path.endswith("razerchromacommon.c")
                if path.endswith("_driver.c"):
                    driver_paths.add(path)
                source_paths.append(path)
            expected_driver = f"driver/razer{'kbd' if device_kind == 'keyboard' else 'mouse'}_driver.c"
            if (
                source_paths != sorted(source_paths)
                or not has_constructor
                or driver_paths != {expected_driver}
            ):
                raise ModelError(f"{rule_label}: source bindings must include constructor and driver")
            rule_ids.append(rule["id"])
        require_unique(rule_ids, f"{label} rule id")
        if rule_ids != sorted(rule_ids):
            raise ModelError(f"{label}: rules must be sorted")
        device_kinds.append(device_kind)
    require_unique(device_kinds, "OpenRazer native command family")
    if set(device_kinds) != {"keyboard", "mouse"}:
        raise ModelError("OpenRazer native command policy must cover mouse and keyboard")
    return policy


def _bound_functions(
    source: str,
    functions: list[str],
    *,
    include_transitive_calls: bool,
) -> list[str]:
    selected = set(functions)
    if not include_transitive_calls:
        return sorted(selected)
    definitions = set(FUNCTION_DEFINITION_PATTERN.findall(source))
    pending = list(functions)
    while pending:
        function = pending.pop()
        body, _ = c_function_source(source, function)
        for called in FUNCTION_CALL_PATTERN.findall(body):
            if called in definitions and called not in selected:
                selected.add(called)
                pending.append(called)
    return sorted(selected)


def _simple_c_value(token: str, constants: dict[str, int]) -> int | None:
    token = token.strip()
    if re.fullmatch(r"0[xX][0-9A-Fa-f]+|[0-9]+", token):
        return int(token, 0)
    return constants.get(token)


def _field_allows_fixed_byte(field: dict[str, Any], offset: int, value: int) -> bool | None:
    start = field["offset"]
    kind = field["kind"]
    if kind == "exact":
        width = len(field["bytes"])
    elif kind in {"one-of", "u8-range"}:
        width = 1
    elif kind == "be16-range":
        width = 2
    elif kind == "rgb":
        width = field["pixels"] * 3
    else:
        raise ModelError(f"unsupported fixed-byte matcher kind: {kind}")
    if not start <= offset < start + width:
        return None
    if kind == "exact":
        return field["bytes"][offset - start] == value
    if kind == "one-of":
        return value in field["values"]
    if kind == "u8-range":
        return field["minimum"] <= value <= field["maximum"]
    # One source-fixed byte cannot determine a combined be16 or RGB value.
    return True


def _verify_rule_source_semantics(
    source_root: Path,
    rule: dict[str, Any],
    label: str,
) -> None:
    chroma_path = source_root / "driver" / "razerchromacommon.c"
    chroma_source = chroma_path.read_text(encoding="utf-8")
    constants = simple_c_defines(
        (source_root / "driver" / "razercommon.h").read_text(encoding="utf-8")
    )
    constants.update(simple_c_defines(chroma_source))
    matcher = rule["matcher"]

    for source in rule["sources"]:
        if source["path"] != "driver/razerchromacommon.c":
            continue
        functions = _bound_functions(
            chroma_source,
            source["functions"],
            include_transitive_calls=True,
        )
        for function in functions:
            body, _ = c_function_source(chroma_source, function)
            for call in REPORT_CONSTRUCTOR_PATTERN.finditer(body):
                actual = tuple(
                    _simple_c_value(token, constants) for token in call.groups()
                )
                for field_name, actual_value, expected_value in zip(
                    ("command class", "command id"),
                    actual[:2],
                    (rule["command_class"], rule["command_id"]),
                    strict=True,
                ):
                    if actual_value is not None and actual_value != expected_value:
                        raise ModelError(
                            f"{label}: {function} source {field_name} "
                            f"{actual_value:#04x} differs from policy {expected_value:#04x}"
                        )
                actual_size = actual[2]
                allowed_sizes = rule.get("data_sizes", [rule.get("data_size")])
                if actual_size is not None and actual_size not in allowed_sizes:
                    raise ModelError(
                        f"{label}: {function} source data size {actual_size:#04x} "
                        "differs from the policy"
                    )
            if matcher["kind"] != "fields":
                continue
            data_sizes = rule.get("data_sizes", [rule.get("data_size")])
            if len(data_sizes) != 1 or data_sizes[0] is None:
                raise ModelError(f"{label}: fields matcher needs one payload size")
            data_size = data_sizes[0]
            for assignment in FIXED_ARGUMENT_PATTERN.finditer(body):
                offset = int(assignment.group(1))
                token = assignment.group(2)
                value = _simple_c_value(token, constants)
                if value is None:
                    raise ModelError(
                        f"{label}: {function} uses unresolved source constant {token}"
                    )
                if offset >= data_size:
                    raise ModelError(
                        f"{label}: {function} writes source-fixed argument {offset} "
                        "outside the policy payload"
                    )
                allowed = next(
                    (
                        result
                        for field in matcher["fields"]
                        if (result := _field_allows_fixed_byte(field, offset, value))
                        is not None
                    ),
                    None,
                )
                if allowed is not True:
                    raise ModelError(
                        f"{label}: {function} source-fixed argument "
                        f"{offset}={value:#04x} is rejected by the policy matcher"
                    )


def _source_bindings(source_root: Path, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source in sources:
        path = source["path"]
        text = (source_root / path).read_text(encoding="utf-8")
        functions = _bound_functions(
            text,
            source["functions"],
            include_transitive_calls=path == "driver/razerchromacommon.c",
        )
        for function in functions:
            body, line = c_function_source(text, function)
            records.append(
                {
                    "path": path,
                    "function": function,
                    "line": line,
                    "sha256": hashlib.sha256(body.encode()).hexdigest(),
                }
            )
    return records


def transformed_native_command_catalog(root: Path, source_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    policy = load_native_command_policy(root)
    inventory = load_native_method_inventory(root)
    probes = load_native_probe_catalog(root)
    if _source_commit(source_root) != policy["source_commit"]:
        raise ModelError("OpenRazer native command checkout does not match the immutable pin")

    policy_by_kind = {
        family["device_kind"]: family for family in policy["families"]
    }
    probe_by_profile = {device["profile_id"]: device for device in probes["devices"]}
    devices: list[dict[str, Any]] = []
    for inventory_device in inventory["devices"]:
        profile_id = inventory_device["profile_id"]
        device_kind = inventory_device["wireless_identity"]["device_kind"]
        expected_write_methods = _feature_write_methods(inventory_device)
        covered_write_methods: set[str] = set()
        rules: list[dict[str, Any]] = []
        for probe in probe_by_profile[profile_id]["probes"]:
            request = probe["request"]
            rules.append(
                {
                    "id": f"read-{probe['method'].replace('_', '-')}",
                    "methods": [probe["method"]],
                    "direction": "read",
                    "command_class": request["command_class"],
                    "command_id": request["command_id"],
                    "data_size": request["data_size"],
                    "matcher": {
                        "kind": "fields",
                        "fields": [
                            {
                                "kind": "exact",
                                "offset": 0,
                                "bytes": deepcopy(request["arguments"]),
                            }
                        ],
                    },
                    "sources": [
                        {
                            "path": probe["driver"]["source_path"],
                            "function": probe["driver"]["request_owner"],
                            "line": probe["driver"]["source_line"],
                            "sha256": probe["driver"]["source_sha256"],
                        }
                    ],
                }
            )
        for policy_rule in policy_by_kind[device_kind]["rules"]:
            methods = sorted(set(policy_rule["methods"]) & expected_write_methods)
            if not methods:
                continue
            _verify_rule_source_semantics(
                source_root,
                policy_rule,
                f"OpenRazer native command {profile_id}/{policy_rule['id']}",
            )
            rules.append(
                {
                    "id": "write-" + policy_rule["id"],
                    "methods": methods,
                    "direction": "write",
                    "command_class": policy_rule["command_class"],
                    "command_id": policy_rule["command_id"],
                    "data_size": policy_rule["data_size"],
                    "matcher": deepcopy(policy_rule["matcher"]),
                    "sources": _source_bindings(source_root, policy_rule["sources"]),
                }
            )
            covered_write_methods.update(methods)
        if covered_write_methods != expected_write_methods:
            missing = sorted(expected_write_methods - covered_write_methods)
            raise ModelError(
                f"OpenRazer native command {profile_id}: family policy misses "
                + ", ".join(missing)
            )
        rules.sort(key=lambda rule: rule["id"])
        devices.append({"profile_id": profile_id, "rules": rules})

    upstream = upstream_index(root)["openrazer"]
    return {
        "$schema": "../../schemas/openrazer-native-command-catalog.schema.json",
        "schema": CATALOG_SCHEMA,
        "upstream": {
            key: upstream[key]
            for key in ("id", "repository", "version", "commit", "license_expression")
        },
        "policy_sha256": sha256_file(
            root / "integrations" / "openrazer" / "native-command-policy.json"
        ),
        "inventory_sha256": sha256_file(
            root / "integrations" / "openrazer" / "native-method-inventory.json"
        ),
        "probe_catalog_sha256": sha256_file(
            root / "integrations" / "openrazer" / "native-probe-catalog.json"
        ),
        "report_size": REPORT_SIZE,
        "devices": devices,
    }


def load_native_command_catalog(root: Path) -> dict[str, Any]:
    path = root / "integrations" / "openrazer" / "native-command-catalog.json"
    catalog = load_json(path)
    _exact_keys(catalog, CATALOG_KEYS, "OpenRazer native command catalog")
    if catalog["$schema"] != "../../schemas/openrazer-native-command-catalog.schema.json":
        raise ModelError("OpenRazer native command catalog schema reference is not canonical")
    if catalog["schema"] != CATALOG_SCHEMA or catalog["report_size"] != REPORT_SIZE:
        raise ModelError("unsupported OpenRazer native command catalog schema")
    upstream = upstream_index(root)["openrazer"]
    expected_upstream = {
        key: upstream[key]
        for key in ("id", "repository", "version", "commit", "license_expression")
    }
    if catalog["upstream"] != expected_upstream:
        raise ModelError("OpenRazer native command catalog upstream binding drifted")
    for field, source_path in (
        ("policy_sha256", "native-command-policy.json"),
        ("inventory_sha256", "native-method-inventory.json"),
        ("probe_catalog_sha256", "native-probe-catalog.json"),
    ):
        expected = sha256_file(root / "integrations" / "openrazer" / source_path)
        if catalog[field] != expected:
            raise ModelError(f"OpenRazer native command catalog {field} is stale")

    load_native_command_policy(root)
    inventory = load_native_method_inventory(root)
    probes = load_native_probe_catalog(root)
    expected_profiles = sorted(device["profile_id"] for device in inventory["devices"])
    expected_read = {
        device["profile_id"]: {probe["method"] for probe in device["probes"]}
        for device in probes["devices"]
    }
    profile_ids: list[str] = []
    for device_index, device in enumerate(catalog["devices"]):
        label = f"OpenRazer native command catalog device {device_index}"
        if not isinstance(device, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(device, CATALOG_DEVICE_KEYS, label)
        profile_id = device["profile_id"]
        rules = device["rules"]
        if not isinstance(rules, list) or not rules or len(rules) > 256:
            raise ModelError(f"{label}: invalid rules")
        ids: list[str] = []
        read_methods: set[str] = set()
        for rule_index, rule in enumerate(rules):
            rule_label = f"{label} rule {rule_index}"
            if not isinstance(rule, dict):
                raise ModelError(f"{rule_label}: expected an object")
            _exact_keys(rule, CATALOG_RULE_KEYS, rule_label)
            direction = rule["direction"]
            command_id = rule["command_id"]
            if (
                direction not in {"read", "write"}
                or not _bounded_int(command_id, 0, 255)
                or (command_id >= 0x80) != (direction == "read")
                or not _bounded_int(rule["command_class"], 0, 255)
                or not _bounded_int(rule["data_size"], 1, 80)
            ):
                raise ModelError(f"{rule_label}: invalid command direction")
            _validate_matcher(rule["matcher"], rule["data_size"], f"{rule_label} matcher")
            sources = rule["sources"]
            if not isinstance(sources, list) or not sources or len(sources) > 32:
                raise ModelError(f"{rule_label}: invalid source records")
            for source_index, source in enumerate(sources):
                source_label = f"{rule_label} source {source_index}"
                if not isinstance(source, dict):
                    raise ModelError(f"{source_label}: expected an object")
                _exact_keys(source, CATALOG_SOURCE_KEYS, source_label)
                if (
                    source["path"] not in SOURCE_PATHS
                    or FUNCTION_PATTERN.fullmatch(source["function"]) is None
                    or not _bounded_int(source["line"], 1, 2**31 - 1)
                    or SHA256_PATTERN.fullmatch(source["sha256"]) is None
                ):
                    raise ModelError(f"{source_label}: invalid source record")
            if direction == "read":
                read_methods.update(rule["methods"])
            ids.append(rule["id"])
        require_unique(ids, f"{label} rule id")
        if ids != sorted(ids):
            raise ModelError(f"{label}: rules must be sorted")
        if read_methods != expected_read.get(profile_id):
            raise ModelError(f"{label}: read policy coverage drifted")
        profile_ids.append(profile_id)
    require_unique(profile_ids, "OpenRazer native command catalog profile")
    if profile_ids != expected_profiles:
        raise ModelError("OpenRazer native command catalog profile coverage drifted")
    return catalog


def write_native_command_catalog(root: Path, source_root: Path) -> Path:
    destination = root / "integrations" / "openrazer" / "native-command-catalog.json"
    destination.write_text(
        json.dumps(
            transformed_native_command_catalog(root, source_root),
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination
