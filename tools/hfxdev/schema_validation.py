# SPDX-License-Identifier: GPL-2.0-only

"""Check a value against a JSON Schema with the best validator this machine has.

Inside the pinned development image (CI and `./hfx ci verify`) the real
`jsonschema` draft 2020-12 validator runs, with the repository schemas in a
`referencing` registry, exactly as the schema-instances node sets it up. The
host has no third-party packages, so there the stdlib `schema_subset` checker
stands in. A CI test keeps the two verdicts in step on every owner-report case,
and the subset must refuse anything the real validator refuses.

Both raise the same `SchemaViolation`, and both follow one privacy contract:
the message names a JSON path and the rule that failed, never the value, and
never the name of a key the schema does not declare.
"""

from __future__ import annotations

from functools import lru_cache
import importlib.util
from pathlib import Path
from typing import Any

from . import schema_subset
from .schema_subset import SchemaViolation

__all__ = [
    "JSONSCHEMA",
    "SUBSET",
    "SchemaViolation",
    "jsonschema_available",
    "error_path",
    "jsonschema_validator",
    "selected_validator",
    "validate",
    "validate_with_jsonschema",
    "validate_with_subset",
]

JSONSCHEMA = "jsonschema"
SUBSET = "subset"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_REQUIRED_MODULES = ("jsonschema", "referencing", "jsonschema_specifications")


def jsonschema_available() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in _REQUIRED_MODULES)


def selected_validator() -> str:
    """`jsonschema` where the pinned image provides it, otherwise `subset`."""
    return JSONSCHEMA if jsonschema_available() else SUBSET


def validate(schema: dict[str, Any], value: Any, *, root: Path | None = None) -> None:
    """Raise SchemaViolation when value does not match schema.

    `root` is the checkout whose schemas the real validator may refer to; it
    defaults to this repository.
    """
    if selected_validator() == JSONSCHEMA:
        validate_with_jsonschema(schema, value, root=root)
    else:
        validate_with_subset(schema, value)


def validate_with_subset(schema: dict[str, Any], value: Any) -> None:
    schema_subset.validate(schema, value)


def validate_with_jsonschema(schema: dict[str, Any], value: Any, *, root: Path | None = None) -> None:
    errors = list(jsonschema_validator(schema, root=root).iter_errors(value))
    if not errors:
        return
    # The shallowest failure first, as the subset reports an object's own
    # rules before it descends into the object's fields.
    error = min(errors, key=lambda item: len(item.absolute_path))
    raise SchemaViolation(f"{error_path(error)}: {_describe(error)}")


def jsonschema_validator(schema: dict[str, Any], *, root: Path | None = None) -> Any:
    """A draft 2020-12 validator over the repository registry and checked formats."""
    from jsonschema import Draft202012Validator

    from .schema_instances import format_checker

    return Draft202012Validator(
        schema, registry=_registry((root or REPOSITORY_ROOT).resolve()), format_checker=format_checker()
    )


@lru_cache(maxsize=4)
def _registry(root: Path) -> Any:
    from .model import repository_files
    from .schema_instances import load_schema_index, schema_registry

    files = [path for path in repository_files(root) if path.name.endswith(".schema.json")]
    return schema_registry(load_schema_index(root, files))


def error_path(error: Any) -> str:
    """The subset checker's path form (`$.a[0].<field>`) with undeclared names hidden."""
    from .schema_instances import redacted_path

    parts = ["$"]
    for segment in redacted_path(error):
        if segment is None:
            parts.append(".<field>")
        elif isinstance(segment, int):
            parts.append(f"[{segment}]")
        else:
            parts.append(f".{segment}")
    return "".join(parts)


def _describe(error: Any) -> str:
    """The subset checker's wording for the failed keyword.

    Reads only the schema (`validator_value`, `schema`) and, for a missing or
    unexpected field, which schema-declared names the object lacks or how many
    keys it has beyond them. Never the jsonschema message, which quotes values.
    """
    keyword = error.validator
    rule = error.validator_value
    instance = error.instance
    if keyword == "required" and isinstance(instance, dict):
        missing = [name for name in rule if name not in instance]
        return f"is missing required field {missing[0]}"
    if keyword == "additionalProperties" and isinstance(instance, dict):
        declared = error.schema.get("properties", {})
        unexpected = [name for name in instance if name not in declared]
        return f"has {len(unexpected)} field(s) the report format does not allow"
    if keyword == "type":
        options = rule if isinstance(rule, list) else [rule]
        return f"must be {' or '.join(options)}"
    if keyword == "format":
        if rule == "date-time":
            return "is not an RFC 3339 date-time"
        return f"is not a valid {rule}"
    fixed = {
        "const": "is not the required fixed value",
        "enum": "is not one of the allowed values",
        "minimum": "is below the minimum",
        "maximum": "is above the maximum",
        "minLength": "is shorter than allowed",
        "maxLength": "is longer than allowed",
        "pattern": "does not match the required pattern",
        "minItems": "has fewer items than allowed",
        "maxItems": "has more items than allowed",
        "uniqueItems": "items must be unique",
        "oneOf": "does not match exactly one allowed shape",
    }
    if keyword in fixed:
        return fixed[keyword]
    return f"does not satisfy {keyword if isinstance(keyword, str) else 'a rule'}"
