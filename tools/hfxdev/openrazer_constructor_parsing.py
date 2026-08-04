# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from typing import Any

from .model import ModelError
from .native_probe import c_function_source, simple_c_defines


REPORT_BYTES = 90
DRIVER_PATHS = {
    "keyboard": "driver/razerkbd_driver.c",
    "mouse": "driver/razermouse_driver.c",
}
DRIVER_HEADER_PATHS = {
    "keyboard": "driver/razerkbd_driver.h",
    "mouse": "driver/razermouse_driver.h",
}
CONSTRUCTOR_CALL = re.compile(
    r"\b(razer_chroma_[a-z0-9_]+|get_razer_report)\s*\("
)
WRITER_CALL = re.compile(r"\b(razer_attr_write_[a-z0-9_]+)\s*\(")
READER_CALL = re.compile(r"\b(razer_attr_read_[a-z0-9_]+)\s*\(")
REQUEST_ASSIGNMENT = re.compile(
    r"\brequest\s*=\s*(razer_chroma_[a-z0-9_]+|get_razer_report)\s*\("
)
TRANSACTION_ASSIGNMENT = re.compile(
    r"\brequest\.transaction_id\.id\s*=\s*"
    r"([A-Z][A-Z0-9_]*|0[xX][0-9A-Fa-f]+|[0-9]+)\s*;"
)
SIMPLE_ASSIGNMENT = re.compile(
    r"(?m)(?<![.>])\b([a-z_][a-z0-9_]*)\s*=\s*"
    r"([A-Z][A-Z0-9_]*|0[xX][0-9A-Fa-f]+|[0-9]+|true|false|[a-z_][a-z0-9_]*)\s*;"
)
DEVICE_ATTR = re.compile(
    r"(?m)^static\s+DEVICE_ATTR\(\s*(?P<attribute>[a-z0-9_]+)\s*,"
    r"\s*[0-7]+\s*,\s*(?P<reader>razer_attr_read_[a-z0-9_]+|NULL)\s*,"
    r"\s*(?P<writer>razer_attr_write_[a-z0-9_]+|NULL)\s*\)\s*;"
)
SAFE_EXPRESSION = re.compile(r"[A-Za-z0-9_\s,&*+\-\[\]().]+\Z")
_PYTHON_ANALYSIS_CACHE: dict[
    str,
    tuple[
        tuple[tuple[str, int, int], ...],
        dict[str, tuple[str, ...]],
        frozenset[str],
    ],
] = {}


@dataclass(frozen=True)
class ConstructorWitness:
    method: str
    attribute: str
    entrypoint: str
    owner: str
    expression: str
    transaction_ids: tuple[int, ...]
    source_functions: tuple[str, ...]
    constructor_functions: tuple[str, ...]
    command_class: int
    command_id: int
    data_size: int
    arguments: tuple[int, ...]
    report_sha256: str


class ConstructorNotApplicable(ModelError):
    """The exact PID path does not use this constructor family."""


class UnmodeledWriteRequest(ConstructorNotApplicable):
    def __init__(self, record_id: str, expression: str) -> None:
        self.record_id = record_id
        self.expression = expression
        self.owner = expression.split("(", 1)[0]
        super().__init__(
            f"{record_id}: unmodeled upstream write request from {expression}"
        )


def constructor_source_path(function: str) -> str:
    if function == "get_razer_report":
        return "driver/razercommon.c"
    if function.startswith("razer_chroma_"):
        return "driver/razerchromacommon.c"
    raise ModelError(f"unknown OpenRazer constructor source: {function}")


def _mask_c(source: str) -> str:
    output = list(source)
    index = 0
    while index < len(source):
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            if end < 0:
                end = len(source)
            for cursor in range(index, end):
                output[cursor] = " "
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise ModelError("unterminated comment in pinned OpenRazer source")
            end += 2
            for cursor in range(index, end):
                if output[cursor] != "\n":
                    output[cursor] = " "
            index = end
            continue
        if source[index] in {'"', "'"}:
            quote = source[index]
            cursor = index + 1
            while cursor < len(source):
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == quote:
                    cursor += 1
                    break
                cursor += 1
            else:
                raise ModelError("unterminated string in pinned OpenRazer source")
            for position in range(index, min(cursor, len(source))):
                if output[position] != "\n":
                    output[position] = " "
            index = cursor
            continue
        index += 1
    return "".join(output)


