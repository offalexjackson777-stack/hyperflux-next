# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from .domain import HEADER_CPP, HEADER_PYTHON, HEADER_RUST
from ..protocol import FieldSpec, ProtocolCatalog


def _rust_type(field: FieldSpec) -> str:
    base = "bool" if field.type_name == "Boolean" else field.type_name
    if field.many:
        return f"Vec<{base}>"
    if not field.required:
        return f"Option<{base}>"
    return base


def _cpp_type(field: FieldSpec) -> str:
    base = "bool" if field.type_name == "Boolean" else field.type_name
    if field.many:
        return f"std::vector<{base}>"
    if not field.required:
        return f"std::optional<{base}>"
    return base


def _python_type(field: FieldSpec) -> str:
    base = "bool" if field.type_name == "Boolean" else field.type_name
    if field.many:
        return f"tuple[{base}, ...]"
    if not field.required:
        return f"{base} | None"
    return base


def _rust_domain_imports(names: list[str]) -> list[str]:
    lines = ["use hfx_domain::{"]
    current = "    "
    for name in names:
        addition = f"{name}, "
        if len(current) + len(addition) > 100:
            lines.append(current.rstrip())
            current = "    "
        current += addition
    lines.append(current.rstrip())
    lines.append("};")
    return lines


def rust_types(catalog: ProtocolCatalog) -> str:
    record_names = {record.name for record in catalog.records}
    domain_names = sorted(
        {
            field.type_name
            for record in catalog.records
            for field in record.fields
            if field.type_name != "Boolean" and field.type_name not in record_names
        }
    )
    lines = [HEADER_RUST.rstrip(), ""]
    lines.extend(_rust_domain_imports(domain_names))
    lines.extend([
        "use serde::{Deserialize, Serialize};",
        "",
        f"pub const MINIMUM_PROTOCOL_VERSION: u16 = {catalog.minimum_version};",
        f"pub const MAXIMUM_PROTOCOL_VERSION: u16 = {catalog.maximum_version};",
        "pub const SUPPORTED_FEATURES: &[&str] = &[",
    ])
    lines.extend(f'    "{feature}",' for feature in catalog.features)
    lines.extend(["];"])
    for record in catalog.records:
        lines.extend([
            "",
            f"/// {record.description}",
            "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
            "#[serde(deny_unknown_fields)]",
        ])
        if not record.fields:
            lines.append(f"pub struct {record.name} {{}}")
            continue
        lines.append(f"pub struct {record.name} {{")
        for field in record.fields:
            lines.extend([f"    /// {field.description}", f"    pub {field.name}: {_rust_type(field)},"])
        lines.append("}")
    lines.extend(["", "#[derive(Clone, Copy, Debug, Eq, PartialEq)]", "pub struct MethodDescriptor {", "    pub name: &'static str,", "    pub request: &'static str,", "    pub response: &'static str,", "    pub required_feature: Option<&'static str>,", "}", "", "pub const METHODS: &[MethodDescriptor] = &["])
    for method in catalog.methods:
        feature = "None" if method.required_feature is None else f'Some("{method.required_feature}")'
        lines.extend(
            [
                "    MethodDescriptor {",
                f'        name: "{method.name}",',
                f'        request: "{method.request}",',
                f'        response: "{method.response}",',
                f"        required_feature: {feature},",
                "    },",
            ]
        )
    lines.extend(["];"])
    return "\n".join(lines) + "\n"


