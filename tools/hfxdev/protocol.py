# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .model import ModelError, load_json, require_unique, sha256_file


CATALOG_KEYS = {
    "$schema",
    "schema",
    "minimum_version",
    "maximum_version",
    "limits",
    "features",
    "records",
    "unions",
    "methods",
}
REGISTRY_KEYS = {"$schema", "schema", "current_version", "versions"}
VERSION_KEYS = {"version", "catalog", "sha256", "served_features"}
RECORD_KEYS = {"name", "description", "fields"}
FIELD_KEYS = {"name", "type", "required", "many", "max_items", "description"}
METHOD_KEYS = {"name", "request", "response", "required_feature", "description"}
UNION_KEYS = {"name", "description", "tag", "content", "variants"}
VARIANT_KEYS = {"name", "wire", "type", "description"}
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
class VariantSpec:
    name: str
    wire: str
    type_name: str
    description: str


@dataclass(frozen=True)
class UnionSpec:
    name: str
    description: str
    tag: str
    content: str
    variants: tuple[VariantSpec, ...]


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
    max_message_bytes: int
    max_json_depth: int
    features: tuple[str, ...]
    records: tuple[RecordSpec, ...]
    unions: tuple[UnionSpec, ...]
    methods: tuple[MethodSpec, ...]


@dataclass(frozen=True)
class ProtocolVersion:
    version: int
    catalog_path: str
    source_sha256: str
    served_features: tuple[str, ...]
    catalog: ProtocolCatalog


@dataclass(frozen=True)
class ProtocolRegistry:
    current_version: int
    versions: tuple[ProtocolVersion, ...]

    @property
    def current(self) -> ProtocolCatalog:
        return next(
            item.catalog for item in self.versions if item.version == self.current_version
        )


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


