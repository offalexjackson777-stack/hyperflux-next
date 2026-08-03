# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

from .model import ModelError
from .native_probe import simple_c_defines
from .openrazer_constructor_execution import _compile_constructor_reports
from .openrazer_constructor_parsing import (
    DRIVER_HEADER_PATHS,
    DRIVER_PATHS,
    ConstructorNotApplicable,
    ConstructorWitness,
    UnmodeledWriteRequest,
    _driver_attribute_readers,
    _driver_attribute_writers,
    _python_method_attributes,
    _trace_reader,
    _trace_writer,
    _transitive_constructor_functions,
)


def _matcher_accepts(matcher: dict[str, Any], arguments: bytes) -> bool:
    if matcher["kind"] == "fields":
        for field in matcher["fields"]:
            offset = field["offset"]
            kind = field["kind"]
            if kind == "exact":
                values = bytes(field["bytes"])
                if arguments[offset : offset + len(values)] != values:
                    return False
            elif kind == "one-of":
                if arguments[offset] not in field["values"]:
                    return False
            elif kind == "u8-range":
                if not field["minimum"] <= arguments[offset] <= field["maximum"]:
                    return False
            elif kind == "be16-range":
                value = int.from_bytes(arguments[offset : offset + 2], "big")
                if not field["minimum"] <= value <= field["maximum"]:
                    return False
            elif kind == "rgb":
                if len(arguments[offset : offset + field["pixels"] * 3]) != field["pixels"] * 3:
                    return False
            else:
                return False
        return True
    if matcher["kind"] == "custom-frame":
        if len(arguments) < 8 or arguments[0:2] != b"\x00\x00":
            return False
        row, start, stop = arguments[2], arguments[3], arguments[4]
        if row >= matcher["rows"] or start > stop or stop >= matcher["columns"]:
            return False
        used = 5 + (stop - start + 1) * 3
        return used <= len(arguments) and not any(arguments[used:])
    if matcher["kind"] == "dpi-stages":
        if len(arguments) != 38 or arguments[0] != 1:
            return False
        active, count = arguments[1], arguments[2]
        if count == 0 or count > matcher["maximum_stages"] or not 1 <= active <= count:
            return False
        for index in range(count):
            offset = 3 + index * 7
            x = int.from_bytes(arguments[offset + 1 : offset + 3], "big")
            y = int.from_bytes(arguments[offset + 3 : offset + 5], "big")
            if (
                arguments[offset] != index
                or not matcher["minimum_dpi"] <= x <= matcher["maximum_dpi"]
                or not matcher["minimum_dpi"] <= y <= matcher["maximum_dpi"]
                or arguments[offset + 5 : offset + 7] != b"\x00\x00"
            ):
                return False
        return not any(arguments[3 + count * 7 :])
    return False


def _report_matches_rule(report: bytes, rule: dict[str, Any]) -> bool:
    data_size = report[5]
    data_sizes = rule.get("data_sizes")
    if data_sizes is None:
        data_sizes = [rule["data_size"]]
    if (
        report[0] != 0
        or data_size not in data_sizes
        or report[6] != rule["command_class"]
        or report[7] != rule["command_id"]
        or data_size == 0
        or data_size > 80
        or any(report[8 + data_size : 88])
        or report[89] != 0
    ):
        return False
    return _matcher_accepts(rule["matcher"], report[8 : 8 + data_size])


def constructor_witness_matches_rule(
    witness: ConstructorWitness, rule: dict[str, Any]
) -> bool:
    data_sizes = rule.get("data_sizes")
    if data_sizes is None:
        data_sizes = [rule["data_size"]]
    return (
        witness.data_size in data_sizes
        and witness.command_class == rule["command_class"]
        and witness.command_id == rule["command_id"]
        and len(witness.arguments) == witness.data_size
        and _matcher_accepts(rule["matcher"], bytes(witness.arguments))
    )