def _matching(source: str, start: int, opening: str, closing: str) -> int:
    if start >= len(source) or source[start] != opening:
        raise ModelError("invalid delimiter in pinned OpenRazer source")
    depth = 1
    cursor = start + 1
    while cursor < len(source):
        if source[cursor] == opening:
            depth += 1
        elif source[cursor] == closing:
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    raise ModelError("unterminated delimiter in pinned OpenRazer source")


def _top_level_labels(block: str) -> list[tuple[str, str, int, int]]:
    masked = _mask_c(block)
    labels: list[tuple[str, str, int, int]] = []
    depth = 0
    cursor = 0
    while cursor < len(masked):
        character = masked[cursor]
        if character == "{":
            depth += 1
            cursor += 1
            continue
        if character == "}":
            depth -= 1
            cursor += 1
            continue
        if depth != 0:
            cursor += 1
            continue
        match = re.match(r"\b(case|default)\b", masked[cursor:])
        if match is None:
            cursor += 1
            continue
        kind = match.group(1)
        label_start = cursor
        expression_start = cursor + len(kind)
        colon = expression_start
        paren_depth = 0
        while colon < len(masked):
            if masked[colon] == "(":
                paren_depth += 1
            elif masked[colon] == ")":
                paren_depth -= 1
            elif masked[colon] == ":" and paren_depth == 0:
                break
            colon += 1
        if colon >= len(masked):
            raise ModelError("unterminated case label in pinned OpenRazer source")
        expression = block[expression_start:colon].strip() if kind == "case" else ""
        labels.append((kind, expression, label_start, colon + 1))
        cursor = colon + 1
    return labels


def _top_level_terminator(segment: str) -> tuple[int, int, str] | None:
    masked = _mask_c(segment)
    depth = 0
    cursor = 0
    while cursor < len(masked):
        if masked[cursor] == "{":
            depth += 1
            cursor += 1
            continue
        if masked[cursor] == "}":
            depth -= 1
            cursor += 1
            continue
        if depth == 0:
            match = re.match(r"\b(break|return|goto)\b", masked[cursor:])
            if match is not None:
                end = masked.find(";", cursor + len(match.group(1)))
                if end < 0:
                    raise ModelError("unterminated switch terminator in OpenRazer source")
                return cursor, end + 1, match.group(1)
        cursor += 1
    return None


def _constant_value(token: str, constants: dict[str, int]) -> int | None:
    token = token.strip()
    if re.fullmatch(r"0[xX][0-9A-Fa-f]+|[0-9]+", token):
        return int(token, 0)
    return constants.get(token)


def _selected_case_body(
    block: str,
    product_id: int,
    constants: dict[str, int],
) -> str:
    labels = _top_level_labels(block)
    if not labels:
        raise ModelError("PID switch in OpenRazer source has no cases")
    selected: int | None = None
    fallback: int | None = None
    for index, (kind, expression, _start, _body_start) in enumerate(labels):
        if kind == "default":
            fallback = index
            continue
        if _constant_value(expression, constants) == product_id:
            selected = index
            break
    if selected is None:
        selected = fallback
    if selected is None:
        return ""

    pieces: list[str] = []
    for index in range(selected, len(labels)):
        _kind, _expression, _label_start, body_start = labels[index]
        body_end = labels[index + 1][2] if index + 1 < len(labels) else len(block)
        segment = block[body_start:body_end]
        terminator = _top_level_terminator(segment)
        if terminator is None:
            pieces.append(segment)
            continue
        start, end, kind = terminator
        if kind == "break":
            pieces.append(segment[:start])
        else:
            pieces.append(segment[:end])
        break
    return "".join(pieces)


