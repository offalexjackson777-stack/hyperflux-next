# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from .domain import HEADER_CPP, HEADER_PYTHON, HEADER_RUST
from ..protocol import FieldSpec, ProtocolCatalog


def _pascal(identifier: str) -> str:
    return "".join(part.capitalize() for part in identifier.replace(".", "-").split("-"))


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
    union_names = {union.name for union in catalog.unions}
    domain_names = sorted(
        {
            field.type_name
            for record in catalog.records
            for field in record.fields
            if field.type_name != "Boolean"
            and field.type_name not in record_names
            and field.type_name not in union_names
        }
    )
    lines = [HEADER_RUST.rstrip(), ""]
    lines.extend(_rust_domain_imports(domain_names))
    lines.extend([
        "use serde::{Deserialize, Serialize};",
        "",
        f"pub const MINIMUM_PROTOCOL_VERSION: u16 = {catalog.minimum_version};",
        f"pub const MAXIMUM_PROTOCOL_VERSION: u16 = {catalog.maximum_version};",
        f"pub const MAX_WIRE_MESSAGE_BYTES: usize = {catalog.max_message_bytes:_};",
        f"pub const MAX_JSON_DEPTH: usize = {catalog.max_json_depth:_};",
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
    for union in catalog.unions:
        lines.extend([
            "",
            f"/// {union.description}",
            "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
            f'#[serde(tag = "{union.tag}", content = "{union.content}")]',
            f"pub enum {union.name} {{",
        ])
        for variant in union.variants:
            lines.extend([
                f"    /// {variant.description}",
                f'    #[serde(rename = "{variant.wire}")]',
                f"    {variant.name}({variant.type_name}),",
            ])
        lines.append("}")
    lines.extend([
        "",
        "/// Unnegotiated request envelope used only for the handshake.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(deny_unknown_fields)]",
        "pub struct NegotiationRequestEnvelope {",
        "    pub request_id: RequestId,",
        "    pub params: ClientHello,",
        "}",
        "",
        "/// Request envelope bound to one negotiated bridge connection.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(deny_unknown_fields)]",
        "pub struct SessionRequestEnvelope<T> {",
        "    pub request_id: RequestId,",
        "    pub protocol_session_id: ProtocolSessionId,",
        "    pub negotiation_token: NegotiationToken,",
        "    pub params: T,",
        "}",
        "",
        "/// Typed request envelope; arbitrary method strings or parameters are impossible.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(tag = \"method\", content = \"request\")]",
        "pub enum RpcRequest {",
    ])
    for method in catalog.methods:
        variant = _pascal(method.name)
        envelope = (
            "NegotiationRequestEnvelope"
            if method.name == "negotiate"
            else f"SessionRequestEnvelope<{method.request}>"
        )
        lines.extend([
            f'    #[serde(rename = "{method.name}")]',
            f"    {variant}({envelope}),",
        ])
    lines.extend([
        "}",
        "",
        "/// Successful method response bound to the serving bridge instance.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(deny_unknown_fields)]",
        "pub struct SuccessEnvelope<T> {",
        "    pub request_id: RequestId,",
        "    pub server_instance_id: ServerInstanceId,",
        "    pub result: T,",
        "}",
        "",
        "/// Structured error response bound to the serving bridge instance.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(deny_unknown_fields)]",
        "pub struct ErrorEnvelope {",
        "    pub request_id: Option<RequestId>,",
        "    pub server_instance_id: ServerInstanceId,",
        "    pub error: RpcError,",
        "}",
        "",
        "/// Typed success-or-error response envelope.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(tag = \"type\", content = \"response\")]",
        "pub enum RpcResponse {",
    ])
    for method in catalog.methods:
        variant = f"{_pascal(method.name)}Success"
        lines.extend([
            f'    #[serde(rename = "{method.name}-success")]',
            f"    {variant}(SuccessEnvelope<{method.response}>),",
        ])
    lines.extend([
        "    #[serde(rename = \"error\")]",
        "    Error(ErrorEnvelope),",
        "}",
    ])
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


def cpp_types(catalog: ProtocolCatalog, namespace: str = "hyperflux") -> str:
    lines = [
        HEADER_CPP.rstrip(),
        "",
        "#pragma once",
        "",
        '#include "domain_types.hpp"',
        "",
        "#include <cstddef>",
        "#include <cstdint>",
        "#include <optional>",
        "#include <string_view>",
        "#include <variant>",
        "#include <vector>",
        "",
        f"namespace {namespace}",
        "{",
        "",
        f"inline constexpr std::uint16_t minimum_protocol_version = {catalog.minimum_version};",
        f"inline constexpr std::uint16_t maximum_protocol_version = {catalog.maximum_version};",
        f"inline constexpr std::size_t max_wire_message_bytes = {catalog.max_message_bytes};",
        f"inline constexpr std::size_t max_json_depth = {catalog.max_json_depth};",
    ]
    for record in catalog.records:
        lines.extend(["", f"// {record.description}", f"struct {record.name}", "{"])
        for field in record.fields:
            lines.append(f"    {_cpp_type(field)} {field.name};")
        lines.append(f"    friend bool operator==(const {record.name}&, const {record.name}&) = default;")
        lines.append("};")
    for union in catalog.unions:
        alternatives: list[str] = []
        for variant in union.variants:
            alternative = f"{union.name}{variant.name}"
            alternatives.append(alternative)
            lines.extend([
                "",
                f"// {variant.description}",
                f"struct {alternative}",
                "{",
                f'    static constexpr std::string_view {union.tag} = "{variant.wire}";',
                f"    {variant.type_name} {union.content};",
                f"    friend bool operator==(const {alternative}&, const {alternative}&) = default;",
                "};",
            ])
        lines.extend([
            "",
            f"// {union.description}",
            f"using {union.name} = std::variant<{', '.join(alternatives)}>;",
        ])
    lines.extend([
        "",
        "struct NegotiationRequestEnvelope",
        "{",
        "    RequestId request_id;",
        "    ClientHello params;",
        "};",
        "",
        "template<typename T>",
        "struct SessionRequestEnvelope",
        "{",
        "    RequestId request_id;",
        "    ProtocolSessionId protocol_session_id;",
        "    NegotiationToken negotiation_token;",
        "    T params;",
        "};",
    ])
    request_alternatives: list[str] = []
    for method in catalog.methods:
        wrapper = f"RpcRequest{_pascal(method.name)}"
        request_alternatives.append(wrapper)
        envelope = (
            "NegotiationRequestEnvelope"
            if method.name == "negotiate"
            else f"SessionRequestEnvelope<{method.request}>"
        )
        lines.extend([
            "",
            f"struct {wrapper}",
            "{",
            f'    static constexpr std::string_view method = "{method.name}";',
            f"    {envelope} request;",
            "};",
        ])
    lines.extend([
        "",
        f"using RpcRequest = std::variant<{', '.join(request_alternatives)}>;",
        "",
        "template<typename T>",
        "struct SuccessEnvelope",
        "{",
        "    RequestId request_id;",
        "    ServerInstanceId server_instance_id;",
        "    T result;",
        "};",
        "",
        "struct ErrorEnvelope",
        "{",
        "    std::optional<RequestId> request_id;",
        "    ServerInstanceId server_instance_id;",
        "    RpcError error;",
        "};",
    ])
    response_alternatives: list[str] = []
    for method in catalog.methods:
        wrapper = f"RpcResponse{_pascal(method.name)}Success"
        response_alternatives.append(wrapper)
        lines.extend([
            "",
            f"struct {wrapper}",
            "{",
            f'    static constexpr std::string_view type = "{method.name}-success";',
            f"    SuccessEnvelope<{method.response}> response;",
            "};",
        ])
    response_alternatives.append("ErrorEnvelope")
    lines.extend([
        "",
        f"using RpcResponse = std::variant<{', '.join(response_alternatives)}>;",
    ])
    lines.extend(["", "struct MethodDescriptor", "{", "    std::string_view name;", "    std::string_view request;", "    std::string_view response;", "    std::optional<std::string_view> required_feature;", "};", "", f"inline constexpr MethodDescriptor methods[{len(catalog.methods)}] = {{"])
    for method in catalog.methods:
        feature = "std::nullopt" if method.required_feature is None else f'std::optional<std::string_view>("{method.required_feature}")'
        lines.append(
            f'    {{"{method.name}", "{method.request}", "{method.response}", {feature}}},'
        )
    lines.extend(["};", "", f"}} // namespace {namespace}", ""])
    return "\n".join(lines)


def python_types(catalog: ProtocolCatalog) -> str:
    domain_names = sorted(
        {
            field.type_name
            for record in catalog.records
            for field in record.fields
            if field.type_name != "Boolean"
            and field.type_name not in {candidate.name for candidate in catalog.records}
            and field.type_name not in {candidate.name for candidate in catalog.unions}
        }
    )
    lines = [
        HEADER_PYTHON.rstrip(),
        "",
        "from dataclasses import dataclass",
        "from typing import ClassVar, Generic, TypeAlias, TypeVar",
        "",
        "from .domain_types import (",
    ]
    lines.extend(f"    {name}," for name in domain_names)
    lines.extend([
        ")",
        "",
        "T = TypeVar(\"T\")",
        "",
        f"MINIMUM_PROTOCOL_VERSION = {catalog.minimum_version}",
        f"MAXIMUM_PROTOCOL_VERSION = {catalog.maximum_version}",
        f"MAX_WIRE_MESSAGE_BYTES = {catalog.max_message_bytes}",
        f"MAX_JSON_DEPTH = {catalog.max_json_depth}",
        "SUPPORTED_FEATURES = (",
    ])
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
    for union in catalog.unions:
        alternatives: list[str] = []
        for variant in union.variants:
            alternative = f"{union.name}{variant.name}"
            alternatives.append(alternative)
            exports.append(alternative)
            lines.extend([
                "@dataclass(frozen=True, slots=True)",
                f"class {alternative}:",
                f'    """{variant.description}"""',
                f'    {union.tag.upper()}: ClassVar[str] = "{variant.wire}"',
                f"    {union.content}: {variant.type_name}",
                "",
                "",
            ])
        exports.append(union.name)
        lines.extend([
            f"{union.name}: TypeAlias = {' | '.join(alternatives)}",
            "",
            "",
        ])
    exports.extend([
        "NegotiationRequestEnvelope",
        "SessionRequestEnvelope",
        "SuccessEnvelope",
        "ErrorEnvelope",
    ])
    lines.extend([
        "@dataclass(frozen=True, slots=True)",
        "class NegotiationRequestEnvelope:",
        "    request_id: RequestId",
        "    params: ClientHello",
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class SessionRequestEnvelope(Generic[T]):",
        "    request_id: RequestId",
        "    protocol_session_id: ProtocolSessionId",
        "    negotiation_token: NegotiationToken",
        "    params: T",
        "",
        "",
    ])
    request_variants: list[str] = []
    for method in catalog.methods:
        variant = f"RpcRequest{_pascal(method.name)}"
        request_variants.append(variant)
        exports.append(variant)
        envelope = (
            "NegotiationRequestEnvelope"
            if method.name == "negotiate"
            else f"SessionRequestEnvelope[{method.request}]"
        )
        lines.extend([
            "@dataclass(frozen=True, slots=True)",
            f"class {variant}:",
            f'    METHOD: ClassVar[str] = "{method.name}"',
            f"    request: {envelope}",
            "",
            "",
        ])
    exports.append("RpcRequest")
    lines.extend([
        f"RpcRequest: TypeAlias = {' | '.join(request_variants)}",
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class SuccessEnvelope(Generic[T]):",
        "    request_id: RequestId",
        "    server_instance_id: ServerInstanceId",
        "    result: T",
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class ErrorEnvelope:",
        "    request_id: RequestId | None",
        "    server_instance_id: ServerInstanceId",
        "    error: RpcError",
        "",
        "",
    ])
    response_variants: list[str] = []
    for method in catalog.methods:
        variant = f"RpcResponse{_pascal(method.name)}Success"
        response_variants.append(variant)
        exports.append(variant)
        lines.extend([
            "@dataclass(frozen=True, slots=True)",
            f"class {variant}:",
            f'    TYPE: ClassVar[str] = "{method.name}-success"',
            f"    response: SuccessEnvelope[{method.response}]",
            "",
            "",
        ])
    exports.append("RpcResponse")
    response_variants.append("ErrorEnvelope")
    lines.extend([
        f"RpcResponse: TypeAlias = {' | '.join(response_variants)}",
        "",
        "",
    ])
    lines.extend(["METHODS = ("])
    for method in catalog.methods:
        feature = "None" if method.required_feature is None else f'"{method.required_feature}"'
        lines.append(
            f'    ("{method.name}", "{method.request}", "{method.response}", {feature}),'
        )
    lines.extend([
        ")",
        "",
        "__all__ = [",
        '    "MAXIMUM_PROTOCOL_VERSION",',
        '    "MAX_JSON_DEPTH",',
        '    "MAX_WIRE_MESSAGE_BYTES",',
        '    "METHODS",',
        '    "MINIMUM_PROTOCOL_VERSION",',
        '    "SUPPORTED_FEATURES",',
    ])
    lines.extend(f'    "{name}",' for name in exports)
    lines.extend(["]", ""])
    return "\n".join(lines)


def markdown(catalog: ProtocolCatalog, title: str = "Bridge Protocol") -> str:
    lines = [
        f"# {title}",
        "",
        "> Generated by `./hfx generate`. Do not edit manually.",
        "",
        f"Supported protocol versions: **{catalog.minimum_version} through {catalog.maximum_version}**.",
        "",
        f"Maximum encoded message: **{catalog.max_message_bytes} bytes**. Maximum JSON nesting: "
        f"**{catalog.max_json_depth} levels**.",
        "",
        "## Clock And Ordering Semantics",
        "",
        "`MonotonicMs` values belong to the serving bridge process monotonic clock. They are "
        "comparable only while `server_instance_id` remains unchanged; they are never wall-clock "
        "timestamps and must not be retained across bridge restart.",
        "",
        "Lease expiries and transaction deadlines are exclusive absolute instants in that clock "
        "domain. Transaction `frame_index` values are contiguous, zero-based, and ordered within "
        "one atomic request. Terminal outcomes are immutable; a client looks them up instead of "
        "replaying a request after uncertain transport.",
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
    if catalog.unions:
        lines.extend(["## Tagged Unions", ""])
    for union in catalog.unions:
        lines.extend([f"### {union.name}", "", union.description, ""])
        lines.extend(["| Tag | Variant | Payload |", "| --- | --- | --- |"])
        for variant in union.variants:
            lines.append(
                f"| `{variant.wire}` | `{variant.name}` | `{variant.type_name}` |"
            )
        lines.append("")
    return "\n".join(lines)