def source_bound_write_witnesses(
    root: Path,
    source_root: Path,
    record: dict[str, Any],
    rule: dict[str, Any],
    *,
    validate_rule: bool = True,
) -> list[ConstructorWitness]:
    kind = record["device_kind"]
    product_id = record["usb_identity"]["product_id"]
    driver_path = DRIVER_PATHS[kind]
    driver_source = (source_root / driver_path).read_text(encoding="utf-8")
    constants = simple_c_defines(
        (source_root / DRIVER_HEADER_PATHS[kind]).read_text(encoding="utf-8")
    )
    constants.update(
        simple_c_defines((source_root / "driver" / "razercommon.h").read_text(encoding="utf-8"))
    )
    pid_names = sorted(name for name, value in constants.items() if value == product_id)
    if not pid_names or not any(
        re.search(
            rf"HID_USB_DEVICE\(\s*USB_VENDOR_ID_RAZER\s*,\s*{re.escape(name)}\s*\)",
            driver_source,
        )
        for name in pid_names
    ):
        raise ConstructorNotApplicable(
            f"{record['record_id']}: exact PID is absent from the upstream HID table"
        )
    writers = _driver_attribute_writers(driver_source)
    method_attributes = _python_method_attributes(source_root)
    expected = {
        function
        for source in rule["sources"]
        if source["path"] == "driver/razerchromacommon.c"
        for function in source["functions"]
    }
    chroma_source = (source_root / "driver" / "razerchromacommon.c").read_text(
        encoding="utf-8"
    )
    expected = set(_transitive_constructor_functions(chroma_source, expected))
    pending: list[
        tuple[str, str, str, str, tuple[int, ...], tuple[str, ...]]
    ] = []
    for method in rule["methods"]:
        attributes = method_attributes.get(method, ())
        if not attributes:
            raise ConstructorNotApplicable(
                f"{record['record_id']}/{method}: OpenRazer method has no source-bound driver attribute"
            )
        method_matches = 0
        for attribute in attributes:
            entrypoint = writers.get(attribute)
            if entrypoint is None:
                continue
            expressions, transactions, functions = _trace_writer(
                driver_source,
                entrypoint,
                product_id,
                constants,
                expected,
            )
            if not expressions:
                continue
            if not transactions:
                raise ConstructorNotApplicable(
                    f"{record['record_id']}/{method}: OpenRazer writer has no nonzero transaction selector"
                )
            for owner, expression in expressions:
                pending.append(
                    (
                        method,
                        attribute,
                        entrypoint,
                        expression,
                        tuple(sorted(transactions)),
                        tuple(sorted(functions)),
                    )
                )
                method_matches += 1
        if method_matches == 0:
            raise ConstructorNotApplicable(
                f"{record['record_id']}/{method}: exact PID branch does not use the policy constructor"
            )

    expressions = sorted({item[3] for item in pending})
    reports = _compile_constructor_reports(root, source_root, expressions)
    witnesses: list[ConstructorWitness] = []
    for method, attribute, entrypoint, expression, transactions, functions in pending:
        report = reports[expression]
        data_size = report[5]
        if (
            report[0] != 0
            or data_size == 0
            or data_size > 80
            or any(report[8 + data_size : 88])
            or report[89] != 0
        ):
            raise ModelError(
                f"{record['record_id']}/{method}: compiled upstream constructor emitted an invalid request"
            )
        if validate_rule and not _report_matches_rule(report, rule):
            raise ModelError(
                f"{record['record_id']}/{method}: compiled upstream constructor is rejected by the generated matcher"
            )
        witnesses.append(
            ConstructorWitness(
                method=method,
                attribute=attribute,
                entrypoint=entrypoint,
                owner=expression.split("(", 1)[0],
                expression=expression,
                transaction_ids=transactions,
                source_functions=functions,
                constructor_functions=tuple(sorted({*expected, "get_razer_report"})),
                command_class=report[6],
                command_id=report[7],
                data_size=report[5],
                arguments=tuple(report[8 : 8 + report[5]]),
                report_sha256=hashlib.sha256(report).hexdigest(),
            )
        )
    return sorted(
        set(witnesses),
        key=lambda value: (
            value.method,
            value.attribute,
            value.entrypoint,
            value.owner,
            value.expression,
        ),
    )


def verify_write_request_closure(
    root: Path,
    source_root: Path,
    record: dict[str, Any],
    methods: set[str],
    rules: list[dict[str, Any]],
) -> None:
    kind = record["device_kind"]
    product_id = record["usb_identity"]["product_id"]
    driver_source = (source_root / DRIVER_PATHS[kind]).read_text(encoding="utf-8")
    constants = simple_c_defines(
        (source_root / DRIVER_HEADER_PATHS[kind]).read_text(encoding="utf-8")
    )
    constants.update(
        simple_c_defines(
            (source_root / "driver" / "razercommon.h").read_text(encoding="utf-8")
        )
    )
    chroma_source = (source_root / "driver" / "razerchromacommon.c").read_text(
        encoding="utf-8"
    )
    constructors = set(
        re.findall(
            r"(?m)^struct\s+razer_report\s+(razer_chroma_[a-z0-9_]+)\s*\(",
            chroma_source,
        )
    )
    constructors.add("get_razer_report")
    writers = _driver_attribute_writers(driver_source)
    attributes = _python_method_attributes(source_root)
    expressions: set[str] = set()
    for method in sorted(methods):
        for attribute in attributes.get(method, ()):
            writer = writers.get(attribute)
            if writer is None:
                continue
            traced, _transactions, _functions = _trace_writer(
                driver_source,
                writer,
                product_id,
                constants,
                constructors,
            )
            expressions.update(expression for _owner, expression in traced)
    if not expressions:
        raise ConstructorNotApplicable(
            f"{record['record_id']}: exact PID has no source-bound write requests"
        )
    reports = _compile_constructor_reports(root, source_root, sorted(expressions))
    for expression, report in reports.items():
        if not any(_report_matches_rule(report, rule) for rule in rules):
            raise UnmodeledWriteRequest(record["record_id"], expression)


