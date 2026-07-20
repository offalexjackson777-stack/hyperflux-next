# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
import re

from .domain import HEADER_CPP, HEADER_PYTHON, HEADER_RUST
from ..errors import ErrorCatalog, ErrorSpec, SafeDetailFieldSpec


ERROR_CLASSES = (
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
)
RETRY_POLICIES = ("never", "bounded-backoff", "after-remediation", "outcome-lookup-only")
SIDE_EFFECT_POLICIES = ("not-applicable", "must-be-none", "runtime-reported", "possible", "partial")
LIFECYCLE_STATES = ("active", "deprecated", "retired")
OWNERS = ("kernel", "bridge", "sdk", "integration", "packaging", "tooling")
DETAIL_TYPES = ("boolean", "u16", "u32", "u64-decimal", "identifier", "text", "enum")


def _variant(value: str) -> str:
    return "".join(part[:1].upper() + part[1:].lower() for part in re.split(r"[^A-Za-z0-9]+", value) if part)


def _constant(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _rust_option(value: str | int | None, constructor: str | None = None) -> str:
    if value is None:
        return "None"
    rendered = _literal(value) if isinstance(value, str) else f"{value:_}"
    if constructor is not None:
        rendered = f"{constructor}::{_variant(str(value))}"
    return f"Some({rendered})"


def _rust_enum(lines: list[str], name: str, values: tuple[str, ...] | list[str]) -> None:
    lines.extend([f"wire_enum!({name} {{"])
    lines.extend(f"    {_variant(value)} => {_literal(value)}," for value in values)
    lines.extend(["});", ""])


def rust_catalog(catalog: ErrorCatalog) -> str:
    lines = [
        HEADER_RUST.rstrip(),
        "",
        "use hfx_domain::{ErrorSeverity, PrivacyClass};",
        "use serde::{Deserialize, Serialize};",
        "use std::fmt;",
        "use std::str::FromStr;",
        "",
        "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
        "pub struct CatalogValueError {",
        "    type_name: &'static str,",
        "}",
        "",
        "impl CatalogValueError {",
        "    const fn new(type_name: &'static str) -> Self {",
        "        Self { type_name }",
        "    }",
        "}",
        "",
        "impl fmt::Display for CatalogValueError {",
        "    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {",
        "        write!(formatter, \"unknown wire value for {}\", self.type_name)",
        "    }",
        "}",
        "",
        "impl std::error::Error for CatalogValueError {}",
        "",
        "macro_rules! wire_enum {",
        "    ($name:ident { $($variant:ident => $wire:literal,)+ }) => {",
        "        #[derive(",
        "            Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize,",
        "        )]",
        "        pub enum $name {",
        "            $(#[serde(rename = $wire)] $variant,)+",
        "        }",
        "",
        "        impl $name {",
        "            #[must_use]",
        "            pub const fn as_str(self) -> &'static str {",
        "                match self {",
        "                    $(Self::$variant => $wire,)+",
        "                }",
        "            }",
        "        }",
        "",
        "        impl FromStr for $name {",
        "            type Err = CatalogValueError;",
        "",
        "            fn from_str(value: &str) -> Result<Self, Self::Err> {",
        "                match value {",
        "                    $($wire => Ok(Self::$variant),)+",
        "                    _ => Err(CatalogValueError::new(stringify!($name))),",
        "                }",
        "            }",
        "        }",
        "",
        "        impl fmt::Display for $name {",
        "            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {",
        "                formatter.write_str(self.as_str())",
        "            }",
        "        }",
        "    };",
        "}",
        "",
    ]
    _rust_enum(lines, "ErrorCode", [item.code for item in catalog.errors])
    _rust_enum(lines, "RemediationId", [item.identifier for item in catalog.remediations])
    _rust_enum(lines, "ErrorClass", ERROR_CLASSES)
    _rust_enum(lines, "RetryPolicy", RETRY_POLICIES)
    _rust_enum(lines, "SideEffectCertaintyPolicy", SIDE_EFFECT_POLICIES)
    _rust_enum(lines, "ErrorLifecycleState", LIFECYCLE_STATES)
    _rust_enum(lines, "ErrorOwner", OWNERS)
    _rust_enum(lines, "SafeDetailKind", DETAIL_TYPES)
    lines.extend(
        [
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub struct RemediationDescriptor {",
            "    pub id: RemediationId,",
            "    pub title: &'static str,",
            "    pub safe_action: &'static str,",
            "    pub verification: &'static str,",
            "    pub automatic: bool,",
            "}",
            "",
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub struct LifecycleDescriptor {",
            "    pub state: ErrorLifecycleState,",
            "    pub introduced_in: &'static str,",
            "    pub deprecated_in: Option<&'static str>,",
            "    pub replacement_code: Option<ErrorCode>,",
            "}",
            "",
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub struct SafeDetailFieldDescriptor {",
            "    pub name: &'static str,",
            "    pub kind: SafeDetailKind,",
            "    pub required: bool,",
            "    pub maximum_length: Option<usize>,",
            "    pub maximum_value: Option<u64>,",
            "    pub allowed_values: &'static [&'static str],",
            "    pub privacy: PrivacyClass,",
            "    pub description: &'static str,",
            "}",
            "",
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub struct ErrorDescriptor {",
            "    pub code: ErrorCode,",
            "    pub error_class: ErrorClass,",
            "    pub severity: ErrorSeverity,",
            "    pub retry_policy: RetryPolicy,",
            "    pub side_effect_certainty_policy: SideEffectCertaintyPolicy,",
            "    pub remediation_id: RemediationId,",
            "    pub safe_detail_fields: &'static [SafeDetailFieldDescriptor],",
            "    pub lifecycle: LifecycleDescriptor,",
            "    pub owner: ErrorOwner,",
            "    pub technical_cause: &'static str,",
            "    pub user_explanation: &'static str,",
            "    pub privacy: PrivacyClass,",
            "    pub docs_path: &'static str,",
            "}",
            "",
            "pub const ERROR_CATALOG_SHA256: &str =",
            f"    {_literal(catalog.source_sha256)};",
            "pub const MAX_ERROR_COUNT: usize = 256;",
            "pub const MAX_REMEDIATION_COUNT: usize = 128;",
            "pub const MAX_SAFE_DETAIL_FIELDS: usize = 12;",
            "pub const MAX_SAFE_DETAIL_LENGTH: usize = 256;",
            "",
            "pub const REMEDIATIONS: &[RemediationDescriptor] = &[",
        ]
    )
    for item in catalog.remediations:
        lines.extend(
            [
                "    RemediationDescriptor {",
                f"        id: RemediationId::{_variant(item.identifier)},",
                f"        title: {_literal(item.title)},",
                f"        safe_action: {_literal(item.safe_action)},",
                f"        verification: {_literal(item.verification)},",
                f"        automatic: {str(item.automatic).lower()},",
                "    },",
            ]
        )
    lines.extend(["];"])
    for error_index, error in enumerate(catalog.errors):
        for field_index, field in enumerate(error.safe_detail_fields):
            if field.allowed_values:
                values = ", ".join(_literal(value) for value in field.allowed_values)
                declaration = f"const DETAIL_VALUES_{error_index}_{field_index}: &[&str] = &[{values}];"
                lines.append("")
                if len(declaration) <= 100:
                    lines.append(declaration)
                else:
                    lines.extend(
                        [
                            f"const DETAIL_VALUES_{error_index}_{field_index}: &[&str] = &[",
                            *(f"    {_literal(value)}," for value in field.allowed_values),
                            "];",
                        ]
                    )
        single_detail = len(error.safe_detail_fields) == 1
        opening = (
            f"const DETAILS_{error_index}: &[SafeDetailFieldDescriptor] = &[SafeDetailFieldDescriptor {{"
            if single_detail
            else f"const DETAILS_{error_index}: &[SafeDetailFieldDescriptor] = &["
        )
        lines.extend(["", opening])
        for field_index, field in enumerate(error.safe_detail_fields):
            allowed = f"DETAIL_VALUES_{error_index}_{field_index}" if field.allowed_values else "&[]"
            if not single_detail:
                lines.append("    SafeDetailFieldDescriptor {")
            indent = "    " if single_detail else "        "
            lines.extend(
                [
                    f"{indent}name: {_literal(field.name)},",
                    f"{indent}kind: SafeDetailKind::{_variant(field.type_name)},",
                    f"{indent}required: {str(field.required).lower()},",
                    f"{indent}maximum_length: {_rust_option(field.maximum_length)},",
                    f"{indent}maximum_value: {_rust_option(field.maximum_value)},",
                    f"{indent}allowed_values: {allowed},",
                    f"{indent}privacy: PrivacyClass::{_variant(field.privacy)},",
                    f"{indent}description: {_literal(field.description)},",
                ]
            )
            if not single_detail:
                lines.append("    },")
        lines.append("}];" if single_detail else "];")
    lines.extend(["", "pub const ERRORS: &[ErrorDescriptor] = &["])
    for index, item in enumerate(catalog.errors):
        replacement = _rust_option(item.lifecycle.replacement_code, "ErrorCode")
        lines.extend(
            [
                "    ErrorDescriptor {",
                f"        code: ErrorCode::{_variant(item.code)},",
                f"        error_class: ErrorClass::{_variant(item.error_class)},",
                f"        severity: ErrorSeverity::{_variant(item.severity)},",
                f"        retry_policy: RetryPolicy::{_variant(item.retry_policy)},",
                "        side_effect_certainty_policy: "
                f"SideEffectCertaintyPolicy::{_variant(item.side_effect_certainty_policy)},",
                f"        remediation_id: RemediationId::{_variant(item.remediation_id)},",
                f"        safe_detail_fields: DETAILS_{index},",
                "        lifecycle: LifecycleDescriptor {",
                f"            state: ErrorLifecycleState::{_variant(item.lifecycle.state)},",
                f"            introduced_in: {_literal(item.lifecycle.introduced_in)},",
                f"            deprecated_in: {_rust_option(item.lifecycle.deprecated_in)},",
                f"            replacement_code: {replacement},",
                "        },",
                f"        owner: ErrorOwner::{_variant(item.owner)},",
                f"        technical_cause: {_literal(item.technical_cause)},",
                f"        user_explanation: {_literal(item.user_explanation)},",
                f"        privacy: PrivacyClass::{_variant(item.privacy)},",
                f"        docs_path: {_literal(item.docs_path)},",
                "    },",
            ]
        )
    lines.extend(
        [
            "];",
            "",
            "#[must_use]",
            "pub fn error_by_code(code: ErrorCode) -> &'static ErrorDescriptor {",
            "    ERRORS",
            "        .iter()",
            "        .find(|descriptor| descriptor.code == code)",
            "        .unwrap_or_else(|| unreachable!(\"generated ErrorCode has no descriptor\"))",
            "}",
            "",
            "#[must_use]",
            "pub fn remediation_by_id(id: RemediationId) -> &'static RemediationDescriptor {",
            "    REMEDIATIONS",
            "        .iter()",
            "        .find(|descriptor| descriptor.id == id)",
            "        .unwrap_or_else(|| unreachable!(\"generated RemediationId has no descriptor\"))",
            "}",
        ]
    )
    return "\n".join(lines) + "\n"


def _cpp_enum(lines: list[str], name: str, values: tuple[str, ...] | list[str]) -> None:
    lines.extend([f"enum class {name}", "{"])
    lines.extend(f"    {_variant(value)}," for value in values)
    lines.extend(["};", "", f"[[nodiscard]] constexpr std::string_view to_string({name} value)", "{", "    switch(value)", "    {"])
    lines.extend(f"        case {name}::{_variant(value)}: return {_literal(value)};" for value in values)
    lines.extend(["    }", '    return "unknown";', "}", ""])


def _cpp_optional(value: str | int | None, enum_name: str | None = None) -> str:
    if value is None:
        return "std::nullopt"
    if enum_name is not None:
        return f"{enum_name}::{_variant(str(value))}"
    return _literal(value) if isinstance(value, str) else f"{value}ULL"


def cpp_catalog(catalog: ErrorCatalog) -> str:
    lines = [
        HEADER_CPP.rstrip(),
        "",
        "#pragma once",
        "",
        "#include <array>",
        "#include <cstddef>",
        "#include <cstdint>",
        "#include <optional>",
        "#include <span>",
        "#include <string_view>",
        "",
        "#include <hyperflux/generated/domain_types.hpp>",
        "",
        "namespace hyperflux::errors",
        "{",
        "",
    ]
    _cpp_enum(lines, "ErrorCode", [item.code for item in catalog.errors])
    _cpp_enum(lines, "RemediationId", [item.identifier for item in catalog.remediations])
    _cpp_enum(lines, "ErrorClass", ERROR_CLASSES)
    _cpp_enum(lines, "RetryPolicy", RETRY_POLICIES)
    _cpp_enum(lines, "SideEffectCertaintyPolicy", SIDE_EFFECT_POLICIES)
    _cpp_enum(lines, "ErrorLifecycleState", LIFECYCLE_STATES)
    _cpp_enum(lines, "ErrorOwner", OWNERS)
    _cpp_enum(lines, "SafeDetailKind", DETAIL_TYPES)
    lines.extend(
        [
            "struct RemediationDescriptor",
            "{",
            "    RemediationId id;",
            "    std::string_view title;",
            "    std::string_view safe_action;",
            "    std::string_view verification;",
            "    bool automatic;",
            "};",
            "",
            "struct LifecycleDescriptor",
            "{",
            "    ErrorLifecycleState state;",
            "    std::string_view introduced_in;",
            "    std::optional<std::string_view> deprecated_in;",
            "    std::optional<ErrorCode> replacement_code;",
            "};",
            "",
            "struct SafeDetailFieldDescriptor",
            "{",
            "    std::string_view name;",
            "    SafeDetailKind kind;",
            "    bool required;",
            "    std::optional<std::size_t> maximum_length;",
            "    std::optional<std::uint64_t> maximum_value;",
            "    std::span<const std::string_view> allowed_values;",
            "    PrivacyClass privacy;",
            "    std::string_view description;",
            "};",
            "",
            "struct ErrorDescriptor",
            "{",
            "    ErrorCode code;",
            "    ErrorClass error_class;",
            "    ErrorSeverity severity;",
            "    RetryPolicy retry_policy;",
            "    SideEffectCertaintyPolicy side_effect_certainty_policy;",
            "    RemediationId remediation_id;",
            "    std::span<const SafeDetailFieldDescriptor> safe_detail_fields;",
            "    LifecycleDescriptor lifecycle;",
            "    ErrorOwner owner;",
            "    std::string_view technical_cause;",
            "    std::string_view user_explanation;",
            "    PrivacyClass privacy;",
            "    std::string_view docs_path;",
            "};",
            "",
            f"inline constexpr std::string_view error_catalog_sha256 = {_literal(catalog.source_sha256)};",
            "inline constexpr std::size_t max_error_count = 256;",
            "inline constexpr std::size_t max_remediation_count = 128;",
            "inline constexpr std::size_t max_safe_detail_fields = 12;",
            "inline constexpr std::size_t max_safe_detail_length = 256;",
            "",
            f"inline constexpr std::array<RemediationDescriptor, {len(catalog.remediations)}> remediations {{{{",
        ]
    )
    for item in catalog.remediations:
        lines.append(
            "    {"
            f"RemediationId::{_variant(item.identifier)}, {_literal(item.title)}, "
            f"{_literal(item.safe_action)}, {_literal(item.verification)}, {str(item.automatic).lower()}"
            "},"
        )
    lines.extend(["}};", ""])
    for error_index, error in enumerate(catalog.errors):
        for field_index, field in enumerate(error.safe_detail_fields):
            if field.allowed_values:
                values = ", ".join(_literal(value) for value in field.allowed_values)
                lines.extend(
                    [
                        f"inline constexpr std::array<std::string_view, {len(field.allowed_values)}> "
                        f"detail_values_{error_index}_{field_index} {{{{{values}}}}};",
                        "",
                    ]
                )
        lines.append(
            f"inline constexpr std::array<SafeDetailFieldDescriptor, {len(error.safe_detail_fields)}> "
            f"details_{error_index} {{{{"
        )
        for field_index, field in enumerate(error.safe_detail_fields):
            allowed = f"detail_values_{error_index}_{field_index}" if field.allowed_values else "std::span<const std::string_view>{}"
            lines.append(
                "    {"
                f"{_literal(field.name)}, SafeDetailKind::{_variant(field.type_name)}, "
                f"{str(field.required).lower()}, {_cpp_optional(field.maximum_length)}, "
                f"{_cpp_optional(field.maximum_value)}, {allowed}, PrivacyClass::{_variant(field.privacy)}, "
                f"{_literal(field.description)}"
                "},"
            )
        lines.extend(["}};", ""])
    lines.append(f"inline constexpr std::array<ErrorDescriptor, {len(catalog.errors)}> errors {{{{")
    for index, item in enumerate(catalog.errors):
        deprecated = _cpp_optional(item.lifecycle.deprecated_in)
        replacement = _cpp_optional(item.lifecycle.replacement_code, "ErrorCode")
        lines.extend(
            [
                "    {",
                f"        ErrorCode::{_variant(item.code)}, ErrorClass::{_variant(item.error_class)},",
                f"        ErrorSeverity::{_variant(item.severity)}, RetryPolicy::{_variant(item.retry_policy)},",
                f"        SideEffectCertaintyPolicy::{_variant(item.side_effect_certainty_policy)},",
                f"        RemediationId::{_variant(item.remediation_id)}, details_{index},",
                "        {"
                f"ErrorLifecycleState::{_variant(item.lifecycle.state)}, "
                f"{_literal(item.lifecycle.introduced_in)}, {deprecated}, {replacement}"
                "},",
                f"        ErrorOwner::{_variant(item.owner)}, {_literal(item.technical_cause)},",
                f"        {_literal(item.user_explanation)}, PrivacyClass::{_variant(item.privacy)},",
                f"        {_literal(item.docs_path)}",
                "    },",
            ]
        )
    lines.extend(
        [
            "}};",
            "",
            "[[nodiscard]] constexpr const ErrorDescriptor* error_by_code(ErrorCode code)",
            "{",
            "    for(const auto& descriptor : errors)",
            "    {",
            "        if(descriptor.code == code)",
            "        {",
            "            return &descriptor;",
            "        }",
            "    }",
            "    return nullptr;",
            "}",
            "",
            "[[nodiscard]] constexpr const RemediationDescriptor* remediation_by_id(RemediationId id)",
            "{",
            "    for(const auto& descriptor : remediations)",
            "    {",
            "        if(descriptor.id == id)",
            "        {",
            "            return &descriptor;",
            "        }",
            "    }",
            "    return nullptr;",
            "}",
            "",
            "} // namespace hyperflux::errors",
            "",
        ]
    )
    return "\n".join(lines)


def _python_enum(lines: list[str], name: str, values: tuple[str, ...] | list[str]) -> None:
    lines.extend([f"class {name}(str, Enum):"])
    lines.extend(f"    {_constant(value)} = {_literal(value)}" for value in values)
    lines.extend(["", ""])


def _python_optional(value: str | int | None, enum_name: str | None = None) -> str:
    if value is None:
        return "None"
    if enum_name is not None:
        return f"{enum_name}.{_constant(str(value))}"
    return repr(value)


def python_catalog(catalog: ErrorCatalog) -> str:
    lines = [
        HEADER_PYTHON.rstrip(),
        "",
        "from collections.abc import Mapping",
        "from dataclasses import dataclass",
        "from enum import Enum",
        "from types import MappingProxyType",
        "from typing import Final",
        "",
        "from .domain_types import ErrorSeverity, PrivacyClass",
        "",
    ]
    _python_enum(lines, "ErrorCode", [item.code for item in catalog.errors])
    _python_enum(lines, "RemediationId", [item.identifier for item in catalog.remediations])
    _python_enum(lines, "ErrorClass", ERROR_CLASSES)
    _python_enum(lines, "RetryPolicy", RETRY_POLICIES)
    _python_enum(lines, "SideEffectCertaintyPolicy", SIDE_EFFECT_POLICIES)
    _python_enum(lines, "ErrorLifecycleState", LIFECYCLE_STATES)
    _python_enum(lines, "ErrorOwner", OWNERS)
    _python_enum(lines, "SafeDetailKind", DETAIL_TYPES)
    lines.extend(
        [
            "@dataclass(frozen=True, slots=True)",
            "class RemediationDescriptor:",
            "    id: RemediationId",
            "    title: str",
            "    safe_action: str",
            "    verification: str",
            "    automatic: bool",
            "",
            "",
            "@dataclass(frozen=True, slots=True)",
            "class LifecycleDescriptor:",
            "    state: ErrorLifecycleState",
            "    introduced_in: str",
            "    deprecated_in: str | None",
            "    replacement_code: ErrorCode | None",
            "",
            "",
            "@dataclass(frozen=True, slots=True)",
            "class SafeDetailFieldDescriptor:",
            "    name: str",
            "    kind: SafeDetailKind",
            "    required: bool",
            "    maximum_length: int | None",
            "    maximum_value: int | None",
            "    allowed_values: tuple[str, ...]",
            "    privacy: PrivacyClass",
            "    description: str",
            "",
            "",
            "@dataclass(frozen=True, slots=True)",
            "class ErrorDescriptor:",
            "    code: ErrorCode",
            "    error_class: ErrorClass",
            "    severity: ErrorSeverity",
            "    retry_policy: RetryPolicy",
            "    side_effect_certainty_policy: SideEffectCertaintyPolicy",
            "    remediation_id: RemediationId",
            "    safe_detail_fields: tuple[SafeDetailFieldDescriptor, ...]",
            "    lifecycle: LifecycleDescriptor",
            "    owner: ErrorOwner",
            "    technical_cause: str",
            "    user_explanation: str",
            "    privacy: PrivacyClass",
            "    docs_path: str",
            "",
            "",
            f"ERROR_CATALOG_SHA256: Final = {_literal(catalog.source_sha256)}",
            "MAX_ERROR_COUNT: Final = 256",
            "MAX_REMEDIATION_COUNT: Final = 128",
            "MAX_SAFE_DETAIL_FIELDS: Final = 12",
            "MAX_SAFE_DETAIL_LENGTH: Final = 256",
            "",
            "REMEDIATIONS: Final = (",
        ]
    )
    for item in catalog.remediations:
        lines.extend(
            [
                "    RemediationDescriptor(",
                f"        id=RemediationId.{_constant(item.identifier)},",
                f"        title={_literal(item.title)},",
                f"        safe_action={_literal(item.safe_action)},",
                f"        verification={_literal(item.verification)},",
                f"        automatic={item.automatic},",
                "    ),",
            ]
        )
    lines.extend([")", ""])
    for index, item in enumerate(catalog.errors):
        lines.extend([f"DETAILS_{index}: Final = ("])
        for field in item.safe_detail_fields:
            values = repr(tuple(field.allowed_values))
            lines.extend(
                [
                    "    SafeDetailFieldDescriptor(",
                    f"        name={_literal(field.name)},",
                    f"        kind=SafeDetailKind.{_constant(field.type_name)},",
                    f"        required={field.required},",
                    f"        maximum_length={field.maximum_length!r},",
                    f"        maximum_value={field.maximum_value!r},",
                    f"        allowed_values={values},",
                    f"        privacy=PrivacyClass.{_constant(field.privacy)},",
                    f"        description={_literal(field.description)},",
                    "    ),",
                ]
            )
        lines.extend([")", ""])
    lines.extend(["ERRORS: Final = ("])
    for index, item in enumerate(catalog.errors):
        lines.extend(
            [
                "    ErrorDescriptor(",
                f"        code=ErrorCode.{_constant(item.code)},",
                f"        error_class=ErrorClass.{_constant(item.error_class)},",
                f"        severity=ErrorSeverity.{_constant(item.severity)},",
                f"        retry_policy=RetryPolicy.{_constant(item.retry_policy)},",
                "        side_effect_certainty_policy="
                f"SideEffectCertaintyPolicy.{_constant(item.side_effect_certainty_policy)},",
                f"        remediation_id=RemediationId.{_constant(item.remediation_id)},",
                f"        safe_detail_fields=DETAILS_{index},",
                "        lifecycle=LifecycleDescriptor(",
                f"            state=ErrorLifecycleState.{_constant(item.lifecycle.state)},",
                f"            introduced_in={_literal(item.lifecycle.introduced_in)},",
                f"            deprecated_in={item.lifecycle.deprecated_in!r},",
                "            replacement_code="
                f"{_python_optional(item.lifecycle.replacement_code, 'ErrorCode')},",
                "        ),",
                f"        owner=ErrorOwner.{_constant(item.owner)},",
                f"        technical_cause={_literal(item.technical_cause)},",
                f"        user_explanation={_literal(item.user_explanation)},",
                f"        privacy=PrivacyClass.{_constant(item.privacy)},",
                f"        docs_path={_literal(item.docs_path)},",
                "    ),",
            ]
        )
    lines.extend(
        [
            ")",
            "",
            "ERRORS_BY_CODE: Final = MappingProxyType({descriptor.code: descriptor for descriptor in ERRORS})",
            "REMEDIATIONS_BY_ID: Final = MappingProxyType({descriptor.id: descriptor for descriptor in REMEDIATIONS})",
            "",
            "",
            "def _is_safe_identifier(value: str) -> bool:",
            "    return bool(value) and value[0].isascii() and value[0].isalnum() and all(",
            "        character.isascii() and (character.isalnum() or character in '._:-')",
            "        for character in value",
            "    )",
            "",
            "",
            "def validate_safe_details(code: ErrorCode | str, details: Mapping[str, object]) -> None:",
            "    code = ErrorCode(code)",
            "    descriptor = ERRORS_BY_CODE[code]",
            "    if len(details) > MAX_SAFE_DETAIL_FIELDS:",
            "        raise ValueError('too many safe detail fields')",
            "    fields = {field.name: field for field in descriptor.safe_detail_fields}",
            "    unknown = sorted(set(details) - set(fields))",
            "    if unknown:",
            "        raise ValueError(f\"unknown safe detail fields: {', '.join(unknown)}\")",
            "    missing = sorted(field.name for field in fields.values() if field.required and field.name not in details)",
            "    if missing:",
            "        raise ValueError(f\"missing safe detail fields: {', '.join(missing)}\")",
            "    for name, value in details.items():",
            "        field = fields[name]",
            "        if field.kind is SafeDetailKind.BOOLEAN:",
            "            valid = isinstance(value, bool)",
            "        elif field.kind in {SafeDetailKind.U16, SafeDetailKind.U32}:",
            "            valid = (",
            "                isinstance(value, int)",
            "                and not isinstance(value, bool)",
            "                and 0 <= value <= (field.maximum_value or 0)",
            "            )",
            "        elif field.kind is SafeDetailKind.U64_DECIMAL:",
            "            valid = (",
            "                isinstance(value, str)",
            "                and len(value) <= 20",
            "                and value.isascii()",
            "                and value.isdecimal()",
            "                and str(int(value)) == value",
            "                and int(value) <= (field.maximum_value or 0)",
            "            )",
            "        elif field.kind is SafeDetailKind.IDENTIFIER:",
            "            valid = (",
            "                isinstance(value, str)",
            "                and len(value) <= (field.maximum_length or 0)",
            "                and _is_safe_identifier(value)",
            "            )",
            "        elif field.kind is SafeDetailKind.TEXT:",
            "            valid = (",
            "                isinstance(value, str)",
            "                and 0 < len(value) <= (field.maximum_length or 0)",
            "                and all(32 <= ord(character) <= 126 for character in value)",
            "            )",
            "        else:",
            "            valid = isinstance(value, str) and value in field.allowed_values",
            "        if not valid:",
            "            raise ValueError(f\"invalid safe detail field: {name}\")",
            "",
            "",
            "__all__ = [",
            '    "ERRORS",',
            '    "ERRORS_BY_CODE",',
            '    "ERROR_CATALOG_SHA256",',
            '    "MAX_ERROR_COUNT",',
            '    "MAX_REMEDIATION_COUNT",',
            '    "MAX_SAFE_DETAIL_FIELDS",',
            '    "MAX_SAFE_DETAIL_LENGTH",',
            '    "REMEDIATIONS",',
            '    "REMEDIATIONS_BY_ID",',
            '    "ErrorClass",',
            '    "ErrorCode",',
            '    "ErrorDescriptor",',
            '    "ErrorLifecycleState",',
            '    "ErrorOwner",',
            '    "ErrorSeverity",',
            '    "LifecycleDescriptor",',
            '    "RemediationDescriptor",',
            '    "RemediationId",',
            '    "RetryPolicy",',
            '    "PrivacyClass",',
            '    "SafeDetailFieldDescriptor",',
            '    "SafeDetailKind",',
            '    "SideEffectCertaintyPolicy",',
            '    "validate_safe_details",',
            "]",
            "",
        ]
    )
    return "\n".join(lines)


def _detail_bound(field: SafeDetailFieldSpec) -> str:
    if field.maximum_length is not None:
        return f"max {field.maximum_length} characters"
    if field.maximum_value is not None:
        encoding = "canonical decimal string" if field.type_name == "u64-decimal" else "JSON number"
        return f"0 through {field.maximum_value} ({encoding})"
    if field.allowed_values:
        return ", ".join(f"`{value}`" for value in field.allowed_values)
    return "boolean"


def markdown(catalog: ErrorCatalog) -> str:
    lines = [
        "# Error Catalog",
        "",
        "> Generated by `./hfx generate`. Do not edit manually.",
        "",
        f"Catalog source: `{catalog.source_sha256}`",
        "",
        "This catalog is the single authority for Doctor, services, SDKs, integrations, package hooks, and support bundles. Safe details are an allowlist: raw payloads, serials, host identifiers, private paths, and arbitrary exception or log text are not accepted.",
        "",
        "## Retry Semantics",
        "",
        "| Policy | Meaning |",
        "| --- | --- |",
        "| `never` | The rejected request is terminal. Correct the cause and create a new request where directed. |",
        "| `bounded-backoff` | Retry is allowed only because transport side effects are proven absent, within the original bound. |",
        "| `after-remediation` | Do not retry until the referenced remediation has completed and been verified. |",
        "| `outcome-lookup-only` | Hardware transport may have started. Look up the original transaction; never resend it. |",
        "",
        "## Side-Effect Policies",
        "",
        "| Policy | Meaning |",
        "| --- | --- |",
        "| `not-applicable` | The finding does not describe a hardware-write attempt. |",
        "| `must-be-none` | The error is valid only when no hardware frame was dispatched. |",
        "| `runtime-reported` | The terminal outcome must carry the certainty established at runtime. |",
        "| `possible` | A hardware effect may have occurred; automatic operation retry is forbidden. |",
        "| `partial` | At least one frame was delivered; automatic operation retry is forbidden. |",
        "",
        "## Index",
        "",
        "| Code | Class | Severity | Retry | Side effects | Owner | Lifecycle |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in catalog.errors:
        lines.append(
            f"| [`{item.code}`](#{item.code.lower()}) | `{item.error_class}` | `{item.severity}` | "
            f"`{item.retry_policy}` | `{item.side_effect_certainty_policy}` | `{item.owner}` | "
            f"`{item.lifecycle.state}` |"
        )
    lines.extend(["", "## Remediations", "", "| ID | Action | Verification | Automatic |", "| --- | --- | --- | --- |"])
    for item in catalog.remediations:
        lines.append(
            f"| `{item.identifier}` | {item.safe_action} | {item.verification} | "
            f"{str(item.automatic).lower()} |"
        )
    remediation_index = {item.identifier: item for item in catalog.remediations}
    for item in catalog.errors:
        remediation = remediation_index[item.remediation_id]
        lines.extend(
            [
                "",
                f"## {item.code}",
                "",
                item.user_explanation,
                "",
                f"**Technical cause:** {item.technical_cause}",
                "",
                f"**Class / severity:** `{item.error_class}` / `{item.severity}`",
                "",
                f"**Retry / side effects:** `{item.retry_policy}` / `{item.side_effect_certainty_policy}`",
                "",
                f"**Owner / privacy:** `{item.owner}` / `{item.privacy}`",
                "",
                f"**Lifecycle:** `{item.lifecycle.state}` since `{item.lifecycle.introduced_in}`",
                "",
                f"**Safe action (`{remediation.identifier}`):** {remediation.safe_action}",
                "",
                f"**Verify:** {remediation.verification}",
                "",
                "### Safe Details",
                "",
            ]
        )
        if not item.safe_detail_fields:
            lines.extend(["No details are accepted for this finding.", ""])
            continue
        lines.extend(["| Field | Type | Required | Bound | Privacy |", "| --- | --- | --- | --- | --- |"])
        for field in item.safe_detail_fields:
            lines.append(
                f"| `{field.name}` | `{field.type_name}` | {str(field.required).lower()} | "
                f"{_detail_bound(field)} | `{field.privacy}` |"
            )
    return "\n".join(lines) + "\n"