def cpp_types(catalog: ProtocolCatalog) -> str:
    lines = [
        HEADER_CPP.rstrip(),
        "",
        "#pragma once",
        "",
        '#include "domain_types.hpp"',
        "",
        "#include <cstdint>",
        "#include <optional>",
        "#include <string_view>",
        "#include <vector>",
        "",
        "namespace hyperflux",
        "{",
        "",
        f"inline constexpr std::uint16_t minimum_protocol_version = {catalog.minimum_version};",
        f"inline constexpr std::uint16_t maximum_protocol_version = {catalog.maximum_version};",
    ]
    for record in catalog.records:
        lines.extend(["", f"// {record.description}", f"struct {record.name}", "{"])
        for field in record.fields:
            lines.append(f"    {_cpp_type(field)} {field.name};")
        lines.append(f"    friend bool operator==(const {record.name}&, const {record.name}&) = default;")
        lines.append("};")
    lines.extend(["", "struct MethodDescriptor", "{", "    std::string_view name;", "    std::string_view request;", "    std::string_view response;", "    std::optional<std::string_view> required_feature;", "};", "", f"inline constexpr MethodDescriptor methods[{len(catalog.methods)}] = {{"])
    for method in catalog.methods:
        feature = "std::nullopt" if method.required_feature is None else f'std::optional<std::string_view>("{method.required_feature}")'
        lines.append(
            f'    {{"{method.name}", "{method.request}", "{method.response}", {feature}}},'
        )
    lines.extend(["};", "", "} // namespace hyperflux", ""])
    return "\n".join(lines)


def python_types(catalog: ProtocolCatalog) -> str:
    domain_names = sorted(
        {
            field.type_name
            for record in catalog.records
            for field in record.fields
            if field.type_name != "Boolean"
            and field.type_name not in {candidate.name for candidate in catalog.records}
        }
    )
    lines = [HEADER_PYTHON.rstrip(), "", "from dataclasses import dataclass", "", "from .domain_types import ("]
    lines.extend(f"    {name}," for name in domain_names)
    lines.extend([")", "", f"MINIMUM_PROTOCOL_VERSION = {catalog.minimum_version}", f"MAXIMUM_PROTOCOL_VERSION = {catalog.maximum_version}", "SUPPORTED_FEATURES = ("])
    lines.extend(f'    "{feature}",' for feature in catalog.features)
    lines.extend([")", ""])
    exports: list[str] = []
    for record in catalog.records:
        exports.append(record.name)
        lines.extend(["@dataclass(frozen=True, slots=True)", f"class {record.name}:", f'    """{record.description}"""'])
        if not record.fields:
            lines.append("    pass")
        else:
            for field in record.fields:
                lines.append(f"    {field.name}: {_python_type(field)}")
        lines.extend(["", ""])
    lines.extend(["METHODS = ("])
    for method in catalog.methods:
        feature = "None" if method.required_feature is None else f'"{method.required_feature}"'
        lines.append(
            f'    ("{method.name}", "{method.request}", "{method.response}", {feature}),' 
        )
    lines.extend([")", "", "__all__ = [", '    "MAXIMUM_PROTOCOL_VERSION",', '    "METHODS",', '    "MINIMUM_PROTOCOL_VERSION",', '    "SUPPORTED_FEATURES",'])
    lines.extend(f'    "{name}",' for name in exports)
    lines.extend(["]", ""])
    return "\n".join(lines)


def markdown(catalog: ProtocolCatalog) -> str:
    lines = [
        "# Bridge Protocol",
        "",
        "> Generated by `./hfx generate`. Do not edit manually.",
        "",
        f"Supported protocol versions: **{catalog.minimum_version} through {catalog.maximum_version}**.",
        "",
        "## Negotiated Features",
        "",
    ]
    lines.extend(f"- `{feature}`" for feature in catalog.features)
    lines.extend(["", "## Methods", "", "| Method | Request | Response | Feature |", "| --- | --- | --- | --- |"])
    for method in catalog.methods:
        feature = f"`{method.required_feature}`" if method.required_feature else "base"
        lines.append(f"| `{method.name}` | `{method.request}` | `{method.response}` | {feature} |")
    lines.extend(["", "## Records", ""])
    for record in catalog.records:
        lines.extend([f"### {record.name}", "", record.description, ""])
        if not record.fields:
            lines.extend(["This record has no fields.", ""])
            continue
        lines.extend(["| Field | Type | Required | Bound |", "| --- | --- | --- | --- |"])
        for field in record.fields:
            bound = f"max {field.max_items}" if field.many else "scalar"
            lines.append(
                f"| `{field.name}` | `{field.type_name}` | `{str(field.required).lower()}` | {bound} |"
            )
        lines.append("")
    return "\n".join(lines)