def source_bound_read_witnesses(
    root: Path,
    source_root: Path,
    record: dict[str, Any],
    method: str,
    driver_attributes: tuple[str, ...] | None = None,
) -> list[ConstructorWitness]:
    kind = record["device_kind"]
    product_id = record["usb_identity"]["product_id"]
    driver_path = DRIVER_PATHS[kind]
    driver_source = (source_root / driver_path).read_text(encoding="utf-8")
    constants = simple_c_defines(
        (source_root / DRIVER_HEADER_PATHS[kind]).read_text(encoding="utf-8")
    )
    constants.update(
        simple_c_defines(
            (source_root / "driver" / "razercommon.h").read_text(encoding="utf-8")
        )
    )
    pid_names = sorted(name for name, value in constants.items() if value == product_id)
    if not pid_names or not any(
        re.search(
            rf"HID_USB_DEVICE\(\s*USB_VENDOR_ID_RAZER\s*,\s*{re.escape(name)}\s*\)",
            driver_source,
        )
        for name in pid_names
    ):
        raise ConstructorNotApplicable(
            f"{record['record_id']}: exact PID is absent from the upstream HID table"
        )

    attributes = (
        driver_attributes
        if driver_attributes is not None
        else _python_method_attributes(source_root).get(method, ())
    )
    if not attributes:
        raise ConstructorNotApplicable(
            f"{record['record_id']}/{method}: OpenRazer method has no source-bound driver attribute"
        )
    readers = _driver_attribute_readers(driver_source)
    chroma_source = (source_root / "driver" / "razerchromacommon.c").read_text(
        encoding="utf-8"
    )
    all_constructors = set(
        re.findall(
            r"(?m)^struct\s+razer_report\s+(razer_chroma_[a-z0-9_]+)\s*\(",
            chroma_source,
        )
    )
    all_constructors.add("get_razer_report")
    pending: list[
        tuple[str, str, str, str, tuple[int, ...], tuple[str, ...]]
    ] = []
    for attribute in attributes:
        reader = readers.get(attribute)
        if reader is None:
            continue
        expressions, transactions, functions = _trace_reader(
            driver_source,
            reader,
            product_id,
            constants,
            all_constructors,
        )
        for owner, expression in expressions:
            pending.append(
                (
                    attribute,
                    reader,
                    owner,
                    expression,
                    tuple(sorted(transactions)),
                    tuple(sorted(functions)),
                )
            )
    if not pending:
        raise ConstructorNotApplicable(
            f"{record['record_id']}/{method}: exact PID reader has no source-bound request"
        )
    reports = _compile_constructor_reports(
        root, source_root, sorted({item[3] for item in pending})
    )
    witnesses: list[ConstructorWitness] = []
    for attribute, reader, owner, expression, transactions, functions in pending:
        report = reports[expression]
        data_size = report[5]
        if (
            report[0] != 0
            or data_size == 0
            or data_size > 80
            or report[7] < 0x80
            or any(report[8 + data_size : 88])
            or report[89] != 0
        ):
            raise ModelError(
                f"{record['record_id']}/{method}: exact upstream read constructor emitted an invalid request"
            )
        constructor_functions = (
            {"get_razer_report"}
            if owner == "get_razer_report"
            else {
                *_transitive_constructor_functions(chroma_source, {owner}),
                "get_razer_report",
            }
        )
        witnesses.append(
            ConstructorWitness(
                method=method,
                attribute=attribute,
                entrypoint=reader,
                owner=owner,
                expression=expression,
                transaction_ids=transactions,
                source_functions=functions,
                constructor_functions=tuple(sorted(constructor_functions)),
                command_class=report[6],
                command_id=report[7],
                data_size=data_size,
                arguments=tuple(report[8 : 8 + data_size]),
                report_sha256=hashlib.sha256(report).hexdigest(),
            )
        )
    return sorted(
        set(witnesses),
        key=lambda value: (
            value.method,
            value.attribute,
            value.entrypoint,
            value.owner,
            value.expression,
        ),
    )
