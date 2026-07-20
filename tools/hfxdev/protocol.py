# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .model import ModelError, load_json, require_unique


CATALOG_KEYS = {
    "$schema",
    "schema",
    "minimum_version",
    "maximum_version",
    "features",
    "records",
    "methods",
}
RECORD_KEYS = {"name", "description", "fields"}
FIELD_KEYS = {"name", "type", "required", "many", "max_items", "description"}
METHOD_KEYS = {"name", "request", "response", "required_feature", "description"}
TYPE_NAME = re.compile(r"^[A-Z][A-Za-z0-9]+$")
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9.-]*$")
BUILTIN_TYPES = {"Boolean"}
RESERVED_FIELD_NAMES = {
    "alignas",
    "alignof",
    "and",
    "as",
    "async",
    "await",
    "break",
    "case",
    "catch",
    "class",
    "const",
    "continue",
    "crate",
    "def",
    "default",
    "delete",
    "do",
    "else",
    "enum",
    "except",
    "extern",
    "false",
    "fn",
    "for",
    "from",
    "if",
    "impl",
    "import",
    "in",
    "let",
    "loop",
    "match",
    "mod",
    "move",
    "namespace",
    "new",
    "not",
    "operator",
    "or",
    "pass",
    "private",
    "protected",
    "public",
    "raise",
    "ref",
    "return",
    "self",
    "sizeof",
    "static",
    "struct",
    "super",
    "switch",
    "template",
    "this",
    "throw",
    "trait",
    "true",
    "try",
    "type",
    "typename",
    "union",
    "unsafe",
    "use",
    "virtual",
    "where",
    "while",
    "with",
    "yield",
}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type_name: str
    required: bool
    many: bool
    max_items: int | None
    description: str


@dataclass(frozen=True)
class RecordSpec:
    name: str
    description: str
    fields: tuple[FieldSpec, ...]


@dataclass(frozen=True)
class MethodSpec:
    name: str
    request: str
    response: str
    required_feature: str | None
    description: str


@dataclass(frozen=True)
class ProtocolCatalog:
    minimum_version: int
    maximum_version: int
    features: tuple[str, ...]
    records: tuple[RecordSpec, ...]
    methods: tuple[MethodSpec, ...]


def _exact(value: dict[str, Any], keys: set[str], label: str) -> None:
    missing = sorted(keys - value.keys())
    extra = sorted(value.keys() - keys)
    if missing:
        raise ModelError(f"{label}: missing fields {', '.join(missing)}")
    if extra:
        raise ModelError(f"{label}: unknown fields {', '.join(extra)}")


def _description(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise ModelError(f"{label}: description must contain 1 through 240 characters")
    return value.strip()


def _named(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ModelError(f"{label}: invalid name")
    return value


def _domain_names(root: Path) -> set[str]:
    catalog = load_json(root / "schemas" / "domain-catalog.json")
    return {
        item["name"]
        for category in ("numeric_types", "string_types", "enums")
        for item in catalog[category]
    }


def _field(value: Any, label: str, known_types: set[str]) -> FieldSpec:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: field must be an object")
    _exact(value, FIELD_KEYS, label)
    name = _named(value["name"], FIELD_NAME, f"{label} name")
    if name in RESERVED_FIELD_NAMES:
        raise ModelError(f"{label}: field name is reserved in a generated language")
    type_name = _named(value["type"], TYPE_NAME, f"{label} type")
    if type_name not in known_types:
        raise ModelError(f"{label}: unknown or forward type reference {type_name}")
    required = value["required"]
    many = value["many"]
    if not isinstance(required, bool) or not isinstance(many, bool):
        raise ModelError(f"{label}: required and many must be boolean")
    max_items = value["max_items"]
    if many:
        if not required:
            raise ModelError(f"{label}: repeated fields are present arrays, not optional arrays")
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 4096:
            raise ModelError(f"{label}: repeated field requires max_items from 1 through 4096")
    elif max_items is not None:
        raise ModelError(f"{label}: scalar field must use null max_items")
    return FieldSpec(
        name=name,
        type_name=type_name,
        required=required,
        many=many,
        max_items=max_items,
        description=_description(value["description"], label),
    )


def _record(value: Any, index: int, known_types: set[str]) -> RecordSpec:
    if not isinstance(value, dict):
        raise ModelError(f"protocol record {index}: must be an object")
    label = f"protocol record {value.get('name', index)}"
    _exact(value, RECORD_KEYS, label)
    name = _named(value["name"], TYPE_NAME, f"{label} name")
    if name in known_types:
        raise ModelError(f"{label}: type name is already defined")
    fields_value = value["fields"]
    if not isinstance(fields_value, list):
        raise ModelError(f"{label}: fields must be an array")
    fields = tuple(_field(field, f"{label} field {field_index}", known_types) for field_index, field in enumerate(fields_value))
    require_unique([field.name for field in fields], f"{name} field name")
    return RecordSpec(
        name=name,
        description=_description(value["description"], label),
        fields=fields,
    )


def _method(value: Any, index: int, records: set[str], features: set[str]) -> MethodSpec:
    if not isinstance(value, dict):
        raise ModelError(f"protocol method {index}: must be an object")
    label = f"protocol method {value.get('name', index)}"
    _exact(value, METHOD_KEYS, label)
    name = _named(value["name"], IDENTIFIER, f"{label} name")
    request = _named(value["request"], TYPE_NAME, f"{label} request")
    response = _named(value["response"], TYPE_NAME, f"{label} response")
    if request not in records or response not in records:
        raise ModelError(f"{label}: request and response must name protocol records")
    feature = value["required_feature"]
    if feature is not None:
        feature = _named(feature, IDENTIFIER, f"{label} required_feature")
        if feature not in features:
            raise ModelError(f"{label}: required feature is not declared")
    return MethodSpec(
        name=name,
        request=request,
        response=response,
        required_feature=feature,
        description=_description(value["description"], label),
    )


def load_protocol_catalog(root: Path) -> ProtocolCatalog:
    value = load_json(root / "protocol" / "catalog.json")
    _exact(value, CATALOG_KEYS, "protocol catalog")
    if value["schema"] != "hyperflux-protocol-catalog-v1":
        raise ModelError("unsupported protocol catalog schema")
    minimum = value["minimum_version"]
    maximum = value["maximum_version"]
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or not 1 <= minimum <= maximum <= 65_535
    ):
        raise ModelError("protocol version range is invalid")
    features_value = value["features"]
    if not isinstance(features_value, list):
        raise ModelError("protocol features must be an array")
    features = tuple(_named(item, IDENTIFIER, "protocol feature") for item in features_value)
    require_unique(list(features), "protocol feature")

    records_value = value["records"]
    if not isinstance(records_value, list) or not records_value:
        raise ModelError("protocol catalog must contain records")
    known_types = _domain_names(root) | BUILTIN_TYPES
    records: list[RecordSpec] = []
    for index, item in enumerate(records_value):
        record = _record(item, index, known_types)
        records.append(record)
        known_types.add(record.name)
    require_unique([record.name for record in records], "protocol record name")
    record_names = {record.name for record in records}

    methods_value = value["methods"]
    if not isinstance(methods_value, list) or not methods_value:
        raise ModelError("protocol catalog must contain methods")
    methods = tuple(
        _method(item, index, record_names, set(features))
        for index, item in enumerate(methods_value)
    )
    require_unique([method.name for method in methods], "protocol method name")
    return ProtocolCatalog(
        minimum_version=minimum,
        maximum_version=maximum,
        features=features,
        records=tuple(records),
        methods=methods,
    )