def _union(value: Any, index: int, record_names: set[str]) -> UnionSpec:
    if not isinstance(value, dict):
        raise ModelError(f"protocol union {index}: must be an object")
    label = f"protocol union {value.get('name', index)}"
    _exact(value, UNION_KEYS, label)
    name = _named(value["name"], TYPE_NAME, f"{label} name")
    tag = _named(value["tag"], FIELD_NAME, f"{label} tag")
    content = _named(value["content"], FIELD_NAME, f"{label} content")
    if tag == content:
        raise ModelError(f"{label}: tag and content fields must differ")
    variants_value = value["variants"]
    if not isinstance(variants_value, list) or not variants_value:
        raise ModelError(f"{label}: variants must be a non-empty array")
    variants: list[VariantSpec] = []
    for variant_index, variant in enumerate(variants_value):
        variant_label = f"{label} variant {variant_index}"
        if not isinstance(variant, dict):
            raise ModelError(f"{variant_label}: must be an object")
        _exact(variant, VARIANT_KEYS, variant_label)
        type_name = _named(variant["type"], TYPE_NAME, f"{variant_label} type")
        if type_name not in record_names:
            raise ModelError(f"{variant_label}: variant type must name a protocol record")
        variants.append(
            VariantSpec(
                name=_named(variant["name"], TYPE_NAME, f"{variant_label} name"),
                wire=_named(variant["wire"], IDENTIFIER, f"{variant_label} wire"),
                type_name=type_name,
                description=_description(variant["description"], variant_label),
            )
        )
    require_unique([variant.name for variant in variants], f"{name} variant name")
    require_unique([variant.wire for variant in variants], f"{name} variant wire")
    return UnionSpec(
        name=name,
        description=_description(value["description"], label),
        tag=tag,
        content=content,
        variants=tuple(variants),
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


def _load_protocol_catalog(root: Path, path: Path) -> ProtocolCatalog:
    value = load_json(path)
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
    limits = value["limits"]
    if not isinstance(limits, dict):
        raise ModelError("protocol limits must be an object")
    _exact(limits, {"max_message_bytes", "max_json_depth"}, "protocol limits")
    max_message_bytes = limits["max_message_bytes"]
    max_json_depth = limits["max_json_depth"]
    if (
        isinstance(max_message_bytes, bool)
        or not isinstance(max_message_bytes, int)
        or not 4_096 <= max_message_bytes <= 16_777_216
    ):
        raise ModelError("protocol message byte bound is invalid")
    if max_json_depth != 128:
        raise ModelError("protocol JSON depth must match serde_json's bounded default of 128")
    features_value = value["features"]
    if not isinstance(features_value, list):
        raise ModelError("protocol features must be an array")
    features = tuple(_named(item, IDENTIFIER, "protocol feature") for item in features_value)
    require_unique(list(features), "protocol feature")

    records_value = value["records"]
    if not isinstance(records_value, list) or not records_value:
        raise ModelError("protocol catalog must contain records")
    record_names_declared = {
        _named(item.get("name"), TYPE_NAME, f"protocol record {index} name")
        for index, item in enumerate(records_value)
        if isinstance(item, dict)
    }
    if len(record_names_declared) != len(records_value):
        raise ModelError("protocol record declarations are invalid or duplicated")
    unions_value = value["unions"]
    if not isinstance(unions_value, list):
        raise ModelError("protocol unions must be an array")
    union_names_declared = {
        _named(item.get("name"), TYPE_NAME, f"protocol union {index} name")
        for index, item in enumerate(unions_value)
        if isinstance(item, dict)
    }
    if len(union_names_declared) != len(unions_value):
        raise ModelError("protocol union declarations are invalid or duplicated")
    base_types = _domain_names(root) | BUILTIN_TYPES
    if record_names_declared & base_types:
        raise ModelError("protocol record names collide with domain or built-in types")
    if union_names_declared & base_types:
        raise ModelError("protocol union names collide with domain or built-in types")
    if record_names_declared & union_names_declared:
        raise ModelError("protocol record and union names must be disjoint")
    known_types = base_types | record_names_declared | union_names_declared
    records: list[RecordSpec] = []
    for index, item in enumerate(records_value):
        record = _record(item, index, known_types)
        records.append(record)
    require_unique([record.name for record in records], "protocol record name")
    record_names = {record.name for record in records}
    unions = tuple(
        _union(item, index, record_names) for index, item in enumerate(unions_value)
    )
    require_unique([union.name for union in unions], "protocol union name")
    protocol_types = record_names | {union.name for union in unions}

    methods_value = value["methods"]
    if not isinstance(methods_value, list) or not methods_value:
        raise ModelError("protocol catalog must contain methods")
    methods = tuple(
        _method(item, index, protocol_types, set(features))
        for index, item in enumerate(methods_value)
    )
    require_unique([method.name for method in methods], "protocol method name")
    return ProtocolCatalog(
        minimum_version=minimum,
        maximum_version=maximum,
        max_message_bytes=max_message_bytes,
        max_json_depth=max_json_depth,
        features=features,
        records=tuple(records),
        unions=unions,
        methods=methods,
    )


def load_protocol_registry(root: Path) -> ProtocolRegistry:
    value = load_json(root / "protocol" / "registry.json")
    _exact(value, REGISTRY_KEYS, "protocol registry")
    if value["schema"] != "hyperflux-protocol-registry-v1":
        raise ModelError("unsupported protocol registry schema")
    current_version = value["current_version"]
    if (
        isinstance(current_version, bool)
        or not isinstance(current_version, int)
        or not 1 <= current_version <= 65_535
    ):
        raise ModelError("protocol current version is invalid")
    raw_versions = value["versions"]
    if not isinstance(raw_versions, list) or not raw_versions:
        raise ModelError("protocol registry must contain at least one version")

    versions: list[ProtocolVersion] = []
    for index, entry in enumerate(raw_versions):
        label = f"protocol registry version {index}"
        if not isinstance(entry, dict):
            raise ModelError(f"{label}: must be an object")
        _exact(entry, VERSION_KEYS, label)
        version = entry["version"]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or not 1 <= version <= 65_535
        ):
            raise ModelError(f"{label}: invalid version")
        expected_path = f"v{version}/catalog.json"
        if entry["catalog"] != expected_path:
            raise ModelError(f"{label}: catalog path must be {expected_path}")
        digest = entry["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ModelError(f"{label}: invalid source digest")
        path = root / "protocol" / expected_path
        if not path.is_file() or sha256_file(path) != digest:
            raise ModelError(f"{label}: frozen catalog digest mismatch")
        catalog = _load_protocol_catalog(root, path)
        if catalog.minimum_version != version or catalog.maximum_version != version:
            raise ModelError(f"{label}: catalog must describe exactly version {version}")
        raw_served_features = entry["served_features"]
        if not isinstance(raw_served_features, list) or len(raw_served_features) > 64:
            raise ModelError(f"{label}: served features must be a bounded array")
        served_features = tuple(
            _named(feature, IDENTIFIER, f"{label} served feature")
            for feature in raw_served_features
        )
        require_unique(list(served_features), f"{label} served feature")
        if tuple(sorted(served_features)) != served_features:
            raise ModelError(f"{label}: served features must be sorted")
        unknown_features = set(served_features) - set(catalog.features)
        if unknown_features:
            raise ModelError(f"{label}: served features are absent from the catalog")
        versions.append(
            ProtocolVersion(
                version=version,
                catalog_path=expected_path,
                source_sha256=digest,
                served_features=served_features,
                catalog=catalog,
            )
        )

    version_numbers = [item.version for item in versions]
    require_unique(version_numbers, "protocol registry version")
    if version_numbers != sorted(version_numbers):
        raise ModelError("protocol registry versions must be sorted")
    if current_version not in version_numbers:
        raise ModelError("protocol current version is not registered")
    return ProtocolRegistry(current_version=current_version, versions=tuple(versions))


def load_protocol_catalog(root: Path) -> ProtocolCatalog:
    return load_protocol_registry(root).current
