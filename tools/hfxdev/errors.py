# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .model import ModelError, load_json, require_unique, sha256_file


MAX_ERRORS = 256
MAX_REMEDIATIONS = 128
MAX_SAFE_DETAIL_FIELDS = 12
MAX_SAFE_DETAIL_LENGTH = 256
MAX_ENUM_VALUES = 32

CATALOG_KEYS = {"$schema", "schema", "remediations", "errors"}
REMEDIATION_KEYS = {"id", "title", "safe_action", "verification", "automatic"}
ERROR_KEYS = {
    "code",
    "class",
    "severity",
    "retry_policy",
    "side_effect_certainty_policy",
    "remediation_id",
    "safe_detail_fields",
    "lifecycle",
    "owner",
    "technical_cause",
    "user_explanation",
    "privacy",
    "docs_path",
}
DETAIL_KEYS = {
    "name",
    "type",
    "required",
    "maximum_length",
    "maximum_value",
    "allowed_values",
    "privacy",
    "description",
}
LIFECYCLE_KEYS = {"state", "introduced_in", "deprecated_in", "replacement_code"}

CODE = re.compile(r"^HFX-([A-Z]+)-[0-9]{3}$")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")

ERROR_CLASSES = {
    "deadline",
    "generation",
    "integration",
    "internal",
    "kernel",
    "ownership",
    "persistence",
    "profile",
    "protocol",
    "queue",
    "request",
    "service",
    "transport",
}
SEVERITIES = {"info", "warning", "error", "critical"}
RETRY_POLICIES = {"never", "bounded-backoff", "after-remediation", "outcome-lookup-only"}
SIDE_EFFECT_POLICIES = {"not-applicable", "must-be-none", "runtime-reported", "possible", "partial"}
LIFECYCLE_STATES = {"active", "deprecated", "retired"}
OWNERS = {"kernel", "bridge", "sdk", "integration", "packaging", "tooling"}
PRIVACY_CLASSES = {"public", "public-summary"}
DETAIL_TYPES = {"boolean", "u16", "u32", "u64-decimal", "identifier", "text", "enum"}
NUMERIC_MAXIMA = {
    "u16": 65_535,
    "u32": 4_294_967_295,
    "u64-decimal": 18_446_744_073_709_551_615,
}
RESERVED_FIELD_NAMES = {
    "class",
    "def",
    "enum",
    "false",
    "fn",
    "from",
    "import",
    "in",
    "match",
    "namespace",
    "new",
    "none",
    "not",
    "operator",
    "or",
    "pass",
    "private",
    "public",
    "return",
    "self",
    "static",
    "struct",
    "template",
    "true",
    "type",
    "union",
    "use",
    "while",
}


@dataclass(frozen=True)
class RemediationSpec:
    identifier: str
    title: str
    safe_action: str
    verification: str
    automatic: bool


@dataclass(frozen=True)
class LifecycleSpec:
    state: str
    introduced_in: str
    deprecated_in: str | None
    replacement_code: str | None


@dataclass(frozen=True)
class SafeDetailFieldSpec:
    name: str
    type_name: str
    required: bool
    maximum_length: int | None
    maximum_value: int | None
    allowed_values: tuple[str, ...]
    privacy: str
    description: str


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    error_class: str
    severity: str
    retry_policy: str
    side_effect_certainty_policy: str
    remediation_id: str
    safe_detail_fields: tuple[SafeDetailFieldSpec, ...]
    lifecycle: LifecycleSpec
    owner: str
    technical_cause: str
    user_explanation: str
    privacy: str
    docs_path: str


@dataclass(frozen=True)
class ErrorCatalog:
    source_sha256: str
    remediations: tuple[RemediationSpec, ...]
    errors: tuple[ErrorSpec, ...]