def _rewrite_pid_switches(
    source: str,
    product_id: int,
    constants: dict[str, int],
) -> str:
    masked = _mask_c(source)
    output: list[str] = []
    cursor = 0
    while cursor < len(source):
        match = re.search(r"\bswitch\s*\(", masked[cursor:])
        if match is None:
            output.append(source[cursor:])
            break
        switch_start = cursor + match.start()
        open_paren = masked.find("(", switch_start)
        close_paren = _matching(masked, open_paren, "(", ")")
        open_brace = close_paren + 1
        while open_brace < len(masked) and masked[open_brace].isspace():
            open_brace += 1
        if open_brace >= len(masked) or masked[open_brace] != "{":
            raise ModelError("OpenRazer switch has no compound body")
        close_brace = _matching(masked, open_brace, "{", "}")
        output.append(source[cursor:switch_start])
        expression = masked[open_paren + 1 : close_paren]
        block = source[open_brace + 1 : close_brace]
        if "usb_pid" in expression:
            selected = _selected_case_body(block, product_id, constants)
            output.append("{\n")
            output.append(_rewrite_pid_switches(selected, product_id, constants))
            output.append("\n}")
        else:
            output.append(source[switch_start : open_brace + 1])
            output.append(_rewrite_pid_switches(block, product_id, constants))
            output.append("}")
        cursor = close_brace + 1
    return "".join(output)


def _split_arguments(arguments: str) -> list[str]:
    values: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    masked = _mask_c(arguments)
    for index, character in enumerate(masked):
        if character in depths:
            depths[character] += 1
        elif character in pairs:
            depths[pairs[character]] -= 1
        elif character == "," and not any(depths.values()):
            values.append(arguments[start:index].strip())
            start = index + 1
    values.append(arguments[start:].strip())
    return values if values != [""] else []


def _call_at(source: str, name_start: int) -> tuple[str, list[str], int]:
    match = re.match(r"([a-z_][a-z0-9_]*)\s*\(", source[name_start:])
    if match is None:
        raise ModelError("invalid function call in pinned OpenRazer source")
    name = match.group(1)
    open_paren = source.find("(", name_start + len(name))
    close_paren = _matching(_mask_c(source), open_paren, "(", ")")
    return name, _split_arguments(source[open_paren + 1 : close_paren]), close_paren + 1


def _function_parameters(function_source: str, name: str) -> list[str]:
    name_start = function_source.find(name)
    if name_start < 0:
        raise ModelError(f"OpenRazer function signature is malformed: {name}")
    open_paren = function_source.find("(", name_start + len(name))
    close_paren = _matching(_mask_c(function_source), open_paren, "(", ")")
    parameters: list[str] = []
    for parameter in _split_arguments(function_source[open_paren + 1 : close_paren]):
        identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", parameter)
        if not identifiers:
            raise ModelError(f"OpenRazer function parameter is malformed: {name}")
        parameters.append(identifiers[-1])
    return parameters


def _substitute_identifiers(expression: str, values: dict[str, str]) -> str:
    result = expression
    for name in sorted(values, key=len, reverse=True):
        replacement = values[name]
        if replacement == name:
            continue
        result = re.sub(rf"\b{re.escape(name)}\b", f"({replacement})", result)
    return result


def _simple_environment(source: str, inherited: dict[str, str]) -> dict[str, str]:
    environment = dict(inherited)
    for match in SIMPLE_ASSIGNMENT.finditer(source):
        name, value = match.groups()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
            environment[name] = environment.get(value, value)
        elif value in environment:
            environment[name] = environment[value]
        elif name in {"led_id", "storage", "variable_storage", "varstore"}:
            environment[name] = value
    return environment


def _driver_attribute_handlers(source: str, role: str) -> dict[str, str]:
    if role not in {"reader", "writer"}:
        raise ValueError(f"invalid OpenRazer DEVICE_ATTR role: {role}")
    handlers: dict[str, str] = {}
    for match in DEVICE_ATTR.finditer(source):
        attribute = match.group("attribute")
        handler = match.group(role)
        if attribute in handlers:
            raise ModelError(f"duplicate OpenRazer driver attribute: {attribute}")
        if handler != "NULL":
            handlers[attribute] = handler
    return handlers


def _driver_attribute_writers(source: str) -> dict[str, str]:
    return _driver_attribute_handlers(source, "writer")


def _driver_attribute_readers(source: str) -> dict[str, str]:
    return _driver_attribute_handlers(source, "reader")


