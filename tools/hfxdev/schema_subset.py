# SPDX-License-Identifier: GPL-2.0-only

"""A small JSON Schema (draft 2020-12) checker for the keywords the repository uses.

The repository has no third-party JSON Schema dependency, so the owner-report
ingest checks reports with this subset. It refuses any keyword it does not know
instead of silently ignoring it, and its messages name a JSON path and the rule
that failed, never the offending value: a refused report may hold private text.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from .model import ModelError

SUPPORTED_KEYWORDS = frozenset(
    {
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "additionalProperties",
        "const",
        "description",
        "enum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
        "uniqueItems",
    }
)

_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)


class SchemaViolation(ModelError):
    """A value does not match the schema. The message holds a path, not the value."""


def validate(schema: dict[str, Any], value: Any) -> None:
    """Raise SchemaViolation when value does not match schema."""
    _check_keywords(schema, "#")
    _validate(schema, schema, value, "$")


def _check_keywords(node: Any, location: str) -> None:
    if isinstance(node, dict):
        unknown = sorted(set(node) - SUPPORTED_KEYWORDS)
        if unknown:
            raise ModelError(f"schema {location} uses unsupported keywords: {', '.join(unknown)}")
        for key in ("properties", "$defs"):
            for name, child in node.get(key, {}).items():
                _check_keywords(child, f"{location}/{key}/{name}")
        if isinstance(node.get("items"), dict):
            _check_keywords(node["items"], f"{location}/items")
        if isinstance(node.get("additionalProperties"), dict):
            _check_keywords(node["additionalProperties"], f"{location}/additionalProperties")
        for index, child in enumerate(node.get("oneOf", [])):
            _check_keywords(child, f"{location}/oneOf/{index}")


def _resolve(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ModelError(f"schema reference {reference} is not local")
    node: Any = root
    for part in reference[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise ModelError(f"schema reference {reference} does not resolve")
        node = node[part]
    if not isinstance(node, dict):
        raise ModelError(f"schema reference {reference} does not resolve to a schema")
    return node


def _type_matches(expected: str, value: Any) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise ModelError(f"schema type {expected} is not supported")


def _equal(left: Any, right: Any) -> bool:
    # JSON equality: true is not 1, and 1 is 1.0.
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _validate(root: dict[str, Any], schema: dict[str, Any], value: Any, path: str) -> None:
    if "$ref" in schema:
        _validate(root, _resolve(root, schema["$ref"]), value, path)
    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            try:
                _validate(root, option, value, path)
            except SchemaViolation:
                continue
            matches += 1
        if matches != 1:
            raise SchemaViolation(f"{path}: does not match exactly one allowed shape")
    if "type" in schema:
        expected = schema["type"]
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(option, value) for option in options):
            raise SchemaViolation(f"{path}: must be {' or '.join(options)}")
    if "const" in schema and not _equal(schema["const"], value):
        raise SchemaViolation(f"{path}: is not the required fixed value")
    if "enum" in schema and not any(_equal(option, value) for option in schema["enum"]):
        raise SchemaViolation(f"{path}: is not one of the allowed values")
    if isinstance(value, str):
        _validate_string(schema, value, path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaViolation(f"{path}: is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaViolation(f"{path}: is above the maximum")
    if isinstance(value, list):
        _validate_array(root, schema, value, path)
    if isinstance(value, dict):
        _validate_object(root, schema, value, path)


def _validate_string(schema: dict[str, Any], value: str, path: str) -> None:
    if "minLength" in schema and len(value) < schema["minLength"]:
        raise SchemaViolation(f"{path}: is shorter than allowed")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise SchemaViolation(f"{path}: is longer than allowed")
    if "pattern" in schema and re.search(schema["pattern"], value) is None:
        raise SchemaViolation(f"{path}: does not match the required pattern")
    if schema.get("format") == "date-time":
        if _DATE_TIME.match(value) is None:
            raise SchemaViolation(f"{path}: is not an RFC 3339 date-time")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise SchemaViolation(f"{path}: is not an RFC 3339 date-time") from error
    elif "format" in schema:
        raise ModelError(f"schema format {schema['format']} is not supported")


def _validate_array(root: dict[str, Any], schema: dict[str, Any], value: list[Any], path: str) -> None:
    if "minItems" in schema and len(value) < schema["minItems"]:
        raise SchemaViolation(f"{path}: has fewer items than allowed")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        raise SchemaViolation(f"{path}: has more items than allowed")
    if schema.get("uniqueItems"):
        for index, item in enumerate(value):
            if any(_equal(item, other) for other in value[:index]):
                raise SchemaViolation(f"{path}: items must be unique")
    if isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate(root, schema["items"], item, f"{path}[{index}]")


def _validate_object(root: dict[str, Any], schema: dict[str, Any], value: dict[str, Any], path: str) -> None:
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in value:
            raise SchemaViolation(f"{path}: is missing required field {name}")
    additional = schema.get("additionalProperties", True)
    unexpected = [name for name in value if name not in properties]
    if unexpected and additional is False:
        # The field names came from the report, so they are counted, not printed.
        raise SchemaViolation(f"{path}: has {len(unexpected)} field(s) the report format does not allow")
    for name, child in properties.items():
        if name in value:
            _validate(root, child, value[name], f"{path}.{name}")
    if isinstance(additional, dict):
        for name in unexpected:
            _validate(root, additional, value[name], f"{path}.<field>")