def _exact(value: dict[str, Any], keys: set[str], label: str) -> None:
    missing = sorted(keys - value.keys())
    extra = sorted(value.keys() - keys)
    if missing:
        raise ModelError(f"{label}: missing fields {', '.join(missing)}")
    if extra:
        raise ModelError(f"{label}: unknown fields {', '.join(extra)}")


def _string(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ModelError(f"{label}: must contain 1 through {maximum} trimmed characters")
    if any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise ModelError(f"{label}: must contain printable ASCII only")
    return value


def _one_of(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ModelError(f"{label}: unsupported value")
    return value


def _identifier(value: Any, label: str) -> str:
    value = _string(value, label, 64)
    if not IDENTIFIER.fullmatch(value):
        raise ModelError(f"{label}: invalid identifier")
    return value


def _code(value: Any, label: str) -> str:
    value = _string(value, label, 32)
    if not CODE.fullmatch(value):
        raise ModelError(f"{label}: invalid error code")
    return value


def _nullable_code(value: Any, label: str) -> str | None:
    return None if value is None else _code(value, label)


def _version(value: Any, label: str) -> str:
    value = _string(value, label, 64)
    if not VERSION.fullmatch(value):
        raise ModelError(f"{label}: invalid version")
    return value


def _remediation(value: Any, index: int) -> RemediationSpec:
    if not isinstance(value, dict):
        raise ModelError(f"remediation {index}: must be an object")
    label = f"remediation {value.get('id', index)}"
    _exact(value, REMEDIATION_KEYS, label)
    automatic = value["automatic"]
    if not isinstance(automatic, bool):
        raise ModelError(f"{label}: automatic must be boolean")
    return RemediationSpec(
        identifier=_identifier(value["id"], f"{label} id"),
        title=_string(value["title"], f"{label} title", 80),
        safe_action=_string(value["safe_action"], f"{label} safe_action", 240),
        verification=_string(value["verification"], f"{label} verification", 160),
        automatic=automatic,
    )


def _lifecycle(value: Any, label: str) -> LifecycleSpec:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: must be an object")
    _exact(value, LIFECYCLE_KEYS, label)
    state = _one_of(value["state"], LIFECYCLE_STATES, f"{label} state")
    introduced = _version(value["introduced_in"], f"{label} introduced_in")
    deprecated_value = value["deprecated_in"]
    deprecated = None if deprecated_value is None else _version(deprecated_value, f"{label} deprecated_in")
    replacement = _nullable_code(value["replacement_code"], f"{label} replacement_code")
    if state == "active" and (deprecated is not None or replacement is not None):
        raise ModelError(f"{label}: active entries cannot declare deprecation or replacement")
    if state != "active" and deprecated is None:
        raise ModelError(f"{label}: deprecated and retired entries require deprecated_in")
    return LifecycleSpec(state, introduced, deprecated, replacement)


def _optional_bound(value: Any, label: str, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ModelError(f"{label}: invalid bound")
    return value


def _safe_detail_field(value: Any, error_code: str, index: int) -> SafeDetailFieldSpec:
    if not isinstance(value, dict):
        raise ModelError(f"{error_code} safe detail {index}: must be an object")
    label = f"{error_code} safe detail {value.get('name', index)}"
    _exact(value, DETAIL_KEYS, label)
    name = _string(value["name"], f"{label} name", 48)
    if not FIELD_NAME.fullmatch(name) or name in RESERVED_FIELD_NAMES:
        raise ModelError(f"{label}: invalid or reserved field name")
    type_name = _one_of(value["type"], DETAIL_TYPES, f"{label} type")
    required = value["required"]
    if not isinstance(required, bool):
        raise ModelError(f"{label}: required must be boolean")
    maximum_length = _optional_bound(value["maximum_length"], f"{label} maximum_length", MAX_SAFE_DETAIL_LENGTH)
    maximum_value_raw = value["maximum_value"]
    if type_name == "u64-decimal":
        if (
            not isinstance(maximum_value_raw, str)
            or len(maximum_value_raw) > 20
            or not maximum_value_raw.isascii()
            or not maximum_value_raw.isdecimal()
            or str(int(maximum_value_raw)) != maximum_value_raw
        ):
            raise ModelError(f"{label}: u64-decimal maximum_value must be a canonical decimal string")
        maximum_value = int(maximum_value_raw)
        if maximum_value > NUMERIC_MAXIMA["u64-decimal"]:
            raise ModelError(f"{label}: maximum_value exceeds u64")
    else:
        maximum_value = _optional_bound(
            maximum_value_raw,
            f"{label} maximum_value",
            NUMERIC_MAXIMA["u64-decimal"],
        )
    allowed_value = value["allowed_values"]
    if not isinstance(allowed_value, list) or len(allowed_value) > MAX_ENUM_VALUES:
        raise ModelError(f"{label}: allowed_values must contain at most {MAX_ENUM_VALUES} items")
    allowed_values = tuple(_identifier(item, f"{label} allowed value") for item in allowed_value)
    require_unique(list(allowed_values), f"{label} allowed value")
    if list(allowed_values) != sorted(allowed_values):
        raise ModelError(f"{label}: allowed_values must be sorted")

    if type_name == "boolean":
        valid_shape = maximum_length is None and maximum_value is None and not allowed_values
    elif type_name in NUMERIC_MAXIMA:
        valid_shape = (
            maximum_length is None
            and maximum_value is not None
            and maximum_value <= NUMERIC_MAXIMA[type_name]
            and not allowed_values
        )
    elif type_name in {"identifier", "text"}:
        valid_shape = maximum_length is not None and maximum_length > 0 and maximum_value is None and not allowed_values
    else:
        valid_shape = maximum_length is None and maximum_value is None and bool(allowed_values)
    if not valid_shape:
        raise ModelError(f"{label}: bounds do not match detail type {type_name}")

    return SafeDetailFieldSpec(
        name=name,
        type_name=type_name,
        required=required,
        maximum_length=maximum_length,
        maximum_value=maximum_value,
        allowed_values=allowed_values,
        privacy=_one_of(value["privacy"], PRIVACY_CLASSES, f"{label} privacy"),
        description=_string(value["description"], f"{label} description", 160),
    )


def _error(value: Any, index: int) -> ErrorSpec:
    if not isinstance(value, dict):
        raise ModelError(f"error {index}: must be an object")
    label = f"error {value.get('code', index)}"
    _exact(value, ERROR_KEYS, label)
    code = _code(value["code"], f"{label} code")
    error_class = _one_of(value["class"], ERROR_CLASSES, f"{label} class")
    match = CODE.fullmatch(code)
    if match is None or match.group(1) != error_class.upper():
        raise ModelError(f"{label}: code prefix must match class")
    details_value = value["safe_detail_fields"]
    if not isinstance(details_value, list) or len(details_value) > MAX_SAFE_DETAIL_FIELDS:
        raise ModelError(f"{label}: too many safe detail fields")
    details = tuple(_safe_detail_field(item, code, detail_index) for detail_index, item in enumerate(details_value))
    require_unique([item.name for item in details], f"{code} safe detail field")
    if [item.name for item in details] != sorted(item.name for item in details):
        raise ModelError(f"{label}: safe detail fields must be sorted by name")
    docs_path = _string(value["docs_path"], f"{label} docs_path", 128)
    expected_docs = f"docs/generated/error-catalog.md#{code.lower()}"
    if docs_path != expected_docs:
        raise ModelError(f"{label}: docs_path must be {expected_docs}")
    lifecycle = _lifecycle(value["lifecycle"], f"{label} lifecycle")
    if lifecycle.replacement_code == code:
        raise ModelError(f"{label}: replacement cannot refer to itself")
    retry_policy = _one_of(value["retry_policy"], RETRY_POLICIES, f"{label} retry_policy")
    side_effect_policy = _one_of(
        value["side_effect_certainty_policy"],
        SIDE_EFFECT_POLICIES,
        f"{label} side_effect_certainty_policy",
    )
    if retry_policy == "bounded-backoff" and side_effect_policy != "must-be-none":
        raise ModelError(f"{label}: bounded retry requires side effects to be absent")
    if side_effect_policy in {"possible", "partial"} and retry_policy != "outcome-lookup-only":
        raise ModelError(f"{label}: uncertain side effects forbid operation retry")
    return ErrorSpec(
        code=code,
        error_class=error_class,
        severity=_one_of(value["severity"], SEVERITIES, f"{label} severity"),
        retry_policy=retry_policy,
        side_effect_certainty_policy=side_effect_policy,
        remediation_id=_identifier(value["remediation_id"], f"{label} remediation_id"),
        safe_detail_fields=details,
        lifecycle=lifecycle,
        owner=_one_of(value["owner"], OWNERS, f"{label} owner"),
        technical_cause=_string(value["technical_cause"], f"{label} technical_cause", 240),
        user_explanation=_string(value["user_explanation"], f"{label} user_explanation", 240),
        privacy=_one_of(value["privacy"], PRIVACY_CLASSES, f"{label} privacy"),
        docs_path=docs_path,
    )


def load_error_catalog(root: Path) -> ErrorCatalog:
    path = root / "errors" / "catalog.json"
    value = load_json(path)
    _exact(value, CATALOG_KEYS, "error catalog")
    if value["schema"] != "hyperflux-error-catalog-v1":
        raise ModelError("unsupported error catalog schema")

    domain = load_json(root / "schemas" / "domain-catalog.json")
    domain_enums = {item["name"]: set(item["values"]) for item in domain.get("enums", [])}
    if domain_enums.get("ErrorSeverity") != SEVERITIES:
        raise ModelError("error severities must match the generated domain catalog")
    if not PRIVACY_CLASSES <= domain_enums.get("PrivacyClass", set()):
        raise ModelError("error privacy classes must be declared by the generated domain catalog")

    remediation_values = value["remediations"]
    if not isinstance(remediation_values, list) or not 1 <= len(remediation_values) <= MAX_REMEDIATIONS:
        raise ModelError(f"error catalog must contain 1 through {MAX_REMEDIATIONS} remediations")
    remediations = tuple(_remediation(item, index) for index, item in enumerate(remediation_values))
    remediation_ids = [item.identifier for item in remediations]
    require_unique(remediation_ids, "remediation id")
    if remediation_ids != sorted(remediation_ids):
        raise ModelError("remediations must be sorted by id")

    error_values = value["errors"]
    if not isinstance(error_values, list) or not 1 <= len(error_values) <= MAX_ERRORS:
        raise ModelError(f"error catalog must contain 1 through {MAX_ERRORS} errors")
    errors = tuple(_error(item, index) for index, item in enumerate(error_values))
    error_codes = [item.code for item in errors]
    require_unique(error_codes, "error code")
    if error_codes != sorted(error_codes):
        raise ModelError("errors must be sorted by code")

    known_remediations = set(remediation_ids)
    unknown_remediations = sorted({item.remediation_id for item in errors} - known_remediations)
    if unknown_remediations:
        raise ModelError(f"unknown remediation ids: {', '.join(unknown_remediations)}")
    unused_remediations = sorted(known_remediations - {item.remediation_id for item in errors})
    if unused_remediations:
        raise ModelError(f"unused remediation ids: {', '.join(unused_remediations)}")

    known_codes = set(error_codes)
    for item in errors:
        replacement = item.lifecycle.replacement_code
        if replacement is not None and replacement not in known_codes:
            raise ModelError(f"{item.code}: replacement code is not in the catalog")

    return ErrorCatalog(
        source_sha256=sha256_file(path),
        remediations=remediations,
        errors=errors,
    )
