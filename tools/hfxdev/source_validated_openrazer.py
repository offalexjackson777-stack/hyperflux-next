# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
from typing import Any

from .model import ModelError, sha256_file
from .native_command import (
    _verify_rule_source_semantics,
    load_native_command_policy,
)
from .native_control import (
    _direct_usb_reference,
    _source_bound_additional_methods,
    load_native_control_policy,
)
from .native_probe import c_function_source
from .openrazer import (
    _ClassCatalog,
    effective_event_regex,
    supported_poll_rates_contract,
)
from .openrazer_constructor import (
    ConstructorNotApplicable,
    UnmodeledWriteRequest,
    constructor_witness_matches_rule,
    constructor_source_path,
    source_bound_cached_read,
    source_bound_read_witnesses,
    source_bound_write_witnesses,
    verify_write_request_closure,
)
from .source_validated_common import (
    BACKEND_REQUIRED,
    REPORT_BYTES,
    SOURCE_VALIDATED,
    SUPPORTED_KINDS,
    _classification,
)


def _openrazer_constructor_rules(root: Path) -> dict[str, list[dict[str, Any]]]:
    policy = load_native_command_policy(root)
    result = {
        family["device_kind"]: deepcopy(family["rules"])
        for family in policy["families"]
    }
    if set(result) != SUPPORTED_KINDS:
        raise ModelError("OpenRazer constructor rules do not cover mouse and keyboard")
    for kind, rules in result.items():
        rule_ids = [rule["id"] for rule in rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ModelError(f"OpenRazer {kind} constructor rule id is duplicated")
        rules.sort(key=lambda rule: rule["id"])
    return result


def _narrow_led_selector(
    rule: dict[str, Any], witnesses: list[Any], kind: str
) -> None:
    if kind != "mouse" or not rule["id"].startswith("led-"):
        return
    fields = rule["matcher"].get("fields", [])
    selector = next(
        (field for field in fields if field.get("kind") == "one-of" and field.get("offset") == 1),
        None,
    )
    if selector is None:
        raise ModelError(f"OpenRazer rule {rule['id']} has no LED selector")
    values = sorted({witness.arguments[1] for witness in witnesses})
    if not values or not set(values) <= set(selector["values"]):
        raise ModelError(f"OpenRazer rule {rule['id']} has an unproven LED selector")
    selector["values"] = values


def _narrow_keyboard_led_storage(
    rule: dict[str, Any], witnesses: list[Any], kind: str
) -> None:
    if kind != "keyboard" or rule["id"] not in {
        "game-mode",
        "macro-effect",
        "macro-mode",
    }:
        return
    selector = next(
        (
            field
            for field in rule["matcher"].get("fields", [])
            if field.get("kind") == "one-of" and field.get("offset") == 0
        ),
        None,
    )
    values = sorted({witness.arguments[0] for witness in witnesses})
    if selector is None or not values or not set(values) <= {0, 1}:
        raise ModelError(f"OpenRazer rule {rule['id']} has no proven storage selector")
    selector["values"] = values


def _parameterize_custom_frame(
    rule: dict[str, Any],
    witnesses: list[Any],
    dimensions: list[int],
    label: str,
) -> None:
    rows, columns = dimensions
    modes: set[str] = set()
    for witness in witnesses:
        arguments = bytes(witness.arguments)
        if len(arguments) < 8 or arguments[0:2] != b"\x00\x00":
            raise ModelError(f"{label}: custom-frame constructor is malformed")
        row, start, stop = arguments[2], arguments[3], arguments[4]
        if row >= rows or start > stop or stop >= columns:
            raise ModelError(f"{label}: custom-frame constructor exceeds exact geometry")
        used = 5 + (stop - start + 1) * 3
        if used == len(arguments):
            modes.add("exact")
        elif len(arguments) == 71 and used < len(arguments) and not any(arguments[used:]):
            modes.add("zero-padded")
        else:
            raise ModelError(f"{label}: custom-frame constructor has unknown framing")
    if len(modes) != 1:
        raise ModelError(f"{label}: custom-frame constructors disagree on framing")
    matcher = rule["matcher"]
    matcher["rows"], matcher["columns"] = dimensions
    matcher["length_mode"] = next(iter(modes))
    if matcher["length_mode"] == "exact":
        rule.pop("data_size", None)
        rule["data_sizes"] = [5 + pixels * 3 for pixels in range(1, columns + 1)]
    else:
        rule.pop("data_sizes", None)
        rule["data_size"] = 71


def _parameterize_rule(
    rule: dict[str, Any],
    witnesses: list[Any],
    record: dict[str, Any],
) -> None:
    matcher = rule["matcher"]
    if matcher["kind"] == "custom-frame":
        dimensions = record["facts"].get("matrix_dimensions")
        if (
            not isinstance(dimensions, list)
            or len(dimensions) != 2
            or not all(isinstance(value, int) for value in dimensions)
        ):
            raise ModelError(f"{record['record_id']}: custom frame lacks exact geometry")
        _parameterize_custom_frame(rule, witnesses, dimensions, record["record_id"])
    elif matcher["kind"] == "dpi-stages":
        matcher["maximum_dpi"] = record["facts"].get("dpi_max", 0)
    elif rule["id"] == "dpi":
        maximum = record["facts"].get("dpi_max", 0)
        for field in matcher["fields"]:
            if field["kind"] == "be16-range":
                field["maximum"] = maximum
    _narrow_led_selector(rule, witnesses, record["device_kind"])
    _narrow_keyboard_led_storage(rule, witnesses, record["device_kind"])


def _parameterize_openrazer_rules(
    root: Path,
    constructor_rules: dict[str, list[dict[str, Any]]],
    record: dict[str, Any],
    methods: set[str],
    source_root: Path,
) -> tuple[list[dict[str, Any]], set[str]]:
    kind = record["device_kind"]
    rules: list[dict[str, Any]] = []
    covered: set[str] = set()
    for template in constructor_rules[kind]:
        selected = sorted(set(template["methods"]) & methods)
        if not selected:
            continue
        proven: list[str] = []
        witnesses = []
        for method in selected:
            candidate = deepcopy(template)
            candidate["methods"] = [method]
            try:
                method_witnesses = source_bound_write_witnesses(
                    root,
                    source_root,
                    record,
                    candidate,
                    validate_rule=False,
                )
            except ConstructorNotApplicable:
                continue
            _parameterize_rule(candidate, method_witnesses, record)
            if not all(
                constructor_witness_matches_rule(witness, candidate)
                for witness in method_witnesses
            ):
                raise ModelError(
                    f"{record['record_id']}/{method}: compiled upstream constructor is rejected by the generated matcher"
                )
            proven.append(method)
            witnesses.extend(method_witnesses)
        if not proven:
            continue

        rule = deepcopy(template)
        rule["methods"] = proven
        _parameterize_rule(rule, witnesses, record)
        matcher = rule["matcher"]
        _verify_rule_source_semantics(
            source_root, rule, f"source-validated {record['record_id']}/{rule['id']}"
        )
        driver_path = f"driver/razer{'kbd' if kind == 'keyboard' else 'mouse'}_driver.c"
        paths_and_functions = {
            (driver_path, function)
            for witness in witnesses
            for function in witness.source_functions
        }
        paths_and_functions.update(
            {
                (constructor_source_path(function), function)
                for witness in witnesses
                for function in witness.constructor_functions
            }
        )
        source_bindings = []
        for relative, function in sorted(paths_and_functions):
            body, line = c_function_source(
                (source_root / relative).read_text(encoding="utf-8"), function
            )
            source_bindings.append(
                {
                    "path": relative,
                    "function": function,
                    "line": line,
                    "sha256": hashlib.sha256(body.encode()).hexdigest(),
                }
            )
        source_bindings.sort(key=lambda value: (value["path"], value["function"]))
        generated_rule = {
            "id": "openrazer-write-" + rule["id"],
            "integrations": ["openrazer"],
            "methods": proven,
            "direction": "write",
            "command_class": rule["command_class"],
            "command_id": rule["command_id"],
            "matcher": matcher,
            "sources": [
                {"upstream": "openrazer", **source}
                for source in source_bindings
            ],
        }
        if "data_sizes" in rule:
            generated_rule["data_sizes"] = rule["data_sizes"]
        else:
            generated_rule["data_size"] = rule["data_size"]
        rules.append(generated_rule)
        covered.update(proven)
    if covered == methods:
        verify_write_request_closure(root, source_root, record, methods, rules)
    return rules, covered


def _read_rules(
    root: Path,
    source_root: Path,
    record: dict[str, Any],
    methods: set[str],
    integrations: tuple[str, ...] = ("openrazer",),
    driver_attribute_overrides: dict[str, tuple[str, ...]] | None = None,
    include_transaction_ids: bool = False,
) -> tuple[list[dict[str, Any]], set[str]]:
    rules: list[dict[str, Any]] = []
    covered: set[str] = set()
    for method in sorted(methods):
        driver_attributes = (
            None
            if driver_attribute_overrides is None
            else driver_attribute_overrides.get(method)
        )
        try:
            witnesses = source_bound_read_witnesses(
                root,
                source_root,
                record,
                method,
                driver_attributes,
            )
        except ConstructorNotApplicable:
            if driver_attributes is None and source_bound_cached_read(source_root, method):
                covered.add(method)
            continue
        driver_path = (
            "driver/razerkbd_driver.c"
            if record["device_kind"] == "keyboard"
            else "driver/razermouse_driver.c"
        )
        grouped: dict[tuple[int, int, int, tuple[int, ...]], list[Any]] = {}
        for witness in witnesses:
            key = (
                witness.command_class,
                witness.command_id,
                witness.data_size,
                witness.arguments,
            )
            grouped.setdefault(key, []).append(witness)
        for index, (request, request_witnesses) in enumerate(sorted(grouped.items())):
            command_class, command_id, data_size, arguments = request
            paths_and_functions = {
                (driver_path, function)
                for witness in request_witnesses
                for function in witness.source_functions
            }
            paths_and_functions.update(
                {
                    (constructor_source_path(function), function)
                    for witness in request_witnesses
                    for function in witness.constructor_functions
                }
            )
            sources = []
            for relative, function in sorted(paths_and_functions):
                body, line = c_function_source(
                    (source_root / relative).read_text(encoding="utf-8"), function
                )
                sources.append(
                    {
                        "upstream": "openrazer",
                        "path": relative,
                        "function": function,
                        "line": line,
                        "sha256": hashlib.sha256(body.encode()).hexdigest(),
                    }
                )
            suffix = "" if len(grouped) == 1 else f"-{index + 1:02d}"
            generated_rule = {
                    "id": "openrazer-read-" + method.replace("_", "-") + suffix,
                    "integrations": list(integrations),
                    "methods": [method],
                    "direction": "read",
                    "command_class": command_class,
                    "command_id": command_id,
                    "data_size": data_size,
                    "matcher": {
                        "kind": "fields",
                        "fields": [
                            {
                                "kind": "exact",
                                "offset": 0,
                                "bytes": list(arguments),
                            }
                        ],
                    },
                    "sources": sources,
                }
            if include_transaction_ids:
                generated_rule["transaction_ids"] = sorted(
                    {
                        transaction
                        for witness in request_witnesses
                        for transaction in witness.transaction_ids
                    }
                )
            rules.append(generated_rule)
        covered.add(method)
    return rules, covered


def _publication_identity(rules: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        rule
        for rule in rules
        if rule["methods"] == ["get_serial"] and rule["direction"] == "read"
    ]
    if len(matches) != 1:
        raise ModelError("OpenRazer source does not produce one exact publication identity")
    rule = matches[0]
    fields = rule["matcher"].get("fields", [])
    transactions = rule.get("transaction_ids", [])
    if (
        len(fields) != 1
        or fields[0].get("kind") != "exact"
        or fields[0].get("offset") != 0
        or len(fields[0].get("bytes", [])) != rule["data_size"]
        or len(transactions) != 1
    ):
        raise ModelError(
            "OpenRazer publication identity is not one exact source-bound request"
        )
    arguments = fields[0]["bytes"]
    transaction_id = transactions[0]
    normalized = bytearray(REPORT_BYTES)
    normalized[1] = transaction_id
    normalized[5] = rule["data_size"]
    normalized[6] = rule["command_class"]
    normalized[7] = rule["command_id"]
    normalized[8 : 8 + len(arguments)] = bytes(arguments)
    return {
        "method": "get_serial",
        "transaction_id": transaction_id,
        "command_class": rule["command_class"],
        "command_id": rule["command_id"],
        "data_size": rule["data_size"],
        "arguments": arguments,
        "normalized_sha256": hashlib.sha256(normalized).hexdigest(),
    }


def _openrazer_integration(
    root: Path,
    source_root: Path,
    catalog: _ClassCatalog,
    record: dict[str, Any],
    constructor_rules: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    policy = load_native_control_policy(root)
    class_name = record["source_device_key"]
    parsed = catalog.classes.get(class_name)
    if parsed is None:
        return (
            {
                "state": BACKEND_REQUIRED,
                "reason": "source-class-absent",
                "record_id": record["record_id"],
            },
            [],
        )
    identity = record["usb_identity"]
    try:
        source_vendor_id = catalog.field(class_name, "USB_VID")
        source_product_id = catalog.field(class_name, "USB_PID")
    except (KeyError, ModelError):
        return (
            {
                "state": BACKEND_REQUIRED,
                "reason": "source-identity-unavailable",
                "record_id": record["record_id"],
            },
            [],
        )
    if (
        isinstance(source_vendor_id, bool)
        or not isinstance(source_vendor_id, int)
        or isinstance(source_product_id, bool)
        or not isinstance(source_product_id, int)
        or source_vendor_id != identity["vendor_id"]
        or source_product_id != identity["product_id"]
        or parsed.device_kind != record["device_kind"]
    ):
        return (
            {
                "state": BACKEND_REQUIRED,
                "reason": "source-identity-mismatch",
                "record_id": record["record_id"],
            },
            [],
        )
    try:
        direct: dict[str, Any] | None = _direct_usb_reference(
            catalog,
            class_name,
            {
                "vendor_id": identity["vendor_id"],
                "product_id": identity["product_id"],
                "device_kind": record["device_kind"],
            },
        )
    except ModelError:
        direct = None
    source_path = parsed.source_file
    try:
        image_url = catalog.field(class_name, "DEVICE_IMAGE")
        event_regex = effective_event_regex(source_root / source_path, class_name)
        if event_regex is not None:
            re.compile(event_regex)
    except (KeyError, ModelError, re.error):
        return (
            {
                "state": BACKEND_REQUIRED,
                "reason": "native-route-presentation-unavailable",
                "record_id": record["record_id"],
            },
            [],
        )
    methods = set(record["settings_methods"])
    methods.update(policy["common_methods"])
    methods.update(policy["kind_methods"][record["device_kind"]])
    methods.update(_source_bound_additional_methods(source_root, class_name))
    # The pinned daemon advertises these methods based on the model class, but
    # their behavior also requires a route-associated evdev source. A global
    # filename regex is not sufficient authority, especially with two
    # same-model receivers, so retain the shared wire commands without claiming
    # the higher-level method until that source is independently proven.
    methods.difference_update(policy["event_source_required_methods"])
    classifications = _classification(policy)
    if "get_supported_poll_rates" in methods:
        try:
            supported_poll_rates_contract(source_root, catalog, class_name)
        except ModelError:
            return (
                {
                    "state": BACKEND_REQUIRED,
                    "reason": "source-software-method-unproven:get_supported_poll_rates",
                    "record_id": record["record_id"],
                },
                [],
            )
        classifications["get_supported_poll_rates"] = "software"
    unclassified = sorted(methods - set(classifications))
    if unclassified:
        return (
            {
                "state": BACKEND_REQUIRED,
                "reason": "unclassified-methods:" + ",".join(unclassified),
                "record_id": record["record_id"],
            },
            [],
        )
    read_methods = {method for method in methods if classifications[method] == "read"}
    write_methods = {method for method in methods if classifications[method] == "write"}
    read_rules, covered_reads = _read_rules(
        root,
        source_root,
        record,
        read_methods,
        driver_attribute_overrides={
            "get_brightness": ("matrix_brightness",),
        },
        include_transaction_ids=True,
    )
    try:
        write_rules, covered_writes = _parameterize_openrazer_rules(
            root, constructor_rules, record, write_methods, source_root
        )
    except UnmodeledWriteRequest as error:
        return (
            {
                "state": BACKEND_REQUIRED,
                "reason": f"unmodeled-write-constructor:{error.owner}",
                "record_id": record["record_id"],
            },
            [],
        )
    missing = sorted((read_methods - covered_reads) | (write_methods - covered_writes))
    if missing:
        return (
            {
                "state": BACKEND_REQUIRED,
                "reason": "uncovered-wire-methods:" + ",".join(missing),
                "record_id": record["record_id"],
            },
            [],
        )
    return (
        {
            "state": SOURCE_VALIDATED,
            "reason": None,
            "record_id": record["record_id"],
            "class_name": class_name,
            "source": {
                "path": source_path,
                "line": parsed.line,
                "sha256": sha256_file(source_root / source_path),
            },
            "image_url": image_url,
            "methods": sorted(methods),
            "driver_family": "razermouse" if record["device_kind"] == "mouse" else "razerkbd",
            "event_file_regex": event_regex,
            "direct_usb_reference": direct,
            "publication_identity": _publication_identity(read_rules),
        },
        sorted(read_rules + write_rules, key=lambda rule: rule["id"]),
    )