def _python_method_analysis(
    source_root: Path,
) -> tuple[dict[str, tuple[str, ...]], frozenset[str]]:
    daemon_root = source_root / "daemon" / "openrazer_daemon"
    files = sorted(
        (daemon_root / "dbus_services" / "dbus_methods").glob("*.py")
    ) + [daemon_root / "hardware" / "device_base.py"]
    ripple_path = daemon_root / "misc" / "ripple_effect.py"
    fingerprint = tuple(
        (
            str(path.relative_to(source_root)),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in [*files, ripple_path]
    )
    cache_key = str(source_root.resolve())
    cached = _PYTHON_ANALYSIS_CACHE.get(cache_key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1], cached[2]

    direct: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    definitions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            definitions.setdefault(node.name, []).append(node)
            attributes = direct.setdefault(node.name, set())
            callees = calls.setdefault(node.name, set())
            for child in ast.walk(node):
                if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
                    continue
                if child.func.attr == "get_driver_path" and child.args:
                    try:
                        value = ast.literal_eval(child.args[0])
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, str):
                        attributes.add(value)
                elif (
                    child.func.attr == "join"
                    and isinstance(child.func.value, ast.Attribute)
                    and child.func.value.attr == "path"
                    and isinstance(child.func.value.value, ast.Name)
                    and child.func.value.value.id == "os"
                    and child.args
                ):
                    try:
                        value = ast.literal_eval(child.args[-1])
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, str):
                        attributes.add(value)
                elif (
                    isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "self"
                    and child.func.attr.startswith(("_get_", "_set_"))
                ):
                    callees.add(child.func.attr)

    def expanded(name: str, pending: set[str]) -> set[str]:
        if name in pending:
            raise ModelError(f"recursive OpenRazer method helper: {name}")
        result = set(direct.get(name, set()))
        for callee in calls.get(name, set()):
            result.update(expanded(callee, pending | {name}))
        return result

    result = {name: tuple(sorted(expanded(name, set()))) for name in direct}
    ripple_source = ripple_path.read_text(encoding="utf-8")
    if "self._parent._set_key_row(payload)" not in ripple_source or "self._parent._set_custom_effect()" not in ripple_source:
        raise ModelError("OpenRazer ripple engine no longer uses the bounded matrix helpers")
    ripple_attributes = set(result.get("_set_key_row", ())) | set(
        result.get("_set_custom_effect", ())
    )
    for name in ("set_ripple_effect", "set_ripple_effect_random_colour"):
        result[name] = tuple(sorted(set(result.get(name, ())) | ripple_attributes))
    pure_builtins = {"bool", "float", "int", "len", "list", "max", "min", "tuple"}
    pure_methods: set[str] = set()
    for name, method_definitions in definitions.items():
        pure = True
        for definition in method_definitions:
            nodes = [
                node for statement in definition.body for node in ast.walk(statement)
            ]
            if not any(isinstance(node, ast.Return) for node in nodes):
                pure = False
                break
            for node in nodes:
                if isinstance(
                    node,
                    (
                        ast.Await,
                        ast.Delete,
                        ast.Global,
                        ast.Nonlocal,
                        ast.Raise,
                        ast.Try,
                        ast.While,
                        ast.With,
                        ast.Yield,
                        ast.YieldFrom,
                        ast.Assign,
                        ast.AnnAssign,
                        ast.AugAssign,
                    ),
                ):
                    pure = False
                    break
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id in pure_builtins:
                    continue
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Attribute)
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == "self"
                    and node.func.value.attr == "logger"
                    and node.func.attr in {"debug", "info"}
                ):
                    continue
                pure = False
                break
            if not pure:
                break
        if pure:
            pure_methods.add(name)
    frozen_pure = frozenset(pure_methods)
    _PYTHON_ANALYSIS_CACHE[cache_key] = (fingerprint, result, frozen_pure)
    return result, frozen_pure


def _python_method_attributes(source_root: Path) -> dict[str, tuple[str, ...]]:
    return _python_method_analysis(source_root)[0]


def source_bound_cached_read(source_root: Path, method: str) -> bool:
    return method in _python_method_analysis(source_root)[1]


def _transitive_constructor_functions(source: str, roots: set[str]) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ModelError(f"recursive OpenRazer constructor path: {name}")
        visiting.add(name)
        body, _line = c_function_source(source, name)
        implementation = body[body.find("{") + 1 :]
        for called in CONSTRUCTOR_CALL.findall(implementation):
            if called == name:
                continue
            try:
                c_function_source(source, called)
            except ModelError:
                continue
            visit(called)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for root in sorted(roots):
        visit(root)
    return ordered


def _trace_attribute_path(
    driver_source: str,
    entrypoint: str,
    product_id: int,
    constants: dict[str, int],
    expected_constructors: set[str],
    helper_prefix: str,
    helper_calls: re.Pattern[str],
    inherited: dict[str, str] | None = None,
    stack: tuple[str, ...] = (),
) -> tuple[list[tuple[str, str]], set[int], set[str]]:
    if entrypoint in stack:
        raise ModelError(
            f"recursive OpenRazer attribute path: {' -> '.join((*stack, entrypoint))}"
        )
    function_source, _line = c_function_source(driver_source, entrypoint)
    rewritten = _rewrite_pid_switches(function_source, product_id, constants)
    body_start = rewritten.find("{")
    if body_start < 0:
        raise ModelError(f"OpenRazer attribute handler has no body: {entrypoint}")
    body = rewritten[body_start + 1 :]
    environment = _simple_environment(body, inherited or {})
    expressions: list[tuple[str, str]] = []
    functions = {entrypoint}

    for match in REQUEST_ASSIGNMENT.finditer(body):
        name, arguments, _end = _call_at(body, match.start(1))
        if name not in expected_constructors:
            continue
        rendered = f"{name}({', '.join(arguments)})"
        expressions.append((name, _substitute_identifiers(rendered, environment)))

    known_helpers = set(
        re.findall(
            rf"(?m)^static\s+ssize_t\s+({re.escape(helper_prefix)}[a-z0-9_]+)\s*\(",
            driver_source,
        )
    )
    for match in helper_calls.finditer(body):
        called, arguments, _end = _call_at(body, match.start(1))
        if called == entrypoint or called not in known_helpers:
            continue
        callee_source, _line = c_function_source(driver_source, called)
        parameters = _function_parameters(callee_source, called)
        if len(parameters) != len(arguments):
            raise ModelError(
                f"OpenRazer attribute call arity changed: {entrypoint} -> {called}"
            )
        bindings = {
            parameter: _substitute_identifiers(argument, environment)
            for parameter, argument in zip(parameters, arguments, strict=True)
        }
        nested, nested_transactions, nested_functions = _trace_attribute_path(
            driver_source,
            called,
            product_id,
            constants,
            expected_constructors,
            helper_prefix,
            helper_calls,
            bindings,
            (*stack, entrypoint),
        )
        expressions.extend(nested)
        functions.update(nested_functions)
        transactions = set(nested_transactions)
        break
    else:
        transactions = set()

    for match in TRANSACTION_ASSIGNMENT.finditer(body):
        value = _constant_value(match.group(1), constants)
        if value is None or value == 0 or value > 0xFF:
            raise ModelError(
                f"OpenRazer attribute handler {entrypoint} has an invalid transaction selector"
            )
        transactions.add(value)
    return expressions, transactions, functions


def _trace_writer(
    driver_source: str,
    writer: str,
    product_id: int,
    constants: dict[str, int],
    expected_constructors: set[str],
    inherited: dict[str, str] | None = None,
    stack: tuple[str, ...] = (),
) -> tuple[list[tuple[str, str]], set[int], set[str]]:
    return _trace_attribute_path(
        driver_source,
        writer,
        product_id,
        constants,
        expected_constructors,
        "razer_attr_write_",
        WRITER_CALL,
        inherited,
        stack,
    )


def _trace_reader(
    driver_source: str,
    reader: str,
    product_id: int,
    constants: dict[str, int],
    expected_constructors: set[str],
    inherited: dict[str, str] | None = None,
    stack: tuple[str, ...] = (),
) -> tuple[list[tuple[str, str]], set[int], set[str]]:
    return _trace_attribute_path(
        driver_source,
        reader,
        product_id,
        constants,
        expected_constructors,
        "razer_attr_read_",
        READER_CALL,
        inherited,
        stack,
    )
